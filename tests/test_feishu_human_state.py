from __future__ import annotations

import unittest
from types import SimpleNamespace

from scripts.merge_job_sources import apply_human_state
from scripts.run_daily_pipeline import build_commands


class FeishuHumanStateTests(unittest.TestCase):
    def test_url_state_overrides_lifecycle_only(self) -> None:
        job = {
            "company": "Acme",
            "title": "后端开发",
            "location": "深圳",
            "official_url": "https://example.test/jobs/1?utm_source=x",
            "canonical_key": "url:https://example.test/jobs/1",
            "status": "OPEN",
            "action": "WATCH",
            "description": "must remain",
        }
        state = [{
            "official_url": "https://example.test/jobs/1",
            "human_fields": {
                "stage": "APPLIED",
                "applied_at": "2026-09-23T01:00:00+00:00",
                "next_action": "等待笔试",
            },
        }]
        result = apply_human_state([job], state)[0]
        self.assertEqual(result["stage"], "APPLIED")
        self.assertEqual(result["action"], "WATCH")  # omitted state does not erase a value
        self.assertEqual(result["next_action"], "等待笔试")
        self.assertEqual(result["status"], "OPEN")
        self.assertEqual(result["description"], "must remain")
        self.assertEqual(result["human_decision_source"], "feishu_readback")

    def test_no_url_identity_state_overrides_watch(self) -> None:
        job = {
            "company": "Robot Co",
            "title": "测试开发工程师",
            "location": "深圳",
            "canonical_key": "manual:abc",
            "action": "WATCH",
            "status": "",
        }
        state = [{
            "company": "Robot Co",
            "title": "测试开发工程师",
            "location": "深圳",
            "human_fields": {"stage": "APPLIED", "action": "APPLIED"},
        }]
        result = apply_human_state([job], state)[0]
        self.assertEqual(result["stage"], "APPLIED")
        self.assertEqual(result["action"], "APPLIED")
        self.assertEqual(result["status"], "")

    def test_legacy_applied_action_is_not_downgraded_without_stage(self) -> None:
        job = {"company": "Acme", "title": "测试", "location": "深圳", "action": "WATCH"}
        result = apply_human_state([job], [{
            "company": "Acme", "title": "测试", "location": "深圳",
            "human_fields": {"action": "APPLIED"},
        }])[0]
        self.assertEqual(result["stage"], "APPLIED")
        self.assertEqual(result["action"], "APPLIED")

    def test_apply_pipeline_refreshes_readback_before_merge(self) -> None:
        args = SimpleNamespace(
            acceptance_output_root="",
            batch_index=-1,
            as_of="2026-09-23",
            skip_repo_sync=True,
            skip_fetch=True,
            skip_documents=True,
            apply_feishu=True,
            mode="daily",
            config="config/targets.yaml",
            feishu_config="config/feishu.yaml",
            execution_config="config/execution.yaml",
            source_registry="config/company_registry.yaml",
            taxonomy_config="config/taxonomies.yaml",
            manual_agent_input="",
            public_web_query_plan="",
            semantic_review_job_key=[],
            semantic_review_dry_run=False,
        )
        targets = {"job_search": {"sources": ["official"]}, "gpt_discovery": {"enabled": False}}
        commands = build_commands(args, targets, "python3", {"sources": {}})
        reader_index = next(i for i, command in enumerate(commands) if "feishu_human_state.py" in " ".join(command))
        merge_index = next(i for i, command in enumerate(commands) if "merge_job_sources.py" in " ".join(command))
        self.assertLess(reader_index, merge_index)
        self.assertIn("--human-state", commands[merge_index])


if __name__ == "__main__":
    unittest.main()
