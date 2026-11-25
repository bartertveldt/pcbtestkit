#!/usr/bin/env python3
"""Generate the 'sample_sensor' KiCad 8 project used by the tests and docs.

The board deliberately contains known weak points (thin tracks, MLCC at the
edge, electrolytic next to a TO-220 regulator, SMD-only USB connector, ...)
so every analysis check has something to find.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from pcbtestkit import kicad_writer as W  # noqa: E402
from pcbtestkit.sexpr import Sym, dumps  # noqa: E402

OUT = os.path.join(HERE, "sample_sensor")
NAME = "sample_sensor"
NETS = ["GND", "VIN_12V", "+5V", "+3V3", "VBUS", "/D-", "/D+", "/SDA", "/SCL", "/LED", "Net-(D2-A)", "/NRST",
        "unconnected-(J1-ID-Pad4)", "unconnected-(U3-PA0-Pad5)"]
NET = {n: (i + 1, n) for i, n in enumerate(NETS)}

M3D = "${KICAD8_3DMODEL_DIR}/"
FCU = ["F.Cu", "F.Paste", "F.Mask"]
BCU = ["B.Cu", "B.Paste", "B.Mask"]
THT = ["*.Cu", "*.Mask"]


def courtyard(x0, y0, x1, y1, side="F"):
    return W.fp_rect(x0, y0, x1, y1, "%s.CrtYd" % side, 0.05)


def fab(x0, y0, x1, y1, side="F"):
    return W.fp_rect(x0, y0, x1, y1, "%s.Fab" % side, 0.1)


def chip(lib, size, ref, value, x, y, n1, n2, angle=0, side="F", props=None, pintypes=("passive", "passive")):
    dims = {"0402": (0.48, 0.56, 0.62, 0.93, 0.47, 0.5, 0.25), "0603": (0.825, 0.8, 0.95, 1.48, 0.73, 0.8, 0.4),
            "0805": (0.9125, 1.025, 1.4, 1.68, 0.95, 1.0, 0.625)}[size]
    px, pw, ph, cx, cy, fx, fy = dims
    sgn = -1 if side == "B" else 1
    lay = FCU if side == "F" else BCU
    items = [courtyard(-cx, -cy * sgn, cx, cy * sgn, side), fab(-fx, -fy, fx, fy, side),
             W.pad("1", "smd", "roundrect", -px, 0, pw, ph, lay, net=NET[n1], angle=angle, rratio=0.25, pintype=pintypes[0]),
             W.pad("2", "smd", "roundrect", px, 0, pw, ph, lay, net=NET[n2], angle=angle, rratio=0.25, pintype=pintypes[1])]
    metric = {"0402": "1005", "0603": "1608", "0805": "2012"}[size]
    name = "%s_%s_%sMetric" % (lib[0], size, metric)
    libname = {"R": "Resistor_SMD", "C": "Capacitor_SMD", "L": "LED_SMD"}[lib[0]]
    if lib == "LED":
        name, libname = "LED_%s_%sMetric" % (size, metric), "LED_SMD"
    return W.footprint("%s:%s" % (libname, name), x, y, ref, value, items, layer="%s.Cu" % side, angle=angle,
                       extra_props=props, models=[M3D + "%s.3dshapes/%s.wrl" % (libname, name)])


def build_board(variant=None):
    items = []
    # ---- outline 60 x 45 mm with 3 mm corner radius --------------------------------
    x0, y0, x1, y1, r = 100.0, 80.0, 160.0, 125.0, 3.0
    items += [W.gr_line(x0 + r, y0, x1 - r, y0), W.gr_line(x1, y0 + r, x1, y1 - r),
              W.gr_line(x1 - r, y1, x0 + r, y1), W.gr_line(x0, y1 - r, x0, y0 + r)]
    k = r * (1 - 0.7071068)
    items += [W.gr_arc((x1 - r, y0), (x1 - k, y0 + k), (x1, y0 + r)), W.gr_arc((x1, y1 - r), (x1 - k, y1 - k), (x1 - r, y1)),
              W.gr_arc((x0 + r, y1), (x0 + k, y1 - k), (x0, y1 - r)), W.gr_arc((x0, y0 + r), (x0 + k, y0 + k), (x0 + r, y0))]
    items.append(W.gr_text("SENSOR NODE", 130, 123, "F.SilkS", 1.2))

    # ---- mounting holes -------------------------------------------------------------
    for i, (hx, hy) in enumerate([(104, 84), (156, 84), (104, 121), (156, 121)], 1):
        items.append(W.footprint("MountingHole:MountingHole_3.2mm_M3", hx, hy, "H%d" % i, "MountingHole",
                                 [W.fp_circle(0, 0, 3.45, "F.CrtYd", 0.05), W.fp_circle(0, 0, 3.2, "Cmts.User", 0.15),
                                  W.pad("", "np_thru_hole", "circle", 0, 0, 3.2, 3.2, ["*.Cu", "*.Mask"], drill=3.2)],
                                 attr=("exclude_from_pos_files", "exclude_from_bom"), ref_pos=(0, -4.2), value_pos=(0, 4.2)))

    # ---- power input: terminal block, 7805 (TO-220), caps --------------------------------
    items.append(W.footprint("TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2_1x02_P5.00mm_Horizontal", 110, 89,
                             "J3", "12V IN", [courtyard(-3.0, -5.5, 8.0, 4.6), fab(-2.5, -5.1, 7.5, 4.1),
                                              W.pad("1", "thru_hole", "rect", 0, 0, 2.6, 2.6, THT, drill=1.3, net=NET["VIN_12V"], pintype="passive"),
                                              W.pad("2", "thru_hole", "circle", 5, 0, 2.6, 2.6, THT, drill=1.3, net=NET["GND"], pintype="passive")],
                             attr=("through_hole",), extra_props={"MPN": "1729128"},
                             models=[M3D + "TerminalBlock_Phoenix.3dshapes/TerminalBlock_Phoenix_MKDS-1,5-2_1x02_P5.00mm_Horizontal.wrl"]))
    items.append(W.footprint("Package_TO_SOT_THT:TO-220-3_Vertical", 122, 88, "U1", "LM7805_TO220",
                             [courtyard(-2.71, -3.27, 7.79, 1.52), fab(-2.46, -3.15, 7.54, 1.25),
                              W.pad("1", "thru_hole", "rect", 0, 0, 1.905, 2.0, THT, drill=1.1, net=NET["VIN_12V"], pinfunction="IN", pintype="power_in"),
                              W.pad("2", "thru_hole", "oval", 2.54, 0, 1.905, 2.0, THT, drill=1.1, net=NET["GND"], pinfunction="GND", pintype="power_in"),
                              W.pad("3", "thru_hole", "oval", 5.08, 0, 1.905, 2.0, THT, drill=1.1, net=NET["+5V"], pinfunction="OUT", pintype="power_out")],
                             attr=("through_hole",), descr="TO-220-3, Vertical", extra_props={"MPN": "LM7805CT"},
                             models=[M3D + "Package_TO_SOT_THT.3dshapes/TO-220-3_Vertical.wrl"]))
    items.append(W.footprint("Capacitor_THT:CP_Radial_D8.0mm_P3.50mm", 132, 89, "C1", "100uF 25V",
                             [W.fp_circle(1.75, 0, 4.25, "F.CrtYd", 0.05), W.fp_circle(1.75, 0, 4.0, "F.Fab", 0.1),
                              W.pad("1", "thru_hole", "rect", 0, 0, 1.6, 1.6, THT, drill=0.8, net=NET["VIN_12V"], pintype="passive"),
                              W.pad("2", "thru_hole", "circle", 3.5, 0, 1.6, 1.6, THT, drill=0.8, net=NET["GND"], pintype="passive")],
                             attr=("through_hole",), extra_props={"MPN": "EEU-FR1E101"},
                             models=[M3D + "Capacitor_THT.3dshapes/CP_Radial_D8.0mm_P3.50mm.wrl"]))
    items.append(W.footprint("Capacitor_THT:CP_Radial_D5.0mm_P2.00mm", 142, 89, "C2", "47uF 6.3V",
                             [W.fp_circle(1.0, 0, 2.75, "F.CrtYd", 0.05), W.fp_circle(1.0, 0, 2.5, "F.Fab", 0.1),
                              W.pad("1", "thru_hole", "rect", 0, 0, 1.6, 1.6, THT, drill=0.8, net=NET["+5V"], pintype="passive"),
                              W.pad("2", "thru_hole", "circle", 2.0, 0, 1.6, 1.6, THT, drill=0.8, net=NET["GND"], pintype="passive")],
                             attr=("through_hole",), models=[M3D + "Capacitor_THT.3dshapes/CP_Radial_D5.0mm_P2.00mm.wrl"]))
    items.append(chip("C", "0805", "C5", "1uF 16V", 112, 97, "VIN_12V", "GND", props={"MPN": "GRM21BR71C105KA01L"}))
    items.append(chip("R", "0402", "R4", "1k", 116, 97, "VIN_12V", "GND"))

    # ---- 3V3 LDO (SOT-223) -----------------------------------------------------------------
    sot = [courtyard(-4.4, -3.6, 4.4, 3.6), fab(-1.85, -3.35, 1.85, 3.35),
           W.pad("1", "smd", "rect", -3.15, -2.3, 2.0, 1.5, FCU, net=NET["GND"], pinfunction="GND", pintype="power_in"),
           W.pad("2", "smd", "rect", -3.15, 0, 2.0, 1.5, FCU, net=NET["+3V3"], pinfunction="VO", pintype="power_out"),
           W.pad("3", "smd", "rect", -3.15, 2.3, 2.0, 1.5, FCU, net=NET["+5V"], pinfunction="VI", pintype="power_in"),
           W.pad("4", "smd", "rect", 3.15, 0, 2.0, 3.8, FCU, net=NET["+3V3"], pinfunction="VO", pintype="power_out")]
    items.append(W.footprint("Package_TO_SOT_SMD:SOT-223-3_TabPin2", 151, 96, "U2", "AMS1117-3.3", sot,
                             extra_props={"MPN": "AMS1117-3.3"}, models=[M3D + "Package_TO_SOT_SMD.3dshapes/SOT-223.wrl"]))
    items.append(chip("C", "0402", "C8", "100nF", 151, 85.5, "+3V3", "GND"))

    # ---- USB micro-B (SMD only) + ESD + OR-ing diode ---------------------------------------------
    usb = [courtyard(-4.25, -3.8, 4.25, 2.7)]
    for i, (px, net) in enumerate([(-1.3, "VBUS"), (-0.65, "/D-"), (0, "/D+"), (0.65, "unconnected-(J1-ID-Pad4)"), (1.3, "GND")], 1):
        usb.append(W.pad(str(i), "smd", "rect", px, -2.7, 0.4, 1.35, FCU, net=NET[net], angle=270, pintype="passive"))
    for sx, sy, sw, sh in ((-3.1, -2.55, 2.1, 1.6), (3.1, -2.55, 2.1, 1.6), (-1.2, 0, 1.9, 1.9), (1.2, 0, 1.9, 1.9)):
        usb.append(W.pad("6", "smd", "rect", sx, sy, sw, sh, FCU, net=NET["GND"], angle=270, pintype="passive"))
    items.append(W.footprint("Connector_USB:USB_Micro-B_Molex-105017-0001", 103.2, 108, "J1", "USB_B_Micro", usb,
                             angle=270, extra_props={"MPN": "105017-0001"},
                             models=[M3D + "Connector_USB.3dshapes/USB_Micro-B_Molex-105017-0001.wrl"]))

    def sod(lib, name, ref, value, x, y, n1, n2, angle, w=0.9, h=1.2, px=1.65, props=None):
        its = [courtyard(-2.35, -1.15, 2.35, 1.15), fab(-1.4, -0.9, 1.4, 0.9),
               W.pad("1", "smd", "roundrect", -px, 0, w, h, FCU, net=NET[n1], angle=angle, rratio=0.25, pinfunction="K", pintype="passive"),
               W.pad("2", "smd", "roundrect", px, 0, w, h, FCU, net=NET[n2], angle=angle, rratio=0.25, pinfunction="A", pintype="passive")]
        return W.footprint("%s:%s" % (lib, name), x, y, ref, value, its, angle=angle, extra_props=props,
                           models=[M3D + "%s.3dshapes/%s.wrl" % (lib, name)])

    items.append(sod("Diode_SMD", "D_SOD-123", "D1", "SS14", 122, 98, "+5V", "VBUS", 0, props={"MPN": "SS14"}))
    items.append(sod("Diode_SMD", "D_SOD-323", "D3", "PESD5V0S1BL", 110, 104, "VBUS", "GND", 90, w=0.6, h=0.45, px=1.05,
                     props={"MPN": "PESD5V0S1BL"}))

    # ---- MCU QFN-32 (0.5 mm pitch, exposed pad without vias) ---------------------------------------
    pinmap = {1: "+3V3", 2: "/NRST", 5: "unconnected-(U3-PA0-Pad5)", 9: "/SDA", 10: "/SCL", 16: "GND",
              20: "/LED", 21: "/D-", 22: "/D+", 24: "+3V3", 32: "GND"}
    ptypes = {1: ("VDD", "power_in"), 24: ("VDD", "power_in"), 16: ("VSS", "power_in"), 32: ("VSS", "power_in"),
              2: ("NRST", "input"), 5: ("PA0", "input"), 9: ("PB7", "bidirectional"), 10: ("PB6", "bidirectional"),
              20: ("PA5", "output"), 21: ("PA11", "bidirectional"), 22: ("PA12", "bidirectional")}
    qfn = [courtyard(-3.1, -3.1, 3.1, 3.1), fab(-2.5, -2.5, 2.5, 2.5)]
    for i in range(32):
        n = i + 1
        side, k = divmod(i, 8)
        off = -1.75 + 0.5 * k
        if side == 0:
            px, py, w, h = -2.4, off, 0.875, 0.25
        elif side == 1:
            px, py, w, h = off, 2.4, 0.25, 0.875
        elif side == 2:
            px, py, w, h = 2.4, -off, 0.875, 0.25
        else:
            px, py, w, h = -off, -2.4, 0.25, 0.875
        net = pinmap.get(n)
        pf, pt = ptypes.get(n, ("P%d" % n, "bidirectional"))
        if net is None:
            net = "unconnected-(U3-P%d-Pad%d)" % (n, n)
            if net not in NET:
                NETS.append(net)
                NET[net] = (len(NETS), net)
        qfn.append(W.pad(str(n), "smd", "roundrect", px, py, w, h, FCU, net=NET[net], rratio=0.25, pinfunction=pf, pintype=pt))
    qfn.append(W.pad("33", "smd", "rect", 0, 0, 3.45, 3.45, FCU, net=NET["GND"], pinfunction="EP", pintype="passive"))
    items.append(W.footprint("Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm", 135, 108, "U3", "STM32G031K8U6",
                             qfn, extra_props={"MPN": "STM32G031K8U6"},
                             models=[M3D + "Package_DFN_QFN.3dshapes/QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm.wrl"]))
    items.append(chip("C", "0402", "C3", "100nF", 131, 104, "+3V3", "GND"))
    items.append(chip("C", "0402", "C4", "100nF", 150, 118, "+3V3", "GND"))
    items.append(chip("C", "0805", "C7", "10uF 10V", 157.6, 108, "+3V3", "GND", props={"MPN": "GRM21BR61A106KE19L"}))
    items.append(chip("R", "0603", "R1", "4k7", 128, 116, "/SDA", "+3V3", props={"MPN": "RC0603FR-074K7L"}))
    items.append(chip("R", "0603", "R2", "4k7", 128, 119, "/SCL", "+3V3", props={"MPN": "RC0603FR-074K7L"}))
    items.append(chip("R", "0402", "R3", "330", 142, 116, "/LED", "Net-(D2-A)"))
    items.append(chip("R", "0603", "R5", "10k", 138, 100, "/NRST", "+3V3", props={"MPN": "RC0603FR-0710KL"}))
    items.append(chip("LED", "0603", "D2", "GREEN", 146, 116, "GND", "Net-(D2-A)", angle=180))

    # ---- I2C header (THT) -----------------------------------------------------------------------
    hdr = [courtyard(-1.8, -1.8, 1.8, 9.4), fab(-1.27, -1.27, 1.27, 8.89)]
    for i, net in enumerate(["+3V3", "/SDA", "/SCL", "GND"], 1):
        hdr.append(W.pad(str(i), "thru_hole", "rect" if i == 1 else "oval", 0, 2.54 * (i - 1), 1.7, 1.7, THT, drill=1.0,
                         net=NET[net], pinfunction="Pin_%d" % i, pintype="passive"))
    items.append(W.footprint("Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical", 110, 113, "J2", "I2C", hdr,
                             attr=("through_hole",), models=[M3D + "Connector_PinHeader_2.54mm.3dshapes/PinHeader_1x04_P2.54mm_Vertical.wrl"]))

    # ---- bottom side: test points + one capacitor ------------------------------------------------
    for i, (tx, ty, net) in enumerate([(120, 110, "+3V3"), (124, 110, "+5V"), (120, 114, "GND"), (124, 114, "/SDA")], 1):
        items.append(W.footprint("TestPoint:TestPoint_Pad_D1.5mm", tx, ty, "TP%d" % i, net.strip("/"),
                                 [W.fp_circle(0, 0, 1.0, "B.CrtYd", 0.05),
                                  W.pad("1", "smd", "circle", 0, 0, 1.5, 1.5, ["B.Cu", "B.Mask"], net=NET[net], pintype="passive")],
                                 layer="B.Cu", attr=("exclude_from_pos_files", "exclude_from_bom")))
    items.append(chip("C", "0402", "C9", "100nF", 140, 104, "+3V3", "GND", side="B"))

    if variant == "stacked":
        items += stacked_extras()

    # ---- tracks ----------------------------------------------------------------------------------
    n = {k: v[0] for k, v in NET.items()}
    items += [W.segment(110, 89, 122, 88, 1.0, "F.Cu", n["VIN_12V"]),
              W.segment(122, 88, 132, 89, 1.0, "F.Cu", n["VIN_12V"]),
              W.segment(127.08, 88, 142, 89, 0.25, "F.Cu", n["+5V"]),            # thin power track
              W.segment(142, 89, 147.85, 98.3, 0.25, "F.Cu", n["+5V"]),
              W.segment(147.85, 96, 140, 100, 0.3, "F.Cu", n["+3V3"]),
              W.segment(110, 115.54, 127.175, 116, 0.1, "F.Cu", n["/SDA"]),     # below fab minimum
              W.segment(110, 118.08, 115, 118, 0.15, "F.Cu", n["/SCL"]),        # near limit
              W.segment(115, 118, 127.175, 119, 0.15, "F.Cu", n["/SCL"]),
              W.segment(115, 118, 124, 113.2, 0.15, "F.Cu", n["/SCL"]),         # acute junction
              W.segment(130.52, 104, 132.6, 104.25, 0.2, "F.Cu", n["+3V3"]),
              W.segment(105, 124.8, 150, 124.8, 0.25, "B.Cu", n["GND"]),         # too close to the edge
              W.via(130, 100, 0.5, 0.25, n["GND"]),                              # drill too small
              W.via(138, 102, 0.45, 0.3, n["+3V3"]),                             # thin annular ring
              W.via(128.825, 116, 0.6, 0.3, n["+3V3"]),                          # via in pad (R1.2)
              W.via(120, 110, 0.6, 0.3, n["+3V3"])]
    # ---- zones (solid connection -> THT solid + tombstone on C3) ---------------------------------
    items.append(W.zone(n["GND"], "GND", "B.Cu", [(100.5, 80.5), (159.5, 80.5), (159.5, 124.5), (100.5, 124.5)], connect="yes"))
    items.append(W.zone(n["GND"], "GND", "F.Cu", [(129, 102), (134, 102), (134, 105), (129, 105)], connect="yes"))
    return W.board(NETS, items, thickness=1.6, title="Sensor Node", company="Example Co", date="2026-11-19")


def stacked_extras():
    """Test variant: a daughter board plugs into pin socket J6 (over the MCU area) and a power header J7 sits on
    the bottom side (through-hole, soldered from the top), so stacked-board probing and two-sided jigs apply."""
    out = []
    sock = [courtyard(-1.8, -1.8, 1.8, 9.4), fab(-1.27, -1.27, 1.27, 8.89)]
    for i, net in enumerate(["+3V3", "/SDA", "/SCL", "GND"], 1):
        sock.append(W.pad(str(i), "thru_hole", "rect" if i == 1 else "oval", 0, 2.54 * (i - 1), 1.7, 1.7, THT, drill=1.0,
                          net=NET[net], pinfunction="Pin_%d" % i, pintype="passive"))
    out.append(W.footprint("Connector_PinSocket_2.54mm:PinSocket_1x04_P2.54mm_Vertical", 144, 103, "J6", "DISPLAY", sock,
                           attr=("through_hole",),
                           models=[M3D + "Connector_PinSocket_2.54mm.3dshapes/PinSocket_1x04_P2.54mm_Vertical.wrl"]))
    # bottom side: KiCad stores a flipped footprint's local geometry mirrored (local y changes sign)
    hdr = [courtyard(-1.8, -4.35, 1.8, 1.8, side="B"), fab(-1.27, -3.81, 1.27, 1.27, side="B")]
    for i, net in enumerate(["+5V", "GND"], 1):
        hdr.append(W.pad(str(i), "thru_hole", "rect" if i == 1 else "oval", 0, -2.54 * (i - 1), 1.7, 1.7, THT, drill=1.0,
                         net=NET[net], pinfunction="Pin_%d" % i, pintype="passive"))
    out.append(W.footprint("Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical", 153, 113, "J7", "5V AUX", hdr,
                           layer="B.Cu", attr=("through_hole",),
                           models=[M3D + "Connector_PinHeader_2.54mm.3dshapes/PinHeader_1x02_P2.54mm_Vertical.wrl"]))
    return out


# =========================================================================== #
# Schematic (root + power sheet)
# =========================================================================== #
def _p(name, value, x=0, y=0, hide=False):
    n = ["property", name, value, ["at", x, y, 0], ["effects", ["font", ["size", 1.27, 1.27]]] + ([["hide", Sym("yes")]] if hide else [])]
    return n


def lib_symbol(lib_id, ref_prefix, pins, power=False, descr=""):
    sym = ["symbol", lib_id]
    if power:
        sym.append(["power"])
    sym += [["pin_names", ["offset", 0]], ["exclude_from_sim", Sym("no")], ["in_bom", Sym("no" if power else "yes")],
            ["on_board", Sym("yes")], _p("Reference", ref_prefix), _p("Value", lib_id.split(":")[1]),
            _p("Footprint", "", hide=True), _p("Datasheet", "~", hide=True), _p("Description", descr, hide=True)]
    unit = ["symbol", "%s_1_1" % lib_id.split(":")[1]]
    for i, (num, etype, name) in enumerate(pins):
        unit.append(["pin", Sym(etype), Sym("line"), ["at", 0, -2.54 * i, 0], ["length", 2.54],
                     ["name", name, ["effects", ["font", ["size", 1.27, 1.27]]]],
                     ["number", num, ["effects", ["font", ["size", 1.27, 1.27]]]]])
    sym.append(unit)
    return sym


def sch_symbol(lib_id, ref, value, fp, x, y, path, project, extra=None, npins=2, dnp=False):
    s = ["symbol", ["lib_id", lib_id], ["at", x, y, 0], ["unit", 1], ["exclude_from_sim", Sym("no")],
         ["in_bom", Sym("yes")], ["on_board", Sym("yes")], ["dnp", Sym("yes" if dnp else "no")], ["uuid", W.uid()],
         _p("Reference", ref, x, y - 2), _p("Value", value, x, y + 2), _p("Footprint", fp, x, y, hide=True),
         _p("Datasheet", "~", x, y, hide=True)]
    for k, v in (extra or {}).items():
        s.append(_p(k, v, x, y, hide=True))
    for i in range(1, npins + 1):
        s.append(["pin", str(i), ["uuid", W.uid()]])
    s.append(["instances", ["project", project, ["path", path, ["reference", ref], ["unit", 1]]]])
    return s


def power_symbol(value, x, y, idx, path, project):
    return ["symbol", ["lib_id", "power:%s" % value], ["at", x, y, 0], ["unit", 1], ["exclude_from_sim", Sym("no")],
            ["in_bom", Sym("yes")], ["on_board", Sym("yes")], ["dnp", Sym("no")], ["uuid", W.uid()],
            _p("Reference", "#PWR0%02d" % idx, x, y, hide=True), _p("Value", value, x, y - 2),
            _p("Footprint", "", x, y, hide=True), _p("Datasheet", "", x, y, hide=True), ["pin", "1", ["uuid", W.uid()]],
            ["instances", ["project", project, ["path", path, ["reference", "#PWR0%02d" % idx], ["unit", 1]]]]]


def build_schematics():
    root_uuid = W.uid()
    sheet_uuid = W.uid()
    libs = ["lib_symbols",
            lib_symbol("Device:R", "R", [("1", "passive", "~"), ("2", "passive", "~")], descr="Resistor"),
            lib_symbol("Device:C", "C", [("1", "passive", "~"), ("2", "passive", "~")], descr="Unpolarized capacitor"),
            lib_symbol("Device:C_Polarized", "C", [("1", "passive", "+"), ("2", "passive", "-")], descr="Polarized capacitor"),
            lib_symbol("Device:LED", "D", [("1", "passive", "K"), ("2", "passive", "A")], descr="Light emitting diode"),
            lib_symbol("Device:D_Schottky", "D", [("1", "passive", "K"), ("2", "passive", "A")], descr="Schottky diode"),
            lib_symbol("Device:D_TVS", "D", [("1", "passive", "A1"), ("2", "passive", "A2")], descr="TVS ESD protection diode"),
            lib_symbol("Connector:USB_B_Micro", "J", [(str(i), "passive", "P%d" % i) for i in range(1, 7)]),
            lib_symbol("Connector:Conn_01x04_Pin", "J", [(str(i), "passive", "Pin_%d" % i) for i in range(1, 5)]),
            lib_symbol("Connector:Screw_Terminal_01x02", "J", [("1", "passive", "Pin_1"), ("2", "passive", "Pin_2")]),
            lib_symbol("Regulator_Linear:LM7805_TO220", "U", [("1", "power_in", "IN"), ("2", "power_in", "GND"), ("3", "power_out", "OUT")],
                       descr="Positive 1A 35V Linear Regulator"),
            lib_symbol("Regulator_Linear:AMS1117-3.3", "U", [("1", "power_in", "GND"), ("2", "power_out", "VO"), ("3", "power_in", "VI")],
                       descr="1A Low Dropout regulator"),
            lib_symbol("MCU_ST_STM32G0:STM32G031K8Ux", "U", [(str(i), "bidirectional", "P%d" % i) for i in range(1, 34)]),
            lib_symbol("Connector:TestPoint", "TP", [("1", "passive", "1")]),
            lib_symbol("Mechanical:MountingHole", "H", []),
            lib_symbol("power:GND", "#PWR", [("1", "power_in", "GND")], power=True),
            lib_symbol("power:+3V3", "#PWR", [("1", "power_in", "+3V3")], power=True),
            lib_symbol("power:+5V", "#PWR", [("1", "power_in", "+5V")], power=True),
            lib_symbol("power:VBUS", "#PWR", [("1", "power_in", "VBUS")], power=True)]
    root_path = "/%s" % root_uuid
    sub_path = "/%s/%s" % (root_uuid, sheet_uuid)
    root = ["kicad_sch", ["version", 20231120], ["generator", "eeschema"], ["generator_version", "8.0"],
            ["uuid", root_uuid], ["paper", "A4"], ["title_block", ["title", "Sensor Node"], ["date", "2026-11-19"],
                                                   ["company", "Example Co"]], libs]
    R = lambda *a, **k: root.append(sch_symbol(*a, path=root_path, project=NAME, **k))  # noqa: E731
    R("Connector:USB_B_Micro", "J1", "USB_B_Micro", "Connector_USB:USB_Micro-B_Molex-105017-0001", 30, 50, npins=6,
      extra={"MPN": "105017-0001"})
    R("Device:D_TVS", "D3", "PESD5V0S1BL", "Diode_SMD:D_SOD-323", 45, 50, extra={"MPN": "PESD5V0S1BL"})
    R("MCU_ST_STM32G0:STM32G031K8Ux", "U3", "STM32G031K8U6", "Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm",
      100, 80, npins=33, extra={"MPN": "STM32G031K8U6"})
    R("Device:C", "C3", "100nF", "Capacitor_SMD:C_0402_1005Metric", 80, 60)
    R("Device:C", "C4", "100nF", "Capacitor_SMD:C_0402_1005Metric", 85, 60)
    R("Device:C", "C7", "10uF 10V", "Capacitor_SMD:C_0805_2012Metric", 90, 60, extra={"MPN": "GRM21BR61A106KE19L"})
    R("Device:C", "C9", "100nF", "Capacitor_SMD:C_0402_1005Metric", 95, 60)
    R("Device:R", "R1", "4k7", "Resistor_SMD:R_0603_1608Metric", 120, 60, extra={"MPN": "RC0603FR-074K7L"})
    R("Device:R", "R2", "4k7", "Resistor_SMD:R_0603_1608Metric", 125, 60, extra={"MPN": "RC0603FR-074K7L"})
    R("Device:R", "R3", "330", "Resistor_SMD:R_0402_1005Metric", 130, 60)
    R("Device:R", "R5", "10k", "Resistor_SMD:R_0603_1608Metric", 135, 60, extra={"MPN": "RC0603FR-0710KL"})
    R("Device:LED", "D2", "GREEN", "LED_SMD:LED_0603_1608Metric", 140, 60)
    R("Connector:Conn_01x04_Pin", "J2", "I2C", "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical", 150, 80, npins=4)
    for i, (v, nm) in enumerate([("+3V3", "TP1"), ("+5V", "TP2"), ("GND", "TP3"), ("SDA", "TP4")], 1):
        R("Connector:TestPoint", nm, v, "TestPoint:TestPoint_Pad_D1.5mm", 160 + 5 * i, 40, npins=1)
    for i in range(1, 5):
        R("Mechanical:MountingHole", "H%d" % i, "MountingHole", "MountingHole:MountingHole_3.2mm_M3", 20 + 5 * i, 150, npins=0)
    for i, v in enumerate(["GND", "+3V3", "+3V3", "GND", "VBUS"], 1):
        root.append(power_symbol(v, 60 + 5 * i, 30, i, root_path, NAME))
    root.append(["sheet", ["at", 30, 100], ["size", 20, 10], ["fields_autoplaced", Sym("yes")],
                 ["stroke", ["width", 0.1524], ["type", Sym("solid")]], ["fill", ["color", 0, 0, 0, 0.0]],
                 ["uuid", sheet_uuid], _p("Sheetname", "Power", 30, 99), _p("Sheetfile", "power.kicad_sch", 30, 111),
                 ["instances", ["project", NAME, ["path", root_path, ["page", "2"]]]]])
    root.append(["sheet_instances", ["path", "/", ["page", "1"]]])

    sub = ["kicad_sch", ["version", 20231120], ["generator", "eeschema"], ["generator_version", "8.0"],
           ["uuid", W.uid()], ["paper", "A4"], libs]
    P = lambda *a, **k: sub.append(sch_symbol(*a, path=sub_path, project=NAME, **k))  # noqa: E731
    P("Connector:Screw_Terminal_01x02", "J3", "12V IN", "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2_1x02_P5.00mm_Horizontal",
      30, 50, extra={"MPN": "1729128"})
    P("Regulator_Linear:LM7805_TO220", "U1", "LM7805_TO220", "Package_TO_SOT_THT:TO-220-3_Vertical", 60, 50, npins=3,
      extra={"MPN": "LM7805CT"})
    P("Device:C_Polarized", "C1", "100uF 25V", "Capacitor_THT:CP_Radial_D8.0mm_P3.50mm", 45, 60, extra={"MPN": "EEU-FR1E101"})
    P("Device:C_Polarized", "C2", "47uF 6.3V", "Capacitor_THT:CP_Radial_D5.0mm_P2.00mm", 75, 60)
    P("Device:C", "C5", "1uF 16V", "Capacitor_SMD:C_0805_2012Metric", 50, 70, extra={"MPN": "GRM21BR71C105KA01L"})
    P("Device:C", "C8", "100nF", "Capacitor_SMD:C_0402_1005Metric", 110, 60)
    P("Device:R", "R4", "1k", "Resistor_SMD:R_0402_1005Metric", 55, 70)
    P("Device:D_Schottky", "D1", "SS14", "Diode_SMD:D_SOD-123", 90, 40, extra={"MPN": "SS14"})
    P("Regulator_Linear:AMS1117-3.3", "U2", "AMS1117-3.3", "Package_TO_SOT_SMD:SOT-223-3_TabPin2", 100, 50, npins=3,
      extra={"MPN": "AMS1117-3.3"})
    for i, v in enumerate(["GND", "+5V", "+3V3", "VBUS"], 10):
        sub.append(power_symbol(v, 60 + 5 * i, 30, i, sub_path, NAME))
    return dumps(root) + "\n", dumps(sub) + "\n"


def build_project():
    return {"board": {"design_settings": {"rules": {"min_clearance": 0.15, "min_track_width": 0.1, "min_via_diameter": 0.45,
                                                    "min_through_hole_diameter": 0.25, "min_copper_edge_clearance": 0.3,
                                                    "min_via_annular_width": 0.075}}},
            "meta": {"filename": NAME + ".kicad_pro", "version": 1},
            "net_settings": {"classes": [{"name": "Default", "clearance": 0.2, "track_width": 0.25, "via_diameter": 0.6,
                                          "via_drill": 0.3}]}}


def main(out=OUT, variant=None):
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, NAME + ".kicad_pcb"), "w") as fh:
        fh.write(build_board(variant))
    root, sub = build_schematics()
    with open(os.path.join(out, NAME + ".kicad_sch"), "w") as fh:
        fh.write(root)
    with open(os.path.join(out, "power.kicad_sch"), "w") as fh:
        fh.write(sub)
    with open(os.path.join(out, NAME + ".kicad_pro"), "w") as fh:
        json.dump(build_project(), fh, indent=2)
    print("sample project written to", out)


if __name__ == "__main__":
    main()
