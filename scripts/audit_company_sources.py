#!/usr/bin/env python3
"""Audit all registry companies without mutating their automation mode."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests

try:
    from ats_detection import ats_adapter_status, detect_ats_evidence, detect_ats_family
    from official_adapters import adapter_for_company, parse_jsonld_job_postings
    from source_adapters import load_source_registry
except ModuleNotFoundError:
    from scripts.ats_detection import ats_adapter_status, detect_ats_evidence, detect_ats_family
    from scripts.official_adapters import adapter_for_company, parse_jsonld_job_postings
    from scripts.source_adapters import load_source_registry


AUDIT_STATES = frozenset(
    {
        "AUTO_API",
        "AUTO_STRUCTURED",
        "BROWSER_PUBLIC",
        "PUBLIC_SEARCH_ONLY",
        "MANUAL_ONLY",
        "BROKEN",
        "UNKNOWN",
    }
)
API_ADAPTER_FAMILIES = frozenset(
    {"Workday", "Beisen", "Hotjob", "Feishu Recruiting"}
)
SCRIPT_RE = re.compile(r"<script[^>]+src=[\"']([^\"']+)", re.IGNORECASE)
ENDPOINT_RE = re.compile(
    r"(?:https?:)?//[^\"'\s<>]+|/[A-Za-z0-9_./-]*(?:api|jobs?|positions?)[A-Za-z0-9_?=&./-]*",
    re.IGNORECASE,
)


@dataclass
class CompanyAudit:
    company: str
    priority: str
    official_url: str
    classification: str
    detected_ats: str
    recommended_adapter: str
    pagination_support: bool
    full_jd_support: bool
    last_probe_result: str
    reason: str
    probed_at: str
    configured_monitor_mode: str


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def _configured_fallback(company: dict[str, Any], reason: str = "") -> CompanyAudit:
    configured_state = str(company.get("source_audit_state") or "").upper()
    if configured_state:
        state = configured_state
    elif str(company.get("monitor_mode") or "").upper() == "MANUAL":
        state = "MANUAL_ONLY"
    else:
        state = "PUBLIC_SEARCH_ONLY"
    if state not in AUDIT_STATES:
        state = "UNKNOWN"
    configured_reason = str(company.get("source_audit_reason") or reason or "")
    return CompanyAudit(
        company=str(company.get("name") or ""),
        priority=str(company.get("monitor_priority") or ""),
        official_url=str(company.get("official_career_url") or ""),
        classification=state,
        detected_ats=str(company.get("ats_family") or "UNKNOWN"),
        recommended_adapter=str(company.get("recommended_adapter") or "public_search"),
        pagination_support=False,
        full_jd_support=False,
        last_probe_result="NOT_PROBED",
        reason=configured_reason or "no verified official career URL is configured",
        probed_at=_now(),
        configured_monitor_mode=str(company.get("monitor_mode") or ""),
    )


def _probe_api_adapter(
    company: dict[str, Any],
    session: requests.Session,
    detected_ats: str,
) -> tuple[bool, Any, str]:
    """Probe one public listing page so URL-shaped ATS guesses are not promoted."""
    selected_company = dict(company)
    selected_company["ats_family"] = detected_ats
    adapter = adapter_for_company(session, selected_company)
    try:
        page = adapter.list_jobs(None)
    except Exception as exc:  # public network/parser boundary, reported in audit
        return False, adapter, f"public API probe failed: {str(exc)[:240]}"
    return (
        True,
        adapter,
        f"public API probe succeeded: {len(page.records)} records on first page",
    )


def audit_company(
    company: dict[str, Any],
    session: requests.Session,
    *,
    timeout: int = 20,
    offline: bool = False,
) -> CompanyAudit:
    url = str(company.get("official_career_url") or "").strip()
    if not url:
        return _configured_fallback(company)
    if offline:
        fallback = _configured_fallback(company, "offline audit used configured evidence")
        fallback.last_probe_result = "SKIPPED_OFFLINE"
        fallback.official_url = url
        fallback.detected_ats = str(
            company.get("ats_family") or detect_ats_family(url) or "UNKNOWN"
        )
        return fallback

    probed_at = _now()
    try:
        response = session.get(url, timeout=timeout, allow_redirects=True)
        status_code = int(response.status_code)
        response.raise_for_status()
        body = str(response.text or "")[:1_000_000]
        scripts = [urljoin(response.url, value) for value in SCRIPT_RE.findall(body)]
        endpoints = [
            urljoin(response.url, value)
            for value in ENDPOINT_RE.findall(body)
            if "api" in value.casefold() or "job" in value.casefold() or "position" in value.casefold()
        ][:40]
        detection = detect_ats_evidence(
            response.url or url,
            html_text=body,
            script_urls=scripts,
            endpoint_urls=endpoints,
        )
        detected = detection.family
        selected_company = dict(company)
        selected_company["ats_family"] = detected
        adapter = adapter_for_company(session, selected_company)
        jsonld_jobs = parse_jsonld_job_postings(body, response.url or url)
        job_link_count = len(
            re.findall(
                r"href=[\"'][^\"']*(?:job|position|career|vacan)[^\"']*[\"']",
                body,
                flags=re.IGNORECASE,
            )
        )
        api_ok = False
        api_reason = ""
        if detected in API_ADAPTER_FAMILIES:
            api_ok, adapter, api_reason = _probe_api_adapter(
                company, session, detected
            )
        if detected in API_ADAPTER_FAMILIES and api_ok:
            classification = "AUTO_API"
        elif detected in API_ADAPTER_FAMILIES:
            classification = "PUBLIC_SEARCH_ONLY"
        elif jsonld_jobs or job_link_count:
            classification = "AUTO_STRUCTURED"
        elif scripts and len(re.sub(r"<[^>]+>", " ", body).strip()) < 500:
            classification = "BROWSER_PUBLIC"
        else:
            configured = str(company.get("source_audit_state") or "").upper()
            classification = (
                configured
                if configured in {"PUBLIC_SEARCH_ONLY", "MANUAL_ONLY"}
                else "PUBLIC_SEARCH_ONLY"
            )
        recommended = (
            adapter.adapter_name
            if classification in {"AUTO_API", "AUTO_STRUCTURED"}
            else "playwright_public_network"
            if classification == "BROWSER_PUBLIC"
            else "public_search"
        )
        evidence = list(detection.evidence)
        if jsonld_jobs:
            evidence.append(f"jsonld_jobpostings:{len(jsonld_jobs)}")
        if job_link_count:
            evidence.append(f"server_rendered_job_links:{job_link_count}")
        if api_reason:
            evidence.append(api_reason)
        return CompanyAudit(
            company=str(company.get("name") or ""),
            priority=str(company.get("monitor_priority") or ""),
            official_url=url,
            classification=classification,
            detected_ats=detected,
            recommended_adapter=recommended,
            pagination_support=bool(adapter.supports_pagination),
            full_jd_support=bool(adapter.supports_full_jd),
            last_probe_result=(
                f"HTTP_{status_code}_API_OK"
                if detected in API_ADAPTER_FAMILIES and api_ok
                else f"HTTP_{status_code}_API_FAILED"
                if detected in API_ADAPTER_FAMILIES
                else f"HTTP_{status_code}"
            ),
            reason="; ".join(evidence) or "reachable public career page; no reusable structured feed confirmed",
            probed_at=probed_at,
            configured_monitor_mode=str(company.get("monitor_mode") or ""),
        )
    except requests.RequestException as exc:
        status_code = int(getattr(getattr(exc, "response", None), "status_code", 0) or 0)
        detected = detect_ats_family(url)
        if detected in API_ADAPTER_FAMILIES:
            api_ok, adapter, api_reason = _probe_api_adapter(
                company, session, detected
            )
            if api_ok:
                return CompanyAudit(
                    company=str(company.get("name") or ""),
                    priority=str(company.get("monitor_priority") or ""),
                    official_url=url,
                    classification="AUTO_API",
                    detected_ats=detected,
                    recommended_adapter=adapter.adapter_name,
                    pagination_support=bool(adapter.supports_pagination),
                    full_jd_support=bool(adapter.supports_full_jd),
                    last_probe_result=(
                        f"HTTP_{status_code}_API_OK" if status_code else "PAGE_ERROR_API_OK"
                    ),
                    reason=f"career landing page failed ({str(exc)[:160]}); {api_reason}",
                    probed_at=probed_at,
                    configured_monitor_mode=str(company.get("monitor_mode") or ""),
                )
        return CompanyAudit(
            company=str(company.get("name") or ""),
            priority=str(company.get("monitor_priority") or ""),
            official_url=url,
            classification="BROKEN",
            detected_ats=str(company.get("ats_family") or detected),
            recommended_adapter="human_review",
            pagination_support=False,
            full_jd_support=False,
            last_probe_result=f"HTTP_{status_code}" if status_code else "NETWORK_ERROR",
            reason=(
                f"{str(exc)[:200]}; {api_reason}"
                if detected in API_ADAPTER_FAMILIES
                else str(exc)[:300]
            ),
            probed_at=probed_at,
            configured_monitor_mode=str(company.get("monitor_mode") or ""),
        )


def audit_registry(
    registry: dict[str, Any],
    *,
    offline: bool = False,
    timeout: int = 20,
) -> dict[str, Any]:
    companies = [
        company
        for company in registry.get("companies", [])
        if isinstance(company, dict) and bool(company.get("enabled", True))
    ]

    def run(company: dict[str, Any]) -> CompanyAudit:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": "jobhunter-source-audit/2.0 (+public career page audit)",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )
        return audit_company(company, session, timeout=timeout, offline=offline)

    if offline:
        audits = [run(company) for company in companies]
    else:
        with ThreadPoolExecutor(max_workers=4) as executor:
            audits = list(executor.map(run, companies))
    counts = {state: 0 for state in sorted(AUDIT_STATES)}
    for item in audits:
        counts[item.classification] += 1
    p0 = [item for item in audits if item.priority == "P0"]
    return {
        "schema_version": "2.0",
        "generated_at": _now(),
        "offline": offline,
        "total_companies": len(audits),
        "p0_total": len(p0),
        "p0_explicit": sum(item.classification != "UNKNOWN" for item in p0),
        "classification_counts": counts,
        "companies": [asdict(item) for item in audits],
    }


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Company source audit",
        "",
        f"Generated: {payload['generated_at']}",
        f"Companies: {payload['total_companies']}; P0 explicit: {payload['p0_explicit']}/{payload['p0_total']}",
        "",
        "| Company | Priority | State | ATS | Adapter | Pagination | Full JD | Probe | Reason |",
        "|---|---|---|---|---|---:|---:|---|---|",
    ]
    for item in payload["companies"]:
        reason = str(item["reason"]).replace("|", "\\|").replace("\n", " ")
        values = dict(item)
        values["reason"] = reason
        lines.append(
            "| {company} | {priority} | {classification} | {detected_ats} | "
            "{recommended_adapter} | {pagination_support} | {full_jd_support} | "
            "{last_probe_result} | {reason} |".format(**values)
        )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit configured public company sources.")
    parser.add_argument("--source-registry", default="config/company_registry.yaml")
    parser.add_argument("--json-output", default="data/job_cache/company_source_audit.json")
    parser.add_argument("--markdown-output", default="data/job_cache/company_source_audit.md")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--offline", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = audit_registry(
        load_source_registry(args.source_registry),
        offline=args.offline,
        timeout=args.timeout,
    )
    json_path = Path(args.json_output)
    markdown_path = Path(args.markdown_output)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown_path.write_text(render_markdown(payload), encoding="utf-8")
    print(
        f"Audited {payload['total_companies']} companies; "
        f"P0 explicit={payload['p0_explicit']}/{payload['p0_total']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
