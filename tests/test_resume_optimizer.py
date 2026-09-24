from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from scripts.generate_resume import (
    load_matching_resume_optimizer_edit,
    load_resume_materials,
)
from scripts.resume_optimizer import (
    ResumeOptimizerValidationError,
    apply_resume_optimizer_edits,
)
from scripts.resume_v2 import build_resume_content_plan, plan_bullet_intents, render_resume_from_plan


ROOT = Path(__file__).resolve().parents[1]
AI_JOB = {
    "company": "深圳市金证科技股份有限公司",
    "title": "大模型应用开发工程师",
    "location": "深圳市",
    "role_family": "ai_application",
    "description": "模型接入、Prompt 与 FastAPI 服务端",
}


class ResumeOptimizerAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.materials = load_resume_materials(ROOT / "cv/materials")
        cls.template = (ROOT / "cv/resume.tex").read_text(encoding="utf-8")

    def setUp(self) -> None:
        self.plan = build_resume_content_plan(AI_JOB, self.materials)
        self.intent = plan_bullet_intents(self.plan)[0]
        self.original = self.intent["variants"]["CN"][self.intent["variant"]]

    def test_existing_edit_cache_requires_exact_job_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = {"job_identity": dict(AI_JOB), "edits": []}
            payload["job_identity"].pop("role_family")
            payload["job_identity"].pop("description")
            Path(tmp, "match.json").write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            self.assertEqual(
                load_matching_resume_optimizer_edit(tmp, AI_JOB),
                payload,
            )
            other = dict(AI_JOB)
            other["location"] = "北京"
            self.assertEqual(load_matching_resume_optimizer_edit(tmp, other), {})

    def _record(self, **changes):
        record = {
            "section": "experience",
            "material_id": self.intent["material_id"],
            "intent_id": self.intent["intent_id"],
            "evidence_ids": list(self.intent["evidence_ids"]),
            "original_text": self.original,
            "action": "KEEP",
            "revised_text": self.original,
            "reason": "原句已经具体且有证据。",
            "jd_keywords_covered": [],
            "new_fact_introduced": False,
            "estimated_cjk_delta": 0,
            "ai_style_risk": "LOW",
        }
        record.update(changes)
        return record

    def _payload(self, record):
        return {"job_identity": deepcopy(AI_JOB), "edits": [record]}

    def test_valid_keep_record_preserves_grounded_intent(self) -> None:
        edited = apply_resume_optimizer_edits(
            self.plan, self._payload(self._record()), job=AI_JOB
        )
        result = plan_bullet_intents(edited)[0]
        self.assertEqual(result["evidence_ids"], self.intent["evidence_ids"])
        self.assertEqual(result["variants"]["CN"][result["variant"]], self.original)

    def test_new_fact_flag_is_rejected(self) -> None:
        with self.assertRaisesRegex(ResumeOptimizerValidationError, "introduces a new fact"):
            apply_resume_optimizer_edits(
                self.plan,
                self._payload(self._record(new_fact_introduced=True)),
                job=AI_JOB,
            )

    def test_unknown_evidence_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(ResumeOptimizerValidationError, "unknown evidence"):
            apply_resume_optimizer_edits(
                self.plan,
                self._payload(self._record(evidence_ids=["MADE_UP_99"])),
                job=AI_JOB,
            )

    def test_unsupported_numeric_claim_is_rejected(self) -> None:
        revised = self.original + "，覆盖 9999 名用户。"
        with self.assertRaisesRegex(ResumeOptimizerValidationError, "unsupported numeric claims"):
            apply_resume_optimizer_edits(
                self.plan,
                self._payload(
                    self._record(
                        action="EXPAND",
                        revised_text=revised,
                        estimated_cjk_delta=5,
                    )
                ),
                job=AI_JOB,
            )

    def test_project_context_is_grounded_and_rendered_without_metadata(self) -> None:
        project = self.plan["projects"][0]
        material = project["material"]
        evidence_ids = list(material["source_evidence_ids"][:2])
        context = "面向历史人物问答场景，串联检索、上下文与模型接口。"
        record = {
            "section": "project_context",
            "material_id": material["id"],
            "intent_id": f"{material['id']}_context",
            "evidence_ids": evidence_ids,
            "original_text": "",
            "action": "EXPAND",
            "revised_text": context,
            "reason": "补充项目上下文。",
            "jd_keywords_covered": ["模型接入"],
            "new_fact_introduced": False,
            "estimated_cjk_delta": 22,
            "ai_style_risk": "LOW",
        }
        edited = apply_resume_optimizer_edits(self.plan, self._payload(record), job=AI_JOB)
        rendered = render_resume_from_plan(self.template, edited, "CN")
        body = rendered.split("\\begin{document}", 1)[1]
        self.assertIn(context, body)
        self.assertNotIn("evidence_id", body)
        self.assertNotIn("source_class", body)

    def test_forbidden_unsupported_jd_term_is_rejected(self) -> None:
        revised = self.original + "，实现多 Agent 任务规划。"
        payload = self._payload(
            self._record(
                action="EXPAND",
                revised_text=revised,
                estimated_cjk_delta=7,
            )
        )
        payload["forbidden_unsupported_terms"] = ["多 Agent"]
        with self.assertRaisesRegex(ResumeOptimizerValidationError, "unsupported JD terms"):
            apply_resume_optimizer_edits(self.plan, payload, job=AI_JOB)


if __name__ == "__main__":
    unittest.main()
