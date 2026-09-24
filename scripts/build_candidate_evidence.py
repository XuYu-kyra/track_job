#!/usr/bin/env python3
"""Build the runtime semantic candidate-evidence JSON from cv/materials."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

try:
    from common import load_config, write_json
    from resume_v2 import validate_resume_blueprints
    from semantic_review import EVIDENCE_SOURCE_CLASS_VALUES, prepare_candidate_evidence
except ModuleNotFoundError:
    from scripts.common import load_config, write_json
    from scripts.resume_v2 import validate_resume_blueprints
    from scripts.semantic_review import EVIDENCE_SOURCE_CLASS_VALUES, prepare_candidate_evidence


DEFAULT_INPUT = "cv/materials/evidence.yaml"
DEFAULT_OUTPUT = "data/job_cache/semantic_candidate_evidence.json"
ACTIVE_MATERIAL_FILES = (
    "profile.yaml",
    "education.yaml",
    "experience.yaml",
    "projects.yaml",
    "skills.yaml",
    "evidence.yaml",
)
CANONICAL_ROLE_FAMILIES = frozenset(
    {
        "test_development",
        "ai_application",
        "robot_software",
        "general_software",
        "backend",
        "data_engineering",
        "engineering_tools",
        "software_automation",
        "reliability",
    }
)
SKILL_PROFICIENCIES = frozenset({"strong", "working", "exposure"})
RUNTIME_SOURCE_CLASSES = frozenset(
    {"VERIFIED", "USER_ATTESTED", "DERIVED_RESUME_SAFE"}
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build anonymized semantic-review evidence from candidate materials."
    )
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--max-items", type=int, default=40)
    return parser.parse_args()


def validate_materials_directory(materials_dir: Path) -> dict[str, dict[str, Any]]:
    """Validate cross-file IDs and controlled vocabularies without changing facts."""

    missing = [name for name in ACTIVE_MATERIAL_FILES if not (materials_dir / name).exists()]
    if missing:
        raise ValueError(f"candidate materials missing files: {', '.join(missing)}")
    materials = {name: load_config(materials_dir / name) for name in ACTIVE_MATERIAL_FILES}
    errors: list[str] = []

    evidence_items = materials["evidence.yaml"].get("evidence", [])
    if not isinstance(evidence_items, list):
        raise ValueError("candidate materials evidence.yaml.evidence must be a list")
    evidence_ids: set[str] = set()
    for index, item in enumerate(evidence_items):
        if not isinstance(item, dict):
            errors.append(f"evidence[{index}] must be an object")
            continue
        evidence_id = str(item.get("evidence_id") or "").strip()
        if not evidence_id:
            errors.append(f"evidence[{index}] is missing evidence_id")
        elif evidence_id in evidence_ids:
            errors.append(f"duplicate evidence_id: {evidence_id}")
        evidence_ids.add(evidence_id)
        source_class = str(item.get("source_class") or "UNCERTAIN").upper()
        if source_class not in EVIDENCE_SOURCE_CLASS_VALUES:
            errors.append(f"{evidence_id or f'evidence[{index}]'} has invalid source_class")
        for role in item.get("suitable_role_families", []):
            if str(role) not in CANONICAL_ROLE_FAMILIES:
                errors.append(f"{evidence_id} has invalid role family: {role}")

    entity_ids: set[str] = set()
    references: list[tuple[str, str]] = []
    section_specs = (
        ("education.yaml", "education", "evidence_ids"),
        ("experience.yaml", "experience", "source_evidence_ids"),
        ("projects.yaml", "projects", "source_evidence_ids"),
    )
    for file_name, section, evidence_key in section_specs:
        items = materials[file_name].get(section, [])
        if not isinstance(items, list):
            errors.append(f"{file_name}.{section} must be a list")
            continue
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append(f"{file_name}.{section}[{index}] must be an object")
                continue
            item_id = str(item.get("id") or "").strip()
            label = item_id or f"{file_name}.{section}[{index}]"
            if not item_id:
                errors.append(f"{label} is missing id")
            elif item_id in entity_ids:
                errors.append(f"duplicate material id: {item_id}")
            entity_ids.add(item_id)
            source_class = str(item.get("source_class") or "UNCERTAIN").upper()
            if source_class not in EVIDENCE_SOURCE_CLASS_VALUES:
                errors.append(f"{label} has invalid source_class")
            for evidence_id in item.get(evidence_key, []):
                references.append((label, str(evidence_id)))
            for evidence_id in item.get("detail_evidence_ids", []):
                references.append((label, str(evidence_id)))
            for role in item.get("role_targets", []):
                if str(role) not in CANONICAL_ROLE_FAMILIES:
                    errors.append(f"{label} has invalid role family: {role}")
            for field in ("bullet_variants", "bullet_variants_cn"):
                variants = item.get(field, {})
                if isinstance(variants, dict):
                    for role in variants:
                        if str(role) not in CANONICAL_ROLE_FAMILIES:
                            errors.append(f"{label}.{field} has invalid role family: {role}")
            for field, base_field in (
                ("bullets_expanded", "bullets"),
                ("bullets_expanded_cn", "bullets_cn"),
            ):
                expanded = item.get(field, [])
                if expanded and len(expanded) != len(item.get(base_field, [])):
                    errors.append(f"{label}.{field} must align one-to-one with {base_field}")

    skill_groups = materials["skills.yaml"].get("skills", {})
    if not isinstance(skill_groups, dict):
        errors.append("skills.yaml.skills must be an object")
    else:
        for role, skills in skill_groups.items():
            if str(role) not in CANONICAL_ROLE_FAMILIES:
                errors.append(f"skills.yaml has invalid role family: {role}")
            if not isinstance(skills, list):
                errors.append(f"skills.yaml.skills.{role} must be a list")
                continue
            for index, skill in enumerate(skills):
                if not isinstance(skill, dict):
                    errors.append(f"skills.yaml.skills.{role}[{index}] must be an object")
                    continue
                label = f"skills.yaml.skills.{role}[{index}]"
                if str(skill.get("proficiency") or "") not in SKILL_PROFICIENCIES:
                    errors.append(f"{label} has invalid proficiency")
                source_class = str(skill.get("source_class") or "UNCERTAIN").upper()
                if source_class not in EVIDENCE_SOURCE_CLASS_VALUES:
                    errors.append(f"{label} has invalid source_class")
                for evidence_id in skill.get("evidence_ids", []):
                    references.append((label, str(evidence_id)))

    profile = materials["profile.yaml"].get("candidate_profile", {})
    if isinstance(profile, dict):
        for field in ("preferred_role_families", "expansion_role_families"):
            for role in profile.get(field, []):
                if str(role) not in CANONICAL_ROLE_FAMILIES:
                    errors.append(f"profile.yaml.{field} has invalid role family: {role}")
    try:
        validate_resume_blueprints(materials["profile.yaml"])
    except ValueError as exc:
        errors.append(str(exc))

    for owner, evidence_id in references:
        if evidence_id not in evidence_ids:
            errors.append(f"{owner} references missing evidence_id: {evidence_id}")

    active_corpus = "\n".join(
        (materials_dir / name).read_text(encoding="utf-8")
        for name in ACTIVE_MATERIAL_FILES
    )
    for marker in ("Example Labs", "github.com/example/", "sample_ai_project"):
        if marker in active_corpus:
            errors.append(f"active candidate materials contain fictional marker: {marker}")

    if errors:
        raise ValueError("candidate materials validation failed: " + "; ".join(errors))
    return materials


def build_runtime_evidence(
    materials: Any,
    *,
    max_items: int = 40,
    generated_from: str = DEFAULT_INPUT,
) -> dict[str, Any]:
    if not isinstance(materials, dict):
        raise ValueError("candidate evidence materials must be an object")
    raw_items = materials.get("evidence", [])
    if not isinstance(raw_items, list):
        raise ValueError("candidate evidence materials.evidence must be a list")

    included = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise ValueError(f"candidate evidence materials.evidence[{index}] must be an object")
        source_class = str(raw.get("source_class") or "UNCERTAIN").upper()
        if source_class not in EVIDENCE_SOURCE_CLASS_VALUES:
            raise ValueError(
                f"candidate evidence materials.evidence[{index}].source_class is invalid"
            )
        if source_class in RUNTIME_SOURCE_CLASSES:
            included.append(raw)

    if len(included) > max_items:
        raise ValueError(
            f"candidate evidence has {len(included)} usable items; max-items is {max_items}"
        )
    prepared = prepare_candidate_evidence(included, max_items=max_items)
    return {
        "schema_version": str(materials.get("schema_version") or "1.0"),
        "generated_from": generated_from,
        "evidence": prepared,
    }


def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    materials_dir = input_path.parent
    if input_path.name == "evidence.yaml" and all(
        (materials_dir / name).exists() for name in ACTIVE_MATERIAL_FILES
    ):
        payload = validate_materials_directory(materials_dir)["evidence.yaml"]
    else:
        payload = load_config(input_path)
    runtime = build_runtime_evidence(
        payload,
        max_items=args.max_items,
        generated_from=str(Path(args.input).as_posix()),
    )
    write_json(args.output, runtime)
    counts: dict[str, int] = {}
    for item in runtime["evidence"]:
        source_class = str(item["source_class"])
        counts[source_class] = counts.get(source_class, 0) + 1
    summary = ", ".join(f"{key}={counts[key]}" for key in sorted(counts))
    print(f"Candidate evidence: {len(runtime['evidence'])} items ({summary}) -> {args.output}")


if __name__ == "__main__":
    main()
