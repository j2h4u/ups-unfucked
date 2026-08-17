#!/usr/bin/env python3
"""Report production module and class source-span budget violations.

Ruff owns function and method complexity.  This small, deterministic report
owns only the separate concentration guardrail: physical module lines and
the source span of each class.  Tests are intentionally not a default input.
The command returns non-zero when any source-span budget is exceeded, so it can
serve as a hard release gate without a baseline or ratchet.
"""

import argparse
import ast
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

MODULE_LINE_LIMIT = 800
CLASS_SPAN_LIMIT = 500
DEFAULT_ROOTS = (Path("src"), Path("scripts"))
GUIDANCE = "assign one coherent responsibility and extract only a real seam"


@dataclass(frozen=True, slots=True)
class SourceSpanViolation:
    """One deterministic module or class budget violation."""

    path: Path
    kind: str
    name: str
    start_line: int
    end_line: int
    span: int
    limit: int

    @property
    def message(self) -> str:
        """Return the stable remediation message shown by the CLI."""
        unit = "module lines" if self.kind == "module" else "class span"
        return (
            f"{self.path}:{self.start_line}-{self.end_line} {self.name} has {self.span} {unit}; "
            f"limit is {self.limit}; remediation: {GUIDANCE}"
        )


class _ClassCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self._parents: list[str] = []
        self.classes: list[tuple[str, ast.ClassDef]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        name = ".".join((*self._parents, node.name))
        self.classes.append((name, node))
        self._parents.append(node.name)
        self.generic_visit(node)
        self._parents.pop()


def _python_files(roots: Iterable[Path]) -> tuple[Path, ...]:
    files: set[Path] = set()
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            files.add(root)
        elif root.is_dir():
            files.update(path for path in root.rglob("*.py") if path.is_file())
    return tuple(sorted(files, key=lambda path: path.as_posix()))


def _class_start(node: ast.ClassDef) -> int:
    starts = [node.lineno, *(decorator.lineno for decorator in node.decorator_list)]
    return min(starts)


def find_source_span_violations(
    roots: Iterable[Path],
    *,
    project_root: Path | None = None,
) -> tuple[SourceSpanViolation, ...]:
    """Return module/class violations sorted by path, start line, and name."""
    base = (project_root or Path.cwd()).resolve()
    violations: list[SourceSpanViolation] = []
    for path in _python_files(roots):
        source = path.read_text(encoding="utf-8")
        line_count = len(source.splitlines())
        display_path = path.resolve().relative_to(base)
        if line_count > MODULE_LINE_LIMIT:
            violations.append(
                SourceSpanViolation(
                    path=display_path,
                    kind="module",
                    name=display_path.as_posix(),
                    start_line=1,
                    end_line=line_count,
                    span=line_count,
                    limit=MODULE_LINE_LIMIT,
                )
            )

        tree = ast.parse(source, filename=str(path))
        collector = _ClassCollector()
        collector.visit(tree)
        for name, node in collector.classes:
            end_line = node.end_lineno or node.lineno
            start_line = _class_start(node)
            span = end_line - start_line + 1
            if span > CLASS_SPAN_LIMIT:
                violations.append(
                    SourceSpanViolation(
                        path=display_path,
                        kind="class",
                        name=name,
                        start_line=start_line,
                        end_line=end_line,
                        span=span,
                        limit=CLASS_SPAN_LIMIT,
                    )
                )

    return tuple(
        sorted(
            violations,
            key=lambda item: (item.path.as_posix(), item.start_line, item.kind, item.name),
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "roots",
        nargs="*",
        type=Path,
        default=list(DEFAULT_ROOTS),
        help="production roots or Python files to inspect (default: src scripts)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    violations = find_source_span_violations(args.roots)
    print("Source-span gate (module <= 800 physical lines, class <= 500 lines)")
    if not violations:
        print("Source-span report passed: no production budget violations")
        return 0
    print(f"Source-span report found {len(violations)} violation(s)")
    for violation in violations:
        print(violation.message)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
