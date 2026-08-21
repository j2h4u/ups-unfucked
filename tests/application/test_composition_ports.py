"""Semantic ownership checks for the post-remediation composition boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

from src.adapters.jsonl_errors import EventConflictError, EventStoreError
from src.application import assessment_worker, safety
from src.application.degraded_startup import DeferredEventStore
from src.application.errors import StoragePortConflict, StoragePortError
from src.application.model_port import AssessmentModelPort
from src.application.ports import (
    AssessmentCloseEventStorePort,
    AssessmentQueryEventStorePort,
    CaptureEventStorePort,
    HealthAlertPort,
    ReportingEventStorePort,
)

ROOT = Path(__file__).parents[2]


def _protocol_methods(protocol: type) -> frozenset[str]:
    return frozenset(
        name for base in protocol.__mro__ for name in base.__dict__ if not name.startswith("_")
    )


def test_event_store_protocols_are_consumer_owned() -> None:
    assert "EventStorePort" not in vars(__import__("src.application.ports", fromlist=["*"]))
    assert _protocol_methods(AssessmentQueryEventStorePort) == frozenset(
        {"project", "history_tail_for_epoch"}
    )
    assert _protocol_methods(AssessmentCloseEventStorePort) == frozenset(
        {"append", "seal", "checkpoint_processing"}
    )
    assert _protocol_methods(CaptureEventStorePort) == frozenset(
        {"open", "append", "recover_startup", "checkpoint_processing"}
    )
    assert "history_tail" in _protocol_methods(ReportingEventStorePort)


def test_assessment_model_port_cannot_execute_a_commit() -> None:
    assert "commit_prepared" not in _protocol_methods(AssessmentModelPort)


def test_jsonl_storage_errors_implement_application_storage_semantics() -> None:
    assert issubclass(EventStoreError, StoragePortError)
    assert issubclass(EventConflictError, StoragePortConflict)
    assert issubclass(EventConflictError, EventStoreError)


def test_health_alert_port_is_one_narrow_publish_operation() -> None:
    assert _protocol_methods(HealthAlertPort) == frozenset({"publish"})


def test_application_codec_has_no_legacy_reexports() -> None:
    assert "json_value" not in vars(assessment_worker)
    assert "virtual_status" not in vars(safety)


def test_deferred_store_has_explicit_activation_methods() -> None:
    assert "__getattr__" not in DeferredEventStore.__dict__
    assert {"activate", "degrade", "open", "append", "storage_health"} <= set(
        DeferredEventStore.__dict__
    )


def test_monitor_composes_background_application_service() -> None:
    tree = ast.parse((ROOT / "src/monitor.py").read_text(encoding="utf-8"))
    assert not any(
        isinstance(node, ast.ClassDef) and node.name == "BackgroundCoordinator"
        for node in tree.body
    )
    assert any(
        isinstance(node, ast.ImportFrom) and node.module == "src.application.background_coordinator"
        for node in tree.body
    )
