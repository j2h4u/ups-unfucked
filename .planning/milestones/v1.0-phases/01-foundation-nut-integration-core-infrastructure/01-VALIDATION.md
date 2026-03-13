---
phase: 1
slug: foundation-nut-integration-core-infrastructure
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-13
---

# Phase 1 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.0+ |
| **Config file** | tests/conftest.py |
| **Quick run command** | `pytest tests/test_nut_client.py tests/test_ema.py -v` |
| **Full suite command** | `pytest tests/ -v --cov=src` |
| **Estimated runtime** | ~30 seconds |

---

## Sampling Rate

- **After every task commit:** Run `pytest tests/test_nut_client.py tests/test_ema.py -v`
- **After every plan wave:** Run `pytest tests/ -v --cov=src`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 30 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 1-01-01 | 01 | 0 | DATA-01 | integration | `pytest tests/test_nut_client.py::test_continuous_polling -v` | ❌ W0 | ⬜ pending |
| 1-01-02 | 01 | 0 | DATA-01 | unit | `pytest tests/test_nut_client.py::test_socket_timeout -v` | ❌ W0 | ⬜ pending |
| 1-02-01 | 01 | 1 | DATA-02 | unit | `pytest tests/test_ema.py::test_ema_convergence -v` | ❌ W0 | ⬜ pending |
| 1-02-02 | 01 | 1 | DATA-02 | unit | `pytest tests/test_ema.py::test_stabilization_gate -v` | ❌ W0 | ⬜ pending |
| 1-03-01 | 01 | 1 | DATA-03 | unit | `pytest tests/test_model.py::test_ir_compensation -v` | ❌ W0 | ⬜ pending |
| 1-04-01 | 01 | 1 | MODEL-01 | unit | `pytest tests/test_model.py::test_model_load_save -v` | ❌ W0 | ⬜ pending |
| 1-05-01 | 01 | 1 | MODEL-02 | unit | `pytest tests/test_model.py::test_vrla_lut_init -v` | ❌ W0 | ⬜ pending |
| 1-06-01 | 01 | 1 | MODEL-04 | unit | `pytest tests/test_model.py::test_atomic_write -v` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_nut_client.py` — stubs for DATA-01 (socket communication, reconnect, timeout)
- [ ] `tests/test_ema.py` — stubs for DATA-02 (EMA convergence, stabilization gate)
- [ ] `tests/test_model.py` — stubs for DATA-03, MODEL-01, MODEL-02, MODEL-04 (IR comp, LUT init, atomic writes)
- [ ] `tests/conftest.py` — shared fixtures (mock upsd responses, temporary model.json paths)
- [ ] Framework install: `pip install pytest pytest-cov` (if not already present)

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Daemon recovers after `systemctl restart nut-server` | DATA-01 | Requires live NUT service restart on senbonzakura | 1. Start daemon. 2. `sudo systemctl restart nut-server`. 3. Verify daemon logs show reconnect within 10 sec. |
| EMA stabilizes in real conditions over 2 min | DATA-02 | Requires live UPS data stream | 1. Start daemon. 2. Wait 2 min. 3. Check logs show voltage oscillation < ±0.1V. |
| model.json unchanged after 24h normal operation | MODEL-04 | Requires 24h runtime on real hardware | 1. Note model.json mtime. 2. Run daemon 24h. 3. Verify mtime unchanged (no spurious writes). |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 30s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
