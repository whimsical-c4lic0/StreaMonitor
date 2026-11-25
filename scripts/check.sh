#!/usr/bin/env bash
# Run the same static checks locally and in CI. Requires a frozen dev install:
#   uv sync --frozen
set -euo pipefail

cd "$(dirname "$0")/.."

uv run --frozen --no-sync ruff check .
uv run --frozen --no-sync ruff format --check .
uv run --frozen --no-sync mypy
uv run --frozen --no-sync python scripts/check_templates.py
