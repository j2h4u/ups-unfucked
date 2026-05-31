default:
    @just --list

# Run all checks: format, lint, typecheck, tests
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

# Dead-code sieve (advisory — vulture has false positives, read with judgment).
# Catches the "test-only / never-called" class; cannot see production-dead-but-tested
# code (e.g. a method tests cover but the daemon never calls) — that needs call-graph
# tracing from main(). Not wired into `check`.
deadcode:
    uv run vulture
