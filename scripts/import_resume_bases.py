#!/usr/bin/env python3
"""Safely import immutable approved resume ZIPs into versioned base directories."""

from __future__ import annotations

import argparse
from pathlib import Path

try:
    from resume_bases import import_registered_bases
except ModuleNotFoundError:
    from scripts.resume_bases import import_registered_bases


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import approved Golden Base resumes safely.")
    parser.add_argument("--registry", default="config/resume_bases.yaml")
    parser.add_argument("--base-id", action="append", default=[])
    parser.add_argument(
        "--refresh-changed",
        action="store_true",
        help="Explicitly replace managed derived files after the registered source hash changes",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = Path(__file__).resolve().parents[1]
    records = import_registered_bases(
        args.registry,
        repo_root=root,
        selected_base_ids=set(args.base_id) or None,
        refresh_changed=args.refresh_changed,
    )
    for record in records:
        print(
            f"{record['base_id']}: {record['source_sha256']} -> "
            f"{record['normalized_template_dir']}"
        )
    print(f"Imported or verified {len(records)} approved resume bases.")


if __name__ == "__main__":
    main()
