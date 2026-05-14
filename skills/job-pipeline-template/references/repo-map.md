# Repository Map

## Root

- `README.md`: public-facing overview and usage
- `.gitignore`: privacy boundary for local-only files
- `Makefile`: local validation commands
- `.github/workflows/ci.yml`: lightweight CI

## Config

- `config/targets.example.yaml`: sample search targets and output paths
- `config/feishu.example.yaml`: sample Feishu config shape
- `config/profile.example.yaml`: sample identity/profile config shape
- `config/scoring.yaml`: tracked shortlist heuristics

## Scripts

- `scripts/fetch_linkedin.py`: LinkedIn guest-page collector with fallback behavior
- `scripts/fetch_indeed.py`: Indeed collector with anti-block handling
- `scripts/import_manual_jobs.py`: manual link normalization and JSON export
- `scripts/merge_job_sources.py`: merge and dedupe source caches
- `scripts/score_jobs.py`: scoring and shortlist selection
- `scripts/generate_resume.py`: targeted LaTeX document generation
- `scripts/update_feishu.py`: Bitable synchronization
- `scripts/send_alert.py`: failure alert record creation
- `scripts/run_daily_pipeline.py`: orchestrated end-to-end runner
- `scripts/install_schedule.py`: local cron installer

## CV templates and materials

- `cv/resume.tex`: generic resume template with placeholder identity fields
- `cv/coverletter.tex`: generic cover-letter template with placeholder identity fields
- `cv/materials/`: publish-safe sample experience, projects, skills, and snippets

## Data

- `data/job_cache/manual_job_inputs.txt`: tracked manual input sample file
- `data/job_cache/*.json`: local-only runtime artifacts once the project is in use

## Skill

- `skills/job-pipeline-template/SKILL.md`: operational guidance for Codex
- `skills/job-pipeline-template/agents/openai.yaml`: UI metadata
