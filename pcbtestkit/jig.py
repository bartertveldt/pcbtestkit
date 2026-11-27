"""3D-printable solder jigs for hand-soldered (typically through-hole) parts, one set per board side.

Workflow it is designed for:
  1. Drop the selected parts upside-down into their snug "hold" pockets.
  2. Lower the PCB, top side down, into the nest; the leads slide through
     their holes. Already-assembled SMD parts sit in clearance pockets.
  3. Solder from the (now upward-facing) bottom side; every part is held flush.

Boards with parts on both sides are built in assembly passes (:func:`assembly_passes`): SMD side 1, SMD side 2
(with a paste-print nest that carries side 1), then one hand-solder pass per side with its own jig set. Each
jig of a bottom-side pass is made from the board turned over (:mod:`pcbtestkit.sides`), with pockets for every
part already on the board and reliefs for the soldered leads of the earlier pass.

Every pocket, relief and well follows the parts' real 3D shapes (:mod:`pcbtestkit.parts3d`, KiCad's own 3D
export): a hold pocket's floor is its part's top surface, so all hand-soldered parts touch their pockets at the
moment the board lands on the support, and every other part, lead and overhanging connector clears the jigs.
:func:`fit_check` (:mod:`pcbtestkit.jigfit`) checks that on every build. Parts and 3D models can be left out
completely (jig.ignore, jig.ignore_models: an enclosure in the KiCad files, say).

Jig coordinates: X = board x, Y = board y (KiCad), Z up. Because the PCB is
flipped about the X axis this is a proper rotation, so the jig is not
mirrored. Outputs: watertight binary STL (heightmap meshing) and an
OpenSCAD file of the same shape.
"""
from __future__ import annotations

import math
import os
import struct
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import classify as C
from . import parts3d as P3
from .geometry import BBox, pt_seg_dist
from .pcb import Footprint
from .project import Design
from .sides import other, real_side, view

Q = 100  # height quantum: 0.01 mm



def jig_ignored(design: Design) -> Dict[str, str]:
    """Parts the jigs leave out completely (no pocket, no 3D model, no stand-in box), ref -> why: those in
    jig.ignore (references, values or footprint names, wildcards allowed), parts whose every 3D model is in
    jig.ignore_models, and footprints without pads (nothing is soldered: logos, enclosures, mechanical models)."""
    import fnmatch
    jc = design.cfg["jig"]
    pats = [str(p).lower() for p in jc.get("ignore") or [] if str(p)]
    mpats = [str(p) for p in jc.get("ignore_models") or [] if str(p)]
    out: Dict[str, str] = {}
    for fp in design.components():
        keys = [k.lower() for k in (fp.ref, fp.value, fp.lib_id, fp.lib_id.rpartition(":")[2]) if k]
        if any(fnmatch.fnmatchcase(k, p) for p in pats for k in keys):
            out[fp.ref] = "jig.ignore"
        elif not fp.pads:
            out[fp.ref] = "no pads (nothing to solder)"
        elif mpats and any(not m.hide for m in fp.models) and \
                all(m.hide or P3.model_matches(m.path, mpats) for m in fp.models):
            out[fp.ref] = "its 3D model is in jig.ignore_models"
    return out


def select_parts(design: Design, side: str = "top") -> Tuple[List[Footprint], List[str]]:
    """Hand-soldered parts on ``side`` of the design (or of its turned-over view): jig.parts, or every
    through-hole part plus parts marked Assembly=Manual when jig.parts is "auto". Never an ignored part."""
    jc = design.cfg["jig"]
    notes: List[str] = []
    comps = design.components()
    excl = set(jc.get("exclude") or [])
    ign = jig_ignored(design)
    if isinstance(jc.get("parts"), list):
        wanted = set(jc["parts"])
        sel = [fp for fp in comps if fp.ref in wanted]
        missing = wanted - {fp.ref for fp in sel}
        if missing:
            notes.append("unknown parts in jig.parts: %s" % ", ".join(sorted(missing)))
    else:
        sel = []
        for fp in comps:
            manual = fp.get_field("Assembly", "Manual", "HandSolder", "Mount").lower() in ("manual", "hand", "yes", "true", "1", "tht")
            if (fp.is_tht or manual) and not fp.dnp and not C.is_mounting_hole(fp) and not C.is_testpoint(fp) and not fp.board_only:
                sel.append(fp)
    return [fp for fp in sel if fp.ref not in excl and fp.ref not in ign and fp.side == side], notes


def hand_parts(design: Design) -> Dict[str, List[Footprint]]:
    """Hand-soldered parts of the real board per side."""
    return {s: select_parts(design, s)[0] for s in ("top", "bottom")}


def _placed(fp: Footprint) -> bool:
    return bool(fp.pads) and not fp.dnp and not fp.board_only and not fp.exclude_from_bom and \
        not C.is_mounting_hole(fp) and not C.is_testpoint(fp)


def assembly_passes(design: Design) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The order the board is built in: SMD sides first (reflow), then one hand-solder pass per side.

    jig.smt_first: auto (the side with fewer SMD parts goes through reflow first, the heavier side last) |
    top | bottom. jig.sides: auto (every side with hand-soldered parts, top first) or a list such as
    ["bottom", "top"]. jig.paste_nest: auto (a paste-print nest for the second SMD side, which rests on the
    parts of the first) | both | none. Returns (passes, notes)."""
    jc = design.cfg["jig"]
    notes: List[str] = []
    ign = jig_ignored(design)
    smd = {s: [fp.ref for fp in design.components() if _placed(fp) and fp.is_smd and fp.side == s and fp.ref not in ign]
           for s in ("top", "bottom")}
    hand = {s: [fp.ref for fp in fps] for s, fps in hand_parts(design).items()}
    smt_sides = [s for s in ("bottom", "top") if smd[s]]
    if len(smt_sides) == 2:
        first = str(jc.get("smt_first", "auto"))
        if first not in ("top", "bottom"):
            first = "bottom" if len(smd["bottom"]) <= len(smd["top"]) else "top"
        smt_sides = [first, other(first)]
    nest = str(jc.get("paste_nest", "auto"))
    passes: List[Dict[str, Any]] = []
    for k, s in enumerate(smt_sides):
        passes.append({"process": "smt", "side": s, "parts": smd[s],
                       "nest": nest == "both" or (nest == "auto" and k == 1)})
    order = jc.get("sides", "auto")
    if isinstance(order, str):
        order = [s for s in ("top", "bottom") if hand[s]] if order == "auto" else [order]
    seen: List[str] = []
    for s in order:
        if s in ("top", "bottom") and s not in seen:
            seen.append(s)
    for s in seen:
        if hand[s]:
            passes.append({"process": "hand", "side": s, "parts": hand[s]})
        else:
            notes.append("jig.sides lists the %s side, but no hand-soldered part is on it" % s)
    for s in ("top", "bottom"):
        if hand[s] and s not in seen:
            notes.append("%d hand-soldered part(s) on the %s side get no jig (jig.sides): %s"
                         % (len(hand[s]), s, ", ".join(hand[s][:8])))
    for i, p in enumerate(passes, 1):
        p["pass"] = i
    return passes, notes


def present_parts(design: Design, selected: List[Footprint], mounted: Optional[set]) -> List[Footprint]:
    """The parts on the board while a jig of this pass is in use: every part already mounted (``mounted`` = refs,
    None: all of them) and the pass's own parts. Not DNP parts, mounting holes (the posts go through them) or
    ignored parts (:func:`jig_ignored`)."""
    ign = jig_ignored(design)
    sel = {fp.ref for fp in selected}
    out = []
    for fp in design.components():
        if fp.ref in ign or C.is_mounting_hole(fp) or (fp.dnp and fp.ref not in sel):
            continue
        if mounted is not None and fp.ref not in mounted and fp.ref not in sel:
            continue                   # fitted in a later pass: not on the board yet
        out.append(fp)
    return out


def view_meshes(design: Design, fps: List[Footprint], log: Optional[List[str]] = None) -> Dict[str, "P3.PartMesh"]:
    """The 3D shapes of ``fps`` as the (possibly turned-over) view ``design`` sees them."""
    allm = P3.part_meshes(design, log)
    return {fp.ref: P3.in_view(design, allm[fp.ref]) for fp in fps if fp.ref in allm}


def shape_notes(meshes: Dict[str, "P3.PartMesh"], refs: List[str]) -> List[str]:
    """Notes for the parts whose 3D shape is a stand-in box (no model, or a STEP model without kicad-cli)."""
    est = sorted((r for r in set(refs) if r in meshes and not meshes[r].exact), key=C.natural_key)
    if not est:
        return []
    why = (" (KiCad's 3D export, which reads STEP models, needs kicad-cli from KiCad 8 or later)"
           if any(meshes[r].source.startswith("box (3D") for r in est) else "")
    return ["Shape estimated as a box for %d part(s)%s: %s. Their fit can't be guaranteed: add a 3D model, set "
            "jig.heights, or leave them out with jig.ignore." % (len(est), why, ", ".join(est[:12]) + (" ..." if len(est) > 12 else ""))]


def _hd(res: float) -> float:
    """Half the diagonal of a cell: a cell's square reaches this far from its centre."""
    return res * 0.70711


# =========================================================================== #
# Heightmap -> watertight mesh
# =========================================================================== #
def _greedy(H: List[List[int]], nx: int, ny: int):
    seen = [bytearray(nx) for _ in range(ny)]
    rects = []
    for j in range(ny):
        row, srow = H[j], seen[j]
        i = 0
        while i < nx:
            if srow[i]:
                i += 1
                continue
            h = row[i]
            w = 1
            while i + w < nx and not srow[i + w] and row[i + w] == h:
                w += 1
            hh = 1
            while j + hh < ny:
                r2, s2 = H[j + hh], seen[j + hh]
                if all(r2[k] == h and not s2[k] for k in range(i, i + w)):
                    hh += 1
                else:
                    break
            for jj in range(j, j + hh):
                s = seen[jj]
                for k in range(i, i + w):
                    s[k] = 1
            rects.append((i, j, i + w, j + hh, h))
            i += w
    return rects


def heightmap_mesh(H: List[List[int]], nx: int, ny: int, res: float) -> List[Tuple]:
    tris: List[Tuple] = []

    def P(i, j, z):
        return (i * res, j * res, z / Q)

    def cell(i, j):
        if 0 <= i < nx and 0 <= j < ny:
            return H[j][i]
        return 0

    def zcol(i, j):
        return {cell(i - 1, j - 1), cell(i, j - 1), cell(i - 1, j), cell(i, j)}

    def fan(poly):
        if len(poly) == 3:
            tris.append((poly[0], poly[1], poly[2]))
            return
        if len(poly) == 4:
            tris.append((poly[0], poly[1], poly[2]))
            tris.append((poly[0], poly[2], poly[3]))
            return
        cx = sum(p[0] for p in poly) / len(poly)
        cy = sum(p[1] for p in poly) / len(poly)
        cz = sum(p[2] for p in poly) / len(poly)
        c = (cx, cy, cz)
        for k in range(len(poly)):
            tris.append((c, poly[k], poly[(k + 1) % len(poly)]))

    # top faces
    for (i0, j0, i1, j1, h) in _greedy(H, nx, ny):
        poly = [P(i, j0, h) for i in range(i0, i1)] + [P(i1, j, h) for j in range(j0, j1)] + \
               [P(i, j1, h) for i in range(i1, i0, -1)] + [P(i0, j, h) for j in range(j1, j0, -1)]
        fan(poly)

    def wall(a2, b2, lo, hi, normal):
        """Vertical wall between grid points a2, b2 from lo to hi, facing `normal` (2D)."""
        (ia, ja), (ib, jb) = a2, b2
        dx, dy = ib - ia, jb - ja
        if dy * normal[0] - dx * normal[1] < 0:
            (ia, ja), (ib, jb) = (ib, jb), (ia, ja)
        zb = sorted(z for z in zcol(ib, jb) if lo < z < hi)
        za = sorted((z for z in zcol(ia, ja) if lo < z < hi), reverse=True)
        poly = [P(ia, ja, lo), P(ib, jb, lo)] + [P(ib, jb, z) for z in zb] + [P(ib, jb, hi), P(ia, ja, hi)] + \
               [P(ia, ja, z) for z in za]
        fan(poly)

    for j in range(ny):
        for i in range(nx):
            h = H[j][i]
            # +x neighbour
            hr = H[j][i + 1] if i + 1 < nx else 0
            if hr != h:
                if h > hr:
                    wall((i + 1, j), (i + 1, j + 1), hr, h, (1, 0))
                else:
                    wall((i + 1, j), (i + 1, j + 1), h, hr, (-1, 0))
            if i == 0 and h > 0:
                wall((0, j), (0, j + 1), 0, h, (-1, 0))
            hu = H[j + 1][i] if j + 1 < ny else 0
            if hu != h:
                if h > hu:
                    wall((i, j + 1), (i + 1, j + 1), hu, h, (0, 1))
                else:
                    wall((i, j + 1), (i + 1, j + 1), h, hu, (0, -1))
            if j == 0 and h > 0:
                wall((i, 0), (i + 1, 0), 0, h, (0, -1))
    # bottom (normal -Z => clockwise seen from above)
    poly = [P(0, j, 0) for j in range(0, ny)] + [P(i, ny, 0) for i in range(0, nx)] + \
           [P(nx, j, 0) for j in range(ny, 0, -1)] + [P(i, 0, 0) for i in range(nx, 0, -1)]
    fan(poly)
    return tris


def render_mesh(H: List[List[int]], nx: int, ny: int, res: float):
    """A light mesh of the heightmap for 3D renders (not for printing): two triangles per flat rectangle and
    one quad per straight run of wall. It has T-junctions, so it is not watertight, but a raytracer doesn't
    mind and renders it many times faster than the print mesh's long fan triangles.
    Every face is wound so its normal points out of the material (walls face the lower side): KiCad's
    raytracer shades by the winding, so a wall wound the wrong way renders black once the jig is turned over.
    Returns (vertices in mm, faces as vertex-index tuples), same coordinates as heightmap_mesh."""
    idx: Dict[Tuple[int, int, int], int] = {}
    verts: List[Tuple[float, float, float]] = []
    faces: List[Tuple[int, ...]] = []

    def v(i, j, z):
        k = (i, j, z)
        if k not in idx:
            idx[k] = len(verts)
            verts.append((i * res, j * res, z / Q))
        return idx[k]

    for (i0, j0, i1, j1, h) in _greedy(H, nx, ny):
        faces.append((v(i0, j0, h), v(i1, j0, h), v(i1, j1, h), v(i0, j1, h)))

    def cell(i, j):
        return H[j][i] if 0 <= i < nx and 0 <= j < ny else 0

    # walls along x boundaries (between columns i-1 and i), merged over runs of rows with the same span
    for i in range(nx + 1):
        run = None
        for j in range(ny + 1):
            span = None
            if j < ny:
                a, b = cell(i - 1, j), cell(i, j)
                if a != b:
                    span = (min(a, b), max(a, b), a > b)
            if run and span == run[1]:
                continue
            if run:
                lo, hi, pos = run[1]
                f = (v(i, run[0], lo), v(i, j, lo), v(i, j, hi), v(i, run[0], hi))     # normal +x
                faces.append(f if pos else f[::-1])
            run = (j, span) if span else None
    # walls along y boundaries (between rows j-1 and j)
    for j in range(ny + 1):
        run = None
        for i in range(nx + 1):
            span = None
            if i < nx:
                a, b = cell(i, j - 1), cell(i, j)
                if a != b:
                    span = (min(a, b), max(a, b), a > b)
            if run and span == run[1]:
                continue
            if run:
                lo, hi, pos = run[1]
                f = (v(run[0], j, lo), v(i, j, lo), v(i, j, hi), v(run[0], j, hi))     # normal -y
                faces.append(f[::-1] if pos else f)
            run = (i, span) if span else None
    faces.append((v(0, 0, 0), v(0, ny, 0), v(nx, ny, 0), v(nx, 0, 0)))
    return verts, faces


def write_heightmap_png(path: str, H: List[List[int]], nx: int, ny: int, max_px: int = 900) -> None:
    """Shaded top view (Y up) of the heightmap; stdlib-only PNG writer."""
    import zlib
    step = max(1, int(math.ceil(max(nx, ny) / float(max_px))))
    w, h = (nx + step - 1) // step, (ny + step - 1) // step
    zmax = max(max(r) for r in H) or 1
    zmin = min(min(r) for r in H)
    rows = []
    for jj in range(h - 1, -1, -1):                      # image row 0 = top = max Y
        j = min(ny - 1, jj * step)
        row = bytearray([0])
        for ii in range(w):
            i = min(nx - 1, ii * step)
            z = H[j][i]
            zl = H[j][max(0, i - step)]
            zd = H[max(0, j - step)][i]
            shade = (z - zmin) / float(max(1, zmax - zmin))
            light = max(-0.25, min(0.25, ((z - zl) - (z - zd)) / float(Q * 4)))
            v = int(max(0, min(255, 40 + 200 * shade + 255 * light)))
            row += bytes((int(v * 0.85), int(v * 0.92), v))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    with open(path, "wb") as fh:
        fh.write(png)


def write_stl(path: str, tris: List[Tuple], name: str = "pcbtestkit jig") -> None:
    with open(path, "wb") as fh:
        fh.write(name.encode("ascii", "replace")[:80].ljust(80, b" "))
        fh.write(struct.pack("<I", len(tris)))
        for a, b, c in tris:
            ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
            vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
            nx_, ny_, nz_ = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
            n = math.sqrt(nx_ * nx_ + ny_ * ny_ + nz_ * nz_) or 1.0
            fh.write(struct.pack("<12fH", nx_ / n, ny_ / n, nz_ / n, *a, *b, *c, 0))


def mesh_check(tris: List[Tuple]) -> Dict[str, Any]:
    """Edge-manifold / orientation check and signed volume."""
    edges: Dict[Tuple, int] = {}
    vol = 0.0
    for a, b, c in tris:
        for p, q in ((a, b), (b, c), (c, a)):
            k = (tuple(round(v, 5) for v in p), tuple(round(v, 5) for v in q))
            edges[k] = edges.get(k, 0) + 1
        vol += (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0]) + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6.0
    unmatched = sum(1 for (p, q), n in edges.items() if edges.get((q, p), 0) != n)
    return {"triangles": len(tris), "unmatched_edges": unmatched, "volume_mm3": round(vol, 1), "watertight": unmatched == 0}


# =========================================================================== #
# Shared geometry for the two-jig workflow
# =========================================================================== #
def outline_segments(design: Design) -> List[Tuple[float, float, float, float]]:
    """Board outline as segments in board coordinates (the bounding box if Edge.Cuts is empty)."""
    b = design.board
    segs = [tuple(s[:4]) for s in b.edge_segs]
    if not segs:
        bb = b.outline_bbox()
        segs = [(bb.x0, bb.y0, bb.x1, bb.y0), (bb.x1, bb.y0, bb.x1, bb.y1), (bb.x1, bb.y1, bb.x0, bb.y1),
                (bb.x0, bb.y1, bb.x0, bb.y0)]
    return segs


def point_inside(x: float, y: float, segs) -> bool:
    """Even-odd ray test against the outline segments."""
    c = False
    for ax, ay, bx, by in segs:
        if (ay > y) != (by > y) and x < ax + (y - ay) * (bx - ax) / (by - ay):
            c = not c
    return c


_point_inside = point_inside


def tile_grid(g: Dict[str, Any], frame: str, cols: int, rows: int, n: int, px: int, py: int, fill: int) -> None:
    """Repeat a jig heightmap for n boards, ``cols`` per row, ``px`` x ``py`` cells apart (in place).

    ``frame`` "y_down" (holding jig: jig Y = board y - oy) or "y_up" (placement jig and nest: jig Y = ay1 - board
    y). Board k sits at board offset (col * px, row * py) * res in both frames, so a placement jig and a holding
    jig tiled with the same pitch still close over every board. Cells between the boards get ``fill``."""
    H, nx, ny = g["H"], g["nx"], g["ny"]
    NX, NY = (cols - 1) * px + nx, (rows - 1) * py + ny
    T = [[fill] * NX for _ in range(NY)]
    res = g["res"]
    offsets = []
    for k in range(n):
        c, r = k % cols, k // cols
        i0 = c * px
        j0 = r * py if frame == "y_down" else (rows - 1 - r) * py
        for j in range(ny):
            T[j0 + j][i0:i0 + nx] = H[j]
        offsets.append((round(c * px * res, 4), round(r * py * res, 4)))
    g["H"], g["nx"], g["ny"] = T, NX, NY
    if frame == "y_up":
        g["ay1"] = g["ay1"] + (rows - 1) * py * res
    g["boards"] = offsets


def mounting_holes(design: Design) -> List[Tuple[float, float, float, str]]:
    """(x, y, drill radius, ref) of every mounting hole in board coordinates (where the jig posts go)."""
    out = []
    for fp in design.components():
        for pd in fp.pads:
            if pd.drill_w >= 2.0 and (pd.is_npth or C.is_mounting_hole(fp)):
                out.append((pd.x, pd.y, pd.drill_w / 2, fp.ref))
    return out



def low_parts(design: Design, meshes: Dict[str, "P3.PartMesh"], segs, reach: float,
              above: float = 1.0) -> List[Tuple[float, float]]:
    """Points (0.5 mm apart) where a part comes lower than ``above`` mm over the board's up face within
    ``reach`` of the outline: a placement-jig stop must keep away from them. View coordinates."""
    from .geometry import pt_seg_dist
    t = float(design.board.thickness)
    res = 0.5
    pts: List[Tuple[float, float]] = []
    for pm in meshes.values():
        sb = P3.silhouette_bbox(pm, z_max=t + above)
        if sb is None:
            continue
        x0, y0, x1, y1 = sb
        cx, cy, hw, hh = (x0 + x1) / 2.0, (y0 + y1) / 2.0, (x1 - x0) / 2.0, (y1 - y0) / 2.0
        if min(pt_seg_dist(cx, cy, *s[:4]) for s in segs) > math.hypot(hw, hh) + reach:
            continue                    # nowhere near the edge
        nx = int(math.ceil((x1 - x0) / res)) + 2
        ny = int(math.ceil((y1 - y0) / res)) + 2
        env = P3.envelope([tr for tr in pm.tris if min(tr[2], tr[5], tr[8]) < t + above],
                          x0 - res, y0 - res, res, nx, ny)
        for k, (lo, _hi) in env.items():
            if lo < t + above:
                j, i = divmod(k, nx)
                pts.append((x0 - res + (i + 0.5) * res, y0 - res + (j + 0.5) * res))
    return pts


def stop_anchors(design: Design, segs, length: float, meshes: Optional[Dict[str, "P3.PartMesh"]] = None,
                 width: float = 2.5, clearance: float = 0.15) -> List[Dict[str, float]]:
    """Where the placement jig's locating stops touch the board edge: up to two per side (left, right, top,
    bottom), away from every part that hangs over the edge (its real 3D shape, :mod:`pcbtestkit.parts3d`; the
    courtyard when no shape is given). Board coordinates, with the outward normal."""
    bb = design.board.outline_bbox()
    reach = length / 2.0 + clearance + width + 1.0          # a stop's corner, and a millimetre to spare
    near_pts: List[Tuple[float, float]] = []
    over: List[BBox] = []
    if meshes is not None:
        near_pts = low_parts(design, meshes, segs, reach)
    else:
        for fp in design.components():
            c = fp.courtyard_local()
            if not c.valid:
                continue
            g = BBox()
            for lx, ly in ((c.x0, c.y0), (c.x1, c.y0), (c.x1, c.y1), (c.x0, c.y1)):
                g.add(*fp.to_board(lx, ly))
            if g.x0 < bb.x0 + 0.5 or g.y0 < bb.y0 + 0.5 or g.x1 > bb.x1 - 0.5 or g.y1 > bb.y1 - 0.5:
                over.append(g.grow(1.5))
    buckets: Dict[Tuple[int, int], List[Tuple[float, float]]] = {}
    for q in near_pts:
        buckets.setdefault((int(q[0] // 4.0), int(q[1] // 4.0)), []).append(q)

    def blocked(x: float, y: float) -> bool:
        if any(b_.contains(x, y) for b_ in over):
            return True
        r = reach + 0.36
        for bi in range(int((x - r) // 4.0), int((x + r) // 4.0) + 1):
            for bj in range(int((y - r) // 4.0), int((y + r) // 4.0) + 1):
                for qx, qy in buckets.get((bi, bj), ()):
                    if (qx - x) ** 2 + (qy - y) ** 2 <= r * r:
                        return True
        return False

    bins: Dict[str, List[Tuple[float, float, float, float]]] = {"left": [], "right": [], "top": [], "bottom": []}
    for ax, ay, bx, by in segs:
        n = max(1, int(math.hypot(bx - ax, by - ay) / 1.0))
        dx, dy = (bx - ax), (by - ay)
        ln = math.hypot(dx, dy) or 1.0
        for k in range(n):
            t = (k + 0.5) / n
            x, y = ax + dx * t, ay + dy * t
            nx_, ny_ = dy / ln, -dx / ln
            if point_inside(x + nx_ * 0.2, y + ny_ * 0.2, segs):
                nx_, ny_ = -nx_, -ny_
            if abs(nx_) > 0.92:
                bins["right" if nx_ > 0 else "left"].append((x, y, nx_, ny_))
            elif abs(ny_) > 0.92:
                bins["bottom" if ny_ > 0 else "top"].append((x, y, nx_, ny_))
    anchors: List[Dict[str, float]] = []
    for side, pts in bins.items():
        if not pts:
            continue
        along = 1 if side in ("left", "right") else 0          # sort along the edge
        pts.sort(key=lambda p: p[along])
        span = pts[-1][along] - pts[0][along]
        wants = (0.25, 0.75) if span > 3 * length else (0.5,)
        for f in wants + (0.15, 0.85, 0.4, 0.6, 0.05, 0.95, 0.3, 0.7):
            target = pts[0][along] + f * span
            p = min(pts, key=lambda q: abs(q[along] - target))
            if blocked(p[0], p[1]):
                continue
            if any(math.hypot(p[0] - a["x"], p[1] - a["y"]) < 2 * length for a in anchors):
                continue
            anchors.append({"x": p[0], "y": p[1], "nx": p[2], "ny": p[3], "side": side})
            if sum(1 for a in anchors if a["side"] == side) >= len(wants):
                break
    return anchors


def _in_stop(px: float, py: float, a: Dict[str, float], length: float) -> bool:
    """Is board point (px, py) within the along-edge reach of stop anchor a?"""
    return math.hypot(px - a["x"], py - a["y"]) <= length / 2.0


def _inside_grid(segs, nx: int, ny: int, res: float) -> List[bytearray]:
    """Cells (centres) inside the outline, by scanline; segs in grid coordinates."""
    inside = [bytearray(nx) for _ in range(ny)]
    for j in range(ny):
        yc = (j + 0.5) * res
        xs = []
        for ax, ay, bx_, by_ in segs:
            if (ay > yc) != (by_ > yc):
                xs.append(ax + (yc - ay) * (bx_ - ax) / (by_ - ay))
        xs.sort()
        for k in range(0, len(xs) - 1, 2):
            i0 = max(0, int(math.ceil(xs[k] / res - 0.5)))
            i1 = min(nx - 1, int(math.floor(xs[k + 1] / res - 0.5)))
            for i in range(i0, i1 + 1):
                inside[j][i] = 1
    return inside


def _near_grid(segs, nx: int, ny: int, res: float, dist: float) -> List[bytearray]:
    """Cells (centres) within ``dist`` of the outline; segs in grid coordinates."""
    near = [bytearray(nx) for _ in range(ny)]
    for ax, ay, bx_, by_ in segs:
        i0 = max(0, int((min(ax, bx_) - dist) / res) - 1)
        i1 = min(nx - 1, int((max(ax, bx_) + dist) / res) + 1)
        j0 = max(0, int((min(ay, by_) - dist) / res) - 1)
        j1 = min(ny - 1, int((max(ay, by_) + dist) / res) + 1)
        for j in range(j0, j1 + 1):
            yc = (j + 0.5) * res
            for i in range(i0, i1 + 1):
                if not near[j][i] and pt_seg_dist((i + 0.5) * res, yc, ax, ay, bx_, by_) <= dist:
                    near[j][i] = 1
    return near



# =========================================================================== #
def _disk_cells(cx: float, cy: float, r: float, nx: int, ny: int, res: float, inner: bool):
    """Cells (i, j) of a disk in grid coordinates: ``inner`` = the whole cell square inside it (a post that must
    not touch its hole), else every cell whose square reaches into it (a hole that must clear a post or lead)."""
    hd = _hd(res)
    lim = r - hd if inner else r + hd
    if lim <= 0:
        return
    for j in range(max(0, int((cy - r) / res) - 1), min(ny, int((cy + r) / res) + 2)):
        for i in range(max(0, int((cx - r) / res) - 1), min(nx, int((cx + r) / res) + 2)):
            if math.hypot((i + 0.5) * res - cx, (j + 0.5) * res - cy) <= lim:
                yield i, j


def _qdown(v: float) -> int:
    """mm -> height quanta, never above v (a jig surface that must stay clear of a part below it)."""
    return int(math.floor(v * Q + 1e-4))


def _qup(v: float) -> int:
    """mm -> height quanta, never below v."""
    return int(math.ceil(v * Q - 1e-4))


def _pocket_info(fp: Footprint, cells: List[Tuple[int, int]], res: float, depth: float, height: float,
                 kind: str, src: str) -> Dict[str, Any]:
    """A pocket for the reports and drawings: the part's oriented box over its pocket cells (jig coordinates)."""
    a = math.radians(fp.angle)
    ca, sa = math.cos(a), math.sin(a)
    us = [((i + 0.5) * res) * ca + ((j + 0.5) * res) * sa for i, j in cells]
    vs = [-((i + 0.5) * res) * sa + ((j + 0.5) * res) * ca for i, j in cells]
    u0, u1, v0, v1 = min(us) - res / 2, max(us) + res / 2, min(vs) - res / 2, max(vs) + res / 2
    uc, vc = (u0 + u1) / 2.0, (v0 + v1) / 2.0
    return {"ref": fp.ref, "type": kind, "center": [round(uc * ca - vc * sa, 3), round(uc * sa + vc * ca, 3)],
            "size_mm": [round(u1 - u0, 2), round(v1 - v0, 2)], "rotation_deg": round(fp.angle, 1),
            "depth_mm": round(depth, 2), "part_height_mm": round(height, 2), "height_source": src,
            "cells": len(cells)}


def _shape_src(pm: "P3.PartMesh") -> str:
    return {"kicad": "3D shape (KiCad 3D export)", "vrml": "3D shape (VRML model)"}.get(pm.source, pm.source) + \
        ("; " + pm.note if pm.note else "")


def build_jig(design: Design, stl_path: Optional[str] = None, scad_path: Optional[str] = None,
              png_path: Optional[str] = None, stops: Optional[List[Dict[str, float]]] = None,
              grid_out: Optional[Dict[str, Any]] = None, mounted: Optional[set] = None,
              log: Optional[List[str]] = None) -> Dict[str, Any]:
    """The holding jig. Its pockets are cut to every part's real 3D shape (:mod:`pcbtestkit.parts3d`): the parts
    of this pass sit in "hold" pockets whose floor is the part's own top surface (grown sideways by
    jig.hold_clearance_mm), so each touches its pocket at the moment the board's face lands on the support; every
    other part on the board gets a "relief" pocket that it never touches. With ``stops`` (two-jig workflow) the
    rim is notched where the placement jig's locating stops sit. ``design`` may be the board turned over
    (bottom-side pass); ``mounted`` = refs already on the board (None: all)."""
    jc = design.cfg["jig"]
    b = design.board
    bb = b.outline_bbox()
    t = float(b.thickness)
    res = float(jc["resolution_mm"])
    edge_clr = float(jc["edge_clearance_mm"])
    rim_w = float(jc["rim_mm"])
    border = edge_clr + rim_w
    rim_h = float(jc["rim_height_mm"])
    two = str(jc.get("workflow", "two_jig")).lower().replace("-", "_") != "single"
    if two and rim_h > t - 0.3:
        # the rim reaches down beside the board onto the placement jig: it must end above the board's underside
        notes_rim = "rim height %.2f mm (jig.rim_height_mm %.2f): it ends 0.3 mm above the %.2f mm board's underside, " \
                    "so it can't touch the placement jig" % (max(0.0, t - 0.3), rim_h, t)
        rim_h = max(0.0, t - 0.3)
    else:
        notes_rim = ""
    hold_clr = float(jc["hold_clearance_mm"])
    rel_clr = float(jc["relief_clearance_mm"])
    rel_extra = float(jc["relief_extra_depth_mm"])
    offset = float(jc.get("hold_depth_offset_mm", 0.0))
    flat = str(jc.get("pocket_floor", "shaped")).lower() == "flat"
    lead = float(jc.get("lead_relief_mm", 2.5))
    floor = float(jc["floor_mm"])
    selected, notes = select_parts(design)
    if notes_rim:
        notes.append(notes_rim)
    sel = {fp.ref for fp in selected}
    present = present_parts(design, selected, mounted)
    meshes = view_meshes(design, present, log)
    zcut = t - rim_h - 0.05                       # lower than this, nothing meets the jig (its rim ends there)
    ext = BBox(bb.x0 - border, bb.y0 - border, bb.x1 + border, bb.y1 + border)
    for fp in present:
        sb = P3.silhouette_bbox(meshes[fp.ref], z_min=zcut) if fp.ref in meshes else None
        if sb:
            g_ = (hold_clr if fp.ref in sel else rel_clr) + 1.5
            ext.merge(BBox(sb[0] - g_, sb[1] - g_, sb[2] + g_, sb[3] + g_))
    lim = BBox(bb.x0 - border - 40.0, bb.y0 - border - 40.0, bb.x1 + border + 40.0, bb.y1 + border + 40.0)
    if ext.x0 < lim.x0 or ext.y0 < lim.y0 or ext.x1 > lim.x1 or ext.y1 > lim.y1:
        notes.append("a part reaches more than 40 mm past the board; the jig ends 40 mm out")
        ext = BBox(max(ext.x0, lim.x0), max(ext.y0, lim.y0), min(ext.x1, lim.x1), min(ext.y1, lim.y1))
    ox, oy = ext.x0, ext.y0
    W_, L_ = ext.w, ext.h
    if (W_ / res) * (L_ / res) > 6e6:                       # keep memory/time bounded on huge boards
        res = math.sqrt(W_ * L_ / 6e6)
        notes.append("jig resolution coarsened to %.2f mm for this board size" % res)
    nx, ny = int(math.ceil(W_ / res)), int(math.ceil(L_ / res))

    def jx(x):
        return x - ox

    def jy(y):
        return y - oy

    # how deep the jig must be cut under every cell (mm below the support plane) and whose pocket it is
    need: Dict[int, float] = {}
    owner: Dict[int, int] = {}
    owners: List[Tuple[str, str]] = []
    pockets: List[Dict[str, Any]] = []
    # clearance pockets are exact near a hand-soldered part (its floor and walls), quicker (whole cells) elsewhere
    hold_bb = [meshes[r].bbox for r in sel if r in meshes and meshes[r].bbox]

    def near_hold(b_) -> bool:
        m_ = rel_clr + hold_clr + 2 * res + 0.5
        return any(b_[0] <= h_[3] + m_ and b_[3] >= h_[0] - m_ and b_[1] <= h_[4] + m_ and b_[4] >= h_[1] - m_
                   for h_ in hold_bb)
    for fp in present:
        pm = meshes.get(fp.ref)
        if pm is None:
            continue
        hold = fp.ref in sel
        grow, extra = (hold_clr, offset) if hold else (rel_clr, rel_extra)
        tris = [tr for tr in pm.tris if max(tr[2], tr[5], tr[8]) > zcut]
        if not tris:
            continue
        env = P3.envelope(tris, ox, oy, res, nx, ny, grow, exact=hold or bool(pm.bbox and near_hold(pm.bbox)))
        if hold and flat:
            top = max(hi for _lo, hi in env.values())
            env = {k: (lo, top) for k, (lo, _hi) in env.items()}
        k_own = len(owners)
        owners.append((fp.ref, "hold" if hold else ("lead" if fp.side == "bottom" else "relief")))
        cells, dmax, hmax = [], -1e9, -1e9
        for key, (_lo, hi) in env.items():
            if hi <= zcut:
                continue
            d = hi - t + extra
            hmax = max(hmax, hi - t)
            if d > need.get(key, -1e9):
                need[key] = d
                owner[key] = k_own
            dmax = max(dmax, d)
            cells.append(divmod(key, nx)[::-1])
        if cells and (hold or hmax > -1e-6):
            pockets.append(_pocket_info(fp, cells, res, dmax, hmax, owners[k_own][1], _shape_src(pm)))
    # the soldered leads of the other side's parts poke out of this face: at least jig.lead_relief_mm of room
    if lead > 0:
        for fp in present:
            if fp.side == "top" or fp.ref in sel or (mounted is None):
                continue
            k_own = len(owners)
            owners.append((fp.ref, "lead"))
            cells = []
            for pd in fp.pads:
                if pd.is_tht and pd.drill_w > 0:
                    for i, j in _disk_cells(jx(pd.x), jy(pd.y), max(pd.w, pd.h, pd.drill_w) / 2.0 + 0.5, nx, ny, res, False):
                        key = j * nx + i
                        if lead > need.get(key, -1e9):
                            need[key] = lead
                            owner[key] = k_own
                        cells.append((i, j))
            if cells and not any(p["ref"] == fp.ref for p in pockets):
                pockets.append(_pocket_info(fp, cells, res, lead, 0.0, "lead", "soldered leads"))
    max_depth = max([1.0] + list(need.values()))
    if max_depth > 80.0:
        deep = sorted({owners[owner[k]][0] for k, v in need.items() if v > 80.0}, key=C.natural_key)
        notes.append("%s: deeper than 80 mm - capped there, so it touches the jig: check its height" % ", ".join(deep))
        max_depth = 80.0
    S = _qup(floor + max_depth)                      # support plane (the board's up face lands here)
    support = S / float(Q)
    rim_q = _qup(rim_h)
    R = S + rim_q
    H = [[S] * nx for _ in range(ny)]

    # board outline (jig coords); the rim only where a cell's whole square clears the outline by edge_clr
    segs = [(jx(s[0]), jy(s[1]), jx(s[2]), jy(s[3])) for s in b.edge_segs]
    if not segs:
        x0, y0, x1, y1 = jx(bb.x0), jy(bb.y0), jx(bb.x1), jy(bb.y1)
        segs = [(x0, y0, x1, y0), (x1, y0, x1, y1), (x1, y1, x0, y1), (x0, y1, x0, y0)]
    inside = _inside_grid(segs, nx, ny, res)
    near = _near_grid(segs, nx, ny, res, edge_clr + _hd(res))
    for j in range(ny):
        for i in range(nx):
            if not inside[j][i] and not near[j][i]:
                H[j][i] = R
    # two-jig workflow: notch the rim (down to the support plane) where the placement jig's stops sit
    rr = float(jc.get("stop_length_mm", 8.0)) / 2.0 + float(jc.get("stop_clearance_mm", 0.15)) + \
        float(jc.get("stop_width_mm", 2.5)) + 0.5
    for a in stops or []:
        for i, j in _disk_cells(jx(a["x"]), jy(a["y"]), rr, nx, ny, res, False):
            if H[j][i] > S:
                H[j][i] = S
    # finger notches through the rim (left and right of the board)
    if jc.get("finger_notches", True):
        nd = _qdown(max(floor, support - 4.0))
        ymid = jy(bb.cy)
        for xa, xb in ((0.0, jx(bb.x0) + 2.0), (jx(bb.x1) - 2.0, W_)):
            for j in range(ny):
                if abs((j + 0.5) * res - ymid) > 7.5:
                    continue
                for i in range(nx):
                    if xa <= (i + 0.5) * res <= xb and H[j][i] >= S:
                        H[j][i] = min(H[j][i], nd)
    # alignment posts through the DUT's mounting holes: every cell of a post lies inside the hole
    posts = []
    if jc.get("posts", True):
        post_clr = float(jc["post_clearance_mm"])
        for hx, hy, hr, ref in mounting_holes(design):
            r = hr - post_clr
            px, py = jx(hx), jy(hy)
            if any(j * nx + i in need for i, j in _disk_cells(px, py, hr, nx, ny, res, False)):
                continue                    # a part sits over the hole
            cells = list(_disk_cells(px, py, r, nx, ny, res, True))
            if not cells:
                continue
            # the post runs through the hole to 1 mm past the board, unless a part sits under the hole: then it
            # stops post_clearance short of it, and it is left out if that leaves less than 0.8 mm in the hole
            end = -1.0
            for fp in present:
                pm = meshes.get(fp.ref)
                if pm is None or not pm.tris:
                    continue
                x0b, y0b, z0b, x1b, y1b, _z1b = pm.bbox
                if z0b >= t or x1b < hx - hr - 1 or x0b > hx + hr + 1 or y1b < hy - hr - 1 or y0b > hy + hr + 1:
                    continue
                near = [tr for tr in pm.tris if max(tr[2], tr[5], tr[8]) <= t + 1e-6 and
                        min(tr[0], tr[3], tr[6]) <= hx + hr + 1 and max(tr[0], tr[3], tr[6]) >= hx - hr - 1 and
                        min(tr[1], tr[4], tr[7]) <= hy + hr + 1 and max(tr[1], tr[4], tr[7]) >= hy - hr - 1]
                env = P3.envelope(near, ox, oy, res, nx, ny, post_clr, exact=False)
                for i, j in cells:
                    e = env.get(j * nx + i)
                    if e is not None:
                        end = max(end, e[1] + post_clr)
            if end > t - 0.8:
                notes.append("no post through %s's hole: a part sits right under it" % ref)
                continue
            P_ = S + _qdown(t - end)
            for i, j in cells:
                H[j][i] = P_
            posts.append((px, py, r, ref))
    # pockets (the deepest wins)
    for key, d in need.items():
        j, i = divmod(key, nx)
        z = S - _qup(d)
        if z < H[j][i]:
            H[j][i] = z
    if any(H[j][i] < 0 for j in range(ny) for i in range(nx)):
        for j in range(ny):
            H[j] = [max(0, v) for v in H[j]]

    up, down = real_side(design, "top"), real_side(design, "bottom")
    top = max(max(row) for row in H) / float(Q)
    if grid_out is not None:                     # heightmap for checks (jig X = board x - ox, Y = board y - oy)
        grid_out.update({"H": H, "nx": nx, "ny": ny, "res": res, "ox": ox, "oy": oy, "support": support,
                         "owner": owner, "owners": owners})
    notes += shape_notes(meshes, [fp.ref for fp in present])
    ign = jig_ignored(design)
    result: Dict[str, Any] = {
        "jig_size_mm": [round(nx * res, 2), round(ny * res, 2), round(top, 2)],
        "support_height_mm": round(support, 2), "floor_mm": floor, "resolution_mm": res,
        "board_bbox_in_jig_mm": [round(jx(bb.x0), 3), round(jy(bb.y0), 3), round(jx(bb.x1), 3), round(jy(bb.y1), 3)],
        "side": up,
        "orientation": ("PCB top side faces down into the jig; jig X = board X, jig Y = board Y (view from above "
                        "shows the board's bottom side)") if up == "top" else
                       ("PCB bottom side faces down into the jig; jig X = board X, jig Y = -board Y (view from above "
                        "shows the board's top side)"),
        "parts_to_place": [fp.ref for fp in selected],
        "pockets": sorted(pockets, key=lambda p: (p["type"] != "hold", C.natural_key(p["ref"]))),
        "posts": [{"hole": ref, "center": [round(px, 2), round(py, 2)], "diameter_mm": round(2 * r, 2)} for px, py, r, ref in posts],
        "pocket_floor": "flat" if flat else "shaped",
        "ignored": {r: w for r, w in sorted(ign.items()) if w != "no pads (nothing to solder)" or
                    any(m for m in design.board.by_ref[r].models if not m.hide)} if ign else {},
        "notes": notes + ["Place parts in the 'hold' pockets, lower the PCB %s side down onto the nest, solder from above." % up,
                          "Pockets follow each part's 3D shape; a hold pocket's floor is the part's own top, so every "
                          "part touches its pocket as the board lands (jig.hold_depth_offset_mm %+.2f mm)." % offset],
    }
    if png_path:
        write_heightmap_png(png_path, H, nx, ny)
        result["preview_png"] = png_path
    if lead > 0 and any(p["type"] == "lead" for p in pockets):
        result["notes"].append("Trim the soldered leads of the %s-side parts to at most %.1f mm before this pass "
                               "(lead reliefs %.1f mm deep)." % (down, max(0.5, lead - 1.0), lead))
    if stl_path:
        tris = heightmap_mesh(H, nx, ny, res)
        write_stl(stl_path, tris, "PCB TestKit solder jig %s%s" % (b.name, "" if up == "top" else " (bottom side)"))
        result["mesh"] = mesh_check(tris) if len(tris) < 600000 else {"triangles": len(tris)}
        result["stl"] = stl_path
    if scad_path:
        with open(scad_path, "w", encoding="utf-8") as fh:
            fh.write(scad_source(design, H, nx, ny, res, S, owner, owners, posts, up))
        result["scad"] = scad_path
    return result


def build_placement_jig(design: Design, stops: List[Dict[str, float]], stl_path: Optional[str] = None,
                        png_path: Optional[str] = None, grid_out: Optional[Dict[str, Any]] = None,
                        mounted: Optional[set] = None, nest: bool = False,
                        log: Optional[List[str]] = None) -> Dict[str, Any]:
    """The placement jig of the two-jig workflow: the PCB rests in it top side up while the parts are fitted.

    ``nest=True`` makes the paste-print nest of an SMD pass instead: the same support with pockets for the
    parts already on the down side, no lead wells, and a frame flush with the PCB's top surface all round so
    the stencil lies flat. ``mounted`` = refs already on the board (None: all).

    Print orientation = use orientation (Z up, PCB top up). Jig X = board x, jig Y = -board y (the view from
    above matches KiCad's editor). Heights:
      floor .. pcb_rest   solid block; the PCB's underside rests on its top surface
      wells               under every through-hole, down to the floor, for the leads (trim them to fit)
      parts               nothing of the jig comes within jig.bottom_part_clearance_mm of a part under the board, or
                          within jig.overhang_clearance_mm of a lead or of a part hanging over the edge (their real
                          3D shapes, :mod:`pcbtestkit.parts3d`)
      post holes          for the holding jig's posts through the mounting holes
      stops               short walls around the edge that locate the PCB; they end 0.2 mm below its top
                          surface so the holding jig can close over it (its rim is notched at the stops)
    """
    jc = design.cfg["jig"]
    b = design.board
    bb = b.outline_bbox()
    t = float(b.thickness)
    res = float(jc["resolution_mm"])
    floor = float(jc["floor_mm"])
    clearance = float(jc.get("placement_clearance_mm", 10.0))
    margin = float(jc.get("lead_well_margin_mm", 1.0))
    border = float(jc.get("nest_border_mm", 15.0) if nest else jc.get("placement_border_mm", 6.0))
    stop_len = float(jc.get("stop_length_mm", 8.0))
    stop_w = float(jc.get("stop_width_mm", 2.5))
    stop_clr = float(jc.get("stop_clearance_mm", 0.15))
    bot_clr = float(jc.get("bottom_part_clearance_mm", 0.8))
    ovh_clr = float(jc.get("overhang_clearance_mm", 0.5))
    notes: List[str] = []
    selected = [] if nest else select_parts(design)[0]
    present = present_parts(design, selected, mounted)
    meshes = view_meshes(design, present, log)
    ztop = t + 0.05                                  # higher than this, nothing meets the jig (its stops end below)
    ext = BBox(bb.x0 - border, bb.y0 - border, bb.x1 + border, bb.y1 + border)
    for fp in present:
        sb = P3.silhouette_bbox(meshes[fp.ref], z_max=ztop) if fp.ref in meshes else None
        if sb:
            g_ = max(bot_clr, ovh_clr) + 1.5
            ext.merge(BBox(sb[0] - g_, sb[1] - g_, sb[2] + g_, sb[3] + g_))
    lim = BBox(bb.x0 - border - 40.0, bb.y0 - border - 40.0, bb.x1 + border + 40.0, bb.y1 + border + 40.0)
    if ext.x0 < lim.x0 or ext.y0 < lim.y0 or ext.x1 > lim.x1 or ext.y1 > lim.y1:
        notes.append("a part reaches more than 40 mm past the board; the jig ends 40 mm out")
        ext = BBox(max(ext.x0, lim.x0), max(ext.y0, lim.y0), min(ext.x1, lim.x1), min(ext.y1, lim.y1))
    ax0, ay1 = ext.x0, ext.y1
    W_, L_ = ext.w, ext.h
    if (W_ / res) * (L_ / res) > 6e6:
        res = math.sqrt(W_ * L_ / 6e6)
    nx, ny = int(math.ceil(W_ / res)), int(math.ceil(L_ / res))

    def jx(x):
        return x - ax0

    def jy(y):
        return ay1 - y

    # the lowest the jig may reach under every cell (board z: 0 = the board's underside) and the parts below it
    cap: Dict[int, float] = {}
    bottom: List[str] = []
    for fp in present:
        pm = meshes.get(fp.ref)
        if pm is None:
            continue
        down = fp.side == "bottom"
        c_ = bot_clr if down else ovh_clr
        tris = [tr for tr in pm.tris if min(tr[2], tr[5], tr[8]) < ztop]
        if not tris:
            continue
        env = P3.envelope(tris, ax0, 0.0, res, nx, ny, c_, frame="y_up", ay1=ay1, exact=False)
        hit = False
        for key, (lo, _hi) in env.items():
            if lo >= ztop:
                continue
            z = lo - c_
            if z < cap.get(key, 1e9):
                cap[key] = z
            hit = True
        if hit and down:
            bottom.append(fp.ref)
    deepest = max([0.0] + [-z for z in cap.values()])
    if nest:
        clearance = max(1.0, deepest)                # no leads under the board in an SMD pass
    elif deepest > clearance:
        notes.append("lead clearance raised from %.1f to %.1f mm for the parts under the board" % (clearance, deepest))
        clearance = deepest
    REST, FLOOR = _qup(floor + clearance), _qdown(floor)
    rest = REST / float(Q)                                    # PCB underside
    H = [[REST] * nx for _ in range(ny)]
    hd = _hd(res)

    # lead wells under every component through-hole (any side), down to the floor
    wells = 0
    holes = {(round(x, 3), round(y, 3)) for x, y, _, _ in mounting_holes(design)}
    for fp in ([] if nest else design.components()):
        for pd in fp.pads:
            if pd.drill_w <= 0 or (round(pd.x, 3), round(pd.y, 3)) in holes:
                continue
            r = max(pd.drill_w / 2.0 + 0.6, min(max(pd.w, pd.h) / 2.0, pd.drill_w / 2.0 + 1.5)) + margin
            for i, j in _disk_cells(jx(pd.x), jy(pd.y), r, nx, ny, res, False):
                H[j][i] = min(H[j][i], FLOOR)
            wells += 1
    # holes for the holding jig's posts
    post_depth = min(_qdown(max(floor, rest - t - 2.0)), REST - _qup(2.0))
    for x, y, r, _ in ([] if nest else mounting_holes(design)):
        for i, j in _disk_cells(jx(x), jy(y), r + 0.6, nx, ny, res, False):
            H[j][i] = min(H[j][i], post_depth)
    # locating stops (or the nest's flush frame): only cells whose whole square clears the outline by stop_clr
    segs = outline_segments(design)
    gsegs = [(jx(a_), jy(b_), jx(c_), jy(d_)) for a_, b_, c_, d_ in segs]
    STOP = _qdown(rest + max(0.4, t - 0.2))
    if nest:
        STOP = _qdown(rest + t)                      # flush with the PCB's face, so the stencil lies flat
        ins, nr = _inside_grid(gsegs, nx, ny, res), _near_grid(gsegs, nx, ny, res, stop_clr + hd)
        for j in range(ny):
            for i in range(nx):
                if not ins[j][i] and not nr[j][i]:
                    H[j][i] = STOP
    stops_cut = []
    for a in ([] if nest else stops):
        reach = stop_len / 2.0 + stop_clr + stop_w
        cx_, cy_ = jx(a["x"]), jy(a["y"])
        for j in range(max(0, int((cy_ - reach) / res) - 1), min(ny, int((cy_ + reach) / res) + 2)):
            for i in range(max(0, int((cx_ - reach) / res) - 1), min(nx, int((cx_ + reach) / res) + 2)):
                bx_, by_ = ax0 + (i + 0.5) * res, ay1 - (j + 0.5) * res
                if not _in_stop(bx_, by_, a, stop_len) or point_inside(bx_, by_, segs):
                    continue
                d = min(pt_seg_dist(bx_, by_, *s) for s in segs)
                if d - hd >= stop_clr and d <= stop_clr + stop_w:
                    H[j][i] = STOP
                    if j * nx + i in cap:
                        stops_cut.append(a["side"])
    # nothing of the jig reaches a part, a lead or a connector hanging over the edge
    for key, z in cap.items():
        j, i = divmod(key, nx)
        q = max(0, _qdown(rest + z))
        if q < H[j][i]:
            H[j][i] = q
    if stops_cut:
        notes.append("a locating stop on the %s side was cut down where a part hangs over it"
                     % " and ".join(sorted(set(stops_cut))))
    # finger notches left and right, to lift the PCB out by its edges
    if jc.get("finger_notches", True):
        nd = _qdown(max(floor, rest - 4.0))
        ymid = jy(bb.cy)
        for xa, xb in ((0.0, jx(bb.x0) + 2.0), (jx(bb.x1) - 2.0, W_)):
            for j in range(ny):
                if abs((j + 0.5) * res - ymid) > 7.5:
                    continue
                for i in range(nx):
                    if xa <= (i + 0.5) * res <= xb and (nest or H[j][i] != STOP):   # never cut away a locating stop
                        H[j][i] = min(H[j][i], nd)
    if len(stops) < 3 and not nest:
        notes.append("only %d locating stop(s) fit around this outline; place the PCB carefully" % len(stops))
    if grid_out is not None:                     # heightmap for checks (jig X = board x - ax0, Y = ay1 - board y)
        grid_out.update({"H": H, "nx": nx, "ny": ny, "res": res, "ax0": ax0, "ay1": ay1, "rest": rest})
    up = real_side(design, "top")
    result: Dict[str, Any] = {
        "jig_size_mm": [round(nx * res, 2), round(ny * res, 2), round(max(max(row) for row in H) / float(Q), 2)],
        "pcb_rest_height_mm": round(rest, 2), "lead_clearance_mm": round(clearance, 2), "floor_mm": floor,
        "resolution_mm": res,
        "board_bbox_in_jig_mm": [round(jx(bb.x0), 3), round(jy(bb.y1), 3), round(jx(bb.x1), 3), round(jy(bb.y0), 3)],
        "orientation": ("PCB top side up; jig X = board X, jig Y = -board Y (the view from above matches KiCad)"
                        if up == "top" else
                        "PCB bottom side up; jig X = board X, jig Y = board Y (the board turned over about its X axis)"),
        "stops": [{"side": a["side"], "at": [round(a["x"], 2), round(a["y"], 2)]} for a in ([] if nest else stops)],
        "lead_wells": wells, "bottom_pockets": sorted(bottom, key=C.natural_key),
        "notes": notes + (["Rest the PCB %s side up in the nest; the frame is flush with the board, so the stencil lies "
                           "flat. Print it in PLA or PETG: it does not go into the reflow oven." % up] if nest else
                          ["Trim the leads to at most %.0f mm below the board, or raise jig.placement_clearance_mm."
                           % max(1.0, clearance - 0.5)]),
    }
    if nest:
        result["kind"] = "paste_nest"
        result["side"] = up
        result["down_side_pockets"] = result.pop("bottom_pockets")
        result.pop("lead_wells")
    if png_path:
        write_heightmap_png(png_path, H, nx, ny)
        result["preview_png"] = png_path
    if stl_path:
        tris = heightmap_mesh(H, nx, ny, res)
        write_stl(stl_path, tris, "PCB TestKit %s %s%s" % ("paste-print nest" if nest else "placement jig", b.name,
                                                             "" if up == "top" else " (bottom side up)"))
        result["mesh"] = mesh_check(tris) if len(tris) < 600000 else {"triangles": len(tris)}
        result["stl"] = stl_path
    return result


def build_jigs(design: Design, stl_path: Optional[str] = None, scad_path: Optional[str] = None,
               png_path: Optional[str] = None, place_stl: Optional[str] = None,
               place_png: Optional[str] = None, grids: Optional[Dict[str, Dict[str, Any]]] = None,
               file_for: Optional[Callable[[str, str], str]] = None, check: bool = True,
               fit_dir: Optional[str] = None) -> Dict[str, Any]:
    """Every solder jig of the board, in assembly order (:func:`assembly_passes`).

    Per hand-solder pass: the holding jig and, in the two-jig workflow (default), the placement jig that pairs
    with it; per SMD pass that needs one: a paste-print nest. Returns the first hand pass's jig (written to the
    given paths) with ``passes`` (the build order) and ``extra`` (the other jigs by key, e.g. "hand_bottom",
    "nest_top"). ``file_for(tag, ext)`` names their files; None computes the geometry only. ``grids`` receives
    the heightmaps per side ({"top": {"holding", "placement"}, ...}; "holding"/"placement" = the first pass).
    ``check``: every jig set gets ``fit``, the check against the parts' 3D shapes (:mod:`pcbtestkit.jigfit`).
    ``fit_dir``: also write every pass's jigs in place on the board there (:func:`write_fit_files`)."""
    jc = design.cfg["jig"]
    two = str(jc.get("workflow", "two_jig")).lower().replace("-", "_") != "single"
    stop_len = float(jc.get("stop_length_mm", 8.0))
    grids = grids if grids is not None else {}
    passes, pass_notes = assembly_passes(design)
    hand = [p for p in passes if p["process"] == "hand"]
    refs = {fp.ref for fp in design.components()}
    later: Dict[int, set] = {}
    acc: set = set()
    for p in reversed(hand):
        acc |= set(p["parts"])
        later[p["pass"]] = set(acc)
    log: List[str] = []

    def name(tag: str, ext: str) -> Optional[str]:
        return file_for(tag, ext) if file_for else None

    # batches: every jig holds several boards; geometry first, files once the heightmaps are tiled
    from .batch import layout as batch_layout
    bbx = design.board.outline_bbox()
    blay = batch_layout(design.cfg, bbx.w, bbx.h)
    tiled = blay["count"] > 1
    later_files: List[Tuple[Dict[str, Any], Dict[str, Any], str, Dict[str, Optional[str]], Any]] = []

    def hand_jig(side: str, mounted: set, files: Dict[str, Optional[str]]) -> Dict[str, Any]:
        v = view(design, side)
        stops: List[Dict[str, float]] = []
        if two:
            sel = select_parts(v)[0]
            stops = stop_anchors(v, outline_segments(v), stop_len, view_meshes(v, present_parts(v, sel, mounted), log),
                                 float(jc.get("stop_width_mm", 2.5)), float(jc.get("stop_clearance_mm", 0.15)))
        g = grids.setdefault(side, {})
        f = {"scad": files.get("scad")} if tiled else files       # tiled: the SCAD is wrapped after tiling
        jig = build_jig(v, f.get("stl"), f.get("scad"), f.get("png"), stops=stops,
                        grid_out=g.setdefault("holding", {}), mounted=mounted, log=log)
        if tiled:
            later_files.append((jig, g["holding"], "y_down", {"stl": files.get("stl"), "png": files.get("png"),
                                                               "scad": files.get("scad")}, v))
        jig["workflow"] = "two_jig" if two else "single"
        if two:
            jig["placement"] = build_placement_jig(v, stops, f.get("place_stl"), f.get("place_png"),
                                                   grid_out=g.setdefault("placement", {}), mounted=mounted, log=log)
            if tiled:
                later_files.append((jig["placement"], g["placement"], "y_up",
                                    {"stl": files.get("place_stl"), "png": files.get("place_png")}, v))
            jig["notes"] = [n for n in jig["notes"] if not n.startswith("Place parts in the 'hold' pockets")] + [
                "Two-jig workflow: put the PCB %s side up in the placement jig, fit the parts, put this holding jig "
                "on top (pockets down), turn the stack over, lift off the placement jig and solder from above." % side]
        if check:
            jig["fit"] = fit_check(v, g, jig, mounted)
            jig["notes"].append(jig["fit"]["summary"])
        if fit_dir:
            jig["fit_files"] = _fit_files(v, g, mounted, "%s-%s" % (design.board.name, side))
        return jig

    def _fit_files(v, g, mounted, stem, nest=False):
        os.makedirs(fit_dir, exist_ok=True)
        with open(os.path.join(fit_dir, "README.txt"), "w", encoding="utf-8") as fh:
            fh.write(FIT_README)
        return [os.path.basename(f) for f in write_fit_files(v, g, mounted, os.path.join(fit_dir, stem), nest, log)]

    primary = hand[0] if hand else None
    p_side = primary["side"] if primary else "top"
    p_mounted = refs - (later[primary["pass"]] if primary else set())
    jig = hand_jig(p_side, p_mounted, {"stl": stl_path, "scad": scad_path, "png": png_path, "place_stl": place_stl,
                                       "place_png": place_png})
    grids["holding"], grids["placement"] = grids[p_side]["holding"], grids[p_side].get("placement", {})
    jig["key"] = "hand_%s" % p_side if primary else None
    extra: Dict[str, Any] = {}
    summary: List[Dict[str, Any]] = []
    smd_done: set = set()
    for p in passes:
        key = None
        if p["process"] == "hand":
            key = "hand_%s" % p["side"]
            if p is not primary:
                tag = "jig-%s" % p["side"]
                extra[key] = hand_jig(p["side"], refs - later[p["pass"]],
                                      {"stl": name(tag, "stl"), "scad": name(tag, "scad"), "png": name(tag, "png"),
                                       "place_stl": name(tag + "-placement", "stl"),
                                       "place_png": name(tag + "-placement", "png")})
        elif p.get("nest"):
            key = "nest_%s" % p["side"]
            tag = "paste-nest-%s" % p["side"]
            g = grids.setdefault("nest_%s" % p["side"], {})
            vn = view(design, p["side"])
            extra[key] = build_placement_jig(vn, [], None if tiled else name(tag, "stl"), None if tiled else name(tag, "png"),
                                             grid_out=g, mounted=set(smd_done), nest=True, log=log)
            if check:
                extra[key]["fit"] = fit_check(vn, {"placement": g}, extra[key], set(smd_done), nest=True)
                extra[key]["notes"].append(extra[key]["fit"]["summary"])
            if fit_dir:
                extra[key]["fit_files"] = _fit_files(vn, {"placement": g}, set(smd_done),
                                                     "%s-paste-nest-%s" % (design.board.name, p["side"]), True)
            if tiled:
                later_files.append((extra[key], g, "y_up", {"stl": name(tag, "stl"), "png": name(tag, "png")}, vn))
        if p["process"] == "smt":
            smd_done |= set(p["parts"])
        summary.append({"pass": p["pass"], "process": p["process"], "side": p["side"],
                        "parts": list(p["parts"]) if p["process"] == "hand" else len(p["parts"]), "jig": key})
    jig["passes"] = summary
    jig["extra"] = extra
    jig["notes"] = jig["notes"] + pass_notes + sorted(set(log))
    if tiled:
        _tile_batch(design, jig, blay, later_files)
    if len(hand) > 1:
        jig["notes"].append("Parts on both sides: %s. Each side has its own jig set; the later set has pockets for "
                            "the parts already soldered." % " then ".join("%s side (pass %d)" % (p["side"], p["pass"])
                                                                          for p in hand))
    return jig


def fit_check(v: Design, g: Dict[str, Dict[str, Any]], jig: Dict[str, Any], mounted: Optional[set],
              nest: bool = False) -> Dict[str, Any]:
    """The jigs of one pass against the parts' 3D shapes and the bare board (:mod:`pcbtestkit.jigfit`), as
    {"ok", "summary", ...}: what collides, which hand-soldered parts don't touch their pockets."""
    from . import jigfit as F
    selected = [] if nest else select_parts(v)[0]
    present = present_parts(v, selected, mounted)
    meshes = view_meshes(v, present)
    refs = [fp.ref for fp in present if fp.ref in meshes]
    hold = [fp.ref for fp in selected]
    out: Dict[str, Any] = {"parts_checked": len(refs), "shapes": {}}
    for r in refs:
        out["shapes"][meshes[r].source] = out["shapes"].get(meshes[r].source, 0) + 1
    problems: List[str] = []
    if not nest:
        h = F.check_holding(v, g["holding"], meshes, refs, hold, Q)
        out.update({"holding_collisions": h["collisions"], "touching": h["touching"], "loose": h["loose"]})
        if h["collisions"]:
            problems.append("parts reach into the holding jig: %s" % _mm_list(h["collisions"]))
        if h["loose"]:
            problems.append("hand-soldered parts don't touch their pockets: %s" % _mm_list(h["loose"]))
        # held sideways: how far each part can slide before it meets a wall (the pocket's clearance, plus at most a
        # cell of the grid: more than that and it is not held on that side)
        jc = v.cfg["jig"]
        low = float(jc["hold_clearance_mm"])
        high = round(low + float(g["holding"]["res"]) + 0.05, 3)
        hb = [meshes[r].bbox for r in hold if r in meshes and meshes[r].bbox]
        near = [r for r in refs if meshes[r].bbox and any(
            meshes[r].bbox[0] <= b_[3] + high + 1 and meshes[r].bbox[3] >= b_[0] - high - 1 and
            meshes[r].bbox[1] <= b_[4] + high + 1 and meshes[r].bbox[4] >= b_[1] - high - 1 for b_ in hb)]
        occ = F.occupied(v, g["holding"], meshes, near)
        far = round(2 * low + 2 * float(g["holding"]["res"]) + 0.1, 3)      # too close for a wall between them
        play, held = {}, {}
        for r in hold:
            if r in meshes:
                play[r], held[r] = F.side_play(v, g["holding"], meshes[r], Q, low, high, occ=occ, far=far)
        free = sorted((r for r, b_ in held.items() if not all(b_.values())), key=C.natural_key)
        by_part = {r: {d_: "%s %.2f mm" % (b_[d_], play[r][d_]) for d_ in b_ if b_[d_] not in ("", "jig")}
                   for r, b_ in held.items()}
        by_part = {r: x for r, x in by_part.items() if x}
        out.update({"side_play": play, "side_play_limit_mm": high, "not_held_sideways": free, "held_by_parts": by_part})
        if free:
            problems.append("parts not held sideways (they slide %.2f mm or more): %s" % (high, ", ".join(free[:10])))
    pg = g.get("placement") or {}
    if pg:
        pl = F.check_placement(v, pg, meshes, refs, Q)
        out["placement_collisions"] = pl["collisions"]
        if pl["collisions"]:
            problems.append("parts reach into the %s: %s" % ("paste nest" if nest else "placement jig",
                                                              _mm_list(pl["collisions"])))
    bd = F.check_board(v, g.get("holding"), pg or None, Q)
    out["board_contacts"] = {"holding": bd["holding"], "placement": bd["placement"]}
    if bd["holding"] or bd["placement"]:
        problems.append("the bare board touches the jig away from its support (%d cells)" % (bd["holding"] + bd["placement"]))
    if not nest and pg:
        st = stack_collisions(v, g)
        out["stack_collisions"] = len(st)
        if st:
            problems.append("the holding jig touches the placement jig at %d places" % len(st))
    est = sorted((r for r in refs if not meshes[r].exact), key=C.natural_key)
    out["estimated_shapes"] = est
    out["ok"] = not problems
    if problems:
        out["summary"] = "FIT CHECK FAILED: " + "; ".join(problems) + "."
    elif nest:
        out["summary"] = ("Fit check (%d part%s, their 3D shapes): the board lies flat on the nest and no part touches "
                          "it." % (len(refs), "" if len(refs) == 1 else "s"))
    else:
        bp = out.get("held_by_parts") or {}
        vals = [x for r, p_ in out.get("side_play", {}).items() for d_, x in p_.items() if d_ not in bp.get(r, {})]
        side = (" and are held on all four sides (%.2f to %.2f mm of play)" % (min(vals), max(vals))) if vals else ""
        if bp:
            items = ["%s %s against %s (%s)" % (r, d_, w.split(" ")[0], w.split(" ", 1)[1])
                     for r in sorted(bp, key=C.natural_key) for d_, w in sorted(bp[r].items())]
            side += ("; held by the part beside it, too close for a jig wall between them: %s"
                     % ", ".join(items[:6]) + (" ..." if len(items) > 6 else ""))
        out["summary"] = ("Fit check (%d parts, their 3D shapes): nothing touches on the way in; all %d hand-soldered "
                          "parts touch their pockets as the board lands on the support (within %.2f mm)%s%s."
                          % (len(refs), len(hold), F.TOUCH, side, "; the holding jig closes over the placement jig "
                             "without touching it" if pg else ""))
    if est:
        out["summary"] += " Estimated (box) shapes: %s." % ", ".join(est[:10])
    return out


FIT_README = """These files show the jigs in their working position on the board, in KiCad's own 3D coordinates
(the same as KiCad's File > Export > STL of the board), so they line up when opened together:

  <board>-<side>-board.stl            the board and every part on it during that pass (KiCad's 3D shapes)
  <board>-<side>-holding-jig.stl      the holding jig, turned over onto the board
  <board>-<side>-placement-jig.stl    the placement jig under the board
  <board>-paste-nest-<side>-*.stl     an SMD pass's paste-print nest and the board on it

Move a jig straight away from the board and back: nothing touches until it is home, and then every hand-soldered
part touches its pocket floor at the moment the board's face meets the support. KiCad draws the board body
slightly thinner than the board (copper and solder mask are separate layers); parts sit on the board's faces, and
the jigs are placed on those faces too. Print the jig STLs next to this folder, not these copies.
"""


def write_fit_files(v: Design, g: Dict[str, Dict[str, Any]], mounted: Optional[set], prefix: str,
                    nest: bool = False, log: Optional[List[str]] = None) -> List[str]:
    """The board with its parts and the jigs of one pass in their working position, in KiCad's 3D coordinates
    (see FIT_README). ``prefix``: the path the file names start with. Returns the files written."""
    base = getattr(v, "_d", v)
    t = float(v.board.thickness)
    turned = getattr(v, "up_side", "top") == "bottom"
    up, down = real_side(v, "top"), real_side(v, "bottom")
    selected = [] if nest else select_parts(v)[0]
    present = present_parts(v, selected, mounted)
    meshes = P3.part_meshes(base, log)

    def k(xv: float, yv: float, zv: float, facing: str):
        x, y, z = (xv, -yv, t - zv) if turned else (xv, yv, zv)
        return P3.to_kicad(base, facing, x, y, z)
    out = []
    board: List[Tuple] = []
    for tri in P3.board_body(base, log) or []:
        board.append((tri[0:3], tri[3:6], tri[6:9]))
    for fp in present:
        pm = meshes.get(fp.ref)
        if pm is None:
            continue
        side = getattr(fp, "_orig", fp).side
        for a in pm.tris:
            board.append((P3.to_kicad(base, side, a[0], a[1], a[2]), P3.to_kicad(base, side, a[3], a[4], a[5]),
                          P3.to_kicad(base, side, a[6], a[7], a[8])))
    path = prefix + "-board.stl"
    write_stl(path, board, "PCB TestKit fit check: %s and its parts" % v.board.name)
    out.append(path)
    hg = g.get("holding")
    if hg and not nest:
        tris = heightmap_mesh(hg["H"], hg["nx"], hg["ny"], hg["res"])
        S = hg["support"]
        moved = [tuple(k(p[0] + hg["ox"], p[1] + hg["oy"], t + S - p[2], up) for p in tr) for tr in tris]
        path = prefix + "-holding-jig.stl"
        write_stl(path, moved, "PCB TestKit fit check: holding jig in place")
        out.append(path)
    pg = g.get("placement")
    if pg:
        tris = heightmap_mesh(pg["H"], pg["nx"], pg["ny"], pg["res"])
        moved = [tuple(k(p[0] + pg["ax0"], pg["ay1"] - p[1], p[2] - pg["rest"], down) for p in tr) for tr in tris]
        path = prefix + ("-nest.stl" if nest else "-placement-jig.stl")
        write_stl(path, moved, "PCB TestKit fit check: %s in place" % ("paste-print nest" if nest else "placement jig"))
        out.append(path)
    return out


def _mm_list(d: Dict[str, float]) -> str:
    items = sorted(d.items(), key=lambda kv: -kv[1])
    return ", ".join("%s %.2f mm" % (r, v) for r, v in items[:8]) + (" ..." if len(items) > 8 else "")


def _tile_batch(design: Design, primary: Dict[str, Any], blay: Dict[str, Any], later: List[Tuple]) -> None:
    """Tile every jig of the board for a batch (batch.count boards), write its files and record the layout.

    The holding and placement jigs share one board pitch (the larger of their footprints), so they still close
    over every board when the stack is turned over; the paste-print nests, which are used on their own, get their
    own pitch with their flush frames touching. When the whole batch does not fit the printer bed
    (batch.bed_mm), each jig holds fewer boards and more sets are printed."""
    from .batch import fit_on_bed
    if not later:
        return
    jc = design.cfg["jig"]
    n_all = int(blay["count"])
    bed = (design.cfg.get("batch") or {}).get("bed_mm") or [220.0, 220.0]
    res = later[0][1]["res"]
    same_res = all(abs(g["res"] - res) < 1e-9 for _, g, _, _, _ in later)
    pitch: Dict[bool, Tuple[int, int]] = {}
    if same_res:
        sizes = []
        for is_nest in (False, True):
            gs = [g for rd, g, _, _, _ in later if (rd.get("kind") == "paste_nest") == is_nest]
            if gs:
                px_ = max(g["nx"] for g in gs)
                py_ = max(g["ny"] for g in gs)
                pitch[is_nest] = (px_, py_)
                sizes.append((px_ * res, py_ * res))
        nb, cols, rows = fit_on_bed(n_all, int(blay["cols"]), sizes, (float(bed[0]), float(bed[1])))
    else:                                       # a coarsened (huge) board: one board per jig
        nb, cols, rows = 1, 1, 1
        primary["notes"].append("batch: these jigs hold one board each (board too large to tile)")
    sets = int(math.ceil(n_all / float(nb)))
    floor = int(round(float(jc["floor_mm"]) * Q))
    name = design.board.name
    for rd, g, frame, files, v in later:
        nest = rd.get("kind") == "paste_nest"
        px, py = pitch.get(nest, (0, 0))
        if nb > 1:
            # between the boards: a floor web joins them (holding / placement jig); the paste nest stays flush
            tile_grid(g, frame, cols, rows, nb, px, py, g["H"][0][0] if nest else floor)
            rd["jig_size_mm"][0], rd["jig_size_mm"][1] = round(g["nx"] * g["res"], 2), round(g["ny"] * g["res"], 2)
            if frame == "y_up":
                dy = (rows - 1) * py * g["res"]
                bbj = rd["board_bbox_in_jig_mm"]
                rd["board_bbox_in_jig_mm"] = [bbj[0], round(bbj[1] + dy, 3), bbj[2], round(bbj[3] + dy, 3)]
        else:
            g["boards"] = [(0.0, 0.0)]
        rd["batch"] = {"boards": nb, "cols": cols, "rows": rows, "pitch_mm": [round(px * g["res"], 3), round(py * g["res"], 3)],
                       "board_offsets_mm": [list(o) for o in g["boards"]], "sets": sets, "batch_count": n_all}
        what = "paste-print nest" if nest else ("placement jig" if frame == "y_up" else "solder jig")
        if files.get("png"):
            write_heightmap_png(files["png"], g["H"], g["nx"], g["ny"])
            rd["preview_png"] = files["png"]
        if files.get("stl"):
            tris = heightmap_mesh(g["H"], g["nx"], g["ny"], g["res"])
            write_stl(files["stl"], tris, "PCB TestKit %s %s (%d boards)" % (what, name, nb))
            rd["mesh"] = mesh_check(tris) if len(tris) < 600000 else {"triangles": len(tris)}
            rd["stl"] = files["stl"]
        if files.get("scad") and os.path.exists(files["scad"]) and nb > 1:
            with open(files["scad"], "r", encoding="utf-8") as fh:
                one = fh.read()
            offs = ", ".join("[%.3f, %.3f]" % (ox_, oy_) for ox_, oy_ in g["boards"])
            with open(files["scad"], "w", encoding="utf-8") as fh:
                fh.write("// PCB TestKit solder jig for a batch: %d boards (%d x %d), %.3f x %.3f mm apart\n"
                         "module one_board() {\n%s}\n\nfor (p = [%s]) translate([p[0], p[1], 0]) one_board();\n"
                         "cube([%.3f, %.3f, %.3f]);   // floor web joining the boards\n" % (
                             nb, cols, rows, px * g["res"], py * g["res"], "".join("  " + ln + "\n" for ln in one.splitlines()),
                             offs, g["nx"] * g["res"], g["ny"] * g["res"], float(jc["floor_mm"])))
            rd["scad"] = files["scad"]
    primary["batch"] = {"boards_per_jig": nb, "cols": cols, "rows": rows, "sets": sets, "batch_count": n_all,
                        "bed_mm": [float(bed[0]), float(bed[1])]}
    if nb < n_all:
        primary["notes"].append("Batch of %d boards: a jig set for all of them does not fit the %.0f x %.0f mm printer bed, "
                                "so each jig holds %d boards (%d x %d); print %d sets." % (n_all, bed[0], bed[1], nb, cols,
                                                                                          rows, sets))
    elif nb > 1:
        primary["notes"].append("Batch of %d boards: every jig holds all of them (%d x %d)." % (nb, cols, rows))


def jig_of(jig: Dict[str, Any], key: Optional[str]) -> Optional[Dict[str, Any]]:
    """The jig of a pass by key ("hand_top", "nest_bottom", ...): the top-level jig or one of ``extra``."""
    if not key or not jig:
        return None
    if jig.get("key") == key:
        return jig
    return (jig.get("extra") or {}).get(key)



def stack_collisions(design: Design, grids: Dict[str, Dict[str, Any]], gap: float = 0.1) -> List[Tuple[float, float, str]]:
    """Where the holding jig, turned over onto the PCB in the placement jig, would touch the placement jig.

    In the placement jig's frame the PCB lies on pcb_rest .. pcb_rest + thickness and the holding jig's lowest point
    over a cell is pcb_rest + thickness + (support - H_hold). Only holding-jig cells that reach below the board's
    face (rim, posts) can meet the placement jig; each is compared with every placement-jig cell its square
    overlaps, so the two grids need not line up. Batch jigs hold a board at every offset of the grids' ``boards``.
    Returns (board x, board y, "rim" | "post") for every conflict."""
    A, B = grids["placement"], grids["holding"]
    if not A or not B:
        return []
    t = float(design.board.thickness)
    rest, support = A["rest"], B["support"]
    S = int(round(support * Q))
    ra, rb = A["res"], B["res"]
    pmax = max(max(row) for row in A["H"]) / float(Q)
    lim = S - int(math.floor((pmax - rest - t + gap) * Q))      # holding cells at or below this can't collide
    holes = mounting_holes(design)
    offsets = A.get("boards") or [(0.0, 0.0)]
    out = []
    for jb in range(B["ny"]):
        row = B["H"][jb]
        for ib in range(B["nx"]):
            hb = row[ib]
            if hb <= lim:
                continue
            low = rest + t + (S - hb) / float(Q)                 # the holding jig's lowest point over this cell
            x0, y0 = B["ox"] + ib * rb, B["oy"] + jb * rb
            x1, y1 = x0 + rb, y0 + rb
            i0, i1 = max(0, int(math.floor((x0 - A["ax0"]) / ra))), min(A["nx"] - 1, int(math.floor((x1 - A["ax0"]) / ra)))
            j0, j1 = max(0, int(math.floor((A["ay1"] - y1) / ra))), min(A["ny"] - 1, int(math.floor((A["ay1"] - y0) / ra)))
            for ja in range(j0, j1 + 1):
                ar = A["H"][ja]
                for ia in range(i0, i1 + 1):
                    if ar[ia] / float(Q) > low - gap:
                        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
                        post = hb > S and any(math.hypot(cx - tx - hx, cy - ty - hy) <= hr for hx, hy, hr, _ in holes
                                              for tx, ty in offsets)
                        out.append((round(cx, 3), round(cy, 3), "post" if post else "rim"))
                        break
                else:
                    continue
                break
    return out


def _rects(cells: Dict[Tuple[int, int], int]) -> List[Tuple[int, int, int, int, int]]:
    """Greedy rectangles (i, j, w, h, value) covering cells of equal value."""
    left = dict(cells)
    out = []
    for (i, j) in sorted(cells, key=lambda c: (c[1], c[0])):
        if (i, j) not in left:
            continue
        v = left[(i, j)]
        w = 1
        while left.get((i + w, j)) == v:
            w += 1
        h = 1
        while all(left.get((i + k, j + h)) == v for k in range(w)):
            h += 1
        for dj in range(h):
            for di in range(w):
                del left[(i + di, j + dj)]
        out.append((i, j, w, h, v))
    return out


def scad_source(design, H, nx: int, ny: int, res: float, S: int, owner: Dict[int, int],
                owners: List[Tuple[str, str]], posts, up: str = "top") -> str:
    """The holding jig as OpenSCAD, exactly the STL's shape: the support block, the rim and posts on it, and one
    group of cuts per part pocket (labelled, so a pocket can be edited or removed by hand)."""
    q = float(Q)
    groups: Dict[str, Dict[Tuple[int, int], int]] = {}
    raised: Dict[Tuple[int, int], int] = {}
    for j in range(ny):
        row = H[j]
        for i in range(nx):
            v = row[i]
            if v > S:
                raised[(i, j)] = v
            elif v < S:
                k = owner.get(j * nx + i)
                label = ("%s (%s)" % owners[k]) if k is not None else "notches"
                groups.setdefault(label, {})[(i, j)] = v
    out = ["// Solder jig for %s - generated by PCB TestKit (the same shape as the STL)" % design.board.name if up == "top" else
           "// Solder jig for %s, bottom-side parts - generated by PCB TestKit (the same shape as the STL)" % design.board.name,
           "// Parts go upside-down into the pockets; the PCB goes %s-side down onto the support." % up,
           "// Each pocket follows its part's 3D shape: one group of cuts per part below.",
           "W = %.3f; L = %.3f; support = %.3f; cell = %.4f;" % (nx * res, ny * res, S / q, res), "",
           "module cut(i, j, w, h, z) { translate([i * cell, j * cell, z]) cube([w * cell, h * cell, support - z + 0.01]); }",
           "module up(i, j, w, h, z) { translate([i * cell, j * cell, support - 0.01]) cube([w * cell, h * cell, z - support + 0.01]); }",
           "", "difference() {", "  union() {", "    cube([W, L, support]);"]
    for px, py, r, ref in posts:
        out.append("    // post through %s (%.2f mm)" % (ref, 2 * r))
    for i, j, w, h, v in _rects(raised):
        out.append("    up(%d, %d, %d, %d, %.2f);" % (i, j, w, h, v / q))
    out.append("  }")
    for label in sorted(groups, key=lambda s: (s == "notches", C.natural_key(s))):
        out.append("  // %s" % label)
        for i, j, w, h, v in _rects(groups[label]):
            out.append("  cut(%d, %d, %d, %d, %.2f);" % (i, j, w, h, v / q))
    out.append("}")
    return "\n".join(out) + "\n"
