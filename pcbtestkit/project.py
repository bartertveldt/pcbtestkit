"""Design = board + schematic + project config, with resolved engineering decisions."""
from __future__ import annotations

import glob
import os
from typing import Any, Dict, List, Optional, Tuple

from . import classify as C
from .config import config_path_for, effective_config, load_user_config
from .pcb import Board, Footprint, load_board
from .schematic import Schematic, load_schematic


def locate(path: str) -> Tuple[str, Optional[str], Optional[str]]:
    """Accept a .kicad_pcb, .kicad_pro, .kicad_sch or a project folder; return (pcb, sch, pro)."""
    p = os.path.abspath(path)
    if os.path.isdir(p):
        pros = sorted(glob.glob(os.path.join(p, "*.kicad_pro")))
        pcbs = sorted(glob.glob(os.path.join(p, "*.kicad_pcb")))
        if pros:
            stem = os.path.splitext(pros[0])[0]
        elif pcbs:
            stem = os.path.splitext(pcbs[0])[0]
        else:
            raise FileNotFoundError("no KiCad project or board found in %s" % p)
    else:
        stem = os.path.splitext(p)[0]
    pcb = stem + ".kicad_pcb"
    if not os.path.exists(pcb):
        raise FileNotFoundError("board file not found: %s" % pcb)
    sch = stem + ".kicad_sch"
    pro = stem + ".kicad_pro"
    return pcb, (sch if os.path.exists(sch) else None), (pro if os.path.exists(pro) else None)


class Design:
    def __init__(self, board: Board, schematic: Optional[Schematic], user_config: Dict[str, Any],
                 config_path: str = ""):
        self.board = board
        self.sch = schematic
        self.user_config = user_config
        self.config_path = config_path
        self.cfg = effective_config(user_config)
        self.overlay: Dict[str, Any] = {}           # settings a test environment adds on top (never saved)
        self.environment: Optional[Dict[str, Any]] = None
        self.warnings: List[str] = []
        self._systems = None
        self._measure_cache = None
        self._tech_cache = None
        self._merge_schematic_fields()
        self._net_classes = C.classify_all_nets(board, self.power_symbol_nets)
        self._apply_net_overrides()
        self._apply_class_limits()

    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, path: str, config_path: Optional[str] = None, overrides: Optional[Dict[str, Any]] = None) -> "Design":
        from .config import deep_merge
        pcb, sch_path, pro = locate(path)
        board = load_board(pcb, pro)
        sch = load_schematic(sch_path) if sch_path else None
        cfg_path = config_path or config_path_for(os.path.dirname(pcb))
        user = load_user_config(cfg_path)
        if overrides:
            user = deep_merge(user, overrides)
        d = cls(board, sch, user, cfg_path)
        if sch:
            d.warnings.extend(sch.warnings)
        else:
            d.warnings.append("no schematic found next to the board; schematic-based checks are skipped")
        from .environment import apply_environment, environment_setting
        envc = environment_setting(d.cfg)
        if envc:                                   # made for a test environment: its station, the batch it allows
            apply_environment(d, envc)
        return d

    def set_overlay(self, overlay: Dict[str, Any]) -> None:
        """Settings on top of pcbtestkit.json that are not saved in it (a test environment's station and batch)."""
        self.overlay = dict(overlay or {})
        self.reload_config(self.user_config)

    def reload_config(self, user_config: Dict[str, Any]) -> None:
        from .config import deep_merge
        self.user_config = user_config
        self.cfg = effective_config(deep_merge(user_config, self.overlay) if self.overlay else user_config)
        self._systems = None
        self._tech_cache = None
        self._net_classes = C.classify_all_nets(self.board, self.power_symbol_nets)
        self._apply_net_overrides()
        self._apply_class_limits()

    # ------------------------------------------------------------------ #
    @property
    def power_symbol_nets(self) -> set:
        return set(self.sch.power_nets) if self.sch else set()

    @property
    def project_dir(self) -> str:
        return os.path.dirname(self.board.path)

    def _merge_schematic_fields(self) -> None:
        if not self.sch:
            return
        for fp in self.board.footprints:
            s = self.sch.by_ref.get(fp.ref)
            if s:
                fp.fields = dict(s.fields)
                if s.dnp:
                    fp.dnp = True

    def _apply_net_overrides(self) -> None:
        for name, ov in (self.cfg.get("nets") or {}).items():
            info = self._net_classes.get(name)
            if info is None:
                self.warnings.append("config refers to unknown net '%s'" % name)
                continue
            if "kind" in ov:
                info["kind"] = ov["kind"]
                info["reason"] = "set in config"

    # ------------------------------------------------------------------ #
    def net_kind(self, net: str) -> str:
        return self._net_classes.get(net, {}).get("kind", "signal")

    def nets_of_kind(self, kind: str) -> List[str]:
        return sorted([n for n, i in self._net_classes.items() if i["kind"] == kind], key=C.natural_key)

    def net_spec(self, net: str) -> Dict[str, Any]:
        """Resolved expectations for a net: kind, nominal, min, max and where each came from."""
        info = self._net_classes.get(net, {"kind": "signal", "reason": "unknown net", "voltage": None})
        ov = (self.cfg.get("nets") or {}).get(net, {})
        tol_default = float(self.cfg["testplan"]["default_tolerance_pct"])
        out: Dict[str, Any] = {"net": net, "kind": info["kind"], "kind_reason": info["reason"]}
        nominal = ov.get("nominal", info.get("voltage"))
        src = "config" if "nominal" in ov else ("net name" if info.get("voltage") is not None else None)
        tol = float(ov.get("tolerance_pct", tol_default))
        vmin, vmax = ov.get("min"), ov.get("max")
        if nominal is not None:
            nominal = float(nominal)
            band = abs(nominal) * tol / 100.0
            if info["kind"] == "ground" or nominal == 0:
                band = max(band, 0.05)
            if vmin is None:
                vmin = nominal - band
            if vmax is None:
                vmax = nominal + band
        out.update({"nominal_v": nominal, "min_v": None if vmin is None else round(float(vmin), 4),
                    "max_v": None if vmax is None else round(float(vmax), 4), "tolerance_pct": tol,
                    "voltage_source": src or ("config" if ("min" in ov or "max" in ov) else None)})
        for k in ("max_current_a", "test", "note", "ripple_mv", "power_on_only"):
            if k in ov:
                out[k] = ov[k]
        return out

    def supply(self) -> Dict[str, Any]:
        """Which net powers the DUT during the powered test and at what voltage."""
        s = self.cfg.get("supply") or {}
        if s.get("net"):
            spec = self.net_spec(s["net"])
            v = s.get("voltage") if s.get("voltage") is not None else spec.get("nominal_v")
            return {"net": s["net"], "voltage": v, "reason": "set in config", "current_limit_a": s.get("current_limit_a")}
        # auto: the highest positive rail that reaches a connector pin
        best: Optional[Tuple[float, str, str]] = None
        connectors = [fp for fp in self.board.footprints if C.is_connector(fp)]
        for net in self.nets_of_kind("power"):
            spec = self.net_spec(net)
            v = spec.get("nominal_v")
            on_conn = [fp.ref for fp in connectors if net in fp.nets()]
            if not on_conn:
                continue
            score = (v if v is not None else 0.0) + (100 if C.base_net(net).upper().startswith(("VIN", "VBUS", "+VIN")) else 0)
            if best is None or score > best[0]:
                best = (score, net, "highest rail on connector %s" % ", ".join(on_conn[:3]))
        if best:
            spec = self.net_spec(best[1])
            return {"net": best[1], "voltage": spec.get("nominal_v"), "reason": best[2],
                    "current_limit_a": s.get("current_limit_a")}
        return {"net": None, "voltage": None, "reason": "no power net reaches a connector; set supply.net",
                "current_limit_a": s.get("current_limit_a")}

    # ------------------------------------------------------------------ #
    # Technology classes (pcbtestkit.classsys)
    # ------------------------------------------------------------------ #
    def class_systems(self) -> Dict[str, Any]:
        """All class systems: built-in, ~/.pcbtestkit/classsystems, <project>/classsystems, config."""
        if self._systems is None:
            from .classsys import load_systems
            self._systems, warns = load_systems(self.cfg, self.project_dir)
            for w in warns:
                if w not in self.warnings:
                    self.warnings.append(w)
        return self._systems

    def class_system(self, sid: Optional[str] = None):
        from .classsys import SystemError_, system_id_from_config
        sid = sid or system_id_from_config(self.cfg)
        s = self.class_systems().get(sid)
        if s is None:
            import difflib
            raise SystemError_("unknown class system %r; known: %s%s" % (
                sid, ", ".join(sorted(self.class_systems())),
                "" if not difflib.get_close_matches(sid, list(self.class_systems()), 1) else
                " (did you mean %s?)" % difflib.get_close_matches(sid, list(self.class_systems()), 1)[0]))
        return s

    def net_voltages(self) -> Dict[str, float]:
        """Known net voltages (nominal, else the largest |limit|) for voltage-dependent spacing."""
        out: Dict[str, float] = {}
        for n in self.board.net_names():
            s = self.net_spec(n)
            v = s.get("nominal_v")
            if v is None and (s.get("min_v") is not None or s.get("max_v") is not None):
                v = max(abs(s.get("min_v") or 0.0), abs(s.get("max_v") or 0.0))
            if v is not None:
                out[n] = float(v)
        return out

    def class_context(self) -> Dict[str, Any]:
        """Board facts + tech overrides + tech.context: what conditional class rules are evaluated against."""
        from .measure import board_info
        ctx: Dict[str, Any] = dict(board_info(self.board))
        tech = self.cfg.get("tech") or {}
        if tech.get("layers"):
            ctx["layers"] = int(tech["layers"])
        if tech.get("base_copper_um"):
            ctx["copper_um"] = float(tech["base_copper_um"])
        ctx.setdefault("mask_color", "green")
        ctx["edge_process"] = "routed"
        for k, v in (tech.get("context") or {}).items():
            if v is not None:
                ctx[k] = v
        ctx["_net_voltage"] = self.net_voltages()
        return ctx

    def measurement(self) -> Dict[str, Any]:
        """Measured fab features as streams (pcbtestkit.measure); re-measured when high-voltage nets change."""
        from .measure import measure_board
        hv = tuple(sorted((n, v) for n, v in self.net_voltages().items() if abs(v) > 30))
        if self._measure_cache is None or self._measure_cache[0] != hv:
            self._measure_cache = (hv, measure_board(self.board, hv_nets=dict(hv)))
        return self._measure_cache[1]

    def class_checks(self) -> List[Tuple[str, Optional[str], bool]]:
        """(system id, class or None, is_primary) for the ordered class and every extra check."""
        from .classsys import system_id_from_config
        tech = self.cfg.get("tech") or {}
        out = [(system_id_from_config(self.cfg), tech.get("class"), True)]
        for c in tech.get("checks") or []:
            if isinstance(c, str):
                sid, _, cls = c.partition(":")
                out.append((sid.strip(), cls.strip() or None, False))
            elif isinstance(c, dict) and c.get("system"):
                out.append((c["system"], c.get("class"), False))
        return out

    def evaluate_class(self, sid: Optional[str] = None, cls: Any = None, use_default: bool = False) -> Dict[str, Any]:
        from .classsys import evaluate
        s = self.class_system(sid)
        target = cls if cls not in (None, "") else (s.defaults() if use_default else None)
        r = evaluate(s, self.measurement(), self.class_context(), target=target)
        r["finding_id"], r["category"] = s.finding_id, s.category
        return r

    def technology(self) -> Dict[str, Any]:
        """Ordered class system: required class, target compliance; plus every extra class check."""
        if self._tech_cache is not None:
            return self._tech_cache
        from .classsys import SystemError_
        primary, checks = None, []
        for sid, cls, is_primary in self.class_checks():
            try:
                r = self.evaluate_class(sid, cls, use_default=not is_primary)
            except (SystemError_, KeyError, ValueError) as e:
                msg = "class check %s%s skipped: %s" % (sid, (":" + cls) if cls else "", e)
                if msg not in self.warnings:
                    self.warnings.append(msg)
                if is_primary:
                    from .classsys import DEFAULT_SYSTEM
                    r = self.evaluate_class(DEFAULT_SYSTEM, None)
                else:
                    continue
            if is_primary:
                primary = r
            else:
                checks.append(r)
        m = self.measurement()
        ctx = self.class_context()
        tech = self.cfg.get("tech") or {}
        req_limits = None
        if not primary.get("target") and primary["required"].get("class"):
            try:
                req_limits = self.evaluate_class(primary["system"], primary["required"]["class"])["target"]["limits"]
            except (SystemError_, KeyError, ValueError):
                req_limits = None
        self._tech_cache = {
            "required_limits": req_limits,
            "system": primary["system"], "name": primary["name"], "status": primary["status"],
            "source": primary.get("source"), "required": primary["required"],
            "target": (primary.get("target") or {}).get("class"), "target_result": primary.get("target"),
            "process": primary.get("process"), "measured": primary["measured"], "primary": primary, "checks": checks,
            "layers": len(self.board.copper_layers), "target_layers": tech.get("layers"),
            "thickness_mm": self.board.thickness, "base_copper_um": ctx.get("copper_um"), "finish": ctx.get("finish"),
            "short_count": m["short_count"], "microvias": m["features"]["microvias"]}
        return self._tech_cache

    def class_overview(self, systems: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Every class system (or the listed ones): the class this board needs and whether it fits."""
        from .classsys import evaluate
        from .classsys.engine import DRIVING
        m, ctx = self.measurement(), self.class_context()
        rows = []
        for sid, s in sorted(self.class_systems().items()):
            if systems and sid not in systems:
                continue
            r = evaluate(s, m, ctx, target=None if any(d.choose == "cheapest" for d in s.dims) else s.defaults())
            req = r["required"]
            block = [v["label"] for v in ((r.get("process") or {}).get("violations") or {}).values() if v.get("severity") in DRIVING]
            if r.get("target"):
                block += [v["label"] for v in r["target"]["violations"].values() if v.get("severity") in DRIVING]
            if req.get("beyond"):
                block += sorted({d["label"] for d in req.get("drivers", [])})
            fits = req["fits"] and (r.get("target") or {}).get("ok", True)
            rows.append({"system": sid, "name": s.name, "kind": s.kind, "status": s.status, "class": req.get("class"),
                         "fits": bool(fits), "blockers": block[:4], "services": req.get("services") or []})
        return rows

    def _apply_class_limits(self) -> None:
        """The ordered class's limits replace the fab profile's for the generic MFG-* checks."""
        tech = self.cfg.get("tech") or {}
        if not tech.get("class"):
            return
        from .classsys import SystemError_, fab_limits
        try:
            s = self.class_system()
            lv = s.parse_class(tech["class"])
            fl = fab_limits(s, lv, self.class_context())
        except (SystemError_, KeyError, ValueError) as e:
            self.warnings.append("technology class %r ignored: %s" % (tech["class"], e))
            return
        fab = self.cfg["fab"]
        fab.update(fl)
        fab.update(self.cfg.get("fab_overrides") or {})
        fab["source"] = "technology class %s (%s)" % (s.format_class(lv), s.name)

    def height_of(self, fp: Footprint) -> Tuple[float, str]:
        hmap = (self.cfg.get("jig") or {}).get("heights") or {}
        if fp.ref in hmap:
            return float(hmap[fp.ref]), "config"
        from .models3d import model_height  # lazy: only when needed
        mh = model_height(fp, self.project_dir)
        if mh is not None:
            return mh, "3D model"
        return C.estimate_height(fp)

    def ignored(self, check_id: str, refs=()) -> bool:
        a = self.cfg["analysis"]
        if check_id in a.get("ignore_checks", []):
            return True
        ig = set(a.get("ignore_refs", []))
        return bool(refs) and all(r in ig for r in refs)

    def components(self, include_virtual: bool = False) -> List[Footprint]:
        out = []
        for fp in self.board.footprints:
            if not fp.ref or fp.ref.startswith("#"):
                continue
            if not include_virtual and fp.board_only and not fp.pads:
                continue
            out.append(fp)
        return out
