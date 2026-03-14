# Expert Panel Review — 2026-03-14 (Post v1.1)

Comprehensive review of codebase after v1.1 milestone completion (all 21 findings from 2026-03-15 review resolved).
Panel: System Architect, Security Analyst, SRE, QA Engineer, Kaizen Master.

## Scope

- **What**: ~1,800 LoC Python daemon (12 modules, 205 tests) replacing CyberPower UPS firmware metrics with physics-based calculations
- **Blast radius**: `host-level` — incorrect LB flag = premature shutdown or data loss from no shutdown
- **Decision type**: Stress-testing post-v1.1 codebase

---

## P0 — Safety Critical

Nothing found. v1.1 addressed the per-poll LB lag (the only P0 from last review). The polling gate at `monitor.py:781` correctly differentiates OB (every poll) from OL (every 6th). LB flag path is clean.

---

## P1 — High Priority

### Architecture

- [x] **`CurrentMetrics.update()` doesn't exist** (Architect): `monitor.py:677` calls `self.current_metrics.update(soc=soc, ...)` but `CurrentMetrics` is a plain `@dataclass` — no `update()` method. This silently succeeds because `dataclass.__init__` doesn't define `update()`, but Python dataclasses have no dict-like `update()`. This must be hitting `AttributeError` in production or there's a `__setattr__` path I'm missing. **Verify this actually works at runtime.** If it does, it's accidental. If it doesn't, metrics never propagate.

- [x] **`atomic_write_json` fdatasync on read-only FD is a no-op** (Architect + SRE): `model.py:49` opens the temp file `O_RDONLY` after writing, then calls `fdatasync(fd)`. On Linux, `fdatasync` on a read-only FD may or may not flush the page cache depending on filesystem and kernel version. The write FD (from `NamedTemporaryFile`) was already closed by the `with` block, which calls `fclose()` → `fflush()` but does NOT call `fsync`/`fdatasync`. **Data could be lost on power failure between close and rename.** Fix: keep the write FD open, fdatasync it, then close, then rename.

- [x] **`_default_config` evaluated at import time** (Architect): `monitor.py:108` creates a Config instance at module import. This means importing `monitor` in tests immediately reads `config.toml` from disk. Test isolation depends on filesystem state. Move to lazy initialization or remove the module-level instance.

### Testing

- [x] **No test for `run()` loop termination** (QA): `MonitorDaemon.run()` is 55 lines with complex branching (error handling, sag state reset, rate-limited logging). Zero test coverage for the main loop. Previous review flagged this (P1), still unfixed.

- [x] **`_write_calibration_points` re-raises from batch flush** (QA): `monitor.py:653` has `raise` inside the except handler for `calibration_batch_flush()`. This propagates into the `run()` loop's generic except handler, which logs and continues. But the `raise` means calibration errors crash the daemon if they're not caught. **Either remove the `raise` or add a test proving the outer handler catches it.**

---

## P2 — Medium Priority

### Code Quality

- [x] **`EMAFilter` duplicates `MetricEMA` logic** (Kaizen): `ema_filter.py:99-117` — `EMAFilter._adaptive_alpha()` and `_update_ema()` are identical copies of `MetricEMA._adaptive_alpha()` and `_update_ema()`. The whole point of v1.1 LOW-03 was to extract `MetricEMA`. But `EMAFilter` still has its own copies of these methods that are never called (because `add_sample` delegates to `MetricEMA.update()`). **Dead code.** Delete `_adaptive_alpha` and `_update_ema` from `EMAFilter`.

- [x] **`soc_predictor.py:58` comment says "Binary search"** (Kaizen): Line 58 comment `# Binary search for bracketing points` but implementation is linear scan. v1.1 QUAL-03 fixed the docstring but missed this inline comment.

- [x] **`virtual_ups.py` uses `os.fsync` while `model.py` uses `os.fdatasync`** (Kaizen): Inconsistent. v1.1 LOW-02 changed `model.py` to fdatasync but didn't touch `virtual_ups.py:77`. On tmpfs this is a no-op anyway, but inconsistency is a smell. Either both use fdatasync or document why they differ.

- [x] **`logger` vs `ups_logger` confusion** (Kaizen): `monitor.py:170` creates `logger = getLogger('ups-battery-monitor')`, then `monitor.py:184` creates `ups_logger = getLogger('ups-battery-monitor')` — same logger instance, two names. `ups_logger` is passed to alerter functions. Just use `logger` everywhere.

- [x] **`discharge_buffer` is a dict, not a dataclass** (Architect): `monitor.py:267-271` — the whole v1.1 point was eliminating untyped dicts. This one was missed. Three keys (`voltages`, `times`, `collecting`), mutated from 4 methods. Extract to `@dataclass`.

- [x] **Phase comments still in code** (Kaizen): `monitor.py:266` "Phase 4:", `monitor.py:282` "Phase 6:", `monitor.py:183` "Phase 4" — implementation artifacts from GSD workflow. Remove, they add no value to the reader.

### Performance

- [x] **`health.json` written every 10s via `atomic_write_json`** (SRE): `atomic_write_json` creates a temp file, writes, opens read-only, fdatasync, rename. That's 4 syscalls per poll. For a health endpoint file, this is overkill. A simple `open + write + close` is sufficient — health.json is ephemeral status, not persistent data. If the write is interrupted, the next poll writes a fresh one.

- [x] **`calibration_write()` does linear dedup scan** (SRE): `model.py:304` scans entire LUT for timestamp match on every call. LUT grows during discharge (up to 1000 entries x `len(lut)` comparisons). O(n^2) during long blackout. Use a `set()` for seen timestamps.

- [x] **`_log_status` recomputes `ir_compensate`** (Kaizen): `monitor.py:699` calls `ir_compensate()` again even though `_compute_metrics()` already computed it. Cache the value.

### Security

- [x] **`os.chmod(str(virtual_ups_path), 0o644)` after rename** (Security): `virtual_ups.py:84` — between rename and chmod, the file has default umask permissions. If UMask=0077 (set in systemd unit), the file is briefly 0600 — NUT's `nut` user can't read it. Race condition on every write. **Fix: set umask before write or use `os.fchmod` on the FD before closing.**

---

## P3 — Low Priority / Deferred

- [x] **No `pyproject.toml`** (Architect): Project has no packaging metadata. Not installable via pip, no declared Python version constraint, no dependency metadata. Fine for single-server use, but blocks reuse.

- [x] **`model.py` `data` dict is public** (Architect): `BatteryModel.data` is directly accessed from monitor.py in 5+ places (`self.battery_model.data['lut']`, `self.battery_model.data['soh']`). Breaks encapsulation. All access should go through getters. Not urgent because single consumer.

- [x] **`_weighted_average_by_voltage` imports `time` inside function** (Kaizen): `soh_calculator.py:110` — `import time as _time` inside function body. Move to module top. (Same pattern v1.1 ARCH-03 was supposed to fix.)

- [x] **`LUT` grows unbounded during calibration** (SRE): `calibration_write()` appends measured entries without pruning. During a 3-hour discharge (1000 samples), LUT gets 1000+ entries. No LUT pruning exists (only soh_history and r_internal_history were pruned in v1.1). After many discharges, LUT bloats -> linear scan and dedup get slower.

- [x] **`replacement_predictor` uses `any` as type hint** (QA): `replacement_predictor.py:9` — `List[Dict[str, any]]` should be `List[Dict[str, Any]]` (capital A). Works at runtime but mypy would flag it.

- [x] **No log rotation for health.json writes** (SRE): P2-7 replaced `atomic_write_json` with simple `json.dump` — no INFO log on every write. Already fixed.

---

## Security Assessment (no new action items beyond P2 chmod race)

- Symlink guard on `/dev/shm` write — good (`virtual_ups.py:49`)
- systemd hardening comprehensive (ProtectSystem=strict, NoNewPrivileges, PrivateDevices, RestrictAddressFamilies)
- `model.json` world-readable — acceptable, battery data only
- No network-facing surface (reads from localhost NUT only)
- **chmod race** on virtual UPS file — only real finding (see P2)

---

## Panel Conflicts

| Topic | Position A | Position B | Resolution |
|-------|-----------|-----------|------------|
| health.json atomic write | SRE: overkill for ephemeral status | Architect: consistency with model.json pattern | **SRE wins** — health.json is not persistent data, simple write suffices |
| `discharge_buffer` -> dataclass | Architect: consistency with v1.1 pattern | Kaizen: 3 keys, 4 usages, YAGNI | **Architect wins** — was explicitly v1.1's goal to eliminate untyped dicts |
| LUT pruning | SRE: unbounded growth over years | Kaizen: `_weighted_average_by_voltage` already consolidates | **Compromise** — add LUT size cap (e.g., 200 entries) in `save()`, similar to soh_history pruning |

---

## Resolved Open Questions

- ~~**`CurrentMetrics.update()` call**: Must verify at runtime.~~ **FIXED**: Was indeed broken (AttributeError). Replaced with direct field assignment.
- ~~**fdatasync on read-only FD**: Needs kernel-level verification or rewrite.~~ **FIXED**: `flush()` + `fdatasync()` now called on write FD inside `with` block.
- ~~**`run()` loop test**: At minimum test error-rate-limiting branch.~~ **FIXED**: 3 tests added — error rate limiting, sag state reset on error, OB per-poll gate.

---

## Summary by Priority

| Priority | Count | Key Items |
|----------|-------|-----------|
| P0 | 0 | None |
| P1 | ~~5~~ 0 | ~~All fixed~~: `update()`→direct assign, fdatasync on write FD, `_default_config` removed, 3 run() loop tests added, bare `raise` removed |
| P2 | ~~9~~ 0 | ~~All fixed~~: dead code deleted, comment fixed, fdatasync+fchmod on write FD, `ups_logger` removed, `DischargeBuffer` dataclass, phase comments removed, health.json simple write, timestamp dedup set, v_norm cached |
| P3 | ~~6~~ 0 | ~~All fixed~~: pyproject.toml added, `set_soh()`+getters for encapsulation, `import time` moved to top, LUT pruning in `save()`, `Any` type hint, health.json log noise (already fixed by P2-7), bisect replaces linear scan |

**Overall assessment**: Solid codebase for a single-server daemon. v1.1 fixed the critical findings well. Remaining issues are correctness edge cases (P1), code hygiene (P2), and long-term maintainability (P3). No safety-critical findings.
