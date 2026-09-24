from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from argparse import Namespace
from collections import Counter
from copy import deepcopy
from pathlib import Path

from scripts.build_candidate_evidence import (
    build_runtime_evidence,
    validate_materials_directory,
)
from scripts.build_candidate_inventory import build_inventory
from scripts.common import load_config
from scripts.generate_resume import (
    build_skills_section,
    collect_candidate_bullets,
    extract_job_signals,
    infer_role_family,
    render_targeted_resume,
    resume_material_allowed,
)
from scripts.run_daily_pipeline import build_commands
from scripts.semantic_review import SemanticReviewValidationError, prepare_candidate_evidence
from scripts.source_adapters import load_source_registry


ROOT = Path(__file__).resolve().parents[1]


class CandidateMaterialsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.materials = load_config(ROOT / "cv/materials/evidence.yaml")

    def test_canonical_evidence_bank_contains_migrated_items_and_source_classes(self) -> None:
        evidence = self.materials["evidence"]
        ids = {item["evidence_id"] for item in evidence}
        migrated = {
            "DISS_NAV2_01",
            "DISS_AUTOMATION_01",
            "DISS_FAULT_01",
            "DISS_VALIDATION_01",
            "DISS_STATS_01",
            "DISS_CLASSIFIER_01",
            "DISS_TRACE_01",
            "DISS_RECOVERY_01",
            "ROBOT_VISION_01",
            "ROBOT_3D_01",
            "ROBOT_VALIDATION_01",
            "RAG_API_01",
            "RAG_VECTOR_01",
            "RAG_PROMPT_01",
            "BACKEND_DJANGO_01",
            "TEST_LOAD_01",
            "DATA_ANALYTICS_01",
            "JOB_PIPELINE_01",
            "JOB_TESTS_01",
        }
        self.assertTrue(migrated.issubset(ids))
        self.assertEqual(len(evidence), 29)
        self.assertEqual(
            Counter(item["source_class"] for item in evidence),
            {"VERIFIED": 17, "USER_ATTESTED": 12},
        )

    def test_runtime_evidence_is_derived_and_excludes_uncertain_items(self) -> None:
        payload = deepcopy(self.materials)
        payload["evidence"].append(
            {
                "evidence_id": "UNCERTAIN_EXAMPLE_01",
                "project": "Unverified",
                "category": "unknown",
                "factual_description": "This item must not enter runtime evidence.",
                "technologies": [],
                "verified_metrics": [],
                "results": [],
                "provenance": [],
                "suitable_role_families": [],
                "confidence": "LOW",
                "source_class": "UNCERTAIN",
            }
        )
        runtime = build_runtime_evidence(payload)
        self.assertEqual(runtime["generated_from"], "cv/materials/evidence.yaml")
        self.assertEqual(len(runtime["evidence"]), 29)
        self.assertNotIn(
            "UNCERTAIN_EXAMPLE_01",
            {item["evidence_id"] for item in runtime["evidence"]},
        )

    def test_semantic_contract_rejects_unknown_source_class(self) -> None:
        item = deepcopy(self.materials["evidence"][0])
        item["source_class"] = "TRUST_ME"
        with self.assertRaises(SemanticReviewValidationError):
            prepare_candidate_evidence([item])

    def test_live_materials_have_no_fictional_example_content(self) -> None:
        corpus = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "cv/materials").glob("*.yaml")
        )
        self.assertNotIn("Example Labs", corpus)
        self.assertNotIn("github.com/example/", corpus)
        self.assertNotIn("sample_ai_project", corpus)

    def test_rich_skills_and_source_classes_are_resume_safe(self) -> None:
        skills = load_config(ROOT / "cv/materials/skills.yaml")["skills"]
        rendered = build_skills_section(
            skills,
            "software_engineer",
            {"body": "fastapi docker", "matched_keywords": [], "focus_terms": []},
        )
        self.assertIn("FastAPI", rendered)
        self.assertIn("Docker", rendered)
        self.assertFalse(
            resume_material_allowed(
                {"source_class": "UNCERTAIN", "resume_eligible": True}
            )
        )

    def test_all_material_files_and_cross_references_validate(self) -> None:
        materials = validate_materials_directory(ROOT / "cv/materials")
        self.assertEqual(len(materials["evidence.yaml"]["evidence"]), 29)

    def test_material_validation_rejects_a_dangling_evidence_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            materials_dir = Path(tmp) / "materials"
            shutil.copytree(ROOT / "cv/materials", materials_dir)
            skills_path = materials_dir / "skills.yaml"
            text = skills_path.read_text(encoding="utf-8").replace(
                "[RAG_API_01, RAG_VECTOR_01, RAG_PROMPT_01, JOB_PIPELINE_01]",
                "[MISSING_EVIDENCE_01]",
                1,
            )
            skills_path.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "references missing evidence_id"):
                validate_materials_directory(materials_dir)

    def test_indentless_yaml_sequences_and_provenance_colons_are_supported(self) -> None:
        evidence = load_config(ROOT / "cv/materials/evidence.yaml")["evidence"]
        self.assertEqual(len(evidence), 29)
        self.assertIsInstance(evidence[0]["provenance"][0], str)
        self.assertTrue(evidence[0]["provenance"][0].startswith("repository:"))

    def test_inventory_preserves_attested_skill_proficiency_and_provenance(self) -> None:
        inventory = build_inventory(
            load_config(ROOT / "config/targets.example.yaml"),
            load_config(ROOT / "config/taxonomies.yaml"),
            ROOT / "cv/materials",
            ROOT / "data/candidate_repos/missing-manifest.json",
        )
        cpp = next(item for item in inventory["skills"] if item["skill"] == "cpp")
        self.assertEqual(cpp["proficiency"], "working")
        self.assertIn("USER_ATTESTED", cpp["source_classes"])
        self.assertIn("CPP_FOUNDATION_01", cpp["material_evidence_ids"])
        self.assertIn("robot_software", cpp["suitable_role_families"])

    def test_resume_helpers_use_canonical_roles_secondary_tags_and_cn_bullets(self) -> None:
        self.assertEqual(
            infer_role_family(
                {
                    "role_family": "test_development",
                    "title": "AI Engineer",
                    "description": "LLM",
                }
            ),
            "test_development",
        )
        self.assertEqual(
            infer_role_family({"resume_family": "robotics_engineer"}),
            "robot_software",
        )
        signals = extract_job_signals(
            {
                "title": "软件工程师",
                "secondary_role_tags": ["robotics", "embodied_ai"],
            },
            "general_software",
        )
        self.assertIn("robotics", signals["body"])
        experience = load_config(ROOT / "cv/materials/experience.yaml")["experience"][0]
        bullets = collect_candidate_bullets(experience, "test_development", "CN")
        self.assertTrue(bullets)
        self.assertTrue(any("负载测试" in bullet for bullet in bullets))

    def test_skill_proficiency_controls_resume_skill_order(self) -> None:
        rendered = build_skills_section(
            {
                "general_software": [
                    {
                        "name": "A Exposure",
                        "proficiency": "exposure",
                        "source_class": "VERIFIED",
                    },
                    {
                        "name": "Z Strong",
                        "proficiency": "strong",
                        "source_class": "VERIFIED",
                    },
                ]
            },
            "general_software",
            {"body": "", "matched_keywords": [], "focus_terms": []},
        )
        self.assertLess(rendered.index("Z Strong"), rendered.index("A Exposure"))

    def test_resume_education_is_rendered_from_active_materials(self) -> None:
        template = (ROOT / "cv/resume.tex").read_text(encoding="utf-8")
        education = load_config(ROOT / "cv/materials/education.yaml")["education"]
        rendered, _, _ = render_targeted_resume(
            template,
            {"title": "软件工程师", "description": ""},
            [],
            [],
            {},
            education_material=education,
        )
        self.assertNotIn("Example University", rendered)
        self.assertIn(education[0]["institution_cn"], rendered)
        self.assertIn(education[0]["qualification_cn"], rendered)

    def test_pipeline_builds_runtime_evidence_before_semantic_review(self) -> None:
        targets = load_config(ROOT / "config/targets.example.yaml")
        registry = load_source_registry(ROOT / "config/company_registry.example.yaml")
        args = Namespace(
            config="config/targets.example.yaml",
            feishu_config="config/feishu.example.yaml",
            execution_config="config/execution.example.yaml",
            source_registry="config/company_registry.example.yaml",
            taxonomy_config="config/taxonomies.yaml",
            mode="daily",
            batch_index=0,
            skip_fetch=True,
            skip_repo_sync=True,
            skip_documents=True,
            apply_feishu=False,
            semantic_review_dry_run=True,
            semantic_review_job_key=[],
            as_of="2026-09-10",
        )
        commands = build_commands(args, targets, "python3", registry)
        scripts = [command[1] for command in commands]
        self.assertLess(
            scripts.index("scripts/build_candidate_evidence.py"),
            scripts.index("scripts/semantic_review.py"),
        )

    def test_manifest_scanning_is_limited_to_curated_projects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            included = root / "included"
            excluded = root / "excluded"
            included.mkdir()
            excluded.mkdir()
            (included / "app.py").write_text("import fastapi\n", encoding="utf-8")
            (excluded / "main.cpp").write_text("int main(){}\n", encoding="utf-8")
            materials_dir = root / "materials"
            materials_dir.mkdir()
            (materials_dir / "projects.yaml").write_text(
                "projects:\n  - repo_name: included\n    evidence_scan: true\n",
                encoding="utf-8",
            )
            (materials_dir / "evidence.yaml").write_text(
                "evidence: []\n", encoding="utf-8"
            )
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "repositories": [
                            {"name": "included", "path": str(included)},
                            {"name": "excluded", "path": str(excluded)},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            targets = load_config(ROOT / "config/targets.example.yaml")
            targets["candidate_profile"]["repo_paths"] = []
            inventory = build_inventory(
                targets,
                load_config(ROOT / "config/taxonomies.yaml"),
                materials_dir,
                manifest,
            )
            self.assertEqual([item["name"] for item in inventory["projects"]], ["included"])

    def test_windows_manifest_paths_fall_back_to_local_cache_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "included"
            repo.mkdir()
            (repo / "app.py").write_text("import fastapi\n", encoding="utf-8")
            materials_dir = root / "materials"
            materials_dir.mkdir()
            (materials_dir / "projects.yaml").write_text(
                "projects:\n  - repo_name: included\n    evidence_scan: true\n",
                encoding="utf-8",
            )
            (materials_dir / "evidence.yaml").write_text(
                "evidence: []\n", encoding="utf-8"
            )
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "repositories": [
                            {
                                "name": "included",
                                "path": "data\\candidate_repos\\included",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            inventory = build_inventory(
                load_config(ROOT / "config/targets.example.yaml"),
                load_config(ROOT / "config/taxonomies.yaml"),
                materials_dir,
                manifest,
            )
            self.assertEqual(inventory["projects"][0]["file_count"], 1)

    def test_excluded_self_clone_is_not_needed_for_verified_job_evidence(self) -> None:
        targets = load_config(ROOT / "config/targets.example.yaml")
        targets["candidate_profile"]["repo_paths"] = []
        inventory = build_inventory(
            targets,
            load_config(ROOT / "config/taxonomies.yaml"),
            ROOT / "cv/materials",
            ROOT / "data/candidate_repos/missing-manifest.json",
        )
        material_ids = {
            evidence_id
            for skill in inventory["skills"]
            for evidence_id in skill.get("material_evidence_ids", [])
        }
        self.assertIn("JOB_PIPELINE_01", material_ids)
        self.assertIn("JOB_TESTS_01", material_ids)

    def test_cpp_alias_does_not_collapse_to_single_character_false_positive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "ordinary-python"
            repo.mkdir()
            (repo / "app.py").write_text(
                "class Candidate:\n    pass\n", encoding="utf-8"
            )
            targets = load_config(ROOT / "config/targets.example.yaml")
            targets["candidate_profile"]["repo_paths"] = [str(repo)]
            inventory = build_inventory(
                targets,
                load_config(ROOT / "config/taxonomies.yaml"),
                root / "missing-materials",
                root / "missing-manifest.json",
            )
            self.assertNotIn("cpp", {item["skill"] for item in inventory["skills"]})


if __name__ == "__main__":
    unittest.main()
