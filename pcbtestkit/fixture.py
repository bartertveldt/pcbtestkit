"""Bed-of-nails fixture board generator (.kicad_pcb).

The fixture PCB carries one pogo pin per probe of the test plan, aligned with
the DUT, plus a header to the measurement hardware (Raspberry Pi ADC HAT, MCU,
...), voltage dividers for channels above the ADC range, a power terminal,
guide-pin holes copied from the DUT's mounting holes, mounting holes, and
cut-outs for tall DUT parts and fitted stacked boards on the probed side.
Several DUT sites can be panelised on one fixture. With probe_side "both" there
is one fixture plate per side (a clamshell: the DUT lies between them).

The board comes out routed by the built-in router (router.py): two layers, KiCad's default rules, wider
GND/VIN tracks. Each divider pair sits above its own header pin, so the channel nets drop straight down.
Connections the router cannot make are reported in the warnings and stay as ratsnest to route in KiCad.
"""
from __future__ import annotations

import math
import os
from typing import Any, Dict, List, Optional, Tuple

from . import classify as C
from . import kicad_writer as W
from .geometry import BBox, point_in_segments, pt_seg_dist
from .project import Design

SILK, FAB, CRT = "F.SilkS", "F.Fab", "F.CrtYd"
THT = ["*.Cu", "*.Mask"]
SMD = ["F.Cu", "F.Paste", "F.Mask"]


def _safe(net: str) -> str:
    out = []
    for ch in C.base_net(net) if not net.startswith("Net-(") else net:
        out.append(ch if ch.isalnum() or ch in "+-_." else "_")
    return "".join(out).strip("_") or "NET"


class FixtureBuilder:
    def __init__(self, design: Design, plan: Dict[str, Any]):
        self.d = design
        self.plan = plan
        self.fx = design.cfg["fixture"]
        self.b = design.board
        self.bb = self.b.outline_bbox()
        self.mirror = plan["probe_side"] == "top"
        self.items: List[list] = []
        self.nets: List[str] = []
        self.net_ix: Dict[str, int] = {}
        self.warnings: List[str] = []
        self.refs: Dict[str, int] = {}
        self.channel_map: List[Dict[str, Any]] = []
        self.pogo_xy: List[Tuple[float, float]] = []
        self._grid_origin: Optional[Tuple[float, float]] = None
        self.routing: Optional[Dict[str, Any]] = None
        pogo = plan["pogo"]
        self.drill = float(pogo["drill_mm"])
        self.pad_d = float(pogo.get("pad_mm") or round(self.drill + float(design.cfg["pogo"]["pad_extra"]), 3))
        self.pitch = float(pogo.get("min_spacing_mm") or pogo["pitch_mm"])

    # ------------------------------------------------------------------ #
    def net(self, name: str) -> Tuple[int, str]:
        if name not in self.net_ix:
            self.nets.append(name)
            self.net_ix[name] = len(self.nets)
        return self.net_ix[name], name

    def ref(self, prefix: str) -> str:
        self.refs[prefix] = self.refs.get(prefix, 0) + 1
        return "%s%d" % (prefix, self.refs[prefix])

    def mx(self, x: float) -> float:
        return 2 * self.bb.cx - x if self.mirror else x

    # ------------------------------------------------------------------ #
    def pogo(self, x, y, net, dut_net, probe_id, tip, role, isolated=True, geo: Optional[Dict[str, Any]] = None):
        """One pogo pin. ``geo``: a finer family for this probe (fine-pitch programming headers)."""
        pad_d = float(geo["pad_mm"]) if geo else self.pad_d
        drill = float(geo["drill_mm"]) if geo else self.drill
        pitch = float(geo["min_spacing_mm"]) if geo else self.pitch
        fam = geo["family"] if geo else self.plan["pogo"]["family"]
        mount = geo["mount"] if geo else self.plan["pogo"]["mount"]
        r = pad_d / 2
        silk = isolated and not geo                  # fine-pitch clusters: labels on the fab layer only
        items = [W.fp_circle(0, 0, max(r + 0.1, pitch / 2 - 0.01), CRT, 0.05),
                 W.fp_text(probe_id, 0, -(r + 1.1), SILK if silk else FAB, 0.8 if silk else 0.5),
                 W.fp_text(C.base_net(dut_net)[:16], 0, r + 0.9, FAB, 0.5),
                 W.pad("1", "thru_hole", "circle", 0, 0, pad_d, pad_d, THT, drill=drill,
                       net=self.net(net), pintype="passive")]
        if not geo:
            items.insert(0, W.fp_circle(0, 0, r + 0.15, SILK, 0.12))
        self.items.append(W.footprint("PCBTestKit:Pogo_%s_%s" % (fam, mount), x, y, self.ref("P"),
                                      C.base_net(dut_net), items, attr=("through_hole",),
                                      extra_props={"Probe": probe_id, "DUT_Net": dut_net, "Tip": tip, "Role": role},
                                      hide_ref=True, models=["${KIPRJMOD}/pcbtestkit-models/%s" % pogo_model_name(fam, tip)]))
        self.pogo_xy.append((x, y))

    def header(self, x, y, pins: List[Tuple[int, str, str]]) -> str:
        """2 x N 2.54 mm header, rotated so the pins run along +X. pins: (number, net, label)."""
        n = len(pins)
        rows = n // 2
        ref = self.ref("J")
        items = [W.fp_rect(-1.8, -1.8, 4.34, 2.54 * (rows - 1) + 1.8, CRT, 0.05),
                 W.fp_rect(-1.27, -1.27, 3.81, 2.54 * (rows - 1) + 1.27, FAB, 0.1)]
        for num, net, label in pins:
            col, row = (num - 1) % 2, (num - 1) // 2
            px, py = 2.54 * col, 2.54 * row
            items.append(W.pad(str(num), "thru_hole", "rect" if num == 1 else "oval", px, py, 1.7, 1.7, THT, drill=1.0,
                               net=self.net(net) if net else None, angle=90, pinfunction=label, pintype="passive"))
            items.append(W.fp_text(label[:10], px + (-2.6 if col == 0 else 5.1), py, FAB, 0.5))
        self.items.append(W.footprint("Connector_PinHeader_2.54mm:PinHeader_2x%02d_P2.54mm_Vertical" % rows, x, y, ref,
                                      "TEST_IO", items, angle=90, attr=("through_hole",), ref_pos=(4.8, 2.54 * (rows - 1) / 2.0),
                                      models=["${KICAD8_3DMODEL_DIR}/Connector_PinHeader_2.54mm.3dshapes/"
                                              "PinHeader_2x%02d_P2.54mm_Vertical.wrl" % rows]))
        return ref

    def terminal(self, x, y, vin: str, gnd: str) -> str:
        ref = self.ref("J")
        items = [W.fp_rect(-2.8, -4.0, 7.9, 4.0, CRT, 0.05), W.fp_rect(-2.54, -3.7, 7.62, 3.7, FAB, 0.1),
                 W.fp_text("+", 0, -3.0, SILK, 1.0), W.fp_text("GND", 5.08, -3.0, SILK, 0.8),
                 W.pad("1", "thru_hole", "rect", 0, 0, 2.6, 2.6, THT, drill=1.3, net=self.net(vin), pinfunction="VIN", pintype="passive"),
                 W.pad("2", "thru_hole", "circle", 5.08, 0, 2.6, 2.6, THT, drill=1.3, net=self.net(gnd), pinfunction="GND", pintype="passive")]
        self.items.append(W.footprint("TerminalBlock:TerminalBlock_bornier-2_P5.08mm", x, y, ref, "DUT_POWER", items,
                                      attr=("through_hole",), ref_pos=(2.54, 5.0)))
        return ref

    def resistor(self, x, y, value_ohm: float, n1: str, n2: str) -> str:
        ref = self.ref("R")
        items = [W.fp_rect(-1.68, -0.95, 1.68, 0.95, CRT, 0.05), W.fp_rect(-1.0, -0.625, 1.0, 0.625, FAB, 0.1),
                 W.pad("1", "smd", "roundrect", -0.9125, 0, 1.025, 1.4, SMD, net=self.net(n1), angle=90, rratio=0.25, pintype="passive"),
                 W.pad("2", "smd", "roundrect", 0.9125, 0, 1.025, 1.4, SMD, net=self.net(n2), angle=90, rratio=0.25, pintype="passive")]
        items.append(W.fp_text(ref, 0, 0, FAB, 0.5))
        self.items.append(W.footprint("Resistor_SMD:R_0805_2012Metric", x, y, ref, _ohm(value_ohm), items, angle=90,
                                      ref_pos=(0, -1.65), value_pos=(0, 1.65), hide_ref=True,
                                      models=["${KICAD8_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0805_2012Metric.wrl"]))
        return ref

    def hole(self, x, y, d, label, prefix="H"):
        items = [W.fp_circle(0, 0, d / 2 + 0.25, CRT, 0.05), W.fp_circle(0, 0, d / 2, "Cmts.User", 0.1),
                 W.pad("", "np_thru_hole", "circle", 0, 0, d, d, ["*.Cu", "*.Mask"], drill=d)]
        items.append(W.fp_text(label, 0, d / 2 + 1.0, "Cmts.User", 0.8))
        self.items.append(W.footprint("MountingHole:MountingHole_%.1fmm" % d, x, y, self.ref(prefix), label, items,
                                      attr=("exclude_from_pos_files", "exclude_from_bom"), ref_pos=(0, -(d / 2 + 1.2)),
                                      value_pos=(0, d / 2 + 1.2), hide_ref=True))

    # ---- programming connector ---------------------------------------------------
    def fixture_net(self, dut_net: str, pre: str) -> str:
        """The fixture net that carries a DUT net on a site ("" when no probe on this plate reaches it)."""
        probes = [p for p in self.plan["probes"] if p["net"] == dut_net]
        if not dut_net or not probes:
            return ""
        if any(p["role"] == "ground" for p in probes):
            return "GND"
        if any(p["role"] == "supply" for p in probes):
            return pre + "VIN"
        return pre + "TP_" + _safe(dut_net)

    def prog_headers(self) -> List[Dict[str, Any]]:
        """Programming headers this plate serves: those with at least one pin reached by its probes."""
        prog = self.plan.get("program") or {}
        mine = {p["id"] for p in self.plan["probes"]}
        return [h for h in prog.get("headers") or [] if any(pin.get("probe") in mine for pin in h["pins"])]

    def _prog_shape(self, header: Dict[str, Any]) -> Dict[str, Any]:
        """The programming connector in its own frame: lib id, pads, 3D models, pins, angle and its board-frame
        box (relative to the footprint origin). ``same`` copies the board's header footprint as designed (a bottom
        footprint is un-mirrored), so the cable that programs the board fits the fixture."""
        from .geometry import to_board
        from .program import STANDARD, pinout
        cached = self.__dict__.setdefault("_prog_cache", {})
        if header["ref"] in cached:
            return cached[header["ref"]]
        kind = header.get("connector") or "same"
        fp = self.b.by_ref.get(header["ref"])
        if kind == "same" and fp is None:
            kind = "pins"
        pins = pinout(header, kind)
        pads: List[Dict[str, Any]] = []
        models: List[str] = []
        if kind == "same":
            flip = fp.local_flipped
            for p in fp.pads:
                ly = -p.local_y if flip else p.local_y
                ang = (p.angle - fp.angle) * (-1 if flip else 1)
                if p.is_npth:
                    d = p.drill_w
                    pads.append({"num": "", "kind": "np_thru_hole", "shape": "circle", "x": p.local_x, "y": ly, "w": d,
                                 "h": d, "drill": d, "angle": 0.0, "rratio": None, "layers": ["*.Cu", "*.Mask"]})
                    continue
                if not p.has_copper:
                    continue
                tht = p.is_tht
                pads.append({"num": p.number, "kind": "thru_hole" if tht else "smd",
                             "shape": p.shape if p.shape in ("circle", "rect", "oval", "roundrect") else "rect",
                             "x": p.local_x, "y": ly, "w": p.w, "h": p.h, "drill": p.drill_w if tht else None,
                             "angle": ang, "rratio": p.rratio if p.shape == "roundrect" else None,
                             "layers": THT if tht else ["F.Cu", "F.Mask"]})
            lib = fp.lib_id or "PCBTestKit:Prog"
            models = [m.path for m in fp.models if not m.hide and "KIPRJMOD" not in m.path]
        else:
            if kind == "pins":
                n = len(pins)
                st = {"cols": 1, "pitch": 2.54, "pad": 1.7, "drill": 1.0,
                      "lib": "Connector_PinHeader_2.54mm:PinHeader_1x%02d_P2.54mm_Vertical" % n,
                      "model": "Connector_PinHeader_2.54mm.3dshapes/PinHeader_1x%02d_P2.54mm_Vertical.wrl" % n}
            else:
                st = STANDARD[kind]
            for num, _net, _label in pins:
                k = int(num)
                col, row = (k - 1) % st["cols"], (k - 1) // st["cols"]
                pads.append({"num": num, "kind": "thru_hole", "shape": "rect" if k == 1 else "oval", "x": st["pitch"] * col,
                             "y": st["pitch"] * row, "w": st["pad"], "h": st["pad"], "drill": st["drill"], "angle": 0.0,
                             "rratio": None, "layers": THT})
            lib = st["lib"]
            models = ["${KICAD8_3DMODEL_DIR}/" + st["model"]]
        lb = BBox()
        for p in pads:
            r = max(p["w"], p["h"]) / 2
            lb.merge(BBox(p["x"] - r, p["y"] - r, p["x"] + r, p["y"] + r))
        angle = 90.0 if lb.h > lb.w + 0.01 else 0.0       # lie flat along the connector row
        board = BBox.of_points(to_board(0.0, 0.0, angle, x, y) for x, y in lb.grow(1.0).corners())
        res = {"kind": kind, "lib": lib, "pads": pads, "models": models, "pins": pins, "angle": angle, "local": lb,
               "box": board}
        cached[header["ref"]] = res
        return res

    def prog_connector(self, x0: float, cy: float, header: Dict[str, Any], pre: str, site: int) -> Dict[str, Any]:
        """Place the programming connector for ``header`` with its left edge at x0, centred on cy."""
        sh = self._prog_shape(header)
        ox, oy = x0 - sh["box"].x0, cy - sh["box"].cy
        net_of = {num: self.fixture_net(dut, pre) for num, dut, _label in sh["pins"]}
        label_of = {num: label for num, _dut, label in sh["pins"]}
        dut_of = {num: dut for num, dut, _label in sh["pins"]}
        for num, _dut, label in sh["pins"]:
            if label == "GND" and not net_of.get(num):
                net_of[num] = "GND"
        lb = sh["local"]
        items = [W.fp_rect(lb.x0 - 1.0, lb.y0 - 1.0, lb.x1 + 1.0, lb.y1 + 1.0, CRT, 0.05),
                 W.fp_rect(lb.x0 - 0.6, lb.y0 - 0.6, lb.x1 + 0.6, lb.y1 + 0.6, FAB, 0.1)]
        out_pins = []
        for p in sh["pads"]:
            net = net_of.get(p["num"], "") if p["num"] else ""
            items.append(W.pad(p["num"], p["kind"], p["shape"], p["x"], p["y"], p["w"], p["h"], p["layers"],
                               drill=p["drill"], net=self.net(net) if net else None, angle=p["angle"] + sh["angle"],
                               rratio=p["rratio"], pinfunction=label_of.get(p["num"], "")[:12] if p["num"] else "",
                               pintype="passive" if p["num"] else ""))
            if p["num"]:
                dut = dut_of.get(p["num"], "")
                pad_ = next((q["pad"] for q in header["pins"] if dut and q["net"] == dut), "")
                out_pins.append({"pin": p["num"], "signal": label_of.get(p["num"], ""), "net": net, "dut_net": dut,
                                 "dut_pad": pad_})
                if not net and dut_of.get(p["num"]):
                    self.warnings.append("programming connector for %s%s: pin %s (%s) is not reached by a probe on this "
                                         "fixture plate" % (header["ref"], " site %d" % site if pre else "", p["num"],
                                                            label_of.get(p["num"])))
        p1 = next((p for p in sh["pads"] if p["num"] in ("1", "A1")), None)
        if p1 is not None:                            # pin-1 mark on the silkscreen, clear of every pad and hole
            r1 = max(p1["w"], p1["h"]) / 2 + 0.6
            for mx_, my_ in ((-r1, 0.0), (0.0, -r1), (0.0, r1), (r1, 0.0), (-r1, -r1)):
                cx_, cy_ = p1["x"] + mx_, p1["y"] + my_
                if all(math.hypot(cx_ - q["x"], cy_ - q["y"]) > max(q["w"], q["h"]) / 2 + 0.45 for q in sh["pads"]):
                    items.append(W.fp_circle(cx_, cy_, 0.2, SILK, 0.2))
                    break
        ref = self.ref("PRG")                         # own prefix: never mistaken for the board's J refs
        title = "PROG %s%s" % (header["ref"], " S%d" % site if pre else "")
        self.items.append(W.footprint(sh["lib"], round(ox, 4), round(oy, 4), ref, title, items, angle=sh["angle"],
                                      attr=("through_hole",) if any(p["kind"] == "thru_hole" for p in sh["pads"]) else ("smd",),
                                      extra_props={"Programs": header["ref"], "Protocol": header.get("protocol", ""),
                                                   "Connector": sh["kind"]},
                                      hide_ref=True, models=sh["models"]))
        b = sh["box"]
        self.items.append(W.gr_text(title, ox + b.cx, oy + b.y0 - 1.2, SILK, 0.9))
        return {"ref": ref, "header": header["ref"], "protocol": header.get("protocol"), "connector": sh["kind"],
                "pins": out_pins, "x": round(ox, 3), "y": round(oy, 3)}

    # ------------------------------------------------------------------ #
    DIV_PITCH_X, DIV_PITCH_Y = 3.5, 8.0

    def _occupied(self) -> List[BBox]:
        """Courtyard boxes of every footprint placed so far (for late placement checks)."""
        from .sexpr import child as _child
        out = []
        for it in self.items:
            if not it or it[0] != "footprint":
                continue
            at = _child(it, "at")
            fx_, fy_ = float(at[1]), float(at[2])
            ang = float(at[3]) if len(at) > 3 else 0.0
            for g in it[1:]:
                if isinstance(g, list) and g and g[0] in ("fp_rect", "fp_circle") and _child(g, "layer")[1] == CRT:
                    if g[0] == "fp_rect":
                        (x0, y0), (x1, y1) = _child(g, "start")[1:3], _child(g, "end")[1:3]
                    else:
                        c, e = _child(g, "center")[1:3], _child(g, "end")[1:3]
                        rr = math.hypot(e[0] - c[0], e[1] - c[1])
                        x0, y0, x1, y1 = c[0] - rr, c[1] - rr, c[0] + rr, c[1] + rr
                    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
                    from .geometry import to_board
                    out.append(BBox.of_points(to_board(fx_, fy_, ang, px, py) for px, py in pts))
        return out

    def _divided_pin_x(self) -> List[float]:
        """For every channel with a divider (in header order): its header pin's x, relative to pin 1."""
        xs = []
        for h in self.plan["header"]:
            if h["signal"] in ("GND", "NC"):
                continue
            probe = next((p for p in self.plan["probes"] if p.get("channel") == h["signal"]), None)
            if probe is not None and probe.get("divider"):
                xs.append(2.54 * ((h["pin"] - 1) // 2))
        return xs

    def divider_slots(self, xs: List[float]) -> List[Tuple[int, float]]:
        """(row, x) of each divider pair: above its own header pin, staggered over as few rows as keep them
        DIV_PITCH_X apart, so each channel net runs straight down to its pin (and is easy to route)."""
        if not xs:
            return []
        best: List[Tuple[int, float]] = []
        for rows in range(1, 9):
            last = [-1e9] * rows
            out = []
            worst = 0.0
            for x in xs:
                r = min(range(rows), key=lambda k: (max(0.0, last[k] + self.DIV_PITCH_X - x), k))
                px = max(x, last[r] + self.DIV_PITCH_X)
                last[r] = px
                out.append((r, round(px, 3)))
                worst = max(worst, px - x)
            best = out
            if worst <= 1.3:
                break
        return best

    def block_layout(self, max_w: Optional[float] = None) -> Dict[str, Any]:
        """Connector block: divider pairs in rows above their header pins, then header + power terminal."""
        n_div = sum(1 for p in self.plan["probes"] if p.get("channel") and p.get("divider"))
        hdr_cols = len(self.plan["header"]) // 2
        hdr_w = 2.54 * max(0, hdr_cols - 1) + 1.7
        term_w = 11.0 if (self.plan.get("supply") or {}).get("net") and (
            not self.plan.get("plate") or any(p["role"] == "supply" for p in self.plan["probes"])) else 0.0
        progs = [self._prog_shape(hd)["box"] for hd in self.prog_headers()]
        prog_w = sum(b.w + 4.0 for b in progs)
        min_w = hdr_w + 6.0 + term_w + 4.0 + prog_w
        slots = self.divider_slots(self._divided_pin_x())
        div_rows = (max(r for r, _ in slots) + 1) if slots else 0
        w = max(min_w, 6.0 + max(x for _, x in slots)) if slots else min_w
        h = div_rows * self.DIV_PITCH_Y + 2.54 + 2.54 + 5.0 + max([0.0] + [b.h + 2.5 - 7.6 for b in progs])
        return {"w": w, "h": h, "div_rows": div_rows, "n_div": n_div, "slots": slots}

    def block_dims(self) -> Tuple[float, float, int]:
        lay = self.block_layout()
        return lay["w"], lay["h"], lay["n_div"]

    def _find_inside(self, w: float, h: float) -> Optional[Tuple[float, float]]:
        """Free spot for the connector block inside the (mirrored) DUT outline."""
        segs = [(self.mx(a), b, self.mx(c), d) for a, b, c, d in self.b.edge_segs]
        keep = [(x, y, self.pitch / 2) for x, y in self.pogo_xy]
        for fp in self.d.components():
            for pd in fp.pads:
                if pd.drill_w >= 1.5 and (pd.is_npth or C.is_mounting_hole(fp)):
                    keep.append((self.mx(pd.x), pd.y, pd.drill_w / 2 + 0.5))
        cut = [t["bbox"] for t in self.plan.get("fixture_keepouts", [])]
        y = self.bb.y1 - h - 1.0
        while y >= self.bb.y0 + 1.0:
            x = self.bb.x0 + 1.0
            while x + w <= self.bb.x1 - 1.0:
                pts = [(x + w * i / 4.0, y + h * j / 2.0) for i in range(5) for j in range(3)]
                if all(point_in_segments(px, py, segs) and min(pt_seg_dist(px, py, *sg) for sg in segs) >= 1.0
                       for px, py in pts):
                    box = BBox(x, y, x + w, y + h).grow(1.0)
                    if not any(box.contains(px, py, rr) for px, py, rr in keep) and \
                            not any(box.overlaps(BBox(c["x_min"], c["y_min"], c["x_max"], c["y_max"])) for c in cut):
                        return x, y
                x += 1.0
            y -= 1.0
        return None

    def build(self) -> Dict[str, Any]:
        fx = self.fx
        from .batch import layout as batch_layout
        blay = batch_layout(self.d.cfg, self.bb.w, self.bb.h)     # batch.count (or fixture.panel): one site per board
        cols, rows, nsite = blay["cols"], blay["rows"], blay["count"]
        margin = float(fx["margin_mm"])
        lay0 = self.block_layout()
        bw, bh, n_div = lay0["w"], lay0["h"], lay0["n_div"]
        self._block_w = None
        spacing = float(blay["spacing_mm"])
        outline_mode = fx.get("outline", "rect")
        if outline_mode == "outline" and (nsite > 1 or not self.b.edge_segs):
            self.warnings.append("outline mode needs a single site and a DUT outline; using a rectangle")
            outline_mode = "rect"
        inside_xy = None
        if outline_mode == "outline":
            for p in self.plan["probes"]:          # pogo positions first, so the search can avoid them
                self.pogo_xy.append((self.mx(p["x"]), p["y"]))
            inner_w = max(20.0, self.bb.w - 8.0)
            lay_in = self.block_layout(inner_w)
            bw, bh = lay_in["w"], lay_in["h"]
            self._block_w = inner_w
            inside_xy = self._find_inside(bw, bh)
            self.pogo_xy = []
            if inside_xy is None:
                self.warnings.append("connector block (%.0f x %.0f mm) does not fit inside the DUT outline; "
                                     "using a rectangular fixture instead" % (bw, bh))
                outline_mode = "rect"
                self._block_w = None
                bw, bh = lay0["w"], lay0["h"]
        site_w = max(self.bb.w + 2 * margin, bw + 18.0)
        site_h = self.bb.h + 2 * margin + (0.0 if outline_mode == "outline" else bh + 7.0)
        if nsite > 1:       # site pitch on the router's 0.254 mm grid, so every site's header pins are on it
            site_w = math.ceil((site_w + spacing) / 0.254 - 1e-9) * 0.254 - spacing
            site_h = math.ceil((site_h + spacing) / 0.254 - 1e-9) * 0.254 - spacing
        fx0, fy0 = self.bb.x0 - margin, self.bb.y0 - margin
        fx1 = fx0 + cols * site_w + (cols - 1) * spacing
        fy1 = fy0 + rows * site_h + (rows - 1) * spacing
        sites = []
        for j in range(rows):
            for i in range(cols):
                k = j * cols + i + 1
                if k > nsite:                           # a batch that does not fill the last row
                    continue
                dx, dy = i * (site_w + spacing), j * (site_h + spacing)
                if inside_xy:
                    bx, by = inside_xy
                else:
                    bx, by = self.bb.x0 - margin + 9.0 + dx, self.bb.y1 + margin + 4.0 + dy
                if outline_mode == "outline":
                    limit = BBox(self.bb.x0, self.bb.y0, self.bb.x1, self.bb.y1)
                    lsegs = [(self.mx(a), b_, self.mx(c), d_) for a, b_, c, d_ in self.b.edge_segs]
                else:
                    limit = BBox(fx0 + dx, fy0 + dy, fx0 + dx + site_w, self.bb.y1 + margin + dy)
                    lsegs = None
                sites.append(self._site(k, nsite, dx, dy, bx, by, limit, lsegs))
        if outline_mode == "outline":
            for sg in self.b.edge_segs:
                self.items.append(W.gr_line(self.mx(sg[0]), sg[1], self.mx(sg[2]), sg[3], "Edge.Cuts", 0.1))
            ob = BBox(self.bb.x0, self.bb.y0, self.bb.x1, self.bb.y1)
        else:
            self.items.append(W.gr_rect(fx0, fy0, fx1, fy1, "Edge.Cuts", 0.1))
            ob = BBox(fx0, fy0, fx1, fy1)
        if fx.get("mounting_holes", True) and outline_mode == "rect":
            d = float(fx.get("mounting_hole_d_mm", 3.2))
            for hx, hy in ((fx0 + 4, fy0 + 4), (fx1 - 4, fy0 + 4), (fx0 + 4, fy1 - 4), (fx1 - 4, fy1 - 4)):
                if any(math.hypot(hx - px, hy - py) < d / 2 + 2.5 for px, py in self.pogo_xy):
                    self.warnings.append("fixture mounting hole at (%.1f, %.1f) skipped: too close to a probe" % (hx, hy))
                    continue
                hb = BBox(hx - d / 2 - 1.0, hy - d / 2 - 1.0, hx + d / 2 + 1.0, hy + d / 2 + 1.0)
                if any(hb.overlaps(ob_) for ob_ in self._occupied()):
                    self.warnings.append("fixture mounting hole at (%.1f, %.1f) skipped: overlaps a part" % (hx, hy))
                    continue
                self.hole(hx, hy, d, "M3 standoff")
        title = "TEST FIXTURE %s %s" % (self.b.name, self.b.title_block.get("rev", ""))
        if outline_mode == "rect":
            self.items.append(W.gr_text(title.strip(), ob.cx, ob.y0 + 2.4, SILK, 1.2))
        else:
            self.items.append(W.gr_text(title.strip(), ob.cx, ob.y0 - 3.0, "Cmts.User", 1.2))
        self.items.append(W.gr_text("probe side: %s | %s %s pins | drill %.2f" % (
            self.plan["probe_side"], self.plan["pogo"]["family"], self.plan["pogo"]["mount"], self.drill),
            ob.cx, ob.y1 + 3.0, "Cmts.User", 0.8))
        if fx.get("route", True):
            self.route()
        text = W.board(self.nets, self.items, thickness=1.6, title="Test fixture for " + self.b.name,
                       rev=self.b.title_block.get("rev", ""), comment="Generated by PCB TestKit")
        return {"kicad_pcb": text, "sites": sites, "warnings": self.warnings, "channel_map": self.channel_map,
                "outline_mode": outline_mode, "outline": ob.as_dict(2), "pogo_count": len(self.pogo_xy),
                "nets": len(self.nets), "routing": self.routing, "batch": blay}

    def route(self) -> None:
        """Route the placed board with the built-in router and add its tracks and vias."""
        from .pcb import load_board
        from .router import route_board
        board = load_board("fixture.kicad_pcb", pro_path="", text=W.board(self.nets, self.items, thickness=1.6))
        opts: Dict[str, Any] = {k: self.fx.get(k) for k in ("track_mm", "power_track_mm", "clearance_mm", "via_mm",
                                                              "via_drill_mm")}
        opts["origin"] = self._grid_origin
        power = [n for n in self.nets if n == "GND" or n.endswith("VIN")]
        res = route_board(board, opts, power)
        for lay, x1, y1, x2, y2, w, net in res["tracks"]:
            self.items.append(W.segment(x1, y1, x2, y2, w, lay, self.net_ix[net]))
        for x, y, size, drill, net in res["vias"]:
            self.items.append(W.via(x, y, size, drill, self.net_ix[net]))
        st = res["stats"]
        self.routing = st
        if st["unrouted_connections"]:
            self.warnings.append("the router left %d of %d connection(s) unrouted (%s): route them in KiCad before "
                                 "ordering" % (st["unrouted_connections"], st["connections"],
                                               ", ".join(st["unrouted_nets"][:8]) +
                                               (", ..." if len(st["unrouted_nets"]) > 8 else "")))

    def _site(self, k: int, nsite: int, dx: float, dy: float, bx: float, by: float, limit: BBox,
              limit_segs=None) -> Dict[str, Any]:
        block_w = self._block_w
        pre = "S%d_" % k if nsite > 1 else ""
        gnd = "GND"
        sup = self.plan.get("supply") or {}
        vin = pre + "VIN"
        side = self.plan["probe_side"]
        tall = {t["ref"]: t for t in self.plan.get("fixture_keepouts", [])}
        if self.fx.get("draw_dut_components", True):
            for fp in self.d.components():
                if fp.side != side:
                    continue
                cb = fp.courtyard_bbox()
                x0, x1 = sorted((self.mx(cb.x0), self.mx(cb.x1)))
                self.items.append(W.gr_rect(x0 + dx, cb.y0 + dy, x1 + dx, cb.y1 + dy, "Dwgs.User", 0.05))
                self.items.append(W.gr_text(fp.ref, (x0 + x1) / 2 + dx, cb.cy + dy, "Dwgs.User", 0.6))
        # ---- pogo pins ------------------------------------------------------------
        probes_out = []
        site_pogos = []
        for p in self.plan["probes"]:
            x, y = self.mx(p["x"]) + dx, p["y"] + dy
            fnet = gnd if p["role"] == "ground" else vin if p["role"] == "supply" else pre + "TP_" + _safe(p["net"])
            iso = all(math.hypot(p["x"] - q["x"], p["y"] - q["y"]) >= 6.0 for q in self.plan["probes"] if q is not p)
            self.pogo(x, y, fnet, p["net"], p["id"], p["tip"], p["role"], iso, geo=p.get("pogo"))
            site_pogos.append((x, y))
            probes_out.append({"probe": p["id"], "x": round(x, 4), "y": round(y, 4), "fixture_net": fnet})
        # ---- cut-outs for tall probe-side parts (merged so outlines never intersect) ----
        cuts: List[BBox] = []
        if self.fx.get("cut_tall_components", True) and tall:
            boxes = []
            for ref, t in tall.items():
                bb = t["bbox"]
                x0, x1 = sorted((self.mx(bb["x_min"]), self.mx(bb["x_max"])))
                boxes.append([BBox(x0 + dx, bb["y_min"] + dy, x1 + dx, bb["y_max"] + dy), [ref], t["height_mm"]])
            merged = True
            while merged:
                merged = False
                for i in range(len(boxes)):
                    for j in range(i + 1, len(boxes)):
                        if boxes[i][0].overlaps(boxes[j][0], 1.0):
                            boxes[i][0].merge(boxes[j][0])
                            boxes[i][1] += boxes[j][1]
                            boxes[i][2] = max(boxes[i][2], boxes[j][2])
                            boxes.pop(j)
                            merged = True
                            break
                    if merged:
                        break
            inner = limit.grow(-1.0)
            for box, refs, h in boxes:
                c = BBox(max(box.x0, inner.x0), max(box.y0, inner.y0), min(box.x1, inner.x1), min(box.y1, inner.y1))
                label = ",".join(sorted(refs, key=C.natural_key))
                if not c.valid or c.w < 2.0 or c.h < 2.0:
                    self.warnings.append("no cut-out for %s: it reaches the fixture edge" % label)
                    continue
                if any(c.contains(px, py, self.pad_d / 2 + 0.5) for px, py in site_pogos):
                    self.warnings.append("no cut-out for %s: a probe sits inside or next to it - check clearance "
                                         "(part height %.0f mm)" % (label, h))
                    continue
                if limit_segs is not None and not all(point_in_segments(px, py, limit_segs) and
                                                      min(pt_seg_dist(px, py, *sg) for sg in limit_segs) >= 1.0
                                                      for px, py in c.corners()):
                    self.warnings.append("no cut-out for %s: too close to the board outline" % label)
                    continue
                cuts.append(c)
                self.items.append(W.gr_rect(c.x0, c.y0, c.x1, c.y1, "Edge.Cuts", 0.1))
                self.items.append(W.gr_text("CUT %s %.0fmm" % (label[:20], h), c.cx, c.cy, "Cmts.User", 0.8))
        # ---- DUT outline on F.Fab; L-shaped silk placement marks just outside its corners ----
        for sg in self.b.edge_segs:
            self.items.append(W.gr_line(self.mx(sg[0]) + dx, sg[1] + dy, self.mx(sg[2]) + dx, sg[3] + dy, FAB, 0.1))
        if limit_segs is None:
            o = BBox(self.bb.x0 + dx, self.bb.y0 + dy, self.bb.x1 + dx, self.bb.y1 + dy).grow(0.8)
            L = min(5.0, o.w / 4, o.h / 4)
            for cx_, cy_, sx, sy in ((o.x0, o.y0, 1, 1), (o.x1, o.y0, -1, 1), (o.x0, o.y1, 1, -1), (o.x1, o.y1, -1, -1)):
                if any(math.hypot(cx_ - px, cy_ - py) < self.pad_d / 2 + L + 0.5 for px, py in site_pogos):
                    continue
                if any(c.grow(L + 0.6).contains(cx_, cy_) for c in cuts):
                    continue
                self.items.append(W.gr_line(cx_, cy_, cx_ + sx * L, cy_, SILK, 0.15))
                self.items.append(W.gr_line(cx_, cy_, cx_, cy_ + sy * L, SILK, 0.15))
        # ---- guide pins from DUT mounting holes ---------------------------------------
        guides = 0
        seen = set()
        for fp in self.d.components():
            for pd in fp.pads:
                if pd.drill_w >= 1.5 and (pd.is_npth or C.is_mounting_hole(fp)):
                    gx, gy = self.mx(pd.x) + dx, pd.y + dy
                    key = (round(gx, 2), round(gy, 2))
                    if key in seen:
                        continue
                    seen.add(key)
                    r = pd.drill_w / 2
                    if any(math.hypot(gx - px, gy - py) < r + self.pitch / 2 + 0.3 for px, py in site_pogos):
                        continue
                    if any(c.grow(r + 0.5).contains(gx, gy) for c in cuts):
                        continue
                    self.hole(gx, gy, round(pd.drill_w, 2), "GUIDE %.1f" % pd.drill_w, prefix="G")
                    guides += 1
        if guides < 2:
            self.warnings.append("site %d: fewer than 2 guide-pin holes (DUT has %d usable holes); add tooling holes to "
                                 "the DUT or locate it with a nest" % (k, guides))
        # ---- connector block: divider rows, header, power terminal -----------------
        lay = self.block_layout(block_w)
        div_y = by + 1.0
        hdr_y = by + lay["div_rows"] * self.DIV_PITCH_Y + 2.54 + 1.0
        hx = bx + 2.0
        if self._grid_origin is None:          # header pin 1: the router aligns its grid to the 2.54 mm pins
            self._grid_origin = (hx, hdr_y + 2.54)
        pins: List[Tuple[int, str, str]] = []
        col = 0
        for h in self.plan["header"]:
            sig = h["signal"]
            if sig in ("GND", "NC"):
                pins.append((h["pin"], gnd if sig == "GND" else "", sig))
                continue
            probe = next((p for p in self.plan["probes"] if p.get("channel") == sig), None)
            if probe is None:
                pins.append((h["pin"], "", sig))
                continue
            src = vin if probe["role"] == "supply" else pre + "TP_" + _safe(probe["net"])
            div = probe.get("divider")
            if div:
                chnet = pre + sig
                row, xr = lay["slots"][col]
                rx = hx + xr
                ry = div_y + self.DIV_PITCH_Y * row
                # rotated 90 degrees, pad 2 is the upper one: probe, channel | channel, GND from top to bottom
                self.resistor(rx, ry + 1.0, div["r_top_ohm"], chnet, src)
                self.resistor(rx, ry + 4.6, div["r_bottom_ohm"], gnd, chnet)
                col += 1
                pins.append((h["pin"], chnet, sig))
            else:
                pins.append((h["pin"], src, sig))
            self.channel_map.append({"site": k, "channel": sig, "header": None, "pin": h["pin"], "dut_net": probe["net"],
                                     "divider_ratio": div["ratio"] if div else 1.0})
        href = self.header(hx, hdr_y + 2.54, pins)
        for cm in self.channel_map:
            if cm["site"] == k and cm["header"] is None:
                cm["header"] = href
        tref = None
        cursor = hx + 2.54 * (len(pins) // 2) + 3.0
        if sup.get("net") and (not self.plan.get("plate") or any(p["role"] == "supply" for p in self.plan["probes"])):
            tx = hx + 2.54 * (len(pins) // 2) + 5.0
            tref = self.terminal(tx, hdr_y + 1.27, vin, gnd)
            cursor = tx + 7.9 + 3.0
        progs = []
        for hd in self.prog_headers():               # one programming connector per header and board
            pc = self.prog_connector(cursor, hdr_y + 1.27, hd, pre, k)
            progs.append(pc)
            cursor += self._prog_shape(hd)["box"].w + 4.0
        return {"site": k, "offset": [round(dx, 3), round(dy, 3)], "header": href, "power_terminal": tref,
                "probes": probes_out, "net_prefix": pre, "program": progs}


def pogo_model_name(family: str, tip: str) -> str:
    return "pogo_%s_%s.wrl" % (family, "crown" if tip == "crown" else "spear")


def write_pogo_models(out_dir: str, plan: Dict[str, Any]) -> str:
    """VRML pogo pins (receptacle + plunger) so the fixture shows real pins in KiCad's 3D viewer."""
    from .visuals import pogo_shapes, write_wrl
    d = os.path.join(out_dir, "pcbtestkit-models")
    os.makedirs(d, exist_ok=True)
    height = float(plan["pogo"].get("probe_clearance_mm", 6.0)) + 1.5
    fams = {plan["pogo"]["family"]: float(plan["pogo"]["drill_mm"])}
    for p in plan.get("probes") or []:          # finer pins for fine-pitch programming headers
        if p.get("pogo"):
            fams.setdefault(p["pogo"]["family"], float(p["pogo"]["drill_mm"]))
    for fam, drill in sorted(fams.items()):
        for tip in ("spear", "crown"):
            write_wrl(os.path.join(d, pogo_model_name(fam, tip)), pogo_shapes(drill, height, tip))
    return d


def _ohm(v: float) -> str:
    if v >= 1e6:
        s = "%gM" % (v / 1e6)
    elif v >= 1e3:
        s = "%gk" % (v / 1e3)
    else:
        s = "%g" % v
    return s


def plate_plan(plan: Dict[str, Any], side: str) -> Dict[str, Any]:
    """The part of a two-sided test plan that one fixture plate carries: its probes, cut-outs and header."""
    sub = dict(plan)
    sub["probe_side"] = side
    sub["plate"] = side
    sub["probes"] = [p for p in plan["probes"] if (p.get("side") or side) == side]
    sub["fixture_keepouts"] = [k for k in plan.get("fixture_keepouts") or [] if k.get("side", side) == side]
    sub["header"] = (plan.get("headers") or {}).get(side) or plan["header"]
    return sub


def build_fixture(design: Design, plan: Dict[str, Any]) -> Dict[str, Any]:
    """The fixture board; with probe_side "both" the primary plate's result with every plate in ``plates``."""
    if plan.get("probe_side") == "both":
        plates: Dict[str, Any] = {}
        for side in plan.get("plates") or ["bottom", "top"]:
            W.reset_uids("fixture:%s:%s" % (design.board.name, side))
            res = FixtureBuilder(design, plate_plan(plan, side)).build()
            res["side"] = side
            plates[side] = res
        first = (plan.get("plates") or ["bottom"])[0]
        out = dict(plates[first])
        out["plates"] = plates
        out["warnings"] = [("%s plate: %s" % (s_, w)) for s_, r_ in plates.items() for w in r_["warnings"]]
        out["routing"] = merge_routing({s_: r_.get("routing") for s_, r_ in plates.items()})
        return out
    return _build_one(design, plan)


def merge_routing(per_plate: Dict[str, Optional[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """Routing stats of all fixture plates together (net names prefixed with their plate)."""
    parts = {k: v for k, v in per_plate.items() if v}
    if not parts:
        return None
    out: Dict[str, Any] = {"nets": 0, "routed_nets": 0, "unrouted_nets": [], "connections": 0, "unrouted_connections": 0,
                           "length_mm": 0.0, "vias": 0}
    for side, st in parts.items():
        for k in ("nets", "routed_nets", "connections", "unrouted_connections", "vias"):
            out[k] += st[k]
        out["length_mm"] = round(out["length_mm"] + st["length_mm"], 1)
        out["unrouted_nets"] += ["%s:%s" % (side, n) for n in st["unrouted_nets"]]
    out["plates"] = parts
    return out


def _build_one(design: Design, plan: Dict[str, Any]) -> Dict[str, Any]:
    W.reset_uids("fixture:%s" % design.board.name)
    return FixtureBuilder(design, plan).build()


def project_file(name: str) -> str:
    """Companion .kicad_pro: footprints are generated inline, so library-link checks are noise here."""
    import json
    return json.dumps({"board": {"design_settings": {"rule_severities": {"lib_footprint_issues": "ignore",
                                                                         "lib_footprint_mismatch": "ignore"}}},
                       "meta": {"filename": name + ".kicad_pro", "version": 3}}, indent=2) + "\n"


def probe_points_csv(plan: Dict[str, Any]) -> str:
    """Every probe point on the board under test, for a fixture the user designs (fixture.mode = "own").
    x/y are KiCad board coordinates of the DUT (mm, y down), seen from the top; probing from the bottom mirrors x."""
    lines = ["probe,dut_net,role,probe_side,x_mm,y_mm,target_mm,target,tip,channel,header_pin,divider_ratio,"
             "nominal_v,min_v,max_v"]
    for p in plan["probes"]:
        e = p.get("expected") or {}
        ratio = p["divider"]["ratio"] if p.get("divider") else (1.0 if p.get("channel") else "")
        lines.append("%s,%s,%s,%s,%.4f,%.4f,%s,%s,%s,%s,%s,%s,%s,%s,%s" % (
            p["id"], '"%s"' % p["net"], p["role"], p.get("side") or plan["probe_side"], p["x"], p["y"], p.get("target_mm", ""),
            '"%s"' % (p.get("label") or ""), p.get("tip", ""), p.get("channel") or "", p.get("header_pin") or "", ratio,
            "" if e.get("nominal_v") is None else e["nominal_v"], "" if e.get("min_v") is None else e["min_v"],
            "" if e.get("max_v") is None else e["max_v"]))
    return "\n".join(lines) + "\n"


def probes_csv(plan: Dict[str, Any], fixture: Dict[str, Any]) -> str:
    lines = ["site,probe,dut_net,role,x_mm,y_mm,drill_mm,tip,channel,header_pin,divider_ratio"]
    by_id = {p["id"]: p for p in plan["probes"]}
    for s in fixture["sites"]:
        for pr in s["probes"]:
            p = by_id[pr["probe"]]
            ratio = p["divider"]["ratio"] if p.get("divider") else (1.0 if p.get("channel") else "")
            lines.append("%d,%s,%s,%s,%.4f,%.4f,%.2f,%s,%s,%s,%s" % (
                s["site"], p["id"], '"%s"' % p["net"], p["role"], pr["x"], pr["y"], plan["pogo"]["drill_mm"], p["tip"],
                p.get("channel") or "", p.get("header_pin") or "", ratio))
    return "\n".join(lines) + "\n"
