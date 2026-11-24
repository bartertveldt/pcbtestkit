"""Read component 3D models (VRML .wrl / STEP .step) to get real body sizes.

Only bounding information is needed: the body's height above the board and
its footprint-local XY extent. VRML from the KiCad library is in 0.1 inch
units; STEP is in millimetres.
"""
from __future__ import annotations

import glob
import json
import math
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

from .geometry import BBox
from .pcb import Footprint, Model3D

_cache: Dict[str, Optional[List[Tuple[float, float, float]]]] = {}
_FLOAT = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_STEP_PT = re.compile(r"#(\d+)\s*=\s*CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*(%s)\s*,\s*(%s)\s*,\s*(%s)\s*\)" % (_FLOAT, _FLOAT, _FLOAT))
_STEP_VTX = re.compile(r"VERTEX_POINT\s*\(\s*'[^']*'\s*,\s*#(\d+)\s*\)")
_STEP_DIR = re.compile(r"#(\d+)\s*=\s*DIRECTION\s*\(\s*'[^']*'\s*,\s*\(\s*(%s)\s*,\s*(%s)\s*,\s*(%s)\s*\)" % (_FLOAT, _FLOAT, _FLOAT))
_STEP_AX = re.compile(r"#(\d+)\s*=\s*AXIS2_PLACEMENT_3D\s*\(\s*'[^']*'\s*,\s*#(\d+)\s*,\s*#(\d+)")
_STEP_CIRC = re.compile(r"CIRCLE\s*\(\s*'[^']*'\s*,\s*#(\d+)\s*,\s*(%s)\s*\)" % _FLOAT)
_WRL_POINT = re.compile(r"point\s*\[([^\]]*)\]", re.S)
_MAX_POINTS = 400000


def _kicad_dirs() -> List[str]:
    """Candidate 3D library roots across platforms and KiCad versions."""
    dirs: List[str] = []
    for k, v in os.environ.items():
        if k.endswith("3DMODEL_DIR") or k in ("KISYS3DMOD",):
            dirs.append(v)
    home = os.path.expanduser("~")
    dirs += glob.glob("/Applications/KiCad*/KiCad.app/Contents/SharedSupport/3dmodels")
    dirs += glob.glob("/Applications/KiCad.app/Contents/SharedSupport/3dmodels")
    dirs += glob.glob("/Volumes/KiCad*/KiCad/KiCad.app/Contents/SharedSupport/3dmodels")
    dirs += glob.glob("C:/Program Files/KiCad/*/share/kicad/3dmodels")
    dirs += ["/usr/share/kicad/3dmodels", "/usr/local/share/kicad/3dmodels", "/usr/share/kicad-packages3D",
             os.path.join(home, ".local/share/kicad/3dmodels"),
             "/var/lib/flatpak/app/org.kicad.KiCad/current/active/files/share/kicad/3dmodels"]
    # user-defined path variables in kicad_common.json
    for pat in (os.path.join(home, "Library/Preferences/kicad/*/kicad_common.json"),
                os.path.join(home, ".config/kicad/*/kicad_common.json"),
                os.path.join(os.environ.get("APPDATA", ""), "kicad/*/kicad_common.json")):
        for f in glob.glob(pat):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    vars_ = (json.load(fh).get("environment") or {}).get("vars") or {}
                dirs += [v for k, v in vars_.items() if "3DMODEL" in k and isinstance(v, str)]
            except (OSError, ValueError):
                pass
    out = []
    for d in dirs:
        if d and os.path.isdir(d) and d not in out:
            out.append(d)
    return out


def resolve_model_path(path: str, project_dir: str) -> Optional[str]:
    if not path:
        return None
    p = path.replace("\\", "/")
    env = dict(os.environ)
    env["KIPRJMOD"] = project_dir
    m = re.match(r"\$\{([^}]+)\}/?(.*)", p) or re.match(r"\$\(([^)]+)\)/?(.*)", p)
    candidates: List[str] = []
    if m:
        var, rest = m.group(1), m.group(2)
        if var in env:
            candidates.append(os.path.join(env[var], rest))
        if var == "KIPRJMOD":
            candidates.append(os.path.join(project_dir, rest))
        else:
            for d in _kicad_dirs():
                candidates.append(os.path.join(d, rest))
    else:
        candidates.append(p if os.path.isabs(p) else os.path.join(project_dir, p))
    expanded = []
    for c in candidates:
        base, ext = os.path.splitext(c)
        expanded.append(c)
        for alt in (".wrl", ".WRL", ".step", ".stp", ".STEP"):
            if alt.lower() != ext.lower():
                expanded.append(base + alt)
    for c in expanded:
        if os.path.isfile(c):
            return c
    return None


def _points(path: str) -> Optional[List[Tuple[float, float, float]]]:
    if path in _cache:
        return _cache[path]
    pts: List[Tuple[float, float, float]] = []
    try:
        with open(path, "r", encoding="latin-1") as fh:
            text = fh.read()
    except OSError:
        _cache[path] = None
        return None
    ext = os.path.splitext(path)[1].lower()
    if ext == ".wrl":
        scale = 2.54
        for blk in _WRL_POINT.findall(text):
            vals = re.findall(_FLOAT, blk)
            for i in range(0, len(vals) - 2, 3):
                pts.append((float(vals[i]) * scale, float(vals[i + 1]) * scale, float(vals[i + 2]) * scale))
                if len(pts) > _MAX_POINTS:
                    break
    else:
        unit = 25.4 if re.search(r"CONVERSION_BASED_UNIT\s*\(\s*'INCH'", text) else 1.0
        allpts = {}
        for m in _STEP_PT.finditer(text):
            allpts[m.group(1)] = (float(m.group(2)) * unit, float(m.group(3)) * unit, float(m.group(4)) * unit)
            if len(allpts) > _MAX_POINTS:
                break
        # Only topological vertices describe the solid; other points (axis placements, surface control
        # points, construction geometry) can lie far outside the part.
        vids = set(_STEP_VTX.findall(text))
        pts = [allpts[i] for i in vids if i in allpts] or list(allpts.values())
        # A full circle has a single vertex, so add each circle's true extent (plane normal from its placement).
        dirs = {m.group(1): (float(m.group(2)), float(m.group(3)), float(m.group(4))) for m in _STEP_DIR.finditer(text)}
        axes = {m.group(1): (m.group(2), m.group(3)) for m in _STEP_AX.finditer(text)}
        for ax, r in _STEP_CIRC.findall(text):
            if ax not in axes:
                continue
            c = allpts.get(axes[ax][0])
            n = dirs.get(axes[ax][1], (0.0, 0.0, 1.0))
            if c is None:
                continue
            r = float(r) * unit
            nn = math.sqrt(sum(v * v for v in n)) or 1.0
            ext = [r * math.sqrt(max(0.0, 1.0 - (v / nn) ** 2)) for v in n]
            for i in range(3):
                for sgn in (-1, 1):
                    q = list(c)
                    q[i] += sgn * ext[i]
                    pts.append(tuple(q))
    _cache[path] = pts or None
    return _cache[path]


def _apply_model_transform(p, m: Model3D):
    """KiCad order: scale, rotate X, Y, Z (negated angles), then offset. Result: 3D frame, Y up."""
    x, y, z = p[0] * m.scale[0], p[1] * m.scale[1], p[2] * m.scale[2]
    rx, ry, rz = (math.radians(-a) for a in m.rotate)
    if rx:
        y, z = y * math.cos(rx) - z * math.sin(rx), y * math.sin(rx) + z * math.cos(rx)
    if ry:
        x, z = x * math.cos(ry) + z * math.sin(ry), -x * math.sin(ry) + z * math.cos(ry)
    if rz:
        x, y = x * math.cos(rz) - y * math.sin(rz), x * math.sin(rz) + y * math.cos(rz)
    return x + m.offset[0], y + m.offset[1], z + m.offset[2]


def model_body(fp: Footprint, project_dir: str) -> Optional[Tuple[BBox, float, str]]:
    """(local XY bbox in footprint coords (y down), height above board, model file) or None."""
    best = None
    for m in fp.models:
        if m.hide:
            continue
        path = resolve_model_path(m.path, project_dir)
        if not path:
            continue
        pts = _points(path)
        if not pts:
            continue
        bb = BBox()
        zmax = -math.inf
        for p in pts:
            x, y, z = _apply_model_transform(p, m)
            if z > zmax:
                zmax = z
            if z > 0.2:            # body above the board surface (ignore leads through the PCB)
                bb.add(x, -y)      # 3D Y up -> board Y down
        if fp.side == "bottom":
            # bottom-side models are mirrored by KiCad; we only need extents for top parts
            pass
        if zmax <= 0 or not bb.valid:
            continue
        # sanity: the body must roughly match the footprint, otherwise the model is mis-scaled/mis-placed
        ref = fp.courtyard_local()
        ref.merge(fp.pads_local_bbox())
        if ref.valid:
            lim = max(ref.w, ref.h) * 3 + 25.0
            if bb.w > lim or bb.h > lim or zmax > 150 or bb.distance_to(ref) > 10.0:
                continue
        if best is None or zmax > best[1]:
            best = (bb, zmax, path)
    return best


def model_height(fp: Footprint, project_dir: str) -> Optional[float]:
    r = model_body(fp, project_dir)
    return round(r[1], 2) if r else None
