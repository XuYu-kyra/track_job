#!/usr/bin/env python3
"""Maintain promotion candidates discovered outside the company registry."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from ats_detection import detect_ats_family
    from common import load_config, normalize_token, read_json, write_json
    from job_schema import is_verified_open_job
    from source_adapters import company_in_registry, load_source_registry, registry_company_names
except ModuleNotFoundError:
    from scripts.ats_detection import detect_ats_family
    from scripts.common import load_config, normalize_token, read_json, write_json
    from scripts.job_schema import is_verified_open_job
    from scripts.source_adapters import (
        company_in_registry,
        load_source_registry,
        registry_company_names,
    )


PROMOTION_STATUSES = frozenset(
    {
        "DISCOVERED",
        "PERSISTENT_MONITORING",
        "PROMOTED_TO_PERSISTENT_MONITORING",
        "PROMOTED_TO_REGISTRY",
    }
)
CONFIDENCE_RANK = {"UNKNOWN": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _valid_url(value: Any) -> str:
    candidate = str(value or "").strip()
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return ""
    return candidate if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def _job_identity(job: dict[str, Any]) -> str:
    return str(
        job.get("canonical_key")
        or job.get("canonical_url")
        or job.get("official_url")
        or job.get("url")
        or f"{job.get('title')}|{job.get('location')}"
    )


def _observed_on(job: dict[str, Any], as_of: str) -> bool:
    return any(
        str(job.get(field) or "")[:10] == as_of
        for field in ("first_seen", "last_observed", "last_verified", "observed_at", "discovered_at")
    )


def _trusted_official_url(job: dict[str, Any]) -> str:
    if str(job.get("canonicality") or "").upper() != "CANONICAL":
        return ""
    if str(job.get("verification_state") or "") != "VERIFIED_OPEN_JOB":
        return ""
    return _valid_url(job.get("canonical_url") or job.get("official_url"))


def _job_summary(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "canonical_key": str(job.get("canonical_key") or ""),
        "title": str(job.get("title") or job.get("position") or ""),
        "location": str(job.get("location") or ""),
        "role_family": str(job.get("role_family") or "other"),
        "action": str(job.get("action") or ""),
        "source": str(job.get("source") or ""),
        "sources": list(dict.fromkeys(_strings(job.get("sources")))),
        "discovery_query": str(
            job.get("discovery_query") or job.get("search_keyword") or ""
        ),
        "verification_state": str(job.get("verification_state") or ""),
        "canonicality": str(job.get("canonicality") or ""),
        "canonical_url": str(job.get("canonical_url") or ""),
        "official_url": str(job.get("official_url") or ""),
        "job_id": str(job.get("job_id") or ""),
        "status": str(job.get("status") or ""),
        "freshness": str(job.get("freshness") or ""),
        "evidence_confidence": str(
            job.get("evidence_confidence") or job.get("source_confidence") or "UNKNOWN"
        ),
        "jd_character_count": len(str(job.get("description") or "").strip()),
        "ready_verified": is_verified_open_job(job),
        "source_urls": list(dict.fromkeys(_strings(job.get("source_urls")))),
        "first_seen": str(job.get("first_seen") or ""),
        "last_seen": str(
            job.get("last_observed") or job.get("last_verified") or job.get("first_seen") or ""
        ),
    }


def _confidence(jobs: list[dict[str, Any]]) -> str:
    values = [
        str(job.get("evidence_confidence") or job.get("source_confidence") or "UNKNOWN").upper()
        for job in jobs
    ]
    return max(values or ["UNKNOWN"], key=lambda item: CONFIDENCE_RANK.get(item, 0))


def _evidence(jobs: list[dict[str, Any]], *, kind: str) -> list[str]:
    values: list[str] = []
    for job in jobs:
        if kind == "shenzhen":
            if any(token in str(job.get("location") or "").casefold() for token in ("深圳", "shenzhen")):
                values.append(f"location:{job.get('location')}")
            values.extend(_strings(job.get("location_evidence")))
        else:
            values.extend(_strings(job.get("graduation_evidence")))
            for field in ("title", "campaign_context", "source_context"):
                text = str(job.get(field) or "")
                if "2027" in text or "27届" in text:
                    values.append(f"{field}:{text[:180]}")
    return list(dict.fromkeys(value for value in values if value))[:20]


def build_discovered_company_store(
    jobs: list[dict[str, Any]],
    registry: dict[str, Any],
    targets: dict[str, Any],
    *,
    existing: dict[str, Any] | None = None,
    as_of: str,
) -> dict[str, Any]:
    known_names = registry_company_names(registry)
    target_roles = set(
        str(value)
        for key in ("preferred_role_families", "expansion_role_families")
        for value in targets.get("candidate_profile", {}).get(key, [])
    )
    monitor_interval_days = max(
        1,
        int(
            targets.get("open_market_discovery", {}).get(
                "dynamic_company_cycle_days", 3
            )
        ),
    )
    existing = existing if isinstance(existing, dict) else {}
    previous_candidates = {
        str(item.get("company_key") or ""): item
        for item in existing.get("companies", [])
        if isinstance(item, dict) and item.get("company_key")
    }
    promotions = [
        item for item in existing.get("promotions", []) if isinstance(item, dict)
    ]

    # If an earlier dynamic candidate now exists in the registry, retain an
    # auditable promotion event while removing it from the unregistered set.
    already_registry_promoted = {
        str(item.get("company_key") or "")
        for item in promotions
        if item.get("status") == "PROMOTED_TO_REGISTRY"
    }
    for key, item in list(previous_candidates.items()):
        if company_in_registry(str(item.get("normalized_name") or ""), known_names):
            if key not in already_registry_promoted:
                promotions.append(
                    {
                        "company_key": key,
                        "normalized_name": item.get("normalized_name"),
                        "promoted_at": as_of,
                        "status": "PROMOTED_TO_REGISTRY",
                        "promotion_reason": list(item.get("promotion_reason") or []),
                    }
                )
            previous_candidates.pop(key, None)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        company = str(job.get("company") or "").strip()
        key = normalize_token(company)
        if not key or company_in_registry(company, known_names):
            continue
        grouped.setdefault(key, []).append(job)

    candidates: list[dict[str, Any]] = []
    for key in sorted(set(previous_candidates) | set(grouped)):
        old = previous_candidates.get(key) or {}
        current_jobs = grouped.get(key, [])
        aliases = list(dict.fromkeys([
            *_strings(old.get("aliases")),
            *(str(job.get("company") or "").strip() for job in current_jobs),
            *(
                alias
                for job in current_jobs
                for alias in _strings(job.get("company_aliases"))
            ),
        ]))
        normalized_name = str(old.get("normalized_name") or (aliases[0] if aliases else key))
        combined_jobs = {
            str(item.get("canonical_key") or item.get("source_urls") or item.get("title") or ""): item
            for item in old.get("discovered_jobs", [])
            if isinstance(item, dict)
        }
        for job in current_jobs:
            combined_jobs[_job_identity(job)] = _job_summary(job)
        job_summaries = list(combined_jobs.values())

        observed_runs = _strings(old.get("observed_fresh_runs"))
        if any(_observed_on(job, as_of) for job in current_jobs):
            observed_runs.append(as_of)
        observed_runs = sorted(set(observed_runs))
        first_seen_dates = [
            str(value)[:10]
            for job in current_jobs
            for value in (job.get("first_seen"), job.get("discovered_at"))
            if value
        ]
        last_seen_dates = [
            str(value)[:10]
            for job in current_jobs
            for value in (
                job.get("last_observed"),
                job.get("last_verified"),
                job.get("observed_at"),
                job.get("first_seen"),
                job.get("discovered_at"),
            )
            if value
        ]
        first_seen = min(
            [value for value in [str(old.get("first_seen") or ""), *first_seen_dates] if value]
            or [as_of]
        )
        last_seen = max(
            [value for value in [str(old.get("last_seen") or ""), *last_seen_dates] if value]
            or [first_seen]
        )
        official_urls = list(dict.fromkeys(filter(None, [
            str(old.get("verified_official_career_url") or ""),
            *(_trusted_official_url(job) for job in current_jobs),
        ])))
        official_url_first_verified = str(old.get("official_url_first_verified") or "")
        if official_urls and not official_url_first_verified:
            official_url_first_verified = as_of
        ats_families = list(dict.fromkeys(filter(None, [
            str(old.get("detected_ats_family") or ""),
            *(
                str(job.get("ats_family") or detect_ats_family(
                    job.get("canonical_url") or job.get("official_url") or job.get("url") or ""
                ))
                for job in current_jobs
            ),
        ])))
        ats_families = [value for value in ats_families if value != "UNKNOWN"]
        target_jobs = [
            item for item in job_summaries if str(item.get("role_family") or "") in target_roles
        ]
        shenzhen_evidence = list(dict.fromkeys([
            *_strings(old.get("shenzhen_evidence")),
            *_evidence(current_jobs, kind="shenzhen"),
        ]))[:20]
        graduation_evidence = list(dict.fromkeys([
            *_strings(old.get("graduation_2027_evidence")),
            *_evidence(current_jobs, kind="graduation"),
        ]))[:20]
        reasons: list[str] = []
        if official_urls:
            reasons.append("verified official career/ATS URL found")
        if len(observed_runs) >= 2:
            reasons.append("observed in multiple fresh runs")
        if len(target_jobs) >= 2:
            reasons.append("multiple target-role jobs observed")
        if any(
            item.get("action") in {"READY", "MUST_APPLY"}
            and bool(item.get("ready_verified"))
            for item in job_summaries
        ):
            reasons.append("READY/MUST_APPLY-quality opportunity observed")
        old_status = str(old.get("status") or "DISCOVERED")
        persistent_monitoring = bool(reasons) or old_status == "PERSISTENT_MONITORING"
        persistent_since = str(old.get("persistent_monitoring_since") or "")
        if persistent_monitoring and not persistent_since:
            persistent_since = as_of
        if persistent_monitoring and old_status != "PERSISTENT_MONITORING":
            promotions.append(
                {
                    "company_key": key,
                    "normalized_name": normalized_name,
                    "promoted_at": as_of,
                    "status": "PROMOTED_TO_PERSISTENT_MONITORING",
                    "promotion_reason": reasons,
                }
            )
        current_confidence = _confidence(current_jobs) if current_jobs else "UNKNOWN"
        old_confidence = str(old.get("source_confidence") or "UNKNOWN").upper()
        source_confidence = max(
            (current_confidence, old_confidence),
            key=lambda item: CONFIDENCE_RANK.get(item, 0),
        )
        candidates.append(
            {
                "company_key": key,
                "normalized_name": normalized_name,
                "aliases": aliases,
                "discovered_jobs": job_summaries,
                "verified_official_career_url": official_urls[0] if official_urls else "",
                "official_url_first_verified": official_url_first_verified,
                "detected_ats_family": ats_families[0] if ats_families else "UNKNOWN",
                "first_seen": first_seen,
                "last_seen": last_seen,
                "observed_fresh_runs": observed_runs,
                "observation_count": len(observed_runs),
                "discovered_job_count": len(job_summaries),
                "target_role_job_count": len(target_jobs),
                "target_role_count": len(target_jobs),
                "sources": sorted({
                    source
                    for item in job_summaries
                    for source in [
                        str(item.get("source") or ""),
                        *[str(value) for value in item.get("sources") or []],
                    ]
                    if source
                }),
                "providers": sorted({
                    str(item.get("source") or "")
                    for item in job_summaries
                    if item.get("source")
                }),
                "discovery_queries": sorted({
                    str(item.get("discovery_query") or "")
                    for item in job_summaries
                    if item.get("discovery_query")
                }),
                "official_url": official_urls[0] if official_urls else "",
                "canonical_url": next(
                    (
                        str(item.get("canonical_url") or "")
                        for item in job_summaries
                        if item.get("canonical_url")
                    ),
                    "",
                ),
                "ats_family": ats_families[0] if ats_families else "UNKNOWN",
                "shenzhen_evidence": shenzhen_evidence,
                "graduation_2027_evidence": graduation_evidence,
                "source_confidence": source_confidence,
                "promotion_reason": reasons,
                "status": "PERSISTENT_MONITORING" if persistent_monitoring else "DISCOVERED",
                "promotion_status": (
                    "PERSISTENT_MONITORING" if persistent_monitoring else "DISCOVERED"
                ),
                "persistent_monitoring_since": persistent_since,
                "monitor_interval_days": (
                    monitor_interval_days if persistent_monitoring else None
                ),
            }
        )

    return {
        "schema_version": "1.0",
        "as_of": as_of,
        "registry_is_allowlist": False,
        "unregistered_companies_allowed": True,
        "companies": candidates,
        "promotions": promotions,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update unregistered-company discovery candidates.")
    parser.add_argument("--jobs", default="data/job_cache/decisioned_jobs.json")
    parser.add_argument("--source-registry", default="config/company_registry.yaml")
    parser.add_argument("--targets-config", default="config/targets.yaml")
    parser.add_argument("--output", default="data/job_cache/discovered_company_candidates.json")
    parser.add_argument("--existing", default="")
    parser.add_argument("--as-of", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = Path(args.output)
    payload = build_discovered_company_store(
        read_json(Path(args.jobs), []),
        load_source_registry(args.source_registry),
        load_config(args.targets_config),
        existing=read_json(Path(args.existing), {}) if args.existing else read_json(output, {}),
        as_of=args.as_of or date.today().isoformat(),
    )
    write_json(output, payload)
    recommended = sum(
        item.get("status") == "PERSISTENT_MONITORING"
        for item in payload["companies"]
    )
    print(
        f"Discovered company candidates: {len(payload['companies'])}; "
        f"persistent monitoring={recommended}; promotion events={len(payload['promotions'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
