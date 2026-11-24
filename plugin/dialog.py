"""wxPython dialog used inside KiCad's PCB editor (KiCad 6-10, SWIG plugin API)."""
from __future__ import annotations

import json
import os
import sys
import traceback
import webbrowser

import wx

from ..pcbtestkit import __version__
from ..pcbtestkit import classify as C
from ..pcbtestkit.config import FAB_PROFILES, POGO_PROFILES, load_user_config, save_user_config
from ..pcbtestkit.project import Design

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class MainDialog(wx.Dialog):
    def __init__(self, parent, board_path: str):
        super().__init__(parent, title="PCB TestKit %s" % __version__, size=(800, 740),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.board_path = board_path
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
        nb.AddPage(self._jig(nb, cfg), "Solder jig")
        nb.AddPage(self._ai(nb), "AI assistant")
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
        self.probe_side = wx.Choice(p, choices=["auto", "bottom", "top"])
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
        self.cols = wx.SpinCtrl(p, min=1, max=10, initial=int((fx.get("panel") or {}).get("cols", 1)))
        self.rows = wx.SpinCtrl(p, min=1, max=10, initial=int((fx.get("panel") or {}).get("rows", 1)))
        sup = self.design.supply()
        self.sup_net = wx.Choice(p, choices=[""] + self.design.nets_of_kind("power"))
        self.sup_net.SetStringSelection(sup.get("net") or "")
        self.sup_v = wx.TextCtrl(p, value="" if sup.get("voltage") is None else str(sup.get("voltage")))
        for lab, ctl in [("Probe side", self.probe_side), ("Pogo pin family", self.pogo), ("Pogo mounting", self.mount),
                         ("Signal nets to probe", self.signals), ("Max ADC channels", self.max_ch),
                         ("ADC input range (V)", self.adc), ("Supply net", self.sup_net), ("Supply voltage (V)", self.sup_v),
                         ("Fixture outline", self.outline), ("Panel columns", self.cols), ("Panel rows", self.rows)]:
            g.Add(wx.StaticText(p, label=lab), 0, wx.ALIGN_CENTER_VERTICAL)
            g.Add(ctl)
        s.Add(g, 0, wx.ALL, 10)
        s.Add(wx.StaticText(p, label="Rail voltages, tolerances and functional steps live in pcbtestkit.json "
                                     "(edit it, or let an AI model decide them via the AI assistant tab)."), 0, wx.ALL, 10)
        p.SetSizer(s)
        return p

    def _jig(self, nb, cfg):
        p = wx.Panel(nb)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(wx.StaticText(p, label="Tick the parts that are hand-soldered. They get snug pockets; every other top-side "
                                     "part gets a clearance pocket.\nThe PCB goes top-side down into the jig."), 0, wx.ALL, 8)
        self.jig_parts = [fp for fp in sorted(self.design.components(), key=lambda f: C.natural_key(f.ref))
                          if fp.side == "top" and not C.is_mounting_hole(fp)]
        labels = []
        for fp in self.jig_parts:
            h, src = self.design.height_of(fp)
            labels.append("%-6s %-18s %-38s %s  h=%.1f mm (%s)" % (fp.ref, fp.value[:18], fp.name[:38], fp.mount.upper(), h, src[:28]))
        self.jig_list = wx.CheckListBox(p, choices=labels)
        self.jig_list.SetFont(wx.Font(9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        parts = cfg["jig"].get("parts")
        for i, fp in enumerate(self.jig_parts):
            sel = (fp.ref in parts) if isinstance(parts, list) else (fp.is_tht and not fp.dnp and not C.is_testpoint(fp))
            self.jig_list.Check(i, sel)
        s.Add(self.jig_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        row = wx.BoxSizer(wx.HORIZONTAL)
        b1, b2 = wx.Button(p, label="Select all THT"), wx.Button(p, label="Clear")
        b1.Bind(wx.EVT_BUTTON, lambda e: [self.jig_list.Check(i, fp.is_tht) for i, fp in enumerate(self.jig_parts)])
        b2.Bind(wx.EVT_BUTTON, lambda e: [self.jig_list.Check(i, False) for i in range(len(self.jig_parts))])
        row.Add(b1, 0, wx.RIGHT, 6)
        row.Add(b2, 0)
        s.Add(row, 0, wx.ALL, 8)
        p.SetSizer(s)
        return p

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
        s.Add(wx.StaticText(p, label="Let your own AI model make the judgement calls (rail limits, tolerances, functional "
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
        fx["outline"] = self.outline.GetStringSelection()
        fx["panel"] = dict(fx.get("panel") or {}, cols=self.cols.GetValue(), rows=self.rows.GetValue())
        if self.sup_net.GetStringSelection():
            sup = u.setdefault("supply", {})
            sup["net"] = self.sup_net.GetStringSelection()
            try:
                sup["voltage"] = float(self.sup_v.GetValue())
            except ValueError:
                pass
        u.setdefault("jig", {})["parts"] = [fp.ref for i, fp in enumerate(self.jig_parts) if self.jig_list.IsChecked(i)]
        return u

    def save_settings(self, notify=False):
        u = self.collect()
        save_user_config(self.design.config_path, u)
        self.design.reload_config(u)
        if notify:
            wx.MessageBox("Saved to %s" % self.design.config_path, "PCB TestKit")

    def on_decisions(self, _evt):
        from ..pcbtestkit.agent import Workspace
        self.save_settings()
        ws = Workspace(self.board_path, persist=False)
        r = ws.call("get_open_decisions")
        lines = ["%s [%s] %s (%d items)" % (d["id"], d["priority"], d["question"], len(d["items"])) for d in r.get("decisions", [])]
        wx.MessageBox("\n\n".join(lines) or "No open decisions.", "Open decisions")

    def on_generate(self, _evt):
        from ..pcbtestkit.pipeline import run_pipeline
        self.save_settings()
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
        msg = ("%s - score %d/100\n%s\n\nTest coverage: %s%% of nets\n\nFiles written to:\n%s\n\nFixture board: open %s in KiCad, "
               "route it (or run Freerouting) and order it.\n\nOpen the HTML report now?") % (
            s["verdict"].upper(), s["score"], ", ".join("%d %s" % (v, k) for k, v in s["counts"].items() if v),
            res["coverage"]["percent"], res["out_dir"], os.path.basename(res["paths"].get("fixture", "")))
        if wx.MessageBox(msg, "PCB TestKit", wx.YES_NO | wx.ICON_INFORMATION) == wx.YES and res["paths"].get("report"):
            webbrowser.open("file://" + res["paths"]["report"])
