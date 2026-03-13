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

### Active

(None — define in next milestone via `/gsd:new-milestone`)

### Out of Scope

- Telegram-алерты — явно отказались, MOTD+journald достаточно
- NUT-агностик / поддержка других UPS — только CyberPower UT850EG через NUT
- Web UI / REST API — не нужны, минимализм
- Docker-контейнер — systemd-демон, не docker
- Изменение конфигурации NUT — встаём поверх, не трогаем

## Context

Shipped v1.0 with 5,003 LOC Python, 160 tests, 103 commits over 2 days.
Tech stack: Python 3.13, NUT (upsc + dummy-ups), systemd, journald.
Hardware: CyberPower UT850EG (425W), USB, NUT usbhid-ups, Debian 13 (senbonzakura).

Real blackout 2026-03-12 validated the model: 47 min actual vs ~22 min firmware prediction.
Firmware showed 0% at minute 35 — UPS ran 12 more minutes.

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

---
*Last updated: 2026-03-14 after v1.0 milestone*
