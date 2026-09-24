#!/usr/bin/env python3
"""Backward-compatible CN/INTL application profile selection."""

from __future__ import annotations

import re
from typing import Any

try:
    from ats_detection import CHINA_ATS_FAMILIES, GLOBAL_ATS_FAMILIES, detect_ats_family
except ModuleNotFoundError:
    from scripts.ats_detection import CHINA_ATS_FAMILIES, GLOBAL_ATS_FAMILIES, detect_ats_family


PROFILE_NAMES = ("CN", "INTL")
PROFILE_FIELDS = (
    "full_name",
    "email",
    "phone",
    "alternate_phone",
    "github_url",
    "linkedin_url",
    "portfolio_url",
    "closing_name",
)


def normalize_profile_name(value: Any) -> str:
    name = str(value or "").strip().upper()
    return name if name in PROFILE_NAMES else ""


def configured_profiles(config: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Load dual profiles while accepting the legacy top-level identity map."""

    legacy = config.get("identity", {})
    if not isinstance(legacy, dict):
        legacy = {}
    raw_profiles = config.get("profiles", {})
    if not isinstance(raw_profiles, dict):
        raw_profiles = {}

    profiles: dict[str, dict[str, str]] = {}
    for name in PROFILE_NAMES:
        raw = raw_profiles.get(name, {})
        if not isinstance(raw, dict):
            raw = {}
        fallback = legacy if not raw_profiles or name == normalize_profile_name(config.get("default_profile")) else {}
        identity: dict[str, str] = {}
        for field in PROFILE_FIELDS:
            value = raw.get(field)
            if value in (None, ""):
                value = fallback.get(field, "")
            identity[field] = str(value or "")
        if not identity["closing_name"]:
            identity["closing_name"] = identity["full_name"]
        profiles[name] = identity
    return profiles


def infer_job_language(job: dict[str, Any]) -> str:
    explicit = str(job.get("job_language") or "").strip().upper()
    if explicit in {"ZH", "CN", "CHINESE", "中文"}:
        return "ZH"
    if explicit in {"EN", "ENGLISH", "英文"}:
        return "EN"
    text = " ".join(
        str(job.get(field) or "")
        for field in (
            "title",
            "position",
            "description",
            "page_context",
            "campaign_context",
            "source_context",
            "recruiting_context",
        )
    )
    chinese_count = len(re.findall(r"[\u3400-\u9fff]", text))
    latin_count = len(re.findall(r"[A-Za-z]", text))
    if chinese_count >= 2 and chinese_count >= latin_count * 0.08:
        return "ZH"
    if latin_count >= 8:
        return "EN"
    return "UNKNOWN"


def recommend_application_profile(job: dict[str, Any]) -> tuple[str, str, str]:
    """Return profile, deterministic reason, and inferred job language."""

    language = infer_job_language(job)
    source = str(job.get("source") or "").casefold()
    context = " ".join(
        str(job.get(field) or "")
        for field in ("recruiting_context", "source_context", "campaign_context")
    ).casefold()
    ats_family = str(job.get("ats_family") or "").strip()
    if not ats_family or ats_family == "UNKNOWN":
        ats_family = detect_ats_family(
            str(job.get("canonical_url") or job.get("official_url") or job.get("source_url") or job.get("url") or "")
        )

    if language == "ZH":
        return "CN", "chinese_job_language", language
    if ats_family in CHINA_ATS_FAMILIES:
        return "CN", f"china_ats:{ats_family}", language
    if source in {"nowcoder", "ncss", "guopin", "university_careers", "boss", "wechat"}:
        return "CN", f"china_recruiting_source:{source}", language
    if any(term in context for term in ("中文申请", "中国校招", "china campus", "校园招聘", "校招")):
        return "CN", "china_recruiting_context", language
    if any(term in context for term in ("international recruiting", "global recruiting", "english application")):
        return "INTL", "international_recruiting_context", language
    if language == "EN":
        return "INTL", "english_job_language", language
    if ats_family in GLOBAL_ATS_FAMILIES:
        return "INTL", f"global_ats:{ats_family}", language
    return "CN", "ambiguous_primary_market_default", language


def select_application_profile(
    job: dict[str, Any],
    manual_override: str = "",
) -> tuple[str, str, str]:
    override = normalize_profile_name(manual_override) or normalize_profile_name(
        job.get("application_profile")
    )
    recommended, reason, language = recommend_application_profile(job)
    if override:
        return override, "manual_override", language
    return recommended, reason, language


def identity_for_job(
    config: dict[str, Any],
    job: dict[str, Any] | None = None,
    *,
    manual_override: str = "",
) -> tuple[str, dict[str, str]]:
    profiles = configured_profiles(config)
    if job is None:
        selected = normalize_profile_name(manual_override) or normalize_profile_name(
            config.get("default_profile")
        ) or "CN"
    else:
        selected, _, _ = select_application_profile(job, manual_override)
    return selected, dict(profiles[selected])


def phone_for_ats(identity: dict[str, str], *, single_phone_field: bool = True) -> str | list[str]:
    primary = str(identity.get("phone") or "").strip()
    if single_phone_field:
        return primary
    alternate = str(identity.get("alternate_phone") or "").strip()
    return [value for value in (primary, alternate) if value]
