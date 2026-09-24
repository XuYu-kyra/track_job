#!/usr/bin/env python3
"""Load and compare independent known-job positive controls."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def load_known_jobs(path: str | Path = "data/discovery/known_jobs.jsonl") -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    source = Path(path)
    if not source.is_file():
        return records
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if not isinstance(item, dict) or not item.get("sample_id"):
            raise ValueError("known job records require sample_id")
        records.append(item)
    return records


def known_job_recall(
    known_jobs: list[dict[str, Any]],
    discovered_jobs: list[dict[str, Any]],
    *,
    query_plan: dict[str, Any] | None = None,
    query_receipts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Match controls and explain misses using same-day plan/receipt evidence.

    The recall percentage is a control-sample metric, never a market-coverage
    claim. Controls default to active when no closure evidence is recorded; an
    explicit ``active=false`` or inactive lifecycle status removes a control from
    the active denominator and is reported rather than silently discarded.
    """

    discovered_keys: set[str] = set()
    for job in discovered_jobs:
        for key in ("job_id", "url", "official_url", "canonical_url"):
            value = str(job.get(key) or "").strip().casefold()
            if value:
                discovered_keys.add(value)
        fallback = "|".join(
            str(job.get(key) or "").strip().casefold()
            for key in ("company", "title", "location")
        )
        if fallback != "||":
            discovered_keys.add(fallback)
    plan_items = [
        item for item in (query_plan or {}).get("queries", [])
        if isinstance(item, dict)
    ]
    receipts = [item for item in (query_receipts or []) if isinstance(item, dict)]
    receipts_by_id = {
        str(item.get("query_id") or ""): item
        for item in receipts
        if str(item.get("query_id") or "")
    }
    inactive_statuses = {"CLOSED", "STALE", "INACTIVE", "EXPIRED", "WITHDRAWN"}

    def control_active(control: dict[str, Any]) -> tuple[bool, str]:
        if control.get("active") is False:
            return False, "EXPLICITLY_INACTIVE"
        status = str(
            control.get("activity_status")
            or control.get("lifecycle_status")
            or "ACTIVE_UNVERIFIED"
        ).upper()
        if status in inactive_statuses:
            return False, status
        return True, status

    def host(url: str) -> str:
        try:
            return urlparse(url).netloc.casefold()
        except ValueError:
            return ""

    def company_names(control: dict[str, Any]) -> set[str]:
        names = {
            str(control.get("company") or "").strip().casefold()
        }
        names.update(
            str(item).strip().casefold()
            for item in control.get("company_aliases") or []
            if str(item).strip()
        )
        return {item for item in names if item}

    rows: list[dict[str, Any]] = []
    for known in known_jobs:
        keys = {
            str(known.get(key) or "").strip().casefold()
            for key in ("job_id", "url", "canonical_url")
            if str(known.get(key) or "").strip()
        }
        fallback = "|".join(
            str(known.get(key) or "").strip().casefold()
            for key in ("company", "title", "location")
        )
        matched = bool(keys.intersection(discovered_keys)) or fallback in discovered_keys
        active, activity_status = control_active(known)
        known_urls = {
            str(known.get(key) or "").strip()
            for key in ("url", "canonical_url")
            if str(known.get(key) or "").strip()
        }
        control_names = company_names(known)
        planned = [
            item for item in plan_items
            if any(
                str(company).strip().casefold() in control_names
                for company in item.get("companies") or []
            )
        ]
        planned_ids = [str(item.get("query_id") or "") for item in planned]
        scoped_receipts = [
            receipts_by_id[item]
            for item in planned_ids
            if item in receipts_by_id
        ]
        raw_urls = list(dict.fromkeys(
            str(url)
            for receipt in scoped_receipts
            for url in receipt.get("source_urls") or []
            if isinstance(url, str)
        ))
        exact_raw = sorted(known_urls.intersection(raw_urls))
        host_raw = sorted({url for url in raw_urls if host(url) and host(url) in {host(item) for item in known_urls}})
        statuses = [str(item.get("status") or "").upper() for item in scoped_receipts]
        if matched:
            cause = "MATCHED"
            evidence = "Matched by stable ID, URL, or company/title/location fallback."
        elif not planned:
            cause = "QUERY_NOT_PLANNED_TODAY"
            evidence = "No same-day query in the supplied plan for this control's company."
        elif exact_raw:
            cause = "URL_RETURNED_NOT_RETAINED"
            evidence = "The exact control URL was in raw receipt URLs but no normalized job matched it."
        elif host_raw:
            cause = "URL_HOST_RETURNED_NOT_RETAINED"
            evidence = "The control host appeared in raw receipt URLs, but the exact control URL was not retained."
        elif statuses and all(status == "EMPTY_VALID" for status in statuses):
            cause = "QUERY_RETURNED_NO_URL"
            evidence = "All same-day company receipts were EMPTY_VALID with zero raw URLs."
        elif any(status in {"FAILED", "RATE_LIMITED", "CAPTCHA", "PARSE_ERROR", "TIMEOUT"} for status in statuses):
            cause = "PROVIDER_OR_DETAIL_FAILURE"
            evidence = "At least one same-day company receipt ended in a provider/detail failure."
        elif statuses:
            cause = "URL_NOT_RETURNED_BY_QUERY"
            evidence = "Same-day company receipts completed, but the control URL was not among raw results."
        else:
            cause = "NO_RECEIPT_FOR_PLANNED_QUERY"
            evidence = "A company query was planned, but no matching receipt was available."
        rows.append({
            "sample_id": known.get("sample_id"),
            "company": known.get("company"),
            "title": known.get("title"),
            "matched": matched,
            "active": active,
            "activity_status": activity_status,
            "activity_evidence": (
                f"verified_at={known.get('verified_at')}"
                if known.get("verified_at")
                else "No lifecycle evidence recorded"
            ),
            "same_day_scope": bool(planned),
            "query_ids": planned_ids,
            "receipt_statuses": statuses,
            "raw_result_urls": raw_urls[:30],
            "matching_raw_urls": exact_raw or host_raw,
            "cause": cause,
            "evidence": evidence,
        })
    active_rows = [row for row in rows if row["active"]]
    inactive_rows = [row for row in rows if not row["active"]]
    matched = sum(bool(row["matched"]) for row in active_rows)
    return {
        "total": len(active_rows),
        "matched": matched,
        "recall_percent": round(100.0 * matched / len(active_rows), 2) if active_rows else 100.0,
        "all_controls_total": len(rows),
        "active_controls": len(active_rows),
        "inactive_controls": len(inactive_rows),
        "inactive_sample_ids": [row["sample_id"] for row in inactive_rows],
        "same_day_scope_total": sum(bool(row["same_day_scope"]) for row in active_rows),
        "same_day_scope_matched": sum(
            bool(row["same_day_scope"] and row["matched"]) for row in active_rows
        ),
        "rows": rows,
    }
