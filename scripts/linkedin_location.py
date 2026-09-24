"""LinkedIn-only job-location scope and post-fetch evidence checks."""

from __future__ import annotations

from typing import Any

try:
    from common import normalize_token
except ModuleNotFoundError:
    from scripts.common import normalize_token


TARGET_REGION_ALIASES: dict[str, tuple[str, ...]] = {
    "Shenzhen": ("shenzhen", "深圳"),
    "Hong Kong": ("hong kong", "hong kong sar", "香港", "香港特别行政区"),
    "United Kingdom": (
        "united kingdom",
        "uk",
        "england",
        "scotland",
        "wales",
        "northern ireland",
    ),
}
REMOTE_TERMS = ("remote", "远程", "work from anywhere", "anywhere")
UK_CITY_ONLY_TERMS = {
    "london", "manchester", "birmingham", "leeds", "liverpool", "bristol",
    "glasgow", "edinburgh", "cardiff", "belfast", "cambridge", "oxford",
}


def linkedin_location_scope(location: Any) -> dict[str, str]:
    """Classify the *returned job location*, not the search request region.

    A bare city/country that can be ambiguous is kept UNCERTAIN.  Explicit
    target evidence wins for multi-location jobs; explicit non-target regions
    are OUT_OF_SCOPE only when no target evidence is present.
    """

    raw = str(location or "").strip()
    normalized = normalize_token(raw)
    if not normalized:
        return {"status": "UNCERTAIN", "region": "", "reason": "location_missing"}

    matches: list[str] = []
    padded = f" {normalized} "
    for region, aliases in TARGET_REGION_ALIASES.items():
        if any(f" {normalize_token(alias)} " in padded for alias in aliases):
            matches.append(region)

    # A global/unspecified remote posting is not evidence of a target location.
    if any(f" {normalize_token(term)} " in padded for term in REMOTE_TERMS):
        if not matches:
            return {"status": "UNCERTAIN", "region": "", "reason": "remote_scope_unspecified"}

    if matches:
        # Deduplicate the region label while preserving deterministic order.
        region = "+".join(dict.fromkeys(matches))
        return {"status": "TARGET", "region": region, "reason": "explicit_target_location"}

    if normalized in UK_CITY_ONLY_TERMS or normalized in {"china", "中国", "greater china"}:
        return {"status": "UNCERTAIN", "region": "", "reason": "location_country_or_city_ambiguous"}

    # Country-only China is not enough to claim Shenzhen; a bare UK city is
    # similarly ambiguous, so only explicit UK country/constituent evidence is
    # accepted above.
    return {"status": "OUT_OF_SCOPE", "region": "", "reason": "explicit_non_target_location"}


def linkedin_query_region(location: Any) -> str:
    """Map a configured query alias to a stable coverage bucket."""

    normalized = normalize_token(str(location or ""))
    for region, aliases in TARGET_REGION_ALIASES.items():
        if any(normalize_token(alias) == normalized for alias in aliases):
            return region
    return str(location or "").strip() or "Unknown"


def is_direct_linkedin_job(job: dict[str, Any]) -> bool:
    """Return true only for records whose evidence is exclusively LinkedIn."""

    sources = job.get("sources")
    if isinstance(sources, list) and sources:
        normalized_sources = {normalize_token(str(item)) for item in sources if item}
    else:
        normalized_sources = {normalize_token(str(job.get("source") or ""))}
    return normalized_sources == {"linkedin"}
