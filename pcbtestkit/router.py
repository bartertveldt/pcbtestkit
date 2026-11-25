"""Built-in autorouter for the generated fixture boards (two copper layers).

A grid maze router: A* over both copper layers with 45-degree moves, vias, a turn penalty and a preferred
direction per layer (F.Cu horizontal, B.Cu vertical). Multi-pin nets grow as a tree, nearest pin first.
Nets are routed shortest first; when some fail, the next pass routes the failed nets first, and the best
pass is kept. Standard library only and deterministic: the same board and settings give the same tracks on
every run (no time limits, no randomness; search effort is bounded by counts).

Staying DRC-clean
-----------------
Every obstacle (copper of other nets, copper without a net, holes, the board edge and cut-outs) is stamped
into an "owner" grid per track width and per layer: a cell is free for a track centre when it lies at least
clearance + half the track width + margin from the obstacle. Obstacles are convex, so when both ends of a
straight move between neighbouring cell centres keep R + m from one, the whole move keeps R as long as
m >= sqrt(R^2 + (L/2)^2) - R (L = the diagonal step); that margin is about 0.05 mm on a 0.25 mm grid.
Vias sit exactly on a cell centre and are checked without that margin. After each pass every track and via
is checked again with exact geometry; a net that fails the check is taken out and reported as unrouted, so
the router never writes a clearance violation.
"""
from __future__ import annotations

import heapq
import math
from array import array
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .geometry import pt_seg_dist, rect_corners, rot, seg_seg_dist

FREE, MULTI, BLOCK = 0, 0xFFFE, 0xFFFF
LAYERS = ("F.Cu", "B.Cu")

# Defaults match KiCad's own default rules, so a board with no rules of its own passes DRC.
DEFAULTS: Dict[str, Any] = {
    "track_mm": 0.25,            # signal tracks
    "power_track_mm": 0.5,       # nets named in power_nets (GND, VIN); falls back to track_mm where it does not fit
    "clearance_mm": 0.2,         # copper to copper
    "via_mm": 0.6,
    "via_drill_mm": 0.3,
    "edge_clearance_mm": 0.5,    # copper to board edge and cut-outs
    "hole_clearance_mm": 0.25,   # copper to a hole of another net (and to unplated holes)
    "hole_to_hole_mm": 0.25,     # drill to drill (vias)
    "grid_mm": 0.254,            # 10 mil: with ``origin`` on a 2.54 mm header pin, every pin and midline is on the grid
    "origin": None,              # (x, y) the grid is aligned to
    "weight": 1.25,              # heuristic weight of the A* search (1 = shortest paths, more = faster)
    "via_cost": 8.0,             # a via costs as much as this many grid steps
    "max_passes": 4,
    "max_cells": 1600000,        # per layer; the grid is made coarser for very large boards
    "max_expansions": 250000,    # per search (the last resort per connection has no cap)
}

_SLACK = 0.005       # extra clearance on every rule, so rounding never lands exactly on a limit
_PIECE = 2.0         # long shapes are stamped in pieces of this length (keeps the stamped boxes small)
_DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]   # around the circle
_TURN = [0, 4, 8, 12, 16, 12, 8, 4]   # cost of a turn by (d - d0) & 7 steps of 45 degrees


# --------------------------------------------------------------------------- #
# Shapes: ("circle", x, y, r) | ("capsule", x1, y1, x2, y2, r) | ("rrect", x, y, half_w, half_h, angle, r)
# A capsule is a segment with round ends (tracks, oval pads); an rrect is a rectangle core grown by r.
# --------------------------------------------------------------------------- #
def pad_shape(p) -> tuple:
    w, h = float(p.w), float(p.h)
    if p.shape == "circle" or (p.shape == "oval" and abs(w - h) < 1e-9):
        return ("circle", p.x, p.y, max(w, h) / 2)
    if p.shape == "oval":
        r = min(w, h) / 2
        half = (max(w, h) - min(w, h)) / 2
        ex, ey = (half, 0.0) if w > h else (0.0, half)
        dx, dy = rot(ex, ey, p.angle)
        return ("capsule", p.x - dx, p.y - dy, p.x + dx, p.y + dy, r)
    r = float(getattr(p, "rratio", 0.0) or 0.0) * min(w, h) if p.shape == "roundrect" else 0.0
    return ("rrect", p.x, p.y, w / 2 - r, h / 2 - r, float(p.angle), r)


def shape_bbox(s: tuple) -> Tuple[float, float, float, float]:
    if s[0] == "circle":
        return s[1] - s[3], s[2] - s[3], s[1] + s[3], s[2] + s[3]
    if s[0] == "capsule":
        r = s[5]
        return min(s[1], s[3]) - r, min(s[2], s[4]) - r, max(s[1], s[3]) + r, max(s[2], s[4]) + r
    _, x, y, hw, hh, a, r = s
    pts = rect_corners(x, y, 2 * hw, 2 * hh, a)
    return (min(p[0] for p in pts) - r, min(p[1] for p in pts) - r, max(p[0] for p in pts) + r,
            max(p[1] for p in pts) + r)


def point_dist(s: tuple, px: float, py: float) -> float:
    """Distance from a point to the shape's copper; negative inside."""
    if s[0] == "circle":
        return math.hypot(px - s[1], py - s[2]) - s[3]
    if s[0] == "capsule":
        return pt_seg_dist(px, py, s[1], s[2], s[3], s[4]) - s[5]
    _, x, y, hw, hh, a, r = s
    lx, ly = rot(px - x, py - y, -a)
    dx, dy = abs(lx) - hw, abs(ly) - hh
    if dx <= 0 and dy <= 0:
        return max(dx, dy) - r
    return math.hypot(max(dx, 0.0), max(dy, 0.0)) - r


def seg_dist(s: tuple, ax: float, ay: float, bx: float, by: float) -> float:
    """Distance from segment a-b to the shape's copper (0 or less when they touch)."""
    if s[0] == "circle":
        return pt_seg_dist(s[1], s[2], ax, ay, bx, by) - s[3]
    if s[0] == "capsule":
        return seg_seg_dist((ax, ay, bx, by), (s[1], s[2], s[3], s[4])) - s[5]
    _, x, y, hw, hh, a, r = s
    lax, lay = rot(ax - x, ay - y, -a)
    lbx, lby = rot(bx - x, by - y, -a)
    if (abs(lax) <= hw and abs(lay) <= hh) or (abs(lbx) <= hw and abs(lby) <= hh):
        return -r
    c = ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))
    seg = (lax, lay, lbx, lby)
    return min(seg_seg_dist(seg, (c[i][0], c[i][1], c[(i + 1) % 4][0], c[(i + 1) % 4][1])) for i in range(4)) - r


def shape_dist(s: tuple, t: tuple) -> float:
    """Distance between a capsule or circle ``t`` (tracks, vias) and any shape ``s``."""
    if t[0] == "circle":
        return point_dist(s, t[1], t[2]) - t[3]
    return seg_dist(s, t[1], t[2], t[3], t[4]) - t[5]


def _pieces(s: tuple) -> List[tuple]:
    """Long capsules split into short ones (stamping walks each piece's bounding box)."""
    if s[0] != "capsule":
        return [s]
    _, x1, y1, x2, y2, r = s
    n = max(1, int(math.ceil(math.hypot(x2 - x1, y2 - y1) / _PIECE)))
    if n == 1:
        return [s]
    return [("capsule", x1 + (x2 - x1) * i / n, y1 + (y2 - y1) * i / n, x1 + (x2 - x1) * (i + 1) / n,
             y1 + (y2 - y1) * (i + 1) / n, r) for i in range(n)]


class _Spatial:
    """Bucket grid of (bbox, payload) for the exact checks."""

    def __init__(self, cell: float = 2.0):
        self.cell = cell
        self.b: Dict[Tuple[int, int], List[Any]] = {}

    def _keys(self, x0, y0, x1, y1):
        c = self.cell
        for i in range(int(math.floor(x0 / c)), int(math.floor(x1 / c)) + 1):
            for j in range(int(math.floor(y0 / c)), int(math.floor(y1 / c)) + 1):
                yield i, j

    def add(self, bb, item) -> None:
        for k in self._keys(*bb):
            self.b.setdefault(k, []).append(item)

    def query(self, bb) -> List[Any]:
        seen, out = set(), []
        for k in self._keys(*bb):
            for it in self.b.get(k, ()):
                if id(it) not in seen:
                    seen.add(id(it))
                    out.append(it)
        return out


# --------------------------------------------------------------------------- #
class Router:
    def __init__(self, board, options: Optional[Dict[str, Any]] = None, power_nets: Sequence[str] = ()):
        o = dict(DEFAULTS)
        for k, v in (options or {}).items():
            if k in DEFAULTS and v is not None:
                o[k] = v
        self.o = o
        self.board = board
        bb = board.outline_bbox()
        g = float(o["grid_mm"])
        while (bb.w / g + 4) * (bb.h / g + 4) > float(o["max_cells"]):
            g = round(g * 1.25, 4)
        self.g = g
        x0, y0 = bb.x0 - g, bb.y0 - g
        org = o.get("origin")
        if org:                        # shift the grid down/left so (origin) is a grid node
            x0 = org[0] - math.ceil((org[0] - x0) / g - 1e-9) * g
            y0 = org[1] - math.ceil((org[1] - y0) / g - 1e-9) * g
        self.x0, self.y0 = x0, y0
        self.nx = int(math.ceil((bb.x1 - x0) / g)) + 2
        self.ny = int(math.ceil((bb.y1 - y0) / g)) + 2
        self.N = self.nx * self.ny
        self.clear = float(o["clearance_mm"])
        self.hw_sig = float(o["track_mm"]) / 2
        r_min = self.clear + self.hw_sig
        self.margin = math.sqrt(r_min ** 2 + (g * math.sqrt(2) / 2) ** 2) - r_min + 0.002
        self.hw_pwr = max(float(o["power_track_mm"]), float(o["track_mm"])) / 2
        self.via_r = float(o["via_mm"]) / 2
        self.via_drill = float(o["via_drill_mm"])
        self.power = set(power_nets)
        # class keys: half widths of the track classes, plus "via"
        self.track_keys = sorted({self.hw_sig, self.hw_pwr})
        self.keys: List[Any] = list(self.track_keys) + ["via"]
        # A* bookkeeping for every state (layer * N + cell), valid when GEN/CL match the current search
        S2 = 2 * self.N
        self._G = array("i", [0]) * S2
        self._CAME = array("i", [0]) * S2
        self._GEN = array("I", [0]) * S2
        self._CL = array("I", [0]) * S2
        self._DIR = bytearray(S2)
        self._gen = 0
        self._shift = S2.bit_length()
        nx = self.nx
        self._moves = []
        for d, (dx, dy) in enumerate(_DIRS):
            if dx and dy:
                cf = cb = 14
            else:
                cf = 12 if dy else 10          # F.Cu prefers horizontal, B.Cu vertical
                cb = 12 if dx else 10
            self._moves.append((d, dx, dy, dy * nx + dx, cf, cb))

        # ---- nets and pins (deterministic: board order) -------------------------------------------------
        self.net_names: List[str] = []
        self.net_id: Dict[str, int] = {}
        self.pins: Dict[str, List[Any]] = {}
        self.pads: List[Tuple[tuple, Tuple[int, ...], int, Any]] = []   # shape, layers, owner, pad
        self.drills: List[Tuple[float, float, float]] = []               # x, y, r of every hole
        self.npth: List[tuple] = []
        for fp in board.footprints:
            for p in fp.pads:
                if p.drill_w > 0:
                    self.drills.append((p.x, p.y, max(p.drill_w, p.drill_h or p.drill_w) / 2))
                if p.is_npth:
                    self.npth.append(("circle", p.x, p.y, max(p.drill_w, p.drill_h or p.drill_w) / 2))
                    if not (p.has_copper and p.w > p.drill_w + 0.01):
                        continue
                if not p.has_copper:
                    continue
                layers = tuple(l for l, name in enumerate(LAYERS) if p.on_copper("top" if l == 0 else "bottom"))
                if not layers:
                    continue
                owner = BLOCK
                if p.net and not p.is_npth:
                    if p.net not in self.net_id:
                        self.net_names.append(p.net)
                        self.net_id[p.net] = len(self.net_names)
                    owner = self.net_id[p.net]
                    self.pins.setdefault(p.net, []).append(p)
                self.pads.append((pad_shape(p), layers, owner, p))
        if len(self.net_names) >= MULTI - 1:
            raise ValueError("too many nets for the router")
        self.edges = list(board.edge_segs)

    # ------------------------------------------------------------------ #
    def cx(self, ix: int) -> float:
        return round(self.x0 + ix * self.g, 4)

    def cy(self, iy: int) -> float:
        return round(self.y0 + iy * self.g, 4)

    def _half(self, key) -> float:
        return self.via_r if key == "via" else key

    def _marg(self, key) -> float:
        return 0.0 if key == "via" else self.margin

    def _cell_range(self, bb, reach):
        g = self.g
        ix0 = max(0, int(math.floor((bb[0] - reach - self.x0) / g)))
        ix1 = min(self.nx - 1, int(math.ceil((bb[2] + reach - self.x0) / g)))
        iy0 = max(0, int(math.floor((bb[1] - reach - self.y0) / g)))
        iy1 = min(self.ny - 1, int(math.ceil((bb[3] + reach - self.y0) / g)))
        return ix0, ix1, iy0, iy1

    def stamp(self, arrs, shape, extra: float, layers, owner: int, keys=None) -> None:
        """Mark every cell where a track (or via) centre of each class would come closer than ``extra``
        (plus half its width, the margin and the slack) to ``shape`` as owned by ``owner``."""
        keys = list(keys if keys is not None else self.keys)
        radii = [(k, extra + self._half(k) + self._marg(k) + _SLACK) for k in keys]
        rmax = max(r for _, r in radii)
        g, x0, y0, nx = self.g, self.x0, self.y0, self.nx
        for piece in _pieces(shape):
            ix0, ix1, iy0, iy1 = self._cell_range(shape_bbox(piece), rmax)
            for iy in range(iy0, iy1 + 1):
                py = y0 + iy * g
                row = iy * nx
                for ix in range(ix0, ix1 + 1):
                    d = point_dist(piece, x0 + ix * g, py)
                    if d >= rmax:
                        continue
                    i = row + ix
                    for k, r in radii:
                        if d < r:
                            for l in layers:
                                a = arrs[k][l]
                                v = a[i]
                                if v == FREE:
                                    a[i] = owner
                                elif v != owner and v != BLOCK:
                                    a[i] = BLOCK if owner == BLOCK else MULTI

    def stamp_mask(self, mask: bytearray, shape, reach: float) -> None:
        g, x0, y0, nx = self.g, self.x0, self.y0, self.nx
        ix0, ix1, iy0, iy1 = self._cell_range(shape_bbox(shape), reach)
        for iy in range(iy0, iy1 + 1):
            py = y0 + iy * g
            row = iy * nx
            for ix in range(ix0, ix1 + 1):
                if point_dist(shape, x0 + ix * g, py) < reach:
                    mask[row + ix] = 1

    # ------------------------------------------------------------------ #
    def _base(self):
        """Owner grids with the board itself: outside, edges, holes and pads."""
        N, nx, g = self.N, self.nx, self.g
        inside = array("H", [BLOCK]) * N
        spans = 0
        for iy in range(self.ny):
            py = self.y0 + iy * g
            xs = sorted(ax + (py - ay) * (bx - ax) / (by - ay) for ax, ay, bx, by in self.edges if (ay > py) != (by > py))
            if len(xs) % 2:
                continue
            for k in range(0, len(xs), 2):
                a = max(0, int(math.ceil((xs[k] - self.x0) / g)))
                b = min(nx - 1, int(math.floor((xs[k + 1] - self.x0) / g)))
                if b >= a:
                    inside[iy * nx + a: iy * nx + b + 1] = array("H", [FREE]) * (b - a + 1)
                    spans += 1
        if not spans:                  # no usable outline: the whole bounding box
            inside = array("H", [FREE]) * N
        arrs = {k: [array("H", inside), array("H", inside)] for k in self.keys}
        ec = float(self.o["edge_clearance_mm"])
        for s in self.edges:
            self.stamp(arrs, ("capsule", s[0], s[1], s[2], s[3], 0.0), ec, (0, 1), BLOCK)
        hc = float(self.o["hole_clearance_mm"])
        for h in self.npth:
            self.stamp(arrs, h, hc, (0, 1), BLOCK)
        for shape, layers, owner, _p in self.pads:
            self.stamp(arrs, shape, self.clear, layers, owner)
        holes = bytearray(N)
        h2h = float(self.o["hole_to_hole_mm"]) + self.via_drill / 2 + _SLACK
        for x, y, r in self.drills:
            self.stamp_mask(holes, ("circle", x, y, r), h2h)
        return arrs, holes

    # ------------------------------------------------------------------ #
    def route(self) -> Dict[str, Any]:
        nets = [n for n in self.net_names if len(self.pins[n]) >= 2]
        need = {n: len(self.pins[n]) - 1 for n in nets}

        def span(n):
            ps = self.pins[n]
            xs, ys = [p.x for p in ps], [p.y for p in ps]
            return (max(xs) - min(xs) + max(ys) - min(ys), self.net_id[n])
        order = sorted(nets, key=span)
        base_arrs, base_holes = self._base()
        best = None
        tried = set()
        passes = 0
        for _ in range(max(1, int(self.o["max_passes"]))):
            key = tuple(order)
            if key in tried:
                break
            tried.add(key)
            passes += 1
            arrs = {k: [array("H", a) for a in v] for k, v in base_arrs.items()}
            holes = bytearray(base_holes)
            results: Dict[str, Dict[str, Any]] = {}
            for n in order:
                results[n] = self._route_net(arrs, holes, n)
            bad = self._verify(results)
            for n in bad:
                results[n] = {"tracks": [], "vias": [], "connected": 0, "rejected": True}
            failed = [n for n in order if results[n]["connected"] < need[n]]
            missing = sum(need[n] - results[n]["connected"] for n in nets)
            length = sum(math.hypot(t[3] - t[1], t[4] - t[2]) for r in results.values() for t in r["tracks"])
            score = (missing, round(length, 3))
            if best is None or score < best[0]:
                best = (score, results, passes)
            if not failed:
                break
            order = failed + [n for n in order if n not in failed]
        score, results, best_pass = best
        tracks, vias = [], []
        for n in self.net_names:
            r = results.get(n)
            if not r:
                continue
            for lay, x1, y1, x2, y2, w in r["tracks"]:
                tracks.append((LAYERS[lay], x1, y1, x2, y2, w, n))
            for x, y in r["vias"]:
                vias.append((x, y, float(self.o["via_mm"]), self.via_drill, n))
        unrouted = [n for n in nets if results[n]["connected"] < need[n]]
        return {"tracks": tracks, "vias": vias, "stats": {
            "nets": len(nets), "routed_nets": len(nets) - len(unrouted), "unrouted_nets": unrouted,
            "connections": sum(need.values()), "unrouted_connections": score[0], "length_mm": round(score[1], 1),
            "vias": len(vias), "grid_mm": self.g, "passes": passes, "best_pass": best_pass,
            "track_mm": float(self.o["track_mm"]), "power_track_mm": float(self.o["power_track_mm"])}}

    # ------------------------------------------------------------------ #
    def _pad_states(self, arrs, key, nid, pin) -> List[int]:
        shape = pad_shape(pin)
        out = []
        g, x0, y0, nx, N = self.g, self.x0, self.y0, self.nx, self.N
        ix0, ix1, iy0, iy1 = self._cell_range(shape_bbox(shape), 0.0)
        layers = [l for l in (0, 1) if pin.on_copper("top" if l == 0 else "bottom")]
        for iy in range(iy0, iy1 + 1):
            for ix in range(ix0, ix1 + 1):
                if point_dist(shape, self.cx(ix), self.cy(iy)) < -0.02:
                    i = iy * nx + ix
                    for l in layers:
                        v = arrs[key][l][i]
                        if v == FREE or v == nid:
                            out.append(l * N + i)
        return out

    def _route_net(self, arrs, holes, net) -> Dict[str, Any]:
        nid = self.net_id[net]
        pins = self.pins[net]
        keys = [self.hw_pwr, self.hw_sig] if (net in self.power and self.hw_pwr > self.hw_sig) else [self.hw_sig]
        nx, N = self.nx, self.N
        # cells inside this net's through-hole pads: change layer there without a via
        tht: Set[int] = set()
        for p in pins:
            if p.is_tht:
                shape = pad_shape(p)
                ix0, ix1, iy0, iy1 = self._cell_range(shape_bbox(shape), 0.0)
                for iy in range(iy0, iy1 + 1):
                    for ix in range(ix0, ix1 + 1):
                        if point_dist(shape, self.cx(ix), self.cy(iy)) < -0.02:
                            tht.add(iy * nx + ix)
        connected = [0]
        remaining = list(range(1, len(pins)))
        tree: Set[int] = set()            # states on this net's tracks
        tree_xy: List[Tuple[int, int]] = []
        branches: List[Tuple[List[int], float]] = []
        vias: List[int] = []
        ok = 0
        while remaining:
            # the unconnected pin nearest to what is already connected
            pts = [(p.x, p.y) for p in (pins[i] for i in connected)] + \
                  [(self.x0 + x * self.g, self.y0 + y * self.g) for x, y in tree_xy[::4]]
            best_i, best_d, near = None, None, None
            for i in remaining:
                p = pins[i]
                for q in pts:
                    d = (p.x - q[0]) ** 2 + (p.y - q[1]) ** 2
                    if best_d is None or d < best_d:
                        best_i, best_d, near = i, d, q
            remaining.remove(best_i)
            target_pin = pins[best_i]
            found = None
            ends = {}
            for key in keys:
                targets = set(self._pad_states(arrs, key, nid, target_pin))
                srcs = set(s for s in tree if self._free(arrs, key, nid, s))
                for ci in connected:
                    srcs.update(self._pad_states(arrs, key, nid, pins[ci]))
                if targets and srcs:
                    ends[key] = (srcs, targets)
            # near the target first (wide, then signal width), the whole board only when both fail there
            for pad_m in (12.0, None):
                for key in keys:
                    if key not in ends:
                        continue
                    srcs, targets = ends[key]
                    path = self._astar(arrs, holes, key, nid, srcs, targets, (target_pin.x, target_pin.y), near,
                                       pad_m, tht, capped=(pad_m is not None or key != keys[-1]))
                    if path:
                        found = (path, key)
                        break
                if found:
                    break
            if not found:
                continue
            path, key = found
            path = self._smooth(arrs, key, nid, path)
            self._commit(arrs, holes, nid, path, key, tht, vias)
            branches.append((path, key))
            for s in path:
                if s not in tree:
                    tree.add(s)
                    i = s % N
                    tree_xy.append((i % nx, i // nx))
            connected.append(best_i)
            ok += 1
        tracks = self._tracks(branches, pins, tht)
        return {"tracks": tracks, "vias": [(self.cx(i % nx), self.cy(i // nx)) for i in vias], "connected": ok}

    def _free(self, arrs, key, nid, s) -> bool:
        l, i = (1, s - self.N) if s >= self.N else (0, s)
        v = arrs[key][l][i]
        return v == FREE or v == nid

    # ------------------------------------------------------------------ #
    def _astar(self, arrs, holes, key, nid, srcs, targets, goal_xy, near, pad_mm, tht, capped=True):
        """Cheapest path (weighted A*) from any source state to any target state, or None."""
        nx, ny, N, g = self.nx, self.ny, self.N, self.g
        aF, aB = arrs[key]
        vF, vB = arrs["via"]
        gx = int(round((goal_xy[0] - self.x0) / g))
        gy = int(round((goal_xy[1] - self.y0) / g))
        if pad_mm is None:
            wx0, wx1, wy0, wy1 = 0, nx - 1, 0, ny - 1
        else:
            m = int(pad_mm / g)
            nx_, ny_ = int(round((near[0] - self.x0) / g)), int(round((near[1] - self.y0) / g))
            wx0, wx1 = max(0, min(gx, nx_) - m), min(nx - 1, max(gx, nx_) + m)
            wy0, wy1 = max(0, min(gy, ny_) - m), min(ny - 1, max(gy, ny_) + m)
            for t in targets:            # always include the whole target pad
                i = t % N
                tx, ty = i % nx, i // nx
                wx0, wx1, wy0, wy1 = min(wx0, tx), max(wx1, tx), min(wy0, ty), max(wy1, ty)
        via_cost = int(round(float(self.o["via_cost"]) * 10))
        # searches are capped, except the last resort (whole board, narrowest track), which may visit every state
        limit = int(self.o["max_expansions"]) if capped else 2 * N
        wq = max(4, int(round(float(self.o["weight"]) * 4)))      # heuristic weight in quarters
        self._gen += 1
        gen = self._gen
        G, GEN, CL, CAME, DIR = self._G, self._GEN, self._CL, self._CAME, self._DIR
        S = self._shift
        smask = (1 << S) - 1
        moves = self._moves
        turn = _TURN
        push, pop = heapq.heappush, heapq.heappop
        heap: List[int] = []
        for s in sorted(srcs):
            i = s - N if s >= N else s
            x, y = i % nx, i // nx
            if not (wx0 <= x <= wx1 and wy0 <= y <= wy1):
                continue
            GEN[s], G[s], CAME[s], DIR[s] = gen, 0, -1, 255
            dx = x - gx if x > gx else gx - x
            dy = y - gy if y > gy else gy - y
            h = 10 * dx + 4 * dy if dx > dy else 10 * dy + 4 * dx
            heap.append((((h * wq >> 2) << 16 | min(h, 65535)) << S) | s)
        heapq.heapify(heap)
        expanded = 0
        while heap:
            k = pop(heap)
            s = k & smask
            if CL[s] == gen:
                continue
            CL[s] = gen
            if s in targets:
                path = [s]
                while CAME[path[-1]] >= 0:
                    path.append(CAME[path[-1]])
                path.reverse()
                return path
            expanded += 1
            if expanded > limit:
                return None
            if s >= N:
                l, i, a = 1, s - N, aB
            else:
                l, i, a = 0, s, aF
            x, y = i % nx, i // nx
            gs = G[s]
            d0 = DIR[s]
            for d, dx, dy, delta, cf, cb in moves:
                x2 = x + dx
                if x2 < wx0 or x2 > wx1:
                    continue
                y2 = y + dy
                if y2 < wy0 or y2 > wy1:
                    continue
                v = a[i + delta]
                if v and v != nid:
                    continue
                s2 = s + delta
                if CL[s2] == gen:
                    continue
                g2 = gs + (cb if l else cf)
                if d0 != 255 and d != d0:
                    g2 += turn[(d - d0) & 7]
                if GEN[s2] != gen or g2 < G[s2]:
                    GEN[s2], G[s2], CAME[s2], DIR[s2] = gen, g2, s, d
                    ddx = x2 - gx if x2 > gx else gx - x2
                    ddy = y2 - gy if y2 > gy else gy - y2
                    h2 = 10 * ddx + 4 * ddy if ddx > ddy else 10 * ddy + 4 * ddx
                    push(heap, (((g2 + (h2 * wq >> 2)) << 16 | (h2 if h2 < 65535 else 65535)) << S) | s2)
            # change layer: free inside this net's through-hole pads, a via elsewhere
            s2 = s - N if l else s + N
            if CL[s2] == gen:
                continue
            o2 = (aF if l else aB)[i]
            if o2 and o2 != nid:
                continue
            if i in tht:
                c = 1
            else:
                vv, vw = vF[i], vB[i]
                if holes[i] or (vv and vv != nid) or (vw and vw != nid):
                    continue
                c = via_cost
            g2 = gs + c
            if GEN[s2] != gen or g2 < G[s2]:
                GEN[s2], G[s2], CAME[s2], DIR[s2] = gen, g2, s, d0
                dx = x - gx if x > gx else gx - x
                dy = y - gy if y > gy else gy - y
                h = 10 * dx + 4 * dy if dx > dy else 10 * dy + 4 * dx
                push(heap, (((g2 + (h * wq >> 2)) << 16 | min(h, 65535)) << S) | s2)
        return None

    # ------------------------------------------------------------------ #
    def _smooth(self, arrs, key, nid, path: List[int]) -> List[int]:
        """Pull each same-layer stretch of a path into as few straight legs as the grid allows: a staircase
        between two corners becomes one 45-degree leg plus one straight leg when every cell of those legs is free
        for this net (the same test the search uses, so the clearance argument still holds). Ends, layer changes
        and the cells where the path changes layer stay where they are."""
        N, nx = self.N, self.nx
        out: List[int] = []
        start = 0
        for k in range(1, len(path) + 1):
            if k < len(path) and (path[k] >= N) == (path[k - 1] >= N):
                continue
            sec = path[start:k]
            start = k
            if len(sec) < 3:
                out.extend(sec)
                continue
            l = 1 if sec[0] >= N else 0
            a = arrs[key][l]
            base = l * N
            cells = [s - base for s in sec]
            # corners: first, last and every cell where the direction changes
            corners = [cells[0]]
            prev_d = None
            for i in range(1, len(cells)):
                d = cells[i] - cells[i - 1]
                if prev_d is not None and d != prev_d:
                    corners.append(cells[i - 1])
                prev_d = d
            corners.append(cells[-1])
            if len(corners) <= 2:
                out.extend(sec)
                continue
            new_cells = [corners[0]]
            i = 0
            while i < len(corners) - 1:
                done = False
                for j in range(min(len(corners) - 1, i + 40), i + 1, -1):
                    legs = self._two_legs(a, nid, corners[i], corners[j])
                    if legs is not None:
                        new_cells.extend(legs)
                        i = j
                        done = True
                        break
                if not done:                       # keep the original stretch between these two corners
                    ia, ib = cells.index(corners[i]), cells.index(corners[i + 1])
                    new_cells.extend(cells[ia + 1: ib + 1])
                    i += 1
            out.extend(c + base for c in new_cells)
        return out

    def _two_legs(self, a, nid, ca: int, cb: int) -> Optional[List[int]]:
        """Cells after ``ca`` up to ``cb`` along a 45-degree leg and a straight leg (either order), or None."""
        nx = self.nx
        ax, ay, bx, by = ca % nx, ca // nx, cb % nx, cb // nx
        dx, dy = bx - ax, by - ay
        sx, sy = (dx > 0) - (dx < 0), (dy > 0) - (dy < 0)
        n_diag = min(abs(dx), abs(dy))
        n_str = max(abs(dx), abs(dy)) - n_diag
        diag = (sx, sy)
        straight = (sx, 0) if abs(dx) > abs(dy) else (0, sy)
        orders = [(diag, n_diag, straight, n_str), (straight, n_str, diag, n_diag)]
        if not n_diag or not n_str:
            orders = orders[:1]
        for (d1, k1), (d2, k2) in (((o[0], o[1]), (o[2], o[3])) for o in orders):
            x, y = ax, ay
            cells = []
            ok = True
            for (ddx, ddy), kk in ((d1, k1), (d2, k2)):
                for _ in range(kk):
                    x += ddx
                    y += ddy
                    i = y * nx + x
                    v = a[i]
                    if v and v != nid:
                        ok = False
                        break
                    cells.append(i)
                if not ok:
                    break
            if ok:
                return cells
        return None

    def _runs(self, path: List[int], tht: Set[int], breaks: Set[int] = frozenset()):
        """Straight runs (layer, s_start, s_end) of a path and the cells where it changes layer by via."""
        N, nx = self.N, self.nx
        runs, vias = [], []
        start = path[0]
        prev = path[0]
        d = None
        for s in path[1:]:
            lp, ls = prev >= N, s >= N
            if lp != ls:
                if start != prev:
                    runs.append((int(lp), start, prev))
                i = s % N
                if i not in tht:
                    vias.append(i)
                start, prev, d = s, s, None
                continue
            ip, is_ = prev % N, s % N
            step = (is_ % nx - ip % nx, is_ // nx - ip // nx)
            if d is None:
                d = step
            elif step != d or prev in breaks:
                runs.append((int(lp), start, prev))
                start, d = prev, step
            prev = s
        if start != prev:
            runs.append((int(prev >= N), start, prev))
        return runs, vias

    def _commit(self, arrs, holes, nid, path, key, tht, vias_out) -> None:
        """Stamp a found path as an obstacle for the other nets."""
        N, nx = self.N, self.nx
        runs, vias = self._runs(path, tht)
        for lay, a, b in runs:
            ia, ib = a % N, b % N
            shape = ("capsule", self.cx(ia % nx), self.cy(ia // nx), self.cx(ib % nx), self.cy(ib // nx), key)
            self.stamp(arrs, shape, self.clear, (lay,), nid)
        for i in vias:
            c = ("circle", self.cx(i % nx), self.cy(i // nx), self.via_r)
            self.stamp(arrs, c, self.clear, (0, 1), nid)
            self.stamp_mask(holes, ("circle", self.cx(i % nx), self.cy(i // nx), self.via_drill / 2),
                            float(self.o["hole_to_hole_mm"]) + self.via_drill / 2 + _SLACK)
            if i not in vias_out:
                vias_out.append(i)

    def _tracks(self, branches, pins, tht) -> List[Tuple[int, float, float, float, float, float]]:
        """Segments of a net: straight runs, split where another branch joins, plus a short stub from each end
        that lies inside a pad to that pad's centre (when it keeps the clearance)."""
        N, nx = self.N, self.nx
        ends = set()
        for path, _k in branches:
            ends.add(path[0])
            ends.add(path[-1])
        out = []
        for path, key in branches:
            runs, _v = self._runs(path, tht, ends)
            for lay, a, b in runs:
                ia, ib = a % N, b % N
                out.append((lay, self.cx(ia % nx), self.cy(ia // nx), self.cx(ib % nx), self.cy(ib // nx), 2 * key, -1))
            for s in (path[0], path[-1]):
                lay, i = int(s >= N), s % N
                x, y = self.cx(i % nx), self.cy(i // nx)
                for p in pins:
                    if p.on_copper("top" if lay == 0 else "bottom") and point_dist(pad_shape(p), x, y) < -0.02:
                        if (abs(x - p.x) > 1e-4 or abs(y - p.y) > 1e-4):
                            out.append((lay, x, y, round(p.x, 4), round(p.y, 4), 2 * key, 1))
                        break
        # stubs are marked (1) so the checker can drop just them instead of the whole net
        seen, uniq = set(), []
        for t in out:
            k = t[:6]
            if k in seen or (t[1] == t[3] and t[2] == t[4]):
                continue
            seen.add(k)
            uniq.append(t)
        return uniq

    # ------------------------------------------------------------------ #
    def _verify(self, results: Dict[str, Dict[str, Any]]) -> List[str]:
        """Exact clearance check of every routed item; stubs that fail are dropped, nets that fail are returned."""
        cl = self.clear + 1e-4
        ec = float(self.o["edge_clearance_mm"]) + 1e-4
        hc = float(self.o["hole_clearance_mm"]) + 1e-4
        h2h = float(self.o["hole_to_hole_mm"]) + 1e-4
        sp = [_Spatial(), _Spatial()]
        for shape, layers, owner, _p in self.pads:
            for l in layers:
                sp[l].add(shape_bbox(shape), (shape, owner))
        items = {}
        for n, r in results.items():
            nid = self.net_id[n]
            for t in r["tracks"]:
                s = ("capsule", t[1], t[2], t[3], t[4], t[5] / 2)
                items.setdefault(n, []).append((t, s, (t[0],)))
                sp[t[0]].add(shape_bbox(s), (s, nid))
            for v in r["vias"]:
                s = ("circle", v[0], v[1], self.via_r)
                items.setdefault(n, []).append((v, s, (0, 1)))
                for l in (0, 1):
                    sp[l].add(shape_bbox(s), (s, nid))
        edge_sp = _Spatial(5.0)
        for e in self.edges:
            edge_sp.add((min(e[0], e[2]), min(e[1], e[3]), max(e[0], e[2]), max(e[1], e[3])), e)
        drills = _Spatial()
        for x, y, r in self.drills:
            drills.add((x - r, y - r, x + r, y + r), (x, y, r))
        for n, r in results.items():
            for v in r["vias"]:
                rr = self.via_drill / 2
                drills.add((v[0] - rr, v[1] - rr, v[0] + rr, v[1] + rr), (v[0], v[1], rr))
        bad: List[str] = []
        for n in sorted(items, key=lambda k: self.net_id[k]):
            nid = self.net_id[n]
            drop = []
            failed = False
            for raw, s, layers in items[n]:
                bb = shape_bbox(s)
                q = (bb[0] - 1.0, bb[1] - 1.0, bb[2] + 1.0, bb[3] + 1.0)
                viol = False
                for l in layers:
                    for o, own in sp[l].query(q):
                        if own != nid and shape_dist(o, s) < cl:
                            viol = True
                            break
                    if viol:
                        break
                if not viol:
                    for e in edge_sp.query(q):
                        if shape_dist(("capsule", e[0], e[1], e[2], e[3], 0.0), s) < ec:
                            viol = True
                            break
                if not viol:
                    for h in self.npth:
                        if shape_dist(h, s) < hc:
                            viol = True
                            break
                if not viol and s[0] == "circle":
                    for x, y, rr in drills.query(q):
                        if (abs(x - s[1]) > 1e-6 or abs(y - s[2]) > 1e-6) and \
                                math.hypot(x - s[1], y - s[2]) - rr - self.via_drill / 2 < h2h:
                            viol = True
                            break
                if viol:
                    if s[0] == "capsule" and len(raw) > 6 and raw[6] == 1:
                        drop.append(raw)
                    else:
                        failed = True
                        break
            if failed:
                bad.append(n)
            elif drop:
                results[n]["tracks"] = [t for t in results[n]["tracks"] if t not in drop]
        for r in results.values():
            r["tracks"] = [t[:6] for t in r["tracks"]]
        return bad


def route_board(board, options: Optional[Dict[str, Any]] = None, power_nets: Sequence[str] = ()) -> Dict[str, Any]:
    """Route every net of a parsed two-layer board. Returns tracks (layer, x1, y1, x2, y2, width, net), vias
    (x, y, size, drill, net) and stats; the board itself is not changed."""
    return Router(board, options, power_nets).route()
