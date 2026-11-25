"""Programming / debug headers on the board under test, and the programming connector on the fixture.

A programming header is found from what its pins carry (net names and pin functions such as SWDIO/SWCLK,
TMS/TCK/TDI/TDO, MOSI/MISO/SCK + RESET, UPDI, PGD/PGC + MCLR, TXD/RXD + BOOT/EN) or from its name
(footprint, reference or value saying SWD, JTAG, ISP, ICSP, PROG, DEBUG or Tag-Connect). The test plan probes
every pin of it, and the fixture gets a programming connector per board wired to those probes, so a
programmer plugs into the fixture and programs the board while it sits on the pins.

Fixture connector (``program.connector``):
  same       a copy of the board's own header footprint (pin header, Tag-Connect pads, ...): the cable that
             programs the board today fits the fixture unchanged. The default.
  swd10      ARM Cortex debug 2 x 5, 1.27 mm (J-Link, ST-Link V3, CMSIS-DAP, Pico probe adapters)
  jtag20     ARM JTAG 2 x 10, 2.54 mm
  avr_isp6   AVR ISP 2 x 3, 2.54 mm
  pic_icsp6  PICkit / ICSP 1 x 6, 2.54 mm
  pins       1 x N, 2.54 mm, one pin per header signal, labelled (jumper wires to anything)
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from . import classify as C

# normalised name (upper case, letters and digits only) -> signal
_SIG = {}
for _sig, _names in {
    "SWDIO": ("SWDIO", "SWDIOTMS", "TMSSWDIO", "SWDAT", "SWDDIO", "TMS", "JTMS", "JTMSSWDIO"),
    "SWCLK": ("SWCLK", "SWCLKTCK", "TCKSWCLK", "SWDCLK", "TCK", "JTCK", "JTCKSWCLK"),
    "SWO": ("SWO", "TDO", "SWOTDO", "TDOSWO", "TRACESWO", "JTDO", "JTDOSWO"),
    "TDI": ("TDI", "JTDI"),
    "NTRST": ("NTRST", "TRST", "NJTRST", "JTRST"),
    "NRST": ("NRST", "RST", "RESET", "NRESET", "RESETN", "XRES", "MCLR", "NMCLR", "MCLRVPP", "VPPMCLR", "VPP", "NSRST",
             "SRST", "RSTN", "EXTRST"),
    "UPDI": ("UPDI",),
    "PDI": ("PDI", "PDIDATA"),
    "MOSI": ("MOSI", "COPI", "ISPMOSI"),
    "MISO": ("MISO", "CIPO", "ISPMISO"),
    "SCK": ("SCK", "SCLK", "ISPSCK"),
    "PGD": ("PGD", "ICSPDAT", "PGED", "PGED1", "PGED2", "PGED3", "ICSPDATA"),
    "PGC": ("PGC", "ICSPCLK", "PGEC", "PGEC1", "PGEC2", "PGEC3"),
    "PGM": ("PGM", "LVP"),
    "TXD": ("TX", "TXD", "UTXD", "U0TXD", "UARTTX", "TXD0", "TX0"),
    "RXD": ("RX", "RXD", "URXD", "U0RXD", "UARTRX", "RXD0", "RX0"),
    "BOOT": ("BOOT", "BOOT0", "GPIO0", "IO0", "BOOTSEL", "BOOTMODE"),
    "EN": ("EN", "CHIPEN", "CHIPPU", "ENABLE"),
}.items():
    for _n in _names:
        _SIG[_n] = _sig

_KEYWORDS = re.compile(r"(SWD|JTAG|ICSP|\bISP\b|_ISP|ISP_|PROG|DEBUG|TAG[-_ ]?CONNECT|TC20[35]0|UPDI|CORTEX)", re.I)
PROTOCOLS = ("swd", "jtag", "avr_isp", "updi", "pdi", "pic_icsp", "uart_boot", "generic")

# fixture-side standard connectors: pins (number -> signal), geometry
STANDARD: Dict[str, Dict[str, Any]] = {
    "swd10": {"title": "ARM Cortex debug 10-pin (SWD)", "cols": 2, "pitch": 1.27, "pad": 0.85, "drill": 0.65,
              "pins": {1: "VREF", 2: "SWDIO", 3: "GND", 4: "SWCLK", 5: "GND", 6: "SWO", 7: "", 8: "TDI", 9: "GND", 10: "NRST"},
              "lib": "Connector_PinHeader_1.27mm:PinHeader_2x05_P1.27mm_Vertical",
              "model": "Connector_PinHeader_1.27mm.3dshapes/PinHeader_2x05_P1.27mm_Vertical.wrl"},
    "jtag20": {"title": "ARM JTAG 20-pin", "cols": 2, "pitch": 2.54, "pad": 1.7, "drill": 1.0,
               "pins": {1: "VREF", 2: "", 3: "NTRST", 4: "GND", 5: "TDI", 6: "GND", 7: "SWDIO", 8: "GND", 9: "SWCLK",
                        10: "GND", 11: "", 12: "GND", 13: "SWO", 14: "GND", 15: "NRST", 16: "GND", 17: "", 18: "GND",
                        19: "", 20: "GND"},
               "lib": "Connector_PinHeader_2.54mm:PinHeader_2x10_P2.54mm_Vertical",
               "model": "Connector_PinHeader_2.54mm.3dshapes/PinHeader_2x10_P2.54mm_Vertical.wrl"},
    "avr_isp6": {"title": "AVR ISP 6-pin", "cols": 2, "pitch": 2.54, "pad": 1.7, "drill": 1.0,
                 "pins": {1: "MISO", 2: "VREF", 3: "SCK", 4: "MOSI", 5: "NRST", 6: "GND"},
                 "lib": "Connector_PinHeader_2.54mm:PinHeader_2x03_P2.54mm_Vertical",
                 "model": "Connector_PinHeader_2.54mm.3dshapes/PinHeader_2x03_P2.54mm_Vertical.wrl"},
    "pic_icsp6": {"title": "PIC ICSP 6-pin (PICkit)", "cols": 1, "pitch": 2.54, "pad": 1.7, "drill": 1.0,
                  "pins": {1: "NRST", 2: "VREF", 3: "GND", 4: "PGD", 5: "PGC", 6: "PGM"},
                  "lib": "Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical",
                  "model": "Connector_PinHeader_2.54mm.3dshapes/PinHeader_1x06_P2.54mm_Vertical.wrl"},
}
CONNECTORS = ("same",) + tuple(STANDARD) + ("pins",)
# the standard connectors that suit each protocol (first = suggestion)
SUITS = {"swd": ("swd10", "jtag20"), "jtag": ("jtag20", "swd10"), "avr_isp": ("avr_isp6",), "pic_icsp": ("pic_icsp6",)}


def _norm(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", C.base_net(name or "").upper())


def pin_signal(design, pad) -> str:
    """What a header pin carries: a programming signal, VREF (a supply rail), GND, or "" (something else)."""
    net = pad.net
    if not net or C.is_unconnected(net):
        return ""
    kind = design.net_kind(net)
    if kind == "ground":
        return "GND"
    for name in (pad.pinfunction, net):
        n = _norm(name)
        if not n:
            continue
        if n in _SIG:
            return _SIG[n]
        for tok in re.split(r"[^A-Z0-9]+", C.base_net(name).upper()):     # e.g. MCU_SWDIO, SWD_CLK_3V3
            if tok in _SIG and len(tok) >= 3:
                return _SIG[tok]
        for key in ("SWDIO", "SWCLK", "UPDI", "ICSPDAT", "ICSPCLK", "NTRST"):
            if key in n:
                return _SIG[key]
    if kind == "power":
        return "VREF"
    return ""


def protocol_of(signals: List[str]) -> str:
    s = set(signals)
    if {"SWDIO", "SWCLK", "TDI"} <= s or ({"SWDIO", "SWCLK", "SWO"} <= s and "NTRST" in s):
        return "jtag"
    if {"SWDIO", "SWCLK"} <= s:
        return "swd"
    if {"MOSI", "MISO", "SCK", "NRST"} <= s:
        return "avr_isp"
    if "UPDI" in s:
        return "updi"
    if "PDI" in s:
        return "pdi"
    if {"PGD", "PGC"} <= s:
        return "pic_icsp"
    if {"TXD", "RXD"} <= s and (s & {"BOOT", "EN", "NRST"}):
        return "uart_boot"
    return "generic"


def detect(design) -> List[Dict[str, Any]]:
    """Every programming/debug header on the board: [{"ref", "protocol", "side", "pins": [{"pad", "net", "signal",
    "x", "y"}], "why", "pitch_mm"}], in reference order."""
    out = []
    for fp in sorted(design.components(), key=lambda f: C.natural_key(f.ref)):
        if fp.dnp or not fp.pads:
            continue
        named = bool(_KEYWORDS.search(" ".join((fp.lib_id, fp.ref, fp.value))))
        if not (C.is_connector(fp) or named):
            continue
        pins = []
        for p in sorted(fp.pads, key=lambda p: C.natural_key(p.number)):
            if not p.has_copper or not p.number:
                continue
            pins.append({"pad": p.number, "net": p.net or "", "signal": pin_signal(design, p), "x": round(p.x, 4),
                         "y": round(p.y, 4)})
        sigs = [p["signal"] for p in pins if p["signal"] not in ("", "GND", "VREF")]
        proto = protocol_of(sigs)
        strong = proto in ("swd", "jtag", "avr_isp", "updi", "pdi", "pic_icsp", "uart_boot")
        if strong and not named and proto in ("avr_isp", "pic_icsp", "uart_boot"):
            # SPI with a reset line or a UART is just as likely an interface (display, sensor, modem): only a
            # header that carries nothing else counts, unless its name says it is for programming
            if any(p["net"] and not p["signal"] for p in pins):
                strong = False
        if not strong and not (named and len([p for p in pins if p["net"]]) >= 2):
            continue
        why = "pins carry %s" % ", ".join(sorted(set(sigs))) if strong else "named %s" % _KEYWORDS.search(
            " ".join((fp.lib_id, fp.ref, fp.value))).group(0)
        out.append({"ref": fp.ref, "value": fp.value, "footprint": fp.lib_id, "protocol": proto, "side": fp.side,
                    "pins": pins, "why": why, "pitch_mm": round(fp.min_pitch(), 3) if len(fp.pads) > 1 else None})
    return out


def selected(design) -> List[Dict[str, Any]]:
    """The headers the fixture serves: program.headers = auto (every detected one) | [refs] | [] (none)."""
    want = (design.cfg.get("program") or {}).get("headers", "auto")
    found = detect(design)
    if want in ("auto", None):
        return found
    if isinstance(want, str):
        want = [want]
    by_ref = {h["ref"]: h for h in found}
    out = []
    for ref in want or []:
        if ref in by_ref:
            out.append(by_ref[ref])
            continue
        fp = design.board.by_ref.get(ref)
        if fp is None:
            continue
        pins = [{"pad": p.number, "net": p.net or "", "signal": pin_signal(design, p), "x": round(p.x, 4),
                 "y": round(p.y, 4)} for p in sorted(fp.pads, key=lambda p: C.natural_key(p.number))
                if p.has_copper and p.number]
        out.append({"ref": ref, "value": fp.value, "footprint": fp.lib_id, "side": fp.side, "pins": pins, "why": "set in config",
                    "protocol": protocol_of([p["signal"] for p in pins]),
                    "pitch_mm": round(fp.min_pitch(), 3) if len(fp.pads) > 1 else None})
    return out


def connector_for(header: Dict[str, Any], choice: str) -> Tuple[str, List[str]]:
    """The fixture connector kind to use for a header, and warnings. A standard connector that cannot carry the
    header's signals (e.g. avr_isp6 for an SWD header) falls back to a copy of the header."""
    warn = []
    choice = (choice or "same").lower()
    if choice not in CONNECTORS:
        warn.append("program.connector %r is unknown; using a copy of the header" % choice)
        return "same", warn
    if choice in STANDARD:
        need = {p["signal"] for p in header["pins"] if p["signal"] not in ("", "GND", "VREF")}
        have = set(STANDARD[choice]["pins"].values())
        missing = sorted(need - have)
        if not need or len(need & have) < 2:
            warn.append("%s: a %s connector cannot carry its %s signals; the fixture gets a copy of the header instead" % (
                header["ref"], choice, header["protocol"]))
            return "same", warn
        if missing:
            warn.append("%s: %s on the board header have no pin on the %s connector" % (header["ref"], ", ".join(missing), choice))
    return choice, warn


def pinout(header: Dict[str, Any], kind: str) -> List[Tuple[str, str, str]]:
    """Fixture connector pins as (pin number, DUT net or "", label)."""
    if kind == "same":
        return [(p["pad"], p["net"], p["signal"] or C.base_net(p["net"])[:8]) for p in header["pins"]]
    if kind == "pins":
        return [(str(i + 1), p["net"], p["signal"] or C.base_net(p["net"])[:8])
                for i, p in enumerate([p for p in header["pins"] if p["net"]])]
    by_sig: Dict[str, str] = {}
    for p in header["pins"]:
        if p["signal"] and p["net"] and p["signal"] not in by_sig:
            by_sig[p["signal"]] = p["net"]
    if "GND" not in by_sig:
        by_sig["GND"] = ""          # filled with the fixture's GND
    out = []
    for num, sig in sorted(STANDARD[kind]["pins"].items()):
        out.append((str(num), by_sig.get(sig, "") if sig else "", sig or "NC"))
    return out
