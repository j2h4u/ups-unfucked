---
phase: 23-test-quality-rewrite
plan: "02"
subsystem: virtual_ups / tests
tags: [dependency-injection, test-quality, refactoring]
dependency_graph:
  requires: []
  provides: [output_path DI on write_virtual_ups_dev, Path-patch-free test_virtual_ups.py]
  affects: [tests/test_virtual_ups.py, src/virtual_ups.py]
tech_stack:
  added: []
  patterns: [dependency injection via optional parameter]
key_files:
  created: []
  modified:
    - src/virtual_ups.py
    - tests/test_virtual_ups.py
decisions:
  - "Optional[Path] default=None: backward compatible — None resolves to production /run/ path"
  - "output_path placed as third parameter to keep ups_name positional order stable"
  - "Removed entire unittest.mock import (Mock, patch, mock_open all unused after rewrite)"
metrics:
  duration: "~8 min"
  completed: "2026-03-20"
  tasks_completed: 2
  files_modified: 2
requirements_satisfied: [TEST-03]
---

# Phase 23 Plan 02: Virtual UPS Dependency Injection Summary

**One-liner:** Add `output_path: Optional[Path] = None` DI parameter to `write_virtual_ups_dev()` and eliminate all 5 Path-class-patching test sites in test_virtual_ups.py.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Add output_path DI parameter to write_virtual_ups_dev() | 556fcce | src/virtual_ups.py |
| 2 | Rewrite test_virtual_ups.py to use output_path DI | 9d36f94 | tests/test_virtual_ups.py |

## What Changed

### src/virtual_ups.py

- Added `Optional` to `typing` imports
- Added `output_path: Optional[Path] = None` as third parameter to `write_virtual_ups_dev()`
- Changed path resolution from `Path("/run/...")` to `output_path or Path("/run/...")`
- Symlink guard unchanged — still applies to the resolved `virtual_ups_path`
- Added `output_path` to docstring Args section

### tests/test_virtual_ups.py

- Replaced all 5 `with patch("src.virtual_ups.Path", side_effect=...)` blocks with `write_virtual_ups_dev(metrics, output_path=test_file)`
- Removed the inner `with patch(...)` context manager wrapping (no longer needed)
- Removed unused `from unittest.mock import Mock, patch, mock_open` import entirely
- `tempfile.TemporaryDirectory` context managers retained — still manage tmpdir lifecycle

## Verification

```
python3 -m pytest tests/test_virtual_ups.py -x -v  → 22 passed
python3 -m pytest tests/ -x -q                     → 555 passed
grep -c 'patch.*virtual_ups.Path' tests/test_virtual_ups.py → 0
grep -c 'output_path=' tests/test_virtual_ups.py           → 5
```

## Deviations from Plan

None — plan executed exactly as written.

## Self-Check: PASSED

- [x] src/virtual_ups.py modified: `output_path` in signature, docstring, and path assignment
- [x] tests/test_virtual_ups.py modified: 5 DI sites, mock import removed
- [x] Commits 556fcce and 9d36f94 exist
- [x] 22 test_virtual_ups.py tests pass
- [x] 555 full suite tests pass
- [x] Zero Path-patching remaining in test_virtual_ups.py
