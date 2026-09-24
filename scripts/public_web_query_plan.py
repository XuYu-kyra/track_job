#!/usr/bin/env python3
"""Build deterministic two-axis public-web discovery plans.

Axis one monitors known companies with P0 daily and P1 three-day coverage.
Axis two searches the open market without requiring a company name.  Registry
membership is metadata only and never bounds the open-market query matrix.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from common import load_config, normalize_token, read_json
    from source_adapters import load_source_registry
    from adaptive_discovery import adaptive_budget_metadata
except ModuleNotFoundError:
    from scripts.common import load_config, normalize_token, read_json
    from scripts.source_adapters import load_source_registry
    from scripts.adaptive_discovery import adaptive_budget_metadata


PUBLIC_SOURCE_LANES = (
    "nowcoder",
    "ncss",
    "guopin",
    "university_careers",
    "public_web",
    "official_domain_discovery",
)
REQUIRED_LANES = (
    *PUBLIC_SOURCE_LANES,
    "company_agnostic_core",
    "company_agnostic_expansion",
    "known_ats_domain",
    "campus_campaign",
    "p0_company_daily",
    "p1_company_rolling_3d",
)
OPTIONAL_LANES = ("discovered_company_monitoring", "urgent_known_company_refresh")
CORE_ROLE_FAMILIES = (
    "general_software",
    "backend",
    "test_development",
    "ai_application",
    "robot_software",
)
DEFAULT_PUBLIC_SOURCE_DOMAINS = {
    "nowcoder": "nowcoder.com/jobs",
    "ncss": "ncss.cn",
    "guopin": "iguopin.com",
    "university_careers": "edu.cn",
}
DEFAULT_ATS_DOMAINS = (
    "myworkdayjobs.com",
    "zhiye.com",
    "hotjob.cn",
    "mokahr.com",
)
DEFAULT_CAMPAIGN_TERMS = ("提前批", "正式批", "秋招", "补招")
P1_CYCLE_DAYS = 3
DEFAULT_EXPANSION_CYCLE_DAYS = 4
DEFAULT_DYNAMIC_COMPANY_CYCLE_DAYS = 3
DEFAULT_QUERY_PLAN_LIMIT = 128
P1_RUN_WEEKDAYS = frozenset({0, 2, 4})  # Monday, Wednesday, Friday
P1_EPOCH = date(2026, 1, 5)  # Monday; stable calendar anchor


def _query_id(lane: str, query: str) -> str:
    digest = hashlib.sha256(f"{lane}\n{query}".encode("utf-8")).hexdigest()[:12]
    return f"{lane}-{digest}"


def _stable_index(seed: str, size: int) -> int:
    if size <= 0:
        return 0
    return int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16) % size


def _dedupe_strings(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        token = normalize_token(text)
        if text and token not in seen:
            seen.add(token)
            result.append(text)
    return result


def _priority_companies(registry: dict[str, Any], priority: str) -> list[str]:
    return sorted(
        str(item.get("name") or "")
        for item in registry.get("companies", [])
        if isinstance(item, dict)
        and str(item.get("monitor_priority") or "").upper() == priority
        and item.get("name")
    )


def _persistent_discovered_companies(store: dict[str, Any] | None) -> list[str]:
    return sorted(
        {
            str(item.get("normalized_name") or "").strip()
            for item in (store or {}).get("companies", [])
            if isinstance(item, dict)
            and item.get("status") == "PERSISTENT_MONITORING"
            and item.get("normalized_name")
        }
    )


def daily_p0_companies(registry: dict[str, Any]) -> list[str]:
    return _priority_companies(registry, "P0")


def rolling_p1_companies(
    registry: dict[str, Any], run_date: date, *, cycle_days: int = P1_CYCLE_DAYS
) -> list[str]:
    if run_date.weekday() not in P1_RUN_WEEKDAYS:
        return []
    companies = _priority_companies(registry, "P1")
    bucket = _p1_scheduled_slot(run_date) % cycle_days
    return [name for index, name in enumerate(companies) if index % cycle_days == bucket]


def is_p1_scheduled_day(run_date: date) -> bool:
    return run_date.weekday() in P1_RUN_WEEKDAYS


def next_p1_scheduled_run(run_date: date) -> date:
    candidate = run_date
    while candidate.weekday() not in P1_RUN_WEEKDAYS:
        candidate = date.fromordinal(candidate.toordinal() + 1)
    return candidate


def _p1_scheduled_slot(run_date: date) -> int:
    if run_date < P1_EPOCH:
        raise ValueError("run_date predates the P1 scheduling epoch")
    days = run_date.toordinal() - P1_EPOCH.toordinal()
    full_weeks, remainder = divmod(days, 7)
    slot = full_weeks * len(P1_RUN_WEEKDAYS)
    slot += sum(
        1
        for offset in range(1, remainder + 1)
        if (P1_EPOCH.weekday() + offset) % 7 in P1_RUN_WEEKDAYS
    )
    return slot


def rotating_p0_companies(registry: dict[str, Any], run_date: date) -> list[str]:
    """Compatibility alias: Source Discovery V3 checks every P0 every day."""
    del run_date
    return daily_p0_companies(registry)


def _configured_roles(
    targets: dict[str, Any], taxonomies: dict[str, Any], key: str, fallback: tuple[str, ...]
) -> list[str]:
    configured = targets.get("candidate_profile", {}).get(key, [])
    role_specs = taxonomies.get("role_families", {})
    roles = [
        str(value)
        for value in configured
        if str(value) in role_specs and role_specs[str(value)].get("aliases")
    ]
    if not roles:
        roles = [value for value in fallback if role_specs.get(value, {}).get("aliases")]
    return list(dict.fromkeys(roles))


def _role_aliases(taxonomies: dict[str, Any], role_family: str) -> list[str]:
    return _dedupe_strings(
        list((taxonomies.get("role_families", {}).get(role_family, {}) or {}).get("aliases", []))
    )


def _contains_cjk(value: str) -> bool:
    return any("\u4e00" <= character <= "\u9fff" for character in value)


def _pick_alias(
    taxonomies: dict[str, Any],
    role_family: str,
    *,
    seed: str,
    language: str = "any",
) -> str:
    aliases = _role_aliases(taxonomies, role_family)
    if language == "zh":
        selected = [item for item in aliases if _contains_cjk(item)]
    elif language == "en":
        selected = [item for item in aliases if not _contains_cjk(item)]
    else:
        selected = aliases
    selected = selected or aliases or [role_family.replace("_", " ")]
    return selected[_stable_index(seed, len(selected))]


def _known_company_query(
    company: str,
    *,
    company_aliases: list[str] | tuple[str, ...] = (),
    taxonomies: dict[str, Any],
    role_families: list[str],
    zh_location: str,
    en_location: str,
    year: str,
    zh_graduation: str,
) -> str:
    """Build one bounded bilingual query that represents all core role families.

    Known-company monitoring intentionally remains one query per company so the
    daily plan stays bounded.  The query must nevertheless cover the official
    entrance, both location languages, early-career vocabulary, and every core
    role family; one random Chinese alias is not company-level coverage.
    """

    role_terms: list[str] = []
    for role_family in role_families:
        for language in ("en", "zh"):
            role_terms.append(
                _pick_alias(
                    taxonomies,
                    role_family,
                    seed=f"known-company:{role_family}:{language}",
                    language=language,
                )
            )
    role_clause = " OR ".join(f'"{term}"' for term in _dedupe_strings(role_terms))
    company_terms = _dedupe_strings([company, *company_aliases])
    company_clause = (
        company_terms[0]
        if len(company_terms) == 1
        else "(" + " OR ".join(f'"{term}"' for term in company_terms) + ")"
    )
    return (
        f'{company_clause} ({zh_location} OR {en_location}) '
        f'({zh_graduation} OR {year} OR "new grad" OR "early career" OR intern OR 校招) '
        f'({role_clause}) (招聘 OR careers OR jobs)'
    )


def _locations(targets: dict[str, Any]) -> tuple[str, str]:
    configured = _dedupe_strings(
        list(targets.get("job_search", {}).get("regions", []))
    )
    zh = next((value for value in configured if _contains_cjk(value)), "深圳")
    en = next((value for value in configured if not _contains_cjk(value)), "Shenzhen")
    return zh, en


def _additional_locations(targets: dict[str, Any], primary: tuple[str, str]) -> list[str]:
    """Return configured remote/surrounding locations not covered by Shenzhen aliases."""

    configured = _dedupe_strings(
        [
            *list(targets.get("job_search", {}).get("regions", [])),
            *list(targets.get("job_search", {}).get("acceptable_locations", [])),
        ]
    )
    primary_tokens = {normalize_token(value) for value in primary}
    return [
        value
        for value in configured
        if normalize_token(value) not in primary_tokens
    ]


def _graduation_terms(targets: dict[str, Any]) -> tuple[str, str, str]:
    year = str(targets.get("candidate_profile", {}).get("graduation_year") or "2027")
    short = year[-2:] if len(year) >= 2 else year
    configured = _dedupe_strings(
        list(targets.get("job_search", {}).get("graduation_queries", []))
    )
    zh = next(
        (value for value in configured if year in value and _contains_cjk(value)),
        f"{year}届",
    )
    short_zh = next((value for value in configured if short in value and "届" in value), f"{short}届")
    return zh, short_zh, year


def _append_query(
    query_specs: list[dict[str, Any]],
    *,
    lane: str,
    query: str,
    role_family: str,
    role_alias: str,
    graduation_term: str,
    location: str,
    companies: list[str] | None = None,
    company_agnostic: bool,
    required_interval_days: int,
    priority: str = "",
    source_domain: str = "",
    campaign_term: str = "",
) -> None:
    if query in {item["query"] for item in query_specs}:
        query = f"{query} {lane.replace('_', ' ')}"
    query_specs.append(
        {
            "query_id": _query_id(lane, query),
            "lane": lane,
            "axis": "open_market" if company_agnostic else "known_company",
            "company_agnostic": company_agnostic,
            "required_interval_days": required_interval_days,
            "priority": priority,
            "role_family": role_family,
            "role_alias": role_alias,
            "graduation_term": graduation_term,
            "location": location,
            "source_domain": source_domain,
            "campaign_term": campaign_term,
            "query": query,
            "companies": list(companies or []),
        }
    )


def _p1_cycle_schedule(
    registry: dict[str, Any], run_date: date
) -> tuple[int, str, str, dict[str, list[str]]]:
    scheduled = next_p1_scheduled_run(run_date)
    slot = _p1_scheduled_slot(scheduled)
    cycle_start_slot = slot - (slot % P1_CYCLE_DAYS)
    start = scheduled
    while _p1_scheduled_slot(start) > cycle_start_slot:
        start = date.fromordinal(start.toordinal() - 1)
        if is_p1_scheduled_day(start) and _p1_scheduled_slot(start) == cycle_start_slot:
            break
    scheduled_dates = [
        date.fromordinal(start.toordinal() + offset)
        for offset in range(0, 7)
        if is_p1_scheduled_day(date.fromordinal(start.toordinal() + offset))
    ][:P1_CYCLE_DAYS]
    end = scheduled_dates[-1] if scheduled_dates else start
    schedule = {
        item.isoformat(): rolling_p1_companies(registry, item)
        for item in scheduled_dates
    }
    return cycle_start_slot // P1_CYCLE_DAYS, start.isoformat(), end.isoformat(), schedule


def _expansion_cycle_schedule(
    roles: list[str], run_date: date, cycle_days: int
) -> tuple[int, str, str, dict[str, list[str]]]:
    bucket = run_date.toordinal() % cycle_days
    start = date.fromordinal(run_date.toordinal() - bucket)
    end = date.fromordinal(start.toordinal() + cycle_days - 1)
    schedule = {
        date.fromordinal(start.toordinal() + offset).isoformat(): [
            role
            for index, role in enumerate(roles)
            if index % cycle_days
            == date.fromordinal(start.toordinal() + offset).toordinal() % cycle_days
        ]
        for offset in range(cycle_days)
    }
    return run_date.toordinal() // cycle_days, start.isoformat(), end.isoformat(), schedule


def build_public_web_query_plan(
    targets: dict[str, Any],
    taxonomies: dict[str, Any],
    registry: dict[str, Any],
    *,
    run_date: date,
    discovered_companies: dict[str, Any] | None = None,
    urgent_companies: list[str] | None = None,
) -> dict[str, Any]:
    discovery = targets.get("open_market_discovery", {})
    configured_core_roles = _configured_roles(
        targets, taxonomies, "preferred_role_families", CORE_ROLE_FAMILIES
    )
    core_roles = list(
        dict.fromkeys(
            [
                *configured_core_roles,
                *(
                    role
                    for role in CORE_ROLE_FAMILIES
                    if (taxonomies.get("role_families", {}).get(role) or {}).get(
                        "aliases"
                    )
                ),
            ]
        )
    )
    expansion_roles = _configured_roles(
        targets, taxonomies, "expansion_role_families", ()
    )
    expansion_cycle_days = max(
        1, int(discovery.get("expansion_role_cycle_days", DEFAULT_EXPANSION_CYCLE_DAYS))
    )
    expansion_bucket = run_date.toordinal() % expansion_cycle_days
    expansion_today = [
        role
        for index, role in enumerate(expansion_roles)
        if index % expansion_cycle_days == expansion_bucket
    ]
    zh_location, en_location = _locations(targets)
    additional_locations = _additional_locations(
        targets, (zh_location, en_location)
    )
    zh_graduation, short_graduation, year = _graduation_terms(targets)
    public_domains = {
        **DEFAULT_PUBLIC_SOURCE_DOMAINS,
        **(discovery.get("public_source_domains", {}) or {}),
    }
    ats_domains = _dedupe_strings(
        list(discovery.get("ats_domains", DEFAULT_ATS_DOMAINS))
    )
    campaign_terms = _dedupe_strings(
        list(discovery.get("campaign_terms", DEFAULT_CAMPAIGN_TERMS))
    )
    query_specs: list[dict[str, Any]] = []
    registry_by_name = {
        str(item.get("name") or ""): item
        for item in registry.get("companies", [])
        if isinstance(item, dict) and str(item.get("name") or "")
    }

    # Every core family is searched daily without a company name in both a
    # configured Chinese and English alias variant.
    for role in core_roles:
        for language, location, graduation in (
            ("zh", zh_location, zh_graduation),
            ("en", en_location, f"{year} graduate"),
        ):
            alias = _pick_alias(
                taxonomies,
                role,
                seed=f"{run_date.isoformat()}:{role}:{language}",
                language=language,
            )
            _append_query(
                query_specs,
                lane="company_agnostic_core",
                query=f"{location} {graduation} {alias}",
                role_family=role,
                role_alias=alias,
                graduation_term=graduation,
                location=location,
                company_agnostic=True,
                required_interval_days=1,
            )
        for location in additional_locations:
            language = "zh" if _contains_cjk(location) else "en"
            graduation = zh_graduation if language == "zh" else f"{year} graduate"
            alias = _pick_alias(
                taxonomies,
                role,
                seed=f"{run_date.isoformat()}:{role}:{location}",
                language=language,
            )
            _append_query(
                query_specs,
                lane="company_agnostic_core",
                query=f"{location} {graduation} {alias}",
                role_family=role,
                role_alias=alias,
                graduation_term=graduation,
                location=location,
                company_agnostic=True,
                required_interval_days=1,
            )

    # Expansion roles rotate through a bounded configured cycle.
    for role in expansion_today:
        alias = _pick_alias(
            taxonomies,
            role,
            seed=f"{run_date.isoformat()}:{role}:expansion",
            language="zh",
        )
        _append_query(
            query_specs,
            lane="company_agnostic_expansion",
            query=f"{zh_location} {short_graduation} {alias} 校招",
            role_family=role,
            role_alias=alias,
            graduation_term=short_graduation,
            location=zh_location,
            company_agnostic=True,
            required_interval_days=expansion_cycle_days,
        )

    # Each public source is a separate daily lane.
    for index, lane in enumerate(PUBLIC_SOURCE_LANES):
        role = core_roles[(run_date.toordinal() + index) % len(core_roles)]
        alias = _pick_alias(
            taxonomies,
            role,
            seed=f"{run_date.isoformat()}:{lane}",
            language="zh",
        )
        domain = str(public_domains.get(lane) or "")
        if lane in DEFAULT_PUBLIC_SOURCE_DOMAINS:
            query = f"site:{domain} {zh_location} {year} {alias} 校园招聘"
        elif lane == "public_web":
            query = f"{zh_location} {year} {alias} 校招 招聘"
        else:
            query = f"{zh_location} {year} {alias} 官方招聘 careers jobs"
        _append_query(
            query_specs,
            lane=lane,
            query=query,
            role_family=role,
            role_alias=alias,
            graduation_term=year,
            location=zh_location,
            company_agnostic=True,
            required_interval_days=1,
            source_domain=domain,
        )

    # Known public ATS domains are searched independently of registry entries.
    for index, domain in enumerate(ats_domains):
        role = core_roles[index % len(core_roles)]
        language = "en" if "workday" in domain else "zh"
        location = en_location if language == "en" else zh_location
        alias = _pick_alias(
            taxonomies,
            role,
            seed=f"{run_date.isoformat()}:{domain}",
            language=language,
        )
        _append_query(
            query_specs,
            lane="known_ats_domain",
            query=f"site:{domain} {location} {year} {alias}",
            role_family=role,
            role_alias=alias,
            graduation_term=year,
            location=location,
            company_agnostic=True,
            required_interval_days=1,
            source_domain=domain,
        )

    # Campaign shards explicitly cover the configured early/formal/autumn/
    # supplementary vocabulary without naming a company.
    for index, term in enumerate(campaign_terms):
        role = core_roles[index % len(core_roles)]
        alias = _pick_alias(
            taxonomies,
            role,
            seed=f"{run_date.isoformat()}:{term}",
            language="zh",
        )
        _append_query(
            query_specs,
            lane="campus_campaign",
            query=f"{zh_location} {zh_graduation} {alias} {term}",
            role_family=role,
            role_alias=alias,
            graduation_term=zh_graduation,
            location=zh_location,
            company_agnostic=True,
            required_interval_days=1,
            campaign_term=term,
        )

    p0_companies = daily_p0_companies(registry)
    for company in p0_companies:
        role = core_roles[_stable_index(company, len(core_roles))]
        alias = _pick_alias(taxonomies, role, seed=f"known-company:{role}:zh", language="zh")
        _append_query(
            query_specs,
            lane="p0_company_daily",
            query=_known_company_query(
                company,
                company_aliases=list((registry_by_name.get(company) or {}).get("aliases") or []),
                taxonomies=taxonomies,
                role_families=core_roles,
                zh_location=zh_location,
                en_location=en_location,
                year=year,
                zh_graduation=zh_graduation,
            ),
            role_family=role,
            role_alias=alias,
            graduation_term=year,
            location=zh_location,
            companies=[company],
            company_agnostic=False,
            required_interval_days=1,
            priority="P0",
        )

    p1_companies = rolling_p1_companies(registry, run_date)
    for company in p1_companies:
        role = core_roles[_stable_index(company, len(core_roles))]
        alias = _pick_alias(taxonomies, role, seed=f"known-company:{role}:zh", language="zh")
        _append_query(
            query_specs,
            lane="p1_company_rolling_3d",
            query=_known_company_query(
                company,
                company_aliases=list((registry_by_name.get(company) or {}).get("aliases") or []),
                taxonomies=taxonomies,
                role_families=core_roles,
                zh_location=zh_location,
                en_location=en_location,
                year=year,
                zh_graduation=zh_graduation,
            ),
            role_family=role,
            role_alias=alias,
            graduation_term=year,
            location=zh_location,
            companies=[company],
            company_agnostic=False,
            required_interval_days=P1_CYCLE_DAYS,
            priority="P1",
        )

    # Explicit manual/urgent refreshes are a narrow override for positive
    # controls or source incidents. They do not alter the deterministic P1
    # calendar and remain separately auditable in their own lane.
    urgent_names = _dedupe_strings(list(urgent_companies or []))
    for company in urgent_names:
        role = core_roles[_stable_index(company, len(core_roles))]
        alias_record = registry_by_name.get(company) or {}
        _append_query(
            query_specs,
            lane="urgent_known_company_refresh",
            query=_known_company_query(
                company,
                company_aliases=list(alias_record.get("aliases") or []),
                taxonomies=taxonomies,
                role_families=core_roles,
                zh_location=zh_location,
                en_location=en_location,
                year=year,
                zh_graduation=zh_graduation,
            ),
            role_family=role,
            role_alias=_pick_alias(
                taxonomies,
                role,
                seed=f"urgent-known-company:{company}",
                language="zh",
            ),
            graduation_term=year,
            location=zh_location,
            companies=[company],
            company_agnostic=False,
            required_interval_days=1,
            priority="URGENT",
        )

    # Known-company monitoring uses the bounded adaptive state machine. Keep
    # this metadata on the query itself so the agent and receiver can audit
    # budget/terminal-state compliance without changing open-market lanes.
    adaptive_meta = adaptive_budget_metadata()
    for spec in query_specs:
        if spec["lane"] in {
            "p0_company_daily",
            "p1_company_rolling_3d",
            "discovered_company_monitoring",
        }:
            spec["adaptive_search"] = {
                "initial_state": "START",
                "terminal_states": [
                    "ROLE_FOUND",
                    "NO_RESULT_CONFIRMED",
                    "BLOCKED",
                    "NEEDS_REVIEW",
                ],
                **adaptive_meta,
            }

    # Promoted open-market discoveries stay outside the registry while gaining
    # a deterministic, capacity-bounded precision-monitoring shard.
    persistent_companies = _persistent_discovered_companies(discovered_companies)
    configured_dynamic_cycle_days = max(
        1,
        int(
            discovery.get(
                "dynamic_company_cycle_days", DEFAULT_DYNAMIC_COMPANY_CYCLE_DAYS
            )
        ),
    )
    query_plan_limit = int(
        discovery.get("query_plan_limit", DEFAULT_QUERY_PLAN_LIMIT)
    )
    if len(query_specs) > query_plan_limit:
        raise ValueError(
            f"base query plan has {len(query_specs)} queries, exceeding limit "
            f"{query_plan_limit}"
        )
    if persistent_companies and len(query_specs) == query_plan_limit:
        raise ValueError("query plan limit leaves no dynamic-company monitoring capacity")
    dynamic_daily_capacity = max(1, query_plan_limit - len(query_specs))
    dynamic_cycle_days = max(
        configured_dynamic_cycle_days,
        math.ceil(len(persistent_companies) / dynamic_daily_capacity)
        if persistent_companies
        else configured_dynamic_cycle_days,
    )
    (
        dynamic_cycle_id,
        dynamic_cycle_start,
        dynamic_cycle_end,
        dynamic_schedule,
    ) = _expansion_cycle_schedule(
        persistent_companies, run_date, dynamic_cycle_days
    )
    persistent_companies_today = dynamic_schedule.get(run_date.isoformat(), [])
    for company in persistent_companies_today:
        role = core_roles[_stable_index(company, len(core_roles))]
        alias = _pick_alias(taxonomies, role, seed=f"known-company:{role}:zh", language="zh")
        _append_query(
            query_specs,
            lane="discovered_company_monitoring",
            query=f'"{company}" {zh_location} {year} {alias} 官方招聘 校招',
            role_family=role,
            role_alias=alias,
            graduation_term=year,
            location=zh_location,
            companies=[company],
            company_agnostic=False,
            required_interval_days=dynamic_cycle_days,
            priority="DYNAMIC",
        )

    optional_lanes_today = []
    if persistent_companies_today:
        optional_lanes_today.append("discovered_company_monitoring")
    if urgent_names:
        optional_lanes_today.append("urgent_known_company_refresh")
    lanes = {
        lane: {
            "planned": sum(item["lane"] == lane for item in query_specs),
            "attempted": 0,
            "succeeded": 0,
            "empty_valid": 0,
            "failed": 0,
            "opened_urls": [],
            "candidate_count": 0,
        }
        for lane in (
            *REQUIRED_LANES,
            *optional_lanes_today,
        )
    }
    cycle_id, cycle_start, cycle_end, p1_schedule = _p1_cycle_schedule(
        registry, run_date
    )
    (
        expansion_cycle_id,
        expansion_cycle_start,
        expansion_cycle_end,
        expansion_schedule,
    ) = _expansion_cycle_schedule(expansion_roles, run_date, expansion_cycle_days)
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    plan_fingerprint = hashlib.sha256(
        json.dumps(query_specs, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "schema_version": "2.0",
        "architecture": "two_axis_known_company_plus_open_market",
        "registry_is_allowlist": False,
        "unregistered_companies_allowed": True,
        "plan_id": f"public-web-{run_date.isoformat()}-{plan_fingerprint}",
        "search_date": run_date.isoformat(),
        "timezone": "Asia/Shanghai",
        "generated_at": now.isoformat(timespec="seconds"),
        "required_lanes": list(REQUIRED_LANES),
        "optional_lanes": list(OPTIONAL_LANES),
        "public_source_lanes": list(PUBLIC_SOURCE_LANES),
        "core_role_families": core_roles,
        "expansion_role_families": expansion_roles,
        "expansion_roles_planned": expansion_today,
        "expansion_cycle_days": expansion_cycle_days,
        "expansion_cycle_id": expansion_cycle_id,
        "expansion_cycle_start": expansion_cycle_start,
        "expansion_cycle_end": expansion_cycle_end,
        "expansion_cycle_schedule": expansion_schedule,
        "company_agnostic_query_count": sum(
            bool(item["company_agnostic"]) for item in query_specs
        ),
        "p0_companies_planned": p0_companies,
        "p1_companies_planned": p1_companies,
        "urgent_companies_requested": urgent_names,
        "urgent_companies_planned": urgent_names,
        "persistent_discovered_companies": persistent_companies,
        "persistent_discovered_companies_planned": persistent_companies_today,
        "dynamic_company_cycle_days": dynamic_cycle_days,
        "dynamic_company_cycle_id": dynamic_cycle_id,
        "dynamic_company_cycle_start": dynamic_cycle_start,
        "dynamic_company_cycle_end": dynamic_cycle_end,
        "dynamic_company_cycle_schedule": dynamic_schedule,
        "query_plan_limit": query_plan_limit,
        "p1_cycle_id": cycle_id,
        "p1_cycle_start": cycle_start,
        "p1_cycle_end": cycle_end,
        "p1_cycle_days": P1_CYCLE_DAYS,
        "p1_cycle_schedule": p1_schedule,
        "p1_schedule_weekdays": ["MONDAY", "WEDNESDAY", "FRIDAY"],
        "p1_scheduled_today": is_p1_scheduled_day(run_date),
        "next_scheduled_run": next_p1_scheduled_run(run_date).isoformat(),
        "skipped_by_schedule": not is_p1_scheduled_day(run_date),
        "adaptive_search": adaptive_meta,
        "queries": query_specs,
        "lanes": lanes,
    }


def validate_query_receipts(
    plan: dict[str, Any], payload: dict[str, Any], *, require_complete: bool = False
) -> list[str]:
    errors: list[str] = []
    planned_items = [item for item in plan.get("queries", []) if isinstance(item, dict)]
    planned_by_id = {str(item.get("query_id") or ""): item for item in planned_items}
    planned_ids = [str(item.get("query_id") or "") for item in planned_items]
    batch = payload.get("batch") if isinstance(payload, dict) else None
    if not isinstance(batch, dict):
        return ["MANUAL_AGENT batch metadata is missing"]
    plan_id = str(plan.get("plan_id") or "")
    if str(batch.get("plan_id") or "") != plan_id:
        errors.append("MANUAL_AGENT batch plan_id does not match the supplied query plan")
    if str((batch or {}).get("search_date") or "") != str(plan.get("search_date") or ""):
        errors.append("MANUAL_AGENT search_date does not match the supplied query plan")
    receipts = [item for item in batch.get("searches", []) if isinstance(item, dict)]
    actual_ids: list[str] = []
    actual_queries: list[str] = []
    for index, receipt in enumerate(receipts):
        query_id = str(receipt.get("query_id") or "")
        actual_ids.append(query_id)
        actual_queries.append(str(receipt.get("query") or ""))
        planned = planned_by_id.get(query_id)
        if planned is None:
            errors.append(f"batch.searches[{index}].query_id is not in the current plan")
            continue
        if str(receipt.get("plan_id") or "") != plan_id:
            errors.append(f"batch.searches[{index}].plan_id does not match the current plan")
        if str(receipt.get("query") or "") != str(planned.get("query") or ""):
            errors.append(f"batch.searches[{index}].query does not match query_id")
        if str(receipt.get("lane") or "") != str(planned.get("lane") or ""):
            errors.append(f"batch.searches[{index}].lane does not match query_id")
        completed = str(receipt.get("completed_at") or "")[:10]
        if completed != str(plan.get("search_date") or ""):
            errors.append(f"batch.searches[{index}].completed_at is outside the plan date")
    expected_queries = [
        str(planned_by_id[query_id].get("query") or "")
        for query_id in planned_ids
        if query_id in set(actual_ids)
    ]
    if list(batch.get("queries") or []) != expected_queries or actual_queries != expected_queries:
        errors.append("MANUAL_AGENT batch queries/receipts must follow current plan order")
    if require_complete and actual_ids != planned_ids:
        errors.append("MANUAL_AGENT batch receipts must cover every current-plan query_id")
    return errors


def lane_receipts(plan: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    by_query_id = {
        str(item.get("query_id") or ""): str(item.get("lane") or "")
        for item in plan.get("queries", [])
    }
    lanes = json.loads(json.dumps(plan.get("lanes") or {}))
    batch = payload.get("batch") if isinstance(payload, dict) else {}
    for receipt in batch.get("searches") or []:
        if not isinstance(receipt, dict):
            continue
        lane = by_query_id.get(str(receipt.get("query_id") or ""))
        if not lane or lane not in lanes:
            continue
        lanes[lane]["attempted"] += 1
        status = str(receipt.get("status") or "").upper()
        if status == "SUCCESS":
            lanes[lane]["succeeded"] += 1
        elif status == "EMPTY_VALID":
            lanes[lane]["empty_valid"] += 1
        elif status in {"FAILED", "RATE_LIMITED", "CAPTCHA", "PARSE_ERROR", "TIMEOUT"}:
            lanes[lane]["failed"] += 1
        lanes[lane]["opened_urls"] = list(
            dict.fromkeys(
                [*lanes[lane]["opened_urls"], *(receipt.get("source_urls") or [])]
            )
        )
    for candidate in payload.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        lane = by_query_id.get(str(candidate.get("discovery_query_id") or ""))
        if lane in lanes:
            lanes[lane]["candidate_count"] += 1
    return lanes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the dated public-web query plan.")
    parser.add_argument("--config", default="config/targets.yaml")
    parser.add_argument("--taxonomy-config", default="config/taxonomies.yaml")
    parser.add_argument("--source-registry", default="config/company_registry.yaml")
    parser.add_argument("--date", default="")
    parser.add_argument(
        "--discovered-companies",
        default="data/job_cache/discovered_company_candidates.json",
    )
    parser.add_argument("--output", default="data/job_cache/public_web_query_plan.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_date = date.fromisoformat(args.date) if args.date else datetime.now(
        ZoneInfo("Asia/Shanghai")
    ).date()
    payload = build_public_web_query_plan(
        load_config(args.config),
        load_config(args.taxonomy_config),
        load_source_registry(args.source_registry),
        run_date=run_date,
        discovered_companies=read_json(Path(args.discovered_companies), {}),
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"Query plan {payload['plan_id']}: {len(payload['queries'])} queries, "
        f"open_market={payload['company_agnostic_query_count']}, "
        f"P0={len(payload['p0_companies_planned'])}, "
        f"P1_today={len(payload['p1_companies_planned'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
