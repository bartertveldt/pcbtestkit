"""2D geometry helpers in KiCad board coordinates (mm, +x right, +y down)."""
from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

Point = Tuple[float, float]
Seg = Tuple[float, float, float, float]


def rot(x: float, y: float, deg: float) -> Point:
    """Rotate a point the way KiCad does (positive angle = counter-clockwise on screen)."""
    if not deg:
        return x, y
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return x * c + y * s, -x * s + y * c


def to_board(fx: float, fy: float, frot: float, lx: float, ly: float) -> Point:
    rx, ry = rot(lx, ly, frot)
    return fx + rx, fy + ry


def dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def norm_angle(a: float) -> float:
    a = a % 360.0
    return 0.0 if abs(a - 360.0) < 1e-6 else a


# --------------------------------------------------------------------------- #
# Arcs / circles
# --------------------------------------------------------------------------- #
def circle_from_3pts(a: Point, b: Point, c: Point) -> Optional[Tuple[float, float, float]]:
    ax, ay = a
    bx, by = b
    cx, cy = c
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    return ux, uy, math.hypot(ax - ux, ay - uy)


def arc_points(start: Point, mid: Point, end: Point, max_seg_deg: float = 10.0) -> List[Point]:
    """Polyline approximation of a 3-point arc (inclusive of both ends)."""
    circ = circle_from_3pts(start, mid, end)
    if circ is None:
        return [start, end]
    cx, cy, r = circ
    a0 = math.atan2(start[1] - cy, start[0] - cx)
    am = math.atan2(mid[1] - cy, mid[0] - cx)
    a1 = math.atan2(end[1] - cy, end[0] - cx)

    def ccw_span(f, t):
        return (t - f) % (2 * math.pi)

    if ccw_span(a0, am) <= ccw_span(a0, a1):
        span = ccw_span(a0, a1)
    else:
        span = -ccw_span(a1, a0)
    if abs(span) < 1e-9:
        span = 2 * math.pi
    n = max(2, int(abs(math.degrees(span)) / max_seg_deg) + 1)
    return [(cx + r * math.cos(a0 + span * i / n), cy + r * math.sin(a0 + span * i / n)) for i in range(n + 1)]


def circle_points(cx: float, cy: float, r: float, n: int = 36) -> List[Point]:
    return [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n + 1)]


def polyline_segments(pts: Sequence[Point], closed: bool = False) -> List[Seg]:
    segs = [(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]) for i in range(len(pts) - 1)]
    if closed and len(pts) > 2 and pts[0] != pts[-1]:
        segs.append((pts[-1][0], pts[-1][1], pts[0][0], pts[0][1]))
    return segs


# --------------------------------------------------------------------------- #
# Distances
# --------------------------------------------------------------------------- #
def pt_seg_dist(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 <= 1e-18:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _orient(ax, ay, bx, by, cx, cy):
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def segs_intersect(s1: Seg, s2: Seg) -> bool:
    ax, ay, bx, by = s1
    cx, cy, dx, dy = s2
    d1 = _orient(cx, cy, dx, dy, ax, ay)
    d2 = _orient(cx, cy, dx, dy, bx, by)
    d3 = _orient(ax, ay, bx, by, cx, cy)
    d4 = _orient(ax, ay, bx, by, dx, dy)
    return (d1 * d2 < 0) and (d3 * d4 < 0)


def seg_seg_dist(s1: Seg, s2: Seg) -> float:
    if segs_intersect(s1, s2):
        return 0.0
    ax, ay, bx, by = s1
    cx, cy, dx, dy = s2
    return min(pt_seg_dist(ax, ay, cx, cy, dx, dy), pt_seg_dist(bx, by, cx, cy, dx, dy),
               pt_seg_dist(cx, cy, ax, ay, bx, by), pt_seg_dist(dx, dy, ax, ay, bx, by))


def point_in_segments(px: float, py: float, segs: Iterable[Seg]) -> bool:
    """Even-odd ray cast against an unordered set of closed-outline segments."""
    inside = False
    for ax, ay, bx, by in segs:
        if (ay > py) != (by > py):
            xint = ax + (py - ay) * (bx - ax) / (by - ay)
            if xint > px:
                inside = not inside
    return inside


def polygon_area(pts: Sequence[Point]) -> float:
    a = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return a / 2.0


# --------------------------------------------------------------------------- #
# Bounding boxes and rectangles
# --------------------------------------------------------------------------- #
class BBox:
    __slots__ = ("x0", "y0", "x1", "y1")

    def __init__(self, x0=math.inf, y0=math.inf, x1=-math.inf, y1=-math.inf):
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1

    @classmethod
    def of_points(cls, pts: Iterable[Point]) -> "BBox":
        b = cls()
        for p in pts:
            b.add(p[0], p[1])
        return b

    def add(self, x: float, y: float) -> None:
        if x < self.x0:
            self.x0 = x
        if x > self.x1:
            self.x1 = x
        if y < self.y0:
            self.y0 = y
        if y > self.y1:
            self.y1 = y

    def merge(self, o: "BBox") -> None:
        if o.valid:
            self.add(o.x0, o.y0)
            self.add(o.x1, o.y1)

    @property
    def valid(self) -> bool:
        return self.x0 <= self.x1 and self.y0 <= self.y1

    @property
    def w(self) -> float:
        return self.x1 - self.x0 if self.valid else 0.0

    @property
    def h(self) -> float:
        return self.y1 - self.y0 if self.valid else 0.0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    def grow(self, d: float) -> "BBox":
        return BBox(self.x0 - d, self.y0 - d, self.x1 + d, self.y1 + d)

    def contains(self, x: float, y: float, margin: float = 0.0) -> bool:
        return self.x0 - margin <= x <= self.x1 + margin and self.y0 - margin <= y <= self.y1 + margin

    def overlaps(self, o: "BBox", margin: float = 0.0) -> bool:
        return not (o.x0 > self.x1 + margin or o.x1 < self.x0 - margin or o.y0 > self.y1 + margin or o.y1 < self.y0 - margin)

    def distance_to(self, o: "BBox") -> float:
        dx = max(0.0, max(o.x0 - self.x1, self.x0 - o.x1))
        dy = max(0.0, max(o.y0 - self.y1, self.y0 - o.y1))
        return math.hypot(dx, dy)

    def corners(self) -> List[Point]:
        return [(self.x0, self.y0), (self.x1, self.y0), (self.x1, self.y1), (self.x0, self.y1)]

    def as_dict(self, nd: int = 3) -> Dict[str, float]:
        return {"x_min": round(self.x0, nd), "y_min": round(self.y0, nd), "x_max": round(self.x1, nd),
                "y_max": round(self.y1, nd), "width": round(self.w, nd), "height": round(self.h, nd)}

    def __repr__(self):
        return "BBox(%.3f, %.3f, %.3f, %.3f)" % (self.x0, self.y0, self.x1, self.y1)


def rect_corners(cx: float, cy: float, w: float, h: float, deg: float) -> List[Point]:
    """Corners of a w x h rectangle centred at (cx, cy) rotated KiCad-style."""
    pts = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
    out = []
    for x, y in pts:
        rx, ry = rot(x, y, deg)
        out.append((cx + rx, cy + ry))
    return out


def point_in_rect(px: float, py: float, cx: float, cy: float, w: float, h: float, deg: float) -> bool:
    lx, ly = rot(px - cx, py - cy, -deg)
    return abs(lx) <= w / 2 and abs(ly) <= h / 2


# --------------------------------------------------------------------------- #
# Spatial hash
# --------------------------------------------------------------------------- #
class Grid:
    """Bucket grid for bbox-indexed items; good enough for a few 100k items."""

    def __init__(self, cell: float = 5.0):
        self.cell = cell
        self.buckets: Dict[Tuple[int, int], List[int]] = {}
        self.items: List[Tuple[BBox, object]] = []

    def _keys(self, b: BBox):
        c = self.cell
        for i in range(int(math.floor(b.x0 / c)), int(math.floor(b.x1 / c)) + 1):
            for j in range(int(math.floor(b.y0 / c)), int(math.floor(b.y1 / c)) + 1):
                yield (i, j)

    def insert(self, bbox: BBox, item: object) -> None:
        idx = len(self.items)
        self.items.append((bbox, item))
        for k in self._keys(bbox):
            self.buckets.setdefault(k, []).append(idx)

    def query(self, bbox: BBox) -> List[object]:
        seen = set()
        out = []
        for k in self._keys(bbox):
            for idx in self.buckets.get(k, ()):
                if idx in seen:
                    continue
                seen.add(idx)
                b, item = self.items[idx]
                if b.overlaps(bbox):
                    out.append(item)
        return out


# --------------------------------------------------------------------------- #
# Outline chaining
# --------------------------------------------------------------------------- #
def chain_segments(segs: Sequence[Seg], tol: float = 0.01) -> List[List[Point]]:
    """Join loose segments into closed loops (best effort)."""
    remaining = list(segs)
    loops: List[List[Point]] = []

    def close(a: Point, b: Point) -> bool:
        return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol

    while remaining:
        s = remaining.pop()
        loop = [(s[0], s[1]), (s[2], s[3])]
        progress = True
        while progress and not close(loop[0], loop[-1]):
            progress = False
            for i, t in enumerate(remaining):
                a, b = (t[0], t[1]), (t[2], t[3])
                if close(loop[-1], a):
                    loop.append(b)
                elif close(loop[-1], b):
                    loop.append(a)
                elif close(loop[0], b):
                    loop.insert(0, a)
                elif close(loop[0], a):
                    loop.insert(0, b)
                else:
                    continue
                remaining.pop(i)
                progress = True
                break
        if len(loop) >= 3:
            if close(loop[0], loop[-1]):
                loop = loop[:-1]
            loops.append(loop)
    loops.sort(key=lambda lp: -abs(polygon_area(lp)))
    return loops
