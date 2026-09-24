from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from scripts.common import load_config
from scripts.fetch_official import parse_career_page
from scripts.import_gpt_discovery import import_gpt_candidates
from scripts.job_schema import ACTIVE_PROCESS_STAGES, action_for_stage
from scripts.merge_job_sources import merge_jobs
from scripts.privacy_check import inspect_git_history, inspect_git_index_contents
from scripts.score_jobs import evaluate_job
from scripts.setup_feishu import compare_schema, configured_specs
from scripts.source_adapters import get_source_adapter, load_source_registry
from scripts.update_feishu import load_feishu_config


ROOT = Path(__file__).resolve().parents[1]


class RepairPass3Tests(unittest.TestCase):
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

    def _official(self, url: str, **overrides: object) -> dict:
        record: dict[str, object] = {
            "source": "official",
            "company": "Acme",
            "title": "2027届 Software Engineer",
            "location": "深圳",
            "url": url,
            "canonical_authority": "OFFICIAL_MONITOR",
            "verification_level": "CANONICAL",
            "observation_origin": "LIVE_FETCH",
            "observed_at": "2026-09-07",
            "verified_at": "2026-09-07",
        }
        record.update(overrides)
        return record

    def test_semantic_li_job_listing_contexts_are_isolated(self) -> None:
        jobs = parse_career_page(
            """
            <ul class="jobs-list">
              <li class="job-listing">
                <span>2027 campus</span><span>Shenzhen</span>
                <a href="/jobs/campus">Software Engineer</a>
              </li>
              <li class="job-listing">
                <span>Social recruitment</span><span>Beijing</span>
                <a href="/jobs/social">Senior Engineer</a>
              </li>
            </ul>
            """,
            company="Example",
            career_url="https://careers.example.com/",
            location="",
            company_type="technology",
            aliases=[],
            graduation_terms=["2027", "campus"],
            max_links=10,
            verified_on="2026-09-07",
        )
        by_title = {job.position: job for job in jobs}
        self.assertEqual(by_title["Software Engineer"].location, "Shenzhen")
        self.assertIn("2027", by_title["Software Engineer"].campaign_context)
        self.assertEqual(by_title["Senior Engineer"].location, "Beijing")
        self.assertNotIn("2027", by_title["Senior Engineer"].campaign_context)
        self.assertNotIn("Shenzhen", by_title["Senior Engineer"].page_context)

    def test_nested_same_tag_context_captures_metadata_after_anchor(self) -> None:
        jobs = parse_career_page(
            """
            <div class="position-listing" data-requisition-id="REQ-42">
              <div class="metadata"><div>2027届 校园招聘</div></div>
              <div><a href="/positions/42">Infrastructure Engineer</a></div>
              <div class="metadata"><div>工作地点：深圳</div></div>
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
        self.assertEqual(jobs[0].location, "深圳")
        self.assertIn("2027", jobs[0].campaign_context)
        self.assertIn("工作地点：深圳", jobs[0].page_context)

    def test_page_campaign_does_not_certify_unrelated_list_item(self) -> None:
        jobs = parse_career_page(
            """
            <h1>2027校园招聘</h1>
            <div class="openings-list"><ul>
              <li><a href="/opening/graduate">Graduate Engineer</a><span>2027届 深圳</span></li>
              <li><a href="/opening/senior">Senior Engineer</a><span>社会招聘 北京 5年经验</span></li>
            </ul></div>
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
        by_title = {job.position: job for job in jobs}
        self.assertIn("2027", by_title["Graduate Engineer"].campaign_context)
        self.assertNotIn("2027", by_title["Senior Engineer"].campaign_context)
        self.assertEqual(by_title["Senior Engineer"].location, "北京")

    def test_every_active_stage_maps_to_lifecycle_action_after_full_path(self) -> None:
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
                    "status": "OPEN",
                    "stage": stage,
                }]
                merged = self._merge([], previous=previous)
                self.assertEqual(merged[0]["freshness"], "stale")
                evaluated = evaluate_job(
                    merged[0],
                    self.scoring,
                    self.targets,
                    self.taxonomies,
                    {"skills": []},
                    as_of="2026-09-07",
                )
                self.assertEqual(evaluated["action"], action_for_stage(stage))
                self.assertNotIn(
                    evaluated["action"], {"WATCH", "HOLD", "READY", "MUST_APPLY"}
                )

    def test_source_namespaced_identity_hierarchy(self) -> None:
        canonical = "https://candidate.wd5.myworkdayjobs.com/job/REQ-42"
        same_canonical = self._merge([
            self._official(canonical, job_id="OFF-1"),
            {
                "source": "linkedin",
                "company": "Acme",
                "title": "Graduate Developer",
                "location": "Shenzhen China",
                "job_id": "LI-999",
                "url": "https://www.linkedin.com/jobs/view/999",
                "canonical_url": canonical,
            },
        ])
        self.assertEqual(len(same_canonical), 1)

        cross_source_collision = self._merge([
            {"source": "linkedin", "company": "Acme", "title": "Engineer", "location": "深圳", "job_id": "42", "url": "https://www.linkedin.com/jobs/view/42"},
            {"source": "nowcoder", "company": "Acme", "title": "Engineer", "location": "深圳", "job_id": "42", "url": "https://www.nowcoder.com/jobs/detail/42"},
        ])
        self.assertEqual(len(cross_source_collision), 2)

        same_source = self._merge([
            {"source": "linkedin", "company": "Acme", "title": "Engineer", "location": "深圳", "job_id": "42", "url": "https://www.linkedin.com/jobs/view/42"},
            {"source": "linkedin", "company": "Acme", "title": "Software Engineer", "location": "Shenzhen", "job_id": "42", "url": "https://www.linkedin.com/jobs/view/42?ref=feed"},
        ])
        self.assertEqual(len(same_source), 1)

        distinct_ids = self._merge([
            {"source": "linkedin", "company": "Acme", "title": "Engineer", "location": "深圳", "job_id": "41", "url": "https://www.linkedin.com/jobs/view/41"},
            {"source": "linkedin", "company": "Acme", "title": "Engineer", "location": "深圳", "job_id": "42", "url": "https://www.linkedin.com/jobs/view/42"},
        ])
        self.assertEqual(len(distinct_ids), 2)

        cohorts = self._merge([
            {"source": "manual", "company": "Acme", "title": "2026届 Software Engineer", "location": "深圳", "url": "https://community.example.org/a"},
            {"source": "manual", "company": "Acme", "title": "2027届 Software Engineer", "location": "深圳", "url": "https://community.example.org/b"},
        ])
        self.assertEqual(len(cohorts), 2)

    def test_four_sources_merge_only_after_trusted_canonical_proof(self) -> None:
        canonical = "https://candidate.wd5.myworkdayjobs.com/job/REQ-88"
        records = [
            self._official(canonical),
            {"source": "gpt_web", "company": "Acme", "title": "Graduate Developer", "location": "深圳", "source_url": "https://www.nowcoder.com/jobs/detail/88", "canonical_url": canonical},
            {"source": "linkedin", "company": "Acme", "title": "Software Engineer", "location": "Shenzhen", "url": "https://www.linkedin.com/jobs/view/88", "canonical_url": canonical},
            {"source": "manual", "manual_ingest": True, "company": "Acme", "title": "Backend Role", "location": "深圳", "url": "https://community.example.org/88", "canonical_url": canonical},
        ]
        merged = self._merge(records)
        self.assertEqual(len(merged), 1)
        self.assertTrue({"official", "gpt_web", "linkedin", "manual"}.issubset(merged[0]["sources"]))
        self.assertGreaterEqual(len(merged[0]["source_observations"]), 4)

    def test_observation_origins_control_timestamps(self) -> None:
        imported, errors = import_gpt_candidates(
            [{
                "company": "Timestamp Co",
                "title": "2027届 Engineer",
                "location": "深圳",
                "source_url": "https://www.nowcoder.com/jobs/detail/700",
            }],
            imported_on="2026-09-07",
        )
        self.assertEqual(errors, [])
        gpt = self._merge(imported)[0]
        self.assertEqual(gpt["first_seen"], "2026-09-07")
        self.assertEqual(gpt["last_observed"], "")
        self.assertEqual(gpt["last_verified"], "")
        self.assertEqual(gpt["observation_origin"], "CACHE_REPLAY")

        manual = self._merge([{
            "source": "manual",
            "manual_ingest": True,
            "company": "Manual Co",
            "title": "2027届 Engineer",
            "location": "深圳",
            "url": "https://community.example.org/manual-700",
        }])[0]
        self.assertEqual(manual["first_seen"], "2026-09-07")
        self.assertEqual(manual["last_observed"], "")
        self.assertEqual(manual["last_verified"], "")
        self.assertEqual(manual["observation_origin"], "MANUAL_IMPORT")

        first_live = self._merge([{
            "source": "linkedin",
            "company": "Live Co",
            "title": "2027届 Engineer",
            "location": "深圳",
            "job_id": "700",
            "url": "https://www.linkedin.com/jobs/view/700",
            "observation_origin": "LIVE_FETCH",
            "observed_at": "2026-09-01",
        }], as_of="2026-09-01")
        refetched = self._merge([{
            "source": "linkedin",
            "company": "Live Co",
            "title": "2027届 Engineer",
            "location": "深圳",
            "job_id": "700",
            "url": "https://www.linkedin.com/jobs/view/700",
            "observation_origin": "LIVE_FETCH",
            "observed_at": "2026-09-07",
        }], previous=first_live)
        self.assertEqual(refetched[0]["last_observed"], "2026-09-07")

        replayed = self._merge([{
            "source": "linkedin",
            "company": "Live Co",
            "title": "2027届 Engineer",
            "location": "深圳",
            "job_id": "700",
            "url": "https://www.linkedin.com/jobs/view/700",
            "observation_origin": "CACHE_REPLAY",
        }], previous=first_live)
        self.assertEqual(replayed[0]["last_observed"], "2026-09-01")

        verified = self._merge([
            self._official(
                "https://jobs.acme.example/700",
                company="Official Timestamp Co",
                observed_at="2026-09-07",
                verified_at="2026-09-07",
            )
        ])[0]
        self.assertEqual(verified["last_observed"], "2026-09-07")
        self.assertEqual(verified["last_verified"], "2026-09-07")

    def test_feishu_contract_is_exact_not_subset_compatible(self) -> None:
        config = load_feishu_config(str(ROOT / "config/feishu.example.yaml"))
        specs = configured_specs(config)
        exact = deepcopy(specs)
        self.assertEqual(compare_schema(specs, exact), ([], []))

        extra_field = deepcopy(exact)
        extra_field["Rogue Field"] = {"field_name": "Rogue Field", "type": 1}
        self.assertTrue(compare_schema(specs, extra_field)[1])

        rogue_option = deepcopy(exact)
        rogue_option["Action"]["property"]["options"].append({"name": "ROGUE_OPTION"})
        self.assertTrue(any("unexpected options" in item for item in compare_schema(specs, rogue_option)[1]))

        missing_option = deepcopy(exact)
        missing_option["Action"]["property"]["options"].pop()
        self.assertTrue(any("missing options" in item for item in compare_schema(specs, missing_option)[1]))

        removed = deepcopy(exact)
        removed.pop("Title")
        self.assertTrue(compare_schema(specs, removed)[0])

        renamed = deepcopy(exact)
        renamed["Renamed Title"] = renamed.pop("Title")
        missing, mismatches = compare_schema(specs, renamed)
        self.assertTrue(missing)
        self.assertTrue(mismatches)

        wrong_type = deepcopy(exact)
        wrong_type["Company"]["type"] = 2
        self.assertTrue(any("expected type" in item for item in compare_schema(specs, wrong_type)[1]))

    def test_ats_family_never_grants_canonical_authority(self) -> None:
        supplied_url = "https://fake.myworkdayjobs.com/en-US/jobs/job/DOES-NOT-EXIST"
        imported, errors = import_gpt_candidates(
            [{
                "source": "gpt_web",
                "company": "Spoof Co",
                "title": "2027届 Engineer",
                "location": "深圳",
                "source_url": "https://community.example.org/spoof",
                "canonical_url": supplied_url,
                "canonical_source": "official_workday",
                "canonicality": "CANONICAL",
                "source_verified": True,
                "source_authoritative": True,
                "evidence_confidence": "High",
                "discovered_by": ["official_monitor"],
                "discovery_sources": ["official"],
            }],
            imported_on="2026-09-07",
        )
        self.assertEqual(errors, [])
        gpt = get_source_adapter("gpt_web", self.registry).normalize(
            imported[0], self.taxonomies, as_of="2026-09-07"
        )
        self.assertEqual(gpt["ats_family"], "Workday")
        self.assertEqual(gpt["canonicality"], "SECONDARY")
        self.assertEqual(gpt["canonical_url"], "")
        self.assertEqual(gpt["evidence_confidence"], "Medium")
        self.assertEqual(gpt["discovered_by"], ["gpt_web"])

        merged = self._merge([imported[0], self._official(supplied_url, company="Spoof Co")])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["canonicality"], "CANONICAL")
        self.assertEqual(merged[0]["evidence_confidence"], "High")
        self.assertIn("gpt_web", merged[0]["discovered_by"])

    @staticmethod
    def _git(repo: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-c", f"safe.directory={repo}", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    def _init_repo(self, repo: Path) -> None:
        self._git(repo, "init", "--quiet")
        self._git(repo, "config", "user.name", "Privacy Test")
        self._git(repo, "config", "user.email", "you@example.com")
        (repo / "README.md").write_text("clean fixture\n", encoding="utf-8")
        self._git(repo, "add", "README.md")
        self._git(repo, "commit", "--quiet", "-m", "clean")

    def test_privacy_inspects_index_and_reachable_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._init_repo(repo)
            index_findings, index_error = inspect_git_index_contents(repo)
            history_findings, warnings, history_error = inspect_git_history(repo)
            self.assertEqual((index_findings, index_error), ([], ""))
            self.assertEqual((history_findings, warnings, history_error), ([], [], ""))

            credential = "sk-" + ("A" * 32)
            (repo / ".env").write_text(f"OPENAI_API_KEY={credential}\n", encoding="utf-8")
            self._git(repo, "add", ".env")
            index_findings, index_error = inspect_git_index_contents(repo)
            self.assertEqual(index_error, "")
            self.assertTrue(any("OpenAI credential" in item for item in index_findings))

            self._git(repo, "commit", "--quiet", "-m", "secret")
            (repo / ".env").write_text("SAFE_PLACEHOLDER=1\n", encoding="utf-8")
            self._git(repo, "add", ".env")
            self._git(repo, "commit", "--quiet", "-m", "clean current")
            index_findings, _ = inspect_git_index_contents(repo)
            history_findings, _, history_error = inspect_git_history(repo)
            self.assertEqual(index_findings, [])
            self.assertEqual(history_error, "")
            self.assertTrue(any("OpenAI credential" in item for item in history_findings))

    def test_historical_public_manual_inbox_is_warning_not_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._init_repo(repo)
            inbox = repo / "data/job_cache/manual_job_inputs.txt"
            inbox.parent.mkdir(parents=True)
            inbox.write_text(
                "https://www.linkedin.com/jobs/view/123456789 | Public Co | Engineer\n",
                encoding="utf-8",
            )
            self._git(repo, "add", "data/job_cache/manual_job_inputs.txt")
            self._git(repo, "commit", "--quiet", "-m", "historical public inbox")
            self._git(repo, "rm", "--quiet", "data/job_cache/manual_job_inputs.txt")
            self._git(repo, "commit", "--quiet", "-m", "remove inbox")
            findings, warnings, error = inspect_git_history(repo)
            self.assertEqual(error, "")
            self.assertEqual(findings, [])
            self.assertTrue(any("manual_job_inputs.txt" in item for item in warnings))


if __name__ == "__main__":
    unittest.main()
