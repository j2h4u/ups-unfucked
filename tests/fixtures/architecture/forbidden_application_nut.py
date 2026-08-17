"""Fixture: application code must not reach the concrete NUT client."""

from src.nut_client import NUTClient

FORBIDDEN_DEPENDENCY = NUTClient
