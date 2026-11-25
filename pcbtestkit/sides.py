"""The board seen from its other side.

Turning a PCB over is a rotation of 180 degrees about the X axis: board point (x, y) is then seen at (x, -y)
and the bottom side faces up. :func:`flipped` returns a light view of a :class:`~pcbtestkit.project.Design`
in which that has been done, so code written for the top side (the solder jigs) works unchanged for the
bottom side: bottom parts appear on top, every shape lands where it physically is, and because it is a
rotation, not a mirror, a jig made from the view is the right way round.

The view carries what the side-independent generators need (outline, footprints, pads, vias, heights,
config); tracks, zones and silkscreen are left out rather than half-converted.
"""
from __future__ import annotations

import copy
import dataclasses
from typing import Any, List

from .geometry import BBox, to_board as _to_board
from .pcb import Board, Footprint, Pad


def swap_layer(layer: str) -> str:
    if layer.startswith("F."):
        return "B." + layer[2:]
    if layer.startswith("B."):
        return "F." + layer[2:]
    return layer


def other(side: str) -> str:
    return "bottom" if side == "top" else "top"


class FlippedFootprint(Footprint):
    """A footprint of the turned-over board. Its local geometry (pads, graphics, 3D model) is the file's;
    only the placement is turned over, so ``to_board`` gives where each local point lies in the view."""

    _orig: Footprint

    def to_board(self, lx: float, ly: float):
        x, y = _to_board(self._orig.x, self._orig.y, self._orig.angle, lx, ly)
        return x, -y

    @property
    def local_flipped(self) -> bool:
        return self._orig.local_flipped

    def pads_local_bbox(self) -> BBox:
        return self._orig.pads_local_bbox()


def _flip_pad(p: Pad) -> Pad:
    return dataclasses.replace(p, y=-p.y, angle=-p.angle, layers=[swap_layer(l) for l in p.layers])


def flip_footprint(fp: Footprint) -> FlippedFootprint:
    g = copy.copy(fp)
    g.__class__ = FlippedFootprint
    g._orig = fp
    g.layer = swap_layer(fp.layer)
    g.y, g.angle = -fp.y, -fp.angle
    g.pads = [_flip_pad(p) for p in fp.pads]
    return g


def flip_board(b: Board) -> Board:
    nb = copy.copy(b)
    nb.footprints = [flip_footprint(fp) for fp in b.footprints]
    nb.edge_segs = [(s[0], -s[1], s[2], -s[3]) + tuple(s[4:]) for s in b.edge_segs]
    nb.vias = [dataclasses.replace(v, y=-v.y) for v in b.vias]
    nb.tracks, nb.zones, nb.silk_lines, nb.silk_texts = [], [], [], []
    nb.copper_layers = list(reversed(b.copper_layers))
    nb._edge_grid = None
    nb._by_ref = None
    return nb


class FlippedDesign:
    """A Design turned over (bottom side up). Everything not overridden here is the original design's."""

    def __init__(self, design: Any):
        self._d = design
        self.board = flip_board(design.board)
        self.up_side = "bottom"          # the real side that faces up in this view

    def __getattr__(self, name: str) -> Any:
        return getattr(self._d, name)

    def components(self, include_virtual: bool = False) -> List[FlippedFootprint]:
        keep = {id(fp) for fp in self._d.components(include_virtual)}
        return [f for f in self.board.footprints if id(f._orig) in keep]

    def height_of(self, fp: Footprint):
        return self._d.height_of(getattr(fp, "_orig", fp))


def view(design: Any, up: str = "top") -> Any:
    """The design with side ``up`` facing up: the design itself for "top", else its turned-over view (cached)."""
    if up == "top":
        return design
    cache = getattr(design, "_flipped_view", None)
    if cache is None:
        cache = FlippedDesign(design)
        design._flipped_view = cache
    return cache


def real_side(design_view: Any, side: str) -> str:
    """The real board side that ``side`` of a (possibly turned-over) view is."""
    return other(side) if getattr(design_view, "up_side", "top") == "bottom" else side
