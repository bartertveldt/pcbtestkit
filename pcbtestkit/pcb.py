"""Board model loaded straight from a .kicad_pcb file (KiCad 6 - 10 formats)."""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from . import sexpr as S
from .geometry import (BBox, Grid, Point, Seg, arc_points, chain_segments, circle_points, point_in_segments,
                       polyline_segments, pt_seg_dist, rect_corners, rot, to_board)

COPPER_SUFFIX = ".Cu"


@dataclass
class Pad:
    ref: str
    number: str
    kind: str              # smd | thru_hole | np_thru_hole | connect
    shape: str             # rect | circle | oval | roundrect | trapezoid | custom
    x: float
    y: float
    angle: float           # absolute orientation (deg)
    w: float
    h: float
    drill: float           # smallest drill dimension, 0 for SMD
    drill_w: float
    drill_h: float
    layers: List[str]
    net: str
    pinfunction: str = ""
    pintype: str = ""
    zone_connect: Optional[int] = None   # 0 none, 1 thermal, 2 solid, 3 tht-thermal; None = inherit
    props: Set[str] = field(default_factory=set)
    local_x: float = 0.0
    local_y: float = 0.0
    rratio: float = 0.0                  # roundrect corner ratio
    remove_unused: bool = False          # no copper on unconnected inner layers
    mask_margin: Optional[float] = None  # solder mask expansion (None = footprint/board default)

    @property
    def name(self) -> str:
        return "%s.%s" % (self.ref, self.number)

    @property
    def is_tht(self) -> bool:
        return self.kind == "thru_hole"

    @property
    def is_npth(self) -> bool:
        return self.kind == "np_thru_hole"

    @property
    def has_copper(self) -> bool:
        return any(l.endswith(COPPER_SUFFIX) for l in self.layers)

    def on_copper(self, side: str) -> bool:
        lay = "F.Cu" if side == "top" else "B.Cu"
        return lay in self.layers or "*.Cu" in self.layers or (self.is_tht and self.has_copper)

    def mask_open(self, side: str) -> bool:
        lay = "F.Mask" if side == "top" else "B.Mask"
        return lay in self.layers or "*.Mask" in self.layers

    @property
    def min_size(self) -> float:
        return min(self.w, self.h)

    def polygon(self) -> List[Point]:
        return rect_corners(self.x, self.y, self.w, self.h, self.angle)

    def bbox(self) -> BBox:
        """Exact bounding box for circle / oval / roundrect / rect pads at any angle."""
        if self.shape in ("circle", "oval", "roundrect"):
            if self.shape == "circle":
                r = self.w / 2
                return BBox(self.x - r, self.y - r, self.x + r, self.y + r)
            if self.shape == "oval":
                r = min(self.w, self.h) / 2
                core = rect_corners(self.x, self.y, self.w - 2 * r if self.w > self.h else 0.0,
                                    self.h - 2 * r if self.h > self.w else 0.0, self.angle)
            else:
                r = self.rratio * min(self.w, self.h)
                core = rect_corners(self.x, self.y, self.w - 2 * r, self.h - 2 * r, self.angle)
            return BBox.of_points(core).grow(r)
        return BBox.of_points(self.polygon())

    def contains(self, px: float, py: float, margin: float = 0.0) -> bool:
        lx, ly = rot(px - self.x, py - self.y, -self.angle)
        if self.shape == "circle":
            return math.hypot(lx, ly) <= self.w / 2 + margin
        return abs(lx) <= self.w / 2 + margin and abs(ly) <= self.h / 2 + margin


@dataclass
class Graphic:
    kind: str              # line | rect | circle | arc | poly
    layer: str
    local: List[Point]     # polyline in footprint-local coords (board coords for board graphics)
    circle: Optional[Tuple[float, float, float]] = None  # cx, cy, r (local)
    width: float = 0.0
    closed: bool = False


@dataclass
class Model3D:
    path: str
    offset: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    scale: Tuple[float, float, float] = (1.0, 1.0, 1.0)
    rotate: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    hide: bool = False


@dataclass
class Footprint:
    ref: str
    value: str
    lib_id: str
    layer: str
    x: float
    y: float
    angle: float
    attrs: Set[str] = field(default_factory=set)
    props: Dict[str, str] = field(default_factory=dict)
    pads: List[Pad] = field(default_factory=list)
    graphics: List[Graphic] = field(default_factory=list)
    models: List[Model3D] = field(default_factory=list)
    uuid: str = ""
    path: str = ""
    descr: str = ""
    dnp: bool = False
    fields: Dict[str, str] = field(default_factory=dict)   # merged schematic fields
    mask_margin: Optional[float] = None
    silk_texts: List[Tuple[float, float, float, float, str, str]] = field(default_factory=list)  # h, stroke, lx, ly, layer, text

    # ---- identity ---------------------------------------------------------
    @property
    def name(self) -> str:
        return self.lib_id.split(":", 1)[-1]

    @property
    def library(self) -> str:
        return self.lib_id.split(":", 1)[0] if ":" in self.lib_id else ""

    @property
    def side(self) -> str:
        return "bottom" if self.layer.startswith("B.") else "top"

    @property
    def local_flipped(self) -> bool:
        """True when the file stores this footprint's local geometry mirrored: KiCad does that for B.* parts
        (pads, graphics and, when rendering, the 3D model are turned 180 degrees about the X axis)."""
        return self.layer.startswith("B.")

    @property
    def prefix(self) -> str:
        p = ""
        for ch in self.ref:
            if ch.isalpha() or ch == "_":
                p += ch
            else:
                break
        return p.upper()

    @property
    def is_tht(self) -> bool:
        if "through_hole" in self.attrs:
            return True
        if "smd" in self.attrs:
            return False
        return any(p.is_tht for p in self.pads)

    @property
    def is_smd(self) -> bool:
        if "smd" in self.attrs:
            return True
        return bool(self.pads) and not self.is_tht and any(p.kind == "smd" for p in self.pads)

    @property
    def mount(self) -> str:
        if self.is_tht:
            return "tht"
        if self.is_smd:
            return "smd"
        return "other"

    @property
    def board_only(self) -> bool:
        return "board_only" in self.attrs

    @property
    def exclude_from_bom(self) -> bool:
        return "exclude_from_bom" in self.attrs

    @property
    def exclude_from_pos(self) -> bool:
        return "exclude_from_pos_files" in self.attrs

    # ---- geometry ---------------------------------------------------------
    def to_board(self, lx: float, ly: float) -> Point:
        return to_board(self.x, self.y, self.angle, lx, ly)

    def layer_graphics(self, layer_kind: str) -> List[Graphic]:
        """layer_kind: 'CrtYd' | 'Fab' | 'SilkS' | 'Edge.Cuts'."""
        return [g for g in self.graphics if g.layer.endswith(layer_kind)]

    def local_bbox(self, layer_kind: str) -> BBox:
        b = BBox()
        for g in self.layer_graphics(layer_kind):
            if g.circle:
                cx, cy, r = g.circle
                b.add(cx - r, cy - r)
                b.add(cx + r, cy + r)
            for p in g.local:
                b.add(p[0], p[1])
        return b

    def pads_local_bbox(self) -> BBox:
        b = BBox()
        for p in self.pads:
            for (cx, cy) in rect_corners(p.local_x, p.local_y, p.w, p.h, p.angle - self.angle):
                b.add(cx, cy)
        return b

    def body_local_bbox(self) -> Tuple[BBox, str]:
        """Best guess of the body outline in local coords and where it came from."""
        fab = self.local_bbox("Fab")
        if fab.valid and fab.w > 0.2 and fab.h > 0.2:
            return fab, "fab"
        crt = self.local_bbox("CrtYd")
        if crt.valid:
            return crt.grow(-0.25), "courtyard"
        pads = self.pads_local_bbox()
        if pads.valid:
            return pads, "pads"
        return BBox(-0.5, -0.5, 0.5, 0.5), "default"

    def courtyard_local(self) -> BBox:
        crt = self.local_bbox("CrtYd")
        if crt.valid:
            return crt
        body, _ = self.body_local_bbox()
        pads = self.pads_local_bbox()
        body.merge(pads)
        return body.grow(0.25)

    def courtyard_bbox(self) -> BBox:
        """Board-coordinate AABB of the courtyard (rotated)."""
        lb = self.courtyard_local()
        return BBox.of_points(self.to_board(x, y) for x, y in lb.corners())

    def courtyard_polygon(self) -> List[Point]:
        lb = self.courtyard_local()
        return [self.to_board(x, y) for x, y in lb.corners()]

    def dominant_circle(self, layer_kind: str = "Fab") -> Optional[Tuple[float, float, float]]:
        circles = [g.circle for g in self.layer_graphics(layer_kind) if g.circle]
        if not circles:
            return None
        return max(circles, key=lambda c: c[2])

    def pad(self, number: str) -> Optional[Pad]:
        for p in self.pads:
            if p.number == number:
                return p
        return None

    def min_pitch(self) -> float:
        """Smallest centre distance between copper pads of this footprint."""
        cu = [p for p in self.pads if p.has_copper]
        best = math.inf
        for i in range(len(cu)):
            for j in range(i + 1, len(cu)):
                d = math.hypot(cu[i].x - cu[j].x, cu[i].y - cu[j].y)
                if 1e-6 < d < best:
                    best = d
        return best

    def nets(self) -> Set[str]:
        return {p.net for p in self.pads if p.net}

    def get_field(self, *names: str, default: str = "") -> str:
        lname = {k.lower(): v for k, v in list(self.props.items()) + list(self.fields.items())}
        for n in names:
            v = lname.get(n.lower())
            if v not in (None, "", "~"):
                return v
        return default


@dataclass
class Track:
    kind: str              # segment | arc
    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    layer: str
    net: str
    mid: Optional[Point] = None

    def segments(self) -> List[Seg]:
        if self.kind == "arc" and self.mid:
            return polyline_segments(arc_points((self.x1, self.y1), self.mid, (self.x2, self.y2)))
        return [(self.x1, self.y1, self.x2, self.y2)]

    def bbox(self) -> BBox:
        b = BBox()
        for s in self.segments():
            b.add(s[0], s[1])
            b.add(s[2], s[3])
        return b.grow(self.width / 2)

    @property
    def length(self) -> float:
        return sum(math.hypot(s[2] - s[0], s[3] - s[1]) for s in self.segments())


@dataclass
class Via:
    x: float
    y: float
    size: float
    drill: float
    layers: List[str]
    net: str
    via_type: str = "through"   # through | blind | micro
    tented: Optional[bool] = None
    remove_unused: bool = False

    @property
    def annular(self) -> float:
        return (self.size - self.drill) / 2


@dataclass
class Zone:
    net: str
    layers: List[str]
    connect: str           # thermal | solid | none | tht_thermal
    clearance: float
    min_thickness: float
    thermal_gap: float
    thermal_bridge: float
    outline: List[Point]
    keepout: bool = False
    priority: int = 0
    filled: bool = False
    name: str = ""
    fills: Dict[str, List[List[Point]]] = field(default_factory=dict)   # layer -> filled polygons

    def contains(self, x: float, y: float) -> bool:
        if len(self.outline) < 3:
            return False
        return point_in_segments(x, y, polyline_segments(self.outline, closed=True))

    def on_layer(self, layer: str) -> bool:
        return layer in self.layers or ("*.Cu" in self.layers and layer.endswith(".Cu"))


@dataclass
class Board:
    path: str
    version: int = 0
    generator: str = ""
    generator_version: str = ""
    thickness: float = 1.6
    copper_layers: List[str] = field(default_factory=list)
    layer_names: Dict[str, str] = field(default_factory=dict)
    nets: Dict[int, str] = field(default_factory=dict)
    footprints: List[Footprint] = field(default_factory=list)
    tracks: List[Track] = field(default_factory=list)
    vias: List[Via] = field(default_factory=list)
    zones: List[Zone] = field(default_factory=list)
    edge_segs: List[Seg] = field(default_factory=list)
    texts: List[Tuple[str, str]] = field(default_factory=list)
    title_block: Dict[str, str] = field(default_factory=dict)
    copper_thickness_mm: float = 0.035
    inner_copper_mm: Optional[float] = None
    copper_finish: str = ""
    mask_margin: float = 0.0                 # board default pad-to-mask clearance
    mask_min_web: float = 0.0                # KiCad merges mask openings closer than this
    silk_lines: List[Tuple[float, float, float, str, str]] = field(default_factory=list)          # width, x, y, layer, owner
    silk_texts: List[Tuple[float, float, float, float, str, str]] = field(default_factory=list)   # h, stroke, x, y, layer, text
    stackup: List[Dict[str, object]] = field(default_factory=list)
    rules: Dict[str, float] = field(default_factory=dict)        # from .kicad_pro
    netclasses: List[Dict[str, object]] = field(default_factory=list)
    project_path: str = ""

    _edge_grid: Optional[Grid] = None
    _by_ref: Optional[Dict[str, Footprint]] = None

    # ---- lookups ----------------------------------------------------------
    @property
    def name(self) -> str:
        return os.path.splitext(os.path.basename(self.path))[0]

    @property
    def by_ref(self) -> Dict[str, Footprint]:
        if self._by_ref is None:
            self._by_ref = {}
            for fp in self.footprints:
                if fp.ref and fp.ref not in self._by_ref:
                    self._by_ref[fp.ref] = fp
        return self._by_ref

    def all_pads(self) -> List[Pad]:
        return [p for fp in self.footprints for p in fp.pads]

    def pads_by_net(self) -> Dict[str, List[Pad]]:
        out: Dict[str, List[Pad]] = {}
        for p in self.all_pads():
            if p.net:
                out.setdefault(p.net, []).append(p)
        return out

    def net_names(self) -> List[str]:
        names = set(n for n in self.nets.values() if n)
        names.update(p.net for p in self.all_pads() if p.net)
        return sorted(names)

    # ---- outline ----------------------------------------------------------
    def outline_bbox(self) -> BBox:
        b = BBox()
        for s in self.edge_segs:
            b.add(s[0], s[1])
            b.add(s[2], s[3])
        if not b.valid:  # no outline: fall back to footprint extents
            for fp in self.footprints:
                b.merge(fp.courtyard_bbox())
        return b

    def outline_loops(self) -> List[List[Point]]:
        return chain_segments(self.edge_segs)

    def _grid(self) -> Grid:
        if self._edge_grid is None:
            g = Grid(5.0)
            for s in self.edge_segs:
                g.insert(BBox(min(s[0], s[2]), min(s[1], s[3]), max(s[0], s[2]), max(s[1], s[3])), s)
            self._edge_grid = g
        return self._edge_grid

    def edge_distance(self, x: float, y: float, search: float = 30.0) -> float:
        if not self.edge_segs:
            return math.inf
        g = self._grid()
        r = 2.5
        while True:
            cands = g.query(BBox(x - r, y - r, x + r, y + r))
            if cands:
                d = min(pt_seg_dist(x, y, *s) for s in cands)
                if d <= r or r >= search:
                    return d
            if r >= search:
                return min(pt_seg_dist(x, y, *s) for s in self.edge_segs)
            r *= 2

    def segs_near(self, bbox: BBox) -> List[Seg]:
        if not self.edge_segs:
            return []
        return self._grid().query(bbox)

    def inside(self, x: float, y: float) -> bool:
        if not self.edge_segs:
            return True
        return point_in_segments(x, y, self.edge_segs)


# =========================================================================== #
# Loader
# =========================================================================== #
def _net_name(node: Optional[list], nets: Dict[int, str]) -> str:
    if node is None:
        return ""
    args = [a for a in node[1:] if not isinstance(a, list)]
    name = ""
    for a in args:
        try:
            int(a)
        except ValueError:
            name = a
    if name:
        return name
    if args:
        try:
            return nets.get(int(args[0]), "")
        except ValueError:
            return ""
    return ""


def _graphic(node: list) -> Optional[Graphic]:
    kind = node[0].split("_", 1)[1]  # fp_line -> line, gr_arc -> arc
    layer = S.value(node, "layer", "")
    stroke = S.child(node, "stroke")
    width = S.num(S.value(stroke, "width", None), 0.0) if stroke else S.num(S.value(node, "width", 0), 0.0)
    if kind == "line":
        a, b = S.xy(S.child(node, "start")), S.xy(S.child(node, "end"))
        return Graphic("line", layer, [a, b], width=width)
    if kind == "rect":
        (x0, y0), (x1, y1) = S.xy(S.child(node, "start")), S.xy(S.child(node, "end"))
        return Graphic("rect", layer, [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], width=width, closed=True)
    if kind == "circle":
        c, e = S.xy(S.child(node, "center")), S.xy(S.child(node, "end"))
        r = math.hypot(e[0] - c[0], e[1] - c[1])
        return Graphic("circle", layer, circle_points(c[0], c[1], r), circle=(c[0], c[1], r), width=width, closed=True)
    if kind == "arc":
        mid = S.child(node, "mid")
        if mid is not None:
            pts = arc_points(S.xy(S.child(node, "start")), S.xy(mid), S.xy(S.child(node, "end")))
        else:  # legacy (KiCad 5): start=center, end=arc start, angle=sweep
            c = S.xy(S.child(node, "start"))
            s0 = S.xy(S.child(node, "end"))
            ang = math.radians(S.num(S.value(node, "angle", 90)))
            r = math.hypot(s0[0] - c[0], s0[1] - c[1])
            a0 = math.atan2(s0[1] - c[1], s0[0] - c[0])
            n = max(2, int(abs(math.degrees(ang)) / 10) + 1)
            pts = [(c[0] + r * math.cos(a0 + ang * i / n), c[1] + r * math.sin(a0 + ang * i / n)) for i in range(n + 1)]
        return Graphic("arc", layer, pts, width=width)
    if kind == "poly":
        pts_node = S.child(node, "pts")
        pts: List[Point] = []
        if pts_node:
            for c in pts_node[1:]:
                if S.is_node(c, "xy"):
                    pts.append(S.xy(c))
                elif S.is_node(c, "arc"):
                    pts.extend(arc_points(S.xy(S.child(c, "start")), S.xy(S.child(c, "mid")), S.xy(S.child(c, "end"))))
        if pts and pts[0] != pts[-1]:
            pts.append(pts[0])
        return Graphic("poly", layer, pts, width=width, closed=True)
    return None


def _hidden(node: list) -> bool:
    eff = S.child(node, "effects")
    return "hide" in S.atoms(node) or S.yes(node, "hide") or (eff is not None and ("hide" in S.atoms(eff) or S.yes(eff, "hide")))


def _font(node: list) -> Tuple[float, float]:
    """(height, stroke thickness) of a text node; thickness 0 when not given."""
    font = S.child(S.child(node, "effects"), "font") if S.child(node, "effects") is not None else None
    size = S.child(font, "size") if font is not None else None
    h = S.num(size[1]) if size is not None and len(size) > 1 else 0.0
    return h, S.num(S.value(font, "thickness", 0.0)) if font is not None else 0.0


def _xyz(node: Optional[list], name: str, default):
    c = S.child(node, name) if node else None
    if c is None:
        return default
    v = S.child(c, "xyz")
    if v is None or len(v) < 4:
        return default
    return (S.num(v[1]), S.num(v[2]), S.num(v[3]))


def _parse_footprint(node: list, nets: Dict[int, str]) -> Footprint:
    lib_id = node[1] if len(node) > 1 and not isinstance(node[1], list) else ""
    at = S.child(node, "at")
    fx, fy = S.xy(at)
    fang = S.num(at[3]) if at is not None and len(at) > 3 else 0.0
    fp = Footprint(ref="", value="", lib_id=lib_id, layer=S.value(node, "layer", "F.Cu"), x=fx, y=fy, angle=fang)
    fp.uuid = S.value(node, "uuid", "") or S.value(node, "tstamp", "")
    fp.path = S.value(node, "path", "")
    fp.descr = S.value(node, "descr", "")
    attr = S.child(node, "attr")
    if attr is not None:
        fp.attrs = set(S.atoms(attr))
    fp.dnp = "dnp" in fp.attrs or S.yes(node, "dnp")
    mm = S.value(node, "solder_mask_margin", None)
    fp.mask_margin = S.num(mm) if mm is not None else None
    for c in node[1:]:
        if not isinstance(c, list) or not c:
            continue
        tag = c[0]
        if tag in ("property", "fp_text") and len(c) >= 3:
            if tag == "property":
                fp.props[str(c[1])] = str(c[2])
            elif c[1] == "reference":
                fp.props.setdefault("Reference", str(c[2]))
            elif c[1] == "value":
                fp.props.setdefault("Value", str(c[2]))
            lay = S.value(c, "layer", "") or ""
            if lay.endswith("SilkS") and not _hidden(c) and str(c[2]).strip():
                h, t = _font(c)
                tx, ty = S.xy(S.child(c, "at"))
                if h > 0:
                    fp.silk_texts.append((h, t, tx, ty, lay, str(c[2])))
        elif tag in ("fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly"):
            g = _graphic(c)
            if g is not None:
                fp.graphics.append(g)
        elif tag == "pad":
            fp.pads.append(_parse_pad(c, fp, nets))
        elif tag == "model" and len(c) > 1:
            off = _xyz(c, "offset", None)
            if off is None:
                at3 = _xyz(c, "at", (0.0, 0.0, 0.0))
                off = tuple(v * 25.4 for v in at3)
            fp.models.append(Model3D(path=str(c[1]), offset=off, scale=_xyz(c, "scale", (1.0, 1.0, 1.0)),
                                     rotate=_xyz(c, "rotate", (0.0, 0.0, 0.0)), hide=S.yes(c, "hide")))
    fp.ref = fp.props.get("Reference", "")
    fp.value = fp.props.get("Value", "")
    if fp.props.get("dnp", "").lower() in ("yes", "true", "1"):
        fp.dnp = True
    for p in fp.pads:
        p.ref = fp.ref
    return fp


def _parse_pad(c: list, fp: Footprint, nets: Dict[int, str]) -> Pad:
    number = str(c[1]) if len(c) > 1 and not isinstance(c[1], list) else ""
    kind = str(c[2]) if len(c) > 2 and not isinstance(c[2], list) else "smd"
    shape = str(c[3]) if len(c) > 3 and not isinstance(c[3], list) else "rect"
    at = S.child(c, "at")
    lx, ly = S.xy(at)
    # pad angles in board files are absolute; an omitted angle means 0 deg (not the footprint's rotation)
    pang = S.num(at[3]) if at is not None and len(at) > 3 else 0.0
    size = S.child(c, "size")
    w = S.num(size[1]) if size and len(size) > 1 else 0.0
    h = S.num(size[2]) if size and len(size) > 2 else w
    drill = S.child(c, "drill")
    dw = dh = 0.0
    if drill is not None:
        vals = [S.num(a) for a in drill[1:] if not isinstance(a, list) and a != "oval"]
        if vals:
            dw = vals[0]
            dh = vals[1] if len(vals) > 1 else vals[0]
    layers_node = S.child(c, "layers")
    layers = [str(a) for a in layers_node[1:]] if layers_node else []
    x, y = fp.to_board(lx, ly)
    zc = S.value(c, "zone_connect", None)
    mm = S.value(c, "solder_mask_margin", None)
    props = set()
    pr = S.child(c, "property")
    if pr is not None:
        props.update(str(a) for a in pr[1:] if not isinstance(a, list))
    return Pad(ref=fp.ref, number=number, kind=kind, shape=shape, x=x, y=y, angle=pang, w=w, h=h,
               drill=min(dw, dh) if dw else 0.0, drill_w=dw, drill_h=dh, layers=layers,
               net=_net_name(S.child(c, "net"), nets), pinfunction=S.value(c, "pinfunction", "") or "",
               pintype=S.value(c, "pintype", "") or "", zone_connect=int(S.num(zc)) if zc is not None else None,
               props=props, local_x=lx, local_y=ly,
               rratio=S.num(S.value(c, "roundrect_rratio", 0.0)) if shape == "roundrect" else 0.0,
               mask_margin=S.num(mm) if mm is not None else None,
               remove_unused=S.value(c, "remove_unused_layers", "no") in ("yes", "true") or
               S.child(c, "remove_unused_layers") is not None and len(S.child(c, "remove_unused_layers")) == 1)


def _parse_zone(c: list, nets: Dict[int, str]) -> Zone:
    layers: List[str] = []
    ln = S.child(c, "layers")
    if ln:
        layers = [str(a) for a in ln[1:]]
    else:
        l1 = S.value(c, "layer", None)
        if l1:
            layers = [l1]
    cp = S.child(c, "connect_pads")
    connect, clearance = "thermal", 0.0
    if cp is not None:
        mode = [a for a in cp[1:] if not isinstance(a, list)]
        if mode:
            connect = {"yes": "solid", "no": "none", "thru_hole_only": "tht_thermal"}.get(mode[0], "thermal")
        clearance = S.num(S.value(cp, "clearance", 0))
    fill = S.child(c, "fill")
    outline: List[Point] = []
    poly = S.child(c, "polygon")
    if poly:
        pts = S.child(poly, "pts")
        if pts:
            outline = [S.xy(p) for p in pts[1:] if S.is_node(p, "xy")]
    netnode = S.child(c, "net")
    net = S.value(c, "net_name", "") or _net_name(netnode, nets)
    fills: Dict[str, List[List[Point]]] = {}
    for fpn in S.children(c, "filled_polygon"):
        lay = S.value(fpn, "layer", None) or (layers[0] if layers else "")
        pts = S.child(fpn, "pts")
        if pts:
            poly = [S.xy(p) for p in pts[1:] if S.is_node(p, "xy")]
            if len(poly) >= 3:
                fills.setdefault(lay, []).append(poly)
    return Zone(net=net, layers=layers, connect=connect, clearance=clearance,
                min_thickness=S.num(S.value(c, "min_thickness", 0.25)),
                thermal_gap=S.num(S.value(fill, "thermal_gap", 0.5)) if fill else 0.5,
                thermal_bridge=S.num(S.value(fill, "thermal_bridge_width", 0.5)) if fill else 0.5,
                outline=outline, keepout=S.child(c, "keepout") is not None,
                priority=int(S.num(S.value(c, "priority", 0))), filled=bool(S.children(c, "filled_polygon")),
                name=S.value(c, "name", "") or "", fills=fills)


def load_project_rules(pro_path: str) -> Tuple[Dict[str, float], List[Dict[str, object]]]:
    rules: Dict[str, float] = {}
    classes: List[Dict[str, object]] = []
    if not pro_path or not os.path.exists(pro_path):
        return rules, classes
    try:
        with open(pro_path, "r", encoding="utf-8") as fh:
            pro = json.load(fh)
    except (OSError, ValueError):
        return rules, classes
    r = pro.get("board", {}).get("design_settings", {}).get("rules", {})
    for k, v in r.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            rules[k] = float(v)
    for nc in pro.get("net_settings", {}).get("classes", []) or []:
        if isinstance(nc, dict):
            classes.append({k: nc.get(k) for k in ("name", "clearance", "track_width", "via_diameter", "via_drill",
                                                    "diff_pair_width", "diff_pair_gap") if k in nc})
    return rules, classes


def load_board(path: str, pro_path: Optional[str] = None, text: Optional[str] = None) -> Board:
    """Parse a .kicad_pcb file, or ``text`` (the file's content; ``path`` then only names it)."""
    root = S.parse(text) if text is not None else S.load(path)
    if not S.is_node(root, "kicad_pcb"):
        raise ValueError("%s is not a KiCad board file" % path)
    b = Board(path=os.path.abspath(path))
    b.version = int(S.num(S.value(root, "version", 0)))
    b.generator = S.value(root, "generator", "") or ""
    b.generator_version = S.value(root, "generator_version", "") or ""
    gen = S.child(root, "general")
    b.thickness = S.num(S.value(gen, "thickness", 1.6), 1.6)

    layers = S.child(root, "layers")
    if layers:
        for l in layers[1:]:
            if isinstance(l, list) and len(l) >= 3:
                lname, ltype = str(l[1]), str(l[2])
                b.layer_names[lname] = str(l[3]) if len(l) > 3 else lname
                if lname.endswith(COPPER_SUFFIX) and ltype in ("signal", "power", "mixed", "jumper"):
                    b.copper_layers.append(lname)
    if not b.copper_layers:
        b.copper_layers = ["F.Cu", "B.Cu"]

    setup = S.child(root, "setup")
    stack = S.child(setup, "stackup") if setup else None
    if stack:
        for l in S.children(stack, "layer"):
            entry = {"name": l[1], "type": S.value(l, "type", ""), "thickness": S.num(S.value(l, "thickness", 0))}
            mat = S.value(l, "material", None)
            if mat:
                entry["material"] = mat
            b.stackup.append(entry)
            if str(l[1]) == "F.Cu" and entry["thickness"]:
                b.copper_thickness_mm = float(entry["thickness"])
            if str(l[1]).startswith("In") and str(l[1]).endswith(".Cu") and entry["thickness"]:
                b.inner_copper_mm = min(b.inner_copper_mm or 1e9, float(entry["thickness"]))
        b.copper_finish = S.value(stack, "copper_finish", "") or ""
    if setup:
        b.mask_margin = S.num(S.value(setup, "pad_to_mask_clearance", 0.0))
        b.mask_min_web = S.num(S.value(setup, "solder_mask_min_width", 0.0))

    tb = S.child(root, "title_block")
    if tb:
        for c in tb[1:]:
            if isinstance(c, list) and len(c) >= 2:
                if c[0] == "comment" and len(c) >= 3:
                    b.title_block["comment%s" % c[1]] = str(c[2])
                else:
                    b.title_block[str(c[0])] = str(c[1])

    for n in S.children(root, "net"):
        if len(n) >= 3:
            try:
                b.nets[int(n[1])] = str(n[2])
            except ValueError:
                pass
        elif len(n) == 2:
            b.nets[len(b.nets)] = str(n[1])

    for c in root[1:]:
        if not isinstance(c, list) or not c:
            continue
        tag = c[0]
        if tag in ("footprint", "module"):
            b.footprints.append(_parse_footprint(c, b.nets))
        elif tag in ("segment", "arc"):
            (x1, y1), (x2, y2) = S.xy(S.child(c, "start")), S.xy(S.child(c, "end"))
            mid = S.xy(S.child(c, "mid")) if S.child(c, "mid") is not None else None
            b.tracks.append(Track(tag, x1, y1, x2, y2, S.num(S.value(c, "width", 0.25)), S.value(c, "layer", ""),
                                  _net_name(S.child(c, "net"), b.nets), mid))
        elif tag == "via":
            x, y = S.xy(S.child(c, "at"))
            ln = S.child(c, "layers")
            vt = "through"
            for a in S.atoms(c):
                if a in ("blind", "micro"):
                    vt = a
            tent = None
            tn = S.child(c, "tenting")
            if tn is not None:
                tent = S.yes(tn, "front") or S.yes(tn, "back") or "front" in S.atoms(tn)
            ru = S.child(c, "remove_unused_layers")
            b.vias.append(Via(x, y, S.num(S.value(c, "size", 0.6)), S.num(S.value(c, "drill", 0.3)),
                              [str(a) for a in ln[1:]] if ln else ["F.Cu", "B.Cu"],
                              _net_name(S.child(c, "net"), b.nets), vt, tent,
                              remove_unused=ru is not None and (len(ru) == 1 or ru[1] in ("yes", "true"))))
        elif tag == "zone":
            b.zones.append(_parse_zone(c, b.nets))
        elif tag in ("gr_line", "gr_rect", "gr_circle", "gr_arc", "gr_poly"):
            lay = S.value(c, "layer", "") or ""
            if lay == "Edge.Cuts":
                g = _graphic(c)
                if g:
                    b.edge_segs.extend(polyline_segments(g.local))
            elif lay.endswith("SilkS"):
                g = _graphic(c)
                if g and g.width > 0 and g.local:
                    b.silk_lines.append((g.width, g.local[0][0], g.local[0][1], lay, "board"))
        elif tag == "gr_text" and len(c) > 1:
            b.texts.append((str(c[1]), S.value(c, "layer", "")))
            lay = S.value(c, "layer", "") or ""
            if lay.endswith("SilkS") and not _hidden(c) and str(c[1]).strip():
                h, t = _font(c)
                tx, ty = S.xy(S.child(c, "at"))
                if h > 0:
                    b.silk_texts.append((h, t, tx, ty, lay, str(c[1])))

    # footprint-level Edge.Cuts (e.g. connector cut-outs), silkscreen lines and texts
    for fp in b.footprints:
        for g in fp.graphics:
            if g.layer == "Edge.Cuts":
                pts = [fp.to_board(x, y) for x, y in g.local]
                b.edge_segs.extend(polyline_segments(pts))
            elif g.layer.endswith("SilkS") and g.width > 0 and g.local:
                x0, y0 = fp.to_board(*g.local[0])
                b.silk_lines.append((g.width, x0, y0, g.layer, fp.ref))
        for h, t, lx, ly, lay, txt in fp.silk_texts:
            tx, ty = fp.to_board(lx, ly)          # text positions are footprint-relative, like pads
            b.silk_texts.append((h, t, tx, ty, lay, "%s: %s" % (fp.ref, txt) if txt != fp.ref else txt))

    if pro_path is None:
        cand = os.path.splitext(path)[0] + ".kicad_pro"
        pro_path = cand if os.path.exists(cand) else ""
    b.project_path = pro_path or ""
    b.rules, b.netclasses = load_project_rules(pro_path or "")
    return b
