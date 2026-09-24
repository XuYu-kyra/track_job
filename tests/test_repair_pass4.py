from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.common import load_config
from scripts.merge_job_sources import merge_jobs
from scripts.source_adapters import load_source_registry


ROOT = Path(__file__).resolve().parents[1]


class RepairPass4Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.taxonomies = load_config(ROOT / "config/taxonomies.yaml")
        self.registry = load_source_registry(ROOT / "config/company_registry.example.yaml")
        self.canonical = "https://candidate.wd5.myworkdayjobs.com/job/REQ-444"
        self.official = {
            "source": "official",
            "company": "Acme",
            "title": "2027 Backend Software Engineer",
            "location": "深圳",
            "job_id": "OFF-444",
            "url": self.canonical,
            "canonical_authority": "OFFICIAL_MONITOR",
            "verification_level": "CANONICAL",
            "observation_origin": "LIVE_FETCH",
            "observed_at": "2026-09-08",
            "verified_at": "2026-09-08",
        }
        self.provisional = [
            {
                "source": "gpt_web",
                "company": "Acme",
                "title": "Graduate Platform Developer",
                "location": "Shenzhen",
                "source_url": "https://www.nowcoder.com/jobs/detail/444",
                "canonical_url": self.canonical,
            },
            {
                "source": "manual",
                "manual_ingest": True,
                "company": "Acme",
                "title": "Campus Backend Role",
                "location": "深圳",
                "url": "https://community.example.org/jobs/444",
                "canonical_url": self.canonical,
            },
            {
                "source": "linkedin",
                "company": "Acme",
                "title": "Software Engineer",
                "location": "Shenzhen China",
                "job_id": "LI-444",
                "url": "https://www.linkedin.com/jobs/view/444",
                "canonical_url": self.canonical,
            },
        ]

    def _merge(self, records: list[dict]) -> list[dict]:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "jobs.json"
            source.write_text(json.dumps(records), encoding="utf-8")
            return merge_jobs(
                [str(source)],
                taxonomies=self.taxonomies,
                source_registry=self.registry,
                as_of="2026-09-08",
            )

    def test_canonical_dedup_is_independent_of_official_source_position(self) -> None:
        expected_identity = "url:https://candidate.wd5.myworkdayjobs.com/job/REQ-444"
        for official_index in range(4):
            with self.subTest(official_position=official_index + 1):
                ordered = list(self.provisional)
                ordered.insert(official_index, dict(self.official))
                merged = self._merge(ordered)
                self.assertEqual(len(merged), 1)
                self.assertEqual(merged[0]["canonical_key"], expected_identity)
                self.assertEqual(
                    set(merged[0]["sources"]),
                    {"official", "gpt_web", "manual", "linkedin"},
                )
                self.assertGreaterEqual(len(merged[0]["source_observations"]), 4)

    def test_canonical_consolidation_keeps_unrelated_stable_groups_separate(self) -> None:
        unrelated = [
            {
                "source": "linkedin",
                "company": "Acme",
                "title": "Infrastructure Engineer",
                "location": "北京",
                "job_id": "42",
                "url": "https://www.linkedin.com/jobs/view/42",
            },
            {
                "source": "nowcoder",
                "company": "Acme",
                "title": "Infrastructure Engineer",
                "location": "北京",
                "job_id": "42",
                "url": "https://www.nowcoder.com/jobs/detail/42",
            },
        ]
        merged = self._merge([*self.provisional, *unrelated, self.official])
        self.assertEqual(len(merged), 3)
        canonical_jobs = [job for job in merged if job["canonical_key"].startswith("url:")]
        self.assertEqual(len(canonical_jobs), 1)
        stable_keys = {
            job["canonical_key"] for job in merged if job["canonical_key"].startswith("job_id:")
        }
        self.assertEqual(len(stable_keys), 2)


if __name__ == "__main__":
    unittest.main()
