from __future__ import annotations

import json
import tempfile
import unittest
from argparse import Namespace
from copy import deepcopy
from pathlib import Path

from scripts.build_analytics import build_analytics
from scripts.build_candidate_inventory import build_inventory
from scripts.build_execution_plan import build_execution_plan, workload_policy
from scripts.common import canonical_job_key, load_config, normalize_token
from scripts.generate_resume import profile_contact_latex
from scripts.fetch_official import parse_career_page
from scripts.job_schema import backfill_discoverable, normalize_job
from scripts.import_manual_jobs import infer_source
from scripts.merge_job_sources import merge_jobs
from scripts.query_matrix import build_query_matrix
from scripts.privacy_check import should_skip
from scripts.run_daily_pipeline import build_commands
from scripts.score_jobs import evaluate_job
from scripts.setup_feishu import compare_schema, configured_specs, field_spec
from scripts.source_adapters import get_source_adapter, load_source_registry
from scripts.sync_candidate_repos import select_candidate_repositories
from scripts.update_feishu import (
    FeishuConfig,
    build_fields_payload,
    generated_attachment_paths,
)
from scripts.update_question_bank import update_bank


ROOT = Path(__file__).resolve().parents[1]


class SchemaAndMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.taxonomies = load_config(ROOT / "config/taxonomies.yaml")

    def test_unicode_normalization_and_key(self) -> None:
        self.assertEqual(normalize_token("深圳 · 测试开发"), "深圳 测试开发")
        self.assertEqual(
            canonical_job_key("示例科技", "测试开发工程师", "深圳"),
            "示例科技|测试开发工程师|深圳",
        )

    def test_normalize_canonical_schema(self) -> None:
        job = normalize_job(
            {
                "company": "Example Robotics",
                "position": "机器人软件工程师",
                "location": "深圳",
                "url": "https://example.com/1",
                "source": "manual",
                "date": "2026-08-20",
                "description": "ROS2 Linux C++ simulation",
            },
            self.taxonomies,
            as_of="2026-08-23",
        )
        self.assertEqual(job["title"], "机器人软件工程师")
        self.assertEqual(job["role_family"], "robot_software")
        self.assertEqual(job["first_seen"], "2026-08-23")
        self.assertEqual(job["posted_at"], "2026-08-20")
        self.assertEqual(job["last_observed"], "")
        self.assertEqual(job["freshness"], "unknown")

    def test_dream_role_survives_normalization(self) -> None:
        job = normalize_job(
            {
                "company": "Example",
                "title": "Software Engineer",
                "location": "深圳",
                "dream_role": True,
            },
            self.taxonomies,
            as_of="2026-08-23",
        )
        self.assertTrue(job["dream_role"])

    def test_merge_sources_and_age_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.json"
            second = Path(tmp) / "second.json"
            first.write_text(
                json.dumps(
                    [
                        {
                            "company": "Example Cloud",
                            "position": "测试开发工程师",
                            "location": "深圳",
                            "url": "https://source-a/1",
                            "source": "linkedin",
                            "observation_origin": "LIVE_FETCH",
                            "observed_at": "2026-08-23",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            second.write_text(
                json.dumps(
                    [
                        {
                            "company": "Example Cloud",
                            "title": "测试开发工程师",
                            "location": "深圳",
                            "url": "https://source-b/1",
                            "source": "official",
                            "description": "Python pytest automation",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            merged = merge_jobs(
                [str(first), str(second)],
                taxonomies=self.taxonomies,
                as_of="2026-08-23",
            )
            self.assertEqual(len(merged), 1)
            self.assertEqual(set(merged[0]["sources"]), {"linkedin", "official"})
            self.assertEqual(len(merged[0]["source_urls"]), 2)

            stale = merge_jobs(
                [],
                taxonomies=self.taxonomies,
                previous_jobs=merged,
                as_of="2026-09-08",
                stale_after_days=14,
            )
            self.assertEqual(stale[0]["freshness"], "stale")
            self.assertEqual(stale[0]["status"], "STALE")

    def test_three_source_dedup_prefers_official_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            canonical = "https://official.example/job"
            for index, source in enumerate(("linkedin", "official", "nowcoder")):
                path = Path(tmp) / f"{source}.json"
                path.write_text(
                    json.dumps(
                        [
                            {
                                "company": "Example Cloud",
                                "title": "后端开发工程师",
                                "location": "深圳",
                                "url": canonical if source == "official" else f"https://{source}.example/job",
                                "source": source,
                                "canonical_url": canonical,
                                "canonical_authority": (
                                    "OFFICIAL_MONITOR" if source == "official" else ""
                                ),
                                "verification_level": (
                                    "CANONICAL" if source == "official" else ""
                                ),
                                "observation_origin": (
                                    "LIVE_FETCH" if source == "official" else "CACHE_REPLAY"
                                ),
                                "observed_at": (
                                    "2026-09-06" if source == "official" else ""
                                ),
                                "verified_at": (
                                    "2026-09-06" if source == "official" else ""
                                ),
                                "description": "面向2027届毕业生 Python backend",
                            }
                        ]
                    ),
                    encoding="utf-8",
                )
                paths.append(str(path))
            merged = merge_jobs(
                paths,
                taxonomies=self.taxonomies,
                source_registry=load_source_registry(ROOT / "config/company_registry.example.yaml"),
                as_of="2026-09-06",
            )
            self.assertEqual(len(merged), 1)
            self.assertEqual(merged[0]["source"], "official")
            self.assertEqual(set(merged[0]["sources"]), {"official", "linkedin", "nowcoder"})
            self.assertEqual(merged[0]["official_url"], "https://official.example/job")


class DecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.targets = load_config(ROOT / "config/targets.example.yaml")
        self.scoring = load_config(ROOT / "config/scoring.yaml")
        self.taxonomies = load_config(ROOT / "config/taxonomies.yaml")
        self.inventory = {
            "skills": [
                {"skill": name, "evidence_count": 1, "evidence": ["test"]}
                for name in ("python", "cpp", "testing_automation", "robotics", "linux_cloud")
            ]
        }

    def _normalize(self, raw: dict) -> dict:
        return normalize_job(raw, self.taxonomies, as_of="2026-08-23")

    def _normalize_verified(self, raw: dict) -> dict:
        prepared = dict(raw)
        prepared["description"] = (
            str(prepared.get("description") or "")
            + " Python Linux software engineering responsibilities and qualifications. " * 3
        )
        prepared.update(
            {
                "source": "official",
                "url": "https://jobs.example.test/verified-role",
                "canonical_authority": "OFFICIAL_MONITOR",
                "source_authoritative": True,
                "source_canonicality": "CANONICAL",
                "verification_level": "CANONICAL",
                "observation_origin": "LIVE_FETCH",
                "observed_at": "2026-08-23",
                "verified_at": "2026-08-23",
                "status": "OPEN",
            }
        )
        return self._normalize(prepared)

    def test_security_sensitive_is_hard_reject(self) -> None:
        job = self._normalize_verified(
            {
                "company": "Secure Co",
                "title": "2027 软件工程师",
                "location": "深圳",
                "description": "涉密岗位，实行上交护照和私人出境审批。",
            }
        )
        result = evaluate_job(
            job,
            self.scoring,
            self.targets,
            self.taxonomies,
            self.inventory,
            as_of="2026-08-23",
        )
        self.assertEqual(result["action"], "REJECT")
        self.assertIn("security_sensitive", result["reject_reason"])

    def test_backend_title_with_2027_jd_is_eligible(self) -> None:
        job = self._normalize_verified(
            {
                "company": "Backend Co",
                "title": "后端开发工程师",
                "location": "深圳",
                "description": "面向2027届毕业生，负责 Python API 开发。",
            }
        )
        result = evaluate_job(job, self.scoring, self.targets, self.taxonomies, self.inventory, as_of="2026-08-23")
        self.assertEqual(result["eligibility"], "ELIGIBLE")
        self.assertTrue(any(item.startswith("description:") for item in result["graduation_evidence"]))

    def test_page_campaign_context_proves_campus_eligibility(self) -> None:
        job = self._normalize_verified(
            {
                "company": "Campus Co",
                "title": "Software Engineer",
                "location": "深圳",
                "description": "Build backend APIs.",
                "page_context": "Campus Co 2027 Campus Recruitment",
            }
        )
        result = evaluate_job(job, self.scoring, self.targets, self.taxonomies, self.inventory, as_of="2026-08-23")
        self.assertEqual(result["eligibility"], "ELIGIBLE")
        self.assertTrue(any(item.startswith("page_context:") for item in result["graduation_evidence"]))

    def test_ai_application_collaboration_is_not_algorithm_reject(self) -> None:
        job = self._normalize(
            {
                "company": "AI Co",
                "title": "AI Application Engineer",
                "location": "深圳",
                "description": "2027 campus role; collaborate with NLP algorithm team to build RAG applications.",
            }
        )
        result = evaluate_job(job, self.scoring, self.targets, self.taxonomies, self.inventory, as_of="2026-08-23")
        self.assertNotEqual(result["reject_reason"], "low_career_value")
        self.assertNotEqual(result["action"], "REJECT")

    def test_senior_title_is_rejected(self) -> None:
        job = self._normalize(
            {
                "company": "Senior Co",
                "title": "Senior Software Engineer",
                "location": "深圳",
                "description": "2027 campus recruitment",
            }
        )
        result = evaluate_job(job, self.scoring, self.targets, self.taxonomies, self.inventory, as_of="2026-08-23")
        self.assertEqual(result["action"], "REJECT")

    def test_explicit_2026_campus_cohort_is_rejected(self) -> None:
        job = self._normalize(
            {
                "company": "Old Cohort Co",
                "title": "2026 Software Engineer",
                "location": "深圳",
                "description": "Campus recruitment role",
            }
        )
        result = evaluate_job(job, self.scoring, self.targets, self.taxonomies, self.inventory, as_of="2026-08-23")
        self.assertEqual(result["action"], "REJECT")
        self.assertIn("not_new_grad", result["reject_reason"])

    def test_centrally_retained_private_passport_is_rejected(self) -> None:
        job = self._normalize(
            {
                "company": "Restricted Co",
                "title": "软件工程师",
                "location": "深圳",
                "description": "2027届岗位，入职后因私护照集中保管。",
            }
        )
        result = evaluate_job(job, self.scoring, self.targets, self.taxonomies, self.inventory, as_of="2026-08-23")
        self.assertEqual(result["action"], "REJECT")
        self.assertIn("security_sensitive", result["reject_reason"])

    def test_vague_security_language_requires_review(self) -> None:
        job = self._normalize(
            {
                "company": "Review Co",
                "title": "Software Engineer",
                "location": "深圳",
                "description": "2027 campus recruitment; standard background check required.",
            }
        )
        result = evaluate_job(job, self.scoring, self.targets, self.taxonomies, self.inventory, as_of="2026-08-23")
        self.assertEqual(result["action"], "WATCH")
        self.assertEqual(result["reject_reason"], "security_review")

    def test_open_30_day_job_with_future_deadline_is_backfill_discoverable(self) -> None:
        self.assertTrue(
            backfill_discoverable(
                {
                    "status": "OPEN",
                    "posted_at": "2026-08-01",
                    "deadline": "2026-09-30",
                },
                "2026-09-06",
                max_age_days=60,
            )
        )
        self.assertFalse(
            backfill_discoverable(
                {
                    "status": "OPEN",
                    "posted_at": "2026-08-20",
                    "deadline": "2026-09-01",
                },
                "2026-09-06",
                max_age_days=60,
            )
        )

    def test_time_sensitive_dream_role_breaks_hold(self) -> None:
        job = self._normalize_verified(
            {
                "company": "Robot Co",
                "title": "2027 Robotics Software Engineer",
                "location": "深圳",
                "deadline": "2026-08-24",
                "dream_role": True,
                "description": "2027届 ROS2 C++ Python Linux simulation testing validation core R&D 招满即止 弹性工作 年假",
            }
        )
        result = evaluate_job(
            job,
            self.scoring,
            self.targets,
            self.taxonomies,
            self.inventory,
            as_of="2026-08-23",
        )
        self.assertEqual(result["action"], "MUST_APPLY")
        self.assertGreaterEqual(result["urgency"], 8)

    def test_must_apply_bypasses_high_capacity_load(self) -> None:
        targets = deepcopy(self.targets)
        targets["application_capacity"]["active_technical_processes"] = 99
        targets["application_capacity"]["process_load_next_72h"] = 99
        job = self._normalize_verified(
            {
                "company": "Robot Co",
                "title": "2027 Robotics Software Engineer",
                "location": "深圳",
                "deadline": "2026-08-24",
                "dream_role": True,
                "description": "2027届 ROS2 C++ Python Linux simulation testing validation core R&D 招满即止 弹性工作 年假",
            }
        )
        result = evaluate_job(job, self.scoring, targets, self.taxonomies, self.inventory, as_of="2026-08-23")
        self.assertEqual(result["action"], "MUST_APPLY")

    def test_normal_role_holds_during_dissertation(self) -> None:
        job = self._normalize_verified(
            {
                "company": "Cloud Co",
                "title": "测试开发工程师 2027届",
                "location": "深圳",
                "description": "Python pytest automation testing API Linux CI/CD engineering tools",
            }
        )
        result = evaluate_job(
            job,
            self.scoring,
            self.targets,
            self.taxonomies,
            self.inventory,
            as_of="2026-08-23",
        )
        self.assertEqual(result["action"], "HOLD")

    def test_sample_funnel_matches_expected_summary(self) -> None:
        inventory = build_inventory(
            self.targets,
            self.taxonomies,
            ROOT / "cv/materials",
        )
        jobs = merge_jobs(
            [str(ROOT / "examples/jobs.sample.json")],
            taxonomies=self.taxonomies,
            as_of="2026-08-23",
        )
        evaluated = [
            evaluate_job(
                job,
                self.scoring,
                self.targets,
                self.taxonomies,
                inventory,
                as_of="2026-08-23",
            )
            for job in jobs
        ]
        counts = {
            action: sum(1 for job in evaluated if job["action"] == action)
            for action in ("MUST_APPLY", "READY", "HOLD", "WATCH", "REJECT")
        }
        expected = json.loads(
            (ROOT / "examples/decision_summary.expected.json").read_text(encoding="utf-8")
        )["counts"]
        self.assertEqual(counts, expected)


class SearchConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.targets = load_config(ROOT / "config/targets.example.yaml")
        self.taxonomies = load_config(ROOT / "config/taxonomies.yaml")
        self.registry = load_source_registry(ROOT / "config/company_registry.example.yaml")

    def test_query_matrix_is_bounded_and_role_balanced(self) -> None:
        matrix = build_query_matrix(
            self.targets,
            self.taxonomies,
            mode="daily",
            source="linkedin",
        )
        self.assertEqual(len(matrix), 24)
        expected = set(self.targets["candidate_profile"]["preferred_role_families"])
        expected.update(self.targets["candidate_profile"]["expansion_role_families"])
        self.assertTrue(expected.issubset({item["role_family"] for item in matrix}))
        self.assertTrue(all(item["graduation"] and item["location"] for item in matrix))
        general_queries = [item for item in matrix if item["role_family"] == "general_software"]
        self.assertEqual(len({item["role_alias"] for item in general_queries}), 2)
        self.assertTrue(
            {"Shenzhen", "Hong Kong SAR", "United Kingdom"}.issubset(
                {item["location"] for item in matrix}
            )
        )

    def test_source_adapter_exposes_truthful_registry_metadata(self) -> None:
        adapter = get_source_adapter("nowcoder", self.registry)
        normalized = adapter.normalize(
            {
                "company": "Example",
                "title": "后端开发工程师",
                "location": "深圳",
                "url": "https://example.com/job",
            },
            self.taxonomies,
            as_of="2026-09-06",
        )
        self.assertEqual(normalized["source_tier"], 2)
        self.assertEqual(normalized["source_implementation_status"], "manual_agent_search")
        self.assertIn("campus recruitment", normalized["source_context"])

    def test_manual_inbox_infers_china_source_labels_without_crawling(self) -> None:
        self.assertEqual(infer_source("https://www.nowcoder.com/jobs/1"), "nowcoder")
        self.assertEqual(infer_source("https://www.ncss.cn/job/1"), "ncss")
        self.assertEqual(infer_source("https://www.zhipin.com/job/1"), "boss")
        self.assertEqual(infer_source("https://mp.weixin.qq.com/s/example"), "wechat")

    def test_registered_official_page_preserves_campaign_context(self) -> None:
        jobs = parse_career_page(
            """
            <html><head><title>2027 校园招聘</title></head><body>
              <h1>Example 2027 校园招聘</h1>
              <div class="job-card">
                <a href="/jobs/1">后端开发工程师</a>
                <span>2027 校园招聘</span>
              </div>
              <a href="/about">关于我们</a>
            </body></html>
            """,
            company="Example",
            career_url="https://careers.example.com/campus",
            location="深圳",
            company_type="private_technology",
            aliases=["后端开发工程师"],
            graduation_terms=["2027", "校园招聘"],
            max_links=10,
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].url, "https://careers.example.com/jobs/1")
        self.assertIn("2027", jobs[0].campaign_context)
        self.assertEqual(jobs[0].company_type, "private_technology")

    def test_repository_selection_excludes_static_portfolio(self) -> None:
        selected = select_candidate_repositories(
            [
                {
                    "name": "my-portfolio.github.io",
                    "description": "static site portfolio",
                    "language": "HTML",
                    "fork": False,
                    "archived": False,
                },
                {
                    "name": "backend-api",
                    "description": "Python backend API with tests",
                    "language": "Python",
                    "fork": False,
                    "archived": False,
                },
            ]
        )
        self.assertEqual([item["name"] for item in selected], ["backend-api"])

    def test_repository_selection_honors_configured_name_exclusions(self) -> None:
        selected = select_candidate_repositories(
            [
                {
                    "name": "track_job",
                    "description": "software automation pipeline",
                    "language": "Python",
                },
                {
                    "name": "kept-api",
                    "description": "backend api",
                    "language": "Python",
                },
            ],
            excluded_names={"track_job"},
        )
        self.assertEqual([item["name"] for item in selected], ["kept-api"])

    def test_synced_candidate_repo_cache_is_outside_working_tree_privacy_scan(self) -> None:
        self.assertTrue(
            should_skip(
                Path("data/candidate_repos/public-project/src/example.py")
            )
        )

    def test_gitignored_local_tool_runtime_is_outside_privacy_scan(self) -> None:
        self.assertTrue(should_skip(Path(".tools/pymupdf/fitz/__init__.py")))

    def test_repository_evidence_is_discovery_only_until_materials_are_curated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "api-project"
            (repo / "src").mkdir(parents=True)
            (repo / "tests").mkdir()
            (repo / "src/app.py").write_text("from fastapi import FastAPI\n", encoding="utf-8")
            (repo / "src/routes.py").write_text("import fastapi\n", encoding="utf-8")
            (repo / "tests/test_api.py").write_text("from fastapi.testclient import TestClient\n", encoding="utf-8")
            targets = deepcopy(self.targets)
            targets["candidate_profile"]["repo_paths"] = [str(repo)]
            inventory = build_inventory(
                targets,
                self.taxonomies,
                Path(tmp) / "empty-materials",
                Path(tmp) / "missing-manifest.json",
            )
            self.assertNotIn(
                "backend_api", {item["skill"] for item in inventory["skills"]}
            )
            backend = inventory["projects"][0]["skill_evidence"]["backend_api"]
            self.assertTrue(any(item["file"] == "src/app.py" for item in backend))

    def test_dependency_alone_is_not_strong_skill_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "dependency-only"
            repo.mkdir()
            (repo / "requirements.txt").write_text("fastapi==1.0\n", encoding="utf-8")
            targets = deepcopy(self.targets)
            targets["candidate_profile"]["repo_paths"] = [str(repo)]
            inventory = build_inventory(
                targets,
                self.taxonomies,
                Path(tmp) / "empty-materials",
                Path(tmp) / "missing-manifest.json",
            )
            self.assertNotIn("backend_api", {item["skill"] for item in inventory["skills"]})

    def test_backfill_command_path_never_contains_feishu_or_documents(self) -> None:
        args = Namespace(
            config="config/targets.yaml",
            feishu_config="config/feishu.yaml",
            execution_config="config/execution.yaml",
            source_registry="config/company_registry.yaml",
            taxonomy_config="config/taxonomies.yaml",
            mode="backfill",
            batch_index=0,
            skip_fetch=True,
            skip_repo_sync=True,
            skip_documents=False,
            apply_feishu=False,
            as_of="2026-09-06",
        )
        commands = build_commands(args, self.targets, "python3", self.registry)
        flattened = "\n".join(" ".join(command) for command in commands)
        self.assertNotIn("update_feishu.py", flattened)
        self.assertNotIn("generate_resume.py", flattened)
        self.assertIn("--mode backfill", flattened)

    def test_daily_automated_sources_are_registry_ordered(self) -> None:
        args = Namespace(
            config="config/targets.yaml",
            feishu_config="config/feishu.yaml",
            execution_config="config/execution.yaml",
            source_registry="config/company_registry.yaml",
            taxonomy_config="config/taxonomies.yaml",
            mode="daily",
            batch_index=0,
            skip_fetch=False,
            skip_repo_sync=True,
            skip_documents=True,
            apply_feishu=False,
            as_of="",
        )
        commands = build_commands(args, self.targets, "python3", self.registry)
        scripts = [command[1] for command in commands]
        self.assertLess(scripts.index("scripts/fetch_official.py"), scripts.index("scripts/fetch_linkedin.py"))
        self.assertNotIn("scripts/fetch_indeed.py", scripts)

    def test_profile_contact_order_and_optional_links(self) -> None:
        contact = profile_contact_latex(
            {
                "full_name": "",
                "email": "you@example.com",
                "phone": "+86 10000",
                "github_url": "https://github.com/example",
                "linkedin_url": "",
                "portfolio_url": "",
                "closing_name": "",
            }
        )
        self.assertLess(contact.index("you@example.com"), contact.index("+86 10000"))
        self.assertLess(contact.index("+86 10000"), contact.index("GitHub"))
        self.assertNotIn("LinkedIn", contact)
        self.assertNotIn("Portfolio", contact)


class FeishuAndFeedbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = FeishuConfig(
            app_id="",
            app_secret="",
            app_token="",
            table_id="",
            view_id="",
            fields={
                "company": "Company",
                "title": "Title",
                "official_url": "Official URL",
                "opportunity_value": "Opportunity Value",
                "action": "Action",
                "stage": "Stage",
                "score_breakdown": "Score Breakdown",
            },
            defaults={},
            attachments={},
            sync={"update_human_managed_fields": False},
            alerts={},
        )

    def test_feishu_update_preserves_human_fields(self) -> None:
        definitions = {
            "Official URL": {"ui_type": "Url"},
            "Opportunity Value": {"ui_type": "Number"},
            "Action": {"ui_type": "SingleSelect"},
            "Stage": {"ui_type": "SingleSelect"},
        }
        job = {
            "company": "Example",
            "title": "Engineer",
            "official_url": "https://example.com/job",
            "opportunity_value": 88,
            "action": "READY",
            "stage": "INTERVIEW",
        }
        fields = build_fields_payload(job, None, self.config, definitions, for_update=True)
        self.assertEqual(fields["Action"], "READY")
        self.assertNotIn("Stage", fields)
        self.assertEqual(fields["Official URL"]["link"], "https://example.com/job")

    def test_schema_check_reports_missing_not_mutation(self) -> None:
        specs = configured_specs(self.config)
        missing, mismatches = compare_schema(specs, {"Company": {"type": 1}})
        self.assertTrue(any(item["field_name"] == "Title" for item in missing))
        self.assertEqual(mismatches, [])

    def test_schema_check_reports_missing_select_options(self) -> None:
        spec = field_spec("role_family", "Role Family")
        _, mismatches = compare_schema(
            {"Role Family": spec},
            {
                "Role Family": {
                    "type": 3,
                    "property": {"options": [{"name": "backend"}]},
                }
            },
        )
        self.assertTrue(any("missing options=" in item for item in mismatches))

    def test_schema_check_accepts_null_property_for_plain_field(self) -> None:
        spec = field_spec("company", "Company")
        missing, mismatches = compare_schema(
            {"Company": spec},
            {"Company": {"type": 1, "property": None}},
        )
        self.assertEqual(missing, [])
        self.assertEqual(mismatches, [])

    def test_only_page_fit_approved_resume_is_attachable(self) -> None:
        generated = {
            "resume_pdf_path": "cv/generated/resumes/example-cn.pdf",
            "coverletter_pdf_path": "cv/generated/coverletters/example.pdf",
            "resume_variants": {
                "CN": {
                    "status": "PAGE_FIT_REVIEW_REQUIRED",
                    "pdf_path": "cv/generated/resumes/example-cn.pdf",
                }
            },
        }
        self.assertEqual(generated_attachment_paths(generated), {})
        generated["resume_variants"]["CN"]["status"] = "READY_WITH_DENSITY_WARNING"
        self.assertEqual(
            generated_attachment_paths(
                generated,
                {
                    "action": "READY",
                    "verification_state": "VERIFIED_OPEN_JOB",
                    "canonicality": "CANONICAL",
                    "canonical_url": "https://example.test/job/1",
                    "status": "OPEN",
                    "freshness": "fresh",
                    "description": "x" * 200,
                },
            ),
            {
                "resume": "cv/generated/resumes/example-cn.pdf",
                "cover": "cv/generated/coverletters/example.pdf",
            },
        )

    def test_windows_manifest_attachment_path_is_uploadable_from_posix(self) -> None:
        from unittest.mock import Mock, patch

        from scripts.update_feishu import FeishuConfig, upload_bitable_attachment

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "generated" / "resume.pdf"
            path.parent.mkdir()
            path.write_bytes(b"pdf")
            windows_style = str(path).replace("/", "\\")
            response = Mock()
            response.status_code = 200
            response.json.return_value = {"code": 0, "data": {"file_token": "token"}}
            config = FeishuConfig(
                app_id="", app_secret="", app_token="app", table_id="table",
                view_id="", fields={}, defaults={}, attachments={}, sync={}, alerts={},
            )
            with patch("scripts.update_feishu.requests.post", return_value=response):
                self.assertEqual(
                    upload_bitable_attachment(config, "tenant", windows_style),
                    "token",
                )

    def test_missing_attachment_is_not_silently_ignored(self) -> None:
        from scripts.update_feishu import FeishuAPIError, FeishuConfig, upload_bitable_attachment

        config = FeishuConfig(
            app_id="", app_secret="", app_token="app", table_id="table",
            view_id="", fields={}, defaults={}, attachments={}, sync={}, alerts={},
        )
        with self.assertRaisesRegex(FeishuAPIError, "does not exist"):
            upload_bitable_attachment(config, "tenant", "missing\\resume.pdf")

    def test_attachment_tokens_remain_structured_for_bitable(self) -> None:
        config = FeishuConfig(
            app_id="", app_secret="", app_token="app", table_id="table",
            view_id="", fields={"resume_draft": "Resume Draft"}, defaults={},
            attachments={}, sync={}, alerts={},
        )
        fields = build_fields_payload(
            {},
            None,
            config,
            {"Resume Draft": {"ui_type": "Attachment"}},
            {"resume": "file-token"},
            for_update=True,
        )
        self.assertEqual(fields["Resume Draft"], [{"file_token": "file-token"}])

    def test_question_bank_deduplicates_and_updates_status(self) -> None:
        debrief = {
            "company": "Example",
            "role_family": "backend",
            "round": "technical_1",
            "questions": [
                {"question": "Explain a database index", "result": "stuck"},
                {"question": "Explain a database index", "result": "clear and correct"},
            ],
        }
        bank = update_bank({}, debrief, as_of="2026-09-01")
        self.assertEqual(bank["summary"]["question_count"], 1)
        self.assertEqual(bank["questions"][0]["times_asked"], 2)
        self.assertEqual(bank["questions"][0]["my_status"], "strong")

    def test_analytics_respects_sample_gate(self) -> None:
        jobs = [
            {"role_family": "backend", "sources": ["official"], "stage": "APPLIED"},
            {"role_family": "backend", "sources": ["official"], "stage": "INTERVIEW"},
        ]
        analytics = build_analytics(jobs, min_applied_sample=3)
        self.assertFalse(analytics["sample"]["conversion_analysis_enabled"])
        self.assertEqual(analytics["by_role_family"]["backend"]["interview"], 1)

    def test_execution_plan_keeps_dissertation_and_must_apply(self) -> None:
        targets = load_config(ROOT / "config/targets.example.yaml")
        execution = load_config(ROOT / "config/execution.example.yaml")
        jobs = [
            {
                "company": "Example",
                "title": "Engineer",
                "action": "MUST_APPLY",
                "verification_state": "VERIFIED_OPEN_JOB",
                "canonicality": "CANONICAL",
                "canonical_url": "https://jobs.example.test/verified-role",
                "status": "OPEN",
                "freshness": "fresh",
                "description": "Complete official job description. " * 8,
                "deadline": "2026-08-24",
                "application_cost": 5,
            }
        ]
        plan = build_execution_plan(
            targets,
            execution,
            jobs,
            {},
            {},
            plan_date="2026-08-23",
        )
        titles = [item["title"] for item in plan["reminders"]]
        self.assertTrue(any("Dissertation" in title for title in titles))
        self.assertTrue(any("MUST_APPLY" in title for title in titles))

    def test_low_completion_reduces_load(self) -> None:
        policy = workload_policy({"completion_rate": 0.4, "consecutive_low_days": 3})
        self.assertEqual(policy["load_factor"], 0.7)
        self.assertTrue(policy["weekly_replan_required"])


if __name__ == "__main__":
    unittest.main()
