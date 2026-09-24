#!/usr/bin/env python3
"""Discover and shallow-sync a bounded set of candidate-relevant GitHub repos."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

try:
    from common import load_config, normalize_token, write_json
except ModuleNotFoundError:
    from scripts.common import load_config, normalize_token, write_json


RELEVANCE_TERMS = (
    "software",
    "backend",
    "api",
    "test",
    "automation",
    "reliability",
    "ai",
    "rag",
    "llm",
    "agent",
    "robot",
    "ros",
    "python",
    "c++",
    "cpp",
    "java",
    "platform",
    "sre",
)
LOW_VALUE_TERMS = ("portfolio", "notes", "notebook", "github io", "static site", "blog")
ENGINEERING_LANGUAGES = {
    "python",
    "java",
    "c++",
    "c",
    "go",
    "rust",
    "typescript",
    "javascript",
    "c#",
}


def github_owner(profile_url: str) -> str:
    parsed = urlparse(profile_url.strip())
    if parsed.netloc.casefold() not in {"github.com", "www.github.com"}:
        raise ValueError("github_profile_url must be a github.com profile URL")
    owner = parsed.path.strip("/").split("/")[0]
    if not re.fullmatch(r"[A-Za-z0-9-]+", owner):
        raise ValueError("github_profile_url does not contain a valid account name")
    return owner


def discover_public_repositories(profile_url: str) -> list[dict[str, Any]]:
    owner = github_owner(profile_url)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "jobhunter-candidate-inventory/1.0",
    }
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = requests.get(
        f"https://api.github.com/users/{owner}/repos",
        params={"per_page": 100, "type": "owner", "sort": "updated"},
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("GitHub repositories response was not a list")
    return [item for item in payload if isinstance(item, dict)]


def repository_relevance(repo: dict[str, Any]) -> int:
    if repo.get("archived") or repo.get("disabled") or repo.get("fork"):
        return -100
    corpus = normalize_token(
        " ".join(
            [
                str(repo.get("name") or ""),
                str(repo.get("description") or ""),
                " ".join(str(item) for item in repo.get("topics", []) if item),
            ]
        )
    )
    score = sum(2 for term in RELEVANCE_TERMS if normalize_token(term) in corpus)
    language = normalize_token(str(repo.get("language") or ""))
    if language in ENGINEERING_LANGUAGES:
        score += 2
    score -= sum(3 for term in LOW_VALUE_TERMS if normalize_token(term) in corpus)
    if str(repo.get("name") or "").casefold().endswith(".github.io"):
        score -= 8
    return score


def select_candidate_repositories(
    repositories: list[dict[str, Any]],
    *,
    max_repositories: int = 12,
    excluded_names: set[str] | None = None,
) -> list[dict[str, Any]]:
    excluded = {name.casefold() for name in (excluded_names or set())}
    scored = [
        (repository_relevance(repo), str(repo.get("updated_at") or ""), repo)
        for repo in repositories
        if str(repo.get("name") or "").casefold() not in excluded
    ]
    selected = [item for item in scored if item[0] > 0]
    selected.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in selected[:max_repositories]]


def _explicit_repository(value: Any, owner: str) -> dict[str, Any] | None:
    if isinstance(value, dict):
        url = str(value.get("clone_url") or value.get("url") or "").strip()
        name = str(value.get("name") or Path(urlparse(url).path).stem).strip()
    else:
        text = str(value or "").strip()
        if not text:
            return None
        url = text if "://" in text else f"https://github.com/{owner}/{text}.git"
        name = Path(urlparse(url).path).stem
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        return None
    return {"name": name, "clone_url": url, "explicit": True}


def sync_repository(repo: dict[str, Any], cache_dir: Path) -> Path:
    name = str(repo.get("name") or "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError(f"Unsafe repository name: {name!r}")
    clone_url = str(repo.get("clone_url") or repo.get("html_url") or "").strip()
    if not clone_url:
        raise ValueError(f"Repository {name} has no clone URL")
    target = cache_dir / name
    cache_dir.mkdir(parents=True, exist_ok=True)
    if (target / ".git").is_dir():
        subprocess.run(
            ["git", "-C", str(target), "pull", "--ff-only", "--depth", "1"],
            check=True,
        )
    elif target.exists():
        raise ValueError(f"Refusing to overwrite non-Git cache path: {target}")
    else:
        subprocess.run(
            ["git", "clone", "--depth", "1", clone_url, str(target)],
            check=True,
        )
    return target


def cached_repositories(cache_dir: Path) -> list[dict[str, Any]]:
    if not cache_dir.exists():
        return []
    return [
        {"name": path.name, "path": str(path), "sync_status": "cached"}
        for path in sorted(cache_dir.iterdir())
        if path.is_dir() and (path / ".git").is_dir()
    ]


def sync_from_config(
    targets: dict[str, Any], *, offline: bool = False, max_repositories: int = 12
) -> dict[str, Any]:
    profile = targets.get("candidate_profile", {})
    profile_url = str(profile.get("github_profile_url") or "").strip()
    excluded_names = {
        str(name).strip().casefold()
        for name in profile.get("github_repository_exclude_names", [])
        if str(name).strip()
    }
    cache_dir = Path(
        targets.get("output", {}).get("candidate_repo_cache_dir", "data/candidate_repos")
    )
    if offline:
        repos = [
            repo
            for repo in cached_repositories(cache_dir)
            if str(repo.get("name") or "").casefold() not in excluded_names
        ]
        return {"github_profile_url": profile_url, "offline": True, "repositories": repos}
    if not profile_url:
        return {"github_profile_url": "", "offline": False, "repositories": []}

    owner = github_owner(profile_url)
    discovered = discover_public_repositories(profile_url)
    selected = select_candidate_repositories(
        discovered,
        max_repositories=max_repositories,
        excluded_names=excluded_names,
    )
    by_name = {str(repo.get("name")): repo for repo in selected if repo.get("name")}
    for value in profile.get("github_repositories", []):
        explicit = _explicit_repository(value, owner)
        if explicit and explicit["name"].casefold() not in excluded_names:
            by_name[explicit["name"]] = {**by_name.get(explicit["name"], {}), **explicit}

    synced: list[dict[str, Any]] = []
    for repo in list(by_name.values())[:max_repositories]:
        try:
            path = sync_repository(repo, cache_dir)
            synced.append(
                {
                    "name": repo.get("name", ""),
                    "path": str(path),
                    "html_url": repo.get("html_url", ""),
                    "description": repo.get("description", ""),
                    "language": repo.get("language", ""),
                    "relevance_score": repository_relevance(repo),
                    "sync_status": "synced",
                }
            )
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            synced.append(
                {
                    "name": repo.get("name", ""),
                    "path": "",
                    "sync_status": "failed",
                    "error": str(exc),
                }
            )
    return {"github_profile_url": profile_url, "offline": False, "repositories": synced}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync candidate-relevant GitHub repositories.")
    parser.add_argument("--config", default="config/targets.yaml")
    parser.add_argument("--output", default="data/candidate_repos/manifest.json")
    parser.add_argument("--offline", action="store_true", help="Use existing cached clones only")
    parser.add_argument("--max-repositories", type=int, default=12)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest = sync_from_config(
        load_config(args.config),
        offline=args.offline,
        max_repositories=args.max_repositories,
    )
    write_json(Path(args.output), manifest)
    synced = sum(1 for item in manifest["repositories"] if item.get("path"))
    print(f"Candidate repositories: available={synced}, manifest={args.output}")


if __name__ == "__main__":
    main()
