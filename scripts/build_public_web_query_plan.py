#!/usr/bin/env python3
"""CLI compatibility wrapper for the two-axis discovery query planner."""

try:
    from public_web_query_plan import main
except ModuleNotFoundError:
    from scripts.public_web_query_plan import main


if __name__ == "__main__":
    raise SystemExit(main())
