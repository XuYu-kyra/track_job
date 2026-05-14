#!/usr/bin/env python3
"""Send pipeline failure alerts into Feishu Bitable."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

try:
    from common import load_config
except ModuleNotFoundError:
    from scripts.common import load_config

try:
    from update_feishu import (
        FeishuConfig,
        feishu_request,
        get_field_definitions,
        get_tenant_access_token,
        load_feishu_config,
        upload_bitable_attachment,
    )
except ModuleNotFoundError:
    from scripts.update_feishu import (
        FeishuConfig,
        feishu_request,
        get_field_definitions,
        get_tenant_access_token,
        load_feishu_config,
        upload_bitable_attachment,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a pipeline alert to Feishu.")
    parser.add_argument("--config", default="config/feishu.yaml")
    parser.add_argument("--summary", required=True)
    parser.add_argument("--details", default="")
    parser.add_argument("--log-file", default="")
    return parser.parse_args()


def build_alert_fields(
    config: FeishuConfig,
    field_definitions: dict[str, dict],
    summary: str,
    details: str,
    log_token: str = "",
) -> dict:
    alerts_cfg = load_config("config/feishu.yaml").get("alerts", {})
    company_value = alerts_cfg.get("company_label", "[Pipeline Alert]")
    status_value = alerts_cfg.get("status", "Need Review")
    position_text = f"{datetime.now().strftime('%Y-%m-%d %H:%M')} {summary}"
    if details:
        position_text = f"{position_text} | {details[:120]}"

    fields = {
        config.fields["company"]: company_value,
        config.fields["position"]: position_text,
        config.fields["status"]: status_value,
        config.fields["matching_score"]: 0,
    }
    cover_field = config.fields.get("cover_letter")
    if cover_field and log_token and field_definitions.get(cover_field, {}).get("ui_type") == "Attachment":
        fields[cover_field] = [{"file_token": log_token}]
    return fields


def send_pipeline_alert(config_path: str, summary: str, details: str = "", log_file: str = "") -> None:
    config = load_feishu_config(config_path)
    alerts_cfg = load_config(config_path).get("alerts", {})
    if not alerts_cfg.get("enabled", True):
        return

    token = get_tenant_access_token(config)
    field_definitions = get_field_definitions(config, token)
    log_token = ""
    if log_file and alerts_cfg.get("attach_log_file", True):
        path = Path(log_file)
        if path.exists():
            log_token = upload_bitable_attachment(config, token, str(path))

    fields = build_alert_fields(config, field_definitions, summary, details, log_token)
    feishu_request(
        "POST",
        token,
        f"/open-apis/bitable/v1/apps/{config.app_token}/tables/{config.table_id}/records",
        json_body={"fields": fields},
    )


def main() -> None:
    args = parse_args()
    send_pipeline_alert(args.config, args.summary, args.details, args.log_file)
    print("Sent pipeline alert to Feishu.")


if __name__ == "__main__":
    main()
