"""Board nest and clamp on every fixture site: the board slides in on rails and a toggle clamp presses it down.

Every board of a batch gets its own nest and clamp, so the boards are loaded, clamped and taken off one at a time.

  nest     A printed carrier floating on four sprung M3 standoffs. Two rails guide the board's edges and an end
           stop at the rear locates it. The rails run on past the board's front edge (the lead-in, with a flared
           mouth): lay the board there and slide it back to the stop. Ledges under the board's edges carry it
           ABOVE the pogo tips while it slides, so the tips never scrape the pads; the middle of the nest is open
           for the pins and the parts on the probed side.
  clamp    A vertical toggle clamp (a bought part, e.g. a GH-101-A) on a printed adapter behind the end stop, with
           a printed press plate on its spindle. The plate's push fingers press on bare spots of the board's other
           side, next to the probes. Closing the clamp pushes board and nest down by the nest's travel; the pogo
           pins come up through the nest and are compressed by their working stroke.

Boards whose underside parts would sweep over a pogo pin (or through a cut-out) while sliding get the "drop" style
instead: four walls with chamfered tops, the board is lowered straight in and still lands above the tips.

Coordinates. The nest is designed in a frame of its own: s along the slide (the board moves +s from the entry to the
end stop), t across it, origin at the board's centre on site 1. ENTRY maps (s, t) to fixture board coordinates.
Heights z are above the fixture board's top face:
    z_work   board underside when clamped = the test plan's probe clearance
    z_tip    pogo tips, uncompressed = z_work + PRELOAD
    z_rest   board underside while loading = z_tip + gap
"""
from __future__ import annotations

import math
import os
from typing import Any, Dict, List, Optional, Tuple

from .geometry import BBox, pt_seg_dist
from .project import Design

PRELOAD = 1.5          # pogo working stroke at the clamped height (the fixture's pogo models are drawn this way)
LEAD_MM = 1.5          # leads of through-hole parts below the board, after trimming
FLARE = 1.5            # the lead-in mouth opens this much per side
STANDOFFS = (6, 8, 10, 12, 15, 20, 25, 30, 35, 40)   # M3 hex standoffs (female-female), mm
STANDOFF_AF = 5.5
BORE_R = 3.6           # the nest's bushing slides on the standoff (corners 6.35 mm)
BOSS_R = 5.75
POST_OUT = 2.5         # standoff centre beyond the rail's outer face
SLOT_W = 6.5           # press plate: slot for the clamp's spindle (M5 or M6, a nut on each side)
PLATE_T = 5.0
ADAPTER = (40.0, 38.0)  # clamp adapter footprint (along s, across t)
ADAPTER_GAP = 2.0
ADAPTER_SCREWS = ((-15.0, -15.0), (15.0, -15.0), (-15.0, 15.0), (15.0, 15.0))
SLOT_ARM = 4.0         # the adapter's cross slots: the clamp's screws may sit 4 mm off the nominal pattern
ARM_PIVOT = (-4.0, 20.0)   # generic clamp drawing: arm pivot (s from adapter centre, z above adapter top)
REACH_MAX = 55.0
COLORS = {"nest": (0.16, 0.62, 0.66), "plate": (0.18, 0.55, 0.60), "adapter": (0.30, 0.32, 0.36),
          "steel": (0.62, 0.64, 0.68), "handle": (0.80, 0.12, 0.10), "spring": (0.75, 0.75, 0.78)}

PART_FILES = {"nest": "nest", "press": "press-plate", "adapter": "clamp-adapter"}

# entry side -> (s unit vector, t unit vector) in fixture coordinates (y down); every frame is a proper rotation
ENTRY = {"front": ((0.0, -1.0), (1.0, 0.0)), "back": ((0.0, 1.0), (-1.0, 0.0)),
         "left": ((1.0, 0.0), (0.0, 1.0)), "right": ((-1.0, 0.0), (0.0, -1.0))}


class Frame:
    """Nest frame (s, t) <-> fixture board coordinates (x, y), origin at ``c``."""

    def __init__(self, entry: str, c: Tuple[float, float]):
        self.entry = entry if entry in ENTRY else "front"
        (self.sx, self.sy), (self.tx, self.ty) = ENTRY[self.entry]
        self.cx, self.cy = c

    def fix(self, s: float, t: float, dx: float = 0.0, dy: float = 0.0) -> Tuple[float, float]:
        return (round(self.cx + s * self.sx + t * self.tx + dx, 4), round(self.cy + s * self.sy + t * self.ty + dy, 4))

    def loc(self, x: float, y: float) -> Tuple[float, float]:
        ddx, ddy = x - self.cx, y - self.cy
        return ddx * self.sx + ddy * self.sy, ddx * self.tx + ddy * self.ty

    def box(self, b: BBox) -> Tuple[float, float, float, float]:
        """A fixture-frame box in nest coordinates (s0, t0, s1, t1); the frames differ by quarter turns."""
        pts = [self.loc(x, y) for x, y in b.corners()]
        return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))

    def model(self, s: float, t: float) -> Tuple[float, float]:
        """KiCad 3D-model coordinates (x right, y up) of a nest point, relative to the frame origin."""
        return s * self.sx + t * self.tx, -(s * self.sy + t * self.ty)


def applies(design: Design, plan: Dict[str, Any]) -> Tuple[bool, str]:
    nc = (design.cfg.get("fixture") or {}).get("nest") or {}
    if not nc.get("enabled", True):
        return False, "fixture.nest.enabled is false"
    if plan.get("probe_side") == "both":
        return False, "probed from both sides: the clamshell's top plate presses the board"
    if (design.cfg.get("fixture") or {}).get("outline", "rect") != "rect":
        return False, "fixture.outline is not rect: no room around the board"
    return True, ""


def plan_nest(design: Design, plan: Dict[str, Any]) -> Dict[str, Any]:
    """Geometry of site 1's nest and clamp (fixture coordinates); the fixture repeats it per site. No files."""
    ok, why = applies(design, plan)
    if not ok:
        return {"enabled": False, "reason": why}
    nc = dict((design.cfg.get("fixture") or {}).get("nest") or {})
    b = design.board
    bb = b.outline_bbox()
    mirror = plan.get("probe_side") == "top"

    def mx(x: float) -> float:
        return 2 * bb.cx - x if mirror else x

    fr = Frame(str(nc.get("entry", "front")), (bb.cx, bb.cy))
    along_y = abs(fr.sy) > 0.5
    Ls, Lt = (bb.h, bb.w) if along_y else (bb.w, bb.h)
    t_dut = float(getattr(b, "thickness", 1.6) or 1.6)
    c = float(nc.get("clearance_mm", 0.25))
    w_rail = float(nc.get("rail_mm", 4.0))
    ledge = float(nc.get("ledge_mm", 2.0))
    above = float(nc.get("rail_above_mm", 1.5))
    lead = float(nc.get("lead_in_mm", 10.0))
    tip_gap = float(nc.get("tip_gap_mm", 1.0))
    solid = float(nc.get("spring_solid_mm", 3.5))
    free = float(nc.get("spring_free_mm", 10.0))
    finger_d = float(nc.get("finger_mm", 4.0))
    clear = float(plan["pogo"].get("probe_clearance_mm", 6.0))
    probe_side = plan.get("probe_side", "bottom")
    warnings: List[str] = []
    segs = [(mx(s_[0]), s_[1], mx(s_[2]), s_[3]) for s_ in b.edge_segs] or \
        [(bb.x0, bb.y0, bb.x1, bb.y0), (bb.x1, bb.y0, bb.x1, bb.y1), (bb.x1, bb.y1, bb.x0, bb.y1), (bb.x0, bb.y1, bb.x0, bb.y0)]
    pin_r = float(plan["pogo"].get("pad_mm") or plan["pogo"]["drill_mm"]) / 2.0
    pins = [fr.loc(mx(p["x"]), p["y"]) for p in plan.get("probes") or []]
    # what hangs below the board (towards the pins) and what stands on its other side (under the press plate)
    under: List[Tuple[Tuple[float, float, float, float], float, str]] = []
    over: List[Tuple[Tuple[float, float, float, float], float, str]] = []
    tall = {t_["ref"] for t_ in plan.get("fixture_keepouts") or []}
    for fp in design.components():
        if getattr(fp, "dnp", False):
            continue
        cb = fp.courtyard_bbox()
        x0, x1 = sorted((mx(cb.x0), mx(cb.x1)))
        box = fr.box(BBox(x0, cb.y0, x1, cb.y1))
        h, _src = design.height_of(fp)
        (under if fp.side == probe_side else over).append((box, float(h), fp.ref))
        for pd in fp.pads:                      # leads of through-hole parts stick out on the far side
            if pd.drill_w > 0 and not pd.is_npth:
                r = max(pd.w, pd.h, pd.drill_w) / 2.0
                px, py = fr.loc(mx(pd.x), pd.y)
                lb = (px - r, py - r, px + r, py + r)
                (over if fp.side == probe_side else under).append((lb, LEAD_MM, fp.ref))
    # ---- style: slide in on the ledges, or drop in from above --------------------------------------------------
    need, blocker, tall_hit = 0.0, "", ""
    for (s0, t0, s1, t1), h, ref in under:
        sw = (s0 - lead, t0 - pin_r - 0.2, s1, t1 + pin_r + 0.2)       # swept on the way in (and where it ends)
        hit = any(sw[0] - pin_r - 0.2 <= ps <= sw[2] + pin_r + 0.2 and sw[1] <= pt <= sw[3] for ps, pt in pins)
        if not hit:
            continue
        if ref in tall or h >= clear - 0.5:
            tall_hit = ref
        elif h + 0.3 > need:
            need, blocker = h + 0.3, ref
    style = str(nc.get("style", "auto"))
    if style not in ("slide", "drop"):
        style = "drop" if (tall_hit or need > 4.0) else "slide"
    elif style == "slide" and (tall_hit or need > 4.0):
        warnings.append("nest: %s would sweep over a pogo pin while the board slides in; use fixture.nest.style "
                        "drop" % (tall_hit or blocker))
    if style == "drop":
        lead, gap = 0.0, tip_gap
    else:
        gap = max(tip_gap, need)
    z_work = clear
    z_tip = clear + PRELOAD
    z_rest = z_tip + gap
    travel = z_rest - z_work
    T = round(z_work - (solid + 0.5), 2)                       # ledge level above the nest's flat underside
    if T < 2.0:
        warnings.append("nest: the probe clearance (%.1f mm) leaves the nest %.1f mm under the board above its springs "
                        "(%.1f mm solid); raise testplan.probe_clearance_mm to %.1f" % (clear, max(T, 0.0), solid,
                                                                                         solid + 2.5))
        T = 2.0
    bottom_work = z_work - T
    bottom_rest = bottom_work + travel
    wall = round(T + t_dut + above, 2)
    so_len = next((L for L in STANDOFFS if L >= bottom_rest + wall + 0.5), STANDOFFS[-1])
    boss = round(so_len - bottom_rest, 2)
    if free < bottom_rest + 0.5:
        warnings.append("nest: springs of %.0f mm free length do not lift the nest to %.1f mm; use longer springs "
                        "(fixture.nest.spring_free_mm)" % (free, bottom_rest))
    # ---- the nest's parts in its own frame ---------------------------------------------------------------------
    s_lo = -Ls / 2.0 - lead - (c + w_rail if style == "drop" else 0.0)
    s_stop = Ls / 2.0 + c
    s_hi = s_stop + w_rail
    t_in = Lt / 2.0 + c
    t_out = t_in + w_rail
    t_post = t_out + POST_OUT
    posts = [(s_lo + BOSS_R, -t_post), (s_lo + BOSS_R, t_post), (s_hi - BOSS_R, -t_post), (s_hi - BOSS_R, t_post)]
    # ---- the clamp: adapter behind the end stop, press plate over the board ------------------------------------
    clamp = str(nc.get("clamp", "toggle"))
    a_s = s_hi + ADAPTER_GAP + ADAPTER[0] / 2.0
    fingers, finger_len, windows = _fingers(design, fr, mx, segs, over, pins, Ls, Lt, finger_d, warnings)
    plate_s = (-Ls / 2.0 + 0.5, s_hi)
    plate_t = (-t_out, t_out)
    z_plate_top = z_work + t_dut + finger_len + PLATE_T
    a_h = round(max(z_work + t_dut, z_plate_top - 12.0), 1)      # the clamp's arm clears the plate by 8 mm
    pivot_s = a_s + ARM_PIVOT[0]
    spindle = round(min(max(0.0, pivot_s - REACH_MAX), plate_s[1] - SLOT_W), 2)   # over the board's centre if it reaches
    slot = (round(max(plate_s[0] + SLOT_W, spindle - 12.0), 2), round(min(plate_s[1] - SLOT_W / 2.0 - 1.0, spindle + 12.0), 2))
    holes = [(a_s + ds, dt) for ds, dt in ADAPTER_SCREWS] if clamp == "toggle" else []
    # ---- extents: how far everything reaches beyond the board on each side (fixture frame) ---------------------
    s_end = a_s + ADAPTER[0] / 2.0 if clamp == "toggle" else s_hi
    t_ext = max(t_post + BOSS_R, ADAPTER[1] / 2.0 if clamp == "toggle" else 0.0)
    corners = [fr.fix(s_, t_) for s_ in (s_lo, s_end) for t_ in (-t_ext, t_ext)]
    ext = {"left": round(bb.x0 - min(p[0] for p in corners), 2), "right": round(max(p[0] for p in corners) - bb.x1, 2),
           "top": round(bb.y0 - min(p[1] for p in corners), 2), "bottom": round(max(p[1] for p in corners) - bb.y1, 2)}
    loc = {"Ls": Ls, "Lt": Lt, "s_lo": s_lo, "s_stop": s_stop, "s_hi": s_hi, "t_in": t_in, "t_out": t_out,
           "t_post": t_post, "lead": lead, "ledge": ledge, "c": c, "rail": w_rail, "s_end": s_end, "t_ext": t_ext,
           "adapter_s": a_s, "plate_s": plate_s, "plate_t": plate_t, "slot": slot, "spindle": spindle}
    nest = {
        "enabled": True, "style": style, "entry": fr.entry, "clamp": clamp,
        "frame": {"s": [fr.sx, fr.sy], "t": [fr.tx, fr.ty], "origin": [round(bb.cx, 4), round(bb.cy, 4)]},
        "board_mm": [round(Ls, 2), round(Lt, 2), t_dut], "local": loc,
        "heights": {"work": round(z_work, 2), "tips": round(z_tip, 2), "rest": round(z_rest, 2), "gap": round(gap, 2),
                    "travel": round(travel, 2), "ledge": T, "wall": wall, "boss": boss, "bottom_rest": round(bottom_rest, 2),
                    "bottom_work": round(bottom_work, 2), "standoff": so_len, "adapter": a_h, "finger": finger_len,
                    "plate": PLATE_T, "plate_top_work": round(z_plate_top, 2)},
        "gap_reason": blocker if style == "slide" and gap > tip_gap else "",
        "posts": [list(fr.fix(*p)) for p in posts], "posts_local": [list(p) for p in posts],
        "adapter_holes": [list(fr.fix(*p)) for p in holes], "adapter_local": [list(p) for p in holes],
        "fingers": [list(fr.fix(*p)) for p in fingers], "fingers_local": [list(p) for p in fingers],
        "windows_local": windows, "extents": ext,
        "spring": {"free_mm": free, "solid_mm": solid, "rest_mm": round(bottom_rest, 2), "work_mm": round(bottom_work, 2)},
        "warnings": warnings, "finger_d": finger_d,
        "clamp_holes_mm": [float(v) for v in (nc.get("clamp_holes_mm") or [20.0, 16.0])[:2]],
    }
    nest["outline_local"] = _outline_local(fr, segs)
    nest["under_local"] = [list(u[0]) + [u[1]] for u in under]
    nest["pins_local"] = [list(p) for p in pins]
    nest["pin_r"] = pin_r
    nest["bom"] = bom(nest)
    return nest


def _outline_local(fr: Frame, segs) -> List[List[float]]:
    out = []
    for ax, ay, bx_, by_ in segs:
        a, b_ = fr.loc(ax, ay), fr.loc(bx_, by_)
        out.append([a[0], a[1], b_[0], b_[1]])
    return out


def _fingers(design, fr: Frame, mx, segs, over, pins, Ls, Lt, d, warnings):
    """Push-finger spots on the board's other side: bare board (no part, no lead) next to the probes, plus the
    corners, at least 7 mm apart; the finger length clears the tallest part there."""
    r = d / 2.0
    lsegs = _outline_local(fr, segs)
    blocks = [((s0 - 0.8, t0 - 0.8, s1 + 0.8, t1 + 0.8)) for (s0, t0, s1, t1), _h, _ref in over]
    cand = []
    step = 1.0
    s = -Ls / 2.0 + r + 2.0
    while s <= Ls / 2.0 - r - 2.0 + 1e-6:
        t = -Lt / 2.0 + r + 2.0
        while t <= Lt / 2.0 - r - 2.0 + 1e-6:
            if abs(t) >= SLOT_W / 2.0 + r + 0.5 and _inside(s, t, lsegs) and \
                    min(pt_seg_dist(s, t, *sg) for sg in lsegs) >= r + 1.5 and \
                    not any(b0 - r <= s <= b2 + r and b1 - r <= t <= b3 + r for b0, b1, b2, b3 in blocks):
                cand.append((s, t))
            t += step
        s += step
    targets = [(p, 12.0) for p in pins] + [((sx * (Ls / 2.0 - 4.0), tx * (Lt / 2.0 - 4.0)), 25.0)
                                           for sx in (-1, 1) for tx in (-1, 1)] + [((0.0, 0.0), 25.0)]
    chosen: List[Tuple[float, float]] = []
    for (ps, pt), reach in targets:
        best = None
        for q in cand:
            dd = math.hypot(q[0] - ps, q[1] - pt)
            if dd <= reach and (best is None or dd < best[0]):
                best = (dd, q)
        if best and all(math.hypot(best[1][0] - q[0], best[1][1] - q[1]) >= 7.0 for q in chosen):
            chosen.append(best[1])
        if len(chosen) >= 20:
            break
    if len(chosen) < 3:
        warnings.append("nest: only %d bare spot(s) on the board's other side for the press plate to push on; "
                        "check the press plate before printing" % len(chosen))
    hmax = max([h for _b, h, _ref in over] or [0.0])
    flen = round(min(20.0, max(3.0, hmax + 1.0)), 1)
    windows = [[b0 - 1.0, b1 - 1.0, b2 + 1.0, b3 + 1.0] for (b0, b1, b2, b3), h, _ref in over if h > flen - 0.5]
    return [(round(a, 3), round(b_, 3)) for a, b_ in chosen], flen, windows


def _inside(s: float, t: float, lsegs) -> bool:
    c = False
    for ax, ay, bx_, by_ in lsegs:
        if (ay > t) != (by_ > t) and s < ax + (t - ay) * (bx_ - ax) / (by_ - ay):
            c = not c
    return c


def bom(nest: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Parts for ONE board's nest and clamp."""
    h = nest["heights"]
    out = [{"qty": 1, "part": "nest (print, PETG)", "file": "nest"},
           {"qty": 4, "part": "M3 hex standoff, female-female, %d mm" % h["standoff"]},
           {"qty": 4, "part": "M3 x 6 screw + M3 large washer (DIN 9021, 9 mm), on top of each standoff"},
           {"qty": 4, "part": "M3 x 6 screw + M3 nylon washer, standoff to fixture board (from below)"},
           {"qty": 4, "part": "compression spring, outer 8 mm, wire 0.5 mm, free length %.0f mm (solid %.1f mm or less)"
                              % (nest["spring"]["free_mm"], nest["spring"]["solid_mm"])}]
    if nest["clamp"] == "toggle":
        out += [{"qty": 1, "part": "vertical toggle clamp, about 25 kg, flanged base (e.g. GH-101-A)"},
                {"qty": 1, "part": "clamp adapter (print)", "file": "adapter"},
                {"qty": 1, "part": "press plate (print)", "file": "press"},
                {"qty": 4, "part": "M4 x 12 screw + M4 nut, clamp to adapter (nuts in the adapter's channels)"},
                {"qty": 4, "part": "M3 x 10 screw + M3 nylon washer, adapter to fixture board from below (cuts its own thread)"},
                {"qty": 2, "part": "nut + washer for the clamp's spindle, one each side of the press plate"}]
    return out


# =========================================================================== #
# printable parts (heightmaps, as the solder jigs)
# =========================================================================== #
def _grid(s0: float, t0: float, s1: float, t1: float, res: float) -> Dict[str, Any]:
    nx, ny = int(math.ceil((s1 - s0) / res)), int(math.ceil((t1 - t0) / res))
    return {"s0": s0, "t0": t0, "res": res, "nx": nx, "ny": ny, "H": [[0] * nx for _ in range(ny)]}


def _fill_box(g, s0, t0, s1, t1, h: int, keep_max: bool = False) -> None:
    res = g["res"]
    i0, i1 = max(0, int(math.floor((s0 - g["s0"]) / res + 0.5))), min(g["nx"], int(math.floor((s1 - g["s0"]) / res + 0.5)))
    j0, j1 = max(0, int(math.floor((t0 - g["t0"]) / res + 0.5))), min(g["ny"], int(math.floor((t1 - g["t0"]) / res + 0.5)))
    for j in range(j0, j1):
        row = g["H"][j]
        for i in range(i0, i1):
            row[i] = max(row[i], h) if keep_max else h


def _fill_disc(g, cs, ct, r, h: int, keep_max: bool = False) -> None:
    res = g["res"]
    i0, i1 = max(0, int((cs - r - g["s0"]) / res) - 1), min(g["nx"], int((cs + r - g["s0"]) / res) + 2)
    j0, j1 = max(0, int((ct - r - g["t0"]) / res) - 1), min(g["ny"], int((ct + r - g["t0"]) / res) + 2)
    for j in range(j0, j1):
        tc = g["t0"] + (j + 0.5) * res
        row = g["H"][j]
        for i in range(i0, i1):
            sc = g["s0"] + (i + 0.5) * res
            if (sc - cs) ** 2 + (tc - ct) ** 2 <= r * r:
                row[i] = max(row[i], h) if keep_max else h


def nest_grid(nest: Dict[str, Any], res: float = 0.25) -> Dict[str, Any]:
    """The nest as printed (flat underside down). Heights in 0.01 mm (jig.Q)."""
    from .jig import Q, _inside_grid, _near_grid
    L, H = nest["local"], nest["heights"]
    t_lim = L["t_post"] + BOSS_R + 0.5
    g = _grid(L["s_lo"] - 0.5, -t_lim, L["s_hi"] + 0.5, t_lim, res)
    q = lambda v: int(round(v * Q))           # noqa: E731
    wall, ledge, boss = q(H["wall"]), q(H["ledge"]), q(H["boss"])
    s_front = -L["Ls"] / 2.0
    # rails and end stop (and the front wall when the board drops in)
    _fill_box(g, L["s_lo"], L["t_in"], L["s_hi"], L["t_out"], wall)
    _fill_box(g, L["s_lo"], -L["t_out"], L["s_hi"], -L["t_in"], wall)
    _fill_box(g, L["s_stop"], -L["t_out"], L["s_hi"], L["t_out"], wall)
    if nest["style"] == "drop":
        _fill_box(g, L["s_lo"], -L["t_out"], s_front - L["c"], L["t_out"], wall)
    # ledges: under the board's edge (where nothing passes over them), the lead-in strips, the gap to the rails
    nx, ny = g["nx"], g["ny"]
    segs = [(a - g["s0"], b_ - g["t0"], c_ - g["s0"], d - g["t0"]) for a, b_, c_, d in nest["outline_local"]]
    inside = _inside_grid(segs, nx, ny, res)
    near = _near_grid(segs, nx, ny, res, L["ledge"])
    t_in, s_stop = L["t_in"], L["s_stop"]
    for j in range(ny):
        tc = g["t0"] + (j + 0.5) * res
        row = g["H"][j]
        for i in range(nx):
            if row[i]:
                continue
            sc = g["s0"] + (i + 0.5) * res
            if abs(tc) >= t_in or sc >= s_stop:
                continue
            if (inside[j][i] and near[j][i]) or (not inside[j][i] and sc >= L["s_lo"] and
                                                   (abs(tc) >= t_in - L["ledge"] or near[j][i])):
                row[i] = ledge
    # nothing may stand where a part or a lead hangs below the board on its way in, nor at a pogo pin
    lead = L["lead"]
    for s0, t0, s1, t1, _h in nest["under_local"]:
        _clear_box(g, s0 - lead - 0.4, t0 - 0.4, s1 + 0.4, t1 + 0.4, wall, ledge)
    for ps, pt in nest["pins_local"]:
        _fill_disc(g, ps, pt, nest["pin_r"] + 0.8, 0)
    # the lead-in mouth: the rails' inner faces open up towards the entry, chamfered tops on the inner edge
    if nest["style"] == "slide" and lead > 0:
        flare = min(6.0, lead)
        for j in range(ny):
            tc = abs(g["t0"] + (j + 0.5) * res)
            row = g["H"][j]
            for i in range(nx):
                sc = g["s0"] + (i + 0.5) * res
                if row[i] == wall and sc < L["s_lo"] + flare and \
                        t_in <= tc < t_in + FLARE * (1.0 - (sc - L["s_lo"]) / flare):
                    row[i] = ledge
    _chamfer(g, nest, wall)
    # standoff bosses with their bores
    for ps, pt in nest["posts_local"]:
        _fill_disc(g, ps, pt, BOSS_R, boss, keep_max=True)
    for ps, pt in nest["posts_local"]:
        _fill_disc(g, ps, pt, BORE_R, 0)
    return g


def _clear_box(g, s0, t0, s1, t1, wall: int, ledge: int) -> None:
    res = g["res"]
    i0, i1 = max(0, int(math.floor((s0 - g["s0"]) / res))), min(g["nx"], int(math.ceil((s1 - g["s0"]) / res)))
    j0, j1 = max(0, int(math.floor((t0 - g["t0"]) / res))), min(g["ny"], int(math.ceil((t1 - g["t0"]) / res)))
    for j in range(j0, j1):
        row = g["H"][j]
        for i in range(i0, i1):
            row[i] = 0


def _chamfer(g, nest, wall: int) -> None:
    """Two steps off the inner top edge of every wall: the board finds its way in."""
    from .jig import Q
    L = nest["local"]
    res = g["res"]
    t_in, s_stop = L["t_in"], L["s_stop"]
    s_front = -L["Ls"] / 2.0 - L["c"]
    steps = ((0.5, int(0.9 * Q)), (1.0, int(0.45 * Q)))
    for j in range(g["ny"]):
        tc = g["t0"] + (j + 0.5) * res
        row = g["H"][j]
        for i in range(g["nx"]):
            if row[i] != wall:
                continue
            sc = g["s0"] + (i + 0.5) * res
            d = min(abs(abs(tc) - t_in) if abs(tc) >= t_in else 99.0, sc - s_stop if sc >= s_stop else 99.0,
                    (s_front - sc) if nest["style"] == "drop" and sc <= s_front else 99.0)
            for lim, drop in steps:
                if d < lim:
                    row[i] = wall - drop
                    break


def press_grid(nest: Dict[str, Any], res: float = 0.25) -> Dict[str, Any]:
    """The press plate as seen from above in use: thickness down from its top face (fingers longer)."""
    from .jig import Q
    L, H = nest["local"], nest["heights"]
    g = _grid(L["plate_s"][0], L["plate_t"][0], L["plate_s"][1], L["plate_t"][1], res)
    q = lambda v: int(round(v * Q))           # noqa: E731
    _fill_box(g, L["plate_s"][0], L["plate_t"][0], L["plate_s"][1], L["plate_t"][1], q(PLATE_T))
    for w in nest["windows_local"]:
        _fill_box(g, w[0], w[1], w[2], w[3], 0)
    a, b_ = L["slot"]
    _fill_box(g, a - SLOT_W / 2.0, -SLOT_W / 2.0, b_ + SLOT_W / 2.0, SLOT_W / 2.0, 0)
    for ps, pt in nest["fingers_local"]:
        _fill_disc(g, ps, pt, float(nest.get("finger_d", 4.0)) / 2.0, q(PLATE_T + H["finger"]), keep_max=True)
    return g


def adapter_grid(nest: Dict[str, Any], res: float = 0.25) -> Dict[str, Any]:
    """The clamp adapter as seen from above in use: H - (pocket depth from its underside). The cross slots take the
    clamp's M4 screws anywhere within +-5 mm of the nominal hole pattern; the nuts sit in channels underneath."""
    from .jig import Q
    L, H = nest["local"], nest["heights"]
    a_s = L["adapter_s"]
    g = _grid(a_s - ADAPTER[0] / 2.0, -ADAPTER[1] / 2.0, a_s + ADAPTER[0] / 2.0, ADAPTER[1] / 2.0, res)
    q = lambda v: int(round(v * Q))           # noqa: E731
    top = q(H["adapter"])
    _fill_box(g, g["s0"], g["t0"], a_s + ADAPTER[0] / 2.0, ADAPTER[1] / 2.0, top)
    pa, pb = nest.get("clamp_holes_mm") or (20.0, 16.0)
    nut = max(0, top - q(3.6))
    reach = SLOT_ARM + 4.1                                   # an M4 nut's corners
    for sa in (-1, 1):
        for sb in (-1, 1):
            cs, ct = a_s + sa * pa / 2.0, sb * pb / 2.0
            for (ds, dt) in ((reach, 3.7), (3.7, reach)):     # nut channels (7.4 mm: the nut cannot turn)
                _fill_box(g, cs - ds, ct - dt, cs + ds, ct + dt, nut)
            for (ds, dt) in ((SLOT_ARM + 2.25, 2.25), (2.25, SLOT_ARM + 2.25)):   # the screw's cross slot, through
                _fill_box(g, cs - ds, ct - dt, cs + ds, ct + dt, 0)
    for ds, dt in ADAPTER_SCREWS:                            # blind holes for the M3 screws from below
        _fill_disc(g, a_s + ds, dt, 1.3, max(0, top - q(8.0)))
    return g


def write_parts(nest: Dict[str, Any], out_dir: str, name: str, count: int = 1) -> Dict[str, Any]:
    """STL + PNG preview of the nest, the press plate and the clamp adapter (one each per board), and the 3D models
    the fixture board shows them with. Returns the files (names relative to out_dir) and mesh checks."""
    from .jig import heightmap_mesh, mesh_check, write_heightmap_png, write_stl
    files: Dict[str, Any] = {}
    checks: Dict[str, Any] = {}
    parts = [("nest", nest_grid(nest), False)]
    if nest["clamp"] == "toggle":
        parts += [("press", press_grid(nest), True), ("adapter", adapter_grid(nest), True)]
    grids = {}
    for tag, g, flip in parts:
        H = g["H"][::-1] if flip else g["H"]           # turned over to print: mirrored across
        tris = heightmap_mesh(H, g["nx"], g["ny"], g["res"])
        stl = "%s-fixture-%s.stl" % (name, PART_FILES[tag])
        write_stl(os.path.join(out_dir, stl), tris, "pcbtestkit %s" % tag)
        png = stl[:-4] + ".png"
        write_heightmap_png(os.path.join(out_dir, png), H, g["nx"], g["ny"])
        files[tag] = {"stl": stl, "preview_png": png, "print": count,
                      "size_mm": [round(g["nx"] * g["res"], 1), round(g["ny"] * g["res"], 1),
                                  round(max(max(r) for r in H) / 100.0, 2)]}
        checks[tag] = mesh_check(tris)
        grids[tag] = g
    models = write_models(nest, grids, os.path.join(out_dir, "pcbtestkit-models"), name)
    return {"files": files, "mesh": checks, "models": models, "_grids": grids}


# =========================================================================== #
# 3D models (VRML) for the fixture board and the animation
# =========================================================================== #
def open_mesh(H: List[List[int]], nx: int, ny: int, res: float) -> Tuple[list, list]:
    """Render mesh of a heightmap with holes (cells of height 0 are empty): top and bottom faces only under
    material, walls wherever the height changes. Faces point out of the material; same frame as jig.render_mesh."""
    from .jig import Q, _greedy
    idx: Dict[Tuple[int, int, int], int] = {}
    verts: List[Tuple[float, float, float]] = []
    faces: List[Tuple[int, ...]] = []

    def v(i, j, z):
        k = (i, j, z)
        if k not in idx:
            idx[k] = len(verts)
            verts.append((i * res, j * res, z / float(Q)))
        return idx[k]

    for (i0, j0, i1, j1, h) in _greedy(H, nx, ny):
        if h <= 0:
            continue
        faces.append((v(i0, j0, h), v(i1, j0, h), v(i1, j1, h), v(i0, j1, h)))
        faces.append((v(i0, j0, 0), v(i0, j1, 0), v(i1, j1, 0), v(i1, j0, 0)))

    def cell(i, j):
        return H[j][i] if 0 <= i < nx and 0 <= j < ny else 0

    for i in range(nx + 1):
        run = None
        for j in range(ny + 1):
            span = None
            if j < ny:
                a, b_ = cell(i - 1, j), cell(i, j)
                if a != b_:
                    span = (min(a, b_), max(a, b_), a > b_)
            if run and span == run[1]:
                continue
            if run:
                lo, hi, pos = run[1]
                f = (v(i, run[0], lo), v(i, j, lo), v(i, j, hi), v(i, run[0], hi))
                faces.append(f if pos else f[::-1])
            run = (j, span) if span else None
    for j in range(ny + 1):
        run = None
        for i in range(nx + 1):
            span = None
            if i < nx:
                a, b_ = cell(i, j - 1), cell(i, j)
                if a != b_:
                    span = (min(a, b_), max(a, b_), a > b_)
            if run and span == run[1]:
                continue
            if run:
                lo, hi, pos = run[1]
                f = (v(run[0], j, lo), v(i, j, lo), v(i, j, hi), v(run[0], j, hi))
                faces.append(f[::-1] if pos else f)
            run = (i, span) if span else None
    return verts, faces


def _mesh_from_grid(nest, g, z0: float, flip_z: bool = False) -> Tuple[list, list]:
    """Render mesh of a nest-frame grid in KiCad model coordinates (origin at the board centre on the site)."""
    fr = _frame(nest)
    verts, faces = open_mesh(g["H"], g["nx"], g["ny"], g["res"])
    out = []
    for x, y, z in verts:
        mxx, myy = fr.model(g["s0"] + x, g["t0"] + y)
        out.append((mxx, myy, z0 - z if flip_z else z0 + z))
    # the nest frame -> model frame mapping mirrors (y up); flipping z mirrors again
    faces = [f for f in faces] if flip_z else [tuple(reversed(f)) for f in faces]
    return out, faces


def _frame(nest) -> Frame:
    fr = Frame.__new__(Frame)
    fr.entry = nest["entry"]
    (fr.sx, fr.sy), (fr.tx, fr.ty) = nest["frame"]["s"], nest["frame"]["t"]
    fr.cx, fr.cy = nest["frame"]["origin"]
    return fr


def _local_box(s0, t0, z0, s1, t1, z1) -> Tuple[list, list]:
    v = [(s0, t0, z0), (s1, t0, z0), (s1, t1, z0), (s0, t1, z0), (s0, t0, z1), (s1, t0, z1), (s1, t1, z1), (s0, t1, z1)]
    f = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    return v, f


def _to_model(nest, verts, faces) -> Tuple[list, list]:
    fr = _frame(nest)
    out = []
    for s, t, z in verts:
        a, b_ = fr.model(s, t)
        out.append((a, b_, z))
    return out, [tuple(reversed(f)) for f in faces]


def _rot(verts, ps: float, pz: float, ang: float):
    """Turn (s, t, z) points about the axis along t through (ps, pz); positive opens the arm upwards."""
    ca, sa = math.cos(ang), math.sin(ang)
    out = []
    for s, t, z in verts:
        ds, dz = s - ps, z - pz
        out.append((ps + ds * ca + dz * sa, t, pz - ds * sa + dz * ca))
    return out


def clamp_shapes(nest: Dict[str, Any], open_deg: float, grids: Optional[Dict[str, Any]] = None,
                 ghost: bool = False) -> List[Tuple]:
    """A generic vertical toggle clamp on its adapter, with the press plate on the spindle, the arm ``open_deg``
    degrees up (0 = clamped); ``ghost``: a see-through press plate (the board shows under it). Shapes in KiCad
    model coordinates for visuals.write_wrl."""
    from .visuals import cylinder
    L, H = nest["local"], nest["heights"]
    a_s, base = L["adapter_s"], H["adapter"]
    grids = grids or {}
    g_a = grids.get("adapter") or adapter_grid(nest, 0.5)
    g_p = grids.get("press") or press_grid(nest, 0.5)
    shapes = [_mesh_from_grid(nest, g_a, base, flip_z=True) + (COLORS["adapter"],)]
    ps, pz = a_s + ARM_PIVOT[0], base + 3.0 + ARM_PIVOT[1]
    ang = math.radians(open_deg)
    bodies = [(_local_box(a_s - 14, -9, base, a_s + 14, 9, base + 3), COLORS["steel"]),
              (_local_box(a_s - 8, -7, base + 3, a_s + 6, -5, pz + 4), COLORS["steel"]),
              (_local_box(a_s - 8, 5, base + 3, a_s + 6, 7, pz + 4), COLORS["steel"])]
    for (v, f), col in bodies:
        mv, mf = _to_model(nest, v, f)
        shapes.append((mv, mf, col))
    sp = L["spindle"]
    z_top = H["plate_top_work"]
    moving = [(_local_box(sp - 6, -4, pz - 3, ps + 3, 4, pz + 3), COLORS["steel"])]
    cv, cf = cylinder(sp, 0.0, z_top, pz - 3, 2.6)
    moving.append(((cv, cf), COLORS["steel"]))
    for (v, f), col in moving:
        mv, mf = _to_model(nest, _rot(v, ps, pz, ang), f)
        shapes.append((mv, mf, col))
    # the press plate hangs on the spindle: turned with the arm
    pv, pf = open_mesh(g_p["H"], g_p["nx"], g_p["ny"], g_p["res"])
    loc = [(g_p["s0"] + x, g_p["t0"] + y, z_top - z) for x, y, z in pv]
    mv, mf = _to_model(nest, _rot(loc, ps, pz, ang), [tuple(reversed(f)) for f in pf])
    shapes.append((mv, mf, COLORS["plate"] + ((0.72,) if ghost else ())))
    # the handle: upright when clamped, swung back when open
    hv, hf = _local_box(a_s + 4, -3, pz, a_s + 8, 3, pz + 34)
    hv = _rot(hv, a_s + 6, pz, 0.55 * ang)
    mv, mf = _to_model(nest, hv, hf)
    shapes.append((mv, mf, COLORS["handle"]))
    return shapes


def nest_shapes(nest: Dict[str, Any], dz: float = 0.0, grids: Optional[Dict[str, Any]] = None) -> List[Tuple]:
    """The nest at its loading height (dz = -travel: pressed down) in model coordinates."""
    g = (grids or {}).get("nest") or nest_grid(nest, 0.5)
    return [_mesh_from_grid(nest, g, nest["heights"]["bottom_rest"] + dz) + (COLORS["nest"],)]


def standoff_shapes(nest: Dict[str, Any]) -> List[Tuple]:
    """One post (origin at its hole): hex standoff, the spring under the nest, washer and screw on top."""
    from .visuals import cylinder
    H = nest["heights"]
    L = H["standoff"]
    out = []
    parts = [(STANDOFF_AF / math.sqrt(3.0), 0.0, L, 6, COLORS["steel"]), (4.5, L, L + 0.8, 18, COLORS["steel"]),
             (2.75, L + 0.8, L + 3.8, 18, COLORS["steel"])]
    coils, top = 6, H["bottom_rest"]
    for k in range(coils):                       # the spring as a stack of coils
        z = top * k / float(coils - 1)
        parts.append((4.0, max(0.0, z - 0.25), min(top, z + 0.25), 18, COLORS["spring"]))
    for r0, z0, z1, n, col in parts:
        v, f = cylinder(0.0, 0.0, z0, z1, r0, n=n)
        out.append((v, f, col))
    return out


def write_models(nest: Dict[str, Any], grids: Dict[str, Any], model_dir: str, name: str) -> Dict[str, str]:
    """VRML of the nest (loading height) and the clamp (open) for the fixture board's 3D view."""
    from .visuals import write_wrl
    os.makedirs(model_dir, exist_ok=True)
    out = {"nest": "nest_%s.wrl" % _safe(name), "post": "nest_post_%s.wrl" % _safe(name)}
    write_wrl(os.path.join(model_dir, out["nest"]), nest_shapes(nest, 0.0, grids))
    write_wrl(os.path.join(model_dir, out["post"]), standoff_shapes(nest))
    if nest["clamp"] == "toggle":
        out["clamp"] = "clamp_%s.wrl" % _safe(name)
        write_wrl(os.path.join(model_dir, out["clamp"]), clamp_shapes(nest, 95.0, grids))
    return out


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)[:60] or "board"


def summary(nest: Dict[str, Any]) -> Dict[str, Any]:
    """What a guide or an agent needs (no geometry arrays)."""
    if not nest or not nest.get("enabled"):
        return {"enabled": False, "reason": (nest or {}).get("reason", "")}
    keep = ("enabled", "style", "entry", "clamp", "heights", "board_mm", "spring", "bom", "warnings", "gap_reason",
            "extents")
    out = {k: nest[k] for k in keep if k in nest}
    out["fingers"] = len(nest.get("fingers") or [])
    return out


def scene_text(text: str, nest: Dict[str, Any], name: str, scene_dir: str, pressed: bool, open_deg: float,
               grids: Optional[Dict[str, Any]] = None) -> str:
    """The fixture board's text with every nest pressed down by its travel (or not) and every clamp ``open_deg``
    up: the models are written to ``scene_dir`` and swapped in (the fixture's own models stay as they are)."""
    import re
    from .kicad_cli import sexpr_path
    from .visuals import write_wrl
    if not nest or not nest.get("enabled"):
        return text
    nm = _safe(name)
    os.makedirs(scene_dir, exist_ok=True)
    grids = grids or {}
    tag = "%s_%s_%d" % (nm, "down" if pressed else "up", int(round(open_deg)))
    swaps = {}
    p_nest = os.path.join(scene_dir, "nest_%s.wrl" % tag)
    if not os.path.exists(p_nest):
        write_wrl(p_nest, nest_shapes(nest, -nest["heights"]["travel"] if pressed else 0.0, grids))
    swaps["nest_%s.wrl" % nm] = p_nest
    if nest.get("clamp") == "toggle":
        p_cl = os.path.join(scene_dir, "clamp_%s.wrl" % tag)
        if not os.path.exists(p_cl):
            write_wrl(p_cl, clamp_shapes(nest, open_deg, grids))
        swaps["clamp_%s.wrl" % nm] = p_cl
    for base, new in swaps.items():
        text = re.sub(r'"[^"]*pcbtestkit-models/%s"' % re.escape(base), lambda m, n_=new: '"%s"' % sexpr_path(n_), text)
    return text
