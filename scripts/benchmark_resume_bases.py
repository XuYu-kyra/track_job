#!/usr/bin/env python3
"""Compile and measure each immutable approved Golden Base locally."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from generate_resume import compile_tex_to_pdf
    from resume_bases import import_registered_bases, load_resume_base_registry
    from resume_v2 import extract_pdf_geometry
except ModuleNotFoundError:
    from scripts.generate_resume import compile_tex_to_pdf
    from scripts.resume_bases import import_registered_bases, load_resume_base_registry
    from scripts.resume_v2 import extract_pdf_geometry


METRIC_KEYS = (
    "page_count",
    "page_width",
    "page_height",
    "first_text_y",
    "last_text_y",
    "text_vertical_span_ratio",
    "non_empty_line_count",
    "cjk_character_count",
    "english_word_count",
    "text_out_of_bounds",
    "text_overlap_detected",
    "text_clipped",
    "image_count",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark all approved Golden Base resumes.")
    parser.add_argument("--registry", default="config/resume_bases.yaml")
    parser.add_argument("--output", default="data/job_cache/resume_base_benchmarks.json")
    parser.add_argument("--base-id", action="append", default=[])
    parser.add_argument(
        "--reuse-existing",
        action="store_true",
        help="Measure already compiled PDFs; intended only after a full benchmark run",
    )
    return parser.parse_args()


def _compiler_findings(log_path: Path) -> list[str]:
    if not log_path.is_file():
        return ["compiler log missing"]
    text = log_path.read_text(encoding="utf-8", errors="replace").casefold()
    findings: list[str] = []
    for token, label in (
        ("latex error:", "LaTeX error marker"),
        ("fontspec error:", "fontspec error marker"),
        ("not loadable", "font not loadable"),
        ("missing character:", "missing character warning"),
    ):
        if token in text:
            findings.append(label)
    return findings


def benchmark_registered_bases(
    registry_path: str | Path = "config/resume_bases.yaml",
    *,
    repo_root: str | Path,
    reuse_existing: bool = False,
    selected_base_ids: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    root = Path(repo_root).resolve()
    import_registered_bases(
        registry_path,
        repo_root=root,
        selected_base_ids=selected_base_ids,
    )
    registry = load_resume_base_registry(registry_path, repo_root=root)
    results: dict[str, dict[str, Any]] = {}
    for base_id, item in sorted(registry["bases"].items()):
        if not item.get("enabled") or (
            selected_base_ids and base_id not in selected_base_ids
        ):
            continue
        base_dir = root / str(item["normalized_template_dir"])
        tex_path = base_dir / "main.tex"
        existing_pdf = tex_path.with_suffix(".pdf")
        pdf_path = (
            existing_pdf
            if reuse_existing and existing_pdf.is_file()
            else Path(compile_tex_to_pdf(tex_path))
        )
        raw = extract_pdf_geometry(pdf_path)
        metrics = {key: raw.get(key) for key in METRIC_KEYS}
        required_image = str(item["photo_variant"]) == "photo"
        metrics["required_image_present"] = (
            not required_image or int(metrics.get("image_count") or 0) > 0
        )
        metrics["compiler_findings"] = _compiler_findings(tex_path.with_suffix(".log"))
        metrics["font_substitution"] = (
            "WSL_ONLY" if (base_dir / "main-font-fallback.tex").is_file() else "NONE"
        )
        metrics["hard_gates_pass"] = all(
            (
                int(metrics.get("page_count") or 0) == 1,
                not bool(metrics.get("text_out_of_bounds")),
                not bool(metrics.get("text_overlap_detected")),
                not bool(metrics.get("text_clipped")),
                bool(metrics["required_image_present"]),
                not metrics["compiler_findings"],
            )
        )
        results[base_id] = metrics
    return results


def main() -> None:
    args = parse_args()
    root = Path(__file__).resolve().parents[1]
    results = benchmark_registered_bases(
        args.registry,
        repo_root=root,
        reuse_existing=args.reuse_existing,
        selected_base_ids=set(args.base_id) or None,
    )
    output = root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for base_id, metrics in results.items():
        print(
            f"{base_id}: pages={metrics['page_count']} "
            f"size={metrics['page_width']}x{metrics['page_height']} "
            f"span={metrics['text_vertical_span_ratio']} "
            f"lines={metrics['non_empty_line_count']} "
            f"hard_gates={'PASS' if metrics['hard_gates_pass'] else 'FAIL'}"
        )
    print(f"Wrote {len(results)} base benchmarks to {output}")


if __name__ == "__main__":
    main()
