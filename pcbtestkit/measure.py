"""Measure the board's manufacturing features once, as item streams for the class-system engine.

The streams are independent of any class system (see pcbtestkit.classsys.catalog):
system-specific choices such as drill-tool offsets are applied later by the engine,
so one measurement serves every class system.

* tracks       every track segment width
* gaps         copper-to-copper gaps between different nets below ``search_mm`` (tracks, pads, vias;
               zone clearance settings); overlaps of different nets are reported as shorts
* pair_gaps    smallest gap per net pair and layer (voltage spacing); nets in ``hv_nets`` are searched
               up to the spacing their voltage can need
* holes        vias, plated and non-plated holes with finished size, pad and finished-basis ring
* hole_pairs   hole-to-hole gaps (different footprints) below ``pair_search_mm``
* hole_copper  hole to the nearest unrelated copper per layer, including zone fills (plane clearances)
* edge         copper to board edge
* mask         solder mask webs between pad openings
* silk_lines / silk_texts / smd_pads, plus board facts and feature counts
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from .geometry import BBox, Grid, point_in_segments, pt_seg_dist, rot, rect_corners, seg_seg_dist
from .pcb import Board

OUTER = ("F.Cu", "B.Cu")


class _Item:
    """Copper shape = core (polyline, point or filled polygon) inflated by ``half``."""
    __slots__ = ("net", "layer", "segs", "filled", "poly", "half", "bbox", "label", "ref", "netless", "kind", "owner")

    def __init__(self, net, layer, label, segs, half, poly=None, ref="", netless=False, kind="track", owner=None):
        self.net, self.layer, self.label, self.ref, self.netless = net, layer, label, ref, netless
        self.segs, self.half, self.poly = segs, half, poly
        self.kind, self.owner = kind, owner
        self.filled = poly is not None
        pts = [(s[0], s[1]) for s in segs] + [(s[2], s[3]) for s in segs]
        self.bbox = BBox.of_points(pts).grow(half)


def _poly_segs(poly):
    return [(poly[i][0], poly[i][1], poly[(i + 1) % len(poly)][0], poly[(i + 1) % len(poly)][1]) for i in range(len(poly))]


class _SegGrid:
    """Lean uniform grid for many short segments (zone-fill edges): no per-segment objects."""
    __slots__ = ("cell", "cells", "segs")

    def __init__(self, cell: float = 2.0):
        self.cell, self.cells, self.segs = cell, {}, []

    def add(self, s, net) -> None:
        i = len(self.segs)
        self.segs.append((s, net))
        c = self.cell
        x0, x1 = (s[0], s[2]) if s[0] <= s[2] else (s[2], s[0])
        y0, y1 = (s[1], s[3]) if s[1] <= s[3] else (s[3], s[1])
        cells = self.cells
        for ix in range(int(math.floor(x0 / c)), int(math.floor(x1 / c)) + 1):
            for iy in range(int(math.floor(y0 / c)), int(math.floor(y1 / c)) + 1):
                lst = cells.get((ix, iy))
                if lst is None:
                    cells[(ix, iy)] = [i]
                else:
                    lst.append(i)

    def query(self, bb: BBox):
        c, out = self.cell, set()
        for ix in range(int(math.floor(bb.x0 / c)), int(math.floor(bb.x1 / c)) + 1):
            for iy in range(int(math.floor(bb.y0 / c)), int(math.floor(bb.y1 / c)) + 1):
                lst = self.cells.get((ix, iy))
                if lst:
                    out.update(lst)
        return out


def _core_seg_dist(core, s) -> float:
    if core[0] == core[2] and core[1] == core[3]:
        return pt_seg_dist(core[0], core[1], s[0], s[1], s[2], s[3])
    return seg_seg_dist(core, s)


def _bbox_gap(a: BBox, b: BBox) -> float:
    dx = max(0.0, b.x0 - a.x1, a.x0 - b.x1)
    dy = max(0.0, b.y0 - a.y1, a.y0 - b.y1)
    return math.hypot(dx, dy)


def _dist(a: _Item, b: _Item) -> float:
    """Edge-to-edge distance between two copper shapes (<= 0 means touching/overlapping)."""
    if a.filled and any(point_in_segments(s[0], s[1], a.segs) for s in b.segs):
        return -1.0
    if b.filled and any(point_in_segments(s[0], s[1], b.segs) for s in a.segs):
        return -1.0
    return min(seg_seg_dist(s, t) for s in a.segs for t in b.segs) - a.half - b.half


def _pad_shape(p):
    """(segs, half, poly) exact for circle, oval, rect and roundrect pads."""
    if p.shape == "circle":
        return [(p.x, p.y, p.x, p.y)], p.w / 2, None
    if p.shape == "oval":
        r = min(p.w, p.h) / 2
        if abs(p.w - p.h) < 1e-6:
            return [(p.x, p.y, p.x, p.y)], r, None
        L = (max(p.w, p.h) - 2 * r) / 2
        lx, ly = (L, 0.0) if p.w > p.h else (0.0, L)
        dx, dy = rot(lx, ly, p.angle)
        return [(p.x - dx, p.y - dy, p.x + dx, p.y + dy)], r, None
    r = p.rratio * min(p.w, p.h) if p.shape == "roundrect" else 0.0
    poly = rect_corners(p.x, p.y, max(1e-4, p.w - 2 * r), max(1e-4, p.h - 2 * r), p.angle)
    return _poly_segs(poly), r, poly


def _hole_core(p) -> Tuple[Tuple[float, float, float, float], float]:
    """Drill as a stadium: (core segment, radius) - round holes are a point."""
    dw, dh = p.drill_w, p.drill_h or p.drill_w
    r = min(dw, dh) / 2
    if abs(dw - dh) < 1e-6:
        return (p.x, p.y, p.x, p.y), r
    L = (max(dw, dh) - min(dw, dh)) / 2
    lx, ly = (L, 0.0) if dw > dh else (0.0, L)
    dx, dy = rot(lx, ly, p.angle)
    return (p.x - dx, p.y - dy, p.x + dx, p.y + dy), r


def hv_radius(volts: float) -> float:
    """Search radius for a high-voltage net: the largest IPC-2221 Table 6-1 spacing at that voltage."""
    v = abs(volts)
    for lim, r in ((50, 0.8), (100, 1.6), (150, 3.3), (250, 6.5), (500, 12.6)):
        if v <= lim:
            return r
    return min(40.0, 12.6 + 0.025 * (v - 500))


def _items(board: Board):
    by_layer: Dict[str, List[_Item]] = {l: [] for l in board.copper_layers}
    used = {l: set() for l in board.copper_layers}      # nets with copper on each layer
    for t in board.tracks:
        if t.layer in used:
            used[t.layer].add(t.net)
    for z in board.zones:
        if not z.keepout:
            for l in board.copper_layers:
                if z.on_layer(l):
                    used[l].add(z.net)
    outer = set(OUTER)

    def present(layer, net, remove_unused):
        return layer in outer or not remove_unused or net in used[layer]

    for t in board.tracks:
        if t.layer not in by_layer:
            continue
        by_layer[t.layer].append(_Item(t.net or "~track", t.layer, "track %s" % (t.net or "(no net)"), t.segments(),
                                       t.width / 2, kind="track"))
    for i, v in enumerate(board.vias):
        for l in _via_layers(board, v):
            if present(l, v.net, v.remove_unused):
                by_layer[l].append(_Item(v.net or "~via", l, "via %s" % (v.net or ""), [(v.x, v.y, v.x, v.y)], v.size / 2,
                                         kind="via", owner=("v", i)))
    for fp in board.footprints:
        for p in fp.pads:
            if not p.has_copper:
                continue
            if p.is_tht or "*.Cu" in p.layers:
                layers = [l for l in board.copper_layers if present(l, p.net, p.remove_unused)]
            else:
                layers = [l for l in p.layers if l in by_layer]
            segs, half, poly = _pad_shape(p)
            net = p.net or "~pad:%s" % p.name
            for l in layers:
                by_layer[l].append(_Item(net, l, "pad %s" % p.name, segs, half, poly, ref=fp.ref, netless=not p.net,
                                         kind="pad", owner=("p", id(p))))
    return by_layer, used


def _via_layers(board: Board, v) -> List[str]:
    if v.via_type == "through":
        return list(board.copper_layers)
    idx = [board.copper_layers.index(l) for l in v.layers if l in board.copper_layers]
    if len(idx) < 2:
        return [l for l in v.layers if l in board.copper_layers]
    lo, hi = min(idx), max(idx)
    return board.copper_layers[lo:hi + 1]


def board_info(board: Board) -> Dict[str, Any]:
    """Board facts used as class-system context (and as the 'board' stream)."""
    from .classsys.values import normalize_finish
    inner = len(board.copper_layers) > 2
    bb = board.outline_bbox() if board.edge_segs else None
    bw = round(bb.w, 3) if bb else None
    bh = round(bb.h, 3) if bb else None
    cu = round(board.copper_thickness_mm * 1000, 1)
    return {"layers": len(board.copper_layers), "thickness_mm": round(board.thickness, 4), "copper_um": cu,
            "inner_copper_um": (round(board.inner_copper_mm * 1000, 1) if board.inner_copper_mm else cu) if inner else None,
            "finish": normalize_finish(board.copper_finish), "board_w_mm": bw, "board_h_mm": bh,
            "board_max_mm": max(bw, bh) if bb else None, "board_min_mm": min(bw, bh) if bb else None}


def measure_board(board: Board, search_mm: float = 0.45, hv_nets: Optional[Dict[str, float]] = None,
                  hole_search_mm: float = 0.6, pair_search_mm: float = 1.0, edge_search_mm: float = 1.0) -> Dict[str, Any]:
    inner_layers = [l for l in board.copper_layers if l not in OUTER]
    S: Dict[str, List[Dict[str, Any]]] = {k: [] for k in ("tracks", "gaps", "pair_gaps", "holes", "hole_pairs", "hole_copper",
                                                           "edge", "mask", "silk_lines", "silk_texts", "smd_pads")}
    feat = {"microvias": 0, "blind_vias": 0, "castellated": 0, "via_in_pad": 0, "mask_no_dam": 0,
            "holeless_vias": 0, "unringed_plated_holes": 0}
    feat_items: Dict[str, List[Dict[str, Any]]] = {k: [] for k in feat}
    by_layer, used = _items(board)

    # ---- tracks ---------------------------------------------------------------------------------
    for t in board.tracks:
        S["tracks"].append({"w": t.width, "layer": t.layer, "inner": t.layer not in OUTER, "net": t.net,
                            "x": round((t.x1 + t.x2) / 2, 3), "y": round((t.y1 + t.y2) / 2, 3)})

    # ---- copper gaps, shorts and per-net-pair minimum gaps -----------------------------------------
    shorts: List[Dict[str, Any]] = []
    pair_min: Dict[Tuple[str, str, str, bool], Dict[str, Any]] = {}
    grids: Dict[str, Grid] = {}

    def note_pair(layer, it, o, d, cx, cy):
        if it.net.startswith("~") or o.net.startswith("~") or (it.ref and it.ref == o.ref):
            return
        na, nb = sorted((it.net, o.net))
        lead = it.kind == "pad" and o.kind == "pad"
        key = (layer, na, nb, lead)
        cur = pair_min.get(key)
        if cur is None or d < cur["d"]:
            pair_min[key] = {"d": round(d, 4), "layer": layer, "inner": layer not in OUTER, "lead": lead,
                             "na": na, "nb": nb, "x": round(cx, 3), "y": round(cy, 3), "a": it.label, "b": o.label}

    for layer, lst in by_layer.items():
        inner = layer not in OUTER
        g = Grid(1.0)
        for i, it in enumerate(lst):
            g.insert(it.bbox, i)
        grids[layer] = g
        for i, it in enumerate(lst):
            for j in g.query(it.bbox.grow(search_mm)):
                if j <= i:
                    continue
                o = lst[j]
                if o.net == it.net or _bbox_gap(it.bbox, o.bbox) > search_mm + 1e-6:
                    continue
                same_fp = it.ref and it.ref == o.ref
                if same_fp and (it.netless or o.netless):
                    continue                     # e.g. multi-part exposed pads: footprint geometry, not routing
                d = _dist(it, o)
                if d <= 1e-6 and same_fp:
                    continue                     # overlapping pads inside one footprint are the footprint's design
                if d >= search_mm:
                    continue
                cx = (it.bbox.cx + o.bbox.cx) / 2
                cy = (it.bbox.cy + o.bbox.cy) / 2
                if d <= 1e-6:
                    shorts.append({"layer": layer, "a": it.label, "b": o.label, "x": round(cx, 3), "y": round(cy, 3)})
                    continue
                S["gaps"].append({"d": round(d, 4), "layer": layer, "inner": inner, "ka": it.kind, "kb": o.kind,
                                  "lead": it.kind == "pad" and o.kind == "pad", "a": it.label, "b": o.label,
                                  "x": round(cx, 3), "y": round(cy, 3)})
                note_pair(layer, it, o, d, cx, cy)
    for z in board.zones:
        if z.keepout or not z.filled or not z.clearance:
            continue
        pts = z.outline or [(0, 0)]
        kinds = {l not in OUTER for l in board.copper_layers if z.on_layer(l)}
        for inner in sorted(kinds):
            S["gaps"].append({"d": z.clearance, "layer": "inner" if inner else "outer", "inner": inner, "ka": "zone",
                              "kb": "setting", "lead": False, "a": "zone %s" % z.net, "b": "clearance setting",
                              "x": round(pts[0][0], 3), "y": round(pts[0][1], 3)})
    # high-voltage nets: search further for the voltage-dependent spacing
    for net, volts in (hv_nets or {}).items():
        r = hv_radius(volts)
        if r <= search_mm:
            continue
        for layer, lst in by_layer.items():
            g = grids[layer]
            for it in lst:
                if it.net != net:
                    continue
                for j in g.query(it.bbox.grow(r)):
                    o = lst[j]
                    if o.net == net or o.net.startswith("~") or _bbox_gap(it.bbox, o.bbox) >= r:
                        continue
                    d = _dist(it, o)
                    if 1e-6 < d < r:
                        note_pair(layer, it, o, d, (it.bbox.cx + o.bbox.cx) / 2, (it.bbox.cy + o.bbox.cy) / 2)
    S["pair_gaps"] = list(pair_min.values())

    # ---- holes ------------------------------------------------------------------------------------
    holes: List[Dict[str, Any]] = []
    hole_geo: List[Tuple[Tuple[float, float, float, float], float, Any, List[str], str]] = []
    for i, v in enumerate(board.vias):
        if v.drill < 0.05:
            feat["holeless_vias"] += 1
            continue
        if v.via_type == "micro":
            feat["microvias"] += 1
            feat_items["microvias"].append({"x": round(v.x, 3), "y": round(v.y, 3), "label": "microvia %s" % v.net})
            continue
        if v.via_type == "blind":
            feat["blind_vias"] += 1
            feat_items["blind_vias"].append({"x": round(v.x, 3), "y": round(v.y, 3), "label": "blind via %s" % v.net})
        span = _via_layers(board, v)
        inner = any(l in inner_layers and (not v.remove_unused or v.net in used[l]) for l in span)
        holes.append({"kind": "via", "sub": v.via_type, "plated": True, "finished": v.drill, "max_d": v.drill, "slot": False,
                      "pad": v.size, "ring_f": (v.size - v.drill) / 2, "ringed": True, "inner": inner, "net": v.net,
                      "x": round(v.x, 3), "y": round(v.y, 3), "label": "via %s" % (v.net or "")})
        hole_geo.append(((v.x, v.y, v.x, v.y), v.drill / 2, ("v", i), span, ""))
    for fp in board.footprints:
        for p in fp.pads:
            if "pad_prop_castellated" in p.props:
                feat["castellated"] += 1
                feat_items["castellated"].append({"x": round(p.x, 3), "y": round(p.y, 3), "label": "pad %s" % p.name})
            if not p.drill_w:
                continue
            dw, dh = p.drill_w, p.drill_h or p.drill_w
            core, r = _hole_core(p)
            base = {"finished": min(dw, dh), "max_d": max(dw, dh), "slot": abs(dw - dh) > 1e-6, "sub": "through",
                    "net": p.net, "x": round(p.x, 3), "y": round(p.y, 3),
                    "label": ("pad %s" % p.name) if p.number else "hole %s" % fp.ref, "ref": fp.ref}
            if p.is_npth:
                holes.append(dict(base, kind="npth", plated=False, pad=None, ring_f=None, ringed=False, inner=False))
                hole_geo.append((core, r, ("p", id(p)), list(board.copper_layers), fp.ref))
                continue
            ring = min((p.w - dw) / 2, (p.h - dh) / 2)
            ringed = not (ring <= 1e-6 and not p.net)
            if not ringed:
                feat["unringed_plated_holes"] += 1   # mechanical plated hole without a ring, by design
            inner = bool(inner_layers) and (not p.remove_unused or any(p.net in used[l] for l in inner_layers))
            holes.append(dict(base, kind="pth", plated=True, pad=min(p.w, p.h), ring_f=ring, ringed=ringed, inner=inner))
            hole_geo.append((core, r, ("p", id(p)), list(board.copper_layers), fp.ref))
    S["holes"] = holes

    # hole-to-hole (different footprints; vias have no footprint)
    hg = Grid(2.0)
    for i, (core, r, _, _, _) in enumerate(hole_geo):
        hg.insert(BBox.of_points([(core[0], core[1]), (core[2], core[3])]).grow(r), i)
    for i, (core, r, _, _, ref) in enumerate(hole_geo):
        bb = BBox.of_points([(core[0], core[1]), (core[2], core[3])]).grow(r + pair_search_mm)
        for j in hg.query(bb):
            if j <= i:
                continue
            core2, r2, _, _, ref2 = hole_geo[j]
            if ref and ref == ref2:
                continue                         # same footprint: designed by the part vendor
            gap = seg_seg_dist(core, core2) - r - r2
            if gap >= pair_search_mm:
                continue
            a, b = holes[i], holes[j]
            ka, kb = a["kind"], b["kind"]
            S["hole_pairs"].append({"gap": round(gap, 4), "ka": ka, "kb": kb, "pth": "pth" in (ka, kb),
                                    "npth": "npth" in (ka, kb), "vias": ka == kb == "via", "npth_pair": ka == kb == "npth",
                                    "diffnet": bool(a["net"] and b["net"] and a["net"] != b["net"]),
                                    "x": round((a["x"] + b["x"]) / 2, 3), "y": round((a["y"] + b["y"]) / 2, 3),
                                    "a": a["label"], "b": b["label"]})

    # hole to unrelated copper (items and zone fills of other nets), per layer
    fill_grids: Dict[str, _SegGrid] = {}
    for z in board.zones:
        if z.keepout:
            continue
        for layer, polys in z.fills.items():
            if layer not in by_layer:
                continue
            fg = fill_grids.setdefault(layer, _SegGrid(2.0))
            for poly in polys:
                n = len(poly)
                for k in range(n):
                    a, b = poly[k], poly[(k + 1) % n]
                    fg.add((a[0], a[1], b[0], b[1]), z.net)
    for h, (core, r, owner, span, ref) in zip(holes, hole_geo):
        reach = r + hole_search_mm
        hb = BBox.of_points([(core[0], core[1]), (core[2], core[3])]).grow(reach)
        hole_item = _Item(h["net"] or "~hole", "", h["label"], [core], 0.0)
        for layer in span:
            lst = by_layer[layer]
            best, to = math.inf, ""
            for j in grids[layer].query(hb):
                o = lst[j]
                if o.owner == owner or (h["net"] and o.net == h["net"]) or _bbox_gap(hb, o.bbox) > 0:
                    continue
                d = _dist(hole_item, o)
                if d < best:
                    best, to = d, o.label
            fg = fill_grids.get(layer)
            if fg:
                for j in fg.query(hb):
                    s, net = fg.segs[j]
                    if h["net"] and net == h["net"]:
                        continue
                    if max(0.0, min(s[0], s[2]) - hb.x1, hb.x0 - max(s[0], s[2])) > 0 or \
                            max(0.0, min(s[1], s[3]) - hb.y1, hb.y0 - max(s[1], s[3])) > 0:
                        continue
                    d = _core_seg_dist(core, s)
                    if d < best:
                        best, to = d, "zone %s" % net
            if best - r < hole_search_mm:
                S["hole_copper"].append({"cdist": round(best, 4), "finished": h["finished"], "kind": h["kind"],
                                         "layer": layer, "inner": layer not in OUTER, "to": to, "label": h["label"],
                                         "x": h["x"], "y": h["y"]})

    # ---- copper to board edge ----------------------------------------------------------------------------
    if board.edge_segs:
        for t in board.tracks:
            near = board.segs_near(t.bbox().grow(edge_search_mm))
            if near:
                d = min(seg_seg_dist(s, e) for s in t.segments() for e in near) - t.width / 2
                if d < edge_search_mm:
                    S["edge"].append({"d": round(d, 4), "kind": "track", "label": "track %s" % t.net,
                                      "x": round((t.x1 + t.x2) / 2, 3), "y": round((t.y1 + t.y2) / 2, 3)})
        for v in board.vias:
            d = board.edge_distance(v.x, v.y) - v.size / 2
            if d < edge_search_mm:
                S["edge"].append({"d": round(d, 4), "kind": "via", "label": "via %s" % v.net, "x": round(v.x, 3), "y": round(v.y, 3)})
        for fp in board.footprints:
            lid = fp.lib_id.lower()
            if "castellat" in lid or "cardedge" in lid.replace("_", ""):
                continue
            for p in fp.pads:
                if not p.has_copper or "pad_prop_castellated" in p.props:
                    continue
                near = board.segs_near(p.bbox().grow(edge_search_mm))
                if not near:
                    continue
                segs, half, _ = _pad_shape(p)
                d = min(seg_seg_dist(s, e) for s in segs for e in near) - half
                if d < edge_search_mm:
                    S["edge"].append({"d": round(d, 4), "kind": "pad", "label": "pad %s" % p.name, "x": round(p.x, 3), "y": round(p.y, 3)})
        # zone fills: query the fill-edge grids from each board-edge segment (few edges, many fill segments)
        for layer, fg in fill_grids.items():
            best: Dict[int, Tuple[float, Any, str]] = {}
            for ei, e in enumerate(board.edge_segs):
                bb = BBox(min(e[0], e[2]), min(e[1], e[3]), max(e[0], e[2]), max(e[1], e[3])).grow(edge_search_mm)
                for j in fg.query(bb):
                    s, net = fg.segs[j]
                    d = seg_seg_dist(s, e)
                    if d < edge_search_mm and (ei not in best or d < best[ei][0]):
                        best[ei] = (d, s, net)
            for d, s, net in best.values():
                S["edge"].append({"d": round(d, 4), "kind": "zone", "label": "zone %s fill (%s)" % (net, layer),
                                  "x": round((s[0] + s[2]) / 2, 3), "y": round((s[1] + s[3]) / 2, 3)})

    # ---- solder mask webs, SMD pads, vias in pads ------------------------------------------------------------
    for side, cu in (("top", "F.Cu"), ("bottom", "B.Cu")):
        mp: List[Tuple[_Item, float]] = []
        smd: List[Any] = []
        for fp in board.footprints:
            fm = fp.mask_margin
            for p in fp.pads:
                if not p.has_copper or not p.on_copper(side):
                    continue
                if p.kind == "smd":
                    smd.append(p)
                if not p.mask_open(side):
                    continue
                m = p.mask_margin if p.mask_margin is not None else (fm if fm is not None else board.mask_margin)
                segs, half, poly = _pad_shape(p)
                mp.append((_Item(p.net, cu, "pad %s" % p.name, segs, half, poly, ref=fp.ref, kind="pad"), m or 0.0))
        g = Grid(1.0)
        for i, (it, _) in enumerate(mp):
            g.insert(it.bbox, i)
        for i, (it, ma) in enumerate(mp):
            for j in g.query(it.bbox.grow(search_mm + 0.2)):
                if j <= i:
                    continue
                o, mb = mp[j]
                d = _dist(it, o)
                if d <= 1e-6:
                    continue
                web = d - ma - mb
                if web <= max(1e-6, board.mask_min_web):
                    feat["mask_no_dam"] += 1
                    if len(feat_items["mask_no_dam"]) < 200:
                        feat_items["mask_no_dam"].append({"x": round((it.bbox.cx + o.bbox.cx) / 2, 3),
                                                          "y": round((it.bbox.cy + o.bbox.cy) / 2, 3),
                                                          "label": "%s / %s" % (it.label, o.label)})
                elif web < search_mm:
                    S["mask"].append({"web": round(web, 4), "x": round((it.bbox.cx + o.bbox.cx) / 2, 3),
                                      "y": round((it.bbox.cy + o.bbox.cy) / 2, 3), "a": it.label, "b": o.label})
        pg = Grid(2.0)
        for p in smd:
            S["smd_pads"].append({"size": min(p.w, p.h), "x": round(p.x, 3), "y": round(p.y, 3), "label": "pad %s" % p.name})
            pg.insert(p.bbox(), p)
        for v in board.vias:
            if v.drill < 0.05 or cu not in _via_layers(board, v):
                continue
            for p in pg.query(BBox(v.x, v.y, v.x, v.y)):
                if p.contains(v.x, v.y):
                    feat["via_in_pad"] += 1
                    feat_items["via_in_pad"].append({"x": round(v.x, 3), "y": round(v.y, 3), "label": "via in pad %s" % p.name})
                    break

    # ---- silkscreen -------------------------------------------------------------------------------------------
    for w, x, y, layer, owner in board.silk_lines:
        S["silk_lines"].append({"w": w, "layer": layer, "x": round(x, 3), "y": round(y, 3), "label": owner})
    for h, t, x, y, layer, text in board.silk_texts:
        S["silk_texts"].append({"h": h, "t": t, "layer": layer, "x": round(x, 3), "y": round(y, 3), "text": text[:40]})

    info = board_info(board)
    for k in S:
        if k in ("tracks", "gaps", "pair_gaps", "hole_pairs", "edge", "mask", "smd_pads"):
            key = {"tracks": "w", "gaps": "d", "pair_gaps": "d", "hole_pairs": "gap", "edge": "d", "mask": "web", "smd_pads": "size"}[k]
            S[k].sort(key=lambda i: i[key])
    return {"streams": S, "board": info, "features": feat, "feature_items": feat_items,
            "shorts": shorts[:200], "short_count": len(shorts),
            "inner_layers": len(inner_layers), "copper_layers": len(board.copper_layers),
            "counts": {k: len(v) for k, v in S.items()}}
