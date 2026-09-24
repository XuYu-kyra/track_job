#!/usr/bin/env python3
"""Discovery provider registry and adapters for the canonical job schema."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from ats_detection import canonical_source_name, detect_ats_family
    from common import load_config, normalize_token
    from job_schema import (
        OBSERVATION_ORIGIN_CACHE_REPLAY,
        OBSERVATION_ORIGIN_MANUAL_IMPORT,
        has_trusted_canonical_authority,
        normalize_job,
        normalize_observation_origin,
    )
except ModuleNotFoundError:
    from scripts.ats_detection import canonical_source_name, detect_ats_family
    from scripts.common import load_config, normalize_token
    from scripts.job_schema import (
        OBSERVATION_ORIGIN_CACHE_REPLAY,
        OBSERVATION_ORIGIN_MANUAL_IMPORT,
        has_trusted_canonical_authority,
        normalize_job,
        normalize_observation_origin,
    )


AUTOMATION_MODES = frozenset({"AUTO", "PUBLIC_SEARCH", "MANUAL"})
DISCOVERY_PRIORITIES = frozenset({"HIGH", "MEDIUM", "LOW"})
CANONICALITY_VALUES = frozenset({"CANONICAL", "SECONDARY", "NON_CANONICAL"})


def _spec(
    *,
    tier: int,
    priority: str,
    automation: str,
    confidence: str,
    canonicality: str,
    family: str,
    status: str,
    campus: bool = False,
    legacy_automation: str = "manual",
) -> dict[str, Any]:
    return {
        "tier": tier,
        "discovery_priority": priority,
        "automation_mode": automation,
        "evidence_confidence": confidence,
        "canonicality": canonicality,
        "source_family": family,
        "implementation_status": status,
        "campus_context": campus,
        # Backward-compatible internal names used by earlier pipeline versions.
        "automation_level": legacy_automation,
        "confidence": confidence.casefold(),
    }


DEFAULT_SOURCE_SPECS: dict[str, dict[str, Any]] = {
    "official": _spec(
        tier=1,
        priority="HIGH",
        automation="AUTO",
        confidence="HIGH",
        canonicality="CANONICAL",
        family="official_monitor",
        status="automated_if_registered",
        legacy_automation="registered_public_page",
    ),
    "ats": _spec(
        tier=1,
        priority="HIGH",
        automation="PUBLIC_SEARCH",
        confidence="HIGH",
        canonicality="CANONICAL",
        family="official_ats",
        status="detection_and_generic_public",
        legacy_automation="public_page",
    ),
    "university_careers": _spec(
        tier=2,
        priority="HIGH",
        automation="PUBLIC_SEARCH",
        confidence="HIGH",
        canonicality="SECONDARY",
        family="university_page",
        status="manual_agent_search",
        campus=True,
        legacy_automation="public_search",
    ),
    "nowcoder": _spec(
        tier=2,
        priority="HIGH",
        automation="PUBLIC_SEARCH",
        confidence="MEDIUM",
        canonicality="SECONDARY",
        family="china_public_jobs",
        status="manual_agent_search",
        campus=True,
        legacy_automation="public_search",
    ),
    "ncss": _spec(
        tier=2,
        priority="HIGH",
        automation="PUBLIC_SEARCH",
        confidence="HIGH",
        canonicality="SECONDARY",
        family="china_public_jobs",
        status="manual_agent_search",
        campus=True,
        legacy_automation="public_search",
    ),
    "guopin": _spec(
        tier=2,
        priority="HIGH",
        automation="PUBLIC_SEARCH",
        confidence="MEDIUM",
        canonicality="SECONDARY",
        family="china_public_jobs",
        status="manual_agent_search",
        campus=True,
        legacy_automation="public_search",
    ),
    "public_web": _spec(
        tier=2,
        priority="HIGH",
        automation="PUBLIC_SEARCH",
        confidence="MEDIUM",
        canonicality="SECONDARY",
        family="general_public_web",
        status="manual_agent_search",
        legacy_automation="public_search",
    ),
    "gpt_web": _spec(
        tier=2,
        priority="HIGH",
        automation="PUBLIC_SEARCH",
        confidence="MEDIUM",
        canonicality="SECONDARY",
        family="gpt_web",
        status="manual_agent",
        legacy_automation="manual_agent",
    ),
    "linkedin": _spec(
        tier=3,
        priority="MEDIUM",
        automation="AUTO",
        confidence="MEDIUM",
        canonicality="SECONDARY",
        family="linkedin",
        status="automated",
        legacy_automation="public_guest",
    ),
    "indeed": _spec(
        tier=3,
        priority="MEDIUM",
        automation="AUTO",
        confidence="MEDIUM",
        canonicality="SECONDARY",
        family="indeed",
        status="automated",
        legacy_automation="public_page",
    ),
    "boss": _spec(
        tier=4,
        priority="HIGH",
        automation="MANUAL",
        confidence="MEDIUM",
        canonicality="SECONDARY",
        family="manual_boss",
        status="manual_no_crawler",
        legacy_automation="human_in_the_loop",
    ),
    "wechat": _spec(
        tier=4,
        priority="HIGH",
        automation="MANUAL",
        confidence="MEDIUM",
        canonicality="SECONDARY",
        family="manual_wechat",
        status="manual_no_crawler",
        campus=True,
        legacy_automation="human_in_the_loop",
    ),
    "manual": _spec(
        tier=4,
        priority="MEDIUM",
        automation="MANUAL",
        confidence="MEDIUM",
        canonicality="SECONDARY",
        family="other_manual",
        status="automated_import",
        legacy_automation="human_in_the_loop",
    ),
    "glassdoor": _spec(
        tier=4,
        priority="LOW",
        automation="MANUAL",
        confidence="LOW",
        canonicality="NON_CANONICAL",
        family="lifestyle_evidence",
        status="manual_evidence_only",
        legacy_automation="manual",
    ),
    "maimai": _spec(
        tier=4,
        priority="LOW",
        automation="MANUAL",
        confidence="LOW",
        canonicality="NON_CANONICAL",
        family="lifestyle_evidence",
        status="manual_evidence_only",
        legacy_automation="manual",
    ),
    "kanzhun": _spec(
        tier=4,
        priority="LOW",
        automation="MANUAL",
        confidence="LOW",
        canonicality="NON_CANONICAL",
        family="lifestyle_evidence",
        status="manual_evidence_only",
        legacy_automation="manual",
    ),
}


PROVENANCE_BY_SOURCE = {
    "official": "official_monitor",
    "ats": "ats",
    "university_careers": "university_page",
    "gpt_web": "gpt_web",
    "boss": "manual_boss",
    "wechat": "manual_wechat",
    "manual": "other_manual",
}


def normalize_source_name(source: str) -> str:
    return str(source or "manual").strip().casefold().replace(" ", "_") or "manual"


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value not in (None, ""):
        return [str(value).strip()]
    return []


def load_source_registry(path: str | Path) -> dict[str, Any]:
    registry_path = Path(path)
    if not registry_path.exists():
        return {"companies": [], "sources": {}}
    registry = load_config(registry_path)
    registry.setdefault("companies", [])
    registry.setdefault("sources", {})
    defaults = registry.get("company_defaults", {})
    if not isinstance(defaults, dict):
        defaults = {}
    expanded: list[dict[str, Any]] = []
    for item in registry.get("companies", []):
        if not isinstance(item, dict):
            continue
        company = dict(defaults)
        company.update(item)
        for list_key in ("aliases", "location_tags", "role_fit_tags"):
            value = company.get(list_key, [])
            company[list_key] = list(value) if isinstance(value, list) else value
        expanded.append(company)
    registry["companies"] = expanded
    return registry


def source_spec(source: str, registry: dict[str, Any] | None = None) -> dict[str, Any]:
    name = normalize_source_name(source)
    spec = dict(DEFAULT_SOURCE_SPECS.get(name, DEFAULT_SOURCE_SPECS["manual"]))
    configured = (registry or {}).get("sources", {}).get(name, {})
    if isinstance(configured, dict):
        spec.update(configured)
    spec.setdefault("source_name", name)
    spec.setdefault("automation_mode", "MANUAL")
    spec.setdefault("discovery_priority", "MEDIUM")
    spec.setdefault("evidence_confidence", str(spec.get("confidence") or "MEDIUM").upper())
    spec.setdefault("canonicality", "SECONDARY")
    spec.setdefault("source_family", PROVENANCE_BY_SOURCE.get(name, name))
    spec.setdefault("terms_access_notes", "")
    return spec


def registry_company_names(registry: dict[str, Any] | None) -> tuple[str, ...]:
    names: list[str] = []
    for company in (registry or {}).get("companies", []):
        if not isinstance(company, dict):
            continue
        names.append(str(company.get("name") or ""))
        names.extend(_as_list(company.get("aliases")))
    return tuple(value for value in names if value)


def company_in_registry(company: str, registry_names: tuple[str, ...]) -> bool:
    token = normalize_token(company)
    return bool(token and token in {normalize_token(name) for name in registry_names})


@dataclass(frozen=True)
class SourceAdapter:
    """Common DiscoveryProvider implementation used by all current sources."""

    name: str
    source_name: str = ""
    tier: int = 4
    automation_level: str = "manual"
    confidence: str = "medium"
    implementation_status: str = "manual"
    authoritative: bool = False
    campus_context: bool = False
    discovery_priority: str = "MEDIUM"
    automation_mode: str = "MANUAL"
    evidence_confidence: str = "MEDIUM"
    canonicality: str = "SECONDARY"
    source_family: str = "other_manual"
    terms_access_notes: str = ""
    registry_names: tuple[str, ...] = ()

    def prepare(self, raw_job: dict[str, Any]) -> dict[str, Any]:
        prepared = dict(raw_job)
        manual_ingest = bool(prepared.get("manual_ingest", False))
        source_url = str(
            prepared.get("source_url") or prepared.get("url") or prepared.get("official_url") or ""
        ).strip()
        canonical_url = str(prepared.get("canonical_url") or prepared.get("official_url") or "").strip()
        source_ats = detect_ats_family(source_url)
        canonical_ats = detect_ats_family(canonical_url)
        ats_family = str(prepared.get("ats_family") or "").strip()
        if not ats_family or ats_family == "UNKNOWN":
            ats_family = canonical_ats if canonical_ats != "UNKNOWN" else source_ats
        if not ats_family:
            ats_family = "UNKNOWN"

        effective_source = "manual" if manual_ingest and self.authoritative else self.name
        effective_authoritative = has_trusted_canonical_authority(prepared) and not manual_ingest
        if effective_authoritative:
            canonical_url = canonical_url or source_url
        else:
            # URL-family detection is classification only. Keep an unverified
            # claimed URL as source provenance, never as canonical authority.
            source_urls = _as_list(prepared.get("source_urls"))
            if canonical_url:
                source_urls.append(canonical_url)
            if source_urls:
                prepared["source_urls"] = list(dict.fromkeys(source_urls))
            canonical_url = ""
            prepared.pop("canonical_url", None)
            prepared.pop("official_url", None)
            prepared.pop("canonical_source", None)
            prepared.pop("canonical_authority", None)
        if canonical_url:
            prepared["canonical_url"] = canonical_url
            prepared["official_url"] = canonical_url
            prepared["canonical_source"] = str(
                prepared.get("canonical_source")
                or canonical_source_name(ats_family, "official")
            )

        manual_spec = DEFAULT_SOURCE_SPECS["manual"] if effective_source == "manual" else {}
        if effective_source != self.name:
            prepared["source_claimed"] = self.name
        prepared["source"] = effective_source
        prepared["source_name"] = str(
            prepared.get("source_name")
            or (f"manual_claim:{self.name}" if effective_source != self.name else "")
            or self.source_name
            or effective_source
        )
        prepared["source_url"] = source_url
        prepared["source_tier"] = int(
            prepared.get("source_tier") or manual_spec.get("tier") or self.tier
        )
        prepared["source_automation"] = str(
            prepared.get("source_automation")
            or manual_spec.get("automation_level")
            or self.automation_level
        )
        prepared["source_confidence"] = str(
            prepared.get("source_confidence") or manual_spec.get("confidence") or self.confidence
        )
        prepared["source_implementation_status"] = str(
            prepared.get("source_implementation_status")
            or manual_spec.get("implementation_status")
            or self.implementation_status
        )
        prepared["source_discovery_priority"] = str(
            prepared.get("source_discovery_priority")
            or manual_spec.get("discovery_priority")
            or self.discovery_priority
        ).upper()
        prepared["source_automation_mode"] = str(
            prepared.get("source_automation_mode")
            or manual_spec.get("automation_mode")
            or self.automation_mode
        ).upper()
        prepared["source_evidence_confidence"] = str(
            prepared.get("source_evidence_confidence")
            or manual_spec.get("evidence_confidence")
            or self.evidence_confidence
        ).upper()
        source_canonicality = str(
            prepared.get("source_canonicality") or self.canonicality
        ).upper()
        if not effective_authoritative and source_canonicality == "CANONICAL":
            source_canonicality = "SECONDARY"
        prepared["source_canonicality"] = source_canonicality
        prepared["source_family"] = str(
            prepared.get("source_family")
            or ("other_manual" if effective_source == "manual" else self.source_family)
        )
        prepared["terms_access_notes"] = str(
            prepared.get("terms_access_notes") or self.terms_access_notes
        )
        if manual_ingest and not effective_authoritative:
            prepared["source_tier"] = int(DEFAULT_SOURCE_SPECS["manual"]["tier"])
            prepared["source_automation"] = str(
                DEFAULT_SOURCE_SPECS["manual"]["automation_level"]
            )
            prepared["source_confidence"] = str(
                DEFAULT_SOURCE_SPECS["manual"]["confidence"]
            )
            prepared["source_implementation_status"] = str(
                DEFAULT_SOURCE_SPECS["manual"]["implementation_status"]
            )
            prepared["source_discovery_priority"] = str(
                DEFAULT_SOURCE_SPECS["manual"]["discovery_priority"]
            )
            prepared["source_automation_mode"] = "MANUAL"
            prepared["source_evidence_confidence"] = "MEDIUM"
            prepared["evidence_confidence"] = "Medium"
        elif not effective_authoritative and self.canonicality == "CANONICAL":
            prepared["source_evidence_confidence"] = "MEDIUM"
            prepared["evidence_confidence"] = "Medium"
        prepared["source_authoritative"] = effective_authoritative
        prepared["ats_family"] = ats_family
        prepared["job_id_namespace"] = (
            f"ats:{normalize_token(ats_family).replace(' ', '_')}"
            if effective_authoritative and ats_family != "UNKNOWN"
            else effective_source
        )
        if manual_ingest:
            prepared["observation_origin"] = OBSERVATION_ORIGIN_MANUAL_IMPORT
        elif effective_source == "gpt_web":
            prepared["observation_origin"] = OBSERVATION_ORIGIN_CACHE_REPLAY
        else:
            prepared["observation_origin"] = normalize_observation_origin(
                prepared.get("observation_origin")
            )
        discovered_by = _as_list(prepared.get("discovered_by"))
        discovered_by.append(PROVENANCE_BY_SOURCE.get(effective_source, effective_source))
        prepared["discovered_by"] = list(dict.fromkeys(discovered_by))
        discovery_sources = _as_list(prepared.get("discovery_sources"))
        discovery_sources.append(prepared["source_name"])
        prepared["discovery_sources"] = list(dict.fromkeys(discovery_sources))
        prepared["registry_member"] = company_in_registry(
            str(prepared.get("company") or prepared.get("employer") or ""),
            self.registry_names,
        )
        prepared["registry_candidate"] = bool(prepared.get("registry_candidate", False))
        if self.campus_context and not prepared.get("source_context"):
            prepared["source_context"] = "campus recruitment discovery source"
        return prepared

    def normalize(
        self,
        raw_job: dict[str, Any],
        taxonomies: dict[str, Any],
        *,
        as_of: str,
        previous: dict[str, Any] | None = None,
        stale_after_days: int = 14,
    ) -> dict[str, Any]:
        return normalize_job(
            self.prepare(raw_job),
            taxonomies,
            as_of=as_of,
            previous=previous,
            stale_after_days=stale_after_days,
        )


# Public name for the common conceptual interface; SourceAdapter remains compatible.
DiscoveryProvider = SourceAdapter


def get_source_adapter(
    source: str,
    registry: dict[str, Any] | None = None,
) -> SourceAdapter:
    name = normalize_source_name(source)
    spec = source_spec(name, registry)
    canonicality = str(spec.get("canonicality") or "SECONDARY").upper()
    return SourceAdapter(
        name=name,
        source_name=str(spec.get("source_name") or name),
        tier=int(spec.get("tier", 4)),
        automation_level=str(spec.get("automation_level") or "manual"),
        confidence=str(spec.get("confidence") or "medium"),
        implementation_status=str(spec.get("implementation_status") or "manual"),
        authoritative=canonicality == "CANONICAL",
        campus_context=bool(spec.get("campus_context", False)),
        discovery_priority=str(spec.get("discovery_priority") or "MEDIUM").upper(),
        automation_mode=str(spec.get("automation_mode") or "MANUAL").upper(),
        evidence_confidence=str(spec.get("evidence_confidence") or "MEDIUM").upper(),
        canonicality=canonicality,
        source_family=str(spec.get("source_family") or name),
        terms_access_notes=str(spec.get("terms_access_notes") or ""),
        registry_names=registry_company_names(registry),
    )


def enabled_sources(registry: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    sources = registry.get("sources", {})
    items = [
        (normalize_source_name(name), source_spec(name, registry))
        for name, spec in sources.items()
        if isinstance(spec, dict) and bool(spec.get("enabled", False))
    ]
    priority_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    return sorted(
        items,
        key=lambda item: (
            priority_rank.get(str(item[1].get("discovery_priority") or "LOW").upper(), 9),
            int(item[1].get("tier", 9)),
            item[0],
        ),
    )
