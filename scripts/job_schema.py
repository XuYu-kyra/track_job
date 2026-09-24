#!/usr/bin/env python3
"""Canonical job schema and source-normalization helpers."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

try:
    from application_profiles import recommend_application_profile, select_application_profile
    from common import canonical_job_key, normalize_token, normalize_whitespace
except ModuleNotFoundError:
    from scripts.application_profiles import recommend_application_profile, select_application_profile
    from scripts.common import canonical_job_key, normalize_token, normalize_whitespace


CANONICAL_FIELDS = (
    "company",
    "company_type",
    "title",
    "role_family",
    "secondary_role_tags",
    "location",
    "source",
    "source_name",
    "sources",
    "source_urls",
    "source_observations",
    "source_tier",
    "source_automation",
    "source_confidence",
    "source_implementation_status",
    "discovery_priority",
    "automation_mode",
    "canonicality",
    "source_family",
    "terms_access_notes",
    "ats_family",
    "discovered_by",
    "discovery_sources",
    "canonical_source",
    "canonical_url",
    "registry_member",
    "registry_candidate",
    "discovery_query",
    "discovered_at",
    "source_type",
    "official_url",
    "job_id",
    "job_id_namespace",
    "canonical_key",
    "description",
    "page_context",
    "campaign_context",
    "source_context",
    "source_evidence",
    "role_evidence",
    "location_evidence",
    "job_language",
    "recruiting_context",
    "recommended_application_profile",
    "application_profile",
    "application_profile_reason",
    "graduation_evidence",
    "graduation_eligibility",
    "security_review",
    "dream_role",
    "first_seen",
    "last_observed",
    "last_verified",
    "observation_origin",
    "observed_at",
    "verification_level",
    "verified_at",
    "canonical_authority",
    "verification_state",
    "posted_at",
    "deadline",
    "status",
    "freshness",
    "technical_fit",
    "career_value",
    "skill_portability",
    "strategic_value",
    "opportunity_value",
    "compensation_signal",
    "wlb_signal",
    "leave_usability",
    "mobility_autonomy",
    "commute",
    "on_call",
    "actual_work_risks",
    "evidence_confidence",
    "evidence_count",
    "last_lifestyle_check",
    "notes",
    "urgency",
    "scarcity",
    "process_trigger_risk",
    "application_cost",
    "regret",
    "release_priority",
    "action_tier",
    "action",
    "reject_reason",
    "resume_family",
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
)


STAGE_OPTIONS = (
    "WATCH",
    "HOLD",
    "READY",
    "MUST_APPLY",
    "APPLIED",
    "OA",
    "AI_INTERVIEW",
    "INTERVIEW",
    "TECHNICAL_INTERVIEW",
    "HR_INTERVIEW",
    "OFFER",
    "REJECTED",
    "WITHDRAWN",
)


# This is the single source of truth for lifecycle stage -> queue action. PROCESS
# is retained for historical caches even though it is not a configured Stage.
STAGE_ACTIONS = {
    "APPLIED": "APPLIED",
    "OA": "PROCESS",
    "AI_INTERVIEW": "PROCESS",
    "INTERVIEW": "PROCESS",
    "TECHNICAL_INTERVIEW": "PROCESS",
    "HR_INTERVIEW": "PROCESS",
    "PROCESS": "PROCESS",
    "OFFER": "OFFER",
    "REJECTED": "REJECT",
    "WITHDRAWN": "REJECT",
}

ACTIVE_PROCESS_STAGES = frozenset(
    stage
    for stage, action in STAGE_ACTIONS.items()
    if action in {"APPLIED", "PROCESS", "OFFER"}
)

OBSERVATION_ORIGIN_LIVE_FETCH = "LIVE_FETCH"
OBSERVATION_ORIGIN_CACHE_REPLAY = "CACHE_REPLAY"
OBSERVATION_ORIGIN_MANUAL_IMPORT = "MANUAL_IMPORT"
OBSERVATION_ORIGIN_OFFICIAL_VERIFICATION = "OFFICIAL_VERIFICATION"
OBSERVATION_ORIGINS = frozenset(
    {
        OBSERVATION_ORIGIN_LIVE_FETCH,
        OBSERVATION_ORIGIN_CACHE_REPLAY,
        OBSERVATION_ORIGIN_MANUAL_IMPORT,
        OBSERVATION_ORIGIN_OFFICIAL_VERIFICATION,
    }
)
TRUSTED_CANONICAL_AUTHORITIES = frozenset(
    {"OFFICIAL_MONITOR", "EXPLICIT_VERIFIER", "TRUSTED_OFFICIAL_IMPORT"}
)
VERIFICATION_STATES = frozenset(
    {"DISCOVERED_CANDIDATE", "VERIFIED_OPEN_JOB", "CLOSED", "UNCERTAIN"}
)
MIN_FULL_JD_CHARS = 120


def verification_state_for(
    *,
    status: Any,
    canonical_verified: bool,
    freshness: Any,
    description: Any,
) -> str:
    """Derive a fail-closed discovery/verification state from source facts."""

    normalized_status = str(status or "").upper()
    if normalized_status in {"CLOSED", "EXPIRED"}:
        return "CLOSED"
    full_jd = len(normalize_whitespace(str(description or ""))) >= MIN_FULL_JD_CHARS
    if (
        canonical_verified
        and normalized_status == "OPEN"
        and str(freshness or "").casefold() == "fresh"
        and full_jd
    ):
        return "VERIFIED_OPEN_JOB"
    if canonical_verified:
        return "UNCERTAIN"
    return "DISCOVERED_CANDIDATE"


def is_verified_open_job(job: dict[str, Any]) -> bool:
    """Return whether a record is safe to advance to READY/MUST_APPLY.

    Do not trust ``verification_state`` by itself.  Older caches can omit the
    field and hand-edited/imported records can contain internally inconsistent
    values.  Recheck all facts that make the state meaningful so every caller
    fails closed even when normalization was skipped.
    """

    canonical_url = canonicalize_url(
        job.get("canonical_url") or job.get("official_url")
    )
    return bool(
        str(job.get("verification_state") or "").upper()
        == "VERIFIED_OPEN_JOB"
        and str(job.get("canonicality") or "").upper() == "CANONICAL"
        and canonical_url
        and str(job.get("status") or "").upper() == "OPEN"
        and str(job.get("freshness") or "").casefold() == "fresh"
        and len(normalize_whitespace(str(job.get("description") or "")))
        >= MIN_FULL_JD_CHARS
    )


def is_active_process_stage(value: Any) -> bool:
    return str(value or "").strip().upper() in ACTIVE_PROCESS_STAGES


def action_for_stage(value: Any) -> str:
    """Return the lifecycle-owned action, or empty for discovery-queue stages."""

    return STAGE_ACTIONS.get(str(value or "").strip().upper(), "")


def normalize_observation_origin(value: Any) -> str:
    """Normalize explicit observation provenance, including the legacy label."""

    origin = str(value or "").strip().upper()
    if origin == "LIVE_EXTERNAL":
        return OBSERVATION_ORIGIN_LIVE_FETCH
    return origin if origin in OBSERVATION_ORIGINS else OBSERVATION_ORIGIN_CACHE_REPLAY


def has_trusted_canonical_authority(job: dict[str, Any]) -> bool:
    return (
        str(job.get("canonical_authority") or "").strip().upper()
        in TRUSTED_CANONICAL_AUTHORITIES
    )


def canonicalize_url(value: Any) -> str:
    """Normalize a verified canonical URL without discarding identity queries."""

    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return ""
    tracking_keys = {
        "fbclid",
        "gclid",
        "ref",
        "refid",
        "source",
        "trackingid",
    }
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in tracking_keys
    ]
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            path,
            urlencode(sorted(query)),
            "",
        )
    )


def _valid_extracted_job_id(value: Any, *, minimum_numeric_length: int = 5) -> str:
    candidate = normalize_whitespace(str(value or "")).strip("/ ")
    if not candidate or re.fullmatch(r"(?:20)?2[4-9]", candidate):
        return ""
    if candidate.isdigit() and len(candidate) < minimum_numeric_length:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", candidate):
        return ""
    return candidate


def extract_stable_job_id(value: Any, source: Any = "") -> str:
    """Extract only source-specific, structurally reliable requisition IDs."""

    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
    except ValueError:
        return ""
    host = (parsed.hostname or "").casefold()
    path = unquote(parsed.path or "")
    source_token = normalize_token(str(source or ""))
    query = {key.casefold(): item for key, item in parse_qsl(parsed.query)}

    if "linkedin.com" in host or source_token == "linkedin":
        match = re.search(r"/jobs/view/(?:[^/?#]*-)?(?P<id>\d{6,})(?:[/?#]|$)", path)
        return _valid_extracted_job_id(match.group("id") if match else "", minimum_numeric_length=6)

    if "amazon.jobs" in host or source_token in {"amazon", "amazon jobs"}:
        match = re.search(r"/jobs/(?P<id>\d{6,})(?:[/?#]|$)", path, flags=re.IGNORECASE)
        return _valid_extracted_job_id(match.group("id") if match else "", minimum_numeric_length=6)

    if "myworkdayjobs.com" in host or "workday" in source_token:
        matches = re.findall(
            r"(?:^|[/_-])(?P<id>(?:JR|REQ|R)[-_]?\d{4,})(?=-\d+(?:/|$)|(?:/|$))",
            path,
            flags=re.IGNORECASE,
        )
        return _valid_extracted_job_id(matches[-1] if matches else "")

    if "indeed." in host or source_token == "indeed":
        return _valid_extracted_job_id(query.get("jk", ""))

    if "nowcoder.com" in host or source_token == "nowcoder":
        match = re.search(r"/jobs/detail/(?P<id>\d{5,})(?:[/?#]|$)", path)
        return _valid_extracted_job_id(match.group("id") if match else "")

    if "jobs.apple.com" in host or source_token == "apple jobs":
        match = re.search(r"/details/(?P<id>\d{5,})(?:[/?#]|$)", path, flags=re.IGNORECASE)
        return _valid_extracted_job_id(match.group("id") if match else "")

    if host.endswith(".zhiye.com"):
        return _valid_extracted_job_id(query.get("jobid", ""))

    if "mokahr.com" in host:
        for key in ("jobid", "positionid", "reqid"):
            candidate = _valid_extracted_job_id(query.get(key, ""))
            if candidate:
                return candidate
    return ""


def company_alias_tokens(registry: dict[str, Any] | None) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for company in (registry or {}).get("companies", []):
        if not isinstance(company, dict):
            continue
        canonical = normalize_token(str(company.get("name") or ""))
        if not canonical:
            continue
        aliases[canonical] = canonical
        for alias in _as_list(company.get("aliases")):
            token = normalize_token(alias)
            if token:
                aliases[token] = canonical
    return aliases


def _normalized_identity_title(value: Any) -> str:
    title = normalize_token(str(value or ""))
    year = r"(?:202[4-9]|2[4-9])"
    title = re.sub(rf"(?<!\d){year}\s*(?:年\s*)?届(?:\s*毕业生?)?", " ", title)
    title = re.sub(
        rf"(?<!\d){year}(?:\s+(?!(?:202[4-9]|2[4-9])\b)[a-z0-9+#.-]+){{0,3}}\s+(?:graduates?|graduation|campus|new grads?)\b",
        " ",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(
        rf"\b(?:class of|graduates?|graduation|campus|new grads?)\s+(?<!\d){year}(?!\d)",
        " ",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(
        r"\b(?:campus|graduate|graduates|new grad|new graduate)\b|(?:届|校招|校园招聘|秋招|应届)",
        " ",
        title,
        flags=re.IGNORECASE,
    )
    return normalize_whitespace(title)


def _identity_graduation_years(value: Any) -> tuple[int, ...]:
    text = normalize_token(str(value or ""))
    year = r"(202[4-9]|2[4-9])"
    patterns = (
        rf"(?<!\d){year}\s*(?:年\s*)?届",
        rf"(?<!\d){year}(?:\s+(?!(?:202[4-9]|2[4-9])\b)[a-z0-9+#.-]+){{0,3}}\s+(?:graduates?|graduation|campus|new grads?)\b",
        rf"\b(?:class of|graduates?|graduation|campus|new grads?)\s+{year}(?!\d)",
    )
    years: set[int] = set()
    for pattern in patterns:
        for match in re.findall(pattern, text, flags=re.IGNORECASE):
            raw = match if isinstance(match, str) else match[0]
            parsed = int(raw)
            years.add(parsed if parsed >= 2000 else 2000 + parsed)
    return tuple(sorted(years))


def _normalized_identity_location(value: Any) -> str:
    location = normalize_token(str(value or ""))
    equivalents = {
        "深圳": "shenzhen",
        "shenzhen china": "shenzhen",
        "beijing china": "beijing",
        "北京": "beijing",
        "上海": "shanghai",
        "shanghai china": "shanghai",
        "广州": "guangzhou",
        "guangzhou china": "guangzhou",
        "香港": "hong kong",
    }
    return equivalents.get(location, location or "unknown")


def stable_job_id_namespace(job: dict[str, Any]) -> str:
    """Return a system-owned source namespace for a stable requisition ID."""

    explicit = normalize_token(str(job.get("job_id_namespace") or ""))
    if explicit:
        return explicit.replace(" ", "_")
    ats_family = normalize_token(str(job.get("ats_family") or ""))
    if (
        ats_family
        and ats_family != "unknown"
        and (
            has_trusted_canonical_authority(job)
            or str(job.get("canonicality") or "").upper() == "CANONICAL"
        )
    ):
        return f"ats:{ats_family.replace(' ', '_')}"
    source_namespace = normalize_token(
        str(job.get("source_name") or job.get("source") or job.get("source_family") or "")
    )
    return source_namespace.replace(" ", "_")


def identity_url_candidates(job: dict[str, Any]) -> frozenset[str]:
    """Return normalized URLs that can attach to a separately verified URL."""

    values: list[Any] = [
        job.get("canonical_url"),
        job.get("official_url"),
        job.get("source_url"),
        job.get("url"),
    ]
    source_urls = job.get("source_urls") or []
    values.extend(source_urls if isinstance(source_urls, list) else [source_urls])
    for observation in job.get("source_observations") or []:
        if isinstance(observation, dict):
            values.append(observation.get("url"))
    return frozenset(
        normalized
        for value in values
        if (normalized := canonicalize_url(value))
    )


def dedup_identity_keys(
    job: dict[str, Any],
    alias_tokens: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return URL, stable-ID, and fallback identities in priority order."""

    canonicality = str(job.get("canonicality") or "").upper()
    observations = job.get("source_observations") or []
    has_canonical_observation = any(
        isinstance(item, dict)
        and str(item.get("canonicality") or "").upper() == "CANONICAL"
        for item in observations
    )
    verified_url = ""
    if canonicality == "CANONICAL" or has_canonical_observation:
        verified_url = canonicalize_url(job.get("canonical_url") or job.get("official_url"))

    company = normalize_token(str(job.get("company") or ""))
    company = (alias_tokens or {}).get(company, company)
    stable_id = normalize_token(str(job.get("job_id") or ""))
    stable_namespace = stable_job_id_namespace(job)
    cohort = ",".join(
        str(year)
        for year in _identity_graduation_years(job.get("title") or job.get("position"))
    ) or "unknown"
    fallback = "|".join(
        (
            company,
            _normalized_identity_title(job.get("title") or job.get("position")),
            _normalized_identity_location(job.get("location")),
            cohort,
        )
    )
    return {
        "canonical": f"url:{verified_url}" if verified_url else "",
        "stable": (
            f"job_id:{stable_namespace}:{company}:{stable_id}"
            if company and stable_namespace and stable_id
            else ""
        ),
        "fallback": f"fallback:{fallback}" if normalize_token(fallback.replace("|", "")) else "",
    }


def iso_date(value: Any, default: str = "") -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = normalize_whitespace(str(value or ""))
    if not text:
        return default
    candidate = text[:10]
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError:
        return default


def freshness_status(last_verified: str, as_of: str, stale_after_days: int = 14) -> str:
    try:
        age = (date.fromisoformat(as_of) - date.fromisoformat(last_verified)).days
    except ValueError:
        return "unknown"
    return "stale" if age > stale_after_days else "fresh"


def backfill_discoverable(
    job: dict[str, Any],
    as_of: str,
    *,
    max_age_days: int = 60,
) -> bool:
    """Return whether an open job belongs in a bounded initial backfill."""

    status = str(job.get("status") or "OPEN").upper()
    if status in {"CLOSED", "EXPIRED"}:
        return False
    try:
        today = date.fromisoformat(as_of)
    except ValueError:
        return False
    deadline = iso_date(job.get("deadline"))
    if deadline:
        try:
            if date.fromisoformat(deadline) >= today:
                return True
            return False
        except ValueError:
            pass
    posted_at = iso_date(job.get("posted_at") or job.get("date"))
    if posted_at:
        age = (today - date.fromisoformat(posted_at)).days
        return 0 <= age <= max_age_days
    last_verified = iso_date(job.get("last_verified"))
    if last_verified:
        age = (today - date.fromisoformat(last_verified)).days
        return 0 <= age <= max_age_days
    return True


def infer_role_classification(
    title: str,
    description: str,
    taxonomies: dict[str, Any],
) -> tuple[str, list[str]]:
    title_token = normalize_token(title)
    body_token = normalize_token(f"{title} {description}")
    secondary_tags: list[str] = []

    testing_markers = (
        "test development",
        "software test",
        "automation test",
        "qa automation",
        "sdet",
        "测试开发",
        "软件测试",
        "自动化测试",
        "测试培训生",
        "测试工程师",
    )
    robotics_markers = ("robot", "robotics", "机器人", "具身智能")
    ai_application_markers = (
        "ai agent",
        "agent工程师",
        "agent开发",
        "llm application",
        "大模型应用",
        "智能体开发",
    )
    software_markers = (
        "software",
        "systems engineer",
        "system engineer",
        "软件",
        "系统工程师",
        "系统软件",
        "嵌入式",
        "c++",
    )

    is_robotics = any(marker in title_token for marker in robotics_markers)
    if any(marker in title_token for marker in testing_markers):
        if is_robotics:
            secondary_tags.append("robotics")
        return "test_development", secondary_tags
    if any(marker in title_token for marker in ai_application_markers):
        if is_robotics:
            secondary_tags.append("robotics")
        return "ai_application", secondary_tags
    if is_robotics and any(marker in title_token for marker in software_markers):
        if "具身智能" in title_token:
            secondary_tags.append("embodied_ai")
        if any(marker in title_token for marker in ("系统", "system")):
            secondary_tags.append("systems")
        return "robot_software", secondary_tags

    best_family = "other"
    best_score = 0
    for family, spec in taxonomies.get("role_families", {}).items():
        aliases = [normalize_token(str(item)) for item in spec.get("aliases", [])]
        keywords = [normalize_token(str(item)) for item in spec.get("keywords", [])]
        score = sum(8 for alias in aliases if alias and alias in title_token)
        score += sum(2 for keyword in keywords if keyword and keyword in body_token)
        if score > best_score:
            best_family = family
            best_score = score
    if is_robotics and best_family != "robot_software":
        secondary_tags.append("robotics")
    return best_family, secondary_tags


def infer_role_family(title: str, description: str, taxonomies: dict[str, Any]) -> str:
    return infer_role_classification(title, description, taxonomies)[0]


def normalize_job_status(raw_job: dict[str, Any], discovery_source: str) -> str:
    # A user-reported Feishu application may intentionally have no public URL
    # or open/closed evidence (for example a BOSS screenshot).  Keep status
    # blank instead of defaulting it to OPEN; the inbox records the application
    # fact, not an employer-side vacancy assertion.
    if str(raw_job.get("source_type") or "").casefold() == "feishu_application_inbox":
        return ""
    raw_status = normalize_whitespace(
        str(
            raw_job.get("status")
            or raw_job.get("job_status")
            or raw_job.get("listing_status")
            or ""
        )
    )
    normalized = raw_status.upper()
    if normalized in {"CLOSED", "EXPIRED", "已结束", "已关闭", "已过期", "招聘结束"}:
        return "CLOSED"

    factual_text = normalize_whitespace(
        " ".join(
            str(raw_job.get(field) or "")
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
    )
    closed_patterns = (
        r"(?:职位|岗位|招聘|申请|投递)(?:状态)?\s*[:：-]?\s*(?:已结束|已关闭|已过期)",
        r"(?:^|[\s|·【\[])\s*(?:已结束|已关闭|已过期|招聘结束)\s*(?:$|[\s|·】\]])",
        r"\b(?:this\s+)?(?:job|position|application|recruitment)\s+(?:is\s+|has\s+)?(?:closed|expired|ended)\b",
        r"\bstatus\s*[:=-]?\s*(?:closed|expired|ended)\b",
        r"(?:^|[\s|·\[(:-])(?:closed|expired)(?:$|[\s|·\]):.-])",
    )
    if any(re.search(pattern, factual_text, flags=re.IGNORECASE) for pattern in closed_patterns):
        return "CLOSED"
    if normalized in {"OPEN", "ACTIVE", "招聘中", "申请中", "投递中"}:
        return "OPEN"
    if raw_status:
        return normalized
    return "" if discovery_source == "gpt_web" else "OPEN"


def resume_family_for(role_family: str, taxonomies: dict[str, Any]) -> str:
    spec = taxonomies.get("role_families", {}).get(role_family, {})
    return str(spec.get("resume_family") or "software_engineer")


def _source_urls(job: dict[str, Any], official_url: str) -> list[str]:
    values: list[str] = []
    raw = job.get("source_urls", [])
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, list):
        values.extend(str(item).strip() for item in raw if str(item).strip())
    for value in (job.get("source_url"), job.get("url"), job.get("canonical_url"), official_url):
        if value and str(value).strip():
            values.append(str(value).strip())
    return list(dict.fromkeys(values))


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value not in (None, ""):
        return [str(value).strip()]
    return []


def _observation_sort_key(observation: dict[str, Any]) -> tuple[int, int, str]:
    canonical_rank = 0 if str(observation.get("canonicality") or "").upper() == "CANONICAL" else 1
    return (
        canonical_rank,
        int(observation.get("tier") or 9),
        str(observation.get("source") or ""),
    )


def _build_source_observations(job: dict[str, Any], source: str, source_url: str) -> list[dict[str, Any]]:
    trusted_canonical = has_trusted_canonical_authority(job)
    canonical_verification = (
        trusted_canonical
        and str(job.get("verification_level") or "").upper() == "CANONICAL"
    )
    source_canonicality = str(job.get("source_canonicality") or "SECONDARY").upper()
    if source_canonicality == "CANONICAL" and not trusted_canonical:
        source_canonicality = "SECONDARY"
    observation_origin = normalize_observation_origin(job.get("observation_origin"))
    observation = {
        "source": source,
        "source_name": str(job.get("source_name") or source),
        "url": source_url,
        "job_id": normalize_whitespace(str(job.get("job_id") or job.get("id") or "")),
        "job_id_namespace": stable_job_id_namespace(job),
        "tier": int(job.get("source_tier") or 4),
        "discovery_priority": str(job.get("source_discovery_priority") or "MEDIUM").upper(),
        "automation_mode": str(job.get("source_automation_mode") or "MANUAL").upper(),
        "evidence_confidence": str(job.get("source_evidence_confidence") or "MEDIUM").upper(),
        "canonicality": source_canonicality,
        "source_family": str(job.get("source_family") or source),
        "terms_access_notes": str(job.get("terms_access_notes") or ""),
        "automation_level": str(job.get("source_automation") or "manual"),
        "confidence": str(job.get("source_confidence") or "medium"),
        "implementation_status": str(job.get("source_implementation_status") or "manual"),
        "authoritative": trusted_canonical,
        "observation_origin": observation_origin,
        "observed_at": iso_date(job.get("observed_at")),
        "verification_level": "CANONICAL" if canonical_verification else "",
        "verified_at": iso_date(job.get("verified_at")) if canonical_verification else "",
    }
    observations = [observation]
    canonical_url = str(job.get("canonical_url") or job.get("official_url") or "").strip()
    canonical_source = str(job.get("canonical_source") or "official").strip()
    source_is_canonical = observation["canonicality"] == "CANONICAL"
    if canonical_url and trusted_canonical and not source_is_canonical:
        observations.append(
            {
                "source": "official",
                "source_name": canonical_source,
                "url": canonical_url,
                "tier": 1,
                "discovery_priority": "HIGH",
                "automation_mode": "PUBLIC_SEARCH",
                "evidence_confidence": "HIGH",
                "canonicality": "CANONICAL",
                "source_family": "official_ats",
                "terms_access_notes": "Public canonical verification URL",
                "automation_level": "public_page",
                "confidence": "high",
                "implementation_status": "detected_or_manually_verified",
                "authoritative": True,
                "observation_origin": OBSERVATION_ORIGIN_OFFICIAL_VERIFICATION,
                "observed_at": iso_date(job.get("observed_at")),
                "verification_level": "CANONICAL",
                "verified_at": iso_date(job.get("verified_at") or job.get("last_verified")),
            }
        )
    observations.sort(key=_observation_sort_key)
    return observations


def normalize_job(
    raw_job: dict[str, Any],
    taxonomies: dict[str, Any],
    *,
    as_of: str | None = None,
    previous: dict[str, Any] | None = None,
    stale_after_days: int = 14,
) -> dict[str, Any]:
    """Convert any supported source shape to the canonical, flat job schema."""

    observed_on = iso_date(as_of, date.today().isoformat())
    company = normalize_whitespace(str(raw_job.get("company") or raw_job.get("employer") or ""))
    title = normalize_whitespace(str(raw_job.get("title") or raw_job.get("position") or raw_job.get("job_title") or ""))
    location = normalize_whitespace(str(raw_job.get("location") or raw_job.get("city") or ""))
    description = normalize_whitespace(str(raw_job.get("description") or raw_job.get("jd") or ""))
    source_url = str(
        raw_job.get("source_url") or raw_job.get("url") or raw_job.get("official_url") or ""
    ).strip()
    claimed_canonical_url = str(
        raw_job.get("canonical_url") or raw_job.get("official_url") or ""
    ).strip()
    trusted_canonical = has_trusted_canonical_authority(raw_job)
    canonical_url = claimed_canonical_url if trusted_canonical else ""
    official_url = canonical_url
    if trusted_canonical and raw_job.get("source_authoritative") and not official_url:
        official_url = source_url
        canonical_url = source_url
    discovery_source = normalize_token(str(raw_job.get("source") or "manual")).replace(" ", "_") or "manual"
    supplied_job_id = normalize_whitespace(
        str(raw_job.get("job_id") or raw_job.get("id") or "")
    )
    if re.fullmatch(r"(?:20)?2[4-9]", supplied_job_id):
        supplied_job_id = ""
    job_id = supplied_job_id or extract_stable_job_id(source_url, discovery_source)
    identity_job = dict(raw_job)
    identity_job["job_id"] = job_id
    source_observations = _build_source_observations(identity_job, discovery_source, source_url)
    primary_observation = source_observations[0]
    source = str(primary_observation.get("source") or discovery_source)
    job_id_namespace = stable_job_id_namespace(identity_job)
    role_evidence_text = " ".join(_as_list(raw_job.get("role_evidence")))
    inferred_role, inferred_secondary_tags = infer_role_classification(
        title,
        f"{description} {role_evidence_text}",
        taxonomies,
    )
    role_family = str(raw_job.get("role_family") or inferred_role)
    secondary_role_tags = list(
        dict.fromkeys(
            [*_as_list(raw_job.get("secondary_role_tags")), *inferred_secondary_tags]
        )
    )
    recommended_profile, recommended_reason, inferred_language = recommend_application_profile(raw_job)
    selected_profile, profile_reason, _ = select_application_profile(raw_job)

    key = str(raw_job.get("canonical_key") or canonical_job_key(company, title, location))
    if not normalize_token(key.replace("|", "")):
        identity = source_url or official_url or job_id or f"{discovery_source}:{description}"
        key = f"unknown|{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"

    previous = previous or {}
    first_seen = iso_date(
        previous.get("first_seen") or raw_job.get("first_seen"),
        observed_on,
    )
    observation_origin = normalize_observation_origin(raw_job.get("observation_origin"))
    live_observation = observation_origin in {
        OBSERVATION_ORIGIN_LIVE_FETCH,
        OBSERVATION_ORIGIN_OFFICIAL_VERIFICATION,
    }
    canonical_verification = (
        trusted_canonical
        and str(raw_job.get("verification_level") or "").upper() == "CANONICAL"
    )
    explicit_observed = iso_date(
        raw_job.get("observed_at") or raw_job.get("last_observed")
    )
    explicit_verified = iso_date(
        raw_job.get("verified_at") or raw_job.get("last_verified")
    )
    if canonical_verification:
        last_verified = explicit_verified or explicit_observed or observed_on
    elif previous:
        last_verified = iso_date(previous.get("last_verified"))
    else:
        last_verified = ""
    if live_observation:
        last_observed = explicit_observed or observed_on
    elif previous:
        last_observed = iso_date(previous.get("last_observed"), explicit_observed)
    else:
        last_observed = explicit_observed
    posted_at = iso_date(raw_job.get("posted_at") or raw_job.get("date"))
    deadline = iso_date(raw_job.get("deadline"))

    status = normalize_job_status(raw_job, discovery_source)
    resolved_freshness = freshness_status(
        last_verified or last_observed,
        observed_on,
        stale_after_days,
    )
    has_discovery_provenance = any(
        raw_job.get(field) not in (None, "")
        for field in (
            "source", "source_url", "url", "official_url", "canonical_url",
            "observation_origin", "source_observations",
        )
    )
    verification_state = (
        verification_state_for(
            status=status,
            canonical_verified=canonical_verification,
            freshness=resolved_freshness,
            description=description,
        )
        if has_discovery_provenance
        else ""
    )
    discovered_by = _as_list(raw_job.get("discovered_by")) or [discovery_source]
    discovery_sources = _as_list(raw_job.get("discovery_sources")) or [
        str(raw_job.get("source_name") or discovery_source)
    ]
    canonical_observation = next(
        (
            item
            for item in source_observations
            if str(item.get("canonicality") or "").upper() == "CANONICAL"
        ),
        None,
    )
    canonical_source = str(
        (raw_job.get("canonical_source") if trusted_canonical else "")
        or ((canonical_observation or {}).get("source_name") if canonical_observation else "")
        or ""
    )
    if canonical_observation and not canonical_url:
        canonical_url = str(canonical_observation.get("url") or "")
    source_evidence = str(primary_observation.get("evidence_confidence") or "MEDIUM").title()

    normalized: dict[str, Any] = {field: "" for field in CANONICAL_FIELDS}
    normalized.update(
        {
            "company": company,
            "company_type": normalize_whitespace(str(raw_job.get("company_type") or "")),
            "title": title,
            "position": title,
            "role_family": role_family,
            "secondary_role_tags": secondary_role_tags,
            "location": location,
            "source": source,
            "source_name": str(primary_observation.get("source_name") or source),
            "sources": list(
                dict.fromkeys(
                    str(item.get("source"))
                    for item in source_observations
                    if item.get("source")
                )
            ),
            "source_urls": _source_urls(raw_job, official_url),
            "source_observations": source_observations,
            "source_tier": int(primary_observation.get("tier") or 4),
            "source_automation": str(primary_observation.get("automation_level") or "manual"),
            "source_confidence": str(primary_observation.get("confidence") or "medium"),
            "source_implementation_status": str(
                primary_observation.get("implementation_status") or "manual"
            ),
            "discovery_priority": str(primary_observation.get("discovery_priority") or "MEDIUM"),
            "automation_mode": str(primary_observation.get("automation_mode") or "MANUAL"),
            "canonicality": str(primary_observation.get("canonicality") or "SECONDARY"),
            "source_family": str(primary_observation.get("source_family") or source),
            "terms_access_notes": str(primary_observation.get("terms_access_notes") or ""),
            "ats_family": str(raw_job.get("ats_family") or "UNKNOWN"),
            "discovered_by": list(dict.fromkeys(discovered_by)),
            "discovery_sources": list(dict.fromkeys(discovery_sources)),
            "canonical_source": canonical_source,
            "canonical_url": canonical_url,
            "registry_member": bool(raw_job.get("registry_member", False)),
            "registry_candidate": bool(raw_job.get("registry_candidate", False)),
            "discovery_query": str(raw_job.get("discovery_query") or raw_job.get("search_keyword") or ""),
            "discovered_at": iso_date(
                raw_job.get("discovered_at") or previous.get("discovered_at"),
                observed_on,
            ),
            "source_type": str(raw_job.get("source_type") or ""),
            "official_url": official_url,
            "url": source_url,
            "job_id": job_id,
            "job_id_namespace": job_id_namespace,
            "canonical_key": key,
            "description": description,
            "page_context": normalize_whitespace(str(raw_job.get("page_context") or "")),
            "campaign_context": normalize_whitespace(
                str(raw_job.get("campaign_context") or "")
            ),
            "source_context": normalize_whitespace(str(raw_job.get("source_context") or "")),
            "source_evidence": _as_list(raw_job.get("source_evidence")),
            "role_evidence": _as_list(raw_job.get("role_evidence")),
            "location_evidence": _as_list(
                raw_job.get("location_evidence") or raw_job.get("shenzhen_evidence")
            ),
            "job_language": inferred_language,
            "recruiting_context": normalize_whitespace(
                str(raw_job.get("recruiting_context") or "")
            ),
            "recommended_application_profile": recommended_profile,
            "application_profile": selected_profile,
            "application_profile_reason": profile_reason or recommended_reason,
            "first_seen": first_seen,
            "last_observed": last_observed,
            "last_verified": last_verified,
            "observation_origin": observation_origin,
            "observed_at": explicit_observed,
            "verification_level": (
                "CANONICAL" if canonical_verification else ""
            ),
            "verified_at": explicit_verified if canonical_verification else "",
            "canonical_authority": (
                str(raw_job.get("canonical_authority") or "").strip().upper()
                if trusted_canonical
                else ""
            ),
            "verification_state": verification_state,
            "posted_at": posted_at,
            "deadline": deadline,
            "status": status,
            "freshness": resolved_freshness,
            "easy_apply": bool(raw_job.get("easy_apply", False)),
            "dream_role": bool(raw_job.get("dream_role", False)),
            "search_keyword": str(raw_job.get("search_keyword") or ""),
            "resume_family": str(
                raw_job.get("resume_family") or resume_family_for(role_family, taxonomies)
            ),
            "evidence_confidence": (
                "High"
                if canonical_observation
                else str(raw_job.get("evidence_confidence") or source_evidence)
            ),
        }
    )

    passthrough_fields = set(CANONICAL_FIELDS) - {
        "company",
        "company_type",
        "title",
        "role_family",
        "secondary_role_tags",
        "location",
        "source",
        "source_name",
        "sources",
        "source_urls",
        "source_observations",
        "source_tier",
        "source_automation",
        "source_confidence",
        "source_implementation_status",
        "discovery_priority",
        "automation_mode",
        "canonicality",
        "source_family",
        "terms_access_notes",
        "ats_family",
        "discovered_by",
        "discovery_sources",
        "canonical_source",
        "canonical_url",
        "registry_member",
        "registry_candidate",
        "discovery_query",
        "discovered_at",
        "source_type",
        "official_url",
        "job_id",
        "job_id_namespace",
        "canonical_key",
        "description",
        "page_context",
        "campaign_context",
        "source_context",
        "source_evidence",
        "role_evidence",
        "location_evidence",
        "job_language",
        "recruiting_context",
        "recommended_application_profile",
        "application_profile",
        "application_profile_reason",
        "first_seen",
        "last_observed",
        "last_verified",
        "observation_origin",
        "observed_at",
        "verification_level",
        "verified_at",
        "canonical_authority",
        "verification_state",
        "posted_at",
        "deadline",
        "status",
        "freshness",
        "resume_family",
        "evidence_confidence",
    }
    for field in passthrough_fields:
        value = raw_job.get(field)
        if value not in (None, "", [], {}):
            normalized[field] = value

    for field in ("stage", "next_action", "next_deadline", "applied_at", "resume_version"):
        if not normalized.get(field) and previous.get(field):
            normalized[field] = previous[field]
    return normalized


def merge_job_records(current: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Merge duplicate observations while retaining source and lifecycle evidence."""

    preferred = candidate if len(str(candidate.get("description", ""))) > len(str(current.get("description", ""))) else current
    other = current if preferred is candidate else candidate
    merged = dict(preferred)
    for field, value in other.items():
        if merged.get(field) in (None, "", [], {}) and value not in (None, "", [], {}):
            merged[field] = value
    def status_evidence_date(record: dict[str, Any]) -> str:
        return max(
            (
                iso_date(record.get(field))
                for field in ("verified_at", "last_verified", "observed_at", "last_observed")
            ),
            default="",
        )

    def trusted_status_source(record: dict[str, Any]) -> bool:
        observations = record.get("source_observations") or []
        authoritative_observation = any(
            isinstance(item, dict)
            and bool(item.get("authoritative"))
            and str(item.get("canonicality") or "").upper() == "CANONICAL"
            for item in observations
        )
        return bool(
            str(record.get("canonicality") or "").upper() == "CANONICAL"
            and (record.get("canonical_url") or record.get("official_url"))
            and (
                bool(record.get("source_authoritative"))
                or str(record.get("canonical_authority") or "").upper()
                in TRUSTED_CANONICAL_AUTHORITIES
                or str(record.get("source") or "").casefold() == "official"
                or authoritative_observation
            )
        )

    status_records = []
    for record in (current, candidate):
        status = str(record.get("status") or "").upper()
        if status not in {"OPEN", "CLOSED", "EXPIRED"}:
            continue
        status_records.append(
            {
                "status": "CLOSED" if status == "EXPIRED" else status,
                "observed_at": status_evidence_date(record),
                "trusted": trusted_status_source(record),
                "source": record.get("source_name") or record.get("source") or "unknown",
            }
        )
    trusted_open_dates = [
        item["observed_at"] for item in status_records
        if item["trusted"] and item["status"] == "OPEN" and item["observed_at"]
    ]
    trusted_closed_dates = [
        item["observed_at"] for item in status_records
        if item["trusted"] and item["status"] == "CLOSED" and item["observed_at"]
    ]
    latest_open = max(trusted_open_dates or [""])
    latest_closed = max(trusted_closed_dates or [""])
    if latest_open and latest_closed and latest_open == latest_closed:
        merged["status"] = "UNCERTAIN"
        merged["requires_human_review"] = True
        merged["status_transition"] = "HUMAN_REVIEW"
    elif latest_open > latest_closed:
        merged["status"] = "OPEN"
        if any(item["status"] == "CLOSED" for item in status_records):
            merged["status_transition"] = "REOPENED"
    elif latest_closed:
        merged["status"] = "CLOSED"
        if any(item["status"] == "OPEN" for item in status_records):
            merged["status_transition"] = "CLOSED"
    elif any(item["status"] == "CLOSED" for item in status_records):
        # No dated authoritative evidence exists: fail closed. In particular,
        # an undated or third-party OPEN cannot displace a known CLOSED state.
        merged["status"] = "CLOSED"

    history: list[dict[str, Any]] = []
    for record in (current, candidate):
        for item in record.get("status_history") or []:
            if isinstance(item, dict):
                history.append(dict(item))
    history.extend(status_records)
    unique_history: list[dict[str, Any]] = []
    seen_history: set[tuple[str, str, str, bool]] = set()
    for item in history:
        key = (
            str(item.get("status") or ""),
            str(item.get("observed_at") or ""),
            str(item.get("source") or ""),
            bool(item.get("trusted")),
        )
        if key not in seen_history:
            seen_history.add(key)
            unique_history.append(item)
    unique_history.sort(key=lambda item: (str(item.get("observed_at") or ""), str(item.get("status") or "")))
    merged["status_history"] = unique_history

    observations: list[dict[str, Any]] = []
    seen_observations: set[tuple[str, str, str, str, str]] = set()
    for record in (current, candidate):
        raw_observations = record.get("source_observations") or []
        if not isinstance(raw_observations, list):
            raw_observations = []
        if not raw_observations:
            raw_observations = [
                {
                    "source": record.get("source") or "manual",
                    "source_name": record.get("source_name") or record.get("source") or "manual",
                    "url": record.get("url") or "",
                    "job_id": record.get("job_id") or "",
                    "job_id_namespace": record.get("job_id_namespace") or "",
                    "tier": record.get("source_tier") or 4,
                    "discovery_priority": record.get("discovery_priority") or "MEDIUM",
                    "automation_mode": record.get("automation_mode") or "MANUAL",
                    "evidence_confidence": str(record.get("source_confidence") or "medium").upper(),
                    "canonicality": record.get("canonicality") or "SECONDARY",
                    "source_family": record.get("source_family") or record.get("source") or "manual",
                    "automation_level": record.get("source_automation") or "manual",
                    "confidence": record.get("source_confidence") or "mixed",
                    "implementation_status": record.get("source_implementation_status") or "manual",
                    "authoritative": record.get("source") == "official",
                    "observation_origin": normalize_observation_origin(
                        record.get("observation_origin")
                    ),
                    "observed_at": record.get("observed_at") or "",
                    "verification_level": record.get("verification_level") or "",
                    "verified_at": record.get("verified_at") or "",
                }
            ]
        for observation in raw_observations:
            if not isinstance(observation, dict):
                continue
            key = (
                str(observation.get("source") or "manual"),
                str(observation.get("source_name") or observation.get("source") or "manual"),
                str(observation.get("url") or ""),
                str(observation.get("job_id_namespace") or ""),
                str(observation.get("job_id") or ""),
            )
            if key in seen_observations:
                continue
            seen_observations.add(key)
            observations.append(dict(observation))
    observations.sort(key=_observation_sort_key)
    merged["source_observations"] = observations
    merged["sources"] = list(
        dict.fromkeys(str(item.get("source")) for item in observations if item.get("source"))
    )
    primary = observations[0] if observations else {}
    merged["source"] = str(primary.get("source") or "manual")
    merged["source_name"] = str(primary.get("source_name") or merged["source"])
    merged["source_tier"] = int(primary.get("tier") or 4)
    merged["source_automation"] = str(primary.get("automation_level") or "manual")
    merged["source_confidence"] = str(primary.get("confidence") or "mixed")
    merged["source_implementation_status"] = str(
        primary.get("implementation_status") or "manual"
    )
    merged["discovery_priority"] = str(primary.get("discovery_priority") or "MEDIUM")
    merged["automation_mode"] = str(primary.get("automation_mode") or "MANUAL")
    merged["canonicality"] = str(primary.get("canonicality") or "SECONDARY")
    merged["source_family"] = str(primary.get("source_family") or merged["source"])

    for field in ("discovered_by", "discovery_sources"):
        values: list[str] = []
        for record in (current, candidate):
            values.extend(_as_list(record.get(field)))
        merged[field] = list(dict.fromkeys(values))

    for field in (
        "source_evidence",
        "graduation_evidence",
        "location_evidence",
        "role_evidence",
        "secondary_role_tags",
    ):
        values = []
        for record in (current, candidate):
            values.extend(_as_list(record.get(field)))
        merged[field] = list(dict.fromkeys(values))

    urls = []
    for record in (current, candidate):
        values = record.get("source_urls") or [record.get("url")]
        if isinstance(values, str):
            values = [values]
        urls.extend(str(item) for item in values if item)
    merged["source_urls"] = list(dict.fromkeys(urls))
    canonical_observation = next(
        (
            item
            for item in observations
            if str(item.get("canonicality") or "").upper() == "CANONICAL"
        ),
        None,
    )
    if canonical_observation:
        merged["canonical_source"] = str(
            canonical_observation.get("source_name") or canonical_observation.get("source") or "official"
        )
        merged["canonical_url"] = str(canonical_observation.get("url") or "")
        merged["official_url"] = merged["canonical_url"]
        merged["evidence_confidence"] = "High"
    else:
        official_urls = [
            str(record.get("official_url") or "")
            for record in (current, candidate)
            if record.get("official_url")
        ]
        if official_urls:
            merged["official_url"] = official_urls[0]
    primary_url = str(primary.get("url") or "")
    merged["url"] = merged.get("official_url") or primary_url or (
        merged["source_urls"][0] if merged["source_urls"] else ""
    )
    merged["registry_member"] = bool(current.get("registry_member") or candidate.get("registry_member"))
    merged["registry_candidate"] = bool(
        current.get("registry_candidate") or candidate.get("registry_candidate")
    )
    if not merged.get("ats_family") or merged.get("ats_family") == "UNKNOWN":
        ats_values = [
            str(record.get("ats_family") or "")
            for record in (current, candidate)
            if record.get("ats_family") not in (None, "", "UNKNOWN")
        ]
        if ats_values:
            merged["ats_family"] = ats_values[0]

    first_dates = [value for value in (current.get("first_seen"), candidate.get("first_seen")) if value]
    observed_dates = [
        value
        for value in (current.get("last_observed"), candidate.get("last_observed"))
        if value
    ]
    last_dates = [value for value in (current.get("last_verified"), candidate.get("last_verified")) if value]
    if first_dates:
        merged["first_seen"] = min(first_dates)
    if observed_dates:
        merged["last_observed"] = max(observed_dates)
    if last_dates:
        merged["last_verified"] = max(last_dates)
    merged["verification_state"] = verification_state_for(
        status=merged.get("status"),
        canonical_verified=any(
            str(item.get("canonicality") or "").upper() == "CANONICAL"
            and str(item.get("verification_level") or "").upper() == "CANONICAL"
            and bool(item.get("verified_at"))
            for item in observations
        ),
        freshness=merged.get("freshness"),
        description=merged.get("description"),
    )
    return merged
