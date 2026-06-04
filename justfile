default:
    @just --list

# Mirror CI (.github/workflows/ci.yml) exactly — green here ⟺ green in CI. Run before every push.
# (CI also runs the test job across the 3.13 + 3.14 matrix; locally it's whatever uv resolves.)
check: fmt-check lint typecheck deadcode test-cov

# Check formatting without writing
fmt-check:
    uv run ruff format --check src tests

# Lint
lint:
    uv run ruff check src tests

# Static type checking
typecheck:
    uv run pyright src

# Run all tests (fast inner loop, no coverage gate)
test:
    uv run pytest

# Tests with the CI coverage gate (CI: pytest --cov=src --cov-fail-under=80)
test-cov:
    uv run pytest --cov=src --cov-fail-under=80

# Auto-fix formatting and lint
fix:
    uv run ruff format src tests
    uv run ruff check --fix src tests

# Dead-code sieve (vulture, whitelist-gated) — part of `check`/CI. Reviewed false
# positives go in vulture_whitelist.py with a rationale, not by loosening the gate.
# Catches the "test-only / never-called" class; cannot see production-dead-but-tested
# code (e.g. a method tests cover but the daemon never calls) — that needs call-graph
# tracing from main().
deadcode:
    uv run vulture

# --- Coverage ---

# Test coverage (quick terminal report).
coverage:
    uv run pytest --cov=src --cov-report=term-missing

# Double coverage: find code exercised by NEITHER tests NOR production runtime
# (strong dead-code signal — catches paths tests cover but the daemon never hits).
# Workflow:
#   1) just coverage-tests      collect test coverage (parallel-mode, combinable)
#   2) just coverage-runtime    collect runtime coverage on the server (see note)
#   3) just coverage-report     combine + report; missing lines = dead in both
# NOTE for step 2: stop the systemd service first
#   (sudo systemctl stop ups-battery-monitor) so two daemons don't both write
#   model.json, and run long enough to span a discharge/test event.
coverage-tests:
    uv run coverage run --parallel-mode -m pytest -q

coverage-runtime:
    uv run coverage run --parallel-mode -m src.monitor

coverage-report:
    uv run coverage combine
    uv run coverage report --show-missing
