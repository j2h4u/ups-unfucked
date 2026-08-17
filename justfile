default:
    @just --list

# Mirror CI (.github/workflows/ci.yml) exactly — green here ⟺ green in CI. Run before every push.
# (CI also runs the test job across the 3.13 + 3.14 matrix; locally it's whatever uv resolves.)
check: fmt-check lint source-spans architecture typecheck deadcode test-quality

# Check formatting without writing
fmt-check:
    uv run ruff format --check src tests scripts

# Lint and structural-complexity gate (C90 + PLR09/PLR17)
lint:
    uv run ruff check src tests scripts
    uv run python scripts/check_complexity_suppressions.py

# Hard source concentration budgets; no baseline or ratchet is permitted.
source-spans:
    uv run python scripts/check_source_spans.py

# Enforce declared import boundaries with both architecture engines.
architecture:
    uv run lint-imports
    uv run tach check
    uv run tach check --exact

# Static type checking (src + scripts, per pyproject [tool.pyright].include)
typecheck:
    uv run pyright

# Run all tests (fast inner loop, no coverage gate)
test:
    uv run pytest

# Tests with the CI CRAP gate. Coverage is an input to CRAP, not an independent target.
test-quality:
    uv run pytest --cov=src --crap --crap-threshold=30
    uv run python scripts/check_crap.py --threshold 30

# Auto-fix formatting and lint
fix:
    uv run ruff format src tests scripts
    uv run ruff check --fix src tests scripts

# Install git hooks (pre-push runs `just check`). Run once per clone.
install-hooks:
    git config core.hooksPath .githooks
    chmod +x .githooks/*
    @echo "core.hooksPath -> .githooks; pre-push now runs 'just check'"

# Dead-code sieve (vulture, whitelist-gated). Reviewed false positives belong in
# vulture_whitelist.py with a rationale, not in a weakened quality gate.
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
