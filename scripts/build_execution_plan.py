#!/usr/bin/env python3
"""Export an Apple Shortcuts-friendly Reminders and Calendar plan."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from common import load_config, read_json, write_json
    from job_schema import is_verified_open_job
except ModuleNotFoundError:
    from scripts.common import load_config, read_json, write_json
    from scripts.job_schema import is_verified_open_job


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a daily execution plan for Apple Shortcuts.")
    parser.add_argument("--targets-config", default="config/targets.yaml")
    parser.add_argument("--execution-config", default="config/execution.yaml")
    parser.add_argument("--jobs", default="data/job_cache/decisioned_jobs.json")
    parser.add_argument("--question-bank", default="data/job_cache/question_bank.json")
    parser.add_argument("--check-in", default="", help="Optional Daily Check-in JSON")
    parser.add_argument("--output", default="data/job_cache/execution_plan.json")
    parser.add_argument("--date", default="")
    return parser.parse_args()


def workload_policy(check_in: dict[str, Any]) -> dict[str, Any]:
    rate = float(check_in.get("completion_rate", 1.0)) if check_in else 1.0
    low_days = int(check_in.get("consecutive_low_days", 0)) if check_in else 0
    if rate >= 0.8:
        factor = 1.0
    elif rate >= 0.5:
        factor = 0.85
    else:
        factor = 0.7
    if low_days >= 3:
        factor = min(factor, 0.7)
    return {
        "completion_rate": rate,
        "load_factor": factor,
        "weekly_replan_required": low_days >= 3,
        "rule": ">=80% maintain; 50-79% keep priorities; <50% reduce about 30%",
    }


def _task(
    title: str,
    list_name: str,
    due_date: str,
    tags: list[str],
    priority: str,
    notes: str = "",
    duration_minutes: int = 0,
) -> dict[str, Any]:
    return {
        "title": title,
        "list": list_name,
        "due_date": due_date,
        "tags": tags,
        "priority": priority,
        "notes": notes,
        "duration_minutes": duration_minutes,
    }


def _application_tasks(
    jobs: list[dict[str, Any]],
    reminders: dict[str, str],
    plan_date: str,
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for job in jobs:
        action = str(job.get("action") or "")
        if action not in {"MUST_APPLY", "READY"}:
            continue
        if not is_verified_open_job(job):
            continue
        due = str(job.get("deadline") or plan_date)
        if due < plan_date:
            due = plan_date
        company = str(job.get("company") or "Unknown company")
        title = str(job.get("title") or job.get("position") or "Role")
        tasks.append(
            _task(
                f"{action}: {company} — {title}",
                reminders.get("applications", "Applications"),
                due,
                ["Application", "Quick" if int(job.get("application_cost", 5)) <= 3 else "DeepWork"],
                "critical" if action == "MUST_APPLY" else "high",
                notes=(
                    f"URL: {job.get('official_url') or job.get('url') or ''}\n"
                    f"Opportunity: {job.get('opportunity_value', 0)}; "
                    f"release priority: {job.get('release_priority', 0)}"
                ),
                duration_minutes=max(15, int(job.get("application_cost", 5)) * 6),
            )
        )
    return tasks


def _weak_topic_task(bank: dict[str, Any], reminders: dict[str, str], plan_date: str) -> dict[str, Any] | None:
    weak = [item for item in bank.get("questions", []) if item.get("my_status") == "weak"]
    if not weak:
        return None
    weak.sort(key=lambda item: (-int(item.get("times_asked", 0)), str(item.get("last_seen", ""))))
    topics = weak[0].get("topics", ["interview gap"])
    topic = str(topics[0] if topics else "interview gap")
    return _task(
        f"Repair interview weak topic: {topic}",
        reminders.get("job_prep", "Job Prep"),
        plan_date,
        ["Interview", "CS", "DeepWork"],
        "high",
        notes=str(weak[0].get("sample_question") or ""),
        duration_minutes=30,
    )


def _calendar_blocks(
    tasks: list[dict[str, Any]],
    calendar_name: str,
    plan_date: str,
    day_start: str,
) -> list[dict[str, Any]]:
    cursor = datetime.fromisoformat(f"{plan_date}T{day_start}:00")
    blocks: list[dict[str, Any]] = []
    for task in tasks:
        duration = int(task.get("duration_minutes", 0))
        if duration <= 0:
            continue
        end = cursor + timedelta(minutes=duration)
        blocks.append(
            {
                "title": task["title"],
                "calendar": calendar_name,
                "start": cursor.isoformat(timespec="minutes"),
                "end": end.isoformat(timespec="minutes"),
                "notes": task.get("notes", ""),
            }
        )
        cursor = end + timedelta(minutes=10)
    return blocks


def build_execution_plan(
    targets: dict[str, Any],
    execution: dict[str, Any],
    jobs: list[dict[str, Any]],
    question_bank: dict[str, Any],
    check_in: dict[str, Any],
    *,
    plan_date: str,
) -> dict[str, Any]:
    apple = execution.get("apple", {})
    reminders = apple.get("reminders_lists", {})
    policy = workload_policy(check_in)
    phase = str(targets.get("operating_mode", {}).get("phase") or "PRE_APPLICATION_HOLD")
    tasks = _application_tasks(jobs, reminders, plan_date)

    if phase == "PRE_APPLICATION_HOLD":
        dissertation = execution.get("dissertation", {})
        tasks.insert(
            0,
            _task(
                str(dissertation.get("title") or "Dissertation deep work"),
                reminders.get("dissertation", "Dissertation"),
                plan_date,
                [str(tag) for tag in dissertation.get("tags", ["Dissertation", "DeepWork"])],
                "critical",
                duration_minutes=int(dissertation.get("duration_minutes", 240)),
            ),
        )
    else:
        modules = list(execution.get("study_modules", []))
        if policy["load_factor"] < 1:
            modules = [module for module in modules if module.get("priority") != "optional"]
        if policy["load_factor"] <= 0.7:
            modules = [module for module in modules if module.get("priority") in {"core"}]
        for module in modules:
            tasks.append(
                _task(
                    str(module.get("title") or "Job preparation"),
                    reminders.get("job_prep", "Job Prep"),
                    plan_date,
                    [str(tag) for tag in module.get("tags", [])],
                    str(module.get("priority") or "normal"),
                    duration_minutes=max(
                        10,
                        round(int(module.get("duration_minutes", 30)) * float(policy["load_factor"])),
                    ),
                )
            )
        weak_task = _weak_topic_task(question_bank, reminders, plan_date)
        if weak_task:
            tasks.append(weak_task)

    blocks = _calendar_blocks(
        tasks,
        str(apple.get("calendar_name") or "Study & Job"),
        plan_date,
        str(apple.get("day_start") or "09:00"),
    )
    return {
        "schema_version": 1,
        "date": plan_date,
        "phase": phase,
        "workload_policy": policy,
        "reminders": tasks,
        "calendar_blocks": blocks,
        "shortcut_contract": {
            "reminder_required_fields": ["title", "list", "due_date", "tags"],
            "calendar_required_fields": ["title", "calendar", "start", "end"],
        },
    }


def main() -> None:
    args = parse_args()
    targets = load_config(args.targets_config)
    execution_path = Path(args.execution_config)
    if not execution_path.exists() and execution_path.name == "execution.yaml":
        execution_path = execution_path.with_name("execution.example.yaml")
    execution = load_config(execution_path)
    jobs = read_json(args.jobs, [])
    bank = read_json(args.question_bank, {})
    check_in = read_json(args.check_in, {}) if args.check_in else {}
    plan_date = args.date or date.today().isoformat()
    plan = build_execution_plan(targets, execution, jobs, bank, check_in, plan_date=plan_date)
    write_json(Path(args.output), plan)
    print(
        f"Execution plan -> {args.output}; reminders={len(plan['reminders'])}; "
        f"calendar_blocks={len(plan['calendar_blocks'])}; phase={plan['phase']}"
    )


if __name__ == "__main__":
    main()
