#!/usr/bin/env python3
"""Import an interview debrief into a deduplicated question bank."""

from __future__ import annotations

import argparse
import hashlib
from datetime import date
from pathlib import Path
from typing import Any

try:
    from common import normalize_token, read_json, write_json
except ModuleNotFoundError:
    from scripts.common import normalize_token, read_json, write_json


TOPIC_TERMS = {
    "algorithm": ["algorithm", "complexity", "leetcode", "数组", "链表", "算法", "复杂度"],
    "operating_system": ["process", "thread", "signal", "memory", "进程", "线程", "内存"],
    "network": ["tcp", "http", "network", "socket", "网络"],
    "database": ["sql", "database", "index", "transaction", "数据库", "索引", "事务"],
    "testing": ["test", "pytest", "mock", "quality", "测试"],
    "project": ["project", "architecture", "design", "项目", "架构", "设计"],
    "behavioral": ["conflict", "challenge", "leadership", "冲突", "挑战", "合作"],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update interview Question Bank from a debrief JSON file.")
    parser.add_argument("--input", required=True, help="Debrief JSON")
    parser.add_argument("--bank", default="data/job_cache/question_bank.json")
    parser.add_argument("--output", default="", help="Defaults to --bank")
    parser.add_argument("--as-of", default="")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def question_id(question: str) -> str:
    normalized = normalize_token(question)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def infer_topics(question: str) -> list[str]:
    normalized = normalize_token(question)
    topics = [
        topic
        for topic, terms in TOPIC_TERMS.items()
        if any(normalize_token(term) in normalized for term in terms)
    ]
    return topics or ["other"]


def status_from_result(result: str, previous: str = "unknown") -> str:
    token = normalize_token(result)
    if any(term in token for term in ("poor", "stuck", "incorrect", "weak", "不会", "卡住", "答错")):
        return "weak"
    if any(term in token for term in ("good", "strong", "correct", "clear", "答对", "清楚", "很好")):
        return "strong"
    if previous in {"weak", "strong"}:
        return previous
    return "learning" if token else "unknown"


def _question_items(debrief: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw in debrief.get("questions", []):
        if isinstance(raw, str):
            items.append({"question": raw, "result": "", "topic": ""})
        elif isinstance(raw, dict) and raw.get("question"):
            items.append(raw)
    return items


def update_bank(
    bank: dict[str, Any],
    debrief: dict[str, Any],
    *,
    as_of: str | None = None,
) -> dict[str, Any]:
    observed_on = as_of or str(debrief.get("interviewed_at") or date.today().isoformat())[:10]
    existing = {
        str(item.get("id")): dict(item)
        for item in bank.get("questions", [])
        if isinstance(item, dict) and item.get("id")
    }
    company = str(debrief.get("company") or "")
    role_family = str(debrief.get("role_family") or "")
    interview_round = str(debrief.get("round") or "")

    for item in _question_items(debrief):
        question = str(item["question"]).strip()
        identifier = question_id(question)
        record = existing.get(
            identifier,
            {
                "id": identifier,
                "normalized_question": normalize_token(question),
                "sample_question": question,
                "topics": [],
                "role_families": [],
                "companies": [],
                "rounds": [],
                "times_asked": 0,
                "first_seen": observed_on,
                "last_seen": observed_on,
                "my_status": "unknown",
                "result_history": [],
            },
        )
        explicit_topic = str(item.get("topic") or "").strip()
        topics = [explicit_topic] if explicit_topic else infer_topics(question)
        record["topics"] = list(dict.fromkeys([*record.get("topics", []), *topics]))
        if role_family:
            record["role_families"] = list(dict.fromkeys([*record.get("role_families", []), role_family]))
        if company:
            record["companies"] = list(dict.fromkeys([*record.get("companies", []), company]))
        if interview_round:
            record["rounds"] = list(dict.fromkeys([*record.get("rounds", []), interview_round]))
        result = str(item.get("result") or "")
        record["times_asked"] = int(record.get("times_asked", 0)) + 1
        record["last_seen"] = observed_on
        record["my_status"] = status_from_result(result, str(record.get("my_status") or "unknown"))
        record.setdefault("result_history", []).append(
            {
                "date": observed_on,
                "company": company,
                "round": interview_round,
                "result": result,
            }
        )
        existing[identifier] = record

    questions = list(existing.values())
    questions.sort(
        key=lambda item: (int(item.get("times_asked", 0)), str(item.get("last_seen", ""))),
        reverse=True,
    )
    status_counts = {
        status: sum(1 for item in questions if item.get("my_status") == status)
        for status in ("unknown", "learning", "weak", "strong")
    }
    return {
        "schema_version": 1,
        "updated_at": observed_on,
        "questions": questions,
        "summary": {"question_count": len(questions), "status_counts": status_counts},
    }


def main() -> None:
    args = parse_args()
    debrief = read_json(args.input, {})
    if not isinstance(debrief, dict):
        raise ValueError("Debrief input must be a JSON object")
    bank = read_json(args.bank, {})
    updated = update_bank(bank, debrief, as_of=args.as_of or None)
    output = args.output or args.bank
    if args.dry_run:
        print(
            f"Dry run: would write {updated['summary']['question_count']} questions to {output}; "
            f"status={updated['summary']['status_counts']}"
        )
        return
    write_json(Path(output), updated)
    print(
        f"Question Bank updated: questions={updated['summary']['question_count']}, "
        f"status={updated['summary']['status_counts']} -> {output}"
    )


if __name__ == "__main__":
    main()
