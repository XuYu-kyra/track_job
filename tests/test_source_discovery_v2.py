from __future__ import annotations

import json
import copy
import argparse
import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import requests

from scripts.ats_detection import detect_ats_evidence
from scripts.adaptive_discovery import AdaptiveSearch, AdaptiveState
from scripts.known_jobs import known_job_recall, load_known_jobs
from scripts.audit_company_sources import audit_company, audit_registry
from scripts.build_discovery_coverage import build_coverage_report
from scripts.common import load_config
from scripts.discovered_companies import build_discovered_company_store
from scripts.fetch_linkedin import JobListing, fetch_jobs
from scripts.generate_resume import eligible_for_document_generation
from scripts.import_gpt_discovery import validate_gpt_payload
from scripts import run_daily_pipeline
from scripts.job_schema import is_verified_open_job, merge_job_records, normalize_job
from scripts.official_adapters import (
    BeisenAdapter,
    FeishuRecruitingAdapter,
    HotjobAdapter,
    JSONLDAdapter,
    SitemapAdapter,
    StableHTMLAdapter,
    WorkdayAdapter,
)
from scripts.public_web_query_plan import (
    CORE_ROLE_FAMILIES,
    PUBLIC_SOURCE_LANES,
    REQUIRED_LANES,
    build_public_web_query_plan,
    validate_query_receipts,
)
from scripts.receive_manual_agent_batch import merge_batches, normalize_receipt_derived_fields
from scripts.query_matrix import deterministic_daily_batch_index
from scripts.run_daily_pipeline import build_commands
from scripts.run_windows_daily import (
    attach_recall_control_urls,
    build_discovery_prompt,
    load_discovered_company_candidates,
    selected_query_plan,
)
from scripts.score_jobs import evaluate_job
from scripts.source_adapters import load_source_registry
from scripts.update_feishu import require_canary_coverage, require_healthy_coverage, require_safe_delivery_jobs


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "source_discovery_v2"


class Response:
    def __init__(self, payload=None, *, text="", status=200, url="https://example.test"):
        self._payload = payload
        self.text = text
        self.status_code = status
        self.url = url
        self.ok = status < 400

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            raise requests.HTTPError(f"{self.status_code} error", response=response)


class WorkdaySession:
    def __init__(self, fixture):
        self.fixture = fixture
        self.offsets = []

    def post(self, _url, *, json, timeout):
        self.offsets.append(json["offset"])
        index = 0 if json["offset"] == 0 else 1
        return Response(self.fixture["pages"][index])

    def get(self, url, *, timeout):
        job_id = url.rsplit("_", 1)[-1]
        return Response({"jobPostingInfo": self.fixture["details"][job_id]})


class SourceDiscoveryV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.targets = load_config(ROOT / "config" / "targets.example.yaml")
        cls.taxonomies = load_config(ROOT / "config" / "taxonomies.yaml")
        cls.scoring = load_config(ROOT / "config" / "scoring.yaml")
        cls.registry = load_source_registry(ROOT / "config" / "company_registry.yaml")

    def fixture(self, name: str):
        path = FIXTURES / name
        if path.suffix == ".json":
            return json.loads(path.read_text(encoding="utf-8"))
        return path.read_text(encoding="utf-8")

    def test_ats_detection_uses_html_and_public_endpoint_markers(self) -> None:
        detection = detect_ats_evidence(
            "https://careers.example.test/campus",
            html_text='<script src="/assets/app.js"></script>',
            endpoint_urls=["https://careers.example.test/api/v1/search/job/posts"],
        )
        self.assertEqual(detection.family, "Feishu Recruiting")
        self.assertTrue(detection.evidence)

    def test_adaptive_search_requires_terminal_state_and_enforces_budget(self) -> None:
        search = AdaptiveSearch()
        search.transition(AdaptiveState.COMPANY_HOME_FOUND.value, reason="home")
        search.record_search()
        search.record_pages(["https://example.test/careers"], page_type="CAREER")
        search.transition(AdaptiveState.CAREER_ENTRY_FOUND.value, reason="careers")
        search.record_search()
        search.record_pages(["https://example.test/campus"], page_type="CAMPUS")
        search.transition(AdaptiveState.CAMPUS_CAMPAIGN_FOUND.value, reason="campus")
        search.transition(AdaptiveState.NO_RESULT_CONFIRMED.value, reason="no role")
        self.assertTrue(search.terminal())
        self.assertEqual(search.receipt_fields()["terminal_state"], "NO_RESULT_CONFIRMED")
        with self.assertRaises(ValueError):
            search.transition(AdaptiveState.ROLE_FOUND.value)

    def test_known_job_controls_match_by_id_or_url(self) -> None:
        known = load_known_jobs(ROOT / "data" / "discovery" / "known_jobs.jsonl")
        result = known_job_recall(
            known[:2],
            [{"job_id": known[0]["job_id"]}, {"url": known[1]["url"]}],
        )
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["matched"], 2)
        self.assertEqual(result["recall_percent"], 100.0)

    def test_known_job_recall_explains_query_scope_and_aliases(self) -> None:
        known = [
            {
                "sample_id": "alias-control",
                "company": "安克创新科技股份有限公司",
                "company_aliases": ["Anker Innovations"],
                "title": "Test Engineer",
                "location": "深圳",
                "url": "https://www.nowcoder.com/jobs/detail/1",
            },
            {
                "sample_id": "not-planned",
                "company": "Apple",
                "title": "Intern",
                "location": "深圳",
                "url": "https://jobs.apple.com/jobs/1",
            },
        ]
        result = known_job_recall(
            known,
            [],
            query_plan={
                "queries": [{
                    "query_id": "anker-query",
                    "companies": ["Anker Innovations"],
                }]
            },
            query_receipts=[{
                "query_id": "anker-query",
                "status": "EMPTY_VALID",
                "source_urls": [],
            }],
        )
        rows = {row["sample_id"]: row for row in result["rows"]}
        self.assertEqual(rows["alias-control"]["cause"], "QUERY_RETURNED_NO_URL")
        self.assertEqual(rows["not-planned"]["cause"], "QUERY_NOT_PLANNED_TODAY")
        self.assertEqual(result["active_controls"], 2)

    def test_urgent_company_refresh_overrides_p1_calendar_in_separate_lane(self) -> None:
        plan = build_public_web_query_plan(
            self.targets,
            self.taxonomies,
            self.registry,
            run_date=date(2026, 9, 22),  # Tuesday: normal P1 is skipped.
            urgent_companies=["Apple"],
        )
        urgent = [
            item for item in plan["queries"]
            if item.get("lane") == "urgent_known_company_refresh"
        ]
        self.assertEqual(len(urgent), 1)
        self.assertEqual(urgent[0]["companies"], ["Apple"])
        self.assertEqual(urgent[0]["priority"], "URGENT")
        self.assertEqual(plan["p1_scheduled_today"], False)
        self.assertEqual(plan["urgent_companies_planned"], ["Apple"])

    def test_urgent_refresh_attaches_known_control_urls(self) -> None:
        plan = build_public_web_query_plan(
            self.targets,
            self.taxonomies,
            self.registry,
            run_date=date(2026, 9, 22),
            urgent_companies=["Apple", "Anker Innovations"],
        )
        attach_recall_control_urls(plan, ROOT)
        urgent = {
            item["companies"][0]: item
            for item in plan["queries"]
            if item.get("lane") == "urgent_known_company_refresh"
        }
        self.assertTrue(any("jobs.apple.com" in url for url in urgent["Apple"]["recall_control_urls"]))
        self.assertTrue(any("nowcoder.com" in url for url in urgent["Anker Innovations"]["recall_control_urls"]))

    def test_canary_coverage_accepts_degraded_market_health_after_recall_gate(self) -> None:
        require_canary_coverage({
            "status": "SOURCE_COVERAGE_DEGRADED",
            "query_execution": {"planned": 79, "terminal_receipts": 79},
            "known_job_recall": {"recall_percent": 91.67},
            "unsafe_ready_jobs": [],
        })
        with self.assertRaisesRegex(RuntimeError, "at least 80"):
            require_canary_coverage({
                "query_execution": {"planned": 1, "terminal_receipts": 1},
                "known_job_recall": {"recall_percent": 75},
                "unsafe_ready_jobs": [],
            })

    def test_workday_complete_pagination_dedup_and_full_jd_hydration(self) -> None:
        fixture = self.fixture("workday.json")
        session = WorkdaySession(fixture)
        adapter = WorkdayAdapter(
            session,
            {
                "name": "Example",
                "official_career_url": "https://example.wd5.myworkdayjobs.com/en-US/Example",
            },
        )
        adapter.page_size = 2
        run = adapter.collect(safety_cap=20)
        self.assertEqual(session.offsets, [0, 2])
        self.assertEqual(run.status, "SUCCESS")
        self.assertTrue(run.pagination_complete)
        self.assertEqual(run.pages_attempted, 2)
        self.assertEqual(len(run.records), 3)
        self.assertTrue(all(len(item["description"]) >= 120 for item in run.records))
        self.assertEqual(len({item["job_id"] for item in run.records}), 3)

    def test_official_safety_cap_returns_resumable_page_cursor(self) -> None:
        fixture = self.fixture("workday.json")
        session = WorkdaySession(fixture)
        adapter = WorkdayAdapter(
            session,
            {
                "name": "Example",
                "official_career_url": "https://example.wd5.myworkdayjobs.com/en-US/Example",
            },
        )
        adapter.page_size = 2
        first = adapter.collect(safety_cap=2)
        self.assertTrue(first.truncated)
        self.assertEqual(first.continuation_cursor, 2)
        second = adapter.collect(safety_cap=2, start_cursor=first.continuation_cursor)
        self.assertTrue(second.pagination_complete)
        self.assertFalse(second.truncated)

    def test_beisen_paginates_until_total(self) -> None:
        fixture = self.fixture("beisen.json")

        class Session:
            def __init__(self):
                self.pages = []

            def post(inner, _url, *, json, timeout):
                inner.pages.append(json["pageIndex"])
                return Response(fixture["pages"][json["pageIndex"] - 1])

        session = Session()
        adapter = BeisenAdapter(
            session,
            {"name": "Example", "official_career_url": "https://example.zhiye.com/campus/jobs"},
        )
        adapter.page_size = 2
        run = adapter.collect(safety_cap=20)
        self.assertEqual(session.pages, [1, 2])
        self.assertTrue(run.pagination_complete)
        self.assertEqual(len(run.records), 3)

    def test_hotjob_and_feishu_use_common_adapter_contract(self) -> None:
        hotjob = self.fixture("hotjob.json")

        class HotjobSession:
            def post(inner, url, **kwargs):
                if "getSLD" in url:
                    return Response({"data": {"linkData": {"link": f"/x/{hotjob['suite']}/index"}}})
                if "listPositionDetail" in url:
                    return Response(hotjob["detail"])
                return Response(hotjob["page"])

        hotjob_run = HotjobAdapter(
            HotjobSession(),
            {"name": "Example", "official_career_url": "https://example.hotjob.cn/"},
        ).collect(safety_cap=20)
        self.assertTrue(hotjob_run.pagination_complete)
        self.assertEqual(len(hotjob_run.records), 1)
        self.assertTrue(hotjob_run.records[0]["description"])

        feishu = self.fixture("feishu.json")

        class FeishuSession:
            def post(inner, _url, **kwargs):
                return Response(feishu["page"])

        feishu_run = FeishuRecruitingAdapter(
            FeishuSession(),
            {"name": "Example", "official_career_url": "https://jobs.example.test/campus"},
        ).collect(safety_cap=20)
        self.assertEqual(feishu_run.status, "SUCCESS")
        self.assertEqual(feishu_run.records[0]["job_id"], "F1")

    def test_jsonld_adapter_extracts_stable_job_and_full_jd(self) -> None:
        body = self.fixture("jsonld.html")

        class Session:
            def get(inner, _url, *, timeout):
                return Response(body, text=body)

        run = JSONLDAdapter(
            Session(),
            {"name": "Example", "official_career_url": "https://example.test/careers"},
        ).collect(safety_cap=20)
        self.assertEqual(run.status, "SUCCESS")
        self.assertEqual(run.records[0]["job_id"], "J-42")
        self.assertIn("Shenzhen", run.records[0]["location"])

    def test_sitemap_and_stable_html_adapters_are_bounded(self) -> None:
        sitemap = self.fixture("sitemap.xml")
        detail = self.fixture("jsonld.html")

        class SitemapSession:
            def get(inner, url, *, timeout):
                return Response(text=sitemap if url.endswith("sitemap.xml") else detail, url=url)

        run = SitemapAdapter(
            SitemapSession(),
            {
                "name": "Example",
                "official_career_url": "https://example.test/sitemap.xml",
                "sitemap_url": "https://example.test/sitemap.xml",
            },
        ).collect(safety_cap=20)
        self.assertTrue(run.pagination_complete)
        self.assertEqual(len(run.records), 1)
        self.assertEqual(run.records[0]["job_id"], "J-42")

        first = self.fixture("stable_html.html")
        second = '<article><a href="/jobs/103">2027 Backend Engineer</a></article>'

        class HTMLSession:
            def get(inner, url, *, timeout):
                if url.endswith("/jobs/101") or url.endswith("/jobs/102") or url.endswith("/jobs/103"):
                    return Response(text=detail, url=url)
                return Response(text=second if "page=2" in url else first, url=url)

        html_run = StableHTMLAdapter(
            HTMLSession(),
            {"name": "Example", "official_career_url": "https://example.test/careers"},
        ).collect(safety_cap=20)
        self.assertEqual(html_run.pages_attempted, 2)
        self.assertTrue(html_run.pagination_complete)
        self.assertEqual(len(html_run.records), 3)

    def test_two_axis_plan_has_daily_p0_and_three_day_p1_coverage(self) -> None:
        expected_p0 = {
            item["name"] for item in self.registry["companies"]
            if item.get("monitor_priority") == "P0"
        }
        expected_p1 = {
            item["name"] for item in self.registry["companies"]
            if item.get("monitor_priority") == "P1"
        }
        seen_p1 = set()
        for run_date in (date(2026, 9, 21), date(2026, 9, 23), date(2026, 9, 25)):
            plan = build_public_web_query_plan(
                self.targets,
                self.taxonomies,
                self.registry,
                run_date=run_date,
            )
            self.assertEqual(set(plan["required_lanes"]), set(REQUIRED_LANES))
            self.assertFalse(plan["registry_is_allowlist"])
            self.assertTrue(plan["unregistered_companies_allowed"])
            self.assertEqual(set(plan["p0_companies_planned"]), expected_p0)
            self.assertTrue(set(CORE_ROLE_FAMILIES).issubset(
                {item["role_family"] for item in plan["queries"]}
            ))
            core_specs = [
                item for item in plan["queries"]
                if item["lane"] == "company_agnostic_core"
            ]
            self.assertEqual(
                {item["role_family"] for item in core_specs},
                set(CORE_ROLE_FAMILIES),
            )
            self.assertTrue(all(item["company_agnostic"] for item in core_specs))
            self.assertTrue(all(not item["companies"] for item in core_specs))
            self.assertEqual(
                {
                    item["lane"] for item in plan["queries"]
                    if item["lane"] in PUBLIC_SOURCE_LANES
                },
                set(PUBLIC_SOURCE_LANES),
            )
            self.assertEqual(
                {
                    item["source_domain"]
                    for item in plan["queries"]
                    if item["lane"] == "known_ats_domain"
                },
                set(self.targets["open_market_discovery"]["ats_domains"]),
            )
            self.assertEqual(
                {
                    item["campaign_term"]
                    for item in plan["queries"]
                    if item["lane"] == "campus_campaign"
                },
                set(self.targets["open_market_discovery"]["campaign_terms"]),
            )
            for spec in plan["queries"]:
                self.assertIn(
                    spec["role_alias"],
                    self.taxonomies["role_families"][spec["role_family"]]["aliases"],
                )
            known_company_specs = [
                item
                for item in plan["queries"]
                if item["lane"] in {"p0_company_daily", "p1_company_rolling_3d"}
            ]
            self.assertTrue(known_company_specs)
            for spec in known_company_specs:
                query = spec["query"]
                for required_term in (
                    "深圳",
                    "Shenzhen",
                    '"new grad"',
                    '"early career"',
                    "intern",
                    "careers",
                    "jobs",
                ):
                    self.assertIn(required_term, query)
                for role_family in CORE_ROLE_FAMILIES:
                    aliases = self.taxonomies["role_families"][role_family]["aliases"]
                    self.assertTrue(
                        any(alias in query for alias in aliases),
                        f"{role_family} missing from known-company query: {query}",
                    )
            if run_date == date(2026, 9, 21):
                dji = next(item for item in known_company_specs if "DJI" in item["companies"])
                anker = next(
                    item for item in known_company_specs if "Anker Innovations" in item["companies"]
                )
                self.assertIn('"大疆"', dji["query"])
                self.assertIn('"安克创新"', anker["query"])
            seen_p1.update(plan["p1_companies_planned"])
        self.assertEqual(seen_p1, expected_p1)
        skipped = build_public_web_query_plan(
            self.targets,
            self.taxonomies,
            self.registry,
            run_date=date(2026, 9, 22),
        )
        self.assertTrue(skipped["skipped_by_schedule"])
        self.assertEqual(skipped["p1_companies_planned"], [])
        self.assertEqual(skipped["next_scheduled_run"], "2026-09-23")

    def test_open_market_plan_covers_remote_and_campaign_vocabulary(self) -> None:
        plan = build_public_web_query_plan(
            self.targets,
            self.taxonomies,
            self.registry,
            run_date=date(2026, 9, 18),
        )
        queries = [str(item["query"]) for item in plan["queries"]]
        self.assertTrue(any("Remote China" in query for query in queries))
        for term in ("应届", "实习转正", "提前批", "秋招", "补录"):
            self.assertTrue(any(term in query for query in queries), term)

    def test_acceptance_commands_are_isolated_and_stop_before_downstream_writes(self) -> None:
        root = "/tmp/track-job-acceptance-contract"
        args = argparse.Namespace(
            config="config/targets.yaml",
            taxonomy_config="config/taxonomies.yaml",
            source_registry="config/company_registry.yaml",
            mode="daily",
            batch_index=0,
            as_of="2026-09-18",
            skip_repo_sync=False,
            skip_fetch=False,
            skip_documents=False,
            apply_feishu=False,
            semantic_review_dry_run=False,
            semantic_review_job_key=[],
            acceptance_output_root=root,
            manual_agent_input=f"{root}/manual_agent_batch.json",
            public_web_query_plan=f"{root}/public_web_query_plan.json",
            execution_config="config/execution.yaml",
            feishu_config="config/feishu.yaml",
        )
        commands = build_commands(
            args, self.targets, "python3", self.registry
        )
        scripts = [command[1] for command in commands]
        self.assertNotIn("scripts/build_public_web_query_plan.py", scripts)
        self.assertNotIn("scripts/generate_resume.py", scripts)
        self.assertNotIn("scripts/update_feishu.py", scripts)
        self.assertNotIn("scripts/build_execution_plan.py", scripts)
        importer = commands[scripts.index("scripts/import_gpt_discovery.py")]
        coverage = commands[scripts.index("scripts/build_discovery_coverage.py")]
        self.assertEqual(
            importer[importer.index("--query-plan") + 1],
            f"{root}/public_web_query_plan.json",
        )
        self.assertEqual(
            coverage[coverage.index("--query-plan") + 1],
            f"{root}/public_web_query_plan.json",
        )
        writable_flags = {
            "--output", "--history", "--all-output", "--json-output",
            "--markdown-output", "--health",
        }
        for command in commands:
            for index, token in enumerate(command[:-1]):
                if token in writable_flags:
                    self.assertTrue(command[index + 1].startswith(root), command)

    def test_dynamic_company_monitoring_stays_bounded_and_complete(self) -> None:
        store = {
            "companies": [
                {
                    "normalized_name": f"Emerging Company {index:03d}",
                    "status": "PERSISTENT_MONITORING",
                }
                for index in range(200)
            ]
        }
        plan = build_public_web_query_plan(
            self.targets,
            self.taxonomies,
            self.registry,
            run_date=date(2026, 9, 17),
            discovered_companies=store,
        )
        self.assertLessEqual(len(plan["queries"]), 128)
        self.assertGreaterEqual(plan["dynamic_company_cycle_days"], 4)
        self.assertEqual(
            {
                company
                for companies in plan["dynamic_company_cycle_schedule"].values()
                for company in companies
            },
            {f"Emerging Company {index:03d}" for index in range(200)},
        )

    def test_unregistered_company_is_promoted_and_planned_for_monitoring(self) -> None:
        as_of = "2026-09-17"
        jobs = [
            {
                "canonical_key": f"newco|role-{index}|shenzhen",
                "company": "NewCo Robotics",
                "source": "official",
                "sources": ["official"],
                "company_aliases": ["NewCo", "新科机器人"],
                "title": f"2027 Robot Software Engineer {index}",
                "location": "深圳",
                "role_family": "robot_software",
                "action": "READY" if index == 1 else "WATCH",
                "verification_state": "VERIFIED_OPEN_JOB",
                "canonicality": "CANONICAL",
                "canonical_url": f"https://newco.example/careers/jobs/{index}",
                "ats_family": "Workday",
                "source_confidence": "HIGH",
                "graduation_evidence": ["2027届"],
                "first_seen": as_of,
                "last_observed": as_of,
            }
            for index in (1, 2)
        ]
        store = build_discovered_company_store(
            jobs,
            self.registry,
            self.targets,
            existing={},
            as_of=as_of,
        )
        self.assertFalse(store["registry_is_allowlist"])
        self.assertTrue(store["unregistered_companies_allowed"])
        self.assertEqual(len(store["companies"]), 1)
        candidate = store["companies"][0]
        self.assertEqual(candidate["status"], "PERSISTENT_MONITORING")
        self.assertEqual(candidate["target_role_job_count"], 2)
        self.assertEqual(set(candidate["aliases"]), {"NewCo Robotics", "NewCo", "新科机器人"})
        self.assertTrue(candidate["shenzhen_evidence"])
        self.assertTrue(candidate["graduation_2027_evidence"])
        self.assertTrue(candidate["verified_official_career_url"])
        self.assertEqual(candidate["observation_count"], 1)
        self.assertEqual(candidate["discovered_job_count"], 2)
        self.assertEqual(candidate["discovered_jobs"][0]["source"], "official")
        self.assertTrue(any(
            event["status"] == "PROMOTED_TO_PERSISTENT_MONITORING"
            for event in store["promotions"]
        ))

        dynamically_planned = set()
        for day in range(17, 20):
            plan = build_public_web_query_plan(
                self.targets,
                self.taxonomies,
                self.registry,
                run_date=date(2026, 9, day),
                discovered_companies=store,
            )
            self.assertLessEqual(len(plan["queries"]), 128)
            dynamically_planned.update(
                company
                for item in plan["queries"]
                if item["lane"] == "discovered_company_monitoring"
                for company in item["companies"]
            )
        self.assertEqual(dynamically_planned, {"NewCo Robotics"})

        promoted_registry = copy.deepcopy(self.registry)
        promoted_registry["companies"].append(
            {"name": "NewCo Robotics", "monitor_priority": "P1"}
        )
        after_registry_promotion = build_discovered_company_store(
            [],
            promoted_registry,
            self.targets,
            existing=store,
            as_of="2026-09-18",
        )
        self.assertEqual(after_registry_promotion["companies"], [])
        self.assertTrue(any(
            event["status"] == "PROMOTED_TO_REGISTRY"
            for event in after_registry_promotion["promotions"]
        ))

    def test_linkedin_batch_index_rotates_from_shanghai_date(self) -> None:
        first = deterministic_daily_batch_index("2026-09-17")
        second = deterministic_daily_batch_index("2026-09-18")
        self.assertEqual(second, first + 1)

    def test_offline_audit_gives_every_p0_an_explicit_state(self) -> None:
        report = audit_registry(self.registry, offline=True)
        self.assertEqual(report["total_companies"], 69)
        self.assertEqual(report["p0_total"], 39)
        self.assertEqual(report["p0_explicit"], 39)

    def test_audit_probes_public_api_when_landing_page_is_blocked(self) -> None:
        class Session:
            def get(inner, _url, **kwargs):
                return Response(status=403)

            def post(inner, _url, *, json, timeout):
                return Response(
                    {
                        "Code": 200,
                        "Data": [{"Id": "B-1", "JobAdName": "Backend Engineer"}],
                        "Total": 1,
                    }
                )

        result = audit_company(
            {
                "name": "Example",
                "monitor_priority": "P0",
                "official_career_url": "https://example.zhiye.com/campus/jobs",
                "ats_family": "Beisen",
                "monitor_mode": "PUBLIC_SEARCH",
            },
            Session(),
        )
        self.assertEqual(result.classification, "AUTO_API")
        self.assertEqual(result.recommended_adapter, "Beisen")
        self.assertEqual(result.last_probe_result, "HTTP_403_API_OK")

    def test_secondary_unknown_job_never_reaches_ready(self) -> None:
        job = normalize_job(
            {
                "source": "gpt_web",
                "company": "Example",
                "title": "2027 Backend Engineer",
                "location": "深圳",
                "url": "https://public.example.test/jobs/42",
                "status": "OPEN",
                "description": "2027届 " + "Python API Linux automated testing backend engineering " * 5,
            },
            self.taxonomies,
            as_of="2026-09-17",
        )
        result = evaluate_job(
            job,
            self.scoring,
            self.targets,
            self.taxonomies,
            {"skills": []},
            as_of="2026-09-17",
        )
        self.assertEqual(job["verification_state"], "DISCOVERED_CANDIDATE")
        self.assertEqual(result["action"], "WATCH")
        self.assertTrue(result["requires_human_review"])

    def test_missing_or_inconsistent_verification_fails_closed(self) -> None:
        description = "2027届 Python Linux backend testing engineering " * 8
        verified = normalize_job(
            {
                "source": "official",
                "company": "Example",
                "title": "2027 Backend Engineer",
                "location": "深圳",
                "url": "https://jobs.example.test/roles/12345",
                "canonical_authority": "OFFICIAL_MONITOR",
                "source_authoritative": True,
                "source_canonicality": "CANONICAL",
                "verification_level": "CANONICAL",
                "observation_origin": "LIVE_FETCH",
                "observed_at": "2026-09-18",
                "verified_at": "2026-09-18",
                "status": "OPEN",
                "description": description,
            },
            self.taxonomies,
            as_of="2026-09-18",
        )
        self.assertTrue(is_verified_open_job(verified))
        missing_verification = dict(verified)
        missing_verification.pop("verification_state", None)
        for broken in (
            missing_verification,
            {**verified, "verification_state": ""},
            {**verified, "verification_state": "INVALID"},
            {**verified, "verification_state": "DISCOVERED_CANDIDATE"},
            {**verified, "verification_state": "UNCERTAIN"},
            {**verified, "freshness": "unknown"},
            {**verified, "canonical_url": ""},
        ):
            result = evaluate_job(
                broken,
                self.scoring,
                self.targets,
                self.taxonomies,
                {"skills": []},
                as_of="2026-09-18",
            )
            self.assertEqual(result["action"], "WATCH")

    def test_feishu_blocks_unsafe_ready_before_attachment_upload(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "attachment upload blocked"):
            require_safe_delivery_jobs(
                [{"company": "Example", "title": "Role", "action": "READY"}]
            )
        self.assertFalse(
            eligible_for_document_generation(
                {"action": "READY", "matching_score": 100},
                min_score=70,
                allowed_actions={"READY", "MUST_APPLY"},
            )
        )

    def test_newer_verified_official_observation_can_reopen_closed_job(self) -> None:
        description = "Complete official job description " * 8
        closed = {
            "status": "CLOSED",
            "last_verified": "2026-09-17",
            "description": description,
            "canonicality": "CANONICAL",
            "canonical_url": "https://jobs.example.test/roles/12345",
            "source": "official",
            "source_authoritative": True,
        }
        reopened = {
            "status": "OPEN",
            "verification_state": "VERIFIED_OPEN_JOB",
            "canonicality": "CANONICAL",
            "canonical_url": "https://jobs.example.test/roles/12345",
            "source": "official",
            "source_authoritative": True,
            "freshness": "fresh",
            "description": description,
            "last_verified": "2026-09-18",
        }
        self.assertEqual(merge_job_records(closed, reopened)["status"], "OPEN")
        self.assertEqual(
            merge_job_records({**closed, "last_verified": "2026-09-19"}, reopened)["status"],
            "CLOSED",
        )
        third_party_open = {
            **reopened,
            "source": "linkedin",
            "source_authoritative": False,
            "canonical_authority": "",
            "last_verified": "2026-09-20",
        }
        self.assertEqual(merge_job_records(closed, third_party_open)["status"], "CLOSED")
        newer_open = merge_job_records(
            {**closed, "last_verified": "2026-09-16"}, reopened
        )
        self.assertEqual(newer_open["status"], "OPEN")
        self.assertEqual(newer_open["status_transition"], "REOPENED")
        self.assertTrue(newer_open["status_history"])
        same_time = merge_job_records(
            {**closed, "last_verified": "2026-09-18"}, reopened
        )
        self.assertEqual(same_time["status"], "UNCERTAIN")
        self.assertTrue(same_time["requires_human_review"])

    def test_linkedin_429_stops_remaining_queries(self) -> None:
        health = {}
        response = requests.Response()
        response.status_code = 429
        error = requests.HTTPError("429 rate limited", response=response)
        with patch(
            "scripts.fetch_linkedin.build_query_matrix",
            return_value=[
                {"keywords": "q1", "location": "深圳"},
                {"keywords": "q2", "location": "深圳"},
            ],
        ), patch("scripts.fetch_linkedin.fetch_query_jobs", side_effect=error) as fetch, patch(
            "scripts.fetch_linkedin.read_json", return_value=[]
        ), patch("scripts.fetch_linkedin.time.sleep"):
            jobs = fetch_jobs(
                str(ROOT / "config" / "targets.example.yaml"),
                8,
                24,
                taxonomy_config=str(ROOT / "config" / "taxonomies.yaml"),
                batch_index=123,
                health=health,
            )
        self.assertEqual(jobs, [])
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(health["attempted"], 1)
        self.assertEqual(health["planned"], 2)
        self.assertTrue(health["rate_limited"])
        self.assertEqual(health["query_receipts"][0]["status"], "RATE_LIMITED")
        self.assertEqual(health["query_receipts"][0]["retry_count"], 2)
        self.assertEqual(len(health["unexecuted_queries"]), 1)

    def test_linkedin_search_offset_rotates_across_daily_pages(self) -> None:
        health = {}
        queries = [
            {"keywords": "q1", "location": "深圳"},
            {"keywords": "q2", "location": "深圳"},
        ]
        with patch(
            "scripts.fetch_linkedin.build_query_matrix", return_value=queries
        ), patch(
            "scripts.fetch_linkedin.fetch_query_jobs", return_value=[]
        ) as fetch, patch(
            "scripts.fetch_linkedin.read_json", return_value=[]
        ), patch("scripts.fetch_linkedin.time.sleep"):
            fetch_jobs(
                str(ROOT / "config" / "targets.example.yaml"),
                8,
                24,
                taxonomy_config=str(ROOT / "config" / "taxonomies.yaml"),
                batch_index=4,
                search_page_cycle=3,
                health=health,
            )
        self.assertEqual(
            [call.kwargs["start"] for call in fetch.call_args_list],
            [25, 50],
        )

    def test_linkedin_capacity_reports_deferred_jobs(self) -> None:
        health = {}
        found = [
            JobListing(
                company="Example",
                position=f"Role {index}",
                url=f"https://www.linkedin.com/jobs/view/{1000 + index}",
                date="2026-09-18",
                job_id=str(1000 + index),
            )
            for index in range(35)
        ]
        with patch(
            "scripts.fetch_linkedin.build_query_matrix",
            return_value=[{"keywords": "q1", "location": "深圳"}],
        ), patch(
            "scripts.fetch_linkedin.fetch_query_jobs", return_value=found
        ), patch(
            "scripts.fetch_linkedin.hydrate_job_details"
        ), patch(
            "scripts.fetch_linkedin.read_json", return_value=[]
        ), patch("scripts.fetch_linkedin.time.sleep"):
            jobs = fetch_jobs(
                str(ROOT / "config" / "targets.example.yaml"),
                50,
                0,
                taxonomy_config=str(ROOT / "config" / "taxonomies.yaml"),
                health=health,
            )
        self.assertEqual(len(jobs), 30)
        self.assertEqual(health["raw_discovered_count"], 35)
        self.assertEqual(health["deduplicated_count"], 35)
        self.assertEqual(health["deferred_count"], 5)

    def test_query_receipt_shards_merge_and_resume_by_query_id(self) -> None:
        full = build_public_web_query_plan(
            self.targets,
            self.taxonomies,
            self.registry,
            run_date=date(2026, 9, 18),
        )
        plan = {**full, "queries": full["queries"][:2]}

        def shard(spec, status: str) -> dict:
            urls = ["https://jobs.example.test/123"] if status == "SUCCESS" else []
            return {
                "schema_version": 3,
                "mode": "MANUAL_AGENT",
                "source": "gpt_web",
                "batch": {
                    "batch_id": f"shard-{spec['query_id']}",
                    "plan_id": plan["plan_id"],
                    "generated_at": "2026-09-18T09:01:00+08:00",
                    "search_date": "2026-09-18",
                    "queries": [spec["query"]],
                    "source_urls": urls,
                    "expires_at": "2026-09-19",
                    "max_age_days": 1,
                    "searches": [{
                        "plan_id": plan["plan_id"],
                        "query_id": spec["query_id"],
                        "lane": spec["lane"],
                        "query": spec["query"],
                        "provider": "codex_web_search",
                        "started_at": "2026-09-18T09:00:00+08:00",
                        "completed_at": "2026-09-18T09:01:00+08:00",
                        "status": status,
                        "result_count": 1 if status == "SUCCESS" else 0,
                        "source_urls": urls,
                        "error_category": "" if status == "SUCCESS" else status,
                        "retry_count": 1 if status == "RATE_LIMITED" else 0,
                        "notes": "test",
                    }],
                },
                "candidates": [],
            }

        first = shard(plan["queries"][0], "SUCCESS")
        second = shard(plan["queries"][1], "RATE_LIMITED")
        self.assertEqual(validate_gpt_payload(first), [])
        self.assertEqual(validate_query_receipts(plan, first), [])
        merged = merge_batches(first, second, plan)
        self.assertEqual(validate_gpt_payload(merged), [])
        self.assertEqual(validate_query_receipts(plan, merged, require_complete=True), [])
        resumed = selected_query_plan(
            plan, existing_payload=merged, resume=True
        )
        self.assertEqual(
            [item["query_id"] for item in resumed["queries"]],
            [plan["queries"][1]["query_id"]],
        )

    def test_receiver_rebuilds_top_level_url_union_without_weakening_candidates(self) -> None:
        payload = {
            "batch": {
                "source_urls": ["https://example.test/model-mutated-url"],
                "searches": [
                    {
                        "source_urls": [
                            "https://example.test/evidence_url",
                            "https://example.test/second-url",
                        ]
                    },
                    {"source_urls": ["https://example.test/evidence_url"]},
                ],
            }
        }
        normalized = normalize_receipt_derived_fields(payload)
        self.assertEqual(
            normalized["batch"]["source_urls"],
            [
                "https://example.test/evidence_url",
                "https://example.test/second-url",
            ],
        )

    def test_receiver_bounds_verbose_receipt_notes_without_losing_terminal_evidence(self) -> None:
        verbose = "rejection detail " * 60
        payload = {
            "batch": {
                "source_urls": [],
                "searches": [{"notes": verbose, "source_urls": []}],
            }
        }
        normalized = normalize_receipt_derived_fields(payload)
        notes = normalized["batch"]["searches"][0]["notes"]
        self.assertEqual(len(notes), 500)
        self.assertTrue(notes.endswith("…"))
        self.assertEqual(normalized["batch"]["searches"][0]["source_urls"], [])

    def test_receiver_drops_candidate_with_untrusted_source_url(self) -> None:
        payload = {
            "batch": {
                "source_urls": ["https://example.test/model-mutated-url"],
                "searches": [{"source_urls": ["https://example.test/evidence-url"]}],
            },
            "candidates": [
                {"source_url": "https://example.test/evidence-url"},
                {"source_url": "https://example.test/evidence-url-typo"},
            ],
        }
        normalized = normalize_receipt_derived_fields(payload)
        self.assertEqual(
            normalized["candidates"],
            [{"source_url": "https://example.test/evidence-url"}],
        )

    def test_receiver_rederives_urls_after_retry_keeps_prior_success_receipt(self) -> None:
        plan = {
            "plan_id": "retry-plan",
            "queries": [{"query_id": "q1", "query": "query 1"}],
        }
        existing = {
            "schema_version": 3,
            "mode": "MANUAL_AGENT",
            "source": "gpt_web",
            "batch": {
                "searches": [{"query_id": "q1", "query": "query 1", "status": "SUCCESS", "source_urls": ["https://example.test/old"]}],
            },
            "candidates": [{"discovery_query_id": "q1", "source_url": "https://example.test/old"}],
        }
        retry = {
            "schema_version": 3,
            "mode": "MANUAL_AGENT",
            "source": "gpt_web",
            "batch": {
                "searches": [{"query_id": "q1", "query": "query 1", "status": "SUCCESS", "source_urls": ["https://example.test/retry-only"]}],
            },
            "candidates": [{"discovery_query_id": "q1", "source_url": "https://example.test/retry-only"}],
        }
        merged = merge_batches(existing, retry, plan)
        normalized = normalize_receipt_derived_fields(merged)
        self.assertEqual(normalized["batch"]["source_urls"], ["https://example.test/old"])
        self.assertEqual(
            normalized["candidates"],
            [{
                "discovery_query_id": "q1",
                "source_url": "https://example.test/old",
                "discovery_query": "query 1",
            }],
        )

    def test_receiver_canonicalizes_fallback_query_from_trusted_receipt(self) -> None:
        payload = {
            "batch": {
                "queries": ["original query"],
                "source_urls": ["https://example.test/evidence-url"],
                "searches": [{
                    "query_id": "q1",
                    "query": "original query",
                    "source_urls": ["https://example.test/evidence-url"],
                }],
            },
            "candidates": [{
                "source_url": "https://example.test/evidence-url",
                "discovery_query": "fallback query",
                "discovery_query_id": "q1",
            }],
        }
        normalized = normalize_receipt_derived_fields(payload)
        self.assertEqual(normalized["candidates"][0]["discovery_query"], "original query")

    def test_discovery_prompt_records_raw_results_and_rejection_reasons(self) -> None:
        prompt = build_discovery_prompt(
            "Search instructions.",
            now=datetime.fromisoformat("2026-09-19T09:00:00+08:00"),
            max_candidates=128,
            query_plan={"plan_id": "positive-control", "queries": []},
        )
        self.assertIn("raw public search results inspected", prompt)
        self.assertIn("at most 500 characters", prompt)
        self.assertIn("EMPTY_VALID only when the provider returned zero result URLs", prompt)
        self.assertIn("A zero-result first search is not enough", prompt)
        self.assertIn("materially different", prompt)
        self.assertIn("Candidate discovery_query is a provenance key", prompt)
        self.assertIn("replace it with the fallback text", prompt)
        self.assertIn("summarize rejection reasons", prompt)
        self.assertNotIn("no usable candidate EMPTY_VALID", prompt)

    def test_acceptance_dynamic_store_precedes_formal_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formal = root / "data" / "job_cache" / "discovered_company_candidates.json"
            isolated_root = root / "acceptance"
            isolated = isolated_root / "discovered_company_candidates.json"
            formal.parent.mkdir(parents=True)
            isolated_root.mkdir()
            formal.write_text(
                json.dumps({"companies": [{"normalized_name": "Formal Co"}]}),
                encoding="utf-8",
            )
            isolated.write_text(
                json.dumps({"companies": [{"normalized_name": "Isolated Co"}]}),
                encoding="utf-8",
            )

            selected = load_discovered_company_candidates(
                root, acceptance_root=isolated_root
            )
            self.assertEqual(
                selected["companies"][0]["normalized_name"], "Isolated Co"
            )

            isolated.unlink()
            seeded = load_discovered_company_candidates(
                root, acceptance_root=isolated_root
            )
            self.assertEqual(
                seeded["companies"][0]["normalized_name"], "Formal Co"
            )

    def test_acceptance_main_does_not_reset_formal_source_health(self) -> None:
        formal = ROOT / "data" / "job_cache" / "source_health.json"
        before = formal.read_bytes() if formal.exists() else None
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {}, clear=False
        ), patch.object(
            sys,
            "argv",
            [
                "run_daily_pipeline.py",
                "--config",
                "config/targets.example.yaml",
                "--source-registry",
                "config/company_registry.example.yaml",
                "--acceptance-output-root",
                directory,
                "--manual-agent-input",
                str(Path(directory) / "batch.json"),
                "--as-of",
                "2026-09-18",
            ],
        ), patch("scripts.run_daily_pipeline.run_command"):
            run_daily_pipeline.main()
            self.assertTrue((Path(directory) / "source_health.json").is_file())
        after = formal.read_bytes() if formal.exists() else None
        self.assertEqual(after, before)

    def test_acceptance_and_feishu_apply_conflict_fails_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            sys,
            "argv",
            [
                "run_daily_pipeline.py",
                "--config",
                "config/targets.example.yaml",
                "--source-registry",
                "config/company_registry.example.yaml",
                "--acceptance",
                "--acceptance-output-root",
                directory,
                "--apply-feishu",
            ],
        ):
            with self.assertRaisesRegex(ValueError, "cannot be combined"):
                run_daily_pipeline.main()

    def test_coverage_gate_and_feishu_apply_gate(self) -> None:
        as_of = "2026-09-17"
        audit = {
            "companies": [
                {
                    "company": item["name"],
                    "classification": "AUTO_API" if item.get("monitor_mode") == "AUTO" else "PUBLIC_SEARCH_ONLY",
                    "probed_at": f"{as_of}T09:00:00+08:00",
                    "last_probe_result": "HTTP_200",
                }
                for item in self.registry["companies"]
            ]
        }
        plan = build_public_web_query_plan(
            self.targets, self.taxonomies, self.registry, run_date=date.fromisoformat(as_of)
        )
        lanes = {}
        for lane, item in plan["lanes"].items():
            lanes[lane] = {
                **item,
                "attempted": item["planned"],
                "succeeded": item["planned"],
            }
        lanes["nowcoder"]["opened_urls"] = [
            "https://www.nowcoder.com/jobs/detail/positive-control"
        ]
        lanes["nowcoder"]["candidate_count"] = 1
        health = {
            "sources": {
                "official_ats": {
                    "status": "SUCCESS", "count": 1, "attempted": 1,
                    "succeeded": 1, "failed": 0, "automatic": True,
                    "details": [{
                        "company": "Example", "status": "SUCCESS",
                        "pages_attempted": 2, "pages_succeeded": 2,
                        "pagination_complete": True, "truncated": False,
                    }],
                },
                "manual_agent_china_web": {
                    "status": "SUCCESS", "count": 1, "attempted": len(plan["queries"]),
                    "succeeded": len(plan["queries"]), "failed": 0,
                    "details": {
                        "plan_id": plan["plan_id"],
                        "lane_receipts": lanes,
                    },
                },
            }
        }
        report = build_coverage_report(
            registry=self.registry,
            source_health=health,
            source_audit=audit,
            query_plan=plan,
            jobs=[{
                "canonical_key": "example|backend|shenzhen",
                "description": "complete job description " * 10,
                "status": "OPEN",
                "canonicality": "CANONICAL",
                "canonical_url": "https://jobs.example.test/roles/12345",
                "freshness": "fresh",
                "role_family": "backend",
                "verification_state": "VERIFIED_OPEN_JOB",
                "action": "READY",
            }],
            as_of=as_of,
            history=[],
        )
        self.assertEqual(report["status"], "SOURCE_COVERAGE_HEALTHY")
        self.assertEqual(
            report["acceptance_requirements"],
            {
                "P0_DAILY_COVERAGE": "100%",
                "P1_ROLLING_3_DAY_COVERAGE": "100%",
                "CORE_ROLE_DAILY_COVERAGE": "100%",
                "PUBLIC_SOURCE_LANE_DAILY_COVERAGE": "100%",
                "REGISTRY_IS_ALLOWLIST": "NO",
                "UNREGISTERED_COMPANIES_ALLOWED": "YES",
            },
        )
        recall = report["public_source_recall_diagnostic"]
        self.assertTrue(recall["diagnostic_only"])
        self.assertEqual(
            recall["by_lane"]["nowcoder"]["status"], "CANDIDATES_FOUND"
        )
        self.assertEqual(
            recall["by_lane"]["ncss"]["status"], "NO_RAW_RESULTS"
        )
        require_healthy_coverage(report)
        with self.assertRaisesRegex(RuntimeError, "Feishu apply is blocked"):
            require_healthy_coverage({"status": "SOURCE_COVERAGE_DEGRADED"})


if __name__ == "__main__":
    unittest.main()
