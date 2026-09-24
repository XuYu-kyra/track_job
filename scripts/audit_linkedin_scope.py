#!/usr/bin/env python3
"""Dry-run the LinkedIn-only location policy against historical decisions."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from common import load_config, read_json, write_json
    from linkedin_location import is_direct_linkedin_job, linkedin_location_scope
    from score_jobs import evaluate_job
    from source_adapters import load_source_registry
except ModuleNotFoundError:
    from scripts.common import load_config, read_json, write_json
    from scripts.linkedin_location import is_direct_linkedin_job, linkedin_location_scope
    from scripts.score_jobs import evaluate_job
    from scripts.source_adapters import load_source_registry


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit historical LinkedIn locations without writing Feishu.")
    parser.add_argument("--config", default="config/targets.yaml")
    parser.add_argument("--scoring-config", default="config/scoring.yaml")
    parser.add_argument("--taxonomy-config", default="config/taxonomies.yaml")
    parser.add_argument("--source-registry", default="config/company_registry.yaml")
    parser.add_argument("--inventory", default="data/job_cache/candidate_inventory.json")
    parser.add_argument("--input", default="data/job_cache/decisioned_jobs.json")
    parser.add_argument("--output", default="data/job_cache/linkedin_scope_audit.json")
    parser.add_argument("--as-of", default="")
    return parser.parse_args()


def audit_linkedin_scope(
    jobs: list[dict[str, Any]],
    *,
    scoring: dict[str, Any],
    targets: dict[str, Any],
    taxonomies: dict[str, Any],
    inventory: dict[str, Any],
    as_of: str,
) -> dict[str, Any]:
    direct = [job for job in jobs if is_direct_linkedin_job(job)]
    before = Counter(str(job.get("action") or "") for job in direct)
    after: Counter[str] = Counter()
    changed: list[dict[str, Any]] = []
    for job in direct:
        evaluated = evaluate_job(
            job,
            scoring,
            targets,
            taxonomies,
            inventory,
            as_of=as_of,
        )
        new_action = str(evaluated.get("action") or "")
        after[new_action] += 1
        old_action = str(job.get("action") or "")
        if old_action == "WATCH" and new_action != "WATCH":
            scope = linkedin_location_scope(job.get("location"))
            changed.append(
                {
                    "canonical_key": job.get("canonical_key") or job.get("url") or "",
                    "company": job.get("company") or "",
                    "title": job.get("title") or job.get("position") or "",
                    "location": job.get("location") or "",
                    "location_scope": scope,
                    "original_action": old_action,
                    "new_action": new_action,
                    "original_reject_reason": job.get("reject_reason") or "",
                    "new_reject_reason": evaluated.get("reject_reason") or "",
                    "url": job.get("url") or job.get("source_url") or "",
                }
            )
    scope_counts = Counter(
        linkedin_location_scope(job.get("location"))["status"] for job in direct
    )
    return {
        "mode": "DRY_RUN",
        "input_count": len(jobs),
        "direct_linkedin_count": len(direct),
        "action_counts_before": dict(before),
        "action_counts_after": dict(after),
        "location_scope_counts": dict(scope_counts),
        "watch_exits": changed,
        "watch_exits_count": len(changed),
        "production_write": False,
    }


def main() -> None:
    args = parse_args()
    targets = load_config(args.config)
    result = audit_linkedin_scope(
        read_json(args.input, []),
        scoring=load_config(args.scoring_config),
        targets=targets,
        taxonomies=load_config(args.taxonomy_config),
        inventory=read_json(args.inventory, {}),
        as_of=args.as_of or str(targets.get("as_of") or "2026-09-24"),
    )
    write_json(args.output, result)
    print(
        "LinkedIn scope audit: "
        f"direct={result['direct_linkedin_count']} "
        f"watch_exits={result['watch_exits_count']} "
        f"scopes={result['location_scope_counts']}"
    )


if __name__ == "__main__":
    main()
