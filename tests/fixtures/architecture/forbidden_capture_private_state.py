"""Fixture: a holder must not reach BlackoutCapture private state."""

from src.application.capture_blackout import BlackoutCapture


class CaptureHolder:
    def __init__(self, capture: BlackoutCapture) -> None:
        self._capture: BlackoutCapture = capture

    def leak(self) -> object:
        return self._capture._store
