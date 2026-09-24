#!/usr/bin/env python3
"""Check the repository for likely privacy leaks while allowing safe placeholders."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


TEXT_EXTENSIONS = {
    ".md",
    ".txt",
    ".py",
    ".yaml",
    ".yml",
    ".tex",
    ".toml",
    ".json",
    ".gitignore",
}

IGNORED_PARTS = {
    ".git",
    ".tools",
    "__pycache__",
    "cv/generated",
    # Imported Golden Bases preserve private immutable source text beside a
    # sanitized marker template. The directory is Git-ignored and governed by
    # SHA-256/import checks instead of the public-tree text scan.
    "cv/templates/bases",
    # All job caches are mutable, Git-ignored runtime state. Tracked/indexed or
    # historical copies are still inspected by the separate Git scans below.
    "data/job_cache",
    # Read-only shallow clones are external evidence inputs, not content owned by
    # this repository. Their own histories and contact metadata must not make the
    # jobhunter working-tree privacy gate fail.
    "data/candidate_repos",
}

PRIVATE_LOCAL_FILES = {
    "config/targets.yaml",
    "config/feishu.yaml",
    "config/profile.yaml",
    "config/execution.yaml",
    "config/company_registry.yaml",
    "data/job_cache/manual_job_inputs.txt",
}

ALLOWED_EMAILS = {
    "you@example.com",
    "cn-you@example.com",
    "intl-you@example.com",
}

ALLOWED_LINKEDIN_URLS = {
    "https://www.linkedin.com/in/your-profile",
}

ALLOWED_PORTAL_TOKENS = {
    "YOUR_FEISHU_APP_ID",
    "YOUR_FEISHU_APP_SECRET",
    "YOUR_BITABLE_APP_TOKEN",
    "YOUR_TABLE_ID",
    "YOUR_VIEW_ID",
}

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
LINKEDIN_RE = re.compile(r"https://www\.linkedin\.com/in/[A-Za-z0-9\-_%]+")
PHONE_RE = re.compile(r"\+\d[\d\s\-()]{7,}\d")
ALLOWED_PHONES = {
    "+44 0000 000000",
    "+86 10000000000",
    "+852 50000000",
    "+44 7000000000",
}
FEISHU_RE = re.compile(
    r'(?P<key>app_id|app_secret|app_token|table_id|view_id):\s*"(?P<value>[^"\n]+)"'
)
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")
GITHUB_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"
)
OPENAI_TOKEN_RE = re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")
AWS_ACCESS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?im)^\s*['\"]?(?P<key>app_secret|feishu_app_secret|aws_secret_access_key|"
    r"openai_api_key|deepseek_api_key|github_token)['\"]?\s*[:=]\s*['\"]?(?P<value>[A-Za-z0-9_+/=-]{8,})"
)


def is_text_path(path: Path) -> bool:
    if path.name in {".gitignore", "Makefile"}:
        return True
    return path.suffix in TEXT_EXTENSIONS


def should_skip(path: Path) -> bool:
    as_posix = path.as_posix()
    if any(as_posix.endswith(value) for value in PRIVATE_LOCAL_FILES):
        return True
    return any(part in as_posix for part in IGNORED_PARTS)


def iter_text_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if should_skip(path):
            continue
        if is_text_path(path):
            files.append(path)
    return files


def scan_text(path: Path, text: str) -> list[str]:
    findings: list[str] = []

    for match in EMAIL_RE.finditer(text):
        value = match.group(0)
        if value not in ALLOWED_EMAILS:
            findings.append("unexpected email address")

    for match in LINKEDIN_RE.finditer(text):
        value = match.group(0)
        if value not in ALLOWED_LINKEDIN_URLS:
            findings.append("unexpected LinkedIn profile URL")

    for match in PHONE_RE.finditer(text):
        value = match.group(0)
        if value not in ALLOWED_PHONES:
            findings.append("unexpected phone number")

    if path.suffix in {".yaml", ".yml"} and "feishu" in path.name:
        for match in FEISHU_RE.finditer(text):
            value = match.group("value").strip()
            if value and value not in ALLOWED_PORTAL_TOKENS:
                findings.append(f"unexpected {match.group('key')} value")

    credential_patterns = (
        (PRIVATE_KEY_RE, "private key material"),
        (GITHUB_TOKEN_RE, "GitHub credential"),
        (OPENAI_TOKEN_RE, "OpenAI credential"),
        (AWS_ACCESS_KEY_RE, "AWS access key"),
    )
    for pattern, label in credential_patterns:
        if pattern.search(text):
            findings.append(label)
    for match in SENSITIVE_ASSIGNMENT_RE.finditer(text):
        value = match.group("value").strip()
        if value not in ALLOWED_PORTAL_TOKENS and not value.startswith("YOUR_"):
            findings.append(f"non-placeholder {match.group('key')} assignment")

    return list(dict.fromkeys(findings))


def scan_file(path: Path) -> list[str]:
    return scan_text(path, path.read_text(encoding="utf-8"))


def _git_args(repo_root: Path, *args: str) -> list[str]:
    resolved = str(repo_root.resolve())
    # Git for Windows 2.37 can compare repository ownership against a path form
    # that differs from Python's resolved spelling. The wildcard is scoped to
    # this one exact `git -C <repo>` process; it does not touch global config.
    return [
        "git",
        "-c",
        "safe.directory=*",
        "-c",
        f"safe.directory={resolved}",
        "-C",
        resolved,
        *args,
    ]


def _run_git(
    repo_root: Path,
    *args: str,
) -> tuple[subprocess.CompletedProcess[str] | None, str]:
    try:
        result = subprocess.run(
            _git_args(repo_root, *args),
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
    except FileNotFoundError:
        return None, "Git executable was not found; repository privacy scan is inconclusive"
    except OSError as exc:
        return None, f"Git privacy scan could not execute: {exc}"
    if result.returncode != 0:
        reason = (result.stderr or result.stdout or "unknown Git error").strip()
        if "dubious ownership" in reason.casefold() or "safe.directory" in reason.casefold():
            reason = f"Git safe-directory/dubious-ownership error: {reason}"
        return None, f"Git privacy scan failed with exit {result.returncode}: {reason}"
    return result, ""


def inspect_git_index(repo_root: Path) -> tuple[list[str], str]:
    result, error = _run_git(repo_root, "ls-files", "--cached")
    if error or result is None:
        return [], error
    tracked = {
        line.strip().replace("\\", "/")
        for line in result.stdout.splitlines()
        if line.strip()
    }
    return sorted(tracked.intersection(PRIVATE_LOCAL_FILES)), ""


def inspect_git_index_contents(repo_root: Path) -> tuple[list[str], str]:
    paths_result, error = _run_git(repo_root, "ls-files", "--cached")
    if error or paths_result is None:
        return [], error
    findings: list[str] = []
    for raw_path in paths_result.stdout.splitlines():
        path = raw_path.strip().replace("\\", "/")
        if not path:
            continue
        blob_result, blob_error = _run_git(repo_root, "show", f":{path}")
        if blob_error or blob_result is None:
            return [], blob_error
        if "\0" in blob_result.stdout:
            continue
        for finding in scan_text(Path(path), blob_result.stdout):
            findings.append(f"index:{path}: {finding}")
    return list(dict.fromkeys(findings)), ""


def inspect_git_history(repo_root: Path) -> tuple[list[str], list[str], str]:
    objects_result, error = _run_git(repo_root, "rev-list", "--objects", "--all")
    if error or objects_result is None:
        return [], [], error

    paths_by_object: dict[str, set[str]] = {}
    for line in objects_result.stdout.splitlines():
        object_id, separator, raw_path = line.partition(" ")
        if not separator or not raw_path:
            continue
        paths_by_object.setdefault(object_id, set()).add(raw_path.replace("\\", "/"))

    findings: list[str] = []
    warnings: list[str] = []
    historical_manual_seen = False
    historical_manual_sensitive = False
    for object_id, paths in paths_by_object.items():
        type_result, type_error = _run_git(repo_root, "cat-file", "-t", object_id)
        if type_error or type_result is None:
            return [], [], type_error
        if type_result.stdout.strip() != "blob":
            continue
        blob_result, blob_error = _run_git(repo_root, "cat-file", "blob", object_id)
        if blob_error or blob_result is None:
            return [], [], blob_error
        if "\0" in blob_result.stdout:
            continue
        for path in sorted(paths):
            blob_findings = scan_text(Path(path), blob_result.stdout)
            if path == "data/job_cache/manual_job_inputs.txt":
                historical_manual_seen = True
                historical_manual_sensitive = historical_manual_sensitive or bool(blob_findings)
            elif path in PRIVATE_LOCAL_FILES:
                findings.append(
                    f"history:{path}@{object_id[:12]}: forbidden sensitive local path is reachable"
                )
            for finding in blob_findings:
                findings.append(f"history:{path}@{object_id[:12]}: {finding}")

    if historical_manual_seen and not historical_manual_sensitive:
        warnings.append(
            "historical data/job_cache/manual_job_inputs.txt is reachable; "
            "its reachable blobs contain no detected credentials or PII"
        )
    return list(dict.fromkeys(findings)), warnings, ""


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    all_findings: list[str] = []
    tracked_runtime, git_error = inspect_git_index(repo_root)
    if git_error:
        print(f"PRIVACY CHECK INCONCLUSIVE: {git_error}")
        return 2
    if tracked_runtime:
        all_findings.extend(
            f"index:{path}: sensitive local/runtime path must not be tracked by Git"
            for path in tracked_runtime
        )
    index_findings, index_error = inspect_git_index_contents(repo_root)
    if index_error:
        print(f"PRIVACY CHECK INCONCLUSIVE: {index_error}")
        return 2
    all_findings.extend(index_findings)
    history_findings, history_warnings, history_error = inspect_git_history(repo_root)
    if history_error:
        print(f"PRIVACY CHECK INCONCLUSIVE: {history_error}")
        return 2
    all_findings.extend(history_findings)
    for path in iter_text_files(repo_root):
        for finding in scan_file(path):
            all_findings.append(f"working:{path.relative_to(repo_root)}: {finding}")

    for warning in history_warnings:
        print(f"WARNING: {warning}")

    if all_findings:
        for finding in dict.fromkeys(all_findings):
            print(finding)
        return 1

    print("Privacy check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
