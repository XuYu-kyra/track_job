#!/usr/bin/env python3
"""Bounded public official-source adapters used by Source Discovery V2.

The adapter contract intentionally separates listing, detail hydration and
normalization.  Collection always completes pagination (or reports why it did
not) before downstream Shenzhen/cohort/role filtering is applied.
"""

from __future__ import annotations

import html
import json
import math
import re
import time
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

try:
    from ats_detection import detect_ats_evidence
    from common import normalize_whitespace
except ModuleNotFoundError:
    from scripts.ats_detection import detect_ats_evidence
    from scripts.common import normalize_whitespace


DEFAULT_SAFETY_CAP = 500


@dataclass
class AdapterPage:
    records: list[dict[str, Any]]
    next_cursor: Any = None
    pagination_complete: bool = True


@dataclass
class AdapterRun:
    records: list[dict[str, Any]]
    status: str
    pagination_complete: bool
    pages_attempted: int
    pages_succeeded: int
    listed_count: int
    hydrated_count: int
    truncated: bool = False
    rate_limited: bool = False
    reason: str = ""
    adapter: str = ""
    continuation_cursor: Any = None

    def health_details(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("records", None)
        return payload


class OfficialSourceAdapter(ABC):
    """Common read-only adapter interface for official public job sources."""

    adapter_name = "unknown"
    supports_pagination = False
    supports_full_jd = False

    def __init__(self, session: Any, company: dict[str, Any]):
        self.session = session
        self.company = company
        self.career_url = str(company.get("official_career_url") or "").strip()
        self._last_run: AdapterRun | None = None

    def probe(self) -> dict[str, Any]:
        try:
            response = self.session.get(self.career_url, timeout=30)
            response.raise_for_status()
            detection = detect_ats_evidence(
                self.career_url,
                html_text=str(getattr(response, "text", "") or "")[:500_000],
            )
            return {
                "ok": True,
                "status_code": int(getattr(response, "status_code", 200) or 200),
                "detected_ats": detection.family,
                "reason": "; ".join(detection.evidence) or "public page reachable",
                "pagination_support": self.supports_pagination,
                "full_jd_support": self.supports_full_jd,
            }
        except Exception as exc:  # network/parser boundary, reported rather than hidden
            return {
                "ok": False,
                "status_code": int(getattr(getattr(exc, "response", None), "status_code", 0) or 0),
                "detected_ats": "UNKNOWN",
                "reason": str(exc)[:300],
                "pagination_support": self.supports_pagination,
                "full_jd_support": self.supports_full_jd,
            }

    @abstractmethod
    def list_jobs(self, cursor: Any = None) -> AdapterPage:
        raise NotImplementedError

    def fetch_job_detail(self, record: dict[str, Any]) -> dict[str, Any]:
        return dict(record)

    @abstractmethod
    def normalize(self, record: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def stable_key(self, record: dict[str, Any]) -> str:
        return str(
            record.get("job_id")
            or record.get("id")
            or record.get("externalPath")
            or record.get("url")
            or ""
        ).strip()

    def health_details(self) -> dict[str, Any]:
        if self._last_run is None:
            return {
                "adapter": self.adapter_name,
                "pagination_complete": False,
                "pages_attempted": 0,
                "pages_succeeded": 0,
                "reason": "adapter has not run",
            }
        return self._last_run.health_details()

    def collect(
        self,
        *,
        safety_cap: int = DEFAULT_SAFETY_CAP,
        detail_predicate: Callable[[dict[str, Any]], bool] | None = None,
        start_cursor: Any = None,
    ) -> AdapterRun:
        if safety_cap <= 0:
            raise ValueError("official adapter safety_cap must be positive")
        cursor: Any = start_cursor
        continuation_cursor: Any = None
        pages_attempted = 0
        pages_succeeded = 0
        pagination_complete = False
        truncated = False
        rate_limited = False
        reason = ""
        raw_records: list[dict[str, Any]] = []
        seen: set[str] = set()
        seen_cursors: set[str] = set()
        try:
            while True:
                cursor_key = json.dumps(cursor, sort_keys=True, ensure_ascii=False, default=str)
                if cursor_key in seen_cursors:
                    reason = "pagination cursor repeated"
                    break
                seen_cursors.add(cursor_key)
                pages_attempted += 1
                page = self.list_jobs(cursor)
                pages_succeeded += 1
                for record in page.records:
                    key = self.stable_key(record)
                    if not key:
                        key = json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)
                    if key in seen:
                        continue
                    seen.add(key)
                    raw_records.append(record)
                # Stop only at a page boundary. This may exceed the soft cap by
                # one page, but guarantees the continuation cursor cannot skip
                # records from a partially consumed page.
                if len(raw_records) >= safety_cap:
                    truncated = page.next_cursor is not None or not page.pagination_complete
                    pagination_complete = not truncated
                    continuation_cursor = page.next_cursor if truncated else None
                    reason = "safety cap reached" if truncated else ""
                    break
                if page.pagination_complete or page.next_cursor is None:
                    pagination_complete = True
                    break
                cursor = page.next_cursor
        except Exception as exc:  # adapter health must preserve partial success
            status_code = int(
                getattr(getattr(exc, "response", None), "status_code", 0) or 0
            )
            rate_limited = status_code == 429 or "429" in str(exc)
            reason = str(exc)[:300]

        hydrated: list[dict[str, Any]] = []
        for record in raw_records:
            if detail_predicate is not None and not detail_predicate(record):
                hydrated.append(dict(record))
                continue
            try:
                hydrated.append(self.fetch_job_detail(record))
            except Exception as exc:
                reason = reason or f"detail hydration failed: {str(exc)[:240]}"
                hydrated.append(dict(record))

        normalized = [self.normalize(record) for record in hydrated]
        if not pagination_complete or truncated or reason:
            status = "PARTIAL_RATE_LIMITED" if rate_limited else "PARTIAL"
        elif normalized:
            status = "SUCCESS"
        else:
            status = "EMPTY_VALID"
        if pages_succeeded == 0:
            status = "PARTIAL_RATE_LIMITED" if rate_limited else "FAILED"
        run = AdapterRun(
            records=normalized,
            status=status,
            pagination_complete=pagination_complete,
            pages_attempted=pages_attempted,
            pages_succeeded=pages_succeeded,
            listed_count=len(raw_records),
            hydrated_count=sum(bool(item.get("description")) for item in normalized),
            truncated=truncated,
            rate_limited=rate_limited,
            reason=reason,
            adapter=self.adapter_name,
            continuation_cursor=continuation_cursor,
        )
        self._last_run = run
        return run


def _plain_text(value: Any) -> str:
    return normalize_whitespace(
        html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    )


def _company_name(company: dict[str, Any]) -> str:
    return str(company.get("name") or "")


class WorkdayAdapter(OfficialSourceAdapter):
    adapter_name = "Workday"
    supports_pagination = True
    supports_full_jd = True
    page_size = 20

    def _coordinates(self) -> tuple[Any, str, str, str]:
        parsed = urlparse(self.career_url)
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise ValueError("Workday URL must include a site name")
        site = parts[-1]
        tenant = parsed.hostname.split(".")[0] if parsed.hostname else ""
        endpoint = f"{parsed.scheme}://{parsed.netloc}/wday/cxs/{tenant}/{site}/jobs"
        return parsed, tenant, site, endpoint

    def list_jobs(self, cursor: Any = None) -> AdapterPage:
        _, _, _, endpoint = self._coordinates()
        offset = int(cursor or 0)
        response = self.session.post(
            endpoint,
            json={
                "appliedFacets": {},
                "limit": self.page_size,
                "offset": offset,
                "searchText": str(self.company.get("search_text") or "2027 China"),
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        records = payload.get("jobPostings") or []
        if not isinstance(records, list):
            raise ValueError("Workday response missing jobPostings")
        total = int(payload.get("total") or payload.get("totalCount") or 0)
        next_offset = offset + len(records)
        has_more = next_offset < total if total else len(records) >= self.page_size
        return AdapterPage(records, next_offset if has_more else None, not has_more)

    def stable_key(self, record: dict[str, Any]) -> str:
        return str(record.get("externalPath") or record.get("bulletFields") or "")

    def fetch_job_detail(self, record: dict[str, Any]) -> dict[str, Any]:
        parsed, tenant, site, _ = self._coordinates()
        path = str(record.get("externalPath") or "")
        if not path:
            return dict(record)
        response = self.session.get(
            f"{parsed.scheme}://{parsed.netloc}/wday/cxs/{tenant}/{site}{path}",
            timeout=30,
        )
        response.raise_for_status()
        detail = (response.json().get("jobPostingInfo") or {})
        result = dict(record)
        result["detail"] = detail
        return result

    def normalize(self, record: dict[str, Any]) -> dict[str, Any]:
        detail = record.get("detail") or {}
        locations = [str(detail.get("location") or record.get("locationsText") or "")]
        locations.extend(str(value) for value in detail.get("additionalLocations") or [])
        path = str(record.get("externalPath") or "")
        return {
            "company": _company_name(self.company),
            "title": str(record.get("title") or detail.get("title") or ""),
            "location": "/".join(dict.fromkeys(filter(None, locations))),
            "description": _plain_text(detail.get("jobDescription")),
            "url": urljoin(self.career_url.rstrip("/") + "/", path.lstrip("/")),
            "job_id": path.rsplit("_", 1)[-1] if "_" in path else path.rstrip("/").rsplit("/", 1)[-1],
            "posted_at": str(detail.get("startDate") or ""),
            "deadline": str(detail.get("endDate") or ""),
            "campaign_context": str(record.get("subtitle") or ""),
            "ats_family": "Workday",
        }


class BeisenAdapter(OfficialSourceAdapter):
    adapter_name = "Beisen"
    supports_pagination = True
    supports_full_jd = True
    page_size = 50

    def list_jobs(self, cursor: Any = None) -> AdapterPage:
        page_index = int(cursor or 1)
        endpoint = urljoin(self.career_url, "/api/JobAd/GetJobAdPageList")
        response = self.session.post(
            endpoint,
            json={"pageIndex": page_index, "pageSize": self.page_size},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("Data")
        if payload.get("Code") != 200 or not isinstance(data, list):
            raise ValueError(
                f"Beisen list API error: {payload.get('Message') or payload.get('Code')}"
            )
        total = int(payload.get("Total") or payload.get("TotalCount") or 0)
        has_more = page_index * self.page_size < total if total else len(data) >= self.page_size
        return AdapterPage(data, page_index + 1 if has_more else None, not has_more)

    def stable_key(self, record: dict[str, Any]) -> str:
        return str(record.get("Id") or record.get("JobAdId") or "")

    def fetch_job_detail(self, record: dict[str, Any]) -> dict[str, Any]:
        if any(record.get(key) for key in ("Duty", "Qualification", "Requirement")):
            return dict(record)
        job_id = self.stable_key(record)
        if not job_id:
            return dict(record)
        response = self.session.get(
            urljoin(self.career_url, f"/api/JobAd/GetJobAd?jobAdId={job_id}"),
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        result = dict(record)
        data = payload.get("Data") or payload.get("data") or {}
        if isinstance(data, dict):
            result.update(data)
        return result

    def normalize(self, record: dict[str, Any]) -> dict[str, Any]:
        locations = record.get("LocNames") or record.get("WorkPlaceNames") or []
        location = (
            "/".join(str(value) for value in locations)
            if isinstance(locations, list)
            else str(locations or "")
        )
        job_id = self.stable_key(record)
        return {
            "company": _company_name(self.company),
            "title": str(record.get("JobAdName") or record.get("Name") or ""),
            "location": location,
            "description": _plain_text(
                " ".join(
                    str(record.get(key) or "")
                    for key in ("Duty", "Qualification", "Requirement")
                )
            ),
            "url": urljoin(self.career_url, f"/campus/detail?jobAdId={job_id}"),
            "job_id": job_id,
            "posted_at": str(record.get("PublishDate") or ""),
            "deadline": str(record.get("EndDate") or ""),
            "campaign_context": str(record.get("RecruitTypeName") or "campus"),
            "ats_family": "Beisen",
        }


class HotjobAdapter(OfficialSourceAdapter):
    adapter_name = "Hotjob"
    supports_pagination = True
    supports_full_jd = True
    page_size = 50
    # Empty search returns the complete campus catalogue; pagination then walks
    # it once without keyword-overlap duplicates.
    keywords = ("",)

    def _suite(self) -> str:
        cached = str(self.company.get("hotjob_suite") or "")
        if cached:
            return cached
        response = self.session.post(
            urljoin(self.career_url, "/wecruit/common/getSLD"),
            data={"sld": urlparse(self.career_url).netloc},
            timeout=30,
        )
        response.raise_for_status()
        portal = ((response.json().get("data") or {}).get("linkData") or {}).get("link")
        match = re.search(r"/(SU[a-zA-Z0-9]+)/", str(portal or ""))
        if not match:
            raise ValueError("Hotjob domain did not resolve to a suite key")
        self.company["hotjob_suite"] = match.group(1)
        return match.group(1)

    def list_jobs(self, cursor: Any = None) -> AdapterPage:
        keyword_index, page = (0, 1)
        if isinstance(cursor, (list, tuple)) and len(cursor) == 2:
            keyword_index, page = int(cursor[0]), int(cursor[1])
        suite = self._suite()
        keyword = self.keywords[keyword_index]
        response = self.session.post(
            urljoin(self.career_url, f"/wecruit/positionInfo/listPosition/{suite}"),
            params={
                "iSaJAx": "isAjax",
                "request_locale": "zh_CN",
                "t": int(time.time() * 1000),
            },
            data={
                "isFrompb": "true",
                "recruitType": 1,
                "pageSize": self.page_size,
                "currentPage": page,
                "postKey": keyword,
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if str(payload.get("state")) != "200":
            raise ValueError(f"Hotjob list API error: {payload.get('msg') or payload.get('state')}")
        page_form = ((payload.get("data") or {}).get("pageForm") or {})
        records = page_form.get("pageData") or []
        if not isinstance(records, list):
            raise ValueError("Hotjob response missing pageData")
        total_pages = int(
            page_form.get("totalPage")
            or page_form.get("pageCount")
            or math.ceil(int(page_form.get("totalCount") or 0) / self.page_size)
            or 0
        )
        page_has_more = page < total_pages if total_pages else len(records) >= self.page_size
        if page_has_more:
            next_cursor: Any = (keyword_index, page + 1)
            complete = False
        elif keyword_index + 1 < len(self.keywords):
            next_cursor = (keyword_index + 1, 1)
            complete = False
        else:
            next_cursor = None
            complete = True
        return AdapterPage(records, next_cursor, complete)

    def stable_key(self, record: dict[str, Any]) -> str:
        return str(record.get("postId") or "")

    def fetch_job_detail(self, record: dict[str, Any]) -> dict[str, Any]:
        post_id = self.stable_key(record)
        if not post_id:
            return dict(record)
        suite = self._suite()
        response = self.session.post(
            urljoin(self.career_url, f"/wecruit/positionInfo/listPositionDetail/{suite}"),
            params={
                "iSaJAx": "isAjax",
                "request_locale": "zh_CN",
                "t": int(time.time() * 1000),
            },
            data={"postId": post_id},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        result = dict(record)
        if str(payload.get("state")) == "200" and isinstance(payload.get("data"), dict):
            result["detail"] = payload["data"]
        return result

    def normalize(self, record: dict[str, Any]) -> dict[str, Any]:
        detail = record.get("detail") or {}
        post_id = self.stable_key(record)
        suite = self._suite()
        return {
            "company": _company_name(self.company),
            "title": str(record.get("postName") or detail.get("postName") or ""),
            "location": str(record.get("workPlaceStr") or detail.get("workPlaceStr") or ""),
            "description": _plain_text(
                " ".join(
                    str(detail.get(key) or record.get(key) or "")
                    for key in (
                        "workContent", "qualification", "postDuty",
                        "postRequirement", "subject",
                    )
                )
            ),
            "url": urljoin(
                self.career_url,
                f"/{suite}/pb/posDetail.html?postId={post_id}&postType=campus",
            ),
            "job_id": post_id,
            "posted_at": str(record.get("publishDate") or ""),
            "deadline": str(record.get("endDate") or ""),
            "campaign_context": str(record.get("projectName") or "campus"),
            "ats_family": "Hotjob",
        }


class FeishuRecruitingAdapter(OfficialSourceAdapter):
    adapter_name = "Feishu Recruiting"
    supports_pagination = True
    supports_full_jd = True
    page_size = 50

    @property
    def origin(self) -> str:
        parsed = urlparse(self.career_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def list_jobs(self, cursor: Any = None) -> AdapterPage:
        offset = int(cursor or 0)
        response = self.session.post(
            f"{self.origin}/api/v1/search/job/posts",
            headers={
                "portal-channel": "campus",
                "portal-platform": "pc",
                "website-path": "campus",
                "Origin": self.origin,
                "Referer": self.career_url,
            },
            json={
                "limit": self.page_size,
                "offset": offset,
                "keyword": "",
                "recruitment_id_list": self.company.get("recruitment_id_list") or ["201"],
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") or {}
        records = data.get("job_post_list")
        if payload.get("code") != 0 or not isinstance(records, list):
            raise ValueError(
                f"Feishu Recruiting API error: {payload.get('message') or payload.get('code')}"
            )
        total = int(data.get("count") or data.get("total") or 0)
        next_offset = offset + len(records)
        has_more = bool(data.get("has_more")) or (
            next_offset < total if total else len(records) >= self.page_size
        )
        return AdapterPage(records, next_offset if has_more else None, not has_more)

    def stable_key(self, record: dict[str, Any]) -> str:
        return str(record.get("id") or record.get("job_post_id") or "")

    def fetch_job_detail(self, record: dict[str, Any]) -> dict[str, Any]:
        if record.get("description") and record.get("requirement"):
            return dict(record)
        job_id = self.stable_key(record)
        if not job_id:
            return dict(record)
        response = self.session.get(
            f"{self.origin}/api/v1/job/posts/{job_id}", timeout=30
        )
        if not getattr(response, "ok", True):
            return dict(record)
        payload = response.json()
        detail = (payload.get("data") or {}).get("job_post") or payload.get("data") or {}
        result = dict(record)
        if isinstance(detail, dict):
            result.update(detail)
        return result

    def normalize(self, record: dict[str, Any]) -> dict[str, Any]:
        cities = [
            str(value.get("name") or "")
            for value in record.get("city_list") or []
            if isinstance(value, dict)
        ]
        if not cities and isinstance(record.get("city_info"), dict):
            cities.append(str(record["city_info"].get("name") or ""))
        subject = record.get("job_subject") or {}
        subject_name: Any = subject.get("name") if isinstance(subject, dict) else subject
        if isinstance(subject_name, dict):
            subject_name = subject_name.get("zh_cn") or subject_name.get("i18n") or ""
        return {
            "company": _company_name(self.company),
            "title": str(record.get("title") or ""),
            "location": "/".join(dict.fromkeys(filter(None, cities))),
            "description": _plain_text(
                f"{record.get('description') or ''} {record.get('requirement') or ''}"
            ),
            "url": f"{self.origin}/campus/position/{self.stable_key(record)}/detail",
            "job_id": self.stable_key(record),
            "posted_at": str(record.get("publish_time") or ""),
            "deadline": str(record.get("deadline") or ""),
            "campaign_context": str(subject_name or "campus"),
            "ats_family": "Feishu Recruiting",
        }


def _jobposting_nodes(value: Any) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            nodes.extend(_jobposting_nodes(item))
    elif isinstance(value, dict):
        types = value.get("@type")
        type_values = types if isinstance(types, list) else [types]
        if any(str(item).casefold() == "jobposting" for item in type_values):
            nodes.append(value)
        for key in ("@graph", "itemListElement"):
            nodes.extend(_jobposting_nodes(value.get(key)))
    return nodes


def parse_jsonld_job_postings(html_text: str, base_url: str) -> list[dict[str, Any]]:
    scripts = re.findall(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    jobs: list[dict[str, Any]] = []
    for script in scripts:
        try:
            payload = json.loads(html.unescape(script).strip())
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _jobposting_nodes(payload):
            location_nodes = node.get("jobLocation") or []
            if isinstance(location_nodes, dict):
                location_nodes = [location_nodes]
            locations: list[str] = []
            for location in location_nodes:
                address = location.get("address") if isinstance(location, dict) else {}
                if isinstance(address, dict):
                    locations.append(
                        ", ".join(
                            str(address.get(key) or "")
                            for key in ("addressLocality", "addressRegion", "addressCountry")
                            if address.get(key)
                        )
                    )
            identifier = node.get("identifier") or {}
            job_id = (
                str(identifier.get("value") or "")
                if isinstance(identifier, dict)
                else str(identifier or "")
            )
            jobs.append(
                {
                    "title": str(node.get("title") or ""),
                    "location": "/".join(filter(None, locations)),
                    "description": _plain_text(node.get("description")),
                    "url": urljoin(base_url, str(node.get("url") or base_url)),
                    "job_id": job_id,
                    "posted_at": str(node.get("datePosted") or ""),
                    "deadline": str(node.get("validThrough") or ""),
                    "employment_type": node.get("employmentType") or "",
                }
            )
    return jobs


class JSONLDAdapter(OfficialSourceAdapter):
    adapter_name = "Schema.org JobPosting JSON-LD"
    supports_full_jd = True

    def list_jobs(self, cursor: Any = None) -> AdapterPage:
        response = self.session.get(self.career_url, timeout=30)
        response.raise_for_status()
        return AdapterPage(
            parse_jsonld_job_postings(response.text, self.career_url), None, True
        )

    def normalize(self, record: dict[str, Any]) -> dict[str, Any]:
        return {
            **record,
            "company": _company_name(self.company),
            "ats_family": "Schema.org JobPosting",
        }


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.current_href = ""
        self.current_text: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.next_href = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): str(value or "") for key, value in attrs}
        if tag.casefold() == "a":
            self.current_href = values.get("href", "")
            self.current_text = []
            if "next" in values.get("rel", "").casefold():
                self.next_href = self.current_href

    def handle_data(self, data: str) -> None:
        if self.current_href:
            self.current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self.current_href:
            self.links.append(
                (self.current_href, normalize_whitespace(" ".join(self.current_text)))
            )
            self.current_href = ""
            self.current_text = []


class StableHTMLAdapter(OfficialSourceAdapter):
    adapter_name = "Stable server-rendered HTML"
    supports_pagination = True
    supports_full_jd = True

    def list_jobs(self, cursor: Any = None) -> AdapterPage:
        page_url = str(cursor or self.career_url)
        response = self.session.get(page_url, timeout=30)
        response.raise_for_status()
        jsonld = parse_jsonld_job_postings(response.text, page_url)
        if jsonld:
            records = jsonld
        else:
            parser = _AnchorParser()
            parser.feed(response.text)
            records = []
            for href, title in parser.links:
                url = urljoin(page_url, href)
                path = urlparse(url).path.casefold()
                if (
                    not title
                    or title.casefold() in {"next", "previous", "下一页", "上一页"}
                    or not any(marker in path for marker in ("job", "position", "vacan"))
                ):
                    continue
                records.append({"title": title, "url": url, "description": ""})
        next_url = urljoin(page_url, parser.next_href) if not jsonld and parser.next_href else None
        return AdapterPage(records, next_url, next_url is None)

    def stable_key(self, record: dict[str, Any]) -> str:
        return str(record.get("job_id") or record.get("url") or "")

    def fetch_job_detail(self, record: dict[str, Any]) -> dict[str, Any]:
        if record.get("description"):
            return dict(record)
        url = str(record.get("url") or "")
        if not url:
            return dict(record)
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        jsonld = parse_jsonld_job_postings(response.text, url)
        if jsonld:
            result = dict(record)
            result.update(jsonld[0])
            return result
        text = _plain_text(response.text)
        result = dict(record)
        result["description"] = text[:20_000]
        return result

    def normalize(self, record: dict[str, Any]) -> dict[str, Any]:
        return {
            **record,
            "company": _company_name(self.company),
            "ats_family": str(self.company.get("ats_family") or "UNKNOWN"),
        }


class SitemapAdapter(StableHTMLAdapter):
    adapter_name = "Sitemap/public job URLs"
    supports_pagination = False

    def list_jobs(self, cursor: Any = None) -> AdapterPage:
        sitemap_url = str(self.company.get("sitemap_url") or self.career_url)
        response = self.session.get(sitemap_url, timeout=30)
        response.raise_for_status()
        try:
            root = ET.fromstring(response.text)
        except ET.ParseError as exc:
            raise ValueError(f"invalid public jobs sitemap: {exc}") from exc
        urls = [
            str(element.text or "").strip()
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] == "loc" and element.text
        ]
        records = [
            {"url": url, "title": "", "description": ""}
            for url in urls
            if any(
                marker in urlparse(url).path.casefold()
                for marker in ("job", "position", "career", "vacan")
            )
        ]
        return AdapterPage(records, None, True)

    def fetch_job_detail(self, record: dict[str, Any]) -> dict[str, Any]:
        result = super().fetch_job_detail(record)
        if not result.get("title"):
            result["title"] = str(result.get("job_id") or "")
        return result


class BrowserPublicNetworkAdapter(OfficialSourceAdapter):
    """Optional Playwright observer for public JS pages.

    It never logs in, solves challenges or submits forms.  The adapter is only
    selected by explicit registry configuration after an audit recommends it.
    """

    adapter_name = "Playwright public-page network"
    supports_pagination = True
    supports_full_jd = True

    def __init__(self, session: Any, company: dict[str, Any]):
        super().__init__(session, company)
        self._observed: list[dict[str, Any]] | None = None

    def _observe(self) -> list[dict[str, Any]]:
        if self._observed is not None:
            return self._observed
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed; keep this source BROWSER_PUBLIC until an approved runtime is available"
            ) from exc
        observed: list[dict[str, Any]] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()

            def capture(response: Any) -> None:
                content_type = str(response.headers.get("content-type") or "")
                if "json" not in content_type.casefold():
                    return
                if not any(marker in response.url.casefold() for marker in ("job", "position")):
                    return
                try:
                    payload = response.json()
                except Exception:
                    return
                observed.append({"endpoint": response.url, "payload": payload})

            page.on("response", capture)
            page.goto(self.career_url, wait_until="networkidle", timeout=30_000)
            browser.close()
        self._observed = observed
        return observed

    def list_jobs(self, cursor: Any = None) -> AdapterPage:
        records: list[dict[str, Any]] = []
        for item in self._observe():
            payload = item.get("payload")
            candidates = []
            if isinstance(payload, list):
                candidates = payload
            elif isinstance(payload, dict):
                for key in ("jobs", "positions", "job_post_list", "data"):
                    value = payload.get(key)
                    if isinstance(value, list):
                        candidates = value
                        break
            for candidate in candidates:
                if isinstance(candidate, dict):
                    records.append(candidate)
        return AdapterPage(records, None, True)

    def normalize(self, record: dict[str, Any]) -> dict[str, Any]:
        job_id = str(record.get("id") or record.get("job_id") or "")
        return {
            "company": _company_name(self.company),
            "title": str(record.get("title") or record.get("name") or ""),
            "location": str(record.get("location") or record.get("city") or ""),
            "description": _plain_text(record.get("description") or record.get("jd")),
            "url": str(record.get("url") or urljoin(self.career_url, f"position/{job_id}")),
            "job_id": job_id,
            "posted_at": str(record.get("posted_at") or record.get("publish_time") or ""),
            "deadline": str(record.get("deadline") or ""),
            "campaign_context": str(record.get("campaign_context") or ""),
            "ats_family": str(self.company.get("ats_family") or "UNKNOWN"),
        }


def adapter_for_company(session: Any, company: dict[str, Any]) -> OfficialSourceAdapter:
    family = str(company.get("ats_family") or "UNKNOWN").casefold()
    mapping: tuple[tuple[set[str], type[OfficialSourceAdapter]], ...] = (
        ({"workday"}, WorkdayAdapter),
        ({"beisen"}, BeisenAdapter),
        ({"hotjob"}, HotjobAdapter),
        ({"feishu recruiting", "feishu jobs"}, FeishuRecruitingAdapter),
        ({"schema.org jobposting", "json-ld"}, JSONLDAdapter),
        ({"sitemap", "public sitemap"}, SitemapAdapter),
        ({"browser public", "playwright public"}, BrowserPublicNetworkAdapter),
    )
    for names, adapter_class in mapping:
        if family in names:
            return adapter_class(session, company)
    return StableHTMLAdapter(session, company)
