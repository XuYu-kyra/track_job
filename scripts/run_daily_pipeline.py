#!/usr/bin/env python3
"""Run the daily multi-source jobs-to-Feishu pipeline with logging and alerts."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

try:
    from common import load_config
except ModuleNotFoundError:
    from scripts.common import load_config

try:
    from send_alert import send_pipeline_alert
except ModuleNotFoundError:
    from scripts.send_alert import send_pipeline_alert


def configure_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("daily_pipeline")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


def run_command(command: list[str], logger: logging.Logger) -> None:
    logger.info("Running command: %s", " ".join(command))
    result = subprocess.run(command, capture_output=True, text=True)
    if result.stdout.strip():
        logger.info("stdout:\n%s", result.stdout.strip())
    if result.stderr.strip():
        logger.warning("stderr:\n%s", result.stderr.strip())
    if result.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {result.returncode}: {' '.join(command)}")


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    targets = load_config(repo_root / "config/targets.yaml")
    log_path = repo_root / targets.get("output", {}).get("cache_dir", "data/job_cache") / "scheduler.log"
    logger = configure_logging(log_path)

    max_jobs = str(targets.get("schedule", {}).get("max_jobs_per_run", 10))
    min_score = str(targets.get("job_search", {}).get("shortlist_min_score", 70))
    sources = set(targets.get("job_search", {}).get("sources", []))
    if not sources:
        source_value = str(targets.get("job_search", {}).get("source", "linkedin")).strip()
        if source_value:
            sources.add(source_value)

    commands: list[list[str]] = []
    if "linkedin" in sources or not sources:
        commands.append([sys.executable, "scripts/fetch_linkedin.py"])
    if "indeed" in sources:
        commands.append([sys.executable, "scripts/fetch_indeed.py"])
    commands.extend(
        [
            [
                sys.executable,
                "scripts/import_manual_jobs.py",
                "--normalized-output",
                "data/job_cache/manual_job_inputs.normalized.txt",
            ],
            [sys.executable, "scripts/merge_job_sources.py"],
            [sys.executable, "scripts/score_jobs.py", "--top-k", max_jobs],
            [sys.executable, "scripts/generate_resume.py", "--min-score", min_score],
            [sys.executable, "scripts/update_feishu.py"],
        ]
    )

    try:
        logger.info("Starting daily pipeline run.")
        for command in commands:
            run_command(command, logger)
        logger.info("Pipeline completed successfully.")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Pipeline failed: %s", exc)
        try:
            send_pipeline_alert(
                str(repo_root / "config/feishu.yaml"),
                "Daily pipeline failed",
                str(exc),
                str(log_path),
            )
            logger.info("Sent failure alert to Feishu.")
        except Exception as alert_exc:  # noqa: BLE001
            logger.exception("Failed to send pipeline alert: %s", alert_exc)
        raise


if __name__ == "__main__":
    main()
