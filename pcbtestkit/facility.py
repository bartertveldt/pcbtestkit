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
_PIN_FUNC = re.compile(r"^(GND|NC|AIN\d+|VOUT\d+|DIO\d+|%s)$" % "|".join(BUS_OF))
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
    wired: Dict[int, set] = {}
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
        for num, fn in pins.items():
            if not str(num).isdigit() or not 1 <= int(num) <= across * along:
                E.append("%s.pins: pin %s is not on a %d x %d connector" % (where, num, across, along))
            if not _PIN_FUNC.match(str(fn)):
                E.append("%s.pins.%s: %r is not a pin function (GND, NC, AIN<n>, VOUT<n>, DIO<n>, %s)" % (
                    where, num, fn, ", ".join(BUS_OF)))
            elif fn in BUS_OF:
                wired.setdefault(site, set()).add(fn)
        if "GND" not in pins.values():
            Wn.append("%s has no GND pin" % where)
    for bus, b in buses.items():
        if bus in BUSES and (b or {}).get("via", "fixture") == "fixture":
            for site, have in sorted(wired.items()) or [(1, set())]:
                miss = [s for s in BUSES[bus] if s not in have]
                if miss:
                    E.append("buses.%s goes through the fixture but site %d's connector has no %s pin" % (
                        bus, site, ", ".join(miss)))
    drv = (raw.get("runner") or {}).get("driver")
    if drv and raw.get("_dir") and not os.path.exists(os.path.join(raw["_dir"], drv)):
        E.append("runner.driver: %s not found next to the facility file" % drv)
    return {"ok": not E, "errors": E, "warnings": Wn}


# --------------------------------------------------------------------------- #
# what the facility means for a board
# --------------------------------------------------------------------------- #
def connectors(fac: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list((fac.get("fixture") or {}).get("connectors") or [])


def analog_limits(fac: Dict[str, Any]) -> Tuple[Optional[int], Optional[float]]:
    """(analog inputs per board, their full-scale voltage); None = the test plan's own settings."""
    an = fac.get("analog") or {}
    n = an.get("inputs")
    if connectors(fac):                    # a fixed connector: as many channels as it has AIN pins per site
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


def assign_pins(fac: Dict[str, Any], channels: List[str], supply: bool, buses: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """What each facility connector pin carries for this board, per connector: channels CH01.. on AIN1.. in order,
    the supply on VOUT1, GND, the buses wired through the fixture; everything else is left open."""
    wired = {sig for b in buses if b["via"] == "fixture" for sig in b["signals"]}
    out = []
    for c in connectors(fac):
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
            pins.append({"pin": int(num), "function": fn, "signal": sig})
        out.append({"name": c.get("name", "TESTER"), "site": c.get("site", 1), "pins": pins})
    return out


def summary(fac: Dict[str, Any]) -> Dict[str, Any]:
    """The facility as the test plan records it (no file paths)."""
    n, vmax = analog_limits(fac)
    fx = fac.get("fixture") or {}
    return {"id": fac["id"], "name": fac.get("name", fac["id"]), "analog_inputs": n, "max_input_v": vmax,
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
