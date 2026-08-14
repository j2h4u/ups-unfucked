# Cross-AI Result: glm-5-3

Execution attempts:

- shared: /home/j2h4u/.opencode/bin/opencode run --model zai-coding-plan/glm-5.3 --agent plan --format json --variant max --attach http://127.0.0.1:49289 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260814T205308Z-standard-rerun/20260814T205317Z/inputs/natural-blackout-learning-implementation.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260814T205308Z-standard-rerun/20260814T205317Z/inputs/deepseek-v4-pro.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260814T205308Z-standard-rerun/20260814T205317Z/inputs/glm-5-3.md --title cross-ai-glm-5-3 -- 'Goal / decision to support: Re-review the complete uncommitted Release A implementation after remediation. Inspect current source and tests, plus the attached prior standard findings. Verify each prior High/Medium is actually closed. Return GO only when there are no actionable Critical, High, or Medium findings. Focus on independent watchdog heartbeat for partial/error responses; bounded one-projection journal counters; fresh estimator state after BaselineReset; foreign-epoch open-event closure without learning; strict current schema with no runtime migration/fallback/self-heal; explicit terminal dispositions and exactly-once markers; one-writer lock; one-second polling/ten-second durable samples; IR golden direction test; deploy runbook outside app code; READY and health observability; scheduler command prohibition; and regression risk. Do not edit repository source files.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'

Command:

```bash
/home/j2h4u/.opencode/bin/opencode run --model zai-coding-plan/glm-5.3 --agent plan --format json --variant max --attach http://127.0.0.1:49289 --dir /home/j2h4u/repos/j2h4u/ups-battery-monitor -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260814T205308Z-standard-rerun/20260814T205317Z/inputs/natural-blackout-learning-implementation.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260814T205308Z-standard-rerun/20260814T205317Z/inputs/deepseek-v4-pro.md -f /home/j2h4u/repos/j2h4u/ups-battery-monitor/docs/plans/cross-ai-release-a-implementation/20260814T205308Z-standard-rerun/20260814T205317Z/inputs/glm-5-3.md --title cross-ai-glm-5-3 -- 'Goal / decision to support: Re-review the complete uncommitted Release A implementation after remediation. Inspect current source and tests, plus the attached prior standard findings. Verify each prior High/Medium is actually closed. Return GO only when there are no actionable Critical, High, or Medium findings. Focus on independent watchdog heartbeat for partial/error responses; bounded one-projection journal counters; fresh estimator state after BaselineReset; foreign-epoch open-event closure without learning; strict current schema with no runtime migration/fallback/self-heal; explicit terminal dispositions and exactly-once markers; one-writer lock; one-second polling/ten-second durable samples; IR golden direction test; deploy runbook outside app code; READY and health observability; scheduler command prohibition; and regression risk. Do not edit repository source files.

Adversarially review the attached context. Focus on correctness, design fit, security/privacy, operational risk, and whether this is ready to ship. Return Critical/High/Medium/Low findings first, ordered by severity, with file/line references where possible. Avoid nitpicks. Do not edit files.

This is a headless, non-interactive session. Never request permission or approval because an unanswered prompt deadlocks the run. Do not delegate to nested agents, invoke task, or use @explore. Your complete working boundary is /home/j2h4u/repos/j2h4u/ups-battery-monitor. Read only files beneath that exact directory. Never inspect sibling directories, parent directories, or any other absolute path. If evidence is unavailable inside the boundary, state that limitation and continue the review. The process already runs from the repository root, so invoke git diff, git status, git show, or git log without git -C. If any tool call is denied, do not search for workarounds: state the denied command and limitation in your final response immediately. Do not edit files.'
```

Output:

# Повторный обзор Release A — вердикт: NO-GO (одно новое Medium)

Статический повторный обзор (bash ограничен командами `git diff/status/log/show`; pytest/ruff/mypy не запускались — то же ограничение, что и в предыдущих обзорах). Все предыдущие находки High/Medium проверены как устраненные; во время исправления регрессии статуса после READY была допущена одна новая ошибка среднего уровня (Medium).

## Средний уровень (Medium)

**M-1. Исправные тики после READY объявляют `STATUS=degraded` для systemd.**
`src/monitor.py:1165-1171`: ветка `else` срабатывает всякий раз, когда `_ready_sent` равно `True` — включая исправные тики. После первого READY каждый последующий исправный опрос (допустимый опрос, успешная запись обоих выходов) отправляет `STATUS=degraded: physical UPS status or output is not fresh`. В `systemctl status ups-battery-monitor` постоянно отображается "degraded" при нормальной работе, поэтому реальная деградация (план §Этап 0.6, §12 — этот сигнал является основной поверхностью для наблюдения оператором за деградацией через systemctl) становится неотличимой от шума на поверхности systemd. Тесты READY останавливаются на опросе READY (`tests/test_monitor_integration.py:960-1004`); ни один тест не утверждает исправный STATUS после READY, из-за чего это и было пропущено. Исправление: три ветки — READY один раз; затем `STATUS=ready` (или ничего) при исправном состоянии и `degraded` только если `outputs_fresh` равно false, — плюс регрессионный тест для исправных тиков после READY.

## Предыдущие результаты проверки

| Предыдущий | Статус | Доказательство |
|---|---|---|
| Watchdog зависает при частичном/ошибочном ответе NUT (deepseek H1 / glm H-1) | Закрыто | Безусловный `sd_notify("WATCHDOG=1")` в `finally` в `src/monitor.py:1033-1046`; частичный ответ снижает статус на `:1080`, но все равно активирует heartbeat; путь исключения все еще проходит через finally. Тесты: `tests/test_monitor.py:1810, 1830, 1855` |
| Квадратичный replay журнала в секунду (deepseek M2 / glm H-2) | Закрыто | Один общий иммуттабельный projection на тик (`src/monitor.py:586-601`, кэш активирован на `:1039-1045`); длительность из projection, а не из replay для каждого события (`:603-627`). Тест: `tests/test_monitor_integration.py:225` |
| BaselineReset наследует оценщик/отслеживание (deepseek M3) | Закрыто | Создаются свежие `CapacityEstimator`/RLS до коммита, заменяются после `reset_baseline` вместе со всеми полями отслеживания обработчика, sag RLS, перепроизводным baseline fingerprint (`src/monitor.py:823-852`); транзакция модели выполняет откат и сохраняет upscmd audit (`src/model.py:679-746`) |
| Открытое событие чужой эпохи зависает в журнале (deepseek M1) | Закрыто | Терминальное закрытие `closed_epoch_mismatch`/`history_only` (`src/discharge_collector.py:428-467`); проверены идемпотентность, отсутствие зависания и принятие нового события (`tests/test_discharge_collector.py:153-234`) |
| Runbook развертывания / инструмент конвертации (glm M-1) | Закрыто* | `docs/RELEASE-A-DEPLOYMENT.md` (резервная копия → остановка → конвертация → валидация загрузчиком → откат). Инструмент намеренно находится за пределами приложения согласно плану §3.0; фактический инструмент на хосте находится за пределами границ репозитория |
| ir_k golden direction test (glm M-2) | Закрыто | `tests/test_release_a_ir_golden.py`: направление ниже/выше эталонной нагрузки, envelope консервативного LB, отсутствие мутации модели |
| Тесты двух писателей + withheld READY/degraded (glm M-3) | Закрыто | Реальный второй конструктор отклонен (`tests/test_monitor.py:1921`); READY-withheld/partial-response/post-outage тесты; `TimeoutStartSec=0` подтвержден |

Предыдущие низкие приоритеты (Lows) также устранены: mkdir `0o700` (`src/monitor.py:93`, протестировано на `tests/test_monitor.py:1879`), `startup_deplay`/`last_event_disposition`/`model_update_mode`/`automatic_dispatch`/`eligible_for_operator_test_at` добавлены в health.json (`src/monitor_config.py:337-342,406-411`, протестировано), несоответствие README/CONTEXT исправлено на 1s/10s/60s, сброс кэшированного наблюдения при завершении работы протестирован, отсутствует защита `pending_replay` от `event_id` (`src/monitor.py:529`).

## Низкий приоритет (Low)

1. `src/scheduler_manager.py:104` docstring все еще утверждает, что "`capture_only` (по умолчанию)"; конструктор по умолчанию использует `proposal_only` (`:215`). Устаревший doc.
2. Мертвый код: `_observed_duration()` (`src/discharge_collector.py:613-616`), `increment_cycle_count`/`add_on_battery_time` не имеют вызывающих сторон — соответствует отложенной очистке на этапе 18.
3. Остаточный полный replay файла происходит раз в секунду (линейный, ограничен байтовым лимитом журнала — допустимо; поддержка projection в памяти при добавлении записей когда-нибудь устранит чтение файла каждую секунду).

## Что подтверждено как надежное

Невизменность scientific в режиме capture-only (права на eligibility + сигнализация fingerprint); строгая текущая схема без миграции/клампа/самовосстановления/перезаписи при запуске (`src/model.py:288-441`); изоляция эпох в replay/счетчиках/маркерах; износоустойчивый хвост под усечением lock с fdatasync; ровно один раз терминальные маркеры с идемпотентностью; блокировка писателя с безопасностью конструктора/`__del__`; структурный `SchedulerModeError`; заморозка SagTracker только для наблюдений; установщик останавливается перед мутацией состояния; явные пути вывода; `POLL_INTERVAL=1` / 10s надежные примеры.

**Итог:** исправить M-1 (логику ветвления STATUS после READY + регрессионный тест), затем этот релиз будет готов к GO; все предыдущие High/Medium закрыты с проверяемыми тестами.
