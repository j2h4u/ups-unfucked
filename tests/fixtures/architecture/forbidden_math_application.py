"""Fixture: battery math must remain independent of application code."""

from src.application import safety

FORBIDDEN_DEPENDENCY = safety.calculate_safety
