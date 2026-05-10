default:
    @just --list

# Run all checks: format, lint, typecheck, tests (no integration)
check: fmt-check lint typecheck test

# Check formatting without writing
fmt-check:
    uv run ruff format --check src tests

# Lint
lint:
    uv run ruff check src tests

# Static type checking
typecheck:
    uv run pyright src

# Run all tests
test:
    uv run pytest

# Auto-fix formatting and lint
fix:
    uv run ruff format src tests
    uv run ruff check --fix src tests
