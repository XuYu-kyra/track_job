#!/usr/bin/env python3
"""Merge multiple raw job source files into one unified cache."""

from __future__ import annotations

import argparse
from pathlib import Path

try:
    from common import canonical_job_key, read_json, write_json
except ModuleNotFoundError:
    from scripts.common import canonical_job_key, read_json, write_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge multiple job source files.")
    parser.add_argument(
        "--inputs",
        nargs="+",
        default=[
            "data/job_cache/linkedin_jobs.json",
            "data/job_cache/indeed_jobs.json",
            "data/job_cache/manual_import_jobs.json",
            "data/job_cache/manual_indeed_jobs.json",
        ],
    )
    parser.add_argument("--output", default="data/job_cache/jobs.json")
    return parser.parse_args()


def choose_better(current: dict, candidate: dict) -> dict:
    current_tuple = (
        int(bool(current.get("description"))),
        int(bool(current.get("easy_apply"))),
        len(current.get("description", "")),
    )
    candidate_tuple = (
        int(bool(candidate.get("description"))),
        int(bool(candidate.get("easy_apply"))),
        len(candidate.get("description", "")),
    )
    return candidate if candidate_tuple > current_tuple else current


def merge_jobs(inputs: list[str]) -> list[dict]:
    merged_by_key: dict[str, dict] = {}
    for input_path in inputs:
        jobs = read_json(input_path, [])
        for job in jobs:
            key = str(job.get("url") or canonical_job_key(job.get("company", ""), job.get("position", "")))
            if key in merged_by_key:
                merged_by_key[key] = choose_better(merged_by_key[key], job)
            else:
                merged_by_key[key] = job
    return list(merged_by_key.values())


def main() -> None:
    args = parse_args()
    merged = merge_jobs(args.inputs)
    write_json(Path(args.output), merged)
    print(f"Wrote {len(merged)} merged jobs to {args.output}")


if __name__ == "__main__":
    main()
