"""Which test checks need the board's firmware, so the rest can run while the board is being programmed.

A board is tested in phases:

  unpowered        visual check, short check (no power)
  power            supply on, supply voltage and current
  program          the microcontroller(s) are programmed; every board of a batch at the same time
  during_program   checks that do not depend on the firmware, measured while the programmers run
  after_program    checks that need the firmware running: its GPIOs, rails it switches on, buses, functional steps
  end              supply off

What depends on the firmware is worked out from the design. The programmed parts are the ICs on the programming
header's signal nets (SWDIO/SWCLK, TCK/TMS, UPDI, ...). A signal net on one of their pins, directly or through one
series resistor (an LED, a pull-up to an output), is set by the firmware. A rail is set by the firmware when the
regulator or load switch that makes it has its enable pin on such a net. Anything else (regulator outputs, dividers,
references, signals between other parts) reads the same before and after programming. A check can force it with
``"needs_firmware": true/false``; custom steps and bus checks need the firmware unless they say otherwise.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

from . import classify as C

# programming signals that identify the part a header programs (power and ground pins do not)
_PROG_SIGNALS = {"SWDIO", "SWCLK", "SWO", "TDI", "TDO", "TCK", "TMS", "NTRST", "UPDI", "PDI", "MOSI", "MISO", "SCK",
                 "PGD", "PGC", "TXD", "RXD", "BOOT", "NRST", "PDI_CLK"}
_OUT_PIN = re.compile(r"^(V?OUT\d*|VO\d*|SW\d*|LX\d*|VSW|PH)$", re.I)
_EN_PIN = re.compile(r"^(~?\{?EN\}?\w*|CE|~?\{?SHDN\}?|ON|EN/UVLO|~?\{?ON\}?|CTRL|RUN|PG?_?EN)$", re.I)
_POWER_PIN = re.compile(r"^(V(DD|CC|BAT|IN|SS|REF|CAP|USB|IO|DDA|DDIO|REG)\w*|IOVDD|DVDD|AVDD|GND\w*|AGND|PGND|"
                        r"EPAD|EP|PAD|NC|DNC)$", re.I)


def _pf(pad) -> str:
    return (getattr(pad, "pinfunction", "") or "").strip()


def targets(design: Any, headers: List[Dict[str, Any]]) -> List[str]:
    """The parts the programming headers program: ICs on at least two of a header's programming signal nets (or
    on its only one, for single-wire UPDI/PDI)."""
    out: List[str] = []
    for h in headers or []:
        nets = {p["net"] for p in h.get("pins") or [] if p.get("net") and str(p.get("signal", "")).upper() in _PROG_SIGNALS}
        if not nets:
            continue
        need = 1 if len(nets) == 1 else 2
        for fp in design.components():
            if fp.ref == h.get("ref") or fp.ref in out or len(fp.pads) < 6:
                continue                                    # the header itself, passives, small parts
            if len(fp.nets() & nets) >= need:
                out.append(fp.ref)
    return sorted(out, key=C.natural_key)


def firmware_nets(design: Any, parts: List[str]) -> Dict[str, str]:
    """Nets whose level the firmware of ``parts`` sets, with the reason."""
    out: Dict[str, str] = {}
    if not parts:
        return out
    b = design.board
    kind = design.net_kind
    for ref in parts:
        fp = b.by_ref.get(ref)
        if fp is None:
            continue
        for pd in fp.pads:
            if pd.net and kind(pd.net) == "signal" and not C.is_unconnected(pd.net) and not _POWER_PIN.match(_pf(pd)):
                out.setdefault(pd.net, "%s pin %s%s" % (ref, pd.number, " (%s)" % _pf(pd) if _pf(pd) else ""))
    # one series resistor further: an LED, a transistor base, a level shifter input driven by a GPIO
    for fp in design.components():
        if len(fp.pads) == 2 and C.has_prefix(fp, "R") and not C.has_prefix(fp, "RV", "RT"):
            a, c = fp.pads[0].net, fp.pads[1].net
            for x, y in ((a, c), (c, a)):
                if x in out and y and y not in out and kind(y) == "signal" and not C.is_unconnected(y):
                    out[y] = "through %s from %s" % (fp.ref, out[x])
    # rails switched on by the firmware: the regulator / load switch that makes them has its enable on such a net
    power = set(design.nets_of_kind("power"))
    for fp in design.components():
        outs = [pd.net for pd in fp.pads if pd.net in power and _OUT_PIN.match(_pf(pd))]
        ens = [pd.net for pd in fp.pads if pd.net and _EN_PIN.match(_pf(pd))]
        for rail in outs:
            for en in ens:
                if en in out:
                    out.setdefault(rail, "%s makes it, enabled by %s (%s)" % (fp.ref, en, out[en]))
    return out


def needs_firmware(step: Dict[str, Any], check: Optional[Dict[str, Any]], fw: Dict[str, str]) -> bool:
    if check is not None and "needs_firmware" in check:
        return bool(check["needs_firmware"])
    if "needs_firmware" in step:
        return bool(step["needs_firmware"])
    if step.get("type") in ("bus", "uart_expect", "command", "gpio") or step.get("custom"):
        return True
    if check is not None:
        return check.get("net") in fw
    return False


def phase_sequence(steps: List[Dict[str, Any]], fw: Dict[str, str], has_program: bool) -> List[Dict[str, Any]]:
    """The sequence in phase order, each step with its ``phase``. With programming, every voltage step is split
    into the checks that can run while the board is programmed and those that need the firmware (id + "F")."""
    unpowered, power, during, prog, after, end = [], [], [], [], [], []
    for st in steps:
        t = st.get("type")
        if t == "program":
            prog.append(dict(st, phase="program"))
        elif t == "power" and st.get("action") == "off":
            end.append(dict(st, phase="end"))
        elif t == "power":
            power.append(dict(st, phase="power"))
        elif st.get("power") == "off" or t in ("resistance",) or (t == "manual" and not st.get("custom")):
            unpowered.append(dict(st, phase="unpowered"))
        elif t == "voltage" and st.get("checks"):
            pre = [c for c in st["checks"] if not needs_firmware(st, c, fw)]
            post = [dict(c, firmware=fw.get(c.get("net"), "set in the plan")) for c in st["checks"] if needs_firmware(st, c, fw)]
            if pre:
                during.append(dict(st, checks=pre, phase="during_program" if has_program else "powered"))
            if post:
                if pre:
                    after.append(dict(st, id=st["id"] + "F", name=st["name"].replace(" (idle)", "") +
                                      " with the firmware running", checks=post,
                                      phase="after_program" if has_program else "powered"))
                else:
                    after.append(dict(st, checks=post, phase="after_program" if has_program else "powered"))
        else:
            nf = needs_firmware(st, None, fw)
            (after if nf else during).append(dict(st, phase=("after_program" if nf else "during_program") if has_program
                                                  else "powered"))
    if prog:
        prog[0]["concurrent"] = [st["id"] for st in during]
        return unpowered + power + prog + during + after + end
    return unpowered + power + during + after + end


def summary(fw: Dict[str, str], parts: List[str]) -> Dict[str, Any]:
    return {"programmed": parts, "firmware_nets": [{"net": n, "why": w} for n, w in sorted(fw.items(), key=lambda kv: C.natural_key(kv[0]))]}
