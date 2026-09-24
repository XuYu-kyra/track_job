#!/usr/bin/env python3
"""Validate and import Cursor/Codex MANUAL_AGENT discovery candidates."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from ats_detection import detect_ats_family
    from common import write_json
    from source_health import record_source_health
    from public_web_query_plan import lane_receipts, validate_query_receipts
except ModuleNotFoundError:
    from scripts.ats_detection import detect_ats_family
    from scripts.common import write_json
    from scripts.source_health import record_source_health
    from scripts.public_web_query_plan import lane_receipts, validate_query_receipts


CONTACT_FIELDS = {"email", "phone", "alternate_phone", "full_name", "contact_details"}
PROFILE_SELECTION_FIELDS = {"application_profile", "recommended_application_profile"}
SYSTEM_PROVENANCE_FIELDS = {
    "canonicality",
    "source_verified",
    "official_url",
    "source_authoritative",
    "canonical_authority",
    "verification_level",
    "verified_at",
    "official",
    "is_official",
}
SCHEMA_VERSION = 3
BATCH_FIELDS = {
    "batch_id",
    "plan_id",
    "generated_at",
    "search_date",
    "queries",
    "source_urls",
    "expires_at",
    "max_age_days",
    "searches",
}
SEARCH_FIELDS = {
    "plan_id",
    "query_id",
    "lane",
    "query",
    "provider",
    "started_at",
    "completed_at",
    "status",
    "source_urls",
    "result_count",
    "error_category",
    "retry_count",
    "notes",
}
ADAPTIVE_SEARCH_FIELDS = {
    "terminal_state",
    "page_types",
    "search_calls",
    "open_calls",
    "hop_count",
    "run_seconds",
    "transition_log",
}
SEARCH_FAILURE_STATUSES = frozenset(
    {"FAILED", "RATE_LIMITED", "CAPTCHA", "PARSE_ERROR", "TIMEOUT"}
)
SEARCH_STATUSES = frozenset({"SUCCESS", "EMPTY_VALID", *SEARCH_FAILURE_STATUSES})
REQUIRED_CANDIDATE_FIELDS = {
    "company", "title", "location", "source_url", "source_name", "source_type",
    "posted_at", "deadline", "status", "description", "source_evidence",
    "graduation_evidence", "role_evidence", "location_evidence", "discovery_query",
    "discovered_at", "evidence_confidence", "notes", "job_language",
    "recruiting_context", "canonical_url",
    "discovery_query_id",
}
ALLOWED_CANDIDATE_FIELDS = {
    "company",
    "title",
    "position",
    "location",
    "source_url",
    "canonical_url",
    "canonical_source",
    "source",
    "source_name",
    "source_type",
    "posted_at",
    "deadline",
    "status",
    "description",
    "graduation_evidence",
    "role_evidence",
    "location_evidence",
    "discovery_query",
    "discovery_query_id",
    "discovered_at",
    "evidence_confidence",
    "notes",
    "job_language",
    "recruiting_context",
    "discovered_by",
    "discovery_sources",
    "job_id",
    "source_evidence",
    *SYSTEM_PROVENANCE_FIELDS,
}
DECISION_CONTROL_FIELDS = {
    "score",
    "matching_score",
    "opportunity_value",
    "action",
    "action_tier",
    "release_priority",
    "application_profile",
    "recommended_application_profile",
    "apply_feishu",
    "write_feishu",
    "feishu_apply",
}
EVIDENCE_LIST_FIELDS = {
    "graduation_evidence",
    "role_evidence",
    "location_evidence",
    "discovered_by",
    "discovery_sources",
    "source_evidence",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import MANUAL_AGENT discovery JSON.")
    parser.add_argument("--input", default="data/discovery/gpt_manual/candidates.json")
    parser.add_argument("--output", default="data/job_cache/gpt_manual_jobs.json")
    parser.add_argument("--check", action="store_true", help="Validate without writing output")
    parser.add_argument("--as-of", default="", help="ISO date used for freshness validation")
    parser.add_argument(
        "--query-plan",
        default="data/job_cache/public_web_query_plan.json",
        help="Dated trusted query plan used to attribute per-lane receipts",
    )
    return parser.parse_args()


def is_real_source_url(value: Any) -> bool:
    candidate = str(value or "").strip()
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return False
    hostname = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or not hostname:
        return False
    if hostname in {"localhost", "example.com", "www.example.com"} or hostname.endswith(".invalid"):
        return False
    return not any(marker in candidate.casefold() for marker in ("<url>", "your-url", "placeholder"))


def validate_gpt_candidate(candidate: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in ("company",):
        if not str(candidate.get(field) or "").strip():
            errors.append(f"{field} is required")
    if not str(candidate.get("title") or candidate.get("position") or "").strip():
        errors.append("title is required")
    source_url = candidate.get("source_url")
    if not is_real_source_url(source_url):
        errors.append("source_url must be a real http(s) source URL")
    canonical_url = candidate.get("canonical_url") or candidate.get("official_url")
    if canonical_url not in (None, "") and not is_real_source_url(canonical_url):
        errors.append("canonical_url must be a real http(s) URL when provided")
    embedded_contacts = sorted(CONTACT_FIELDS.intersection(candidate))
    if embedded_contacts:
        errors.append(f"contact details are not allowed in discovery JSON: {', '.join(embedded_contacts)}")
    selected_profiles = sorted(
        field for field in PROFILE_SELECTION_FIELDS if candidate.get(field) not in (None, "")
    )
    if selected_profiles:
        errors.append(
            "GPT discovery cannot select an application profile: "
            + ", ".join(selected_profiles)
        )
    if candidate.get("source") not in (None, "", "gpt_web"):
        errors.append("GPT discovery candidate source must be gpt_web")
    forbidden = sorted(
        field
        for field in candidate
        if field in DECISION_CONTROL_FIELDS or field.casefold().startswith("feishu_")
    )
    if forbidden:
        errors.append(
            "decision/write control fields are not allowed in GPT discovery JSON: "
            + ", ".join(forbidden)
        )
    unknown = sorted(
        set(candidate) - ALLOWED_CANDIDATE_FIELDS - CONTACT_FIELDS - PROFILE_SELECTION_FIELDS
    )
    if unknown:
        errors.append("unexpected GPT discovery fields: " + ", ".join(unknown))
    for field in EVIDENCE_LIST_FIELDS:
        value = candidate.get(field)
        if value not in (None, "") and (
            not isinstance(value, list)
            or any(not isinstance(item, str) or not item.strip() for item in value)
        ):
            errors.append(f"{field} must be a list of non-empty strings")
    return errors


def validate_gpt_payload(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["GPT discovery input must be a schema envelope object"]
    errors: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if str(payload.get("mode") or "").upper() != "MANUAL_AGENT":
        errors.append("mode must be MANUAL_AGENT")
    if str(payload.get("source") or "") != "gpt_web":
        errors.append("source must be gpt_web")
    unknown = sorted(set(payload) - {"schema_version", "mode", "source", "batch", "candidates"})
    if unknown:
        errors.append("unexpected GPT discovery envelope fields: " + ", ".join(unknown))
    if not isinstance(payload.get("candidates"), list):
        errors.append("candidates must be a list")
    elif any(not isinstance(item, dict) for item in payload["candidates"]):
        errors.append("every candidate must be an object")
    batch = payload.get("batch")
    if not isinstance(batch, dict):
        errors.append("batch must be an object")
        return errors
    unknown_batch = sorted(set(batch) - BATCH_FIELDS)
    if unknown_batch:
        errors.append("unexpected batch fields: " + ", ".join(unknown_batch))
    missing_batch = sorted(BATCH_FIELDS - set(batch))
    if missing_batch:
        errors.append("missing batch fields: " + ", ".join(missing_batch))
    for field in ("batch_id", "plan_id", "generated_at", "search_date"):
        if not str(batch.get(field) or "").strip():
            errors.append(f"batch.{field} is required")
    for field in ("queries",):
        value = batch.get(field)
        if not isinstance(value, list) or not value or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            errors.append(f"batch.{field} must be a non-empty string list")
        elif len(value) > 128:
            errors.append(f"batch.{field} must contain at most 128 items")
        elif len(value) != len(set(value)):
            errors.append(f"batch.{field} must not contain duplicates")
    source_urls = batch.get("source_urls")
    if not isinstance(source_urls, list) or any(
        not isinstance(item, str) or not is_real_source_url(item)
        for item in (source_urls if isinstance(source_urls, list) else [])
    ):
        errors.append("batch.source_urls must be a list of real public http(s) URLs")
    elif len(source_urls) > 512:
        errors.append("batch.source_urls must contain at most 512 items")
    elif len(source_urls) != len(set(source_urls)):
        errors.append("batch.source_urls must not contain duplicates")
    if batch.get("expires_at") in (None, ""):
        errors.append("batch.expires_at is required")
    if batch.get("max_age_days") not in (None, "") and (
        isinstance(batch.get("max_age_days"), bool)
        or not isinstance(batch.get("max_age_days"), int)
        or batch["max_age_days"] < 1
    ):
        errors.append("batch.max_age_days must be a positive integer")
    searches = batch.get("searches")
    if not isinstance(searches, list) or not searches:
        errors.append("batch.searches must contain at least one executed search receipt")
    elif len(searches) > 128:
        errors.append("batch.searches must contain at most 128 receipts")
    else:
        configured_queries = set(batch.get("queries") or [])
        receipt_queries: list[str] = []
        receipt_query_ids: list[str] = []
        for index, search in enumerate(searches):
            label = f"batch.searches[{index}]"
            if not isinstance(search, dict):
                errors.append(f"{label} must be an object")
                continue
            unknown_search = sorted(set(search) - SEARCH_FIELDS - ADAPTIVE_SEARCH_FIELDS)
            if unknown_search:
                errors.append(f"{label} has unexpected fields: {', '.join(unknown_search)}")
            missing_search = sorted(SEARCH_FIELDS - set(search))
            if missing_search:
                errors.append(f"{label} is missing fields: {', '.join(missing_search)}")
            query = str(search.get("query") or "").strip()
            if not query:
                errors.append(f"{label}.query is required")
            elif query not in configured_queries:
                errors.append(f"{label}.query must appear in batch.queries")
            receipt_queries.append(query)
            for field in ("plan_id", "query_id", "lane", "provider"):
                if not str(search.get(field) or "").strip():
                    errors.append(f"{label}.{field} is required")
            receipt_query_ids.append(str(search.get("query_id") or ""))
            try:
                started = _parse_datetime(search.get("started_at"), f"{label}.started_at")
                completed = _parse_datetime(search.get("completed_at"), f"{label}.completed_at")
                if completed < started:
                    errors.append(f"{label}.completed_at must not precede started_at")
            except ValueError as exc:
                errors.append(str(exc))
            status = str(search.get("status") or "").upper()
            if status not in SEARCH_STATUSES:
                errors.append(
                    f"{label}.status must be one of {', '.join(sorted(SEARCH_STATUSES))}"
                )
            search_urls = search.get("source_urls")
            if not isinstance(search_urls, list) or any(
                not isinstance(item, str) or not is_real_source_url(item)
                for item in (search_urls if isinstance(search_urls, list) else [])
            ):
                errors.append(f"{label}.source_urls must be a list of real public URLs")
            elif len(search_urls) > 30:
                errors.append(f"{label}.source_urls must contain at most 30 items")
            elif len(search_urls) != len(set(search_urls)):
                errors.append(f"{label}.source_urls must not contain duplicates")
            notes = search.get("notes")
            if not isinstance(notes, str) or len(notes) > 500:
                errors.append(f"{label}.notes must be a string of at most 500 characters")
            terminal_state = search.get("terminal_state")
            if terminal_state not in (None, "", "ROLE_FOUND", "NO_RESULT_CONFIRMED", "BLOCKED", "NEEDS_REVIEW"):
                errors.append(f"{label}.terminal_state is invalid")
            for field in ("search_calls", "open_calls", "hop_count", "run_seconds"):
                value = search.get(field)
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                ):
                    errors.append(f"{label}.{field} must be a non-negative integer")
            if search.get("search_calls") is not None and int(search["search_calls"]) > 3:
                errors.append(f"{label}.search_calls exceeds adaptive budget")
            if search.get("open_calls") is not None and int(search["open_calls"]) > 8:
                errors.append(f"{label}.open_calls exceeds adaptive budget")
            if search.get("hop_count") is not None and int(search["hop_count"]) > 3:
                errors.append(f"{label}.hop_count exceeds adaptive budget")
            if search.get("run_seconds") is not None and int(search["run_seconds"]) > 300:
                errors.append(f"{label}.run_seconds exceeds adaptive budget")
            if search.get("page_types") is not None and (
                not isinstance(search["page_types"], list)
                or any(not isinstance(item, str) for item in search["page_types"])
            ):
                errors.append(f"{label}.page_types must be a string list")
            if search.get("transition_log") is not None and not isinstance(search["transition_log"], list):
                errors.append(f"{label}.transition_log must be a list")
            error_category = search.get("error_category")
            if not isinstance(error_category, str) or len(error_category) > 100:
                errors.append(f"{label}.error_category must be a string of at most 100 characters")
            retry_count = search.get("retry_count")
            if isinstance(retry_count, bool) or not isinstance(retry_count, int) or retry_count < 0:
                errors.append(f"{label}.retry_count must be a non-negative integer")
            result_count = search.get("result_count")
            if (
                isinstance(result_count, bool)
                or not isinstance(result_count, int)
                or result_count < 0
            ):
                errors.append(f"{label}.result_count must be a non-negative integer")
            elif status == "EMPTY_VALID" and result_count != 0:
                errors.append(f"{label}.EMPTY_VALID must use result_count=0")
            elif status == "SUCCESS" and (result_count == 0 or not search_urls):
                errors.append(f"{label}.SUCCESS requires a result and a source URL")
            elif status in SEARCH_FAILURE_STATUSES and result_count != 0:
                errors.append(f"{label}.{status} must use result_count=0")
            if status in SEARCH_FAILURE_STATUSES and not str(error_category or "").strip():
                errors.append(f"{label}.{status} requires error_category")
            if status in {"SUCCESS", "EMPTY_VALID"} and str(error_category or "").strip():
                errors.append(f"{label}.{status} must use an empty error_category")
        if len(receipt_queries) != len(set(receipt_queries)):
            errors.append("batch.searches must contain exactly one receipt per query")
        if len(receipt_query_ids) != len(set(receipt_query_ids)):
            errors.append("batch.searches must contain unique query_id values")
        if set(receipt_queries) != configured_queries:
            errors.append("batch.searches must cover every batch query")
        search_url_union = {
            url
            for search in searches
            if isinstance(search, dict)
            for url in (search.get("source_urls") or [])
            if isinstance(url, str)
        }
        if isinstance(source_urls, list) and set(source_urls) != search_url_union:
            errors.append("batch.source_urls must equal the URLs recorded by batch.searches")
    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        if len(candidates) > 128:
            errors.append("candidates must contain at most 128 jobs")
        batch_urls = set(source_urls or []) if isinstance(source_urls, list) else set()
        batch_queries = set(batch.get("queries") or [])
        for index, candidate in enumerate(candidates):
            if not isinstance(candidate, dict):
                continue
            missing_candidate = sorted(REQUIRED_CANDIDATE_FIELDS - set(candidate))
            if missing_candidate:
                errors.append(
                    f"candidate[{index}] is missing fields: {', '.join(missing_candidate)}"
                )
            for field, maximum in {
                "company": 200,
                "title": 300,
                "location": 200,
                "source_name": 100,
                "source_type": 100,
                "description": 12000,
                "notes": 1000,
                "recruiting_context": 500,
            }.items():
                value = candidate.get(field)
                if not isinstance(value, str) or len(value) > maximum:
                    errors.append(
                        f"candidate[{index}].{field} must be a string of at most {maximum} characters"
                    )
            for field in ("company", "title", "source_name", "source_type"):
                if not str(candidate.get(field) or "").strip():
                    errors.append(f"candidate[{index}].{field} must not be empty")
            for field in ("posted_at", "deadline", "status", "canonical_url"):
                if candidate.get(field) is not None and not isinstance(candidate.get(field), str):
                    errors.append(f"candidate[{index}].{field} must be a string or null")
            if str(candidate.get("evidence_confidence") or "") not in {"High", "Medium", "Low"}:
                errors.append(f"candidate[{index}].evidence_confidence has an invalid value")
            if str(candidate.get("job_language") or "") not in {"ZH", "EN", "BOTH", "UNKNOWN"}:
                errors.append(f"candidate[{index}].job_language has an invalid value")
            source_evidence = candidate.get("source_evidence")
            if not isinstance(source_evidence, list) or not source_evidence or any(
                not isinstance(item, str) or not item.strip()
                for item in source_evidence
            ):
                errors.append(
                    f"candidate[{index}].source_evidence must be a non-empty list of factual excerpts"
                )
            for field in (
                "source_evidence", "graduation_evidence", "role_evidence", "location_evidence"
            ):
                value = candidate.get(field)
                if isinstance(value, list) and len(value) > 12:
                    errors.append(f"candidate[{index}].{field} must contain at most 12 items")
                if isinstance(value, list) and any(len(item) > 500 for item in value if isinstance(item, str)):
                    errors.append(f"candidate[{index}].{field} items must be at most 500 characters")
            if str(candidate.get("discovery_query") or "") not in batch_queries:
                errors.append(f"candidate[{index}].discovery_query must appear in batch.queries")
            receipt_ids = {
                str(item.get("query_id") or "")
                for item in searches
                if isinstance(item, dict)
            }
            if str(candidate.get("discovery_query_id") or "") not in receipt_ids:
                errors.append(f"candidate[{index}].discovery_query_id must match a receipt")
            if str(candidate.get("source_url") or "") not in batch_urls:
                errors.append(f"candidate[{index}].source_url must appear in batch.source_urls")
    return errors


def search_execution_summary(payload: dict[str, Any]) -> tuple[str, dict[str, int]]:
    batch = payload.get("batch") if isinstance(payload, dict) else None
    searches = batch.get("searches") if isinstance(batch, dict) else None
    if not isinstance(searches, list) or not searches:
        return "FAILED", {"attempted": 0, "succeeded": 0, "failed": 0}
    statuses = [
        str(item.get("status") or "").upper()
        for item in searches
        if isinstance(item, dict)
    ]
    succeeded = sum(status in {"SUCCESS", "EMPTY_VALID"} for status in statuses)
    failed = sum(status in SEARCH_FAILURE_STATUSES for status in statuses)
    summary = {"attempted": len(searches), "succeeded": succeeded, "failed": failed}
    if succeeded and failed:
        return "PARTIAL", summary
    if succeeded:
        return "SUCCESS", summary
    return "FAILED", summary


def _parse_date(value: Any, field: str) -> date:
    raw = str(value or "").strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date or datetime") from exc


def _parse_datetime(value: Any, field: str) -> datetime:
    raw = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO datetime") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed


def batch_freshness(payload: dict[str, Any], *, as_of: str | None = None) -> tuple[bool, str]:
    batch = payload.get("batch")
    if not isinstance(batch, dict):
        return False, "missing batch metadata"
    try:
        current = _parse_date(as_of or date.today().isoformat(), "as_of")
        generated = _parse_date(batch.get("generated_at"), "batch.generated_at")
        search_date = _parse_date(batch.get("search_date"), "batch.search_date")
        expiry = (
            _parse_date(batch.get("expires_at"), "batch.expires_at")
            if batch.get("expires_at") not in (None, "")
            else generated + timedelta(days=int(batch["max_age_days"]))
        )
    except (ValueError, TypeError, KeyError) as exc:
        return False, str(exc)
    if search_date > current or generated > current:
        return False, "batch date is in the future"
    if current > expiry:
        return False, f"batch expired on {expiry.isoformat()}"
    return True, f"fresh through {expiry.isoformat()}"


def prepare_gpt_candidate(candidate: dict[str, Any], *, imported_on: str) -> dict[str, Any]:
    prepared = dict(candidate)
    # MANUAL_AGENT discovery is not authoritative for stable source IDs. The
    # canonical normalizer may re-extract one later from a supported URL shape.
    prepared.pop("job_id", None)
    source_url = str(candidate.get("source_url") or candidate.get("url") or "").strip()
    supplied_canonical = str(
        candidate.get("canonical_url") or candidate.get("official_url") or ""
    ).strip()
    for field in (
        "discovered_by",
        "discovery_sources",
        "canonical_source",
        "canonical_url",
        "source_name",
        "evidence_confidence",
        *SYSTEM_PROVENANCE_FIELDS,
    ):
        prepared.pop(field, None)
    ats_family = detect_ats_family(supplied_canonical or source_url)
    source_urls = list(dict.fromkeys(filter(None, (source_url, supplied_canonical))))
    prepared.update(
        {
            "source": "gpt_web",
            "source_url": source_url,
            "url": source_url,
            "source_name": "gpt_web",
            "source_type": "public_web",
            "discovered_by": ["gpt_web"],
            "discovery_sources": ["gpt_web"],
            "evidence_confidence": "Medium",
            "discovered_at": str(candidate.get("discovered_at") or imported_on),
            "search_keyword": str(candidate.get("discovery_query") or "gpt_manual_agent"),
            "observation_origin": "CACHE_REPLAY",
            "ats_family": ats_family,
            "source_urls": source_urls,
        }
    )
    if candidate.get("status") in (None, ""):
        prepared.pop("status", None)
    return prepared


def load_gpt_candidates(path: str | Path) -> list[dict[str, Any]]:
    input_path = Path(path)
    if not input_path.exists():
        return []
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    # Compatibility-only loader for tests/tools that validate candidate rows in
    # isolation. The daily importer uses load_gpt_payload and never treats this
    # legacy v1 envelope as a current batch.
    if isinstance(payload, dict) and payload.get("schema_version") == 1:
        if (
            str(payload.get("mode") or "").upper() != "MANUAL_AGENT"
            or payload.get("source") != "gpt_web"
            or not isinstance(payload.get("candidates"), list)
            or set(payload) - {"schema_version", "mode", "source", "candidates"}
        ):
            raise ValueError("invalid legacy GPT discovery envelope")
        return payload["candidates"]
    payload_errors = validate_gpt_payload(payload)
    if payload_errors:
        raise ValueError("; ".join(payload_errors))
    candidates = payload["candidates"]
    return candidates


def load_gpt_payload(path: str | Path) -> dict[str, Any] | None:
    input_path = Path(path)
    if not input_path.exists():
        return None
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("GPT discovery input must be a schema envelope object")
    return payload


def import_gpt_candidates(
    candidates: list[dict[str, Any]],
    *,
    imported_on: str | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    imported_on = imported_on or date.today().isoformat()
    valid: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, candidate in enumerate(candidates):
        candidate_errors = validate_gpt_candidate(candidate)
        if candidate_errors:
            errors.extend(f"candidate[{index}]: {message}" for message in candidate_errors)
            continue
        valid.append(prepare_gpt_candidate(candidate, imported_on=imported_on))
    return valid, errors


def main() -> int:
    args = parse_args()
    payload = load_gpt_payload(args.input)
    if payload is None:
        if not args.check:
            write_json(args.output, [])
        record_source_health(
            "manual_agent_china_web",
            "FAILED",
            details="candidate batch is missing",
        )
        print("FAILED: GPT MANUAL_AGENT candidate batch is missing")
        return 1
    payload_errors = validate_gpt_payload(payload)
    plan: dict[str, Any] = {}
    plan_path = Path(args.query_plan)
    if plan_path.is_file():
        try:
            loaded_plan = json.loads(plan_path.read_text(encoding="utf-8"))
            if isinstance(loaded_plan, dict):
                plan = loaded_plan
                payload_errors.extend(validate_query_receipts(plan, payload))
        except (OSError, json.JSONDecodeError) as exc:
            payload_errors.append(f"public-web query plan cannot be read: {exc}")
    if payload_errors:
        if not args.check:
            write_json(args.output, [])
        reason = "; ".join(payload_errors)
        record_source_health(
            "manual_agent_china_web",
            "FAILED",
            details=reason,
        )
        print(f"FAILED: {reason}")
        return 1
    fresh, freshness_reason = batch_freshness(payload, as_of=args.as_of or None)
    if not fresh:
        if not args.check:
            write_json(args.output, [])
        record_source_health(
            "manual_agent_china_web",
            "REFRESH_REQUIRED",
            details=freshness_reason,
        )
        print(f"REFRESH_REQUIRED: {freshness_reason}")
        return 1
    candidates = payload["candidates"]
    valid, errors = import_gpt_candidates(candidates)
    for error in errors:
        print(f"ERROR: {error}")
    search_status, search_counts = search_execution_summary(payload)
    if search_status == "FAILED":
        valid = []
        if not args.check:
            write_json(args.output, [])
        status = "FAILED"
    elif search_status == "PARTIAL" or errors:
        status = "PARTIAL" if valid or search_counts["succeeded"] else "FAILED"
    else:
        status = "SUCCESS" if valid else "EMPTY_VALID"
    if not args.check:
        write_json(args.output, valid)
        print(f"Wrote {len(valid)} GPT MANUAL_AGENT jobs to {args.output}")
    else:
        print(f"Validated {len(valid)} GPT MANUAL_AGENT jobs")
    record_source_health(
        "manual_agent_china_web",
        status,
        count=len(valid),
        attempted=search_counts["attempted"],
        succeeded=search_counts["succeeded"],
        failed=search_counts["failed"] + len(errors),
        details={
            "batch_id": payload["batch"]["batch_id"],
            "freshness": freshness_reason,
            "candidate_count": len(valid),
            "plan_id": str(plan.get("plan_id") or "") if plan else "",
            "lane_receipts": lane_receipts(plan, payload) if plan else {},
            "query_receipts": [
                {
                    "query": str(item.get("query") or ""),
                    "plan_id": str(item.get("plan_id") or ""),
                    "query_id": str(item.get("query_id") or ""),
                    "lane": str(item.get("lane") or ""),
                    "provider": str(item.get("provider") or ""),
                    "status": str(item.get("status") or "").upper(),
                    "started_at": str(item.get("started_at") or ""),
                    "completed_at": str(item.get("completed_at") or ""),
                    "source_urls": list(item.get("source_urls") or []),
                    "result_count": int(item.get("result_count") or 0),
                    "error_category": str(item.get("error_category") or ""),
                    "retry_count": int(item.get("retry_count") or 0),
                }
                for item in payload["batch"].get("searches", [])
                if isinstance(item, dict)
            ],
        },
    )
    return 1 if errors or status == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
