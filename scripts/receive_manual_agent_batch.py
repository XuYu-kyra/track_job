#!/usr/bin/env python3
"""Validate and atomically promote one externally researched MANUAL_AGENT batch."""

from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

try:
    from import_gpt_discovery import (
        batch_freshness,
        import_gpt_candidates,
        load_gpt_payload,
        search_execution_summary,
        validate_gpt_payload,
    )
    from public_web_query_plan import validate_query_receipts
except ModuleNotFoundError:
    from scripts.import_gpt_discovery import (
        batch_freshness,
        import_gpt_candidates,
        load_gpt_payload,
        search_execution_summary,
        validate_gpt_payload,
    )
    from scripts.public_web_query_plan import validate_query_receipts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Receive a fresh MANUAL_AGENT v3 batch.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", default="data/discovery/gpt_manual/candidates.json")
    parser.add_argument("--as-of", default="")
    parser.add_argument("--query-plan", default="")
    parser.add_argument("--merge", action="store_true", help="Merge this shard with existing receipts")
    return parser.parse_args()


SUCCESS_TERMINAL = {"SUCCESS", "EMPTY_VALID"}
MAX_RECEIPT_NOTES = 500


def normalize_receipt_derived_fields(payload: dict) -> dict:
    """Rebuild redundant envelope fields from the authoritative receipts.

    Search models occasionally alter punctuation while copying a long URL into
    ``batch.source_urls``. The per-query receipt is the evidence-bearing source,
    so the trusted receiver deterministically rebuilds the top-level union before
    validation. Candidate URLs must still match a receipt and remain fail-closed.
    A malformed candidate is dropped rather than invalidating otherwise valid
    query receipts, so one model URL typo cannot discard an entire shard.
    """

    batch = payload.get("batch") if isinstance(payload, dict) else None
    if not isinstance(batch, dict):
        return payload
    receipts = batch.get("searches")
    if not isinstance(receipts, list):
        return payload
    # Search notes are advisory rejection context. Keep the receiver resilient
    # when a provider returns a verbose note, while preserving the authoritative
    # URLs, counts, and candidate evidence used for acceptance decisions.
    for receipt in receipts:
        if not isinstance(receipt, dict):
            continue
        notes = receipt.get("notes")
        if isinstance(notes, str) and len(notes) > MAX_RECEIPT_NOTES:
            receipt["notes"] = notes[: MAX_RECEIPT_NOTES - 1].rstrip() + "…"
    # Fallback searches are allowed inside a receipt's bounded search budget,
    # but candidate provenance remains keyed to the original plan query. If a
    # model copies the fallback text into discovery_query, canonicalize it from
    # the trusted query_id before strict payload validation. A mismatched or
    # unknown query_id is left untouched and will fail closed below.
    receipt_queries = {
        str(receipt.get("query_id") or ""): str(receipt.get("query") or "")
        for receipt in receipts
        if isinstance(receipt, dict)
        and str(receipt.get("query_id") or "").strip()
        and str(receipt.get("query") or "").strip()
    }
    candidates_for_provenance = payload.get("candidates")
    canonicalized = 0
    if isinstance(candidates_for_provenance, list):
        for candidate in candidates_for_provenance:
            if not isinstance(candidate, dict):
                continue
            canonical_query = receipt_queries.get(
                str(candidate.get("discovery_query_id") or "")
            )
            if canonical_query and candidate.get("discovery_query") != canonical_query:
                candidate["discovery_query"] = canonical_query
                canonicalized += 1
    if canonicalized:
        print(
            "RECEIVER: canonicalized "
            f"{canonicalized} candidate discovery_query value(s) from trusted receipt IDs"
        )
    batch["source_urls"] = list(
        dict.fromkeys(
            url
            for receipt in receipts
            if isinstance(receipt, dict)
            for url in receipt.get("source_urls", [])
            if isinstance(url, str)
        )
    )
    receipt_urls = set(batch["source_urls"])
    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        retained: list[dict] = []
        dropped = 0
        for candidate in candidates:
            if not isinstance(candidate, dict):
                dropped += 1
                continue
            source_url = candidate.get("source_url")
            if not isinstance(source_url, str) or source_url not in receipt_urls:
                dropped += 1
                continue
            retained.append(candidate)
        if dropped:
            print(
                "RECEIVER: dropped "
                f"{dropped} candidate(s) whose source_url was not in a trusted search receipt"
            )
        payload["candidates"] = retained
    return payload


def merge_batches(existing: dict, incoming: dict, plan: dict) -> dict:
    """Merge receipt shards idempotently using stable query IDs."""

    plan_order = [str(item.get("query_id") or "") for item in plan.get("queries", [])]
    query_by_id = {
        str(item.get("query_id") or ""): str(item.get("query") or "")
        for item in plan.get("queries", [])
        if isinstance(item, dict)
    }
    receipt_by_id = {
        str(item.get("query_id") or ""): dict(item)
        for item in (existing.get("batch", {}).get("searches") or [])
        if isinstance(item, dict)
    }
    incoming_ids: set[str] = set()
    preserved_success_ids: set[str] = set()
    for item in incoming.get("batch", {}).get("searches") or []:
        if not isinstance(item, dict):
            continue
        query_id = str(item.get("query_id") or "")
        incoming_ids.add(query_id)
        prior = receipt_by_id.get(query_id)
        if prior and str(prior.get("status") or "").upper() in SUCCESS_TERMINAL:
            preserved_success_ids.add(query_id)
            continue
        receipt_by_id[query_id] = dict(item)
    receipts = [receipt_by_id[item] for item in plan_order if item in receipt_by_id]

    retained_candidates = [
        dict(item)
        for item in existing.get("candidates", [])
        if isinstance(item, dict)
        and (
            str(item.get("discovery_query_id") or "") not in incoming_ids
            or str(item.get("discovery_query_id") or "") in preserved_success_ids
        )
    ]
    candidates = retained_candidates + [
        dict(item) for item in incoming.get("candidates", []) if isinstance(item, dict)
    ]
    deduplicated: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in candidates:
        key = (
            str(item.get("discovery_query_id") or ""),
            str(item.get("canonical_url") or item.get("source_url") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(item)

    batch = dict(incoming["batch"])
    batch["batch_id"] = f"{plan.get('plan_id')}-aggregate"
    batch["plan_id"] = str(plan.get("plan_id") or "")
    batch["searches"] = receipts
    batch["queries"] = [query_by_id[item] for item in plan_order if item in receipt_by_id]
    batch["source_urls"] = list(
        dict.fromkeys(
            url
            for receipt in receipts
            for url in receipt.get("source_urls", [])
            if isinstance(url, str)
        )
    )
    return {
        "schema_version": incoming["schema_version"],
        "mode": incoming["mode"],
        "source": incoming["source"],
        "batch": batch,
        "candidates": deduplicated,
    }


def receive_batch(
    input_path: str | Path,
    output_path: str | Path,
    *,
    as_of: str = "",
    query_plan_path: str | Path = "",
    merge_existing: bool = False,
) -> dict:
    payload = load_gpt_payload(input_path)
    if payload is None:
        raise ValueError("MANUAL_AGENT output file is missing")
    payload = normalize_receipt_derived_fields(payload)
    errors = validate_gpt_payload(payload)
    plan: dict = {}
    if query_plan_path:
        plan_path = Path(query_plan_path)
        if not plan_path.is_file():
            errors.append("supplied public-web query plan is missing")
        else:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            errors.extend(validate_query_receipts(plan, payload))
    if errors:
        raise ValueError("; ".join(errors))
    fresh, freshness_reason = batch_freshness(payload, as_of=as_of or None)
    if not fresh:
        raise ValueError(f"MANUAL_AGENT batch is not fresh: {freshness_reason}")
    search_status, counts = search_execution_summary(payload)
    _, candidate_errors = import_gpt_candidates(
        payload["candidates"], imported_on=as_of or date.today().isoformat()
    )
    if candidate_errors:
        raise ValueError("; ".join(candidate_errors))

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if merge_existing and destination.is_file():
        existing = load_gpt_payload(destination)
        existing_errors = validate_gpt_payload(existing)
        if plan:
            existing_errors.extend(validate_query_receipts(plan, existing))
        if existing_errors:
            raise ValueError("existing receipt store is invalid: " + "; ".join(existing_errors))
        payload = merge_batches(existing, payload, plan)
        # A retry may repeat a query whose earlier receipt was already
        # accepted.  ``merge_batches`` intentionally keeps that trusted
        # receipt, so re-derive the candidate URL union once more before the
        # final validation.  This drops a candidate that cites a URL returned
        # only by the discarded retry receipt instead of rejecting the whole
        # aggregate (and preserves fail-closed provenance).
        payload = normalize_receipt_derived_fields(payload)
        merged_errors = validate_gpt_payload(payload)
        if plan:
            merged_errors.extend(validate_query_receipts(plan, payload))
        if merged_errors:
            raise ValueError("merged receipt store is invalid: " + "; ".join(merged_errors))
        search_status, counts = search_execution_summary(payload)
    temporary = destination.with_name(destination.name + ".receiving")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, destination)
    return {
        "batch_id": payload["batch"]["batch_id"],
        "search_status": search_status,
        "searches": counts,
        "candidates": len(payload["candidates"]),
        "freshness": freshness_reason,
    }


def main() -> int:
    args = parse_args()
    summary = receive_batch(
        args.input,
        args.output,
        as_of=args.as_of,
        query_plan_path=args.query_plan,
        merge_existing=args.merge,
    )
    print(
        "Accepted MANUAL_AGENT batch "
        f"{summary['batch_id']}: searches={summary['searches']['attempted']}, "
        f"candidates={summary['candidates']}, status={summary['search_status']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
