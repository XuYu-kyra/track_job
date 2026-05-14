# Contributing

## Scope

This repository is a template, not a hosted service. Contributions should improve one of:

- pipeline reliability
- publish-safe defaults
- scoring clarity
- documentation quality
- portability for other users

## Before opening a pull request

1. Keep personal data, generated job caches, and secrets out of commits.
2. Run:

```bash
make check
```

3. If you add or change sample configs, keep them clearly fake and reusable.

## Style expectations

- Prefer small, reviewable changes.
- Preserve the privacy boundary between tracked examples and local-only files.
- Avoid adding user-specific hardcoded identity data.
