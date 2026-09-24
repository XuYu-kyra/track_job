#!/usr/bin/env python3
"""Native Windows scheduled daily closure with fresh MANUAL_AGENT discovery."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from common import load_config
    from import_gpt_discovery import (
        batch_freshness,
        search_execution_summary,
        validate_gpt_payload,
    )
    from public_web_query_plan import build_public_web_query_plan, validate_query_receipts
    from source_adapters import load_source_registry
    from known_jobs import load_known_jobs
except ModuleNotFoundError:
    from scripts.common import load_config
    from scripts.import_gpt_discovery import (
        batch_freshness,
        search_execution_summary,
        validate_gpt_payload,
    )
    from scripts.public_web_query_plan import build_public_web_query_plan, validate_query_receipts
    from scripts.source_adapters import load_source_registry
    from scripts.known_jobs import load_known_jobs


SHANGHAI = ZoneInfo("Asia/Shanghai")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the native Windows daily closure.")
    parser.add_argument("--config", default="config/targets.yaml")
    parser.add_argument("--acceptance", action="store_true")
    parser.add_argument("--acceptance-run-id", default="")
    parser.add_argument(
        "--acceptance-output-root",
        default="",
        help="Keep the MANUAL_AGENT batch, plan, logs, and pipeline outputs isolated",
    )
    parser.add_argument("--lane", action="append", default=[])
    parser.add_argument("--query-id", action="append", default=[])
    parser.add_argument(
        "--urgent-company",
        action="append",
        default=[],
        help="Explicitly refresh a known company outside the normal P1 calendar",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--apply-feishu",
        action="store_true",
        help="Enable the explicit real Feishu sync after a non-isolated daily run",
    )
    parser.add_argument(
        "--allow-degraded-review-sync",
        action="store_true",
        help=(
            "Permit degraded discovery coverage for review-pool sync; "
            "READY/MUST_APPLY attachments remain individually gated"
        ),
    )
    return parser.parse_args()


def _resolve(repo_root: Path, value: str) -> Path:
    candidate = Path(value)
    return candidate if candidate.is_absolute() else repo_root / candidate


def utf8_subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def production_state_manifest(repo_root: Path) -> dict[str, str]:
    """Hash every formal cache/document artifact without reading its contents into logs."""

    roots = {
        "data/job_cache": repo_root / "data" / "job_cache",
        "cv/generated": repo_root / "cv" / "generated",
    }
    manifest: dict[str, str] = {}
    for label, root in roots.items():
        if not root.exists():
            continue
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            # The scheduler opens and locks this transient coordination file
            # before taking the acceptance snapshot. It is not production
            # state, and Windows may deny a second read while we hold the lock.
            if label == "data/job_cache" and path.name == "windows_daily.lock":
                continue
            relative = path.relative_to(root).as_posix()
            digest = hashlib.sha256()
            try:
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            except PermissionError as exc:
                raise PermissionError(
                    f"cannot read formal production file '{path}'. "
                    "Close any editor/PDF viewer or fix its Windows ACL, then rerun."
                ) from exc
            manifest[f"{label}/{relative}"] = digest.hexdigest()
    return manifest


def compare_production_manifests(before: dict[str, str], after: dict[str, str]) -> dict:
    paths = sorted(set(before) | set(after))
    changed = [path for path in paths if before.get(path) != after.get(path)]
    return {
        "unchanged": not changed,
        "before_file_count": len(before),
        "after_file_count": len(after),
        "changed_paths": changed,
    }


def find_codex_executable() -> Path:
    configured = str(os.getenv("CODEX_EXECUTABLE") or "").strip()
    if configured:
        path = Path(configured)
        if not path.is_file():
            raise RuntimeError("CODEX_EXECUTABLE does not point to an existing file")
        return path
    discovered = shutil.which("codex.exe") or shutil.which("codex")
    if discovered:
        return Path(discovered)
    profile = Path(str(os.getenv("USERPROFILE") or ""))
    matches = sorted(
        profile.glob(
            ".cursor/extensions/openai.chatgpt-*/bin/windows-x86_64/codex.exe"
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if matches:
        return matches[0]
    raise RuntimeError(
        "Codex CLI was not found; install/authenticate it or set CODEX_EXECUTABLE"
    )


def build_discovery_prompt(
    instructions: str,
    *,
    now: datetime,
    max_candidates: int,
    query_plan: dict | None = None,
) -> str:
    search_date = now.date().isoformat()
    expires_at = (now.date() + timedelta(days=1)).isoformat()
    plan_json = json.dumps(query_plan or {}, ensure_ascii=False, indent=2)
    return f"""{instructions}

Scheduled-run constraints override any example placeholders above:
- Today in Asia/Shanghai is {search_date}; current time is {now.isoformat()}.
- Actually execute the supplied bounded public-web query plan now. Do not reuse an old batch.
- Use schema_version=3 and copy the supplied plan_id into batch.plan_id and every receipt.
- Execute every supplied query exactly once and preserve query_id, lane, and exact query text.
- Return at most {max_candidates} strong jobs.
- For each known-company query, follow the bounded state machine START ->
  COMPANY_HOME_FOUND -> CAREER_ENTRY_FOUND -> CAMPUS_CAMPAIGN_FOUND ->
  ATS_LIST_FOUND -> ROLE_FOUND/NO_RESULT_CONFIRMED/BLOCKED/NEEDS_REVIEW.
  Continue from a homepage to careers/campus/graduate/校招, and from a campaign
  or ATS list to role-level 2027/Shenzhen URLs. Stop after a verified role URL.
- For known-company queries, never exceed 3 public searches, 8 page opens, 3
  hops, or 300 seconds. Every receipt must include terminal_state, page_types,
  search_calls, open_calls, hop_count, run_seconds, and transition_log. A
  budget exhaustion is BLOCKED, not SUCCESS or EMPTY_VALID.
- A zero-result first search is not enough to declare a known company empty.
  Within the same bounded budget, retry with at least two materially different
  variants when the first search returns no URLs: (1) the configured English
  and Chinese company aliases plus a broad role/cohort/location expression,
  and (2) an official-career/ATS or campus/graduate expression when an
  official domain or entry point is known. Keep each variant's URLs and
  rejection evidence in the same receipt. Use EMPTY_VALID only after all
  permitted variants return zero raw URLs; if any variant returns URLs, use
  SUCCESS (even when every returned result is later filtered).
- Each receipt must include provider, started_at, completed_at, status, result_count,
  result URLs, error_category, retry_count, and notes.
- Keep each receipt's notes concise and at most 500 characters; preserve detailed
  facts in source URLs and candidate evidence instead.
- Receipt result_count and source_urls describe the raw public search results inspected,
  not only candidates that survived filtering. Record up to 30 distinct result URLs.
- Candidate discovery_query is a provenance key: copy the exact original query
  string from batch.queries, even when a fallback variant found the URL. Never
  replace it with the fallback text; record fallback details in receipt notes,
  transition_log, and source evidence. Keep discovery_query_id equal to the
  supplied query_id.
- For an `urgent_known_company_refresh` query with `recall_control_urls`, this
  is an explicit positive-control refresh: inspect each supplied control URL
  or its host within the same budget, even if an unrelated role was already
  found. Preserve whether the exact URL was returned, opened, retained,
  filtered, or unavailable in the receipt notes and transition log.
- Use EMPTY_VALID only when the provider returned zero result URLs. If results were
  returned but all were rejected, use SUCCESS, keep candidates empty for that query,
  and summarize rejection reasons (closed, wrong cohort/location/role, duplicate,
  or insufficient evidence) in notes.
- batch.search_date must be {search_date}; expires_at must be {expires_at}; max_age_days must be 1.
- batch.source_urls must exactly equal the unique union of searches[].source_urls.
- Every candidate source_url must be in that union and source_evidence must contain concise factual excerpts observed at that URL.
- It is valid to return candidates=[] only after at least one query was executed; receipt status still follows raw-result semantics above.
- Do not emit progress/status placeholder JSON. Work silently until every supplied query has a real terminal receipt, then return the one final schema-valid object.
- Do not use shell, apply_patch, or any file-writing tool for the result. The scheduler captures your final response and a trusted receiver writes it later.
- Return exactly one JSON object matching the supplied JSON Schema. No Markdown or surrounding prose.

Supplied dated query plan (trusted input; do not invent or omit queries):
{plan_json}
"""


def load_discovered_company_candidates(
    repo_root: Path,
    *,
    acceptance_root: Path | None = None,
) -> dict:
    """Load the latest dynamic-company store without crossing isolation boundaries.

    An acceptance run may seed its first day from the formal store, but once the
    isolated pipeline has produced its own store, every later plan must consume
    that isolated copy. Otherwise a multi-day acceptance run silently forgets
    companies promoted during earlier isolated days.
    """

    formal_path = repo_root / "data" / "job_cache" / "discovered_company_candidates.json"
    candidates = (
        [acceptance_root / "discovered_company_candidates.json", formal_path]
        if acceptance_root is not None
        else [formal_path]
    )
    for path in candidates:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def attach_recall_control_urls(plan: dict, repo_root: Path) -> dict:
    """Attach independent control URLs to explicit urgent-refresh queries.

    The URLs are evidence for a bounded recall refresh, not additional search
    queries and not a source allowlist. Ordinary daily plans remain unchanged.
    """

    urgent = [
        item for item in plan.get("queries", [])
        if isinstance(item, dict) and item.get("lane") == "urgent_known_company_refresh"
    ]
    if not urgent:
        return plan
    controls = load_known_jobs(repo_root / "data" / "discovery" / "known_jobs.jsonl")
    for query in urgent:
        names = {
            str(company).strip().casefold()
            for company in query.get("companies") or []
            if str(company).strip()
        }
        urls: list[str] = []
        for control in controls:
            control_names = {
                str(control.get("company") or "").strip().casefold(),
                *{
                    str(alias).strip().casefold()
                    for alias in control.get("company_aliases") or []
                    if str(alias).strip()
                },
            }
            if names.intersection(control_names):
                for key in ("url", "canonical_url"):
                    value = str(control.get(key) or "").strip()
                    if value and value not in urls:
                        urls.append(value)
        if urls:
            query["recall_control_urls"] = urls
    return plan
    return {}


def build_codex_command(
    codex: Path,
    repo_root: Path,
    schema_path: Path,
    raw_output: Path,
    *,
    model: str = "gpt-5.6-luna",
    reasoning_effort: str = "low",
) -> list[str]:
    # `--search`, sandboxing and approval policy are Codex top-level options.
    # Keep them before the `exec` subcommand for Windows CLI compatibility.
    allowed_efforts = {"low", "medium", "high", "xhigh", "max", "ultra"}
    if reasoning_effort not in allowed_efforts:
        raise ValueError(f"unsupported MANUAL_AGENT reasoning effort: {reasoning_effort}")
    return [
        str(codex),
        "--model",
        str(model),
        "--config",
        f'model_reasoning_effort="{reasoning_effort}"',
        "--search",
        "--sandbox",
        "read-only",
        "--ask-for-approval",
        "never",
        "exec",
        "--ephemeral",
        "--color",
        "never",
        "--cd",
        str(repo_root),
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(raw_output),
        "-",
    ]


def discovery_run_metrics(path: Path, started: float) -> dict[str, int | str]:
    """Summarize accepted receipts without treating candidates as search calls."""

    payload: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except (OSError, UnicodeError, json.JSONDecodeError):
            payload = {}
    receipts = payload.get("batch", {}).get("searches", []) if payload else []
    search_calls = len([item for item in receipts if isinstance(item, dict)])
    open_calls = sum(
        int(item.get("open_calls") or len(item.get("source_urls") or []))
        for item in receipts
        if isinstance(item, dict)
    )
    terminal_states = sorted(
        {
            str(item.get("terminal_state") or "NEEDS_REVIEW")
            for item in receipts
            if isinstance(item, dict)
        }
    )
    return {
        "search_calls": search_calls,
        "open_calls": open_calls,
        "run_seconds": max(0, int(time.monotonic() - started)),
        "terminal_states": ",".join(terminal_states),
    }


def reusable_daily_batch(
    path: Path,
    *,
    now: datetime,
    query_plan: dict | None = None,
    retry_failed: bool = False,
) -> tuple[bool, str]:
    """Accept only an already-validated, fresh batch from this calendar day."""
    if not path.is_file():
        return False, "batch file is missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return False, f"batch cannot be read: {exc}"
    errors = validate_gpt_payload(payload)
    if query_plan:
        errors.extend(validate_query_receipts(query_plan, payload, require_complete=True))
    if errors:
        return False, "; ".join(errors)
    today = now.date().isoformat()
    if str(payload["batch"].get("search_date") or "") != today:
        return False, "batch was not searched today"
    fresh, reason = batch_freshness(payload, as_of=today)
    if not fresh:
        return False, reason
    search_status, counts = search_execution_summary(payload)
    if search_status == "FAILED":
        return False, "batch has no completed public-web search"
    if retry_failed and counts["failed"]:
        return False, f"batch has {counts['failed']} failed searches to retry"
    return (
        True,
        f"{payload['batch']['batch_id']} "
        f"({counts['attempted']} searches, {len(payload['candidates'])} candidates)",
    )


def selected_query_plan(
    plan: dict,
    *,
    lanes: list[str] | None = None,
    query_ids: list[str] | None = None,
    existing_payload: dict | None = None,
    resume: bool = False,
) -> dict:
    """Return a stable subset for lane/query filtering and receipt recovery."""

    lane_filter = set(lanes or [])
    id_filter = set(query_ids or [])
    completed: set[str] = set()
    if resume and existing_payload:
        completed = {
            str(item.get("query_id") or "")
            for item in existing_payload.get("batch", {}).get("searches", [])
            if isinstance(item, dict)
            and str(item.get("status") or "").upper() in {"SUCCESS", "EMPTY_VALID"}
        }
    selected = [
        dict(item)
        for item in plan.get("queries", [])
        if isinstance(item, dict)
        and (not lane_filter or str(item.get("lane") or "") in lane_filter)
        and (not id_filter or str(item.get("query_id") or "") in id_filter)
        and (not resume or str(item.get("query_id") or "") not in completed)
    ]
    subset = dict(plan)
    subset["queries"] = selected
    subset["selected_query_count"] = len(selected)
    return subset


def shard_query_plan(plan: dict, max_queries_per_shard: int) -> list[dict]:
    """Split a selected plan without changing query order or plan identity."""

    if max_queries_per_shard < 1:
        raise ValueError("max_queries_per_shard must be at least 1")
    queries = [dict(item) for item in plan.get("queries", []) if isinstance(item, dict)]
    prompt_keys = (
        "schema_version",
        "architecture",
        "registry_is_allowlist",
        "unregistered_companies_allowed",
        "plan_id",
        "search_date",
        "timezone",
        "generated_at",
        "required_lanes",
        "optional_lanes",
    )
    prompt_metadata = {key: plan[key] for key in prompt_keys if key in plan}
    shards: list[dict] = []
    for start in range(0, len(queries), max_queries_per_shard):
        selected = queries[start : start + max_queries_per_shard]
        shards.append(
            {**prompt_metadata, "queries": selected, "selected_query_count": len(selected)}
        )
    return shards


def build_daily_pipeline_command(
    repo_root: Path,
    config_path: Path,
    *,
    acceptance_root: Path | None = None,
    manual_agent_input: Path | None = None,
    public_web_query_plan: Path | None = None,
    apply_feishu: bool = False,
    allow_degraded_review_sync: bool = False,
) -> list[str]:
    command = [
        sys.executable,
        str(repo_root / "scripts" / "run_daily_pipeline.py"),
        "--config",
        str(config_path),
        "--mode",
        "daily",
        "--skip-repo-sync",
    ]
    if acceptance_root is not None:
        command.extend(["--acceptance", "--acceptance-output-root", str(acceptance_root)])
    if manual_agent_input is not None:
        command.extend(["--manual-agent-input", str(manual_agent_input)])
    if public_web_query_plan is not None:
        command.extend(["--public-web-query-plan", str(public_web_query_plan)])
    if apply_feishu:
        command.append("--apply-feishu")
    if allow_degraded_review_sync:
        command.append("--allow-degraded-review-sync")
    return command


def run_manual_agent_discovery(
    repo_root: Path,
    config: dict,
    log,
    *,
    now: datetime,
    acceptance_root: Path | None = None,
    lanes: list[str] | None = None,
    query_ids: list[str] | None = None,
    urgent_companies: list[str] | None = None,
    resume: bool = False,
) -> Path:
    gpt = config.get("gpt_discovery", {})
    runner = gpt.get("runner", {})
    if not (bool(gpt.get("enabled", False)) and bool(runner.get("enabled", False))):
        raise RuntimeError("scheduled MANUAL_AGENT runner is disabled")
    prompt_path = _resolve(repo_root, str(runner.get("prompt") or ""))
    schema_path = _resolve(repo_root, str(runner.get("output_schema") or ""))
    destination = (
        acceptance_root / "manual_agent_batch.json"
        if acceptance_root is not None
        else _resolve(repo_root, str(gpt.get("input") or ""))
    )
    plan_path = (
        acceptance_root / "public_web_query_plan.json"
        if acceptance_root is not None
        else repo_root / "data" / "job_cache" / "public_web_query_plan.json"
    )
    plan = build_public_web_query_plan(
        config,
        load_config(repo_root / "config" / "taxonomies.yaml"),
        load_source_registry(repo_root / "config" / "company_registry.yaml"),
        run_date=now.date(),
        discovered_companies=load_discovered_company_candidates(
            repo_root,
            acceptance_root=acceptance_root,
        ),
        urgent_companies=urgent_companies,
    )
    plan = attach_recall_control_urls(plan, repo_root)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    reusable, detail = reusable_daily_batch(
        destination,
        now=now,
        query_plan=plan,
        retry_failed=resume,
    )
    if reusable:
        log.write(f"MANUAL_AGENT: reusing today's accepted batch {detail}\n")
        log.flush()
        return destination
    existing_payload: dict | None = None
    if destination.is_file():
        try:
            candidate = json.loads(destination.read_text(encoding="utf-8"))
            if (
                isinstance(candidate, dict)
                and not validate_gpt_payload(candidate)
                and not validate_query_receipts(plan, candidate)
            ):
                existing_payload = candidate
        except (OSError, UnicodeError, json.JSONDecodeError):
            existing_payload = None
    prompt_plan = selected_query_plan(
        plan,
        lanes=lanes,
        query_ids=query_ids,
        existing_payload=existing_payload,
        resume=resume,
    )
    if not prompt_plan["queries"]:
        if destination.is_file():
            log.write("MANUAL_AGENT: no failed/unexecuted selected queries remain\n")
            log.flush()
            return destination
        raise RuntimeError("query filters selected no current-plan queries")
    codex = find_codex_executable()
    max_candidates = int(runner.get("max_candidates", 128))
    max_queries_per_shard = int(runner.get("max_queries_per_shard", 10))
    prompt_shards = shard_query_plan(prompt_plan, max_queries_per_shard)
    if max_candidates < 0:
        raise ValueError("max_candidates cannot be negative")
    existing_candidate_count = len((existing_payload or {}).get("candidates", []))
    remaining_candidates = max(0, max_candidates - existing_candidate_count)
    candidates_per_shard, candidate_remainder = divmod(
        remaining_candidates, len(prompt_shards)
    )
    timeout = int(runner.get("shard_timeout_seconds", runner.get("timeout_seconds", 900)))
    model = str(runner.get("model") or "gpt-5.6-luna")
    reasoning_effort = str(runner.get("reasoning_effort") or "low")
    log.write(f"MODEL_USED={model}\n")
    log.write(f"REASONING_EFFORT={reasoning_effort}\n")
    log.flush()
    has_current_receipts = existing_payload is not None
    for shard_index, shard_plan in enumerate(prompt_shards, start=1):
        shard_candidate_limit = candidates_per_shard + (
            1 if shard_index <= candidate_remainder else 0
        )
        prompt = build_discovery_prompt(
            prompt_path.read_text(encoding="utf-8"),
            now=now,
            max_candidates=shard_candidate_limit,
            query_plan=shard_plan,
        )
        with tempfile.TemporaryDirectory(prefix="track-job-manual-agent-") as directory:
            raw_output = Path(directory) / "batch.json"
            command = build_codex_command(
                codex,
                repo_root,
                schema_path,
                raw_output,
                model=model,
                reasoning_effort=reasoning_effort,
            )
            log.write(
                "MANUAL_AGENT: starting shard "
                f"{shard_index}/{len(prompt_shards)} "
                f"({len(shard_plan['queries'])} queries)\n"
            )
            log.flush()
            shard_succeeded = False
            last_failure = ""
            for attempt in range(2):
                if raw_output.exists():
                    raw_output.unlink()
                try:
                    result = subprocess.run(
                        command,
                        cwd=repo_root,
                        input=prompt,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=timeout,
                        env=utf8_subprocess_environment(),
                    )
                except subprocess.TimeoutExpired as exc:
                    last_failure = f"Codex timeout: {exc}"
                    result = None
                if result is None or result.returncode != 0 or not raw_output.is_file():
                    last_failure = last_failure or (
                        f"Codex exit={getattr(result, 'returncode', 'missing output')}"
                    )
                else:
                    receive_command = [
                        sys.executable,
                        str(repo_root / "scripts" / "receive_manual_agent_batch.py"),
                        "--input",
                        str(raw_output),
                        "--output",
                        str(destination),
                        "--as-of",
                        now.date().isoformat(),
                        "--query-plan",
                        str(plan_path),
                    ]
                    if has_current_receipts:
                        receive_command.append("--merge")
                    receive = subprocess.run(
                        receive_command,
                        cwd=repo_root,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        env=utf8_subprocess_environment(),
                    )
                    if receive.returncode == 0:
                        shard_succeeded = True
                        break
                    last_failure = f"receiver exit={receive.returncode}"
                if attempt == 0:
                    log.write(
                        f"MANUAL_AGENT: retrying shard {shard_index}/{len(prompt_shards)} "
                        "once with the same minimal shard context\n"
                    )
                    log.flush()
            if not shard_succeeded:
                raise RuntimeError(
                    "MANUAL_AGENT discovery failed after one retry in shard "
                    f"{shard_index}/{len(prompt_shards)} ({last_failure})"
                )
            log.write(
                f"MANUAL_AGENT: shard {shard_index}/{len(prompt_shards)} promoted atomically\n"
            )
            log.flush()
            has_current_receipts = True
    complete, detail = reusable_daily_batch(destination, now=now, query_plan=plan)
    if not complete:
        raise RuntimeError(f"MANUAL_AGENT shard aggregate is incomplete: {detail}")
    log.write(f"MANUAL_AGENT: fresh aggregate complete {detail}\n")
    log.flush()
    return destination


def main() -> int:
    args = parse_args()
    if os.name != "nt":
        raise RuntimeError("run_windows_daily.py must run under native Windows Python")
    import msvcrt

    repo_root = Path(__file__).resolve().parents[1]
    config_path = _resolve(repo_root, args.config)
    config = load_config(config_path)
    acceptance_text = str(args.acceptance_output_root or "").strip()
    if args.acceptance and not acceptance_text:
        raise ValueError("--acceptance requires --acceptance-output-root")
    if args.acceptance and args.apply_feishu:
        raise ValueError("--acceptance cannot be combined with --apply-feishu")
    if args.allow_degraded_review_sync and not args.apply_feishu:
        raise ValueError("--allow-degraded-review-sync requires --apply-feishu")
    acceptance_root = (
        Path(acceptance_text).expanduser().resolve()
        if acceptance_text
        else None
    )
    if acceptance_root is not None and not Path(acceptance_text).expanduser().is_absolute():
        raise ValueError("--acceptance-output-root must be an absolute path")
    formal_cache_root = (repo_root / "data" / "job_cache").resolve()
    if acceptance_root is not None and (
        acceptance_root == formal_cache_root
        or formal_cache_root in acceptance_root.parents
    ):
        raise ValueError(
            "--acceptance-output-root must be outside the formal job cache"
        )
    generated_root = (repo_root / "cv" / "generated").resolve()
    if acceptance_root is not None and (
        acceptance_root == generated_root or generated_root in acceptance_root.parents
    ):
        raise ValueError("acceptance output must be outside cv/generated")
    cache_dir = acceptance_root or (repo_root / "data" / "job_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    lock_path = cache_dir / "windows_daily.lock"
    lock_file = lock_path.open("a+b")
    try:
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print("Another daily pipeline instance is active; exiting without overlap.")
        return 0
    now = datetime.now(SHANGHAI)
    timestamp = now.strftime("%Y%m%d-%H%M%S")
    log_path = cache_dir / f"scheduler-{timestamp}.log"
    return_code = 1
    production_before: dict[str, str] | None = None
    production_comparison: dict | None = None
    if acceptance_root is not None:
        try:
            production_before = production_state_manifest(repo_root)
        except (OSError, RuntimeError) as exc:
            lock_file.close()
            print(f"Production state snapshot failed: {exc}")
            return 2
        (acceptance_root / "production_state_before.json").write_text(
            json.dumps(production_before, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    try:
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"Daily run started: {now.isoformat()}\n")
            discovery_started = time.monotonic()
            discovery_path = (
                acceptance_root / "manual_agent_batch.json"
                if acceptance_root is not None
                else _resolve(repo_root, str(config.get("gpt_discovery", {}).get("input") or ""))
            )
            try:
                manual_agent_input = run_manual_agent_discovery(
                    repo_root,
                    config,
                    log,
                    now=now,
                    acceptance_root=acceptance_root,
                    lanes=args.lane,
                    query_ids=args.query_id,
                    urgent_companies=args.urgent_company,
                    resume=args.resume,
                )
            except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
                log.write(f"DAILY FAILURE before pipeline: {exc}\n")
                return_code = 2
            else:
                discovery_path = manual_agent_input
            metrics = discovery_run_metrics(discovery_path, discovery_started)
            runner_config = config.get("gpt_discovery", {}).get("runner", {})
            log.write(f"MODEL_USED={runner_config.get('model') or 'gpt-5.6-luna'}\n")
            log.write(f"REASONING_EFFORT={runner_config.get('reasoning_effort') or 'low'}\n")
            log.write(f"SEARCH_CALLS={metrics['search_calls']}\n")
            log.write(f"OPEN_CALLS={metrics['open_calls']}\n")
            log.write(f"RUN_SECONDS={metrics['run_seconds']}\n")
            log.write(f"ADAPTIVE_TERMINAL_STATES={metrics['terminal_states']}\n")
            log.flush()
            if return_code == 2:
                pass
            else:
                command = build_daily_pipeline_command(
                    repo_root,
                    config_path,
                    acceptance_root=acceptance_root,
                    manual_agent_input=(manual_agent_input if acceptance_root else None),
                    public_web_query_plan=(
                        cache_dir / "public_web_query_plan.json"
                    ),
                    apply_feishu=args.apply_feishu,
                    allow_degraded_review_sync=args.allow_degraded_review_sync,
                )
                result = subprocess.run(
                    command,
                    cwd=repo_root,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=utf8_subprocess_environment(),
                )
                return_code = result.returncode
                log.write(f"Daily pipeline exit: {return_code}\n")
    finally:
        if acceptance_root is not None and production_before is not None:
            production_after = production_state_manifest(repo_root)
            production_comparison = compare_production_manifests(
                production_before, production_after
            )
            (acceptance_root / "production_state_after.json").write_text(
                json.dumps(production_after, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (acceptance_root / "production_state_comparison.json").write_text(
                json.dumps(production_comparison, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if not production_comparison["unchanged"]:
                return_code = 3
        lock_file.close()
    print(f"Daily scheduler log: {log_path}")
    if production_comparison is not None:
        print(
            "Production state unchanged: "
            f"{str(production_comparison['unchanged']).upper()}"
        )
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
