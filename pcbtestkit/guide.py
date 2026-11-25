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
    for kind in ("relief", "lead", "hold"):
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


def _png(path: Optional[str], cap: str) -> str:
    if not path or not os.path.exists(path):
        return ""
    import base64
    with open(path, "rb") as fh:
        return ("<div class='grid2'><div><img alt='%s' style='max-width:100%%;border-radius:8px' src='data:image/png;base64,%s'>"
                "<div class='cap'>%s</div></div></div>" % (_e(cap), base64.b64encode(fh.read()).decode(), _e(cap)))


def _prog_title(kind: str, plan: Dict[str, Any]) -> str:
    from .program import STANDARD
    if kind == "same":
        return "copy of the board's programming header"
    if kind == "pins":
        return "1 x N pin header 2.54 mm"
    return STANDARD[kind]["title"]


def _program_section(plan: Dict[str, Any], fixture: Optional[Dict[str, Any]]) -> str:
    """The programming connectors: which header they serve, their pinout and how the boards are programmed."""
    heads = (plan.get("program") or {}).get("headers") or []
    if not heads:
        return ""
    sites = (fixture or {}).get("sites") or []
    out = ["<h3>Programming</h3>"]
    for hd in heads:
        conns = [pc for st in sites for pc in st.get("program") or [] if pc["header"] == hd["ref"]]
        kind = hd.get("connector", "same")
        out.append("<p><b>%s</b> (%s, %s): the pogo pins reach every pin of it, and the fixture has %s per board, %s. "
                   "Plug your programmer into it while the board sits on the pins: it programs the board as if it were "
                   "plugged into %s.</p>" % (
                       _e(hd["ref"]), _e(hd["protocol"].upper().replace("_", " ")), _e(hd.get("value") or hd["footprint"]),
                       "a %s" % _e(_prog_title(kind, plan)), ", ".join(_e(pc["ref"]) for pc in conns) or "-", _e(hd["ref"])))
        if conns:
            rows = []
            for q in conns[0]["pins"]:
                board = ("%s pin %s" % (hd["ref"], q["dut_pad"])) if q.get("dut_pad") else (
                    "GND" if q["net"] == "GND" else "not connected")
                rows.append("<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (_e(q["pin"]), _e(q["signal"] or "-"), _e(board)))
            out.append("<table><tr><th>Connector pin</th><th>Signal</th><th>Board</th></tr>%s</table>" % "".join(rows))
        if hd.get("missing"):
            out.append("<p class='small muted'>No probe fits pin(s) %s: program through test points on those nets, or "
                       "probe the other side.</p>" % _e(", ".join(hd["missing"])))
    return "".join(out)


def _step_html(n: int, text: str, img: Optional[str] = None, cap: str = "") -> str:
    pic = _img(img, cap) if img else ""
    if pic:
        pic = "<div style='margin-top:8px;max-width:760px'>%s<div class='cap'>%s</div></div>" % (pic, _e(cap))
    return "<div class='step'><div class='num'>%d</div><div style='flex:1'>%s%s</div></div>" % (n, text, pic)


def _hand_steps(design, jig: Dict[str, Any], side: str, stl: str, place_stl: str, im: Optional[Dict[str, str]] = None) -> str:
    """Numbered steps for one hand-solder pass (two-jig or single-jig workflow) on ``side``. ``im``: the step
    renders (jig2_*) of this jig, shown under the step they illustrate."""
    im = im or {}
    holds = [p for p in jig["pockets"] if p["type"] == "hold"]
    refs = ", ".join("%s (%s)" % (p["ref"], _e(design.board.by_ref[p["ref"]].value) if p["ref"] in design.board.by_ref else "")
                     for p in holds)
    leads = any(p["type"] == "lead" for p in jig["pockets"])
    pj = jig.get("placement")
    out = []
    if pj:
        steps = [("Print both jigs as they are (no supports): <code>%s</code> (placement jig) and <code>%s</code> "
                  "(holding jig)." % (_e(os.path.basename(place_stl)), _e(os.path.basename(stl))),
                  im.get("jig2_print"), "The two printed jigs: placement jig (left) and holding jig (right)"),
                 ("Put the PCB <b>%s side up</b> into the placement jig; the stops around the edge locate it%s." % (
                     _e(side), "; the parts already on the other side sit in its pockets" if pj.get("bottom_pockets") and side != "top" else ""),
                  None, ""),
                 ("Fit the hand-soldered parts from above, the normal way: %s. Mind polarity marks. The leads go through "
                  "their holes into the wells under the board; trim leads longer than %.0f mm." % (
                      refs or "none", max(1.0, pj["lead_clearance_mm"] - 0.5)),
                  im.get("jig2_fit"), "PCB top side up in the placement jig; each part goes straight down into its holes"),
                 ("Put the holding jig on top, <b>pockets down</b>: its posts go through the mounting holes and every "
                  "part sits in its pocket.", im.get("jig2_cover"), "The holding jig comes down over the parts, pockets first"),
                 ("Hold both jigs together and turn the stack over. Lift off the placement jig: every lead now points up.",
                  im.get("jig2_unload"), "Stack turned over, placement jig lifted off: the board lies in the holding jig, leads up"),
                 ("Solder all leads from above, clip them, lift the board out with the finger notches.",
                  im.get("jig2_solder") or im.get("jig_assembled"), "Solder from above; the jig holds every part flush and square")]
        if leads:
            steps.insert(0, ("Clip the soldered leads of the earlier pass short (the holding jig has shallow reliefs for them).", None, ""))
        out.append("".join(_step_html(i + 1, t, p, c) for i, (t, p, c) in enumerate(steps)))
        out.append(_png(pj.get("preview_png"), "Placement jig (%s x %s x %s mm): lead wells, post holes and locating stops" %
                        tuple(pj["jig_size_mm"])))
    else:
        steps = ["Drop the parts upside down into their orange pockets: %s. Mind polarity marks." % (refs or "none"),
                 "Lower the PCB %s-side down onto the alignment posts; the leads slide through their holes." % _e(side),
                 "Solder all leads from above, clip them, lift the board out with the finger notches."]
        out.append("".join("<div class='step'><div class='num'>%d</div><div>%s</div></div>" % (i + 1, t) for i, t in enumerate(steps)))
    return "".join(out)


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
        nsite = len((fixture or {}).get("sites") or []) or int((plan.get("batch") or {}).get("count") or 1)
        pogo = plan["pogo"]
        holes_g = [] if not fixture else None
        fams = Counter((p.get("pogo") or {}).get("family") or pogo["family"] for p in plan["probes"])
        bom = []
        for fam, n in sorted(fams.items()):
            tips = Counter(p["tip"] for p in plan["probes"] if ((p.get("pogo") or {}).get("family") or pogo["family"]) == fam)
            bom.append("%d x pogo pin %s (%s)" % (n * nsite, fam, ", ".join("%d %s tip" % (v * nsite, k) for k, v in tips.items())))
            fine = next((p["pogo"] for p in plan["probes"] if p.get("pogo") and p["pogo"]["family"] == fam), None)
            mount, drill = (fine["mount"], fine["drill_mm"]) if fine else (pogo["mount"], pogo["drill_mm"])
            if mount == "receptacle":
                bom.append("%d x receptacle R%s (fixture drill %.2f mm)" % (n * nsite, fam[1:], drill))
        divs = Counter()
        for p in plan["probes"]:
            if p.get("divider"):
                divs["%g" % p["divider"]["r_top_ohm"]] += nsite
                divs["%g" % p["divider"]["r_bottom_ohm"]] += nsite
        if divs:
            bom.append("0805 resistors: " + ", ".join("%d x %s ohm" % (v, k) for k, v in sorted(divs.items(), key=lambda kv: float(kv[0]))))
        plates = list((fixture or {}).get("plates") or {})
        if plates:
            for sd in plates:
                bom.append("%s plate: 1 x 2 x %d pin header 2.54 mm" % (sd, len((plan.get("headers") or {}).get(sd, plan["header"])) // 2))
            bom.append("1 x 2-pin 5.08 mm terminal block (on the plate with the supply probes)")
        else:
            bom.append("%d x pin header 2 x %d, 2.54 mm; %d x 2-pin 5.08 mm terminal block" % (nsite, len(plan["header"]) // 2, nsite))
        progs = [pc for st in (fixture or {}).get("sites") or [] for pc in st.get("program") or []]
        if progs:
            kinds = Counter(pc["connector"] for pc in progs)
            bom.append("programming connector: " + ", ".join("%d x %s" % (v, _prog_title(k, plan)) for k, v in sorted(kinds.items())))
        bom.append("guide pins matching the board's mounting holes; 4 x M3 standoffs")
        h.append("<h3>Parts</h3><ul>%s</ul>" % "".join("<li>%s</li>" % _e(x) for x in bom))
        files_ = ", ".join("<code>%s</code>" % _e(os.path.basename(paths.get("fixture_" + sd, sd))) for sd in plates) if plates else \
            "<code>%s</code>" % _e(os.path.basename(paths.get("fixture", "fixture")))
        from .report import routing_note
        steps = ["%s: %s" % (files_, _e(routing_note((fixture or {}).get("routing")))),
                 "Press the receptacles into the plated holes and solder them; insert the pogo pins (crown tips on through-hole leads, spear tips on flat pads - see the probes CSV).",
                 "Fit the guide pins in the GUIDE holes; they locate the board through its mounting holes. Mount the fixture on M3 standoffs.",
                 "Solder the divider resistors, header and power terminal. Wire the header to the measurement hardware as shown below."]
        if plates:
            steps.append("Two plates (clamshell): the board lies on the bottom plate's guide pins; the top plate is mirrored, "
                         "comes down from above on the same guide pins and presses its probes onto the top side. Wire both "
                         "headers to the measurement hardware (channel numbers are shared).")
        if nsite > 1:
            b_ = plan.get("batch") or {}
            steps.append("The fixture takes a batch of <b>%d boards</b> (%d x %d sites, numbered row by row from the top left, "
                         "S1..S%d on the silkscreen). Every site has its own header and power terminal; wire them as "
                         "<code>runner/hardware.json</code> describes (one power switch per site)." % (
                             nsite, b_.get("cols", nsite), b_.get("rows", 1), nsite))
        h.append("".join("<div class='step'><div class='num'>%d</div><div>%s</div></div>" % (i + 1, t) for i, t in enumerate(steps)))
        h.append("<h3>Wiring</h3><div class='svgbox'>%s</div>" % wiring_svg(plan))
        h.append(_program_section(plan, fixture))
        h.append("</section>")

    stacks = [st for st in plan.get("stack") or [] if st.get("valid")]
    if stacks:
        rows = []
        for st in stacks:
            how = ("fitted during the test: no probe under it%s" % (
                       ", the fixture plate has a window for it" if any(k.get("kind") == "stack" and k["ref"] == st["name"]
                                                                       for k in plan.get("fixture_keepouts") or []) else "")
                   if st["fitted_at_test"] else "fitted after the test: the board is tested without it")
            rows.append("<li><b>%s</b> on %s (%s side, %.0f mm high): %s.</li>" % (
                _e(st["name"]), _e(", ".join(st["refs"]) or "-"), _e(st["side"]), st["height_mm"], _e(how)))
        h.append("<section><h3>Stacked boards</h3><ul>%s</ul></section>" % "".join(rows))

    # ---- test ----------------------------------------------------------------------------------------
    h.append("<section id='test'><h2>2 &middot; Run a production test</h2>")
    if mode == "generate":
        h.append("<div class='grid2'><div>%s<div class='cap'>Lower the board onto the guide pins: a pogo pin stands under "
                 "every test point...</div></div><div>%s<div class='cap'>"
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
    nsites = int((plan.get("batch") or {}).get("count") or 1)
    if mode == "generate" and nsites > 1:
        steps[2] = ("For every batch: put a board in each of the %d sites, press down, run <code>python3 pcbtest_runner.py test "
                    "--loop</code>. The runner tests site 1 to %d in turn, asks the serial number of each board and gives a "
                    "verdict per board (<code>--site 2</code> retests one board)." % (nsites, nsites))
    if mode == "generate" and (plan.get("program") or {}).get("headers"):
        cmd = (plan["program"] or {}).get("command")
        steps.insert(3, ("Programming (step SP): the runner flashes each board with <code>%s</code> through its PROG connector "
                         "(start it with <code>--allow-commands</code>)." % _e(" ".join(cmd))) if cmd else
                     ("Programming (step SP): when the runner asks, program the board through the fixture's PROG connector of "
                      "that site with your programmer, then answer y. Set <code>program.command</code> to let the runner do it."))
    for a in plan.get("after_test") or []:
        steps.append("After a PASS, fit the stacked board <b>%s</b>%s." % (_e(a["fit"]), (" on " + _e(", ".join(a["refs"]))) if a["refs"] else ""))
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
        from .jig import jig_of
        passes = jig.get("passes") or []
        sides_ = [p["side"] for p in passes if p["process"] == "hand"]
        h.append("<section id='jig'><h2>3 &middot; Hand-solder with the %s</h2>" % ("jigs" if jig.get("placement") or len(sides_) > 1 else "jig"))
        if len(passes) > 1:
            rows = []
            for p in passes:
                what = ("reflow the %d SMD part(s) on the %s side" % (p["parts"], p["side"]) if p["process"] == "smt" else
                        "hand-solder %s (%s side)" % (", ".join(p["parts"]), p["side"]))
                tool = {"nest": "paste-print nest", "hand": "jig set"}.get((p.get("jig") or "").split("_")[0], "")
                rows.append("<tr><td><b>%d</b></td><td>%s</td><td class='small'>%s</td></tr>" % (p["pass"], _e(what), _e(tool)))
            h.append("<h3>Build order</h3><table><tr><th>Pass</th><th>What</th><th>Jig</th></tr>%s</table>" % "".join(rows))
        for p in passes:
            ej = jig_of(jig, p.get("jig"))
            if ej is None or not p["jig"].startswith("nest_"):
                continue
            key = p["jig"]
            h.append("<h3>Paste-print nest for the %s side (pass %d)</h3>" % (_e(p["side"]), p["pass"]))
            h.append("<p>The %s side is printed after the other side has been reflowed. Print <code>%s</code>, put the PCB in "
                     "it %s side up: its parts on the underside sit in pockets, the board lies flat and the frame around it "
                     "is flush with the board, so the stencil lies flat for printing the paste. PLA or PETG: the nest does "
                     "not go into the reflow oven.</p>" % (_e(p["side"]), _e(os.path.basename(paths.get("jig_%s_stl" % key, "paste-nest.stl"))),
                                                           _e(p["side"])))
            h.append(_png(ej.get("preview_png"), "Paste-print nest (%s x %s x %s mm)" % tuple(ej["jig_size_mm"])))
        jb = jig.get("batch") or {}
        if jb.get("boards_per_jig", 1) > 1 or jb.get("batch_count", 1) > 1:
            h.append("<p><b>Batch:</b> every jig holds %d board(s) (%d x %d)%s.</p>" % (
                jb["boards_per_jig"], jb["cols"], jb["rows"],
                "; print %d sets for a batch of %d (one set for all of them does not fit the %.0f x %.0f mm printer bed)" % (
                    jb["sets"], jb["batch_count"], jb["bed_mm"][0], jb["bed_mm"][1]) if jb["sets"] > 1 else ""))
        side0 = jig.get("side", "top")
        if len(sides_) > 1 or any((p.get("jig") or "").startswith("nest_") for p in passes):
            h.append("<h3>%s-side parts</h3>" % _e(side0.capitalize()))
        h.append("<div class='grid2'><div>%s<div class='cap'>3D-printed jig (%s x %s x %s mm)</div></div><div class='svgbox'>%s"
                 "<div class='legend small'><span style='background:var(--hold)'></span>place part here"
                 "<span style='background:var(--relief)'></span>clearance for assembled parts"
                 "<span style='background:var(--post)'></span>alignment post</div></div></div>" % (
                     _img(im.get("jig_iso"), "Solder jig"), jig["jig_size_mm"][0], jig["jig_size_mm"][1], jig["jig_size_mm"][2],
                     pocket_map_svg(jig)))
        h.append("<p class='small muted'>Print <code>%s</code> as it is (pockets face up, no supports): PLA or PETG, 0.2 mm layers, "
                 "3 walls. The pocket map is the jig as it lies on your desk.</p>" % _e(os.path.basename(paths.get("jig_stl", "jig.stl"))))
        h.append(_hand_steps(design, jig, side0, paths.get("jig_stl", "jig.stl"), paths.get("jig_place_stl", "jig-placement.stl"), im))
        if not jig.get("placement"):        # single-jig workflow: parts in the pockets, PCB lowered onto them
            h.append("<div class='grid2'><div>%s<div class='cap'>Parts in their pockets, PCB coming down (top side facing the jig)</div></div>"
                     "<div>%s<div class='cap'>PCB seated: solder from above</div></div></div>" %
                     (_img(im.get("jig_exploded"), "Exploded jig"), _img(im.get("jig_assembled"), "Assembled jig")))
        for p in passes:
            ej = jig_of(jig, p.get("jig"))
            if ej is None or ej is jig:
                continue
            key = p["jig"]
            if key.startswith("hand_"):
                h.append("<h3>%s-side parts (pass %d)</h3>" % (_e(p["side"].capitalize()), p["pass"]))
                h.append("<div class='grid2'><div class='svgbox'>%s<div class='cap'>Holding jig for the %s-side parts (%s x %s x %s mm), "
                         "as it lies on your desk. Small circles: reliefs for the soldered leads of pass %s.</div></div></div>" % (
                             pocket_map_svg(ej), _e(p["side"]), ej["jig_size_mm"][0], ej["jig_size_mm"][1], ej["jig_size_mm"][2],
                             ", ".join(str(q["pass"]) for q in passes if q["process"] == "hand" and q["pass"] < p["pass"]) or "-"))
                h.append(_hand_steps(design, ej, p["side"], paths.get("jig_%s_stl" % key, "jig-%s.stl" % p["side"]),
                                     paths.get("jig_%s_place_stl" % key, "jig-%s-placement.stl" % p["side"])))
        h.append("</section>")
    h.append("<section id='files'><h2>Files</h2><ul>%s</ul></section>" % "".join(
        "<li>%s: <code>%s</code></li>" % (_e(k), _e(os.path.basename(v))) for k, v in paths.items()))
    h.append("</main></body></html>")
    return "\n".join(h)
