#!/usr/bin/env python3
"""Validated adapter for evidence-grounded Chinese resume edits.

The external resume-optimizer skill is an editor, not a fact source.  This
module applies its structured edit records to an existing Resume V2 content
plan and rejects records that escape the selected material/evidence scope.
It does not call a model or change candidate materials.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Callable


ALLOWED_ACTIONS = frozenset({"KEEP", "REWRITE", "EXPAND", "COMPRESS", "MERGE", "DELETE"})
ALLOWED_AI_STYLE_RISKS = frozenset({"LOW", "MEDIUM", "HIGH"})
REQUIRED_EDIT_FIELDS = frozenset(
    {
        "section",
        "material_id",
        "intent_id",
        "evidence_ids",
        "original_text",
        "action",
        "revised_text",
        "reason",
        "jd_keywords_covered",
        "new_fact_introduced",
        "estimated_cjk_delta",
        "ai_style_risk",
    }
)


class ResumeOptimizerValidationError(ValueError):
    """Raised when an editor record cannot be grounded in the content plan."""


def _cjk_count(value: str) -> int:
    return len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", value))


def _number_tokens(value: str) -> set[str]:
    return set(re.findall(r"(?<![A-Za-z])\d+(?:\.\d+)?(?:/\d+)?", value))


@dataclass
class _EditTarget:
    section: str
    material_id: str
    intent_id: str
    original_text: str
    evidence_ids: list[str]
    allowed_evidence_ids: set[str]
    support_text: str
    apply: Callable[[dict[str, Any]], None]


def _material_support(material: dict[str, Any]) -> str:
    values: list[str] = []
    for key in (
        "bullets",
        "bullets_cn",
        "bullets_expanded",
        "bullets_expanded_cn",
        "details",
        "details_cn",
    ):
        raw = material.get(key, [])
        if isinstance(raw, list):
            values.extend(str(value) for value in raw)
    return " ".join(values)


def _build_targets(plan: dict[str, Any]) -> dict[str, _EditTarget]:
    targets: dict[str, _EditTarget] = {}

    for slot in plan.get("education", []):
        material = slot["selected_material"]
        allowed = set(slot.get("evidence_ids", []))
        for index, detail in enumerate(slot.get("details", {}).get("CN", [])):
            intent_id = f"{slot['intent_id']}_detail_{index + 1}"

            def apply_education(record: dict[str, Any], *, target_slot=slot, target_index=index) -> None:
                target_slot["details"]["CN"][target_index] = record["revised_text"]

            targets[intent_id] = _EditTarget(
                section="education",
                material_id=str(slot["material_id"]),
                intent_id=intent_id,
                original_text=str(detail),
                evidence_ids=list(slot.get("evidence_ids", [])),
                allowed_evidence_ids=allowed,
                support_text=_material_support(material),
                apply=apply_education,
            )

    for section in ("experience", "projects"):
        for group in plan.get(section, []):
            material = group["material"]
            allowed = set(material.get("source_evidence_ids", material.get("evidence_ids", [])))
            for collection_name in ("intents", "reserve_intents"):
                for intent in group.get(collection_name, []):
                    intent_id = str(intent["intent_id"])
                    original = str(intent["variants"]["CN"][intent["variant"]])

                    def apply_bullet(
                        record: dict[str, Any],
                        *, target_group=group,
                        target_intent=intent,
                        source_collection=collection_name,
                    ) -> None:
                        action = record["action"]
                        if action == "DELETE":
                            if target_intent in target_group[source_collection]:
                                target_group[source_collection].remove(target_intent)
                            return
                        target_intent["evidence_ids"] = list(record["evidence_ids"])
                        for variant in ("compact", "standard", "expanded"):
                            target_intent["variants"]["CN"][variant] = record["revised_text"]
                        target_intent["variant"] = "standard"
                        if source_collection == "reserve_intents":
                            target_group["reserve_intents"].remove(target_intent)
                            target_intent["optional"] = False
                            target_intent["reserve"] = False
                            target_group["intents"].append(target_intent)
                            target_group["intents"].sort(key=lambda item: item["priority"])

                    targets[intent_id] = _EditTarget(
                        section=section,
                        material_id=str(intent["material_id"]),
                        intent_id=intent_id,
                        original_text=original,
                        evidence_ids=list(intent.get("evidence_ids", [])),
                        allowed_evidence_ids=allowed,
                        support_text=" ".join(
                            [
                                str(intent.get("factual_scope", "")),
                                *[
                                    str(value)
                                    for language in ("CN", "EN")
                                    for value in intent.get("variants", {}).get(language, {}).values()
                                ],
                                _material_support(material),
                            ]
                        ),
                        apply=apply_bullet,
                    )

            if section == "projects":
                context_id = f"{material['id']}_context"

                def apply_context(record: dict[str, Any], *, target_group=group) -> None:
                    target_group["editor_context"] = {
                        "intent_id": record["intent_id"],
                        "material_id": record["material_id"],
                        "evidence_ids": list(record["evidence_ids"]),
                        "CN": record["revised_text"],
                    }

                targets[context_id] = _EditTarget(
                    section="project_context",
                    material_id=str(material["id"]),
                    intent_id=context_id,
                    original_text="",
                    evidence_ids=[],
                    allowed_evidence_ids=allowed,
                    support_text=_material_support(material),
                    apply=apply_context,
                )
    return targets


def _evidence_support(plan: dict[str, Any], evidence_ids: list[str]) -> str:
    index = plan.get("evidence_index", {})
    values: list[str] = []
    for evidence_id in evidence_ids:
        item = index.get(evidence_id, {})
        values.append(str(item.get("factual_description", "")))
        values.extend(str(value) for value in item.get("technologies", []))
        values.extend(str(value) for value in item.get("verified_metrics", []))
    return " ".join(values)


def _validate_record(
    plan: dict[str, Any],
    record: dict[str, Any],
    target: _EditTarget,
) -> None:
    missing = sorted(REQUIRED_EDIT_FIELDS - set(record))
    if missing:
        raise ResumeOptimizerValidationError(
            f"{target.intent_id} is missing fields: {', '.join(missing)}"
        )
    if record["section"] != target.section:
        raise ResumeOptimizerValidationError(f"{target.intent_id} section does not match target")
    if str(record["material_id"]) != target.material_id:
        raise ResumeOptimizerValidationError(f"{target.intent_id} material_id does not match target")
    if str(record["original_text"]) != target.original_text:
        raise ResumeOptimizerValidationError(f"{target.intent_id} original_text is stale or incorrect")
    if record["action"] not in ALLOWED_ACTIONS:
        raise ResumeOptimizerValidationError(f"{target.intent_id} action is invalid")
    if record["ai_style_risk"] not in ALLOWED_AI_STYLE_RISKS:
        raise ResumeOptimizerValidationError(f"{target.intent_id} ai_style_risk is invalid")
    if record["new_fact_introduced"] is not False:
        raise ResumeOptimizerValidationError(f"{target.intent_id} introduces a new fact")
    if not isinstance(record["reason"], str) or not record["reason"].strip():
        raise ResumeOptimizerValidationError(f"{target.intent_id} reason must be non-empty")
    if not isinstance(record["jd_keywords_covered"], list) or not all(
        isinstance(value, str) for value in record["jd_keywords_covered"]
    ):
        raise ResumeOptimizerValidationError(f"{target.intent_id} jd_keywords_covered is invalid")

    evidence_ids = record["evidence_ids"]
    bank = set(plan.get("evidence_bank_ids", []))
    if not isinstance(evidence_ids, list) or not evidence_ids:
        raise ResumeOptimizerValidationError(f"{target.intent_id} must retain evidence IDs")
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ResumeOptimizerValidationError(f"{target.intent_id} contains duplicate evidence IDs")
    if not set(evidence_ids).issubset(bank):
        raise ResumeOptimizerValidationError(f"{target.intent_id} references unknown evidence")
    if not set(evidence_ids).issubset(target.allowed_evidence_ids):
        raise ResumeOptimizerValidationError(
            f"{target.intent_id} escapes the source material evidence scope"
        )

    revised = str(record["revised_text"])
    if record["action"] == "DELETE":
        if revised.strip():
            raise ResumeOptimizerValidationError(f"{target.intent_id} DELETE must have empty revised_text")
    elif not revised.strip():
        raise ResumeOptimizerValidationError(f"{target.intent_id} revised_text must be non-empty")
    if record["action"] == "KEEP" and revised != target.original_text:
        raise ResumeOptimizerValidationError(f"{target.intent_id} KEEP changed the wording")

    actual_delta = _cjk_count(revised) - _cjk_count(target.original_text)
    if type(record["estimated_cjk_delta"]) is not int or record["estimated_cjk_delta"] != actual_delta:
        raise ResumeOptimizerValidationError(
            f"{target.intent_id} estimated_cjk_delta must equal {actual_delta}"
        )
    support_text = " ".join(
        [target.support_text, _evidence_support(plan, evidence_ids), target.original_text]
    )
    unsupported_numbers = _number_tokens(revised) - _number_tokens(support_text)
    if unsupported_numbers:
        raise ResumeOptimizerValidationError(
            f"{target.intent_id} introduces unsupported numeric claims: "
            f"{', '.join(sorted(unsupported_numbers))}"
        )


def apply_resume_optimizer_edits(
    plan: dict[str, Any],
    payload: dict[str, Any],
    *,
    job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and apply one bounded Chinese editing pass to a Resume V2 plan."""
    if not isinstance(payload, dict) or not isinstance(payload.get("edits"), list):
        raise ResumeOptimizerValidationError("resume-optimizer payload must contain an edits list")
    identity = payload.get("job_identity", {})
    if job is not None and identity:
        for key in ("company", "title", "location"):
            actual = job.get(key) or (job.get("position") if key == "title" else "")
            if str(identity.get(key, "")) != str(actual or ""):
                raise ResumeOptimizerValidationError(f"resume-optimizer job {key} does not match")

    working = copy.deepcopy(plan)
    targets = _build_targets(working)
    seen: set[str] = set()
    for record in payload["edits"]:
        if not isinstance(record, dict):
            raise ResumeOptimizerValidationError("every resume-optimizer edit must be an object")
        intent_id = str(record.get("intent_id", ""))
        if intent_id in seen:
            raise ResumeOptimizerValidationError(f"duplicate resume-optimizer edit: {intent_id}")
        if intent_id not in targets:
            raise ResumeOptimizerValidationError(f"unknown resume-optimizer intent: {intent_id}")
        seen.add(intent_id)
        target = targets[intent_id]
        _validate_record(working, record, target)
        target.apply(record)

    minimum = int(working["blueprint"]["project_bullets"]["min"])
    if any(len(group["intents"]) < minimum for group in working.get("projects", [])):
        raise ResumeOptimizerValidationError("editor removed too many project bullets")
    experience_minimum = int(working["blueprint"]["experience_bullets"]["min"])
    if any(len(group["intents"]) < experience_minimum for group in working.get("experience", [])):
        raise ResumeOptimizerValidationError("editor removed too many experience bullets")

    forbidden_terms = [str(value) for value in payload.get("forbidden_unsupported_terms", [])]
    rendered_claims = [
        intent["variants"]["CN"][intent["variant"]]
        for section in ("experience", "projects")
        for group in working.get(section, [])
        for intent in group.get("intents", [])
    ]
    rendered_claims.extend(
        str(group.get("editor_context", {}).get("CN", ""))
        for group in working.get("projects", [])
    )
    claim_text = " ".join(rendered_claims).casefold()
    present = [term for term in forbidden_terms if term.casefold() in claim_text]
    if present:
        raise ResumeOptimizerValidationError(
            "editor introduced unsupported JD terms: " + ", ".join(present)
        )

    working["resume_optimizer"] = {
        "skill": str(payload.get("skill", "resume-optimizer")),
        "skill_source": str(payload.get("skill_source", "")),
        "edit_records": copy.deepcopy(payload["edits"]),
        "critic_findings": copy.deepcopy(payload.get("critic_findings", [])),
    }
    return working


def editor_claim_intents(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Return project-context claims added by the editor for factual gates."""
    return [
        group["editor_context"]
        for group in plan.get("projects", [])
        if group.get("editor_context", {}).get("CN")
    ]
