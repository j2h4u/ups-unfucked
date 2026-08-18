"""Structural guards for the production dependency and model-writer boundaries."""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
MODEL_OWNER = "src/adapters/model_owner.py"
MODEL_TRANSFORM = "src/adapters/model_transform.py"
JSONL_OWNERS = frozenset(
    {
        "src/adapters/jsonl_event_store.py",
        "src/adapters/jsonl_event_catalog.py",
        "src/adapters/jsonl_event_capacity.py",
        "src/adapters/jsonl_event_stream.py",
        "src/adapters/jsonl_filesystem.py",
        "src/adapters/jsonl_health_inventory.py",
        "src/adapters/jsonl_health_report.py",
        "src/adapters/jsonl_index.py",
        "src/adapters/jsonl_index_merge.py",
        "src/adapters/jsonl_work_registry.py",
    }
)
DOMAIN_KERNEL_MODULES = frozenset(
    {
        "src/domain/fragments.py",
        "src/domain/fragment_policy.py",
        "src/domain/fragment_primitives.py",
        "src/domain/fragment_values.py",
        "src/domain/fragment_builder.py",
        "src/domain/load_sag_assessment.py",
        "src/domain/curve_assessment.py",
        "src/domain/firmware_lb_assessment.py",
        "src/domain/blackout_terminal.py",
        "src/domain/ir_learning_decision.py",
        "src/domain/terminal_outcome.py",
    }
)
DOMAIN_FORBIDDEN_IMPORT_PREFIXES = (
    "src.adapters",
    "src.application",
    "src.alerter",
    "src.motd_status",
    "src.monitor",
    "src.monitor_config",
    "src.nut_client",
    "src.virtual_ups_exporter",
)
SAFETY_MODULES = frozenset(
    {
        "src/application/safety.py",
        "src/application/safety_oracle.py",
        "src/domain/safety_policy.py",
    }
)
SAFETY_FORBIDDEN_IMPORT_PREFIXES = (
    "src.adapters",
    "src.application.assessment",
    "src.application.persistence",
    "src.domain.fragments",
    "src.domain.fragment_policy",
    "src.domain.load_sag_assessment",
    "src.domain.curve_assessment",
    "src.domain.firmware_lb_assessment",
    "src.persistence",
)
FUTURE_V3_ADAPTER_MODULES = frozenset(
    {
        "src.adapters.jsonl_v3_canonical",
        "src.adapters.jsonl_v3_fragment_profile_codec",
        "src.adapters.jsonl_v3_fragment_wire",
        "src.adapters.jsonl_v3_fragment_packing",
        "src.adapters.jsonl_v3_fragment_replay",
        "src.adapters.jsonl_v3_load_sag_assessment_codec",
        "src.adapters.jsonl_v3_curve_assessment_codec",
        "src.adapters.jsonl_v3_firmware_lb_assessment_codec",
        "src.adapters.jsonl_v3_terminal_tail_codec",
        "src.adapters.jsonl_v3_learning_decision_codec",
        "src.adapters.jsonl_v3_model_commit_receipt_codec",
        "src.adapters.jsonl_v3_terminal_outcome_codec",
        "src.adapters.jsonl_v3_tail_budget",
    }
)
V3_ADAPTER_ALLOWED_IMPORTS = {
    "src.adapters.jsonl_v3_canonical": frozenset(),
    "src.adapters.jsonl_v3_fragment_profile_codec": frozenset(
        {
            "src.adapters.jsonl_v3_canonical",
            "src.adapters.jsonl_v3_fragment_packing",
            "src.adapters.jsonl_v3_fragment_replay",
            "src.adapters.jsonl_v3_fragment_wire",
            "src.domain.fragment_policy",
            "src.domain.fragments",
        }
    ),
    "src.adapters.jsonl_v3_fragment_wire": frozenset(
        {
            "src.adapters.jsonl_v3_canonical",
            "src.domain.fragments",
            "src.domain.reasons",
            "src.domain.values",
        }
    ),
    "src.adapters.jsonl_v3_fragment_packing": frozenset(
        {
            "src.adapters.jsonl_v3_canonical",
            "src.adapters.jsonl_v3_fragment_wire",
            "src.domain.fragments",
        }
    ),
    "src.adapters.jsonl_v3_fragment_replay": frozenset(
        {
            "src.adapters.jsonl_v3_canonical",
            "src.adapters.jsonl_v3_fragment_wire",
            "src.domain.fragments",
            "src.domain.reasons",
            "src.domain.values",
        }
    ),
    "src.adapters.jsonl_v3_load_sag_assessment_codec": frozenset(
        {
            "src.adapters.jsonl_v3_canonical",
            "src.adapters.jsonl_v3_fragment_profile_codec",
            "src.domain.fragments",
            "src.domain.load_sag_assessment",
        }
    ),
    "src.adapters.jsonl_v3_curve_assessment_codec": frozenset(
        {
            "src.adapters.jsonl_v3_canonical",
            "src.adapters.jsonl_v3_fragment_profile_codec",
            "src.domain.curve_assessment",
            "src.domain.fragments",
            "src.domain.reasons",
            "src.domain.values",
        }
    ),
    "src.adapters.jsonl_v3_firmware_lb_assessment_codec": frozenset(
        {
            "src.adapters.jsonl_v3_canonical",
            "src.adapters.jsonl_v3_fragment_profile_codec",
            "src.domain.firmware_lb_assessment",
            "src.domain.fragments",
        }
    ),
    "src.adapters.jsonl_v3_terminal_tail_codec": frozenset(
        {
            "src.adapters.jsonl_v3_canonical",
            "src.domain.blackout_terminal",
            "src.domain.fragments",
        }
    ),
    "src.adapters.jsonl_v3_learning_decision_codec": frozenset(
        {
            "src.adapters.jsonl_v3_canonical",
            "src.domain.ir_learning_decision",
            "src.domain.learning",
            "src.domain.reasons",
            "src.domain.values",
        }
    ),
    "src.adapters.jsonl_v3_model_commit_receipt_codec": frozenset(
        {
            "src.adapters.jsonl_v3_canonical",
            "src.adapters.jsonl_v3_learning_decision_codec",
            "src.domain.ir_learning_decision",
            "src.domain.values",
        }
    ),
    "src.adapters.jsonl_v3_terminal_outcome_codec": frozenset(
        {
            "src.adapters.jsonl_v3_canonical",
            "src.adapters.jsonl_v3_curve_assessment_codec",
            "src.adapters.jsonl_v3_firmware_lb_assessment_codec",
            "src.adapters.jsonl_v3_fragment_profile_codec",
            "src.adapters.jsonl_v3_learning_decision_codec",
            "src.adapters.jsonl_v3_load_sag_assessment_codec",
            "src.adapters.jsonl_v3_model_commit_receipt_codec",
            "src.adapters.jsonl_v3_terminal_tail_codec",
            "src.domain.blackout_terminal",
            "src.domain.fragments",
            "src.domain.reasons",
            "src.domain.terminal_outcome",
        }
    ),
    "src.adapters.jsonl_v3_tail_budget": frozenset(
        {
            "src.adapters.jsonl_v3_canonical",
            "src.domain.fragment_policy",
            "src.adapters.jsonl_v3_fragment_profile_codec",
        }
    ),
}
GENERIC_DISPATCH_SUFFIXES = frozenset({"Bus", "Registry", "Dispatcher", "Dispatch"})
GENERIC_DISPATCH_PREFIXES = frozenset({"Capability", "Evidence", "Assessment", "Evaluator"})
LEGACY_TERMINAL_SYMBOLS = frozenset(
    {"TerminalSciencePolicy", "EvidenceClass", "authorizes_science"}
)
LEGACY_V3_SYMBOLS = {
    "src.domain.evidence": frozenset({"TerminalSciencePolicy", "EvidenceClass"}),
    "src.domain.values": frozenset(
        {"EvidenceAssessment", "EvidenceClass", "LearningDecision", "TerminalOutcome"}
    ),
}
LEGACY_TERMINAL_ALLOWED_USE_COUNTS = {
    "src/application/assessment_codec.py": {"EvidenceClass": 2},
    "src/application/assessment_worker.py": {
        "TerminalSciencePolicy": 3,
        "EvidenceClass": 6,
        "authorizes_science": 4,
    },
    "src/application/close_blackout.py": {"EvidenceClass": 2},
    "src/application/decline_reporting.py": {"authorizes_science": 2},
    "src/domain/decline_policy.py": {"EvidenceClass": 2},
    "src/domain/evidence.py": {
        "TerminalSciencePolicy": 3,
        "EvidenceClass": 5,
        "authorizes_science": 2,
    },
    "src/domain/forward_comparison.py": {"EvidenceClass": 2},
    "src/domain/learning.py": {"EvidenceClass": 2},
    "src/domain/values.py": {"EvidenceClass": 1},
}


def _production_trees() -> tuple[tuple[str, ast.Module], ...]:
    return tuple(
        (
            path.relative_to(PROJECT_ROOT).as_posix(),
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
        )
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
    )


def _qualified_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and (parent := _qualified_name(node.value)):
        return f"{parent}.{node.attr}"
    return None


def _imported_modules(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.ImportFrom):
        return (node.module,) if node.module is not None else ()
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    return ()


def _module_is_under(module: str | None, prefix: str) -> bool:
    return module == prefix or (module is not None and module.startswith(f"{prefix}."))


def _subscript_path(node: ast.expr) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if not isinstance(node, ast.Subscript):
        return ()
    if not isinstance(node.slice, ast.Constant) or not isinstance(node.slice.value, str):
        return ()
    return (*_subscript_path(node.value), node.slice.value)


def _target_names(node: ast.expr) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(name for element in node.elts for name in _target_names(element))
    if isinstance(node, ast.Starred):
        return _target_names(node.value)
    return ()


def _defined_symbol_names(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return (node.name,)
    if isinstance(node, ast.Assign):
        return tuple(name for target in node.targets for name in _target_names(target))
    if isinstance(node, ast.AnnAssign):
        return _target_names(node.target)
    if type(node).__name__ == "TypeAlias":
        target = getattr(node, "name", None)
        if isinstance(target, ast.Name):
            return (target.id,)
    return ()


def _semantic_tokens(name: str) -> tuple[str, ...]:
    return tuple(
        token.lower()
        for chunk in name.split("_")
        for token in re.findall(r"[A-Z]+(?=[A-Z][a-z]|$)|[A-Z]?[a-z]+|[0-9]+", chunk)
    )


def _is_generic_dispatch_symbol(name: str) -> bool:
    tokens = _semantic_tokens(name)
    if not tokens:
        return False
    if tokens == ("evidence", "capability"):
        return True
    if tokens[-1] == "capability" and "generic" in tokens:
        return True
    return tokens[-1] in {suffix.lower() for suffix in GENERIC_DISPATCH_SUFFIXES} and bool(
        set(tokens[:-1]) & {prefix.lower() for prefix in GENERIC_DISPATCH_PREFIXES}
    )


def _generic_dispatch_references(tree: ast.Module) -> tuple[tuple[int, str], ...]:
    generic_bindings = _generic_dispatch_bindings(tree)
    return tuple(
        reference
        for node in ast.walk(tree)
        for reference in _generic_dispatch_node_references(node, generic_bindings)
    )


def _generic_dispatch_bindings(tree: ast.Module) -> frozenset[str]:
    generic_bindings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if _is_generic_dispatch_symbol(alias.name):
                    generic_bindings.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if _is_generic_dispatch_symbol(alias.name) or (
                    alias.asname is not None and _is_generic_dispatch_symbol(alias.asname)
                ):
                    generic_bindings.add(alias.asname or alias.name.split(".", maxsplit=1)[0])
    return frozenset(generic_bindings)


def _generic_dispatch_node_references(
    node: ast.AST, generic_bindings: frozenset[str]
) -> tuple[tuple[int, str], ...]:
    if isinstance(node, ast.ImportFrom):
        return tuple(
            (node.lineno, name)
            for alias in node.names
            if _is_generic_dispatch_symbol(alias.name)
            for name in (alias.name, alias.asname)
            if name is not None
        )
    if isinstance(node, ast.Import):
        return tuple(
            (node.lineno, alias.asname or alias.name)
            for alias in node.names
            if _is_generic_dispatch_symbol(alias.name)
            or (alias.asname is not None and _is_generic_dispatch_symbol(alias.asname))
        )
    if isinstance(node, ast.Attribute) and _is_generic_dispatch_symbol(node.attr):
        return ((node.lineno, node.attr),)
    if isinstance(node, ast.Name) and (
        _is_generic_dispatch_symbol(node.id) or node.id in generic_bindings
    ):
        return ((node.lineno, node.id),)
    return tuple(
        (getattr(node, "lineno", 0), name)
        for name in _defined_symbol_names(node)
        if _is_generic_dispatch_symbol(name)
    )


def _canonical_legacy_symbol(qualified: str) -> str | None:
    for symbol in ("TerminalSciencePolicy", "EvidenceClass"):
        if qualified in {
            f"src.domain.evidence.{symbol}",
            f"src.domain.values.{symbol}",
        }:
            return symbol
    return None


def _legacy_symbol_from_qualified(
    qualified: str,
    module_bindings: dict[str, str],
    symbol_bindings: dict[str, str],
) -> str | None:
    parts = qualified.split(".")
    direct = _canonical_legacy_symbol(qualified)
    if direct is not None:
        return direct
    if parts[0] in symbol_bindings:
        return symbol_bindings[parts[0]]
    if parts[0] in module_bindings:
        qualified = ".".join((module_bindings[parts[0]], *parts[1:]))
    return _canonical_legacy_symbol(qualified)


def _legacy_import_bindings(
    tree: ast.Module,
) -> tuple[dict[str, str], dict[str, str], list[tuple[int, str]]]:
    module_bindings: dict[str, str] = {}
    symbol_bindings: dict[str, str] = {}
    references: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            _record_from_bindings(node, module_bindings, symbol_bindings, references)
        elif isinstance(node, ast.Import):
            _record_import_bindings(node, module_bindings)
    return module_bindings, symbol_bindings, references


def _record_from_bindings(
    node: ast.ImportFrom,
    module_bindings: dict[str, str],
    symbol_bindings: dict[str, str],
    references: list[tuple[int, str]],
) -> None:
    if node.module is None:
        return
    for alias in node.names:
        bound = alias.asname or alias.name
        if alias.name in LEGACY_TERMINAL_SYMBOLS:
            symbol_bindings[bound] = alias.name
            references.append((node.lineno, alias.name))
        elif alias.name.isidentifier():
            module_bindings[bound] = f"{node.module}.{alias.name}"


def _record_import_bindings(node: ast.Import, module_bindings: dict[str, str]) -> None:
    for alias in node.names:
        bound = alias.asname or alias.name.split(".", maxsplit=1)[0]
        module_bindings[bound] = alias.name


def _legacy_reference_for_node(
    node: ast.AST,
    module_bindings: dict[str, str],
    symbol_bindings: dict[str, str],
) -> tuple[int, str] | None:
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        symbol = symbol_bindings.get(
            node.id, node.id if node.id in LEGACY_TERMINAL_SYMBOLS else None
        )
        return None if symbol is None else (node.lineno, symbol)
    if not isinstance(node, ast.Attribute):
        return None
    if node.attr == "authorizes_science":
        return node.lineno, "authorizes_science"
    symbol = _legacy_symbol_from_qualified(
        _qualified_name(node) or "", module_bindings, symbol_bindings
    )
    return None if symbol is None else (node.lineno, symbol)


def _legacy_terminal_references(tree: ast.Module) -> tuple[tuple[int, str], ...]:
    module_bindings, symbol_bindings, references = _legacy_import_bindings(tree)
    attribute_children = {
        id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and id(node) in attribute_children:
            continue
        reference = _legacy_reference_for_node(node, module_bindings, symbol_bindings)
        if reference is not None:
            references.append(reference)
    return tuple(references)


def _legacy_terminal_use_counts(tree: ast.Module) -> dict[str, int]:
    return dict(Counter(symbol for _, symbol in _legacy_terminal_references(tree)))


def _legacy_v3_references(tree: ast.Module) -> tuple[tuple[int, str], ...]:
    module_bindings, symbol_bindings, imports = _legacy_v3_import_bindings(tree)
    return (*imports, *_legacy_v3_node_references(tree, module_bindings, symbol_bindings))


def _legacy_v3_import_bindings(
    tree: ast.Module,
) -> tuple[dict[str, str], dict[str, str], tuple[tuple[int, str], ...]]:
    module_bindings: dict[str, str] = {}
    symbol_bindings: dict[str, str] = {}
    references: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            _record_legacy_v3_from_import(node, module_bindings, symbol_bindings, references)
        elif isinstance(node, ast.Import):
            _record_legacy_v3_import(node, module_bindings)
    return module_bindings, symbol_bindings, tuple(references)


def _record_legacy_v3_from_import(
    node: ast.ImportFrom,
    module_bindings: dict[str, str],
    symbol_bindings: dict[str, str],
    references: list[tuple[int, str]],
) -> None:
    if node.module not in LEGACY_V3_SYMBOLS:
        return
    for alias in node.names:
        if alias.name in LEGACY_V3_SYMBOLS[node.module]:
            bound = alias.asname or alias.name
            symbol_bindings[bound] = f"{node.module}.{alias.name}"
            references.append((node.lineno, alias.name))


def _record_legacy_v3_import(node: ast.Import, module_bindings: dict[str, str]) -> None:
    for alias in node.names:
        if alias.name in LEGACY_V3_SYMBOLS:
            module_bindings[alias.asname or alias.name.split(".", maxsplit=1)[0]] = alias.name


def _legacy_v3_node_references(
    tree: ast.Module,
    module_bindings: dict[str, str],
    symbol_bindings: dict[str, str],
) -> tuple[tuple[int, str], ...]:
    references: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            qualified = symbol_bindings.get(node.id)
            if qualified is not None:
                references.append((node.lineno, qualified.rsplit(".", maxsplit=1)[-1]))
        elif isinstance(node, ast.Attribute):
            qualified = _qualified_name(node)
            if qualified is None:
                continue
            parts = qualified.split(".")
            if parts[0] in module_bindings:
                qualified = ".".join((module_bindings[parts[0]], *parts[1:]))
            module, _, symbol = qualified.rpartition(".")
            if symbol in LEGACY_V3_SYMBOLS.get(module, ()):
                references.append((node.lineno, symbol))
    return tuple(references)


def _is_protocol_stub(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return (
        len(node.body) == 1
        and isinstance(node.body[0], ast.Expr)
        and (
            node.body[0].value.value is Ellipsis
            if isinstance(node.body[0].value, ast.Constant)
            else False
        )
    )


def test_jsonl_collaborators_have_local_state_ownership() -> None:
    """The JSONL lanes must not regress to a shared mutable state bag."""
    assert not (SOURCE_ROOT / "adapters" / "jsonl_state.py").exists()
    inventory_fields = frozenset(
        {
            "_health_inventory_complete",
            "_health_catalog_offset",
            "_health_catalog_seq",
            "_health_catalog_prev_hash",
            "_health_catalog_target_offset",
            "_health_pending_event_count",
            "_health_pending_total_bytes",
            "_health_published_stats",
        }
    )
    inventory_field_owners: set[tuple[str, str]] = set()
    for path, tree in _production_trees():
        if path not in JSONL_OWNERS:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id != "JsonlStoreState"
            if isinstance(node, ast.Attribute):
                assert node.attr != "_state"
                if node.attr in inventory_fields:
                    inventory_field_owners.add((path, node.attr))
    assert all(
        path == "src/adapters/jsonl_health_inventory.py" for path, _ in inventory_field_owners
    )


def _model_owner_constructors(tree: ast.Module) -> frozenset[str]:
    constructors: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            constructors.update(
                f"{alias.asname or alias.name}.ModelOwner"
                for alias in node.names
                if alias.name == "src.adapters.model_owner"
            )
        elif isinstance(node, ast.ImportFrom) and (
            node.module == "src.adapters.model_owner"
            or (node.level == 1 and node.module == "model_owner")
        ):
            constructors.update(
                alias.asname or alias.name for alias in node.names if alias.name == "ModelOwner"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "src.adapters":
            constructors.update(
                f"{alias.asname or alias.name}.ModelOwner"
                for alias in node.names
                if alias.name == "model_owner"
            )
    return frozenset(constructors)


def test_production_has_no_legacy_model_import_edge() -> None:
    importers: list[tuple[str, int]] = []
    for path, tree in _production_trees():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and (node.module == "src.model" or node.module.startswith("src.model."))
            ):
                importers.append((path, node.lineno))
            elif isinstance(node, ast.Import) and any(
                alias.name == "src.model" or alias.name.startswith("src.model.")
                for alias in node.names
            ):
                importers.append((path, node.lineno))

    assert importers == []


def test_application_has_no_concrete_adapter_import_edges() -> None:
    forbidden = (
        "src.adapters",
        "src.alerter",
        "src.motd_status",
        "src.nut_client",
        "src.virtual_ups_exporter",
    )
    importers: list[tuple[str, int, str]] = []
    for path, tree in _production_trees():
        if not path.startswith("src/application/"):
            continue
        for node in ast.walk(tree):
            imported = (
                node.module
                if isinstance(node, ast.ImportFrom)
                else next((alias.name for alias in node.names), None)
                if isinstance(node, ast.Import)
                else None
            )
            if imported is not None and any(
                imported == prefix or imported.startswith(f"{prefix}.") for prefix in forbidden
            ):
                importers.append((path, getattr(node, "lineno", 0), imported))

    assert importers == []


def test_domain_fragment_modules_have_no_runtime_boundary_imports() -> None:
    violations: list[tuple[str, int, str]] = []
    for path, tree in _production_trees():
        if path not in DOMAIN_KERNEL_MODULES:
            continue
        for node in ast.walk(tree):
            imported = next(
                (
                    module
                    for module in _imported_modules(node)
                    if any(
                        _module_is_under(module, prefix)
                        for prefix in DOMAIN_FORBIDDEN_IMPORT_PREFIXES
                    )
                ),
                None,
            )
            if imported is not None:
                violations.append((path, getattr(node, "lineno", 0), imported))

    assert violations == []


def test_generic_capability_and_assessment_dispatch_symbols_are_forbidden() -> None:
    violations: list[tuple[str, int, str]] = []
    for path, tree in _production_trees():
        violations.extend(
            (path, lineno, name) for lineno, name in _generic_dispatch_references(tree)
        )

    assert violations == []


def test_generic_dispatch_symbol_matcher_preserves_specific_assessments() -> None:
    tree = ast.parse(
        """
class TelemetryCapability:
    pass

def load_sag_assessment():
    pass

class AssessmentRegistry:
    pass

evaluator_dispatch = object()
"""
    )
    defined = {
        name
        for node in ast.walk(tree)
        for name in _defined_symbol_names(node)
        if _is_generic_dispatch_symbol(name)
    }
    assert defined == {"AssessmentRegistry", "evaluator_dispatch"}


def test_generic_dispatch_guard_resolves_import_aliases_and_qualified_uses() -> None:
    tree = ast.parse(
        """
from package import CapabilityBus as ConcreteBus
from package import AssessmentDispatcher as Dispatcher
import package.registry as registry

class TelemetryCapability:
    pass

class EvidenceAssessment:
    pass

def load_sag_assessment():
    pass

type ConcreteAlias = Dispatcher
registry.AssessmentRegistry = ConcreteBus
value = ConcreteBus
"""
    )
    names = {name for _, name in _generic_dispatch_references(tree)}
    assert names == {
        "CapabilityBus",
        "AssessmentDispatcher",
        "AssessmentRegistry",
        "ConcreteBus",
        "Dispatcher",
    }


def test_safety_modules_do_not_cross_into_fragments_assessment_or_persistence() -> None:
    violations: list[tuple[str, int, str]] = []
    for path, tree in _production_trees():
        if path not in SAFETY_MODULES:
            continue
        for node in ast.walk(tree):
            imported = next(
                (
                    module
                    for module in _imported_modules(node)
                    if any(
                        _module_is_under(module, prefix)
                        for prefix in SAFETY_FORBIDDEN_IMPORT_PREFIXES
                    )
                ),
                None,
            )
            if imported is not None:
                violations.append((path, getattr(node, "lineno", 0), imported))

    assert violations == []


def test_domain_and_application_do_not_import_future_v3_adapters() -> None:
    violations: list[tuple[str, int, str]] = []
    for path, tree in _production_trees():
        if not (path.startswith("src/domain/") or path.startswith("src/application/")):
            continue
        for node in ast.walk(tree):
            violations.extend(
                (path, getattr(node, "lineno", 0), module)
                for module in _imported_modules(node)
                if module in FUTURE_V3_ADAPTER_MODULES
            )
    assert violations == []


def test_v3_adapter_dependencies_are_explicit_and_consumer_scoped() -> None:
    violations: list[tuple[str, str]] = []
    for module, allowed in V3_ADAPTER_ALLOWED_IMPORTS.items():
        path = PROJECT_ROOT / (module.replace(".", "/") + ".py")
        assert path.exists(), module
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = {
            imported_module
            for node in ast.walk(tree)
            for imported_module in _imported_modules(node)
            if imported_module.startswith("src.")
        }
        violations.extend((module, imported_module) for imported_module in imported - allowed)
    assert violations == []


def test_v3_modules_do_not_import_legacy_terminal_or_learning_symbols() -> None:
    domain_modules = tuple(
        path.removesuffix(".py").replace("/", ".") for path in DOMAIN_KERNEL_MODULES
    )
    modules = tuple(V3_ADAPTER_ALLOWED_IMPORTS) + domain_modules
    violations: list[tuple[str, int, str]] = []
    for module in modules:
        path = PROJECT_ROOT / (module.replace(".", "/") + ".py")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations.extend((module, line, symbol) for line, symbol in _legacy_v3_references(tree))
    assert violations == []


def test_future_fragment_profile_codec_reuses_canonical_adapter_when_present() -> None:
    canonical_path = SOURCE_ROOT / "adapters" / "jsonl_v3_canonical.py"
    profile_path = SOURCE_ROOT / "adapters" / "jsonl_v3_fragment_profile_codec.py"
    if not profile_path.exists():
        return
    assert canonical_path.exists()
    tree = ast.parse(profile_path.read_text(encoding="utf-8"), filename=str(profile_path))
    assert any(
        (
            isinstance(node, ast.Import)
            and any(alias.name == "src.adapters.jsonl_v3_canonical" for alias in node.names)
        )
        or (
            isinstance(node, ast.ImportFrom)
            and (
                node.module == "src.adapters.jsonl_v3_canonical"
                or (node.level == 1 and node.module == "jsonl_v3_canonical")
            )
        )
        for node in ast.walk(tree)
    )


def test_legacy_terminal_science_consumers_are_bounded_until_wave3_cutover() -> None:
    """Allow current consumers to shrink, but reject any new module or symbol use."""
    violations: list[tuple[str, str, int, int]] = []
    for path, tree in _production_trees():
        expected = LEGACY_TERMINAL_ALLOWED_USE_COUNTS.get(path, {})
        actual = _legacy_terminal_use_counts(tree)
        violations.extend(
            (path, symbol, count, expected.get(symbol, 0))
            for symbol, count in actual.items()
            if count > expected.get(symbol, 0)
        )

    assert violations == []


def test_legacy_terminal_use_counts_allow_only_monotonic_shrink() -> None:
    tree = ast.parse(
        """
from src.domain.values import EvidenceClass

def use(value):
    return EvidenceClass.QUALIFYING, EvidenceClass.OPERATIONAL_ONLY
"""
    )
    assert _legacy_terminal_use_counts(tree) == {"EvidenceClass": 3}
    baseline = {"EvidenceClass": 3}
    assert all(
        count <= baseline.get(symbol, 0)
        for symbol, count in _legacy_terminal_use_counts(tree).items()
    )
    assert _legacy_terminal_use_counts(ast.parse("")) == {}


def test_legacy_terminal_guard_resolves_alias_and_qualified_uses() -> None:
    tree = ast.parse(
        """
import src.domain.values as values
from src.domain.evidence import TerminalSciencePolicy as Policy

def use(policy: Policy):
    return values.EvidenceClass.QUALIFYING, policy.authorizes_science
"""
    )
    assert {symbol for _, symbol in _legacy_terminal_references(tree)} == {
        "EvidenceClass",
        "TerminalSciencePolicy",
        "authorizes_science",
    }


def test_automatic_model_target_is_explicitly_single_ir_k() -> None:
    schema_path = SOURCE_ROOT / "adapters" / "model_state_schema.py"
    schema_tree = ast.parse(schema_path.read_text(encoding="utf-8"), filename=str(schema_path))
    target_assignments = [
        node
        for node in schema_tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "IR_PARAMETER" for target in node.targets
        )
    ]
    assert len(target_assignments) == 1
    target_value = target_assignments[0].value
    assert isinstance(target_value, ast.Constant)
    assert target_value.value == "ir_k_v_per_pp"

    owner_path = SOURCE_ROOT / "adapters" / "model_owner.py"
    owner_tree = ast.parse(owner_path.read_text(encoding="utf-8"), filename=str(owner_path))
    identity_guard = next(
        node
        for node in owner_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_validate_change_identity"
    )
    owner_class = next(
        node
        for node in owner_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ModelOwner"
    )
    owner_functions = {
        node.name: node for node in owner_class.body if isinstance(node, ast.FunctionDef)
    }
    unsupported_guards = [
        node
        for node in ast.walk(identity_guard)
        if isinstance(node, ast.Compare)
        and _qualified_name(node.left) == "change.parameter"
        and len(node.comparators) == 1
        and isinstance(node.ops[0], ast.NotEq)
        and _qualified_name(node.comparators[0]) == "schema.IR_PARAMETER"
    ]
    assert len(unsupported_guards) == 1

    candidate_function = owner_functions["_prepare_candidate"]
    physics_writes = {
        path
        for node in ast.walk(candidate_function)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if (path := _subscript_path(target)) and len(path) >= 2 and path[1] == "physics"
    }
    assert physics_writes == {("candidate", "physics", "ir_compensation", "k_volts_per_percent")}


def _blackout_capture_constructors(tree: ast.Module) -> frozenset[str]:
    constructors: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "src.application.capture_blackout":
            constructors.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "BlackoutCapture"
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "src.application.capture_blackout":
                    bound = alias.asname or alias.name
                    constructors.add(f"{bound}.BlackoutCapture")
    return frozenset(constructors)


def _blackout_capture_instances(tree: ast.Module, constructors: frozenset[str]) -> frozenset[str]:
    instances: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.annotation is not None:
            if _qualified_name(node.annotation) in constructors:
                instances.add(node.arg)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if _qualified_name(node.annotation) in constructors:
                instances.add(node.target.id)
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            if _qualified_name(node.value.func) in constructors:
                instances.update(
                    target.id for target in node.targets if isinstance(target, ast.Name)
                )
    return frozenset(instances)


def _assignment_paths(node: ast.Assign | ast.AnnAssign) -> tuple[str, ...]:
    targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
    return tuple(path for target in targets if (path := _qualified_name(target)) is not None)


def _blackout_capture_references(tree: ast.Module, constructors: frozenset[str]) -> frozenset[str]:
    references = set(_blackout_capture_instances(tree, constructors))
    capture_names = frozenset(references)
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Attribute):
            if _qualified_name(node.annotation) in constructors:
                references.add(_qualified_name(node.target) or "")
        elif isinstance(node, ast.Assign):
            source = _qualified_name(node.value)
            source_name = source.rsplit(".", maxsplit=1)[-1] if source else None
            if (
                isinstance(node.value, ast.Call)
                and _qualified_name(node.value.func) in constructors
            ):
                references.update(_assignment_paths(node))
            elif source in references or source_name in capture_names:
                references.update(_assignment_paths(node))
    references.discard("")
    return frozenset(references)


def _blackout_capture_private_violations(
    tree: ast.Module, constructors: frozenset[str]
) -> list[tuple[int, str]]:
    references = _blackout_capture_references(tree, constructors)
    return [
        (node.lineno, node.attr)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr.startswith("_")
        and _qualified_name(node.value) in references
    ]


def test_blackout_capture_private_state_stays_in_aggregate() -> None:
    """No production module outside the aggregate may reach its private state."""
    violations: list[tuple[str, int, str]] = []
    for path, tree in _production_trees():
        if path == "src/application/capture_blackout.py":
            continue
        constructors = _blackout_capture_constructors(tree)
        if not constructors:
            continue
        violations.extend(
            (path, lineno, attr)
            for lineno, attr in _blackout_capture_private_violations(tree, constructors)
        )
    assert violations == []


def test_blackout_capture_private_guard_rejects_holder_fixture() -> None:
    path = (
        PROJECT_ROOT
        / "tests"
        / "fixtures"
        / "architecture"
        / ("forbidden_capture_private_state.py")
    )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    constructors = _blackout_capture_constructors(tree)

    assert _blackout_capture_private_violations(tree, constructors) == [(11, "_store")]


def test_monitor_capture_holder_uses_public_capture_api() -> None:
    path = SOURCE_ROOT / "monitor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    constructors = _blackout_capture_constructors(tree)

    assert "self._capture" in _blackout_capture_references(tree, constructors)
    assert _blackout_capture_private_violations(tree, constructors) == []


def test_decline_raw_admission_policy_stays_in_domain() -> None:
    path = SOURCE_ROOT / "application" / "decline_reporting.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    application_definitions = {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    domain_policy_names = {
        "natural_observation",
        "natural_event",
        "natural_prefix",
        "load_sag_observations",
    }
    imported_domain_policy = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "src.domain"
        and any(alias.name == "decline_policy" for alias in node.names)
        for node in tree.body
    )

    assert application_definitions.isdisjoint(domain_policy_names)
    assert imported_domain_policy


def test_model_persistence_authority_is_confined_to_model_state_adapter():
    definitions: list[tuple[str, int]] = []
    persistence_owners: set[str] = set()
    for path, tree in _production_trees():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                node.name == "atomic_write_model"
            ):
                definitions.append((path, node.lineno))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "atomic_write_model":
                    persistence_owners.add(path)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "atomic_write_model":
                    persistence_owners.add(path)

    assert [path for path, _ in definitions] == ["src/adapters/model_state_persistence.py"]
    assert persistence_owners <= {
        MODEL_OWNER,
        MODEL_TRANSFORM,
        "src/adapters/model_state_persistence.py",
    }


def test_model_write_authority_is_semantically_single_lane() -> None:
    mutable_methods = frozenset({"commit", "commit_prepared", "reset_baseline"})
    definitions: set[tuple[str, str, str]] = set()
    mutation_owners: set[str] = set()
    owner_importers: set[str] = set()
    provisioning_calls: list[tuple[str, int]] = []
    for path, tree in _production_trees():
        model_owner_constructors = _model_owner_constructors(tree)
        if model_owner_constructors:
            owner_importers.add(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in mutable_methods:
                    mutation_owners.add(path)
        provisioning_calls.extend(
            (path, node.lineno)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _qualified_name(node.func) in model_owner_constructors
            and any(keyword.arg == "create_if_missing" for keyword in node.keywords)
        )
        for class_node in (node for node in tree.body if isinstance(node, ast.ClassDef)):
            for node in class_node.body:
                if (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name in mutable_methods
                    and not _is_protocol_stub(node)
                ):
                    definitions.add((path, class_node.name, node.name))

    assert owner_importers == {"src/monitor.py"}
    assert provisioning_calls == []
    assert definitions == {
        (MODEL_OWNER, "ModelOwner", "commit"),
        (MODEL_OWNER, "ModelOwner", "commit_prepared"),
        (MODEL_OWNER, "ModelOwner", "reset_baseline"),
    }
    assert mutation_owners == {MODEL_OWNER, "src/application/close_blackout.py"}


def test_production_nut_boundary_is_read_only() -> None:
    command_calls: list[tuple[str, int, str]] = []
    command_definitions: list[tuple[str, int, str]] = []
    forbidden_names = frozenset({"send_instcmd", "upscmd", "systemctl"})
    for path, tree in _production_trees():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in forbidden_names:
                    command_definitions.append((path, node.lineno, node.name))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in forbidden_names:
                    command_calls.append((path, node.lineno, node.func.attr))

    assert command_definitions == []
    assert command_calls == []
