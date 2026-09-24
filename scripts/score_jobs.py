#!/usr/bin/env python3
"""Apply the eligibility, opportunity, timing, and application-queue funnel."""

from __future__ import annotations

import argparse
import re
from datetime import date
from pathlib import Path
from typing import Any

try:
    from common import load_config, normalize_token, read_json, write_json
    from job_schema import action_for_stage, backfill_discoverable, is_verified_open_job
    from source_adapters import company_in_registry, load_source_registry, registry_company_names
    from linkedin_location import is_direct_linkedin_job, linkedin_location_scope
except ModuleNotFoundError:
    from scripts.common import load_config, normalize_token, read_json, write_json
    from scripts.job_schema import action_for_stage, backfill_discoverable, is_verified_open_job
    from scripts.source_adapters import company_in_registry, load_source_registry, registry_company_names
    from scripts.linkedin_location import is_direct_linkedin_job, linkedin_location_scope


ACTION_ORDER = {
    "PROCESS": 0,
    "APPLIED": 1,
    "OFFER": 2,
    "MUST_APPLY": 3,
    "READY": 4,
    "HOLD": 5,
    "WATCH": 6,
    "REJECT": 7,
}

NON_FACTUAL_EVIDENCE_PREFIX = re.compile(
    r"^(?:notes?|comments?|warnings?|classifier(?:\s+explanation)?|"
    r"reject[_\s-]?reason|备注|评论|警告|分类器)\s*[:：-]",
    flags=re.IGNORECASE,
)
SOURCE_CLOSED_MARKERS = (
    "职位已关闭",
    "岗位已关闭",
    "职位已下线",
    "岗位已下线",
    "招聘已结束",
    "停止招聘",
    "已停止招聘",
    "no longer accepting applications",
    "job is closed",
    "position is closed",
    "position has been filled",
    "job posting is no longer available",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the complete job decision funnel.")
    parser.add_argument("--config", default="config/targets.yaml")
    parser.add_argument("--scoring-config", default="config/scoring.yaml")
    parser.add_argument("--taxonomy-config", default="config/taxonomies.yaml")
    parser.add_argument("--source-registry", default="config/company_registry.yaml")
    parser.add_argument("--inventory", default="data/job_cache/candidate_inventory.json")
    parser.add_argument("--input", default="data/job_cache/jobs.json")
    parser.add_argument("--output", default="data/job_cache/scored_jobs.json")
    parser.add_argument("--all-output", default="data/job_cache/decisioned_jobs.json")
    parser.add_argument("--top-k", type=int, default=0, help="0 uses config max_jobs_per_run")
    parser.add_argument("--as-of", default="", help="ISO date override for deterministic runs")
    parser.add_argument("--mode", choices=("daily", "backfill"), default="daily")
    return parser.parse_args()


def _factual_evidence_items(value: Any) -> list[str]:
    raw = value if isinstance(value, list) else [value]
    return [
        str(item)
        for item in raw
        if str(item or "").strip()
        and not NON_FACTUAL_EVIDENCE_PREFIX.match(str(item).strip())
    ]


def _evidence_field_text(job: dict[str, Any], field: str) -> str:
    if field in {"graduation_evidence", "location_evidence", "role_evidence"}:
        return " ".join(_factual_evidence_items(job.get(field)))
    return str(job.get(field) or "")


def _body(job: dict[str, Any]) -> str:
    return normalize_token(
        " ".join(
            _evidence_field_text(job, field)
            for field in (
                "title",
                "position",
                "description",
                "location",
                "page_context",
                "campaign_context",
                "source_context",
                "recruiting_context",
                "graduation_evidence",
                "role_evidence",
                "location_evidence",
            )
        )
    )


def source_closure_evidence(job: dict[str, Any]) -> list[str]:
    evidence: list[str] = []
    if str(job.get("status") or "").upper() in {"CLOSED", "EXPIRED"}:
        evidence.append(f"status:{job.get('status')}")
    for field in (
        "notes",
        "page_context",
        "source_context",
        "recruiting_context",
        "source_evidence",
    ):
        value = job.get(field)
        items = value if isinstance(value, list) else [value]
        for item in items:
            text = normalize_token(str(item or ""))
            for marker in SOURCE_CLOSED_MARKERS:
                if normalize_token(marker) in text:
                    evidence.append(f"{field}:{marker}")
    return list(dict.fromkeys(evidence))


def _matches(text: str, phrases: list[Any]) -> list[str]:
    matches: list[str] = []
    for phrase in phrases:
        normalized = normalize_token(str(phrase))
        if not normalized:
            continue
        if re.fullmatch(r"[a-z0-9 ]+", normalized):
            pattern = r"(?<![a-z0-9])" + re.escape(normalized).replace(r"\ ", r"\s+") + r"(?![a-z0-9])"
            found = re.search(pattern, text) is not None
        else:
            found = normalized in text
        if found:
            matches.append(str(phrase))
    return matches


def _candidate_years(text: str) -> set[int]:
    year = r"(202[4-9]|2[4-9])"
    patterns = (
        rf"(?<!\d){year}\s*(?:年\s*)?届",
        rf"(?<!\d){year}\s*(?:年\s*)?(?:毕业生?|应届|校招|秋招|校园招聘)",
        rf"(?:应届|校招|秋招|校园招聘|毕业生?)\s*{year}(?!\d)",
        rf"(?<!\d){year}(?:\s+(?!(?:202[4-9]|2[4-9])\b)[a-z0-9+#.-]+){{0,3}}\s+(?:graduates?|graduation|campus|new grads?)\b",
        rf"\b(?:class of|graduates?|graduation|campus|new grads?)"
        rf"(?:\s+(?!(?:202[4-9]|2[4-9])\b)[a-z0-9+#.-]+){{0,3}}\s+{year}(?!\d)",
    )
    years: set[int] = set()
    for pattern in patterns:
        for value in re.findall(pattern, text, flags=re.IGNORECASE):
            raw = value if isinstance(value, str) else value[0]
            parsed = int(raw)
            years.add(parsed if parsed >= 2000 else 2000 + parsed)
    return years


def graduation_evidence(
    job: dict[str, Any], scoring: dict[str, Any], targets: dict[str, Any]
) -> tuple[str, str, list[str]]:
    cfg = scoring.get("eligibility", {})
    graduation_year = int(targets.get("candidate_profile", {}).get("graduation_year", 2027))
    evidence: list[str] = []
    for field in (
        "title",
        "position",
        "description",
        "page_context",
        "campaign_context",
        "source_context",
        "recruiting_context",
        "graduation_evidence",
    ):
        text = normalize_token(_evidence_field_text(job, field))
        cohort_years = _candidate_years(text)
        for term in _matches(text, cfg.get("graduation_terms", [])):
            if (
                re.fullmatch(r"(?:20)?2[4-9]", normalize_token(term))
                and not cohort_years
            ):
                continue
            evidence.append(f"{field}:{term}")
    evidence = list(dict.fromkeys(evidence))
    evidence_fields = (
        "title",
        "position",
        "description",
        "page_context",
        "campaign_context",
        "source_context",
        "recruiting_context",
        "graduation_evidence",
    )
    positive_fields = 0
    negative_fields = 0
    ambiguous_fields = 0
    explicit_years: set[int] = set()
    for field in evidence_fields:
        text = normalize_token(_evidence_field_text(job, field))
        years = _candidate_years(text)
        if not years:
            continue
        explicit_years.update(years)
        if graduation_year not in years:
            negative_fields += 1
            continue
        other_years = years - {graduation_year}
        explicitly_allowed_set = bool(
            other_years
            and re.search(
                r"均可|都可|皆可|均接受|均面向|both\s+(?:cohorts?\s+)?(?:are\s+)?eligible|either\s+(?:cohort\s+)?(?:is\s+)?eligible",
                text,
                flags=re.IGNORECASE,
            )
        )
        if other_years and not explicitly_allowed_set:
            ambiguous_fields += 1
        else:
            positive_fields += 1
    # A year in the title can be qualified by a nearby campus/new-grad phrase in
    # the same job description. Keep this local to the job card; do not combine
    # independent source/campaign contexts, which could conceal a real conflict.
    if not explicit_years:
        local_job_text = normalize_token(
            f"{job.get('title') or job.get('position') or ''} {job.get('description') or ''}"
        )
        local_years = _candidate_years(local_job_text)
        if local_years:
            explicit_years.update(local_years)
            if graduation_year in local_years:
                if local_years - {graduation_year}:
                    ambiguous_fields += 1
                else:
                    positive_fields += 1
            else:
                negative_fields += 1
    if explicit_years:
        if ambiguous_fields or (positive_fields and negative_fields):
            return "WATCH", "mixed_graduation_year_evidence", evidence
        if positive_fields:
            return "ELIGIBLE", "", evidence
        return "REJECT", "not_new_grad / eligibility", evidence
    factual_text = normalize_token(
        " ".join(_evidence_field_text(job, field) for field in evidence_fields)
    )
    experienced_hits = _matches(factual_text, cfg.get("experienced_hire_terms", []))
    if experienced_hits and not evidence:
        return "REJECT", "experienced_or_social_recruitment", evidence
    # Generic campus/new-grad language is useful discovery evidence, but without
    # an explicit cohort it cannot prove 2027 eligibility.
    return "WATCH", "graduation_year_unknown", evidence


def _algorithm_role_is_primary(job: dict[str, Any], scoring: dict[str, Any]) -> bool:
    title = normalize_token(str(job.get("title") or job.get("position") or ""))
    text = _body(job)
    identity_terms = (
        "algorithm engineer",
        "algorithm research",
        "research scientist",
        "算法工程师",
        "算法研究",
        "研究科学家",
    )
    if any(normalize_token(term) in title for term in identity_terms):
        return True
    responsibility_markers = ("responsible for", "primary responsibility", "负责", "主要职责")
    return bool(
        _matches(text, scoring.get("eligibility", {}).get("non_target_algorithm_terms", []))
        and any(normalize_token(marker) in text for marker in responsibility_markers)
    )


def eligibility_gate(job: dict[str, Any], scoring: dict[str, Any], targets: dict[str, Any]) -> tuple[str, str]:
    text = _body(job)
    title = normalize_token(str(job.get("title") or job.get("position") or ""))
    cfg = scoring.get("eligibility", {})
    if not job.get("company") or not title:
        return "WATCH", "insufficient_identity"
    # LinkedIn has a source-specific geographic preference.  Do not apply it
    # to official/public sources, and do not reject a cross-source canonical
    # job when an authoritative non-LinkedIn observation supplies the target
    # location.
    if is_direct_linkedin_job(job):
        linkedin_scope = linkedin_location_scope(job.get("location"))
        if linkedin_scope["status"] == "OUT_OF_SCOPE":
            return "REJECT", "linkedin_location_out_of_scope"
        if linkedin_scope["status"] == "UNCERTAIN":
            return "WATCH", f"linkedin_location_uncertain:{linkedin_scope['reason']}"
    if source_closure_evidence(job):
        return "WATCH", "source_closed_or_conflicting_status / human_review"
    if _matches(text, cfg.get("security_sensitive_terms", [])):
        return "REJECT", "travel_restriction / security_sensitive"
    if _matches(title, cfg.get("senior_title_terms", [])):
        return "REJECT", "not_new_grad / eligibility"

    if _matches(text, cfg.get("security_review_terms", [])):
        return "WATCH", "security_review"

    graduation_status, graduation_reason, _ = graduation_evidence(job, scoring, targets)
    if graduation_status != "ELIGIBLE":
        return graduation_status, graduation_reason

    preferred = set(targets.get("candidate_profile", {}).get("preferred_role_families", []))
    expansion = set(targets.get("candidate_profile", {}).get("expansion_role_families", []))
    role_family = str(job.get("role_family") or "other")
    if role_family == "algorithm_research" and _algorithm_role_is_primary(job, scoring):
        return "REJECT", "low_career_value"
    if role_family not in preferred | expansion:
        if role_family == "other":
            return "WATCH", "weak_technical_fit"
        return "REJECT", "weak_technical_fit"

    linkedin_direct = is_direct_linkedin_job(job)
    acceptable = [normalize_token(str(item)) for item in targets.get("job_search", {}).get("acceptable_locations", [])]
    location = normalize_token(str(job.get("location") or ""))
    remote_terms = ("remote", "远程")
    uncertain_location_terms = (
        "多地",
        "待定",
        "具体待定",
        "以岗位详情为准",
        "具体工作地点",
        "multiple locations",
        "various locations",
        "location tbd",
        "to be determined",
    )
    if acceptable and not location:
        return "WATCH", "location_evidence_missing"
    if location and any(term in location for term in uncertain_location_terms):
        return "WATCH", "location_evidence_uncertain"
    # A direct LinkedIn target-location job has already passed the scoped
    # LinkedIn gate above; the global Shenzhen-only list must not narrow it.
    if location and acceptable and not linkedin_direct and not any(item and item in location for item in acceptable):
        if location in {"china", "中国"}:
            return "WATCH", "location / commute"
        if not any(term in location for term in remote_terms):
            return "REJECT", "location / commute"
    return "ELIGIBLE", ""


def _candidate_skills(inventory: dict[str, Any]) -> set[str]:
    return {
        str(item.get("skill"))
        for item in inventory.get("skills", [])
        if isinstance(item, dict) and item.get("skill")
    }


def _role_priority(job: dict[str, Any], taxonomies: dict[str, Any]) -> str:
    role = str(job.get("role_family") or "other")
    return str(taxonomies.get("role_families", {}).get(role, {}).get("priority") or "other")


def technical_fit_score(
    job: dict[str, Any],
    scoring: dict[str, Any],
    taxonomies: dict[str, Any],
    inventory: dict[str, Any],
) -> tuple[int, list[str]]:
    weight = int(scoring.get("weights", {}).get("technical_fit", 30))
    priority_base = {"primary": 16, "expansion": 12, "opportunistic": 8}.get(
        _role_priority(job, taxonomies), 3
    )
    text = _body(job)
    matched_skills: list[str] = []
    for skill in _candidate_skills(inventory):
        aliases = taxonomies.get("skill_taxonomy", {}).get(skill, {}).get("aliases", [skill])
        if _matches(text, aliases):
            matched_skills.append(skill)
    score = priority_base + min(14, len(matched_skills) * 3)
    return min(weight, score), matched_skills


def career_value_score(job: dict[str, Any], scoring: dict[str, Any], taxonomies: dict[str, Any]) -> int:
    weight = int(scoring.get("weights", {}).get("career_value", 25))
    base = {"primary": 16, "expansion": 12, "opportunistic": 8}.get(
        _role_priority(job, taxonomies), 4
    )
    positive_hits = len(_matches(_body(job), scoring.get("career_positive_terms", [])))
    if job.get("dream_role"):
        base += 4
    return min(weight, base + positive_hits * 2)


def portability_score(job: dict[str, Any], scoring: dict[str, Any]) -> tuple[int, list[str]]:
    weight = int(scoring.get("weights", {}).get("skill_portability", 20))
    hits = _matches(_body(job), scoring.get("portable_skill_terms", []))
    return min(weight, 4 + len(hits) * 3), hits


def strategic_value_score(job: dict[str, Any], scoring: dict[str, Any], taxonomies: dict[str, Any]) -> int:
    weight = int(scoring.get("weights", {}).get("strategic_value", 15))
    value = {"primary": 12, "expansion": 9, "opportunistic": 5}.get(
        _role_priority(job, taxonomies), 2
    )
    if job.get("dream_role"):
        value += 3
    return min(weight, value)


def lifestyle_score_and_signals(job: dict[str, Any], scoring: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    weight = int(scoring.get("weights", {}).get("lifestyle_evidence", 10))
    text = _body(job)
    positive = _matches(text, scoring.get("lifestyle_positive_terms", []))
    risks = _matches(text, scoring.get("lifestyle_risk_terms", []))
    actual_work_risks = [
        risk_name
        for risk_name, terms in scoring.get("actual_work_risk_terms", {}).items()
        if _matches(text, terms)
    ]
    score = max(
        0,
        min(weight, 4 + len(positive) * 2 - len(risks) * 2 - len(actual_work_risks)),
    )
    confidence = str(job.get("evidence_confidence") or "Low")
    raw_evidence = job.get("evidence", [])
    evidence_count = int(job.get("evidence_count") or (len(raw_evidence) if isinstance(raw_evidence, list) else 0))
    return score, {
        "compensation_signal": job.get("compensation_signal") or "unknown",
        "wlb_signal": job.get("wlb_signal") or ("risk" if risks else "positive" if positive else "unknown"),
        "leave_usability": job.get("leave_usability") or "unknown",
        "mobility_autonomy": job.get("mobility_autonomy") or "unknown",
        "on_call": job.get("on_call") or ("risk" if any("call" in item or "7x24" in item for item in risks) else "unknown"),
        "evidence_confidence": confidence,
        "evidence_count": evidence_count,
        "lifestyle_positive_evidence": positive,
        "lifestyle_risk_evidence": risks,
        "actual_work_risks": actual_work_risks,
    }


def compute_score(
    job: dict[str, Any],
    scoring: dict[str, Any],
    targets: dict[str, Any],
    taxonomies: dict[str, Any] | None = None,
    inventory: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    del targets
    taxonomies = taxonomies or {}
    inventory = inventory or {}
    technical, matched_skills = technical_fit_score(job, scoring, taxonomies, inventory)
    career = career_value_score(job, scoring, taxonomies)
    portability, portable_hits = portability_score(job, scoring)
    strategic = strategic_value_score(job, scoring, taxonomies)
    lifestyle, signals = lifestyle_score_and_signals(job, scoring)
    total = max(0, min(100, technical + career + portability + strategic + lifestyle))
    breakdown = {
        "technical_fit": technical,
        "matched_candidate_skills": matched_skills,
        "career_value": career,
        "skill_portability": portability,
        "portable_skill_hits": portable_hits,
        "strategic_value": strategic,
        "lifestyle_evidence": lifestyle,
        **signals,
    }
    return total, breakdown


def timing_signals(job: dict[str, Any], scoring: dict[str, Any], as_of: str) -> dict[str, int]:
    text = _body(job)
    cfg = scoring.get("timing", {})
    urgency = min(10, len(_matches(text, cfg.get("urgent_terms", []))) * 4)
    deadline = str(job.get("deadline") or "")
    if deadline:
        try:
            days = (date.fromisoformat(deadline) - date.fromisoformat(as_of)).days
            urgency = max(urgency, 10 if days <= 1 else 8 if days <= 3 else 6 if days <= 7 else 3)
        except ValueError:
            pass
    scarcity = min(10, len(_matches(text, cfg.get("scarcity_terms", []))) * 3 + (4 if job.get("dream_role") else 0))
    process_trigger = min(10, len(_matches(text, cfg.get("process_trigger_terms", []))) * 4)
    raw_cost = job.get("application_cost")
    try:
        cost = int(raw_cost) if raw_cost not in (None, "") else (2 if job.get("easy_apply") else 5 + process_trigger // 2)
    except (TypeError, ValueError):
        cost = 5
    return {
        "urgency": urgency,
        "scarcity": scarcity,
        "process_trigger_risk": process_trigger,
        "application_cost": min(10, max(0, cost)),
    }


def ready_gate(targets: dict[str, Any]) -> bool:
    checks = targets.get("ready_to_apply", {})
    required = (
        "base_cv",
        "intro_1m",
        "two_core_projects",
        "algorithms_refreshed",
        "cs_fundamentals_refreshed",
        "next_72h_available",
    )
    return all(bool(checks.get(key, False)) for key in required)


def queue_decision(
    job: dict[str, Any],
    score: int,
    eligibility: str,
    reject_reason: str,
    timing: dict[str, int],
    scoring: dict[str, Any],
    targets: dict[str, Any],
) -> tuple[str, str, int, int]:
    lifecycle_action = action_for_stage(job.get("stage"))
    if lifecycle_action:
        return (
            lifecycle_action,
            str(
                job.get("action_tier")
                or ("REJECT" if lifecycle_action == "REJECT" else "CAPACITY_SENSITIVE")
            ),
            int(job.get("release_priority") or 0),
            int(job.get("regret") or 0),
        )
    if eligibility == "REJECT":
        return "REJECT", "REJECT", 0, 0
    verification_state = str(job.get("verification_state") or "").upper()
    if not is_verified_open_job(job):
        return "WATCH", "WATCH", 0, 0
    if eligibility == "WATCH" or job.get("freshness") == "stale":
        return "WATCH", "WATCH", 0, 0

    regret = min(10, round(score / 10) + (2 if job.get("dream_role") else 0))
    capacity = targets.get("application_capacity", {})
    process_load = int(capacity.get("process_load_next_72h", 0))
    active = int(capacity.get("active_technical_processes", 0))
    release_priority = max(
        0,
        score
        + timing["urgency"] * 2
        + timing["scarcity"]
        + regret
        - process_load
        - timing["application_cost"],
    )
    thresholds = scoring.get("thresholds", {})
    must_threshold = int(thresholds.get("must_apply", 90))
    capacity_threshold = int(thresholds.get("capacity_sensitive", 80))
    opportunistic_threshold = int(thresholds.get("opportunistic", 70))
    reject_below = int(thresholds.get("reject_below", 45))

    must_apply = score >= must_threshold or (
        score >= capacity_threshold and timing["urgency"] >= 8 and regret >= 8
    )
    if must_apply:
        return "MUST_APPLY", "MUST_APPLY", release_priority, regret
    if score < reject_below:
        return "REJECT", "REJECT", release_priority, regret
    if score < opportunistic_threshold:
        return "WATCH", "WATCH", release_priority, regret

    tier = "CAPACITY_SENSITIVE" if score >= capacity_threshold else "OPPORTUNISTIC"
    phase = str(targets.get("operating_mode", {}).get("phase") or "PRE_APPLICATION_HOLD")
    if phase == "PRE_APPLICATION_HOLD":
        return "HOLD", tier, release_priority, regret
    if not ready_gate(targets):
        return "HOLD", tier, release_priority, regret
    overloaded = active >= int(capacity.get("comfort_max", 5)) or process_load >= int(
        capacity.get("high_load_threshold", 7)
    )
    if overloaded:
        return "HOLD", tier, release_priority, regret
    if tier == "OPPORTUNISTIC" and process_load > 0:
        return "WATCH", tier, release_priority, regret
    return "READY", tier, release_priority, regret


def evaluate_job(
    job: dict[str, Any],
    scoring: dict[str, Any],
    targets: dict[str, Any],
    taxonomies: dict[str, Any],
    inventory: dict[str, Any],
    *,
    as_of: str,
) -> dict[str, Any]:
    evaluated = dict(job)
    eligibility, reject_reason = eligibility_gate(job, scoring, targets)
    graduation_status, _, graduation_hits = graduation_evidence(job, scoring, targets)
    security_review_hits = _matches(
        _body(job), scoring.get("eligibility", {}).get("security_review_terms", [])
    )
    score, breakdown = compute_score(job, scoring, targets, taxonomies, inventory)
    timing = timing_signals(job, scoring, as_of)
    action, tier, release_priority, regret = queue_decision(
        job, score, eligibility, reject_reason, timing, scoring, targets
    )
    verification_state = str(job.get("verification_state") or "").upper()
    if action == "WATCH" and not is_verified_open_job(job) and not reject_reason:
        verification_reasons = {
            "DISCOVERED_CANDIDATE": "secondary_source_requires_fresh_canonical_verification",
            "UNCERTAIN": "open_freshness_or_full_jd_uncertain",
            "CLOSED": "source_closed_or_conflicting_status",
        }
        reject_reason = (
            f"{verification_reasons.get(verification_state, 'verification_state_missing_or_inconsistent')} "
            "/ human_review"
        )
    if action == "REJECT" and not reject_reason:
        reject_reason = "low_career_value"

    evaluated.update(
        {
            "eligibility": eligibility,
            "graduation_eligibility": graduation_status,
            "graduation_evidence": graduation_hits,
            "security_review": security_review_hits,
            "technical_fit": breakdown["technical_fit"],
            "career_value": breakdown["career_value"],
            "skill_portability": breakdown["skill_portability"],
            "strategic_value": breakdown["strategic_value"],
            "opportunity_value": score,
            "matching_score": score,
            "score_breakdown": breakdown,
            "compensation_signal": breakdown["compensation_signal"],
            "wlb_signal": breakdown["wlb_signal"],
            "leave_usability": breakdown["leave_usability"],
            "mobility_autonomy": breakdown["mobility_autonomy"],
            "on_call": breakdown["on_call"],
            "actual_work_risks": breakdown["actual_work_risks"],
            "evidence_confidence": breakdown["evidence_confidence"],
            "evidence_count": breakdown["evidence_count"],
            **timing,
            "regret": regret,
            "release_priority": release_priority,
            "action_tier": tier,
            "action": action,
            "reject_reason": reject_reason,
        }
    )
    if "human_review" in reject_reason:
        evaluated["requires_human_review"] = True
        evaluated["human_review_reason"] = reject_reason
    return evaluated


def mark_registry_candidates(
    jobs: list[dict[str, Any]],
    registry: dict[str, Any],
    scoring: dict[str, Any],
) -> list[dict[str, Any]]:
    """Flag repeated strong non-registry companies for human registry review only."""

    known_names = registry_company_names(registry)
    strong_threshold = int(scoring.get("thresholds", {}).get("capacity_sensitive", 80))
    strong_keys: dict[str, set[str]] = {}
    for job in jobs:
        company_token = normalize_token(str(job.get("company") or ""))
        if (
            not company_token
            or company_in_registry(str(job.get("company") or ""), known_names)
            or str(job.get("eligibility") or "") == "REJECT"
            or str(job.get("role_family") or "other") == "other"
            or int(job.get("opportunity_value") or 0) < strong_threshold
        ):
            continue
        strong_keys.setdefault(company_token, set()).add(
            str(job.get("canonical_key") or job.get("official_url") or job.get("url") or "")
        )

    for job in jobs:
        company = str(job.get("company") or "")
        company_token = normalize_token(company)
        member = company_in_registry(company, known_names)
        job["registry_member"] = member
        job["registry_candidate"] = bool(
            not member and len(strong_keys.get(company_token, set())) >= 2
        )
    return jobs


def main() -> None:
    args = parse_args()
    targets = load_config(args.config)
    scoring = load_config(args.scoring_config)
    taxonomies = load_config(args.taxonomy_config)
    source_registry = load_source_registry(args.source_registry)
    inventory = read_json(args.inventory, {})
    jobs = read_json(args.input, [])
    as_of = args.as_of or date.today().isoformat()

    if args.mode == "backfill":
        backfill_config = targets.get("job_search", {}).get("search_modes", {}).get("backfill", {})
        max_age_days = int(backfill_config.get("posted_within_days", 60))
        jobs = [
            job
            for job in jobs
            if isinstance(job, dict)
            and backfill_discoverable(job, as_of, max_age_days=max_age_days)
        ]

    evaluated = [
        evaluate_job(job, scoring, targets, taxonomies, inventory, as_of=as_of)
        for job in jobs
        if isinstance(job, dict)
    ]
    mark_registry_candidates(evaluated, source_registry, scoring)
    evaluated.sort(
        key=lambda item: (
            ACTION_ORDER.get(str(item.get("action")), 9),
            -int(item.get("release_priority", 0)),
            -int(item.get("opportunity_value", 0)),
        )
    )
    write_json(args.all_output, evaluated)

    selected = [item for item in evaluated if item.get("action") != "REJECT"]
    top_k = args.top_k or int(targets.get("schedule", {}).get("max_jobs_per_run", 30))
    selected = selected[:top_k]
    write_json(Path(args.output), selected)
    counts = {action: sum(1 for item in evaluated if item.get("action") == action) for action in ACTION_ORDER}
    print(f"Decision funnel -> {args.all_output}; queue -> {args.output}; counts={counts}")


if __name__ == "__main__":
    main()
