from __future__ import annotations

import unittest
from copy import deepcopy

from scripts.common import load_config
from scripts.linkedin_location import linkedin_location_scope
from scripts.query_matrix import build_query_matrix
from scripts.score_jobs import eligibility_gate


ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


class LinkedInScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.targets = load_config(ROOT / "config/targets.example.yaml")
        self.taxonomies = load_config(ROOT / "config/taxonomies.yaml")
        self.scoring = load_config(ROOT / "config/scoring.yaml")

    def test_target_and_non_target_location_classification(self) -> None:
        self.assertEqual(linkedin_location_scope("Shenzhen, Guangdong, China")["status"], "TARGET")
        self.assertEqual(linkedin_location_scope("Hong Kong SAR")["region"], "Hong Kong")
        self.assertEqual(linkedin_location_scope("London, England, United Kingdom")["status"], "TARGET")
        self.assertEqual(linkedin_location_scope("Austin, TX")["status"], "OUT_OF_SCOPE")
        self.assertEqual(linkedin_location_scope("Remote")["status"], "UNCERTAIN")
        self.assertEqual(linkedin_location_scope("London")["status"], "UNCERTAIN")

    def test_linkedin_matrix_reserves_all_configured_regions(self) -> None:
        matrix = build_query_matrix(self.targets, self.taxonomies, mode="daily", source="linkedin")
        self.assertEqual(len(matrix), 24)
        locations = {item["location"] for item in matrix}
        self.assertTrue({"Shenzhen", "Hong Kong SAR", "United Kingdom"}.issubset(locations))

    def test_other_source_keeps_global_regions(self) -> None:
        targets = deepcopy(self.targets)
        targets["job_search"].pop("linkedin_search", None)
        matrix = build_query_matrix(targets, self.taxonomies, mode="daily", source="nowcoder")
        self.assertEqual({item["location"] for item in matrix}, {"Shenzhen", "深圳"})

    def test_direct_linkedin_scope_gate_does_not_narrow_other_sources(self) -> None:
        base = {
            "company": "Example",
            "title": "2027 Software Engineer",
            "location": "London, England, United Kingdom",
            "source": "linkedin",
            "sources": ["linkedin"],
            "role_family": "general_software",
            "description": "2027 graduate software engineer",
        }
        eligibility, reason = eligibility_gate(base, self.scoring, self.targets)
        self.assertNotEqual((eligibility, reason), ("REJECT", "location / commute"))
        out = dict(base, location="Austin, TX")
        self.assertEqual(eligibility_gate(out, self.scoring, self.targets), ("REJECT", "linkedin_location_out_of_scope"))
        merged = dict(out, source="official", sources=["official", "linkedin"], location="Shenzhen, China")
        self.assertNotEqual(eligibility_gate(merged, self.scoring, self.targets)[0], "REJECT")


if __name__ == "__main__":
    unittest.main()
