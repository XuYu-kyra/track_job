#!/usr/bin/env python3
"""Check the repository for likely privacy leaks while allowing safe placeholders."""

from __future__ import annotations

import re
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
    "__pycache__",
    "cv/generated",
}

ALLOWED_EMAILS = {
    "you@example.com",
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
FEISHU_RE = re.compile(
    r'(?P<key>app_id|app_secret|app_token|table_id|view_id):\s*"(?P<value>[^"\n]+)"'
)


def is_text_path(path: Path) -> bool:
    if path.name in {".gitignore", "Makefile"}:
        return True
    return path.suffix in TEXT_EXTENSIONS


def should_skip(path: Path) -> bool:
    as_posix = path.as_posix()
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


def scan_file(path: Path) -> list[str]:
    findings: list[str] = []
    text = path.read_text(encoding="utf-8")

    for match in EMAIL_RE.finditer(text):
        value = match.group(0)
        if value not in ALLOWED_EMAILS:
            findings.append(f"unexpected email: {value}")

    for match in LINKEDIN_RE.finditer(text):
        value = match.group(0)
        if value not in ALLOWED_LINKEDIN_URLS:
            findings.append(f"unexpected LinkedIn URL: {value}")

    for match in PHONE_RE.finditer(text):
        value = match.group(0)
        if value != "+44 0000 000000":
            findings.append(f"unexpected phone number: {value}")

    if path.suffix in {".yaml", ".yml"} and "feishu" in path.name:
        for match in FEISHU_RE.finditer(text):
            value = match.group("value").strip()
            if value and value not in ALLOWED_PORTAL_TOKENS:
                findings.append(f"unexpected {match.group('key')} value: {value}")

    return findings


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    all_findings: list[tuple[Path, str]] = []
    for path in iter_text_files(repo_root):
        for finding in scan_file(path):
            all_findings.append((path.relative_to(repo_root), finding))

    if all_findings:
        for rel_path, finding in all_findings:
            print(f"{rel_path}: {finding}")
        return 1

    print("Privacy check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
