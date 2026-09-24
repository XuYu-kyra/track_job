#!/usr/bin/env python3
"""Build a candidate skill/project inventory from verified materials and repos."""

from __future__ import annotations

import argparse
from pathlib import Path, PurePath
from typing import Any

try:
    from common import load_config, normalize_token, read_json, write_json
except ModuleNotFoundError:
    from scripts.common import load_config, normalize_token, read_json, write_json


IGNORED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    "vendor",
    "third_party",
    "third-party",
}
DEPENDENCY_FILES = {
    "requirements.txt",
    "pyproject.toml",
    "package.json",
    "pom.xml",
    "build.gradle",
    "cargo.toml",
    "go.mod",
}
SOURCE_SUFFIXES = {
    ".py",
    ".java",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".go",
    ".rs",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".cs",
    ".sh",
}
CONFIG_SUFFIXES = {".yaml", ".yml", ".json", ".toml", ".xml"}
PROFICIENCY_ORDER = {"strong": 0, "working": 1, "exposure": 2}


def _alias_matches(alias: str, normalized_corpus: str, raw_corpus: str = "") -> bool:
    if "+" in alias:
        return alias.casefold() in raw_corpus.casefold()
    normalized_alias = normalize_token(alias)
    if len(normalized_alias) < 2:
        return False
    return f" {normalized_alias} " in f" {normalized_corpus} "


def relative_evidence_path(path: PurePath, repo_path: PurePath) -> str:
    return path.relative_to(repo_path).as_posix()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build candidate evidence inventory.")
    parser.add_argument("--targets-config", default="config/targets.yaml")
    parser.add_argument("--taxonomy-config", default="config/taxonomies.yaml")
    parser.add_argument("--materials-dir", default="cv/materials")
    parser.add_argument("--repo-manifest", default="data/candidate_repos/manifest.json")
    parser.add_argument("--output", default="data/job_cache/candidate_inventory.json")
    return parser.parse_args()


def _material_terms(materials_dir: Path) -> list[tuple[str, str, str]]:
    canonical_path = materials_dir / "evidence.yaml"
    if canonical_path.exists():
        payload = load_config(canonical_path)
        items = payload.get("evidence", []) if isinstance(payload, dict) else []
        evidence: list[tuple[str, str, str]] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            source_class = str(item.get("source_class") or "UNCERTAIN").upper()
            if source_class == "UNCERTAIN":
                continue
            kind = {
                "VERIFIED": "verified_material",
                "USER_ATTESTED": "user_attested_material",
                "DERIVED_RESUME_SAFE": "derived_resume_safe",
            }.get(source_class)
            if not kind:
                continue
            evidence_id = str(item.get("evidence_id") or "unknown")
            for technology in item.get("technologies", []):
                term = str(technology).strip()
                if term:
                    evidence.append((term, f"evidence.yaml#{evidence_id}", kind))
        return evidence

    legacy_evidence: list[tuple[str, str]] = []
    for file_name in ("skills.yaml", "projects.yaml", "experience.yaml"):
        path = materials_dir / file_name
        if not path.exists():
            continue
        payload = load_config(path)
        collect_material_evidence(payload, file_name, legacy_evidence)
    return [(term, source, "verified_material") for term, source in legacy_evidence]


def _material_skill_claims(materials_dir: Path) -> list[dict[str, Any]]:
    path = materials_dir / "skills.yaml"
    if not path.exists():
        return []
    payload = load_config(path)
    groups = payload.get("skills", []) if isinstance(payload, dict) else []
    if not isinstance(groups, dict):
        return []
    claims: list[dict[str, Any]] = []
    for role_family, items in groups.items():
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            claims.append(
                {
                    "name": name,
                    "role_family": str(role_family),
                    "proficiency": str(item.get("proficiency") or "exposure"),
                    "source_class": str(item.get("source_class") or "UNCERTAIN").upper(),
                    "evidence_ids": [str(value) for value in item.get("evidence_ids", [])],
                }
            )
    return claims


def collect_material_evidence(
    value: Any,
    source: str,
    evidence: list[tuple[str, str]],
    key: str = "",
) -> None:
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            collect_material_evidence(child_value, source, evidence, str(child_key))
        return
    if isinstance(value, list):
        for child in value:
            collect_material_evidence(child, source, evidence, key)
        return
    if key in {"tags", "skills"} or source == "skills.yaml":
        text = str(value).strip()
        if text:
            evidence.append((text, f"materials:{source}"))


def _repo_files(repo_path: Path) -> list[Path]:
    files: list[Path] = []
    if not repo_path.exists() or not repo_path.is_dir():
        return files
    for path in repo_path.rglob("*"):
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        if path.is_file():
            files.append(path)
        if len(files) >= 3000:
            break
    return files


def _evidence_kind(path: Path, repo_path: Path) -> str:
    relative = path.relative_to(repo_path)
    lowered_parts = [part.casefold() for part in relative.parts]
    name = path.name.casefold()
    if name.startswith("readme"):
        return "readme"
    if name in DEPENDENCY_FILES:
        return "dependency"
    if ".github" in lowered_parts and "workflows" in lowered_parts:
        return "ci"
    if "test" in name or any(part in {"test", "tests"} for part in lowered_parts):
        return "test"
    if name in {"package.xml", "colcon.meta"}:
        return "ros_metadata"
    if any(part in {"launch", "config"} for part in lowered_parts):
        return "launch_config"
    if name.startswith(("openapi", "swagger")) or name.endswith((".proto", ".graphql")):
        return "api_definition"
    if path.suffix.casefold() in SOURCE_SUFFIXES:
        return "source"
    return "config"


def _readable_evidence_file(path: Path, kind: str) -> bool:
    if path.stat().st_size > 256_000:
        return False
    return kind in {
        "readme",
        "dependency",
        "ci",
        "test",
        "ros_metadata",
        "launch_config",
        "api_definition",
        "source",
    } or path.suffix.casefold() in CONFIG_SUFFIXES


def _skill_level(details: list[dict[str, str]]) -> str:
    kinds = {item["kind"] for item in details}
    implementation = [
        item
        for item in details
        if item["kind"] in {"source", "test", "api_definition", "ros_metadata", "launch_config"}
    ]
    if len({item["file"] for item in implementation}) >= 3 and kinds & {
        "test",
        "ci",
        "api_definition",
        "ros_metadata",
    }:
        return "STRONG"
    if implementation:
        return "USED"
    if kinds & {"verified_material", "derived_resume_safe", "user_attested_material"} or (
        {"readme", "dependency"} <= kinds
    ):
        return "FAMILIAR"
    return "MENTION_ONLY"


def inspect_repo(repo_path: Path, skill_aliases: dict[str, list[str]]) -> dict[str, Any]:
    files = _repo_files(repo_path)
    evidence_by_skill: dict[str, list[dict[str, str]]] = {}
    for path in files:
        kind = _evidence_kind(path, repo_path)
        if not _readable_evidence_file(path, kind):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        relative = relative_evidence_path(path, repo_path)
        raw_corpus = f"{relative} {content}"
        corpus = normalize_token(raw_corpus)
        for skill, aliases in skill_aliases.items():
            if any(_alias_matches(alias, corpus, raw_corpus) for alias in aliases):
                evidence_kind = kind
                if skill == "cpp" and path.suffix.casefold() not in {
                    ".c",
                    ".cc",
                    ".cpp",
                    ".h",
                    ".hpp",
                }:
                    evidence_kind = "dependency"
                evidence_by_skill.setdefault(skill, []).append(
                    {"repo": repo_path.name, "file": relative, "kind": evidence_kind}
                )

    test_files = sum(1 for path in files if _evidence_kind(path, repo_path) == "test")
    has_ci = any(_evidence_kind(path, repo_path) == "ci" for path in files)
    has_readme = any(_evidence_kind(path, repo_path) == "readme" for path in files)
    has_container = any(
        path.name.casefold() in {"dockerfile", "compose.yaml", "docker-compose.yml"}
        for path in files
    )
    architecture_signals = sum(
        1
        for path in files
        if _evidence_kind(path, repo_path)
        in {"test", "ci", "api_definition", "ros_metadata", "launch_config"}
    )
    implementation_evidence = sum(
        1
        for details in evidence_by_skill.values()
        for item in details
        if item["kind"] in {"source", "test", "api_definition", "ros_metadata", "launch_config"}
    )
    candidate_relevant = implementation_evidence > 0 or architecture_signals >= 2
    strength = min(
        100,
        15
        + min(len(files), 100) // 5
        + min(test_files, 10) * 4
        + int(has_ci) * 15
        + int(has_readme) * 8
        + int(has_container) * 10
        + min(architecture_signals, 5) * 3,
    )
    return {
        "name": repo_path.name,
        "path": str(repo_path),
        "skills": sorted(evidence_by_skill),
        "skill_evidence": evidence_by_skill,
        "file_count": len(files),
        "test_file_count": test_files,
        "has_ci": has_ci,
        "has_readme": has_readme,
        "has_container_config": has_container,
        "candidate_relevant": candidate_relevant,
        "project_strength": strength,
        "evidence_files": sorted(
            {
                item["file"]
                for details in evidence_by_skill.values()
                for item in details
            }
        ),
    }


def _curated_manifest_repositories(materials_dir: Path) -> set[str]:
    path = materials_dir / "projects.yaml"
    if not path.exists():
        return set()
    payload = load_config(path)
    projects = payload.get("projects", []) if isinstance(payload, dict) else []
    return {
        str(project.get("repo_name") or "").strip().casefold()
        for project in projects
        if isinstance(project, dict)
        and bool(project.get("evidence_scan", True))
        and str(project.get("repo_name") or "").strip()
    }


def _configured_repo_paths(
    targets: dict[str, Any],
    manifest_path: Path,
    *,
    manifest_allowlist: set[str] | None = None,
) -> list[Path]:
    values = list(targets.get("candidate_profile", {}).get("repo_paths", []))
    excluded_names = {
        str(name).strip().casefold()
        for name in targets.get("candidate_profile", {}).get(
            "github_repository_exclude_names", []
        )
        if str(name).strip()
    }
    manifest = read_json(manifest_path, {})
    if isinstance(manifest, dict):
        for repo in manifest.get("repositories", []):
            repo_name = str(repo.get("name") or "").strip().casefold() if isinstance(repo, dict) else ""
            allowed = (
                (not manifest_allowlist or repo_name in manifest_allowlist)
                and repo_name not in excluded_names
            )
            if isinstance(repo, dict) and repo.get("path") and allowed:
                portable = Path(str(repo["path"]).replace("\\", "/"))
                local_sibling = manifest_path.parent / str(repo.get("name") or "")
                values.append(local_sibling if not portable.exists() and local_sibling.exists() else portable)
    paths: list[Path] = []
    seen: set[str] = set()
    for value in values:
        path = Path(str(value).replace("\\", "/")).expanduser()
        key = str(path.resolve()) if path.exists() else str(path)
        if key not in seen:
            seen.add(key)
            paths.append(path)
    return paths


def build_inventory(
    targets: dict[str, Any],
    taxonomies: dict[str, Any],
    materials_dir: Path,
    repo_manifest: Path | None = None,
) -> dict[str, Any]:
    aliases = {
        skill: [str(item) for item in spec.get("aliases", [])]
        for skill, spec in taxonomies.get("skill_taxonomy", {}).items()
    }
    skill_details: dict[str, list[dict[str, str]]] = {skill: [] for skill in aliases}
    material_claims: dict[str, list[dict[str, Any]]] = {skill: [] for skill in aliases}
    for claim in _material_skill_claims(materials_dir):
        normalized_name = normalize_token(str(claim["name"]))
        for skill, skill_aliases in aliases.items():
            if any(
                _alias_matches(alias, normalized_name, str(claim["name"]))
                for alias in skill_aliases
            ):
                material_claims[skill].append(claim)
    for term, source, kind in _material_terms(materials_dir):
        normalized_term = normalize_token(term)
        for skill, skill_aliases in aliases.items():
            if any(_alias_matches(alias, normalized_term, term) for alias in skill_aliases):
                skill_details[skill].append(
                    {"repo": "materials", "file": source, "kind": kind}
                )
    curated_skills = {
        skill for skill, details in skill_details.items() if details
    }

    manifest_path = repo_manifest or Path("data/candidate_repos/manifest.json")
    projects = [
        inspect_repo(path, aliases)
        for path in _configured_repo_paths(
            targets,
            manifest_path,
            manifest_allowlist=_curated_manifest_repositories(materials_dir),
        )
    ]
    for project in projects:
        if not project["candidate_relevant"]:
            continue
        for skill, details in project["skill_evidence"].items():
            if skill in curated_skills:
                skill_details.setdefault(skill, []).extend(details)

    skills = []
    for skill, details in skill_details.items():
        unique = {
            (item["repo"], item["file"], item["kind"]): item
            for item in details
        }
        evidence_details = sorted(
            unique.values(), key=lambda item: (item["repo"], item["file"], item["kind"])
        )
        if not evidence_details:
            continue
        claims = material_claims.get(skill, [])
        proficiency = min(
            (str(item["proficiency"]) for item in claims),
            key=lambda value: PROFICIENCY_ORDER.get(value, 99),
            default="",
        )
        skills.append(
            {
                "skill": skill,
                "level": _skill_level(evidence_details),
                "proficiency": proficiency,
                "source_classes": sorted(
                    {str(item["source_class"]) for item in claims}
                ),
                "material_evidence_ids": sorted(
                    {
                        evidence_id
                        for item in claims
                        for evidence_id in item["evidence_ids"]
                    }
                ),
                "suitable_role_families": sorted(
                    {str(item["role_family"]) for item in claims}
                ),
                "evidence_count": len(evidence_details),
                "evidence": [
                    f"{item['repo']}:{item['file']}:{item['kind']}" for item in evidence_details
                ],
                "evidence_details": evidence_details,
            }
        )
    level_order = {"STRONG": 0, "USED": 1, "FAMILIAR": 2, "MENTION_ONLY": 3}
    skills.sort(
        key=lambda item: (
            level_order.get(str(item["level"]), 9),
            -int(item["evidence_count"]),
            str(item["skill"]),
        )
    )
    return {
        "skills": skills,
        "projects": projects,
        "summary": {
            "canonical_skill_count": len(skills),
            "project_count": len(projects),
            "candidate_relevant_project_count": sum(
                1 for project in projects if project["candidate_relevant"]
            ),
            "strong_project_count": sum(
                1
                for project in projects
                if project["candidate_relevant"] and project["project_strength"] >= 70
            ),
        },
    }


def main() -> None:
    args = parse_args()
    targets_path = Path(args.targets_config)
    targets = load_config(targets_path) if targets_path.exists() else {}
    taxonomies = load_config(args.taxonomy_config)
    inventory = build_inventory(
        targets,
        taxonomies,
        Path(args.materials_dir),
        Path(args.repo_manifest),
    )
    write_json(args.output, inventory)
    print(
        "Candidate inventory: "
        f"skills={inventory['summary']['canonical_skill_count']}, "
        f"projects={inventory['summary']['project_count']} -> {args.output}"
    )


if __name__ == "__main__":
    main()
