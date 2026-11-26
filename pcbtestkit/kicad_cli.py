"""Optional integration with ``kicad-cli`` (KiCad 7+): DRC, ERC, STEP export.

If kicad-cli is not found everything else still works; the report then says
that KiCad's own rule checks were not included.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional

_SEV = {"error": "major", "warning": "minor", "exclusion": "info", "info": "info"}

# subprocess.run options for the tools PCB TestKit calls (kicad-cli, ffmpeg, KiCad's Python): output decoded as
# UTF-8 whatever the Windows code page, and on Windows no console window flashing up for every call made from
# KiCad's GUI (a video makes thousands of kicad-cli calls).
QUIET: Dict[str, Any] = {"encoding": "utf-8", "errors": "replace"}
if os.name == "nt":
    QUIET["creationflags"] = 0x08000000          # CREATE_NO_WINDOW


def sexpr_path(path: str) -> str:
    """A folder or file path to paste into KiCad file text as it is (e.g. for ${KIPRJMOD}): forward slashes,
    which KiCad takes on every OS. A Windows path such as C:\\Users\\builder\\new would otherwise be read with
    \\b and \\n as escape sequences, and the 3D models under it would not load."""
    return path.replace("\\", "/")


def find_kicad_cli() -> Optional[str]:
    env = os.environ.get("KICAD_CLI")
    if env and os.path.exists(env):
        return env
    exe_dir = os.path.dirname(sys.executable)  # inside KiCad's bundled python
    for c in (os.path.join(exe_dir, "kicad-cli"), os.path.join(exe_dir, "kicad-cli.exe"),
              os.path.join(exe_dir, "..", "..", "..", "MacOS", "kicad-cli")):
        if os.path.exists(c):
            return os.path.abspath(c)
    w = shutil.which("kicad-cli")
    if w:
        return w
    pats = ["/Applications/KiCad*/KiCad.app/Contents/MacOS/kicad-cli",
            "/Applications/KiCad.app/Contents/MacOS/kicad-cli",
            "/Volumes/KiCad*/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
            os.path.expanduser("~/Applications/KiCad*/KiCad.app/Contents/MacOS/kicad-cli"),
            "C:/Program Files/KiCad/*/bin/kicad-cli.exe", "/usr/bin/kicad-cli", "/usr/local/bin/kicad-cli"]
    for p in pats:
        hits = sorted(glob.glob(p))
        if hits:
            return hits[-1]
    return None


def kicad_version(cli: str) -> str:
    try:
        return subprocess.run([cli, "version"], capture_output=True, timeout=30, **QUIET).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _run(args: List[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, timeout=timeout, **QUIET)


def run_drc(cli: str, pcb_path: str, parity: bool = True) -> Optional[Dict]:
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "drc.json")
        args = [cli, "pcb", "drc", "--format", "json", "--units", "mm", "--severity-all", "-o", out]
        if parity:
            args.append("--schematic-parity")
        args.append(pcb_path)
        try:
            _run(args)
            if not os.path.exists(out) and parity:
                _run([a for a in args if a != "--schematic-parity"])
            with open(out, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None


def run_erc(cli: str, sch_path: str) -> Optional[Dict]:
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "erc.json")
        try:
            _run([cli, "sch", "erc", "--format", "json", "--units", "mm", "--severity-all", "-o", out, sch_path])
            with open(out, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None


def export_step(cli: str, pcb_path: str, out_path: str) -> bool:
    try:
        r = _run([cli, "pcb", "export", "step", "--subst-models", "--force", "-o", out_path, pcb_path], timeout=900)
        return r.returncode == 0 and os.path.exists(out_path)
    except (OSError, subprocess.SubprocessError):
        return False


def drc_file(cli: str, pcb_path: str) -> Optional[Dict]:
    """DRC without parity, used to validate generated fixture boards."""
    return run_drc(cli, pcb_path, parity=False)


def _group(violations: List[Dict], category: str, prefix: str):
    from .analysis import Finding
    groups: Dict[str, Dict] = {}
    for v in violations:
        t = v.get("type", "unknown")
        g = groups.setdefault(t, {"desc": v.get("description", t), "sev": v.get("severity", "warning"), "items": [], "n": 0})
        g["n"] += 1
        for it in (v.get("items") or [])[:2]:
            pos = it.get("pos") or {}
            g["items"].append({"x": round(float(pos.get("x", 0)), 2), "y": round(float(pos.get("y", 0)), 2),
                               "item": (it.get("description") or "")[:120]})
        if _SEV.get(v.get("severity"), "minor") == "major":
            g["sev"] = "error"
    out = []
    for t, g in groups.items():
        sev = _SEV.get(g["sev"], "minor")
        out.append(Finding("%s-%s" % (prefix, t.upper().replace("_", "-")), sev, category,
                           "KiCad %s: %s (%d)" % (prefix, g["desc"], g["n"]),
                           "Reported by KiCad's own rule checker.", "Fix in KiCad or exclude it there if intended.",
                           items=g["items"], count=g["n"]))
    return out


def kicad_findings(design) -> List:
    from .analysis import Finding
    cli = find_kicad_cli()
    a = design.cfg["analysis"]
    if not cli:
        return [Finding("KICAD-CLI", "info", "drc", "KiCad DRC/ERC not included",
                        "kicad-cli was not found, so KiCad's own design rule and electrical rule checks were skipped.",
                        "Install KiCad 7+ or set the KICAD_CLI environment variable.")]
    out: List = []
    if a.get("run_kicad_drc", True):
        drc = run_drc(cli, design.board.path, parity=design.sch is not None)
        if drc is None:
            out.append(Finding("KICAD-DRC", "info", "drc", "KiCad DRC could not run", "kicad-cli returned no report."))
        else:
            out += _group(drc.get("violations", []), "drc", "DRC")
            unc = drc.get("unconnected_items", [])
            if unc:
                out.append(Finding("DRC-UNCONNECTED", "critical", "drc", "%d unrouted connection(s)" % len(unc),
                                   "The board has ratsnest lines left: these nets are open on every unit.",
                                   "Finish routing before ordering boards.", count=len(unc),
                                   items=[{"x": round(float((u.get("items") or [{}])[0].get("pos", {}).get("x", 0)), 2),
                                           "y": round(float((u.get("items") or [{}])[0].get("pos", {}).get("y", 0)), 2),
                                           "item": ((u.get("items") or [{}])[0].get("description") or "")[:120]} for u in unc]))
            par = drc.get("schematic_parity", [])
            if par:
                out += _group(par, "documentation", "PARITY")
    if a.get("run_kicad_erc", True) and design.sch is not None:
        erc = run_erc(cli, design.sch.root_path)
        if erc is not None:
            viol = []
            for sheet in erc.get("sheets", []):
                viol.extend(sheet.get("violations", []))
            out += _group(viol, "erc", "ERC")
    return out
