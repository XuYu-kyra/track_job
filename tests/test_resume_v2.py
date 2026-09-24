from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from scripts.common import load_config
from scripts.generate_resume import load_resume_materials
from scripts.resume_v2 import (
    build_resume_content_plan,
    content_statistics,
    fit_resume_pdf,
    page_fit_state,
    plan_bullet_intents,
    render_resume_from_plan,
    run_quality_gates,
    validate_resume_blueprints,
)


ROOT = Path(__file__).resolve().parents[1]
AI_JOB = {
    "company": "深圳市金证科技股份有限公司",
    "title": "大模型应用开发工程师",
    "location": "深圳市",
    "role_family": "ai_application",
    "description": (
        "开发大模型Agent系统，涵盖模型接入、Prompt与工具链、FastAPI服务端、"
        "Tool Use、Memory、任务规划、多Agent和交互界面。"
    ),
}


class ResumeV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.materials = load_resume_materials(ROOT / "cv/materials")
        cls.template = (ROOT / "cv/resume.tex").read_text(encoding="utf-8")

    def test_all_five_role_blueprints_have_bounded_sections_and_slots(self) -> None:
        blueprints = validate_resume_blueprints(self.materials["profile.yaml"])
        self.assertEqual(
            set(blueprints["roles"]),
            {"test_development", "ai_application", "robot_software", "general_software", "backend"},
        )
        self.assertEqual(blueprints["page_fit"]["max_iterations"], 3)

    def test_ai_base_is_full_and_not_reduced_to_semantic_top_n(self) -> None:
        semantic = {
            "result": {
                "candidate_evidence_ranking": [
                    {"evidence_id": "RAG_API_01", "relevance": "HIGH", "reason": "direct"}
                ]
            }
        }
        plan = build_resume_content_plan(AI_JOB, self.materials, semantic)
        self.assertEqual(len(plan["education"]), 3)
        self.assertEqual(len(plan["experience"]), 1)
        self.assertEqual(len(plan["experience"][0]["intents"]), 4)
        self.assertEqual(
            [group["material"]["id"] for group in plan["projects"]],
            ["historical_dialogue_rag", "campus_job_pipeline", "msc_navigation_reliability"],
        )
        self.assertGreaterEqual(sum(len(group["intents"]) for group in plan["projects"]), 8)
        self.assertEqual(len(plan["skills"]), 4)
        skill_names = [name.casefold() for row in plan["skills"] for name in row["skills"]]
        self.assertEqual(len(skill_names), len(set(skill_names)))
        selected_ids = {eid for intent in plan_bullet_intents(plan) for eid in intent["evidence_ids"]}
        self.assertIn("RAG_API_01", selected_ids)
        self.assertIn("JOB_PIPELINE_01", selected_ids)
        self.assertTrue(any(eid.startswith("DISS_") for eid in selected_ids))

    def test_every_substantive_bullet_has_valid_evidence_and_three_fact_variants(self) -> None:
        plan = build_resume_content_plan(AI_JOB, self.materials)
        bank = {item["evidence_id"] for item in self.materials["evidence.yaml"]["evidence"]}
        for intent in plan_bullet_intents(plan):
            self.assertTrue(intent["evidence_ids"])
            self.assertTrue(set(intent["evidence_ids"]).issubset(bank))
            self.assertEqual(set(intent["variants"]), {"CN", "EN"})
            for language in ("CN", "EN"):
                self.assertEqual(
                    set(intent["variants"][language]), {"compact", "standard", "expanded"}
                )
                self.assertTrue(all(intent["variants"][language].values()))
            for variant in intent["variants"]["EN"].values():
                self.assertEqual(intent["metrics"], __import__("scripts.resume_v2", fromlist=["_metric_tokens"])._metric_tokens(variant))

    def test_cn_and_en_render_from_same_intent_plan_without_internal_metadata_in_body(self) -> None:
        plan = build_resume_content_plan(AI_JOB, self.materials)
        cn = render_resume_from_plan(self.template, plan, "CN")
        en = render_resume_from_plan(self.template, plan, "EN")
        ids = [intent["intent_id"] for intent in plan_bullet_intents(plan)]
        self.assertTrue(all(intent_id in cn.split("\\begin{document}", 1)[0] for intent_id in ids))
        self.assertTrue(all(intent_id in en.split("\\begin{document}", 1)[0] for intent_id in ids))
        for body in (cn.split("\\begin{document}", 1)[1], en.split("\\begin{document}", 1)[1]):
            self.assertNotIn("source_class", body)
            self.assertNotIn("confidence:", body)
        self.assertIn("硕士论文", cn)
        self.assertIn("MSc dissertation", en)

    def test_golden_base_density_cannot_silently_collapse(self) -> None:
        plan = build_resume_content_plan(AI_JOB, self.materials)
        cn = content_statistics(plan, "CN")
        en = content_statistics(plan, "EN")
        # Plan-only counts exclude headers, section labels, dates and locations;
        # rendered PDF geometry tests/acceptance measure the complete document.
        self.assertGreaterEqual(cn["cjk_character_count"], 700)
        self.assertGreaterEqual(en["english_word_count"], 325)
        self.assertGreaterEqual(cn["substantive_content_rows"], 16)
        self.assertGreaterEqual(en["substantive_content_rows"], 13)

    def test_page_fit_controller_stops_after_one_ready_measurement(self) -> None:
        plan = build_resume_content_plan(AI_JOB, self.materials)
        metrics = {
            "page_count": 1,
            "first_text_y": 30.0,
            "last_text_y": 800.0,
            "page_height": 841.0,
            "text_vertical_span_ratio": 0.93,
            "non_empty_line_count": 68,
            "cjk_character_count": 1050,
            "english_word_count": 400,
            "text": "rendered",
        }
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            result = fit_resume_pdf(
                self.template,
                plan,
                "CN",
                Path(tmp) / "resume.tex",
                compile_pdf=lambda path: calls.append(path) or str(path.with_suffix(".pdf")),
                measure_pdf=lambda _: metrics,
            )
        self.assertEqual(result["status"], "READY")
        self.assertEqual(len(calls), 1)

    def test_page_fit_controller_is_bounded_and_fails_closed_when_sparse(self) -> None:
        plan = build_resume_content_plan(AI_JOB, self.materials)
        sparse = {
            "page_count": 1,
            "first_text_y": 40.0,
            "last_text_y": 500.0,
            "page_height": 841.0,
            "text_vertical_span_ratio": 0.55,
            "non_empty_line_count": 30,
            "cjk_character_count": 500,
            "english_word_count": 200,
            "text": "sparse",
        }
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            result = fit_resume_pdf(
                self.template,
                plan,
                "EN",
                Path(tmp) / "resume.tex",
                compile_pdf=lambda path: calls.append(path) or str(path.with_suffix(".pdf")),
                measure_pdf=lambda _: sparse,
            )
        self.assertEqual(result["status"], "PAGE_FIT_REVIEW_REQUIRED")
        self.assertEqual(len(calls), 3)
        self.assertNotIn("adjustment", result["iterations"][-1])

    def test_page_state_uses_geometry_as_final_authority(self) -> None:
        contract = {"min_vertical_span_ratio": 0.89, "max_vertical_span_ratio": 0.95}
        self.assertEqual(page_fit_state({"page_count": 1, "text_vertical_span_ratio": 0.91}, contract), "READY")
        self.assertEqual(page_fit_state({"page_count": 2, "text_vertical_span_ratio": 0.91}, contract), "OVERFLOW")

    def _rollback_fixture(self, tmp: str):
        plan = build_resume_content_plan(AI_JOB, self.materials)
        one_page = {
            "page_count": 1,
            "first_text_y": 38.0,
            "last_text_y": 804.0,
            "page_height": 841.89,
            "text_vertical_span_ratio": 0.91,
            "non_empty_line_count": 65,
            "cjk_character_count": 1035,
            "english_word_count": 80,
            "text_out_of_bounds": False,
            "text_overlap_detected": False,
            "text": "single-page",
        }
        two_page = {
            "page_count": 2,
            "first_text_y": 34.0,
            "last_text_y": 806.0,
            "page_height": 841.89,
            "text_vertical_span_ratio": 0.94,
            "non_empty_line_count": 76,
            "cjk_character_count": 1200,
            "english_word_count": 100,
            "text_out_of_bounds": False,
            "text_overlap_detected": False,
            "page_text_block_counts": [65, 1],
            "text": "multi-page",
        }
        compiled: list[bytes] = []
        rendered_tex: list[str] = []

        def compile_candidate(path: Path) -> str:
            rendered_tex.append(path.read_text(encoding="utf-8"))
            marker = b"single-page-pdf" if not compiled else b"two-page-pdf"
            compiled.append(marker)
            path.with_suffix(".pdf").write_bytes(marker)
            return str(path.with_suffix(".pdf"))

        def measure_candidate(path: Path):
            return deepcopy(one_page if path.read_bytes() == b"single-page-pdf" else two_page)

        target = Path(tmp) / "resume.tex"
        result = fit_resume_pdf(
            self.template,
            plan,
            "CN",
            target,
            compile_pdf=compile_candidate,
            measure_pdf=measure_candidate,
        )
        return result, target, rendered_tex, one_page, two_page, measure_candidate

    def test_later_two_page_candidate_cannot_replace_saved_single_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _, _, _, _ = self._rollback_fixture(tmp)
        self.assertEqual(result["selected_candidate_iteration"], 1)
        self.assertEqual(result["metrics"]["page_count"], 1)
        self.assertIn("multi-page candidate", result["iterations"][-1]["rejected"])

    def test_soft_density_targets_cannot_outvote_single_page_constraint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _, one_page, two_page, _ = self._rollback_fixture(tmp)
        self.assertLess(one_page["cjk_character_count"], two_page["cjk_character_count"])
        self.assertLess(one_page["non_empty_line_count"], two_page["non_empty_line_count"])
        self.assertEqual(result["metrics"]["page_count"], 1)

    def test_relative_to_golden_ratio_is_diagnostic_only(self) -> None:
        contract = {
            "min_vertical_span_ratio": 0.92,
            "max_vertical_span_ratio": 0.95,
            "golden_vertical_span_ratio": 0.9391,
            "min_relative_to_golden_span": 0.95,
        }
        metrics = {
            "page_count": 1,
            "text_vertical_span_ratio": 0.90,
            "non_empty_line_count": 0,
            "cjk_character_count": 0,
            "english_word_count": 0,
        }
        self.assertGreater(metrics["text_vertical_span_ratio"] / 0.9391, 0.95)
        self.assertEqual(page_fit_state(metrics, contract), "TOO_SHORT")

    def test_failed_expansion_restores_complete_single_page_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, target, rendered_tex, _, _, _ = self._rollback_fixture(tmp)
            self.assertEqual(target.read_text(encoding="utf-8"), rendered_tex[0])
            self.assertEqual(target.with_suffix(".pdf").read_bytes(), b"single-page-pdf")
        self.assertEqual(
            len(plan_bullet_intents(result["plan"])),
            len(plan_bullet_intents(build_resume_content_plan(AI_JOB, self.materials))),
        )

    def test_reported_page_count_matches_restored_pdf_measurement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, target, _, _, _, measure = self._rollback_fixture(tmp)
            actual = measure(target.with_suffix(".pdf"))
        self.assertEqual(result["metrics"]["page_count"], actual["page_count"])

    def test_single_text_block_on_second_page_is_still_overflow(self) -> None:
        contract = {"min_vertical_span_ratio": 0.92, "max_vertical_span_ratio": 0.95}
        metrics = {
            "page_count": 2,
            "page_text_block_counts": [65, 1],
            "text_vertical_span_ratio": 0.94,
            "cjk_character_count": 1111,
            "non_empty_line_count": 66,
        }
        self.assertEqual(page_fit_state(metrics, contract), "OVERFLOW")

    def test_quality_gate_rejects_language_specific_fact_drift(self) -> None:
        plan = build_resume_content_plan(AI_JOB, self.materials)
        cn_plan = deepcopy(plan)
        en_plan = deepcopy(plan)
        en_plan["projects"][0]["intents"].pop()
        ready = {"status": "READY", "rendered_text": "", "plan": cn_plan}
        drifted = {"status": "READY", "rendered_text": "", "plan": en_plan}
        gates = run_quality_gates(plan, ready, drifted)
        self.assertEqual(gates["bilingual_consistency"], "FAIL")


if __name__ == "__main__":
    unittest.main()
