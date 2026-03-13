# UPS Battery Monitor

## What This Is

Программный слой поверх CyberPower UT850EG с ненадёжной прошивкой. Демон читает физические данные с реального UPS через NUT, вычисляет честные значения `battery.runtime`, `battery.charge` и `ups.status` по собственной модели батареи, и публикует их через dummy-ups — прозрачно для всех потребителей (NUT, upsmon, Grafana). Один установленный systemd-сервис, никакого ручного участия в работе.

## Core Value

Сервер должен выключаться чисто и вовремя при блекауте, используя каждую доступную минуту — не полагаясь на ненадёжные показания прошивки CyberPower.

## Requirements

### Validated

(None yet — ship to validate)

### Active

- [ ] Честный прогноз остаточного времени на батарее (voltage + load → LUT → Peukert)
- [ ] Безопасный автоматический shutdown через upsmon (эмитируем LB сами)
- [ ] Различение реального блекаута и теста батареи по input.voltage
- [ ] Самообучающаяся модель батареи с накоплением measured-точек из discharge events
- [ ] Предсказание даты замены батареи по линейной регрессии SoH-истории
- [ ] Виртуальный dummy-ups как прозрачный источник данных (переключение без смены дашбордов)
- [ ] Алерты через MOTD и journald (деградация батареи, SoH, дата замены)
- [ ] Systemd-демон: установка, запуск, автозапуск после установки
- [ ] Режим ручной калибровки (--calibration-mode) для получения cliff region

### Out of Scope

- Telegram-алерты — явно отказались, MOTD+journald достаточно
- NUT-агностик / поддержка других UPS — только CyberPower UT850EG через NUT
- Web UI / REST API — не нужны, минимализм
- Docker-контейнер — systemd-демон, не docker
- Изменение конфигурации NUT — встаём поверх, не трогаем

## Context

**Hardware:** CyberPower UT850EG (425W), USB, NUT usbhid-ups v2.8.1, сервер senbonzakura (Debian 13).

**Реальный блекаут 2026-03-12:** 47 мин реального времени против ~22 мин по firmware. Прошивка показала 0% на 35-й минуте — UPS проработал ещё 12 мин. Автошатдаун не сработал из-за бага `onlinedischarge_calibration: true` в конфиге NUT (firmware-калибровка классифицировалась как OB+LB, но не critical).

**Надёжные физические данные:** только `battery.voltage` (физический показатель) и `ups.load` (текущая нагрузка %). Все остальные поля (`battery.charge`, `battery.runtime`) — ненадёжны.

**Калибровка:** UT850EG не поддерживает `calibrate.start` через NUT. Firmware накапливает данные через deep test (ежемесячно). Реальная кривая строится из discharge events.

**Математика:** EMA-сглаживание (окно ~2 мин) → IR compensation (нормализация к эталонной нагрузке) → LUT lookup → Peukert (показатель 1.2 для VRLA) → Time_rem.

**Хранение:** model.json на диске (обновляется только по завершении discharge event — раз в месяц), /dev/shm/ups-virtual.dev в tmpfs для runtime-метрик, ring buffer в RAM для EMA.

## Constraints

- **Minimal deps**: нет тяжёлых зависимостей, минимум RAM, не изнашивать SSD
- **NUT stack**: Python-скрипт или легковесный демон, интеграция с NUT через `upsc` + dummy-ups
- **Disk writes**: обновление model.json только по завершении discharge event
- **No root in hot path**: демон работает как системный сервис с минимальными правами
- **Production install**: файлы устанавливаются в систему (не запускаются из ~/repos/)

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| dummy-ups как прозрачный proxy | Grafana и upsmon не меняются — переключаются на виртуальный источник | — Pending |
| Различение блекаут/тест по input.voltage | Физический признак, не зависит от интерпретации firmware | — Pending |
| Мы — арбитр ups.status и флага LB | Обходим баг onlinedischarge_calibration без изменения NUT конфига | — Pending |
| LUT + IR + Peukert вместо формул | VRLA-кривая не описывается формулой — только таблицей точек | — Pending |
| model.json как персистентная модель | Discharge events редки (раз в месяц), SSD не изнашивается | — Pending |
| SoH через площадь под кривой voltage×time | Единственный способ посчитать деградацию без доступа к calibrate | — Pending |

---
*Last updated: 2026-03-13 after initialization*
