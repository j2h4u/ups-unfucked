"""Fixture: application code must not import the concrete JSONL adapter."""

from src.adapters.jsonl_errors import EventStoreError

FORBIDDEN_DEPENDENCY = EventStoreError
