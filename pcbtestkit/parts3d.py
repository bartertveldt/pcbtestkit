"""The real 3D shape of every part, so the jigs fit them exactly.

Where the shapes come from, best first:

  kicad   KiCad's own 3D export (``kicad-cli pcb export glb``, KiCad 8+): every model, STEP or VRML, placed exactly
          as KiCad's 3D viewer and its STEP / STL exports show it. One export per board file, cached.
  vrml    the part's .wrl model, read here (KiCad 6 and 7 libraries reference VRML models).
  box     a box: the model's bounding box (a STEP model without kicad-cli) or, with no model, the courtyard and an
          estimated height. The jig notes name these parts: their fit can't be guaranteed.

Frame: board coordinates (x, y as in KiCad, y down) and z above the board's bottom face (its top face is at the
board thickness). :func:`in_view` turns a mesh over with the board (:mod:`pcbtestkit.sides`).

:func:`envelope` gives, for every grid cell, the highest and the lowest point of a part over the cell's square grown
by a clearance. It clips every triangle to that square, so it is exact: a jig cut to an envelope never reaches into
the part, and touches it exactly where the part is highest (or lowest).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
from array import array
from typing import Any, Dict, List, Optional, Tuple

Tri = Tuple[float, float, float, float, float, float, float, float, float]
GLB_REV = 3


class PartMesh:
    """One part's triangles in board coordinates (z above the bottom face) and where they came from."""

    def __init__(self, ref: str, tris: List[Tri], source: str, note: str = ""):
        self.ref, self.tris, self.source, self.note = ref, tris, source, note
        xs = [v for t in tris for v in (t[0], t[3], t[6])]
        ys = [v for t in tris for v in (t[1], t[4], t[7])]
        zs = [v for t in tris for v in (t[2], t[5], t[8])]
        self.bbox = (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs)) if tris else None

    @property
    def exact(self) -> bool:
        return self.source in ("kicad", "vrml")


# =========================================================================== #
# KiCad's 3D export (binary glTF)
# =========================================================================== #
_COMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
_FMT = {5126: "f", 5125: "I", 5123: "H", 5121: "B", 5122: "h", 5120: "b"}


def _mat_mul(a: List[float], b: List[float]) -> List[float]:
    return [sum(a[r * 4 + k] * b[k * 4 + c] for k in range(4)) for r in range(4) for c in range(4)]


def _node_matrix(n: Dict[str, Any]) -> List[float]:
    """The node's local transform, row-major 4x4."""
    if "matrix" in n:
        m = n["matrix"]                          # glTF: column-major
        return [m[c * 4 + r] for r in range(4) for c in range(4)]
    tx, ty, tz = n.get("translation", (0.0, 0.0, 0.0))
    qx, qy, qz, qw = n.get("rotation", (0.0, 0.0, 0.0, 1.0))
    sx, sy, sz = n.get("scale", (1.0, 1.0, 1.0))
    r = [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw),
         2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw),
         2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy)]
    return [r[0] * sx, r[1] * sy, r[2] * sz, tx, r[3] * sx, r[4] * sy, r[5] * sz, ty,
            r[6] * sx, r[7] * sy, r[8] * sz, tz, 0.0, 0.0, 0.0, 1.0]


def _accessor(js: Dict[str, Any], blob: bytes, idx: int) -> List[float]:
    a = js["accessors"][idx]
    bv = js["bufferViews"][a["bufferView"]]
    n = _COMP[a["type"]]
    fmt = _FMT[a["componentType"]]
    size = struct.calcsize("<" + fmt)
    start = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
    stride = bv.get("byteStride") or size * n
    if stride == size * n:
        arr = array(fmt)
        arr.frombytes(blob[start:start + size * n * a["count"]])
        if sys.byteorder == "big":
            arr.byteswap()
        return list(arr)
    out: List[float] = []
    for k in range(a["count"]):
        out.extend(struct.unpack_from("<%d%s" % (n, fmt), blob, start + k * stride))
    return out


def read_glb(path: str, names: Optional[set] = None,
             merged: bool = False) -> List[Tuple[str, Tuple[float, float, float], List[Tri]]]:
    """The named nodes of a binary glTF file (KiCad names each footprint's node by its reference) as
    (name, origin, triangles), everything in the file's frame and units. ``names``: only these (None: all).
    ``merged``: every triangle of the scene as one entry ("", origin 0)."""
    with open(path, "rb") as fh:
        data = fh.read()
    magic, _ver, length = struct.unpack_from("<III", data, 0)
    if magic != 0x46546C67:
        raise ValueError("not a binary glTF file")
    off, js, blob = 12, None, b""
    while off + 8 <= min(length, len(data)):
        clen, ctype = struct.unpack_from("<II", data, off)
        chunk = data[off + 8:off + 8 + clen]
        off += 8 + clen
        if ctype == 0x4E4F534A:
            js = json.loads(chunk.decode("utf-8"))
        elif ctype == 0x004E4942:
            blob = chunk
    if js is None:
        raise ValueError("glTF file without JSON")
    nodes = js.get("nodes", [])
    out: List[Tuple[str, Tuple[float, float, float], List[Tri]]] = []
    mesh_tris: Dict[int, List[Tuple[float, ...]]] = {}

    def local_tris(mi: int) -> List[Tuple[float, ...]]:
        if mi not in mesh_tris:
            tl: List[Tuple[float, ...]] = []
            for prim in js["meshes"][mi].get("primitives", []):
                if prim.get("mode", 4) != 4 or "POSITION" not in prim.get("attributes", {}):
                    continue
                pos = _accessor(js, blob, prim["attributes"]["POSITION"])
                idx = _accessor(js, blob, prim["indices"]) if "indices" in prim else list(range(len(pos) // 3))
                for k in range(0, len(idx) - 2, 3):
                    a, b, c = idx[k] * 3, idx[k + 1] * 3, idx[k + 2] * 3
                    tl.append((pos[a], pos[a + 1], pos[a + 2], pos[b], pos[b + 1], pos[b + 2],
                               pos[c], pos[c + 1], pos[c + 2]))
            mesh_tris[mi] = tl
        return mesh_tris[mi]

    def collect(ni: int, m: List[float], acc: List[Tri]) -> None:
        nd = nodes[ni]
        w = _mat_mul(m, _node_matrix(nd))
        if "mesh" in nd:
            for t in local_tris(nd["mesh"]):
                p = []
                for k in (0, 3, 6):
                    x, y, z = t[k], t[k + 1], t[k + 2]
                    p += [w[0] * x + w[1] * y + w[2] * z + w[3], w[4] * x + w[5] * y + w[6] * z + w[7],
                          w[8] * x + w[9] * y + w[10] * z + w[11]]
                acc.append(tuple(p))           # type: ignore[arg-type]
        for c in nd.get("children", []):
            collect(c, w, acc)

    def walk(ni: int, m: List[float]) -> None:
        nd = nodes[ni]
        w = _mat_mul(m, _node_matrix(nd))
        name = nd.get("name")
        if name and (names is None or name in names) and not name.startswith("=>"):
            acc: List[Tri] = []
            collect(ni, m, acc)
            out.append((name, (w[3], w[7], w[11]), acc))
            return
        for c in nd.get("children", []):
            walk(c, w)

    ident = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
    scenes = js.get("scenes") or [{"nodes": list(range(len(nodes)))}]
    if merged:
        acc: List[Tri] = []
        for root in scenes[js.get("scene", 0)].get("nodes", []):
            collect(root, ident, acc)
        return [("", (0.0, 0.0, 0.0), acc)]
    for root in scenes[js.get("scene", 0)].get("nodes", []):
        walk(root, ident)
    return out


def _cache_dir() -> str:
    from .settings import home
    return os.path.join(home(), "cache", "parts3d")


_MODEL = re.compile(r'\(model\s+("(?:[^"\\]|\\.)*"|[^\s()]+)')


def model_matches(path: str, patterns: List[str]) -> bool:
    """Does a 3D model path match one of the patterns (file name or full path, wildcards, any case)?"""
    import fnmatch
    p = path.replace("\\", "/").lower()
    base = p.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(base, q.lower()) or fnmatch.fnmatchcase(p, q.lower()) for q in patterns if q)


def board_for_export(text: str, project_dir: str, patterns: List[str]) -> str:
    """The board file's text for KiCad's 3D export: the 3D models matching ``patterns`` removed and every other
    model path replaced by the file it resolves to (:func:`pcbtestkit.models3d.resolve_model_path`): ${KIPRJMOD}
    and relative paths made absolute (the copy is exported from another folder), the 3D library variables of older
    KiCad versions (KICAD8_3DMODEL_DIR ...) mapped to the installed library, and a .wrl that no longer exists
    replaced by the STEP model of the same name (KiCad 9+ ship STEP models only)."""
    from .models3d import resolve_model_path
    out, pos = [], 0
    for m in _MODEL.finditer(text):
        raw = m.group(1)
        path = raw[1:-1].replace('\\"', '"') if raw.startswith('"') else raw
        start, depth, i = m.start(), 0, m.start()
        while i < len(text):
            ch = text[i]
            if ch == '"':
                i += 1
                while i < len(text) and text[i] != '"':
                    i += 2 if text[i] == "\\" else 1
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out.append(text[pos:start])
        pos = i + 1
        if model_matches(path, patterns):
            continue
        new = resolve_model_path(path, project_dir)
        if not new:
            pd = project_dir.replace("\\", "/")
            new = path.replace("${KIPRJMOD}", pd).replace("$(KIPRJMOD)", pd)
            if not new.startswith("$") and not os.path.isabs(new) and not re.match(r"[A-Za-z]:/", new):
                new = pd + "/" + new
        new = new.replace("\\", "/")
        out.append(text[start:m.start(1)] + '"%s"' % new.replace('"', '\\"') + text[m.end(1):pos])
    out.append(text[pos:])
    return "".join(out)


def kicad_export(pcb_path: str, refs: Optional[List[str]] = None, log: Optional[List[str]] = None,
                 drop_models: Optional[List[str]] = None, body: bool = False) -> Optional[str]:
    """KiCad's 3D export of the board's parts (no board body) as a cached .glb, or None (no kicad-cli 8+, or the
    export failed). ``drop_models``: 3D model file patterns left out; ``body``: the board body alone instead. The
    cache key is the board file's content and the KiCad version."""
    from .kicad_cli import QUIET, find_kicad_cli, kicad_version
    if os.environ.get("PCBTESTKIT_NO_KICAD_3D"):
        return None
    cli = find_kicad_cli()
    if not cli:
        return None
    try:
        with open(pcb_path, "rb") as fh:
            text_b = fh.read()
    except OSError:
        return None
    src_text = board_for_export(text_b.decode("utf-8", "replace"), os.path.dirname(os.path.abspath(pcb_path)),
                                drop_models or [])
    text_b = src_text.encode("utf-8")
    key = hashlib.sha1(text_b + ("|%s|%d|%s|%s" % (kicad_version(cli), GLB_REV, ",".join(sorted(refs or [])),
                                                   "body" if body else "parts")).encode())
    out = os.path.join(_cache_dir(), key.hexdigest()[:24] + ".glb")
    if os.path.exists(out):
        return out
    os.makedirs(os.path.dirname(out), exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".glb", dir=os.path.dirname(out))
    os.close(fd)
    src = tmp[:-4] + ".kicad_pcb"
    with open(src, "w", encoding="utf-8") as fh:
        fh.write(src_text)
    # --subst-models: a .wrl model is read from the STEP model of the same name where there is one (exact shapes)
    args = [cli, "pcb", "export", "glb", "--force", "--no-components" if body else "--no-board-body", "--subst-models",
            "-o", tmp]
    if refs:
        args += ["--component-filter", ",".join(refs)]
    r = None
    try:
        r = subprocess.run(args + [src], capture_output=True, timeout=900, cwd=os.path.dirname(pcb_path) or None,
                           **QUIET)
        ok = r.returncode == 0 and os.path.getsize(tmp) > 20
    except (OSError, subprocess.SubprocessError) as e:
        ok = False
        if log is not None:
            log.append("kicad-cli 3D export failed: %s" % e)
    finally:
        if src != pcb_path:
            try:
                os.remove(src)
            except OSError:
                pass
    if not ok:
        if log is not None and r is not None:
            log.append("kicad-cli 3D export failed: %s" % (r.stderr or r.stdout or "").strip()[-300:])
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None
    os.replace(tmp, out)
    return out


def seat_planes(design: Any, nodes, wanted: Dict[str, List[Any]]) -> Tuple[float, float, float, float]:
    """(dx, dy, top seat, bottom seat) of a KiCad 3D export: the shift from the export's x / z to board x / y, and
    the heights (mm) where KiCad seats top- and bottom-side models. Taken from the parts whose models have no
    offset of their own (a model's offset is part of its node's transform); KiCad's usual values otherwise."""
    t = float(design.board.thickness)
    dxs, dys, tops, bots = [], [], [], []
    for name, (ox, oy, oz), _ in nodes:
        fps = wanted.get(name) or []
        if len(fps) != 1 or any(any(abs(v) > 1e-9 for v in m.offset) for m in fps[0].models):
            continue
        dxs.append(fps[0].x - ox * 1000.0)
        dys.append(fps[0].y - oz * 1000.0)
        (tops if fps[0].side == "top" else bots).append(oy * 1000.0)

    def med(v, default):
        v = sorted(v)
        return v[len(v) // 2] if v else default
    return med(dxs, 0.0), med(dys, 0.0), med(tops, t - 0.005), med(bots, -0.085)


def _from_glb(design: Any, path: str, wanted: Dict[str, List[Any]]) -> Dict[int, PartMesh]:
    """Meshes of the wanted footprints (ref -> footprints with that ref) from KiCad's export, in board frame: a
    top-side model's seating plane at the board's top face (z = thickness), a bottom-side one's at z = 0."""
    t = float(design.board.thickness)
    nodes = read_glb(path, set(wanted))
    dx, dy, top_seat, bot_seat = seat_planes(design, nodes, wanted)
    design._kicad_seats = (top_seat, bot_seat)
    design._kicad_shift = (dx, dy)
    taken: Dict[int, PartMesh] = {}
    for name, _origin, tris in nodes:
        fps = [f for f in (wanted.get(name) or []) if id(f) not in taken]
        if not fps or not tris:
            continue
        if len(fps) > 1:                 # a reference used twice: the footprint nearest to the shape
            xs = [a[k] * 1000.0 + dx for a in tris for k in (0, 3, 6)]
            ys = [a[k] * 1000.0 + dy for a in tris for k in (2, 5, 8)]
            cx, cy = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
            fps.sort(key=lambda f: math.hypot(f.x - cx, f.y - cy))
        fp = fps[0]
        zs = (t - top_seat) if fp.side == "top" else -bot_seat
        conv: List[Tri] = []
        for a in tris:
            conv.append((a[0] * 1000.0 + dx, a[2] * 1000.0 + dy, a[1] * 1000.0 + zs,
                         a[3] * 1000.0 + dx, a[5] * 1000.0 + dy, a[4] * 1000.0 + zs,
                         a[6] * 1000.0 + dx, a[8] * 1000.0 + dy, a[7] * 1000.0 + zs))
        taken[id(fp)] = PartMesh(fp.ref, conv, "kicad")
    return taken


# =========================================================================== #
# VRML models
# =========================================================================== #
_FLOAT = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_vrml_cache: Dict[str, Optional[List[Tri]]] = {}


def read_vrml(path: str) -> Optional[List[Tri]]:
    """Triangles of a VRML 2 model (IndexedFaceSet shapes, KiCad's 0.1 inch units -> mm), model frame."""
    if path in _vrml_cache:
        return _vrml_cache[path]
    try:
        with open(path, "r", encoding="latin-1") as fh:
            text = fh.read()
    except OSError:
        _vrml_cache[path] = None
        return None
    tris: List[Tri] = []
    s = 2.54
    for m in re.finditer(r"IndexedFaceSet\s*\{", text):
        i, depth = m.end(), 1
        while i < len(text) and depth:
            ch = text[i]
            depth += 1 if ch == "{" else -1 if ch == "}" else 0
            i += 1
        blk = text[m.end():i]
        cm = re.search(r"coordIndex\s*\[([^\]]*)\]", blk)
        pm = re.search(r"point\s*\[([^\]]*)\]", blk)
        if not cm or not pm:
            continue
        v = [float(x) * s for x in re.findall(_FLOAT, pm.group(1))]
        pts = [(v[k], v[k + 1], v[k + 2]) for k in range(0, len(v) - 2, 3)]
        face: List[int] = []
        for tok in re.findall(r"-?\d+", cm.group(1)) + ["-1"]:
            k = int(tok)
            if k >= 0:
                face.append(k)
                continue
            if len(face) >= 3 and all(f < len(pts) for f in face):
                a = pts[face[0]]
                for q in range(1, len(face) - 1):
                    b, c = pts[face[q]], pts[face[q + 1]]
                    tris.append((a[0], a[1], a[2], b[0], b[1], b[2], c[0], c[1], c[2]))
            face = []
    _vrml_cache[path] = tris or None
    return _vrml_cache[path]


def _model_to_board(fp: Any, m: Any, t: float, p: Tuple[float, float, float]) -> Tuple[float, float, float]:
    """A model point (mm, model frame) -> board coordinates with z above the bottom face (KiCad's placement:
    the model transform, then the footprint's rotation; bottom parts turned about the X axis)."""
    from .models3d import _apply_model_transform
    x, y, z = _apply_model_transform(p, m)          # footprint 3D frame, Y up
    lx, ly = x, -y                                  # footprint local, y down
    if fp.side == "bottom":
        ly, z = -ly, -z
    bx, by = fp.to_board(lx, ly)
    return bx, by, (t + z) if fp.side == "top" else z


def _from_vrml(design: Any, fp: Any, drop: List[str]) -> Optional[PartMesh]:
    from .models3d import resolve_model_path
    t = float(design.board.thickness)
    tris: List[Tri] = []
    for m in fp.models:
        if m.hide or model_matches(m.path, drop):
            continue
        path = resolve_model_path(m.path, design.project_dir)
        if not path or not path.lower().endswith(".wrl"):
            base = os.path.splitext(path or "")[0]
            path = next((base + e for e in (".wrl", ".WRL") if base and os.path.isfile(base + e)), None)
        if not path:
            continue
        mt = read_vrml(path)
        if not mt:
            continue
        for a in mt:
            p0 = _model_to_board(fp, m, t, (a[0], a[1], a[2]))
            p1 = _model_to_board(fp, m, t, (a[3], a[4], a[5]))
            p2 = _model_to_board(fp, m, t, (a[6], a[7], a[8]))
            tris.append(p0 + p1 + p2)                # type: ignore[arg-type]
    return PartMesh(fp.ref, tris, "vrml") if tris else None


def box_mesh(ref: str, corners: List[Tuple[float, float]], z0: float, z1: float, source: str = "box",
             note: str = "") -> PartMesh:
    """A prism over a convex quadrilateral (board coordinates) from z0 to z1."""
    a, b, c, d = corners
    lo = [(p[0], p[1], z0) for p in (a, b, c, d)]
    hi = [(p[0], p[1], z1) for p in (a, b, c, d)]
    tris: List[Tri] = []

    def tri(p, q, r):
        tris.append((p[0], p[1], p[2], q[0], q[1], q[2], r[0], r[1], r[2]))
    tri(lo[0], lo[1], lo[2]), tri(lo[0], lo[2], lo[3]), tri(hi[0], hi[2], hi[1]), tri(hi[0], hi[3], hi[2])
    for k in range(4):
        n = (k + 1) % 4
        tri(lo[k], lo[n], hi[n]), tri(lo[k], hi[n], hi[k])
    return PartMesh(ref, tris, source, note)


def _box(design: Any, fp: Any, drop: List[str]) -> PartMesh:
    """A box for a part with no readable mesh: the 3D model's bounding box, else courtyard / fab outline and
    the estimated height."""
    import copy
    from .models3d import model_body
    t = float(design.board.thickness)
    if drop and any(model_matches(m.path, drop) for m in fp.models):
        fp = copy.copy(fp)
        fp.models = [m for m in fp.models if not model_matches(m.path, drop)]
    mb = model_body(fp, design.project_dir)
    if mb is not None:
        lb, h, src = mb[0], mb[1], "box (3D model bounds)"
    else:
        lb = fp.courtyard_local()
        if not lb.valid:
            lb = fp.pads_local_bbox()
        h, hs = design.height_of(fp)
        src = "box (%s height)" % hs
    corners = [fp.to_board(x, y) for x, y in ((lb.x0, lb.y0), (lb.x1, lb.y0), (lb.x1, lb.y1), (lb.x0, lb.y1))]
    z0, z1 = (t, t + h) if fp.side == "top" else (-h, 0.0)
    return box_mesh(fp.ref, corners, z0, z1, src)


# =========================================================================== #
def part_meshes(design: Any, log: Optional[List[str]] = None) -> Dict[str, PartMesh]:
    """Every part's mesh (by reference; parts sharing a reference keep the first) in the real board's frame, without
    the 3D models in jig.ignore_models; a height in jig.heights stretches the part's shape to it. ``design`` may be
    a turned-over view: the meshes are still of the real board (see :func:`in_view`)."""
    base = getattr(design, "_d", design)
    jc = base.cfg.get("jig") or {}
    drop = [str(p) for p in jc.get("ignore_models") or []]
    heights = {str(k): float(v) for k, v in (jc.get("heights") or {}).items()}
    key = (tuple(drop), tuple(sorted(heights.items())))
    cache = getattr(base, "_part_meshes", None)
    if cache is not None and cache[0] == key:
        return cache[1]
    fps = [fp for fp in base.components() if fp.pads or fp.models]
    by_id: Dict[int, PartMesh] = {}
    wanted: Dict[str, List[Any]] = {}
    for fp in fps:
        if any(not m.hide and not model_matches(m.path, drop) for m in fp.models):
            wanted.setdefault(fp.ref, []).append(fp)
    glb = kicad_export(base.board.path, None, log, drop) if wanted and getattr(base.board, "path", "") else None
    if glb:
        try:
            by_id.update(_from_glb(base, glb, wanted))
        except (OSError, ValueError, KeyError, IndexError, struct.error) as e:
            if log is not None:
                log.append("KiCad 3D export unreadable (%s); using the models directly" % e)
    t = float(base.board.thickness)
    out: Dict[str, PartMesh] = {}
    for fp in fps:
        if fp.ref in out:
            continue
        pm = by_id.get(id(fp)) or (_from_vrml(base, fp, drop) if wanted.get(fp.ref) else None) or _box(base, fp, drop)
        if fp.ref in heights and pm.bbox:
            pm = _stretch(pm, fp.side, t, heights[fp.ref])
        out[fp.ref] = pm
    base._part_meshes = (key, out)
    return out


def _stretch(pm: PartMesh, side: str, t: float, h: float) -> PartMesh:
    """The part's shape stretched (or squashed) away from the board so its body is ``h`` high (jig.heights)."""
    if side == "top":
        cur = pm.bbox[5] - t
        f = h / cur if cur > 1e-6 else 1.0
        z = lambda v: t + (v - t) * f if v > t else v          # noqa: E731
    else:
        cur = -pm.bbox[2]
        f = h / cur if cur > 1e-6 else 1.0
        z = lambda v: v * f if v < 0.0 else v                    # noqa: E731
    tris = [(a[0], a[1], z(a[2]), a[3], a[4], z(a[5]), a[6], a[7], z(a[8])) for a in pm.tris]
    return PartMesh(pm.ref, tris, pm.source, "height set to %.2f mm (jig.heights)" % h)


def kicad_seats(design: Any) -> Tuple[float, float]:
    """Where KiCad's 3D view seats top- and bottom-side models (mm above the board body's underside): the board's
    faces in KiCad's 3D coordinates."""
    base = getattr(design, "_d", design)
    part_meshes(base)
    t = float(base.board.thickness)
    return getattr(base, "_kicad_seats", (t - 0.005, -0.085))


def to_kicad(design: Any, side: str, x: float, y: float, z: float) -> Tuple[float, float, float]:
    """A point of the real board's frame (z above the bottom face) -> KiCad's 3D / STL coordinates (X, Y up, Z),
    with z measured from the seating plane of ``side`` (the face the point belongs with)."""
    base = getattr(design, "_d", design)
    top, bot = kicad_seats(base)
    t = float(base.board.thickness)
    return x, -y, (z - t + top) if side == "top" else (z + bot)


def board_body(design: Any, log: Optional[List[str]] = None) -> Optional[List[Tri]]:
    """KiCad's 3D board body (its own export, cached) in KiCad's 3D coordinates (X, Y up, Z), or None."""
    base = getattr(design, "_d", design)
    part_meshes(base, log)
    path = kicad_export(base.board.path, None, log, None, body=True) if getattr(base.board, "path", "") else None
    if not path:
        return None
    try:
        tris = read_glb(path, merged=True)[0][2]
    except (OSError, ValueError, KeyError, IndexError, struct.error):
        return None
    dx, dy = getattr(base, "_kicad_shift", (0.0, 0.0))
    return [(a[0] * 1000.0 + dx, -(a[2] * 1000.0 + dy), a[1] * 1000.0, a[3] * 1000.0 + dx, -(a[5] * 1000.0 + dy),
             a[4] * 1000.0, a[6] * 1000.0 + dx, -(a[8] * 1000.0 + dy), a[7] * 1000.0) for a in tris]


def in_view(design_view: Any, pm: PartMesh) -> PartMesh:
    """The mesh as seen in a (possibly turned-over) view: (x, y, z) -> (x, -y, thickness - z) when turned."""
    if getattr(design_view, "up_side", "top") != "bottom":
        return pm
    cache = getattr(pm, "_turned", None)
    if cache is None:
        t = float(design_view.board.thickness)
        tris = [(a[0], -a[1], t - a[2], a[3], -a[4], t - a[5], a[6], -a[7], t - a[8]) for a in pm.tris]
        cache = PartMesh(pm.ref, tris, pm.source, pm.note)
        pm._turned = cache
    return cache


# =========================================================================== #
# Envelopes
# =========================================================================== #
def _clip(poly: List[Tuple[float, float, float]], axis: int, bound: float, keep_ge: bool):
    out = []
    n = len(poly)
    for k in range(n):
        p, q = poly[k], poly[(k + 1) % n]
        pa, qa = p[axis], q[axis]
        pin = pa >= bound if keep_ge else pa <= bound
        qin = qa >= bound if keep_ge else qa <= bound
        if pin:
            out.append(p)
        if pin != qin:
            f = (bound - pa) / (qa - pa)
            out.append((p[0] + (q[0] - p[0]) * f, p[1] + (q[1] - p[1]) * f, p[2] + (q[2] - p[2]) * f))
    return out


def dilate(env: Dict[int, Tuple[float, float]], nx: int, ny: int, k: int) -> Dict[int, Tuple[float, float]]:
    """Every cell takes the lowest and highest values of the cells up to ``k`` cells away (a square), in two
    passes (rows, then columns)."""
    if k <= 0:
        return env
    rows: Dict[int, Tuple[float, float]] = {}
    for key, (lo, hi) in env.items():
        j, i = divmod(key, nx)
        for ii in range(max(0, i - k), min(nx - 1, i + k) + 1):
            kk = j * nx + ii
            cur = rows.get(kk)
            rows[kk] = (lo, hi) if cur is None else (min(lo, cur[0]), max(hi, cur[1]))
    out: Dict[int, Tuple[float, float]] = {}
    for key, (lo, hi) in rows.items():
        j, i = divmod(key, nx)
        for jj in range(max(0, j - k), min(ny - 1, j + k) + 1):
            kk = jj * nx + i
            cur = out.get(kk)
            out[kk] = (lo, hi) if cur is None else (min(lo, cur[0]), max(hi, cur[1]))
    return out


def envelope(tris: List[Tri], x0: float, y0: float, res: float, nx: int, ny: int, grow: float = 0.0,
             frame: str = "y_down", ay1: float = 0.0, exact: bool = True) -> Dict[int, Tuple[float, float]]:
    """{cell j * nx + i: (lowest z, highest z)} of the triangles over every cell's square grown by ``grow``.

    Grid frames: "y_down" cell (i, j) covers x in x0 + [i, i+1] * res, y in y0 + [j, j+1] * res; "y_up" (jig Y =
    ay1 - y) covers y in ay1 - [j+1, j] * res. Exact: every triangle is clipped to every cell it reaches.
    ``exact=False`` (for clearance pockets: much faster with a large ``grow``): the cells' own envelopes spread to
    every cell within ceil(grow / res) cells, which covers the grown square and a little more."""
    if not exact and grow > 0:
        return dilate(envelope(tris, x0, y0, res, nx, ny, 0.0, frame, ay1), nx, ny, int(math.ceil(grow / res - 1e-9)))
    env: Dict[int, Tuple[float, float]] = {}
    g = grow
    for t in tris:
        ax, ay, az, bx, by, bz, cx, cy, cz = t
        mnx, mxx = min(ax, bx, cx) - g, max(ax, bx, cx) + g
        mny, mxy = min(ay, by, cy) - g, max(ay, by, cy) + g
        i0, i1 = max(0, int(math.floor((mnx - x0) / res))), min(nx - 1, int(math.floor((mxx - x0) / res)))
        if frame == "y_down":
            j0, j1 = max(0, int(math.floor((mny - y0) / res))), min(ny - 1, int(math.floor((mxy - y0) / res)))
        else:
            j0, j1 = max(0, int(math.floor((ay1 - mxy) / res))), min(ny - 1, int(math.floor((ay1 - mny) / res)))
        if i0 > i1 or j0 > j1:
            continue
        zlo, zhi = min(az, bz, cz), max(az, bz, cz)
        tri = [(ax, ay, az), (bx, by, bz), (cx, cy, cz)]
        whole = (i0 == i1 and j0 == j1)
        for j in range(j0, j1 + 1):
            if frame == "y_down":
                ylo, yhi = y0 + j * res - g, y0 + (j + 1) * res + g
            else:
                ylo, yhi = ay1 - (j + 1) * res - g, ay1 - j * res + g
            if whole:
                band = tri
            else:
                band = _clip(_clip(tri, 1, ylo, True), 1, yhi, False)
                if not band:
                    continue
            bxs = [p[0] for p in band]
            k0 = max(i0, int(math.floor((min(bxs) - g - x0) / res)))
            k1 = min(i1, int(math.floor((max(bxs) + g - x0) / res)))
            flat = zhi - zlo < 1e-9
            row = j * nx
            for i in range(k0, k1 + 1):
                if flat or whole:
                    lo, hi = zlo, zhi
                    if not whole:
                        xlo, xhi = x0 + i * res - g, x0 + (i + 1) * res + g
                        if not _clip(_clip(band, 0, xlo, True), 0, xhi, False):
                            continue
                else:
                    xlo, xhi = x0 + i * res - g, x0 + (i + 1) * res + g
                    cell = _clip(_clip(band, 0, xlo, True), 0, xhi, False)
                    if not cell:
                        continue
                    zs = [p[2] for p in cell]
                    lo, hi = min(zs), max(zs)
                key = row + i
                cur = env.get(key)
                if cur is None:
                    env[key] = (lo, hi)
                elif lo < cur[0] or hi > cur[1]:
                    env[key] = (min(lo, cur[0]), max(hi, cur[1]))
    return env


def silhouette_bbox(pm: PartMesh, z_min: Optional[float] = None, z_max: Optional[float] = None):
    """(x0, y0, x1, y1) of the mesh's points with z_min <= z <= z_max (vertices only), or None."""
    xs, ys = [], []
    for t in pm.tris:
        for k in (0, 3, 6):
            z = t[k + 2]
            if (z_min is None or z >= z_min) and (z_max is None or z <= z_max):
                xs.append(t[k])
                ys.append(t[k + 1])
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None
