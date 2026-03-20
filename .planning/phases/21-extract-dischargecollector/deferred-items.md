# Deferred Items — Phase 21

## Pre-existing test failure (out of scope)

**File:** tests/test_discharge_handler.py
**Class:** TestSulfationMethodSplit
**Test:** test_compute_returns_all_required_keys
**Error:** `AttributeError: 'DischargeHandler' object has no attribute '_compute_sulfation_metrics'`
**Status:** Pre-existing failure present before Phase 21. The TestSulfationMethodSplit class was added to test_discharge_handler.py in a prior commit (visible in git status at plan start). The method `_compute_sulfation_metrics` is planned for future DischargeHandler refactoring. Not caused by Phase 21 changes.
