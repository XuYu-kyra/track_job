#!/usr/bin/env python3
"""Build application-funnel analytics after enough real observations exist."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from common import read_json, write_json
except ModuleNotFoundError:
    from scripts.common import read_json, write_json


STAGE_RANK = {
    "": 0,
    "WATCH": 0,
    "HOLD": 0,
    "READY": 0,
    "MUST_APPLY": 0,
    "APPLIED": 1,
    "OA": 2,
    "AI_INTERVIEW": 2,
    "INTERVIEW": 3,
    "TECHNICAL_INTERVIEW": 3,
    "HR_INTERVIEW": 3,
    "OFFER": 4,
    "REJECTED": 1,
    "WITHDRAWN": 1,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build job-search conversion analytics.")
    parser.add_argument("--jobs", default="data/job_cache/decisioned_jobs.json")
    parser.add_argument("--output", default="data/job_cache/analytics.json")
    parser.add_argument("--min-applied-sample", type=int, default=10)
    return parser.parse_args()


def _stage_rank(job: dict[str, Any]) -> int:
    stage = str(job.get("stage") or "").upper()
    if job.get("offer"):
        return 4
    if job.get("applied_at") and STAGE_RANK.get(stage, 0) == 0:
        return 1
    return STAGE_RANK.get(stage, 0)


def _dimension(jobs: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        raw = job.get(field) or "unknown"
        values = raw if isinstance(raw, list) else [raw]
        for value in values:
            groups.setdefault(str(value), []).append(job)
    result: dict[str, dict[str, Any]] = {}
    for name, items in sorted(groups.items()):
        applied = sum(1 for item in items if _stage_rank(item) >= 1)
        oa = sum(1 for item in items if _stage_rank(item) >= 2)
        interview = sum(1 for item in items if _stage_rank(item) >= 3)
        offer = sum(1 for item in items if _stage_rank(item) >= 4)
        result[name] = {
            "discovered": len(items),
            "applied": applied,
            "oa": oa,
            "interview": interview,
            "offer": offer,
            "applied_to_interview": round(interview / applied, 3) if applied else None,
            "interview_to_offer": round(offer / interview, 3) if interview else None,
        }
    return result


def build_analytics(jobs: list[dict[str, Any]], min_applied_sample: int) -> dict[str, Any]:
    applied_count = sum(1 for job in jobs if _stage_rank(job) >= 1)
    reject_reasons = Counter(
        str(job.get("reject_reason"))
        for job in jobs
        if job.get("reject_reason")
    )
    return {
        "sample": {
            "job_count": len(jobs),
            "applied_count": applied_count,
            "minimum_applied_sample": min_applied_sample,
            "conversion_analysis_enabled": applied_count >= min_applied_sample,
        },
        "by_role_family": _dimension(jobs, "role_family"),
        "by_source": _dimension(jobs, "sources"),
        "by_discovered_by": _dimension(jobs, "discovered_by"),
        "by_discovery_source": _dimension(jobs, "discovery_sources"),
        "by_resume_family": _dimension(jobs, "resume_family"),
        "reject_reasons": dict(reject_reasons.most_common()),
    }


def main() -> None:
    args = parse_args()
    jobs = read_json(args.jobs, [])
    analytics = build_analytics(jobs, args.min_applied_sample)
    write_json(Path(args.output), analytics)
    print(
        f"Analytics -> {args.output}; applied={analytics['sample']['applied_count']}; "
        f"conversion_enabled={analytics['sample']['conversion_analysis_enabled']}"
    )


if __name__ == "__main__":
    main()
