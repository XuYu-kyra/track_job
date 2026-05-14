#!/usr/bin/env python3
"""Fetch recent Indeed jobs with a conservative parser and local fallback."""

from __future__ import annotations

import argparse
import html
import json
import random
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

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
    source: str = "indeed"
    description: str = ""
    easy_apply: bool = False
    search_keyword: str = ""
    job_id: str = ""


SEARCH_ENDPOINT = "https://uk.indeed.com/jobs"
DETAIL_ENDPOINT = "https://uk.indeed.com/viewjob"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Indeed jobs for the daily pipeline.")
    parser.add_argument("--config", default="config/targets.yaml")
    parser.add_argument("--output", default="data/job_cache/indeed_jobs.json")
    parser.add_argument("--limit-per-query", type=int, default=12)
    parser.add_argument("--detail-limit-total", type=int, default=18)
    return parser.parse_args()


def fetch_jobs(config_path: str, limit_per_query: int, detail_limit_total: int) -> list[JobListing]:
    config = load_config(config_path)
    search_config = config.get("job_search", {})
    posted_hours = int(search_config.get("posted_within_hours", 24))
    titles = list(search_config.get("titles", []))
    regions = list(search_config.get("regions", [])) or ["United Kingdom"]
    cache_dir = Path(config.get("output", {}).get("cache_dir", "data/job_cache"))
    fallback_path = cache_dir / "manual_indeed_jobs.json"

    session = requests.Session()
    session.headers.update(HEADERS)
    collected: list[JobListing] = []
    blocked = False

    for region in regions:
        for title in titles:
            try:
                jobs = fetch_query_jobs(session, title, region, posted_hours, limit_per_query)
                collected.extend(jobs)
                time.sleep(random.uniform(1.0, 2.0))
            except requests.RequestException as exc:
                print(f"Indeed fetch warning for '{title}' in '{region}': {exc}")
                blocked = True
                continue
            except RuntimeError as exc:
                print(f"Indeed fetch warning for '{title}' in '{region}': {exc}")
                blocked = True
                continue

    deduped = dedupe_jobs(collected)
    if deduped:
        hydrate_job_details(session, deduped[:detail_limit_total])
        return deduped

    fallback = read_json(fallback_path, [])
    if fallback:
        print(f"Falling back to cached manual Indeed jobs from {fallback_path}")
        return [JobListing(**item) for item in fallback]

    if blocked:
        print("Indeed returned blocked or empty responses; continuing without Indeed jobs.")
    return []


def fetch_query_jobs(
    session: requests.Session,
    title: str,
    region: str,
    posted_hours: int,
    limit_per_query: int,
) -> list[JobListing]:
    params = {
        "q": title,
        "l": region,
        "fromage": max(1, posted_hours // 24),
        "sort": "date",
    }
    response = session.get(SEARCH_ENDPOINT, params=params, timeout=30)
    response.raise_for_status()

    html_text = response.text
    if is_blocked_page(html_text):
        raise RuntimeError("Indeed blocked the request with an anti-bot page")

    jobs = parse_search_results(html_text, title)
    return jobs[:limit_per_query]


def is_blocked_page(html_text: str) -> bool:
    lowered = html_text.lower()
    return "blocked - indeed.com" in lowered or "waf_block" in lowered or "cf-challenge" in lowered


def parse_search_results(html_text: str, search_keyword: str) -> list[JobListing]:
    jobs = parse_embedded_json_jobs(html_text, search_keyword)
    if jobs:
        return jobs
    return parse_html_cards(html_text, search_keyword)


def parse_embedded_json_jobs(html_text: str, search_keyword: str) -> list[JobListing]:
    jobs: list[JobListing] = []
    seen: set[str] = set()

    for match in re.finditer(r'"jobkey":"(?P<jobkey>[^"]+)"', html_text):
        start = max(0, match.start() - 1200)
        end = min(len(html_text), match.end() + 2400)
        snippet = html_text[start:end]

        job_id = match.group("jobkey")
        title = extract_json_value(snippet, "displayTitle") or extract_json_value(snippet, "title")
        company = extract_json_value(snippet, "company") or extract_json_value(snippet, "companyName")
        location = extract_json_value(snippet, "formattedLocation") or extract_json_value(snippet, "location")
        date = extract_json_value(snippet, "datePublished") or extract_json_value(snippet, "pubDate") or ""
        easy_apply = '"Indeed Apply"' in snippet or '"indeedApply"' in snippet or '"isIndeedApply":true' in snippet

        if not (job_id and title and company):
            continue

        url = f"{DETAIL_ENDPOINT}?jk={job_id}"
        key = f"{job_id}|{title}|{company}"
        if key in seen:
            continue
        seen.add(key)

        jobs.append(
            JobListing(
                company=clean_text(company),
                position=clean_text(title),
                url=url,
                date=clean_text(date) or "",
                location=clean_text(location),
                easy_apply=easy_apply,
                search_keyword=search_keyword,
                job_id=job_id,
            )
        )

    return jobs


def extract_json_value(snippet: str, key: str) -> str:
    pattern = rf'"{re.escape(key)}":"(?P<value>(?:\\.|[^"])*)"'
    match = re.search(pattern, snippet)
    if not match:
        return ""
    raw = match.group("value")
    return normalize_whitespace(bytes(raw, "utf-8").decode("unicode_escape"))


def parse_html_cards(html_text: str, search_keyword: str) -> list[JobListing]:
    jobs: list[JobListing] = []
    cards = re.findall(r"<article[^>]*>(.*?)</article>", html_text, flags=re.DOTALL | re.IGNORECASE)
    for card in cards:
        link_match = re.search(r'href="(?P<href>/[^"]*viewjob[^"]*)"', card, flags=re.IGNORECASE)
        title_match = re.search(r'title="(?P<title>[^"]+)"', card, flags=re.IGNORECASE)
        company_match = re.search(r'data-testid="company-name"[^>]*>\s*(?P<company>.*?)\s*</', card, flags=re.DOTALL)
        location_match = re.search(r'data-testid="text-location"[^>]*>\s*(?P<location>.*?)\s*</', card, flags=re.DOTALL)
        date_match = re.search(r'data-testid="myJobsStateDate"[^>]*>\s*(?P<date>.*?)\s*</', card, flags=re.DOTALL)

        if not (link_match and title_match and company_match):
            continue

        href = html.unescape(link_match.group("href"))
        url = urljoin("https://uk.indeed.com", href)
        job_id = parse_qs(urlparse(url).query).get("jk", [""])[0]
        jobs.append(
            JobListing(
                company=clean_text(company_match.group("company")),
                position=clean_text(title_match.group("title")),
                url=url,
                date=clean_text(date_match.group("date")) if date_match else "",
                location=clean_text(location_match.group("location")) if location_match else "",
                easy_apply="Indeed Apply" in card or "Easily apply" in card,
                search_keyword=search_keyword,
                job_id=job_id,
            )
        )
    return jobs


def hydrate_job_details(session: requests.Session, jobs: list[JobListing]) -> None:
    for job in jobs:
        if not job.job_id:
            continue
        try:
            response = session.get(DETAIL_ENDPOINT, params={"jk": job.job_id}, timeout=30)
            if response.ok and not is_blocked_page(response.text):
                job.description = extract_description(response.text)
            time.sleep(random.uniform(0.5, 1.1))
        except requests.RequestException:
            continue


def extract_description(html_text: str) -> str:
    for pattern in (
        r'<div[^>]+id="jobDescriptionText"[^>]*>(?P<body>.*?)</div>',
        r'"jobDescriptionText":"(?P<body>(?:\\.|[^"])*)"',
    ):
        match = re.search(pattern, html_text, flags=re.DOTALL | re.IGNORECASE)
        if not match:
            continue
        body = match.group("body")
        if "<" in body:
            body = re.sub(r"<[^>]+>", " ", body)
        else:
            body = bytes(body, "utf-8").decode("unicode_escape")
        return normalize_whitespace(html.unescape(body))
    return ""


def clean_text(raw_text: str) -> str:
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
