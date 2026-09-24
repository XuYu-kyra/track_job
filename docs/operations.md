# Operations Runbook

## Ready Gate / Wave-0 buildup

The dissertation is DONE. If `operating_mode.phase: PRE_APPLICATION_HOLD` is retained, it now means a temporary Ready Gate and Wave-0 preparation period. The daily program may collect and score jobs, while ordinary opportunities stay in `HOLD` until application materials, interview preparation, and near-term availability are honestly ready. Real `MUST_APPLY` exceptions remain eligible for immediate human review.

Recommended command:

```bash
python3 scripts/run_daily_pipeline.py --skip-documents
```

The generator itself also filters to `READY/MUST_APPLY`, so leaving documents enabled remains safe. In normal daily mode, application documents are compiled to PDF; use `--skip-documents` to disable both document generation and compilation.

PDF compilation uses `latexmk` with XeLaTeX when available, then `TECTONIC_BIN`, a `tectonic` executable on `PATH`, or the Git-ignored project-local `.tools/tectonic` fallback. Page-fit measurement uses PyMuPDF, with existing Poppler tools retained as a compatibility fallback.

### Golden Base maintenance

Verify and import all 12 immutable approved ZIP sources before the first
base-backed run:

```bash
python3 scripts/import_resume_bases.py
python3 scripts/benchmark_resume_bases.py
```

The benchmark command compiles every original `main.tex` and stores page size,
text-span, line/content density, clipping/overlap and required-image results.
Do not hand-edit imported `main.tex` or reuse a version ID after changing a ZIP;
the importer fails closed in either case.

Normal generation defaults to CN/EN no-photo according to the existing language
policy. Request a Chinese photo base explicitly:

```bash
python3 scripts/generate_resume.py --resume-variant photo --compile-pdf
```

There is no approved English photo base. EN + photo is a validation error unless
the operator explicitly adds `--allow-english-photo-fallback`, which is recorded
as a fallback to EN no-photo in artifact metadata. To roll back, select/re-enable
the previous registered version; do not remove the newer or older source ZIP.

Inspect `tailoring_report` in `cv/generated/generated_manifest.json`. Any change
above 30%, preservation below 70%, missing image, factual/evidence failure,
clipping, overlap or second page blocks attachment upload. Source wording listed
under `source_wording_review_slots` was deliberately not rendered automatically.

## Source registry and first bootstrap

Add only verified company career URLs to `config/company_registry.yaml`. The
registry controls known-company monitoring priority and is never a discovery
allowlist or eligibility requirement. Unregistered companies found through the
open-market lanes remain valid and are retained in
`data/job_cache/discovered_company_candidates.json`. Copy
`data/job_cache/manual_job_inputs.example.txt` to the Git-ignored
`data/job_cache/manual_job_inputs.txt`; sources marked `manual` or
`human_in_the_loop` must enter through that runtime file. Do not automate login
or captcha-protected platforms.

Before the daily schedule is enabled, run bounded backfill batches:

```bash
python3 scripts/run_daily_pipeline.py --mode backfill --batch-index 0
```

Backfill stops after the local queue files and never invokes CV generation or Feishu. Review `data/job_cache/backfill_scored_jobs.json`; use additional batch indexes only when needed. Daily mode remains:

```bash
python3 scripts/run_daily_pipeline.py --mode daily --skip-documents
```

To validate orchestration without network access, combine `--skip-fetch --skip-repo-sync`.

### Discovery health and write gate

Use the explicit read-only maintenance commands when investigating source drift:

```bash
make source-audit
make public-web-query-plan
make discovery-coverage
```

The source audit does not promote registry entries. Review its ATS evidence and
pagination/full-JD capability before changing `monitor_mode`. The per-company
official limit is `job_search.official_safety_cap` (default 200); reaching it is
a degraded partial run until an operator deliberately raises the bound or narrows
the upstream source safely.

Before any real Feishu sync, read `discovery_coverage_report.md`. A degraded or
failed report blocks the normal `--apply` gate before authentication or writes.
When the day's query receipts are all terminal and there are no unsafe
READY/MUST_APPLY jobs, an explicitly authorized
`--apply --allow-degraded-review-sync` may synchronize the review pool while
keeping READY/MUST_APPLY attachment gates individual. WATCH/REJECT records
never receive generated resume attachments. `--dry-run` continues to produce a
local preview. To roll back a source onboarding, restore
the preceding registry URL/ATS/mode, retain the old history and job IDs, rerun the
audit and discovery, and confirm a new coverage report. Never compensate for a
broken source by promoting cached or unverified secondary data to canonical.
Also verify P0 daily, P1 rolling-three-day, core-role daily, public-source lane,
company-agnostic query, dynamic-company promotion, and overdue-shard metrics.

## Candidate repositories

`candidate_profile.github_profile_url` is remote identity; `repo_paths` remains local-only. `sync_candidate_repos.py` discovers a bounded selection, rejects obvious archive/fork/static portfolio candidates, and shallow-syncs its own cache. Review its manifest before treating projects as CV evidence.

The one human-maintained source of truth for candidate facts is `cv/materials/`; the GitHub cache, manifest, inventory and semantic-review JSON are generated evidence inputs, not parallel authoring locations.

After reviewing newly synced repositories, update `cv/materials/evidence.yaml` and the related project, experience and skill records, then rebuild both runtime views:

```bash
python3 scripts/build_candidate_inventory.py
python3 scripts/build_candidate_evidence.py
```

Evidence uses four source classes: `VERIFIED` for inspected code, tests, artifacts or formal documents; `USER_ATTESTED` for explicit candidate statements without stronger artifacts; `DERIVED_RESUME_SAFE` for conservative wording traceable to the first two classes; and `UNCERTAIN` for material that must not be rendered or sent for ranking.

The sync cache is ignored by Git but is not automatically security-safe. Before reusing or publishing a synced repository, run a separate read-only secret scan over `data/candidate_repos/` and fix or rotate findings in the source repository as appropriate; do not copy discovered values into reports or materials.

## Active application phase

Change the phase to `ACTIVE_APPLICATION`, update the six Ready Gate booleans honestly, and set current WIP/load before each material scheduling change:

```yaml
application_capacity:
  active_technical_processes: 3
  process_load_next_72h: 5

ready_to_apply:
  base_cv: true
  intro_1m: true
  two_core_projects: true
  algorithms_refreshed: true
  cs_fundamentals_refreshed: true
  next_72h_available: true
```

Wave 0/1/2 remains a human release strategy. The program supplies `READY` and `MUST_APPLY`; it does not click Apply.

## Daily Check-in and load changes

Feed the Shortcut output back to `build_execution_plan.py --check-in FILE`.

- `>=80%`: keep load.
- `50–79%`: remove optional work and shorten blocks.
- `<50%`: keep core items and reduce planned load about 30%.
- Three consecutive low days: `weekly_replan_required` becomes true.

Deadline-critical application tasks remain even when learning load is reduced.

## Interview debrief

Within minutes of an interview, record the questions, answer result, team, round, and manager signal. Import the JSON:

```bash
python3 scripts/update_question_bank.py --input PATH_TO_DEBRIEF.json
```

The next execution plan adds the highest-frequency weak topic when the system is in active application mode.

## Analytics

```bash
python3 scripts/build_analytics.py
```

Role/source/resume conversion is calculated immediately but explicitly disabled for decision-making until the applied sample reaches the configured minimum (default 10). Reject reason counts do not require that threshold.

## Scheduler

After configs and dry-run verification:

```bash
python3 scripts/install_schedule.py
```

The native Windows task is installed only after a real full apply and table
verification. Its command explicitly uses
`--apply-feishu --allow-degraded-review-sync`: review-pool records may sync on a
degraded source day, while READY/MUST_APPLY rows and attachments remain
individually gated. It never clicks Apply/Submit on external job sites.

The native Windows daily wrapper reuses an already accepted MANUAL_AGENT batch only when retrying later on the same Shanghai calendar day; the next day's run performs fresh discovery. It passes `--skip-repo-sync` because GitHub candidate-repository discovery is portfolio maintenance, not a daily job-search dependency. Candidate inventory and semantic evidence are still rebuilt from the reviewed `cv/materials/` truth on every daily run. Run `sync_candidate_repos.py` separately after intentionally changing the repository selection.

Generated resume and cover-letter PDFs are uploaded as Bitable attachments only during a run explicitly launched with `--apply-feishu`. Dry-run mode never authenticates, uploads attachments, or writes records.

`setup_feishu.py --apply` is an initialization/administrator command only. After initialization the 55-field schema is exact and frozen: the live field-name set must equal the 55 configured names, every type must match, and every single/multi-select option set must match exactly (order is ignored). Normal production sync performs this blocking preflight and never creates, renames, retypes, or adds options to fields. An extra field or rogue option is incompatible and blocks writes.

## Privacy gate

Run `python3 scripts/privacy_check.py` before any publication or real-data dry-run. It checks the working tree, current tracked index, and reachable Git history using a repository-scoped `safe.directory` invocation. A Git failure or missing executable is inconclusive and returns non-zero. Historical `data/job_cache/manual_job_inputs.txt` presence is reported explicitly; public job URLs alone are informational, while credentials or PII fail the gate. Do not rewrite history solely to remove non-sensitive public URLs.

## Bounded semantic review

`semantic_review` is a non-canonical review layer after deterministic scoring. It automatically selects only `READY` and `MUST_APPLY`; a `WATCH` or `HOLD` job is included only when its canonical key is passed with `--semantic-review-job-key`. `max_reviews_per_run` remains the hard per-run cost cap.

Use the no-call path while checking orchestration:

```bash
python3 scripts/run_daily_pipeline.py --skip-fetch --skip-documents --semantic-review-dry-run
```

A future one-job provider smoke test requires the user to set `DEEPSEEK_API_KEY` in the process environment and explicitly request the call. The key must never be placed in YAML, JSON, logs, Feishu, or a job record. Candidate evidence is generated from `cv/materials/evidence.yaml` by `scripts/build_candidate_evidence.py` and must follow the anonymized ID contract illustrated by `examples/semantic_candidate_evidence.sample.json`; identity and contact fields are rejected at the final HTTP boundary.

Valid results are cached under `data/job_cache/semantic_reviews/`. Conflicts never overwrite deterministic fields: the cache record lists `conflicts` and sets `requires_human_review`. The structured `result` is the future evidence-selection interface shared by CN and EN rendering; this stage does not use it to generate final resumes.

## Failure handling

Dry-run pipeline failures remain local and do not create Feishu alerts. A run explicitly launched with `--apply-feishu` may create a failure alert record because that run already has remote-write authority.
