#!/usr/bin/env python3
"""Receive Feishu bot messages that mean "the user already applied".

The module deliberately keeps the transport, evidence extraction and durable
state separate.  It can be used as a small HTTPS-facing adapter behind a
reverse proxy, or fed one saved event with ``--input`` for local validation.
It never submits an application to a recruitment site.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import requests

try:
    from common import load_config, normalize_token, normalize_whitespace
    from job_schema import canonicalize_url
    from update_feishu import (
        FeishuAPIError,
        FeishuConfig,
        _coerce_value,
        assert_frozen_schema_compatible,
        extract_url_value,
        feishu_request,
        get_field_definitions,
        get_tenant_access_token,
        load_feishu_config,
    )
except ModuleNotFoundError:
    from scripts.common import load_config, normalize_token, normalize_whitespace
    from scripts.job_schema import canonicalize_url
    from scripts.update_feishu import (
        FeishuAPIError,
        FeishuConfig,
        _coerce_value,
        assert_frozen_schema_compatible,
        extract_url_value,
        feishu_request,
        get_field_definitions,
        get_tenant_access_token,
        load_feishu_config,
    )


URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
IMAGE_MESSAGE_TYPES = {"image", "post"}
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_PAGE_BYTES = 2 * 1024 * 1024
DO_NOT_APPLY_RE = re.compile(r"(?:我不投|不投了|不考虑(?:这个岗位)?|不要投|do\s+not\s+apply|won't\s+apply)", re.I)
DATE_RE = re.compile(
    r"(?:投递时间|申请时间|applied(?:\s+at)?|submitted(?:\s+at)?)\s*[:：]?\s*"
    r"(20\d{2}[-/]\d{1,2}[-/]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?)",
    re.I,
)
FIELD_PATTERNS = {
    "company": re.compile(r"(?:公司|企业|雇主|招聘单位|company|employer)\s*[:：]\s*([^\n|｜]{1,160})", re.I),
    "title": re.compile(r"(?:职位名称|职位|岗位名称|岗位|招聘职位|title|position|role)\s*[:：]\s*([^\n|｜]{1,220})", re.I),
    "location": re.compile(r"(?:地点|工作地点|城市|location|city)\s*[:：]\s*([^\n|｜]{1,120})", re.I),
}
IMAGE_TITLE_HINT_RE = re.compile(
    r"(?:20\d{2}|应届|校招|储备生|工程师|开发|测试|后端|前端|算法|软件|技术|AI|Agent|机器人)",
    re.I,
)
IMAGE_COMPANY_HINT_RE = re.compile(
    r"(?:公司|集团|科技|股份|有限公司|有限|BGI|华大|创新|机器人|银行|大学)",
    re.I,
)
IMAGE_NAV_RE = re.compile(
    r"^(?:首页|社会招聘|校园招聘|实习生招聘|客服|联培|Join\s+Cultivation|"
    r"campus|careers?|jobs?|home|login|退出|收藏)$",
    re.I,
)

# Public job boards are evidence sources, not official employer URLs.  Known
# ATS hosts are treated as official career endpoints; arbitrary hosts require
# an explicit company-domain match before being promoted to Official URL.
PUBLIC_JOB_HOSTS = {
    "zhipin.com", "bosszhipin.com", "nowcoder.com", "ncss.cn", "iguopin.com",
    "liepin.com", "linkedin.com", "indeed.com", "jobui.com", "51job.com",
}
OFFICIAL_ATS_HOSTS = {
    "myworkdayjobs.com", "zhiye.com", "mokahr.com", "hotjob.cn",
    "smartrecruiters.com", "greenhouse.io", "lever.co", "ashbyhq.com",
    "jobs.ashbyhq.com",
}

IDENTITY_CONFIDENCE_VALUES = {"HIGH", "MEDIUM", "LOW", "UNCERTAIN"}
IDENTITY_RESULT_KEYS = {"company", "title", "location", "confidence", "evidence"}
OCR_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s()\-]{7,}\d)(?!\w)")
OCR_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _event_time(value: Any) -> str:
    text = str(value or "").strip()
    if text.isdigit():
        number = int(text)
        if number > 10**12:
            number //= 1000
        return datetime.fromtimestamp(number, tz=timezone.utc).replace(microsecond=0).isoformat()
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.replace(microsecond=0).isoformat()
        except ValueError:
            pass
    return _now_iso()


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except (TypeError, ValueError):
            return {"text": value}
        return loaded if isinstance(loaded, dict) else {"text": value}
    return {}


def extract_urls(*values: str) -> list[str]:
    found: list[str] = []
    for value in values:
        for raw in URL_RE.findall(str(value or "")):
            cleaned = raw.rstrip(".,;!?)]}>。！？；，、")
            canonical = canonicalize_url(cleaned) or cleaned
            if canonical not in found:
                found.append(canonical)
    return found


def extract_image_keys(message: dict[str, Any], content: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    direct = content.get("image_key") or content.get("imageKey")
    if isinstance(direct, str) and direct.strip():
        keys.append(direct.strip())
    for item in content.get("content", []) if isinstance(content.get("content"), list) else []:
        if isinstance(item, dict):
            key = item.get("image_key") or item.get("imageKey")
            if isinstance(key, str) and key.strip():
                keys.append(key.strip())
    if str(message.get("message_type") or "").lower() == "image":
        key = content.get("image_key")
        if isinstance(key, str) and key.strip():
            keys.append(key.strip())
    return list(dict.fromkeys(keys))


@dataclass
class MessageEnvelope:
    event_id: str
    message_id: str
    chat_id: str
    chat_type: str
    sender_open_id: str
    sent_at: str
    text: str
    urls: list[str] = field(default_factory=list)
    image_keys: list[str] = field(default_factory=list)
    raw_message_type: str = ""


class InboxStateError(RuntimeError):
    """The durable inbox state is unreadable or violates its contract."""


def parse_message_event(payload: dict[str, Any]) -> MessageEnvelope | None:
    event = payload.get("event") if isinstance(payload, dict) else None
    if not isinstance(event, dict):
        return None
    message = event.get("message") or {}
    if not isinstance(message, dict):
        return None
    content = _json_object(message.get("content"))
    text = normalize_whitespace(str(content.get("text") or content.get("content") or ""))
    image_keys = extract_image_keys(message, content)
    urls = extract_urls(text)
    sender = event.get("sender") or {}
    sender_id = sender.get("sender_id") if isinstance(sender, dict) else {}
    return MessageEnvelope(
        event_id=str((payload.get("header") or {}).get("event_id") or event.get("event_id") or ""),
        message_id=str(message.get("message_id") or ""),
        chat_id=str(message.get("chat_id") or ""),
        chat_type=str(message.get("chat_type") or ""),
        sender_open_id=str((sender_id or {}).get("open_id") or ""),
        sent_at=_event_time(message.get("create_time") or event.get("create_time")),
        text=text,
        urls=urls,
        image_keys=image_keys,
        raw_message_type=str(message.get("message_type") or ""),
    )


class _PageTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title: list[str] = []
        self.meta: dict[str, str] = {}
        self.visible: list[str] = []
        self._in_title = False
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key.casefold(): str(value or "") for key, value in attrs}
        if tag.casefold() == "title":
            self._in_title = True
        if tag.casefold() in {"script", "style", "noscript", "svg"}:
            self._skip += 1
        if tag.casefold() == "meta":
            name = attrs_dict.get("name") or attrs_dict.get("property")
            content = attrs_dict.get("content")
            if name and content:
                self.meta[name.casefold()] = html.unescape(content)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "title":
            self._in_title = False
        if tag.casefold() in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        value = normalize_whitespace(html.unescape(data))
        if not value:
            return
        if self._in_title:
            self.title.append(value)
        if not self._skip:
            self.visible.append(value)


def parse_public_page(url: str, *, timeout: float = 15.0) -> dict[str, Any]:
    """Fetch only public page content; login/CAPTCHA pages remain evidence-only."""

    try:
        response = requests.get(
            url,
            headers={"User-Agent": "track-job-application-inbox/1.0"},
            timeout=timeout,
            stream=True,
        )
        content_type = str(response.headers.get("content-type") or "")
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > MAX_PAGE_BYTES:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
        text = raw.decode(response.encoding or "utf-8", errors="replace")
        parser = _PageTextParser()
        if "html" in content_type or "xml" in content_type or "<html" in text[:1000].casefold():
            parser.feed(text)
            page_text = "\n".join(parser.visible)
            title = " ".join(parser.title)
            meta = parser.meta
        else:
            page_text, title, meta = normalize_whitespace(text), "", {}
        return {
            "url": url,
            "status_code": response.status_code,
            "content_type": content_type,
            "title": title,
            "meta": meta,
            "text": page_text[:120000],
            "restricted": response.status_code in {401, 403, 429} or "captcha" in text[:4000].casefold(),
            "error": "",
        }
    except requests.RequestException as exc:
        return {"url": url, "status_code": 0, "content_type": "", "title": "", "meta": {}, "text": "", "restricted": False, "error": type(exc).__name__}


def extract_labeled_fields(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for field_name, pattern in FIELD_PATTERNS.items():
        match = pattern.search(text or "")
        if match:
            result[field_name] = normalize_whitespace(match.group(1).strip(" ：:|｜"))
    return result


def infer_image_identity(text: str) -> dict[str, str]:
    """Infer only strong heading-like company/title lines from OCR text."""

    lines = [normalize_whitespace(item) for item in str(text or "").splitlines()]
    lines = [item for item in lines if item and not IMAGE_NAV_RE.fullmatch(item)]
    if not lines:
        return {}
    title_candidates = [
        (index, line)
        for index, line in enumerate(lines)
        if IMAGE_TITLE_HINT_RE.search(line) and len(line) <= 180
    ]
    if not title_candidates:
        return {}
    title_index, title = max(
        title_candidates,
        key=lambda item: (
            bool(re.search(r"20\d{2}|应届|校招", item[1], re.I)),
            bool(re.search(r"储备生|工程师|开发|测试|后端|前端|算法|软件|技术", item[1], re.I)),
            -len(item[1]),
        ),
    )
    company = ""
    company_candidates = [
        (index, line)
        for index, line in enumerate(lines[: max(title_index, 1)])
        if line != title and len(line) <= 100 and IMAGE_COMPANY_HINT_RE.search(line)
    ]
    if company_candidates:
        company = min(company_candidates, key=lambda item: (abs(title_index - item[0]), len(item[1])))[1]
    return {key: value for key, value in (("company", company), ("title", title)) if value}


def _extract_responses_text(payload: dict[str, Any]) -> str:
    """Read text from the DeepSeek Responses API without trusting extra fields."""

    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    chunks: list[str] = []
    for item in payload.get("output", []) if isinstance(payload.get("output"), list) else []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) if isinstance(item.get("content"), list) else []:
            if not isinstance(content, dict):
                continue
            value = content.get("text") or content.get("value")
            if isinstance(value, str) and value.strip():
                chunks.append(value.strip())
    return "\n".join(chunks)


def _redact_ocr_for_model(text: str, limit: int = 12000) -> str:
    """Keep OCR useful for identity extraction while removing contact data."""

    redacted = OCR_EMAIL_RE.sub("[EMAIL]", str(text or ""))
    redacted = OCR_PHONE_RE.sub("[PHONE]", redacted)
    return redacted[: max(1000, int(limit))]


def _image_data_url(path: str | Path) -> str:
    """Build a bounded data URL for DeepSeek vision input."""

    try:
        raw = Path(path).read_bytes()
    except OSError:
        return ""
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        return ""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif raw.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif raw.startswith((b"GIF87a", b"GIF89a")):
        mime = "image/gif"
    elif raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        return ""
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


class DeepSeekIdentityEnricher:
    """Conservatively normalize noisy OCR into a job identity.

    It can send both redacted OCR text and the original screenshot to
    DeepSeek's vision-capable ``deepseek-flash`` model. A failed or unavailable
    provider always falls back to the deterministic OCR path; it can never
    turn a message into a fabricated application record.
    """

    def __init__(self, settings: dict[str, Any]) -> None:
        self.settings = settings
        self.enabled = bool(settings.get("enabled", True))
        self.provider = str(settings.get("provider") or "deepseek").casefold()
        self.api_key = str(os.getenv("DEEPSEEK_API_KEY") or "").strip()
        self.base_url = str(settings.get("base_url") or DEFAULT_DEEPSEEK_BASE_URL).rstrip("/")
        # DeepSeek's current Responses API model id is ``deepseek-flash``;
        # ``deepseek-v4-flash`` was used by an older internal config and is
        # rejected by the live API.
        self.model = str(settings.get("model") or "deepseek-flash")
        self.send_image = bool(settings.get("send_image", True))
        self.timeout_seconds = max(5.0, float(settings.get("timeout_seconds", 20)))
        self.max_retries = max(0, min(2, int(settings.get("max_retries", 1))))
        self.max_input_chars = max(1000, int(settings.get("max_input_chars", 12000)))

    def __call__(
        self,
        ocr_text: str,
        deterministic: dict[str, str] | None = None,
        image_paths: list[str] | None = None,
    ) -> dict[str, str]:
        if self.provider != "deepseek":
            _long_connection_debug(f"identity enrichment skipped: unsupported provider={self.provider}")
            return {}
        if not self.enabled or not self.api_key or not str(ocr_text or "").strip():
            if self.enabled and not self.api_key:
                _long_connection_debug("identity enrichment skipped: DEEPSEEK_API_KEY is not set")
            return {}
        if self.base_url != DEFAULT_DEEPSEEK_BASE_URL:
            _long_connection_debug("identity enrichment skipped: unsupported DeepSeek base URL")
            return {}
        deterministic = deterministic or {}
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "company": {"type": "string"},
                "title": {"type": "string"},
                "location": {"type": "string"},
                "confidence": {"type": "string", "enum": sorted(IDENTITY_CONFIDENCE_VALUES)},
                "evidence": {"type": "string"},
            },
            "required": ["company", "title", "location", "confidence", "evidence"],
        }
        instruction = (
            "Extract the company and job title from noisy OCR of a job screenshot. "
            "Correct obvious OCR substitutions only when supported by the text; do not guess "
            "a company or title that is not evidenced. Ignore navigation, phone numbers, "
            "dates and application buttons. Return exactly one JSON object. Use empty strings "
            "for unknown fields and confidence UNCERTAIN when the text is insufficient. "
            "The deterministic OCR candidate is only a hint and may be wrong."
        )
        user_input = json.dumps(
            {
                "ocr_text": _redact_ocr_for_model(ocr_text, self.max_input_chars),
                "deterministic_candidate": {
                    key: str(value or "") for key, value in deterministic.items() if key in {"company", "title", "location"}
                },
            },
            ensure_ascii=False,
        )
        input_text = "Return the identity JSON for this OCR:\n" + user_input
        input_value: str | list[dict[str, Any]] = input_text
        if self.send_image and image_paths:
            image_url = _image_data_url(image_paths[0])
            if image_url:
                input_value = [{
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": input_text},
                        {"type": "input_image", "image_url": image_url, "detail": "high"},
                    ],
                }]
        body = {
            "model": self.model,
            "instructions": instruction,
            "input": input_value,
            "text": {"format": {"type": "json_schema", "name": "job_identity", "schema": schema}},
            "reasoning": {"effort": "none"},
            "max_output_tokens": 512,
        }
        for attempt in range(self.max_retries + 1):
            try:
                response = requests.post(
                    f"{self.base_url}/responses",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json=body,
                    timeout=self.timeout_seconds,
                )
                if response.status_code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                    time.sleep(0.5 * (2**attempt))
                    continue
                if not 200 <= response.status_code < 300:
                    _long_connection_debug(f"identity enrichment HTTP status={response.status_code}")
                    return {}
                payload = response.json()
                raw = _extract_responses_text(payload if isinstance(payload, dict) else {})
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    return {}
                result = {
                    key: normalize_whitespace(str(parsed.get(key) or ""))
                    for key in ("company", "title", "location", "confidence", "evidence")
                }
                if result["confidence"] not in IDENTITY_CONFIDENCE_VALUES:
                    result["confidence"] = "UNCERTAIN"
                if result["confidence"] == "UNCERTAIN" or not (result["company"] and result["title"]):
                    return {}
                return result
            except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
                if attempt < self.max_retries:
                    time.sleep(0.5 * (2**attempt))
                    continue
                _long_connection_debug(f"identity enrichment unavailable: {type(exc).__name__}")
                return {}
        return {}


def infer_page_fields(page: dict[str, Any]) -> dict[str, str]:
    text = "\n".join(
        item
        for item in (
            page.get("title"),
            (page.get("meta") or {}).get("og:site_name"),
            (page.get("meta") or {}).get("og:title"),
            page.get("text"),
        )
        if item
    )
    fields = extract_labeled_fields(text)
    title = str(page.get("title") or "").strip()
    if not fields.get("title") and title:
        parts = [normalize_whitespace(item) for item in re.split(r"\s*[|｜—–-]\s*", title) if normalize_whitespace(item)]
        if len(parts) >= 2:
            fields["title"] = parts[0]
            fields["company"] = fields.get("company") or parts[-1]
        elif parts:
            fields["title"] = parts[0]
    return fields


def is_official_job_url(url: str, page: dict[str, Any], company: str = "") -> bool:
    """Conservatively classify a user URL as an official career endpoint."""

    host = (urlparse(url).hostname or "").casefold().strip(".")
    if not host:
        return False
    if any(host == blocked or host.endswith("." + blocked) for blocked in PUBLIC_JOB_HOSTS):
        return False
    if any(host == ats or host.endswith("." + ats) for ats in OFFICIAL_ATS_HOSTS):
        return True
    company_token = normalize_token(company).replace(" ", "")
    host_token = normalize_token(host).replace(" ", "")
    if company_token and len(company_token) >= 4 and company_token in host_token:
        return True
    page_title = normalize_token(str(page.get("title") or "")).replace(" ", "")
    return bool(company_token and len(company_token) >= 4 and company_token in page_title and ("job" in host or "career" in host))


def parse_applied_at(text: str, default: str) -> tuple[str, bool]:
    match = DATE_RE.search(text or "")
    if not match:
        return default, True
    raw = match.group(1).replace("/", "-")
    try:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.replace(microsecond=0).isoformat(), False
    except ValueError:
        return raw, False


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value)[:100] or "evidence"


def _pending_fingerprint(envelope: MessageEnvelope, image_paths: list[str]) -> str:
    parts = [normalize_whitespace(envelope.text), *envelope.urls, *envelope.image_keys]
    for path in image_paths:
        try:
            parts.append(hashlib.sha256(Path(path).read_bytes()).hexdigest())
        except OSError:
            parts.append(str(path))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:20]


class InboxStore:
    """Small atomic JSON store for events, applications and pending evidence."""

    def __init__(self, path: str | Path, evidence_dir: str | Path | None = None) -> None:
        self.path = Path(path)
        self.evidence_dir = Path(evidence_dir or self.path.parent / "feishu_inbox_evidence")
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": 1, "events": {}, "applications": {}, "pending": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise InboxStateError(f"cannot read inbox state safely: {self.path}") from exc
        if not isinstance(value, dict):
            raise InboxStateError(f"inbox state must be an object: {self.path}")
        for key in ("events", "applications", "pending"):
            if not isinstance(value.get(key), dict):
                raise InboxStateError(f"inbox state field {key!r} is invalid: {self.path}")
        value.setdefault("schema_version", 1)
        return value

    def write(self, value: dict[str, Any]) -> None:
        temporary = self.path.with_name(self.path.name + ".tmp")
        encoded = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        with temporary.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    @property
    def jobs_path(self) -> Path:
        return self.path.with_name("feishu_application_jobs.json")

    def write_jobs(self, value: dict[str, Any]) -> None:
        jobs = [
            item.get("job")
            for item in value.get("applications", {}).values()
            if isinstance(item, dict) and isinstance(item.get("job"), dict)
        ]
        self.jobs_path.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")

    def save_evidence(self, event_id: str, image_key: str, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        destination = self.evidence_dir / f"{_safe_filename(event_id or 'event')}-{digest[:16]}.bin"
        if not destination.exists():
            destination.write_bytes(data)
        return str(destination)

    def processing_payloads(self) -> list[dict[str, Any]]:
        state = self.read()
        payloads: list[dict[str, Any]] = []
        seen_messages: set[str] = set()
        for item in state["events"].values():
            if not isinstance(item, dict) or str(item.get("status") or "").upper() != "PROCESSING":
                continue
            payload = item.get("raw_event")
            message_id = str(item.get("message_id") or "")
            if isinstance(payload, dict) and message_id not in seen_messages:
                payloads.append(payload)
                seen_messages.add(message_id)
        return payloads


@dataclass
class ProcessResult:
    status: str
    message: str
    canonical_key: str = ""
    record_id: str = ""
    pending_id: str = ""
    duplicate: bool = False


class FeishuInboxClient:
    def __init__(
        self,
        config: FeishuConfig,
        *,
        record_link_template: str = "",
    ) -> None:
        self.config = config
        self.record_link_template = record_link_template
        self._token: str | None = None

    def token(self) -> str:
        self._token = self._token or get_tenant_access_token(self.config)
        return self._token

    def list_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            payload = feishu_request(
                "GET", self.token(),
                f"/open-apis/bitable/v1/apps/{self.config.app_token}/tables/{self.config.table_id}/records",
                params=params,
            )
            data = payload.get("data", {})
            records.extend(data.get("items", []))
            if not data.get("has_more"):
                return records
            page_token = str(data.get("page_token") or "")

    def reply(self, message_id: str, text: str) -> None:
        feishu_request(
            "POST", self.token(), f"/open-apis/im/v1/messages/{message_id}/reply",
            json_body={"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)},
        )

    def download_image(self, message_id: str, file_key: str, destination: Path) -> bytes:
        response = requests.get(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
            headers={"Authorization": f"Bearer {self.token()}"},
            params={"type": "image"},
            timeout=30,
            stream=True,
        )
        status_code = getattr(response, "status_code", 200)
        if not isinstance(status_code, int):
            status_code = 200
        if status_code >= 400:
            try:
                error_payload = response.json()
            except ValueError:
                error_payload = {}
            code = error_payload.get("code") if isinstance(error_payload, dict) else ""
            message = error_payload.get("msg") if isinstance(error_payload, dict) else ""
            raise FeishuAPIError(
                f"image resource HTTP {status_code} "
                f"code={code or 'unknown'} msg={message or 'unknown'}"
            )
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > MAX_IMAGE_BYTES:
                raise FeishuAPIError("image exceeds the 12 MB inbox limit")
            chunks.append(chunk)
        data = b"".join(chunks)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return data

    def matching_records(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        return find_matching_records(self.list_records(), job, self.config)

    def company_title_records(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        return find_company_title_records(self.list_records(), job, self.config)

    def upsert_applied(self, job: dict[str, Any], evidence: dict[str, Any]) -> tuple[str, bool]:
        token = self.token()
        definitions = get_field_definitions(self.config, token)
        assert_frozen_schema_compatible(self.config, definitions)
        records = self.list_records()
        matches = find_matching_records(records, job, self.config)
        record = matches[0] if len(matches) == 1 else None
        if len(matches) > 1:
            raise FeishuAPIError("ambiguous existing jobs; URL or location is required")
        fields = build_applied_fields(job, evidence, self.config, definitions, existing=record)
        if record:
            feishu_request(
                "PUT", token,
                f"/open-apis/bitable/v1/apps/{self.config.app_token}/tables/{self.config.table_id}/records/{record['record_id']}",
                json_body={"fields": fields},
            )
            return str(record["record_id"]), False
        payload = feishu_request(
            "POST", token,
            f"/open-apis/bitable/v1/apps/{self.config.app_token}/tables/{self.config.table_id}/records",
            json_body={"fields": fields},
        )
        return str((payload.get("data") or {}).get("record", {}).get("record_id") or ""), True

    def record_link(self, record_id: str) -> str:
        if not record_id:
            return ""
        template = self.record_link_template or "https://www.feishu.cn/base/{app_token}?table={table_id}&view={view_id}&record={record_id}"
        return template.format(
            app_token=self.config.app_token,
            table_id=self.config.table_id,
            view_id=self.config.view_id,
            record_id=record_id,
        )


def _field_value(fields: dict[str, Any], config: FeishuConfig, key: str) -> Any:
    name = config.fields.get(key)
    return fields.get(name) if name else None


def find_matching_records(records: list[dict[str, Any]], job: dict[str, Any], config: FeishuConfig) -> list[dict[str, Any]]:
    target_url = canonicalize_url(job.get("official_url") or job.get("url"))
    target_key = str(job.get("canonical_key") or "").strip()
    company = normalize_token(str(job.get("company") or ""))
    title = normalize_token(str(job.get("title") or job.get("position") or ""))
    location = normalize_token(str(job.get("location") or ""))
    direct: list[dict[str, Any]] = []
    identity: list[dict[str, Any]] = []
    for record in records:
        fields = record.get("fields") or {}
        url = canonicalize_url(extract_url_value(_field_value(fields, config, "official_url")))
        key = str(_field_value(fields, config, "canonical_key") or "").strip()
        if target_url and url == target_url:
            direct.append(record)
            continue
        if target_key and key == target_key:
            direct.append(record)
            continue
        if company and title:
            same = normalize_token(str(_field_value(fields, config, "company") or "")) == company
            same = same and normalize_token(str(_field_value(fields, config, "title") or "")) == title
            if location:
                same = same and normalize_token(str(_field_value(fields, config, "location") or "")) == location
                if same:
                    identity.append(record)
            # Missing target location is intentionally ambiguous whenever more
            # than one company/title candidate exists.
            elif same:
                identity.append(record)
    matches = direct or identity
    unique: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for record in matches:
        record_id = str(record.get("record_id") or id(record))
        if record_id not in seen_ids:
            seen_ids.add(record_id)
            unique.append(record)
    return unique


def find_company_title_records(records: list[dict[str, Any]], job: dict[str, Any], config: FeishuConfig) -> list[dict[str, Any]]:
    """Find same company/title candidates without claiming they are matches."""

    company = normalize_token(str(job.get("company") or ""))
    title = normalize_token(str(job.get("title") or job.get("position") or ""))
    if not company or not title:
        return []
    candidates: list[dict[str, Any]] = []
    for record in records:
        fields = record.get("fields") or {}
        if (
            normalize_token(str(_field_value(fields, config, "company") or "")) == company
            and normalize_token(str(_field_value(fields, config, "title") or "")) == title
        ):
            candidates.append(record)
    return candidates


def find_matching_record(records: list[dict[str, Any]], job: dict[str, Any], config: FeishuConfig) -> dict[str, Any] | None:
    matches = find_matching_records(records, job, config)
    return matches[0] if len(matches) == 1 else None


def build_applied_fields(
    job: dict[str, Any],
    evidence: dict[str, Any],
    config: FeishuConfig,
    definitions: dict[str, dict[str, Any]],
    *,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing_fields = (existing or {}).get("fields") or {}
    raw: dict[str, Any] = {}
    mapping = {
        "company": job.get("company"), "title": job.get("title") or job.get("position"),
        "location": job.get("location"), "source": ["manual"],
        "official_url": job.get("official_url") or job.get("url"),
        "job_id": job.get("job_id"), "canonical_key": job.get("canonical_key"),
        "status": job.get("status"), "evidence_confidence": evidence.get("confidence") or "Low",
        "action": "APPLIED", "stage": "APPLIED", "applied_at": evidence.get("applied_at"),
        "notes": evidence.get("notes") or "User-reported application; message receipt is the confirmation.",
    }
    for key, value in mapping.items():
        name = config.fields.get(key)
        if not name or value in (None, "", []):
            continue
        if key == "applied_at" and existing_fields.get(name):
            continue
        raw[name] = value
    if evidence.get("source_evidence") and config.fields.get("notes"):
        raw[config.fields["notes"]] = normalize_whitespace(
            f"{raw.get(config.fields['notes'], '')} Evidence: {evidence['source_evidence']}"
        )[:1000]
    return {
        field_name: coerced
        for field_name, value in raw.items()
        if (coerced := _coerce_value(value, definitions.get(field_name))) not in (None, "", [], {})
    }


def build_job_from_evidence(
    envelope: MessageEnvelope,
    text: str,
    pages: list[dict[str, Any]],
    image_paths: list[str],
    *,
    identity_hint: dict[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    combined = "\n".join([envelope.text, text, *(str(page.get("text") or "") for page in pages)])
    fields = extract_labeled_fields(combined)
    # A model-normalized identity is preferred over arbitrary OCR heading
    # guesses, but only fields returned by the strict identity contract are
    # accepted here.
    for key in ("company", "title", "location"):
        value = normalize_whitespace(str((identity_hint or {}).get(key) or ""))
        if value and not fields.get(key):
            fields[key] = value
    for page in pages:
        if page.get("restricted") or page.get("error") or int(page.get("status_code") or 0) >= 400:
            continue
        inferred = infer_page_fields(page)
        for key, value in inferred.items():
            if value and not fields.get(key):
                fields[key] = value
    if not pages:
        inferred_image = infer_image_identity(combined)
        for key, value in inferred_image.items():
            if value and not fields.get(key):
                fields[key] = value
    if not fields.get("company") or not fields.get("title"):
        # Avoid fabricating identity from a screenshot's first arbitrary line.
        lines = [normalize_whitespace(item) for item in combined.splitlines() if normalize_whitespace(item)]
        if len(lines) >= 2 and not pages:
            fields.setdefault("company", lines[0])
            fields.setdefault("title", lines[1])
    urls = envelope.urls or extract_urls(combined)
    pages_by_url = {
        canonicalize_url(str(page.get("url") or "")): page
        for page in pages
        if canonicalize_url(str(page.get("url") or ""))
    }
    official_url = next(
        (
            url
            for url in urls
            if is_official_job_url(url, pages_by_url.get(canonicalize_url(url), {}), fields.get("company", ""))
        ),
        "",
    )
    identity = "|".join(
        normalize_token(fields.get(name, ""))
        for name in ("company", "title", "location")
    )
    # A manual screenshot must remain idempotent across a re-send.  Event IDs
    # and retained file paths are evidence provenance, not job identity; using
    # them in the key would create a second local job for the same screenshot.
    evidence_digest = hashlib.sha256(
        (identity or "|".join(urls) or envelope.text).encode("utf-8")
    ).hexdigest()[:20]
    key = f"url:{official_url}" if official_url else f"manual:{evidence_digest}"
    applied_at, is_default = parse_applied_at(envelope.text, envelope.sent_at)
    evidence = {
        "urls": urls,
        "image_paths": image_paths,
        "source_evidence": "; ".join(
            item for item in [
                f"message_id={envelope.message_id}" if envelope.message_id else "",
                f"source_urls={' | '.join(urls)}" if urls else "",
                f"public_url={official_url}" if official_url else "",
                f"images={len(image_paths)}" if image_paths else "",
            ] if item
        ),
        "confidence": "High" if fields.get("company") and fields.get("title") and (urls or image_paths) else "Low",
        "applied_at": applied_at,
        "applied_at_is_default_message_time": is_default,
        "notes": "Applied time defaults to the Feishu message time; screenshot timestamp was not used.",
    }
    if identity_hint:
        evidence["identity_enrichment"] = {
            key: value for key, value in identity_hint.items() if key in IDENTITY_RESULT_KEYS and value
        }
        evidence["notes"] = (
            f"{evidence['notes']} OCR identity normalized by DeepSeek "
            f"({identity_hint.get('confidence', 'UNCERTAIN')})."
        )
    job = {
        "company": fields.get("company", ""), "title": fields.get("title", ""),
        "position": fields.get("title", ""), "location": fields.get("location", ""),
        "official_url": official_url, "url": official_url, "canonical_key": key,
        "source_url": urls[0] if urls else "",
        "job_id": "", "source": "manual", "source_name": "feishu_application_inbox",
        "source_type": "feishu_application_inbox", "source_urls": urls,
        "description": combined[:4000], "status": "", "observation_origin": "MANUAL_IMPORT",
        "manual_ingest": True,
        "discovered_by": ["feishu_application_inbox"], "discovery_sources": ["feishu_application_inbox"],
        "canonicality": "CANONICAL" if official_url else "SECONDARY",
        "canonical_url": official_url, "verification_state": "UNCERTAIN", "action": "APPLIED", "stage": "APPLIED",
        "applied_at": applied_at, "inbox_evidence_paths": image_paths,
    }
    missing = [key for key in ("company", "title") if not fields.get(key)]
    return job, evidence, missing


class ApplicationInbox:
    def __init__(
        self,
        store: InboxStore,
        *,
        client: FeishuInboxClient | None = None,
        ocr: Callable[[str], str] | None = None,
        allowed_open_ids: set[str] | None = None,
        identity_enricher: Callable[[str, dict[str, str] | None], dict[str, str]] | None = None,
        identity_settings: dict[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.client = client
        self.ocr = ocr or default_ocr
        # ``None`` is used by local replay/tests; the live receiver passes an
        # explicit set and therefore an empty configured whitelist denies all.
        self.allowed_open_ids = None if allowed_open_ids is None else set(allowed_open_ids)
        if identity_enricher is not None:
            self.identity_enricher = identity_enricher
        elif identity_settings is not None:
            self.identity_enricher = DeepSeekIdentityEnricher(identity_settings)
        else:
            self.identity_enricher = None

    def recover_processing(self) -> list[ProcessResult]:
        """Replay durable PROCESSING events after a process restart."""

        results: list[ProcessResult] = []
        for payload in self.store.processing_payloads():
            try:
                results.append(self.process_event(payload))
            except Exception as exc:  # noqa: BLE001
                # Leave the event PROCESSING and retain raw_event for the next
                # restart; no failed recovery is silently converted to success.
                state = self.store.read()
                envelope = parse_message_event(payload)
                if envelope and envelope.message_id in state["events"]:
                    record = dict(state["events"][envelope.message_id])
                    record.update({"last_error": type(exc).__name__, "updated_at": _now_iso()})
                    state["events"][envelope.message_id] = record
                    if envelope.event_id:
                        state["events"][envelope.event_id] = record
                    self.store.write(state)
        return results

    def persist_event(self, payload: dict[str, Any]) -> tuple[MessageEnvelope | None, ProcessResult | None]:
        """Persist the raw event and idempotency marker before acknowledging HTTP."""

        envelope = parse_message_event(payload)
        if envelope is None or not envelope.message_id:
            return None, ProcessResult("IGNORED", "不是可处理的飞书消息事件。")
        if envelope.chat_type and envelope.chat_type.casefold() not in {"p2p", "private"}:
            return envelope, ProcessResult("IGNORED", "请私聊机器人发送岗位 URL 或 JD 截图。")
        if self.allowed_open_ids is not None and envelope.sender_open_id not in self.allowed_open_ids:
            return envelope, ProcessResult("IGNORED", "该机器人未开放给当前账号。")
        with self.store.lock:
            state = self.store.read()
            if envelope.message_id in state["events"] or envelope.event_id in state["events"]:
                prior = state["events"].get(envelope.message_id) or state["events"].get(envelope.event_id) or {}
                prior_status = str(prior.get("status") or "").upper()
                if prior_status and prior_status != "PROCESSING":
                    return envelope, ProcessResult(prior_status, "这条消息已经记录过了。", duplicate=True)
                # A crash left PROCESSING: retain the original payload and
                # allow a redelivery/restart to resume processing.
                return envelope, None
            event_record = {
                "event_id": envelope.event_id,
                "message_id": envelope.message_id,
                "status": "PROCESSING",
                "received_at": _now_iso(),
                "raw_event": payload,
            }
            state["events"][envelope.message_id] = event_record
            if envelope.event_id:
                state["events"][envelope.event_id] = event_record
            self.store.write(state)
        return envelope, None

    def process_event(self, payload: dict[str, Any]) -> ProcessResult:
        envelope, early = self.persist_event(payload)
        if early is not None:
            return early
        if envelope is None:
            return ProcessResult("IGNORED", "不是可处理的飞书消息事件。")
        return self._process_new(envelope)

    def _process_new(self, envelope: MessageEnvelope) -> ProcessResult:
        state_before = self.store.read()
        pending_context = next(
            (
                (pending_id, item)
                for pending_id, item in state_before.get("pending", {}).items()
                if isinstance(item, dict)
                and item.get("status") == "NEEDS_INFO"
                and str(item.get("sender_open_id") or "") == envelope.sender_open_id
            ),
            None,
        )
        pending_id, pending_item = pending_context or ("", {})
        if pending_item.get("raw_text"):
            envelope = MessageEnvelope(
                **{
                    **asdict(envelope),
                    "text": normalize_whitespace(
                        f"{pending_item.get('raw_text', '')}\n{envelope.text}"
                    ),
                    "urls": list(dict.fromkeys([*(pending_item.get("urls") or []), *envelope.urls])),
                }
            )
        image_paths: list[str] = []
        image_text: list[str] = []
        if envelope.image_keys:
            if not self.client:
                return self._pending(envelope, "暂时无法读取图片，请补充公司和职位文字。", image_paths=[])
            for key in envelope.image_keys:
                try:
                    download_path = self.store.evidence_dir / ".downloads" / f"{_safe_filename(envelope.message_id)}-{_safe_filename(key)}.bin"
                    raw = self.client.download_image(envelope.message_id, key, download_path)
                    path = self.store.save_evidence(envelope.event_id or envelope.message_id, key, raw)
                    if download_path != Path(path):
                        download_path.unlink(missing_ok=True)
                    image_paths.append(path)
                except Exception as exc:  # noqa: BLE001
                    _long_connection_debug(
                        f"image download error={type(exc).__name__}: {str(exc)[:240]}"
                    )
                    return self._pending(
                        envelope,
                        "图片下载失败。请重新发送图片，或直接补充公司和职位文字。",
                        image_paths=image_paths,
                        error=f"{type(exc).__name__}: {str(exc)[:240]}",
                    )
                try:
                    image_text.append(self.ocr(path))
                except Exception as exc:  # noqa: BLE001
                    # The original image is still durable evidence.  OCR is an
                    # optional enrichment step and must not turn a valid image
                    # into a generic read failure on Windows encoding/tooling.
                    _long_connection_debug(
                        f"image OCR error={type(exc).__name__}: {str(exc)[:240]}"
                    )
                    image_text.append("")
        ocr_text = "\n".join(image_text)
        pages = [parse_public_page(url) for url in envelope.urls]
        identity_hint: dict[str, str] = {}
        if self.identity_enricher and ocr_text:
            try:
                try:
                    identity_hint = self.identity_enricher(
                        ocr_text,
                        infer_image_identity(ocr_text),
                        image_paths,
                    ) or {}
                except TypeError:
                    # Keep compatibility with small test/local enrichers that
                    # implement the original two-argument callback.
                    identity_hint = self.identity_enricher(
                        ocr_text,
                        infer_image_identity(ocr_text),
                    ) or {}
                if identity_hint:
                    _long_connection_debug(
                        "identity enrichment completed "
                        f"confidence={identity_hint.get('confidence', 'UNCERTAIN')}"
                    )
            except Exception as exc:  # noqa: BLE001
                # Model enrichment is an optional correction layer.  A
                # provider outage must not discard the original screenshot.
                _long_connection_debug(
                    f"identity enrichment error={type(exc).__name__}: {str(exc)[:240]}"
                )
        # When semantic cleanup is enabled, do not write a screenshot whose
        # only identity is a potentially corrupted OCR heading.  Labeled text
        # in the message remains acceptable; otherwise ask for a correction
        # instead of creating another wrong Bitable row.
        if (
            image_paths
            and self.identity_enricher
            and bool(getattr(self.identity_enricher, "enabled", True))
            and not identity_hint
        ):
            labeled = extract_labeled_fields(f"{envelope.text}\n{ocr_text}")
            if not labeled.get("company") or not labeled.get("title"):
                return self._pending(
                    envelope,
                    "截图文字识别结果不够可靠，未自动写入。请设置 DEEPSEEK_API_KEY 后重发，或直接补充公司和职位。",
                    image_paths=image_paths,
                    error="identity enrichment returned no confident identity",
                )
        job, evidence, missing = build_job_from_evidence(
            envelope,
            ocr_text,
            pages,
            image_paths,
            identity_hint=identity_hint,
        )
        prior_job = pending_item.get("job") if isinstance(pending_item.get("job"), dict) else {}
        for key in ("company", "title", "position", "location"):
            if not job.get(key) and prior_job.get(key):
                job[key] = prior_job[key]
        if prior_job.get("inbox_evidence_paths"):
            job["inbox_evidence_paths"] = list(dict.fromkeys([
                *(prior_job.get("inbox_evidence_paths") or []), *image_paths
            ]))
            evidence["image_paths"] = job["inbox_evidence_paths"]
        if job.get("company") and job.get("title"):
            if job.get("official_url"):
                job["canonical_key"] = f"url:{job['official_url']}"
            elif prior_job.get("canonical_key") and str(prior_job["canonical_key"]).startswith("manual:"):
                job["canonical_key"] = prior_job["canonical_key"]
            missing = []
        if pending_id and not missing:
            state_before.get("pending", {}).pop(pending_id, None)
            self.store.write(state_before)
        do_not_apply = bool(DO_NOT_APPLY_RE.search(envelope.text))
        if do_not_apply:
            return self._process_do_not_apply(envelope, job, evidence, missing)
        if missing:
            missing_label = "公司" if missing == ["company"] else "职位" if missing == ["title"] else "公司和职位"
            return self._pending(envelope, f"已收到证据，但还缺少{missing_label}。请只补充{missing_label}即可。", image_paths=image_paths, job=job, evidence=evidence)
        return self._apply(envelope, job, evidence)

    def _apply(self, envelope: MessageEnvelope, job: dict[str, Any], evidence: dict[str, Any]) -> ProcessResult:
        state = self.store.read()
        application_id = str(job["canonical_key"])
        prior = state["applications"].get(application_id)
        if prior and prior.get("message_id") == envelope.message_id:
            return ProcessResult("APPLIED", "这条投递记录已经处理过了。", application_id, str(prior.get("record_id") or ""), duplicate=True)
        record_id = ""
        created = False
        if self.client:
            matching_records = getattr(self.client, "matching_records", None)
            if callable(matching_records):
                matches = matching_records(job)
                if not str(job.get("location") or "").strip() and not str(job.get("official_url") or "").strip():
                    company_title_records = getattr(self.client, "company_title_records", None)
                    if callable(company_title_records) and company_title_records(job):
                        return self._pending(
                            envelope,
                            "已识别公司和职位，但缺少地点且已有同名岗位。请补充地点或岗位 URL，未创建新记录。",
                            image_paths=evidence.get("image_paths", []),
                            job=job,
                            evidence=evidence,
                        )
                if len(matches) > 1:
                    missing = "地点或岗位 URL" if not str(job.get("location") or "").strip() else "岗位 URL"
                    return self._pending(
                        envelope,
                        f"找到多个可能的岗位，请补充{missing}后再记录，未创建新记录。",
                        image_paths=evidence.get("image_paths", []),
                        job=job,
                        evidence=evidence,
                    )
            record_id, created = self.client.upsert_applied(job, evidence)
        state = self.store.read()
        state["applications"][application_id] = {
            "canonical_key": application_id, "message_id": envelope.message_id,
            "event_id": envelope.event_id, "record_id": record_id, "created": created,
            "job": job, "evidence": evidence, "status": "APPLIED", "updated_at": _now_iso(),
        }
        event_record = dict(state["events"].get(envelope.message_id) or {})
        event_record.update({"event_id": envelope.event_id, "status": "APPLIED", "canonical_key": application_id, "updated_at": _now_iso()})
        state["events"][envelope.message_id] = event_record
        if envelope.event_id:
            state["events"][envelope.event_id] = event_record
        self.store.write(state)
        self.store.write_jobs(state)
        link = self.client.record_link(record_id) if self.client else ""
        suffix = f"｜{link}" if link else ""
        default_note = "（投递时间取消息发送时间，可在飞书修改）" if evidence.get("applied_at_is_default_message_time") else ""
        return ProcessResult("APPLIED", f"已记录：{job['company']}—{job['title']}｜{evidence['applied_at']}{default_note}{suffix}", application_id, record_id)

    def _pending(self, envelope: MessageEnvelope, message: str, *, image_paths: list[str], job: dict[str, Any] | None = None, evidence: dict[str, Any] | None = None, error: str = "") -> ProcessResult:
        pending_id = f"pending:{_pending_fingerprint(envelope, image_paths)}"
        state = self.store.read()
        state["pending"][pending_id] = {
            "pending_id": pending_id, "message_id": envelope.message_id, "event_id": envelope.event_id,
            "sender_open_id": envelope.sender_open_id,
            "raw_text": envelope.text, "urls": envelope.urls, "image_keys": envelope.image_keys,
            "image_paths": image_paths, "job": job or {}, "evidence": evidence or {},
            "status": "NEEDS_INFO", "error": error, "updated_at": _now_iso(),
        }
        event_record = dict(state["events"].get(envelope.message_id) or {})
        event_record.update({"event_id": envelope.event_id, "status": "NEEDS_INFO", "pending_id": pending_id, "updated_at": _now_iso()})
        state["events"][envelope.message_id] = event_record
        self.store.write(state)
        return ProcessResult("NEEDS_INFO", message, pending_id=pending_id)

    def _process_do_not_apply(self, envelope: MessageEnvelope, job: dict[str, Any], evidence: dict[str, Any], missing: list[str]) -> ProcessResult:
        if missing:
            return self._pending(envelope, "已收到“不投”指令，但还无法唯一定位岗位。请补充岗位 URL，或补充公司和职位。", image_paths=evidence.get("image_paths", []), job=job, evidence=evidence)
        if self.client:
            state = self.store.read()
            if not str(job.get("location") or "").strip() and not str(job.get("official_url") or "").strip():
                company_title_records = getattr(self.client, "company_title_records", None)
                if callable(company_title_records) and company_title_records(job):
                    return self._pending(
                        envelope,
                        "已识别公司和职位，但缺少地点且已有同名岗位。请补充地点或岗位 URL。",
                        image_paths=evidence.get("image_paths", []),
                        job=job,
                        evidence=evidence,
                    )
            matches = find_matching_records(self.client.list_records(), job, self.client.config)
            if len(matches) > 1:
                return self._pending(
                    envelope,
                    "找到多个可能的岗位，无法安全执行“我不投”。请补充地点或岗位 URL。",
                    image_paths=evidence.get("image_paths", []),
                    job=job,
                    evidence=evidence,
                )
            existing = matches[0] if matches else None
            if existing:
                token = self.client.token()
                definitions = get_field_definitions(self.client.config, token)
                assert_frozen_schema_compatible(self.client.config, definitions)
                fields = {
                    self.client.config.fields["stage"]: _coerce_value("WITHDRAWN", definitions.get(self.client.config.fields["stage"])),
                    self.client.config.fields["action"]: _coerce_value("REJECT", definitions.get(self.client.config.fields["action"])),
                    self.client.config.fields["notes"]: _coerce_value("USER_DO_NOT_APPLY; employer Job Status is unchanged.", definitions.get(self.client.config.fields["notes"])),
                }
                feishu_request("PUT", token, f"/open-apis/bitable/v1/apps/{self.client.config.app_token}/tables/{self.client.config.table_id}/records/{existing['record_id']}", json_body={"fields": fields})
                return ProcessResult("WITHDRAWN", "已记录“我不投”，岗位仍保留为真实公开状态。", str(job.get("canonical_key") or ""), str(existing.get("record_id") or ""))
        state = self.store.read()
        event_record = dict(state["events"].get(envelope.message_id) or {})
        event_record.update({
            "event_id": envelope.event_id,
            "status": "WITHDRAWN",
            "canonical_key": str(job.get("canonical_key") or ""),
            "updated_at": _now_iso(),
        })
        state["events"][envelope.message_id] = event_record
        self.store.write(state)
        return ProcessResult("WITHDRAWN", "已记录“我不投”；岗位仍保留为真实公开状态。", str(job.get("canonical_key") or ""))


def default_ocr(path: str) -> str:
    configured = str(os.getenv("TESSERACT_CMD") or "").strip()
    candidates = [
        configured,
        shutil.which("tesseract") or "",
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ]
    executable = next((item for item in candidates if item and Path(item).exists()), "")
    if not executable:
        _long_connection_debug("OCR unavailable: tesseract executable not found")
        return ""
    try:
        result = subprocess.run(
            [executable, path, "stdout", "-l", "chi_sim+eng"],
            capture_output=True,
            text=False,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")[:240]
            _long_connection_debug(f"OCR returned code={result.returncode} stderr={stderr}")
        decoded = result.stdout.decode("utf-8", errors="replace")
        text = "\n".join(
            line
            for line in (normalize_whitespace(item) for item in decoded.splitlines())
            if line
        )
        _long_connection_debug(
            f"OCR completed executable={executable} returncode={result.returncode} output_chars={len(text)}"
        )
        return text
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return ""


def verify_request(payload: dict[str, Any], verification_token: str = "") -> bool:
    if not verification_token:
        return False
    return str((payload.get("header") or {}).get("token") or payload.get("token") or "") == verification_token


def load_receiver_settings(feishu_config_path: str | Path) -> dict[str, Any]:
    raw = load_config(feishu_config_path)
    settings = raw.get("receiver") or {}
    return settings if isinstance(settings, dict) else {}


def build_runtime(feishu_config_path: str, *, store_path: str = "", evidence_dir: str = "") -> tuple[InboxStore, FeishuInboxClient, dict[str, Any]]:
    config = load_feishu_config(feishu_config_path)
    settings = load_receiver_settings(feishu_config_path)
    store = InboxStore(store_path or settings.get("store_path") or "data/job_cache/feishu_application_inbox.json", evidence_dir or settings.get("evidence_dir") or "")
    client = FeishuInboxClient(
        config,
        record_link_template=str(settings.get("record_link_template") or ""),
    )
    return store, client, settings


class _Handler(BaseHTTPRequestHandler):
    inbox: ApplicationInbox
    verification_token: str = ""

    def do_POST(self) -> None:  # noqa: N802
        if not self.verification_token:
            self.send_error(503, "event verification token is not configured")
            return
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(min(length, 2 * 1024 * 1024))
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self.send_error(400, "invalid JSON")
            return
        if payload.get("type") == "url_verification" or payload.get("challenge"):
            if not verify_request(payload, self.verification_token):
                self.send_error(403, "invalid verification token")
                return
            self._json({"challenge": payload.get("challenge", "")})
            return
        if not verify_request(payload, self.verification_token):
            self.send_error(403, "invalid verification token")
            return
        envelope, early = self.inbox.persist_event(payload)
        if early is not None:
            if early.status == "IGNORED":
                self.send_error(403, early.message)
                return
            self._json({"code": 0})
            return
        if envelope is None:
            self.send_error(400, "invalid event")
            return
        # persist_event is atomic before this acknowledgement.  Processing is
        # asynchronous, but a crash leaves raw_event + PROCESSING for replay.
        self._json({"code": 0})
        threading.Thread(target=self._background_process, args=(payload,), daemon=True).start()

    def _background_process(self, payload: dict[str, Any]) -> None:
        result = self.inbox.process_event(payload)
        event = payload.get("event") or {}
        message = event.get("message") or {}
        if result.status not in {"IGNORED", "DUPLICATE"} and self.inbox.client and message.get("message_id"):
            try:
                self.inbox.client.reply(str(message["message_id"]), result.message)
            except Exception:
                # The durable store is already updated; a retry of the same
                # message remains idempotent and the operator can inspect it.
                pass

    def _json(self, value: dict[str, Any]) -> None:
        encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def _sdk_object_to_dict(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {key: _sdk_object_to_dict(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sdk_object_to_dict(item) for item in value]
    for method_name in ("model_dump", "to_dict", "dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                return _sdk_object_to_dict(method())
            except TypeError:
                continue
    json_method = getattr(value, "model_dump_json", None)
    if callable(json_method):
        try:
            return json.loads(json_method())
        except (TypeError, ValueError):
            pass
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        public_attributes = {
            name: _sdk_object_to_dict(item)
            for name, item in attributes.items()
            if not name.startswith("_")
        }
        if public_attributes:
            return public_attributes
    return {
        name: _sdk_object_to_dict(getattr(value, name))
        for name in ("event", "message", "sender", "header")
        if hasattr(value, name)
    }


def sdk_event_payload(data: Any) -> dict[str, Any]:
    """Convert lark-oapi event models to the JSON shape used by the inbox."""

    raw = _sdk_object_to_dict(data)
    if isinstance(raw, dict) and isinstance(raw.get("event"), dict):
        return raw
    event = _sdk_object_to_dict(getattr(data, "event", data))
    header = _sdk_object_to_dict(getattr(data, "header", {}))
    return {"header": header if isinstance(header, dict) else {}, "event": event if isinstance(event, dict) else {}}


def process_long_connection_event(inbox: ApplicationInbox, data: Any) -> ProcessResult:
    payload = sdk_event_payload(data)
    envelope, early = inbox.persist_event(payload)
    if early is not None:
        return early
    if envelope is None:
        return ProcessResult("IGNORED", "不是可处理的飞书消息事件。")
    result = inbox.process_event(payload)
    message_id = envelope.message_id
    if result.status not in {"IGNORED", "DUPLICATE"} and inbox.client and message_id:
        try:
            inbox.client.reply(message_id, result.message)
        except Exception:
            # State is already durable; the next event/recovery pass can retry
            # processing, while operators can inspect the terminal record.
            pass
    return result


def _long_connection_debug(message: str) -> None:
    """Print safe long-connection diagnostics when explicitly enabled."""

    if str(os.getenv("FEISHU_INBOX_DEBUG") or "").strip().lower() in {"1", "true", "yes"}:
        print(f"[Feishu inbox] {message}", flush=True)


def serve_long_connection(feishu_config: str, *, store_path: str = "", evidence_dir: str = "") -> None:
    """Run the official lark-oapi WebSocket event receiver."""

    try:
        import lark_oapi as lark
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "long connection requires the official lark-oapi package; install requirements.txt first"
        ) from exc
    store, client, settings = build_runtime(feishu_config, store_path=store_path, evidence_dir=evidence_dir)
    allowed = {str(item) for item in settings.get("allowed_open_ids", []) if str(item).strip()}
    if not allowed:
        raise RuntimeError("receiver.allowed_open_ids must contain at least one authorized Feishu open_id")
    inbox = ApplicationInbox(
        store,
        client=client,
        allowed_open_ids=allowed,
        identity_settings=settings.get("identity_enrichment")
        if isinstance(settings.get("identity_enrichment"), dict)
        else None,
    )
    inbox.recover_processing()
    config = load_feishu_config(feishu_config)
    _long_connection_debug("long connection receiver initialized")

    def on_message(data: Any) -> None:
        payload = sdk_event_payload(data)
        event = payload.get("event") if isinstance(payload, dict) else {}
        message = event.get("message") if isinstance(event, dict) else {}
        sender = event.get("sender") if isinstance(event, dict) else {}
        sender_id = sender.get("sender_id") if isinstance(sender, dict) else {}
        try:
            result = process_long_connection_event(inbox, data)
        except Exception as exc:  # noqa: BLE001
            _long_connection_debug(
                f"message handler error={type(exc).__name__}: {str(exc)[:500]}"
            )
            return
        _long_connection_debug(
            "message event received "
            f"status={result.status} message_id={str((message or {}).get('message_id') or '')} "
            f"sender_open_id={str((sender_id or {}).get('open_id') or '')} "
            f"reason={result.message}"
        )

    builder = lark.EventDispatcherHandler.builder("", "")
    builder = builder.register_p2_im_message_receive_v1(on_message)
    event_handler = builder.build()
    # Keep SDK logs quiet so message text/image metadata is not copied into
    # ordinary process logs.
    log_level = getattr(getattr(lark, "LogLevel", None), "ERROR", None)
    kwargs: dict[str, Any] = {"app_id": config.app_id, "app_secret": config.app_secret, "event_handler": event_handler}
    if log_level is not None:
        kwargs["log_level"] = log_level
    ws_client = lark.ws.Client(**kwargs)
    # lark-oapi owns ping/reconnect behavior.  Restart the client if its
    # outer loop returns or raises, without exposing credentials in output.
    while True:
        try:
            _long_connection_debug("connecting")
            ws_client.start()
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            _long_connection_debug(f"connection error={type(exc).__name__}; retrying in 5 seconds")
            time.sleep(5)


def serve(feishu_config: str, *, host: str, port: int, store_path: str = "", evidence_dir: str = "") -> None:
    store, client, settings = build_runtime(feishu_config, store_path=store_path, evidence_dir=evidence_dir)
    allowed = {str(item) for item in settings.get("allowed_open_ids", []) if str(item).strip()}
    if not allowed:
        raise RuntimeError("receiver.allowed_open_ids must contain at least one authorized Feishu open_id")
    inbox = ApplicationInbox(
        store,
        client=client,
        allowed_open_ids=allowed,
        identity_settings=settings.get("identity_enrichment")
        if isinstance(settings.get("identity_enrichment"), dict)
        else None,
    )
    inbox.recover_processing()
    handler = type("InboxHandler", (_Handler,), {})
    handler.inbox = inbox
    handler.verification_token = str(os.getenv("FEISHU_EVENT_VERIFICATION_TOKEN") or settings.get("verification_token") or "")
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Feishu application inbox listening on {host}:{port}; state={store.path}")
    server.serve_forever()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Receive Feishu application evidence without submitting jobs.")
    parser.add_argument("--config", default="config/feishu.yaml")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--long-connection", action="store_true", help="Use the official lark-oapi WebSocket receiver")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--input", help="Process one saved Feishu event JSON")
    parser.add_argument("--store", default="")
    parser.add_argument("--evidence-dir", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.long_connection:
        serve_long_connection(args.config, store_path=args.store, evidence_dir=args.evidence_dir)
        return 0
    if args.serve:
        serve(args.config, host=args.host, port=args.port, store_path=args.store, evidence_dir=args.evidence_dir)
        return 0
    if not args.input:
        raise SystemExit("choose --serve or --input")
    store, client, settings = build_runtime(args.config, store_path=args.store, evidence_dir=args.evidence_dir)
    allowed_values = [str(item) for item in settings.get("allowed_open_ids", []) if str(item).strip()]
    allowed = set(allowed_values) if allowed_values else None
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    result = ApplicationInbox(
        store,
        client=client,
        allowed_open_ids=allowed,
        identity_settings=settings.get("identity_enrichment")
        if isinstance(settings.get("identity_enrichment"), dict)
        else None,
    ).process_event(payload)
    print(json.dumps(asdict(result), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
