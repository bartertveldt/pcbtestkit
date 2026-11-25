"""Boards stacked on the board under test: daughter boards, shields, displays on headers or board-to-board
connectors, modules drawn as one footprint.

A stacked board is a volume over (or under) part of the DUT. When it is fitted during the test, nothing can be
probed under it on that side and the fixture plate needs a window for it; the test planner treats it like a
very large tall part, and with ``testplan.probe_side = "both"`` the nets it covers are probed from the other
side. When it is fitted after the test, the board is tested bare and the test sequence ends with fitting it.

Config (``stack`` in pcbtestkit.json), one entry per stacked board::

    {"name": "display", "refs": ["J6"], "side": "top", "outline": [x0, y0, x1, y1], "height_mm": 14,
     "fitted_at_test": true}

``outline`` is the stacked board's shadow in DUT board coordinates (mm, as in KiCad); without it, the
outline of the footprints in ``refs`` is used (right for a board drawn as a footprint, too small for a board on
connectors: then it is marked as an estimate). ``height_mm`` is the top of the stack above the DUT surface.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import classify as C
from .geometry import BBox

ASSUMED_BOARD_MM = 1.6
ASSUMED_PARTS_MM = 3.0


def resolve(design: Any) -> List[Dict[str, Any]]:
    """The configured stacked boards with outline, side, height and their sources (problems in ``problems``)."""
    out: List[Dict[str, Any]] = []
    by_ref = design.board.by_ref
    for i, st in enumerate(design.cfg.get("stack") or []):
        if not isinstance(st, dict):
            continue
        name = str(st.get("name") or "stack%d" % (i + 1))
        refs = [r for r in (st.get("refs") or []) if r in by_ref]
        problems = ["unknown part %s" % r for r in (st.get("refs") or []) if r not in by_ref]
        side = st.get("side") if st.get("side") in ("top", "bottom") else (by_ref[refs[0]].side if refs else "top")
        o = st.get("outline")
        bb = BBox()
        if isinstance(o, (list, tuple)) and len(o) == 4 and all(isinstance(v, (int, float)) for v in o):
            bb = BBox(min(o[0], o[2]), min(o[1], o[3]), max(o[0], o[2]), max(o[1], o[3]))
            o_src = "config"
        else:
            for r in refs:
                bb.merge(by_ref[r].courtyard_bbox())
            board_fp = any((C.stacking_hint(by_ref[r]) or "").startswith("footprint of a board") for r in refs)
            o_src = "footprint outline" if board_fp else "estimate: outline of %s only" % ", ".join(refs)
        if not bb.valid or bb.w <= 0 or bb.h <= 0:
            problems.append("no outline (give stack[].outline or refs)")
            out.append({"name": name, "refs": refs, "side": side, "valid": False, "problems": problems})
            continue
        h = st.get("height_mm")
        if isinstance(h, (int, float)) and h > 0:
            height, h_src = float(h), "config"
        else:
            hs = [design.height_of(by_ref[r])[0] for r in refs]
            board_fp = o_src == "footprint outline"
            height = (max(hs) if board_fp else max(hs) + ASSUMED_BOARD_MM + ASSUMED_PARTS_MM) if hs else 15.0
            h_src = "3D model of %s" % ", ".join(refs) if board_fp else "estimate: connector height + board + parts"
        out.append({"name": name, "refs": refs, "side": side, "valid": True, "bbox": bb,
                    "outline": [round(bb.x0, 3), round(bb.y0, 3), round(bb.x1, 3), round(bb.y1, 3)],
                    "outline_source": o_src, "height_mm": round(height, 2), "height_source": h_src,
                    "fitted_at_test": st.get("fitted_at_test", True) is not False, "problems": problems})
    return out


def candidates(design: Any) -> List[Dict[str, Any]]:
    """Parts that may carry a stacked board and are not covered by a stack entry yet."""
    used = {r for st in design.cfg.get("stack") or [] if isinstance(st, dict) for r in st.get("refs") or []}
    out = []
    for fp in design.components():
        if fp.ref in used or fp.dnp:
            continue
        why = C.stacking_hint(fp)
        if why:
            out.append({"ref": fp.ref, "value": fp.value, "side": fp.side, "why": why,
                        "outline": [round(v, 2) for v in _tuple(fp.courtyard_bbox())],
                        "height_mm": round(design.height_of(fp)[0], 2)})
    return out


def _tuple(b: BBox):
    return (b.x0, b.y0, b.x1, b.y1)


def brief(st: Dict[str, Any]) -> Dict[str, Any]:
    """JSON-safe summary of a resolved entry."""
    return {k: v for k, v in st.items() if k != "bbox"}


def validate_entry(e: Any, refs_known) -> Optional[str]:
    """Problem with one user entry (dialog / tool input), or None."""
    if not isinstance(e, dict):
        return "each stacked board must be an object"
    if e.get("side") not in (None, "top", "bottom"):
        return "%s: side must be top or bottom" % e.get("name", "?")
    o = e.get("outline")
    if o is not None:
        if not (isinstance(o, (list, tuple)) and len(o) == 4 and all(isinstance(v, (int, float)) for v in o)):
            return "%s: outline must be [x0, y0, x1, y1] in mm" % e.get("name", "?")
        if abs(o[2] - o[0]) < 1.0 or abs(o[3] - o[1]) < 1.0:
            return "%s: outline is smaller than 1 x 1 mm" % e.get("name", "?")
    h = e.get("height_mm")
    if h is not None and not (isinstance(h, (int, float)) and 0.5 <= h <= 150):
        return "%s: height_mm must be 0.5..150" % e.get("name", "?")
    bad = [r for r in e.get("refs") or [] if r not in refs_known]
    if bad:
        return "%s: unknown part(s) %s" % (e.get("name", "?"), ", ".join(bad))
    if not e.get("refs") and o is None:
        return "%s: give refs (the connectors or the board footprint) or an outline" % e.get("name", "?")
    return None
