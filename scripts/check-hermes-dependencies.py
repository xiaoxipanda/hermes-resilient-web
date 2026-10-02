#!/usr/bin/env python3
"""Check the plugin's recorded provider pins against a Hermes checkout."""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path


def _load(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("hermes_pyproject", type=Path)
    parser.add_argument(
        "--require-extra",
        action="append",
        default=[],
        help="Fail if this Hermes extra is absent (repeatable).",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    metadata = _load(root / "dependencies.toml")
    hermes = _load(args.hermes_pyproject)

    direct = metadata.get("plugin", {}).get("direct-requirements")
    manifest = (root / "plugin.yaml").read_text(encoding="utf-8")
    errors: list[str] = []
    if direct != []:
        errors.append("plugin.direct-requirements must remain an empty list")
    if "python_dependencies: []" not in manifest:
        errors.append("plugin.yaml must explicitly declare python_dependencies: []")

    project = hermes.get("project", {})
    host_extras = project.get("optional-dependencies", {})
    required = set(args.require_extra)

    print(
        "Hermes "
        f"{project.get('version', 'unknown')} "
        f"(Python {project.get('requires-python', 'unknown')})"
    )

    for extra, spec in metadata.get("extras", {}).items():
        expected = spec.get("requirements")
        actual = host_extras.get(extra)
        if actual is None:
            if not spec.get("allow-missing", False) or extra in required:
                errors.append(f"Hermes extra {extra!r} is missing")
            else:
                print(f"SKIP {extra}: not declared by this Hermes revision")
            continue
        if actual != expected:
            errors.append(
                f"Hermes extra {extra!r} differs: "
                f"expected {expected!r}, found {actual!r}"
            )
            continue
        print(f"OK   {extra}: {', '.join(actual)}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
