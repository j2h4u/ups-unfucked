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

Split into two scripts with different audiences:

### 1. `scripts/install.sh` — product installer (universal, ships with repo)
For any user installing ups-battery-monitor on their system:
- systemd service + dummy-ups config
- upsmon switchover to virtual UPS
- MOTD health script (replace old `51-ups.sh` if exists)
- No assumptions about specific monitoring stack

### 2. `scripts/deploy-senbonzakura.sh` — site-specific deploy (our server only, not shipped)
For our specific senbonzakura setup:
- Calls `install.sh` first
- Patches nut_exporter query param (`?ups=cyberpower-virtual`)
- Patches Alloy config (`metrics_path` with virtual UPS)
- Any other senbonzakura-specific integrations
- Lives in repo but documented as site-specific, not universal

## Lessons Learned (2026-03-14)

### nut_exporter не поддерживает выбор UPS через CLI флаг
- `--nut.ups_name` не существует (проверено: `nut_exporter --help`)
- При наличии двух UPS (cyberpower + cyberpower-virtual) exporter отказывается работать: `Multiple UPS devices were found by NUT for this scrape`
- Решение: query string parameter в URL скрейпа: `/ups_metrics?ups=cyberpower-virtual`
- Это патчится на стороне **scraper'а** (Alloy config), не на стороне exporter'а

### Alloy config: metrics_path с query string
- Файл: `/etc/alloy/config.alloy`
- Было: `metrics_path = "/ups_metrics"`
- Стало: `metrics_path = "/ups_metrics?ups=cyberpower-virtual"`
- Секция: `prometheus.scrape "nut_metrics"`
- Source of truth для Alloy config: `~/repos/j2h4u/grafana-config/alloy-config.alloy`

### MOTD дубли
- install.sh копировал `51-ups-health.sh` рядом со старым `51-ups.sh`
- Оба executable → оба выполняются → две строки статуса
- Решение: install.sh должен удалять `51-ups.sh` при установке `51-ups-health.sh`
- `chmod -x` недостаточно — нужно `rm`, иначе мусор

### Общий принцип
- Продуктовый инсталлятор не должен знать про Alloy, nut_exporter и прочие site-specific компоненты
- Site-specific скрипт должен аудитить систему на компоненты, ссылающиеся на `cyberpower`, и переключать их на `cyberpower-virtual`
- При откате (удалении ups-battery-monitor) site-specific скрипт должен вернуть всё обратно
