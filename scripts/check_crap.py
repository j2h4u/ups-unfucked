#!/usr/bin/env python3
"""Fail when a measured production function exceeds the CRAP threshold."""

from __future__ import annotations

import argparse
from pathlib import Path

from coverage import Coverage
from pytest_crap.calculator import FunctionScore, calculate_crap


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=float, default=30.0)
    parser.add_argument("--coverage-file", type=Path, default=Path(".coverage"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    coverage = Coverage(data_file=str(args.coverage_file))
    coverage.load()
    root = Path.cwd().resolve()
    source_root = root / "src"
    offenders: list[FunctionScore] = []

    for measured in coverage.get_data().measured_files():
        path = Path(measured).resolve()
        if path.suffix != ".py" or not path.is_relative_to(source_root):
            continue
        covered_lines = set(coverage.get_data().lines(measured) or ())
        offenders.extend(
            score
            for score in calculate_crap(str(path), covered_lines)
            if score.crap > args.threshold
        )

    if not offenders:
        print(f"CRAP gate passed: every measured function <= {args.threshold:g}")
        return 0

    print(f"CRAP gate failed: {len(offenders)} function(s) exceed {args.threshold:g}")
    for score in sorted(offenders, key=lambda item: item.crap, reverse=True):
        relative = Path(score.file_path).resolve().relative_to(root)
        print(
            f"{relative}:{score.start_line} {score.name}: "
            f"CRAP={score.crap:.2f}, CC={score.cc}, coverage={score.coverage_percent:.1f}%"
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
