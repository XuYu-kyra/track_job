#!/usr/bin/env python3
"""Install or remove a daily cron entry for the job pipeline."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

try:
    from common import load_config
except ModuleNotFoundError:
    from scripts.common import load_config


CRON_MARKER = "# find_job_daily_pipeline"
TZ_MARKER = "# find_job_daily_pipeline_tz"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install a cron schedule for the daily job pipeline.")
    parser.add_argument("--config", default="config/targets.yaml")
    parser.add_argument("--remove", action="store_true")
    return parser.parse_args()


def read_crontab() -> list[str]:
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines()]


def write_crontab(lines: list[str]) -> None:
    content = "\n".join(lines).rstrip() + "\n"
    subprocess.run(["crontab", "-"], input=content, text=True, check=True)


def build_cron_line(config_path: str) -> str:
    config = load_config(config_path)
    schedule = config.get("schedule", {})
    time_value = str(schedule.get("daily_run_time", "19:00"))
    hour, minute = time_value.split(":")
    repo_root = Path(__file__).resolve().parents[1]
    log_path = repo_root / "data/job_cache/scheduler.log"
    python_bin = subprocess.run(["which", "python3"], capture_output=True, text=True, check=True).stdout.strip()
    command = (
        f"{int(minute)} {int(hour)} * * * cd {repo_root} && "
        f"{python_bin} {repo_root / 'scripts/run_daily_pipeline.py'} >> {log_path} 2>&1 {CRON_MARKER}"
    )
    return command


def main() -> None:
    args = parse_args()
    lines = [line for line in read_crontab() if CRON_MARKER not in line and TZ_MARKER not in line]
    if args.remove:
        write_crontab(lines)
        print("Removed scheduled pipeline cron entry.")
        return

    timezone = str(load_config(args.config).get("schedule", {}).get("timezone", "UTC"))
    lines.append(f"CRON_TZ={timezone} {TZ_MARKER}")
    lines.append(build_cron_line(args.config))
    write_crontab(lines)
    print("Installed daily pipeline cron entry.")


if __name__ == "__main__":
    main()
