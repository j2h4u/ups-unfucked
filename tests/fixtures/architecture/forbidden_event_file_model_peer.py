"""Fixture: event-file adapter internals must not import model adapters."""

from src.adapters.model_owner import ModelOwner

FORBIDDEN_DEPENDENCY = ModelOwner
