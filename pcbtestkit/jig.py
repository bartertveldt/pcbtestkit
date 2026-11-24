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
def build_jig(design: Design, stl_path: Optional[str] = None, scad_path: Optional[str] = None,
              png_path: Optional[str] = None) -> Dict[str, Any]:
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
        for fp in design.components():
            for pd in fp.pads:
                if pd.drill_w >= 2.0 and (pd.is_npth or C.is_mounting_hole(fp)):
                    r = pd.drill_w / 2 - float(jc["post_clearance_mm"])
                    px, py = jx(pd.x), jy(pd.y)
                    if any(pk.contains(px, py) for pk in pockets):
                        continue
                    posts.append((px, py, r, fp.ref))
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
