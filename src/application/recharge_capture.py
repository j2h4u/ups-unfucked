"""Safety-independent, bounded capture of the online interval after a blackout."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any, cast

from src.application.assessment_codec import ProjectionInputError, json_value, parse_observation
from src.application.capture_writer import CaptureCommand, CaptureCommandKind, CaptureWriter
from src.application.ports import AssessmentCloseEventStorePort, CaptureEventStorePort
from src.application.storage_values import (
    EventHandle,
    EventProjection,
    EventRecord,
    EventStart,
    RecoveredCapture,
    TerminalOutcomeRecord,
)
from src.domain.recharge import (
    RechargeObservationContext,
    RechargeSamplingPolicy,
    RechargeTerminalContext,
    RechargeTermination,
    decide_observation,
    observation_identity,
    terminal_assessment,
    update_stable_since,
)
from src.domain.values import PhysicalObservation


@dataclass(slots=True)
class _ActiveRecharge:
    episode_id: str
    preceding_blackout_id: str | None
    restoration_observation_id: str
    first_observation: PhysicalObservation
    last_observed: PhysicalObservation
    last_persisted: PhysicalObservation
    handle: EventHandle | None
    observed_samples: int = 1
    persisted_samples: int = 1
    stable_windows: int = 1
    stable_since: datetime | None = None
    elapsed_s: float = 0.0
    state: str = "preparing"
    sample_scheduled: int = 0
    battery_epoch_id: str | None = None
    observation_origin: str = "natural"
    uat_intent_id: str | None = None
    restart_gap: tuple[str, str, str] | None = None
    restart_pending: bool = False
    stable_windows_since_restart: int | None = None


@dataclass(frozen=True, slots=True)
class _StartDetails:
    preceding_blackout_id: str | None
    battery_epoch_id: str | None = None
    observation_origin: str = "natural"
    uat_intent_id: str | None = None
    restart_gap: tuple[str, str, str] | None = None


class RechargeCapture:
    """Capture recharge as an ordinary event on the sole storage port."""

    def __init__(
        self,
        store: CaptureEventStorePort,
        writer: CaptureWriter,
        *,
        policy: RechargeSamplingPolicy | None = None,
        recovered: RecoveredCapture | None = None,
    ) -> None:
        self._store = store
        self._writer = writer
        self._policy = policy or RechargeSamplingPolicy()
        self._lock = Lock()
        self._active: _ActiveRecharge | None = None
        self._pending_restoration_blackout_id: str | None = None
        if recovered is not None:
            self.recover(recovered)

    def recover(self, recovered: RecoveredCapture) -> None:
        """Attach the exact open recharge event found by startup recovery."""
        if recovered.event_kind != "recharge":
            raise ValueError("recovered event is not a recharge")
        first_payload = (
            recovered.first_observation.payload
            if recovered.first_observation is not None
            else recovered.last_observation.payload
        )
        first = _observation_from_record_payload(first_payload)
        last_payload = recovered.last_observation.payload
        last = _observation_from_record_payload(last_payload)
        state = _recharge_state(last_payload)
        restoration_id = _string_value(first_payload.get("restoration_observation_id"))
        if restoration_id is None:
            restoration_id = observation_identity(first)
        preceding = _optional_string(first_payload.get("preceding_blackout_id"))
        stable_since = _parse_stable_since(state.get("stable_since_utc"))
        with self._lock:
            if self._active is not None and self._active.episode_id != recovered.blackout_id:
                raise RuntimeError("another recharge episode is already active")
            self._active = _ActiveRecharge(
                recovered.blackout_id,
                preceding,
                restoration_id,
                first,
                last,
                last,
                recovered.handle,
                observed_samples=_nonnegative_int(state.get("observed_samples"), 1),
                persisted_samples=_nonnegative_int(state.get("persisted_samples"), 1),
                stable_windows=_nonnegative_int(state.get("stable_windows"), 0),
                stable_since=stable_since,
                elapsed_s=max(
                    0.0, last.wall_time_utc.timestamp() - first.wall_time_utc.timestamp()
                ),
                state="capturing",
                battery_epoch_id=_optional_string(first_payload.get("battery_epoch_id")),
                observation_origin=_string_value(first_payload.get("observation_origin"))
                or "natural",
                uat_intent_id=_optional_string(first_payload.get("uat_intent_id")),
                restart_gap=_restart_gap(first_payload.get("restart_gap")),
                restart_pending=True,
                stable_windows_since_restart=0,
            )

    def reconcile_restart(
        self,
        observation: PhysicalObservation,
        projections: Sequence[EventProjection],
    ) -> bool:
        """Start one gap-marked recharge from a durable restoration handoff."""
        with self._lock:
            if self._active is not None:
                return True
            if self._pending_restoration_blackout_id is not None:
                return False
            recharge_links = {
                _optional_string(start.payload.get("preceding_blackout_id"))
                for projection in projections
                if projection.event_kind == "recharge" and (start := projection.start) is not None
            }
            candidates = [
                projection
                for projection in projections
                if projection.event_kind == "blackout"
                and projection.end is not None
                and projection.end.payload.get("termination") == "power_restored"
                and projection.start is not None
                and projection.start.blackout_id not in recharge_links
            ]
            if not candidates:
                return False
            candidate = max(
                candidates,
                key=lambda item: item.end.wall_time_utc if item.end is not None else "",
            )
            end = candidate.end
            start = candidate.start
            if end is None or start is None:
                return False
            gap = (
                end.wall_time_utc,
                _utc(observation),
                "process restarted before recharge start was durable",
            )
            blackout_id = start.blackout_id
        return self._begin(observation, _StartDetails(blackout_id, restart_gap=gap))

    def on_power_restored(self, blackout_id: str) -> None:
        """Remember a durable blackout END for the next online observation."""
        with self._lock:
            if self._active is None:
                self._pending_restoration_blackout_id = blackout_id

    def consume_pending_restoration(self) -> str | None:
        """Peek at the one durable restoration identity at an online poll."""
        with self._lock:
            return self._pending_restoration_blackout_id

    def acknowledge_pending_restoration(self, blackout_id: str) -> None:
        """Clear a restoration identity only after its recharge START is queued."""
        with self._lock:
            if self._pending_restoration_blackout_id == blackout_id:
                self._pending_restoration_blackout_id = None

    def begin(
        self,
        observation: PhysicalObservation,
        *,
        preceding_blackout_id: str | None,
        battery_epoch_id: str | None = None,
        observation_origin: str = "natural",
        uat_intent_id: str | None = None,
    ) -> bool:
        """Durably start one idempotent episode from the first online poll."""
        return self._begin(
            observation,
            _StartDetails(
                preceding_blackout_id,
                battery_epoch_id,
                observation_origin,
                uat_intent_id,
            ),
        )

    def _begin(self, observation: PhysicalObservation, details: _StartDetails) -> bool:
        if details.observation_origin not in {"natural", "self_test", "uat"}:
            raise ValueError("recharge observation origin is invalid")
        restoration_id = observation_identity(observation)
        with self._lock:
            if self._active is not None:
                return self._active.restoration_observation_id == restoration_id
            episode_id = uuid.uuid4().hex
            segment_id = uuid.uuid4().hex
            stable_since = observation.wall_time_utc
            self._active = _ActiveRecharge(
                episode_id,
                details.preceding_blackout_id,
                restoration_id,
                observation,
                observation,
                observation,
                None,
                stable_since=stable_since,
                battery_epoch_id=details.battery_epoch_id,
                observation_origin=details.observation_origin,
                uat_intent_id=details.uat_intent_id,
                restart_gap=details.restart_gap,
            )
            start = EventStart(
                episode_id,
                segment_id,
                observation.boot_id,
                _utc(observation),
                observation.monotonic_ns,
                {
                    "observation": json_value(observation),
                    "restoration_observation_id": restoration_id,
                    "preceding_blackout_id": details.preceding_blackout_id,
                    "policy_revision": self._policy.revision,
                    "battery_epoch_id": details.battery_epoch_id,
                    "observation_origin": details.observation_origin,
                    "uat_intent_id": details.uat_intent_id,
                    "restart_gap": _restart_gap_payload(details.restart_gap),
                    "recharge_state": _state_payload(self._active),
                },
                "recharge",
            )

            def execute() -> None:
                handle = self._store.open(start)
                with self._lock:
                    active = self._active
                    if active is None or active.episode_id != episode_id:
                        raise RuntimeError("recharge start changed before durability")
                    active.handle = handle
                    active.state = "capturing"

            accepted = self._writer.submit(
                CaptureCommand(
                    kind=CaptureCommandKind.START,
                    execute=execute,
                    scope_id=episode_id,
                    recover_failure=lambda _exc: self._recover_start(episode_id, restoration_id),
                )
            )
            if not accepted:
                self._active = None
            return accepted

    def observe(self, observation: PhysicalObservation) -> bool:
        """Offer an online poll; persistence is sampled by the frozen policy."""
        with self._lock:
            active = self._active
            if active is None or active.state != "capturing":
                return False
            return self._observe_active_locked(active, observation)

    def _observe_active_locked(
        self, active: _ActiveRecharge, observation: PhysicalObservation
    ) -> bool:
        active.observed_samples += 1
        active.elapsed_s = max(
            0.0,
            observation.wall_time_utc.timestamp()
            - active.first_observation.wall_time_utc.timestamp(),
        )
        stable_windows = _advance_stability(self._policy, active, observation)
        active.last_observed = observation
        if active.elapsed_s >= self._policy.maximum_duration_s:
            return self._close_locked(
                RechargeTermination.EPISODE_BUDGET_EXHAUSTED,
                observation,
                "maximum recharge duration reached",
            )
        decision = decide_observation(
            RechargeObservationContext(
                self._policy,
                active.first_observation,
                active.last_persisted,
                observation,
                active.elapsed_s,
                active.observed_samples,
                stable_windows,
            )
        )
        if active.observed_samples >= self._policy.maximum_samples:
            return self._close_locked(
                RechargeTermination.EPISODE_BUDGET_EXHAUSTED,
                observation,
                "maximum recharge sample budget reached",
            )
        stable_duration = _stable_duration(active.stable_since, observation)
        if (
            stable_windows >= self._policy.required_consecutive_stable_windows
            and stable_duration >= self._policy.minimum_stabilization_duration_s
        ):
            return self._close_locked(
                RechargeTermination.CHARGE_STABILIZED,
                observation,
                "stable voltage window; full charge is not established",
            )
        if not decision.persist:
            return True
        active.sample_scheduled += 1
        sample_index = active.persisted_samples + active.sample_scheduled
        episode_id = active.episode_id
        payload = {
            "observation": json_value(observation),
            "sample_kind": decision.sample_kind.value if decision.sample_kind else "enrichment",
            "policy_revision": self._policy.revision,
            "sample_index": sample_index,
            "recharge_state": _state_payload(active, persisted_samples=sample_index),
        }

        def execute() -> None:
            with self._lock:
                current = self._active
                handle = None if current is None else current.handle
            if current is None or current.episode_id != episode_id or handle is None:
                raise RuntimeError("recharge sample has no active durable handle")
            next_handle = self._store.append(
                handle,
                EventRecord(
                    "observation",
                    observation.boot_id,
                    _utc(observation),
                    observation.monotonic_ns,
                    payload,
                    "physical",
                    "recharge",
                ),
            )
            with self._lock:
                current = self._active
                if current is not None and current.episode_id == episode_id:
                    current.handle = next_handle
                    current.last_persisted = observation
                    current.persisted_samples += 1
                    current.sample_scheduled = max(0, current.sample_scheduled - 1)

        accepted = self._writer.submit(
            CaptureCommand(
                kind=CaptureCommandKind.OBSERVATION,
                execute=execute,
                scope_id=episode_id,
                recover_failure=lambda _exc: False,
            )
        )
        if not accepted:
            active.sample_scheduled = max(0, active.sample_scheduled - 1)
            return self._close_locked(
                RechargeTermination.GAP,
                observation,
                "recharge observation queue overflow",
            )
        return True

    def supersede_by_blackout(
        self, observation: PhysicalObservation, *, blackout_id: str | None
    ) -> bool:
        with self._lock:
            had_pending = self._pending_restoration_blackout_id is not None
            self._pending_restoration_blackout_id = None
            if self._active is None:
                return had_pending
            return self._close_locked(
                RechargeTermination.SUPERSEDED_BY_BLACKOUT,
                observation,
                "new blackout superseded recharge",
                superseding_blackout_id=blackout_id,
            )

    def service_stop(self, observation: PhysicalObservation) -> bool:
        with self._lock:
            return self._close_locked(
                RechargeTermination.SERVICE_STOP,
                observation,
                "service stopped before recharge stabilization",
            )

    def _close_locked(
        self,
        termination: RechargeTermination,
        observation: PhysicalObservation,
        reason: str,
        *,
        superseding_blackout_id: str | None = None,
        new_battery_epoch_id: str | None = None,
    ) -> bool:
        active = self._active
        if active is None or active.state not in {"capturing", "preparing"}:
            return False
        active.state = "processing"
        durable_sample_count = active.persisted_samples + active.sample_scheduled
        stable_windows = (
            active.stable_windows
            if active.stable_windows_since_restart is None
            else active.stable_windows_since_restart
        )
        assessment = terminal_assessment(
            termination,
            RechargeTerminalContext(
                durable_sample_count,
                active.observed_samples,
                stable_windows,
                self._policy,
                _stable_duration(active.stable_since, observation),
                continuity_gap=active.restart_gap is not None,
            ),
        )
        payload: dict[str, Any] = {
            "episode_id": active.episode_id,
            "preceding_blackout_id": active.preceding_blackout_id,
            "superseding_blackout_id": superseding_blackout_id,
            "new_battery_epoch_id": new_battery_epoch_id,
            "final_observation_id": observation_identity(observation),
            "termination": termination.value,
            "reason": reason,
            "assessment": {
                "kind": assessment.kind.value,
                "reason": assessment.reason,
                "persisted_samples": assessment.persisted_samples,
                "observed_samples": assessment.observed_samples,
            },
            "policy_revision": self._policy.revision,
            "battery_epoch_id": active.battery_epoch_id,
            "observation_origin": active.observation_origin,
            "uat_intent_id": active.uat_intent_id,
            "restart_gap": _restart_gap_payload(active.restart_gap),
            "continuity_gap": active.restart_gap is not None,
            "persisted_samples": durable_sample_count,
            "observed_samples": active.observed_samples,
            "stable_since_utc": _utc(active.stable_since) if active.stable_since else None,
            "stable_windows_since_restart": active.stable_windows_since_restart,
        }
        episode_id = active.episode_id

        def execute() -> None:
            with self._lock:
                current = self._active
                handle = None if current is None else current.handle
            if current is None or current.episode_id != episode_id or handle is None:
                raise RuntimeError("recharge terminal has no active durable handle")
            ended = self._store.append(
                handle,
                EventRecord(
                    "end",
                    observation.boot_id,
                    _utc(observation),
                    observation.monotonic_ns,
                    payload,
                    "physical",
                    "recharge",
                ),
            )
            cast(AssessmentCloseEventStorePort, self._store).seal(
                ended,
                TerminalOutcomeRecord(
                    observation.boot_id,
                    _utc(observation),
                    observation.monotonic_ns,
                    payload,
                    "recharge",
                ),
            )
            with self._lock:
                current = self._active
                if current is not None and current.episode_id == episode_id:
                    self._active = None

        return self._writer.submit(
            CaptureCommand(
                kind=CaptureCommandKind.END,
                execute=execute,
                scope_id=episode_id,
                recover_failure=lambda _exc: False,
            )
        )

    def _recover_start(self, episode_id: str, restoration_id: str) -> bool:
        recovered = self._store.recover_startup()
        if recovered is None or recovered.event_kind != "recharge":
            return False
        if recovered.blackout_id != episode_id or _restoration_id(recovered) != restoration_id:
            return False
        self.recover(recovered)
        return True


def _observation_from_record_payload(payload: Mapping[str, Any]) -> PhysicalObservation:
    raw = payload.get("observation", payload)
    try:
        return parse_observation(raw, 0)
    except (ProjectionInputError, TypeError, ValueError) as exc:
        raise ValueError("recovered recharge observation is invalid") from exc


def _advance_stability(
    policy: RechargeSamplingPolicy,
    active: _ActiveRecharge,
    observation: PhysicalObservation,
) -> int:
    if active.restart_pending:
        active.restart_gap = (
            _utc(active.last_observed),
            _utc(observation),
            "process restarted between durable recharge observations",
        )
        active.stable_since = update_stable_since(policy, observation, observation, None)
        active.restart_pending = False
        active.stable_windows_since_restart = 1 if active.stable_since else 0
    else:
        active.stable_since = update_stable_since(
            policy,
            active.last_observed,
            observation,
            active.stable_since,
        )
        active.stable_windows = active.stable_windows + 1 if active.stable_since else 0
        if active.stable_windows_since_restart is not None:
            active.stable_windows_since_restart = (
                active.stable_windows_since_restart + 1 if active.stable_since else 0
            )
    return (
        active.stable_windows
        if active.stable_windows_since_restart is None
        else active.stable_windows_since_restart
    )


def _recharge_state(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    state = payload.get("recharge_state")
    return state if isinstance(state, dict) else payload


def _state_payload(
    active: _ActiveRecharge,
    *,
    persisted_samples: int | None = None,
) -> dict[str, Any]:
    return {
        "observed_samples": active.observed_samples,
        "persisted_samples": (
            active.persisted_samples if persisted_samples is None else persisted_samples
        ),
        "stable_windows": active.stable_windows,
        "stable_windows_since_restart": active.stable_windows_since_restart,
        "stable_since_utc": _utc(active.stable_since) if active.stable_since else None,
    }


def _restoration_id(recovered: RecoveredCapture) -> str:
    payload = recovered.first_observation.payload if recovered.first_observation else {}
    return _string_value(payload.get("restoration_observation_id")) or observation_identity(
        _observation_from_record_payload(recovered.last_observation.payload)
    )


def _restart_gap(value: object) -> tuple[str, str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("recharge restart gap is invalid")
    kind = value.get("kind")
    start = value.get("from_utc")
    end = value.get("to_utc")
    reason = value.get("reason")
    science_usable = value.get("science_usable")
    if (
        kind != "restart_before_recharge_start"
        or not isinstance(start, str)
        or not isinstance(end, str)
        or not isinstance(reason, str)
        or science_usable is not False
    ):
        raise ValueError("recharge restart gap is invalid")
    return start, end, reason


def _restart_gap_payload(gap: tuple[str, str, str] | None) -> dict[str, object] | None:
    if gap is None:
        return None
    start, end, reason = gap
    return {
        "kind": "restart_before_recharge_start",
        "from_utc": start,
        "to_utc": end,
        "reason": reason,
        "science_usable": False,
    }


def _stable_duration(stable_since: datetime | None, observation: PhysicalObservation) -> float:
    if stable_since is None:
        return 0.0
    return max(0.0, observation.wall_time_utc.timestamp() - stable_since.timestamp())


def _parse_stable_since(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("recharge stable_since_utc is invalid")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError) as exc:
        raise ValueError("recharge stable_since_utc is invalid") from exc


def _nonnegative_int(value: object, fallback: int) -> int:
    if value is None:
        return fallback
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("recharge state counter is invalid")
    return value


def _optional_string(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise ValueError("recharge state string is invalid")


def _string_value(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _utc(observation: PhysicalObservation | datetime) -> str:
    value = (
        observation.wall_time_utc if isinstance(observation, PhysicalObservation) else observation
    )
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
