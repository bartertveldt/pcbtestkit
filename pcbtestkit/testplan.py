"""Test point selection and test scheme generation.

Picks one or more probe locations per net on the probe side (or on both sides: a two-plate fixture),
respecting the pogo pin pitch, board-edge keep-out, tall components and stacked boards, assigns measurement
channels (with voltage dividers where the ADC range requires it) and writes an ordered test sequence that the
Raspberry Pi / MCU runner executes.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import classify as C
from .geometry import BBox
from .pcb import Footprint, Pad
from .project import Design

E24 = [1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0, 3.3, 3.6, 3.9, 4.3, 4.7, 5.1, 5.6, 6.2, 6.8, 7.5, 8.2, 9.1]
TESTPLAN_SCHEMA = "pcbtestkit/testplan@1"


@dataclass
class Candidate:
    net: str
    x: float
    y: float
    size: float
    source: str          # testpoint | pad_testpoint | tht | smd | via
    label: str           # e.g. "TP3.1", "R5.2", "via"
    ref: str
    score: float
    side: str = ""


@dataclass
class ProbePoint:
    id: str
    net: str
    role: str            # ground | supply | measure | program
    x: float
    y: float
    source: str
    label: str
    target_mm: float
    tip: str
    channel: Optional[str] = None
    header_pin: Optional[int] = None
    divider: Optional[Dict[str, float]] = None
    expected: Optional[Dict[str, Any]] = None
    side: str = ""            # bottom | top: the fixture plate this probe is on
    pogo: Optional[Dict[str, Any]] = None     # a finer pogo family than the plan's (fine-pitch programming headers)


def e24_at_least(v: float) -> float:
    if v <= 0:
        return 0.0
    dec = 10 ** math.floor(math.log10(v))
    for m in E24 + [10.0]:
        if m * dec >= v * 0.9999:
            return round(m * dec, 6)
    return 10 * dec


def design_divider(v_max: float, adc_max: float, r_bottom: float) -> Optional[Dict[str, float]]:
    target = adc_max * 0.9
    if v_max <= target:
        return None
    ratio_needed = v_max / target
    r_top = e24_at_least((ratio_needed - 1.0) * r_bottom)
    ratio = (r_top + r_bottom) / r_bottom
    return {"r_top_ohm": r_top, "r_bottom_ohm": r_bottom, "ratio": round(ratio, 6),
            "full_scale_v": round(adc_max * ratio, 3)}


class Planner:
    def __init__(self, design: Design, side: Optional[str] = None):
        self.d = design
        self.b = design.board
        self.tp = design.cfg["testplan"]
        self.pogo = design.cfg["pogo"]
        self.side = side or self.tp["probe_side"]
        if self.side not in ("top", "bottom", "both"):
            self.side = "bottom"
        self.sides = ["bottom", "top"] if self.side == "both" else [self.side]
        self.min_target = float(self.tp.get("min_target_mm") or self.pogo["min_target"])
        from .config import pogo_geometry
        self.drill, self.pad_d, self.pitch = pogo_geometry(design.cfg)
        self.warnings: List[str] = []
        self.keepouts: List[Tuple[BBox, str, str]] = []      # (box, ref or "stack:<name>", side)
        self.tall: List[Dict[str, Any]] = []
        self.covered: Dict[str, set] = {}                    # net -> stacked boards hiding a candidate
        from .stack import resolve
        self.stacks = resolve(design)
        self._find_tall_parts()
        self._stack_keepouts()

    # ------------------------------------------------------------------ #
    def _find_tall_parts(self) -> None:
        clear = float(self.tp["probe_clearance_mm"])
        for fp in self.d.components():
            if fp.side not in self.sides or fp.dnp:
                continue
            h, src = self.d.height_of(fp)
            if h >= clear - 0.5:
                bb = fp.courtyard_bbox().grow(1.0)          # fixture cut-out
                self.keepouts.append((bb.grow(self.pad_d / 2 + 0.6), fp.ref, fp.side))   # probes stay clear of the cut-out
                self.tall.append({"ref": fp.ref, "height_mm": round(h, 2), "height_source": src,
                                  "bbox": bb.as_dict(2), "side": fp.side})

    def _stack_keepouts(self) -> None:
        """A stacked board fitted during the test: no probes under it; a window in the fixture plate if it is taller
        than the probe clearance."""
        clear = float(self.tp["probe_clearance_mm"])
        gap = float(self.tp.get("stack_clearance_mm", 1.0))
        for st in self.stacks:
            if not st["valid"] or not st["fitted_at_test"] or st["side"] not in self.sides:
                continue
            bb = st["bbox"]
            self.keepouts.append((bb.grow(gap + self.pad_d / 2), "stack:" + st["name"], st["side"]))
            if st["height_mm"] >= clear - 0.5:
                self.tall.append({"ref": st["name"], "kind": "stack", "height_mm": st["height_mm"],
                                  "height_source": st["height_source"], "bbox": bb.grow(gap).as_dict(2),
                                  "side": st["side"], "refs": st["refs"]})

    def _in_keepout(self, x: float, y: float, side: Optional[str] = None) -> Optional[str]:
        side = side or self.sides[0]
        for bb, ref, s_ in self.keepouts:
            if s_ == side and bb.contains(x, y):
                return ref
        return None

    def _hidden(self, net: str, ref: Optional[str], side: str) -> bool:
        """Is a candidate behind keep-out ``ref``? Remembers which stacked boards hide which nets."""
        if ref is None:
            return False
        if ref.startswith("stack:"):
            self.covered.setdefault(net, set()).add("%s (%s side)" % (ref[6:], side))
        return True

    def candidates(self) -> Dict[str, List[Candidate]]:
        out: Dict[str, List[Candidate]] = {}
        edge_keep = float(self.tp["edge_keepout_mm"])
        fine = float(self.tp["fine_pitch_exclude_mm"])
        for side in self.sides:
            for fp in self.d.components():
                if fp.dnp:
                    continue
                is_tp = C.is_testpoint(fp)
                pitch = fp.min_pitch() if len(fp.pads) > 1 else math.inf
                for p in fp.pads:
                    if not p.net or C.is_unconnected(p.net) or not p.has_copper:
                        continue
                    c = self._pad_candidate(fp, p, is_tp, pitch, fine, side)
                    if c is None:
                        continue
                    if self.b.edge_segs and (self.b.edge_distance(c.x, c.y) < edge_keep or not self.b.inside(c.x, c.y)):
                        continue
                    if self._hidden(p.net, self._in_keepout(c.x, c.y, side), side):
                        continue
                    out.setdefault(p.net, []).append(c)
            if self.tp.get("allow_vias"):
                for v in self.b.vias:
                    if not v.net or v.size < self.min_target or v.tented:
                        continue
                    if self.b.edge_segs and self.b.edge_distance(v.x, v.y) < edge_keep:
                        continue
                    if self._hidden(v.net, self._in_keepout(v.x, v.y, side), side):
                        continue
                    out.setdefault(v.net, []).append(Candidate(v.net, v.x, v.y, v.size, "via", "via@%.2f,%.2f" % (v.x, v.y), "",
                                                               15 + v.size * 5, side))
        for lst in out.values():
            lst.sort(key=lambda c: -c.score)
        return out

    def _pad_candidate(self, fp: Footprint, p: Pad, is_tp: bool, pitch: float, fine: float,
                       side: Optional[str] = None, min_target: Optional[float] = None) -> Optional[Candidate]:
        side = side or self.sides[0]
        min_target = self.min_target if min_target is None else min_target
        size = p.min_size if p.shape != "circle" else p.w
        if p.is_tht:
            # THT leads are reachable from the opposite side of the body only
            if fp.side == side and not is_tp:
                return None
            score = 60 + min(size, 2.5) * 6 + (10 if C.is_connector(fp) else 0)
            src, tip = "tht", "crown"
        elif p.kind == "smd":
            if not (p.on_copper(side) and p.mask_open(side)):
                return None
            if size < min_target:
                return None
            if not is_tp and "pad_prop_testpoint" not in p.props:
                if not self.tp.get("allow_component_pads", True):
                    return None
                if pitch < fine:
                    return None
                score = 25 + min(size, 2.0) * 8
                if len(fp.pads) <= 3 and (C.is_resistor(fp) or C.is_capacitor(fp) or C.is_inductor(fp)):
                    score += 10
                if C.is_ic(fp):
                    score -= 15
            else:
                score = 100 + size * 5
            src, tip = "smd", "spear"
        else:
            return None
        if is_tp:
            src = "testpoint"
            score = max(score, 100 + size * 5)
        elif "pad_prop_testpoint" in p.props:
            src = "pad_testpoint"
            score = max(score, 95 + size * 5)
        return Candidate(p.net, p.x, p.y, size, src, p.name, fp.ref, score, side)

    # ------------------------------------------------------------------ #
    def select_nets(self, cands: Dict[str, List[Candidate]]):
        """Return ordered list of (net, role, count) and a dict of skipped nets with reasons."""
        tp = self.tp
        pads_by_net = self.b.pads_by_net()
        cfg_nets = self.d.cfg.get("nets") or {}
        include = set(tp.get("include_nets") or [])
        exclude = set(tp.get("exclude_nets") or []) | {n for n, o in cfg_nets.items() if o.get("test") is False}
        supply = self.d.supply()
        plan: List[Tuple[str, str, int]] = []
        skipped: Dict[str, str] = {}

        grounds = [n for n in self.d.nets_of_kind("ground") if n not in exclude]
        main_gnd = None
        if grounds:
            main_gnd = max(grounds, key=lambda n: (C.base_net(n).upper() == "GND", len(pads_by_net.get(n, []))))
        if supply.get("net") and supply["net"] not in exclude:
            plan.append((supply["net"], "supply", int(tp["supply_points"])))
        if main_gnd:
            plan.append((main_gnd, "ground", int(tp["gnd_points"])))
        for n in grounds:
            if n != main_gnd:
                plan.append((n, "measure", 1))
        for n in self.d.nets_of_kind("power"):
            if n == supply.get("net") or n in exclude:
                continue
            plan.append((n, "measure", 1))

        policy = tp.get("signals", "auto")
        connector_nets = set()
        tp_nets = set()
        for fp in self.d.components():
            if C.is_connector(fp):
                connector_nets |= fp.nets()
            if C.is_testpoint(fp):
                tp_nets |= fp.nets()
        tp_nets |= {p.net for p in self.b.all_pads() if "pad_prop_testpoint" in p.props}
        signals = []
        for n in self.d.nets_of_kind("signal"):
            if n in exclude:
                skipped[n] = "excluded in config"
                continue
            npads = len(pads_by_net.get(n, []))
            if npads < 2 and n not in include and n not in tp_nets:
                continue
            wanted = (policy == "all" or n in include or n in tp_nets or
                      (policy == "auto" and (n in connector_nets or not C.base_net(n).startswith("Net-("))))
            if not wanted:
                skipped[n] = "not selected by signals policy '%s'" % policy
                continue
            signals.append(n)
        signals.sort(key=lambda n: (n not in include, n not in tp_nets, len(cands.get(n, [])), C.natural_key(n)))
        plan += [(n, "measure", 1) for n in signals]
        for n in exclude:
            skipped.setdefault(n, "excluded in config")
        return plan, skipped, supply, main_gnd

    def build(self) -> Dict[str, Any]:
        cands = self.candidates()
        plan, skipped, supply, main_gnd = self.select_nets(cands)
        chosen: List[ProbePoint] = []
        untestable: Dict[str, str] = {}
        maxch = int(self.tp["max_channels"])
        nch = 0
        tp = self.tp
        adc_max = float(tp["adc_max_v"])
        known = [abs(self.d.net_spec(n).get("max_v") or 0) for n in self.d.nets_of_kind("power")]
        sig_vmax = tp.get("max_signal_voltage")
        if sig_vmax is None:
            pos = [v for v in known if 0 < v <= 5.5]
            sig_vmax = max(pos) if pos else adc_max
        fx = self.d.cfg["fixture"]
        for net, role, count in plan:
            needs_ch = role in ("measure", "supply")
            if needs_ch and nch >= maxch:
                untestable[net] = "channel limit (%d) reached" % maxch
                continue
            got = 0
            for c in cands.get(net, []):
                if got >= count:
                    break
                if any(q.side == c.side and math.hypot(c.x - q.x, c.y - q.y) < self.pitch for q in chosen):
                    continue
                if got and any(q.net == net and q.side == c.side and math.hypot(c.x - q.x, c.y - q.y) < 5.0 for q in chosen):
                    continue  # spread redundant ground/supply pins
                pp = ProbePoint(id="PP%02d" % (len(chosen) + 1), net=net, role=role if got == 0 or role != "supply" else "supply",
                                x=round(c.x, 4), y=round(c.y, 4), source=c.source, label=c.label,
                                target_mm=round(c.size, 3), tip="crown" if c.source == "tht" else "spear", side=c.side)
                if needs_ch and got == 0:
                    nch += 1
                    pp.channel = "CH%02d" % nch
                    spec = self.d.net_spec(net)
                    vmax = spec.get("max_v")
                    if vmax is None:
                        logic = C.net_voltage(net) if spec["kind"] == "signal" else None
                        if logic and 0 < logic <= sig_vmax:
                            vmax = logic * 1.1          # e.g. UART_TX_1V8: logic level from the name
                        else:
                            vmax = sig_vmax if spec["kind"] == "signal" else max([sig_vmax] + known)
                    rb = float(fx["divider_r_bottom_rail"] if spec["kind"] in ("power", "ground") or role == "supply"
                               else fx["divider_r_bottom_signal"])
                    if fx.get("dividers", True):
                        pp.divider = design_divider(abs(float(vmax)), adc_max, rb)
                    pp.expected = {k: spec.get(k) for k in ("nominal_v", "min_v", "max_v", "voltage_source")}
                chosen.append(pp)
                got += 1
            if got == 0:
                if not cands.get(net) and net in self.covered:
                    untestable[net] = "only reachable under stacked board %s" % ", ".join(sorted(self.covered[net]))
                elif not cands.get(net):
                    untestable[net] = "no accessible pad on %s" % ("either side" if self.side == "both" else
                                                                     "the %s side" % self.side)
                else:
                    untestable[net] = "all candidate pads conflict with other probes (pitch %.2f mm)" % self.pitch
            elif got < count:
                self.warnings.append("only %d of %d %s probes placed for %s" % (got, count, role, net))
        program = self._program(cands, chosen)
        for pp in chosen:                    # reached for programming (no measurement channel): no longer untestable
            if pp.role == "program":
                untestable.pop(pp.net, None)
        headers: Dict[str, List[Dict[str, Any]]] = {}
        if self.side == "both":
            # one fixture plate per side that has probes, each with a header for the channels of its own probes
            count = {s_: sum(1 for pp in chosen if pp.side == s_) for s_ in self.sides}
            plates = [s_ for s_ in sorted(self.sides, key=lambda s_: -count[s_]) if count[s_]] or self.sides[:1]
            for s_ in plates:
                headers[s_] = _header([pp for pp in chosen if pp.side == s_])
            header = headers[plates[0]]
            for s_ in self.sides:
                if s_ not in plates:
                    self.warnings.append("probe_side both: every probe fits on the %s side; no %s fixture plate is "
                                         "made" % (plates[0], s_))
        else:
            plates = list(self.sides)
            header = _header(chosen)
        return {"points": chosen, "untestable": untestable, "skipped": skipped, "supply": supply,
                "main_ground": main_gnd, "header": header, "headers": headers, "plates": plates, "candidates": cands,
                "program": program}

    # ------------------------------------------------------------------ #
    def _program(self, cands: Dict[str, List[Candidate]], chosen: List[ProbePoint]) -> List[Dict[str, Any]]:
        """Probe every pin of the programming headers, so a programmer on the fixture reaches the board.

        A pin whose net already has a probe (a rail, GND, a measured signal) is wired to that probe; any other pin
        gets a "program" probe, on the header's own pad first. Pads closer than the plan's pogo spacing (1.27 mm
        Tag-Connect / Cortex headers) get a finer pogo family."""
        from .config import pogo_for_pitch
        from .program import selected
        out: List[Dict[str, Any]] = []
        spacing: Dict[str, float] = {}

        def gap(q: ProbePoint) -> float:
            return spacing.get(q.id, self.pitch)

        edge_keep = float(self.tp["edge_keepout_mm"])
        fine = float(self.tp["fine_pitch_exclude_mm"])
        for h in selected(self.d):
            fp = self.b.by_ref.get(h["ref"])
            pitch_h = h.get("pitch_mm") or 99.0
            geo = pogo_for_pitch(self.d.cfg, pitch_h) if pitch_h < self.pitch - 1e-6 else None
            pins, missing = [], []
            for pin in h["pins"]:
                rec = {"pad": pin["pad"], "net": pin["net"], "signal": pin["signal"], "probe": None}
                net = pin["net"]
                if not net or C.is_unconnected(net):
                    pins.append(rec)
                    continue
                ex = next((q for q in chosen if q.net == net), None)
                if ex is not None:
                    rec["probe"] = ex.id
                    pins.append(rec)
                    continue
                opts: List[Tuple[Candidate, Optional[Dict[str, Any]]]] = []
                pad = fp.pad(pin["pad"]) if fp is not None else None
                if pad is not None:
                    for side in self.sides:
                        c = self._pad_candidate(fp, pad, True, 99.0, fine, side,
                                                min_target=geo["min_target_mm"] if geo else None)
                        if c is None or (self.b.edge_segs and (self.b.edge_distance(c.x, c.y) < edge_keep
                                                               or not self.b.inside(c.x, c.y))):
                            continue
                        if self._in_keepout(c.x, c.y, side):
                            continue
                        opts.append((c, geo))
                opts += [(c, None) for c in cands.get(net, []) if not (fp is not None and c.ref == fp.ref)]
                placed = None
                for c, g in opts:
                    sp = g["min_spacing_mm"] if g else self.pitch
                    if any(q.side == c.side and math.hypot(c.x - q.x, c.y - q.y) < max(sp, gap(q)) - 1e-6 for q in chosen):
                        continue
                    pp = ProbePoint(id="PP%02d" % (len(chosen) + 1), net=net, role="program", x=round(c.x, 4),
                                    y=round(c.y, 4), source=c.source, label=c.label, target_mm=round(c.size, 3),
                                    tip="crown" if c.source == "tht" else "spear", side=c.side,
                                    pogo={k: g[k] for k in ("family", "mount", "drill_mm", "pad_mm", "min_spacing_mm")} if g else None)
                    chosen.append(pp)
                    spacing[pp.id] = sp
                    placed = pp
                    break
                if placed is None:
                    missing.append("%s (%s)" % (pin["pad"], pin["signal"] or C.base_net(net)))
                else:
                    rec["probe"] = placed.id
                pins.append(rec)
            if missing:
                self.warnings.append("programming header %s: no probe fits pin(s) %s; program through test points on "
                                     "those nets or probe the %s side" % (h["ref"], ", ".join(missing),
                                                                          "other" if self.side != "both" else "board's"))
            if fp is not None and fp.side in self.sides and fp.is_smd and not (fp.board_only or fp.exclude_from_bom):
                self.warnings.append("programming header %s is fitted on the probed side: the pogo pins land on its "
                                     "pads, under the connector. Leave it unfitted in production (DNP) or probe from "
                                     "the other side" % h["ref"])
            rec_h = {k: h[k] for k in ("ref", "value", "footprint", "protocol", "side", "why", "pitch_mm")}
            rec_h.update(pins=pins, missing=missing, pogo=geo["family"] if geo else None)
            out.append(rec_h)
        return out


def _header(points: List[ProbePoint]) -> List[Dict[str, Any]]:
    """Header pins: 1 = GND, then the channels; GND every 10 pins for shielding; even pin count. Sets header_pin."""
    pin = 1
    header: List[Dict[str, Any]] = [{"pin": 1, "signal": "GND"}]
    for pp in points:
        if pp.channel:
            pin += 1
            if pin % 10 == 0:
                header.append({"pin": pin, "signal": "GND"})
                pin += 1
            pp.header_pin = pin
            header.append({"pin": pin, "signal": pp.channel, "net": pp.net})
    pin += 1
    header.append({"pin": pin, "signal": "GND"})
    if pin % 2:
        pin += 1
        header.append({"pin": pin, "signal": "NC"})
    return header


# --------------------------------------------------------------------------- #
# Test sequence
# --------------------------------------------------------------------------- #
def _visual_items(design: Design, findings) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    comps = design.components()
    pol = sorted([fp.ref for fp in comps if C.is_polarized(fp) and not fp.dnp], key=C.natural_key)
    if pol:
        items.append({"id": "VIS-POLARITY", "check": "Polarity/orientation correct", "refs": pol})
    ics = sorted([fp.ref for fp in comps if C.is_ic(fp) and not fp.dnp], key=C.natural_key)
    if ics:
        items.append({"id": "VIS-PIN1", "check": "IC pin-1 orientation matches silkscreen", "refs": ics})
    dnp = sorted([fp.ref for fp in comps if fp.dnp], key=C.natural_key)
    if dnp:
        items.append({"id": "VIS-DNP", "check": "Not populated (DNP) parts are absent", "refs": dnp})
    tht = sorted([fp.ref for fp in comps if fp.is_tht and not fp.dnp and not C.is_mounting_hole(fp)], key=C.natural_key)
    if tht:
        items.append({"id": "VIS-THT", "check": "Through-hole joints filled, leads clipped <= 1.5 mm", "refs": tht})
    fine = sorted([fp.ref for fp in comps if fp.is_smd and len(fp.pads) >= 4 and fp.min_pitch() <= 0.65], key=C.natural_key)
    if fine:
        items.append({"id": "VIS-BRIDGES", "check": "No solder bridges on fine-pitch parts", "refs": fine})
    for f in findings or []:
        if f.id == "ASM-MLCC-EDGE":
            items.append({"id": "VIS-MLCC-CRACK", "check": "No cracked MLCCs near the board edge", "refs": sorted(set(f.refs))})
    from .stack import resolve
    for st in resolve(design):
        if st["valid"] and st["fitted_at_test"]:
            items.append({"id": "VIS-STACK-%s" % st["name"].upper().replace(" ", "_"),
                          "check": "Stacked board %s fully seated, nothing trapped under it" % st["name"], "refs": st["refs"]})
    for mc in design.cfg["testplan"].get("manual_checks") or []:
        if isinstance(mc, dict) and mc.get("check"):
            items.append({"id": mc.get("id") or "VIS-USER-%d" % (len(items) + 1), "check": mc["check"],
                          "refs": mc.get("refs", [])})
    return items


def _side_eval(r: Dict[str, Any], design: Design) -> Dict[str, Any]:
    power = set(design.nets_of_kind("power") + design.nets_of_kind("ground"))
    nets = {p.net for p in r["points"]}
    sup = (r.get("supply") or {}).get("net")
    tps = sum(1 for p in r["points"] if p.source in ("testpoint", "pad_testpoint"))
    comp = sum(1 for p in r["points"] if p.source == "smd")
    ev = {"power_nets": len(nets & power), "nets": len(nets), "test_pads": tps, "component_pads": comp,
          "supply_reached": bool(sup and sup in nets)}
    # rails count most, dedicated test pads show designer intent, probing component joints is a last resort
    ev["score"] = 3 * ev["power_nets"] + ev["nets"] + 2 * tps - comp + (10 if ev["supply_reached"] else 0)
    return ev


def build_testplan(design: Design, findings=None) -> Dict[str, Any]:
    side_cfg = design.cfg["testplan"].get("probe_side", "auto")
    side_note = ""
    side_eval: Dict[str, Any] = {}
    if side_cfg == "auto":
        evals = {}
        for side in ("bottom", "top"):
            p_ = Planner(design, side)
            evals[side] = (p_, p_.build())
            side_eval[side] = _side_eval(evals[side][1], design)
        chosen = "top" if side_eval["top"]["score"] > side_eval["bottom"]["score"] else "bottom"
        pl, r = evals[chosen]
        side_note = "auto: %s side scored higher (bottom %d, top %d)" % (
            chosen, side_eval["bottom"]["score"], side_eval["top"]["score"])
        power_ = set(design.nets_of_kind("power") + design.nets_of_kind("ground"))
        there = {p_.net for p_ in evals["top" if chosen == "bottom" else "bottom"][1]["points"]}
        both_reaches = sorted((n for n in r["untestable"] if n in power_ and n in there), key=C.natural_key)
    else:
        pl = Planner(design)
        r = pl.build()
        both_reaches = []
    points: List[ProbePoint] = r["points"]
    tp = design.cfg["testplan"]
    sup = r["supply"]
    by_net = {}
    for p in points:
        if p.channel:
            by_net[p.net] = p
    main_gnd = r["main_ground"]
    steps: List[Dict[str, Any]] = []
    steps.append({"id": "S1", "name": "Visual inspection", "type": "manual", "power": "off",
                  "items": _visual_items(design, findings)})
    shorts = []
    for p in points:
        if p.channel and design.net_kind(p.net) == "power" and main_gnd:
            shorts.append({"id": "R_%s" % C.base_net(p.net), "channel": p.channel, "net": p.net, "reference_net": main_gnd,
                           "min_ohm": float(tp["short_test_min_ohm"])})
    if shorts:
        steps.append({"id": "S2", "name": "Unpowered short check (rails to %s)" % main_gnd, "type": "resistance",
                      "power": "off", "optional": True, "checks": shorts,
                      "note": "Needs the source-resistor circuit described in runner/README; skipped otherwise."})
    sup_point = by_net.get(sup.get("net") or "")
    if sup.get("net"):
        cur = {"limit_a": (design.cfg["supply"] or {}).get("current_limit_a"),
               "max_idle_a": (design.cfg["supply"] or {}).get("max_idle_current_a"),
               "min_idle_a": (design.cfg["supply"] or {}).get("min_idle_current_a")}
        steps.append({"id": "S3", "name": "Apply power (%s = %s V)" % (sup["net"], sup.get("voltage")), "type": "power",
                      "action": "on", "net": sup["net"], "voltage": sup.get("voltage"), "settle_ms": int(tp["settle_ms"]),
                      "current": cur, "abort_on_fail": True,
                      "checks": ([{"id": "V_SUPPLY", "channel": sup_point.channel, "net": sup["net"],
                                   **{k: v for k, v in (sup_point.expected or {}).items() if k in ("min_v", "max_v")}}]
                                 if sup_point and sup_point.expected else [])})
    rails = []
    for p in points:
        if not p.channel or p.role == "supply":
            continue
        kind = design.net_kind(p.net)
        if kind in ("power", "ground"):
            e = p.expected or {}
            chk = {"id": "V_%s" % C.base_net(p.net), "channel": p.channel, "net": p.net, "min_v": e.get("min_v"),
                   "max_v": e.get("max_v"), "nominal_v": e.get("nominal_v")}
            if e.get("min_v") is None:
                chk["learn"] = True
            rails.append(chk)
    if rails:
        steps.append({"id": "S4", "name": "Rail voltages", "type": "voltage", "power": "on", "checks": rails})
    prog_cfg = design.cfg.get("program") or {}
    prog_headers = r.get("program") or []
    if prog_headers:
        refs = ", ".join("%s (%s)" % (h["ref"], h["protocol"]) for h in prog_headers)
        steps.append({"id": "SP", "name": "Program the board through %s" % refs, "type": "program", "power": "on",
                      "headers": [h["ref"] for h in prog_headers], "command": list(prog_cfg.get("command") or []),
                      "timeout_s": float(prog_cfg.get("timeout_s", 180)),
                      "instruction": "Connect the programmer to the fixture's PROG connector of this board and program it."})
    sigs = []
    for p in points:
        if p.channel and design.net_kind(p.net) == "signal":
            e = p.expected or {}
            chk = {"id": "V_%s" % C.base_net(p.net).replace("/", "_"), "channel": p.channel, "net": p.net,
                   "min_v": e.get("min_v"), "max_v": e.get("max_v")}
            if e.get("min_v") is None:
                chk["learn"] = True
                chk["learn_tolerance_v"] = float(tp["signal_tolerance_v"])
            sigs.append(chk)
    if sigs:
        steps.append({"id": "S5", "name": "Signal levels (idle)", "type": "voltage", "power": "on", "checks": sigs})
    for i, cs in enumerate(tp.get("custom_steps") or [], 1):
        st = json.loads(json.dumps(cs))
        st.setdefault("id", "C%d" % i)
        for chk in st.get("checks", []) or []:   # channels are re-resolved: probe numbering may have changed
            p = by_net.get(chk.get("net"))
            chk["channel"] = p.channel if p else None
            if p is None:
                chk["unavailable"] = "net is no longer probed"
                pl.warnings.append("custom step %s: %s has no probe channel any more" % (st["id"], chk.get("net")))
        st["custom"] = True
        steps.append(st)
    if sup.get("net"):
        steps.append({"id": "S9", "name": "Remove power", "type": "power", "action": "off"})

    total_nets = [n for n, pads in design.board.pads_by_net().items() if not C.is_unconnected(n) and len(pads) >= 2]
    tested = {p.net for p in points}
    power = design.nets_of_kind("power") + design.nets_of_kind("ground")
    cov = {"nets_total": len(total_nets), "nets_probed": len([n for n in total_nets if n in tested]),
           "power_nets": len(power), "power_nets_probed": len([n for n in power if n in tested]),
           "untestable": [{"net": n, "reason": why} for n, why in sorted(r["untestable"].items(), key=lambda kv: C.natural_key(kv[0]))]}
    cov["percent"] = round(100.0 * cov["nets_probed"] / cov["nets_total"], 1) if cov["nets_total"] else 0.0
    pogo = design.cfg["pogo"]
    mount = tp.get("pogo_mount", "receptacle")
    from .batch import layout as batch_layout
    from .program import connector_for
    from .stack import brief
    bb = design.board.outline_bbox()
    batch = batch_layout(design.cfg, bb.w, bb.h)
    program = None
    if prog_headers:
        conns = []
        for h in prog_headers:
            kind, warn = connector_for(h, prog_cfg.get("connector", "same"))
            h["connector"] = kind
            conns += warn
        pl.warnings.extend(conns)
        program = {"headers": prog_headers, "connector": prog_cfg.get("connector", "same"),
                   "command": list(prog_cfg.get("command") or []), "timeout_s": float(prog_cfg.get("timeout_s", 180))}
    return {
        "schema": TESTPLAN_SCHEMA,
        "board": {"name": design.board.name, "revision": design.board.title_block.get("rev", "")},
        "probe_side": pl.side,
        "probe_side_reason": side_note or "set in config",
        "probe_side_eval": side_eval or None,
        "pogo": {"family": tp.get("pogo", "P75"), "mount": mount, "pitch_mm": pogo["pitch"],
                 "drill_mm": pl.drill, "pad_mm": pl.pad_d, "min_spacing_mm": pl.pitch,
                 "min_target_mm": pl.min_target, "probe_clearance_mm": tp["probe_clearance_mm"]},
        "adc": {"max_input_v": float(tp["adc_max_v"])},
        "supply": sup,
        "main_ground": main_gnd,
        "probes": [asdict(p) for p in points],
        "header": r["header"],
        "sequence": steps,
        "coverage": cov,
        "fixture_keepouts": pl.tall,
        "plates": r["plates"],
        "headers": r["headers"] or None,
        "probe_side_hint": {"both_sides_reach": both_reaches} if both_reaches else None,
        "stack": [brief(st) for st in pl.stacks],
        "after_test": [{"fit": st["name"], "refs": st["refs"], "side": st["side"]} for st in pl.stacks
                       if st["valid"] and not st["fitted_at_test"]],
        "batch": batch,
        "program": program,
        "warnings": pl.warnings + ["stacked board %s: %s" % (st["name"], pr) for st in pl.stacks for pr in st["problems"]],
    }


def fixture_side(plan: Dict[str, Any]) -> str:
    """The side the (primary) fixture plate probes: probe_side, or the first plate when probing both sides."""
    if plan.get("probe_side") == "both":
        return (plan.get("plates") or ["bottom"])[0]
    return plan.get("probe_side") or "bottom"


def testability_findings(design: Design, plan: Dict[str, Any]):
    from .analysis import Finding
    out = []
    power = set(design.nets_of_kind("power") + design.nets_of_kind("ground"))
    unt = plan["coverage"]["untestable"]
    under = [u for u in unt if u["reason"].startswith("only reachable under stacked board")]
    unt = [u for u in unt if u not in under]
    pw = [u for u in unt if u["net"] in power]
    sg = [u for u in unt if u["net"] not in power]
    hint = (plan.get("probe_side_hint") or {}).get("both_sides_reach") or []
    if pw:
        where = "on either side" if plan["probe_side"] == "both" else "on the %s side" % plan["probe_side"]
        fix = "Add a >= %.1f mm test pad %s for each listed rail." % (plan["pogo"]["min_target_mm"], where)
        if hint:
            fix += " The other side reaches %s: probe both sides (testplan.probe_side = \"both\", two fixture plates)." % (
                ", ".join(hint[:6]))
        out.append(Finding("TEST-POWER-ACCESS", "major", "testability", "%d power net(s) cannot be probed" % len(pw),
                           "Rails without probe access cannot be verified on the production tester.", fix,
                           nets=[u["net"] for u in pw], items=pw))
    if under:
        names = sorted({n for st in plan.get("stack") or [] for n in [st["name"]]
                        if any(st["name"] in u["reason"] for u in under)})
        sev = "major" if any(u["net"] in power for u in under) else "info"
        out.append(Finding("TEST-STACK-ACCESS", sev, "testability",
                           "%d net(s) only reachable under stacked board %s" % (len(under), ", ".join(names)),
                           "A stacked board fitted during the test covers the only probe targets of these nets.",
                           "Probe from both sides (testplan.probe_side = \"both\"), test before fitting the stacked board "
                           "(stack[].fitted_at_test = false), or add test pads outside its outline.",
                           nets=[u["net"] for u in under], items=under))
    if sg:
        out.append(Finding("TEST-SIGNAL-ACCESS", "info", "testability", "%d signal net(s) not probed" % len(sg),
                           "These nets are not covered by the bed-of-nails test.",
                           "Add test pads, raise testplan.max_channels, or accept the gap.", nets=[u["net"] for u in sg], items=sg))
    if not plan.get("supply", {}).get("net"):
        out.append(Finding("TEST-SUPPLY", "major", "testability", "No supply net identified for powered tests",
                           plan.get("supply", {}).get("reason", ""), "Set supply.net (and supply.voltage) in pcbtestkit.json."))
    tall = [k for k in plan["fixture_keepouts"] if k.get("kind") != "stack"]
    if tall:
        refs = [k["ref"] for k in tall]
        out.append(Finding("TEST-FIXTURE-CLEARANCE", "info", "testability",
                           "%d probe-side part(s) taller than the fixture clearance" % len(refs),
                           "The fixture board gets cut-outs for these parts and no probes are placed under them.",
                           "Check the cut-outs in the generated fixture board.", refs=refs, items=tall))
    for st in plan.get("stack") or []:
        if not st.get("valid"):
            continue
        est = [k for k in ("outline_source", "height_source") if str(st.get(k, "")).startswith("estimate")]
        if est:
            out.append(Finding("TEST-STACK-ESTIMATE", "info", "testability",
                               "Stacked board %s: %s estimated" % (st["name"], " and ".join(
                                   "outline" if k == "outline_source" else "height" for k in est)),
                               "The fixture window and the probe keep-out use %s." % "; ".join(
                                   "%s = %s" % (k.split("_")[0], st[k]) for k in est),
                               "Set stack[].outline (board mm) and height_mm for %s (dialog: Stacked boards tab)." % st["name"],
                               refs=st.get("refs") or []))
    return out
