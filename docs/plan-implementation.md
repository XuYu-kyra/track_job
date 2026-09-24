# Plan-to-Implementation Map

| Frozen-plan deliverable | Implementation |
|---|---|
| Canonical Job Schema | `scripts/job_schema.py` |
| Role taxonomy and aliases | `config/taxonomies.yaml` → `role_families` |
| Skill taxonomy and synonyms | `config/taxonomies.yaml` → `skill_taxonomy` |
| Candidate repository discovery/cache | `scripts/sync_candidate_repos.py` |
| Candidate skill/project inventory | `scripts/build_candidate_inventory.py`, including evidence level and repo/file provenance |
| Source adapter architecture | `config/company_registry.yaml`, `scripts/source_adapters.py`, official/supplemental collectors and manual inbox |
| Role/graduation/location query matrix | `scripts/query_matrix.py`, `scripts/public_web_query_plan.py` |
| Two-axis known-company/open-market discovery | `scripts/public_web_query_plan.py` |
| Dynamic unregistered-company monitoring | `scripts/discovered_companies.py`, `data/job_cache/discovered_company_candidates.json` |
| Discovery coverage and interval gate | `scripts/build_discovery_coverage.py` |
| Daily vs initial backfill | `scripts/run_daily_pipeline.py --mode daily/backfill` |
| Cross-source dedupe and freshness | `scripts/merge_job_sources.py`, local history |
| Eligibility → Opportunity → Lifestyle/Evidence → Timing → Queue | `scripts/score_jobs.py` |
| Dissertation HOLD and time-sensitive exception | `operating_mode` plus queue rules |
| Soft application capacity and Ready Gate | `application_capacity`, `ready_to_apply` |
| Resume families | Taxonomy mapping plus existing `generate_resume.py`; limited to `READY/MUST_APPLY` |
| Company/source registry | `config/company_registry.example.yaml` |
| Feishu fields and status dictionary | `config/feishu.example.yaml`, `setup_feishu.py`, `update_feishu.py` |
| Interview Question Bank | `scripts/update_question_bank.py` |
| Application analytics | `scripts/build_analytics.py` with sample gate |
| Apple execution layer | `scripts/build_execution_plan.py` and Shortcut JSON contract |
| Sample input/output | `examples/` |
| Unit tests and privacy gate | `tests/`, `Makefile`, `privacy_check.py` |

## Intentionally excluded

The implementation does not automate application submission, recruitment-platform login, recruiter email, or uncontrolled scraper expansion. These are explicit frozen-plan boundaries, not missing features.

Apple Reminders and Calendar are reached through a Shortcut import contract instead of direct iCloud automation. That keeps credentials out of the repository and the final task creation visible to the user.
