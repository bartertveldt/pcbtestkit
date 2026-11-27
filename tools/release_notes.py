#!/usr/bin/env python3
"""The release notes for a version: its section of CHANGELOG.md and how to check the downloads.

    python3 tools/release_notes.py 1.2.0 > dist/RELEASE_NOTES.md
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = "bartertveldt/pcbtestkit"


def section(text: str, ver: str) -> str:
    """The body of '## <ver>' (or '## [<ver>]') up to the next '## ' heading."""
    m = re.search(r"^## \[?v?%s\]?[^\n]*\n(.*?)(?=^## |\Z)" % re.escape(ver), text, re.M | re.S)
    return m.group(1).strip() if m else ""


def unpublished(text: str, ver: str) -> str:
    """The sections right after ``ver`` whose heading says they were never published (their tags' builds failed):
    their changes reach users with this release, so its notes include them."""
    out = []
    heads = list(re.finditer(r"^## \[?v?([0-9][^\s\]]*)\]?([^\n]*)\n", text, re.M))
    idx = next((i for i, m in enumerate(heads) if m.group(1) == ver), None)
    if idx is None:
        return ""
    for i in range(idx + 1, len(heads)):
        m = heads[i]
        if "not published" not in m.group(2):
            break
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        out.append("## Also in this release: %s%s\n\n%s" % (m.group(1), "", text[m.end():end].strip()))
    return "\n\n".join(out)


def notes(ver: str) -> str:
    with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as fh:
        text = fh.read()
    body = section(text, ver) or "See CHANGELOG.md."
    more = unpublished(text, ver)
    if more:
        body += "\n\n" + more
    return ("%s\n\n## Install\n\n* **KiCad:** *Plugin and Content Manager*, or *Install from File...* with "
            "`pcbtestkit-%s-kicad-pcm.zip`.\n* **CLI and MCP server:** `pipx install pcbtestkit-%s-py3-none-any.whl`.\n\n"
            "## Check the downloads\n\nEvery file has signed build provenance and a SHA-256 in `SHA256SUMS`; "
            "`pcbtestkit-%s.cdx.json` is the SBOM (CycloneDX).\n\n```bash\nsha256sum -c SHA256SUMS --ignore-missing\n"
            "gh attestation verify pcbtestkit-%s-kicad-pcm.zip --repo %s\n```\n\nSecurity problems: see "
            "[SECURITY.md](https://github.com/%s/blob/v%s/SECURITY.md).\n" % (body, ver, ver, ver, ver, REPO, REPO, ver))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: release_notes.py VERSION")
    sys.stdout.write(notes(sys.argv[1].lstrip("v")))
