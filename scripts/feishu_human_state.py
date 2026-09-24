#!/usr/bin/env python3
"""Read human-owned Feishu fields for the next local scoring run.

This is deliberately read-only.  Automatic discovery may refresh factual job
fields, but it must see the current Feishu lifecycle decision before scoring
or writing the next view.  Missing/empty human fields are omitted rather than
converted into defaults.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

try:
    from common import write_json
    from job_schema import canonicalize_url
    from update_feishu import (
        extract_url_value,
        feishu_request,
        get_tenant_access_token,
        load_feishu_config,
    )
except ModuleNotFoundError:
    from scripts.common import write_json
    from scripts.job_schema import canonicalize_url
    from scripts.update_feishu import (
        extract_url_value,
        feishu_request,
        get_tenant_access_token,
        load_feishu_config,
    )


# Applied At/stage/follow-up values are user-owned.  Action and reject_reason
# are included because the inbox's "我不投" operation uses them to distinguish
# a personal decision from an employer rejection.
HUMAN_STATE_KEYS = (
    "applied_at",
    "stage",
    "action",
    "reject_reason",
    "next_action",
    "next_deadline",
    "round",
    "team",
    "manager_signal",
    "questions",
    "weak_topics",
    "debrief",
    "offer",
)


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("text") or value.get("link") or value.get("name") or ""
    if isinstance(value, list):
        # Bitable multi-select/text values may be arrays.  Keep lists because
        # canonical jobs already use list-valued fields for several fields.
        return [_plain(item) for item in value]
    return value


def _field(fields: dict[str, Any], config: Any, key: str) -> Any:
    name = config.fields.get(key)
    return _plain(fields.get(name)) if name else ""


def list_records(config: Any, token: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    page_token = ""
    while True:
        params: dict[str, Any] = {"page_size": 500}
        # Do not scope readback to a user-facing view: APPLIED/WITHDRAWN rows
        # are intentionally absent from some views, but their decisions still
        # must be available to the next scoring run.
        if page_token:
            params["page_token"] = page_token
        payload = feishu_request(
            "GET",
            token,
            f"/open-apis/bitable/v1/apps/{config.app_token}/tables/{config.table_id}/records",
            params=params,
        )
        data = payload.get("data", {})
        records.extend(item for item in data.get("items", []) if isinstance(item, dict))
        if not data.get("has_more"):
            return records
        page_token = str(data.get("page_token") or "")


def build_state(config: Any, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    state: list[dict[str, Any]] = []
    for record in records:
        fields = record.get("fields") or {}
        human_fields = {
            key: _field(fields, config, key)
            for key in HUMAN_STATE_KEYS
            if _field(fields, config, key) not in (None, "", [])
        }
        if not human_fields:
            continue
        official_url = canonicalize_url(
            extract_url_value(_field(fields, config, "official_url"))
        )
        canonical_key = str(_field(fields, config, "canonical_key") or "").strip()
        state.append(
            {
                "record_id": str(record.get("record_id") or ""),
                "official_url": official_url,
                "canonical_key": canonical_key,
                "company": str(_field(fields, config, "company") or "").strip(),
                "title": str(_field(fields, config, "title") or "").strip(),
                "location": str(_field(fields, config, "location") or "").strip(),
                "human_fields": human_fields,
            }
        )
    return state


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read Feishu human lifecycle fields without writing.")
    parser.add_argument("--config", default="config/feishu.yaml")
    parser.add_argument("--output", default="data/job_cache/feishu_human_state.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_feishu_config(args.config)
    token = get_tenant_access_token(config)
    state = build_state(config, list_records(config, token))
    write_json(Path(args.output), state)
    print(f"Read {len(state)} Feishu human-state records into {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
