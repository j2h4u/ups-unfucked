"""Finite freshness policy for one virtual safety publication."""

import math
from dataclasses import dataclass
from enum import StrEnum

from src.application.safety import SAFETY_LB_FLOOR_MINUTES
from src.domain.safety_policy import validate_shutdown_threshold_minutes


class PublicationFreshnessState(StrEnum):
    """Bounded liveness state of the virtual safety publication."""

    FRESH = "fresh"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    STALE_FAILED = "stale_failed"


def telemetry_loss_grace_s(
    *,
    shutdown_minutes: int,
    nut_timeout_s: float,
    polling_interval_s: float,
) -> float:
    """Derive one bounded no-model-feedback grace for missing telemetry.

    An unavailable NUT endpoint cannot distinguish an ordinary upsd restart
    from a simultaneous blackout. The explicit transport budget tolerates a
    bounded restart; the reserve margin prevents that tolerance from consuming
    the two-minute hard safety floor. This is a policy bound, not a claim that
    a missing reading proves the physical UPS state.
    """
    shutdown_minutes = validate_shutdown_threshold_minutes(shutdown_minutes)
    for label, value in (
        ("nut_timeout_s", nut_timeout_s),
        ("polling_interval_s", polling_interval_s),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value <= 0.0
        ):
            raise ValueError(f"{label} must be positive and finite")

    reserve_margin_s = max(
        1.0,
        (shutdown_minutes - SAFETY_LB_FLOOR_MINUTES) * 60.0,
    )
    transport_budget_s = 5.0 * float(nut_timeout_s) + 2.0 * float(polling_interval_s)
    grace_s = min(max(30.0, transport_budget_s), reserve_margin_s)
    if not math.isfinite(grace_s) or grace_s <= 0.0:
        raise ValueError("derived telemetry-loss grace must be positive and finite")
    return grace_s


@dataclass(frozen=True, slots=True)
class PublicationFreshness:
    """Current publication age and the reason it is not fresh, if any."""

    state: PublicationFreshnessState
    age_s: float | None
    reason: str | None
    fail_safe_active: bool


class PublicationFreshnessTracker:
    """Own the finite grace and stale transitions independently of file I/O."""

    def __init__(
        self,
        *,
        initial_monotonic: float,
        initial_file_age_s: float | None,
        max_age_s: float,
    ) -> None:
        self._initial_monotonic = initial_monotonic
        self._initial_file_age_s = initial_file_age_s
        self._max_age_s = max_age_s
        self._cold_start_deadline = initial_monotonic + max_age_s
        self._last_success_monotonic: float | None = None
        self._has_prior_publication = initial_file_age_s is not None
        self._state = (
            PublicationFreshnessState.STALE_FAILED
            if initial_file_age_s is not None and initial_file_age_s > max_age_s
            else PublicationFreshnessState.TEMPORARILY_UNAVAILABLE
        )
        self._fail_safe_active = False

    @property
    def state(self) -> PublicationFreshnessState:
        return self._state

    @property
    def has_prior_publication(self) -> bool:
        return self._has_prior_publication

    @property
    def fail_safe_active(self) -> bool:
        return self._fail_safe_active

    def record_success(self, now: float) -> None:
        self._last_success_monotonic = now
        self._initial_file_age_s = None
        self._has_prior_publication = True
        self._state = PublicationFreshnessState.FRESH
        self._fail_safe_active = False

    def mark_temporarily_unavailable(self) -> None:
        self._state = PublicationFreshnessState.TEMPORARILY_UNAVAILABLE

    def mark_stale(self, *, fail_safe_active: bool) -> None:
        self._state = PublicationFreshnessState.STALE_FAILED
        self._fail_safe_active = fail_safe_active

    def evaluate(
        self,
        now: float,
        *,
        last_error: str | None,
        has_current_publication: bool,
    ) -> PublicationFreshness:
        if not math.isfinite(now):
            raise ValueError("publication clock must be finite")
        age = self._age(now)
        if self._state == PublicationFreshnessState.STALE_FAILED:
            return self._result(age, last_error)
        if not self._has_prior_publication and not has_current_publication:
            if now < self._cold_start_deadline:
                return self._result(age, last_error or "no_physical_publication")
            self._state = PublicationFreshnessState.STALE_FAILED
            return self._result(age, "no_physical_publication_before_cold_start_deadline")
        if age is not None and age <= self._max_age_s:
            return self._result(age, last_error)
        self._state = PublicationFreshnessState.STALE_FAILED
        return self._result(age, last_error or "publication_age_exceeded")

    def _age(self, now: float) -> float | None:
        if self._last_success_monotonic is not None:
            return max(0.0, now - self._last_success_monotonic)
        if self._initial_file_age_s is None:
            return None
        return max(0.0, self._initial_file_age_s + now - self._initial_monotonic)

    def _result(self, age_s: float | None, reason: str | None) -> PublicationFreshness:
        return PublicationFreshness(self._state, age_s, reason, self._fail_safe_active)
