#!/usr/bin/env python3
"""Score fetched jobs and select the best matches."""

from __future__ import annotations

import argparse
from pathlib import Path

try:
    from common import canonical_job_key, load_config, normalize_whitespace, read_json, write_json
except ModuleNotFoundError:
    from scripts.common import canonical_job_key, load_config, normalize_whitespace, read_json, write_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score cached jobs for resume targeting.")
    parser.add_argument("--config", default="config/targets.yaml")
    parser.add_argument("--scoring-config", default="config/scoring.yaml")
    parser.add_argument("--input", default="data/job_cache/jobs.json")
    parser.add_argument("--output", default="data/job_cache/scored_jobs.json")
    parser.add_argument("--top-k", type=int, default=0, help="0 means use config max_jobs_per_run")
    return parser.parse_args()


def normalized_title_and_body(job: dict) -> tuple[str, str]:
    title = normalize_whitespace(job.get("position", "")).lower()
    body = normalize_whitespace(
        f"{job.get('position', '')} {job.get('description', '')} {job.get('location', '')}"
    ).lower()
    return title, body


def contains_any(text: str, phrases: list[str]) -> bool:
    text_lower = text.lower()
    return any(str(phrase).lower() in text_lower for phrase in phrases)


def matching_phrases(text: str, phrases: list[str]) -> list[str]:
    text_lower = text.lower()
    return [phrase for phrase in phrases if str(phrase).lower() in text_lower]


def hard_filter_reason(job: dict, scoring: dict) -> str:
    title, body = normalized_title_and_body(job)
    hard_filters = scoring.get("hard_filters", {})
    if contains_any(title, hard_filters.get("title_contains", [])):
        return "title_hard_filter"
    if contains_any(body, hard_filters.get("experience_hard_filters", [])):
        return "experience_hard_filter"
    if contains_any(body, hard_filters.get("visa_hard_filters", [])):
        return "visa_hard_filter"
    if contains_any(body, hard_filters.get("student_status_hard_filters", [])):
        return "student_status_hard_filter"
    return ""


def best_role_track_score(title: str, body: str, scoring: dict) -> tuple[int, str]:
    weight = int(scoring.get("weights", {}).get("role_track_fit", 25))
    best_track = ""
    best_score = 0.0
    for track_name, track in scoring.get("role_tracks", {}).items():
        title_hits = sum(1 for keyword in track.get("title_keywords", []) if keyword.lower() in title)
        jd_hits = sum(1 for keyword in track.get("jd_keywords", []) if keyword.lower() in body)
        raw_score = title_hits * 6 + jd_hits * 2
        if raw_score > best_score:
            best_score = raw_score
            best_track = track_name
    return min(weight, int(round(best_score))), best_track


def evidence_skill_score(body: str, scoring: dict, best_track: str) -> tuple[int, list[str]]:
    weight = int(scoring.get("weights", {}).get("evidence_skill_fit", 30))
    track_weights = scoring.get("track_evidence_weights", {}).get(best_track, {})
    matched_groups: list[str] = []
    score = 0.0
    for group_name, keywords in scoring.get("strong_evidence_skills", {}).items():
        hits = sum(1 for keyword in keywords if keyword.lower() in body)
        if hits:
            group_weight = float(track_weights.get(group_name, 1.0 if not track_weights else 0.0))
            if group_weight <= 0:
                continue
            matched_groups.append(group_name)
            group_score = min(10, 3 + hits * 2)
            score += group_score * group_weight
    return min(weight, int(round(score))), matched_groups


def unsupported_stack_penalty(body: str, scoring: dict) -> tuple[int, list[str]]:
    penalties: list[str] = []
    total_penalty = 0
    downgrade_markers = ("preferred", "nice to have", "familiarity", "bonus", "desirable", "plus")
    adjustments = scoring.get("penalty_adjustments", {})
    training_markers = tuple(marker.lower() for marker in adjustments.get("training_markers", []))
    reduction_factor = float(adjustments.get("unsupported_stack_penalty_reduction_factor", 1.0))
    for gap_name, gap in scoring.get("unsupported_critical_stack", {}).items():
        keywords = [keyword.lower() for keyword in gap.get("keywords", [])]
        hits = sum(1 for keyword in keywords if keyword in body)
        if hits >= 2 and not any(marker in body for marker in downgrade_markers):
            penalties.append(gap_name)
            penalty = int(gap.get("penalty_if_core", 0))
            if training_markers and any(marker in body for marker in training_markers):
                penalty = max(1, int(round(penalty * reduction_factor)))
            total_penalty += penalty
    return total_penalty, penalties


def seniority_training_score(title: str, body: str, scoring: dict) -> int:
    weight = int(scoring.get("weights", {}).get("seniority_training_fit", 20))
    positive = scoring.get("seniority_positive", {})
    negative = scoring.get("seniority_negative", {})
    score = 0
    for keyword, value in positive.get("title_keywords", {}).items():
        if keyword.lower() in title:
            score += int(value)
    for keyword, value in positive.get("jd_phrases", {}).items():
        if keyword.lower() in body:
            score += int(value)
    for keyword, value in negative.get("title_keywords", {}).items():
        if keyword.lower() in title:
            score += int(value)
    for keyword, value in negative.get("jd_phrases", {}).items():
        if keyword.lower() in body:
            score += int(value)
    return max(0, min(weight, score))


def delivery_workflow_score(body: str, scoring: dict) -> int:
    weight = int(scoring.get("weights", {}).get("delivery_workflow_fit", 10))
    score = 0
    for keyword, value in scoring.get("delivery_workflow_fit", {}).items():
        if keyword.lower() in body:
            score += int(value)
    return min(weight, score)


def visa_work_auth_score(body: str, scoring: dict) -> tuple[int, str]:
    weight = int(scoring.get("weights", {}).get("visa_work_auth_fit", 10))
    visa_cfg = scoring.get("visa_work_auth_fit", {})
    for keyword, value in visa_cfg.get("positive", {}).items():
        if keyword.lower() in body:
            return min(weight, int(value)), "positive"
    if contains_any(body, [phrase.lower() for phrase in scoring.get("visa_medium_risks", [])]):
        return min(weight, int(visa_cfg.get("medium_risk_score", 3))), "medium_risk"
    if contains_any(body, [phrase.lower() for phrase in scoring.get("visa_soft_risks", [])]):
        return min(weight, int(visa_cfg.get("soft_risk_score", 3))), "soft_risk"
    return min(weight, int(visa_cfg.get("neutral_default", 5))), "neutral"


def risk_penalty(body: str, scoring: dict) -> tuple[int, list[str]]:
    total_penalty = 0
    flags: list[str] = []
    risk_cfg = scoring.get("risk_penalties", {})

    medium_hits = matching_phrases(body, [phrase.lower() for phrase in scoring.get("visa_medium_risks", [])])
    if medium_hits:
        flags.append("clearance_eligibility_risk")
        total_penalty += int(risk_cfg.get("clearance_eligibility_risk", 10))

    soft_hits = matching_phrases(body, [phrase.lower() for phrase in scoring.get("visa_soft_risks", [])])
    if soft_hits:
        sector_risk_terms = {"defence", "defense", "national security", "homeland security", "government clients"}
        if any(term in hit for hit in soft_hits for term in sector_risk_terms):
            flags.append("defence_security_sector")
            total_penalty += int(risk_cfg.get("defence_security_sector", 5))
        else:
            flags.append("regulated_or_export_risk")
            total_penalty += int(risk_cfg.get("regulated_or_export_risk", 3))

    return total_penalty, flags


def application_effort_bonus(job: dict, scoring: dict) -> int:
    weight = int(scoring.get("weights", {}).get("application_effort_bonus", 5))
    if job.get("easy_apply"):
        return min(weight, int(scoring.get("application_effort_bonus", {}).get("easy_apply", 5)))
    return 0


def experience_penalty(body: str, scoring: dict) -> int:
    penalties_cfg = scoring.get("experience_penalties", {})
    if contains_any(body, penalties_cfg.get("heavy", [])):
        return 15
    if contains_any(body, penalties_cfg.get("medium", [])):
        return 8
    return 0


def compute_score(job: dict, scoring: dict, targets: dict) -> tuple[int, dict]:
    del targets
    title, body = normalized_title_and_body(job)
    role_score, best_track = best_role_track_score(title, body, scoring)
    evidence_score, evidence_groups = evidence_skill_score(body, scoring, best_track)
    seniority_score = seniority_training_score(title, body, scoring)
    delivery_score = delivery_workflow_score(body, scoring)
    visa_score, visa_bucket = visa_work_auth_score(body, scoring)
    effort_bonus = application_effort_bonus(job, scoring)
    stack_penalty, stack_flags = unsupported_stack_penalty(body, scoring)
    years_penalty = experience_penalty(body, scoring)
    extra_risk_penalty, risk_flags = risk_penalty(body, scoring)

    total = role_score + evidence_score + seniority_score + delivery_score + visa_score + effort_bonus
    total -= stack_penalty + years_penalty + extra_risk_penalty
    total = max(0, min(100, total))

    breakdown = {
        "role_track_fit": role_score,
        "best_track": best_track,
        "evidence_skill_fit": evidence_score,
        "evidence_groups": evidence_groups,
        "seniority_training_fit": seniority_score,
        "delivery_workflow_fit": delivery_score,
        "visa_work_auth_fit": visa_score,
        "visa_bucket": visa_bucket,
        "application_effort_bonus": effort_bonus,
        "unsupported_stack_penalty": stack_penalty,
        "unsupported_stack_flags": stack_flags,
        "experience_penalty": years_penalty,
        "risk_penalty": extra_risk_penalty,
        "risk_flags": risk_flags,
    }
    return int(total), breakdown


def choose_better_duplicate(current: dict, candidate: dict) -> dict:
    current_tuple = (
        int(current.get("matching_score", 0)),
        len(current.get("description", "")),
        int(bool(current.get("easy_apply"))),
    )
    candidate_tuple = (
        int(candidate.get("matching_score", 0)),
        len(candidate.get("description", "")),
        int(bool(candidate.get("easy_apply"))),
    )
    return candidate if candidate_tuple > current_tuple else current


def main() -> None:
    args = parse_args()
    targets = load_config(args.config)
    scoring = load_config(args.scoring_config)
    input_path = Path(args.input)
    jobs = read_json(input_path, [])

    deduped_scored_jobs: dict[str, dict] = {}
    for job in jobs:
        reject_reason = hard_filter_reason(job, scoring)
        if reject_reason:
            continue
        scored = dict(job)
        score, breakdown = compute_score(job, scoring, targets)
        scored["matching_score"] = score
        scored["score_breakdown"] = breakdown
        key = canonical_job_key(scored.get("company", ""), scored.get("position", ""))
        if key in deduped_scored_jobs:
            deduped_scored_jobs[key] = choose_better_duplicate(deduped_scored_jobs[key], scored)
        else:
            deduped_scored_jobs[key] = scored

    scored_jobs = list(deduped_scored_jobs.values())
    scored_jobs.sort(key=lambda item: item.get("matching_score", 0), reverse=True)
    shortlist_min_score = int(targets.get("job_search", {}).get("shortlist_min_score", 70))
    selected = [job for job in scored_jobs if job.get("matching_score", 0) >= shortlist_min_score]
    top_k = args.top_k or int(targets.get("schedule", {}).get("max_jobs_per_run", 10))
    selected = selected[:top_k]

    output_path = Path(args.output)
    write_json(output_path, selected)
    print(f"Wrote {len(selected)} scored jobs to {output_path}")


if __name__ == "__main__":
    main()
