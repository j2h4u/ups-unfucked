"""Structural guards for the production dependency and model-writer boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
MODEL_OWNER = "src/adapters/model_owner.py"
MODEL_TRANSFORM = "src/adapters/model_transform.py"
EVENT_FILE_OWNERS = frozenset(
    {
        "src/adapters/minimal_event_file.py",
        "src/adapters/telemetry_jsonl.py",
    }
)


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
    for path, tree in _production_trees():
        if path not in EVENT_FILE_OWNERS:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id != "JsonlStoreState"
            if isinstance(node, ast.Attribute):
                assert node.attr != "_state"


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
