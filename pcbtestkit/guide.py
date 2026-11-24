"""Per-board visual usage guide (self-contained HTML): how to build and use the fixture and the jig."""
from __future__ import annotations

import base64
import html
import math
import os
from collections import Counter
from typing import Any, Dict, List, Optional

from . import classify as C
from .report import CSS, SEV_COLOR, board_svg

_e = html.escape


def _img(path: Optional[str], alt: str, cls: str = "shot") -> str:
    if not path or not os.path.exists(path):
        return ""
    with open(path, "rb") as fh:
        data = base64.b64encode(fh.read()).decode()
    return "<img class='%s' alt='%s' src='data:image/png;base64,%s'>" % (cls, _e(alt), data)


# --------------------------------------------------------------------------- #
# Diagrams
# --------------------------------------------------------------------------- #
def pocket_map_svg(jig: Dict[str, Any]) -> str:
    """Top view of the jig (Y up, as printed) with every pocket labelled."""
    W_, L_ = jig["jig_size_mm"][0], jig["jig_size_mm"][1]
    pad = 2.0
    out = ["<svg xmlns='http://www.w3.org/2000/svg' viewBox='%.2f %.2f %.2f %.2f' width='100%%' style='max-height:560px' "
           "role='img' aria-label='Jig pocket map'>" % (-pad, -pad, W_ + 2 * pad, L_ + 2 * pad)]
    out.append("<rect x='0' y='0' width='%.2f' height='%.2f' rx='1.5' fill='var(--jig)' stroke='var(--edge)' stroke-width='0.3'/>" % (W_, L_))
    bx0, by0, bx1, by1 = jig["board_bbox_in_jig_mm"]
    out.append("<rect x='%.2f' y='%.2f' width='%.2f' height='%.2f' fill='none' stroke='var(--edge)' stroke-dasharray='1 0.8' "
               "stroke-width='0.25'/>" % (bx0, L_ - by1, bx1 - bx0, by1 - by0))
    for post in jig.get("posts", []):
        cx, cy = post["center"]
        out.append("<circle cx='%.2f' cy='%.2f' r='%.2f' fill='var(--post)'/>" % (cx, L_ - cy, post["diameter_mm"] / 2))
    for kind in ("relief", "hold"):
        for p in jig["pockets"]:
            if p["type"] != kind:
                continue
            cx, cy = p["center"]
            y = L_ - cy
            fill = "var(--hold)" if kind == "hold" else "var(--relief)"
            if "diameter_mm" in p:
                out.append("<circle cx='%.2f' cy='%.2f' r='%.2f' fill='%s' stroke='var(--edge)' stroke-width='0.15'/>" %
                           (cx, y, p["diameter_mm"] / 2, fill))
            else:
                w, h = p["size_mm"]
                out.append("<rect x='%.2f' y='%.2f' width='%.2f' height='%.2f' fill='%s' stroke='var(--edge)' stroke-width='0.15' "
                           "transform='rotate(%.1f %.2f %.2f)'/>" % (cx - w / 2, y - h / 2, w, h, fill, p.get("rotation_deg", 0.0), cx, y))
            if kind == "hold":
                fs = max(1.4, min(3.0, (p.get("diameter_mm") or min(p.get("size_mm", [3, 3]))) * 0.45))
                out.append("<text x='%.2f' y='%.2f' font-size='%.2f' text-anchor='middle' dominant-baseline='central' "
                           "font-weight='700' fill='var(--holdtxt)'>%s</text>" % (cx, y, fs, _e(p["ref"])))
    out.append("</svg>")
    return "".join(out)


def wiring_svg(plan: Dict[str, Any]) -> str:
    """Fixture header -> ADS1115 boards -> Raspberry Pi, plus the supply path."""
    chans = [h for h in plan["header"] if h["signal"].startswith("CH")]
    n_adc = max(1, int(math.ceil(len(chans) / 4.0)))
    row = 18
    hdr_h = 66 + row * len(plan["header"])
    adc_h = 150
    h = max(hdr_h, n_adc * (adc_h + 16)) + 150
    o = ["<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 980 %d' width='100%%' role='img' aria-label='Wiring diagram' "
         "font-family='-apple-system,Segoe UI,sans-serif' font-size='13'>" % h]

    def box(x, y, w, hh, title, sub=""):
        o.append("<rect x='%d' y='%d' width='%d' height='%d' rx='8' fill='var(--card)' stroke='var(--edge)'/>" % (x, y, w, hh))
        o.append("<text x='%d' y='%d' font-weight='700' fill='var(--fg)'>%s</text>" % (x + 10, y + 20, _e(title)))
        if sub:
            o.append("<text x='%d' y='%d' fill='var(--muted)' font-size='11'>%s</text>" % (x + 10, y + 36, _e(sub)))

    box(20, 20, 260, hdr_h, "Fixture header", "2 x %d, 2.54 mm (pin: signal, DUT net)" % (len(plan["header"]) // 2))
    pin_y = {}
    for i, hd in enumerate(plan["header"]):
        y = 20 + 62 + i * row
        label = hd["signal"] + (("  " + C.base_net(hd.get("net", ""))) if hd.get("net") else "")
        o.append("<text x='34' y='%d' fill='var(--muted)' font-size='11'>%2d</text><text x='60' y='%d' fill='var(--fg)' "
                 "font-size='11'>%s</text>" % (y, hd["pin"], y, _e(label[:30])))
        o.append("<circle cx='272' cy='%d' r='3' fill='var(--edge)'/>" % (y - 4))
        pin_y[hd["signal"]] = y - 4
    for k in range(n_adc):
        y0 = 20 + k * (adc_h + 16)
        box(420, y0, 200, adc_h, "ADS1115 #%d" % (k + 1), "I2C address 0x%02X, 3V3 + GND" % (0x48 + k))
        for j in range(4):
            idx = k * 4 + j
            yy = y0 + 62 + j * 22
            o.append("<circle cx='428' cy='%d' r='3' fill='var(--edge)'/><text x='438' y='%d' fill='var(--fg)' font-size='11'>A%d</text>" %
                     (yy - 4, yy, j))
            if idx < len(chans):
                ch = chans[idx]["signal"]
                py_ = pin_y.get(ch, yy - 4)
                o.append("<path d='M272 %d C 350 %d, 350 %d, 428 %d' fill='none' stroke='var(--wire)' stroke-width='1.6'/>" %
                         (py_, py_, yy - 4, yy - 4))
                o.append("<text x='470' y='%d' fill='var(--muted)' font-size='11'>%s</text>" % (yy, _e(ch)))
        o.append("<path d='M620 %d L 700 %d' stroke='var(--i2c)' stroke-width='2'/>" % (y0 + 40, min(y0 + 40, 130)))
    box(700, 20, 260, 120, "Raspberry Pi", "I2C: GPIO2 SDA, GPIO3 SCL")
    o.append("<text x='710' y='76' fill='var(--fg)' font-size='11'>3V3 + GND to every ADS1115</text>")
    o.append("<text x='710' y='94' fill='var(--fg)' font-size='11'>GPIO17 -> relay / MOSFET (DUT power)</text>")
    o.append("<text x='710' y='112' fill='var(--fg)' font-size='11'>optional INA219 (I2C 0x40) current</text>")
    sup = plan.get("supply") or {}
    if sup.get("net"):
        y = h - 100
        box(20, y, 260, 70, "Power terminal (fixture)", "+ = %s, - = GND" % sup["net"])
        box(420, y, 200, 70, "Relay / MOSFET", "switched by GPIO17")
        box(700, y, 260, 70, "Bench supply", "%s V, current limit %s A" % (sup.get("voltage"), sup.get("current_limit_a")))
        o.append("<path d='M280 %d L418 %d M620 %d L698 %d' stroke='var(--pwr)' stroke-width='2.5'/>" % (y + 35, y + 35, y + 35, y + 35))
    o.append("</svg>")
    return "".join(o)


# --------------------------------------------------------------------------- #
GUIDE_CSS = CSS + """
:root{--jig:#f6b24b;--hold:#d9480f;--holdtxt:#fff;--relief:#c99a52;--post:#8a6a33;--wire:#2f7d4f;--i2c:#3a6ea5;--pwr:#d13438;--card:#fff;--fg:#1d1d1f}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--jig:#b27a26;--relief:#8a6a3a;--card:#1f1f22;--fg:#eee}}
.shot{width:100%;border-radius:10px;border:1px solid var(--line);background:#cfd3e0}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px;margin:12px 0}
.step{display:flex;gap:12px;margin:10px 0}.num{flex:0 0 30px;height:30px;border-radius:50%;background:#2f7d4f;color:#fff;
display:flex;align-items:center;justify-content:center;font-weight:700}.cap{font-size:12px;color:var(--muted);margin-top:4px}
nav a{margin-right:14px;font-size:13px}section{margin-top:28px}video{width:100%;border-radius:10px}
.legend span{display:inline-block;width:12px;height:12px;border-radius:3px;margin:0 4px -1px 12px}
"""


def build_guide(design, spec: Dict[str, Any], plan: Dict[str, Any], fixture: Optional[Dict[str, Any]],
                jig: Optional[Dict[str, Any]], visuals: Dict[str, Any], paths: Dict[str, str]) -> str:
    b = spec["board"]
    im = (visuals or {}).get("images", {})
    tech = spec.get("technology") or {}
    s = spec["summary"]
    h: List[str] = ["<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
                    "<title>Production Guide</title><style>%s</style></head><body><main>" % GUIDE_CSS]
    h.append("<h1>%s - production guide</h1><div class='muted'>%s%s &middot; generated by %s</div>" % (
        _e(b["title"] or b["name"]), _e(("rev " + b["revision"]) if b["revision"] else ""), " &middot; " + _e(b["name"]), _e(spec["generator"])))
    mode = (design.cfg.get("fixture") or {}).get("mode", "generate")
    fix_title = {"generate": "Build the fixture", "own": "Your test fixture", "none": "Test points"}.get(mode, "Build the fixture")
    h.append("<nav><a href='#order'>Before you order</a><a href='#fixture'>1 %s</a><a href='#test'>2 Run a test</a>" % fix_title +
             "<a href='#jig'>3 Solder with the jigs</a><a href='#files'>Files</a></nav>")
    if paths.get("video") and os.path.exists(paths["video"]):
        h.append("<section><h2>Explainer video</h2><video controls src='%s'></video></section>" % _e(os.path.basename(paths["video"])))
    h.append("<div class='grid2'><div>%s<div class='cap'>The board (KiCad 3D render)</div></div><div>%s<div class='cap'>Bottom side</div></div></div>" %
             (_img(im.get("dut_iso"), "Board, 3D view"), _img(im.get("dut_bottom"), "Board bottom")))

    # ---- before you order -------------------------------------------------------------------
    h.append("<section id='order'><h2>Before you order the boards</h2>")
    req = (tech.get("required") or {})
    h.append("<div class='cards'><div class='card'><span class='muted'>Readiness</span><b>%s</b><span class='muted'>score %d/100</span></div>"
             "<div class='card'><span class='muted'>Technology class needed</span><b>%s</b><span class='muted'>%s</span></div>"
             "<div class='card'><span class='muted'>Class ordered</span><b>%s</b><span class='muted'>%s</span></div>"
             "<div class='card'><span class='muted'>Test coverage</span><b>%s%%</b><span class='muted'>%d probes</span></div></div>" % (
                 _e(s["verdict"]), s["score"], _e(req.get("class") or "finer than %s" % tech.get("name", "the system")),
                 _e(", ".join(req.get("services") or []) or tech.get("name", "")),
                 _e(tech.get("ordered") or "not set"), _e(tech.get("name", "")), plan["coverage"]["percent"], len(plan["probes"])))
    fabs = [r for r in tech.get("overview") or [] if r.get("kind") != "standard"]
    fits = [r for r in fabs if r["fits"] and r["class"]]
    if fits:
        h.append("<div class='muted small'>Fab class systems this board fits as drawn: %s.</div>" % _e(
            ", ".join("%s %s" % (r["name"], r["class"]) for r in fits)))
    elif fabs:
        h.append("<div class='muted small'>None of the %d built-in fab class systems accepts the board as drawn; "
                 "the report's class overview lists what blocks each one.</div>" % len(fabs))
    top = [f for f in spec["findings"] if f["severity"] in ("critical", "major") and not f.get("waived")][:8]
    if top:
        h.append("<table><tr><th>Severity</th><th>Fix before ordering</th><th>What to do</th></tr>")
        for f in top:
            h.append("<tr><td><span class='sev' style='background:%s'>%s</span></td><td><b>%s</b><br><span class='muted small'>%s</span></td>"
                     "<td class='small'>%s</td></tr>" % (SEV_COLOR[f["severity"]], f["severity"], _e(f["title"]),
                                                       _e(", ".join(f.get("refs", [])[:10])), _e(f.get("recommendation", ""))))
        h.append("</table>")
    h.append("</section>")

    # ---- fixture ------------------------------------------------------------------------------
    points_cap = "<div class='cap'>Probe points on the board (red = supply, dark = GND, green = measured)</div>"
    if mode == "none":
        h.append("<section id='fixture'><h2>1 &middot; Test points (no fixture)</h2>")
        h.append("<div class='svgbox'>%s%s</div>" % (board_svg(design, [], plan, 700), points_cap))
        h.append("<p>No fixture is made for this board. Power it from a bench supply and measure the marked points with a "
                 "multimeter, following the steps and limits in section 2.</p></section>")
    elif mode == "own":
        h.append("<section id='fixture'><h2>1 &middot; Your test fixture</h2>")
        h.append("<div class='svgbox'>%s%s</div>" % (board_svg(design, [], plan, 700), points_cap))
        steps = ["Design the fixture with one probe at every point in <code>%s</code>: KiCad board coordinates of this board "
                 "(mm, seen from the top; mirror x when the fixture probes the bottom side), net, role, suggested tip and the "
                 "expected voltage." % _e(os.path.basename(paths.get("probe_points", "probe-points.csv"))),
                 "Probe side: <b>%s</b>. Pogo family %s, drill %.2f mm." % (_e(plan["probe_side"]), _e(plan["pogo"]["family"]),
                                                                             plan["pogo"]["drill_mm"]),
                 "Wire the probes to the measurement hardware as shown below (channel and header pin per probe are in the CSV)."]
        h.append("".join("<div class='step'><div class='num'>%d</div><div>%s</div></div>" % (i + 1, t) for i, t in enumerate(steps)))
        h.append("<h3>Wiring</h3><div class='svgbox'>%s</div></section>" % wiring_svg(plan))
    else:
        h.append("<section id='fixture'><h2>1 &middot; Build the test fixture</h2>")
        h.append("<div class='grid2'><div>%s<div class='cap'>Fixture PCB with pogo pins (render of the generated .kicad_pcb)</div></div>"
                 "<div class='svgbox'>%s%s</div></div>" % (_img(im.get("fixture_iso"), "Fixture"), board_svg(design, [], plan, 700), points_cap))
        tips = Counter(p["tip"] for p in plan["probes"])
        pogo = plan["pogo"]
        holes_g = [] if not fixture else None
        bom = ["%d x pogo pin %s (%s)" % (len(plan["probes"]), pogo["family"], ", ".join("%d %s tip" % (v, k) for k, v in tips.items()))]
        if pogo["mount"] == "receptacle":
            bom.append("%d x receptacle R%s (fixture drill %.2f mm)" % (len(plan["probes"]), pogo["family"][1:], pogo["drill_mm"]))
        divs = Counter()
        for p in plan["probes"]:
            if p.get("divider"):
                divs["%g" % p["divider"]["r_top_ohm"]] += 1
                divs["%g" % p["divider"]["r_bottom_ohm"]] += 1
        if divs:
            bom.append("0805 resistors: " + ", ".join("%d x %s ohm" % (v, k) for k, v in sorted(divs.items(), key=lambda kv: float(kv[0]))))
        bom.append("1 x 2 x %d pin header 2.54 mm, 1 x 2-pin 5.08 mm terminal block" % (len(plan["header"]) // 2))
        bom.append("guide pins matching the board's mounting holes; 4 x M3 standoffs")
        h.append("<h3>Parts</h3><ul>%s</ul>" % "".join("<li>%s</li>" % _e(x) for x in bom))
        steps = ["Open <code>%s</code> in KiCad, route the ratsnest (or run Freerouting), run DRC and order the PCB." % _e(os.path.basename(paths.get("fixture", "fixture"))),
                 "Press the receptacles into the plated holes and solder them; insert the pogo pins (crown tips on through-hole leads, spear tips on flat pads - see the probes CSV).",
                 "Fit the guide pins in the GUIDE holes; they locate the board through its mounting holes. Mount the fixture on M3 standoffs.",
                 "Solder the divider resistors, header and power terminal. Wire the header to the measurement hardware as shown below."]
        h.append("".join("<div class='step'><div class='num'>%d</div><div>%s</div></div>" % (i + 1, t) for i, t in enumerate(steps)))
        h.append("<h3>Wiring</h3><div class='svgbox'>%s</div>" % wiring_svg(plan))
        h.append("</section>")

    # ---- test ----------------------------------------------------------------------------------------
    h.append("<section id='test'><h2>2 &middot; Run a production test</h2>")
    if mode == "generate":
        h.append("<div class='grid2'><div>%s<div class='cap'>Lower the board onto the guide pins...</div></div><div>%s<div class='cap'>"
                 "...and press it down: every pogo pin touches its test point</div></div></div>" %
                 (_img(im.get("fixture_dut_exploded"), "Board above the fixture"), _img(im.get("fixture_dut_seated"), "Board on the fixture")))
    sup = plan["supply"]
    steps = ["Power the board from a bench supply: %s at %s V, current limit %s A." % (
                 _e(sup.get("net") or "the supply input"), _e(str(sup.get("voltage") if sup.get("voltage") is not None else "?")),
                 _e(str(sup.get("current_limit_a") or 0.5))),
             "Measure every step below at the marked test points (section 1) and compare with the limits.",
             "Record pass/fail per board; any value outside its limits is a fail."] if mode == "none" else ["Copy the <code>runner</code> folder to the Raspberry Pi and describe your wiring in <code>hardware.json</code>.",
             "Once, with known-good boards: <code>python3 pcbtest_runner.py learn --units 3</code> to fill in signal limits.",
             "For every board: place it, press down, run <code>python3 pcbtest_runner.py test --loop</code> and enter the serial number.",
             "Green PASS: next board. Red FAIL: the failed checks are listed and stored in <code>results/results.csv</code>."]
    h.append("".join("<div class='step'><div class='num'>%d</div><div>%s</div></div>" % (i + 1, t) for i, t in enumerate(steps)))
    h.append("<table><tr><th>Step</th><th>What is checked</th></tr>")
    for st in plan["sequence"]:
        checks = st.get("checks") or st.get("items") or []
        desc = ", ".join("%s %s..%s" % (C.base_net(c.get("net", "")), c.get("min_v", c.get("min_ohm", "")), c.get("max_v", ""))
                         if c.get("net") else c.get("check", "") for c in checks[:6])
        h.append("<tr><td><b>%s</b> %s</td><td class='small'>%s</td></tr>" % (_e(st["id"]), _e(st["name"]), _e(desc)))
    h.append("</table></section>")

    # ---- jig ---------------------------------------------------------------------------------------
    if jig:
        holds = [p for p in jig["pockets"] if p["type"] == "hold"]
        h.append("<section id='jig'><h2>3 &middot; Hand-solder with the %s</h2>" % ("jigs" if jig.get("placement") else "jig"))
        h.append("<div class='grid2'><div>%s<div class='cap'>3D-printed jig (%s x %s x %s mm)</div></div><div class='svgbox'>%s"
                 "<div class='legend small'><span style='background:var(--hold)'></span>place part here"
                 "<span style='background:var(--relief)'></span>clearance for assembled parts"
                 "<span style='background:var(--post)'></span>alignment post</div></div></div>" % (
                     _img(im.get("jig_iso"), "Solder jig"), jig["jig_size_mm"][0], jig["jig_size_mm"][1], jig["jig_size_mm"][2],
                     pocket_map_svg(jig)))
        h.append("<p class='small muted'>Print <code>%s</code> as it is (pockets face up, no supports): PLA or PETG, 0.2 mm layers, "
                 "3 walls. The pocket map is the jig as it lies on your desk.</p>" % _e(os.path.basename(paths.get("jig_stl", "jig.stl"))))
        refs = ", ".join("%s (%s)" % (p["ref"], _e(design.board.by_ref[p["ref"]].value) if p["ref"] in design.board.by_ref else "")
                         for p in holds)
        pj = jig.get("placement")
        if pj:
            steps = ["Print both jigs as they are (no supports): <code>%s</code> (placement jig) and <code>%s</code> "
                     "(holding jig)." % (_e(os.path.basename(paths.get("jig_place_stl", "jig-placement.stl"))),
                                         _e(os.path.basename(paths.get("jig_stl", "jig.stl")))),
                     "Put the PCB <b>top side up</b> into the placement jig; the stops around the edge locate it.",
                     "Fit the hand-soldered parts: %s. Mind polarity marks. The leads go into the wells under the board; "
                     "trim leads longer than %.0f mm." % (refs or "none", max(1.0, pj["lead_clearance_mm"] - 0.5)),
                     "Put the holding jig on top, <b>pockets down</b>: its posts go through the mounting holes and every "
                     "part sits in its pocket.",
                     "Hold both jigs together and turn the stack over. Lift off the placement jig: every lead now points up.",
                     "Solder all leads from above, clip them, lift the board out with the finger notches."]
            if pj.get("preview_png") and os.path.exists(pj["preview_png"]):
                import base64
                with open(pj["preview_png"], "rb") as fh:
                    h.append("<div class='grid2'><div><img alt='Placement jig' style='max-width:100%%;border-radius:8px' "
                             "src='data:image/png;base64,%s'><div class='cap'>Placement jig (%s x %s x %s mm): lead wells, "
                             "post holes and locating stops</div></div></div>" % (
                                 base64.b64encode(fh.read()).decode(), pj["jig_size_mm"][0], pj["jig_size_mm"][1], pj["jig_size_mm"][2]))
        else:
            steps = ["Drop the parts upside down into their orange pockets: %s. Mind polarity marks." % (refs or "none"),
                     "Lower the PCB top-side down onto the alignment posts; the leads slide through their holes.",
                     "Solder all leads from above, clip them, lift the board out with the finger notches."]
        h.append("".join("<div class='step'><div class='num'>%d</div><div>%s</div></div>" % (i + 1, t) for i, t in enumerate(steps)))
        h.append("<div class='grid2'><div>%s<div class='cap'>Parts in their pockets, PCB coming down (top side facing the jig)</div></div>"
                 "<div>%s<div class='cap'>PCB seated: solder from above</div></div></div>" %
                 (_img(im.get("jig_exploded"), "Exploded jig"), _img(im.get("jig_assembled"), "Assembled jig")))
        h.append("</section>")
    h.append("<section id='files'><h2>Files</h2><ul>%s</ul></section>" % "".join(
        "<li>%s: <code>%s</code></li>" % (_e(k), _e(os.path.basename(v))) for k, v in paths.items()))
    h.append("</main></body></html>")
    return "\n".join(h)
