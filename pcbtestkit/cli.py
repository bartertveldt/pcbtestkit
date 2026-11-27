"""Command line interface.

    python3 -m pcbtestkit run      <project> [--out DIR] [--no-fixture] ...   full export
    python3 -m pcbtestkit analyze  <project> [--json]                         weak points only
    python3 -m pcbtestkit init     <project>                                  write pcbtestkit.json template
    python3 -m pcbtestkit api      <tool> --project P [--args JSON]           one agent-API call (JSON out)
    python3 -m pcbtestkit tools    [--format mcp|anthropic|openai]            export tool schemas
    python3 -m pcbtestkit mcp      [--project P] [--read-only]                MCP stdio server
    python3 -m pcbtestkit classes  list | show S [--class C] | check <project> [--system S] [--target C]
                                   | validate [file.json ...] | template [--out F] | catalog
    python3 -m pcbtestkit facilities list | show ID | validate [file.json ...] | template [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from . import __version__


def _overrides(args) -> Dict[str, Any]:
    """The command line's settings as pcbtestkit.json overrides (not saved)."""
    overrides: Dict[str, Any] = {}
    if getattr(args, "probe_side", None):
        overrides.setdefault("testplan", {})["probe_side"] = args.probe_side
    if getattr(args, "panel", None):
        c, r = args.panel.lower().split("x")
        overrides.setdefault("fixture", {})["panel"] = {"cols": int(c), "rows": int(r)}
    env: Dict[str, Any] = {}
    if getattr(args, "batch", None):
        if str(args.batch).lower() == "auto":           # as many as the test environment's station allows
            env["batch"] = "auto"
        else:
            overrides.setdefault("batch", {})["count"] = int(args.batch)
            env["batch"] = int(args.batch)
    if getattr(args, "no_environment", False):
        env = {"path": "none"}
    elif getattr(args, "environment", None):
        env["path"] = os.path.abspath(args.environment)
    if getattr(args, "station", None) and env.get("path") != "none":
        env["station"] = args.station
    if env:
        overrides["environment"] = env
    if getattr(args, "facility", None):
        overrides["facility"] = args.facility
    if getattr(args, "program_connector", None):
        overrides.setdefault("program", {})["connector"] = args.program_connector
    if getattr(args, "outline", None):
        overrides.setdefault("fixture", {})["outline"] = args.outline
    if getattr(args, "no_route", False):
        overrides.setdefault("fixture", {})["route"] = False
    if getattr(args, "jig_parts", None):
        overrides.setdefault("jig", {})["parts"] = [p.strip() for p in args.jig_parts.split(",") if p.strip()]
    for opt, key in (("jig_ignore", "ignore"), ("jig_ignore_models", "ignore_models")):
        if getattr(args, opt, None):
            overrides.setdefault("jig", {})[key] = [p.strip() for p in getattr(args, opt).split(",") if p.strip()]
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
    if getattr(args, "video_quality", None):
        overrides.setdefault("video", {})["quality"] = args.video_quality
    if getattr(args, "image_quality", None):
        overrides.setdefault("visuals", {})["quality"] = args.image_quality
    if getattr(args, "video_stills", False):
        overrides.setdefault("video", {})["animate"] = False
    return overrides


def _design(args):
    from .project import Design
    overrides = _overrides(args)
    d = Design.load(args.project, config_path=getattr(args, "config", None), overrides=overrides or None)
    if getattr(args, "remember_environment", False):
        from . import settings
        from .environment import environment_setting
        envc = environment_setting(d.cfg)
        if envc and not envc.get("remembered"):
            settings.remember_environment(envc)
            print("remembered for every future build: %s" % envc["path"], file=sys.stderr)
    return d


def cmd_run(args) -> int:
    from .pipeline import run_pipeline
    d = _design(args)
    outputs = {"fixture": not args.no_fixture, "jig": not args.no_jig, "runner": not args.no_runner}
    if getattr(args, "no_jig_fit_files", False):
        outputs["jig_fit_check"] = False
    if args.no_visuals:
        outputs["visuals"] = False
    if args.video:
        outputs["video"] = not args.background

    def prog(msg, frac):
        if not args.quiet:
            print("[%3d%%] %s" % (int(frac * 100), msg), file=sys.stderr)
    res = run_pipeline(d, out_dir=args.out, outputs=outputs, progress=prog,
                       include_kicad=False if args.no_kicad else None)
    env = d.environment or {}
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
        vid = res.get("video") or {}
        if vid.get("ok") and vid.get("reused"):
            print("  video made from frames kept from the last run: %s" % ", ".join(vid["reused"]))
        if args.video and args.background:
            from .video_job import launch
            job = launch(d.board.path, res["out_dir"], overrides=_overrides(args) or None)
            print("  video: %s" % ("rendering in the background (pid %d%s; log %s)" % (
                job["pid"], ", progress window" if job["window"] else "", job["log"]) if job.get("ok") else
                "not started: %s" % job.get("reason")))
        if env.get("ok"):
            print("  test environment %s: %s, %d board%s per batch%s" % (
                env["environment"], env["station"], env["batch"], "" if env["batch"] == 1 else "s",
                " (as many as it allows: %s)" % env.get("limit") if env.get("auto") else ""))
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


def cmd_facilities(args) -> int:
    """Test facilities (the tester a fixture plugs into): list, show, validate, template."""
    from . import facility as F
    act = args.action
    if act == "template":
        txt = json.dumps(F.TEMPLATE, indent=1)
        if not args.out:
            print(txt)
            return 0
        os.makedirs(args.out, exist_ok=True)
        with open(os.path.join(args.out, "facility.json"), "w", encoding="utf-8") as fh:
            fh.write(txt + "\n")
        drv = os.path.join(F.BUILTIN_DIR, "bench-tester-example", "driver.py")
        if not os.path.exists(os.path.join(args.out, "driver.py")):
            with open(drv, encoding="utf-8") as src, open(os.path.join(args.out, "driver.py"), "w", encoding="utf-8") as dst:
                dst.write(src.read())
        print("wrote %s (facility.json + driver.py) - edit them, then: pcbtestkit facilities validate %s" % (
            args.out, os.path.join(args.out, "facility.json")))
        return 0
    if act == "validate":
        files = args.target or []
        if not files:
            found, warns = F.load_facilities()
            for w in warns:
                print("WARN", w)
            print("%d facilities OK: %s" % (len(found), ", ".join(sorted(found))))
            return 1 if warns else 0
        bad = 0
        for f in files:
            try:
                raw = F.read_facility(f)
            except (OSError, ValueError) as e:
                print("%s: cannot read (%s)" % (f, e))
                bad += 1
                continue
            r = F.validate(raw)
            print("%s: %s" % (f, "OK" if r["ok"] else "INVALID"))
            for e in r["errors"]:
                print("  ERROR", e)
            for w in r["warnings"]:
                print("  warn ", w)
            bad += 0 if r["ok"] else 1
        return 1 if bad else 0
    if act == "from-pcb":
        if not args.target:
            print("give the station's .kicad_pcb: pcbtestkit facilities from-pcb station.kicad_pcb", file=sys.stderr)
            return 2
        extra = {"analog": {"max_v": args.analog_max_v}} if args.analog_max_v else None
        try:
            fac, warns = F.facility_from_pcb(args.target[0], connector=args.connector, ident=args.id, extra=extra)
        except (OSError, ValueError) as e:
            print("error: %s" % e, file=sys.stderr)
            return 2
        txt = json.dumps(fac, indent=1)
        if args.out and args.out.lower().endswith(".json"):          # a file of its own
            os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(txt + "\n")
            print("wrote %s" % args.out)
        elif args.out:
            os.makedirs(args.out, exist_ok=True)
            with open(os.path.join(args.out, "facility.json"), "w", encoding="utf-8") as fh:
                fh.write(txt + "\n")
            print("wrote %s - install it with: cp -r %s ~/.pcbtestkit/facilities/%s" % (
                os.path.join(args.out, "facility.json"), args.out, fac["id"]))
        else:
            print(txt)
        for w in warns:
            print("WARN", w, file=sys.stderr)
        return 0
    project_dir = None
    found, warns = F.load_facilities(None, project_dir)
    if act == "list":
        rows = [F.summary(f) for _k, f in sorted(found.items())]
        if args.json:
            print(json.dumps(rows, indent=1))
        else:
            for r_, (k, f) in zip(rows, sorted(found.items())):
                print("%-24s %s%s" % (k, r_["name"], "" if f["_origin"] == "built-in" else "  [%s]" % f["_origin"]))
                bits = ["%s analog inputs" % r_["analog_inputs"] if r_["analog_inputs"] else "channels: testplan.max_channels"]
                if r_["buses"]:
                    bits.append("buses " + ", ".join("%s (%s)" % kv for kv in sorted(r_["buses"].items())))
                if r_["plate_mm"]:
                    bits.append("plate %g x %g mm" % tuple(r_["plate_mm"]))
                if r_["connectors"]:
                    bits.append("connector " + ", ".join(r_["connectors"]))
                print("%-24s %s" % ("", "; ".join(bits)))
        for w in warns:
            print("WARN", w, file=sys.stderr)
        return 0
    if act == "show":
        if not args.target or args.target[0] not in found:
            print("unknown facility; pcbtestkit facilities list", file=sys.stderr)
            return 2
        f = dict(found[args.target[0]])
        f.pop("_dir", None)
        print(json.dumps(f, indent=1))
        return 0
    return 2


def _batches(items) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for it in items or []:
        name, _, n = str(it).partition("=")
        if not n.strip().isdigit():
            raise SystemExit("--batch takes BOARD=N, e.g. --batch sample_sensor=2 (got %r)" % it)
        out[name.strip()] = int(n)
    return out


def cmd_station(args) -> int:
    """Several board types on one test station: plan (batch sizes) or run (every board's outputs + station page)."""
    from .station import plan_station, run_station
    extra: Dict[str, Any] = {}
    if args.analog_max_v:
        extra["analog"] = {"max_v": args.analog_max_v}
    if args.max_sites:
        extra["batch"] = {"max_sites": args.max_sites}

    def prog(msg, frac):
        if not args.quiet and not args.json:
            print("[%3d%%] %s" % (int(frac * 100), msg), file=sys.stderr)
    try:
        if args.action == "plan":
            st = plan_station(args.station, args.projects, _batches(args.batch), args.connector, extra or None, progress=prog)
            st.pop("_facility", None)
        else:
            video = args.video or "none"
            st = run_station(args.station, args.projects, args.out or "station_out", _batches(args.batch), args.connector,
                             extra or None, visuals=not args.no_visuals, board_videos=video in ("all", "boards"),
                             station_video=video in ("all", "station"), route=not args.no_route, progress=prog)
    except ValueError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(st, indent=1, default=str))
    else:
        cap = st["capacity"]
        print("\nstation %s: %s inputs, %s supplies, buses %s, plate %s mm" % (
            st["station"]["id"], cap.get("inputs", "-"), cap.get("supplies", "-"),
            ", ".join("%s x%d" % kv for kv in sorted((cap.get("buses") or {}).items())) or "-",
            " x ".join("%g" % v for v in cap.get("plate_mm") or []) or "-"))
        for b in st["boards"]:
            if b.get("status") == "ok":
                print("  %-24s %d per fixture  %s inputs of %s  limited by %s" % (
                    b["name"], b["batch"], b["inputs_used"], b.get("inputs_total") or "-", b.get("limit") or "-"))
            else:
                print("  %-24s %s: %s" % (b["name"], b.get("status", "?").upper(), b.get("reason", "")))
        if args.action == "run":
            print("\n  station page    %s" % os.path.join(args.out or "station_out", "station.html"))
            if (st.get("video") or {}).get("path"):
                print("  station video   %s" % os.path.join(args.out or "station_out", st["video"]["path"]))
            elif st.get("video"):
                print("  ! station video: %s" % st["video"].get("reason"))
        for w in st.get("warnings", []):
            print("  ! %s" % w)
    return 0 if all(b.get("status") == "ok" for b in st["boards"]) else 1


def _env_spec(args):
    """The environment spec: defaults, --spec, then the command-line settings."""
    from .environment import load_spec
    over: Dict[str, Any] = {}
    if getattr(args, "max_stations", None):
        over["max_stations"] = args.max_stations
    if getattr(args, "max_batch", None):
        over["max_batch"] = args.max_batch
    if getattr(args, "plate", None):
        w, _x, h = args.plate.lower().partition("x")
        over["plate_mm"] = [float(w), float(h)]
    if getattr(args, "name", None):
        over["name"] = args.name
    if getattr(args, "image_quality", None):
        over["visuals"] = {"quality": args.image_quality}
    if getattr(args, "video_quality", None):
        over["video_quality"] = args.video_quality
    if getattr(args, "layout", None):
        over["layout"] = args.layout
    if getattr(args, "priorities", None):
        pr: Dict[str, float] = {}
        import csv
        with open(args.priorities, encoding="utf-8") as fh:
            for row in csv.reader(fh):
                if len(row) >= 2 and row[0].strip() and not row[0].startswith("#"):
                    try:
                        pr[row[0].strip()] = float(row[1])
                    except ValueError:
                        continue                    # a header line
        over["priorities"] = pr
    return load_spec(getattr(args, "spec", None), over)


def cmd_environment(args) -> int:
    """A test environment for a whole collection of boards: init (the editable spec), plan, build."""
    from . import environment as E
    if args.action == "stay-on":
        from . import power as PW
        out = args.out or (args.boards[0] if args.boards else "")
        if not out or not os.path.isdir(out):
            print("give the environment folder: pcbtestkit environment stay-on FOLDER", file=sys.stderr)
            return 2
        st = PW.stay_on(out)
        print("the computer stays on after the run in %s%s" % (out, " (it was %s)" % st["state"] if st.get("state") else ""))
        return 0
    try:
        power_cfg = _power_args(args)
    except ValueError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    if args.action in ("use", "forget", "show"):
        from . import settings
        if args.action == "use":
            if not args.boards:
                print("give the test environment: pcbtestkit environment use FOLDER (or a station board)", file=sys.stderr)
                return 2
            target = args.boards[0]
            if os.path.isdir(target) and not os.path.exists(os.path.join(target, "environment.json")):
                print("error: %s has no environment.json (made by pcbtestkit environment build)" % target, file=sys.stderr)
                return 2
            settings.remember_environment({"path": target, "station": args.use_station or "auto",
                                           "batch": args.use_batch or "auto"})
        elif args.action == "forget":
            settings.remember_environment(None)
        rem = settings.remembered_environment()
        if args.json:
            print(json.dumps(rem or {}, indent=1))
        elif rem:
            print("every build is made for the test environment %s (station %s, batch %s)\n  settings: %s\n"
                  "  a board's own pcbtestkit.json can choose another, or \"environment\": {\"path\": \"none\"}" % (
                      rem["path"], rem.get("station", "auto"), rem.get("batch", "auto"), settings.path()))
        else:
            print("no test environment is remembered (pcbtestkit environment use FOLDER)")
        return 0
    if args.action in ("constraints", "options", "upgrade") or (args.action == "add"):
        rc = _environment_folder_action(args, power_cfg)
        if rc is not None:
            return rc
    if args.action == "init":
        out = args.out or "environment-spec.json"
        if os.path.exists(out) and not args.force:
            print("error: %s exists (add --force to overwrite)" % out, file=sys.stderr)
            return 2
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(E.default_spec(), fh, indent=2)
            fh.write("\n")
        print("wrote %s - edit it (plate, sockets, limits, max_stations, priorities), then:\n"
              "  pcbtestkit environment plan BOARDS... --spec %s" % (out, out))
        return 0
    if not args.boards and not args.environment:
        print("give the boards: project folders, .kicad_pro/.kicad_pcb files or @list.txt", file=sys.stderr)
        return 2
    try:
        spec = _env_spec(args)
    except (OSError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2

    if args.background:                     # the same work as a background job (pcbtestkit.environment_job)
        if args.action == "build" and not args.out:
            print("error: build needs --out FOLDER", file=sys.stderr)
            return 2
        from .environment_job import launch
        out = args.out or "environment_plan"
        req = {"action": args.action, "boards": [os.path.abspath(b) if not b.startswith("@") else "@" + os.path.abspath(b[1:])
                                                 for b in args.boards],
               "out": out, "spec": spec, "spec_file": None,
               "environment": os.path.abspath(args.environment) if args.environment else None,
               "stations": [os.path.abspath(x) for x in args.station], "new_stations": not args.no_new_stations,
               "jobs": args.jobs, "visuals": not args.no_visuals, "route": not args.no_route, "video": args.video,
               "video_boards": args.video_boards, "resume": not args.no_resume, "power": power_cfg}
        res = launch(req, window=args.window)
        if not res.get("ok"):
            print("error: %s" % res.get("reason"), file=sys.stderr)
            return 2
        print("running in the background (pid %d)%s\n  progress: %s\n  status:   %s\n  when done: %s" % (
            res["pid"], " with a progress window" if res.get("window") else "", res["log"],
            os.path.join(os.path.abspath(out), "environment-job.json"), os.path.join(os.path.abspath(out), "index.html")))
        if res.get("power"):
            print("  power:    this %s%s" % (res["power"], "" if power_cfg["after"] == "none" else
                                            "\n            keep it on: pcbtestkit environment stay-on %s" % out))
        if res.get("power_warning"):
            print("warning: %s" % res["power_warning"], file=sys.stderr)
        return 0

    def prog(msg, frac):
        if not args.quiet and not args.json:
            print("[%3d%%] %s" % (int(frac * 100), msg), file=sys.stderr, flush=True)
    from . import power as PW
    if args.action == "build" and not args.out:
        print("error: build needs --out FOLDER (the environment is made there)", file=sys.stderr)
        return 2
    ses = None
    if PW.active(power_cfg):
        ses = PW.Session(power_cfg, args.out, hint="Ctrl+C keeps it on",
                         log=None if args.quiet and power_cfg["after"] == "none" else
                         (lambda m: print("power: %s" % m, file=sys.stderr, flush=True))).start()
    try:
        env = _environment_run(args, spec, prog)
    except ValueError as e:
        print("error: %s" % e, file=sys.stderr)
        if ses:
            ses.finish("failed", str(e))
        return 2
    except KeyboardInterrupt:
        if ses:
            ses.finish("cancelled")
        raise
    except Exception as e:  # noqa: BLE001 - a failed run still sleeps or shuts down when asked, then the error shows
        if ses:
            ses.finish("failed", repr(e))
        raise
    if args.json:
        print(json.dumps(E.shareable(env), indent=1, default=str))
    else:
        print(environment_text(env, args.top))
        if args.action == "build" or args.out:
            print("\n  environment page  %s" % os.path.join(args.out, "index.html"))
            print("  requirements      %s" % os.path.join(args.out, "requirements.md"))
    sys.stdout.flush()
    if ses:                                 # the results are on screen first, then the computer sleeps / shuts down
        page = os.path.join(args.out, "index.html") if args.out else ""
        ses.finish("done" if not page or os.path.exists(page) else "failed")
    s_ = env["summary"]
    if s_["compatible"] == 0:
        return 1
    return 1 if args.strict and s_["compatible"] < s_["boards"] else 0


def _upgrade_changes(args, env: Dict[str, Any]) -> Dict[str, Any]:
    """upgrade: the changes the options ask for and what they gain and cost (upgrades.plan_goal)."""
    from . import upgrades as U
    changes: List[Dict[str, Any]] = []
    if args.option or args.path:
        opt = U.options(env)
        ids = {o["id"]: o for o in opt["options"]}
        for oid in args.option:
            o = ids.get(oid.upper())
            if o is None:
                raise ValueError("no option %s (there are %s)" % (oid, ", ".join(ids) or "none"))
            changes.append(o["change"])
        if args.path:
            steps = (opt["paths"].get(args.path) or {}).get("steps") or []
            changes += [st_["change"] for st_ in steps[:args.steps or len(steps)]]
    for text in args.add_changes:
        changes += U.parse_changes(text)
    fit = [x.strip() for x in (args.fit or "").split(",") if x.strip()]
    batch = {}
    for item in args.batch_goal:
        name, _eq, n = item.partition("=")
        if not n.strip().isdigit():
            raise ValueError("--batch %s: write board=N" % item)
        batch[name.strip()] = int(n)
    for ch in changes:                      # an option's new station comes designed already
        if ch.get("kind") == "station" and "resources" in ch:
            ch["res"] = ch.pop("resources")
    return U.plan_goal(env, fit=fit, batch=batch, budget=args.budget, changes=changes)


def _goal_text(g: Dict[str, Any]) -> str:
    cur = g["currency"]
    L = ["", "changes:"] + ["  - %s" % c["text"] for c in g["changes"]] if g["changes"] else ["", "no change is needed"]
    gain = g["gain"]
    L.append("gain: %+d boards that fit, %+d boards per load%s" % (
        gain["boards_fit"], gain["slots"], (" (fits %s)" % ", ".join(gain["fit"][:8])) if gain["fit"] else ""))
    if gain["batches_up"]:
        L.append("  bigger batches: " + ", ".join("%s %d->%d" % (x["board"], x["from"], x["to"]) for x in gain["batches_up"][:10])
                 + (" and %d more" % (len(gain["batches_up"]) - 10) if len(gain["batches_up"]) > 10 else ""))
    L.append("cost: %s %s (new parts, programmers, a new fixture per board whose batch grows); worth %s %s; net %s %s" % (
        cur, "{:,.0f}".format(gain["cost"]), cur, "{:,.0f}".format(gain["worth"]), cur, "{:,.0f}".format(gain["net"])))
    L.append("then: %d of %d boards fit, %d per load" % (g["after"]["boards_fit"], g["after"]["boards"], g["after"]["slots"]))
    for c in g["cannot"]:
        L.append("cannot: %s at batch %d: %s" % (c["board"], c["batch"], c["reason"]))
    for u in g["unknown"]:
        L.append("unknown board: %s" % u)
    for f in g["flags"]:
        L.append("note: %s" % f)
    return "\n".join(L)


def _environment_folder_action(args, power_cfg) -> Optional[int]:
    """add, upgrade, constraints, options: actions on a built environment's folder (the first BOARDS argument)."""
    from . import environment as E
    from . import upgrades as U
    if not args.boards:
        print("give the environment folder: pcbtestkit environment %s DIR%s" % (
            args.action, " BOARDS..." if args.action == "add" else ""), file=sys.stderr)
        return 2
    env_dir = args.boards[0]
    try:
        env = E._env_json(env_dir)
    except ValueError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    if args.action == "constraints":
        cons = U.constraints(env)
        if args.board:
            cons = [c for c in cons if args.board.lower() in (c["board"].lower(), c["folder"].lower())]
            if not cons:
                print("error: no board %s in %s" % (args.board, env_dir), file=sys.stderr)
                return 2
        if args.json:
            print(json.dumps(cons, indent=1))
        else:
            print(U.constraints_md(env, cons))
        return 0
    if args.action == "options":
        opt = U.options(env)
        print(json.dumps(opt, indent=1) if args.json else U.upgrades_md(env, opt))
        return 0
    common = dict(jobs=args.jobs, visuals=not args.no_visuals, route=not args.no_route, video=args.video,
                  video_boards=args.video_boards, resume=not args.no_resume)
    over: Dict[str, Any] = {}
    if args.image_quality:
        over["visuals"] = {"quality": args.image_quality}
    if args.video_quality:
        over["video_quality"] = args.video_quality
    if args.action == "upgrade":
        try:
            g = _upgrade_changes(args, env)
        except ValueError as e:
            print("error: %s" % e, file=sys.stderr)
            return 2
        if not args.apply:
            print(json.dumps(g, indent=1, default=str) if args.json else _goal_text(g) +
                  "\n\nadd --apply to make it (the stations keep every pin they have: the fixtures you have still plug in)")
            return 0
        if not g["changes"]:
            print("nothing to change")
            return 0
        changes = [{k: v for k, v in c.items() if k != "text"} for c in g["changes"]]
    from . import power as PW
    if args.background:
        from .environment_job import launch
        req = {"action": args.action, "env_dir": os.path.abspath(env_dir), "out": os.path.abspath(env_dir),
               "boards": [os.path.abspath(b) for b in args.boards[1:]], "changes": changes if args.action == "upgrade" else None,
               "spec_over": over, "new_stations": not args.add_no_new, "power": power_cfg, **common}
        res = launch(req, window=args.window)
        if not res.get("ok"):
            print("error: %s" % res.get("reason"), file=sys.stderr)
            return 2
        print("running in the background (pid %d)\n  progress: %s" % (res["pid"], res["log"]))
        if res.get("power"):
            print("  power:    this %s" % res["power"])
        return 0

    def prog(msg, frac):
        if not args.quiet and not args.json:
            print("[%3d%%] %s" % (int(frac * 100), msg), file=sys.stderr, flush=True)
    ses = PW.Session(power_cfg, env_dir, hint="Ctrl+C keeps it on").start() if PW.active(power_cfg) else None
    try:
        if args.action == "add":
            if len(args.boards) < 2:
                print("give the new boards: pcbtestkit environment add DIR BOARDS...", file=sys.stderr)
                return 2
            out = E.add_boards(env_dir, args.boards[1:], over, new_stations=not args.add_no_new, progress=prog, **common)
        else:
            out = E.upgrade_environment(env_dir, changes, over, progress=prog, **common)
    except ValueError as e:
        print("error: %s" % e, file=sys.stderr)
        if ses:
            ses.finish("failed", str(e))
        return 2
    except KeyboardInterrupt:
        if ses:
            ses.finish("cancelled")
        raise
    except Exception as e:  # noqa: BLE001
        if ses:
            ses.finish("failed", repr(e))
        raise
    print(json.dumps(E.shareable(out), indent=1, default=str) if args.json else environment_text(out, args.top))
    if not args.json:
        if out.get("added"):
            print("\n  added: %s" % ", ".join(out["added"][:20]) + (" and %d more" % (len(out["added"]) - 20)
                                                                  if len(out["added"]) > 20 else ""))
        for u in out.get("upgraded") or []:
            print("  upgraded: %s" % u["text"])
        print("\n  environment page  %s" % os.path.join(env_dir, "index.html"))
    sys.stdout.flush()
    if ses:
        ses.finish("done")
    return 0


def _power_args(args) -> Dict[str, Any]:
    """environment: the power setting from --when-done, --no-keep-awake, --lid and --grace."""
    from . import power as PW
    return PW.normalize({"after": args.when_done or "none", "keep_awake": not args.no_keep_awake, "lid": args.lid,
                         "grace_s": args.grace})


def _environment_run(args, spec, prog) -> Dict[str, Any]:
    """Plan or build in this process."""
    from . import environment as E
    if args.action == "plan":
        env = E.plan_environment(args.boards, spec, args.environment, args.station, not args.no_new_stations,
                                 args.jobs, args.out, progress=prog)
        env.pop("_facilities", None)
        if args.out:
            E.write_docs(env, args.out)
        tmp = env.pop("_tmp", None)
        if tmp:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        return env
    return E.build_environment(args.boards, args.out, spec, args.environment, args.station, not args.no_new_stations,
                               args.jobs, visuals=not args.no_visuals, route=not args.no_route, video=args.video,
                               video_boards=args.video_boards, resume=not args.no_resume, progress=prog)


def environment_text(env: Dict[str, Any], top: int = 25) -> str:
    """The environment for the terminal: summary, every station's needs, the ranking, the boards that do not fit."""
    s = env["summary"]
    L = ["", "%s: %d of %d boards fit (%s%%) on %d station(s), %d boards per full load; %d do not fit, %d unreadable" % (
        env.get("name") or "environment", s["compatible"], s["boards"], s["compatible_pct"], s["stations"], s["board_slots"],
        s["incompatible"], s["errors"])]
    if env.get("layouts"):
        L.append("  layout %s (of %d compared, by worth minus cost): %s" % (
            env.get("layout"), len(env["layouts"]), "; ".join("%s%s net %s" % (x["name"], "*" if x["chosen"] else "",
                                                                             "{:,.0f}".format(x["net"]))
                                                           for x in env["layouts"])))
    for st in env["stations"]:
        rq, r = st["requirements"], st["resources"]
        bus = ", ".join("%s x%d" % ("ETH" if k == "ethernet" else k.upper(), v) for k, v in sorted((r.get("buses") or {}).items()) if v)
        L.append("")
        L.append("  %s%s: plate %g x %g mm, %d socket(s) / %d pins, %d board types (%d boards per load)" % (
            st["name"], " (existing)" if st["fixed"] else "", st["plate_mm"][0], st["plate_mm"][1], st["sockets"], st["pins"],
            rq["boards"], rq["board_slots"]))
        L.append("    %d analog inputs (0..%g V), %d switched supplies%s%s, %s" % (
            r.get("ain", 0), rq["analog_inputs"]["range_v"][1], r.get("vout", 0),
            " %s V" % "/".join("%g" % v for v in rq["supplies"]["voltages_v"]) if rq["supplies"]["voltages_v"] else "",
            ", up to %g A" % rq["supplies"]["max_current_a"] if rq["supplies"]["max_current_a"] else "", bus or "no buses"))
        if rq["programming"]:
            L.append("    programmers: %s" % ", ".join("%d x %s" % (pg["programmers"], pg["what"]) for _p, pg in
                                                    sorted(rq["programming"].items())))
    L.append("")
    L.append("  %-4s %-28s %-9s %-11s %5s  %-26s %s" % ("#", "board", "priority", "station", "batch", "limited by", "needs per board"))
    shown = [r for r in env["ranking"] if r["status"] == "compatible"][:top]
    for r in shown:
        needs = "%s ch" % r.get("channels")
        if r.get("supply_v") is not None:
            needs += ", %g V" % r["supply_v"]
        if r["buses"]:
            needs += ", " + " ".join(x.upper() for x in r["buses"])
        L.append("  %-4d %-28s %-9g %-11s %5d  %-26s %s" % (r["rank"], r["name"][:28], r["priority"], r.get("station") or "-",
                                                         r["batch"], (r.get("limit") or "-")[:26], needs))
    more = sum(1 for r in env["ranking"] if r["status"] == "compatible") - len(shown)
    if more > 0:
        L.append("  ... %d more that fit (ranking.csv / --json)" % more)
    bad = [r for r in env["ranking"] if r["status"] != "compatible"]
    if bad:
        L.append("")
        L.append("  Do not fit:")
        for r in bad:
            L.append("  %-4d %-28s %s: %s" % (r["rank"], r["name"][:28], r["status"].upper(), r.get("reason") or ""))
    for w in env.get("warnings") or []:
        L.append("  ! %s" % w)
    return "\n".join(L)


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
    p.add_argument("--batch", help="boards per batch: one fixture site per board, jigs that hold them all; with a test "
                                   "environment also 'auto' (default there: as many as its station allows)")
    p.add_argument("--environment", help="make the fixture for an existing test environment: a folder an environment "
                                         "build made, or a station board (.kicad_pcb / facility.json)")
    p.add_argument("--station", help="with --environment: which station (default: the one that takes the most boards)")
    p.add_argument("--remember-environment", action="store_true",
                   help="remember --environment for every future build (pcbtestkit environment use does the same)")
    p.add_argument("--no-environment", action="store_true", help="ignore the remembered test environment for this run")
    p.add_argument("--facility", help="test facility the fixture plugs into (pcbtestkit facilities list), default raspberry-pi")
    p.add_argument("--program-connector", choices=["same", "swd10", "jtag20", "avr_isp6", "pic_icsp6", "pins"],
                   help="programming connector per board on the fixture (default: a copy of the board's header)")
    p.add_argument("--outline", choices=["rect", "outline"])
    p.add_argument("--jig-parts", help="comma separated refs (default: all THT)")
    p.add_argument("--jig-ignore", help="parts the jigs leave out completely (no pocket, no 3D model, no stand-in): comma "
                                        "separated refs, values or footprint names, wildcards allowed, e.g. ENC1,*Enclosure*")
    p.add_argument("--jig-ignore-models", help="3D model files the jigs leave out, comma separated, e.g. '*case*.step'")
    p.add_argument("--no-fixture", action="store_true")
    p.add_argument("--no-route", action="store_true", help="leave the fixture board unrouted (ratsnest only)")
    p.add_argument("--no-jig", action="store_true")
    p.add_argument("--no-jig-fit-files", action="store_true",
                   help="leave out jig-fit-check/ (the board and its jigs in place, in KiCad's 3D coordinates; the fit "
                        "check itself always runs)")
    p.add_argument("--no-runner", action="store_true")
    p.add_argument("--no-visuals", action="store_true", help="skip the KiCad 3D renders in the guide")
    p.add_argument("--video", action="store_true",
                   help="also render the explainer video: real 3D animation rendered with KiCad (needs ffmpeg; several minutes)")
    p.add_argument("--video-fps", type=float, help="frame rate of the video's motion shots (default 24)")
    p.add_argument("--video-stills", action="store_true", help="quick video from stills only, without the 3D animation")
    p.add_argument("--video-quality", choices=["basic", "standard", "best", "high"],
                   help="ray tracing of the motion shots (default standard: shadows and ambient occlusion)")
    p.add_argument("--image-quality", choices=["basic", "standard", "best", "high"],
                   help="ray tracing of the guide's 3D images (default best: shadows, anti-aliasing; high = KiCad's "
                        "own, about 6x slower)")
    p.add_argument("--background", action="store_true",
                   help="with --video: make everything else now and render the video in a background job (progress window)")
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

    p = sub.add_parser("facilities", help="test facilities (your tester: connector, buses, driver): list, show, validate, template")
    p.add_argument("action", choices=["list", "show", "validate", "template", "from-pcb"])
    p.add_argument("target", nargs="*", help="facility id (show), facility.json files (validate) or the station's "
                                             ".kicad_pcb (from-pcb)")
    p.add_argument("--out", help="folder to write facility.json (and driver.py for template) to; from-pcb also takes a .json file")
    p.add_argument("--connector", help="from-pcb: reference of the station's interface connector (default: found from its nets)")
    p.add_argument("--id", help="from-pcb: facility id (default: from the board's file name)")
    p.add_argument("--analog-max-v", type=float, help="from-pcb: full-scale voltage of the station's analog inputs")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_facilities)

    p = sub.add_parser("station", help="several board types on one test station: plan the batch of each, or run them all")
    p.add_argument("action", choices=["plan", "run"])
    p.add_argument("station", help="the station: its .kicad_pcb (interface connector nets AIN1.., VOUT1.., CAN1_H, ...), "
                                   "a facility.json or an installed facility id")
    p.add_argument("projects", nargs="+", help="the boards: .kicad_pcb, .kicad_pro or project folders")
    p.add_argument("--out", help="run: output folder (default station_out): <board>/ per board, station.html, station.json")
    p.add_argument("--batch", action="append", metavar="BOARD=N", help="boards per fixture for one board type (default: "
                   "as many as the station's inputs, supplies, buses and plate allow); repeatable")
    p.add_argument("--connector", help="reference of the station's interface connector (default: found from its nets)")
    p.add_argument("--analog-max-v", type=float, help="full-scale voltage of the station's analog inputs")
    p.add_argument("--max-sites", type=int, help="at most this many boards per fixture")
    p.add_argument("--no-visuals", action="store_true", help="run: skip the KiCad 3D renders in the guides")
    p.add_argument("--no-route", action="store_true", help="run: leave the fixture boards unrouted (faster)")
    p.add_argument("--video", nargs="?", const="all", choices=["all", "station", "boards"],
                   help="run: render videos (KiCad 3D animation; minutes per video): all = the station video and every "
                        "board's explainer, station = only the station video, boards = only the boards'")
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_station)

    p = sub.add_parser("environment", help="design 1-3 test stations for a whole collection of boards (100+) and make "
                                          "every station, fixture, guide and video: init, plan, build")
    p.add_argument("action", choices=["init", "plan", "build", "add", "upgrade", "constraints", "options", "use", "forget",
                                      "show", "stay-on"],
                   help="init: write the spec; plan / build: design the stations for the boards; add DIR BOARDS...: new "
                        "boards for a built environment (its stations stay, their videos get the new boards); upgrade "
                        "DIR: what a change gains and costs, --apply makes it; constraints DIR: what limits every board; "
                        "options DIR: the upgrade options; use DIR: remember an environment for every future build; "
                        "forget; show; stay-on DIR: keep the computer on after the run there")
    p.add_argument("boards", nargs="*", help="project folders (searched recursively), .kicad_pro / .kicad_pcb files or "
                                             "@list.txt (one path per line); use: the environment folder")
    p.add_argument("--use-station", help="use: the station every board goes on (default auto: the one that takes the most)")
    p.add_argument("--use-batch", help="use: boards per batch (default auto: as many as the station allows)")
    p.add_argument("--out", help="init: the spec file (default environment-spec.json); build: the environment folder "
                                 "(stations, every board's outputs, index.html, requirements.md); plan: also write the "
                                 "plan's documents there")
    p.add_argument("--spec", help="environment spec (JSON; pcbtestkit environment init writes one to edit)")
    p.add_argument("--environment", help="an environment folder an earlier build made: keep its stations (edited in KiCad "
                                         "or not) and only design new ones for the boards they cannot take")
    p.add_argument("--station", action="append", default=[], help="a station of your own: its .kicad_pcb, a facility.json "
                                                                  "or a facility id (repeatable); kept as it is")
    p.add_argument("--no-new-stations", action="store_true", help="only the given stations, design none")
    p.add_argument("--max-stations", type=int, help="at most this many stations (spec max_stations, default 3)")
    p.add_argument("--max-batch", type=int, help="at most this many boards of one type per fixture (default 12)")
    p.add_argument("--plate", help="station plate W x H in mm, e.g. 300x220")
    p.add_argument("--name", help="the environment's name")
    p.add_argument("--priorities", help="CSV of board,weight: how much it matters that a board fits (default 1)")
    p.add_argument("--layout", help="how the stations are laid out: auto (default: the one worth the most for what it "
                                    "costs), general (one main station, more for what it cannot take), specialized-N "
                                    "(N stations for groups of similar boards) or split-<bus> (e.g. split-ethernet)")
    p.add_argument("--jobs", type=int, default=0, help="boards worked on at the same time (default: cores - 1, at most 8)")
    p.add_argument("--video", choices=["none", "swap", "all"], default="swap",
                   help="build: swap = each station's video of its fixtures and boards (default), all = also every "
                        "board type's explainer (KiCad 3D animation, minutes each), none")
    p.add_argument("--image-quality", choices=["basic", "standard", "best", "high"],
                   help="ray tracing of the guides' 3D images (default best; high is about 6x slower)")
    p.add_argument("--video-quality", choices=["basic", "standard", "best", "high"],
                   help="ray tracing of the videos (station videos default basic, board explainers standard)")
    p.add_argument("--video-boards", type=int, default=8, help="build/add: board types in each station's video: the "
                                                                 "first build shows N, an add appends up to N of the new "
                                                                 "boards (default 8; about a minute of rendering each)")
    p.add_argument("--no-visuals", action="store_true", help="build: skip the KiCad 3D renders in the guides")
    p.add_argument("--no-route", action="store_true", help="build: leave the fixture boards unrouted (faster)")
    p.add_argument("--no-resume", action="store_true", help="build: make every board's outputs again, even if up to date")
    p.add_argument("--top", type=int, default=25, help="plan/build: rows of the ranking to print")
    p.add_argument("--strict", action="store_true", help="exit 1 when any board does not fit")
    p.add_argument("--background", action="store_true", help="run as a background job and return at once (progress "
                                                              "in <out>/environment-job.log and .json)")
    p.add_argument("--window", action="store_true", help="with --background: show the progress window (KiCad's Python)")
    p.add_argument("--when-done", choices=["none", "sleep", "hibernate", "shutdown"],
                   help="plan/build: once the run has finished or failed (not when cancelled), put the computer to "
                        "sleep, hibernate it or shut it down, after --grace seconds (stay-on FOLDER keeps it on)")
    p.add_argument("--grace", type=int, default=120, help="seconds between the end of the run and --when-done "
                                                         "(default 120)")
    p.add_argument("--no-keep-awake", action="store_true", help="plan/build: let the computer sleep while it runs "
                                                               "(by default it is kept awake)")
    p.add_argument("--lid", action="store_true", help="plan/build: keep running with the lid closed (macOS asks for the "
                                                     "administrator password once; Windows sets the lid action to do "
                                                     "nothing until the run ends)")
    p.add_argument("--option", action="append", default=[], help="upgrade: an option id from upgrades.md (U1, ...), "
                                                                  "repeatable")
    p.add_argument("--path", choices=["value", "fit", "batch"], help="upgrade: the changes of a path in upgrades.md "
                                                                     "(best value, most boards, biggest batches)")
    p.add_argument("--steps", type=int, help="upgrade --path: only its first N changes")
    p.add_argument("--add", dest="add_changes", action="append", default=[],
                   help="upgrade: changes of your own, e.g. \"station-1:can+1,ethernet+1,ain+8,supplies+2,plate=400x300\"; "
                        "\"supply_v=48\"; \"max_batch=16\"; \"new_station\" (repeatable, or ; between)")
    p.add_argument("--fit", help="upgrade: boards that must fit (comma separated): the cheapest changes that take them")
    p.add_argument("--batch", dest="batch_goal", action="append", default=[],
                   help="upgrade: a board and the batch it must have, e.g. sensor_04=8 (repeatable)")
    p.add_argument("--budget", type=float, help="upgrade --fit/--batch: at most this much (spec costs currency)")
    p.add_argument("--apply", action="store_true", help="upgrade: make the changes and build the environment again "
                                                       "(else it only shows what they gain and cost)")
    p.add_argument("--board", help="constraints: only this board")
    p.add_argument("--no-new-stations-for-new", dest="add_no_new", action="store_true",
                   help="add: put the new boards only on the stations there are")
    p.add_argument("--force", action="store_true", help="init: overwrite the spec file")
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_environment)

    p = sub.add_parser("mcp", help="run the MCP stdio server")
    p.add_argument("--project", help="board or project to open at start (open_project can switch later)")
    p.add_argument("--read-only", action="store_true", help="refuse every write tool")
    p.add_argument("--profile", choices=["full", "core", "readonly"], default="full",
                   help="tools to expose: full (all 40), core (16, about a third of the tokens), readonly")
    p.set_defaults(fn=cmd_mcp)

    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except FileNotFoundError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
