#!/usr/bin/env python3
"""Build a bounded, role-balanced search query matrix."""

from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from common import load_config, normalize_token, write_json
except ModuleNotFoundError:
    from scripts.common import load_config, normalize_token, write_json


DEFAULT_GRADUATION_QUERIES = [
    "2027",
    "27届",
    "2027届",
    "校招",
    "校园招聘",
    "应届",
    "秋招",
]


def deterministic_daily_batch_index(
    as_of: str | date | None = None,
    *,
    timezone: str = "Asia/Shanghai",
) -> int:
    """Rotate supplemental query batches from the Shanghai calendar date."""

    if isinstance(as_of, date):
        current = as_of
    elif as_of:
        current = date.fromisoformat(str(as_of)[:10])
    else:
        current = datetime.now(ZoneInfo(timezone)).date()
    return current.toordinal()


def _role_families(targets: dict[str, Any]) -> list[str]:
    profile = targets.get("candidate_profile", {})
    values = list(profile.get("preferred_role_families", []))
    values.extend(profile.get("expansion_role_families", []))
    return list(dict.fromkeys(str(value) for value in values if value))


def _queries_by_family(
    targets: dict[str, Any], taxonomies: dict[str, Any], *, source: str = ""
) -> dict[str, list[dict[str, str]]]:
    search = targets.get("job_search", {})
    source_search = search.get(f"{source}_search", {}) if source else {}
    source_regions = source_search.get("regions", []) if isinstance(source_search, dict) else []
    locations = [str(item) for item in source_regions if item]
    if not locations:
        locations = [str(item) for item in search.get("regions", []) if item] or ["深圳"]
    configured_graduation = search.get("graduation_queries", DEFAULT_GRADUATION_QUERIES)
    graduation_terms = [str(item) for item in configured_graduation if item]
    result: dict[str, list[dict[str, str]]] = {}
    for family in _role_families(targets):
        spec = taxonomies.get("role_families", {}).get(family, {})
        aliases = [str(item) for item in spec.get("aliases", []) if item]
        if not aliases:
            continue
        family_queries: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        combinations: list[tuple[str, str, str]] = []
        diagonal_count = len(aliases) * len(graduation_terms) * len(locations)
        for index in range(diagonal_count):
            combinations.append(
                (
                    aliases[index % len(aliases)],
                    graduation_terms[index % len(graduation_terms)],
                    locations[index % len(locations)],
                )
            )
        combinations.extend(
            (alias, graduation, location)
            for alias in aliases
            for graduation in graduation_terms
            for location in locations
        )
        for alias, graduation, location in combinations:
            key = (
                normalize_token(alias),
                normalize_token(graduation),
                normalize_token(location),
            )
            if key in seen:
                continue
            seen.add(key)
            family_queries.append(
                {
                    "role_family": family,
                    "role_alias": alias,
                    "graduation": graduation,
                    "location": location,
                    "keywords": f"{graduation} {alias}",
                }
            )
        result[family] = family_queries
    return result


def build_query_matrix(
    targets: dict[str, Any],
    taxonomies: dict[str, Any],
    *,
    mode: str = "daily",
    source: str = "",
    batch_index: int = 0,
) -> list[dict[str, str]]:
    """Round-robin families so a bounded batch does not starve later roles."""

    if mode not in {"daily", "backfill"}:
        raise ValueError(f"Unsupported search mode: {mode}")
    by_family = _queries_by_family(targets, taxonomies, source=source)
    families = list(by_family)
    balanced: list[dict[str, str]] = []
    offset = 0
    while families:
        remaining: list[str] = []
        for family in families:
            queries = by_family[family]
            if offset < len(queries):
                query = dict(queries[offset])
                if source:
                    query["source"] = source
                balanced.append(query)
            if offset + 1 < len(queries):
                remaining.append(family)
        families = remaining
        offset += 1

    search = targets.get("job_search", {})
    mode_config = search.get("search_modes", {}).get(mode, {})
    limit = int(mode_config.get("query_limit") or search.get("query_batch_limit", 48))
    if limit <= 0 or not balanced:
        return []
    start = (max(0, batch_index) * limit) % len(balanced)
    rotated = balanced[start:] + balanced[:start]
    if str(source).casefold() == "linkedin":
        # LinkedIn has a source-specific three-region scope.  Reserve one
        # query for every configured region before filling the remaining
        # slots, so adding UK cannot starve Shenzhen or Hong Kong in a daily
        # bounded batch.  Role-family round-robin remains the fill order.
        try:
            from linkedin_location import linkedin_query_region
        except ModuleNotFoundError:
            from scripts.linkedin_location import linkedin_query_region
        selected: list[dict[str, str]] = []
        seen_regions: set[str] = set()
        for query in rotated:
            region = linkedin_query_region(query.get("location"))
            if region in seen_regions:
                continue
            seen_regions.add(region)
            selected.append(query)
            if len(selected) >= limit:
                return selected[:limit]
        selected_ids = {id(item) for item in selected}
        selected.extend(item for item in rotated if id(item) not in selected_ids)
        return selected[:limit]
    return rotated[:limit]


def posted_window_hours(targets: dict[str, Any], mode: str) -> int:
    search = targets.get("job_search", {})
    mode_config = search.get("search_modes", {}).get(mode, {})
    if mode == "backfill":
        return int(mode_config.get("posted_within_days", 60)) * 24
    return int(mode_config.get("posted_within_hours") or search.get("posted_within_hours", 72))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview the bounded search query matrix.")
    parser.add_argument("--config", default="config/targets.yaml")
    parser.add_argument("--taxonomy-config", default="config/taxonomies.yaml")
    parser.add_argument("--mode", choices=("daily", "backfill"), default="daily")
    parser.add_argument("--source", default="")
    parser.add_argument("--batch-index", type=int, default=0)
    parser.add_argument("--output", default="data/job_cache/query_matrix.json")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    matrix = build_query_matrix(
        load_config(args.config),
        load_config(args.taxonomy_config),
        mode=args.mode,
        source=args.source,
        batch_index=args.batch_index,
    )
    write_json(Path(args.output), matrix)
    print(f"Query matrix: mode={args.mode}, source={args.source or 'all'}, queries={len(matrix)}")


if __name__ == "__main__":
    main()
