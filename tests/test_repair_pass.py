from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from copy import deepcopy
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

from scripts.build_candidate_inventory import relative_evidence_path
from scripts.common import load_config
from scripts.fetch_official import parse_career_page
from scripts.import_gpt_discovery import (
    import_gpt_candidates,
    load_gpt_candidates,
    validate_gpt_candidate,
)
from scripts.job_schema import ACTIVE_PROCESS_STAGES, action_for_stage
from scripts.merge_job_sources import merge_jobs
from scripts.run_daily_pipeline import build_commands
from scripts.score_jobs import eligibility_gate, evaluate_job, graduation_evidence
from scripts.source_adapters import get_source_adapter, load_source_registry
from scripts.update_feishu import FeishuAPIError, FeishuConfig, sync_records
from scripts.validate_config import validate_configuration


ROOT = Path(__file__).resolve().parents[1]


class RepairPassTests(unittest.TestCase):
    def setUp(self) -> None:
        self.targets = load_config(ROOT / "config/targets.example.yaml")
        self.taxonomies = load_config(ROOT / "config/taxonomies.yaml")
        self.scoring = load_config(ROOT / "config/scoring.yaml")
        self.registry = load_source_registry(ROOT / "config/company_registry.example.yaml")

    def _graduation(self, text: str) -> tuple[str, str, list[str]]:
        return graduation_evidence(
            {"title": "Software Engineer", "description": text},
            self.scoring,
            self.targets,
        )

    def _merge(self, records: list[dict]) -> list[dict]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            path.write_text(json.dumps(records), encoding="utf-8")
            return merge_jobs(
                [str(path)],
                taxonomies=self.taxonomies,
                source_registry=self.registry,
                as_of="2026-09-07",
            )

    def test_chinese_cohort_parsing_is_explicit_and_safe(self) -> None:
        for text in ("面向26届毕业生", "面向28届毕业生", "面向2026届毕业生"):
            with self.subTest(text=text):
                self.assertEqual(self._graduation(text)[0], "REJECT")
        self.assertEqual(self._graduation("面向2027届毕业生")[0], "ELIGIBLE")
        self.assertEqual(self._graduation("面向27届毕业生")[0], "ELIGIBLE")
        self.assertEqual(
            self._graduation("2027届与2028届联合招聘")[0],
            "WATCH",
        )
        for text in ("校园招聘", "应届毕业生", "校招岗位"):
            with self.subTest(text=text):
                self.assertEqual(self._graduation(text)[0], "WATCH")

    def test_official_parser_uses_job_level_location_only(self) -> None:
        jobs = parse_career_page(
            """
            <div class="job-card"><a href="/jobs/beijing">Backend Engineer</a><span>工作地点：北京</span></div>
            <div class="job-card"><a href="/jobs/unknown">Platform Engineer</a></div>
            <div class="job-card"><a href="/jobs/shenzhen">Test Engineer</a><span>Location: Shenzhen</span></div>
            """,
            company="Example",
            career_url="https://careers.example.com/campus",
            location="深圳",
            company_type="technology",
            aliases=["backend"],
            graduation_terms=["2027"],
            max_links=10,
            verified_on="2026-09-07",
        )
        by_url = {job.url: job for job in jobs}
        self.assertEqual(by_url["https://careers.example.com/jobs/beijing"].location, "北京")
        self.assertEqual(by_url["https://careers.example.com/jobs/unknown"].location, "")
        self.assertEqual(by_url["https://careers.example.com/jobs/shenzhen"].location, "Shenzhen")
        unknown = get_source_adapter("official", self.registry).normalize(
            by_url["https://careers.example.com/jobs/unknown"].__dict__,
            self.taxonomies,
            as_of="2026-09-07",
        )
        unknown["description"] = "2027届 Backend Engineer"
        self.assertEqual(
            eligibility_gate(unknown, self.scoring, self.targets),
            ("WATCH", "location_evidence_missing"),
        )

    def test_mixed_campus_social_page_context_is_isolated(self) -> None:
        jobs = parse_career_page(
            """
            <h1>2027 校园招聘</h1>
            <div class="job-card"><a href="/jobs/campus">Backend Engineer</a><span>2027届 校园招聘 深圳</span></div>
            <div class="job-card"><a href="/jobs/experienced">Site Reliability Engineer</a><span>社会招聘 3年经验 北京</span></div>
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
        self.assertIn("2027", by_title["Backend Engineer"].campaign_context)
        social = by_title["Site Reliability Engineer"]
        self.assertNotIn("2027", social.campaign_context)
        normalized = get_source_adapter("official", self.registry).normalize(
            social.__dict__, self.taxonomies, as_of="2026-09-07"
        )
        self.assertNotEqual(
            graduation_evidence(normalized, self.scoring, self.targets)[0],
            "ELIGIBLE",
        )

    def test_non_alias_job_survives_but_navigation_does_not(self) -> None:
        jobs = parse_career_page(
            '<a href="/jobs/infra-1">Infrastructure Engineer</a><a href="/about">关于我们</a>',
            company="Example",
            career_url="https://careers.example.com/",
            location="深圳",
            company_type="technology",
            aliases=["backend developer"],
            graduation_terms=["2027"],
            max_links=10,
        )
        self.assertEqual([job.position for job in jobs], ["Infrastructure Engineer"])

    def test_canonical_url_dedup_handles_title_and_location_variants(self) -> None:
        jobs = self._merge(
            [
                {
                    "source": "official",
                    "canonical_authority": "OFFICIAL_MONITOR",
                    "verification_level": "CANONICAL",
                    "observation_origin": "LIVE_FETCH",
                    "observed_at": "2026-09-07",
                    "verified_at": "2026-09-07",
                    "company": "Acme",
                    "title": "2027 Software Engineer",
                    "location": "深圳",
                    "url": "https://jobs.acme.com/jobs/123?utm_source=feed",
                },
                {
                    "source": "official",
                    "company": "Acme",
                    "title": "Graduate Software Developer",
                    "location": "Shenzhen, China",
                    "url": "https://jobs.acme.com/jobs/123/",
                },
            ]
        )
        self.assertEqual(len(jobs), 1)

    def test_stable_job_id_dedup_and_distinct_id_safety(self) -> None:
        same_id = self._merge(
            [
                {"source": "linkedin", "company": "Acme", "title": "Engineer", "location": "深圳", "job_id": "REQ-7", "url": "https://www.linkedin.com/jobs/view/1000001"},
                {"source": "gpt_web", "company": "Acme", "title": "Software Engineer", "location": "Shenzhen", "job_id": "REQ-7", "source_url": "https://www.nowcoder.com/jobs/detail/1000002"},
            ]
        )
        self.assertEqual(len(same_id), 2)
        distinct = self._merge(
            [
                {"source": "linkedin", "company": "Acme", "title": "Engineer", "location": "深圳", "job_id": "REQ-8", "url": "https://www.linkedin.com/jobs/view/1000003"},
                {"source": "linkedin", "company": "Acme", "title": "Engineer", "location": "深圳", "job_id": "REQ-9", "url": "https://www.linkedin.com/jobs/view/1000004"},
            ]
        )
        self.assertEqual(len(distinct), 2)

    def test_four_source_variant_merge_retains_provenance(self) -> None:
        canonical = "https://candidate.wd5.myworkdayjobs.com/en-US/jobs/job/REQ-42"
        records = [
            {"source": "official", "company": "Acme", "title": "2027 Backend Engineer", "location": "深圳", "url": canonical, "canonical_authority": "OFFICIAL_MONITOR", "verification_level": "CANONICAL", "observation_origin": "LIVE_FETCH", "observed_at": "2026-09-07", "verified_at": "2026-09-07"},
            {"source": "gpt_web", "company": "Acme Ltd", "title": "Graduate Backend Developer", "location": "Shenzhen", "source_url": "https://www.nowcoder.com/jobs/detail/1000042", "canonical_url": canonical},
            {"source": "linkedin", "company": "Acme", "title": "Software Engineer - Backend", "location": "Shenzhen China", "url": "https://www.linkedin.com/jobs/view/1000042", "canonical_url": canonical},
            {"source": "manual", "manual_ingest": True, "company": "ACME", "title": "Backend Role", "location": "深圳", "url": "https://community.example.org/post/42", "canonical_url": canonical},
        ]
        jobs = self._merge(records)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["canonical_url"], canonical)
        self.assertTrue({"official", "gpt_web", "linkedin", "manual"}.issubset(jobs[0]["sources"]))
        self.assertGreaterEqual(len(jobs[0]["source_observations"]), 4)
        self.assertTrue({"official_monitor", "gpt_web", "linkedin", "other_manual"}.issubset(jobs[0]["discovered_by"]))

    def test_cached_replay_preserves_verification_but_live_official_advances(self) -> None:
        base = get_source_adapter("gpt_web", self.registry).normalize(
            {
                "source": "gpt_web",
                "company": "Replay Co",
                "title": "2027 Engineer",
                "location": "深圳",
                "source_url": "https://www.nowcoder.com/jobs/detail/7654321",
            },
            self.taxonomies,
            as_of="2026-09-01",
        )
        # Simulate a canonical verification already stored by an earlier
        # trusted system path; replay itself must not advance or invent it.
        base["last_verified"] = "2026-09-01"
        cached = {
            "source": "gpt_web",
            "company": "Replay Co",
            "title": "2027 Engineer",
            "location": "深圳",
            "source_url": "https://www.nowcoder.com/jobs/detail/7654321",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cached.json"
            path.write_text(json.dumps([cached]), encoding="utf-8")
            replayed = merge_jobs([str(path)], taxonomies=self.taxonomies, previous_jobs=[base], as_of="2026-09-07", source_registry=self.registry)
            replayed_twice = merge_jobs([str(path)], taxonomies=self.taxonomies, previous_jobs=replayed, as_of="2026-09-08", source_registry=self.registry)
            manual = dict(cached, source="manual", manual_ingest=True)
            path.write_text(json.dumps([manual]), encoding="utf-8")
            manual_replay = merge_jobs([str(path)], taxonomies=self.taxonomies, previous_jobs=[base], as_of="2026-09-09", source_registry=self.registry)
            live = dict(
                cached,
                source="official",
                canonical_authority="OFFICIAL_MONITOR",
                observation_origin="LIVE_FETCH",
                observed_at="2026-09-10",
                verification_level="CANONICAL",
                verified_at="2026-09-10",
            )
            path.write_text(json.dumps([live]), encoding="utf-8")
            live_result = merge_jobs([str(path)], taxonomies=self.taxonomies, previous_jobs=[base], as_of="2026-09-10", source_registry=self.registry)
        self.assertEqual(replayed_twice[0]["last_verified"], "2026-09-01")
        self.assertEqual(manual_replay[0]["last_verified"], "2026-09-01")
        self.assertEqual(live_result[0]["last_verified"], "2026-09-10")

    def test_backfill_pipeline_history_is_isolated_from_daily(self) -> None:
        args = Namespace(
            config="config/targets.example.yaml",
            feishu_config="config/feishu.yaml",
            execution_config="config/execution.yaml",
            source_registry="config/company_registry.example.yaml",
            taxonomy_config="config/taxonomies.yaml",
            mode="backfill",
            batch_index=0,
            skip_fetch=True,
            skip_repo_sync=True,
            skip_documents=True,
            apply_feishu=False,
            as_of="2026-09-07",
        )
        commands = build_commands(args, self.targets, sys.executable, self.registry)
        merge_command = next(
            command
            for command in commands
            if any(part.endswith("merge_job_sources.py") for part in command)
        )
        history_index = merge_command.index("--history") + 1
        self.assertEqual(merge_command[history_index], "data/job_cache/backfill_job_history.json")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            daily = tmp_path / "job_history.json"
            daily.write_text('[{"stage":"OA"}]', encoding="utf-8")
            before = daily.read_bytes()
            empty = tmp_path / "empty.json"
            empty.write_text("[]", encoding="utf-8")
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/merge_job_sources.py"),
                    "--inputs", str(empty),
                    "--output", str(tmp_path / "backfill_jobs.json"),
                    "--history", str(tmp_path / "backfill_job_history.json"),
                    "--taxonomy-config", str(ROOT / "config/taxonomies.yaml"),
                    "--targets-config", str(ROOT / "config/targets.example.yaml"),
                    "--source-registry", str(ROOT / "config/company_registry.example.yaml"),
                    "--as-of", "2026-09-07",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(daily.read_bytes(), before)

    def test_normal_pipeline_never_invokes_schema_setup(self) -> None:
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
            apply_feishu=True,
            as_of="2026-09-07",
        )
        flattened = "\n".join(
            " ".join(command)
            for command in build_commands(args, self.targets, sys.executable, self.registry)
        )
        self.assertNotIn("setup_feishu.py", flattened)

    def test_daily_pipeline_compiles_application_pdfs(self) -> None:
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
            skip_documents=False,
            apply_feishu=False,
            as_of="2026-09-07",
        )
        commands = build_commands(args, self.targets, sys.executable, self.registry)
        generate_command = next(
            command
            for command in commands
            if any(part.endswith("generate_resume.py") for part in command)
        )
        self.assertIn("--compile-pdf", generate_command)

    def test_all_configured_active_stages_resist_stale_demotion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty.json"
            empty.write_text("[]", encoding="utf-8")
            for stage in ACTIVE_PROCESS_STAGES:
                with self.subTest(stage=stage):
                    previous = [{
                        "company": "Process Co",
                        "title": f"Engineer {stage}",
                        "location": "深圳",
                        "canonical_key": f"process-{stage}",
                        "last_verified": "2026-08-01",
                        "status": "OPEN",
                        "stage": stage,
                    }]
                    result = merge_jobs([str(empty)], taxonomies=self.taxonomies, previous_jobs=previous, as_of="2026-09-07", source_registry=self.registry)
                    self.assertEqual(result[0]["status"], "OPEN")
                    evaluated = evaluate_job(
                        result[0],
                        self.scoring,
                        self.targets,
                        self.taxonomies,
                        {"skills": []},
                        as_of="2026-09-07",
                    )
                    self.assertEqual(evaluated["action"], action_for_stage(stage))

    def test_registry_can_grow_and_alias_conflicts_are_checked(self) -> None:
        grown = deepcopy(self.registry)
        grown["companies"].append(
            {
                "name": "Human Approved New Company",
                "aliases": ["HANC"],
                "monitor_priority": "P0",
                "monitor_mode": "PUBLIC_SEARCH",
                "current_2027_status": "UNKNOWN",
                "official_career_url": None,
            }
        )
        errors = validate_configuration(
            self.targets, self.taxonomies, self.scoring, grown
        )
        self.assertEqual(errors, [])

    def test_gpt_schema_and_decision_controls_are_rejected(self) -> None:
        valid_candidate = {
            "company": "Acme",
            "title": "2027 Engineer",
            "source_url": "https://www.nowcoder.com/jobs/detail/1234567",
        }
        for field in ("action", "score", "release_priority", "application_profile", "feishu_apply"):
            with self.subTest(field=field):
                errors = validate_gpt_candidate({**valid_candidate, field: "MUST_APPLY"})
                self.assertTrue(any("not allowed" in error for error in errors))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "candidates.json"
            path.write_text(json.dumps({"schema_version": 2, "mode": "MANUAL_AGENT", "source": "gpt_web", "candidates": []}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_gpt_candidates(path)
            path.write_text(json.dumps({"schema_version": 1, "mode": "MANUAL_AGENT", "source": "gpt_web", "candidates": [valid_candidate]}), encoding="utf-8")
            candidates = load_gpt_candidates(path)
            imported, errors = import_gpt_candidates(candidates, imported_on="2026-09-07")
            self.assertEqual(len(imported), 1)
            self.assertEqual(errors, [])

    def test_manual_source_label_cannot_self_promote(self) -> None:
        unverified = get_source_adapter("official", self.registry).normalize(
            {
                "source": "official",
                "manual_ingest": True,
                "company": "Claimed Co",
                "title": "Engineer",
                "url": "https://community.example.org/post/abc",
            },
            self.taxonomies,
            as_of="2026-09-07",
        )
        self.assertNotEqual(unverified["canonicality"], "CANONICAL")
        self.assertNotEqual(unverified["evidence_confidence"], "High")
        self.assertEqual(unverified["canonical_url"], "")
        ats_shaped = get_source_adapter("official", self.registry).normalize(
            {
                "source": "official",
                "manual_ingest": True,
                "company": "Claimed Co",
                "title": "Engineer",
                "url": "https://claimed.wd5.myworkdayjobs.com/en-US/jobs/job/123",
            },
            self.taxonomies,
            as_of="2026-09-07",
        )
        self.assertEqual(ats_shaped["ats_family"], "Workday")
        self.assertEqual(ats_shaped["canonicality"], "SECONDARY")
        self.assertNotEqual(ats_shaped["evidence_confidence"], "High")

    def test_feishu_schema_mismatch_causes_zero_writes(self) -> None:
        config = FeishuConfig(
            app_id="app",
            app_secret="secret",
            app_token="token",
            table_id="table",
            view_id="",
            fields={"company": "Company", "title": "Title"},
            defaults={},
            attachments={},
            sync={},
            alerts={},
        )
        with patch("scripts.update_feishu.feishu_request") as request:
            with self.assertRaises(FeishuAPIError):
                sync_records(
                    config,
                    "token",
                    [{"company": "Acme", "title": "Engineer"}],
                    {},
                    {"Company": {"type": 2}},
                    dry_run=False,
                )
        request.assert_not_called()

    def test_windows_evidence_paths_are_serialized_as_posix(self) -> None:
        value = relative_evidence_path(
            PureWindowsPath(r"C:\repo\src\app.py"),
            PureWindowsPath(r"C:\repo"),
        )
        self.assertEqual(value, "src/app.py")

    def test_mutable_manual_inbox_is_ignored(self) -> None:
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("data/job_cache/manual_job_inputs.txt", ignored)


if __name__ == "__main__":
    unittest.main()
