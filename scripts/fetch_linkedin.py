#!/usr/bin/env python3
"""Fetch recent LinkedIn jobs from public guest pages.

This uses LinkedIn's public jobs guest endpoints as a first-pass collection
method. It is intentionally conservative:
- fetch a small number of title/location combinations
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
from datetime import UTC, datetime
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlencode

import requests

try:
    from common import load_config, normalize_whitespace, read_json
except ModuleNotFoundError:
    from scripts.common import load_config, normalize_whitespace, read_json


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch LinkedIn jobs for the daily pipeline.")
    parser.add_argument("--config", default="config/targets.yaml")
    parser.add_argument("--output", default="data/job_cache/linkedin_jobs.json")
    parser.add_argument("--limit-per-query", type=int, default=25)
    parser.add_argument("--detail-limit-total", type=int, default=24)
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


def fetch_jobs(config_path: str, limit_per_query: int, detail_limit_total: int) -> list[JobListing]:
    config = load_config(config_path)
    search_config = config.get("job_search", {})
    posted_hours = int(search_config.get("posted_within_hours", 24))
    titles = list(search_config.get("titles", []))
    regions = list(search_config.get("regions", [])) or ["United Kingdom"]
    cache_fallback = Path(config.get("output", {}).get("cache_dir", "data/job_cache")) / "manual_linkedin_jobs.json"

    collected: list[JobListing] = []
    session = requests.Session()
    session.headers.update(HEADERS)
    enough_raw_jobs = max(limit_per_query * 3, 24)

    for region in regions:
        for title in titles:
            try:
                collected.extend(fetch_query_jobs(session, title, region, posted_hours, limit_per_query))
                time.sleep(random.uniform(0.9, 1.8))
            except requests.RequestException as exc:
                print(f"LinkedIn fetch warning for '{title}' in '{region}': {exc}")
                if "429" in str(exc):
                    time.sleep(random.uniform(2.0, 3.5))
                    continue

    deduped = dedupe_jobs(collected)
    if deduped:
        hydrate_job_details(session, deduped[:detail_limit_total])
        return deduped[:enough_raw_jobs]

    fallback = read_json(cache_fallback, [])
    if fallback:
        print(f"Falling back to cached manual jobs from {cache_fallback}")
        return [JobListing(**item) for item in fallback]
    return []


def fetch_query_jobs(
    session: requests.Session,
    title: str,
    region: str,
    posted_hours: int,
    limit_per_query: int,
) -> list[JobListing]:
    params = {
        "keywords": title,
        "location": region,
        "f_TPR": f"r{posted_hours * 3600}",
        "sortBy": "DD",
        "start": 0,
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
        date = time_match.group("date") if time_match else datetime.now(UTC).date().isoformat()

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
    jobs = fetch_jobs(args.config, args.limit_per_query, args.detail_limit_total)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps([asdict(job) for job in jobs], indent=2), encoding="utf-8")
    print(f"Wrote {len(jobs)} jobs to {output_path}")


if __name__ == "__main__":
    main()
