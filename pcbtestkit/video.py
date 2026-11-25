"""Board-specific explainer video (MP4): what to fix, how to assemble the board with the jig, how to test it.

Pipeline:
  1. motion shots (pcbtestkit.animate): real 3D animation rendered frame by frame with kicad-cli, i.e. parts
     dropping onto the PCB, the jig being printed and loaded, the PCB sliding onto the jig, the fixture being
     built and the board pressed onto the pogo pins;
  2. storyboard (this module) mixing those shots with stills, a probe map and the simulated test run;
  3. titles and captions drawn onto the frames (pcbtestkit.video_frames, KiCad's Python with wxPython);
  4. MP4 (ffmpeg, x264) with cross-fades.
Requires kicad-cli, KiCad's Python and ffmpeg. When one is missing the video is skipped with a clear
message and everything else still works. Set video.animate = false for a quick stills-only video.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional

from . import classify as C
from .kicad_cli import find_kicad_cli

HERE = os.path.dirname(os.path.abspath(__file__))


def find_ffmpeg() -> Optional[str]:
    return os.environ.get("FFMPEG") or shutil.which("ffmpeg") or next(
        (p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg") if os.path.exists(p)), None)


def find_wx_python() -> Optional[str]:
    """A Python interpreter that can import wx: KiCad's bundled one, or the current one."""
    cands: List[str] = []
    cli = find_kicad_cli()
    if cli:
        app = os.path.dirname(os.path.dirname(cli))                      # .../KiCad.app/Contents
        cands += sorted(glob.glob(os.path.join(app, "Frameworks", "Python.framework", "Versions", "*", "bin", "python3")))
        cands += [os.path.join(os.path.dirname(cli), "python.exe"), os.path.join(os.path.dirname(cli), "python3")]
    cands += [sys.executable, "/usr/bin/python3"]
    for c in cands:
        if c and os.path.exists(c):
            try:
                r = subprocess.run([c, "-c", "import wx"], capture_output=True, timeout=60)
                if r.returncode == 0:
                    return c
            except (OSError, subprocess.SubprocessError):
                pass
    return None


def _runner_lines(out_dir: str, work: str) -> List[str]:
    runner = os.path.join(out_dir, "runner", "pcbtest_runner.py")
    if not os.path.exists(runner):
        return []
    try:
        r = subprocess.run([sys.executable if os.path.basename(sys.executable).startswith("python") else "python3",
                            runner, "test", "--simulate", "--non-interactive", "--serial", "DEMO-0001",
                            "--results", os.path.join(work, "demo_results")],
                           capture_output=True, text=True, timeout=120)
        txt = re.sub(r"\x1b\[[0-9;]*m", "", r.stdout)
        return [l.rstrip() for l in txt.splitlines() if l.strip()]
    except (OSError, subprocess.SubprocessError):
        return []


def _refs_text(refs: List[str], n: int = 8) -> str:
    if not refs:
        return "the parts"
    head = ", ".join(refs[:n])
    return head + (" and %d more" % (len(refs) - n) if len(refs) > n else "")


def _anim(shot: Dict[str, Any], **kw) -> Dict[str, Any]:
    sc = {"kind": "anim", "images": shot["images"], "fps": shot["fps"], "hold": shot["hold"]}
    sc.update(kw)
    return sc


def storyboard(design, spec, plan, fixture, jig, visuals, out_dir, work,
               shots: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    im = visuals.get("images", {})
    shots = shots or {}
    b = spec["board"]
    s = spec["summary"]
    tech = spec.get("technology") or {}
    req = (tech.get("required") or {})
    title = "%s - production test & assembly" % (b["title"] or b["name"])
    subtitle = "Generated from the KiCad design by PCB TestKit%s" % ((" - rev " + b["revision"]) if b["revision"] else "")
    scenes: List[Dict[str, Any]] = []

    # ---- intro and findings --------------------------------------------------------------------
    if shots.get("orbit"):
        scenes.append(_anim(shots["orbit"], overlay="card", title=title, subtitle=subtitle))
    else:
        scenes.append({"kind": "card", "bg": im.get("dut_iso"), "duration": 4.0, "title": title, "subtitle": subtitle})
    top = [f for f in spec["findings"] if f["severity"] in ("critical", "major") and not f.get("waived")][:4]
    bul = [{"text": f["title"], "sev": f["severity"]} for f in top]
    cls = req.get("class") or ("finer than %s offers" % tech.get("name", "the fab") if req.get("beyond") else "-")
    bul.append({"text": "Technology class needed: %s%s" % (cls, (" (" + ", ".join(req.get("services", [])[:3]) + ")")
                                                          if req.get("services") else ""), "sev": "info"})
    scenes.append({"kind": "image", "bg": im.get("dut_top") or im.get("dut_iso"), "duration": 6.0, "step": "",
                   "title": "Before you order: %s (score %d)" % (s["verdict"], s["score"]), "bullets": bul,
                   "caption": "Fix the weak points first - they cost yield and field returns.",
                   "sub": "The full list with locations is in the HTML report."})

    # ---- 1. assemble ----------------------------------------------------------------------------
    if shots.get("place"):
        meta = shots["place"].get("meta") or {}
        refs = meta.get("parts", [])
        n, smd = len(refs), int(meta.get("smd", 0))
        if smd == n:
            cap = "Pick-and-place: %d SMD part%s" % (n, "" if n == 1 else "s")
        elif smd:
            cap = "First the %d parts the jig does not hold (%d of them SMD)" % (n, smd)
        else:
            cap = "First fit %s - the jig does not hold %s" % (_refs_text(refs, 4), "it" if n == 1 else "them")
        scenes.append(_anim(shots["place"], step=1, title="Assemble the board", caption=cap,
                            sub="Reflow or solder these before the hand-soldered parts go in the jig."))
    if jig and shots.get("place2"):
        # two-jig workflow: fit the parts top side up in the placement jig, close the holding jig over them,
        # turn the stack over, lift off the placement jig, solder
        pj = jig.get("placement") or {}
        parts = (shots["place2"].get("meta") or {}).get("parts") or jig.get("parts_to_place", [])
        if shots.get("print2"):
            scenes.append(_anim(shots["print2"], step=1, title="Print the two jigs",
                                caption="Placement jig (%s x %s x %s mm) and holding jig (%s x %s x %s mm): print as they are, "
                                        "no supports." % (tuple(pj.get("jig_size_mm", ["?"] * 3)) + tuple(jig["jig_size_mm"])),
                                sub="The placement jig holds the PCB while you fit the parts; the holding jig keeps them in place "
                                    "while you solder."))
        scenes.append(_anim(shots["place2"], step=1, title="Fit the parts in the placement jig",
                            caption="PCB top side up, then %s into their holes." % _refs_text(parts),
                            sub="The leads go into the wells below the board; trim leads longer than %.0f mm."
                                % max(1.0, float(pj.get("lead_clearance_mm", 10.0)) - 0.5)))
        if shots.get("cover"):
            scenes.append(_anim(shots["cover"], step=1, title="Put the holding jig on top",
                                caption="Pockets down: every part sits in its pocket, the posts go through the mounting holes."))
        if shots.get("flip"):
            scenes.append(_anim(shots["flip"], step=1, title="Turn the stack over",
                                caption="Hold both jigs together and turn them over as one."))
        if shots.get("unload"):
            scenes.append(_anim(shots["unload"], step=1, title="Lift off the placement jig",
                                caption="Every lead now points up; the holding jig keeps each part flush and square."))
        if shots.get("solder"):
            n = (shots["solder"].get("meta") or {}).get("joints", 0)
            scenes.append(_anim(shots["solder"], step=1, title="Solder from above",
                                caption="Solder each lead, then clip it%s." % (" (%d joints)" % n if n else ""),
                                sub="After the first joints the video runs faster."))
        if shots.get("lift2"):
            scenes.append(_anim(shots["lift2"], step=1, title="Lift out the finished board",
                                caption="All %d hand-soldered parts sit flush and square." % len(jig.get("parts_to_place", []))))
    elif jig:
        size = "%s x %s x %s mm" % tuple(jig["jig_size_mm"])
        parts = jig.get("parts_to_place", [])
        if shots.get("print"):
            scenes.append(_anim(shots["print"], step=1, title="Print the solder jig",
                                caption="%s - print as is, no supports." % size,
                                sub="Deep pockets hold the hand-soldered parts; shallow pockets clear the parts already on the board."))
        elif im.get("jig_iso"):
            scenes.append({"kind": "image", "bg": im["jig_iso"], "duration": 5.0, "step": 1, "title": "Print the solder jig",
                           "caption": "%s - print as is, no supports." % size,
                           "sub": "Orange pockets hold the hand-soldered parts; shallow pockets clear the assembled SMD parts."})
        if shots.get("load"):
            refs = (shots["load"].get("meta") or {}).get("parts") or parts
            scenes.append(_anim(shots["load"], step=1, title="Load the jig",
                                caption="Drop %s upside down into their pockets." % _refs_text(refs),
                                sub="Each pocket fits one part, so nothing can go in the wrong place or turned."))
        if shots.get("slide"):
            scenes.append(_anim(shots["slide"], step=1, title="Lower the PCB onto the jig",
                                caption="Top side down: every lead slides through its hole. Solder from above.",
                                sub="The posts in the mounting holes keep the board square while you solder."))
        if shots.get("lift"):
            scenes.append(_anim(shots["lift"], step=1, title="Lift out the finished board",
                                caption="All %d hand-soldered parts sit flush and square." % len(parts)))
        if not (shots.get("load") or shots.get("slide")) and im.get("jig_exploded"):
            scenes.append({"kind": "image", "bg": im["jig_exploded"], "duration": 5.0, "step": 1, "title": "Hand-solder with the jig",
                           "caption": "Drop %s into the jig, lower the PCB top-side down, solder from above." % _refs_text(parts, 10)})

    # ---- 2. fixture -------------------------------------------------------------------------------
    bb = design.board.outline_bbox()
    pads = []
    for fp in design.board.footprints:
        for p in fp.pads:
            if p.has_copper:
                pads.append([[round(x, 3), round(y, 3)] for x, y in p.polygon()])
    mirror = plan.get("probe_side") == "top"
    probes = [{"x": (2 * bb.cx - p["x"]) if mirror else p["x"], "y": p["y"], "role": p["role"],
               "label": "%s %s" % (p["id"], C.base_net(p["net"])[:10])} for p in plan["probes"]]
    edges = [list(e) for e in design.board.edge_segs]
    if mirror:
        edges = [[2 * bb.cx - a, b_, 2 * bb.cx - c, d] for a, b_, c, d in edges]
        for pd in pads:
            for q in pd:
                q[0] = round(2 * bb.cx - q[0], 3)
    scenes.append({"kind": "map", "duration": 6.0, "step": 2, "title": "Test points (%s)" % ("both sides" if plan["probe_side"] == "both" else "%s side" % plan["probe_side"]),
                   "map": {"bbox": [bb.x0, bb.y0, bb.x1, bb.y1], "edges": edges, "pads": pads, "probes": probes},
                   "caption": "%d pogo pins: supply, ground and %d measurement channels" % (
                       len(plan["probes"]), sum(1 for p in plan["probes"] if p.get("channel"))),
                   "sub": "Red = supply in, white = GND, green = measured. Coverage %s%% of nets." % plan["coverage"]["percent"]})
    build = ["Route and order the fixture PCB", "Press in %d receptacles + %s pogo pins" % (len(plan["probes"]), plan["pogo"]["family"]),
             "Fit guide pins in the GUIDE holes", "Wire the header to the Pi ADC"]
    if shots.get("pogo"):
        scenes.append(_anim(shots["pogo"], step=2, title="Build the fixture", bullets=build,
                            caption="The fixture PCB is generated as a KiCad board - every pin sits under a test point."))
    elif im.get("fixture_iso"):
        scenes.append({"kind": "image", "bg": im["fixture_iso"], "duration": 5.0, "step": 2, "title": "Build the fixture",
                       "bullets": build, "caption": "The fixture PCB is generated as a KiCad board - every pin sits under a test point."})

    # ---- 3. test -----------------------------------------------------------------------------------
    if shots.get("press"):
        scenes.append(_anim(shots["press"], step=3, title="Test a board",
                            caption="Slide the board over the guide pins and press it down onto the pogo pins."))
    elif im.get("fixture_dut_seated"):
        scenes.append({"kind": "image", "bg": im["fixture_dut_seated"], "duration": 4.0, "step": 3, "title": "Test a board",
                       "caption": "Place the board on the guide pins and press it down onto the pogo pins."})
    lines = _runner_lines(out_dir, work)
    if lines:
        scenes.append({"kind": "terminal", "lines": lines, "fps": 4, "hold": 2.0, "step": 3, "title": "Run the test",
                       "caption": "python3 pcbtest_runner.py test --loop",
                       "sub": "Rails, signals and supply current are checked automatically; results go to results.csv."})
    scenes.append({"kind": "card", "bg": im.get("dut_iso"), "duration": 4.0, "title": "Everything is in the output folder",
                   "bullets": ["%s-guide.html - this guide with all steps" % b["name"], "%s-fixture.kicad_pcb - fixture PCB" % b["name"],
                               "%s-jig.stl - solder jig" % b["name"], "runner/ - Raspberry Pi test runner"]})
    return scenes


def _clip_inputs(scenes, frames_lists, list_dir, fps: float):
    """ffmpeg input args per scene and each clip's duration."""
    args, durs = [], []
    for i, (sc, files) in enumerate(zip(scenes, frames_lists)):
        if not files:
            continue
        if len(files) == 1:
            d = float(sc.get("duration", 4.0))
            args += ["-loop", "1", "-framerate", "%g" % fps, "-t", "%.3f" % d, "-i", files[0]]
        else:
            per = 1.0 / float(sc.get("fps", 10))
            hold = max(per, float(sc.get("hold", 1.5)))
            lst = os.path.join(list_dir, "clip_%02d.txt" % i)
            with open(lst, "w") as fh:
                for f in files[:-1]:
                    fh.write("file '%s'\nduration %.6f\n" % (f.replace("'", "'\\''"), per))
                fh.write("file '%s'\nduration %.6f\n" % (files[-1].replace("'", "'\\''"), hold))
                fh.write("file '%s'\n" % files[-1].replace("'", "'\\''"))
            d = per * (len(files) - 1) + hold
            args += ["-f", "concat", "-safe", "0", "-i", lst]
        durs.append(d)
    return args, durs


def video_settings(cfg: Dict[str, Any]) -> Dict[str, Any]:
    v = dict((cfg or {}).get("video") or {})
    return {"animate": bool(v.get("animate", True)), "fps": float(v.get("fps", 24) or 24),
            "size": (int(v.get("width", 1280) or 1280), int(v.get("height", 720) or 720)),
            "workers": int(v.get("workers", 0) or 0), "quality": str(v.get("quality", "basic") or "basic")}


def build_video(design, spec, plan, fixture, jig, visuals, out_dir: str, out_path: str, work_dir: Optional[str] = None,
                size=None, fade: float = 0.6, paths: Optional[Dict[str, str]] = None, visuals_dir: Optional[str] = None,
                progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, Any]:
    ff = find_ffmpeg()
    if not ff:
        return {"ok": False, "reason": "ffmpeg not found (install it or set FFMPEG)"}
    py = find_wx_python()
    if not py:
        return {"ok": False, "reason": "no Python with wxPython found (KiCad's bundled Python provides it)"}
    vs = video_settings(design.cfg)
    size = tuple(size or vs["size"])
    fps = vs["fps"]
    work = work_dir or os.path.join(out_dir, "visuals", "scenes", "video")
    os.makedirs(work, exist_ok=True)
    shots: Dict[str, Dict[str, Any]] = {}
    anim_info: Dict[str, Any] = {}
    if vs["animate"] and paths is not None and visuals.get("available", True):
        from .animate import build_shots
        try:
            res = build_shots(design, plan, fixture, jig, paths, visuals_dir or os.path.join(out_dir, "visuals"),
                              fps=fps, size=size, quality=vs["quality"], workers=vs["workers"], progress=progress)
            shots, anim_info = res["shots"], res["stats"]
            for line in res.get("log", []):
                design.warnings.append(line)
        except Exception as exc:  # animation is optional; fall back to stills
            design.warnings.append("video animation skipped: %r" % exc)
    scenes = storyboard(design, spec, plan, fixture, jig, visuals, out_dir, work, shots)
    job = {"size": list(size), "frames_dir": work, "scenes": scenes}
    job_path = os.path.join(work, "storyboard.json")
    with open(job_path, "w") as fh:
        json.dump(job, fh)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.dirname(HERE) + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run([py, "-m", "pcbtestkit.video_frames", job_path], capture_output=True, text=True, env=env, timeout=3600)
    fj = os.path.join(work, "frames.json")
    if r.returncode != 0 or not os.path.exists(fj):
        return {"ok": False, "reason": "frame rendering failed: %s" % (r.stderr or r.stdout)[-400:]}
    with open(fj) as fh:
        frames_lists = json.load(fh)
    used = [(sc, fl) for sc, fl in zip(scenes, frames_lists) if fl]
    args, durs = _clip_inputs([u[0] for u in used], [u[1] for u in used], work, fps)
    n = len(durs)
    W, H = size
    filt = []
    for i in range(n):
        filt.append("[%d:v]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,fps=%g,"
                    "format=yuv420p,settb=AVTB,setpts=PTS-STARTPTS[v%d]" % (i, W, H, W, H, fps, i))
    last = "v0"
    t = durs[0]
    for i in range(1, n):
        off = t - fade
        filt.append("[%s][v%d]xfade=transition=fade:duration=%.2f:offset=%.3f[x%d]" % (last, i, fade, off, i))
        last = "x%d" % i
        t = t + durs[i] - fade
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error"] + args + [
        "-filter_complex", ";".join(filt), "-map", "[%s]" % last, "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0 or not os.path.exists(out_path):
        return {"ok": False, "reason": "ffmpeg failed: %s" % r.stderr[-400:]}
    return {"ok": True, "path": out_path, "duration_s": round(t, 1), "scenes": n, "animated_shots": sorted(shots),
            "animation": anim_info}
