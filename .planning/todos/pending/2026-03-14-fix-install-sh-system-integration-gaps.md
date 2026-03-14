---
created: 2026-03-14T12:36:27.955Z
title: Fix install.sh system integration gaps
area: tooling
files:
  - scripts/install.sh
  - /etc/systemd/system/nut-exporter.service
  - /etc/alloy/config.alloy
---

## Problem

install.sh only handles its own components (systemd service, dummy-ups config, upsmon switchover) but doesn't audit or update other system components that need to point at the virtual UPS:

1. **nut_exporter** — still scrapes ambiguous default (both cyberpower and cyberpower-virtual). Needs `?ups=cyberpower-virtual` query param in scrape URL. Was manually fixed in `/etc/alloy/config.alloy` (`metrics_path` with query string).

2. **Alloy config** — `prometheus.scrape "nut_metrics"` needs `metrics_path = "/ups_metrics?ups=cyberpower-virtual"` instead of bare `/ups_metrics`. Not touched by installer.

3. **MOTD** — installer copies `51-ups-health.sh` but doesn't remove old `51-ups.sh`, causing duplicate status lines. Was manually deleted.

## Solution

Add to install.sh:
- Detect and update nut_exporter service file (add query param for virtual UPS)
- Detect and update Alloy config (switch metrics_path)
- Remove old `51-ups.sh` if `51-ups-health.sh` is being installed (idempotent)
- General principle: installer should audit system for components that reference `cyberpower` and offer to switch them to `cyberpower-virtual`
