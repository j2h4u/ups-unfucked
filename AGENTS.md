# Agent quality gates

Use targeted tests while developing a coherent cluster. Before handing off a release candidate,
run `just check`; CI mirrors the same gates.

`uv run ruff check src tests scripts` is a mandatory structural-complexity gate, not optional style. It
enforces the repository defaults for:

- McCabe complexity (`C901`);
- public methods per class (`PLR0904`);
- returns, branches, arguments, locals, statements, Boolean expressions, and positional
  arguments (`PLR0911` through `PLR0917`);
- nested blocks (`PLR1702`).

Do not suppress a finding, loosen a threshold, or add an exception merely to make a gate green.
Refactor the boundary or explain why the release is blocked.

The test quality gate is per-function CRAP score, with a maximum of 30. Coverage is collected only
as an input to CRAP; there is no repository-wide coverage percentage target. Prefer tests for risky
branches or a simpler function over padding coverage with low-value assertions.
