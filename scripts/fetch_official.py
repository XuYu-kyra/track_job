#!/usr/bin/env python3
"""Conservatively discover job links from registered public career pages."""

from __future__ import annotations

import argparse
import html
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

try:
    from ats_detection import canonical_source_name, detect_ats_family
    from common import load_config, normalize_token, normalize_whitespace
    from official_adapters import (
        DEFAULT_SAFETY_CAP,
        BeisenAdapter,
        FeishuRecruitingAdapter,
        HotjobAdapter,
        WorkdayAdapter,
        adapter_for_company,
    )
    from source_adapters import load_source_registry
    from source_health import record_source_health
except ModuleNotFoundError:
    from scripts.ats_detection import canonical_source_name, detect_ats_family
    from scripts.common import load_config, normalize_token, normalize_whitespace
    from scripts.official_adapters import (
        DEFAULT_SAFETY_CAP,
        BeisenAdapter,
        FeishuRecruitingAdapter,
        HotjobAdapter,
        WorkdayAdapter,
        adapter_for_company,
    )
    from scripts.source_adapters import load_source_registry
    from scripts.source_health import record_source_health


HEADERS = {
    "User-Agent": "jobhunter-campus-discovery/1.0 (+registered public career pages)",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
}
MACOS_HEADERS = {
    **HEADERS,
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


@dataclass
class OfficialListing:
    company: str
    position: str
    url: str
    company_type: str = ""
    date: str = ""
    location: str = ""
    source: str = "official"
    description: str = ""
    page_context: str = ""
    campaign_context: str = ""
    status: str = "OPEN"
    search_keyword: str = "registered_official_page"
    job_id: str = ""
    source_name: str = "official"
    source_type: str = "official_career"
    discovered_by: tuple[str, ...] = ("official_monitor",)
    canonical_source: str = "official"
    canonical_url: str = ""
    ats_family: str = "UNKNOWN"
    evidence_confidence: str = "High"
    location_evidence: tuple[str, ...] = ()
    observation_origin: str = "CACHE_REPLAY"
    observed_at: str = ""
    verification_level: str = ""
    verified_at: str = ""
    last_verified: str = ""
    canonical_authority: str = ""


class CareerPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, Any]] = []
        self.text_parts: list[str] = []
        self._href = ""
        self._anchor_text: list[str] = []
        self._anchor_attrs: dict[str, str] = {}
        self._anchor_container: dict[str, Any] | None = None
        self._elements: list[dict[str, Any]] = []

    @staticmethod
    def _class_tokens(attributes: dict[str, str]) -> tuple[str, ...]:
        raw = f"{attributes.get('class', '')} {attributes.get('id', '')}"
        # Preserve each class/id boundary while normalizing camelCase and BEM
        # variants such as jobListing and jobs-list__item.
        raw = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", raw)
        return tuple(
            token.casefold().strip("-_")
            for token in re.split(r"\s+", raw)
            if token.strip("-_")
        )

    @classmethod
    def _is_list_wrapper(cls, attributes: dict[str, str]) -> bool:
        for token in cls._class_tokens(attributes):
            normalized = token.replace("_", "-")
            if normalized in {
                "jobs-list",
                "job-list",
                "positions-list",
                "position-list",
                "openings-list",
                "opening-list",
                "vacancies-list",
                "vacancy-list",
                "careers-list",
                "career-list",
                "results-list",
                "job-results",
                "job-search-results",
                "jobs-wrapper",
                "job-wrapper",
                "jobs-container",
                "job-container",
                "job-grid",
            }:
                return True
            if re.fullmatch(
                r"(?:jobs?|positions?|openings?|vacanc(?:y|ies)|careers?)-(?:list|results?|wrapper|container|grid)",
                normalized,
            ):
                return True
        return False

    @staticmethod
    def _has_stable_job_id(attributes: dict[str, str]) -> bool:
        stable_keys = {
            "data-job-id",
            "data-jobid",
            "data-req-id",
            "data-requisition-id",
            "data-position-id",
            "data-opening-id",
        }
        if any(attributes.get(key, "").strip() for key in stable_keys):
            return True
        element_id = attributes.get("id", "").strip()
        return bool(
            element_id
            and re.search(
                r"(?:^|[-_])(?:job|req(?:uisition)?|position|opening)[-_]?(?:id[-_]?)?[a-z0-9]*\d[a-z0-9_-]*(?:$|[-_])",
                element_id,
                flags=re.IGNORECASE,
            )
        )

    @classmethod
    def _has_job_card_marker(cls, attributes: dict[str, str]) -> bool:
        entity = r"(?:jobs?|req(?:uisition)?s?|positions?|openings?|careers?|vacanc(?:y|ies)|roles?)"
        item = r"(?:card|item|row|result|posting|listing|tile|entry)"
        for token in cls._class_tokens(attributes):
            normalized = token.replace("_", "-")
            if re.fullmatch(rf"{entity}-{item}", normalized):
                return True
            # Common BEM/container variants: jobs-list__item,
            # job-search-result-card, and position-listing--featured.
            parts = tuple(part for part in re.split(r"[-_]+", normalized) if part)
            if parts and re.fullmatch(item, parts[-1]) and any(
                re.fullmatch(entity, part) for part in parts[:-1]
            ):
                return True
        return False

    def _li_is_list_item(self) -> bool:
        # Do not turn metadata bullets inside an already identified card into
        # competing job contexts. The closest semantic list outside a card is
        # sufficient for an otherwise unclassified <li>.
        if any(element.get("context") is not None for element in self._elements):
            return False
        closest_list_index = next(
            (
                index
                for index in range(len(self._elements) - 1, -1, -1)
                if self._elements[index]["tag"] in {"ul", "ol"}
            ),
            None,
        )
        if closest_list_index is None:
            return False
        if self._is_list_wrapper(self._elements[closest_list_index]["attrs"]):
            return True
        return any(
            self._is_list_wrapper(element["attrs"])
            for element in self._elements[:closest_list_index]
        )

    def _is_job_container(self, tag: str, attributes: dict[str, str]) -> bool:
        if self._is_list_wrapper(attributes):
            return False
        if self._has_stable_job_id(attributes) or self._has_job_card_marker(attributes):
            return True
        if tag in {"article", "tr"}:
            return True
        return tag == "li" and self._li_is_list_item()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered_tag = tag.casefold()
        attributes = {key.casefold(): value or "" for key, value in attrs}
        context = None
        if self._is_job_container(lowered_tag, attributes):
            context = {"text_parts": [], "attrs": attributes}
        if lowered_tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self._elements.append(
                {"tag": lowered_tag, "attrs": attributes, "context": context}
            )
        if lowered_tag != "a":
            return
        self._href = attributes.get("href") or ""
        self._anchor_text = []
        self._anchor_attrs = attributes
        self._anchor_container = next(
            (
                element["context"]
                for element in reversed(self._elements)
                if element.get("context") is not None
            ),
            None,
        )

    def handle_data(self, data: str) -> None:
        cleaned = normalize_whitespace(data)
        if cleaned:
            self.text_parts.append(cleaned)
            seen_contexts: set[int] = set()
            for element in self._elements:
                context = element.get("context")
                if context is None or id(context) in seen_contexts:
                    continue
                seen_contexts.add(id(context))
                context["text_parts"].append(cleaned)
            if self._href:
                self._anchor_text.append(cleaned)

    def handle_endtag(self, tag: str) -> None:
        lowered_tag = tag.casefold()
        if lowered_tag == "a" and self._href:
            self.links.append(
                {
                    "href": self._href,
                    "text": normalize_whitespace(" ".join(self._anchor_text)),
                    "attrs": self._anchor_attrs,
                    "container": self._anchor_container,
                }
            )
            self._href = ""
            self._anchor_text = []
            self._anchor_attrs = {}
            self._anchor_container = None
        for index in range(len(self._elements) - 1, -1, -1):
            if self._elements[index]["tag"] == lowered_tag:
                del self._elements[index:]
                break


NAVIGATION_LABELS = {
    "about",
    "about us",
    "careers",
    "contact",
    "home",
    "jobs",
    "learn more",
    "login",
    "privacy",
    "search",
    "关于我们",
    "全部职位",
    "加入我们",
    "首页",
    "联系我们",
    "职位搜索",
    "隐私政策",
}

ROLE_TITLE_MARKERS = (
    "engineer",
    "developer",
    "analyst",
    "scientist",
    "architect",
    "specialist",
    "intern",
    "工程师",
    "开发",
    "研发",
    "测试",
    "分析师",
    "科学家",
    "架构师",
    "实习",
)

JOB_PATH_MARKERS = (
    "/job/",
    "/jobs/",
    "/position/",
    "/positions/",
    "/vacancy/",
    "/opening/",
    "/requisition/",
    "jobdetail",
    "job_detail",
    "post.html",
)

CITY_ALIASES = (
    ("深圳", "深圳"),
    ("shenzhen", "Shenzhen"),
    ("北京", "北京"),
    ("beijing", "Beijing"),
    ("上海", "上海"),
    ("shanghai", "Shanghai"),
    ("广州", "广州"),
    ("guangzhou", "Guangzhou"),
    ("杭州", "杭州"),
    ("hangzhou", "Hangzhou"),
    ("香港", "香港"),
    ("hong kong", "Hong Kong"),
)


def _is_plausible_job_link(
    href: str,
    anchor_text: str,
    local_context: str,
    aliases: list[str],
) -> bool:
    normalized_anchor = normalize_token(anchor_text)
    if not normalized_anchor or normalized_anchor in NAVIGATION_LABELS:
        return False
    if len(anchor_text) > 180 or href.startswith(("#", "javascript:", "mailto:")):
        return False
    lowered_href = href.casefold()
    structural = any(marker in lowered_href for marker in JOB_PATH_MARKERS)
    title_like = any(marker in normalized_anchor for marker in ROLE_TITLE_MARKERS)
    alias_like = any(
        normalized_alias in normalized_anchor
        for alias in aliases
        if (normalized_alias := normalize_token(alias))
    )
    container = normalize_token(local_context)
    local_job_marker = any(marker in container for marker in ROLE_TITLE_MARKERS)
    return structural or title_like or alias_like or local_job_marker


def _job_location(
    anchor_text: str,
    local_context: str,
    attributes: dict[str, str],
) -> tuple[str, tuple[str, ...]]:
    attributed = normalize_whitespace(
        " ".join(
            attributes.get(key, "")
            for key in ("data-location", "data-city", "location", "city")
        )
    )
    candidates = [
        ("attribute", attributed),
        ("job_card", local_context),
        ("title", anchor_text),
    ]
    for source, text in candidates:
        normalized = normalize_token(text)
        if not normalized:
            continue
        for alias, canonical in CITY_ALIASES:
            if normalize_token(alias) in normalized:
                return canonical, (f"{source}:{alias}",)
        explicit = re.search(
            r"(?:location|工作地点|职位地点|地点|城市)\s*[:：-]?\s*([A-Za-z\u4e00-\u9fff ]{2,30})",
            text,
            flags=re.IGNORECASE,
        )
        if explicit:
            value = normalize_whitespace(explicit.group(1))
            if value:
                return value, (f"{source}:{value}",)
    return "", ()


def _all_aliases(taxonomies: dict[str, Any], targets: dict[str, Any]) -> list[str]:
    profile = targets.get("candidate_profile", {})
    families = list(profile.get("preferred_role_families", []))
    families.extend(profile.get("expansion_role_families", []))
    aliases: list[str] = []
    for family in dict.fromkeys(str(item) for item in families):
        aliases.extend(
            str(item)
            for item in taxonomies.get("role_families", {}).get(family, {}).get("aliases", [])
            if item
        )
    return list(dict.fromkeys(aliases))


def _plain_text(value: Any) -> str:
    return normalize_whitespace(html.unescape(re.sub(r"<[^>]+>", " ", str(value or ""))))


def _relevant_listing(title: str, description: str, location: str, aliases: list[str]) -> bool:
    haystack = normalize_token(f"{title} {description}")
    role_markers = tuple(ROLE_TITLE_MARKERS) + tuple(normalize_token(item) for item in aliases)
    role_match = any(marker and marker in haystack for marker in role_markers)
    target_context = any(
        marker in normalize_token(f"{title} {description} {location}")
        for marker in ("2027", "27届", "campus", "graduate", "深圳", "shenzhen")
    )
    return role_match and target_context


def _listing(
    *, company: str, title: str, url: str, location: str, description: str,
    ats_family: str, observed_on: str, posted_at: str = "", deadline: str = "",
    campaign_context: str = "", job_id: str = "",
) -> OfficialListing:
    return OfficialListing(
        company=company,
        position=normalize_whitespace(title),
        url=url,
        date=posted_at,
        location=normalize_whitespace(location),
        description=normalize_whitespace(description),
        page_context=normalize_whitespace(description)[:12000],
        campaign_context=normalize_whitespace(campaign_context),
        status="OPEN",
        job_id=job_id,
        source_name="official_ats",
        source_type="official_ats",
        canonical_source=canonical_source_name(ats_family),
        canonical_url=url,
        ats_family=ats_family,
        location_evidence=(f"job_record:{location}",) if location else (),
        observation_origin="LIVE_FETCH",
        observed_at=observed_on,
        verification_level="CANONICAL",
        verified_at=observed_on,
        last_verified=observed_on,
        canonical_authority="OFFICIAL_MONITOR",
    )


def _fetch_workday(session: requests.Session, company: dict[str, Any], aliases: list[str], limit: int) -> list[OfficialListing]:
    career_url = str(company["official_career_url"])
    parsed = urlparse(career_url)
    parts = [part for part in parsed.path.split("/") if part]
    if parts and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
        parts = parts[1:]
    if not parts:
        raise ValueError("Workday career URL must include the career-site name")
    site = parts[0]
    tenant = parsed.hostname.split(".")[0] if parsed.hostname else ""
    endpoint = f"{parsed.scheme}://{parsed.netloc}/wday/cxs/{tenant}/{site}/jobs"
    response = session.post(
        endpoint,
        json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "2027 China"},
        timeout=30,
    )
    response.raise_for_status()
    postings = response.json().get("jobPostings")
    if not isinstance(postings, list):
        raise ValueError("Workday response missing jobPostings")
    results: list[OfficialListing] = []
    observed_on = date.today().isoformat()
    for item in postings:
        title = str(item.get("title") or "")
        location = str(item.get("locationsText") or "")
        external_path = str(item.get("externalPath") or "")
        if not title or not external_path or not _relevant_listing(title, "2027", location, aliases):
            continue
        detail_url = f"{parsed.scheme}://{parsed.netloc}/wday/cxs/{tenant}/{site}{external_path}"
        detail = session.get(detail_url, timeout=30)
        detail.raise_for_status()
        info = detail.json().get("jobPostingInfo") or {}
        description = _plain_text(info.get("jobDescription"))
        detail_locations = [str(info.get("location") or location)]
        detail_locations.extend(
            str(value) for value in (info.get("additionalLocations") or []) if value
        )
        resolved_location = "/".join(dict.fromkeys(filter(None, detail_locations)))
        public_url = urljoin(career_url.rstrip("/") + "/", external_path.lstrip("/"))
        results.append(_listing(
            company=str(company.get("name") or ""), title=title, url=public_url,
            location=resolved_location, description=description,
            ats_family="Workday", observed_on=observed_on,
            posted_at=str(info.get("startDate") or ""), campaign_context="2027",
        ))
        if len(results) >= limit:
            break
    return results


def _fetch_beisen(session: requests.Session, company: dict[str, Any], aliases: list[str], limit: int) -> list[OfficialListing]:
    career_url = str(company["official_career_url"])
    endpoint = urljoin(career_url, "/api/JobAd/GetJobAdPageList")
    response = session.post(endpoint, json={"pageIndex": 1, "pageSize": 50}, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if payload.get("Code") != 200 or not isinstance(payload.get("Data"), list):
        raise ValueError(f"Beisen list API error: {payload.get('Message') or payload.get('Code')}")
    results: list[OfficialListing] = []
    observed_on = date.today().isoformat()
    for item in payload["Data"]:
        title = str(item.get("JobAdName") or "")
        locations = item.get("LocNames") or item.get("WorkPlaceNames") or []
        location = "/".join(str(value) for value in locations) if isinstance(locations, list) else str(locations or "")
        description = normalize_whitespace(f"{item.get('Duty') or ''} {item.get('Qualification') or item.get('Requirement') or ''}")
        if not title or not _relevant_listing(title, f"{description} campus", location, aliases):
            continue
        job_id = str(item.get("Id") or "")
        public_url = urljoin(career_url, f"/campus/detail?jobAdId={job_id}")
        results.append(_listing(
            company=str(company.get("name") or ""), title=title, url=public_url,
            location=location, description=description, ats_family="Beisen",
            observed_on=observed_on, campaign_context="campus",
        ))
        if len(results) >= limit:
            break
    return results


def _fetch_hotjob(session: requests.Session, company: dict[str, Any], aliases: list[str], limit: int) -> list[OfficialListing]:
    career_url = str(company["official_career_url"])
    resolved = session.post(
        urljoin(career_url, "/wecruit/common/getSLD"),
        data={"sld": urlparse(career_url).netloc}, timeout=30,
    )
    resolved.raise_for_status()
    portal = ((resolved.json().get("data") or {}).get("linkData") or {}).get("link")
    match = re.search(r"/(SU[a-zA-Z0-9]+)/", str(portal or ""))
    if not match:
        raise ValueError("Hotjob domain did not resolve to a suite key")
    suite = match.group(1)
    endpoint = urljoin(career_url, f"/wecruit/positionInfo/listPosition/{suite}")
    results: list[OfficialListing] = []
    observed_on = date.today().isoformat()
    seen: set[str] = set()
    for keyword in ("软件", "测试", "大模型", "机器人"):
        response = session.post(
            endpoint,
            params={"iSaJAx": "isAjax", "request_locale": "zh_CN", "t": int(time.time() * 1000)},
            data={"isFrompb": "true", "recruitType": 1, "pageSize": 12, "currentPage": 1, "postKey": keyword},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if str(payload.get("state")) != "200":
            raise ValueError(f"Hotjob list API error: {payload.get('msg') or payload.get('state')}")
        records = (((payload.get("data") or {}).get("pageForm") or {}).get("pageData") or [])
        if not isinstance(records, list):
            raise ValueError("Hotjob response missing pageData")
        for item in records:
            post_id = str(item.get("postId") or "")
            title = str(item.get("postName") or "")
            location = str(item.get("workPlaceStr") or "")
            campaign = str(item.get("projectName") or "")
            # Keep this bounded daily monitor Shenzhen-focused. A missing
            # location remains eligible so uncertainty is preserved, while an
            # explicitly non-Shenzhen record must not consume the result cap.
            if location and not any(
                marker in normalize_token(location) for marker in ("深圳", "shenzhen")
            ):
                continue
            if not post_id or post_id in seen or not _relevant_listing(title, campaign, location, aliases):
                continue
            seen.add(post_id)
            detail_response = session.post(
                urljoin(career_url, f"/wecruit/positionInfo/listPositionDetail/{suite}"),
                params={"iSaJAx": "isAjax", "request_locale": "zh_CN", "t": int(time.time() * 1000)},
                data={"postId": post_id}, timeout=30,
            )
            detail_response.raise_for_status()
            detail_payload = detail_response.json()
            detail_data = detail_payload.get("data") if str(detail_payload.get("state")) == "200" else {}
            description = normalize_whitespace(" ".join(
                str((detail_data or {}).get(key) or "")
                for key in ("workContent", "qualification", "postDuty", "postRequirement", "subject")
            )) or campaign
            public_url = urljoin(career_url, f"/{suite}/pb/posDetail.html?postId={post_id}&postType=campus")
            results.append(_listing(
                company=str(company.get("name") or ""), title=title, url=public_url,
                location=location, description=description, ats_family="Hotjob",
                observed_on=observed_on, posted_at=str(item.get("publishDate") or ""),
                deadline=str(item.get("endDate") or ""), campaign_context=campaign,
            ))
            if len(results) >= limit:
                return results
    return results


def _fetch_feishu(session: requests.Session, company: dict[str, Any], aliases: list[str], limit: int) -> list[OfficialListing]:
    career_url = str(company["official_career_url"])
    origin = f"{urlparse(career_url).scheme}://{urlparse(career_url).netloc}"
    response = session.post(
        f"{origin}/api/v1/search/job/posts",
        headers={**MACOS_HEADERS, "portal-channel": "campus", "portal-platform": "pc", "website-path": "campus", "Origin": origin, "Referer": career_url},
        json={"limit": 20, "offset": 0, "keyword": "深圳 2027", "recruitment_id_list": ["201"]},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    records = (payload.get("data") or {}).get("job_post_list")
    if payload.get("code") != 0 or not isinstance(records, list):
        raise ValueError(f"Feishu Recruiting API error: {payload.get('message') or payload.get('code')}")
    results: list[OfficialListing] = []
    observed_on = date.today().isoformat()
    target_title_markers = (
        "大模型", "agent", "llm", "测试", "软件", "后端", "机器人",
        "machine learning", "software", "backend", "robot", "sdet",
    )
    for item in records:
        title = str(item.get("title") or "")
        location_values = [str(value.get("name") or "") for value in (item.get("city_list") or []) if isinstance(value, dict)]
        if not location_values and isinstance(item.get("city_info"), dict):
            location_values.append(str(item["city_info"].get("name") or ""))
        location = "/".join(filter(None, location_values))
        description = normalize_whitespace(f"{item.get('description') or ''} {item.get('requirement') or ''}")
        normalized_title = normalize_token(title)
        if not any(normalize_token(marker) in normalized_title for marker in target_title_markers):
            continue
        if not title or not _relevant_listing(title, description, location, aliases):
            continue
        job_id = str(item.get("id") or "")
        public_url = f"{origin}/campus/position/{job_id}/detail"
        subject = item.get("job_subject") or {}
        subject_name = subject.get("name") or ""
        if isinstance(subject_name, dict):
            subject_name = subject_name.get("zh_cn") or subject_name.get("i18n") or ""
        publish_time = item.get("publish_time")
        posted = datetime.fromtimestamp(publish_time / 1000).date().isoformat() if isinstance(publish_time, (int, float)) else ""
        results.append(_listing(
            company=str(company.get("name") or ""), title=title, url=public_url,
            location=location, description=description, ats_family="Feishu Recruiting",
            observed_on=observed_on, posted_at=posted, campaign_context=str(subject_name),
        ))
        if len(results) >= limit:
            break
    return results


def _adapter_records_to_listings(
    records: list[dict[str, Any]],
    company: dict[str, Any],
    aliases: list[str],
) -> list[OfficialListing]:
    """Filter only after the selected adapter completed collection/hydration."""

    observed_on = date.today().isoformat()
    listings: list[OfficialListing] = []
    for item in records:
        title = str(item.get("title") or "")
        location = str(item.get("location") or "")
        description = str(item.get("description") or "")
        campaign = str(item.get("campaign_context") or "")
        if not title or not _relevant_listing(
            title, f"{description} {campaign}", location, aliases
        ):
            continue
        ats_family = str(
            item.get("ats_family") or company.get("ats_family") or "UNKNOWN"
        )
        listings.append(
            _listing(
                company=str(company.get("name") or item.get("company") or ""),
                title=title,
                url=str(item.get("url") or ""),
                location=location,
                description=description,
                ats_family=ats_family,
                observed_on=observed_on,
                posted_at=str(item.get("posted_at") or ""),
                deadline=str(item.get("deadline") or ""),
                campaign_context=campaign,
                job_id=str(item.get("job_id") or ""),
            )
        )
    return listings


def _raw_record_maybe_relevant(record: dict[str, Any], aliases: list[str]) -> bool:
    """Choose detail hydration candidates after list pagination has completed."""

    title = str(
        record.get("title")
        or record.get("JobAdName")
        or record.get("postName")
        or record.get("Name")
        or ""
    )
    location_value = (
        record.get("location")
        or record.get("locationsText")
        or record.get("LocNames")
        or record.get("WorkPlaceNames")
        or record.get("workPlaceStr")
        or record.get("city_list")
        or ""
    )
    context = str(
        record.get("description")
        or record.get("projectName")
        or record.get("subtitle")
        or record.get("RecruitTypeName")
        or record.get("Duty")
        or record.get("Qualification")
        or ""
    )
    return _relevant_listing(title, context, str(location_value), aliases)


def _hydrate_apple_listings(session: requests.Session, listings: list[OfficialListing]) -> None:
    for listing in listings:
        response = session.get(listing.url, timeout=30)
        response.raise_for_status()
        match = re.search(
            r'window\.__staticRouterHydrationData\s*=\s*JSON\.parse\(("(?:\\.|[^"\\])*")\)',
            response.text,
        )
        if not match:
            raise ValueError("Apple detail page missing hydration data")
        payload = json.loads(json.loads(match.group(1)))
        job = (((payload.get("loaderData") or {}).get("jobDetails") or {}).get("jobsData") or {})
        description = normalize_whitespace(" ".join(
            str(job.get(key) or "")
            for key in ("jobSummary", "description", "minimumQualifications", "preferredQualifications")
        ))
        if description:
            listing.description = description
            listing.page_context = description[:12000]
        locations = [str(item.get("name") or "") for item in (job.get("locations") or []) if isinstance(item, dict)]
        if locations:
            listing.location = "/".join(dict.fromkeys(filter(None, locations)))
            listing.location_evidence = tuple(f"job_record:{value}" for value in locations if value)
        listing.date = str(job.get("postingDateMeta") or listing.date)


def _dedupe_apple_listings(listings: list[OfficialListing]) -> list[OfficialListing]:
    """Collapse Apple locale/location variants of the same requisition."""

    deduped: list[OfficialListing] = []
    seen: set[str] = set()
    for listing in listings:
        match = re.search(r"/details/(?P<id>\d{5,})(?:-|/|$)", listing.url)
        key = match.group("id") if match else listing.url
        if key in seen:
            continue
        seen.add(key)
        deduped.append(listing)
    return deduped


def parse_career_page(
    html_text: str,
    *,
    company: str,
    career_url: str,
    location: str,
    company_type: str,
    aliases: list[str],
    graduation_terms: list[str],
    max_links: int,
    ats_family: str = "UNKNOWN",
    verified_on: str = "",
) -> list[OfficialListing]:
    parser = CareerPageParser()
    parser.feed(html_text)
    listings: list[OfficialListing] = []
    seen: set[str] = set()
    for link in parser.links:
        href = str(link.get("href") or "")
        anchor_text = str(link.get("text") or "")
        container = link.get("container") or {}
        local_context = normalize_whitespace(
            " ".join(container.get("text_parts", []))
        ) or anchor_text
        if not _is_plausible_job_link(href, anchor_text, local_context, aliases):
            continue
        url = urljoin(career_url, href)
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        campaign_haystack = normalize_token(f"{local_context} {urlparse(url).path}")
        campaign_hits = [
            term
            for term in graduation_terms
            if normalize_token(term) and normalize_token(term) in campaign_haystack
        ]
        campaign_context = " ".join(campaign_hits[:5])
        job_location, location_evidence = _job_location(
            anchor_text,
            local_context,
            {**container.get("attrs", {}), **link.get("attrs", {})},
        )
        listings.append(
            OfficialListing(
                company=company,
                position=anchor_text,
                url=url,
                company_type=company_type,
                # Registry location is only monitoring relevance; never copy it
                # into a job unless this job's own card/metadata provides it.
                location=job_location,
                description=anchor_text,
                page_context=local_context[:2000],
                campaign_context=campaign_context,
                source_name="official_ats" if ats_family != "UNKNOWN" else "official",
                source_type="official_ats" if ats_family != "UNKNOWN" else "official_career",
                canonical_source=canonical_source_name(ats_family),
                canonical_url=url,
                ats_family=ats_family,
                location_evidence=location_evidence,
                observation_origin="LIVE_FETCH" if verified_on else "CACHE_REPLAY",
                observed_at=verified_on,
                verification_level="CANONICAL" if verified_on else "",
                verified_at=verified_on,
                last_verified=verified_on,
                canonical_authority="OFFICIAL_MONITOR" if verified_on else "",
            )
        )
        if len(listings) >= max_links:
            break
    return listings


def fetch_registered_pages(
    targets: dict[str, Any],
    taxonomies: dict[str, Any],
    registry: dict[str, Any],
    *,
    max_links_per_company: int = DEFAULT_SAFETY_CAP,
    record_health: bool = False,
    continuation_state_path: str | Path = "",
) -> list[OfficialListing]:
    aliases = _all_aliases(taxonomies, targets)
    graduation_terms = [
        str(item) for item in targets.get("job_search", {}).get("title_keywords_include", []) if item
    ]
    session = requests.Session()
    session.headers.update(HEADERS)
    collected: list[OfficialListing] = []
    health_details: list[dict[str, Any]] = []
    state_path = Path(continuation_state_path) if continuation_state_path else None
    continuation_state: dict[str, Any] = {}
    if state_path and state_path.is_file():
        try:
            loaded_state = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(loaded_state, dict):
                continuation_state = loaded_state
        except (OSError, UnicodeError, json.JSONDecodeError):
            continuation_state = {}
    for company in registry.get("companies", []):
        if not isinstance(company, dict) or not bool(company.get("enabled", True)):
            continue
        monitor_mode = str(company.get("monitor_mode") or "AUTO").upper()
        if monitor_mode != "AUTO":
            continue
        career_url = str(
            company.get("official_career_url") or company.get("career_url") or ""
        ).strip()
        if not career_url:
            continue
        configured_ats = str(company.get("ats_family") or company.get("ats_type") or "UNKNOWN")
        ats_family = configured_ats if configured_ats != "UNKNOWN" else detect_ats_family(career_url)
        name = str(company.get("name") or career_url)
        before = len(collected)
        try:
            selected_company = dict(company)
            selected_company["ats_family"] = ats_family
            adapter = adapter_for_company(session, selected_company)
            state_key = normalize_token(name)
            run = adapter.collect(
                safety_cap=max_links_per_company,
                detail_predicate=lambda record: _raw_record_maybe_relevant(
                    record, aliases
                ),
                start_cursor=(continuation_state.get(state_key) or {}).get("cursor"),
            )
            if run.truncated and run.continuation_cursor is not None:
                continuation_state[state_key] = {
                    "company": name,
                    "adapter": run.adapter,
                    "cursor": run.continuation_cursor,
                    "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
            else:
                continuation_state.pop(state_key, None)
            jobs = _adapter_records_to_listings(run.records, selected_company, aliases)
            if normalize_token(ats_family) == "apple jobs":
                _hydrate_apple_listings(session, jobs)
                jobs = _dedupe_apple_listings(jobs)
            collected.extend(jobs)
            health_details.append({
                "company": name,
                "ats_family": ats_family,
                "adapter": run.adapter,
                "status": run.status,
                "count": len(jobs),
                "listed_count": run.listed_count,
                "hydrated_count": run.hydrated_count,
                "url": career_url,
                "pagination_complete": run.pagination_complete,
                "pages_attempted": run.pages_attempted,
                "pages_succeeded": run.pages_succeeded,
                "truncated": run.truncated,
                "reason": run.reason,
                "continuation_cursor": run.continuation_cursor,
            })
        except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError) as exc:
            print(f"Official page warning for {name}: {exc}")
            health_details.append({
                "company": name,
                "ats_family": ats_family,
                "status": "FAILED",
                "count": len(collected) - before,
                "url": career_url,
                "error": str(exc)[:300],
            })
    if state_path:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_state = state_path.with_name(state_path.name + ".tmp")
        temporary_state.write_text(
            json.dumps(continuation_state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_state.replace(state_path)
    attempted = len(health_details)
    succeeded = sum(
        item["status"] in {"SUCCESS", "EMPTY_VALID"} for item in health_details
    )
    failed = sum(item["status"] == "FAILED" for item in health_details)
    partial = sum(
        item["status"] in {"PARTIAL", "PARTIAL_RATE_LIMITED"}
        for item in health_details
    )
    if partial or (failed and succeeded):
        status = "PARTIAL"
    elif failed:
        status = "FAILED"
    elif collected:
        status = "SUCCESS"
    else:
        status = "EMPTY_VALID" if attempted else "DISABLED"
    if record_health:
        record_source_health(
            "official_ats",
            status,
            count=len(collected),
            attempted=attempted,
            succeeded=succeeded,
            failed=failed,
            details=health_details,
            automatic=True,
        )
    return collected


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch registered public official career pages.")
    parser.add_argument("--config", default="config/targets.yaml")
    parser.add_argument("--taxonomy-config", default="config/taxonomies.yaml")
    parser.add_argument("--source-registry", default="config/company_registry.yaml")
    parser.add_argument("--mode", choices=("daily", "backfill"), default="daily")
    parser.add_argument("--output", default="")
    parser.add_argument(
        "--continuation-state",
        default="data/job_cache/official_continuations.json",
    )
    parser.add_argument(
        "--max-links-per-company",
        type=int,
        default=DEFAULT_SAFETY_CAP,
        help="Hard per-company safety cap; pagination remains complete below this cap",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    jobs = fetch_registered_pages(
        load_config(args.config),
        load_config(args.taxonomy_config),
        load_source_registry(args.source_registry),
        max_links_per_company=args.max_links_per_company,
        record_health=True,
        continuation_state_path=args.continuation_state,
    )
    default_name = "official_jobs.json" if args.mode == "daily" else "official_backfill_jobs.json"
    output = Path(args.output or f"data/job_cache/{default_name}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps([asdict(job) for job in jobs], indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(jobs)} official {args.mode} jobs to {output}")


if __name__ == "__main__":
    main()
