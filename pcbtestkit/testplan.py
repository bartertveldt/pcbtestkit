"""Test point selection and test scheme generation.

Picks one or more probe locations per net on the probe side, respecting the
pogo pin pitch, board-edge keep-out and tall components, assigns measurement
channels (with voltage dividers where the ADC range requires it) and writes
an ordered test sequence that the Raspberry Pi / MCU runner executes.
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


@dataclass
class ProbePoint:
    id: str
    net: str
    role: str            # ground | supply | measure
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
        if self.side not in ("top", "bottom"):
            self.side = "bottom"
        self.min_target = float(self.tp.get("min_target_mm") or self.pogo["min_target"])
        from .config import pogo_geometry
        self.drill, self.pad_d, self.pitch = pogo_geometry(design.cfg)
        self.warnings: List[str] = []
        self.keepouts: List[Tuple[BBox, str]] = []
        self.tall: List[Dict[str, Any]] = []
        self._find_tall_parts()

    # ------------------------------------------------------------------ #
    def _find_tall_parts(self) -> None:
        clear = float(self.tp["probe_clearance_mm"])
        for fp in self.d.components():
            if fp.side != self.side or fp.dnp:
                continue
            h, src = self.d.height_of(fp)
            if h >= clear - 0.5:
                bb = fp.courtyard_bbox().grow(1.0)          # fixture cut-out
                self.keepouts.append((bb.grow(self.pad_d / 2 + 0.6), fp.ref))   # probes stay clear of the cut-out
                self.tall.append({"ref": fp.ref, "height_mm": round(h, 2), "height_source": src,
                                  "bbox": bb.as_dict(2)})

    def _in_keepout(self, x: float, y: float, own_ref: str = "") -> Optional[str]:
        for bb, ref in self.keepouts:
            if ref != own_ref and bb.contains(x, y):
                return ref
            if ref == own_ref and bb.contains(x, y):
                return ref
        return None

    def candidates(self) -> Dict[str, List[Candidate]]:
        out: Dict[str, List[Candidate]] = {}
        edge_keep = float(self.tp["edge_keepout_mm"])
        fine = float(self.tp["fine_pitch_exclude_mm"])
        for fp in self.d.components():
            if fp.dnp:
                continue
            is_tp = C.is_testpoint(fp)
            pitch = fp.min_pitch() if len(fp.pads) > 1 else math.inf
            for p in fp.pads:
                if not p.net or C.is_unconnected(p.net) or not p.has_copper:
                    continue
                c = self._pad_candidate(fp, p, is_tp, pitch, fine)
                if c is None:
                    continue
                if self.b.edge_segs and (self.b.edge_distance(c.x, c.y) < edge_keep or not self.b.inside(c.x, c.y)):
                    continue
                if self._in_keepout(c.x, c.y):
                    continue
                out.setdefault(p.net, []).append(c)
        if self.tp.get("allow_vias"):
            for v in self.b.vias:
                if not v.net or v.size < self.min_target or v.tented:
                    continue
                if self.b.edge_segs and self.b.edge_distance(v.x, v.y) < edge_keep:
                    continue
                if self._in_keepout(v.x, v.y):
                    continue
                out.setdefault(v.net, []).append(Candidate(v.net, v.x, v.y, v.size, "via", "via@%.2f,%.2f" % (v.x, v.y), "", 15 + v.size * 5))
        for lst in out.values():
            lst.sort(key=lambda c: -c.score)
        return out

    def _pad_candidate(self, fp: Footprint, p: Pad, is_tp: bool, pitch: float, fine: float) -> Optional[Candidate]:
        size = p.min_size if p.shape != "circle" else p.w
        if p.is_tht:
            # THT leads are reachable from the opposite side of the body only
            if fp.side == self.side and not is_tp:
                return None
            score = 60 + min(size, 2.5) * 6 + (10 if C.is_connector(fp) else 0)
            src, tip = "tht", "crown"
        elif p.kind == "smd":
            if not (p.on_copper(self.side) and p.mask_open(self.side)):
                return None
            if size < self.min_target:
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
        return Candidate(p.net, p.x, p.y, size, src, p.name, fp.ref, score)

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
                if any(math.hypot(c.x - q.x, c.y - q.y) < self.pitch for q in chosen):
                    continue
                if got and any(q.net == net and math.hypot(c.x - q.x, c.y - q.y) < 5.0 for q in chosen):
                    continue  # spread redundant ground/supply pins
                pp = ProbePoint(id="PP%02d" % (len(chosen) + 1), net=net, role=role if got == 0 or role != "supply" else "supply",
                                x=round(c.x, 4), y=round(c.y, 4), source=c.source, label=c.label,
                                target_mm=round(c.size, 3), tip="crown" if c.source == "tht" else "spear")
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
                if not cands.get(net):
                    untestable[net] = "no accessible pad on the %s side" % self.side
                else:
                    untestable[net] = "all candidate pads conflict with other probes (pitch %.2f mm)" % self.pitch
            elif got < count:
                self.warnings.append("only %d of %d %s probes placed for %s" % (got, count, role, net))
        # header pins: 1 = GND, then channels, then supply sense; GND every 10 pins for shielding
        pin = 1
        header: List[Dict[str, Any]] = [{"pin": 1, "signal": "GND"}]
        for pp in chosen:
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
        return {"points": chosen, "untestable": untestable, "skipped": skipped, "supply": supply,
                "main_ground": main_gnd, "header": header, "candidates": cands}


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
    else:
        pl = Planner(design)
        r = pl.build()
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
        "warnings": pl.warnings,
    }


def testability_findings(design: Design, plan: Dict[str, Any]):
    from .analysis import Finding
    out = []
    power = set(design.nets_of_kind("power") + design.nets_of_kind("ground"))
    unt = plan["coverage"]["untestable"]
    pw = [u for u in unt if u["net"] in power]
    sg = [u for u in unt if u["net"] not in power]
    if pw:
        out.append(Finding("TEST-POWER-ACCESS", "major", "testability", "%d power net(s) cannot be probed" % len(pw),
                           "Rails without probe access cannot be verified on the production tester.",
                           "Add a >= %.1f mm test pad on the %s side for each listed rail." % (plan["pogo"]["min_target_mm"], plan["probe_side"]),
                           nets=[u["net"] for u in pw], items=pw))
    if sg:
        out.append(Finding("TEST-SIGNAL-ACCESS", "info", "testability", "%d signal net(s) not probed" % len(sg),
                           "These nets are not covered by the bed-of-nails test.",
                           "Add test pads, raise testplan.max_channels, or accept the gap.", nets=[u["net"] for u in sg], items=sg))
    if not plan.get("supply", {}).get("net"):
        out.append(Finding("TEST-SUPPLY", "major", "testability", "No supply net identified for powered tests",
                           plan.get("supply", {}).get("reason", ""), "Set supply.net (and supply.voltage) in pcbtestkit.json."))
    if plan["fixture_keepouts"]:
        refs = [k["ref"] for k in plan["fixture_keepouts"]]
        out.append(Finding("TEST-FIXTURE-CLEARANCE", "info", "testability",
                           "%d probe-side part(s) taller than the fixture clearance" % len(refs),
                           "The fixture board gets cut-outs for these parts and no probes are placed under them.",
                           "Check the cut-outs in the generated fixture board.", refs=refs, items=plan["fixture_keepouts"]))
    return out
