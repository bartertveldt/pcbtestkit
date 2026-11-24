#!/usr/bin/env python3
"""Build the KiCad Plugin and Content Manager (PCM) package and the metadata to submit to KiCad's repository.

    python3 tools/build_pcm.py            build dist/pcbtestkit-<version>-kicad-pcm.zip (+ repository entry)
    python3 tools/build_pcm.py --check    only validate metadata and files, write nothing
    python3 tools/build_pcm.py --submission v1.0.0
                                          prepare the merge request for KiCad's official list from the
                                          published GitHub release (writes dist/kicad-submission/, sends nothing)

Outputs:
  dist/pcbtestkit-<v>-kicad-pcm.zip                       install with KiCad: Plugin and Content Manager >
                                                          Install from File...
  dist/pcm-repository/packages/<identifier>/metadata.json  the file for a merge request to
  dist/pcm-repository/packages/<identifier>/icon.png       https://gitlab.com/kicad/addons/metadata

Zip layout (KiCad PCM rules): metadata.json, plugins/ (the action plugin package), resources/icon.png (64x64).
KiCad extracts plugins/ to <KiCad user dir>/<ver>/3rdparty/plugins/<identifier with . -> _>/ and imports it as
a package, which is why every import inside the plugin is relative.
Standard library only; the checks mirror https://go.kicad.org/pcm/schemas/v1.
"""
import argparse
import hashlib
import json
import os
import re
import struct
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PLACEHOLDER = "YOUR-GITHUB-USER"
# what goes into plugins/: the SWIG action plugin package (KiCad 8-10)
PLUGIN_FILES = ["__init__.py", "LICENSE", "plugin", "pcbtestkit", "resources"]
SKIP_DIRS = {"__pycache__", ".pcbtestkit", "production_test"}
SKIP_EXT = {".pyc", ".pyo"}

LICENSES = {"MIT", "GPL-2.0", "GPL-3.0", "LGPL-2.1", "LGPL-3.0", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause",
            "MPL-1.1", "ISC", "Zlib", "Unlicense", "CC0-1.0", "open-source"}   # subset of the schema enum
RE_ID = re.compile(r"^[a-zA-Z][-a-zA-Z0-9.]{0,98}[a-zA-Z0-9]$")
RE_TAG = re.compile(r"^[a-z][-a-z0-9]{0,48}[a-z0-9]$")
RE_VER = re.compile(r"^\d{1,4}(\.\d{1,4}(\.\d{1,6})?)?$")
RE_KICAD = re.compile(r"^\d{1,2}(\.\d{1,2}(\.\d{1,2})?)?$")
RE_KEY = re.compile(r"^[a-zA-Z][-a-zA-Z0-9 ]{0,48}[a-zA-Z0-9]$")


def version() -> str:
    from pcbtestkit import __version__
    return __version__


def load_metadata(ver: str) -> dict:
    with open(os.path.join(ROOT, "pcm", "metadata.template.json"), encoding="utf-8") as fh:
        meta = json.load(fh)
    for v in meta["versions"]:
        if v.get("version") == "VERSION":
            v["version"] = ver
    return meta


def png_size(path):
    with open(path, "rb") as fh:
        head = fh.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">II", head[16:24])


def validate(meta: dict, icon: str) -> list:
    """Return a list of problems (empty = valid)."""
    errs = []
    for k in ("name", "description", "description_full", "identifier", "type", "author", "license", "resources", "versions"):
        if k not in meta:
            errs.append("missing required field %s" % k)
    if len(meta.get("name", "")) > 200:
        errs.append("name longer than 200 characters")
    if len(meta.get("description", "")) > 150:
        errs.append("description is %d characters; KiCad's docs allow 150" % len(meta["description"]))
    if len(meta.get("description_full", "")) > 5000:
        errs.append("description_full longer than 5000 characters")
    ident = meta.get("identifier", "")
    if not RE_ID.match(ident):
        errs.append("identifier %r does not match %s" % (ident, RE_ID.pattern))
    if len(ident) > 50:
        errs.append("identifier longer than 50 characters (KiCad docs)")
    if meta.get("type") != "plugin":
        errs.append("type must be plugin")
    if meta.get("license") not in LICENSES:
        errs.append("license %r not in the PCM license list" % meta.get("license"))
    for who in ("author", "maintainer"):
        c = meta.get(who)
        if c is None:
            continue
        if not c.get("name") or not isinstance(c.get("contact"), dict):
            errs.append("%s needs name and contact" % who)
        for k in (c.get("contact") or {}):
            if not re.match(r"^[a-z][-a-z0-9 ]{0,48}[a-z0-9]$", k):
                errs.append("%s.contact key %r invalid" % (who, k))
    for k, v in (meta.get("resources") or {}).items():
        if not RE_KEY.match(k) or not isinstance(v, str) or len(v) > 500:
            errs.append("resources.%s invalid" % k)
    tags = meta.get("tags") or []
    if len(set(tags)) != len(tags):
        errs.append("duplicate tags")
    for t in tags:
        if not RE_TAG.match(t):
            errs.append("tag %r invalid" % t)
    for v in meta.get("versions") or []:
        if not RE_VER.match(str(v.get("version", ""))):
            errs.append("version %r invalid" % v.get("version"))
        if v.get("status") not in ("stable", "testing", "development", "deprecated"):
            errs.append("status %r invalid" % v.get("status"))
        if not RE_KICAD.match(str(v.get("kicad_version", ""))):
            errs.append("kicad_version %r invalid" % v.get("kicad_version"))
        if v.get("runtime") not in (None, "swig", "ipc"):
            errs.append("runtime %r invalid" % v.get("runtime"))
        for k in ("download_url", "download_sha256", "download_size"):
            if k in v:
                errs.append("%s must not be inside the package's own metadata.json" % k)
    size = png_size(icon) if os.path.exists(icon) else None
    if size != (64, 64):
        errs.append("pcm/icon.png must be a 64x64 PNG (got %s)" % (size,))
    return errs


RE_SHA = re.compile(r"^[a-f0-9]{64}$")
DOWNLOAD_KEYS = ("download_url", "download_sha256", "download_size", "install_size")
METADATA_REPO_RAW = "https://gitlab.com/kicad/addons/metadata/-/raw/main/packages/%s/metadata.json"


def _fetch(url: str, missing_ok: bool = False):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "pcbtestkit-build-pcm"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if missing_ok and e.code == 404:
            return None
        raise SystemExit("cannot download %s: HTTP %d" % (url, e.code))


def check_archive(data: bytes, version_: str, ident: str) -> list:
    """KiCad's archive rules: only metadata.json, plugins/ and resources/; the inner metadata has exactly one
    version (this one) and no download fields."""
    import io
    errs = []
    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()
    extra = sorted({n.split("/")[0] for n in names} - {"metadata.json", "plugins", "resources"})
    if extra:
        errs.append("archive has entries KiCad does not allow at the top level: %s" % ", ".join(extra))
    if "plugins/__init__.py" not in names:
        errs.append("archive has no plugins/__init__.py")
    inner = json.loads(z.read("metadata.json"))
    if inner.get("identifier") != ident:
        errs.append("archive metadata identifier %r != %r" % (inner.get("identifier"), ident))
    vers = inner.get("versions") or []
    if len(vers) != 1 or vers[0].get("version") != version_:
        errs.append("archive metadata must hold exactly one version, %s" % version_)
    if any(k in v for v in vers for k in DOWNLOAD_KEYS[:3]):
        errs.append("archive metadata must not contain download fields")
    return errs


def submission(tag: str, out_dir: str) -> dict:
    """Build packages/<identifier>/ for a merge request to https://gitlab.com/kicad/addons/metadata from the
    published release: its zip and metadata are downloaded and verified, and versions KiCad already lists are
    kept (an update adds the new version on top). Nothing is uploaded."""
    ver = tag.lstrip("v")
    local = load_metadata(ver)
    ident = local["identifier"]
    if PLACEHOLDER in json.dumps(local):
        raise SystemExit("run tools/set_owner.py first")
    owner = ident.split(".")[2]
    base = "https://github.com/%s/pcbtestkit/releases/download/%s/" % (owner, tag)
    entry = json.loads(_fetch(base + "kicad-pcm-metadata.json"))
    new = entry["versions"][0]
    errs = []
    if new.get("version") != ver:
        errs.append("release metadata is for version %s, not %s" % (new.get("version"), ver))
    data = _fetch(new["download_url"])
    sha = hashlib.sha256(data).hexdigest()
    if not str(new["download_url"]).startswith(base):
        errs.append("download_url is not this release's asset: %s" % new["download_url"])
    if not RE_SHA.match(str(new.get("download_sha256", ""))) or sha != new["download_sha256"]:
        errs.append("download_sha256 does not match the published zip (%s)" % sha)
    if len(data) != new.get("download_size"):
        errs.append("download_size %s != %d bytes" % (new.get("download_size"), len(data)))
    if not isinstance(new.get("install_size"), int) or new["install_size"] <= 0:
        errs.append("install_size missing")
    errs += check_archive(data, ver, ident)
    bare = json.loads(json.dumps(entry))
    for v in bare["versions"]:
        for k in DOWNLOAD_KEYS:
            v.pop(k, None)
    errs += validate(bare, os.path.join(ROOT, "pcm", "icon.png"))
    old = _fetch(METADATA_REPO_RAW % ident, missing_ok=True)
    listed = []
    if old:
        listed = [v for v in json.loads(old).get("versions") or [] if v.get("version") != ver]
    entry["versions"] = [new] + listed
    if errs:
        raise SystemExit("not ready to submit:\n  " + "\n  ".join(errs))
    pdir = os.path.join(out_dir, "packages", ident)
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, "metadata.json"), "w", encoding="utf-8") as fh:
        json.dump(entry, fh, indent=2)
        fh.write("\n")
    with open(os.path.join(ROOT, "pcm", "icon.png"), "rb") as src, open(os.path.join(pdir, "icon.png"), "wb") as dst:
        dst.write(src.read())
    kind = "Update" if listed else "Add"
    mr = os.path.join(out_dir, "MERGE_REQUEST.md")
    with open(mr, "w", encoding="utf-8") as fh:
        fh.write(MR_TEXT.format(kind=kind, name=entry["name"], ident=ident, ver=ver, owner=owner, tag=tag,
                                license=entry["license"], kicad=new["kicad_version"], sha=sha))
    return {"dir": pdir, "merge_request": mr, "kind": kind, "versions": [v["version"] for v in entry["versions"]],
            "sha256": sha, "download_size": len(data)}


MR_TEXT = """Title: {kind} {name} {ver} ({ident})

{name} turns a KiCad board into a production-test kit: weak-point (DFM) analysis, fab class checks, a test
scheme, a pogo-pin fixture PCB (or a probe-point list for your own fixture), 3D-printable solder jigs and an
HTML guide. It runs locally with the Python standard library and KiCad's own kicad-cli; it needs no network
access and no AI service (an optional MCP server lets AI assistants drive it).

- Source and issues: https://github.com/{owner}/pcbtestkit (issues: https://github.com/{owner}/pcbtestkit/issues)
- License: {license}
- Package: action plugin (SWIG runtime), KiCad {kicad} and later; release {tag},
  sha256 {sha}
- Writes only inside the project folder: the output folder, pcbtestkit.json and the .pcbtestkit/ undo log.

Tested on: <fill in: KiCad versions and operating systems you installed the release zip on>
"""


def plugin_files():
    for top in PLUGIN_FILES:
        src = os.path.join(ROOT, top)
        if os.path.isfile(src):
            yield src, top
            continue
        for dirpath, dirnames, filenames in os.walk(src):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for f in sorted(filenames):
                if os.path.splitext(f)[1] in SKIP_EXT or f == ".DS_Store":
                    continue
                full = os.path.join(dirpath, f)
                yield full, os.path.relpath(full, ROOT).replace(os.sep, "/")


def build(out_dir: str) -> dict:
    ver = version()
    meta = load_metadata(ver)
    icon = os.path.join(ROOT, "pcm", "icon.png")
    errs = validate(meta, icon)
    if errs:
        raise SystemExit("metadata problems:\n  " + "\n  ".join(errs))
    os.makedirs(out_dir, exist_ok=True)
    zpath = os.path.join(out_dir, "pcbtestkit-%s-kicad-pcm.zip" % ver)
    install_size = 0
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("metadata.json", json.dumps(meta, indent=2) + "\n")
        install_size += len(json.dumps(meta, indent=2)) + 1
        for full, rel in plugin_files():
            z.write(full, "plugins/" + rel)
            install_size += os.path.getsize(full)
        z.write(icon, "resources/icon.png")
        install_size += os.path.getsize(icon)
    with open(zpath, "rb") as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()
    entry = json.loads(json.dumps(meta))
    owner = meta["identifier"].split(".")[2] if meta["identifier"].startswith("com.github.") else PLACEHOLDER
    entry["versions"][0].update({
        "download_url": "https://github.com/%s/pcbtestkit/releases/download/v%s/%s" % (owner, ver, os.path.basename(zpath)),
        "download_sha256": sha, "download_size": os.path.getsize(zpath), "install_size": install_size})
    rdir = os.path.join(out_dir, "pcm-repository", "packages", meta["identifier"])
    os.makedirs(rdir, exist_ok=True)
    with open(os.path.join(rdir, "metadata.json"), "w", encoding="utf-8") as fh:
        json.dump(entry, fh, indent=2)
        fh.write("\n")
    with open(icon, "rb") as src, open(os.path.join(rdir, "icon.png"), "wb") as dst:
        dst.write(src.read())
    return {"zip": zpath, "sha256": sha, "download_size": os.path.getsize(zpath), "install_size": install_size,
            "repository_entry": os.path.join(rdir, "metadata.json"), "placeholder": PLACEHOLDER in json.dumps(entry)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--check", action="store_true", help="validate only")
    ap.add_argument("--submission", metavar="TAG", help="prepare the KiCad merge request from a published release")
    a = ap.parse_args()
    if a.submission:
        res = submission(a.submission, os.path.join(a.out, "kicad-submission"))
        print("%s merge request ready (versions %s), nothing was sent:" % (res["kind"], ", ".join(res["versions"])))
        print("  %s" % res["dir"])
        print("  %s" % res["merge_request"])
        print("release zip verified: %d bytes, sha256 %s" % (res["download_size"], res["sha256"]))
        return 0
    if a.check:
        meta = load_metadata(version())
        errs = validate(meta, os.path.join(ROOT, "pcm", "icon.png"))
        for e in errs:
            print("ERROR", e)
        if PLACEHOLDER in json.dumps(meta):
            print("NOTE  metadata still has %s; run tools/set_owner.py before a release" % PLACEHOLDER)
        print("OK" if not errs else "FAILED")
        return 1 if errs else 0
    res = build(a.out)
    print("package    %s (%d bytes, sha256 %s)" % (res["zip"], res["download_size"], res["sha256"]))
    print("repository %s" % res["repository_entry"])
    if res["placeholder"]:
        print("NOTE: the repository entry still contains %s - run tools/set_owner.py --owner <github-user>, "
              "then rebuild before submitting." % PLACEHOLDER)
    return 0


if __name__ == "__main__":
    sys.exit(main())
