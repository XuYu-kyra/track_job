#!/usr/bin/env python3
"""Import manually collected job links into the unified pipeline format."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from common import normalize_whitespace
    from job_schema import extract_stable_job_id
    from source_health import record_source_health
except ModuleNotFoundError:
    from scripts.common import normalize_whitespace
    from scripts.job_schema import extract_stable_job_id
    from scripts.source_health import record_source_health


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import manually collected job URLs.")
    parser.add_argument("--input", default="data/job_cache/manual_job_inputs.txt")
    parser.add_argument("--output", default="data/job_cache/manual_import_jobs.json")
    parser.add_argument("--normalized-output", default="")
    return parser.parse_args()


def infer_source(url: str) -> str:
    lowered = url.lower()
    if "linkedin.com" in lowered:
        return "linkedin"
    if "indeed." in lowered:
        return "indeed"
    if "glassdoor." in lowered:
        return "glassdoor"
    if "maimai.cn" in lowered:
        return "maimai"
    if "kanzhun.com" in lowered:
        return "kanzhun"
    if "nowcoder.com" in lowered:
        return "nowcoder"
    if "ncss.cn" in lowered:
        return "ncss"
    if "iguopin.com" in lowered:
        return "guopin"
    if "zhipin.com" in lowered or "boss直聘" in lowered:
        return "boss"
    if "weixin.qq.com" in lowered or "mp.weixin.qq.com" in lowered:
        return "wechat"
    if any(marker in lowered for marker in ("career.", "career/", "jobs.", "/jobs/")):
        return "public_web"
    return "manual"


def infer_job_id(url: str) -> str:
    return extract_stable_job_id(url, infer_source(url))


def clean_url(url: str) -> str:
    parsed = urlparse(url.strip())
    query = parse_qs(parsed.query)

    keep_keys = []
    if "indeed." in parsed.netloc:
        keep_keys = ["jk"]
    elif "linkedin.com" in parsed.netloc:
        keep_keys = []
    elif "glassdoor." in parsed.netloc:
        keep_keys = ["jl"]

    kept_pairs: list[str] = []
    for key in keep_keys:
        for value in query.get(key, []):
            kept_pairs.append(f"{key}={value}")

    clean_query = "&".join(kept_pairs)
    normalized = parsed._replace(query=clean_query, fragment="")
    cleaned = normalized.geturl()
    return cleaned.rstrip("?")


def parse_manual_line(line: str) -> dict | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None

    parts = [part.strip() for part in stripped.split("|")]
    parts += [""] * (10 - len(parts))
    (
        url,
        company,
        position,
        location,
        description,
        deadline,
        source_override,
        dream_role,
        notes,
        application_profile,
    ) = parts[:10]
    if not url.startswith("http"):
        return None
    url = clean_url(url)

    return {
        "company": normalize_whitespace(company),
        "position": normalize_whitespace(position),
        "url": url,
        "location": normalize_whitespace(location),
        "source": normalize_whitespace(source_override) or infer_source(url),
        "description": normalize_whitespace(description),
        "deadline": normalize_whitespace(deadline),
        "dream_role": dream_role.casefold() in {"1", "true", "yes", "y", "是"},
        "notes": normalize_whitespace(notes),
        "easy_apply": False,
        "manual_ingest": True,
        "observation_origin": "MANUAL_IMPORT",
        "search_keyword": "manual_import",
        "job_id": infer_job_id(url),
        "application_profile": application_profile.strip().upper(),
    }


def normalize_manual_lines(input_path: str) -> list[str]:
    path = Path(input_path)
    if not path.exists():
        return []

    normalized_lines: list[str] = []
    seen_urls: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parsed = parse_manual_line(raw_line)
        if not parsed:
            continue
        url = parsed["url"]
        if url in seen_urls:
            continue
        seen_urls.add(url)
        normalized_lines.append(
            " | ".join(
                [
                    url,
                    parsed.get("company", ""),
                    parsed.get("position", ""),
                    parsed.get("location", ""),
                    parsed.get("description", ""),
                    parsed.get("deadline", ""),
                    parsed.get("source", ""),
                    "true" if parsed.get("dream_role") else "false",
                    parsed.get("notes", ""),
                    parsed.get("application_profile", ""),
                ]
            ).rstrip()
        )
    return normalized_lines


def import_manual_jobs(input_path: str) -> list[dict]:
    path = Path(input_path)
    if not path.exists():
        return []

    jobs: list[dict] = []
    seen_urls: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        parsed = parse_manual_line(raw_line)
        if parsed:
            if parsed["url"] in seen_urls:
                continue
            seen_urls.add(parsed["url"])
            jobs.append(parsed)
    return jobs


def main() -> None:
    args = parse_args()
    jobs = import_manual_jobs(args.input)
    if args.normalized_output:
        normalized_lines = normalize_manual_lines(args.input)
        normalized_path = Path(args.normalized_output)
        normalized_path.parent.mkdir(parents=True, exist_ok=True)
        normalized_path.write_text("\n".join(normalized_lines) + ("\n" if normalized_lines else ""), encoding="utf-8")
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(jobs, indent=2, ensure_ascii=False), encoding="utf-8")
    record_source_health(
        "manual_inbox",
        "SUCCESS" if jobs else "MANUAL_BY_DESIGN",
        count=len(jobs),
        attempted=len(jobs),
        succeeded=len(jobs),
        details="BOSS/WeChat/other manual intake; no closed-platform crawling",
    )
    print(f"Wrote {len(jobs)} manual jobs to {output_path}")


if __name__ == "__main__":
    main()
