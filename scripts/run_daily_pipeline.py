#!/usr/bin/env python3
"""Run the canonical daily or initial-backfill job pipeline."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from common import load_config, read_json
    from query_matrix import deterministic_daily_batch_index
    from source_adapters import load_source_registry
    from source_health import automatic_discovery_succeeded, reset_source_health
except ModuleNotFoundError:
    from scripts.common import load_config, read_json
    from scripts.query_matrix import deterministic_daily_batch_index
    from scripts.source_adapters import load_source_registry
    from scripts.source_health import automatic_discovery_succeeded, reset_source_health

try:
    from send_alert import send_pipeline_alert
except ModuleNotFoundError:
    from scripts.send_alert import send_pipeline_alert


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the jobs-to-Feishu operating system.")
    parser.add_argument("--config", default="config/targets.yaml")
    parser.add_argument("--feishu-config", default="config/feishu.yaml")
    parser.add_argument("--execution-config", default="config/execution.yaml")
    parser.add_argument("--source-registry", default="")
    parser.add_argument("--taxonomy-config", default="config/taxonomies.yaml")
    parser.add_argument("--mode", choices=("daily", "backfill"), default="daily")
    parser.add_argument("--batch-index", type=int, default=-1)
    parser.add_argument("--skip-fetch", action="store_true", help="Use existing source cache files")
    parser.add_argument("--skip-repo-sync", action="store_true", help="Use existing candidate repo cache")
    parser.add_argument("--skip-documents", action="store_true", help="Do not generate application drafts")
    parser.add_argument("--apply-feishu", action="store_true", help="Explicitly permit Feishu writes")
    parser.add_argument(
        "--allow-degraded-review-sync",
        action="store_true",
        help=(
            "Allow degraded discovery coverage for review-pool Feishu sync; "
            "READY/MUST_APPLY attachments remain individually gated"
        ),
    )
    parser.add_argument(
        "--acceptance",
        action="store_true",
        help="Enable fail-closed isolated acceptance mode",
    )
    parser.add_argument("--acceptance-run-id", default="")
    parser.add_argument(
        "--semantic-review-dry-run",
        action="store_true",
        help="Select semantic-review jobs without calling the configured provider",
    )
    parser.add_argument(
        "--semantic-review-job-key",
        action="append",
        default=[],
        help="Explicitly select one WATCH/HOLD canonical key for semantic review",
    )
    parser.add_argument("--as-of", default="", help="ISO date override for deterministic runs")
    parser.add_argument(
        "--acceptance-output-root",
        default="",
        help=(
            "Write discovery/normalization/scoring/coverage artifacts under this "
            "absolute isolated directory; documents and Feishu remain disabled"
        ),
    )
    parser.add_argument(
        "--manual-agent-input",
        default="",
        help="Optional isolated MANUAL_AGENT batch used with --acceptance-output-root",
    )
    parser.add_argument(
        "--public-web-query-plan",
        default="",
        help="Reuse the exact query plan executed by the MANUAL_AGENT runner",
    )
    return parser.parse_args()


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


def utf8_subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def run_command(command: list[str], logger: logging.Logger, repo_root: Path) -> None:
    logger.info("Running command: %s", " ".join(command))
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=repo_root,
        env=utf8_subprocess_environment(),
    )
    if result.stdout.strip():
        logger.info("stdout:\n%s", result.stdout.strip())
    if result.stderr.strip():
        logger.warning("stderr:\n%s", result.stderr.strip())
    if result.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {result.returncode}: {' '.join(command)}")


def build_commands(
    args: argparse.Namespace,
    targets: dict,
    python: str,
    source_registry: dict | None = None,
) -> list[list[str]]:
    acceptance_root = Path(str(getattr(args, "acceptance_output_root", "") or ""))
    isolated = bool(str(getattr(args, "acceptance_output_root", "") or "").strip())

    def cache_file(name: str) -> str:
        return str(acceptance_root / name) if isolated else f"data/job_cache/{name}"

    if isolated:
        isolated_discovered = acceptance_root / "discovered_company_candidates.json"
        discovered_company_seed = str(
            isolated_discovered
            if isolated_discovered.is_file()
            else acceptance_root / "discovered_company_baseline.json"
        )
    else:
        discovered_company_seed = cache_file("discovered_company_candidates.json")

    max_jobs = str(targets.get("schedule", {}).get("max_jobs_per_run", 30))
    min_score = str(targets.get("job_search", {}).get("shortlist_min_score", 70))
    official_safety_cap = str(
        targets.get("job_search", {}).get("official_safety_cap", 500)
    )
    sources = [str(item) for item in targets.get("job_search", {}).get("sources", [])]
    if not sources:
        sources.append(str(targets.get("job_search", {}).get("source", "linkedin")))
    registry_specs = (source_registry or {}).get("sources", {})
    priority_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    sources = sorted(
        dict.fromkeys(sources),
        key=lambda name: (
            priority_rank.get(
                str(registry_specs.get(name, {}).get("discovery_priority") or "LOW").upper(),
                9,
            ),
            int(registry_specs.get(name, {}).get("tier", 9)),
            sources.index(name),
        ),
    )

    commands: list[list[str]] = []
    batch_index = (
        int(args.batch_index)
        if int(getattr(args, "batch_index", -1)) >= 0
        else deterministic_daily_batch_index(args.as_of or None)
    )
    if not args.skip_repo_sync and not isolated:
        repo_command = [python, "scripts/sync_candidate_repos.py", "--config", args.config]
        if args.skip_fetch:
            repo_command.append("--offline")
        commands.append(repo_command)
    commands.append([
        python,
        "scripts/build_candidate_inventory.py",
        "--targets-config",
        args.config,
        "--output",
        cache_file("candidate_inventory.json"),
    ])
    evidence_command = [
        python,
        "scripts/build_candidate_evidence.py",
        "--input",
        "cv/materials/evidence.yaml",
    ]
    configured_evidence_output = (
        cache_file("semantic_candidate_evidence.json")
        if isolated
        else str(targets.get("semantic_review", {}).get("candidate_evidence_input") or "").strip()
    )
    if configured_evidence_output:
        evidence_command.extend(["--output", configured_evidence_output])
    commands.append(evidence_command)
    source_inputs: list[str] = []
    if not args.skip_fetch:
        commands.append(
            [
                python,
                "scripts/audit_company_sources.py",
                "--source-registry",
                args.source_registry,
                "--json-output",
                cache_file("company_source_audit.json"),
                "--markdown-output",
                cache_file("company_source_audit.md"),
            ]
        )
        for source in sources:
            spec = registry_specs.get(source, {})
            if spec and not bool(spec.get("enabled", False)):
                continue
            if spec and spec.get("implementation_status") not in {
                "automated",
                "automated_if_registered",
            }:
                continue
            if source == "official":
                commands.append(
                    [
                        python,
                        "scripts/fetch_official.py",
                        "--config",
                        args.config,
                        "--taxonomy-config",
                        args.taxonomy_config,
                        "--source-registry",
                        args.source_registry,
                        "--mode",
                        args.mode,
                        "--max-links-per-company",
                        official_safety_cap,
                        "--output",
                        cache_file(
                            "official_jobs.json"
                            if args.mode == "daily"
                            else "official_backfill_jobs.json"
                        ),
                        "--continuation-state",
                        cache_file("official_continuations.json"),
                    ]
                )
            elif source in {"linkedin", "indeed"}:
                commands.append(
                    [
                        python,
                        f"scripts/fetch_{source}.py",
                        "--config",
                        args.config,
                        "--taxonomy-config",
                        args.taxonomy_config,
                        "--mode",
                        args.mode,
                        "--batch-index",
                        str(batch_index),
                        "--output",
                        cache_file(
                            f"{source}_jobs.json"
                            if args.mode == "daily"
                            else f"{source}_backfill_jobs.json"
                        ),
                    ]
                )

    gpt_config = targets.get("gpt_discovery", {})
    gpt_mode = str(gpt_config.get("mode") or "MANUAL_AGENT").upper()
    if (
        bool(gpt_config.get("enabled", True))
        and gpt_mode == "MANUAL_AGENT"
        and "gpt_web" in sources
    ):
        supplied_query_plan = str(
            getattr(args, "public_web_query_plan", "") or ""
        ).strip()
        query_plan_path = supplied_query_plan or cache_file("public_web_query_plan.json")
        if not supplied_query_plan:
            query_plan_command = [
                python,
                "scripts/build_public_web_query_plan.py",
                "--config",
                args.config,
                "--taxonomy-config",
                args.taxonomy_config,
                "--source-registry",
                args.source_registry,
                "--discovered-companies",
                discovered_company_seed,
                "--output",
                query_plan_path,
            ]
            if args.as_of:
                query_plan_command.extend(["--date", args.as_of])
            commands.append(query_plan_command)
        commands.append(
            ([
                python,
                "scripts/import_gpt_discovery.py",
                "--input",
                str(
                    getattr(args, "manual_agent_input", "")
                    or gpt_config.get("input")
                    or "data/discovery/gpt_manual/candidates.json"
                ),
                "--output",
                cache_file("gpt_manual_jobs.json"),
                "--query-plan",
                query_plan_path,
            ] + (["--as-of", args.as_of] if args.as_of else []))
        )

    suffix = "jobs.json" if args.mode == "daily" else "backfill_jobs.json"
    for source in ("official", "linkedin", "indeed"):
        spec = registry_specs.get(source, {})
        supported = not spec or (
            bool(spec.get("enabled", False))
            and spec.get("implementation_status") in {"automated", "automated_if_registered"}
        )
        if source in sources and supported:
            source_inputs.append(cache_file(f"{source}_{suffix}"))
    if not isolated:
        commands.append(
            [
                python,
                "scripts/import_manual_jobs.py",
                "--normalized-output",
                "data/job_cache/manual_job_inputs.normalized.txt",
            ]
        )
        source_inputs.append("data/job_cache/manual_import_jobs.json")
    if (
        bool(gpt_config.get("enabled", True))
        and gpt_mode == "MANUAL_AGENT"
        and "gpt_web" in sources
    ):
        source_inputs.append(
            cache_file("gpt_manual_jobs.json")
        )
    # Feishu application-inbox evidence is a user-confirmed manual source.
    # Missing files are treated as an empty source by merge_job_sources, so a
    # bot outage never blocks ordinary discovery.
    source_inputs.append(cache_file("feishu_application_jobs.json"))
    merge_output = (
        cache_file("jobs.json")
        if args.mode == "daily"
        else cache_file("backfill_jobs.json")
    )
    history_output = (
        cache_file("job_history.json")
        if args.mode == "daily"
        else cache_file("backfill_job_history.json")
    )
    decision_output = (
        cache_file("decisioned_jobs.json")
        if args.mode == "daily"
        else cache_file("backfill_decisioned_jobs.json")
    )
    queue_output = (
        cache_file("scored_jobs.json")
        if args.mode == "daily"
        else cache_file("backfill_scored_jobs.json")
    )
    human_state_output = cache_file("feishu_human_state.json")
    # A readback refresh is production-only.  Acceptance runs must stay
    # isolated and never contact Feishu; an existing local snapshot remains
    # usable for offline scoring/dry-run inspection.
    if not isolated and bool(getattr(args, "apply_feishu", False)):
        commands.append([
            python,
            "scripts/feishu_human_state.py",
            "--config",
            args.feishu_config,
            "--output",
            human_state_output,
        ])
    merge_command = [
        python,
        "scripts/merge_job_sources.py",
        "--targets-config",
        args.config,
        "--taxonomy-config",
        args.taxonomy_config,
        "--source-registry",
        args.source_registry,
        "--output",
        merge_output,
        "--history",
        history_output,
        "--inputs",
        *source_inputs,
    ]
    if not isolated:
        merge_command.extend(["--human-state", human_state_output])
    score_command = [
        python,
        "scripts/score_jobs.py",
        "--config",
        args.config,
        "--taxonomy-config",
        args.taxonomy_config,
        "--source-registry",
        args.source_registry,
        "--inventory",
        cache_file("candidate_inventory.json"),
        "--input",
        merge_output,
        "--output",
        queue_output,
        "--all-output",
        decision_output,
        "--top-k",
        max_jobs,
        "--mode",
        args.mode,
    ]
    if args.as_of:
        merge_command.extend(["--as-of", args.as_of])
        score_command.extend(["--as-of", args.as_of])
    commands.extend([merge_command, score_command])
    discovered_companies_command = [
        python,
        "scripts/discovered_companies.py",
        "--jobs",
        decision_output,
        "--source-registry",
        args.source_registry,
        "--targets-config",
        args.config,
        "--output",
        cache_file("discovered_company_candidates.json"),
    ]
    if isolated:
        discovered_companies_command.extend(
            ["--existing", discovered_company_seed]
        )
    if args.as_of:
        discovered_companies_command.extend(["--as-of", args.as_of])
    coverage_command = [
        python,
        "scripts/build_discovery_coverage.py",
        "--source-registry",
        args.source_registry,
        "--jobs",
        decision_output,
        "--scored-jobs",
        queue_output,
        "--health",
        cache_file("source_health.json"),
        "--source-audit",
        cache_file("company_source_audit.json"),
        "--query-plan",
        str(getattr(args, "public_web_query_plan", "") or cache_file("public_web_query_plan.json")),
        "--discovered-companies",
        cache_file("discovered_company_candidates.json"),
        "--history",
        cache_file("discovery_coverage_history.json"),
        "--json-output",
        cache_file("discovery_coverage_report.json"),
        "--markdown-output",
        cache_file("discovery_coverage_report.md"),
    ]
    if args.as_of:
        coverage_command.extend(["--as-of", args.as_of])
    semantic_config = targets.get("semantic_review", {})
    if bool(semantic_config.get("enabled", False)) and not isolated:
        semantic_command = [
            python,
            "scripts/semantic_review.py",
            "--config",
            args.config,
            "--jobs",
            decision_output,
        ]
        candidate_evidence = str(semantic_config.get("candidate_evidence_input") or "").strip()
        if candidate_evidence:
            semantic_command.extend(["--candidate-evidence", candidate_evidence])
        for job_key in getattr(args, "semantic_review_job_key", []) or []:
            semantic_command.extend(["--select-job-key", str(job_key)])
        if bool(getattr(args, "semantic_review_dry_run", False)):
            semantic_command.append("--dry-run")
        commands.append(semantic_command)
    commands.extend([discovered_companies_command, coverage_command])
    if isolated:
        pack_command = [
            python,
            "scripts/build_daily_application_pack.py",
            "--targets-config",
            args.config,
            "--source-registry",
            args.source_registry,
            "--jobs",
            merge_output,
            "--decisioned",
            decision_output,
            "--health",
            cache_file("source_health.json"),
            "--manifest",
            cache_file("generated_manifest.json"),
            "--output",
            cache_file("daily_application_pack.md"),
        ]
        if args.as_of:
            pack_command.extend(["--as-of", args.as_of])
        commands.append(pack_command)
        return commands
    if args.mode == "backfill":
        return commands
    if not args.skip_documents:
        commands.append(
            [
                python,
                "scripts/generate_resume.py",
                "--min-score",
                min_score,
                "--compile-pdf",
            ]
        )
    pack_command = [
        python,
        "scripts/build_daily_application_pack.py",
        "--targets-config",
        args.config,
        "--source-registry",
        args.source_registry,
    ]
    if args.as_of:
        pack_command.extend(["--as-of", args.as_of])
    commands.append(pack_command)
    commands.append([python, "scripts/build_analytics.py"])
    execution_command = [
        python,
        "scripts/build_execution_plan.py",
        "--targets-config",
        args.config,
        "--execution-config",
        args.execution_config,
    ]
    if args.as_of:
        execution_command.extend(["--date", args.as_of])
    commands.append(execution_command)
    feishu_command = [python, "scripts/update_feishu.py", "--config", args.feishu_config]
    feishu_command.extend(
        ["--coverage-report", "data/job_cache/discovery_coverage_report.json"]
    )
    if bool(getattr(args, "allow_degraded_review_sync", False)):
        feishu_command.append("--allow-degraded-review-sync")
    feishu_command.append("--apply" if args.apply_feishu else "--dry-run")
    commands.append(feishu_command)
    return commands


def main() -> None:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    config_path = repo_root / args.config
    if not config_path.exists():
        raise FileNotFoundError(
            f"Missing {args.config}. Copy config/targets.example.yaml to config/targets.yaml first."
        )
    targets = load_config(config_path)
    acceptance_root_text = str(args.acceptance_output_root or "").strip()
    acceptance = bool(args.acceptance or acceptance_root_text)
    if acceptance and args.apply_feishu:
        raise ValueError("--acceptance cannot be combined with --apply-feishu")
    if args.allow_degraded_review_sync and not args.apply_feishu:
        raise ValueError("--allow-degraded-review-sync requires --apply-feishu")
    if acceptance and not acceptance_root_text:
        raise ValueError("--acceptance requires --acceptance-output-root")
    if args.mode == "backfill" and args.apply_feishu:
        raise ValueError("Backfill mode never writes to Feishu; review the local queue first.")
    if not args.source_registry:
        args.source_registry = str(
            targets.get("job_search", {}).get("source_registry", "config/company_registry.yaml")
        )
    source_registry = load_source_registry(repo_root / args.source_registry)
    if acceptance_root_text:
        raw_acceptance_root = Path(acceptance_root_text).expanduser()
        if not raw_acceptance_root.is_absolute():
            raise ValueError("--acceptance-output-root must be an absolute path")
        acceptance_root = raw_acceptance_root.resolve()
        formal_cache_root = (
            repo_root
            / targets.get("output", {}).get("cache_dir", "data/job_cache")
        ).resolve()
        if acceptance_root == formal_cache_root or formal_cache_root in acceptance_root.parents:
            raise ValueError(
                "--acceptance-output-root must be outside the formal job cache"
            )
        generated_root = (repo_root / "cv" / "generated").resolve()
        if acceptance_root == generated_root or generated_root in acceptance_root.parents:
            raise ValueError("acceptance output must be outside cv/generated")
        acceptance_root.mkdir(parents=True, exist_ok=True)
        formal_discovered = formal_cache_root / "discovered_company_candidates.json"
        isolated_baseline = acceptance_root / "discovered_company_baseline.json"
        if formal_discovered.is_file():
            shutil.copy2(formal_discovered, isolated_baseline)
        else:
            isolated_baseline.write_text("{}\n", encoding="utf-8")
        os.environ["TRACK_JOB_HEALTH_PATH"] = str(acceptance_root / "source_health.json")
        args.skip_repo_sync = True
        args.skip_documents = True
        args.apply_feishu = False
        log_path = acceptance_root / "acceptance.log"
        health_path = acceptance_root / "source_health.json"
    else:
        log_path = repo_root / targets.get("output", {}).get("cache_dir", "data/job_cache") / "scheduler.log"
        health_path = repo_root / targets.get("output", {}).get("cache_dir", "data/job_cache") / "source_health.json"
    logger = configure_logging(log_path)
    reset_source_health(
        health_path,
        timezone=str(targets.get("schedule", {}).get("timezone") or "Asia/Shanghai"),
    )
    commands = build_commands(args, targets, sys.executable, source_registry)

    try:
        logger.info(
            "Starting %s pipeline (feishu_mode=%s).",
            args.mode,
            "APPLY" if args.apply_feishu else "DRY_RUN",
        )
        for command in commands:
            run_command(command, logger, repo_root)
        health = read_json(health_path, {})
        if args.skip_fetch or automatic_discovery_succeeded(health):
            logger.info("Pipeline completed successfully.")
        else:
            logger.warning("Pipeline completed, but automatic discovery was DEGRADED_NO_AUTOMATIC_DISCOVERY.")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Pipeline failed: %s", exc)
        if args.apply_feishu:
            try:
                send_pipeline_alert(
                    str(repo_root / args.feishu_config),
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
