"""Generic "good unit" specification (JSON).

The spec is tool-neutral: it describes what a correct board is (geometry,
fabrication limits, BOM/placement, connectivity, electrical expectations and
acceptance criteria) so that any tester, MES or AI model can consume it.
"""
from __future__ import annotations

import datetime
import math
import os
from typing import Any, Dict, List, Optional

from . import __version__
from . import classify as C
from .project import Design

SPEC_SCHEMA = "pcbtestkit/spec@1"


def _r(v, nd=3):
    return None if v is None or (isinstance(v, float) and math.isinf(v)) else round(float(v), nd)


def manufacturing_stats(design: Design) -> Dict[str, Any]:
    b = design.board
    tw = [t.width for t in b.tracks]
    vd = [v.drill for v in b.vias]
    va = [v.annular for v in b.vias]
    tht = [p for p in b.all_pads() if p.is_tht and p.drill_w]
    tht_ring = [min((p.w - p.drill_w) / 2, (p.h - p.drill_h) / 2) for p in tht]
    holes = vd + [p.drill_w for p in b.all_pads() if p.drill_w]
    drills: Dict[str, int] = {}
    for h in holes:
        k = "%.2f" % h
        drills[k] = drills.get(k, 0) + 1
    edge = [b.edge_distance(v.x, v.y) - v.size / 2 for v in b.vias]
    edge += [b.edge_distance(p.x, p.y) - max(p.w, p.h) / 2 for p in b.all_pads() if p.has_copper]
    return {
        "min_track_width_mm": _r(min(tw)) if tw else None,
        "min_via_drill_mm": _r(min(vd)) if vd else None,
        "min_via_annular_mm": _r(min(va)) if va else None,
        "min_tht_annular_mm": _r(min(tht_ring)) if tht_ring else None,
        "min_hole_mm": _r(min(holes)) if holes else None,
        "max_via_aspect_ratio": _r(b.thickness / min(vd), 2) if vd else None,
        "min_pad_via_edge_clearance_mm": _r(min(edge)) if edge and b.edge_segs else None,
        "via_count": len(b.vias),
        "plated_holes": len([p for p in b.all_pads() if p.is_tht]) + len(b.vias),
        "non_plated_holes": len([p for p in b.all_pads() if p.is_npth]),
        "drill_histogram_mm": dict(sorted(drills.items(), key=lambda kv: float(kv[0]))),
        "track_count": len(b.tracks),
        "zone_count": len([z for z in b.zones if not z.keepout]),
    }


def component_entry(design: Design, fp) -> Dict[str, Any]:
    h, hsrc = design.height_of(fp)
    e = {"ref": fp.ref, "value": fp.value, "footprint": fp.lib_id, "side": fp.side, "x": _r(fp.x), "y": _r(fp.y),
         "rotation": _r(fp.angle, 2), "mount": fp.mount, "dnp": fp.dnp, "height_mm": _r(h, 2), "height_source": hsrc}
    mpn = C.mpn_of(fp)
    if mpn:
        e["mpn"] = mpn
    man = fp.get_field("Manufacturer", "MFR", "Mfr")
    if man:
        e["manufacturer"] = man
    rv, _ = C.rated_voltage(fp) if C.is_capacitor(fp) else (None, "")
    if rv:
        e["rated_voltage_v"] = rv
    if C.is_polarized(fp):
        e["polarized"] = True
    if fp.exclude_from_bom:
        e["exclude_from_bom"] = True
    e["pins"] = {p.number: p.net for p in fp.pads if p.number and p.net}
    return e


def _generated_at() -> str:
    """Generation time; SOURCE_DATE_EPOCH (reproducible-builds convention) pins it, making every output byte-identical."""
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch and epoch.strip().isdigit():
        return datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc).replace(tzinfo=None).isoformat()
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def _compact_viol(viol: Dict[str, Any]) -> Dict[str, Any]:
    return {m: {k: v[k] for k in ("label", "count", "worst", "limit", "limits", "severity", "note") if v.get(k) is not None}
            for m, v in (viol or {}).items()}


def _compact_eval(r: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: r.get(k) for k in ("system", "name", "status", "source", "verified", "category", "context", "measured")}
    out["required"] = {k: v for k, v in r["required"].items() if k != "drivers"}
    out["required"]["drivers"] = [{k: d[k] for k in ("dimension", "level", "metric", "measured", "limit")} for d in r["required"].get("drivers", [])]
    tg = r.get("target")
    if tg:
        out["target"] = {"class": tg["class"], "ok": tg["ok"], "services": tg.get("services"), "limits": tg.get("limits"),
                         "manual": tg.get("manual"), "violations": _compact_viol(tg.get("violations"))}
    if r.get("process"):
        out["process"] = {"ok": r["process"]["ok"], "violations": _compact_viol(r["process"].get("violations"))}
    return out


def _tech_section(design: Design) -> Dict[str, Any]:
    t = design.technology()
    return {"system": t["system"], "name": t["name"], "status": t["status"], "source": t["source"],
            "required": {k: t["required"].get(k) for k in ("class", "fits", "services", "levels", "beyond")},
            "ordered": t["target"], "layers": t["layers"], "thickness_mm": t["thickness_mm"],
            "base_copper_um": t["base_copper_um"], "finish": t["finish"], "short_count": t["short_count"],
            "microvias": t["microvias"], "primary": _compact_eval(t["primary"]),
            "checks": [_compact_eval(c) for c in t["checks"]], "overview": design.class_overview()}


def build_spec(design: Design, findings, plan: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from .analysis import summarize
    b = design.board
    bb = b.outline_bbox()
    comps = [fp for fp in design.components() if not C.is_fiducial(fp) or True]
    pads_by_net = b.pads_by_net()
    probes_by_net: Dict[str, List[str]] = {}
    if plan:
        for p in plan["probes"]:
            probes_by_net.setdefault(p["net"], []).append(p["id"])
    nets = []
    for n in b.net_names():
        if C.is_unconnected(n):
            continue
        spec = design.net_spec(n)
        entry = {"name": n, "kind": spec["kind"], "pad_count": len(pads_by_net.get(n, [])),
                 "pads": sorted([p.name for p in pads_by_net.get(n, [])], key=C.natural_key)}
        if spec.get("nominal_v") is not None or spec.get("min_v") is not None:
            entry["expected_v"] = {"nominal": spec.get("nominal_v"), "min": spec.get("min_v"), "max": spec.get("max_v"),
                                   "source": spec.get("voltage_source")}
        if spec.get("max_current_a"):
            entry["max_current_a"] = spec["max_current_a"]
        if n in probes_by_net:
            entry["probes"] = probes_by_net[n]
        nets.append(entry)

    criteria: List[Dict[str, Any]] = []
    criteria.append({"id": "DIM-OUTLINE", "type": "dimensional", "description": "Board outline %.2f x %.2f mm" % (bb.w, bb.h),
                     "limits": {"width_mm": [_r(bb.w - 0.2), _r(bb.w + 0.2)], "height_mm": [_r(bb.h - 0.2), _r(bb.h + 0.2)]}})
    criteria.append({"id": "DIM-THICKNESS", "type": "dimensional", "description": "Board thickness %.2f mm +/-10%%" % b.thickness,
                     "limits": {"thickness_mm": [_r(b.thickness * 0.9), _r(b.thickness * 1.1)]}})
    criteria.append({"id": "WORKMANSHIP", "type": "visual", "description": "Soldering per IPC-A-610 Class %s" %
                     design.cfg.get("ipc_class", 2)})
    if plan:
        for st in plan["sequence"]:
            for chk in st.get("checks", []) or []:
                c = {"id": chk["id"], "type": "electrical", "step": st["id"], "description": "%s on %s" % (st["name"], chk.get("net"))}
                lim = {k: chk[k] for k in ("min_v", "max_v", "min_ohm") if chk.get(k) is not None}
                if lim:
                    c["limits"] = lim
                if chk.get("learn"):
                    c["limits_pending"] = "learn from a golden unit (runner 'learn' mode) or set in config"
                criteria.append(c)
            for it in st.get("items", []) or []:
                criteria.append({"id": it["id"], "type": "visual", "step": st["id"], "description": it["check"],
                                 "refs": it.get("refs", [])})
    sup = design.supply()
    supply_cfg = design.cfg.get("supply") or {}
    if supply_cfg.get("max_idle_current_a") is not None:
        criteria.append({"id": "I-IDLE", "type": "electrical", "description": "Idle supply current",
                         "limits": {"min_a": supply_cfg.get("min_idle_current_a"), "max_a": supply_cfg["max_idle_current_a"]}})

    smd_top = sum(1 for fp in comps if fp.is_smd and fp.side == "top" and not fp.dnp)
    smd_bot = sum(1 for fp in comps if fp.is_smd and fp.side == "bottom" and not fp.dnp)
    tht = sum(1 for fp in comps if fp.is_tht and not fp.dnp)
    fdicts = [f.to_dict(int(design.cfg["analysis"]["max_items_per_finding"])) for f in findings]
    return {
        "schema": SPEC_SCHEMA,
        "generator": "PCB TestKit %s" % __version__,
        "generated": _generated_at(),
        # file names only: the spec is shared, and absolute paths would leak the author's folder layout
        "source": {"board": os.path.basename(b.path),
                   "schematic": os.path.basename(design.sch.root_path) if design.sch else None,
                   "file_format_version": b.version, "generator": "%s %s" % (b.generator, b.generator_version)},
        "board": {
            "name": b.name, "title": b.title_block.get("title", ""), "revision": b.title_block.get("rev", ""),
            "date": b.title_block.get("date", ""), "company": b.title_block.get("company", ""),
            "outline_mm": bb.as_dict(3), "area_mm2": _r(bb.w * bb.h, 1), "thickness_mm": b.thickness,
            "copper_layers": len(b.copper_layers), "layer_names": b.copper_layers,
            "copper_thickness_um": _r(b.copper_thickness_mm * 1000, 1), "finish": b.copper_finish or None,
            "stackup": b.stackup or None,
        },
        "technology": _tech_section(design),
        "manufacturing": {"fab_profile": design.cfg.get("fab_profile"), "fab_limits": design.cfg["fab"],
                          "project_rules": b.rules or None, "netclasses": b.netclasses or None,
                          "measured": manufacturing_stats(design)},
        "assembly": {"component_count": len(comps), "smd_top": smd_top, "smd_bottom": smd_bot, "tht": tht,
                     "dnp": sorted([fp.ref for fp in comps if fp.dnp], key=C.natural_key),
                     "components": [component_entry(design, fp) for fp in sorted(comps, key=lambda f: C.natural_key(f.ref))]},
        "electrical": {"supply": sup, "power_nets": design.nets_of_kind("power"), "ground_nets": design.nets_of_kind("ground"),
                       "nets": nets},
        "acceptance_criteria": criteria,
        "findings": fdicts,
        "summary": summarize(findings),
        "decision_log": (design.user_config.get("_audit") or [])[-100:],
        "warnings": list(design.warnings),
    }
