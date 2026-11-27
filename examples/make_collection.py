#!/usr/bin/env python3
"""Generate a collection of varied KiCad boards to try test environments on (one folder per project).

    python3 examples/make_collection.py OUT_DIR [--count 110] [--seed 7]

Families, mixed in fixed proportions: small sensor nodes (I2C/UART, SWD), CAN nodes (12/24 V), RS-485 I/O boards,
USB dongles, gateways (Ethernet, USB, CAN, RS-485, JTAG or SWD), power boards (many rails, PIC ICSP or none),
large controllers (Ethernet + CAN + USB + UART, JTAG; too big for a small station plate), AVR boards (ISP), plus a
few that no station takes by design: 48 V boards and boards probed from both sides. Every board has a supply
terminal, regulators, an MCU (most), bus connectors and transceivers, decoupling, test pads on the bottom for its
rails and most bus and programming nets, mounting holes and 3D models from KiCad's library. They are not routed:
they exist to be planned, fitted and tested, not ordered.
"""
import argparse
import json
import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from pcbtestkit import kicad_writer as W  # noqa: E402

M3D = "${KICAD8_3DMODEL_DIR}/"
FCU = ["F.Cu", "F.Paste", "F.Mask"]
BCU = ["B.Cu", "B.Mask"]
THT = ["*.Cu", "*.Mask"]

BUS_NETS = {
    "can": ["CANH", "CANL"], "uart": ["UART_TX", "UART_RX"], "i2c": ["I2C_SDA", "I2C_SCL"],
    "rs485": ["RS485_A", "RS485_B"], "usb": ["USB_D+", "USB_D-"], "ethernet": ["ETH_TX_P", "ETH_TX_N", "ETH_RX_P", "ETH_RX_N"],
}
PROG_NETS = {"swd": ["SWDIO", "SWCLK", "NRST"], "jtag": ["TCK", "TMS", "TDI", "TDO", "NRST"],
             "avr_isp": ["MISO", "SCK", "MOSI", "RESET"], "pic_icsp": ["MCLR", "PGD", "PGC"]}
FAMILIES = [
    # name, share, w range, h range, input volts, rails, bus choices, programming choices, mcu, tall-part chance
    ("sensor", 22, (28, 55), (20, 40), [5], ["+3V3"], [["i2c"], ["uart"], [], ["i2c", "uart"]], ["swd"], "qfn32", 0.1),
    ("can_node", 18, (45, 75), (30, 55), [12, 24], ["+5V", "+3V3"], [["can"], ["can", "uart"]], ["swd"], "qfn32", 0.5),
    ("rs485_io", 11, (50, 90), (35, 60), [24], ["+5V", "+3V3"], [["rs485"], ["rs485", "uart"]], ["swd", "avr_isp"], "qfn32", 0.4),
    ("usb_dongle", 13, (22, 40), (15, 26), [5], ["+3V3"], [["usb"]], ["swd"], "qfn32", 0.0),
    ("gateway", 11, (80, 120), (55, 85), [12, 24], ["+5V", "+3V3", "+1V2"],
     [["ethernet", "usb"], ["ethernet", "can", "rs485"], ["usb", "can"], ["ethernet"]], ["swd", "jtag"], "lqfp100", 0.6),
    ("power", 11, (60, 110), (45, 80), [24], ["+12V", "+5V", "+3V3", "+1V8"], [[], ["i2c"]], [None, "pic_icsp"], "qfn32", 0.9),
    ("avr_board", 7, (40, 70), (30, 50), [5, 12], ["+5V"], [["uart"], ["i2c", "uart"]], ["avr_isp"], "qfn32", 0.3),
    ("controller", 7, (140, 200), (95, 140), [24], ["+12V", "+5V", "+3V3", "+1V8", "+1V2"],
     [["can", "ethernet", "usb", "uart"], ["can", "ethernet"]], ["jtag"], "lqfp100", 0.8),
    ("hv_driver", 3, (60, 90), (40, 60), [48], ["+12V", "+5V"], [["can"]], ["swd"], "qfn32", 0.7),
    ("dual_side", 3, (50, 80), (40, 60), [12], ["+5V", "+3V3"], [["uart"]], ["swd"], "qfn32", 0.3),
]


class Board:
    def __init__(self, name: str, w: float, h: float, rnd: random.Random):
        self.name, self.w, self.h, self.rnd = name, round(w, 1), round(h, 1), rnd
        self.x0, self.y0 = 100.0, 100.0
        self.nets = ["GND"]
        self.items = []
        self.boxes = []                 # placed top-side bodies (x0, y0, x1, y1)
        self.tht = []                   # through-hole pads and holes (x, y, r): kept clear on the bottom
        self.refs = {}
        self.cx, self.cy, self.row_h = self.x0 + 3.0, self.y0 + 3.0, 0.0

    def net(self, name):
        if not name:
            return None
        if name not in self.nets:
            self.nets.append(name)
        return (self.nets.index(name) + 1, name)

    def ref(self, p):
        self.refs[p] = self.refs.get(p, 0) + 1
        return "%s%d" % (p, self.refs[p])

    def place(self, bw, bh):
        """Next free spot for a bw x bh body, left to right in rows; None when the board is full."""
        if self.cx + bw > self.x0 + self.w - 3.0:
            self.cx, self.cy, self.row_h = self.x0 + 3.0, self.cy + self.row_h + 1.5, 0.0
        if self.cy + bh > self.y0 + self.h - 3.0 or bw > self.w - 6.0:
            return None
        x, y = self.cx + bw / 2.0, self.cy + bh / 2.0
        if any(abs(x - hx) < bw / 2.0 + 4.0 and abs(y - hy) < bh / 2.0 + 4.0 for hx, hy, r in self.tht if r > 1.5):
            self.cx += bw + 1.5                     # a mounting hole: skip past it
            return self.place(bw, bh)
        self.cx += bw + 1.5
        self.row_h = max(self.row_h, bh)
        self.boxes.append((x - bw / 2.0, y - bh / 2.0, x + bw / 2.0, y + bh / 2.0))
        return x, y

    def add(self, lib, model, ref_prefix, value, bw, bh, pads, nets, tht=False, height=None):
        at = self.place(bw, bh)
        if at is None:
            return False
        x, y = at
        items = [W.fp_rect(-bw / 2.0, -bh / 2.0, bw / 2.0, bh / 2.0, "F.CrtYd", 0.05),
                 W.fp_rect(-bw / 2.0 + 0.25, -bh / 2.0 + 0.25, bw / 2.0 - 0.25, bh / 2.0 - 0.25, "F.Fab", 0.1)]
        for (num, px, py, pw, ph, kind, drill) in pads:
            n = nets.get(num)
            if kind == "smd":
                items.append(W.pad(num, "smd", "roundrect", px, py, pw, ph, FCU, net=self.net(n), rratio=0.25))
            elif kind == "npth":
                items.append(W.pad("", "np_thru_hole", "circle", px, py, pw, pw, THT, drill=drill))
                self.tht.append((x + px, y + py, pw / 2.0))
            else:
                items.append(W.pad(num, "thru_hole", "rect" if num == "1" else "circle", px, py, pw, ph, THT,
                                   drill=drill, net=self.net(n)))
                self.tht.append((x + px, y + py, pw / 2.0))
        props = {"Height": "%g" % height} if height else None
        self.items.append(W.footprint(lib, x, y, self.ref(ref_prefix), value, items, attr=("through_hole",) if tht else ("smd",),
                                      extra_props=props, models=[M3D + model] if model else []))
        return True

    def hole(self, x, y):
        self.items.append(W.footprint("MountingHole:MountingHole_3.2mm_M3", x, y, self.ref("H"), "M3",
                                      [W.pad("", "np_thru_hole", "circle", 0, 0, 3.2, 3.2, THT, drill=3.2),
                                       W.fp_circle(0, 0, 3.45, "F.CrtYd", 0.05)],
                                      attr=("exclude_from_pos_files", "exclude_from_bom"), hide_ref=True))
        self.tht.append((x, y, 3.2))

    def testpads(self, nets):
        """Bottom test pads for ``nets``, on a 3.5 mm grid clear of every through-hole pad and hole."""
        spots = []
        y = self.y0 + 3.5
        while y < self.y0 + self.h - 3.0:
            x = self.x0 + 3.5
            while x < self.x0 + self.w - 3.0:
                if all(math.hypot(x - tx, y - ty) > r + 2.2 for tx, ty, r in self.tht):
                    spots.append((x, y))
                x += 3.5
            y += 3.5
        self.rnd.shuffle(spots)
        for n, (x, y) in zip(nets, spots):
            self.items.append(W.footprint("TestPoint:TestPoint_Pad_D1.5mm", x, y, self.ref("TP"), n.lstrip("/"),
                                          [W.pad("1", "smd", "circle", 0, 0, 1.5, 1.5, BCU, net=self.net(n)),
                                           W.fp_circle(0, 0, 1.0, "B.CrtYd", 0.05)], layer="B.Cu"))
        return len(spots) >= len(nets)

    def text(self):
        segs = [W.gr_rect(self.x0, self.y0, self.x0 + self.w, self.y0 + self.h, "Edge.Cuts", 0.1)]
        return W.board(self.nets, segs + self.items, thickness=1.6, title=self.name, rev="A",
                       comment="PCB TestKit collection board")


def qfn(n_side, pitch, body, ep):
    pads, num = [], 1
    half = (n_side - 1) * pitch / 2.0
    off = body / 2.0
    for side in range(4):
        for i in range(n_side):
            t = -half + i * pitch
            x, y, w, h = [(-off, t, 0.8, 0.25), (t, off, 0.25, 0.8), (off, -t, 0.8, 0.25), (-t, -off, 0.25, 0.8)][side]
            pads.append((str(num), x, y, w, h, "smd", 0))
            num += 1
    pads.append((str(num), 0.0, 0.0, ep, ep, "smd", 0))
    return pads, num


def soic8():
    return [(str(n), -2.475 if n <= 4 else 2.475, (-1.905 + 1.27 * (n - 1)) if n <= 4 else (1.905 - 1.27 * (n - 5)), 1.95, 0.6,
             "smd", 0) for n in range(1, 9)]


def header(n, rows=1, pitch=2.54):
    pads = []
    for k in range(n * rows):
        r, c = (k % rows, k // rows) if rows == 2 else (0, k)
        pads.append((str(k + 1), (c - (n - 1) / 2.0) * pitch, (r - (rows - 1) / 2.0) * pitch, 1.7, 1.7, "tht", 1.0))
    return pads


def make_board(name, fam, rnd):
    (_fam, _share, wr, hr, vins, rails, bus_choices, progs, mcu, tall) = fam
    b = Board(name, rnd.uniform(*wr), rnd.uniform(*hr), rnd)
    vin = rnd.choice(vins)
    vnet = "VBUS" if (vin == 5 and _fam == "usb_dongle") else "VIN_%dV" % vin
    buses = list(rnd.choice(bus_choices))
    prog = rnd.choice(progs)
    if b.w > 40 and b.h > 30:
        for hx, hy in ((3.5, 3.5), (b.w - 3.5, 3.5), (3.5, b.h - 3.5), (b.w - 3.5, b.h - 3.5)):
            b.hole(b.x0 + hx, b.y0 + hy)
    extra_rails = []
    if _fam == "power":
        extra_rails = ["+%dV%s" % (v, "") for v in rnd.sample([15, 9, 6], rnd.randint(0, 2))]
    all_rails = rails + extra_rails
    # supply in
    if _fam == "usb_dongle":
        pass
    else:
        b.add("TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2_1x02_P5.00mm_Horizontal",
              "TerminalBlock_Phoenix.3dshapes/TerminalBlock_Phoenix_MKDS-1,5-2_1x02_P5.00mm_Horizontal.step",
              "J", "PWR_IN", 10.5, 9.0, [("1", -2.5, 0, 2.6, 2.6, "tht", 1.3), ("2", 2.5, 0, 2.6, 2.6, "tht", 1.3)],
              {"1": vnet, "2": "GND"}, tht=True, height=11.0)
    # regulators: VIN -> first rail -> next ...
    src = vnet
    for r in all_rails:
        b.add("Package_TO_SOT_SMD:SOT-223-3_TabPin2", "Package_TO_SOT_SMD.3dshapes/SOT-223-3_TabPin2.step", "U", "REG" + r,
              7.0, 7.5, [("1", -2.3, 3.15, 1.0, 2.0, "smd", 0), ("2", 0, 3.15, 1.0, 2.0, "smd", 0), ("3", 2.3, 3.15, 1.0, 2.0, "smd", 0),
                         ("4", 0, -3.15, 3.25, 2.0, "smd", 0)], {"1": "GND", "2": r, "3": src, "4": r})
        for _ in range(2):
            b.add("Capacitor_SMD:C_0603_1608Metric", "Capacitor_SMD.3dshapes/C_0603_1608Metric.step", "C", "10uF", 2.4, 1.4,
                  [("1", -0.775, 0, 0.9, 0.95, "smd", 0), ("2", 0.775, 0, 0.9, 0.95, "smd", 0)], {"1": r, "2": "GND"})
        src = r if rnd.random() < 0.6 else src
    if rnd.random() < tall:
        b.add("Capacitor_THT:CP_Radial_D10.0mm_P5.00mm", "Capacitor_THT.3dshapes/CP_Radial_D10.0mm_P5.00mm.step", "C", "470uF",
              11.0, 11.0, [("1", -2.5, 0, 1.6, 1.6, "tht", 0.8), ("2", 2.5, 0, 1.6, 1.6, "tht", 0.8)], {"1": vnet, "2": "GND"},
              tht=True, height=16.0)
    logic = all_rails[-1] if all_rails[-1] in ("+3V3", "+5V") else ("+3V3" if "+3V3" in all_rails else all_rails[0])
    bus_nets = [n for bus in buses for n in BUS_NETS[bus]]
    prog_nets = PROG_NETS.get(prog, []) if prog else []
    signals = ["/LED%d" % i for i in range(1, rnd.randint(2, 4))]
    if _fam in ("power", "controller"):
        signals += ["/SENSE%d" % i for i in range(1, rnd.randint(3, 9))]
    # the MCU
    if mcu == "qfn32":
        pads, ep = qfn(8, 0.5, 4.6, 3.45)
        lib, model, size = "Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm", \
            "Package_DFN_QFN.3dshapes/QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm.step", (6.2, 6.2)
    else:
        pads, ep = qfn(25, 0.5, 15.0, 0.1)
        pads = pads[:-1]
        lib, model, size = "Package_QFP:LQFP-100_14x14mm_P0.5mm", "Package_QFP.3dshapes/LQFP-100_14x14mm_P0.5mm.step", (16.5, 16.5)
    pin_nets = [logic, "GND"] + bus_nets[:] + prog_nets + signals + [logic, "GND"]
    nets = {str(i + 1): n for i, n in enumerate(pin_nets) if i + 1 < len(pads)}
    nets[str(len(pads))] = "GND" if mcu == "qfn32" else nets.get(str(len(pads)))
    b.add(lib, model, "U", "MCU", size[0], size[1], pads, nets)
    # transceivers and bus connectors
    for bus in buses:
        if bus in ("can", "rs485", "i2c"):
            a, c = BUS_NETS[bus]
            b.add("Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", "Package_SO.3dshapes/SOIC-8_3.9x4.9mm_P1.27mm.step", "U",
                  {"can": "TCAN1042", "rs485": "THVD1450", "i2c": "TMP117"}[bus], 6.0, 5.4, soic8(),
                  {"3": logic, "2": "GND", "6": c, "7": a, "5": logic})
        if bus in ("can", "rs485"):
            a, c = BUS_NETS[bus]
            b.add("TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-3_1x03_P5.00mm_Horizontal",
                  "TerminalBlock_Phoenix.3dshapes/TerminalBlock_Phoenix_MKDS-1,5-3_1x03_P5.00mm_Horizontal.step", "J",
                  bus.upper(), 15.5, 9.0, [("1", -5, 0, 2.6, 2.6, "tht", 1.3), ("2", 0, 0, 2.6, 2.6, "tht", 1.3),
                                           ("3", 5, 0, 2.6, 2.6, "tht", 1.3)], {"1": a, "2": c, "3": "GND"}, tht=True, height=11.0)
        if bus == "uart":
            b.add("Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
                  "Connector_PinHeader_2.54mm.3dshapes/PinHeader_1x04_P2.54mm_Vertical.step", "J", "UART", 10.6, 2.6,
                  header(4), {"1": "GND", "2": "UART_TX", "3": "UART_RX", "4": logic}, tht=True)
        if bus == "usb":
            b.add("Connector_USB:USB_Micro-B_Molex_47346-0001", "Connector_USB.3dshapes/USB_Micro-B_Molex_47346-0001.step",
                  "J", "USB", 8.0, 6.0, [("1", -1.3, -2.0, 0.45, 1.35, "smd", 0), ("2", -0.65, -2.0, 0.45, 1.35, "smd", 0),
                                         ("3", 0.0, -2.0, 0.45, 1.35, "smd", 0), ("4", 0.65, -2.0, 0.45, 1.35, "smd", 0),
                                         ("5", 1.3, -2.0, 0.45, 1.35, "smd", 0)],
                  {"1": "VBUS" if _fam == "usb_dongle" else None, "2": "USB_D-", "3": "USB_D+", "5": "GND"})
        if bus == "ethernet":
            rj = [(str(n), -4.445 + 1.27 * (n - 1), -2.0 + (1.78 if n % 2 == 0 else 0.0), 1.5, 1.5, "tht", 0.9) for n in range(1, 9)]
            rj += [("", -5.715, 4.0, 3.25, 3.25, "npth", 3.25), ("", 5.715, 4.0, 3.25, 3.25, "npth", 3.25)]
            b.add("Connector_RJ:RJ45_Amphenol_RJHSE538X", "Connector_RJ.3dshapes/RJ45_Amphenol_RJHSE538X.step", "J", "ETH",
                  17.0, 15.0, rj, {"1": "ETH_TX_P", "2": "ETH_TX_N", "3": "ETH_RX_P", "6": "ETH_RX_N", "4": "GND"},
                  tht=True, height=13.5)
    if prog:
        n = len(PROG_NETS[prog]) + 2
        if prog == "avr_isp":
            b.add("Connector_PinHeader_2.54mm:PinHeader_2x03_P2.54mm_Vertical",
                  "Connector_PinHeader_2.54mm.3dshapes/PinHeader_2x03_P2.54mm_Vertical.step", "J", "ISP", 8.0, 5.6, header(3, 2),
                  {"1": "MISO", "2": logic, "3": "SCK", "4": "MOSI", "5": "RESET", "6": "GND"}, tht=True)
        else:
            nn = min(n, 6)
            b.add("Connector_PinHeader_2.54mm:PinHeader_1x%02d_P2.54mm_Vertical" % nn,
                  "Connector_PinHeader_2.54mm.3dshapes/PinHeader_1x%02d_P2.54mm_Vertical.step" % nn, "J", prog.upper(),
                  2.54 * nn + 0.5, 2.6, header(nn), dict({"1": logic, "2": "GND"},
                                                         **{str(i + 3): x for i, x in enumerate(PROG_NETS[prog][:nn - 2])}), tht=True)
    for s in signals[:3]:
        b.add("LED_SMD:LED_0603_1608Metric", "LED_SMD.3dshapes/LED_0603_1608Metric.step", "D", "LED", 2.4, 1.4,
              [("1", -0.775, 0, 0.9, 0.95, "smd", 0), ("2", 0.775, 0, 0.9, 0.95, "smd", 0)], {"1": "GND", "2": s})
    # test pads underneath: supply, rails, most bus and programming nets, the sense lines
    tp = [vnet, "GND"] + all_rails + [n for n in bus_nets if rnd.random() < 0.85] + \
         [n for n in prog_nets if rnd.random() < 0.5] + [s for s in signals if s.startswith("/SENSE")]
    b.testpads(tp)
    cfg = {}
    if _fam == "dual_side":
        cfg = {"testplan": {"probe_side": "both"}}
    return b, cfg


def main(out, count=110, seed=7):
    rnd = random.Random(seed)
    total = sum(f[1] for f in FAMILIES)
    plan = []
    for fam in FAMILIES:
        plan += [fam] * max(1, int(round(count * fam[1] / float(total))))
    plan = plan[:count] if len(plan) >= count else plan + [FAMILIES[0]] * (count - len(plan))
    rnd.shuffle(plan)
    os.makedirs(out, exist_ok=True)
    made = []
    seen = {}
    for fam in plan:
        seen[fam[0]] = seen.get(fam[0], 0) + 1
        name = "%s_%02d" % (fam[0], seen[fam[0]])
        b, cfg = make_board(name, fam, random.Random(rnd.random()))
        d = os.path.join(out, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name + ".kicad_pcb"), "w", encoding="utf-8") as fh:
            fh.write(b.text())
        with open(os.path.join(d, name + ".kicad_pro"), "w", encoding="utf-8") as fh:
            json.dump({"board": {"design_settings": {}}, "meta": {"filename": name + ".kicad_pro", "version": 1}}, fh, indent=2)
        if cfg:
            with open(os.path.join(d, "pcbtestkit.json"), "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2)
        made.append(name)
    print("%d boards written to %s" % (len(made), out))
    return made


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out")
    ap.add_argument("--count", type=int, default=110)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    main(a.out, a.count, a.seed)
