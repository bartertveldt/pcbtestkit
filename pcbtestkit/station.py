"""Several board types on one test station.

A test station is a facility with one *shared* interface connector (see facility.py): every fixture made for it is
a plate of the station's size with its holes and the mating connector in the same place, so the fixture of any
board type plugs into the same station. Each board of a batch gets its own share of the connector: a block of
analog inputs, its own switched supply and its own instance of every bus it is tested on.

``plan_station`` decides, per board type, how many boards one fixture holds: as many as the station's inputs,
supplies, bus instances and plate allow (and the station's own limit), unless a batch size is given.
``run_station`` then makes every board's full output (spec, plan, fixture, jigs, runner, guide, report, and
videos on request) for that batch, plus the station's own page and, on request, a video of the fixtures
swapping on the station.

    pcbtestkit station plan  examples/station/test_station.kicad_pcb a.kicad_pro b.kicad_pro
    pcbtestkit station run   examples/station/test_station.kicad_pcb a.kicad_pro b.kicad_pro --out station_out
"""
from __future__ import annotations

import html
import json
import math
import os
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import __version__
from . import facility as F
from . import htmlkit as K

SCHEMA = "pcbtestkit-station/1"


# --------------------------------------------------------------------------- #
# the station
# --------------------------------------------------------------------------- #
def load_station(spec: str, connector: Optional[str] = None, extra: Optional[Dict[str, Any]] = None
                 ) -> Tuple[Dict[str, Any], List[str]]:
    """The station facility from a station board (.kicad_pcb / .kicad_pro / folder with one), a facility JSON
    file or folder, or the id of an installed facility. Returns (facility, warnings)."""
    path = spec
    if os.path.isdir(spec):
        pcbs = sorted(f for f in os.listdir(spec) if f.endswith(".kicad_pcb"))
        if os.path.exists(os.path.join(spec, "facility.json")):
            path = os.path.join(spec, "facility.json")
        elif len(pcbs) == 1:
            path = os.path.join(spec, pcbs[0])
    if path.endswith(".kicad_pro"):
        path = path[:-len(".kicad_pro")] + ".kicad_pcb"
    if path.endswith(".kicad_pcb"):
        if not os.path.exists(path):
            raise ValueError("station board %s not found" % spec)
        fac, warns = F.facility_from_pcb(path, connector=connector, extra=extra)
        fac["_station_path"] = os.path.abspath(path)
        return fac, warns
    if path.endswith(".json") or os.path.isfile(path):
        raw = F.read_facility(path)
        for k, v in (extra or {}).items():
            raw[k] = dict(raw[k], **v) if isinstance(v, dict) and isinstance(raw.get(k), dict) else v
        r = F.validate(raw)
        if not r["ok"]:
            raise ValueError("station %s is not valid: %s" % (spec, "; ".join(r["errors"][:4])))
        return raw, r["warnings"]
    found, warns = F.load_facilities()
    if spec not in found:
        raise ValueError("no station %r: give a station .kicad_pcb, a facility.json or one of %s" % (spec, ", ".join(sorted(found))))
    return found[spec], warns


def _inline(fac: Dict[str, Any]) -> Dict[str, Any]:
    """The facility as a pcbtestkit.json override (no private keys)."""
    return {k: v for k, v in fac.items() if not str(k).startswith("_")}


def _design(project: str, fac: Dict[str, Any], count: int, overrides: Optional[Dict[str, Any]] = None):
    from .project import Design
    ov = {"facility": _inline(fac), "batch": {"count": int(count)}, "environment": {"path": "none"}}
    for k, v in (overrides or {}).items():
        ov[k] = dict(ov.get(k) or {}, **v) if isinstance(v, dict) and isinstance(ov.get(k), dict) else v
    return Design.load(project, overrides=ov)


# --------------------------------------------------------------------------- #
# planning: how many boards of each type one fixture holds
# --------------------------------------------------------------------------- #
def plan_board(project: str, fac: Dict[str, Any], batch: Optional[int] = None, check_plate: bool = True,
               hint: Optional[int] = None) -> Dict[str, Any]:
    """One board type on the station: what a board needs, how many fit at once and what limits it. ``hint``: a
    batch the plate probably takes (the search starts there and goes up or down instead of down from the most)."""
    from .pipeline import analyze
    t0 = time.time()
    d1 = _design(project, fac, 1)
    bb = d1.board.outline_bbox()
    findings, plan1 = analyze(d1, include_kicad=False)
    need = F.board_needs(plan1)
    cap = F.capacity(fac)
    fit = F.max_sites(cap, need)
    row: Dict[str, Any] = {"name": d1.board.name, "project": os.path.abspath(project), "size_mm": [round(bb.w, 1), round(bb.h, 1)],
                           "needs": need, "max_by": dict(fit["by"]), "limit": fit["limit"], "requested": batch,
                           "supply_v": (plan1.get("supply") or {}).get("voltage"), "probe_side": plan1.get("probe_side"),
                           "probes": len(plan1.get("probes") or [])}
    n = fit["count"] if batch is None else min(int(batch), fit["count"])
    if batch is not None and int(batch) > fit["count"]:
        row["note"] = "%d requested; the station serves at most %d (its %s)" % (int(batch), fit["count"], fit["limit"])
    if n < 1:
        row.update(batch=0, status="does not fit", reason="needs more %s than the station has" % fit["limit"])
        row["seconds"] = round(time.time() - t0, 1)
        return row
    plate = (fac.get("fixture") or {}).get("plate_mm")
    cols, entry = None, None
    if check_plate and plate:
        # the boards' nests: slide in from the front (clamps behind them) or from the side (clamps beside them),
        # whichever holds more boards; a board whose pcbtestkit.json sets fixture.nest.entry keeps it
        nc = (d1.cfg.get("fixture") or {}).get("nest") or {}
        own = str(nc.get("entry", "front"))
        entries = [own] if (own != "front" or not nc.get("enabled", True)) else ["front", "left"]
        best, last_fx = 0, None
        for e in entries:
            m_e, c_e, fx_e = _plate_fit(project, fac, findings, n, e if nc.get("enabled", True) else None, hint)
            last_fx = last_fx or fx_e
            if m_e > best:
                best, cols, entry, last_fx = m_e, c_e, e, fx_e
            if best >= n:
                break
        real = ((last_fx or {}).get("nest") or {}).get("entry") if (last_fx or {}).get("plate_fit", True) else None
        entry = real or entry                       # the fixture may have turned the nests itself to fit the plate
        if entry and entry != own:
            row["entry"] = entry
        if best < n:
            row["max_by"]["plate"] = best
            if best < 1:
                row.update(batch=0, status="does not fit", limit="plate",
                           reason="a %s x %s mm board with its connector block%s does not fit the %s x %s mm plate" % (
                               row["size_mm"][0], row["size_mm"][1],
                               " and nest" if ((d1.cfg.get("fixture") or {}).get("nest") or {}).get("enabled", True) else "",
                               plate[0], plate[1]))
                row["warnings"] = [w for w in (last_fx or {}).get("warnings", []) if "fit" in w][:2]
                row["seconds"] = round(time.time() - t0, 1)
                return row
            if batch is None or best < int(batch):
                row["limit"] = "plate"
            n = best
    cin = int(cap.get("inputs") or 0)
    if cols:
        row["cols"] = cols
    row.update(batch=n, status="ok", channels=need["channels"],
               inputs_used=need["channels"] * n, inputs_total=cin or None,
               utilization_pct=round(100.0 * need["channels"] * n / cin, 1) if cin else None,
               supplies_used=n if need["supply"] else 0, supplies_total=cap.get("supplies"),
               buses={b: n for b in need["buses"]}, seconds=round(time.time() - t0, 1))
    return row


def _plate_fit(project: str, fac: Dict[str, Any], findings, n: int, entry: Optional[str], hint: Optional[int] = None):
    """(largest batch <= n whose fixture fits the station's plate, its column count or None, the last fixture).
    With ``hint`` the search starts there: up while it fits, else down."""
    from .fixture import build_fixture
    from .testplan import build_testplan
    fxo: Dict[str, Any] = {"route": False}
    if entry:
        fxo["nest"] = {"entry": entry}
    tried: Dict[int, Tuple[bool, Optional[int], Any]] = {}

    def fits(m: int) -> Tuple[bool, Optional[int], Any]:
        if m not in tried:
            dm = _design(project, fac, m, {"fixture": fxo})
            fx = build_fixture(dm, build_testplan(dm, findings))
            ok, cols = bool(fx.get("plate_fit", True)), None
            if not ok and fx.get("plate_fits_cols"):
                c = fx["plate_fits_cols"][0]        # another column count fits: confirm it
                dm = _design(project, fac, m, {"fixture": fxo, "batch": {"cols": c}})
                fx2 = build_fixture(dm, build_testplan(dm, findings))
                if fx2.get("plate_fit", True):
                    ok, cols, fx = True, c, fx2
            tried[m] = (ok, cols, fx)
        return tried[m]
    if hint and 1 <= hint < n:
        m = int(hint)
        if fits(m)[0]:
            while m < n and fits(m + 1)[0]:
                m += 1
            return m, tried[m][1], tried[m][2]
        n = m - 1
    last_fx = None
    for m in range(n, 0, -1):                       # largest batch whose fixture fits the station's plate
        ok, cols, fx = fits(m)
        last_fx = fx
        if ok:
            return m, cols, fx
    return 0, None, last_fx


def plan_station(spec: str, projects: List[str], batches: Optional[Dict[str, int]] = None, connector: Optional[str] = None,
                 extra: Optional[Dict[str, Any]] = None, check_plate: bool = True,
                 progress: Optional[Callable[[str, float], None]] = None) -> Dict[str, Any]:
    """Every board type on the station: {"schema", "station", "capacity", "boards": [...], "warnings"}."""
    fac, warns = load_station(spec, connector, extra)
    cap = F.capacity(fac)
    if not cap.get("shared"):
        warns.append("station %s has no shared connector: each fixture site needs its own connector (%s); batches "
                     "follow from that" % (fac["id"], cap.get("sites")))
    boards = []
    batches = batches or {}
    for i, p in enumerate(projects):
        if progress:
            progress("Planning %s on the station" % os.path.basename(p), i / max(1, len(projects)))
        key = os.path.splitext(os.path.basename(p.rstrip("/")))[0]
        try:
            boards.append(plan_board(p, fac, batches.get(key), check_plate))
        except Exception as exc:  # one broken project must not stop the others
            boards.append({"name": key, "project": os.path.abspath(p), "status": "error", "batch": 0,
                           "reason": "%s: %s" % (type(exc).__name__, exc)})
    names = [b["name"] for b in boards]
    for b in boards:
        if names.count(b["name"]) > 1:
            b["folder"] = "%s-%d" % (b["name"], names[:boards.index(b) + 1].count(b["name"]))
        else:
            b["folder"] = b["name"]
    return {"schema": SCHEMA, "generator": "PCB TestKit %s" % __version__, "station": F.summary(fac),
            "station_source": os.path.basename(fac.get("_station_path") or spec), "capacity": cap,
            "connector": _connector_brief(fac), "connectors": connector_briefs(fac), "boards": boards, "warnings": warns,
            "_facility": fac}


def _connector_brief(fac: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    briefs = connector_briefs(fac)
    return briefs[0] if briefs else None


def connector_briefs(fac: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every interface socket of the station (the shared ones; else all), for the pin maps."""
    sh = [c for c in F.connectors(fac) if c.get("shared")] or F.connectors(fac)
    return [{"name": c.get("name", "TESTER"), "pins_across": int(c.get("pins_across", 2)), "pins_along": int(c["pins_along"]),
             "pitch_mm": float(c.get("pitch_mm", 2.54)), "numbering": c.get("numbering", "zigzag"),
             "functions": {int(k): v for k, v in c["pins"].items()}} for c in sh]


# --------------------------------------------------------------------------- #
# running: every board's outputs for its batch, the station page, the station video
# --------------------------------------------------------------------------- #
def _stamp_key(project: str, fac: Dict[str, Any], batch: int, ov: Dict[str, Any], flags: Dict[str, Any]) -> str:
    """What a board's outputs on a station depend on: its files, the station, its batch and settings, the version."""
    import hashlib
    from .project import locate
    h = hashlib.sha1()
    pcb, _sch, pro = locate(project)
    for p in (pcb, os.path.join(os.path.dirname(pcb), "pcbtestkit.json"), _sch):
        if p and os.path.exists(p):
            with open(p, "rb") as fh:
                h.update(fh.read())
    h.update(json.dumps([_inline(fac), batch, ov, flags, __version__, K.PAGE_REV], sort_keys=True, default=str).encode())
    return h.hexdigest()


STAMP = ".pcbtestkit-station.json"


def board_outputs(job: Dict[str, Any]) -> Dict[str, Any]:
    """One board type's outputs on a station (runs in a worker process): {"outputs", "warnings", "fixture", "pins",
    "probe_side"} with paths relative to ``out_dir``, or {"error"}. ``resume``: reuse what an earlier run made
    when nothing it depends on changed."""
    from .pipeline import run_pipeline
    fac, b, out_dir = job["fac"], job["board"], job["out_dir"]
    ov: Dict[str, Any] = {"fixture": {} if job.get("route", True) else {"route": False}}
    if b.get("cols"):
        ov["batch"] = {"cols": b["cols"]}
    if b.get("entry"):                              # the nests turned to fit the plate (see plan_board)
        ov["fixture"]["nest"] = {"entry": b["entry"]}
    if job.get("video_workers"):
        ov["video"] = {"workers": int(job["video_workers"])}
    if job.get("router") and job.get("route", True):
        ov["fixture"]["router"] = dict(job["router"])
    for k, v in (job.get("board_cfg") or {}).items():         # e.g. the guide renders' quality
        ov[k] = dict(ov.get(k) or {}, **v) if isinstance(v, dict) else v
    if job.get("render_workers"):                             # boards side by side share the cores
        ov["visuals"] = dict(ov.get("visuals") or {}, workers=int(job["render_workers"]))
    bdir = os.path.join(out_dir, b["folder"])
    flags = {"visuals": job.get("visuals", True), "video": job.get("board_videos", False),
             "jig_fit_check": job.get("jig_fit_check", False)}
    try:
        key = _stamp_key(b["project"], fac, int(b["batch"]), ov, flags)
    except (OSError, ValueError) as exc:
        return {"error": "%s: %s" % (type(exc).__name__, exc)}
    stamp = os.path.join(bdir, STAMP)
    if job.get("resume"):
        try:
            with open(stamp, encoding="utf-8") as fh:
                old = json.load(fh)
            if old.get("key") == key and all(os.path.exists(os.path.join(out_dir, v)) for v in old["result"]["outputs"].values()):
                return dict(old["result"], reused=True)
        except (OSError, ValueError, KeyError):
            pass
    try:
        d = _design(b["project"], fac, int(b["batch"]), ov)
        res = run_pipeline(d, out_dir=bdir, include_kicad=False, outputs=flags)
        out: Dict[str, Any] = {"outputs": {k: os.path.relpath(v, out_dir) for k, v in res["paths"].items() if isinstance(v, str)}}
        out["warnings"] = [w for w in res.get("fixture_warnings", []) + res.get("warnings", []) if "station" in w or "fit" in w][:6]
        with open(res["paths"]["fixture_meta"], encoding="utf-8") as fh:
            fxm = json.load(fh)
        out["fixture"] = {"sites": [{"site": s_["site"], "offset": s_["offset"]} for s_ in fxm.get("sites", [])],
                          "outline": fxm.get("outline"), "connector": (fxm.get("facility") or {}).get("connectors"),
                          "drc": fxm.get("drc"), "routing": {k: (fxm.get("routing") or {}).get(k) for k in
                                                             ("connections", "unrouted_connections")}}
        out["nest"] = {k: (fxm.get("nest") or {}).get(k) for k in ("enabled", "style", "entry", "heights")}
        with open(res["paths"]["testplan"], encoding="utf-8") as fh:
            tp = json.load(fh)
        out["pins"] = [q for c in (tp.get("facility") or {}).get("connectors") or [] for q in c["pins"]]
        out["pins_by"] = {c.get("name", "TESTER"): c["pins"] for c in (tp.get("facility") or {}).get("connectors") or []}
        out["probe_side"] = tp.get("probe_side")
        out["programming"] = sorted({h.get("protocol") or "?" for h in (tp.get("program") or {}).get("headers") or []})
        if res.get("video"):
            out["video"] = {k: v for k, v in res["video"].items() if k in ("ok", "reason")}
    except Exception as exc:  # one board must never stop the others
        return {"error": "outputs failed: %s: %s" % (type(exc).__name__, exc)}
    try:
        with open(stamp, "w", encoding="utf-8") as fh:
            json.dump({"key": key, "result": out}, fh, indent=1, default=str)
    except OSError:
        pass
    return out


def run_jobs(fn: Callable[[Dict[str, Any]], Dict[str, Any]], jobs: List[Dict[str, Any]], workers: int,
             done: Optional[Callable[[int, int, Dict[str, Any], Dict[str, Any]], None]] = None) -> List[Dict[str, Any]]:
    """fn over jobs, ``workers`` at a time in separate processes (1 = in this process), results in job order.
    Falls back to one at a time when worker processes cannot be started (inside KiCad)."""
    results: List[Optional[Dict[str, Any]]] = [None] * len(jobs)
    n = 0
    if workers > 1 and len(jobs) > 1:
        try:
            import multiprocessing
            from concurrent.futures import ProcessPoolExecutor, as_completed
            from concurrent.futures.process import BrokenProcessPool
            ctx = multiprocessing.get_context("spawn")
            with ProcessPoolExecutor(max_workers=min(workers, len(jobs)), mp_context=ctx) as ex:
                futs = {ex.submit(fn, j): i for i, j in enumerate(jobs)}
                for f in as_completed(futs):
                    i = futs[f]
                    try:
                        results[i] = f.result()
                    except BrokenProcessPool:        # the workers could not start: do them here (below)
                        continue
                    except Exception as exc:         # a worker that died
                        results[i] = {"error": "worker failed: %s: %s" % (type(exc).__name__, exc)}
                    n += 1
                    if done:
                        done(n, len(jobs), jobs[i], results[i])
        except (OSError, ImportError, RuntimeError, NotImplementedError):
            pass
    for i, j in enumerate(jobs):
        if results[i] is not None:
            continue
        try:
            results[i] = fn(j)
        except Exception as exc:
            results[i] = {"error": "%s: %s" % (type(exc).__name__, exc)}
        n += 1
        if done:
            done(n, len(jobs), j, results[i])
    return [r or {"error": "no result"} for r in results]


def run_station(spec: str, projects: List[str], out_dir: str, batches: Optional[Dict[str, int]] = None,
                connector: Optional[str] = None, extra: Optional[Dict[str, Any]] = None, visuals: bool = True,
                board_videos: bool = False, station_video: bool = False, route: bool = True,
                progress: Optional[Callable[[str, float], None]] = None, planned: Optional[Dict[str, Any]] = None,
                jobs: int = 1, resume: bool = False, video_boards: Optional[int] = None,
                title: Optional[str] = None, router: Optional[Dict[str, Any]] = None,
                board_cfg: Optional[Dict[str, Any]] = None, video_quality: str = "basic",
                video_prefer: Sequence[str] = ()) -> Dict[str, Any]:
    """Plan the station (or take ``planned``, a plan_station result), then make every fitting board's outputs in
    <out_dir>/<board>/ (``jobs`` boards at a time; ``resume`` keeps what is still up to date) and the station's
    page (<out_dir>/station.html, station.json); ``video_boards``: board types in the station video (the first N).
    Returns the station result (paths relative to out_dir)."""

    def step(msg: str, frac: float) -> None:
        if progress:
            progress(msg, frac)

    os.makedirs(out_dir, exist_ok=True)
    st = planned if planned is not None else plan_station(spec, projects, batches, connector, extra,
                                                           progress=lambda m, f: step(m, 0.2 * f))
    fac = st.pop("_facility")
    if title:
        st["title"] = title
    ok = [b for b in st["boards"] if b.get("status") == "ok"]
    cores = os.cpu_count() or 2
    work = [{"fac": b.pop("_fac", None) or fac, "board": b, "out_dir": out_dir, "route": route, "visuals": visuals, "board_videos": board_videos,
             "resume": resume,
             "video_workers": max(2, cores // max(1, min(int(jobs), len(ok) or 1))) if board_videos and jobs > 1 else 0,
             "router": router, "board_cfg": board_cfg,
             "render_workers": max(1, cores // max(1, min(int(jobs), len(ok) or 1))) if jobs > 1 else 0}
            for b in ok]
    step("Making the outputs for %d board type(s)" % len(ok), 0.2)

    def done(n: int, total: int, job: Dict[str, Any], res: Dict[str, Any]) -> None:
        step("Outputs %d of %d: %s%s" % (n, total, job["board"]["name"], " (up to date)" if res.get("reused") else ""),
             0.2 + 0.6 * n / max(1, total))
    for b, res in zip(ok, run_jobs(board_outputs, work, max(1, int(jobs)), done)):
        if res.get("error"):
            b.update(status="error", reason=res["error"])
            continue
        b.update({k: v for k, v in res.items() if k != "reused"})
    st["boards_ok"] = sum(1 for b in st["boards"] if b.get("status") == "ok")
    if station_video:
        step("Rendering the station video with KiCad (several minutes)", 0.82)
        from .station_video import build_station_video
        try:
            vres = build_station_video(st, fac, out_dir, progress=lambda a, n: step("Station video frame %d of %d" % (a, n),
                                                                                        0.82 + 0.15 * a / max(1, n)),
                                       max_boards=video_boards, quality=video_quality, prefer=video_prefer)
        except Exception as exc:  # the video is optional
            vres = {"ok": False, "reason": "%s: %s" % (type(exc).__name__, exc)}
        st["video"] = {k: (os.path.relpath(v, out_dir) if k == "path" else v) for k, v in vres.items()
                       if k in ("ok", "path", "reason", "duration_s", "clips", "shown", "board_types")}
    step("Writing the station page", 0.97)
    if fac.get("_station_path"):
        img = os.path.join(out_dir, "station.png")
        if render_station(fac["_station_path"], img):
            st["image"] = "station.png"
    shared = dict(st, boards=[dict(b, project=_project_name(b.get("project", ""))) for b in st["boards"]])
    with open(os.path.join(out_dir, "station.json"), "w", encoding="utf-8") as fh:     # no local paths in it
        json.dump(shared, fh, indent=2, default=str)
        fh.write("\n")
    with open(os.path.join(out_dir, "station.html"), "w", encoding="utf-8") as fh:
        fh.write(station_html(st, out_dir))
    link_pages(st, out_dir)
    st["paths"] = {"station_json": "station.json", "station_html": "station.html"}
    step("Done", 1.0)
    return st


def link_pages(st: Dict[str, Any], out_dir: str, parents: Sequence[Tuple[str, str]] = ()) -> int:
    """Breadcrumbs from every board type's guide and report up to this station's page (and its ``parents``:
    (href relative to the station's folder, label), e.g. the test environment), and from the station's page up to
    its parents. Returns how many pages were linked."""
    name = (st.get("station") or {}).get("name") or st.get("title") or "Test station"
    n = 0
    if parents:
        n += K.set_context(os.path.join(out_dir, "station.html"), K.crumbs(list(parents) + [(None, name)]))
    for b in st.get("boards") or []:
        o = b.get("outputs") or {}
        if b.get("status") != "ok" or not o:
            continue
        up = [("../" + href, label) for href, label in parents] + [("../station.html", name)]
        banner = ("<div class='callout info small' style='margin:0 0 14px'>Tested on <b>%s</b>: %d board%s per fixture%s. "
                  "<a href='../station.html'>Station page</a></div>" % (
                      K.esc(name), int(b.get("batch") or 1), "" if int(b.get("batch") or 1) == 1 else "s",
                      (", limited by %s" % K.esc(b["limit"])) if b.get("limit") else ""))
        for key, what in (("guide", "guide"), ("report", "report")):
            if o.get(key):
                n += K.set_context(os.path.join(out_dir, o[key]), K.crumbs(up + [(None, "%s %s" % (b["name"], what))]) + banner)
    return n


def _project_name(path: str) -> str:
    """A project's folder name (or file name) for files that are shared: never the local path."""
    p = path.rstrip("/\\")
    if p.endswith((".kicad_pcb", ".kicad_pro")):
        p = os.path.dirname(p) or p
    return os.path.basename(p)


def render_station(pcb: str, png: str) -> bool:
    """A KiCad 3D render of the station board (skipped without kicad-cli)."""
    from .visuals import Renderer
    rd = Renderer(os.path.dirname(os.path.abspath(png)), 1600, 1000)
    if not rd.available:
        return False
    try:
        return rd.render(pcb, png, rotate="-40,0,25", quality="basic", perspective=True, stackup_colors=True)
    finally:
        import shutil
        shutil.rmtree(rd.scene_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# the station page
# --------------------------------------------------------------------------- #
SITE_COLORS = ["#2f7d4f", "#3a6ea5", "#d9480f", "#8e44ad", "#c99a00", "#16a085", "#c0392b", "#7f8c8d"]


def connector_svg(conn: Dict[str, Any], pins: List[Dict[str, Any]]) -> str:
    """The station connector, pin 1 top left, each pin coloured by the board (site) it serves."""
    across, along = conn["pins_across"], conn["pins_along"]
    zig = conn.get("numbering", "zigzag") == "zigzag"
    by = {int(q["pin"]): q for q in pins}
    cell = 22
    w, h = along * cell + 20, across * cell + 40
    out = ["<svg viewBox='0 0 %d %d' width='100%%' style='max-width:%dpx' role='img' aria-label='connector map'>" % (w, h, w)]
    for n in range(1, across * along + 1):
        i, j = ((n - 1) % across, (n - 1) // across) if zig else ((n - 1) // along, (n - 1) % along)
        x, y = 10 + j * cell, 10 + i * cell
        q = by.get(n, {})
        fn = conn["functions"].get(n) or conn["functions"].get(str(n)) or "NC"       # str keys after a JSON round trip
        site = q.get("site")
        fill = SITE_COLORS[(site - 1) % len(SITE_COLORS)] if site else ("#555" if fn == "GND" else "none")
        out.append("<rect x='%d' y='%d' width='%d' height='%d' rx='3' fill='%s' stroke='var(--line)'><title>pin %d: %s%s</title></rect>"
                   % (x, y, cell - 3, cell - 3, fill, n, html.escape(fn),
                      (" - board %d %s" % (site, html.escape(q.get("signal", "")))) if site else ""))
        if n in (1, across * along):
            out.append("<text x='%d' y='%d' font-size='8' fill='var(--muted)'>%d</text>" % (x + 2, y + cell + 6 if i else y - 1, n))
    out.append("</svg>")
    return "".join(out)


def _thumb(base: Optional[str], folder: str) -> Optional[str]:
    """A board's 3D render next to the page (relative), if its outputs have one."""
    if not base or not folder:
        return None
    return K.first_existing(base, ["%s/visuals/%s" % (folder, n) for n in ("dut_iso.png", "dut_top.png")])


def _explain_station_board(b: Dict[str, Any]) -> str:
    """One sentence on a board type on a station: what it uses and what sets its batch."""
    bits = ["%d x %d measured channel%s" % (b["batch"], b.get("channels", 0), "" if b.get("channels") == 1 else "s")]
    if b.get("supplies_used"):
        bits.append("%d switched suppl%s" % (b["supplies_used"], "y" if b["supplies_used"] == 1 else "ies"))
    if b.get("buses"):
        bits.append(", ".join("%s x%d" % (k.upper(), v) for k, v in sorted(b["buses"].items())))
    if b.get("programming"):
        bits.append("programmed over %s" % "/".join(p.upper() for p in b["programming"]))
    why = "; the batch is set by %s" % b["limit"] if b.get("limit") else ""
    return "Uses %s%s." % (", ".join(bits), why)


def station_html(st: Dict[str, Any], out_dir: Optional[str] = None) -> str:
    """The station page: the station (render, swap video, what it offers), every board type on it (render, batch,
    what it uses, its outputs and explainer video), the pin maps and how to swap fixtures. ``out_dir``: the folder
    the page goes into (renders and videos are shown when they are there)."""
    e = K.esc
    cap, stn = st["capacity"], st["station"]
    boards = st["boards"]
    ok = [b for b in boards if b.get("status") == "ok"]
    off = [b for b in boards if b.get("status") != "ok"]
    plate = " x ".join("%g" % v for v in (cap.get("plate_mm") or [])) or "?"
    conns = st.get("connectors") or ([st["connector"]] if st.get("connector") else [])
    cname = " + ".join(c.get("name", "") for c in conns) or (st.get("connector") or {}).get("name", "")
    vid = st.get("video") or {}
    have_vid = bool(vid.get("ok") and vid.get("path"))
    vids = [(b, (b.get("outputs") or {}).get("video")) for b in ok]
    vids = [(b, v) for b, v in vids if v and (out_dir is None or os.path.exists(os.path.join(out_dir, v)))]
    title = stn["name"]
    lead = ("One station, %d board type%s: every fixture is the station's %s mm plate with the mating connector %s in "
            "the same place, so swapping a fixture is all it takes to test another board." % (
                len(boards), "" if len(boards) == 1 else "s", plate, cname or "-"))
    slots = sum(int(b.get("batch") or 0) for b in ok)
    pills = [("%d of %d board types fit" % (len(ok), len(boards)), "ok" if not off else "warn"),
             ("%d boards per full load" % slots, "info")]
    if off:
        pills.append(("%d do not fit" % len(off), "bad"))
    stats = [(len(ok), "board types", ""), (slots, "boards, all fixtures", ""), (cap.get("inputs", "-"), "analog inputs", ""),
             (cap.get("supplies", "-"), "switched supplies", ""), ("%s mm" % plate, "plate", "")]
    actions = [("#video" if have_vid else "", "Watch the station video", True), ("#boards", "Board types", False),
               ("#swap", "Swapping fixtures", False)]
    h = [K.head("%s - test station" % title, lead, script=True),
         K.hero("Test station", title, lead, pills, stats, actions),
         K.nav([("video", "Station") if have_vid or st.get("image") else ("", ""), ("offers", "What it offers"),
                ("boards", "Board types"), ("videos", "Videos") if vids else ("", ""),
                ("pins", "Pin maps") if conns else ("", ""), ("swap", "Swapping"), ("notes", "Notes") if st.get("warnings") else ("", "")]),
         "<main id='content'>"]
    # ---- the station -----------------------------------------------------------------------------
    media = []
    if have_vid:
        media.append(K.video(vid["path"], "Every fixture on the station", "%d board type%s%s" % (
            len(ok), "" if len(ok) == 1 else "s", (", %d s" % vid["duration_s"]) if vid.get("duration_s") else ""),
            poster=st.get("image") or ""))
    elif st.get("image"):
        media.append("<img class='shot' src='%s' alt='The test station (KiCad 3D render)'>" % e(st["image"]))
    rows = [("Analog inputs", cap.get("inputs", "-")), ("Switched supplies", cap.get("supplies", "-")),
            ("Buses (instances)", ", ".join("%s x%d" % (b.upper(), n) for b, n in sorted((cap.get("buses") or {}).items())) or "-"),
            ("Digital lines", cap.get("dio", "-")), ("Plate", "%s mm" % plate), ("Connector", cname or "-")]
    table = "<div class='wrap'><table><thead><tr><th>The station offers</th><th></th></tr></thead><tbody>%s</tbody></table></div>" % (
        "".join("<tr><td>%s</td><td><b>%s</b></td></tr>" % (e(k), e(v)) for k, v in rows))
    h.append("<section id='video'><div class='station'><div>%s</div><div id='offers'>%s<p class='small muted'>Generated by %s "
             "from %s.</p></div></div></section>" % ("".join(media), table, e(st.get("generator", "")),
                                                    e(st.get("station_source", ""))))
    # ---- board types -----------------------------------------------------------------------------
    h.append("<section id='boards'><h2>Board types on the station</h2><p class='muted'>Each card is one fixture: how "
             "many boards it takes at once, what they use of the station and everything made for it.</p>")
    h.append(K.filter_tools("bcards", "Find a board type", (("", "All"), ("ok", "Fit"), ("off", "Do not fit")) if off else (),
                            len(boards)))
    h.append("<div class='boards' id='bcards'>")
    for b in ok:
        o = b.get("outputs") or {}
        th = _thumb(out_dir, b.get("folder", ""))
        thumb = ("<img loading='lazy' src='%s' alt='%s, KiCad 3D render'>" % (e(th), e(b["name"])) if th
                 else K.board_shape(b.get("size_mm"), b["name"]))
        name = ("<a href='%s'>%s</a>" % (e(o["guide"]), e(b["name"]))) if o.get("guide") else e(b["name"])
        facts = ["<span class='fact big'>%d per fixture</span>" % b["batch"],
                 "<span class='fact'>%s mm</span>" % e(" x ".join("%g" % v for v in b.get("size_mm") or [])),
                 "<span class='fact'>%s of %s inputs</span>" % (b.get("inputs_used", "-"), b.get("inputs_total") or "-")]
        if b.get("probe_side"):
            facts.append("<span class='fact'>probes %s</span>" % e(b["probe_side"]))
        links = "".join("<a href='%s'>%s</a>" % (e(o[k]), lab) for k, lab in (
            ("guide", "Guide"), ("report", "Report"), ("fixture", "Fixture PCB"), ("video", "Video"), ("runner", "Runner"))
            if o.get(k))
        h.append("<article class='bcard' data-row='ok'><div class='thumb'>%s</div><div class='body'><h3>%s</h3>"
                 "<div class='facts'>%s</div><p class='why'>%s</p><div class='links'>%s</div></div></article>" % (
                     thumb, name, "".join(facts), e(_explain_station_board(b)), links))
    for b in off:
        h.append("<article class='bcard off' data-row='off'><div class='thumb'>%s</div><div class='body'><h3>%s</h3>"
                 "<div class='facts'><span class='fact'>%s</span></div><p class='why'>%s</p></div></article>" % (
                     K.board_shape(b.get("size_mm"), b["name"]), e(b["name"]), e(b.get("status", "")), e(b.get("reason", ""))))
    h.append("</div>")
    h.append("<div class='wrap'><table class='sortable'><thead><tr><th>Board</th><th class='r' data-num>Per fixture</th>"
             "<th>Inputs used</th><th>Supplies</th><th>Buses</th><th>Limited by</th></tr></thead><tbody>")
    for b in ok:
        h.append("<tr><td><b>%s</b><div class='muted small'>%s mm</div></td><td class='r ok'>%d</td><td>%s</td><td>%s</td>"
                 "<td>%s</td><td>%s</td></tr>" % (
                     e(b["name"]), e(" x ".join("%g" % v for v in b["size_mm"])), b["batch"],
                     "%d x %d = %d of %s (%s%%)" % (b["batch"], b["channels"], b["inputs_used"], b.get("inputs_total") or "-",
                                                    b.get("utilization_pct") if b.get("utilization_pct") is not None else "-"),
                     "%d of %s" % (b["supplies_used"], b.get("supplies_total") or "-") if b.get("supplies_used") else "-",
                     e(", ".join("%s x%d" % (k.upper(), v) for k, v in sorted(b.get("buses", {}).items())) or "-"),
                     e(b.get("limit") or "-")))
    h.append("</tbody></table></div></section>")
    # ---- every board type's explainer --------------------------------------------------------------
    if vids:
        h.append("<section id='videos'><h2>Every board type's explainer</h2><p class='muted'>Assembly, the jigs, the "
                 "fixture and the test of each board type, rendered by KiCad.</p><div class='gallery'>")
        for b, v in vids:
            th = _thumb(out_dir, b.get("folder", ""))
            h.append(K.video(v, b["name"], "%d per fixture" % b["batch"], poster=th or "", preload="none"))
        h.append("</div></section>")
    # ---- pin maps ----------------------------------------------------------------------------------
    if conns:
        h.append("<section id='pins'><h2>%s %s: which board uses which pin</h2>" % (
            "Connectors" if len(conns) > 1 else "Connector", e(cname)))
        for b in ok:
            chips = "".join("<span style='background:%s'>board %d</span>" % (SITE_COLORS[(k - 1) % len(SITE_COLORS)], k)
                            for k in range(1, b["batch"] + 1))
            maps = "".join(("<div class='muted small'>%s</div>" % e(c["name"]) if len(conns) > 1 else "") +
                           connector_svg(c, (b.get("pins_by") or {}).get(c["name"]) or (b.get("pins") or [] if len(conns) == 1 else []))
                           for c in conns)
            h.append("<div class='card' style='margin:14px 0'><b>%s</b> <span class='chips'>%s<span style='background:#555'>GND"
                     "</span></span>%s</div>" % (e(b["name"]), chips, maps))
        h.append("</section>")
    # ---- swapping ----------------------------------------------------------------------------------
    steps = ["Switch the station's supplies off and unscrew the fixture.",
             "Plug the next board type's fixture onto connector %s and screw it down in the same holes." % e(cname or "-"),
             "Run that board's runner (its folder above): its <code>testplan.json</code> knows which station pins its boards use.",
             "Every board has its own nest and toggle clamp on the fixture: slide a board into each nest up to the stop "
             "(above the pogo pins), close its clamp, and the runner tests the boards one by one; open a clamp to take "
             "that board off.%s" % "".join(" %s: from the %s." % (e(b["name"]), e(b["entry"])) for b in boards if b.get("entry"))]
    h.append("<section id='swap'><h2>Swapping to another board type</h2>%s</section>" % "".join(
        "<div class='step'><div class='num'>%d</div><div>%s</div></div>" % (i + 1, s) for i, s in enumerate(steps)))
    if st.get("warnings"):
        h.append("<section id='notes'><h2>Notes</h2><ul>%s</ul></section>" % "".join("<li>%s</li>" % e(w) for w in st["warnings"]))
    h.append(K.footer(st.get("generator", ""), script=True))
    return "\n".join(h)
