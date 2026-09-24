# Cursor/Codex MANUAL_AGENT discovery instruction

Use public web research to find Shenzhen/深圳 2027 campus opportunities. Search by role family, not only by registry company name. Cover the core families (general software, backend, test/SDET, AI application, robot software) and rotate through expansion families (platform/SRE/DevOps, engineering tools/developer productivity, reliability/software automation, FinTech software, and data engineering).

Execute the dated query plan supplied at the end of the scheduled prompt. Do not
replace it with an invented sample. The plan has two independent axes: daily/
rolling monitoring for known companies, and company-agnostic open-market recall.
It covers public web, university-career, NowCoder, NCSS, Guopin,
official-domain discovery, known ATS domains, campus campaign terms, every P0
company daily, and the day's deterministic P1/expansion shards. Registry
membership is never required for returning a candidate.

For every result:

1. Open and verify at least one real public source URL. Never fabricate a URL.
2. Attempt canonical verification in this order: official company job page, official ATS job page, official campus page.
3. Preserve the discovery page even when a canonical page is found.
4. Do not infer unavailable deadline, salary, 2027 eligibility, or open/closed status. Use `null` or omit the field.
5. Do not include applicant contact details and do not emit `application_profile` or `recommended_application_profile`.
6. Do not write Feishu, apply, log in, bypass CAPTCHA/anti-bot controls, or call a paid model API.

Return exactly one JSON object with the following shape. Do not write it to the
repository yourself; the trusted receiver validates the final response and then
atomically promotes it to `data/discovery/gpt_manual/candidates.json`:

```json
{
  "schema_version": 3,
  "mode": "MANUAL_AGENT",
  "source": "gpt_web",
  "batch": {
    "batch_id": "shenzhen-2027-YYYY-MM-DD-01",
    "plan_id": "public-web-YYYY-MM-DD-fingerprint",
    "generated_at": "YYYY-MM-DDTHH:MM:SS+08:00",
    "search_date": "YYYY-MM-DD",
    "queries": ["深圳 2027 测试开发 校招"],
    "source_urls": ["https://public-source.example/real-result"],
    "searches": [
      {
        "plan_id": "public-web-YYYY-MM-DD-fingerprint",
        "query_id": "company_agnostic_core-hash",
        "lane": "company_agnostic_core",
        "query": "深圳 2027 测试开发 校招",
        "provider": "codex_web_search",
        "started_at": "YYYY-MM-DDTHH:MM:SS+08:00",
        "completed_at": "YYYY-MM-DDTHH:MM:SS+08:00",
        "status": "SUCCESS",
        "source_urls": ["https://public-source.example/real-result"],
        "result_count": 1,
        "error_category": "",
        "retry_count": 0,
        "notes": "Opened and checked the public result."
      }
    ],
    "max_age_days": 1,
    "expires_at": "YYYY-MM-DD"
  },
  "candidates": [
    {
      "company": "",
      "title": "",
      "location": "",
      "source_url": "",
      "source_type": "public_web",
      "source_name": "",
      "posted_at": null,
      "deadline": null,
      "status": null,
      "graduation_evidence": [],
      "role_evidence": [],
      "location_evidence": [],
      "discovery_query": "",
      "discovery_query_id": "",
      "discovered_at": "YYYY-MM-DD",
      "evidence_confidence": "Medium",
      "notes": "",
      "job_language": "UNKNOWN",
      "recruiting_context": "",
      "source_evidence": [
        "Public page excerpt supporting company, title, and location"
      ],
      "canonical_url": null
    }
  ]
}
```

Delete any candidate without a trustworthy source URL. `canonical_url` must be a separately verified official/ATS URL, not a guessed company career homepage.

The envelope metadata is mandatory. `expires_at` or `max_age_days` must define
freshness (both may be supplied). `queries` records the bounded role-driven and
company-driven search set; `searches` records whether every query was actually
executed and the raw public result pages it inspected (up to 30 URLs per query),
including pages later rejected as closed, wrong-cohort, wrong-location,
wrong-role, duplicate, or insufficiently evidenced. `source_urls` must equal the
union of those per-query URLs. Each candidate must include source evidence tied
to its real `source_url`.

A completed search is `EMPTY_VALID` only when the provider returned no result
URLs. When raw results exist but none survives candidate filtering, use
`SUCCESS`, preserve the inspected URLs and raw result count, emit no candidate
for that query, and summarize rejection reasons in `notes`. Use `RATE_LIMITED`,
`CAPTCHA`, `PARSE_ERROR`, or `TIMEOUT` when that cause is known; generic `FAILED`
means the search otherwise could not be executed. A missing/schema-invalid batch
is `FAILED`; a valid but
expired batch is `REFRESH_REQUIRED`. The importer never re-labels an old batch as
current and never treats an unexecuted search as a successful empty result.
