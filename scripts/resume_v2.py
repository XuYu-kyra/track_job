#!/usr/bin/env python3
"""Bounded role-base resume composition and bilingual PDF page fitting.

This module is deliberately downstream of candidate materials and semantic
review.  It never calls a model and never creates candidate facts: semantic
evidence rankings affect ordering and page value only.
"""

from __future__ import annotations

import copy
import ctypes
import importlib
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

try:
    from common import normalize_token
except ModuleNotFoundError:
    from scripts.common import normalize_token


CANONICAL_BLUEPRINT_ROLES = frozenset(
    {"test_development", "ai_application", "robot_software", "general_software", "backend"}
)
RELEVANCE_WEIGHT = {"HIGH": 5.0, "MEDIUM": 3.0, "LOW": 1.0}
SOURCE_STRENGTH = {"VERIFIED": 3.0, "USER_ATTESTED": 3.0, "DERIVED_RESUME_SAFE": 2.5}
CN_BANNED = (
    "深度参与", "全面负责", "显著提升", "有效提升", "赋能", "助力", "积极协作",
    "具备良好的", "深入理解", "成功实现", "前沿技术", "高效完成", "高效地",
    "极大提升", "大幅优化",
)
EN_BANNED = (
    "leveraged cutting-edge", "spearheaded", "revolutionized",
    "significantly enhanced", "highly scalable", "robust solution",
)
INTERNAL_RENDER_BANNED = (
    "source_class:", "confidence:", "provenance:", "public repository does not prove",
)
SKILL_LABELS_CN = {
    "ai_application": "AI / 大模型应用",
    "backend": "后端 / API",
    "general_software": "编程 / 工程基础",
    "test_development": "测试 / 自动化",
    "robot_software": "机器人软件",
    "reliability": "可靠性工程",
    "engineering_tools": "工程工具",
    "software_automation": "软件自动化",
    "data_engineering": "数据分析",
}


def validate_resume_blueprints(profile_material: dict[str, Any]) -> dict[str, Any]:
    blueprints = profile_material.get("resume_blueprints")
    if not isinstance(blueprints, dict):
        raise ValueError("profile.yaml.resume_blueprints must be an object")
    roles = blueprints.get("roles")
    if not isinstance(roles, dict):
        raise ValueError("profile.yaml.resume_blueprints.roles must be an object")
    missing = sorted(CANONICAL_BLUEPRINT_ROLES - set(roles))
    if missing:
        raise ValueError(f"resume blueprints missing canonical roles: {', '.join(missing)}")
    required_sections = {"education", "experience", "projects", "skills"}
    for role in sorted(CANONICAL_BLUEPRINT_ROLES):
        blueprint = roles[role]
        sections = set(blueprint.get("mandatory_sections", []))
        if sections != required_sections:
            raise ValueError(f"resume blueprint {role} must require all base sections")
        if not blueprint.get("preferred_project_order"):
            raise ValueError(f"resume blueprint {role} needs preferred_project_order")
        if not blueprint.get("preferred_evidence_families"):
            raise ValueError(f"resume blueprint {role} needs preferred_evidence_families")
        if not blueprint.get("skill_priorities"):
            raise ValueError(f"resume blueprint {role} needs skill_priorities")
        for section in ("experience_bullets", "project_bullets", "skill_rows"):
            limits = blueprint.get(section, {})
            values = [int(limits.get(key, -1)) for key in ("min", "target", "max")]
            if not (0 <= values[0] <= values[1] <= values[2]):
                raise ValueError(f"resume blueprint {role}.{section} has invalid min/target/max")
    page_fit = blueprints.get("page_fit", {})
    if int(page_fit.get("max_iterations", 0)) != 3:
        raise ValueError("resume page-fit max_iterations must be exactly 3")
    for language in ("CN", "EN"):
        contract = page_fit.get(language, {})
        if float(contract.get("min_vertical_span_ratio", 0)) <= 0:
            raise ValueError(f"resume page-fit {language} contract is missing")
    return blueprints


def _tokens(value: Any) -> set[str]:
    normalized = normalize_token(str(value or ""))
    return {token for token in normalized.split() if len(token) > 1}


def _evidence_overlap(text: str, evidence: dict[str, Any]) -> int:
    corpus = " ".join(
        [
            str(evidence.get("factual_description", "")),
            " ".join(str(value) for value in evidence.get("technologies", [])),
            " ".join(str(value) for value in evidence.get("verified_metrics", [])),
        ]
    )
    return len(_tokens(text) & _tokens(corpus))


def _intent_evidence_ids(
    english_fact: str,
    material: dict[str, Any],
    evidence_index: dict[str, dict[str, Any]],
    semantic: dict[str, dict[str, Any]],
) -> list[str]:
    candidates = [
        str(value)
        for value in material.get("source_evidence_ids", material.get("evidence_ids", []))
        if str(value) in evidence_index
    ]
    ranked = sorted(
        (
            (
                _evidence_overlap(english_fact, evidence_index[evidence_id])
                + RELEVANCE_WEIGHT.get(str(semantic.get(evidence_id, {}).get("relevance", "")), 0.0),
                evidence_id,
            )
            for evidence_id in candidates
        ),
        key=lambda item: (-item[0], candidates.index(item[1])),
    )
    maximum = ranked[0][0] if ranked else 0
    positive = [evidence_id for score, evidence_id in ranked if score > 0 and score >= maximum - 1]
    return positive[:3] or candidates[:2]


def _semantic_ranking(review: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    result = (review or {}).get("result", review or {})
    return {
        str(item.get("evidence_id")): item
        for item in result.get("candidate_evidence_ranking", [])
        if isinstance(item, dict) and item.get("evidence_id")
    }


def _metric_tokens(text: str) -> list[str]:
    patterns = (
        r"\b\d+(?:\.\d+)?%\b", r"\b\d+/\d+\b", r"\b\d+(?:\.\d+)?\s*(?:s|seconds?|runs?|worlds?|episodes?)\b",
    )
    found: list[str] = []
    for pattern in patterns:
        found.extend(re.findall(pattern, text, flags=re.IGNORECASE))
    return found


def _compact(text: str, language: str) -> str:
    """Conservatively compress a claim without adding or changing facts."""
    if language == "CN":
        replacements = (("用于后续", "供后续"), ("并通过", "，通过"), ("以及", "与"))
    else:
        replacements = ((" in order to ", " to "), (" as well as ", " and "), ("downstream ", ""))
    compact = text
    for source, target in replacements:
        compact = compact.replace(source, target)
    return compact


def _page_value(
    *,
    fact: str,
    evidence_ids: list[str],
    evidence_index: dict[str, dict[str, Any]],
    semantic: dict[str, dict[str, Any]],
    job_text: str,
    preferred_families: list[str],
    formal_experience: bool,
    breadth: bool,
    duplication_penalty: float,
) -> tuple[float, float, float]:
    evidence_relevance = max(
        (RELEVANCE_WEIGHT.get(str(semantic.get(evidence_id, {}).get("relevance", "")), 0.0) for evidence_id in evidence_ids),
        default=0.0,
    )
    jd_importance = min(5.0, float(len(_tokens(fact) & _tokens(job_text))))
    family_bonus = 1.5 if any(
        any(evidence_id.startswith(prefix) for prefix in preferred_families)
        for evidence_id in evidence_ids
    ) else 0.0
    evidence_strength = max(
        (SOURCE_STRENGTH.get(str(evidence_index.get(evidence_id, {}).get("source_class", "")), 0.0) for evidence_id in evidence_ids),
        default=0.0,
    )
    specificity = min(2.0, len(_metric_tokens(fact)) + sum(char.isdigit() for char in fact) * 0.1)
    page_cost = max(1.0, len(fact) / 130.0)
    value = (
        2.0 * jd_importance + 2.2 * evidence_relevance + family_bonus + evidence_strength
        + specificity + (2.0 if formal_experience else 0.0) + (1.5 if breadth else 0.0)
        - duplication_penalty - page_cost
    )
    return round(value, 3), jd_importance, evidence_relevance


def _build_bullet_intents(
    material: dict[str, Any],
    *,
    section: str,
    semantic: dict[str, dict[str, Any]],
    evidence_index: dict[str, dict[str, Any]],
    job_text: str,
    preferred_families: list[str],
    breadth: bool = False,
) -> list[dict[str, Any]]:
    english = list(material.get("bullets", []))
    chinese = list(material.get("bullets_cn", []))
    expanded_english = list(material.get("bullets_expanded", []))
    expanded_chinese = list(material.get("bullets_expanded_cn", []))
    intents: list[dict[str, Any]] = []
    seen_tokens: set[str] = set()
    for index, english_fact in enumerate(english):
        cn_fact = chinese[index] if index < len(chinese) else english_fact
        en_expanded = expanded_english[index] if index < len(expanded_english) else english_fact
        cn_expanded = expanded_chinese[index] if index < len(expanded_chinese) else cn_fact
        evidence_ids = _intent_evidence_ids(str(english_fact), material, evidence_index, semantic)
        fact_tokens = _tokens(english_fact)
        overlap = len(fact_tokens & seen_tokens) / max(1, len(fact_tokens))
        page_value, jd_relevance, semantic_relevance = _page_value(
            fact=str(english_fact),
            evidence_ids=evidence_ids,
            evidence_index=evidence_index,
            semantic=semantic,
            job_text=job_text,
            preferred_families=preferred_families,
            formal_experience=section == "experience",
            breadth=breadth,
            duplication_penalty=overlap * 3.0,
        )
        seen_tokens.update(fact_tokens)
        intent_id = f"{material['id']}_bullet_{index + 1}"
        intents.append(
            {
                "intent_id": intent_id,
                "material_id": material["id"],
                "selected_material": material["id"],
                "resume_role": section,
                "priority": index + 1,
                "evidence_ids": evidence_ids,
                "factual_scope": str(english_fact),
                "metrics": _metric_tokens(str(english_fact)),
                "jd_keyword_targets": sorted(_tokens(english_fact) & _tokens(job_text)),
                "jd_relevance": jd_relevance,
                "semantic_relevance": semantic_relevance,
                "duplication_penalty": round(overlap, 3),
                "page_value": page_value,
                "optional": False,
                "reserve": False,
                "variants": {
                    "CN": {"compact": _compact(str(cn_fact), "CN"), "standard": str(cn_fact), "expanded": str(cn_expanded)},
                    "EN": {"compact": _compact(str(english_fact), "EN"), "standard": str(english_fact), "expanded": str(en_expanded)},
                },
                "variant": "expanded",
            }
        )
    represented = {evidence_id for intent in intents for evidence_id in intent["evidence_ids"]}
    for evidence_id in material.get("source_evidence_ids", material.get("evidence_ids", [])):
        evidence_id = str(evidence_id)
        if (
            evidence_id in represented
            or evidence_id not in evidence_index
            or str(semantic.get(evidence_id, {}).get("relevance", "")).upper() != "HIGH"
            or not intents
        ):
            continue
        best = max(
            intents,
            key=lambda intent: _evidence_overlap(intent["factual_scope"], evidence_index[evidence_id]),
        )
        if _evidence_overlap(best["factual_scope"], evidence_index[evidence_id]) > 0:
            if len(best["evidence_ids"]) >= 3:
                best["evidence_ids"].pop()
            best["evidence_ids"].append(evidence_id)
            represented.add(evidence_id)
    return intents


def _ordered_materials(items: list[dict[str, Any]], ids: list[str]) -> list[dict[str, Any]]:
    index = {str(item.get("id")): item for item in items if isinstance(item, dict)}
    return [index[item_id] for item_id in ids if item_id in index]


def _skill_rows(
    skill_groups: dict[str, list[Any]],
    priorities: list[str],
    job_text: str,
    target: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    used_skills: set[str] = set()
    for group in priorities:
        allowed = [
            item for item in skill_groups.get(group, [])
            if isinstance(item, dict)
            and item.get("name")
            and normalize_token(item["name"]) not in used_skills
        ]
        allowed.sort(
            key=lambda item: (
                0 if normalize_token(item["name"]) in normalize_token(job_text) else 1,
                {"strong": 0, "working": 1, "exposure": 2}.get(str(item.get("proficiency")), 3),
            )
        )
        if not allowed:
            continue
        selected = allowed[:10]
        used_skills.update(normalize_token(item["name"]) for item in selected)
        rows.append(
            {
                "intent_id": f"skills_{group}",
                "resume_role": "skills",
                "material_id": "skills.yaml",
                "selected_material": group,
                "priority": len(rows) + 1,
                "evidence_ids": sorted({eid for item in selected for eid in item.get("evidence_ids", [])}),
                "group": group,
                "skills": [str(item["name"]) for item in selected],
                "optional": len(rows) >= 3,
                "reserve": False,
                "page_value": float(20 - len(rows)),
                "jd_relevance": sum(1 for item in selected if normalize_token(item["name"]) in normalize_token(job_text)),
                "duplication_penalty": 0.0,
            }
        )
        if len(rows) >= target:
            break
    return rows


def build_resume_content_plan(
    job: dict[str, Any],
    materials: dict[str, dict[str, Any]],
    semantic_review: dict[str, Any] | None = None,
    *,
    role_family: str | None = None,
) -> dict[str, Any]:
    """Build one language-neutral role-base plan for both CN and EN."""
    profile_material = materials["profile.yaml"]
    blueprints = validate_resume_blueprints(profile_material)
    role = str(role_family or job.get("role_family") or job.get("resume_family") or "general_software")
    if role not in CANONICAL_BLUEPRINT_ROLES:
        role = "general_software"
    blueprint = blueprints["roles"][role]
    evidence = materials["evidence.yaml"].get("evidence", [])
    evidence_index = {str(item.get("evidence_id")): item for item in evidence if isinstance(item, dict)}
    semantic = _semantic_ranking(semantic_review)
    job_text = " ".join(
        [str(job.get("title") or job.get("position") or ""), str(job.get("description") or ""),
         " ".join(str(value) for value in job.get("secondary_role_tags", []))]
    )

    education: list[dict[str, Any]] = []
    for item in materials["education.yaml"].get("education", []):
        details = list(item.get("details", []))
        details_cn = list(item.get("details_cn", []))
        detail_ids = [eid for eid in item.get("detail_evidence_ids", item.get("evidence_ids", [])) if eid in evidence_index]
        education.append(
            {
                "intent_id": f"education_{str(item.get('id', '')).lower()}",
                "resume_role": "education",
                "material_id": item.get("id"),
                "priority": len(education) + 1,
                "selected_material": item,
                "evidence_ids": detail_ids or [eid for eid in item.get("evidence_ids", []) if eid in evidence_index],
                "details": {"EN": details, "CN": details_cn or details},
                "details_active": bool(details),
                "optional": False,
                "reserve": False,
                "page_value": float(30 - len(education)),
                "jd_relevance": 0.0,
                "duplication_penalty": 0.0,
            }
        )

    experiences = []
    exp_limits = blueprint["experience_bullets"]
    language_targets = blueprint.get("language_targets", {})
    for item in materials["experience.yaml"].get("experience", []):
        intents = _build_bullet_intents(
            item, section="experience", semantic=semantic, evidence_index=evidence_index,
            job_text=job_text, preferred_families=blueprint["preferred_evidence_families"],
        )
        target = max(
            int(exp_limits["target"]),
            int(language_targets.get("CN", {}).get(item.get("id"), 0)),
            int(language_targets.get("EN", {}).get(item.get("id"), 0)),
        )
        experiences.append(
            {"material": item, "intents": intents[: min(int(exp_limits["max"]), target)], "reserve_intents": intents[target:]}
        )

    project_items = materials["projects.yaml"].get("projects", [])
    preferred = _ordered_materials(project_items, list(blueprint["preferred_project_order"]))
    reserve = _ordered_materials(project_items, list(blueprint.get("reserve_projects", [])))
    project_limits = blueprint["project_bullets"]
    projects: list[dict[str, Any]] = []
    for project_index, item in enumerate(preferred):
        intents = _build_bullet_intents(
            item, section="projects", semantic=semantic, evidence_index=evidence_index,
            job_text=job_text, preferred_families=blueprint["preferred_evidence_families"],
            breadth=project_index == len(preferred) - 1,
        )
        target = max(
            int(project_limits["target"]),
            int(language_targets.get("CN", {}).get(item.get("id"), 0)),
            int(language_targets.get("EN", {}).get(item.get("id"), 0)),
        )
        chosen = sorted(intents, key=lambda intent: (-intent["page_value"], intent["priority"]))[:target]
        chosen.sort(key=lambda intent: intent["priority"])
        projects.append(
            {"material": item, "intents": chosen, "reserve_intents": [intent for intent in intents if intent not in chosen], "reserve": False}
        )
    reserve_projects = []
    for item in reserve:
        intents = _build_bullet_intents(
            item, section="projects", semantic=semantic, evidence_index=evidence_index,
            job_text=job_text, preferred_families=blueprint["preferred_evidence_families"],
        )
        for intent in intents:
            intent["optional"] = True
            intent["reserve"] = True
        reserve_projects.append({"material": item, "intents": intents[: int(project_limits["min"])], "reserve_intents": intents[int(project_limits["min"]):], "reserve": True})

    skill_limits = blueprint["skill_rows"]
    skills = _skill_rows(materials["skills.yaml"].get("skills", {}), list(blueprint["skill_priorities"]), job_text, int(skill_limits["target"]))
    return {
        "schema_version": "2.0",
        "role_family": role,
        "mandatory_sections": list(blueprint["mandatory_sections"]),
        "blueprint": copy.deepcopy(blueprint),
        "page_fit_contracts": copy.deepcopy(blueprints["page_fit"]),
        "education": education,
        "experience": experiences,
        "projects": projects,
        "reserve_projects": reserve_projects,
        "skills": skills,
        "reserve_actions": [],
        "evidence_bank_ids": sorted(evidence_index),
        "evidence_index": {
            evidence_id: {
                "factual_description": str(item.get("factual_description", "")),
                "technologies": list(item.get("technologies", [])),
                "verified_metrics": list(item.get("verified_metrics", [])),
            }
            for evidence_id, item in evidence_index.items()
        },
        "semantic_high_evidence_ids": sorted(
            evidence_id for evidence_id, item in semantic.items()
            if str(item.get("relevance", "")).upper() == "HIGH"
        ),
        "language_intent_mapping": {
            "CN": "one-to-one",
            "EN": "one-to-one",
            "merged_or_split_intents": [],
        },
    }


def plan_bullet_intents(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        intent
        for section in ("experience", "projects")
        for item in plan.get(section, [])
        for intent in item.get("intents", [])
    ]


def content_statistics(plan: dict[str, Any], language: str) -> dict[str, int]:
    language = language.upper()
    text_parts: list[str] = []
    for slot in plan.get("education", []):
        material = slot["selected_material"]
        text_parts.extend(
            [str(_localized(material, "institution", language)), str(_localized(material, "qualification", language))]
        )
        if slot.get("details_active"):
            text_parts.extend(str(value) for value in slot.get("details", {}).get(language, []))
    for section in ("experience", "projects"):
        for group in plan.get(section, []):
            material = group["material"]
            text_parts.append(str(_localized(material, "name" if section == "projects" else "company", language)))
            if section == "projects":
                context = str(group.get("editor_context", {}).get(language, ""))
                if context:
                    text_parts.append(context)
            for intent in group["intents"]:
                text_parts.append(intent["variants"][language][intent["variant"]])
    for row in plan.get("skills", []):
        text_parts.extend(row["skills"])
    text = " ".join(text_parts)
    return {
        "cjk_character_count": len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text)),
        "english_word_count": len(re.findall(r"\b[A-Za-z]+(?:[-'][A-Za-z]+)*\b", text)),
        "substantive_content_rows": (
            sum(len(group["intents"]) for group in plan.get("experience", []))
            + sum(len(group["intents"]) for group in plan.get("projects", []))
            + sum(len(slot.get("details", {}).get(language, [])) for slot in plan.get("education", []) if slot.get("details_active"))
            + len(plan.get("skills", []))
        ),
    }


def _localized(item: dict[str, Any], key: str, language: str) -> Any:
    if language == "CN" and item.get(f"{key}_cn"):
        return item[f"{key}_cn"]
    return item.get(key, "")


def _latex_escape(value: Any) -> str:
    text = str(value or "")
    for source, target in (
        ("\\", r"\textbackslash{}"), ("&", r"\&"), ("%", r"\%"), ("$", r"\$"),
        ("#", r"\#"), ("_", r"\_"), ("{", r"\{"), ("}", r"\}"),
    ):
        text = text.replace(source, target)
    return text


def _section_block(title: str, body: list[str]) -> str:
    return "\n".join([f"\\section{{{title}}}", r"\resumeSubHeadingListStart", *body, r"\resumeSubHeadingListEnd"])


def _subheading_section_body(body: list[str]) -> str:
    return "\n".join([r"\resumeSubHeadingListStart", *body, r"\resumeSubHeadingListEnd"])


def render_resume_from_plan(
    template: str,
    plan: dict[str, Any],
    language: str,
) -> str:
    language = language.upper()
    if language not in {"CN", "EN"}:
        raise ValueError("resume language must be CN or EN")
    titles = (
        {"education": "教育背景", "experience": "实习经历", "projects": "项目经历", "skills": "技术能力"}
        if language == "CN" else
        {"education": "Education", "experience": "Experience", "projects": "Projects", "skills": "Technical Skills"}
    )
    education_body: list[str] = []
    for slot in plan["education"]:
        item = slot["selected_material"]
        education_body.extend(
            [
                r"  \resumeSubheading",
                f"    {{{_latex_escape(_localized(item, 'institution', language))}}}{{}}",
                f"    {{{_latex_escape(_localized(item, 'qualification', language))}}}{{{_latex_escape(_localized(item, 'date_range', language))}}}",
            ]
        )
        details = slot.get("details", {}).get(language, []) if slot.get("details_active") else []
        if details:
            education_body.append(r"    \resumeItemListStart")
            education_body.extend(f"      \\item \\small{{{_latex_escape(detail)}}}" for detail in details)
            education_body.append(r"    \resumeItemListEnd")

    experience_body: list[str] = []
    for group in plan["experience"]:
        item = group["material"]
        experience_body.extend(
            [
                r"  \resumeSubheading",
                f"    {{{_latex_escape(_localized(item, 'company', language))}}}{{{_latex_escape(_localized(item, 'location', language))}}}",
                f"    {{{_latex_escape(_localized(item, 'title', language))}}}{{{_latex_escape(_localized(item, 'date_range', language))}}}",
                r"    \resumeItemListStart",
            ]
        )
        for intent in group["intents"]:
            bullet = intent["variants"][language][intent["variant"]]
            experience_body.append(f"      \\item \\small{{{_latex_escape(bullet)}}}")
        experience_body.append(r"    \resumeItemListEnd")

    project_body: list[str] = []
    for group in plan["projects"]:
        item = group["material"]
        name = _latex_escape(_localized(item, "name", language))
        if item.get("repo_url"):
            name = r"\href{" + str(item["repo_url"]) + "}{" + name + "}"
        project_body.extend(
            [
                r"  \resumeSubheading",
                f"    {{{name}}}{{}}",
                f"    {{{_latex_escape(_localized(item, 'display_role', language) or 'Project')}}}{{{_latex_escape(item.get('date_range', ''))}}}",
                r"    \resumeItemListStart",
            ]
        )
        context = str(group.get("editor_context", {}).get(language, ""))
        if context:
            project_body.append(f"      \\item[] \\small{{{_latex_escape(context)}}}")
        for intent in group["intents"]:
            bullet = intent["variants"][language][intent["variant"]]
            project_body.append(f"      \\item \\small{{{_latex_escape(bullet)}}}")
        project_body.append(r"    \resumeItemListEnd")

    skills_body = [r"  \item{"]
    for row in plan["skills"]:
        label = SKILL_LABELS_CN.get(row["group"], row["group"]) if language == "CN" else row["group"].replace("_", " ").title()
        skills_body.append(f"    \\textbf{{{_latex_escape(label)}}}{{: {_latex_escape(', '.join(row['skills']))}}} \\\\")
    skills_body.append(r"  }")
    sections = {
        "education": _section_block(titles["education"], education_body),
        "experience": _section_block(titles["experience"], experience_body),
        "projects": _section_block(titles["projects"], project_body),
        "skills": _section_block(titles["skills"], skills_body),
    }
    marker_sections = {
        "education": _subheading_section_body(education_body),
        "skills": _subheading_section_body(skills_body),
        "experience": _subheading_section_body(experience_body),
        "projects": _subheading_section_body(project_body),
    }
    # Keep one template and one visual identity while allowing language-specific
    # leading/section rhythm.  Font size and margins remain unchanged.
    language_setup = "\\linespread{1.00}\\selectfont" if language == "CN" else ""
    template = template.replace(r"\pagestyle{empty}", r"\pagestyle{empty}" + "\n" + language_setup, 1)
    if language == "CN":
        template = template.replace(
            "leftmargin=14pt,topsep=2pt,itemsep=1pt,parsep=0pt,partopsep=0pt",
            "leftmargin=14pt,topsep=0pt,itemsep=0pt,parsep=0pt,partopsep=0pt",
            1,
        )
    template = template.replace(
        r"\titlespacing*{\section}{0pt}{8pt}{4pt}",
        r"\titlespacing*{\section}{0pt}{6pt}{2pt}"
        if language == "CN" else r"\titlespacing*{\section}{0pt}{10pt}{4pt}",
        1,
    )
    marker_tokens = {
        name: "{{RESUME_SECTION_" + name.upper() + "}}"
        for name in ("education", "skills", "experience", "projects")
    }
    uses_markers = any(token in template for token in marker_tokens.values())
    match = re.search(r"\\section\{Education\}", template)
    end = template.rfind(r"\end{document}")
    if not uses_markers and (not match or end < 0):
        raise ValueError("resume template does not expose a marker or legacy section architecture")
    body = "\n\n".join(sections[name] for name in ("education", "experience", "projects", "skills"))
    intent_ids = ", ".join(intent["intent_id"] for intent in plan_bullet_intents(plan))
    editor_contexts = [
        group["editor_context"]
        for group in plan.get("projects", [])
        if group.get("editor_context", {}).get(language)
    ]
    evidence_ids = ", ".join(
        sorted(
            {eid for intent in plan_bullet_intents(plan) for eid in intent["evidence_ids"]}
            | {eid for context in editor_contexts for eid in context["evidence_ids"]}
        )
    )
    header = (
        "% Resume Generator V2\n"
        f"% Role family: {plan['role_family']}\n"
        f"% Golden Base: {plan.get('selected_base_id', 'legacy')}\n"
        f"% Fact-plan intents: {intent_ids}\n"
        f"% Evidence IDs: {evidence_ids}\n"
    )
    if uses_markers:
        missing = [name for name, token in marker_tokens.items() if token not in template]
        if missing:
            raise ValueError(f"Golden Base template is missing section markers: {missing}")
        rendered = template
        for name, token in marker_tokens.items():
            rendered = rendered.replace(token, marker_sections[name], 1)
        if "{{RESUME_SECTION_" in rendered:
            raise ValueError("Golden Base template contains unresolved section markers")
        return header + rendered
    return header + template[: match.start()] + body + "\n\n" + template[end:]


def _poppler_tool(name: str) -> tuple[str, dict[str, str] | None]:
    installed = shutil.which(name)
    if installed:
        return installed, None
    local_root = Path(__file__).resolve().parents[1] / ".tools" / "poppler"
    local_tool = local_root / "usr" / "bin" / name
    if not local_tool.is_file():
        raise FileNotFoundError(
            f"Missing {name}; install poppler-utils or provide the project-local .tools/poppler runtime"
        )
    env = os.environ.copy()
    local_lib = str(local_root / "usr" / "lib" / "x86_64-linux-gnu")
    current = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = local_lib if not current else f"{local_lib}{os.pathsep}{current}"
    return str(local_tool), env


def _load_pymupdf() -> Any:
    try:
        return importlib.import_module("fitz")
    except ImportError as original_error:
        local_root = Path(__file__).resolve().parents[1] / ".tools" / "pymupdf"
        local_packages = local_root / "usr" / "lib" / "python3" / "dist-packages"
        local_lib = local_root / "usr" / "lib" / "x86_64-linux-gnu"
        if not local_packages.is_dir():
            raise original_error
        for name in (
            "libopenjp2.so.7",
            "libjbig2dec.so.0",
            "libmujs.so.1",
            "libgumbo.so.1",
        ):
            library = local_lib / name
            if library.is_file():
                ctypes.CDLL(str(library), mode=ctypes.RTLD_GLOBAL)
        sys.path.insert(0, str(local_packages))
        return importlib.import_module("fitz")


def _extract_pdf_geometry_pymupdf(pdf_path: Path) -> dict[str, Any]:
    fitz = _load_pymupdf()
    document = fitz.open(str(pdf_path))
    words: list[tuple[Any, ...]] = []
    lines: list[tuple[tuple[float, float, float, float], str]] = []
    out_of_bounds = False
    overlap_detected = False
    image_count = 0
    try:
        for page in document:
            image_count += len(page.get_images(full=True))
            page_words = list(page.get_text("words"))
            words.extend(page_words)
            for word in page_words:
                out_of_bounds = out_of_bounds or (
                    float(word[0]) < float(page.rect.x0) - 0.5
                    or float(word[1]) < float(page.rect.y0) - 0.5
                    or float(word[2]) > float(page.rect.x1) + 0.5
                    or float(word[3]) > float(page.rect.y1) + 0.5
                )
            page_lines: list[tuple[tuple[float, float, float, float], str]] = []
            for block in page.get_text("dict").get("blocks", []):
                if int(block.get("type", -1)) != 0:
                    continue
                for line in block.get("lines", []):
                    text = "".join(str(span.get("text") or "") for span in line.get("spans", [])).strip()
                    bbox = line.get("bbox")
                    if text and bbox and len(bbox) == 4:
                        page_lines.append((tuple(float(value) for value in bbox), text))
            lines.extend(page_lines)
            for index, (left_box, _) in enumerate(page_lines):
                for right_box, _ in page_lines[index + 1 :]:
                    x_overlap = min(left_box[2], right_box[2]) - max(left_box[0], right_box[0])
                    y_overlap = min(left_box[3], right_box[3]) - max(left_box[1], right_box[1])
                    min_height = min(left_box[3] - left_box[1], right_box[3] - right_box[1])
                    if x_overlap > 1.0 and y_overlap > max(1.0, min_height * 0.6):
                        overlap_detected = True
                        break
                if overlap_detected:
                    break
        text = " ".join(str(word[4]) for word in words)
        if document.page_count and words:
            page_width = float(document[0].rect.width)
            page_height = float(document[0].rect.height)
            first_y = min(float(word[1]) for word in words)
            last_y = max(float(word[3]) for word in words)
            span = (last_y - first_y) / page_height
        else:
            page_width = page_height = first_y = last_y = span = 0.0
        return {
            "page_count": int(document.page_count),
            "page_width": round(page_width, 3),
            "first_text_y": round(first_y, 3),
            "last_text_y": round(last_y, 3),
            "page_height": round(page_height, 3),
            "text_vertical_span_ratio": round(span, 4),
            "non_empty_line_count": len(lines),
            "cjk_character_count": len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text)),
            "english_word_count": len(re.findall(r"\b[A-Za-z]+(?:[-'][A-Za-z]+)*\b", text)),
            "text_out_of_bounds": out_of_bounds,
            "text_overlap_detected": overlap_detected,
            "text_clipped": out_of_bounds,
            "image_count": image_count,
            "text": text,
        }
    finally:
        document.close()


def _extract_pdf_geometry_poppler(pdf_path: Path) -> dict[str, Any]:
    """Fallback for environments that already provide Poppler."""
    pdfinfo, pdfinfo_env = _poppler_tool("pdfinfo")
    info = subprocess.run(
        [pdfinfo, str(pdf_path)], check=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, env=pdfinfo_env,
    ).stdout
    pages_match = re.search(r"Pages:\s+(\d+)", info)
    page_count = int(pages_match.group(1)) if pages_match else 0
    pdftotext, pdftotext_env = _poppler_tool("pdftotext")
    bbox = subprocess.run(
        [pdftotext, "-bbox-layout", str(pdf_path), "-"], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=pdftotext_env,
    ).stdout
    root = ET.fromstring(bbox)
    pages = [element for element in root.iter() if element.tag.endswith("page")]
    words = [element for element in root.iter() if element.tag.endswith("word")]
    lines = [element for element in root.iter() if element.tag.endswith("line") and "yMin" in element.attrib]
    text = " ".join("".join(element.itertext()) for element in words)
    out_of_bounds = False
    overlap_detected = False
    for page in pages:
        width = float(page.attrib.get("width", 0.0))
        height = float(page.attrib.get("height", 0.0))
        page_words = [element for element in page.iter() if element.tag.endswith("word")]
        out_of_bounds = out_of_bounds or any(
            float(word.attrib.get("xMin", 0.0)) < -0.5
            or float(word.attrib.get("yMin", 0.0)) < -0.5
            or float(word.attrib.get("xMax", 0.0)) > width + 0.5
            or float(word.attrib.get("yMax", 0.0)) > height + 0.5
            for word in page_words
        )
        page_lines = [
            element
            for element in page.iter()
            if element.tag.endswith("line")
            and all(key in element.attrib for key in ("xMin", "yMin", "xMax", "yMax"))
        ]
        for index, left in enumerate(page_lines):
            left_box = tuple(float(left.attrib[key]) for key in ("xMin", "yMin", "xMax", "yMax"))
            for right in page_lines[index + 1 :]:
                right_box = tuple(float(right.attrib[key]) for key in ("xMin", "yMin", "xMax", "yMax"))
                x_overlap = min(left_box[2], right_box[2]) - max(left_box[0], right_box[0])
                y_overlap = min(left_box[3], right_box[3]) - max(left_box[1], right_box[1])
                min_height = min(left_box[3] - left_box[1], right_box[3] - right_box[1])
                if x_overlap > 1.0 and y_overlap > max(1.0, min_height * 0.6):
                    overlap_detected = True
                    break
            if overlap_detected:
                break
    if pages and words:
        page_width = float(pages[0].attrib["width"])
        page_height = float(pages[0].attrib["height"])
        first_y = min(float(word.attrib["yMin"]) for word in words)
        last_y = max(float(word.attrib["yMax"]) for word in words)
        span = (last_y - first_y) / page_height
    else:
        page_width = page_height = first_y = last_y = span = 0.0
    return {
        "page_count": page_count,
        "page_width": round(page_width, 3),
        "first_text_y": round(first_y, 3),
        "last_text_y": round(last_y, 3),
        "page_height": round(page_height, 3),
        "text_vertical_span_ratio": round(span, 4),
        "non_empty_line_count": len(lines),
        "cjk_character_count": len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text)),
        "english_word_count": len(re.findall(r"\b[A-Za-z]+(?:[-'][A-Za-z]+)*\b", text)),
        "text_out_of_bounds": out_of_bounds,
        "text_overlap_detected": overlap_detected,
        "text_clipped": out_of_bounds,
        "image_count": 0,
        "text": text,
    }


def extract_pdf_geometry(pdf_path: Path) -> dict[str, Any]:
    """Measure PDF pages and rendered text without relaxing page-fit gates."""
    try:
        return _extract_pdf_geometry_pymupdf(pdf_path)
    except ImportError:
        return _extract_pdf_geometry_poppler(pdf_path)


def page_fit_state(metrics: dict[str, Any], contract: dict[str, Any]) -> str:
    if int(metrics.get("page_count", 0)) > 1:
        return "OVERFLOW"
    if int(metrics.get("page_count", 0)) != 1:
        return "INVALID"
    if any(
        bool(metrics.get(key, False))
        for key in ("text_out_of_bounds", "text_overlap_detected", "text_clipped")
    ):
        return "INVALID"
    span = float(metrics.get("text_vertical_span_ratio", 0.0))
    preferred_span = (
        float(contract["min_vertical_span_ratio"])
        <= span
        <= float(contract["max_vertical_span_ratio"])
    )
    line_count = int(metrics.get("non_empty_line_count", 0))
    preferred_lines = (
        int(contract.get("min_lines", 0)) <= line_count
        and (not contract.get("max_lines") or line_count <= int(contract["max_lines"]))
    )
    cjk_count = int(metrics.get("cjk_character_count", 0))
    word_count = int(metrics.get("english_word_count", 0))
    preferred_cjk = (
        not contract.get("min_cjk_chars")
        or (
            int(contract["min_cjk_chars"]) <= cjk_count
            and (not contract.get("max_cjk_chars") or cjk_count <= int(contract["max_cjk_chars"]))
        )
    )
    preferred_words = (
        not contract.get("min_words")
        or (
            int(contract["min_words"]) <= word_count
            and (not contract.get("max_words") or word_count <= int(contract["max_words"]))
        )
    )
    if preferred_span and preferred_lines and preferred_cjk and preferred_words:
        return "READY"

    acceptable_min = float(
        contract.get("acceptable_min_vertical_span_ratio", contract["min_vertical_span_ratio"])
    )
    acceptable_max = float(
        contract.get("acceptable_max_vertical_span_ratio", contract["max_vertical_span_ratio"])
    )
    density_targets_present = any(
        contract.get(key)
        for key in ("min_lines", "min_cjk_chars", "min_words")
    )
    minimum_lines_met = (
        not contract.get("min_lines") or line_count >= int(contract["min_lines"])
    )
    minimum_cjk_met = (
        not contract.get("min_cjk_chars") or cjk_count >= int(contract["min_cjk_chars"])
    )
    minimum_words_met = (
        not contract.get("min_words") or word_count >= int(contract["min_words"])
    )
    density_floor_met = (
        density_targets_present
        and minimum_lines_met
        and minimum_cjk_met
        and minimum_words_met
    )
    if acceptable_min <= span <= acceptable_max or density_floor_met:
        return "READY_WITH_DENSITY_WARNING"
    if span > acceptable_max:
        return "READY_WITH_DENSITY_WARNING"
    return "TOO_SHORT"


def _fit_gates_pass(gates: dict[str, Any]) -> bool:
    required = (
        "factual", "evidence", "privacy", "style",
        "required_image", "required_sections",
    )
    return all(gates.get(key, "PASS") in (True, "PASS") for key in required)


def _legal_single_page(metrics: dict[str, Any], gates: dict[str, Any]) -> bool:
    return (
        int(metrics.get("page_count", 0)) == 1
        and not any(
            bool(metrics.get(key, False))
            for key in ("text_out_of_bounds", "text_overlap_detected", "text_clipped")
        )
        and _fit_gates_pass(gates)
    )


def _distance_to_range(value: float, minimum: float, maximum: float) -> float:
    if value < minimum:
        return minimum - value
    if value > maximum:
        return value - maximum
    return 0.0


def _single_page_score(metrics: dict[str, Any], contract: dict[str, Any]) -> tuple[float, ...]:
    """Rank only legal one-page candidates; absolute span always leads soft density."""
    span = float(metrics.get("text_vertical_span_ratio", 0.0))
    span_target = (
        float(contract["min_vertical_span_ratio"])
        + float(contract["max_vertical_span_ratio"])
    ) / 2.0
    cjk = float(metrics.get("cjk_character_count", 0))
    words = float(metrics.get("english_word_count", 0))
    lines = float(metrics.get("non_empty_line_count", 0))
    content_distance = (
        _distance_to_range(
            cjk,
            float(contract.get("min_cjk_chars", cjk)),
            float(contract.get("max_cjk_chars", cjk)),
        )
        if contract.get("min_cjk_chars")
        else _distance_to_range(
            words,
            float(contract.get("min_words", words)),
            float(contract.get("max_words", words)),
        )
    )
    line_distance = _distance_to_range(
        lines,
        float(contract.get("min_lines", lines)),
        float(contract.get("max_lines", lines)),
    )
    return (-abs(span - span_target), -content_distance, -line_distance)


def _artifact_snapshot(tex_path: Path, pdf_path: Path) -> dict[Path, bytes]:
    paths = {tex_path, pdf_path, tex_path.with_suffix(".log"), tex_path.with_suffix(".aux")}
    return {path: path.read_bytes() for path in paths if path.exists()}


def _restore_artifacts(snapshot: dict[Path, bytes]) -> None:
    for path, content in snapshot.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def resume_candidate_gates(
    plan: dict[str, Any], metrics: dict[str, Any], language: str
) -> dict[str, str]:
    """Run local factual/evidence/privacy/style gates for one rendered candidate."""
    bank = set(plan.get("evidence_bank_ids", []))
    claims = plan_bullet_intents(plan) + [
        group["editor_context"]
        for group in plan.get("projects", [])
        if group.get("editor_context", {}).get(language)
    ]
    evidence_pass = all(
        claim.get("evidence_ids") and set(claim["evidence_ids"]).issubset(bank)
        for claim in claims
    )
    text = str(metrics.get("text", ""))
    style_terms = CN_BANNED if language == "CN" else EN_BANNED
    style_pass = not any(term.casefold() in text.casefold() for term in style_terms)
    privacy_pass = not any(
        term.casefold() in text.casefold() for term in INTERNAL_RENDER_BANNED
    )
    selected_base = plan.get("selected_base") or {}
    required_image_pass = (
        str(selected_base.get("photo_variant") or "no_photo") != "photo"
        or int(metrics.get("image_count", 0)) > 0
    )
    sections_pass = all(
        plan.get(section) for section in plan.get("mandatory_sections", [])
    )
    return {
        "factual": "PASS" if evidence_pass else "FAIL",
        "evidence": "PASS" if evidence_pass else "FAIL",
        "privacy": "PASS" if privacy_pass else "FAIL",
        "style": "PASS" if style_pass else "FAIL",
        "required_image": "PASS" if required_image_pass else "FAIL",
        "required_sections": "PASS" if sections_pass else "FAIL",
    }


def _expand_plan(plan: dict[str, Any], language: str = "EN") -> str:
    language = language.upper()
    for intent in sorted(plan_bullet_intents(plan), key=lambda item: -item["page_value"]):
        if intent["variant"] == "compact" and intent["variants"][language]["compact"] != intent["variants"][language]["standard"]:
            intent["variant"] = "standard"
            return f"expanded {intent['intent_id']} compact->standard"
        if intent["variant"] == "standard" and intent["variants"][language]["standard"] != intent["variants"][language]["expanded"]:
            intent["variant"] = "expanded"
            return f"expanded {intent['intent_id']} standard->expanded"
    for group in sorted(plan["projects"], key=lambda item: -max((intent["page_value"] for intent in item["reserve_intents"]), default=-999)):
        if group["reserve_intents"]:
            added = max(group["reserve_intents"], key=lambda item: item["page_value"])
            group["reserve_intents"].remove(added)
            added["optional"] = True
            group["intents"].append(added)
            group["intents"].sort(key=lambda item: item["priority"])
            return f"added reserve bullet {added['intent_id']}"
    for slot in plan["education"]:
        if slot["details"]["EN"] and not slot["details_active"]:
            slot["details_active"] = True
            return f"restored education detail {slot['intent_id']}"
    if plan["reserve_projects"]:
        added_group = plan["reserve_projects"].pop(0)
        plan["projects"].append(added_group)
        return f"added reserve project {added_group['material']['id']}"
    return "no evidence-backed expansion available"


def _compact_plan(plan: dict[str, Any], language: str = "EN") -> str:
    language = language.upper()
    for intent in sorted(plan_bullet_intents(plan), key=lambda item: item["page_value"]):
        if intent["variant"] == "expanded" and intent["variants"][language]["expanded"] != intent["variants"][language]["standard"]:
            intent["variant"] = "standard"
            return f"compacted {intent['intent_id']} expanded->standard"
        if intent["variant"] == "standard" and intent["variants"][language]["standard"] != intent["variants"][language]["compact"]:
            intent["variant"] = "compact"
            return f"compacted {intent['intent_id']} standard->compact"
    minimum = int(plan["blueprint"]["project_bullets"]["min"])
    removable = [
        (intent["page_value"], group, intent)
        for group in plan["projects"]
        if len(group["intents"]) > minimum
        for intent in group["intents"]
        if intent.get("optional") or len(group["intents"]) > int(plan["blueprint"]["project_bullets"]["target"])
    ]
    if removable:
        _, group, removed = min(removable, key=lambda item: item[0])
        group["intents"].remove(removed)
        return f"removed reserve bullet {removed['intent_id']}"
    optional_details = [slot for slot in reversed(plan["education"]) if slot.get("details_active") and slot.get("details", {}).get("EN")]
    if optional_details:
        optional_details[0]["details_active"] = False
        return f"removed education detail {optional_details[0]['intent_id']}"
    return "no safe compaction available"


def fit_resume_pdf(
    template: str,
    plan: dict[str, Any],
    language: str,
    tex_path: Path,
    *,
    compile_pdf: Callable[[Path], str],
    measure_pdf: Callable[[Path], dict[str, Any]] = extract_pdf_geometry,
    candidate_gate: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Render at most three candidates and always retain the best legal single page."""
    language = language.upper()
    working = copy.deepcopy(plan)
    contract = working["page_fit_contracts"][language]
    history: list[dict[str, Any]] = []
    pdf_path = tex_path.with_suffix(".pdf")
    best_single_page_candidate: dict[str, Any] | None = None
    last_metrics: dict[str, Any] = {}
    for iteration in range(1, 4):
        tex_path.parent.mkdir(parents=True, exist_ok=True)
        rendered_tex = render_resume_from_plan(template, working, language)
        tex_path.write_text(rendered_tex, encoding="utf-8")
        pdf_path = Path(compile_pdf(tex_path))
        metrics = measure_pdf(pdf_path)
        last_metrics = metrics
        golden_span = float(contract.get("golden_vertical_span_ratio", 0.0))
        if golden_span:
            metrics["relative_to_golden_span"] = round(
                float(metrics.get("text_vertical_span_ratio", 0.0)) / golden_span,
                4,
            )
        state = page_fit_state(metrics, contract)
        gates = (
            candidate_gate(working, metrics)
            if candidate_gate
            else resume_candidate_gates(working, metrics, language)
        )
        legal_single_page = _legal_single_page(metrics, gates)
        record = {
            "iteration": iteration,
            "state": state,
            "metrics": {key: value for key, value in metrics.items() if key != "text"},
            "gates": copy.deepcopy(gates),
            "legal_single_page": legal_single_page,
        }
        history.append(record)
        if legal_single_page:
            candidate = {
                "iteration": iteration,
                "score": _single_page_score(metrics, contract),
                "state": state,
                "metrics": copy.deepcopy(metrics),
                "gates": copy.deepcopy(gates),
                "plan": copy.deepcopy(working),
                "rendered_tex": rendered_tex,
                "artifacts": _artifact_snapshot(tex_path, pdf_path),
            }
            if (
                best_single_page_candidate is None
                or candidate["score"] > best_single_page_candidate["score"]
            ):
                best_single_page_candidate = candidate
                record["saved_as_best_single_page"] = True
        if state == "READY" and legal_single_page:
            break
        if int(metrics.get("page_count", 0)) > 1 and best_single_page_candidate is not None:
            record["rejected"] = "multi-page candidate cannot replace saved single-page candidate"
            record["rollback_to_iteration"] = best_single_page_candidate["iteration"]
            break
        if iteration < 3:
            span = float(metrics.get("text_vertical_span_ratio", 0.0))
            action = _compact_plan(working, language) if (
                state == "OVERFLOW"
                or span > float(contract["max_vertical_span_ratio"])
            ) else _expand_plan(working, language)
            record["adjustment"] = action

    selected_iteration: int | None = None
    selected_plan = working
    selected_gates: dict[str, Any] = {}
    if best_single_page_candidate is not None:
        _restore_artifacts(best_single_page_candidate["artifacts"])
        selected_iteration = int(best_single_page_candidate["iteration"])
        selected_plan = best_single_page_candidate["plan"]
        restored_metrics = measure_pdf(pdf_path)
        if golden_span:
            restored_metrics["relative_to_golden_span"] = round(
                float(restored_metrics.get("text_vertical_span_ratio", 0.0)) / golden_span,
                4,
            )
        selected_gates = (
            candidate_gate(selected_plan, restored_metrics)
            if candidate_gate
            else best_single_page_candidate["gates"]
        )
        if not _legal_single_page(restored_metrics, selected_gates):
            raise ValueError("restored page-fit candidate failed hard single-page or safety gates")
        final_metrics = restored_metrics
        final_state = page_fit_state(final_metrics, contract)
        rendered_text = str(final_metrics.get("text", ""))
    else:
        final_metrics = last_metrics
        final_state = history[-1]["state"] if history else "INVALID"
        rendered_text = str(last_metrics.get("text", ""))

    if best_single_page_candidate is None:
        status = "PAGE_FIT_REVIEW_REQUIRED"
    elif final_state == "READY":
        status = "READY"
    elif final_state == "READY_WITH_DENSITY_WARNING":
        status = "READY_WITH_DENSITY_WARNING"
    else:
        status = "PAGE_FIT_REVIEW_REQUIRED"
    return {
        "status": status,
        "language": language,
        "tex_path": str(tex_path),
        "pdf_path": str(pdf_path),
        "iterations": history,
        "selected_candidate_iteration": selected_iteration,
        "best_single_page_candidate": (
            {
                "iteration": selected_iteration,
                "metrics": {key: value for key, value in final_metrics.items() if key != "text"},
                "gates": selected_gates,
            }
            if best_single_page_candidate is not None
            else None
        ),
        "plan": selected_plan,
        "metrics": {key: value for key, value in final_metrics.items() if key != "text"},
        "rendered_text": rendered_text,
    }


def run_quality_gates(plan: dict[str, Any], cn_fit: dict[str, Any], en_fit: dict[str, Any]) -> dict[str, Any]:
    intents = plan_bullet_intents(plan)
    bank = set(plan.get("evidence_bank_ids", []))
    factual_errors = [
        intent["intent_id"] for intent in intents
        if not intent.get("evidence_ids") or not set(intent["evidence_ids"]).issubset(bank)
    ]
    metric_drift = [
        intent["intent_id"] for intent in intents
        if any(
            _metric_tokens(intent["variants"][language][variant]) != intent["metrics"]
            for language in ("CN", "EN")
            for variant in ("compact", "standard", "expanded")
        )
    ]
    factual_errors.extend(metric_drift)
    editor_contexts = [
        group["editor_context"]
        for group in plan.get("projects", [])
        if group.get("editor_context", {}).get("CN")
    ]
    factual_errors.extend(
        context["intent_id"]
        for context in editor_contexts
        if not context.get("evidence_ids") or not set(context["evidence_ids"]).issubset(bank)
    )
    cn_text = str(cn_fit.get("rendered_text", ""))
    en_text = str(en_fit.get("rendered_text", ""))
    language_findings = {
        "CN": [phrase for phrase in CN_BANNED + INTERNAL_RENDER_BANNED if phrase.casefold() in cn_text.casefold()],
        "EN": [phrase for phrase in EN_BANNED + INTERNAL_RENDER_BANNED if phrase.casefold() in en_text.casefold()],
    }
    duplicate_findings: dict[str, list[str]] = {}
    for language, fit in (("CN", cn_fit), ("EN", en_fit)):
        seen: set[str] = set()
        duplicates: list[str] = []
        for intent in plan_bullet_intents(fit.get("plan", plan)):
            wording = normalize_token(intent["variants"][language][intent["variant"]])
            if wording in seen:
                duplicates.append(intent["intent_id"])
            seen.add(wording)
        duplicate_findings[language] = duplicates
    language_pass = not any(language_findings.values()) and not any(duplicate_findings.values())
    cn_facts = {
        intent["intent_id"]: (intent["factual_scope"], tuple(intent["metrics"]))
        for intent in plan_bullet_intents(cn_fit.get("plan", plan))
    }
    en_facts = {
        intent["intent_id"]: (intent["factual_scope"], tuple(intent["metrics"]))
        for intent in plan_bullet_intents(en_fit.get("plan", plan))
    }
    bilingual_pass = cn_facts == en_facts
    section_complete = all(plan.get(section) for section in plan["mandatory_sections"])
    project_ids = [group["material"]["id"] for group in plan["projects"]]
    selected_evidence = {eid for intent in intents for eid in intent["evidence_ids"]}
    missing_high = sorted(set(plan.get("semantic_high_evidence_ids", [])) - selected_evidence)
    return {
        "factual": "PASS" if not factual_errors else "FAIL",
        "missing_evidence_intents": factual_errors,
        "metric_drift_intents": metric_drift,
        "relevance": "PASS" if plan["role_family"] in CANONICAL_BLUEPRINT_ROLES and project_ids and not missing_high else "FAIL",
        "missing_high_semantic_evidence": missing_high,
        "completeness": "PASS" if section_complete and len(project_ids) >= 3 and bool(plan["experience"]) else "FAIL",
        "language": "PASS" if language_pass else "FAIL",
        "language_findings": language_findings,
        "duplicate_wording_findings": duplicate_findings,
        "bilingual_consistency": "PASS" if bilingual_pass else "FAIL",
        "page_fit_CN": cn_fit["status"],
        "page_fit_EN": en_fit["status"],
        "ready_CN": all((not factual_errors, not missing_high, section_complete, language_pass, bilingual_pass, cn_fit["status"] == "READY")),
        "ready_EN": all((not factual_errors, not missing_high, section_complete, language_pass, bilingual_pass, en_fit["status"] == "READY")),
    }
