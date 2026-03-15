# UPS Battery Monitor

## What This Is

Программный слой поверх CyberPower UT850EG с ненадёжной прошивкой. Демон читает физические данные с реального UPS через NUT, вычисляет честные значения `battery.runtime`, `battery.charge` и `ups.status` по собственной модели батареи (LUT + IR compensation + Peukert), и публикует их через dummy-ups — прозрачно для всех потребителей (NUT, upsmon, Grafana). Отслеживает деградацию батареи (SoH), предсказывает дату замены, алертит через MOTD и journald. Один systemd-сервис, zero manual intervention после установки.

## Core Value

Сервер должен выключаться чисто и вовремя при блекауте, используя каждую доступную минуту — не полагаясь на ненадёжные показания прошивки CyberPower.

## Requirements

### Validated

- ✓ Честный прогноз остаточного времени на батарее (voltage + load → LUT → Peukert) — v1.0
- ✓ Безопасный автоматический shutdown через upsmon (эмитируем LB сами) — v1.0
- ✓ Различение реального блекаута и теста батареи по input.voltage — v1.0
- ✓ Самообучающаяся модель батареи с накоплением measured-точек из discharge events — v1.0
- ✓ Предсказание даты замены батареи по линейной регрессии SoH-истории — v1.0
- ✓ Виртуальный dummy-ups как прозрачный источник данных — v1.0
- ✓ Алерты через MOTD и journald (деградация батареи, SoH, дата замены) — v1.0
- ✓ Systemd-демон: установка, запуск, автозапуск после установки — v1.0
- ✓ Режим ручной калибровки (--calibration-mode) для получения cliff region — v1.0
- ✓ Per-poll virtual UPS writes during blackout (safety-critical LB flag fix) — v1.1
- ✓ CurrentMetrics + Config frozen dataclasses (typed, testable architecture) — v1.1
- ✓ Full OL→OB→OL lifecycle + Peukert + signal handler test coverage (205 tests) — v1.1
- ✓ Batch calibration writes, _safe_save helper, consolidated error handling — v1.1
- ✓ History pruning, fdatasync optimization, health.json endpoint — v1.1
- ✓ MetricEMA generic class for extensible per-metric tracking — v1.1

### Active

## Current Milestone: v2.0 Actual Capacity Estimation

**Goal:** Measure real battery capacity (Ah) from discharge data — replace rated label value, enable accurate SoH from day one and cross-brand benchmarking.

**Target features:**
- Back-calculate actual Ah from deep discharge events
- Statistical confidence tracking across multiple discharges
- New battery detection (user input to separate capacity from degradation)
- Auto-rebaseline SoH against measured capacity
- MOTD/journald reporting of rated vs measured capacity

### Out of Scope

- Telegram-алерты — явно отказались, MOTD+journald достаточно
- NUT-агностик / поддержка других UPS — только CyberPower UT850EG через NUT
- Web UI / REST API — не нужны, минимализм
- Docker-контейнер — systemd-демон, не docker
- Изменение конфигурации NUT — встаём поверх, не трогаем

## Context

Shipped v1.1 with 6,596 LOC Python, 205 tests, 215 commits over 2 days.
Tech stack: Python 3.13, NUT (upsc + dummy-ups), systemd, journald.
Hardware: CyberPower UT850EG (425W), USB, NUT usbhid-ups, Debian 13 (senbonzakura).

Real blackout 2026-03-12 validated the model: 47 min actual vs ~22 min firmware prediction.
Firmware showed 0% at minute 35 — UPS ran 12 more minutes.

v1.1 addressed all 19 findings from expert panel review (P0-P3): safety-critical LB lag, architecture refactors, test coverage, code quality, polish.

Known v2 candidates: automatic IR coefficient estimation (CAL2-01), Peukert exponent refinement (CAL2-02), Grafana metrics export (MON-01).

## Constraints

- **Minimal deps**: нет тяжёлых зависимостей, минимум RAM, не изнашивать SSD
- **NUT stack**: Python-скрипт, интеграция с NUT через `upsc` + dummy-ups
- **Disk writes**: обновление model.json только по завершении discharge event
- **No root in hot path**: демон работает с минимальными правами
- **Production install**: файлы устанавливаются в систему (не запускаются из ~/repos/)

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| dummy-ups как прозрачный proxy | Grafana и upsmon не меняются — переключаются на виртуальный источник | ✓ Good |
| Различение блекаут/тест по input.voltage | Физический признак, не зависит от интерпретации firmware | ✓ Good |
| Мы — арбитр ups.status и флага LB | Обходим баг onlinedischarge_calibration без изменения NUT конфига | ✓ Good |
| LUT + IR + Peukert вместо формул | VRLA-кривая не описывается формулой — только таблицей точек | ✓ Good |
| model.json как персистентная модель | Discharge events редки (раз в месяц), SSD не изнашивается | ✓ Good |
| SoH через площадь под кривой voltage×time | Единственный способ посчитать деградацию без доступа к calibrate | ✓ Good |
| Frozen Config dataclass вместо module globals | Testability, no global state pollution, future multi-UPS ready | ✓ Good — v1.1 |
| Per-poll writes only during OB state | Eliminates LB lag without extra SSD wear during normal ops | ✓ Good — v1.1 |
| MetricEMA generic class | Decoupled per-metric EMA enables temperature sensor without code changes | ✓ Good — v1.1 |

---
*Last updated: 2026-03-15 after v2.0 milestone start*
