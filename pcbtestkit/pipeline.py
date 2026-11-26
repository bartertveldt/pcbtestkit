"""End-to-end export: analysis -> spec -> test plan -> fixture -> jig -> report -> runner bundle."""
from __future__ import annotations

import glob
import json
import math
import os
import shutil
from typing import Any, Callable, Dict, List, Optional

from .analysis import apply_waivers, run_analysis, sort_findings
from .fixture import build_fixture, probe_points_csv, probes_csv, project_file
from .project import Design
from .spec import build_spec
from .testplan import build_testplan, testability_findings

HERE = os.path.dirname(os.path.abspath(__file__))


def _write_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)
        fh.write("\n")


def analyze(design: Design, include_kicad: Optional[bool] = None):
    """Findings including testability, plus the plan they were derived from."""
    findings = run_analysis(design, include_kicad=include_kicad)
    plan = build_testplan(design, findings)
    findings += testability_findings(design, plan)
    apply_waivers(design, findings)
    sort_findings(findings)
    return findings, plan


SITE_POWER_GPIOS = [17, 22, 23, 24, 25, 5, 6, 12, 13, 16, 19, 20, 21, 26, 4, 18]


def hardware_template(plan: Dict[str, Any], fixture: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    chans = [p["channel"] for p in plan["probes"] if p.get("channel")]
    nsite = int((plan.get("batch") or {}).get("count") or 1)
    if fixture and fixture.get("sites"):
        nsite = len(fixture["sites"])
    adc_n = max(1, int(math.ceil(len(chans) * nsite / 4.0)))
    hw: Dict[str, Any] = {
        "_comment": "Map test channels to your ADC hardware. Types: ads1115, mcp3008, serial (MCU firmware), simulated.",
        "adc": {"type": "ads1115", "i2c_bus": 1, "addresses": ["0x%02X" % (0x48 + i) for i in range(min(4, adc_n))],
                "fsr_v": 4.096},
        "channel_map": "auto",
        "channel_map_example": {ch: {"device": "0x%02X" % (0x48 + i // 4), "input": i % 4} for i, ch in enumerate(chans)},
        "power": {"type": "gpio", "pin": 17, "active_high": True},
        "current_sensor": {"type": "none", "_example": {"type": "ina219", "address": "0x40", "shunt_ohm": 0.1}},
        "resistance": {"type": "none", "_example": {"type": "source_resistor", "gpio": 27, "r_ref_ohm": 1000, "v_source": 3.3}},
        "adc_max_v": plan["adc"]["max_input_v"],
        "site": 1,
    }
    if nsite > 1:
        hw["_batch_comment"] = (
            "Batch fixture with %d sites (one board each; every board's supply and channels on the fixture's batch "
            "connector, or on its own header with fixture.batch_connector per_site). With channel_map 'auto', "
            "site k uses the ADC inputs right after site k-1's (%d per site, %d in all: %s). 'power' has one entry "
            "per site so each board is switched on only while it is tested; override a site in 'sites', e.g. "
            "{\"2\": {\"channel_map\": {...}, \"power\": {...}}}." % (
                nsite, len(chans), len(chans) * nsite,
                "more than 4 ADS1115 on one bus: use mcp3008 or a serial MCU" if adc_n > 4 else "%d x ADS1115" % adc_n))
        hw["power"] = [{"type": "gpio", "pin": SITE_POWER_GPIOS[k], "active_high": True} if k < len(SITE_POWER_GPIOS)
                       else {"type": "manual"} for k in range(nsite)]
        hw["sites"] = {}
    return hw


def run_pipeline(design: Design, out_dir: Optional[str] = None, outputs: Optional[Dict[str, Any]] = None,
                 progress: Optional[Callable[[str, float], None]] = None, include_kicad: Optional[bool] = None,
                 cancel: Optional[Any] = None) -> Dict[str, Any]:
    from .jig import build_jigs
    from .report import build_report

    def step(msg: str, frac: float):
        if progress:
            progress(msg, frac)

    want = dict(design.cfg.get("outputs") or {})
    want.update(outputs or {})
    out_dir = out_dir or os.path.join(design.project_dir, "production_test")
    os.makedirs(out_dir, exist_ok=True)
    name = design.board.name
    paths: Dict[str, str] = {}

    step("Analysing board and schematic", 0.05)
    findings, plan = analyze(design, include_kicad=include_kicad)
    step("Writing specification", 0.35)
    spec = build_spec(design, findings, plan)
    if want.get("spec", True):
        paths["spec"] = os.path.join(out_dir, "%s-spec.json" % name)
        _write_json(paths["spec"], spec)
    if want.get("testplan", True):
        paths["testplan"] = os.path.join(out_dir, "%s-testplan.json" % name)
        _write_json(paths["testplan"], plan)

    fixture = None
    # fixture.mode: generate = pogo-pin fixture board; own = the user designs it (probe-point list);
    # none = no fixture (manual test with the test scheme, no runner bundle)
    mode = (design.cfg.get("fixture") or {}).get("mode", "generate")
    if mode == "own":
        paths["probe_points"] = os.path.join(out_dir, "%s-probe-points.csv" % name)
        with open(paths["probe_points"], "w", encoding="utf-8") as fh:
            fh.write(probe_points_csv(plan))
    if want.get("fixture", True) and mode == "generate":
        step("Generating fixture board", 0.5)
        fixture = build_fixture(design, plan)
        from .fixture import write_pogo_models
        write_pogo_models(out_dir, plan)
        # one board per fixture plate: "<name>-fixture" (one side) or "<name>-fixture-top" / "-bottom" (both sides)
        plates = fixture.get("plates") or {"": fixture}
        metas: Dict[str, Any] = {}
        for side, fx in plates.items():
            base = "%s-fixture%s" % (name, "-" + side if side else "")
            key = "fixture" + ("_" + side if side else "")
            paths[key] = os.path.join(out_dir, base + ".kicad_pcb")
            with open(paths[key], "w", encoding="utf-8") as fh:
                fh.write(fx["kicad_pcb"])
            pro = os.path.join(out_dir, base + ".kicad_pro")
            if not os.path.exists(pro):            # never overwrite settings the user made in KiCad
                with open(pro, "w", encoding="utf-8") as fh:
                    fh.write(project_file(base))
            paths[key + "_project"] = pro
            paths[key + "_probes"] = os.path.join(out_dir, base + "-probes.csv")
            with open(paths[key + "_probes"], "w", encoding="utf-8") as fh:
                fh.write(probes_csv(plan, fx))
            meta = {k: v for k, v in fx.items() if k not in ("kicad_pcb", "plates")}
            try:
                from .kicad_cli import drc_file, find_kicad_cli
                cli = find_kicad_cli()
                if cli and (include_kicad is not False):
                    rep = drc_file(cli, paths[key])
                    if rep is not None:
                        kinds: Dict[str, int] = {}
                        for v in rep.get("violations", []):
                            kinds[v.get("type", "?")] = kinds.get(v.get("type", "?"), 0) + 1
                        meta["drc"] = {"violations": kinds, "unconnected": len(rep.get("unconnected_items", []))}
                        left = meta["drc"]["unconnected"]
                        fx["drc"] = "%d violation type(s) %s, %d unrouted connection(s)%s" % (
                            len(kinds), kinds, left, " (route them before ordering)" if left else "")
            except Exception:  # validation is best-effort
                pass
            metas[side] = meta
        if "plates" in fixture:
            first = (plan.get("plates") or ["bottom"])[0]
            for k in ("", "_project", "_probes"):
                paths["fixture" + k] = paths["fixture_%s%s" % (first, k)]
            fixture["drc"] = fixture["plates"][first].get("drc")
            meta = dict(metas[first], plates=metas)
        else:
            meta = metas[""]
        paths["fixture_meta"] = os.path.join(out_dir, "%s-fixture.json" % name)
        _write_json(paths["fixture_meta"], meta)

    jig = None
    if want.get("jig", True):
        step("Generating solder jig", 0.65)
        paths["jig_stl"] = os.path.join(out_dir, "%s-jig.stl" % name)
        paths["jig_scad"] = os.path.join(out_dir, "%s-jig.scad" % name)
        paths["jig_preview"] = os.path.join(out_dir, "%s-jig.png" % name)
        paths["jig_place_stl"] = os.path.join(out_dir, "%s-jig-placement.stl" % name)
        paths["jig_place_preview"] = os.path.join(out_dir, "%s-jig-placement.png" % name)
        jig = build_jigs(design, paths["jig_stl"], paths["jig_scad"], paths["jig_preview"],
                         paths["jig_place_stl"], paths["jig_place_preview"],
                         file_for=lambda tag, ext: os.path.join(out_dir, "%s-%s.%s" % (name, tag, ext)))
        if "placement" not in jig:                       # single-jig workflow
            for k in ("jig_place_stl", "jig_place_preview"):
                paths.pop(k, None)
        # the other passes' jigs: jig_<key>_stl / _scad / _preview (+ _place_stl / _place_preview)
        for key, ej in sorted((jig.get("extra") or {}).items()):
            for k, pk in (("stl", "stl"), ("scad", "scad"), ("preview_png", "preview")):
                if ej.get(k):
                    paths["jig_%s_%s" % (key, pk)] = ej[k]
            for k, pk in (("stl", "place_stl"), ("preview_png", "place_preview")):
                if (ej.get("placement") or {}).get(k):
                    paths["jig_%s_%s" % (key, pk)] = ej["placement"][k]
        paths["jig_meta"] = os.path.join(out_dir, "%s-jig.json" % name)

        def rel(d):
            # in the file, refer to the jig's own outputs by name (they sit next to it), never by absolute path
            return {k: (os.path.basename(v) if k in ("stl", "scad", "preview_png") and isinstance(v, str)
                        else rel(v) if isinstance(v, dict) else v) for k, v in d.items()}
        _write_json(paths["jig_meta"], rel(jig))

    # how long a batch takes to assemble, program and test (pcbtestkit.timing); kept in the test plan
    from .timing import production_timing
    plan["timing"] = production_timing(design, plan, jig)
    if "testplan" in paths:
        _write_json(paths["testplan"], plan)

    if want.get("runner", True) and mode != "none":
        step("Bundling test runner", 0.85)
        rdir = os.path.join(out_dir, "runner")
        src = os.path.join(HERE, "runner")
        if os.path.isdir(src):
            if os.path.isdir(rdir):
                shutil.rmtree(rdir)
            shutil.copytree(src, rdir, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            shutil.copy2(paths["testplan"], os.path.join(rdir, "testplan.json")) if "testplan" in paths else None
            # the test facility's own hardware.json and runner driver, else the Raspberry Pi template
            from .facility import facility_for, hardware_template as facility_hardware
            fac = facility_for(design)
            hw = facility_hardware(fac)
            if hw is None:
                hw = hardware_template(plan, fixture)
            elif (fac.get("runner") or {}).get("driver"):
                shutil.copy2(os.path.join(fac["_dir"], fac["runner"]["driver"]), os.path.join(rdir, "facility_driver.py"))
            _write_json(os.path.join(rdir, "hardware.json"), hw)
            paths["runner"] = rdir

    visuals: Dict[str, Any] = {}
    video: Optional[Dict[str, Any]] = None
    keep_frames = False
    if want.get("guide", True):
        vdir = os.path.join(out_dir, "visuals")
        kept_imgs = sorted(glob.glob(os.path.join(vdir, "*.png"))) if want.get("visuals") == "reuse" else []
        if kept_imgs:                           # the stills of the run just before (the video in the background)
            from .kicad_cli import find_kicad_cli
            visuals = {"images": {os.path.splitext(os.path.basename(p_))[0]: p_ for p_ in kept_imgs}, "log": [],
                       "available": bool(find_kicad_cli())}
        elif want.get("visuals", True):
            step("Rendering 3D views with KiCad (this takes a minute)", 0.88)
            from .visuals import build_visuals
            try:
                visuals = build_visuals(design, plan, fixture, jig, paths, vdir)
            except Exception as exc:  # renders are optional
                visuals = {"images": {}, "log": ["renders failed: %r" % exc]}
        if want.get("video"):
            step("Rendering the explainer video with KiCad (several minutes)", 0.9)
            from .video import build_video
            vp = os.path.join(out_dir, "%s-explainer.mp4" % name)

            def anim_progress(done: int, total: int) -> None:
                if done == total or done % max(1, total // 100) == 0:
                    step("Rendering video frame %d of %d" % (done, total), 0.9 + 0.04 * done / max(1, total))

            try:
                vres = build_video(design, spec, plan, fixture, jig, visuals, out_dir, vp,
                                   work_dir=os.path.join(vdir, "scenes", "video"), paths=paths, visuals_dir=vdir,
                                   progress=anim_progress, cancel=cancel)
            except Exception as exc:
                vres = {"ok": False, "reason": repr(exc)}
            if vres.get("ok"):
                paths["video"] = vp
                video = {"ok": True, "path": vp, "reused": vres.get("reused") or [], "encoder": vres.get("encoder")}
                stale = os.path.join(out_dir, "%s-video-error.txt" % name)
                if os.path.exists(stale):              # from an earlier failed try
                    os.remove(stale)
            else:
                why = vres.get("reason") or "unknown error"
                design.warnings.append("video not made: %s" % why)
                # keep the frames rendered so far and write the whole error, so the failure can be looked into
                keep_frames = True
                paths["video_error"] = os.path.join(out_dir, "%s-video-error.txt" % name)
                log = (vres.get("log") or "").replace(out_dir + os.sep, "")
                with open(paths["video_error"], "w", encoding="utf-8") as fh:
                    fh.write("PCB TestKit could not make the explainer video.\n\nReason: %s\n\nThe frames rendered so far "
                             "are kept in visuals/scenes/. Fix the cause and generate again with the same settings: the "
                             "frames are reused (only changed or missing ones are made again), so the next try is quick. "
                             "The folder is removed once the video is made.\n\n%s\n" % (why, log))
                video = {"ok": False, "reason": why, "error_file": paths["video_error"]}
        from .guide import build_guide
        paths["guide"] = os.path.join(out_dir, "%s-guide.html" % name)
        with open(paths["guide"], "w", encoding="utf-8") as fh:
            fh.write(build_guide(design, spec, plan, fixture, jig, visuals, paths))
        if not want.get("keep_scenes") and not keep_frames:
            from .visuals import clean_scenes
            clean_scenes(vdir)
        for w in (visuals.get("log") or []):
            design.warnings.append(w)

    if want.get("report", True):
        step("Writing report", 0.95)
        paths["report"] = os.path.join(out_dir, "%s-report.html" % name)
        with open(paths["report"], "w", encoding="utf-8") as fh:
            fh.write(build_report(design, spec, plan, jig, fixture, paths))
    step("Done", 1.0)
    return {"out_dir": out_dir, "paths": paths, "summary": spec["summary"], "coverage": plan["coverage"],
            "fixture_warnings": fixture["warnings"] if fixture else [],
            "fixture_routing": fixture.get("routing") if fixture else None,
            "jig": {k: jig[k] for k in ("jig_size_mm", "parts_to_place", "mesh", "workflow", "passes", "batch") if k in jig} if jig else None,
            "batch": plan.get("batch"),
            "video": video,
            "program": [{"header": h["ref"], "protocol": h["protocol"], "connector": h.get("connector"), "missing": h["missing"]}
                        for h in (plan.get("program") or {}).get("headers") or []],
            "warnings": design.warnings + plan["warnings"]}
