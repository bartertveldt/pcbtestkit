"""The look every PCB TestKit page shares: the board guide and report, the station page and the test environment's
catalog. One stylesheet (light and dark, print-friendly, keyboard-friendly), a hero header, video blocks, cards and
tables, and safe defaults:

* every page carries a strict Content-Security-Policy: nothing is loaded from the network, no frames, no forms;
  the only script is :data:`TABLE_JS` (sorting and filtering tables), allowed by its SHA-256 hash;
* every text that comes from a board, a schematic or a setting goes through :func:`esc`;
* links to other sites open without a referrer.

A page is ``head(...) + hero(...) + nav(...) + "<main>" + ... + footer(...)``. Pages that belong to a station or a
test environment get breadcrumbs back to it afterwards (:func:`set_context`), so a board's guide links to its
station and its catalog without the board's own run knowing about them.
"""
from __future__ import annotations

import base64
import hashlib
import html
import os
import re
from typing import Any, Iterable, List, Optional, Sequence, Tuple

CTX_OPEN, CTX_CLOSE = "<!--pcbtk:context-->", "<!--/pcbtk:context-->"
PAGE_REV = 1          # bumped when the pages change: a station or environment build then makes them again


def esc(s: Any) -> str:
    """Text for HTML (element content and quoted attributes)."""
    return html.escape("" if s is None else str(s), quote=True)


# --------------------------------------------------------------------------- #
# The one script: sortable and filterable tables and card lists (pages work without it)
# --------------------------------------------------------------------------- #
TABLE_JS = r"""(function(){
var $=function(s,r){return [].slice.call((r||document).querySelectorAll(s));};
$('[data-filter]').forEach(function(inp){
  var box=document.getElementById(inp.getAttribute('data-filter'));if(!box)return;
  var items=$('[data-row]',box),sel=document.querySelector('[data-status-for="'+box.id+'"]'),
      out=document.querySelector('[data-count-for="'+box.id+'"]');
  function apply(){var q=inp.value.trim().toLowerCase(),s=sel?sel.value:'',n=0;
    items.forEach(function(r){var ok=(!q||r.textContent.toLowerCase().indexOf(q)>=0)&&(!s||r.getAttribute('data-row')===s);
      r.hidden=!ok;if(ok)n++;});
    if(out)out.textContent=n;}
  inp.addEventListener('input',apply);if(sel)sel.addEventListener('change',apply);
  inp.closest('.tools').hidden=false;});
$('table.sortable th').forEach(function(th){
  th.tabIndex=0;th.setAttribute('aria-sort','none');
  function go(){var t=th.closest('table'),b=t.tBodies[0],i=th.cellIndex,num=th.hasAttribute('data-num'),
      d=th.getAttribute('aria-sort')==='ascending'?-1:1;
    $('th',t).forEach(function(x){x.setAttribute('aria-sort','none');});
    th.setAttribute('aria-sort',d>0?'ascending':'descending');
    var v=function(r){var c=r.cells[i];return c?(c.getAttribute('data-v')||c.textContent).trim():'';};
    $('tr',b).sort(function(a,c){var x=v(a),y=v(c);
      if(num){x=parseFloat(x);y=parseFloat(y);x=isNaN(x)?-1e9:x;y=isNaN(y)?-1e9:y;return (x-y)*d;}
      return x.localeCompare(y,undefined,{numeric:true,sensitivity:'base'})*d;}).forEach(function(r){b.appendChild(r);});}
  th.addEventListener('click',go);
  th.addEventListener('keydown',function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();go();}});});
})();"""


def _sha(js: str) -> str:
    return "'sha256-%s'" % base64.b64encode(hashlib.sha256(js.encode("utf-8")).digest()).decode()


def csp(script: bool = False) -> str:
    """The page's Content-Security-Policy: images and videos from the page's own folder (or embedded), styles inline,
    no network, no frames, no forms; :data:`TABLE_JS` only when ``script``."""
    return ("default-src 'none'; img-src 'self' data: file:; media-src 'self' file:; style-src 'unsafe-inline'; "
            "script-src %s; base-uri 'none'; form-action 'none'; object-src 'none'" % (_sha(TABLE_JS) if script else "'none'"))


# --------------------------------------------------------------------------- #
# The stylesheet
# --------------------------------------------------------------------------- #
_TRACES = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160' fill='none' "
           "stroke='%23888' stroke-width='1.2' opacity='.5'%3E%3Cpath d='M0 40h40l20 20h50l20-20h30M20 160v-40l20-20h40'/%3E"
           "%3Cpath d='M100 0v30l20 20v40l-20 20v50M160 120h-30l-20 20'/%3E%3Ccircle cx='40' cy='40' r='3'/%3E%3Ccircle "
           "cx='110' cy='60' r='3'/%3E%3Ccircle cx='80' cy='100' r='3'/%3E%3Ccircle cx='120' cy='90' r='3'/%3E%3C/svg%3E")

CSS = """
:root{color-scheme:light dark;
--bg:#f4f5f7;--surface:#fff;--surface-2:#f8f9fb;--fg:#15171b;--muted:#5d6470;--line:#e2e5ea;--line-2:#d3d8df;
--accent:#14845a;--accent-2:#0f6e4a;--accent-soft:rgba(20,132,90,.10);--copper:#c0772b;--copper-soft:rgba(192,119,43,.12);
--ok:#16834f;--ok-soft:rgba(22,131,79,.11);--warn:#a86400;--warn-soft:rgba(208,128,0,.13);--bad:#c62828;--bad-soft:rgba(198,40,40,.10);
--info:#2f6fb2;--info-soft:rgba(47,111,178,.11);--shadow:0 1px 2px rgba(16,24,40,.05),0 8px 24px rgba(16,24,40,.06);
--radius:14px;--card:var(--surface);
--board-bg:#e6f0e8;--edge:#222;--crt-top:#3a6ea5;--crt-bottom:#9a4ea5;--pad-top:#c08a2d;--pad-bottom:#8a5ab5;--pad-tht:#777;
--txt:#333;--probe:#0a8f5a;--gnd:#333;--sup:#d13438}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
--bg:#0d0f12;--surface:#161a1f;--surface-2:#1b2027;--fg:#e8eaee;--muted:#9aa3ae;--line:#272d35;--line-2:#333b45;
--accent:#3ccf8e;--accent-2:#5fe0a6;--accent-soft:rgba(60,207,142,.12);--copper:#e59a4f;--copper-soft:rgba(229,154,79,.13);
--ok:#46d08f;--ok-soft:rgba(70,208,143,.12);--warn:#f0b13c;--warn-soft:rgba(240,177,60,.13);--bad:#ff6b6b;--bad-soft:rgba(255,107,107,.12);
--info:#6aa8ef;--info-soft:rgba(106,168,239,.13);--shadow:0 1px 2px rgba(0,0,0,.4),0 10px 30px rgba(0,0,0,.35);
--board-bg:#1a2820;--edge:#ddd;--txt:#ccc;--gnd:#ddd}}
*,*::before,*::after{box-sizing:border-box}
html{scroll-behavior:smooth;-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,
"Helvetica Neue",Arial,sans-serif;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
a{color:var(--accent);text-decoration-thickness:1px;text-underline-offset:2px}a:hover{color:var(--accent-2)}
:focus-visible{outline:3px solid var(--accent);outline-offset:2px;border-radius:6px}
.skip{position:absolute;left:-999px;top:8px;background:var(--surface);padding:8px 12px;border-radius:8px;z-index:9}
.skip:focus{left:12px}
.wrapx{max-width:1200px;margin:0 auto;padding:0 20px}
main{max-width:1200px;margin:0 auto;padding:8px 20px 72px}
h1,h2,h3{line-height:1.2;letter-spacing:-.015em}
h2{font-size:22px;margin:44px 0 14px;padding-top:6px}h3{font-size:17px;margin:28px 0 10px}
section{scroll-margin-top:72px}
p{margin:10px 0}code{font:12.5px/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--surface-2);
border:1px solid var(--line);border-radius:6px;padding:1px 5px}
.muted{color:var(--muted)}.small{font-size:13px}.nowrap{white-space:nowrap}
/* hero */
.hero{position:relative;overflow:hidden;border-bottom:1px solid var(--line);
background:radial-gradient(900px 360px at 0% -20%,var(--accent-soft),transparent 70%),
radial-gradient(800px 340px at 100% 0%,var(--copper-soft),transparent 70%),var(--surface)}
.hero::before{content:"";position:absolute;inset:0;background:url("%TRACES%") repeat;opacity:.07;pointer-events:none}
.hero .wrapx{position:relative;padding-top:26px;padding-bottom:28px}
.crumbs{display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:13px;color:var(--muted);margin-bottom:14px}
.crumbs a{color:var(--muted);text-decoration:none;border:1px solid var(--line);background:var(--surface);border-radius:99px;padding:2px 10px}
.crumbs a:hover{color:var(--fg);border-color:var(--line-2)}.crumbs .sep{opacity:.6}
.kicker{font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--accent)}
.hero h1{font-size:clamp(28px,4.2vw,44px);font-weight:760;margin:6px 0 8px;letter-spacing:-.025em}
.lead{font-size:16.5px;color:var(--muted);max-width:820px;margin:0}
.pills{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0 0}
.pill{display:inline-flex;align-items:center;gap:6px;border-radius:99px;padding:3px 11px;font-size:13px;font-weight:600;
background:var(--surface-2);border:1px solid var(--line);color:var(--fg)}
.pill::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--muted)}
.pill.ok{background:var(--ok-soft);border-color:transparent;color:var(--ok)}.pill.ok::before{background:var(--ok)}
.pill.warn{background:var(--warn-soft);border-color:transparent;color:var(--warn)}.pill.warn::before{background:var(--warn)}
.pill.bad{background:var(--bad-soft);border-color:transparent;color:var(--bad)}.pill.bad::before{background:var(--bad)}
.pill.info{background:var(--info-soft);border-color:transparent;color:var(--info)}.pill.info::before{background:var(--info)}
.actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:18px}
.btn{display:inline-flex;align-items:center;gap:8px;border-radius:10px;padding:8px 14px;font-weight:600;font-size:14px;
text-decoration:none;border:1px solid var(--line-2);background:var(--surface);color:var(--fg)}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}.btn.primary:hover{background:var(--accent-2);color:#fff}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:22px 0 0}
.stat{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:14px 16px;box-shadow:var(--shadow)}
.stat b{display:block;font-size:26px;font-weight:760;letter-spacing:-.02em;line-height:1.15;font-variant-numeric:tabular-nums}
.stat span{font-size:13px;color:var(--muted)}.stat.ok b{color:var(--ok)}.stat.warn b{color:var(--warn)}.stat.bad b{color:var(--bad)}
/* the page's own short menu: a row of chips under the hero (no side menu) */
nav.toc{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--bg) 82%,transparent);
-webkit-backdrop-filter:saturate(1.4) blur(10px);backdrop-filter:saturate(1.4) blur(10px);border-bottom:1px solid var(--line)}
@supports not (background:color-mix(in srgb,red 50%,blue)){nav.toc{background:var(--bg)}}
nav.toc .wrapx{display:flex;gap:6px;overflow-x:auto;padding-top:9px;padding-bottom:9px;scrollbar-width:none}
nav.toc a{flex:0 0 auto;font-size:13px;font-weight:600;text-decoration:none;color:var(--muted);padding:5px 12px;border-radius:99px;
border:1px solid transparent}
nav.toc a:hover{color:var(--fg);background:var(--surface);border-color:var(--line)}
/* media */
.media{margin:22px 0;background:#000;border-radius:var(--radius);overflow:hidden;box-shadow:var(--shadow);border:1px solid var(--line)}
.media video{display:block;width:100%;height:auto;aspect-ratio:16/9;background:#000;border-radius:0}
.media figcaption{background:var(--surface);color:var(--muted);font-size:13px;padding:10px 14px;display:flex;gap:10px;
justify-content:space-between;flex-wrap:wrap}
.media figcaption b{color:var(--fg)}
video{max-width:100%;border-radius:10px}img{max-width:100%}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:18px}
.gallery .media{margin:0}
/* cards and grids */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:16px 0}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:14px 16px;box-shadow:var(--shadow)}
.cards .card b{display:block;font-size:21px;font-weight:720;letter-spacing:-.01em;margin:2px 0}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:18px;margin:14px 0}
.svgbox{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:10px;box-shadow:var(--shadow)}
.shot{width:100%;border-radius:var(--radius);border:1px solid var(--line);background:#cfd3e0;box-shadow:var(--shadow);display:block}
.cap{font-size:13px;color:var(--muted);margin-top:8px}
img.jig{max-width:100%;image-rendering:pixelated;border-radius:10px;border:1px solid var(--line)}
/* board and station cards */
.boards{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px;margin:16px 0}
.bcard{display:flex;flex-direction:column;background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);
overflow:hidden;box-shadow:var(--shadow);transition:transform .15s ease,border-color .15s ease}
.bcard:hover{transform:translateY(-2px);border-color:var(--line-2)}
.bcard .thumb{display:flex;align-items:center;justify-content:center;aspect-ratio:16/10;background:
radial-gradient(400px 200px at 30% 20%,var(--accent-soft),transparent 70%),var(--surface-2);border-bottom:1px solid var(--line)}
.bcard .thumb img{width:100%;height:100%;object-fit:cover}
.bcard .thumb svg{width:62%;height:70%}
.bcard .body{padding:14px 16px 16px;display:flex;flex-direction:column;gap:8px;flex:1}
.bcard h3{margin:0;font-size:17px}.bcard h3 a{color:var(--fg);text-decoration:none}.bcard h3 a:hover{color:var(--accent)}
.bcard .why{font-size:13.5px;color:var(--muted);margin:0}
.bcard .links{display:flex;flex-wrap:wrap;gap:6px;margin-top:auto;padding-top:6px}
.bcard .links a{font-size:13px;font-weight:600;text-decoration:none;border:1px solid var(--line);border-radius:8px;padding:3px 9px}
.bcard .links a:hover{border-color:var(--accent)}
.bcard details video{margin-top:8px;width:100%}
.bcard.off{opacity:.92}.bcard.off .thumb{background:var(--surface-2)}
.facts{display:flex;flex-wrap:wrap;gap:6px}
.fact{font-size:12px;font-weight:600;border-radius:7px;padding:2px 8px;background:var(--surface-2);border:1px solid var(--line);color:var(--muted)}
.fact.big{font-size:13px;color:var(--accent);background:var(--accent-soft);border-color:transparent}
.station{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:22px;align-items:start;margin:18px 0 8px}
@media (max-width:860px){.station{grid-template-columns:1fr}}
.station .media{margin:0 0 14px}
/* tables */
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:var(--radius);background:var(--surface);box-shadow:var(--shadow);margin:12px 0}
table{width:100%;border-collapse:collapse;background:var(--surface);font-size:14px}
.wrap>table{border:0}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);font-weight:700;background:var(--surface-2);
position:sticky;top:0}
tbody tr:hover td{background:var(--surface-2)}
tr:last-child td{border-bottom:0}
table.sortable th{cursor:pointer;user-select:none}table.sortable th::after{content:" \\2195";opacity:.35}
table.sortable th[aria-sort=ascending]::after{content:" \\2191";opacity:.9}table.sortable th[aria-sort=descending]::after{content:" \\2193";opacity:.9}
td.r,th.r{text-align:right;font-variant-numeric:tabular-nums}
.ok{color:var(--ok);font-weight:650}.no{color:var(--bad);font-weight:650}
.sev{display:inline-block;padding:2px 9px;border-radius:99px;color:#fff;font-size:12px;font-weight:700;letter-spacing:.02em}
.tools{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:12px 0}
.tools input,.tools select{font:inherit;font-size:14px;color:var(--fg);background:var(--surface);border:1px solid var(--line-2);
border-radius:10px;padding:8px 12px}
.tools input{flex:1 1 260px;max-width:420px}
/* steps */
.step{display:flex;gap:14px;margin:12px 0;align-items:flex-start}
.num{flex:0 0 32px;height:32px;border-radius:50%;background:linear-gradient(135deg,var(--accent),var(--accent-2));color:#fff;
display:flex;align-items:center;justify-content:center;font-weight:760;font-size:14px;box-shadow:0 2px 8px var(--accent-soft)}
.step>div:last-child{padding-top:4px}
ol,ul{padding-left:22px}li{margin:4px 0}
details{border-radius:10px}details summary{cursor:pointer;font-weight:600}
.legend span{display:inline-block;width:12px;height:12px;border-radius:3px;margin:0 4px -1px 12px}
.chips span{display:inline-block;border-radius:99px;padding:1px 9px;margin:2px;color:#fff;font-size:12px;font-weight:600}
[id]{scroll-margin-top:64px}
.callout{border-radius:var(--radius);padding:12px 16px;margin:14px 0;border:1px solid var(--line);background:var(--surface)}
.callout.ok{background:var(--ok-soft);border-color:transparent;color:var(--fg)}.callout.warn{background:var(--warn-soft);border-color:transparent}
.callout.bad{background:var(--bad-soft);border-color:transparent}.callout.info{background:var(--info-soft);border-color:transparent}
.callout .ok,.callout .no{font-weight:700}
/* footer */
footer.foot{border-top:1px solid var(--line);color:var(--muted);font-size:13px;background:var(--surface)}
footer.foot .wrapx{padding-top:18px;padding-bottom:26px;display:flex;flex-wrap:wrap;gap:8px 24px;justify-content:space-between}
@media (max-width:560px){.hero .wrapx{padding-top:18px;padding-bottom:20px}.stat{padding:12px}.stat b{font-size:21px}
h2{font-size:20px;margin-top:34px}main,.wrapx{padding-left:16px;padding-right:16px}}
@media (prefers-reduced-motion:reduce){html{scroll-behavior:auto}*{transition:none!important;animation:none!important}}
@media print{nav.toc,.skip,.tools,video,.media video{display:none!important}.hero::before{display:none}
body{background:#fff;color:#000}.card,.bcard,.stat,.wrap,.svgbox,.media{box-shadow:none}
.bcard,.card,.step,tr,figure{break-inside:avoid}a{color:#000}th{position:static}}
""".replace("%TRACES%", _TRACES)

_FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' "
            "height='32' rx='7' fill='%2314845a'/%3E%3Cpath d='M8 22V10h7a4 4 0 0 1 0 8H8m12 4h4M20 10h4' fill='none' "
            "stroke='%23fff' stroke-width='2.6' stroke-linecap='round'/%3E%3C/svg%3E")


# --------------------------------------------------------------------------- #
# Building blocks
# --------------------------------------------------------------------------- #
def head(title: str, description: str = "", css: str = "", script: bool = False) -> str:
    """Everything up to and including <body> and the skip link."""
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<meta http-equiv='Content-Security-Policy' content=\"%s\">"
            "<meta name='referrer' content='no-referrer'><meta name='color-scheme' content='light dark'>"
            "<meta name='generator' content='PCB TestKit'>%s"
            "<title>%s</title><link rel='icon' href=\"%s\"><style>%s%s</style></head><body>"
            "<a class='skip' href='#content'>Skip to the content</a>" % (
                csp(script), ("<meta name='description' content='%s'>" % esc(description)) if description else "",
                esc(title), _FAVICON, CSS, css))


def pill(text: str, tone: str = "") -> str:
    return "<span class='pill %s'>%s</span>" % (esc(tone), esc(text))


def stat(value: Any, label: str, tone: str = "") -> str:
    return "<div class='stat %s'><b>%s</b><span>%s</span></div>" % (esc(tone), esc(value), esc(label))


def hero(kicker: str, title: str, lead: str = "", pills: Sequence[Tuple[str, str]] = (),
         stats: Sequence[Tuple[Any, str, str]] = (), actions: Sequence[Tuple[str, str, bool]] = (),
         lead_html: bool = False) -> str:
    """The page's header: breadcrumbs (filled in by :func:`set_context`), kicker, title, a lead sentence, status pills,
    stat tiles and buttons ((href, label, primary))."""
    out = ["<header class='hero'><div class='wrapx'>", CTX_OPEN, CTX_CLOSE,
           "<div class='kicker'>%s</div><h1>%s</h1>" % (esc(kicker), esc(title))]
    if lead:
        out.append("<p class='lead'>%s</p>" % (lead if lead_html else esc(lead)))
    if pills:
        out.append("<div class='pills'>%s</div>" % "".join(pill(t, tone) for t, tone in pills if t))
    if actions:
        out.append("<div class='actions'>%s</div>" % "".join(
            "<a class='btn%s' href='%s'>%s</a>" % (" primary" if primary else "", esc(href), esc(label))
            for href, label, primary in actions if href))
    if stats:
        out.append("<div class='stats'>%s</div>" % "".join(stat(v, l, t) for v, l, t in stats))
    out.append("</div></header>")
    return "".join(out)


def nav(items: Iterable[Tuple[str, str]]) -> str:
    """A row of chips to the page's sections (stays at the top while scrolling)."""
    links = "".join("<a href='#%s'>%s</a>" % (esc(a), esc(t)) for a, t in items if a)
    return ("<nav class='toc' aria-label='On this page'><div class='wrapx'>%s</div></nav>" % links) if links else ""


def video(src: str, title: str = "", note: str = "", poster: str = "", preload: str = "metadata") -> str:
    """A video in its frame with a caption and a download link (it plays from the file next to the page)."""
    return ("<figure class='media'><video controls playsinline preload='%s' src='%s'%s%s>"
            "<a href='%s'>Download the video</a></video><figcaption><span><b>%s</b>%s</span>"
            "<a href='%s' download>Download</a></figcaption></figure>" % (
                esc(preload), esc(src), (" poster='%s'" % esc(poster)) if poster else "",
                (" aria-label='%s'" % esc(title)) if title else "", esc(src), esc(title),
                (" &middot; %s" % esc(note)) if note else "", esc(src)))


def board_shape(size_mm: Optional[Sequence[float]], label: str = "") -> str:
    """A board's outline to scale (for cards without a render): a rounded rectangle and its size."""
    try:
        w, h = float(size_mm[0]), float(size_mm[1])
    except (TypeError, ValueError, IndexError):
        w, h = 50.0, 35.0
    s = 100.0 / max(w, h, 1.0)
    bw, bh = max(8.0, w * s), max(8.0, h * s)
    x, y = (120 - bw) / 2.0, (110 - bh) / 2.0 - 4
    return ("<svg viewBox='0 0 120 120' role='img' aria-label='%s'><rect x='%.1f' y='%.1f' width='%.1f' height='%.1f' rx='3' "
            "fill='var(--accent-soft)' stroke='var(--accent)' stroke-width='1.2'/><circle cx='%.1f' cy='%.1f' r='1.6' "
            "fill='var(--accent)'/><circle cx='%.1f' cy='%.1f' r='1.6' fill='var(--accent)'/><text x='60' y='116' "
            "text-anchor='middle' font-size='9' fill='var(--muted)'>%s</text></svg>" % (
                esc(label or "board outline"), x, y, bw, bh, x + 4, y + 4, x + bw - 4, y + bh - 4,
                esc("%g x %g mm" % (round(w, 1), round(h, 1)))))


def filter_tools(target: str, placeholder: str, statuses: Sequence[Tuple[str, str]] = (), total: int = 0) -> str:
    """A search box (and a status filter) for a table or card list with ``id=target`` whose items carry data-row;
    hidden until the script runs."""
    sel = ""
    if statuses:
        sel = "<select data-status-for='%s' aria-label='Show'>%s</select>" % (
            esc(target), "".join("<option value='%s'>%s</option>" % (esc(v), esc(t)) for v, t in statuses))
    return ("<div class='tools' hidden><input type='search' data-filter='%s' placeholder='%s' aria-label='%s'>%s"
            "<span class='muted small'><b data-count-for='%s'>%d</b> shown</span></div>" % (
                esc(target), esc(placeholder), esc(placeholder), sel, esc(target), total))


def footer(generator: str, note: str = "", script: bool = False) -> str:
    """Closes the page: what made it, that it loads nothing from the internet, and the script if the page uses it."""
    return ("</main><footer class='foot'><div class='wrapx'><span>%s</span><span>Made on your computer from your KiCad "
            "files. This page loads nothing from the internet.%s</span></div></footer>%s</body></html>" % (
                esc(generator), (" " + esc(note)) if note else "", ("<script>%s</script>" % TABLE_JS) if script else ""))


def crumbs(items: Sequence[Tuple[Optional[str], str]]) -> str:
    """Breadcrumbs: (href or None, label) from the top down; the last one is this page."""
    parts = []
    for href, label in items:
        parts.append("<a href='%s'>%s</a>" % (esc(href), esc(label)) if href else "<span>%s</span>" % esc(label))
    return "<div class='crumbs'>%s</div>" % "<span class='sep'>/</span>".join(parts)


def set_context(path: str, block: str) -> bool:
    """Put ``block`` (breadcrumbs, a banner) into the page at ``path`` between its context markers; True if done."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return False
    i, j = text.find(CTX_OPEN), text.find(CTX_CLOSE)
    if i < 0 or j < i:
        return False
    new = text[:i + len(CTX_OPEN)] + block + text[j:]
    if new != text:
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(new)
        except OSError:
            return False
    return True


def rel(path: str, start: str) -> str:
    """``path`` relative to the folder ``start``, with / (for links in pages)."""
    return os.path.relpath(path, start).replace(os.sep, "/")


def first_existing(base: str, names: Sequence[str]) -> Optional[str]:
    """The first of ``names`` (relative to ``base``) that exists, or None."""
    for n in names:
        if n and os.path.exists(os.path.join(base, n)):
            return n
    return None


_TAG = re.compile(r"<[^>]+>")


def plain(text: str) -> str:
    """HTML to plain text (for a description meta tag)."""
    return html.unescape(_TAG.sub("", text or "")).strip()


def tone_of_score(score: Any) -> str:
    try:
        s = float(score)
    except (TypeError, ValueError):
        return ""
    return "ok" if s >= 80 else "warn" if s >= 50 else "bad"


def join(parts: List[str]) -> str:
    return "".join(p for p in parts if p)
