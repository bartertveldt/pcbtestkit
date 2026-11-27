"""A test station's own KiCad board, made from what it has to offer (pcbtestkit.environment designs that).

The board is the station's top: its outline is the plate every fixture copies, its mounting holes are where every
fixture is screwed down, and its interface socket(s) carry, named by net, every analog input (AIN<n>), switched
supply (VOUT<n>), bus instance (CAN<k>_H, UART<k>_TX, I2C<k>_SDA, RS485_<k>_A, USB<k>_DP, ETH<k>_TX_P, ...), digital
line (DIO<n>) and GND every few pins. facility.facility_from_pcb reads it back, so a station edited in KiCad (moved
socket, other pin map, bigger plate) is what the next run fits the boards to.

The electronics behind the pins are drawn as real footprints (ADCs, transceivers, Ethernet and USB jacks, supply
switches, the Raspberry Pi header) so the board renders as a station and lists what to fit; they carry no nets.
"""
from __future__ import annotations

import json
import math
import os
from typing import Any, Dict, List, Sequence, Tuple

from . import kicad_writer as W

M3D = "${KICAD8_3DMODEL_DIR}/"
THT = ["*.Cu", "*.Mask"]
FCU = ["F.Cu", "F.Paste", "F.Mask"]
X0, Y0 = 50.0, 50.0
PITCH = 2.54
BUS_ORDER = ("can", "uart", "i2c", "rs485", "usb", "ethernet")
BUS_NETS = {
    "can": lambda k: ["CAN%d_H" % k, "CAN%d_L" % k],
    "uart": lambda k: ["UART%d_TX" % k, "UART%d_RX" % k],
    "i2c": lambda k: ["I2C%d_SDA" % k, "I2C%d_SCL" % k],
    "rs485": lambda k: ["RS485_%d_A" % k, "RS485_%d_B" % k],
    "usb": lambda k: ["USB%d_DP" % k, "USB%d_DM" % k],
    "ethernet": lambda k: ["ETH%d_TX_P" % k, "ETH%d_TX_N" % k, "ETH%d_RX_P" % k, "ETH%d_RX_N" % k],
}
BUS_PINS = {bus: len(f(1)) for bus, f in BUS_NETS.items()}
MAX_POSITIONS = 40                      # 2 x 40: the largest 2.54 mm socket in KiCad's library


def signal_groups(res: Dict[str, Any]) -> List[List[str]]:
    """The station's signals in groups that stay together on the socket (a bus instance's lines side by side)."""
    groups: List[List[str]] = [["AIN%d" % i] for i in range(1, int(res.get("ain", 0)) + 1)]
    groups += [["VOUT%d" % i] for i in range(1, int(res.get("vout", 0)) + 1)]
    for bus in BUS_ORDER:
        for k in range(1, int((res.get("buses") or {}).get(bus, 0)) + 1):
            groups.append(BUS_NETS[bus](k))
    groups += [["DIO%d" % i] for i in range(1, int(res.get("dio", 0)) + 1)]
    return groups


def pin_count(res: Dict[str, Any], gnd_every: int = 6) -> int:
    """Pins the station's socket(s) need: its signals, GND every ``gnd_every`` signals and two at each end."""
    return sum(len(p) for p in socket_pins(res, gnd_every, 10 ** 6))


def socket_pins(res: Dict[str, Any], gnd_every: int = 6, max_pins: int = 2 * MAX_POSITIONS) -> List[List[str]]:
    """Per socket, its pins in order (pin 1 first): GND GND, the signal groups with GND between every
    ``gnd_every`` signals, GND GND, then NC up to an even count. A new socket starts when one is full."""
    out: List[List[str]] = []
    cur: List[str] = ["GND", "GND"]
    since = 0
    for g in signal_groups(res):
        gnd = 1 if since >= gnd_every else 0
        if len(cur) + gnd + len(g) + 2 > max_pins:      # this socket is full: close it, start the next
            out.append(cur + ["GND", "GND"])
            cur, since, gnd = ["GND", "GND"], 0, 0
        if gnd:
            cur.append("GND")
            since = 0
        cur += g
        since += len(g)
    out.append(cur + ["GND", "GND"])
    for s in out:
        if len(s) % 2:
            s.append("NC")
    return out


def upgrade_sockets(old: Sequence[Sequence[str]], res: Dict[str, Any], gnd_every: int = 6,
                    max_pins: int = 2 * MAX_POSITIONS) -> List[List[str]]:
    """The sockets of a station that gets more: every pin it has keeps its signal (the fixtures made for it still
    plug in, aligned at pin 1); the new signals follow on the last socket while it can grow, then on new ones."""
    have = {n for sk in old for n in sk}
    new_groups = [g for g in signal_groups(res) if not any(n in have for n in g)]
    out = [list(sk) for sk in old]
    if not new_groups:
        return out
    cur = out.pop() if out else ["GND", "GND"]
    grew = bool(out) or bool(old)
    since = gnd_every                                   # a GND before the first new signal
    for g in new_groups:
        gnd = 1 if since >= gnd_every else 0
        if len(cur) + gnd + len(g) + 2 > max_pins:      # full: close it, start the next
            out.append(cur + (["GND", "GND"] if not grew else []))
            cur, since, gnd, grew = ["GND", "GND"], 0, 0, False
        if gnd:
            cur.append("GND")
            since = 0
        cur += g
        since += len(g)
    out.append(cur + ["GND", "GND"])
    for sk in out:
        if len(sk) % 2:
            sk.append("NC")
    return out


def numbered(fn: str) -> str:
    """A pin function as this module names its net: the facility reader calls a bus's first instance CAN_H,
    UART_TX, ETH_TX_P, RS485_A, ...; the station board names it CAN1_H, UART1_TX, ETH1_TX_P, RS485_1_A."""
    import re
    m = re.match(r"^(CAN|UART|I2C|USB)_(H|L|TX|RX|SDA|SCL|DP|DM)$", fn)
    if m:
        return "%s1_%s" % (m.group(1), m.group(2))
    m = re.match(r"^ETH_(TX_P|TX_N|RX_P|RX_N)$", fn)
    if m:
        return "ETH1_" + m.group(1)
    m = re.match(r"^RS485_(A|B)$", fn)
    if m:
        return "RS485_1_" + m.group(1)
    return fn


def keep_of(fac: Dict[str, Any]) -> Dict[str, Any]:
    """What an existing station's fixtures rely on (read from its facility): every socket's pins in order and its
    pin 1, the mounting holes, the plate."""
    fx = fac.get("fixture") or {}
    conns = fx.get("connectors") or []
    return {"sockets": [[numbered(str(c["pins"][k])) for k in sorted(c["pins"], key=int)] for c in conns],
            "places": [[float(c["at_mm"][0]), float(c["at_mm"][1])] for c in conns],
            "holes": [[float(h["at_mm"][0]), float(h["at_mm"][1])] for h in fx.get("holes") or []],
            "plate_mm": [float(v) for v in fx.get("plate_mm") or [0.0, 0.0]]}


# --------------------------------------------------------------------------- #
# the board
# --------------------------------------------------------------------------- #
def _socket(ref: str, x: float, y: float, pins: List[str], net: Dict[str, Tuple[int, str]]) -> list:
    n = len(pins) // 2
    pads = []
    for num, name in enumerate(pins, 1):
        i, j = (num - 1) % 2, (num - 1) // 2
        pads.append(W.pad(str(num), "thru_hole", "rect" if num == 1 else "oval", PITCH * i, PITCH * j, 1.7, 1.7, THT,
                          drill=1.0, net=net.get(name) if name != "NC" else None, angle=90,
                          pinfunction=name if name != "NC" else "", pintype="passive"))
    pads += [W.fp_rect(-1.27, -1.27, 3.81, PITCH * (n - 1) + 1.27, "F.Fab", 0.1),
             W.fp_rect(-1.8, -1.8, 4.34, PITCH * (n - 1) + 1.8, "F.CrtYd", 0.05)]
    lib = "Connector_PinSocket_2.54mm:PinSocket_2x%02d_P2.54mm_Vertical" % n
    return W.footprint(lib, x, y, ref, "FIXTURE", pads, angle=90, attr=("through_hole",), ref_pos=(-3.5, 0),
                       models=[M3D + "Connector_PinSocket_2.54mm.3dshapes/PinSocket_2x%02d_P2.54mm_Vertical.step" % n])


def _so(ref: str, value: str, x: float, y: float, pins: int) -> list:
    """SOIC-8 / SOIC-16 (no nets: the station's electronics as a parts list on the board)."""
    half = pins // 2
    pads = [W.pad(str(n), "smd", "roundrect", (-2.475 if n <= half else 2.475),
                  (-(half - 1) * 0.635 + 1.27 * ((n - 1) % half)) if n <= half else ((half - 1) * 0.635 - 1.27 * ((n - half - 1) % half)),
                  1.95, 0.6, FCU, rratio=0.25) for n in range(1, pins + 1)]
    name = "SOIC-8_3.9x4.9mm_P1.27mm" if pins == 8 else "SOIC-16_3.9x9.9mm_P1.27mm"
    return W.footprint("Package_SO:" + name, x, y, ref, value, pads, models=[M3D + "Package_SO.3dshapes/%s.step" % name])


def _part(kind: str, ref: str, value: str, x: float, y: float) -> Tuple[list, Tuple[float, float]]:
    """A station part: (footprint, its size (w, h) in mm)."""
    if kind == "adc":
        return _so(ref, value, x, y, 16), (8.0, 12.0)
    if kind in ("can", "rs485", "i2c", "uart"):
        return _so(ref, value, x, y, 8), (8.0, 7.0)
    if kind == "vout":
        pads = [W.pad(str(n), "smd", "rect", -2.3 + 2.3 * (n - 1), 3.15, 1.0, 2.0, FCU) for n in range(1, 4)] + \
               [W.pad("4", "smd", "rect", 0, -3.15, 3.25, 2.0, FCU)]
        return W.footprint("Package_TO_SOT_SMD:SOT-223-3_TabPin2", x, y, ref, value, pads,
                           models=[M3D + "Package_TO_SOT_SMD.3dshapes/SOT-223-3_TabPin2.step"]), (8.0, 9.0)
    if kind == "ethernet":
        pads = [W.pad(str(n), "thru_hole", "circle", -4.445 + 1.27 * (n - 1), -8.89 + (1.78 if n % 2 == 0 else 0.0),
                      1.5, 1.5, THT, drill=0.9) for n in range(1, 9)] + \
               [W.pad("", "np_thru_hole", "circle", -5.715, 0.0, 3.25, 3.25, THT, drill=3.25),
                W.pad("", "np_thru_hole", "circle", 5.715, 0.0, 3.25, 3.25, THT, drill=3.25)]
        return W.footprint("Connector_RJ:RJ45_Amphenol_RJHSE538X", x, y, ref, value, pads, attr=("through_hole",),
                           models=[M3D + "Connector_RJ.3dshapes/RJ45_Amphenol_RJHSE538X.step"]), (17.0, 22.0)
    if kind == "usb":
        pads = [W.pad(str(n), "thru_hole", "circle", -3.5 + 2.33 * (n - 1), 0.0, 1.5, 1.5, THT, drill=0.92)
                for n in range(1, 5)] + [W.pad("5", "thru_hole", "circle", -6.57, 2.7, 3.0, 3.0, THT, drill=2.3),
                                         W.pad("5", "thru_hole", "circle", 6.57, 2.7, 3.0, 3.0, THT, drill=2.3)]
        return W.footprint("Connector_USB:USB_A_Molex_67643_Horizontal", x, y, ref, value, pads, attr=("through_hole",),
                           models=[M3D + "Connector_USB.3dshapes/USB_A_Molex_67643_Horizontal.step"]), (17.0, 16.0)
    if kind == "pi":
        pads = [W.pad(str(n), "thru_hole", "oval", PITCH * ((n - 1) % 2), PITCH * ((n - 1) // 2), 1.7, 1.7, THT, drill=1.0)
                for n in range(1, 41)]
        return W.footprint("Connector_PinHeader_2.54mm:PinHeader_2x20_P2.54mm_Vertical", x, y, ref, value, pads,
                           attr=("through_hole",),
                           models=[M3D + "Connector_PinHeader_2.54mm.3dshapes/PinHeader_2x20_P2.54mm_Vertical.step"]), (6.0, 52.0)
    if kind == "jack":
        pads = [W.pad("1", "thru_hole", "rect", 0, 0, 3.5, 3.5, THT, drill=1.8),
                W.pad("2", "thru_hole", "oval", -6.0, 0, 3.0, 3.0, THT, drill=1.5)]
        return W.footprint("Connector_BarrelJack:BarrelJack_Horizontal", x, y, ref, value, pads, attr=("through_hole",),
                           models=[M3D + "Connector_BarrelJack.3dshapes/BarrelJack_Horizontal.step"]), (14.0, 12.0)
    raise ValueError(kind)


def mounting_holes(w: float, h: float, inset: float = 5.0) -> List[Tuple[float, float]]:
    """Corner holes, plus the middle of each long side on large plates: (x, y) from the plate's top-left corner."""
    pts = [(inset, inset), (w - inset, inset), (inset, h - inset), (w - inset, h - inset)]
    if w > 300.0:
        pts += [(w / 2.0, inset), (w / 2.0, h - inset)]
    if h > 240.0:
        pts += [(inset, h / 2.0), (w - inset, h / 2.0)]
    return pts


def socket_places(w: float, h: float, sockets: List[List[str]]) -> List[Tuple[float, float]]:
    """Pin 1 of each socket (plate coordinates): along the front edge, side by side when they fit, else a row behind."""
    lens = [PITCH * (len(p) // 2) for p in sockets]
    total = sum(lens) + 12.0 * (len(lens) - 1)
    out = []
    if total <= w - 24.0:
        x = (w - total) / 2.0
        for L in lens:
            out.append((round(x, 2), h - 12.0))
            x += L + 12.0
    else:
        for i, L in enumerate(lens):
            out.append((round((w - L) / 2.0, 2), h - 12.0 - 14.0 * i))
    return out


def station_pcb(station: Dict[str, Any]) -> str:
    """The station board as KiCad text. ``station``: {"name", "title", "plate_mm": [w, h], "resources": {"ain",
    "vout", "buses": {bus: n}, "dio"}, "gnd_every", "max_pins", "lines": [silkscreen text], "rev"}; with "keep"
    (keep_of: an existing station's sockets, pin 1s, holes and plate) it is that station upgraded: every pin, socket
    and hole stays where it was (moved with the plate when it grows), the new signals go on new pins."""
    w, h = (float(v) for v in station["plate_mm"])
    res = station["resources"]
    keep = station.get("keep")
    ge, mp = int(station.get("gnd_every", 6)), int(station.get("max_pins", 2 * MAX_POSITIONS))
    old_holes: List[Tuple[float, float]] = []
    if keep and keep.get("sockets"):                    # an upgrade: the fixtures made for it must still fit
        kw, kh = (float(v) for v in keep.get("plate_mm") or (w, h))
        w, h = max(w, kw), max(h, kh)
        dx, dy = (w - kw) / 2.0, h - kh                 # a bigger plate grows to the sides and to the back
        sockets = upgrade_sockets(keep["sockets"], res, ge, mp)
        places = [(round(px + dx, 2), round(py + dy, 2)) for px, py in keep["places"]]
        for k in range(len(places), len(sockets)):      # new sockets: a row behind the ones there are
            L = PITCH * (len(sockets[k]) // 2)
            places.append((round((w - L) / 2.0, 2), round(min(py for _px, py in places) - 14.0, 2)))
        old_holes = [(hx + dx, hy + dy) for hx, hy in keep.get("holes") or []]
    else:
        sockets = socket_pins(res, ge, mp)
        places = socket_places(w, h, sockets)
    names = sorted({n for s in sockets for n in s if n != "NC"}, key=lambda n: (n != "GND", n))
    net = {n: (i + 1, n) for i, n in enumerate(names)}
    items: List[list] = [W.gr_rect(X0, Y0, X0 + w, Y0 + h, "Edge.Cuts", 0.1)]
    for i, (pins, (px, py)) in enumerate(zip(sockets, places)):
        items.append(_socket("J%d" % (i + 1), X0 + px, Y0 + py, pins, net))
        items.append(W.gr_text("FIXTURE CONNECTOR J%d - pin 1 left" % (i + 1), X0 + px + PITCH * len(pins) / 4.0,
                               Y0 + py - 6.0, "F.SilkS", 1.4))
    holes = mounting_holes(w, h)
    holes += [hp for hp in old_holes if all(abs(hp[0] - q[0]) > 1.0 or abs(hp[1] - q[1]) > 1.0 for q in holes)]
    for k, (hx, hy) in enumerate(holes, 1):
        items.append(W.footprint("MountingHole:MountingHole_3.2mm_M3", X0 + hx, Y0 + hy, "H%d" % k, "M3",
                                 [W.pad("", "np_thru_hole", "circle", 0, 0, 3.2, 3.2, THT, drill=3.2)],
                                 attr=("exclude_from_pos_files", "exclude_from_bom"), hide_ref=True))
    # the station's electronics: a flow of parts over the board, clear of the holes and the socket band
    parts: List[Tuple[str, str, str]] = [("pi", "J100", "RASPBERRY_PI")]
    parts += [("adc", "U%d" % (i + 1), "ADC_4CH") for i in range(int(math.ceil(int(res.get("ain", 0)) / 4.0)))]
    for kind, label in (("can", "CAN_XCVR"), ("rs485", "RS485_XCVR"), ("i2c", "I2C_BUFFER"), ("uart", "UART_LEVEL")):
        parts += [(kind, "U", "%s%d" % (label, i + 1)) for i in range(int((res.get("buses") or {}).get(kind, 0)))]
    parts += [("vout", "Q", "VOUT%d_SWITCH" % (i + 1)) for i in range(int(res.get("vout", 0)))]
    parts += [("ethernet", "J", "ETH%d" % (i + 1)) for i in range(int((res.get("buses") or {}).get("ethernet", 0)))]
    parts += [("usb", "J", "USB%d" % (i + 1)) for i in range(int((res.get("buses") or {}).get("usb", 0)))]
    parts.append(("jack", "J101", "24V_IN"))
    band_top = min(py for _px, py in places) - 10.0
    x, y, row_h = 14.0, 26.0, 0.0
    nref = {"U": 200, "Q": 300, "J": 400}
    for kind, ref, value in parts:
        if ref in nref:
            nref[ref] += 1
            ref = "%s%d" % (ref, nref[ref])
        fp, (pw, ph) = _part(kind, ref, value, 0.0, 0.0)
        if x + pw > w - 12.0:
            x, y, row_h = 14.0, y + row_h + 6.0, 0.0
        if y + ph > band_top:
            break                                       # the rest is in the list on the silkscreen
        cx, cy = x + pw / 2.0, y + ph / 2.0
        if any(abs(cx - hx) < pw / 2.0 + 5.0 and abs(cy - hy) < ph / 2.0 + 5.0 for hx, hy in holes):
            x += pw + 6.0
            continue
        fp, _ = _part(kind, ref, value, X0 + cx, Y0 + cy)
        items.append(fp)
        x += pw + 6.0
        row_h = max(row_h, ph)
    items.append(W.gr_text(station.get("title", station["name"]).upper(), X0 + w / 2.0, Y0 + 8.0, "F.SilkS", 3.0))
    for i, line in enumerate(station.get("lines") or []):
        items.append(W.gr_text(line, X0 + w / 2.0, Y0 + 14.0 + 3.0 * i, "F.SilkS", 1.6))
    return W.board([n for n in names], items, thickness=1.6, title=station.get("title", station["name"]),
                   rev=str(station.get("rev") or "A"),
                   comment="PCB TestKit test station: every fixture plugs into J1", mask_color="Blue")


def write_station(station: Dict[str, Any], out_dir: str) -> str:
    """Write <name>.kicad_pcb and .kicad_pro to out_dir; returns the board's path."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, station["name"] + ".kicad_pcb")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(station_pcb(station))
    pro = os.path.join(out_dir, station["name"] + ".kicad_pro")
    if not os.path.exists(pro):
        with open(pro, "w", encoding="utf-8") as fh:
            json.dump({"board": {"design_settings": {}}, "meta": {"filename": station["name"] + ".kicad_pro", "version": 1}},
                      fh, indent=2)
    return path


def requirement_lines(res: Dict[str, Any], extra: Sequence[str] = ()) -> List[str]:
    """Short silkscreen lines: what the station offers."""
    bits = ["%d AIN" % res.get("ain", 0), "%d SUPPLIES" % res.get("vout", 0)]
    bits += ["%d %s" % (n, b.upper() if b != "ethernet" else "ETH") for b, n in sorted((res.get("buses") or {}).items()) if n]
    if res.get("dio"):
        bits.append("%d DIO" % res["dio"])
    return ["  ".join(bits)] + list(extra)


__all__ = ["station_pcb", "write_station", "socket_pins", "upgrade_sockets", "keep_of", "pin_count", "signal_groups",
           "BUS_PINS", "BUS_ORDER",
           "requirement_lines", "mounting_holes", "socket_places", "MAX_POSITIONS"]
