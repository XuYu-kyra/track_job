#!/usr/bin/env python3
"""Bounded, privacy-safe semantic review before application preparation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

try:
    from common import load_config, read_json, write_json
except ModuleNotFoundError:
    from scripts.common import load_config, read_json, write_json


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_PROMPT_VERSION = "semantic-job-review-v4-compact-responses-json-schema"
DEFAULT_SCHEMA_VERSION = "1.0"
DEFAULT_MAX_OUTPUT_TOKENS = 4096
MAX_OUTPUT_TOKENS = 4096
DEFAULT_PROVIDER_CONTRACT_VERSION = "semantic-provider-v1"
DEEPSEEK_PROVIDER_CONTRACT_VERSION = "deepseek-responses-json-schema-v2"
ALLOWED_TRIGGER_ACTIONS = frozenset({"READY", "MUST_APPLY", "USER_SELECTED"})
CONFIDENCE_VALUES = frozenset({"HIGH", "MEDIUM", "LOW"})
EVIDENCE_SOURCE_CLASS_VALUES = frozenset(
    {"VERIFIED", "USER_ATTESTED", "DERIVED_RESUME_SAFE", "UNCERTAIN"}
)
GRADUATION_DECISIONS = frozenset({"ELIGIBLE", "INELIGIBLE", "UNCERTAIN"})
LOCATION_DECISIONS = frozenset({"SHENZHEN_ALLOWED", "NOT_SHENZHEN", "UNCERTAIN"})
EXPERIENCE_DECISIONS = frozenset(
    {"NEW_GRAD_COMPATIBLE", "EXPERIENCED_ONLY", "UNCERTAIN"}
)
STATUS_DECISIONS = frozenset({"OPEN", "CLOSED", "UNCERTAIN"})
PROFILE_VALUES = frozenset({"CN", "INTL", "BOTH"})
LANGUAGE_VALUES = frozenset({"CN", "EN", "BOTH"})
TRANSIENT_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

PROHIBITED_INPUT_KEYS = frozenset(
    {
        "full_name",
        "email",
        "phone",
        "alternate_phone",
        "address",
        "visa",
        "passport",
        "visa_information",
        "passport_information",
        "app_id",
        "app_secret",
        "app_token",
        "table_id",
        "view_id",
        "api_key",
        "deepseek_api_key",
        "feishu_credentials",
    }
)
CONTACT_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
CONTACT_PHONE_RE = re.compile(
    r"(?<!\w)(?:\+\d[\d\s()\-]{7,}\d|1[3-9]\d{9}|(?:\d[\s()\-]?){10,}\d)(?!\w)"
)
SECRET_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
NON_FACTUAL_EVIDENCE_PREFIX = re.compile(
    r"^(?:notes?|comments?|warnings?|classifier(?:\s+explanation)?|"
    r"reject[_\s-]?reason|备注|评论|警告|分类器)\s*[:：-]",
    flags=re.IGNORECASE,
)

RESULT_KEYS = frozenset(
    {
        "schema_version",
        "graduation",
        "location",
        "experience_requirement",
        "job_status",
        "primary_role_family",
        "secondary_role_tags",
        "must_have_skills",
        "preferred_skills",
        "core_responsibilities",
        "resume_focus",
        "candidate_evidence_ranking",
        "recommended_application_profile",
        "recommended_resume_language",
        "uncertainties",
    }
)
DECISION_KEYS = frozenset({"decision", "confidence", "evidence"})
SKILL_ITEM_KEYS = frozenset({"skill", "evidence"})
RANKING_ITEM_KEYS = frozenset({"evidence_id", "relevance", "reason"})
REVIEW_INPUT_KEYS = frozenset({"job", "candidate_evidence"})
JOB_INPUT_KEYS = frozenset(
    {
        "company",
        "title",
        "raw_jd",
        "location",
        "location_evidence",
        "graduation_evidence",
        "source_context",
        "source_urls",
        "deterministic_outputs",
    }
)
SOURCE_CONTEXT_KEYS = frozenset(
    {"page_context", "campaign_context", "source_context", "recruiting_context"}
)
DETERMINISTIC_OUTPUT_KEYS = frozenset(
    {
        "canonical_key",
        "role_family",
        "secondary_role_tags",
        "eligibility",
        "graduation_eligibility",
        "status",
        "action",
        "reject_reason",
    }
)


class SemanticReviewError(RuntimeError):
    """Base exception for semantic review failures."""


class SemanticReviewConfigurationError(SemanticReviewError):
    """Raised when an enabled provider lacks safe runtime configuration."""


class SemanticReviewAPIError(SemanticReviewError):
    """Raised for bounded provider request failures."""

    def __init__(self, message: str, diagnostics: dict[str, Any] | None = None) -> None:
        self.diagnostics = diagnostics or {}
        suffix = (
            f"; diagnostics={json.dumps(self.diagnostics, sort_keys=True)}"
            if self.diagnostics
            else ""
        )
        super().__init__(message + suffix)


class SemanticReviewValidationError(SemanticReviewError):
    """Raised when an input or result violates the strict contract."""


class SemanticReviewProviderOutputError(SemanticReviewValidationError):
    """Raised when a successful response has unusable provider output."""

    def __init__(self, message: str, diagnostics: dict[str, Any]) -> None:
        self.diagnostics = diagnostics
        super().__init__(
            f"{message}; diagnostics={json.dumps(diagnostics, sort_keys=True)}"
        )


class SemanticJobReviewProvider(ABC):
    """Provider contract for a structured, non-canonical semantic opinion."""

    provider_name = "abstract"
    contract_version = DEFAULT_PROVIDER_CONTRACT_VERSION

    @property
    @abstractmethod
    def model(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def review(
        self,
        review_input: dict[str, Any],
        *,
        schema_version: str,
        evidence_ids: set[str],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        raise NotImplementedError


def _string_array_schema(*, max_items: int = 40) -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "maxItems": max_items,
    }


def _decision_schema(allowed: frozenset[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(DECISION_KEYS),
        "properties": {
            "decision": {"type": "string", "enum": sorted(allowed)},
            "confidence": {"type": "string", "enum": sorted(CONFIDENCE_VALUES)},
            "evidence": _string_array_schema(max_items=12),
        },
    }


def semantic_review_json_schema(
    schema_version: str = DEFAULT_SCHEMA_VERSION,
    *,
    evidence_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Build the provider JSON Schema from the same contract used locally."""

    evidence_id_schema: dict[str, Any] = {"type": "string", "minLength": 1}
    if evidence_ids:
        evidence_id_schema["enum"] = sorted(evidence_ids)
    skill_item_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(SKILL_ITEM_KEYS),
        "properties": {
            "skill": {"type": "string", "minLength": 1},
            "evidence": {"type": "string", "minLength": 1},
        },
    }
    ranking_item_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(RANKING_ITEM_KEYS),
        "properties": {
            "evidence_id": evidence_id_schema,
            "relevance": {"type": "string", "enum": sorted(CONFIDENCE_VALUES)},
            "reason": {"type": "string", "minLength": 1},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(RESULT_KEYS),
        "properties": {
            "schema_version": {"type": "string", "enum": [schema_version]},
            "graduation": _decision_schema(GRADUATION_DECISIONS),
            "location": _decision_schema(LOCATION_DECISIONS),
            "experience_requirement": _decision_schema(EXPERIENCE_DECISIONS),
            "job_status": _decision_schema(STATUS_DECISIONS),
            "primary_role_family": {"type": "string", "minLength": 1},
            "secondary_role_tags": _string_array_schema(),
            "must_have_skills": {
                "type": "array",
                "items": skill_item_schema,
                "maxItems": 40,
            },
            "preferred_skills": {
                "type": "array",
                "items": skill_item_schema,
                "maxItems": 40,
            },
            "core_responsibilities": _string_array_schema(),
            "resume_focus": _string_array_schema(),
            "candidate_evidence_ranking": {
                "type": "array",
                "items": ranking_item_schema,
                "maxItems": 40,
            },
            "recommended_application_profile": {
                "type": "string",
                "enum": sorted(PROFILE_VALUES),
            },
            "recommended_resume_language": {
                "type": "string",
                "enum": sorted(LANGUAGE_VALUES),
            },
            "uncertainties": _string_array_schema(),
        },
    }


def semantic_review_example(schema_version: str = DEFAULT_SCHEMA_VERSION) -> dict[str, Any]:
    """Return a fact-free example conforming to the canonical review contract."""

    return {
        "schema_version": schema_version,
        "graduation": {"decision": "UNCERTAIN", "confidence": "LOW", "evidence": []},
        "location": {"decision": "UNCERTAIN", "confidence": "LOW", "evidence": []},
        "experience_requirement": {
            "decision": "UNCERTAIN",
            "confidence": "LOW",
            "evidence": [],
        },
        "job_status": {"decision": "UNCERTAIN", "confidence": "LOW", "evidence": []},
        "primary_role_family": "uncertain",
        "secondary_role_tags": [],
        "must_have_skills": [],
        "preferred_skills": [],
        "core_responsibilities": [],
        "resume_focus": [],
        "candidate_evidence_ranking": [],
        "recommended_application_profile": "BOTH",
        "recommended_resume_language": "BOTH",
        "uncertainties": [],
    }


def _strict_object(value: Any, keys: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SemanticReviewValidationError(f"{label} must be an object")
    missing = keys - set(value)
    extra = set(value) - keys
    if missing:
        raise SemanticReviewValidationError(
            f"{label} missing fields: {', '.join(sorted(missing))}"
        )
    if extra:
        raise SemanticReviewValidationError(
            f"{label} has unexpected fields: {', '.join(sorted(extra))}"
        )
    return value


def _string_list(value: Any, label: str, *, max_items: int = 40) -> list[str]:
    if not isinstance(value, list) or len(value) > max_items:
        raise SemanticReviewValidationError(f"{label} must be a bounded list")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise SemanticReviewValidationError(f"{label} must contain non-empty strings")
    return value


def _validate_decision(
    value: Any,
    label: str,
    allowed: frozenset[str],
) -> None:
    item = _strict_object(value, DECISION_KEYS, label)
    if item["decision"] not in allowed:
        raise SemanticReviewValidationError(f"{label}.decision is invalid")
    if item["confidence"] not in CONFIDENCE_VALUES:
        raise SemanticReviewValidationError(f"{label}.confidence is invalid")
    evidence = _string_list(item["evidence"], f"{label}.evidence", max_items=12)
    if item["decision"] != "UNCERTAIN" and not evidence:
        raise SemanticReviewValidationError(
            f"{label}.evidence is required for a non-UNCERTAIN decision"
        )


def _validate_skill_items(value: Any, label: str) -> None:
    if not isinstance(value, list) or len(value) > 40:
        raise SemanticReviewValidationError(f"{label} must be a bounded list")
    for index, item in enumerate(value):
        skill = _strict_object(
            item,
            SKILL_ITEM_KEYS,
            f"{label}[{index}]",
        )
        if not all(isinstance(skill[key], str) and skill[key].strip() for key in skill):
            raise SemanticReviewValidationError(f"{label}[{index}] values must be strings")


def validate_semantic_result(
    result: Any,
    *,
    evidence_ids: set[str],
    schema_version: str = DEFAULT_SCHEMA_VERSION,
) -> dict[str, Any]:
    item = _strict_object(result, RESULT_KEYS, "semantic result")
    if item["schema_version"] != schema_version:
        raise SemanticReviewValidationError("semantic result schema_version is invalid")
    _validate_decision(item["graduation"], "graduation", GRADUATION_DECISIONS)
    _validate_decision(item["location"], "location", LOCATION_DECISIONS)
    _validate_decision(
        item["experience_requirement"],
        "experience_requirement",
        EXPERIENCE_DECISIONS,
    )
    _validate_decision(item["job_status"], "job_status", STATUS_DECISIONS)
    if not isinstance(item["primary_role_family"], str) or not item["primary_role_family"].strip():
        raise SemanticReviewValidationError("primary_role_family must be a non-empty string")
    _string_list(item["secondary_role_tags"], "secondary_role_tags")
    _validate_skill_items(item["must_have_skills"], "must_have_skills")
    _validate_skill_items(item["preferred_skills"], "preferred_skills")
    _string_list(item["core_responsibilities"], "core_responsibilities")
    _string_list(item["resume_focus"], "resume_focus")
    _string_list(item["uncertainties"], "uncertainties")

    rankings = item["candidate_evidence_ranking"]
    if not isinstance(rankings, list) or len(rankings) > 40:
        raise SemanticReviewValidationError("candidate_evidence_ranking must be a bounded list")
    seen_ids: set[str] = set()
    for index, ranking in enumerate(rankings):
        entry = _strict_object(
            ranking,
            RANKING_ITEM_KEYS,
            f"candidate_evidence_ranking[{index}]",
        )
        if not isinstance(entry["evidence_id"], str) or not entry["evidence_id"].strip():
            raise SemanticReviewValidationError(
                f"candidate_evidence_ranking[{index}].evidence_id must be a string"
            )
        evidence_id = entry["evidence_id"]
        if evidence_id not in evidence_ids:
            raise SemanticReviewValidationError(
                f"candidate evidence ID was not supplied: {evidence_id}"
            )
        if evidence_id in seen_ids:
            raise SemanticReviewValidationError(
                f"candidate evidence ID was ranked more than once: {evidence_id}"
            )
        seen_ids.add(evidence_id)
        if entry["relevance"] not in CONFIDENCE_VALUES:
            raise SemanticReviewValidationError(
                f"candidate_evidence_ranking[{index}].relevance is invalid"
            )
        if not isinstance(entry["reason"], str) or not entry["reason"].strip():
            raise SemanticReviewValidationError(
                f"candidate_evidence_ranking[{index}].reason must be a string"
            )
    if item["recommended_application_profile"] not in PROFILE_VALUES:
        raise SemanticReviewValidationError("recommended_application_profile is invalid")
    if item["recommended_resume_language"] not in LANGUAGE_VALUES:
        raise SemanticReviewValidationError("recommended_resume_language is invalid")
    return item


def parse_strict_json_result(
    content: Any,
    *,
    evidence_ids: set[str],
    schema_version: str = DEFAULT_SCHEMA_VERSION,
) -> dict[str, Any]:
    if not isinstance(content, str) or not content.strip():
        raise SemanticReviewValidationError("provider returned empty content")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise SemanticReviewValidationError("provider returned invalid JSON") from exc
    return validate_semantic_result(
        parsed,
        evidence_ids=evidence_ids,
        schema_version=schema_version,
    )


def _system_prompt(schema_version: str) -> str:
    example = semantic_review_example(schema_version)
    compact_example = json.dumps(example, ensure_ascii=False, separators=(",", ":"))
    return f"""You are a bounded semantic job reviewer. Return exactly one non-empty JSON object using schema_version {schema_version}, with no surrounding prose or Markdown. Obey the supplied JSON Schema exactly. This compact JSON example shows the required shape and contains no candidate facts:
{compact_example}
Allowed JSON enum values: graduation.decision is {', '.join(sorted(GRADUATION_DECISIONS))}; location.decision is {', '.join(sorted(LOCATION_DECISIONS))}; experience_requirement.decision is {', '.join(sorted(EXPERIENCE_DECISIONS))}; job_status.decision is {', '.join(sorted(STATUS_DECISIONS))}; confidence and ranking relevance are {', '.join(sorted(CONFIDENCE_VALUES))}; recommended_application_profile is {', '.join(sorted(PROFILE_VALUES))}; recommended_resume_language is {', '.join(sorted(LANGUAGE_VALUES))}.
Use only the supplied job/source evidence and supplied anonymized candidate evidence. Never invent candidate facts, eligibility, status, location, skills, metrics, or results. Important conclusions must use verbatim or tightly bounded JD evidence. If evidence is insufficient, return UNCERTAIN.
Notes and classifier comments are not factual JD evidence. Distinguish graduation cohorts from application/date ranges, job-level location from campaign/company location, required from preferred experience, and primary function from industry/domain. An explicitly allowed set such as 2026 and 2027 both eligible permits 2027. Conflicting sources require UNCERTAIN. Campaign-level Shenzhen without a job-level location is UNCERTAIN. Robot software testing is primary_role_family test_development with robotics as a secondary tag; robot systems software is robot_software. Do not rank any candidate evidence ID that was not supplied.
For a mixed CAX algorithm/AI-application role, infer the primary family from actual responsibilities; if those responsibilities do not resolve it, record the ambiguity in uncertainties instead of guessing. Evidence is required for every non-UNCERTAIN decision.
Keep the JSON compact: at most 8 must-have skills, 8 preferred skills, 6 core responsibilities, 6 resume-focus items, and 12 candidate-evidence ranking items. Keep each reason/evidence string concise. Do not emit Markdown or extra keys."""


class DeepSeekSemanticJobReviewProvider(SemanticJobReviewProvider):
    provider_name = "deepseek"
    contract_version = DEEPSEEK_PROVIDER_CONTRACT_VERSION

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 30,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.5,
        session: requests.Session | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if str(base_url or DEFAULT_BASE_URL).rstrip("/") != DEFAULT_BASE_URL:
            raise SemanticReviewConfigurationError(
                f"DeepSeek semantic review base URL must be {DEFAULT_BASE_URL}"
            )
        resolved_key = os.getenv("DEEPSEEK_API_KEY", "")
        if not resolved_key:
            raise SemanticReviewConfigurationError(
                "DEEPSEEK_API_KEY is required for an enabled DeepSeek semantic review"
            )
        self._api_key = resolved_key
        self._model = str(model or DEFAULT_MODEL)
        self.base_url = DEFAULT_BASE_URL
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.max_output_tokens = max(1, min(int(max_output_tokens), MAX_OUTPUT_TOKENS))
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self.session = session or requests.Session()
        self.sleeper = sleeper
        self.last_diagnostics: dict[str, Any] = {}

    @property
    def model(self) -> str:
        return self._model

    def review(
        self,
        review_input: dict[str, Any],
        *,
        schema_version: str,
        evidence_ids: set[str],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        review_input = validate_review_input(review_input)
        supplied_evidence_ids = {
            item["evidence_id"] for item in review_input["candidate_evidence"]
        }
        if evidence_ids != supplied_evidence_ids:
            raise SemanticReviewValidationError(
                "candidate evidence ID set does not match the supplied review input"
            )
        input_json = json.dumps(review_input, ensure_ascii=False, sort_keys=True)
        attempt_diagnostics: list[dict[str, Any]] = []
        aggregate_usage: dict[str, int | float] = {}
        request_count = 0
        transport_retries = 0
        empty_retries = 0
        truncation_retries = 0

        def base_diagnostics(http_status: int | None) -> dict[str, Any]:
            return {
                "http_status": http_status,
                "model": self.model,
                "response_status": None,
                "output_text_present": False,
                "content_present": False,
                "reasoning_content_present": False,
                "incomplete_details": None,
                "error": None,
            }

        def combined_diagnostics(last: dict[str, Any]) -> dict[str, Any]:
            combined = {
                **last,
                "thinking_mode": "disabled",
                "reasoning_effort": "none",
                "retry_count": max(0, request_count - 1),
                "usage": dict(aggregate_usage),
                "attempts": list(attempt_diagnostics),
            }
            self.last_diagnostics = combined
            return combined

        while True:
            user_instruction = (
                "Return the semantic review as JSON. Input follows:\n" + input_json
            )
            if empty_retries or truncation_retries:
                user_instruction += (
                    "\nReturn exactly one non-empty JSON object and no surrounding prose. "
                    "Keep strings concise and respect the item limits."
                )
            request_body = {
                "model": self.model,
                "instructions": _system_prompt(schema_version),
                "input": user_instruction,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "semantic_job_review",
                        "schema": semantic_review_json_schema(
                            schema_version,
                            evidence_ids=evidence_ids,
                        ),
                    }
                },
                "reasoning": {"effort": "none"},
                "max_output_tokens": self.max_output_tokens,
            }
            retry_count = request_count
            request_count += 1
            try:
                response = self.session.post(
                    f"{self.base_url}/responses",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=request_body,
                    timeout=self.timeout_seconds,
                )
            except (requests.Timeout, requests.ConnectionError):
                if transport_retries < self.max_retries:
                    self.sleeper(self.retry_backoff_seconds * (2**transport_retries))
                    transport_retries += 1
                    continue
                diagnostics = combined_diagnostics(base_diagnostics(None))
                raise SemanticReviewAPIError(
                    "DeepSeek semantic review failed after transient network errors",
                    diagnostics,
                ) from None
            except requests.RequestException:
                diagnostics = combined_diagnostics(base_diagnostics(None))
                raise SemanticReviewAPIError(
                    "DeepSeek semantic review request failed", diagnostics
                ) from None

            if (
                response.status_code in TRANSIENT_STATUS_CODES
                and transport_retries < self.max_retries
            ):
                self.sleeper(self.retry_backoff_seconds * (2**transport_retries))
                transport_retries += 1
                continue
            if not 200 <= response.status_code < 300:
                diagnostics = combined_diagnostics(
                    base_diagnostics(response.status_code)
                )
                raise SemanticReviewAPIError(
                    f"DeepSeek semantic review failed with HTTP {response.status_code}",
                    diagnostics,
                )
            try:
                response_payload = response.json()
            except ValueError:
                diagnostics = combined_diagnostics(
                    base_diagnostics(response.status_code)
                )
                raise SemanticReviewProviderOutputError(
                    "DeepSeek response body was not JSON", diagnostics
                ) from None

            if not isinstance(response_payload, dict):
                response_payload = {}
            usage = _normalize_deepseek_usage(response_payload.get("usage", {}))
            for key, value in usage.items():
                aggregate_usage[key] = aggregate_usage.get(key, 0) + value

            content = _extract_responses_output_text(response_payload)
            content_present = bool(content.strip())
            reasoning_content_present = _responses_reasoning_present(response_payload)
            model = _safe_diagnostic_text(response_payload.get("model"), self.model)
            response_status = _safe_diagnostic_text(response_payload.get("status"), None)
            incomplete_details = _safe_response_detail(
                response_payload.get("incomplete_details")
            )
            response_error = _safe_response_detail(response_payload.get("error"))
            attempt_metadata = {
                "http_status": response.status_code,
                "model": model,
                "response_status": response_status,
                "output_text_present": content_present,
                "content_present": content_present,
                "reasoning_content_present": reasoning_content_present,
                "usage": usage,
                "incomplete_details": incomplete_details,
                "error": response_error,
                "retry_count": retry_count,
            }
            attempt_diagnostics.append(attempt_metadata)

            if response_status == "failed":
                diagnostics = combined_diagnostics(attempt_metadata)
                raise SemanticReviewAPIError(
                    "DeepSeek Responses API returned a failed response", diagnostics
                )
            if response_status == "incomplete":
                reason = (
                    incomplete_details.get("reason")
                    if isinstance(incomplete_details, dict)
                    else None
                )
                if reason == "max_output_tokens" and truncation_retries == 0:
                    truncation_retries = 1
                    continue
                diagnostics = combined_diagnostics(attempt_metadata)
                message = (
                    "DeepSeek response was truncated at max_output_tokens"
                    if reason == "max_output_tokens"
                    else "DeepSeek Responses API returned an incomplete response"
                )
                raise SemanticReviewProviderOutputError(message, diagnostics)
            if response_status != "completed":
                diagnostics = combined_diagnostics(attempt_metadata)
                raise SemanticReviewProviderOutputError(
                    "DeepSeek response status was missing or not completed", diagnostics
                )
            if not content_present:
                if empty_retries == 0:
                    empty_retries = 1
                    continue
                diagnostics = combined_diagnostics(attempt_metadata)
                raise SemanticReviewProviderOutputError(
                    "DeepSeek returned no output_text twice", diagnostics
                )
            try:
                result = parse_strict_json_result(
                    content,
                    evidence_ids=evidence_ids,
                    schema_version=schema_version,
                )
            except SemanticReviewValidationError as exc:
                diagnostics = combined_diagnostics(attempt_metadata)
                raise SemanticReviewProviderOutputError(str(exc), diagnostics) from exc

            metadata: dict[str, Any] = {
                **aggregate_usage,
                "model": model,
                "response_status": response_status,
                "thinking_mode": "disabled",
                "reasoning_effort": "none",
                "output_text_present": True,
                "content_present": True,
                "reasoning_content_present": reasoning_content_present,
                "incomplete_details": incomplete_details,
                "error": response_error,
                "retry_count": max(0, request_count - 1),
                "attempts": list(attempt_diagnostics),
            }
            self.last_diagnostics = metadata
            return result, metadata


def semantic_result_requires_human_review(
    result: dict[str, Any], conflicts: list[dict[str, Any]]
) -> bool:
    uncertain = any(
        result[field]["decision"] == "UNCERTAIN"
        for field in ("graduation", "location", "experience_requirement", "job_status")
    )
    source_closed = result["job_status"]["decision"] == "CLOSED"
    return bool(conflicts or uncertain or source_closed)


def _safe_diagnostic_text(value: Any, fallback: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return fallback
    return value.strip()[:200]


def _safe_response_detail(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    safe: dict[str, Any] = {}
    for key in ("code", "type", "reason", "message"):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            safe[key] = _redact_source_text(item.strip()[:500])
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            safe[key] = item
    return safe or None


def _extract_responses_output_text(payload: dict[str, Any]) -> str:
    top_level = payload.get("output_text")
    if isinstance(top_level, str) and top_level.strip():
        return top_level
    parts: list[str] = []
    output = payload.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _responses_reasoning_present(payload: dict[str, Any]) -> bool:
    usage = payload.get("usage")
    if isinstance(usage, dict):
        details = usage.get("output_tokens_details")
        if isinstance(details, dict):
            reasoning_tokens = details.get("reasoning_tokens")
            if (
                isinstance(reasoning_tokens, (int, float))
                and not isinstance(reasoning_tokens, bool)
                and reasoning_tokens > 0
            ):
                return True
    output = payload.get("output")
    if not isinstance(output, list):
        return False
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            continue
        if item.get("content") not in (None, "", []):
            return True
        if item.get("summary") not in (None, "", []):
            return True
    return False


def _normalize_deepseek_usage(value: Any) -> dict[str, int | float]:
    raw = value if isinstance(value, dict) else {}
    allowed = {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cost",
        "input_cost",
        "output_cost",
        "total_cost",
    }
    usage = {
        key: item
        for key, item in raw.items()
        if key in allowed and isinstance(item, (int, float)) and not isinstance(item, bool)
    }
    input_details = raw.get("input_tokens_details")
    output_details = raw.get("output_tokens_details")
    nested = {
        "cached_tokens": (
            input_details.get("cached_tokens") if isinstance(input_details, dict) else None
        ),
        "reasoning_tokens": (
            output_details.get("reasoning_tokens")
            if isinstance(output_details, dict)
            else None
        ),
    }
    usage.update(
        {
            key: item
            for key, item in nested.items()
            if isinstance(item, (int, float)) and not isinstance(item, bool)
        }
    )
    return usage


def _redact_source_text(value: Any) -> str:
    text = str(value or "")
    text = CONTACT_EMAIL_RE.sub("[REDACTED_CONTACT]", text)
    text = CONTACT_PHONE_RE.sub("[REDACTED_CONTACT]", text)
    return SECRET_RE.sub("[REDACTED_SECRET]", text)


def _redacted_string_list(value: Any) -> list[str]:
    raw = value if isinstance(value, list) else [value] if value not in (None, "") else []
    return [_redact_source_text(item) for item in raw if str(item or "").strip()]


def _source_evidence_list(value: Any) -> list[str]:
    return [
        item
        for item in _redacted_string_list(value)
        if not NON_FACTUAL_EVIDENCE_PREFIX.match(item.strip())
    ]


def _redact_source_url(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    hostname = parsed.hostname
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        return ""
    safe_netloc = f"{hostname}{port}"
    sensitive_query_markers = (
        "api_key",
        "apikey",
        "secret",
        "token",
        "password",
        "email",
        "phone",
        "passport",
        "visa",
        "address",
    )
    safe_query = [
        (key, "[REDACTED]")
        if any(marker in key.casefold() for marker in sensitive_query_markers)
        else (key, _redact_source_text(item))
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    return _redact_source_text(
        urlunsplit((parsed.scheme, safe_netloc, parsed.path, urlencode(safe_query), ""))
    )


def _assert_private_keys_absent(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in PROHIBITED_INPUT_KEYS:
                raise SemanticReviewValidationError(
                    f"prohibited personal or credential field: {key}"
                )
            _assert_private_keys_absent(child)
    elif isinstance(value, list):
        for child in value:
            _assert_private_keys_absent(child)


def prepare_candidate_evidence(value: Any, *, max_items: int = 40) -> list[dict[str, Any]]:
    if value in (None, "", {}):
        return []
    raw_items = value.get("evidence", []) if isinstance(value, dict) else value
    if not isinstance(raw_items, list):
        raise SemanticReviewValidationError("candidate evidence must be a list")
    allowed = {
        "evidence_id",
        "project",
        "category",
        "factual_description",
        "technologies",
        "verified_metrics",
        "results",
        "provenance",
        "suitable_role_families",
        "confidence",
        "source_class",
    }
    prepared: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_items[:max_items]):
        if not isinstance(raw, dict):
            raise SemanticReviewValidationError(
                f"candidate evidence[{index}] must be an object"
            )
        _assert_private_keys_absent(raw)
        extra = set(raw) - allowed
        if extra:
            raise SemanticReviewValidationError(
                f"candidate evidence[{index}] has unexpected fields: {', '.join(sorted(extra))}"
            )
        evidence_id = str(raw.get("evidence_id") or "").strip()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9_:-]{2,79}", evidence_id):
            raise SemanticReviewValidationError(
                f"candidate evidence[{index}] requires an anonymized evidence_id"
            )
        if evidence_id in seen:
            raise SemanticReviewValidationError(f"duplicate candidate evidence ID: {evidence_id}")
        seen.add(evidence_id)
        description = _redact_source_text(raw.get("factual_description"))
        if not description.strip():
            raise SemanticReviewValidationError(
                f"candidate evidence[{index}] requires factual_description"
            )
        technologies = raw.get("technologies", [])
        if not isinstance(technologies, list) or any(
            not isinstance(item, str) or not item.strip() for item in technologies
        ):
            raise SemanticReviewValidationError(
                f"candidate evidence[{index}].technologies must be a string list"
            )
        provenance = raw.get("provenance", [])
        if not isinstance(provenance, list) or any(
            not isinstance(item, str) or not item.strip() for item in provenance
        ):
            raise SemanticReviewValidationError(
                f"candidate evidence[{index}].provenance must be a string list"
            )
        role_families = raw.get("suitable_role_families", [])
        if not isinstance(role_families, list) or any(
            not isinstance(item, str) or not item.strip() for item in role_families
        ):
            raise SemanticReviewValidationError(
                f"candidate evidence[{index}].suitable_role_families must be a string list"
            )
        confidence = str(raw.get("confidence") or "LOW").upper()
        if confidence not in CONFIDENCE_VALUES:
            raise SemanticReviewValidationError(
                f"candidate evidence[{index}].confidence is invalid"
            )
        source_class = str(raw.get("source_class") or "UNCERTAIN").upper()
        if source_class not in EVIDENCE_SOURCE_CLASS_VALUES:
            raise SemanticReviewValidationError(
                f"candidate evidence[{index}].source_class is invalid"
            )
        prepared.append(
            {
                "evidence_id": evidence_id,
                "project": _redact_source_text(raw.get("project")),
                "category": _redact_source_text(raw.get("category")),
                "factual_description": description,
                "technologies": [_redact_source_text(item) for item in technologies],
                "verified_metrics": _redacted_string_list(raw.get("verified_metrics")),
                "results": _redacted_string_list(raw.get("results")),
                "provenance": [_redact_source_text(item) for item in provenance],
                "suitable_role_families": [
                    _redact_source_text(item) for item in role_families
                ],
                "confidence": confidence,
                "source_class": source_class,
            }
        )
    return prepared


def _assert_strings_are_sanitized(value: Any) -> None:
    if isinstance(value, dict):
        for child in value.values():
            _assert_strings_are_sanitized(child)
    elif isinstance(value, list):
        for child in value:
            _assert_strings_are_sanitized(child)
    elif isinstance(value, str) and _redact_source_text(value) != value:
        raise SemanticReviewValidationError(
            "semantic review input contains unsanitized contact or secret data"
        )


def validate_review_input(value: Any) -> dict[str, Any]:
    """Enforce the privacy-safe allowlist again at the final HTTP boundary."""

    payload = _strict_object(value, REVIEW_INPUT_KEYS, "semantic review input")
    _assert_private_keys_absent(payload)
    _assert_strings_are_sanitized(payload)
    job = _strict_object(payload["job"], JOB_INPUT_KEYS, "semantic review input.job")
    for field in ("company", "title", "raw_jd", "location"):
        if not isinstance(job[field], str):
            raise SemanticReviewValidationError(
                f"semantic review input.job.{field} must be a string"
            )
    for field in ("location_evidence", "graduation_evidence", "source_urls"):
        _string_list(job[field], f"semantic review input.job.{field}")
    source_context = job["source_context"]
    if not isinstance(source_context, dict):
        raise SemanticReviewValidationError(
            "semantic review input.job.source_context must be an object"
        )
    unexpected_context = set(source_context) - SOURCE_CONTEXT_KEYS
    if unexpected_context or any(
        not isinstance(item, str) for item in source_context.values()
    ):
        raise SemanticReviewValidationError(
            "semantic review input.job.source_context violates the allowlist"
        )
    deterministic = job["deterministic_outputs"]
    if not isinstance(deterministic, dict):
        raise SemanticReviewValidationError(
            "semantic review input.job.deterministic_outputs must be an object"
        )
    unexpected_outputs = set(deterministic) - DETERMINISTIC_OUTPUT_KEYS
    if unexpected_outputs:
        raise SemanticReviewValidationError(
            "semantic review input.job.deterministic_outputs violates the allowlist"
        )
    for url in job["source_urls"]:
        if not _redact_source_url(url) or _redact_source_url(url) != url:
            raise SemanticReviewValidationError(
                "semantic review input contains an unsafe source URL"
            )
    prepared_evidence = prepare_candidate_evidence(payload["candidate_evidence"])
    if prepared_evidence != payload["candidate_evidence"]:
        raise SemanticReviewValidationError(
            "candidate evidence must use the normalized privacy-safe contract"
        )
    return payload


def build_review_input(
    job: dict[str, Any],
    candidate_evidence: list[dict[str, Any]],
    *,
    max_input_chars: int,
) -> dict[str, Any]:
    max_chars = max(1000, int(max_input_chars))
    source_urls = job.get("source_urls") or [job.get("official_url"), job.get("url")]
    if not isinstance(source_urls, list):
        source_urls = [source_urls]
    raw_jd = _redact_source_text(job.get("raw_jd") or job.get("description"))[:max_chars]
    payload = {
        "job": {
            "company": _redact_source_text(job.get("company")),
            "title": _redact_source_text(job.get("title") or job.get("position")),
            "raw_jd": raw_jd,
            "location": _redact_source_text(job.get("location")),
            "location_evidence": _source_evidence_list(job.get("location_evidence")),
            "graduation_evidence": _source_evidence_list(job.get("graduation_evidence")),
            "source_context": {
                field: _redact_source_text(job.get(field))
                for field in (
                    "page_context",
                    "campaign_context",
                    "source_context",
                    "recruiting_context",
                )
                if job.get(field)
            },
            "source_urls": [
                redacted
                for item in source_urls
                if (redacted := _redact_source_url(item))
            ],
            "deterministic_outputs": {
                field: job.get(field)
                for field in (
                    "canonical_key",
                    "role_family",
                    "secondary_role_tags",
                    "eligibility",
                    "graduation_eligibility",
                    "status",
                    "action",
                    "reject_reason",
                )
                if job.get(field) not in (None, "", [], {})
            },
        },
        "candidate_evidence": [dict(item) for item in candidate_evidence],
    }
    _assert_private_keys_absent(payload)
    serialized_length = len(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if serialized_length > max_chars:
        overflow = serialized_length - max_chars
        current_jd = payload["job"]["raw_jd"]
        payload["job"]["raw_jd"] = current_jd[: max(0, len(current_jd) - overflow)]
    while (
        len(json.dumps(payload, ensure_ascii=False, sort_keys=True)) > max_chars
        and payload["candidate_evidence"]
    ):
        payload["candidate_evidence"].pop()
    if len(json.dumps(payload, ensure_ascii=False, sort_keys=True)) > max_chars:
        payload["job"]["source_context"] = {}
    if len(json.dumps(payload, ensure_ascii=False, sort_keys=True)) > max_chars:
        raise SemanticReviewValidationError(
            "semantic review metadata exceeds max_input_chars"
        )
    return validate_review_input(payload)


def select_jobs_for_review(
    jobs: list[dict[str, Any]],
    *,
    triggers: list[str],
    selected_job_keys: set[str],
    max_reviews: int,
) -> list[dict[str, Any]]:
    if int(max_reviews) <= 0:
        return []
    configured = {str(item).upper() for item in triggers}
    invalid = configured - ALLOWED_TRIGGER_ACTIONS
    if invalid:
        raise SemanticReviewConfigurationError(
            f"unsupported semantic review triggers: {', '.join(sorted(invalid))}"
        )
    selected: list[dict[str, Any]] = []
    for job in jobs:
        action = str(job.get("action") or "").upper()
        canonical_key = str(job.get("canonical_key") or "")
        automatic = action in {"READY", "MUST_APPLY"} and action in configured
        explicit = (
            "USER_SELECTED" in configured
            and canonical_key in selected_job_keys
            and action in {"WATCH", "HOLD"}
        )
        if automatic or explicit:
            selected.append(job)
        if len(selected) >= max(0, int(max_reviews)):
            break
    return selected


def _semantic_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _cache_semantic_input(review_input: dict[str, Any]) -> dict[str, Any]:
    """Remove queue-only churn and normalize unordered evidence before hashing."""

    normalized = json.loads(json.dumps(review_input, ensure_ascii=False))
    deterministic = normalized["job"]["deterministic_outputs"]
    for field in ("action", "reject_reason", "canonical_key"):
        deterministic.pop(field, None)
    for field in ("location_evidence", "graduation_evidence", "source_urls"):
        normalized["job"][field] = sorted(set(normalized["job"][field]))
    normalized["candidate_evidence"] = sorted(
        normalized["candidate_evidence"],
        key=lambda item: item["evidence_id"],
    )
    return normalized


def deterministic_location_decision(job: dict[str, Any]) -> str:
    location = str(job.get("location") or "").casefold().strip()
    if not location:
        return "UNCERTAIN"
    uncertain_markers = (
        "多地",
        "待定",
        "以岗位详情为准",
        "具体工作地点",
        "multiple locations",
        "various locations",
        "location tbd",
        "to be determined",
    )
    if any(marker in location for marker in uncertain_markers) or location in {
        "china",
        "中国",
    }:
        return "UNCERTAIN"
    if "深圳" in location or "shenzhen" in location:
        return "SHENZHEN_ALLOWED"
    return "NOT_SHENZHEN"


def deterministic_semantic_conflicts(
    job: dict[str, Any],
    result: dict[str, Any],
) -> list[dict[str, str]]:
    conflicts: list[dict[str, str]] = []
    deterministic_graduation = str(job.get("graduation_eligibility") or "").upper()
    deterministic_graduation = {
        "REJECT": "INELIGIBLE",
        "INELIGIBLE": "INELIGIBLE",
        "WATCH": "UNCERTAIN",
    }.get(deterministic_graduation, deterministic_graduation)
    semantic_graduation = str(result["graduation"]["decision"])
    if deterministic_graduation == "ELIGIBLE" and semantic_graduation == "INELIGIBLE":
        conflicts.append(
            {"field": "graduation", "deterministic": "ELIGIBLE", "semantic": "INELIGIBLE"}
        )
    if deterministic_graduation == "INELIGIBLE" and semantic_graduation == "ELIGIBLE":
        conflicts.append(
            {"field": "graduation", "deterministic": "INELIGIBLE", "semantic": "ELIGIBLE"}
        )

    deterministic_location = deterministic_location_decision(job)
    semantic_location = str(result["location"]["decision"])
    if (
        deterministic_location != "UNCERTAIN"
        and semantic_location != "UNCERTAIN"
        and deterministic_location != semantic_location
    ):
        conflicts.append(
            {
                "field": "location",
                "deterministic": deterministic_location,
                "semantic": semantic_location,
            }
        )

    deterministic_status = str(job.get("status") or "").upper()
    if deterministic_status == "EXPIRED":
        deterministic_status = "CLOSED"
    semantic_status = str(result["job_status"]["decision"])
    if (
        deterministic_status in {"OPEN", "CLOSED"}
        and semantic_status in {"OPEN", "CLOSED"}
        and deterministic_status != semantic_status
    ):
        conflicts.append(
            {
                "field": "job_status",
                "deterministic": deterministic_status,
                "semantic": semantic_status,
            }
        )

    deterministic_role = str(job.get("role_family") or "")
    semantic_role = str(result.get("primary_role_family") or "")
    if deterministic_role and semantic_role and deterministic_role != semantic_role:
        conflicts.append(
            {
                "field": "primary_role_family",
                "deterministic": deterministic_role,
                "semantic": semantic_role,
            }
        )
    return conflicts


def _cached_review(
    path: Path,
    *,
    input_hash: str,
    prompt_version: str,
    provider_contract_version: str,
    provider_name: str,
    model: str,
    evidence_ids: set[str],
    schema_version: str,
) -> dict[str, Any] | None:
    cached = read_json(path, {})
    if not isinstance(cached, dict):
        return None
    if (
        cached.get("input_hash") != input_hash
        or cached.get("prompt_version") != prompt_version
        or cached.get("provider_contract_version") != provider_contract_version
        or cached.get("provider") != provider_name
        or cached.get("model") != model
    ):
        return None
    try:
        validate_semantic_result(
            cached.get("result"),
            evidence_ids=evidence_ids,
            schema_version=schema_version,
        )
    except SemanticReviewValidationError:
        return None
    return cached


def run_semantic_review_batch(
    jobs: list[dict[str, Any]],
    candidate_evidence: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    selected_job_keys: set[str] | None = None,
    dry_run: bool = False,
    provider: SemanticJobReviewProvider | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    if not bool(config.get("enabled", True)):
        return {
            "selected": 0,
            "reviewed": 0,
            "cache_hits": 0,
            "failed": 0,
            "dry_run": bool(dry_run),
            "job_keys": [],
            "reviews": [],
        }
    triggers = list(config.get("trigger", ["READY", "MUST_APPLY", "USER_SELECTED"]))
    max_reviews = int(config.get("max_reviews_per_run", 3))
    selected = select_jobs_for_review(
        jobs,
        triggers=triggers,
        selected_job_keys=selected_job_keys or set(),
        max_reviews=max_reviews,
    )
    summary: dict[str, Any] = {
        "selected": len(selected),
        "reviewed": 0,
        "cache_hits": 0,
        "failed": 0,
        "dry_run": bool(dry_run),
        "job_keys": [str(job.get("canonical_key") or "") for job in selected],
        "reviews": [],
    }
    if dry_run or not selected:
        return summary

    provider_name = str(config.get("provider") or "deepseek").casefold()
    if provider_name != "deepseek":
        raise SemanticReviewConfigurationError(
            f"unsupported semantic review provider: {provider_name}"
        )
    if provider is None:
        provider = DeepSeekSemanticJobReviewProvider(
            model=str(config.get("model") or DEFAULT_MODEL),
            base_url=str(config.get("base_url") or DEFAULT_BASE_URL),
            timeout_seconds=float(config.get("timeout_seconds", 30)),
            max_output_tokens=int(
                config.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
            ),
            max_retries=int(config.get("max_retries", 2)),
            retry_backoff_seconds=float(config.get("retry_backoff_seconds", 0.5)),
        )

    prompt_version = str(config.get("prompt_version") or DEFAULT_PROMPT_VERSION)
    schema_version = str(config.get("schema_version") or DEFAULT_SCHEMA_VERSION)
    provider_contract_version = str(
        getattr(provider, "contract_version", DEFAULT_PROVIDER_CONTRACT_VERSION)
    )
    cache_dir = Path(str(config.get("cache_dir") or "data/job_cache/semantic_reviews"))
    for job in selected:
        review_input = build_review_input(
            job,
            candidate_evidence,
            max_input_chars=int(config.get("max_input_chars", 30000)),
        )
        evidence_ids = {
            str(item["evidence_id"])
            for item in review_input["candidate_evidence"]
        }
        cache_input = _cache_semantic_input(review_input)
        jd_hash = _semantic_hash(cache_input["job"])
        input_hash = _semantic_hash(
            {
                "prompt_version": prompt_version,
                "schema_version": schema_version,
                "provider_contract_version": provider_contract_version,
                "provider": provider.provider_name,
                "model": provider.model,
                "job_hash": jd_hash,
                "candidate_evidence": cache_input["candidate_evidence"],
            }
        )
        cache_path = cache_dir / f"{input_hash}.json"
        cached = _cached_review(
            cache_path,
            input_hash=input_hash,
            prompt_version=prompt_version,
            provider_contract_version=provider_contract_version,
            provider_name=provider.provider_name,
            model=provider.model,
            evidence_ids=evidence_ids,
            schema_version=schema_version,
        )
        if cached is not None:
            conflicts = deterministic_semantic_conflicts(job, cached["result"])
            requires_human_review = semantic_result_requires_human_review(
                cached["result"], conflicts
            )
            cached["conflicts"] = conflicts
            cached["requires_human_review"] = requires_human_review
            job["semantic_review_status"] = "CACHE_HIT"
            job["semantic_conflicts"] = conflicts
            job["requires_human_review"] = requires_human_review
            write_json(cache_path, cached)
            summary["cache_hits"] += 1
            summary["reviews"].append({**cached, "cache_hit": True})
            continue

        try:
            result, usage = provider.review(
                review_input,
                schema_version=schema_version,
                evidence_ids=evidence_ids,
            )
        except (SemanticReviewAPIError, SemanticReviewProviderOutputError) as exc:
            # A bounded daily batch must retain strict output validation without
            # allowing one provider/API failure to abort unrelated discovery,
            # scoring, document generation, and the local application pack.
            # Configuration/input-contract errors still fail the run.
            diagnostics = dict(getattr(exc, "diagnostics", {}) or {})
            failure = {
                "status": "FAILED",
                "input_job_canonical_key": str(job.get("canonical_key") or ""),
                "error": str(exc),
                "diagnostics": diagnostics,
                "requires_human_review": True,
                "cache_written": False,
            }
            job["semantic_review_status"] = "FAILED"
            job["semantic_review_error"] = str(exc)
            job["requires_human_review"] = True
            summary["failed"] += 1
            summary["reviews"].append(failure)
            continue
        result = validate_semantic_result(
            result,
            evidence_ids=evidence_ids,
            schema_version=schema_version,
        )
        conflicts = deterministic_semantic_conflicts(job, result)
        requires_human_review = semantic_result_requires_human_review(result, conflicts)
        stored = {
            "schema_version": schema_version,
            "provider": provider.provider_name,
            "model": provider.model,
            "reviewed_at": now().isoformat(),
            "prompt_version": prompt_version,
            "provider_contract_version": provider_contract_version,
            "input_job_canonical_key": str(job.get("canonical_key") or ""),
            "jd_hash": jd_hash,
            "input_hash": input_hash,
            "result": result,
            "conflicts": conflicts,
            "requires_human_review": requires_human_review,
            "usage": usage,
        }
        job["semantic_review_status"] = "SUCCESS"
        job["semantic_conflicts"] = conflicts
        job["requires_human_review"] = requires_human_review
        write_json(cache_path, stored)
        summary["reviewed"] += 1
        summary["reviews"].append({**stored, "cache_hit": False})
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run bounded semantic review for selected jobs.")
    parser.add_argument("--config", default="config/targets.yaml")
    parser.add_argument("--jobs", default="data/job_cache/decisioned_jobs.json")
    parser.add_argument(
        "--candidate-evidence",
        default="data/job_cache/semantic_candidate_evidence.json",
    )
    parser.add_argument("--select-job-key", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true", help="Select only; make no API calls")
    parser.add_argument(
        "--summary-output",
        default="data/job_cache/semantic_review_run.json",
        help="Local per-run review/cache/failure summary",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    targets = load_config(args.config)
    config = targets.get("semantic_review", {})
    if not bool(config.get("enabled", False)):
        print("Semantic review is disabled; no API calls made.")
        return 0
    jobs = read_json(args.jobs, [])
    if not isinstance(jobs, list):
        raise SemanticReviewValidationError("semantic review jobs input must be a list")
    evidence_path = Path(args.candidate_evidence)
    raw_evidence = read_json(evidence_path, []) if evidence_path.exists() else []
    candidate_evidence = prepare_candidate_evidence(
        raw_evidence,
        max_items=int(config.get("max_candidate_evidence_items", 40)),
    )
    summary = run_semantic_review_batch(
        [job for job in jobs if isinstance(job, dict)],
        candidate_evidence,
        config,
        selected_job_keys=set(args.select_job_key),
        dry_run=args.dry_run,
    )
    write_json(args.summary_output, summary)
    # Persist only system-owned review status flags added above. The underlying
    # deterministic actions and source facts are not changed.
    write_json(args.jobs, jobs)
    print(
        "Semantic review: "
        f"selected={summary['selected']}, reviewed={summary['reviewed']}, "
        f"cache_hits={summary['cache_hits']}, failed={summary['failed']}, "
        f"dry_run={summary['dry_run']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
