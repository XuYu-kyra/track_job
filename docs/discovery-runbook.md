# Discovery runbook

All commands below remain local until an explicit Feishu apply flag is used. Neither discovery path applies to jobs or automates closed-platform logins.

## 0. Audit company sources

Run the public, read-only registry audit before promoting or changing adapters:

```bash
python3 scripts/audit_company_sources.py
```

Review `data/job_cache/company_source_audit.json` and `.md`. An `AUTO_*` result is
a recommendation, not a configuration mutation. Promote a company to
`monitor_mode: AUTO` only after the URL, ATS family, pagination and full-JD
behavior have been reviewed. `BROKEN` must remain human-review-required;
companies without a verified URL must carry an explicit `PUBLIC_SEARCH_ONLY` or
`MANUAL_ONLY` reason in the private registry.

## 1. Official sources and bounded pagination

```bash
python3 scripts/fetch_official.py --max-links-per-company 200
```

Inspect `source_health.json` for every adapter's `pages_attempted`,
`pages_succeeded`, `pagination_complete`, `truncated` and reason. `PARTIAL` is
expected when the safety cap is reached; raising the cap is an explicit operator
decision, not an automatic retry. Filtering happens after listing pagination;
only potentially relevant records are detail-hydrated.

## 2. Deterministic two-axis public-web and LinkedIn lanes

Generate the trusted plan before MANUAL_AGENT execution:

```bash
python3 scripts/build_public_web_query_plan.py
python3 scripts/import_gpt_discovery.py --check \
  --query-plan data/job_cache/public_web_query_plan.json
```

The plan checks every P0 company daily and a deterministic P1 shard whose three
successive calendar days cover every P1 company. Independently, its open-market
axis searches every core role family daily without a company name and rotates
expansion roles. NowCoder, NCSS, Guopin, university career sites, general public
web and official-domain discovery are separate daily lanes; configured ATS
domains and campus campaign terms add further generated shards. The registry is
priority metadata, not an allowlist.

The accepted batch must reproduce every supplied query exactly and provide one
receipt per query. `EMPTY_VALID` is a valid completed search; `FAILED` is not.
For LinkedIn, omit the batch index or use `-1` so the Asia/Shanghai date selects
the daily batch. The collector stops on the first 429 and marks the source
`PARTIAL_RATE_LIMITED`.

## A. Deterministic initial backfill

Run bounded 60-day batches. This stops after local normalize/dedup/eligibility/scoring/queue and never generates documents or writes Feishu:

```bash
python3 scripts/run_daily_pipeline.py --mode backfill --batch-index 0
```

Increment `--batch-index` for the next role-balanced batch. Do not run many batches as an unattended production crawl.

## B. Cursor/Codex MANUAL_AGENT research

Ask Cursor/Codex to follow `docs/gpt-discovery-agent.md`. The agent searches public pages and writes:

```text
data/discovery/gpt_manual/candidates.json
```

No OpenAI API key or paid API is used. Validate first, then import:

```bash
python3 scripts/import_gpt_discovery.py --check
python3 scripts/import_gpt_discovery.py
```

Records without a real source URL are rejected. The output is `data/job_cache/gpt_manual_jobs.json`.
The JSON must use the schema-v2 batch envelope documented in
`docs/gpt-discovery-agent.md` (batch ID, generated/search date, exact queries,
per-query execution receipts, opened source URLs, source evidence, and
expiry/max-age). A completed search with no matching jobs is successful and is
reported as `EMPTY_VALID`. Invalid/missing schema or a batch in which every
search failed is `FAILED`; a structurally valid expired batch is
`REFRESH_REQUIRED`. All cases import zero jobs safely while the other source lanes
can continue.

On Windows, `scripts/run_windows_daily.py` can invoke an installed, already
authenticated Codex CLI in ephemeral read-only web-search mode, validate its
schema-constrained temporary response, atomically promote the fresh inbox, and
then run the ordinary daily pipeline. The task never stores an account token in
the repository and never enables Feishu apply mode.

## C. Manual BOSS/WeChat intake

Copy `data/job_cache/manual_job_inputs.example.txt` to the Git-ignored runtime file
`data/job_cache/manual_job_inputs.txt`, then add a URL and the visible JD using:

```text
URL | Company | Position | Location | Description | Deadline | Source | Dream role | Notes | Application profile
```

Use source `boss` or `wechat`; the optional final value is `CN` or `INTL`. Never automate login, CAPTCHA, browser sessions, or scraping.

## D. Canonical verification

For GPT/manual discoveries, try to find the exact official company, official ATS, or official campus job URL and keep it alongside the original `source_url`. During GPT/manual import it remains an unverified source candidate: known ATS families may set `ats_family`, but the URL shape, `canonical_url` label, or `source=official` label never grants canonical authority or High confidence. A trusted official monitor/importer or explicit verifier must successfully establish canonical authority. A missing official URL does not reject a valid secondary-source candidate. Registry seeds without a verified URL remain `PUBLIC_SEARCH`; change a company to `monitor_mode: AUTO` only after adding a verified, publicly accessible career URL suitable for the generic monitor.

## E. Downstream dry-run and inspection

To import all current local inboxes and run the daily pipeline without fetching, documents, or Feishu writes:

```bash
python3 scripts/run_daily_pipeline.py --mode daily --skip-fetch --skip-repo-sync --skip-documents
```

Inspect:

```text
data/job_cache/jobs.json
data/job_cache/decisioned_jobs.json
data/job_cache/scored_jobs.json
data/job_cache/discovered_company_candidates.json
```

The discovered-company store contains only companies absent from the registry.
It retains aliases, jobs, verified career/ATS URL, ATS family, first/last seen,
fresh-run history, target-role count, Shenzhen/2027 evidence, confidence and
promotion reasons. Meeting any promotion condition moves a company into dynamic
persistent monitoring; a later explicit registry addition is recorded as a
separate promotion event.

The normal daily pipeline also keeps Feishu in dry-run unless `--apply-feishu` is explicitly supplied. Never use that flag during discovery validation.

## F. Verification and coverage gate

After merge and scoring, build and inspect both formats:

```bash
python3 scripts/build_discovery_coverage.py
```

`DISCOVERED_CANDIDATE`, `CLOSED`, and `UNCERTAIN` jobs cannot become `READY` or
`MUST_APPLY`; they stay in `WATCH/HUMAN_REVIEW_REQUIRED` unless a deterministic
hard rejection applies. A real `update_feishu.py --apply` checks
`discovery_coverage_report.json` and refuses all writes unless its status is
`SOURCE_COVERAGE_HEALTHY`. `--dry-run` remains usable under degraded coverage.
The report exposes the acceptance keys `P0_DAILY_COVERAGE`,
`P1_ROLLING_3_DAY_COVERAGE`, `CORE_ROLE_DAILY_COVERAGE`,
`PUBLIC_SOURCE_LANE_DAILY_COVERAGE`, `REGISTRY_IS_ALLOWLIST`, and
`UNREGISTERED_COMPANIES_ALLOWED`, plus runtime checked/executed counts and any
overdue search shards.

Rollback is configuration-only: return a newly promoted company to
`PUBLIC_SEARCH`, restore the previous verified URL/ATS family, or disable the
problem adapter, then rerun audit, discovery and coverage. Do not delete history,
canonical IDs, prior coverage reports or secondary leads during rollback.
