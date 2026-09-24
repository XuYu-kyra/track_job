#!/usr/bin/env python3
"""Build the local daily application pack from canonical pipeline artifacts."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from common import load_config, normalize_whitespace, read_json
    from job_schema import is_verified_open_job
    from source_adapters import load_source_registry
except ModuleNotFoundError:
    from scripts.common import load_config, normalize_whitespace, read_json
    from scripts.job_schema import is_verified_open_job
    from scripts.source_adapters import load_source_registry


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the local daily application pack.")
    parser.add_argument("--targets-config", default="config/targets.yaml")
    parser.add_argument("--source-registry", default="config/company_registry.yaml")
    parser.add_argument("--jobs", default="data/job_cache/jobs.json")
    parser.add_argument("--decisioned", default="data/job_cache/decisioned_jobs.json")
    parser.add_argument("--health", default="data/job_cache/source_health.json")
    parser.add_argument("--manifest", default="cv/generated/generated_manifest.json")
    parser.add_argument("--output", default="data/job_cache/daily_application_pack.md")
    parser.add_argument("--as-of", default="")
    return parser.parse_args()


def _text(value: Any, limit: int = 360) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        value = str(value)
    text = normalize_whitespace(str(value or "")).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _url(job: dict[str, Any], canonical: bool = False) -> str:
    if canonical:
        return str(job.get("canonical_url") or job.get("official_url") or "")
    return str(job.get("url") or job.get("source_url") or "")


def _job_line(job: dict[str, Any]) -> str:
    return (
        f"- **{_text(job.get('company'), 80)} — {_text(job.get('title') or job.get('position'), 120)}** "
        f"({_text(job.get('location') or '地点待确认', 60)}); action={job.get('action') or '—'}, "
        f"score={job.get('matching_score') or '—'}; source={_url(job) or '—'}; "
        f"canonical={_url(job, True) or '待验证'}"
    )


def _manifest_index(manifest: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (str(item.get("company") or ""), str(item.get("title") or item.get("position") or "")): item
        for item in manifest if isinstance(item, dict)
    }


def _resume_status(item: dict[str, Any] | None) -> tuple[str, str, list[str]]:
    if not item:
        return "not generated", "", []
    statuses: list[str] = []
    paths: list[str] = []
    for language, value in (item.get("resume_variants") or {}).items():
        if not isinstance(value, dict):
            continue
        statuses.append(f"{language}:{value.get('status') or 'UNKNOWN'}")
        if value.get("pdf_path"):
            paths.append(str(value["pdf_path"]))
    return ", ".join(statuses) or "not validated", str(item.get("application_profile") or ""), paths


def build_pack(
    *, run_at: datetime, health: dict[str, Any], jobs: list[dict[str, Any]],
    decisioned: list[dict[str, Any]], manifest: list[dict[str, Any]], registry: dict[str, Any],
) -> str:
    source_records = dict(health.get("sources") or {})
    source_records.setdefault("manual_agent_china_web", {"status": "REFRESH_REQUIRED", "count": 0, "details": "no current batch health record"})
    source_records.setdefault("manual_inbox", {"status": "MANUAL_BY_DESIGN", "count": 0, "details": "human intake"})
    for name, spec in (registry.get("sources") or {}).items():
        if isinstance(spec, dict) and not bool(spec.get("enabled", True)):
            source_records.setdefault(name, {"status": "DISABLED", "count": 0, "details": "disabled in registry"})
    automatic = [record for record in source_records.values() if isinstance(record, dict) and record.get("automatic")]
    auto_ok = bool(automatic) and any(record.get("status") in {"SUCCESS", "PARTIAL", "EMPTY_VALID"} for record in automatic)
    new_jobs = [job for job in jobs if job.get("discovery_state") == "NEW"]
    updated_jobs = [job for job in jobs if job.get("discovery_state") == "UPDATED"]
    ready = [
        job for job in decisioned
        if str(job.get("action") or "").upper() in {"READY", "MUST_APPLY"}
        and is_verified_open_job(job)
    ]
    human = [job for job in decisioned if job.get("requires_human_review") or job.get("human_review_required")]
    rejected = [job for job in decisioned if str(job.get("action") or "").upper() == "REJECT"]
    manifest_by_job = _manifest_index(manifest)
    lines = [
        "# Daily Application Pack", "",
        f"Run time: {run_at.isoformat(timespec='seconds')}",
        f"Discovery status: {'SUCCESS' if auto_ok else 'DEGRADED_NO_AUTOMATIC_DISCOVERY'}", "",
        "## Source health", "", "| lane | status | jobs | attempted | succeeded | failed | details |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for lane, record in sorted(source_records.items()):
        details = record.get("details", "") if isinstance(record, dict) else ""
        if isinstance(details, (dict, list)):
            detail_text = _text(str(details), 260)
        else:
            detail_text = _text(details, 260)
        lines.append(
            f"| {_text(lane, 80)} | {record.get('status', 'FAILED')} | {record.get('count', 0)} | "
            f"{record.get('attempted', 0)} | {record.get('succeeded', 0)} | {record.get('failed', 0)} | {detail_text} |"
        )
    lines.extend(["", f"Raw current-source records: {sum(int(r.get('count', 0)) for r in source_records.values() if isinstance(r, dict))}", f"Canonical/deduplicated records: {len(jobs)}", ""])
    for heading, collection in (("New jobs discovered today", new_jobs), ("Existing jobs updated today", updated_jobs)):
        lines.extend([f"## {heading}", ""])
        lines.extend([_job_line(job) for job in collection] or ["None."])
        lines.append("")
    lines.extend(["## READY / MUST_APPLY", ""])
    if not ready:
        lines.extend(["No current READY/MUST_APPLY recommendation. No fake recommendation was created.", ""])
    for job in ready:
        manifest_item = manifest_by_job.get((str(job.get("company") or ""), str(job.get("title") or job.get("position") or "")))
        validation, profile, paths = _resume_status(manifest_item)
        description = _text(job.get("description") or job.get("page_context") or "JD not available", 700)
        evidence = (manifest_item or {}).get("selected_projects") or job.get("top_candidate_evidence_ids") or []
        base_summary = "legacy/unavailable"
        if manifest_item and manifest_item.get("base_resume_id"):
            base_summary = (
                f"id={manifest_item.get('base_resume_id')}; "
                f"role={manifest_item.get('base_role')}; "
                f"language={manifest_item.get('resume_language')}; "
                f"variant={manifest_item.get('resume_variant')}; "
                f"version={manifest_item.get('template_version')}; "
                f"sha256={manifest_item.get('source_zip_hash')}; "
                f"tailoring={manifest_item.get('tailoring_percentage')}; "
                f"page_fit={manifest_item.get('page_fit_status')}; "
                f"factual={manifest_item.get('factual_validation_status')}"
            )
        gaps = (
            job.get("semantic_review_error")
            or job.get("reject_reason")
            or job.get("semantic_conflicts")
            or job.get("notes")
            or "No explicit gap recorded; verify uncertain eligibility manually."
        )
        lines.extend([
            _job_line(job),
            f"  - JD responsibilities/requirements: {description}",
            f"  - Fit: {_text(job.get('fit_reason') or job.get('decision_reason') or job.get('role_family') or 'deterministic score and role-family match', 400)}",
            f"  - Evidence selected: {_text(', '.join(map(str, evidence)) if evidence else 'not selected', 400)}",
            f"  - Gaps/uncertainties: {_text(gaps, 500)}",
            f"  - Language/profile: {job.get('recommended_resume_language') or job.get('job_language') or 'review'} / {profile or job.get('application_profile') or job.get('recommended_application_profile') or 'review'}",
            f"  - Resume PDFs: {_text(', '.join(paths) if paths else 'not generated', 500)}",
            f"  - Golden Base: {_text(base_summary, 700)}",
            f"  - Validation: {validation}", "",
        ])
    lines.extend(["## Human review required", ""])
    lines.extend([_job_line(job) for job in human] or ["None explicitly flagged."])
    lines.extend(["", "## Rejected", ""])
    if rejected:
        for job in rejected:
            lines.append(f"{_job_line(job)}; reason={_text(job.get('reject_reason') or 'deterministic gate', 280)}")
    else:
        lines.append("None.")
    lines.extend(["", "Feishu mode: DRY_RUN (this pack builder never writes Feishu).", ""])
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    targets = load_config(args.targets_config)
    timezone = str(targets.get("schedule", {}).get("timezone") or "Asia/Shanghai")
    run_at = datetime.now(ZoneInfo(timezone))
    if args.as_of:
        run_at = datetime.fromisoformat(args.as_of).replace(tzinfo=ZoneInfo(timezone))
    content = build_pack(
        run_at=run_at,
        health=read_json(args.health, {}),
        jobs=read_json(args.jobs, []),
        decisioned=read_json(args.decisioned, []),
        manifest=read_json(args.manifest, []),
        registry=load_source_registry(args.source_registry),
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")
    print(f"Wrote daily application pack to {output}")


if __name__ == "__main__":
    main()
