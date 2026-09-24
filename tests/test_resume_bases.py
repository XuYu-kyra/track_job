from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from scripts.generate_resume import load_resume_materials
from scripts.resume_bases import (
    APPROVED_BASE_IDS,
    _safe_zip_members,
    apply_base_content_manifest,
    build_tailoring_report,
    import_registered_bases,
    load_resume_base_content,
    load_resume_base_registry,
    prepare_selected_template,
    select_resume_base,
    sha256_file,
)
from scripts.resume_v2 import build_resume_content_plan, plan_bullet_intents, render_resume_from_plan
from scripts.sync_resume_base_sources import _with_benchmark_metrics, _with_source_hash
from scripts.privacy_check import should_skip
from scripts.update_feishu import generated_attachment_paths


class ResumeBaseArchitectureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.registry = load_resume_base_registry(repo_root=cls.root)
        cls.content = load_resume_base_content(repo_root=cls.root)
        cls.materials = load_resume_materials(cls.root / "cv" / "materials")
        import_registered_bases(repo_root=cls.root)

    def _job(self, role_family: str, title: str = "2027 Graduate Engineer") -> dict:
        return {
            "company": "Example",
            "title": title,
            "position": title,
            "description": "2027 graduate role in Shenzhen using Python, testing and APIs.",
            "location": "深圳",
            "role_family": role_family,
            "action": "READY",
            "matching_score": 90,
        }

    def _base_plan(self, role: str = "ai") -> tuple[dict, dict]:
        selection = select_resume_base(
            self._job(self.content["roles"][role]["plan_role"]),
            self.registry,
            language="CN",
        )
        plan = build_resume_content_plan(
            self._job(selection["plan_role"]),
            self.materials,
            {},
            role_family=selection["plan_role"],
        )
        plan = apply_base_content_manifest(
            plan,
            role,
            self.content,
            self.registry["defaults"]["tailoring"],
        )
        plan["selected_base_id"] = selection["base_id"]
        plan["selected_base"] = selection
        return plan, selection

    def test_registry_contains_exactly_twelve_enabled_bases(self) -> None:
        enabled = {
            base_id for base_id, item in self.registry["bases"].items() if item["enabled"]
        }
        self.assertEqual(enabled, APPROVED_BASE_IDS)

    def test_every_source_zip_exists_and_matches_registered_sha(self) -> None:
        for base_id, item in self.registry["bases"].items():
            source = self.root / item["source_zip"]
            self.assertTrue(source.is_file(), base_id)
            self.assertEqual(sha256_file(source), item["source_sha256"], base_id)

    def test_import_is_idempotent_and_rejects_zip_slip(self) -> None:
        first = import_registered_bases(repo_root=self.root)
        second = import_registered_bases(repo_root=self.root)
        self.assertEqual(first, second)
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "unsafe.zip"
            with ZipFile(archive_path, "w") as archive:
                archive.writestr("../escape.tex", "unsafe")
            with ZipFile(archive_path) as archive:
                with self.assertRaisesRegex(ValueError, "unsafe ZIP member"):
                    _safe_zip_members(archive)

    def test_sync_registry_updates_only_the_selected_base_block(self) -> None:
        registry_text = (self.root / "config" / "resume_bases.yaml").read_text(
            encoding="utf-8"
        )
        digest = "a" * 64
        updated = _with_source_hash(
            registry_text, "robot.cn.no_photo.v1", digest
        )
        self.assertIn(f"    source_sha256: {digest}", updated)
        self.assertEqual(updated.count(f"    source_sha256: {digest}"), 1)
        self.assertEqual(
            registry_text.split("  robot.cn.no_photo.v1:", 1)[0],
            updated.split("  robot.cn.no_photo.v1:", 1)[0],
        )

        metrics = {
            "page_count": 1,
            "page_width": 1.0,
            "page_height": 2.0,
            "first_text_y": 0.1,
            "last_text_y": 1.9,
            "text_vertical_span_ratio": 0.9,
            "non_empty_line_count": 42,
            "cjk_character_count": 100,
            "english_word_count": 10,
            "text_out_of_bounds": False,
            "text_overlap_detected": False,
            "text_clipped": False,
            "image_count": 0,
            "required_image_present": True,
            "font_substitution": "NONE",
        }
        updated = _with_benchmark_metrics(
            updated, "robot.cn.no_photo.v1", metrics
        )
        selected_block = updated.split("  robot.cn.no_photo.v1:", 1)[1].split(
            "  robot.cn.photo.v1:", 1
        )[0]
        self.assertIn("      non_empty_line_count: 42", selected_block)
        self.assertIn('      font_substitution: "NONE"', selected_block)

    def test_private_import_directory_remains_outside_public_tree_scan(self) -> None:
        self.assertTrue(
            should_skip(Path("cv/templates/bases/ai/cn/no_photo/v1/main.tex"))
        )
        self.assertTrue(should_skip(Path("data/job_cache/jobs.json")))

    def test_four_primary_roles_select_distinct_approved_bases(self) -> None:
        expected = {
            "ai_application": "ai.cn.no_photo.v1",
            "robot_software": "robot.cn.no_photo.v1",
            "general_software": "se.cn.no_photo.v1",
            "test_development": "test.cn.no_photo.v1",
        }
        for role_family, base_id in expected.items():
            selected = select_resume_base(
                self._job(role_family), self.registry, language="CN"
            )
            self.assertEqual(selected["base_id"], base_id)

    def test_cn_photo_and_no_photo_are_explicit(self) -> None:
        job = self._job("ai_application")
        self.assertEqual(
            select_resume_base(job, self.registry, language="CN", variant="photo")["base_id"],
            "ai.cn.photo.v1",
        )
        self.assertEqual(
            select_resume_base(job, self.registry, language="CN", variant="no_photo")["base_id"],
            "ai.cn.no_photo.v1",
        )

    def test_english_photo_requires_explicit_fallback(self) -> None:
        job = self._job("ai_application")
        with self.assertRaisesRegex(ValueError, "no approved Golden Base"):
            select_resume_base(job, self.registry, language="EN", variant="photo")
        selected = select_resume_base(
            job,
            self.registry,
            language="EN",
            variant="photo",
            allow_english_photo_fallback=True,
        )
        self.assertEqual(selected["base_id"], "ai.en.no_photo.v1")
        self.assertTrue(selected["fallback_used"])

    def test_test_and_backend_do_not_collapse_to_wrong_base(self) -> None:
        self.assertEqual(
            select_resume_base(
                self._job("test_development"), self.registry, language="CN"
            )["role_base"],
            "test",
        )
        for role in ("backend", "general_software", "fintech_backend"):
            self.assertEqual(
                select_resume_base(self._job(role), self.registry, language="CN")["role_base"],
                "se",
            )

    def test_algorithm_research_cannot_become_eligible_via_ai_base(self) -> None:
        with self.assertRaisesRegex(ValueError, "excluded"):
            select_resume_base(
                self._job("algorithm_research"), self.registry, language="CN"
            )

    def test_chinese_base_uses_markers_not_english_section_anchor(self) -> None:
        plan, selection = self._base_plan("test")
        with tempfile.TemporaryDirectory() as directory:
            template = prepare_selected_template(
                selection, Path(directory) / "resume", repo_root=self.root
            )
            self.assertNotIn(r"\section{Education}", template)
            rendered = render_resume_from_plan(template, plan, "CN")
        self.assertIn(r"\section{测试与技术能力}", rendered)
        self.assertNotIn("{{RESUME_SECTION_", rendered)

    def test_photo_asset_is_copied_and_no_photo_has_no_reference(self) -> None:
        photo = select_resume_base(
            self._job("robot_software"), self.registry, language="CN", variant="photo"
        )
        no_photo = select_resume_base(
            self._job("robot_software"), self.registry, language="CN", variant="no_photo"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            photo_template = prepare_selected_template(
                photo, root / "photo-resume", repo_root=self.root
            )
            self.assertTrue((root / "photo-resume-headshot.jpg").is_file())
            self.assertIn("photo-resume-headshot.jpg", photo_template)
            plain_template = prepare_selected_template(
                no_photo, root / "plain-resume", repo_root=self.root
            )
            self.assertNotIn("headshot", plain_template.casefold())

    def test_tailoring_stays_within_budget_and_claims_have_evidence(self) -> None:
        plan, selection = self._base_plan("ai")
        report = build_tailoring_report(plan, selected_base=selection)
        self.assertEqual(report["status"], "PASS")
        self.assertLessEqual(report["tailoring_percentage"], 0.30)
        evidence_bank = set(plan["evidence_bank_ids"])
        for intent in plan_bullet_intents(plan):
            self.assertTrue(intent["evidence_ids"])
            self.assertTrue(set(intent["evidence_ids"]).issubset(evidence_bank))

    def test_cn_and_en_share_one_fact_plan(self) -> None:
        plan, _ = self._base_plan("robot")
        for intent in plan_bullet_intents(plan):
            self.assertEqual(set(intent["variants"]), {"CN", "EN"})
            self.assertEqual(
                intent["metrics"],
                __import__("scripts.resume_v2", fromlist=["_metric_tokens"])._metric_tokens(
                    intent["factual_scope"]
                ),
            )

    def test_all_bases_store_valid_single_page_benchmarks(self) -> None:
        for base_id, item in self.registry["bases"].items():
            metrics = item["benchmark_metrics"]
            self.assertEqual(metrics["page_count"], 1, base_id)
            self.assertGreater(metrics["page_width"], 0, base_id)
            self.assertGreater(metrics["page_height"], 0, base_id)
            self.assertFalse(metrics["text_out_of_bounds"], base_id)
            self.assertFalse(metrics["text_overlap_detected"], base_id)
            self.assertFalse(metrics["text_clipped"], base_id)
            self.assertTrue(metrics["required_image_present"], base_id)
            if item["photo_variant"] == "photo":
                self.assertGreater(metrics["image_count"], 0, base_id)
            else:
                self.assertEqual(metrics["image_count"], 0, base_id)

    def test_unrendered_source_wording_is_explicitly_reported(self) -> None:
        plan, selection = self._base_plan("se")
        report = build_tailoring_report(plan, selected_base=selection)
        self.assertTrue(report["source_wording_review_slots"])

    def test_feishu_attachment_gate_rejects_human_review_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "resume.pdf"
            pdf.write_bytes(b"%PDF-placeholder")
            generated = {
                "resume_pdf_path": str(pdf),
                "resume_variants": {
                    "CN": {
                        "pdf_path": str(pdf),
                        "status": "HUMAN_REVIEW_REQUIRED",
                    }
                },
            }
            self.assertEqual(generated_attachment_paths(generated), {})


if __name__ == "__main__":
    unittest.main()
