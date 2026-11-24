"""Command line interface.

    python3 -m pcbtestkit run      <project> [--out DIR] [--no-fixture] ...   full export
    python3 -m pcbtestkit analyze  <project> [--json]                         weak points only
    python3 -m pcbtestkit init     <project>                                  write pcbtestkit.json template
    python3 -m pcbtestkit api      <tool> --project P [--args JSON]           one agent-API call (JSON out)
    python3 -m pcbtestkit tools    [--format mcp|anthropic|openai]            export tool schemas
    python3 -m pcbtestkit mcp      [--project P] [--read-only]                MCP stdio server
    python3 -m pcbtestkit classes  list | show S [--class C] | check <project> [--system S] [--target C]
                                   | validate [file.json ...] | template [--out F] | catalog
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__


def _design(args):
    from .project import Design
    overrides = {}
    if getattr(args, "probe_side", None):
        overrides.setdefault("testplan", {})["probe_side"] = args.probe_side
    if getattr(args, "panel", None):
        c, r = args.panel.lower().split("x")
        overrides.setdefault("fixture", {})["panel"] = {"cols": int(c), "rows": int(r)}
    if getattr(args, "outline", None):
        overrides.setdefault("fixture", {})["outline"] = args.outline
    if getattr(args, "jig_parts", None):
        overrides.setdefault("jig", {})["parts"] = [p.strip() for p in args.jig_parts.split(",") if p.strip()]
    if getattr(args, "fab", None):
        overrides["fab_profile"] = args.fab
    if getattr(args, "class_system", None):
        overrides.setdefault("tech", {})["system"] = args.class_system
    if getattr(args, "tech_class", None):
        overrides.setdefault("tech", {})["class"] = args.tech_class
    if getattr(args, "check_class", None):
        overrides.setdefault("tech", {})["checks"] = list(args.check_class)
    if getattr(args, "video_fps", None):
        overrides.setdefault("video", {})["fps"] = args.video_fps
    if getattr(args, "video_stills", False):
        overrides.setdefault("video", {})["animate"] = False
    return Design.load(args.project, config_path=getattr(args, "config", None), overrides=overrides or None)


def cmd_run(args) -> int:
    from .pipeline import run_pipeline
    d = _design(args)
    outputs = {"fixture": not args.no_fixture, "jig": not args.no_jig, "runner": not args.no_runner}
    if args.no_visuals:
        outputs["visuals"] = False
    if args.video:
        outputs["video"] = True

    def prog(msg, frac):
        if not args.quiet:
            print("[%3d%%] %s" % (int(frac * 100), msg), file=sys.stderr)
    res = run_pipeline(d, out_dir=args.out, outputs=outputs, progress=prog,
                       include_kicad=False if args.no_kicad else None)
    if args.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        s = res["summary"]
        print("\n%s: %s (score %d) - %s" % (d.board.name, s["verdict"], s["score"],
                                           ", ".join("%d %s" % (v, k) for k, v in s["counts"].items() if v)))
        print("Test coverage: %s%% of nets (%d/%d power nets)" % (res["coverage"]["percent"], res["coverage"]["power_nets_probed"],
                                                                  res["coverage"]["power_nets"]))
        for k, v in res["paths"].items():
            print("  %-15s %s" % (k, v))
        for w in res["fixture_warnings"] + res["warnings"]:
            print("  ! %s" % w)
    return 0


def cmd_analyze(args) -> int:
    from .pipeline import analyze
    from .analysis import summarize
    d = _design(args)
    findings, plan = analyze(d, include_kicad=False if args.no_kicad else None)
    if args.json:
        print(json.dumps({"summary": summarize(findings), "findings": [f.to_dict() for f in findings]}, indent=2, default=str))
        return 0
    for f in findings:
        print("%s %-8s %-22s %s%s" % (f.uid, f.severity.upper(), f.id, f.title, ("  [%s]" % ", ".join(f.refs[:10])) if f.refs else ""))
    s = summarize(findings)
    print("\n%s - score %d/100" % (s["verdict"], s["score"]))
    return 1 if s["counts"]["critical"] and args.strict else 0


def cmd_init(args) -> int:
    from .project import locate
    from .config import config_path_for, save_user_config
    pcb, _, _ = locate(args.project)
    path = config_path_for(os.path.dirname(pcb))
    if os.path.exists(path) and not args.force:
        print("%s already exists (use --force)" % path)
        return 1
    d = _design(args)
    sup = d.supply()
    tmpl = {"fab_profile": "standard", "supply": {"net": sup.get("net"), "voltage": sup.get("voltage"), "current_limit_a": 0.5},
            "nets": {n: {"nominal": d.net_spec(n)["nominal_v"], "tolerance_pct": 5}
                     for n in d.nets_of_kind("power") if d.net_spec(n)["nominal_v"] is not None},
            "testplan": {"probe_side": "auto", "pogo": "P75", "signals": "auto", "max_channels": 32},
            "fixture": {"outline": "rect", "panel": {"cols": 1, "rows": 1}}, "jig": {"parts": "auto", "heights": {}}}
    save_user_config(path, tmpl)
    print("wrote", path)
    return 0


def cmd_api(args) -> int:
    from .agent import Workspace
    ws = Workspace(args.project, persist=not args.no_save, actor=args.actor)
    try:
        a = json.loads(args.args) if args.args else {}
    except ValueError as e:
        print(json.dumps({"ok": False, "error": {"code": "INVALID_JSON", "message": str(e)}}))
        return 2
    res = ws.call(args.tool, a)
    print(json.dumps(res, separators=(",", ":") if args.compact else None, indent=None if args.compact else 1, default=str))
    return 0 if res.get("ok") else 1


def cmd_tools(args) -> int:
    from .agent import TOOLS, tool_definitions
    defs = tool_definitions(args.format, include_writes=not args.read_only, profile=args.profile)
    if args.name:
        t = TOOLS.get(args.name)
        if t is None:
            import difflib
            near = difflib.get_close_matches(args.name, list(TOOLS), n=3)
            print("unknown tool %r%s" % (args.name, (" - did you mean " + ", ".join(near)) if near else ""), file=sys.stderr)
            return 2
        print(json.dumps({"name": t["name"], "read_only": t["read_only"], "description": t["description"],
                          "input_schema": t["input_schema"]}, separators=(",", ":")))
        return 0
    if args.brief:
        for t in TOOLS.values():
            if any(d.get("name", d.get("function", {}).get("name")) == t["name"] for d in defs):
                print("%-22s %s %s" % (t["name"], "R" if t["read_only"] else "W", t["description"].split(". ")[0].rstrip(".")))
        return 0
    print(json.dumps(defs, indent=1))
    return 0


def cmd_guide(args) -> int:
    from .quickref import quick_reference
    print(quick_reference())
    return 0


def cmd_classes(args) -> int:
    from . import classsys as CS
    act = args.action
    if act == "template":
        txt = json.dumps(CS.TEMPLATE, indent=2)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(txt + "\n")
            print("wrote", args.out, "- edit it, then: pcbtestkit classes validate", args.out)
        else:
            print(txt)
        return 0
    if act == "catalog":
        if args.json:
            print(json.dumps({"metrics": CS.METRICS, "aliases": CS.ALIASES, "context_keys": CS.CONTEXT_KEYS}, indent=1))
            return 0
        print("Metrics (limit kind, unit) - use these names in class-system files:")
        for k, v in CS.METRICS.items():
            print("  %-26s %-8s %-6s %s" % (k, v["kind"], v.get("unit", "mm"), v["label"]))
        print("\nAliases:")
        for k, v in CS.ALIASES.items():
            print("  %-22s -> %s" % (k, ", ".join(v)))
        print("\nContext keys for 'when' conditions:")
        for k, v in CS.CONTEXT_KEYS.items():
            print("  %-16s %s" % (k, v))
        return 0
    if act == "validate":
        files = args.target or []
        results = []
        if files:
            for f in files:
                with open(f, encoding="utf-8") as fh:
                    results.append((f, CS.validate(json.load(fh))))
        else:
            from .classsys.registry import builtin_raw, user_dirs
            import glob
            for sid, raw in builtin_raw().items():
                results.append(("built-in " + sid, CS.validate(raw)))
            for d in user_dirs(None, os.getcwd()):
                for f in sorted(glob.glob(os.path.join(d, "*.json"))):
                    with open(f, encoding="utf-8") as fh:
                        results.append((f, CS.validate(json.load(fh))))
        bad = 0
        for name, r in results:
            print("%s %s  (%d test vector(s))" % ("OK  " if r["ok"] else "FAIL", name, len(r["tests"])))
            for e in r["errors"]:
                print("     error:", e)
            for w in r["warnings"]:
                print("     warning:", w)
            bad += not r["ok"]
        return 1 if bad else 0
    if act == "check" and not args.target:
        print("usage: pcbtestkit classes check <project> [--system S] [--target C]", file=sys.stderr)
        return 2
    project = args.target[0] if act == "check" else None
    d = None
    if project:
        from .project import Design
        d = Design.load(project, config_path=args.config)
        systems = d.class_systems()
    else:
        systems, warns = CS.load_systems(None, os.getcwd())
        for w in warns:
            print("! " + w, file=sys.stderr)
    if act == "list":
        rows = [s.summary() for s in systems.values()]
        if args.json:
            print(json.dumps(rows, indent=1))
            return 0
        for s in sorted(rows, key=lambda r: r["id"]):
            dims = "  x  ".join("%s: %s" % (dd["id"], " ".join(dd["levels"])) for dd in s["dimensions"])
            print("%-22s %-10s %-15s %s" % (s["id"], s["status"], s["kind"], s["name"]))
            print("%-22s %s" % ("", dims))
        return 0
    if act == "show":
        if not args.target:
            print("usage: pcbtestkit classes show <system> [--class C]", file=sys.stderr)
            return 2
        s = systems.get(args.target[0])
        if s is None:
            print("unknown system %s; known: %s" % (args.target[0], ", ".join(sorted(systems))), file=sys.stderr)
            return 2
        print(json.dumps(CS.describe(s, None, args.cls), indent=1 if args.json else 1, default=str))
        return 0
    if act == "check":
        if args.target_class:
            sid = (args.system or [None])[0]
            r = d.evaluate_class(sid, args.target_class)
            if args.json:
                print(json.dumps(r, indent=1, default=str))
                return 0
            tg = r["target"]
            viol = dict(tg["violations"])
            viol.update((r.get("process") or {}).get("violations") or {})
            print("%s %s: %s (board needs %s)" % (r["name"], tg["class"], "OK" if tg["ok"] else "VIOLATIONS",
                                                 r["required"].get("class") or "a finer process"))
            for m, v in viol.items():
                if m.startswith("_"):                       # e.g. a level's 'requires' (finish) not met
                    print("  %-8s %s" % (v.get("severity", ""), v["label"]))
                    continue
                print("  %-8s %-34s %5d  worst %s  limit %s" % (v.get("severity", ""), v["label"], v["count"], v.get("worst"),
                                                             v.get("limit") if v.get("limit") is not None else v.get("limits")))
                for i in (v.get("items") or [])[:3]:
                    print("             at (%s, %s) %s" % (i.get("x"), i.get("y"), i.get("label") or i.get("a") or i.get("text") or ""))
            for mm in tg.get("manual") or []:
                print("  order note: %s %s %s" % (mm["label"], mm["requirement"], mm["note"]))
            return 0 if tg["ok"] else 1
        rows = d.class_overview(args.system)
        if args.json:
            print(json.dumps(rows, indent=1))
            return 0
        print("%s: which class systems fit" % d.board.name)
        for r in rows:
            print("  %-22s %-18s %-4s %s" % (r["system"], r["class"] or "-", "yes" if r["fits"] else "NO", ", ".join(r["blockers"])))
        return 0
    print("unknown action %s" % act, file=sys.stderr)
    return 2


def cmd_mcp(args) -> int:
    from .mcp_server import serve
    serve(args.project, read_only=args.read_only, profile=args.profile)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The full CLI (also used by tools/gen_docs.py to write the CLI reference)."""
    ap = argparse.ArgumentParser(prog="pcbtestkit", description="Production test & assembly toolkit for KiCad (v%s). "
                                 "AI agents: run `pcbtestkit guide` first." % __version__)
    ap.add_argument("--version", action="version", version="pcbtestkit %s" % __version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("project", help=".kicad_pcb, .kicad_pro or project folder")
        p.add_argument("--config", help="alternative pcbtestkit.json")
        p.add_argument("--probe-side", choices=["auto", "bottom", "top"])
        p.add_argument("--fab", choices=["standard", "advanced", "conservative"])
        p.add_argument("--class-system", help="class system id (pcbtestkit classes list), default eurocircuits-2024")
        p.add_argument("--tech-class", help="class to order in that system, e.g. 6C, standard, 4-layer")
        p.add_argument("--check-class", action="append", metavar="SYSTEM[:CLASS]",
                       help="extra class check, repeatable, e.g. ipc-2221-voltage:B2")
        p.add_argument("--no-kicad", action="store_true", help="skip kicad-cli DRC/ERC")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("run", help="full export")
    common(p)
    p.add_argument("--out", help="output folder (default <project>/production_test)")
    p.add_argument("--panel", help="fixture sites, e.g. 2x2")
    p.add_argument("--outline", choices=["rect", "outline"])
    p.add_argument("--jig-parts", help="comma separated refs (default: all THT)")
    p.add_argument("--no-fixture", action="store_true")
    p.add_argument("--no-jig", action="store_true")
    p.add_argument("--no-runner", action="store_true")
    p.add_argument("--no-visuals", action="store_true", help="skip the KiCad 3D renders in the guide")
    p.add_argument("--video", action="store_true",
                   help="also render the explainer video: real 3D animation rendered with KiCad (needs ffmpeg; several minutes)")
    p.add_argument("--video-fps", type=float, help="frame rate of the video's motion shots (default 24)")
    p.add_argument("--video-stills", action="store_true", help="quick video from stills only, without the 3D animation")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("analyze", help="weak-point analysis only")
    common(p)
    p.add_argument("--strict", action="store_true", help="exit 1 on critical findings (CI)")
    p.set_defaults(fn=cmd_analyze)

    p = sub.add_parser("init", help="write a pcbtestkit.json template next to the project")
    common(p)
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("api", help="call one agent-API tool")
    p.add_argument("tool")
    p.add_argument("--project", required=False)
    p.add_argument("--args", help="JSON arguments")
    p.add_argument("--no-save", action="store_true", help="do not persist writes")
    p.add_argument("--actor", default="ai", help="name stored in the audit log")
    p.add_argument("--compact", action="store_true")
    p.set_defaults(fn=cmd_api)

    p = sub.add_parser("tools", help="list agent tools (--brief) or export their definitions")
    p.add_argument("--format", choices=["mcp", "anthropic", "openai"], default="mcp")
    p.add_argument("--read-only", action="store_true")
    p.add_argument("--profile", choices=["full", "core", "readonly"], default="full")
    p.add_argument("--brief", action="store_true", help="one line per tool: name, R/W, summary")
    p.add_argument("--name", help="print one tool's description and input schema")
    p.set_defaults(fn=cmd_tools)

    p = sub.add_parser("guide", help="print the agent quick reference (cheapest way for an AI to use this CLI)")
    p.set_defaults(fn=cmd_guide)

    p = sub.add_parser("classes", help="technology-class systems: list, show, check a board, validate, template")
    p.add_argument("action", choices=["list", "show", "check", "validate", "template", "catalog"])
    p.add_argument("target", nargs="*", help="system id (show), project (check) or JSON files (validate)")
    p.add_argument("--class", dest="cls", help="class to show (show)")
    p.add_argument("--system", action="append", help="limit check to these systems (check)")
    p.add_argument("--target", dest="target_class", help="check compliance with this class of one --system (check)")
    p.add_argument("--config", help="alternative pcbtestkit.json (check)")
    p.add_argument("--out", help="output file (template)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_classes)

    p = sub.add_parser("mcp", help="run the MCP stdio server")
    p.add_argument("--project", help="board or project to open at start (open_project can switch later)")
    p.add_argument("--read-only", action="store_true", help="refuse every write tool")
    p.add_argument("--profile", choices=["full", "core", "readonly"], default="full",
                   help="tools to expose: full (31), core (16, about half the tokens), readonly")
    p.set_defaults(fn=cmd_mcp)

    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except FileNotFoundError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
