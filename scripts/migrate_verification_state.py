#!/usr/bin/env python3
"""Safely derive verification state for legacy job caches.

The migration is deliberately fail-closed: a canonical URL alone is not proof
that the URL was checked by an authoritative observer.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

try:
    from common import write_json
    from job_schema import (
        TRUSTED_CANONICAL_AUTHORITIES,
        canonicalize_url,
        freshness_status,
        is_verified_open_job,
        verification_state_for,
    )
except ModuleNotFoundError:
    from scripts.common import write_json
    from scripts.job_schema import (
        TRUSTED_CANONICAL_AUTHORITIES,
        canonicalize_url,
        freshness_status,
        is_verified_open_job,
        verification_state_for,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fail-closed legacy verification migration.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", default="")
    parser.add_argument("--report", default="")
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _authoritative_canonical_evidence(job: dict[str, Any]) -> bool:
    authority = str(job.get("canonical_authority") or "").upper()
    explicit = bool(job.get("source_authoritative")) or authority in TRUSTED_CANONICAL_AUTHORITIES
    observations = job.get("source_observations") or []
    observed = any(
        isinstance(item, dict)
        and bool(item.get("authoritative"))
        and str(item.get("canonicality") or "").upper() == "CANONICAL"
        and str(item.get("verification_level") or "").upper() == "CANONICAL"
        and bool(item.get("verified_at"))
        for item in observations
    )
    return bool(
        canonicalize_url(job.get("canonical_url") or job.get("official_url"))
        and str(job.get("canonicality") or "").upper() == "CANONICAL"
        and (explicit or observed)
        and (job.get("last_verified") or job.get("verified_at"))
    )


def migrate_job(job: dict[str, Any], *, as_of: str) -> dict[str, Any]:
    migrated = dict(job)
    last_verified = str(job.get("last_verified") or job.get("verified_at") or "")[:10]
    freshness = freshness_status(last_verified, as_of) if last_verified else "unknown"
    migrated["freshness"] = freshness
    migrated["verification_state"] = verification_state_for(
        status=job.get("status"),
        canonical_verified=_authoritative_canonical_evidence(job),
        freshness=freshness,
        description=job.get("description"),
    )
    if (
        str(job.get("action") or job.get("action_tier") or "").upper()
        in {"READY", "MUST_APPLY"}
        and not is_verified_open_job(migrated)
    ):
        migrated["action"] = "WATCH"
        if "action_tier" in migrated:
            migrated["action_tier"] = "WATCH"
        migrated["verification_gate_reason"] = (
            "legacy record lacks current authoritative VERIFIED_OPEN_JOB evidence"
        )
    return migrated


def migrate_jobs(jobs: list[dict[str, Any]], *, as_of: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    migrated = [migrate_job(job, as_of=as_of) for job in jobs]
    prior_ready = [
        job for job in jobs
        if str(job.get("action") or job.get("action_tier") or "").upper()
        in {"READY", "MUST_APPLY"}
    ]
    transitions = []
    for old, new in zip(jobs, migrated):
        if old in prior_ready:
            transitions.append(
                {
                    "canonical_key": old.get("canonical_key"),
                    "company": old.get("company"),
                    "title": old.get("title") or old.get("position"),
                    "old_action": old.get("action") or old.get("action_tier"),
                    "new_action": new.get("action") or new.get("action_tier"),
                    "new_verification_state": new.get("verification_state"),
                }
            )
    return migrated, {
        "as_of": as_of,
        "input_count": len(jobs),
        "prior_ready_count": len(prior_ready),
        "unsafe_ready_after": sum(
            str(job.get("action") or job.get("action_tier") or "").upper()
            in {"READY", "MUST_APPLY"}
            and not is_verified_open_job(job)
            for job in migrated
        ),
        "ready_transitions": transitions,
    }


def main() -> int:
    args = parse_args()
    source = Path(args.input).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise ValueError("legacy cache must be a JSON list of job objects")
    migrated, report = migrate_jobs(payload, as_of=args.as_of)
    if not args.dry_run:
        if not args.output:
            raise ValueError("--output is required unless --dry-run is used")
        destination = Path(args.output).resolve()
        if destination == source:
            raise ValueError("migration refuses to overwrite its input")
        if destination.exists():
            raise FileExistsError(f"migration output already exists: {destination}")
        write_json(destination, migrated)
    if args.report:
        report_path = Path(args.report).resolve()
        if report_path.exists():
            raise FileExistsError(f"migration report already exists: {report_path}")
        write_json(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
