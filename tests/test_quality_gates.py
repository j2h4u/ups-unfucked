"""Focused tests for the independent quality-gate tooling."""

import ast
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

from scripts.check_complexity_suppressions import find_complexity_suppressions
from scripts.check_source_spans import find_source_span_violations

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "architecture"
PROJECT_ROOT = FIXTURE_ROOT.parents[2]

_FORBIDDEN_FIXTURE_TARGETS = {
    "forbidden_domain_adapter.py": "src/domain/architecture_violation.py",
    "forbidden_application_nut.py": "src/application/architecture_violation.py",
    "forbidden_application_jsonl.py": "src/application/architecture_jsonl_violation.py",
    "forbidden_application_alerter.py": "src/application/architecture_alerter_violation.py",
    "forbidden_math_application.py": "src/battery_math/architecture_violation.py",
    "forbidden_jsonl_model_peer.py": "src/adapters/jsonl_event_store.py",
}


def _isolated_architecture_project(tmp_path: Path) -> Path:
    project = tmp_path / "architecture-project"
    shutil.copytree(
        PROJECT_ROOT / "src",
        project / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for filename in ("pyproject.toml", "tach.toml"):
        shutil.copy2(PROJECT_ROOT / filename, project / filename)
    return project


def _run_architecture_tool(project: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(PROJECT_ROOT),
            "--no-sync",
            *arguments,
        ],
        cwd=project,
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=30,
    )


def _command_output(result: subprocess.CompletedProcess[str]) -> str:
    return f"{result.stdout}\n{result.stderr}"


def _inject_forbidden_fixtures(project: Path) -> None:
    for fixture_name, target_name in _FORBIDDEN_FIXTURE_TARGETS.items():
        fixture = (FIXTURE_ROOT / fixture_name).read_text(encoding="utf-8")
        target = project / target_name
        if target.exists():
            target.write_text(f"{target.read_text(encoding='utf-8')}\n{fixture}", encoding="utf-8")
        else:
            target.write_text(fixture, encoding="utf-8")


def _write_module(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_source_span_checker_enforces_strict_module_boundary(tmp_path: Path) -> None:
    _write_module(tmp_path / "exact.py", ["value = 1"] * 800)
    _write_module(tmp_path / "over.py", ["value = 1"] * 801)

    violations = find_source_span_violations([tmp_path], project_root=tmp_path)

    assert [(item.kind, item.span, item.limit) for item in violations] == [("module", 801, 800)]


def test_source_span_checker_includes_nested_class_boundaries(tmp_path: Path) -> None:
    nested = [
        "class Outer:",
        "    class Inner:",
        *["        value = 1"] * 501,
        "    value = 1",
    ]
    _write_module(tmp_path / "nested.py", nested)

    violations = find_source_span_violations([tmp_path], project_root=tmp_path)

    assert [(item.name, item.span) for item in violations] == [
        ("Outer", 504),
        ("Outer.Inner", 502),
    ]


def test_source_span_checker_accepts_exact_class_boundary(tmp_path: Path) -> None:
    _write_module(tmp_path / "exact_class.py", ["class Exact:", *["    value = 1"] * 499])

    assert find_source_span_violations([tmp_path], project_root=tmp_path) == ()


def test_source_span_checker_uses_decorated_class_start_line(tmp_path: Path) -> None:
    lines = ["@decorator", "class Example:", *["    value = 1"] * 499]
    _write_module(tmp_path / "decorated.py", lines)

    violations = find_source_span_violations([tmp_path], project_root=tmp_path)

    assert [(item.name, item.start_line, item.span) for item in violations] == [("Example", 1, 501)]


def test_suppression_scanner_only_rejects_mandatory_complexity_rules(
    tmp_path: Path,
) -> None:
    _write_module(
        tmp_path / "suppressions.py",
        [
            "value = 1  # noqa: E501",
            "value = 2  # noqa: PLR0911, E501",
            "value = 3  # noqa",
            "text = '# noqa: C901'",
        ],
    )

    findings = find_complexity_suppressions([tmp_path], project_root=tmp_path)

    assert [(item.line, item.codes) for item in findings] == [
        (2, ("PLR0911",)),
        (3, ("<all>",)),
    ]


def test_forbidden_edge_fixtures_pin_the_architecture_contract() -> None:
    expected = {
        "forbidden_domain_adapter.py": "src.adapters",
        "forbidden_application_nut.py": "src.nut_client",
        "forbidden_application_jsonl.py": "src.adapters",
        "forbidden_application_alerter.py": "src.alerter",
        "forbidden_math_application.py": "src.application",
        "forbidden_jsonl_model_peer.py": "src.adapters.model_owner",
    }

    for filename, forbidden_prefix in expected.items():
        tree = ast.parse((FIXTURE_ROOT / filename).read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        )
        assert any(
            imported == forbidden_prefix or imported.startswith(f"{forbidden_prefix}.")
            for imported in imports
        )


def test_import_linter_declares_nested_adapter_family_boundaries() -> None:
    config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    contracts = config["tool"]["importlinter"]["contracts"]
    names = {contract["name"] for contract in contracts}

    assert "JSONL adapter internals do not reach model or NUT adapters" in names
    assert "Model and NUT adapter internals do not reach JSONL adapters" in names
    nested_sources = next(
        contract["source_modules"]
        for contract in contracts
        if contract["name"] == "JSONL adapter internals do not reach model or NUT adapters"
    )
    assert "src.adapters.jsonl_event_store" in nested_sources
    assert "src.adapters.jsonl_filesystem" in nested_sources


def test_configured_architecture_tools_reject_forbidden_edge_fixtures(
    tmp_path: Path,
) -> None:
    project = _isolated_architecture_project(tmp_path)

    clean_import_linter = _run_architecture_tool(
        project,
        "lint-imports",
        "--config",
        str(project / "pyproject.toml"),
        "--no-cache",
    )
    clean_tach = _run_architecture_tool(project, "tach", "check", "--exact")
    assert clean_import_linter.returncode == 0, _command_output(clean_import_linter)
    assert clean_tach.returncode == 0, _command_output(clean_tach)

    _inject_forbidden_fixtures(project)
    broken_import_linter = _run_architecture_tool(
        project,
        "lint-imports",
        "--config",
        str(project / "pyproject.toml"),
        "--no-cache",
    )
    broken_tach = _run_architecture_tool(project, "tach", "check", "--exact")
    lint_output = _command_output(broken_import_linter)
    tach_output = _command_output(broken_tach)

    assert broken_import_linter.returncode != 0, lint_output
    assert "BROKEN" in lint_output
    for target_name in _FORBIDDEN_FIXTURE_TARGETS.values():
        target_module = target_name.removesuffix(".py").replace("/", ".")
        assert target_module in lint_output
        assert target_name in tach_output
    assert broken_tach.returncode != 0, tach_output
    assert "Cannot use" in tach_output
    assert "src.adapters" in tach_output
    assert "src.nut_client" in tach_output
    assert "src.application" in tach_output
    assert "src.adapters.model_owner" in tach_output
