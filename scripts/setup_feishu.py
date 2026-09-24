#!/usr/bin/env python3
"""Preview, check, or create the configured Feishu Bitable fields."""

from __future__ import annotations

import argparse
from typing import Any

try:
    from job_schema import STAGE_OPTIONS
    from update_feishu import (
        FeishuConfig,
        feishu_request,
        get_field_definitions,
        get_tenant_access_token,
        load_feishu_config,
    )
except ModuleNotFoundError:
    from scripts.job_schema import STAGE_OPTIONS
    from scripts.update_feishu import (
        FeishuConfig,
        feishu_request,
        get_field_definitions,
        get_tenant_access_token,
        load_feishu_config,
    )


TYPE_TEXT = 1
TYPE_NUMBER = 2
TYPE_SINGLE_SELECT = 3
TYPE_MULTI_SELECT = 4
TYPE_DATE_TIME = 5
TYPE_CHECKBOX = 7
TYPE_URL = 15
TYPE_ATTACHMENT = 17

FROZEN_SCHEMA_VERSION = "2027-v1-55"
FROZEN_FIELD_NAMES = {
    "company": "Company",
    "title": "Title",
    "role_family": "Role Family",
    "location": "Location",
    "source": "Sources",
    "official_url": "Official URL",
    "job_id": "Job ID",
    "canonical_key": "Canonical Key",
    "first_seen": "First Seen",
    "last_verified": "Last Verified",
    "posted_at": "Posted At",
    "deadline": "Deadline",
    "status": "Job Status",
    "freshness": "Freshness",
    "technical_fit": "Technical Fit",
    "career_value": "Career Value",
    "skill_portability": "Skill Portability",
    "strategic_value": "Strategic Value",
    "opportunity_value": "Opportunity Value",
    "score_breakdown": "Score Breakdown",
    "compensation_signal": "Compensation Signal",
    "wlb_signal": "WLB Signal",
    "leave_usability": "Leave Usability",
    "mobility_autonomy": "Mobility Autonomy",
    "commute": "Commute",
    "on_call": "On Call",
    "actual_work_risks": "Actual Work Risks",
    "evidence_confidence": "Evidence Confidence",
    "evidence_count": "Evidence Count",
    "last_lifestyle_check": "Last Lifestyle Check",
    "notes": "Notes",
    "urgency": "Urgency",
    "scarcity": "Scarcity",
    "process_trigger_risk": "Process Trigger Risk",
    "application_cost": "Application Cost",
    "regret": "Regret",
    "release_priority": "Release Priority",
    "action_tier": "Action Tier",
    "action": "Action",
    "reject_reason": "Reject Reason",
    "resume_family": "Resume Family",
    "resume_version": "Resume Version",
    "resume_draft": "Resume Draft",
    "cover_letter": "Cover Letter",
    "applied_at": "Applied At",
    "stage": "Stage",
    "next_action": "Next Action",
    "next_deadline": "Next Deadline",
    "round": "Round",
    "team": "Team",
    "manager_signal": "Manager Signal",
    "questions": "Questions",
    "weak_topics": "Weak Topics",
    "debrief": "Debrief",
    "offer": "Offer",
}

NUMBER_KEYS = {
    "technical_fit",
    "career_value",
    "skill_portability",
    "strategic_value",
    "opportunity_value",
    "evidence_count",
    "urgency",
    "scarcity",
    "process_trigger_risk",
    "application_cost",
    "regret",
    "release_priority",
}
DATE_KEYS = {
    "first_seen",
    "last_verified",
    "posted_at",
    "deadline",
    "last_lifestyle_check",
    "applied_at",
    "next_deadline",
}
SINGLE_SELECT_OPTIONS = {
    "role_family": [
        "test_development",
        "ai_application",
        "robot_software",
        "general_software",
        "backend",
        "fintech_backend",
        "fintech_tech",
        "data_engineering",
        "platform_sre",
        "engineering_tools",
        "software_automation",
        "developer_productivity",
        "reliability",
        "data_platform_sre",
        "algorithm_research",
        "other",
    ],
    "status": ["OPEN", "STALE", "CLOSED", "EXPIRED"],
    "freshness": ["fresh", "stale", "unknown"],
    "evidence_confidence": ["High", "Medium", "Low"],
    "action_tier": ["MUST_APPLY", "CAPACITY_SENSITIVE", "OPPORTUNISTIC", "WATCH", "REJECT"],
    "action": [
        "WATCH",
        "HOLD",
        "READY",
        "MUST_APPLY",
        "APPLIED",
        "PROCESS",
        "OFFER",
        "REJECT",
    ],
    "stage": list(STAGE_OPTIONS),
}
MULTI_SELECT_OPTIONS = {
    "source": [
        "official",
        "nowcoder",
        "ncss",
        "guopin",
        "university_careers",
        "public_web",
        "gpt_web",
        "linkedin",
        "indeed",
        "manual",
        "glassdoor",
        "boss",
        "wechat",
    ],
    "actual_work_risks": ["manual_testing_heavy", "implementation_heavy", "excessive_travel", "on_call"],
}
MULTI_SELECT_KEYS = set(MULTI_SELECT_OPTIONS)
URL_KEYS = {"official_url"}
ATTACHMENT_KEYS = {"resume_draft", "cover_letter"}
CHECKBOX_KEYS = {"offer"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare the Feishu Bitable schema safely.")
    parser.add_argument("--config", default="config/feishu.yaml")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Read and compare the live schema")
    mode.add_argument("--apply", action="store_true", help="Create missing fields")
    return parser.parse_args()


def field_spec(config_key: str, field_name: str) -> dict[str, Any]:
    field_type = TYPE_TEXT
    if config_key in NUMBER_KEYS:
        field_type = TYPE_NUMBER
    elif config_key in DATE_KEYS:
        field_type = TYPE_DATE_TIME
    elif config_key in SINGLE_SELECT_OPTIONS:
        field_type = TYPE_SINGLE_SELECT
    elif config_key in MULTI_SELECT_KEYS:
        field_type = TYPE_MULTI_SELECT
    elif config_key in URL_KEYS:
        field_type = TYPE_URL
    elif config_key in ATTACHMENT_KEYS:
        field_type = TYPE_ATTACHMENT
    elif config_key in CHECKBOX_KEYS:
        field_type = TYPE_CHECKBOX
    payload: dict[str, Any] = {"field_name": field_name, "type": field_type}
    if config_key in SINGLE_SELECT_OPTIONS:
        payload["property"] = {
            "options": [{"name": option} for option in SINGLE_SELECT_OPTIONS[config_key]]
        }
    elif config_key in MULTI_SELECT_OPTIONS:
        payload["property"] = {
            "options": [{"name": option} for option in MULTI_SELECT_OPTIONS[config_key]]
        }
    return payload


def configured_specs(config: FeishuConfig) -> dict[str, dict[str, Any]]:
    del config
    return {
        field_name: field_spec(config_key, field_name)
        for config_key, field_name in FROZEN_FIELD_NAMES.items()
    }


def frozen_mapping_errors(config: FeishuConfig) -> list[str]:
    errors: list[str] = []
    configured = {str(key): str(value) for key, value in config.fields.items() if value}
    for key, expected_name in FROZEN_FIELD_NAMES.items():
        actual_name = configured.get(key)
        if actual_name is None:
            errors.append(f"missing frozen field mapping: {key} -> {expected_name}")
        elif actual_name != expected_name:
            errors.append(
                f"renamed frozen field mapping: {key} expected {expected_name!r}, got {actual_name!r}"
            )
    for key in sorted(set(configured) - set(FROZEN_FIELD_NAMES)):
        errors.append(f"unexpected field mapping outside {FROZEN_SCHEMA_VERSION}: {key}")
    return errors


def compare_schema(
    specs: dict[str, dict[str, Any]],
    definitions: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    missing: list[dict[str, Any]] = []
    mismatches: list[str] = []
    for name in sorted(set(definitions) - set(specs)):
        mismatches.append(f"unexpected field outside frozen contract: {name}")
    for name, spec in specs.items():
        existing = definitions.get(name)
        if not existing:
            missing.append(spec)
            continue
        actual_type = int(existing.get("type", -1))
        if actual_type != spec["type"]:
            mismatches.append(f"{name}: expected type={spec['type']}, actual type={actual_type}")
            continue
        missing_options = missing_select_options(spec, existing)
        if missing_options:
            mismatches.append(f"{name}: missing options={', '.join(missing_options)}")
        unexpected_options = unexpected_select_options(spec, existing)
        if unexpected_options:
            mismatches.append(
                f"{name}: unexpected options={', '.join(unexpected_options)}"
            )
    return missing, mismatches


def missing_select_options(spec: dict[str, Any], existing: dict[str, Any]) -> list[str]:
    expected = [
        str(item.get("name") or "")
        for item in (spec.get("property") or {}).get("options", [])
        if item.get("name")
    ]
    actual = {
        str(item.get("name") or "")
        for item in (existing.get("property") or {}).get("options", [])
        if item.get("name")
    }
    return [name for name in expected if name not in actual]


def unexpected_select_options(spec: dict[str, Any], existing: dict[str, Any]) -> list[str]:
    expected = {
        str(item.get("name") or "")
        for item in (spec.get("property") or {}).get("options", [])
        if item.get("name")
    }
    actual = [
        str(item.get("name") or "")
        for item in (existing.get("property") or {}).get("options", [])
        if item.get("name")
    ]
    return [name for name in actual if name not in expected]


def create_field(config: FeishuConfig, token: str, spec: dict[str, Any]) -> None:
    feishu_request(
        "POST",
        token,
        f"/open-apis/bitable/v1/apps/{config.app_token}/tables/{config.table_id}/fields",
        json_body=spec,
    )


def update_field_options(
    config: FeishuConfig,
    token: str,
    existing: dict[str, Any],
    spec: dict[str, Any],
) -> None:
    options = [
        {"name": str(item.get("name"))}
        for item in (existing.get("property") or {}).get("options", [])
        if item.get("name")
    ]
    known = {item["name"] for item in options}
    for item in spec.get("property", {}).get("options", []):
        name = str(item.get("name") or "")
        if name and name not in known:
            known.add(name)
            options.append({"name": name})
    feishu_request(
        "PUT",
        token,
        (
            f"/open-apis/bitable/v1/apps/{config.app_token}/tables/{config.table_id}"
            f"/fields/{existing['field_id']}"
        ),
        json_body={
            "field_name": spec["field_name"],
            "type": spec["type"],
            "property": {"options": options},
        },
    )


def main() -> None:
    args = parse_args()
    config = load_feishu_config(args.config)
    mapping_errors = frozen_mapping_errors(config)
    if mapping_errors:
        raise ValueError(
            f"Feishu frozen schema {FROZEN_SCHEMA_VERSION} configuration mismatch: "
            + "; ".join(mapping_errors)
        )
    specs = configured_specs(config)
    if not args.check and not args.apply:
        print("Feishu schema preview (no network, no writes):")
        for name, spec in specs.items():
            print(f"- {name}: type={spec['type']}")
        print("Use --check for a read-only comparison or --apply to create missing fields.")
        return

    token = get_tenant_access_token(config)
    definitions = get_field_definitions(config, token)
    missing, mismatches = compare_schema(specs, definitions)
    print(f"Live schema: existing={len(definitions)}, missing={len(missing)}, mismatched={len(mismatches)}")
    for mismatch in mismatches:
        print(f"Schema mismatch: {mismatch}")
    fatal_mismatches = [
        mismatch for mismatch in mismatches if ": missing options=" not in mismatch
    ]
    if args.apply and fatal_mismatches:
        raise ValueError(
            "Feishu schema contains non-repairable frozen-contract mismatches; "
            "no fields or options were written: " + "; ".join(fatal_mismatches)
        )
    for spec in missing:
        if args.apply:
            create_field(config, token, spec)
            print(f"Created field: {spec['field_name']}")
        else:
            print(f"Missing field: {spec['field_name']} (type={spec['type']})")
    for name, spec in specs.items():
        existing = definitions.get(name)
        if not existing or int(existing.get("type", -1)) != spec["type"]:
            continue
        option_names = missing_select_options(spec, existing)
        if not option_names:
            continue
        if args.apply:
            update_field_options(config, token, existing, spec)
            print(f"Added options to {name}: {', '.join(option_names)}")
        else:
            print(f"Missing options in {name}: {', '.join(option_names)}")
    if args.check:
        print("Read-only check complete. Re-run with --apply to create missing fields.")


if __name__ == "__main__":
    main()
