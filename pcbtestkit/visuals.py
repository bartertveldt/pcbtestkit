"""Photoreal visuals with KiCad's own 3D renderer (kicad-cli pcb render, KiCad 8+).

Scenes are ordinary .kicad_pcb files: a copy of the DUT or fixture board plus
"board only" footprints that carry extra 3D models (the solder jig mesh, the
DUT exported as VRML, pogo pins). Rendering them with kicad-cli gives:

  dut_iso / dut_top / dut_bottom      the board itself
  fixture_iso                          fixture PCB with pogo pins
  fixture_dut_exploded / _seated       DUT above / on the pins
  jig_iso                              the printed jig on its own
  jig_exploded / jig_assembled         parts in the jig, PCB above / seated
  jig2_print .. jig2_solder            two-jig workflow, one still per step (from the video's scenes)

The explainer video's motion shots are rendered by :mod:`pcbtestkit.animate` from the same scenes.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import kicad_writer as W
from .kicad_cli import QUIET, find_kicad_cli, sexpr_path

MM_PER_UNIT = 2.54          # KiCad reads VRML models in 0.1 inch units

Shape = Tuple[List[Tuple[float, float, float]], List[Tuple[int, ...]], Tuple[float, float, float]]


# --------------------------------------------------------------------------- #
# VRML
# --------------------------------------------------------------------------- #
def write_wrl(path: str, shapes: Sequence[Shape], shininess: float = 0.3) -> None:
    s = 1.0 / MM_PER_UNIT
    with open(path, "w", encoding="ascii") as fh:
        fh.write("#VRML V2.0 utf8\n")
        for verts, faces, col in shapes:
            fh.write("Shape { appearance Appearance { material Material { diffuseColor %.3f %.3f %.3f "
                     "specularColor 0.25 0.25 0.25 shininess %.2f } }\n" % (col[0], col[1], col[2], shininess))
            fh.write(" geometry IndexedFaceSet { solid FALSE creaseAngle 0.6 coord Coordinate { point [\n")
            fh.write(",\n".join("%.4f %.4f %.4f" % (x * s, y * s, z * s) for x, y, z in verts))
            fh.write(" ] }\n coordIndex [\n")
            fh.write(",\n".join(",".join(str(i) for i in f) + ",-1" for f in faces))
            fh.write(" ] } }\n")


def cylinder(cx: float, cy: float, z0: float, z1: float, r0: float, r1: Optional[float] = None, n: int = 20) -> Tuple[list, list]:
    r1 = r0 if r1 is None else r1
    verts, faces = [], []
    for i in range(n):
        a = 2 * math.pi * i / n
        verts.append((cx + r0 * math.cos(a), cy + r0 * math.sin(a), z0))
        verts.append((cx + r1 * math.cos(a), cy + r1 * math.sin(a), z1))
    for i in range(n):
        j = (i + 1) % n
        faces.append((2 * i, 2 * j, 2 * j + 1, 2 * i + 1))
    b = len(verts)
    verts += [(cx, cy, z0), (cx, cy, z1)]
    for i in range(n):
        j = (i + 1) % n
        faces.append((b, 2 * j, 2 * i))
        faces.append((b + 1, 2 * i + 1, 2 * j + 1))
    return verts, faces


GOLD, NICKEL, STEEL = (0.83, 0.64, 0.18), (0.78, 0.78, 0.80), (0.62, 0.64, 0.68)


def pogo_shapes(drill: float, height: float, tip: str = "spear") -> List[Shape]:
    """Receptacle + barrel + plunger + tip; z = 0 is the fixture's top surface, the tip ends at ``height``."""
    rr = drill / 2 * 0.98
    rb = rr * 0.72
    rp = rr * 0.42
    out: List[Shape] = []
    for (z0, z1, r0, r1, col) in ((-6.0, 0.9, rr, rr, GOLD),            # receptacle (press-fit sleeve)
                                  (0.9, 1.25, rr * 1.12, rr * 1.12, GOLD),  # collar
                                  (1.25, height * 0.55, rb, rb, NICKEL),     # barrel
                                  (height * 0.55, height - 0.6, rp, rp, STEEL)):  # plunger
        v, f = cylinder(0, 0, z0, z1, r0, r1)
        out.append((v, f, col))
    if tip == "crown":
        for k in range(4):
            a = math.pi / 2 * k
            v, f = cylinder(0.22 * rp * math.cos(a) * 3, 0.22 * rp * math.sin(a) * 3, height - 0.6, height, rp * 0.45, 0.05, 8)
            out.append((v, f, STEEL))
    else:
        v, f = cylinder(0, 0, height - 0.6, height, rp, 0.04)
        out.append((v, f, STEEL))
    return out


def read_stl(path: str) -> List[Tuple[Tuple[float, float, float], ...]]:
    with open(path, "rb") as fh:
        data = fh.read()
    n = struct.unpack("<I", data[80:84])[0]
    tris = []
    for i in range(n):
        rec = struct.unpack("<12fH", data[84 + 50 * i: 84 + 50 * (i + 1)])
        tris.append((tuple(rec[3:6]), tuple(rec[6:9]), tuple(rec[9:12])))
    return tris


def jig_scene_shapes(stl_path: str, origin_x: float, origin_y: float, support_mm: float, color=(0.95, 0.55, 0.12)) -> List[Shape]:
    """Jig mesh expressed in the DUT's 3D frame (PCB flipped onto the jig = 180 deg about X).

    Jig coords (X, Y, Z) -> board 3D model coords relative to a footprint at (origin_x, origin_y):
    x3 = X, y3 = -Y (board y down), z3 = support - Z (support plane touches the PCB top surface).
    """
    idx: Dict[Tuple[float, float, float], int] = {}
    verts: List[Tuple[float, float, float]] = []
    faces: List[Tuple[int, ...]] = []
    for tri in read_stl(stl_path):
        f = []
        for (x, y, z) in tri:
            p = (round(x, 4), round(-y, 4), round(support_mm - z, 4))
            if p not in idx:
                idx[p] = len(verts)
                verts.append(p)
            f.append(idx[p])
        faces.append(tuple(f))
    return [(verts, faces, color)]


# --------------------------------------------------------------------------- #
# Board text surgery (keeps the original file intact, only appends / hides)
# --------------------------------------------------------------------------- #
def _block_end(text: str, start: int) -> int:
    depth, i, n = 0, start, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def footprint_spans(text: str) -> List[Tuple[int, int, str]]:
    out = []
    for m in re.finditer(r"\(\s*(footprint|module)\s", text):
        s = m.start()
        e = _block_end(text, s)
        body = text[s:e]
        r = re.search(r'\(property\s+"?Reference"?\s+"([^"]*)"', body) or re.search(r'\(fp_text\s+reference\s+"?([^"\s)]*)', body)
        out.append((s, e, r.group(1) if r else ""))
    return out


def hide_models(text: str, refs: Sequence[str]) -> str:
    """Return board text with the 3D models of ``refs`` hidden."""
    want = set(refs)
    pieces, last = [], 0
    for s, e, ref in footprint_spans(text):
        if ref not in want:
            continue
        body = text[s:e]
        body = re.sub(r'(\(model\s+"[^"]*")', r'\1 (hide yes)', body)
        pieces.append(text[last:s])
        pieces.append(body)
        last = e
    pieces.append(text[last:])
    return "".join(pieces)


def append_items(text: str, items: List[list]) -> str:
    from .sexpr import dumps
    end = text.rstrip().rfind(")")
    add = "".join("\t" + dumps(it, 1).lstrip() + "\n" for it in items)
    return text[:end] + add + text[end:]


def model_footprint(ref: str, x: float, y: float, model_path: str, offset=(0.0, 0.0, 0.0), rotate=(0.0, 0.0, 0.0),
                    angle: float = 0.0, scale=(1.0, 1.0, 1.0)) -> list:
    fp = W.footprint("PCBTestKit:Scene", x, y, ref, "scene", [], angle=angle,
                     attr=("board_only", "exclude_from_pos_files", "exclude_from_bom"), hide_ref=True)
    fp.append(["model", model_path, ["offset", ["xyz", offset[0], offset[1], offset[2]]],
               ["scale", ["xyz", scale[0], scale[1], scale[2]]], ["rotate", ["xyz", rotate[0], rotate[1], rotate[2]]]])
    for it in fp:
        if isinstance(it, list) and it and it[0] == "property" and it[1] == "Value":
            it.append(["hide", W.Sym("yes")])
    return fp


# --------------------------------------------------------------------------- #
# Renderer
# --------------------------------------------------------------------------- #
# kicad-cli always renders with KiCad's ray tracer; its "basic" quality just turns shadows, ambient occlusion and
# anti-aliasing off, and "high" turns everything on (soft shadows, reflections, refractions: about 10x slower).
# These presets sit in between. They run kicad-cli with --quality user and a private settings folder
# (KICAD_CONFIG_HOME) holding a copy of your kicad_common.json (path variables) and these ray-tracing switches,
# so your own KiCad settings are never touched. Seconds per 1280 x 720 frame on an 8-core laptop, 8 frames at a
# time: basic 0.42, standard 0.77, best 1.6, high 4.7.
RAYTRACE_PRESETS: Dict[str, Dict[str, Any]] = {
    "standard": {"raytrace_shadows": True, "raytrace_nrsamples_shadows": 1, "raytrace_post_processing": True},
    "best": {"raytrace_shadows": True, "raytrace_nrsamples_shadows": 1, "raytrace_post_processing": True,
             "raytrace_anti_aliasing": True, "raytrace_procedural_textures": True},
}
_RAYTRACE_OFF = {"raytrace_shadows": False, "raytrace_backfloor": False, "raytrace_refractions": False,
                 "raytrace_reflections": False, "raytrace_post_processing": False, "raytrace_procedural_textures": False,
                 "raytrace_anti_aliasing": False}
QUALITIES = ("basic", "standard", "best", "high")


def kicad_user_config_dir(version: str) -> Optional[str]:
    """KiCad's own settings folder for ``version`` ("10.0"), where kicad_common.json and 3d_viewer.json live."""
    env = os.environ.get("KICAD_CONFIG_HOME")
    if env:
        cands = [os.path.join(env, version), env]
    elif sys.platform == "darwin":
        cands = [os.path.expanduser("~/Library/Preferences/kicad/%s" % version)]
    elif os.name == "nt":
        cands = [os.path.join(os.environ.get("APPDATA", ""), "kicad", version)]
    else:
        cands = [os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "kicad", version)]
    return next((c for c in cands if os.path.isdir(c)), None)


class Renderer:
    def __init__(self, out_dir: str, width: int = 1600, height: int = 1000, quality: str = "high"):
        self.cli = find_kicad_cli()
        self.out = out_dir
        self.w, self.h, self.quality = width, height, quality
        self.scene_dir = os.path.join(out_dir, "scenes")
        os.makedirs(self.scene_dir, exist_ok=True)
        self.log: List[str] = []
        # KiCad caches every 3D model it loads (by content). Scene meshes are throw-away, so they get a
        # private cache that is deleted with the scenes instead of filling the user's KiCad cache.
        self.env = dict(os.environ)
        self.env["KICAD_CACHE_HOME"] = os.path.join(self.scene_dir, "kicad_cache")
        self._preset_env: Dict[str, Dict[str, str]] = {}

    def env_for(self, quality: str) -> Dict[str, str]:
        """The environment kicad-cli runs in for ``quality``: a preset gets its private settings folder."""
        if quality not in RAYTRACE_PRESETS:
            return self.env
        if quality not in self._preset_env:
            from .kicad_cli import kicad_version
            m = re.search(r"(\d+)\.(\d+)", (kicad_version(self.cli) if self.cli else "") or "")
            ver = "%s.%s" % (m.group(1), m.group(2)) if m else "10.0"
            home = os.path.join(self.scene_dir, "kicad_config_" + quality)
            dst = os.path.join(home, ver)
            os.makedirs(dst, exist_ok=True)
            src = kicad_user_config_dir(ver)
            viewer: Dict[str, Any] = {}
            if src:
                if os.path.exists(os.path.join(src, "kicad_common.json")):      # path variables for 3D models
                    shutil.copy2(os.path.join(src, "kicad_common.json"), dst)
                try:
                    with open(os.path.join(src, "3d_viewer.json"), encoding="utf-8") as fh:
                        viewer = json.load(fh)
                except (OSError, ValueError):
                    viewer = {}
            render = dict(viewer.get("render") or {})
            render.update(_RAYTRACE_OFF)
            render.update(RAYTRACE_PRESETS[quality])
            viewer["render"] = render
            with open(os.path.join(dst, "3d_viewer.json"), "w", encoding="utf-8") as fh:
                json.dump(viewer, fh, indent=1)
            env = dict(self.env)
            env["KICAD_CONFIG_HOME"] = home
            self._preset_env[quality] = env
        return self._preset_env[quality]

    @property
    def available(self) -> bool:
        return bool(self.cli)

    def render(self, board: str, png: str, side: str = "top", rotate: Optional[str] = None, zoom: float = 1.0,
               w: Optional[int] = None, h: Optional[int] = None, floor: bool = False, quality: Optional[str] = None,
               perspective: bool = False, pan: Optional[str] = None) -> bool:
        if not self.cli:
            return False
        q = quality or self.quality
        args = [self.cli, "pcb", "render", "-o", png, "-w", str(w or self.w), "-h", str(h or self.h), "--side", side,
                "--quality", "user" if q in RAYTRACE_PRESETS else q, "--background", "opaque", "--zoom", "%g" % zoom]
        if rotate:
            args += ["--rotate", rotate]
        if floor:
            args.append("--floor")
        if perspective:
            args.append("--perspective")
        if pan:
            args += ["--pan", pan]
        args.append(board)
        try:
            r = subprocess.run(args, capture_output=True, timeout=600, env=self.env_for(q), **QUIET)
        except (OSError, subprocess.SubprocessError) as e:
            self.log.append("render failed: %s" % e)
            return False
        ok = r.returncode == 0 and os.path.exists(png)
        if not ok:
            self.log.append("render failed for %s: %s" % (os.path.basename(png), (r.stderr or r.stdout)[-300:]))
        return ok

    def export_vrml(self, board: str, wrl: str, origin: Tuple[float, float]) -> bool:
        if not self.cli:
            return False
        args = [self.cli, "pcb", "export", "vrml", "--units", "tenths", "-f", "--user-origin",
                "%.4fx%.4fmm" % origin, "-o", wrl, board]
        try:
            r = subprocess.run(args, capture_output=True, timeout=900, env=self.env, **QUIET)
            return r.returncode == 0 and os.path.exists(wrl)
        except (OSError, subprocess.SubprocessError):
            return False


ISO_TOP = "-40,0,25"         # tilt towards the viewer, then turn
ISO_SIDE = "-62,0,25"         # lower, closer to the side: shows what stands between two boards
ISO_BOTTOM = "-40,0,155"      # seen from the board's bottom side: the jig lies underneath


def build_visuals(design, plan: Dict[str, Any], fixture: Optional[Dict[str, Any]], jig: Optional[Dict[str, Any]],
                  paths: Dict[str, str], out_dir: str, size: Tuple[int, int] = (1600, 1000)) -> Dict[str, Any]:
    """Render all stills. Returns {"images": {name: png path}, "log", "available"}."""
    rd = Renderer(out_dir, size[0], size[1])
    res: Dict[str, Any] = {"images": {}, "log": rd.log, "available": rd.available}
    if not rd.available:
        rd.log.append("kicad-cli not found: 3D renders skipped")
        return res
    img = res["images"]
    b = design.board
    bb = b.outline_bbox()
    sd = rd.scene_dir

    def P(name):
        return os.path.join(out_dir, name + ".png")

    # ---- the DUT itself -------------------------------------------------------------------
    for name, side, rot in (("dut_iso", "top", ISO_TOP), ("dut_top", "top", None), ("dut_bottom", "bottom", None)):
        if rd.render(b.path, P(name), side=side, rotate=rot):
            img[name] = P(name)
    dut_wrl = os.path.join(sd, "dut.wrl")
    have_dut_wrl = rd.export_vrml(b.path, dut_wrl, (bb.cx, bb.cy))

    # ---- fixture ----------------------------------------------------------------------------
    if fixture and paths.get("fixture") and os.path.exists(paths["fixture"]):
        if rd.render(paths["fixture"], P("fixture_iso"), rotate=ISO_TOP):
            img["fixture_iso"] = P("fixture_iso")
        if have_dut_wrl:
            with open(paths["fixture"], "r", encoding="utf-8") as fh:
                fx_text = fh.read().replace("${KIPRJMOD}", sexpr_path(os.path.dirname(os.path.abspath(paths["fixture"]))))
            clear = float(plan["pogo"].get("probe_clearance_mm", 6.0))
            from .testplan import fixture_side
            mirror = fixture_side(plan) == "top"
            rotate = (0.0, 180.0, 0.0) if mirror else (0.0, 0.0, 0.0)

            def fixture_scene(lift: float, tag: str) -> str:
                items = []
                for s in fixture.get("sites", []):
                    dx, dy = s["offset"]
                    items.append(model_footprint("DUT%d" % s["site"], bb.cx + dx, bb.cy + dy, dut_wrl,
                                                 offset=(0.0, 0.0, clear + b.thickness / 2 + lift), rotate=rotate))
                path = os.path.join(sd, "fixture_scene_%s.kicad_pcb" % tag)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(append_items(fx_text, items))
                return path

            # exploded: the board well above the pins and a lower camera, so the pogo pins show between the boards
            high = max(30.0, min(60.0, 0.3 * max(bb.w, bb.h)))
            for name, lift, rot, zoom in (("fixture_dut_exploded", high, ISO_SIDE, 0.72), ("fixture_dut_seated", 0.0, ISO_TOP, 0.8)):
                if rd.render(fixture_scene(lift, name), P(name), rotate=rot, zoom=zoom):
                    img[name] = P(name)

    # ---- solder jig -----------------------------------------------------------------------------
    if jig and paths.get("jig_stl") and os.path.exists(paths["jig_stl"]):
        x0, y0, _, _ = jig["board_bbox_in_jig_mm"]
        ox, oy = bb.x0 - x0, bb.y0 - y0               # board coords of the jig frame origin
        support = float(jig["support_height_mm"])
        jig_wrl = os.path.join(sd, "jig_scene.wrl")
        write_wrl(jig_wrl, jig_scene_shapes(paths["jig_stl"], ox, oy, support))
        # jig alone, on a neutral plate
        jw, jl = jig["jig_size_mm"][0], jig["jig_size_mm"][1]
        plate = [W.gr_rect(ox - 2, oy - jl - 2, ox + jw + 2, oy + 2),
                 model_footprint("JIG", ox, oy, jig_wrl, offset=(0.0, 0.0, support), rotate=(180.0, 0.0, 0.0))]
        plate_path = os.path.join(sd, "jig_alone.kicad_pcb")
        with open(plate_path, "w", encoding="utf-8") as fh:
            fh.write(W.board([], plate, thickness=0.2))
        if rd.render(plate_path, P("jig_iso"), rotate=ISO_TOP, zoom=0.85):
            img["jig_iso"] = P("jig_iso")
        with open(b.path, "r", encoding="utf-8") as fh:
            dut_text = fh.read()
        parts = jig.get("parts_to_place", [])

        def jig_scene(gap: float, parts_in_jig: bool, tag: str) -> str:
            text = dut_text
            items = [model_footprint("JIG", ox, oy, jig_wrl, offset=(0.0, 0.0, gap))]
            if parts_in_jig and gap > 0:
                text = hide_models(text, parts)
                for ref in parts:
                    fp = b.by_ref.get(ref)
                    if fp is None:
                        continue
                    for m in fp.models:
                        if m.hide:
                            continue
                        items.append(model_footprint("P_" + ref, fp.x, fp.y, m.path,
                                                     offset=(m.offset[0], m.offset[1], m.offset[2] + gap),
                                                     rotate=m.rotate, angle=fp.angle, scale=m.scale))
            path = os.path.join(sd, "jig_scene_%s.kicad_pcb" % tag)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(append_items(text, items))
            return path

        gap = max(15.0, support * 1.1, 0.16 * max(bb.w, bb.h))
        for name, g, inj in (("jig_exploded", gap, True), ("jig_assembled", 0.0, False)):
            if rd.render(jig_scene(g, inj, name), P(name), side="bottom", rotate=ISO_BOTTOM, zoom=0.75):
                img[name] = P(name)
        # two-jig workflow: one still per step (print, fit the parts, holding jig on top, turned over, solder)
        if str(jig.get("workflow", "")) == "two_jig" and jig.get("placement"):
            try:
                from .animate import Stage, render_shots
                st = Stage(design, plan, fixture, jig, paths, rd, out_dir, size=size, quality=rd.quality)
                shot = st.step_stills()
                if shot is not None:
                    render_shots([shot], rd)
                    for f in shot.files:
                        img[os.path.splitext(os.path.basename(f))[0]] = f
                rd.log.extend(st.log)
            except Exception as exc:  # the step stills are optional
                rd.log.append("jig step renders failed: %r" % exc)
    return res


def clean_scenes(out_dir: str) -> None:
    shutil.rmtree(os.path.join(out_dir, "scenes"), ignore_errors=True)
