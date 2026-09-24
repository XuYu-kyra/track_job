#!/usr/bin/env python3
"""Safely upsert canonical job records into Feishu Bitable."""

from __future__ import annotations

import argparse
import mimetypes
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests

try:
    from common import load_config, read_json
    from job_schema import is_verified_open_job
except ModuleNotFoundError:
    from scripts.common import load_config, read_json
    from scripts.job_schema import is_verified_open_job


class FeishuAPIError(RuntimeError):
    pass


@dataclass
class FeishuConfig:
    app_id: str
    app_secret: str
    app_token: str
    table_id: str
    view_id: str
    fields: dict[str, str]
    defaults: dict[str, Any]
    attachments: dict[str, Any]
    sync: dict[str, Any]
    alerts: dict[str, Any]


FIELD_UI_SINGLE_SELECT = "SingleSelect"
FIELD_UI_MULTI_SELECT = "MultiSelect"
FIELD_UI_ATTACHMENT = "Attachment"
FIELD_UI_URL = "Url"
FIELD_UI_CREATED_TIME = "CreatedTime"
FIELD_UI_DATE_TIME = "DateTime"
FIELD_UI_NUMBER = "Number"
FIELD_UI_CHECKBOX = "Checkbox"


JOB_FIELD_MAP = {
    "company": "company",
    "title": "title",
    "position": "title",
    "role_family": "role_family",
    "location": "location",
    "source": "sources",
    "official_url": "official_url",
    "url": "official_url",
    "job_id": "job_id",
    "canonical_key": "canonical_key",
    "first_seen": "first_seen",
    "last_verified": "last_verified",
    "posted_at": "posted_at",
    "deadline": "deadline",
    "status": "status",
    "freshness": "freshness",
    "technical_fit": "technical_fit",
    "career_value": "career_value",
    "skill_portability": "skill_portability",
    "strategic_value": "strategic_value",
    "opportunity_value": "opportunity_value",
    "matching_score": "opportunity_value",
    "compensation_signal": "compensation_signal",
    "wlb_signal": "wlb_signal",
    "leave_usability": "leave_usability",
    "mobility_autonomy": "mobility_autonomy",
    "commute": "commute",
    "on_call": "on_call",
    "actual_work_risks": "actual_work_risks",
    "evidence_confidence": "evidence_confidence",
    "evidence_count": "evidence_count",
    "last_lifestyle_check": "last_lifestyle_check",
    "notes": "notes",
    "urgency": "urgency",
    "scarcity": "scarcity",
    "process_trigger_risk": "process_trigger_risk",
    "application_cost": "application_cost",
    "regret": "regret",
    "release_priority": "release_priority",
    "action_tier": "action_tier",
    "action": "action",
    "reject_reason": "reject_reason",
    "resume_family": "resume_family",
    "resume_version": "resume_version",
    "applied_at": "applied_at",
    "stage": "stage",
    "next_action": "next_action",
    "next_deadline": "next_deadline",
    "round": "round",
    "team": "team",
    "manager_signal": "manager_signal",
    "questions": "questions",
    "weak_topics": "weak_topics",
    "debrief": "debrief",
    "offer": "offer",
}

HUMAN_MANAGED_KEYS = {
    "resume_version",
    "applied_at",
    "stage",
    "next_action",
    "next_deadline",
    "round",
    "team",
    "manager_signal",
    "questions",
    "weak_topics",
    "debrief",
    "offer",
    "progress",
}

READY_DOCUMENT_STATUSES = {"READY", "READY_WITH_DENSITY_WARNING"}


def require_healthy_coverage(report: dict[str, Any]) -> None:
    coverage_status = str(report.get("status") or "")
    if coverage_status != "SOURCE_COVERAGE_HEALTHY":
        raise RuntimeError(
            "Feishu apply is blocked unless discovery coverage is "
            f"SOURCE_COVERAGE_HEALTHY (current={coverage_status or 'MISSING'})"
        )


def require_review_sync_coverage(report: dict[str, Any]) -> None:
    """Allow degraded discovery records into Feishu review views safely.

    This policy does not claim source completeness and does not relax the
    per-job delivery gate.  It only permits the review pool to be refreshed
    when every planned query has a terminal receipt and no unsafe READY job is
    present; attachments are still restricted in ``generated_attachment_paths``.
    """

    status = str(report.get("status") or "")
    if status == "SOURCE_COVERAGE_HEALTHY":
        return
    execution = report.get("query_execution") or {}
    planned = int(execution.get("planned") or 0)
    terminal = int(execution.get("terminal_receipts") or 0)
    if planned < 1 or terminal != planned:
        raise RuntimeError(
            "Feishu review sync is blocked unless every planned query has a terminal receipt "
            f"(planned={planned}, terminal={terminal})"
        )
    if report.get("unsafe_ready_jobs"):
        raise RuntimeError("Feishu review sync is blocked by unsafe READY/MUST_APPLY jobs")


def require_canary_coverage(report: dict[str, Any]) -> None:
    """Allow the explicitly authorized one-row canary after the recall gate.

    A canary does not require market-wide source health, but it does require
    complete query execution, active positive-control recall of at least 80%,
    and no unsafe READY/MUST_APPLY records.
    """

    execution = report.get("query_execution") or {}
    planned = int(execution.get("planned") or 0)
    terminal = int(execution.get("terminal_receipts") or 0)
    if planned < 1 or terminal != planned:
        raise RuntimeError(
            "Feishu canary is blocked unless every planned query has a terminal receipt"
        )
    recall = report.get("known_job_recall") or {}
    if float(recall.get("recall_percent") or 0.0) < 80.0:
        raise RuntimeError(
            "Feishu canary is blocked unless active known-control recall is at least 80%"
        )
    if report.get("unsafe_ready_jobs"):
        raise RuntimeError("Feishu canary is blocked by unsafe READY/MUST_APPLY jobs")


def require_safe_delivery_jobs(jobs: list[dict[str, Any]]) -> None:
    """Fail before auth/upload when a deliverable job is not verified open."""

    unsafe = [
        str(job.get("canonical_key") or job.get("job_id") or job.get("title") or "UNKNOWN")
        for job in jobs
        if str(job.get("action") or job.get("action_tier") or "").upper()
        in {"READY", "MUST_APPLY"}
        and not is_verified_open_job(job)
    ]
    if unsafe:
        raise RuntimeError(
            "Feishu delivery/attachment upload blocked for unverified READY jobs: "
            + ", ".join(unsafe)
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview or apply a Feishu Bitable job upsert.")
    parser.add_argument("--config", default="config/feishu.yaml")
    parser.add_argument("--targets-config", default="config/targets.yaml")
    parser.add_argument("--jobs", default="data/job_cache/decisioned_jobs.json")
    parser.add_argument("--generated", default="cv/generated/generated_manifest.json")
    parser.add_argument(
        "--coverage-report",
        default="data/job_cache/discovery_coverage_report.json",
    )
    parser.add_argument(
        "--canary-job-id",
        default="",
        help="Restrict an explicit --canary apply to exactly one job ID",
    )
    parser.add_argument(
        "--canary-canonical-key",
        default="",
        help="Restrict an explicit --canary apply to exactly one canonical key",
    )
    parser.add_argument(
        "--canary",
        action="store_true",
        help="Authorize exactly one real Feishu row after the recall gate",
    )
    parser.add_argument(
        "--verify-canary",
        action="store_true",
        help="Read back and verify one previously written canary row without writing",
    )
    parser.add_argument(
        "--allow-degraded-review-sync",
        action="store_true",
        help=(
            "Allow degraded discovery coverage for review-pool records; "
            "READY/MUST_APPLY attachments remain individually gated"
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Perform Feishu writes")
    mode.add_argument("--dry-run", action="store_true", help="Explicit preview mode (the default)")
    return parser.parse_args()


def load_feishu_config(path: str) -> FeishuConfig:
    raw = load_config(path)
    app = raw.get("app", {})
    bitable = raw.get("bitable", {})
    return FeishuConfig(
        app_id=os.getenv("FEISHU_APP_ID", str(app.get("app_id", ""))),
        app_secret=os.getenv("FEISHU_APP_SECRET", str(app.get("app_secret", ""))),
        app_token=os.getenv("FEISHU_APP_TOKEN", str(bitable.get("app_token", ""))),
        table_id=os.getenv("FEISHU_TABLE_ID", str(bitable.get("table_id", ""))),
        view_id=os.getenv("FEISHU_VIEW_ID", str(bitable.get("view_id", ""))),
        fields=raw.get("fields", {}),
        defaults=raw.get("defaults", {}),
        attachments=raw.get("attachments", {}),
        sync=raw.get("sync", {}),
        alerts=raw.get("alerts", {}),
    )


def validate_live_config(config: FeishuConfig) -> None:
    required = {
        "app_id": config.app_id,
        "app_secret": config.app_secret,
        "app_token": config.app_token,
        "table_id": config.table_id,
    }
    missing = [name for name, value in required.items() if not value or value.startswith("YOUR_")]
    if missing:
        raise ValueError(f"Missing live Feishu configuration: {', '.join(missing)}")


def get_tenant_access_token(config: FeishuConfig) -> str:
    validate_live_config(config)
    response = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": config.app_id, "app_secret": config.app_secret},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise FeishuAPIError(f"Auth failed: {payload.get('msg')}")
    return str(payload["tenant_access_token"])


def feishu_request(
    method: str,
    token: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = requests.request(
        method,
        f"https://open.feishu.cn{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        json=json_body,
        timeout=30,
    )
    try:
        payload = response.json()
    except ValueError as exc:
        raise FeishuAPIError(f"Non-JSON response from {path}: HTTP {response.status_code}") from exc
    if response.status_code >= 400:
        raise FeishuAPIError(
            f"HTTP {response.status_code} on {path}: {payload.get('msg') or response.text}"
        )
    if payload.get("code") != 0:
        raise FeishuAPIError(f"Feishu API error {payload.get('code')}: {payload.get('msg')}")
    return payload


def extract_url_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list) and value:
        return extract_url_value(value[0])
    if isinstance(value, dict):
        return str(value.get("link") or value.get("text") or "").strip()
    return ""


def list_existing_records(config: FeishuConfig, token: str) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    page_token = ""
    while True:
        params: dict[str, Any] = {"page_size": 500}
        if config.view_id:
            params["view_id"] = config.view_id
        if page_token:
            params["page_token"] = page_token
        payload = feishu_request(
            "GET",
            token,
            f"/open-apis/bitable/v1/apps/{config.app_token}/tables/{config.table_id}/records",
            params=params,
        )
        data = payload.get("data", {})
        for record in data.get("items", []):
            fields = record.get("fields", {})
            url_name = config.fields.get("official_url") or config.fields.get("url")
            canonical_name = config.fields.get("canonical_key")
            url = extract_url_value(fields.get(url_name)) if url_name else ""
            canonical = str(fields.get(canonical_name) or "").strip() if canonical_name else ""
            if url:
                records[f"url:{url}"] = record
            if canonical:
                records[f"key:{canonical}"] = record
        if not data.get("has_more"):
            break
        page_token = str(data.get("page_token") or "")
    return records


def get_field_definitions(config: FeishuConfig, token: str) -> dict[str, dict[str, Any]]:
    definitions: dict[str, dict[str, Any]] = {}
    page_token = ""
    while True:
        params: dict[str, Any] = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        payload = feishu_request(
            "GET",
            token,
            f"/open-apis/bitable/v1/apps/{config.app_token}/tables/{config.table_id}/fields",
            params=params,
        )
        data = payload.get("data", {})
        definitions.update({item["field_name"]: item for item in data.get("items", [])})
        if not data.get("has_more"):
            break
        page_token = str(data.get("page_token") or "")
    return definitions


def build_generated_lookup(generated: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (str(item.get("company", "")), str(item.get("position") or item.get("title") or "")): item
        for item in generated
    }


def generated_attachment_paths(
    generated: dict[str, Any] | None,
    job: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Return only final, page-fit-approved application artifacts."""
    if not generated:
        return {}
    action = str((job or {}).get("action") or (job or {}).get("action_tier") or "").upper()
    if action not in {"READY", "MUST_APPLY"}:
        return {}
    if not is_verified_open_job(job or {}):
        return {}
    resume_path = str(generated.get("resume_pdf_path") or "").strip()
    cover_path = str(generated.get("coverletter_pdf_path") or "").strip()
    variants = generated.get("resume_variants") or {}
    if variants:
        resume_ready = any(
            str(item.get("pdf_path") or "").strip() == resume_path
            and str(item.get("status") or "") in READY_DOCUMENT_STATUSES
            for item in variants.values()
            if isinstance(item, dict)
        )
        if not resume_ready:
            return {}
    paths: dict[str, str] = {}
    if resume_path:
        paths["resume"] = resume_path
    if cover_path:
        paths["cover"] = cover_path
    return paths


def format_score_breakdown(job: dict[str, Any]) -> str:
    breakdown = job.get("score_breakdown", {}) or {}
    skills = ", ".join(breakdown.get("matched_candidate_skills", [])) or "-"
    return "\n".join(
        [
            f"opportunity={job.get('opportunity_value', job.get('matching_score', 0))}",
            (
                f"technical={job.get('technical_fit', 0)}, career={job.get('career_value', 0)}, "
                f"portability={job.get('skill_portability', 0)}, strategic={job.get('strategic_value', 0)}"
            ),
            f"candidate_skills=[{skills}]",
            (
                f"urgency={job.get('urgency', 0)}, scarcity={job.get('scarcity', 0)}, "
                f"regret={job.get('regret', 0)}, cost={job.get('application_cost', 0)}, "
                f"release={job.get('release_priority', 0)}"
            ),
        ]
    )


def upload_bitable_attachment(config: FeishuConfig, token: str, file_path: str) -> str:
    path = Path(file_path)
    # Generated manifests are shared by native Windows and WSL. Resolve a
    # relative path written with the other platform's separator before upload.
    if not path.exists() and os.name != "nt" and "\\" in file_path:
        cross_platform_path = Path(file_path.replace("\\", "/"))
        if cross_platform_path.exists():
            path = cross_platform_path
    if not path.exists():
        raise FeishuAPIError(f"Attachment file does not exist: {file_path}")
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with path.open("rb") as handle:
        response = requests.post(
            "https://open.feishu.cn/open-apis/drive/v1/medias/upload_all",
            headers={"Authorization": f"Bearer {token}"},
            data={
                "file_name": path.name,
                "parent_type": "bitable_file",
                "parent_node": config.app_token,
                "size": str(path.stat().st_size),
            },
            files={"file": (path.name, handle, mime_type)},
            timeout=60,
        )
    payload = response.json()
    if response.status_code >= 400 or payload.get("code") != 0:
        raise FeishuAPIError(
            f"Attachment upload failed for {path.name}: {payload.get('msg') or response.text}"
        )
    data = payload.get("data", {})
    return str(data.get("file_token") or data.get("media_token") or data.get("token") or "")


def _datetime_ms(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(text[:10]), datetime.min.time())
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _coerce_value(value: Any, field_definition: dict[str, Any] | None) -> Any:
    if not field_definition:
        if isinstance(value, list):
            return ", ".join(str(item) for item in value)
        return value
    ui_type = field_definition.get("ui_type")
    if ui_type == FIELD_UI_CREATED_TIME:
        return None
    if ui_type == FIELD_UI_URL:
        return {"text": str(value), "link": str(value)} if value else None
    if ui_type == FIELD_UI_SINGLE_SELECT:
        if isinstance(value, list):
            value = value[0] if value else ""
        return str(value) if value else None
    if ui_type == FIELD_UI_MULTI_SELECT:
        values = value if isinstance(value, list) else [value]
        return [str(item) for item in values if item] or None
    if ui_type == FIELD_UI_ATTACHMENT:
        if not isinstance(value, list) or any(
            not isinstance(item, dict) or not str(item.get("file_token") or "").strip()
            for item in value
        ):
            raise FeishuAPIError(
                "Attachment field value must be a list of non-empty file_token objects"
            )
        return [{"file_token": str(item["file_token"])} for item in value]
    if ui_type == FIELD_UI_DATE_TIME:
        return _datetime_ms(value)
    if ui_type == FIELD_UI_NUMBER:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if ui_type == FIELD_UI_CHECKBOX:
        return bool(value)
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return value


def build_fields_payload(
    job: dict[str, Any],
    generated: dict[str, Any] | None,
    config: FeishuConfig,
    field_definitions: dict[str, dict[str, Any]],
    attachment_tokens: dict[str, str] | None = None,
    *,
    for_update: bool = False,
) -> dict[str, Any]:
    raw_fields: dict[str, Any] = {}
    for config_key, field_name in config.fields.items():
        if not field_name or config_key in {"resume_draft", "cover_letter", "score_breakdown"}:
            continue
        if config_key in HUMAN_MANAGED_KEYS:
            continue
        job_key = JOB_FIELD_MAP.get(config_key)
        if not job_key:
            continue
        value = job.get(job_key)
        if config_key == "official_url" and not value:
            value = job.get("url")
        if value not in (None, "", [], {}):
            raw_fields[field_name] = value

    score_field = config.fields.get("score_breakdown")
    if score_field:
        raw_fields[score_field] = format_score_breakdown(job)

    if not for_update:
        for config_key in ("status", "progress"):
            if config_key in HUMAN_MANAGED_KEYS:
                continue
            field_name = config.fields.get(config_key)
            default = config.defaults.get(config_key)
            if field_name and field_name not in raw_fields and default not in (None, ""):
                raw_fields[field_name] = default

    if generated and config.attachments.get("store_local_path_when_upload_disabled", False):
        if config.fields.get("resume_draft"):
            raw_fields[config.fields["resume_draft"]] = generated.get("resume_path", "")
        if config.fields.get("cover_letter"):
            raw_fields[config.fields["cover_letter"]] = generated.get("coverletter_path", "")
    if attachment_tokens:
        if config.fields.get("resume_draft") and attachment_tokens.get("resume"):
            raw_fields[config.fields["resume_draft"]] = [{"file_token": attachment_tokens["resume"]}]
        if config.fields.get("cover_letter") and attachment_tokens.get("cover"):
            raw_fields[config.fields["cover_letter"]] = [{"file_token": attachment_tokens["cover"]}]

    fields: dict[str, Any] = {}
    for field_name, value in raw_fields.items():
        coerced = _coerce_value(value, field_definitions.get(field_name))
        if coerced not in (None, "", [], {}):
            fields[field_name] = coerced
    return fields


def _find_record(existing: dict[str, dict[str, Any]], job: dict[str, Any]) -> dict[str, Any] | None:
    url = str(job.get("official_url") or job.get("url") or "")
    key = str(job.get("canonical_key") or "")
    return existing.get(f"url:{url}") or existing.get(f"key:{key}")


def assert_frozen_schema_compatible(
    config: FeishuConfig,
    field_definitions: dict[str, dict[str, Any]],
) -> None:
    """Abort production sync before any write when the frozen schema differs."""

    try:
        from setup_feishu import compare_schema, configured_specs, frozen_mapping_errors
    except ModuleNotFoundError:
        from scripts.setup_feishu import compare_schema, configured_specs, frozen_mapping_errors

    mapping_errors = frozen_mapping_errors(config)
    if mapping_errors:
        raise FeishuAPIError(
            "Frozen Feishu field mapping mismatch; no records were written: "
            + "; ".join(mapping_errors)
        )
    missing, mismatches = compare_schema(configured_specs(config), field_definitions)
    if missing or mismatches:
        details = [
            *(f"missing field: {item['field_name']}" for item in missing),
            *mismatches,
        ]
        raise FeishuAPIError(
            "Frozen Feishu schema preflight failed; no records were written: "
            + "; ".join(details)
        )


def sync_records(
    config: FeishuConfig,
    token: str,
    jobs: list[dict[str, Any]],
    generated_lookup: dict[tuple[str, str], dict[str, Any]],
    field_definitions: dict[str, dict[str, Any]],
    *,
    dry_run: bool,
) -> None:
    require_safe_delivery_jobs(jobs)
    if not dry_run:
        assert_frozen_schema_compatible(config, field_definitions)
    existing = list_existing_records(config, token) if not dry_run else {}
    created = 0
    updated = 0
    attachments_uploaded = 0
    attachments_skipped_review_only = 0
    attachment_blocked = 0
    for job in jobs:
        title = str(job.get("title") or job.get("position") or "")
        generated = generated_lookup.get((str(job.get("company", "")), title))
        action = str(job.get("action") or job.get("action_tier") or "").upper()
        record = _find_record(existing, job) if existing else None
        attachment_paths = (
            generated_attachment_paths(generated, job)
            if config.attachments.get("upload_generated_files", False)
            else {}
        )
        if (
            not dry_run
            and config.attachments.get("upload_generated_files", False)
            and action in {"READY", "MUST_APPLY"}
            and not attachment_paths
        ):
            attachment_blocked += 1
            print(
                "Feishu skip: blocked READY/MUST_APPLY without verified page-fit attachment: "
                + str(job.get("canonical_key") or job.get("job_id") or title)
            )
            continue
        if dry_run:
            fields = build_fields_payload(job, generated, config, field_definitions, for_update=False)
            attachments = (
                attachment_paths
                if config.attachments.get("upload_generated_files", False)
                else {}
            )
            attachment_preview = ",".join(sorted(attachments)) or "none"
            if generated and action not in {"READY", "MUST_APPLY"}:
                attachments_skipped_review_only += 1
            print(
                f"Dry run: would upsert {job.get('company')} | {title} | "
                f"action={job.get('action', '')} | fields={len(fields)} | "
                f"attachments={attachment_preview}"
            )
            continue

        attachment_tokens: dict[str, str] | None = None
        if generated and config.attachments.get("upload_generated_files", False):
            if action not in {"READY", "MUST_APPLY"}:
                attachments_skipped_review_only += 1
            attachment_tokens = {}
            if attachment_paths.get("resume"):
                attachment_tokens["resume"] = upload_bitable_attachment(
                    config, token, attachment_paths["resume"]
                )
                attachments_uploaded += 1
            if attachment_paths.get("cover"):
                attachment_tokens["cover"] = upload_bitable_attachment(
                    config, token, attachment_paths["cover"]
                )
                attachments_uploaded += 1
        fields = build_fields_payload(
            job,
            generated,
            config,
            field_definitions,
            attachment_tokens,
            for_update=bool(record),
        )
        if record:
            feishu_request(
                "PUT",
                token,
                f"/open-apis/bitable/v1/apps/{config.app_token}/tables/{config.table_id}/records/{record['record_id']}",
                json_body={"fields": fields},
            )
            updated += 1
        else:
            feishu_request(
                "POST",
                token,
                f"/open-apis/bitable/v1/apps/{config.app_token}/tables/{config.table_id}/records",
                json_body={"fields": fields},
            )
            created += 1
    if not dry_run:
        print(
            "Feishu sync complete: "
            f"created={created}, updated={updated}, "
            f"attachments_uploaded={attachments_uploaded}, "
            f"attachments_skipped_review_only={attachments_skipped_review_only}, "
            f"attachment_blocked={attachment_blocked}"
        )


def main() -> None:
    args = parse_args()
    config = load_feishu_config(args.config)
    jobs = read_json(Path(args.jobs), [])
    generated = read_json(Path(args.generated), [])
    generated_lookup = build_generated_lookup(generated if isinstance(generated, list) else [])
    if not jobs:
        print("No decisioned jobs to sync.")
        return
    selectors = {
        "job_id": str(args.canary_job_id or "").strip(),
        "canonical_key": str(args.canary_canonical_key or "").strip(),
    }
    if args.canary and args.verify_canary:
        raise ValueError("--canary and --verify-canary are mutually exclusive")
    if args.canary and not args.apply:
        raise ValueError("--canary requires --apply")
    if (args.canary or args.verify_canary) and not any(selectors.values()):
        raise ValueError("canary selection requires --canary-job-id or --canary-canonical-key")
    if any(selectors.values()):
        jobs = [
            job for job in jobs
            if (not selectors["job_id"] or str(job.get("job_id") or "") == selectors["job_id"])
            and (
                not selectors["canonical_key"]
                or str(job.get("canonical_key") or "") == selectors["canonical_key"]
            )
        ]
        if (args.canary or args.verify_canary) and len(jobs) != 1:
            raise ValueError(f"canary selector must match exactly one job (matched={len(jobs)})")
        if not jobs:
            raise ValueError("selected Feishu canary job was not found")
    require_safe_delivery_jobs(jobs)
    if not args.apply and not args.verify_canary:
        sync_records(config, "", jobs, generated_lookup, {}, dry_run=True)
        print("Preview only. Re-run with --apply to write to Feishu.")
        return
    coverage = read_json(Path(args.coverage_report), {})
    if args.canary or args.verify_canary:
        require_canary_coverage(coverage if isinstance(coverage, dict) else {})
    else:
        if args.allow_degraded_review_sync:
            require_review_sync_coverage(coverage if isinstance(coverage, dict) else {})
        else:
            require_healthy_coverage(coverage if isinstance(coverage, dict) else {})
    token = get_tenant_access_token(config)
    field_definitions = get_field_definitions(config, token)
    if args.verify_canary:
        job = jobs[0]
        records = list_existing_records(config, token)
        record = _find_record(records, job)
        if not record:
            raise FeishuAPIError("canary record was not found by Official URL or Canonical Key")
        generated = generated_lookup.get(
            (str(job.get("company") or ""), str(job.get("title") or job.get("position") or ""))
        )
        managed_fields = build_fields_payload(
            job, generated, config, field_definitions, for_update=False
        )
        protected_names = {
            config.fields.get(key)
            for key in HUMAN_MANAGED_KEYS
            if config.fields.get(key)
        }
        protected_written = sorted(set(managed_fields).intersection(protected_names))
        if protected_written:
            raise FeishuAPIError(
                "canary managed payload contains protected human fields: "
                + ", ".join(protected_written)
            )
        fields = record.get("fields") or {}
        canonical_name = config.fields.get("canonical_key") or "Canonical Key"
        url_name = config.fields.get("official_url") or "Official URL"
        attachment_names = [
            name for name in (config.fields.get("resume_draft"), config.fields.get("cover_letter"))
            if name
        ]
        missing_attachments = [name for name in attachment_names if not fields.get(name)]
        if missing_attachments:
            raise FeishuAPIError(
                "canary attachment fields are empty: " + ", ".join(missing_attachments)
            )
        print(
            "Canary verification: "
            f"record_id={record.get('record_id','')}, "
            f"canonical_key_match={str(fields.get(canonical_name) == job.get('canonical_key')).upper()}, "
            f"official_url_match={str(extract_url_value(fields.get(url_name)) == job.get('official_url', job.get('url'))).upper()}, "
            f"managed_fields={len(managed_fields)}, protected_fields_written={len(protected_written)}, "
            f"attachments_present={len(attachment_names)}, view_id={config.view_id or 'default'}"
        )
        print("Canary verification complete: read-only; no Feishu writes performed.")
        return
    sync_records(config, token, jobs, generated_lookup, field_definitions, dry_run=False)


if __name__ == "__main__":
    main()
