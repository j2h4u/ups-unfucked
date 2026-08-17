#!/usr/bin/env python3
"""Fail when Ruff complexity rules are suppressed with ``noqa``."""

import argparse
import io
import tokenize
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

MANDATORY_COMPLEXITY_CODES = frozenset(
    {
        "C901",
        "PLR0904",
        "PLR0911",
        "PLR0912",
        "PLR0913",
        "PLR0914",
        "PLR0915",
        "PLR0916",
        "PLR0917",
        "PLR1702",
    }
)
DEFAULT_ROOTS = (Path("src"), Path("tests"), Path("scripts"))


@dataclass(frozen=True, slots=True)
class ComplexitySuppression:
    """One ``noqa`` comment that bypasses a mandatory complexity rule."""

    path: Path
    line: int
    codes: tuple[str, ...]
    comment: str


def _python_files(roots: Iterable[Path]) -> tuple[Path, ...]:
    files: set[Path] = set()
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            files.add(root)
        elif root.is_dir():
            files.update(path for path in root.rglob("*.py") if path.is_file())
    return tuple(sorted(files, key=lambda path: path.as_posix()))


def _comment_codes(comment: str) -> tuple[str, ...] | None:
    marker = comment.lower().find("# noqa")
    if marker < 0:
        return None
    suffix = comment[marker + len("# noqa") :]
    if suffix and (suffix[0].isalnum() or suffix[0] == "_"):
        return None
    if not suffix.lstrip().startswith(":"):
        return ("<all>",)
    listed = suffix.lstrip()[1:]
    codes = tuple(
        code.upper()
        for code in listed.replace(",", " ").split()
        if code.upper() in MANDATORY_COMPLEXITY_CODES
    )
    return codes or None


def find_complexity_suppressions(
    roots: Iterable[Path],
    *,
    project_root: Path | None = None,
) -> tuple[ComplexitySuppression, ...]:
    """Return mandatory-complexity ``noqa`` comments in deterministic order."""
    base = (project_root or Path.cwd()).resolve()
    findings: list[ComplexitySuppression] = []
    for path in _python_files(roots):
        source = path.read_text(encoding="utf-8")
        try:
            tokens = tokenize.generate_tokens(io.StringIO(source).readline)
            comments = (token for token in tokens if token.type == tokenize.COMMENT)
            for token in comments:
                codes = _comment_codes(token.string)
                if codes is None:
                    continue
                findings.append(
                    ComplexitySuppression(
                        path=path.resolve().relative_to(base),
                        line=token.start[0],
                        codes=codes,
                        comment=token.string,
                    )
                )
        except (tokenize.TokenError, IndentationError):
            # Ruff is responsible for syntax diagnostics.  This scanner still
            # reports all comments tokenized before an incomplete source file.
            continue
    return tuple(sorted(findings, key=lambda item: (item.path.as_posix(), item.line)))


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
    findings = find_complexity_suppressions(args.roots)
    if not findings:
        print("Complexity suppression scan passed: no mandatory-rule noqa comments")
        return 0
    print(f"Complexity suppression scan failed: {len(findings)} suppression(s)")
    for finding in findings:
        codes = ",".join(finding.codes)
        print(f"{finding.path}:{finding.line}: noqa suppresses {codes}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
