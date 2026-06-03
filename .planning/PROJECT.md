# UPS Battery Monitor

## What This Is

Программный слой поверх CyberPower UT850EG с ненадёжной прошивкой. Демон читает физические данные с реального UPS через NUT, вычисляет честные значения `battery.runtime`, `battery.charge` и `ups.status` по собственной модели батареи (LUT + IR compensation + Peukert), и публикует их через dummy-ups — прозрачно для всех потребителей (NUT, upsmon, Grafana). Измеряет реальную ёмкость батареи из глубоких разрядов (coulomb counting + voltage anchor), заменяет номинальное значение измеренным, и рекалибрует SoH на основе фактической ёмкости. Отслеживает деградацию батареи (SoH), предсказывает дату замены, алертит через MOTD и journald. Изредка запускает диагностические тесты ёмкости (IEEE-1188-каденция) с safety-гейтами — чтобы **измерить** SoH/ёмкость, а не «лечить» батарею (см. v3.2: премиса активной десульфатации разрядами опровергнута). Один systemd-сервис, zero manual intervention после установки.

## Core Value

Сервер должен выключаться чисто и вовремя при блекауте, используя каждую доступную минуту — не полагаясь на ненадёжные показания прошивки CyberPower.

## Current Milestone: v3.2 Honest Monitoring & Diagnostic Verification

**Goal:** Убрать из v3.0 механизм «активной десульфатации разрядами» (премиса опровергнута кросс-чеком: у VRLA разряд *образует* сульфат, обращает его *заряд*, которым демон не управляет) и свести батарейную логику к честному мониторингу + редкой диагностике ёмкости; попутно починить bootstrap-дедлок планировщика.

**Target work:**
- Удалить `sulfation.py`, `cycle_roi.py`, sulfation/ROI как драйвер планирования, blackout-credit-как-десульфатация, recovery_delta-доказательство и поля `sulfation_score`/`cycle_roi` (health/model/MOTD/journald) + их тесты
- Переосмыслить `evaluate_test_scheduling` → диагностическая capacity-проверка по простому персистентному time-триггеру (~12 мес, IEEE-1188); safety-гейты сохранить; первый тест — quick; чинит Catch-22
- Сохранить: capacity estimation, SoH, IR-тренд, прогноз замены, телеметрию, dispatch `test.battery.start.*`, `motd_status.py`
- Доки: убрать заявления про «активную борьбу с сульфатацией»; ADR с опровержением премисы + факт об отсутствии charge-control
- Секция «Maintenance & schedule» в `battery-health.py`

**Key context:** single-host fail-fast, no backward-compat; демон умеет только **измерять** (диагностический разряд) или **изнашивать** (глубокий разряд) — реальный рычаг ухода (float voltage) внутри прошивки CyberPower, недоступен через NUT.

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
- ✓ Deep discharge capacity estimation with coulomb counting + voltage anchor (CAP-01–04) — v2.0
- ✓ Statistical confidence tracking with CoV-based convergence (CAP-03) — v2.0
- ✓ New battery detection post-discharge + CLI --new-battery reset (CAP-05) — v2.0
- ✓ SoH recalibration against measured capacity with baseline versioning (SOH-01–03) — v2.0
- ✓ MOTD capacity display + journald structured events + health endpoint metrics (RPT-01–03) — v2.0
- ✓ Discharge quality filters: micro-discharge rejection + Peukert fixed at 1.2 (VAL-01–02) — v2.0
- ✓ Math kernel extraction to src/battery_math/ with year-long simulation harness — v2.0
- ⚠️ Sulfation model: physics-based scoring + data-driven detection (IR trend, recovery delta) — v3.0 → **RETRACTED v3.2** (premise disproven: discharge forms sulfate, charge reverses it; see ADR)
- ✓ Smart test scheduling: daemon calls upscmd with safety gates, replaces static systemd timers — v3.0 (**reframed v3.2**: diagnostic capacity verification, not desulfation)
- ⚠️ Cycle ROI metric: desulfation benefit vs wear cost, exported to health.json — v3.0 → **RETRACTED v3.2** (no real desulfation benefit from discharge)
- ⚠️ Natural blackout credit: skip scheduled deep tests when recent blackouts already desulfated — v3.0 → **RETRACTED v3.2** (desulfation-by-discharge premise disproven)
- ✓ Safety constraints: SoH floor, rate limiting, grid stability, cycle budget gates — v3.0 (kept; apply to diagnostic tests)
- ✓ Reporting: scheduling decisions in health.json, journald, MOTD — v3.0 (**v3.2**: sulfation_score/cycle_roi fields removed)
- ✓ 53-fix kaizen pass: naming, error handling, observability, security, complexity — v3.0
- ✓ Unified coulomb counting: single integrate_current() with IEEE-1106 trapezoidal rule — v3.1
- ✓ MonitorDaemon decomposition: SagTracker, SchedulerManager, DischargeCollector extracted — v3.1
- ✓ Naming + docs sweep: BatteryModel.data→state, category→power_source, docstrings — v3.1
- ✓ Test quality rewrite: outcome assertions, DI, real collaborators, pytest markers — v3.1
- ✓ Temperature + security hardening: NUT probe, model.json validation, PASSWORD docs — v3.1

### Active

(No active requirements — planning next milestone)

### Out of Scope

- Telegram-алерты — явно отказались, MOTD+journald достаточно
- NUT-агностик / поддержка других UPS — только CyberPower UT850EG через NUT
- Web UI / REST API — не нужны, минимализм
- Docker-контейнер — systemd-демон, не docker
- Изменение конфигурации NUT — встаём поверх, не трогаем
- Temperature compensation — indoor ±3°C, negligible variation; NUT confirms no sensor available
- Offline mode / multi-UPS — single CyberPower UT850EG only
- **Активная десульфатация разрядами (v3.0)** — премиса опровергнута (v3.2): у VRLA разряд *образует* сульфат, обращает его *заряд*; «десульфатация-разрядом» — миф (BU-804b, Vertiv BattCon, IEEE-1188). Глубокий разряд = износ, не терапия
- **Управление зарядом / float voltage** — недоступно: CyberPower через NUT отдаёт только beeper/driver/load/shutdown/test.battery.* (нет settable-переменных заряда). Реальный рычаг ухода вне досягаемости демона
- **sulfation_score / cycle_roi / blackout-credit-desulfation** — удалены в v3.2 (следствие опровергнутой премисы)

## Context

Shipped v3.1 with 5,768 LOC Python, 568 tests, 24 phases across 5 milestones over 9 days.
Tech stack: Python 3.13, NUT (upsc + dummy-ups), systemd, journald.
Hardware: CyberPower UT850EG (425W), USB, NUT usbhid-ups, Debian 13 (senbonzakura).

Real blackout 2026-03-12 validated the model: 47 min actual vs ~22 min firmware prediction.

v3.1 completed 8-agent code quality review findings: MonitorDaemon decomposed into SagTracker + SchedulerManager + DischargeCollector, unified coulomb counting, outcome-based test suite, temperature placeholder resolved, model.json hardened.

Operating environment: frequent blackouts (several/week), battery at ~35°C due to inverter heat. Daemon controls test scheduling directly via upscmd.

Known future candidates: Peukert auto-calibration, cliff-edge degradation detector, discharge curve shape analysis, seasonal thermal correction.

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
| Math kernel as src/battery_math/ package | Formulas have different change frequencies; mixing in one file couples physics to daemon | ✓ Good — v2.0 |
| Frozen BatteryState dataclass | Circular deps visible at type level; mutable state = hidden side channels | ✓ Good — v2.0 |
| Peukert fixed at 1.2 for v2.0 | Avoid circular dependency (capacity ↔ exponent); v2.1+ owns refinement | ✓ Good — v2.0 |
| CoV-based convergence (count≥3, CoV<10%) | IEEE-450 backed: 2-3 samples → ±5% accuracy. Not a confidence interval — named convergence_score | ✓ Good — v2.0 |
| New battery detection post-discharge, not startup | Fresh measurement vs stored estimate avoids false positives from stale data | ✓ Good — v2.0 |
| Discharge cooldown 60s | Power flicker is physically one discharge; processing as two wastes signal | ✓ Good — v2.0 |
| 30s minimum for SoH update | Short flickers produce junk SoH entries that degrade replacement prediction | ✓ Good — v2.0 |
| MonitorDaemon decomposition into 3 modules | God class → focused modules; each testable without constructing MonitorDaemon | ✓ Good — v3.1 |
| Outcome-based test assertions | Mock sequence replay is brittle; observable state is the contract | ✓ Good — v3.1 |
| Temperature: probe at startup, keep 35°C hardcode | UT850EG has no sensor; document absence rather than pretend | ✓ Good — v3.1 |
| model.json warn+reset, never raise | Daemon must survive corrupt persistence; warn-and-heal is safer than crash | ✓ Good — v3.1 |
| v3.0 desulfation premise retracted | Cross-check (BU-804b, Vertiv BattCon, IEEE-1188): discharge forms sulfate, charge reverses it; daemon has no charge control on CyberPower. Deep-discharge-for-desulfation = wear, not therapy. Scheduler reframed to diagnostic-only capacity verification | ✓ Corrected — v3.2 |

---
*Last updated: 2026-06-03 — milestone v3.2 started (Honest Monitoring & Diagnostic Verification)*
