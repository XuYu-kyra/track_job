#!/usr/bin/env python3
"""Synchronize changed Golden Base ZIPs with their managed derived artifacts.

This is the explicit working-base refresh path. It updates the registry hash,
reimports ``main.tex`` and assets, rebuilds the normalized marker template,
compiles/benchmarks the affected bases, and writes the fresh benchmark metrics
back to the registry. Unchanged bases are left byte-for-byte alone.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

try:
    from benchmark_resume_bases import METRIC_KEYS, benchmark_registered_bases
    from common import load_config
    from resume_bases import import_registered_bases, sha256_file
except ModuleNotFoundError:
    from scripts.benchmark_resume_bases import METRIC_KEYS, benchmark_registered_bases
    from scripts.common import load_config
    from scripts.resume_bases import import_registered_bases, sha256_file


REGISTRY_METRIC_KEYS = (
    *METRIC_KEYS,
    "required_image_present",
    "font_substitution",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refresh changed Golden Base ZIPs and every managed derivative."
    )
    parser.add_argument("--registry", default="config/resume_bases.yaml")
    parser.add_argument(
        "--benchmark-output",
        default="data/job_cache/resume_base_benchmarks.json",
        help="Merge refreshed benchmark results into this cache",
    )
    parser.add_argument(
        "--role",
        action="append",
        default=[],
        help="Limit synchronization to a role base such as robot, ai, se or test",
    )
    parser.add_argument("--base-id", action="append", default=[])
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reimport and rebenchmark selected bases even when ZIP hashes match",
    )
    return parser.parse_args()


def _base_block_pattern(base_id: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?ms)^  {re.escape(base_id)}:\n.*?(?=^  [^ \n][^\n]*:\n|\Z)"
    )


def _replace_base_block(text: str, base_id: str, transform) -> str:
    pattern = _base_block_pattern(base_id)
    match = pattern.search(text)
    if not match:
        raise ValueError(f"resume base is missing from registry text: {base_id}")
    replacement = transform(match.group(0))
    return text[: match.start()] + replacement + text[match.end() :]


def _with_source_hash(text: str, base_id: str, digest: str) -> str:
    def transform(block: str) -> str:
        updated, count = re.subn(
            r"(?m)^    source_sha256: [0-9a-f]{64}$",
            f"    source_sha256: {digest}",
            block,
            count=1,
        )
        if count != 1:
            raise ValueError(f"resume base has no replaceable source_sha256: {base_id}")
        return updated

    return _replace_base_block(text, base_id, transform)


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _with_benchmark_metrics(
    text: str, base_id: str, metrics: dict[str, Any]
) -> str:
    metric_lines = ["    benchmark_metrics:"]
    for key in REGISTRY_METRIC_KEYS:
        metric_lines.append(f"      {key}: {_yaml_scalar(metrics.get(key))}")
    replacement = "\n".join(metric_lines) + "\n"

    def transform(block: str) -> str:
        updated, count = re.subn(
            r"(?ms)^    benchmark_metrics:\n.*?(?=^    enabled:)",
            replacement,
            block,
            count=1,
        )
        if count != 1:
            raise ValueError(f"resume base has no benchmark_metrics block: {base_id}")
        return updated

    return _replace_base_block(text, base_id, transform)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def sync_resume_base_sources(
    registry_path: str | Path,
    *,
    repo_root: str | Path,
    roles: set[str] | None = None,
    base_ids: set[str] | None = None,
    force: bool = False,
    benchmark_output: str | Path = "data/job_cache/resume_base_benchmarks.json",
) -> list[dict[str, Any]]:
    root = Path(repo_root).resolve()
    path = Path(registry_path)
    if not path.is_absolute():
        path = root / path
    payload = load_config(path)
    bases = payload.get("bases") or {}
    selected: dict[str, dict[str, Any]] = {}
    for base_id, item in bases.items():
        if not isinstance(item, dict) or not item.get("enabled"):
            continue
        if roles and str(item.get("role_base") or "") not in roles:
            continue
        if base_ids and base_id not in base_ids:
            continue
        source = root / str(item.get("source_zip") or "")
        actual_hash = sha256_file(source)
        if force or actual_hash != str(item.get("source_sha256") or ""):
            selected[base_id] = {**item, "actual_hash": actual_hash}

    if not selected:
        return []

    registry_text = path.read_text(encoding="utf-8")
    for base_id, item in selected.items():
        registry_text = _with_source_hash(
            registry_text, base_id, str(item["actual_hash"])
        )

    selected_ids = set(selected)
    staged_registry = path.with_name(f".{path.name}.sync-stage")
    _atomic_write(staged_registry, registry_text)
    try:
        import_registered_bases(
            staged_registry,
            repo_root=root,
            selected_base_ids=selected_ids,
            refresh_changed=True,
        )
        benchmarks = benchmark_registered_bases(
            staged_registry,
            repo_root=root,
            selected_base_ids=selected_ids,
        )
    finally:
        staged_registry.unlink(missing_ok=True)
    failed = [
        base_id
        for base_id, metrics in benchmarks.items()
        if not bool(metrics.get("hard_gates_pass"))
    ]
    if failed:
        raise RuntimeError(
            "refusing to approve refreshed resume bases with failed hard gates: "
            + ", ".join(sorted(failed))
        )

    for base_id, metrics in benchmarks.items():
        registry_text = _with_benchmark_metrics(
            registry_text, base_id, metrics
        )

    benchmark_path = Path(benchmark_output)
    if not benchmark_path.is_absolute():
        benchmark_path = root / benchmark_path
    cached_benchmarks: dict[str, Any] = {}
    if benchmark_path.is_file():
        loaded = json.loads(benchmark_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"benchmark cache must contain an object: {benchmark_path}")
        cached_benchmarks = loaded
    cached_benchmarks.update(benchmarks)
    # Commit metadata only after every refreshed base passes the hard gates.
    # If import or compilation fails, the official registry retains its old
    # hash, so the next normal invocation will select the ZIP again.
    _atomic_write(
        benchmark_path,
        json.dumps(cached_benchmarks, indent=2, ensure_ascii=False) + "\n",
    )
    _atomic_write(path, registry_text)

    return [
        {
            "base_id": base_id,
            "source_zip": str(item["source_zip"]),
            "source_sha256": str(item["actual_hash"]),
            "normalized_template_dir": str(item["normalized_template_dir"]),
            "benchmark_metrics": benchmarks[base_id],
        }
        for base_id, item in sorted(selected.items())
    ]


def main() -> None:
    args = parse_args()
    root = Path(__file__).resolve().parents[1]
    records = sync_resume_base_sources(
        args.registry,
        repo_root=root,
        roles=set(args.role) or None,
        base_ids=set(args.base_id) or None,
        force=args.force,
        benchmark_output=args.benchmark_output,
    )
    if not records:
        print("No selected resume source ZIP changed; nothing to synchronize.")
        return
    for item in records:
        metrics = item["benchmark_metrics"]
        print(
            f"{item['base_id']}: {item['source_sha256']} -> "
            f"{item['normalized_template_dir']} "
            f"(pages={metrics['page_count']}, hard_gates=PASS)"
        )
    print(f"Synchronized {len(records)} resume base source ZIPs and derivatives.")


if __name__ == "__main__":
    main()
