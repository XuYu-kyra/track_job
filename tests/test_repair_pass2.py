from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from scripts.common import load_config
from scripts.fetch_official import parse_career_page
from scripts.import_gpt_discovery import import_gpt_candidates
from scripts.job_schema import ACTIVE_PROCESS_STAGES, STAGE_OPTIONS, action_for_stage
from scripts.merge_job_sources import merge_jobs
from scripts.privacy_check import inspect_git_index, main as privacy_main
from scripts.score_jobs import evaluate_job, graduation_evidence
from scripts.setup_feishu import (
    FROZEN_FIELD_NAMES,
    FROZEN_SCHEMA_VERSION,
    configured_specs,
    frozen_mapping_errors,
)
from scripts.source_adapters import get_source_adapter, load_source_registry
from scripts.update_feishu import (
    FeishuAPIError,
    build_fields_payload,
    load_feishu_config,
    sync_records,
)
from scripts.validate_config import validate_configuration


ROOT = Path(__file__).resolve().parents[1]
FROZEN_SCHEMA_SHA256 = "baffec98089b835cb09cd73474bf99888cc63fe77f259e15df9d3d78cf9e1a5c"


class RepairPass2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.targets = load_config(ROOT / "config/targets.example.yaml")
        self.taxonomies = load_config(ROOT / "config/taxonomies.yaml")
        self.scoring = load_config(ROOT / "config/scoring.yaml")
        self.registry = load_source_registry(ROOT / "config/company_registry.example.yaml")

    def _merge(
        self,
        records: list[dict],
        *,
        previous: list[dict] | None = None,
        as_of: str = "2026-09-07",
    ) -> list[dict]:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "jobs.json"
            source.write_text(json.dumps(records), encoding="utf-8")
            return merge_jobs(
                [str(source)],
                taxonomies=self.taxonomies,
                previous_jobs=previous,
                as_of=as_of,
                source_registry=self.registry,
            )

    def test_jobs_list_wrapper_does_not_cross_contaminate_cards(self) -> None:
        jobs = parse_career_page(
            """
            <div class="jobs-list">
              <div class="job-card">
                <span>2027届 校园招聘</span><span>深圳</span>
                <a href="/jobs/campus">Backend Engineer</a>
              </div>
              <div class="job-card">
                <span>社会招聘 3年经验</span><span>北京</span>
                <a href="/jobs/social">Site Reliability Engineer</a>
              </div>
            </div>
            """,
            company="Example",
            career_url="https://careers.example.com/",
            location="深圳",
            company_type="technology",
            aliases=["backend"],
            graduation_terms=["2027", "校园招聘"],
            max_links=10,
            verified_on="2026-09-07",
        )
        by_title = {job.position: job for job in jobs}
        campus = by_title["Backend Engineer"]
        social = by_title["Site Reliability Engineer"]
        self.assertEqual(campus.location, "深圳")
        self.assertIn("2027", campus.campaign_context)
        self.assertEqual(social.location, "北京")
        self.assertNotIn("2027", social.campaign_context)
        self.assertNotIn("深圳", social.page_context)

    def test_nested_same_tag_metadata_keeps_outer_job_context(self) -> None:
        jobs = parse_career_page(
            """
            <div class="job-card">
              <div class="meta"><span>2027届 校园招聘</span><span>Location: Shenzhen</span></div>
              <div class="content"><a href="/jobs/42">Infrastructure Engineer</a></div>
            </div>
            """,
            company="Example",
            career_url="https://careers.example.com/",
            location="",
            company_type="technology",
            aliases=[],
            graduation_terms=["2027", "校园招聘"],
            max_links=10,
            verified_on="2026-09-07",
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].location, "Shenzhen")
        self.assertIn("2027", jobs[0].campaign_context)
        self.assertIn("Location: Shenzhen", jobs[0].page_context)

    def test_active_stages_survive_merge_then_evaluate(self) -> None:
        for stage in ACTIVE_PROCESS_STAGES:
            with self.subTest(stage=stage):
                previous = [{
                    "company": "Process Co",
                    "title": "2027届 Backend Engineer",
                    "description": "2027届 Python backend",
                    "role_family": "backend",
                    "location": "深圳",
                    "canonical_key": f"process:{stage}",
                    "first_seen": "2026-08-01",
                    "last_observed": "2026-08-01",
                    "last_verified": "2026-08-01",
                    "freshness": "fresh",
                    "status": "OPEN",
                    "stage": stage,
                }]
                merged = self._merge([], previous=previous, as_of="2026-09-07")
                self.assertEqual(merged[0]["freshness"], "stale")
                evaluated = evaluate_job(
                    merged[0],
                    self.scoring,
                    self.targets,
                    self.taxonomies,
                    {"skills": []},
                    as_of="2026-09-07",
                )
                self.assertEqual(evaluated["status"], "OPEN")
                self.assertEqual(evaluated["action"], action_for_stage(stage))

    def test_canonical_url_overrides_conflicting_source_ids(self) -> None:
        canonical = "https://jobs.apple.com/en-us/details/200000001/example-role"
        merged = self._merge(
            [
                {"source": "official", "company": "Apple", "title": "2027 Software Engineer", "location": "深圳", "job_id": "OFF-1", "url": canonical, "canonical_authority": "OFFICIAL_MONITOR", "verification_level": "CANONICAL", "observation_origin": "LIVE_FETCH", "observed_at": "2026-09-07", "verified_at": "2026-09-07"},
                {"source": "linkedin", "company": "Apple", "title": "Graduate Software Developer", "location": "Shenzhen China", "job_id": "LI-999", "url": "https://www.linkedin.com/jobs/view/9999999", "canonical_url": canonical},
            ]
        )
        self.assertEqual(len(merged), 1)
        self.assertIn("official", merged[0]["sources"])
        self.assertIn("linkedin", merged[0]["sources"])

    def test_same_source_namespace_and_stable_id_merge(self) -> None:
        merged = self._merge(
            [
                {"source": "linkedin", "company": "Acme", "title": "Software Engineer", "location": "深圳", "job_id": "REQ-88", "url": "https://www.linkedin.com/jobs/view/8800001"},
                {"source": "linkedin", "company": "Acme", "title": "Graduate Software Developer", "location": "Shenzhen China", "job_id": "REQ-88", "url": "https://www.linkedin.com/jobs/view/8800002"},
            ]
        )
        self.assertEqual(len(merged), 1)

    def test_same_url_merges_fallback_history_after_stable_id_enrichment(self) -> None:
        url = "https://www.nowcoder.com/jobs/detail/464426"
        previous = [{
            "source": "gpt_web",
            "source_name": "gpt_web",
            "company": "EcoFlow",
            "title": "自动化测试开发工程师",
            "location": "深圳",
            "url": url,
            "source_url": url,
            "canonical_key": "fallback:ecoflow|自动化测试开发工程师|shenzhen|unknown",
            "first_seen": "2026-09-01",
        }]
        merged = self._merge(
            [{
                "source": "gpt_web",
                "company": "EcoFlow",
                "title": "自动化测试开发工程师",
                "location": "深圳",
                "source_url": url,
                "job_id": "464426",
            }],
            previous=previous,
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["job_id"], "464426")
        self.assertTrue(merged[0]["canonical_key"].startswith("job_id:"))

    def test_shared_recruiting_page_does_not_merge_distinct_titles(self) -> None:
        url = "https://university.example.edu/campus/company/88"
        merged = self._merge(
            [
                {
                    "source": "gpt_web",
                    "company": "Example Co",
                    "title": "AI Agent工程师",
                    "location": "深圳",
                    "source_url": url,
                },
                {
                    "source": "gpt_web",
                    "company": "Example Co",
                    "title": "后端开发工程师",
                    "location": "深圳",
                    "source_url": url,
                },
            ]
        )
        self.assertEqual(len(merged), 2)

    def test_fallback_identity_preserves_explicit_cohorts(self) -> None:
        merged = self._merge(
            [
                {"source": "manual", "company": "Acme", "title": "2026届 Software Engineer", "location": "深圳", "url": "https://community.example.org/a"},
                {"source": "manual", "company": "Acme", "title": "2027届 Software Engineer", "location": "深圳", "url": "https://community.example.org/b"},
            ]
        )
        self.assertEqual(len(merged), 2)

    def test_live_observation_and_canonical_verification_timestamps(self) -> None:
        first = self._merge(
            [{
                "source": "linkedin",
                "company": "Timestamp Co",
                "title": "2027届 Engineer",
                "location": "深圳",
                "job_id": "777",
                "url": "https://www.linkedin.com/jobs/view/777",
                "observation_origin": "LIVE_FETCH",
                "observed_at": "2026-09-01",
            }],
            as_of="2026-09-01",
        )
        live_again = self._merge(
            [{
                "source": "linkedin",
                "company": "Timestamp Co",
                "title": "2027届 Engineer",
                "location": "深圳",
                "job_id": "777",
                "url": "https://www.linkedin.com/jobs/view/777",
                "observation_origin": "LIVE_FETCH",
                "observed_at": "2026-09-07",
            }],
            previous=first,
        )
        self.assertEqual(live_again[0]["last_observed"], "2026-09-07")
        self.assertEqual(live_again[0]["last_verified"], "")

        cached_gpt = self._merge(
            [{
                "source": "gpt_web",
                "company": "Timestamp Co",
                "title": "2027届 Engineer",
                "location": "深圳",
                "job_id": "777",
                "source_url": "https://www.nowcoder.com/jobs/detail/777",
                "observation_origin": "CACHE_REPLAY",
            }],
            previous=first,
        )
        self.assertEqual(cached_gpt[0]["last_observed"], "2026-09-01")
        self.assertEqual(cached_gpt[0]["last_verified"], "")

        official = self._merge(
            [{
                "source": "official",
                "company": "Timestamp Co",
                "title": "2027届 Engineer",
                "location": "深圳",
                "job_id": "777",
                "url": "https://jobs.apple.com/en-us/details/777/example",
                "canonical_authority": "OFFICIAL_MONITOR",
                "observation_origin": "LIVE_FETCH",
                "observed_at": "2026-09-07",
                "verification_level": "CANONICAL",
                "verified_at": "2026-09-07",
            }],
            previous=first,
        )
        self.assertEqual(official[0]["last_observed"], "2026-09-07")
        self.assertEqual(official[0]["last_verified"], "2026-09-07")

    def test_unrelated_years_are_not_graduation_cohorts(self) -> None:
        status, _, _ = graduation_evidence(
            {
                "title": "2027 graduate engineer",
                "description": "Company founded in 2026. Copyright 2026. 2026 product roadmap.",
            },
            self.scoring,
            self.targets,
        )
        self.assertEqual(status, "ELIGIBLE")
        for text in ("Copyright 2026", "Company founded in 2026", "2026 product roadmap"):
            with self.subTest(text=text):
                self.assertEqual(
                    graduation_evidence(
                        {"title": "Software Engineer", "description": text},
                        self.scoring,
                        self.targets,
                    )[0],
                    "WATCH",
                )
        self.assertEqual(
            graduation_evidence(
                {
                    "title": "Software Engineer",
                    "description": "Copyright 2026. Join our 2027 campus recruitment.",
                },
                self.scoring,
                self.targets,
            )[0],
            "ELIGIBLE",
        )

    def test_frozen_feishu_contract_has_exact_55_field_fingerprint(self) -> None:
        config = load_feishu_config(str(ROOT / "config/feishu.example.yaml"))
        specs = configured_specs(config)
        payload = {
            "version": FROZEN_SCHEMA_VERSION,
            "fields": FROZEN_FIELD_NAMES,
            "specs": specs,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.assertEqual(len(FROZEN_FIELD_NAMES), 55)
        self.assertEqual(len(specs), 55)
        self.assertEqual(config.fields, FROZEN_FIELD_NAMES)
        self.assertEqual(digest, FROZEN_SCHEMA_SHA256)
        self.assertTrue((ACTIVE_PROCESS_STAGES - {"PROCESS"}).issubset(STAGE_OPTIONS))

    def test_local_feishu_mapping_cannot_redefine_frozen_schema(self) -> None:
        config = load_feishu_config(str(ROOT / "config/feishu.example.yaml"))
        config.fields.pop("title")
        self.assertTrue(frozen_mapping_errors(config))
        with patch("scripts.update_feishu.feishu_request") as request:
            with self.assertRaises(FeishuAPIError):
                sync_records(config, "token", [], {}, {}, dry_run=False)
        request.assert_not_called()

    def test_human_fields_are_protected_even_when_config_requests_overwrite(self) -> None:
        config = load_feishu_config(str(ROOT / "config/feishu.example.yaml"))
        config.sync["update_human_managed_fields"] = True
        job = {
            "company": "Acme",
            "title": "Engineer",
            "stage": "OA",
            "next_action": "Complete OA",
            "offer": True,
        }
        for for_update in (False, True):
            with self.subTest(for_update=for_update):
                fields = build_fields_payload(job, None, config, {}, for_update=for_update)
                self.assertNotIn("Stage", fields)
                self.assertNotIn("Next Action", fields)
                self.assertNotIn("Offer", fields)

    def test_registry_priority_contract_rejects_p2_but_allows_growth(self) -> None:
        grown = deepcopy(self.registry)
        grown["companies"].append({
            "name": "Approved Growth Co",
            "aliases": ["AGC"],
            "monitor_priority": "P1",
            "monitor_mode": "PUBLIC_SEARCH",
            "current_2027_status": "UNKNOWN",
            "official_career_url": None,
        })
        self.assertEqual(
            validate_configuration(self.targets, self.taxonomies, self.scoring, grown),
            [],
        )
        grown["companies"][-1]["monitor_priority"] = "P2"
        self.assertTrue(
            any(
                "invalid monitor_priority" in error
                for error in validate_configuration(
                    self.targets, self.taxonomies, self.scoring, grown
                )
            )
        )

    def test_spoofed_gpt_provenance_is_overwritten_by_system(self) -> None:
        imported, errors = import_gpt_candidates(
            [{
                "source": "gpt_web",
                "company": "Spoof Co",
                "title": "2027届 Backend Engineer",
                "location": "深圳",
                "source_url": "https://community.example.org/posts/777",
                "source_name": "official_monitor",
                "discovered_by": ["official_monitor", "linkedin"],
                "discovery_sources": ["official"],
                "canonical_source": "official_workday",
                "canonical_url": "https://careers.example.org/jobs/777",
                "evidence_confidence": "High",
            }],
            imported_on="2026-09-07",
        )
        self.assertEqual(errors, [])
        normalized = get_source_adapter("gpt_web", self.registry).normalize(
            imported[0], self.taxonomies, as_of="2026-09-07"
        )
        self.assertEqual(normalized["source"], "gpt_web")
        self.assertEqual(normalized["source_name"], "gpt_web")
        self.assertEqual(normalized["discovered_by"], ["gpt_web"])
        self.assertEqual(normalized["discovery_sources"], ["gpt_web"])
        self.assertEqual(normalized["canonical_url"], "")
        self.assertEqual(normalized["evidence_confidence"], "Medium")

    def test_privacy_git_scan_success_failure_and_missing_git(self) -> None:
        with patch(
            "scripts.privacy_check.subprocess.run",
            return_value=CompletedProcess([], 0, stdout="", stderr=""),
        ):
            tracked, error = inspect_git_index(ROOT)
            self.assertEqual(tracked, [])
            self.assertEqual(error, "")
        with patch(
            "scripts.privacy_check.subprocess.run",
            return_value=CompletedProcess(
                [], 128, stdout="", stderr="fatal: detected dubious ownership"
            ),
        ), patch("scripts.privacy_check.iter_text_files", return_value=[]):
            self.assertNotEqual(privacy_main(), 0)
        with patch(
            "scripts.privacy_check.subprocess.run", side_effect=FileNotFoundError
        ), patch("scripts.privacy_check.iter_text_files", return_value=[]):
            self.assertNotEqual(privacy_main(), 0)


if __name__ == "__main__":
    unittest.main()
