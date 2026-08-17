"""Fixture: application code must not import the journald adapter."""

from src.alerter import JournaldHealthAlertSink

FORBIDDEN_DEPENDENCY = JournaldHealthAlertSink
