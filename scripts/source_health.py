#!/usr/bin/env python3
"""Small shared contract for per-run discovery source health."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


HEALTH_STATUSES = frozenset(
    {
        "SUCCESS",
        "PARTIAL",
        "PARTIAL_RATE_LIMITED",
        "EMPTY_VALID",
        "REFRESH_REQUIRED",
        "FAILED",
        "DISABLED",
        "MANUAL_BY_DESIGN",
    }
)
DEFAULT_HEALTH_PATH = Path("data/job_cache/source_health.json")


def configured_health_path() -> Path:
    override = str(os.environ.get("TRACK_JOB_HEALTH_PATH") or "").strip()
    return Path(override) if override else DEFAULT_HEALTH_PATH


def _now(timezone: str = "Asia/Shanghai") -> str:
    return datetime.now(ZoneInfo(timezone)).isoformat(timespec="seconds")


def reset_source_health(
    path: str | Path | None = None,
    *,
    timezone: str = "Asia/Shanghai",
) -> dict[str, Any]:
    payload = {"run_started_at": _now(timezone), "timezone": timezone, "sources": {}}
    target = Path(path) if path is not None else configured_health_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def record_source_health(
    lane: str,
    status: str,
    *,
    count: int = 0,
    attempted: int = 0,
    succeeded: int = 0,
    failed: int = 0,
    details: Any = None,
    automatic: bool = False,
    path: str | Path | None = None,
    timezone: str = "Asia/Shanghai",
) -> dict[str, Any]:
    normalized = str(status).upper()
    if normalized not in HEALTH_STATUSES:
        raise ValueError(f"Unsupported source health status: {status}")
    target = Path(path) if path is not None else configured_health_path()
    if target.exists():
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            payload = reset_source_health(target, timezone=timezone)
    else:
        payload = reset_source_health(target, timezone=timezone)
    sources = payload.setdefault("sources", {})
    sources[lane] = {
        "status": normalized,
        "count": max(0, int(count)),
        "attempted": max(0, int(attempted)),
        "succeeded": max(0, int(succeeded)),
        "failed": max(0, int(failed)),
        "checked_at": _now(timezone),
        "details": details if details not in (None, "") else [],
        "automatic": bool(automatic),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return sources[lane]


def automatic_discovery_succeeded(payload: dict[str, Any]) -> bool:
    sources = payload.get("sources", {}) if isinstance(payload, dict) else {}
    automatic = [
        record
        for record in sources.values()
        if isinstance(record, dict) and record.get("automatic") is True
    ]
    return bool(automatic) and any(
        record.get("status") in {
            "SUCCESS", "PARTIAL", "PARTIAL_RATE_LIMITED", "EMPTY_VALID"
        }
        for record in automatic
    )
