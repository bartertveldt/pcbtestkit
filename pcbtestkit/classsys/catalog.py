"""The metric catalog: the shared vocabulary every class system is written in.

A class system (Eurocircuits classes, a fab's capability page, an IPC table, ...)
never measures anything itself. It only states limits on the metrics below.
:mod:`pcbtestkit.measure` measures the board once into *streams* of items, and
each metric is "a stream + an item filter + the value to compare". Adding a
class system therefore needs no code; adding a metric needs one line here (or a
``metrics`` entry in the system file, built from the same streams).

kind
    min      measured value must be >= limit      (track width, gaps, rings, drills)
    max      measured value must be <= limit      (aspect ratio, board thickness)
    allowed  measured value must be in the list   (layer count, board thickness options)
    feature  count of a feature; limit false = not allowed, true = allowed, number = max count
    manual   cannot be measured from the design; listed as a check for the order/fab
"""
from __future__ import annotations

from typing import Any, Dict, List

# Streams produced by pcbtestkit.measure.measure_board (fields per item).
STREAMS: Dict[str, str] = {
    "tracks": "w (width), layer, inner, net, x, y",
    "gaps": "d (copper edge-to-edge, different nets), layer, inner, ka/kb (track|pad|via|zone), lead (pad-pad), a, b, x, y",
    "holes": "kind (via|pth|npth), sub (through|blind), finished (smallest finished size), max_d, slot, pad, ring_f "
             "(ring from the finished hole), ringed, inner (has inner-layer copper), net, x, y",
    "hole_pairs": "gap (finished hole edge-to-edge, different footprints), ka/kb, pth, npth, vias, npth_pair, diffnet, x, y",
    "hole_copper": "cdist (hole centre-line to nearest unrelated copper edge), finished, kind, layer, inner, to, x, y",
    "edge": "d (copper to board edge), kind, x, y",
    "mask": "web (solder mask web between two pad openings), x, y",
    "silk_lines": "w (line width), layer, x, y",
    "silk_texts": "h (text height), t (stroke), layer, text, x, y",
    "smd_pads": "size (smallest pad dimension), x, y",
    "pair_gaps": "d (smallest gap per net pair and layer), na/nb, inner, lead, x, y; dv added from net voltages",
    "board": "layers, thickness_mm, copper_um, inner_copper_um, board_w_mm, board_h_mm, finish",
    "features": "counts: microvias, blind_vias, castellated, via_in_pad, mask_no_dam",
}

# Item fields per stream (usable in rule "items" filters and in per-item expressions such as "finished + 0.1").
FIELDS: Dict[str, List[str]] = {
    "tracks": ["w", "inner"], "gaps": ["d", "inner", "lead"],
    "holes": ["finished", "max_d", "pad", "ring_f", "slot", "inner", "plated", "ringed", "ring", "tool", "aspect"],
    "hole_pairs": ["gap", "pth", "npth", "vias", "npth_pair", "diffnet", "hole_gap"],
    "hole_copper": ["cdist", "finished", "inner", "hole_clear"], "edge": ["d"], "mask": ["web"],
    "silk_lines": ["w"], "silk_texts": ["h", "t"], "smd_pads": ["size"], "pair_gaps": ["d", "inner", "lead", "dv"],
    "board": [], "features": [],
}

# Derived values computed by the engine with the system's hole basis:
#   ring        ring_f, minus pth_offset/2 when hole_basis.ring == "toolsize"
#   tool        finished + pth_offset (plated) / npth_offset (npth)
#   aspect      board thickness / tool (plated through holes)
#   hole_clear  cdist - hole radius (finished, or tool when hole_basis.hole_clearance == "toolsize")
#   hole_gap    gap (finished) or gap - offsets/2 when hole_basis.hole_to_hole == "toolsize"
#   dv          |V(na) - V(nb)| from the design's net voltages (unknown nets count as 0 V)
DERIVED = ("ring", "tool", "aspect", "hole_clear", "hole_gap", "dv")


def _m(label, stream, value, kind="min", where=None, unit="mm", severity="major", doc=""):
    return {"label": label, "stream": stream, "value": value, "kind": kind, "where": where or {}, "unit": unit,
            "severity": severity, "doc": doc}


METRICS: Dict[str, Dict[str, Any]] = {
    # ---- copper pattern -------------------------------------------------------------------------
    "track_outer": _m("outer track width", "tracks", "w", where={"inner": False}),
    "track_inner": _m("inner track width", "tracks", "w", where={"inner": True}),
    "gap_outer": _m("outer copper gap", "gaps", "d", where={"inner": False},
                    doc="Edge-to-edge copper gap between different nets (track, pad, via; zone clearance settings)."),
    "gap_inner": _m("inner copper gap", "gaps", "d", where={"inner": True}),
    "gap_pad_pad_outer": _m("outer pad-to-pad gap", "gaps", "d", where={"inner": False, "lead": True},
                            doc="Gap between two pads of different nets on an outer layer."),
    "smd_pad_min": _m("smallest SMD pad", "smd_pads", "size"),
    # ---- rings and holes -------------------------------------------------------------------------
    "ring_via_outer": _m("via annular ring (outer)", "holes", "ring", where={"kind": "via", "ringed": True}),
    "ring_via_inner": _m("via annular ring (inner)", "holes", "ring", where={"kind": "via", "ringed": True, "inner": True}),
    "ring_pth_outer": _m("component hole annular ring (outer)", "holes", "ring", where={"kind": "pth", "ringed": True}),
    "ring_pth_inner": _m("component hole annular ring (inner)", "holes", "ring",
                         where={"kind": "pth", "ringed": True, "inner": True}),
    "via_pad": _m("via pad diameter", "holes", "pad", where={"kind": "via"}),
    "drill_via": _m("via hole (finished)", "holes", "finished", where={"kind": "via"}),
    "drill_pth": _m("component plated hole (finished)", "holes", "finished", where={"kind": "pth", "slot": False}),
    "drill_npth": _m("non-plated hole", "holes", "finished", where={"kind": "npth", "slot": False}),
    "toolsize": _m("smallest drill tool", "holes", "tool",
                   doc="Production drill = finished hole + the system's pth_offset (npth_offset for NPTH)."),
    "drill_max": _m("largest round hole", "holes", "max_d", kind="max", where={"slot": False}),
    "slot_plated": _m("plated slot width", "holes", "finished", where={"kind": "pth", "slot": True}),
    "slot_npth": _m("non-plated slot width", "holes", "finished", where={"kind": "npth", "slot": True}),
    "aspect_ratio": _m("aspect ratio (thickness : drill)", "holes", "aspect", kind="max", unit="ratio",
                       where={"plated": True, "sub": "through"}),
    "hole_to_hole": _m("hole-to-hole gap", "hole_pairs", "hole_gap"),
    "hole_to_hole_via": _m("via-to-via hole gap", "hole_pairs", "hole_gap", where={"vias": True}),
    "hole_to_hole_pth": _m("hole gap involving a component hole", "hole_pairs", "hole_gap", where={"pth": True}),
    "hole_to_hole_npth": _m("hole gap involving a non-plated hole", "hole_pairs", "hole_gap", where={"npth": True}),
    "hole_to_hole_diffnet": _m("hole gap between different nets", "hole_pairs", "hole_gap", where={"diffnet": True}),
    "hole_to_copper_via": _m("via hole to copper (outer)", "hole_copper", "hole_clear", where={"kind": "via", "inner": False}),
    "hole_to_copper_pth": _m("component hole to copper (outer)", "hole_copper", "hole_clear", where={"kind": "pth", "inner": False}),
    "hole_to_copper_npth": _m("non-plated hole to copper", "hole_copper", "hole_clear", where={"kind": "npth"}),
    "hole_to_copper_inner_via": _m("via hole to inner-layer copper", "hole_copper", "hole_clear",
                                   where={"kind": "via", "inner": True},
                                   doc="Includes plane clearances around unconnected holes (Eurocircuits IPI)."),
    "hole_to_copper_inner_pth": _m("component hole to inner-layer copper", "hole_copper", "hole_clear",
                                   where={"kind": "pth", "inner": True}),
    "copper_to_edge": _m("copper to board edge", "edge", "d"),
    # ---- mask and legend --------------------------------------------------------------------------
    "mask_web": _m("solder mask web", "mask", "web", severity="minor",
                   doc="Mask between two pad openings (pad gap minus both mask expansions). Too thin: the fab removes it."),
    "silk_line": _m("silkscreen line width", "silk_lines", "w", severity="minor"),
    "silk_text_height": _m("silkscreen text height", "silk_texts", "h", severity="minor"),
    "silk_text_thickness": _m("silkscreen text stroke", "silk_texts", "t", severity="minor", where={"t": {"gt": 0}}),
    # ---- whole board --------------------------------------------------------------------------------
    "layers_min": _m("copper layers (min)", "board", "layers", unit="count"),
    "layers_max": _m("copper layers (max)", "board", "layers", kind="max", unit="count"),
    "layers_allowed": _m("copper layer count (allowed)", "board", "layers", kind="allowed", unit="count"),
    "thickness_min": _m("board thickness (min)", "board", "thickness_mm"),
    "thickness_max": _m("board thickness (max)", "board", "thickness_mm", kind="max"),
    "thickness_allowed": _m("board thickness (allowed)", "board", "thickness_mm", kind="allowed"),
    "copper_max_um": _m("outer base copper", "board", "copper_um", kind="max", unit="um"),
    "inner_copper_max_um": _m("inner base copper", "board", "inner_copper_um", kind="max", unit="um"),
    "board_size_max": _m("largest board dimension", "board", "board_max_mm", kind="max"),
    "board_size_min": _m("smallest board dimension", "board", "board_min_mm"),
    # ---- process features (counts) -------------------------------------------------------------------
    "microvias": _m("laser microvias", "features", "microvias", kind="feature", unit="count"),
    "blind_vias": _m("blind/buried vias", "features", "blind_vias", kind="feature", unit="count"),
    "castellated": _m("castellated pads", "features", "castellated", kind="feature", unit="count"),
    "via_in_pad": _m("vias inside SMD pads", "features", "via_in_pad", kind="feature", unit="count",
                     doc="Need filling and capping, or they wick solder away from the joint."),
    "mask_no_dam": _m("pad pairs without a mask dam", "features", "mask_no_dam", kind="feature", unit="count",
                      severity="minor"),
    # ---- voltage-dependent spacing (limits come from a lookup table per net-pair voltage) --------------
    "voltage_gap_inner": _m("inner conductor spacing for the voltage", "pair_gaps", "d", where={"inner": True}),
    "voltage_gap_outer": _m("outer conductor spacing for the voltage", "pair_gaps", "d", where={"inner": False, "lead": False}),
    "voltage_gap_lead": _m("component lead spacing for the voltage", "pair_gaps", "d", where={"inner": False, "lead": True}),
    # ---- not measurable from the design: listed as order/fab checks ------------------------------------
    "plating_um": _m("hole wall copper plating", None, None, kind="manual", unit="um"),
    "silk_to_pad": _m("silkscreen to pad clearance", None, None, kind="manual"),
    "mask_expansion": _m("solder mask expansion", None, None, kind="manual"),
}

ALIASES: Dict[str, List[str]] = {
    "track": ["track_outer", "track_inner"],
    "gap": ["gap_outer", "gap_inner"],
    "ring": ["ring_via_outer", "ring_via_inner", "ring_pth_outer", "ring_pth_inner"],
    "ring_outer": ["ring_via_outer", "ring_pth_outer"],
    "ring_inner": ["ring_via_inner", "ring_pth_inner"],
    "ring_via": ["ring_via_outer", "ring_via_inner"],
    "ring_pth": ["ring_pth_outer", "ring_pth_inner"],
    "drill_plated": ["drill_via", "drill_pth"],
    "slot": ["slot_plated", "slot_npth"],
    "hole_to_copper": ["hole_to_copper_via", "hole_to_copper_pth", "hole_to_copper_npth",
                       "hole_to_copper_inner_via", "hole_to_copper_inner_pth"],
    "hole_to_copper_plated": ["hole_to_copper_via", "hole_to_copper_pth", "hole_to_copper_inner_via",
                              "hole_to_copper_inner_pth"],
    "hole_to_copper_inner": ["hole_to_copper_inner_via", "hole_to_copper_inner_pth"],
    "voltage_gap": ["voltage_gap_inner", "voltage_gap_outer", "voltage_gap_lead"],
}

# Context keys a rule's "when" (and an expression) may use. Board-derived keys are filled in
# automatically; the others come from tech.context in pcbtestkit.json or the system's parameters.
CONTEXT_KEYS: Dict[str, str] = {
    "layers": "copper layer count (board, or tech.layers)",
    "copper_um": "outer base copper in um (board stack-up, or tech.base_copper_um)",
    "inner_copper_um": "inner base copper in um (board stack-up)",
    "thickness_mm": "board thickness",
    "finish": "surface finish: HASL, HASL-LF, ENIG, ENEPIG, OSP, IMMERSION-SILVER, IMMERSION-TIN, HARD-GOLD "
              "(from the stack-up; override with tech.context.finish)",
    "board_w_mm": "board outline width",
    "board_h_mm": "board outline height",
    "mask_color": "solder mask colour (tech.context.mask_color, default green)",
    "edge_process": "routed | vscore (tech.context.edge_process, default routed)",
}

ITEM_LIMIT = 25
