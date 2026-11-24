"""Schematic reader (.kicad_sch, KiCad 6 - 10), including hierarchical sheets.

Only what the toolkit needs is extracted: symbol fields per reference
(values, ratings, part numbers, DNP flags), power-symbol net names and the
title block. Net connectivity comes from the board, which is authoritative
for test planning.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from . import sexpr as S


@dataclass
class SchSymbol:
    lib_id: str
    refs: List[str]
    value: str
    footprint: str
    fields: Dict[str, str] = field(default_factory=dict)
    in_bom: bool = True
    on_board: bool = True
    dnp: bool = False
    is_power: bool = False
    uuid: str = ""
    sheet_file: str = ""
    pins: Dict[str, str] = field(default_factory=dict)  # pin number -> electrical type (from lib symbol)


@dataclass
class Schematic:
    root_path: str
    symbols: List[SchSymbol] = field(default_factory=list)
    by_ref: Dict[str, SchSymbol] = field(default_factory=dict)
    power_nets: Set[str] = field(default_factory=set)
    sheets: List[str] = field(default_factory=list)
    title_block: Dict[str, str] = field(default_factory=dict)
    version: int = 0
    labels: Set[str] = field(default_factory=set)
    warnings: List[str] = field(default_factory=list)

    def component(self, ref: str) -> Optional[SchSymbol]:
        return self.by_ref.get(ref)


def _props(node: list) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for c in S.children(node, "property"):
        if len(c) >= 3:
            out[str(c[1])] = str(c[2])
    return out


def _lib_symbol_info(lib_symbols: Optional[list]):
    power: Set[str] = set()
    pins: Dict[str, Dict[str, str]] = {}
    descr: Dict[str, str] = {}
    if not lib_symbols:
        return power, pins, descr
    for sym in S.children(lib_symbols, "symbol"):
        name = str(sym[1])
        if S.child(sym, "power") is not None or "power" in S.atoms(sym):
            power.add(name)
        p = _props(sym)
        if p.get("Description") or p.get("ki_description"):
            descr[name] = p.get("Description") or p.get("ki_description", "")
        pin_map: Dict[str, str] = {}

        def walk(n):
            for c in n[1:]:
                if S.is_node(c, "pin") and len(c) > 1:
                    num_node = S.child(c, "number")
                    if num_node and len(num_node) > 1:
                        pin_map[str(num_node[1])] = str(c[1])
                elif S.is_node(c, "symbol"):
                    walk(c)
        walk(sym)
        pins[name] = pin_map
    return power, pins, descr


def load_schematic(root_path: str) -> Schematic:
    sch = Schematic(root_path=os.path.abspath(root_path))
    visited: Set[str] = set()
    v6_instances: Dict[str, List[str]] = {}

    def visit(path: str):
        apath = os.path.abspath(path)
        if apath in visited:
            return
        visited.add(apath)
        if not os.path.exists(apath):
            sch.warnings.append("missing sheet file: %s" % path)
            return
        try:
            root = S.load(apath)
        except (OSError, S.SexprError) as exc:
            sch.warnings.append("could not read %s: %s" % (path, exc))
            return
        sch.sheets.append(apath)
        if apath == sch.root_path:
            sch.version = int(S.num(S.value(root, "version", 0)))
            tb = S.child(root, "title_block")
            if tb:
                for c in tb[1:]:
                    if isinstance(c, list) and len(c) >= 2:
                        key = "comment%s" % c[1] if c[0] == "comment" and len(c) >= 3 else str(c[0])
                        sch.title_block[key] = str(c[-1])
            si = S.child(root, "symbol_instances")  # KiCad 6
            if si:
                for p in S.children(si, "path"):
                    uuid = str(p[1]).rstrip("/").split("/")[-1]
                    ref = S.value(p, "reference", "")
                    if ref:
                        v6_instances.setdefault(uuid, []).append(ref)
        power_ids, lib_pins, lib_descr = _lib_symbol_info(S.child(root, "lib_symbols"))
        for lab in S.children(root, "global_label") + S.children(root, "label") + S.children(root, "hierarchical_label"):
            if len(lab) > 1:
                sch.labels.add(str(lab[1]))
        for sym in S.children(root, "symbol"):
            lib_id = S.value(sym, "lib_id", "") or ""
            props = _props(sym)
            uuid = S.value(sym, "uuid", "") or ""
            refs: List[str] = []
            inst = S.child(sym, "instances")
            if inst:
                for proj in S.children(inst, "project"):
                    for p in S.children(proj, "path"):
                        r = S.value(p, "reference", "")
                        if r and r not in refs:
                            refs.append(r)
            if not refs and uuid in v6_instances:
                refs = list(v6_instances[uuid])
            if not refs and props.get("Reference"):
                refs = [props["Reference"]]
            is_power = lib_id in power_ids or lib_id.lower().startswith("power:") or \
                any(r.startswith("#PWR") or r.startswith("#FLG") for r in refs)
            s = SchSymbol(lib_id=lib_id, refs=refs, value=props.get("Value", ""),
                          footprint=props.get("Footprint", ""), fields=props,
                          in_bom=S.value(sym, "in_bom", "yes") != "no",
                          on_board=S.value(sym, "on_board", "yes") != "no",
                          dnp=S.value(sym, "dnp", "no") == "yes", is_power=is_power, uuid=uuid,
                          sheet_file=apath, pins=dict(lib_pins.get(lib_id, {})))
            if lib_id in lib_descr and not props.get("Description"):
                s.fields["Description"] = lib_descr[lib_id]
            sch.symbols.append(s)
            if is_power:
                if s.value and s.value.upper() != "PWR_FLAG":
                    sch.power_nets.add(s.value)
                continue
            for r in refs:
                if r in sch.by_ref:  # multi-unit symbol: merge fields
                    for k, v in s.fields.items():
                        sch.by_ref[r].fields.setdefault(k, v)
                    sch.by_ref[r].pins.update(s.pins)
                else:
                    sch.by_ref[r] = s
        for sheet in S.children(root, "sheet"):
            props = _props(sheet)
            f = props.get("Sheetfile") or props.get("Sheet file") or ""
            if f:
                visit(os.path.join(os.path.dirname(apath), f))

    visit(root_path)
    return sch
