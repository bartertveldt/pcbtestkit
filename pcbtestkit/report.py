"""Self-contained HTML report (findings, board map, test plan, jig)."""
from __future__ import annotations

import base64
import html
import os
from typing import Any, Dict, List, Optional

from .project import Design

SEV_COLOR = {"critical": "#d13438", "major": "#e8710a", "minor": "#c9a400", "info": "#5b7fa6"}


def _e(s: Any) -> str:
    return html.escape("" if s is None else str(s))


def board_svg(design: Design, findings: List[Dict[str, Any]], plan: Optional[Dict[str, Any]], width: int = 900) -> str:
    b = design.board
    bb = b.outline_bbox().grow(3)
    scale = width / max(bb.w, 1)
    hgt = bb.h * scale
    out = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="%.3f %.3f %.3f %.3f" width="100%%" style="max-height:%dpx" '
           'role="img" aria-label="Board map">' % (bb.x0, bb.y0, bb.w, bb.h, int(hgt) + 40)]
    out.append('<rect x="%.3f" y="%.3f" width="%.3f" height="%.3f" fill="var(--board-bg)"/>' % (bb.x0, bb.y0, bb.w, bb.h))
    for s in b.edge_segs:
        out.append('<line x1="%.3f" y1="%.3f" x2="%.3f" y2="%.3f" stroke="var(--edge)" stroke-width="0.25"/>' % s)
    for fp in design.components():
        poly = fp.courtyard_polygon()
        pts = " ".join("%.3f,%.3f" % p for p in poly)
        dash = ' stroke-dasharray="0.6 0.4"' if fp.side == "bottom" else ""
        out.append('<polygon points="%s" fill="none" stroke="var(--crt-%s)" stroke-width="0.12"%s><title>%s %s (%s)</title></polygon>' %
                   (pts, fp.side, dash, _e(fp.ref), _e(fp.value), fp.side))
        for p in fp.pads:
            pp = " ".join("%.3f,%.3f" % q for q in p.polygon())
            out.append('<polygon points="%s" fill="var(--pad-%s)" opacity="0.75"/>' % (pp, "tht" if p.is_tht or p.is_npth else fp.side))
        out.append('<text x="%.3f" y="%.3f" font-size="1.1" text-anchor="middle" fill="var(--txt)">%s</text>' % (fp.x, fp.y, _e(fp.ref)))
    if plan:
        for p in plan["probes"]:
            col = {"ground": "var(--gnd)", "supply": "var(--sup)"}.get(p["role"], "var(--probe)")
            out.append('<circle cx="%.3f" cy="%.3f" r="0.9" fill="none" stroke="%s" stroke-width="0.35"><title>%s %s (%s)</title></circle>' %
                       (p["x"], p["y"], col, _e(p["id"]), _e(p["net"]), _e(p.get("channel") or p["role"])))
            out.append('<text x="%.3f" y="%.3f" font-size="1.2" fill="%s" font-weight="bold">%s</text>' % (p["x"] + 1.1, p["y"] - 1.0, col, _e(p["id"])))
    for f in findings:
        for it in f.get("items", [])[:25]:
            if "x" in it and "y" in it:
                out.append('<circle cx="%s" cy="%s" r="0.55" fill="%s" opacity="0.85"><title>%s %s</title></circle>' %
                           (it["x"], it["y"], SEV_COLOR.get(f["severity"], "#888"), _e(f["uid"]), _e(f["title"])))
    out.append("</svg>")
    return "".join(out)


CSS = """
:root{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1f;--muted:#666;--line:#e3e3e0;--board-bg:#e9f1ea;--edge:#222;--crt-top:#3a6ea5;
--crt-bottom:#9a4ea5;--pad-top:#c08a2d;--pad-bottom:#8a5ab5;--pad-tht:#777;--txt:#333;--probe:#0a8f5a;--gnd:#333;--sup:#d13438}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#161618;--card:#1f1f22;--fg:#eee;--muted:#aaa;
--line:#333;--board-bg:#1e2a22;--edge:#ddd;--txt:#ccc;--gnd:#ddd}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 64px}h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 10px}
.muted{color:var(--muted)}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:16px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}.card b{display:block;font-size:22px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:12px;color:var(--muted);font-weight:600}
.sev{display:inline-block;padding:1px 8px;border-radius:99px;color:#fff;font-size:12px;font-weight:600}
.wrap{overflow-x:auto}.small{font-size:12px}code{font-size:12px}.svgbox{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px}
details summary{cursor:pointer}img.jig{max-width:100%;image-rendering:pixelated;border-radius:8px;border:1px solid var(--line)}
"""


def routing_note(st: Optional[Dict[str, Any]]) -> str:
    """One sentence on the fixture board's routing, for the report and the guide."""
    if not st:
        return "Route the ratsnest in KiCad before ordering."
    if not st.get("unrouted_connections"):
        return ("Routed by PCB TestKit (%d connections, %d vias, KiCad's default rules): open it in KiCad, run DRC "
                "and order it." % (st["connections"], st["vias"]))
    return ("The router left %d of %d connection(s) unrouted (%s): route them in KiCad, run DRC and order it."
            % (st["unrouted_connections"], st["connections"], ", ".join(st["unrouted_nets"][:6])))


def build_report(design: Design, spec: Dict[str, Any], plan: Optional[Dict[str, Any]], jig: Optional[Dict[str, Any]],
                 fixture: Optional[Dict[str, Any]], outputs: Dict[str, str]) -> str:
    s = spec["summary"]
    b = spec["board"]
    findings = spec["findings"]
    h: List[str] = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
                    "<title>Production Test Report</title><style>%s</style></head><body><main>" % CSS]
    h.append("<h1>%s %s</h1><div class='muted'>%s &middot; generated %s &middot; %s</div>" %
             (_e(b["title"] or b["name"]), _e(("rev " + b["revision"]) if b["revision"] else "(no revision)"), _e(b["name"]),
              _e(spec["generated"]), _e(spec["generator"])))
    c = s["counts"]
    h.append("<div class='cards'>")
    h.append("<div class='card'><span class='muted'>Readiness</span><b>%s</b><span class='muted'>score %d/100</span></div>" % (_e(s["verdict"]), s["score"]))
    for sev in ("critical", "major", "minor", "info"):
        h.append("<div class='card'><span class='muted'>%s</span><b style='color:%s'>%d</b></div>" % (sev, SEV_COLOR[sev], c.get(sev, 0)))
    if plan:
        cov = plan["coverage"]
        h.append("<div class='card'><span class='muted'>Test coverage</span><b>%s%%</b><span class='muted'>%d/%d nets, %d/%d power</span></div>" %
                 (cov["percent"], cov["nets_probed"], cov["nets_total"], cov["power_nets_probed"], cov["power_nets"]))
    h.append("</div>")
    h.append("<div class='muted small'>Board %.1f x %.1f mm, %d copper layers, %.2f mm thick, %d components (%d SMD top, %d SMD bottom, %d THT).</div>" %
             (b["outline_mm"]["width"], b["outline_mm"]["height"], b["copper_layers"], b["thickness_mm"],
              spec["assembly"]["component_count"], spec["assembly"]["smd_top"], spec["assembly"]["smd_bottom"], spec["assembly"]["tht"]))

    tech = spec.get("technology")
    if tech:
        h.extend(tech_html(tech))
    h.append("<h2>Weak points</h2><div class='wrap'><table><tr><th>#</th><th>Severity</th><th>Finding</th><th>Why it matters / what to do</th><th>Parts / nets</th></tr>")
    for f in findings:
        refs = ", ".join(f.get("refs", [])[:20]) + (" ..." if len(f.get("refs", [])) > 20 else "")
        nets = ", ".join(f.get("nets", [])[:10])
        h.append("<tr><td class='small'>%s</td><td><span class='sev' style='background:%s'>%s</span></td><td><b>%s</b><br><code>%s</code> &middot; %s</td>"
                 "<td>%s<br><span class='muted'>%s</span></td><td class='small'>%s%s</td></tr>" %
                 (_e(f["uid"]), SEV_COLOR.get(f["severity"], "#888"), _e(f["severity"]), _e(f["title"]), _e(f["id"]), _e(f["category"]),
                  _e(f["detail"]), _e(f.get("recommendation", "")), _e(refs), ("<br>" + _e(nets)) if nets else ""))
    h.append("</table></div>")

    h.append("<h2>Board map</h2><div class='muted small'>Courtyards (solid = top, dashed = bottom), pads, probe points "
             "(green = measure, red = supply, dark = ground) and finding locations (coloured dots, hover for details).</div>")
    h.append("<div class='svgbox'>%s</div>" % board_svg(design, findings, plan))

    if plan:
        h.append("<h2>Test scheme</h2><div class='muted small'>Probe side: <b>%s</b> &middot; pogo %s (%s), drill %.2f mm, min pitch %.2f mm &middot; supply: %s</div>" %
                 (_e(plan["probe_side"]), _e(plan["pogo"]["family"]), _e(plan["pogo"]["mount"]), plan["pogo"]["drill_mm"],
                  plan["pogo"]["pitch_mm"], _e("%s @ %s V" % (plan["supply"].get("net"), plan["supply"].get("voltage")))))
        for st in plan.get("stack") or []:
            if st.get("valid"):
                h.append("<div class='muted small'>Stacked board <b>%s</b> (%s, %s side, %.0f mm, outline %s): %s.</div>" % (
                    _e(st["name"]), _e(", ".join(st["refs"]) or "-"), _e(st["side"]), st["height_mm"], _e(st["outline_source"]),
                    "fitted during the test - no probes under it" if st["fitted_at_test"] else "fitted after the test"))
        both = plan["probe_side"] == "both"
        h.append("<div class='wrap'><table><tr><th>Probe</th>%s<th>Net</th><th>Role</th><th>Location (mm)</th><th>Target</th><th>Channel / header pin</th><th>Divider</th><th>Expected</th></tr>"
                 % ("<th>Side</th>" if both else ""))
        for p in plan["probes"]:
            e = p.get("expected") or {}
            exp = ("%s .. %s V" % (e.get("min_v"), e.get("max_v"))) if e.get("min_v") is not None else ("learn" if p.get("channel") else "")
            div = p.get("divider")
            h.append("<tr><td>%s</td>%s<td>%s</td><td>%s</td><td>%.2f, %.2f</td><td>%s (%s)</td><td>%s</td><td>%s</td><td>%s</td></tr>" %
                     (_e(p["id"]), ("<td>%s</td>" % _e(p.get("side", ""))) if both else "", _e(p["net"]), _e(p["role"]), p["x"], p["y"],
                      _e(p["label"]), _e(p["source"]),
                      _e("%s / %s" % (p.get("channel"), p.get("header_pin")) if p.get("channel") else "-"),
                      _e("%g / %g (x%.3f)" % (div["r_top_ohm"], div["r_bottom_ohm"], div["ratio"]) if div else "-"), _e(exp)))
        h.append("</table></div>")
        h.append("<ol>")
        for st in plan["sequence"]:
            n = len(st.get("checks", []) or st.get("items", []) or [])
            h.append("<li><b>%s</b> <span class='muted'>(%s%s)</span> %s</li>" % (_e(st["name"]), _e(st["type"]),
                     ", %d checks" % n if n else "", "<span class='muted'>optional</span>" if st.get("optional") else ""))
        h.append("</ol>")
        if plan["coverage"]["untestable"]:
            h.append("<details><summary>%d net(s) without probe access</summary><ul>%s</ul></details>" % (
                len(plan["coverage"]["untestable"]),
                "".join("<li><code>%s</code> - %s</li>" % (_e(u["net"]), _e(u["reason"])) for u in plan["coverage"]["untestable"])))
    if fixture and fixture.get("plates"):
        h.append("<h2>Fixture boards</h2><div class='muted small'>Two plates (probes from both sides): %s. %s</div>" % (
            _e(", ".join("%s %d pogo pin(s)" % (sd, r_["pogo_count"]) for sd, r_ in fixture["plates"].items())),
            _e(routing_note(fixture.get("routing")))))
        for w in fixture.get("warnings", []):
            h.append("<div class='small' style='color:%s'>&#9888; %s</div>" % (SEV_COLOR["major"], _e(w)))
        if fixture.get("drc"):
            h.append("<div class='small muted'>KiCad DRC on the fixture: %s</div>" % _e(fixture["drc"]))
    elif fixture:
        h.append("<h2>Fixture board</h2><div class='muted small'>%d pogo pin(s), %d site(s), outline %s. %s</div>" %
                 (fixture["pogo_count"], len(fixture["sites"]), _e(fixture.get("outline_mode")),
                  _e(routing_note(fixture.get("routing")))))
        for w in fixture.get("warnings", []):
            h.append("<div class='small' style='color:%s'>&#9888; %s</div>" % (SEV_COLOR["major"], _e(w)))
        if fixture.get("drc"):
            h.append("<div class='small muted'>KiCad DRC on the fixture: %s</div>" % _e(fixture["drc"]))
    elif plan:
        mode = (design.cfg.get("fixture") or {}).get("mode", "generate")
        if mode == "own":
            h.append("<h2>Fixture</h2><div class='muted small'>You design the fixture: %d probe point(s) with coordinates, "
                     "nets, tips and expected values are in <code>%s</code>.</div>" % (
                         len(plan["probes"]), _e(os.path.basename(outputs.get("probe_points", "probe-points.csv")))))
        elif mode == "none":
            h.append("<h2>Fixture</h2><div class='muted small'>No fixture (manual test): measure the %d test point(s) with a "
                     "bench supply and a multimeter, following the test scheme.</div>" % len(plan["probes"]))
    if jig:
        h.append("<h2>Solder jig</h2><div class='muted small'>%s x %s x %s mm, support plane at %s mm. Parts to place: %s.</div>" %
                 (jig["jig_size_mm"][0], jig["jig_size_mm"][1], jig["jig_size_mm"][2], jig["support_height_mm"],
                  _e(", ".join(jig["parts_to_place"]) or "none")))
        if jig.get("preview_png") and os.path.exists(jig["preview_png"]):
            with open(jig["preview_png"], "rb") as fh:
                h.append("<p><img class='jig' alt='Jig top view (board seen from its bottom side)' src='data:image/png;base64,%s'></p>" %
                         base64.b64encode(fh.read()).decode())
        h.append("<div class='wrap'><table><tr><th>Part</th><th>Pocket</th><th>Size</th><th>Depth</th><th>Height source</th></tr>")
        for p in jig["pockets"]:
            if p["type"] != "hold":
                continue
            size = ("D%.2f" % p["diameter_mm"]) if "diameter_mm" in p else "%.2f x %.2f" % tuple(p["size_mm"])
            h.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%.2f</td><td class='small'>%s</td></tr>" %
                     (_e(p["ref"]), _e(p["type"]), _e(size), p["depth_mm"], _e(p["height_source"])))
        h.append("</table></div>")
        if jig.get("mesh"):
            h.append("<div class='small muted'>Mesh: %s triangles, watertight: %s</div>" % (jig["mesh"].get("triangles"), jig["mesh"].get("watertight")))
        pj = jig.get("placement")
        if pj:
            h.append("<h3>Placement jig (two-jig workflow)</h3><div class='muted small'>%s x %s x %s mm. The PCB rests top side up "
                     "%.1f mm above the bed; %d lead wells, %d locating stops. %s</div>" %
                     (pj["jig_size_mm"][0], pj["jig_size_mm"][1], pj["jig_size_mm"][2], pj["pcb_rest_height_mm"],
                      pj["lead_wells"], len(pj["stops"]), _e(" ".join(pj.get("notes", [])))))
            if pj.get("preview_png") and os.path.exists(pj["preview_png"]):
                with open(pj["preview_png"], "rb") as fh:
                    h.append("<p><img class='jig' alt='Placement jig top view' src='data:image/png;base64,%s'></p>" %
                             base64.b64encode(fh.read()).decode())
    if jig and len(jig.get("passes") or []) > 1:
        from .jig import jig_of
        h.append("<h3>Build order</h3><ol>")
        for p in jig["passes"]:
            ej = jig_of(jig, p.get("jig"))
            what = ("SMD %s side (%d parts)" % (p["side"], p["parts"]) if p["process"] == "smt" else
                    "hand-solder %s side: %s" % (p["side"], ", ".join(p["parts"])))
            if ej is not None and ej is not jig:
                what += " - %s %s x %s x %s mm%s" % ("paste-print nest" if ej.get("kind") == "paste_nest" else "holding jig",
                                                     ej["jig_size_mm"][0], ej["jig_size_mm"][1], ej["jig_size_mm"][2],
                                                     (", placement jig %s x %s x %s mm" % tuple(ej["placement"]["jig_size_mm"]))
                                                     if ej.get("placement") else "")
            elif ej is jig:
                what += " - the jig above"
            h.append("<li>%s</li>" % _e(what))
        h.append("</ol>")
    h.append("<h2>Files</h2><ul>")
    for k, v in outputs.items():
        h.append("<li>%s: <code>%s</code></li>" % (_e(k), _e(os.path.basename(v))))
    h.append("</ul>")
    if spec.get("warnings"):
        h.append("<details><summary>Warnings</summary><ul>%s</ul></details>" % "".join("<li>%s</li>" % _e(w) for w in spec["warnings"]))
    h.append("</main></body></html>")
    return "\n".join(h)


def _lim_txt(v: Any) -> str:
    if isinstance(v, list):
        return " / ".join(_lim_txt(x) for x in v)
    if isinstance(v, float):
        return "%.3f" % v
    return _e(v)


def tech_html(tech: Dict[str, Any]) -> List[str]:
    """Technology-class section: ordered system, other class checks, and which class systems fit."""
    ok_mark = "&#10003;"
    bad_mark = "<b style='color:%s'>&#10007;</b>" % SEV_COLOR["major"]
    p = tech["primary"]
    req = p["required"]
    tg = p.get("target")
    h = ["<h2>Technology class</h2>"]
    h.append("<div class='muted small'>%s (%s). Needs: <b>%s</b>%s. Ordered: <b>%s</b>%s.</div>" % (
        _e(p["name"]), _e(p["status"]), _e(req.get("class") or "finer than this system"),
        (" &middot; " + _e(", ".join(req.get("services") or []))) if req.get("services") else "",
        _e(tech.get("ordered") or "not set"), (" &middot; " + (ok_mark if tg["ok"] else bad_mark)) if tg else ""))
    limits = (tg or {}).get("limits") or {}
    viol = dict((tg or {}).get("violations") or {})
    viol.update((p.get("process") or {}).get("violations") or {})
    h.append("<div class='wrap'><table><tr><th>Parameter</th><th>Board</th><th>%s limit</th><th></th></tr>" %
             ("Ordered-class" if tg else "Process"))
    labels = {}
    for mid, l in limits.items():
        labels[mid] = l["label"]
    for mid, v in ((p.get("process") or {}).get("violations") or {}).items():
        labels.setdefault(mid, v["label"])
    from .classsys.catalog import METRICS
    for mid, val in sorted(p["measured"].items()):
        lab = labels.get(mid) or METRICS.get(mid, {}).get("label", mid)
        lim = limits.get(mid, {}).get("limit") if mid in limits else None
        v = viol.get(mid)
        mark = "" if lim is None and v is None else (bad_mark + " %d" % v["count"] if v else ok_mark)
        h.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (_e(lab), _lim_txt(val), "-" if lim is None else _lim_txt(lim), mark))
    h.append("</table></div>")
    if req.get("drivers"):
        h.append("<div class='muted small'>Class driven by: %s</div>" % _e("; ".join(
            "%s %s (%s %s needs %s)" % (d["metric"], d["measured"], d["dimension"], d["level"], _lim_txt(d["limit"]))
            for d in req["drivers"][:8])))
    if tech.get("checks"):
        h.append("<h3>Other class checks</h3><div class='wrap'><table><tr><th>System</th><th>Class</th><th>Result</th><th>Details</th></tr>")
        for c in tech["checks"]:
            t = c.get("target") or {}
            v = dict(t.get("violations") or {})
            v.update((c.get("process") or {}).get("violations") or {})
            det = "; ".join("%s: %d (worst %s vs %s)" % (x["label"], x["count"], x.get("worst"), _lim_txt(x.get("limit") or x.get("limits")))
                            for x in v.values())
            h.append("<tr><td>%s</td><td>%s</td><td>%s</td><td class='small'>%s</td></tr>" % (
                _e(c["name"]), _e(t.get("class", "-")), ok_mark if t.get("ok", True) else bad_mark, det or "all checked items pass"))
        h.append("</table></div>")
    if tech.get("overview"):
        h.append("<h3>Which class systems fit this board</h3><div class='wrap'><table><tr><th>System</th><th>Needs</th><th>Fits</th><th>Blocked by</th></tr>")
        for r in tech["overview"]:
            h.append("<tr><td>%s <span class='muted small'>(%s)</span></td><td>%s</td><td>%s</td><td class='small'>%s</td></tr>" % (
                _e(r["name"]), _e(r["status"]), _e(r["class"] or "-"), ok_mark if r["fits"] else bad_mark, _e(", ".join(r["blockers"]))))
        h.append("</table></div>")
    return h
