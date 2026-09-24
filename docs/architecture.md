# Architecture

## Runtime flow

```text
deterministic monitoring + GPT/Codex MANUAL_AGENT + human discovery
  -> candidate job inbox
  -> company source audit + dated public-web query plan
  -> official_adapters.py (bounded complete pagination + JD hydration)
  -> official/ATS canonical verification
  -> source_adapters.py
  -> job_schema.py
  -> merge_job_sources.py + job_history.json
  -> build_candidate_inventory.py
  -> score_jobs.py
       Eligibility
       Opportunity/Lifestyle/Evidence
       Timing/Capacity/Ready Gate
       Queue action
  -> decisioned_jobs.json + scored_jobs.json
  -> discovery coverage report + Feishu apply gate
  -> generate_resume.py (READY/MUST_APPLY only)
       canonical role_family
       -> Golden Base Registry / Selector
       -> evidence-addressable base manifest
       -> bounded tailoring (>=70% preserved, <=30% changed)
       -> base-specific renderer / benchmark / page-fit gates
  -> update_feishu.py (dry-run unless --apply)
  -> build_analytics.py
  -> build_execution_plan.py

GitHub profile
  -> sync_candidate_repos.py (bounded discovery + shallow local cache)
  -> human verification against source, tests, configs and stored artifacts
  -> cv/materials/ (only human-maintained candidate-fact source of truth)
       -> build_candidate_inventory.py -> candidate_inventory.json
       -> build_candidate_evidence.py -> semantic_candidate_evidence.json
       -> semantic_review.py (read-only advisory review)

interview_debrief.json
  -> update_question_bank.py
  -> question_bank.json
  -> build_execution_plan.py
```

## Canonical Job Schema

The internal record is intentionally flat so JSON caches, tests, Feishu fields, and manual corrections share one contract.

| Group | Fields |
|---|---|
| Identity | `company`, `company_type`, `title`, `role_family`, `location`, `source(s)`, `official_url`, `job_id`, `job_id_namespace`, `canonical_key` |
| Source | `source_urls`, `source_observations`, `discovery_priority`, `automation_mode`, `canonicality`, `source_family` |
| Provenance | `discovered_by`, `discovery_sources`, `canonical_source`, `canonical_url`, `ats_family`, `registry_member`, `registry_candidate` |
| Eligibility context | `page_context`, `campaign_context`, `source_context`, `graduation_evidence`, `graduation_eligibility`, `security_review` |
| Freshness and verification | `first_seen`, `last_observed`, `last_verified`, `observation_origin`, `observed_at`, `verification_level`, `verification_state`, `verified_at`, `posted_at`, `deadline`, `status`, `freshness` |
| Opportunity | `technical_fit`, `career_value`, `skill_portability`, `strategic_value`, `opportunity_value` |
| Lifestyle | `compensation_signal`, `wlb_signal`, `leave_usability`, `mobility_autonomy`, `commute`, `on_call`, `actual_work_risks` |
| Evidence | `evidence_confidence`, `evidence_count`, `last_lifestyle_check`, `notes` |
| Timing | `urgency`, `scarcity`, `process_trigger_risk`, `application_cost`, `regret`, `release_priority` |
| Queue | `action_tier`, `action`, `reject_reason` |
| Application | `recommended_application_profile`, `application_profile`, `resume_family`, `resume_version`, `applied_at`, `stage`, `next_action`, `next_deadline` |
| Interview | `round`, `team`, `manager_signal`, `questions`, `weak_topics`, `debrief` |
| Exit | `offer`, `reject_reason` |

`position` and `url` remain compatibility aliases for the original resume generator and old caches.

## Dedupe and freshness

Identity follows a strict hierarchy: verified canonical URL first, stable requisition/job ID within its system-owned source/ATS namespace (and company where needed) second, and normalized company-alias + title + location + explicit-cohort fallback last. `linkedin:42` and `nowcoder:42` are distinct. An exact verified canonical URL match overrides conflicting secondary-source IDs and can attach earlier secondary observations of that exact URL. Fallback identity is used only when neither side has canonical proof nor a trustworthy stable ID. Distinct stable IDs do not collapse through a generic fallback, and explicit graduation cohorts remain part of fallback identity. Duplicate observations retain all URLs and provenance, make a trusted canonical official/ATS observation primary when one exists, and keep the richer description.

`first_seen` is the first ingestion date. Every observation has an explicit origin: `LIVE_FETCH`, `CACHE_REPLAY`, `MANUAL_IMPORT`, or `OFFICIAL_VERIFICATION` (legacy `LIVE_EXTERNAL` is normalized to `LIVE_FETCH`). `last_observed` advances only when a live external fetch actually returns the job. `last_verified` advances only through a trusted canonical official/ATS verifier. A first GPT/manual/cache ingestion sets `first_seen` but does not fabricate either later timestamp; replaying it advances neither timestamp merely because the pipeline reran.

Unseen records remain in history. Freshness uses `last_verified` when available and otherwise falls back to `last_observed`; after the configured 14 days the record becomes stale. Discovery freshness never owns an active application action: `APPLIED` maps to `Action=APPLIED`; OA/AI/technical/general/HR interview stages and the legacy `PROCESS` stage map to `Action=PROCESS`; `OFFER` maps to `Action=OFFER`. Rejected/withdrawn lifecycle stages map to `REJECT` rather than re-entering the discovery queue.

## Decision boundaries

Eligibility hard filters run before opportunity ranking. Scores remain explainable and are saved as components. Lifestyle unknowns are not treated as facts; evidence confidence stays explicit.

Graduation evidence may come from title, JD, page/campaign context, or registered campus-source metadata. Missing evidence is `WATCH`, an explicit incompatible cohort/social-recruitment signal is rejected, and vague security language is reviewed rather than hard-rejected. Only explicit passport/private-travel restrictions are hard security rejects.

Daily discovery and the one-time backfill use the same normalize/dedup/eligibility/score/queue components. Daily uses a 48–72 hour window. Backfill uses bounded 45–60 day batches and stops before document generation or Feishu sync.

The company registry is priority and monitoring metadata, never a whitelist,
discovery boundary, score bonus, or resume-eligibility condition. Discovery has
two axes: precise known-company monitoring and company-agnostic open-market
recall. All P0 companies are planned daily; P1 companies follow a deterministic
three-calendar-day cycle. Core role families are queried daily without a company
name, while expansion families rotate. NowCoder, NCSS, Guopin, university career
sites, general public web, and official-domain discovery remain separate lanes.
ATS-domain and campus-campaign matrices are generated from configuration rather
than a fixed example list.

Jobs from unknown companies continue through the same normalization, dedupe,
scoring and resume gates. `discovered_company_candidates.json` retains their
aliases, jobs, verified official URL, ATS family, observation history, target-job
count, Shenzhen/2027 evidence, confidence and promotion state. A verified
official URL, multiple fresh runs, multiple target jobs, or a READY/MUST_APPLY
opportunity promotes the company to dynamic persistent monitoring without
silently mutating the registry. ATS URL-family detection only sets `ats_family`;
URL shape alone never grants `canonicality=CANONICAL`, official provenance, or
High confidence. Canonical authority requires a trusted official monitor/importer
or explicit verifier.

GPT discovery defaults to `MANUAL_AGENT`: Cursor/Codex public-web research writes local JSON, then the normal pipeline validates, normalizes, deduplicates and scores it. `API` is only a disabled enum placeholder and has no implementation or API key.

Discovery and verification are separate states. A secondary observation begins as
`DISCOVERED_CANDIDATE`; a trusted current official observation with open status,
complete-enough JD, supported graduation cohort and job-level Shenzhen eligibility
may become `VERIFIED_OPEN_JOB`. Explicit closure becomes `CLOSED`; conflicting,
stale or incomplete evidence becomes `UNCERTAIN`. Only `VERIFIED_OPEN_JOB` can
proceed to `READY` or `MUST_APPLY`. Useful secondary leads remain visible in
`WATCH` instead of being discarded.

The company audit is advisory. It classifies all registry entries as `AUTO_API`,
`AUTO_STRUCTURED`, `BROWSER_PUBLIC`, `PUBLIC_SEARCH_ONLY`, `MANUAL_ONLY`,
`BROKEN`, or `UNKNOWN`, but never silently promotes `monitor_mode`. Official
adapters paginate before filtering, preserve source job IDs, hydrate the full JD
only for potentially retained records, and report page counts/completeness. The
dated public-web plan covers all core role families and all 39 P0 companies every
day, partitions P1 companies across a deterministic three-day cycle, and rotates
configured expansion roles.

`build_discovery_coverage.py` combines registry, audit, source health, per-query
receipts, decisioned jobs, prior cycle history and the dynamic-company store. It
reports P0 daily and P1 rolling coverage, company-agnostic execution, core and
expansion roles, public-source lanes, new/promoted companies and overdue shards.
Missing lanes, partial pagination, source failures, unknown status/JD gaps or
abnormal count drops degrade the result. A real Feishu write requires
`SOURCE_COVERAGE_HEALTHY`; local dry-run remains available for diagnosis.

Queue decisions are separate from opportunity value:

- `MUST_APPLY`: high value or a high-value urgency/regret exception.
- `HOLD`: valuable, but protected by dissertation phase, Ready Gate, or current process load.
- `READY`: allowed for manual application now.
- `WATCH`: insufficient evidence, opportunistic, or stale.
- `REJECT`: standardized hard reason or very low career value.
- `APPLIED`: submitted but not yet in an active interview process.
- `PROCESS`: OA or an active interview/HR process.
- `OFFER`: offer-stage lifecycle state.

No action means auto-application. The user remains the final application boundary.

## Golden Base resume layer

`config/resume_bases.yaml` is the human-readable registry for four logical role
bases and 12 approved variants. The original ZIP SHA-256 is part of the contract.
`scripts/import_resume_bases.py` performs a zip-slip-safe, idempotent import and
preserves the source `main.tex`; a separate normalized `template.tex` replaces
uncontrolled identity text and complete content blocks with explicit header and
section markers. Chinese rendering therefore does not depend on an English
section title.

`config/resume_base_content.yaml` maps each logical base to evidence-addressable
education, experience, project, skill and bullet-slot material. Golden Base text
is not automatically trusted: automatic output is still rendered exclusively
from `cv/materials/` claims carrying valid evidence IDs. Source wording that has
no automatic material slot is reported for manual review and excluded.

Selection is based on canonical `role_family`. Test development never collapses
to SE; backend and general software use SE; reliability uses test only when the
JD is primarily validation/testing. `algorithm_research` remains excluded.
CN/EN variants share one fact-intention plan, while each selected base supplies
its own layout, language, photo policy and measured density contract.

Tailoring reports list unchanged/reordered/replaced slots, wording-variant
changes, selected evidence IDs, change percentage and semantic-cache identity.
Budget overflow returns `HUMAN_REVIEW_REQUIRED`. Page fitting retains the best
valid one-page candidate and cannot relax clipping, overlap, image, section,
fact or evidence gates.

## Private/runtime files

All mutable state is ignored by Git:

- `config/targets.yaml`, `config/feishu.yaml`, `config/profile.yaml`
- `config/execution.yaml`, `config/company_registry.yaml`
- `data/job_cache/*.json`, logs, normalized inbox files
- `data/candidate_repos/`
- `cv/generated/`

The privacy gate scans the working tree, current Git index, and all reachable Git history. Git inspection failure is inconclusive/failing, never passing. A historical manual inbox containing only public job URLs is reported as a warning; credentials or PII remain a failure.
