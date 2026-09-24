from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.build_daily_application_pack import build_pack
from scripts.fetch_official import OfficialListing, _dedupe_apple_listings, _fetch_workday
from scripts.import_gpt_discovery import batch_freshness, validate_gpt_payload
from scripts.install_schedule import build_windows_task_xml
from scripts.run_daily_pipeline import build_commands, utf8_subprocess_environment
from scripts.run_windows_daily import (
    build_codex_command,
    build_daily_pipeline_command,
    compare_production_manifests,
    production_state_manifest,
    reusable_daily_batch,
    shard_query_plan,
)
from scripts.resume_v2 import page_fit_state
from scripts.semantic_review import (
    SemanticReviewProviderOutputError,
    run_semantic_review_batch,
    semantic_review_example,
)
from scripts.source_health import (
    HEALTH_STATUSES,
    automatic_discovery_succeeded,
    record_source_health,
    reset_source_health,
)


class DailyMainlineContractTests(unittest.TestCase):
    def _manual_agent_payload(self) -> dict:
        return {
            "schema_version": 3,
            "mode": "MANUAL_AGENT",
            "source": "gpt_web",
            "batch": {
                "batch_id": "shenzhen-2027-20260913",
                "plan_id": "public-web-2026-09-13-test",
                "generated_at": "2026-09-13T09:00:00+08:00",
                "search_date": "2026-09-13",
                "queries": ["深圳 2027 大模型 应届"],
                "source_urls": ["https://www.nowcoder.com/jobs/detail/123"],
                "searches": [{
                    "plan_id": "public-web-2026-09-13-test",
                    "query_id": "company_agnostic_core-test",
                    "lane": "company_agnostic_core",
                    "query": "深圳 2027 大模型 应届",
                    "provider": "codex_web_search",
                    "started_at": "2026-09-13T09:00:00+08:00",
                    "completed_at": "2026-09-13T09:01:00+08:00",
                    "status": "SUCCESS",
                    "source_urls": ["https://www.nowcoder.com/jobs/detail/123"],
                    "result_count": 1,
                    "error_category": "",
                    "retry_count": 0,
                    "notes": "Search executed.",
                }],
                "max_age_days": 1,
                "expires_at": "2026-09-14",
            },
            "candidates": [],
        }

    def test_manual_agent_batch_contract_and_expiry(self) -> None:
        payload = self._manual_agent_payload()
        self.assertEqual(validate_gpt_payload(payload), [])
        self.assertEqual(batch_freshness(payload, as_of="2026-09-14")[0], True)
        fresh, reason = batch_freshness(payload, as_of="2026-09-15")
        self.assertFalse(fresh)
        self.assertIn("expired", reason)

    def test_source_health_has_exact_status_contract_and_automatic_gate(self) -> None:
        self.assertEqual(
            HEALTH_STATUSES,
            {
                "SUCCESS", "PARTIAL", "PARTIAL_RATE_LIMITED", "EMPTY_VALID", "REFRESH_REQUIRED",
                "FAILED", "DISABLED", "MANUAL_BY_DESIGN",
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            health_path = Path(directory) / "health.json"
            payload = reset_source_health(health_path)
            self.assertFalse(automatic_discovery_succeeded(payload))
            record_source_health(
                "official_ats", "EMPTY_VALID", automatic=True, path=health_path
            )
            import json

            payload = json.loads(health_path.read_text(encoding="utf-8"))
            self.assertTrue(automatic_discovery_succeeded(payload))

    def test_daily_pack_is_useful_when_no_ready_job_exists(self) -> None:
        content = build_pack(
            run_at=datetime(2026, 9, 13, 19, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            health={
                "sources": {
                    "official_ats": {
                        "status": "SUCCESS", "count": 2, "attempted": 1,
                        "succeeded": 1, "failed": 0, "automatic": True,
                        "details": ["Workday"],
                    }
                }
            },
            jobs=[{
                "company": "Example", "title": "AI Engineer", "location": "深圳",
                "url": "https://jobs.example.test/job/1", "discovery_state": "NEW",
            }],
            decisioned=[{
                "company": "Example", "title": "Other", "action": "REJECT",
                "reject_reason": ["graduation cohort uncertain"],
            }, {
                "company": "Unsafe", "title": "Unverified Ready", "action": "READY",
                "matching_score": 100,
            }],
            manifest=[],
            registry={"sources": {}},
        )
        for heading in (
            "## Source health", "## New jobs discovered today",
            "## Existing jobs updated today", "## READY / MUST_APPLY",
            "## Human review required", "## Rejected",
        ):
            self.assertIn(heading, content)
        self.assertIn("No fake recommendation", content)
        self.assertNotIn("Unverified Ready", content)
        self.assertIn("Feishu mode: DRY_RUN", content)

    def test_windows_task_preview_uses_native_runtime_and_safe_settings(self) -> None:
        xml = build_windows_task_xml("config/targets.yaml", r"D:\Python\python.exe")
        self.assertIn(r"D:\Python\python.exe", xml)
        self.assertIn(r"D:\jobhunter\track_job\scripts\run_windows_daily.py", xml)
        self.assertIn("<WorkingDirectory>D:\\jobhunter\\track_job</WorkingDirectory>", xml)
        self.assertIn("<StartWhenAvailable>true</StartWhenAvailable>", xml)
        self.assertIn("<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>", xml)
        self.assertIn("--apply-feishu", xml)
        self.assertIn("--allow-degraded-review-sync", xml)

    def test_windows_codex_search_flag_precedes_exec_subcommand(self) -> None:
        command = build_codex_command(
            Path(r"C:\tools\codex.exe"),
            Path(r"D:\jobhunter\track_job"),
            Path(r"D:\jobhunter\track_job\config\manual_agent_batch.schema.json"),
            Path(r"C:\Temp\batch.json"),
        )
        self.assertLess(command.index("--search"), command.index("exec"))
        self.assertLess(command.index("--ask-for-approval"), command.index("exec"))
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-luna")
        self.assertEqual(
            command[command.index("--config") + 1],
            'model_reasoning_effort="low"',
        )
        self.assertNotIn("--apply-feishu", command)

    def test_windows_daily_retry_reuses_only_same_day_valid_batch(self) -> None:
        payload = self._manual_agent_payload()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            usable, detail = reusable_daily_batch(
                path,
                now=datetime(2026, 9, 13, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            )
            self.assertTrue(usable)
            self.assertIn(payload["batch"]["batch_id"], detail)
            usable_tomorrow, _ = reusable_daily_batch(
                path,
                now=datetime(2026, 9, 14, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            )
            self.assertFalse(usable_tomorrow)

    def test_windows_daily_shards_preserve_order_and_plan_identity(self) -> None:
        queries = [
            {"query_id": f"q-{index}", "query": f"query {index}", "lane": "test"}
            for index in range(23)
        ]
        plan = {
            "plan_id": "plan-1",
            "queries": queries,
            "p0_companies_planned": ["unneeded bulky prompt metadata"],
        }
        shards = shard_query_plan(plan, 10)
        self.assertEqual([len(item["queries"]) for item in shards], [10, 10, 3])
        self.assertEqual(
            [query["query_id"] for shard in shards for query in shard["queries"]],
            [query["query_id"] for query in queries],
        )
        self.assertTrue(all(item["plan_id"] == "plan-1" for item in shards))
        self.assertTrue(all("p0_companies_planned" not in item for item in shards))

    def test_windows_daily_resume_retries_failed_receipts(self) -> None:
        payload = self._manual_agent_payload()
        payload["batch"]["searches"].append(
            {
                **payload["batch"]["searches"][0],
                "query_id": "failed-query",
                "query": "failed query",
                "status": "TIMEOUT",
                "source_urls": [],
                "result_count": 0,
                "error_category": "TIMEOUT",
            }
        )
        payload["batch"]["queries"].append("failed query")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            usable, detail = reusable_daily_batch(
                path,
                now=datetime(2026, 9, 13, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                retry_failed=True,
            )
        self.assertFalse(usable)
        self.assertIn("failed searches to retry", detail)

    def test_windows_daily_skips_rate_limited_repo_discovery_not_pipeline(self) -> None:
        root = Path(r"D:\jobhunter-acceptance\repair-pass1-real")
        command = build_daily_pipeline_command(
            Path(r"D:\jobhunter\track_job"),
            Path(r"D:\jobhunter\track_job\config\targets.yaml"),
            acceptance_root=root,
            manual_agent_input=root / "manual_agent_batch.json",
            public_web_query_plan=root / "public_web_query_plan.json",
        )
        self.assertIn("run_daily_pipeline.py", command[1])
        self.assertIn("--skip-repo-sync", command)
        self.assertNotIn("--skip-fetch", command)
        self.assertNotIn("--skip-documents", command)
        self.assertNotIn("--apply-feishu", command)
        self.assertEqual(
            command[command.index("--public-web-query-plan") + 1],
            str(root / "public_web_query_plan.json"),
        )

    def test_acceptance_production_hash_manifest_detects_any_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "data" / "job_cache"
            generated = root / "cv" / "generated"
            cache.mkdir(parents=True)
            generated.mkdir(parents=True)
            (cache / "jobs.json").write_text("[]", encoding="utf-8")
            before = production_state_manifest(root)
            self.assertTrue(compare_production_manifests(before, before)["unchanged"])
            (generated / "manifest.json").write_text("[]", encoding="utf-8")
            after = production_state_manifest(root)
            comparison = compare_production_manifests(before, after)
            self.assertFalse(comparison["unchanged"])
            self.assertEqual(comparison["changed_paths"], ["cv/generated/manifest.json"])

    def test_acceptance_production_hash_manifest_streams_large_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "data" / "job_cache"
            cache.mkdir(parents=True)
            payload = b"0123456789abcdef" * 100_000
            (cache / "large.bin").write_bytes(payload)
            manifest = production_state_manifest(root)
            self.assertEqual(
                manifest["data/job_cache/large.bin"],
                hashlib.sha256(payload).hexdigest(),
            )

    def test_acceptance_production_hash_manifest_excludes_scheduler_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "data" / "job_cache"
            cache.mkdir(parents=True)
            (cache / "windows_daily.lock").write_bytes(b"transient lock")
            (cache / "jobs.json").write_text("[]", encoding="utf-8")
            manifest = production_state_manifest(root)
            self.assertNotIn("data/job_cache/windows_daily.lock", manifest)
            self.assertIn("data/job_cache/jobs.json", manifest)

    def test_daily_children_force_utf8_on_windows_console(self) -> None:
        environment = utf8_subprocess_environment()
        self.assertEqual(environment["PYTHONUTF8"], "1")
        self.assertEqual(environment["PYTHONIOENCODING"], "utf-8")
        for key, value in os.environ.items():
            self.assertEqual(environment[key], value)

    def test_manual_agent_output_schema_types_consts_and_enums_explicitly(self) -> None:
        schema = json.loads(
            Path("config/manual_agent_batch.schema.json").read_text(encoding="utf-8")
        )

        def visit(value):
            if isinstance(value, dict):
                if "const" in value or "enum" in value:
                    self.assertIn("type", value)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(schema)
        serialized = json.dumps(schema, sort_keys=True)
        for unsupported in ("uniqueItems", "minLength", "maxLength", "const", "$schema"):
            self.assertNotIn(f'"{unsupported}"', serialized)

    def test_daily_command_order_builds_pack_before_feishu_dry_run(self) -> None:
        args = argparse.Namespace(
            config="config/targets.yaml", feishu_config="config/feishu.yaml",
            execution_config="config/execution.yaml", source_registry="config/company_registry.yaml",
            taxonomy_config="config/taxonomies.yaml", mode="daily", batch_index=0,
            skip_fetch=False, skip_repo_sync=False, skip_documents=False,
            apply_feishu=False, semantic_review_dry_run=True,
            semantic_review_job_key=[], as_of="2026-09-13",
        )
        targets = {
            "schedule": {"max_jobs_per_run": 30},
            "job_search": {"sources": ["official", "linkedin", "gpt_web"], "shortlist_min_score": 70},
            "gpt_discovery": {"enabled": True, "mode": "MANUAL_AGENT"},
            "semantic_review": {"enabled": False},
        }
        registry = {
            "sources": {
                "official": {"enabled": True, "implementation_status": "automated_if_registered"},
                "linkedin": {"enabled": True, "implementation_status": "automated"},
                "gpt_web": {"enabled": True, "implementation_status": "manual_agent"},
            }
        }
        commands = build_commands(args, targets, r"D:\Python\python.exe", registry)
        scripts = [command[1] for command in commands]
        pack_index = scripts.index("scripts/build_daily_application_pack.py")
        feishu_index = scripts.index("scripts/update_feishu.py")
        self.assertLess(pack_index, feishu_index)
        self.assertEqual(commands[feishu_index][-1], "--dry-run")
        official = commands[scripts.index("scripts/fetch_official.py")]
        self.assertEqual(official[official.index("--max-links-per-company") + 1], "500")

    def test_one_invalid_semantic_provider_output_does_not_abort_daily_batch(self) -> None:
        class InvalidProvider:
            provider_name = "deepseek"
            model = "deepseek-v4-flash"
            contract_version = "test-contract"

            def review(self, *_args, **_kwargs):
                raise SemanticReviewProviderOutputError(
                    "preferred_skills[0] has unexpected fields",
                    {"http_status": 200, "usage": {"total_tokens": 100}},
                )

        job = {
            "canonical_key": "fallback:example|ai-engineer|shenzhen|2027",
            "company": "Example", "title": "2027 AI Engineer", "location": "深圳",
            "description": "面向2027届，在深圳开发大模型应用。", "action": "READY",
        }
        with tempfile.TemporaryDirectory() as directory:
            summary = run_semantic_review_batch(
                [job], [],
                {
                    "enabled": True, "provider": "deepseek", "trigger": ["READY"],
                    "cache_dir": directory, "max_reviews_per_run": 1,
                },
                provider=InvalidProvider(),
            )
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["reviews"][0]["cache_written"], False)
        self.assertTrue(job["requires_human_review"])
        self.assertEqual(job["semantic_review_status"], "FAILED")

    def test_workday_monitor_requests_china_and_preserves_additional_locations(self) -> None:
        class Response:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        class Session:
            def post(self, _url, *, json, timeout):
                self.search = json["searchText"]
                return Response({
                    "jobPostings": [{
                        "title": "NVIDIA 2027 New College Graduate: Software Engineering - China",
                        "locationsText": "3 Locations",
                        "externalPath": "/job/China-Shanghai/role_JR2024111",
                    }]
                })

            def get(self, _url, *, timeout):
                return Response({
                    "jobPostingInfo": {
                        "location": "China, Shanghai",
                        "additionalLocations": ["China, Beijing", "China, Shenzhen"],
                        "jobDescription": "2027 graduate software engineering role",
                    }
                })

        session = Session()
        jobs = _fetch_workday(
            session,
            {
                "name": "NVIDIA",
                "official_career_url": "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite",
            },
            [],
            4,
        )
        self.assertEqual(session.search, "2027 China")
        self.assertEqual(len(jobs), 1)
        self.assertIn("China, Shenzhen", jobs[0].location)

    def test_successful_semantic_review_propagates_human_review_status(self) -> None:
        class ValidProvider:
            provider_name = "deepseek"
            model = "deepseek-v4-flash"
            contract_version = "test-contract"

            def review(self, *_args, **_kwargs):
                return semantic_review_example("1.0"), {"total_tokens": 10}

        job = {
            "canonical_key": "fallback:example|ai-engineer|shenzhen|2027",
            "company": "Example", "title": "2027 AI Engineer", "location": "深圳",
            "description": "面向2027届，在深圳开发大模型应用。", "action": "READY",
        }
        with tempfile.TemporaryDirectory() as directory:
            summary = run_semantic_review_batch(
                [job], [],
                {
                    "enabled": True, "provider": "deepseek", "trigger": ["READY"],
                    "cache_dir": directory, "max_reviews_per_run": 1,
                },
                provider=ValidProvider(),
            )
        self.assertEqual(summary["reviewed"], 1)
        self.assertEqual(job["semantic_review_status"], "SUCCESS")
        self.assertTrue(job["requires_human_review"])

    def test_semantic_closed_result_always_requires_human_review(self) -> None:
        class ClosedProvider:
            provider_name = "deepseek"
            model = "deepseek-v4-flash"
            contract_version = "test-contract"

            def review(self, *_args, **_kwargs):
                result = semantic_review_example("1.0")
                result["graduation"] = {
                    "decision": "ELIGIBLE", "confidence": "HIGH",
                    "evidence": ["面向2027届"],
                }
                result["location"] = {
                    "decision": "SHENZHEN_ALLOWED", "confidence": "HIGH",
                    "evidence": ["工作地点深圳"],
                }
                result["experience_requirement"] = {
                    "decision": "NEW_GRAD_COMPATIBLE", "confidence": "HIGH",
                    "evidence": ["校园招聘"],
                }
                result["job_status"] = {
                    "decision": "CLOSED", "confidence": "HIGH",
                    "evidence": ["页面明确显示职位已关闭"],
                }
                return result, {"total_tokens": 10}

        job = {
            "canonical_key": "fallback:example|ai-engineer|shenzhen|2027",
            "company": "Example", "title": "2027 AI Engineer", "location": "深圳",
            "description": "面向2027届，在深圳开发大模型应用。", "action": "READY",
        }
        with tempfile.TemporaryDirectory() as directory:
            summary = run_semantic_review_batch(
                [job], [],
                {"enabled": True, "provider": "deepseek", "trigger": ["READY"],
                 "cache_dir": directory, "max_reviews_per_run": 1},
                provider=ClosedProvider(),
            )
        self.assertEqual(summary["reviewed"], 1)
        self.assertTrue(job["requires_human_review"])

    def test_apple_locale_variants_share_one_requisition(self) -> None:
        listings = [
            OfficialListing(
                company="Apple", position="AI Tools Intern",
                url="https://jobs.apple.com/en-us/details/200676912-3715/ai-tools",
            ),
            OfficialListing(
                company="Apple", position="AI Tools Intern",
                url="https://jobs.apple.com/en-us/details/200676912-3435/ai-tools",
            ),
        ]
        self.assertEqual(len(_dedupe_apple_listings(listings)), 1)

    def test_density_floor_uses_minimums_not_preferred_maximums(self) -> None:
        state = page_fit_state(
            {
                "page_count": 1,
                "text_vertical_span_ratio": 0.885,
                "non_empty_line_count": 63,
                "english_word_count": 394,
                "cjk_character_count": 0,
                "text_out_of_bounds": False,
                "text_overlap_detected": False,
            },
            {
                "min_vertical_span_ratio": 0.89,
                "max_vertical_span_ratio": 0.95,
                "min_lines": 50,
                "max_lines": 56,
                "min_words": 380,
                "max_words": 430,
            },
        )
        self.assertEqual(state, "READY_WITH_DENSITY_WARNING")


if __name__ == "__main__":
    unittest.main()
