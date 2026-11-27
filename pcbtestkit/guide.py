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


def _mmss(sec: float) -> str:
    m, s = divmod(int(round(sec)), 60)
    return "%d min %02d s" % (m, s) if m else "%d s" % s


def timing_section(t: Dict[str, Any]) -> str:
    """Production timing: the batch flow as a timeline per board, and the time per board by batch size."""
    test, asm = t["test"], t["assembly"]
    n = t["batch"]
    out = ["<h3>Production timing</h3>"]
    line = ("A batch of <b>%d</b> takes about <b>%s</b> to test (%s per board)" % (n, _mmss(test["batch_s"]), _mmss(test["per_board_s"])))
    if n > 1 and test.get("saved_s", 0) > 0:
        line += ", %s less than one board after the other" % _mmss(test["saved_s"])
    if asm.get("batch_s"):
        line += ", and about <b>%s</b> to hand-solder with the jigs (%d parts, %d joints per board, %d jig load%s)" % (
            _mmss(asm["batch_s"]), asm["parts"], asm["joints"], asm["jig_loads"], "" if asm["jig_loads"] == 1 else "s")
    out.append("<p>%s. Per board, everything together: <b>%s</b>.</p>" % (line, _mmss(t["per_board_s"])))
    # timeline (the runner's batch flow)
    lanes = test.get("timeline") or []
    total = max([sg["end"] for ln in lanes for sg in ln["segments"]] or [1.0])
    colors = {"load, check, power up": "#e8a33a", "programming": "#c548c5", "checks while programming": "#3fb950",
              "checks with firmware": "#d4b106", "checks": "#3fb950"}
    W, rowh, left = 760, 22, 70
    svg = ["<svg viewBox='0 0 %d %d' width='100%%' xmlns='http://www.w3.org/2000/svg' font-size='11'>" % (W, 30 + rowh * len(lanes) + 30)]
    for i, ln in enumerate(lanes):
        y = 20 + i * rowh
        svg.append("<text x='4' y='%d'>Board %d</text>" % (y + 14, ln["site"]))
        for sg in ln["segments"]:
            x0 = left + (W - left - 10) * sg["start"] / total
            x1 = left + (W - left - 10) * sg["end"] / total
            svg.append("<rect x='%.1f' y='%d' width='%.1f' height='%d' rx='3' fill='%s'><title>%s: %.0f-%.0f s</title></rect>" % (
                x0, y + 3, max(1.0, x1 - x0), rowh - 6, colors.get(sg["phase"], "#888"), _e(sg["phase"]), sg["start"], sg["end"]))
    y = 24 + rowh * len(lanes)
    for k in range(0, int(total) + 1, max(10, int(total / 8) // 10 * 10 or 10)):
        x = left + (W - left - 10) * k / total
        svg.append("<line x1='%.1f' y1='14' x2='%.1f' y2='%d' stroke='#ccc' stroke-width='0.5'/><text x='%.1f' y='%d' "
                   "text-anchor='middle' fill='#888'>%d s</text>" % (x, x, y, x, y + 14, k))
    svg.append("</svg>")
    legend = "".join("<span style='background:%s'></span>%s" % (c, _e(p)) for p, c in colors.items() if
                     any(sg["phase"] == p for ln in lanes for sg in ln["segments"]))
    out.append("<div class='svgbox'>%s<div class='legend small'>%s</div></div>" % ("".join(svg), legend))
    rows = "".join("<tr%s><td>%d</td><td>%s</td><td>%s</td><td><b>%s</b></td></tr>" % (
        " style='background:#eef7ee'" if r["boards"] == n else "", r["boards"], _mmss(r["test_per_board_s"]),
        _mmss(r["assembly_per_board_s"]), _mmss(r["per_board_s"])) for r in t["by_batch_size"])
    out.append("<table><tr><th>Boards per batch</th><th>Test per board</th><th>Assembly per board</th><th>Per board</th></tr>"
               "%s</table><p class='small'>Past <b>%d</b> boards per batch a bigger batch saves less than 5%% per board. "
               "Estimates (programming %s per board, set your own durations in <code>timing</code> in "
               "<code>pcbtestkit.json</code>); the runner records the real ones in every result file.</p>" % (
                   rows, t["suggested_batch"], _mmss(t["assumptions"]["program_s"])))
    return "".join(out)


def central_pin_table(plan: Dict[str, Any], fixture: Dict[str, Any]) -> str:
    """The batch's one connector: pin, board, signal and the board net it measures, in pin order, two columns."""
    net_of = {p["channel"]: p["net"] for p in plan.get("probes") or [] if p.get("channel")}
    net_of["VIN"] = (plan.get("supply") or {}).get("net") or ""
    for h in (plan.get("program") or {}).get("headers") or []:
        for pin in h.get("pins") or []:
            net_of.setdefault(str(pin.get("signal") or pin.get("pad")), pin.get("net", ""))
    rows = ["<tr><td>%d</td><td>%s</td><td>%s</td><td class='small'>%s</td></tr>" % (
        q["pin"], "S%d" % q["site"] if q["site"] else "-", _e(q["signal"]),
        _e(C.base_net(net_of.get(q["signal"], "")) if q["site"] else "")) for q in fixture["central_header"]["pins"]]
    half = (len(rows) + 1) // 2
    head = "<tr><th>Pin</th><th>Board</th><th>Signal</th><th>Net</th></tr>"
    return ("<div class='grid2'><div><table>%s%s</table></div><div><table>%s%s</table></div></div>" % (
        head, "".join(rows[:half]), head, "".join(rows[half:])))


def facility_pin_table(plan: Dict[str, Any]) -> str:
    """The test facility's connector pins: function, what the fixture puts on it, the board net (site 1)."""
    fac = plan.get("facility") or {}
    net_of = {p["channel"]: p["net"] for p in plan.get("probes") or [] if p.get("channel")}
    for b in fac.get("bus_checks") or []:
        if b.get("via") == "fixture":
            net_of.update(b["signals"])
    net_of["VIN"] = (plan.get("supply") or {}).get("net") or ""
    out = []
    for c in fac.get("connectors") or []:
        rows = "".join("<tr><td>%d</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            q["pin"], _e(q["function"]), _e(q["signal"] if q["signal"] != "NC" else "-"), _e(net_of.get(q["signal"], "")))
            for q in c["pins"] if q["signal"] != "NC" or q["function"] != "NC")
        out.append("<p class='small'>%s%s</p><table><tr><th>Pin</th><th>Facility</th><th>Fixture</th><th>Board net</th></tr>%s"
                   "</table>" % (_e(c["name"]), " (site %d)" % c["site"] if len(fac.get("connectors") or []) > 1 else "", rows))
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
.fitok,.fitbad{border-radius:10px;padding:10px 14px;margin:10px 0;border:1px solid var(--line)}
.fitok{background:#e8f5ec;border-color:#9ccfab}.fitbad{background:#fdecea;border-color:#e6a39b}
.fitok ul,.fitbad ul{margin:6px 0 0 18px;padding:0}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) .fitok{background:#16301f;border-color:#2e6b42}
:root:not([data-theme="light"]) .fitbad{background:#3a1714;border-color:#7a3028}}
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


def _part_img(path: Optional[str], cap: str) -> str:
    if not path or not os.path.exists(path):
        return ""
    with open(path, "rb") as fh:
        return ("<div><img alt='%s' style='max-width:100%%;max-height:260px;border-radius:8px;background:#222' "
                "src='data:image/png;base64,%s'><div class='cap'>%s</div></div>" % (_e(cap), base64.b64encode(fh.read()).decode(), _e(cap)))


def nest_section(fixture: Optional[Dict[str, Any]], paths: Dict[str, str], nsite: int) -> str:
    """How every board is held on the fixture: its nest on sprung standoffs and its toggle clamp."""
    nest = (fixture or {}).get("nest") or {}
    if not nest.get("enabled"):
        return ""
    H = nest["heights"]
    slide = nest.get("style") == "slide"
    toggle = nest.get("clamp") == "toggle"
    h = ["<h3 id='nest'>Board nest and clamp (one per board%s)</h3>" % (", %d in all" % nsite if nsite > 1 else "")]
    h.append("<p>%s rests on two ledges along its edges, %.1f mm above the pogo tips, between two rails%s. %s The nest floats "
             "on four sprung standoffs: closing the clamp pushes board and nest down %.1f mm, the pins come up through the "
             "open middle of the nest and are compressed %.1f mm.</p>" % (
                 "The board slides in from the %s: it" % _e(nest.get("entry", "front")) if slide else "The board drops in: it",
                 H["gap"], " up to the end stop" if slide else " and two end walls",
                 ("Lay it on the rails' lead-in in front of its place and slide it back to the stop." if slide else
                  "Its underside parts would brush over the pogo pins if it slid in, so it is lowered straight in."),
                 H["travel"], H["tips"] - H["work"]))
    if nest.get("gap_reason"):
        h.append("<p class='small muted'>The gap above the tips clears %s on the board's underside while it slides in.</p>" %
                 _e(nest["gap_reason"]))
    imgs = [_part_img(paths.get("nest_preview"), "Nest (print with the flat side down)")]
    if toggle:
        imgs += [_part_img(paths.get("nest_press_preview"), "Press plate (print fingers up)"),
                 _part_img(paths.get("nest_adapter_preview"), "Clamp adapter (print upside down)")]
    h.append("<div class='grid3' style='display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px'>%s</div>"
             % "".join(imgs))
    steps = ["Print the parts below (PETG holds its shape under the spring and clamp load), %s of each." % (
                 "%d" % nsite if nsite > 1 else "one"),
             "Screw the %d mm M3 standoffs onto the fixture board from below (the four NEST holes of every board). Slip a "
             "spring over each standoff." % H["standoff"],
             "Lay the nest on the springs with its bosses over the standoffs and screw a large washer on top of each: the "
             "springs lift the nest against the washers, %.1f mm above the fixture." % H["bottom_rest"]]
    if toggle:
        steps += ["Put the M4 nuts in the channels under the clamp adapter, bolt the toggle clamp onto it (the cross slots "
                  "take base patterns within 4 mm of %g x %g mm; set <code>fixture.nest.clamp_holes_mm</code> for yours and "
                  "reprint only the adapter), and screw the adapter to the fixture from below (the CLAMP holes)." %
                  tuple(nest.get("clamp_holes_mm") or (20, 16)),
                  "Take the rubber tip off the clamp's spindle and fix the press plate in its slot with a nut on each side, "
                  "fingers down, centred over the board.",
                  "Adjust the spindle: with a board in the nest, the clamp should lock just as the nest reaches the bottom of "
                  "its travel (%.1f mm down). The fingers then press the board %.1f mm onto the pins." % (
                      H["travel"], H["tips"] - H["work"])]
    h.append("".join("<div class='step'><div class='num'>%s</div><div>%s</div></div>" % (chr(97 + i), t)
                     for i, t in enumerate(steps)))
    rows = [("Board underside, clamped", H["work"]), ("Pogo tips, free", H["tips"]), ("Board underside, loading", H["rest"]),
            ("Nest travel", H["travel"]), ("Standoff length", H["standoff"]), ("Push-finger length", H["finger"]),
            ("Clamp adapter height", H["adapter"])]
    h.append("<table><tr><th>Height above the fixture board</th><th>mm</th></tr>%s</table>" % "".join(
        "<tr><td>%s</td><td>%.1f</td></tr>" % (_e(a), b_) for a, b_ in (rows if toggle else rows[:5])))
    for w in nest.get("warnings") or []:
        h.append("<p class='warn'>%s</p>" % _e(w))
    return "".join(h)


def _step_html(n: int, text: str, img: Optional[str] = None, cap: str = "") -> str:
    pic = _img(img, cap) if img else ""
    if pic:
        pic = "<div style='margin-top:8px;max-width:760px'>%s<div class='cap'>%s</div></div>" % (pic, _e(cap))
    return "<div class='step'><div class='num'>%d</div><div style='flex:1'>%s%s</div></div>" % (n, text, pic)


def _fit_box(jig: Dict[str, Any], paths: Dict[str, str]) -> str:
    """The fit check of every jig set (cut to the parts' 3D shapes), as a short box."""
    rows = []
    sets = [("%s side" % jig.get("side", "top"), jig)] + [(k.replace("hand_", "").replace("nest_", "paste nest, ") +
                                                           (" side" if k.startswith("hand_") else ""), e)
                                                          for k, e in sorted((jig.get("extra") or {}).items())]
    for label, j in sets:
        f = j.get("fit")
        if not f:
            continue
        rows.append("<li><b>%s:</b> %s</li>" % (_e(label), _e(f["summary"])))
    if not rows:
        return ""
    ok = all((j.get("fit") or {}).get("ok", True) for _, j in sets)
    more = (" <code>jig-fit-check/</code> has the board and every jig in its working position, in KiCad's 3D coordinates: "
            "open them together in any STL viewer." if paths.get("jig_fit_check") else "")
    return ("<div class='%s'><b>%s</b> The jigs are cut to every part's 3D shape (KiCad's 3D export); the check compares "
            "each part with each jig.%s<ul class='small'>%s</ul></div>" % (
                "fitok" if ok else "fitbad", "Fit check passed." if ok else "Fit check FAILED.", more, "".join(rows)))


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
        ins = _insert_step(jig)
        if ins:
            steps.insert(len(steps) - 3, (ins, None, ""))
        out.append("".join(_step_html(i + 1, t, p, c) for i, (t, p, c) in enumerate(steps)))
        out.append(_png(pj.get("preview_png"), "Placement jig (%s x %s x %s mm): lead wells, post holes and locating stops" %
                        tuple(pj["jig_size_mm"])))
    else:
        steps = ["Drop the parts upside down into their orange pockets: %s. Mind polarity marks." % (refs or "none"),
                 "Lower the PCB %s-side down onto the alignment posts; the leads slide through their holes." % _e(side),
                 "Solder all leads from above, clip them, lift the board out with the finger notches."]
        ins = _insert_step(jig)
        if ins:
            steps.insert(0, ins)
        out.append("".join("<div class='step'><div class='num'>%d</div><div>%s</div></div>" % (i + 1, t) for i, t in enumerate(steps)))
    return "".join(out)


def _insert_step(jig: Dict[str, Any]) -> str:
    """The step that lines the pockets with room (heat-resistant inserts, jig.room), or ""."""
    room = jig.get("room") or {}
    if not room:
        return ""
    hot = [r for r, v in room.items() if v.get("why") == "heat insert"]
    own = [r for r in room if r not in hot]
    bits = []
    if hot:
        bits.append("<b>Heat:</b> %s get hot all over while soldered (%s). Line their pockets in the holding jig with "
                    "%.1f mm of heat-resistant material (silicone sheet, PTFE or FR-4) so the hot part never touches the "
                    "print." % (_e(", ".join(hot)), _e(room[hot[0]]["hot"][0] if room[hot[0]].get("hot") else "heavy pins"),
                                room[hot[0]]["mm"]))
    if own:
        bits.append("The pockets of %s have extra room (%s) for what you put in them." % (
            _e(", ".join(own)), _e(", ".join("%s %.1f mm" % (r, room[r]["mm"]) for r in own))))
    return " ".join(bits)


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
    elif paths.get("video_error"):
        why = next((w[len("video not made: "):] for w in design.warnings if w.startswith("video not made: ")), "")
        h.append("<section><h2>Explainer video</h2><p>The video could not be made: %s</p><p class='small muted'>The whole "
                 "error is in <code>%s</code>; the frames rendered so far are kept in <code>visuals/scenes/</code> and are reused "
                 "when you generate again with the same settings.</p>"
                 "</section>" % (_e(why), _e(os.path.basename(paths["video_error"]))))
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
        fac = plan.get("facility") or {}
        fconns = fac.get("connectors") or []
        if fconns:                                  # the test facility's own connector instead of header + terminal
            bom.append("%d x %s connector: the interface of %s (footprint and pin map from the facility)" % (
                len(fconns), ", ".join(sorted({c["name"] for c in fconns})), fac.get("name", fac.get("id", ""))))
        elif plates:
            for sd in plates:
                bom.append("%s plate: 1 x 2 x %d pin header 2.54 mm" % (sd, len((plan.get("headers") or {}).get(sd, plan["header"])) // 2))
            bom.append("1 x 2-pin 5.08 mm terminal block (on the plate with the supply probes)")
        elif (fixture or {}).get("central_header"):
            ch = fixture["central_header"]
            bom.append("1 x pin header 2 x %d, 2.54 mm: the one connector for all %d boards (supply, channels and "
                       "programming lines of each)" % (len(ch["pins"]) // 2, nsite))
        else:
            bom.append("%d x pin header 2 x %d, 2.54 mm; %d x 2-pin 5.08 mm terminal block" % (nsite, len(plan["header"]) // 2, nsite))
        progs = [pc for st in (fixture or {}).get("sites") or [] for pc in st.get("program") or []]
        if progs:
            kinds = Counter(pc["connector"] for pc in progs)
            bom.append("programming connector: " + ", ".join("%d x %s" % (v, _prog_title(k, plan)) for k, v in sorted(kinds.items())))
        nest = (fixture or {}).get("nest") or {}
        if nest.get("enabled"):
            bom += ["%d x %s" % (it["qty"] * nsite, it["part"]) for it in nest.get("bom") or []]
            bom.append("4 x M3 standoffs under the fixture board")
        else:
            bom.append("guide pins matching the board's mounting holes; 4 x M3 standoffs")
        h.append("<h3>Parts</h3><ul>%s</ul>" % "".join("<li>%s</li>" % _e(x) for x in bom))
        files_ = ", ".join("<code>%s</code>" % _e(os.path.basename(paths.get("fixture_" + sd, sd))) for sd in plates) if plates else \
            "<code>%s</code>" % _e(os.path.basename(paths.get("fixture", "fixture")))
        from .report import routing_note
        steps = ["%s: %s" % (files_, _e(routing_note((fixture or {}).get("routing")))),
                 "Press the receptacles into the plated holes and solder them; insert the pogo pins (crown tips on through-hole leads, spear tips on flat pads - see the probes CSV).",
                 ("Build a nest and a clamp for every board (section below): standoffs, springs, the printed nest, the "
                  "clamp on its adapter and the press plate. Mount the fixture on M3 standoffs."
                  if ((fixture or {}).get("nest") or {}).get("enabled") else
                  "Fit the guide pins in the GUIDE holes; they locate the board through its mounting holes. Mount the fixture on M3 standoffs."),
                 "Solder the divider resistors, header and power terminal. Wire the header to the measurement hardware as shown below."
                 if not fconns else
                 "Solder the divider resistors and the %s connector. The fixture is made for <b>%s</b>%s: it plugs straight in, "
                 "the pin map is below." % (_e(", ".join(sorted({c["name"] for c in fconns}))), _e(fac.get("name", fac.get("id", ""))),
                                           " (fixed %g x %g mm plate, its mounting holes and connector position)" % tuple(fac["plate_mm"])
                                           if fac.get("plate_mm") else "")]
        if plates:
            steps.append("Two plates (clamshell): the board lies on the bottom plate's guide pins; the top plate is mirrored, "
                         "comes down from above on the same guide pins and presses its probes onto the top side. Wire both "
                         "headers to the measurement hardware (channel numbers are shared).")
        if nsite > 1:
            b_ = plan.get("batch") or {}
            if (fixture or {}).get("central_header"):
                steps.append("The fixture takes a batch of <b>%d boards</b> (%d x %d sites, numbered row by row from the top "
                             "left, S1..S%d on the silkscreen) with <b>one connector</b> in the middle: every board's dividers sit "
                             "on its edge facing it, so the traces run about as far for every board. Each board has its own "
                             "supply pin (<code>S1 VIN</code>, ...) for its own power switch, its channels and its programming "
                             "lines; the pin map is below and <code>runner/hardware.json</code> has one power switch per site." % (
                                 nsite, b_.get("cols", nsite), b_.get("rows", 1), nsite))
            else:
                steps.append("The fixture takes a batch of <b>%d boards</b> (%d x %d sites, numbered row by row from the top left, "
                             "S1..S%d on the silkscreen). Every site has its own header and power terminal; wire them as "
                             "<code>runner/hardware.json</code> describes (one power switch per site)." % (
                                 nsite, b_.get("cols", nsite), b_.get("rows", 1), nsite))
        h.append("".join("<div class='step'><div class='num'>%d</div><div>%s</div></div>" % (i + 1, t) for i, t in enumerate(steps)))
        if fconns:
            h.append("<h3>%s pin map</h3>%s" % (_e(fac.get("name", fac.get("id", ""))), facility_pin_table(plan)))
        elif (fixture or {}).get("central_header"):
            h.append("<h3>Batch connector pin map</h3>%s" % central_pin_table(plan, fixture))
        else:
            h.append("<h3>Wiring</h3><div class='svgbox'>%s</div>" % wiring_svg(plan))
        h.append(_program_section(plan, fixture))
        h.append(nest_section(fixture, paths, nsite))
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
    nest_on = bool(((fixture or {}).get("nest") or {}).get("enabled"))
    if mode == "generate" and nest_on:
        slide = (fixture["nest"].get("style") == "slide")
        h.append("<div class='grid2'><div>%s<div class='cap'>%s</div></div><div>%s<div class='cap'>"
                 "...and close its clamp: the board and nest go down, every pogo pin touches its test point</div></div></div>" %
                 (_img(im.get("fixture_dut_loaded") or im.get("fixture_dut_exploded"), "Board in its nest"),
                  "Slide the board in along the rails to the stop: it rides above the pogo tips..." if slide else
                  "Drop the board into its nest: it rests above the pogo tips...",
                  _img(im.get("fixture_dut_seated"), "Board clamped on the fixture")))
    elif mode == "generate":
        h.append("<div class='grid2'><div>%s<div class='cap'>Lower the board onto the guide pins: a pogo pin stands under "
                 "every test point...</div></div><div>%s<div class='cap'>"
                 "...and press it down: every pogo pin touches its test point</div></div></div>" %
                 (_img(im.get("fixture_dut_exploded"), "Board above the fixture"), _img(im.get("fixture_dut_seated"), "Board on the fixture")))
    sup = plan["supply"]
    steps = ["Power the board from a bench supply: %s at %s V, current limit %s A." % (
                 _e(sup.get("net") or "the supply input"), _e(str(sup.get("voltage") if sup.get("voltage") is not None else "?")),
                 _e(str(sup.get("current_limit_a") or 0.5))),
             "Measure every step below at the marked test points (section 1) and compare with the limits.",
             "Record pass/fail per board; any value outside its limits is a fail."] if mode == "none" else [
             ("Copy the <code>runner</code> folder to the Raspberry Pi and describe your wiring in <code>hardware.json</code>."
              if not (plan.get("facility") or {}).get("driver") else
              "Copy the <code>runner</code> folder to the computer that drives <b>%s</b> and set how to reach it in "
              "<code>hardware.json</code>; its driver (<code>facility_driver.py</code>) measures, switches the supply and "
              "runs the bus checks." % _e((plan.get("facility") or {}).get("name", ""))),
             "Once, with known-good boards: <code>python3 pcbtest_runner.py learn --units 3</code> to fill in signal limits.",
             ("For every board: slide it into its nest up to the stop, close the clamp, run <code>python3 pcbtest_runner.py "
              "test --loop</code> and enter the serial number." if nest_on else
              "For every board: place it, press down, run <code>python3 pcbtest_runner.py test --loop</code> and enter the serial number."),
             "Green PASS: next board. Red FAIL: the failed checks are listed and stored in <code>results/results.csv</code>."]
    nsites = int((plan.get("batch") or {}).get("count") or 1)
    phased = any(st.get("phase") == "program" for st in plan.get("sequence") or [])
    if mode == "generate" and nsites > 1:
        load = "slide a board into each of the %d nests and close its clamp" if nest_on else "put a board in each of the %d sites, press down"
        steps[2] = (("For every batch: " + load + ", run <code>python3 pcbtest_runner.py test "
                     "--loop</code>. The runner tests site 1 to %d in turn, asks the serial number of each board and gives a "
                     "verdict per board (<code>--site 2</code> retests one board).") % (nsites, nsites)) if not phased else ((
                    "For every batch: " + load + ", run <code>python3 pcbtest_runner.py test "
                    "--loop</code> and enter the serial numbers. The runner checks every board unpowered and powers it up, "
                    "then programs all %d at the same time and, while the programmers run, measures what needs no "
                    "firmware; the checks that need the firmware follow. A verdict per board (<code>--site 2</code> retests "
                    "one board, <code>--sequential</code> tests one board after the other).") % (nsites, nsites))
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
    h.append("</table>")
    if (plan.get("firmware") or {}).get("programmed"):
        fwn = plan["firmware"]["firmware_nets"]
        h.append("<p class='small'>What needs the firmware: the nets of <b>%s</b>, the programmed part%s (%d nets, e.g. %s). "
                 "Their checks run after programming; everything else runs while the boards are programmed. A check can "
                 "force it with <code>needs_firmware</code> in the plan.</p>" % (
                     _e(", ".join(plan["firmware"]["programmed"])), "s" if len(plan["firmware"]["programmed"]) > 1 else "",
                     len(fwn), _e(", ".join("%s (%s)" % (C.base_net(x["net"]), x["why"]) for x in fwn[:3]))))
    if plan.get("timing"):
        h.append(timing_section(plan["timing"]))
    h.append("</section>")

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
        h.append(_fit_box(jig, paths))
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
