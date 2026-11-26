#!/usr/bin/env python3
"""Generate the example test station 'test_station' (KiCad 8 board) used by the station examples and tests.

A test station is the tester every fixture plugs into. Its interface connector J1 (a 2 x 40 pin socket) carries,
named by net:
    32 analog inputs AIN1..AIN32, 4 switched supplies VOUT1..VOUT4, CAN1 and CAN2, UART1 and UART2,
    I2C1..I2C4, 8 digital lines DIO1..DIO8 and GND every few pins.
Every fixture made for this station is a copy of its 260 x 190 mm outline with the four M3 holes in the same
place and the mating 2 x 40 header where J1 is, so a fixture for any board type plugs into the same station:
    pcbtestkit station run examples/station/test_station.kicad_pcb board1.kicad_pro board2.kicad_pro
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from pcbtestkit import kicad_writer as W  # noqa: E402

OUT = os.path.join(HERE, "station")
NAME = "test_station"
X0, Y0, WIDTH, HEIGHT = 50.0, 50.0, 260.0, 190.0
M3D = "${KICAD8_3DMODEL_DIR}/"
THT = ["*.Cu", "*.Mask"]
FCU = ["F.Cu", "F.Paste", "F.Mask"]


def pin_map():
    """J1 pin -> net, 2 x 40 zigzag numbering (1 and 2 side by side)."""
    m = {1: "GND", 2: "GND", 35: "GND", 36: "GND", 41: "GND", 42: "GND", 51: "GND", 52: "GND", 61: "GND", 62: "GND",
         79: "GND", 80: "GND"}
    for k in range(32):
        m[3 + k] = "AIN%d" % (k + 1)
    for k in range(4):
        m[37 + k] = "VOUT%d" % (k + 1)
    m.update({43: "CAN1_H", 44: "CAN1_L", 45: "CAN2_H", 46: "CAN2_L",
              47: "UART1_TX", 48: "UART1_RX", 49: "UART2_TX", 50: "UART2_RX"})
    for k in range(4):
        m[53 + 2 * k], m[54 + 2 * k] = "I2C%d_SDA" % (k + 1), "I2C%d_SCL" % (k + 1)
    for k in range(8):
        m[63 + k] = "DIO%d" % (k + 1)
    return m                                                   # pins 71..78 are spare (no net)


def build():
    pins = pin_map()
    nets = ["GND"] + sorted({n for n in pins.values() if n != "GND"},
                            key=lambda n: (n.rstrip("0123456789_HLSDACXR"), n))
    net = {n: (i + 1, n) for i, n in enumerate(nets)}
    items = []
    x1, y1 = X0 + WIDTH, Y0 + HEIGHT
    items.append(W.gr_rect(X0, Y0, x1, y1, "Edge.Cuts", 0.1))
    # interface connector: 2 x 40 pin socket along the front edge, pins running in +X (angle 90)
    jx, jy = X0 + 79.2, y1 - 12.0
    pads = []
    for n in range(1, 81):
        i, j = (n - 1) % 2, (n - 1) // 2
        lx, ly = 2.54 * i, 2.54 * j
        pads.append(W.pad(str(n), "thru_hole", "rect" if n == 1 else "oval", lx, ly, 1.7, 1.7, THT, drill=1.0,
                          net=net[pins[n]] if n in pins else None, angle=90, pinfunction=pins.get(n, ""), pintype="passive"))
    pads += [W.fp_rect(-1.27, -1.27, 3.81, 2.54 * 39 + 1.27, "F.Fab", 0.1),
             W.fp_rect(-1.8, -1.8, 4.34, 2.54 * 39 + 1.8, "F.CrtYd", 0.05)]
    items.append(W.footprint("Connector_PinSocket_2.54mm:PinSocket_2x40_P2.54mm_Vertical", jx, jy, "J1", "FIXTURE",
                             pads, angle=90, attr=("through_hole",), ref_pos=(-3.5, 0),
                             models=[M3D + "Connector_PinSocket_2.54mm.3dshapes/PinSocket_2x40_P2.54mm_Vertical.step"]))
    # mounting holes where every fixture is screwed down
    for k, (hx, hy) in enumerate(((X0 + 5, Y0 + 5), (x1 - 5, Y0 + 5), (X0 + 5, y1 - 5), (x1 - 5, y1 - 5)), 1):
        items.append(W.footprint("MountingHole:MountingHole_3.2mm_M3", hx, hy, "H%d" % k, "M3",
                                 [W.pad("", "np_thru_hole", "circle", 0, 0, 3.2, 3.2, THT, drill=3.2)],
                                 attr=("exclude_from_pos_files", "exclude_from_bom"), hide_ref=True))
    # the station's own electronics (for a realistic render): Raspberry Pi header, ADCs, CAN transceivers, supply
    items.append(W.footprint("Connector_PinHeader_2.54mm:PinHeader_2x20_P2.54mm_Vertical", X0 + 20.0, Y0 + 20.0, "J2",
                             "RASPBERRY_PI", [W.pad(str(n), "thru_hole", "oval", 2.54 * ((n - 1) % 2), 2.54 * ((n - 1) // 2),
                                                    1.7, 1.7, THT, drill=1.0) for n in range(1, 41)],
                             attr=("through_hole",),
                             models=[M3D + "Connector_PinHeader_2.54mm.3dshapes/PinHeader_2x20_P2.54mm_Vertical.step"]))
    for k in range(8):
        cx, cy = X0 + 60.0 + 22.0 * (k % 4), Y0 + 30.0 + 26.0 * (k // 4)
        items.append(W.footprint("Package_SO:SOIC-16_3.9x9.9mm_P1.27mm", cx, cy, "U%d" % (k + 1), "ADC%d" % (k + 1),
                                 [W.pad(str(n), "smd", "roundrect", (-2.475 if n <= 8 else 2.475),
                                        (-4.445 + 1.27 * ((n - 1) % 8)) if n <= 8 else (4.445 - 1.27 * ((n - 9) % 8)),
                                        1.95, 0.6, FCU, rratio=0.25) for n in range(1, 17)],
                                 models=[M3D + "Package_SO.3dshapes/SOIC-16_3.9x9.9mm_P1.27mm.step"]))
    for k in range(2):
        items.append(W.footprint("Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", X0 + 175.0 + 16.0 * k, Y0 + 30.0, "U%d" % (9 + k),
                                 "CAN%d" % (k + 1),
                                 [W.pad(str(n), "smd", "roundrect", (-2.475 if n <= 4 else 2.475),
                                        (-1.905 + 1.27 * ((n - 1) % 4)) if n <= 4 else (1.905 - 1.27 * ((n - 5) % 4)),
                                        1.95, 0.6, FCU, rratio=0.25) for n in range(1, 9)],
                                 models=[M3D + "Package_SO.3dshapes/SOIC-8_3.9x4.9mm_P1.27mm.step"]))
    items.append(W.footprint("Connector_BarrelJack:BarrelJack_Horizontal", x1 - 25.0, Y0 + 70.0, "J3", "24V_IN",
                             [W.pad("1", "thru_hole", "rect", 0, 0, 3.5, 3.5, THT, drill=1.8),
                              W.pad("2", "thru_hole", "oval", -6.0, 0, 3.0, 3.0, THT, drill=1.5)],
                             attr=("through_hole",), models=[M3D + "Connector_BarrelJack.3dshapes/BarrelJack_Horizontal.step"]))
    items.append(W.gr_text("PCB TESTKIT TEST STATION", X0 + WIDTH / 2, Y0 + 8.0, "F.SilkS", 3.0))
    items.append(W.gr_text("32 AIN  4 SUPPLIES  2 CAN  2 UART  4 I2C  8 DIO", X0 + WIDTH / 2, Y0 + 14.0, "F.SilkS", 1.6))
    items.append(W.gr_text("FIXTURE CONNECTOR J1 - pin 1 left", jx + 50.8, jy - 6.0, "F.SilkS", 1.4))
    return W.board(nets, items, thickness=1.6, title="PCB TestKit test station", rev="A",
                   comment="Example station: every fixture plugs into J1", mask_color="Blue")


def main(out=OUT):
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, NAME + ".kicad_pcb"), "w", encoding="utf-8") as fh:
        fh.write(build())
    with open(os.path.join(out, NAME + ".kicad_pro"), "w", encoding="utf-8") as fh:
        json.dump({"board": {"design_settings": {}}, "meta": {"filename": NAME + ".kicad_pro", "version": 1}}, fh, indent=2)
    print("station written to", out)
    return os.path.join(out, NAME + ".kicad_pcb")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else OUT)
