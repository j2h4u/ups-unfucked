"""Fixture: domain code must not import a concrete adapter."""

from src.adapters import model_owner

FORBIDDEN_DEPENDENCY = model_owner.ModelOwner
