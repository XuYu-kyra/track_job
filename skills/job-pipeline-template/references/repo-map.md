# Repository Map

## Root

- `README.md`: public-facing overview and usage
- `.gitignore`: privacy boundary for local-only files
- `Makefile`: local validation commands

## Config

- `config/targets.example.yaml`: sample search, candidate-repo, semantic-review and output settings
- `config/company_registry.example.yaml`: sample official/source registry
- `config/execution.example.yaml`: sample execution controls
- `config/feishu.example.yaml`: sample Feishu config shape
- `config/profile.example.yaml`: sample identity/profile config shape
- `config/scoring.yaml`: tracked shortlist heuristics
- `config/taxonomies.yaml`: role, title and skill taxonomy

## Scripts

- `scripts/run_daily_pipeline.py`: orchestrated daily/backfill runner
- `scripts/fetch_official.py`, `fetch_linkedin.py`, `fetch_indeed.py`: automated discovery adapters
- `scripts/import_gpt_discovery.py`, `import_manual_jobs.py`: bounded human/agent inbox importers
- `scripts/source_adapters.py`, `ats_detection.py`, `query_matrix.py`: source metadata and query support
- `scripts/job_schema.py`: canonical job normalization contract
- `scripts/merge_job_sources.py`: merge and dedupe source caches
- `scripts/score_jobs.py`: scoring and shortlist selection
- `scripts/sync_candidate_repos.py`: bounded public-repository cache sync
- `scripts/build_candidate_inventory.py`: deterministic skill/repository inventory
- `scripts/build_candidate_evidence.py`: generated anonymized semantic evidence
- `scripts/semantic_review.py`: bounded semantic extraction and evidence ranking
- `scripts/generate_resume.py`: targeted LaTeX document generation
- `scripts/build_analytics.py`, `build_execution_plan.py`: local analytics and scheduling
- `scripts/setup_feishu.py`, `update_feishu.py`, `update_question_bank.py`: Feishu schema/sync tools
- `scripts/validate_config.py`, `privacy_check.py`: validation and privacy gates

## CV templates and materials

- `cv/resume.tex`: generic resume template with placeholder identity fields
- `cv/coverletter.tex`: generic cover-letter template with placeholder identity fields
- `cv/materials/`: six-file human-maintained source for profile, education, experience, projects, skills and evidence

## Data

- `data/job_cache/manual_job_inputs.example.txt`: tracked manual input sample file
- `data/job_cache/`: ignored runtime jobs, inventory, semantic evidence/reviews and analytics
- `data/candidate_repos/`: ignored read-only public-repository cache and manifest
- `data/discovery/gpt_manual/`: ignored semantic-discovery inbox

## Documentation and tests

- `docs/architecture.md`, `docs/operations.md`: active architecture and operating guide
- `docs/discovery-runbook.md`, `docs/feishu-setup.md`, `docs/gpt-discovery-agent.md`: focused runbooks
- `examples/`: public fixtures and input/output contract examples
- `tests/`: regression suite

## Skill

- `skills/job-pipeline-template/SKILL.md`: operational guidance for Codex
- `skills/job-pipeline-template/agents/openai.yaml`: UI metadata
