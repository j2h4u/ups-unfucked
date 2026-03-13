# Requirements: UPS Battery Monitor

**Defined:** 2026-03-13
**Core Value:** Сервер выключается чисто и вовремя при блекауте, используя каждую доступную минуту — не полагаясь на ненадёжные показания прошивки CyberPower.

## v1 Requirements

### Data Collection

- [x] **DATA-01**: Демон читает телеметрию реального UPS через `upsc cyberpower@localhost` с настраиваемым интервалом (5 или 10 сек)
- [x] **DATA-02**: EMA-сглаживание напряжения и нагрузки в RAM с окном ~2 мин (α = 1 - exp(-N/120))
- [x] **DATA-03**: IR compensation — нормализация напряжения к эталонной нагрузке (V_norm = V_ema + k*(L_ema - L_base))

### Battery Model

- [x] **MODEL-01**: model.json хранит LUT (voltage → SoC%) с поддержкой source: standard/measured/anchor
- [x] **MODEL-02**: LUT инициализируется из стандартной VRLA-кривой по даташиту
- [x] **MODEL-03**: SoH-история хранится в model.json как список `{date, soh}` точек
- [x] **MODEL-04**: model.json обновляется только по завершении discharge event (диск не изнашивается)

### Prediction Engine

- [x] **PRED-01**: V_norm → LUT lookup с линейной интерполяцией → SoC
- [x] **PRED-02**: Time_rem по закону Пеукерта: `(capacity_ah * SoC * SoH) / (L_ema ^ 1.2) * Const`
- [x] **PRED-03**: battery.charge вычисляется из SoC (честное значение взамен firmware)

### Event Classification

- [x] **EVT-01**: Различение реального блекаута и теста батареи по input.voltage (≈0 vs ≈230V)
- [x] **EVT-02**: При реальном блекауте — считаем Time_rem, готовим shutdown
- [x] **EVT-03**: При тесте батареи — собираем калибровочные данные, shutdown не нужен
- [x] **EVT-04**: Арбитраж ups.status: сами эмитируем OB DISCHRG LB когда Time_rem < порог
- [x] **EVT-05**: При переходе OB→OL — обновляем LUT measured-точками и пересчитываем SoH

### Virtual UPS (dummy-ups)

- [ ] **VUPS-01**: Демон пишет все поля в /dev/shm/ups-virtual.dev (tmpfs, не диск)
- [ ] **VUPS-02**: Все поля реального UPS прозрачно проксируются в виртуальный
- [ ] **VUPS-03**: Три поля переопределяются нашими значениями: battery.runtime, battery.charge, ups.status
- [ ] **VUPS-04**: dummy-ups настроен в NUT как источник для upsmon и Grafana Alloy

### Shutdown Safety

- [ ] **SHUT-01**: upsmon получает LB от виртуального UPS и инициирует shutdown штатно
- [ ] **SHUT-02**: Порог shutdown настраивается (минут до конца)
- [ ] **SHUT-03**: При calibration-mode порог shutdown снижается до ~1 мин

### Battery Health & Alerts

- [ ] **HLTH-01**: SoH пересчитывается после каждого discharge event (площадь под кривой voltage×time)
- [ ] **HLTH-02**: Линейная регрессия по soh_history → предсказание даты когда SoH < порог замены
- [ ] **HLTH-03**: MOTD-модуль отображает: статус, заряд, Time_rem, нагрузку, SoH, дату замены
- [ ] **HLTH-04**: Алерт в journald при деградации SoH ниже порога
- [ ] **HLTH-05**: MOTD-алерт при расчётном Time_rem@100% < X мин (X — TBD, настраивается)

### Installation & Operations

- [ ] **OPS-01**: Systemd unit файл для автозапуска демона
- [ ] **OPS-02**: Install-скрипт: копирует файлы, настраивает NUT dummy-ups, активирует сервис
- [ ] **OPS-03**: Демон работает как системный сервис с минимальными правами (не root в hot path)
- [ ] **OPS-04**: Логирование в journald (structured, с идентификатором)

### Calibration Mode

- [ ] **CAL-01**: Флаг `--calibration-mode` снижает порог shutdown до ~1 мин
- [ ] **CAL-02**: В calibration-mode каждая точка пишется на диск с fsync
- [ ] **CAL-03**: Cliff region после калибровки дорисовывается интерполяцией до anchor (10.5V, 0 мин)

## v2 Requirements

### Advanced Calibration

- **CAL2-01**: Автоматическая оценка k (IR compensation коэффициента) из discharge данных
- **CAL2-02**: Уточнение показателя Пеукерта (1.2) по реальным данным нескольких циклов

### Monitoring Integration

- **MON-01**: Метрики демона (ошибки, лаг опроса) экспортируются в Grafana через Alloy

## Out of Scope

| Feature | Reason |
|---------|--------|
| Telegram-алерты | Явный отказ: MOTD+journald достаточно |
| Поддержка других UPS / протоколов | Только CyberPower UT850EG через NUT |
| Web UI / REST API | Минимализм, не нужен |
| Docker-контейнер | Systemd-демон — правильное место для этого сервиса |
| Изменение конфигурации NUT | Встаём поверх, не трогаем существующий NUT |
| Real-time push-уведомления | journald+MOTD покрывают потребность |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| DATA-01 | 1 | Complete |
| DATA-02 | 1 | Complete |
| DATA-03 | 1 | Complete |
| MODEL-01 | 1 | Complete |
| MODEL-02 | 1 | Complete |
| MODEL-03 | 1 | Complete |
| MODEL-04 | 1 | Complete |
| PRED-01 | 2 | Complete |
| PRED-02 | 2 | Complete |
| PRED-03 | 2 | Complete |
| EVT-01 | 2 | Complete |
| EVT-02 | 2 | Complete |
| EVT-03 | 2 | Complete |
| EVT-04 | 2 | Complete |
| EVT-05 | 2 | Complete |
| VUPS-01 | 3 | Pending |
| VUPS-02 | 3 | Pending |
| VUPS-03 | 3 | Pending |
| VUPS-04 | 3 | Pending |
| SHUT-01 | 3 | Pending |
| SHUT-02 | 3 | Pending |
| SHUT-03 | 3 | Pending |
| HLTH-01 | 4 | Pending |
| HLTH-02 | 4 | Pending |
| HLTH-03 | 4 | Pending |
| HLTH-04 | 4 | Pending |
| HLTH-05 | 4 | Pending |
| OPS-01 | 5 | Pending |
| OPS-02 | 5 | Pending |
| OPS-03 | 5 | Pending |
| OPS-04 | 5 | Pending |
| CAL-01 | 6 | Pending |
| CAL-02 | 6 | Pending |
| CAL-03 | 6 | Pending |

**Coverage:**
- v1 requirements: 34 total
- Mapped to phases: 34
- Unmapped: 0 ✓

---
*Requirements defined: 2026-03-13*
*Traceability updated: 2026-03-13 after roadmap creation*
