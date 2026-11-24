"""Where class systems come from.

Later sources override earlier ones with the same id:

1. built-in            pcbtestkit/classsys/systems/*.json
2. per user            ~/.pcbtestkit/classsystems/*.json   (or $PCBTESTKIT_CLASSSYSTEMS, ':'-separated)
3. per project         <project>/classsystems/*.json
4. config folders      tech.systems_dirs in pcbtestkit.json
5. config inline       tech.systems in pcbtestkit.json ({id: definition})
6. legacy tables       tech.tables (the pre-framework Eurocircuits-style format) -> converted

User/project definitions with errors are skipped with a warning; built-in ones
must validate (the test suite checks that).
"""
from __future__ import annotations

import glob
import json
import os
from typing import Any, Dict, List, Optional, Tuple

from .engine import System

HERE = os.path.dirname(os.path.abspath(__file__))
BUILTIN_DIR = os.path.join(HERE, "systems")
DEFAULT_SYSTEM = "eurocircuits-2024"
_BUILTIN: Optional[Dict[str, Dict[str, Any]]] = None


def _read(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def builtin_raw() -> Dict[str, Dict[str, Any]]:
    global _BUILTIN
    if _BUILTIN is None:
        _BUILTIN = {}
        for p in sorted(glob.glob(os.path.join(BUILTIN_DIR, "*.json"))):
            raw = _read(p)
            _BUILTIN[raw["id"]] = raw
    return _BUILTIN


def user_dirs(cfg: Optional[Dict[str, Any]] = None, project_dir: Optional[str] = None) -> List[str]:
    env = os.environ.get("PCBTESTKIT_CLASSSYSTEMS")
    dirs = env.split(os.pathsep) if env else [os.path.join(os.path.expanduser("~"), ".pcbtestkit", "classsystems")]
    if project_dir:
        dirs.append(os.path.join(project_dir, "classsystems"))
    for d in ((cfg or {}).get("tech") or {}).get("systems_dirs") or []:
        dirs.append(d if os.path.isabs(d) or not project_dir else os.path.join(project_dir, d))
    return dirs


def from_legacy_table(name: str, t: Dict[str, Any]) -> Dict[str, Any]:
    """Pre-framework tech.tables entry (pattern 3..9 x drill A..E) -> class-system definition."""
    pat_keys = {"otw": "track_outer", "ott": "gap_outer", "oar": "ring_outer", "itw": "track_inner", "itt": "gap_inner",
                "iar": "ring_inner", "ipi": "hole_to_copper_inner"}
    pattern = []
    for k in sorted(t["pattern"], key=lambda x: int(x)):
        v = t["pattern"][k]
        lim = {pat_keys[p]: v[p] for p in pat_keys if p in v}
        pattern.append({"id": str(k), "limits": lim, "offers": v.get("services", "all")})
    drill = []
    for k in sorted(t["drill"]):
        v = t["drill"][k]
        lim = {"drill_plated": v["pth"], "drill_npth": v["npth"], "toolsize": v["toolsize"]}
        if "max_thickness" in v:
            lim["thickness_max"] = v["max_thickness"]
        drill.append({"id": str(k), "limits": lim, "offers": v.get("services", "all")})
    off = float(t.get("pth_toolsize_offset", 0.10))
    return {"id": name, "name": t.get("name", name), "status": "user", "source": t.get("source"),
            "hole_basis": {"pth_offset": off, "npth_offset": float(t.get("npth_toolsize_offset", 0.0)),
                           "ring": "toolsize", "hole_clearance": "toolsize", "aspect": "toolsize"},
            "class_format": "{pattern}{drill}",
            "dimensions": [{"id": "pattern", "label": "Pattern class", "levels": pattern},
                           {"id": "drill", "label": "Drill class", "levels": drill}],
            "always": {"aspect_ratio": float(t.get("aspect_ratio", 8.0))}}


def load_systems(cfg: Optional[Dict[str, Any]] = None, project_dir: Optional[str] = None,
                 validate_user: bool = True) -> Tuple[Dict[str, System], List[str]]:
    from .validate import validate
    out: Dict[str, System] = {}
    warnings: List[str] = []
    for sid, raw in builtin_raw().items():
        out[sid] = System(raw, origin="built-in")

    def add(raw: Dict[str, Any], origin: str) -> None:
        if validate_user:
            r = validate(raw, run_tests=True)
            if not r["ok"]:
                warnings.append("class system from %s skipped: %s" % (origin, "; ".join(r["errors"][:3])))
                return
        sid = raw["id"]
        if sid in out:
            warnings.append("class system %s from %s replaces the %s one" % (sid, origin, out[sid].origin))
        out[sid] = System(raw, origin=origin)

    for d in user_dirs(cfg, project_dir):
        for p in sorted(glob.glob(os.path.join(d, "*.json"))):
            try:
                add(_read(p), p)
            except (OSError, ValueError, KeyError) as e:
                warnings.append("cannot read class system %s: %s" % (p, e))
    tech = (cfg or {}).get("tech") or {}
    inline = tech.get("systems") or {}
    for sid, raw in (inline.items() if isinstance(inline, dict) else [(r.get("id"), r) for r in inline]):
        raw = dict(raw)
        raw.setdefault("id", sid)
        add(raw, "pcbtestkit.json tech.systems")
    for name, t in (tech.get("tables") or {}).items():
        try:
            add(from_legacy_table(name, t), "pcbtestkit.json tech.tables")
        except (KeyError, TypeError, ValueError) as e:
            warnings.append("legacy table %s not converted: %s" % (name, e))
    return out, warnings


def system_id_from_config(cfg: Dict[str, Any]) -> str:
    tech = cfg.get("tech") or {}
    return tech.get("system") or tech.get("table") or DEFAULT_SYSTEM
