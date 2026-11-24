"""Model Context Protocol server (stdio, JSON-RPC 2.0) - standard library only.

Lets any MCP client (Claude Code, Claude Desktop, Cursor, VS Code, Codex, local runners with MCP support)
drive PCB TestKit with its own model:

    pcbtestkit mcp --project /path/to/board.kicad_pro [--profile core|full|readonly] [--read-only]
    python3 -m pcbtestkit mcp ...            (when the repo/plugin folder is on PYTHONPATH)

What it offers:
  tools      the agent API (pcbtestkit.agent): 31 tools, or 16 with --profile core (about half the tokens)
  resources  agent guide, CLI quick reference, config defaults, class-system list/catalog/definitions,
             the open project's overview
  prompts    ready-made workflows with arguments (plan the test, review weak points, pick a fab class,
             add a fab's class system, generate outputs, a cheap DFM check)
  completion argument completion for prompt arguments and resource templates (class-system ids)

Tool results are returned once, as compact JSON text (no duplicate structuredContent), to save tokens.
Client configuration snippets: docs/MCP.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

from . import __version__
from .agent import GUIDE, TOOLS, Workspace, profile_tools, tool_definitions

SUPPORTED = ["2025-06-18", "2025-03-26", "2024-11-05"]


# --------------------------------------------------------------------------- #
# prompts
# --------------------------------------------------------------------------- #
PROMPTS: Dict[str, Dict[str, Any]] = {
    "plan_production_test": {
        "title": "Plan the production test",
        "description": "Work through the open decisions and generate all production-test outputs for the open board.",
        "arguments": [{"name": "video", "description": "yes to also render the 3D-animated explainer video (takes minutes)"}],
        "text": "Prepare this KiCad board for production testing with PCB TestKit. Workflow: get_overview, then "
                "get_open_decisions; resolve each decision with the tool named in apply_with (batch writes, one short "
                "rationale each; use your knowledge of the parts' datasheets for rail voltages and tolerances). Add "
                "functional test steps only where they add coverage. Run validate until there are no errors, then "
                "generate_outputs{video_clause}. Finish with at most 10 lines: the decisions you made and what a human "
                "must still check.",
    },
    "review_weak_points": {
        "title": "Review weak points",
        "description": "Explain the critical and major weak points and propose concrete layout fixes.",
        "arguments": [{"name": "limit", "description": "how many findings to cover (default 10)"}],
        "text": "Call list_findings with min_severity=major and limit={limit}. For each finding, call get_finding only if "
                "the summary is not enough, then propose one concrete KiCad change (what to move, widen, add or remove, "
                "and where). Do not waive anything unless the user agrees. Answer as a numbered list, one or two lines each.",
    },
    "choose_fab_class": {
        "title": "Choose the fab and class to order",
        "description": "Find which fab class systems the board fits as drawn and what blocks the others.",
        "arguments": [{"name": "system", "description": "class system id to focus on, e.g. jlcpcb-2026 (optional)"},
                      {"name": "target", "description": "class you plan to order in that system (optional)"}],
        "text": "Call check_classes with {check_args}. Report: the cheapest class the board needs in each "
                "system, which systems it fits as drawn, and for the others the 1-3 features that block them with their "
                "locations. If the user picks a system and class, store it with set_analysis_options(class_system, "
                "tech_class) and a one-line rationale.",
    },
    "add_fab_class_system": {
        "title": "Add a fab's class system",
        "description": "Turn a fab's capability page or design-rule table into a validated class-system definition.",
        "arguments": [{"name": "source", "description": "URL or pasted text of the fab's design rules", "required": True}],
        "text": "Read the fab's design rules from: {source}\nThen call get_schema with name=class_system for the template, "
                "metric names and rule syntax. Write one definition: numbers exactly as the source states them "
                "(e.g. \"4mil\", \"100um\"), conditions for limits that depend on layers/copper/finish, a note for anything "
                "the numbers cannot express, and 3-6 test vectors covering the class boundaries. Call add_class_system "
                "with dry_run=true, fix every validator error, then call it again without dry_run. Report the levels you "
                "encoded and anything you could not encode.",
    },
    "generate_outputs": {
        "title": "Generate production files",
        "description": "Validate, then write spec, test plan, fixture board, solder jig, report and runner.",
        "arguments": [{"name": "video", "description": "yes to also render the 3D-animated explainer video (takes minutes)"},
                      {"name": "out_dir", "description": "output folder (default <project>/production_test)"}],
        "text": "Call validate. If it reports errors, stop and list them. Otherwise call generate_outputs{gen_args} and "
                "report the output paths and the summary in at most 8 lines.",
    },
    "quick_dfm_check": {
        "title": "Quick DFM check (cheapest)",
        "description": "Two tool calls: board summary and the top weak points. For a fast first look.",
        "arguments": [],
        "text": "Call get_overview, then list_findings with min_severity=major and limit=8. Do not call any other tool. "
                "Answer in at most 12 lines: board, readiness score, the main weak points with refs, and the single most "
                "valuable fix.",
    },
}


def _yes(v: Any) -> bool:
    return str(v or "").strip().lower() in ("1", "y", "yes", "true", "on")


def render_prompt(name: str, args: Dict[str, Any]) -> str:
    p = PROMPTS[name]
    args = dict(args or {})
    video = _yes(args.get("video"))
    check: Dict[str, Any] = {"max_items": 3}
    if args.get("system"):
        check["systems"] = [args["system"]]
    if args.get("target"):
        check["target"] = args["target"]
    gen: Dict[str, Any] = {}
    if args.get("out_dir"):
        gen["out_dir"] = args["out_dir"]
    if video:
        gen["outputs"] = {"video": True}
    return p["text"].format(
        video_clause=" with outputs={\"video\": true}" if video else "",
        limit=int(args.get("limit") or 10),
        check_args=json.dumps(check),
        gen_args=(" with " + json.dumps(gen)) if gen else "",
        source=args.get("source") or "(ask the user for the source)",
    )


# --------------------------------------------------------------------------- #
# resources
# --------------------------------------------------------------------------- #
def _class_systems() -> Dict[str, Any]:
    from .classsys import load_systems
    systems, _warnings = load_systems({}, None)
    return systems


STATIC_RESOURCES = [
    {"uri": "pcbtestkit://guide", "name": "agent-guide", "title": "Agent workflow guide", "mimeType": "text/plain",
     "description": "The tool workflow in 10 lines: overview, decisions, writes, validate, generate."},
    {"uri": "pcbtestkit://cli", "name": "cli-quick-reference", "title": "CLI quick reference", "mimeType": "text/plain",
     "description": "Cheapest way to drive PCB TestKit from a shell, with token rules."},
    {"uri": "pcbtestkit://config/defaults", "name": "config-defaults", "title": "pcbtestkit.json defaults",
     "mimeType": "application/json", "description": "Every configuration key with its default value."},
    {"uri": "pcbtestkit://class-systems", "name": "class-systems", "title": "Built-in fab class systems",
     "mimeType": "application/json", "description": "id, name, kind, status and classes of every class system."},
    {"uri": "pcbtestkit://class-systems/catalog", "name": "class-system-catalog", "title": "Class-system metric catalog",
     "mimeType": "application/json", "description": "Metric names, aliases and context keys a class system can use."},
]
PROJECT_RESOURCE = {"uri": "pcbtestkit://project/overview", "name": "project-overview", "title": "Open project overview",
                    "mimeType": "application/json", "description": "get_overview of the board this server has open."}
TEMPLATES = [
    {"uriTemplate": "pcbtestkit://class-systems/{id}", "name": "class-system", "title": "Class-system definition",
     "mimeType": "application/json", "description": "The JSON definition of one class system (levels, limits, tests)."},
]


class Server:
    def __init__(self, project: Optional[str] = None, read_only: bool = False, profile: str = "full"):
        self.read_only = read_only
        self.profile = profile
        self.allowed = set(profile_tools(profile))
        self.allowed.add("open_project")
        self.startup_error: Optional[str] = None
        self.ws = Workspace(None, persist=True, actor="ai")
        if project:
            try:
                self.ws.open(project)
            except Exception as exc:  # start anyway; the model can call open_project
                self.startup_error = "could not open %s: %s" % (project, exc)

    # -- JSON-RPC ----------------------------------------------------------------
    def handle(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        mid = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}
        if mid is None:  # notification (initialized, cancelled, ...)
            return None
        try:
            result = self.dispatch(method, params)
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        except KeyError:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found: %s" % method}}
        except ValueError as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": str(e)}}
        except Exception as e:  # pragma: no cover
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": "%s: %s" % (type(e).__name__, e)}}

    def instructions(self) -> str:
        head = "PCB TestKit %s (profile %s%s). " % (__version__, self.profile, ", read-only" if self.read_only else "")
        if self.ws.design is not None:
            head += "Open board: %s. " % self.ws.design.board.name
        elif self.startup_error:
            head += "No board open (%s). Call open_project with a .kicad_pcb/.kicad_pro path. " % self.startup_error
        else:
            head += "No board open: call open_project with a .kicad_pcb/.kicad_pro path first. "
        return head + "\n" + GUIDE

    def dispatch(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if method == "initialize":
            req = params.get("protocolVersion")
            ver = req if req in SUPPORTED else SUPPORTED[0]
            return {"protocolVersion": ver,
                    "capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False},
                                     "resources": {"listChanged": False, "subscribe": False}, "completions": {}},
                    "serverInfo": {"name": "pcbtestkit", "title": "PCB TestKit", "version": __version__},
                    "instructions": self.instructions()}
        if method == "ping":
            return {}
        if method == "logging/setLevel":
            return {}
        if method == "tools/list":
            return {"tools": tool_definitions("mcp", include_writes=not self.read_only, profile=self.profile)
                    + ([] if "open_project" in profile_tools(self.profile) else
                       [d for d in tool_definitions("mcp") if d["name"] == "open_project"])}
        if method == "tools/call":
            return self.call_tool(params.get("name"), params.get("arguments") or {})
        if method == "prompts/list":
            return {"prompts": [{"name": k, "title": v["title"], "description": v["description"],
                                 "arguments": v["arguments"]} for k, v in PROMPTS.items()]}
        if method == "prompts/get":
            name = params.get("name")
            if name not in PROMPTS:
                raise ValueError("unknown prompt %r (have: %s)" % (name, ", ".join(PROMPTS)))
            args = params.get("arguments") or {}
            for a in PROMPTS[name]["arguments"]:
                if a.get("required") and not args.get(a["name"]):
                    raise ValueError("prompt %s needs argument %r" % (name, a["name"]))
            return {"description": PROMPTS[name]["description"],
                    "messages": [{"role": "user", "content": {"type": "text", "text": render_prompt(name, args)}}]}
        if method == "resources/list":
            res = list(STATIC_RESOURCES)
            if self.ws.design is not None:
                res.append(PROJECT_RESOURCE)
            return {"resources": res}
        if method == "resources/templates/list":
            return {"resourceTemplates": TEMPLATES}
        if method == "resources/read":
            return self.read_resource(params.get("uri") or "")
        if method == "completion/complete":
            return self.complete(params)
        raise KeyError(method)

    # -- tools ----------------------------------------------------------------
    def call_tool(self, name: Any, args: Dict[str, Any]) -> Dict[str, Any]:
        if name not in TOOLS:
            raise ValueError("unknown tool %r" % name)
        if name not in self.allowed:
            res = {"ok": False, "error": {"code": "NOT_IN_PROFILE",
                                          "message": "%s is not exposed in profile %s; restart with --profile full" % (name, self.profile)}}
        elif self.read_only and not TOOLS[name]["read_only"] and name != "open_project":
            res = {"ok": False, "error": {"code": "READ_ONLY", "message": "server started with --read-only"}}
        else:
            res = self.ws.call(name, args)
        text = json.dumps(res, separators=(",", ":"), default=str)
        return {"content": [{"type": "text", "text": text}], "isError": not res.get("ok", False)}

    # -- resources ------------------------------------------------------------
    def read_resource(self, uri: str) -> Dict[str, Any]:
        def text(body: str, mime: str = "text/plain") -> Dict[str, Any]:
            return {"contents": [{"uri": uri, "mimeType": mime, "text": body}]}

        def js(obj: Any) -> Dict[str, Any]:
            return text(json.dumps(obj, separators=(",", ":"), default=str), "application/json")

        if uri == "pcbtestkit://guide":
            return text(GUIDE)
        if uri == "pcbtestkit://cli":
            from .quickref import quick_reference
            return text(quick_reference())
        if uri == "pcbtestkit://config/defaults":
            from .config import DEFAULTS
            return js(DEFAULTS)
        if uri == "pcbtestkit://class-systems":
            rows = []
            for sid, s in sorted(_class_systems().items()):
                rows.append({"id": sid, "name": s.name, "kind": getattr(s, "kind", ""), "status": s.status,
                             "classes": s.all_classes()[:60]})
            return js(rows)
        if uri == "pcbtestkit://class-systems/catalog":
            from .classsys import ALIASES, CONTEXT_KEYS, METRICS
            return js({"metrics": {k: {kk: vv for kk, vv in v.items() if kk in ("label", "kind", "unit", "stream")}
                                   for k, v in METRICS.items()},
                       "aliases": ALIASES, "context_keys": CONTEXT_KEYS})
        if uri.startswith("pcbtestkit://class-systems/"):
            sid = uri.rsplit("/", 1)[1]
            systems = _class_systems()
            if sid not in systems:
                raise ValueError("unknown class system %r (see pcbtestkit://class-systems)" % sid)
            return js(systems[sid].raw)
        if uri == "pcbtestkit://project/overview":
            return js(self.ws.call("get_overview", {}))
        raise ValueError("unknown resource %r" % uri)

    # -- completion ------------------------------------------------------------
    def complete(self, params: Dict[str, Any]) -> Dict[str, Any]:
        ref = params.get("ref") or {}
        arg = params.get("argument") or {}
        name, value = arg.get("name"), str(arg.get("value") or "")
        values: List[str] = []
        if name in ("system", "id"):
            values = sorted(_class_systems())
        elif ref.get("type") == "ref/prompt" and name == "video":
            values = ["yes", "no"]
        hits = [v for v in values if v.startswith(value)][:100]
        return {"completion": {"values": hits, "total": len(hits), "hasMore": False}}


def serve(project: Optional[str] = None, read_only: bool = False, stdin=None, stdout=None, profile: str = "full") -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    srv = Server(project, read_only, profile)
    if srv.startup_error:
        print("pcbtestkit mcp: %s (continuing without a board)" % srv.startup_error, file=sys.stderr)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) + "\n")
            stdout.flush()
            continue
        batch = msg if isinstance(msg, list) else [msg]
        out = [r for r in (srv.handle(m) for m in batch) if r is not None]
        if out:
            stdout.write(json.dumps(out if isinstance(msg, list) else out[0], default=str) + "\n")
            stdout.flush()


def main(argv: Optional[List[str]] = None) -> int:
    """Console entry point `pcbtestkit-mcp` (same as `pcbtestkit mcp`)."""
    ap = argparse.ArgumentParser(prog="pcbtestkit-mcp", description="PCB TestKit MCP server (stdio)")
    ap.add_argument("--project", help="board or project to open at start")
    ap.add_argument("--read-only", action="store_true", help="refuse every write tool")
    ap.add_argument("--profile", choices=["full", "core", "readonly"], default="full")
    a = ap.parse_args(argv)
    serve(a.project, read_only=a.read_only, profile=a.profile)
    return 0


if __name__ == "__main__":
    sys.exit(main())
