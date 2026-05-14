---
name: job-pipeline-template
description: Operate, adapt, and sanitize this repository as a publish-safe job-search pipeline template. Use when Codex needs to understand the template layout, set up local-only config files, modify scoring or job-source scripts, generate safe sample materials, or prepare a private job-pipeline project for public release without leaking identity data, secrets, or generated application artifacts.
---

# Job Pipeline Template

## Overview

Use this skill to work inside this repository while preserving the privacy boundary between tracked template files and untracked local files.

Prefer reusing the existing scripts and config shape over inventing a parallel workflow.

## Workflow

1. Read the root files first:
   `README.md`, `.gitignore`, `Makefile`, `config/*.example.yaml`.
2. Determine whether the task is about adapting the pipeline, changing scoring, changing document generation, or preparing a public release.
3. Keep private runtime files local-only:
   `config/targets.yaml`, `config/feishu.yaml`, `config/profile.yaml`, `data/job_cache/*.json`, `cv/generated/`.
4. Patch the existing scripts in `scripts/` instead of adding duplicate implementations.
5. Before proposing publication, run the validation checks described below.

## Privacy Boundary

Never add or commit:

- real names, phone numbers, email addresses, LinkedIn URLs, or portfolio URLs
- live Feishu credentials, app tokens, table IDs, or view IDs
- fetched job caches
- generated resumes or cover letters
- scheduler logs or machine-specific output

If the task is about public release, inspect `.gitignore`, the example configs, and the generated-document paths first.

## Repository Map

Read `references/repo-map.md` when you need a quick orientation to where each responsibility lives.

High-value paths:

- `scripts/fetch_linkedin.py`, `scripts/fetch_indeed.py`: source collection
- `scripts/import_manual_jobs.py`: normalize hand-collected links
- `scripts/merge_job_sources.py`: dedupe raw job sources
- `scripts/score_jobs.py`: scoring and shortlist logic
- `scripts/generate_resume.py`: LaTeX resume and cover-letter generation
- `scripts/update_feishu.py`, `scripts/send_alert.py`: Feishu sync and alerting
- `config/scoring.yaml`: weighting and penalty rules
- `config/*.example.yaml`: publish-safe config templates
- `cv/materials/`: placeholder resume and cover-letter content

## Common Tasks

### Adapt the template for a new user

- Keep tracked files generic.
- Put user-specific values in local-only config files copied from `config/*.example.yaml`.
- Route identity fields through `config/profile.yaml` instead of hardcoding them.

### Tune scoring behavior

- Update `config/scoring.yaml` first when the change is purely heuristic.
- Patch `scripts/score_jobs.py` only when the scoring model needs new logic.
- Preserve explainability by keeping the score breakdown readable.

### Change document generation

- Reuse `scripts/generate_resume.py`.
- Preserve the placeholder behavior when `config/profile.yaml` is missing.
- Keep tracked LaTeX templates generic and safe to publish.

### Prepare a public release of a private fork

- Replace tracked secrets with example placeholders.
- Replace tracked personal materials with reusable samples.
- Ensure generated outputs and caches are ignored.
- Run the validation checks below before publishing.

## Validation

Run:

```bash
make check
```

If the task is specifically about safe publication, also run:

```bash
make privacy-check
```

When the skill itself changes, validate it with:

```bash
python3 /home/student24/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/job-pipeline-template
```

## References

- Read `references/repo-map.md` for file-level orientation.
