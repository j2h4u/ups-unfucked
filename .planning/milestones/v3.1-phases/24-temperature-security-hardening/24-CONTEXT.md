# Phase 24: Temperature + Security Hardening - Context

**Gathered:** 2026-03-21
**Status:** Ready for planning

<domain>
## Phase Boundary

Resolve temperature sensor placeholder behavior (real check or documented absence), add field-level validation to model.json scheduling fields, document NUT empty PASSWORD security implication, and log atomic_write cleanup failures. Pure hardening — no new features, no behavior changes visible to users.

</domain>

<decisions>
## Implementation Decisions

### Temperature sensor resolution (SEC-01)
- Daemon checks NUT variables for temperature at startup (e.g., `ups.temperature`, `battery.temperature`)
- **Confirmed: UT850EG exposes NO temperature variable via NUT** (verified live 2026-03-21)
- When absent: log structured message "temperature sensor unavailable, skipping thermal compensation" once at startup
- Keep 35°C hardcode in sulfation model with `temperature_source: 'assumed_constant'` — this is already the v3.0 design
- When present (future UPS or firmware update): log "temperature sensor found: X°C" and use real value
- No periodic re-check — single probe at startup is sufficient

### model.json field-level validation (SEC-03)
- Extend `_validate_and_clamp_fields()` to cover all scheduling string fields: `last_upscmd_type`, `last_upscmd_status`, `scheduled_test_reason`, `test_block_reason`
- Pattern: if field is not None and not isinstance(str), log warning with event_type `model_field_clamped` and reset to None
- Validate list fields: `sulfation_history`, `discharge_events`, `roi_history`, `natural_blackout_events` — if not a list, log warning and reset to `[]`
- Consistent with existing validation pattern (soh clamping, timestamp checks, blackout_credit dict check)
- No new exceptions raised — warn + reset, preserving daemon startup resilience

### NUT empty PASSWORD documentation (SEC-02)
- Add code comment at `nut_client.py` PASSWORD line explaining the security implication: upsd.users must allow passwordless auth for upsmon, this means any local process can send INSTCMD
- Add security note in README explaining: NUT access is local-only (loopback), empty PASSWORD is acceptable for single-server deployment, but users should be aware of the trust boundary
- No code change to auth flow — this is documentation only

### atomic_write cleanup logging (SEC-04)
- Replace `pass` in `except OSError` block (model.py:104) with `logger.warning()` including the temp file path and the OSError
- Log level: warning (not error) — the original exception that triggered cleanup is the real error; failed cleanup is secondary
- Include `event_type: 'atomic_write_cleanup_failed'` for structured logging consistency

### Claude's Discretion
- Exact wording of security documentation in README
- Which NUT variable names to probe for temperature (ups.temperature, battery.temperature, ambient.temperature — check NUT documentation for standard names)
- Test structure for the new validation code (extend existing test_model.py or new test functions)

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements
- `.planning/REQUIREMENTS.md` — SEC-01 through SEC-04 definitions and acceptance criteria

### Existing validation
- `src/model.py` §267-293 (`_validate_and_clamp_fields`) — current validation pattern to extend
- `src/model.py` §247-265 (`_apply_defaults`) — scheduling field defaults
- `src/model.py` §60-108 (`atomic_write`) — cleanup error to fix

### NUT authentication
- `src/nut_client.py` §205-260 (`send_instcmd`) — PASSWORD auth flow to document

### Temperature references
- `src/discharge_handler.py` §281,351,385 — hardcoded 35°C temperature values
- `src/battery_math/sulfation.py` §39-47,83 — temperature parameter in sulfation model

### Project context
- `.planning/PROJECT.md` — "Temperature compensation — indoor ±3°C, negligible variation" (out of scope confirmation)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `_validate_and_clamp_fields()` in model.py: established validation+warn+reset pattern — extend for scheduling fields
- `MetricEMA` in ema_filter.py: ready for temperature tracking if sensor becomes available (future-proof, no action needed now)
- Structured logging with `event_type` extra field: consistent across all modules — use same pattern for new warnings

### Established Patterns
- Model validation: warn + clamp/reset, never raise (daemon must survive corrupt model.json)
- Atomic write: tempfile + fdatasync + os.replace, same-dir guarantee
- NUT client: socket session context manager, command-response protocol
- Temperature: hardcoded 35°C with `temperature_source: 'assumed_constant'` tag in all sulfation events

### Integration Points
- `BatteryModel.load()` calls `_validate_and_clamp_fields()` — new validation added there runs automatically
- `MonitorDaemon.__init__` or startup path — temperature probe runs here once
- `nut_client.NUTClient.get_ups_vars()` — returns dict of all NUT variables, can check for temperature keys

</code_context>

<specifics>
## Specific Ideas

- Temperature probe should use `get_ups_vars()` which already returns all variables — check if any key starts with `ups.temperature` or `battery.temperature`
- The 35°C hardcode is validated by operating context: UPS sits next to inverter, ambient ±3°C variation is negligible for sulfation rate calculation
- SEC-04 is a one-line fix (replace `pass` with `logger.warning`)

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope

### Reviewed Todos (not folded)
- "Anti-sulfation deep discharge scheduling for battery longevity" — already implemented in Phase 17 (v3.0); todo is stale

</deferred>

---

*Phase: 24-temperature-security-hardening*
*Context gathered: 2026-03-21*
