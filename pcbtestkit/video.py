"""Board-specific explainer video (MP4): what to fix, how to assemble the board with the jig, how to test it.

Pipeline:
  1. motion shots (pcbtestkit.animate): real 3D animation rendered frame by frame with kicad-cli, i.e. parts
     dropping onto the PCB, the jig being printed and loaded, the PCB sliding onto the jig, the fixture being
     built and the board pressed onto the pogo pins;
  2. storyboard (this module) mixing those shots with stills, a probe map and the simulated test run;
  3. titles and captions drawn onto the frames (pcbtestkit.video_frames, KiCad's Python with wxPython);
  4. MP4 (ffmpeg, x264) with cross-fades.
Requires kicad-cli, KiCad's Python and ffmpeg. When one is missing the video is not made, with a clear message,
and everything else still works. Set video.animate = false for a quick stills-only video.

Steps 3 and 4 run from the folder the storyboard is written to (``storyboard.json``, every path in it relative to
that folder). When the MP4 cannot be made there, the frames stay and ``BUILD-VIDEO.md`` / ``video-recipe.json`` say
how to finish it, by hand, by an AI assistant or with ``python -m pcbtestkit.video <that folder>``.
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
from .animate import later_jig_sets
from .kicad_cli import QUIET, find_kicad_cli

HERE = os.path.dirname(os.path.abspath(__file__))
RECIPE_MD, RECIPE_JSON = "BUILD-VIDEO.md", "video-recipe.json"

# where ffmpeg usually is when it is not on the PATH (KiCad started before ffmpeg was installed does not see a
# new PATH on Windows): Homebrew, Linux, winget, Chocolatey, Scoop, a manual install
FFMPEG_PLACES = ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg",
                 "%LOCALAPPDATA%/Microsoft/WinGet/Links/ffmpeg.exe",
                 "%LOCALAPPDATA%/Microsoft/WinGet/Packages/*/ffmpeg-*/bin/ffmpeg.exe",
                 "C:/ProgramData/chocolatey/bin/ffmpeg.exe", "~/scoop/shims/ffmpeg.exe", "~/scoop/apps/ffmpeg/current/bin/ffmpeg.exe",
                 "C:/ffmpeg/bin/ffmpeg.exe", "C:/Program Files/ffmpeg/bin/ffmpeg.exe", "C:/Program Files/FFmpeg/bin/ffmpeg.exe"]


def find_ffmpeg() -> Optional[str]:
    env = os.environ.get("FFMPEG")
    if env:
        return env
    w = shutil.which("ffmpeg")
    if w:
        return w
    for pat in FFMPEG_PLACES:
        pat = os.path.expanduser(os.path.expandvars(pat))
        if "%" in pat:                                  # variable not set on this OS
            continue
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    return None


def _rel(path: str, base: str) -> str:
    """``path`` relative to ``base`` with forward slashes (portable, and short command lines on Windows); the path
    itself when it is on another drive."""
    try:
        return os.path.relpath(path, base).replace(os.sep, "/")
    except ValueError:
        return path


def _abs(path: str, base: str) -> str:
    return path if not path or os.path.isabs(path) else os.path.normpath(os.path.join(base, path))


def kicad_python() -> Optional[str]:
    """KiCad's bundled Python (next to kicad-cli on Windows and Linux, in the app bundle on macOS)."""
    cli = find_kicad_cli()
    if not cli:
        return None
    app = os.path.dirname(os.path.dirname(cli))                          # .../KiCad.app/Contents
    cands = sorted(glob.glob(os.path.join(app, "Frameworks", "Python.framework", "Versions", "*", "bin", "python3")))
    cands += [os.path.join(os.path.dirname(cli), "python.exe"), os.path.join(os.path.dirname(cli), "python3")]
    return next((c for c in cands if os.path.exists(c)), None)


def find_python() -> str:
    """A Python to run the bundled test runner with. Inside KiCad ``sys.executable`` can be KiCad itself, and on
    Windows "python3" may be the Microsoft Store's placeholder, so KiCad's own Python comes next."""
    if os.path.basename(sys.executable).lower().startswith("python"):
        return sys.executable
    return kicad_python() or shutil.which("python3") or shutil.which("python") or "python3"


def _last_line(text: str) -> str:
    """The last meaningful line of a tool's output (the error), shortened."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return (lines[-1] if lines else "no output")[:300]


def find_wx_python() -> Optional[str]:
    """A Python interpreter that can import wx: KiCad's bundled one, or the current one."""
    cands: List[str] = []
    cli = find_kicad_cli()
    if cli:
        app = os.path.dirname(os.path.dirname(cli))                      # .../KiCad.app/Contents
        cands += sorted(glob.glob(os.path.join(app, "Frameworks", "Python.framework", "Versions", "*", "bin", "python3")))
        cands += [os.path.join(os.path.dirname(cli), "python.exe"), os.path.join(os.path.dirname(cli), "python3")]
    if os.path.basename(sys.executable).lower().startswith("python"):    # inside KiCad it can be KiCad itself
        cands.append(sys.executable)
    cands += ["/usr/bin/python3"]
    for c in cands:
        if c and os.path.exists(c):
            try:
                r = subprocess.run([c, "-c", "import wx"], capture_output=True, timeout=60, **QUIET)
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
        r = subprocess.run([find_python(), runner, "test", "--simulate", "--non-interactive", "--serial", "DEMO-0001",
                            "--results", os.path.join(work, "demo_results")], capture_output=True, timeout=120, **QUIET)
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


def _hand_pass_scenes(jig, shots, im, sfx: str = "", side: Optional[str] = None, nxt: Optional[str] = None,
                      total: int = 0, pas: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """The scenes of one hand-solder pass with its jig set. ``sfx`` picks that pass's shots ("" for the first,
    "_bottom" for a later one); ``side`` names the board side in the captions when the board has a jig set per
    side (None: one jig set, the captions it always had); ``nxt`` is the side of the next pass, ``total`` the
    hand-soldered parts of all passes; ``pas`` the later pass itself (still for a pass without shots)."""
    from .sides import other
    scenes: List[Dict[str, Any]] = []
    shot = {k[:-len(sfx)] if sfx else k: v for k, v in shots.items() if (k.endswith(sfx) if sfx else True)}
    parts_all = jig.get("parts_to_place", [])
    S = "%s-side " % side if side else ""

    def its(refs):
        return "its" if side and len(refs) == 1 else "their"

    def lifted(name: str) -> Dict[str, Any]:
        if not side or not nxt:
            return _anim(shot[name], step=1, title="Lift out the finished board",
                         caption="All %d hand-soldered parts sit flush and square." % (total if side else len(parts_all)))
        return _anim(shot[name], step=1, title="Lift out the board",
                     caption="The %d %s-side part%s sit%s flush and square." % (
                         len(parts_all), side, "" if len(parts_all) == 1 else "s", "s" if len(parts_all) == 1 else ""),
                     sub="Next: the %s-side parts, with their own jig set." % nxt)

    if shot.get("place2"):
        # two-jig workflow: fit the parts top side up in the placement jig, close the holding jig over them,
        # turn the stack over, lift off the placement jig, solder
        pj = jig.get("placement") or {}
        parts = (shot["place2"].get("meta") or {}).get("parts") or parts_all
        sizes = tuple(pj.get("jig_size_mm", ["?"] * 3)) + tuple(jig["jig_size_mm"])
        if shot.get("print2"):
            if side:
                scenes.append(_anim(shot["print2"], step=1, title="Print the %sjigs" % S,
                                    caption="Placement jig (%s x %s x %s mm) and holding jig (%s x %s x %s mm) for the %s-side "
                                            "parts: print as they are, no supports." % (sizes + (side,)),
                                    sub="The placement jig holds the PCB while you fit the parts; the holding jig keeps them in "
                                        "place while you solder." if pas is None else
                                        "Their pockets clear the parts and leads already on the board, so the board lies flat."))
            else:
                scenes.append(_anim(shot["print2"], step=1, title="Print the two jigs",
                                    caption="Placement jig (%s x %s x %s mm) and holding jig (%s x %s x %s mm): print as they are, "
                                            "no supports." % sizes,
                                    sub="The placement jig holds the PCB while you fit the parts; the holding jig keeps them in place "
                                        "while you solder."))
        scenes.append(_anim(shot["place2"], step=1, title="Fit the %sparts in the placement jig" % S,
                            caption="PCB %s side up, then %s into %s holes." % (side or "top", _refs_text(parts), its(parts)),
                            sub="The leads go into the wells below the board; trim leads longer than %.0f mm."
                                % max(1.0, float(pj.get("lead_clearance_mm", 10.0)) - 0.5)))
        if shot.get("cover"):
            scenes.append(_anim(shot["cover"], step=1, title="Put the holding jig on top",
                                caption="Pockets down: every part sits in its pocket, the posts go through the mounting holes."))
        if shot.get("flip"):
            scenes.append(_anim(shot["flip"], step=1, title="Turn the stack over",
                                caption="Hold both jigs together and turn them over as one%s." % (
                                    ": the %s side now faces up" % other(side) if side else "")))
        if shot.get("unload"):
            scenes.append(_anim(shot["unload"], step=1, title="Lift off the placement jig",
                                caption="Every lead now points up; the holding jig keeps each part flush and square."))
        if shot.get("solder"):
            n = (shot["solder"].get("meta") or {}).get("joints", 0)
            scenes.append(_anim(shot["solder"], step=1, title="Solder from above",
                                caption="Solder each lead%s, then clip it%s." % (" on the %s side" % other(side) if side else "",
                                                                                " (%d joints)" % n if n else ""),
                                sub="After the first joints the video runs faster."))
        if shot.get("lift2"):
            scenes.append(lifted("lift2"))
        return scenes
    size = "%s x %s x %s mm" % tuple(jig["jig_size_mm"])
    if shot.get("print"):
        scenes.append(_anim(shot["print"], step=1, title="Print the %ssolder jig" % S,
                            caption="%s - print as is, no supports." % size,
                            sub="Deep pockets hold the hand-soldered parts; shallow pockets clear the parts already on the board."))
    elif im.get("jig_iso"):
        scenes.append({"kind": "image", "bg": im["jig_iso"], "duration": 5.0, "step": 1, "title": "Print the %ssolder jig" % S,
                       "caption": "%s - print as is, no supports." % size,
                       "sub": "Orange pockets hold the hand-soldered parts; shallow pockets clear the assembled SMD parts."})
    if shot.get("load"):
        refs = (shot["load"].get("meta") or {}).get("parts") or parts_all
        scenes.append(_anim(shot["load"], step=1, title="Load the %sjig" % S,
                            caption="Drop %s upside down into %s pockets." % (_refs_text(refs), its(refs)),
                            sub="Each pocket fits one part, so nothing can go in the wrong place or turned."))
    if shot.get("slide"):
        scenes.append(_anim(shot["slide"], step=1, title="Lower the PCB onto the jig",
                            caption="%s side down: every lead slides through its hole. Solder from above." % (side or "top").capitalize(),
                            sub="The posts in the mounting holes keep the board square while you solder."))
    if shot.get("lift"):
        scenes.append(lifted("lift"))
    if not (shot.get("load") or shot.get("slide")) and im.get("jig_exploded"):
        scenes.append({"kind": "image", "bg": im["jig_exploded"], "duration": 5.0, "step": 1, "title": "Hand-solder with the jig",
                       "caption": "Drop %s into the jig, lower the PCB top-side down, solder from above." % _refs_text(parts_all, 10)})
    if pas is not None and not scenes:                 # a later pass without animation: one still with the steps
        two = bool(jig.get("placement"))
        steps = (["Print the %s-side placement and holding jigs" % side, "PCB %s side up in the placement jig, fit %s" % (
                      side, _refs_text(parts_all, 4)), "Holding jig on top, turn the stack over",
                  "Lift off the placement jig, solder from above"] if two else
                 ["Print the %s-side solder jig" % side, "Drop %s into the jig" % _refs_text(parts_all, 4),
                  "Lower the PCB %s side down, solder from above" % side])
        scenes.append({"kind": "image", "bg": im.get("dut_bottom" if side == "bottom" else "dut_top"), "duration": 5.0,
                       "step": 1, "title": "Then the %s-side parts (pass %s)" % (side, pas.get("pass", "")), "bullets": steps,
                       "caption": "Hand-solder %s with the %s-side jig set; it has pockets for the parts already on the "
                                  "board." % (_refs_text(parts_all, 6), side)})
    return scenes


def _live_test_scene(shot: Dict[str, Any], lines: List[str], plan: Dict[str, Any]) -> Dict[str, Any]:
    """The boards on the fixture with the tested copper lighting up, beside the runner's output scrolling in step
    (each event of the shot's timeline starts at the runner line of its step and board)."""
    events = (shot.get("meta") or {}).get("events") or []
    nsite = int((shot.get("meta") or {}).get("sites") or 1)
    starts: List[int] = []
    pos = 0
    for e in events:
        k = (e.get("sites") or [1])[0]
        idx = None
        for sid in e.get("steps") or []:
            pat = ("[S%d] %s " % (k, sid)) if nsite > 1 else ("  %s " % sid)
            idx = next((i for i in range(pos, len(lines)) if (pat in lines[i] if nsite > 1 else lines[i].startswith(pat.strip())
                                                                or lines[i].startswith(pat))), None)
            if idx is not None:
                break
        if idx is None and e.get("phase") == "program":
            idx = next((i for i in range(pos, len(lines)) if "Programming" in lines[i]), None)
        idx = pos if idx is None else idx
        starts.append(idx)
        pos = idx
    line_at: List[int] = []
    caption_at: List[int] = []
    for path in shot["images"]:
        m = re.search(r"_(\d+)\.png$", path)
        f = int(m.group(1)) if m else len(line_at)
        j = next((i for i, e in enumerate(events) if e["f0"] <= f <= e["f1"]), len(events) - 1)
        e = events[j] if events else {"f0": 0, "f1": 1}
        nxt = starts[j + 1] if j + 1 < len(starts) else len(lines)
        frac = (f - e["f0"]) / float(max(1, e["f1"] - e["f0"]))
        line_at.append(int(round(starts[j] + 1 + frac * max(0, nxt - starts[j] - 1))) if starts else len(lines))
        caption_at.append(max(0, j))
    progs = [st for st in plan.get("sequence") or [] if st.get("phase") == "program"]
    sub = ("Every board is programmed at the same time; the checks that need no firmware run meanwhile."
           if progs and nsite > 1 else "Rails, signals and supply current are checked automatically; results go to results.csv.")
    return {"kind": "anim_live", "images": shot["images"], "fps": shot["fps"], "hold": shot["hold"], "step": 3,
            "title": "Test the batch of %d boards" % nsite if nsite > 1 else "Run the test", "lines": lines,
            "line_at": line_at, "captions": [e["label"] for e in events] or [""], "caption_at": caption_at, "sub": sub}


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
    later_sets = later_jig_sets(jig) if jig else []
    if jig:
        # one jig set per hand-solder pass: the first, then (parts on both sides) the other side's, turned over
        sets = [(jig, "", None)] + [(ej, "_" + p["side"], p) for p, ej in later_sets]
        sides = [str(jig.get("side") or "top")] + [p["side"] for p, _ in later_sets]
        total = sum(len(j.get("parts_to_place", [])) for j, _, _ in sets)
        for k, (j, sfx, p) in enumerate(sets):
            scenes += _hand_pass_scenes(j, shots, im if k == 0 else {}, sfx, sides[k] if later_sets else None,
                                        sides[k + 1] if k + 1 < len(sets) else None, total, p)

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
    fac = plan.get("facility") or {}
    build = ["Route and order the fixture PCB", "Press in %d receptacles + %s pogo pins" % (len(plan["probes"]), plan["pogo"]["family"]),
             ("Fit a nest and a toggle clamp for every board" if ((fixture or {}).get("nest") or {}).get("enabled") else
              "Fit guide pins in the GUIDE holes"), "Wire the header to the Pi ADC" if not fac.get("connectors") else
             "Plug it into %s" % fac.get("name", fac.get("id", "the tester"))]
    if shots.get("pogo"):
        scenes.append(_anim(shots["pogo"], step=2, title="Build the fixture", bullets=build,
                            caption="The fixture PCB is generated as a KiCad board - every pin sits under a test point."))
    elif im.get("fixture_iso"):
        scenes.append({"kind": "image", "bg": im["fixture_iso"], "duration": 5.0, "step": 2, "title": "Build the fixture",
                       "bullets": build, "caption": "The fixture PCB is generated as a KiCad board - every pin sits under a test point."})

    # ---- 3. test -----------------------------------------------------------------------------------
    nsite = len((fixture or {}).get("sites") or []) or 1
    nest = (fixture or {}).get("nest") or {}
    if nest.get("enabled"):
        into = "Slide the board into its nest up to the stop" if nest.get("style") == "slide" else "Drop the board into its nest"
        press_cap = ("%s, above the pogo pins, and close the clamp: board and nest go down onto the pins." % into if nsite == 1
                     else "Every board gets its own nest and clamp: load them one by one, close each clamp (%d boards)." % nsite)
    else:
        press_cap = ("Slide the board over the guide pins and press it down onto the pogo pins." if nsite == 1
                     else "Put a board on each of the %d sites, over its guide pins, and press them down." % nsite)
    if shots.get("press"):
        scenes.append(_anim(shots["press"], step=3, title="Test a board" if nsite == 1 else "Load the batch", caption=press_cap))
    elif im.get("fixture_dut_seated"):
        scenes.append({"kind": "image", "bg": im["fixture_dut_seated"], "duration": 4.0, "step": 3, "title": "Test a board",
                       "caption": press_cap})
    lines = _runner_lines(out_dir, work)
    if lines and shots.get("test"):
        scenes.append(_live_test_scene(shots["test"], lines, plan))
    elif lines:
        scenes.append({"kind": "terminal", "lines": lines, "fps": 4, "hold": 2.0, "step": 3, "title": "Run the test",
                       "caption": "python3 pcbtest_runner.py test --loop",
                       "sub": "Rails, signals and supply current are checked automatically; results go to results.csv."})
    files = ["%s-guide.html - this guide with all steps" % b["name"], "%s-fixture.kicad_pcb - fixture PCB" % b["name"],
             "%s-jig.stl - solder jig" % b["name"], "runner/ - Raspberry Pi test runner" if not fac.get("driver") else
             "runner/ - test runner with the %s driver" % fac.get("name", fac.get("id", ""))]
    if jig and later_sets:
        files[2] = "%s-jig.stl - %s-side solder jig" % (b["name"], jig.get("side") or "top")
        for p, ej in later_sets:
            files.insert(3, "%s - %s-side solder jig" % (os.path.basename(ej.get("stl") or "%s-jig-%s.stl" % (b["name"], p["side"])),
                                                       p["side"]))
    scenes.append({"kind": "card", "bg": im.get("dut_iso"), "duration": 4.0, "title": "Everything is in the output folder",
                   "bullets": files})
    return scenes


def _clip_inputs(scenes, frames_lists, list_dir, fps: float, prefix: str = "clip"):
    """ffmpeg input args per scene and each clip's duration. Animated scenes get a concat list
    (<prefix>_NN.txt in list_dir); every path is relative to list_dir, so ffmpeg runs there."""
    args, durs = [], []

    def q(f: str) -> str:
        return _rel(_abs(f, list_dir), list_dir).replace("'", "'\\''")

    for i, (sc, files) in enumerate(zip(scenes, frames_lists)):
        if not files:
            continue
        if len(files) == 1:
            d = float(sc.get("duration", 4.0))
            args += ["-loop", "1", "-framerate", "%g" % fps, "-t", "%.3f" % d, "-i", _rel(_abs(files[0], list_dir), list_dir)]
        else:
            per = 1.0 / float(sc.get("fps", 10))
            hold = max(per, float(sc.get("hold", 1.5)))
            name = "%s_%02d.txt" % (prefix, i)
            with open(os.path.join(list_dir, name), "w", encoding="utf-8", newline="\n") as fh:
                for f in files[:-1]:
                    fh.write("file '%s'\nduration %.6f\n" % (q(f), per))
                fh.write("file '%s'\nduration %.6f\n" % (q(files[-1]), hold))
                fh.write("file '%s'\n" % q(files[-1]))
            d = per * (len(files) - 1) + hold
            args += ["-f", "concat", "-safe", "0", "-i", name]
        durs.append(d)
    return args, durs


def raw_frames(sc: Dict[str, Any]) -> List[str]:
    """A scene's frames without titles and captions: the animation frames, or the still behind a card or image."""
    if sc.get("kind") == "anim":
        return list(sc.get("images") or [])
    return [sc["bg"]] if sc.get("bg") else []


def video_settings(cfg: Dict[str, Any]) -> Dict[str, Any]:
    v = dict((cfg or {}).get("video") or {})
    return {"animate": bool(v.get("animate", True)), "fps": float(v.get("fps", 24) or 24),
            "size": (int(v.get("width", 1280) or 1280), int(v.get("height", 720) or 720)),
            "workers": int(v.get("workers", 0) or 0), "quality": str(v.get("quality", "standard") or "standard")}


# H.264 encoders in order of preference (libx264 is missing from some ffmpeg builds), then MPEG-4 as a last resort
ENCODERS = [("libx264", ["-preset", "medium", "-crf", "20"]), ("libopenh264", ["-b:v", "8M"]),
            ("h264_videotoolbox", ["-b:v", "8M"]), ("h264_mf", ["-b:v", "8M"]), ("h264_vaapi", ["-b:v", "8M"]),
            ("mpeg4", ["-q:v", "3"])]


def ffmpeg_caps(ff: str) -> Dict[str, Any]:
    """The encoders (from ENCODERS) and whether the xfade filter exist in this ffmpeg."""
    def listing(flag: str) -> str:
        try:
            return subprocess.run([ff, "-hide_banner", flag], capture_output=True, timeout=60, **QUIET).stdout
        except (OSError, subprocess.SubprocessError):
            return ""
    enc = listing("-encoders")
    names = {ln.split()[1] for ln in enc.splitlines() if len(ln.split()) > 1 and ln.strip()[:1] == "V"}
    found = [e for e in ENCODERS if e[0] in names] or [ENCODERS[0], ENCODERS[-1]]    # unknown listing: just try
    return {"encoders": found, "xfade": " xfade " in listing("-filters") or not enc}


def filter_graph(durs: List[float], size, fps: float, fade: float, xfade: bool):
    """ffmpeg filter graph joining the clips (cross-fades with xfade, plain cuts without). Returns (graph, out label,
    total duration)."""
    W, H = size
    n = len(durs)
    # fps last: since ffmpeg 7 setpts leaves a stream without a fixed frame rate, and xfade needs one
    filt = ["[%d:v]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,settb=AVTB,setpts=PTS-STARTPTS,"
            "fps=%g,format=yuv420p[v%d]" % (i, W, H, W, H, fps, i) for i in range(n)]
    if not xfade or n == 1:
        if n == 1:
            return ";".join(filt), "v0", durs[0]
        filt.append("%sconcat=n=%d:v=1:a=0[cat]" % ("".join("[v%d]" % i for i in range(n)), n))
        return ";".join(filt), "cat", sum(durs)
    last, t = "v0", durs[0]
    for i in range(1, n):
        filt.append("[%s][v%d]xfade=transition=fade:duration=%.2f:offset=%.3f[x%d]" % (last, i, fade, t - fade, i))
        last = "x%d" % i
        t = t + durs[i] - fade
    return ";".join(filt), last, t


def video_fingerprint(design, paths: Optional[Dict[str, str]], vs: Dict[str, Any], size) -> str:
    """What the animation frames depend on: the board, the generated fixture and jigs, the settings, the video
    size and quality, and the PCB TestKit version. Frames kept from a failed run are reused while it matches."""
    import hashlib
    from . import __version__
    h = hashlib.sha1()
    from .animate import ANIM_REV
    h.update(("%s|%s|%s|%s|%s" % (__version__, ANIM_REV, list(size), vs, json.dumps(design.cfg, sort_keys=True,
                                                                                     default=str))).encode())
    keys = ["fixture", "jig_stl", "jig_place_stl"] + sorted(k for k in (paths or {}) if k.startswith("jig_hand_")
                                                           and k.endswith("stl"))          # the other sides' jig sets
    for p in [design.board.path] + [(paths or {}).get(k) for k in keys]:
        if p and os.path.exists(p):
            with open(p, "rb") as fh:
                h.update(fh.read())
    return h.hexdigest()


def _kept_shots(anim_dir: str, fp: str) -> Optional[Dict[str, Any]]:
    """Animation shots kept from an earlier run with the same fingerprint, if every frame is still there."""
    path = os.path.join(anim_dir, "shots.json")
    try:
        with open(path) as fh:
            kept = json.load(fh)
    except (OSError, ValueError):
        return None
    if kept.get("fingerprint") != fp:
        return None
    if not all(os.path.exists(f) for sh in kept.get("shots", {}).values() for f in sh.get("images", [])):
        return None
    return kept


def build_video(design, spec, plan, fixture, jig, visuals, out_dir: str, out_path: str, work_dir: Optional[str] = None,
                size=None, fade: float = 0.6, paths: Optional[Dict[str, str]] = None, visuals_dir: Optional[str] = None,
                progress: Optional[Callable[[int, int], None]] = None, cancel: Optional[Any] = None) -> Dict[str, Any]:
    """Make the MP4. Every stage keeps its results until the video is done, so a run after a failure reuses them:
    the 3D animation frames (when the board, fixture, jigs, settings and version are unchanged) and the caption
    frames (when the storyboard is the same); only what is missing or changed is made again.

    A missing ffmpeg or wxPython does not stop the frames being made: whatever can be done is done, and the
    folder gets BUILD-VIDEO.md / video-recipe.json saying how to finish the video from those frames."""
    ff = find_ffmpeg()
    py = find_wx_python()
    missing = ([] if ff else ["ffmpeg not found (install it or set FFMPEG)"]) + (
        [] if py else ["no Python with wxPython found (KiCad's bundled Python provides it)"])
    vs = video_settings(design.cfg)
    size = tuple(size or vs["size"])
    fps = vs["fps"]
    work = work_dir or os.path.join(out_dir, "visuals", "scenes", "video")
    os.makedirs(work, exist_ok=True)
    shots: Dict[str, Dict[str, Any]] = {}
    anim_info: Dict[str, Any] = {}
    reused: List[str] = []
    if vs["animate"] and paths is not None and visuals.get("available", True):
        vdir = visuals_dir or os.path.join(out_dir, "visuals")
        anim_dir = os.path.join(vdir, "scenes", "anim")
        fp = video_fingerprint(design, paths, vs, size)
        kept = _kept_shots(anim_dir, fp)
        if kept is not None:
            shots, anim_info = kept["shots"], kept.get("stats", {})
            reused.append("%d animation frames" % sum(len(sh["images"]) for sh in shots.values()))
        else:
            from .animate import Cancelled, build_shots
            # frames of an interrupted run for the same board and settings are kept: render only the missing ones
            marker = os.path.join(anim_dir, "fingerprint.txt")
            try:
                with open(marker, encoding="utf-8") as fh:
                    resume = fh.read().strip() == fp
            except OSError:
                resume = False
            os.makedirs(anim_dir, exist_ok=True)
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write(fp)
            try:
                res = build_shots(design, plan, fixture, jig, paths, vdir, fps=fps, size=size, quality=vs["quality"],
                                  workers=vs["workers"], progress=progress, resume=resume, cancel=cancel)
                shots, anim_info = res["shots"], res["stats"]
                if anim_info.get("kept"):
                    reused.append("%d animation frames of an interrupted run" % anim_info["kept"])
                for line in res.get("log", []):
                    design.warnings.append(line)
                if shots:
                    with open(os.path.join(anim_dir, "shots.json"), "w") as fh:
                        json.dump({"fingerprint": fp, "shots": shots, "stats": anim_info}, fh)
            except Cancelled as exc:
                return {"ok": False, "reason": "cancelled (%s); the frames are kept and the next run goes on from there" % exc,
                        "cancelled": True}
            except Exception as exc:  # animation is optional; fall back to stills
                design.warnings.append("video animation skipped: %r" % exc)
    scenes = storyboard(design, spec, plan, fixture, jig, visuals, out_dir, work, shots)
    # every path relative to the folder: the storyboard and the steps after it can be finished anywhere
    job = {"size": list(size), "fps": fps, "fade": fade, "output": _rel(out_path, work), "frames_dir": ".",
           "scenes": [dict(sc, **({"bg": _rel(sc["bg"], work)} if sc.get("bg") else {}),
                           **({"images": [_rel(f, work) for f in sc["images"]]} if sc.get("images") else {}))
                      for sc in scenes]}
    job_text = json.dumps(job, sort_keys=True)
    job_path = os.path.join(work, "storyboard.json")
    try:
        with open(job_path, encoding="utf-8") as fh:
            same = fh.read() == job_text
    except OSError:
        same = False
    if not same:                                    # another storyboard: its caption frames are drawn again
        with open(job_path, "w", encoding="utf-8") as fh:
            fh.write(job_text)
        if os.path.exists(os.path.join(work, "frames.json")):
            os.remove(os.path.join(work, "frames.json"))
    for name in (RECIPE_MD, RECIPE_JSON):            # from an earlier failure
        if os.path.exists(os.path.join(work, name)):
            os.remove(os.path.join(work, name))
    if missing:
        res = {"ok": False, "reason": "; ".join(missing)}
        if py:                                       # get as far as possible: only the MP4 step is left then
            drawn = draw_captions(work, py)
            if not drawn.get("ok"):
                res["reason"] += "; " + drawn["reason"]
    else:
        res = finish_video(work, py=py, ff=ff)
    res["reused"] = reused + res.get("reused", [])
    res["animated_shots"], res["animation"] = sorted(shots), anim_info
    if not res.get("ok"):
        notes = write_build_notes(work, res["reason"])
        res["build_notes"] = notes
        res["log"] = ("To finish the video from the kept frames without rendering them again, follow %s (or run "
                      "python -m pcbtestkit.video %s).\n\n%s" % (_rel(notes, out_dir), _rel(work, out_dir), res.get("log") or ""))
    return res


def _kept_captions(work: str) -> Optional[List[List[str]]]:
    """Caption frames drawn earlier for the storyboard in ``work`` (frames.json), if every frame is still there."""
    try:
        with open(os.path.join(work, "frames.json"), encoding="utf-8") as fh:
            old = json.load(fh)
    except (OSError, ValueError):
        return None
    if not all(os.path.exists(_abs(f, work)) for fl in old for f in fl):
        return None
    return old


def draw_captions(work: str, py: Optional[str] = None) -> Dict[str, Any]:
    """Step 3: titles and captions drawn onto every frame by ``python -m pcbtestkit.video_frames storyboard.json``
    (a Python with wxPython: KiCad's bundled one). Writes the frames and frames.json into ``work``."""
    if _kept_captions(work) is not None:
        return {"ok": True, "reused": ["the caption frames"]}
    py = py or find_wx_python()
    if not py:
        return {"ok": False, "reason": "no Python with wxPython found (KiCad's bundled Python provides it)"}
    fj = os.path.join(work, "frames.json")
    if os.path.exists(fj):
        os.remove(fj)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.dirname(HERE) + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [py, "-m", "pcbtestkit.video_frames", os.path.join(work, "storyboard.json")]
    # wxPython on Linux needs a display: on a machine without one (a server, CI) use a virtual one when there is
    no_display = sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if no_display and shutil.which("xvfb-run"):
        cmd = [shutil.which("xvfb-run") or "xvfb-run", "-a"] + cmd
    r = subprocess.run(cmd, capture_output=True, env=env, timeout=3600, **QUIET)
    if r.returncode != 0 or not os.path.exists(fj):
        why = _last_line(r.stderr or r.stdout)
        if no_display and not shutil.which("xvfb-run"):
            why += " (no display: install xvfb so xvfb-run gives the captions one, or finish the video without " \
                   "captions: python -m pcbtestkit.video <folder> --no-captions)"
        return {"ok": False, "reason": "drawing the titles and captions (%s -m pcbtestkit.video_frames) failed: %s" % (
                    os.path.basename(py), why),
                "log": "exit code %s\n--- stdout ---\n%s\n--- stderr ---\n%s" % (r.returncode, r.stdout, r.stderr)}
    return {"ok": True, "reused": []}


def _mp4_command(ff: str, args: List[str], graph: str, last: str, encoder: str, opts: List[str], out: str) -> List[str]:
    return [ff, "-y", "-hide_banner", "-loglevel", "error"] + args + ["-filter_complex", graph, "-map", "[%s]" % last,
                                                                       "-c:v", encoder] + opts + [
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]


def finish_video(work: str, py: Optional[str] = None, ff: Optional[str] = None, captions: bool = True,
                 out_path: Optional[str] = None) -> Dict[str, Any]:
    """Steps 3 and 4 from ``work``/storyboard.json: draw the captions (reused when already drawn for this
    storyboard), then join the clips into the MP4 with ffmpeg, which runs in ``work`` with relative paths.
    captions=False joins the plain frames instead (no wxPython needed)."""
    with open(os.path.join(work, "storyboard.json"), encoding="utf-8") as fh:
        job = json.load(fh)
    size, fps, fade = tuple(job["size"]), float(job.get("fps", 24)), float(job.get("fade", 0.6))
    out_path = os.path.abspath(out_path) if out_path else _abs(job.get("output") or "explainer.mp4", work)
    reused: List[str] = []
    if captions:
        drawn = draw_captions(work, py)
        if not drawn.get("ok"):
            return drawn
        reused += drawn.get("reused", [])
        frames_lists = _kept_captions(work) or []
    else:
        frames_lists = [raw_frames(sc) for sc in job["scenes"]]
    ff = ff or find_ffmpeg()
    if not ff:
        return {"ok": False, "reason": "ffmpeg not found (install it or set FFMPEG)", "reused": reused}
    used = [(sc, fl) for sc, fl in zip(job["scenes"], frames_lists) if fl]
    if not used:
        return {"ok": False, "reason": "no frames to join", "reused": reused}
    args, durs = _clip_inputs([u[0] for u in used], [u[1] for u in used], work, fps, "clip" if captions else "plain")
    caps = ffmpeg_caps(ff)
    graph, last, t = filter_graph(durs, size, fps, fade, caps["xfade"])
    tried = []
    r = None
    for name, opts in caps["encoders"]:
        r = subprocess.run(_mp4_command(ff, args, graph, last, name, opts, _rel(out_path, work)), capture_output=True,
                           timeout=3600, cwd=work, **QUIET)
        if r.returncode == 0 and os.path.exists(out_path):
            return {"ok": True, "path": out_path, "duration_s": round(t, 1), "scenes": len(durs), "encoder": name,
                    "crossfades": caps["xfade"], "captions": captions, "reused": reused}
        tried.append("--- %s: exit code %s ---\n%s" % (name, r.returncode, r.stderr))
    return {"ok": False, "reason": "ffmpeg (%s) could not join the frames into the MP4 with %s: %s" % (
                ff, ", ".join(e[0] for e in caps["encoders"]), _last_line(r.stderr if r else "")),
            "reused": reused,
            "log": "frames reused: %s\ncross-fades: %s\n%s" % (", ".join(reused) or "none", caps["xfade"], "\n".join(tried))}


def _scene_seconds(sc: Dict[str, Any]) -> float:
    """How long a scene of the storyboard plays (before the cross-fades overlap it with its neighbours)."""
    if sc["kind"] == "anim":
        n = len(sc.get("images") or [])
        return round(max(0, n - 1) / float(sc.get("fps", 10)) + float(sc.get("hold", 1.5)), 2)
    if sc["kind"] == "terminal":
        return round(len(sc.get("lines") or []) / float(sc.get("fps", 4)) + float(sc.get("hold", 2.0)), 2)
    return round(float(sc.get("duration", 4.0)), 2)


def _shell(cmd: List[str]) -> str:
    """A command line to paste into a POSIX shell or PowerShell (single quotes work in both)."""
    import shlex
    return " ".join(shlex.quote(c) for c in cmd)


def write_build_notes(work: str, reason: str = "") -> str:
    """BUILD-VIDEO.md and video-recipe.json in ``work``: what is there, what is left and the exact commands, so
    the video can be finished from the kept frames by hand, by an AI assistant or with
    ``python -m pcbtestkit.video <work>``. Every path is relative to ``work``. Returns the .md path."""
    from . import __version__
    with open(os.path.join(work, "storyboard.json"), encoding="utf-8") as fh:
        job = json.load(fh)
    size, fps, fade = tuple(job["size"]), float(job.get("fps", 24)), float(job.get("fade", 0.6))
    out = job.get("output") or "explainer.mp4"
    scenes = job["scenes"]
    drawn = _kept_captions(work)
    missing_raw = sorted({f for sc in scenes for f in raw_frames(sc) if not os.path.exists(_abs(f, work))})
    enc = [list(e) for e in ENCODERS]

    def plan_for(frames_lists, prefix):
        used = [(sc, fl) for sc, fl in zip(scenes, frames_lists) if fl]
        if not used:
            return None
        args, durs = _clip_inputs([u[0] for u in used], [u[1] for u in used], work, fps, prefix)
        graph, last, t = filter_graph(durs, size, fps, fade, True)
        plain_graph, plain_last, plain_t = filter_graph(durs, size, fps, fade, False)
        return {"command": _mp4_command("ffmpeg", args, graph, last, ENCODERS[0][0], ENCODERS[0][1], out),
                "without_xfade": _mp4_command("ffmpeg", args, plain_graph, plain_last, ENCODERS[0][0], ENCODERS[0][1], out),
                "duration_s": round(t, 1), "clips": len(durs)}

    with_captions = plan_for(drawn, "clip") if drawn else None
    plain = plan_for([raw_frames(sc) for sc in scenes], "plain")
    captions_cmd = ["python", "-m", "pcbtestkit.video_frames", "storyboard.json"]
    recipe = {
        "about": "PCB TestKit %s explainer video, not finished: %s. Every path is relative to this folder; run the "
                 "commands in it. See BUILD-VIDEO.md." % (__version__, reason or "see BUILD-VIDEO.md"),
        "reason": reason, "output": out, "size": list(size), "fps": fps, "crossfade_s": fade,
        "finish": ["python", "-m", "pcbtestkit.video", "."],
        "steps": [
            {"step": "3d_frames", "done": not missing_raw, "missing": missing_raw[:20],
             "how": "kicad-cli renders them (pcbtestkit.animate); generate the video again in PCB TestKit to make the "
                    "missing ones (kept frames are reused)"},
            {"step": "captions", "done": drawn is not None, "command": captions_cmd,
             "needs": "a Python with wxPython (KiCad's bundled Python) and the folder that contains the pcbtestkit "
                      "package on PYTHONPATH", "writes": "s<scene>_<frame>.png and frames.json"},
            {"step": "mp4", "done": False, "with_captions": with_captions, "without_captions": plain,
             "encoders": enc,
             "note": "if libx264 is missing, replace '-c:v libx264 -preset medium -crf 20' with another encoder from "
                     "'encoders'; if the xfade filter is missing use 'without_xfade'"}],
        "scenes": [{"scene": i, "kind": sc["kind"], "title": sc.get("title", ""), "caption": sc.get("caption", ""),
                    "sub": sc.get("sub", ""), "frames": len(raw_frames(sc)), "seconds": _scene_seconds(sc)}
                   for i, sc in enumerate(scenes)]}
    with open(os.path.join(work, RECIPE_JSON), "w", encoding="utf-8") as fh:
        json.dump(recipe, fh, indent=1)

    L = ["# Finish the explainer video", "",
         "PCB TestKit %s could not make `%s`: %s." % (__version__, out, reason or "see below"), "",
         "Everything it needs is in this folder, and every path below is relative to it: run the commands here. "
         "`video-recipe.json` has the same in machine-readable form (commands as argument lists).", "",
         "## Quickest way", "",
         "```", _shell(["python", "-m", "pcbtestkit.video", "."]), "```", "",
         "Run it with the folder that contains the `pcbtestkit` package on `PYTHONPATH` (the PCB TestKit plugin folder "
         "in KiCad's `3rdparty/plugins`, or the repository). It draws the captions if they are not drawn yet (needs "
         "wxPython: use KiCad's bundled Python, below) and joins everything with ffmpeg. Add `--no-captions` to join the "
         "plain frames with ffmpeg alone, `--ffmpeg PATH` / `--python PATH` to point at the tools.", "",
         "KiCad's bundled Python: macOS `/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/"
         "Current/bin/python3`, Windows `C:\\Program Files\\KiCad\\<version>\\bin\\python.exe`, Linux the system "
         "`python3` with `python3-wxgtk4.0`.", "",
         "## What is here", "",
         "- `storyboard.json`: the %d scenes in order (kind, title, caption, frames and timing)." % len(scenes),
         "- 3D frames: %s" % ("all there (`../anim/`)." if not missing_raw else
                             "%d missing, e.g. `%s` - generate the video again in PCB TestKit." % (len(missing_raw), missing_raw[0])),
         "- Caption frames (`s<scene>_<frame>.png`, listed in `frames.json`): %s" % (
             "drawn." if drawn is not None else "not drawn yet (step 1)."), "",
         "## Step 1: titles and captions%s" % (" (done)" if drawn is not None else ""), "",
         "```", _shell(captions_cmd), "```", "",
         "Reads `storyboard.json`, draws the title bar, caption and bullets of each scene onto its frames and writes "
         "`frames.json` (the frames per scene).", "",
         "## Step 2: the MP4", ""]
    if with_captions:
        L += ["With the captions (concat lists `clip_NN.txt` are written):", "", "```", _shell(with_captions["command"]), "```", ""]
    else:
        L += ["After step 1, `python -m pcbtestkit.video .` writes the concat lists and runs ffmpeg. Without the "
              "captions, ffmpeg alone is enough:", ""]
    if plain:
        L += ["Without captions (plain 3D frames and stills, concat lists `plain_NN.txt` are written):", "", "```",
              _shell(plain["command"]), "```", ""]
    L += ["- Each animated scene is a concat list: one `file` line per frame shown for 1/fps of the scene, the last "
          "frame held for the scene's `hold`; a still is looped for its `duration`.",
          "- The clips are scaled to %dx%d, set to %g fps and joined with %.1f s cross-fades (`xfade`). An ffmpeg "
          "without `xfade` joins them with `concat` (see `without_xfade` in `video-recipe.json`)." % (size[0], size[1], fps, fade),
          "- No `libx264`? Use another H.264 encoder (`libopenh264`, `h264_videotoolbox`, `h264_mf`, `h264_vaapi`) or "
          "`mpeg4 -q:v 3`.", "",
          "## Scenes", "", "| # | Kind | Title | Caption | Frames | Seconds |", "|---|---|---|---|---|---|"]
    for sc in recipe["scenes"]:
        L.append("| %d | %s | %s | %s | %d | %.1f |" % (sc["scene"], sc["kind"], sc["title"].replace("|", "/"),
                                                      sc["caption"].replace("|", "/"), sc["frames"], sc["seconds"]))
    path = os.path.join(work, RECIPE_MD)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    return path


def main(argv: Optional[List[str]] = None) -> int:
    """python -m pcbtestkit.video <folder>: finish the explainer video from the frames PCB TestKit kept."""
    import argparse
    ap = argparse.ArgumentParser(prog="python -m pcbtestkit.video",
                                 description="Finish the explainer video from the frames PCB TestKit kept "
                                             "(<output>/visuals/scenes/video, see BUILD-VIDEO.md there).")
    ap.add_argument("folder", help="the folder with storyboard.json")
    ap.add_argument("-o", "--output", help="the MP4 to write (default: the one the storyboard names)")
    ap.add_argument("--python", help="a Python with wxPython for the captions (default: KiCad's bundled one)")
    ap.add_argument("--ffmpeg", help="the ffmpeg to use (default: FFMPEG, the PATH or the usual install folders)")
    ap.add_argument("--no-captions", action="store_true", help="join the plain frames (ffmpeg only, no wxPython)")
    a = ap.parse_args(argv)
    work = os.path.abspath(a.folder)
    if not os.path.exists(os.path.join(work, "storyboard.json")):
        print("no storyboard.json in %s" % work, file=sys.stderr)
        return 2
    res = finish_video(work, py=a.python, ff=a.ffmpeg, captions=not a.no_captions, out_path=a.output)
    if res.get("ok"):
        print("video written: %s (%.1f s, %d scenes, %s%s)" % (res["path"], res["duration_s"], res["scenes"], res["encoder"],
                                                              "" if res["captions"] else ", no captions"))
        return 0
    print("video not made: %s" % res["reason"], file=sys.stderr)
    if res.get("log"):
        print(res["log"], file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
