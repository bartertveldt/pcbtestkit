"""The fit check of the solder jigs against every part's real 3D shape (:mod:`pcbtestkit.parts3d`).

It is written apart from the jig builder, so the two check each other. The builder clips every triangle to every
cell; this check takes, for each triangle, the only points where its height can peak or bottom out inside a cell
(its corners, where its edges cross the grid lines, and the grid corners inside it: the corners of the polygon a
cell cuts out of it) and compares each with the jig surface of every cell it lies on. That is exact. For each jig
of a hand-solder pass:

  holding jig   no part of the board reaches into it; every part of the pass touches its pocket floor at the
                same moment the board's face lands on the support (the gap between them is the height step,
                0.01 mm); the board clears the rim and the posts.
  placement jig no part, lead or overhanging connector reaches into it, and the board clears its stops.
  paste nest    the same for the parts already on the board's down side.

The jigs and parts only move straight up and down against each other, and every jig is a height map (one surface
height per cell), so "nothing touches at the end" is the same as "nothing touches on the way there": a part that
clears a jig in place cleared it all the way down.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from . import parts3d as P3

TOUCH = 0.015          # a part touches when its gap to the pocket floor is at most this (height step 0.01 mm)
TOL = 1e-3


def _cells_of(u: float, n: int) -> range:
    """Grid indices whose closed interval [i, i + 1] holds coordinate u (two on a grid line)."""
    return range(max(0, int(math.ceil(u - 1.0 - 1e-9))), min(n - 1, int(math.floor(u + 1e-9))) + 1)


def critical_points(tri, x0: float, y0: float, res: float, flip_y: bool = False, ay1: float = 0.0):
    """(u, v, z) in grid units: the triangle's corners, its edges' crossings with the grid lines and the grid
    corners inside it. Over any cell the triangle is highest and lowest at some of these points.
    ``flip_y``: grid v = (ay1 - y) / res (a "y_up" jig), else (y - y0) / res."""
    if flip_y:
        P = [((tri[k] - x0) / res, (ay1 - tri[k + 1]) / res, tri[k + 2]) for k in (0, 3, 6)]
    else:
        P = [((tri[k] - x0) / res, (tri[k + 1] - y0) / res, tri[k + 2]) for k in (0, 3, 6)]
    out = list(P)
    for e in range(3):
        p, q = P[e], P[(e + 1) % 3]
        for ax in (0, 1):
            a0, a1 = p[ax], q[ax]
            if abs(a1 - a0) < 1e-12:
                continue
            for k in range(int(math.ceil(min(a0, a1))), int(math.floor(max(a0, a1))) + 1):
                f = (k - a0) / (a1 - a0)
                pt = [p[0] + (q[0] - p[0]) * f, p[1] + (q[1] - p[1]) * f, p[2] + (q[2] - p[2]) * f]
                pt[ax] = float(k)
                out.append(tuple(pt))
    (ua, va, za), (ub, vb, zb), (uc, vc, zc) = P
    det = (vb - vc) * (ua - uc) + (uc - ub) * (va - vc)
    if abs(det) > 1e-12:
        for k in range(int(math.ceil(min(ua, ub, uc))), int(math.floor(max(ua, ub, uc))) + 1):
            for m in range(int(math.ceil(min(va, vb, vc))), int(math.floor(max(va, vb, vc))) + 1):
                l1 = ((vb - vc) * (k - uc) + (uc - ub) * (m - vc)) / det
                l2 = ((vc - va) * (k - uc) + (ua - uc) * (m - vc)) / det
                l3 = 1.0 - l1 - l2
                if l1 >= -1e-9 and l2 >= -1e-9 and l3 >= -1e-9:
                    out.append((float(k), float(m), l1 * za + l2 * zb + l3 * zc))
    return out


def _worst(tris, x0, y0, res, nx, ny, surf, sign: int, flip_y: bool = False, ay1: float = 0.0) -> float:
    """The smallest sign * (surface - z) over every triangle point on the grid: < 0 means the part reaches into
    the jig. ``surf(i, j)`` is the jig's surface height over cell (i, j)."""
    worst = math.inf
    for tri in tris:
        for u, v, z in critical_points(tri, x0, y0, res, flip_y, ay1):
            if u < -1.0 or v < -1.0 or u > nx + 1.0 or v > ny + 1.0:
                continue
            for j in _cells_of(v, ny):
                for i in _cells_of(u, nx):
                    d = sign * (surf(i, j) - z)
                    if d < worst:
                        worst = d
    return worst


def check_holding(view: Any, g: Dict[str, Any], meshes: Dict[str, P3.PartMesh], present: List[str],
                  hold: List[str], Q: int) -> Dict[str, Any]:
    """Parts against a holding jig (grid ``g`` of one board: H, nx, ny, res, ox, oy, support). ``meshes``: the
    parts as the (possibly turned-over) view sees them (:func:`pcbtestkit.jig.view_meshes`)."""
    t = float(view.board.thickness)
    H, nx, ny, res, ox, oy = g["H"], g["nx"], g["ny"], g["res"], g["ox"], g["oy"]
    base = t + g["support"]

    def surf(i, j):                     # the jig's lowest point over the cell, board z (the jig is turned over)
        return base - H[j][i] / float(Q)
    hits: Dict[str, float] = {}
    gaps: Dict[str, float] = {}
    for ref in present:
        pm = meshes.get(ref)
        if pm is None:
            continue
        vm = pm
        tris = [tr for tr in vm.tris if max(tr[2], tr[5], tr[8]) > t - 2.5]   # lower is nowhere near this jig
        worst = _worst(tris, ox, oy, res, nx, ny, surf, 1)
        if worst < -TOL:
            hits[ref] = round(-worst, 3)
        if ref in hold and worst < math.inf:
            gaps[ref] = round(worst, 3)
    loose = {r: v for r, v in gaps.items() if v > TOUCH}
    return {"collisions": hits, "gaps": gaps, "loose": loose,
            "touching": sorted(r for r, v in gaps.items() if -TOL <= v <= TOUCH)}


def occupied(view: Any, g: Dict[str, Any], meshes: Dict[str, P3.PartMesh], refs: List[str]) -> Dict[int, set]:
    """{holding-jig cell: refs of the parts standing on the board there}: a part next to another can't slide into
    it (both stand on the board's face)."""
    t = float(view.board.thickness)
    out: Dict[int, set] = {}
    for r in refs:
        pm = meshes.get(r)
        if pm is None:
            continue
        tris = [tr for tr in pm.tris if max(tr[2], tr[5], tr[8]) > t + 0.05]
        for key, (_lo, hi) in P3.envelope(tris, g["ox"], g["oy"], g["res"], g["nx"], g["ny"]).items():
            if hi > t + 0.05:
                out.setdefault(key, set()).add(r)
    return out


def side_play(view: Any, g: Dict[str, Any], pm: P3.PartMesh, Q: int, low: float, high: float, steps: int = 3,
              occ: Optional[Dict[int, set]] = None, far: float = 0.0) -> Tuple[Dict[str, float], Dict[str, str]]:
    """How far a part seated in its holding-jig pocket can slide in +x, -x, +y and -y (board directions) before it
    touches the jig or a neighbouring part (``occ``, :func:`occupied`), in mm, and what stops it each way ("jig",
    a neighbour's reference, or "" when nothing does within the search). The pocket was cut ``low`` mm clear of the
    part, so the answer lies above that; it is found by halving between ``low`` and ``high`` (to (high - low) /
    2**steps). A part that slides ``high`` is tried up to ``far`` against its neighbours (parts closer together than
    a jig wall needs hold each other). ``pm``: the part's mesh in the view."""
    t = float(view.board.thickness)
    H, nx, ny, res, ox, oy = g["H"], g["nx"], g["ny"], g["res"], g["ox"], g["oy"]
    base = t + g["support"]
    tris = [tr for tr in pm.tris if max(tr[2], tr[5], tr[8]) > t + 0.01]
    own = {k for k, (_lo, hi) in P3.envelope(tris, ox, oy, res, nx, ny).items() if hi > t + 0.05} if occ else set()
    # a neighbour's cells, but not the ones this part already stands on (parts closer than a cell share some)
    others = {k: rs - {pm.ref} for k, rs in (occ or {}).items() if rs - {pm.ref} and k not in own}
    reach = max(high, far)

    def reachable(ux: int, uy: int):
        """The triangles that could meet the jig or a neighbour while sliding up to ``reach`` this way."""
        keep = []
        for a in tris:
            x0, x1 = min(a[0], a[3], a[6]), max(a[0], a[3], a[6])
            y0, y1 = min(a[1], a[4], a[7]), max(a[1], a[4], a[7])
            x0, x1 = (x0, x1 + reach) if ux > 0 else (x0 - reach, x1) if ux < 0 else (x0, x1)
            y0, y1 = (y0, y1 + reach) if uy > 0 else (y0 - reach, y1) if uy < 0 else (y0, y1)
            top = max(a[2], a[5], a[8]) - TOL
            i0, i1 = max(0, int(math.floor((x0 - ox) / res))), min(nx - 1, int(math.floor((x1 - ox) / res)))
            j0, j1 = max(0, int(math.floor((y0 - oy) / res))), min(ny - 1, int(math.floor((y1 - oy) / res)))
            if any(base - H[j][i] / float(Q) < top or (j * nx + i in others and top > t + 0.05)
                   for j in range(j0, j1 + 1) for i in range(i0, i1 + 1)):
                keep.append(a)
        return keep

    def hits(part, dx: float, dy: float) -> str:
        moved = [(a[0] + dx, a[1] + dy, a[2], a[3] + dx, a[4] + dy, a[5], a[6] + dx, a[7] + dy, a[8]) for a in part]
        env = P3.envelope(moved, ox, oy, res, nx, ny)
        nb = ""
        for key, (_lo, hi) in env.items():
            j, i = divmod(key, nx)
            if hi > base - H[j][i] / float(Q) + TOL:
                return "jig"
            if not nb and key in others and hi > t + 0.05:
                nb = sorted(others[key], key=C.natural_key)[0]
        return nb

    from . import classify as C
    out: Dict[str, float] = {}
    by: Dict[str, str] = {}
    for name, (ux, uy) in (("+x", (1, 0)), ("-x", (-1, 0)), ("+y", (0, 1)), ("-y", (0, -1))):
        part = reachable(ux, uy)
        what = hits(part, ux * high, uy * high) if part else ""
        lo, hi = low, high
        if not what:
            nb = hits(part, ux * far, uy * far) if part and far > high and others else ""
            if not nb or nb == "jig":
                out[name], by[name] = (far if nb else high), nb
                if not nb:
                    continue
            lo, hi, what = high, far, nb
        for _ in range(steps):
            mid = (lo + hi) / 2.0
            w = hits(part, ux * mid, uy * mid)
            if w:
                hi, what = mid, w
            else:
                lo = mid
        out[name], by[name] = round(hi, 3), what
    return out, by


def check_placement(view: Any, g: Dict[str, Any], meshes: Dict[str, P3.PartMesh], present: List[str],
                    Q: int) -> Dict[str, Any]:
    """Parts against a placement jig or paste nest (grid ``g``: H, nx, ny, res, ax0, ay1, rest). ``meshes``: the
    parts as the view sees them, as for :func:`check_holding`."""
    t = float(view.board.thickness)
    H, nx, ny, res, ax0, ay1, rest = g["H"], g["nx"], g["ny"], g["res"], g["ax0"], g["ay1"], g["rest"]
    top = max(max(row) for row in H) / float(Q) - rest       # board z of the jig's highest point

    def surf(i, j):                     # the jig's top over the cell, board z
        return H[j][i] / float(Q) - rest
    hits: Dict[str, float] = {}
    for ref in present:
        pm = meshes.get(ref)
        if pm is None:
            continue
        vm = pm
        tris = [tr for tr in vm.tris if min(tr[2], tr[5], tr[8]) < top + 0.01]
        worst = _worst(tris, ax0, 0.0, res, nx, ny, surf, -1, True, ay1)
        if worst < -TOL:
            hits[ref] = round(-worst, 3)
    return {"collisions": hits, "top": round(top + t * 0, 3)}


def check_board(view: Any, hold_g: Optional[Dict[str, Any]], place_g: Optional[Dict[str, Any]], Q: int) -> Dict[str, Any]:
    """The bare board against the jigs: it lies flat on the holding jig's support (nothing of the jig higher than
    the support reaches over the board, except posts inside its holes) and on the placement jig's rest (nothing
    higher than the rest reaches under it). Returns how many cells would touch it."""
    from .geometry import pt_seg_dist
    from .jig import mounting_holes, outline_segments, point_inside
    segs = [s_[:4] for s_ in outline_segments(view)]
    holes = mounting_holes(view)
    bb = view.board.outline_bbox()
    out: Dict[str, Any] = {"holding": 0, "placement": 0, "support_cells": 0}

    def overlaps(x0, y0, x1, y1) -> bool:
        """Does the closed square reach the board (outside its mounting holes)?"""
        if x1 < bb.x0 or x0 > bb.x1 or y1 < bb.y0 or y0 > bb.y1:
            return False
        cx, cy, half = (x0 + x1) / 2.0, (y0 + y1) / 2.0, (x1 - x0) / 2.0
        for hx, hy, hr, _ in holes:
            if all(math.hypot(px - hx, py - hy) <= hr + 1e-9 for px, py in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))):
                return False
        if point_inside(cx, cy, segs):
            return True
        return min(pt_seg_dist(cx, cy, *sg) for sg in segs) <= half * math.sqrt(2.0) + 1e-9

    g = hold_g
    if g:
        S = int(round(g["support"] * Q))
        r = g["res"]
        for j in range(g["ny"]):
            row = g["H"][j]
            for i in range(g["nx"]):
                h = row[i]
                if h == S:
                    out["support_cells"] += 1
                elif h > S:
                    x0, y0 = g["ox"] + i * r, g["oy"] + j * r
                    if overlaps(x0, y0, x0 + r, y0 + r):
                        out["holding"] += 1
    if place_g:
        g = place_g
        R = int(round(g["rest"] * Q))
        r = g["res"]
        for j in range(g["ny"]):
            row = g["H"][j]
            for i in range(g["nx"]):
                if row[i] > R:
                    x0, y1 = g["ax0"] + i * r, g["ay1"] - j * r
                    if overlaps(x0, y1 - r, x0 + r, y1):
                        out["placement"] += 1
    out["thickness"] = float(view.board.thickness)
    return out
