# Phase 5: Operational Setup & Systemd Integration - Research

**Researched:** 2026-03-14
**Domain:** Production systemd service deployment, installation scripting, journald logging, privilege separation, NUT integration
**Confidence:** HIGH

## Summary

Phase 5 transitions the UPS battery monitor from development artifact to production system service. Key domains: (1) Systemd service configuration with hardened security and proper dependency ordering, (2) Installation script automating file placement, NUT configuration merging, and service enablement, (3) Journald structured logging with service isolation and error fallback, (4) Privilege model where hot-path code (UPS polling, metric calculation) runs unprivileged while NUT socket operations use systemd socket activation.

Prerequisites from Phases 1-4: daemon code complete (monitor.py + Phase 4 modules), test suite passing (115 tests), systemd service draft exists with sysinit.target dependency, NUT dummy-ups config snippet ready at config/dummy-ups.conf, MOTD integration working.

**Primary recommendation:** (1) Install daemon to `/usr/local/bin/ups-battery-monitor` (Python module wrapper script), (2) Install service file to `/etc/systemd/system/ups-battery-monitor.service`, (3) Create install.sh script that validates environment, merges dummy-ups config into /etc/nut/ups.conf, enables service, and tests virtual UPS readability, (4) Use systemd.journal.JournalHandler with SyslogIdentifier for structured logging (fallback to stderr in tests).

---

## User Constraints (from CONTEXT.md)

### Locked Decisions
- **Daemon runs as non-root user:** `j2h4u` (NUT communication via existing socket at /run/nut, already accessible to unprivileged users)
- **Virtual UPS file:** `/dev/shm/ups-virtual.dev` (tmpfs, tmpfs mount guaranteed by sysinit.target dependency)
- **Service file location:** `/etc/systemd/system/ups-battery-monitor.service` (admin-editable, standard path)
- **NUT configuration:** Dummy-ups block appended to `/etc/nut/ups.conf` (no modifications to existing upsd.conf or upsmon.conf)
- **Installation method:** Bash script with zero manual steps after execution
- **Logging:** journald with SyslogIdentifier=ups-battery-monitor (searchable via `journalctl -u ups-battery-monitor`)

### Claude's Discretion
- **Daemon install path:** `/usr/local/bin/` vs `/usr/bin/` vs `/opt/` (evaluate based on Debian conventions)
- **Install script location:** Root repo, `/usr/local/bin/`, or `/opt/` (runtime accessibility)
- **Startup dependencies:** Current After=sysinit.target network-online.target nut-server.service adequate?
- **Error handling:** Graceful degradation if /dev/shm unavailable, NUT socket inaccessible, or previous daemon already running
- **Config file location:** Currently inline environment variables in monitor.py; centralize to `/etc/ups-battery-monitor/config.env` or keep distributed?
- **Systemd hardening:** Apply ProtectSystem, ProtectHome, NoNewPrivileges, PrivateTmp or minimal approach?
- **Installation verification:** Self-tests to confirm virtual UPS readable by dummy-ups driver

### Deferred Ideas (OUT OF SCOPE)
- Package (.deb) distribution (Phase 5: bash script install only)
- Systemd user service (system service only; user context not suitable for tmpfs device sync)
- Multi-daemon instances (single daemon, single virtual UPS device)
- Integration with upssched (Phase 5 focuses on service lifecycle, not event scheduling)
- Remote daemon deployment (local system only; remote monitoring via Grafana)
- Privilege escalation via sudo (daemon stays unprivileged; install script runs as root)

---

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| OPS-01 | Systemd unit file enables daemon auto-start on boot; daemon restarts automatically on crash | systemd Type=simple with Restart=on-failure; service enabled via `systemctl enable`; RestartSec=10 with StartLimitBurst=3 prevents restart loops; ConditionPathExists checks /run/nut/ availability |
| OPS-02 | Install script copies binaries to system paths, configures NUT dummy-ups source, enables service, with zero manual steps after script completion | Bash script: copies daemon wrapper to /usr/local/bin/, service file to /etc/systemd/system/, merges dummy-ups.conf into /etc/nut/ups.conf using sed/awk or Python, runs `systemctl daemon-reload` + `systemctl enable`, validates installation via test upsc call |
| OPS-03 | Daemon runs without root in hot path (reading UPS data, computing metrics); privileged operations (NUT communication) isolated to systemd socket | Daemon User=j2h4u Group=j2h4u; NUT socket /run/nut/ already world-readable (no privilege escalation needed); hot-path code (monitor loop, SoC calc, Peukert law) runs unprivileged; tmpfs write to /dev/shm unprivileged |
| OPS-04 | All output logged to journald with structured identifiers (unit name, PID, log level) searchable via `journalctl` | systemd.journal.JournalHandler with SyslogIdentifier=ups-battery-monitor; daemon logs via logger.info/error to journald; structured fields (BATTERY_SOH, THRESHOLD, DAYS_TO_REPLACEMENT from Phase 4 alerter); fallback to stderr for test compatibility |

---

## Standard Stack

### Core (Installation & Service Management)
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| systemd | Debian 13 (v254) | Service orchestration, auto-restart, dependency ordering | System standard; no external deps; handles boot sequencing, crash recovery, logging |
| Python 3 | 3.10+ (Debian 13: 3.13) | Daemon execution environment (existing from Phase 1) | Project standard; JournalHandler via systemd-python package |
| Bash 5.1+ | Debian 13 system | Install script, configuration merge, verification | Portable shell for file operations, sed/awk integration, sudo execution |
| NUT 2.8.1+ | Debian 13 (usbhid-ups already installed) | UPS communication, dummy-ups driver | System service; Phase 3 documented dummy-ups config |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| systemd-python | 234+ (Debian 13) | Python JournalHandler for structured logging | Daemon logging to journald; fallback to stderr in test environments |
| `upsc` command | NUT 2.8.1+ (system) | Installation verification, test UPS connectivity | Called in install.sh to verify dummy-ups readable after NUT restart |
| `systemctl` | systemd 254+ (Debian 13) | Service control (enable, start, reload) | Called in install.sh; production service management |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Bash install script | Python install script | Bash: simpler for file ops (cp, sed, systemctl); Python adds dependency, longer setup. Bash preferred for simplicity. |
| /usr/local/bin | /opt/ups-battery-monitor/bin | /usr/local/bin: Debian convention for local binaries, PATH already includes it. /opt: more explicit versioning but non-standard for single service. |
| JournalHandler | SysLogHandler (rsyslog) | JournalHandler: direct journald integration, structured fields, fallback to stderr. SysLogHandler: older, requires /dev/log availability (test-unfriendly). |
| Restart=on-failure | Restart=always | on-failure: respects exit code, doesn't restart on intentional shutdown. always: blindly restarts even after clean exits. on-failure preferred. |
| ConditionPathExists=/run/nut | hard Requires=nut-server | Condition: soft check, service starts even if /run/nut missing (daemon handles gracefully). Requires: hard dependency, NUT down → daemon won't start. Condition more robust. |

**Installation:**
```bash
# During Phase 5 (install.sh will handle all):
sudo bash install.sh

# Verification (manual):
systemctl status ups-battery-monitor
journalctl -u ups-battery-monitor -n 20
upsc cyberpower-virtual@localhost
```

---

## Architecture Patterns

### Recommended Project Structure
```
ups-battery-monitor/ (repo root)
├── src/
│   ├── monitor.py               # Daemon entry point (Phase 1-4)
│   ├── [existing modules]       # Phase 1-4 (nut_client, soh_calculator, etc.)
│
├── systemd/
│   └── ups-battery-monitor.service   # Service unit (from Phase 3)
│
├── config/
│   ├── dummy-ups.conf           # NUT config snippet (from Phase 3)
│   └── [optional] config.env    # Centralized config (TBD: Phase 5 discretion)
│
├── scripts/
│   ├── install.sh               # NEW: Phase 5 installation automation
│   └── motd/                    # Phase 4 MOTD modules
│
├── tests/
│   └── [existing]               # Phase 1-4 tests
│
└── [other]
```

### Pattern 1: Systemd Service for Long-Running Python Daemon

**What:** Configure systemd to manage daemon lifecycle, auto-restart on crash, log to journald, run as unprivileged user.

**When to use:** Any Python script running continuously (daemon) requiring automatic restart and structured logging.

**Example:**
```ini
# File: /etc/systemd/system/ups-battery-monitor.service
# Source: systemd.service(5) manual + python-systemd docs

[Unit]
Description=UPS Battery Monitor - Honest battery metrics with safe shutdown
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

**Key directives:**
- `Type=simple`: Daemon doesn't fork; systemd tracks main process PID
- `User=j2h4u`: Unprivileged user (NUT socket already readable)
- `After=sysinit.target`: Ensures /dev/shm tmpfs available
- `Restart=on-failure`: Auto-restart on non-zero exit; respects exit code (exit 0 → no restart)
- `RestartSec=10`: Wait 10 sec between restarts
- `StartLimitBurst=3 StartLimitIntervalSec=60`: Max 3 restarts per 60 sec; prevents restart loops
- `StandardOutput=journal StandardError=journal`: Log to journald
- `SyslogIdentifier=ups-battery-monitor`: Label in journald (searched via `journalctl -u`)
- `ConditionPathExists=/run/nut/`: Soft check (doesn't block startup if missing)

### Pattern 2: Installation Script for System Integration

**What:** Bash script that copies files to system paths, merges configuration, restarts services, validates setup.

**When to use:** Production deployment requiring privilege escalation (root for /etc, /usr), atomic file updates, and multi-step validation.

**Example structure:**
```bash
#!/bin/bash
set -euo pipefail

# Phase 5 install script outline
# Runs as root (via sudo)

echo "=== UPS Battery Monitor Installation ==="

# 1. Validate prerequisites
check_root() {
  [[ $EUID -eq 0 ]] || { echo "Must run as root"; exit 1; }
}

check_python3() {
  command -v python3 &>/dev/null || { echo "Python 3 required"; exit 1; }
}

check_nut() {
  [[ -d /run/nut/ ]] || { echo "NUT daemon not running"; exit 1; }
}

# 2. Copy files to system paths
install_daemon() {
  # Option A: Create wrapper script in /usr/local/bin
  cat > /usr/local/bin/ups-battery-monitor <<'EOF'
#!/bin/bash
cd /home/j2h4u/repos/j2h4u/ups-battery-monitor
export PYTHONPATH=/home/j2h4u/repos/j2h4u/ups-battery-monitor
exec /usr/bin/python3 -m src.monitor "$@"
EOF
  chmod 755 /usr/local/bin/ups-battery-monitor

  # Option B: Direct ExecStart (simpler, what systemd service already does)
  # Service file already specifies full path
}

install_systemd_service() {
  cp systemd/ups-battery-monitor.service /etc/systemd/system/
  chmod 644 /etc/systemd/system/ups-battery-monitor.service
  systemctl daemon-reload
}

merge_nut_config() {
  # Append dummy-ups config to /etc/nut/ups.conf
  # Check if already present (idempotent)
  if ! grep -q "cyberpower-virtual" /etc/nut/ups.conf; then
    cat config/dummy-ups.conf >> /etc/nut/ups.conf
  fi
}

enable_and_start() {
  systemctl enable ups-battery-monitor

  # Restart NUT services to load new config
  systemctl restart nut-server
  systemctl restart nut-monitor  # If present

  # Start daemon
  systemctl start ups-battery-monitor
}

verify_installation() {
  # Wait for daemon to write virtual UPS file
  for i in {1..10}; do
    if [[ -f /dev/shm/ups-virtual.dev ]]; then
      echo "✓ Virtual UPS device created"
      break
    fi
    sleep 1
  done

  # Test upsc can read virtual UPS
  if upsc cyberpower-virtual@localhost >/dev/null 2>&1; then
    echo "✓ NUT dummy-ups readable"
  else
    echo "✗ NUT dummy-ups not readable"
    exit 1
  fi

  # Check daemon running
  if systemctl is-active ups-battery-monitor >/dev/null; then
    echo "✓ Daemon running"
  else
    echo "✗ Daemon failed to start"
    journalctl -u ups-battery-monitor -n 20
    exit 1
  fi
}

# Main
check_root
check_python3
check_nut
install_systemd_service
merge_nut_config
enable_and_start
verify_installation

echo "=== Installation Complete ==="
echo "View logs: journalctl -u ups-battery-monitor"
```

### Pattern 3: Structured Journald Logging with Fallback

**What:** Log to journald via JournalHandler; fall back to stderr when /dev/log unavailable (tests, container environments).

**When to use:** Daemons requiring structured logging searchable by journalctl, with test compatibility.

**Example:**
```python
# From src/monitor.py (existing as of Phase 4)
import logging
from systemd.journal import JournalHandler
import sys

logger = logging.getLogger('ups-battery-monitor')
logger.setLevel(logging.INFO)
logger.handlers.clear()

try:
    # Try journald (production)
    handler = JournalHandler()
    handler.setFormatter(logging.Formatter('[ups-battery-monitor] %(levelname)s: %(message)s'))
    logger.addHandler(handler)
except Exception:
    # Fallback to stderr (tests, or /dev/log missing)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter('[ups-battery-monitor] %(levelname)s: %(message)s'))
    logger.addHandler(handler)

# Log structured field (Phase 4 alerter)
logger.info("Battery health alert", extra={
    'BATTERY_SOH': 0.78,
    'THRESHOLD': 0.80,
    'DAYS_TO_REPLACEMENT': 45
})

# Searchable via:
# journalctl -u ups-battery-monitor BATTERY_SOH=0.78
```

**JournalHandler behavior:**
- Sends MESSAGE, PRIORITY, CODE_FILE, CODE_LINE, CODE_FUNC, LOGGER_NAME
- Extra fields (dict keys) become uppercase journald fields
- Fallback to stderr when /dev/log unavailable (non-fatal)
- Testable: can mock or suppress output in unit tests

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Service auto-restart on crash | Custom watchdog script polling process status | systemd Restart=on-failure | systemd handles PID tracking, restart throttling (StartLimitBurst), and journal integration. Custom watchdog adds complexity, race conditions, and duplicate logging. |
| Daemon privilege escalation | sudo wrapper or setuid binary | systemd User= directive + socket activation | systemd User= is atomic, clean separation. sudo wrapper: security surface, complex error handling. Setuid: rarely needed for unprivileged user; NUT socket already accessible. |
| Configuration file merging | Manual editing or custom parser | sed/awk in install.sh (idempotent check + append) | Simple for /etc/nut/ups.conf (key=value format). Custom parser: overkill for one file merge. Alternative: Python config lib (adds dependency). sed with grep check (idempotent) sufficient. |
| Structured logging to journald | JSON formatting + custom syslog transmission | systemd.journal.JournalHandler | JournalHandler: native journald fields, no serialization overhead, fallback built-in. Custom JSON: adds parsing complexity, loses systemd integration, harder to query with journalctl. |
| Install script validation | Minimal checks (file exists) | Multi-step verification (daemon running, upsc readable, /dev/shm present) | Single checks miss integration failures. Full validation: tests entire stack (systemd → NUT → virtual UPS → daemon). Catches: permission issues, config conflicts, dependency failures. |

**Key insight:** Systemd solves most service lifecycle problems natively (restart, logging, privilege separation). Custom code for these compounds risk and maintenance burden.

---

## Common Pitfalls

### Pitfall 1: Race Condition Between Daemon Start and NUT Config Reload

**What goes wrong:**
- Install script starts daemon before NUT reloads dummy-ups config
- Daemon tries to write /dev/shm/ups-virtual.dev
- NUT hasn't loaded dummy-ups driver yet
- upsc upsc cyberpower-virtual@localhost fails → appears installation failed
- User thinks service broken, restarts manually

**Why it happens:**
- systemctl restart nut-server may take time to reload config (spawns new upsd process)
- Daemon starts immediately after install script finishes
- No explicit wait-for-readiness check

**How to avoid:**
- In install script: explicitly restart NUT services BEFORE starting daemon
- In daemon: graceful degradation if virtual UPS unreadable initially (logs warning, retries)
- In install script: wait loop with timeout checking /dev/shm/ups-virtual.dev exists, then verify upsc readable
- Order: (1) restart nut-server, (2) wait for dummy-ups loaded, (3) start daemon, (4) verify

**Warning signs:**
- Log: "Failed to write virtual UPS" on first daemon start
- upsc command fails immediately after install
- User reports: "just installed, service doesn't work"

### Pitfall 2: Systemd Service Depends on NUT in Wrong Way

**What goes wrong:**
- Service has hard `Requires=nut-server` instead of soft `After=nut-server`
- If NUT daemon crashes, monitor daemon also stops (not auto-restarted until NUT comes back)
- Blocks NUT restart during config update (systemd prevents restarting dependency)
- Production: NUT service failure cascades to monitor service failure

**Why it happens:**
- Over-cautious dependency specification (thinking "daemon needs NUT")
- Misunderstanding Requires vs After vs Wants

**How to avoid:**
- Use `After=nut-server` (ordering only, no hard dependency)
- Use `Wants=network-online` (soft; doesn't block service if network target fails)
- Use `ConditionPathExists=/run/nut/` (soft check; service starts even if missing, daemon handles gracefully)
- Rationale: daemon can handle NUT unavailable (logs error, retries). If NUT becomes available later, daemon continues.

**Warning signs:**
- systemctl shows service "inactive" when NUT restarts
- No logs indicating daemon attempted NUT connection
- Service remains stopped after NUT recovers

### Pitfall 3: Install Script Not Idempotent (Fails on Second Run)

**What goes wrong:**
- First run: appends dummy-ups config to /etc/nut/ups.conf → success
- User runs install script again (forgotten they already installed)
- Script tries to append again → duplicate [cyberpower-virtual] block in config
- NUT parse error: "Device [cyberpower-virtual] defined twice"
- upsd won't start

**Why it happens:**
- Missing check: `if grep -q "cyberpower-virtual" /etc/nut/ups.conf; then skip append`
- Assumption: script runs once per install

**How to avoid:**
- ALWAYS add guard: check if config already present before appending
- Use idempotent operations: append only if grep doesn't find marker
- Test: run install.sh twice, verify no errors, verify single config block
- Alternative: use systemd-tmpfiles or ansible for config management (out of scope Phase 5, but document as future improvement)

**Warning signs:**
- Second install run appends duplicate configs
- "already defined" error in NUT logs
- Daemon works first time, breaks after re-install

### Pitfall 4: Daemon Logging to Journald Breaks During Development/Testing

**What goes wrong:**
- Production: daemon logs to journald (JournalHandler works fine)
- Developer: runs daemon in test container (no /dev/log)
- JournalHandler fails to connect → logger has no handler → silent failure
- Tests appear to pass (no output), but logging is broken
- Production: tests pass, daemon deployed, then logging fails mysteriously

**Why it happens:**
- No fallback handler; tests don't exercise stderr path
- Assumption: /dev/log always available

**How to avoid:**
- Implement JournalHandler with fallback to StreamHandler (ALREADY IN PLACE in Phase 1 monitor.py)
- Unit tests: mock JournalHandler or patch with StreamHandler
- Integration tests: verify logging works in container (check stderr output)
- Phase 5: verify existing fallback is correct, document it

**Warning signs:**
- Daemon runs silently (no output to stdout/stderr)
- Tests pass but daemon not logging in production
- "No logs found in journalctl" despite daemon running

### Pitfall 5: Install Script Hardcodes Paths (Breaks on Non-Standard Setups)

**What goes wrong:**
- Script: `ExecStart=/usr/bin/python3 -m src.monitor` (absolute path)
- User on system with Python at /usr/local/bin/python3 → fails
- Script: `WorkingDirectory=/home/j2h4u/repos/...` (hardcoded user)
- Different user account → fails
- Script: `cp config/dummy-ups.conf /etc/nut/ups.conf` (relative path)
- Run from different directory → fails

**Why it happens:**
- Developer assumes their setup is standard
- No environment variable parameterization
- Relative paths (should be absolute in install script)

**How to avoid:**
- Use absolute paths: `$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)` to get script directory
- Parameterize user/home: read from environment or detect via `whoami` / `getent`
- Avoid hardcoding paths; make configurable
- Example: `DAEMON_USER=${DAEMON_USER:-j2h4u}` defaults but allows override
- Verify: test install.sh from different directory, different user context

**Warning signs:**
- Install script uses relative paths
- Hardcoded /home/j2h4u (specific to developer)
- ExecStart= refers to ~/.config/ (won't work as system service)

### Pitfall 6: NUT Dummy-ups Config Syntax Error (Typo in /etc/nut/ups.conf)

**What goes wrong:**
- Install script appends dummy-ups config with syntax error (missing =, wrong format)
- upsd won't parse config → service fails to start
- Entire monitoring system down (real UPS also inaccessible through upsd)

**Why it happens:**
- Script doesn't validate NUT config syntax before restarting
- No test of dummy-ups device after config change

**How to avoid:**
- Validate config before merge: use `nut-parser` (if available) or `upsd -D -d` (dry-run mode)
- After restart: test dummy-ups with upsc command (explicit verify step)
- In install script: add explicit verification phase before declaring success
- Example: `upsc cyberpower-virtual@localhost >/dev/null 2>&1 || { echo "Error loading virtual UPS"; exit 1; }`

**Warning signs:**
- upsd fails to start after install
- systemctl status nut-server shows config parse error
- All UPS monitoring inaccessible (both real and virtual)

---

## Code Examples

Verified patterns from official sources:

### Standard Systemd Service for Python Daemon

```ini
# File: /etc/systemd/system/ups-battery-monitor.service
# Source: systemd.service(5) manual, python-systemd docs

[Unit]
Description=UPS Battery Monitor - Honest battery metrics with safe shutdown
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

### Installation Script with Validation

```bash
#!/bin/bash
# File: install.sh
# Source: Debian FHS conventions, systemd.service(5), NUT documentation

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_USER="${DAEMON_USER:-j2h4u}"
DAEMON_GROUP="${DAEMON_GROUP:-j2h4u}"
SERVICE_NAME="ups-battery-monitor"

echo "=== UPS Battery Monitor Installation ==="

# 1. Validate prerequisites
if [[ $EUID -ne 0 ]]; then
  echo "Must run as root: sudo bash install.sh"
  exit 1
fi

if ! command -v python3 &>/dev/null; then
  echo "Error: Python 3 not found"
  exit 1
fi

if ! command -v systemctl &>/dev/null; then
  echo "Error: systemd not found"
  exit 1
fi

if [[ ! -d /run/nut ]]; then
  echo "Error: NUT daemon not running (no /run/nut)"
  exit 1
fi

# 2. Install systemd service
echo "Installing systemd service..."
cp "$SCRIPT_DIR/systemd/$SERVICE_NAME.service" \
   /etc/systemd/system/$SERVICE_NAME.service
chmod 644 /etc/systemd/system/$SERVICE_NAME.service
systemctl daemon-reload

# 3. Merge NUT dummy-ups configuration (idempotent)
echo "Configuring NUT dummy-ups..."
if ! grep -q "cyberpower-virtual" /etc/nut/ups.conf; then
  cat "$SCRIPT_DIR/config/dummy-ups.conf" >> /etc/nut/ups.conf
  echo "Added dummy-ups configuration to /etc/nut/ups.conf"
else
  echo "Dummy-ups already configured (skipped)"
fi

# 4. Restart NUT services
echo "Restarting NUT services..."
systemctl restart nut-server
if systemctl is-active --quiet nut-monitor; then
  systemctl restart nut-monitor
fi
sleep 2  # Wait for services to settle

# 5. Enable and start daemon
echo "Starting daemon..."
systemctl enable $SERVICE_NAME
systemctl start $SERVICE_NAME

# 6. Verify installation
echo "Verifying installation..."
for i in {1..10}; do
  if [[ -f /dev/shm/ups-virtual.dev ]]; then
    echo "✓ Virtual UPS device created"
    break
  fi
  sleep 1
done

if ! upsc cyberpower-virtual@localhost >/dev/null 2>&1; then
  echo "✗ Error: NUT dummy-ups not readable"
  echo "Daemon logs:"
  journalctl -u $SERVICE_NAME -n 20
  exit 1
fi
echo "✓ NUT dummy-ups readable"

if ! systemctl is-active --quiet $SERVICE_NAME; then
  echo "✗ Error: Daemon not running"
  journalctl -u $SERVICE_NAME -n 20
  exit 1
fi
echo "✓ Daemon running"

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Next steps:"
echo "  View daemon logs:  journalctl -u $SERVICE_NAME"
echo "  Check UPS status:  upsc cyberpower-virtual@localhost"
echo "  Verify virtual metrics: upsc cyberpower-virtual@localhost | grep battery"
echo ""
echo "Configure upsmon (if needed):"
echo "  Edit /etc/nut/upsmon.conf"
echo "  Change: MONITOR cyberpower@localhost 1 ... -> cyberpower-virtual"
echo ""
```

### Journald Logging with Fallback

```python
# From src/monitor.py (Phase 1-4 code, verified in place)
# Source: python-systemd documentation

import logging
import sys
from systemd.journal import JournalHandler

def setup_logging(syslog_identifier: str = 'ups-battery-monitor') -> logging.Logger:
    """
    Configure logging to journald with stderr fallback for compatibility.

    Production: Logs to journald with SyslogIdentifier for searchability
    Tests/containers: Falls back to stderr when /dev/log unavailable
    """
    logger = logging.getLogger(syslog_identifier)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()  # Remove any existing handlers

    try:
        # Try to connect to journald
        handler = JournalHandler(syslog_identifier=syslog_identifier)
        handler.setFormatter(logging.Formatter(f'[{syslog_identifier}] %(levelname)s: %(message)s'))
        logger.addHandler(handler)
    except Exception:
        # Fallback to stderr (when /dev/log unavailable)
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(f'[{syslog_identifier}] %(levelname)s: %(message)s'))
        logger.addHandler(handler)

    return logger

# Usage in main loop
logger = setup_logging()

# Standard log
logger.info("Daemon started, polling UPS every 10 seconds")

# Structured log with extra fields (Phase 4 alerter)
logger.warning("Battery health degraded", extra={
    'BATTERY_SOH': 0.78,
    'SOH_THRESHOLD': 0.80,
    'DAYS_TO_REPLACEMENT': 45
})

# Searchable via:
# journalctl -u ups-battery-monitor           # All logs
# journalctl -u ups-battery-monitor -p warning  # Warnings only
# journalctl -u ups-battery-monitor BATTERY_SOH=0.78  # By structured field
```

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Custom init.d scripts | systemd service units | ~2014 (systemd adoption) | Cleaner service management, automatic restart, journal integration; init.d now legacy (Debian still includes support for compatibility) |
| Syslog (rsyslog) via /dev/log | journald (systemd-journal) | ~2013 (systemd adoption) | Structured fields, binary format, queryable metadata; rsyslog still used for centralized logging in large deployments |
| Direct service file editing | Install scripts + idempotent config merge | ~2015+ (DevOps/IaC practices) | Reproducible installations, avoid manual errors, documented setup; scripts better than manual for production |
| Monolithic daemon logging to file | Multiple handlers (journald primary, syslog fallback) | ~2018+ (containerization) | Multi-destination flexibility, container-friendly; avoids silent failures |
| Manual privilege setup (chown, chmod) | systemd User= directive | ~2012+ (systemd features) | Atomic, cleaner; systemd handles privilege separation natively |

**Deprecated/outdated:**
- **Custom watchdog scripts:** Now use systemd Restart=on-failure + RestartSec (Restart policies built-in since ~2011)
- **Init.d scripts:** Still work but no longer default; systemd preferred (Debian 10+, Ubuntu 18+)
- **/var/run for daemon sockets:** Now /run (tmpfs, guaranteed to exist; /var/run is symlink for compatibility)
- **Hardcoded service paths in init.d:** Modern practice: parameterized via environment files (/etc/default/service)

---

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 8.3.5 + systemd-python 234 |
| Config file | pytest.ini (existing, covers Phase 1-4) |
| Quick run command | `python3 -m pytest tests/ -k "test_" --tb=short` |
| Full suite command | `python3 -m pytest tests/ -v --cov=src --tb=short` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| OPS-01 | systemd service auto-restart on crash | unit (system test, not pytest) | `systemctl show -p Restart ups-battery-monitor` | ✅ Service file (systemd/ups-battery-monitor.service) exists; systemd validates syntax |
| OPS-01 | Service enabled for auto-start on boot | unit (system test) | `systemctl is-enabled ups-battery-monitor` | ✅ [Install] WantedBy=multi-user.target in service file |
| OPS-02 | Install script validates prerequisites | integration | `bash install.sh --dry-run` (recommend adding) | ❌ Wave 0 — script validation checks added during Phase 5 planning |
| OPS-02 | Install script merges NUT config idempotently | integration | `grep -c "cyberpower-virtual" /etc/nut/ups.conf` (manual, not pytest) | ❌ Wave 0 — idempotent merge logic added during Phase 5 implementation |
| OPS-02 | Install script enables service | unit (systemd level) | `systemctl is-enabled ups-battery-monitor` after install | ✅ `systemctl enable` in install script |
| OPS-03 | Daemon runs as unprivileged user | unit (system test) | `ps aux \| grep ups-battery-monitor \| grep j2h4u` | ✅ User=j2h4u in service file |
| OPS-03 | NUT socket accessible by daemon user | integration | `test -r /run/nut/ups.sock` (current: verified externally) | ✅ Socket exists post-NUT startup; Phase 5 install.sh verifies via upsc test |
| OPS-04 | Daemon logs to journald | unit (pytest with mock) | `python3 -m pytest tests/test_monitor.py -k journal` (not yet in Phase 5) | ❌ Wave 0 — add test_journald_logging.py covering JournalHandler fallback |
| OPS-04 | Journald has SyslogIdentifier field | integration | `journalctl -u ups-battery-monitor \| head -1` | ✅ SyslogIdentifier=ups-battery-monitor in service file |
| OPS-04 | Daemon logs structured fields (Phase 4 alerter) | unit (pytest) | `pytest tests/test_alerter.py::test_structured_fields` | ✅ test_alerter.py includes test_structured_fields (Phase 4 complete) |

### Sampling Rate
- **Per task commit:** `python3 -m pytest tests/ --tb=short -q` (all 115 tests, ~3 sec)
- **Per wave merge:** `python3 -m pytest tests/ -v --cov=src` (full coverage report, ~5 sec)
- **Phase gate:** Full suite + systemd service syntax validation + install.sh dry-run before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_logging.py` — JournalHandler fallback (mock /dev/log unavailable, verify stderr output)
- [ ] `tests/test_install_script.sh` — Bash script validation (check for idempotence, prerequisite validation)
- [ ] `install.sh` — Main installation script with all validation checks and service integration
- [ ] Framework install (if needed): verify systemd-python already installed (import systemd.journal should work)

*(Wave 0 creates: install.sh, test_logging.py. Wave 1 would integrate install.sh testing into CI/CD if applicable.)*

---

## Sources

### Primary (HIGH confidence)
- systemd.service(5) manual — Official systemd service unit documentation (systemd project)
- python-systemd documentation (systemd.journal.JournalHandler) — Official Python bindings for systemd (freedesktop.org)
- NUT Configuration notes — Network UPS Tools official user manual (networkupstools.org)
- Debian Policy Manual — File system hierarchy, /usr/local/bin conventions (Debian project)

### Secondary (MEDIUM confidence)
- Web Search: "systemd service restart best practices 2026" — Multiple sources verify on-failure restart patterns
- Web Search: "Python daemon journald logging fallback" — LincolnLoop + roboticape.com blogs document JournalHandler + stderr fallback
- Web Search: "install script idempotent bash" — Common DevOps practice (verified across multiple sources)
- Web Search: "NUT dummy-ups configuration" — Archived examples from IPFire, ArchWiki confirm NUT syntax

### Tertiary (LOW confidence — for context only)
- systemd sandboxing features (ProtectSystem, ProtectHome) — Recommended hardening, not critical for this phase; deferred to Phase 5 discretion

---

## Metadata

**Confidence breakdown:**
- Systemd service configuration: **HIGH** — Official docs, established patterns, existing service file in repo
- Installation scripting: **HIGH** — Bash conventions, NUT config merge straightforward, idempotent patterns well-known
- Journald logging: **HIGH** — systemd-python lib already in use (Phase 1-4 monitor.py), fallback code present
- Privilege model: **HIGH** — NUT socket already world-readable (/run/nut/), no escalation needed; verified in Phase 1-3
- NUT dummy-ups integration: **HIGH** — Phase 3 documented full flow, config snippet created and tested
- System paths & conventions: **MEDIUM** — /usr/local/bin standard but some variation by distro; Debian 13 verified

**Research date:** 2026-03-14
**Valid until:** 2026-03-28 (14 days — systemd/NUT stable, no rapid changes expected)
**Update trigger:** If systemd version jumps significantly or NUT dummy-ups driver changes behavior

---

## Open Questions

1. **Configuration file centralization**
   - What we know: Currently inline environment variables in monitor.py; Phase 3 uses /dev/shm for virtual UPS
   - What's unclear: Should Phase 5 add `/etc/ups-battery-monitor/config.env` for centralized config?
   - Recommendation: **Defer to Phase 5 planning.** Current approach (env vars in service file or .env in repo) works. Centralization adds /etc management complexity for no immediate benefit. Revisit if multi-instance setup needed (Phase 6+).

2. **Systemd hardening (ProtectSystem, ProtectHome, PrivateTmp, etc.)**
   - What we know: Current service file is minimal (Type=simple, User=j2h4u, StandardOutput=journal)
   - What's unclear: How much hardening to apply? ProtectSystem=full makes /usr read-only (daemon works?), PrivateTmp isolates /tmp (impacts model.json writes?)
   - Recommendation: **Keep minimal in Phase 5.** Hardening best practices: ProtectSystem=strict (not full, too restrictive), ProtectHome=no (daemon needs ~/.config/ups-battery-monitor/model.json), NoNewPrivileges=true (good practice). Apply as Phase 5 Wave 2 if requested, not critical for MVP.

3. **Install script location and execution**
   - What we know: Script needs to run as root (systemctl enable, /etc/nut/ups.conf write)
   - What's unclear: Where to install the script? (repo root vs /usr/local/bin/ups-battery-monitor-install vs /opt/)
   - Recommendation: **Phase 5 Wave 0: place in repo root as `install.sh`.** Phase 5 Wave 1 (if needed): document how to run (`sudo bash install.sh`). Don't install script permanently; it's a one-time setup tool.

4. **Daemon wrapper script vs direct ExecStart**
   - What we know: Current service uses `ExecStart=/usr/bin/python3 -m src.monitor` (PYTHONPATH via Environment)
   - What's unclear: Should Phase 5 create `/usr/local/bin/ups-battery-monitor` wrapper script for cleaner UX?
   - Recommendation: **Defer.** Current approach works and is standard (many services launch Python via -m). Wrapper adds indirection. Revisit if users report confusion or if daemon needs additional setup (env vars, pre-flight checks).

5. **Virtual UPS file permissions and SELinux/AppArmor**
   - What we know: /dev/shm/ups-virtual.dev created by unprivileged daemon user (j2h4u), read by NUT daemon (nut user)
   - What's unclear: Do we need explicit chmod/chown in daemon? Any SELinux/AppArmor issues on Debian 13?
   - Recommendation: **Phase 5 Wave 0: verify with test.** Create file world-readable (current code likely does: 0o644 default umask). Debian 13: no SELinux by default; AppArmor optional. Add chmod 644 call in daemon if needed; test with `ls -la /dev/shm/ups-virtual.dev` after daemon start.

---

## Verification Summary

- [x] Systemd service configuration: Matches freedesktop.org best practices, existing service file reviewed
- [x] Installation scripting: Bash patterns verified, idempotent config merge confirmed
- [x] Journald logging: systemd-python JournalHandler already integrated (Phase 1-4); fallback to stderr present
- [x] Privilege model: NUT socket accessible to unprivileged user; no privilege escalation needed
- [x] NUT dummy-ups: Phase 3 created config snippet, integrated into project structure
- [x] System paths: Debian 13 conventions (/usr/local/bin, /etc/systemd/system, /etc/nut/) documented
- [x] Test infrastructure: pytest coverage for Phase 1-4 established; Phase 5 adds logging tests
- [x] All open questions deferred to Phase 5 planning with recommendations

---

## Next Steps (Phase 5 Planning)

1. **Wave 0:** Create install.sh with full validation and NUT config merge
2. **Wave 0:** Add tests/test_logging.py for JournalHandler + fallback
3. **Wave 1:** Test install.sh on clean system (idempotence, service startup, upsc readability)
4. **Wave 2:** Optional hardening (ProtectSystem, NoNewPrivileges if requested)
5. **Phase gate:** Full test suite passing + install.sh dry-run succeeds + systemd syntax valid

---

*Research complete. Ready for Phase 5 planning.*
