#!/usr/bin/env python3
"""Validate the search, source, taxonomy, and profile configuration contract."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from application_profiles import PROFILE_FIELDS, PROFILE_NAMES, configured_profiles
    from common import load_config, normalize_token
    from setup_feishu import MULTI_SELECT_OPTIONS, SINGLE_SELECT_OPTIONS
    from resume_bases import load_resume_base_content, load_resume_base_registry
    from source_adapters import (
        AUTOMATION_MODES,
        CANONICALITY_VALUES,
        DISCOVERY_PRIORITIES,
        load_source_registry,
    )
except ModuleNotFoundError:
    from scripts.application_profiles import PROFILE_FIELDS, PROFILE_NAMES, configured_profiles
    from scripts.common import load_config, normalize_token
    from scripts.setup_feishu import MULTI_SELECT_OPTIONS, SINGLE_SELECT_OPTIONS
    from scripts.resume_bases import load_resume_base_content, load_resume_base_registry
    from scripts.source_adapters import (
        AUTOMATION_MODES,
        CANONICALITY_VALUES,
        DISCOVERY_PRIORITIES,
        load_source_registry,
    )


REQUIRED_GRADUATION_TERMS = {
    "2027",
    "27届",
    "2027届",
    "2027 graduate",
    "2027 campus",
    "new grad",
    "new graduate",
    "graduate",
    "应届",
    "应届毕业生",
    "校园招聘",
    "校招",
    "秋招",
    "届毕业生",
}
REGISTRY_PRIORITIES = frozenset({"P0", "P1"})
SOURCE_METADATA_KEYS = {
    "tier",
    "source_name",
    "discovery_priority",
    "automation_mode",
    "evidence_confidence",
    "canonicality",
    "source_family",
    "implementation_status",
    "enabled",
}
SEMANTIC_REVIEW_TRIGGERS = frozenset({"READY", "MUST_APPLY", "USER_SELECTED"})
REQUIRED_PUBLIC_SOURCE_LANES = frozenset(
    {"nowcoder", "ncss", "guopin", "university_careers"}
)
SOURCE_AUDIT_STATES = frozenset(
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


def _semantic_secret_config_paths(value: Any, prefix: str = "semantic_review") -> list[str]:
    paths: list[str] = []
    if not isinstance(value, dict):
        return paths
    for key, child in value.items():
        key_text = str(key)
        normalized = "".join(character for character in key_text.casefold() if character.isalnum())
        path = f"{prefix}.{key_text}"
        if normalized in {"apikey", "deepseekapikey", "authorization", "bearertoken"}:
            paths.append(path)
        paths.extend(_semantic_secret_config_paths(child, path))
    return paths


def validate_configuration(
    targets: dict[str, Any],
    taxonomies: dict[str, Any],
    scoring: dict[str, Any],
    registry: dict[str, Any],
    profile: dict[str, Any] | None = None,
    *,
    require_verified_profile: bool = False,
) -> list[str]:
    errors: list[str] = []
    if targets.get("operating_mode", {}).get("dissertation_active") is not False:
        errors.append("operating_mode.dissertation_active must be false")

    search = targets.get("job_search", {})
    if int(search.get("posted_within_hours", 0)) not in range(48, 73):
        errors.append("daily posted_within_hours must be between 48 and 72")
    modes = search.get("search_modes", {})
    if int(modes.get("backfill", {}).get("posted_within_days", 0)) not in range(45, 61):
        errors.append("backfill posted_within_days must be between 45 and 60")
    if int(modes.get("daily", {}).get("batch_size", 0)) > int(
        targets.get("schedule", {}).get("max_jobs_per_run", 30)
    ):
        errors.append("daily batch_size cannot exceed max_jobs_per_run")
    try:
        official_safety_cap = int(search.get("official_safety_cap", 200))
        if official_safety_cap <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("job_search.official_safety_cap must be a positive integer")

    open_market = targets.get("open_market_discovery", {})
    if open_market.get("registry_is_allowlist") is not False:
        errors.append("open_market_discovery.registry_is_allowlist must be false")
    if open_market.get("unregistered_companies_allowed") is not True:
        errors.append("open_market_discovery.unregistered_companies_allowed must be true")
    if int(open_market.get("p0_interval_days", 0) or 0) != 1:
        errors.append("open_market_discovery.p0_interval_days must be 1")
    if int(open_market.get("p1_cycle_days", 0) or 0) != 3:
        errors.append("open_market_discovery.p1_cycle_days must be 3")
    for key in ("expansion_role_cycle_days", "dynamic_company_cycle_days"):
        try:
            if int(open_market.get(key, 0)) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"open_market_discovery.{key} must be positive")
    try:
        if int(open_market.get("query_plan_limit", 0)) < 64:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("open_market_discovery.query_plan_limit must be at least 64")
    source_domains = open_market.get("public_source_domains", {})
    if not isinstance(source_domains, dict):
        errors.append("open_market_discovery.public_source_domains must be a mapping")
    else:
        missing_lanes = REQUIRED_PUBLIC_SOURCE_LANES - set(source_domains)
        if missing_lanes:
            errors.append(
                "open_market_discovery.public_source_domains missing: "
                + ", ".join(sorted(missing_lanes))
            )
    for key in ("ats_domains", "campaign_terms"):
        values = open_market.get(key)
        if not isinstance(values, list) or not values or any(
            not str(value).strip() for value in values
        ):
            errors.append(f"open_market_discovery.{key} must be a non-empty list")

    graduation_terms = {
        str(item) for item in scoring.get("eligibility", {}).get("graduation_terms", [])
    }
    missing_graduation = sorted(REQUIRED_GRADUATION_TERMS - graduation_terms)
    if missing_graduation:
        errors.append(f"missing graduation terms: {', '.join(missing_graduation)}")

    profile_config = targets.get("candidate_profile", {})
    for repo_path in profile_config.get("repo_paths", []):
        if "://" in str(repo_path):
            errors.append("candidate_profile.repo_paths must contain local paths, not URLs")
    github_profile = str(profile_config.get("github_profile_url") or "")
    if github_profile and not github_profile.startswith("https://github.com/"):
        errors.append("candidate_profile.github_profile_url must use https://github.com/")
    repo_exclusions = profile_config.get("github_repository_exclude_names", [])
    if not isinstance(repo_exclusions, list):
        errors.append("candidate_profile.github_repository_exclude_names must be a list")
    elif any(
        not re.fullmatch(r"[A-Za-z0-9_.-]+", str(name).strip())
        for name in repo_exclusions
    ):
        errors.append(
            "candidate_profile.github_repository_exclude_names contains an invalid name"
        )

    role_families = taxonomies.get("role_families", {})
    configured_roles = list(profile_config.get("preferred_role_families", []))
    configured_roles.extend(profile_config.get("expansion_role_families", []))
    for family in configured_roles:
        if family not in role_families:
            errors.append(f"unknown configured role family: {family}")
        elif not role_families[family].get("aliases"):
            errors.append(f"role family has no aliases: {family}")

    source_specs = registry.get("sources", {})
    for source in search.get("sources", []):
        if source not in source_specs:
            errors.append(f"source is not registered: {source}")
            continue
        missing_keys = SOURCE_METADATA_KEYS - set(source_specs[source])
        if missing_keys:
            errors.append(f"source {source} missing metadata: {', '.join(sorted(missing_keys))}")
    for source, spec in source_specs.items():
        if not isinstance(spec, dict):
            errors.append(f"source {source} metadata must be a mapping")
            continue
        missing_keys = SOURCE_METADATA_KEYS - set(spec)
        if missing_keys:
            errors.append(f"source {source} missing metadata: {', '.join(sorted(missing_keys))}")
        if str(spec.get("automation_mode") or "").upper() not in AUTOMATION_MODES:
            errors.append(f"source {source} has invalid automation_mode")
        if str(spec.get("discovery_priority") or "").upper() not in DISCOVERY_PRIORITIES:
            errors.append(f"source {source} has invalid discovery_priority")
        if str(spec.get("canonicality") or "").upper() not in CANONICALITY_VALUES:
            errors.append(f"source {source} has invalid canonicality")

    if "official" not in search.get("sources", []) or "gpt_web" not in search.get("sources", []):
        errors.append("job_search.sources must include official and gpt_web discovery")
    if search.get("sources", []) == ["linkedin"]:
        errors.append("LinkedIn cannot be the sole discovery source")

    gpt_config = targets.get("gpt_discovery", {})
    gpt_mode = str(gpt_config.get("mode") or "MANUAL_AGENT").upper()
    if gpt_mode not in {"DISABLED", "MANUAL_AGENT", "API"}:
        errors.append("gpt_discovery.mode must be DISABLED, MANUAL_AGENT, or API")
    if bool(gpt_config.get("enabled", False)) and gpt_mode == "API":
        errors.append("gpt_discovery API mode is intentionally unimplemented and cannot be enabled")
    if bool(gpt_config.get("enabled", False)) and gpt_mode == "MANUAL_AGENT":
        if not str(gpt_config.get("input") or "").strip():
            errors.append("gpt_discovery.input is required for MANUAL_AGENT")
        if not str(gpt_config.get("output") or "").strip():
            errors.append("gpt_discovery.output is required for MANUAL_AGENT")
        runner = gpt_config.get("runner", {})
        if not isinstance(runner, dict):
            errors.append("gpt_discovery.runner must be a mapping")
        elif bool(runner.get("enabled", False)):
            if str(runner.get("model") or "").strip() != "gpt-5.6-luna":
                errors.append("gpt_discovery.runner.model must be gpt-5.6-luna")
            if str(runner.get("reasoning_effort") or "").strip() != "low":
                errors.append("gpt_discovery.runner.reasoning_effort must be low")
            for key in ("prompt", "output_schema"):
                value = str(runner.get(key) or "").strip()
                if not value:
                    errors.append(f"gpt_discovery.runner.{key} is required")
                elif not Path(value).is_file():
                    errors.append(f"gpt_discovery.runner.{key} does not exist: {value}")
            for key in ("timeout_seconds", "max_candidates"):
                try:
                    if int(runner.get(key, 0)) <= 0:
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append(f"gpt_discovery.runner.{key} must be positive")

    semantic = targets.get("semantic_review", {})
    secret_paths = _semantic_secret_config_paths(semantic)
    if secret_paths:
        errors.append(
            "semantic_review API keys must only come from DEEPSEEK_API_KEY; remove: "
            + ", ".join(sorted(secret_paths))
        )
    if bool(semantic.get("enabled", False)):
        if str(semantic.get("provider") or "").casefold() != "deepseek":
            errors.append("semantic_review.provider must be deepseek")
        if str(semantic.get("base_url") or "") != "https://api.deepseek.com":
            errors.append("semantic_review.base_url must be https://api.deepseek.com")
        if not str(semantic.get("model") or "").strip():
            errors.append("semantic_review.model is required")
        triggers = semantic.get("trigger")
        if not isinstance(triggers, list) or not triggers:
            errors.append("semantic_review.trigger must be a non-empty list")
        else:
            invalid_triggers = {
                str(item).upper() for item in triggers
            } - SEMANTIC_REVIEW_TRIGGERS
            if invalid_triggers:
                errors.append(
                    "semantic_review has invalid triggers: "
                    + ", ".join(sorted(invalid_triggers))
                )
        positive_limits = (
            "max_input_chars",
            "max_candidate_evidence_items",
            "max_output_tokens",
            "timeout_seconds",
            "max_reviews_per_run",
        )
        for key in positive_limits:
            try:
                if float(semantic.get(key, 0)) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(f"semantic_review.{key} must be positive")
        try:
            if int(semantic.get("max_output_tokens", 0)) > 4096:
                errors.append("semantic_review.max_output_tokens must not exceed 4096")
        except (TypeError, ValueError):
            pass
        try:
            if int(semantic.get("max_retries", -1)) < 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append("semantic_review.max_retries must be zero or greater")
        try:
            if float(semantic.get("retry_backoff_seconds", -1)) < 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append("semantic_review.retry_backoff_seconds must be zero or greater")
        if not str(semantic.get("cache_dir") or "").strip():
            errors.append("semantic_review.cache_dir is required")

    canonical_company_keys: set[str] = set()
    identity_owners: dict[str, str] = {}
    for company in registry.get("companies", []):
        if not isinstance(company, dict):
            errors.append("registry company entries must be mappings")
            continue
        name = str(company.get("name") or "").strip()
        if not name:
            errors.append("registry company missing name")
            continue
        canonical_key = normalize_token(str(company.get("company_key") or name))
        if not canonical_key:
            errors.append(f"registry company {name} has an empty canonical key")
        elif canonical_key in canonical_company_keys:
            errors.append(f"duplicate registry canonical company key: {canonical_key}")
        canonical_company_keys.add(canonical_key)

        aliases = company.get("aliases", [])
        if not isinstance(aliases, list):
            errors.append(f"registry company {name} aliases must be a list")
            aliases = []
        for identity in [name, *(str(alias) for alias in aliases if str(alias).strip())]:
            token = normalize_token(identity)
            owner = identity_owners.get(token)
            if token and owner and owner != canonical_key:
                errors.append(
                    f"registry company/alias conflict: {identity} belongs to both {owner} and {canonical_key}"
                )
            elif token:
                identity_owners[token] = canonical_key
        priority = str(company.get("monitor_priority") or "").upper()
        if priority not in REGISTRY_PRIORITIES:
            errors.append(
                f"registry company {name} has invalid monitor_priority; expected one of "
                + ", ".join(sorted(REGISTRY_PRIORITIES))
            )
        status = str(company.get("current_2027_status") or "UNKNOWN").upper()
        monitor_mode = str(company.get("monitor_mode") or "").upper()
        if monitor_mode not in AUTOMATION_MODES:
            errors.append(f"registry company {name} has invalid monitor_mode")
        if status not in {"UNKNOWN", "OPEN", "CLOSED", "PAUSED"}:
            errors.append(f"registry company {name} has invalid current_2027_status")
        official_url = str(company.get("official_career_url") or "").strip()
        audit_state = str(company.get("source_audit_state") or "").upper()
        if audit_state and audit_state not in SOURCE_AUDIT_STATES:
            errors.append(f"registry company {name} has invalid source_audit_state")
        if audit_state and not str(company.get("source_audit_reason") or "").strip():
            errors.append(
                f"registry company {name} source_audit_state requires source_audit_reason"
            )
        if official_url:
            parsed = urlparse(official_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                errors.append(f"registry company {name} has invalid official_career_url")
        elif status == "OPEN":
            errors.append(f"registry company {name} cannot be OPEN without a verified URL")
    missing_role_options = set(configured_roles) - set(SINGLE_SELECT_OPTIONS["role_family"])
    if missing_role_options:
        errors.append(f"Feishu role options missing: {', '.join(sorted(missing_role_options))}")
    missing_source_options = set(search.get("sources", [])) - set(MULTI_SELECT_OPTIONS["source"])
    if missing_source_options:
        errors.append(f"Feishu source options missing: {', '.join(sorted(missing_source_options))}")

    if profile is not None:
        identity = profile.get("identity", {})
        if identity.get("github_url") != github_profile:
            errors.append("profile.identity.github_url must match candidate github_profile_url")
        raw_profiles = profile.get("profiles", {})
        for name in PROFILE_NAMES:
            if not isinstance(raw_profiles, dict) or not isinstance(raw_profiles.get(name), dict):
                errors.append(f"profile.profiles.{name} is required")
        profiles = configured_profiles(profile)
        default_name = str(profile.get("default_profile") or "").upper()
        if default_name not in PROFILE_NAMES:
            errors.append("profile.default_profile must be CN or INTL")
        elif any(
            str(identity.get(key) or "") != str(profiles[default_name].get(key) or "")
            for key in PROFILE_FIELDS
        ):
            errors.append("legacy profile.identity must match the configured default profile")
        for name, configured in profiles.items():
            if configured.get("github_url") != github_profile:
                errors.append(
                    f"profile.profiles.{name}.github_url must match candidate github_profile_url"
                )
        if require_verified_profile:
            required_identity = ("full_name", "email", "phone", "github_url", "closing_name")
            for name, configured in profiles.items():
                for key in required_identity:
                    if not str(configured.get(key) or "").strip():
                        errors.append(f"verified profile field is missing: {name}.{key}")
            if not str(profiles["INTL"].get("alternate_phone") or "").strip():
                errors.append("verified profile field is missing: INTL.alternate_phone")
            placeholder_markers = ("your name", "you@example.com", "your-profile", "your-portfolio")
            for name, configured in profiles.items():
                for key, value in configured.items():
                    lowered = str(value or "").casefold()
                    if any(marker in lowered for marker in placeholder_markers):
                        errors.append(f"profile field still contains a placeholder: {name}.{key}")
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate local job-search configuration.")
    parser.add_argument("--targets", default="config/targets.yaml")
    parser.add_argument("--taxonomies", default="config/taxonomies.yaml")
    parser.add_argument("--scoring", default="config/scoring.yaml")
    parser.add_argument("--source-registry", default="config/company_registry.yaml")
    parser.add_argument("--profile", default="config/profile.yaml")
    parser.add_argument("--resume-bases", default="config/resume_bases.yaml")
    parser.add_argument("--resume-base-content", default="config/resume_base_content.yaml")
    parser.add_argument("--require-verified-profile", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    profile_path = Path(args.profile)
    errors: list[str] = []
    try:
        load_resume_base_registry(args.resume_bases)
        load_resume_base_content(args.resume_base_content)
    except (FileNotFoundError, ValueError) as exc:
        errors.append(f"resume base configuration: {exc}")
    errors.extend(validate_configuration(
        load_config(args.targets),
        load_config(args.taxonomies),
        load_config(args.scoring),
        load_source_registry(args.source_registry),
        load_config(profile_path) if profile_path.exists() else None,
        require_verified_profile=args.require_verified_profile,
    ))
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Configuration validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
