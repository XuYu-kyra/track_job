#!/usr/bin/env python3
"""Shared helpers for the job pipeline."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def load_config(path: str | Path) -> dict[str, Any]:
    """Load a small YAML subset used by this project.

    PyYAML is not available in the current environment, so this parser handles
    the project's simple config and materials files:
    - mappings
    - nested mappings via indentation
    - lists
    - quoted and plain scalars
    """

    text = Path(path).read_text(encoding="utf-8")
    lines = _prepare_lines(text)
    if not lines:
        return {}
    value, next_index = _parse_block(lines, 0, lines[0][0])
    if next_index != len(lines):
        raise ValueError(f"Unexpected trailing YAML content in {path}")
    if not isinstance(value, dict):
        raise ValueError(f"Expected top-level mapping in {path}")
    return value


def read_json(path: str | Path, default: Any) -> Any:
    file_path = Path(path)
    if not file_path.exists():
        return default
    return json.loads(file_path.read_text(encoding="utf-8"))


def write_json(path: str | Path, payload: Any) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def slugify(text: str) -> str:
    safe = [char.lower() if char.isalnum() else "-" for char in text]
    collapsed = "".join(safe)
    while "--" in collapsed:
        collapsed = collapsed.replace("--", "-")
    return collapsed.strip("-") or "job"


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_token(text: str) -> str:
    cleaned = normalize_whitespace(text).lower()
    cleaned = re.sub(r"[^a-z0-9]+", " ", cleaned)
    return normalize_whitespace(cleaned)


def canonical_job_key(company: str, position: str) -> str:
    return f"{normalize_token(company)}|{normalize_token(position)}"


def _prepare_lines(text: str) -> list[tuple[int, str]]:
    prepared: list[tuple[int, str]] = []
    for raw_line in text.splitlines():
        without_comment = _strip_comment(raw_line)
        if not without_comment.strip():
            continue
        indent = len(without_comment) - len(without_comment.lstrip(" "))
        prepared.append((indent, without_comment.strip()))
    return prepared


def _strip_comment(line: str) -> str:
    in_single = False
    in_double = False
    for index, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            return line[:index]
    return line


def _parse_block(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    if lines[index][1].startswith("- "):
        return _parse_list(lines, index, indent)
    return _parse_mapping(lines, index, indent)


def _parse_mapping(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[dict[str, Any], int]:
    mapping: dict[str, Any] = {}
    while index < len(lines):
        line_indent, content = lines[index]
        if line_indent < indent:
            break
        if line_indent > indent:
            raise ValueError(f"Unexpected indentation near: {content}")
        if content.startswith("- "):
            break

        key, _, value = content.partition(":")
        if not _:
            raise ValueError(f"Invalid mapping line: {content}")

        key = key.strip()
        value = value.strip()
        index += 1

        if value:
            mapping[key] = _parse_scalar(value)
            continue

        if index >= len(lines) or lines[index][0] <= line_indent:
            mapping[key] = {}
            continue

        nested, index = _parse_block(lines, index, lines[index][0])
        mapping[key] = nested

    return mapping, index


def _parse_list(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    while index < len(lines):
        line_indent, content = lines[index]
        if line_indent < indent:
            break
        if line_indent != indent or not content.startswith("- "):
            break

        item_content = content[2:].strip()
        index += 1

        if not item_content:
            if index >= len(lines) or lines[index][0] <= line_indent:
                items.append(None)
                continue
            nested, index = _parse_block(lines, index, lines[index][0])
            items.append(nested)
            continue

        if ":" in item_content:
            key, _, value = item_content.partition(":")
            item_map: dict[str, Any] = {key.strip(): _parse_scalar(value.strip()) if value.strip() else {}}
            while index < len(lines):
                next_indent, next_content = lines[index]
                if next_indent <= line_indent:
                    break
                if next_content.startswith("- ") and next_indent == indent:
                    break
                nested_map, index = _parse_mapping(lines, index, next_indent)
                item_map.update(nested_map)
            items.append(item_map)
            continue

        items.append(_parse_scalar(item_content))

    return items, index


def _parse_scalar(value: str) -> Any:
    if value == "[]":
        return []
    if value == "{}":
        return {}
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    if value.lower() in {"null", "none"}:
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    return value
