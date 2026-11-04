# Contributing to ha-ebay

Thank you for contributing to the Home Assistant eBay integration!

## Prerequisites

Development requires:
- Python >= 3.14.2
- `uv` package and project manager
- Git

## Local Setup

Install dependencies and set up pre-commit hooks using `uv`:

```bash
uv sync --locked
uv run --locked pre-commit install --hook-types pre-commit --hook-types pre-push
```

## Local Validation Contract

Before submitting a pull request, run the portfolio standard validation interface:

```bash
./scripts/validate-local.sh
```

This automated script executes the complete local hygiene and quality gate:
1. Toolchain & Python 3.14 verification
2. Lockfile freshness check (`uv lock --check`)
3. Pre-commit hooks (`uv run --locked pre-commit run --all-files`)
4. Ruff linting and format verification (`uv run --locked ruff check .` and `uv run --locked ruff format --check .`)
5. Python bytecode compilation (`uv run --locked python -m compileall -q custom_components scripts tests`)
6. Pytest suite with branch coverage enforcement (`--cov-fail-under=90`, currently achieving >90.8%)
7. Clean git working tree verification

## Code Style and Conventional Commits

- Commit messages and PR titles must follow [Conventional Commits](https://www.conventionalcommits.org/) (e.g., `feat:`, `fix:`, `chore:`).
- All changes must maintain or improve test coverage without regression.
- For detailed architecture, OAuth test flows, and devcontainer instructions, see [Development Guide](docs/development.md).
