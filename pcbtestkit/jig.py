"""3D-printable solder jig for hand-soldered (typically through-hole) parts.

Workflow it is designed for:
  1. Drop the selected parts upside-down into their snug "hold" pockets.
  2. Lower the PCB, top side down, into the nest; the leads slide through
     their holes. Already-assembled SMD parts sit in clearance pockets.
  3. Solder from the (now upward-facing) bottom side; every part is held flush.

Jig coordinates: X = board x, Y = board y (KiCad), Z up. Because the PCB is
flipped about the X axis this is a proper rotation, so the jig is not
mirrored. Outputs: watertight binary STL (heightmap meshing) and an
editable OpenSCAD file with exact shapes.
"""
from __future__ import annotations

import math
import struct
from typing import Any, Dict, List, Optional, Tuple

from . import classify as C
from .geometry import BBox, chain_segments, point_in_rect, pt_seg_dist
from .models3d import model_body
from .pcb import Footprint
from .project import Design

Q = 100  # height quantum: 0.01 mm


class Pocket:
    def __init__(self, ref, kind, cx, cy, depth, shape, w=0.0, h=0.0, angle=0.0, r=0.0, height=0.0, src=""):
        self.ref, self.kind, self.cx, self.cy, self.depth = ref, kind, cx, cy, depth
        self.shape, self.w, self.h, self.angle, self.r = shape, w, h, angle, r
        self.height, self.src = height, src

    def bbox(self) -> BBox:
        if self.shape == "circle":
            return BBox(self.cx - self.r, self.cy - self.r, self.cx + self.r, self.cy + self.r)
        d = math.hypot(self.w, self.h) / 2
        return BBox(self.cx - d, self.cy - d, self.cx + d, self.cy + d)

    def contains(self, x, y) -> bool:
        if self.shape == "circle":
            return math.hypot(x - self.cx, y - self.cy) <= self.r
        return point_in_rect(x, y, self.cx, self.cy, self.w, self.h, self.angle)

    def to_dict(self):
        d = {"ref": self.ref, "type": self.kind, "center": [round(self.cx, 3), round(self.cy, 3)],
             "depth_mm": round(self.depth, 2), "part_height_mm": round(self.height, 2), "height_source": self.src}
        if self.shape == "circle":
            d["diameter_mm"] = round(2 * self.r, 2)
        else:
            d.update({"size_mm": [round(self.w, 2), round(self.h, 2)], "rotation_deg": round(self.angle, 1)})
        return d


def select_parts(design: Design) -> Tuple[List[Footprint], List[str]]:
    jc = design.cfg["jig"]
    notes: List[str] = []
    comps = design.components()
    excl = set(jc.get("exclude") or [])
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
    out = []
    for fp in sel:
        if fp.ref in excl:
            continue
        if fp.side == "bottom":
            notes.append("%s is on the bottom side; it needs a second jig (not generated)" % fp.ref)
            continue
        out.append(fp)
    return out, notes


def _rect_pocket(fp: Footprint, lb: BBox, grow: float, kind: str, depth: float, height: float, src: str,
                 jx, jy) -> Pocket:
    cx, cy = fp.to_board(lb.cx, lb.cy)
    return Pocket(fp.ref, kind, jx(cx), jy(cy), depth, "rect", lb.w + 2 * grow, lb.h + 2 * grow, fp.angle,
                  height=height, src=src)


def build_pockets(design: Design, selected: List[Footprint], jx, jy) -> List[Pocket]:
    jc = design.cfg["jig"]
    sel = {fp.ref for fp in selected}
    pockets: List[Pocket] = []
    for fp in design.components():
        if fp.side != "top" or C.is_mounting_hole(fp) or not fp.pads and not fp.graphics:
            continue
        if fp.dnp and fp.ref not in sel:
            continue
        height, src = design.height_of(fp)
        if fp.ref in sel:
            clr = float(jc["hold_clearance_mm"])
            depth = height + float(jc["hold_depth_offset_mm"])
            mb = model_body(fp, design.project_dir)
            circ = fp.dominant_circle("Fab")
            body, bsrc = fp.body_local_bbox()
            if mb is not None:
                fab_bb, fab_src = body, bsrc
                body = BBox(mb[0].x0, mb[0].y0, mb[0].x1, mb[0].y1)
                if fab_src == "fab":
                    body.merge(fab_bb)       # a pocket must never be smaller than the documented body
                bsrc = "3D model" + (" + fab" if fab_src == "fab" else "")
            if circ and body.valid and abs(2 * circ[2] - max(body.w, body.h)) < 0.6:
                cx, cy = fp.to_board(circ[0], circ[1])
                pockets.append(Pocket(fp.ref, "hold", jx(cx), jy(cy), depth, "circle", r=circ[2] + clr, height=height,
                                      src="%s; outline %s" % (src, "fab circle")))
            else:
                pockets.append(_rect_pocket(fp, body, clr, "hold", depth, height, "%s; outline %s" % (src, bsrc), jx, jy))
        elif jc.get("relief_all_top", True):
            crt = fp.courtyard_local()
            mb = model_body(fp, design.project_dir)
            if mb is not None:
                crt = BBox(min(crt.x0, mb[0].x0), min(crt.y0, mb[0].y0), max(crt.x1, mb[0].x1), max(crt.y1, mb[0].y1))
            pockets.append(_rect_pocket(fp, crt, float(jc["relief_clearance_mm"]), "relief",
                                        height + float(jc["relief_extra_depth_mm"]), height, src, jx, jy))
    return pockets


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
                    span = (min(a, b), max(a, b))
            if run and span == run[1]:
                continue
            if run:
                lo, hi = run[1]
                faces.append((v(i, run[0], lo), v(i, j, lo), v(i, j, hi), v(i, run[0], hi)))
            run = (j, span) if span else None
    # walls along y boundaries (between rows j-1 and j)
    for j in range(ny + 1):
        run = None
        for i in range(nx + 1):
            span = None
            if i < nx:
                a, b = cell(i, j - 1), cell(i, j)
                if a != b:
                    span = (min(a, b), max(a, b))
            if run and span == run[1]:
                continue
            if run:
                lo, hi = run[1]
                faces.append((v(run[0], j, lo), v(i, j, lo), v(i, j, hi), v(run[0], j, hi)))
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


def mounting_holes(design: Design) -> List[Tuple[float, float, float, str]]:
    """(x, y, drill radius, ref) of every mounting hole in board coordinates (where the jig posts go)."""
    out = []
    for fp in design.components():
        for pd in fp.pads:
            if pd.drill_w >= 2.0 and (pd.is_npth or C.is_mounting_hole(fp)):
                out.append((pd.x, pd.y, pd.drill_w / 2, fp.ref))
    return out


def stop_anchors(design: Design, segs, length: float) -> List[Dict[str, float]]:
    """Where the placement jig's locating stops touch the board edge: up to two per side (left, right, top,
    bottom), away from parts that overhang the edge. Board coordinates, with the outward normal."""
    bb = design.board.outline_bbox()
    # parts that reach over the board edge (connectors): no stop next to them
    over: List[BBox] = []
    for fp in design.components():
        c = fp.courtyard_local()
        if not c.valid:
            continue
        g = BBox()
        for lx, ly in ((c.x0, c.y0), (c.x1, c.y0), (c.x1, c.y1), (c.x0, c.y1)):
            g.add(*fp.to_board(lx, ly))
        if g.x0 < bb.x0 + 0.5 or g.y0 < bb.y0 + 0.5 or g.x1 > bb.x1 - 0.5 or g.y1 > bb.y1 - 0.5:
            over.append(g.grow(1.5))
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
        for f in wants + (0.15, 0.85, 0.4, 0.6):
            target = pts[0][along] + f * span
            p = min(pts, key=lambda q: abs(q[along] - target))
            if any(bx_.contains(p[0], p[1]) for bx_ in over):
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


# =========================================================================== #
def build_jig(design: Design, stl_path: Optional[str] = None, scad_path: Optional[str] = None,
              png_path: Optional[str] = None, stops: Optional[List[Dict[str, float]]] = None,
              grid_out: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The holding jig. With ``stops`` (two-jig workflow) its rim is notched where the placement jig's
    locating stops sit, so the two jigs close over the PCB without touching."""
    jc = design.cfg["jig"]
    b = design.board
    bb = b.outline_bbox()
    res = float(jc["resolution_mm"])
    edge_clr = float(jc["edge_clearance_mm"])
    rim_w = float(jc["rim_mm"])
    border = edge_clr + rim_w
    ox, oy = bb.x0 - border, bb.y0 - border

    def jx(x):
        return x - ox

    def jy(y):
        return y - oy

    selected, notes = select_parts(design)
    pockets = build_pockets(design, selected, jx, jy)
    # pockets may overhang the board (connectors); grow the jig to include them
    ext = BBox(0, 0, bb.w + 2 * border, bb.h + 2 * border)
    for p in pockets:
        ext.merge(p.bbox().grow(1.5))
    # overhanging parts may enlarge the jig, but never by more than 40 mm per side
    ext = BBox(max(ext.x0, -40.0), max(ext.y0, -40.0), min(ext.x1, bb.w + 2 * border + 40.0),
               min(ext.y1, bb.h + 2 * border + 40.0))
    for p in pockets:
        if p.depth > 80.0:
            notes.append("%s: pocket depth %.0f mm capped at 80 mm - check its height" % (p.ref, p.depth))
            p.depth = 80.0
    shift_x, shift_y = -min(0.0, ext.x0), -min(0.0, ext.y0)
    if shift_x or shift_y:
        for p in pockets:
            p.cx += shift_x
            p.cy += shift_y
        ox -= shift_x
        oy -= shift_y
    W_ = max(ext.x1 + shift_x, bb.w + 2 * border)
    L_ = max(ext.y1 + shift_y, bb.h + 2 * border)
    if (W_ / res) * (L_ / res) > 6e6:                       # keep memory/time bounded on huge boards
        res = math.sqrt(W_ * L_ / 6e6)
        notes.append("jig resolution coarsened to %.2f mm for this board size" % res)
    nx, ny = int(math.ceil(W_ / res)), int(math.ceil(L_ / res))
    max_depth = max([p.depth for p in pockets] + [1.0])
    floor = float(jc["floor_mm"])
    support = floor + max_depth
    rim_h = float(jc["rim_height_mm"])
    S = int(round(support * Q))
    H = [[S] * nx for _ in range(ny)]

    # board outline (jig coords)
    segs = [(jx(s[0]), jy(s[1]), jx(s[2]), jy(s[3])) for s in b.edge_segs]
    if not segs:
        x0, y0, x1, y1 = jx(bb.x0), jy(bb.y0), jx(bb.x1), jy(bb.y1)
        segs = [(x0, y0, x1, y0), (x1, y0, x1, y1), (x1, y1, x0, y1), (x0, y1, x0, y0)]
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
    near = [bytearray(nx) for _ in range(ny)]
    for ax, ay, bx_, by_ in segs:
        i0 = max(0, int((min(ax, bx_) - edge_clr) / res) - 1)
        i1 = min(nx - 1, int((max(ax, bx_) + edge_clr) / res) + 1)
        j0 = max(0, int((min(ay, by_) - edge_clr) / res) - 1)
        j1 = min(ny - 1, int((max(ay, by_) + edge_clr) / res) + 1)
        for j in range(j0, j1 + 1):
            yc = (j + 0.5) * res
            for i in range(i0, i1 + 1):
                if not near[j][i] and pt_seg_dist((i + 0.5) * res, yc, ax, ay, bx_, by_) <= edge_clr:
                    near[j][i] = 1
    R = int(round((support + rim_h) * Q))
    for j in range(ny):
        for i in range(nx):
            if not inside[j][i] and not near[j][i]:
                H[j][i] = R
    # two-jig workflow: notch the rim (down to the support plane) where the placement jig's stops sit
    notch_len = float(jc.get("stop_length_mm", 8.0)) + 2.0
    for a in stops or []:
        ax_, ay_ = jx(a["x"]), jy(a["y"])
        rr = notch_len / 2.0
        for j in range(max(0, int((ay_ - rr) / res) - 1), min(ny, int((ay_ + rr) / res) + 2)):
            for i in range(max(0, int((ax_ - rr) / res) - 1), min(nx, int((ax_ + rr) / res) + 2)):
                if H[j][i] == R and math.hypot((i + 0.5) * res - ax_, (j + 0.5) * res - ay_) <= rr:
                    H[j][i] = S
    # finger notches through the rim (left and right of the board)
    if jc.get("finger_notches", True):
        nd = int(round(max(floor, support - 4.0) * Q))
        ymid = jy(bb.cy)
        for xa, xb in ((0.0, jx(bb.x0) + 2.0), (jx(bb.x1) - 2.0, W_)):
            for j in range(ny):
                if abs((j + 0.5) * res - ymid) > 7.5:
                    continue
                for i in range(nx):
                    if xa <= (i + 0.5) * res <= xb and H[j][i] >= S:
                        H[j][i] = nd
    # alignment posts through the DUT's mounting holes
    posts = []
    if jc.get("posts", True):
        for hx, hy, hr, ref in mounting_holes(design):
            r = hr - float(jc["post_clearance_mm"])
            px, py = jx(hx), jy(hy)
            if any(pk.contains(px, py) for pk in pockets):
                continue
            posts.append((px, py, r, ref))
        P_ = int(round((support + design.board.thickness + 1.0) * Q))
        for px, py, r, _ in posts:
            for j in range(max(0, int((py - r) / res) - 1), min(ny, int((py + r) / res) + 2)):
                for i in range(max(0, int((px - r) / res) - 1), min(nx, int((px + r) / res) + 2)):
                    if math.hypot((i + 0.5) * res - px, (j + 0.5) * res - py) <= r:
                        H[j][i] = P_
    # pockets (deepest wins)
    for pk in sorted(pockets, key=lambda p: p.depth):
        d = int(round((support - pk.depth) * Q))
        bbp = pk.bbox()
        for j in range(max(0, int(bbp.y0 / res) - 1), min(ny, int(bbp.y1 / res) + 2)):
            yc = (j + 0.5) * res
            for i in range(max(0, int(bbp.x0 / res) - 1), min(nx, int(bbp.x1 / res) + 2)):
                if pk.contains((i + 0.5) * res, yc):
                    H[j][i] = min(H[j][i], d)

    top = support + max(rim_h, (design.board.thickness + 1.0) if posts else 0.0)
    if grid_out is not None:                     # heightmap for checks (jig X = board x - ox, Y = board y - oy)
        grid_out.update({"H": H, "nx": nx, "ny": ny, "res": res, "ox": ox, "oy": oy, "support": support})
    result: Dict[str, Any] = {
        "jig_size_mm": [round(nx * res, 2), round(ny * res, 2), round(top, 2)],
        "support_height_mm": round(support, 2), "floor_mm": floor, "resolution_mm": res,
        "board_bbox_in_jig_mm": [round(jx(bb.x0), 3), round(jy(bb.y0), 3), round(jx(bb.x1), 3), round(jy(bb.y1), 3)],
        "orientation": "PCB top side faces down into the jig; jig X = board X, jig Y = board Y (view from above "
                       "shows the board's bottom side)",
        "parts_to_place": [fp.ref for fp in selected],
        "pockets": [p.to_dict() for p in sorted(pockets, key=lambda p: (p.kind != "hold", C.natural_key(p.ref)))],
        "posts": [{"hole": ref, "center": [round(px, 2), round(py, 2)], "diameter_mm": round(2 * r, 2)} for px, py, r, ref in posts],
        "notes": notes + ["Place parts in the 'hold' pockets, lower the PCB top-side down onto the nest, solder from above.",
                          "Hold pocket depth = part height + %.2f mm; adjust jig.heights.<REF> if a part sits proud." % float(jc["hold_depth_offset_mm"])],
    }
    if png_path:
        write_heightmap_png(png_path, H, nx, ny)
        result["preview_png"] = png_path
    if stl_path:
        tris = heightmap_mesh(H, nx, ny, res)
        write_stl(stl_path, tris, "PCB TestKit solder jig %s" % b.name)
        result["mesh"] = mesh_check(tris) if len(tris) < 600000 else {"triangles": len(tris)}
        result["stl"] = stl_path
    if scad_path:
        with open(scad_path, "w", encoding="utf-8") as fh:
            fh.write(scad_source(design, pockets, posts, segs, nx * res, ny * res, support, rim_h, edge_clr, floor))
        result["scad"] = scad_path
    return result


def build_placement_jig(design: Design, stops: List[Dict[str, float]], stl_path: Optional[str] = None,
                        png_path: Optional[str] = None, grid_out: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The placement jig of the two-jig workflow: the PCB rests in it top side up while the parts are fitted.

    Print orientation = use orientation (Z up, PCB top up). Jig X = board x, jig Y = -board y (the view from
    above matches KiCad's editor). Heights:
      floor .. pcb_rest   solid block; the PCB's underside rests on its top surface
      wells               under every through-hole, down to the floor, for the leads (trim them to fit)
      pockets             for parts on the PCB's bottom side
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
    border = float(jc.get("placement_border_mm", 6.0))
    stop_len = float(jc.get("stop_length_mm", 8.0))
    stop_w = float(jc.get("stop_width_mm", 2.5))
    stop_clr = float(jc.get("stop_clearance_mm", 0.15))
    bot_clr = float(jc.get("bottom_part_clearance_mm", 0.8))
    notes: List[str] = []

    # parts on the bottom side need pockets; a tall one deepens the whole jig
    bottom = []
    for fp in design.components():
        if fp.side != "bottom" or C.is_mounting_hole(fp) or not fp.pads:
            continue
        h, _src = design.height_of(fp)
        c = fp.courtyard_local()
        if not c.valid:
            c = fp.pads_local_bbox()
        g = BBox()
        for lx, ly in ((c.x0, c.y0), (c.x1, c.y0), (c.x1, c.y1), (c.x0, c.y1)):
            g.add(*fp.to_board(lx, ly))
        bottom.append((fp.ref, g.grow(bot_clr), h + bot_clr))
    deepest = max([d for _, _, d in bottom] + [0.0])
    if deepest + 0.5 > clearance:
        notes.append("lead clearance raised from %.1f to %.1f mm for the bottom-side parts" % (clearance, deepest + 0.5))
        clearance = deepest + 0.5
    rest = floor + clearance                                   # PCB underside
    ax0, ay1 = bb.x0 - border, bb.y1 + border

    def jx(x):
        return x - ax0

    def jy(y):
        return ay1 - y

    W_, L_ = bb.w + 2 * border, bb.h + 2 * border
    if (W_ / res) * (L_ / res) > 6e6:
        res = math.sqrt(W_ * L_ / 6e6)
    nx, ny = int(math.ceil(W_ / res)), int(math.ceil(L_ / res))
    REST, FLOOR = int(round(rest * Q)), int(round(floor * Q))
    H = [[REST] * nx for _ in range(ny)]

    def board_xy(i, j):
        return ax0 + (i + 0.5) * res, ay1 - (j + 0.5) * res

    def disk(bx_, by_, r, z):
        cx_, cy_ = jx(bx_), jy(by_)
        for j in range(max(0, int((cy_ - r) / res) - 1), min(ny, int((cy_ + r) / res) + 2)):
            for i in range(max(0, int((cx_ - r) / res) - 1), min(nx, int((cx_ + r) / res) + 2)):
                if math.hypot((i + 0.5) * res - cx_, (j + 0.5) * res - cy_) <= r:
                    H[j][i] = min(H[j][i], z)

    # lead wells under every component through-hole (any side), down to the floor
    wells = 0
    holes = {(round(x, 3), round(y, 3)) for x, y, _, _ in mounting_holes(design)}
    for fp in design.components():
        for pd in fp.pads:
            if pd.drill_w <= 0 or (round(pd.x, 3), round(pd.y, 3)) in holes:
                continue
            r = max(pd.drill_w / 2.0 + 0.6, min(max(pd.w, pd.h) / 2.0, pd.drill_w / 2.0 + 1.5)) + margin
            disk(pd.x, pd.y, r, FLOOR)
            wells += 1
    # holes for the holding jig's posts
    post_depth = int(round(max(floor, rest - t - 2.0) * Q))
    for x, y, r, _ in mounting_holes(design):
        disk(x, y, r + 0.6, min(post_depth, REST - int(2.0 * Q)))
    # bottom-side part pockets
    for ref, g, d in bottom:
        z = int(round(max(floor, rest - d) * Q))
        for j in range(max(0, int(jy(g.y1) / res) - 1), min(ny, int(jy(g.y0) / res) + 2)):
            for i in range(max(0, int(jx(g.x0) / res) - 1), min(nx, int(jx(g.x1) / res) + 2)):
                bx_, by_ = board_xy(i, j)
                if g.x0 <= bx_ <= g.x1 and g.y0 <= by_ <= g.y1:
                    H[j][i] = min(H[j][i], z)
    # locating stops: just outside the outline, ending 0.2 mm below the PCB's top surface
    segs = outline_segments(design)
    STOP = int(round((rest + max(0.4, t - 0.2)) * Q))
    for a in stops:
        reach = stop_len / 2.0 + stop_clr + stop_w
        cx_, cy_ = jx(a["x"]), jy(a["y"])
        for j in range(max(0, int((cy_ - reach) / res) - 1), min(ny, int((cy_ + reach) / res) + 2)):
            for i in range(max(0, int((cx_ - reach) / res) - 1), min(nx, int((cx_ + reach) / res) + 2)):
                bx_, by_ = board_xy(i, j)
                if not _in_stop(bx_, by_, a, stop_len) or point_inside(bx_, by_, segs):
                    continue
                d = min(pt_seg_dist(bx_, by_, *s) for s in segs)
                if stop_clr <= d <= stop_clr + stop_w:
                    H[j][i] = STOP
    # finger notches left and right, to lift the PCB out by its edges
    if jc.get("finger_notches", True):
        nd = int(round(max(floor, rest - 4.0) * Q))
        ymid = jy(bb.cy)
        for xa, xb in ((0.0, jx(bb.x0) + 2.0), (jx(bb.x1) - 2.0, W_)):
            for j in range(ny):
                if abs((j + 0.5) * res - ymid) > 7.5:
                    continue
                for i in range(nx):
                    if xa <= (i + 0.5) * res <= xb and H[j][i] != STOP:     # never cut away a locating stop
                        H[j][i] = min(H[j][i], nd)
    if len(stops) < 3:
        notes.append("only %d locating stop(s) fit around this outline; place the PCB carefully" % len(stops))
    if grid_out is not None:                     # heightmap for checks (jig X = board x - ax0, Y = ay1 - board y)
        grid_out.update({"H": H, "nx": nx, "ny": ny, "res": res, "ax0": ax0, "ay1": ay1, "rest": rest})
    result: Dict[str, Any] = {
        "jig_size_mm": [round(nx * res, 2), round(ny * res, 2), round(rest + max(0.4, t - 0.2), 2)],
        "pcb_rest_height_mm": round(rest, 2), "lead_clearance_mm": round(clearance, 2), "floor_mm": floor,
        "resolution_mm": res,
        "board_bbox_in_jig_mm": [round(jx(bb.x0), 3), round(jy(bb.y1), 3), round(jx(bb.x1), 3), round(jy(bb.y0), 3)],
        "orientation": "PCB top side up; jig X = board X, jig Y = -board Y (the view from above matches KiCad)",
        "stops": [{"side": a["side"], "at": [round(a["x"], 2), round(a["y"], 2)]} for a in stops],
        "lead_wells": wells, "bottom_pockets": [r for r, _, _ in bottom],
        "notes": notes + ["Trim the leads to at most %.0f mm below the board, or raise jig.placement_clearance_mm."
                          % max(1.0, clearance - 0.5)],
    }
    if png_path:
        write_heightmap_png(png_path, H, nx, ny)
        result["preview_png"] = png_path
    if stl_path:
        tris = heightmap_mesh(H, nx, ny, res)
        write_stl(stl_path, tris, "PCB TestKit placement jig %s" % b.name)
        result["mesh"] = mesh_check(tris) if len(tris) < 600000 else {"triangles": len(tris)}
        result["stl"] = stl_path
    return result


def build_jigs(design: Design, stl_path: Optional[str] = None, scad_path: Optional[str] = None,
               png_path: Optional[str] = None, place_stl: Optional[str] = None,
               place_png: Optional[str] = None, grids: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The holding jig and, in the two-jig workflow (default), the placement jig that pairs with it."""
    jc = design.cfg["jig"]
    two = str(jc.get("workflow", "two_jig")).lower().replace("-", "_") != "single"
    stops = stop_anchors(design, outline_segments(design), float(jc.get("stop_length_mm", 8.0))) if two else []
    grids = grids if grids is not None else {}
    jig = build_jig(design, stl_path, scad_path, png_path, stops=stops, grid_out=grids.setdefault("holding", {}))
    jig["workflow"] = "two_jig" if two else "single"
    if two:
        jig["placement"] = build_placement_jig(design, stops, place_stl, place_png, grid_out=grids.setdefault("placement", {}))
        jig["notes"] = [n for n in jig["notes"] if not n.startswith("Place parts in the 'hold' pockets")] + [
            "Two-jig workflow: put the PCB top side up in the placement jig, fit the parts, put this holding jig "
            "on top (pockets down), turn the stack over, lift off the placement jig and solder from above."]
    return jig


def stack_collisions(design: Design, grids: Dict[str, Dict[str, Any]], gap: float = 0.1) -> List[Tuple[float, float, str]]:
    """Where the holding jig, turned over onto the PCB in the placement jig, would touch the placement jig.

    Both heightmaps are compared in board coordinates, in the placement jig's frame: the PCB lies on
    pcb_rest .. pcb_rest + thickness; the holding jig's lowest point above board point p is
    pcb_rest + thickness + (support - H_hold(p)). Returns (board x, board y, what) for every conflict.
    """
    A, B = grids["placement"], grids["holding"]
    t = float(design.board.thickness)
    rest, support = A["rest"], B["support"]
    segs = outline_segments(design)
    out = []
    for j in range(A["ny"]):
        for i in range(A["nx"]):
            bx_ = A["ax0"] + (i + 0.5) * A["res"]
            by_ = A["ay1"] - (j + 0.5) * A["res"]
            ib, jb = int((bx_ - B["ox"]) / B["res"]), int((by_ - B["oy"]) / B["res"])
            if not (0 <= ib < B["nx"] and 0 <= jb < B["ny"]):
                continue
            za = A["H"][j][i] / float(Q)
            zb_low = rest + t + (support - B["H"][jb][ib] / float(Q))
            on_board = point_inside(bx_, by_, segs)
            if on_board and zb_low < rest + t - 1e-6 and za > zb_low - gap:
                out.append((bx_, by_, "post"))          # a post through a hole must find a hole below
            elif not on_board and za > zb_low - gap:
                out.append((bx_, by_, "rim"))
            elif on_board and za > rest + 1e-6:
                out.append((bx_, by_, "under PCB"))
    return out


def scad_source(design, pockets, posts, segs, W_, L_, support, rim_h, edge_clr, floor) -> str:
    loops = chain_segments(segs)
    pts, paths = [], []
    for lp in loops:
        start = len(pts)
        pts += [(round(x, 3), round(y, 3)) for x, y in lp]
        paths.append(list(range(start, len(pts))))
    out = ["// Solder jig for %s - generated by PCB TestKit" % design.board.name,
           "// Parts go upside-down into the pockets; the PCB goes top-side down into the nest.",
           "$fn = 64;", "W = %.3f; L = %.3f; support = %.3f; rim_h = %.3f; edge_clearance = %.3f; floor_t = %.3f;" %
           (W_, L_, support, rim_h, edge_clr, floor), "",
           "module board_outline() {", "  polygon(points=%s, paths=%s);" % (pts, paths), "}", "",
           "difference() {", "  union() {", "    cube([W, L, support]);",
           "    translate([0, 0, support]) linear_extrude(rim_h) difference() {",
           "      square([W, L]);", "      offset(delta = edge_clearance) board_outline();", "    }"]
    for px, py, r, ref in posts:
        out.append("    translate([%.3f, %.3f, support]) cylinder(r = %.3f, h = %.3f); // post through %s" %
                   (px, py, r, design.board.thickness + 1.0, ref))
    out.append("  }")
    for p in pockets:
        z = "support - %.3f" % p.depth
        if p.shape == "circle":
            out.append("  translate([%.3f, %.3f, %s]) cylinder(r = %.3f, h = %.3f); // %s %s" %
                       (p.cx, p.cy, z, p.r, p.depth + rim_h + 5, p.kind, p.ref))
        else:
            out.append("  translate([%.3f, %.3f, %s]) rotate([0, 0, %.3f]) translate([%.3f, %.3f, 0]) cube([%.3f, %.3f, %.3f]); // %s %s" %
                       (p.cx, p.cy, z, -p.angle, -p.w / 2, -p.h / 2, p.w, p.h, p.depth + rim_h + 5, p.kind, p.ref))
    out.append("}")
    return "\n".join(out) + "\n"
