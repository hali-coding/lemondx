#!/usr/bin/env python3
"""CI check: the CLI parser builds, and every command's help formats.

build_parser() constructs every subcommand eagerly, so building it alone
catches things like two subcommands claiming the same short flag -- argparse
raises before anything is printed. But a command's help text is only
formatted when someone asks for that command's --help, so a stray `%` in one
help string, or an argparse change in a newer Python, breaks just that
command and nothing else notices. This formats all of them, nested
subcommands and aliases included.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


def walk(parser: argparse.ArgumentParser, path: str) -> int:
    parser.format_help()
    print(f"ok: {path}")
    count = 1
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                count += walk(sub, f"{path} {name}")
    return count


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from waterpump.cli import build_parser

    count = walk(build_parser(), "waterpump")
    print(f"{count} parsers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
