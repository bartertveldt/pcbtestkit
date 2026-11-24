#!/usr/bin/env python3
"""Fill in the GitHub owner everywhere before the first release.

    python3 tools/set_owner.py --owner my-github-user            replace the placeholder in every file
    python3 tools/set_owner.py --owner my-github-user --dry-run  only list what would change

Replaces YOUR-GITHUB-USER in URLs (README, docs, pyproject, plugin manifests, skill) and sets the KiCad
package identifier to com.github.<owner, lower case>.pcbtestkit. The identifier is permanent once a
version is published in KiCad's repository, so pick the account the repository will stay under.
"""
import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLACEHOLDER = "YOUR-GITHUB-USER"
TEXT_EXT = {".md", ".json", ".toml", ".txt", ".py", ".yml", ".yaml", ".cfg"}
SKIP_DIRS = {".git", "dist", "build", "__pycache__", "docs/examples", ".venv", "venv"}


def files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel = os.path.relpath(dirpath, ROOT).replace(os.sep, "/")
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and (rel + "/" + d).lstrip("./") not in SKIP_DIRS]
        for f in filenames:
            # the two tools hold the placeholder itself (to detect it), so they are never rewritten
            if os.path.splitext(f)[1] in TEXT_EXT and f not in ("set_owner.py", "build_pcm.py"):
                yield os.path.join(dirpath, f)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--owner", required=True, help="GitHub user or organisation")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not re.match(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$", a.owner):
        print("not a valid GitHub account name: %r" % a.owner, file=sys.stderr)
        return 2
    ident_old = "com.github.%s.pcbtestkit" % PLACEHOLDER
    ident_new = "com.github.%s.pcbtestkit" % a.owner.lower()
    changed = 0
    for path in files():
        with open(path, "r", encoding="utf-8") as fh:
            try:
                text = fh.read()
            except UnicodeDecodeError:
                continue
        if PLACEHOLDER not in text:
            continue
        new = text.replace(ident_old, ident_new).replace(PLACEHOLDER, a.owner)
        changed += 1
        print(("would update " if a.dry_run else "updated ") + os.path.relpath(path, ROOT))
        if not a.dry_run:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(new)
    print("%d file(s) %s. KiCad identifier: %s" % (changed, "to update" if a.dry_run else "updated", ident_new))
    return 0


if __name__ == "__main__":
    sys.exit(main())
