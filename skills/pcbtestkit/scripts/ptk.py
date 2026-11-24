#!/usr/bin/env python3
"""Run the PCB TestKit CLI from this skill without a pip install.

    python3 <skill-dir>/scripts/ptk.py <command> [args...]      same as `pcbtestkit <command> ...`

Looks for the `pcbtestkit` package in the repository / Claude Code plugin root this skill ships in
(skills/pcbtestkit/scripts -> ../../..), then falls back to an installed package (pip install pcbtestkit).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
if os.path.isdir(os.path.join(ROOT, "pcbtestkit")):
    sys.path.insert(0, ROOT)

try:
    from pcbtestkit.cli import main
except ImportError:
    sys.stderr.write("PCB TestKit is not installed. Install it with `pip install pcbtestkit` (or `pip install .` in the\n"
                     "repository), or use this skill from a clone of the repository / the Claude Code plugin.\n")
    sys.exit(2)

if __name__ == "__main__":
    sys.exit(main())
