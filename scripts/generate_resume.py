#!/usr/bin/env python3
"""Generate targeted resume and cover-letter variants from LaTeX materials."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

try:
    from application_profiles import identity_for_job, infer_job_language
    from common import canonical_job_key, load_config, normalize_token, normalize_whitespace, read_json, slugify
    from job_schema import is_verified_open_job
except ModuleNotFoundError:
    from scripts.application_profiles import identity_for_job, infer_job_language
    from scripts.common import canonical_job_key, load_config, normalize_token, normalize_whitespace, read_json, slugify
    from scripts.job_schema import is_verified_open_job

try:
    from resume_v2 import (
        build_resume_content_plan,
        extract_pdf_geometry,
        fit_resume_pdf,
        render_resume_from_plan,
        run_quality_gates,
    )
except ModuleNotFoundError:
    from scripts.resume_v2 import (
        build_resume_content_plan,
        extract_pdf_geometry,
        fit_resume_pdf,
        render_resume_from_plan,
        run_quality_gates,
    )

try:
    from resume_optimizer import apply_resume_optimizer_edits
except ModuleNotFoundError:
    from scripts.resume_optimizer import apply_resume_optimizer_edits

try:
    from resume_bases import (
        apply_base_content_manifest,
        base_page_fit_contract,
        build_tailoring_report,
        import_registered_bases,
        load_resume_base_content,
        load_resume_base_registry,
        prepare_selected_template,
        select_resume_base,
        semantic_cache_identity,
    )
except ModuleNotFoundError:
    from scripts.resume_bases import (
        apply_base_content_manifest,
        base_page_fit_contract,
        build_tailoring_report,
        import_registered_bases,
        load_resume_base_content,
        load_resume_base_registry,
        prepare_selected_template,
        select_resume_base,
        semantic_cache_identity,
    )


ROLE_KEYWORDS = {
    "test_development": ("test", "testing", "sdet", "qa", "automation", "pytest", "validation"),
    "ai_application": ("ai", "machine learning", "llm", "nlp", "rag", "agent", "prompt"),
    "robot_software": ("robotics", "robot", "ros2", "vision", "perception", "sensor"),
    "general_software": ("software", "python", "api", "linux", "application", "developer"),
    "backend": ("backend", "server", "api", "django", "fastapi", "database"),
    "data_engineering": ("data", "pipeline", "sql", "etl", "analytics", "dashboard"),
    "engineering_tools": ("tooling", "developer tools", "build system", "ci/cd"),
    "software_automation": ("automation", "workflow", "scripting"),
    "reliability": ("reliability", "resilience", "fault", "observability"),
}

ROLE_DISPLAY = {
    "test_development": "Test Development / Automation",
    "ai_application": "AI Application / LLM",
    "robot_software": "Robotics Software",
    "general_software": "Programming / Interfaces",
    "backend": "Backend / APIs",
    "data_engineering": "Data Engineering",
    "engineering_tools": "Engineering Tools",
    "software_automation": "Software Automation",
    "reliability": "Reliability Engineering",
}

CANONICAL_MATERIAL_ROLES = frozenset(ROLE_KEYWORDS)
ROLE_ALIASES = {
    "ai_engineer": "ai_application",
    "software_engineer": "general_software",
    "data_scientist": "data_engineering",
    "robotics_engineer": "robot_software",
    "fintech_backend": "backend",
    "fintech_tech": "general_software",
    "platform_sre": "reliability",
    "data_platform_sre": "reliability",
    "developer_productivity": "engineering_tools",
    "algorithm_research": "ai_application",
}
PROFICIENCY_RANK = {"strong": 0, "working": 1, "exposure": 2}

SAFE_FOCUS_PHRASES = {
    "production": "with a focus on reliable delivery",
    "testing": "with an emphasis on testing and debugging",
    "stakeholder": "to support stakeholder-facing decisions",
    "scale": "with attention to maintainability and scale",
    "workflow": "to support reproducible engineering workflows",
}
RESUME_SAFE_SOURCE_CLASSES = frozenset(
    {"VERIFIED", "USER_ATTESTED", "DERIVED_RESUME_SAFE"}
)

DEFAULT_PROFILE = {
    "full_name": "",
    "email": "",
    "phone": "",
    "alternate_phone": "",
    "github_url": "",
    "linkedin_url": "",
    "portfolio_url": "",
    "closing_name": "",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate targeted resume variants.")
    parser.add_argument("--jobs", default="data/job_cache/scored_jobs.json")
    parser.add_argument("--resume-template", default="cv/resume.tex")
    parser.add_argument("--coverletter-template", default="cv/coverletter.tex")
    parser.add_argument("--output-dir", default="cv/generated")
    parser.add_argument("--profile-config", default="config/profile.yaml")
    parser.add_argument("--materials-dir", default="cv/materials")
    parser.add_argument("--resume-bases", default="config/resume_bases.yaml")
    parser.add_argument("--resume-base-content", default="config/resume_base_content.yaml")
    parser.add_argument(
        "--resume-version", choices=("v1", "v2"), default="v2",
        help="V2 uses a full role-family blueprint and rendered PDF page fitting",
    )
    parser.add_argument(
        "--resume-language", choices=("CN", "EN", "BOTH"), default="",
        help="Independent resume-language override; profile selection remains separate",
    )
    parser.add_argument(
        "--resume-variant", choices=("auto", "photo", "no_photo"), default="auto",
        help="Select the approved CN photo/no-photo variant; defaults remain configurable",
    )
    parser.add_argument(
        "--allow-english-photo-fallback",
        action="store_true",
        help="Explicitly allow EN + photo requests to resolve to the approved EN no-photo base",
    )
    parser.add_argument(
        "--legacy-resume-fallback",
        action="store_true",
        help="Explicitly allow the generic cv/resume.tex path when no approved role base exists",
    )
    parser.add_argument(
        "--semantic-review-cache", default="data/job_cache/semantic_reviews",
        help="Existing local review cache used only for evidence priority",
    )
    parser.add_argument(
        "--resume-optimizer-edits",
        default="",
        help="Optional local JSON containing validated Chinese resume edit records",
    )
    parser.add_argument(
        "--resume-optimizer-dir",
        default="data/job_cache/resume_optimizer",
        help="Validated edit cache; exact job_identity matches are reused without a model call",
    )
    parser.add_argument(
        "--application-profile",
        choices=("CN", "INTL"),
        default="",
        help="Manual global override; otherwise each job's override/recommendation is used",
    )
    parser.add_argument("--min-score", type=int, default=60)
    parser.add_argument(
        "--actions",
        default="MUST_APPLY,READY",
        help="Comma-separated queue actions allowed to generate drafts",
    )
    compile_mode = parser.add_mutually_exclusive_group()
    compile_mode.add_argument("--compile-pdf", dest="compile_pdf", action="store_true")
    compile_mode.add_argument("--no-compile-pdf", dest="compile_pdf", action="store_false")
    parser.set_defaults(compile_pdf=False)
    return parser.parse_args()


def load_profile(
    path: str,
    job: dict | None = None,
    *,
    application_profile: str = "",
) -> dict[str, str]:
    profile = dict(DEFAULT_PROFILE)
    config_path = Path(path)
    if not config_path.exists():
        return profile
    raw = load_config(config_path)
    _, identity = identity_for_job(raw, job, manual_override=application_profile)
    for key in profile:
        loaded = identity.get(key)
        if loaded:
            profile[key] = str(loaded)
    if not profile["closing_name"]:
        profile["closing_name"] = profile["full_name"]
    return profile


def apply_profile_placeholders(template: str, profile: dict[str, str]) -> str:
    replacements = {
        "{{FULL_NAME}}": profile["full_name"],
        "{{EMAIL}}": profile["email"],
        "{{PHONE}}": profile["phone"],
        "{{GITHUB_URL}}": profile["github_url"],
        "{{LINKEDIN_URL}}": profile["linkedin_url"],
        "{{PORTFOLIO_URL}}": profile["portfolio_url"],
        "{{CONTACT_LINE}}": profile_contact_latex(profile),
        "{{CLOSING_NAME}}": profile["closing_name"],
    }
    updated = template
    for source, target in replacements.items():
        updated = updated.replace(source, target)
    return updated


def profile_contact_latex(profile: dict[str, str]) -> str:
    parts: list[str] = []
    if profile.get("email"):
        email = latex_escape(profile["email"])
        parts.append(r"\href{mailto:" + email + "}{" + email + "}")
    if profile.get("phone"):
        parts.append(latex_escape(profile["phone"]))
    for key, label in (
        ("github_url", "GitHub"),
        ("linkedin_url", "LinkedIn"),
        ("portfolio_url", "Portfolio"),
    ):
        if profile.get(key):
            parts.append(r"\href{" + latex_escape(profile[key]) + "}{" + label + "}")
    return r" \;|\; ".join(parts)


def latex_escape(text: object) -> str:
    text = str(text or "")
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text


def canonical_material_role(value: object) -> str:
    role = str(value or "").strip()
    if role in CANONICAL_MATERIAL_ROLES:
        return role
    return ROLE_ALIASES.get(role, "")


def infer_role_family(job: dict) -> str:
    explicit = canonical_material_role(job.get("role_family"))
    if explicit:
        return explicit
    compatible_resume_family = canonical_material_role(job.get("resume_family"))
    if compatible_resume_family:
        return compatible_resume_family
    title_text = job.get("position") or job.get("title") or ""
    title = normalize_token(title_text)
    secondary_tags = " ".join(str(item) for item in job.get("secondary_role_tags", []))
    body = normalize_token(
        f"{title_text} {job.get('description', '')} {secondary_tags}"
    )
    scores = {}
    for role, keywords in ROLE_KEYWORDS.items():
        score = 0
        for keyword in keywords:
            if keyword in body:
                score += 1
            if keyword in title:
                score += 2
        scores[role] = score
    if any(token in title for token in ("ai", "machine learning", "artificial intelligence", "llm", "nlp", "大模型")):
        scores["ai_application"] += 6
    if any(token in title for token in ("robotics", "ros2", "perception", "vision")):
        scores["robot_software"] += 6
    if any(token in title for token in ("test", "sdet", "qa", "测试")):
        scores["test_development"] += 6
    return max(scores, key=scores.get) if max(scores.values()) > 0 else "general_software"


def extract_job_signals(job: dict, role_family: str) -> dict[str, list[str] | str]:
    role_family = canonical_material_role(role_family) or "general_software"
    secondary_tags = [str(item) for item in job.get("secondary_role_tags", [])]
    body = normalize_token(
        f"{job.get('position') or job.get('title') or ''} "
        f"{job.get('description', '')} {' '.join(secondary_tags)}"
    )
    role_keywords = list(ROLE_KEYWORDS.get(role_family, ()))
    matched_keywords = [keyword for keyword in role_keywords if keyword in body]
    focus_terms = []
    for key in SAFE_FOCUS_PHRASES:
        if key in body:
            focus_terms.append(key)
    company_focus = extract_context_sentences(job.get("description", ""))
    return {
        "body": body,
        "matched_keywords": matched_keywords[:8],
        "focus_terms": focus_terms[:4],
        "company_focus": company_focus[:4],
        "secondary_role_tags": secondary_tags,
    }


def extract_context_sentences(description: str) -> list[str]:
    cleaned = normalize_whitespace(description)
    if not cleaned:
        return []
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    keywords = (
        "team",
        "product",
        "workflow",
        "production",
        "reliability",
        "stakeholder",
        "internal",
        "tool",
        "engineering",
        "delivery",
        "maintain",
        "scale",
    )
    banned_phrases = (
        "grow to their full potential",
        "all-time great companies",
        "that’s our promise",
        "competitive pay",
        "benefits include",
    )
    selected: list[tuple[int, str]] = []
    high_value_keywords = ("production", "reliability", "workflow", "internal", "tool", "engineering", "product", "delivery", "maintain", "scale")
    for sentence in sentences:
        lowered = sentence.lower()
        if any(phrase in lowered for phrase in banned_phrases):
            continue
        if 40 <= len(sentence) <= 240 and any(keyword in lowered for keyword in keywords):
            score = sum(2 for keyword in high_value_keywords if keyword in lowered)
            score += sum(1 for keyword in keywords if keyword in lowered)
            selected.append((score, sentence.strip()))
        if len(selected) >= 8:
            break
    selected.sort(key=lambda item: item[0], reverse=True)
    return [sentence for _, sentence in selected[:4]]


def item_score(
    job_signals: dict,
    item: dict,
    role_family: str,
    material_language: str = "EN",
) -> int:
    score = 0
    body = str(job_signals["body"])
    for tag in item.get("tags", []):
        if normalize_token(str(tag)) in body:
            score += 5
    if role_family in item.get("role_targets", []):
        score += 8
    for bullet in collect_candidate_bullets(item, role_family, material_language):
        normalized_bullet = normalize_token(str(bullet))
        for keyword in job_signals.get("matched_keywords", []):
            if keyword in normalized_bullet and keyword in body:
                score += 2
    return score


def collect_candidate_bullets(
    item: dict,
    role_family: str,
    material_language: str = "EN",
) -> list[str]:
    role_family = canonical_material_role(role_family) or "general_software"
    suffix = "_cn" if material_language == "CN" else ""
    variants = item.get(f"bullet_variants{suffix}", {})
    role_specific = list(variants.get(role_family, []))
    base = list(item.get(f"bullets{suffix}", []))
    if material_language == "CN" and not (role_specific or base):
        return collect_candidate_bullets(item, role_family, "EN")
    return role_specific + base


def resume_material_allowed(item: dict) -> bool:
    if item.get("resume_eligible") is False:
        return False
    source_class = str(item.get("source_class") or "DERIVED_RESUME_SAFE").upper()
    return source_class in RESUME_SAFE_SOURCE_CLASSES


def skill_name(item: object) -> str:
    if isinstance(item, dict):
        return str(item.get("name") or "").strip()
    return str(item).strip()


def skill_resume_allowed(item: object) -> bool:
    return not isinstance(item, dict) or resume_material_allowed(item)


def bullet_score(job_signals: dict, bullet: str) -> int:
    normalized_bullet = normalize_token(bullet)
    score = 0
    for keyword in job_signals.get("matched_keywords", []):
        if keyword in normalized_bullet:
            score += 3
    for focus_term in job_signals.get("focus_terms", []):
        if focus_term in normalized_bullet:
            score += 2
    return score


def rewrite_bullet_for_job(bullet: str, job_signals: dict) -> str:
    updated = bullet.strip()
    lowered = normalize_token(updated)
    additions = []
    for focus_term in job_signals.get("focus_terms", []):
        phrase = SAFE_FOCUS_PHRASES[focus_term]
        if focus_term == "production" and any(token in lowered for token in ("backend", "api", "workflow", "docker")):
            additions.append(phrase)
        elif focus_term == "testing" and any(token in lowered for token in ("testing", "debug", "evaluation", "checks")):
            additions.append(phrase)
        elif focus_term == "stakeholder" and any(token in lowered for token in ("dashboard", "report", "stakeholder", "analysis")):
            additions.append(phrase)
        elif focus_term == "scale" and any(token in lowered for token in ("backend", "workflow", "module")):
            additions.append(phrase)
        elif focus_term == "workflow" and any(token in lowered for token in ("workflow", "docker", "testing", "reproducibility")):
            additions.append(phrase)
    if additions:
        suffix = additions[0]
        if suffix not in lowered and not updated.endswith("."):
            updated += "."
        updated = updated.rstrip(".") + f", {suffix}."
    return updated


def select_ranked_bullets(
    job_signals: dict,
    item: dict,
    limit: int,
    material_language: str = "EN",
) -> list[str]:
    role_family = str(item.get("active_role_family", ""))
    candidates = collect_candidate_bullets(item, role_family, material_language)
    ranked = sorted(candidates, key=lambda bullet: bullet_score(job_signals, str(bullet)), reverse=True)
    chosen = ranked[:limit] if ranked else candidates[:limit]
    return [rewrite_bullet_for_job(str(bullet), job_signals) for bullet in chosen]


def count_pdf_pages(pdf_path: Path) -> int:
    return int(extract_pdf_geometry(pdf_path)["page_count"])


def _localized(item: dict, key: str, material_language: str) -> object:
    if material_language == "CN" and item.get(f"{key}_cn"):
        return item[f"{key}_cn"]
    return item.get(key, "")


def build_experience_section(
    experiences: list[dict], material_language: str = "EN"
) -> str:
    lines = [r"\section{Experience}", r"\resumeSubHeadingListStart", ""]
    for experience in experiences:
        lines.extend(
            [
                r"  \resumeSubheading",
                f"    {{{latex_escape(_localized(experience, 'company', material_language))}}}{{{latex_escape(_localized(experience, 'location', material_language))}}}",
                f"    {{{latex_escape(_localized(experience, 'title', material_language))}}}{{{latex_escape(_localized(experience, 'date_range', material_language))}}}",
                r"    \resumeItemListStart",
            ]
        )
        for bullet in experience.get("selected_bullets", []):
            lines.append(f"      \\item \\small{{{latex_escape(bullet)}}}")
        lines.extend([r"    \resumeItemListEnd", ""])
    lines.append(r"\resumeSubHeadingListEnd")
    return "\n".join(lines)


def build_education_section(
    education_material: list[dict], material_language: str = "EN"
) -> str:
    lines = [r"\section{Education}", r"\resumeSubHeadingListStart"]
    for education in education_material:
        if not resume_material_allowed(education):
            continue
        lines.extend(
            [
                r"  \resumeSubheading",
                f"    {{{latex_escape(_localized(education, 'institution', material_language))}}}{{}}",
                f"    {{{latex_escape(_localized(education, 'qualification', material_language))}}}{{{latex_escape(_localized(education, 'date_range', material_language))}}}",
            ]
        )
    lines.append(r"\resumeSubHeadingListEnd")
    return "\n".join(lines)


def build_project_section(projects: list[dict], material_language: str = "EN") -> str:
    lines = [r"\section{Projects}", r"\resumeSubHeadingListStart", ""]
    for project in projects:
        repo_url = project.get("repo_url", "")
        name = _localized(project, "name", material_language)
        if repo_url:
            title = r"\href{" + repo_url + "}{" + latex_escape(name) + "}"
        else:
            title = latex_escape(name)
        lines.extend(
            [
                r"  \resumeSubheading",
                f"    {{{title}}}{{}}",
                f"    {{{latex_escape(_localized(project, 'display_role', material_language) or 'Project')}}}{{{latex_escape(_localized(project, 'date_range', material_language))}}}",
                r"    \resumeItemListStart",
            ]
        )
        for bullet in project.get("selected_bullets", []):
            lines.append(f"      \\item \\small{{{latex_escape(bullet)}}}")
        lines.extend([r"    \resumeItemListEnd", ""])
    lines.append(r"\resumeSubHeadingListEnd")
    return "\n".join(lines)

def build_skills_section(
    skill_groups: dict[str, list[object]],
    role_family: str,
    job_signals: dict,
    max_skills_per_group: int = 12,
) -> str:
    body = str(job_signals["body"])
    role_family = canonical_material_role(role_family) or "general_software"
    preferred_order = [role_family] + [key for key in skill_groups.keys() if key != role_family]
    rendered_rows = []
    for group_name in preferred_order:
        skills = [
            (
                skill_name(item),
                PROFICIENCY_RANK.get(
                    str(item.get("proficiency") or "exposure") if isinstance(item, dict) else "exposure",
                    99,
                ),
            )
            for item in skill_groups.get(group_name, [])
            if skill_resume_allowed(item) and skill_name(item)
        ]
        skills.sort(
            key=lambda item: (
                0 if normalize_token(item[0]) in body else 1,
                item[1],
                item[0].lower(),
            )
        )
        if skills:
            rendered_rows.append(
                f"    \\textbf{{{ROLE_DISPLAY.get(group_name, group_name)}}}{{: {latex_escape(', '.join(name for name, _ in skills[:max_skills_per_group]))}}} \\\\"
            )
    return "\n".join(
        [
            r"\section{Technical Skills}",
            r"\resumeSubHeadingListStart",
            r"  \item{",
            *rendered_rows,
            r"  }",
            r"\resumeSubHeadingListEnd",
        ]
    )


def replace_section(template: str, section_name: str, replacement: str, next_section: str) -> str:
    pattern = rf"\\section\{{{re.escape(section_name)}\}}.*?\\section\{{{re.escape(next_section)}\}}"
    return re.sub(pattern, lambda _: replacement + "\n\n" + rf"\section{{{next_section}}}", template, flags=re.DOTALL)


def render_targeted_resume(
    template: str,
    job: dict,
    experiences_material: list[dict],
    projects_material: list[dict],
    skill_groups: dict[str, list[str]],
    *,
    education_material: list[dict] | None = None,
    experience_bullet_limit: int = 4,
    project_bullet_limit: int = 3,
    max_projects: int = 3,
    max_skills_per_group: int = 12,
) -> tuple[str, list[str], list[str]]:
    role_family = infer_role_family(job)
    job_signals = extract_job_signals(job, role_family)
    requested_language = str(
        job.get("resume_language") or job.get("recommended_resume_language") or ""
    ).upper()
    material_language = "CN" if requested_language == "CN" else "EN"
    if requested_language in {"", "BOTH"} and infer_job_language(job) == "ZH":
        material_language = "CN"

    ranked_experiences = []
    for experience in experiences_material:
        if not resume_material_allowed(experience):
            continue
        item = dict(experience)
        item["active_role_family"] = role_family
        item["score"] = item_score(
            job_signals, item, role_family, material_language
        )
        item["selected_bullets"] = select_ranked_bullets(
            job_signals, item, experience_bullet_limit, material_language
        )
        ranked_experiences.append(item)
    ranked_experiences.sort(key=lambda item: item.get("score", 0), reverse=True)
    selected_experiences = ranked_experiences[:1]

    ranked_projects = []
    for project in projects_material:
        if not resume_material_allowed(project):
            continue
        item = dict(project)
        item["active_role_family"] = role_family
        item["score"] = item_score(
            job_signals, item, role_family, material_language
        )
        item["selected_bullets"] = select_ranked_bullets(
            job_signals, item, project_bullet_limit, material_language
        )
        ranked_projects.append(item)
    ranked_projects.sort(key=lambda item: item.get("score", 0), reverse=True)
    selected_projects = ranked_projects[:max_projects]
    selected_ids = [item.get("id", "") for item in selected_projects]
    selected_names = [
        str(_localized(item, "name", material_language)) for item in selected_projects
    ]

    education_section = build_education_section(
        education_material or [], material_language
    )
    experience_section = build_experience_section(selected_experiences, material_language)
    project_section = build_project_section(selected_projects, material_language)
    skills_section = build_skills_section(skill_groups, role_family, job_signals, max_skills_per_group=max_skills_per_group)

    updated = replace_section(template, "Education", education_section, "Experience")
    updated = replace_section(updated, "Experience", experience_section, "Projects")
    updated = replace_section(updated, "Projects", project_section, "Technical Skills")
    updated = re.sub(
        r"\\section\{Technical Skills\}.*?\\end\{document\}",
        lambda _: skills_section + "\n\n" + r"\end{document}",
        updated,
        flags=re.DOTALL,
    )
    header = (
        f"% Generated for {job.get('company', '')} - {job.get('position', '')}\n"
        f"% Selected role family: {role_family}\n"
        f"% Selected material language: {material_language}\n"
        f"% Selected projects: {', '.join(selected_ids)}\n"
    )
    return header + updated, selected_ids, selected_names


def company_focus_sentence(job: dict, job_signals: dict) -> str:
    description = normalize_whitespace(job.get("description", ""))
    body = description.lower()
    company_focus = job_signals.get("company_focus", [])
    if company_focus:
        return f"What attracts me most is the practical direction described in the role itself: {company_focus[0]}"
    if "production" in body and "reliability" in body:
        return "What attracts me most is the chance to help move systems into reliable, production-ready use."
    if "internal tools" in body or "stakeholder" in body:
        return "What attracts me most is the practical focus on building tools that support real internal users and decisions."
    if "team" in body and "product" in body:
        return "What attracts me most is the opportunity to work closely across product, engineering and data-facing workflows."
    return "What attracts me most is the chance to build practical software and AI systems that are useful for real users."


def build_cover_letter_paragraphs(
    job: dict,
    role_family: str,
    job_signals: dict,
    selected_project_names: list[str],
    *,
    compact: bool = False,
) -> list[str]:
    role_family = canonical_material_role(role_family) or "general_software"
    company = job.get("company", "the company")
    position = job.get("position", "the role")
    role_opening = {
        "ai_application": "building practical AI systems",
        "general_software": "building maintainable software systems",
        "backend": "building maintainable backend and API systems",
        "test_development": "building reliable test and automation systems",
        "data_engineering": "using data, metrics and tooling to support better decisions",
        "robot_software": "building reliable robotics and perception software",
    }.get(role_family, "building practical software systems")
    paragraph_1 = (
        f"I am applying for the {position} role at {company} because I am interested in {role_opening} that move beyond experimentation and become reliable tools for real users. "
        + company_focus_sentence(job, job_signals)
    )
    paragraph_2 = {
        "ai_application": "My background combines applied AI, backend integration, reproducible workflows and evaluation-minded delivery.",
        "general_software": "My background combines backend engineering, testing, debugging and maintainable application structure.",
        "backend": "My background combines backend engineering, API integration, testing and maintainable application structure.",
        "test_development": "My background combines test automation, validation, debugging and reproducible engineering workflows.",
        "data_engineering": "My background combines data analysis, internal tooling, quality checks and communication around metrics and findings.",
        "robot_software": "My background combines robotics software, perception workflows, modular integration and testing-minded debugging.",
    }.get(role_family, "My background combines backend engineering, testing, debugging and maintainable application structure.")
    if compact:
        paragraph_2 += " Across my studies, internship and projects, I have built systems with reproducible workflows and clear communication."
    else:
        paragraph_2 += " Across my studies, internship experience and software projects, I have built systems that combine implementation detail with reproducible workflows and clear communication."
    project_phrase = ", ".join(selected_project_names[:2]) if selected_project_names else "my most relevant projects"
    project_alignment = {
        "ai_application": "AI workflow design, retrieval and reproducible application orchestration",
        "general_software": "backend structure, API-oriented implementation and testing-minded workflow design",
        "backend": "service structure, API-oriented implementation and testing-minded workflow design",
        "test_development": "automation, validation and evidence-driven debugging",
        "data_engineering": "data representation, evaluation logic and practical workflow support",
        "robot_software": "perception software, modular integration and system-focused debugging",
    }.get(role_family, "real delivery work")
    paragraph_3 = (
        f"My strongest evidence for this role comes from {project_phrase}. "
        f"In these projects, I worked across {project_alignment}, which maps well to roles that expect early-career engineers to contribute to real delivery work."
    )
    paragraph_4 = "During my internship, I built internal tooling and supported evaluation-focused delivery work."
    paragraph_4 += " That experience strengthened my ability to connect technical implementation with usability, metrics and day-to-day collaboration."
    if role_family == "data_engineering":
        paragraph_4 += " During my internship, I also built internal analysis tooling and supported metric-oriented reporting and review."
    elif role_family == "robot_software":
        paragraph_4 += " That work complemented my internship experience by reinforcing testing-minded engineering habits and system integration awareness."
    paragraph_5 = (
        f"I believe I would be a strong fit because I bring hands-on experience in Python, delivery-focused engineering work, documentation and collaborative problem-solving, and I am motivated to contribute as I continue growing in a strong team at {company}."
    )
    paragraphs = [paragraph_1, paragraph_2, paragraph_3, paragraph_4, paragraph_5]
    if compact:
        return [paragraph_1, paragraph_2, paragraph_3, paragraph_5]
    return paragraphs


def render_targeted_cover_letter(
    template: str,
    job: dict,
    role_family: str,
    job_signals: dict,
    selected_project_names: list[str],
    profile: dict[str, str],
    *,
    compact: bool = False,
) -> str:
    del template  # Keep the function signature stable while generating a fresh letter body.
    company = job.get("company", "Hiring Team")
    paragraphs = build_cover_letter_paragraphs(
        job,
        role_family,
        job_signals,
        selected_project_names,
        compact=compact,
    )
    rendered_paragraphs = "\n\n".join(latex_escape(paragraph) for paragraph in paragraphs)
    header_lines = [
        r"    {\Large \textbf{" + latex_escape(profile["full_name"]) + r"}}\\[2pt]"
    ]
    contact_line = profile_contact_latex(profile)
    if contact_line:
        header_lines.append("    " + contact_line)
    return "\n".join(
        [
            "% Generated tailored cover letter.",
            r"\documentclass[a4paper,11pt]{letter}",
            "",
            r"\usepackage[a4paper,top=0.7in,bottom=0.7in,left=0.8in,right=0.8in]{geometry}",
            r"\usepackage[hidelinks]{hyperref}",
            r"\usepackage{parskip}",
            "",
            r"\begin{document}",
            "",
            r"\begin{letter}{}",
            "",
            r"\begin{center}",
            *header_lines,
            r"\end{center}",
            "",
            r"\vspace{-0.1cm}",
            "",
            r"\opening{Dear " + latex_escape(company) + r" Hiring Team,}",
            "",
            rendered_paragraphs,
            "",
            r"\closing{Yours sincerely,\\[2pt] " + latex_escape(profile["closing_name"]) + r"}",
            "",
            r"\end{letter}",
            "",
            r"\end{document}",
        ]
    )


def compile_tex_to_pdf(tex_path: Path) -> str:
    latexmk = shutil.which("latexmk")
    local_tectonic = (
        Path(__file__).resolve().parents[1]
        / ".tools"
        / ("tectonic.exe" if os.name == "nt" else "tectonic")
    )
    tectonic = (
        os.environ.get("TECTONIC_BIN")
        or shutil.which("tectonic")
        or (str(local_tectonic) if local_tectonic.is_file() else "")
    )
    if not latexmk and not tectonic:
        raise FileNotFoundError(
            "No XeLaTeX-capable compiler found; install latexmk/XeLaTeX or set TECTONIC_BIN"
        )
    environment = os.environ.copy()
    # WSL does not automatically expose host fonts to Fontconfig.  The approved
    # Chinese bases use Noto CJK family names; when those are unavailable but
    # Windows CJK fonts are mounted, provide a project-local alias without
    # changing the immutable source TeX.
    windows_fonts = Path("/mnt/c/Windows/Fonts")
    if (
        os.name != "nt"
        and not environment.get("FONTCONFIG_FILE")
        and (windows_fonts / "msyh.ttc").is_file()
        and (windows_fonts / "simsun.ttc").is_file()
    ):
        fontconfig_dir = Path(__file__).resolve().parents[1] / ".tools" / "fontconfig"
        fontconfig_dir.mkdir(parents=True, exist_ok=True)
        fontconfig_path = fontconfig_dir / "fonts.conf"
        if not fontconfig_path.exists():
            fontconfig_path.write_text(
                """<?xml version=\"1.0\"?>
<!DOCTYPE fontconfig SYSTEM \"fonts.dtd\">
<fontconfig>
  <include ignore_missing=\"yes\">/etc/fonts/fonts.conf</include>
  <dir>/mnt/c/Windows/Fonts</dir>
  <cachedir>/tmp/track-job-font-cache</cachedir>
  <alias binding=\"strong\">
    <family>Noto Serif CJK SC</family>
    <prefer><family>SimSun</family></prefer>
  </alias>
  <alias binding=\"strong\">
    <family>Noto Sans CJK SC</family>
    <prefer><family>Microsoft YaHei</family></prefer>
  </alias>
</fontconfig>
""",
                encoding="utf-8",
            )
        environment["FONTCONFIG_FILE"] = str(fontconfig_path)
    compile_input = tex_path
    if windows_fonts.is_dir() and tex_path.is_file():
        source_text = tex_path.read_text(encoding="utf-8")
        substituted = source_text.replace("Noto Serif CJK SC", "SimSun").replace(
            "Noto Sans CJK SC", "Microsoft YaHei"
        )
        # The bundled XeTeX/ICU build on some WSL installations does not expose
        # the short ``zh`` locale. xeCJK still supplies CJK break handling; this
        # environment-only compile copy uses the available ICU locale.
        substituted = substituted.replace(
            r'\XeTeXlinebreaklocale "zh"',
            r'% WSL Tectonic ICU locale fallback; xeCJK retains CJK handling',
        )
        # The minimal bundled Tectonic cache used in WSL may not contain the
        # Computer Modern math TFM files. Keep the approved source unchanged,
        # but use the equivalent Unicode glyph in the environment-only compile
        # copy for the inline 2D-to-3D notation used by robotics bases.
        substituted = substituted.replace(r"$\rightarrow$", "→")
        if r"\usepackage{fontspec}" not in substituted and not re.search(
            r"[\u3400-\u4dbf\u4e00-\u9fff]", substituted
        ):
            substituted = re.sub(
                r"(\\documentclass(?:\[[^]]*\])?\{[^}]+\})",
                (
                    r"\1\n\\usepackage{fontspec}\n"
                    r"\\setmainfont{Times New Roman}\n"
                    r"\\DeclareMathSizes{9}{10}{7}{5}"
                ),
                substituted,
                count=1,
            )
        if substituted != source_text:
            compile_input = tex_path.with_name(f"{tex_path.stem}-font-fallback.tex")
            compile_input.write_text(substituted, encoding="utf-8")
    if latexmk:
        command = [
            latexmk, "-xelatex", "-interaction=nonstopmode", "-halt-on-error",
            compile_input.name,
        ]
    else:
        command = [str(tectonic), "--keep-logs", compile_input.name]
    subprocess.run(
        command,
        cwd=str(tex_path.parent),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
    )
    if compile_input != tex_path:
        compiled_pdf = compile_input.with_suffix(".pdf")
        compiled_log = compile_input.with_suffix(".log")
        if compiled_pdf.is_file():
            shutil.copyfile(compiled_pdf, tex_path.with_suffix(".pdf"))
        if compiled_log.is_file():
            shutil.copyfile(compiled_log, tex_path.with_suffix(".log"))
    return str(tex_path.with_suffix(".pdf"))


def load_resume_materials(materials_dir: str | Path = "cv/materials") -> dict[str, dict]:
    root = Path(materials_dir)
    return {
        name: load_config(root / name)
        for name in (
            "profile.yaml", "education.yaml", "experience.yaml", "projects.yaml",
            "skills.yaml", "evidence.yaml",
        )
    }


def load_cached_semantic_review(
    cache_dir: str | Path,
    job: dict,
) -> dict:
    """Find the newest existing review for this job without any provider call."""
    target = canonical_job_key(
        str(job.get("company", "")),
        str(job.get("title") or job.get("position") or ""),
        str(job.get("location", "")),
    )
    matches: list[tuple[str, dict]] = []
    target_identity = target.split("|")[:2]
    for path in Path(cache_dir).glob("*.json"):
        payload = read_json(path, {})
        cached = str(payload.get("input_job_canonical_key") or "")
        cached_key = cached.removeprefix("fallback:")
        if cached_key == target or cached_key.split("|")[:2] == target_identity:
            matches.append((str(payload.get("reviewed_at") or ""), payload))
    return max(matches, key=lambda item: item[0])[1] if matches else {}


def load_matching_resume_optimizer_edit(
    cache_dir: str | Path,
    job: dict,
) -> dict:
    """Load one existing edit payload only for an exact job identity."""
    expected = {
        "company": str(job.get("company") or ""),
        "title": str(job.get("title") or job.get("position") or ""),
        "location": str(job.get("location") or ""),
    }
    matches: list[dict] = []
    for path in Path(cache_dir).glob("*.json"):
        payload = read_json(path, {})
        identity = payload.get("job_identity") if isinstance(payload, dict) else None
        if isinstance(identity, dict) and all(
            str(identity.get(key) or "") == value for key, value in expected.items()
        ):
            matches.append(payload)
    if len(matches) > 1:
        raise ValueError(
            "Multiple resume-optimizer payloads match the same exact job identity"
        )
    return matches[0] if matches else {}


def generate_resume_v2_pair(
    job: dict,
    template: str | dict[str, str],
    materials: dict[str, dict],
    semantic_review: dict,
    output_stem: Path,
    *,
    languages: tuple[str, ...] = ("CN", "EN"),
    compile_pdf_output: bool = True,
    resume_optimizer_payload: dict | None = None,
    selected_bases: dict[str, dict] | None = None,
    base_content: dict | None = None,
    registry_defaults: dict | None = None,
) -> dict:
    """Generate requested languages from one fact plan; no model or external write."""
    base_plan_role = None
    base_role = ""
    if selected_bases:
        first_selection = selected_bases[languages[0]]
        base_plan_role = str(first_selection["plan_role"])
        base_role = str(first_selection["role_base"])
        if any(str(item["role_base"]) != base_role for item in selected_bases.values()):
            raise ValueError("CN and EN variants must share one logical role base")
    plan = build_resume_content_plan(
        job,
        materials,
        semantic_review,
        role_family=base_plan_role,
    )
    if selected_bases:
        if not base_content:
            raise ValueError("base-backed generation requires a content manifest")
        defaults = registry_defaults or {}
        plan = apply_base_content_manifest(
            plan,
            base_role,
            base_content,
            defaults.get("tailoring") or {},
        )
        plan["selected_base_id"] = selected_bases[languages[0]]["base_id"]
        plan["selected_base_ids"] = {
            language: selected_bases[language]["base_id"] for language in languages
        }
    if resume_optimizer_payload:
        plan = apply_resume_optimizer_edits(plan, resume_optimizer_payload, job=job)
    variants: dict[str, dict] = {}
    for language in languages:
        language_plan = copy.deepcopy(plan)
        language_template = template[language] if isinstance(template, dict) else template
        selection = (selected_bases or {}).get(language)
        if selection:
            language_plan["selected_base_id"] = selection["base_id"]
            language_plan["selected_base"] = copy.deepcopy(selection)
            fallback_contract = language_plan["page_fit_contracts"][language]
            language_plan["page_fit_contracts"][language] = base_page_fit_contract(
                selection,
                registry_defaults or {},
                fallback_contract,
            )
        tex_path = output_stem.with_name(f"{output_stem.name}-{language.lower()}.tex")
        if compile_pdf_output:
            variants[language] = fit_resume_pdf(
                language_template,
                language_plan,
                language,
                tex_path,
                compile_pdf=compile_tex_to_pdf,
            )
        else:
            tex_path.parent.mkdir(parents=True, exist_ok=True)
            tex_path.write_text(
                render_resume_from_plan(language_template, language_plan, language),
                encoding="utf-8",
            )
            variants[language] = {
                "status": "PAGE_FIT_REVIEW_REQUIRED",
                "language": language,
                "tex_path": str(tex_path),
                "pdf_path": "",
                "iterations": [],
                "metrics": {},
                "plan": language_plan,
            }
        if selection:
            result_plan = variants[language].get("plan", language_plan)
            report = build_tailoring_report(
                result_plan,
                selected_base=selection,
                semantic_review_cache_identity=semantic_cache_identity(semantic_review),
            )
            variants[language]["tailoring_report"] = report
            variants[language]["base_metadata"] = {
                "base_resume_id": selection["base_id"],
                "base_role": selection["role_base"],
                "language": selection["language"],
                "photo_variant": selection["photo_variant"],
                "template_version": selection["template_version"],
                "source_zip_hash": selection["source_sha256"],
                "fallback_used": bool(selection.get("fallback_used", False)),
            }
            best = variants[language].get("best_single_page_candidate") or {}
            gates = best.get("gates") or {}
            variants[language]["factual_validation_status"] = gates.get(
                "factual", "NOT_COMPILED"
            )
            if report["status"] != "PASS":
                variants[language]["status"] = "HUMAN_REVIEW_REQUIRED"
    if set(languages) == {"CN", "EN"}:
        variants["quality_gates"] = run_quality_gates(plan, variants["CN"], variants["EN"])
    return {"plan": plan, "variants": variants}


def render_and_compile_pair(
    job: dict,
    resume_template: str,
    cover_template: str,
    experiences_material: list[dict],
    projects_material: list[dict],
    skill_groups: dict[str, list[str]],
    profile: dict[str, str],
    resume_target: Path,
    cover_target: Path,
    education_material: list[dict] | None = None,
) -> tuple[str, str, list[str]]:
    role_family = infer_role_family(job)
    job_signals = extract_job_signals(job, role_family)
    attempts = [
        {"experience_bullet_limit": 4, "project_bullet_limit": 3, "max_projects": 3, "max_skills_per_group": 12, "compact_cover": False},
        {"experience_bullet_limit": 3, "project_bullet_limit": 3, "max_projects": 3, "max_skills_per_group": 10, "compact_cover": False},
        {"experience_bullet_limit": 3, "project_bullet_limit": 2, "max_projects": 3, "max_skills_per_group": 9, "compact_cover": True},
        {"experience_bullet_limit": 2, "project_bullet_limit": 2, "max_projects": 2, "max_skills_per_group": 8, "compact_cover": True},
    ]
    selected_projects: list[str] = []
    resume_pdf_path = ""
    cover_pdf_path = ""
    for attempt in attempts:
        tailored_resume, selected_projects, selected_project_names = render_targeted_resume(
            resume_template,
            job,
            experiences_material,
            projects_material,
            skill_groups,
            education_material=education_material,
            experience_bullet_limit=attempt["experience_bullet_limit"],
            project_bullet_limit=attempt["project_bullet_limit"],
            max_projects=attempt["max_projects"],
            max_skills_per_group=attempt["max_skills_per_group"],
        )
        tailored_cover = render_targeted_cover_letter(
            cover_template,
            job,
            role_family,
            job_signals,
            selected_project_names,
            profile,
            compact=attempt["compact_cover"],
        )
        resume_target.write_text(tailored_resume, encoding="utf-8")
        cover_target.write_text(tailored_cover, encoding="utf-8")
        resume_pdf_path = compile_tex_to_pdf(resume_target)
        cover_pdf_path = compile_tex_to_pdf(cover_target)
        resume_pages = count_pdf_pages(Path(resume_pdf_path))
        cover_pages = count_pdf_pages(Path(cover_pdf_path))
        if resume_pages <= 1 and cover_pages <= 1:
            return resume_pdf_path, cover_pdf_path, selected_projects
    return resume_pdf_path, cover_pdf_path, selected_projects


def eligible_for_document_generation(
    job: dict[str, Any], *, min_score: int, allowed_actions: set[str]
) -> bool:
    if int(job.get("matching_score") or 0) < min_score:
        return False
    if job.get("action") and job.get("action") not in allowed_actions:
        return False
    if job.get("action") in {"READY", "MUST_APPLY"} and not is_verified_open_job(job):
        return False
    return True


def main() -> None:
    args = parse_args()
    jobs = read_json(Path(args.jobs), [])

    output_dir = Path(args.output_dir)
    resume_dir = output_dir / "resumes"
    cover_dir = output_dir / "coverletters"
    resume_dir.mkdir(parents=True, exist_ok=True)
    cover_dir.mkdir(parents=True, exist_ok=True)

    resume_template = Path(args.resume_template).read_text(encoding="utf-8")
    cover_template = Path(args.coverletter_template).read_text(encoding="utf-8")
    resume_materials = load_resume_materials(args.materials_dir)
    repo_root = Path(__file__).resolve().parents[1]
    base_registry: dict = {}
    base_content: dict = {}
    if args.resume_version == "v2":
        base_registry = load_resume_base_registry(args.resume_bases, repo_root=repo_root)
        base_content = load_resume_base_content(args.resume_base_content, repo_root=repo_root)
        import_registered_bases(args.resume_bases, repo_root=repo_root)
    resume_optimizer_payload = (
        read_json(Path(args.resume_optimizer_edits), {}) if args.resume_optimizer_edits else None
    )
    education_material = resume_materials["education.yaml"].get("education", [])
    experiences_material = resume_materials["experience.yaml"].get("experience", [])
    projects_material = resume_materials["projects.yaml"].get("projects", [])
    skill_groups = resume_materials["skills.yaml"].get("skills", {})

    generated = []
    seen_keys: set[str] = set()
    allowed_actions = {item.strip() for item in args.actions.split(",") if item.strip()}
    for job in jobs:
        title = job.get("title") or job.get("position", "")
        job["position"] = title
        if not eligible_for_document_generation(
            job, min_score=args.min_score, allowed_actions=allowed_actions
        ):
            continue
        dedupe_key = canonical_job_key(job.get("company", ""), title, job.get("location", ""))
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        selected_profile_name, _ = identity_for_job(
            load_config(args.profile_config),
            job,
            manual_override=args.application_profile,
        )
        profile = load_profile(
            args.profile_config,
            job,
            application_profile=args.application_profile,
        )
        job_resume_template = apply_profile_placeholders(resume_template, profile)
        job_cover_template = apply_profile_placeholders(cover_template, profile)

        slug = slugify(f"{job.get('company', '')}-{title}")
        resume_target = resume_dir / f"{slug}.tex"
        cover_target = cover_dir / f"{slug}.tex"
        resume_pdf_path = ""
        cover_pdf_path = ""
        selected_projects: list[str] = []
        resume_variants: dict = {}
        job_optimizer_payload: dict = {}
        selected_bases: dict[str, dict] = {}
        generation_error = ""
        if args.resume_version == "v2":
            requested_language = str(
                args.resume_language
                or job.get("resume_language")
                or job.get("recommended_resume_language")
                or ("CN" if infer_job_language(job) == "ZH" else "EN")
            ).upper()
            languages = ("CN", "EN") if requested_language == "BOTH" else (requested_language,)
            try:
                requested_variant = str(
                    job.get("resume_variant") or args.resume_variant or "auto"
                )
                for language in languages:
                    selected_bases[language] = select_resume_base(
                        job,
                        base_registry,
                        language=language,
                        variant=requested_variant,
                        allow_english_photo_fallback=args.allow_english_photo_fallback,
                    )
                base_templates = {
                    language: apply_profile_placeholders(
                        prepare_selected_template(
                            selection,
                            resume_dir / f"{slug}-{language.lower()}",
                            repo_root=repo_root,
                        ),
                        profile,
                    )
                    for language, selection in selected_bases.items()
                }
                job_optimizer_payload = (
                    resume_optimizer_payload
                    or load_matching_resume_optimizer_edit(args.resume_optimizer_dir, job)
                )
                v2_result = generate_resume_v2_pair(
                    job,
                    base_templates,
                    resume_materials,
                    load_cached_semantic_review(args.semantic_review_cache, job),
                    resume_dir / slug,
                    languages=languages,
                    compile_pdf_output=args.compile_pdf,
                    resume_optimizer_payload=job_optimizer_payload,
                    selected_bases=selected_bases,
                    base_content=base_content,
                    registry_defaults=base_registry.get("defaults") or {},
                )
                resume_variants = v2_result["variants"]
                first = resume_variants[languages[0]]
                resume_target = Path(first["tex_path"])
                resume_pdf_path = str(first.get("pdf_path") or "")
                selected_projects = [
                    group["material"]["id"] for group in v2_result["plan"]["projects"]
                ]
                selected_project_names = [
                    str(group["material"].get("name") or "")
                    for group in v2_result["plan"]["projects"]
                ]
                tailored_cover = render_targeted_cover_letter(
                    job_cover_template,
                    job,
                    infer_role_family(job),
                    extract_job_signals(job, infer_role_family(job)),
                    selected_project_names,
                    profile,
                )
                cover_target.write_text(tailored_cover, encoding="utf-8")
                if args.compile_pdf:
                    cover_pdf_path = compile_tex_to_pdf(cover_target)
            except (subprocess.CalledProcessError, FileNotFoundError, ValueError) as exc:
                output = getattr(exc, "stdout", "")
                generation_error = str(output or exc)
                if args.legacy_resume_fallback and not selected_bases:
                    print(f"Golden Base unavailable for {slug}; explicit legacy fallback requested: {generation_error}")
                    legacy_result = generate_resume_v2_pair(
                        job,
                        job_resume_template,
                        resume_materials,
                        load_cached_semantic_review(args.semantic_review_cache, job),
                        resume_dir / slug,
                        languages=languages,
                        compile_pdf_output=args.compile_pdf,
                        resume_optimizer_payload=job_optimizer_payload,
                    )
                    resume_variants = legacy_result["variants"]
                    first = resume_variants[languages[0]]
                    resume_target = Path(first["tex_path"])
                    resume_pdf_path = str(first.get("pdf_path") or "")
                else:
                    print(f"Resume V2 warning for {slug}: {generation_error}")
        elif args.compile_pdf:
            try:
                resume_pdf_path, cover_pdf_path, selected_projects = render_and_compile_pair(
                    job,
                    job_resume_template,
                    job_cover_template,
                    experiences_material,
                    projects_material,
                    skill_groups,
                    profile,
                    resume_target,
                    cover_target,
                    education_material=education_material,
                )
            except subprocess.CalledProcessError as exc:
                print(f"PDF compile warning for {slug}: {exc.stdout}")
        else:
            tailored_resume, selected_projects, selected_project_names = render_targeted_resume(
                job_resume_template,
                job,
                experiences_material,
                projects_material,
                skill_groups,
                education_material=education_material,
            )
            tailored_cover = render_targeted_cover_letter(
                job_cover_template,
                job,
                infer_role_family(job),
                extract_job_signals(job, infer_role_family(job)),
                selected_project_names,
                profile,
            )
            resume_target.write_text(tailored_resume, encoding="utf-8")
            cover_target.write_text(tailored_cover, encoding="utf-8")

        primary_selection = next(iter(selected_bases.values()), {})
        primary_variant = next(
            (
                value
                for language, value in resume_variants.items()
                if language in {"CN", "EN"} and isinstance(value, dict)
            ),
            {},
        )
        primary_tailoring = primary_variant.get("tailoring_report") or {}
        generated.append(
            {
                "company": job.get("company", ""),
                "position": title,
                "title": title,
                "resume_family": job.get("resume_family", infer_role_family(job)),
                "resume_path": str(resume_target),
                "coverletter_path": str(cover_target),
                "resume_pdf_path": resume_pdf_path,
                "coverletter_pdf_path": cover_pdf_path,
                "matching_score": job.get("matching_score", 0),
                "selected_projects": selected_projects,
                "application_profile": selected_profile_name,
                "resume_version": args.resume_version,
                "resume_optimizer_applied": bool(job_optimizer_payload),
                "generation_error": generation_error,
                "base_resume_ids": {
                    language: selection["base_id"]
                    for language, selection in selected_bases.items()
                },
                "base_resume_id": primary_selection.get("base_id", "legacy"),
                "base_role": primary_selection.get("role_base", ""),
                "resume_language": primary_selection.get("language", ""),
                "resume_variant": primary_selection.get("photo_variant", ""),
                "template_version": primary_selection.get("template_version", ""),
                "source_zip_hash": primary_selection.get("source_sha256", ""),
                "tailoring_percentage": primary_tailoring.get("tailoring_percentage"),
                "page_fit_status": primary_variant.get("status", "NOT_GENERATED"),
                "factual_validation_status": primary_variant.get(
                    "factual_validation_status", "NOT_RUN"
                ),
                "resume_variants": {
                    language: {
                        key: value
                        for key, value in result.items()
                        if key not in {"plan", "rendered_text"}
                    }
                    for language, result in resume_variants.items()
                    if language in {"CN", "EN"}
                },
            }
        )

    manifest_path = output_dir / "generated_manifest.json"
    manifest_path.write_text(json.dumps(generated, indent=2), encoding="utf-8")
    print(f"Wrote {len(generated)} generated draft records to {manifest_path}")


if __name__ == "__main__":
    main()
