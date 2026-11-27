"""Hand-soldered parts that get hot all over while they are soldered, so the holding jig gives them room for a
heat-resistant insert (jig.heat_insert_mm).

A printed jig softens at 60 to 80 degrees C (PLA, PETG). Most parts stay cool where they touch it: the soldering heat
stays at the pin and the plastic body barely conducts it. Two things together make a part hot where it lies in its
pocket:

  a heavy joint   a pin on a ground or supply net that takes a lot of heat to solder: joined solidly (no thermal
                  relief) to a copper zone, or a big pin (drill >= 1.4 mm, or a pad of 7 mm2 or more), or a shield /
                  mounting pin of the part's metal shell
  a metal body    the heat reaches the surface that touches the jig: a metal shell or can (USB, RJ45, D-sub, coaxial,
                  barrel and audio jacks), a metal tab (TO-220, TO-247, ...), a crystal can, a heatsink

A part with both is hot; so is a part with a very heavy joint alone (a big pin joined solidly to a plane). jig.hot_parts
overrides the detection ("auto", "none", or a list of references and wildcards), jig.room gives any part room of
your own.
"""
from __future__ import annotations

import fnmatch
import math
import re
from typing import Any, Dict, List

from . import classify as C

SHELL_PAD = re.compile(r"^(SH\d*|SHIELD\d*|S\d*|MP\d*|MH\d*|0|EP\d*|TAB|G)$", re.I)
METAL_LIBS = ("connector_usb", "connector_rj", "connector_dsub", "connector_coaxial", "connector_barreljack",
              "connector_audio", "connector_amass", "heatsink", "crystal")
METAL_WORDS = ("usb", "rj45", "rj-45", "rj11", "8p8c", "dsub", "d-sub", "db9", "db15", "db25", "de9", "barreljack",
               "barrel_jack", "dc_jack", "dcjack", "powerjack", "power_jack", "bnc", "sma_", "_sma", "u.fl", "mcx",
               "f-type", "to-220", "to-247", "to-3p", "to-218", "to-264", "to-3_", "heatsink", "hc-49", "hc49",
               "shield", "xt30", "xt60", "xt90", "banana", "binding_post", "jack_3.5", "audiojack", "faston")


def _metal(fp) -> str:
    lib, _, name = fp.lib_id.lower().rpartition(":")
    if any(lib.startswith(m) for m in METAL_LIBS):
        return "metal shell or can (%s)" % lib
    text = " ".join((name, fp.value.lower()))
    hit = next((w for w in METAL_WORDS if w in text), "")
    return ("metal body (%s)" % hit.strip("_-")) if hit else ""


def _zone_mode(design, pad) -> str:
    """How a pad joins a copper zone of its own net: "solid", "thermal", "none" or "" (no zone there)."""
    b = design.board
    layers = list(b.copper_layers) if pad.is_tht else [l for l in ("F.Cu", "B.Cu") if l in pad.layers]
    hit = None
    for z in b.zones:
        if z.keepout or z.net != pad.net or not any(z.on_layer(l) for l in layers):
            continue
        if z.contains(pad.x, pad.y):
            hit = z
            break
    if hit is None:
        return ""
    if pad.zone_connect is not None and pad.zone_connect >= 0:
        return {0: "none", 1: "thermal", 2: "solid", 3: "thermal"}.get(pad.zone_connect, "thermal")
    mode = hit.connect
    if mode == "tht_thermal":
        mode = "thermal" if pad.is_tht else "solid"
    fp = b.by_ref.get(pad.ref)
    fz = (fp.props.get("zone_connect") if fp is not None else None) or ""
    return {"0": "none", "1": "thermal", "2": "solid", "3": "thermal"}.get(fz, mode)


def assess(design, fp) -> List[str]:
    """Why ``fp`` gets hot where it touches the jig (empty: it doesn't)."""
    heavy, very = [], False
    for p in fp.pads:
        if not p.is_tht or not p.net or design.net_kind(p.net) not in ("ground", "power"):
            continue
        area = p.w * p.h if p.shape in ("rect", "roundrect") else math.pi * p.w * p.h / 4.0
        zone = _zone_mode(design, p)
        why = []
        if zone == "solid":
            why.append("joined solidly to the %s zone" % p.net)
        if p.drill_w >= 1.4 or area >= 7.0:
            why.append("a big pin (%.1f mm drill)" % p.drill_w)
        if SHELL_PAD.match(p.number or "") or not p.number:
            why.append("a shell pin")
        line = "pin %s on %s: %s" % (p.number or "-", p.net, ", ".join(why))
        if why and line not in heavy:
            heavy.append(line)
            very = very or (zone == "solid" and (p.drill_w >= 1.8 or area >= 12.0))
    if not heavy:
        return []
    metal = _metal(fp)
    if metal:
        return [metal] + heavy[:3]
    if very:
        return ["a very heavy joint"] + heavy[:3]
    return []


def hot_parts(design, parts) -> Dict[str, List[str]]:
    """{ref: reasons} of the parts in ``parts`` (footprints) that get hot where they touch the holding jig,
    following jig.hot_parts ("auto" | "none" | list of references or wildcards)."""
    jc = design.cfg["jig"]
    mode = jc.get("hot_parts", "auto")
    out: Dict[str, List[str]] = {}
    for fp in parts:
        if isinstance(mode, list):
            if any(fnmatch.fnmatchcase(fp.ref.lower(), str(p).lower()) for p in mode):
                out[fp.ref] = ["set in jig.hot_parts"] + (assess(design, getattr(fp, "_orig", fp)) or [])
        elif mode != "none":
            why = assess(design, getattr(fp, "_orig", fp))
            if why:
                out[fp.ref] = why
    return out


def room_for(design, parts) -> Dict[str, Dict[str, Any]]:
    """{ref: {"mm", "why"}}: extra room in the holding jig for the parts in ``parts``: jig.room (references or
    wildcards -> mm, wins) and jig.heat_insert_mm for the hot parts (:func:`hot_parts`)."""
    jc = design.cfg["jig"]
    insert = float(jc.get("heat_insert_mm", 1.0) or 0.0)
    manual = {str(k): float(v) for k, v in (jc.get("room") or {}).items()}
    hot = hot_parts(design, parts) if insert > 0 else {}
    out: Dict[str, Dict[str, Any]] = {}
    for fp in sorted(parts, key=lambda f: C.natural_key(f.ref)):
        pick = next((v for k, v in manual.items() if fnmatch.fnmatchcase(fp.ref.lower(), k.lower())), None)
        if pick is not None:
            if pick > 0:
                out[fp.ref] = {"mm": round(pick, 3), "why": "jig.room", "hot": hot.get(fp.ref, [])}
        elif fp.ref in hot:
            out[fp.ref] = {"mm": round(insert, 3), "why": "heat insert", "hot": hot[fp.ref]}
    return out
