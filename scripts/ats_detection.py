#!/usr/bin/env python3
"""Detect known public ATS and proprietary official career URL families.

URL-only detection remains available for the normalization pipeline.  Source
Discovery V2 also exposes :func:`detect_ats_evidence`, which can use public HTML,
script URLs and observed public endpoints during a read-only source audit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlparse


@dataclass(frozen=True)
class ATSFamily:
    name: str
    domains: tuple[str, ...]
    path_markers: tuple[str, ...] = ()
    region: str = "GLOBAL"
    adapter_status: str = "extension_point"


ATS_FAMILIES: tuple[ATSFamily, ...] = (
    ATSFamily("Moka", ("mokahr.com",), region="CHINA", adapter_status="generic_public"),
    ATSFamily("Beisen", ("zhiye.com",), region="CHINA", adapter_status="generic_public"),
    ATSFamily("Hotjob", ("hotjob.cn", "hotjob.com.cn"), region="CHINA"),
    ATSFamily("51job campus", ("51job.com",), ("campus",), region="CHINA"),
    ATSFamily("Zhaopin campus", ("zhaopin.com",), ("campus", "xiaoyuan"), region="CHINA"),
    ATSFamily(
        "Workday",
        ("myworkdayjobs.com", "myworkdaysite.com", "workday.com"),
        adapter_status="generic_public",
    ),
    ATSFamily("Greenhouse", ("greenhouse.io",), adapter_status="generic_public"),
    ATSFamily("Lever", ("lever.co",), adapter_status="generic_public"),
    ATSFamily("SmartRecruiters", ("smartrecruiters.com",), adapter_status="generic_public"),
    ATSFamily("SAP SuccessFactors", ("successfactors.com",)),
    ATSFamily("Oracle Recruiting Cloud / Taleo", ("oraclecloud.com", "taleo.net")),
    ATSFamily("Workable", ("workable.com",), adapter_status="generic_public"),
    ATSFamily(
        "Feishu Recruiting",
        ("jobs.bytedance.com", "jobs.ecoflow.com", "jobs.feishu.cn"),
        region="CHINA",
        adapter_status="generic_public",
    ),
    ATSFamily("Apple Jobs", ("jobs.apple.com",), adapter_status="generic_official"),
    ATSFamily("Amazon Jobs", ("amazon.jobs",), adapter_status="generic_official"),
    ATSFamily("DJI Careers", ("career.dji.com", "we.dji.com"), region="CHINA", adapter_status="generic_official"),
    ATSFamily(
        "Tencent Careers",
        ("join.qq.com", "careers.tencent.com"),
        region="CHINA",
        adapter_status="generic_official",
    ),
    ATSFamily("TME Careers", ("join.tencentmusic.com",), region="CHINA", adapter_status="generic_official"),
    ATSFamily("ASML Careers", ("asml.com",), ("career", "job"), adapter_status="generic_official"),
    ATSFamily(
        "Siemens Healthineers Careers",
        ("siemens-healthineers.com",),
        ("career", "job"),
        adapter_status="generic_official",
    ),
    ATSFamily("HSBC Careers", ("apply.careers.hsbc.com", "mycareer.hsbc.com"), adapter_status="generic_official"),
)


@dataclass(frozen=True)
class ATSDetection:
    family: str
    evidence: tuple[str, ...]
    public_endpoints: tuple[str, ...]


CONTENT_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Workday", ("/wday/cxs/", "workday/cxs", "myworkdayjobs")),
    ("Beisen", ("getjobadpagelist", "jobadid", "beisen", "zhiye.com")),
    ("Hotjob", ("/wecruit/", "listpositiondetail", "hotjob")),
    ("Feishu Recruiting", ("/api/v1/search/job/posts", "portal-channel", "job_post_list")),
    ("Moka", ("mokahr", "moka-candidate")),
    ("Greenhouse", ("boards-api.greenhouse.io", "greenhouse.io/embed")),
    ("Lever", ("api.lever.co/v0/postings", "jobs.lever.co")),
    ("SmartRecruiters", ("api.smartrecruiters.com", "smartrecruiters.com")),
)


CHINA_ATS_FAMILIES = frozenset(family.name for family in ATS_FAMILIES if family.region == "CHINA")
GLOBAL_ATS_FAMILIES = frozenset(family.name for family in ATS_FAMILIES if family.region == "GLOBAL")


def _domain_matches(hostname: str, domain: str) -> bool:
    return hostname == domain or hostname.endswith(f".{domain}")


def detect_ats_family(url: str) -> str:
    """Return a stable ATS/official family name, or ``UNKNOWN``."""

    candidate = str(url or "").strip()
    if not candidate:
        return "UNKNOWN"
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return "UNKNOWN"
    hostname = (parsed.hostname or "").casefold()
    path_and_query = f"{parsed.path} {parsed.query}".casefold()
    if not hostname:
        return "UNKNOWN"
    for family in ATS_FAMILIES:
        if not any(_domain_matches(hostname, domain) for domain in family.domains):
            continue
        if family.path_markers and not any(
            marker in hostname or marker in path_and_query for marker in family.path_markers
        ):
            continue
        return family.name
    return "UNKNOWN"


def detect_ats_evidence(
    url: str,
    *,
    html_text: str = "",
    script_urls: Iterable[str] = (),
    endpoint_urls: Iterable[str] = (),
) -> ATSDetection:
    """Detect an ATS using URL plus public-page evidence.

    This function does not perform network access.  Callers provide the HTML and
    any public script/XHR URLs they observed, keeping probing policy separate
    from classification.
    """

    url_family = detect_ats_family(url)
    evidence: list[str] = []
    endpoints = tuple(dict.fromkeys(str(value) for value in endpoint_urls if value))
    if url_family != "UNKNOWN":
        evidence.append(f"hostname/path:{url_family}")

    corpus_parts = [str(html_text or "").casefold()]
    corpus_parts.extend(str(value).casefold() for value in script_urls if value)
    corpus_parts.extend(str(value).casefold() for value in endpoints)
    corpus = "\n".join(corpus_parts)
    marker_family = "UNKNOWN"
    for family, markers in CONTENT_MARKERS:
        matched = [marker for marker in markers if marker.casefold() in corpus]
        if matched:
            marker_family = family
            evidence.extend(f"public_marker:{marker}" for marker in matched[:4])
            break

    family = url_family if url_family != "UNKNOWN" else marker_family
    if (
        url_family != "UNKNOWN"
        and marker_family != "UNKNOWN"
        and url_family != marker_family
    ):
        evidence.append(f"marker_conflict:{marker_family}")
    return ATSDetection(
        family=family,
        evidence=tuple(dict.fromkeys(evidence)),
        public_endpoints=endpoints,
    )


def ats_adapter_status(family_name: str) -> str:
    for family in ATS_FAMILIES:
        if family.name == family_name:
            return family.adapter_status
    return "unrecognized"


def canonical_source_name(ats_family: str, fallback: str = "official") -> str:
    if not ats_family or ats_family == "UNKNOWN":
        return fallback
    slug = re.sub(r"[^a-z0-9]+", "_", ats_family.casefold()).strip("_")
    return f"official_{slug}" if slug else fallback
