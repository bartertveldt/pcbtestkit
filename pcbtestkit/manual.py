"""Decisions without AI: the data behind the KiCad dialog's Rails, Stacked boards and Weak points tabs.

Everything an AI model can decide through the tools can also be decided here, by hand: rail voltages and
tolerances, which rails are tested, boards stacked on this one, and which weak points are accepted. The dialog shows these rows and
writes the user's edits back to pcbtestkit.json with the functions below (no wx here, so it is testable).
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

from .project import Design

DRIVING = ("critical", "major")


def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    t = str(v).strip().replace(",", ".")
    if not t:
        return None
    return float(t)            # ValueError for text that is not a number: the dialog reports it


def rail_rows(design: Design) -> List[Dict[str, Any]]:
    """One row per power rail: what the test plan will use now, plus a suggestion where the voltage is unknown."""
    from .agent import _regulator_hint
    excluded = set(design.cfg["testplan"].get("exclude_nets") or [])
    rows = []
    for n in design.nets_of_kind("power"):
        s = design.net_spec(n)
        hint = None
        if s["nominal_v"] is None and s.get("min_v") is None:
            h = _regulator_hint(design, n)
            if h:
                hint = {"v": h[0], "why": h[1]}
        src = s.get("voltage_source") or ("unknown" if s["nominal_v"] is None and s.get("min_v") is None else "")
        rows.append({"net": n, "test": n not in excluded, "nominal_v": s["nominal_v"], "tolerance_pct": s["tolerance_pct"],
                     "min_v": s.get("min_v"), "max_v": s.get("max_v"), "source": src, "hint": hint})
    return rows


def apply_rail_edits(user: Dict[str, Any], before: List[Dict[str, Any]], after: List[Dict[str, Any]]) -> List[str]:
    """Write changed rows to ``user['nets']`` and the test ticks to ``testplan.exclude_nets``.

    Only rows the user changed are stored, so voltages inferred from net names keep following the names.
    Edited min/max wins over nominal +/- tolerance. Returns problems (bad numbers) to show the user."""
    problems: List[str] = []
    nets = user.setdefault("nets", {})
    tp = user.setdefault("testplan", {})
    excl = [n for n in tp.get("exclude_nets") or []]
    old = {r["net"]: r for r in before}
    for r in after:
        n, o = r["net"], old.get(r["net"], {})
        try:
            vals = {k: _num(r.get(k)) for k in ("nominal_v", "tolerance_pct", "min_v", "max_v")}
            prev = {k: _num(o.get(k)) for k in vals}
        except ValueError:
            problems.append("%s: voltages and tolerance must be numbers" % n)
            continue
        changed = {k for k in vals if (vals[k] is None) != (prev[k] is None) or
                   (vals[k] is not None and abs(vals[k] - prev[k]) > 1e-9)}
        if changed:
            spec = dict(nets.get(n) or {})
            for k in ("nominal", "tolerance_pct", "min", "max"):
                spec.pop(k, None)
            if changed & {"min_v", "max_v"} and vals["min_v"] is not None and vals["max_v"] is not None:
                if vals["min_v"] > vals["max_v"]:
                    problems.append("%s: min %g V is above max %g V" % (n, vals["min_v"], vals["max_v"]))
                    continue
                spec.update({"min": vals["min_v"], "max": vals["max_v"]})
                if vals["nominal_v"] is not None:
                    spec["nominal"] = vals["nominal_v"]
            elif vals["nominal_v"] is not None:
                spec["nominal"] = vals["nominal_v"]
                if vals["tolerance_pct"] is not None:
                    if not 0 < vals["tolerance_pct"] <= 50:
                        problems.append("%s: tolerance %g %% is outside 0..50" % (n, vals["tolerance_pct"]))
                        continue
                    spec["tolerance_pct"] = vals["tolerance_pct"]
            if spec:
                nets[n] = spec
            else:
                nets.pop(n, None)
        if r.get("test", True) and n in excl:
            excl.remove(n)
        elif not r.get("test", True) and n not in excl:
            excl.append(n)
    tp["exclude_nets"] = excl
    return problems


def waiver_rows(findings) -> List[Dict[str, Any]]:
    """Critical/major weak points the user may accept (test-coverage notes are handled in the Rails tab)."""
    return [{"uid": f.uid, "id": f.id, "severity": f.severity, "title": f.title, "refs": list(f.refs or []),
             "waived": bool(f.waived), "reason": f.waived if isinstance(f.waived, str) else ""}
            for f in findings if f.severity in DRIVING and not f.id.startswith("TEST-")]


def apply_waivers(user: Dict[str, Any], rows: List[Dict[str, Any]], checked: List[bool], reason: str) -> None:
    """Replace the waivers of the listed findings with the ticked ones; other waivers are kept."""
    a = user.setdefault("analysis", {})
    listed = {(r["id"], tuple(sorted(r["refs"]))) for r in rows}
    keep = [w for w in a.get("waivers") or [] if (w.get("id"), tuple(sorted(w.get("refs") or []))) not in listed]
    today = datetime.date.today().isoformat()
    for r, on in zip(rows, checked):
        if on:
            why = (r.get("reason") if r.get("waived") and r.get("reason") else (reason or "").strip()) or "accepted by the designer"
            keep.append({"id": r["id"], "refs": r["refs"], "reason": why[:300], "by": "dialog", "date": today})
    a["waivers"] = keep


STACK_COLS = ("use", "name", "refs", "side", "outline", "height_mm", "fitted_at_test", "note")


def stack_rows(design: Design) -> List[Dict[str, Any]]:
    """The stacked boards in the config (ticked), then parts that may carry one (not ticked) with the reason."""
    from .stack import candidates, resolve
    fmt = lambda v: "" if v is None else ("%g" % v)
    rows = []
    resolved = {st["name"]: st for st in resolve(design)}
    for i, e in enumerate(design.cfg.get("stack") or []):
        if not isinstance(e, dict):
            continue
        r = resolved.get(str(e.get("name") or "stack%d" % (i + 1))) or {}
        o = e.get("outline")
        note = "; ".join(x for x in (r.get("outline_source", "") if str(r.get("outline_source", "")).startswith("estimate") else "",
                                     r.get("height_source", "") if str(r.get("height_source", "")).startswith("estimate") else "")
                         if x)
        rows.append({"use": True, "name": str(e.get("name") or ""), "refs": ", ".join(e.get("refs") or []),
                     "side": e.get("side") or r.get("side") or "",
                     "outline": ", ".join(fmt(v) for v in o) if isinstance(o, list) else "",
                     "height_mm": fmt(e.get("height_mm")), "fitted_at_test": e.get("fitted_at_test", True) is not False,
                     "note": note})
    for c in candidates(design):
        rows.append({"use": False, "name": "", "refs": c["ref"], "side": c["side"], "outline": "", "height_mm": "",
                     "fitted_at_test": True, "note": "%s; part outline %s, %g mm high" % (
                         c["why"], ", ".join("%g" % v for v in c["outline"]), c["height_mm"])})
    return rows


def apply_stack_edits(user: Dict[str, Any], rows: List[Dict[str, Any]], refs_known) -> List[str]:
    """Write the ticked rows to ``user['stack']`` (replacing it). Empty outline/height = estimated from the parts.
    Returns problems (bad numbers, unknown parts) to show the user; nothing is written when there are any."""
    from .stack import validate_entry
    problems: List[str] = []
    out, names = [], set()
    for i, r in enumerate(rows):
        if not r.get("use"):
            continue
        refs = [x for x in str(r.get("refs") or "").replace(";", ",").replace(" ", ",").split(",") if x]
        name = str(r.get("name") or "").strip() or (refs[0] + "-board" if refs else "stack%d" % (i + 1))
        e: Dict[str, Any] = {"name": name, "refs": refs}
        side = str(r.get("side") or "").strip().lower()
        if side:
            e["side"] = side
        try:
            txt = str(r.get("outline") or "").replace(";", ",").strip()
            if txt:
                e["outline"] = [float(v.strip().replace(" ", "")) for v in txt.split(",") if v.strip()]
                if len(e["outline"]) != 4:
                    raise ValueError
            h = _num(r.get("height_mm"))
            if h is not None:
                e["height_mm"] = h
        except ValueError:
            problems.append("%s: outline must be four numbers x0, y0, x1, y1 (mm) and height a number" % name)
            continue
        e["fitted_at_test"] = bool(r.get("fitted_at_test", True))
        if name in names:
            problems.append("two stacked boards are called %s" % name)
            continue
        names.add(name)
        bad = validate_entry(e, refs_known)
        if bad:
            problems.append(bad)
            continue
        out.append(e)
    if not problems:
        user["stack"] = out
    return problems
