#!/usr/bin/env python3
"""Build the two-axis Source Discovery completeness report and apply gate."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from common import read_json
    from known_jobs import known_job_recall, load_known_jobs
    from job_schema import MIN_FULL_JD_CHARS, is_verified_open_job
    from public_web_query_plan import PUBLIC_SOURCE_LANES, REQUIRED_LANES
    from source_adapters import load_source_registry
except ModuleNotFoundError:
    from scripts.common import read_json
    from scripts.known_jobs import known_job_recall, load_known_jobs
    from scripts.job_schema import MIN_FULL_JD_CHARS, is_verified_open_job
    from scripts.public_web_query_plan import PUBLIC_SOURCE_LANES, REQUIRED_LANES
    from scripts.source_adapters import load_source_registry


COVERAGE_STATUSES = frozenset(
    {"SOURCE_COVERAGE_HEALTHY", "SOURCE_COVERAGE_DEGRADED", "SOURCE_COVERAGE_FAILED"}
)


def _today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _percent(numerator: int, denominator: int) -> float:
    return round((100.0 * numerator / denominator), 2) if denominator else 100.0


def _completed_lane(item: dict[str, Any]) -> bool:
    return bool(
        int(item.get("planned") or 0) > 0
        and int(item.get("attempted") or 0) >= int(item.get("planned") or 0)
        and int(item.get("failed") or 0) == 0
    )


def _terminal_lane(item: dict[str, Any]) -> bool:
    return bool(
        int(item.get("planned") or 0) > 0
        and int(item.get("attempted") or 0) >= int(item.get("planned") or 0)
    )


def build_coverage_report(
    *,
    registry: dict[str, Any],
    source_health: dict[str, Any],
    source_audit: dict[str, Any],
    query_plan: dict[str, Any],
    jobs: list[dict[str, Any]],
    as_of: str,
    discovered_companies: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
    scored_jobs: list[dict[str, Any]] | None = None,
    known_jobs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    companies = [item for item in registry.get("companies", []) if isinstance(item, dict)]
    p0_names = {
        str(item.get("name") or "")
        for item in companies
        if item.get("monitor_priority") == "P0"
    }
    p1_names = {
        str(item.get("name") or "")
        for item in companies
        if item.get("monitor_priority") == "P1"
    }
    audit_rows = {
        str(item.get("company") or ""): item
        for item in _as_list(source_audit.get("companies"))
        if isinstance(item, dict)
    }
    health_sources = source_health.get("sources") or {}
    manual_details = (
        (health_sources.get("manual_agent_china_web") or {}).get("details") or {}
    )
    lane_receipts = (
        manual_details.get("lane_receipts")
        if isinstance(manual_details, dict)
        else {}
    ) or {}
    plan_lanes = query_plan.get("lanes") or {}
    receipt_plan_matches = bool(
        query_plan.get("plan_id")
        and isinstance(manual_details, dict)
        and manual_details.get("plan_id") == query_plan.get("plan_id")
    )
    lane_coverage: dict[str, Any] = {}
    for lane in dict.fromkeys([*REQUIRED_LANES, *plan_lanes.keys()]):
        configured = (
            lane_receipts.get(lane)
            if receipt_plan_matches
            else plan_lanes.get(lane)
        ) or {}
        lane_coverage[lane] = {
            "planned": int(configured.get("planned") or 0),
            "attempted": int(configured.get("attempted") or 0),
            "succeeded": int(configured.get("succeeded") or 0),
            "empty_valid": int(configured.get("empty_valid") or 0),
            "failed": int(configured.get("failed") or 0),
            "opened_urls": list(configured.get("opened_urls") or []),
            "candidate_count": int(configured.get("candidate_count") or 0),
        }

    plan_queries = [
        item for item in query_plan.get("queries", []) if isinstance(item, dict)
    ]
    query_receipts = (
        manual_details.get("query_receipts")
        if isinstance(manual_details, dict)
        else []
    ) or []
    receipt_status = {
        str(item.get("query_id") or ""): str(item.get("status") or "").upper()
        for item in query_receipts
        if isinstance(item, dict)
    }
    terminal_statuses = {
        "SUCCESS", "EMPTY_VALID", "FAILED", "RATE_LIMITED", "CAPTCHA", "PARSE_ERROR", "TIMEOUT"
    }
    successful_statuses = {"SUCCESS", "EMPTY_VALID"}

    def query_executed(spec: dict[str, Any]) -> bool:
        if not receipt_plan_matches:
            return False
        status = receipt_status.get(str(spec.get("query_id") or ""))
        if status:
            return status in terminal_statuses
        # Older fixtures stored only aggregate lane receipts. Preserve their
        # compatibility without fabricating per-query results in new runs.
        return not query_receipts and _completed_lane(
            lane_coverage.get(str(spec.get("lane") or ""), {})
        )

    def query_successful(spec: dict[str, Any]) -> bool:
        if not receipt_plan_matches:
            return False
        status = receipt_status.get(str(spec.get("query_id") or ""))
        if status:
            return status in successful_statuses
        return not query_receipts and _completed_lane(
            lane_coverage.get(str(spec.get("lane") or ""), {})
        )

    executed_specs = [spec for spec in plan_queries if query_executed(spec)]
    executed_query_ids = {
        str(spec.get("query_id") or "") for spec in executed_specs
    }
    successful_specs = [spec for spec in plan_queries if query_successful(spec)]
    successful_query_ids = {str(spec.get("query_id") or "") for spec in successful_specs}
    missing_query_ids = [
        str(spec.get("query_id") or "")
        for spec in plan_queries
        if str(spec.get("query_id") or "") not in receipt_status
    ]
    audited_today = {
        name
        for name, item in audit_rows.items()
        if str(item.get("probed_at") or "")[:10] == as_of
        and str(item.get("last_probe_result") or "") != "NOT_PROBED"
    }
    planned_p0 = set(query_plan.get("p0_companies_planned") or [])
    planned_p1 = set(query_plan.get("p1_companies_planned") or [])
    executed_p0 = {
        str(company)
        for spec in executed_specs
        if spec.get("lane") == "p0_company_daily"
        for company in spec.get("companies") or []
    }
    executed_p1 = {
        str(company)
        for spec in executed_specs
        if spec.get("lane") == "p1_company_rolling_3d"
        for company in spec.get("companies") or []
    }
    executed_dynamic_companies = {
        str(company)
        for spec in executed_specs
        if spec.get("lane") == "discovered_company_monitoring"
        for company in spec.get("companies") or []
    }
    checked_p0 = p0_names.intersection(audited_today)
    checked_p0.update(p0_names.intersection(executed_p0))

    p1_cycle_id = query_plan.get("p1_cycle_id")
    cycle_history = [
        item
        for item in (history or [])
        if item.get("p1_cycle_id") == p1_cycle_id
    ]
    checked_p1 = p1_names.intersection(audited_today)
    checked_p1.update(p1_names.intersection(executed_p1))
    for item in cycle_history:
        checked_p1.update(
            p1_names.intersection(set(item.get("p1_checked_companies") or []))
        )

    dynamic_cycle_id = query_plan.get("dynamic_company_cycle_id")
    dynamic_cycle_companies = {
        str(company)
        for names in (query_plan.get("dynamic_company_cycle_schedule") or {}).values()
        for company in names
    }
    checked_dynamic_companies = set(executed_dynamic_companies)
    for item in history or []:
        if item.get("dynamic_company_cycle_id") == dynamic_cycle_id:
            checked_dynamic_companies.update(
                str(company)
                for company in item.get("dynamic_companies_executed") or []
            )

    company_agnostic_specs = [
        spec for spec in plan_queries if bool(spec.get("company_agnostic"))
    ]
    executed_company_agnostic = [
        spec for spec in company_agnostic_specs if query_executed(spec)
    ]
    core_roles = set(query_plan.get("core_role_families") or [])
    core_roles_planned = {
        str(spec.get("role_family") or "")
        for spec in company_agnostic_specs
        if spec.get("lane") == "company_agnostic_core"
    }
    core_roles_executed = {
        str(spec.get("role_family") or "")
        for spec in executed_company_agnostic
        if spec.get("lane") == "company_agnostic_core"
    }
    expansion_roles_today = set(query_plan.get("expansion_roles_planned") or [])
    expansion_roles_executed = {
        str(spec.get("role_family") or "")
        for spec in executed_company_agnostic
        if spec.get("lane") == "company_agnostic_expansion"
    }
    expansion_cycle_id = query_plan.get("expansion_cycle_id")
    expansion_cycle_roles = {
        str(role)
        for roles in (query_plan.get("expansion_cycle_schedule") or {}).values()
        for role in roles
    }
    expansion_cycle_executed = set(expansion_roles_executed)
    for item in history or []:
        if item.get("expansion_cycle_id") == expansion_cycle_id:
            expansion_cycle_executed.update(
                str(role) for role in item.get("expansion_roles_executed") or []
            )
    source_lanes_executed = {
        lane
        for lane in PUBLIC_SOURCE_LANES
        if _terminal_lane(lane_coverage.get(lane, {}))
    }
    source_lanes_successful = {
        lane for lane in PUBLIC_SOURCE_LANES
        if _completed_lane(lane_coverage.get(lane, {}))
    }
    source_lanes_planned = {
        lane
        for lane in PUBLIC_SOURCE_LANES
        if int(lane_coverage.get(lane, {}).get("planned") or 0) > 0
    }
    p1_scheduled_cycle = set(
        company
        for names in (query_plan.get("p1_cycle_schedule") or {}).values()
        for company in names
    )
    p0_daily_percent = _percent(len(checked_p0), len(p0_names))
    p0_schedule_percent = _percent(
        len(p0_names.intersection(planned_p0)), len(p0_names)
    )
    p1_cycle_percent = _percent(len(checked_p1), len(p1_names))
    p1_schedule_percent = _percent(
        len(p1_names.intersection(p1_scheduled_cycle)), len(p1_names)
    )
    core_daily_percent = _percent(
        len(core_roles.intersection(core_roles_executed)), len(core_roles)
    )
    core_schedule_percent = _percent(
        len(core_roles.intersection(core_roles_planned)), len(core_roles)
    )
    expansion_today_percent = _percent(
        len(expansion_roles_today.intersection(expansion_roles_executed)),
        len(expansion_roles_today),
    )
    expansion_cycle_percent = _percent(
        len(expansion_cycle_roles.intersection(expansion_cycle_executed)),
        len(expansion_cycle_roles),
    )
    public_lane_percent = _percent(
        len(set(PUBLIC_SOURCE_LANES).intersection(source_lanes_executed)),
        len(PUBLIC_SOURCE_LANES),
    )
    public_lane_success_percent = _percent(
        len(set(PUBLIC_SOURCE_LANES).intersection(source_lanes_successful)),
        len(PUBLIC_SOURCE_LANES),
    )
    public_lane_schedule_percent = _percent(
        len(set(PUBLIC_SOURCE_LANES).intersection(source_lanes_planned)),
        len(PUBLIC_SOURCE_LANES),
    )
    public_source_recall_by_lane: dict[str, Any] = {}
    for lane in PUBLIC_SOURCE_LANES:
        item = lane_coverage.get(lane, {})
        raw_urls = list(dict.fromkeys(item.get("opened_urls") or []))
        candidate_count = int(item.get("candidate_count") or 0)
        if not _terminal_lane(item):
            recall_status = "NOT_EXECUTED"
        elif candidate_count:
            recall_status = "CANDIDATES_FOUND"
        elif raw_urls:
            recall_status = "RAW_RESULTS_REJECTED"
        else:
            recall_status = "NO_RAW_RESULTS"
        public_source_recall_by_lane[lane] = {
            "status": recall_status,
            "raw_result_count": len(raw_urls),
            "raw_result_urls": raw_urls,
            "candidate_count": candidate_count,
        }
    public_recall_raw_lanes = {
        lane
        for lane, item in public_source_recall_by_lane.items()
        if item["raw_result_count"] > 0
    }
    public_recall_candidate_lanes = {
        lane
        for lane, item in public_source_recall_by_lane.items()
        if item["candidate_count"] > 0
    }
    dynamic_cycle_percent = _percent(
        len(dynamic_cycle_companies.intersection(checked_dynamic_companies)),
        len(dynamic_cycle_companies),
    )

    overdue_shards: list[str] = []
    for spec in plan_queries:
        # Every spec emitted in today's deterministic plan is due today. The
        # interval controls when a rotating shard reappears, not whether an
        # emitted query may be skipped.
        if not query_executed(spec):
            overdue_shards.append(str(spec.get("query_id") or spec.get("query") or ""))
    if as_of >= str(query_plan.get("p1_cycle_end") or as_of) and checked_p1 != p1_names:
        overdue_shards.extend(f"p1:{name}" for name in sorted(p1_names - checked_p1))
    if (
        as_of >= str(query_plan.get("expansion_cycle_end") or as_of)
        and expansion_cycle_percent < 100.0
    ):
        overdue_shards.extend(
            f"expansion:{role}"
            for role in sorted(expansion_cycle_roles - expansion_cycle_executed)
        )
    if (
        as_of >= str(query_plan.get("dynamic_company_cycle_end") or as_of)
        and dynamic_cycle_percent < 100.0
    ):
        overdue_shards.extend(
            f"dynamic_company:{company}"
            for company in sorted(
                dynamic_cycle_companies - checked_dynamic_companies
            )
        )

    p0_company_coverage = []
    for company in companies:
        name = str(company.get("name") or "")
        if name not in p0_names:
            continue
        audit = audit_rows.get(name) or {}
        p0_company_coverage.append(
            {
                "company": name,
                "checked_today": name in checked_p0,
                "classification": str(audit.get("classification") or "UNKNOWN"),
                "detected_ats": str(audit.get("detected_ats") or "UNKNOWN"),
                "recommended_adapter": str(
                    audit.get("recommended_adapter") or "human_review"
                ),
                "last_probe_result": str(
                    audit.get("last_probe_result") or "NOT_PROBED"
                ),
                "reason": str(audit.get("reason") or ""),
            }
        )

    official_health = health_sources.get("official_ats") or {}
    official_details = _as_list(official_health.get("details"))
    pagination = [
        {
            "company": item.get("company"),
            "status": item.get("status"),
            "pages_attempted": int(item.get("pages_attempted") or 0),
            "pages_succeeded": int(item.get("pages_succeeded") or 0),
            "pagination_complete": bool(item.get("pagination_complete", False)),
            "truncated": bool(item.get("truncated", False)),
        }
        for item in official_details
        if isinstance(item, dict)
    ]
    source_counts = {
        name: {
            "status": item.get("status"),
            "count": int(item.get("count") or 0),
            "attempted": int(item.get("attempted") or 0),
            "succeeded": int(item.get("succeeded") or 0),
            "failed": int(item.get("failed") or 0),
        }
        for name, item in health_sources.items()
        if isinstance(item, dict)
    }
    discovered_count = 0
    source_deferred_count = 0
    for name, item in source_counts.items():
        details = (health_sources.get(name) or {}).get("details") or {}
        if isinstance(details, dict):
            discovered_count += int(
                details.get("raw_discovered_count") or item["count"]
            )
            source_deferred_count += int(details.get("deferred_count") or 0)
        else:
            discovered_count += item["count"]
    scored_count = len(jobs)
    queued_count = len(scored_jobs) if isinstance(scored_jobs, list) else len(jobs)
    known_control_report = known_job_recall(
        known_jobs if known_jobs is not None else load_known_jobs(),
        jobs,
        query_plan=query_plan,
        query_receipts=query_receipts,
    )
    discovered_companies = (
        discovered_companies if isinstance(discovered_companies, dict) else {}
    )
    unregistered_candidates = [
        item
        for item in discovered_companies.get("companies", [])
        if isinstance(item, dict)
    ]
    promotion_events = [
        item
        for item in discovered_companies.get("promotions", [])
        if isinstance(item, dict)
    ]
    new_unregistered = [
        str(item.get("normalized_name") or "")
        for item in unregistered_candidates
        if str(item.get("first_seen") or "")[:10] == as_of
    ]
    newly_verified_official_urls = [
        {
            "company": str(item.get("normalized_name") or ""),
            "url": str(item.get("verified_official_career_url") or ""),
        }
        for item in unregistered_candidates
        if item.get("verified_official_career_url")
        and str(item.get("official_url_first_verified") or "")[:10] == as_of
    ]
    promoted_companies = [
        str(item.get("normalized_name") or "")
        for item in promotion_events
        if item.get("status") == "PROMOTED_TO_PERSISTENT_MONITORING"
        and str(item.get("promoted_at") or "")[:10] == as_of
    ]
    promoted_to_registry = [
        str(item.get("normalized_name") or "")
        for item in promotion_events
        if item.get("status") == "PROMOTED_TO_REGISTRY"
        and str(item.get("promoted_at") or "")[:10] == as_of
    ]

    missing_full_jd = [
        str(job.get("canonical_key") or job.get("url") or "")
        for job in jobs
        if len(str(job.get("description") or "").strip()) < MIN_FULL_JD_CHARS
    ]
    unknown_open = [
        str(job.get("canonical_key") or job.get("url") or "")
        for job in jobs
        if str(job.get("status") or "").upper() not in {"OPEN", "CLOSED", "EXPIRED"}
    ]
    canonical_count = sum(
        str(job.get("canonicality") or "").upper() == "CANONICAL" for job in jobs
    )
    secondary_count = len(jobs) - canonical_count
    role_distribution = dict(
        sorted(Counter(str(job.get("role_family") or "other") for job in jobs).items())
    )
    verification_distribution = dict(
        sorted(
            Counter(
                str(job.get("verification_state") or "UNKNOWN") for job in jobs
            ).items()
        )
    )
    prevented_ready = [
        {
            "canonical_key": str(job.get("canonical_key") or ""),
            "verification_state": str(job.get("verification_state") or ""),
            "action": str(job.get("action") or ""),
        }
        for job in jobs
        if not is_verified_open_job(job)
        and str(job.get("action") or "") not in {"READY", "MUST_APPLY"}
    ]
    unsafe_ready = [
        {
            "canonical_key": str(job.get("canonical_key") or ""),
            "company": str(job.get("company") or ""),
            "title": str(job.get("title") or job.get("position") or ""),
            "verification_state": str(job.get("verification_state") or ""),
            "action": str(job.get("action") or ""),
        }
        for job in jobs
        if str(job.get("action") or "").upper() in {"READY", "MUST_APPLY"}
        and not is_verified_open_job(job)
    ]

    previous_success = next(
        (
            item
            for item in reversed(history or [])
            if item.get("status") == "SOURCE_COVERAGE_HEALTHY"
        ),
        None,
    )
    previous_total = int((previous_success or {}).get("total_jobs") or 0)
    unexpected_drop = bool(previous_total >= 10 and len(jobs) < previous_total * 0.5)

    hard_failures: list[str] = []
    degradations: list[str] = []
    if not companies:
        hard_failures.append("company registry is empty")
    if not p0_names:
        hard_failures.append("company registry has no P0 monitoring entries")
    if not p1_names:
        hard_failures.append("company registry has no P1 monitoring entries")
    if not health_sources:
        hard_failures.append("source health is missing")
    if unsafe_ready:
        hard_failures.append(
            f"{len(unsafe_ready)} READY/MUST_APPLY jobs failed verification consistency"
        )
    if query_plan.get("registry_is_allowlist") is not False:
        hard_failures.append("query plan does not declare registry_is_allowlist=false")
    if query_plan.get("unregistered_companies_allowed") is not True:
        hard_failures.append("query plan does not allow unregistered companies")
    if not receipt_plan_matches:
        degradations.append("manual-agent receipts do not match the current query plan")
    if p0_schedule_percent < 100.0:
        hard_failures.append("query plan does not schedule every P0 company daily")
    if p1_schedule_percent < 100.0:
        hard_failures.append("query plan does not cover every P1 company in three days")
    if core_schedule_percent < 100.0:
        hard_failures.append("query plan does not schedule every core role daily")
    if public_lane_schedule_percent < 100.0:
        hard_failures.append("query plan does not schedule every public source lane daily")
    automatic_records = [
        item for item in health_sources.values()
        if isinstance(item, dict) and item.get("automatic") is True
    ]
    if automatic_records and all(item.get("status") == "FAILED" for item in automatic_records):
        hard_failures.append("all automatic discovery sources failed")
    if any(
        str(item.get("status") or "")
        in {"FAILED", "PARTIAL", "PARTIAL_RATE_LIMITED", "REFRESH_REQUIRED"}
        for item in health_sources.values()
        if isinstance(item, dict)
    ):
        degradations.append("one or more discovery sources were incomplete")
    if any(not item["pagination_complete"] or item["truncated"] for item in pagination):
        degradations.append("official pagination was incomplete or truncated")
    for lane, item in lane_coverage.items():
        scheduled_skip = (
            lane == "p1_company_rolling_3d"
            and bool(query_plan.get("skipped_by_schedule"))
        )
        required_but_empty = lane in REQUIRED_LANES and item["planned"] <= 0 and not scheduled_skip
        planned_but_incomplete = item["planned"] > 0 and (
            item["attempted"] < item["planned"] or item["failed"]
        )
        if required_but_empty or planned_but_incomplete:
            degradations.append(f"query lane incomplete: {lane}")
    missing_p0_today = sorted(p0_names - checked_p0)
    missing_p1_cycle = sorted(p1_names - checked_p1)
    missing_core_roles = sorted(core_roles - core_roles_executed)
    missing_expansion_roles = sorted(expansion_roles_today - expansion_roles_executed)
    missing_public_lanes = sorted(set(PUBLIC_SOURCE_LANES) - source_lanes_executed)
    if p0_daily_percent < 100.0:
        degradations.append(f"P0 daily coverage is {p0_daily_percent:g}%")
    if core_daily_percent < 100.0:
        degradations.append(f"core-role daily coverage is {core_daily_percent:g}%")
    if public_lane_percent < 100.0:
        degradations.append(f"public-source lane daily coverage is {public_lane_percent:g}%")
    if len(executed_company_agnostic) < len(company_agnostic_specs):
        degradations.append("company-agnostic query execution is incomplete")
    if expansion_today_percent < 100.0:
        degradations.append("today's expansion-role shard was not fully executed")
    if as_of >= str(query_plan.get("p1_cycle_end") or as_of) and p1_cycle_percent < 100.0:
        degradations.append(f"P1 rolling three-day coverage is {p1_cycle_percent:g}%")
    if overdue_shards:
        degradations.append(
            f"{len(overdue_shards)} search shards were not executed within their required interval"
        )
    if missing_full_jd:
        degradations.append(f"{len(missing_full_jd)} jobs are missing a full JD")
    if unknown_open:
        degradations.append(f"{len(unknown_open)} jobs have unknown open status")
    if unexpected_drop:
        degradations.append("job count dropped by more than 50% versus the last healthy run")

    if hard_failures:
        status = "SOURCE_COVERAGE_FAILED"
    elif degradations:
        status = "SOURCE_COVERAGE_DEGRADED"
    else:
        status = "SOURCE_COVERAGE_HEALTHY"
    return {
        "schema_version": "3.0",
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "as_of": as_of,
        "status": status,
        "hard_failures": hard_failures,
        "degradations": list(dict.fromkeys(degradations)),
        "architecture": {
            "known_company_monitoring": True,
            "open_market_company_agnostic_discovery": True,
        },
        "registry_is_allowlist": False,
        "unregistered_companies_allowed": True,
        "registry_total": len(companies),
        "p0_total": len(p0_names),
        "p0_checked_today": len(checked_p0),
        "p0_companies_not_checked": missing_p0_today,
        "p0_company_coverage": p0_company_coverage,
        "p0_daily_coverage": {
            "checked": len(checked_p0),
            "total": len(p0_names),
            "percent": p0_daily_percent,
            "companies_not_checked": missing_p0_today,
        },
        "p1_rolling_3_day_coverage": {
            "cycle_id": p1_cycle_id,
            "cycle_start": query_plan.get("p1_cycle_start"),
            "cycle_end": query_plan.get("p1_cycle_end"),
            "checked_in_current_cycle": len(checked_p1),
            "total": len(p1_names),
            "percent": p1_cycle_percent,
            "companies_not_checked": missing_p1_cycle,
            "planned_today": sorted(planned_p1),
            "executed_today": sorted(executed_p1),
            "scheduled_cycle_percent": p1_schedule_percent,
            "scheduled_today": bool(query_plan.get("p1_scheduled_today", True)),
            "skipped_by_schedule": bool(query_plan.get("skipped_by_schedule", False)),
            "next_scheduled_run": query_plan.get("next_scheduled_run"),
        },
        "company_agnostic_queries": {
            "planned": len(company_agnostic_specs),
            "executed": len(executed_company_agnostic),
            "percent": _percent(
                len(executed_company_agnostic), len(company_agnostic_specs)
            ),
        },
        "public_source_lane_daily_coverage": {
            "planned": len(PUBLIC_SOURCE_LANES),
            "executed": len(source_lanes_executed),
            "percent": public_lane_percent,
            "missing": missing_public_lanes,
        },
        "public_source_recall_diagnostic": {
            "diagnostic_only": True,
            "lanes_with_raw_results": sorted(public_recall_raw_lanes),
            "lanes_with_candidates": sorted(public_recall_candidate_lanes),
            "raw_result_lane_percent": _percent(
                len(public_recall_raw_lanes), len(PUBLIC_SOURCE_LANES)
            ),
            "candidate_lane_percent": _percent(
                len(public_recall_candidate_lanes), len(PUBLIC_SOURCE_LANES)
            ),
            "by_lane": public_source_recall_by_lane,
        },
        "core_role_daily_coverage": {
            "planned": sorted(core_roles),
            "executed": sorted(core_roles_executed),
            "percent": core_daily_percent,
            "missing": missing_core_roles,
        },
        "expansion_role_coverage": {
            "cycle_id": query_plan.get("expansion_cycle_id"),
            "cycle_start": query_plan.get("expansion_cycle_start"),
            "cycle_end": query_plan.get("expansion_cycle_end"),
            "scheduled_today": sorted(expansion_roles_today),
            "executed_today": sorted(expansion_roles_executed),
            "percent": expansion_today_percent,
            "missing": missing_expansion_roles,
            "cycle_schedule": query_plan.get("expansion_cycle_schedule") or {},
            "cycle_roles": sorted(expansion_cycle_roles),
            "cycle_roles_executed": sorted(expansion_cycle_executed),
            "cycle_percent": expansion_cycle_percent,
        },
        "dynamic_company_monitoring_coverage": {
            "cycle_id": dynamic_cycle_id,
            "cycle_start": query_plan.get("dynamic_company_cycle_start"),
            "cycle_end": query_plan.get("dynamic_company_cycle_end"),
            "cycle_days": query_plan.get("dynamic_company_cycle_days"),
            "scheduled_today": sorted(
                query_plan.get("persistent_discovered_companies_planned") or []
            ),
            "executed_today": sorted(executed_dynamic_companies),
            "cycle_companies": sorted(dynamic_cycle_companies),
            "cycle_companies_executed": sorted(checked_dynamic_companies),
            "cycle_percent": dynamic_cycle_percent,
        },
        "newly_discovered_unregistered_companies": sorted(new_unregistered),
        "newly_verified_official_career_urls": newly_verified_official_urls,
        "companies_promoted_to_persistent_monitoring": sorted(promoted_companies),
        "companies_promoted_to_registry": sorted(promoted_to_registry),
        "companies_in_persistent_monitoring": sorted(
            str(item.get("normalized_name") or "")
            for item in unregistered_candidates
            if item.get("status") == "PERSISTENT_MONITORING"
        ),
        "search_shards_not_executed_within_required_interval": sorted(
            set(overdue_shards)
        ),
        "executed_query_ids": sorted(executed_query_ids),
        "successful_query_ids": sorted(successful_query_ids),
        "missing_query_ids": missing_query_ids,
        "query_receipt_status_counts": dict(sorted(Counter(receipt_status.values()).items())),
        "query_execution": {
            "planned": len(plan_queries),
            "terminal_receipts": len(executed_specs),
            "successful": len(successful_specs),
            "yielding_results": sum(status == "SUCCESS" for status in receipt_status.values()),
            "missing": len(missing_query_ids),
            "execution_percent": _percent(len(executed_specs), len(plan_queries)),
            "successful_percent": _percent(len(successful_specs), len(plan_queries)),
        },
        "known_job_recall": known_control_report,
        "p0_checked_companies": sorted(checked_p0),
        "p1_checked_companies": sorted(checked_p1),
        "core_roles_executed": sorted(core_roles_executed),
        "expansion_roles_executed": sorted(expansion_roles_executed),
        "dynamic_companies_executed": sorted(executed_dynamic_companies),
        "acceptance_requirements": {
            "P0_DAILY_COVERAGE": f"{p0_schedule_percent:g}%",
            "P1_ROLLING_3_DAY_COVERAGE": f"{p1_schedule_percent:g}%",
            "CORE_ROLE_DAILY_COVERAGE": f"{core_schedule_percent:g}%",
            "PUBLIC_SOURCE_LANE_DAILY_COVERAGE": f"{public_lane_schedule_percent:g}%",
            "REGISTRY_IS_ALLOWLIST": "NO",
            "UNREGISTERED_COMPANIES_ALLOWED": "YES",
        },
        "runtime_execution_coverage": {
            "P0_CHECKED_TODAY": f"{p0_daily_percent:g}%",
            "P1_CHECKED_CURRENT_CYCLE": f"{p1_cycle_percent:g}%",
            "CORE_ROLE_QUERIES_EXECUTED_TODAY": f"{core_daily_percent:g}%",
            "PUBLIC_SOURCE_LANES_EXECUTED_TODAY": f"{public_lane_percent:g}%",
            "PUBLIC_SOURCE_LANES_SUCCESSFUL_TODAY": f"{public_lane_success_percent:g}%",
        },
        "processing_funnel": {
            "discovered_count": discovered_count,
            "fetched_count": discovered_count,
            "normalized_count": len(jobs),
            "deduplicated_count": len(jobs),
            "scored_count": scored_count,
            "queued_count": queued_count,
            "deferred_count": source_deferred_count,
        },
        "configured_auto_companies": sum(
            str(item.get("monitor_mode") or "").upper() == "AUTO"
            for item in companies
        ),
        "official_auto_coverage": sum(
            str(item.get("classification") or "").startswith("AUTO_")
            for item in audit_rows.values()
        ),
        "public_search_only_companies": sorted(
            name
            for name, item in audit_rows.items()
            if str(item.get("classification") or "") == "PUBLIC_SEARCH_ONLY"
        ),
        "manual_only_companies": sorted(
            name
            for name, item in audit_rows.items()
            if str(item.get("classification") or "") == "MANUAL_ONLY"
        ),
        "broken_companies": sorted(
            name
            for name, item in audit_rows.items()
            if str(item.get("classification") or "") == "BROKEN"
        ),
        "source_counts": source_counts,
        "pagination": pagination,
        "query_lane_coverage": lane_coverage,
        "total_jobs": len(jobs),
        "jobs_missing_full_jd": missing_full_jd,
        "jobs_unknown_open_status": unknown_open,
        "canonical_jobs": canonical_count,
        "secondary_jobs": secondary_count,
        "role_family_distribution": role_distribution,
        "verification_state_distribution": verification_distribution,
        "unknown_or_unverified_jobs": sum(
            state != "VERIFIED_OPEN_JOB"
            for state in (
                str(job.get("verification_state") or "UNKNOWN") for job in jobs
            )
        ),
        "unexpected_count_drop": unexpected_drop,
        "previous_healthy_total": previous_total,
        "jobs_prevented_from_ready": prevented_ready,
        "unsafe_ready_jobs": unsafe_ready,
    }


def render_markdown(report: dict[str, Any]) -> str:
    p0 = report["p0_daily_coverage"]
    p1 = report["p1_rolling_3_day_coverage"]
    company_agnostic = report["company_agnostic_queries"]
    core = report["core_role_daily_coverage"]
    public_lanes = report["public_source_lane_daily_coverage"]
    public_recall = report["public_source_recall_diagnostic"]
    dynamic = report["dynamic_company_monitoring_coverage"]
    lines = [
        "# Discovery coverage report",
        "",
        f"Status: **{report['status']}**",
        f"As of: {report['as_of']}",
        "",
        "## Two-axis planning-policy acceptance",
        "",
        "| Requirement | Result |",
        "|---|---:|",
        *(
            f"| {key} | {value} |"
            for key, value in report["acceptance_requirements"].items()
        ),
        "",
        "These acceptance values validate the deterministic plan. Runtime execution is reported separately below and controls the health gate.",
        "",
        f"- Registry: {report['registry_total']} companies; priority metadata, not an allowlist",
        f"- P0 checked today: {p0['checked']}/{p0['total']} ({p0['percent']:g}%)",
        f"- P1 checked in current cycle: {p1['checked_in_current_cycle']}/{p1['total']} ({p1['percent']:g}%); deterministic schedule coverage {p1['scheduled_cycle_percent']:g}%",
        f"- Company-agnostic queries: {company_agnostic['executed']}/{company_agnostic['planned']}",
        f"- Core roles searched today: {len(core['executed'])}/{len(core['planned'])} ({core['percent']:g}%)",
        f"- Public source lanes: {public_lanes['executed']}/{public_lanes['planned']} ({public_lanes['percent']:g}%)",
        f"- Query terminal receipts: {report['query_execution']['terminal_receipts']}/{report['query_execution']['planned']}; successful: {report['query_execution']['successful']}; yielding results: {report['query_execution']['yielding_results']}",
        f"- Known positive controls: {report['known_job_recall']['matched']}/{report['known_job_recall']['total']} active controls ({report['known_job_recall']['recall_percent']:g}%); this is a control-sample metric, not market coverage",
        f"- New unregistered companies: {len(report['newly_discovered_unregistered_companies'])}",
        f"- Newly verified official career URLs: {len(report['newly_verified_official_career_urls'])}",
        f"- Promoted to persistent monitoring: {len(report['companies_promoted_to_persistent_monitoring'])}",
        f"- Dynamic monitoring current cycle: {len(dynamic['cycle_companies_executed'])}/{len(dynamic['cycle_companies'])} ({dynamic['cycle_percent']:g}%)",
        f"- Overdue search shards: {len(report['search_shards_not_executed_within_required_interval'])}",
        f"- Configured AUTO: {report['configured_auto_companies']}; verified/recommended AUTO: {report['official_auto_coverage']}",
        f"- Jobs: {report['total_jobs']} total; {report['canonical_jobs']} canonical; {report['secondary_jobs']} secondary",
        f"- Missing full JD: {len(report['jobs_missing_full_jd'])}",
        f"- Unknown open status: {len(report['jobs_unknown_open_status'])}",
        f"- Unknown/unverified jobs: {report['unknown_or_unverified_jobs']}",
        f"- Unsafe READY/MUST_APPLY jobs: {len(report['unsafe_ready_jobs'])}",
        "",
        "## Source health",
        "",
        "| Source | Status | Count | Attempted | Succeeded | Failed |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for name, item in report["source_counts"].items():
        lines.append(
            f"| {name} | {item['status']} | {item['count']} | {item['attempted']} | "
            f"{item['succeeded']} | {item['failed']} |"
        )
    lines.extend(["", "## Query lanes", "", "| Lane | Planned | Attempted | Completed | Failed | Candidates |", "|---|---:|---:|---:|---:|---:|"])
    for lane, item in report["query_lane_coverage"].items():
        lines.append(
            f"| {lane} | {item['planned']} | {item['attempted']} | "
            f"{item['succeeded'] + item['empty_valid']} | {item['failed']} | {item['candidate_count']} |"
        )
    lines.extend(
        [
            "",
            "## Public-source recall diagnostic",
            "",
            "Execution coverage and recall evidence are separate: a completed lane may still return no raw results or no eligible candidates.",
            "",
            f"- Lanes with raw results: {len(public_recall['lanes_with_raw_results'])}/{len(PUBLIC_SOURCE_LANES)} ({public_recall['raw_result_lane_percent']:g}%)",
            f"- Lanes with retained candidates: {len(public_recall['lanes_with_candidates'])}/{len(PUBLIC_SOURCE_LANES)} ({public_recall['candidate_lane_percent']:g}%)",
            "",
            "| Lane | Recall status | Raw results | Candidates |",
            "|---|---|---:|---:|",
        ]
    )
    for lane, item in public_recall["by_lane"].items():
        lines.append(
            f"| {lane} | {item['status']} | {item['raw_result_count']} | "
            f"{item['candidate_count']} |"
        )
    recall = report["known_job_recall"]
    lines.extend(
        [
            "",
            "## Known positive-control recall (not market coverage)",
            "",
            f"- Active controls: {recall['active_controls']}; inactive controls excluded with evidence: {recall['inactive_controls']}",
            f"- Active recall: {recall['matched']}/{recall['total']} ({recall['recall_percent']:g}%)",
            f"- Same-day planned scope: {recall['same_day_scope_matched']}/{recall['same_day_scope_total']}",
            "",
            "| Sample | Company | Active | Same-day scope | Matched | Cause | Evidence |",
            "|---|---|---:|---:|---:|---|---|",
        ]
    )
    for row in recall["rows"]:
        evidence = (
            f"{row.get('evidence','')} {row.get('activity_evidence','')}"
            .replace("|", "\\|")
        )
        lines.append(
            f"| {row.get('sample_id','')} | {row.get('company','')} | "
            f"{row.get('active')} | {row.get('same_day_scope')} | "
            f"{row.get('matched')} | {row.get('cause','')} | {evidence} |"
        )
    lines.extend(
        [
            "",
            "## P0 company coverage",
            "",
            "| Company | Checked today | State | ATS | Adapter | Probe |",
            "|---|---:|---|---|---|---|",
        ]
    )
    for item in report["p0_company_coverage"]:
        lines.append(
            f"| {item['company']} | {item['checked_today']} | "
            f"{item['classification']} | {item['detected_ats']} | "
            f"{item['recommended_adapter']} | {item['last_probe_result']} |"
        )
    lines.extend(["", "## Coverage findings", ""])
    findings = [*report["hard_failures"], *report["degradations"]]
    lines.extend(f"- {item}" for item in findings or ["No coverage gap detected."])
    lines.extend(["", "## P0 companies not checked today", ""])
    lines.extend(f"- {name}" for name in report["p0_companies_not_checked"] or ["None"])
    lines.extend(["", "## P1 companies not yet checked in current cycle", ""])
    lines.extend(
        f"- {name}"
        for name in report["p1_rolling_3_day_coverage"]["companies_not_checked"]
        or ["None"]
    )
    lines.extend(["", "## Open-market company discovery", ""])
    lines.extend(
        f"- New: {name}"
        for name in report["newly_discovered_unregistered_companies"]
    )
    lines.extend(
        f"- Persistent monitoring: {name}"
        for name in report["companies_in_persistent_monitoring"]
    )
    if not (
        report["newly_discovered_unregistered_companies"]
        or report["companies_in_persistent_monitoring"]
    ):
        lines.append("- None")
    for title, key in (
        ("Remaining PUBLIC_SEARCH_ONLY", "public_search_only_companies"),
        ("Remaining MANUAL_ONLY", "manual_only_companies"),
        ("Broken / human review required", "broken_companies"),
    ):
        lines.extend(["", f"## {title}", ""])
        lines.extend(f"- {name}" for name in report[key] or ["None"])
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build discovery completeness gate.")
    parser.add_argument("--source-registry", default="config/company_registry.yaml")
    parser.add_argument("--health", default="data/job_cache/source_health.json")
    parser.add_argument("--source-audit", default="data/job_cache/company_source_audit.json")
    parser.add_argument("--query-plan", default="data/job_cache/public_web_query_plan.json")
    parser.add_argument("--jobs", default="data/job_cache/decisioned_jobs.json")
    parser.add_argument("--scored-jobs", default="")
    parser.add_argument("--known-jobs", default="data/discovery/known_jobs.jsonl")
    parser.add_argument(
        "--discovered-companies",
        default="data/job_cache/discovered_company_candidates.json",
    )
    parser.add_argument("--history", default="data/job_cache/discovery_coverage_history.json")
    parser.add_argument("--json-output", default="data/job_cache/discovery_coverage_report.json")
    parser.add_argument("--markdown-output", default="data/job_cache/discovery_coverage_report.md")
    parser.add_argument("--as-of", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    as_of = args.as_of or _today()
    history_path = Path(args.history)
    history = read_json(history_path, [])
    if not isinstance(history, list):
        history = []
    report = build_coverage_report(
        registry=load_source_registry(args.source_registry),
        source_health=read_json(Path(args.health), {}),
        source_audit=read_json(Path(args.source_audit), {}),
        query_plan=read_json(Path(args.query_plan), {}),
        jobs=read_json(Path(args.jobs), []),
        as_of=as_of,
        discovered_companies=read_json(Path(args.discovered_companies), {}),
        history=history,
        scored_jobs=(read_json(Path(args.scored_jobs), []) if args.scored_jobs else None),
        known_jobs=load_known_jobs(args.known_jobs),
    )
    json_path = Path(args.json_output)
    markdown_path = Path(args.markdown_output)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    history.append(
        {
            "as_of": as_of,
            "status": report["status"],
            "total_jobs": report["total_jobs"],
            "generated_at": report["generated_at"],
            "p1_cycle_id": report["p1_rolling_3_day_coverage"]["cycle_id"],
            "p0_checked_companies": report["p0_checked_companies"],
            "p1_checked_companies": report["p1_checked_companies"],
            "executed_query_ids": report["executed_query_ids"],
            "core_roles_executed": report["core_roles_executed"],
            "expansion_roles_executed": report["expansion_roles_executed"],
            "expansion_cycle_id": report["expansion_role_coverage"]["cycle_id"],
            "dynamic_company_cycle_id": report[
                "dynamic_company_monitoring_coverage"
            ]["cycle_id"],
            "dynamic_companies_executed": report["dynamic_companies_executed"],
        }
    )
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        json.dumps(history[-90:], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"{report['status']}: jobs={report['total_jobs']}, "
        f"P0 checked={report['p0_checked_today']}/{report['p0_total']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
