"""What limits every board on its test environment, and what upgrading the environment would gain and cost.

``constraints(env)``  per board: what each resource of its station allows (analog inputs, switched supplies, every
                      bus, the plate, the batch limit), which ones bind, what one more board per batch would need and
                      cost; for a board that does not fit, what it lacks on every station and the cheapest change
                      that would take it (or why no station change helps).
``options(env)``      one change to one station at a time (more analog inputs, supplies or bus instances, a bigger
                      plate, a higher supply voltage, more boards per fixture, a further station), each with what it
                      gains (boards that fit, bigger batches, their worth) and what it costs, ranked by net worth;
                      and three paths of changes taken one after another: best value, most boards, biggest batches.
``plan_goal(env)``    the cheapest changes for what you ask: these boards must fit, that board with a batch of 6,
                      within a budget.

Everything is estimated from the board profiles kept in environment.json (no fixture is built); a build with the
changes checks every board with its real fixture layout. Prices are rough list prices in spec["costs"] (set your
own); ``value_per_board`` is what it is worth that one board type (priority 1) fits a station at all (the dedicated
tester it would need otherwise), and a batch of n is worth 1 + batch_weight x log2(n) of that.
"""
from __future__ import annotations

import copy
import json
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import environment as E

COSTS: Dict[str, Any] = E.SPEC_DEFAULTS["costs"]
BUS_WORD = {"can": "CAN", "uart": "UART", "i2c": "I2C", "rs485": "RS-485", "usb": "USB", "ethernet": "Ethernet"}
WORDS = {"inputs": "analog inputs", "supplies": "switched supplies", "plate": "plate area",
         "batch limit": "boards per fixture (max_batch)", "station limit": "the station's batch limit"}
WORDS.update({"%s buses" % b: "%s instances" % w for b, w in BUS_WORD.items()})


def costs(spec: Dict[str, Any]) -> Dict[str, Any]:
    c = dict(COSTS)
    c.update((spec or {}).get("costs") or {})
    return c


def word(key: str) -> str:
    return WORDS.get(key, key)


# --------------------------------------------------------------------------- #
# the environment as estimates: boards, stations, where each board is
# --------------------------------------------------------------------------- #
def prof_of(b: Dict[str, Any]) -> Dict[str, Any]:
    """A board of environment.json as the profile estimate() takes."""
    size = b.get("size_mm") or [50.0, 50.0]
    site = b.get("site_mm") or {"front": [float(size[0]) + 20.0, float(size[1]) + 40.0]}
    ok = b.get("status") in ("compatible", "incompatible")
    return {"folder": b["folder"], "name": b["name"], "status": "ok" if ok else b.get("status", "error"),
            "reason": b.get("reason"), "issues": list(b.get("issues") or []), "channels": int(b.get("channels") or 0),
            "supply": dict(b.get("supply") or {"needed": False}), "buses": list(b.get("buses") or []), "site": site,
            "spacing_mm": float(b.get("spacing_mm") or 10.0), "priority": float(b.get("priority", 1.0)),
            "programming": [{"protocol": x} for x in b.get("programming") or []]}


def model_of(st: Dict[str, Any], spec: Dict[str, Any]) -> Dict[str, Any]:
    return {"name": st["name"], "res": copy.deepcopy(st["resources"]), "plate": [float(v) for v in st["plate_mm"]],
            "free": [float(v) for v in st.get("free_mm") or st["plate_mm"]], "sockets": int(st.get("sockets") or 1),
            "pins": int(st.get("pins") or 0), "fixed": bool(st.get("fixed")), "own": bool(st.get("own")),
            "max_sites": int(spec.get("max_batch", 12)), "uid": E._uid()}


class Context:
    """An environment ready for what-ifs: its boards as profiles, its stations as models, where every board is."""

    def __init__(self, env: Dict[str, Any], spec: Optional[Dict[str, Any]] = None):
        self.env = env
        self.spec = copy.deepcopy(spec or env.get("spec") or E.default_spec())
        self.boards = [b for b in env.get("boards") or []]
        self.profs = {b["folder"]: prof_of(b) for b in self.boards}
        self.models = [model_of(st, self.spec) for st in env.get("stations") or []]
        self.base = {b["folder"]: ((b.get("station"), int(b.get("batch") or 0)) if b.get("status") == "compatible"
                                   else (None, 0)) for b in self.boards}
        self.cache: Dict[Tuple[str, int], Tuple[int, str]] = {}

    # ---- one board on one station --------------------------------------------------------------------
    def caps(self, b: Dict[str, Any], m: Dict[str, Any], spec: Dict[str, Any]) -> Tuple[Dict[str, int], Dict[str, Any]]:
        """What each resource of station ``m`` allows of board ``b`` ({} when the board has an issue of its own):
        the real layout's plate limit where the build found one, estimates otherwise."""
        p = self.profs[b["folder"]]
        e = E.estimate(p, m, spec, self.cache)
        by = dict(e.get("by") or {})
        if by and b.get("status") == "compatible" and b.get("station") == m["name"]:
            real = b.get("by") or {}
            if "plate" in real:
                if m.get("plate_changed") and b.get("plate_est"):
                    by["plate"] = int(by["plate"] * float(real["plate"]) / max(1.0, float(b["plate_est"])))
                elif not m.get("plate_changed"):
                    by["plate"] = min(int(by.get("plate", real["plate"])), int(real["plate"]))
        return by, e

    def batch_on(self, b: Dict[str, Any], i: int, models: Sequence[Dict[str, Any]], spec: Dict[str, Any]) -> int:
        m = models[i]
        st, n0 = self.base[b["folder"]]
        if st == m["name"] and not m.get("changed") and not spec.get("_changed"):
            return n0
        if st is None and not m.get("changed") and not spec.get("_changed") and m["name"] in _failed_on(b):
            return 0                                     # its real fixture did not fit there, and nothing changed
        by, _e = self.caps(b, m, spec)
        n = max(0, min(by.values())) if by else 0
        return max(n, n0) if st == m["name"] else n

    def where(self, models: Sequence[Dict[str, Any]], spec: Dict[str, Any]) -> Dict[str, Tuple[Optional[str], int]]:
        """Every board's (station, batch): a board stays on its station, one that fits none goes to the first that
        takes it."""
        out: Dict[str, Tuple[Optional[str], int]] = {}
        names = [m["name"] for m in models]
        for b in self.boards:
            p = self.profs[b["folder"]]
            if p["status"] != "ok":
                out[b["folder"]] = (None, 0)
                continue
            st, _n0 = self.base[b["folder"]]
            if st in names:
                out[b["folder"]] = (st, self.batch_on(b, names.index(st), models, spec))
                continue
            got: Tuple[Optional[str], int] = (None, 0)
            for i in range(len(models)):
                n = self.batch_on(b, i, models, spec)
                if n >= 1:
                    got = (names[i], n)
                    break
            out[b["folder"]] = got
        return out

    # ---- worth and cost ------------------------------------------------------------------------------
    def metrics(self, models: Sequence[Dict[str, Any]], spec: Dict[str, Any],
                where: Optional[Dict[str, Tuple[Optional[str], int]]] = None) -> Dict[str, Any]:
        where = where if where is not None else self.where(models, spec)
        c = costs(spec)
        fit = [f for f, (s, n) in where.items() if s and n >= 1]
        worth = sum(self.profs[f]["priority"] * E.value(where[f][1], spec) for f in fit) * float(c["value_per_board"])
        cost = environment_cost(models, spec, where, self.profs)
        return {"boards_fit": len(fit), "boards": len(self.boards), "slots": sum(where[f][1] for f in fit),
                "worth": round(worth, 0), "cost": round(cost["total"], 0), "net": round(worth - cost["total"], 0),
                "cost_parts": cost}


def _failed_on(b: Dict[str, Any]) -> set:
    return {t.get("station") for t in b.get("tried") or []}


def station_cost(res: Dict[str, Any], plate: Sequence[float], spec: Dict[str, Any]) -> Dict[str, Any]:
    """What one station costs: {"total", "parts": {what: cost}}."""
    c = costs(spec)
    _pins, sockets = E._pins(res, spec)
    group = max(1, int(spec.get("ain_group", 4)))
    hv = float((spec.get("supplies") or {}).get("max_v", 24.0)) > 24.0 + 1e-6
    parts = {"station": float(c["station"]), "sockets": sockets * float(c["socket"]),
             "analog inputs": int(math.ceil(int(res.get("ain", 0)) / float(group))) * float(c["ain_group"]),
             "supplies": int(res.get("vout", 0)) * (float(c["supply"]) + (float(c["high_voltage_supply"]) if hv else 0.0)),
             "plate": float(plate[0]) * float(plate[1]) / 10000.0 * float(c["plate_dm2"])}
    for bus, n in sorted((res.get("buses") or {}).items()):
        if n:
            parts[BUS_WORD.get(bus, bus)] = int(n) * float(c.get(bus, 20.0))
    return {"total": round(sum(parts.values()), 2), "parts": {k: round(v, 2) for k, v in parts.items() if v}}


def environment_cost(models: Sequence[Dict[str, Any]], spec: Dict[str, Any], where: Dict[str, Tuple[Optional[str], int]],
                     profs: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Stations, the programmers their batches need, and every board type's fixture."""
    c = costs(spec)
    st = {m["name"]: station_cost(m["res"], m["plate"], spec) for m in models}
    prog: Dict[Tuple[str, str], int] = {}
    fixtures = 0.0
    for f, (s, n) in where.items():
        if not s or n < 1:
            continue
        fixtures += float(c["fixture"]) + n * float(c["fixture_site"])
        for x in profs[f].get("programming") or []:
            k = (s, x["protocol"])
            prog[k] = max(prog.get(k, 0), n)
    programmers = sum(prog.values()) * float(c["programmer"])
    total = sum(v["total"] for v in st.values()) + programmers + fixtures
    return {"total": round(total, 2), "stations": {k: v["total"] for k, v in st.items()},
            "programmers": round(programmers, 2), "fixtures": round(fixtures, 2)}


def _programmers(where: Dict[str, Tuple[Optional[str], int]], profs: Dict[str, Dict[str, Any]]) -> Dict[Tuple[str, str], int]:
    out: Dict[Tuple[str, str], int] = {}
    for f, (s, n) in where.items():
        if s and n >= 1:
            for x in profs[f].get("programming") or []:
                k = (s, x["protocol"])
                out[k] = max(out.get(k, 0), n)
    return out


def change_cost(ctx: "Context", m0: Sequence[Dict[str, Any]], s0: Dict[str, Any], w0: Dict[str, Tuple[Optional[str], int]],
                m1: Sequence[Dict[str, Any]], s1: Dict[str, Any], w1: Dict[str, Tuple[Optional[str], int]],
                only: Optional[set] = None) -> float:
    """What a change costs: the stations' new hardware, the programmers bigger batches need, and a new fixture for
    every board type whose station or batch changes (what you have is not counted again)."""
    c = costs(s1)
    before = {m["name"]: station_cost(m["res"], m["plate"], s0)["total"] for m in m0}
    hw = sum(max(0.0, station_cost(m["res"], m["plate"], s1)["total"] - before.get(m["name"], 0.0)) for m in m1)
    p0, p1 = _programmers(w0, ctx.profs), _programmers(w1, ctx.profs)
    prog = sum(max(0, n - p0.get(k, 0)) for k, n in p1.items()) * float(c["programmer"])
    fx = sum(float(c["fixture"]) + n * float(c["fixture_site"]) for f, (st, n) in w1.items()
             if st and n >= 1 and w0.get(f) != (st, n) and (only is None or f in only))
    return round(hw + prog + fx, 2)


# --------------------------------------------------------------------------- #
# changes
# --------------------------------------------------------------------------- #
def describe_change(ch: Dict[str, Any], models: Sequence[Dict[str, Any]], spec: Dict[str, Any],
                    station: bool = True) -> str:
    text = _describe(ch, models, spec)
    if not station and ch.get("station") and text.startswith(ch["station"] + ": "):
        text = text[len(ch["station"]) + 2:]
    return text


def _describe(ch: Dict[str, Any], models: Sequence[Dict[str, Any]], spec: Dict[str, Any]) -> str:
    k = ch["kind"]
    if k == "supply_v":
        return "supplies up to %g V (now %g V)" % (ch["to"], float((spec.get("supplies") or {}).get("max_v", 24.0)))
    if k == "max_batch":
        return "up to %d boards per fixture (now %d)" % (ch["to"], int(spec.get("max_batch", 12)))
    if k == "station":
        return "a further station, %s, for %d board%s that fit none" % (ch["name"], ch.get("for", 0),
                                                                       "" if ch.get("for") == 1 else "s")
    m = next((x for x in models if x["name"] == ch["station"]), None)
    res = m["res"] if m else {}
    if k == "ain":
        return "%s: %d more analog inputs (%d -> %d)" % (ch["station"], ch["to"] - res.get("ain", 0), res.get("ain", 0), ch["to"])
    if k == "vout":
        return "%s: %d more switched supplies (%d -> %d)" % (ch["station"], ch["to"] - res.get("vout", 0), res.get("vout", 0), ch["to"])
    if k == "bus":
        have = int((res.get("buses") or {}).get(ch["bus"], 0))
        return "%s: %d more %s (%d -> %d)" % (ch["station"], ch["to"] - have, BUS_WORD.get(ch["bus"], ch["bus"]), have, ch["to"])
    if k == "plate":
        return "%s: plate %g x %g mm (now %g x %g mm)" % (ch["station"], ch["to"][0], ch["to"][1],
                                                         m["plate"][0] if m else 0, m["plate"][1] if m else 0)
    return json.dumps(ch)


def apply_change(models: Sequence[Dict[str, Any]], spec: Dict[str, Any], ch: Dict[str, Any],
                 ctx: Optional[Context] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """The stations and spec with one change made (new lists; the old ones stay as they were)."""
    ms = [dict(m, res=copy.deepcopy(m["res"])) for m in models]
    sp = copy.deepcopy(spec)
    k = ch["kind"]
    if k == "supply_v":
        sp.setdefault("supplies", {})["max_v"] = float(ch["to"])
        sp["_changed"] = True
        return ms, sp
    if k == "max_batch":
        sp["max_batch"] = int(ch["to"])
        sp["_changed"] = True
        for m in ms:
            m["max_sites"] = int(ch["to"])
        return ms, sp
    if k == "station":
        m = E.model_from_resources(ch["res"], ch["plate"], sp, ch["name"])
        m.update(changed=True, new=True, max_sites=int(sp.get("max_batch", 12)))
        ms.append(m)
        return ms, sp
    i = next(j for j, m in enumerate(ms) if m["name"] == ch["station"])
    m = ms[i]
    res = m["res"]
    plate = m["plate"]
    if k == "ain":
        res["ain"] = int(ch["to"])
    elif k == "vout":
        res["vout"] = int(ch["to"])
    elif k == "bus":
        res.setdefault("buses", {})[ch["bus"]] = int(ch["to"])
    elif k == "plate":
        plate = [float(ch["to"][0]), float(ch["to"][1])]
    new = E.model_from_resources(res, plate, sp, m["name"])
    if k != "plate" and m.get("fixed"):
        new["free"] = list(m["free"])                    # an existing station keeps its socket band
    new.update(changed=True, fixed=m.get("fixed"), own=m.get("own"), max_sites=m.get("max_sites"),
               plate_changed=bool(m.get("plate_changed") or k == "plate"), new=m.get("new"))
    ms[i] = new
    return ms, sp


def _limit_flags(m: Dict[str, Any], spec: Dict[str, Any]) -> List[str]:
    lim = spec.get("limits") or {}
    so = spec.get("sockets") or {}
    out = []
    res = m["res"]
    if int(res.get("ain", 0)) > int(lim.get("analog_inputs", 96)):
        out.append("more analog inputs than limits.analog_inputs (%d)" % int(lim.get("analog_inputs", 96)))
    if int(res.get("vout", 0)) > int(lim.get("supplies", 8)):
        out.append("more supplies than limits.supplies (%d)" % int(lim.get("supplies", 8)))
    for bus, n in (res.get("buses") or {}).items():
        if int(n) > int(lim.get(bus, 0)):
            out.append("more %s than limits.%s (%d)" % (BUS_WORD.get(bus, bus), bus, int(lim.get(bus, 0))))
    _p, sockets = E._pins(res, spec)
    if sockets > int(so.get("max", 2)):
        out.append("%d interface sockets: more than sockets.max (%d)" % (sockets, int(so.get("max", 2))))
    if m.get("own"):
        out.append("your own station board: make the change in KiCad")
    return out


def _round_plate(w: float, h: float) -> List[float]:
    """Plates come in 50 mm steps (a plate a few mm bigger is a new plate all the same)."""
    return [float(int(math.ceil(w / 50.0 - 1e-9)) * 50), float(int(math.ceil(h / 50.0 - 1e-9)) * 50)]


def _plate_ok(pl: Sequence[float], spec: Dict[str, Any]) -> bool:
    big = spec.get("large_plate_mm") or spec.get("plate_mm")
    return float(pl[0]) <= float(big[0]) + 1e-6 and float(pl[1]) <= float(big[1]) + 1e-6


def plate_for(p: Dict[str, Any], n: int, m: Dict[str, Any]) -> Optional[List[float]]:
    """The smallest plate (10 mm steps, never smaller than the station's) whose free area takes n sites of board p."""
    best = None
    band = float(m["plate"][1]) - float(m["free"][1])            # the socket band at the front stays
    for _entry, site in (p.get("site") or {}).items():
        w, h = float(site[0]), float(site[1])
        s = float(p.get("spacing_mm", 10.0))
        for cols in range(1, n + 1):
            rows = int(math.ceil(n / float(cols)))
            need_w = max(float(m["plate"][0]), cols * w + (cols - 1) * s)
            need_h = max(float(m["plate"][1]), rows * h + (rows - 1) * s + band)
            pl = _round_plate(need_w, need_h)
            if best is None or pl[0] * pl[1] < best[0] * best[1]:
                best = pl
    return best


def candidates(ctx: Context, models: Sequence[Dict[str, Any]], spec: Dict[str, Any],
               where: Dict[str, Tuple[Optional[str], int]]) -> List[Dict[str, Any]]:
    """The single changes worth looking at: for every station, the smallest step of each resource that lets one
    more board of a type on it (or a board that fits none onto it), a few steps further, a bigger plate; and the
    environment-wide ones (supply voltage, boards per fixture, a further station)."""
    out: List[Dict[str, Any]] = []
    group = max(1, int(spec.get("ain_group", 4)))
    mb = int(spec.get("max_batch", 12))
    names = [m["name"] for m in models]
    for i, m in enumerate(models):
        res = m["res"]
        t_ain, t_vout, t_plate = set(), set(), []
        t_bus: Dict[str, set] = {}
        for b in ctx.boards:
            p = ctx.profs[b["folder"]]
            if p["status"] != "ok" or E.hard_issue(p, spec):
                continue
            st, n = where[b["folder"]]
            if st is not None and st != m["name"]:
                continue
            target = n + 1 if st == m["name"] else 1
            if target > min(mb, int(m.get("max_sites") or mb)):
                continue
            by, _e = ctx.caps(b, m, spec)
            if not by:
                continue
            if p["channels"] and by.get("inputs", 999) < target:
                t_ain.add(int(math.ceil(p["channels"] * target / float(group))) * group)
            if p["supply"].get("needed") and by.get("supplies", 999) < target:
                t_vout.add(target)
            for bus in p["buses"]:
                if by.get("%s buses" % bus, 999) < target:
                    t_bus.setdefault(bus, set()).add(target)
            if by.get("plate", 999) < target:
                pl = plate_for(p, target, m)
                if pl:
                    t_plate.append(pl)

        def steps(targets, now):
            ts = sorted(t for t in targets if t > now)
            return sorted(set(ts[:3] + ts[-1:]))
        for t in steps(t_ain, int(res.get("ain", 0))):
            out.append({"kind": "ain", "station": m["name"], "to": t})
        for t in steps(t_vout, int(res.get("vout", 0))):
            out.append({"kind": "vout", "station": m["name"], "to": t})
        for bus, ts in sorted(t_bus.items()):
            for t in steps(ts, int((res.get("buses") or {}).get(bus, 0))):
                out.append({"kind": "bus", "station": m["name"], "bus": bus, "to": t})
        plates = sorted({tuple(p) for p in t_plate if _plate_ok(p, spec)}, key=lambda p: p[0] * p[1])
        big = spec.get("large_plate_mm")
        if big and float(big[0]) * float(big[1]) > m["plate"][0] * m["plate"][1] and t_plate:
            plates.append((float(big[0]), float(big[1])))
        for pl in sorted(set(plates[:2] + plates[-1:]), key=lambda p: p[0] * p[1]):
            if pl[0] * pl[1] > m["plate"][0] * m["plate"][1] + 1:
                out.append({"kind": "plate", "station": m["name"], "to": [pl[0], pl[1]]})
    # environment-wide: a supply voltage some boards need, more boards per fixture, a further station
    volts = set()
    for b in ctx.boards:
        p = ctx.profs[b["folder"]]
        v = (p.get("supply") or {}).get("voltage")
        if p["status"] == "ok" and not p["issues"] and p["supply"].get("needed") and v is not None and \
                float(v) > float((spec.get("supplies") or {}).get("max_v", 24.0)) + 1e-6:
            volts.add(float(v))
    for v in sorted(volts)[:3]:
        out.append({"kind": "supply_v", "to": v})
    if any(where[b["folder"]][0] and where[b["folder"]][1] >= mb and
           (ctx.caps(b, models[names.index(where[b["folder"]][0])], spec)[0].get("batch limit") == mb)
           for b in ctx.boards if where[b["folder"]][0] in names):
        out.append({"kind": "max_batch", "to": mb + 4})
    unfit = [ctx.profs[b["folder"]] for b in ctx.boards if not where[b["folder"]][0] and ctx.profs[b["folder"]]["status"] == "ok"
             and not E.hard_issue(ctx.profs[b["folder"]], spec)]
    if unfit:
        name = "station-%d" % (len(models) + 1)
        k = 1
        while name in names:
            k += 1
            name = "station-%d" % (len(models) + k)
        for plate in [spec["plate_mm"]] + ([spec["large_plate_mm"]] if spec.get("large_plate_mm") else []):
            res = E.design_station(unfit, spec, plate, name)
            out.append({"kind": "station", "name": name, "res": res, "plate": [float(plate[0]), float(plate[1])],
                        "for": len(unfit)})
    return out


def _gain(ctx: Context, before: Dict[str, Any], after: Dict[str, Any], w0, w1, cost: float) -> Dict[str, Any]:
    newly = sorted(ctx.profs[f]["name"] for f in w1 if w1[f][0] and not w0[f][0])
    up = sorted(({"board": ctx.profs[f]["name"], "from": w0[f][1], "to": w1[f][1]} for f in w1
                 if w0[f][0] and w1[f][1] > w0[f][1]), key=lambda x: (x["from"] - x["to"], x["board"]))
    worth = round(after["worth"] - before["worth"], 0)
    return {"boards_fit": after["boards_fit"] - before["boards_fit"], "fit": newly, "slots": after["slots"] - before["slots"],
            "batches_up": up, "worth": worth, "cost": round(cost, 0), "net": round(worth - cost, 0)}


def _try(ctx: Context, models, spec, ch, before, w0) -> Optional[Dict[str, Any]]:
    try:
        ms, sp = apply_change(models, spec, ch, ctx)
    except (StopIteration, KeyError, ValueError):
        return None
    w1 = ctx.where(ms, sp)
    after = ctx.metrics(ms, sp, w1)
    g = _gain(ctx, before, after, w0, w1, change_cost(ctx, models, spec, w0, ms, sp, w1))
    m = next((x for x in ms if x["name"] == ch.get("station", ch.get("name"))), None)
    flags = _limit_flags(m, sp) if m else []
    if ch["kind"] == "station" and len(ms) > int(spec.get("max_stations", 3)):
        flags.append("more stations than max_stations (%d)" % int(spec.get("max_stations", 3)))
    if ch["kind"] == "max_batch":
        flags.append("more pogo pins and a bigger fixture per board type; check the press force")
    if ch["kind"] == "plate" and not _plate_ok(ch["to"], spec):
        flags.append("bigger than large_plate_mm")
    if ch["kind"] in ("ain", "vout", "bus", "plate") and m and m.get("fixed") and not m.get("own"):
        flags.append("the station keeps its pins: the fixtures you have still plug in")
    return {"change": {k: v for k, v in ch.items() if k != "res"} if ch["kind"] != "station" else
            {"kind": "station", "name": ch["name"], "plate": ch["plate"], "resources": ch["res"]},
            "text": describe_change(ch, models, spec), "gain": g, "flags": flags,
            "_models": ms, "_spec": sp, "_where": w1, "_after": after}


def _clean(o: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in o.items() if not k.startswith("_")}


def options(env: Dict[str, Any], spec: Optional[Dict[str, Any]] = None, top: int = 20, path_steps: int = 8) -> Dict[str, Any]:
    """Every single change worth making, ranked by net worth, and three paths (best value, most boards, biggest
    batches). Option ids (U1, U2, ...) are what ``pcbtestkit environment upgrade --option`` takes."""
    ctx = Context(env, spec)
    models, sp = ctx.models, ctx.spec
    w0 = ctx.where(models, sp)
    now = ctx.metrics(models, sp, w0)
    singles = [o for o in (_try(ctx, models, sp, ch, now, w0) for ch in candidates(ctx, models, sp, w0)) if o]
    singles = [o for o in singles if o["gain"]["boards_fit"] > 0 or o["gain"]["slots"] > 0]
    singles.sort(key=lambda o: (-o["gain"]["net"], -o["gain"]["boards_fit"], -o["gain"]["slots"], o["gain"]["cost"]))
    for i, o in enumerate(singles, 1):
        o["id"] = "U%d" % i
    paths = {}
    for goal in ("value", "fit", "batch"):
        paths[goal] = _path(ctx, goal, path_steps)
    c = costs(sp)
    return {"currency": c["currency"], "value_per_board": c["value_per_board"], "now": _clean(dict(now)),
            "options": [_clean(o) for o in singles[:top]], "more_options": max(0, len(singles) - top),
            "paths": paths,
            "notes": ["Estimates from the board profiles: a build checks every board with its real fixture layout.",
                      "Worth: a board type that fits counts %s %s (value_per_board) x its priority, a batch of n "
                      "1 + %g x log2(n) of that; costs are rough list prices (spec costs)." % (
                          c["currency"], c["value_per_board"], float(sp.get("batch_weight", 0.35)))]}


def _path(ctx: Context, goal: str, steps: int) -> Dict[str, Any]:
    """Changes taken one after another, each the best for ``goal`` given the ones before (value: net worth;
    fit: boards that fit per cost; batch: boards per load per cost), while they gain."""
    models, sp = ctx.models, ctx.spec
    w = ctx.where(models, sp)
    cur = ctx.metrics(models, sp, w)
    start, w_start = cur, w
    out = []
    for _ in range(steps):
        best = None
        for ch in candidates(ctx, models, sp, w):
            o = _try(ctx, models, sp, ch, cur, w)
            if not o:
                continue
            g = o["gain"]
            if goal == "value":
                key = g["net"] if g["net"] > 0 else None
            elif goal == "fit":
                key = (g["boards_fit"] / max(1.0, g["cost"]), g["net"]) if g["boards_fit"] > 0 else None
            else:
                key = (g["slots"] / max(1.0, g["cost"]), g["net"]) if g["slots"] > 0 else None
            if key is not None and (best is None or key > best[0]):
                best = (key, o)
        if best is None:
            break
        o = best[1]
        models, sp, w, cur = o["_models"], o["_spec"], o["_where"], o["_after"]
        out.append(_clean(o))
    spent = change_cost(ctx, ctx.models, ctx.spec, w_start, models, sp, w)
    worth = round(cur["worth"] - start["worth"], 0)
    total = {"boards_fit": cur["boards_fit"] - start["boards_fit"], "slots": cur["slots"] - start["slots"],
             "worth": worth, "cost": round(spent, 0), "net": round(worth - spent, 0)}
    return {"steps": out, "total": total, "after": {k: v for k, v in cur.items() if k != "cost_parts"}}


# --------------------------------------------------------------------------- #
# constraints per board
# --------------------------------------------------------------------------- #
def _needs_for(ctx: Context, b: Dict[str, Any], m: Dict[str, Any], spec: Dict[str, Any], n: int) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The changes station ``m`` needs to take ``n`` boards of type ``b`` ([] when it already does), and what no
    station change can fix."""
    p = ctx.profs[b["folder"]]
    hard = E.hard_issue(p, spec)
    if hard:
        v = (p.get("supply") or {}).get("voltage")
        if not p["issues"] and p["supply"].get("needed") and v is not None and \
                float(v) > float((spec.get("supplies") or {}).get("max_v", 24.0)):
            sp2 = dict(spec, supplies=dict(spec.get("supplies") or {}, max_v=float(v)))
            more, fixed = _needs_for(ctx, b, m, sp2, n)
            return [{"kind": "supply_v", "to": float(v)}] + more, fixed
        return [], [hard]
    by, _e = ctx.caps(b, m, spec)
    out: List[Dict[str, Any]] = []
    group = max(1, int(spec.get("ain_group", 4)))
    res = m["res"]
    if p["channels"] and by.get("inputs", 999) < n:
        out.append({"kind": "ain", "station": m["name"], "to": max(int(res.get("ain", 0)),
                                                                   int(math.ceil(p["channels"] * n / float(group))) * group)})
    if p["supply"].get("needed") and by.get("supplies", 999) < n:
        out.append({"kind": "vout", "station": m["name"], "to": n})
    for bus in p["buses"]:
        if by.get("%s buses" % bus, 999) < n:
            out.append({"kind": "bus", "station": m["name"], "bus": bus, "to": n})
    if by.get("plate", 999) < n:
        pl = plate_for(p, n, m)
        if pl:
            out.append({"kind": "plate", "station": m["name"], "to": pl})
    if by.get("batch limit", 999) < n:
        out.append({"kind": "max_batch", "to": n})
    return out, []


def _cost_of(ctx: Context, changes: Sequence[Dict[str, Any]], models=None, spec=None,
             only: Optional[set] = None) -> Tuple[float, List[Dict[str, Any]], Dict[str, Any]]:
    models = models if models is not None else ctx.models
    spec = spec if spec is not None else ctx.spec
    w0 = ctx.where(models, spec)
    ms, sp = models, spec
    for ch in changes:
        ms, sp = apply_change(ms, sp, ch, ctx)
    return change_cost(ctx, models, spec, w0, ms, sp, ctx.where(ms, sp), only), ms, sp


def constraints(env: Dict[str, Any], spec: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Per board: what limits it on its station (every resource's allowance, nearest first), what one more board
    per batch needs and costs; for a board that fits none: what it lacks on every station and the cheapest change."""
    ctx = Context(env, spec)
    sp = ctx.spec
    c = costs(sp)
    out = []
    names = [m["name"] for m in ctx.models]
    for b in ctx.boards:
        p = ctx.profs[b["folder"]]
        item: Dict[str, Any] = {"board": b["name"], "folder": b["folder"], "status": b["status"],
                                "station": b.get("station"), "batch": int(b.get("batch") or 0),
                                "priority": p["priority"]}
        if p["status"] != "ok":
            item["reason"] = b.get("reason") or p.get("reason")
            out.append(item)
            continue
        if b["status"] == "compatible" and b.get("station") in names:
            m = ctx.models[names.index(b["station"])]
            by, _e = ctx.caps(b, m, sp)
            n = int(b.get("batch") or 0)
            if by.get("station limit") == by.get("batch limit"):
                by.pop("station limit", None)
            limits = sorted(({"what": word(k), "key": k, "allows": int(v), "binds": int(v) <= n} for k, v in by.items()),
                            key=lambda x: (x["allows"], x["what"]))
            item["limits"] = limits
            item["limited_by"] = [x["what"] for x in limits if x["binds"]] or [word(b.get("limit") or "-")]
            mb = min(int(sp.get("max_batch", 12)), int(m.get("max_sites") or 99))
            if n < 48:
                need, fixed = _needs_for(ctx, b, m, sp, n + 1)
                if need:
                    cost, _ms, _sp = _cost_of(ctx, need, only={b["folder"]})
                    item["next"] = {"batch": n + 1, "changes": [describe_change(x, ctx.models, sp) for x in need],
                                    "change": need, "cost": round(cost, 0),
                                    "note": "beyond max_batch %d" % mb if n + 1 > mb else None}
        else:
            per = {}
            best = None
            own_issue = E.hard_issue(p, sp)
            v = (p.get("supply") or {}).get("voltage")
            if own_issue and (p["issues"] or not p["supply"].get("needed") or v is None):
                item.update(reason=b.get("reason") or own_issue,
                            to_fit={"station": None, "changes": [], "cost": None,
                                    "note": "no station change helps: " + own_issue})
                out.append(item)
                continue
            for m in ctx.models:
                need, fixed = _needs_for(ctx, b, m, sp, 1)
                if fixed:
                    per[m["name"]] = {"lacks": fixed}
                    continue
                per[m["name"]] = {"lacks": [describe_change(x, ctx.models, sp, station=False) for x in need]}
                if m["name"] in _failed_on(b) and not need:
                    per[m["name"]]["lacks"] = [t.get("reason") for t in b.get("tried") or [] if t.get("station") == m["name"]][:1]
                    continue
                if need:
                    cost, _ms, _sp = _cost_of(ctx, need, only={b["folder"]})
                    if best is None or cost < best[0]:
                        best = (cost, m["name"], need)
            item["stations"] = per
            item["reason"] = b.get("reason")
            fixed_all = [v["lacks"][0] for v in per.values() if v.get("lacks") and not best] if not best else []
            if best:
                item["to_fit"] = {"station": best[1], "changes": [describe_change(x, ctx.models, sp) for x in best[2]],
                                  "change": best[2], "cost": round(best[0], 0)}
            elif E.hard_issue(p, sp):
                item["to_fit"] = {"station": None, "changes": [], "cost": None,
                                  "note": "no station change helps: " + E.hard_issue(p, sp)}
            elif fixed_all:
                item["to_fit"] = {"station": None, "changes": [], "cost": None, "note": fixed_all[0]}
        out.append(item)
    out.sort(key=lambda x: (x["status"] != "compatible", -x["priority"], x["board"].lower()))
    for x in out:
        x["currency"] = c["currency"]
    return out


# --------------------------------------------------------------------------- #
# goals: what you need, at the lowest cost
# --------------------------------------------------------------------------- #
def parse_changes(text: str) -> List[Dict[str, Any]]:
    """Changes written as text: ``station-1:can+1,ethernet+2,ain+8,supplies+1,plate=400x300;supply_v=48;max_batch=16;
    new_station`` (several separated by ;)."""
    out: List[Dict[str, Any]] = []
    for part in [x.strip() for x in str(text).replace("\n", ";").split(";") if x.strip()]:
        if part in ("new_station", "station"):
            out.append({"kind": "station"})
            continue
        if part.startswith(("supply_v=", "supply_v:")):
            out.append({"kind": "supply_v", "to": float(part.split("=", 1)[-1].split(":", 1)[-1])})
            continue
        if part.startswith(("max_batch=", "max_batch:")):
            out.append({"kind": "max_batch", "to": int(part.split("=", 1)[-1].split(":", 1)[-1])})
            continue
        if ":" not in part:
            raise ValueError("%r: write station:resource+N (e.g. station-1:can+1)" % part)
        st, rest = part.split(":", 1)
        for item in [x.strip() for x in rest.split(",") if x.strip()]:
            if item.startswith("plate"):
                wh = item.split("=", 1)[-1].lower().replace(" ", "").split("x")
                if len(wh) != 2:
                    raise ValueError("%r: plate=WxH in mm" % item)
                out.append({"kind": "plate", "station": st.strip(), "to": [float(wh[0]), float(wh[1])]})
                continue
            if "+" in item:
                what, n = item.split("+", 1)
                rel = True
            elif "=" in item:
                what, n = item.split("=", 1)
                rel = False
            else:
                raise ValueError("%r: write resource+N or resource=N" % item)
            what = what.strip().lower().replace("-", "")
            what = {"analog": "ain", "inputs": "ain", "ainputs": "ain", "supplies": "vout", "supply": "vout",
                    "eth": "ethernet", "rs485": "rs485"}.get(what, what)
            try:
                n = int(n)
            except ValueError:
                raise ValueError("%r: N must be a whole number" % item)
            ch = {"station": st.strip(), "add" if rel else "to": n}
            if what in ("ain", "vout"):
                ch["kind"] = what
            elif what in E.BUSES:
                ch.update(kind="bus", bus=what)
            else:
                raise ValueError("%r: unknown resource %r (ain, supplies, %s, plate)" % (item, what, ", ".join(E.BUSES)))
            out.append(ch)
    return out


def resolve(changes: Sequence[Dict[str, Any]], models: Sequence[Dict[str, Any]], spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Relative changes ("add") made absolute ("to") against the stations as they are; a "station" change designed."""
    out = []
    names = [m["name"] for m in models]
    ms, sp = list(models), spec
    for ch in changes:
        ch = dict(ch)
        if ch["kind"] in ("ain", "vout", "bus", "plate") and ch.get("station") not in names:
            raise ValueError("no station %r in this environment (%s)" % (ch.get("station"), ", ".join(names)))
        if "add" in ch:
            m = next(x for x in ms if x["name"] == ch["station"])
            have = int(m["res"].get(ch["kind"], 0)) if ch["kind"] != "bus" else int((m["res"].get("buses") or {}).get(ch["bus"], 0))
            n = int(ch.pop("add"))
            if ch["kind"] == "ain":
                g = max(1, int(sp.get("ain_group", 4)))
                n = int(math.ceil(n / float(g))) * g
            ch["to"] = have + n
        out.append(ch)
        if ch["kind"] != "station":
            ms, sp = apply_change(ms, sp, ch)
    return out


def plan_goal(env: Dict[str, Any], fit: Sequence[str] = (), batch: Optional[Dict[str, int]] = None,
              budget: Optional[float] = None, changes: Sequence[Dict[str, Any]] = (),
              spec: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The cheapest changes that make the boards in ``fit`` fit and give the boards in ``batch`` that batch (plus
    any ``changes`` you set yourself), with what they gain and cost. A goal no station change can meet is listed
    in "cannot" with the reason; with ``budget`` the goals are taken by priority while they stay within it."""
    ctx = Context(env, spec)
    by_name = {}
    for b in ctx.boards:
        by_name[b["name"].lower()] = b
        by_name[b["folder"].lower()] = b
    goals: List[Tuple[Dict[str, Any], int]] = []
    unknown = []
    for name in fit or []:
        b = by_name.get(str(name).lower())
        if b is None:
            unknown.append(name)
        else:
            goals.append((b, max(1, int(b.get("batch") or 0)) if b.get("status") == "compatible" else 1))
    for name, n in (batch or {}).items():
        b = by_name.get(str(name).lower())
        if b is None:
            unknown.append(name)
        else:
            goals.append((b, int(n)))
    goals.sort(key=lambda g: -ctx.profs[g[0]["folder"]]["priority"])
    models, sp = ctx.models, ctx.spec
    own = resolve(changes, models, sp) if changes else []
    for ch in own:
        if ch["kind"] == "station":
            unfit = [ctx.profs[b["folder"]] for b in ctx.boards if ctx.where(models, sp)[b["folder"]][0] is None
                     and ctx.profs[b["folder"]]["status"] == "ok" and not E.hard_issue(ctx.profs[b["folder"]], sp)]
            name = "station-%d" % (len(models) + 1)
            ch.update(name=name, plate=[float(v) for v in sp["plate_mm"]], res=E.design_station(unfit, sp, sp["plate_mm"], name),
                      **{"for": len(unfit)})
        models, sp = apply_change(models, sp, ch, ctx)
    chosen: List[Dict[str, Any]] = list(own)
    cannot, met = [], []
    w0 = ctx.where(ctx.models, ctx.spec)
    start = ctx.metrics(ctx.models, ctx.spec, w0)
    for b, n in goals:
        w = ctx.where(models, sp)
        st, have = w[b["folder"]]
        if st and have >= n:
            met.append({"board": b["name"], "batch": have, "station": st, "note": "already"})
            continue
        best = None
        cands = [m for m in models if m["name"] == st] if st else models
        for m in cands:
            need, fixed = _needs_for(ctx, b, m, sp, n)
            if fixed:
                continue
            if not need:
                continue
            try:
                cost, ms2, sp2 = _cost_of(ctx, need, models, sp)
            except (StopIteration, KeyError):
                continue
            got = ctx.where(ms2, sp2)[b["folder"]]
            if got[1] < n:                                # the estimate says the change is not enough on its own
                continue
            if best is None or cost < best[0]:
                best = (cost, need, ms2, sp2, m["name"])
        if best is None:
            p = ctx.profs[b["folder"]]
            cannot.append({"board": b["name"], "batch": n, "reason": E.hard_issue(p, sp) or
                           "no change of one station gives it a batch of %d (the plate, max_batch or limits)" % n})
            continue
        if budget is not None:
            spent = change_cost(ctx, ctx.models, ctx.spec, w0, best[2], best[3], ctx.where(best[2], best[3]))
            if spent > float(budget) + 1e-6:
                cannot.append({"board": b["name"], "batch": n, "reason": "over the budget: %s %.0f in all" % (
                    costs(sp)["currency"], spent)})
                continue
        chosen += best[1]
        models, sp = best[2], best[3]
        met.append({"board": b["name"], "batch": n, "station": best[4]})
    w1 = ctx.where(models, sp)
    after = ctx.metrics(models, sp, w1)
    return {"currency": costs(sp)["currency"], "changes": [dict(c_, text=describe_change(c_, ctx.models, ctx.spec))
                                                          for c_ in _merge_changes(chosen)],
            "met": met, "cannot": cannot, "unknown": unknown,
            "gain": _gain(ctx, start, after, w0, w1, change_cost(ctx, ctx.models, ctx.spec, w0, models, sp, w1)),
            "before": {k: v for k, v in start.items() if k != "cost_parts"},
            "after": {k: v for k, v in after.items() if k != "cost_parts"},
            "flags": sorted({f for m in models if m.get("changed") for f in _limit_flags(m, sp)})}


def _merge_changes(chs: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One change per station and resource (the largest)."""
    out: Dict[Tuple, Dict[str, Any]] = {}
    for ch in chs:
        key = (ch["kind"], ch.get("station"), ch.get("bus"), ch.get("name"))
        old = out.get(key)
        if old is None:
            out[key] = dict(ch)
        elif ch["kind"] == "plate":
            old["to"] = [max(old["to"][0], ch["to"][0]), max(old["to"][1], ch["to"][1])]
        elif "to" in ch:
            old["to"] = max(old["to"], ch["to"])
    return list(out.values())


def changed_stations(env: Dict[str, Any], changes: Sequence[Dict[str, Any]],
                     spec: Optional[Dict[str, Any]] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """The stations (as models) and the spec after ``changes``: what a build with the upgrade makes."""
    ctx = Context(env, spec)
    ms, sp = ctx.models, ctx.spec
    for ch in resolve(changes, ms, sp):
        if ch["kind"] == "station" and "res" not in ch:
            w = ctx.where(ms, sp)
            unfit = [ctx.profs[f] for f, (s, _n) in w.items() if s is None and ctx.profs[f]["status"] == "ok"
                     and not E.hard_issue(ctx.profs[f], sp)]
            name = "station-%d" % (len(ms) + 1)
            ch.update(name=name, plate=[float(v) for v in sp["plate_mm"]], res=E.design_station(unfit, sp, sp["plate_mm"], name))
        ms, sp = apply_change(ms, sp, ch, ctx)
    sp.pop("_changed", None)
    return ms, sp


# --------------------------------------------------------------------------- #
# documents
# --------------------------------------------------------------------------- #
def _money(v: Optional[float], cur: str) -> str:
    return "-" if v is None else "%s %s" % (cur, "{:,.0f}".format(float(v)))


def constraints_md(env: Dict[str, Any], cons: Sequence[Dict[str, Any]]) -> str:
    cur = costs(env.get("spec") or {})["currency"]
    L = ["# %s: what limits each board" % (env.get("name") or "Test environment"), "",
         "For every board: what each resource of its station allows (the binding ones in **bold**), what one more "
         "board per fixture would need and cost (the station's new parts and this board's new fixture); for every "
         "board that fits none, what it lacks on each station and "
         "the cheapest change that would take it. Estimated from the board profiles; `upgrades.md` has the changes "
         "ranked, `pcbtestkit environment upgrade` makes them.", ""]
    ok = [c for c in cons if c["status"] == "compatible"]
    no = [c for c in cons if c["status"] != "compatible"]
    L += ["## Boards that fit (%d)" % len(ok), "", "| board | station | batch | limits (boards each allows) | one more per fixture |",
          "|---|---|---:|---|---|"]
    for c in ok:
        lim = ", ".join(("**%s %d**" if x["binds"] else "%s %d") % (x["what"], x["allows"]) for x in c.get("limits") or [])
        nxt = c.get("next")
        L.append("| %s | %s | %d | %s | %s |" % (c["board"], c["station"], c["batch"], lim or "-",
                                                ("%s (%s)" % ("; ".join(nxt["changes"]), _money(nxt["cost"], cur))
                                                 if nxt else "-")))
    L += ["", "## Boards that fit no station (%d)" % len(no), "",
          "| board | what it lacks | cheapest change that takes it |", "|---|---|---|"]
    for c in no:
        lacks = "; ".join("%s: %s" % (s, ", ".join(v.get("lacks") or ["-"])) for s, v in (c.get("stations") or {}).items()) \
            or (c.get("reason") or "-")
        tf = c.get("to_fit") or {}
        fix = ("%s (%s)" % ("; ".join(tf["changes"]), _money(tf.get("cost"), cur)) if tf.get("changes")
               else tf.get("note") or c.get("reason") or "-")
        L.append("| %s | %s | %s |" % (c["board"], lacks.replace("|", "/"), str(fix).replace("|", "/")))
    return "\n".join(L) + "\n"


def upgrades_md(env: Dict[str, Any], opt: Dict[str, Any]) -> str:
    cur = opt["currency"]
    now = opt["now"]
    L = ["# %s: upgrade options" % (env.get("name") or "Test environment"), "",
         "Now: %d of %d boards fit, %d boards per full load; worth %s, costs %s (stations %s, programmers %s, "
         "fixtures %s)." % (now["boards_fit"], now["boards"], now["slots"], _money(now["worth"], cur), _money(now["cost"], cur),
                            _money(sum(now["cost_parts"]["stations"].values()), cur), _money(now["cost_parts"]["programmers"], cur),
                            _money(now["cost_parts"]["fixtures"], cur)), "",
         "Make one: `pcbtestkit environment upgrade DIR --option U1 [--option U3 ...]`, a path: `--path value` "
         "(or `fit`, `batch`; `--steps N` for its first N), your own: `--add station-1:can+1,ain+8` or what you need: "
         "`--fit board_a,board_b --batch board_c=6 [--budget 500]`. It shows what it gains and costs; `--apply` "
         "builds it (the stations keep every pin they have, so the fixtures you have still plug in).", "",
         "## Single changes, best first", "", "| id | change | boards that fit | boards per load | worth | cost | net | notes |",
         "|---|---|---:|---:|---:|---:|---:|---|"]
    for o in opt["options"]:
        g = o["gain"]
        notes = []
        if g["fit"]:
            notes.append("fits " + ", ".join(g["fit"][:6]) + (" +%d" % (len(g["fit"]) - 6) if len(g["fit"]) > 6 else ""))
        if g["batches_up"]:
            notes.append("bigger batches: " + ", ".join("%s %d->%d" % (x["board"], x["from"], x["to"]) for x in g["batches_up"][:4])
                         + (" +%d" % (len(g["batches_up"]) - 4) if len(g["batches_up"]) > 4 else ""))
        notes += o.get("flags") or []
        L.append("| %s | %s | %+d | %+d | %s | %s | %s | %s |" % (o["id"], o["text"], g["boards_fit"], g["slots"],
                                                             _money(g["worth"], cur), _money(g["cost"], cur),
                                                             _money(g["net"], cur), "; ".join(notes).replace("|", "/")))
    if opt.get("more_options"):
        L.append("")
        L.append("%d more in upgrades.json." % opt["more_options"])
    for goal, title in (("value", "Best value"), ("fit", "Most boards"), ("batch", "Biggest batches")):
        p = opt["paths"].get(goal) or {}
        L += ["", "## Path: %s (`--path %s`)" % (title.lower(), goal), ""]
        if not p.get("steps"):
            L.append("Nothing gains here.")
            continue
        for i, s in enumerate(p["steps"], 1):
            g = s["gain"]
            L.append("%d. %s: %+d boards, %+d per load, net %s" % (i, s["text"], g["boards_fit"], g["slots"], _money(g["net"], cur)))
        t = p["total"]
        L.append("")
        L.append("In all: %+d boards that fit, %+d boards per load, costs %s, net %s; then %d of %d boards fit." % (
            t["boards_fit"], t["slots"], _money(t["cost"], cur), _money(t["net"], cur), p["after"]["boards_fit"], now["boards"]))
    L += ["", "---", ""] + ["- " + n for n in opt.get("notes") or []]
    return "\n".join(L) + "\n"


def constraints_csv(cons: Sequence[Dict[str, Any]]) -> str:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["board", "status", "station", "batch", "limited_by", "limits", "next_batch", "next_changes", "next_cost",
                "to_fit_station", "to_fit_changes", "to_fit_cost", "reason"])
    for c in cons:
        nx = c.get("next") or {}
        tf = c.get("to_fit") or {}
        w.writerow([c["board"], c["status"], c.get("station") or "", c.get("batch") or 0, "; ".join(c.get("limited_by") or []),
                    "; ".join("%s=%d" % (x["what"], x["allows"]) for x in c.get("limits") or []),
                    nx.get("batch") or "", "; ".join(nx.get("changes") or []), nx.get("cost") if nx.get("cost") is not None else "",
                    tf.get("station") or "", "; ".join(tf.get("changes") or []) or tf.get("note") or "",
                    tf.get("cost") if tf.get("cost") is not None else "", c.get("reason") or ""])
    return buf.getvalue()


def write(env: Dict[str, Any], out_dir: str) -> Dict[str, str]:
    """constraints.json/.md/.csv and upgrades.json/.md into the environment's folder."""
    import os
    cons = constraints(env)
    opt = options(env)
    files = {"constraints": "constraints.md", "constraints_json": "constraints.json", "constraints_csv": "constraints.csv",
             "upgrades": "upgrades.md", "upgrades_json": "upgrades.json"}
    with open(os.path.join(out_dir, files["constraints_json"]), "w", encoding="utf-8") as fh:
        json.dump({"schema": "pcbtestkit-constraints/1", "boards": cons}, fh, indent=1)
    with open(os.path.join(out_dir, files["constraints"]), "w", encoding="utf-8") as fh:
        fh.write(constraints_md(env, cons))
    with open(os.path.join(out_dir, files["constraints_csv"]), "w", encoding="utf-8", newline="") as fh:
        fh.write(constraints_csv(cons))
    with open(os.path.join(out_dir, files["upgrades_json"]), "w", encoding="utf-8") as fh:
        json.dump(dict(opt, schema="pcbtestkit-upgrades/1"), fh, indent=1)
    with open(os.path.join(out_dir, files["upgrades"]), "w", encoding="utf-8") as fh:
        fh.write(upgrades_md(env, opt))
    env["_constraints"], env["_upgrades"] = cons, opt
    return files


def html_sections(env: Dict[str, Any]) -> str:
    """The environment page's part on constraints and upgrades (from write())."""
    import html as H
    e = H.escape
    cons, opt = env.get("_constraints"), env.get("_upgrades")
    if not cons or not opt:
        return ""
    cur = opt["currency"]
    tally: Dict[str, int] = {}
    for c in cons:
        for w in c.get("limited_by") or []:
            tally[w] = tally.get(w, 0) + 1
    no = [c for c in cons if c["status"] != "compatible"]
    h = ["<h2 id='limits'>What limits the boards</h2><p class='muted'>What each board's batch is limited by on its station, and what "
         "the boards that fit none lack. Per board, with what one more board per fixture would need and cost: "
         "<a href='constraints.md'>constraints.md</a> (<a href='constraints.csv'>csv</a>, "
         "<a href='constraints.json'>json</a>).</p><div class='wrap'><table><tr><th>Limited by</th><th>Board types</th></tr>"]
    for w, n in sorted(tally.items(), key=lambda kv: -kv[1])[:12]:
        h.append("<tr><td>%s</td><td>%d</td></tr>" % (e(w), n))
    h.append("</table></div>")
    if no:
        h.append("<div class='wrap' style='margin-top:10px'><table><tr><th>Fits no station</th><th>Cheapest change that "
                 "takes it</th></tr>")
        for c in no[:20]:
            tf = c.get("to_fit") or {}
            fix = ("; ".join(tf["changes"]) + " (%s)" % _money(tf.get("cost"), cur)) if tf.get("changes") else \
                (tf.get("note") or c.get("reason") or "-")
            h.append("<tr><td>%s</td><td>%s</td></tr>" % (e(c["board"]), e(fix)))
        h.append("</table></div>")
    now = opt["now"]
    h.append("<h2 id='upgrades'>Upgrade options</h2><p class='muted'>Now %d of %d boards fit, %d per full load; the environment costs "
             "about %s (stations, programmers, fixtures). One change at a time, best first; worth: %s per board type "
             "that fits (value_per_board), more for bigger batches; cost: the new parts and a new fixture for every "
             "board whose batch grows. <a href='upgrades.md'>upgrades.md</a> has the paths and how to make them "
             "(<code>pcbtestkit environment upgrade</code>).</p><div class='wrap'><table><tr><th>id</th><th>Change</th>"
             "<th>Boards that fit</th><th>Boards per load</th><th>Worth</th><th>Cost</th><th>Net</th></tr>" % (
                 now["boards_fit"], now["boards"], now["slots"], _money(now["cost"], cur), _money(opt["value_per_board"], cur)))
    for o in opt["options"][:10]:
        g = o["gain"]
        h.append("<tr><td>%s</td><td>%s%s</td><td>%+d</td><td>%+d</td><td>%s</td><td>%s</td><td class='%s'>%s</td></tr>" % (
            e(o["id"]), e(o["text"]), ("<div class='muted'>%s</div>" % e("; ".join(o["flags"]))) if o.get("flags") else "",
            g["boards_fit"], g["slots"], _money(g["worth"], cur), _money(g["cost"], cur), "ok" if g["net"] > 0 else "no",
            _money(g["net"], cur)))
    h.append("</table></div>")
    if not opt["options"]:
        h.append("<p>No single change lets another board fit or a batch grow.</p>")
    return "".join(h)
