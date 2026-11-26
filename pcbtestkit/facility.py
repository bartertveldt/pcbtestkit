"""Test facilities: the tester a fixture plugs into, as data.

PCB TestKit's own facility is a Raspberry Pi with an ADC (``raspberry-pi``): the fixture gets a generated
2 x N header and the runner reads ADS1115 / MCP3008 / an MCU over serial. A facility file describes any other
tester, so the generated fixture plugs straight into it:

- what it measures and supplies: analog inputs and their range, switched supplies to the board;
- the buses it talks (CAN, USB, Ethernet, UART, I2C, RS-485), each either wired through the fixture (pogo pins on
  the board's bus nets, onto the facility's connector) or by cable into the board's own connector;
- the fixture it takes (optional): a fixed plate size, fixed mounting holes and fixed interface connector(s) with
  a pin map, the same on every fixture, so a fixture made for any board fits the same tester;
- how many boards of a batch it tests on one fixture;
- the runner driver (optional): a Python file with a ``Driver`` class the runner calls to measure, switch power and
  run the bus checks, and the ``hardware.json`` it starts from.

A facility is ``<id>.json`` or a folder ``<id>/facility.json`` (with its ``driver.py`` next to it). PCB TestKit
looks in its own ``facilities/`` folder, then ``~/.pcbtestkit/facilities`` (or the folders in
``PCBTESTKIT_FACILITIES``), then ``<project>/facilities`` and ``facility_dirs`` in pcbtestkit.json; a later one
with the same id replaces an earlier one. ``pcbtestkit facilities template`` writes a starting point and
``pcbtestkit facilities validate`` checks one. Pick one with ``"facility": "<id>"`` in pcbtestkit.json.
"""
from __future__ import annotations

import copy
import glob
import json
import math
import os
import re
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
BUILTIN_DIR = os.path.join(HERE, "facilities")
SCHEMA = "pcbtestkit-facility/1"
DEFAULT = "raspberry-pi"

# bus -> its signals, named as the board sees them (UART_TX = the board's transmit line)
BUSES: Dict[str, Tuple[str, ...]] = {
    "can": ("CAN_H", "CAN_L"),
    "usb": ("USB_DP", "USB_DM"),
    "ethernet": ("ETH_TX_P", "ETH_TX_N", "ETH_RX_P", "ETH_RX_N"),
    "uart": ("UART_TX", "UART_RX"),
    "i2c": ("I2C_SDA", "I2C_SCL"),
    "rs485": ("RS485_A", "RS485_B"),
}
BUS_OF = {sig: bus for bus, sigs in BUSES.items() for sig in sigs}
VIA = ("fixture", "cable")
_PIN_FUNC = re.compile(r"^(GND|NC|AIN\d+|VOUT\d+|DIO\d+)$")
# A station can offer a bus more than once: CAN_H / CAN1_H is instance 1, CAN2_H instance 2 (RS485_2_A for RS-485,
# whose name ends in a digit). Each board of a batch on a shared connector gets its own instance.
_BUS_INST = {sig: re.compile(r"^%s%s(\d+)_%s$" % (re.escape(sig.split("_", 1)[0]), "_" if sig.split("_", 1)[0][-1].isdigit()
                                                   else "", re.escape(sig.split("_", 1)[1]))) for sig in BUS_OF}


def bus_pin(fn: Any) -> Optional[Tuple[str, int]]:
    """(bus signal, instance) for a connector pin function that carries a bus line, else None."""
    fn = str(fn).upper()
    if fn in BUS_OF:
        return fn, 1
    for sig, rx in _BUS_INST.items():
        m = rx.match(fn)
        if m and int(m.group(1)) >= 1:
            return sig, int(m.group(1))
    return None


def bus_function(sig: str, k: int) -> str:
    """The pin function for instance k of bus signal sig (CAN_H, 2 -> CAN2_H)."""
    if k <= 1:
        return sig
    p, r = sig.split("_", 1)
    return "%s%s%d_%s" % (p, "_" if p[-1].isdigit() else "", k, r)


def is_pin_function(fn: Any) -> bool:
    return bool(_PIN_FUNC.match(str(fn))) or bus_pin(fn) is not None
# Connector geometry as in KiCad's libraries: at angle 0 the connector runs down (+Y), pins_along positions long and
# pins_across pins wide. zigzag numbering: 1 and 2 side by side, then 3 and 4 (2 x N headers, IDC); rows: 1 to
# pins_along down the first column, then the next column.
NUMBERING = ("zigzag", "rows")

# board net names per bus signal (upper case, separators _ - . / and spaces)
_SEP = r"[_\-./ ]?"
_NET_RX = {
    "CAN_H": r"CAN\d*%s(H|HI|HIGH)" % _SEP,
    "CAN_L": r"CAN\d*%s(L|LO|LOW)" % _SEP,
    "USB_DP": r"(USB\d*%s(D%s)?(P|\+|PLUS)|D\+)" % (_SEP, _SEP),
    "USB_DM": r"(USB\d*%s(D%s)?(M|N|-|MINUS)|D-)" % (_SEP, _SEP),
    "ETH_TX_P": r"(ETH\w*%s|PHY%s)?(TX|TD)%s(P|\+)" % (_SEP, _SEP, _SEP),
    "ETH_TX_N": r"(ETH\w*%s|PHY%s)?(TX|TD)%s(N|-)" % (_SEP, _SEP, _SEP),
    "ETH_RX_P": r"(ETH\w*%s|PHY%s)?(RX|RD)%s(P|\+)" % (_SEP, _SEP, _SEP),
    "ETH_RX_N": r"(ETH\w*%s|PHY%s)?(RX|RD)%s(N|-)" % (_SEP, _SEP, _SEP),
    "UART_TX": r"((U|US)ART\d*|SERIAL\d*|DBG|DEBUG)?%s(TX|TXD)\d?" % _SEP,
    "UART_RX": r"((U|US)ART\d*|SERIAL\d*|DBG|DEBUG)?%s(RX|RXD)\d?" % _SEP,
    "I2C_SDA": r"(I2C\d*%s)?SDA\d?" % _SEP,
    "I2C_SCL": r"(I2C\d*%s)?SCL\d?" % _SEP,
    "RS485_A": r"(RS)?485%s(A|P|\+)" % _SEP,
    "RS485_B": r"(RS)?485%s(B|N|-)" % _SEP,
}
_NET_RE = {sig: re.compile(r"^%s$" % rx) for sig, rx in _NET_RX.items()}


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #
def facility_dirs(cfg: Optional[Dict[str, Any]] = None, project_dir: Optional[str] = None) -> List[str]:
    env = os.environ.get("PCBTESTKIT_FACILITIES")
    dirs = [BUILTIN_DIR]
    dirs += env.split(os.pathsep) if env else [os.path.join(os.path.expanduser("~"), ".pcbtestkit", "facilities")]
    if project_dir:
        dirs.append(os.path.join(project_dir, "facilities"))
    for d in (cfg or {}).get("facility_dirs") or []:
        dirs.append(d if os.path.isabs(d) or not project_dir else os.path.join(project_dir, d))
    return dirs


def _files(d: str) -> List[str]:
    return sorted(glob.glob(os.path.join(d, "*.json")) + glob.glob(os.path.join(d, "*", "facility.json")))


def read_facility(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise ValueError("a facility file holds one JSON object")
    raw["_dir"] = os.path.dirname(os.path.abspath(path))
    raw["_origin"] = "built-in" if os.path.abspath(path).startswith(BUILTIN_DIR + os.sep) else path
    raw.setdefault("id", os.path.basename(raw["_dir"]) if os.path.basename(path) == "facility.json"
                   else os.path.splitext(os.path.basename(path))[0])
    return raw


def load_facilities(cfg: Optional[Dict[str, Any]] = None,
                    project_dir: Optional[str] = None) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Every facility PCB TestKit can find, by id, and warnings for files it skipped."""
    out: Dict[str, Dict[str, Any]] = {}
    warnings: List[str] = []
    for d in facility_dirs(cfg, project_dir):
        for p in _files(d):
            try:
                raw = read_facility(p)
            except (OSError, ValueError) as e:
                warnings.append("cannot read facility %s: %s" % (p, e))
                continue
            r = validate(raw)
            if not r["ok"]:
                warnings.append("facility %s skipped: %s" % (p, "; ".join(r["errors"][:3])))
                continue
            if raw["id"] in out and out[raw["id"]]["_origin"] != "built-in":
                warnings.append("facility %s from %s replaces the one from %s" % (raw["id"], p, out[raw["id"]]["_origin"]))
            out[raw["id"]] = raw
    return out, warnings


def facility_for(design: Any) -> Dict[str, Any]:
    """The facility the design is tested on (``facility`` in pcbtestkit.json: an id or an inline definition).
    An unknown id falls back to the built-in Raspberry Pi with a warning in design.warnings."""
    cfg = design.cfg
    sel = cfg.get("facility") or DEFAULT
    key = json.dumps([sel, cfg.get("facility_dirs") or []], sort_keys=True, default=str)
    cache = getattr(design, "_facility", None)
    if cache is not None and cache[0] == key:          # the same choice since the config was last loaded
        return cache[1]
    fac: Optional[Dict[str, Any]] = None
    if isinstance(sel, dict):
        raw = dict(sel)
        raw.setdefault("id", "inline")
        raw.setdefault("_dir", design.project_dir)
        raw["_origin"] = "pcbtestkit.json"
        r = validate(raw)
        if r["ok"]:
            fac = raw
        else:
            design.warnings.append("facility in pcbtestkit.json is not valid (%s); using %s" % ("; ".join(r["errors"][:3]),
                                                                                                DEFAULT))
    else:
        found, warns = load_facilities(cfg, design.project_dir)
        design.warnings.extend(warns)
        fac = found.get(str(sel))
        if fac is None:
            design.warnings.append("facility %r not found (pcbtestkit facilities list); using %s" % (sel, DEFAULT))
    if fac is None:
        fac = load_facilities()[0][DEFAULT]
    design._facility = (key, fac)
    return fac


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Check a facility definition. Returns {"ok", "errors", "warnings"}."""
    E: List[str] = []
    Wn: List[str] = []
    if raw.get("schema", SCHEMA) != SCHEMA:
        E.append("schema must be %r" % SCHEMA)
    if not re.match(r"^[a-z0-9][a-z0-9._-]*$", str(raw.get("id", ""))):
        E.append("id: lower-case letters, digits, '.', '_' and '-' only")
    if not raw.get("name"):
        Wn.append("no name")
    an = raw.get("analog") or {}
    for k in ("inputs", "max_v"):
        if an.get(k) is not None and (not _num(an[k]) or an[k] <= 0):
            E.append("analog.%s must be a positive number" % k)
    pw = raw.get("power") or {}
    for k in ("max_v", "max_a"):
        if pw.get(k) is not None and (not _num(pw[k]) or pw[k] <= 0):
            E.append("power.%s must be a positive number" % k)
    sites = (raw.get("batch") or {}).get("max_sites")
    if sites is not None and (not isinstance(sites, int) or sites < 1):
        E.append("batch.max_sites must be a whole number of at least 1")
    buses = raw.get("buses") or {}
    if not isinstance(buses, dict):
        E.append("buses: an object such as {\"can\": {\"via\": \"fixture\"}}")
        buses = {}
    for bus, b in buses.items():
        if bus not in BUSES:
            E.append("buses.%s: unknown bus (%s)" % (bus, ", ".join(BUSES)))
        elif (b or {}).get("via", "fixture") not in VIA:
            E.append("buses.%s.via: fixture or cable" % bus)
    fx = raw.get("fixture") or {}
    plate = fx.get("plate_mm")
    if plate is not None and not (isinstance(plate, list) and len(plate) == 2 and all(_num(v) and v > 10 for v in plate)):
        E.append("fixture.plate_mm: [width, height] in mm")
    for i, h in enumerate(fx.get("holes") or []):
        if not (isinstance(h.get("at_mm"), list) and len(h["at_mm"]) == 2 and _num(h.get("d_mm", 3.2))):
            E.append("fixture.holes[%d]: {\"at_mm\": [x, y], \"d_mm\": d}" % i)
    wired: Dict[Any, set] = {}
    for i, c in enumerate(fx.get("connectors") or []):
        where = "fixture.connectors[%d]" % i
        across, along = c.get("pins_across", 2), c.get("pins_along")
        if not (isinstance(across, int) and isinstance(along, int) and across >= 1 and along >= 1):
            E.append("%s: pins_across and pins_along (whole numbers, e.g. 2 and 20 for a 2 x 20 header)" % where)
            continue
        if not (isinstance(c.get("at_mm"), list) and len(c["at_mm"]) == 2):
            E.append("%s.at_mm: [x, y] of pin 1 from the plate's top-left corner" % where)
        if c.get("numbering", "zigzag") not in NUMBERING:
            E.append("%s.numbering: %s" % (where, " or ".join(NUMBERING)))
        if not _num(c.get("pitch_mm", 2.54)) or c.get("pitch_mm", 2.54) <= 0.5:
            E.append("%s.pitch_mm must be more than 0.5" % where)
        pins = c.get("pins") or {}
        if not isinstance(pins, dict) or not pins:
            E.append("%s.pins: {\"1\": \"GND\", \"2\": \"AIN1\", ...}" % where)
            continue
        site = c.get("site", 1)
        if c.get("shared") is not None and not isinstance(c.get("shared"), bool):
            E.append("%s.shared: true (one connector for every board of a batch) or false" % where)
        for num, fn in pins.items():
            if not str(num).isdigit() or not 1 <= int(num) <= across * along:
                E.append("%s.pins: pin %s is not on a %d x %d connector" % (where, num, across, along))
            bp = bus_pin(fn)
            if bp is None and not _PIN_FUNC.match(str(fn)):
                E.append("%s.pins.%s: %r is not a pin function (GND, NC, AIN<n>, VOUT<n>, DIO<n>, %s, or a bus line "
                         "with an instance number such as CAN2_H)" % (where, num, fn, ", ".join(BUS_OF)))
            elif bp:
                wired.setdefault("shared" if c.get("shared") else site, set()).add(bp)
        if "GND" not in pins.values():
            Wn.append("%s has no GND pin" % where)
    for bus, b in buses.items():
        if bus in BUSES and (b or {}).get("via", "fixture") == "fixture":
            inst = (b or {}).get("instances")
            if inst is not None and (not isinstance(inst, int) or inst < 1):
                E.append("buses.%s.instances must be a whole number of at least 1" % bus)
            for key, have in sorted(wired.items(), key=lambda kv: str(kv[0])) or [(1, set())]:
                if key == "shared":
                    if not _instances(have, bus):
                        E.append("buses.%s goes through the fixture but the shared connector has no complete %s "
                                 "(%s)" % (bus, bus, ", ".join(BUSES[bus])))
                    continue
                miss = [sg for sg in BUSES[bus] if not any(h[0] == sg for h in have)]
                if miss:
                    E.append("buses.%s goes through the fixture but site %s's connector has no %s pin" % (
                        bus, key, ", ".join(miss)))
    drv = (raw.get("runner") or {}).get("driver")
    if drv and raw.get("_dir") and not os.path.exists(os.path.join(raw["_dir"], drv)):
        E.append("runner.driver: %s not found next to the facility file" % drv)
    return {"ok": not E, "errors": E, "warnings": Wn}


def _instances(have, bus: str) -> List[int]:
    """Instances of ``bus`` with every line present among ``have`` ({(signal, instance)})."""
    ks = sorted({k for sg, k in have if BUS_OF.get(sg) == bus})
    return [k for k in ks if all((sg, k) in have for sg in BUSES[bus])]


# --------------------------------------------------------------------------- #
# what the facility means for a board
# --------------------------------------------------------------------------- #
def connectors(fac: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list((fac.get("fixture") or {}).get("connectors") or [])


def shared(fac: Dict[str, Any]) -> bool:
    """A station: one connector for every board of a batch (each board gets its own inputs, supply and bus lines)."""
    return any(c.get("shared") for c in connectors(fac))


def analog_limits(fac: Dict[str, Any], sites: int = 1) -> Tuple[Optional[int], Optional[float]]:
    """(analog inputs per board, their full-scale voltage); None = the test plan's own settings. On a shared
    connector (a station) the inputs are split between the ``sites`` boards of the batch."""
    an = fac.get("analog") or {}
    n = an.get("inputs")
    if shared(fac):
        k = sum(1 for c in connectors(fac) if c.get("shared") for f in c["pins"].values() if str(f).startswith("AIN"))
        k //= max(1, int(sites))
        n = min(n // max(1, int(sites)), k) if n else k
    elif connectors(fac):                  # a fixed connector: as many channels as it has AIN pins per site
        per = {}
        for c in connectors(fac):
            per[c.get("site", 1)] = per.get(c.get("site", 1), 0) + sum(1 for f in c["pins"].values() if f.startswith("AIN"))
        k = min(per.values()) if per else 0
        n = min(n, k) if n else k
    return (int(n) if n is not None else None), (float(an["max_v"]) if an.get("max_v") else None)


def bus_nets(design: Any) -> Dict[str, Dict[str, str]]:
    """The board's bus nets by bus and signal (complete buses only): from net names, or testplan.bus_nets
    (e.g. {"can": {"CAN_H": "/CANH", "CAN_L": "/CANL"}}) for names it does not recognise."""
    found: Dict[str, Dict[str, str]] = {}
    nets = sorted(n for n in design.board.pads_by_net() if n)
    for n in nets:
        base = n.split("/")[-1].upper().strip()
        for sig, rx in _NET_RE.items():
            if rx.match(base):
                found.setdefault(BUS_OF[sig], {}).setdefault(sig, n)
    out = {bus: sigs for bus, sigs in found.items() if all(s in sigs for s in BUSES[bus])}
    for bus, sigs in ((design.cfg.get("testplan") or {}).get("bus_nets") or {}).items():
        if bus in BUSES:
            out[bus] = dict(sigs)
    return out


def buses_for(design: Any, fac: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The facility's buses the board has: [{"bus", "via", "signals": {signal: net}, "note"}]."""
    have = bus_nets(design)
    out = []
    for bus, b in sorted((fac.get("buses") or {}).items()):
        b = b or {}
        if bus in have:
            out.append({"bus": bus, "via": b.get("via", "fixture"), "signals": have[bus], "note": b.get("note", ""),
                        "check": b.get("check") or {}})
    return out


def assign_pins(fac: Dict[str, Any], channels: List[str], supply: bool, buses: List[Dict[str, Any]],
                sites: int = 1) -> List[Dict[str, Any]]:
    """What each facility connector pin carries for this board, per connector: channels CH01.. on AIN1.. in order,
    the supply on VOUT1, GND, the buses wired through the fixture; everything else is left open.

    A shared connector (a station) serves every board of the batch: board k gets the k-th block of AIN pins (as
    many as it has channels), VOUT<k> for its supply and instance k of each bus. Every pin then also says which
    board ("site") it belongs to; GND belongs to all."""
    wired = {sig for b in buses if b["via"] == "fixture" for sig in b["signals"]}
    out = []
    for c in connectors(fac):
        if c.get("shared"):
            out.append(_assign_shared(c, channels, supply, wired, max(1, int(sites))))
            continue
        ains = sorted((int(f[3:]), num) for num, f in c["pins"].items() if f.startswith("AIN"))
        ch_of = {num: channels[i] for i, (_n, num) in enumerate(ains) if i < len(channels)}
        pins = []
        for num in sorted(c["pins"], key=int):
            fn = c["pins"][num]
            if fn == "GND":
                sig = "GND"
            elif fn == "VOUT1" and supply:
                sig = "VIN"
            elif fn.startswith("AIN"):
                sig = ch_of.get(num, "NC")
            elif fn in wired:
                sig = fn
            else:
                sig = "NC"
            pins.append({"pin": int(num), "function": fn, "signal": sig, "site": c.get("site", 1) if sig not in ("NC", "GND") else None})
        out.append({"name": c.get("name", "TESTER"), "site": c.get("site", 1), "pins": pins})
    return out


def _assign_shared(c: Dict[str, Any], channels: List[str], supply: bool, wired: set, sites: int) -> Dict[str, Any]:
    ains = sorted((int(f[3:]), num) for num, f in c["pins"].items() if str(f).startswith("AIN"))
    per = len(channels)
    who: Dict[str, Tuple[int, str]] = {}                 # pin number -> (site, signal)
    for k in range(1, sites + 1):
        block = ains[(k - 1) * per: k * per]
        if len(block) == per:
            for ch, (_n, num) in zip(channels, block):
                who[num] = (k, ch)
    for num, fn in c["pins"].items():
        f = str(fn)
        if f.startswith("VOUT") and supply:
            k = int(f[4:])
            if 1 <= k <= sites:
                who[num] = (k, "VIN")
        bp = bus_pin(f)
        if bp and bp[0] in wired and 1 <= bp[1] <= sites:
            who[num] = (bp[1], bp[0])
    pins = []
    for num in sorted(c["pins"], key=int):
        fn = c["pins"][num]
        if fn == "GND":
            pins.append({"pin": int(num), "function": fn, "signal": "GND", "site": None})
        elif num in who:
            pins.append({"pin": int(num), "function": fn, "signal": who[num][1], "site": who[num][0]})
        else:
            pins.append({"pin": int(num), "function": fn, "signal": "NC", "site": None})
    return {"name": c.get("name", "TESTER"), "site": 1, "shared": True, "sites": sites, "pins": pins}


# --------------------------------------------------------------------------- #
# stations: how many boards of a type fit one fixture
# --------------------------------------------------------------------------- #
def capacity(fac: Dict[str, Any]) -> Dict[str, Any]:
    """What the station's shared connector(s) offer: analog inputs, switched supplies, complete bus instances
    wired through the fixture, the most boards per fixture, the plate. Without a shared connector each fixture
    site needs its own connector: ``sites`` is how many the facility has."""
    fx = fac.get("fixture") or {}
    out: Dict[str, Any] = {"shared": shared(fac), "max_sites": (fac.get("batch") or {}).get("max_sites"),
                           "plate_mm": fx.get("plate_mm")}
    sh = [c for c in connectors(fac) if c.get("shared")]
    if not sh:
        out["sites"] = len({c.get("site", 1) for c in connectors(fac)}) or None
        return out
    fns = [str(f) for c in sh for f in c["pins"].values()]
    have = {bp for bp in (bus_pin(f) for f in fns) if bp}
    via = {b: (v or {}).get("via", "fixture") for b, v in (fac.get("buses") or {}).items()}
    buses = {}
    for bus in BUSES:
        inst = _instances(have, bus)
        if inst and via.get(bus, "fixture") == "fixture":
            lim = ((fac.get("buses") or {}).get(bus) or {}).get("instances")
            buses[bus] = min(len(inst), int(lim)) if lim else len(inst)
    an = (fac.get("analog") or {}).get("inputs")
    n_ain = sum(1 for f in fns if f.startswith("AIN"))
    out.update({"inputs": min(n_ain, int(an)) if an else n_ain,
                "supplies": len({f for f in fns if f.startswith("VOUT")}),
                "gnd_pins": sum(1 for f in fns if f == "GND"), "buses": buses,
                "dio": len({f for f in fns if f.startswith("DIO")})})
    return out


def board_needs(plan: Dict[str, Any]) -> Dict[str, Any]:
    """What one board takes from a station, from its test plan: measurement channels, a switched supply, buses
    wired through the fixture."""
    chans = [p["channel"] for p in plan.get("probes") or [] if p.get("channel")]
    buses = sorted({b["bus"] for b in (plan.get("facility") or {}).get("bus_checks") or [] if b.get("via") == "fixture"})
    return {"channels": len(chans), "supply": bool((plan.get("supply") or {}).get("net")), "buses": buses}


def max_sites(cap: Dict[str, Any], need: Dict[str, Any]) -> Dict[str, Any]:
    """The most boards of one type a station fixture can serve at once: {"count", "limit", "by": {resource: n}}.
    count 0 means the board does not fit the station at all (``limit`` says why)."""
    by: Dict[str, int] = {}
    if not cap.get("shared"):
        by["connectors"] = int(cap.get("sites") or 1)
    else:
        if need["channels"]:
            by["inputs"] = int(cap.get("inputs", 0)) // need["channels"]
        if need["supply"]:
            by["supplies"] = int(cap.get("supplies", 0))
        for bus in need["buses"]:
            by["%s buses" % bus] = int((cap.get("buses") or {}).get(bus, 0))
    if cap.get("max_sites"):
        by["station limit"] = int(cap["max_sites"])
    if not by:
        return {"count": 1, "limit": None, "by": {}}
    limit = min(by, key=lambda k: (by[k], k))
    return {"count": max(0, by[limit]), "limit": limit, "by": by}


def summary(fac: Dict[str, Any]) -> Dict[str, Any]:
    """The facility as the test plan records it (no file paths)."""
    n, vmax = analog_limits(fac)
    fx = fac.get("fixture") or {}
    return {"id": fac["id"], "name": fac.get("name", fac["id"]), "analog_inputs": n, "max_input_v": vmax,
            "shared": shared(fac),
            "buses": {k: (v or {}).get("via", "fixture") for k, v in sorted((fac.get("buses") or {}).items())},
            "plate_mm": fx.get("plate_mm"), "connectors": [c.get("name", "TESTER") for c in connectors(fac)],
            "max_sites": (fac.get("batch") or {}).get("max_sites"),
            "driver": bool((fac.get("runner") or {}).get("driver"))}


def pin_count(c: Dict[str, Any]) -> int:
    return int(c.get("pins_across", 2)) * int(c["pins_along"])


def pin_xy(c: Dict[str, Any], num: int) -> Tuple[float, float]:
    """Pin ``num`` of connector ``c`` relative to pin 1, before the connector's rotation (KiCad's footprint frame)."""
    across, along, p = int(c.get("pins_across", 2)), int(c["pins_along"]), float(c.get("pitch_mm", 2.54))
    if c.get("numbering", "zigzag") == "zigzag":
        i, j = (num - 1) % across, (num - 1) // across
    else:
        i, j = (num - 1) // along, (num - 1) % along
    return i * p, j * p


def hardware_template(fac: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The runner's hardware.json for this facility, or None for PCB TestKit's own (Raspberry Pi) template."""
    rn = fac.get("runner") or {}
    if not rn.get("driver") and rn.get("hardware") is None:
        return None
    hw = copy.deepcopy(rn.get("hardware") or {})
    hw["facility"] = {"id": fac["id"], "name": fac.get("name", fac["id"])}
    if rn.get("driver"):
        hw["facility"].update(driver="facility_driver.py", **{"class": rn.get("class", "Driver")})
    return hw


TEMPLATE: Dict[str, Any] = {
    "schema": SCHEMA,
    "id": "my-tester",
    "name": "My tester",
    "description": "What this tester is; shown in the dialog and the guide.",
    "analog": {"inputs": 8, "max_v": 10.0},
    "power": {"outputs": 1, "max_v": 24.0, "max_a": 1.0, "current_sense": True},
    "batch": {"max_sites": 1},
    "buses": {"can": {"via": "fixture", "check": {"send": "7DF#0201000000000000", "expect": "7E8#"}},
              "usb": {"via": "cable", "note": "Plug the tester's USB cable into the board's USB port."}},
    "fixture": {
        "plate_mm": [160.0, 120.0],
        "holes": [{"at_mm": [5.0, 5.0], "d_mm": 3.2}, {"at_mm": [155.0, 5.0], "d_mm": 3.2},
                  {"at_mm": [5.0, 115.0], "d_mm": 3.2}, {"at_mm": [155.0, 115.0], "d_mm": 3.2}],
        "connectors": [{"name": "TESTER", "site": 1, "at_mm": [68.57, 113.0], "angle": 90, "pins_across": 2, "pins_along": 10,
                        "pitch_mm": 2.54, "numbering": "zigzag",
                        "footprint": "Connector_IDC:IDC-Header_2x10_P2.54mm_Vertical",
                        "pins": {"1": "GND", "2": "GND", "3": "AIN1", "4": "AIN2", "5": "AIN3", "6": "AIN4",
                                 "7": "AIN5", "8": "AIN6", "9": "AIN7", "10": "AIN8", "11": "GND", "12": "VOUT1",
                                 "13": "CAN_H", "14": "CAN_L", "15": "DIO1", "16": "DIO2", "17": "NC", "18": "NC",
                                 "19": "GND", "20": "GND"}}]},
    "runner": {"driver": "driver.py", "class": "Driver",
               "hardware": {"connection": {"type": "tcp", "host": "192.168.1.50", "port": 5025}}},
}


# --------------------------------------------------------------------------- #
# a station from its own KiCad board
# --------------------------------------------------------------------------- #
# Net names on the station's interface connector -> pin functions. Bus lines are named as the board under test
# sees them (UART1_TX = the board's transmit line); a number picks the instance (CAN2_H).
_STATION_NET = [
    (re.compile(r"^(GND|GNDA|GNDD|AGND|DGND|VSS|0V|EARTH|PE)$"), lambda m: "GND"),
    (re.compile(r"^(AIN|ADC|AI|ANA|ANALOG|CH|MEAS|IN)[_-]?0*(\d+)$"), lambda m: "AIN%d" % int(m.group(2))),
    (re.compile(r"^(VOUT|VSUP|SUPPLY|PWR|DUT_?PWR|VDUT|DUT_?VIN|SW_?PWR|PSU)[_-]?0*(\d+)$"), lambda m: "VOUT%d" % int(m.group(2))),
    (re.compile(r"^(VOUT|VSUP|VDUT|DUT_?PWR|DUT_?VIN|PSU)$"), lambda m: "VOUT1"),
    (re.compile(r"^(DIO|GPIO|IO|DIG)[_-]?0*(\d+)$"), lambda m: "DIO%d" % int(m.group(2))),
    (re.compile(r"^CAN[_-]?(\d*)[_-]?(H|HI|HIGH|L|LO|LOW)$"),
     lambda m: bus_function("CAN_" + m.group(2)[0], int(m.group(1) or 1))),
    (re.compile(r"^(?:UART|USART|SERIAL)[_-]?(\d*)[_-]?(TXD?|RXD?)$"),
     lambda m: bus_function("UART_" + m.group(2)[:2], int(m.group(1) or 1))),
    (re.compile(r"^I2C[_-]?(\d*)[_-]?(SDA|SCL)$"), lambda m: bus_function("I2C_" + m.group(2), int(m.group(1) or 1))),
    (re.compile(r"^RS485[_-]?(\d*)[_-]?(A|B)$"), lambda m: bus_function("RS485_" + m.group(2), int(m.group(1) or 1))),
    (re.compile(r"^USB[_-]?(\d*)[_-]?(DP|DM|D\+|D-)$"),
     lambda m: bus_function("USB_" + ("DP" if m.group(2) in ("DP", "D+") else "DM"), int(m.group(1) or 1))),
]
# the fixture carries the mating part of the station's connector
_MATE = [("PinSocket", "PinHeader"), ("PinHeader", "PinSocket")]


def station_pin_function(net: str) -> Optional[str]:
    """The pin function a station connector net stands for (GND, AIN3, VOUT2, CAN2_H, ...), or None."""
    name = str(net or "").split("/")[-1].strip().upper()
    for rx, f in _STATION_NET:
        m = rx.match(name)
        if m:
            return f(m)
    return None


def _grid(pads: List[Tuple[int, float, float]]) -> Optional[Dict[str, Any]]:
    """Connector geometry from its numbered pads (board coordinates): pins_across, pins_along, pitch, numbering
    and angle such that pin_xy + the footprint rotation puts every pad where it is; None if no grid fits."""
    from .geometry import to_board
    pads = sorted(pads)
    n = len(pads)
    if n < 2 or [p[0] for p in pads] != list(range(1, n + 1)):
        return None
    x1, y1 = pads[0][1], pads[0][2]
    d = sorted(math.hypot(a[1] - b[1], a[2] - b[2]) for i, a in enumerate(pads) for b in pads[i + 1:i + 9])
    pitch = round(d[0], 3) if d else 2.54
    best = None
    for across in (1, 2, 3, 4):
        if n % across:
            continue
        for numbering in NUMBERING:
            for ang in (0.0, 90.0, 180.0, 270.0):
                c = {"pins_across": across, "pins_along": n // across, "pitch_mm": pitch, "numbering": numbering}
                err = max(math.hypot(px - q[0], py - q[1]) for (num, px, py) in pads
                          for q in [to_board(x1, y1, ang, *pin_xy(c, num))])
                if best is None or err < best[0] - 1e-9:
                    best = (err, dict(c, angle=ang))
    return best[1] if best and best[0] < 0.05 else None


def facility_from_pcb(path: str, connector: Optional[str] = None, ident: Optional[str] = None,
                      name: Optional[str] = None, extra: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], List[str]]:
    """A station facility read from the station's own KiCad board: the plate is the board outline, the holes are
    its mounting holes and the interface connector (``connector`` = its reference, else the one whose pads carry
    the most station nets) is one shared connector for every board of a batch, its pin functions read from the
    net names (GND, AIN1.., VOUT1.., DIO1.., CAN1_H/CAN1_L, UART1_TX/RX, I2C_SDA/SCL, RS485_A/B, USB_DP/DM).
    ``extra`` is merged on top (analog.max_v, power limits, runner driver, batch.max_sites, ...).
    Returns (facility, warnings)."""
    from .pcb import load_board
    from . import classify as C_
    b = load_board(path)
    bb = b.outline_bbox()
    warns: List[str] = []
    best = None
    for fp in b.footprints:
        if connector and fp.ref != connector:
            continue
        nums = [p for p in fp.pads if str(p.number).isdigit()]
        fns = {str(p.number): station_pin_function(p.net) for p in nums}
        score = sum(1 for f in fns.values() if f and f != "GND")
        if score and (best is None or score > best[0]):
            best = (score, fp, fns)
    if best is None:
        raise ValueError("no interface connector found on %s%s: name its nets AIN1.., VOUT1.., GND, CAN1_H ..."
                         % (os.path.basename(path), " (reference %s)" % connector if connector else ""))
    _score, fp, fns = best
    pads = [(int(p.number), p.x, p.y) for p in fp.pads if str(p.number).isdigit()]
    uniq = {}
    for num, x, y in pads:
        uniq.setdefault(num, (num, x, y))
    geo = _grid(list(uniq.values()))
    if geo is None:
        raise ValueError("%s on %s is not a connector on a regular grid (pins numbered 1..N)" % (fp.ref, os.path.basename(path)))
    unknown = sorted({p.net for p in fp.pads if p.net and station_pin_function(p.net) is None})
    if unknown:
        warns.append("%s: nets not recognised as station signals (left unconnected on fixtures): %s"
                     % (fp.ref, ", ".join(unknown[:12]) + (" ..." if len(unknown) > 12 else "")))
    pins = {num: (fns.get(num) or "NC") for num in sorted(fns, key=int)}
    x1, y1 = uniq[1][1], uniq[1][2]
    lib = fp.lib_id
    model = next((m.path for m in fp.models if not m.hide), None)
    for a, bmate in _MATE:
        if a in lib:
            lib, model = lib.replace(a, bmate), (model.replace(a, bmate) if model else model)
            break
    holes = []
    for f2 in b.footprints:
        for pd in f2.pads:
            if pd.drill_w >= 2.0 and (pd.is_npth or C_.is_mounting_hole(f2) or not pd.net):
                holes.append({"at_mm": [round(pd.x - bb.x0, 3), round(pd.y - bb.y0, 3)], "d_mm": round(pd.drill_w, 2),
                              "label": "M%d" % int(round(pd.drill_w - 0.2)) if pd.drill_w < 6 else "HOLE"})
    funcs = list(pins.values())
    have = {bp for bp in (bus_pin(f) for f in funcs) if bp}
    buses = {bus: {"via": "fixture"} for bus in BUSES if _instances(have, bus)}
    base = os.path.splitext(os.path.basename(path))[0]
    fac: Dict[str, Any] = {
        "schema": SCHEMA,
        "id": ident or re.sub(r"[^a-z0-9._-]+", "-", base.lower()).strip("-") or "station",
        "name": name or (b.title_block.get("title") or base),
        "description": "Test station read from %s: interface connector %s (%d pins), %.0f x %.0f mm plate." % (
            os.path.basename(path), fp.ref, len(pins), bb.w, bb.h),
        "analog": {"inputs": sum(1 for f in funcs if f.startswith("AIN"))},
        "power": {"outputs": len({f for f in funcs if f.startswith("VOUT")}), "switched": True},
        "buses": buses,
        "fixture": {
            "plate_mm": [round(bb.w, 3), round(bb.h, 3)],
            "holes": holes,
            "connectors": [dict(geo, name=fp.ref, shared=True, at_mm=[round(x1 - bb.x0, 3), round(y1 - bb.y0, 3)],
                                footprint=lib, pins=pins, **({"model": model} if model else {}))],
        },
        "station_pcb": os.path.basename(path),
    }
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(fac.get(k), dict):
            fac[k] = dict(fac[k], **v)
        else:
            fac[k] = v
    r = validate(fac)
    if not r["ok"]:
        raise ValueError("the station read from %s is not valid: %s" % (os.path.basename(path), "; ".join(r["errors"][:4])))
    warns += r["warnings"]
    if not fac["analog"]["inputs"]:
        warns.append("no AIN pins on %s: boards on this station get no measurement channels" % fp.ref)
    return fac, warns
