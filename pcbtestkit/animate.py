"""Animated 3D shots for the explainer video, rendered frame by frame with KiCad's own 3D renderer.

Each frame is an ordinary .kicad_pcb scene, written just before it is rendered and deleted afterwards,
so the board, parts, jig and fixture look exactly as they do in KiCad's 3D viewer. Motion comes from 3D-model
offsets and rotations, and kicad-cli is the only tool needed. Frames render in parallel.

Shots, in production order (two-jig workflow, the default):
  orbit   the finished board with the camera circling it (intro)
  place   bare PCB; the parts the jig does not hold drop onto their pads
  print2  both jigs being 3D-printed layer by layer
  place2  the PCB drops top side up into the placement jig, the hand-soldered parts drop into their holes
  cover   the holding jig comes down over them, pockets first
  flip    the whole stack turns over
  unload  the placement jig lifts off: every lead points up
  solder  a soldering iron works along the leads; solder joints appear
  lift2   the finished board lifts out and turns over
  pogo    the pogo pins drop into the fixture PCB
  press   the finished board slides over the fixture and lowers onto the pins
Single-jig workflow: print, load (parts into the jig), slide (PCB onto them) and lift instead of print2 .. lift2.

Two stages hold still while things move over them: a thin plate under the jig (jig shots) and the fixture
PCB (fixture shots). The moving PCB is the DUT exported once as VRML. The hand-soldered parts in the jig
are placed from the DUT's own 3D models through the same flip that turns the PCB over onto the jig.
"""
from __future__ import annotations

import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import kicad_writer as W
from .sexpr import dumps, fmt_num
from .visuals import Renderer, _block_end, cylinder, footprint_spans, hide_models, model_footprint, read_stl

ISO = -38.0                 # camera tilt (deg) used by all shots


# --------------------------------------------------------------------------- #
# timing
# --------------------------------------------------------------------------- #
def clamp(t: float) -> float:
    return 0.0 if t < 0 else 1.0 if t > 1 else t


def smooth(t: float) -> float:
    t = clamp(t)
    return t * t * (3 - 2 * t)


def ease_out(t: float) -> float:
    t = clamp(t)
    return 1 - (1 - t) ** 3


def phase(t: float, a: float, b: float) -> float:
    """Progress 0..1 of t within [a, b]."""
    return clamp((t - a) / (b - a)) if b > a else float(t >= a)


def stagger(i: int, n: int, t: float, span: float) -> float:
    """Progress of item i of n when the items start one after another and each takes ``span`` of the shot."""
    span = min(1.0, max(span, 1e-6))
    start = (1.0 - span) * (i / float(n - 1) if n > 1 else 0.0)
    return clamp((t - start) / span)


def drop(p: float) -> float:
    """Height factor 1 -> 0 for an item coming down into place: fast at first, settling gently."""
    return 1.0 - ease_out(p)


# --------------------------------------------------------------------------- #
# rotations in KiCad's 3D frame (y up); KiCad applies model rotations X, then Y, then Z, with negated angles
# --------------------------------------------------------------------------- #
def _rx(a):
    c, s = math.cos(a), math.sin(a)
    return [[1, 0, 0], [0, c, -s], [0, s, c]]


def _ry(a):
    c, s = math.cos(a), math.sin(a)
    return [[c, 0, s], [0, 1, 0], [-s, 0, c]]


def _rz(a):
    c, s = math.cos(a), math.sin(a)
    return [[c, -s, 0], [s, c, 0], [0, 0, 1]]


def _mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def kicad_rotation(rot: Sequence[float]):
    rx, ry, rz = (math.radians(-v) for v in rot)
    return _mul(_rz(rz), _mul(_ry(ry), _rx(rx)))


def kicad_angles(m) -> Tuple[float, float, float]:
    """Inverse of :func:`kicad_rotation`: the (x, y, z) model rotation KiCad needs for matrix m."""
    sy = -m[2][0]
    if abs(sy) < 0.999999:
        b = math.asin(max(-1.0, min(1.0, sy)))
        a = math.atan2(m[2][1], m[2][2])
        g = math.atan2(m[1][0], m[0][0])
    else:                                   # gimbal lock: put everything into x
        b = math.copysign(math.pi / 2, sy)
        a = math.atan2(-m[1][2], m[1][1])
        g = 0.0
    return tuple(round(-math.degrees(v), 6) + 0.0 for v in (a, b, g))  # type: ignore[return-value]


FLIP_X = _rx(math.pi)


class Xf:
    """Rigid transform (rotation R, translation t) in KiCad's 3D frame (mm, y up, z up from the stage)."""

    __slots__ = ("R", "t")

    def __init__(self, R=None, t=(0.0, 0.0, 0.0)):
        self.R = R or [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        self.t = (float(t[0]), float(t[1]), float(t[2]))

    def __mul__(self, o: "Xf") -> "Xf":                     # self after o
        return Xf(_mul(self.R, o.R), tuple(sum(self.R[i][k] * o.t[k] for k in range(3)) + self.t[i] for i in range(3)))

    def apply(self, p) -> Tuple[float, float, float]:
        return tuple(sum(self.R[i][k] * p[k] for k in range(3)) + self.t[i] for i in range(3))  # type: ignore[return-value]

    def inv(self) -> "Xf":
        Rt = [[self.R[j][i] for j in range(3)] for i in range(3)]
        return Xf(Rt, tuple(-sum(Rt[i][k] * self.t[k] for k in range(3)) for i in range(3)))

    @staticmethod
    def T(x: float, y: float, z: float) -> "Xf":
        return Xf(None, (x, y, z))

    @staticmethod
    def Rx(a: float) -> "Xf":
        return Xf(_rx(a))

    @staticmethod
    def Rz(a: float) -> "Xf":
        return Xf(_rz(a))


def place(ref: str, path: str, xf: Xf, scale=(1.0, 1.0, 1.0)) -> list:
    """A scene footprint that puts a 3D model at world pose xf (KiCad applies scale, then rotation, then offset)."""
    return model_footprint(ref, xf.t[0], -xf.t[1], path, offset=(0.0, 0.0, xf.t[2]), rotate=kicad_angles(xf.R), scale=scale)
_ZVAL = 98765.4321          # placeholder z offset in cached footprint text
_ZMARK = "@Z@"


# --------------------------------------------------------------------------- #
# board text templates
# --------------------------------------------------------------------------- #
_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_OFFSET = re.compile(r"\((offset|at)\s+\(xyz\s+(%s)\s+(%s)\s+(%s)\s*\)\s*\)" % (_NUM, _NUM, _NUM))
_TRANSPARENCY = re.compile(r"transparency\s+%s" % _NUM)
_MODEL_HEAD = re.compile(r'(\(model\s+(?:"(?:[^"\\]|\\.)*"|[^\s()]+))')


def _edit_model(block: str, dz: float, hide: bool) -> str:
    if dz:
        m = _OFFSET.search(block)
        if m:
            unit = 25.4 if m.group(1) == "at" else 1.0          # KiCad 5 files store the offset in inches
            z = float(m.group(4)) + dz / unit
            block = block[:m.start()] + "(%s (xyz %s %s %.5f))" % (m.group(1), m.group(2), m.group(3), z) + block[m.end():]
        else:
            block = _MODEL_HEAD.sub(lambda h: h.group(1) + " (offset (xyz 0 0 %.5f))" % dz, block, count=1)
    if hide:
        block = _MODEL_HEAD.sub(lambda h: h.group(1) + " (hide yes)", block, count=1)
    return block


class Template:
    """Board text split so the 3D models of chosen footprints can be lifted or hidden per frame cheaply."""

    def __init__(self, text: str, refs: Optional[Sequence[str]] = None):
        want = None if refs is None else set(refs)
        self.chunks: List[Any] = []
        last = 0
        for s, e, ref in footprint_spans(text):
            if want is not None and ref not in want:
                continue
            for m in re.finditer(r"\(model\s", text[s:e]):
                ms = s + m.start()
                if ms < last:
                    continue
                me = _block_end(text, ms)
                self.chunks.append(text[last:ms])
                self.chunks.append((ref, text[ms:me]))
                last = me
        tail = text[last:]
        end = tail.rstrip().rfind(")")
        self.chunks.append(tail[:end])
        self.close = tail[end:]
        self.refs = sorted({c[0] for c in self.chunks if isinstance(c, tuple)})

    def text(self, edits: Optional[Dict[str, Tuple[float, bool]]] = None, items: Sequence[list] = ()) -> str:
        """edits: {ref: (dz_mm, hidden)}; items: extra footprints appended to the board."""
        edits = edits or {}
        out = []
        for c in self.chunks:
            if isinstance(c, tuple):
                e = edits.get(c[0])
                out.append(_edit_model(c[1], e[0], e[1]) if e else c[1])
            else:
                out.append(c)
        out.append("".join("\t" + dumps(it, 1).lstrip() + "\n" for it in items))
        out.append(self.close)
        return "".join(out)


# --------------------------------------------------------------------------- #
# VRML meshes written per frame
# --------------------------------------------------------------------------- #
_S = 1.0 / 2.54             # KiCad reads VRML models in 0.1 inch units


def _shape_wrl(verts, faces_txt: str, col, shininess=0.3) -> str:
    pts = ",\n".join("%.4f %.4f %.4f" % (x * _S, y * _S, z * _S) for x, y, z in verts)
    return ("Shape { appearance Appearance { material Material { diffuseColor %.3f %.3f %.3f "
            "specularColor 0.25 0.25 0.25 shininess %.2f } }\n geometry IndexedFaceSet { solid FALSE creaseAngle 0.6 "
            "coord Coordinate { point [\n%s ] }\n coordIndex [\n%s ] } }\n" % (col[0], col[1], col[2], shininess, pts, faces_txt))


def _faces_txt(faces) -> str:
    return ",\n".join(",".join(str(i) for i in f) + ",-1" for f in faces)


class JigMesh:
    """The jig STL in its print orientation (jig X, Y, Z), cut off at any print height."""

    COLOR = (0.95, 0.55, 0.12)

    @classmethod
    def from_grid(cls, grid: Dict[str, Any]) -> "JigMesh":
        """Light render mesh straight from a jig heightmap (see jig.render_mesh), much faster to raytrace."""
        from .jig import render_mesh
        verts, faces = render_mesh(grid["H"], grid["nx"], grid["ny"], grid["res"])
        m = cls.__new__(cls)
        m.verts = verts
        m.faces = _faces_txt(faces)
        m.zmax = max(v[2] for v in verts) if verts else 0.0
        xs, ys = [v[0] for v in verts], [v[1] for v in verts]
        m.bbox = (min(xs), min(ys), max(xs), max(ys)) if xs else (0, 0, 0, 0)
        m._full = None
        return m

    def __init__(self, stl_path: str):
        idx: Dict[Tuple[float, float, float], int] = {}
        self.verts: List[Tuple[float, float, float]] = []
        faces = []
        for tri in read_stl(stl_path):
            f = []
            for p in tri:
                q = (round(p[0], 4), round(p[1], 4), round(p[2], 4))
                if q not in idx:
                    idx[q] = len(self.verts)
                    self.verts.append(q)
                f.append(idx[q])
            faces.append(f)
        self.faces = _faces_txt(faces)
        xs = [v[0] for v in self.verts]
        ys = [v[1] for v in self.verts]
        self.zmax = max(v[2] for v in self.verts) if self.verts else 0.0
        self.bbox = (min(xs), min(ys), max(xs), max(ys)) if xs else (0, 0, 0, 0)
        self._full: Optional[str] = None

    def wrl(self, zcap: Optional[float] = None, extra: Sequence[Tuple[list, list, tuple]] = ()) -> str:
        """VRML text. The jig is a heightmap (pockets cut from the top), so capping every vertex at ``zcap``
        gives exactly the part printed so far: top faces above the cap collapse into the current layer."""
        if zcap is None and not extra and self._full is not None:
            return self._full
        verts = self.verts if zcap is None else [(x, y, z if z < zcap else zcap) for x, y, z in self.verts]
        out = ["#VRML V2.0 utf8\n", _shape_wrl(verts, self.faces, self.COLOR)]
        for v, f, col in extra:
            out.append(_shape_wrl(v, _faces_txt(f), col, 0.6))
        txt = "".join(out)
        if zcap is None and not extra:
            self._full = txt
        return txt


def _box(x0, y0, z0, x1, y1, z1):
    v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    f = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    return v, f


def hotend(x: float, y: float, z: float) -> List[Tuple[list, list, tuple]]:
    """A small print head whose nozzle tip touches (x, y, z)."""
    out = []
    v, f = cylinder(x, y, z, z + 2.0, 0.3, 1.2, 16)
    out.append((v, f, (0.86, 0.64, 0.22)))             # brass nozzle
    v, f = _box(x - 5, y - 4, z + 2.0, x + 5, y + 6, z + 8.0)
    out.append((v, f, (0.82, 0.83, 0.86)))             # aluminium heater block
    v, f = cylinder(x, y + 1, z + 8.0, z + 14.0, 1.6, 1.6, 16)
    out.append((v, f, (0.7, 0.72, 0.75)))              # heat break
    v, f = _box(x - 8, y - 7, z + 14.0, x + 8, y + 9, z + 28.0)
    out.append((v, f, (0.42, 0.45, 0.5)))              # fan shroud
    return out


SOLDER = (0.80, 0.81, 0.84)


def joints_wrl(pads: Sequence[Tuple[float, float, float]], t: float) -> str:
    """Solder joints on the PCB's bottom side, in the PCB frame D (bottom surface at z = -t): a shiny cone
    around each lead, from the pad (radius r) to the lead."""
    verts: List[Tuple[float, float, float]] = []
    faces: List[Tuple[int, ...]] = []
    for x, y, r in pads:
        v, f = cylinder(x, y, -t, -t - 0.95, r, 0.38, 14)
        b = len(verts)
        verts += v
        faces += [tuple(i + b for i in fc) for fc in f]
    if not verts:
        return "#VRML V2.0 utf8\n"
    return "#VRML V2.0 utf8\n" + _shape_wrl(verts, _faces_txt(faces), SOLDER, 0.9)


def iron_wrl(tilt_deg: float = 38.0, yaw_deg: float = 35.0) -> str:
    """A soldering iron with its tip at the origin, leaning back from the vertical."""
    tl, yw = math.radians(tilt_deg), math.radians(yaw_deg)
    rot = _mul(_rz(yw), _ry(tl))
    out = ["#VRML V2.0 utf8\n"]
    for z0, z1, r0, r1, col, sh in ((0.0, 6.0, 0.28, 1.3, (0.75, 0.47, 0.22), 0.7),   # copper tip
                                    (6.0, 32.0, 1.3, 1.3, (0.76, 0.77, 0.80), 0.8),   # steel shaft
                                    (32.0, 37.0, 3.4, 3.4, (0.25, 0.26, 0.28), 0.3),  # collar
                                    (37.0, 96.0, 5.0, 5.4, (0.13, 0.32, 0.62), 0.3),  # grip
                                    (96.0, 118.0, 1.6, 1.6, (0.08, 0.08, 0.09), 0.2)):  # cable
        v, f = cylinder(0.0, 0.0, z0, z1, r0, r1, 18)
        v = [tuple(sum(rot[i][k] * p[k] for k in range(3)) for i in range(3)) for p in v]
        out.append(_shape_wrl(v, _faces_txt(f), col, sh))
    return "".join(out)


# --------------------------------------------------------------------------- #
# frames and rendering
# --------------------------------------------------------------------------- #
class Shot:
    def __init__(self, name: str, fps: float, hold: float = 1.0):
        self.name, self.fps, self.hold = name, fps, hold
        self.jobs: List[Dict[str, Any]] = []
        self.files: List[str] = []
        self.meta: Dict[str, Any] = {}

    def add(self, png: str, cam: Dict[str, Any], scene: Optional[str] = None,
            make: Optional[Callable[[str], Tuple[str, Dict[str, str]]]] = None) -> None:
        self.jobs.append({"png": png, "cam": cam, "scene": scene, "make": make})


def _render_job(rd: Renderer, job: Dict[str, Any], tmp: str, key: str) -> bool:
    scene, temp = job.get("scene"), []
    try:
        if scene is None:
            prefix = os.path.join(tmp, key)
            text, files = job["make"](prefix)
            for p, content in files.items():
                with open(p, "w", encoding="ascii", errors="replace") as fh:
                    fh.write(content)
                temp.append(p)
            scene = prefix + ".kicad_pcb"
            with open(scene, "w", encoding="utf-8") as fh:
                fh.write(text)
            temp += [scene, prefix + ".kicad_prl", prefix + ".kicad_pro"]
        return rd.render(scene, job["png"], **job["cam"])
    finally:
        for p in temp:
            try:
                os.remove(p)
            except OSError:
                pass


def render_shots(shots: Sequence[Shot], rd: Renderer, workers: int = 0,
                 progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, Any]:
    jobs = [(s, j) for s in shots for j in s.jobs]
    total = len(jobs)
    workers = workers or max(2, min(8, (os.cpu_count() or 4) - 2))
    tmp = os.path.join(rd.scene_dir, "anim_tmp")
    os.makedirs(tmp, exist_ok=True)
    t0 = time.time()
    results = [False] * total
    # Workers only run kicad-cli; progress is reported from this (the caller's) thread, so a GUI progress
    # dialog can be updated safely.
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_render_job, rd, job, tmp, "f%05d" % i): i for i, (shot, job) in enumerate(jobs)}
        for k, fut in enumerate(as_completed(futs)):
            try:
                results[futs[fut]] = bool(fut.result())
            except Exception as exc:  # a failed frame is dropped, the shot still plays
                rd.log.append("animation: frame failed (%r)" % exc)
            if progress:
                progress(k + 1, total)
    for (shot, job), ok in zip(jobs, results):
        if ok:
            shot.files.append(job["png"])
    try:
        os.rmdir(tmp)
    except OSError:
        pass
    return {"frames": total, "rendered": sum(results), "seconds": round(time.time() - t0, 1), "workers": workers}


# --------------------------------------------------------------------------- #
# the stage: everything the shots share
# --------------------------------------------------------------------------- #
class Stage:
    def __init__(self, design, plan: Dict[str, Any], fixture: Optional[Dict[str, Any]], jig: Optional[Dict[str, Any]],
                 paths: Dict[str, str], rd: Renderer, out_dir: str, fps: float = 30.0, size: Tuple[int, int] = (1280, 720),
                 quality: str = "basic"):
        self.design, self.plan, self.fixture, self.jig, self.paths, self.rd = design, plan, fixture, jig, paths, rd
        self.out = out_dir
        os.makedirs(out_dir, exist_ok=True)
        self.fps, self.size, self.quality = float(fps), size, quality
        b = design.board
        self.b, self.bb, self.t = b, b.outline_bbox(), float(b.thickness)
        self.proj = os.path.dirname(os.path.abspath(b.path))
        with open(b.path, "r", encoding="utf-8") as fh:
            self.dut_text = fh.read().replace("${KIPRJMOD}", self.proj)
        self.big = max(self.bb.w, self.bb.h)
        self.log: List[str] = []
        parts = (jig or {}).get("parts_to_place") or []
        self.jig_parts = [r for r in parts if r in b.by_ref and b.by_ref[r].side == "top" and self._models(b.by_ref[r])]
        self._wrl: Dict[str, Optional[str]] = {}

    # -- helpers ---------------------------------------------------------------
    @staticmethod
    def _models(fp) -> list:
        return [m for m in fp.models if not m.hide]

    def cam(self, z: float, zoom: float = 0.9, tilt: float = ISO, side: str = "top", rise: float = 0.0) -> Dict[str, Any]:
        """Camera for one frame. ``rise`` (mm) moves the view up to follow something above the stage; kicad-cli's
        pan is in screen space and roughly centimetres, and a height h shows as about h * sin(tilt) on screen."""
        cam = {"side": side, "rotate": "%.3f,0,%.3f" % (tilt, z), "zoom": zoom, "perspective": True,
                "w": self.size[0] + 32, "h": self.size[1] + 32, "quality": self.quality}   # kicad-cli trims a few px
        if rise:
            cam["pan"] = "0,%.3f,0" % (-rise * math.sin(math.radians(abs(tilt))) / 10.0)
        return cam

    def n(self, seconds: float) -> int:
        return max(2, int(round(seconds * self.fps)))

    def png(self, shot: str, i: int) -> str:
        return os.path.join(self.out, "%s_%04d.png" % (shot, i))

    def dut_wrl(self, without: Sequence[str] = ()) -> Optional[str]:
        """The DUT exported as VRML (origin = board centre, z = 0 at mid-thickness), optionally without some parts."""
        key = ",".join(sorted(without))
        if key in self._wrl:
            return self._wrl[key]
        name = "anim_dut_bare" if without else "anim_dut"
        wrl = os.path.join(self.rd.scene_dir, name + ".wrl")
        done = os.path.join(self.rd.scene_dir, "dut.wrl")          # written by visuals.build_visuals
        if without or not os.path.exists(done):
            src = os.path.join(self.rd.scene_dir, name + ".kicad_pcb")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(hide_models(self.dut_text, without) if without else self.dut_text)
            done = os.path.join(self.rd.scene_dir, name + "_export.wrl")
            if not self.rd.export_vrml(src, done, (self.bb.cx, self.bb.cy)):
                self.log.append("animation: VRML export of the board failed")
                self._wrl[key] = None
                return None
        # KiCad exports the solder mask slightly transparent; the raytracer then follows every ray through it,
        # which makes a frame about 3x slower. An opaque mask renders fast and reads as a real green board.
        with open(done, "r", encoding="latin-1") as fh:
            text = _TRANSPARENCY.sub("transparency 0", fh.read())
        with open(wrl, "w", encoding="latin-1") as fh:
            fh.write(text)
        self._wrl[key] = wrl
        return wrl

    # -- the jig stage -------------------------------------------------------------
    def jig_stage(self):
        """(plate board text, jig origin on the plate, jig mesh). Jig point (X, Y, Z) -> plate (ox + X, oy - Y), height Z."""
        if getattr(self, "_jig_stage", None) is not None:
            return self._jig_stage
        jig = self.jig
        mesh = self._render_meshes()[0]
        x0, y0, _, _ = jig["board_bbox_in_jig_mm"]
        ox, oy = self.bb.x0 - x0, self.bb.y0 - y0
        jx0, jy0, jx1, jy1 = mesh.bbox
        # the plate sits just inside the jig footprint, so it frames the camera without being seen
        plate = [W.gr_rect(ox + jx0 + 1.0, oy - jy1 + 1.0, ox + jx1 - 1.0, oy - jy0 - 1.0)]
        text = W.board([], plate, thickness=0.2)
        end = text.rstrip().rfind(")")
        self._jig_wrl = os.path.join(self.rd.scene_dir, "anim_jig.wrl")
        with open(self._jig_wrl, "w", encoding="ascii") as fh:
            fh.write(mesh.wrl())
        self._jig_stage = (text[:end], text[end:], ox, oy, mesh)
        return self._jig_stage

    def jig_scene(self, prefix: str, zcap: Optional[float] = None, nozzle: Optional[Tuple[float, float]] = None,
                  parts_lift: Optional[Dict[str, float]] = None, dut: Optional[Dict[str, Any]] = None):
        head, close, ox, oy, mesh = self.jig_stage()
        files: Dict[str, str] = {}
        if zcap is None and nozzle is None:
            wrl = self._jig_wrl                         # the finished jig: one file for every frame
        else:
            extra = hotend(nozzle[0], nozzle[1], zcap or 0.0) if nozzle is not None else ()
            wrl = prefix + "_jig.wrl"
            files[wrl] = mesh.wrl(zcap, extra)
        items = [model_footprint("JIG", ox, oy, wrl)]
        support = float(self.jig["support_height_mm"])
        if dut:
            items.append(model_footprint("DUT", self.bb.cx + dut.get("dx", 0.0), 2 * oy - self.bb.cy, dut["wrl"],
                                         offset=(0.0, 0.0, support + self.t / 2 + dut.get("lift", 0.0)),
                                         rotate=(dut.get("rx", 180.0), 0.0, 0.0)))
        body = ["\t" + dumps(it, 1).lstrip() + "\n" for it in items]
        for ref, lift in (parts_lift or {}).items():
            body += [t.replace(_ZMARK, "%.4f" % (z0 + support + lift)) for t, z0 in self.part_in_jig(ref, oy)]
        return head + "".join(body) + close, files

    def part_in_jig(self, ref: str, oy: float) -> List[Tuple[str, float]]:
        """The part's own 3D models, turned over with the PCB (180 deg about X) so they sit in the jig at height z.

        On the DUT a model point p goes to Rz(a) (o + R p) + (x, -y, 0). Turning the PCB over onto the jig maps
        that to Rz(-a) (Fo + F R p) + (x, y, 0): a footprint at (x, 2*oy - y), angle -a, model rotation F R and
        offset F o, where F = 180 deg about X.
        """
        cache = self.__dict__.setdefault("_part_txt", {})
        if ref in cache:
            return cache[ref]
        fp = self.b.by_ref[ref]
        out = []
        for k, m in enumerate(self._models(fp)):
            rot = kicad_angles(_mul(FLIP_X, kicad_rotation(m.rotate)))
            it = model_footprint("P_%s_%d" % (ref, k), fp.x, 2 * oy - fp.y, m.path.replace("${KIPRJMOD}", self.proj),
                                 offset=(m.offset[0], -m.offset[1], _ZVAL), rotate=rot, angle=-fp.angle, scale=m.scale)
            txt = "\t" + dumps(it, 1).lstrip() + "\n"
            out.append((txt.replace(fmt_num(_ZVAL), _ZMARK), -m.offset[2]))
        cache[ref] = out
        return out

    # ------------------------------------------------------------------------------------------- #
    # shots
    # ------------------------------------------------------------------------------------------- #
    def shot_orbit(self, seconds: float = 3.5) -> Shot:
        s = Shot("orbit", self.fps, hold=0.2)
        n = self.n(seconds)
        for i in range(n):
            t = i / float(n - 1)
            s.add(self.png("orbit", i), self.cam(-5 + 60 * smooth(t), zoom=0.95, tilt=ISO - 6 * math.sin(math.pi * t)), scene=self.b.path)
        return s

    def machine_parts(self) -> List[Any]:
        jp = set(self.jig_parts)
        fps = [fp for fp in self.b.footprints if fp.ref and fp.side == "top" and fp.ref not in jp and self._models(fp)]
        fps.sort(key=lambda fp: (round((fp.y - self.bb.y0) / max(8.0, self.bb.h / 6)), fp.x))
        return fps

    def shot_place(self) -> Optional[Shot]:
        parts = self.machine_parts()
        if not parts:
            return None
        tpl = Template(self.dut_text, [fp.ref for fp in parts] + self.jig_parts)
        s = Shot("place", self.fps, hold=0.8)
        s.meta = {"parts": [fp.ref for fp in parts], "smd": sum(1 for fp in parts if "smd" in fp.attrs)}
        n = self.n(min(5.0, 2.5 + 0.035 * len(parts)))
        hmax = max(15.0, min(60.0, 0.35 * self.big))
        span = min(0.45, max(0.12, 6.0 / max(1, len(parts))))
        hidden = {r: (0.0, True) for r in self.jig_parts}
        for i in range(n):
            t = i / float(n - 1)
            edits = dict(hidden)
            for k, fp in enumerate(parts):
                p = stagger(k, len(parts), phase(t, 0.0, 0.92), span)
                edits[fp.ref] = (hmax * drop(p), True) if p <= 0 else (hmax * drop(p), False)
            s.add(self.png("place", i), self.cam(15 + 14 * smooth(t), zoom=0.95),
                  make=lambda prefix, e=edits: (tpl.text(e), {}))
        return s

    def shot_print(self, seconds: float = 3.2) -> Optional[Shot]:
        if not self._jig_ok():
            return None
        _, _, _, _, mesh = self.jig_stage()
        jx0, jy0, jx1, jy1 = mesh.bbox
        s = Shot("print", self.fps, hold=0.6)
        n = self.n(seconds)
        rows = 7.0
        for i in range(n):
            t = i / float(n - 1)
            pt = phase(t, 0.0, 0.88)
            z = max(0.25, mesh.zmax * pt)
            u = (pt * rows) % 1.0
            row = int(pt * rows)
            fx = u if row % 2 == 0 else 1.0 - u                 # zig-zag over the bed, one pass per few layers
            fy = 0.5 + 0.42 * math.sin(2 * math.pi * pt * 3.0)
            nozzle = None if t > 0.94 else (jx0 + (jx1 - jx0) * (0.08 + 0.84 * fx), jy0 + (jy1 - jy0) * fy)
            if t > 0.88:                                       # the head moves away once the last layer is down
                k = phase(t, 0.88, 0.94)
                nozzle = (jx1 + 40 * k, jy1 + 30 * k) if t <= 0.94 else None
            zc = None if pt >= 1.0 else z
            s.add(self.png("print", i), self.cam(20 + 10 * smooth(t), zoom=0.82),
                  make=lambda prefix, zc=zc, nz=nozzle: self.jig_scene(prefix, zcap=zc, nozzle=nz))
        return s

    def _jig_ok(self) -> bool:
        return bool(self.jig and self.paths.get("jig_stl") and os.path.exists(self.paths["jig_stl"]))

    def shot_load(self) -> Optional[Shot]:
        if not self._jig_ok() or not self.jig_parts:
            return None
        s = Shot("load", self.fps, hold=0.6)
        parts = sorted(self.jig_parts, key=lambda r: (self.b.by_ref[r].x, self.b.by_ref[r].y))
        s.meta = {"parts": parts}
        n = self.n(min(4.5, 2.5 + 0.03 * len(parts)))
        hmax = max(20.0, min(50.0, 0.3 * self.big))
        span = min(0.45, max(0.1, 6.0 / len(parts)))
        for i in range(n):
            t = i / float(n - 1)
            lifts = {}
            for k, r in enumerate(parts):
                p = stagger(k, len(parts), phase(t, 0.0, 0.92), span)
                if p > 0:
                    lifts[r] = hmax * drop(p)
            s.add(self.png("load", i), self.cam(30 + 8 * smooth(t), zoom=0.82),
                  make=lambda prefix, l=lifts: self.jig_scene(prefix, parts_lift=l))
        return s

    def shot_slide(self, seconds: float = 3.6) -> Optional[Shot]:
        if not self._jig_ok():
            return None
        wrl = self.dut_wrl(self.jig_parts)
        if not wrl:
            return None
        s = Shot("slide", self.fps, hold=1.2)
        n = self.n(seconds)
        seated = {r: 0.0 for r in self.jig_parts}
        lift0 = max(14.0, 0.12 * self.big)
        dx0 = -(self.bb.w + 30.0)
        for i in range(n):
            t = i / float(n - 1)
            a, b = ease_out(phase(t, 0.0, 0.5)), smooth(phase(t, 0.55, 0.95))
            dut = {"wrl": wrl, "dx": dx0 * (1 - a), "lift": lift0 * (1 - b)}
            s.add(self.png("slide", i), self.cam(38 - 6 * smooth(t), zoom=0.8),
                  make=lambda prefix, d=dut: self.jig_scene(prefix, parts_lift=seated, dut=d))
        return s

    def shot_lift(self, seconds: float = 3.2) -> Optional[Shot]:
        if not self._jig_ok():
            return None
        wrl = self.dut_wrl()
        if not wrl:
            return None
        s = Shot("lift", self.fps, hold=1.0)
        n = self.n(seconds)
        top = self.bb.h / 2 + 8.0                       # high enough to turn over clear of the parts' leads
        # KiCad frames the camera on the jig plate; a small board turning over rises high compared with its
        # jig, so zoom out by how far the board reaches above the plate relative to the jig's size.
        jx0, jy0, jx1, jy1 = self.jig_stage()[4].bbox
        reach = float(self.jig["support_height_mm"]) + top + self.bb.h / 2
        zoom = 0.72 * min(1.0, 0.7 * max(jx1 - jx0, jy1 - jy0) / max(1.0, reach))
        for i in range(n):
            t = i / float(n - 1)
            up, turn = smooth(phase(t, 0.0, 0.45)), smooth(phase(t, 0.35, 0.95))
            dut = {"wrl": wrl, "lift": top * up, "rx": 180.0 * (1 - turn)}
            follow = 0.5 * reach * smooth(phase(t, 0.05, 0.6))    # the camera tilts up with the rising board
            s.add(self.png("lift", i), self.cam(32 + 10 * smooth(t), zoom=zoom, rise=follow),
                  make=lambda prefix, d=dut: self.jig_scene(prefix, dut=d))
        return s

    # -- the two-jig workflow ---------------------------------------------------------
    # Frames: D = the PCB (origin at the centre of its top surface, x right, y up, z out of the top side).
    # World W = the holding jig's STL frame as it lies on the bench at the end (soldering). The placement jig
    # (A) holds the PCB top side up; the holding jig (B) closes over it upside down; the stack is turned over.
    def two_ok(self) -> bool:
        pj = (self.jig or {}).get("placement")
        return bool(pj and self._jig_ok() and self.paths.get("jig_place_stl") and os.path.exists(self.paths["jig_place_stl"]))

    def two(self) -> Dict[str, Any]:
        if getattr(self, "_two", None) is not None:
            return self._two
        jig, pj = self.jig, self.jig["placement"]
        bb, t = self.bb, self.t
        cx, cy = bb.cx, bb.cy
        x0b, y0b = jig["board_bbox_in_jig_mm"][0], jig["board_bbox_in_jig_mm"][1]
        oxb, oyb = bb.x0 - x0b, bb.y0 - y0b                          # holding jig: X = bx - oxb, Y = by - oyb
        axa, aya = bb.x0 - pj["board_bbox_in_jig_mm"][0], bb.y1 + pj["board_bbox_in_jig_mm"][1]   # X = bx - axa, Y = aya - by
        support, rest = float(jig["support_height_mm"]), float(pj["pcb_rest_height_mm"])
        B_D = Xf.T(cx - oxb, cy - oyb, support) * Xf.Rx(math.pi)     # D -> holding jig frame (PCB top down)
        A_D = Xf.T(cx - axa, aya - cy, rest + t)                    # D -> placement jig frame (PCB top up)
        final = B_D                                                 # world == holding jig frame
        up = Xf.T(final.t[0], final.t[1], rest + t)                  # PCB top up, placement jig on the bench
        mesh_b, mesh_a = self._render_meshes()
        sd = self.rd.scene_dir
        wa, wb = os.path.join(sd, "anim_jig_place.wrl"), os.path.join(sd, "anim_jig_hold.wrl")
        for path, m in ((wa, mesh_a), (wb, mesh_b)):
            with open(path, "w", encoding="ascii") as fh:
                fh.write(m.wrl())
        # bench plate: hidden under whichever jig lies on it (the overlap of both footprints)
        a_w = (up * A_D.inv()).t
        bx0, by0, bx1, by1 = mesh_b.bbox
        ax0_, ay0_, ax1_, ay1_ = mesh_a.bbox
        px0, py0 = max(bx0, a_w[0] + ax0_) + 1.0, max(by0, a_w[1] + ay0_) + 1.0
        px1, py1 = min(bx1, a_w[0] + ax1_) - 1.0, min(by1, a_w[1] + ay1_) - 1.0
        plate = W.board([], [W.gr_rect(px0, -py1, px1, -py0)], thickness=0.2)
        end = plate.rstrip().rfind(")")
        # parts in D, solder joints, iron
        parts = []
        for ref in self.jig_parts:
            fp = self.b.by_ref[ref]
            for k, m in enumerate(self._models(fp)):
                xf = Xf.T(fp.x - cx, -(fp.y - cy), 0.0) * Xf.Rz(math.radians(fp.angle)) * \
                    Xf.T(*m.offset) * Xf(kicad_rotation(m.rotate))
                parts.append(("P_%s_%d" % (ref, k), m.path.replace("${KIPRJMOD}", self.proj), xf, tuple(m.scale), ref))
        pads = []
        for ref in self.jig_parts:
            for pd in self.b.by_ref[ref].pads:
                if pd.drill_w > 0 and pd.has_copper:
                    r = max(0.5, min(1.6, 0.85 * max(pd.w, pd.h) / 2.0))
                    pads.append((pd.x - cx, -(pd.y - cy), r))
        pads.sort(key=lambda p: (round(-p[1] / 6.0), p[0] if int(round(-p[1] / 6.0)) % 2 == 0 else -p[0]))
        iron = os.path.join(sd, "anim_iron.wrl")
        with open(iron, "w", encoding="ascii") as fh:
            fh.write(iron_wrl())
        joints_all = os.path.join(sd, "anim_joints.wrl")
        with open(joints_all, "w", encoding="ascii") as fh:
            fh.write(joints_wrl(pads, t))
        self._two = {"B_D": B_D, "A_D": A_D, "final": final, "up": up, "wa": wa, "wb": wb, "mesh_a": mesh_a, "mesh_b": mesh_b,
                     "plate": (plate[:end], plate[end:]), "parts": parts, "pads": pads, "iron": iron, "joints": joints_all,
                     "support": support, "rest": rest, "stack": (max(by1 - by0, ay1_ - ay0_), support + rest + t)}
        return self._two

    def _render_meshes(self):
        """Both jigs as light render meshes, rebuilt from the same design at 0.5 mm (invisible at video size);
        falls back to the printed STLs."""
        from .jig import build_jigs
        jc = self.design.cfg["jig"]
        old = jc.get("resolution_mm")
        try:
            jc["resolution_mm"] = max(0.5, float(old or 0.25))
            grids: Dict[str, Dict[str, Any]] = {}
            build_jigs(self.design, grids=grids)
            hold = JigMesh.from_grid(grids["holding"])
            place_ = JigMesh.from_grid(grids["placement"]) if grids.get("placement", {}).get("H") else None
        except Exception as exc:  # pragma: no cover - the STLs always work
            self.log.append("animation: render meshes failed (%r); using the STLs" % exc)
            hold, place_ = JigMesh(self.paths["jig_stl"]), None
        finally:
            jc["resolution_mm"] = old
        if place_ is None and self.paths.get("jig_place_stl") and os.path.exists(self.paths["jig_place_stl"]):
            place_ = JigMesh(self.paths["jig_place_stl"])
        return hold, place_

    def scene2(self, items: List[list], plate=None) -> str:
        head, close = plate or self.two()["plate"]
        return head + "".join("\t" + dumps(it, 1).lstrip() + "\n" for it in items) + close

    def stack_items(self, pose: Xf, a: Optional[Xf] = None, b: Optional[Xf] = None, parts_lift: Optional[Dict[str, float]] = None,
                    pcb_lift: float = 0.0, pcb: bool = True, parts: bool = True, seated: bool = False) -> List[list]:
        """Everything that rides with the PCB at pose (world <- D); a/b = extra world motion of each jig
        (None = jig not shown). seated=True: the parts sit on the PCB, so the whole assembled board is one
        exported model (one file for KiCad to load instead of the bare board plus every part model)."""
        tw = self.two()
        items = []
        if a is not None:
            items.append(place("JIG_A", tw["wa"], a * pose * tw["A_D"].inv()))
        if b is not None:
            items.append(place("JIG_B", tw["wb"], b * pose * tw["B_D"].inv()))
        if seated and self.dut_wrl():
            items.append(place("DUT", self.dut_wrl() or "", pose * Xf.T(0, 0, -self.t / 2)))
            return items
        if pcb:
            items.append(place("DUT", self.dut_wrl(self.jig_parts) or "", Xf.T(0, 0, pcb_lift) * pose * Xf.T(0, 0, -self.t / 2)))
        if parts:
            for name, path, xf, scale, ref in tw["parts"]:
                lift = (parts_lift or {}).get(ref, 0.0)
                if lift is None:
                    continue
                items.append(place(name, path, Xf.T(0, 0, lift) * pose * xf, scale))
        return items

    def _print2_layout(self):
        """Both jigs side by side as printed: (pose of the placement jig, pose of the holding jig, bench plate)."""
        tw = self.two()
        ma, mb = tw["mesh_a"], tw["mesh_b"]
        gap = 14.0
        wa_w = ma.bbox[2] - ma.bbox[0]
        dy = ((ma.bbox[3] - ma.bbox[1]) - (mb.bbox[3] - mb.bbox[1])) / 2.0
        pos_a, pos_b = Xf.T(-ma.bbox[0], -ma.bbox[1], 0), Xf.T(wa_w + gap - mb.bbox[0], dy - mb.bbox[1], 0)
        rects = []
        for pos, m in ((pos_a, ma), (pos_b, mb)):
            x0, y0 = pos.apply((m.bbox[0], m.bbox[1], 0))[:2]
            x1, y1 = pos.apply((m.bbox[2], m.bbox[3], 0))[:2]
            rects.append(W.gr_rect(x0 + 1, -(y1 - 1), x1 - 1, -(y0 + 1)))
        plate = W.board([], rects, thickness=0.2)
        end = plate.rstrip().rfind(")")
        return pos_a, pos_b, (plate[:end], plate[end:])

    def step_stills(self) -> Optional[Shot]:
        """One still per step of the two-jig workflow for the production guide, from the same scenes as the video:
        jig2_print, jig2_fit, jig2_cover, jig2_unload and jig2_solder (PNG names in ``self.out``)."""
        if not self.two_ok() or not self.dut_wrl(self.jig_parts):
            return None
        tw = self.two()
        s = Shot("steps", self.fps)

        def png(name: str) -> str:
            return os.path.join(self.out, name + ".png")

        def still(name: str, cam: Dict[str, Any], items: List[list], plate=None) -> None:
            s.add(png(name), cam, make=lambda prefix: (self.scene2(items, plate), {}))

        # 1  both jigs, printed
        pos_a, pos_b, plate = self._print2_layout()
        still("jig2_print", self.cam(25, zoom=0.85), [place("JIG_A", tw["wa"], pos_a), place("JIG_B", tw["wb"], pos_b)], plate)
        # 2  the PCB top side up in the placement jig, the hand-soldered parts just above their holes
        h = max(25.0, min(60.0, 0.3 * self.big))
        still("jig2_fit", self.cam(30, zoom=0.62, rise=0.3 * h),
              self.stack_items(tw["up"], a=Xf(), parts_lift={r: 0.55 * h for r in self.jig_parts}))
        # 3  the holding jig comes down over the parts, pockets first (low camera: the gap shows)
        h0 = max(45.0, 0.4 * self.big)
        still("jig2_cover", self.cam(30, zoom=0.48, tilt=-58.0, rise=0.55 * h0),
              self.stack_items(tw["up"], a=Xf(), b=Xf.T(0, 0, h0), seated=True))
        # 4  the stack turned over, the placement jig lifted off and moved back: every lead points up
        back = 0.9 * (tw["mesh_a"].bbox[3] - tw["mesh_a"].bbox[1])
        still("jig2_unload", self.cam(32, zoom=0.5, tilt=-50.0, rise=18.5),
              self.stack_items(tw["final"], a=Xf.T(0.0, back, 25.0), b=Xf(), seated=True))
        # 5  soldering from above: half the joints done, the iron on the next one
        pads = tw["pads"]
        if pads:
            world = [tw["final"].apply((x, y, -self.t)) for x, y, _ in pads]
            k = len(pads) // 2
            tip = (world[k][0], world[k][1], world[k][2] + 1.1)

            def make(prefix, done=pads[:k], tip=tip):
                files = {}
                items = self.stack_items(tw["final"], b=Xf(), seated=True)
                if done:
                    path = prefix + "_joints.wrl"
                    files[path] = joints_wrl(done, self.t)
                    items.append(place("JOINTS", path, tw["final"]))
                items.append(place("IRON", tw["iron"], Xf.T(*tip)))
                return self.scene2(items), files
            s.add(png("jig2_solder"), self.cam(32, zoom=0.95), make=make)
        return s

    def shot_print2(self, seconds: float = 3.0) -> Optional[Shot]:
        if not self.two_ok():
            return None
        ma, mb = self.two()["mesh_a"], self.two()["mesh_b"]
        pos_a, pos_b, plate = self._print2_layout()
        s = Shot("print2", self.fps, hold=0.6)
        n = self.n(seconds)
        for i in range(n):
            t = i / float(n - 1)
            pt = phase(t, 0.0, 0.88)

            def make(prefix, pt=pt, t=t):
                files, items = {}, []
                for tag, pos, m in (("a", pos_a, ma), ("b", pos_b, mb)):
                    z = max(0.25, m.zmax * pt)
                    u = (pt * 7.0) % 1.0
                    fx = u if int(pt * 7.0) % 2 == 0 else 1.0 - u
                    fy = 0.5 + 0.42 * math.sin(2 * math.pi * pt * 3.0 + (1.3 if tag == "b" else 0.0))
                    x0, y0, x1, y1 = m.bbox
                    noz = None if t > 0.9 else (x0 + (x1 - x0) * (0.08 + 0.84 * fx), y0 + (y1 - y0) * fy)
                    path = "%s_%s.wrl" % (prefix, tag)
                    files[path] = m.wrl(None if pt >= 1.0 else z, hotend(noz[0], noz[1], z) if noz else ())
                    items.append(place("JIG_" + tag, path, pos))
                return self.scene2(items, plate), files
            s.add(self.png("print2", i), self.cam(20 + 10 * smooth(t), zoom=0.85), make=make)
        return s

    def shot_place2(self) -> Optional[Shot]:
        if not self.two_ok() or not self.dut_wrl(self.jig_parts):
            return None
        tw = self.two()
        parts = sorted(self.jig_parts, key=lambda r: (self.b.by_ref[r].x, self.b.by_ref[r].y))
        s = Shot("place2", self.fps, hold=0.6)
        s.meta = {"parts": parts}
        n = self.n(min(4.5, 3.0 + 0.025 * len(parts)))
        hmax = max(25.0, min(60.0, 0.3 * self.big))
        span = min(0.45, max(0.1, 6.0 / max(1, len(parts))))
        for i in range(n):
            t = i / float(n - 1)
            pcb_lift = hmax * drop(phase(t, 0.0, 0.3))
            lifts: Dict[str, Optional[float]] = {}
            for k, r in enumerate(parts):
                p = stagger(k, len(parts), phase(t, 0.32, 0.95), span)
                lifts[r] = None if p <= 0 else hmax * drop(p)
            s.add(self.png("place2", i), self.cam(24 + 10 * smooth(t), zoom=0.85),
                  make=lambda prefix, pl=pcb_lift, lf=lifts: (self.scene2(self.stack_items(tw["up"], a=Xf(), parts_lift=lf,
                                                                                          pcb_lift=pl)), {}))
        return s

    def shot_cover(self, seconds: float = 2.4) -> Optional[Shot]:
        if not self.two_ok():
            return None
        tw = self.two()
        s = Shot("cover", self.fps, hold=0.5)
        n = self.n(seconds)
        h0 = max(35.0, 0.25 * self.big)
        for i in range(n):
            t = i / float(n - 1)
            h = h0 * (1.0 - smooth(phase(t, 0.0, 0.85)))
            s.add(self.png("cover", i), self.cam(32 + 4 * smooth(t), zoom=0.78, rise=0.35 * h + 8.0),
                  make=lambda prefix, h=h: (self.scene2(self.stack_items(tw["up"], a=Xf(), b=Xf.T(0, 0, h), seated=True)), {}))
        return s

    def shot_flip(self, seconds: float = 3.0) -> Optional[Shot]:
        if not self.two_ok():
            return None
        tw = self.two()
        u, f = tw["up"].t, tw["final"].t
        # bounding corners of the whole stack in D, to keep it off the bench while it turns
        half = tw["stack"][0] / 2.0 + 2.0
        zlo, zhi = -(tw["rest"] + self.t), tw["support"] + 2.0
        corners = [(0.0, y, z) for y in (-half, half) for z in (zlo, zhi)]
        s = Shot("flip", self.fps, hold=0.4)
        n = self.n(seconds)
        for i in range(n):
            t = i / float(n - 1)
            a = smooth(phase(t, 0.05, 0.95))
            base = (u[0] + a * (f[0] - u[0]), u[1] + a * (f[1] - u[1]), u[2] + a * (f[2] - u[2]))
            rot = Xf.Rx(math.pi * a)
            low = min(rot.apply(c)[2] for c in corners) + base[2]
            lift = max(0.0, -low + 4.0 * math.sin(math.pi * a)) + 12.0 * math.sin(math.pi * a)
            G = Xf.T(base[0], base[1], base[2] + lift) * rot * Xf.T(-u[0], -u[1], -u[2])
            pose = G * tw["up"]
            s.add(self.png("flip", i), self.cam(32, zoom=0.6 - 0.12 * math.sin(math.pi * a), rise=lift + 10.0),
                  make=lambda prefix, pose=pose: (self.scene2(self.stack_items(pose, a=Xf(), b=Xf(), seated=True)), {}))
        return s

    def shot_unload(self, seconds: float = 2.2) -> Optional[Shot]:
        if not self.two_ok():
            return None
        tw = self.two()
        s = Shot("unload", self.fps, hold=0.5)
        n = self.n(seconds)
        for i in range(n):
            t = i / float(n - 1)
            up_, away = smooth(phase(t, 0.0, 0.55)), smooth(phase(t, 0.45, 1.0))
            a = Xf.T(-away * (self.bb.w + 80.0), 0.0, 45.0 * up_ + 20.0 * away)
            s.add(self.png("unload", i), self.cam(32, zoom=0.72, rise=18.0 * (1 - away) + 6.0),
                  make=lambda prefix, a=a: (self.scene2(self.stack_items(tw["final"], a=a, b=Xf(), seated=True)), {}))
        return s

    def shot_solder(self, seconds: float = 4.4) -> Optional[Shot]:
        if not self.two_ok():
            return None
        tw = self.two()
        pads = tw["pads"]
        if not pads:
            return None
        world = [tw["final"].apply((x, y, -self.t)) for x, y, _ in pads]   # joints on the (now upward) bottom side
        s = Shot("solder", self.fps, hold=0.8)
        s.meta = {"joints": len(pads)}
        n = self.n(seconds)
        detail = min(6, len(pads))
        t_detail, t_rest = 0.5, 0.9                       # first joints at working pace, the rest sped up
        jz = 1.1                                          # iron tip height above the board at a joint
        for i in range(n):
            t = i / float(n - 1)
            if t < t_detail:
                q = t / t_detail * detail
                k = min(detail - 1, int(q))
                f = q - k
                done = k + (1 if f > 0.55 else 0)
                nxt = world[min(k + 1, len(world) - 1)]
                if f < 0.7:                                # dwell on joint k
                    tip = (world[k][0], world[k][1], world[k][2] + jz)
                else:                                      # hop to the next joint
                    g = (f - 0.7) / 0.3
                    tip = (world[k][0] + (nxt[0] - world[k][0]) * g, world[k][1] + (nxt[1] - world[k][1]) * g,
                           world[k][2] + jz + 4.0 * math.sin(math.pi * g))
            elif t < t_rest:
                g = (t - t_detail) / (t_rest - t_detail)
                done = detail + int(round(g * (len(pads) - detail)))
                k = min(len(world) - 1, max(0, done - 1))
                tip = (world[k][0], world[k][1], world[k][2] + jz + 1.5)
            else:
                done = len(pads)
                g = (t - t_rest) / (1 - t_rest)
                last = world[-1]
                tip = (last[0] + 30 * g, last[1] + 20 * g, last[2] + jz + 40 * g)
            sub = pads[:done]

            def make(prefix, sub=sub, tip=tip):
                files = {}
                items = self.stack_items(tw["final"], b=Xf(), seated=True)
                if sub:
                    path = prefix + "_joints.wrl"
                    files[path] = joints_wrl(sub, self.t)
                    items.append(place("JOINTS", path, tw["final"]))
                items.append(place("IRON", tw["iron"], Xf.T(*tip)))
                return self.scene2(items), files
            s.add(self.png("solder", i), self.cam(30 + 6 * smooth(t), zoom=0.95), make=make)
        return s

    def shot_lift2(self, seconds: float = 3.2) -> Optional[Shot]:
        if not self.two_ok():
            return None
        wrl = self.dut_wrl()
        if not wrl:
            return None
        tw = self.two()
        s = Shot("lift2", self.fps, hold=1.0)
        n = self.n(seconds)
        top = self.bb.h / 2 + 8.0
        mid = tw["final"].apply((0.0, 0.0, -self.t / 2))
        mb = tw["mesh_b"].bbox
        reach = tw["support"] + top + self.bb.h / 2
        zoom = 0.72 * min(1.0, 0.7 * max(mb[2] - mb[0], mb[3] - mb[1]) / max(1.0, reach))
        for i in range(n):
            t = i / float(n - 1)
            up_, turn = smooth(phase(t, 0.0, 0.45)), smooth(phase(t, 0.35, 0.95))
            pose = Xf.T(mid[0], mid[1], mid[2] + top * up_) * Xf.Rx(math.pi * (1 - turn)) * Xf.T(0, 0, self.t / 2)
            follow = 0.5 * reach * smooth(phase(t, 0.05, 0.6))
            items = [place("JIG_B", tw["wb"], Xf()), place("DUT", wrl, pose * Xf.T(0, 0, -self.t / 2)),
                     place("JOINTS", tw["joints"], pose)]
            s.add(self.png("lift2", i), self.cam(32 + 10 * smooth(t), zoom=zoom, rise=follow),
                  make=lambda prefix, it=items: (self.scene2(it), {}))
        return s

    # -- the fixture stage ---------------------------------------------------------
    def _fixture_ok(self) -> bool:
        return bool(self.fixture and self.paths.get("fixture") and os.path.exists(self.paths["fixture"]))

    def fixture_template(self) -> Template:
        if getattr(self, "_fx_tpl", None) is None:
            path = self.paths["fixture"]
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read().replace("${KIPRJMOD}", os.path.dirname(os.path.abspath(path)))
            pogo = [ref for s_, e_, ref in footprint_spans(text) if re.search(r'\(model\s+"[^"]*pogo', text[s_:e_])]
            self._fx_tpl = Template(text, pogo)
        return self._fx_tpl

    def fixture_duts(self, wrl: str, lift: float, dx: float = 0.0) -> List[list]:
        clear = float(self.plan["pogo"].get("probe_clearance_mm", 6.0))
        from .testplan import fixture_side
        rotate = (0.0, 180.0, 0.0) if fixture_side(self.plan) == "top" else (0.0, 0.0, 0.0)
        out = []
        for st in self.fixture.get("sites", []) or [{"site": 1, "offset": [0.0, 0.0]}]:
            ddx, ddy = st["offset"]
            out.append(model_footprint("DUT%d" % st["site"], self.bb.cx + ddx + dx, self.bb.cy + ddy, wrl,
                                       offset=(0.0, 0.0, clear + self.t / 2 + lift), rotate=rotate))
        return out

    def shot_pogo(self) -> Optional[Shot]:
        if not self._fixture_ok():
            return None
        tpl = self.fixture_template()
        pins = tpl.refs
        if not pins:
            return None
        s = Shot("pogo", self.fps, hold=0.6)
        n = self.n(min(3.5, 2.0 + 0.04 * len(pins)))
        hmax = 30.0
        span = min(0.5, max(0.15, 5.0 / len(pins)))
        order = sorted(pins)
        for i in range(n):
            t = i / float(n - 1)
            edits = {}
            for k, r in enumerate(order):
                p = stagger(k, len(order), phase(t, 0.0, 0.9), span)
                edits[r] = (hmax * drop(p), p <= 0)
            s.add(self.png("pogo", i), self.cam(18 + 12 * smooth(t), zoom=1.45, tilt=ISO + 6),
                  make=lambda prefix, e=edits: (tpl.text(e), {}))
        return s

    def shot_press(self, seconds: float = 3.6) -> Optional[Shot]:
        if not self._fixture_ok():
            return None
        wrl = self.dut_wrl()
        if not wrl:
            return None
        tpl = self.fixture_template()
        s = Shot("press", self.fps, hold=1.0)
        n = self.n(seconds)
        lift0 = max(18.0, 0.15 * self.big)
        dx0 = -(self.bb.w + 40.0)
        for i in range(n):
            t = i / float(n - 1)
            a, b = ease_out(phase(t, 0.0, 0.5)), smooth(phase(t, 0.55, 0.95))
            items = self.fixture_duts(wrl, lift0 * (1 - b), dx0 * (1 - a))
            s.add(self.png("press", i), self.cam(30 - 8 * smooth(t), zoom=0.8),
                  make=lambda prefix, it=items: (tpl.text(None, it), {}))
        return s


SHOTS = ("orbit", "place", "print", "load", "slide", "lift", "pogo", "press")              # single-jig workflow
SHOTS_TWO_JIG = ("orbit", "place", "print2", "place2", "cover", "flip", "unload", "solder", "lift2", "pogo", "press")


def build_shots(design, plan, fixture, jig, paths, visuals_dir: str, fps: float = 30.0, size=(1280, 720),
                quality: str = "basic", workers: int = 0, only: Optional[Sequence[str]] = None,
                progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, Any]:
    """Render every shot that applies to this board into <visuals_dir>/scenes/anim.

    Returns {"shots": {name: {"images", "fps", "hold", "meta"}}, "stats", "log"}.
    """
    rd = Renderer(visuals_dir, size[0], size[1], quality=quality)
    res: Dict[str, Any] = {"shots": {}, "log": rd.log, "stats": {}}
    if not rd.available:
        rd.log.append("animation: kicad-cli not found")
        return res
    st = Stage(design, plan, fixture, jig, paths, rd, os.path.join(rd.scene_dir, "anim"), fps, size, quality)
    shots: List[Shot] = []
    two = str((jig or {}).get("workflow", "")) == "two_jig" and st.two_ok()
    for name in (only or (SHOTS_TWO_JIG if two else SHOTS)):
        try:
            sh = getattr(st, "shot_" + name)()
        except Exception as exc:  # one broken shot must not cost the whole video
            st.log.append("animation: shot %s skipped (%r)" % (name, exc))
            sh = None
        if sh is not None and sh.jobs:
            shots.append(sh)
    res["stats"] = render_shots(shots, rd, workers, progress)
    for sh in shots:
        if sh.files:
            res["shots"][sh.name] = {"images": sh.files, "fps": sh.fps, "hold": sh.hold, "meta": sh.meta}
    res["log"] = rd.log + st.log
    return res
