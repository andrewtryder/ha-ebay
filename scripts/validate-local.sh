#!/usr/bin/env bash
set -euo pipefail

# Portfolio Universal Local Validation Contract
# Repository: ha-ebay
# Gate: 90% component branch coverage

echo "==> [1/7] Verifying toolchain and interpreter"
command -v uv >/dev/null 2>&1 || { echo "Error: uv is required but not installed"; exit 1; }
python_version=$(uv run --locked python --version)
echo "Using $python_version"

echo "==> [2/7] Verifying dependency lockfile freshness"
uv lock --check

echo "==> [3/7] Running pre-commit hooks on all files"
uv run --locked pre-commit run --all-files

echo "==> [4/7] Running Ruff lint and formatting checks"
uv run --locked ruff check .
uv run --locked ruff format --check .

echo "==> [5/7] Compiling Python source files"
uv run --locked python -m compileall -q custom_components scripts tests

echo "==> [6/7] Running Python test suite with branch coverage (>=90% gate)"
uv run --locked pytest -q

echo "==> [7/7] Verifying clean git working tree"
git diff --exit-code

echo "==> Local validation passed successfully!"
