# Track Job

Privacy-first job search automation for people who want a reusable system, not another spreadsheet.

`track_job` helps you collect roles, score them, generate tailored application materials, and keep the private parts of your workflow out of Git. It also ships with a companion Codex skill so the repo is easier to adapt, maintain, and open-source safely.

## What you can use this for

- Build a personal daily job pipeline instead of manually juggling tabs, notes, and drafts
- Collect roles from LinkedIn, Indeed, or your own hand-picked links
- Shortlist opportunities with transparent scoring rules you can actually inspect
- Generate targeted resume and cover-letter drafts from your own local materials
- Sync shortlisted roles into Feishu Bitable if you want a lightweight review queue
- Turn a private, messy job-application repo into a publish-safe template without leaking identity data

## Why this project is different

Most job-search tooling breaks in one of two ways:

- it is too generic, so it never fits your actual application workflow
- it stores too much personal data in the repo, so publishing it becomes risky

`track_job` is built around a cleaner split:

- reusable code and examples stay in Git
- your profile, secrets, job caches, and generated documents stay local

That makes this repository useful both as:

- a real working pipeline you can fork and personalize
- a reference architecture for privacy-safe automation projects

## Why the companion skill matters

This repo includes a Codex skill at `skills/job-pipeline-template/`.

Use the repository when you want the code.

Use the skill when you want Codex to:

- understand the project structure fast
- adapt the template for a new user
- tune scoring or pipeline behavior
- preserve the privacy boundary while making changes
- help publish a private fork safely

In short:

- the repo is the system
- the skill is the operator guide for AI pair-programming on that system

## How it works

```text
job sources
  -> LinkedIn / Indeed / manual links
  -> merge and dedupe
  -> score with configurable rules
  -> shortlist good-fit roles
  -> generate tailored resume + cover letter drafts
  -> optionally sync to Feishu
```

## Who this is for

This project is a strong fit if you:

- apply to many roles but still want human review over the final shortlist
- want repeatable application materials without storing private data in GitHub
- like hackable local tools more than SaaS dashboards
- want to build on top of an existing pipeline instead of starting from zero
- use Codex or AI coding assistants and want a repo that is skill-aware

## Feature highlights

- Multi-source collection scripts for LinkedIn, Indeed, and manual link imports
- Merge and dedupe pipeline for raw job feeds
- Configurable heuristic scoring system in `config/scoring.yaml`
- Local resume and cover-letter generation using LaTeX templates
- Optional Feishu Bitable sync and failure alerting
- Publish-safe example configs and placeholder materials
- Companion Codex skill for operating and adapting the template

## Privacy-first by design

This repository is intentionally designed to be publishable.

It does not track:

- real names, phone numbers, email addresses, LinkedIn URLs, or portfolio URLs
- Feishu app secrets, app tokens, table IDs, or view IDs
- fetched job caches
- generated resumes and cover letters
- scheduler logs and local runtime artifacts

Before pushing changes, run:

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

6. Run the local checks:

```bash
make check
```

## Repository layout

```text
config/                  Example configs and scoring rules
cv/                      Publishable placeholder templates and materials
data/job_cache/          Local input area for manual links and generated caches
scripts/                 Pipeline scripts
skills/                  Companion Codex skill
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

## Good starting use cases

- Fork this repo and turn it into your own job-application operating system
- Reuse just the scoring and dedupe pieces in another personal pipeline
- Use it as a template for any workflow that needs a public/private split
- Pair it with Codex using the included skill for faster customization

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
- This project is local-first and user-managed rather than a hosted service.
