"""Values, units, expressions and conditions used in class-system files.

Lengths are stored in mm. In a file you may write them the way the source
publishes them, so a definition can be checked against its source line by line:

    0.1          -> 0.1 mm
    "4mil"       -> 0.1016 mm         ("mil", "thou")
    "100um"      -> 0.1 mm            ("um", "µm")
    "0.004in"    -> 0.1016 mm         ("in", "inch", '"')
    "0.05 + registration_mm"          expression over context values and parameters
    "1oz"        -> 35 (um)           only for copper_um / inner_copper_um conditions
"""
from __future__ import annotations

import ast
import math
import operator
import re
from typing import Any, Dict, Iterable, Optional

LENGTH_UNITS = {"mm": 1.0, "um": 0.001, "µm": 0.001, "μm": 0.001, "mil": 0.0254, "mils": 0.0254, "thou": 0.0254,
                "in": 25.4, "inch": 25.4, '"': 25.4, "cm": 10.0}
COPPER_UNITS = {"um": 1.0, "µm": 1.0, "μm": 1.0, "oz": 35.0, "mm": 1000.0}
_QTY = re.compile(r"^\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*(mm|um|µm|μm|mils?|thou|inch|in|\"|cm|oz)?\s*$")
_INLINE = re.compile(r"(\d*\.?\d+)\s*(mm|um|µm|μm|mils?|thou|inch|in\b|cm)")


class ValueError_(ValueError):
    pass


def parse_quantity(v: Any, unit: str = "mm") -> float:
    """Number or unit string -> float in the metric's unit (mm, um for copper, plain for ratio/count)."""
    if isinstance(v, bool):
        raise ValueError_("expected a number, got %r" % v)
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        raise ValueError_("expected a number or unit string, got %r" % (v,))
    m = _QTY.match(v)
    if not m:
        raise ValueError_("cannot read %r as a quantity" % v)
    num, u = float(m.group(1)), (m.group(2) or "")
    if not u:
        return num
    if unit == "um":
        if u not in COPPER_UNITS:
            raise ValueError_("unit %r is not a copper thickness unit (um, oz)" % u)
        return num * COPPER_UNITS[u]
    if unit in ("ratio", "count", "V"):
        raise ValueError_("%r: this value has no unit" % v)
    if u not in LENGTH_UNITS:
        raise ValueError_("unit %r is not a length unit" % u)
    return num * LENGTH_UNITS[u]


_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
_FUN = {"min": min, "max": max, "abs": abs, "round": round}


def is_expression(v: Any) -> bool:
    return isinstance(v, str) and not _QTY.match(v)


def expr_names(text: str) -> set:
    tree = ast.parse(_INLINE.sub(lambda m: "0", text), mode="eval")
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id not in _FUN}


def eval_expression(text: str, ctx: Dict[str, Any]) -> float:
    """Safe arithmetic: numbers, unit literals (4mil), context names, + - * /, min/max/abs/round."""
    src = _INLINE.sub(lambda m: repr(parse_quantity(m.group(0))), text)
    tree = ast.parse(src, mode="eval")

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return float(n.value)
        if isinstance(n, ast.Name):
            v = ctx.get(n.id)
            if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError_("expression %r: %s is not a known number" % (text, n.id))
            return float(v)
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
            return _BIN[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            return -ev(n.operand) if isinstance(n.op, ast.USub) else ev(n.operand)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUN and not n.keywords:
            return float(_FUN[n.func.id](*[ev(a) for a in n.args]))
        raise ValueError_("expression %r: only numbers, names, + - * / and min/max/abs/round are allowed" % text)
    return ev(tree)


def number(v: Any, ctx: Dict[str, Any], unit: str = "mm") -> float:
    if is_expression(v):
        return eval_expression(v, ctx)
    return parse_quantity(v, unit)


# --------------------------------------------------------------------------- #
# Conditions (rule "when" on the context, rule "items" on a measured item)
# --------------------------------------------------------------------------- #
OPS = ("min", "max", "lt", "gt", "eq", "not", "in")


def _unit_for(key: str) -> str:
    return "um" if key.endswith("copper_um") else "mm"


def _cmp_value(key: str, v: Any) -> Any:
    if isinstance(v, str) and _QTY.match(v):
        return parse_quantity(v, _unit_for(key))
    return v


def _eq(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().upper() == b.strip().upper()
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(float(a) - float(b)) <= 1e-6
    return a == b


def test(cond: Any, actual: Any, key: str = "") -> bool:
    """One value against a condition: scalar (equal), list (one of), or {min,max,lt,gt,eq,not,in}."""
    if isinstance(cond, dict):
        for op, ref in cond.items():
            if op == "not":
                if test(ref, actual, key):
                    return False
                continue
            if op == "in":
                if not test(list(ref), actual, key):
                    return False
                continue
            if op == "eq":
                if not test(ref, actual, key):
                    return False
                continue
            if actual is None or isinstance(actual, bool) or not isinstance(actual, (int, float)):
                return False
            r = _cmp_value(key, ref)
            if op == "min" and not actual >= r - 1e-9:
                return False
            if op == "max" and not actual <= r + 1e-9:
                return False
            if op == "lt" and not actual < r - 1e-9:
                return False
            if op == "gt" and not actual > r + 1e-9:
                return False
        return True
    if isinstance(cond, list):
        return any(test(c, actual, key) for c in cond)
    if actual is None:
        return cond is None
    return _eq(_cmp_value(key, cond), actual)


def matches(conds: Any, values: Dict[str, Any]) -> bool:
    """Dict = all keys must match; list of dicts = any of them."""
    if conds is None:
        return True
    if isinstance(conds, list):
        return any(matches(c, values) for c in conds)
    return all(test(c, values.get(k), k) for k, c in conds.items())


def condition_keys(conds: Any) -> Iterable[str]:
    if isinstance(conds, list):
        for c in conds:
            yield from condition_keys(c)
    elif isinstance(conds, dict):
        yield from conds.keys()


def normalize_finish(text: Optional[str]) -> Optional[str]:
    """KiCad stack-up finish names -> short tokens used in conditions."""
    t = (text or "").strip().upper()
    if not t or t in ("NONE", "USER DEFINED"):
        return None
    if "ENEPIG" in t:
        return "ENEPIG"
    if "ENIG" in t or "IMMERSION GOLD" in t or "IMMERSION NICKEL" in t:
        return "ENIG"
    if "HAL" in t or "HASL" in t:
        return "HASL-LF" if ("FREE" in t or "LF" in t.split()) else "HASL"
    if "OSP" in t:
        return "OSP"
    if "SILVER" in t:
        return "IMMERSION-SILVER"
    if "TIN" in t:
        return "IMMERSION-TIN"
    if "HARD" in t and "GOLD" in t:
        return "HARD-GOLD"
    return t


def finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
