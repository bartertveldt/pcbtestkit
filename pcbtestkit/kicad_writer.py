"""Builders that emit KiCad 8 board syntax (file format 20240108).

KiCad 8, 9 and 10 open this format; KiCad upgrades it on save.
"""
from __future__ import annotations

import uuid as _uuid
from typing import Dict, List, Optional, Sequence, Tuple

from .sexpr import Sym, dumps

PCB_VERSION = 20240108

LAYERS_2 = [
    (0, "F.Cu", "signal"), (31, "B.Cu", "signal"), (32, "B.Adhes", "user", "B.Adhesive"),
    (33, "F.Adhes", "user", "F.Adhesive"), (34, "B.Paste", "user"), (35, "F.Paste", "user"),
    (36, "B.SilkS", "user", "B.Silkscreen"), (37, "F.SilkS", "user", "F.Silkscreen"), (38, "B.Mask", "user"),
    (39, "F.Mask", "user"), (40, "Dwgs.User", "user", "User.Drawings"), (41, "Cmts.User", "user", "User.Comments"),
    (42, "Eco1.User", "user", "User.Eco1"), (43, "Eco2.User", "user", "User.Eco2"), (44, "Edge.Cuts", "user"),
    (45, "Margin", "user"), (46, "B.CrtYd", "user", "B.Courtyard"), (47, "F.CrtYd", "user", "F.Courtyard"),
    (48, "B.Fab", "user"), (49, "F.Fab", "user"),
]


def uid() -> str:
    return str(_uuid.uuid4())


def effects(size: float = 1.0, thickness: Optional[float] = None, justify: Optional[str] = None, mirror: bool = False) -> list:
    font = ["font", ["size", size, size]]
    font.append(["thickness", thickness if thickness is not None else round(size * 0.15, 3)])
    e = ["effects", font]
    if justify or mirror:
        j = ["justify"]
        if justify:
            j.append(Sym(justify))
        if mirror:
            j.append(Sym("mirror"))
        e.append(j)
    return e


def stroke(width: float) -> list:
    return ["stroke", ["width", width], ["type", Sym("solid")]]


def fp_property(name: str, value: str, x: float, y: float, layer: str, hide: bool = False, size: float = 1.0,
                angle: float = 0.0) -> list:
    n = ["property", name, value, ["at", x, y, angle], ["layer", layer]]
    if hide:
        n.append(["hide", Sym("yes")])
    n += [["uuid", uid()], effects(size, mirror=layer.startswith("B."))]
    return n


def fp_text(text: str, x: float, y: float, layer: str, size: float = 0.8) -> list:
    return ["fp_text", Sym("user"), text, ["at", x, y, 0], ["layer", layer], ["uuid", uid()],
            effects(size, mirror=layer.startswith("B."))]


def fp_line(x1, y1, x2, y2, layer: str, width: float = 0.12) -> list:
    return ["fp_line", ["start", x1, y1], ["end", x2, y2], stroke(width), ["layer", layer], ["uuid", uid()]]


def fp_rect(x1, y1, x2, y2, layer: str, width: float = 0.05) -> list:
    return ["fp_rect", ["start", x1, y1], ["end", x2, y2], stroke(width), ["fill", Sym("none")], ["layer", layer],
            ["uuid", uid()]]


def fp_circle(cx, cy, r, layer: str, width: float = 0.12) -> list:
    return ["fp_circle", ["center", cx, cy], ["end", cx + r, cy], stroke(width), ["fill", Sym("none")],
            ["layer", layer], ["uuid", uid()]]


def pad(number: str, kind: str, shape: str, x: float, y: float, w: float, h: float, layers: Sequence[str],
        drill: Optional[float] = None, net: Optional[Tuple[int, str]] = None, angle: float = 0.0,
        rratio: Optional[float] = None, pinfunction: str = "", pintype: str = "", props: Sequence[str] = ()) -> list:
    at = ["at", x, y] + ([angle] if angle else [])
    n = ["pad", number, Sym(kind), Sym(shape), at, ["size", w, h]]
    if drill:
        n.append(["drill", drill])
    if props:
        n.append(["property"] + [Sym(p) for p in props])
    n.append(["layers"] + list(layers))
    if kind == "thru_hole":
        n.append(["remove_unused_layers", Sym("no")])
    if rratio is not None:
        n.append(["roundrect_rratio", rratio])
    if net is not None and net[1]:
        n.append(["net", net[0], net[1]])
    if pinfunction:
        n.append(["pinfunction", pinfunction])
    if pintype:
        n.append(["pintype", pintype])
    n.append(["uuid", uid()])
    return n


def footprint(lib_id: str, x: float, y: float, ref: str, value: str, items: List[list], layer: str = "F.Cu",
              angle: float = 0.0, attr: Sequence[str] = ("smd",), descr: str = "", extra_props: Optional[Dict[str, str]] = None,
              models: Sequence[str] = (), ref_pos: Tuple[float, float] = (0.0, -1.5), value_pos: Tuple[float, float] = (0.0, 1.5),
              hide_ref: bool = False) -> list:
    side = "B" if layer.startswith("B.") else "F"
    n = ["footprint", lib_id, ["layer", layer], ["uuid", uid()], ["at", x, y] + ([angle] if angle else [])]
    if descr:
        n.append(["descr", descr])
    n.append(fp_property("Reference", ref, ref_pos[0], ref_pos[1], "%s.SilkS" % side, hide=hide_ref))
    n.append(fp_property("Value", value, value_pos[0], value_pos[1], "%s.Fab" % side))
    n.append(fp_property("Footprint", lib_id, 0, 0, "%s.Fab" % side, hide=True))
    n.append(fp_property("Datasheet", "", 0, 0, "%s.Fab" % side, hide=True))
    n.append(fp_property("Description", descr, 0, 0, "%s.Fab" % side, hide=True))
    for k, v in (extra_props or {}).items():
        n.append(fp_property(k, v, 0, 0, "%s.Fab" % side, hide=True))
    n.append(["attr"] + [Sym(a) for a in attr])
    n += items
    for m in models:
        n.append(["model", m, ["offset", ["xyz", 0, 0, 0]], ["scale", ["xyz", 1, 1, 1]], ["rotate", ["xyz", 0, 0, 0]]])
    return n


def gr_line(x1, y1, x2, y2, layer: str = "Edge.Cuts", width: float = 0.1) -> list:
    return ["gr_line", ["start", x1, y1], ["end", x2, y2], stroke(width), ["layer", layer], ["uuid", uid()]]


def gr_arc(start, mid, end, layer: str = "Edge.Cuts", width: float = 0.1) -> list:
    return ["gr_arc", ["start", start[0], start[1]], ["mid", mid[0], mid[1]], ["end", end[0], end[1]], stroke(width),
            ["layer", layer], ["uuid", uid()]]


def gr_rect(x1, y1, x2, y2, layer: str = "Edge.Cuts", width: float = 0.1) -> list:
    return ["gr_rect", ["start", x1, y1], ["end", x2, y2], stroke(width), ["fill", Sym("none")], ["layer", layer],
            ["uuid", uid()]]


def gr_circle(cx, cy, r, layer: str = "Edge.Cuts", width: float = 0.1) -> list:
    return ["gr_circle", ["center", cx, cy], ["end", cx + r, cy], stroke(width), ["fill", Sym("none")],
            ["layer", layer], ["uuid", uid()]]


def gr_text(text: str, x: float, y: float, layer: str = "F.SilkS", size: float = 1.5, justify: Optional[str] = None) -> list:
    return ["gr_text", text, ["at", x, y, 0], ["layer", layer], ["uuid", uid()],
            effects(size, justify=justify, mirror=layer.startswith("B."))]


def segment(x1, y1, x2, y2, width: float, layer: str, net: int) -> list:
    return ["segment", ["start", x1, y1], ["end", x2, y2], ["width", width], ["layer", layer], ["net", net],
            ["uuid", uid()]]


def via(x, y, size: float, drill: float, net: int) -> list:
    return ["via", ["at", x, y], ["size", size], ["drill", drill], ["layers", "F.Cu", "B.Cu"], ["net", net],
            ["uuid", uid()]]


def zone(net: int, net_name: str, layer: str, pts: Sequence[Tuple[float, float]], connect: Optional[str] = None,
         clearance: float = 0.3) -> list:
    cp = ["connect_pads"] + ([Sym(connect)] if connect else []) + [["clearance", clearance]]
    return ["zone", ["net", net], ["net_name", net_name], ["layer", layer], ["uuid", uid()],
            ["hatch", Sym("edge"), 0.5], cp, ["min_thickness", 0.25], ["filled_areas_thickness", Sym("no")],
            ["fill", ["thermal_gap", 0.5], ["thermal_bridge_width", 0.5]],
            ["polygon", ["pts"] + [["xy", x, y] for x, y in pts]]]


def board(nets: Sequence[str], items: List[list], thickness: float = 1.6, title: str = "", rev: str = "",
          company: str = "", date: str = "", comment: str = "") -> str:
    layers = ["layers"]
    for l in LAYERS_2:
        node = [l[0], l[1], Sym(l[2])]
        if len(l) > 3:
            node.append(l[3])
        layers.append(node)
    tb = ["title_block"]
    if title:
        tb.append(["title", title])
    if date:
        tb.append(["date", date])
    if rev:
        tb.append(["rev", rev])
    if company:
        tb.append(["company", company])
    if comment:
        tb.append(["comment", 1, comment])
    root = ["kicad_pcb", ["version", PCB_VERSION], ["generator", "pcbtestkit"], ["generator_version", "8.0"],
            ["general", ["thickness", thickness], ["legacy_teardrops", Sym("no")]], ["paper", "A4"]]
    if len(tb) > 1:
        root.append(tb)
    root.append(layers)
    root.append(["setup", ["pad_to_mask_clearance", 0], ["allow_soldermask_bridges_in_footprints", Sym("no")]])
    root.append(["net", 0, ""])
    for i, n in enumerate(nets, 1):
        root.append(["net", i, n])
    root += items
    text = dumps(_fix_layer_rows(root))
    return text + "\n"


def _fix_layer_rows(root: list) -> list:
    """Layer rows are written as (0 "F.Cu" signal): the head is a number, not a keyword."""
    return root
