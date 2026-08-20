"""Structural least-authority tests for v3 blackout application ports."""

import inspect
from typing import get_type_hints

import pytest

from src.application import blackout_ports

PORT_METHODS = {
    blackout_ports.BlackoutCaptureStorePort: {
        "open",
        "append_sample",
        "append_gap",
        "append_anchor",
        "rollover",
        "close",
        "recover",
    },
    blackout_ports.BlackoutEvidencePort: {"page"},
    blackout_ports.BlackoutTailStorePort: {"append_tail", "mark_processed"},
    blackout_ports.BlackoutModelCapturePort: {"capture"},
    blackout_ports.BlackoutHistoryPort: {"page_summaries", "project"},
}


def _public_methods(port: type) -> set[str]:
    return {
        name for name, value in vars(port).items() if not name.startswith("_") and callable(value)
    }


def test_ports_are_protocols_with_only_their_narrow_capability_methods() -> None:
    for port, expected_methods in PORT_METHODS.items():
        assert getattr(port, "_is_protocol", False) is True
        assert _public_methods(port) == expected_methods
        assert not any(
            name in vars(port) for name in ("repository", "save", "delete", "query", "model", "bus")
        )


@pytest.mark.parametrize(
    ("port", "method", "parameters"),
    (
        (blackout_ports.BlackoutCaptureStorePort, "open", ("self", "start")),
        (
            blackout_ports.BlackoutCaptureStorePort,
            "append_sample",
            ("self", "ref", "cursor", "sample"),
        ),
        (blackout_ports.BlackoutCaptureStorePort, "append_gap", ("self", "ref", "cursor", "gap")),
        (
            blackout_ports.BlackoutCaptureStorePort,
            "append_anchor",
            ("self", "ref", "cursor", "anchor"),
        ),
        (
            blackout_ports.BlackoutCaptureStorePort,
            "rollover",
            ("self", "ref", "cursor", "budget_kind"),
        ),
        (blackout_ports.BlackoutCaptureStorePort, "close", ("self", "ref", "cursor", "end")),
        (blackout_ports.BlackoutCaptureStorePort, "recover", ("self", "cursor", "limit")),
        (blackout_ports.BlackoutEvidencePort, "page", ("self", "ref", "cursor", "limit")),
        (blackout_ports.BlackoutTailStorePort, "append_tail", ("self", "ref", "batch")),
        (blackout_ports.BlackoutTailStorePort, "mark_processed", ("self", "processing")),
        (blackout_ports.BlackoutModelCapturePort, "capture", ("self",)),
        (blackout_ports.BlackoutHistoryPort, "page_summaries", ("self", "cursor", "limit")),
        (blackout_ports.BlackoutHistoryPort, "project", ("self", "ref")),
    ),
)
def test_port_signatures_expose_explicit_typed_arguments(
    port: type, method: str, parameters: tuple[str, ...]
) -> None:
    signature = inspect.signature(getattr(port, method))
    assert tuple(signature.parameters) == parameters
    assert all(
        parameter.annotation is not inspect.Parameter.empty
        for name, parameter in signature.parameters.items()
        if name != "self"
    )
    assert signature.return_annotation is not inspect.Signature.empty


def test_recovery_and_page_limits_are_keyword_only_and_bounded() -> None:
    recovery = inspect.signature(blackout_ports.BlackoutCaptureStorePort.recover)
    assert recovery.parameters["limit"].kind is inspect.Parameter.KEYWORD_ONLY
    assert recovery.parameters["limit"].default == 32
    evidence = inspect.signature(blackout_ports.BlackoutEvidencePort.page)
    assert evidence.parameters["limit"].kind is inspect.Parameter.KEYWORD_ONLY
    assert evidence.parameters["limit"].default == 1024


def test_history_page_query_has_bounded_default_limit() -> None:
    signature = inspect.signature(blackout_ports.BlackoutHistoryPort.page_summaries)
    assert signature.parameters["limit"].default == 100
    assert signature.parameters["cursor"].default is None
    assert signature.parameters["cursor"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["limit"].kind is inspect.Parameter.KEYWORD_ONLY


def test_port_annotations_reject_wire_and_generic_boundary_types() -> None:
    forbidden = ("bytes", "EncodedV3Record", "jsonl", "adapter", "Any", "object", "Mapping")
    for port in PORT_METHODS:
        for method in PORT_METHODS[port]:
            hints = get_type_hints(getattr(port, method))
            assert hints.get("return") is not None
            rendered = " ".join(repr(annotation) for annotation in hints.values())
            assert not any(token.lower() in rendered.lower() for token in forbidden)


def test_model_port_is_read_only_and_returns_atomic_capture() -> None:
    signature = inspect.signature(blackout_ports.BlackoutModelCapturePort.capture)
    assert signature.return_annotation == "FrozenModelCapture"
    assert _public_methods(blackout_ports.BlackoutModelCapturePort) == {"capture"}
