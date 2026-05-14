# Security Policy

## Reporting a vulnerability

Do not open a public issue for secrets exposure, credential leakage, or unintended personal-data publication.

Instead, privately contact the maintainer and include:

- a short description of the issue
- affected paths
- whether the issue is present in tracked files, generated outputs, or ignored local files

## Sensitive data classes for this repository

- personal identity data
- live Feishu credentials and IDs
- private job caches and generated application materials

## Safe contribution rule

If a change makes it easier to accidentally commit private candidate data, treat it as a security-sensitive regression.
