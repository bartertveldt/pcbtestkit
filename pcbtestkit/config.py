"""Defaults, fabrication/pogo profiles and the per-project config file.

All decisions that need engineering judgement (rail voltages, tolerances,
which nets to test, which parts are hand-soldered, ...) live in one JSON file
next to the project: ``pcbtestkit.json``. People edit it by hand, the KiCad
dialog writes it, and AI models change it through the validated operations in
:mod:`pcbtestkit.agent`. Every output is regenerated deterministically from
board + schematic + this file.
"""
from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict

CONFIG_NAME = "pcbtestkit.json"
SCHEMA_VERSION = 1

FAB_PROFILES: Dict[str, Dict[str, float]] = {
    # Values are absolute minimums of the fab process; "near limit" is flagged at warn_factor x limit.
    "standard": {"min_track": 0.127, "min_clearance": 0.127, "min_drill": 0.3, "min_via_annular": 0.1,
                 "min_tht_annular": 0.15, "min_edge_clearance": 0.3, "min_hole_to_hole": 0.25,
                 "max_aspect_ratio": 8.0, "warn_factor": 1.25},
    "advanced": {"min_track": 0.09, "min_clearance": 0.09, "min_drill": 0.2, "min_via_annular": 0.075,
                 "min_tht_annular": 0.125, "min_edge_clearance": 0.25, "min_hole_to_hole": 0.2,
                 "max_aspect_ratio": 10.0, "warn_factor": 1.2},
    "conservative": {"min_track": 0.2, "min_clearance": 0.2, "min_drill": 0.35, "min_via_annular": 0.15,
                     "min_tht_annular": 0.25, "min_edge_clearance": 0.5, "min_hole_to_hole": 0.4,
                     "max_aspect_ratio": 6.0, "warn_factor": 1.3},
}

# Pogo pin families. pitch = minimum centre distance; drills per mounting style.
POGO_PROFILES: Dict[str, Dict[str, float]] = {
    "P50": {"pitch": 1.27, "drill_direct": 0.75, "drill_receptacle": 0.9, "min_target": 0.6, "pad_extra": 0.4},
    "P75": {"pitch": 1.91, "drill_direct": 1.1, "drill_receptacle": 1.4, "min_target": 0.9, "pad_extra": 0.5},
    "P100": {"pitch": 2.54, "drill_direct": 1.45, "drill_receptacle": 1.7, "min_target": 1.0, "pad_extra": 0.6},
}

DEFAULTS: Dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "fab_profile": "standard",
    "fab_overrides": {},
    # Technology class to order. system = class system id (pcbtestkit classes list), class = e.g. "6C" for
    # Eurocircuits or "standard" for a fab tier. When set, its limits replace the fab profile's.
    # checks = extra class checks, e.g. ["ipc-2221-voltage:B2", "ipc-6012-ring:3"].
    # context = values the board cannot tell (finish, mask_color, edge_process, system parameters).
    "tech": {"system": None, "class": None, "layers": None, "base_copper_um": None, "context": {}, "checks": [],
             "systems_dirs": [], "systems": {}, "table": None, "tables": {}},
    "analysis": {
        "edge_mlcc_mm": 3.0,
        "edge_component_mm": 0.5,
        "decoupling_max_mm": 5.0,
        "heat_distance_mm": 10.0,
        "tall_component_mm": 15.0,
        "power_min_track_mm": 0.3,
        "temperature_rise_c": 10.0,
        "cap_derating": {"ceramic": 0.5, "electrolytic": 0.8, "tantalum": 0.5, "other": 0.8},
        "resistor_derating": 0.5,
        "mounting_hole_flex_mm": 3.0,
        "large_board_mm2": 2500.0,
        "acid_trap_deg": 80.0,
        "run_kicad_drc": True,
        "run_kicad_erc": True,
        "ignore_checks": [],
        "ignore_refs": [],
        "waivers": [],
        "max_items_per_finding": 25,
    },
    # Per-net decisions. Example: {"+3V3": {"nominal": 3.3, "tolerance_pct": 5}, "VBAT": {"min": 3.0, "max": 4.25}}
    "nets": {},
    "supply": {"net": None, "voltage": None, "current_limit_a": 0.5, "max_idle_current_a": None,
               "min_idle_current_a": None},
    # Boards stacked on this one (daughter board, shield, display on headers or board-to-board connectors).
    # One entry each: {"name": "display", "refs": ["J6"], "side": "top", "outline": [x0, y0, x1, y1] (board mm),
    # "height_mm": 14, "fitted_at_test": true}. Fitted: no probes under it, the fixture gets a window for it.
    # Not fitted: the board is tested before the stack goes on. Missing outline/height are estimated.
    "stack": [],
    "testplan": {
        "probe_side": "auto",       # auto | bottom | top | both (two fixture plates, probes from both sides)
        "pogo": "P75",
        "pogo_mount": "receptacle",
        "pogo_drill_mm": None,      # override the profile drill (check your pogo/receptacle datasheet)
        "signals": "auto",          # none | auto | all
        "include_nets": [],
        "exclude_nets": [],
        "max_channels": 32,
        "edge_keepout_mm": 1.5,
        "min_target_mm": None,
        "allow_vias": False,
        "allow_component_pads": True,
        "gnd_points": 2,
        "supply_points": 2,
        "default_tolerance_pct": 5.0,
        "signal_tolerance_v": 0.15,
        "adc_max_v": 3.3,
        "max_signal_voltage": None,
        "probe_clearance_mm": 6.0,
        "stack_clearance_mm": 1.0,  # probes stay this far outside a fitted stacked board's outline
        "short_test_min_ohm": 100.0,
        "settle_ms": 500,
        "fine_pitch_exclude_mm": 0.8,
        "custom_steps": [],
        "manual_checks": [],
    },
    "fixture": {
        "mode": "generate",         # generate (pogo-pin fixture board) | own (you design it: probe-point CSV) | none
        "outline": "rect",          # rect | outline
        "margin_mm": 8.0,
        "panel": {"cols": 1, "rows": 1, "spacing_mm": 10.0},
        "mounting_holes": True,
        "mounting_hole_d_mm": 3.2,
        "dividers": True,
        "divider_r_bottom_rail": 10000.0,
        "divider_r_bottom_signal": 100000.0,
        "draw_dut_components": True,
        "cut_tall_components": True,
        "route": True,              # route the fixture board with the built-in router (two layers)
        "track_mm": 0.25,           # signal tracks
        "power_track_mm": 0.5,      # GND and VIN tracks (narrower where that does not fit)
        "clearance_mm": 0.2,
        "via_mm": 0.6,
        "via_drill_mm": 0.3,
    },
    "jig": {
        "parts": "auto",            # auto | [refs]
        "exclude": [],
        "relief_all_top": True,
        "resolution_mm": 0.25,
        "hold_clearance_mm": 0.25,
        "relief_clearance_mm": 0.8,
        "relief_extra_depth_mm": 1.0,
        "hold_depth_offset_mm": 0.1,
        "floor_mm": 2.0,
        "rim_mm": 3.0,
        "rim_height_mm": 1.2,
        "edge_clearance_mm": 0.3,
        "posts": True,
        "post_clearance_mm": 0.2,
        "finger_notches": True,
        "heights": {},
        # two-jig workflow (default): a placement jig holds the PCB top side up while the parts are fitted,
        # the holding jig closes over them, the stack is turned over and the placement jig lifted off
        "workflow": "two_jig",          # two_jig | single (parts into the holding jig, PCB lowered onto them)
        "placement_clearance_mm": 10.0,  # room under the PCB for the leads (trim them to fit)
        "placement_border_mm": 6.0,
        "lead_well_margin_mm": 1.0,
        "stop_length_mm": 8.0,
        "stop_width_mm": 2.5,
        "stop_clearance_mm": 0.15,
        "bottom_part_clearance_mm": 0.8,
        # boards with parts on both sides: one jig set per hand-soldered side, in this order
        "sides": "auto",                 # auto (top then bottom, where there are hand-soldered parts) | ["bottom", "top"] | ...
        "smt_first": "auto",             # SMD side reflowed first: auto (the side with fewer parts) | top | bottom
        "paste_nest": "auto",            # paste-print nest: auto (second SMD side) | both | none
        "lead_relief_mm": 2.5,           # reliefs for the soldered leads of the other side's parts
        "nest_border_mm": 15.0,          # flat frame around the board in the paste-print nest (stencil support)
    },
    "video": {
        "animate": True,            # real 3D motion shots rendered with kicad-cli; false = quick stills-only video
        "fps": 24,
        "width": 1280,
        "height": 720,
        "workers": 0,               # parallel kicad-cli renders; 0 = number of CPU cores - 2 (max 8)
        "quality": "basic",         # kicad-cli render quality for the motion shots: basic | high
    },
    "outputs": {"spec": True, "testplan": True, "fixture": True, "jig": True, "report": True, "runner": True,
                "guide": True, "visuals": True, "video": False, "keep_scenes": False},
}


def deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("nets", "heights", "tables", "systems") and not k.startswith("_"):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def config_path_for(project_dir: str) -> str:
    return os.path.join(project_dir, CONFIG_NAME)


def load_user_config(path: str) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("%s must contain a JSON object" % path)
    return data


def effective_config(user: Dict[str, Any]) -> Dict[str, Any]:
    cfg = deep_merge(DEFAULTS, user or {})
    prof = FAB_PROFILES.get(cfg.get("fab_profile", "standard"), FAB_PROFILES["standard"])
    fab = dict(prof)
    # A technology class (tech.class) is applied by project.Design, which knows the board (layer count,
    # copper, finish) that conditional class rules depend on.
    fab["source"] = "fab profile %s" % cfg.get("fab_profile", "standard")
    fab.update(cfg.get("fab_overrides") or {})
    cfg["fab"] = fab
    pogo = POGO_PROFILES.get(cfg["testplan"].get("pogo", "P75"), POGO_PROFILES["P75"])
    cfg["pogo"] = dict(pogo)
    return cfg


def save_user_config(path: str, user: Dict[str, Any]) -> None:
    data = dict(user)
    data["schema_version"] = SCHEMA_VERSION
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, path)


def pogo_geometry(cfg: Dict[str, Any]):
    """(drill, pad diameter, minimum centre spacing) for the configured pogo family and mounting.

    The spacing honours both the family pitch and the fixture PCB: pads need >= 0.5 mm copper gap.
    """
    tp, pg = cfg["testplan"], cfg["pogo"]
    mount = tp.get("pogo_mount", "receptacle")
    drill = float(tp.get("pogo_drill_mm") or pg["drill_receptacle" if mount == "receptacle" else "drill_direct"])
    pad = round(drill + float(pg["pad_extra"]), 3)
    return drill, pad, round(max(float(pg["pitch"]), pad + 0.5), 3)
