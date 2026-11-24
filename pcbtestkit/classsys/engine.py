"""Generic class-system engine.

A *class system* is a JSON definition (see ``systems/*.json`` and
``pcbtestkit classes template``) with:

* ``dimensions``: ordered axes such as Eurocircuits' pattern class (3..9) and
  drill class (A..E), a fab's capability tiers, or IPC-6012 class 1/2/3. Each
  level states limits on catalog metrics. ``choose: "cheapest"`` (default)
  means levels are listed from easiest/cheapest to finest and the engine picks
  the first level the board meets; ``choose: "user"`` means the level is a
  choice (environment, product class) and the engine only reports which pass.
  The validator warns when a later level is coarser than an earlier one;
  ``"monotonic": true`` makes that an error, ``"monotonic_exempt": [metrics]``
  skips named metrics, and ``"monotonic": false`` skips the check for levels
  that are alternatives in price order rather than a fineness ladder (stack-ups).
* ``always``: limits that apply whatever class is ordered (process limits).
* ``hole_basis``: whether rings, hole clearances and aspect ratio are measured
  from the finished hole or from the drill tool (finished + offset).
* ``parameters`` and ``tables``: named numbers for expressions, and lookup
  tables (e.g. spacing per voltage) for per-item limits.

A rule is a value, or a list of clauses; per measured item the first clause
whose ``when`` (context) and ``items`` (item fields) match supplies the limit:

    "track_outer": 0.1
    "track_outer": [{"when": {"layers": {"max": 2}}, "value": "4mil"}, {"value": "3.5mil"}]
    "ring": [{"items": {"finished": {"max": 0.45}}, "value": 0.2}, {"value": 0.3}]
    "voltage_gap_outer": {"lookup": "spacing", "column": "B2"}
"""
from __future__ import annotations

import bisect
import copy
import itertools
import re
from typing import Any, Dict, List, Optional, Tuple

from . import catalog as K
from . import values as V

DEFAULT_BASIS = {"pth_offset": 0.0, "npth_offset": 0.0, "ring": "finished", "hole_clearance": "finished",
                 "hole_to_hole": "finished", "aspect": "finished"}
DRIVING = ("critical", "major")      # severities that decide the class and whether the board fits


class SystemError_(ValueError):
    pass


# --------------------------------------------------------------------------- #
# Definitions
# --------------------------------------------------------------------------- #
class Clause:
    __slots__ = ("when", "items", "value", "severity", "note", "source")

    def __init__(self, d: Dict[str, Any]):
        self.when = d.get("when")
        self.items = d.get("items")
        self.value = d.get("lookup") and {"lookup": d["lookup"], "column": d.get("column")} or d.get("value")
        self.severity = d.get("severity")
        self.note = d.get("note")
        self.source = d.get("source")


def compile_rule(raw: Any) -> List[Clause]:
    if isinstance(raw, list) and raw and all(isinstance(c, dict) for c in raw):
        return [Clause(c) for c in raw]
    if isinstance(raw, dict) and ("value" in raw or "lookup" in raw):
        return [Clause(raw)]
    return [Clause({"value": raw})]


class Level:
    def __init__(self, d: Dict[str, Any]):
        self.raw = d
        self.id = str(d["id"])
        self.label = d.get("label", self.id)
        self.aliases = [str(a) for a in d.get("aliases", [])]
        self.rules: Dict[str, Any] = d.get("limits", {}) or {}
        self.offers = d.get("offers")
        self.note = d.get("note", "")
        self.requires = d.get("requires")


class Dimension:
    def __init__(self, d: Dict[str, Any]):
        self.raw = d
        self.id = str(d["id"])
        self.label = d.get("label", self.id)
        self.choose = d.get("choose", "cheapest")
        self.default = d.get("default")
        self.levels = [Level(l) for l in d.get("levels", [])]

    def level(self, lid: str) -> Optional[Level]:
        n = _norm(lid)
        for l in self.levels:
            if _norm(l.id) == n or n in (_norm(a) for a in l.aliases):
                return l
        return None


def _norm(s: Any) -> str:
    return re.sub(r"[\s_\-./:]+", "", str(s)).upper()


class System:
    def __init__(self, raw: Dict[str, Any], origin: str = "built-in"):
        self.raw = raw
        self.origin = origin
        self.id = str(raw.get("id", ""))
        self.name = raw.get("name", self.id)
        self.short = raw.get("short") or raw.get("vendor") or self.name
        self.status = raw.get("status", "draft")
        self.kind = raw.get("kind", "fab-classes")
        self.category = raw.get("category", "manufacturing")
        self.finding_id = raw.get("finding_id", "MFG-TECH-CLASS")
        self.tolerance = float(raw.get("tolerance_mm", 0.0005))
        self.basis = dict(DEFAULT_BASIS, **(raw.get("hole_basis") or {}))
        self.params = {k: (v.get("default") if isinstance(v, dict) else v) for k, v in (raw.get("parameters") or {}).items()}
        self.tables = raw.get("tables") or {}
        self.metrics = dict(K.METRICS)
        for mid, md in (raw.get("metrics") or {}).items():
            self.metrics[mid] = dict({"where": {}, "unit": "mm", "severity": "major", "kind": "min", "doc": ""}, **md)
        self.aliases = dict(K.ALIASES, **(raw.get("aliases") or {}))
        self.dims = [Dimension(d) for d in raw.get("dimensions", [])]
        self.always: Dict[str, Any] = raw.get("always", {}) or {}
        self.offer_labels = raw.get("offer_labels") or {}
        self.class_format = raw.get("class_format") or "-".join("{%s}" % d.id for d in self.dims)
        self._expanded: Dict[int, Tuple[Dict[str, Any], Dict[str, Tuple[List[Clause], str]]]] = {}

    # ---- rules ---------------------------------------------------------------
    def expand(self, rules: Dict[str, Any]) -> Dict[str, Tuple[List[Clause], str]]:
        """Rule keys (metrics or aliases) -> {metric: (clauses, written-as)}; specific beats general."""
        key = id(rules)
        hit = self._expanded.get(key)
        if hit is not None and hit[0] is rules:      # the stored reference keeps the id from being reused
            return hit[1]
        out: Dict[str, Tuple[List[Clause], str]] = {}
        al = [k for k in rules if k in self.aliases and k not in self.metrics]
        for k in sorted(al, key=lambda a: -len(self.aliases[a])):
            for m in self.aliases[k]:
                out[m] = (compile_rule(rules[k]), k)
        for k in rules:
            if k in self.metrics:
                out[k] = (compile_rule(rules[k]), k)
        self._expanded[key] = (rules, out)
        return out

    # ---- classes ---------------------------------------------------------------
    def format_class(self, levels: Dict[str, str]) -> str:
        try:
            return self.class_format.format(**levels)
        except (KeyError, IndexError):
            return "-".join(str(levels.get(d.id, "?")) for d in self.dims)

    def all_classes(self, limit: int = 2000) -> List[str]:
        combos = itertools.product(*[[l.id for l in d.levels] for d in self.dims])
        out = []
        for c in combos:
            out.append(self.format_class(dict(zip([d.id for d in self.dims], c))))
            if len(out) >= limit:
                break
        return out

    def parse_class(self, text: Any) -> Dict[str, str]:
        """'6C' / {'pattern': 6, 'drill': 'C'} / 'standard' -> {dim: level id}."""
        if isinstance(text, dict):
            out = {}
            for d in self.dims:
                if d.id not in text:
                    if d.default is None:
                        raise SystemError_("%s: missing %s" % (self.id, d.id))
                    out[d.id] = str(d.default)
                    continue
                lv = d.level(str(text[d.id]))
                if lv is None:
                    raise SystemError_("%s: %s has no level %r (%s)" % (self.id, d.id, text[d.id], ", ".join(l.id for l in d.levels)))
                out[d.id] = lv.id
            return out
        n = _norm(text)
        if not n:
            raise SystemError_("empty class for %s" % self.id)
        if len(self.dims) == 1:
            lv = self.dims[0].level(str(text))
            if lv is not None:
                return {self.dims[0].id: lv.id}
        names = [[(l.id, [l.id] + l.aliases) for l in d.levels] for d in self.dims]
        for combo in itertools.product(*names):
            for spell in itertools.product(*[c[1] for c in combo]):
                lv = dict(zip([d.id for d in self.dims], spell))
                if _norm(self.format_class(lv)) == n:
                    return dict(zip([d.id for d in self.dims], [c[0] for c in combo]))
        raise SystemError_("%r is not a class of %s; examples: %s" % (text, self.id, ", ".join(self.all_classes(8))))

    def defaults(self) -> Optional[Dict[str, str]]:
        if all(d.default is not None for d in self.dims):
            return {d.id: str(d.default) for d in self.dims}
        return None

    def summary(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "short": self.short, "status": self.status, "kind": self.kind, "category": self.category,
                "vendor": self.raw.get("vendor"), "source": self.raw.get("source"), "verified": self.raw.get("verified"),
                "origin": self.origin,
                "dimensions": [{"id": d.id, "label": d.label, "choose": d.choose, "default": d.default,
                                "levels": [l.id for l in d.levels]} for d in self.dims],
                "class_format": self.class_format, "example": (self.all_classes(1) or [""])[0]}


# --------------------------------------------------------------------------- #
# Measured values per metric
# --------------------------------------------------------------------------- #
class Values:
    """Lazily computed, sorted (value, item) lists per metric for one system + board."""

    def __init__(self, system: System, meas: Optional[Dict[str, Any]], ctx: Dict[str, Any],
                 synthetic: Optional[Dict[str, Any]] = None):
        self.s, self.meas, self.ctx = system, meas or {}, ctx
        self.cache: Dict[str, Optional[List[Tuple[float, Dict[str, Any]]]]] = {}
        self.synthetic = synthetic

    def field(self, it: Dict[str, Any], name: str) -> Any:
        try:
            return self._field(it, name)
        except (KeyError, TypeError):
            return None                  # e.g. a synthetic test item without the raw fields

    def _field(self, it: Dict[str, Any], name: str) -> Any:
        b = self.s.basis
        if name == "ring":
            return it["ring_f"] - (float(b["pth_offset"]) / 2 if b["ring"] == "toolsize" else 0.0)
        if name == "tool":
            return it["finished"] + float(b["pth_offset"] if it.get("kind") in ("via", "pth") else b["npth_offset"])
        if name == "aspect":
            t = self.ctx.get("thickness_mm") or 0.0
            tool = it["finished"] + (float(b["pth_offset"]) if b["aspect"] == "toolsize" else 0.0)
            return t / tool if tool > 0 else None
        if name == "hole_clear":
            off = float(b["pth_offset"] if it.get("kind") in ("via", "pth") else b["npth_offset"])
            return it["cdist"] - it["finished"] / 2 - (off / 2 if b["hole_clearance"] == "toolsize" else 0.0)
        if name == "hole_gap":
            if b["hole_to_hole"] != "toolsize":
                return it["gap"]
            off = lambda k: float(b["pth_offset"] if k in ("via", "pth") else b["npth_offset"])
            return it["gap"] - off(it.get("ka")) / 2 - off(it.get("kb")) / 2
        if name == "dv":
            if "dv" in it:
                return it["dv"]
            vm = self.ctx.get("_net_voltage") or {}
            return abs(float(vm.get(it.get("na"), 0.0) or 0.0) - float(vm.get(it.get("nb"), 0.0) or 0.0))
        return it.get(name)

    def _filtered(self, stream: str, where: Dict[str, Any], items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Items matching a catalog filter; shared by every system evaluated on the same measurement."""
        key = (stream, repr(sorted(where.items())))
        cache = self.meas.setdefault("_filter_cache", {}) if isinstance(self.meas, dict) else {}
        hit = cache.get(key)
        if hit is not None:
            return hit
        simple = all(isinstance(v, (bool, str)) for v in where.values())
        if simple:
            pairs = list(where.items())
            out = [it for it in items if all(it.get(k) == v for k, v in pairs)]
        else:
            out = [it for it in items if V.matches(where, it)]
        cache[key] = out
        return out

    def get(self, mid: str) -> Optional[List[Tuple[float, Dict[str, Any]]]]:
        if mid in self.cache:
            return self.cache[mid]
        md = self.s.metrics.get(mid)
        out: Optional[List[Tuple[float, Dict[str, Any]]]] = None
        if self.synthetic is not None:
            raw = self.synthetic.get(mid)
            if raw is not None:
                lst = raw if isinstance(raw, list) else [raw]
                out = [(float(v["value"]), dict(v)) if isinstance(v, dict) else (float(v), {"value": float(v)}) for v in lst]
        elif md is not None and md.get("stream"):
            st = md["stream"]
            if st in ("board", "features"):
                items = [self.meas.get(st) or {}]
            else:
                items = (self.meas.get("streams") or {}).get(st, [])
            where = md.get("where") or {}
            if where and st not in ("board", "features"):
                items = self._filtered(st, where, items)
            out = []
            for it in items:
                v = self.field(it, md["value"])
                if v is None or isinstance(v, bool) and md["kind"] != "feature":
                    continue
                out.append((float(v), it))
        if out is not None:
            out.sort(key=lambda p: -p[0] if (md or {}).get("kind") == "max" else p[0])
        self.cache[mid] = out
        return out


# --------------------------------------------------------------------------- #
# Checking
# --------------------------------------------------------------------------- #
def _lookup(system: System, spec: Dict[str, Any], item: Dict[str, Any], vals: Values) -> float:
    t = system.tables.get(spec["lookup"])
    if t is None:
        raise SystemError_("%s: unknown table %r" % (system.id, spec["lookup"]))
    col = spec.get("column")
    key = vals.field(item, t.get("key", "dv"))
    key = float(key or 0.0)
    for row in t["rows"]:
        if key <= float(row["upto"]) + 1e-9:
            return V.number(row[col], vals.ctx)
    b = t.get("beyond") or {}
    base = V.number(t["rows"][-1][col], vals.ctx)
    per = float((b.get("per_unit") or {}).get(col, 0.0))
    return base + per * (key - float(b.get("from", t["rows"][-1]["upto"])))


def _limit(system: System, md: Dict[str, Any], clause: Clause, item: Dict[str, Any], vals: Values) -> Any:
    v = clause.value
    if isinstance(v, dict) and "lookup" in v:
        return _lookup(system, v, item, vals)
    if md["kind"] == "allowed":
        lst = v if isinstance(v, list) else [v]
        return [V.number(x, vals.ctx, md.get("unit", "mm")) for x in lst]
    if md["kind"] == "feature":
        return v
    if item and V.is_expression(v):
        scope = dict(vals.ctx)
        for f in K.FIELDS.get(md.get("stream") or "", []):
            fv = vals.field(item, f) if f in K.DERIVED else item.get(f)
            if V.finite(fv):
                scope[f] = fv
        return V.number(v, scope, md.get("unit", "mm"))
    return V.number(v, vals.ctx, md.get("unit", "mm"))


def _fails(kind: str, value: float, lim: Any, tol: float) -> bool:
    if kind == "min":
        return value < lim - tol
    if kind == "max":
        return value > lim + tol
    if kind == "allowed":
        return not any(abs(value - a) <= max(tol, 0.01) for a in lim)
    if kind == "feature":
        if lim is True or lim is None:
            return False
        if lim is False:
            return value > 0
        return value > float(lim)
    return False


def _uniform(clauses: List[Clause], fields: List[str]) -> bool:
    if len(clauses) != 1 or clauses[0].items:
        return False
    v = clauses[0].value
    if isinstance(v, dict) and "lookup" in v:
        return False
    return not (V.is_expression(v) and V.expr_names(v) & set(fields))


def check_metric(system: System, mid: str, clauses: List[Clause], vals: Values,
                 keep: int = K.ITEM_LIMIT, count_only: bool = False) -> Optional[Dict[str, Any]]:
    """Violations of one metric under one rule. None = rule not applicable / no data."""
    md = system.metrics[mid]
    kind = md["kind"]
    if kind == "manual":
        return None
    data = vals.get(mid)
    if data is None or not data:
        return None
    ctx = vals.ctx
    tol = system.tolerance if md.get("unit", "mm") == "mm" else 1e-9
    active = [c for c in clauses if V.matches(c.when, ctx)]
    if not active:
        return None
    bad: List[Tuple[float, Dict[str, Any], Any, Clause]] = []
    checked = 0
    limits_seen: List[Any] = []
    if _uniform(active, K.FIELDS.get(md.get("stream") or "", [])) and kind in ("min", "max"):
        c = active[0]
        lim = _limit(system, md, c, {}, vals)
        limits_seen.append(lim)
        keys = [p[0] for p in data] if kind == "min" else [-p[0] for p in data]
        cut = bisect.bisect_left(keys, (lim - tol) if kind == "min" else -(lim + tol))
        checked = len(data)
        bad = [(v, it, lim, c) for v, it in data[:cut]]
    else:
        for v, it in data:
            c = next((c for c in active if c.items is None or V.matches(c.items, dict(it, value=v))), None)
            if c is None:
                continue
            checked += 1
            lim = _limit(system, md, c, it, vals)
            if lim not in limits_seen and len(limits_seen) < 6:
                limits_seen.append(lim)
            if _fails(kind, v, lim, tol):
                bad.append((v, it, lim, c))
    if not checked:
        return None
    res = {"metric": mid, "label": md["label"], "kind": kind, "count": len(bad), "checked": checked,
           "worst": round(data[0][0], 4), "limit": _fmt_lim(limits_seen[0]) if len(limits_seen) == 1 else None,
           "limits": [_fmt_lim(l) for l in limits_seen] if len(limits_seen) > 1 else None}
    if bad:
        res["severity"] = next((b[3].severity for b in bad if b[3].severity), None) or md.get("severity", "major")
        res["shortfall"] = _shortfall(kind, bad)
    if bad and not count_only:
        notes = sorted({b[3].note for b in bad if b[3].note})
        if notes:
            res["note"] = "; ".join(notes)
        if kind == "feature":
            res["items"] = [dict(i, value=1) for i in (vals.meas.get("feature_items") or {}).get(md["value"], [])[:keep]]
        elif md.get("stream") == "pair_gaps":
            res["items"] = [dict(_item(v, it, lim), dv=round(vals.field(it, "dv"), 1)) for v, it, lim, _ in bad[:keep]]
        else:
            res["items"] = [_item(v, it, lim) for v, it, lim, _ in bad[:keep]]
    return res


def _shortfall(kind: str, bad: List[Tuple[float, Dict[str, Any], Any, Clause]]) -> float:
    """Largest relative miss of the failing items (0.2 = 20 % under a minimum); 1.0 for features and lists."""
    worst = 0.0
    for v, _, lim, _ in bad:
        if kind not in ("min", "max") or not V.finite(lim) or not lim:
            return 1.0
        worst = max(worst, ((lim - v) if kind == "min" else (v - lim)) / abs(lim))
    return round(worst, 6)


def _fmt_lim(l: Any) -> Any:
    if isinstance(l, float):
        return round(l, 4)
    if isinstance(l, list):
        return [round(x, 4) if isinstance(x, float) else x for x in l]
    return l


def _item(v: float, it: Dict[str, Any], lim: Any) -> Dict[str, Any]:
    d = {"value": round(v, 4), "limit": _fmt_lim(lim)}
    for k in ("x", "y", "layer", "net", "a", "b", "label", "to", "text", "na", "nb", "dv"):
        if it.get(k) not in (None, ""):
            d[k] = round(it[k], 3) if isinstance(it[k], float) else it[k]
    return d


def _run_rules(system: System, rules: Dict[str, Any], vals: Values, count_only=False, keep=K.ITEM_LIMIT):
    """{metric: result} for every metric the rules name that has data."""
    out = {}
    for mid, (clauses, written) in system.expand(rules).items():
        r = check_metric(system, mid, clauses, vals, keep=keep, count_only=count_only)
        if r is not None:
            r["rule"] = written
            out[mid] = r
    return out


def _manual(system: System, rules: Dict[str, Any], ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for mid, (clauses, _) in system.expand(rules).items():
        if system.metrics[mid]["kind"] != "manual":
            continue
        c = next((c for c in clauses if V.matches(c.when, ctx)), None)
        if c is not None:
            out.append({"metric": mid, "label": system.metrics[mid]["label"], "requirement": c.value,
                        "note": c.note or ""})
    return out


def _offers(system: System, levels: List[Level]) -> List[str]:
    sets = []
    for l in levels:
        if l.offers in (None, "all"):
            continue
        sets.append(set(l.offers.split() if isinstance(l.offers, str) else l.offers))
    if not system.offer_labels:
        return sorted(set.intersection(*sets)) if sets else []
    keys = list(system.offer_labels)
    allowed = set(keys).intersection(*sets) if sets else set(keys)
    return [system.offer_labels[k] for k in keys if k in allowed]


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #
def evaluate(system: System, meas: Optional[Dict[str, Any]], ctx: Dict[str, Any], target: Any = None,
             synthetic: Optional[Dict[str, Any]] = None, keep: int = K.ITEM_LIMIT) -> Dict[str, Any]:
    ctx = dict(system.params, **{k: v for k, v in ctx.items() if v is not None})
    vals = Values(system, meas, ctx, synthetic)
    out: Dict[str, Any] = {"system": system.id, "name": system.name, "short": system.short, "status": system.status,
                           "source": system.raw.get("source"), "verified": system.raw.get("verified"),
                           "category": system.category,
                           "context": {k: ctx.get(k) for k in list(K.CONTEXT_KEYS) + list(system.params) if ctx.get(k) is not None}}
    # ---- process limits (any class) ----------------------------------------------
    proc = _run_rules(system, system.always, vals, keep=keep)
    proc_bad = {m: r for m, r in proc.items() if r["count"]}
    proc_blocking = {m: r for m, r in proc_bad.items() if r.get("severity") in DRIVING}
    # ---- required class -------------------------------------------------------------
    req_levels: Dict[str, Optional[str]] = {}
    drivers: List[Dict[str, Any]] = []
    passing: Dict[str, List[str]] = {}
    beyond: List[str] = []
    chosen: List[Level] = []
    for d in system.dims:
        eligible = [l for l in d.levels if V.matches(l.requires, ctx)]
        fails_by_level: List[Tuple[Level, Dict[str, Any]]] = []
        for l in eligible:
            res = _run_rules(system, l.rules, vals, count_only=True)
            # minor items (silkscreen, mask webs) are reported but do not push the board into a finer class
            fails_by_level.append((l, {m: r for m, r in res.items() if r["count"] and r.get("severity") in DRIVING}))
        passing[d.id] = [l.id for l, f in fails_by_level if not f]
        if d.choose != "cheapest":
            lv = d.level(str(d.default)) if d.default is not None else None
            req_levels[d.id] = lv.id if lv else None
            if lv:
                chosen.append(lv)
            continue
        idx = next((i for i, (l, f) in enumerate(fails_by_level) if not f), None)
        if idx is None:
            req_levels[d.id] = None
            beyond.append(d.id)
            # report the closest level: board-level mismatches (layer count, thickness, copper) count as far,
            # then fewest failing metrics, then the smallest total shortfall - i.e. what the
            # designer can change to reach it. On a fineness ladder the shortfall step changes nothing (finer
            # levels never have larger limits); on alternatives (stack-ups) it skips levels with coarser limits.
            def distance(lf):
                return (sum(1 for m in lf[1] if system.metrics[m].get("stream") == "board"), len(lf[1]),
                        round(sum(r.get("shortfall", 1.0) for r in lf[1].values()), 6))
            # ties: the finest level on a ladder, the cheapest of equal alternatives (monotonic: false)
            order = fails_by_level if d.raw.get("monotonic") is False else list(reversed(fails_by_level))
            prev = min(order, key=distance) if order else None
        else:
            req_levels[d.id] = fails_by_level[idx][0].id
            chosen.append(fails_by_level[idx][0])
            prev = fails_by_level[idx - 1] if idx > 0 else None
        if prev:
            for m, r in prev[1].items():
                drivers.append({"dimension": d.id, "level": prev[0].id, "metric": m, "label": r["label"],
                                "measured": r["worst"], "limit": r["limit"] if r["limit"] is not None else r["limits"],
                                "count": r["count"]})
    req: Dict[str, Any] = {"levels": req_levels, "drivers": drivers, "fits": not beyond and not proc_blocking}
    if not beyond and all(v is not None for v in req_levels.values()):
        req["class"] = system.format_class(req_levels)
        req["services"] = _offers(system, chosen)
        notes = [l.note for l in chosen if l.note]
        if notes:
            req["notes"] = notes
    else:
        req["class"] = None
        req["beyond"] = beyond
    if any(d.choose != "cheapest" for d in system.dims):
        req["passing"] = passing
    out["required"] = req
    if proc:
        out["process"] = {"ok": not proc_bad, "violations": proc_bad,
                          "checked": sorted(proc)}
    # ---- target class -----------------------------------------------------------------------
    tl = system.parse_class(target) if target not in (None, "") else None
    if tl is not None:
        levels = [d.level(tl[d.id]) for d in system.dims]
        bad: Dict[str, Any] = {}
        limits: Dict[str, Any] = {}
        for d, l in zip(system.dims, levels):
            if not V.matches(l.requires, ctx):
                bad["_requires_" + d.id] = {"metric": "_requires", "label": "%s %s needs %s" % (d.label, l.id, l.requires),
                                            "count": 1, "severity": "major", "items": []}
            res = _run_rules(system, l.rules, vals, keep=keep)
            for m, r in res.items():
                limits[m] = {"label": r["label"], "limit": r["limit"] if r["limit"] is not None else r["limits"],
                             "worst": r["worst"], "ok": not r["count"], "from": "%s %s" % (d.id, l.id)}
                if r["count"]:
                    bad[m] = r
        for m, r in proc.items():
            limits.setdefault(m, {"label": r["label"], "limit": r["limit"] if r["limit"] is not None else r["limits"],
                                  "worst": r["worst"], "ok": not r["count"], "from": "process"})
        tclass = system.format_class(tl)
        blocking = [m for m, r in list(bad.items()) + list(proc_bad.items()) if r.get("severity") in DRIVING]
        out["target"] = {"class": tclass, "levels": tl, "ok": not blocking, "violations": bad,
                         "limits": limits, "services": _offers(system, levels),
                         "manual": _manual(system, dict(system.always, **{k: v for l in levels for k, v in l.rules.items()}), ctx)}
        out["target"]["notes"] = [l.note for l in levels if l.note]
    # ---- what was measured -----------------------------------------------------------------
    used = set(system.expand(system.always))
    for d in system.dims:
        for l in d.levels:
            used |= set(system.expand(l.rules))
    meas_out = {}
    for m in sorted(used):
        data = vals.get(m)
        if data:
            meas_out[m] = round(data[0][0], 4)
    out["measured"] = meas_out
    return out


def limits_for(system: System, levels: Dict[str, str], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Uniform limits (first matching clause without item filters) of a class in a context."""
    ctx = dict(system.params, **{k: v for k, v in ctx.items() if v is not None})
    vals = Values(system, {}, ctx)
    out: Dict[str, Any] = {}
    rule_sets = [system.always] + [system.dims[i].level(levels[d.id]).rules for i, d in enumerate(system.dims)]
    for rules in rule_sets:
        for mid, (clauses, _) in system.expand(rules).items():
            md = system.metrics[mid]
            c = next((c for c in clauses if V.matches(c.when, ctx) and not c.items), None)
            if c is None or (isinstance(c.value, dict) and "lookup" in c.value):
                continue
            try:
                lim = _limit(system, md, c, {}, vals)
            except (V.ValueError_, KeyError, TypeError):
                continue
            if mid in out and md["kind"] in ("min", "max"):
                lim = max(lim, out[mid]) if md["kind"] == "min" else min(lim, out[mid])
            out[mid] = lim
    return out


# Legacy fab-limit keys used by the generic MFG-* checks, filled from the ordered class.
def fab_limits(system: System, levels: Dict[str, str], ctx: Dict[str, Any]) -> Dict[str, float]:
    lim = limits_for(system, levels, ctx)
    b = system.basis
    ring_add = float(b["pth_offset"]) / 2 if b["ring"] == "toolsize" else 0.0
    out: Dict[str, float] = {}

    def first(*keys):
        vals = [lim[k] for k in keys if isinstance(lim.get(k), (int, float))]
        return min(vals) if vals else None
    pairs = [("min_track", first("track_outer")), ("min_clearance", first("gap_outer")),
             ("min_drill", first("drill_via", "drill_pth")),
             ("min_via_annular", None if first("ring_via_outer") is None else first("ring_via_outer") + ring_add),
             ("min_tht_annular", None if first("ring_pth_outer") is None else first("ring_pth_outer") + ring_add),
             ("min_edge_clearance", first("copper_to_edge")),
             ("min_hole_to_hole", first("hole_to_hole", "hole_to_hole_via", "hole_to_hole_pth", "hole_to_hole_diffnet")),
             ("max_aspect_ratio", first("aspect_ratio")), ("max_thickness", first("thickness_max"))]
    for k, v in pairs:
        if v is not None:
            out[k] = round(v, 4)
    if "max_aspect_ratio" in out:
        out["aspect_toolsize_offset"] = float(b["pth_offset"]) if b["aspect"] == "toolsize" else 0.0
    return out


def describe(system: System, ctx: Optional[Dict[str, Any]] = None, cls: Any = None) -> Dict[str, Any]:
    """Human/AI-readable view of a system: levels and their limits (resolved for ctx when given)."""
    d = system.summary()
    d.update({"notes": system.raw.get("notes", []), "hole_basis": system.basis, "parameters": system.raw.get("parameters", {}),
              "always": system.always})
    if cls is not None:
        lv = system.parse_class(cls)
        d["class"] = system.format_class(lv)
        if ctx is not None:
            d["limits_mm"] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in limits_for(system, lv, ctx).items()}
        d["levels"] = {dim.id: copy.deepcopy(dim.level(lv[dim.id]).raw) for dim in system.dims}
    else:
        d["levels"] = {dim.id: [copy.deepcopy(l.raw) for l in dim.levels] for dim in system.dims}
    return d
