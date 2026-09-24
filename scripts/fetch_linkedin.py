#!/usr/bin/env python3
"""Fetch recent LinkedIn jobs from public guest pages.

This uses LinkedIn's public jobs guest endpoints as a first-pass collection
method. It is intentionally conservative:
- fetch a bounded, role-balanced graduation/alias/location matrix
- deduplicate aggressively
- tolerate partial failures
- keep a manual fallback file when guest fetching is blocked
"""

from __future__ import annotations

import argparse
import html
import json
import random
import re
import time
from datetime import datetime, timezone
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlencode

import requests

try:
    from common import load_config, normalize_whitespace, read_json
    from query_matrix import (
        build_query_matrix,
        deterministic_daily_batch_index,
        posted_window_hours,
    )
    from source_health import record_source_health
    from linkedin_location import linkedin_location_scope, linkedin_query_region
except ModuleNotFoundError:
    from scripts.common import load_config, normalize_whitespace, read_json
    from scripts.query_matrix import (
        build_query_matrix,
        deterministic_daily_batch_index,
        posted_window_hours,
    )
    from scripts.source_health import record_source_health
    from scripts.linkedin_location import linkedin_location_scope, linkedin_query_region


@dataclass
class JobListing:
    company: str
    position: str
    url: str
    date: str
    location: str = ""
    source: str = "linkedin"
    description: str = ""
    easy_apply: bool = False
    search_keyword: str = ""
    job_id: str = ""
    campaign_context: str = ""
    observation_origin: str = "CACHE_REPLAY"
    observed_at: str = ""
    location_scope: str = ""
    location_scope_reason: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch LinkedIn jobs for the daily pipeline.")
    parser.add_argument("--config", default="config/targets.yaml")
    parser.add_argument("--taxonomy-config", default="config/taxonomies.yaml")
    parser.add_argument("--output", default="")
    parser.add_argument("--mode", choices=("daily", "backfill"), default="daily")
    parser.add_argument("--batch-index", type=int, default=-1)
    parser.add_argument("--limit-per-query", type=int, default=8)
    parser.add_argument("--detail-limit-total", type=int, default=24)
    parser.add_argument(
        "--search-page-cycle",
        type=int,
        default=3,
        help="Rotate LinkedIn guest result offsets across this many daily pages",
    )
    parser.add_argument("--rate-limit-retries", type=int, default=2)
    return parser.parse_args()


SEARCH_ENDPOINT = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_ENDPOINT = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
}


def fetch_jobs(
    config_path: str,
    limit_per_query: int,
    detail_limit_total: int,
    *,
    taxonomy_config: str = "config/taxonomies.yaml",
    mode: str = "daily",
    batch_index: int = 0,
    search_page_cycle: int = 3,
    rate_limit_retries: int = 2,
    health: dict | None = None,
) -> list[JobListing]:
    config = load_config(config_path)
    search_config = config.get("job_search", {})
    taxonomies = load_config(taxonomy_config)
    posted_hours = posted_window_hours(config, mode)
    queries = build_query_matrix(
        config,
        taxonomies,
        mode=mode,
        source="linkedin",
        batch_index=batch_index,
    )
    cache_fallback = Path(config.get("output", {}).get("cache_dir", "data/job_cache")) / "manual_linkedin_jobs.json"

    collected: list[JobListing] = []
    health = health if health is not None else {}
    health.update({
        "planned": len(queries),
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "fallback": False,
        "rate_limited": False,
        "batch_index": batch_index,
        "query_receipts": [],
        "unexecuted_queries": [],
        "raw_discovered_count": 0,
        "deduplicated_count": 0,
        "deferred_count": 0,
        "region_coverage": {},
        "filtered_out_count": 0,
        "filtered_out_examples": [],
    })
    session = requests.Session()
    session.headers.update(HEADERS)
    mode_config = search_config.get("search_modes", {}).get(mode, {})
    enough_raw_jobs = int(mode_config.get("batch_size", 30))

    for query_index, query in enumerate(queries):
        health["attempted"] += 1
        keyword = query["keywords"]
        region = query["location"]
        region_bucket = linkedin_query_region(region)
        region_coverage = health["region_coverage"].setdefault(
            region_bucket,
            {"planned": 0, "executed": 0, "returned": 0, "retained": 0},
        )
        region_coverage["planned"] += 1
        region_coverage["executed"] += 1
        try:
            page_index = (max(0, batch_index) + query_index) % max(1, search_page_cycle)
            retry_count = 0
            while True:
                try:
                    found = fetch_query_jobs(
                        session,
                        keyword,
                        region,
                        posted_hours,
                        limit_per_query,
                        start=page_index * 25,
                    )
                    break
                except requests.RequestException as retry_exc:
                    retry_status = int(
                        getattr(getattr(retry_exc, "response", None), "status_code", 0) or 0
                    )
                    if retry_status != 429 or retry_count >= max(0, rate_limit_retries):
                        raise
                    retry_count += 1
                    time.sleep(min(8.0, 2.0 ** retry_count) + random.uniform(0.0, 0.4))
            health["succeeded"] += 1
            collected.extend(found)
            region_coverage["returned"] += len(found)
            health["query_receipts"].append(
                {
                    "keywords": keyword,
                    "location": region,
                    "status": "SUCCESS" if found else "EMPTY_VALID",
                    "start": page_index * 25,
                    "result_count": len(found),
                    "retry_count": retry_count,
                }
            )
            time.sleep(random.uniform(0.9, 1.8))
        except requests.RequestException as exc:
            health["failed"] += 1
            print(f"LinkedIn fetch warning for '{keyword}' in '{region}': {exc}")
            status_code = int(
                getattr(getattr(exc, "response", None), "status_code", 0) or 0
            )
            if status_code == 429 or "429" in str(exc):
                health["rate_limited"] = True
                health["query_receipts"].append(
                    {
                        "keywords": keyword,
                        "location": region,
                        "status": "RATE_LIMITED",
                        "start": page_index * 25,
                        "result_count": 0,
                        "retry_count": locals().get("retry_count", 0),
                    }
                )
                health["unexecuted_queries"] = [
                    {
                        "keywords": str(item.get("keywords") or ""),
                        "location": str(item.get("location") or ""),
                        "reason": "RATE_LIMITED",
                    }
                    for item in queries[query_index + 1 :]
                ]
                break
            health["query_receipts"].append(
                {
                    "keywords": keyword,
                    "location": region,
                    "status": "FAILED",
                    "start": page_index * 25,
                    "result_count": 0,
                    "retry_count": locals().get("retry_count", 0),
                }
            )

    deduped = dedupe_jobs(collected)
    health["raw_discovered_count"] = len(collected)
    health["deduplicated_count"] = len(deduped)
    scoped_jobs: list[JobListing] = []
    for job in deduped:
        scope = linkedin_location_scope(job.location)
        job.location_scope = scope["status"]
        job.location_scope_reason = scope["reason"]
        if scope["status"] != "OUT_OF_SCOPE":
            scoped_jobs.append(job)
            if scope["status"] == "TARGET":
                for region_bucket in str(scope["region"] or "").split("+"):
                    if region_bucket in health["region_coverage"]:
                        health["region_coverage"][region_bucket]["retained"] += 1
        else:
            health["filtered_out_count"] += 1
            if len(health["filtered_out_examples"]) < 20:
                health["filtered_out_examples"].append(
                    {
                        "company": job.company,
                        "position": job.position,
                        "location": job.location,
                        "url": job.url,
                        "scope": scope["status"],
                        "reason": scope["reason"],
                    }
                )
    health["deferred_count"] = max(0, len(scoped_jobs) - enough_raw_jobs)
    if scoped_jobs:
        hydrate_job_details(session, scoped_jobs[:detail_limit_total])
        observed_at = datetime.now(timezone.utc).date().isoformat()
        for job in scoped_jobs:
            job.observation_origin = "LIVE_FETCH"
            job.observed_at = observed_at
        return scoped_jobs[:enough_raw_jobs]

    fallback = read_json(cache_fallback, []) if not deduped else []
    if fallback:
        print(f"Falling back to cached manual jobs from {cache_fallback}")
        jobs = []
        for item in fallback:
            job = JobListing(**item)
            scope = linkedin_location_scope(job.location)
            job.location_scope = scope["status"]
            job.location_scope_reason = scope["reason"]
            if scope["status"] != "OUT_OF_SCOPE":
                jobs.append(job)
            else:
                health["filtered_out_count"] += 1
        for job in jobs:
            job.observation_origin = "CACHE_REPLAY"
        health["fallback"] = True
        return jobs
    return []


def fetch_query_jobs(
    session: requests.Session,
    title: str,
    region: str,
    posted_hours: int,
    limit_per_query: int,
    *,
    start: int = 0,
) -> list[JobListing]:
    params = {
        "keywords": title,
        "location": region,
        "f_TPR": f"r{posted_hours * 3600}",
        "sortBy": "DD",
        "start": max(0, int(start)),
    }
    url = f"{SEARCH_ENDPOINT}?{urlencode(params)}"
    response = session.get(url, timeout=30)
    response.raise_for_status()
    listings = parse_search_cards(response.text, title)
    return listings[:limit_per_query]


def hydrate_job_details(session: requests.Session, jobs: list[JobListing]) -> None:
    for job in jobs:
        if not job.job_id:
            continue
        try:
            detail_response = session.get(DETAIL_ENDPOINT.format(job_id=job.job_id), timeout=30)
            if detail_response.ok:
                job.description = extract_description(detail_response.text)
            time.sleep(random.uniform(0.5, 1.1))
        except requests.RequestException:
            continue


def parse_search_cards(html_text: str, search_keyword: str) -> list[JobListing]:
    cards = re.findall(r"<li[^>]*>(.*?)</li>", html_text, flags=re.DOTALL | re.IGNORECASE)
    jobs: list[JobListing] = []
    for card in cards:
        link_match = re.search(
            r'href="(?P<url>https://(?:[a-z]{2,3}\.)?linkedin\.com/jobs/view/[^"?]*?(?P<id>\d+)[^"]*)"',
            card,
            flags=re.IGNORECASE,
        )
        title_match = re.search(
            r'class="base-search-card__title[^"]*"[^>]*>\s*(?P<title>.*?)\s*</h3>',
            card,
            flags=re.DOTALL | re.IGNORECASE,
        )
        company_match = re.search(
            r'class="base-search-card__subtitle[^"]*"[^>]*>\s*(?:<a[^>]*>)?(?P<company>.*?)(?:</a>)?\s*</h4>',
            card,
            flags=re.DOTALL | re.IGNORECASE,
        )
        location_match = re.search(
            r'class="job-search-card__location[^"]*"[^>]*>\s*(?P<location>.*?)\s*</span>',
            card,
            flags=re.DOTALL | re.IGNORECASE,
        )
        time_match = re.search(r"<time[^>]*datetime=\"(?P<date>[^\"]+)\"", card, flags=re.IGNORECASE)
        easy_apply = "Easy Apply" in card

        if not (link_match and title_match and company_match):
            continue

        url = html.unescape(link_match.group("url")).split("?")[0]
        title = clean_html_text(title_match.group("title"))
        company = clean_html_text(company_match.group("company"))
        location = clean_html_text(location_match.group("location")) if location_match else ""
        date = time_match.group("date") if time_match else datetime.now(timezone.utc).date().isoformat()

        jobs.append(
            JobListing(
                company=company,
                position=title,
                url=url,
                date=date,
                location=location,
                easy_apply=easy_apply,
                search_keyword=search_keyword,
                job_id=link_match.group("id"),
            )
        )

    return jobs


def extract_description(html_text: str) -> str:
    match = re.search(
        r'class="show-more-less-html__markup[^"]*"[^>]*>(?P<body>.*?)</div>',
        html_text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return ""
    text = re.sub(r"<[^>]+>", " ", match.group("body"))
    return normalize_whitespace(html.unescape(text))


def clean_html_text(raw_text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw_text)
    return normalize_whitespace(html.unescape(text))


def dedupe_jobs(jobs: list[JobListing]) -> list[JobListing]:
    deduped: list[JobListing] = []
    seen: set[str] = set()
    for job in jobs:
        key = job.url or f"{job.company}|{job.position}|{job.location}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(job)
    return deduped


def main() -> None:
    args = parse_args()
    if args.batch_index < 0:
        args.batch_index = deterministic_daily_batch_index()
    health: dict = {}
    jobs = fetch_jobs(
        args.config,
        args.limit_per_query,
        args.detail_limit_total,
        taxonomy_config=args.taxonomy_config,
        mode=args.mode,
        batch_index=args.batch_index,
        search_page_cycle=args.search_page_cycle,
        rate_limit_retries=args.rate_limit_retries,
        health=health,
    )
    default_name = "linkedin_jobs.json" if args.mode == "daily" else "linkedin_backfill_jobs.json"
    output_path = Path(args.output or f"data/job_cache/{default_name}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps([asdict(job) for job in jobs], indent=2), encoding="utf-8")
    if health.get("rate_limited"):
        status = "PARTIAL_RATE_LIMITED"
    elif health.get("fallback") or (health.get("failed") and health.get("succeeded")):
        status = "PARTIAL"
    elif health.get("failed") and not health.get("succeeded"):
        status = "FAILED"
    elif jobs:
        status = "SUCCESS"
    else:
        status = "EMPTY_VALID"
    record_source_health(
        "linkedin_supplemental", status, count=len(jobs),
        attempted=health.get("attempted", 0), succeeded=health.get("succeeded", 0),
        failed=health.get("failed", 0), details={
            "cache_fallback": health.get("fallback", False),
            "rate_limited": health.get("rate_limited", False),
            "batch_index": health.get("batch_index"),
            "planned": health.get("planned", 0),
            "query_receipts": health.get("query_receipts", []),
            "unexecuted_queries": health.get("unexecuted_queries", []),
            "raw_discovered_count": health.get("raw_discovered_count", 0),
            "deduplicated_count": health.get("deduplicated_count", 0),
            "deferred_count": health.get("deferred_count", 0),
            "region_coverage": health.get("region_coverage", {}),
            "filtered_out_count": health.get("filtered_out_count", 0),
            "filtered_out_examples": health.get("filtered_out_examples", []),
        },
        automatic=True,
    )
    print(f"Wrote {len(jobs)} LinkedIn {args.mode} jobs to {output_path}")


if __name__ == "__main__":
    main()
