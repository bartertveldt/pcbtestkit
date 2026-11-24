"""Weak-point analysis: production (fab + assembly) and life-cycle risks.

Each check is a small function registered with :func:`check`. Findings are
aggregated per check (one finding listing all affected items instead of one
finding per track), which keeps reports readable and keeps token usage low
when an AI model reads them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import classify as C
from .geometry import BBox, Grid, pt_seg_dist, seg_seg_dist
from .pcb import Footprint, Pad
from .project import Design

SEVERITIES = ["critical", "major", "minor", "info"]
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}
CATEGORIES = ["manufacturing", "assembly", "reliability", "electrical", "testability", "documentation", "drc", "erc"]


@dataclass
class Finding:
    id: str
    severity: str
    category: str
    title: str
    detail: str
    recommendation: str = ""
    refs: List[str] = field(default_factory=list)
    nets: List[str] = field(default_factory=list)
    items: List[Dict[str, Any]] = field(default_factory=list)
    count: int = 0
    data: Dict[str, Any] = field(default_factory=dict)
    uid: str = ""
    waived: str = ""

    def to_dict(self, max_items: int = 25) -> Dict[str, Any]:
        d: Dict[str, Any] = {"uid": self.uid, "id": self.id, "severity": self.severity, "category": self.category,
                             "title": self.title, "detail": self.detail}
        if self.recommendation:
            d["recommendation"] = self.recommendation
        if self.refs:
            d["refs"] = sorted(set(self.refs), key=C.natural_key)
        if self.nets:
            d["nets"] = sorted(set(self.nets), key=C.natural_key)
        d["count"] = self.count or max(1, len(self.items))
        if self.items:
            d["items"] = self.items[:max_items]
            if len(self.items) > max_items:
                d["items_truncated"] = len(self.items) - max_items
        if self.data:
            d["data"] = self.data
        if self.waived:
            d["waived"] = self.waived
        return d


CheckFn = Callable[["Ctx"], Iterable[Finding]]
CHECKS: List[Tuple[str, str, CheckFn]] = []


def check(check_id: str, description: str):
    def deco(fn: CheckFn) -> CheckFn:
        CHECKS.append((check_id, description, fn))
        return fn
    return deco


def _r(v: float, nd: int = 3) -> float:
    return round(float(v), nd)


def _loc(x: float, y: float, **kw) -> Dict[str, Any]:
    d = {"x": _r(x, 2), "y": _r(y, 2)}
    d.update({k: v for k, v in kw.items() if v not in (None, "")})
    return d


class Ctx:
    """Shared, lazily-built indexes for the checks."""

    def __init__(self, design: Design):
        self.d = design
        self.b = design.board
        self.cfg = design.cfg
        self.a = design.cfg["analysis"]
        self.fab = design.cfg["fab"]
        self.comps = design.components()
        self._pad_grid: Optional[Grid] = None
        self._track_grid: Optional[Grid] = None
        self._net_pads: Optional[Dict[str, List[Pad]]] = None

    @property
    def pad_grid(self) -> Grid:
        if self._pad_grid is None:
            g = Grid(2.0)
            for p in self.b.all_pads():
                g.insert(p.bbox(), p)
            self._pad_grid = g
        return self._pad_grid

    @property
    def track_grid(self) -> Grid:
        if self._track_grid is None:
            g = Grid(2.0)
            for t in self.b.tracks:
                g.insert(t.bbox(), t)
            self._track_grid = g
        return self._track_grid

    @property
    def net_pads(self) -> Dict[str, List[Pad]]:
        if self._net_pads is None:
            self._net_pads = self.b.pads_by_net()
        return self._net_pads

    def pads_at(self, x: float, y: float, margin: float = 0.0) -> List[Pad]:
        return [p for p in self.pad_grid.query(BBox(x - margin, y - margin, x + margin, y + margin))
                if p.contains(x, y, margin)]

    def fp_of(self, pad: Pad) -> Optional[Footprint]:
        return self.b.by_ref.get(pad.ref)

    def pad_edge_distance(self, p: Pad) -> float:
        if not self.b.edge_segs:
            return math.inf
        if p.shape == "circle":
            return self.b.edge_distance(p.x, p.y) - p.w / 2
        poly = p.polygon()
        segs = [(poly[i][0], poly[i][1], poly[(i + 1) % 4][0], poly[(i + 1) % 4][1]) for i in range(4)]
        near = self.b.segs_near(p.bbox().grow(6.0))
        if not near:
            return self.b.edge_distance(p.x, p.y) - max(p.w, p.h) / 2
        return min(seg_seg_dist(s, e) for s in segs for e in near)

    def fp_edge_distance(self, fp: Footprint) -> float:
        cu = [p for p in fp.pads if p.has_copper]
        if cu:
            return min(self.pad_edge_distance(p) for p in cu)
        return self.b.edge_distance(fp.x, fp.y)

    def nearest_edge_dir(self, x: float, y: float) -> Optional[Tuple[float, float]]:
        near = self.b.segs_near(BBox(x - 10, y - 10, x + 10, y + 10)) or self.b.edge_segs
        if not near:
            return None
        s = min(near, key=lambda e: pt_seg_dist(x, y, *e))
        dx, dy = s[2] - s[0], s[3] - s[1]
        n = math.hypot(dx, dy) or 1.0
        return dx / n, dy / n

    def effective_zone_connection(self, pad: Pad) -> Optional[str]:
        """'solid' | 'thermal' | 'none' | None (no zone of this net under the pad)."""
        if not pad.net:
            return None
        if pad.is_tht:
            pad_layers = list(self.b.copper_layers)
        else:
            pad_layers = [l for l, side in (("F.Cu", "top"), ("B.Cu", "bottom")) if pad.on_copper(side)]
        hit = None
        for z in self.b.zones:
            if z.keepout or z.net != pad.net:
                continue
            if not any(z.on_layer(l) for l in pad_layers):
                continue
            if not z.contains(pad.x, pad.y):
                continue
            hit = z
            break
        if hit is None:
            return None
        if pad.zone_connect is not None and pad.zone_connect >= 0:
            mode = {0: "none", 1: "thermal", 2: "solid", 3: "thermal"}.get(pad.zone_connect, "thermal")
        else:
            fp = self.fp_of(pad)
            mode = hit.connect
            if mode == "tht_thermal":
                mode = "thermal" if pad.is_tht else "solid"
            if fp is not None:
                fz = fp.props.get("zone_connect")
                if fz:
                    mode = {"0": "none", "1": "thermal", "2": "solid", "3": "thermal"}.get(fz, mode)
        return mode


def _sev_max(items: List[Dict[str, Any]], key: str = "severity", default: str = "minor") -> str:
    sevs = [i.get(key, default) for i in items]
    return min(sevs, key=lambda s: SEV_RANK.get(s, 9)) if sevs else default


def ipc2221_current(width_mm: float, copper_mm: float, rise_c: float, external: bool = True) -> float:
    """IPC-2221 trace current capacity (A)."""
    area_mil2 = (width_mm / 0.0254) * (copper_mm / 0.0254)
    k = 0.048 if external else 0.024
    return k * (rise_c ** 0.44) * (area_mil2 ** 0.725)


# =========================================================================== #
# Manufacturing
# =========================================================================== #
@check("MFG-TRACK-WIDTH", "Track widths below or close to the fabrication minimum")
def c_track_width(x: Ctx):
    lim, warn = x.fab["min_track"], x.fab["min_track"] * x.fab["warn_factor"]
    bad, near = [], []
    for t in x.b.tracks:
        if t.width < lim - 1e-6:
            bad.append(t)
        elif t.width < warn - 1e-6:
            near.append(t)
    for lst, sev, label in ((bad, "critical", "below"), (near, "minor", "close to")):
        if not lst:
            continue
        lst.sort(key=lambda t: t.width)
        items = [_loc((t.x1 + t.x2) / 2, (t.y1 + t.y2) / 2, layer=t.layer, net=t.net, width=_r(t.width)) for t in lst]
        yield Finding("MFG-TRACK-WIDTH", sev, "manufacturing", "%d track segment(s) %s the %.3f mm fab minimum" %
                      (len(lst), label, lim),
                      "Narrowest track: %.3f mm. Over-etching on thin tracks causes opens and lower yield." % lst[0].width,
                      "Widen tracks to at least %.3f mm where space allows, or choose a fab profile that supports them." % warn,
                      nets=[t.net for t in lst], items=items, count=len(lst), data={"min_width_mm": _r(lst[0].width)})


@check("MFG-VIA", "Via drill size, annular ring and aspect ratio")
def c_vias(x: Ctx):
    f = x.fab
    small, ring, ring_near, aspect = [], [], [], []
    off = float(f.get("aspect_toolsize_offset", 0.0))
    for v in x.b.vias:
        loc = _loc(v.x, v.y, net=v.net, drill=_r(v.drill), size=_r(v.size))
        if v.via_type == "micro" or v.drill < 0.05:
            continue
        if v.drill < f["min_drill"] - 1e-6:
            small.append(loc)
        if v.annular < f["min_via_annular"] - 1e-6:
            ring.append(dict(loc, annular=_r(v.annular)))
        elif v.annular < f["min_via_annular"] * f["warn_factor"] - 1e-6:
            ring_near.append(dict(loc, annular=_r(v.annular)))
        if v.via_type == "through" and v.drill > 0 and x.b.thickness / (v.drill + off) > f["max_aspect_ratio"]:
            aspect.append(dict(loc, aspect=_r(x.b.thickness / (v.drill + off), 1)))
    if small:
        yield Finding("MFG-VIA", "critical", "manufacturing", "%d via(s) with drill below %.2f mm" % (len(small), f["min_drill"]),
                      "Mechanical drills below the fab minimum need laser drilling (HDI) or are rejected.",
                      "Increase via drill to >= %.2f mm or order an HDI process." % f["min_drill"], items=small)
    if ring:
        yield Finding("MFG-VIA", "major", "manufacturing", "%d via(s) with annular ring below %.3f mm" % (len(ring), f["min_via_annular"]),
                      "Drill wander can break out of a thin annular ring, giving intermittent or open connections.",
                      "Increase via pad diameter (size - drill >= %.2f mm)." % (2 * f["min_via_annular"] * f["warn_factor"]), items=ring)
    if ring_near:
        yield Finding("MFG-VIA", "minor", "manufacturing", "%d via(s) with annular ring close to the minimum" % len(ring_near),
                      "Annular ring is within %d%% of the fab limit; yield drops at the limit." % int((f["warn_factor"] - 1) * 100),
                      "Use a slightly larger via pad where space allows.", items=ring_near)
    if aspect:
        yield Finding("MFG-VIA-ASPECT", "major", "manufacturing",
                      "%d via(s) exceed aspect ratio %.0f:1 (board %.2f mm)" % (len(aspect), f["max_aspect_ratio"], x.b.thickness),
                      "High aspect-ratio holes plate unevenly: thin barrel copper cracks under thermal cycling.",
                      "Use larger drills or a thinner board.", items=aspect)


@check("MFG-THT-ANNULAR", "Annular ring of plated through-hole pads")
def c_tht_ring(x: Ctx):
    f = x.fab
    bad, near = [], []
    for p in x.b.all_pads():
        if not p.is_tht or not p.drill_w:
            continue
        ring = min((p.w - p.drill_w) / 2, (p.h - p.drill_h) / 2)
        it = _loc(p.x, p.y, pad=p.name, annular=_r(ring), drill=_r(p.drill_w))
        if ring < f["min_tht_annular"] - 1e-6:
            bad.append(it)
        elif ring < f["min_tht_annular"] * f["warn_factor"] - 1e-6:
            near.append(it)
    if bad:
        yield Finding("MFG-THT-ANNULAR", "major", "manufacturing", "%d plated hole pad(s) with annular ring < %.2f mm" %
                      (len(bad), f["min_tht_annular"]),
                      "Thin rings lift during hand or wave soldering and give weak solder fillets.",
                      "Enlarge the pads; a ring of >= 0.25 mm is a good target for hand-soldered parts.",
                      refs=[i["pad"].split(".")[0] for i in bad], items=bad)
    if near:
        yield Finding("MFG-THT-ANNULAR", "minor", "manufacturing", "%d plated hole pad(s) with annular ring close to the minimum" % len(near),
                      "Rings within %d%% of the fab limit." % int((f["warn_factor"] - 1) * 100),
                      "Enlarge pads where possible.", refs=[i["pad"].split(".")[0] for i in near], items=near)


@check("MFG-EDGE-CLEARANCE", "Copper too close to the board edge")
def c_edge(x: Ctx):
    if not x.b.edge_segs:
        yield Finding("MFG-OUTLINE", "critical", "manufacturing", "No board outline on Edge.Cuts",
                      "Without an outline the fab cannot route the board and edge checks are skipped.",
                      "Draw a closed outline on the Edge.Cuts layer.")
        return
    lim = x.fab["min_edge_clearance"]
    warn = lim * x.fab["warn_factor"]
    bad, near = [], []
    for t in x.b.tracks:
        bb = t.bbox().grow(warn + 1)
        segs = x.b.segs_near(bb)
        if not segs:
            continue
        dmin = min(seg_seg_dist(s, e) for s in t.segments() for e in segs) - t.width / 2
        if dmin < warn:
            (bad if dmin < lim else near).append(_loc((t.x1 + t.x2) / 2, (t.y1 + t.y2) / 2, kind="track", net=t.net,
                                                     layer=t.layer, clearance=_r(dmin)))
    for v in x.b.vias:
        d = x.b.edge_distance(v.x, v.y) - v.size / 2
        if d < warn:
            (bad if d < lim else near).append(_loc(v.x, v.y, kind="via", net=v.net, clearance=_r(d)))
    for fp in x.comps:
        if "castellat" in fp.lib_id.lower() or "cardedge" in fp.lib_id.lower().replace("_", ""):
            continue
        for p in fp.pads:
            if not p.has_copper or "pad_prop_castellated" in p.props:
                continue
            d = x.pad_edge_distance(p)
            if d < warn:
                (bad if d < lim else near).append(_loc(p.x, p.y, kind="pad", pad=p.name, net=p.net, clearance=_r(d)))
    if bad:
        yield Finding("MFG-EDGE-CLEARANCE", "major", "manufacturing", "%d copper item(s) closer than %.2f mm to the board edge" % (len(bad), lim),
                      "Routing/scoring exposes or burrs copper that sits this close to the edge (shorts to enclosure, delamination).",
                      "Pull copper back to >= %.2f mm from Edge.Cuts or ask the fab for edge plating." % warn,
                      refs=[i["pad"].split(".")[0] for i in bad if "pad" in i], nets=[i.get("net", "") for i in bad if i.get("net")],
                      items=sorted(bad, key=lambda i: i["clearance"]))
    if near:
        yield Finding("MFG-EDGE-CLEARANCE", "minor", "manufacturing", "%d copper item(s) close to the board edge" % len(near),
                      "Between %.2f and %.2f mm from the edge." % (lim, warn), "Add margin where cheap to do so.",
                      items=sorted(near, key=lambda i: i["clearance"]))


@check("MFG-TECH-CLASS", "Technology class the board needs vs the class ordered (any class system), plus extra class checks")
def c_tech_class(x: Ctx):
    t = x.d.technology()
    m = x.d.measurement()
    if m["short_count"]:
        yield Finding("MFG-COPPER-SHORT", "critical", "manufacturing", "%d place(s) where copper of different nets touches" % m["short_count"],
                      "Overlapping tracks/pads of different nets short every board.", "Fix the routing (run KiCad DRC).",
                      items=m["shorts"], count=m["short_count"])
    if t["microvias"]:
        yield Finding("MFG-HDI", "info", "manufacturing", "%d laser microvia(s): HDI process" % t["microvias"],
                      "Microvias are outside standard drill classes and need an HDI process.", "Confirm availability and cost with the fab.")
    for r in [t["primary"]] + t["checks"]:
        yield from _class_findings(x, r, primary=r is t["primary"])
    tl = t.get("target_layers")
    if tl and int(tl) != t["layers"]:
        yield Finding("MFG-TECH-LAYERS", "major", "manufacturing", "Board has %d copper layers, %s specified" % (t["layers"], tl),
                      "The stack-up does not match the technology you plan to order.", "Change the board setup or the tech.layers value.")


def _short(r: Dict[str, Any]) -> str:
    return r.get("short") or r["name"]


def _viol_text(viol: Dict[str, Any]) -> str:
    parts = []
    for mid, v in viol.items():
        if mid.startswith("_"):
            parts.append(v["label"])
            continue
        lim = v["limit"] if v["limit"] is not None else "/".join(str(l) for l in (v.get("limits") or []))
        parts.append("%s: %d (worst %s vs %s)" % (v["label"], v["count"], v["worst"], lim))
    return "; ".join(parts)


def _viol_items(viol: Dict[str, Any], per: int = 8) -> List[Dict[str, Any]]:
    items = []
    for mid, v in viol.items():
        for i in (v.get("items") or [])[:per]:
            items.append(dict(i, parameter=mid))
    return items


def _class_findings(x: Ctx, r: Dict[str, Any], primary: bool):
    from .classsys.engine import DRIVING
    fid, cat = r.get("finding_id", "MFG-TECH-CLASS"), r.get("category", "manufacturing")
    system = x.d.class_system(r["system"])
    req, tg = r["required"], r.get("target")
    name = _short(r)
    cheapest = [d for d in system.dims if d.choose == "cheapest"]
    drv = "; ".join("%s %s at %s %s (limit %s)" % (d["label"], d["measured"], d["dimension"], d["level"], d["limit"])
                    for d in req.get("drivers", [])[:6])
    data = {"system": r["system"], "required": req.get("class"), "measured": r["measured"], "status": r["status"]}
    if cheapest:
        if req.get("beyond"):
            yield Finding(fid, "major", cat, "Board is finer than %s supports (%s)" % (name, ", ".join(req["beyond"])),
                          "No level of %s accepts these features: %s." % (r["name"], drv or "see items"),
                          "Widen the listed features, or choose a finer process or another class system.",
                          data=data)
        elif not tg:
            svc = (" (%s)" % ", ".join(req["services"])) if req.get("services") else ""
            yield Finding(fid, "info", cat, "Board needs %s class %s%s" % (name, req["class"], svc),
                          ("Driven by: %s." % drv) if drv else "The coarsest class of %s fits." % r["name"],
                          "Set the class you will order (tech.class) to check the board against its limits." if primary else "",
                          data=data)
    if tg:
        viol = dict(tg["violations"])
        for mid, v in ((r.get("process") or {}).get("violations") or {}).items():
            viol.setdefault(mid, v)
        blocking = {k: v for k, v in viol.items() if v.get("severity") in DRIVING}
        minor = {k: v for k, v in viol.items() if v.get("severity") not in DRIVING}
        finer = cheaper = False
        if req.get("class") and cheapest:
            for d in cheapest:
                ids = [l.id for l in d.levels]
                a, b = req["levels"].get(d.id), tg["levels"].get(d.id)
                if a in ids and b in ids:
                    finer |= ids.index(a) > ids.index(b)
                    cheaper |= ids.index(a) < ids.index(b)
        if blocking:
            n = sum(v["count"] for v in blocking.values())
            title = ("Board needs %s %s but %s is ordered" % (name, req["class"], tg["class"]) if finer else
                     "%d feature(s) break %s %s" % (n, name, tg["class"]))
            yield Finding(fid, "major", cat, title, _viol_text(blocking) + ".",
                          "Change the listed features to meet %s %s%s." % (
                              name, tg["class"], (", or order %s" % req["class"]) if finer else ""),
                          items=_viol_items(blocking), count=n, data=dict(data, target=tg["class"]))
        else:
            yield Finding(fid, "info", cat, "Board meets %s %s%s" % (name, tg["class"],
                                                                  (" (it would also fit %s)" % req["class"]) if cheaper else ""),
                          "Checked: %s." % ", ".join(sorted(v["label"] for v in tg["limits"].values()))[:600],
                          "A coarser class is usually cheaper and has better yield." if cheaper else "",
                          data=dict(data, target=tg["class"]))
        if minor:
            n = sum(v["count"] for v in minor.values())
            yield Finding(fid, "minor", cat, "%d minor item(s) outside %s %s" % (n, name, tg["class"]), _viol_text(minor) + ".",
                          "The fab usually adjusts these (thins legend, removes mask webs); fix them if appearance or solder bridging matters.",
                          items=_viol_items(minor), count=n, data=dict(data, target=tg["class"]))
        if tg.get("manual"):
            yield Finding(fid, "info", cat, "Put on the order for %s %s" % (name, tg["class"]),
                          "; ".join("%s: %s%s" % (mm["label"], mm["requirement"], (" (%s)" % mm["note"]) if mm["note"] else "")
                                    for mm in tg["manual"]) + ".",
                          "These cannot be checked from the design; state them on the order or in the fab notes.",
                          data=dict(data, target=tg["class"]))
    else:
        pv = (r.get("process") or {}).get("violations") or {}
        blocking = {k: v for k, v in pv.items() if v.get("severity") in DRIVING}
        minor = {k: v for k, v in pv.items() if v.get("severity") not in DRIVING}
        if blocking:
            n = sum(v["count"] for v in blocking.values())
            yield Finding(fid, "major", cat, "%d feature(s) break %s process limits" % (n, name), _viol_text(blocking) + ".",
                          "These limits apply to every class of %s." % r["name"], items=_viol_items(blocking), count=n, data=data)
        if minor:
            n = sum(v["count"] for v in minor.values())
            yield Finding(fid, "minor", cat, "%d minor item(s) outside %s limits" % (n, name), _viol_text(minor) + ".",
                          "The fab usually adjusts these.", items=_viol_items(minor), count=n, data=data)


@check("MFG-HOLE-SPACING", "Drill-to-drill spacing")
def c_hole_spacing(x: Ctx):
    holes: List[Tuple[float, float, float, str, str]] = []
    for v in x.b.vias:
        holes.append((v.x, v.y, v.drill / 2, "via", ""))
    for p in x.b.all_pads():
        if p.drill_w:
            holes.append((p.x, p.y, max(p.drill_w, p.drill_h) / 2, p.name, p.ref))
    g = Grid(3.0)
    for i, h in enumerate(holes):
        g.insert(BBox(h[0] - h[2], h[1] - h[2], h[0] + h[2], h[1] + h[2]), i)
    lim = x.fab["min_hole_to_hole"]
    out = []
    for i, h in enumerate(holes):
        for j in g.query(BBox(h[0] - h[2] - lim, h[1] - h[2] - lim, h[0] + h[2] + lim, h[1] + h[2] + lim)):
            if j <= i:
                continue
            o = holes[j]
            if h[4] and h[4] == o[4]:
                continue  # same footprint: designed by the part vendor
            gap = math.hypot(h[0] - o[0], h[1] - o[1]) - h[2] - o[2]
            if gap < lim:
                out.append(_loc((h[0] + o[0]) / 2, (h[1] + o[1]) / 2, a=h[3], b=o[3], gap=_r(gap)))
    if out:
        yield Finding("MFG-HOLE-SPACING", "major", "manufacturing", "%d hole pair(s) closer than %.2f mm" % (len(out), lim),
                      "Thin webs between drills crack or merge during drilling.", "Increase spacing between the holes.",
                      items=sorted(out, key=lambda i: i["gap"]))


@check("MFG-ACID-TRAP", "Acute angles between tracks (acid traps)")
def c_acid(x: Ctx):
    thr = x.a["acid_trap_deg"]
    ends: Dict[Tuple[str, str, int, int], List[Tuple[float, float]]] = {}
    for t in x.b.tracks:
        if t.kind != "segment":
            continue
        for (px, py, qx, qy) in ((t.x1, t.y1, t.x2, t.y2), (t.x2, t.y2, t.x1, t.y1)):
            key = (t.layer, t.net, int(round(px * 1000)), int(round(py * 1000)))
            ends.setdefault(key, []).append((qx - px, qy - py))
    out = []
    for (layer, net, ix, iy), vecs in ends.items():
        if len(vecs) < 2:
            continue
        px, py = ix / 1000.0, iy / 1000.0
        if any(p.net == net for p in x.pads_at(px, py)):
            continue
        worst = 180.0
        for i in range(len(vecs)):
            for j in range(i + 1, len(vecs)):
                a, b = vecs[i], vecs[j]
                na, nb = math.hypot(*a), math.hypot(*b)
                if na < 1e-6 or nb < 1e-6:
                    continue
                cosang = max(-1.0, min(1.0, (a[0] * b[0] + a[1] * b[1]) / (na * nb)))
                ang = math.degrees(math.acos(cosang))
                worst = min(worst, ang)
        if 1.0 < worst < thr:
            out.append(_loc(px, py, layer=layer, net=net, angle=_r(worst, 1)))
    if out:
        yield Finding("MFG-ACID-TRAP", "minor", "manufacturing", "%d acute track junction(s) (< %d deg)" % (len(out), thr),
                      "Acute copper angles trap etchant and can over-etch, creating thin necks.",
                      "Re-route the junctions with 45/90 degree entries.", items=out)


# =========================================================================== #
# Assembly
# =========================================================================== #
def _smd_parts(x: Ctx, side: str) -> List[Footprint]:
    return [fp for fp in x.comps if fp.side == side and fp.is_smd and not C.is_fiducial(fp) and not C.is_testpoint(fp)
            and not fp.board_only and not fp.dnp]


@check("ASM-FIDUCIALS", "Fiducials for pick-and-place")
def c_fiducials(x: Ctx):
    for side in ("top", "bottom"):
        smd = _smd_parts(x, side)
        if not smd:
            continue
        fids = [fp for fp in x.comps if C.is_fiducial(fp) and fp.side == side]
        if len(fids) == 0:
            yield Finding("ASM-FIDUCIALS", "major", "assembly", "No fiducials on the %s side (%d SMD parts)" % (side, len(smd)),
                          "Pick-and-place machines need >= 2 global fiducials per populated side for accurate placement.",
                          "Add 2-3 fiducials in opposite corners of the %s side, or confirm that the panel rails carry them." % side,
                          data={"side": side, "smd_parts": len(smd)})
        elif len(fids) == 1:
            yield Finding("ASM-FIDUCIALS", "minor", "assembly", "Only one fiducial on the %s side" % side,
                          "One fiducial cannot correct rotation.", "Add at least one more fiducial, far from the first.",
                          refs=[f.ref for f in fids])


@check("ASM-FINE-PITCH", "Fine-pitch and BGA parts")
def c_fine_pitch(x: Ctx):
    fine, bga = [], []
    for fp in x.comps:
        if not fp.is_smd or len(fp.pads) < 4:
            continue
        pitch = fp.min_pitch()
        nm = fp.name.upper()
        if "BGA" in nm or "WLCSP" in nm or "CSP" in nm:
            bga.append({"ref": fp.ref, "package": fp.name, "pitch": _r(pitch)})
        elif pitch <= 0.5 + 1e-6:
            fine.append({"ref": fp.ref, "package": fp.name, "pitch": _r(pitch)})
    if fine:
        yield Finding("ASM-FINE-PITCH", "minor", "assembly", "%d fine-pitch (<= 0.5 mm) part(s)" % len(fine),
                      "Fine pitch raises the risk of solder bridges and insufficient joints.",
                      "Use a <= 0.12 mm stencil with reduced apertures, solder-mask dams between pads and AOI after reflow.",
                      refs=[i["ref"] for i in fine], items=fine)
    if bga:
        yield Finding("ASM-BGA", "info", "assembly", "%d BGA/CSP part(s) need X-ray inspection" % len(bga),
                      "Hidden joints cannot be inspected optically.", "Budget for X-ray inspection and boundary scan if available.",
                      refs=[i["ref"] for i in bga], items=bga)


@check("ASM-MLCC-EDGE", "Ceramic capacitors near board edges (flex cracking)")
def c_mlcc_edge(x: Ctx):
    lim = x.a["edge_mlcc_mm"]
    items = []
    for fp in x.comps:
        if not C.is_mlcc(fp) or len(fp.pads) != 2:
            continue
        d = x.fp_edge_distance(fp)
        if d >= lim:
            continue
        p1, p2 = fp.pads[0], fp.pads[1]
        ax, ay = p2.x - p1.x, p2.y - p1.y
        n = math.hypot(ax, ay) or 1.0
        ed = x.nearest_edge_dir(fp.x, fp.y)
        orient = ""
        if ed:
            cosang = abs((ax * ed[0] + ay * ed[1]) / n)
            orient = "parallel" if cosang > 0.7 else "perpendicular"
        size = C.passive_size(fp)
        big = size in ("0805", "1206", "1210", "1812", "2220")
        sev = "major" if (big or d < 1.0 or orient == "perpendicular") else "minor"
        items.append({"ref": fp.ref, "package": size, "edge_mm": _r(d, 2), "orientation": orient, "severity": sev,
                      "x": _r(fp.x, 2), "y": _r(fp.y, 2)})
    if items:
        yield Finding("ASM-MLCC-EDGE", _sev_max(items), "reliability", "%d MLCC(s) within %.1f mm of the board edge" % (len(items), lim),
                      "Board flexing during depaneling, ICT, connector insertion or screwing cracks MLCCs; cracks short the rail "
                      "later in the field. Perpendicular orientation (terminals at different distances from the edge) is worst.",
                      "Move MLCCs >= %.1f mm from edges and V-score lines, orient them parallel to the edge, or use "
                      "soft-termination / flexible-termination parts." % lim,
                      refs=[i["ref"] for i in items], items=items)


@check("ASM-COMPONENT-EDGE", "Components overhanging or touching the edge")
def c_comp_edge(x: Ctx):
    lim = x.a["edge_component_mm"]
    items = []
    if not x.b.edge_segs:
        return
    for fp in x.comps:
        if C.is_connector(fp) or C.is_mounting_hole(fp) or C.is_fiducial(fp) or fp.board_only:
            continue
        poly = fp.courtyard_polygon()
        inside = all(x.b.inside(px, py) for px, py in poly)
        dmin = min(x.b.edge_distance(px, py) for px, py in poly)
        segs = [(poly[i][0], poly[i][1], poly[(i + 1) % 4][0], poly[(i + 1) % 4][1]) for i in range(4)]
        near = x.b.segs_near(BBox.of_points(poly).grow(lim + 1))
        if near:
            dmin = min(dmin, min(seg_seg_dist(s, e) for s in segs for e in near))
        if not inside or dmin < lim:
            items.append({"ref": fp.ref, "edge_mm": _r(dmin if inside else 0.0, 2), "overhang": not inside})
    if items:
        yield Finding("ASM-COMPONENT-EDGE", "minor", "assembly", "%d component(s) within %.1f mm of (or over) the edge" % (len(items), lim),
                      "Parts at the edge collide with panel rails, conveyor clamps and depaneling tools.",
                      "Keep courtyards >= %.1f mm inside the outline or add breakaway rails." % max(lim, 3.0),
                      refs=[i["ref"] for i in items], items=items)


@check("ASM-TOMBSTONE", "Thermal imbalance on small two-terminal parts (tombstoning)")
def c_tombstone(x: Ctx):
    items = []
    for fp in x.comps:
        size = C.passive_size(fp)
        if size not in ("01005", "0201", "0402", "0603") or len(fp.pads) != 2 or not fp.is_smd:
            continue
        loads = []
        for p in fp.pads:
            heavy = []
            zc = x.effective_zone_connection(p)
            if zc == "solid":
                heavy.append("solid zone")
            tw = 0.0
            for t in x.track_grid.query(p.bbox()):
                if t.net != p.net:
                    continue
                if p.contains(t.x1, t.y1) or p.contains(t.x2, t.y2):
                    tw += t.width
            if tw >= max(0.6, p.min_size * 1.2):
                heavy.append("%.2f mm of track" % tw)
            if any(p.contains(v.x, v.y) for v in x.b.vias if v.net == p.net and abs(v.x - p.x) < 2 and abs(v.y - p.y) < 2):
                heavy.append("via in pad")
            loads.append(heavy)
        if bool(loads[0]) != bool(loads[1]):
            heavy_pad = fp.pads[0] if loads[0] else fp.pads[1]
            sev = "major" if size in ("01005", "0201", "0402") else "minor"
            items.append({"ref": fp.ref, "package": size, "heavy_pad": heavy_pad.number,
                          "reason": ", ".join(loads[0] or loads[1]), "severity": sev})
    if items:
        yield Finding("ASM-TOMBSTONE", _sev_max(items), "assembly", "%d small part(s) with unbalanced pad heating" % len(items),
                      "One pad heats much slower than the other (solid plane, wide track or via); surface tension then lifts "
                      "the part on end during reflow (tombstoning/open joint).",
                      "Use thermal reliefs on the plane connection and equal-width tracks on both pads.",
                      refs=[i["ref"] for i in items], items=items)


@check("ASM-VIA-IN-PAD", "Unfilled vias inside SMD pads (solder wicking)")
def c_via_in_pad(x: Ctx):
    items = []
    for v in x.b.vias:
        for p in x.pads_at(v.x, v.y):
            if p.kind != "smd" or p.net != v.net or not any(l.endswith(".Paste") for l in p.layers):
                continue  # no paste -> no solder to wick (test pads, edge fingers)
            fp = x.fp_of(p)
            if fp is None:
                continue
            is_ep = p.number.upper() in ("EP", "PAD", "0", "") or (p.w * p.h >= 4 * min(q.w * q.h for q in fp.pads if q.kind == "smd"))
            if is_ep and len(fp.pads) > 4:
                continue  # thermal pads: handled by ASM-THERMAL-PAD
            small = p.min_size < 0.8 or fp.min_pitch() < 0.8
            items.append(_loc(v.x, v.y, pad=p.name, net=p.net, severity="major" if small else "minor"))
            break
    if items:
        yield Finding("ASM-VIA-IN-PAD", _sev_max(items), "assembly", "%d via(s) inside SMD pads" % len(items),
                      "Open vias in pads wick solder away from the joint (starved joints, voids) unless they are filled and capped.",
                      "Move vias next to the pad (dog-bone) or order filled & capped vias (IPC-4761 type VII).",
                      refs=sorted({i["pad"].split(".")[0] for i in items}), items=items)


@check("ASM-THT-SOLID-ZONE", "Through-hole pads connected solid to planes")
def c_tht_solid(x: Ctx):
    items = []
    for fp in x.comps:
        for p in fp.pads:
            if p.is_tht and p.net and x.effective_zone_connection(p) == "solid":
                items.append(_loc(p.x, p.y, pad=p.name, net=p.net))
    if items:
        yield Finding("ASM-THT-SOLID-ZONE", "major", "assembly", "%d through-hole pad(s) with solid plane connection" % len(items),
                      "The plane sinks heat away: hole fill is poor in wave/selective soldering and hand soldering creates cold joints.",
                      "Use thermal-relief spokes for THT pads (zone 'Thermal reliefs for PTH' or pad override).",
                      refs=[i["pad"].split(".")[0] for i in items], nets=[i["net"] for i in items], items=items)


@check("ASM-POLARITY", "Orientation consistency of polarized parts")
def c_polarity(x: Ctx):
    groups: Dict[Tuple[str, str], Dict[int, List[str]]] = {}
    for fp in x.comps:
        if not C.is_polarized(fp):
            continue
        kind = "diode" if C.is_diode(fp) else "polarized capacitor"
        ang = int(round(fp.angle)) % 360
        groups.setdefault((kind, fp.side), {}).setdefault(ang, []).append(fp.ref)
    for (kind, side), angs in groups.items():
        if len(angs) > 2:
            items = [{"rotation": a, "refs": sorted(r, key=C.natural_key)} for a, r in sorted(angs.items())]
            yield Finding("ASM-POLARITY", "info", "assembly", "%ss on the %s side use %d different orientations" % (kind.capitalize(), side, len(angs)),
                          "Mixed orientations of polarized parts increase placement and visual-inspection errors.",
                          "Align polarized parts to one or two orientations and mark polarity clearly on silkscreen.",
                          refs=[r for v in angs.values() for r in v], items=items)


@check("ASM-BOTTOM-HEAVY", "Heavy parts on the bottom side")
def c_bottom_heavy(x: Ctx):
    items = []
    for fp in x.comps:
        if fp.side != "bottom" or not fp.is_smd or fp.dnp:
            continue
        cb = fp.courtyard_local()
        area = cb.w * cb.h if cb.valid else 0
        h, _ = x.d.height_of(fp)
        if area > 150 or h > 5.0:
            items.append({"ref": fp.ref, "area_mm2": _r(area, 1), "height_mm": _r(h, 1)})
    if items:
        yield Finding("ASM-BOTTOM-HEAVY", "minor", "assembly", "%d large/heavy SMD part(s) on the bottom side" % len(items),
                      "In double-sided reflow, heavy bottom parts can drop or shift during the second pass.",
                      "Move them to the top side or specify adhesive dots for the bottom side.",
                      refs=[i["ref"] for i in items], items=items)


@check("ASM-PROCESS", "Assembly process summary")
def c_process(x: Ctx):
    top = len(_smd_parts(x, "top"))
    bot = len(_smd_parts(x, "bottom"))
    tht = [fp.ref for fp in x.comps if fp.is_tht and not fp.dnp and not C.is_mounting_hole(fp)]
    steps = []
    if top:
        steps.append("SMT top (%d parts)" % top)
    if bot:
        steps.append("SMT bottom (%d parts)" % bot)
    if tht:
        steps.append("THT (%d parts: wave, selective or hand soldering)" % len(tht))
    if bot and tht:
        sev, extra = "minor", " Bottom SMD + THT usually needs selective soldering or a wave pallet."
    else:
        sev, extra = "info", ""
    yield Finding("ASM-PROCESS", sev, "assembly", "Assembly process: " + " + ".join(steps or ["no parts"]),
                  "Each extra process step adds cost and handling (handling = flex & ESD exposure)." + extra,
                  "The generated solder jig holds the THT parts for hand soldering." if tht else "",
                  refs=tht, data={"smd_top": top, "smd_bottom": bot, "tht": len(tht)})


# =========================================================================== #
# Reliability / life cycle
# =========================================================================== #
@check("REL-ELCAP-HEAT", "Electrolytic capacitors close to heat sources")
def c_elcap_heat(x: Ctx):
    dist = x.a["heat_distance_mm"]
    heat = [(fp, C.heat_source_reason(fp)) for fp in x.comps]
    heat = [(fp, r) for fp, r in heat if r]
    if not heat:
        return
    items = []
    for cap in x.comps:
        if not C.is_electrolytic(cap):
            continue
        cb = cap.courtyard_bbox()
        for hs, reason in heat:
            d = cb.distance_to(hs.courtyard_bbox())
            if d < dist:
                items.append({"ref": cap.ref, "heat_source": hs.ref, "reason": reason, "distance_mm": _r(d, 1)})
    if items:
        yield Finding("REL-ELCAP-HEAT", "major", "reliability", "%d electrolytic capacitor(s) within %.0f mm of a heat source" % (len({i["ref"] for i in items}), dist),
                      "Electrolytic life roughly halves for every 10 C rise (Arrhenius); electrolyte dries out and ESR rises, a "
                      "classic end-of-life failure of power supplies.",
                      "Move the capacitors away from regulators/MOSFETs/inductors, use 105 C long-life series or polymer capacitors.",
                      refs=[i["ref"] for i in items] + [i["heat_source"] for i in items], items=items)


@check("REL-TALL-PARTS", "Tall or heavy parts needing mechanical support")
def c_tall(x: Ctx):
    lim = x.a["tall_component_mm"]
    items = []
    for fp in x.comps:
        if fp.dnp:
            continue
        h, src = x.d.height_of(fp)
        big_cap = C.is_electrolytic(fp) and fp.courtyard_local().w >= 10
        if h >= lim or big_cap:
            items.append({"ref": fp.ref, "height_mm": _r(h, 1), "height_source": src})
    if items:
        yield Finding("REL-TALL-PARTS", "minor", "reliability", "%d tall/heavy part(s) may need staking" % len(items),
                      "Under vibration and shock, tall parts lever on their solder joints and leads fatigue.",
                      "Glue/stake them to the board (RTV or hot melt), add clamps, or use lower-profile parts.",
                      refs=[i["ref"] for i in items], items=items)


@check("REL-SMD-CONNECTOR", "Connectors held only by SMD pads")
def c_smd_conn(x: Ctx):
    items = []
    for fp in x.comps:
        if not C.is_connector(fp) or not fp.pads:
            continue
        has_anchor = any(p.is_tht or p.is_npth for p in fp.pads)
        if has_anchor:
            continue
        ext = C.is_external_connector(fp)
        items.append({"ref": fp.ref, "footprint": fp.name, "external": ext, "severity": "major" if ext else "minor"})
    if items:
        yield Finding("REL-SMD-CONNECTOR", _sev_max(items), "reliability", "%d connector(s) without through-hole anchors" % len(items),
                      "Mating forces and cable strain go straight into SMD solder joints; pads rip off after repeated use.",
                      "Use connector variants with through-hole shell tabs/pegs, or add an enclosure strain relief.",
                      refs=[i["ref"] for i in items], items=items)


@check("REL-MOUNTING", "Mounting holes and flex-sensitive parts near them")
def c_mounting(x: Ctx):
    holes = [fp for fp in x.comps if C.is_mounting_hole(fp)]
    bb = x.b.outline_bbox()
    area = bb.w * bb.h
    if not holes and area > x.a["large_board_mm2"]:
        yield Finding("REL-MOUNTING", "minor", "reliability", "No mounting holes on a %.0f x %.0f mm board" % (bb.w, bb.h),
                      "Unsupported boards flex and vibrate; the fixture also needs tooling holes for alignment.",
                      "Add >= 3 mounting holes (they double as test-fixture guide-pin holes).")
        return
    lim = x.a["mounting_hole_flex_mm"]
    items = []
    for h in holes:
        hb = h.courtyard_bbox()
        for fp in x.comps:
            if fp is h or not fp.is_smd:
                continue
            sensitive = C.is_mlcc(fp) or "BGA" in fp.name.upper() or "QFN" in fp.name.upper()
            if sensitive and hb.distance_to(fp.courtyard_bbox()) < lim:
                items.append({"ref": fp.ref, "hole": h.ref})
    if items:
        yield Finding("REL-MOUNTING", "minor", "reliability", "%d flex-sensitive part(s) within %.0f mm of mounting holes" % (len(items), lim),
                      "Screw torque bends the board locally and cracks MLCCs / BGA joints.",
                      "Keep MLCCs and area-array packages away from screws or use standoffs with washers.",
                      refs=[i["ref"] for i in items], items=items)


@check("REL-POWER-TRACKS", "Track width on power nets (current capacity)")
def c_power_tracks(x: Ctx):
    minw = x.a["power_min_track_mm"]
    rise = x.a["temperature_rise_c"]
    cu = x.b.copper_thickness_mm or 0.035
    per_net: Dict[str, float] = {}
    for t in x.b.tracks:
        if x.d.net_kind(t.net) in ("power", "ground"):
            per_net[t.net] = min(per_net.get(t.net, math.inf), t.width)
    items, over = [], []
    for net, w in sorted(per_net.items()):
        cap = ipc2221_current(w, cu, rise, external=True)
        spec = x.d.net_spec(net)
        need = spec.get("max_current_a")
        if need and cap < float(need):
            over.append({"net": net, "min_width_mm": _r(w), "capacity_a": _r(cap, 2), "required_a": need})
        elif w < minw:
            items.append({"net": net, "min_width_mm": _r(w), "capacity_a": _r(cap, 2)})
    if over:
        yield Finding("REL-POWER-TRACKS", "major", "electrical", "%d power net(s) narrower than their current needs" % len(over),
                      "IPC-2221 estimate at %.0f C rise with %.0f um copper." % (rise, cu * 1000),
                      "Widen the narrowest segments or use pours/planes.", nets=[i["net"] for i in over], items=over)
    if items:
        yield Finding("REL-POWER-TRACKS", "minor", "electrical", "%d power net(s) contain tracks narrower than %.2f mm" % (len(items), minw),
                      "Thin power tracks drop voltage and heat up; capacity shown is IPC-2221 at %.0f C rise (%.0f um Cu). "
                      "Set nets.<name>.max_current_a in the config to turn this into a hard check." % (rise, cu * 1000),
                      "Widen power tracks where space allows.", nets=[i["net"] for i in items], items=items)


def _net_v(x: Ctx, net: str) -> Optional[float]:
    s = x.d.net_spec(net)
    if s.get("nominal_v") is not None:
        return float(s["nominal_v"])
    if s.get("max_v") is not None:
        return float(s["max_v"])
    return None


@check("REL-CAP-VOLTAGE", "Capacitor voltage derating")
def c_cap_voltage(x: Ctx):
    der = x.a["cap_derating"]
    unknown, over = [], []
    for fp in x.comps:
        if not C.is_capacitor(fp) or len(fp.pads) < 2 or fp.dnp:
            continue
        nets = [p.net for p in fp.pads if p.net]
        vs = [_net_v(x, n) for n in nets]
        if len(vs) < 2 or any(v is None for v in vs[:2]):
            continue
        work = abs(vs[0] - vs[1])
        if work <= 0:
            continue
        rated, src = C.rated_voltage(fp)
        if rated is None:
            unknown.append(fp.ref)
            continue
        kind = "electrolytic" if C.is_electrolytic(fp) else "tantalum" if C.is_tantalum(fp) else \
            "ceramic" if C.is_mlcc(fp) else "other"
        lim = float(der.get(kind, 0.8))
        ratio = work / rated
        if ratio > 1.0:
            sev = "critical"
        elif ratio > lim:
            sev = "major" if kind in ("electrolytic", "tantalum") else "minor"
        else:
            continue
        over.append({"ref": fp.ref, "kind": kind, "working_v": _r(work, 2), "rated_v": rated, "usage_pct": int(ratio * 100),
                     "limit_pct": int(lim * 100), "severity": sev})
    if over:
        yield Finding("REL-CAP-VOLTAGE", _sev_max(over), "reliability", "%d capacitor(s) exceed voltage derating" % len(over),
                      "Over-stressed capacitors fail early: tantalums short (and burn), electrolytics wear out, class-II ceramics "
                      "lose most of their capacitance under DC bias.",
                      "Pick parts with a higher voltage rating (ceramic >= 2x, tantalum >= 2x, electrolytic >= 1.25x working voltage).",
                      refs=[i["ref"] for i in over], items=over)
    if unknown:
        yield Finding("DOC-CAP-RATING", "minor", "documentation", "%d capacitor(s) on known rails have no voltage rating" % len(unknown),
                      "Without a rating the derating cannot be verified and purchasing may substitute a low-voltage part.",
                      "Add a 'Voltage' field (e.g. 16V) to the capacitor symbols.", refs=unknown)


@check("REL-RES-POWER", "Resistor power dissipation")
def c_res_power(x: Ctx):
    der = x.a["resistor_derating"]
    items = []
    for fp in x.comps:
        if not C.is_resistor(fp) or len(fp.pads) != 2 or fp.dnp:
            continue
        r = C.parse_si(fp.value)
        v1, v2 = _net_v(x, fp.pads[0].net), _net_v(x, fp.pads[1].net)
        if r is None or v1 is None or v2 is None:
            continue
        dv = abs(v1 - v2)
        if dv == 0:
            continue
        if r == 0:
            items.append({"ref": fp.ref, "value": fp.value, "volts": _r(dv, 2), "severity": "critical",
                          "note": "0 ohm between rails %s and %s" % (fp.pads[0].net, fp.pads[1].net)})
            continue
        pw = dv * dv / r
        rated, src = C.rated_power(fp)
        if not rated:
            continue
        if pw > rated:
            sev = "critical"
        elif pw > der * rated:
            sev = "major"
        else:
            continue
        items.append({"ref": fp.ref, "value": fp.value, "power_w": _r(pw, 3), "rated_w": rated, "rating_source": src, "severity": sev})
    if items:
        yield Finding("REL-RES-POWER", _sev_max(items), "reliability", "%d resistor(s) over their derated power" % len(items),
                      "Computed from the rail voltages across the resistor (P = V^2/R).",
                      "Use a larger package/higher rating, or a higher resistance.", refs=[i["ref"] for i in items], items=items)


@check("ELEC-DECOUPLING", "Decoupling capacitors close to IC power pins")
def c_decoupling(x: Ctx):
    lim = x.a["decoupling_max_mm"]
    cap_pads: Dict[str, List[Pad]] = {}
    for fp in x.comps:
        if C.is_capacitor(fp) and not fp.dnp:
            for p in fp.pads:
                if p.net:
                    cap_pads.setdefault(p.net, []).append(p)
    items = []
    for fp in x.comps:
        if not C.is_ic(fp) or fp.dnp:
            continue
        worst: Dict[str, float] = {}
        for p in fp.pads:
            if not p.net or C.is_unconnected(p.net):
                continue
            if p.pintype != "power_in" and not (not p.pintype and x.d.net_kind(p.net) == "power"):
                continue
            if x.d.net_kind(p.net) == "ground":
                continue
            caps = cap_pads.get(p.net, [])
            d = min((math.hypot(c.x - p.x, c.y - p.y) for c in caps), default=math.inf)
            worst[p.net] = max(worst.get(p.net, 0.0), d)
        for net, d in worst.items():
            if d > lim:
                sev = "major" if d == math.inf or d > 2 * lim else "minor"
                items.append({"ref": fp.ref, "net": net, "nearest_cap_mm": None if d == math.inf else _r(d, 1), "severity": sev})
    if items:
        yield Finding("ELEC-DECOUPLING", _sev_max(items), "electrical", "%d IC power net(s) lack a nearby decoupling capacitor" % len(items),
                      "Distant decoupling adds loop inductance: supply noise, brown-out resets and EMC failures that show up "
                      "unit-to-unit and over temperature.",
                      "Place a 100 nF capacitor within %.0f mm of every power pin (plus bulk capacitance per rail)." % lim,
                      refs=[i["ref"] for i in items], nets=[i["net"] for i in items], items=items)


@check("ELEC-ESD", "ESD protection on external connectors")
def c_esd(x: Ctx):
    esd_nets = set()
    for fp in x.comps:
        if C.is_esd_device(fp):
            esd_nets.update(fp.nets())
    items = []
    for fp in x.comps:
        if not C.is_external_connector(fp):
            continue
        unprot = []
        for p in fp.pads:
            n = p.net
            if not n or C.is_unconnected(n) or x.d.net_kind(n) in ("ground", "power"):
                continue
            if len(x.net_pads.get(n, [])) < 2:
                continue
            if n not in esd_nets:
                unprot.append(n)
        if unprot:
            items.append({"ref": fp.ref, "nets": sorted(set(unprot), key=C.natural_key)})
    if items:
        yield Finding("ELEC-ESD", "minor", "electrical", "%d external connector(s) with unprotected signal lines" % len(items),
                      "ESD at user-accessible connectors is a top cause of field returns (latent damage, not caught by test).",
                      "Add TVS/ESD diodes close to the connector (e.g. USBLC6 for USB, low-capacitance arrays for data lines).",
                      refs=[i["ref"] for i in items], nets=[n for i in items for n in i["nets"]], items=items)


@check("ELEC-FLOATING", "Unconnected input and power pins")
def c_floating(x: Ctx):
    items = []
    for fp in x.comps:
        for p in fp.pads:
            if p.pintype not in ("input", "power_in"):
                continue
            single = not p.net or C.is_unconnected(p.net) or len(x.net_pads.get(p.net, [])) < 2
            if single:
                items.append({"pad": p.name, "pinfunction": p.pinfunction, "pintype": p.pintype,
                              "severity": "critical" if p.pintype == "power_in" else "major"})
    if items:
        yield Finding("ELEC-FLOATING", _sev_max(items), "electrical", "%d input/power pin(s) left unconnected" % len(items),
                      "Floating CMOS inputs oscillate, draw extra current and cause random behaviour; unconnected power pins "
                      "leave parts of the IC unpowered.",
                      "Tie unused inputs to a defined level (per datasheet) and connect all power pins.",
                      refs=sorted({i["pad"].split(".")[0] for i in items}), items=items)


@check("REL-THERMAL-PAD", "Exposed thermal pads without thermal vias")
def c_thermal_pad(x: Ctx):
    items = []
    for fp in x.comps:
        smd = [p for p in fp.pads if p.kind == "smd" and p.has_copper]
        if len(smd) < 5:
            continue
        big = max(smd, key=lambda p: p.w * p.h)
        others = sorted(p.w * p.h for p in smd if p is not big)
        if not others or big.w * big.h < 4 * others[len(others) // 2]:
            continue
        if math.hypot(big.local_x, big.local_y) > 0.6:
            continue
        nvia = sum(1 for v in x.b.vias if big.contains(v.x, v.y))
        nvia += sum(1 for p in fp.pads if p.is_tht and big.contains(p.x, p.y))
        if nvia == 0 and big.net:
            items.append({"ref": fp.ref, "pad": big.number, "net": big.net, "pad_mm": "%.1fx%.1f" % (big.w, big.h)})
    if items:
        yield Finding("REL-THERMAL-PAD", "minor", "reliability", "%d exposed pad(s) without thermal vias" % len(items),
                      "Without vias the exposed pad cannot spread heat; the part runs hot and ages faster.",
                      "Add an array of 0.3 mm vias (tented or filled on the bottom to limit solder voids).",
                      refs=[i["ref"] for i in items], items=items)


# =========================================================================== #
# Documentation / sync / testability
# =========================================================================== #
@check("DOC-MPN", "Manufacturer part numbers in the BOM")
def c_mpn(x: Ctx):
    parts = [fp for fp in x.comps if not fp.exclude_from_bom and not fp.board_only and not C.is_fiducial(fp)
             and not C.is_mounting_hole(fp) and not C.is_testpoint(fp) and not fp.dnp]
    if not parts:
        return
    missing = [fp.ref for fp in parts if not C.mpn_of(fp)]
    if not missing:
        return
    if len(missing) == len(parts):
        yield Finding("DOC-MPN", "info", "documentation", "No manufacturer part numbers in the schematic",
                      "The BOM relies on generic values; the assembler chooses parts.",
                      "Add an MPN field (or LCSC/Digikey numbers) to control what gets assembled.", count=len(missing))
    else:
        yield Finding("DOC-MPN", "minor", "documentation", "%d of %d part(s) have no part number" % (len(missing), len(parts)),
                      "Unspecified parts get substituted, which changes ratings, footprints and long-term reliability.",
                      "Add MPN fields for the remaining parts.", refs=missing)


@check("DOC-SCH-SYNC", "Schematic and board are in sync")
def c_sync(x: Ctx):
    if not x.d.sch:
        return
    sch_refs = {r for r, s in x.d.sch.by_ref.items() if s.on_board and not s.is_power and not r.startswith("#")}
    pcb_refs = {fp.ref for fp in x.comps if not fp.board_only}
    missing_pcb = sorted(sch_refs - pcb_refs, key=C.natural_key)
    missing_sch = sorted(pcb_refs - sch_refs, key=C.natural_key)
    mism = []
    for ref in sorted(sch_refs & pcb_refs, key=C.natural_key):
        s = x.d.sch.by_ref[ref]
        fp = x.b.by_ref[ref]
        if s.value and fp.value and s.value != fp.value:
            mism.append({"ref": ref, "schematic": s.value, "board": fp.value})
    if missing_pcb:
        yield Finding("DOC-SCH-SYNC", "major", "documentation", "%d schematic part(s) missing on the board" % len(missing_pcb),
                      "The board was not updated from the schematic.", "Run 'Update PCB from Schematic' (F8).", refs=missing_pcb)
    if missing_sch:
        yield Finding("DOC-SCH-SYNC", "major", "documentation", "%d board footprint(s) not in the schematic" % len(missing_sch),
                      "Extra footprints are not in the BOM/netlist (unless intended as board-only).",
                      "Mark them 'Not in schematic' (board only) or add symbols.", refs=missing_sch)
    if mism:
        yield Finding("DOC-SCH-SYNC", "major", "documentation", "%d value mismatch(es) between schematic and board" % len(mism),
                      "Assembly files come from the board, the BOM from the schematic: they will disagree.",
                      "Update the board from the schematic.", refs=[i["ref"] for i in mism], items=mism)


@check("DOC-DNP", "Do-not-populate parts")
def c_dnp(x: Ctx):
    dnp = [fp.ref for fp in x.comps if fp.dnp]
    if dnp:
        yield Finding("DOC-DNP", "info", "documentation", "%d part(s) marked DNP" % len(dnp),
                      "These must be absent on a good unit; they are listed in the visual inspection step.", refs=dnp)


@check("DOC-REVISION", "Board revision and identification")
def c_revision(x: Ctx):
    tb = x.b.title_block
    if not tb.get("rev"):
        yield Finding("DOC-REVISION", "minor", "documentation", "No revision in the board title block",
                      "Test results, fixtures and field returns cannot be traced to a board revision.",
                      "Set the revision in Board Setup > Title block and print it on silkscreen (${REVISION}).")


@check("TEST-TESTPOINTS", "Dedicated test points")
def c_testpoints(x: Ctx):
    tps = [fp for fp in x.comps if C.is_testpoint(fp)]
    pad_tps = [p for p in x.b.all_pads() if "pad_prop_testpoint" in p.props]
    if not tps and not pad_tps:
        yield Finding("TEST-TESTPOINTS", "minor", "testability", "No dedicated test points",
                      "The fixture has to probe component pads and THT leads; probes on solder joints wear tips and can "
                      "hide cracked joints.",
                      "Add >= 1 mm test pads on the probe side for every rail and key signal (see the test plan coverage).")


# =========================================================================== #
# Runner
# =========================================================================== #
def run_analysis(design: Design, include_kicad: Optional[bool] = None) -> List[Finding]:
    x = Ctx(design)
    findings: List[Finding] = []
    for cid, _desc, fn in CHECKS:
        if design.ignored(cid):
            continue
        try:
            for f in fn(x):
                if design.ignored(f.id):
                    continue
                if f.refs and design.ignored(f.id, f.refs):
                    continue
                ig = set(design.cfg["analysis"].get("ignore_refs", []))
                if ig and f.refs:
                    f.refs = [r for r in f.refs if r not in ig]
                findings.append(f)
        except Exception as exc:  # a failing heuristic must never block the export
            findings.append(Finding(cid, "info", "documentation", "Check %s could not run" % cid, repr(exc)))
    if include_kicad is None:
        include_kicad = bool(design.cfg["analysis"].get("run_kicad_drc"))
    if include_kicad:
        from .kicad_cli import kicad_findings
        findings.extend(kicad_findings(design))
    apply_waivers(design, findings)
    sort_findings(findings)
    return findings


def apply_waivers(design: Design, findings: List[Finding]) -> None:
    for w in design.cfg["analysis"].get("waivers") or []:
        wid, wrefs = w.get("id"), set(w.get("refs") or [])
        for f in findings:
            if f.id != wid or f.waived:
                continue
            if not wrefs or (f.refs and set(f.refs) <= wrefs):
                f.waived = w.get("reason", "waived")
            elif f.refs:
                f.refs = [r for r in f.refs if r not in wrefs]
                f.items = [i for i in f.items if i.get("ref") not in wrefs]


def sort_findings(findings: List[Finding]) -> None:
    findings.sort(key=lambda f: (bool(f.waived), SEV_RANK.get(f.severity, 9),
                                 CATEGORIES.index(f.category) if f.category in CATEGORIES else 99, f.id))
    for i, f in enumerate(findings, 1):
        f.uid = "F%03d" % i


def summarize(findings: List[Finding]) -> Dict[str, Any]:
    counts = {s: 0 for s in SEVERITIES}
    waived = 0
    for f in findings:
        if f.waived:
            waived += 1
            continue
        counts[f.severity] = counts.get(f.severity, 0) + 1
    score = max(0, 100 - 15 * counts["critical"] - 6 * counts["major"] - 2 * counts["minor"])
    verdict = "not ready" if counts["critical"] else ("needs review" if counts["major"] else "ready")
    return {"counts": counts, "score": score, "verdict": verdict, "total": len(findings) - waived, "waived": waived}


def list_checks() -> List[Dict[str, str]]:
    return [{"id": cid, "description": desc} for cid, desc, _ in CHECKS]
