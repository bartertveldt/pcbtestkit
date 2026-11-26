"""The station video: one test station taking the fixture of one board type after another (KiCad 3D animation).

Frames are rendered like the board explainers (pcbtestkit.animate): every frame is a .kicad_pcb scene with the
station board as the stage, each fixture board and its batch of boards as 3D models (exported once as VRML) moved
by their 3D-model offsets. The captions and the MP4 come from pcbtestkit.video's caption and encode steps.

Shots: the station on its own (camera circling), then per board type: its fixture slides in and seats on the
station's connector, the batch of boards drops onto the pogo pins, a short hold, everything lifts away; last, a
card with every board type and its batch size.
"""
from __future__ import annotations

import json
import math
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import kicad_writer as W
from .animate import ISO, Shot, Xf, _TRANSPARENCY, ease_out, phase, place, render_shots, smooth, stagger
from .sexpr import dumps
from .visuals import Renderer

STANDOFF = 11.0          # fixture underside above the station's top surface (mating pin header + socket)


def _opaque(src: str, dst: str) -> str:
    """A copy of a KiCad VRML export with its slightly transparent solder mask made opaque (renders ~3x faster)."""
    with open(src, "r", encoding="latin-1") as fh:
        text = _TRANSPARENCY.sub("transparency 0", fh.read())
    with open(dst, "w", encoding="latin-1") as fh:
        fh.write(text)
    return dst


def station_board(fac: Dict[str, Any], path: str) -> str:
    """A stand-in station board for a station given as a facility (no .kicad_pcb of its own): its plate, its
    holes and its connector."""
    from .facility import connectors, pin_count, pin_xy
    fx = fac.get("fixture") or {}
    w, h = (fx.get("plate_mm") or [160.0, 120.0])[:2]
    items = [W.gr_rect(0.0, 0.0, float(w), float(h), "Edge.Cuts", 0.1)]
    for k, hole in enumerate(fx.get("holes") or [], 1):
        d = float(hole.get("d_mm", 3.2))
        items.append(W.footprint("MountingHole:MountingHole_%.1fmm" % d, float(hole["at_mm"][0]), float(hole["at_mm"][1]),
                                 "H%d" % k, "MOUNT", [W.pad("", "np_thru_hole", "circle", 0, 0, d, d, ["*.Cu", "*.Mask"], drill=d)],
                                 attr=("exclude_from_pos_files", "exclude_from_bom"), hide_ref=True))
    for k, c in enumerate(connectors(fac), 1):
        p = float(c.get("pitch_mm", 2.54))
        pads = [W.pad(str(n), "thru_hole", "oval", *pin_xy(c, n), 0.67 * p, 0.67 * p, ["*.Cu", "*.Mask"], drill=0.4 * p,
                      angle=float(c.get("angle", 0) or 0)) for n in range(1, pin_count(c) + 1)]
        items.append(W.footprint(c.get("footprint") or "PCBTestKit:StationConnector", float(c["at_mm"][0]), float(c["at_mm"][1]),
                                 "J%d" % k, c.get("name", "TESTER"), pads, angle=float(c.get("angle", 0) or 0),
                                 attr=("through_hole",), models=[c["model"]] if c.get("model") else ()))
    items.append(W.gr_text(fac.get("name", fac["id"]), float(w) / 2, 8.0, "F.SilkS", 2.5))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(W.board([], items, title=fac.get("name", fac["id"]), mask_color="Blue"))
    return path


class StationScene:
    """The station board as the stage plus fixtures and boards placed on it, in the station's board coordinates."""

    def __init__(self, st: Dict[str, Any], fac: Dict[str, Any], out_dir: str, work: str, size: Tuple[int, int], quality: str):
        from .pcb import load_board
        from .project import locate
        self.st, self.fac, self.out, self.work = st, fac, out_dir, work
        self.size, self.quality = size, quality
        self.rd = Renderer(work, size[0], size[1], quality=quality)
        sd = self.rd.scene_dir
        self.station = fac.get("_station_path") or station_board(fac, os.path.join(sd, "station_board.kicad_pcb"))
        sb = load_board(self.station)
        self.sbb = sb.outline_bbox()
        with open(self.station, "r", encoding="utf-8") as fh:
            text = fh.read().replace("${KIPRJMOD}", os.path.dirname(os.path.abspath(self.station)))
        end = text.rstrip().rfind(")")
        self.head, self.close = text[:end], text[end:]
        self.boards: List[Dict[str, Any]] = []
        for b in st["boards"]:
            if b.get("status") != "ok" or not (b.get("outputs") or {}).get("fixture"):
                continue
            fx_pcb = os.path.join(out_dir, b["outputs"]["fixture"])
            ol = (b.get("fixture") or {}).get("outline") or {}
            fx0, fy0 = float(ol.get("x_min", 0.0)), float(ol.get("y_min", 0.0))
            fcx, fcy = (fx0 + float(ol.get("x_max", 0.0))) / 2.0, (fy0 + float(ol.get("y_max", 0.0))) / 2.0
            tag = "fx_%d" % len(self.boards)
            raw = os.path.join(sd, tag + "_export.wrl")
            if not self.rd.export_vrml(fx_pcb, raw, (fcx, fcy)):
                continue
            fx_wrl = _opaque(raw, os.path.join(sd, tag + ".wrl"))
            pcb, _sch, _pro = locate(b["project"])
            db = load_board(pcb)
            dbb = db.outline_bbox()
            raw = os.path.join(sd, "dut_%d_export.wrl" % len(self.boards))
            dut_wrl = None
            if self.rd.export_vrml(pcb, raw, (dbb.cx, dbb.cy)):
                dut_wrl = _opaque(raw, os.path.join(sd, "dut_%d.wrl" % len(self.boards)))
            with open(os.path.join(out_dir, b["outputs"]["testplan"]), encoding="utf-8") as fh:
                tp = json.load(fh)
            clear = float((tp.get("pogo") or {}).get("probe_clearance_mm", 6.0))
            try:                                    # boards rest in their nests, above the pins (clamps open)
                with open(os.path.join(out_dir, b["outputs"]["fixture_meta"]), encoding="utf-8") as fh:
                    nest = (json.load(fh).get("nest") or {})
                if nest.get("enabled"):
                    clear += float(nest["heights"]["travel"])
            except (KeyError, OSError, ValueError):
                pass
            mirror = tp.get("probe_side") == "top"
            # fixture board coordinates -> station board coordinates (both plates start at their top-left corner)
            dx, dy = self.sbb.x0 - fx0, self.sbb.y0 - fy0
            duts = []
            for s in (b.get("fixture") or {}).get("sites") or [{"site": 1, "offset": [0.0, 0.0]}]:
                ox, oy = s["offset"]
                duts.append((dbb.cx + ox + dx, dbb.cy + oy + dy))
            self.boards.append({"row": b, "fx_wrl": fx_wrl, "fx_xy": (fcx + dx, fcy + dy), "dut_wrl": dut_wrl,
                                "duts": duts, "clear": clear, "mirror": mirror, "t_dut": float(db.thickness)})

    def text(self, items: List[list]) -> str:
        return self.head + "".join("\t" + dumps(it, 1).lstrip() + "\n" for it in items) + self.close

    def cam(self, z: float, zoom: float = 0.78, tilt: float = ISO, rise: float = 0.0) -> Dict[str, Any]:
        cam = {"side": "top", "rotate": "%.3f,0,%.3f" % (tilt, z), "zoom": zoom, "perspective": True,
               "w": self.size[0] + 32, "h": self.size[1] + 32, "quality": self.quality, "stackup_colors": True}
        if rise:
            cam["pan"] = "0,%.3f,0" % (-rise * math.sin(math.radians(abs(tilt))) / 10.0)
        return cam

    def items(self, bd: Dict[str, Any], slide: float, lift: float, dut_drop: List[Optional[float]]) -> List[list]:
        """The fixture of board type bd ``slide`` mm along x and ``lift`` mm above its seat; dut_drop: per site the
        height above its seat on the pins (None = not there yet)."""
        x, y = bd["fx_xy"]
        z_fix = STANDOFF + 0.8 + lift
        out = [place("FIXTURE", bd["fx_wrl"], Xf.T(x + slide, -y, z_fix))]
        if bd["dut_wrl"]:
            for k, ((px, py), h) in enumerate(zip(bd["duts"], dut_drop), 1):
                if h is None:
                    continue
                z = STANDOFF + 1.6 + bd["clear"] + bd["t_dut"] / 2 + lift + h
                pose = Xf.T(px + slide, -py, z)
                if bd["mirror"]:                       # probed from the top: the board lies face down on the pins
                    pose = pose * Xf(_ry(math.pi))
                out.append(place("DUT%d" % k, bd["dut_wrl"], pose))
        return out


def _ry(a: float):
    c, s = math.cos(a), math.sin(a)
    return [[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]]


def build_station_video(st: Dict[str, Any], fac: Dict[str, Any], out_dir: str, fps: float = 24.0,
                        size: Tuple[int, int] = (1280, 720), quality: str = "basic", workers: int = 0,
                        progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, Any]:
    """Render <out_dir>/station-compatibility.mp4. Returns {"ok", "path" | "reason", ...}."""
    from .video import _rel, finish_video
    work = os.path.join(out_dir, "station_video")
    os.makedirs(work, exist_ok=True)
    sc = StationScene(st, fac, out_dir, work, size, quality)
    if not sc.rd.available:
        return {"ok": False, "reason": "kicad-cli not found: the station video needs KiCad 9 or newer"}
    frames = os.path.join(work, "frames")
    os.makedirs(frames, exist_ok=True)

    def n_of(seconds: float) -> int:
        return max(2, int(round(seconds * fps)))

    shots: List[Shot] = []
    intro = Shot("intro", fps, hold=0.3)
    n = n_of(3.0)
    for i in range(n):
        t = i / float(n - 1)
        intro.add(os.path.join(frames, "intro_%04d.png" % i), sc.cam(-5 + 50 * smooth(t), zoom=0.85), scene=sc.station)
    shots.append(intro)
    span_x = sc.sbb.w + 60.0
    for j, bd in enumerate(sc.boards):
        sh = Shot("board%d" % j, fps, hold=0.2)
        n = n_of(4.6)
        nd = len(bd["duts"])
        for i in range(n):
            t = i / float(n - 1)
            come, seat = ease_out(phase(t, 0.0, 0.28)), smooth(phase(t, 0.28, 0.42))
            leave = smooth(phase(t, 0.8, 1.0))
            slide = -span_x * (1 - come) + span_x * leave
            lift = 30.0 * (1 - seat) + 25.0 * leave
            drops: List[Optional[float]] = []
            for k in range(nd):
                p = stagger(k, nd, phase(t, 0.44, 0.7), min(0.6, max(0.25, 1.5 / max(1, nd))))
                drops.append(None if p <= 0 else 35.0 * (1 - ease_out(p)))
            sh.add(os.path.join(frames, "board%d_%04d.png" % (j, i)), sc.cam(20 + 14 * smooth(t), zoom=0.8, rise=12.0),
                   make=lambda prefix, it=sc.items(bd, slide, lift, drops): (sc.text(it), {}))
        sh.meta = {"row": bd["row"]}
        shots.append(sh)
    stats = render_shots(shots, sc.rd, workers, progress)
    still = os.path.join(work, "station_still.png")
    sc.rd.render(sc.station, still, rotate="-40,0,25", zoom=0.85, perspective=True, quality=quality, stackup_colors=True)
    cap = st["capacity"]
    title = st["station"]["name"]
    scenes: List[Dict[str, Any]] = []
    for sh in shots:
        if not sh.files:
            continue
        base = {"kind": "anim", "images": [_rel(f, work) for f in sh.files], "fps": fps, "hold": sh.hold}
        if sh.name == "intro":
            scenes.append(dict(base, overlay="card", title="One test station, %d board types" % len(sc.boards),
                               subtitle="%s: %s inputs, %s switched supplies, %s - every fixture plugs into the same connector."
                               % (title, cap.get("inputs", "-"), cap.get("supplies", "-"),
                                  ", ".join("%s x%d" % (k.upper(), v) for k, v in sorted((cap.get("buses") or {}).items()))
                                  or "no buses")))
            continue
        b = sh.meta["row"]
        bus = ", ".join("%s x%d" % (k.upper(), v) for k, v in sorted((b.get("buses") or {}).items()))
        scenes.append(dict(base, step="", title="%s: %d board%s per fixture" % (b["name"], b["batch"], "" if b["batch"] == 1 else "s"),
                           caption="Uses %d of %s inputs, %d of %s supplies%s" % (
                               b["inputs_used"], b.get("inputs_total") or "-", b["supplies_used"], b.get("supplies_total") or "-",
                               (", " + bus) if bus else ""),
                           sub="Limited by the station's %s. Swap the fixture to test another board type." % (b.get("limit") or "plate")))
    lines = ["%s: %d per fixture (limited by %s)" % (b["name"], b["batch"], b.get("limit") or "-") for b in st["boards"]
             if b.get("status") == "ok"]
    lines += ["%s: %s" % (b["name"], b.get("reason") or b.get("status")) for b in st["boards"] if b.get("status") != "ok"]
    scenes.append({"kind": "card", "bg": _rel(still, work) if os.path.exists(still) else None, "duration": 5.0,
                   "title": "Swap the fixture, test another board", "bullets": lines[:8]})
    out_path = os.path.join(out_dir, "station-compatibility.mp4")
    job = {"size": list(size), "fps": fps, "fade": 0.6, "output": _rel(out_path, work), "frames_dir": ".", "scenes": scenes}
    with open(os.path.join(work, "storyboard.json"), "w", encoding="utf-8") as fh:
        json.dump(job, fh, sort_keys=True)
    if os.path.exists(os.path.join(work, "frames.json")):
        os.remove(os.path.join(work, "frames.json"))
    res = finish_video(work)
    res["frames"] = stats
    if res.get("ok"):
        import shutil
        shutil.rmtree(sc.rd.scene_dir, ignore_errors=True)
    return res
