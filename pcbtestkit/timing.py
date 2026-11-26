"""How long a batch takes to assemble, program and test, and which batch size gives the shortest time per board.

The model follows the test plan's phases (pcbtestkit.phases) and the runner's batch flow: every board gets its
unpowered checks and is powered up, then all programmers start at the same time and, while they run, the checks
that do not need the firmware are measured board by board; the checks that need it follow once the programmers are
done. Hand assembly uses the jigs: fitting and soldering every part, plus one placement/holding jig cycle per jig
load (a jig holds several boards of a batch).

The durations are estimates; set your own in ``timing`` in pcbtestkit.json. The runner records how long each step
really took (``timings`` in every result file), so the numbers can be checked against the line.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from .config import DEFAULTS as _CFG

DEFAULTS: Dict[str, float] = dict(_CFG["timing"], program_s=float(_CFG["program"]["estimate_s"]))


def settings(cfg: Dict[str, Any]) -> Dict[str, float]:
    """The durations: pcbtestkit.json ``timing`` over the defaults; programming from ``program.estimate_s``."""
    t = dict(DEFAULTS)
    for k, v in ((cfg or {}).get("timing") or {}).items():
        if k in t and isinstance(v, (int, float)):
            t[k] = float(v)
    est = ((cfg or {}).get("program") or {}).get("estimate_s")
    if isinstance(est, (int, float)) and est > 0:
        t["program_s"] = float(est)
    return t


def _step_s(st: Dict[str, Any], t: Dict[str, float]) -> float:
    typ = st.get("type")
    if typ == "manual":
        return t["visual_s"] if not st.get("custom") else 10.0
    if typ == "resistance":
        return t["short_check_s"] * max(1, len(st.get("checks") or []))
    if typ == "power":
        return 0.3 + float(st.get("settle_ms", 500)) / 1000.0 if st.get("action") != "off" else 0.2
    if typ == "voltage":
        return t["read_s"] * len(st.get("checks") or []) + float(st.get("settle_ms", 0) or 0) / 1000.0
    if typ == "bus":
        return t["bus_s"]
    if typ == "wait":
        return float(st.get("wait_ms", 0)) / 1000.0
    if typ == "program":
        return t["program_s"]
    return 2.0


def test_times(plan: Dict[str, Any], n: int, t: Dict[str, float]) -> Dict[str, Any]:
    """Seconds to test a batch of ``n`` boards: one after the other (each programmed in turn) and with the runner's
    batch flow (all programmed at once, checks meanwhile). With a timeline per board for the batch flow."""
    seq = plan.get("sequence") or []
    by = {}
    for st in seq:
        by.setdefault(st.get("phase", "powered"), []).append(st)
    dur = {ph: sum(_step_s(st, t) for st in sts) for ph, sts in by.items()}
    per_board_io = t["board_handling_s"] + t["serial_entry_s"]
    one = per_board_io + sum(dur.values())
    sequential = n * one
    has_prog = bool(by.get("program"))
    timeline: List[Dict[str, Any]] = []
    clock = 0.0
    lanes = {k: [] for k in range(1, n + 1)}
    for k in range(1, n + 1):                         # load the boards, unpowered checks and power-up, board by board
        a = clock
        clock += per_board_io + dur.get("unpowered", 0.0) + dur.get("power", 0.0)
        lanes[k].append({"phase": "load, check, power up", "start": round(a, 1), "end": round(clock, 1)})
    if has_prog:
        p0 = clock
        lanes_end = p0
        for k in range(1, n + 1):                     # every programmer runs from p0; the runner measures meanwhile
            lanes[k].append({"phase": "programming", "start": round(p0, 1), "end": round(p0 + t["program_s"], 1)})
        for k in range(1, n + 1):
            a = lanes_end
            lanes_end += dur.get("during_program", 0.0)
            if dur.get("during_program"):
                lanes[k].append({"phase": "checks while programming", "start": round(a, 1), "end": round(lanes_end, 1)})
        clock = max(p0 + t["program_s"], lanes_end)
    for k in range(1, n + 1):
        a = clock
        clock += dur.get("after_program", 0.0) + dur.get("powered", 0.0) + dur.get("end", 0.0)
        lanes[k].append({"phase": "checks with firmware" if has_prog else "checks", "start": round(a, 1), "end": round(clock, 1)})
    for k in range(1, n + 1):
        timeline.append({"site": k, "segments": lanes[k]})
    return {"one_board_s": round(one, 1), "sequential_s": round(sequential, 1), "batch_s": round(clock, 1),
            "per_board_s": round(clock / n, 1), "saved_s": round(sequential - clock, 1), "timeline": timeline,
            "phases_s": {k: round(v, 1) for k, v in dur.items()}}


def assembly_times(design: Any, jig: Optional[Dict[str, Any]], n: int, t: Dict[str, float]) -> Dict[str, Any]:
    """Seconds to hand-solder a batch of ``n`` boards with the jigs (every hand pass of the board)."""
    if not jig:
        return {"per_board_s": 0.0, "batch_s": 0.0, "parts": 0, "joints": 0, "jig_loads": 0}
    from .jig import jig_of
    passes = [p for p in jig.get("passes") or [] if p.get("process") == "hand"] or [{"jig": jig.get("key")}]
    per_jig = max(1, int((jig.get("batch") or {}).get("boards_per_jig") or 1))
    parts = joints = loads = 0
    for p in passes:
        j = jig_of(jig, p.get("jig")) or jig
        refs = j.get("parts_to_place") or []
        parts += len(refs)
        joints += sum(1 for r in refs if r in design.board.by_ref for pd in design.board.by_ref[r].pads
                      if pd.drill_w > 0 and pd.has_copper)
        loads += int(math.ceil(n / float(per_jig)))
    work = n * (parts * t["place_part_s"] + joints * t["solder_joint_s"])
    total = work + loads * t["jig_cycle_s"]
    return {"per_board_s": round(total / n, 1), "batch_s": round(total, 1), "parts": parts, "joints": joints,
            "jig_loads": loads, "boards_per_jig": per_jig}


def production_timing(design: Any, plan: Dict[str, Any], jig: Optional[Dict[str, Any]],
                      max_batch: int = 8) -> Dict[str, Any]:
    """The estimates for the configured batch, and the time per board for every batch size up to ``max_batch``."""
    t = settings(design.cfg)
    n = int((plan.get("batch") or {}).get("count") or 1)
    test = test_times(plan, n, t)
    asm = assembly_times(design, jig, n, t)
    sizes = []
    for k in range(1, max(n, max_batch) + 1):
        tt = test_times(plan, k, t)
        aa = assembly_times(design, jig, k, t) if jig else {"per_board_s": 0.0}
        sizes.append({"boards": k, "test_per_board_s": tt["per_board_s"], "assembly_per_board_s": aa["per_board_s"],
                      "per_board_s": round(tt["per_board_s"] + aa["per_board_s"], 1)})
    best = min(sizes, key=lambda r: (r["per_board_s"], r["boards"]))
    # past this size a bigger batch saves less than 5 % per board: worth no larger fixture
    knee = next((r for r in sizes if r["per_board_s"] <= best["per_board_s"] * 1.05), best)
    return {"assumptions": t, "batch": n, "test": test, "assembly": asm,
            "per_board_s": round(test["per_board_s"] + asm["per_board_s"], 1), "by_batch_size": sizes,
            "suggested_batch": knee["boards"],
            "note": "Estimates; set your own durations in 'timing' in pcbtestkit.json. The runner records the real "
                    "ones in every result file."}
