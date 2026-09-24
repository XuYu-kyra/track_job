#!/usr/bin/env python3
"""Normalize, merge, deduplicate, and age all configured job sources."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
from typing import Any

try:
    from common import load_config, normalize_token, read_json, write_json
    from job_schema import (
        company_alias_tokens,
        canonicalize_url,
        dedup_identity_keys,
        freshness_status,
        identity_url_candidates,
        is_active_process_stage,
        merge_job_records,
    )
    from source_adapters import get_source_adapter, load_source_registry
except ModuleNotFoundError:
    from scripts.common import load_config, normalize_token, read_json, write_json
    from scripts.job_schema import (
        company_alias_tokens,
        canonicalize_url,
        dedup_identity_keys,
        freshness_status,
        identity_url_candidates,
        is_active_process_stage,
        merge_job_records,
    )
    from scripts.source_adapters import get_source_adapter, load_source_registry


DEFAULT_INPUTS = [
    "data/job_cache/linkedin_jobs.json",
    "data/job_cache/indeed_jobs.json",
    "data/job_cache/manual_import_jobs.json",
    "data/job_cache/manual_indeed_jobs.json",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalize and merge job source files.")
    parser.add_argument("--inputs", nargs="+", default=DEFAULT_INPUTS)
    parser.add_argument("--output", default="data/job_cache/jobs.json")
    parser.add_argument("--history", default="data/job_cache/job_history.json")
    parser.add_argument("--taxonomy-config", default="config/taxonomies.yaml")
    parser.add_argument("--targets-config", default="config/targets.yaml")
    parser.add_argument("--source-registry", default="config/company_registry.yaml")
    parser.add_argument("--as-of", default="", help="ISO date override for deterministic runs")
    parser.add_argument(
        "--human-state",
        default="",
        help="Optional read-only Feishu human lifecycle state exported as JSON",
    )
    return parser.parse_args()


HUMAN_STATE_FIELDS = (
    "applied_at",
    "stage",
    "action",
    "reject_reason",
    "next_action",
    "next_deadline",
    "round",
    "team",
    "manager_signal",
    "questions",
    "weak_topics",
    "debrief",
    "offer",
)


def _state_match_keys(job: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    url = canonicalize_url(job.get("official_url") or job.get("url"))
    if url:
        keys.add(f"url:{url}")
    canonical = str(job.get("canonical_key") or "").strip()
    if canonical:
        keys.add(f"key:{canonical}")
    company = normalize_token(str(job.get("company") or ""))
    title = normalize_token(str(job.get("title") or job.get("position") or ""))
    location = normalize_token(str(job.get("location") or ""))
    if company and title:
        keys.add(f"identity:{company}|{title}|{location}")
    return keys


def apply_human_state(jobs: list[dict[str, Any]], state: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Overlay non-empty Feishu decisions without changing factual job fields."""

    by_key: dict[str, dict[str, Any]] = {}
    for item in state:
        if not isinstance(item, dict):
            continue
        human_fields = item.get("human_fields")
        if not isinstance(human_fields, dict):
            continue
        keys: set[str] = set()
        url = canonicalize_url(item.get("official_url"))
        if url:
            keys.add(f"url:{url}")
        canonical = str(item.get("canonical_key") or "").strip()
        if canonical:
            keys.add(f"key:{canonical}")
        company = normalize_token(str(item.get("company") or ""))
        title = normalize_token(str(item.get("title") or ""))
        location = normalize_token(str(item.get("location") or ""))
        if company and title:
            keys.add(f"identity:{company}|{title}|{location}")
        for key in keys:
            by_key[key] = human_fields

    for job in jobs:
        overlays: list[dict[str, Any]] = []
        seen: set[int] = set()
        for key in _state_match_keys(job):
            candidate = by_key.get(key)
            if candidate is not None and id(candidate) not in seen:
                overlays.append(candidate)
                seen.add(id(candidate))
        # Prefer the most specific matching state.  A URL/key match is already
        # represented first by insertion order; merging all matches preserves
        # fields when one record only has an identity fallback.
        for overlay in overlays:
            for field in HUMAN_STATE_FIELDS:
                value = overlay.get(field)
                if value not in (None, "", []):
                    job[field] = value
        # Older rows may carry the applied decision in Action while Stage was
        # never populated.  Treat that explicit user-action value as APPLIED
        # so scoring cannot downgrade it to WATCH on the next run.
        if not str(job.get("stage") or "").strip() and str(job.get("action") or "").upper() == "APPLIED":
            job["stage"] = "APPLIED"
        if job.get("stage") in {"APPLIED", "WITHDRAWN"}:
            job["human_decision_source"] = "feishu_readback"
    return jobs


def merge_jobs(
    inputs: list[str],
    *,
    taxonomies: dict[str, Any] | None = None,
    previous_jobs: list[dict[str, Any]] | None = None,
    as_of: str | None = None,
    stale_after_days: int = 14,
    source_registry: dict[str, Any] | None = None,
    human_state: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    observed_on = as_of or date.today().isoformat()
    taxonomies = taxonomies or {}
    previous_records = [job for job in (previous_jobs or []) if isinstance(job, dict)]
    alias_tokens = company_alias_tokens(source_registry)
    groups: list[dict[str, Any]] = []
    observed_previous: set[int] = set()

    def identities(record: dict[str, Any]) -> dict[str, str]:
        return dedup_identity_keys(record, alias_tokens)

    def records_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
        left_keys = identities(left)
        right_keys = identities(right)
        left_canonical = left_keys["canonical"]
        right_canonical = right_keys["canonical"]
        if left_canonical and right_canonical:
            return left_canonical == right_canonical
        if left_canonical or right_canonical:
            canonical = left_canonical or right_canonical
            provisional = right if left_canonical else left
            if canonical.removeprefix("url:") in identity_url_candidates(provisional):
                return True
            # A source-namespaced requisition ID can attach an earlier record
            # that did not yet carry the now-verified URL.
            return bool(
                left_keys["stable"]
                and left_keys["stable"] == right_keys["stable"]
            )
        # Preserve idempotence when an older fallback-only history record is
        # later enriched with a stable source job ID. The shared public job URL
        # is still the same observation and must not create a second record.
        if identity_url_candidates(left) & identity_url_candidates(right):
            left_fallback = left_keys["fallback"].removeprefix("fallback:").split("|")
            right_fallback = right_keys["fallback"].removeprefix("fallback:").split("|")
            if left_fallback[:3] == right_fallback[:3]:
                return True
        if left_keys["stable"] or right_keys["stable"]:
            return bool(
                left_keys["stable"]
                and left_keys["stable"] == right_keys["stable"]
            )
        return bool(
            left_keys["fallback"]
            and left_keys["fallback"] == right_keys["fallback"]
        )

    def find_matches(
        records: list[dict[str, Any]], candidate: dict[str, Any]
    ) -> list[int]:
        matches = [
            index
            for index, record in enumerate(records)
            if records_match(record, candidate)
        ]
        candidate_canonical = identities(candidate)["canonical"]
        matched_canonicals = {
            identities(records[index])["canonical"]
            for index in matches
            if identities(records[index])["canonical"]
        }
        # An unverified record listing multiple URLs must not bridge two
        # independently verified canonical jobs.
        if not candidate_canonical and len(matched_canonicals) > 1:
            return [
                index
                for index in matches
                if not identities(records[index])["canonical"]
            ]
        return matches

    def merge_into_groups(candidate: dict[str, Any]) -> None:
        match_indices = find_matches(groups, candidate)
        if not match_indices:
            groups.append(candidate)
            return
        consolidated = candidate
        for index in match_indices:
            consolidated = merge_job_records(groups[index], consolidated)
        insertion_index = min(match_indices)
        matched = set(match_indices)
        groups[:] = [
            record for index, record in enumerate(groups) if index not in matched
        ]
        groups.insert(insertion_index, consolidated)

    for input_path in inputs:
        jobs = read_json(input_path, [])
        if not isinstance(jobs, list):
            raise ValueError(f"Expected a list of jobs in {input_path}")
        for raw_job in jobs:
            if not isinstance(raw_job, dict):
                continue
            adapter = get_source_adapter(
                str(raw_job.get("source") or "manual"), source_registry
            )
            first_pass = adapter.normalize(
                raw_job,
                taxonomies,
                as_of=observed_on,
                stale_after_days=stale_after_days,
            )
            previous_indices = find_matches(previous_records, first_pass)
            previous: dict[str, Any] | None = None
            for previous_index in previous_indices:
                observed_previous.add(previous_index)
                previous = (
                    merge_job_records(previous, previous_records[previous_index])
                    if previous is not None
                    else previous_records[previous_index]
                )
            normalized = adapter.normalize(
                raw_job,
                taxonomies,
                as_of=observed_on,
                previous=previous,
                stale_after_days=stale_after_days,
            )
            if previous is None:
                normalized["discovery_state"] = "NEW"
            else:
                changed = any(
                    str(first_pass.get(field) or "") != str(previous.get(field) or "")
                    for field in ("title", "location", "description", "status", "deadline", "canonical_url")
                )
                normalized["discovery_state"] = "UPDATED" if changed else "HISTORICAL"
            if previous is not None:
                normalized = merge_job_records(previous, normalized)
                normalized["discovery_state"] = "UPDATED" if changed else "HISTORICAL"
            merge_into_groups(normalized)

    for index, previous in enumerate(previous_records):
        if index in observed_previous:
            continue
        historical = dict(previous)
        historical["discovery_state"] = "HISTORICAL"
        historical["freshness"] = freshness_status(
            str(historical.get("last_verified") or historical.get("last_observed") or ""),
            observed_on,
            stale_after_days,
        )
        if historical["freshness"] == "stale" and not is_active_process_stage(
            historical.get("stage")
        ):
            if str(historical.get("status") or "").upper() not in {"CLOSED", "EXPIRED"}:
                historical["status"] = "STALE"
            historical["action"] = "WATCH"
        merge_into_groups(historical)

    for record in groups:
        keys = identities(record)
        record["canonical_key"] = (
            keys["canonical"] or keys["stable"] or keys["fallback"] or record.get("canonical_key", "")
        )
        record["freshness"] = freshness_status(
            str(record.get("last_verified") or record.get("last_observed") or ""),
            observed_on,
            stale_after_days,
        )
    merged = groups
    apply_human_state(merged, human_state or [])
    merged.sort(
        key=lambda job: (
            job.get("freshness") != "fresh",
            str(job.get("company", "")),
            str(job.get("title", "")),
        )
    )
    return merged


def main() -> None:
    args = parse_args()
    taxonomies = load_config(args.taxonomy_config)
    targets_path = Path(args.targets_config)
    targets = load_config(targets_path) if targets_path.exists() else {}
    stale_days = int(targets.get("operating_mode", {}).get("stale_after_days", 14))
    previous = read_json(args.history, [])
    source_registry = load_source_registry(args.source_registry)
    merged = merge_jobs(
        args.inputs,
        taxonomies=taxonomies,
        previous_jobs=previous,
        as_of=args.as_of or None,
        stale_after_days=stale_days,
        source_registry=source_registry,
        human_state=read_json(args.human_state, []) if args.human_state else [],
    )
    write_json(Path(args.output), merged)
    write_json(Path(args.history), merged)
    fresh_count = sum(1 for job in merged if job.get("freshness") == "fresh")
    stale_count = sum(1 for job in merged if job.get("freshness") == "stale")
    print(f"Wrote {len(merged)} canonical jobs to {args.output} (fresh={fresh_count}, stale={stale_count})")


if __name__ == "__main__":
    main()
