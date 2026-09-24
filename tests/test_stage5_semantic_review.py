from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import requests

from scripts.common import load_config, read_json, write_json
from scripts.import_gpt_discovery import prepare_gpt_candidate
from scripts.import_manual_jobs import infer_job_id, parse_manual_line
from scripts.job_schema import extract_stable_job_id, merge_job_records, normalize_job
from scripts.run_daily_pipeline import build_commands
from scripts.score_jobs import eligibility_gate, graduation_evidence
from scripts.semantic_review import (
    CONFIDENCE_VALUES,
    DECISION_KEYS,
    DeepSeekSemanticJobReviewProvider,
    EXPERIENCE_DECISIONS,
    GRADUATION_DECISIONS,
    LANGUAGE_VALUES,
    LOCATION_DECISIONS,
    PROFILE_VALUES,
    RANKING_ITEM_KEYS,
    RESULT_KEYS,
    SKILL_ITEM_KEYS,
    STATUS_DECISIONS,
    SemanticJobReviewProvider,
    SemanticReviewAPIError,
    SemanticReviewConfigurationError,
    SemanticReviewProviderOutputError,
    SemanticReviewValidationError,
    build_review_input,
    deterministic_location_decision,
    deterministic_semantic_conflicts,
    parse_strict_json_result,
    prepare_candidate_evidence,
    run_semantic_review_batch,
    semantic_review_example,
    semantic_review_json_schema,
    select_jobs_for_review,
    validate_review_input,
    validate_semantic_result,
)
from scripts.validate_config import validate_configuration


ROOT = Path(__file__).resolve().parents[1]


def valid_result() -> dict:
    return {
        "schema_version": "1.0",
        "graduation": {
            "decision": "ELIGIBLE",
            "confidence": "HIGH",
            "evidence": ["面向2027届毕业生"],
        },
        "location": {
            "decision": "SHENZHEN_ALLOWED",
            "confidence": "HIGH",
            "evidence": ["工作地点：深圳"],
        },
        "experience_requirement": {
            "decision": "NEW_GRAD_COMPATIBLE",
            "confidence": "HIGH",
            "evidence": ["校园招聘"],
        },
        "job_status": {
            "decision": "OPEN",
            "confidence": "MEDIUM",
            "evidence": ["立即申请"],
        },
        "primary_role_family": "test_development",
        "secondary_role_tags": ["robotics"],
        "must_have_skills": [{"skill": "Python", "evidence": "熟悉Python"}],
        "preferred_skills": [],
        "core_responsibilities": ["开发自动化测试工具"],
        "resume_focus": ["测试自动化"],
        "candidate_evidence_ranking": [
            {"evidence_id": "TEST_AUTOMATION_01", "relevance": "HIGH", "reason": "直接相关"}
        ],
        "recommended_application_profile": "CN",
        "recommended_resume_language": "BOTH",
        "uncertainties": [],
    }


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class FakeSession:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append(deepcopy({"url": url, **kwargs}))
        if not self.outcomes:
            raise AssertionError("unexpected extra provider call")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # type: ignore[return-value]


class FakeProvider(SemanticJobReviewProvider):
    provider_name = "fake-deepseek"

    def __init__(self, result: dict | None = None) -> None:
        self.calls = 0
        self.result = result or valid_result()

    @property
    def model(self) -> str:
        return "deepseek-v4-flash"

    def review(
        self,
        review_input: dict,
        *,
        schema_version: str,
        evidence_ids: set[str],
    ) -> tuple[dict, dict[str, int]]:
        self.calls += 1
        return deepcopy(self.result), {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}


class Stage5SemanticReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.targets = load_config(ROOT / "config/targets.example.yaml")
        self.scoring = load_config(ROOT / "config/scoring.yaml")
        self.taxonomies = load_config(ROOT / "config/taxonomies.yaml")
        self.registry = load_config(ROOT / "config/company_registry.example.yaml")
        self.evidence = prepare_candidate_evidence(
            [
                {
                    "evidence_id": "TEST_AUTOMATION_01",
                    "project": "ANON_TEST_PROJECT",
                    "category": "test automation",
                    "factual_description": "Built deterministic API test automation.",
                    "technologies": ["Python", "pytest"],
                    "verified_metrics": ["20 regression cases"],
                    "results": ["repeatable local validation"],
                    "provenance": ["repository:ANON_TEST_PROJECT:tests/test_api.py"],
                    "suitable_role_families": ["test_development", "general_software"],
                    "confidence": "HIGH",
                }
            ]
        )
        self.assertEqual(
            self.evidence[0]["provenance"],
            ["repository:ANON_TEST_PROJECT:tests/test_api.py"],
        )
        self.assertEqual(
            self.evidence[0]["suitable_role_families"],
            ["test_development", "general_software"],
        )

    def _graduation(self, text: str) -> tuple[str, str, list[str]]:
        return graduation_evidence(
            {"title": "Backend Engineer", "description": text},
            self.scoring,
            self.targets,
        )

    _DEFAULT_CONTENT = object()

    def _response(
        self,
        result: dict | None = None,
        *,
        content: object = _DEFAULT_CONTENT,
        model: str = "deepseek-v4-flash",
        response_status: str = "completed",
        reasoning_content: str | None = None,
        usage: dict | None = None,
        status_code: int = 200,
        incomplete_details: dict | None = None,
        error: dict | None = None,
        include_message: bool = True,
    ) -> FakeResponse:
        resolved_content = (
            json.dumps(result or valid_result())
            if content is self._DEFAULT_CONTENT
            else content
        )
        output = []
        if reasoning_content is not None:
            output.append(
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": reasoning_content}],
                }
            )
        if include_message:
            message_content = []
            if resolved_content is not None:
                message_content.append(
                    {"type": "output_text", "text": resolved_content}
                )
            output.append(
                {
                    "type": "message",
                    "status": response_status,
                    "role": "assistant",
                    "content": message_content,
                }
            )
        return FakeResponse(
            status_code,
            {
                "model": model,
                "status": response_status,
                "output": output,
                "incomplete_details": incomplete_details,
                "error": error,
                "usage": usage
                or {
                    "input_tokens": 10,
                    "input_tokens_details": {"cached_tokens": 3},
                    "output_tokens": 20,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 30,
                    "total_cost": 0.0012,
                },
            },
        )

    def _provider(
        self,
        session: FakeSession,
        *,
        max_retries: int = 0,
        max_output_tokens: int = 3072,
    ) -> DeepSeekSemanticJobReviewProvider:
        return DeepSeekSemanticJobReviewProvider(
            max_retries=max_retries,
            max_output_tokens=max_output_tokens,
            retry_backoff_seconds=0,
            session=session,
            sleeper=lambda _: None,
        )

    def _call_provider(
        self,
        provider: DeepSeekSemanticJobReviewProvider,
    ) -> tuple[dict, dict]:
        return provider.review(
            build_review_input(
                {
                    "company": "Acme",
                    "title": "Engineer",
                    "description": "面向2027届，工作地点深圳，熟悉Python。",
                },
                self.evidence,
                max_input_chars=10000,
            ),
            schema_version="1.0",
            evidence_ids={"TEST_AUTOMATION_01"},
        )

    def test_missing_api_key_fails_without_fake_result(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SemanticReviewConfigurationError):
                DeepSeekSemanticJobReviewProvider()

    def test_api_key_is_never_in_error_or_output(self) -> None:
        secret = "secret-value-that-must-not-appear"
        session = FakeSession([FakeResponse(401)])
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret}, clear=True):
            provider = DeepSeekSemanticJobReviewProvider(
                max_retries=0,
                session=session,
            )
            captured = io.StringIO()
            with redirect_stdout(captured), redirect_stderr(captured):
                with self.assertRaises(SemanticReviewAPIError) as raised:
                    provider.review(
                        build_review_input(
                            {"company": "Acme", "title": "Engineer"},
                            [],
                            max_input_chars=10000,
                        ),
                        schema_version="1.0",
                        evidence_ids=set(),
                    )
        self.assertEqual(
            session.calls[0]["url"],
            "https://api.deepseek.com/responses",
        )
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(secret, captured.getvalue())

    def test_valid_json_first_call_uses_responses_structured_output(self) -> None:
        session = FakeSession([self._response()])
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "runtime-secret"}, clear=True):
            provider = self._provider(session, max_output_tokens=3072)
            result, metadata = self._call_provider(provider)

        self.assertEqual(len(session.calls), 1)
        request_body = session.calls[0]["json"]
        self.assertEqual(request_body["reasoning"], {"effort": "none"})
        self.assertEqual(request_body["max_output_tokens"], 3072)
        self.assertNotIn("tools", request_body)
        self.assertNotIn("messages", request_body)
        self.assertNotIn("response_format", request_body)
        output_format = request_body["text"]["format"]
        self.assertEqual(output_format["type"], "json_schema")
        self.assertEqual(output_format["name"], "semantic_job_review")
        self.assertEqual(
            output_format["schema"]["properties"]["candidate_evidence_ranking"]
            ["items"]["properties"]["evidence_id"]["enum"],
            ["TEST_AUTOMATION_01"],
        )
        system_prompt = request_body["instructions"]
        user_prompt = request_body["input"]
        self.assertIn("JSON", system_prompt)
        self.assertIn("JSON", user_prompt)
        example = json.loads(system_prompt.splitlines()[1])
        self.assertEqual(validate_semantic_result(example, evidence_ids=set()), example)
        self.assertIn(
            ", ".join(sorted(LOCATION_DECISIONS)),
            system_prompt,
        )
        self.assertNotIn("Acme", system_prompt)
        self.assertNotIn("TEST_AUTOMATION_01", system_prompt)
        self.assertEqual(result["graduation"]["decision"], "ELIGIBLE")
        self.assertEqual(metadata["model"], "deepseek-v4-flash")
        self.assertEqual(metadata["thinking_mode"], "disabled")
        self.assertEqual(metadata["reasoning_effort"], "none")
        self.assertEqual(metadata["response_status"], "completed")
        self.assertEqual(metadata["input_tokens"], 10)
        self.assertEqual(metadata["output_tokens"], 20)
        self.assertEqual(metadata["cached_tokens"], 3)
        self.assertEqual(metadata["reasoning_tokens"], 0)
        self.assertEqual(metadata["retry_count"], 0)

    def test_empty_content_retries_exactly_once_then_succeeds(self) -> None:
        first_usage = {
            "input_tokens": 11,
            "input_tokens_details": {"cached_tokens": 2},
            "output_tokens": 0,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 11,
        }
        second_usage = {
            "input_tokens": 12,
            "input_tokens_details": {"cached_tokens": 4},
            "output_tokens": 20,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 32,
        }
        session = FakeSession(
            [
                self._response(content=None, usage=first_usage),
                self._response(usage=second_usage),
            ]
        )
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "runtime-secret"}, clear=True):
            result, metadata = self._call_provider(
                self._provider(session, max_retries=0)
            )

        self.assertEqual(len(session.calls), 2)
        strengthened = "Return exactly one non-empty JSON object and no surrounding prose."
        self.assertNotIn(strengthened, session.calls[0]["json"]["input"])
        self.assertIn(strengthened, session.calls[1]["json"]["input"])
        self.assertEqual(result["location"]["decision"], "SHENZHEN_ALLOWED")
        self.assertEqual(metadata["input_tokens"], 23)
        self.assertEqual(metadata["output_tokens"], 20)
        self.assertEqual(metadata["total_tokens"], 43)
        self.assertEqual(metadata["cached_tokens"], 6)
        self.assertEqual(metadata["retry_count"], 1)
        self.assertFalse(metadata["attempts"][0]["content_present"])
        self.assertTrue(metadata["attempts"][1]["content_present"])

    def test_empty_content_twice_is_controlled_failure_with_one_retry(self) -> None:
        session = FakeSession(
            [self._response(content=None), self._response(content="")]
        )
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "runtime-secret"}, clear=True):
            provider = self._provider(session, max_retries=2)
            with self.assertRaises(SemanticReviewProviderOutputError) as raised:
                self._call_provider(provider)

        self.assertEqual(len(session.calls), 2)
        diagnostics = raised.exception.diagnostics
        self.assertEqual(diagnostics["retry_count"], 1)
        self.assertFalse(diagnostics["content_present"])
        self.assertFalse(diagnostics["reasoning_content_present"])
        self.assertEqual(len(diagnostics["attempts"]), 2)

    def test_whitespace_content_is_retried_once(self) -> None:
        session = FakeSession(
            [self._response(content=" \n\t "), self._response()]
        )
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "runtime-secret"}, clear=True):
            _, metadata = self._call_provider(self._provider(session))
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(metadata["retry_count"], 1)

    def test_usage_is_preserved_when_nonempty_content_fails_json_parse(self) -> None:
        session = FakeSession(
            [
                self._response(
                    content="not-json",
                    model="deepseek-v4-flash",
                    usage={
                        "input_tokens": 101,
                        "input_tokens_details": {"cached_tokens": 80},
                        "output_tokens": 9,
                        "output_tokens_details": {"reasoning_tokens": 0},
                        "total_tokens": 110,
                    },
                )
            ]
        )
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "runtime-secret"}, clear=True):
            with self.assertRaises(SemanticReviewProviderOutputError) as raised:
                self._call_provider(self._provider(session))

        self.assertEqual(len(session.calls), 1)
        diagnostics = raised.exception.diagnostics
        self.assertEqual(diagnostics["model"], "deepseek-v4-flash")
        self.assertEqual(diagnostics["response_status"], "completed")
        self.assertEqual(diagnostics["usage"]["input_tokens"], 101)
        self.assertEqual(diagnostics["usage"]["output_tokens"], 9)
        self.assertEqual(diagnostics["usage"]["total_tokens"], 110)
        self.assertEqual(diagnostics["usage"]["cached_tokens"], 80)

    def test_malformed_json_is_not_retried_or_leniently_accepted(self) -> None:
        session = FakeSession([self._response(content="{]")])
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "runtime-secret"}, clear=True):
            with self.assertRaisesRegex(
                SemanticReviewProviderOutputError, "invalid JSON"
            ):
                self._call_provider(self._provider(session, max_retries=2))
        self.assertEqual(len(session.calls), 1)

    def test_completed_response_without_output_message_retries_once(self) -> None:
        session = FakeSession(
            [self._response(include_message=False), self._response()]
        )
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "runtime-secret"}, clear=True):
            _, metadata = self._call_provider(self._provider(session, max_retries=0))
        self.assertEqual(len(session.calls), 2)
        self.assertFalse(metadata["attempts"][0]["output_text_present"])
        self.assertTrue(metadata["attempts"][1]["output_text_present"])

    def test_failed_response_is_rejected_with_safe_diagnostics(self) -> None:
        session = FakeSession(
            [
                self._response(
                    response_status="failed",
                    error={"type": "provider_error", "code": "upstream_failure"},
                )
            ]
        )
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "runtime-secret"}, clear=True):
            with self.assertRaises(SemanticReviewAPIError) as raised:
                self._call_provider(self._provider(session, max_retries=2))
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(raised.exception.diagnostics["response_status"], "failed")
        self.assertEqual(
            raised.exception.diagnostics["error"],
            {"type": "provider_error", "code": "upstream_failure"},
        )

    def test_completed_schema_invalid_output_is_not_retried(self) -> None:
        result = valid_result()
        result["unexpected"] = True
        session = FakeSession([self._response(result)])
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "runtime-secret"}, clear=True):
            with self.assertRaisesRegex(
                SemanticReviewProviderOutputError, "unexpected fields"
            ):
                self._call_provider(self._provider(session, max_retries=2))
        self.assertEqual(len(session.calls), 1)

    def test_provider_rejects_ranking_item_missing_reason(self) -> None:
        result = valid_result()
        result["candidate_evidence_ranking"][0].pop("reason")
        session = FakeSession([self._response(result)])
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "runtime-secret"}, clear=True):
            with self.assertRaisesRegex(
                SemanticReviewProviderOutputError, "missing fields: reason"
            ):
                self._call_provider(self._provider(session))
        self.assertEqual(len(session.calls), 1)

    def test_provider_rejects_hallucinated_evidence_id(self) -> None:
        result = valid_result()
        result["candidate_evidence_ranking"][0]["evidence_id"] = "MADE_UP_99"
        session = FakeSession([self._response(result)])
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "runtime-secret"}, clear=True):
            with self.assertRaisesRegex(
                SemanticReviewProviderOutputError, "was not supplied"
            ):
                self._call_provider(self._provider(session))
        self.assertEqual(len(session.calls), 1)

    def test_provider_rejects_invalid_enum_without_alias_mapping(self) -> None:
        result = valid_result()
        result["graduation"]["decision"] = "YES"
        session = FakeSession([self._response(result)])
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "runtime-secret"}, clear=True):
            with self.assertRaisesRegex(
                SemanticReviewProviderOutputError, "graduation.decision is invalid"
            ):
                self._call_provider(self._provider(session))
        self.assertEqual(len(session.calls), 1)

    def test_canonical_schema_and_local_contract_agree(self) -> None:
        schema = semantic_review_json_schema(
            "1.0", evidence_ids={"TEST_AUTOMATION_01"}
        )
        properties = schema["properties"]
        self.assertEqual(set(schema["required"]), RESULT_KEYS)
        self.assertFalse(schema["additionalProperties"])
        decision_contracts = {
            "graduation": GRADUATION_DECISIONS,
            "location": LOCATION_DECISIONS,
            "experience_requirement": EXPERIENCE_DECISIONS,
            "job_status": STATUS_DECISIONS,
        }
        for field, allowed in decision_contracts.items():
            self.assertEqual(set(properties[field]["required"]), DECISION_KEYS)
            self.assertEqual(
                set(properties[field]["properties"]["decision"]["enum"]), allowed
            )
            self.assertEqual(
                set(properties[field]["properties"]["confidence"]["enum"]),
                CONFIDENCE_VALUES,
            )
        skill_schema = properties["must_have_skills"]["items"]
        self.assertEqual(set(skill_schema["required"]), SKILL_ITEM_KEYS)
        ranking_schema = properties["candidate_evidence_ranking"]["items"]
        self.assertEqual(set(ranking_schema["required"]), RANKING_ITEM_KEYS)
        self.assertEqual(
            set(ranking_schema["properties"]["relevance"]["enum"]),
            CONFIDENCE_VALUES,
        )
        self.assertEqual(
            ranking_schema["properties"]["evidence_id"]["enum"],
            ["TEST_AUTOMATION_01"],
        )
        self.assertEqual(
            set(properties["recommended_application_profile"]["enum"]),
            PROFILE_VALUES,
        )
        self.assertEqual(
            set(properties["recommended_resume_language"]["enum"]),
            LANGUAGE_VALUES,
        )
        example = semantic_review_example("1.0")
        self.assertEqual(validate_semantic_result(example, evidence_ids=set()), example)

    def test_incomplete_max_output_tokens_retries_once_with_compact_instruction(self) -> None:
        session = FakeSession(
            [
                self._response(
                    response_status="incomplete",
                    incomplete_details={"reason": "max_output_tokens"},
                ),
                self._response(content=json.dumps(valid_result())),
            ]
        )
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "runtime-secret"}, clear=True):
            result, metadata = self._call_provider(self._provider(session, max_retries=2))
        self.assertEqual(result, valid_result())
        self.assertEqual(len(session.calls), 2)
        self.assertIn("Keep strings concise", session.calls[1]["json"]["input"])
        self.assertEqual(metadata["retry_count"], 1)
        self.assertEqual(metadata["total_tokens"], 60)

    def test_incomplete_max_output_tokens_twice_is_controlled_failure(self) -> None:
        session = FakeSession([
            self._response(
                response_status="incomplete",
                incomplete_details={"reason": "max_output_tokens"},
            ),
            self._response(
                response_status="incomplete",
                incomplete_details={"reason": "max_output_tokens"},
            ),
        ])
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "runtime-secret"}, clear=True):
            with self.assertRaises(SemanticReviewProviderOutputError) as raised:
                self._call_provider(self._provider(session, max_retries=2))
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(raised.exception.diagnostics["response_status"], "incomplete")
        self.assertEqual(
            raised.exception.diagnostics["incomplete_details"],
            {"reason": "max_output_tokens"},
        )
        self.assertEqual(raised.exception.diagnostics["usage"]["total_tokens"], 60)

    def test_api_key_is_absent_from_provider_output_diagnostics(self) -> None:
        secret = "secret-value-that-must-not-appear"
        session = FakeSession(
            [self._response(content=None), self._response(content="   ")]
        )
        captured = io.StringIO()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret}, clear=True):
            provider = self._provider(session)
            with redirect_stdout(captured), redirect_stderr(captured):
                with self.assertRaises(SemanticReviewProviderOutputError) as raised:
                    self._call_provider(provider)
        serialized = json.dumps(raised.exception.diagnostics, sort_keys=True)
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(secret, repr(raised.exception))
        self.assertNotIn(secret, serialized)
        self.assertNotIn(secret, captured.getvalue())

    def test_strict_json_parsing_and_invalid_json(self) -> None:
        parsed = parse_strict_json_result(
            json.dumps(valid_result()),
            evidence_ids={"TEST_AUTOMATION_01"},
        )
        self.assertEqual(parsed["recommended_resume_language"], "BOTH")
        invalid = valid_result()
        invalid["unexpected"] = True
        with self.assertRaises(SemanticReviewValidationError):
            parse_strict_json_result(json.dumps(invalid), evidence_ids={"TEST_AUTOMATION_01"})
        with self.assertRaises(SemanticReviewValidationError):
            parse_strict_json_result("```json\n{}\n```", evidence_ids=set())

    def test_timeout_fails_after_bounded_attempts(self) -> None:
        session = FakeSession([requests.Timeout("timeout")])
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "runtime-secret"}, clear=True):
            provider = DeepSeekSemanticJobReviewProvider(
                max_retries=0,
                session=session,
            )
            with self.assertRaises(SemanticReviewAPIError):
                provider.review(
                    build_review_input(
                        {"company": "Acme", "title": "Engineer"},
                        [],
                        max_input_chars=10000,
                    ),
                    schema_version="1.0",
                    evidence_ids=set(),
                )
        self.assertEqual(len(session.calls), 1)

    def test_429_retries_once_then_parses_result(self) -> None:
        session = FakeSession([FakeResponse(429), self._response()])
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "runtime-secret"}, clear=True):
            provider = DeepSeekSemanticJobReviewProvider(
                max_retries=1,
                retry_backoff_seconds=0,
                session=session,
                sleeper=lambda _: None,
            )
            result, usage = provider.review(
                build_review_input(
                    {"company": "Acme", "title": "Engineer"},
                    self.evidence,
                    max_input_chars=10000,
                ),
                schema_version="1.0",
                evidence_ids={"TEST_AUTOMATION_01"},
            )
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(result["graduation"]["decision"], "ELIGIBLE")
        self.assertEqual(usage["total_tokens"], 30)
        self.assertEqual(usage["total_cost"], 0.0012)

    def test_candidate_evidence_id_hallucination_is_rejected(self) -> None:
        result = valid_result()
        result["candidate_evidence_ranking"][0]["evidence_id"] = "MADE_UP_99"
        with self.assertRaisesRegex(SemanticReviewValidationError, "was not supplied"):
            validate_semantic_result(result, evidence_ids={"TEST_AUTOMATION_01"})

    def test_uncertain_and_both_recommendations_are_supported(self) -> None:
        result = valid_result()
        for field in ("graduation", "location", "experience_requirement", "job_status"):
            result[field] = {"decision": "UNCERTAIN", "confidence": "LOW", "evidence": []}
        result["recommended_application_profile"] = "BOTH"
        result["recommended_resume_language"] = "BOTH"
        validated = validate_semantic_result(result, evidence_ids={"TEST_AUTOMATION_01"})
        self.assertEqual(validated["location"]["decision"], "UNCERTAIN")
        self.assertEqual(validated["recommended_application_profile"], "BOTH")

    def test_notes_cannot_supply_graduation_or_location_evidence(self) -> None:
        graduation = graduation_evidence(
            {
                "title": "Backend Engineer",
                "description": "Python backend role",
                "notes": "classifier note: 2027届校园招聘",
            },
            self.scoring,
            self.targets,
        )
        self.assertEqual(graduation[0], "WATCH")
        status, reason = eligibility_gate(
            {
                "company": "Acme",
                "title": "Backend Engineer",
                "description": "面向2027届毕业生 Python backend",
                "role_family": "backend",
                "location": "",
                "notes": "assume Shenzhen",
            },
            self.scoring,
            self.targets,
        )
        self.assertEqual((status, reason), ("WATCH", "location_evidence_missing"))

    def test_source_closed_note_forces_human_review_watch(self) -> None:
        status, reason = eligibility_gate(
            {
                "company": "Acme",
                "title": "2027 Backend Engineer",
                "description": "面向2027届毕业生 Python backend",
                "role_family": "backend",
                "location": "深圳",
                "notes": "来源页面明确显示：职位已关闭",
            },
            self.scoring,
            self.targets,
        )
        self.assertEqual(status, "WATCH")
        self.assertIn("human_review", reason)

    def test_allowed_cohort_set_and_other_only_cohort(self) -> None:
        self.assertEqual(self._graduation("2026届及2027届均可")[0], "ELIGIBLE")
        self.assertEqual(self._graduation("仅面向2028届毕业生")[0], "REJECT")

    def test_conflicting_sources_are_uncertain_and_dates_are_not_cohorts(self) -> None:
        conflicting = graduation_evidence(
            {
                "title": "Backend Engineer",
                "description": "仅面向2026届毕业生",
                "source_context": "官方岗位页：面向2027届毕业生",
            },
            self.scoring,
            self.targets,
        )
        self.assertEqual(conflicting[:2], ("WATCH", "mixed_graduation_year_evidence"))
        self.assertEqual(
            self._graduation("application window Oct 2026–Jul 2027")[0],
            "WATCH",
        )

    def test_campaign_level_shenzhen_is_uncertain(self) -> None:
        job = normalize_job(
            {
                "source": "official",
                "company": "Acme",
                "title": "Backend Engineer",
                "description": "面向2027届毕业生 Python backend",
                "campaign_context": "校招城市包含深圳、北京，岗位地点以详情为准",
                "location": "",
            },
            self.taxonomies,
            as_of="2026-09-09",
        )
        self.assertEqual(
            eligibility_gate(job, self.scoring, self.targets),
            ("WATCH", "location_evidence_missing"),
        )
        uncertain_location = dict(job, location="中国多地，具体待定")
        self.assertEqual(
            eligibility_gate(uncertain_location, self.scoring, self.targets),
            ("WATCH", "location_evidence_uncertain"),
        )

    def test_robot_testing_and_robot_systems_have_functional_primary_roles(self) -> None:
        testing = normalize_job(
            {
                "company": "Acme",
                "title": "机器人软件测试工程师",
                "description": "Python 自动化测试",
            },
            self.taxonomies,
        )
        systems = normalize_job(
            {
                "company": "Acme",
                "title": "具身智能系统软件工程师",
                "description": "C++ Linux ROS2",
            },
            self.taxonomies,
        )
        self.assertEqual(testing["role_family"], "test_development")
        self.assertIn("robotics", testing["secondary_role_tags"])
        self.assertEqual(systems["role_family"], "robot_software")
        self.assertIn("embodied_ai", systems["secondary_role_tags"])

    def test_same_semantic_input_uses_cache_without_duplicate_call(self) -> None:
        job = {
            "canonical_key": "acme|robot-test|shenzhen",
            "company": "Acme",
            "title": "机器人软件测试工程师",
            "description": "面向2027届，工作地点深圳，熟悉Python。",
            "location": "深圳",
            "role_family": "test_development",
            "graduation_eligibility": "ELIGIBLE",
            "status": "OPEN",
            "action": "READY",
            "url": "https://jobs.example.com/robot-test",
        }
        provider = FakeProvider()
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "trigger": ["READY", "MUST_APPLY", "USER_SELECTED"],
                "cache_dir": tmp,
                "max_input_chars": 10000,
                "max_reviews_per_run": 3,
            }
            first = run_semantic_review_batch(
                [job],
                self.evidence,
                config,
                provider=provider,
                now=lambda: datetime(2026, 9, 9, tzinfo=timezone.utc),
            )
            second = run_semantic_review_batch(
                [job],
                self.evidence,
                config,
                provider=provider,
                now=lambda: datetime(2026, 9, 9, tzinfo=timezone.utc),
            )
        self.assertEqual(first["reviewed"], 1)
        self.assertEqual(second["cache_hits"], 1)
        self.assertEqual(provider.calls, 1)

    def test_candidate_evidence_change_changes_input_hash_and_misses_cache(self) -> None:
        job = {
            "canonical_key": "acme|robot-test|shenzhen",
            "company": "Acme",
            "title": "机器人软件测试工程师",
            "description": "面向2027届，工作地点深圳，熟悉Python。",
            "location": "深圳",
            "action": "READY",
        }
        provider = FakeProvider()
        extra = deepcopy(self.evidence[0])
        extra["evidence_id"] = "AI_APP_02"
        extra["factual_description"] = "Built a bounded AI application API."
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "provider": "deepseek",
                "trigger": ["READY"],
                "cache_dir": tmp,
                "max_input_chars": 10000,
                "max_reviews_per_run": 1,
            }
            first = run_semantic_review_batch(
                [job], self.evidence, config, provider=provider
            )
            second = run_semantic_review_batch(
                [job], [*self.evidence, extra], config, provider=provider
            )
            cache_files = list(Path(tmp).glob("*.json"))
        self.assertEqual(first["reviewed"], 1)
        self.assertEqual(second["reviewed"], 1)
        self.assertEqual(second["cache_hits"], 0)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(len(cache_files), 2)

    def test_cache_without_current_provider_contract_is_not_reused(self) -> None:
        job = {
            "canonical_key": "acme|robot-test|shenzhen",
            "company": "Acme",
            "title": "机器人软件测试工程师",
            "description": "面向2027届，工作地点深圳，熟悉Python。",
            "location": "深圳",
            "action": "READY",
        }
        provider = FakeProvider()
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "provider": "deepseek",
                "trigger": ["READY"],
                "cache_dir": tmp,
                "max_input_chars": 10000,
                "max_reviews_per_run": 1,
            }
            run_semantic_review_batch([job], self.evidence, config, provider=provider)
            cache_path = next(Path(tmp).glob("*.json"))
            stale = read_json(cache_path, {})
            stale.pop("provider_contract_version")
            write_json(cache_path, stale)
            second = run_semantic_review_batch(
                [job], self.evidence, config, provider=provider
            )
        self.assertEqual(second["reviewed"], 1)
        self.assertEqual(second["cache_hits"], 0)
        self.assertEqual(provider.calls, 2)

    def test_trigger_scope_excludes_unselected_watch_and_hold(self) -> None:
        jobs = [
            {"canonical_key": "ready", "action": "READY"},
            {"canonical_key": "must", "action": "MUST_APPLY"},
            {"canonical_key": "watch", "action": "WATCH"},
            {"canonical_key": "hold", "action": "HOLD"},
            {"canonical_key": "discovered", "action": "DISCOVERED"},
        ]
        selected = select_jobs_for_review(
            jobs,
            triggers=["READY", "MUST_APPLY", "USER_SELECTED"],
            selected_job_keys={"hold", "discovered"},
            max_reviews=10,
        )
        self.assertEqual(
            [job["canonical_key"] for job in selected],
            ["ready", "must", "hold"],
        )

    def test_review_input_excludes_identity_notes_and_redacts_contacts(self) -> None:
        recruiter_email = "recruiter" + "@" + "example.org"
        candidate_email = "candidate" + "@" + "example.org"
        recruiter_phone = "+86 138" + " 0013 8000"
        candidate_phone = "+86 138" + " 0013 8001"
        payload = build_review_input(
            {
                "company": "Acme",
                "title": "Backend Engineer",
                "description": f"Contact {recruiter_email} or {recruiter_phone}. 2027届。",
                "notes": "Assume candidate lives in Shenzhen",
                "full_name": "Private Candidate",
                "email": candidate_email,
                "phone": candidate_phone,
                "url": (
                    "https://jobs.example.org/role?id=123&api_key=private-runtime-key"
                    f"&email={candidate_email}"
                ),
            },
            self.evidence,
            max_input_chars=10000,
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("notes", serialized)
        self.assertNotIn("Private Candidate", serialized)
        self.assertNotIn(candidate_email, serialized)
        self.assertNotIn(recruiter_email, serialized)
        self.assertNotIn(recruiter_phone, serialized)
        self.assertNotIn("private-runtime-key", serialized)

    def test_input_size_is_bounded(self) -> None:
        payload = build_review_input(
            {
                "company": "Acme",
                "title": "Backend Engineer",
                "description": "x" * 50000,
                "source_context": "y" * 5000,
            },
            self.evidence,
            max_input_chars=2000,
        )
        self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False, sort_keys=True)), 2000)

    def test_explicit_closed_is_normalized_without_using_notes(self) -> None:
        closed = normalize_job(
            {
                "source": "gpt_web",
                "company": "Acme",
                "title": "2027 Backend Engineer",
                "description": "职位状态：已结束",
            },
            self.taxonomies,
        )
        notes_only = normalize_job(
            {
                "source": "gpt_web",
                "company": "Acme",
                "title": "2027 Backend Engineer",
                "description": "Python backend",
                "notes": "classifier says closed",
            },
            self.taxonomies,
        )
        self.assertEqual(closed["status"], "CLOSED")
        self.assertEqual(notes_only["status"], "")

    def test_ineligible_deterministic_result_conflict_is_recorded(self) -> None:
        result = valid_result()
        conflicts = deterministic_semantic_conflicts(
            {"graduation_eligibility": "INELIGIBLE"},
            result,
        )
        self.assertEqual(conflicts[0]["field"], "graduation")

    def test_provider_enforces_endpoint_and_final_privacy_boundary(self) -> None:
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "runtime-secret"}, clear=True):
            with self.assertRaises(SemanticReviewConfigurationError):
                DeepSeekSemanticJobReviewProvider(base_url="https://example.org")

            session = FakeSession([self._response()])
            provider = DeepSeekSemanticJobReviewProvider(session=session)
            unsafe = build_review_input(
                {"company": "Acme", "title": "Engineer"},
                [],
                max_input_chars=10000,
            )
            unsafe["full_name"] = "Private Candidate"
            with self.assertRaises(SemanticReviewValidationError):
                provider.review(unsafe, schema_version="1.0", evidence_ids=set())
            self.assertEqual(session.calls, [])

    def test_config_rejects_nested_api_key_material(self) -> None:
        targets = deepcopy(self.targets)
        targets["semantic_review"]["auth"] = {"apiKey": "must-not-be-here"}
        errors = validate_configuration(
            targets,
            self.taxonomies,
            self.scoring,
            self.registry,
        )
        self.assertTrue(any("API keys must only come" in item for item in errors))

    def test_source_url_userinfo_is_removed_before_review(self) -> None:
        unsafe_url = (
            "https://private-user:private-pass"
            + "@"
            + "jobs.example.org/role"
        )
        payload = build_review_input(
            {
                "company": "Acme",
                "title": "Engineer",
                "url": unsafe_url,
            },
            [],
            max_input_chars=10000,
        )
        self.assertEqual(payload["job"]["source_urls"], ["https://jobs.example.org/role"])
        self.assertIs(validate_review_input(payload), payload)

    def test_same_jd_action_change_uses_cache(self) -> None:
        job = {
            "canonical_key": "acme|robot-test|shenzhen",
            "company": "Acme",
            "title": "机器人软件测试工程师",
            "description": "面向2027届，工作地点深圳，熟悉Python。",
            "location": "深圳",
            "role_family": "test_development",
            "graduation_eligibility": "ELIGIBLE",
            "status": "OPEN",
            "action": "READY",
            "url": "https://jobs.example.com/robot-test",
        }
        provider = FakeProvider()
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "enabled": True,
                "provider": "deepseek",
                "trigger": ["READY", "MUST_APPLY", "USER_SELECTED"],
                "cache_dir": tmp,
                "max_input_chars": 10000,
                "max_reviews_per_run": 3,
            }
            run_semantic_review_batch([job], self.evidence, config, provider=provider)
            changed = dict(job, action="MUST_APPLY", reject_reason="")
            second = run_semantic_review_batch(
                [changed], self.evidence, config, provider=provider
            )
        self.assertEqual(second["cache_hits"], 1)
        self.assertEqual(provider.calls, 1)

    def test_disabled_and_dry_run_make_no_provider_calls(self) -> None:
        provider = FakeProvider()
        job = {"canonical_key": "ready", "action": "READY"}
        disabled = run_semantic_review_batch(
            [job],
            [],
            {"enabled": False, "trigger": ["INVALID"]},
            provider=provider,
        )
        dry = run_semantic_review_batch(
            [job],
            [],
            {
                "enabled": True,
                "trigger": ["READY"],
                "max_reviews_per_run": 1,
            },
            dry_run=True,
            provider=provider,
        )
        self.assertEqual(disabled["selected"], 0)
        self.assertEqual(dry["selected"], 1)
        self.assertEqual(provider.calls, 0)

    def test_unrelated_dates_and_note_prefixed_evidence_are_not_cohort_evidence(self) -> None:
        status, _, evidence = self._graduation(
            "application window Oct 2026–Jul 2027"
        )
        self.assertEqual(status, "WATCH")
        self.assertEqual(evidence, [])
        note_only = graduation_evidence(
            {
                "title": "Backend Engineer",
                "description": "Python backend",
                "graduation_evidence": ["notes:2027届校招", "classifier:2027届"],
            },
            self.scoring,
            self.targets,
        )
        self.assertEqual(note_only, ("WATCH", "graduation_year_unknown", []))

    def test_uncertain_location_is_not_treated_as_explicit_exclusion(self) -> None:
        self.assertEqual(
            deterministic_location_decision({"location": "中国多地，具体待定"}),
            "UNCERTAIN",
        )
        result = valid_result()
        self.assertFalse(
            any(
                item["field"] == "location"
                for item in deterministic_semantic_conflicts(
                    {"location": "中国多地，具体待定"}, result
                )
            )
        )

    def test_explicit_closed_text_overrides_open_default(self) -> None:
        closed = normalize_job(
            {
                "source": "official",
                "company": "Acme",
                "title": "Backend Engineer",
                "status": "OPEN",
                "description": "[Expired]",
            },
            self.taxonomies,
        )
        self.assertEqual(closed["status"], "CLOSED")
        open_observation = dict(closed, status="OPEN")
        self.assertEqual(
            merge_job_records(open_observation, closed)["status"],
            "CLOSED",
        )

    def test_explicit_primary_role_keeps_inferred_secondary_tags(self) -> None:
        normalized = normalize_job(
            {
                "company": "Acme",
                "title": "机器人软件测试工程师",
                "description": "Python 自动化测试",
                "role_family": "test_development",
            },
            self.taxonomies,
        )
        self.assertIn("robotics", normalized["secondary_role_tags"])

        merged = merge_job_records(
            dict(normalized, graduation_evidence=["面向2027届"]),
            dict(normalized, graduation_evidence=["面向2026届"], secondary_role_tags=["robotics"]),
        )
        self.assertEqual(
            merged["graduation_evidence"],
            ["面向2027届", "面向2026届"],
        )
        self.assertEqual(
            graduation_evidence(merged, self.scoring, self.targets)[:2],
            ("WATCH", "mixed_graduation_year_evidence"),
        )

    def test_manual_import_query_parsing_remains_operational(self) -> None:
        parsed = parse_manual_line(
            "https://www.linkedin.com/jobs/view/4123456789?refId=feed | Acme | Engineer"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["job_id"], "4123456789")

    def test_pipeline_places_semantic_review_between_scoring_and_documents(self) -> None:
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
            semantic_review_dry_run=True,
            semantic_review_job_key=["watch-key"],
            as_of="2026-09-09",
        )
        commands = build_commands(args, self.targets, sys.executable, self.registry)
        scripts = [command[1] for command in commands]
        score_index = scripts.index("scripts/score_jobs.py")
        semantic_index = scripts.index("scripts/semantic_review.py")
        resume_index = scripts.index("scripts/generate_resume.py")
        self.assertLess(score_index, semantic_index)
        self.assertLess(semantic_index, resume_index)
        self.assertIn("--dry-run", commands[semantic_index])
        self.assertIn("watch-key", commands[semantic_index])

    def test_source_specific_ids_never_extract_year_token(self) -> None:
        self.assertEqual(infer_job_id("https://careers.example.com/campus/2027"), "")
        self.assertEqual(
            extract_stable_job_id("https://www.linkedin.com/jobs/view/software-engineer-4123456789"),
            "4123456789",
        )
        self.assertEqual(
            extract_stable_job_id(
                "https://nvidia.wd5.myworkdayjobs.com/job/China/Engineer_JR2024111-1"
            ),
            "JR2024111",
        )
        self.assertEqual(
            extract_stable_job_id("https://www.amazon.jobs/en/jobs/10493864/example"),
            "10493864",
        )
        sanitized = normalize_job(
            {
                "source": "manual",
                "company": "Acme",
                "title": "2027 Backend Engineer",
                "job_id": "2027",
                "url": "https://careers.example.com/campus/2027",
            },
            self.taxonomies,
        )
        self.assertEqual(sanitized["job_id"], "")

        gpt_candidate = prepare_gpt_candidate(
            {
                "company": "Acme",
                "title": "Engineer",
                "source_url": "https://careers.example.org/campus/2027",
                "job_id": "2027-campus",
            },
            imported_on="2026-09-09",
        )
        self.assertNotIn("job_id", gpt_candidate)


if __name__ == "__main__":
    unittest.main()
