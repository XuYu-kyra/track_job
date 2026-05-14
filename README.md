# Find Job Public Template

A sanitized, open-source-friendly template for a daily job-search pipeline:

- fetch jobs from LinkedIn, Indeed, or manual inputs
- merge and score opportunities against configurable heuristics
- generate tailored resume and cover-letter drafts from local materials
- optionally sync shortlisted jobs into Feishu Bitable

This repository is intentionally designed to be publishable. It does not track personal identity data, private job caches, generated documents, or live platform credentials.

## Why this repo exists

This template separates reusable pipeline code from private candidate data. The idea is simple:

- keep code, schemas, and workflow logic in Git
- keep your profile, secrets, generated outputs, and real job history local

That split makes the project easier to open-source, review, fork, and extend.

## Features

- Multi-source collection scripts for LinkedIn, Indeed, and manual link imports
- Merge and dedupe pipeline for raw job feeds
- Configurable heuristic scoring system
- Local resume and cover-letter generation using LaTeX templates
- Optional Feishu Bitable sync and alerting
- Companion Codex skill for operating and adapting the template

## Repository layout

```text
config/                  Example configs and scoring rules
cv/                      Publishable placeholder templates and materials
data/job_cache/          Local input area for manual links and generated caches
scripts/                 Pipeline scripts
skills/                  Companion Codex skill
.github/workflows/       Basic CI
```

## Privacy model

This template intentionally keeps the following out of version control:

- real names, phone numbers, email addresses, LinkedIn URLs, portfolio URLs
- Feishu app secrets, app tokens, table IDs, and view IDs
- cached fetched jobs
- generated resumes and cover letters
- scheduler logs and local runtime artifacts

Before pushing, run:

```bash
make privacy-check
```

## Quick start

1. Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

2. Copy local configs:

```bash
cp config/targets.example.yaml config/targets.yaml
cp config/feishu.example.yaml config/feishu.yaml
cp config/profile.example.yaml config/profile.yaml
```

3. Edit the copied local files with your own values.

4. Add manual links to `data/job_cache/manual_job_inputs.txt`.

5. Run the pipeline in stages:

```bash
python3 scripts/import_manual_jobs.py
python3 scripts/merge_job_sources.py
python3 scripts/score_jobs.py --config config/targets.yaml --scoring-config config/scoring.yaml
python3 scripts/generate_resume.py --min-score 60
```

6. Run a local health check:

```bash
make check
```

## Local-only files you should create

These files are expected locally but ignored by Git:

- `config/targets.yaml`
- `config/feishu.yaml`
- `config/profile.yaml`

Start from:

- `config/targets.example.yaml`
- `config/feishu.example.yaml`
- `config/profile.example.yaml`

## Skill support

This repository includes a companion skill in `skills/job-pipeline-template/`. Use it when you want Codex to:

- understand the template structure quickly
- adapt the pipeline for a new user
- prepare a safe public release of a private job-pipeline repo

## Development

Common commands:

```bash
make check
make privacy-check
```

## Notes

- `scripts/generate_resume.py` supports an optional local `config/profile.yaml`.
- If `config/profile.yaml` is missing, generated documents fall back to safe placeholders.
- LaTeX PDF generation requires `latexmk`.
- This repository assumes a local, user-managed workflow rather than a hosted service.
