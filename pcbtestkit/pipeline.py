"""End-to-end export: analysis -> spec -> test plan -> fixture -> jig -> report -> runner bundle."""
from __future__ import annotations

import json
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


def hardware_template(plan: Dict[str, Any], fixture: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    chans = [p["channel"] for p in plan["probes"] if p.get("channel")]
    return {
        "_comment": "Map test channels to your ADC hardware. Types: ads1115, mcp3008, serial (MCU firmware), simulated.",
        "adc": {"type": "ads1115", "i2c_bus": 1, "addresses": ["0x48", "0x49", "0x4A", "0x4B"], "fsr_v": 4.096},
        "channel_map": "auto",
        "channel_map_example": {ch: {"device": "0x%02X" % (0x48 + i // 4), "input": i % 4} for i, ch in enumerate(chans)},
        "power": {"type": "gpio", "pin": 17, "active_high": True},
        "current_sensor": {"type": "none", "_example": {"type": "ina219", "address": "0x40", "shunt_ohm": 0.1}},
        "resistance": {"type": "none", "_example": {"type": "source_resistor", "gpio": 27, "r_ref_ohm": 1000, "v_source": 3.3}},
        "adc_max_v": plan["adc"]["max_input_v"],
        "site": 1,
    }


def run_pipeline(design: Design, out_dir: Optional[str] = None, outputs: Optional[Dict[str, bool]] = None,
                 progress: Optional[Callable[[str, float], None]] = None, include_kicad: Optional[bool] = None) -> Dict[str, Any]:
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
        paths["fixture"] = os.path.join(out_dir, "%s-fixture.kicad_pcb" % name)
        from .fixture import write_pogo_models
        write_pogo_models(out_dir, plan)
        with open(paths["fixture"], "w", encoding="utf-8") as fh:
            fh.write(fixture["kicad_pcb"])
        pro = os.path.join(out_dir, "%s-fixture.kicad_pro" % name)
        if not os.path.exists(pro):            # never overwrite settings the user made in KiCad
            with open(pro, "w", encoding="utf-8") as fh:
                fh.write(project_file("%s-fixture" % name))
        paths["fixture_project"] = pro
        paths["fixture_probes"] = os.path.join(out_dir, "%s-fixture-probes.csv" % name)
        with open(paths["fixture_probes"], "w", encoding="utf-8") as fh:
            fh.write(probes_csv(plan, fixture))
        meta = {k: v for k, v in fixture.items() if k != "kicad_pcb"}
        try:
            from .kicad_cli import drc_file, find_kicad_cli
            cli = find_kicad_cli()
            if cli and (include_kicad is not False):
                rep = drc_file(cli, paths["fixture"])
                if rep is not None:
                    kinds: Dict[str, int] = {}
                    for v in rep.get("violations", []):
                        kinds[v.get("type", "?")] = kinds.get(v.get("type", "?"), 0) + 1
                    meta["drc"] = {"violations": kinds, "unconnected": len(rep.get("unconnected_items", []))}
                    fixture["drc"] = "%d violation type(s) %s, %d unrouted connection(s) (route them before ordering)" % (
                        len(kinds), kinds, meta["drc"]["unconnected"])
        except Exception:  # validation is best-effort
            pass
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
                         paths["jig_place_stl"], paths["jig_place_preview"])
        if "placement" not in jig:                       # single-jig workflow
            for k in ("jig_place_stl", "jig_place_preview"):
                paths.pop(k, None)
        paths["jig_meta"] = os.path.join(out_dir, "%s-jig.json" % name)

        def rel(d):
            # in the file, refer to the jig's own outputs by name (they sit next to it), never by absolute path
            return {k: (os.path.basename(v) if k in ("stl", "scad", "preview_png") and isinstance(v, str)
                        else rel(v) if isinstance(v, dict) else v) for k, v in d.items()}
        _write_json(paths["jig_meta"], rel(jig))

    if want.get("runner", True) and mode != "none":
        step("Bundling test runner", 0.85)
        rdir = os.path.join(out_dir, "runner")
        src = os.path.join(HERE, "runner")
        if os.path.isdir(src):
            if os.path.isdir(rdir):
                shutil.rmtree(rdir)
            shutil.copytree(src, rdir, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            shutil.copy2(paths["testplan"], os.path.join(rdir, "testplan.json")) if "testplan" in paths else None
            _write_json(os.path.join(rdir, "hardware.json"), hardware_template(plan, fixture))
            paths["runner"] = rdir

    visuals: Dict[str, Any] = {}
    if want.get("guide", True):
        vdir = os.path.join(out_dir, "visuals")
        if want.get("visuals", True):
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
                if done == total or done % max(1, total // 25) == 0:
                    step("Rendering video frame %d of %d" % (done, total), 0.9 + 0.04 * done / max(1, total))

            try:
                vres = build_video(design, spec, plan, fixture, jig, visuals, out_dir, vp,
                                   work_dir=os.path.join(vdir, "scenes", "video"), paths=paths, visuals_dir=vdir,
                                   progress=anim_progress)
            except Exception as exc:
                vres = {"ok": False, "reason": repr(exc)}
            if vres.get("ok"):
                paths["video"] = vp
            else:
                design.warnings.append("video skipped: %s" % vres.get("reason"))
        from .guide import build_guide
        paths["guide"] = os.path.join(out_dir, "%s-guide.html" % name)
        with open(paths["guide"], "w", encoding="utf-8") as fh:
            fh.write(build_guide(design, spec, plan, fixture, jig, visuals, paths))
        if not want.get("keep_scenes"):
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
            "fixture_warnings": fixture["warnings"] if fixture else [], "jig": {k: jig[k] for k in ("jig_size_mm", "parts_to_place", "mesh", "workflow") if k in jig} if jig else None,
            "warnings": design.warnings + plan["warnings"]}
