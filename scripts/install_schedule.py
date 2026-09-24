#!/usr/bin/env python3
"""Install cron or preview a native Windows Task Scheduler definition."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date
from html import escape
from pathlib import Path

try:
    from common import load_config
except ModuleNotFoundError:
    from scripts.common import load_config


CRON_MARKER = "# find_job_daily_pipeline"
TZ_MARKER = "# find_job_daily_pipeline_tz"
WINDOWS_TASK_NAME = "track_job_daily_pipeline"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage the deterministic daily pipeline schedule.")
    parser.add_argument("--config", default="config/targets.yaml")
    parser.add_argument("--remove", action="store_true")
    parser.add_argument("--platform", choices=("auto", "unix", "windows"), default="auto")
    parser.add_argument("--preview", action="store_true", help="Print/write definition without installing it")
    parser.add_argument("--install", action="store_true", help="Install/update the native Windows task")
    parser.add_argument("--run-now", action="store_true", help="Trigger the installed Windows task once")
    parser.add_argument("--python-executable", default="")
    parser.add_argument("--preview-output", default="data/job_cache/windows_task_preview.xml")
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


def build_windows_task_xml(config_path: str, python_executable: str) -> str:
    config = load_config(config_path)
    schedule = config.get("schedule", {})
    if str(schedule.get("timezone") or "") != "Asia/Shanghai":
        raise ValueError("Windows daily task requires schedule.timezone=Asia/Shanghai")
    time_value = str(schedule.get("daily_run_time", "19:00"))
    hour, minute = (int(part) for part in time_value.split(":"))
    repo_root = Path(__file__).resolve().parents[1]
    if os.name == "nt":
        windows_repo = str(repo_root)
    else:
        # Previewing from WSL is supported, but the task itself is native Windows.
        parts = repo_root.parts
        if len(parts) < 4 or parts[:3] != ("/", "mnt", "d"):
            raise ValueError("Cannot map repository path to the required D: Windows path")
        windows_repo = "D:\\" + "\\".join(parts[3:])
    if windows_repo.casefold() != r"d:\jobhunter\track_job":
        raise ValueError(f"Unexpected Windows repository path: {windows_repo}")
    executable = python_executable or str(schedule.get("windows_python_executable") or "")
    if not executable and os.name == "nt":
        executable = sys.executable
    if not re_full_windows_executable(executable):
        raise ValueError("A real absolute Windows python.exe path is required")
    wrapper = windows_repo + r"\scripts\run_windows_daily.py"
    boundary = f"{date.today().isoformat()}T{hour:02d}:{minute:02d}:00"
    user_element = ""
    if os.name == "nt":
        domain = str(os.getenv("USERDOMAIN") or "").strip()
        username = str(os.getenv("USERNAME") or "").strip()
        if username:
            user = f"{domain}\\{username}" if domain else username
            user_element = f"<UserId>{escape(user)}</UserId>"
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>track_job daily pipeline; timezone Asia/Shanghai; Feishu review-pool sync with per-job delivery gates</Description></RegistrationInfo>
  <Triggers><CalendarTrigger><StartBoundary>{boundary}</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger></Triggers>
  <Principals><Principal id="Author">{user_element}<LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><StartWhenAvailable>true</StartWhenAvailable><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT4H</ExecutionTimeLimit><Enabled>true</Enabled></Settings>
  <Actions Context="Author"><Exec><Command>{escape(executable)}</Command><Arguments>"{escape(wrapper)}" --apply-feishu --allow-degraded-review-sync</Arguments><WorkingDirectory>{escape(windows_repo)}</WorkingDirectory></Exec></Actions>
</Task>
'''


def re_full_windows_executable(value: str) -> bool:
    candidate = str(value or "").strip()
    return len(candidate) > 3 and candidate[1:3] == ":\\" and candidate.casefold().endswith("python.exe")


def main() -> None:
    args = parse_args()
    platform = args.platform
    if platform == "auto":
        platform = "windows" if os.name == "nt" else "unix"
    if platform == "windows":
        if args.remove:
            if os.name != "nt":
                raise RuntimeError("Windows task removal requires native Windows Python")
            subprocess.run(
                ["schtasks.exe", "/Delete", "/TN", WINDOWS_TASK_NAME, "/F"],
                check=True,
            )
            print(f"Removed Windows task {WINDOWS_TASK_NAME}.")
            return
        if args.install and args.preview:
            raise ValueError("choose either --install or --preview")
        if args.run_now and not args.install:
            raise ValueError("--run-now requires --install")
        xml = build_windows_task_xml(args.config, args.python_executable)
        output = Path(args.preview_output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(xml, encoding="utf-16")
        if not args.install:
            print(xml)
            print(f"Windows Task Scheduler preview written to {output}; task was NOT installed.")
            return
        if os.name != "nt":
            raise RuntimeError("Windows task installation requires native Windows Python")
        subprocess.run(
            [
                "schtasks.exe", "/Create", "/TN", WINDOWS_TASK_NAME,
                "/XML", str(output.resolve()), "/F",
            ],
            check=True,
        )
        print(f"Installed Windows task {WINDOWS_TASK_NAME} from {output.resolve()}.")
        if args.run_now:
            subprocess.run(
                ["schtasks.exe", "/Run", "/TN", WINDOWS_TASK_NAME], check=True
            )
            print(f"Triggered Windows task {WINDOWS_TASK_NAME}.")
        return
    lines = [line for line in read_crontab() if CRON_MARKER not in line and TZ_MARKER not in line]
    if args.remove:
        write_crontab(lines)
        print("Removed scheduled pipeline cron entry.")
        return

    timezone = str(load_config(args.config).get("schedule", {}).get("timezone", "UTC"))
    lines.append(f"CRON_TZ={timezone} {TZ_MARKER}")
    lines.append(build_cron_line(args.config))
    if args.preview:
        print("\n".join(lines))
        print("Cron preview only; schedule was NOT installed.")
    else:
        write_crontab(lines)
        print("Installed daily pipeline cron entry.")


if __name__ == "__main__":
    main()
