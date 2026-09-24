from __future__ import annotations

import json
import tempfile
import unittest
from argparse import Namespace
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from scripts.application_profiles import (
    configured_profiles,
    phone_for_ats,
    select_application_profile,
)
from scripts.ats_detection import detect_ats_family
from scripts.common import load_config
from scripts.fetch_official import fetch_registered_pages
from scripts.generate_resume import load_profile
from scripts.import_gpt_discovery import import_gpt_candidates, validate_gpt_candidate
from scripts.job_schema import backfill_discoverable
from scripts.merge_job_sources import merge_jobs
from scripts.query_matrix import posted_window_hours
from scripts.run_daily_pipeline import build_commands
from scripts.score_jobs import compute_score, evaluate_job, mark_registry_candidates
from scripts.source_adapters import get_source_adapter, load_source_registry, source_spec


ROOT = Path(__file__).resolve().parents[1]


class DiscoveryArchitectureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.targets = load_config(ROOT / "config/targets.example.yaml")
        self.taxonomies = load_config(ROOT / "config/taxonomies.yaml")
        self.scoring = load_config(ROOT / "config/scoring.yaml")
        self.registry = load_source_registry(ROOT / "config/company_registry.example.yaml")
        self.inventory = {
            "skills": [
                {"skill": "python"},
                {"skill": "backend_api"},
                {"skill": "testing_automation"},
            ]
        }

    def _normalize(self, raw: dict, registry: dict | None = None) -> dict:
        adapter = get_source_adapter(str(raw.get("source") or "manual"), registry or self.registry)
        return adapter.normalize(raw, self.taxonomies, as_of="2026-09-07")

    def test_boss_is_manual_and_pipeline_has_no_boss_crawler(self) -> None:
        spec = source_spec("boss", self.registry)
        self.assertEqual(spec["automation_mode"], "MANUAL")
        self.assertEqual(spec["implementation_status"], "manual_no_crawler")
        args = Namespace(
            config="config/targets.example.yaml",
            feishu_config="config/feishu.yaml",
            execution_config="config/execution.yaml",
            source_registry="config/company_registry.example.yaml",
            taxonomy_config="config/taxonomies.yaml",
            mode="daily",
            batch_index=0,
            skip_fetch=False,
            skip_repo_sync=True,
            skip_documents=True,
            apply_feishu=False,
            as_of="",
        )
        flattened = "\n".join(" ".join(item) for item in build_commands(args, self.targets, "python3", self.registry))
        self.assertNotIn("fetch_boss", flattened)

    def test_ats_detection_alone_remains_secondary(self) -> None:
        job = self._normalize(
            {
                "source": "gpt_web",
                "company": "Candidate Company",
                "title": "2027 Software Engineer",
                "location": "Shenzhen",
                "source_url": "https://candidate.wd5.myworkdayjobs.com/en-US/jobs/job/123",
            }
        )
        self.assertEqual(job["ats_family"], "Workday")
        self.assertEqual(job["canonicality"], "SECONDARY")
        self.assertEqual(job["evidence_confidence"], "Medium")
        self.assertEqual(job["source"], "gpt_web")

    def test_linkedin_is_supplemental_and_not_the_only_source(self) -> None:
        spec = source_spec("linkedin", self.registry)
        self.assertEqual(spec["canonicality"], "SECONDARY")
        self.assertEqual(spec["discovery_priority"], "MEDIUM")
        self.assertIn("official", self.targets["job_search"]["sources"])
        self.assertIn("gpt_web", self.targets["job_search"]["sources"])

    def test_unknown_non_registry_company_enters_normal_pipeline(self) -> None:
        job = self._normalize(
            {
                "source": "gpt_web",
                "company": "Long Tail Robotics",
                "title": "2027 Backend Engineer",
                "location": "深圳",
                "source_url": "https://jobs.longtail-robotics.cn/positions/123",
                "status": "OPEN",
                "description": "2027届 Python API testing Linux backend",
            }
        )
        result = evaluate_job(
            job,
            self.scoring,
            self.targets,
            self.taxonomies,
            self.inventory,
            as_of="2026-09-07",
        )
        self.assertFalse(result["registry_member"])
        self.assertNotEqual(result["action"], "REJECT")

    def test_registry_membership_does_not_change_opportunity_score(self) -> None:
        raw = {
            "source": "linkedin",
            "company": "Tencent",
            "title": "2027 Backend Engineer",
            "location": "深圳",
            "url": "https://www.linkedin.com/jobs/view/123456789",
            "description": "2027届 Python API testing Linux backend",
        }
        registered = self._normalize(raw, self.registry)
        unregistered = self._normalize(raw, {"companies": [], "sources": self.registry["sources"]})
        score_a, _ = compute_score(registered, self.scoring, self.targets, self.taxonomies, self.inventory)
        score_b, _ = compute_score(unregistered, self.scoring, self.targets, self.taxonomies, self.inventory)
        self.assertTrue(registered["registry_member"])
        self.assertFalse(unregistered["registry_member"])
        self.assertEqual(score_a, score_b)

    def test_required_ats_families_are_detected(self) -> None:
        cases = {
            "https://app.mokahr.com/campus-recruitment/acme/1": "Moka",
            "https://acme.zhiye.com/campus/jobs/1": "Beisen",
            "https://campus.hotjob.cn/wt/acme/web/index": "Hotjob",
            "https://campus.51job.com/acme/job.htm": "51job campus",
            "https://xiaoyuan.zhaopin.com/job/1": "Zhaopin campus",
            "https://foo.wd5.myworkdayjobs.com/job/1": "Workday",
            "https://boards.greenhouse.io/acme/jobs/1": "Greenhouse",
            "https://jobs.lever.co/acme/1": "Lever",
            "https://jobs.smartrecruiters.com/Acme/1": "SmartRecruiters",
            "https://jobs.successfactors.com/career?company=acme": "SAP SuccessFactors",
            "https://acme.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/job": "Oracle Recruiting Cloud / Taleo",
            "https://apply.workable.com/acme/j/1": "Workable",
            "https://jobs.apple.com/en-us/details/1": "Apple Jobs",
            "https://www.amazon.jobs/en/jobs/1": "Amazon Jobs",
            "https://career.dji.com/position/1": "DJI Careers",
            "https://join.qq.com/post.html": "Tencent Careers",
            "https://join.tencentmusic.com/job/1": "TME Careers",
            "https://www.asml.com/en/careers/find-your-job/1": "ASML Careers",
            "https://www.siemens-healthineers.com/careers/job/1": "Siemens Healthineers Careers",
            "https://apply.careers.hsbc.com/job/1": "HSBC Careers",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(detect_ats_family(url), expected)

    def test_gpt_record_without_source_url_is_rejected(self) -> None:
        errors = validate_gpt_candidate({"company": "No URL", "title": "Engineer"})
        self.assertTrue(any("source_url" in error for error in errors))
        valid, import_errors = import_gpt_candidates(
            [{"company": "No URL", "title": "Engineer"}],
            imported_on="2026-09-07",
        )
        self.assertEqual(valid, [])
        self.assertTrue(import_errors)
        profile_errors = validate_gpt_candidate(
            {
                "source_url": "https://www.nowcoder.com/jobs/detail/123456",
                "application_profile": "CN",
            }
        )
        self.assertTrue(any("application profile" in error for error in profile_errors))

    def test_gpt_evidence_still_uses_normal_eligibility_gate(self) -> None:
        job = self._normalize(
            {
                "source": "gpt_web",
                "company": "Evidence Company",
                "title": "Backend Engineer",
                "location": "深圳",
                "source_url": "https://www.nowcoder.com/jobs/detail/777777",
                "description": "Python API testing",
                "graduation_evidence": ["source excerpt: 2027届校园招聘"],
                "role_evidence": ["后端开发工程师"],
                "status": "OPEN",
            }
        )
        result = evaluate_job(
            job,
            self.scoring,
            self.targets,
            self.taxonomies,
            self.inventory,
            as_of="2026-09-07",
        )
        self.assertEqual(result["graduation_eligibility"], "ELIGIBLE")
        self.assertNotEqual(result["action"], "REJECT")

    def test_gpt_non_registry_company_is_processed_normally(self) -> None:
        valid, errors = import_gpt_candidates(
            [
                {
                    "company": "Unlisted Systems",
                    "title": "2027 测试开发工程师",
                    "location": "深圳",
                    "source_url": "https://www.nowcoder.com/jobs/detail/123456",
                    "description": "2027届 Python pytest 自动化测试",
                    "status": "OPEN",
                }
            ],
            imported_on="2026-09-07",
        )
        self.assertEqual(errors, [])
        result = evaluate_job(
            self._normalize(valid[0]),
            self.scoring,
            self.targets,
            self.taxonomies,
            self.inventory,
            as_of="2026-09-07",
        )
        self.assertFalse(result["registry_member"])
        self.assertNotEqual(result["action"], "REJECT")

    def test_gpt_ats_candidate_stays_unverified_and_keeps_provenance(self) -> None:
        imported, errors = import_gpt_candidates(
            [{
                "source": "gpt_web",
                "source_name": "university_page",
                "company": "Candidate Company",
                "title": "2027 Software Engineer",
                "location": "Shenzhen",
                "source_url": "https://career.example.edu.cn/jobs/123",
                "canonical_url": "https://candidate.wd5.myworkdayjobs.com/en-US/jobs/job/123",
                "evidence_confidence": "Medium",
                "discovered_by": ["gpt_web", "university_page"],
            }],
            imported_on="2026-09-07",
        )
        self.assertEqual(errors, [])
        job = self._normalize(imported[0])
        self.assertEqual(job["canonical_source"], "")
        self.assertEqual(job["official_url"], "")
        self.assertEqual(job["canonical_url"], "")
        self.assertEqual(job["ats_family"], "Workday")
        self.assertEqual(job["evidence_confidence"], "Medium")
        self.assertEqual(job["discovered_by"], ["gpt_web"])
        self.assertIn("gpt_web", job["sources"])
        self.assertNotIn("official", job["sources"])

    def test_gpt_cannot_bypass_downstream_scoring(self) -> None:
        job = self._normalize(
            {
                "source": "gpt_web",
                "company": "Candidate Company",
                "title": "2027 Software Engineer",
                "location": "深圳",
                "source_url": "https://www.nowcoder.com/jobs/detail/654321",
                "description": "2027届 entry level software role",
                "status": "OPEN",
                "action": "MUST_APPLY",
                "opportunity_value": 100,
            }
        )
        result = evaluate_job(
            job,
            self.scoring,
            self.targets,
            self.taxonomies,
            {},
            as_of="2026-09-07",
        )
        self.assertNotEqual(result["action"], "MUST_APPLY")
        self.assertNotEqual(result["opportunity_value"], 100)

    def test_manual_boss_job_is_normalized_deduped_and_scored(self) -> None:
        raw = {
            "source": "boss",
            "company": "Manual Company",
            "title": "2027 后端开发工程师",
            "location": "深圳",
            "url": "https://www.zhipin.com/job_detail/abc123.html",
            "description": "2027届 Python API Linux testing",
            "status": "OPEN",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "boss.json"
            path.write_text(json.dumps([raw, raw]), encoding="utf-8")
            merged = merge_jobs(
                [str(path)],
                taxonomies=self.taxonomies,
                as_of="2026-09-07",
                source_registry=self.registry,
            )
        self.assertEqual(len(merged), 1)
        result = evaluate_job(
            merged[0],
            self.scoring,
            self.targets,
            self.taxonomies,
            self.inventory,
            as_of="2026-09-07",
        )
        self.assertIn(result["action"], {"WATCH", "HOLD", "READY", "MUST_APPLY"})

    def test_profile_recommendation_and_manual_override(self) -> None:
        chinese, _, _ = select_application_profile(
            {"title": "2027届软件开发工程师", "description": "深圳校园招聘"}
        )
        international, _, _ = select_application_profile(
            {
                "title": "Graduate Software Engineer",
                "description": "English-language global graduate recruitment",
                "url": "https://candidate.wd5.myworkdayjobs.com/job/123",
            }
        )
        overridden, reason, _ = select_application_profile(
            {"title": "2027届软件开发工程师", "application_profile": "INTL"}
        )
        self.assertEqual(chinese, "CN")
        self.assertEqual(international, "INTL")
        self.assertEqual(overridden, "INTL")
        self.assertEqual(reason, "manual_override")

    def test_single_phone_ats_uses_only_selected_primary(self) -> None:
        profiles = configured_profiles(load_config(ROOT / "config/profile.example.yaml"))
        self.assertEqual(phone_for_ats(profiles["CN"]), profiles["CN"]["phone"])
        self.assertEqual(phone_for_ats(profiles["INTL"]), profiles["INTL"]["phone"])
        self.assertNotIn(profiles["INTL"]["alternate_phone"], phone_for_ats(profiles["INTL"]))

    def test_resume_loader_uses_job_context_and_override(self) -> None:
        path = str(ROOT / "config/profile.example.yaml")
        cn = load_profile(path, {"title": "2027届软件开发工程师", "description": "中文申请"})
        intl = load_profile(
            path,
            {
                "title": "Graduate Software Engineer",
                "description": "English global recruiting flow",
            },
        )
        overridden = load_profile(
            path,
            {"title": "2027届软件开发工程师"},
            application_profile="INTL",
        )
        self.assertNotEqual(cn["full_name"], intl["full_name"])
        self.assertEqual(overridden["full_name"], intl["full_name"])

    def test_public_search_registry_seed_is_not_auto_fetched(self) -> None:
        registry = {
            "companies": [
                {
                    "name": "Research First Company",
                    "enabled": True,
                    "monitor_mode": "PUBLIC_SEARCH",
                    "official_career_url": "https://jobs.research-first.cn",
                }
            ]
        }
        with patch("scripts.fetch_official.requests.Session.get") as get:
            jobs = fetch_registered_pages(self.targets, self.taxonomies, registry)
        self.assertEqual(jobs, [])
        get.assert_not_called()

    def test_backfill_and_daily_refresh_contract(self) -> None:
        self.assertTrue(
            backfill_discoverable(
                {"status": "OPEN", "posted_at": "2026-08-08", "deadline": "2026-09-30"},
                "2026-09-07",
                max_age_days=60,
            )
        )
        self.assertEqual(posted_window_hours(self.targets, "daily"), 72)
        official = get_source_adapter("official", self.registry)
        previous = official.normalize(
            {
                "source": "official",
                "canonical_authority": "OFFICIAL_MONITOR",
                "verification_level": "CANONICAL",
                "observation_origin": "LIVE_FETCH",
                "observed_at": "2026-09-01",
                "verified_at": "2026-09-01",
                "company": "Refresh Company",
                "title": "2027 Backend Engineer",
                "location": "深圳",
                "url": "https://jobs.refresh-company.cn/123",
                "posted_at": "2026-08-01",
                "last_verified": "2026-09-01",
            },
            self.taxonomies,
            as_of="2026-09-01",
        )
        refreshed_raw = {
                "source": "official",
                "canonical_authority": "OFFICIAL_MONITOR",
                "observation_origin": "LIVE_FETCH",
                "observed_at": "2026-09-07",
                "verification_level": "CANONICAL",
                "verified_at": "2026-09-07",
                "company": "Refresh Company",
                "title": "2027 Backend Engineer",
                "location": "深圳",
                "url": "https://jobs.refresh-company.cn/123",
                "posted_at": "2026-08-01",
                "status": "OPEN",
            }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "refresh.json"
            path.write_text(json.dumps([refreshed_raw]), encoding="utf-8")
            merged = merge_jobs(
                [str(path)],
                taxonomies=self.taxonomies,
                previous_jobs=[previous],
                as_of="2026-09-07",
                source_registry=self.registry,
            )
        self.assertEqual(merged[0]["first_seen"], previous["first_seen"])
        self.assertEqual(merged[0]["last_verified"], "2026-09-07")
        self.assertEqual(merged[0]["posted_at"], "2026-08-01")

    def test_repeated_strong_non_registry_company_is_review_flagged(self) -> None:
        jobs = [
            {
                "company": "Emerging Company",
                "canonical_key": f"emerging|role-{index}|shenzhen",
                "role_family": "backend",
                "eligibility": "ELIGIBLE",
                "opportunity_value": 85,
            }
            for index in range(2)
        ]
        mark_registry_candidates(jobs, {"companies": []}, self.scoring)
        self.assertTrue(all(job["registry_candidate"] for job in jobs))
        self.assertTrue(all(not job["registry_member"] for job in jobs))

    def test_four_source_dedup_retains_all_provenance_and_canonical(self) -> None:
        canonical = "https://jobs.unified-company.cn/123"
        common = {
            "company": "Unified Company",
            "title": "2027 Backend Engineer",
            "location": "深圳",
            "description": "2027届 Python API backend",
            "status": "OPEN",
        }
        records = [
            {
                **common,
                "source": "official",
                "url": canonical,
                "canonical_authority": "OFFICIAL_MONITOR",
                "verification_level": "CANONICAL",
                "observation_origin": "LIVE_FETCH",
                "observed_at": "2026-09-07",
                "verified_at": "2026-09-07",
            },
            {**common, "source": "gpt_web", "url": "https://www.nowcoder.com/jobs/detail/123", "canonical_url": canonical},
            {**common, "source": "linkedin", "url": "https://www.linkedin.com/jobs/view/123456789", "canonical_url": canonical},
            {**common, "source": "manual", "manual_ingest": True, "url": "https://career.example.edu.cn/jobs/123", "canonical_url": canonical},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for index, record in enumerate(records):
                path = Path(tmp) / f"source-{index}.json"
                path.write_text(json.dumps([record]), encoding="utf-8")
                paths.append(str(path))
            merged = merge_jobs(
                paths,
                taxonomies=self.taxonomies,
                as_of="2026-09-07",
                source_registry=self.registry,
            )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["source"], "official")
        self.assertEqual(
            set(merged[0]["sources"]),
            {"official", "gpt_web", "linkedin", "manual"},
        )
        self.assertTrue({"official_monitor", "gpt_web", "linkedin", "other_manual"}.issubset(set(merged[0]["discovered_by"])))

    def test_registry_seed_shape_and_unknown_statuses(self) -> None:
        self.assertTrue(self.registry["companies"])
        self.assertTrue(
            all(company["monitor_priority"] in {"P0", "P1"} for company in self.registry["companies"])
        )
        names = [company["name"].casefold() for company in self.registry["companies"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(company["current_2027_status"] == "UNKNOWN" for company in self.registry["companies"]))
        self.assertTrue(all(not company["official_career_url"] for company in self.registry["companies"]))


if __name__ == "__main__":
    unittest.main()
