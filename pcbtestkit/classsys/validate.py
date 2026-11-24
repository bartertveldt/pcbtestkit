"""Validate a class-system definition before it is used.

Catches what typically goes wrong when a table is typed in by hand or by an AI
model: unknown metric names, unreadable units, expressions that use unknown
names, conditions on unknown context keys, missing table columns, levels that
are out of order (a finer level with a coarser limit), a metric limited by two
dimensions, and the system's own test vectors (measured values -> expected class).
"""
from __future__ import annotations

import difflib
import itertools
import re
from typing import Any, Dict, List

from . import catalog as K
from . import values as V
from .engine import Clause, System, SystemError_, compile_rule, evaluate, limits_for

STATUSES = ("verified", "secondary", "partial", "draft", "user")
SEVERITIES = ("critical", "major", "minor", "info")
CLAUSE_KEYS = {"when", "items", "value", "lookup", "column", "severity", "note", "source"}
TOP_KEYS = {"id", "name", "short", "vendor", "kind", "status", "source", "sources", "verified", "version", "notes", "category", "finding_id",
            "tolerance_mm", "hole_basis", "parameters", "tables", "metrics", "aliases", "dimensions", "always",
            "offer_labels", "class_format", "context_keys", "tests", "description", "$schema", "_doc"}
SAMPLE_CTX = [dict(layers=l, copper_um=c, inner_copper_um=c, thickness_mm=t, finish=f, mask_color="green", edge_process="routed",
                   board_w_mm=100.0, board_h_mm=80.0)
              for l, c, t, f in itertools.product((1, 2, 4, 6, 8), (12, 18, 35, 70, 105), (0.8, 1.6), (None, "ENIG", "HASL"))]


def validate(raw: Dict[str, Any], run_tests: bool = True) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []
    E, W = errors.append, warnings.append
    if not isinstance(raw, dict):
        return {"ok": False, "errors": ["definition must be a JSON object"], "warnings": [], "tests": []}
    for k in raw:
        if k not in TOP_KEYS and not k.startswith("_"):
            W("unknown top-level key %r (ignored)" % k)
    sid = raw.get("id")
    if not isinstance(sid, str) or not re.match(r"^[a-z0-9][a-z0-9._\-]*$", sid or ""):
        E("id must be a lower-case slug like 'myfab-2025'")
    if not raw.get("name"):
        E("name is required")
    status = raw.get("status", "draft")
    if status not in STATUSES:
        E("status must be one of %s" % ", ".join(STATUSES))
    if status in ("verified", "secondary", "partial"):
        if not raw.get("source"):
            E("status %s needs a source URL" % status)
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(raw.get("verified", ""))):
            E("status %s needs 'verified': 'YYYY-MM-DD' (date the numbers were checked against the source)" % status)
    hb = raw.get("hole_basis") or {}
    for k, v in hb.items():
        if k in ("pth_offset", "npth_offset"):
            if not V.finite(v) or v < 0 or v > 0.5:
                E("hole_basis.%s must be a number between 0 and 0.5 mm" % k)
        elif k in ("ring", "hole_clearance", "hole_to_hole", "aspect"):
            if v not in ("finished", "toolsize"):
                E("hole_basis.%s must be 'finished' or 'toolsize'" % k)
        else:
            E("unknown hole_basis key %r" % k)
    params = {}
    for k, v in (raw.get("parameters") or {}).items():
        dv = v.get("default") if isinstance(v, dict) else v
        if not re.match(r"^[a-z_][a-z0-9_]*$", k):
            E("parameter name %r must be an identifier" % k)
        if k in K.CONTEXT_KEYS:
            E("parameter %r clashes with a context key" % k)
        if not V.finite(dv) and not isinstance(dv, str):
            E("parameter %r needs a default value" % k)
        params[k] = dv
    ctx_keys = set(K.CONTEXT_KEYS) | set(params) | set(raw.get("context_keys") or {})
    try:
        system = System(raw, origin="validate")
    except Exception as e:  # noqa: BLE001 - report any construction error
        return {"ok": False, "errors": errors + ["cannot build system: %s" % e], "warnings": warnings, "tests": []}
    for mid, md in (raw.get("metrics") or {}).items():
        if md.get("stream") not in K.STREAMS and md.get("kind") != "manual":
            E("metric %s: stream must be one of %s" % (mid, ", ".join(K.STREAMS)))
        if md.get("kind", "min") not in ("min", "max", "allowed", "feature", "manual"):
            E("metric %s: kind must be min|max|allowed|feature|manual" % mid)
        if mid in K.METRICS:
            W("metric %s redefines a catalog metric" % mid)
    tables = raw.get("tables") or {}
    for tid, t in tables.items():
        rows = t.get("rows") or []
        if not rows:
            E("table %s has no rows" % tid)
            continue
        ups = [r.get("upto") for r in rows]
        if not all(V.finite(u) for u in ups) or ups != sorted(ups):
            E("table %s: rows need ascending numeric 'upto'" % tid)
        cols = set().union(*[set(r) for r in rows]) - {"upto"}
        for r in rows:
            for c in cols:
                if c not in r:
                    E("table %s: row upto=%s misses column %s" % (tid, r.get("upto"), c))
                elif not V.finite(r[c]) and not (isinstance(r[c], str) and _qty_ok(r[c])):
                    E("table %s: %s at upto=%s is not a number" % (tid, c, r.get("upto")))
        for c in ((t.get("beyond") or {}).get("per_unit") or {}):
            if c not in cols:
                E("table %s: beyond.per_unit has unknown column %s" % (tid, c))
    dims = raw.get("dimensions") or []
    if not dims and not raw.get("always"):
        E("a system needs dimensions (classes) or 'always' limits")
    seen_dim = set()
    owner: Dict[str, str] = {}
    for d in system.dims:
        if d.id in seen_dim:
            E("duplicate dimension id %s" % d.id)
        seen_dim.add(d.id)
        if d.choose not in ("cheapest", "user"):
            E("dimension %s: choose must be 'cheapest' or 'user'" % d.id)
        if not d.levels:
            E("dimension %s has no levels" % d.id)
        names = {}
        for l in d.levels:
            for n in [l.id] + l.aliases:
                key = re.sub(r"[\s_\-./:]+", "", n).upper()
                if key in names:
                    E("dimension %s: level name %r used twice" % (d.id, n))
                names[key] = l.id
            _check_rules(system, l.rules, "%s %s" % (d.id, l.id), ctx_keys, tables, E, W)
            for k in V.condition_keys(l.requires):
                if k not in ctx_keys:
                    E("%s %s: requires unknown context key %r" % (d.id, l.id, k))
            for m in system.expand(l.rules):
                if owner.get(m, d.id) != d.id:
                    E("metric %s is limited by two dimensions (%s and %s)" % (m, owner[m], d.id))
                owner[m] = d.id
        if d.default is not None and d.level(str(d.default)) is None:
            E("dimension %s: default %r is not a level" % (d.id, d.default))
        if d.choose == "user" and d.default is None:
            W("dimension %s: a 'user' dimension should have a default level" % d.id)
        if d.choose == "cheapest" and d.raw.get("monotonic") is not False:
            # monotonic: true = errors, unset = warnings, false = levels are alternatives (stack-ups), not a ladder
            _monotonic(system, d, params, E if d.raw.get("monotonic") else W)
    fmt = system.class_format
    for d in system.dims:
        if fmt.count("{%s}" % d.id) != 1:
            E("class_format must contain {%s} exactly once" % d.id)
    _check_rules(system, system.always, "always", ctx_keys, tables, E, W)
    if system.dims:
        try:
            for c in system.all_classes(50):
                system.parse_class(c)
        except SystemError_ as e:
            E("class names are ambiguous: %s" % e)
    results = []
    if run_tests and not errors:
        tests = raw.get("tests") or []
        if not tests:
            W("no test vectors: add 'tests' with measured values and the class you expect")
        for i, t in enumerate(tests):
            results.append(_run_test(system, t, i))
        for r in results:
            if not r["ok"]:
                E("test %s failed: %s" % (r["name"], "; ".join(r["problems"])))
    return {"ok": not errors, "errors": errors, "warnings": warnings, "tests": results}


def _qty_ok(s: str) -> bool:
    try:
        V.parse_quantity(s)
        return True
    except ValueError:
        return False


def _check_rules(system: System, rules: Dict[str, Any], where: str, ctx_keys, tables, E, W) -> None:
    if not isinstance(rules, dict):
        E("%s: limits must be an object" % where)
        return
    for key, raw in rules.items():
        if key not in system.metrics and key not in system.aliases:
            names = list(system.metrics) + list(system.aliases)
            close = difflib.get_close_matches(key, names, 3, 0.5) or [m for m in names if key.split("_")[0] in m][:3]
            E("%s: unknown metric %r%s" % (where, key, (" (did you mean %s?)" % ", ".join(close)) if close else ""))
            continue
        members = system.aliases.get(key, [key]) if key not in system.metrics else [key]
        kinds = {system.metrics[m]["kind"] for m in members}
        units = {system.metrics[m].get("unit", "mm") for m in members}
        kind, unit = kinds.pop(), units.pop()
        clauses = raw if isinstance(raw, list) and raw and all(isinstance(c, dict) for c in raw) else [raw]
        for c in clauses:
            if isinstance(c, dict) and ("value" in c or "lookup" in c or "when" in c or "items" in c):
                for k in c:
                    if k not in CLAUSE_KEYS:
                        E("%s.%s: unknown clause key %r" % (where, key, k))
                for k in V.condition_keys(c.get("when")):
                    if k not in ctx_keys:
                        E("%s.%s: 'when' uses unknown context key %r (known: %s)" % (where, key, k, ", ".join(sorted(ctx_keys))))
                if c.get("severity") and c["severity"] not in SEVERITIES:
                    E("%s.%s: severity must be one of %s" % (where, key, ", ".join(SEVERITIES)))
                if "lookup" in c:
                    t = tables.get(c["lookup"])
                    if t is None:
                        E("%s.%s: unknown table %r" % (where, key, c["lookup"]))
                    elif not t.get("rows") or c.get("column") not in t["rows"][0]:
                        E("%s.%s: table %s has no column %r" % (where, key, c["lookup"], c.get("column")))
                    continue
                if "value" not in c:
                    E("%s.%s: clause needs 'value' or 'lookup'" % (where, key))
                    continue
                val = c["value"]
            else:
                val = c
            fields = set()
            for m in members:
                fields |= set(K.FIELDS.get(system.metrics[m].get("stream") or "", []))
            _check_value(val, kind, unit, "%s.%s" % (where, key), set(ctx_keys) | fields, E)


def _check_value(val: Any, kind: str, unit: str, where: str, ctx_keys, E) -> None:
    if kind == "manual":
        return
    if kind == "feature":
        if not isinstance(val, bool) and not (V.finite(val) and val >= 0):
            E("%s: feature limit must be true, false or a maximum count" % where)
        return
    if kind == "allowed":
        lst = val if isinstance(val, list) else [val]
        for x in lst:
            try:
                V.parse_quantity(x, unit)
            except ValueError as e:
                E("%s: %s" % (where, e))
        return
    if V.is_expression(val):
        try:
            names = V.expr_names(val)
        except SyntaxError:
            E("%s: %r is neither a quantity nor a valid expression" % (where, val))
            return
        bad = [n for n in names if n not in ctx_keys]
        if bad:
            E("%s: expression %r uses unknown name(s) %s" % (where, val, ", ".join(bad)))
        return
    try:
        x = V.parse_quantity(val, unit)
    except ValueError as e:
        E("%s: %s" % (where, e))
        return
    if unit == "mm" and not (0 <= x <= 3000):
        E("%s: %s mm is not a plausible length" % (where, x))
    if unit == "mm" and 0 < x < 0.01:
        E("%s: %s mm is below 10 um; was this meant in mil or um?" % (where, x))


def _monotonic(system: System, d, params, report) -> None:
    """Finer levels (later in the list) must not have coarser limits."""
    seen = set()
    exempt = set(d.raw.get("monotonic_exempt") or [])
    for ctx in SAMPLE_CTX:
        ctx = dict(params, **ctx)
        if not all(V.matches(l.requires, ctx) for l in d.levels):
            continue
        prev = None
        for l in d.levels:
            others = {dd.id: (dd.level(str(dd.default)) or dd.levels[0]).id for dd in system.dims if dd.id != d.id}
            others[d.id] = l.id
            try:
                cur = {m: v for m, v in limits_for(system, others, ctx).items() if m in system.expand(l.rules)}
            except Exception:  # noqa: BLE001
                continue
            if prev:
                for m, v in cur.items():
                    if m in exempt or m not in prev[1] or not V.finite(v) or not V.finite(prev[1][m]):
                        continue
                    kind = system.metrics[m]["kind"]
                    if (kind == "min" and v > prev[1][m] + 1e-9) or (kind == "max" and v < prev[1][m] - 1e-9):
                        key = (m, prev[0], l.id)
                        if key not in seen:
                            seen.add(key)
                            report("dimension %s: %s %s is coarser at %s (%g) than at %s (%g)" % (
                                d.id, m, "limit", l.id, v, prev[0], prev[1][m]))
            prev = (l.id, cur)


def _run_test(system: System, t: Dict[str, Any], i: int) -> Dict[str, Any]:
    name = t.get("name", "#%d" % (i + 1))
    probs: List[str] = []
    exp = t.get("expect") or {}
    try:
        ctx = dict({"layers": 2, "thickness_mm": 1.6, "copper_um": 35}, **(t.get("context") or {}))
        r = evaluate(system, None, ctx, target=exp.get("target"), synthetic=t.get("measured") or {})
    except Exception as e:  # noqa: BLE001
        return {"name": name, "ok": False, "problems": ["error: %s" % e]}
    if "required" in exp and r["required"].get("class") != exp["required"]:
        probs.append("required %r, expected %r (drivers: %s)" % (
            r["required"].get("class"), exp["required"], ", ".join(x["metric"] for x in r["required"]["drivers"])))
    if "fits" in exp and r["required"]["fits"] != exp["fits"]:
        probs.append("fits %s, expected %s" % (r["required"]["fits"], exp["fits"]))
    if "passing" in exp:
        for dim, lst in exp["passing"].items():
            got = (r["required"].get("passing") or {}).get(dim)
            if got != lst:
                probs.append("passing %s %s, expected %s" % (dim, got, lst))
    if "target" in exp:
        tg = r.get("target") or {}
        if "ok" in exp and tg.get("ok") != exp["ok"]:
            probs.append("target %s ok=%s, expected %s" % (exp["target"], tg.get("ok"), exp["ok"]))
        if "violations" in exp:
            got = sorted(m for m in (tg.get("violations") or {}) if not m.startswith("_"))
            got_all = got + sorted((r.get("process") or {}).get("violations", {}))
            if sorted(set(got_all)) != sorted(exp["violations"]):
                probs.append("violations %s, expected %s" % (sorted(set(got_all)), sorted(exp["violations"])))
    return {"name": name, "ok": not probs, "problems": probs}


TEMPLATE: Dict[str, Any] = {
    "_doc": "PCB TestKit class system. Lengths in mm unless a unit is written (\"4mil\", \"100um\"). "
            "Run: pcbtestkit classes validate <this file>. Metric names: pcbtestkit classes catalog.",
    "id": "myfab-2026",
    "name": "My Fab standard capabilities",
    "vendor": "My Fab",
    "kind": "fab-capability",
    "status": "draft",
    "source": "https://example.com/capabilities",
    "verified": "2026-01-01",
    "notes": ["What the source says that the limits do not capture."],
    "hole_basis": {"pth_offset": 0.0, "npth_offset": 0.0, "ring": "finished", "hole_clearance": "finished"},
    "parameters": {},
    "dimensions": [{
        "id": "tier", "label": "Service tier", "choose": "cheapest",
        "levels": [
            {"id": "standard", "label": "Standard (no surcharge)", "limits": {
                "track": "6mil", "gap": "6mil", "ring": "5mil", "drill_plated": 0.3, "via_pad": 0.6,
                "copper_to_edge": 0.3, "layers_max": 6}},
            {"id": "advanced", "label": "Advanced (surcharge)", "note": "costs extra", "limits": {
                "track": [{"when": {"copper_um": {"max": 40}}, "value": "4mil"}, {"value": "6mil"}],
                "gap": "4mil", "ring": "4mil", "drill_plated": 0.2, "via_pad": 0.45, "copper_to_edge": 0.25,
                "layers_max": 12}}]}],
    "always": {"aspect_ratio": 8, "thickness_min": 0.4, "thickness_max": 3.2, "microvias": False},
    "tests": [
        {"name": "6 mil board is standard", "measured": {"track_outer": 0.16, "gap_outer": 0.16, "drill_via": 0.3},
         "expect": {"required": "standard"}},
        {"name": "4 mil board needs advanced", "measured": {"track_outer": 0.1016, "gap_outer": 0.11},
         "expect": {"required": "advanced"}},
    ],
}
