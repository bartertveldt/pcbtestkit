#!/usr/bin/env python3
"""A CycloneDX 1.6 SBOM for a PCB TestKit release (standard library only).

    python3 tools/sbom.py --out dist/pcbtestkit-1.2.0.cdx.json dist/*-kicad-pcm.zip dist/*.whl dist/*.tar.gz

It describes PCB TestKit itself (version, licence, where its source and advisories are), states that it has no
dependencies (standard library only), lists every released file with its SHA-256, and lists the programs it can call
but does not ship (KiCad, ffmpeg) as optional external tools. SOURCE_DATE_EPOCH sets the timestamp, so the same
release gives the same SBOM.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = "https://github.com/bartertveldt/pcbtestkit"


def version() -> str:
    sys.path.insert(0, ROOT)
    from pcbtestkit import __version__
    return __version__


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sbom(files, ver: str) -> dict:
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH") or 0) or int(datetime.datetime.now().timestamp())
    stamp = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    purl = "pkg:pypi/pcbtestkit@%s" % ver
    me = {"type": "application", "bom-ref": purl, "name": "pcbtestkit", "version": ver, "purl": purl,
          "description": "KiCad production-test toolkit: KiCad plugin, CLI, MCP server and test runner",
          "licenses": [{"license": {"id": "MIT"}}],
          "externalReferences": [
              {"type": "vcs", "url": REPO + ".git"},
              {"type": "website", "url": REPO},
              {"type": "issue-tracker", "url": REPO + "/issues"},
              {"type": "security-contact", "url": REPO + "/security/advisories/new"},
              {"type": "advisories", "url": REPO + "/security/advisories"},
              {"type": "release-notes", "url": REPO + "/releases/tag/v" + ver}],
          "properties": [{"name": "pcbtestkit:runtime", "value": "Python >= 3.9, standard library only"},
                         {"name": "pcbtestkit:runtime-dependencies", "value": "none"}]}
    comps = []
    for f in files:
        name = os.path.basename(f)
        comps.append({"type": "file", "bom-ref": "file:" + name, "name": name, "version": ver,
                      "hashes": [{"alg": "SHA-256", "content": sha256(f)}]})
    tools = [("kicad", "KiCad / kicad-cli", "GPL-3.0-or-later", "https://www.kicad.org"),
             ("ffmpeg", "ffmpeg", "LGPL-2.1-or-later", "https://ffmpeg.org")]
    for ref, name, lic, url in tools:
        comps.append({"type": "application", "bom-ref": "external:" + ref, "name": name, "scope": "optional",
                      "description": "called if installed, not shipped", "licenses": [{"license": {"id": lic}}],
                      "externalReferences": [{"type": "website", "url": url}]})
    return {
        "bomFormat": "CycloneDX", "specVersion": "1.6",
        "serialNumber": "urn:uuid:%s" % uuid.uuid5(uuid.NAMESPACE_URL, "%s/releases/v%s" % (REPO, ver)),
        "version": 1,
        "metadata": {"timestamp": stamp, "lifecycles": [{"phase": "build"}],
                     "tools": {"components": [{"type": "application", "name": "pcbtestkit tools/sbom.py", "version": ver}]},
                     "component": me, "licenses": [{"license": {"id": "MIT"}}]},
        "components": comps,
        "dependencies": [{"ref": purl, "dependsOn": []}] + [{"ref": c["bom-ref"], "dependsOn": []} for c in comps],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="*", help="the released files")
    ap.add_argument("--out", required=True)
    ap.add_argument("--version", help="default: pcbtestkit.__version__")
    a = ap.parse_args(argv)
    missing = [f for f in a.files if not os.path.isfile(f)]
    if missing:
        print("not found: %s" % ", ".join(missing), file=sys.stderr)
        return 2
    doc = sbom(sorted(a.files, key=os.path.basename), a.version or version())
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    print("%s: %d file(s)" % (a.out, len(a.files)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
