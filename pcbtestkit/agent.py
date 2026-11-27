"""AI agent API: a validated, token-efficient tool layer over the toolkit.

Design rules (why this exists instead of "let the model edit JSON"):

* The model makes *decisions*; deterministic code does geometry, test-point
  selection, dividers, KiCad files and meshes. Decisions are small typed
  writes (``set_net_specs``, ``set_supply``, ``add_test_step``...) persisted
  with a rationale in ``pcbtestkit.json``; outputs are regenerated from them.
* ``get_open_decisions`` tells the model exactly what needs judgement, with
  evidence, a suggested value and ready-to-send arguments. No exploring.
* Reads are compact, filterable and paginated; responses are terse JSON.
* Every write is validated (unknown names -> did-you-mean, plausibility,
  conflicts with net names, ADC range, probe access). Risky overrides need
  ``confirm: true``. All writes support ``dry_run`` and ``undo_last``.

Transport-neutral: use :class:`Workspace` directly, via the MCP server
(:mod:`pcbtestkit.mcp_server`), via ``pcbtestkit api`` (shell), or export the
tool schemas for any function-calling model (``pcbtestkit tools``).
"""
from __future__ import annotations

import copy
import datetime
import difflib
import fnmatch
import json
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import __version__
from . import classify as C
from .config import DEFAULTS, FAB_PROFILES, POGO_PROFILES, config_path_for, load_user_config, save_user_config

GUIDE = """PCB TestKit agent workflow (follow in order, batch your writes):
1. get_overview -> board, rails, supply, readiness, open-decision count.
2. get_open_decisions -> each decision has evidence, a suggestion and example_args for the tool to call.
3. Resolve decisions with the set_* tools. Batch: set_net_specs takes many nets at once. Give a short rationale.
   Use list_nets / get_net / get_component / list_findings only when a decision needs more evidence.
4. Optionally add_test_step for functional checks (they can only measure nets that have a probe channel);
   set_batch_and_programming for boards per batch and how each board is programmed on the fixture.
5. validate -> fix every error; warnings are advisory.
6. generate_outputs -> spec JSON, test plan, fixture .kicad_pcb, solder jig STL/SCAD, HTML report, Pi runner.
Rules: never invent net names or refs (use the names returned by the tools); units are in field names
(_v volts, _mm, _a amps, _pct percent); if a write returns CONFIRM_REQUIRED, re-send with confirm=true only if
you are sure; undo_last reverts the previous write. Writes return effects (coverage/channels) - read them."""


class ToolError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


# =========================================================================== #
# Tool registry
# =========================================================================== #
TOOLS: Dict[str, Dict[str, Any]] = {}


def tool(name: str, description: str, schema: Dict[str, Any], read_only: bool = True, writes: bool = False):
    schema = dict(schema)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    schema.setdefault("additionalProperties", False)

    def deco(fn: Callable):
        TOOLS[name] = {"name": name, "description": description, "input_schema": schema, "fn": fn,
                       "read_only": read_only, "writes": writes}
        return fn
    return deco


WRITE_COMMON = {
    "rationale": {"type": "string", "description": "One short sentence: why (stored in the audit log)."},
    "dry_run": {"type": "boolean", "description": "Validate and report effects without saving."},
    "confirm": {"type": "boolean", "description": "Override CONFIRM_REQUIRED warnings."},
}

V_NUM = {"type": ["number", "string"], "description": "Volts; number or text like '3V3', '3.3V', '1800mV'."}


def parse_v(v: Any, field: str) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, bool):
        raise ToolError("BAD_VALUE", "%s must be a number" % field)
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip().upper().replace(" ", "")
    m = re.match(r"^([+-]?\d+(?:\.\d+)?)MV$", t)
    if m:
        return float(m.group(1)) / 1000.0
    m = re.match(r"^([+-]?)(\d+)V(\d+)$", t)
    if m:
        return float("%s%s.%s" % (m.group(1), m.group(2), m.group(3)))
    m = re.match(r"^([+-]?\d+(?:\.\d+)?)V?$", t)
    if m:
        return float(m.group(1))
    raise ToolError("BAD_VALUE", "cannot parse %s=%r as a voltage" % (field, v), hint="use a number in volts, e.g. 3.3")


def _num(v: Any, field: str, lo: float, hi: float) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ToolError("BAD_VALUE", "%s must be a number" % field)
    if not (lo <= f <= hi):
        raise ToolError("OUT_OF_RANGE", "%s=%g outside the plausible range [%g, %g]" % (field, f, lo, hi))
    return f


# =========================================================================== #
# Workspace
# =========================================================================== #
class Workspace:
    def __init__(self, project: Optional[str] = None, persist: bool = True, actor: str = "ai"):
        self.persist = persist
        self.actor = actor
        self.design = None
        self.history: List[Dict[str, Any]] = []
        self._findings = None
        self._plan = None
        if project:
            self.open(project)

    # ---------------------------------------------------------------- core
    def open(self, path: str) -> None:
        from .project import Design
        self.design = Design.load(path)
        self.history = self._load_undo()
        self._invalidate()

    # Undo survives between CLI calls and MCP sessions: previous configs are kept next to the project.
    UNDO_DEPTH = 20

    def _undo_path(self) -> Optional[str]:
        d = self.design
        if not (self.persist and d is not None and d.config_path):
            return None
        return os.path.join(os.path.dirname(d.config_path), ".pcbtestkit", "undo.json")

    def _load_undo(self) -> List[Dict[str, Any]]:
        p = self._undo_path()
        if not p or not os.path.exists(p):
            return []
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return [c for c in data if isinstance(c, dict)][-self.UNDO_DEPTH:] if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save_undo(self) -> None:
        p = self._undo_path()
        if not p:
            return
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(self.history[-self.UNDO_DEPTH:], fh)

    def _d(self):
        if self.design is None:
            raise ToolError("NO_PROJECT", "no project open", hint="call open_project with a .kicad_pcb/.kicad_pro path")
        return self.design

    def _invalidate(self) -> None:
        self._findings = None
        self._plan = None

    def analysis(self):
        if self._findings is None:
            from .pipeline import analyze
            self._findings, self._plan = analyze(self._d(), include_kicad=False)
        return self._findings, self._plan

    def plan(self):
        return self.analysis()[1]

    def call(self, name: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Run a tool; always returns a dict with ok=True/False (never raises)."""
        t = TOOLS.get(name)
        if t is None:
            return {"ok": False, "error": {"code": "UNKNOWN_TOOL", "message": "no tool named %r" % name,
                                           "did_you_mean": difflib.get_close_matches(name, list(TOOLS), 3)}}
        args = dict(args or {})
        try:
            _check_args(t["input_schema"], args)
            res = t["fn"](self, **args)
            out = {"ok": True}
            out.update(res or {})
            return out
        except ToolError as e:
            err = {"code": e.code, "message": e.message}
            err.update(e.extra)
            return {"ok": False, "error": err}
        except Exception as e:  # never leak a traceback into the model context
            return {"ok": False, "error": {"code": "INTERNAL", "message": "%s: %s" % (type(e).__name__, e)}}

    # ---------------------------------------------------------------- names
    def resolve_net(self, name: str) -> str:
        d = self._d()
        nets = d.board.net_names()
        if name in nets:
            return name
        cands = [n for n in nets if n.lstrip("/") == str(name).lstrip("/")]
        if len(cands) == 1:
            return cands[0]
        low = [n for n in nets if n.lower() == str(name).lower() or C.base_net(n).lower() == str(name).lower().lstrip("/")]
        if len(low) == 1:
            return low[0]
        sugg = difflib.get_close_matches(str(name), nets, 5, 0.5) or [n for n in nets if str(name).lower() in n.lower()][:5]
        raise ToolError("UNKNOWN_NET", "net %r not found" % name, did_you_mean=sugg, hint="list_nets(query=...) shows exact names")

    def resolve_ref(self, ref: str):
        d = self._d()
        fp = d.board.by_ref.get(ref) or d.board.by_ref.get(str(ref).upper())
        if fp is None:
            raise ToolError("UNKNOWN_REF", "component %r not found" % ref,
                            did_you_mean=difflib.get_close_matches(str(ref).upper(), list(d.board.by_ref), 5, 0.5))
        return fp

    # ---------------------------------------------------------------- writes
    def apply(self, mutate: Callable[[Dict[str, Any]], List[str]], rationale: str = "", dry_run: bool = False,
              confirm: bool = False, what: str = "") -> Dict[str, Any]:
        d = self._d()
        before_cfg = copy.deepcopy(d.user_config)
        new_cfg = copy.deepcopy(d.user_config)
        warnings = mutate(new_cfg) or []
        needs_confirm = [w for w in warnings if w.startswith("CONFIRM:")]
        if needs_confirm and not confirm:
            raise ToolError("CONFIRM_REQUIRED", "; ".join(w[8:].strip() for w in needs_confirm),
                            hint="re-send the same call with confirm=true if this is intended")
        warnings = [w[8:].strip() if w.startswith("CONFIRM:") else w for w in warnings]
        before = self._metrics()
        d.reload_config(new_cfg)
        self._invalidate()
        try:
            after = self._metrics()
        except Exception as exc:
            d.reload_config(before_cfg)
            self._invalidate()
            raise ToolError("INVALID_CONFIG", "the change makes the plan fail: %s" % exc)
        effects = {k: [before[k], after[k]] for k in after if before.get(k) != after[k]}
        if dry_run:
            d.reload_config(before_cfg)
            self._invalidate()
            return {"dry_run": True, "effects": effects, "warnings": warnings}
        entry = {"t": datetime.datetime.now().replace(microsecond=0).isoformat(), "by": self.actor, "what": what,
                 "why": (rationale or "")[:300]}
        log = new_cfg.setdefault("_audit", [])
        log.append(entry)
        del log[:-200]
        d.reload_config(new_cfg)
        self.history.append(before_cfg)
        del self.history[:-self.UNDO_DEPTH]
        if self.persist and d.config_path:
            save_user_config(d.config_path, new_cfg)
            self._save_undo()
        return {"saved": bool(self.persist), "effects": effects, "warnings": warnings}

    def _metrics(self) -> Dict[str, Any]:
        findings, plan = self.analysis()
        cov = plan["coverage"]
        crit = sum(1 for f in findings if f.severity == "critical" and not f.waived)
        maj = sum(1 for f in findings if f.severity == "major" and not f.waived)
        limited = sum(1 for st in plan["sequence"] for c in st.get("checks", []) or [] if c.get("min_v") is not None or c.get("min_ohm") is not None)
        learn = sum(1 for st in plan["sequence"] for c in st.get("checks", []) or [] if c.get("learn"))
        return {"coverage_pct": cov["percent"], "probes": len(plan["probes"]),
                "channels": sum(1 for p in plan["probes"] if p.get("channel")), "checks_with_limits": limited,
                "checks_learn": learn, "steps": len(plan["sequence"]), "critical": crit, "major": maj,
                "supply": (plan.get("supply") or {}).get("net")}


def _check_args(schema: Dict[str, Any], args: Dict[str, Any]) -> None:
    props = schema.get("properties", {})
    unknown = [k for k in args if k not in props]
    if unknown and schema.get("additionalProperties") is False:
        raise ToolError("UNKNOWN_ARGUMENT", "unknown argument(s): %s" % ", ".join(unknown),
                        allowed=sorted(props), did_you_mean={u: difflib.get_close_matches(u, list(props), 1) for u in unknown})
    missing = [k for k in schema.get("required", []) if k not in args]
    if missing:
        raise ToolError("MISSING_ARGUMENT", "missing required argument(s): %s" % ", ".join(missing))
    for k, v in args.items():
        p = props.get(k, {})
        if "enum" in p and v not in p["enum"]:
            raise ToolError("BAD_VALUE", "%s must be one of %s" % (k, p["enum"]))
        ty = p.get("type")
        types = ty if isinstance(ty, list) else [ty] if ty else []
        ok = not types or any(_is_type(v, t) for t in types)
        if not ok:
            raise ToolError("BAD_TYPE", "%s must be %s" % (k, "/".join(types)))


def _is_type(v, t) -> bool:
    return {"string": isinstance(v, str), "number": isinstance(v, (int, float)) and not isinstance(v, bool),
            "integer": isinstance(v, int) and not isinstance(v, bool), "boolean": isinstance(v, bool),
            "array": isinstance(v, list), "object": isinstance(v, dict), "null": v is None}.get(t, True)


def _page(rows: List[Any], limit: int, offset: int) -> Dict[str, Any]:
    total = len(rows)
    sl = rows[offset:offset + limit]
    out = {"total": total, "rows": sl}
    if offset + limit < total:
        out["next_offset"] = offset + limit
    return out


PAGING = {"limit": {"type": "integer", "description": "Max rows (default 50, max 500)."},
          "offset": {"type": "integer", "description": "Rows to skip (from next_offset)."}}


def _lim(limit, default=50):
    return max(1, min(500, int(limit or default)))


# =========================================================================== #
# Read tools
# =========================================================================== #
@tool("open_project", "Open a KiCad project (.kicad_pcb, .kicad_pro or folder). Needed once per session unless the server was started with --project.",
      {"properties": {"path": {"type": "string"}}, "required": ["path"]})
def t_open(ws: Workspace, path: str):
    ws.open(path)
    return {"board": ws.design.board.name, "config": ws.design.config_path}


@tool("get_agent_guide", "Short workflow and rules for using these tools. Call first if unsure.", {})
def t_guide(ws: Workspace):
    return {"guide": GUIDE, "version": __version__}


@tool("get_overview", "Compact board summary: size, layers, counts, rails with expected voltages, supply, readiness, coverage, open decisions.", {})
def t_overview(ws: Workspace):
    d = ws._d()
    b = d.board
    bb = b.outline_bbox()
    findings, plan = ws.analysis()
    from .analysis import summarize
    comps = d.components()
    rails = []
    for n in d.nets_of_kind("power"):
        s = d.net_spec(n)
        rails.append("%s=%s%s" % (n, "?" if s["nominal_v"] is None else "%g" % s["nominal_v"],
                                  "" if s["voltage_source"] in (None, "net name") else "(cfg)"))
    return {"board": b.name, "rev": b.title_block.get("rev", ""), "size_mm": [round(bb.w, 1), round(bb.h, 1)],
            "layers": len(b.copper_layers), "thickness_mm": b.thickness,
            "parts": {"total": len(comps), "smd_top": sum(1 for f in comps if f.is_smd and f.side == "top"),
                      "smd_bottom": sum(1 for f in comps if f.is_smd and f.side == "bottom"),
                      "tht": sum(1 for f in comps if f.is_tht),
                      "tht_bottom": sum(1 for f in comps if f.is_tht and f.side == "bottom")},
            "stacked_boards": [st["name"] for st in plan.get("stack") or []],
            "batch": (plan.get("batch") or {}).get("count", 1),
            "program_headers": ["%s:%s" % (h["ref"], h["protocol"]) for h in (plan.get("program") or {}).get("headers") or []],
            "nets": len(b.net_names()), "ground": d.nets_of_kind("ground"), "rails": rails,
            "supply": {k: plan["supply"].get(k) for k in ("net", "voltage", "reason")},
            "readiness": summarize(findings), "coverage_pct": plan["coverage"]["percent"],
            "channels": sum(1 for p in plan["probes"] if p.get("channel")),
            "tech": _tech_brief(d),
            "open_decisions": len(_decisions(ws)), "schematic": d.sch is not None}


def _tech_brief(d) -> Dict[str, Any]:
    t = d.technology()
    return {"system": t["system"], "needs": t["required"].get("class"), "fits": t["required"]["fits"], "ordered": t["target"],
            "ordered_ok": (t["target_result"] or {}).get("ok"),
            "checks": [{"system": c["system"], "class": (c.get("target") or {}).get("class"), "ok": (c.get("target") or {}).get("ok")}
                       for c in t["checks"]]}


def _regulator_hint(d, net: str) -> Optional[Tuple[float, str]]:
    for p in d.board.pads_by_net().get(net, []):
        fp = d.board.by_ref.get(p.ref)
        if fp is None:
            continue
        if p.pintype == "power_out" or C.is_ic(fp):
            v = fp.value.upper()
            m = re.search(r"(?:-|_)(\d{1,2})[.V](\d{1,2})\b", v) or re.search(r"(?:-|_)(\d{1,2})(?:V|$)", v)
            if m:
                val = float("%s.%s" % (m.group(1), m.group(2))) if m.lastindex == 2 else float(m.group(1))
                if 0.5 <= val <= 48:
                    return val, "%s (%s) drives this net" % (fp.ref, fp.value)
            m = re.search(r"78(?:L|M)?(\d\d)", v)
            if m and p.pintype == "power_out":
                return float(int(m.group(1))), "%s (%s) output" % (fp.ref, fp.value)
    return None


def _pullup_hint(d, net: str) -> Optional[str]:
    for p in d.board.pads_by_net().get(net, []):
        fp = d.board.by_ref.get(p.ref)
        if fp is None or not C.is_resistor(fp) or len(fp.pads) != 2:
            continue
        other = fp.pads[1] if fp.pads[0].number == p.number else fp.pads[0]
        s = d.net_spec(other.net)
        if d.net_kind(other.net) == "power" and s.get("nominal_v") is not None:
            return "pull-up %s (%s) to %s -> idle high ~%g V unless driven" % (fp.ref, fp.value, other.net, s["nominal_v"])
        if d.net_kind(other.net) == "ground":
            return "pull-down %s (%s) to %s -> idle low unless driven" % (fp.ref, fp.value, other.net)
    return None


def _decisions(ws: Workspace) -> List[Dict[str, Any]]:
    d = ws._d()
    findings, plan = ws.analysis()
    cfg_nets = d.cfg.get("nets") or {}
    out: List[Dict[str, Any]] = []

    def add(topic, priority, question, items, tool_name, example, **extra):
        e = {"id": "D%d" % (len(out) + 1), "topic": topic, "priority": priority, "question": question,
             "items": items, "apply_with": tool_name, "example_args": example}
        e.update(extra)
        out.append(e)

    tech = d.cfg.get("tech") or {}
    if not tech.get("class"):
        t = d.technology()
        req = t["required"]
        fits = [{"system": r["system"], "class": r["class"]} for r in d.class_overview() if r["fits"] and r["class"]]
        system = d.class_system(t["system"])
        suggestion = req.get("class") or (fits[0]["class"] if fits and fits[0]["system"] == t["system"] else system.all_classes()[-1])
        add("tech_class", "medium", "Which class system (fab or class table) and class will you order? The manufacturing checks "
            "then use its limits. Standards (IPC-2221 voltage spacing, IPC-6012 class) can be added with class_checks.",
            [{"system": t["system"], "required_class": req.get("class"), "fits": req["fits"], "services": req.get("services"),
              "drivers": [{k: x[k] for k in ("metric", "measured", "limit", "level")} for x in req.get("drivers", [])[:6]],
              "layers": t["layers"], "thickness_mm": t["thickness_mm"], "finish": t["finish"], "fits_as_drawn": fits[:10]}],
            "set_analysis_options", {"class_system": t["system"], "tech_class": suggestion, "tech_layers": t["layers"],
                                     "rationale": "coarsest class the layout fits"})
    sup = plan["supply"]
    if not (d.cfg.get("supply") or {}).get("net"):
        cands = []
        for n in d.nets_of_kind("power"):
            on = [fp.ref for fp in d.components() if C.is_connector(fp) and n in fp.nets()]
            if on:
                cands.append({"net": n, "v": d.net_spec(n).get("nominal_v"), "connectors": on[:4]})
        add("supply", "high", "Which net powers the board on the tester, at what voltage and current limit?",
            cands, "set_supply", {"net": sup.get("net") or (cands[0]["net"] if cands else "VIN"),
                                  "voltage": sup.get("voltage") or 5.0, "current_limit_a": 0.5,
                                  "rationale": "main input on the power connector"},
            auto_choice=sup.get("net"), auto_reason=sup.get("reason"))
    unknown, unconfirmed = [], []
    for n in d.nets_of_kind("power"):
        s = d.net_spec(n)
        if s["nominal_v"] is None and s.get("min_v") is None:
            item = {"net": n, "pins": _pins_brief(d, n, 6)}
            h = _regulator_hint(d, n)
            if h:
                item["hint_v"], item["hint"] = h
            unknown.append(item)
        elif n not in cfg_nets:
            unconfirmed.append({"net": n, "v": s["nominal_v"], "from": s["voltage_source"], "tol_pct": s["tolerance_pct"]})
    if unknown:
        add("rail_voltage", "high", "Expected voltage (and tolerance) of rails whose voltage is not in the name.",
            unknown, "set_net_specs", {"updates": [{"net": u["net"], "nominal_v": u.get("hint_v", 3.3), "tolerance_pct": 5}
                                                   for u in unknown[:3]], "rationale": "from regulator part numbers"})
    if unconfirmed:
        add("confirm_rails", "medium", "Confirm or adjust rail limits inferred from net names (default +/-%g%%)."
            % d.cfg["testplan"]["default_tolerance_pct"], unconfirmed, "set_net_specs",
            {"updates": [{"net": u["net"], "nominal_v": u["v"], "tolerance_pct": 5} for u in unconfirmed[:3]],
             "rationale": "regulator accuracy +/-2% plus load"})
    learn = []
    for st in plan["sequence"]:
        for c in st.get("checks", []) or []:
            if c.get("learn"):
                it = {"net": c["net"], "channel": c["channel"]}
                h = _pullup_hint(d, c["net"])
                if h:
                    it["hint"] = h
                learn.append(it)
    if learn:
        add("signal_limits", "low", "Signal checks without limits will be learned from a golden unit. Set ranges where the "
            "idle level is known (pull-ups, enables), or leave them to learn mode.", learn, "set_net_specs",
            {"updates": [{"net": learn[0]["net"], "min_v": 2.8, "max_v": 3.5}], "rationale": "pulled up to 3V3 at idle"})
    sc = d.cfg.get("supply") or {}
    if sc.get("max_idle_current_a") is None and sup.get("net"):
        add("idle_current", "medium", "Maximum (and optional minimum) idle supply current of a good board. Catches shorts, "
            "missing/reversed parts. Needs a current sensor on the tester; skip if none.", [], "set_supply",
            {"net": sup.get("net"), "max_idle_current_a": 0.05, "min_idle_current_a": 0.002,
             "rationale": "MCU idle + LDO quiescent current"})
    from .stack import candidates as stack_candidates
    if not d.cfg.get("stack"):
        cands = stack_candidates(d)
        if cands:
            strong = [c for c in cands if not c["why"].startswith("pin socket")]
            c0 = (strong or cands)[0]
            add("stacked_boards", "medium" if strong else "low",
                "Is a board stacked on any of these parts (daughter board, shield, display)? If so, give its outline, "
                "height above this board and whether it is fitted during the test: probes then stay clear of it and the "
                "fixture gets a window. Answer with an empty list if not.", cands, "set_stacked_boards",
                {"boards": [{"name": "daughter", "refs": [c0["ref"]], "outline": c0["outline"],
                             "height_mm": round(c0["height_mm"] + 4.6, 1), "fitted_at_test": True}],
                 "rationale": "display board plugs into %s" % c0["ref"]})
    est = [st for st in plan.get("stack") or [] if st.get("valid") and
           (str(st.get("outline_source", "")).startswith("estimate") or str(st.get("height_source", "")).startswith("estimate"))]
    if est:
        add("confirm_stack", "medium", "Outline and height of these stacked boards are estimates. Give the real outline "
            "(board mm, KiCad coordinates) and height of the top of the stack above this board.",
            est, "set_stacked_boards", {"boards": [{"name": st["name"], "refs": st["refs"], "outline": st["outline"],
                                                    "height_mm": st["height_mm"], "fitted_at_test": st["fitted_at_test"]}
                                                   for st in est]})
    hint = (plan.get("probe_side_hint") or {}).get("both_sides_reach")
    if hint:
        add("probe_both_sides", "medium", "These power nets can only be reached from the other side. Probe from both "
            "sides (two fixture plates, the board between them) or add test pads?",
            [{"net": n} for n in hint], "set_test_options", {"probe_side": "both", "rationale": "rails only reachable on the other side"})
    ev = plan.get("probe_side_eval") or {}
    if ev:
        other = "top" if plan["probe_side"] == "bottom" else "bottom"
        if ev[other]["power_nets"] > ev[plan["probe_side"]]["power_nets"]:
            add("probe_side", "medium", "The %s side was chosen (test pads, fewer probes on solder joints) but the %s side "
                "reaches more rails. Which side should the fixture probe?" % (plan["probe_side"], other),
                [dict(side=k, **v) for k, v in ev.items()], "set_test_options",
                {"probe_side": plan["probe_side"], "rationale": "dedicated test pads on this side"})
    pw_unt = [u for u in plan["coverage"]["untestable"] if d.net_kind(u["net"]) in ("power", "ground")]
    if pw_unt:
        add("test_access", "high", "These power nets cannot be probed. Either add test pads in the layout (design "
            "change, preferred) or explicitly exclude them from the test.", pw_unt, "set_test_options",
            {"exclude_nets": [u["net"] for u in pw_unt], "rationale": "covered indirectly by downstream rail checks"})
    if any("channel limit" in u["reason"] for u in plan["coverage"]["untestable"]):
        add("channel_budget", "medium", "Channel limit reached; raise max_channels or narrow the signal policy.",
            [u for u in plan["coverage"]["untestable"] if "channel limit" in u["reason"]][:20], "set_test_options",
            {"max_channels": d.cfg["testplan"]["max_channels"] + 16})
    from .jig import select_parts
    sel = select_parts(d, "top")[0] + select_parts(d, "bottom")[0]
    weak_h = []
    confirmed = set(((d.cfg.get("jig") or {}).get("heights") or {}).keys())
    for fp in sel:
        if fp.ref in confirmed:
            continue
        h, src = d.height_of(fp)
        weak_h.append({"ref": fp.ref, "value": fp.value, "mpn": C.mpn_of(fp) or None, "footprint": fp.name,
                       "side": fp.side, "height_mm": round(h, 2), "source": src})
    if weak_h:
        add("part_heights", "medium", "Confirm the body height above the PCB (mm) of each hand-placed part: the jig "
            "pocket depth equals it. Library 3D models are often generic (e.g. all radial caps as tall as wide) - use "
            "the datasheet of the actual part number.", weak_h, "set_jig_options",
            {"heights": {weak_h[0]["ref"]: weak_h[0]["height_mm"]}, "rationale": "datasheet body height"})
    if not isinstance(d.cfg["jig"].get("parts"), list):
        add("jig_parts", "low", "Parts the solder jig holds for hand soldering (auto = all through-hole parts).",
            [{"ref": f.ref, "value": f.value} for f in sel], "set_jig_options", {"parts": [f.ref for f in sel]})
    serious = [f for f in findings if f.severity in ("critical", "major") and not f.id.startswith("TEST-")]
    if serious:
        add("triage_findings", "medium", "Critical/major weak points: fix them in KiCad, or waive with a reason if "
            "acceptable for this product.", [{"uid": f.uid, "id": f.id, "sev": f.severity, "title": f.title} for f in serious],
            "waive_finding", {"check_id": serious[0].id, "reason": "accepted: <why>"})
    return out


def _pins_brief(d, net: str, n: int) -> List[str]:
    out = []
    for p in d.board.pads_by_net().get(net, [])[:n]:
        fp = d.board.by_ref.get(p.ref)
        out.append("%s(%s%s)" % (p.name, fp.value if fp else "", (":" + p.pinfunction) if p.pinfunction else ""))
    return out


@tool("get_open_decisions", "What still needs engineering judgement, highest priority first. Each item has evidence, a "
      "suggestion and example_args for the tool named in apply_with.",
      {"properties": {"topic": {"type": "string", "description": "Only this topic (e.g. rail_voltage)."}}})
def t_decisions(ws: Workspace, topic: Optional[str] = None):
    ds = _decisions(ws)
    if topic:
        ds = [x for x in ds if x["topic"] == topic]
    rank = {"high": 0, "medium": 1, "low": 2}
    ds.sort(key=lambda x: rank.get(x["priority"], 9))
    return {"count": len(ds), "decisions": ds}


@tool("list_nets", "List nets as compact rows [name, kind, nominal_v, min_v, max_v, pads, probe, channel].",
      dict(properties=dict(PAGING, **{
          "kind": {"type": "string", "enum": ["power", "ground", "signal", "unconnected", "any"]},
          "query": {"type": "string", "description": "Substring or glob (e.g. '*SDA*')."},
          "probed": {"type": "boolean", "description": "true = only nets with a probe, false = only without."}})))
def t_list_nets(ws: Workspace, kind: str = "any", query: Optional[str] = None, probed: Optional[bool] = None,
                limit: Optional[int] = None, offset: int = 0):
    d = ws._d()
    plan = ws.plan()
    probes = {}
    for p in plan["probes"]:
        probes.setdefault(p["net"], p)
    pads = d.board.pads_by_net()
    rows = []
    for n in d.board.net_names():
        k = d.net_kind(n)
        if k == "unconnected" and kind != "unconnected":
            continue
        if kind not in ("any", None) and k != kind:
            continue
        if query and not (fnmatch.fnmatch(n.lower(), query.lower()) or query.lower() in n.lower()):
            continue
        pr = probes.get(n)
        if probed is not None and bool(pr) != probed:
            continue
        s = d.net_spec(n)
        rows.append([n, k, s["nominal_v"], s["min_v"], s["max_v"], len(pads.get(n, [])), pr["id"] if pr else None,
                     pr.get("channel") if pr else None])
    rows.sort(key=lambda r: ({"power": 0, "ground": 1, "signal": 2}.get(r[1], 3), C.natural_key(r[0])))
    out = _page(rows, _lim(limit), int(offset or 0))
    out["columns"] = ["net", "kind", "nominal_v", "min_v", "max_v", "pads", "probe", "channel"]
    return out


@tool("get_net", "Details of one net: spec, connected pins (ref.pad value:function/type), probe and why it is (not) probed.",
      {"properties": {"net": {"type": "string"}, "max_pins": {"type": "integer"}}, "required": ["net"]})
def t_get_net(ws: Workspace, net: str, max_pins: int = 40):
    d = ws._d()
    n = ws.resolve_net(net)
    plan = ws.plan()
    pads = d.board.pads_by_net().get(n, [])
    pins = []
    for p in pads[:max(1, min(500, max_pins))]:
        fp = d.board.by_ref.get(p.ref)
        pins.append("%s %s%s%s %s" % (p.name, fp.value if fp else "", (":" + p.pinfunction) if p.pinfunction else "",
                                      ("/" + p.pintype) if p.pintype else "", fp.side if fp else ""))
    probe = [{k: p[k] for k in ("id", "role", "label", "channel", "divider", "x", "y")} for p in plan["probes"] if p["net"] == n]
    why = next((u["reason"] for u in plan["coverage"]["untestable"] if u["net"] == n), None)
    out = {"net": n, "spec": d.net_spec(n), "pin_count": len(pads), "pins": pins, "probes": probe}
    if why:
        out["not_probed_because"] = why
    hint = _regulator_hint(d, n) or None
    if hint:
        out["voltage_hint"] = {"v": hint[0], "why": hint[1]}
    pu = _pullup_hint(d, n)
    if pu:
        out["bias_hint"] = pu
    return out


@tool("list_components", "List components as rows [ref, value, footprint, side, mount, nets(n), mpn, height_mm].",
      dict(properties=dict(PAGING, **{
          "query": {"type": "string", "description": "Glob on ref or substring of value/footprint, e.g. 'U*' or 'AMS1117'."},
          "side": {"type": "string", "enum": ["top", "bottom", "any"]},
          "mount": {"type": "string", "enum": ["smd", "tht", "other", "any"]},
          "kind": {"type": "string", "enum": ["ic", "connector", "capacitor", "resistor", "inductor", "diode", "testpoint", "any"]}})))
def t_list_components(ws: Workspace, query: Optional[str] = None, side: str = "any", mount: str = "any", kind: str = "any",
                      limit: Optional[int] = None, offset: int = 0):
    d = ws._d()
    kinds = {"ic": C.is_ic, "connector": C.is_connector, "capacitor": C.is_capacitor, "resistor": C.is_resistor,
             "inductor": C.is_inductor, "diode": C.is_diode, "testpoint": C.is_testpoint}
    rows = []
    for fp in sorted(d.components(), key=lambda f: C.natural_key(f.ref)):
        if side != "any" and fp.side != side:
            continue
        if mount != "any" and fp.mount != mount:
            continue
        if kind != "any" and not kinds[kind](fp):
            continue
        if query:
            q = query.lower()
            if not (fnmatch.fnmatch(fp.ref.lower(), q) or q in fp.value.lower() or q in fp.lib_id.lower()):
                continue
        h, _ = d.height_of(fp)
        rows.append([fp.ref, fp.value, fp.name, fp.side, fp.mount, len(fp.nets()), C.mpn_of(fp) or None, round(h, 1)])
    out = _page(rows, _lim(limit), int(offset or 0))
    out["columns"] = ["ref", "value", "footprint", "side", "mount", "nets", "mpn", "height_mm"]
    return out


@tool("get_component", "Details of one component: fields (MPN, ratings), pins->nets, height and its source, related findings.",
      {"properties": {"ref": {"type": "string"}}, "required": ["ref"]})
def t_get_component(ws: Workspace, ref: str):
    d = ws._d()
    fp = ws.resolve_ref(ref)
    findings, _ = ws.analysis()
    h, src = d.height_of(fp)
    fields = {k: v for k, v in list(fp.fields.items()) + list(fp.props.items())
              if v and k not in ("Reference", "Footprint", "Datasheet") and len(v) < 200}
    rv, _ = C.rated_voltage(fp) if C.is_capacitor(fp) else (None, "")
    return {"ref": fp.ref, "value": fp.value, "footprint": fp.lib_id, "side": fp.side, "mount": fp.mount,
            "position_mm": [round(fp.x, 3), round(fp.y, 3), round(fp.angle, 1)], "dnp": fp.dnp, "fields": fields,
            "rated_voltage_v": rv, "height_mm": round(h, 2), "height_source": src,
            "pins": {p.number: "%s%s" % (p.net, (" (" + p.pinfunction + ")") if p.pinfunction else "") for p in fp.pads if p.number},
            "findings": [{"uid": f.uid, "id": f.id, "sev": f.severity, "title": f.title} for f in findings if fp.ref in f.refs]}


@tool("list_findings", "Weak points as compact rows [uid, severity, id, category, title, refs]. Filter to keep it short.",
      dict(properties=dict(PAGING, **{
          "min_severity": {"type": "string", "enum": ["critical", "major", "minor", "info"]},
          "category": {"type": "string"}, "ref": {"type": "string"}, "net": {"type": "string"}})))
def t_list_findings(ws: Workspace, min_severity: str = "info", category: Optional[str] = None, ref: Optional[str] = None,
                    net: Optional[str] = None, limit: Optional[int] = None, offset: int = 0):
    from .analysis import SEV_RANK
    findings, _ = ws.analysis()
    rows = []
    for f in findings:
        if SEV_RANK[f.severity] > SEV_RANK[min_severity]:
            continue
        if category and f.category != category:
            continue
        if ref and ref not in f.refs:
            continue
        if net and net not in f.nets:
            continue
        refs = sorted(set(f.refs), key=C.natural_key)
        rows.append([f.uid, f.severity, f.id, f.category, f.title, refs[:12] + (["+%d" % (len(refs) - 12)] if len(refs) > 12 else [])])
    out = _page(rows, _lim(limit), int(offset or 0))
    out["columns"] = ["uid", "severity", "id", "category", "title", "refs"]
    out["waived"] = len((ws._d().cfg["analysis"].get("waivers") or []))
    return out


@tool("get_finding", "Full detail of one finding (by uid like F003, or check id), items truncated to max_items.",
      {"properties": {"uid": {"type": "string"}, "max_items": {"type": "integer"}}, "required": ["uid"]})
def t_get_finding(ws: Workspace, uid: str, max_items: int = 10):
    findings, _ = ws.analysis()
    for f in findings:
        if f.uid == uid or f.id == uid:
            return {"finding": f.to_dict(max(1, min(100, max_items)))}
    raise ToolError("UNKNOWN_FINDING", "no finding %r" % uid, hint="list_findings shows uids")


@tool("get_testplan", "Current test plan. section: summary | probes | sequence | coverage | header.",
      {"properties": {"section": {"type": "string", "enum": ["summary", "probes", "sequence", "coverage", "header"]}}})
def t_get_testplan(ws: Workspace, section: str = "summary"):
    plan = ws.plan()
    if section == "probes":
        return {"columns": ["id", "net", "role", "side", "x", "y", "label", "channel", "header_pin", "divider_ratio", "min_v", "max_v"],
                "rows": [[p["id"], p["net"], p["role"], p.get("side"), p["x"], p["y"], p["label"], p.get("channel"), p.get("header_pin"),
                          (p.get("divider") or {}).get("ratio"), (p.get("expected") or {}).get("min_v"),
                          (p.get("expected") or {}).get("max_v")] for p in plan["probes"]]}
    if section == "sequence":
        return {"sequence": plan["sequence"]}
    if section == "coverage":
        return {"coverage": plan["coverage"]}
    if section == "header":
        return {"header": plan["header"], "headers": plan.get("headers")} if plan.get("headers") else {"header": plan["header"]}
    return {"probe_side": plan["probe_side"], "plates": plan.get("plates"), "pogo": plan["pogo"], "supply": plan["supply"],
            "probes": len(plan["probes"]), "stacked_boards": plan.get("stack") or [],
            "channels": sum(1 for p in plan["probes"] if p.get("channel")),
            "steps": [[s["id"], s["name"], s["type"], len(s.get("checks", []) or s.get("items", []) or [])] for s in plan["sequence"]],
            "coverage_pct": plan["coverage"]["percent"], "untestable": len(plan["coverage"]["untestable"]),
            "warnings": plan["warnings"]}


@tool("get_config", "Project config. scope: overrides (what was decided, default) | effective (all values) | audit (decision log).",
      {"properties": {"scope": {"type": "string", "enum": ["overrides", "effective", "audit"]},
                      "section": {"type": "string", "description": "e.g. testplan, jig, fixture, analysis, nets, supply"}}})
def t_get_config(ws: Workspace, scope: str = "overrides", section: Optional[str] = None):
    d = ws._d()
    if scope == "audit":
        return {"audit": (d.user_config.get("_audit") or [])[-50:]}
    src = d.cfg if scope == "effective" else {k: v for k, v in d.user_config.items() if k != "_audit"}
    if section:
        return {section: src.get(section)}
    return {"config": src, "path": d.config_path}


@tool("get_schema", "JSON schema/allowed values for: test_step | net_spec | testplan_options | jig_options | fixture_options | "
      "stacked_boards | profiles | tech_classes (the ordered class system) | class_system (format, metric names, template) | "
      "facilities (testers to pick with set_facility).",
      {"properties": {"name": {"type": "string", "enum": ["test_step", "net_spec", "testplan_options", "jig_options",
                                                          "fixture_options", "stacked_boards", "profiles", "tech_classes",
                                                          "class_system", "facilities"]}}, "required": ["name"]})
def t_schema(ws: Workspace, name: str):
    if name == "facilities":
        from .facility import facility_for, load_facilities, summary
        d = ws.design
        found, warns = load_facilities(d.cfg if d else None, d.project_dir if d else None)
        return {"selected": facility_for(d)["id"] if d else None, "facilities": [summary(f) for _k, f in sorted(found.items())],
                "warnings": warns, "write_your_own": "pcbtestkit facilities template --out <folder>; see docs/FACILITIES.md"}
    if name == "profiles":
        return {"fab_profiles": FAB_PROFILES, "pogo_profiles": POGO_PROFILES}
    if name == "tech_classes":
        from .classsys import describe
        return {"system": describe(ws._d().class_system())}
    if name == "class_system":
        from .classsys import ALIASES, CONTEXT_KEYS, METRICS, STREAMS, TEMPLATE
        from .classsys import engine as E
        return {"rules": E.__doc__.strip(), "template": TEMPLATE,
                "metrics": {k: {"label": v["label"], "kind": v["kind"], "unit": v.get("unit", "mm"), "stream": v.get("stream")}
                            for k, v in METRICS.items()},
                "aliases": ALIASES, "context_keys": CONTEXT_KEYS, "streams": STREAMS}
    m = {"test_step": STEP_SCHEMA, "net_spec": NET_UPDATE_SCHEMA, "testplan_options": TESTPLAN_OPTS,
         "jig_options": JIG_OPTS, "fixture_options": FIXTURE_OPTS, "stacked_boards": STACK_SCHEMA}
    return {"schema": m[name]}


# =========================================================================== #
# Write tools
# =========================================================================== #
NET_UPDATE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["net"],
    "properties": {
        "net": {"type": "string"},
        "kind": {"type": "string", "enum": ["power", "ground", "signal"]},
        "nominal_v": V_NUM, "tolerance_pct": {"type": "number", "description": "Symmetric band around nominal (0.1-50)."},
        "min_v": V_NUM, "max_v": V_NUM,
        "max_current_a": {"type": "number", "description": "Design current; turns track-width checks into hard checks."},
        "test": {"type": "boolean", "description": "false = do not probe/test this net."},
        "note": {"type": "string"},
        "clear": {"type": "boolean", "description": "Remove all decisions for this net (back to inferred values)."},
    },
}


@tool("set_net_specs", "Set expected voltage limits / kind / test inclusion for one or more nets (batch up to 200). "
      "Limits: nominal_v+tolerance_pct, or explicit min_v/max_v.",
      {"properties": dict({"updates": {"type": "array", "items": NET_UPDATE_SCHEMA, "minItems": 1, "maxItems": 200}}, **WRITE_COMMON),
       "required": ["updates"]}, read_only=False, writes=True)
def t_set_net_specs(ws: Workspace, updates: List[Dict[str, Any]], rationale: str = "", dry_run: bool = False, confirm: bool = False):
    d = ws._d()
    resolved = []
    for u in updates:
        if not isinstance(u, dict) or "net" not in u:
            raise ToolError("BAD_VALUE", "each update needs a 'net'")
        extra = set(u) - set(NET_UPDATE_SCHEMA["properties"])
        if extra:
            raise ToolError("UNKNOWN_ARGUMENT", "unknown field(s) in update for %s: %s" % (u.get("net"), sorted(extra)),
                            allowed=sorted(NET_UPDATE_SCHEMA["properties"]))
        resolved.append((ws.resolve_net(u["net"]), u))

    def mutate(cfg):
        warnings = []
        nets = cfg.setdefault("nets", {})
        for net, u in resolved:
            if u.get("clear"):
                nets.pop(net, None)
                continue
            entry = dict(nets.get(net, {}))
            kind = u.get("kind") or entry.get("kind") or d.net_kind(net)
            nom = parse_v(u.get("nominal_v"), "nominal_v") if "nominal_v" in u else entry.get("nominal")
            vmin = parse_v(u.get("min_v"), "min_v") if "min_v" in u else entry.get("min")
            vmax = parse_v(u.get("max_v"), "max_v") if "max_v" in u else entry.get("max")
            tol = _num(u.get("tolerance_pct"), "tolerance_pct", 0.1, 50) if "tolerance_pct" in u else entry.get("tolerance_pct")
            for nm, v in (("nominal_v", nom), ("min_v", vmin), ("max_v", vmax)):
                if v is not None and abs(v) > 1000:
                    raise ToolError("OUT_OF_RANGE", "%s %s=%g V is not plausible" % (net, nm, v))
            if vmin is not None and vmax is not None and vmin >= vmax:
                raise ToolError("BAD_LIMITS", "%s: min_v (%g) must be below max_v (%g)" % (net, vmin, vmax))
            if nom is not None and vmin is not None and vmax is not None and not (vmin <= nom <= vmax):
                raise ToolError("BAD_LIMITS", "%s: nominal_v %g outside [%g, %g]" % (net, nom, vmin, vmax))
            if kind == "ground" and nom not in (None, 0, 0.0):
                warnings.append("CONFIRM: %s is a ground net but nominal_v=%g" % (net, nom))
            name_v = C.net_voltage(net)
            if nom is not None and name_v is not None and abs(nom - name_v) > 0.2 * max(abs(name_v), 0.5):
                warnings.append("CONFIRM: %s nominal_v=%g conflicts with the voltage in its name (%g V)" % (net, nom, name_v))
            if "kind" in u:
                entry["kind"] = u["kind"]
            for key, val in (("nominal", nom), ("min", vmin), ("max", vmax), ("tolerance_pct", tol)):
                if val is not None:
                    entry[key] = val
            if "max_current_a" in u:
                entry["max_current_a"] = _num(u["max_current_a"], "max_current_a", 0.0001, 200)
            if "test" in u:
                entry["test"] = bool(u["test"])
            if "note" in u:
                entry["note"] = str(u["note"])[:200]
            top = vmax if vmax is not None else (nom * (1 + (tol or 5) / 100.0) if nom is not None else None)
            if top is not None and abs(top) > float(d.cfg["testplan"]["adc_max_v"]) * 0.9 and not d.cfg["fixture"].get("dividers", True):
                warnings.append("%s up to %g V exceeds the ADC range and dividers are disabled" % (net, top))
            nets[net] = entry
        return warnings
    return ws.apply(mutate, rationale, dry_run, confirm, what="set_net_specs %s" % ",".join(n for n, _ in resolved)[:200])


@tool("set_supply", "Set how the tester powers the DUT: net, voltage, current limit, idle current window.",
      {"properties": dict({"net": {"type": "string"}, "voltage": V_NUM, "current_limit_a": {"type": "number"},
                           "max_idle_current_a": {"type": "number"}, "min_idle_current_a": {"type": "number"}}, **WRITE_COMMON)},
      read_only=False, writes=True)
def t_set_supply(ws: Workspace, net: Optional[str] = None, voltage=None, current_limit_a=None, max_idle_current_a=None,
                 min_idle_current_a=None, rationale: str = "", dry_run: bool = False, confirm: bool = False):
    d = ws._d()
    rnet = ws.resolve_net(net) if net else None

    def mutate(cfg):
        w = []
        s = cfg.setdefault("supply", {})
        if rnet:
            if d.net_kind(rnet) != "power":
                w.append("CONFIRM: %s is classified %s, not power" % (rnet, d.net_kind(rnet)))
            s["net"] = rnet
        if voltage is not None:
            v = parse_v(voltage, "voltage")
            if not (0 < v <= 1000):
                raise ToolError("OUT_OF_RANGE", "supply voltage must be > 0")
            if v > 60:
                w.append("CONFIRM: %g V is above SELV (60 V DC); the fixture and runner are not designed for hazardous voltages" % v)
            nv = C.net_voltage(rnet or s.get("net") or "")
            if nv and abs(v - nv) > 0.25 * nv:
                w.append("CONFIRM: supply voltage %g V differs from the net name (%g V)" % (v, nv))
            s["voltage"] = v
        if current_limit_a is not None:
            s["current_limit_a"] = _num(current_limit_a, "current_limit_a", 0.001, 20)
        if max_idle_current_a is not None:
            s["max_idle_current_a"] = _num(max_idle_current_a, "max_idle_current_a", 0.0, 20)
        if min_idle_current_a is not None:
            s["min_idle_current_a"] = _num(min_idle_current_a, "min_idle_current_a", 0.0, 20)
        lo, hi = s.get("min_idle_current_a"), s.get("max_idle_current_a")
        if lo is not None and hi is not None and lo >= hi:
            raise ToolError("BAD_LIMITS", "min_idle_current_a must be below max_idle_current_a")
        lim = s.get("current_limit_a")
        if hi is not None and lim is not None and hi > lim:
            raise ToolError("BAD_LIMITS", "max_idle_current_a (%g) exceeds current_limit_a (%g)" % (hi, lim))
        return w
    return ws.apply(mutate, rationale, dry_run, confirm, what="set_supply")


TESTPLAN_OPTS = {
    "probe_side": {"type": "string", "enum": ["auto", "bottom", "top", "both"],
                   "description": "both = two fixture plates, the board between them (nets covered on one side are probed "
                                  "from the other)."},
    "pogo": {"type": "string", "enum": sorted(POGO_PROFILES)},
    "pogo_mount": {"type": "string", "enum": ["receptacle", "direct"]},
    "pogo_drill_mm": {"type": "number", "description": "Override the fixture drill for the pogo pins/receptacles."},
    "signals": {"type": "string", "enum": ["none", "auto", "all"], "description": "Which signal nets get probes."},
    "include_nets": {"type": "array", "items": {"type": "string"}},
    "exclude_nets": {"type": "array", "items": {"type": "string"}},
    "max_channels": {"type": "integer"}, "edge_keepout_mm": {"type": "number"}, "min_target_mm": {"type": "number"},
    "allow_vias": {"type": "boolean"}, "allow_component_pads": {"type": "boolean"}, "gnd_points": {"type": "integer"},
    "supply_points": {"type": "integer"}, "default_tolerance_pct": {"type": "number"},
    "signal_tolerance_v": {"type": "number"}, "adc_max_v": {"type": "number"}, "max_signal_voltage": {"type": "number"},
    "probe_clearance_mm": {"type": "number"}, "short_test_min_ohm": {"type": "number"}, "settle_ms": {"type": "integer"},
    "stack_clearance_mm": {"type": "number", "description": "Probes stay this far outside a fitted stacked board."},
}
_RANGES = {"pogo_drill_mm": (0.4, 3.0), "max_channels": (1, 512), "edge_keepout_mm": (0, 20), "min_target_mm": (0.3, 5), "gnd_points": (1, 8),
           "supply_points": (1, 8), "default_tolerance_pct": (0.1, 50), "signal_tolerance_v": (0.01, 5),
           "adc_max_v": (0.5, 12), "max_signal_voltage": (0.5, 60), "probe_clearance_mm": (1, 30),
           "short_test_min_ohm": (0.1, 1e7), "settle_ms": (0, 60000), "margin_mm": (1, 50), "mounting_hole_d_mm": (1, 8),
           "resolution_mm": (0.1, 1.0), "hold_clearance_mm": (0, 2), "relief_clearance_mm": (0, 5),
           "relief_extra_depth_mm": (0, 10), "hold_depth_offset_mm": (-1, 2), "floor_mm": (0.6, 10), "rim_mm": (1, 15),
           "rim_height_mm": (0, 10), "edge_clearance_mm": (0, 2), "post_clearance_mm": (0, 1),
           "placement_clearance_mm": (2, 40), "placement_border_mm": (3, 30), "lead_well_margin_mm": (0, 5),
           "stop_length_mm": (3, 30), "stop_width_mm": (1, 10), "stop_clearance_mm": (0, 1),
           "bottom_part_clearance_mm": (0, 5), "stack_clearance_mm": (0, 10), "lead_relief_mm": (0, 10),
           "nest_border_mm": (5, 50),
           "divider_r_bottom_rail": (100, 1e6), "divider_r_bottom_signal": (1e3, 1e7),
           "track_mm": (0.1, 2.0), "power_track_mm": (0.1, 5.0), "clearance_mm": (0.1, 2.0), "via_mm": (0.3, 2.0),
           "via_drill_mm": (0.15, 1.2)}


def _set_section(ws: Workspace, section: str, opts: Dict[str, Any], schema: Dict[str, Any], rationale, dry_run, confirm):
    d = ws._d()
    clean: Dict[str, Any] = {}
    for k, v in opts.items():
        if v is None:
            continue
        p = schema[k]
        if "enum" in p and v not in p["enum"]:
            raise ToolError("BAD_VALUE", "%s must be one of %s" % (k, p["enum"]))
        if k in _RANGES:
            v = _num(v, k, *_RANGES[k])
            if p.get("type") == "integer":
                v = int(v)
        if k in ("include_nets", "exclude_nets"):
            v = [ws.resolve_net(n) for n in v]
        clean[k] = v
    if not clean:
        raise ToolError("NOTHING_TO_DO", "no options given")

    def mutate(cfg):
        cfg.setdefault(section, {}).update(clean)
        w = []
        if section == "testplan" and clean.get("adc_max_v", 3.3) > 5.5:
            w.append("CONFIRM: adc_max_v %g V is unusual for Pi/MCU ADCs (3.3 or 5 V)" % clean["adc_max_v"])
        return w
    return ws.apply(mutate, rationale, dry_run, confirm, what="set %s options %s" % (section, sorted(clean)))


@tool("set_test_options", "Test-plan options (probe side, pogo family, signal policy, include/exclude nets, channels, ADC range...). "
      "See get_schema('testplan_options').", {"properties": dict(TESTPLAN_OPTS, **WRITE_COMMON)}, read_only=False, writes=True)
def t_set_test_options(ws: Workspace, rationale: str = "", dry_run: bool = False, confirm: bool = False, **opts):
    want = [ws.resolve_net(n) for n in (opts.get("include_nets") or [])]
    if dry_run and want:
        # evaluate reachability on the would-be plan before it is reverted
        res = _set_section(ws, "testplan", opts, TESTPLAN_OPTS, rationale, False, confirm)
        notes = _probe_notes(ws, want)
        t_undo(ws)
        res.update({"dry_run": True, "saved": False})
        res["warnings"] = res.get("warnings", []) + notes
        return res
    res = _set_section(ws, "testplan", opts, TESTPLAN_OPTS, rationale, dry_run, confirm)
    res["warnings"] = res.get("warnings", []) + _probe_notes(ws, want)
    return res


def _probe_notes(ws: Workspace, nets: List[str]) -> List[str]:
    plan = ws.plan()
    probed = {p["net"] for p in plan["probes"] if p.get("channel")}
    why = {u["net"]: u["reason"] for u in plan["coverage"]["untestable"]}
    return ["%s is still not probed: %s" % (n, why.get(n, "not selected")) for n in nets if n not in probed]


JIG_OPTS = {
    "parts": {"type": ["array", "string"], "items": {"type": "string"}, "description": "Refs to hold, or 'auto' (all THT)."},
    "exclude": {"type": "array", "items": {"type": "string"}},
    "heights": {"type": "object", "description": "{ref: body height above PCB in mm}"},
    "relief_all_top": {"type": "boolean"}, "resolution_mm": {"type": "number"}, "hold_clearance_mm": {"type": "number"},
    "relief_clearance_mm": {"type": "number"}, "relief_extra_depth_mm": {"type": "number"},
    "hold_depth_offset_mm": {"type": "number"}, "floor_mm": {"type": "number"}, "rim_mm": {"type": "number"},
    "rim_height_mm": {"type": "number"}, "edge_clearance_mm": {"type": "number"}, "posts": {"type": "boolean"},
    "post_clearance_mm": {"type": "number"}, "finger_notches": {"type": "boolean"},
    "workflow": {"type": "string", "enum": ["two_jig", "single"],
                 "description": "two_jig (default): placement jig + holding jig, parts fitted top side up. single: parts "
                                "dropped into the holding jig, PCB lowered onto them."},
    "placement_clearance_mm": {"type": "number", "description": "Room under the PCB for the leads in the placement jig."},
    "placement_border_mm": {"type": "number"}, "lead_well_margin_mm": {"type": "number"},
    "stop_length_mm": {"type": "number"}, "stop_width_mm": {"type": "number"}, "stop_clearance_mm": {"type": "number"},
    "bottom_part_clearance_mm": {"type": "number"},
    "sides": {"type": ["array", "string"], "items": {"type": "string", "enum": ["top", "bottom"]},
              "description": "Hand-solder passes, in order: 'auto' (every side with hand-soldered parts, top first) or e.g. "
                             "['bottom', 'top']. Each side gets its own jig set."},
    "smt_first": {"type": "string", "enum": ["auto", "top", "bottom"], "description": "SMD side reflowed first."},
    "paste_nest": {"type": "string", "enum": ["auto", "both", "none"],
                   "description": "Paste-print nest: auto = for the second SMD side (rests on the first side's parts)."},
    "lead_relief_mm": {"type": "number", "description": "Relief depth for the soldered leads of the other side's parts."},
    "nest_border_mm": {"type": "number"},
}


@tool("set_jig_options", "Solder-jig options: workflow (two jigs or one), which parts it holds, part heights, clearances. "
      "See get_schema('jig_options').",
      {"properties": dict(JIG_OPTS, **WRITE_COMMON)}, read_only=False, writes=True)
def t_set_jig_options(ws: Workspace, rationale: str = "", dry_run: bool = False, confirm: bool = False, **opts):
    d = ws._d()
    if "parts" in opts and opts["parts"] != "auto":
        if not isinstance(opts["parts"], list):
            raise ToolError("BAD_VALUE", "parts must be a list of refs or 'auto'")
        opts["parts"] = [ws.resolve_ref(r).ref for r in opts["parts"]]
    if "exclude" in opts:
        opts["exclude"] = [ws.resolve_ref(r).ref for r in opts["exclude"]]
    if "sides" in opts and opts["sides"] is not None:
        sd = opts["sides"]
        sd = [sd] if isinstance(sd, str) and sd != "auto" else sd
        if sd != "auto" and (not isinstance(sd, list) or not sd or any(x not in ("top", "bottom") for x in sd) or len(set(sd)) != len(sd)):
            raise ToolError("BAD_VALUE", "sides must be 'auto' or a list of distinct 'top'/'bottom'")
        opts["sides"] = sd
    if "heights" in opts:
        hs = {}
        for r, h in (opts["heights"] or {}).items():
            hs[ws.resolve_ref(r).ref] = _num(h, "height of %s" % r, 0.1, 80)
        merged = dict(d.user_config.get("jig", {}).get("heights", {}))
        merged.update(hs)
        opts["heights"] = merged
    for k in [k for k in opts if k in ("parts", "exclude", "heights")]:
        pass
    clean = {k: v for k, v in opts.items() if v is not None}
    special = {k: clean.pop(k) for k in ("parts", "exclude", "heights", "sides") if k in clean}
    res = None
    if clean:
        res = _set_section(ws, "jig", clean, JIG_OPTS, rationale, dry_run, confirm)
    if special:
        def mutate(cfg):
            cfg.setdefault("jig", {}).update(special)
            return []
        res = ws.apply(mutate, rationale, dry_run, confirm, what="set jig %s" % sorted(special))
    if res is None:
        raise ToolError("NOTHING_TO_DO", "no options given")
    return res


FIXTURE_OPTS = {
    "mode": {"type": "string", "enum": ["generate", "own", "none"],
             "description": "generate the fixture board, own = the user designs it (probe-point CSV), none = no fixture"},
    "outline": {"type": "string", "enum": ["rect", "outline"]}, "margin_mm": {"type": "number"},
    "panel_cols": {"type": "integer"}, "panel_rows": {"type": "integer"}, "panel_spacing_mm": {"type": "number"},
    "mounting_holes": {"type": "boolean"}, "mounting_hole_d_mm": {"type": "number"}, "dividers": {"type": "boolean"},
    "divider_r_bottom_rail": {"type": "number"}, "divider_r_bottom_signal": {"type": "number"},
    "draw_dut_components": {"type": "boolean"}, "cut_tall_components": {"type": "boolean"},
    "route": {"type": "boolean", "description": "Route the fixture board with the built-in router (default true)."},
    "track_mm": {"type": "number"}, "power_track_mm": {"type": "number", "description": "GND and VIN tracks."},
    "clearance_mm": {"type": "number"}, "via_mm": {"type": "number"}, "via_drill_mm": {"type": "number"},
    "nest": {"type": "boolean", "description": "A nest (rails, end stop, sprung standoffs) and a toggle clamp per board."},
    "nest_entry": {"type": "string", "enum": ["front", "back", "left", "right"], "description": "Side the boards slide in from."},
    "nest_style": {"type": "string", "enum": ["auto", "slide", "drop"]},
    "nest_clamp": {"type": "string", "enum": ["toggle", "none"]},
    "nest_clamp_holes_mm": {"type": "array", "items": {"type": "number"},
                            "description": "The toggle clamp base's hole pattern [along, across] in mm."},
    "nest_lead_in_mm": {"type": "number"}, "nest_tip_gap_mm": {"type": "number"},
}
NEST_RANGES = {"lead_in_mm": (0.0, 40.0), "tip_gap_mm": (0.3, 5.0)}


@tool("set_fixture_options", "Fixture options: mode (generate|own|none), outline, margin, panel of N x M sites, dividers, "
      "holes, routing (route, track and via sizes), every board's nest and clamp (nest_*).",
      {"properties": dict(FIXTURE_OPTS, **WRITE_COMMON)}, read_only=False, writes=True)
def t_set_fixture_options(ws: Workspace, rationale: str = "", dry_run: bool = False, confirm: bool = False, **opts):
    nest: Dict[str, Any] = {}
    for k in [k_ for k_ in list(opts) if k_ == "nest" or k_.startswith("nest_")]:
        v = opts.pop(k)
        if v is None:
            continue
        key = "enabled" if k == "nest" else k[5:]
        p = FIXTURE_OPTS[k]
        if "enum" in p and v not in p["enum"]:
            raise ToolError("BAD_VALUE", "%s must be one of %s" % (k, p["enum"]))
        if key in NEST_RANGES:
            v = _num(v, k, *NEST_RANGES[key])
        if key == "clamp_holes_mm":
            if not isinstance(v, list) or len(v) != 2:
                raise ToolError("BAD_VALUE", "nest_clamp_holes_mm is [along, across] in mm")
            v = [_num(x, k, 5.0, 60.0) for x in v]
        nest[key] = v
    panel = {}
    for k, pk in (("panel_cols", "cols"), ("panel_rows", "rows"), ("panel_spacing_mm", "spacing_mm")):
        if opts.get(k) is not None:
            lo, hi = (1, 20) if pk != "spacing_mm" else (2, 100)
            panel[pk] = _num(opts.pop(k), k, lo, hi)
            if pk != "spacing_mm":
                panel[pk] = int(panel[pk])
        else:
            opts.pop(k, None)
    clean = {k: v for k, v in opts.items() if v is not None}
    if panel or nest:
        cur = dict(ws._d().cfg["fixture"].get("panel") or {})
        cur.update(panel)
        if cur.get("cols", 1) * cur.get("rows", 1) > 24:
            raise ToolError("OUT_OF_RANGE", "more than 24 sites per fixture is not supported")

        def mutate(cfg):
            fx_ = cfg.setdefault("fixture", {})
            if panel:
                fx_["panel"] = cur
            if nest:
                fx_["nest"] = dict(fx_.get("nest") or {}, **nest)
            if clean:
                fx_.update(clean)
            return []
        for k, v in clean.items():
            if k in _RANGES:
                _num(v, k, *_RANGES[k])
        return ws.apply(mutate, rationale, dry_run, confirm, what="set fixture options")
    return _set_section(ws, "fixture", clean, FIXTURE_OPTS, rationale, dry_run, confirm)


PRODUCTION_OPTS = {
    "batch_count": {"type": "integer", "description": "Boards per batch: one fixture site per board, jigs that hold them."},
    "batch_cols": {"type": "integer", "description": "Columns of boards (0 = automatic)."},
    "bed_mm": {"type": "array", "items": {"type": "number"}, "description": "3D-printer bed [x, y] mm for the jigs."},
    "program_headers": {"type": "array", "items": {"type": "string"},
                        "description": "Programming headers the fixture serves (refs); omit = auto-detected, [] = none."},
    "program_connector": {"type": "string", "enum": ["same", "swd10", "jtag20", "avr_isp6", "pic_icsp6", "pins"],
                          "description": "Fixture connector per board: same = copy of the board's header."},
    "program_command": {"type": "array", "items": {"type": "string"},
                        "description": "argv the runner uses to flash each board ({site}, {serial} filled in)."},
}


@tool("set_batch_and_programming", "Batch size (boards per fixture and jig set) and programming: headers, fixture connector "
      "type, flash command.", {"properties": dict(PRODUCTION_OPTS, **WRITE_COMMON)}, read_only=False, writes=True)
def t_set_batch_and_programming(ws: Workspace, rationale: str = "", dry_run: bool = False, confirm: bool = False, **opts):
    batch: Dict[str, Any] = {}
    prog: Dict[str, Any] = {}
    if opts.get("batch_count") is not None:
        batch["count"] = int(_num(opts["batch_count"], "batch_count", 1, 48))
    if opts.get("batch_cols") is not None:
        batch["cols"] = int(_num(opts["batch_cols"], "batch_cols", 0, 12))
    if opts.get("bed_mm") is not None:
        bed = opts["bed_mm"]
        if not isinstance(bed, list) or len(bed) != 2:
            raise ToolError("BAD_VALUE", "bed_mm must be [x, y] in mm")
        batch["bed_mm"] = [_num(bed[0], "bed_mm", 50, 1000), _num(bed[1], "bed_mm", 50, 1000)]
    if opts.get("program_headers") is not None:
        prog["headers"] = [ws.resolve_ref(r).ref for r in opts["program_headers"]]
    if opts.get("program_connector") is not None:
        if opts["program_connector"] not in PRODUCTION_OPTS["program_connector"]["enum"]:
            raise ToolError("BAD_VALUE", "program_connector must be one of %s" % PRODUCTION_OPTS["program_connector"]["enum"])
        prog["connector"] = opts["program_connector"]
    if opts.get("program_command") is not None:
        cmd = [str(a) for a in opts["program_command"]]
        prog["command"] = cmd
    if not batch and not prog:
        raise ToolError("NOTHING_TO_DO", "no options given")

    def mutate(cfg):
        if batch:
            cfg.setdefault("batch", {}).update(batch)
        if prog:
            cfg.setdefault("program", {}).update(prog)
        w = []
        if prog.get("command"):
            w.append("CONFIRM: the test runner will run %r on the test station for every board (with --allow-commands)"
                     % " ".join(prog["command"])[:120])
        if batch.get("count", 1) > 16:
            w.append("CONFIRM: %d boards per batch makes a large fixture; check it fits your test station" % batch["count"])
        return w
    return ws.apply(mutate, rationale, dry_run, confirm, what="set batch/programming %s" % sorted(list(batch) + list(prog)))


@tool("set_facility", "Pick the test facility (tester) the fixture plugs into, by id from get_schema('facilities'): its "
      "channel count and range, fixed connector and plate, buses and runner driver.",
      {"properties": dict({"facility": {"type": "string", "description": "Facility id, e.g. raspberry-pi."}}, **WRITE_COMMON),
       "required": ["facility"]}, read_only=False, writes=True)
def t_set_facility(ws: Workspace, facility: str, rationale: str = "", dry_run: bool = False, confirm: bool = False):
    from .facility import load_facilities
    d = ws._d()
    found, _w = load_facilities(d.cfg, d.project_dir)
    if facility not in found:
        raise ToolError("UNKNOWN_FACILITY", "no facility %r (known: %s)" % (facility, ", ".join(sorted(found))),
                        hint="get_schema('facilities') lists them; pcbtestkit facilities template writes a new one")

    def mutate(cfg):
        cfg["facility"] = facility
        fac = found[facility]
        n = (fac.get("batch") or {}).get("max_sites")
        count = int(((cfg.get("batch") or {}).get("count")) or 1)
        return ["facility %s tests at most %d board(s) per fixture; the batch is %d" % (facility, n, count)] if n and count > n else []
    return ws.apply(mutate, rationale, dry_run, confirm, what="set facility %s" % facility)


@tool("set_test_environment", "Make this board's fixture and jigs for an existing test environment (a folder an "
      "environment build made, or a station board): station (auto = the one that takes most) and batch (auto = as many "
      "as it allows, or N); remember=true uses it for every future build. path 'none' switches it off.",
      {"properties": dict({"path": {"type": "string"}, "station": {"type": "string"},
                           "batch": {"description": "'auto' or a number"}, "remember": {"type": "boolean"}},
                          **WRITE_COMMON), "required": ["path"]}, read_only=False, writes=True)
def t_set_test_environment(ws: Workspace, path: str, station: str = "auto", batch: Any = "auto", remember: bool = False,
                           rationale: str = "", dry_run: bool = False, confirm: bool = False):
    from .environment import board_setup
    p = str(path or "").strip()
    if p.lower() not in ("none", ""):
        p = os.path.abspath(p if os.path.isabs(p) else os.path.join(ws._d().project_dir, p))
        if not os.path.exists(p):
            raise ToolError("NOT_FOUND", "no test environment at %s" % p, hint="a folder with environment.json, or a station .kicad_pcb")
    b_ = "auto" if str(batch).lower() == "auto" else int(_num(batch, "batch", 1, 64))
    setting = {"path": p, "station": station or "auto", "batch": b_}
    warns: List[str] = []
    if p.lower() not in ("none", ""):
        setup = board_setup(ws._d().board.path, setting, ws._d().config_path)
        if setup.get("ok"):
            warns.append("station %s, %d board(s) per batch%s" % (setup["station"], setup["batch"],
                                                                 " (as many as it allows: %s)" % setup.get("limit") if setup.get("auto") else ""))
        else:
            warns.append("CONFIRM: %s" % setup.get("reason"))

    def mutate(cfg):
        cfg["environment"] = setting
        return list(warns)
    res = ws.apply(mutate, rationale, dry_run, confirm, what="set test environment %s" % (p or "remembered"))
    if remember and not dry_run and p.lower() not in ("none", "") and res.get("saved", True):
        from . import settings
        settings.remember_environment(setting)
    return res


def _env_dir(ws: Workspace, path: str) -> str:
    """An environment folder (relative to the open project's folder, else the working folder)."""
    p = str(path or "").strip()
    if not p:
        raise ToolError("BAD_VALUE", "path is the test environment's folder (made by pcbtestkit environment build)")
    if not os.path.isabs(p):
        base = ws.design.project_dir if ws.design is not None else os.getcwd()
        p = os.path.join(base, p)
    p = os.path.abspath(p)
    if not os.path.exists(os.path.join(p, "environment.json")):
        raise ToolError("NOT_FOUND", "no environment.json in %s" % p, hint="the folder an environment build made")
    return p


def _env_load(ws: Workspace, path: str) -> Tuple[str, Dict[str, Any]]:
    from .environment import _env_json
    d = _env_dir(ws, path)
    try:
        return d, _env_json(d)
    except ValueError as exc:
        raise ToolError("BAD_ENVIRONMENT", str(exc))


UPGRADE_ARGS = {
    "options": {"type": "array", "items": {"type": "string"}, "description": "Option ids from get_environment (U1, ...)."},
    "path_name": {"type": "string", "enum": ["value", "fit", "batch"], "description": "Take a path's changes: best value, "
                  "most boards, biggest batches."},
    "steps": {"type": "integer", "description": "Only the path's first N changes."},
    "changes": {"type": "string", "description": "Changes of your own: 'station-1:can+1,ethernet+1,ain+8,supplies+2,"
                "plate=400x300; supply_v=48; max_batch=16; new_station'."},
    "fit": {"type": "array", "items": {"type": "string"}, "description": "Boards that must fit."},
    "batch": {"type": "object", "description": "Board -> the batch it must have, e.g. {\"sensor_04\": 8}."},
    "budget": {"type": "number", "description": "At most this much (the spec's currency)."},
}


def _upgrade_goal(env: Dict[str, Any], options: Optional[List[str]] = None, path_name: Optional[str] = None,
                  steps: Optional[int] = None, changes: Optional[str] = None, fit: Optional[List[str]] = None,
                  batch: Optional[Dict[str, Any]] = None, budget: Optional[float] = None) -> Dict[str, Any]:
    from . import upgrades as U
    chs: List[Dict[str, Any]] = []
    if options or path_name:
        opt = U.options(env)
        ids = {o["id"]: o for o in opt["options"]}
        for oid in options or []:
            if str(oid).upper() not in ids:
                raise ToolError("NOT_FOUND", "no option %s" % oid, hint="get_environment lists them (%s)" % ", ".join(ids))
            chs.append(dict(ids[str(oid).upper()]["change"]))
        if path_name:
            st_ = (opt["paths"].get(path_name) or {}).get("steps") or []
            chs += [dict(x["change"]) for x in st_[:steps or len(st_)]]
    if changes:
        try:
            chs += U.parse_changes(changes)
        except ValueError as exc:
            raise ToolError("BAD_VALUE", str(exc))
    for ch in chs:
        if ch.get("kind") == "station" and "resources" in ch:
            ch["res"] = ch.pop("resources")
    try:
        bt = {str(k): int(_num(v, "batch.%s" % k, 1, 64)) for k, v in (batch or {}).items()}
        return U.plan_goal(env, fit=fit or [], batch=bt, budget=budget, changes=chs)
    except ValueError as exc:
        raise ToolError("BAD_VALUE", str(exc))


@tool("get_environment", "A built test environment: its stations, layout, what limits the boards and the best upgrade "
      "options with what they gain and cost; board=NAME gives that board's constraints.",
      {"properties": {"path": {"type": "string"}, "board": {"type": "string"},
                      "top": {"type": "integer", "description": "Upgrade options to list (default 8)."}},
       "required": ["path"]})
def t_get_environment(ws: Workspace, path: str, board: Optional[str] = None, top: int = 8):
    from . import upgrades as U
    d, env = _env_load(ws, path)
    if board:
        cons = [c for c in U.constraints(env) if str(board).lower() in (c["board"].lower(), c["folder"].lower())]
        if not cons:
            raise ToolError("NOT_FOUND", "no board %s in this environment" % board)
        return {"board": cons[0]}
    cons = U.constraints(env)
    opt = U.options(env, top=max(1, min(30, int(top or 8))))
    tally: Dict[str, int] = {}
    for c in cons:
        for w in c.get("limited_by") or []:
            tally[w] = tally.get(w, 0) + 1
    return {"name": env.get("name"), "summary": env["summary"], "layout": env.get("layout"),
            "stations": [{"name": s_["name"], "rev": s_.get("rev", "A"), "focus": s_.get("focus"), "plate_mm": s_["plate_mm"],
                          "resources": s_["resources"], "boards": len(s_["boards"]), "own": s_.get("own", False)}
                         for s_ in env["stations"]],
            "limited_by": dict(sorted(tally.items(), key=lambda kv: -kv[1])),
            "fit_none": [{"board": c["board"], "fix": (c.get("to_fit") or {}).get("changes") or (c.get("to_fit") or {}).get("note"),
                          "cost": (c.get("to_fit") or {}).get("cost")} for c in cons if c["status"] != "compatible"][:30],
            "now": {k: v for k, v in opt["now"].items() if k != "cost_parts"}, "currency": opt["currency"],
            "options": [{"id": o["id"], "text": o["text"], "boards_fit": o["gain"]["boards_fit"], "slots": o["gain"]["slots"],
                         "cost": o["gain"]["cost"], "net": o["gain"]["net"], "flags": o["flags"]} for o in opt["options"]],
            "paths": {k: {"steps": [x["text"] for x in v["steps"]], "total": v["total"]} for k, v in opt["paths"].items()},
            "files": {"constraints": os.path.join(d, "constraints.md"), "upgrades": os.path.join(d, "upgrades.md")}}


@tool("plan_environment_upgrade", "What changing a test environment gains and costs, without changing it: option ids, "
      "a path, changes of your own, or goals (boards that must fit, batches, a budget) solved at the lowest cost.",
      {"properties": dict({"path": {"type": "string"}}, **UPGRADE_ARGS), "required": ["path"]})
def t_plan_environment_upgrade(ws: Workspace, path: str, **kw):
    _d, env = _env_load(ws, path)
    return _upgrade_goal(env, **kw)


@tool("upgrade_environment", "Make a test environment upgrade and rebuild it in the background (hours for 100+ boards): "
      "the stations keep every pin, so existing fixtures still plug in. Same arguments as plan_environment_upgrade; "
      "needs confirm=true.",
      {"properties": dict(dict({"path": {"type": "string"}}, **UPGRADE_ARGS),
                          **{"dry_run": WRITE_COMMON["dry_run"], "confirm": WRITE_COMMON["confirm"],
                             "when_done": {"type": "string", "enum": ["none", "sleep", "hibernate", "shutdown"]}}),
       "required": ["path"]}, read_only=False)
def t_upgrade_environment(ws: Workspace, path: str, dry_run: bool = False, confirm: bool = False,
                          when_done: str = "none", **kw):
    d, env = _env_load(ws, path)
    goal = _upgrade_goal(env, **kw)
    if not goal["changes"]:
        return dict(goal, started=False, note="nothing to change")
    if dry_run or not confirm:
        return dict(goal, started=False, note="CONFIRM: call again with confirm=true to make these changes and rebuild "
                                               "the environment in the background")
    from .environment_job import launch
    res = launch({"action": "upgrade", "env_dir": d, "out": d, "boards": [],
                  "changes": [{k: v for k, v in c.items() if k != "text"} for c in goal["changes"]],
                  "power": {"after": when_done}}, window=False)
    if not res.get("ok"):
        raise ToolError("NOT_STARTED", res.get("reason") or "the job did not start")
    return dict(goal, started=True, pid=res["pid"], log=res["log"], status_tool="get_environment_job")


@tool("add_boards_to_environment", "Add boards (project folders or .kicad_pcb files) to a built test environment in the "
      "background: its stations stay, its boards are reused, the swap videos get the new boards. Needs confirm=true.",
      {"properties": {"path": {"type": "string"}, "boards": {"type": "array", "items": {"type": "string"}},
                      "new_stations": {"type": "boolean", "description": "Design a further station for new boards no "
                                       "station takes (within max_stations; default true)."},
                      "dry_run": WRITE_COMMON["dry_run"], "confirm": WRITE_COMMON["confirm"],
                      "when_done": {"type": "string", "enum": ["none", "sleep", "hibernate", "shutdown"]}},
       "required": ["path", "boards"]}, read_only=False)
def t_add_boards_to_environment(ws: Workspace, path: str, boards: List[str], new_stations: bool = True,
                                dry_run: bool = False, confirm: bool = False, when_done: str = "none"):
    from .environment import discover
    d, env = _env_load(ws, path)
    base = ws.design.project_dir if ws.design is not None else os.getcwd()
    full = [b if os.path.isabs(b) else os.path.abspath(os.path.join(base, b)) for b in boards or []]
    found, warns = discover(full)
    known = {b["folder"] for b in env.get("boards") or []}
    new = [f for f in found if os.path.basename(os.path.dirname(f)) not in known]
    if not found:
        raise ToolError("NOT_FOUND", "no KiCad boards in %s" % ", ".join(boards or []), hint="project folders or .kicad_pcb files")
    info = {"boards_found": len(found), "new": [os.path.splitext(os.path.basename(f))[0] for f in new][:40],
            "warnings": warns[:10]}
    if dry_run or not confirm:
        return dict(info, started=False, note="CONFIRM: call again with confirm=true to build them into the environment")
    from .environment_job import launch
    res = launch({"action": "add", "env_dir": d, "out": d, "boards": found, "new_stations": bool(new_stations),
                  "power": {"after": when_done}}, window=False)
    if not res.get("ok"):
        raise ToolError("NOT_STARTED", res.get("reason") or "the job did not start")
    return dict(info, started=True, pid=res["pid"], log=res["log"], status_tool="get_environment_job")


@tool("get_environment_job", "How far a test environment's background job is (build, add, upgrade): state, message, "
      "fraction; and what it will do with the computer's power afterwards.",
      {"properties": {"path": {"type": "string"}}, "required": ["path"]})
def t_get_environment_job(ws: Workspace, path: str):
    from . import environment_job as J
    from . import power as PW
    p = str(path or "")
    p = os.path.abspath(p if os.path.isabs(p) else os.path.join(ws.design.project_dir if ws.design is not None else os.getcwd(), p))
    st = J.status(p)
    if not st:
        raise ToolError("NOT_FOUND", "no environment job in %s" % p)
    pw = PW.status(p) or {}
    return {"state": st.get("state"), "message": st.get("message"), "fraction": st.get("fraction"),
            "elapsed_s": st.get("elapsed_s"), "running": J.running(p), "summary": st.get("summary"),
            "power": {k: pw.get(k) for k in ("state", "after", "describe", "at") if pw.get(k) is not None} or None}


STACK_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "name": {"type": "string"},
        "refs": {"type": "array", "items": {"type": "string"}, "description": "Connectors it plugs into, or the footprint "
                 "that draws the stacked board."},
        "side": {"type": "string", "enum": ["top", "bottom"]},
        "outline": {"type": "array", "items": {"type": "number"}, "description": "[x0, y0, x1, y1]: its shadow on this "
                    "board, KiCad board coordinates in mm. Default: outline of refs (an estimate for connectors)."},
        "height_mm": {"type": "number", "description": "Top of the stack above this board's surface."},
        "fitted_at_test": {"type": "boolean", "description": "true (default): on the board during the test; false: "
                           "fitted after it."},
    },
}


@tool("set_stacked_boards", "Declare boards stacked on this one (daughter board, shield, display): parts, outline, height, "
      "fitted during the test or after. Replaces the whole list; [] clears it.",
      {"properties": dict({"boards": {"type": "array", "items": STACK_SCHEMA}}, **WRITE_COMMON), "required": ["boards"]},
      read_only=False, writes=True)
def t_set_stacked_boards(ws: Workspace, boards: List[Dict[str, Any]], rationale: str = "", dry_run: bool = False,
                         confirm: bool = False):
    from .stack import validate_entry
    d = ws._d()
    clean, names = [], set()
    for i, e in enumerate(boards or []):
        if not isinstance(e, dict):
            raise ToolError("BAD_VALUE", "boards[%d] must be an object" % i)
        e = dict(e)
        e["refs"] = [ws.resolve_ref(r).ref for r in e.get("refs") or []]
        if e.get("outline") is not None:
            if not isinstance(e["outline"], list) or len(e["outline"]) != 4:
                raise ToolError("BAD_VALUE", "outline must be [x0, y0, x1, y1] in mm")
            e["outline"] = [round(_num(v, "outline", -5000, 5000), 3) for v in e["outline"]]
        if e.get("height_mm") is not None:
            e["height_mm"] = _num(e["height_mm"], "height_mm", 0.5, 150)
        e["name"] = str(e.get("name") or "stack%d" % (i + 1))
        if e["name"] in names:
            raise ToolError("BAD_VALUE", "two stacked boards are called %r" % e["name"])
        names.add(e["name"])
        problem = validate_entry(e, set(d.board.by_ref))
        if problem:
            raise ToolError("BAD_VALUE", problem)
        clean.append({k: e[k] for k in ("name", "refs", "side", "outline", "height_mm", "fitted_at_test") if e.get(k) is not None})

    def mutate(cfg):
        cfg["stack"] = clean
        return ["%s: no outline given; the outline of %s is used (likely smaller than the board)" % (e["name"], ", ".join(e["refs"]))
                for e in clean if "outline" not in e and e.get("refs")]
    return ws.apply(mutate, rationale, dry_run, confirm, what="set stacked boards %s" % sorted(names))


_UNSET = object()


def _class_ref(ws: Workspace, system: Optional[str], cls: Any):
    """Validate a (system, class) pair against the loaded class systems -> (System, canonical class string)."""
    from .classsys import SystemError_
    d = ws._d()
    try:
        s = d.class_system(system)
    except SystemError_ as e:
        raise ToolError("UNKNOWN_CLASS_SYSTEM", str(e), known=sorted(d.class_systems()))
    if cls in (None, ""):
        return s, None
    try:
        return s, s.format_class(s.parse_class(cls))
    except SystemError_ as e:
        raise ToolError("BAD_VALUE", str(e), classes=s.all_classes(16))


@tool("set_analysis_options", "Analysis settings: class system + class you will order (class_system e.g. 'jlcpcb-2026', tech_class "
      "e.g. '6C' or 'standard'), extra class checks (class_checks e.g. ['ipc-2221-voltage:B2']), tech_context (finish, mask_color, "
      "edge_process, system parameters), tech_layers, base_copper_um, fab_profile, fab_overrides, thresholds, ignore_checks. "
      "list_class_systems shows the choices.",
      {"properties": dict({"class_system": {"type": ["string", "null"], "description": "Class system id; null = default (eurocircuits-2024)."},
                           "tech_class": {"type": ["string", "null"], "description": "Class of that system, e.g. 6C, standard, 4-layer; null clears it."},
                           "class_checks": {"type": "array", "items": {"type": "string"},
                                            "description": "Extra systems to check, 'system' or 'system:class'; replaces the list."},
                           "tech_context": {"type": "object", "description": "Merged into tech.context; null value removes a key. "
                                            "e.g. {finish: 'ENIG', mask_color: 'black', registration_mm: 0.05}"},
                           "tech_layers": {"type": ["integer", "null"], "description": "Copper layer count you will order."},
                           "base_copper_um": {"type": ["number", "null"], "description": "Outer base copper in um (12/18/35/70/105)."},
                           "fab_profile": {"type": "string", "enum": sorted(FAB_PROFILES)},
                           "fab_overrides": {"type": "object", "description": "e.g. {min_track: 0.1}"},
                           "thresholds": {"type": "object", "description": "Keys of the 'analysis' config section, e.g. decoupling_max_mm."},
                           "ignore_checks": {"type": "array", "items": {"type": "string"}}}, **WRITE_COMMON)},
      read_only=False, writes=True)
def t_set_analysis(ws: Workspace, fab_profile=None, fab_overrides=None, thresholds=None, ignore_checks=None,
                   class_system=_UNSET, tech_class=_UNSET, class_checks=None, tech_context=None, tech_layers=_UNSET,
                   base_copper_um=_UNSET, rationale: str = "", dry_run: bool = False, confirm: bool = False):
    from .analysis import CHECKS
    from .classsys import CONTEXT_KEYS, system_id_from_config
    d = ws._d()
    sys_id = system_id_from_config(d.cfg) if class_system is _UNSET else class_system
    system, canon = _class_ref(ws, sys_id, None if tech_class in (_UNSET, None) else tech_class)
    if tech_class not in (_UNSET, None):
        tech_class = canon
    checks_clean = None
    if class_checks is not None:
        checks_clean = []
        for c in class_checks:
            sid, _, cl = str(c).partition(":")
            s2, canon2 = _class_ref(ws, sid.strip(), cl.strip() or None)
            checks_clean.append(s2.id + ((":" + canon2) if canon2 else ""))
    if tech_context:
        known = set(CONTEXT_KEYS) | set(system.params)
        for sid in [c.split(":")[0] for c in (checks_clean or (d.cfg.get("tech") or {}).get("checks") or [])]:
            if sid in d.class_systems():
                known |= set(d.class_systems()[sid].params)
        for k in tech_context:
            if k not in known:
                raise ToolError("UNKNOWN_ARGUMENT", "unknown tech_context key %r" % k, allowed=sorted(known),
                                did_you_mean=difflib.get_close_matches(k, sorted(known), 2))
    if tech_layers is not _UNSET and tech_layers is not None:
        tech_layers = int(_num(tech_layers, "tech_layers", 1, 64))
    if base_copper_um is not _UNSET and base_copper_um is not None:
        base_copper_um = _num(base_copper_um, "base_copper_um", 5, 420)
    ids = {c[0] for c in CHECKS}
    for ck in ignore_checks or []:
        if ck not in ids:
            raise ToolError("UNKNOWN_CHECK", "no check %r" % ck, did_you_mean=difflib.get_close_matches(ck, sorted(ids), 3))
    allowed_fab = set(FAB_PROFILES["standard"])
    for k in (fab_overrides or {}):
        if k not in allowed_fab:
            raise ToolError("UNKNOWN_ARGUMENT", "unknown fab key %r" % k, allowed=sorted(allowed_fab))
    allowed_thr = {k for k, v in DEFAULTS["analysis"].items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    for k in (thresholds or {}):
        if k not in allowed_thr:
            raise ToolError("UNKNOWN_ARGUMENT", "unknown threshold %r" % k, allowed=sorted(allowed_thr))

    def mutate(cfg):
        if fab_profile:
            cfg["fab_profile"] = fab_profile
        if fab_overrides:
            cfg.setdefault("fab_overrides", {}).update({k: _num(v, k, 0, 50) for k, v in fab_overrides.items()})
        if thresholds:
            cfg.setdefault("analysis", {}).update({k: _num(v, k, 0, 1e6) for k, v in thresholds.items()})
        if ignore_checks is not None:
            cfg.setdefault("analysis", {})["ignore_checks"] = list(ignore_checks)
        w = []
        tech = cfg.setdefault("tech", {})
        if class_system is not _UNSET:
            tech["system"] = class_system
            tech.pop("table", None)
            if tech_class is _UNSET and tech.get("class"):
                tech["class"] = None
                w.append("the ordered class was cleared because the class system changed")
        for key, val in (("class", tech_class), ("layers", tech_layers), ("base_copper_um", base_copper_um)):
            if val is not _UNSET:
                tech[key] = val
        if checks_clean is not None:
            tech["checks"] = checks_clean
        if tech_context:
            ctx = tech.setdefault("context", {})
            for k, v in tech_context.items():
                if v is None:
                    ctx.pop(k, None)
                else:
                    ctx[k] = v
        if tech_class not in (_UNSET, None):
            r = d.evaluate_class(system.id, tech_class)
            if not r["target"]["ok"]:
                w.append("CONFIRM: the board breaks %s %s (needs %s); you will get violations to fix" % (
                    system.short, tech_class, r["required"].get("class") or "a finer process"))
        return w
    return ws.apply(mutate, rationale, dry_run, confirm, what="set analysis options")


@tool("list_class_systems", "Technology-class systems available (fab class tables, fab capability tiers, IPC standards), "
      "with their dimensions and levels. Use the id with set_analysis_options/check_classes.",
      {"properties": {"kind": {"type": "string", "enum": ["fab-classes", "fab-capability", "standard"]}}})
def t_list_class_systems(ws: Workspace, kind: Optional[str] = None):
    d = ws._d()
    out = []
    for sid, s in sorted(d.class_systems().items()):
        if kind and s.kind != kind:
            continue
        out.append({"id": sid, "name": s.name, "kind": s.kind, "status": s.status, "origin": s.origin,
                    "classes": {dim.id: [l.id for l in dim.levels] for dim in s.dims},
                    "choose": {dim.id: dim.choose for dim in s.dims}, "format": s.class_format})
    from .classsys import system_id_from_config
    return {"systems": out, "ordered": {"system": system_id_from_config(d.cfg), "class": (d.cfg.get("tech") or {}).get("class")},
            "note": "status: verified = checked against the source on the 'verified' date; secondary = from reproductions; "
                    "partial = only part of the source encoded."}


@tool("describe_class_system", "Limits of a class system: per level, or resolved for this board (resolved=true with a class).",
      {"properties": {"system": {"type": "string"}, "class": {"type": "string", "description": "e.g. 6C; omit for all levels."},
                      "resolved": {"type": "boolean", "description": "Resolve conditional rules for this board (needs class)."}},
       "required": ["system"]})
def t_describe_class_system(ws: Workspace, system: str, resolved: bool = False, **kw):
    from .classsys import describe
    cls = kw.get("class")
    s, canon = _class_ref(ws, system, cls)
    out = describe(s, ws._d().class_context() if (resolved and canon) else None, canon)
    out.pop("always", None) if canon and resolved else None
    return {"system": out}


@tool("check_classes", "Evaluate the board against class systems: class needed, fits or not, and the features that block. "
      "Without arguments: every system. With target: compliance with that class of one system.",
      {"properties": {"systems": {"type": "array", "items": {"type": "string"}}, "target": {"type": "string"},
                      "max_items": {"type": "integer", "description": "Violation locations per parameter (default 3)."}}})
def t_check_classes(ws: Workspace, systems: Optional[List[str]] = None, target: Optional[str] = None, max_items: int = 3):
    d = ws._d()
    if target:
        if not systems or len(systems) != 1:
            raise ToolError("BAD_VALUE", "target needs exactly one system in systems")
        s, canon = _class_ref(ws, systems[0], target)
        r = d.evaluate_class(s.id, canon)
        tg = r["target"]
        viol = dict(tg["violations"])
        viol.update((r.get("process") or {}).get("violations") or {})
        return {"system": s.id, "class": canon, "ok": tg["ok"], "needs": r["required"].get("class"),
                "violations": {m: {"label": v["label"], "count": v["count"], "worst": v.get("worst"),
                                   "limit": v.get("limit") if v.get("limit") is not None else v.get("limits"),
                                   "severity": v.get("severity"), "items": (v.get("items") or [])[:max_items]}
                               for m, v in viol.items()},
                "manual": tg.get("manual"), "services": tg.get("services")}
    for sid in systems or []:
        _class_ref(ws, sid, None)
    return {"overview": d.class_overview(systems), "context": {k: v for k, v in d.class_context().items() if not k.startswith("_")}}


@tool("add_class_system", "Add or replace a class system from a JSON definition (fab capability page, class table, standard). "
      "It is validated first (metric names, units, conditions, level order, its own test vectors) and stored in the project "
      "config (tech.systems). get_schema('class_system') gives the format, metric names and a template.",
      {"properties": dict({"definition": {"type": "object"}}, **WRITE_COMMON), "required": ["definition"]},
      read_only=False, writes=True)
def t_add_class_system(ws: Workspace, definition: Dict[str, Any], rationale: str = "", dry_run: bool = False, confirm: bool = False):
    from .classsys import validate
    from .classsys.registry import builtin_raw
    r = validate(definition)
    if not r["ok"]:
        raise ToolError("INVALID_DEFINITION", "%d error(s) in the class system" % len(r["errors"]), errors=r["errors"][:20],
                        warnings=r["warnings"][:10], tests=[t for t in r["tests"] if not t["ok"]][:5])
    sid = definition["id"]

    def mutate(cfg):
        w = list(r["warnings"][:5])
        if sid in builtin_raw():
            w.append("CONFIRM: %s replaces the built-in system with the same id" % sid)
        if not definition.get("tests"):
            w.append("no test vectors: add 'tests' so mistakes in the table are caught")
        cfg.setdefault("tech", {}).setdefault("systems", {})[sid] = copy.deepcopy(definition)
        return w
    out = ws.apply(mutate, rationale, dry_run, confirm, what="add class system %s" % sid)
    out["tests"] = ["%s: %s" % (t["name"], "ok" if t["ok"] else "; ".join(t["problems"])) for t in r["tests"]]
    return out


@tool("waive_finding", "Accept a weak point on purpose (kept out of the score, listed as waived in the spec). Needs a reason.",
      {"properties": dict({"check_id": {"type": "string", "description": "Check id like ASM-MLCC-EDGE, or a finding uid like F004."},
                           "refs": {"type": "array", "items": {"type": "string"}, "description": "Limit to these refs (default: all)."},
                           "reason": {"type": "string"}}, **WRITE_COMMON), "required": ["check_id", "reason"]},
      read_only=False, writes=True)
def t_waive(ws: Workspace, check_id: str, reason: str, refs: Optional[List[str]] = None, rationale: str = "",
            dry_run: bool = False, confirm: bool = False):
    findings, _ = ws.analysis()
    f = next((x for x in findings if x.uid == check_id), None)
    cid = f.id if f else check_id
    if not any(x.id == cid for x in findings):
        raise ToolError("UNKNOWN_FINDING", "no current finding with id %r" % check_id,
                        did_you_mean=difflib.get_close_matches(check_id, sorted({x.id for x in findings}), 3))
    if len((reason or "").strip()) < 8:
        raise ToolError("BAD_VALUE", "reason must explain why the risk is acceptable (>= 8 characters)")
    rr = [ws.resolve_ref(r).ref for r in (refs or [])]
    sev = next(x.severity for x in findings if x.id == cid)

    def mutate(cfg):
        w = cfg.setdefault("analysis", {}).setdefault("waivers", [])
        w.append({"id": cid, "refs": rr, "reason": reason.strip()[:300], "by": ws.actor,
                  "date": datetime.date.today().isoformat()})
        return ["CONFIRM: waiving a critical finding (%s)" % cid] if sev == "critical" else []
    return ws.apply(mutate, rationale or reason, dry_run, confirm, what="waive %s" % cid)


STEP_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["name", "type"],
    "properties": {
        "id": {"type": "string", "description": "Optional unique id (auto C<n>)."},
        "name": {"type": "string"},
        "type": {"type": "string", "enum": ["voltage", "current", "wait", "manual", "gpio", "uart_expect", "command"]},
        "power": {"type": "string", "enum": ["on", "off"], "description": "Required DUT power state (default on)."},
        "checks": {"type": "array", "description": "voltage: [{net, min_v, max_v}]", "items": {
            "type": "object", "additionalProperties": False, "required": ["net"],
            "properties": {"net": {"type": "string"}, "min_v": V_NUM, "max_v": V_NUM, "id": {"type": "string"}}}},
        "min_a": {"type": "number"}, "max_a": {"type": "number"},
        "wait_ms": {"type": "integer"},
        "instruction": {"type": "string", "description": "manual: what the operator does/checks (pass/fail prompt)."},
        "gpio": {"type": "object", "description": "{pin, level 0|1, hold_ms}"},
        "uart": {"type": "object", "description": "{port, baud, send, expect (regex), timeout_ms}"},
        "command": {"type": "object", "description": "{argv: [..], timeout_s, expect_exit}. Runs on the tester only with --allow-commands."},
        "abort_on_fail": {"type": "boolean"},
    },
}


@tool("add_test_step", "Add a functional/custom test step (voltage checks on probed nets, current window, wait, operator "
      "check, GPIO, UART expect, command). Inserted before power-off. See get_schema('test_step').",
      {"properties": dict({"step": STEP_SCHEMA}, **WRITE_COMMON), "required": ["step"]}, read_only=False, writes=True)
def t_add_step(ws: Workspace, step: Dict[str, Any], rationale: str = "", dry_run: bool = False, confirm: bool = False):
    d = ws._d()
    plan = ws.plan()
    st = _validate_step(ws, step, plan)
    existing = [s.get("id") for s in plan["sequence"]] + [s.get("id") for s in d.cfg["testplan"].get("custom_steps") or []]
    if st.get("id") and st["id"] in existing:
        raise ToolError("DUPLICATE_ID", "step id %r already exists" % st["id"])
    if not st.get("id"):
        n = len(d.cfg["testplan"].get("custom_steps") or []) + 1
        while "C%d" % n in existing:
            n += 1
        st["id"] = "C%d" % n

    def mutate(cfg):
        cfg.setdefault("testplan", {}).setdefault("custom_steps", []).append(st)
        return ["CONFIRM: command steps run programs on the tester; review argv %s" % st["command"].get("argv")] \
            if st["type"] == "command" else []
    r = ws.apply(mutate, rationale, dry_run, confirm, what="add step %s" % st["id"])
    r["step_id"] = st["id"]
    return r


def _validate_step(ws: Workspace, step: Dict[str, Any], plan) -> Dict[str, Any]:
    extra = set(step) - set(STEP_SCHEMA["properties"])
    if extra:
        raise ToolError("UNKNOWN_ARGUMENT", "unknown step field(s): %s" % sorted(extra), allowed=sorted(STEP_SCHEMA["properties"]))
    st = {k: v for k, v in step.items() if v is not None}
    if not str(st.get("name", "")).strip():
        raise ToolError("MISSING_ARGUMENT", "step.name is required")
    t = st.get("type")
    if t not in STEP_SCHEMA["properties"]["type"]["enum"]:
        raise ToolError("BAD_VALUE", "step.type must be one of %s" % STEP_SCHEMA["properties"]["type"]["enum"])
    st.setdefault("power", "on")
    chans = {p["net"]: p for p in plan["probes"] if p.get("channel")}
    if t == "voltage":
        if not st.get("checks"):
            raise ToolError("MISSING_ARGUMENT", "voltage steps need checks: [{net, min_v, max_v}]")
        out = []
        for i, c in enumerate(st["checks"]):
            net = ws.resolve_net(c["net"])
            p = chans.get(net)
            if p is None:
                raise ToolError("NET_NOT_PROBED", "%s has no measurement channel in the current plan" % net,
                                hint="set_test_options(include_nets=[...]) adds a probe if the net is reachable; get_net shows why not")
            lo, hi = parse_v(c.get("min_v"), "min_v"), parse_v(c.get("max_v"), "max_v")
            if lo is None or hi is None:
                raise ToolError("MISSING_ARGUMENT", "check on %s needs both min_v and max_v" % net)
            if lo >= hi:
                raise ToolError("BAD_LIMITS", "check on %s: min_v must be below max_v" % net)
            fs = (p.get("divider") or {}).get("full_scale_v") or plan["adc"]["max_input_v"]
            if hi > fs * 1.02:
                raise ToolError("OUT_OF_RANGE", "max_v %g V on %s is above the channel full scale (%g V)" % (hi, net, fs),
                                hint="set_net_specs with a higher max_v first so the divider is resized")
            out.append({"id": c.get("id") or "V%d_%s" % (i + 1, C.base_net(net)), "net": net, "channel": p["channel"],
                        "min_v": lo, "max_v": hi})
        st["checks"] = out
    elif t == "current":
        lo, hi = _num(st.get("min_a"), "min_a", 0, 20), _num(st.get("max_a"), "max_a", 0, 20)
        if hi is None:
            raise ToolError("MISSING_ARGUMENT", "current steps need max_a (and optional min_a)")
        if lo is not None and lo >= hi:
            raise ToolError("BAD_LIMITS", "min_a must be below max_a")
    elif t == "wait":
        st["wait_ms"] = int(_num(st.get("wait_ms"), "wait_ms", 1, 600000) or 0)
        if not st["wait_ms"]:
            raise ToolError("MISSING_ARGUMENT", "wait steps need wait_ms")
    elif t == "manual":
        if len(str(st.get("instruction", "")).strip()) < 5:
            raise ToolError("MISSING_ARGUMENT", "manual steps need an instruction for the operator")
    elif t == "gpio":
        g = st.get("gpio") or {}
        if not isinstance(g.get("pin"), int) or g.get("level") not in (0, 1):
            raise ToolError("BAD_VALUE", "gpio needs {pin: int, level: 0|1, hold_ms?}")
        _num(g.get("pin"), "gpio.pin", 0, 64)
    elif t == "uart_expect":
        u = st.get("uart") or {}
        if not u.get("expect"):
            raise ToolError("MISSING_ARGUMENT", "uart needs at least {expect: regex}")
        try:
            re.compile(u["expect"])
        except re.error as e:
            raise ToolError("BAD_VALUE", "uart.expect is not a valid regex: %s" % e)
        u.setdefault("port", "/dev/ttyUSB0")
        u.setdefault("baud", 115200)
        u.setdefault("timeout_ms", 3000)
        _num(u["baud"], "uart.baud", 300, 4000000)
        st["uart"] = u
    elif t == "command":
        c = st.get("command") or {}
        if not isinstance(c.get("argv"), list) or not c["argv"] or not all(isinstance(a, str) for a in c["argv"]):
            raise ToolError("BAD_VALUE", "command needs argv: [program, args...] (no shell strings)")
        c.setdefault("timeout_s", 30)
        c.setdefault("expect_exit", 0)
        st["command"] = c
    return st


@tool("remove_test_step", "Remove a custom step by id (built-in steps S1-S9 come from the board; change options instead).",
      {"properties": dict({"id": {"type": "string"}}, **WRITE_COMMON), "required": ["id"]}, read_only=False, writes=True)
def t_remove_step(ws: Workspace, id: str, rationale: str = "", dry_run: bool = False, confirm: bool = False):
    d = ws._d()
    steps = d.cfg["testplan"].get("custom_steps") or []
    if not any(s.get("id") == id for s in steps):
        raise ToolError("UNKNOWN_STEP", "no custom step %r" % id, existing=[s.get("id") for s in steps])

    def mutate(cfg):
        cs = cfg.setdefault("testplan", {}).get("custom_steps") or []
        cfg["testplan"]["custom_steps"] = [s for s in cs if s.get("id") != id]
        return []
    return ws.apply(mutate, rationale, dry_run, confirm, what="remove step %s" % id)


@tool("add_manual_check", "Add a visual/operator inspection item to step S1 (e.g. 'connector J1 latch closed').",
      {"properties": dict({"check": {"type": "string"}, "refs": {"type": "array", "items": {"type": "string"}}}, **WRITE_COMMON),
       "required": ["check"]}, read_only=False, writes=True)
def t_manual(ws: Workspace, check: str, refs: Optional[List[str]] = None, rationale: str = "", dry_run: bool = False,
             confirm: bool = False):
    if len(check.strip()) < 5:
        raise ToolError("BAD_VALUE", "check text too short")
    rr = [ws.resolve_ref(r).ref for r in (refs or [])]

    def mutate(cfg):
        mc = cfg.setdefault("testplan", {}).setdefault("manual_checks", [])
        mc.append({"id": "VIS-USER-%d" % (len(mc) + 1), "check": check.strip()[:200], "refs": rr})
        return []
    return ws.apply(mutate, rationale, dry_run, confirm, what="add manual check")


@tool("undo_last", "Revert the previous write (up to 20 steps; kept across CLI calls and sessions).", {"properties": {}},
      read_only=False, writes=True)
def t_undo(ws: Workspace):
    d = ws._d()
    if not ws.history:
        raise ToolError("NOTHING_TO_UNDO", "no earlier write to revert")
    prev = ws.history.pop()
    d.reload_config(prev)
    ws._invalidate()
    if ws.persist and d.config_path:
        save_user_config(d.config_path, prev)
        ws._save_undo()
    return {"reverted": True, "remaining_undo": len(ws.history)}


@tool("validate", "Check that decisions are complete and consistent. errors must be fixed; warnings are advisory.", {})
def t_validate(ws: Workspace):
    d = ws._d()
    findings, plan = ws.analysis()
    errors, warnings = [], []
    if not plan["supply"].get("net"):
        errors.append({"code": "NO_SUPPLY", "message": "no supply net", "fix": "set_supply"})
    elif plan["supply"].get("voltage") is None:
        errors.append({"code": "NO_SUPPLY_VOLTAGE", "message": "supply voltage unknown", "fix": "set_supply(voltage=...)"})
    for st in plan["sequence"]:
        for c in st.get("checks", []) or []:
            if st["type"] == "voltage" and c.get("min_v") is None and not c.get("learn"):
                errors.append({"code": "NO_LIMITS", "message": "check %s has no limits" % c["id"], "fix": "set_net_specs"})
    rails_unknown = [n for n in d.nets_of_kind("power") if d.net_spec(n)["min_v"] is None]
    probed = {p["net"] for p in plan["probes"] if p.get("channel")}
    for n in rails_unknown:
        (errors if n in probed else warnings).append({"code": "RAIL_NO_LIMITS", "message": "rail %s has no expected voltage" % n,
                                                      "fix": "set_net_specs"})
    for p in plan["probes"]:
        e = p.get("expected") or {}
        fs = (p.get("divider") or {}).get("full_scale_v") or plan["adc"]["max_input_v"]
        if e.get("max_v") is not None and abs(e["max_v"]) > fs * 1.02:
            errors.append({"code": "OVER_RANGE", "message": "%s max %g V exceeds channel full scale %g V" % (p["net"], e["max_v"], fs),
                           "fix": "enable fixture dividers"})
        if e.get("min_v") is not None and e["min_v"] < -0.05:
            warnings.append({"code": "NEGATIVE_RAIL", "message": "%s is negative; single-ended ADCs cannot read it" % p["net"]})
    learn = sum(1 for st in plan["sequence"] for c in st.get("checks", []) or [] if c.get("learn"))
    if learn:
        warnings.append({"code": "LEARN_PENDING", "message": "%d check(s) will take limits from a golden unit (runner learn mode)" % learn})
    for u in plan["coverage"]["untestable"]:
        if d.net_kind(u["net"]) in ("power", "ground"):
            warnings.append({"code": "UNTESTABLE_RAIL", "message": "%s: %s" % (u["net"], u["reason"]), "fix": "add a test pad or exclude"})
    for st in plan.get("stack") or []:
        for k in ("outline_source", "height_source"):
            if str(st.get(k, "")).startswith("estimate"):
                warnings.append({"code": "STACK_ESTIMATE", "message": "stacked board %s: %s" % (st["name"], st[k]),
                                 "fix": "set_stacked_boards"})
    crit = [f.uid for f in findings if f.severity == "critical"]
    if crit:
        warnings.append({"code": "CRITICAL_FINDINGS", "message": "%d critical weak point(s) open: %s" % (len(crit), ", ".join(crit))})
    for w in plan["warnings"] + d.warnings:
        warnings.append({"code": "PLAN", "message": w})
    return {"valid": not errors, "errors": errors, "warnings": warnings[:40]}


@tool("generate_outputs", "Write spec JSON, test plan, fixture .kicad_pcb (+probes CSV), solder jig STL/SCAD, HTML report "
      "and the Pi runner bundle. Returns paths and a summary, not file contents.",
      {"properties": {"out_dir": {"type": "string"}, "outputs": {"type": "object", "description": "{spec, testplan, fixture, jig, report, runner, guide, visuals, video: bool}; video renders a 3D-animated explainer with KiCad and takes several minutes"},
                      "run_kicad_checks": {"type": "boolean", "description": "Run KiCad DRC/ERC via kicad-cli (slower)."}}},
      read_only=False)
def t_generate(ws: Workspace, out_dir: Optional[str] = None, outputs: Optional[Dict[str, bool]] = None,
               run_kicad_checks: bool = False):
    from .pipeline import run_pipeline
    d = ws._d()
    v = t_validate(ws)
    res = run_pipeline(d, out_dir=out_dir, outputs=outputs, include_kicad=run_kicad_checks)
    res["validation"] = {"valid": v["valid"], "errors": v["errors"]}
    return res


@tool("read_output", "Read part of a generated JSON file (spec|testplan|fixture|jig) via a JSON pointer, truncated to max_chars.",
      {"properties": {"file": {"type": "string", "enum": ["spec", "testplan", "fixture", "jig"]},
                      "pointer": {"type": "string", "description": "e.g. /electrical/supply or /probes/0"},
                      "out_dir": {"type": "string"}, "max_chars": {"type": "integer"}}, "required": ["file"]})
def t_read_output(ws: Workspace, file: str, pointer: str = "", out_dir: Optional[str] = None, max_chars: int = 4000):
    d = ws._d()
    od = out_dir or os.path.join(d.project_dir, "production_test")
    suffix = {"spec": "-spec.json", "testplan": "-testplan.json", "fixture": "-fixture.json", "jig": "-jig.json"}[file]
    path = os.path.join(od, d.board.name + suffix)
    if not os.path.exists(path):
        raise ToolError("NOT_GENERATED", "%s not found" % path, hint="call generate_outputs first")
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    for part in [p for p in pointer.split("/") if p]:
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(data, list):
            try:
                data = data[int(part)]
            except (ValueError, IndexError):
                raise ToolError("BAD_POINTER", "index %r invalid (list of %d)" % (part, len(data)))
        elif isinstance(data, dict):
            if part not in data:
                raise ToolError("BAD_POINTER", "key %r not found" % part, keys=sorted(data)[:40])
            data = data[part]
        else:
            raise ToolError("BAD_POINTER", "cannot descend into a scalar")
    text = json.dumps(data, separators=(",", ":"), default=str)
    lim = max(200, min(100000, int(max_chars or 4000)))
    out = {"value": json.loads(text) if len(text) <= lim else None}
    if len(text) > lim:
        out["truncated_text"] = text[:lim]
        out["total_chars"] = len(text)
        if isinstance(data, dict):
            out["keys"] = sorted(data)[:60]
        elif isinstance(data, list):
            out["length"] = len(data)
    return out


# =========================================================================== #
# Export / dispatch helpers
# =========================================================================== #
# Tool profiles keep the tool list (which an AI client re-sends with every request) as small as the job allows.
PROFILES: Dict[str, Optional[List[str]]] = {
    "full": None,                     # all tools
    "core": ["open_project", "get_overview", "get_open_decisions", "list_findings", "get_finding", "list_nets",
             "get_net", "set_net_specs", "set_supply", "set_test_options", "check_classes", "set_analysis_options",
             "validate", "generate_outputs", "read_output", "undo_last"],
    "readonly": [],                   # every read-only tool (filled in below)
}
# Writes that overwrite files or definitions; every other write is reversible with undo_last.
DESTRUCTIVE = {"generate_outputs", "add_class_system"}
# Writes that give the same result when repeated with the same arguments.
IDEMPOTENT_WRITES = {"set_net_specs", "set_supply", "set_test_options", "set_jig_options", "set_fixture_options",
                     "set_stacked_boards", "set_batch_and_programming",
                     "set_analysis_options", "waive_finding", "generate_outputs", "add_class_system"}


def profile_tools(profile: str = "full") -> List[str]:
    if profile not in PROFILES:
        raise ValueError("profile must be one of %s" % ", ".join(PROFILES))
    if profile == "readonly":
        return [n for n, t in TOOLS.items() if t["read_only"]]
    names = PROFILES[profile]
    return list(TOOLS) if names is None else [n for n in names if n in TOOLS]


def tool_definitions(fmt: str = "mcp", include_writes: bool = True, profile: str = "full") -> List[Dict[str, Any]]:
    """Tool schemas for MCP, Anthropic (Messages API tools) or OpenAI-style function calling."""
    out = []
    keep = set(profile_tools(profile))
    for t in TOOLS.values():
        if t["name"] not in keep or (not include_writes and not t["read_only"]):
            continue
        if fmt == "mcp":
            ro = t["read_only"]
            out.append({"name": t["name"], "title": t["name"].replace("_", " ").capitalize(),
                        "description": t["description"], "inputSchema": t["input_schema"],
                        "annotations": {"readOnlyHint": ro, "destructiveHint": (not ro) and t["name"] in DESTRUCTIVE,
                                        "idempotentHint": ro or t["name"] in IDEMPOTENT_WRITES, "openWorldHint": False}})
        elif fmt == "anthropic":
            out.append({"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]})
        elif fmt == "openai":
            out.append({"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                         "parameters": t["input_schema"]}})
        else:
            raise ValueError("format must be mcp, anthropic or openai")
    return out


def dispatch(ws: Workspace, name: str, args: Any) -> str:
    """Run a tool from a model's tool call; returns compact JSON text for the tool result."""
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except ValueError:
            return json.dumps({"ok": False, "error": {"code": "INVALID_JSON", "message": "arguments are not valid JSON"}})
    return json.dumps(ws.call(name, args), separators=(",", ":"), default=str)
