#!/usr/bin/env python3
"""Generate targeted resume and cover-letter variants from LaTeX materials."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

try:
    from common import canonical_job_key, load_config, normalize_token, normalize_whitespace, read_json, slugify
except ModuleNotFoundError:
    from scripts.common import canonical_job_key, load_config, normalize_token, normalize_whitespace, read_json, slugify


ROLE_KEYWORDS = {
    "ai_engineer": ("ai", "machine learning", "llm", "nlp", "rag", "artificial intelligence", "prompt"),
    "software_engineer": ("software", "backend", "api", "python", "django", "fastapi", "testing"),
    "data_scientist": ("data", "analytics", "sql", "scientist", "metrics", "dashboard"),
    "robotics_engineer": ("robotics", "ros2", "vision", "perception", "autonomous", "sensor"),
}

ROLE_DISPLAY = {
    "ai_engineer": "AI / ML",
    "software_engineer": "Programming / Interfaces",
    "data_scientist": "Evaluation / Delivery",
    "robotics_engineer": "Engineering Tools",
}

SAFE_FOCUS_PHRASES = {
    "production": "with a focus on reliable delivery",
    "testing": "with an emphasis on testing and debugging",
    "stakeholder": "to support stakeholder-facing decisions",
    "scale": "with attention to maintainability and scale",
    "workflow": "to support reproducible engineering workflows",
}

DEFAULT_PROFILE = {
    "full_name": "Your Name",
    "email": "you@example.com",
    "phone": "+44 0000 000000",
    "linkedin_url": "https://www.linkedin.com/in/your-profile",
    "portfolio_url": "https://your-portfolio.example.com",
    "closing_name": "Your Name",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate targeted resume variants.")
    parser.add_argument("--jobs", default="data/job_cache/scored_jobs.json")
    parser.add_argument("--resume-template", default="cv/resume.tex")
    parser.add_argument("--coverletter-template", default="cv/coverletter.tex")
    parser.add_argument("--output-dir", default="cv/generated")
    parser.add_argument("--profile-config", default="config/profile.yaml")
    parser.add_argument("--min-score", type=int, default=60)
    parser.add_argument("--compile-pdf", action="store_true", default=True)
    return parser.parse_args()


def load_profile(path: str) -> dict[str, str]:
    profile = dict(DEFAULT_PROFILE)
    config_path = Path(path)
    if not config_path.exists():
        return profile
    raw = load_config(config_path)
    identity = raw.get("identity", {})
    for key, value in profile.items():
        loaded = identity.get(key)
        if loaded:
            profile[key] = str(loaded)
    return profile


def apply_profile_placeholders(template: str, profile: dict[str, str]) -> str:
    replacements = {
        "{{FULL_NAME}}": profile["full_name"],
        "{{EMAIL}}": profile["email"],
        "{{PHONE}}": profile["phone"],
        "{{LINKEDIN_URL}}": profile["linkedin_url"],
        "{{PORTFOLIO_URL}}": profile["portfolio_url"],
        "{{CLOSING_NAME}}": profile["closing_name"],
    }
    updated = template
    for source, target in replacements.items():
        updated = updated.replace(source, target)
    return updated


def latex_escape(text: str) -> str:
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


def infer_role_family(job: dict) -> str:
    title = normalize_token(job.get("position", ""))
    body = normalize_token(f"{job.get('position', '')} {job.get('description', '')}")
    scores = {}
    for role, keywords in ROLE_KEYWORDS.items():
        score = 0
        for keyword in keywords:
            if keyword in body:
                score += 1
            if keyword in title:
                score += 2
        scores[role] = score
    if any(token in title for token in ("ai", "machine learning", "artificial intelligence", "llm", "nlp")):
        scores["ai_engineer"] += 6
    if any(token in title for token in ("robotics", "ros2", "perception", "vision")):
        scores["robotics_engineer"] += 6
    return max(scores, key=scores.get) if max(scores.values()) > 0 else "software_engineer"


def extract_job_signals(job: dict, role_family: str) -> dict[str, list[str] | str]:
    body = normalize_token(f"{job.get('position', '')} {job.get('description', '')}")
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


def item_score(job_signals: dict, item: dict, role_family: str) -> int:
    score = 0
    body = str(job_signals["body"])
    for tag in item.get("tags", []):
        if normalize_token(str(tag)) in body:
            score += 5
    if role_family in item.get("role_targets", []):
        score += 8
    for bullet in collect_candidate_bullets(item, role_family):
        normalized_bullet = normalize_token(str(bullet))
        for keyword in job_signals.get("matched_keywords", []):
            if keyword in normalized_bullet and keyword in body:
                score += 2
    return score


def collect_candidate_bullets(item: dict, role_family: str) -> list[str]:
    variants = item.get("bullet_variants", {})
    role_specific = list(variants.get(role_family, []))
    base = list(item.get("bullets", []))
    return role_specific + base


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


def select_ranked_bullets(job_signals: dict, item: dict, limit: int) -> list[str]:
    role_family = str(item.get("active_role_family", ""))
    candidates = collect_candidate_bullets(item, role_family)
    ranked = sorted(candidates, key=lambda bullet: bullet_score(job_signals, str(bullet)), reverse=True)
    chosen = ranked[:limit] if ranked else candidates[:limit]
    return [rewrite_bullet_for_job(str(bullet), job_signals) for bullet in chosen]


def count_pdf_pages(pdf_path: Path) -> int:
    result = subprocess.run(
        ["pdfinfo", str(pdf_path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    match = re.search(r"Pages:\s+(\d+)", result.stdout)
    return int(match.group(1)) if match else 0


def build_experience_section(experiences: list[dict]) -> str:
    lines = [r"\section{Experience}", r"\resumeSubHeadingListStart", ""]
    for experience in experiences:
        lines.extend(
            [
                r"  \resumeSubheading",
                f"    {{{latex_escape(experience.get('company', ''))}}}{{{latex_escape(experience.get('location', ''))}}}",
                f"    {{{latex_escape(experience.get('title', ''))}}}{{{latex_escape(experience.get('date_range', ''))}}}",
                r"    \resumeItemListStart",
            ]
        )
        for bullet in experience.get("selected_bullets", []):
            lines.append(f"      \\item \\small{{{latex_escape(bullet)}}}")
        lines.extend([r"    \resumeItemListEnd", ""])
    lines.append(r"\resumeSubHeadingListEnd")
    return "\n".join(lines)


def build_project_section(projects: list[dict]) -> str:
    lines = [r"\section{Projects}", r"\resumeSubHeadingListStart", ""]
    for project in projects:
        repo_url = project.get("repo_url", "")
        name = project.get("name", "")
        if repo_url:
            title = r"\href{" + repo_url + "}{" + latex_escape(name) + "}"
        else:
            title = latex_escape(name)
        lines.extend(
            [
                r"  \resumeSubheading",
                f"    {{{title}}}{{}}",
                f"    {{{latex_escape(project.get('display_role', 'Project'))}}}{{{latex_escape(project.get('date_range', ''))}}}",
                r"    \resumeItemListStart",
            ]
        )
        for bullet in project.get("selected_bullets", []):
            lines.append(f"      \\item \\small{{{latex_escape(bullet)}}}")
        lines.extend([r"    \resumeItemListEnd", ""])
    lines.append(r"\resumeSubHeadingListEnd")
    return "\n".join(lines)

def build_skills_section(
    skill_groups: dict[str, list[str]],
    role_family: str,
    job_signals: dict,
    max_skills_per_group: int = 12,
) -> str:
    body = str(job_signals["body"])
    preferred_order = [role_family] + [key for key in skill_groups.keys() if key != role_family]
    rendered_rows = []
    for group_name in preferred_order:
        skills = list(skill_groups.get(group_name, []))
        skills.sort(key=lambda item: (0 if normalize_token(item) in body else 1, item.lower()))
        if skills:
            rendered_rows.append(
                f"    \\textbf{{{ROLE_DISPLAY.get(group_name, group_name)}}}{{: {latex_escape(', '.join(skills[:max_skills_per_group]))}}} \\\\"
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
    experience_bullet_limit: int = 4,
    project_bullet_limit: int = 3,
    max_projects: int = 3,
    max_skills_per_group: int = 12,
) -> tuple[str, list[str], list[str]]:
    role_family = infer_role_family(job)
    job_signals = extract_job_signals(job, role_family)

    ranked_experiences = []
    for experience in experiences_material:
        item = dict(experience)
        item["active_role_family"] = role_family
        item["score"] = item_score(job_signals, item, role_family)
        item["selected_bullets"] = select_ranked_bullets(job_signals, item, experience_bullet_limit)
        ranked_experiences.append(item)
    ranked_experiences.sort(key=lambda item: item.get("score", 0), reverse=True)
    selected_experiences = ranked_experiences[:1]

    ranked_projects = []
    for project in projects_material:
        item = dict(project)
        item["active_role_family"] = role_family
        item["score"] = item_score(job_signals, item, role_family)
        item["selected_bullets"] = select_ranked_bullets(job_signals, item, project_bullet_limit)
        ranked_projects.append(item)
    ranked_projects.sort(key=lambda item: item.get("score", 0), reverse=True)
    selected_projects = ranked_projects[:max_projects]
    selected_ids = [item.get("id", "") for item in selected_projects]
    selected_names = [item.get("name", "") for item in selected_projects]

    experience_section = build_experience_section(selected_experiences)
    project_section = build_project_section(selected_projects)
    skills_section = build_skills_section(skill_groups, role_family, job_signals, max_skills_per_group=max_skills_per_group)

    updated = replace_section(template, "Experience", experience_section, "Projects")
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
        f"% Selected projects: {', '.join(selected_ids)}\n"
    )
    return header + updated, selected_ids, selected_names


def company_focus_sentence(job: dict, job_signals: dict, snippets: dict[str, list[str]]) -> str:
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
    mission_snippets = snippets.get("team_mission", [])
    if mission_snippets:
        return mission_snippets[0]
    return "What attracts me most is the chance to build practical software and AI systems that are useful for real users."


def build_cover_letter_paragraphs(
    job: dict,
    role_family: str,
    job_signals: dict,
    selected_project_names: list[str],
    snippets: dict[str, list[str]],
    *,
    compact: bool = False,
) -> list[str]:
    company = job.get("company", "the company")
    position = job.get("position", "the role")
    role_opening = {
        "ai_engineer": "building practical AI systems",
        "software_engineer": "building maintainable software systems",
        "data_scientist": "using data, metrics and tooling to support better decisions",
        "robotics_engineer": "building reliable robotics and perception software",
    }.get(role_family, "building practical software systems")
    paragraph_1 = (
        f"I am applying for the {position} role at {company} because I am interested in {role_opening} that move beyond experimentation and become reliable tools for real users. "
        + company_focus_sentence(job, job_signals, snippets)
    )
    paragraph_2 = {
        "ai_engineer": "My background combines applied AI, backend integration, reproducible workflows and evaluation-minded delivery.",
        "software_engineer": "My background combines backend engineering, testing, debugging and maintainable application structure.",
        "data_scientist": "My background combines data analysis, internal tooling, quality checks and communication around metrics and findings.",
        "robotics_engineer": "My background combines robotics software, perception workflows, modular integration and testing-minded debugging.",
    }.get(role_family, "My background combines backend engineering, testing, debugging and maintainable application structure.")
    if compact:
        paragraph_2 += " Across my studies, internship and projects, I have built systems with reproducible workflows and clear communication."
    else:
        paragraph_2 += " Across my studies, internship experience and software projects, I have built systems that combine implementation detail with reproducible workflows and clear communication."
    project_phrase = ", ".join(selected_project_names[:2]) if selected_project_names else "my most relevant projects"
    project_alignment = {
        "ai_engineer": "AI workflow design, retrieval and reproducible application orchestration",
        "software_engineer": "backend structure, API-oriented implementation and testing-minded workflow design",
        "data_scientist": "data representation, evaluation logic and practical workflow support",
        "robotics_engineer": "perception software, modular integration and system-focused debugging",
    }.get(role_family, "real delivery work")
    paragraph_3 = (
        f"My strongest evidence for this role comes from {project_phrase}. "
        f"In these projects, I worked across {project_alignment}, which maps well to roles that expect early-career engineers to contribute to real delivery work."
    )
    paragraph_4 = snippets.get("designlibro", ["During my internship, I built internal tooling and supported evaluation-focused delivery work."])[0]
    paragraph_4 += " That experience strengthened my ability to connect technical implementation with usability, metrics and day-to-day collaboration."
    if role_family == "data_scientist":
        paragraph_4 = snippets.get("data_delivery", [paragraph_4])[0] + " During my internship, I also built internal analysis tooling and supported metric-oriented reporting and review."
    elif role_family == "robotics_engineer":
        paragraph_4 = snippets.get("robotics", [paragraph_4])[0] + " That work complemented my internship experience by reinforcing testing-minded engineering habits and system integration awareness."
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
    snippets: dict[str, list[str]],
    profile: dict[str, str],
    *,
    compact: bool = False,
) -> str:
    del template  # Keep the function signature stable while generating a fresh letter body.
    company = job.get("company", "Hiring Team")
    paragraphs = build_cover_letter_paragraphs(job, role_family, job_signals, selected_project_names, snippets, compact=compact)
    rendered_paragraphs = "\n\n".join(latex_escape(paragraph) for paragraph in paragraphs)
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
            r"    {\Large \textbf{" + latex_escape(profile["full_name"]) + r"}}\\[2pt]",
            r"    \href{mailto:" + latex_escape(profile["email"]) + r"}{" + latex_escape(profile["email"]) + r"}",
            r"    \;|\;",
            latex_escape(profile["phone"]),
            r"    \;|\;",
            r"    \href{" + latex_escape(profile["linkedin_url"]) + r"}{LinkedIn}",
            r"    \;|\;",
            r"    \href{" + latex_escape(profile["portfolio_url"]) + r"}{Portfolio}",
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
    command = [
        "latexmk",
        "-pdf",
        "-interaction=nonstopmode",
        "-halt-on-error",
        tex_path.name,
    ]
    subprocess.run(
        command,
        cwd=str(tex_path.parent),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return str(tex_path.with_suffix(".pdf"))


def render_and_compile_pair(
    job: dict,
    resume_template: str,
    cover_template: str,
    experiences_material: list[dict],
    projects_material: list[dict],
    skill_groups: dict[str, list[str]],
    coverletter_snippets: dict[str, list[str]],
    profile: dict[str, str],
    resume_target: Path,
    cover_target: Path,
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
            coverletter_snippets,
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
    profile = load_profile(args.profile_config)
    resume_template = apply_profile_placeholders(resume_template, profile)
    cover_template = apply_profile_placeholders(cover_template, profile)
    experiences_material = load_config("cv/materials/experience.yaml").get("experience", [])
    projects_material = load_config("cv/materials/projects.yaml").get("projects", [])
    skill_groups = load_config("cv/materials/skills.yaml").get("skills", {})
    coverletter_snippets = load_config("cv/materials/coverletter_snippets.yaml").get("snippets", {})

    generated = []
    seen_keys: set[str] = set()
    for job in jobs:
        if job.get("matching_score", 0) < args.min_score:
            continue
        dedupe_key = canonical_job_key(job.get("company", ""), job.get("position", ""))
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        slug = slugify(f"{job.get('company', '')}-{job.get('position', '')}")
        resume_target = resume_dir / f"{slug}.tex"
        cover_target = cover_dir / f"{slug}.tex"
        resume_pdf_path = ""
        cover_pdf_path = ""
        selected_projects: list[str] = []
        if args.compile_pdf:
            try:
                resume_pdf_path, cover_pdf_path, selected_projects = render_and_compile_pair(
                    job,
                    resume_template,
                    cover_template,
                    experiences_material,
                    projects_material,
                    skill_groups,
                    coverletter_snippets,
                    profile,
                    resume_target,
                    cover_target,
                )
            except subprocess.CalledProcessError as exc:
                print(f"PDF compile warning for {slug}: {exc.stdout}")
        else:
            tailored_resume, selected_projects, selected_project_names = render_targeted_resume(
                resume_template,
                job,
                experiences_material,
                projects_material,
                skill_groups,
            )
            tailored_cover = render_targeted_cover_letter(
                cover_template,
                job,
                infer_role_family(job),
                extract_job_signals(job, infer_role_family(job)),
                selected_project_names,
                coverletter_snippets,
                profile,
            )
            resume_target.write_text(tailored_resume, encoding="utf-8")
            cover_target.write_text(tailored_cover, encoding="utf-8")

        generated.append(
            {
                "company": job.get("company", ""),
                "position": job.get("position", ""),
                "resume_path": str(resume_target),
                "coverletter_path": str(cover_target),
                "resume_pdf_path": resume_pdf_path,
                "coverletter_pdf_path": cover_pdf_path,
                "matching_score": job.get("matching_score", 0),
                "selected_projects": selected_projects,
            }
        )

    manifest_path = output_dir / "generated_manifest.json"
    manifest_path.write_text(json.dumps(generated, indent=2), encoding="utf-8")
    print(f"Wrote {len(generated)} generated draft records to {manifest_path}")


if __name__ == "__main__":
    main()
