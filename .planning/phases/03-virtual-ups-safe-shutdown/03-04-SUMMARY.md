---
phase: 03-virtual-ups-safe-shutdown
plan: 04
subsystem: systemd-configuration-nut-integration
tags:
  - Wave 3 infrastructure
  - Systemd service configuration
  - NUT dummy-ups registration
  - Shutdown coordination documentation

requires:
  - phase: "03-virtual-ups-safe-shutdown"
    provides: "Wave 2: Virtual UPS writing integration (monitor.py polling loop)"
  - phase: "02-battery-model-state-estimation-event-classification"
    provides: "Event classifier, runtime calculator, monitor daemon structure"

provides:
  - "Systemd service with sysinit.target dependency ensuring /dev/shm availability"
  - "NUT dummy-ups configuration snippet ready for Phase 5 installation"
  - "Shutdown coordination mechanism fully documented"

affects:
  - "Phase 5: Installation and live NUT integration testing"
  - "Phase 4+: Alert mechanisms and model updates based on virtual UPS status"

tech-stack:
  added: []
  patterns:
    - "Systemd dependency ordering via After= (sysinit.target for tmpfs availability)"
    - "NUT dummy-ups driver configuration (dummy-once mode for atomic reads)"
    - "Virtual UPS transparent proxy pattern: 3 computed overrides + passthrough fields"

key-files:
  created:
    - "config/dummy-ups.conf"
  modified:
    - "systemd/ups-battery-monitor.service"
    - "CONTEXT.md"

key-decisions:
  - "Added sysinit.target before network-online.target to guarantee tmpfs mount"
  - "Use dummy-once mode in NUT config for atomic reads on file timestamp change"
  - "Virtual device file in /dev/shm (tmpfs) for zero SSD wear"
  - "Device name [cyberpower-virtual] to avoid conflicts with real UPS [cyberpower]"

patterns-established:
  - "Systemd service pattern: order dependencies by system init stage (sysinit → network → application)"
  - "NUT integration pattern: dummy-ups with mode=dummy-once for atomic virtual device reads"
  - "Configuration snippet pattern: ready-for-install documentation with parameter explanations"

requirements-completed:
  - SHUT-01 (systemd service with sysinit dependency)
  - SHUT-02 (NUT dummy-ups configuration)
  - SHUT-03 (shutdown coordination documentation)

duration: "~4 minutes"
completed: 2026-03-14T00:30:00Z
---

# Phase 3 Plan 04: Systemd Configuration & NUT Integration Summary

**Wave 3 Objective:** Configure systemd service for safe daemon startup and provide NUT dummy-ups configuration snippet for transparent virtual UPS integration.

**One-liner:** Updated systemd service with sysinit.target dependency and created NUT dummy-ups configuration block for Phase 5 installation.

---

## Performance

- **Duration:** ~4 minutes
- **Started:** 2026-03-14T00:25:00Z
- **Completed:** 2026-03-14T00:30:00Z
- **Tasks:** 3/3 complete
- **Files created:** 1 (config/dummy-ups.conf)
- **Files modified:** 2 (systemd/ups-battery-monitor.service, CONTEXT.md)

---

## Accomplishments

### Task 1: Update systemd service with After=sysinit.target dependency

**Completed:** ✓

- **Modified:** systemd/ups-battery-monitor.service
- **Change:** Updated After= line from `After=network-online.target nut-server.service` to `After=sysinit.target network-online.target nut-server.service`
- **Rationale:** sysinit.target ensures basic system services (filesystems, tmpfs mounts) are online before daemon starts
- **Verification:** grep confirms sysinit.target present in After= line
- **Defense-in-depth:** Works with daemon-level /dev/shm existence checks (no single point of failure)

**File Content (Updated):**
```ini
[Unit]
Description=UPS Battery Monitor
Documentation=https://github.com/j2h4u/ups-battery-monitor
After=sysinit.target network-online.target nut-server.service
Wants=network-online.target
ConditionPathExists=/run/nut/

[Service]
Type=simple
User=j2h4u
Group=j2h4u
WorkingDirectory=/home/j2h4u/repos/j2h4u/ups-battery-monitor
Environment="PYTHONPATH=/home/j2h4u/repos/j2h4u/ups-battery-monitor"
ExecStart=/usr/bin/python3 -m src.monitor
Restart=on-failure
RestartSec=10
StartLimitBurst=3
StartLimitIntervalSec=60
TimeoutStartSec=30
StandardOutput=journal
StandardError=journal
SyslogIdentifier=ups-battery-monitor

[Install]
WantedBy=multi-user.target
```

### Task 2: Create NUT dummy-ups configuration snippet

**Completed:** ✓

- **Created:** config/dummy-ups.conf
- **Contents:** NUT device block ready for Phase 5 installation into /etc/nut/ups.conf
- **Verification:** All 4 required parameters present (driver, port, mode, desc)

**File Content (Created):**
```bash
# Configuration block to add to /etc/nut/ups.conf
# This registers the virtual UPS that reads corrected metrics from /dev/shm/ups-virtual.dev
# Added by ups-battery-monitor Phase 3

[cyberpower-virtual]
driver = dummy-ups
port = /dev/shm/ups-virtual.dev
mode = dummy-once
desc = "Virtual UPS proxy with corrected battery metrics from calculated model"
```

**Parameter Explanation:**
- `[cyberpower-virtual]`: Device name (matches references in upsmon.conf)
- `driver = dummy-ups`: NUT driver reads static .dev file format
- `port = /dev/shm/ups-virtual.dev`: Path to virtual device file written by daemon
- `mode = dummy-once`: Re-parse file only on timestamp change (atomic reads, CPU efficient vs dummy-loop polling)
- `desc`: Human-readable description for NUT tools

**Installation Notes (Phase 5):**
- This block appends to /etc/nut/ups.conf (or inserts before [cyberpower] real UPS block)
- Real UPS [cyberpower] block remains unchanged
- After installation, upsmon.conf updated to MONITOR cyberpower-virtual@localhost instead of cyberpower
- Error handling: If /dev/shm/ups-virtual.dev missing → dummy-ups fails to start upsd (daemon writes it every 10 sec)

### Task 3: Document shutdown coordination in CONTEXT.md

**Completed:** ✓

- **Modified:** CONTEXT.md (added "Shutdown Coordination (Phase 3)" section)
- **Location:** After "Хранение состояния" section, before "Модель батареи"
- **Coverage:** Complete 5-step flow from daemon monitoring through upsmon shutdown trigger
- **Verification:** grep confirms section present

**Documented:**

1. **Virtual UPS Proxy Flow** — 5-step sequence:
   - Daemon monitors battery state (Phase 1-2)
   - Computes ups.status override based on event type (Phase 3)
   - Writes atomic virtual UPS file to /dev/shm
   - NUT dummy-ups reads virtual device
   - upsmon receives LB signal and initiates shutdown

2. **Shutdown Threshold Configuration:**
   - Environment variable: `UPS_MONITOR_SHUTDOWN_THRESHOLD_MIN` (default: 5 minutes)
   - Configurable for different safety margins
   - Calibration mode: reduce to 1 minute for battery cutoff data collection

3. **Event Classification:**
   - ONLINE: Mains present (input.voltage ~230V)
   - BLACKOUT_REAL: Battery only, no mains (input.voltage ~0V)
   - BLACKOUT_TEST: Battery test with mains present (input.voltage ~230V)
   - Prevents false shutdown triggers during battery tests

---

## Task Commits

| Task | Commit | Message |
|------|--------|---------|
| 1 | `b4d2225` | feat(03-04): add sysinit.target dependency to ensure /dev/shm is available |
| 2 | `72a47bb` | feat(03-04): create NUT dummy-ups configuration snippet |
| 3 | `efee038` | docs(03-04): document shutdown coordination mechanism in CONTEXT.md |

---

## Decisions Made

1. **Dependency Ordering:** Add sysinit.target before network-online.target
   - Ensures tmpfs filesystems mount before daemon startup
   - Critical: /dev/shm must exist before daemon tries to write virtual UPS file
   - Non-critical dependency on nut-server (Wants vs Requires)

2. **NUT Configuration Location:** Separate config/dummy-ups.conf file
   - Rationale: Ready-for-install documentation; Phase 5 merges into /etc/nut/ups.conf
   - Alternative considered: Embed in README → rejected (harder to copy-paste for sysadmins)

3. **Dummy-ups Mode Selection:** mode=dummy-once
   - Rationale: Atomic reads on file timestamp change; CPU efficient
   - Alternative: mode=dummy-loop (polls continuously) → rejected (unnecessary CPU usage)

4. **Virtual Device Path:** /dev/shm/ups-virtual.dev (tmpfs)
   - Rationale: Zero SSD wear; survives daemon restart; cleared on reboot
   - Alternative: /run/ups-virtual.dev → same tmpfs, but /dev/shm is more standard for device files

5. **Device Naming:** [cyberpower-virtual] (not [cyberpower] or [virtual-cyberpower])
   - Rationale: Keeps real UPS name unchanged; clear virtual proxy relationship
   - Consequence: upsmon.conf must be updated to reference new device name (Phase 5)

---

## Deviations from Plan

None — plan executed exactly as written. All tasks completed successfully.

---

## Verification Summary

- ✓ systemd service After= line includes sysinit.target
- ✓ Service unit is syntactically valid (no parse errors)
- ✓ config/dummy-ups.conf contains all 4 required parameters
- ✓ NUT configuration block is installable (valid NUT syntax)
- ✓ CONTEXT.md shutdown coordination section complete
- ✓ All 3 tasks committed with descriptive messages
- ✓ No regressions in existing code
- ✓ Phase 4 readiness: systemd and NUT configs ready for Phase 5 installation

---

## Issues Encountered

None — all tasks executed smoothly without blockers.

---

## User Setup Required

**None for this phase.** Systemd service and NUT configuration are provisioning artifacts for Phase 5.

Installation will occur in Phase 5:
- Copy systemd/ups-battery-monitor.service → /etc/systemd/system/
- Append config/dummy-ups.conf to /etc/nut/ups.conf
- Run systemctl daemon-reload + systemctl enable ups-battery-monitor
- Restart NUT services (upsd, upsmon)

---

## Next Phase Readiness

✓ Wave 3 Systemd & NUT Configuration complete
✓ Service ordering ensures /dev/shm available before daemon starts
✓ NUT dummy-ups configuration ready for production installation
✓ Shutdown coordination mechanism fully documented
✓ Ready for Phase 4: Alert thresholds and model lifecycle

**What's ready:**
- Systemd service with correct dependency ordering
- NUT dummy-ups configuration block (copy-paste ready)
- Complete documentation of shutdown coordination flow
- Virtual UPS infrastructure complete end-to-end (Wave 0-3)

**Next step (Phase 4):** Define alert thresholds (SoH < X%, runtime < Y min) and model update strategies.

---

## Self-Check

- [x] Task 1: systemd service updated with After=sysinit.target
- [x] Task 2: config/dummy-ups.conf created with all parameters
- [x] Task 3: CONTEXT.md shutdown coordination section added
- [x] All 3 commits created with descriptive messages
- [x] File verification: grep confirms all expected content
- [x] No syntax errors in modified files
- [x] No deviations from plan
- [x] Ready for Phase 5 installation testing

---

*Phase: 03-virtual-ups-safe-shutdown*
*Plan: 04 (Wave 3)*
*Completed: 2026-03-14*
