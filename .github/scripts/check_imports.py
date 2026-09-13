#!/usr/bin/env python3
"""CI check: every module under src/lemondx imports cleanly on its own.

compileall (run separately in the workflow) only parses source, so it will
not catch an error that only surfaces when a module actually runs -- most
importantly, an accidentally-added third-party import. pyproject.toml
declares `dependencies = []` as a hard constraint, and a fresh CI Python has
nothing but the stdlib installed, so this fails loudly the moment a module
reaches outside it.
"""

from __future__ import annotations

import importlib
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    for path in sorted((REPO_ROOT / "src" / "lemondx").glob("*.py")):
        name = "lemondx" if path.name == "__init__.py" else f"lemondx.{path.stem}"
        importlib.import_module(name)
        print(f"ok: {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
