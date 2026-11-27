"""Test environments: one to three test stations that a whole collection of boards fits, and everything for them.

    pcbtestkit environment init  [--out environment-spec.json]          the editable environment spec
    pcbtestkit environment plan  BOARDS... [--spec F] [--environment DIR]  which stations, which board where
    pcbtestkit environment build BOARDS... --out DIR [...]                 the stations, every board's outputs, videos

BOARDS are project folders (searched recursively), .kicad_pro / .kicad_pcb files or @list.txt files (one per line).

How it works
1. Profile every board (in parallel, cached in <out>/profiles): its test plan on a "probe" station that has
   everything (how many measurement channels, a switched supply, which buses through the fixture, programming
   interface, supply voltage), and the size of one fixture site (board, nest, clamp, divider block) for the boards
   sliding in from the front or from the side.
2. Design the stations. A station is a plate and one or two interface sockets; its pins are analog inputs, switched
   supplies, bus instances and digital lines, with GND every few pins. Board k of a batch takes the k-th block of
   inputs, supply k and bus instance k, so a board type's batch on a station is the smallest of: inputs // its
   channels, supplies, each of its buses, the fixture sites that fit the plate, the spec's max_batch. A greedy
   designer spends the pin budget where it gains the most (priority x (1 + batch_weight x log2(batch)) summed over
   the boards), a further station is made for the boards the earlier ones cannot take (up to max_stations), and
   every board goes to the station that gives it the largest batch.
3. Check every board on its station with a real fixture layout (station.plan_board), generate each station's KiCad
   board (station_board) and read it back as the facility the fixtures are made for.
4. build: every board type's outputs per station (station.run_station: guides, fixtures with a nest and clamp per
   board, jigs, runner, optionally videos), each station's page and swap video, and for the environment index.html,
   requirements.md (what each station needs: inputs, supplies, buses, programmers, plate), ranking.csv and
   environment.json.

An existing environment (a folder an earlier build made, whose station boards you may have edited in KiCad) or
station boards of your own are kept as they are; new stations are only designed for the boards they cannot take.
"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import math
import os
import re
import shutil
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import __version__
from . import facility as F
from . import station_board as SB

SCHEMA = "pcbtestkit-environment/1"
SPEC_SCHEMA = "pcbtestkit-environment-spec/1"
MARKER = ".pcbtestkit-environment"
BUSES = ("can", "uart", "i2c", "rs485", "usb", "ethernet")
ENTRIES = ("front", "left")
QUALITIES = ("basic", "standard", "best", "high")      # kicad-cli ray tracing (see visuals.RAYTRACE_PRESETS)
PROFILE_REV = 3          # bumped when the fixture layout changes what a board profile says (sites are re-measured)

SPEC_DEFAULTS: Dict[str, Any] = {
    "schema": SPEC_SCHEMA,
    "name": "Test environment",
    "max_stations": 3,               # at most this many stations (2-3 is the aim; 1 = everything on one)
    "min_boards_per_station": 1,     # a further station only when it takes at least this many more board types
    "rebalance": False,              # false: a further station takes only the boards the earlier ones cannot;
                                     # true: a board also moves to a later station for a batch 1.5x as big
    "plate_mm": [300.0, 220.0],      # every station's plate: the top every fixture copies
    "large_plate_mm": [400.0, 300.0],  # a further station may get this plate for boards that need it ([] = never)
    "sockets": {"max": 2, "pins": 80, "gnd_every": 6},   # 2 x 40 sockets, GND between every 6 signals
    "limits": {"analog_inputs": 96, "supplies": 8, "can": 4, "uart": 4, "i2c": 4, "rs485": 2, "usb": 2, "ethernet": 2},
    "ain_group": 4,                  # analog inputs come in groups (4-channel ADCs)
    "dio": 4,                        # digital lines every station has (not used by the boards' tests)
    "analog": {"max_v": 10.0},       # full scale of the analog inputs (the fixtures' dividers are sized for it)
    "supplies": {"max_v": 24.0, "max_current_a": 3.0},
    "max_batch": 12,                 # boards of one type per fixture at most
    "batch_weight": 0.35,            # a board that fits counts 1, a batch of n counts 1 + 0.35 x log2(n)
    "priorities": {},                # board name -> weight (default 1): how much it matters that a board fits
    "nest": True,                    # every board in a nest with a toggle clamp (fixture.nest)
    # the fixture router's effort per board (100+ fixtures): a faster search, two passes; what it leaves is listed
    # in each board's warnings to route in KiCad. {} = the router's own thorough defaults
    "router": {"weight": 1.6, "max_passes": 2, "last_resort_expansions": 500000},
    # the guides' 3D images: best (shadows, anti-aliasing) | standard | basic | high (KiCad's own, ~6x slower)
    "visuals": {"quality": "best"},
    "video_quality": None,           # ray tracing of the videos: None = station videos basic, board explainers standard
    "exclude": [],                   # board names or folder names to leave out
    # how the stations are laid out: auto = the alternative worth the most (what its boards are worth minus what it
    # costs, spec "costs"); general = one main station and further ones for what it cannot take; specialized-N =
    # N stations, each for boards that need much the same (k-means); split-<bus> = one station for the boards with
    # that bus (e.g. split-ethernet), one for the rest. Every alternative is in environment.json "layouts".
    "layout": "auto",
    "coverage_first": True,          # auto never takes a layout that fits fewer boards than another one would
}

_LIMIT_KEYS = {"analog_inputs": (0, 512), "supplies": (0, 64)}
SPEC_DEFAULTS["costs"] = {               # rough list prices for upgrades.py and the station designs; set your own
    "currency": "EUR",
    "value_per_board": 300.0,     # one board type that fits (priority 1): what a tester of its own would cost
    "station": 600.0,             # frame, press, controller (Raspberry Pi), enclosure, wiring
    "socket": 25.0,               # an interface socket with its share of the station board
    "ain_group": 30.0,            # a 4-channel ADC with its input protection
    "supply": 60.0,               # a switched, current-limited supply output
    "high_voltage_supply": 90.0,  # extra per supply output when supplies go above 24 V
    "can": 20.0, "uart": 6.0, "i2c": 6.0, "rs485": 15.0, "usb": 25.0, "ethernet": 45.0,   # per bus instance
    "programmer": 60.0,           # a programmer (SWD probe, ISP, ...) per board of a batch programmed at once
    "plate_dm2": 5.0,             # the station plate and its fixture plates, per square decimetre
    "fixture": 60.0,              # a fixture board per board type (PCB, header, assembly)
    "fixture_site": 15.0,         # per board of its batch: pogo pins, nest, clamp
}


# --------------------------------------------------------------------------- #
# the spec
# --------------------------------------------------------------------------- #
def default_spec() -> Dict[str, Any]:
    return json.loads(json.dumps(SPEC_DEFAULTS))


def _merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "priorities" else v
    return out


def load_spec(path: Optional[str] = None, over: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The defaults, a spec file on top, then ``over`` (command-line settings). Raises ValueError when invalid."""
    spec = default_spec()
    if path:
        with open(path, encoding="utf-8") as fh:
            spec = _merge(spec, json.load(fh))
    spec = _merge(spec, over or {})
    errs = validate_spec(spec)
    if errs:
        raise ValueError("environment spec: " + "; ".join(errs))
    return spec


def validate_spec(spec: Dict[str, Any]) -> List[str]:
    E: List[str] = []

    def num(path: str, v: Any, lo: float, hi: float) -> None:
        try:
            f = float(v)
        except (TypeError, ValueError):
            E.append("%s must be a number" % path)
            return
        if not lo <= f <= hi:
            E.append("%s = %g is outside %g..%g" % (path, f, lo, hi))
    num("max_stations", spec.get("max_stations"), 1, 10)
    num("max_batch", spec.get("max_batch"), 1, 64)
    num("batch_weight", spec.get("batch_weight"), 0, 5)
    num("sockets.max", (spec.get("sockets") or {}).get("max"), 1, 4)
    num("sockets.pins", (spec.get("sockets") or {}).get("pins"), 10, 2 * SB.MAX_POSITIONS)
    num("sockets.gnd_every", (spec.get("sockets") or {}).get("gnd_every"), 2, 40)
    for key in ("plate_mm",) + (("large_plate_mm",) if spec.get("large_plate_mm") else ()):
        v = spec.get(key)
        if not (isinstance(v, (list, tuple)) and len(v) == 2):
            E.append("%s must be [width, height] in mm" % key)
        else:
            num(key + "[0]", v[0], 60, 2000)
            num(key + "[1]", v[1], 60, 2000)
    lim = spec.get("limits") or {}
    for k, v in lim.items():
        if k not in ("analog_inputs", "supplies") + BUSES:
            E.append("limits.%s is not a resource (analog_inputs, supplies, %s)" % (k, ", ".join(BUSES)))
            continue
        num("limits." + k, v, 0, _LIMIT_KEYS.get(k, (0, 32))[1])
    for k, v in (spec.get("priorities") or {}).items():
        num("priorities.%s" % k, v, 0, 1000)
    for path, q in (("visuals.quality", (spec.get("visuals") or {}).get("quality")), ("video_quality", spec.get("video_quality"))):
        if q and q not in QUALITIES:
            E.append("%s must be one of %s" % (path, ", ".join(QUALITIES)))
    lay = str(spec.get("layout") or "auto")
    if lay not in ("auto", "general") and not re.match(r"^(specialized-[1-9]|split-(%s))$" % "|".join(BUSES), lay):
        E.append("layout must be auto, general, specialized-N or split-<bus> (%s)" % ", ".join(BUSES))
    for k, v in (spec.get("costs") or {}).items():
        if k == "currency":
            if not isinstance(v, str) or not v.strip():
                E.append("costs.currency must be a text such as EUR")
        elif k not in SPEC_DEFAULTS["costs"]:
            E.append("costs.%s is not a cost (%s)" % (k, ", ".join(sorted(x for x in SPEC_DEFAULTS["costs"] if x != "currency"))))
        else:
            num("costs." + k, v, 0, 1e7)
    return E


# --------------------------------------------------------------------------- #
# the collection
# --------------------------------------------------------------------------- #
SKIP_DIRS = {"production_test", ".pcbtestkit", "__pycache__", ".git", ".svn", "node_modules", "profiles", "visuals",
             "runner", "pcbtestkit-models", "station_video"}


def _is_output_dir(d: str, files: Sequence[str]) -> bool:
    """Folders PCB TestKit made (board outputs, stations, environments): not projects to test."""
    names = set(files)
    return bool(names & {MARKER, "station.json", ".pcbtestkit-station.json"}) or \
        any(f.endswith(("-fixture.kicad_pcb", "-fixture-top.kicad_pcb", "-fixture-bottom.kicad_pcb")) for f in names)


def discover(inputs: Sequence[str]) -> Tuple[List[str], List[str]]:
    """The board files of every KiCad project in ``inputs`` (folders searched recursively, .kicad_pro/.kicad_pcb
    files, @list.txt files with one path per line, relative to the list). Returns (boards, warnings)."""
    out: List[str] = []
    warns: List[str] = []
    seen = set()

    def add(pcb: str) -> None:
        real = os.path.realpath(pcb)
        if real not in seen and os.path.exists(pcb):
            seen.add(real)
            out.append(os.path.abspath(pcb))

    for raw in inputs:
        item = raw[1:] if raw.startswith("@") else raw
        if raw.startswith("@") or (os.path.isfile(item) and item.lower().endswith((".txt", ".lst", ".list"))):
            try:
                with open(item, encoding="utf-8") as fh:
                    lines = [ln.strip() for ln in fh if ln.strip() and not ln.strip().startswith("#")]
            except OSError as exc:
                warns.append("cannot read %s: %s" % (item, exc))
                continue
            base = os.path.dirname(os.path.abspath(item))
            sub, w2 = discover([ln if os.path.isabs(ln) else os.path.join(base, ln) for ln in lines])
            for p in sub:
                add(p)
            warns += w2
            continue
        if os.path.isfile(item):
            stem, ext = os.path.splitext(item)
            if ext in (".kicad_pro", ".kicad_pcb", ".kicad_sch") and os.path.exists(stem + ".kicad_pcb"):
                add(stem + ".kicad_pcb")
            else:
                warns.append("%s: not a KiCad project or board" % item)
            continue
        if not os.path.isdir(item):
            warns.append("%s: not found" % item)
            continue
        for d, dirs, files in os.walk(item):
            dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not x.endswith("-backups") and not x.startswith("."))
            if _is_output_dir(d, files):
                dirs[:] = []
                continue
            pros = sorted(f for f in files if f.endswith(".kicad_pro"))
            pcbs = sorted(f for f in files if f.endswith(".kicad_pcb") and not f.startswith("_autosave"))
            stems = {os.path.splitext(f)[0] for f in pros}
            for f in pcbs:
                if not stems or os.path.splitext(f)[0] in stems:
                    add(os.path.join(d, f))
    return out, warns


def _folders(names: Sequence[str]) -> List[str]:
    """Unique, file-system safe folder names for boards (a name used twice gets -2, -3, ...)."""
    out, used = [], {}
    for n in names:
        base = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in n).strip("._") or "board"
        used[base] = used.get(base, 0) + 1
        out.append(base if used[base] == 1 else "%s-%d" % (base, used[base]))
    return out


# --------------------------------------------------------------------------- #
# board profiles
# --------------------------------------------------------------------------- #
def probe_facility(spec: Dict[str, Any]) -> Dict[str, Any]:
    """A station with everything (for profiling): what a board asks of a station, not what one station offers."""
    res = {"ain": 128, "vout": 2, "buses": {b: 1 for b in BUSES}, "dio": 0}
    pins = SB.socket_pins(res, 1000, 10 ** 6)[0]
    return {"schema": F.SCHEMA, "id": "environment-probe", "name": "Profiling station",
            "analog": {"inputs": 128, "max_v": float((spec.get("analog") or {}).get("max_v", 10.0))},
            "power": {"outputs": 2, "switched": True},
            "buses": {b: {"via": "fixture"} for b in BUSES},
            "fixture": {"connectors": [{"name": "J1", "shared": True, "pins_across": 2, "pins_along": len(pins) // 2,
                                        "pitch_mm": 2.54, "numbering": "zigzag", "angle": 90, "at_mm": [0.0, 0.0],
                                        "pins": {str(i + 1): f for i, f in enumerate(pins)}}]}}


def _file_sha(paths: Sequence[Optional[str]], extra: Any) -> str:
    h = hashlib.sha1()
    for p in paths:
        if p and os.path.exists(p):
            with open(p, "rb") as fh:
                h.update(fh.read())
        h.update(b"|")
    h.update(json.dumps(extra, sort_keys=True, default=str).encode())
    return h.hexdigest()


def profile_key(pcb: str, spec: Dict[str, Any]) -> str:
    from .project import locate
    _pcb, sch, _pro = locate(pcb)
    cfg = os.path.join(os.path.dirname(pcb), "pcbtestkit.json")
    return _file_sha([pcb, sch, cfg], [__version__, PROFILE_REV, (spec.get("analog") or {}).get("max_v"),
                                       bool(spec.get("nest", True))])


def profile_board(job: Dict[str, Any]) -> Dict[str, Any]:
    """What one board asks of a station (runs in a worker process): channels, supply, buses, programming,
    the fixture site's size per nest entry; status "ok" or "error" with the reason."""
    from .fixture import build_fixture
    from .pipeline import analyze
    from .project import Design
    pcb, spec = job["pcb"], job["spec"]
    t0 = time.time()
    prof: Dict[str, Any] = {"pcb": pcb, "name": os.path.splitext(os.path.basename(pcb))[0], "status": "ok", "issues": []}
    cache = job.get("cache")
    key = None
    try:
        key = profile_key(pcb, spec)
        if cache:
            try:
                with open(cache, encoding="utf-8") as fh:
                    old = json.load(fh)
                if old.get("key") == key:
                    old["profile"]["pcb"] = pcb
                    return dict(old["profile"], cached=True)
            except (OSError, ValueError, KeyError):
                pass
        ov: Dict[str, Any] = {"facility": job["probe"], "batch": {"count": 1}, "environment": {"path": "none"},
                              "fixture": {"route": False, "nest": {"enabled": bool(spec.get("nest", True))}}}
        d = Design.load(pcb, overrides=ov)
        bb = d.board.outline_bbox()
        _findings, plan = analyze(d, include_kicad=False)
        need = F.board_needs(plan)
        sup = plan.get("supply") or {}
        prof.update({
            "name": d.board.name, "size_mm": [round(bb.w, 1), round(bb.h, 1)], "layers": getattr(d.board, "copper_layers", None),
            "channels": int(need["channels"]), "buses": list(need["buses"]),
            "supply": {"needed": bool(need["supply"]), "net": sup.get("net"), "voltage": sup.get("voltage"),
                       "current_a": sup.get("current_limit_a")},
            "programming": [{"protocol": h.get("protocol") or "?", "ref": h.get("ref")} for h in (plan.get("program") or {}).get("headers") or []],
            "probe_side": plan.get("probe_side"), "probes": len(plan.get("probes") or []),
            "coverage_pct": (plan.get("coverage") or {}).get("percent"),
        })
        if plan.get("probe_side") == "both":
            prof["issues"].append("probed from both sides (two fixture plates): a station takes one-sided fixtures; set "
                                  "testplan.probe_side to top or bottom")
        if not plan.get("probes"):
            prof["issues"].append("no test point the fixture can reach")
        sites: Dict[str, List[float]] = {}
        spacing = 10.0
        for entry in (ENTRIES if spec.get("nest", True) else ("front",)):
            if entry != "front":
                ov_e = dict(ov, fixture=dict(ov["fixture"], nest={"enabled": True, "entry": entry}))
                d = Design.load(pcb, overrides=ov_e)
            fx = build_fixture(d, plan)
            sites[entry] = list(fx.get("site_mm") or [bb.w + 20.0, bb.h + 40.0])
            spacing = float(fx.get("site_spacing_mm") or spacing)
        prof["site"] = sites
        prof["spacing_mm"] = spacing
    except Exception as exc:  # a board that cannot be read is reported, never fatal
        prof.update(status="error", reason="%s: %s" % (type(exc).__name__, exc))
    prof["seconds"] = round(time.time() - t0, 1)
    if cache and key and prof["status"] == "ok":
        try:
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "w", encoding="utf-8") as fh:
                json.dump({"key": key, "profile": dict(prof, pcb=None)}, fh, indent=1)
        except OSError:
            pass
    return prof


def profile_all(boards: Sequence[str], spec: Dict[str, Any], cache_dir: Optional[str] = None, jobs: int = 1,
                progress: Optional[Callable[[str, float], None]] = None) -> List[Dict[str, Any]]:
    from .station import run_jobs
    probe = probe_facility(spec)
    folders = _folders([os.path.splitext(os.path.basename(p))[0] for p in boards])
    work = [{"pcb": p, "spec": spec, "probe": probe,
             "cache": os.path.join(cache_dir, "%s-%s.json" % (f, hashlib.sha1(p.encode()).hexdigest()[:8])) if cache_dir else None}
            for p, f in zip(boards, folders)]

    def done(n: int, total: int, job: Dict[str, Any], res: Dict[str, Any]) -> None:
        if progress:
            progress("Profiled %d of %d: %s%s" % (n, total, res.get("name"), " (cached)" if res.get("cached") else ""),
                     n / float(max(1, total)))
    profs = run_jobs(profile_board, work, jobs, done)
    for p in profs:
        if p.get("error") and not p.get("status"):
            p.update(status="error", reason=p.pop("error"))
    for p, pcb in zip(profs, boards):
        p.setdefault("pcb", pcb)
        p["pcb"] = pcb
        p.setdefault("name", os.path.splitext(os.path.basename(pcb))[0])
    for p, f in zip(profs, _folders([p["name"] for p in profs])):
        p["folder"] = f
    excl = set(spec.get("exclude") or [])
    for p in profs:
        if p["name"] in excl or p["folder"] in excl:
            p.update(status="excluded", reason="excluded in the environment spec")
        p["priority"] = float((spec.get("priorities") or {}).get(p["name"], (spec.get("priorities") or {}).get(p["folder"], 1.0)))
    return profs


# --------------------------------------------------------------------------- #
# stations: what they offer, and what a board gets on one
# --------------------------------------------------------------------------- #
def model_from_resources(res: Dict[str, Any], plate: Sequence[float], spec: Dict[str, Any], name: str) -> Dict[str, Any]:
    """A designed station: its resources, plate, sockets and the plate area its fixtures can use."""
    so = spec.get("sockets") or {}
    sockets = SB.socket_pins(res, int(so.get("gnd_every", 6)), int(so.get("pins", 80)))
    w, h = float(plate[0]), float(plate[1])
    places = SB.socket_places(w, h, sockets)
    free_h = min(py for _px, py in places) - SB.PITCH - 4.0
    return {"name": name, "res": json.loads(json.dumps(res)), "plate": [w, h], "free": [round(w, 2), round(free_h, 2)],
            "sockets": len(sockets), "pins": sum(len(s) for s in sockets), "fixed": False, "uid": _uid()}


def model_from_facility(fac: Dict[str, Any], name: str) -> Dict[str, Any]:
    """An existing station (a facility, e.g. read from its own board): what it offers."""
    from .fixture import plate_free
    cap = F.capacity(fac)
    plate = (fac.get("fixture") or {}).get("plate_mm") or [10000.0, 10000.0]
    free = plate_free([c for c in F.connectors(fac)], float(plate[0]), float(plate[1]))
    res = {"ain": int(cap.get("inputs") or 0), "vout": int(cap.get("supplies") or 0),
           "buses": {b: int(n) for b, n in (cap.get("buses") or {}).items()}, "dio": int(cap.get("dio") or 0)}
    return {"name": name, "res": res, "plate": [float(plate[0]), float(plate[1])], "free": [round(free.w, 2), round(free.h, 2)],
            "sockets": len(F.connectors(fac)), "pins": sum(len(c.get("pins") or {}) for c in F.connectors(fac)),
            "fixed": True, "max_sites": (fac.get("batch") or {}).get("max_sites"), "uid": _uid()}


_UID = [0]


def _uid() -> int:
    _UID[0] += 1
    return _UID[0]


def plate_sites(prof: Dict[str, Any], model: Dict[str, Any], spec: Dict[str, Any]) -> Tuple[int, str]:
    """(boards of this type whose fixture sites fit the station's plate, nest entry that fits the most)."""
    from .fixture import sites_on_plate
    best = (0, "front")
    for entry, site in sorted((prof.get("site") or {}).items(), key=lambda kv: kv[0] != "front"):
        n, _cols = sites_on_plate(site, float(prof.get("spacing_mm", 10.0)), model["free"][0], model["free"][1],
                                  int(spec.get("max_batch", 12)))
        if n > best[0]:
            best = (n, entry)
    return best


def hard_issue(prof: Dict[str, Any], spec: Dict[str, Any]) -> Optional[str]:
    """Why a board fits no station whatever it offers (None if it could fit one)."""
    if prof.get("status") != "ok":
        return prof.get("reason") or prof.get("status") or "could not be read"
    if prof.get("issues"):
        return prof["issues"][0]
    v = (prof.get("supply") or {}).get("voltage")
    vmax = float((spec.get("supplies") or {}).get("max_v", 24.0))
    if prof["supply"].get("needed") and v is not None and float(v) > vmax + 1e-6:
        return "needs a %g V supply; the environment's supplies go to %g V (supplies.max_v)" % (float(v), vmax)
    lim = spec.get("limits") or {}
    if prof.get("channels", 0) > int(lim.get("analog_inputs", 999)):
        return "needs %d analog inputs; a station has at most %d (limits.analog_inputs)" % (
            prof["channels"], int(lim.get("analog_inputs", 0)))
    for bus in prof.get("buses") or []:
        if int(lim.get(bus, 0)) < 1:
            return "needs a %s bus through the fixture; limits.%s is 0" % (bus.upper() if bus != "ethernet" else "Ethernet", bus)
    return None


def estimate(prof: Dict[str, Any], model: Dict[str, Any], spec: Dict[str, Any],
             plate_cache: Optional[Dict[Tuple[str, int], Tuple[int, str]]] = None) -> Dict[str, Any]:
    """A board type on a station without building its fixture: {"batch", "limit", "by", "entry"} (batch 0 = it
    does not fit; "reason" says why)."""
    hard = hard_issue(prof, spec)
    if hard:
        return {"batch": 0, "limit": "board", "by": {}, "entry": "front", "reason": hard}
    res = model["res"]
    by: Dict[str, int] = {}
    if prof["channels"]:
        by["inputs"] = int(res.get("ain", 0)) // prof["channels"]
    if prof["supply"]["needed"]:
        by["supplies"] = int(res.get("vout", 0))
    for bus in prof["buses"]:
        by["%s buses" % bus] = int((res.get("buses") or {}).get(bus, 0))
    key = (prof["folder"], int(model.get("uid") or 0))
    if plate_cache is not None and key in plate_cache:
        n_plate, entry = plate_cache[key]
    else:
        n_plate, entry = plate_sites(prof, model, spec)
        if plate_cache is not None:
            plate_cache[key] = (n_plate, entry)
    by["plate"] = n_plate
    by["batch limit"] = int(spec.get("max_batch", 12))
    if model.get("max_sites"):
        by["station limit"] = int(model["max_sites"])
    limit = min(by, key=lambda k: (by[k], k != "plate", k))
    n = max(0, by[limit])
    out = {"batch": n, "limit": limit, "by": by, "entry": entry}
    if n == 0:
        out["reason"] = blocker_text(prof, model, by)
    return out


def blocker_text(prof: Dict[str, Any], model: Dict[str, Any], by: Dict[str, int]) -> str:
    bits = []
    res = model["res"]
    if by.get("inputs") == 0:
        bits.append("needs %d analog inputs, %s has %d" % (prof["channels"], model["name"], res.get("ain", 0)))
    if by.get("supplies") == 0:
        bits.append("needs a switched supply, %s has none" % model["name"])
    for bus in prof.get("buses") or []:
        if by.get("%s buses" % bus) == 0:
            bits.append("needs %s, %s has none" % (bus.upper() if bus != "ethernet" else "Ethernet", model["name"]))
    if by.get("plate") == 0:
        sites = prof.get("site") or {}
        small = min(sites.values(), key=lambda s: s[0] * s[1]) if sites else [0, 0]
        bits.append("one board with its nest, clamp and divider block takes %.0f x %.0f mm; %s leaves %.0f x %.0f mm "
                    "of its %.0f x %.0f mm plate" % (small[0], small[1], model["name"], model["free"][0], model["free"][1],
                                                    model["plate"][0], model["plate"][1]))
    return "; ".join(bits) or "does not fit %s" % model["name"]


def value(n: int, spec: Dict[str, Any]) -> float:
    return 0.0 if n <= 0 else 1.0 + float(spec.get("batch_weight", 0.35)) * math.log(n, 2)


# --------------------------------------------------------------------------- #
# designing a station: spend the pin budget where it gains the most
# --------------------------------------------------------------------------- #
def _pins(res: Dict[str, Any], spec: Dict[str, Any]) -> Tuple[int, int]:
    """(pins, sockets) a station with these resources needs."""
    so = spec.get("sockets") or {}
    s = SB.socket_pins(res, int(so.get("gnd_every", 6)), int(so.get("pins", 80)))
    return sum(len(x) for x in s), len(s)


def design_station(profs: Sequence[Dict[str, Any]], spec: Dict[str, Any], plate: Sequence[float], name: str,
                   prior: Optional[Dict[str, int]] = None, keep: float = 0.3) -> Dict[str, Any]:
    """The station (resources on a plate) that makes the most of the boards, given the batches they already have
    elsewhere (``prior``: improvements for boards another station takes count ``keep`` as much)."""
    prior = prior or {}
    lim = spec.get("limits") or {}
    so = spec.get("sockets") or {}
    group = max(1, int(spec.get("ain_group", 4)))
    base = {"ain": 0, "vout": 0, "buses": {}, "dio": int(spec.get("dio", 0))}
    probe = model_from_resources(base, plate, spec, name)
    cands = []
    for p in profs:
        if hard_issue(p, spec):
            continue
        cap, _e = plate_sites(p, probe, spec)
        cap = min(cap, int(spec.get("max_batch", 12)))
        if cap < 1:
            continue
        w = float(p.get("priority", 1.0))
        old = int(prior.get(p["folder"], 0))
        cands.append((p, cap, w, old))
    if not cands:
        return base

    def batch(p: Dict[str, Any], cap: int, res: Dict[str, Any]) -> int:
        n = cap
        if p["channels"]:
            n = min(n, res["ain"] // p["channels"])
        if p["supply"]["needed"]:
            n = min(n, res["vout"])
        for bus in p["buses"]:
            n = min(n, res["buses"].get(bus, 0))
        return max(0, n)

    def total(res: Dict[str, Any]) -> float:
        t = 0.0
        for p, cap, w, old in cands:
            n = batch(p, cap, res)
            if n > old:
                t += w * (value(n, spec) - value(old, spec)) * (keep if old else 1.0)
        return t

    def grown(res: Dict[str, Any], p: Dict[str, Any], n: int) -> Optional[Dict[str, Any]]:
        new = {"ain": res["ain"], "vout": res["vout"], "buses": dict(res["buses"]), "dio": res["dio"]}
        if p["channels"]:
            new["ain"] = max(new["ain"], int(math.ceil(p["channels"] * n / float(group))) * group)
            if new["ain"] > int(lim.get("analog_inputs", 96)):
                new["ain"] = (int(lim.get("analog_inputs", 96)) // group) * group
                if new["ain"] < p["channels"] * n:
                    return None
        if p["supply"]["needed"]:
            new["vout"] = max(new["vout"], n)
            if new["vout"] > int(lim.get("supplies", 8)):
                return None
        for bus in p["buses"]:
            new["buses"][bus] = max(new["buses"].get(bus, 0), n)
            if new["buses"][bus] > int(lim.get(bus, 0)):
                return None
        if _pins(new, spec)[1] > int(so.get("max", 2)):
            return None
        return new

    res = base
    cur = total(res)
    for _ in range(4000):
        best = None
        cur_pins = _pins(res, spec)[0]
        tried = set()
        for p, cap, w, old in cands:
            n0 = batch(p, cap, res)
            for n in sorted({n0 + 1, min(cap, n0 + 2), cap}):
                if n <= n0 or n > cap:
                    continue
                new = grown(res, p, n)
                if new is None:
                    continue
                sig = json.dumps(new, sort_keys=True)
                if sig in tried:
                    continue
                tried.add(sig)
                gain = total(new) - cur
                if gain <= 1e-9:
                    continue
                cost = max(1, _pins(new, spec)[0] - cur_pins)
                key = (gain / cost, gain)
                if best is None or key > best[0]:
                    best = (key, new, gain)
        if best is None:
            break
        res, cur = best[1], cur + best[2]
    return res


def design(profs: Sequence[Dict[str, Any]], spec: Dict[str, Any], fixed: Sequence[Dict[str, Any]] = (),
           new_stations: bool = True, progress: Optional[Callable[[str, float], None]] = None) -> Dict[str, Any]:
    """The environment's stations: the ``fixed`` ones (existing) first, then designed ones up to max_stations, each
    for the boards the earlier ones cannot take or take only a few of. Returns {"stations": [model], "estimates":
    {folder: [estimate per station]}}."""
    models = [dict(m) for m in fixed]
    cache: Dict[Tuple[str, int], Tuple[int, str]] = {}
    plates = [spec["plate_mm"]] + ([spec["large_plate_mm"]] if spec.get("large_plate_mm") else [])
    maxst = max(len(models), int(spec.get("max_stations", 3)))

    def best_of(ms: Sequence[Dict[str, Any]]) -> Dict[str, int]:
        return {p["folder"]: max([estimate(p, m, spec, cache)["batch"] for m in ms] or [0]) for p in profs}
    while new_stations and len(models) < maxst:
        name = "station-%d" % (len(models) + 1)
        if progress:
            progress("Designing %s" % name, 0.0)
        prior = best_of(models)
        options = []
        # the first station has the plate asked for (plate_mm); a further one may get the large plate for the boards
        # that need it. With one station allowed, it gets the large plate if that takes more boards.
        for plate in (plates[:1] if (not models and maxst > 1) else plates):
            m = model_from_resources(design_station(profs, spec, plate, name, prior,
                                                    keep=0.3 if spec.get("rebalance") else 0.0), plate, spec, name)
            new_n = {p["folder"]: estimate(p, m, spec, cache)["batch"] for p in profs}
            gained = sum(float(p.get("priority", 1.0)) for p in profs if new_n[p["folder"]] >= 1 and prior[p["folder"]] == 0)
            better = sum(float(p.get("priority", 1.0)) * max(0.0, value(new_n[p["folder"]], spec) - value(prior[p["folder"]], spec))
                         for p in profs)
            options.append((gained, better, m))
        gained, better, m = options[0]                      # the usual plate, unless the large one takes more boards
        if len(options) > 1 and options[1][0] > gained + 1e-9:
            gained, better, m = options[1]
        m["plate_options"] = [{"plate_mm": o[2]["plate"], "boards_gained": round(o[0], 2)} for o in options]
        if models and gained < float(spec.get("min_boards_per_station", 1)) - 1e-9:
            break
        models.append(m)
        if gained <= 0 and better <= 0:                     # nothing fits: the station shows what it would be
            break
    # rebalance: every designed station again, knowing what the others take (without it the stations are made in
    # order, each for the boards the earlier ones cannot take, and stay that way)
    for _round in range(2 if spec.get("rebalance") else 0):
        changed = False
        for j, m in enumerate(models):
            if m.get("fixed"):
                continue
            others = [x for i, x in enumerate(models) if i != j]
            res = design_station(profs, spec, m["plate"], m["name"], best_of(others), keep=0.0 if others else 0.3)
            m2 = model_from_resources(res, m["plate"], spec, m["name"])
            m2["plate_options"] = m.get("plate_options")
            trial = list(models)
            trial[j] = m2
            old_total, new_total = _total_value(profs, models, spec, cache), _total_value(profs, trial, spec, cache)
            if new_total > old_total + 1e-6 or (abs(new_total - old_total) <= 1e-6 and m2["pins"] < m["pins"]):
                models[j] = m2
                changed = True
        if not changed:
            break
    ests = {p["folder"]: [estimate(p, m, spec, cache) for m in models] for p in profs}
    return {"stations": models, "estimates": ests}


def _total_value(profs, models, spec, cache) -> float:
    t = 0.0
    for p in profs:
        n = max([estimate(p, m, spec, cache)["batch"] for m in models] or [0])
        t += float(p.get("priority", 1.0)) * value(n, spec)
    return t


SWITCH = 1.5         # a board moves to a later station only for a batch this much bigger


def assign(profs: Sequence[Dict[str, Any]], models: Sequence[Dict[str, Any]], ests: Dict[str, List[Dict[str, Any]]],
           rebalance: bool = False) -> Dict[str, Optional[int]]:
    """Every board to the first station that takes it (the first stations are the main ones, later ones are for
    what they cannot take); with ``rebalance`` a later station also takes a board when its batch is SWITCH times
    as big. None if no station takes it."""
    out: Dict[str, Optional[int]] = {}
    for p in profs:
        e = ests.get(p["folder"]) or []
        best = None
        for i, x in enumerate(e):
            if x["batch"] >= 1 and (best is None or (rebalance and x["batch"] >= SWITCH * e[best]["batch"])):
                best = i
        out[p["folder"]] = best
    return out


def assign_best(profs: Sequence[Dict[str, Any]], ests: Dict[str, List[Dict[str, Any]]], spec: Dict[str, Any]) -> Dict[str, Optional[int]]:
    """Every board to the station that gives it the biggest batch (stations made for groups of boards: none is
    the main one); None if no station takes it."""
    out: Dict[str, Optional[int]] = {}
    for p in profs:
        e = ests.get(p["folder"]) or []
        best = None
        for i, x in enumerate(e):
            if x["batch"] >= 1 and (best is None or x["batch"] > e[best]["batch"]):
                best = i
        out[p["folder"]] = best
    return out


# --------------------------------------------------------------------------- #
# layouts: one main station, or stations each made for a group of boards, chosen by worth minus cost
# --------------------------------------------------------------------------- #
def _features(p: Dict[str, Any], free_area: float) -> List[float]:
    """What a board asks of a station, as numbers that compare: bus pins, analog inputs, supply, plate share."""
    v = [SB.BUS_PINS.get(b, 2) / 2.0 if b in (p.get("buses") or []) else 0.0 for b in BUSES]
    v.append(min(4.0, int(p.get("channels") or 0) / 8.0))
    v.append(1.0 if (p.get("supply") or {}).get("needed") else 0.0)
    site = min((s for s in (p.get("site") or {}).values()), key=lambda s: s[0] * s[1], default=[50.0, 50.0])
    v.append(min(3.0, 6.0 * site[0] * site[1] / max(1.0, free_area)))
    return v


def kmeans(profs: Sequence[Dict[str, Any]], k: int, free_area: float, rounds: int = 25) -> List[List[Dict[str, Any]]]:
    """The boards in k groups that need much the same (deterministic: the first centre is the board that needs
    the most, each next one the board farthest from the centres so far; weighted by priority)."""
    pts = [(p, _features(p, free_area), float(p.get("priority", 1.0))) for p in profs]
    if len(pts) <= k:
        return [[p] for p, _f, _w in pts]

    def d2(a, b):
        return sum((x - y) ** 2 for x, y in zip(a, b))
    centres = [max(pts, key=lambda t: (sum(t[1]), t[0]["folder"]))[1]]
    while len(centres) < k:
        centres.append(max(pts, key=lambda t: (min(d2(t[1], c) for c in centres), t[0]["folder"]))[1])
    groups: List[List[int]] = []
    for _ in range(rounds):
        groups = [[] for _c in centres]
        for i, (_p, f, _w) in enumerate(pts):
            groups[min(range(len(centres)), key=lambda j: (d2(f, centres[j]), j))].append(i)
        new = []
        for j, g in enumerate(groups):
            if not g:
                new.append(centres[j])
                continue
            wsum = sum(pts[i][2] for i in g) or 1.0
            new.append([sum(pts[i][1][d] * pts[i][2] for i in g) / wsum for d in range(len(centres[j]))])
        if new == centres:
            break
        centres = new
    return [[pts[i][0] for i in g] for g in groups if g]


def focus(group: Sequence[Dict[str, Any]]) -> str:
    """What a group of boards has in common, in a few words (e.g. "CAN, analog")."""
    n = max(1, len(group))
    bits = []
    for bus in BUSES:
        if sum(1 for p in group if bus in (p.get("buses") or [])) >= 0.5 * n:
            bits.append({"ethernet": "Ethernet", "rs485": "RS-485"}.get(bus, bus.upper()))
    if sum(int(p.get("channels") or 0) for p in group) / float(n) >= 10:
        bits.append("many analog inputs")
    big = sum(1 for p in group if min((s[0] * s[1] for s in (p.get("site") or {}).values()), default=0) > 15000)
    if big >= 0.5 * n:
        bits.append("large boards")
    return ", ".join(bits) or "general"


def _layout_from_groups(name: str, title: str, groups: Sequence[Sequence[Dict[str, Any]]], profs: Sequence[Dict[str, Any]],
                        spec: Dict[str, Any], cache) -> Dict[str, Any]:
    """A station per group (made for that group's boards), then, while stations are left, one for what fits none."""
    models: List[Dict[str, Any]] = []
    for gi, grp in enumerate(groups):
        nm = "station-%d" % (gi + 1)
        plate = spec["plate_mm"]
        res = design_station([p for p in grp], spec, plate, nm)
        m = model_from_resources(res, plate, spec, nm)
        if spec.get("large_plate_mm"):                  # a group of big boards may need the large plate
            n_small = sum(1 for p in grp if estimate(p, m, spec, cache)["batch"] >= 1)
            big = spec["large_plate_mm"]
            m2 = model_from_resources(design_station(list(grp), spec, big, nm), big, spec, nm)
            if sum(1 for p in grp if estimate(p, m2, spec, cache)["batch"] >= 1) > n_small:
                m = m2
        m["focus"] = focus(grp)
        models.append(m)
    maxst = int(spec.get("max_stations", 3))
    while len(models) < maxst:
        left = [p for p in profs if not hard_issue(p, spec) and
                max([estimate(p, m, spec, cache)["batch"] for m in models] or [0]) == 0]
        if len(left) < max(1, int(spec.get("min_boards_per_station", 1))):
            break
        nm = "station-%d" % (len(models) + 1)
        best = None
        for plate in [spec["plate_mm"]] + ([spec["large_plate_mm"]] if spec.get("large_plate_mm") else []):
            m = model_from_resources(design_station(left, spec, plate, nm), plate, spec, nm)
            n = sum(1 for p in left if estimate(p, m, spec, cache)["batch"] >= 1)
            if best is None or n > best[0]:
                best = (n, m)
        if not best or best[0] < max(1, int(spec.get("min_boards_per_station", 1))):
            break
        best[1]["focus"] = "boards the others cannot take"
        models.append(best[1])
    return {"name": name, "title": title, "models": models, "assign": "best"}


def layouts(profs: Sequence[Dict[str, Any]], spec: Dict[str, Any], cache=None,
            progress: Optional[Callable[[str, float], None]] = None) -> List[Dict[str, Any]]:
    """The station layouts worth comparing for these boards, each with its worth, cost and net (see choose)."""
    cache = cache if cache is not None else {}
    ok = [p for p in profs if p.get("status") == "ok" and not hard_issue(p, spec)]
    out = []
    g = design(profs, spec, (), True, progress=progress)
    for m in g["stations"]:
        m.setdefault("focus", "main" if m is g["stations"][0] else "boards the others cannot take")
    out.append({"name": "general", "title": "One main station, further ones for the boards it cannot take",
                "models": g["stations"], "assign": "first"})
    maxst = int(spec.get("max_stations", 3))
    want = str(spec.get("layout") or "auto")
    if maxst >= 2 and len(ok) >= 4 and want != "general":
        free = float(spec["plate_mm"][0]) * float(spec["plate_mm"][1]) * 0.85
        for k in range(2, maxst + 1):
            if want not in ("auto", "specialized-%d" % k):
                continue
            if progress:
                progress("Comparing layouts: %d stations for groups of boards" % k, 0.0)
            grp = kmeans(ok, k, free)
            if len(grp) == k:
                out.append(_layout_from_groups("specialized-%d" % k, "%d stations, each for boards that need much the "
                                               "same" % k, grp, profs, spec, cache))
        for bus in BUSES:
            if want not in ("auto", "split-%s" % bus):
                continue
            users = [p for p in ok if bus in (p.get("buses") or [])]
            rest = [p for p in ok if bus not in (p.get("buses") or [])]
            if len(users) >= 3 and len(rest) >= 3:
                if progress:
                    progress("Comparing layouts: a station for the %s boards" % bus.upper(), 0.0)
                out.append(_layout_from_groups("split-%s" % bus, "A station for the %d boards with %s, one for the rest" % (
                    len(users), {"ethernet": "Ethernet", "rs485": "RS-485"}.get(bus, bus.upper())), [users, rest], profs,
                    spec, cache))
    for lay in out:
        lay.update(score_layout(lay, profs, spec, cache))
    return out


def score_layout(lay: Dict[str, Any], profs: Sequence[Dict[str, Any]], spec: Dict[str, Any], cache) -> Dict[str, Any]:
    """A layout's estimates: boards that fit, boards per load, worth, cost (stations, programmers, fixtures), net."""
    from . import upgrades as U
    models = lay["models"]
    ests = {p["folder"]: [estimate(p, m, spec, cache) for m in models] for p in profs}
    where_i = assign_best(profs, ests, spec) if lay.get("assign") == "best" else assign(profs, models, ests,
                                                                                         bool(spec.get("rebalance")))
    where = {p["folder"]: ((models[where_i[p["folder"]]]["name"], ests[p["folder"]][where_i[p["folder"]]]["batch"])
                           if where_i.get(p["folder"]) is not None else (None, 0)) for p in profs}
    c = U.costs(spec)
    pr = {p["folder"]: p for p in profs}
    fit = [f for f, (st, n) in where.items() if st and n >= 1]
    worth = sum(float(pr[f].get("priority", 1.0)) * value(where[f][1], spec) for f in fit) * float(c["value_per_board"])
    cost = U.environment_cost(models, spec, where, pr)
    return {"boards_fit": len(fit), "fit_weight": round(sum(float(pr[f].get("priority", 1.0)) for f in fit), 3),
            "slots": sum(where[f][1] for f in fit), "worth": round(worth, 0), "cost": round(cost["total"], 0),
            "net": round(worth - cost["total"], 0), "cost_parts": cost, "where": where,
            "stations": [{"name": m["name"], "focus": m.get("focus"), "plate_mm": m["plate"], "resources": m["res"],
                          "boards": sum(1 for f in fit if where[f][0] == m["name"]),
                          "cost": cost["stations"].get(m["name"])} for m in models]}


def choose(lays: Sequence[Dict[str, Any]], spec: Dict[str, Any]) -> Dict[str, Any]:
    """The layout the spec asks for by name, else (auto) the one worth the most net, among those that fit as many
    boards as any (coverage_first)."""
    want = str(spec.get("layout") or "auto")
    named = [l_ for l_ in lays if l_["name"] == want]
    if named:
        return named[0]
    pool = list(lays)
    if spec.get("coverage_first", True):
        top = max(l_["fit_weight"] for l_ in lays)
        pool = [l_ for l_ in lays if l_["fit_weight"] >= top - 1e-9]
    return max(pool, key=lambda l_: (l_["net"], l_["slots"], -len(l_["models"]), l_["name"] == "general"))


def layout_summary(lays: Sequence[Dict[str, Any]], chosen: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"name": l_["name"], "title": l_["title"], "chosen": l_ is chosen, "boards_fit": l_["boards_fit"],
             "slots": l_["slots"], "worth": l_["worth"], "cost": l_["cost"], "net": l_["net"],
             "stations": l_["stations"]} for l_ in lays]


# --------------------------------------------------------------------------- #
# the plan: stations made real, every board checked on its station
# --------------------------------------------------------------------------- #
def station_spec(m: Dict[str, Any], spec: Dict[str, Any], title: str) -> Dict[str, Any]:
    """What station_board needs to draw station ``m``."""
    so = spec.get("sockets") or {}
    return {"name": m["name"], "title": title, "plate_mm": m["plate"], "resources": m["res"],
            "gnd_every": int(so.get("gnd_every", 6)), "max_pins": int(so.get("pins", 80)),
            "lines": SB.requirement_lines(m["res"], ["%s - every fixture plugs into J1%s" % (
                spec.get("name", "Test environment"), " and J2" if m.get("sockets", 1) > 1 else "")])}


def check_board(job: Dict[str, Any]) -> Dict[str, Any]:
    """A board on its station with a real fixture layout (station.plan_board; runs in a worker process)."""
    from .station import plan_board
    try:
        row = plan_board(job["pcb"], job["fac"], None, hint=job.get("hint"))
    except Exception as exc:
        return {"status": "error", "batch": 0, "reason": "%s: %s" % (type(exc).__name__, exc)}
    row.pop("project", None)
    return row


CHECK_REV = 1            # bumped when station.plan_board's answer for the same board and station changes


def _check_key(job: Dict[str, Any]) -> Optional[str]:
    """What a board's check on a station depends on: its files, the station, the hint, the version."""
    from .project import locate
    h = hashlib.sha1()
    try:
        pcb, _sch, pro = locate(job["pcb"])
    except Exception:  # noqa: BLE001
        return None
    for f in (pcb, os.path.join(os.path.dirname(pcb), "pcbtestkit.json")):
        if f and os.path.exists(f):
            with open(f, "rb") as fh:
                h.update(fh.read())
    fac = {k: v for k, v in job["fac"].items() if not str(k).startswith("_")}
    h.update(json.dumps([fac, job.get("hint"), CHECK_REV, PROFILE_REV, __version__], sort_keys=True, default=str).encode())
    return h.hexdigest()


def _cached_checks(work: List[Dict[str, Any]], jobs: int, done, cache_dir: str) -> List[Dict[str, Any]]:
    """check_board for every job, reusing the answer an earlier plan or build got for the same board and station
    (a board added to an environment does not make the others be checked again)."""
    from .station import run_jobs
    out: List[Optional[Dict[str, Any]]] = [None] * len(work)
    keys = [_check_key(j) for j in work]
    todo = []
    for i, (j, k) in enumerate(zip(work, keys)):
        if k:
            try:
                with open(os.path.join(cache_dir, k[:24] + ".json"), encoding="utf-8") as fh:
                    out[i] = json.load(fh)
                done(i + 1, len(work), j, out[i])
                continue
            except (OSError, ValueError):
                pass
        todo.append(i)
    res = run_jobs(check_board, [work[i] for i in todo], jobs, done) if todo else []
    for i, r in zip(todo, res):
        out[i] = r
        if keys[i] and r.get("status") in ("ok", "does not fit"):
            try:
                os.makedirs(cache_dir, exist_ok=True)
                with open(os.path.join(cache_dir, keys[i][:24] + ".json"), "w", encoding="utf-8") as fh:
                    json.dump(r, fh)
            except OSError:
                pass
    return [r or {"status": "error", "reason": "not checked"} for r in out]


def plan_environment(inputs: Sequence[str], spec: Optional[Dict[str, Any]] = None, environment: Optional[str] = None,
                     stations: Sequence[str] = (), new_stations: bool = True, jobs: int = 0,
                     work_dir: Optional[str] = None, progress: Optional[Callable[[str, float], None]] = None,
                     verify: bool = True) -> Dict[str, Any]:
    """Profile the collection, design (or take) the stations, check every board on its station. ``work_dir`` gets
    the station boards (stations/<name>/) and the profile cache; a temporary folder when None.
    Returns the environment (see SCHEMA) with "_facilities" (station name -> facility) for build_environment."""
    t0 = time.time()

    def step(msg: str, frac: float) -> None:
        if progress:
            progress(msg, max(0.0, min(1.0, frac)))
    jobs = int(jobs) if jobs else max(1, min(8, (os.cpu_count() or 2) - 1))
    spec = spec or default_spec()
    keep_tmp = work_dir is not None
    work_dir = work_dir or tempfile.mkdtemp(prefix="pcbtk_env_")
    warns: List[str] = []
    # (name, the user's own or edited, board path, title: a station an environment build made, None: your own)
    fixed_facs: List[Tuple[str, bool, str, Optional[str]]] = []
    if not environment and keep_tmp:
        prev = _try_env_json(work_dir).get("stations") or []
        titles = {st["name"]: st.get("title") for st in prev}
        edited = edited_stations(work_dir)
        for name, pcb in edited:                                   # a station board edited since: keep it
            keep = os.path.join(tempfile.mkdtemp(prefix="pcbtk_edited_"), os.path.basename(pcb))
            shutil.copy2(pcb, keep)
            fixed_facs.append((name, True, keep, titles.get(name) or name))
            warns.append("%s: its board was edited since the last build, so it is kept as it is" % name)
        for st in prev:                                            # grown or upgraded: it is hardware by now
            pcb = os.path.join(work_dir, st.get("board") or "")
            if st["name"] in {n for n, _p in edited} or not st.get("board") or not os.path.exists(pcb):
                continue
            if st.get("fixed") or str(st.get("rev") or "A") != "A":
                keep = os.path.join(tempfile.mkdtemp(prefix="pcbtk_kept_"), os.path.basename(pcb))
                shutil.copy2(pcb, keep)
                fixed_facs.append((st["name"], bool(st.get("own")), keep, st.get("title") or st["name"]))
                warns.append("%s: kept as it is (boards were added to it or it was upgraded); build into a new folder "
                             "to design the stations again" % st["name"])
    if environment:
        try:
            with open(os.path.join(environment, "environment.json"), encoding="utf-8") as fh:
                old = json.load(fh)
        except (OSError, ValueError) as exc:
            raise ValueError("%s is not an environment folder (no readable environment.json): %s" % (environment, exc))
        for st in old.get("stations") or []:
            pcb = os.path.join(environment, st.get("board") or "")
            if not os.path.exists(pcb):
                warns.append("station %s: its board %s is missing; it is designed again" % (st.get("name"), st.get("board")))
                continue
            own = bool(st.get("own")) or bool(st.get("board_sha") and board_sha(pcb) != st["board_sha"])
            fixed_facs.append((st["name"], own, pcb, st.get("title") or st["name"]))   # read below (maybe edited)
    for i, s in enumerate(stations):
        fixed_facs.append((os.path.splitext(os.path.basename(s))[0] or "station-%d" % (i + 1), True, s, None))
    from .station import load_station
    fixed_models, facs, sources = [], {}, {}
    names_used = set()
    for name, own, src, title in fixed_facs:
        extra = {"analog": {"max_v": float((spec.get("analog") or {}).get("max_v", 10.0))}}
        if title and src.endswith(".kicad_pcb"):            # a station an environment build made: read it as it was
            fac, w2 = F.facility_from_pcb(src, ident=name, name=title, extra=extra)
            fac["_station_path"] = os.path.abspath(src)
            fac["batch"] = {"max_sites": int(spec.get("max_batch", 12))}
        else:
            fac, w2 = load_station(src, extra=extra if src.endswith((".kicad_pcb", ".kicad_pro")) else None)
        warns += ["%s: %s" % (name, w) for w in w2]
        base = name
        k = 2
        while name in names_used:
            name, k = "%s-%d" % (base, k), k + 1
        names_used.add(name)
        fixed_models.append(dict(model_from_facility(fac, name), own=bool(own)))
        facs[name] = fac
        sources[name] = os.path.abspath(src)
    step("Finding the boards", 0.01)
    boards, w3 = discover(inputs)
    warns += w3
    if not boards:
        raise ValueError("no KiCad boards found in %s" % ", ".join(inputs))
    step("Profiling %d boards (%d at a time)" % (len(boards), jobs), 0.02)
    profs = profile_all(boards, spec, os.path.join(work_dir, "profiles"), jobs,
                        progress=lambda m, f: step(m, 0.02 + 0.5 * f))
    step("Designing the stations", 0.53)
    lay_info: List[Dict[str, Any]] = []
    assign_mode = "first"
    if not fixed_models and new_stations:            # nothing to keep: compare layouts by worth and cost
        lays = layouts(profs, spec, {}, progress=lambda m, f: step(m, 0.53))
        chosen = choose(lays, spec)
        lay_info = layout_summary(lays, chosen)
        models = chosen["models"]
        assign_mode = chosen.get("assign", "first")
        step("Layout: %s (%d stations, %d boards fit)" % (chosen["name"], len(models), chosen["boards_fit"]), 0.55)
    else:
        dz = design(profs, spec, fixed_models, new_stations=new_stations, progress=lambda m, f: step(m, 0.53))
        models = dz["stations"]
    for i, m in enumerate(models):
        if m["name"] in facs:
            continue
        while m["name"] in names_used:
            m["name"] = "station-%d" % (len(names_used) + 1)
        names_used.add(m["name"])
        sdir = os.path.join(work_dir, "stations", m["name"])
        title = "%s %s" % (spec.get("name", "Test environment"), m["name"].replace("station-", "station "))
        if m.get("focus") and assign_mode == "best":         # a station made for a group of boards says which
            title += " (%s)" % m["focus"]
        pcb = SB.write_station(station_spec(m, spec, title), sdir)
        fac, w4 = F.facility_from_pcb(pcb, ident=m["name"], name=title,
                                      extra={"analog": {"max_v": float((spec.get("analog") or {}).get("max_v", 10.0))}})
        warns += ["%s: %s" % (m["name"], w) for w in w4]
        fac["_station_path"] = pcb
        fac["batch"] = {"max_sites": int(spec.get("max_batch", 12))}
        facs[m["name"]] = fac
        sources[m["name"]] = pcb
        models[i] = dict(model_from_facility(fac, m["name"]), fixed=False, plate_options=m.get("plate_options"),
                         focus=m.get("focus"))
    cache = {}
    ests = {p["folder"]: [estimate(p, m, spec, cache) for m in models] for p in profs}
    where = assign_best(profs, ests, spec) if assign_mode == "best" else assign(profs, models, ests,
                                                                                 bool(spec.get("rebalance")))
    rows: Dict[str, Dict[str, Any]] = {}
    if verify:
        from .station import run_jobs
        todo = [p for p in profs if where.get(p["folder"]) is not None]
        step("Checking %d boards on their stations with real fixture layouts" % len(todo), 0.6)
        tries = {p["folder"]: [where[p["folder"]]] + [i for i in sorted(range(len(models)), key=lambda i: -ests[p["folder"]][i]["batch"])
                                                      if ests[p["folder"]][i]["batch"] >= 1 and i != where[p["folder"]]]
                 for p in todo}
        pending = list(todo)
        rnd = 0
        while pending and rnd < len(models):
            work = []
            for p in pending:
                i = tries[p["folder"]][rnd] if rnd < len(tries[p["folder"]]) else None
                if i is None:
                    continue
                m = models[i]
                work.append({"pcb": p["pcb"], "fac": facs[m["name"]], "station": i, "folder": p["folder"],
                             "hint": min(ests[p["folder"]][i]["batch"], int(spec.get("max_batch", 12)))})

            def done(n: int, total: int, job: Dict[str, Any], res: Dict[str, Any]) -> None:
                step("Checked %d of %d: %s on %s, %s" % (n, total, res.get("name", job["folder"]), models[job["station"]]["name"],
                                                       "batch %d" % res.get("batch", 0) if res.get("status") == "ok"
                                                       else res.get("status")), 0.6 + 0.35 * n / max(1, total))
            res = _cached_checks(work, jobs, done, os.path.join(work_dir, "profiles", "checks"))
            nxt = []
            for job, r in zip(work, res):
                if r.get("status") == "ok" and r.get("batch", 0) >= 1:
                    r["station"] = models[job["station"]]["name"]
                    rows[job["folder"]] = r
                else:
                    p = next(x for x in todo if x["folder"] == job["folder"])
                    p.setdefault("tried", []).append({"station": models[job["station"]]["name"],
                                                      "reason": r.get("reason") or r.get("status")})
                    nxt.append(p)
            pending = nxt
            rnd += 1
    else:
        for p in profs:
            i = where.get(p["folder"])
            if i is not None:
                e = ests[p["folder"]][i]
                rows[p["folder"]] = {"name": p["name"], "status": "ok", "batch": e["batch"], "limit": e["limit"],
                                     "station": models[i]["name"], "entry": e["entry"] if e["entry"] != "front" else None,
                                     "estimated": True}
    env = _environment(profs, models, ests, rows, spec, facs, sources, warns, work_dir)
    if lay_info:
        env["layouts"] = lay_info
        env["layout"] = next(x["name"] for x in lay_info if x["chosen"])
    env["seconds"] = round(time.time() - t0, 1)
    if not keep_tmp:
        env["_tmp"] = work_dir
    step("Planned: %d of %d boards on %d station(s)" % (env["summary"]["compatible"], env["summary"]["boards"],
                                                         len(models)), 1.0)
    return env


def _try_env_json(d: str) -> Dict[str, Any]:
    try:
        with open(os.path.join(d, "environment.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def board_sha(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha1(fh.read()).hexdigest()
    except OSError:
        return None


def edited_stations(out_dir: str) -> List[Tuple[str, str]]:
    """(name, board) of the stations in an earlier build's folder whose board changed since it was made."""
    try:
        with open(os.path.join(out_dir, "environment.json"), encoding="utf-8") as fh:
            old = json.load(fh)
    except (OSError, ValueError):
        return []
    out = []
    for st in old.get("stations") or []:
        pcb = os.path.join(out_dir, st.get("board") or "")
        sha = board_sha(pcb) if st.get("board") else None
        if sha and st.get("board_sha") and sha != st["board_sha"]:
            out.append((st["name"], pcb))
    return out


def _environment(profs, models, ests, rows, spec, facs, sources, warns, work_dir) -> Dict[str, Any]:
    boards = []
    for p in profs:
        r = rows.get(p["folder"])
        e_all = ests.get(p["folder"]) or []
        item = {"name": p["name"], "folder": p["folder"], "project": _project_name(p["pcb"]), "_pcb": p["pcb"],
                "priority": p.get("priority", 1.0), "status": "compatible" if r else ("error" if p.get("status") == "error"
                                                                                     else p.get("status") if p.get("status") == "excluded"
                                                                                     else "incompatible"),
                "size_mm": p.get("size_mm"), "channels": p.get("channels"), "supply": p.get("supply"),
                "buses": p.get("buses") or [], "programming": sorted({x["protocol"] for x in p.get("programming") or []}),
                "probe_side": p.get("probe_side"), "probes": p.get("probes"), "coverage_pct": p.get("coverage_pct"),
                "estimates": {models[i]["name"]: {"batch": e["batch"], "limit": e["limit"]} for i, e in enumerate(e_all)},
                # what upgrades.py needs to estimate the board on a changed station without profiling it again
                "site_mm": p.get("site"), "spacing_mm": p.get("spacing_mm"), "issues": list(p.get("issues") or [])}
        if p.get("tried"):
            item["tried"] = p["tried"]
        if r:
            by = dict(r.get("max_by") or {})
            item["by"] = dict(by)
            si = next((i for i, m in enumerate(models) if m["name"] == r["station"]), None)
            if si is not None and si < len(e_all):
                item["plate_est"] = e_all[si]["by"].get("plate")
            if si is not None and si < len(e_all) and "plate" not in by:
                by["plate"] = e_all[si]["by"].get("plate", 0)          # the plate estimate, when the fixture fit it
            binding = sorted(k for k, v in by.items() if int(v) == int(r["batch"])) or [r.get("limit") or "-"]
            item.update(station=r["station"], batch=int(r["batch"]), limit=" = ".join(binding), entry=r.get("entry"),
                        cols=r.get("cols"), row=r)
        else:
            item["reason"] = why_not(p, models, e_all, spec)
        boards.append(item)
    ranked = rank(boards, spec)
    st_out = []
    for m in models:
        mine = [b for b in boards if b.get("station") == m["name"]]
        fac = facs.get(m["name"]) or {}
        st_out.append({"name": m["name"], "title": fac.get("name", m["name"]), "fixed": bool(m.get("fixed")),
                       "own": bool(m.get("own")), "focus": m.get("focus"),
                       "plate_options": m.get("plate_options") or [],
                       "board": os.path.relpath(sources[m["name"]], work_dir) if sources.get(m["name"], "").startswith(work_dir)
                       else os.path.basename(sources.get(m["name"], "")),
                       "board_sha": board_sha(sources.get(m["name"], "")),
                       "resources": m["res"], "plate_mm": m["plate"], "free_mm": m["free"], "sockets": m["sockets"],
                       "pins": m["pins"], "boards": [b["folder"] for b in mine],
                       "requirements": requirements(m, fac, mine, spec)})
    n_ok = sum(1 for b in boards if b["status"] == "compatible")
    return {"schema": SCHEMA, "generator": "PCB TestKit %s" % __version__, "name": spec.get("name"), "spec": spec,
            "summary": {"boards": len(boards), "compatible": n_ok, "incompatible": sum(1 for b in boards if b["status"] == "incompatible"),
                        "errors": sum(1 for b in boards if b["status"] == "error"),
                        "excluded": sum(1 for b in boards if b["status"] == "excluded"),
                        "stations": len(models), "board_slots": sum(b.get("batch", 0) for b in boards if b["status"] == "compatible"),
                        "compatible_pct": round(100.0 * n_ok / max(1, len(boards)), 1)},
            "stations": st_out, "boards": boards, "ranking": ranked, "warnings": warns,
            "_facilities": facs}


def _project_name(pcb: str) -> str:
    return os.path.basename(os.path.dirname(pcb)) or os.path.splitext(os.path.basename(pcb))[0]


def why_not(p: Dict[str, Any], models: Sequence[Dict[str, Any]], ests: Sequence[Dict[str, Any]], spec: Dict[str, Any]) -> str:
    """Why a board fits no station: its own issue, the station it came closest on, or what the real fixture said."""
    if p.get("status") == "excluded":
        return p.get("reason", "excluded")
    hard = hard_issue(p, spec)
    if hard:
        return hard
    if p.get("tried"):
        return "; ".join("%s: %s" % (t["station"], t["reason"]) for t in p["tried"])[:400]
    if not ests:
        return "no station"
    best = min(range(len(ests)), key=lambda i: sum(1 for v in ests[i]["by"].values() if v == 0))
    return ests[best].get("reason") or "does not fit"


# --------------------------------------------------------------------------- #
# ranking and requirements
# --------------------------------------------------------------------------- #
def pins_per_board(b: Dict[str, Any]) -> int:
    return int(b.get("channels") or 0) + (1 if (b.get("supply") or {}).get("needed") else 0) + \
        sum(SB.BUS_PINS.get(x, 2) for x in b.get("buses") or [])


def rank(boards: Sequence[Dict[str, Any]], spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The boards in order of how much it matters that they are covered: compatible ones by priority and batch
    (what a production run gains), then the ones that do not fit by priority and by how few pins they would take
    (the easiest to bring in first). Each row says why it ranks where it does."""
    def key_ok(b):
        return (-float(b.get("priority", 1.0)), -value(b.get("batch", 0), spec), pins_per_board(b), b["name"].lower())

    def key_no(b):
        return (-float(b.get("priority", 1.0)), b["status"] != "incompatible", pins_per_board(b), b["name"].lower())
    ok = sorted([b for b in boards if b["status"] == "compatible"], key=key_ok)
    no = sorted([b for b in boards if b["status"] != "compatible"], key=key_no)
    out = []
    for i, b in enumerate(ok + no, 1):
        out.append({"rank": i, "name": b["name"], "folder": b["folder"], "priority": b.get("priority", 1.0),
                    "status": b["status"], "station": b.get("station"), "batch": b.get("batch", 0), "limit": b.get("limit"),
                    "channels": b.get("channels"), "supply_v": (b.get("supply") or {}).get("voltage"),
                    "buses": b.get("buses") or [], "programming": b.get("programming") or [],
                    "pins_per_board": pins_per_board(b), "size_mm": b.get("size_mm"), "reason": b.get("reason", "")})
    return out


BUS_NOTE = {
    "can": "a CAN transceiver per instance (ISO 11898, 120 ohm termination you can switch)",
    "uart": "a 3.3 V UART per instance (TX/RX crossed: the board's TX goes to the station's RX)",
    "i2c": "an I2C controller per instance, with pull-ups",
    "rs485": "a half-duplex RS-485 transceiver per instance",
    "usb": "a USB 2.0 host port per instance (full speed through pogo pins: keep the traces short)",
    "ethernet": "a 100BASE-TX port with magnetics per instance (the pairs go through pogo pins: keep them under 50 mm)",
}
PROG_NOTE = {"swd": "SWD probe", "jtag": "JTAG adapter", "isp": "AVR ISP programmer", "avr_isp": "AVR ISP programmer",
             "icsp": "PIC ICSP programmer", "pic_icsp": "PIC ICSP programmer", "updi": "UPDI programmer",
             "spi": "SPI flash programmer", "uart": "UART boot loader cable", "?": "programmer"}


def requirements(m: Dict[str, Any], fac: Dict[str, Any], mine: Sequence[Dict[str, Any]], spec: Dict[str, Any]) -> Dict[str, Any]:
    """What station m has to offer, exactly, and which boards make it so."""
    res = m["res"]

    def most(key: Callable[[Dict[str, Any]], float]) -> Optional[str]:
        best = max(mine, key=key, default=None)
        return best["name"] if best is not None and key(best) > 0 else None
    volts = sorted({float((b.get("supply") or {}).get("voltage")) for b in mine
                    if (b.get("supply") or {}).get("needed") and (b.get("supply") or {}).get("voltage") is not None})
    amps = [float((b.get("supply") or {}).get("current_a") or 0) for b in mine if (b.get("supply") or {}).get("needed")]
    used_ain = max([int(b.get("channels") or 0) * int(b.get("batch") or 0) for b in mine] or [0])
    used_vout = max([int(b.get("batch") or 0) for b in mine if (b.get("supply") or {}).get("needed")] or [0])
    prog: Dict[str, Dict[str, Any]] = {}
    for b in mine:
        for proto in b.get("programming") or []:
            e = prog.setdefault(proto, {"programmers": 0, "boards": [], "what": PROG_NOTE.get(proto, proto + " programmer")})
            e["programmers"] = max(e["programmers"], int(b.get("batch") or 0))
            e["boards"].append(b["name"])
    buses = {}
    for bus, n in sorted((res.get("buses") or {}).items()):
        if not n:
            continue
        users = [b for b in mine if bus in (b.get("buses") or [])]
        buses[bus] = {"instances": int(n), "used": max([int(b.get("batch") or 0) for b in users] or [0]),
                      "boards": [b["name"] for b in users], "what": BUS_NOTE.get(bus, bus)}
    conns = F.connectors(fac) if fac else []
    return {
        "plate_mm": m["plate"], "free_area_mm": m["free"], "holes": len((fac.get("fixture") or {}).get("holes") or []) if fac else 0,
        "sockets": [{"ref": c.get("name"), "pins": len(c.get("pins") or {}),
                     "type": "2 x %d socket, 2.54 mm" % int(c.get("pins_along", 0)), "at_mm": c.get("at_mm")} for c in conns],
        "analog_inputs": {"count": int(res.get("ain", 0)), "used_max": used_ain,
                          "range_v": [0.0, float((spec.get("analog") or {}).get("max_v", 10.0))],
                          "suggested": "%d x 4-channel 16-bit ADC (e.g. ADS1115)" % int(math.ceil(res.get("ain", 0) / 4.0))
                          if res.get("ain") else "-", "driven_by": most(lambda b: int(b.get("channels") or 0) * int(b.get("batch") or 0))},
        "supplies": {"count": int(res.get("vout", 0)), "used_max": used_vout,
                     "voltages_v": volts, "max_current_a": max(amps) if amps else None,
                     "what": "a switched, current-limited output per board of a batch"
                             + (", adjustable %g..%g V" % (volts[0], volts[-1]) if len(volts) > 1 else
                                (", %g V" % volts[0] if volts else ""))},
        "buses": buses, "dio": int(res.get("dio", 0)), "programming": prog,
        "boards": len(mine), "board_slots": sum(int(b.get("batch") or 0) for b in mine),
        "fixtures": len(mine),
    }


# --------------------------------------------------------------------------- #
# documents
# --------------------------------------------------------------------------- #
def requirements_md(env: Dict[str, Any]) -> str:
    s = env["summary"]
    L = ["# %s" % (env.get("name") or "Test environment"), "",
         "Generated by %s. %d boards: %d compatible (%s%%) on %d station(s), %d do not fit, %d could not be read." % (
             env["generator"], s["boards"], s["compatible"], s["compatible_pct"], s["stations"], s["incompatible"], s["errors"]), ""]
    lay = next((x for x in env.get("layouts") or [] if x.get("chosen")), None)
    if lay:
        cur = ((env.get("spec") or {}).get("costs") or {}).get("currency", "EUR")
        L += ["Layout: **%s** - %s. Of the %d layouts compared it is worth the most for what it costs (estimated: "
              "%d boards per load, worth %s %s, costs %s %s; see index.html)." % (
                  lay["name"], lay["title"], len(env["layouts"]), lay["slots"], cur, "{:,.0f}".format(lay["worth"]),
                  cur, "{:,.0f}".format(lay["cost"])), ""]
    for st in env["stations"]:
        rq = st["requirements"]
        L += ["## %s%s" % (st["title"], " (existing)" if st["fixed"] else ""), "",
              "Takes %d board types (%d boards per load in all). Station board: `%s`." % (rq["boards"], rq["board_slots"], st["board"]), "",
              ] + ([plate_note(st), ""] if plate_note(st) else []) + [
              "| What | Count | Needed by | Notes |", "|---|---|---|---|",
              "| Plate | %g x %g mm | - | fixtures copy it; %d mounting holes; %g x %g mm for the boards |" % (
                  st["plate_mm"][0], st["plate_mm"][1], rq["holes"], rq["free_area_mm"][0], rq["free_area_mm"][1])]
        for so in rq["sockets"]:
            L.append("| Interface socket %s | %d pins | - | %s at (%g, %g) mm from the plate's top-left corner |" % (
                so["ref"], so["pins"], so["type"], so["at_mm"][0], so["at_mm"][1]))
        a = rq["analog_inputs"]
        L.append("| Analog inputs | %d | %s | 0..%g V, %s; up to %d used at once |" % (
            a["count"], a["driven_by"] or "-", a["range_v"][1], a["suggested"], a["used_max"]))
        sp = rq["supplies"]
        L.append("| Switched supplies | %d | - | %s%s; up to %d used at once |" % (
            sp["count"], sp["what"], ", up to %g A" % sp["max_current_a"] if sp["max_current_a"] else "", sp["used_max"]))
        for bus, b in rq["buses"].items():
            L.append("| %s | %d | %s | %s |" % (bus.upper() if bus != "ethernet" else "Ethernet", b["instances"],
                                               ", ".join(b["boards"][:4]) + (" ..." if len(b["boards"]) > 4 else ""), b["what"]))
        if rq["dio"]:
            L.append("| Digital lines | %d | - | spare, for your own checks |" % rq["dio"])
        for proto, pg in sorted(rq["programming"].items()):
            L.append("| %s | %d | %s | one per board of the largest batch, on the fixtures' programming connectors |" % (
                pg["what"], pg["programmers"], ", ".join(pg["boards"][:4]) + (" ..." if len(pg["boards"]) > 4 else "")))
        L.append("")
    bad = [b for b in env["boards"] if b["status"] != "compatible"]
    if bad:
        L += ["## Boards that do not fit", "", "| Board | Why |", "|---|---|"]
        L += ["| %s | %s |" % (b["name"], (b.get("reason") or b["status"]).replace("|", "/")) for b in bad]
        L.append("")
    return "\n".join(L) + "\n"


def plate_note(st: Dict[str, Any]) -> str:
    """Why the station has the plate it has, when there was a choice."""
    opts = st.get("plate_options") or []
    if len(opts) < 2:
        return ""
    chosen = st["plate_mm"]
    other = [o for o in opts if [float(v) for v in o["plate_mm"]] != [float(v) for v in chosen]]
    if not other:
        return ""
    o = other[0]
    mine = next((x for x in opts if [float(v) for v in x["plate_mm"]] == [float(v) for v in chosen]), None)
    return ("Plate: %g x %g mm takes %g board types more than a %g x %g mm plate would (%g against %g); set large_plate_mm "
            "to [] in the spec to keep every station at plate_mm." % (
                chosen[0], chosen[1], (mine or {}).get("boards_gained", 0) - o["boards_gained"], o["plate_mm"][0],
                o["plate_mm"][1], (mine or {}).get("boards_gained", 0), o["boards_gained"])
            if (mine or {}).get("boards_gained", 0) > o["boards_gained"] else
            "Plate: %g x %g mm (a %g x %g mm plate takes no more board types)." % (chosen[0], chosen[1], o["plate_mm"][0],
                                                                                 o["plate_mm"][1]))


def ranking_csv(env: Dict[str, Any]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["rank", "board", "folder", "priority", "status", "station", "batch", "limited_by", "channels", "supply_v",
                "buses", "programming", "pins_per_board", "width_mm", "height_mm", "reason"])
    for r in env["ranking"]:
        sz = r.get("size_mm") or ["", ""]
        w.writerow([r["rank"], r["name"], r["folder"], r["priority"], r["status"], r.get("station") or "", r.get("batch") or 0,
                    r.get("limit") or "", r.get("channels") if r.get("channels") is not None else "",
                    r.get("supply_v") if r.get("supply_v") is not None else "", " ".join(r["buses"]),
                    " ".join(r["programming"]), r["pins_per_board"], sz[0], sz[1], r.get("reason") or ""])
    return buf.getvalue()


def environment_html(env: Dict[str, Any]) -> str:
    e = html.escape
    s = env["summary"]
    css = ("body{font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f6f6f4;color:#1d1d1f}"
           "main{max-width:1200px;margin:0 auto;padding:24px 16px 64px}h1{font-size:24px;margin:0 0 4px}"
           "h2{font-size:18px;margin:30px 0 10px}.muted{color:#666}.cards{display:grid;grid-template-columns:repeat(auto-fit,"
           "minmax(150px,1fr));gap:10px;margin:16px 0}.card{background:#fff;border:1px solid #e3e3e0;border-radius:10px;padding:12px}"
           ".card b{font-size:22px;display:block}table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}"
           "th,td{border-bottom:1px solid #eee;padding:6px 8px;text-align:left;vertical-align:top}th{background:#fafafa}"
           ".ok{color:#0a7d42;font-weight:600}.no{color:#b3261e;font-weight:600}.wrap{overflow-x:auto}"
           "video,img{max-width:100%;border-radius:8px}.st{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px}"
           "@media(max-width:800px){.st{grid-template-columns:1fr}}"
           "a{color:#0b62c4}td:nth-child(4),td:nth-child(5){white-space:nowrap}"
           "@media (prefers-color-scheme:dark){body{background:#161618;color:#eee}.card,table{background:#1f1f22;border-color:#333}"
           "th{background:#26262a}td,th{border-color:#333}.muted{color:#aaa}a{color:#6cb4ff}}")
    h = ["<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
         "<title>%s</title><style>%s</style></head><body><main>" % (e(env.get("name") or "Test environment"), css),
         "<h1>%s</h1><div class='muted'>%s &middot; %d boards</div>" % (e(env.get("name") or "Test environment"), e(env["generator"]), s["boards"]),
         ("<video controls preload='metadata' src='%s' style='margin:12px 0'></video>" % e(env["video"]) if env.get("video") else ""),
         "<div class='cards'><div class='card'><b>%d of %d</b>boards fit (%s%%)</div><div class='card'><b>%d</b>station(s)</div>"
         "<div class='card'><b>%d</b>boards per full load</div><div class='card'><b>%d</b>do not fit</div>"
         "<div class='card'><b>%d</b>could not be read</div></div>" % (
             s["compatible"], s["boards"], s["compatible_pct"], s["stations"], s["board_slots"], s["incompatible"], s["errors"])]
    for st in env["stations"]:
        rq = st["requirements"]
        link = st.get("page")
        h.append("<h2>%s%s</h2>" % (e(st["title"]), " (existing)" if st["fixed"] else ""))
        media = []
        if st.get("image"):
            media.append("<img src='%s' alt='%s'>" % (e(st["image"]), e(st["name"])))
        if st.get("video"):
            media.append("<video controls preload='metadata' src='%s'></video>" % e(st["video"]))
        rows = ["<tr><td>Plate</td><td>%g x %g mm</td><td>%d holes; %g x %g mm for the boards</td></tr>" % (
            st["plate_mm"][0], st["plate_mm"][1], rq["holes"], rq["free_area_mm"][0], rq["free_area_mm"][1])]
        rows += ["<tr><td>Socket %s</td><td>%d pins</td><td>%s</td></tr>" % (e(so["ref"] or ""), so["pins"], e(so["type"]))
                 for so in rq["sockets"]]
        a = rq["analog_inputs"]
        rows.append("<tr><td>Analog inputs</td><td>%d</td><td>0..%g V; %s; most at once %d%s</td></tr>" % (
            a["count"], a["range_v"][1], e(a["suggested"]), a["used_max"], (" (%s)" % e(a["driven_by"])) if a["driven_by"] else ""))
        sp = rq["supplies"]
        rows.append("<tr><td>Switched supplies</td><td>%d</td><td>%s%s</td></tr>" % (
            sp["count"], e(sp["what"]), ", up to %g A" % sp["max_current_a"] if sp["max_current_a"] else ""))
        for bus, b in rq["buses"].items():
            rows.append("<tr><td>%s</td><td>%d</td><td>%s<div class='muted'>%s</div></td></tr>" % (
                "Ethernet" if bus == "ethernet" else bus.upper(), b["instances"], e(b["what"]),
                e(", ".join(b["boards"][:6]) + (" ..." if len(b["boards"]) > 6 else ""))))
        if rq["dio"]:
            rows.append("<tr><td>Digital lines</td><td>%d</td><td>spare</td></tr>" % rq["dio"])
        for proto, pg in sorted(rq["programming"].items()):
            rows.append("<tr><td>%s</td><td>%d</td><td>one per board of the largest batch<div class='muted'>%s</div></td></tr>" % (
                e(pg["what"]), pg["programmers"], e(", ".join(pg["boards"][:6]) + (" ..." if len(pg["boards"]) > 6 else ""))))
        if plate_note(st):
            media.append("<p class='muted'>%s</p>" % e(plate_note(st)))
        h.append("<div class='st'><div>%s<p>Takes <b>%d</b> board types, %d boards per load in all.%s</p></div>"
                 "<div class='wrap'><table><tr><th>Needs</th><th>Count</th><th></th></tr>%s</table></div></div>" % (
                     "".join(media), rq["boards"], rq["board_slots"],
                     " <a href='%s'>Station page (pin maps, every board type's outputs)</a>" % e(link) if link else "",
                     "".join(rows)))
    from . import upgrades as U
    if env.get("layouts"):
        cur = ((env.get("spec") or {}).get("costs") or {}).get("currency", "EUR")
        h.append("<h2>Layouts compared</h2><p class='muted'>Every way of laying out the stations that was looked at, "
                 "estimated from the board profiles: what the boards that fit are worth (value_per_board, more for "
                 "bigger batches) minus what the stations, programmers and fixtures cost. The chosen one is in bold; "
                 "<code>--layout NAME</code> (spec <code>layout</code>) takes another.%s</p><div class='wrap'><table><tr>"
                 "<th>Layout</th><th>Stations</th><th>Boards that fit</th><th>Boards per load</th><th>Worth</th>"
                 "<th>Cost</th><th>Net</th></tr>" % ((" " + e(env["layouts_note"][:1].upper() + env["layouts_note"][1:]) + ".")
                                                      if env.get("layouts_note") else ""))
        for lay in env["layouts"]:
            sts = "<br>".join("%s: %s, %d boards, %g x %g mm" % (e(x["name"]), e(x.get("focus") or "-"), x["boards"],
                                                                x["plate_mm"][0], x["plate_mm"][1]) for x in lay["stations"])
            b0, b1 = ("<b>", "</b>") if lay["chosen"] else ("", "")
            h.append("<tr><td>%s%s%s<div class='muted'>%s</div></td><td>%s</td><td>%d</td><td>%d</td><td>%s %s</td>"
                     "<td>%s %s</td><td>%s%s %s%s</td></tr>" % (
                         b0, e(lay["name"]), b1, e(lay["title"]), sts, lay["boards_fit"], lay["slots"], cur,
                         "{:,.0f}".format(lay["worth"]), cur, "{:,.0f}".format(lay["cost"]), b0, cur,
                         "{:,.0f}".format(lay["net"]), b1))
        h.append("</table></div>")
    h.append(U.html_sections(env))
    h.append("<h2>Ranking</h2><p class='muted'>Boards that fit first, by priority and batch size; then the ones that do not, "
             "by priority and by how few pins they would need. <a href='ranking.csv'>ranking.csv</a></p>"
             "<div class='wrap'><table><tr><th>#</th><th>Board</th><th>Priority</th><th>Station</th><th>Batch</th>"
             "<th>Limited by</th><th>Needs per board</th><th>Programming</th><th>Fixture</th><th>Why not</th></tr>")
    fixture_note = {}
    for b_ in env["boards"]:
        ro = b_.get("routing") or {}
        if b_.get("status") != "compatible":
            continue
        if b_.get("kept_fixture"):
            fixture_note[b_["folder"]] = "kept"
        elif ro.get("connections"):
            left = int(ro.get("unrouted_connections") or 0)
            fixture_note[b_["folder"]] = "routed" if not left else "%d of %d to route in KiCad" % (left, ro["connections"])
        elif b_.get("outputs_dir"):
            fixture_note[b_["folder"]] = "not routed"
    pages = {st["name"]: st.get("page_dir") for st in env["stations"]}
    for r in env["ranking"]:
        needs = "%s ch" % (r.get("channels") if r.get("channels") is not None else "?")
        if r.get("supply_v") is not None:
            needs += ", %g V" % r["supply_v"]
        if r["buses"]:
            needs += ", " + " ".join(x.upper() for x in r["buses"])
        name = e(r["name"])
        if r["status"] == "compatible" and pages.get(r.get("station")):
            guide = next((b.get("guide") for b in env["boards"] if b["folder"] == r["folder"]), None)
            if guide:
                name = "<a href='%s'>%s</a>" % (e(guide), name)
        h.append("<tr><td>%d</td><td>%s</td><td>%g</td><td>%s</td><td class='%s'>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                 "<td>%s</td><td>%s</td></tr>" % (r["rank"], name, r["priority"], e(r.get("station") or "-"),
                                                  "ok" if r["status"] == "compatible" else "no",
                                                  r["batch"] if r["status"] == "compatible" else e(r["status"]),
                                                  e(r.get("limit") or "-"), e(needs), e(" ".join(r["programming"]) or "-"),
                                                  e(fixture_note.get(r["folder"], "-")), e(r.get("reason") or "")))
    h.append("</table></div>")
    if env.get("warnings"):
        h.append("<h2>Notes</h2><ul>%s</ul>" % "".join("<li>%s</li>" % e(w) for w in env["warnings"][:60]))
    h.append("<h2>Files</h2><ul><li><a href='requirements.md'>requirements.md</a>: what every station needs</li>"
             "<li><a href='environment.json'>environment.json</a>: everything, machine-readable</li>"
             "<li><a href='environment-spec.json'>environment-spec.json</a>: the settings (edit and build again)</li>"
             "<li><a href='constraints.md'>constraints.md</a>: what limits every board, what one more per fixture "
             "needs; <a href='upgrades.md'>upgrades.md</a>: the upgrade options with what they gain and cost</li>"
             "<li><a href='ranking.csv'>ranking.csv</a>, <a href='boards.csv'>boards.csv</a>, "
             "<a href='constraints.csv'>constraints.csv</a></li></ul>")
    h.append("</main></body></html>")
    return "".join(h)


def boards_csv(env: Dict[str, Any]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["board", "folder", "project", "status", "station", "batch", "limited_by", "entry", "channels", "supply_net",
                "supply_v", "supply_a", "buses", "programming", "probe_side", "probes", "coverage_pct", "width_mm", "height_mm",
                "reason"])
    for b in env["boards"]:
        sp = b.get("supply") or {}
        sz = b.get("size_mm") or ["", ""]
        w.writerow([b["name"], b["folder"], b["project"], b["status"], b.get("station") or "", b.get("batch") or 0,
                    b.get("limit") or "", b.get("entry") or "", b.get("channels") if b.get("channels") is not None else "",
                    sp.get("net") or "", sp.get("voltage") if sp.get("voltage") is not None else "", sp.get("current_a") or "",
                    " ".join(b.get("buses") or []), " ".join(b.get("programming") or []), b.get("probe_side") or "",
                    b.get("probes") if b.get("probes") is not None else "", b.get("coverage_pct") if b.get("coverage_pct") is not None else "",
                    sz[0], sz[1], b.get("reason") or ""])
    return buf.getvalue()


def shareable(env: Dict[str, Any]) -> Dict[str, Any]:
    """The environment without private keys and local paths (for environment.json)."""
    out = {k: v for k, v in env.items() if not k.startswith("_")}
    out["boards"] = [{k: v for k, v in b.items() if not k.startswith("_") and k != "row"} for b in env["boards"]]
    return out


def write_docs(env: Dict[str, Any], out_dir: str) -> Dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    files = {"index": "index.html", "requirements": "requirements.md", "ranking": "ranking.csv", "boards": "boards.csv",
             "environment": "environment.json", "spec": "environment-spec.json"}
    with open(os.path.join(out_dir, files["requirements"]), "w", encoding="utf-8") as fh:
        fh.write(requirements_md(env))
    with open(os.path.join(out_dir, files["ranking"]), "w", encoding="utf-8", newline="") as fh:
        fh.write(ranking_csv(env))
    with open(os.path.join(out_dir, files["boards"]), "w", encoding="utf-8", newline="") as fh:
        fh.write(boards_csv(env))
    with open(os.path.join(out_dir, files["environment"]), "w", encoding="utf-8") as fh:
        json.dump(shareable(env), fh, indent=1, default=str)
        fh.write("\n")
    with open(os.path.join(out_dir, files["spec"]), "w", encoding="utf-8") as fh:
        json.dump(env["spec"], fh, indent=2)
        fh.write("\n")
    from . import upgrades as U
    try:
        files.update(U.write(env, out_dir))
    except Exception as exc:  # noqa: BLE001 - the analysis must never cost the build its documents
        env.setdefault("warnings", []).append("the constraints and upgrade options could not be worked out: %r" % exc)
    with open(os.path.join(out_dir, files["index"]), "w", encoding="utf-8") as fh:
        fh.write(environment_html(env))
    with open(os.path.join(out_dir, MARKER), "w", encoding="utf-8") as fh:
        fh.write("PCB TestKit environment %s\n" % __version__)
    return files


# --------------------------------------------------------------------------- #
# building it all
# --------------------------------------------------------------------------- #
def build_environment(inputs: Sequence[str], out_dir: str, spec: Optional[Dict[str, Any]] = None,
                      environment: Optional[str] = None, stations: Sequence[str] = (), new_stations: bool = True,
                      jobs: int = 0, visuals: bool = True, route: bool = True, video: str = "swap", video_boards: int = 8,
                      resume: bool = True, progress: Optional[Callable[[str, float], None]] = None,
                      station_revs: Optional[Dict[str, str]] = None, keep_fixtures: bool = False) -> Dict[str, Any]:
    """Plan the environment into ``out_dir`` (its stations in out_dir/stations/<name>/, with every board type's
    outputs in a folder of its own), then the station pages and videos and the environment's documents.
    ``video``: none | swap (each station's swap video) | all (also every board type's explainer). A station's video
    keeps the board types it showed and adds the boards that are new to the environment first (up to
    ``video_boards`` per build). ``station_revs``: the revision each (upgraded) station's board has;
    ``keep_fixtures``: a board whose station and batch stay keeps the fixture made for the station's previous
    revision (an upgrade keeps every pin, so it still plugs in)."""
    from .station import _connector_brief, connector_briefs, run_station

    def step(msg: str, frac: float) -> None:
        if progress:
            progress(msg, max(0.0, min(1.0, frac)))
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    if environment and os.path.abspath(environment) == out_dir:
        environment_copy = tempfile.mkdtemp(prefix="pcbtk_envold_")     # stations are rewritten below: read the old
        shutil.copytree(os.path.join(out_dir, "stations"), os.path.join(environment_copy, "stations"), dirs_exist_ok=True)
        shutil.copy2(os.path.join(out_dir, "environment.json"), os.path.join(environment_copy, "environment.json"))
        environment = environment_copy
    jobs = int(jobs) if jobs else max(1, min(8, (os.cpu_count() or 2) - 1))
    before: Dict[str, Any] = {}
    for d in (out_dir, environment):                  # what this folder had (an upgrade's stations come from elsewhere)
        try:
            with open(os.path.join(d, "environment.json"), encoding="utf-8") as fh:
                before = json.load(fh)
            break
        except (OSError, ValueError, TypeError):
            continue
    known = {b["folder"] for b in before.get("boards") or []}
    was = {b["folder"]: (b.get("station"), int(b.get("batch") or 0)) for b in before.get("boards") or []
           if b.get("status") == "compatible"}
    old_facs: Dict[str, Dict[str, Any]] = {}
    if keep_fixtures:
        for st in before.get("stations") or []:
            try:
                with open(os.path.join(out_dir, "stations", st["name"], "facility.json"), encoding="utf-8") as fh:
                    old_facs[st["name"]] = json.load(fh)
            except (OSError, ValueError):
                pass
    old_revs = {st["name"]: st.get("rev", "A") for st in before.get("stations") or []}
    env = plan_environment(inputs, spec, environment, stations, new_stations, jobs, out_dir,
                           progress=lambda m, f: step(m, 0.3 * f))
    new_boards = [b["folder"] for b in env["boards"] if b["folder"] not in known] if known else []
    env["added"] = new_boards
    if not env.get("layouts") and before.get("layouts"):         # stations kept: the comparison they came from
        env["layouts"], env["layout"] = before["layouts"], before.get("layout")
        env["layouts_note"] = before.get("layouts_note") or ("compared when the stations were designed; boards added "
                                                             "or upgrades since are not compared again")
    facs = env.pop("_facilities")
    spec = env["spec"]
    boards_by = {b["folder"]: b for b in env["boards"]}
    order = {r["folder"]: r["rank"] for r in env["ranking"]}
    n_st = max(1, len(env["stations"]))
    for si, st in enumerate(env["stations"]):
        fac = facs[st["name"]]
        sdir = os.path.join(out_dir, "stations", st["name"])
        os.makedirs(sdir, exist_ok=True)
        src = fac.get("_station_path")
        if src and os.path.abspath(os.path.dirname(src)) != os.path.abspath(sdir):   # an existing station: copy its board
            for ext in (".kicad_pcb", ".kicad_pro"):
                s_ = os.path.splitext(src)[0] + ext
                if os.path.exists(s_):
                    shutil.copy2(s_, os.path.join(sdir, os.path.basename(s_)))
            fac["_station_path"] = os.path.join(sdir, os.path.basename(src))
        st["rev"] = (station_revs or {}).get(st["name"]) or old_revs.get(st["name"]) or "A"
        st["board"] = os.path.relpath(fac.get("_station_path") or os.path.join(sdir, st["name"] + ".kicad_pcb"), out_dir)
        st["board_sha"] = board_sha(os.path.join(out_dir, st["board"]))
        with open(os.path.join(sdir, "facility.json"), "w", encoding="utf-8") as fh:
            json.dump({k: v for k, v in fac.items() if not str(k).startswith("_")}, fh, indent=1)
        rows = []
        for folder in sorted(st["boards"], key=lambda f: order.get(f, 10 ** 6)):
            b = boards_by[folder]
            row = dict(b["row"], project=b["_pcb"], folder=folder, name=b["name"], status="ok")
            if keep_fixtures and st["name"] in old_facs and was.get(folder) == (st["name"], int(row.get("batch") or 0)):
                row["_fac"] = old_facs[st["name"]]          # same station, same batch: its fixture still fits
                b["kept_fixture"] = True
            rows.append(row)
        planned = {"schema": "pcbtestkit-station/1", "generator": "PCB TestKit %s" % __version__, "station": F.summary(fac),
                   "station_source": os.path.basename(fac.get("_station_path") or ""), "capacity": F.capacity(fac),
                   "connector": _connector_brief(fac), "connectors": connector_briefs(fac), "boards": rows, "warnings": [],
                   "_facility": fac}
        base = 0.3 + 0.65 * si / n_st
        stres = run_station(fac.get("_station_path") or "", [], sdir, planned=planned, visuals=visuals,
                            board_videos=(video == "all"), station_video=(video in ("swap", "all")) and bool(rows),
                            route=route, jobs=jobs, resume=resume, video_boards=video_boards, title=st["title"],
                            router=spec.get("router") or None,
                            board_cfg=dict({"visuals": spec["visuals"]} if spec.get("visuals") else {},
                                           **({"video": {"quality": spec["video_quality"]}} if spec.get("video_quality") else {}))
                            or None, video_quality=spec.get("video_quality") or "basic",
                            video_prefer=[f for f in new_boards if f in st["boards"]],
                            progress=lambda m, f, b0=base, n=st["name"]: step("%s: %s" % (n, m), b0 + 0.65 / n_st * f))
        for r in stres["boards"]:
            b = boards_by.get(r.get("folder"))
            if b is None:
                continue
            if r.get("status") == "error":
                b.update(status="error", reason=r.get("reason"))
                continue
            outs = r.get("outputs") or {}
            if outs.get("guide"):
                b["guide"] = os.path.relpath(os.path.join(sdir, outs["guide"]), out_dir)
            b["outputs_dir"] = os.path.relpath(os.path.join(sdir, folder_of(r)), out_dir)
            if (r.get("fixture") or {}).get("drc"):
                b["drc"] = r["fixture"]["drc"]
            if (r.get("fixture") or {}).get("routing"):
                b["routing"] = r["fixture"]["routing"]
        st["page"] = os.path.relpath(os.path.join(sdir, "station.html"), out_dir)
        st["page_dir"] = os.path.relpath(sdir, out_dir)
        if stres.get("image"):
            st["image"] = os.path.relpath(os.path.join(sdir, stres["image"]), out_dir)
        if (stres.get("video") or {}).get("ok"):
            st["video"] = os.path.relpath(os.path.join(sdir, stres["video"]["path"]), out_dir)
        elif stres.get("video"):
            st["video_error"] = stres["video"].get("reason")
    vids = [os.path.join(out_dir, st["video"]) for st in env["stations"] if st.get("video")]
    if vids:
        step("Joining the station videos", 0.96)
        joined = join_videos(vids, os.path.join(out_dir, "test-environment.mp4"))
        if joined:
            env["video"] = os.path.basename(joined)
    n_ok = sum(1 for b in env["boards"] if b["status"] == "compatible")
    env["summary"].update(compatible=n_ok, errors=sum(1 for b in env["boards"] if b["status"] == "error"),
                          compatible_pct=round(100.0 * n_ok / max(1, len(env["boards"])), 1))
    env["ranking"] = rank(env["boards"], spec)
    step("Writing the environment's documents", 0.97)
    env["files"] = write_docs(env, out_dir)
    write_sources(env, out_dir)
    step("Done: %d of %d boards on %d station(s)" % (n_ok, len(env["boards"]), len(env["stations"])), 1.0)
    return env


SOURCES = "sources.json"


def write_sources(env: Dict[str, Any], out_dir: str) -> str:
    """sources.json: where every board came from, relative to the environment's folder (so no local path is in
    it), for ``environment add`` and ``environment upgrade``, which build again from it."""
    def rel(p: str) -> str:
        try:
            return os.path.relpath(p, out_dir).replace(os.sep, "/")
        except ValueError:                              # another drive (Windows): the path as it is
            return p
    data = {"schema": "pcbtestkit-environment-sources/1",
            "boards": {b["folder"]: rel(b["_pcb"]) for b in env["boards"] if b.get("_pcb")}}
    path = os.path.join(out_dir, SOURCES)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    return path


def environment_sources(env_dir: str) -> Tuple[List[str], List[str]]:
    """(the boards an environment was built from, warnings for the ones that are gone)."""
    try:
        with open(os.path.join(env_dir, SOURCES), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return [], ["%s has no %s (made by every build since PCB TestKit 1.1.2): give its boards again" % (env_dir, SOURCES)]
    out, warns = [], []
    for folder, p in sorted((data.get("boards") or {}).items()):
        full = p if os.path.isabs(p) else os.path.normpath(os.path.join(env_dir, p))
        if os.path.exists(full):
            out.append(full)
        else:
            warns.append("%s: its board %s is gone; it is left out" % (folder, p))
    return out, warns


def _env_json(env_dir: str) -> Dict[str, Any]:
    try:
        with open(os.path.join(env_dir, "environment.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise ValueError("%s is not an environment folder (no readable environment.json): %s" % (env_dir, exc))


def add_boards(env_dir: str, boards: Sequence[str], spec_over: Optional[Dict[str, Any]] = None, new_stations: bool = True,
               **build: Any) -> Dict[str, Any]:
    """New boards for an environment that is built: its stations stay as they are (edited in KiCad or not), the
    boards it has are reused (their checks and outputs are cached), the new ones are checked and made, a further
    station is only designed for new boards no station takes (``new_stations``, within max_stations), and each
    station's swap video gets the new board types after the ones it showed."""
    env_dir = os.path.abspath(env_dir)
    old = _env_json(env_dir)
    srcs, warns = environment_sources(env_dir)
    if not srcs and old.get("boards"):
        raise ValueError(warns[0] if warns else "no sources")
    spec_file = os.path.join(env_dir, "environment-spec.json")
    spec = load_spec(spec_file if os.path.exists(spec_file) else None, spec_over or {})
    env = build_environment(list(srcs) + list(boards), env_dir, spec, environment=env_dir, new_stations=new_stations,
                            **build)
    env["warnings"] = warns + env.get("warnings", [])
    return env


def upgrade_environment(env_dir: str, changes: Sequence[Dict[str, Any]], spec_over: Optional[Dict[str, Any]] = None,
                        **build: Any) -> Dict[str, Any]:
    """Make the ``changes`` (upgrades.parse_changes / an option's "change") and build the environment again: every
    changed station gets a new revision of its board that keeps every pin, socket and hole it had (moved with the
    plate when that grows), so the fixtures made for it still plug in; the boards whose station or batch did not
    change keep their outputs. A station that is your own board (or edited in KiCad) is not changed: ValueError
    says what to change in it."""
    from . import upgrades as U
    env_dir = os.path.abspath(env_dir)
    env = _env_json(env_dir)
    srcs, warns = environment_sources(env_dir)
    if not srcs:
        raise ValueError(warns[0] if warns else "no sources")
    models, spec = U.changed_stations(env, changes, spec_over and _merge(env.get("spec") or default_spec(), spec_over))
    own = [m["name"] for m in models if m.get("changed") and (m.get("own") or
                                                               next((st for st in env["stations"] if st["name"] == m["name"]), {}).get("own"))]
    if own:
        raise ValueError("%s: your own station board (or edited in KiCad): make the change there, then build with "
                         "--environment" % ", ".join(own))
    tmp = tempfile.mkdtemp(prefix="pcbtk_upgrade_")
    revs: Dict[str, str] = {}
    stations_out = []
    try:
        for m in models:
            old = next((st for st in env["stations"] if st["name"] == m["name"]), None)
            sdir = os.path.join(tmp, "stations", m["name"])
            os.makedirs(sdir, exist_ok=True)
            if old and not m.get("changed"):
                src = os.path.join(env_dir, old["board"])
                shutil.copy2(src, os.path.join(sdir, os.path.basename(src)))
                rel = os.path.join("stations", m["name"], os.path.basename(src))
                stations_out.append({"name": m["name"], "board": rel, "board_sha": board_sha(os.path.join(tmp, rel))})
                continue
            title = (old or {}).get("title") or "%s %s" % (spec.get("name", "Test environment"), m["name"].replace("station-", "station "))
            st = station_spec(m, spec, title)
            if old:
                with open(os.path.join(env_dir, "stations", m["name"], "facility.json"), encoding="utf-8") as fh:
                    st["keep"] = SB.keep_of(json.load(fh))
                rv = str(old.get("rev") or "A")
                st["rev"] = revs[m["name"]] = chr(ord(rv[-1]) + 1) if rv[-1].isalpha() and rv[-1] not in "Zz" else rv + "1"
            pcb = SB.write_station(st, sdir)
            rel = os.path.relpath(pcb, tmp)
            stations_out.append({"name": m["name"], "board": rel, "board_sha": board_sha(pcb)})
        with open(os.path.join(tmp, "environment.json"), "w", encoding="utf-8") as fh:
            json.dump({"schema": SCHEMA, "stations": stations_out}, fh)
        out = build_environment(srcs, env_dir, spec, environment=tmp, new_stations=False, station_revs=revs,
                                keep_fixtures=True, **build)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out["warnings"] = warns + out.get("warnings", [])
    out["upgraded"] = [dict(c, text=U.describe_change(c, U.Context(env).models, env.get("spec") or {}))
                       for c in U.resolve(changes, U.Context(env).models, env.get("spec") or {})]
    return out


def join_videos(videos: Sequence[str], out_path: str) -> Optional[str]:
    """The stations' videos one after the other in one MP4 (ffmpeg, no re-encoding); a copy when there is one."""
    if len(videos) == 1:
        shutil.copy2(videos[0], out_path)
        return out_path
    from .video import find_ffmpeg
    ff = find_ffmpeg()
    if not ff:
        return None
    lst = out_path + ".txt"
    with open(lst, "w", encoding="utf-8") as fh:
        for v in videos:
            fh.write("file '%s'\n" % os.path.abspath(v).replace("'", "'\\''"))
    try:
        import subprocess
        r = subprocess.run([ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out_path],
                           capture_output=True, text=True, timeout=600)
        return out_path if r.returncode == 0 and os.path.exists(out_path) else None
    except (OSError, ValueError):
        return None
    finally:
        try:
            os.remove(lst)
        except OSError:
            pass


def folder_of(row: Dict[str, Any]) -> str:
    return row.get("folder") or row.get("name") or "board"


# --------------------------------------------------------------------------- #
# one board for an existing environment (pcbtestkit.json "environment", or the remembered one)
# --------------------------------------------------------------------------- #
SETUP_REV = 3


def environment_setting(cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The test environment a board is made for: its own (pcbtestkit.json "environment"), else the remembered one
    (pcbtestkit.settings), else None. path "none" switches it off for the board."""
    from . import settings
    e = cfg.get("environment") or {}
    p = str(e.get("path") or "").strip()
    if p.lower() == "none":
        return None
    if not p:
        rem = settings.remembered_environment()
        if not rem:
            return None
        out = dict(rem, remembered=True)
        if str(e.get("station") or "auto") != "auto":     # the board's own choices win
            out["station"] = e["station"]
        if str(e.get("batch") if e.get("batch") is not None else "auto") != "auto":
            out["batch"] = e["batch"]
        return out
    return {"path": p, "station": str(e.get("station") or "auto"), "batch": e.get("batch", "auto")}


def environment_stations(path: str) -> Tuple[List[Tuple[str, Dict[str, Any]]], Dict[str, Any], List[str]]:
    """The stations of an environment folder (each read from its own board, edits included), or of one station
    (a .kicad_pcb, facility.json or facility id): ([(name, facility)], spec, warnings)."""
    from .station import load_station
    warns: List[str] = []
    out: List[Tuple[str, Dict[str, Any]]] = []
    if os.path.isdir(path):
        try:
            with open(os.path.join(path, "environment.json"), encoding="utf-8") as fh:
                env = json.load(fh)
        except (OSError, ValueError):
            raise ValueError("%s is not a test environment folder (no environment.json from an environment build)" % path)
        spec = _merge(default_spec(), env.get("spec") or {})
        for st in env.get("stations") or []:
            pcb = os.path.join(path, st.get("board") or "")
            if not st.get("board") or not os.path.exists(pcb):
                warns.append("station %s: its board %s is missing" % (st.get("name"), st.get("board")))
                continue
            fac, w = F.facility_from_pcb(pcb, ident=st["name"], name=st.get("title") or st["name"],
                                         extra={"analog": {"max_v": float((spec.get("analog") or {}).get("max_v", 10.0))}})
            fac["batch"] = {"max_sites": int(spec.get("max_batch", 12))}
            fac["_station_path"] = pcb
            warns += ["%s: %s" % (st["name"], x) for x in w]
            out.append((st["name"], fac))
        return out, spec, warns
    fac, w = load_station(path)
    name = fac.get("id") or os.path.splitext(os.path.basename(path))[0]
    return [(name, fac)], default_spec(), w


def board_setup(pcb: str, envc: Dict[str, Any], project_cfg: Optional[str] = None) -> Dict[str, Any]:
    """Which station of the environment this board goes on and the batch: {"ok", "environment", "station",
    "batch", "cols", "entry", "limit", "facility", "considered": [...], "note"} or {"ok": False, "reason"}.
    batch "auto" = as many as the station allows (inputs, supplies, buses, plate), N = that many (or fewer, with a
    note). Cached until the board, its settings or a station board change."""
    from . import settings
    from .project import locate
    from .station import _inline, plan_board
    epath = envc["path"]
    if not os.path.isabs(epath):
        epath = os.path.normpath(os.path.join(os.path.dirname(pcb), epath))
    if not os.path.exists(epath) and not os.path.splitext(epath)[1] == "":
        return {"ok": False, "reason": "the test environment %s is not there" % epath, "environment": epath}
    want_station = str(envc.get("station") or "auto")
    batch = envc.get("batch", "auto")
    batch = None if str(batch).lower() in ("auto", "", "none", "0") else max(1, int(batch))
    _p, sch, _pro = locate(pcb)
    # the cache key from the files alone (the station boards are only read when something changed)
    src = [os.path.join(epath, "environment.json")]
    if os.path.isdir(epath):
        try:
            with open(src[0], encoding="utf-8") as fh:
                src += [os.path.join(epath, st.get("board") or "") for st in json.load(fh).get("stations") or []]
        except (OSError, ValueError):
            pass
    else:
        src = [epath]
    files = [pcb, sch, project_cfg or os.path.join(os.path.dirname(pcb), "pcbtestkit.json")] + src
    key = _file_sha(files, [__version__, SETUP_REV, want_station, batch, os.path.abspath(epath)])
    cache = settings.cache_file("environment-setup", os.path.abspath(pcb) + "|" + os.path.abspath(epath))
    try:
        with open(cache, encoding="utf-8") as fh:
            old = json.load(fh)
        if old.get("key") == key:
            return old["setup"]
    except (OSError, ValueError, KeyError):
        pass
    try:
        stations, _spec, warns = environment_stations(epath)
    except (OSError, ValueError) as exc:
        return {"ok": False, "reason": str(exc), "environment": epath}
    if not stations:
        return {"ok": False, "reason": "the test environment %s has no stations" % epath, "environment": epath}
    considered = []
    best = None
    for name, fac in stations:
        if want_station != "auto" and name != want_station:
            continue
        try:
            row = plan_board(pcb, fac, batch)
        except Exception as exc:  # a station that cannot be planned is reported, never fatal
            row = {"status": "error", "batch": 0, "reason": "%s: %s" % (type(exc).__name__, exc)}
        hard = row_issue(row, _spec)                # what no station of this environment can do for it
        if hard and row.get("status") == "ok":
            row = dict(row, status="does not fit", batch=0, reason=hard)
        considered.append({"station": name, "status": row.get("status"), "batch": row.get("batch", 0),
                           "limit": row.get("limit"), "reason": row.get("reason")})
        if row.get("status") == "ok" and row.get("batch", 0) >= 1 and (best is None or row["batch"] > best[1]["batch"]):
            best = (name, row, fac)
    env_name = os.path.basename(os.path.normpath(epath))
    if want_station != "auto" and not considered:
        setup = {"ok": False, "reason": "the test environment has no station %s (it has %s)" % (
            want_station, ", ".join(n for n, _f in stations)), "environment": env_name}
    elif best is None:
        setup = {"ok": False, "environment": env_name, "considered": considered,
                 "reason": "this board fits none of its stations: " + "; ".join(
                     "%s: %s" % (c["station"], c.get("reason") or c.get("status")) for c in considered)}
    else:
        name, row, fac = best
        setup = {"ok": True, "environment": env_name, "station": name, "batch": int(row["batch"]), "cols": row.get("cols"),
                 "entry": row.get("entry"), "limit": row.get("limit"), "facility": _inline(fac),
                 "auto": batch is None, "requested": batch, "considered": considered, "note": row.get("note")}
    setup["warnings"] = warns
    try:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump({"key": key, "setup": setup}, fh)
    except OSError:
        pass
    return setup


def row_issue(row: Dict[str, Any], spec: Dict[str, Any]) -> Optional[str]:
    """A station plan row's board against the environment's own limits (supply voltage, one-sided fixtures)."""
    v = row.get("supply_v")
    vmax = float((spec.get("supplies") or {}).get("max_v", 24.0))
    if (row.get("needs") or {}).get("supply") and v is not None and float(v) > vmax + 1e-6:
        return "needs a %g V supply; the environment's supplies go to %g V (supplies.max_v)" % (float(v), vmax)
    if row.get("probe_side") == "both":
        return "probed from both sides (two fixture plates): a station takes one-sided fixtures"
    if row.get("probes") == 0:
        return "no test point the fixture can reach"
    return None


def apply_environment(design: Any, envc: Dict[str, Any]) -> None:
    """Make ``design`` for the environment's station: its facility, the batch, columns and nest orientation, as a
    config overlay (pcbtestkit.json is not changed). When the board fits no station, a warning says why."""
    setup = board_setup(design.board.path, envc, design.config_path)
    design.environment = dict(setup, remembered=bool(envc.get("remembered")))
    for w in setup.get("warnings") or []:
        design.warnings.append("test environment: %s" % w)
    if not setup.get("ok"):
        design.warnings.append("test environment %s: %s; the fixture is made without it" % (
            setup.get("environment"), setup.get("reason")))
        return
    ov: Dict[str, Any] = {"facility": setup["facility"], "batch": {"count": setup["batch"], "cols": setup.get("cols") or 0}}
    if setup.get("entry"):
        ov["fixture"] = {"nest": {"entry": setup["entry"]}}
    design.set_overlay(ov)
    if setup.get("note"):
        design.warnings.append("test environment: %s" % setup["note"])
