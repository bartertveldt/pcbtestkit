"""The station video: one test station taking the fixture of one board type after another (KiCad 3D animation).

Frames are rendered like the board explainers (pcbtestkit.animate): every frame is a .kicad_pcb scene with the
station board as the stage, each fixture board and its batch of boards as 3D models (exported once as VRML) moved
by their 3D-model offsets. The captions and the MP4 come from pcbtestkit.video's caption and encode steps.

Shots: the station on its own (camera circling), then per board type: its fixture slides in and seats on the
station's connector, the batch of boards drops onto the pogo pins, a short hold, everything lifts away; last, a
card with every board type and its batch size. Each is kept as a clip, so boards added later are rendered on their
own and joined after the ones shown before.
"""
from __future__ import annotations

import json
import math
import os
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

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
            nest = self._nest(b, out_dir)
            src = _without_nests(fx_pcb) if nest else fx_pcb    # the nests and clamps move: drawn per frame
            try:
                ok = self.rd.export_vrml(src, raw, (fcx, fcy))
            finally:
                if src != fx_pcb and os.path.exists(src):
                    os.remove(src)
            if not ok:
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
                    nmeta = (json.load(fh).get("nest") or {})
                if nmeta.get("enabled"):
                    clear += float(nmeta["heights"]["travel"])
            except (KeyError, OSError, ValueError):
                pass
            mirror = tp.get("probe_side") == "top"
            # fixture board coordinates -> station board coordinates (both plates start at their top-left corner)
            dx, dy = self.sbb.x0 - fx0, self.sbb.y0 - fy0
            duts = []
            for s in (b.get("fixture") or {}).get("sites") or [{"site": 1, "offset": [0.0, 0.0]}]:
                ox, oy = s["offset"]
                duts.append((dbb.cx + ox + dx, dbb.cy + oy + dy))
            bd = {"row": b, "fx_wrl": fx_wrl, "fx_xy": (fcx + dx, fcy + dy), "dut_wrl": dut_wrl,
                  "duts": duts, "clear": clear, "mirror": mirror, "t_dut": float(db.thickness), "nest": nest,
                  "travel": float(nest["heights"]["travel"]) if nest else 0.0}
            if nest:
                from .nest import nest_shapes
                from .visuals import write_wrl
                bd["nest_wrl"] = os.path.join(sd, tag + "_nest.wrl")
                write_wrl(bd["nest_wrl"], nest_shapes(nest, 0.0))
                bd["clamps"] = {}
            self.boards.append(bd)

    def _nest(self, b: Dict[str, Any], out_dir: str) -> Optional[Dict[str, Any]]:
        """The board type's nest geometry (pcbtestkit.nest), or None when its fixture has no nests."""
        try:
            with open(os.path.join(out_dir, b["outputs"]["fixture_meta"]), encoding="utf-8") as fh:
                nmeta = json.load(fh).get("nest") or {}
            if not nmeta.get("enabled"):
                return None
            with open(os.path.join(out_dir, b["outputs"]["testplan"]), encoding="utf-8") as fh:
                tp = json.load(fh)
            from .nest import plan_nest
            from .station import _design
            ov: Dict[str, Any] = {"fixture": {"route": False}}
            entry = nmeta.get("entry") or b.get("entry")     # as the fixture has them (it may have turned them)
            if entry:
                ov["fixture"]["nest"] = {"entry": entry}
            n = plan_nest(_design(b["project"], self.fac, int(b["batch"]), ov), tp)
            return n if n.get("enabled") else None
        except Exception:  # the video still shows the fixture with its static nests
            return None

    def clamp_wrl(self, bd: Dict[str, Any], deg: float) -> Optional[str]:
        """The board type's clamp ``deg`` up (cached per 5 degrees)."""
        if not bd.get("nest") or bd["nest"].get("clamp") != "toggle":
            return None
        key = int(round(deg / 5.0))
        if key not in bd["clamps"]:
            from .nest import clamp_shapes
            from .visuals import write_wrl
            path = bd["nest_wrl"].replace("_nest.wrl", "_clamp_%02d.wrl" % key)
            write_wrl(path, clamp_shapes(bd["nest"], key * 5.0))
            bd["clamps"][key] = path
        return bd["clamps"][key]

    def text(self, items: List[list]) -> str:
        return self.head + "".join("\t" + dumps(it, 1).lstrip() + "\n" for it in items) + self.close

    def cam(self, z: float, zoom: float = 0.78, tilt: float = ISO, rise: float = 0.0) -> Dict[str, Any]:
        cam = {"side": "top", "rotate": "%.3f,0,%.3f" % (tilt, z), "zoom": zoom, "perspective": True,
               "w": self.size[0] + 32, "h": self.size[1] + 32, "quality": self.quality, "stackup_colors": True}
        if rise:
            cam["pan"] = "0,%.3f,0" % (-rise * math.sin(math.radians(abs(tilt))) / 10.0)
        return cam

    def items(self, bd: Dict[str, Any], slide: float, lift: float, dut_drop: List[Optional[float]],
              clamp: Optional[List[float]] = None) -> List[list]:
        """The fixture of board type bd ``slide`` mm along x and ``lift`` mm above its seat; dut_drop: per site the
        height above its seat in the nest (None = not there yet); clamp: per site 0 (open) .. 1 (closed: the nest
        and the board pressed down onto the pins)."""
        x, y = bd["fx_xy"]
        z_fix = STANDOFF + 0.8 + lift
        out = [place("FIXTURE", bd["fx_wrl"], Xf.T(x + slide, -y, z_fix))]
        clamp = clamp or [0.0] * len(bd["duts"])
        top = STANDOFF + 1.6 + lift                         # the fixture board's top face
        if bd.get("nest_wrl"):
            for k, ((px, py), c) in enumerate(zip(bd["duts"], clamp), 1):
                down = smooth(phase(c, 0.7, 1.0))           # the last part of the clamp's travel presses
                out.append(place("NEST%d" % k, bd["nest_wrl"], Xf.T(px + slide, -py, top - bd["travel"] * down)))
                cw = self.clamp_wrl(bd, 95.0 * (1.0 - smooth(c)))
                if cw:
                    out.append(place("CLAMP%d" % k, cw, Xf.T(px + slide, -py, top)))
        if bd["dut_wrl"]:
            for k, ((px, py), h) in enumerate(zip(bd["duts"], dut_drop), 1):
                if h is None:
                    continue
                c = clamp[k - 1] if k - 1 < len(clamp) else 0.0
                z = STANDOFF + 1.6 + bd["clear"] + bd["t_dut"] / 2 + lift + h - bd.get("travel", 0.0) * smooth(phase(c, 0.7, 1.0))
                pose = Xf.T(px + slide, -py, z)
                if bd["mirror"]:                       # probed from the top: the board lies face down on the pins
                    pose = pose * Xf(_ry(math.pi))
                out.append(place("DUT%d" % k, bd["dut_wrl"], pose))
        return out


def _without_nests(fx_pcb: str) -> str:
    """A copy of the fixture board (next to it, so its 3D models resolve) with the nests' and clamps' models hidden."""
    import re
    from .animate import Template
    from .visuals import footprint_spans
    with open(fx_pcb, "r", encoding="utf-8") as fh:
        text = fh.read()
    refs = [ref for s_, e_, ref in footprint_spans(text)
            if re.search(r'\(model\s+"[^"]*pcbtestkit-models/(nest_(?!post_)|clamp_)', text[s_:e_])]
    out = os.path.join(os.path.dirname(fx_pcb), ".pcbtk_video_fixture.kicad_pcb")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(Template(text, refs).text({r: (0.0, True) for r in refs}))
    return out


def _ry(a: float):
    c, s = math.cos(a), math.sin(a)
    return [[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]]


VIDEO_REV = 2            # bumped when the station video's look changes: its kept clips are made again
PARTS = "station_video_parts"


def _sha(*items: Any) -> str:
    import hashlib
    h = hashlib.sha1()
    for it in items:
        if isinstance(it, str) and os.path.isfile(it):
            with open(it, "rb") as fh:
                h.update(fh.read())
        else:
            h.update(json.dumps(it, sort_keys=True, default=str).encode())
    return h.hexdigest()[:16]


def _row_key(base: str, b: Dict[str, Any], out_dir: str) -> str:
    """What a board type's clip shows: the row's numbers, its fixture, nest and test plan files, the board itself."""
    from .project import locate
    outs = b.get("outputs") or {}
    files = [os.path.join(out_dir, outs[k]) for k in ("fixture", "fixture_meta", "testplan") if outs.get(k)]
    try:
        files.append(locate(b["project"])[0])
    except Exception:  # noqa: BLE001 - a board that moved: its clip is made again
        files.append(str(b.get("project")))
    sig = {k: b.get(k) for k in ("name", "batch", "entry", "cols", "limit", "inputs_used", "inputs_total", "supplies_used",
                                 "supplies_total", "buses")}
    return _sha(base, sig, *files)


def _clip_name(folder: str) -> str:
    import re
    return "board-%s-%s.mp4" % (re.sub(r"[^A-Za-z0-9_.-]+", "_", folder)[:40], _sha(folder)[:6])


def build_station_video(st: Dict[str, Any], fac: Dict[str, Any], out_dir: str, fps: float = 24.0,
                        size: Tuple[int, int] = (1280, 720), quality: str = "basic", workers: int = 0,
                        progress: Optional[Callable[[int, int], None]] = None,
                        max_boards: Optional[int] = None, prefer: Sequence[str] = ()) -> Dict[str, Any]:
    """Render <out_dir>/station-compatibility.mp4: the station, then every shown board type's fixture sliding onto
    it with its boards dropping into their nests, then a card with every board type and its batch.

    Every part is kept as a clip (<out_dir>/station_video_parts/, parts.json): a later build renders only the board
    types that are new or changed and joins the clips again, the board types it showed before first, so boards
    added to an environment are put after the ones already in its video. ``max_boards``: the first build shows
    that many board types, a later one adds at most that many of the boards new to it (``prefer``) and fills up to
    that many otherwise; a build without new boards keeps the video as it is. The card lists every board type.
    Returns {"ok", "path" | "reason", "clips": {"made", "kept"}, "shown", ...}."""
    from .video import _rel, encode_each, join_clips
    parts = os.path.join(out_dir, PARTS)
    os.makedirs(parts, exist_ok=True)
    man_path = os.path.join(parts, "parts.json")
    try:
        with open(man_path, encoding="utf-8") as fh:
            man = json.load(fh)
    except (OSError, ValueError):
        man = {}
    ok_rows = [b for b in st["boards"] if b.get("status") == "ok"]
    by = {(b.get("folder") or b["name"]): b for b in ok_rows if (b.get("outputs") or {}).get("fixture")}
    base = _sha(VIDEO_REV, list(size), fps, quality, fac.get("_station_path") or
                {k: v for k, v in fac.items() if not str(k).startswith("_")})
    old = {c["folder"]: c for c in man.get("boards") or [] if c.get("folder")}
    prev = [f for f in (c["folder"] for c in man.get("boards") or []) if f in by]
    pref = [f for f in prefer if f in by and f not in prev]
    rest = [f for f in by if f not in prev and f not in pref]
    if max_boards:                  # the boards just added (at most max_boards), then fill up to max_boards
        shown = prev + pref[:max_boards]
        shown += rest[:max(0, max_boards - len(shown))]
    else:
        shown = prev + pref + rest
    keys = {f: _row_key(base, by[f], out_dir) for f in shown}

    def kept(c: Optional[Dict[str, Any]], key: str) -> bool:
        return bool(c and c.get("key") == key and os.path.exists(os.path.join(parts, c.get("file", "-"))))
    title = st["station"]["name"]
    cap = st["capacity"]
    intro_title = "%s: every fixture plugs into the same connector" % title
    intro_key = _sha(base, intro_title, cap)
    todo = [f for f in shown if not kept(old.get(f), keys[f])]
    need_intro = not kept(man.get("intro"), intro_key)
    work = os.path.join(out_dir, "station_video")
    if os.path.isdir(work):
        import shutil
        shutil.rmtree(work, ignore_errors=True)        # frames of an unfinished run: this run's are made below
    os.makedirs(work, exist_ok=True)
    frames = os.path.join(work, "frames")
    os.makedirs(frames, exist_ok=True)
    sc = StationScene(dict(st, boards=[by[f] for f in todo]), fac, out_dir, work, size, quality)
    if not sc.rd.available:
        return {"ok": False, "reason": "kicad-cli not found: the station video needs KiCad 9 or newer"}

    def n_of(seconds: float) -> int:
        return max(2, int(round(seconds * fps)))

    shots: List[Shot] = []
    if need_intro:
        intro = Shot("intro", fps, hold=0.3)
        n = n_of(3.0)
        for i in range(n):
            t = i / float(n - 1)
            intro.add(os.path.join(frames, "intro_%04d.png" % i), sc.cam(-5 + 50 * smooth(t), zoom=0.85), scene=sc.station)
        shots.append(intro)
    span_x = sc.sbb.w + 60.0
    for j, bd in enumerate(sc.boards):
        sh = Shot("board%d" % j, fps, hold=0.2)
        nested = bool(bd.get("nest_wrl"))
        n = n_of(5.4 if nested else 4.6)
        nd = len(bd["duts"])
        spread = min(0.6, max(0.25, 1.5 / max(1, nd)))
        for i in range(n):
            t = i / float(n - 1)
            if nested:      # slide in, seat, boards into their nests, clamps close one by one, hold, open, away
                come, seat = ease_out(phase(t, 0.0, 0.2)), smooth(phase(t, 0.2, 0.3))
                leave = smooth(phase(t, 0.86, 1.0))
                drop_t, clamp_t, open_t = phase(t, 0.32, 0.52), phase(t, 0.52, 0.74), phase(t, 0.78, 0.86)
            else:
                come, seat = ease_out(phase(t, 0.0, 0.28)), smooth(phase(t, 0.28, 0.42))
                leave = smooth(phase(t, 0.8, 1.0))
                drop_t, clamp_t, open_t = phase(t, 0.44, 0.7), 0.0, 0.0
            slide = -span_x * (1 - come) + span_x * leave
            lift = 30.0 * (1 - seat) + 25.0 * leave
            drops: List[Optional[float]] = []
            clamps: List[float] = []
            for k in range(nd):
                p = stagger(k, nd, drop_t, spread)
                drops.append(None if p <= 0 else 35.0 * (1 - ease_out(p)))
                clamps.append(max(0.0, stagger(k, nd, clamp_t, spread) - (smooth(open_t) if open_t > 0 else 0.0)))
            sh.add(os.path.join(frames, "board%d_%04d.png" % (j, i)), sc.cam(20 + 14 * smooth(t), zoom=0.8, rise=12.0),
                   make=lambda prefix, it=sc.items(bd, slide, lift, drops, clamps): (sc.text(it), {}))
        sh.meta = {"row": bd["row"]}
        shots.append(sh)
    stats = render_shots(shots, sc.rd, workers, progress) if shots else {}
    still = os.path.join(parts, "station_still.png")
    if man.get("still_key") != base or not os.path.exists(still):
        sc.rd.render(sc.station, still, rotate="-40,0,25", zoom=0.85, perspective=True, quality=quality, stackup_colors=True)
    scenes: List[Dict[str, Any]] = []
    outs: List[str] = []
    made: Dict[str, str] = {}
    for sh in shots:
        if not sh.files:
            continue
        base_sc = {"kind": "anim", "images": [_rel(f, work) for f in sh.files], "fps": fps, "hold": sh.hold}
        if sh.name == "intro":
            scenes.append(dict(base_sc, overlay="card", title=intro_title,
                               subtitle="%s inputs, %s switched supplies, %s." % (
                                   cap.get("inputs", "-"), cap.get("supplies", "-"),
                                   ", ".join("%s x%d" % (k.upper(), v) for k, v in sorted((cap.get("buses") or {}).items()))
                                   or "no buses")))
            outs.append(os.path.join(parts, "intro.mp4"))
            made["intro"] = "intro.mp4"
            continue
        b = sh.meta["row"]
        f = b.get("folder") or b["name"]
        bus = ", ".join("%s x%d" % (k.upper(), v) for k, v in sorted((b.get("buses") or {}).items()))
        scenes.append(dict(base_sc, step="", title="%s: %d board%s per fixture" % (b["name"], b["batch"], "" if b["batch"] == 1 else "s"),
                           caption="Uses %d of %s inputs, %d of %s supplies%s" % (
                               b["inputs_used"], b.get("inputs_total") or "-", b["supplies_used"], b.get("supplies_total") or "-",
                               (", " + bus) if bus else ""),
                           sub="Limited by the station's %s. Swap the fixture to test another board type." % (b.get("limit") or "plate")))
        outs.append(os.path.join(parts, _clip_name(f)))
        made[f] = _clip_name(f)
    lines = ["%s: %d per fixture (limited by %s)" % (b["name"], b["batch"], b.get("limit") or "-") for b in ok_rows]
    lines += ["%s: %s" % (b["name"], b.get("reason") or b.get("status")) for b in st["boards"] if b.get("status") != "ok"]
    if len(lines) > 8:
        lines = lines[:7] + ["... and %d more board types: see station.html" % (len(lines) - 7)]
    scenes.append({"kind": "card", "bg": _rel(still, work) if os.path.exists(still) else None, "duration": 5.0,
                   "title": "%d board types: swap the fixture, test another board" % len(ok_rows), "bullets": lines})
    outs.append(os.path.join(parts, "end.mp4"))
    job = {"size": list(size), "fps": fps, "fade": 0.6, "output": "unused.mp4", "frames_dir": ".", "scenes": scenes}
    with open(os.path.join(work, "storyboard.json"), "w", encoding="utf-8") as fh:
        json.dump(job, fh, sort_keys=True)
    enc = encode_each(work, outs)
    if not enc.get("ok"):
        return dict(enc, frames=stats)
    durs = {os.path.basename(x["path"]): x["duration_s"] for x in enc["parts"]}
    clips = {"intro": dict(man.get("intro") or {}) if not need_intro else {"file": "intro.mp4", "key": intro_key,
                                                                          "duration_s": durs["intro.mp4"]}}
    boards = []
    for f in shown:
        if f in made:
            boards.append({"folder": f, "file": made[f], "key": keys[f], "duration_s": durs[made[f]]})
        elif kept(old.get(f), keys[f]):
            boards.append(dict(old[f]))
    seq = [clips["intro"]] + boards + [{"file": "end.mp4", "duration_s": durs["end.mp4"]}]
    out_path = os.path.join(out_dir, "station-compatibility.mp4")
    res = join_clips([os.path.join(parts, c["file"]) for c in seq], [c["duration_s"] for c in seq], out_path, size, fps)
    if not res.get("ok"):
        return dict(res, frames=stats)
    for c in man.get("boards") or []:                 # clips of board types no longer shown
        if c.get("folder") not in {b["folder"] for b in boards} and c.get("file"):
            try:
                os.remove(os.path.join(parts, c["file"]))
            except OSError:
                pass
    with open(man_path, "w", encoding="utf-8") as fh:
        json.dump({"version": VIDEO_REV, "base": base, "still_key": base, "intro": clips["intro"], "boards": boards},
                  fh, indent=1)
    import shutil
    shutil.rmtree(work, ignore_errors=True)            # the frames (hundreds of MB) go; the clips stay
    return dict(res, frames=stats, clips={"made": len(made), "kept": len(boards) + 1 - len(made)}, shown=len(boards),
                board_types=len(ok_rows))
