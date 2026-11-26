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
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import __version__
from . import facility as F

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
    ov = {"facility": _inline(fac), "batch": {"count": int(count)}}
    for k, v in (overrides or {}).items():
        ov[k] = dict(ov.get(k) or {}, **v) if isinstance(v, dict) and isinstance(ov.get(k), dict) else v
    return Design.load(project, overrides=ov)


# --------------------------------------------------------------------------- #
# planning: how many boards of each type one fixture holds
# --------------------------------------------------------------------------- #
def plan_board(project: str, fac: Dict[str, Any], batch: Optional[int] = None, check_plate: bool = True) -> Dict[str, Any]:
    """One board type on the station: what a board needs, how many fit at once and what limits it."""
    from .pipeline import analyze
    t0 = time.time()
    d1 = _design(project, fac, 1)
    bb = d1.board.outline_bbox()
    findings, plan1 = analyze(d1, include_kicad=False)
    need = F.board_needs(plan1)
    cap = F.capacity(fac)
    fit = F.max_sites(cap, need)
    row: Dict[str, Any] = {"name": d1.board.name, "project": os.path.abspath(project), "size_mm": [round(bb.w, 1), round(bb.h, 1)],
                           "needs": need, "max_by": dict(fit["by"]), "limit": fit["limit"], "requested": batch}
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
            m_e, c_e, fx_e = _plate_fit(project, fac, findings, n, e if nc.get("enabled", True) else None)
            last_fx = last_fx or fx_e
            if m_e > best:
                best, cols, entry, last_fx = m_e, c_e, e, fx_e
            if best >= n:
                break
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


def _plate_fit(project: str, fac: Dict[str, Any], findings, n: int, entry: Optional[str]):
    """(largest batch <= n whose fixture fits the station's plate, its column count or None, the last fixture)."""
    from .fixture import build_fixture
    from .testplan import build_testplan
    fxo: Dict[str, Any] = {"route": False}
    if entry:
        fxo["nest"] = {"entry": entry}
    last_fx = None
    for m in range(n, 0, -1):                       # largest batch whose fixture fits the station's plate
        dm = _design(project, fac, m, {"fixture": fxo})
        fx = build_fixture(dm, build_testplan(dm, findings))
        last_fx = fx
        if fx.get("plate_fit", True):
            return m, None, fx
        if fx.get("plate_fits_cols"):
            c = fx["plate_fits_cols"][0]            # another column count fits: confirm it
            dm = _design(project, fac, m, {"fixture": fxo, "batch": {"cols": c}})
            fx = build_fixture(dm, build_testplan(dm, findings))
            if fx.get("plate_fit", True):
                return m, c, fx
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
            "connector": _connector_brief(fac), "boards": boards, "warnings": warns, "_facility": fac}


def _connector_brief(fac: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    sh = [c for c in F.connectors(fac) if c.get("shared")] or F.connectors(fac)
    if not sh:
        return None
    c = sh[0]
    return {"name": c.get("name", "TESTER"), "pins_across": int(c.get("pins_across", 2)), "pins_along": int(c["pins_along"]),
            "pitch_mm": float(c.get("pitch_mm", 2.54)), "numbering": c.get("numbering", "zigzag"),
            "functions": {int(k): v for k, v in c["pins"].items()}}


# --------------------------------------------------------------------------- #
# running: every board's outputs for its batch, the station page, the station video
# --------------------------------------------------------------------------- #
def run_station(spec: str, projects: List[str], out_dir: str, batches: Optional[Dict[str, int]] = None,
                connector: Optional[str] = None, extra: Optional[Dict[str, Any]] = None, visuals: bool = True,
                board_videos: bool = False, station_video: bool = False, route: bool = True,
                progress: Optional[Callable[[str, float], None]] = None) -> Dict[str, Any]:
    """Plan the station, then make every fitting board's outputs in <out_dir>/<board>/ and the station's page
    (<out_dir>/station.html, station.json). Returns the station result (paths relative to out_dir)."""
    from .pipeline import run_pipeline

    def step(msg: str, frac: float) -> None:
        if progress:
            progress(msg, frac)

    os.makedirs(out_dir, exist_ok=True)
    st = plan_station(spec, projects, batches, connector, extra, progress=lambda m, f: step(m, 0.2 * f))
    fac = st.pop("_facility")
    ok = [b for b in st["boards"] if b.get("status") == "ok"]
    for i, b in enumerate(ok):
        step("Making the outputs for %s (batch of %d)" % (b["name"], b["batch"]), 0.2 + 0.6 * i / max(1, len(ok)))
        ov: Dict[str, Any] = {"fixture": {} if route else {"route": False}}
        if b.get("cols"):
            ov["batch"] = {"cols": b["cols"]}
        if b.get("entry"):                          # the nests turned to fit the plate (see plan_board)
            ov["fixture"]["nest"] = {"entry": b["entry"]}
        d = _design(b["project"], fac, b["batch"], ov)
        bdir = os.path.join(out_dir, b["folder"])
        try:
            res = run_pipeline(d, out_dir=bdir, include_kicad=False,
                               outputs={"visuals": visuals, "video": board_videos})
        except Exception as exc:
            b.update(status="error", reason="outputs failed: %s: %s" % (type(exc).__name__, exc))
            continue
        b["outputs"] = {k: os.path.relpath(v, out_dir) for k, v in res["paths"].items() if isinstance(v, str)}
        b["warnings"] = [w for w in res.get("fixture_warnings", []) + res.get("warnings", []) if "station" in w or "fit" in w][:6]
        with open(res["paths"]["fixture_meta"], encoding="utf-8") as fh:
            fxm = json.load(fh)
        b["fixture"] = {"sites": [{"site": s["site"], "offset": s["offset"]} for s in fxm.get("sites", [])],
                        "outline": fxm.get("outline"), "connector": (fxm.get("facility") or {}).get("connectors")}
        with open(res["paths"]["testplan"], encoding="utf-8") as fh:
            tp = json.load(fh)
        b["pins"] = [q for c in (tp.get("facility") or {}).get("connectors") or [] for q in c["pins"]]
        b["probe_side"] = tp.get("probe_side")
    st["boards_ok"] = sum(1 for b in st["boards"] if b.get("status") == "ok")
    if station_video:
        step("Rendering the station video with KiCad (several minutes)", 0.82)
        from .station_video import build_station_video
        try:
            vres = build_station_video(st, fac, out_dir, progress=lambda a, n: step("Station video frame %d of %d" % (a, n),
                                                                                        0.82 + 0.15 * a / max(1, n)))
        except Exception as exc:  # the video is optional
            vres = {"ok": False, "reason": "%s: %s" % (type(exc).__name__, exc)}
        st["video"] = {k: (os.path.relpath(v, out_dir) if k == "path" else v) for k, v in vres.items()
                       if k in ("ok", "path", "reason", "duration_s")}
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
        fh.write(station_html(st))
    st["paths"] = {"station_json": "station.json", "station_html": "station.html"}
    step("Done", 1.0)
    return st


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
        fn = conn["functions"].get(n, "NC")
        site = q.get("site")
        fill = SITE_COLORS[(site - 1) % len(SITE_COLORS)] if site else ("#555" if fn == "GND" else "none")
        out.append("<rect x='%d' y='%d' width='%d' height='%d' rx='3' fill='%s' stroke='var(--line)'><title>pin %d: %s%s</title></rect>"
                   % (x, y, cell - 3, cell - 3, fill, n, html.escape(fn),
                      (" - board %d %s" % (site, html.escape(q.get("signal", "")))) if site else ""))
        if n in (1, across * along):
            out.append("<text x='%d' y='%d' font-size='8' fill='var(--muted)'>%d</text>" % (x + 2, y + cell + 6 if i else y - 1, n))
    out.append("</svg>")
    return "".join(out)


def station_html(st: Dict[str, Any]) -> str:
    e = html.escape
    cap, stn = st["capacity"], st["station"]
    h = ["<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
         "<title>%s - test station</title>" % e(stn["name"]),
         "<style>:root{--fg:#1d1d1f;--bg:#fff;--muted:#6b6b70;--line:#d9d9de;--card:#f6f6f8;--ok:#2f7d4f;--no:#c0392b}"
         "@media (prefers-color-scheme:dark){:root{--fg:#eee;--bg:#161618;--muted:#9a9aa0;--line:#3a3a3e;--card:#1f1f22}}"
         "body{font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:var(--fg);background:var(--bg);margin:0 auto;"
         "max-width:1100px;padding:16px}h1{font-size:26px;margin:.2em 0}h2{margin-top:1.6em;border-bottom:1px solid var(--line)}"
         "table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;"
         "vertical-align:top}th{color:var(--muted);font-weight:600}.wrap{overflow-x:auto}.muted{color:var(--muted)}"
         ".ok{color:var(--ok);font-weight:600}.no{color:var(--no);font-weight:600}img{max-width:100%;border-radius:8px}"
         ".card{background:var(--card);border-radius:10px;padding:12px 14px;margin:12px 0}.chips span{display:inline-block;"
         "border-radius:10px;padding:1px 8px;margin:2px;color:#fff;font-size:12px}</style></head><body>"]
    h.append("<h1>Test station: %s</h1>" % e(stn["name"]))
    h.append("<p class='muted'>One station, %d board type%s: every fixture is the station's %s mm plate with the mating "
             "connector %s in the same place, so swapping a fixture is all it takes to test another board. Generated "
             "by %s from %s.</p>" % (len(st["boards"]), "" if len(st["boards"]) == 1 else "s",
                                     " x ".join("%g" % v for v in (cap.get("plate_mm") or ["?", "?"])),
                                     e((st.get("connector") or {}).get("name", "")), e(st["generator"]), e(st["station_source"])))
    if st.get("image"):
        h.append("<p><img src='%s' alt='The test station'></p>" % e(st["image"]))
    if (st.get("video") or {}).get("ok"):
        h.append("<p><video controls src='%s' style='max-width:100%%'></video></p>" % e(st["video"]["path"]))
    h.append("<h2>What the station offers</h2><div class='wrap'><table><tr><th>Analog inputs</th><th>Switched supplies</th>"
             "<th>Buses (instances)</th><th>Digital lines</th><th>Plate</th></tr>")
    h.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s mm</td></tr></table></div>" % (
        cap.get("inputs", "-"), cap.get("supplies", "-"),
        ", ".join("%s x%d" % (b.upper(), n) for b, n in sorted((cap.get("buses") or {}).items())) or "-",
        cap.get("dio", "-"), " x ".join("%g" % v for v in (cap.get("plate_mm") or []))))
    h.append("<h2>Board types on the station</h2><div class='wrap'><table><tr><th>Board</th><th>Boards per fixture</th>"
             "<th>Inputs used</th><th>Supplies</th><th>Buses</th><th>Limited by</th><th>Outputs</th></tr>")
    for b in st["boards"]:
        if b.get("status") != "ok":
            h.append("<tr><td><b>%s</b><div class='muted'>%s mm</div></td><td class='no'>%s</td><td colspan='4'>%s</td><td></td></tr>"
                     % (e(b["name"]), " x ".join("%g" % v for v in b.get("size_mm") or []), e(b.get("status", "")),
                        e(b.get("reason", ""))))
            continue
        o = b.get("outputs") or {}
        links = " &middot; ".join("<a href='%s'>%s</a>" % (e(o[k]), lab) for k, lab in (
            ("guide", "guide"), ("report", "report"), ("fixture", "fixture PCB"), ("video", "video"), ("runner", "runner")) if k in o)
        h.append("<tr><td><b>%s</b><div class='muted'>%s mm</div></td><td class='ok'>%d</td><td>%s</td><td>%s</td><td>%s</td>"
                 "<td>%s</td><td>%s</td></tr>" % (
                     e(b["name"]), " x ".join("%g" % v for v in b["size_mm"]), b["batch"],
                     "%d x %d = %d of %s (%s%%)" % (b["batch"], b["channels"], b["inputs_used"], b.get("inputs_total") or "-",
                                                    b.get("utilization_pct") if b.get("utilization_pct") is not None else "-"),
                     "%d of %s" % (b["supplies_used"], b.get("supplies_total") or "-") if b["supplies_used"] else "-",
                     ", ".join("%s x%d" % (k.upper(), v) for k, v in sorted(b.get("buses", {}).items())) or "-",
                     e(b.get("limit") or "-"), links or "-"))
    h.append("</table></div>")
    conn = st.get("connector")
    if conn:
        h.append("<h2>Connector %s: which board uses which pin</h2>" % e(conn["name"]))
        for b in st["boards"]:
            if b.get("status") != "ok":
                continue
            chips = "".join("<span style='background:%s'>board %d</span>" % (SITE_COLORS[(k - 1) % len(SITE_COLORS)], k)
                            for k in range(1, b["batch"] + 1))
            h.append("<div class='card'><b>%s</b> <span class='chips'>%s<span style='background:#555'>GND</span></span>%s</div>"
                     % (e(b["name"]), chips, connector_svg(conn, b.get("pins") or [])))
    h.append("<h2>Swapping to another board type</h2><ol><li>Switch the station's supplies off and unscrew the fixture.</li>"
             "<li>Plug the next board type's fixture onto connector %s and screw it down in the same holes.</li>"
             "<li>Run that board's runner (its folder below): its testplan.json knows which station pins its boards use.</li>"
             "<li>Every board has its own nest and toggle clamp on the fixture: slide a board into each nest up to the stop "
             "(above the pogo pins), close its clamp, and the runner tests the boards one by one; open a clamp to take "
             "that board off.%s</li></ol>" % (
                 e((conn or {}).get("name", "")),
                 "".join(" %s: from the %s." % (e(b["name"]), e(b["entry"])) for b in st["boards"] if b.get("entry"))))
    if st.get("warnings"):
        h.append("<h2>Notes</h2><ul>%s</ul>" % "".join("<li>%s</li>" % e(w) for w in st["warnings"]))
    h.append("</body></html>")
    return "\n".join(h)
