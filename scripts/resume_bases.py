#!/usr/bin/env python3
"""Approved Golden Base registry, safe import, selection and tailoring contracts.

The ZIP files are immutable source artifacts.  This module never treats text as
evidence merely because it appeared in a ZIP: rendered claims still come from
``cv/materials`` and carry evidence IDs through Resume Generator V2.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import shutil
import stat
from pathlib import Path, PurePosixPath
from typing import Any
from zipfile import BadZipFile, ZipFile

try:
    from common import load_config, normalize_token
except ModuleNotFoundError:
    from scripts.common import load_config, normalize_token


APPROVED_BASE_IDS = frozenset(
    f"{role}.{language}.{variant}.v1"
    for role in ("ai", "robot", "se", "test")
    for language, variant in (
        ("cn", "no_photo"),
        ("cn", "photo"),
        ("en", "no_photo"),
    )
)
BASE_PLAN_ROLES = {
    "ai": "ai_application",
    "robot": "robot_software",
    "se": "general_software",
    "test": "test_development",
}
VALIDATION_TERMS = (
    "test", "testing", "validation", "verification", "qa", "sdet",
    "fault injection", "regression", "测试", "验证", "质量", "故障注入",
)
SOFTWARE_TERMS = (
    "backend", "platform", "service", "api", "development", "developer",
    "software", "后端", "平台", "服务", "开发", "软件",
)
SECTION_KEYS = ("education", "skills", "experience", "projects")


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _repo_path(repo_root: Path, value: Any) -> Path:
    path = Path(str(value or ""))
    resolved = path.resolve() if path.is_absolute() else (repo_root / path).resolve()
    try:
        resolved.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise ValueError(f"resume-base path leaves repository: {value}") from exc
    return resolved


def load_resume_base_registry(
    path: str | Path = "config/resume_bases.yaml",
    *,
    repo_root: str | Path | None = None,
    require_complete: bool = True,
) -> dict[str, Any]:
    registry_path = Path(path)
    root = Path(repo_root).resolve() if repo_root else registry_path.resolve().parents[1]
    if not registry_path.is_absolute():
        registry_path = root / registry_path
    payload = load_config(registry_path)
    bases = payload.get("bases")
    if not isinstance(bases, dict):
        raise ValueError("resume_bases.yaml.bases must be an object")
    enabled = {base_id for base_id, item in bases.items() if bool(item.get("enabled", False))}
    if require_complete and enabled != APPROVED_BASE_IDS:
        missing = sorted(APPROVED_BASE_IDS - enabled)
        extra = sorted(enabled - APPROVED_BASE_IDS)
        raise ValueError(
            "approved resume-base registry must contain exactly 12 enabled bases; "
            f"missing={missing}, extra={extra}"
        )
    for base_id, item in bases.items():
        if not isinstance(item, dict) or str(item.get("base_id") or "") != base_id:
            raise ValueError(f"resume base {base_id} has an invalid stable base_id")
        if str(item.get("language") or "").upper() not in {"CN", "EN"}:
            raise ValueError(f"resume base {base_id} has invalid language")
        if str(item.get("photo_variant") or "") not in {"photo", "no_photo"}:
            raise ValueError(f"resume base {base_id} has invalid photo_variant")
        if list(item.get("section_order") or []) != list(SECTION_KEYS):
            raise ValueError(f"resume base {base_id} must preserve approved section order")
        source_zip = _repo_path(root, item.get("source_zip"))
        if not source_zip.is_file():
            raise FileNotFoundError(f"registered source ZIP does not exist: {source_zip}")
        expected_hash = str(item.get("source_sha256") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValueError(f"resume base {base_id} has invalid source_sha256")
    payload["_registry_path"] = str(registry_path)
    payload["_repo_root"] = str(root)
    return payload


def load_resume_base_content(
    path: str | Path = "config/resume_base_content.yaml",
    *,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    content_path = Path(path)
    root = Path(repo_root).resolve() if repo_root else content_path.resolve().parents[1]
    if not content_path.is_absolute():
        content_path = root / content_path
    payload = load_config(content_path)
    roles = payload.get("roles")
    if not isinstance(roles, dict) or set(roles) != set(BASE_PLAN_ROLES):
        raise ValueError("resume-base content manifest must define ai/robot/se/test")
    for role, expected_plan_role in BASE_PLAN_ROLES.items():
        item = roles[role]
        if str(item.get("plan_role") or "") != expected_plan_role:
            raise ValueError(f"resume-base content {role} has invalid plan_role")
        if len(item.get("project_entries") or []) != 3:
            raise ValueError(f"resume-base content {role} must define three base projects")
        if not item.get("experience_entries") or not item.get("skill_groups"):
            raise ValueError(f"resume-base content {role} is incomplete")
    return payload


def _safe_zip_members(archive: ZipFile) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        path = PurePosixPath(name)
        file_mode = (info.external_attr >> 16) & 0xFFFF
        if (
            not name
            or path.is_absolute()
            or ".." in path.parts
            or re.match(r"^[A-Za-z]:", name)
            or stat.S_ISLNK(file_mode)
        ):
            raise ValueError(f"unsafe ZIP member rejected: {info.filename}")
        if info.is_dir():
            continue
        if len(path.parts) != 1:
            raise ValueError(f"unexpected nested ZIP member rejected: {info.filename}")
        members[path.name] = archive.read(info)
    return members


def _header_template(photo_variant: str) -> str:
    if photo_variant == "photo":
        return "\n".join(
            [
                r"\begin{center}",
                r"\begin{minipage}[c]{0.76\textwidth}",
                r"\centering",
                r"{\LARGE \textbf{{{FULL_NAME}}}}\\[3pt]",
                r"{{CONTACT_LINE}}",
                r"\end{minipage}\hfill",
                r"\begin{minipage}[c]{0.16\textwidth}",
                r"\centering",
                r"\includegraphics[width=2.3cm,height=3.0cm,keepaspectratio]{{{HEADSHOT_PATH}}}",
                r"\end{minipage}",
                r"\end{center}",
            ]
        )
    return "\n".join(
        [
            r"\begin{center}",
            r"{\LARGE \textbf{{{FULL_NAME}}}}\\[3pt]",
            r"{{CONTACT_LINE}}",
            r"\end{center}",
        ]
    )


def normalize_base_template(
    source: str,
    *,
    section_order: list[str],
    photo_variant: str,
) -> tuple[str, dict[str, str]]:
    """Keep the approved preamble/typography and replace content with explicit slots."""
    document_marker = r"\begin{document}"
    end_marker = r"\end{document}"
    document_start = source.find(document_marker)
    document_end = source.rfind(end_marker)
    if document_start < 0 or document_end < document_start:
        raise ValueError("approved base main.tex has no complete document environment")
    preamble = source[:document_start]
    body_start = document_start + len(document_marker)
    body = source[body_start:document_end]
    sections = list(re.finditer(r"\\section\{([^{}]+)\}", body))
    if len(sections) != len(section_order):
        raise ValueError(
            f"approved base must contain {len(section_order)} top-level sections; found {len(sections)}"
        )
    titles = {
        section_key: sections[index].group(1)
        for index, section_key in enumerate(section_order)
    }
    section_blocks: list[str] = []
    for section_key in section_order:
        token = section_key.upper()
        section_blocks.extend(
            [
                f"% RESUME_SECTION:{section_key}:BEGIN",
                f"\\section{{{titles[section_key]}}}",
                f"{{{{RESUME_SECTION_{token}}}}}",
                f"% RESUME_SECTION:{section_key}:END",
            ]
        )
    normalized = "\n".join(
        [
            "% Normalized Golden Base template; source main.tex is preserved beside this file.",
            preamble.rstrip(),
            document_marker,
            "% RESUME_HEADER:BEGIN",
            _header_template(photo_variant),
            "% RESUME_HEADER:END",
            "",
            *section_blocks,
            "",
            end_marker,
            "",
        ]
    )
    return normalized, titles


def import_registered_bases(
    registry_path: str | Path = "config/resume_bases.yaml",
    *,
    repo_root: str | Path | None = None,
    selected_base_ids: set[str] | None = None,
    refresh_changed: bool = False,
) -> list[dict[str, Any]]:
    registry = load_resume_base_registry(registry_path, repo_root=repo_root)
    root = Path(registry["_repo_root"])
    imported: list[dict[str, Any]] = []
    for base_id, item in sorted(registry["bases"].items()):
        if not item.get("enabled") or (selected_base_ids and base_id not in selected_base_ids):
            continue
        source_zip = _repo_path(root, item["source_zip"])
        actual_hash = sha256_file(source_zip)
        if actual_hash != str(item["source_sha256"]).lower():
            raise ValueError(
                f"source ZIP hash mismatch for {base_id}: expected {item['source_sha256']}, got {actual_hash}"
            )
        try:
            with ZipFile(source_zip) as archive:
                members = _safe_zip_members(archive)
        except BadZipFile as exc:
            raise ValueError(f"invalid source ZIP for {base_id}: {source_zip}") from exc
        expected_assets = set(str(value) for value in item.get("expected_assets", []))
        if set(members) != expected_assets or "main.tex" not in members:
            raise ValueError(
                f"source ZIP assets for {base_id} differ from registry: "
                f"expected={sorted(expected_assets)}, actual={sorted(members)}"
            )
        try:
            source_tex = members["main.tex"].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"main.tex for {base_id} is not UTF-8") from exc
        normalized, titles = normalize_base_template(
            source_tex,
            section_order=list(item["section_order"]),
            photo_variant=str(item["photo_variant"]),
        )
        target = _repo_path(root, item["normalized_template_dir"])
        marker = target / ".source_sha256"
        if (
            marker.exists()
            and marker.read_text(encoding="utf-8").strip() != actual_hash
            and not refresh_changed
        ):
            raise ValueError(
                f"refusing to replace changed source under existing version ID {base_id}"
            )
        planned = {
            "main.tex": members["main.tex"],
            "template.tex": normalized.encode("utf-8"),
            ".source_sha256": f"{actual_hash}\n".encode("utf-8"),
        }
        for asset in sorted(expected_assets - {"main.tex"}):
            planned[asset] = members[asset]
        manifest = {
            "base_id": base_id,
            "source_zip": str(item["source_zip"]),
            "source_sha256": actual_hash,
            "template_version": str(item["template_version"]),
            "expected_assets": sorted(expected_assets),
            "section_order": list(item["section_order"]),
            "section_titles": titles,
        }
        planned["import_manifest.json"] = (
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        if target.exists():
            for name, content in planned.items():
                existing = target / name
                if (
                    existing.exists()
                    and existing.read_bytes() != content
                    and not refresh_changed
                ):
                    raise ValueError(
                        f"idempotent import refused to overwrite changed file: {existing}"
                    )
        target.mkdir(parents=True, exist_ok=True)
        for name, content in planned.items():
            destination = target / name
            if not destination.exists() or destination.read_bytes() != content:
                temporary = destination.with_name(destination.name + ".tmp")
                temporary.write_bytes(content)
                temporary.replace(destination)
        imported.append({**manifest, "normalized_template_dir": str(target)})
    return imported


def _reliability_base(job: dict[str, Any]) -> str:
    text = normalize_token(
        " ".join(
            [
                str(job.get("title") or job.get("position") or ""),
                str(job.get("description") or ""),
                " ".join(str(value) for value in job.get("secondary_role_tags", [])),
            ]
        )
    )
    validation_score = sum(1 for term in VALIDATION_TERMS if term in text)
    software_score = sum(1 for term in SOFTWARE_TERMS if term in text)
    return "test" if validation_score > 0 and validation_score >= software_score else "se"


def role_base_for_job(job: dict[str, Any], registry: dict[str, Any]) -> str:
    role_family = str(job.get("role_family") or "").strip()
    if role_family == "algorithm_research":
        raise ValueError("algorithm_research is excluded and cannot select an AI resume base")
    if role_family == "reliability":
        return _reliability_base(job)
    role_base = str((registry.get("role_mapping") or {}).get(role_family) or "")
    if not role_base:
        raise ValueError(f"no approved resume base mapping for canonical role_family={role_family!r}")
    return role_base


def _selected_language(job: dict[str, Any], language: str) -> str:
    requested = str(language or "AUTO").upper()
    if requested in {"CN", "EN"}:
        return requested
    configured = str(
        job.get("resume_language")
        or job.get("recommended_resume_language")
        or job.get("job_language")
        or ""
    ).upper()
    if configured in {"CN", "ZH", "CHINESE"}:
        return "CN"
    if configured in {"EN", "ENGLISH"}:
        return "EN"
    corpus = f"{job.get('title') or job.get('position') or ''} {job.get('description') or ''}"
    return "CN" if re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", corpus) else "EN"


def select_resume_base(
    job: dict[str, Any],
    registry: dict[str, Any],
    *,
    language: str = "AUTO",
    variant: str = "auto",
    allow_english_photo_fallback: bool | None = None,
) -> dict[str, Any]:
    selected_language = _selected_language(job, language)
    requested_variant = str(variant or "auto").lower()
    if requested_variant not in {"auto", "photo", "no_photo"}:
        raise ValueError("resume variant must be auto, photo or no_photo")
    defaults = registry.get("defaults") or {}
    selected_variant = (
        str(defaults.get("variant") or "no_photo")
        if requested_variant == "auto"
        else requested_variant
    )
    fallback_allowed = (
        bool(defaults.get("allow_english_photo_fallback", False))
        if allow_english_photo_fallback is None
        else bool(allow_english_photo_fallback)
    )
    fallback_used = False
    if selected_language == "EN" and selected_variant == "photo":
        if not fallback_allowed:
            raise ValueError(
                "EN + photo has no approved Golden Base; enable the explicit English photo fallback to use EN no-photo"
            )
        selected_variant = "no_photo"
        fallback_used = True
    role_base = role_base_for_job(job, registry)
    base_id = f"{role_base}.{selected_language.lower()}.{selected_variant}.v1"
    entry = copy.deepcopy((registry.get("bases") or {}).get(base_id) or {})
    if not entry or not entry.get("enabled"):
        raise ValueError(f"approved resume base is unavailable or disabled: {base_id}")
    entry["fallback_used"] = fallback_used
    entry["requested_variant"] = requested_variant
    entry["selected_variant"] = selected_variant
    entry["plan_role"] = BASE_PLAN_ROLES[role_base]
    return entry


def prepare_selected_template(
    selection: dict[str, Any],
    output_stem: Path,
    *,
    repo_root: str | Path,
) -> str:
    root = Path(repo_root).resolve()
    template_dir = _repo_path(root, selection["normalized_template_dir"])
    template_path = template_dir / "template.tex"
    if not template_path.is_file():
        raise FileNotFoundError(
            f"normalized Golden Base is missing for {selection['base_id']}; run import_resume_bases.py"
        )
    template = template_path.read_text(encoding="utf-8")
    if selection["photo_variant"] == "photo":
        source_image = template_dir / "headshot.jpg"
        if not source_image.is_file():
            raise FileNotFoundError(f"photo base asset is missing: {source_image}")
        output_stem.parent.mkdir(parents=True, exist_ok=True)
        image_name = f"{output_stem.name}-headshot.jpg"
        destination = output_stem.parent / image_name
        if not destination.exists() or destination.read_bytes() != source_image.read_bytes():
            shutil.copyfile(source_image, destination)
        template = template.replace("{{HEADSHOT_PATH}}", image_name)
    elif "{{HEADSHOT_PATH}}" in template or "headshot.jpg" in template:
        raise ValueError(f"no-photo base unexpectedly references a headshot: {selection['base_id']}")
    return template


def base_page_fit_contract(
    selection: dict[str, Any],
    defaults: dict[str, Any],
    fallback: dict[str, Any],
) -> dict[str, Any]:
    metrics = dict(selection.get("benchmark_metrics") or {})
    if not metrics:
        return copy.deepcopy(fallback)
    tolerance = defaults.get("density_tolerance") or {}
    span = float(metrics.get("text_vertical_span_ratio", 0.0))
    lines = int(metrics.get("non_empty_line_count", 0))
    contract: dict[str, Any] = {
        "benchmark_base_id": selection["base_id"],
        "golden_vertical_span_ratio": span,
        "min_vertical_span_ratio": max(0.0, span - float(tolerance.get("vertical_span_below", 0.04))),
        "max_vertical_span_ratio": min(1.0, span + float(tolerance.get("vertical_span_above", 0.02))),
        "acceptable_min_vertical_span_ratio": max(0.0, span - float(tolerance.get("vertical_span_below", 0.04))),
        "acceptable_max_vertical_span_ratio": min(1.0, span + float(tolerance.get("vertical_span_above", 0.02))),
        "min_lines": max(1, lines - int(tolerance.get("line_delta", 8))),
        "max_lines": lines + int(tolerance.get("line_delta", 8)),
    }
    ratio_below = float(tolerance.get("content_ratio_below", 0.80))
    ratio_above = float(tolerance.get("content_ratio_above", 1.20))
    if selection["language"] == "CN":
        count = int(metrics.get("cjk_character_count", 0))
        contract.update(
            min_cjk_chars=max(1, math.floor(count * ratio_below)),
            max_cjk_chars=max(1, math.ceil(count * ratio_above)),
        )
    else:
        count = int(metrics.get("english_word_count", 0))
        contract.update(
            min_words=max(1, math.floor(count * ratio_below)),
            max_words=max(1, math.ceil(count * ratio_above)),
        )
    return contract


def _all_group_intents(group: dict[str, Any]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for intent in list(group.get("intents", [])) + list(group.get("reserve_intents", [])):
        by_id[str(intent["intent_id"])] = intent
    return sorted(by_id.values(), key=lambda item: int(item.get("priority", 0)))


def apply_base_content_manifest(
    plan: dict[str, Any],
    base_role: str,
    content: dict[str, Any],
    tailoring_defaults: dict[str, Any],
) -> dict[str, Any]:
    """Constrain an evidence-backed fact plan to one approved role base."""
    result = copy.deepcopy(plan)
    manifest = copy.deepcopy(content["roles"][base_role])
    experience_index = {
        str(group["material"].get("id")): group
        for group in result.get("experience", [])
    }
    project_index = {
        str(group["material"].get("id")): group
        for group in list(result.get("projects", [])) + list(result.get("reserve_projects", []))
    }
    selected_experience: list[dict[str, Any]] = []
    selected_projects: list[dict[str, Any]] = []
    baseline_ids: list[str] = []
    source_wording_review: list[str] = []
    replacement_candidates: list[tuple[float, dict[str, Any], dict[str, Any]]] = []

    for entry in manifest["experience_entries"]:
        material_id = str(entry["material_id"])
        if material_id not in experience_index:
            raise ValueError(f"base {base_role} experience material is missing: {material_id}")
        group = experience_index[material_id]
        intents = _all_group_intents(group)
        count = int(entry["rendered_slots"])
        baseline = intents[:count]
        if len(baseline) != count:
            raise ValueError(f"base {base_role} lacks evidence-backed experience slots for {material_id}")
        for intent in intents:
            intent["variant"] = "standard"
            intent["reserve"] = intent not in baseline
        group["intents"] = baseline
        group["reserve_intents"] = [intent for intent in intents if intent not in baseline]
        selected_experience.append(group)
        baseline_ids.extend(str(intent["intent_id"]) for intent in baseline)
        for position in range(count, int(entry["source_slots"])):
            source_wording_review.append(f"experience:{material_id}:source_slot_{position + 1}")

    for entry in manifest["project_entries"]:
        material_id = str(entry["material_id"])
        if material_id not in project_index:
            raise ValueError(f"base {base_role} project material is missing: {material_id}")
        group = project_index[material_id]
        intents = _all_group_intents(group)
        count = int(entry["rendered_slots"])
        baseline = intents[:count]
        if len(baseline) != count:
            raise ValueError(f"base {base_role} lacks evidence-backed project slots for {material_id}")
        original_selected = {
            str(intent["intent_id"]) for intent in group.get("intents", [])
        }
        for intent in intents:
            intent["variant"] = "standard"
            intent["reserve"] = intent not in baseline
            if str(intent["intent_id"]) in original_selected and intent not in baseline:
                replacement_candidates.append(
                    (float(intent.get("page_value", 0.0)), group, intent)
                )
        group["intents"] = baseline
        group["reserve_intents"] = [intent for intent in intents if intent not in baseline]
        group["reserve"] = False
        selected_projects.append(group)
        baseline_ids.extend(str(intent["intent_id"]) for intent in baseline)
        for position in range(count, int(entry["source_slots"])):
            source_wording_review.append(f"project:{material_id}:source_slot_{position + 1}")

    skill_index = {str(row.get("group")): row for row in result.get("skills", [])}
    selected_skills = [skill_index[name] for name in manifest["skill_groups"] if name in skill_index]
    if len(selected_skills) != len(manifest["skill_groups"]):
        missing = sorted(set(manifest["skill_groups"]) - set(skill_index))
        raise ValueError(f"base {base_role} lacks evidence-backed skill rows: {missing}")
    baseline_ids.extend(str(row["intent_id"]) for row in selected_skills)

    maximum_ratio = float(tailoring_defaults.get("maximum_changed_ratio", 0.30))
    max_changes = min(
        int(tailoring_defaults.get("normal_replacement_max", 4)),
        math.floor(len(baseline_ids) * maximum_ratio),
    )
    changes = 0
    for _, group, candidate in sorted(replacement_candidates, key=lambda item: -item[0]):
        if changes >= max_changes:
            break
        active = group["intents"]
        replaceable = min(active, key=lambda intent: float(intent.get("page_value", 0.0)))
        if float(candidate.get("page_value", 0.0)) <= float(replaceable.get("page_value", 0.0)):
            continue
        active.remove(replaceable)
        active.append(candidate)
        active.sort(key=lambda intent: int(intent.get("priority", 0)))
        group["reserve_intents"].remove(candidate)
        group["reserve_intents"].append(replaceable)
        candidate["reserve"] = False
        replaceable["reserve"] = True
        changes += 1

    result["experience"] = selected_experience
    result["projects"] = selected_projects
    result["reserve_projects"] = [
        project_index[item_id]
        for item_id in manifest.get("reserve_projects", [])
        if item_id in project_index and project_index[item_id] not in selected_projects
    ]
    result["skills"] = selected_skills
    result["base_role"] = base_role
    result["base_content_manifest"] = manifest
    result["base_baseline_slot_ids"] = baseline_ids
    result["source_wording_review_slots"] = source_wording_review
    result["tailoring_policy"] = {
        "minimum_preserved_ratio": float(tailoring_defaults.get("minimum_preserved_ratio", 0.70)),
        "maximum_changed_ratio": maximum_ratio,
        "normal_replacement_min": int(tailoring_defaults.get("normal_replacement_min", 2)),
        "normal_replacement_max": int(tailoring_defaults.get("normal_replacement_max", 4)),
    }
    return result


def _current_slot_ids(plan: dict[str, Any]) -> list[str]:
    slots = [
        str(intent["intent_id"])
        for section in ("experience", "projects")
        for group in plan.get(section, [])
        for intent in group.get("intents", [])
    ]
    slots.extend(str(row["intent_id"]) for row in plan.get("skills", []))
    return slots


def build_tailoring_report(
    plan: dict[str, Any],
    *,
    selected_base: dict[str, Any],
    semantic_review_cache_identity: str = "",
) -> dict[str, Any]:
    baseline = list(plan.get("base_baseline_slot_ids", []))
    current = _current_slot_ids(plan)
    baseline_set = set(baseline)
    current_set = set(current)
    unchanged = [slot for slot in baseline if slot in current_set]
    removed = [slot for slot in baseline if slot not in current_set]
    added = [slot for slot in current if slot not in baseline_set]
    reordered = [
        slot for slot in unchanged
        if baseline.index(slot) != current.index(slot)
    ]
    variant_changes = [
        {
            "slot_id": str(intent["intent_id"]),
            "variant": str(intent.get("variant") or "standard"),
            "reason": "base-specific page-fit adjustment",
        }
        for section in ("experience", "projects")
        for group in plan.get(section, [])
        for intent in group.get("intents", [])
        if str(intent.get("variant") or "standard") != "standard"
    ]
    changed_slots = set(removed) | {item["slot_id"] for item in variant_changes}
    changed_slots.update(reordered)
    percentage = len(changed_slots) / max(1, len(baseline))
    maximum = float(plan.get("tailoring_policy", {}).get("maximum_changed_ratio", 0.30))
    preserved = 1.0 - (len(removed) / max(1, len(baseline)))
    minimum = float(plan.get("tailoring_policy", {}).get("minimum_preserved_ratio", 0.70))
    status = (
        "PASS"
        if percentage <= maximum + 1e-9 and preserved >= minimum - 1e-9
        else "HUMAN_REVIEW_REQUIRED"
    )
    return {
        "selected_base_id": selected_base["base_id"],
        "selected_evidence_ids": sorted(
            {
                str(evidence_id)
                for section in ("experience", "projects")
                for group in plan.get(section, [])
                for intent in group.get("intents", [])
                for evidence_id in intent.get("evidence_ids", [])
            }
        ),
        "unchanged_slots": unchanged,
        "reordered_slots": reordered,
        "replaced_slots": [
            {
                "removed": removed[index] if index < len(removed) else "",
                "added": added[index] if index < len(added) else "",
                "reason": "JD evidence relevance within approved tailoring budget",
            }
            for index in range(max(len(removed), len(added)))
        ],
        "wording_variants_changed": variant_changes,
        "tailoring_percentage": round(percentage, 4),
        "preserved_ratio": round(preserved, 4),
        "maximum_changed_ratio": maximum,
        "minimum_preserved_ratio": minimum,
        "semantic_review_cache_identity": semantic_review_cache_identity,
        "source_wording_review_slots": list(plan.get("source_wording_review_slots", [])),
        "status": status,
    }


def semantic_cache_identity(review: dict[str, Any]) -> str:
    for key in ("input_hash", "cache_identity", "review_hash"):
        if review.get(key):
            return str(review[key])
    stable = {
        key: review.get(key)
        for key in ("input_job_canonical_key", "provider", "model", "prompt_version")
        if review.get(key)
    }
    if not stable:
        return ""
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()
