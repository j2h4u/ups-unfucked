# UPS Battery Monitor — Project Context

Этот документ — единственный источник истины о проекте до начала проектирования.
Здесь: наблюдения, факты, принятые решения, открытые вопросы. Ничего финального.

---

## Железо и окружение

- **UPS**: CyberPower UT850EG (425W номинал), подключён по USB
- **Драйвер**: NUT `usbhid-ups` v2.8.1
- **Сервер**: senbonzakura, Debian 13, нагрузка на UPS обычно 16–21%
- **Телеметрия NUT**: `upsc cyberpower@localhost`

---

## Реальный блекаут 2026-03-12

### Таймлайн

| Время | Событие |
|-------|---------|
| 17:53 | Пропало питание, UPS перешёл в OB DISCHRG (зафиксировано в `journalctl -u nut-monitor`) |
| ~18:28 | `battery.charge` упало до 0%, `battery.runtime` → 0 сек |
| ~18:40 | Сервер выключился |

**Итого**: ~47 минут на батарее при нагрузке 16–18%.
**Важно**: UPS проработал ещё ~12 минут после того, как контроллер показал 0%.

### Почему автошатдаун не сработал

В конфиге драйвера стоит `onlinedischarge_calibration: true`. При статусе OB+LB+DISCHRG NUT решил, что это калибровочный прогон, и не объявил critical state.

Точный лог:
```
is_ups_critical: UPS [cyberpower] is OB+LB now, but it is also calibrating - not declaring a critical state
```

### Почему firmware-калибровка не помогла

После полного разряда + перезарядки `battery.runtime` остался ~1300–1400 сек (~22 мин) — те же значения. Команды `calibrate.start` в `upscmd -l` нет:

```
beeper.disable / enable / mute
driver.killpower / reload
load.off / load.on (+ delay variants)
shutdown.return / stayoff / stop
test.battery.start.deep / quick
test.battery.stop
```

UT850EG не поддерживает пересчёт ёмкости через NUT. Заводские алгоритмы зашиты в микрокод.

---

## Телеметрия NUT

### Все доступные поля

```
battery.charge          # ненадёжно — врёт
battery.charge.low: 10
battery.charge.warning: 20
battery.runtime         # ненадёжно — врёт (~22 мин при реальных ~47)
battery.runtime.low: 300
battery.type: PbAcid
battery.voltage         # НАДЁЖНО — физический показатель
battery.voltage.nominal: 12
ups.load                # НАДЁЖНО — текущая нагрузка в %
ups.realpower.nominal: 425
ups.status              # OL / OB DISCHRG / OB DISCHRG LB
input.voltage
output.voltage
```

### Наблюдения по напряжению

- 100% заряд → ~13.4 V
- 64% заряд → ~12.4 V
- При «0%» по счётчику батарея ещё держала нагрузку ~12 минут

---

## Суть проблемы

Контроллер CyberPower считает заряд по собственному алгоритму, зашитому в микрокод. Алгоритм врёт в ~2 раза по времени и обрывается на 35-й минуте из 47 возможных. `upsmon` доверяет этим данным → shutdown слишком рано или не срабатывает вообще.

Надёжных физических данных два: `battery.voltage` и `ups.load`. Всё остальное строим сами.

---

## Бизнес-требования

1. **Точный прогноз** остаточного времени работы при текущей нагрузке — использовать каждую доступную минуту
2. **Безопасный shutdown** — инициировать самостоятельно, не полагаясь на NUT critical state
3. **Предсказание даты замены батареи** — знать заранее конкретную дату (месяц, год) когда нужно заказать новую батарею, до того как она откажет
4. **Прозрачность для Grafana** — вся телеметрия из одного источника (виртуального UPS), переключение без изменения дашбордов
5. **Минимализм**: no heavy deps, минимум RAM, не изнашивать SSD, systemd-демон («настроил и забыл»)

---

## Концепция: умная нашлёпка

Мы делаем программный слой поверх дешёвого бытового UPS с кривой прошивкой. Не меняем железо, не ломаем NUT, не трогаем Grafana. Встаём между реальным устройством и всеми потребителями и исправляем то, что прошивка делает неправильно.

---

## Архитектурные решения

### Поток данных

```
upsc cyberpower (реальный usbhid-ups)
    ↓ опрос каждые N сек
monitor.py
    ↓ прозрачный проброс всех полей
    ↓ переопределяет 3 поля (см. ниже)
    ↓ пишет все поля в /dev/shm/ups-virtual.dev (tmpfs)
dummy-ups ← читает файл
    ↓
upsd / upsmon / Grafana Alloy — штатная работа, источник данных — виртуальный UPS
```

### dummy-ups: что переопределяем

Виртуальный UPS — зеркало реального. Переопределяются только три поля:

| Поле | Источник |
|------|---------|
| `battery.runtime` | наш расчёт |
| `battery.charge` | наш расчёт |
| `ups.status` | наш расчёт (мы — арбитр флага `LB`) |
| все остальные | прямой проброс из `usbhid-ups` |

Grafana переключается на виртуальный источник и получает те же графики, только три поля становятся честными.

### Различение блекаута и теста батареи

Физический признак — `input.voltage`:

```
ups.status = OB DISCHRG:
    input.voltage ≈ 0   → реальный блекаут → считаем Time_rem, готовим shutdown
    input.voltage ≈ 230 → тест батареи     → собираем калибровочные данные, shutdown не нужен
```

Во время теста UPS переключается на батарею, но питание физически присутствует. Работает независимо от того, кто инициировал тест. Конфиг NUT трогать не нужно.

### ups.status — наш арбитр

Мы сами различаем ситуации по `input.voltage` и выставляем корректные статусы — не полагаясь на интерпретацию NUT/firmware:

- Реальный блекаут + `Time_rem < порог` → эмитируем `OB DISCHRG LB` → `upsmon` инициирует shutdown
- Тест батареи → эмитируем `OB DISCHRG` без `LB`, при необходимости с `CAL` → данные идут в калибровку

Баг с `onlinedischarge_calibration` нерелевантен и не требует исправления: мы не основываем свои решения на флагах от реального устройства, а сами производим правильную классификацию и правильные статусы.

### Хранение состояния

```
RAM:       ring buffer EMA (~2 мин), текущий цикл разряда если идёт
tmpfs:     /dev/shm/ups-virtual.dev — текущие метрики для NUT/Grafana
disk:      ~/.config/ups-battery-monitor/model.json — модель батареи
```

`model.json` обновляется только по завершении discharge event (раз в месяц или после блекаута). Между событиями диск не трогается.

---

## Shutdown Coordination (Phase 3)

### Virtual UPS Proxy Flow

1. **Daemon monitors battery state** (Phase 1-2):
   - Polls real UPS every 10 sec
   - Calculates SoC from voltage, runtime from Peukert law

2. **Daemon computes ups.status override** (Phase 3):
   - ONLINE (input.voltage ≈230V, mains present) → emit "OL"
   - BLACKOUT_REAL (input.voltage ≈0V, no mains) + time_rem < 5min threshold → emit "OB DISCHRG LB"
   - BLACKOUT_TEST (battery test with mains present) → emit "OB DISCHRG" (no LB, calibration data OK)

3. **Daemon writes virtual UPS file** (Phase 3):
   - Write to /dev/shm/ups-virtual.dev (tmpfs, zero SSD wear)
   - Format: NUT standard key-value (VAR lines)
   - Atomic write: tempfile → fsync → rename
   - Every poll cycle (10 sec)

4. **NUT dummy-ups reads virtual device**:
   - dummy-ups driver loads /dev/shm/ups-virtual.dev (reads every timestamp change due to mode=dummy-once)
   - Provides metrics to upsmon and Grafana

5. **upsmon receives LB signal**:
   - Monitors ups.status field
   - When "LB" flag present, triggers LOWBATT notify event
   - Executes SHUTDOWNCMD for graceful shutdown
   - Shutdown does not happen before time_rem actually expires (safety margin: LB fires at < threshold)

### Shutdown Threshold Configuration

Environment variable: `UPS_MONITOR_SHUTDOWN_THRESHOLD_MIN` (default: 5 minutes)
- Set to minutes before battery depletion when LB flag should be raised
- Must be long enough for shutdown to complete (FINALDELAY + script execution time)
- Shorter threshold = more runtime available, but riskier if shutdown takes longer than expected
- Recommended: 5 minutes (allows ~3-4 min shutdown + 1 min safety margin)
- Calibration mode (Phase 6): reduce to 1 minute to collect data to battery cutoff

### Event Classification

- **ONLINE**: UPS on mains (input.voltage ~230V, status="OL")
- **BLACKOUT_REAL**: UPS on battery, no mains (input.voltage ~0V, status="OB DISCHRG")
- **BLACKOUT_TEST**: Battery test with mains present (input.voltage ~230V, status="OB DISCHRG")

Distinction prevents false shutdown triggers during intentional battery tests.

---

## Модель батареи

### Форма разрядной кривой VRLA

Кривая не описывается формулой — только таблицей точек с интерполяцией:

```
13.4V ──────────────────╮
                         │  плато (большая часть энергии)
12.2V               ╭───╯
                    │  колено (начало ускорения)
11.5V          ╭───╯
               │  cliff (резкое падение)
10.5V ─────────╯  cutoff (физический anchor)
```

Каждый экземпляр батареи имеет индивидуальные отклонения: положение колена, крутизна cliff, уровень плато. Разница в 0.2V в зоне колена — это уже несколько минут ошибки в предсказании.

### Структура model.json

Стандартная VRLA-кривая из даташита — это **начальное состояние** model.json, не константа в коде. Со временем точки `"standard"` заменяются на `"measured"` по мере накопления реальных данных:

```json
{
  "full_capacity_ah_ref": 7.2,
  "soh": 0.91,
  "lut": [
    {"v": 13.4, "soc": 1.00, "source": "measured"},
    {"v": 12.8, "soc": 0.85, "source": "measured"},
    {"v": 12.4, "soc": 0.64, "source": "measured"},
    {"v": 12.1, "soc": 0.40, "source": "measured"},
    {"v": 11.6, "soc": 0.18, "source": "standard"},
    {"v": 11.0, "soc": 0.06, "source": "standard"},
    {"v": 10.5, "soc": 0.00, "source": "anchor"}
  ],
  "soh_history": [
    {"date": "2026-04-01", "soh": 1.00},
    {"date": "2026-05-01", "soh": 0.98},
    {"date": "2026-06-01", "soh": 0.96}
  ]
}
```

`"anchor"` — физическая константа VRLA (10.5V = 0 мин), не обновляется никогда.

### Что деградирует с возрастом

Форма кривой (voltage → SoC%) остаётся стабильной. Деградирует только **полная ёмкость** (`full_capacity_ah`). SoH — это отношение текущей ёмкости к исходной:

```
SoH = full_capacity_ah_current / full_capacity_ah_new
```

### Математика предиктора

**EMA-сглаживание (в RAM):**
```
α = 1 - exp(-N/120)   # N — интервал опроса в сек, окно ~2 мин
V_ema = α * V + (1-α) * V_ema_prev
L_ema = α * L + (1-α) * L_ema_prev
```

**IR Compensation** (нормализация напряжения к эталонной нагрузке):
```
V_norm = V_ema + k * (L_ema - L_base)
```
k ≈ 0.01–0.02 В на 1% нагрузки, уточняется на реальных данных.

**Lookup:**
```
V_norm → LUT → SoC
```

**Закон Пеукерта** (химические потери при текущем токе):
```
Time_rem = (full_capacity_ah * SoC * SoH) / (L_ema ^ 1.2) * Const
```
Показатель 1.2 — типовой для VRLA, уточняется на реальных данных.

---

## Калибровка

### Источники данных

| Событие | Частота | Глубина | Что даёт |
|---|---|---|---|
| Короткий блекаут | ~еженедельно | 1–2 мин, верхняя часть | Ничего нового |
| Deep test | раз в месяц | до firmware-порога (~11.5–12V, уточним эмпирически) | Верхняя + средняя часть, `full_capacity_ah` |
| Длинный блекаут | раз в несколько лет | потенциально полная кривая | Cliff region — но ждать непрактично |
| Ручная калибровка | один раз | до cutoff (~10.5V) | Cliff region — сразу |

По первым же запущенным deep test'ам мы эмпирически узнаем реальную глубину — насколько низко firmware UT850EG опускает батарею при тесте.

### Обновление модели после discharge event

При переходе OB→OL:
1. Сравниваем площадь под кривой voltage×time с эталоном → пересчитываем SoH
2. Точки кривой в измеренном диапазоне заменяем с `"standard"` на `"measured"`
3. Обновляем model.json
4. Сырые данные цикла из RAM выбрасываем

### [OPTIONAL] Ручная калибровка в ноль

Одноразовая операция на новой батарее: выдернуть UPS из розетки, дать разрядиться под реальной нагрузкой до cutoff. Единственный практичный способ получить cliff region в обозримом будущем.

**Реализация:**
- Флаг `--calibration-mode` → порог shutdown ~1 мин вместо штатного
- Каждая точка пишется на диск с `fsync` (разовая операция, SSD не жалеем)
- Shutdown инициируем сами — чисто, данные флашатся корректно
- Последний ~1 мин дорисовывается интерполяцией до anchor (10.5V, 0 мин)

**Downside:** глубокий разряд слегка ускоряет износ VRLA. Один раз на новой батарее — некритично.
**Повторять:** только при замене батареи.
**До первого запуска:** система работает на верхней + средней части кривой из deep test'ов, cliff region — из стандартной VRLA-кривой.

---

## Открытые вопросы

### Поведение системы
- [ ] Интервал опроса: 5 сек или 10?
- [ ] Порог shutdown: за сколько минут до конца?
- [x] Алерты: MOTD + journald, Telegram не нужен
- [ ] Порог алерта деградации: расчётное время при 100% заряде < X мин (конкретное значение X — TBD)
- [x] История разрядов: хранить только `(дата, SoH)` после каждого цикла в `soh_history` внутри model.json. Сырая телеметрия не нужна. Линейная регрессия по точкам → экстраполяция даты замены. В MOTD: `Battery: SoH 91% · замена ~Mar 2028`

### Разработка и деплой
- [ ] Определить процесс установки в систему: скрипт, Makefile, пакет?
- [ ] Куда устанавливается бинарь/скрипт: `/usr/local/bin/`, `/opt/ups-battery-monitor/`?
- [ ] Куда устанавливается systemd unit: `/etc/systemd/system/`?
- [ ] Как обновляться: вручную, git pull + переустановка?

**Важное замечание**: запускать скрипты напрямую из `~/repos/` — допустимо только на ранних этапах разработки. В production система должна работать с установленными файлами, а не с рабочей копией репозитория.
