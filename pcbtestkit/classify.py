"""Heuristic classification of components and nets, plus value parsing.

Everything here is deterministic and explainable. Each heuristic returns
*why* it decided something where that matters, so a human or an AI model can
override the decision through the project config instead of guessing.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .pcb import Board, Footprint

# --------------------------------------------------------------------------- #
# Value parsing
# --------------------------------------------------------------------------- #
_SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "": 1.0, "k": 1e3, "K": 1e3,
       "M": 1e6, "G": 1e9, "R": 1.0, "r": 1.0}


def parse_si(text: str, unit_letters: str = "") -> Optional[float]:
    """Parse '4k7', '4.7k', '100n', '2u2', '1R5', '10 kΩ', '3.3V', '100mW' -> float."""
    if text is None:
        return None
    t = str(text).strip().replace(",", ".").replace("Ω", "").replace("ohm", "").replace("Ohm", "")
    for u in unit_letters:
        t = re.sub(r"(?<=[0-9pnuµμmkKMGRr])\s*%s\b" % re.escape(u), "", t)
    t = t.replace(" ", "")
    m = re.match(r"^([0-9]*\.?[0-9]+)([pnuµμmkKMGRr]?)([0-9]*)", t)
    if not m:
        return None
    whole, pre, frac = m.groups()
    try:
        base = float(whole + ("." + frac if frac and "." not in whole else ""))
    except ValueError:
        return None
    return base * _SI.get(pre, 1.0)


def parse_voltage(text: str) -> Optional[float]:
    """Find a voltage rating inside free text: '10uF 16V', '50V', 'X7R/25V', '6V3'."""
    if not text:
        return None
    t = str(text)
    m = re.search(r"(\d+)V(\d+)(?![0-9])", t)           # 6V3
    if m:
        return float("%s.%s" % (m.group(1), m.group(2)))
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(k?)V(?:DC|dc)?(?![a-zA-Z0-9])", t)
    if m:
        v = float(m.group(1).replace(",", "."))
        return v * 1000 if m.group(2) else v
    return None


def parse_power_w(text: str) -> Optional[float]:
    if not text:
        return None
    t = str(text).strip()
    m = re.search(r"(\d+)\s*/\s*(\d+)\s*W", t)
    if m:
        return float(m.group(1)) / float(m.group(2))
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(m?)W\b", t)
    if m:
        v = float(m.group(1).replace(",", "."))
        return v / 1000 if m.group(2) else v
    return None


# --------------------------------------------------------------------------- #
# Net classification
# --------------------------------------------------------------------------- #
_GND = re.compile(r"(^|[^A-Z])(A|D|P|S|C|E|I)?GND|^VSS|^0V$|^EARTH|^CHASSIS|^PE$|^GROUND", re.I)
_POWER_NAMES = re.compile(r"^(V(CC|DD|BUS|IN|BAT|SYS|MOT|SUPPLY|MAIN|EXT|USB|DC|AA|PP|REF|IO|CORE|LED|OUT|\+)|"
                          r"\+?\d+V\d*|\+|PWR|POWER|BATT?|VCC|VDD)", re.I)


def base_net(name: str) -> str:
    n = name or ""
    if n.startswith("Net-(") or n.startswith("unconnected-("):
        return n
    return n.rsplit("/", 1)[-1]


def is_unconnected(name: str) -> bool:
    return (not name) or name.startswith("unconnected-(")


def is_ground(name: str) -> bool:
    b = base_net(name)
    if b.startswith("Net-("):
        return False
    return bool(_GND.search(b))


def net_voltage(name: str) -> Optional[float]:
    """Infer the nominal voltage from a net name: '+3V3' -> 3.3, 'VIN_12V' -> 12, 'GND' -> 0."""
    b = base_net(name)
    if not b or b.startswith("Net-(") or b.startswith("unconnected-("):
        return None
    if is_ground(b):
        return 0.0
    u = b.upper()
    sign = -1.0 if u.startswith("-") or u.startswith("N") and re.match(r"^N\d", u) else 1.0
    m = re.search(r"(?<![A-Z0-9.])[+-]?(\d+)V(\d+)(?![0-9])", u)
    if not m:
        m = re.search(r"(?<=[A-Z_+-])(\d+)V(\d+)(?![0-9])", u)
    if m:
        return sign * float("%s.%s" % (m.group(1), m.group(2)))
    m = re.search(r"(?<![A-Z0-9.])[+-]?(\d+(?:\.\d+)?)V(?![A-Z0-9])", u)
    if not m:
        m = re.search(r"(?<=[_+-])(\d+(?:\.\d+)?)V(?![A-Z0-9])", u)
    if m:
        return sign * float(m.group(1))
    m = re.search(r"V(?:DD|CC|IO|CORE)?_?(\d+)P(\d+)V?$", u)  # VDD1P8, V3P3
    if m:
        return float("%s.%s" % (m.group(1), m.group(2)))
    if "VBUS" in u or u in ("USB_5V", "VUSB"):
        return 5.0
    return None


# Tokens that make a net a control/sense/data signal even if a voltage appears in its name
# (e.g. UART_TX_1V8, 16V_FB, 3V3_EN, VBUS_DET, 12V_PHASE).
SIGNAL_TOKENS = {
    "FB", "VFB", "MODE", "MODE1", "MODE2", "BOOT", "BST", "PHASE", "PH", "SW", "LX", "EN", "ENABLE", "PG", "PGOOD",
    "PWRGD", "DET", "DETECT", "MON", "MONITOR", "SENSE", "SNS", "ISET", "ILIM", "IMON", "TX", "RX", "TXD", "RXD", "SCL",
    "SDA", "IRQ", "INT", "CS", "CLK", "SCK", "MISO", "MOSI", "RST", "RESET", "NRST", "TACH", "PWM", "GPIO", "PEN", "ON",
    "OFF", "CTRL", "SEL", "ALERT", "FAULT", "FLT", "ADC", "COMP", "SS", "TRK", "RT", "GATE", "HG", "LG", "HO", "LO",
    "DRV_EN", "DIV", "SIG", "DATA", "D", "DP", "DM", "CC", "CC1", "CC2", "SBU", "OK", "STAT", "STATUS", "CHG", "LED",
    "BTN", "KEY", "PROG", "SET", "TRIM", "VSET", "NTC", "TEMP", "SYNC", "ACK", "REQ", "WAKE", "SLEEP", "STBY_N",
}


def _tokens(b: str) -> List[str]:
    return [t for t in re.split(r"[^A-Z0-9]+", b.upper().replace("~", "").replace("{", "").replace("}", "")) if t]


def signal_token(name: str) -> str:
    for t in _tokens(base_net(name)):
        if t in SIGNAL_TOKENS:
            return t
    return ""


def classify_net(name: str, power_symbol_nets: Iterable[str] = (), pintypes: Iterable[str] = ()) -> Tuple[str, str]:
    """Return (kind, reason). kind is 'ground' | 'power' | 'signal' | 'unconnected'."""
    if is_unconnected(name):
        return "unconnected", "unconnected pin"
    b = base_net(name)
    if is_ground(name):
        return "ground", "ground-style net name"
    if name in power_symbol_nets or b in power_symbol_nets:
        return "power", "driven by a schematic power symbol"
    tok = signal_token(name) if not b.startswith("Net-(") else ""
    if tok:
        return "signal", "control/sense signal (name contains %s)" % tok
    if not b.startswith("Net-("):
        if net_voltage(name) is not None:
            return "power", "voltage encoded in the net name"
        if _POWER_NAMES.match(b.lstrip("+")) or _POWER_NAMES.match(b):
            return "power", "power-style net name"
    pts = set(pintypes)
    if "power_out" in pts:
        return "power", "connected to a power_out pin"
    return "signal", "default"


# --------------------------------------------------------------------------- #
# Component classification
# --------------------------------------------------------------------------- #
_PKG = re.compile(r"(?:^|[_:\-])(01005|0201|0402|0603|0805|1206|1210|1806|1812|2010|2220|2512)(?:_|$|Metric)")
_PKG_POWER_W = {"01005": 0.031, "0201": 0.05, "0402": 0.0625, "0603": 0.1, "0805": 0.125, "1206": 0.25,
                "1210": 0.5, "1812": 0.75, "2010": 0.75, "2512": 1.0}


def passive_size(fp: Footprint) -> str:
    m = _PKG.search(fp.name)
    return m.group(1) if m else ""


def has_prefix(fp: Footprint, *prefixes: str) -> bool:
    p = fp.prefix
    return any(p == x for x in prefixes)


def _txt(fp: Footprint) -> str:
    return " ".join([fp.lib_id, fp.value, fp.descr, fp.get_field("Description", "Datasheet", "MPN")]).lower()


def is_capacitor(fp: Footprint) -> bool:
    return has_prefix(fp, "C") or fp.library.startswith("Capacitor")


_POLARIZED = re.compile(r"(?<![a-z])(?<!un)(?<!non-)(?<!non )polari[sz]ed|electrolytic|(?<![a-z])elko(?![a-z])")


def is_electrolytic(fp: Footprint) -> bool:
    if not is_capacitor(fp):
        return False
    t = _txt(fp)
    if "tantal" in t or "cp_eia" in t:
        return False
    name = fp.name.lower()
    if name.startswith(("cp_elec", "cp_radial", "cp_axial", "c_elec")) or "_elec_" in name:
        return True
    return bool(_POLARIZED.search(t))


def is_tantalum(fp: Footprint) -> bool:
    t = _txt(fp)
    return is_capacitor(fp) and ("tantal" in t or "cp_eia" in t)


def is_mlcc(fp: Footprint) -> bool:
    return is_capacitor(fp) and fp.is_smd and not is_electrolytic(fp) and not is_tantalum(fp) and bool(passive_size(fp))


def is_resistor(fp: Footprint) -> bool:
    return (has_prefix(fp, "R") or fp.library.startswith("Resistor")) and not has_prefix(fp, "RV", "RN")


def is_inductor(fp: Footprint) -> bool:
    return has_prefix(fp, "L", "FB") or fp.library.startswith("Inductor")


def is_diode(fp: Footprint) -> bool:
    return has_prefix(fp, "D", "LED", "CR") or fp.library.startswith(("Diode", "LED"))


def is_ic(fp: Footprint) -> bool:
    return has_prefix(fp, "U", "IC")


def is_connector(fp: Footprint) -> bool:
    if has_prefix(fp, "J", "P", "CN", "X", "USB", "CON", "JP") and not has_prefix(fp, "PS"):
        return True
    t = fp.lib_id.lower()
    return t.startswith("connector") or any(k in t for k in ("usb_", "pinheader", "pinsocket", "terminalblock",
                                                            "barreljack", "rj45", "jst_", "molex", "idc-header"))


def is_external_connector(fp: Footprint) -> bool:
    t = fp.lib_id.lower()
    return is_connector(fp) and any(k in t for k in ("usb", "rj45", "rj11", "jack", "dsub", "sma", "bnc", "hdmi",
                                                      "terminalblock", "barrel", "microsd", "sd_", "m12", "phoenix"))


_STACK_BOARD = {"arduino", "shield", "hat", "feather", "featherwing", "pololu", "teensy", "nucleo", "xiao", "mezzanine",
                "daughterboard", "raspberrypi", "rpi"}
_STACK_BTB = re.compile(r"\b(board to board|btb|slimstack|stacking|df40\w*|df12\w*|df17\w*|fx8\w*|fx10\w*|fx18\w*|"
                        r"fx23\w*|qsh|qth|qse|qte|erf8|erm8|bse|bte)\b")


def stacking_hint(fp: Footprint) -> Optional[str]:
    """Why this footprint may carry a stacked board (daughter board, shield, display), or None.

    Board footprints (a Raspberry Pi, Arduino shield or module drawn as one part), board-to-board / mezzanine
    connectors, and pin sockets (female headers) are candidates; the user confirms them."""
    words = " ".join(re.findall(r"[a-z0-9]+", ("%s %s %s" % (fp.lib_id, fp.value, fp.descr)).lower()))
    toks = set(words.split())
    if toks & _STACK_BOARD or "raspberry pi" in words:
        return "footprint of a board (%s)" % fp.name
    if _STACK_BTB.search(words):
        return "board-to-board connector (%s)" % fp.name
    if "pinsocket" in toks or "pin socket" in words or "female header" in words or "socket strip" in words:
        return "pin socket (%s): a board may plug into it" % fp.name
    return None


def is_testpoint(fp: Footprint) -> bool:
    return has_prefix(fp, "TP") or "testpoint" in fp.lib_id.lower() or "test_point" in fp.lib_id.lower()


def is_fiducial(fp: Footprint) -> bool:
    return has_prefix(fp, "FID", "FD") or "fiducial" in fp.lib_id.lower()


def is_mounting_hole(fp: Footprint) -> bool:
    lib, _, name = fp.lib_id.lower().rpartition(":")
    # the MountingHole library or a footprint named so (not e.g. "DSUB-9_..._MountingHolesOffset9.12mm")
    if lib.startswith("mountinghole") or name.startswith("mountinghole") or has_prefix(fp, "H", "MH"):
        return True
    return bool(fp.pads) and all((p.is_npth or (p.is_tht and not p.net)) and p.drill >= 2.0 for p in fp.pads)


def is_polarized(fp: Footprint) -> bool:
    return is_diode(fp) or is_electrolytic(fp) or is_tantalum(fp)


def is_esd_device(fp: Footprint) -> bool:
    t = _txt(fp)
    return any(k in t for k in ("esd", "tvs", "pesd", "usblc", "smaj", "smbj", "smcj", "prtr", "tpd", "sp05",
                                "rclamp", "ip4220", "transient"))


_HEAT_FP = re.compile(r"to-220|to-247|to-263|to-252|d2pak|dpak|sot-223|powerpak|to-3p|lfpak|d-pak|sot-89", re.I)
_EP_FP = re.compile(r"_ep|-1ep|hsop|pqfn|powerpad|thermal", re.I)
_HEAT_VAL = re.compile(r"78\d\d|79\d\d|lm317|lm337|ams1117|ld1117|lm1117|ap2112|lm2596|lm2576|mp1584|tps5|"
                       r"tps6|lt\d{4}|irf|irl|ao\d{4}|si\d{4}|mosfet|regulator|ldo|buck|boost|driver|"
                       r"drv8|a4988|tb6612|l298|uln2003|tip1\d\d|bd13\d|power", re.I)


def heat_source_reason(fp: Footprint) -> str:
    if is_ic(fp) or has_prefix(fp, "Q", "VR", "PS"):
        if _HEAT_FP.search(fp.name):
            return "power package (%s)" % fp.name
        if _HEAT_VAL.search(fp.value) or _HEAT_VAL.search(fp.get_field("Description")):
            return "power device (%s)" % fp.value
        if _EP_FP.search(fp.name) and _HEAT_VAL.search(" ".join([fp.value, fp.get_field("Description")])):
            return "power device in exposed-pad package (%s)" % fp.value
    if is_inductor(fp) and fp.courtyard_bbox().w * fp.courtyard_bbox().h >= 16:
        return "power inductor"
    if is_resistor(fp) and passive_size(fp) in ("2010", "2512"):
        return "power resistor (%s)" % passive_size(fp)
    return ""


# --------------------------------------------------------------------------- #
# Component height estimation (used by the solder jig and the probe fixture)
# --------------------------------------------------------------------------- #
_RADIAL_H = [(5.0, 11.0), (6.3, 11.0), (8.0, 11.5), (10.0, 16.0), (12.5, 20.0), (16.0, 25.0), (18.0, 35.0),
             (22.0, 40.0), (25.0, 45.0), (30.0, 50.0), (35.0, 55.0)]

_NAME_HEIGHTS = [
    (r"PinHeader.*_Vertical", 8.5), (r"PinSocket.*_Vertical", 8.5), (r"PinHeader.*_Horizontal", 2.6),
    (r"PinSocket.*_Horizontal", 2.9), (r"IDC-Header", 9.0), (r"BoxHeader", 9.0),
    (r"TO-220.*_Vertical", 20.0), (r"TO-220.*_Horizontal", 5.0), (r"TO-247.*_Vertical", 22.0),
    (r"TO-92", 8.0), (r"DIP-\d+.*Socket", 5.5), (r"DIP-\d+", 4.5),
    (r"_Axial_.*_Vertical", 10.0), (r"_Axial_.*_Horizontal", 3.0), (r"DO-41", 2.8), (r"DO-201", 5.5),
    (r"C_Disc_D(\d+(?:\.\d+)?)", None), (r"C_Rect_", 10.0), (r"TerminalBlock", 10.0), (r"BarrelJack", 11.0),
    (r"USB_A", 7.0), (r"USB_B", 11.0), (r"USB_C", 3.3), (r"Micro.?USB", 3.0), (r"RJ45", 13.5), (r"Relay", 15.6),
    (r"Crystal_HC49-U_Vertical", 13.5), (r"HC49", 4.0), (r"Buzzer", 9.5), (r"SW_PUSH_6mm", 5.0),
    (r"SW_PUSH_12mm", 7.3), (r"Potentiometer", 10.0), (r"Fuseholder", 10.0), (r"JST_XH.*_Vertical", 9.6),
    (r"JST_PH.*_Vertical", 6.0), (r"JST_EH", 8.0), (r"Molex_KK", 11.0), (r"LED_D5\.0mm", 8.6),
    (r"LED_D3\.0mm", 5.3), (r"Transformer", 20.0), (r"Heatsink", 25.0), (r"BatteryHolder", 16.0),
    (r"Arduino|RaspberryPi|ESP32-DevKit|Module.*Socket", 9.0), (r"DSUB", 12.5), (r"SMA_", 9.0),
    (r"Jack_3\.5mm", 6.0),
]


def estimate_height(fp: Footprint) -> Tuple[float, str]:
    """(height above the board in mm, source). Override with a 'Height' field."""
    for key in ("Height", "JigHeight", "Component Height", "height_mm"):
        v = fp.get_field(key)
        if v:
            h = parse_si(v, "m")
            if h:
                return (h * 1000 if h < 0.2 else h), "field '%s'" % key
    n = fp.name
    m = re.search(r"CP_Radial_D(\d+(?:\.\d+)?)mm.*?_H(\d+(?:\.\d+)?)mm", n)
    if m:
        return float(m.group(2)), "footprint name"
    m = re.search(r"CP_Radial_D(\d+(?:\.\d+)?)mm", n) or re.search(r"L_Radial_D(\d+(?:\.\d+)?)mm", n)
    if m:
        d = float(m.group(1))
        for dd, hh in _RADIAL_H:
            if d <= dd + 0.01:
                return hh, "typical height for D%gmm radial" % d
        return d * 1.6, "typical height for D%gmm radial" % d
    m = re.search(r"CP_Elec_(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)", n)
    if m:
        return float(m.group(2)) + 0.3, "footprint name"
    m = re.search(r"_H(\d+(?:\.\d+)?)mm", n)
    if m:
        return float(m.group(1)), "footprint name"
    for pat, h in _NAME_HEIGHTS:
        mm = re.search(pat, n, re.I)
        if mm:
            if h is None:
                return float(mm.group(1)) + 2.0, "footprint name"
            return h, "typical height for %s" % pat.replace("\\", "").replace(".*", "*")
    size = passive_size(fp)
    if size:
        return {"01005": 0.2, "0201": 0.3, "0402": 0.55, "0603": 0.9, "0805": 1.3, "1206": 1.6, "1210": 2.5,
                "1812": 2.5, "2010": 0.7, "2512": 0.7}.get(size, 1.0), "typical for %s" % size
    if fp.is_tht:
        return 10.0, "default for THT (set a 'Height' field to override)"
    cb = fp.courtyard_local()
    area = cb.w * cb.h if cb.valid else 1.0
    return (1.0 if area < 20 else 2.5), "default for SMD"


def mpn_of(fp: Footprint) -> str:
    return fp.get_field("MPN", "Manufacturer_Part_Number", "Mfr. Part #", "Mfr Part Number", "Manufacturer Part Number",
                        "Part Number", "PN", "MFN", "MFPN", "LCSC", "LCSC Part", "JLCPCB Part #", "Digikey",
                        "DigiKey_PN", "Mouser")


def rated_voltage(fp: Footprint) -> Tuple[Optional[float], str]:
    for key in ("Voltage", "Rated Voltage", "Voltage Rating", "Vrated", "V Rating", "Rating", "Vmax"):
        v = fp.get_field(key)
        if v:
            pv = parse_voltage(v) or parse_si(v, "V")
            if pv:
                return pv, "field '%s'" % key
    pv = parse_voltage(fp.value)
    if pv:
        return pv, "value text"
    pv = parse_voltage(fp.get_field("Description"))
    if pv:
        return pv, "description"
    return None, ""


def rated_power(fp: Footprint) -> Tuple[Optional[float], str]:
    for key in ("Power", "Power Rating", "Wattage", "Pmax"):
        v = fp.get_field(key)
        if v:
            p = parse_power_w(v)
            if p:
                return p, "field '%s'" % key
    p = parse_power_w(fp.value)
    if p:
        return p, "value text"
    size = passive_size(fp)
    if size in _PKG_POWER_W:
        return _PKG_POWER_W[size], "typical for %s" % size
    return None, ""


def net_pintypes(board: Board) -> Dict[str, Set[str]]:
    out: Dict[str, Set[str]] = {}
    for p in board.all_pads():
        if p.net and p.pintype:
            out.setdefault(p.net, set()).add(p.pintype)
    return out


def classify_all_nets(board: Board, power_symbol_nets: Iterable[str] = ()) -> Dict[str, Dict[str, object]]:
    pts = net_pintypes(board)
    psn = set(power_symbol_nets)
    out: Dict[str, Dict[str, object]] = {}
    for n in board.net_names():
        kind, reason = classify_net(n, psn, pts.get(n, ()))
        out[n] = {"kind": kind, "reason": reason, "voltage": net_voltage(n) if kind in ("power", "ground") else None}
    return out


def refs_compact(refs: List[str], limit: int = 40) -> str:
    refs = sorted(set(refs), key=natural_key)
    if len(refs) > limit:
        return ", ".join(refs[:limit]) + " ... (+%d more)" % (len(refs) - limit)
    return ", ".join(refs)


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s or "")]
