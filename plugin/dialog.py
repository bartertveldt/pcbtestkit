"""wxPython dialog used inside KiCad's PCB editor (KiCad 6-10, SWIG plugin API)."""
from __future__ import annotations

import json
import os
import sys
import traceback
import webbrowser

import wx
import wx.grid

from ..pcbtestkit import __version__
from ..pcbtestkit import classify as C
from ..pcbtestkit.config import FAB_PROFILES, POGO_PROFILES, load_user_config, save_user_config
from ..pcbtestkit.project import Design

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class MainDialog(wx.Dialog):
    def __init__(self, parent, board_path: str):
        super().__init__(parent, title="PCB TestKit %s" % __version__, size=(860, 760),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.board_path = board_path
        self.problems = []
        self.design = Design.load(board_path)
        self.user = dict(self.design.user_config)
        cfg = self.design.cfg
        root = wx.BoxSizer(wx.VERTICAL)
        b = self.design.board
        bb = b.outline_bbox()
        info = wx.StaticText(self, label="%s - %.1f x %.1f mm, %d layers, %d parts.  Uses the SAVED board file; save first."
                             % (b.name, bb.w, bb.h, len(b.copper_layers), len(self.design.components())))
        root.Add(info, 0, wx.ALL, 8)
        nb = wx.Notebook(self)
        nb.AddPage(self._general(nb, cfg), "Outputs")
        nb.AddPage(self._testing(nb, cfg), "Test && fixture")
        nb.AddPage(self._rails(nb), "Rails")
        nb.AddPage(self._stacks(nb), "Stacked boards")
        nb.AddPage(self._jig(nb, cfg), "Solder jig")
        nb.AddPage(self._weak(nb), "Weak points")
        nb.AddPage(self._ai(nb), "AI assistant (optional)")
        root.Add(nb, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        btns = wx.BoxSizer(wx.HORIZONTAL)
        save = wx.Button(self, label="Save settings")
        run = wx.Button(self, label="Generate")
        close = wx.Button(self, wx.ID_CANCEL, "Close")
        run.SetDefault()
        save.Bind(wx.EVT_BUTTON, lambda e: self.save_settings(True))
        run.Bind(wx.EVT_BUTTON, self.on_generate)
        btns.Add(save, 0, wx.RIGHT, 6)
        btns.AddStretchSpacer()
        btns.Add(run, 0, wx.RIGHT, 6)
        btns.Add(close, 0)
        root.Add(btns, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(root)

    # ------------------------------------------------------------------ #
    def _general(self, nb, cfg):
        p = wx.Panel(nb)
        s = wx.BoxSizer(wx.VERTICAL)
        g = wx.FlexGridSizer(0, 2, 6, 8)
        g.AddGrowableCol(1)
        g.Add(wx.StaticText(p, label="Output folder"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.out_dir = wx.DirPickerCtrl(p, path=os.path.join(self.design.project_dir, "production_test"))
        g.Add(self.out_dir, 1, wx.EXPAND)
        g.Add(wx.StaticText(p, label="Fab capability profile"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.fab = wx.Choice(p, choices=sorted(FAB_PROFILES))
        self.fab.SetStringSelection(cfg.get("fab_profile", "standard"))
        g.Add(self.fab)
        from ..pcbtestkit.classsys import system_id_from_config
        systems = self.design.class_systems()
        self.order_systems = [sid for sid, sy in sorted(systems.items()) if sy.kind != "standard"]
        self.std_systems = [sid for sid, sy in sorted(systems.items()) if sy.kind == "standard"]
        g.Add(wx.StaticText(p, label="Class system (fab / table)"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.csys = wx.Choice(p, choices=["%s - %s" % (sid, systems[sid].name) for sid in self.order_systems])
        cur_sys = system_id_from_config(cfg)
        if cur_sys in self.order_systems:
            self.csys.SetSelection(self.order_systems.index(cur_sys))
        g.Add(self.csys, 1, wx.EXPAND)
        g.Add(wx.StaticText(p, label="Class to order"), 0, wx.ALIGN_CENTER_VERTICAL)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.tech = wx.Choice(p, choices=[])
        row.Add(self.tech, 0, wx.RIGHT, 8)
        self.tech_info = wx.StaticText(p, label="")
        row.Add(self.tech_info, 1, wx.ALIGN_CENTER_VERTICAL)
        g.Add(row, 1, wx.EXPAND)
        g.Add(wx.StaticText(p, label="Also check"), 0, wx.ALIGN_TOP)
        cur_checks = [str(c).split(":")[0] if isinstance(c, str) else c.get("system") for c in (cfg.get("tech") or {}).get("checks") or []]
        self.std_checks = wx.CheckListBox(p, choices=["%s (%s)" % (systems[sid].name, systems[sid].format_class(systems[sid].defaults() or {}))
                                                      for sid in self.std_systems])
        for i, sid in enumerate(self.std_systems):
            self.std_checks.Check(i, sid in cur_checks)
        g.Add(self.std_checks, 1, wx.EXPAND)
        self.csys.Bind(wx.EVT_CHOICE, lambda e: self._fill_classes(None))
        self._fill_classes((cfg.get("tech") or {}).get("class"))
        s.Add(g, 0, wx.EXPAND | wx.ALL, 10)
        box = wx.StaticBoxSizer(wx.VERTICAL, p, "Generate")
        self.outs = {}
        labels = [("spec", "Specification JSON (what a good unit is)"), ("testplan", "Test scheme JSON"),
                  ("fixture", "Pogo-pin fixture board (.kicad_pcb)"), ("jig", "Solder jig (STL + OpenSCAD)"),
                  ("report", "HTML report"), ("runner", "Raspberry Pi / MCU test runner bundle"),
                  ("guide", "Visual usage guide (HTML)"), ("visuals", "3D renders in the guide (about a minute)"),
                  ("video", "Explainer video MP4 with 3D animation (needs ffmpeg, about 10 minutes)")]
        for k, lab in labels:
            cb = wx.CheckBox(p, label=lab)
            cb.SetValue(bool(cfg["outputs"].get(k, True)))
            box.Add(cb, 0, wx.ALL, 3)
            self.outs[k] = cb
        self.kicad_checks = wx.CheckBox(p, label="Include KiCad DRC / ERC (kicad-cli)")
        self.kicad_checks.SetValue(bool(cfg["analysis"].get("run_kicad_drc", True)))
        box.Add(self.kicad_checks, 0, wx.ALL, 3)
        s.Add(box, 0, wx.EXPAND | wx.ALL, 10)
        p.SetSizer(s)
        return p

    def _testing(self, nb, cfg):
        p = wx.Panel(nb)
        s = wx.BoxSizer(wx.VERTICAL)
        tp, fx = cfg["testplan"], cfg["fixture"]
        g = wx.FlexGridSizer(0, 2, 6, 8)
        self.fix_modes = [("generate", "Generate a pogo-pin fixture board (.kicad_pcb)"),
                          ("own", "I design my own fixture (probe-point list CSV)"),
                          ("none", "No fixture - manual test with a multimeter")]
        self.fix_mode = wx.Choice(p, choices=[lab for _, lab in self.fix_modes])
        modes = [m for m, _ in self.fix_modes]
        self.fix_mode.SetSelection(modes.index(fx.get("mode", "generate")) if fx.get("mode", "generate") in modes else 0)
        self.probe_side = wx.Choice(p, choices=["auto", "bottom", "top", "both"])
        self.probe_side.SetStringSelection(tp["probe_side"])
        self.pogo = wx.Choice(p, choices=sorted(POGO_PROFILES))
        self.pogo.SetStringSelection(tp["pogo"])
        self.mount = wx.Choice(p, choices=["receptacle", "direct"])
        self.mount.SetStringSelection(tp.get("pogo_mount", "receptacle"))
        self.signals = wx.Choice(p, choices=["none", "auto", "all"])
        self.signals.SetStringSelection(tp.get("signals", "auto"))
        self.max_ch = wx.SpinCtrl(p, min=1, max=512, initial=int(tp["max_channels"]))
        self.adc = wx.TextCtrl(p, value=str(tp["adc_max_v"]))
        self.outline = wx.Choice(p, choices=["rect", "outline"])
        self.outline.SetStringSelection(fx.get("outline", "rect"))
        bc, panel = cfg.get("batch") or {}, fx.get("panel") or {}
        n0 = int(bc.get("count") or 1)
        if n0 <= 1:                                   # older projects: fixture.panel cols x rows
            n0 = int(panel.get("cols", 1)) * int(panel.get("rows", 1))
        self.batch_n = wx.SpinCtrl(p, min=1, max=48, initial=n0)
        self.batch_cols = wx.SpinCtrl(p, min=0, max=12, initial=int(bc.get("cols") or 0))
        pc = cfg.get("program") or {}
        self.prog_opts = [("same", "Copy of the board's header (its cable fits)"), ("swd10", "ARM Cortex 10-pin SWD, 1.27 mm"),
                          ("jtag20", "ARM 20-pin JTAG, 2.54 mm"), ("avr_isp6", "AVR ISP 6-pin"),
                          ("pic_icsp6", "PIC ICSP 6-pin (PICkit)"), ("pins", "1 x N pin header, labelled")]
        self.prog_conn = wx.Choice(p, choices=[lab for _, lab in self.prog_opts])
        kinds = [k for k, _ in self.prog_opts]
        self.prog_conn.SetSelection(kinds.index(pc.get("connector", "same")) if pc.get("connector", "same") in kinds else 0)
        import shlex
        self.prog_cmd = wx.TextCtrl(p, value=" ".join(shlex.quote(a) for a in pc.get("command") or []), size=(380, -1))
        self.prog_cmd.SetHint("optional, e.g. openocd -f board.cfg -c \"program fw.elf verify reset exit\"")
        try:
            from ..pcbtestkit.program import detect
            found = detect(self.design)
        except Exception:  # detection must never stop the dialog
            found = []
        self.prog_found = ", ".join("%s (%s)" % (h["ref"], h["protocol"]) for h in found) or "none found"
        self.route_fx = wx.CheckBox(p, label="tracks and vias, KiCad's default rules")
        self.route_fx.SetValue(bool(fx.get("route", True)))
        sup = self.design.supply()
        self.sup_net = wx.Choice(p, choices=[""] + self.design.nets_of_kind("power"))
        self.sup_net.SetStringSelection(sup.get("net") or "")
        self.sup_v = wx.TextCtrl(p, value="" if sup.get("voltage") is None else str(sup.get("voltage")))
        sc = cfg.get("supply") or {}
        fmt = lambda v: "" if v is None else str(v)
        self.sup_i = wx.TextCtrl(p, value=fmt(sc.get("current_limit_a")))
        self.idle_max = wx.TextCtrl(p, value=fmt(sc.get("max_idle_current_a")))
        self.idle_min = wx.TextCtrl(p, value=fmt(sc.get("min_idle_current_a")))
        for lab, ctl in [("Test fixture", self.fix_mode), ("Probe side", self.probe_side), ("Pogo pin family", self.pogo), ("Pogo mounting", self.mount),
                         ("Signal nets to probe", self.signals), ("Max ADC channels", self.max_ch),
                         ("ADC input range (V)", self.adc), ("Supply net", self.sup_net), ("Supply voltage (V)", self.sup_v),
                         ("Supply current limit (A)", self.sup_i), ("Max idle current (A, empty = no check)", self.idle_max),
                         ("Min idle current (A, empty = no check)", self.idle_min),
                         ("Fixture outline", self.outline),
                         ("Boards per batch (fixture sites, jig positions)", self.batch_n),
                         ("Batch columns (0 = automatic)", self.batch_cols),
                         ("Route the fixture board", self.route_fx),
                         ("Programming connector per board", self.prog_conn),
                         ("Programming command (test runner)", self.prog_cmd)]:
            g.Add(wx.StaticText(p, label=lab), 0, wx.ALIGN_CENTER_VERTICAL)
            g.Add(ctl)
        s.Add(g, 0, wx.ALL, 10)
        s.Add(wx.StaticText(p, label="Rail voltages and tolerances: Rails tab. Signal limits are learned from a known-good "
                                     "board by the test runner (learn mode).\nCustom functional steps can be added in "
                                     "pcbtestkit.json (testplan.custom_steps). No AI is needed for any of it.\n"
                                     "Probe side 'both' makes two fixture plates (the board between them), e.g. when a stacked "
                                     "board covers test points on one side.\n"
                                     "Programming headers on this board: %s. The fixture gets a programming connector per "
                                     "board, wired to pogo pins on the header,\nso any programmer that fits it programs the "
                                     "board on the fixture. The command (optional) is run by the test runner for each board."
                                     % self.prog_found), 0, wx.ALL, 10)
        p.SetSizer(s)
        return p

    RAIL_COLS = [("test", "Test", 44), ("net", "Rail", 130), ("nominal_v", "Nominal V", 80), ("tolerance_pct", "Tol. %", 60),
                 ("min_v", "Min V", 70), ("max_v", "Max V", 70), ("source", "From", 250)]

    def _rails(self, nb):
        from ..pcbtestkit.manual import rail_rows
        p = wx.Panel(nb)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(wx.StaticText(p, label="Expected voltage of every power rail. Edit nominal + tolerance, or min/max for a range "
                                     "(e.g. a battery). Untick Test to leave a rail out.\nOnly rows you change are saved; "
                                     "the others keep following the net names."), 0, wx.ALL, 8)
        self.rail_before = rail_rows(self.design)
        g = self.rail_grid = wx.grid.Grid(p)
        g.CreateGrid(len(self.rail_before), len(self.RAIL_COLS))
        g.HideRowLabels()
        for c, (_, lab, w) in enumerate(self.RAIL_COLS):
            g.SetColLabelValue(c, lab)
            g.SetColSize(c, w)
        fmt = lambda v: "" if v is None else ("%g" % v)
        for r, row in enumerate(self.rail_before):
            g.SetCellEditor(r, 0, wx.grid.GridCellBoolEditor())
            g.SetCellRenderer(r, 0, wx.grid.GridCellBoolRenderer())
            g.SetCellValue(r, 0, "1" if row["test"] else "")
            g.SetCellValue(r, 1, row["net"])
            g.SetReadOnly(r, 1)
            for c, k in ((2, "nominal_v"), (3, "tolerance_pct"), (4, "min_v"), (5, "max_v")):
                g.SetCellValue(r, c, fmt(row[k]))
            src = row["source"] or ""
            if row["hint"]:
                src = "unknown - suggestion %g V (%s)" % (row["hint"]["v"], row["hint"]["why"])
            g.SetCellValue(r, 6, src)
            g.SetReadOnly(r, 6)
        s.Add(g, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        b = wx.Button(p, label="Fill in the suggestions for unknown rails")

        def fill(_e):
            for r, row in enumerate(self.rail_before):
                if row["hint"] and not g.GetCellValue(r, 2).strip():
                    g.SetCellValue(r, 2, "%g" % row["hint"]["v"])
        b.Bind(wx.EVT_BUTTON, fill)
        s.Add(b, 0, wx.ALL, 8)
        p.SetSizer(s)
        return p

    STACK_COLS = [("use", "Use", 40), ("name", "Name", 90), ("refs", "On parts", 80), ("side", "Side", 60),
                  ("outline", "Outline x0, y0, x1, y1 (mm)", 180), ("height_mm", "Height mm", 70),
                  ("fitted_at_test", "Fitted at test", 85), ("note", "Note", 260)]

    def _stacks(self, nb):
        from ..pcbtestkit.manual import stack_rows
        p = wx.Panel(nb)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(wx.StaticText(p, label="Boards stacked on this one (daughter board, shield, display on headers or board-to-board "
                                     "connectors). Tick a row to use it.\nOutline = the stacked board's shadow on this board in "
                                     "KiCad coordinates (mm); height = top of the stack above this board. Empty = estimated "
                                     "from the parts.\nFitted at test: probes stay clear of it and the fixture gets a window; "
                                     "unticked: the board is tested first and the stacked board fitted after a pass."), 0, wx.ALL, 8)
        rows = stack_rows(self.design)
        g = self.stack_grid = wx.grid.Grid(p)
        g.CreateGrid(0, len(self.STACK_COLS))
        g.HideRowLabels()
        for c, (_, lab, w) in enumerate(self.STACK_COLS):
            g.SetColLabelValue(c, lab)
            g.SetColSize(c, w)
        for row in rows + [{}]:
            self._stack_add(row)
        s.Add(g, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        b = wx.Button(p, label="Add a row")
        b.Bind(wx.EVT_BUTTON, lambda e: self._stack_add({}))
        s.Add(b, 0, wx.ALL, 8)
        p.SetSizer(s)
        return p

    def _stack_add(self, row):
        g = self.stack_grid
        g.AppendRows(1)
        r = g.GetNumberRows() - 1
        for c, (k, _, _) in enumerate(self.STACK_COLS):
            if k in ("use", "fitted_at_test"):
                g.SetCellEditor(r, c, wx.grid.GridCellBoolEditor())
                g.SetCellRenderer(r, c, wx.grid.GridCellBoolRenderer())
                g.SetCellValue(r, c, "1" if row.get(k, k == "fitted_at_test") else "")
            elif k == "side":
                g.SetCellEditor(r, c, wx.grid.GridCellChoiceEditor(["top", "bottom"]))
                g.SetCellValue(r, c, row.get(k) or "top")
            else:
                g.SetCellValue(r, c, str(row.get(k) or ""))
        g.SetReadOnly(r, len(self.STACK_COLS) - 1)

    def _stack_after(self):
        g = self.stack_grid
        g.SaveEditControlValue()
        out = []
        for r in range(g.GetNumberRows()):
            row = {k: g.GetCellValue(r, c) for c, (k, _, _) in enumerate(self.STACK_COLS)}
            row["use"], row["fitted_at_test"] = row["use"] == "1", row["fitted_at_test"] == "1"
            out.append(row)
        return out

    def _rail_after(self):
        g = self.rail_grid
        g.SaveEditControlValue()
        out = []
        for r, row in enumerate(self.rail_before):
            out.append({"net": row["net"], "test": g.GetCellValue(r, 0) == "1", "nominal_v": g.GetCellValue(r, 2),
                        "tolerance_pct": g.GetCellValue(r, 3), "min_v": g.GetCellValue(r, 4), "max_v": g.GetCellValue(r, 5)})
        return out

    def _weak(self, nb):
        p = wx.Panel(nb)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(wx.StaticText(p, label="Critical and major weak points. Fix them in KiCad, or tick the ones you accept for this "
                                     "product (they are kept in the report, marked as waived)."), 0, wx.ALL, 8)
        self.weak_rows = None
        self.weak_list = wx.CheckListBox(p, choices=[])
        s.Add(self.weak_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        row = wx.BoxSizer(wx.HORIZONTAL)
        load = wx.Button(p, label="Check the board")
        row.Add(load, 0, wx.RIGHT, 8)
        row.Add(wx.StaticText(p, label="Reason for newly accepted items:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.weak_reason = wx.TextCtrl(p, value="")
        row.Add(self.weak_reason, 1)
        s.Add(row, 0, wx.EXPAND | wx.ALL, 8)

        def on_load(_e):
            from ..pcbtestkit.analysis import run_analysis
            from ..pcbtestkit.manual import waiver_rows
            wx.BeginBusyCursor()
            try:
                rows = waiver_rows(run_analysis(Design.load(self.board_path), include_kicad=False))
            finally:
                wx.EndBusyCursor()
            self.weak_rows = rows
            self.weak_list.Set(["[%s] %s  %s%s" % (r["severity"], r["id"], r["title"],
                                                   ("  (%s)" % ", ".join(r["refs"][:6])) if r["refs"] else "") for r in rows])
            for i, r in enumerate(rows):
                self.weak_list.Check(i, r["waived"])
            if not rows:
                self.weak_list.Set(["No critical or major weak points."])
        load.Bind(wx.EVT_BUTTON, on_load)
        p.SetSizer(s)
        return p

    def _jig(self, nb, cfg):
        p = wx.Panel(nb)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(wx.StaticText(p, label="Tick the parts that are hand-soldered. They get snug pockets; every other part on "
                                     "the same side gets a clearance pocket.\nParts on both sides: each side gets its own jig set "
                                     "(the second one has pockets for the parts already soldered)."), 0, wx.ALL, 8)
        self.jig_parts = [fp for fp in sorted(self.design.components(), key=lambda f: (f.side != "top", C.natural_key(f.ref)))
                          if not C.is_mounting_hole(fp)]
        self.jig_heights = dict((cfg["jig"].get("heights") or {}))
        self.jig_list = wx.CheckListBox(p, choices=[self._jig_label(fp) for fp in self.jig_parts])
        self.jig_list.SetFont(wx.Font(9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        parts = cfg["jig"].get("parts")
        for i, fp in enumerate(self.jig_parts):
            sel = (fp.ref in parts) if isinstance(parts, list) else (fp.is_tht and not fp.dnp and not C.is_testpoint(fp))
            self.jig_list.Check(i, sel)
        s.Add(self.jig_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        row = wx.BoxSizer(wx.HORIZONTAL)
        b1, b2 = wx.Button(p, label="Select all THT"), wx.Button(p, label="Clear")
        b3 = wx.Button(p, label="Set height of selected part...")
        b3.Bind(wx.EVT_BUTTON, self.on_height)
        b1.Bind(wx.EVT_BUTTON, lambda e: [self.jig_list.Check(i, fp.is_tht) for i, fp in enumerate(self.jig_parts)])
        b2.Bind(wx.EVT_BUTTON, lambda e: [self.jig_list.Check(i, False) for i in range(len(self.jig_parts))])
        row.Add(b1, 0, wx.RIGHT, 6)
        row.Add(b2, 0, wx.RIGHT, 18)
        row.Add(b3, 0)
        s.Add(row, 0, wx.ALL, 8)
        g2 = wx.FlexGridSizer(0, 2, 6, 8)
        self.jig_side_opts = [("auto", "auto (top, then bottom)"), (["top"], "top side only"), (["bottom"], "bottom side only"),
                              (["top", "bottom"], "top, then bottom"), (["bottom", "top"], "bottom, then top")]
        self.jig_sides = wx.Choice(p, choices=[lab for _, lab in self.jig_side_opts])
        cur = cfg["jig"].get("sides", "auto")
        vals = [v for v, _ in self.jig_side_opts]
        self.jig_sides.SetSelection(vals.index(cur) if cur in vals else 0)
        self.nest_opts = [("auto", "auto (second SMD side)"), ("both", "both SMD sides"), ("none", "none")]
        self.nest = wx.Choice(p, choices=[lab for _, lab in self.nest_opts])
        nv = [v for v, _ in self.nest_opts]
        self.nest.SetSelection(nv.index(cfg["jig"].get("paste_nest", "auto")) if cfg["jig"].get("paste_nest", "auto") in nv else 0)
        for lab, ctl in (("Hand-solder sides (jig sets)", self.jig_sides), ("Paste-print nest", self.nest)):
            g2.Add(wx.StaticText(p, label=lab), 0, wx.ALIGN_CENTER_VERTICAL)
            g2.Add(ctl)
        s.Add(g2, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        s.Add(wx.StaticText(p, label="Heights come from the 3D models; set the datasheet body height where a model is generic. "
                                     "The STL files are generated by the plugin itself (no AI, same file every time)."),
              0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        p.SetSizer(s)
        return p

    def _jig_label(self, fp):
        if fp.ref in self.jig_heights:
            h, src = float(self.jig_heights[fp.ref]), "set by you"
        else:
            h, src = self.design.height_of(fp)
        return "%-6s %-6s %-18s %-34s %s  h=%.1f mm (%s)" % (fp.ref, fp.side, fp.value[:18], fp.name[:34], fp.mount.upper(), h, src[:28])

    def on_height(self, _evt):
        i = self.jig_list.GetSelection()
        if i == wx.NOT_FOUND:
            wx.MessageBox("Select a part in the list first.", "PCB TestKit")
            return
        fp = self.jig_parts[i]
        cur = self.jig_heights.get(fp.ref, self.design.height_of(fp)[0])
        dlg = wx.TextEntryDialog(self, "Body height of %s above the PCB (mm, from the datasheet):" % fp.ref, "Part height",
                                 "%g" % float(cur))
        if dlg.ShowModal() == wx.ID_OK:
            try:
                h = float(dlg.GetValue().replace(",", "."))
                if not 0.1 <= h <= 80:
                    raise ValueError
                self.jig_heights[fp.ref] = round(h, 2)
                checked = self.jig_list.IsChecked(i)
                self.jig_list.SetString(i, self._jig_label(fp))
                self.jig_list.Check(i, checked)
            except ValueError:
                wx.MessageBox("Enter a height between 0.1 and 80 mm.", "PCB TestKit")
        dlg.Destroy()

    def _ai(self, nb):
        p = wx.Panel(nb)
        s = wx.BoxSizer(wx.VERTICAL)
        py = sys.executable if os.path.basename(sys.executable).lower().startswith("python") else "python3"
        proj = self.design.project_dir
        cmd = '%s -m pcbtestkit mcp --project "%s" --profile core' % (py, proj)
        self.mcp_cmd = "claude mcp add pcbtestkit --env PYTHONPATH=\"%s\" -- %s" % (ROOT, cmd)
        self.agent_cmd = 'PYTHONPATH="%s" %s -m pcbtestkit guide' % (ROOT, py)
        self.mcp_json = json.dumps({"mcpServers": {"pcbtestkit": {"command": py, "args": ["-m", "pcbtestkit", "mcp", "--project", proj,
                                                                                            "--profile", "core"],
                                                                 "env": {"PYTHONPATH": ROOT}}}}, indent=2)
        s.Add(wx.StaticText(p, label="Optional. Every setting can be made in the other tabs without AI.\n"
                                     "If you like, let your own AI model make the judgement calls (rail limits, tolerances, functional "
                                     "steps).\nIt works through validated tools; the plugin generates all files from its "
                                     "decisions. Connect any MCP client (core profile: about half the tokens of the full "
                                     "tool set).\nAgents with a shell (Claude Code, Codex, Cursor) are cheaper still: let them "
                                     "run the third command and follow it."), 0, wx.ALL, 8)
        t = wx.TextCtrl(p, value=self.mcp_cmd + "\n\n" + self.mcp_json + "\n\n" + self.agent_cmd,
                        style=wx.TE_MULTILINE | wx.TE_READONLY)
        t.SetFont(wx.Font(9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        s.Add(t, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        row = wx.BoxSizer(wx.HORIZONTAL)
        for lab, txt in (("Copy Claude Code command", self.mcp_cmd), ("Copy MCP JSON config", self.mcp_json),
                         ("Copy agent command", self.agent_cmd)):
            b = wx.Button(p, label=lab)
            b.Bind(wx.EVT_BUTTON, lambda e, x=txt: self._copy(x))
            row.Add(b, 0, wx.RIGHT, 6)
        dec = wx.Button(p, label="Show open decisions")
        dec.Bind(wx.EVT_BUTTON, self.on_decisions)
        row.Add(dec, 0)
        s.Add(row, 0, wx.ALL, 8)
        p.SetSizer(s)
        return p

    def _fill_classes(self, current):
        """Classes of the selected system + what this board needs in it."""
        sid = self.order_systems[self.csys.GetSelection()] if self.csys.GetSelection() >= 0 else None
        if not sid:
            return
        sy = self.design.class_systems()[sid]
        classes = ["(not set - use fab profile)"] + sy.all_classes(400)
        self.tech.SetItems(classes)
        try:
            cur = sy.format_class(sy.parse_class(current)) if current else None
        except ValueError:
            cur = None
        self.tech.SetStringSelection(cur if cur in classes else classes[0])
        try:
            req = self.design.evaluate_class(sid, None)["required"]
            if req.get("class"):
                txt = "this board needs at least %s%s%s" % (req["class"], (" (" + ", ".join(req["services"][:3]) + ")") if req.get("services") else "",
                                                            "" if req["fits"] else " - but breaks process limits, see report")
            else:
                txt = "this board is finer than %s supports" % sy.short
        except Exception as e:  # noqa: BLE001 - keep the dialog usable
            txt = "cannot evaluate: %s" % e
        self.tech_info.SetLabel(txt)

    # ------------------------------------------------------------------ #
    def _copy(self, text):
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(text))
            wx.TheClipboard.Close()

    def collect(self):
        u = load_user_config(self.design.config_path) if os.path.exists(self.design.config_path) else {}
        u["fab_profile"] = self.fab.GetStringSelection()
        tc = self.tech.GetStringSelection()
        tech = u.setdefault("tech", {})
        if self.csys.GetSelection() >= 0:
            tech["system"] = self.order_systems[self.csys.GetSelection()]
            tech.pop("table", None)
        tech["class"] = None if (not tc or tc.startswith("(")) else tc
        keep = [c for c in tech.get("checks") or [] if (c.split(":")[0] if isinstance(c, str) else c.get("system")) not in self.std_systems]
        old = {(c.split(":")[0] if isinstance(c, str) else c.get("system")): c for c in tech.get("checks") or []}
        tech["checks"] = keep + [old.get(sid, sid) for i, sid in enumerate(self.std_systems) if self.std_checks.IsChecked(i)]
        u["outputs"] = {k: cb.GetValue() for k, cb in self.outs.items()}
        a = u.setdefault("analysis", {})
        a["run_kicad_drc"] = a["run_kicad_erc"] = self.kicad_checks.GetValue()
        tp = u.setdefault("testplan", {})
        tp.update({"probe_side": self.probe_side.GetStringSelection(), "pogo": self.pogo.GetStringSelection(),
                   "pogo_mount": self.mount.GetStringSelection(), "signals": self.signals.GetStringSelection(),
                   "max_channels": self.max_ch.GetValue()})
        try:
            tp["adc_max_v"] = float(self.adc.GetValue())
        except ValueError:
            pass
        fx = u.setdefault("fixture", {})
        fx["mode"] = self.fix_modes[max(0, self.fix_mode.GetSelection())][0]
        fx["outline"] = self.outline.GetStringSelection()
        fx["panel"] = {"cols": 1, "rows": 1, "spacing_mm": float((fx.get("panel") or {}).get("spacing_mm", 10.0))}
        u.setdefault("batch", {}).update({"count": self.batch_n.GetValue(), "cols": self.batch_cols.GetValue()})
        fx["route"] = self.route_fx.GetValue()
        import shlex
        pr = u.setdefault("program", {})
        pr["connector"] = self.prog_opts[max(0, self.prog_conn.GetSelection())][0]
        try:
            pr["command"] = shlex.split(self.prog_cmd.GetValue())
        except ValueError:
            pass
        sup = u.setdefault("supply", {})
        if self.sup_net.GetStringSelection():
            sup["net"] = self.sup_net.GetStringSelection()
            try:
                sup["voltage"] = float(self.sup_v.GetValue())
            except ValueError:
                pass
        for key, ctl in (("current_limit_a", self.sup_i), ("max_idle_current_a", self.idle_max), ("min_idle_current_a", self.idle_min)):
            t = ctl.GetValue().strip().replace(",", ".")
            try:
                sup[key] = float(t) if t else (0.5 if key == "current_limit_a" else None)
            except ValueError:
                self.problems.append("%s: %r is not a number" % (key, t))
        from ..pcbtestkit.manual import apply_rail_edits, apply_stack_edits, apply_waivers
        self.problems += apply_rail_edits(u, self.rail_before, self._rail_after())
        if self.weak_rows:
            apply_waivers(u, self.weak_rows, [self.weak_list.IsChecked(i) for i in range(len(self.weak_rows))],
                          self.weak_reason.GetValue())
        self.problems += apply_stack_edits(u, self._stack_after(), set(self.design.board.by_ref))
        jg = u.setdefault("jig", {})
        jg["parts"] = [fp.ref for i, fp in enumerate(self.jig_parts) if self.jig_list.IsChecked(i)]
        jg["sides"] = self.jig_side_opts[max(0, self.jig_sides.GetSelection())][0]
        jg["paste_nest"] = self.nest_opts[max(0, self.nest.GetSelection())][0]
        if self.jig_heights:
            jg["heights"] = dict(self.jig_heights)
        return u

    def save_settings(self, notify=False):
        self.problems = []
        u = self.collect()
        if self.problems:
            wx.MessageBox("Not saved:\n\n" + "\n".join(self.problems), "PCB TestKit", wx.ICON_WARNING)
            return False
        save_user_config(self.design.config_path, u)
        self.design.reload_config(u)
        if notify:
            wx.MessageBox("Saved to %s" % self.design.config_path, "PCB TestKit")
        return True

    def on_decisions(self, _evt):
        from ..pcbtestkit.agent import Workspace
        self.save_settings()
        ws = Workspace(self.board_path, persist=False)
        r = ws.call("get_open_decisions")
        lines = ["%s [%s] %s (%d items)" % (d["id"], d["priority"], d["question"], len(d["items"])) for d in r.get("decisions", [])]
        wx.MessageBox("\n\n".join(lines) or "No open decisions.", "Open decisions")

    def on_generate(self, _evt):
        from ..pcbtestkit.pipeline import run_pipeline
        if not self.save_settings():
            return
        prog = wx.ProgressDialog("PCB TestKit", "Starting...", 100, self, wx.PD_APP_MODAL | wx.PD_AUTO_HIDE)
        try:
            d = Design.load(self.board_path)
            res = run_pipeline(d, out_dir=self.out_dir.GetPath(),
                               progress=lambda m, f: prog.Update(min(99, int(f * 100)), m),
                               include_kicad=self.kicad_checks.GetValue())
        except Exception:
            prog.Destroy()
            wx.MessageBox(traceback.format_exc(), "PCB TestKit - error", wx.ICON_ERROR)
            return
        prog.Destroy()
        s = res["summary"]
        if res["paths"].get("fixture"):
            rt = res.get("fixture_routing") or {}
            left = rt.get("unrouted_connections")
            if rt and not left:
                nxt = "Fixture board: %s is routed. Open it in KiCad, run DRC and order it." % os.path.basename(res["paths"]["fixture"])
            elif rt:
                nxt = ("Fixture board: the router left %d of %d connection(s) unrouted in %s. Route them in KiCad, run DRC "
                       "and order it." % (left, rt.get("connections", 0), os.path.basename(res["paths"]["fixture"])))
            else:
                nxt = "Fixture board: open %s in KiCad, route it and order it." % os.path.basename(res["paths"]["fixture"])
        elif res["paths"].get("probe_points"):
            nxt = "Your fixture: the probe points are in %s." % os.path.basename(res["paths"]["probe_points"])
        else:
            nxt = "No fixture: the guide lists the test points and limits for a manual test."
        msg = ("%s - score %d/100\n%s\n\nTest coverage: %s%% of nets\n\nFiles written to:\n%s\n\n%s\n\nOpen the HTML report now?") % (
            s["verdict"].upper(), s["score"], ", ".join("%d %s" % (v, k) for k, v in s["counts"].items() if v),
            res["coverage"]["percent"], res["out_dir"], nxt)
        if wx.MessageBox(msg, "PCB TestKit", wx.YES_NO | wx.ICON_INFORMATION) == wx.YES and res["paths"].get("report"):
            webbrowser.open("file://" + res["paths"]["report"])
