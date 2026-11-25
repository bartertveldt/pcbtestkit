"""Boards made and tested in batches.

``batch.count`` boards are handled together: the fixture gets one test site per board (each with its own
pogo pins, measurement header, power terminal and programming connector) and the solder jigs hold that many
boards side by side. Site numbers run row by row from the top left, the same in the fixture, the jigs, the
runner and the results. When a jig set for the whole batch does not fit the printer bed (``batch.bed_mm``),
each jig holds fewer boards and the guide says how many sets to print.

Without ``batch`` (count 1), the older ``fixture.panel`` (cols x rows) still sets the fixture sites.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple


def auto_cols(n: int, w: float, h: float, gap: float) -> int:
    """Columns for n items of w x h: as square as possible, then the smallest area, then fewer columns."""
    best = None
    for c in range(1, max(1, n) + 1):
        r = int(math.ceil(n / float(c)))
        W, H = c * w + (c - 1) * gap, r * h + (r - 1) * gap
        key = (round(max(W, H), 3), round(W * H, 3), c)
        if best is None or key < best[0]:
            best = (key, c)
    return best[1]


def layout(cfg: Dict[str, Any], w: float, h: float) -> Dict[str, Any]:
    """{"count", "cols", "rows", "spacing_mm", "source"} for boards of w x h mm."""
    b = cfg.get("batch") or {}
    n = int(b.get("count") or 1)
    if n <= 1:
        panel = (cfg.get("fixture") or {}).get("panel") or {}
        cols, rows = max(1, int(panel.get("cols", 1))), max(1, int(panel.get("rows", 1)))
        return {"count": cols * rows, "cols": cols, "rows": rows, "spacing_mm": float(panel.get("spacing_mm", 10.0)),
                "source": "fixture.panel" if cols * rows > 1 else "single board"}
    gap = float(b.get("spacing_mm", 10.0))
    cols = int(b.get("cols") or 0)
    if cols <= 0:
        cols = auto_cols(n, w, h, gap)
    cols = max(1, min(cols, n))
    return {"count": n, "cols": cols, "rows": int(math.ceil(n / float(cols))), "spacing_mm": gap, "source": "batch"}


def positions(lay: Dict[str, Any]) -> List[Tuple[int, int, int]]:
    """(site number, column, row) of every board, row by row."""
    out = []
    for k in range(lay["count"]):
        out.append((k + 1, k % lay["cols"], k // lay["cols"]))
    return out


def fit_on_bed(n: int, cols: int, sizes: List[Tuple[float, float]], bed: Tuple[float, float]) -> Tuple[int, int, int]:
    """Largest number of boards (<= n) for which every jig (one w x h footprint per board, copies side by side)
    fits the printer bed, turned either way; the batch's own column count first. Returns (boards per jig,
    columns, rows)."""
    bw, bh = float(bed[0]), float(bed[1])
    w0, h0 = max(s[0] for s in sizes), max(s[1] for s in sizes)
    for k in range(n, 0, -1):
        first = min(cols, k) if cols > 0 else auto_cols(k, w0, h0, 0.0)
        for c in sorted(set(range(1, k + 1)), key=lambda c_: (c_ != first, c_)):
            r = int(math.ceil(k / float(c)))
            if all((c * w <= bw and r * h <= bh) or (c * w <= bh and r * h <= bw) for w, h in sizes):
                return k, c, r
    return 1, 1, 1
