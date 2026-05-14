#!/usr/bin/env python3
"""Push scored job entries and generated file references to Feishu Bitable."""

from __future__ import annotations

import argparse
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

try:
    from common import load_config, read_json
except ModuleNotFoundError:
    from scripts.common import load_config, read_json


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


FIELD_UI_SINGLE_SELECT = "SingleSelect"
FIELD_UI_MULTI_SELECT = "MultiSelect"
FIELD_UI_ATTACHMENT = "Attachment"
FIELD_UI_URL = "Url"
FIELD_UI_CREATED_TIME = "CreatedTime"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update Feishu with selected jobs.")
    parser.add_argument("--config", default="config/feishu.yaml")
    parser.add_argument("--targets-config", default="config/targets.yaml")
    parser.add_argument("--jobs", default="data/job_cache/scored_jobs.json")
    parser.add_argument("--generated", default="cv/generated/generated_manifest.json")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def load_feishu_config(path: str) -> FeishuConfig:
    raw = load_config(path)
    app = raw.get("app", {})
    bitable = raw.get("bitable", {})
    return FeishuConfig(
        app_id=os.getenv("FEISHU_APP_ID", app.get("app_id", "")),
        app_secret=os.getenv("FEISHU_APP_SECRET", app.get("app_secret", "")),
        app_token=os.getenv("FEISHU_APP_TOKEN", bitable.get("app_token", "")),
        table_id=os.getenv("FEISHU_TABLE_ID", bitable.get("table_id", "")),
        view_id=os.getenv("FEISHU_VIEW_ID", bitable.get("view_id", "")),
        fields=raw.get("fields", {}),
        defaults=raw.get("defaults", {}),
        attachments=raw.get("attachments", {}),
    )


def get_tenant_access_token(config: FeishuConfig) -> str:
    response = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": config.app_id, "app_secret": config.app_secret},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise FeishuAPIError(f"Auth failed: {payload.get('msg')}")
    return payload["tenant_access_token"]


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
    payload = response.json()
    if response.status_code >= 400:
        raise FeishuAPIError(
            f"HTTP {response.status_code} on {path}: {payload.get('msg') or response.text}"
        )
    if payload.get("code") != 0:
        raise FeishuAPIError(f"Feishu API error {payload.get('code')}: {payload.get('msg')}")
    return payload


def list_existing_records(config: FeishuConfig, token: str) -> dict[str, dict[str, Any]]:
    records_by_url: dict[str, dict[str, Any]] = {}
    page_token = None
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
            url_field_name = config.fields.get("url", "URL")
            url_value = extract_url_value(fields.get(url_field_name))
            if url_value:
                records_by_url[url_value] = record
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
    return records_by_url


def get_field_definitions(config: FeishuConfig, token: str) -> dict[str, dict[str, Any]]:
    payload = feishu_request(
        "GET",
        token,
        f"/open-apis/bitable/v1/apps/{config.app_token}/tables/{config.table_id}/fields",
    )
    return {item["field_name"]: item for item in payload.get("data", {}).get("items", [])}


def extract_url_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, dict):
            return str(first.get("link") or first.get("text") or "").strip()
    if isinstance(value, dict):
        return str(value.get("link") or value.get("text") or "").strip()
    return ""


def build_generated_lookup(generated: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(item.get("company", ""), item.get("position", "")): item for item in generated}


def format_score_breakdown(job: dict[str, Any]) -> str:
    total = int(job.get("matching_score", 0))
    breakdown = job.get("score_breakdown", {}) or {}
    if not isinstance(breakdown, dict):
        return f"total={total}"

    evidence_groups = ", ".join(breakdown.get("evidence_groups", [])) or "-"
    stack_flags = ", ".join(breakdown.get("unsupported_stack_flags", [])) or "-"
    risk_flags = ", ".join(breakdown.get("risk_flags", [])) or "-"
    best_track = breakdown.get("best_track") or "-"
    visa_bucket = breakdown.get("visa_bucket") or "-"

    lines = [
        f"total={total}",
        (
            "fit: "
            f"track={breakdown.get('role_track_fit', 0)} ({best_track}), "
            f"evidence={breakdown.get('evidence_skill_fit', 0)} [{evidence_groups}], "
            f"seniority={breakdown.get('seniority_training_fit', 0)}"
        ),
        (
            "workflow/visa: "
            f"delivery={breakdown.get('delivery_workflow_fit', 0)}, "
            f"visa={breakdown.get('visa_work_auth_fit', 0)} ({visa_bucket}), "
            f"easy_apply_bonus={breakdown.get('application_effort_bonus', 0)}"
        ),
        (
            "penalties: "
            f"stack={breakdown.get('unsupported_stack_penalty', 0)} [{stack_flags}], "
            f"experience={breakdown.get('experience_penalty', 0)}, "
            f"risk={breakdown.get('risk_penalty', 0)} [{risk_flags}]"
        ),
    ]
    return "\n".join(lines)


def upload_bitable_attachment(config: FeishuConfig, token: str, file_path: str) -> str:
    path = Path(file_path)
    if not path.exists():
        return ""
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    with path.open("rb") as handle:
        files = {"file": (path.name, handle, mime_type)}
        data = {
            "file_name": path.name,
            "parent_type": "bitable_file",
            "parent_node": config.app_token,
            "size": str(path.stat().st_size),
        }
        response = requests.post(
            "https://open.feishu.cn/open-apis/drive/v1/medias/upload_all",
            headers={"Authorization": f"Bearer {token}"},
            data=data,
            files=files,
            timeout=60,
        )
    payload = response.json()
    if response.status_code >= 400 or payload.get("code") != 0:
        raise FeishuAPIError(
            f"Attachment upload failed for {path.name}: {payload.get('msg') or response.text}"
        )
    data = payload.get("data", {})
    return str(data.get("file_token") or data.get("media_token") or data.get("token") or "")


def build_fields_payload(
    job: dict[str, Any],
    generated: dict[str, Any] | None,
    config: FeishuConfig,
    field_definitions: dict[str, dict[str, Any]],
    attachment_tokens: dict[str, str] | None = None,
) -> dict[str, Any]:
    raw_fields = {
        config.fields["company"]: job.get("company", ""),
        config.fields["position"]: job.get("position", ""),
        config.fields["url"]: job.get("url", ""),
        config.fields["status"]: config.defaults.get("status", "Need Review"),
        config.fields["progress"]: config.defaults.get("progress", ""),
        config.fields["matching_score"]: job.get("matching_score", 0),
    }
    score_breakdown_field = config.fields.get("score_breakdown")
    if score_breakdown_field:
        raw_fields[score_breakdown_field] = format_score_breakdown(job)

    if generated and config.attachments.get("store_local_path_when_upload_disabled", True):
        resume_field = config.fields.get("resume_draft")
        cover_field = config.fields.get("cover_letter")
        if resume_field:
            raw_fields[resume_field] = generated.get("resume_path", "")
        if cover_field:
            raw_fields[cover_field] = generated.get("coverletter_path", "")

    if attachment_tokens:
        resume_field = config.fields.get("resume_draft")
        cover_field = config.fields.get("cover_letter")
        if resume_field and attachment_tokens.get("resume"):
            raw_fields[resume_field] = [{"file_token": attachment_tokens["resume"]}]
        if cover_field and attachment_tokens.get("cover"):
            raw_fields[cover_field] = [{"file_token": attachment_tokens["cover"]}]

    fields: dict[str, Any] = {}
    for field_name, value in raw_fields.items():
        field_definition = field_definitions.get(field_name)
        if not field_definition:
            if field_name == config.fields.get("score_breakdown"):
                continue
            fields[field_name] = value
            continue

        ui_type = field_definition.get("ui_type")
        if ui_type == FIELD_UI_CREATED_TIME:
            continue
        if ui_type == FIELD_UI_URL:
            if value:
                fields[field_name] = {"text": str(value), "link": str(value)}
            continue
        if ui_type == FIELD_UI_SINGLE_SELECT:
            if value:
                fields[field_name] = str(value)
            continue
        if ui_type == FIELD_UI_MULTI_SELECT:
            if value:
                if isinstance(value, list):
                    fields[field_name] = [str(item) for item in value if item]
                else:
                    fields[field_name] = [str(value)]
            continue
        if ui_type == FIELD_UI_ATTACHMENT:
            if value:
                fields[field_name] = value
            continue

        fields[field_name] = value

    return fields


def sync_records(
    config: FeishuConfig,
    token: str,
    jobs: list[dict[str, Any]],
    generated_lookup: dict[tuple[str, str], dict[str, Any]],
    field_definitions: dict[str, dict[str, Any]],
    *,
    dry_run: bool,
) -> None:
    existing = list_existing_records(config, token) if not dry_run else {}

    created = 0
    updated = 0
    for job in jobs:
        generated = generated_lookup.get((job.get("company", ""), job.get("position", "")))
        attachment_tokens: dict[str, str] | None = None
        if not dry_run and generated and config.attachments.get("upload_generated_files", False):
            attachment_tokens = {}
            resume_pdf = generated.get("resume_pdf_path", "")
            cover_pdf = generated.get("coverletter_pdf_path", "")
            if resume_pdf:
                attachment_tokens["resume"] = upload_bitable_attachment(config, token, resume_pdf)
            if cover_pdf:
                attachment_tokens["cover"] = upload_bitable_attachment(config, token, cover_pdf)

        fields = build_fields_payload(job, generated, config, field_definitions, attachment_tokens)
        record = existing.get(job.get("url", ""))
        if dry_run:
            action = "update" if record else "create"
            print(f"Dry run: would {action} record for {job.get('company')} | {job.get('position')}")
            continue

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
        print(f"Feishu sync complete: created={created}, updated={updated}")


def main() -> None:
    args = parse_args()
    config = load_feishu_config(args.config)
    jobs = read_json(Path(args.jobs), [])
    generated = read_json(Path(args.generated), [])
    generated_lookup = build_generated_lookup(generated)

    if not jobs:
        print("No scored jobs to sync.")
        return

    if args.dry_run:
        sync_records(config, "", jobs, generated_lookup, {}, dry_run=True)
        return

    token = get_tenant_access_token(config)
    field_definitions = get_field_definitions(config, token)
    sync_records(config, token, jobs, generated_lookup, field_definitions, dry_run=False)


if __name__ == "__main__":
    main()
