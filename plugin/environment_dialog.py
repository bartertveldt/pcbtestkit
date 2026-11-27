"""The KiCad dialog for a test environment over many boards (no command line needed).

Pick the KiCad projects (a folder is searched for every project in it), optionally an existing test environment or a
station board of your own, the output folder and a few settings; Plan shows which stations and which board where in
a minute or two, Build makes everything (stations, every board's fixture, guide and jigs, the swap videos) in a
background job with a progress window, so KiCad stays usable. For long runs (100+ boards with every video takes
hours) the job can keep the computer awake, also with the lid closed, and put it to sleep or shut it down when done.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import wx

from ..pcbtestkit import __version__
from ..pcbtestkit import environment as E


def _open(path: str) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", path])


class EnvironmentDialog(wx.Dialog):
    def __init__(self, parent, start_board: str = ""):
        super().__init__(parent, title="PCB TestKit %s - test environment for many boards" % __version__, size=(820, 860),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.boards: list = []
        spec = E.default_spec()
        root = wx.BoxSizer(wx.VERTICAL)
        intro = wx.StaticText(self, label=(
            "Every board gets a fixture for the same test station: the station is designed from what all boards need "
            "(analog inputs, supplies, CAN, UART, I2C, USB, Ethernet, RS-485, plate size), up to the number of stations "
            "you allow. Boards that cannot fit are listed with the reason."))
        intro.Wrap(780)
        root.Add(intro, 0, wx.ALL, 10)

        # ---- 1 the boards ----------------------------------------------------------------------------
        box = wx.StaticBoxSizer(wx.VERTICAL, self, "1  The boards (KiCad projects)")
        self.list = wx.ListBox(box.GetStaticBox(), style=wx.LB_EXTENDED, size=(-1, 170))
        row = wx.BoxSizer(wx.HORIZONTAL)
        b_dir = wx.Button(box.GetStaticBox(), label="Add a folder (all projects in it)...")
        b_files = wx.Button(box.GetStaticBox(), label="Add projects...")
        b_del = wx.Button(box.GetStaticBox(), label="Remove")
        b_clear = wx.Button(box.GetStaticBox(), label="Clear")
        for b in (b_dir, b_files, b_del, b_clear):
            row.Add(b, 0, wx.RIGHT, 6)
        self.count = wx.StaticText(box.GetStaticBox(), label="")
        box.Add(self.list, 1, wx.EXPAND | wx.ALL, 6)
        box.Add(row, 0, wx.LEFT | wx.BOTTOM, 6)
        box.Add(self.count, 0, wx.LEFT | wx.BOTTOM, 6)
        root.Add(box, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        b_dir.Bind(wx.EVT_BUTTON, self.on_add_dir)
        b_files.Bind(wx.EVT_BUTTON, self.on_add_files)
        b_del.Bind(wx.EVT_BUTTON, self.on_remove)
        b_clear.Bind(wx.EVT_BUTTON, lambda e: self._set_boards([]))

        # ---- 2 the stations --------------------------------------------------------------------------
        box2 = wx.StaticBoxSizer(wx.VERTICAL, self, "2  The test station(s)")
        sb = box2.GetStaticBox()
        self.mode_new = wx.RadioButton(sb, label="Design new stations for these boards", style=wx.RB_GROUP)
        self.mode_env = wx.RadioButton(sb, label="Use an existing test environment (a folder an earlier build made):")
        self.env_dir = wx.DirPickerCtrl(sb, message="The existing test environment folder")
        self.mode_st = wx.RadioButton(sb, label="Use a station board of my own (.kicad_pcb or facility.json):")
        self.st_file = wx.FilePickerCtrl(sb, message="The station board",
                                         wildcard="Station (*.kicad_pcb;*.json)|*.kicad_pcb;*.json")
        self.extra = wx.CheckBox(sb, label="Design extra stations for the boards it cannot take")
        self.extra.SetValue(True)
        self.into = wx.CheckBox(sb, label="Add the boards into that environment (same folder: its stations, boards and "
                                          "videos stay, the new boards are added)")
        self.into.SetValue(True)
        g = wx.FlexGridSizer(0, 2, 6, 10)
        g.AddGrowableCol(1)
        g.Add(self.mode_new, 0, wx.ALIGN_CENTER_VERTICAL)
        g.Add((1, 1))
        g.Add(self.mode_env, 0, wx.ALIGN_CENTER_VERTICAL)
        g.Add(self.env_dir, 1, wx.EXPAND)
        g.Add(self.mode_st, 0, wx.ALIGN_CENTER_VERTICAL)
        g.Add(self.st_file, 1, wx.EXPAND)
        box2.Add(g, 0, wx.EXPAND | wx.ALL, 6)
        box2.Add(self.extra, 0, wx.LEFT | wx.BOTTOM, 6)
        box2.Add(self.into, 0, wx.LEFT | wx.BOTTOM, 6)
        root.Add(box2, 0, wx.EXPAND | wx.ALL, 10)

        # ---- 3 settings ------------------------------------------------------------------------------
        box3 = wx.StaticBoxSizer(wx.VERTICAL, self, "3  Settings")
        s3 = box3.GetStaticBox()
        g3 = wx.FlexGridSizer(0, 4, 6, 10)
        self.name = wx.TextCtrl(s3, value=spec["name"], size=(220, -1))
        self.max_st = wx.SpinCtrl(s3, min=1, max=6, initial=int(spec["max_stations"]))
        self.plate_w = wx.SpinCtrl(s3, min=80, max=1200, initial=int(spec["plate_mm"][0]))
        self.plate_h = wx.SpinCtrl(s3, min=80, max=1200, initial=int(spec["plate_mm"][1]))
        self.max_batch = wx.SpinCtrl(s3, min=1, max=48, initial=int(spec["max_batch"]))
        self.layout_opts = [("auto", "auto: worth the most for what it costs"), ("general", "one main station + extras"),
                            ("specialized-2", "2 stations for groups of boards"), ("specialized-3", "3 stations for groups"),
                            ("split-can", "a CAN station + the rest"), ("split-ethernet", "an Ethernet station + the rest")]
        self.layout = wx.Choice(s3, choices=[t for _k, t in self.layout_opts])
        self.layout.SetSelection(0)
        self.layout.SetToolTip("How the stations are laid out when they are all new: compared by what the boards that "
                               "fit are worth minus what stations, programmers and fixtures cost (spec costs)")
        self.spec_file = wx.FilePickerCtrl(s3, message="Environment spec (optional)", wildcard="Spec (*.json)|*.json")
        self.prio_file = wx.FilePickerCtrl(s3, message="Priorities CSV: board,weight (optional)", wildcard="CSV (*.csv)|*.csv")
        for lab, ctl in (("Name", self.name), ("At most stations", self.max_st), ("Plate width (mm)", self.plate_w),
                         ("Plate height (mm)", self.plate_h), ("Boards per fixture, at most", self.max_batch),
                         ("Layout", self.layout),
                         ("Spec file (optional)", self.spec_file), ("Priorities (optional)", self.prio_file)):
            g3.Add(wx.StaticText(s3, label=lab), 0, wx.ALIGN_CENTER_VERTICAL)
            g3.Add(ctl, 0, wx.EXPAND)
        b_spec = wx.Button(s3, label="New spec file to edit...")
        b_spec.SetToolTip("Writes every setting (sockets, limits per resource, supplies, large plate, ...) to a JSON "
                          "file and opens it; pick it as the spec file afterwards.")
        g3.Add(b_spec, 0)
        box3.Add(g3, 0, wx.ALL, 6)
        root.Add(box3, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        b_spec.Bind(wx.EVT_BUTTON, self.on_new_spec)

        # ---- 4 outputs ---------------------------------------------------------------------------------
        box4 = wx.StaticBoxSizer(wx.VERTICAL, self, "4  Outputs")
        s4 = box4.GetStaticBox()
        r4 = wx.BoxSizer(wx.HORIZONTAL)
        r4.Add(wx.StaticText(s4, label="Folder"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.out = wx.DirPickerCtrl(s4, message="Where the test environment is made", style=wx.DIRP_USE_TEXTCTRL)
        r4.Add(self.out, 1, wx.EXPAND)
        box4.Add(r4, 0, wx.EXPAND | wx.ALL, 6)
        self.o_visuals = wx.CheckBox(s4, label="3D renders in every board's guide")
        self.o_route = wx.CheckBox(s4, label="Route every fixture board")
        self.o_swap = wx.CheckBox(s4, label="A video per station: swapping its fixtures and boards")
        self.o_boardvid = wx.CheckBox(s4, label="An explainer video per board type (KiCad 3D animation: minutes each)")
        self.o_remember = wx.CheckBox(s4, label="Remember this test environment: every future board build makes its "
                                                "fixture and jigs for it")
        for c, v in ((self.o_visuals, True), (self.o_route, True), (self.o_swap, True), (self.o_boardvid, False),
                     (self.o_remember, True)):
            c.SetValue(v)
            box4.Add(c, 0, wx.LEFT | wx.BOTTOM, 6)
        rq = wx.BoxSizer(wx.HORIZONTAL)
        self.q_opts = [("basic", "basic (flat, fastest)"), ("standard", "standard (shadows)"),
                       ("best", "best (shadows, anti-aliasing)"), ("high", "KiCad high (about 6x slower)")]
        self.img_q = wx.Choice(s4, choices=[t for _k, t in self.q_opts])
        self.img_q.SetSelection(2)
        self.vid_q = wx.Choice(s4, choices=["automatic (stations basic, boards standard)"] + [t for _k, t in self.q_opts])
        self.vid_q.SetSelection(0)
        rq.Add(wx.StaticText(s4, label="3D images"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        rq.Add(self.img_q, 0, wx.RIGHT, 16)
        rq.Add(wx.StaticText(s4, label="Videos"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        rq.Add(self.vid_q, 0)
        box4.Add(rq, 0, wx.LEFT | wx.BOTTOM, 6)
        root.Add(box4, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        # ---- 5 long runs ---------------------------------------------------------------------------------
        from ..pcbtestkit import power as PW
        from ..pcbtestkit import settings
        try:
            pw = PW.normalize(settings.power_setting())
        except ValueError:
            pw = PW.normalize(None)
        box5 = wx.StaticBoxSizer(wx.VERTICAL, self, "5  Long runs (100+ boards with every video takes hours)")
        s5 = box5.GetStaticBox()
        self.p_awake = wx.CheckBox(s5, label="Keep the computer awake while it runs")
        lid_note = {"darwin": "macOS asks for the administrator password once; on battery down to 10 %",
                    "win32": "the lid action is set to do nothing until the run ends"}.get(
                        sys.platform, "where the desktop allows it")
        self.p_lid = wx.CheckBox(s5, label="Also with the lid closed (%s)" % lid_note)
        self.p_awake.SetValue(pw["keep_awake"])
        self.p_lid.SetValue(pw["lid"])
        self.p_awake.Bind(wx.EVT_CHECKBOX, lambda e: self.p_lid.Enable(self.p_awake.GetValue()))
        self.p_lid.Enable(pw["keep_awake"])
        self.after_opts = [("none", "Leave the computer on"), ("sleep", "Put it to sleep"), ("hibernate", "Hibernate it"),
                           ("shutdown", "Shut it down")]
        self.p_after = wx.Choice(s5, choices=[t for _k, t in self.after_opts])
        self.p_after.SetSelection([k for k, _t in self.after_opts].index(pw["after"]))
        self.p_grace = pw["grace_s"]
        r5 = wx.BoxSizer(wx.HORIZONTAL)
        r5.Add(wx.StaticText(s5, label="When it has finished or failed:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        r5.Add(self.p_after, 0, wx.RIGHT, 8)
        r5.Add(wx.StaticText(s5, label="after a %s warning (not when you cancel it)" % (
            "%d-minute" % (self.p_grace // 60) if self.p_grace % 60 == 0 and self.p_grace else "%d-second" % self.p_grace)),
            0, wx.ALIGN_CENTER_VERTICAL)
        for c in (self.p_awake, self.p_lid):
            box5.Add(c, 0, wx.LEFT | wx.BOTTOM, 6)
        box5.Add(r5, 0, wx.LEFT | wx.BOTTOM, 6)
        root.Add(box5, 0, wx.EXPAND | wx.ALL, 10)

        btns = wx.BoxSizer(wx.HORIZONTAL)
        b_plan = wx.Button(self, label="Plan only")
        b_plan.SetToolTip("Which stations, which board on which, the ranking and what does not fit: a minute or two")
        b_build = wx.Button(self, label="Build everything")
        b_close = wx.Button(self, wx.ID_CANCEL, "Close")
        b_build.SetDefault()
        b_up = wx.Button(self, label="Upgrade an environment...")
        b_up.SetToolTip("What limits each board of a built environment, the upgrades with what they gain and cost, "
                        "and making one (every fixture you have still plugs in)")
        b_up.Bind(wx.EVT_BUTTON, self.on_upgrade)
        btns.Add(b_up, 0)
        btns.AddStretchSpacer()
        btns.Add(b_plan, 0, wx.RIGHT, 6)
        btns.Add(b_build, 0, wx.RIGHT, 6)
        btns.Add(b_close, 0)
        root.Add(btns, 0, wx.EXPAND | wx.ALL, 10)
        b_plan.Bind(wx.EVT_BUTTON, lambda e: self.on_run("plan"))
        b_build.Bind(wx.EVT_BUTTON, lambda e: self.on_run("build"))
        self.SetSizer(root)
        if start_board:
            d = os.path.dirname(os.path.dirname(os.path.abspath(start_board)))
            self.out.SetPath(os.path.join(d, "test_environment"))
            self._set_boards([os.path.abspath(start_board)])
        else:
            self._set_boards([])

    # ------------------------------------------------------------------ #
    def _set_boards(self, boards) -> None:
        seen, out = set(), []
        for b in boards:
            if os.path.realpath(b) not in seen:
                seen.add(os.path.realpath(b))
                out.append(b)
        self.boards = out
        self.list.Set([os.path.relpath(b, os.path.commonpath(out)) if len(out) > 1 else b for b in out])
        self.count.SetLabel("%d board%s" % (len(out), "" if len(out) == 1 else "s"))

    def on_add_dir(self, _e) -> None:
        with wx.DirDialog(self, "A folder with KiCad projects (searched with every sub-folder)") as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            with wx.BusyCursor():
                found, warns = E.discover([dlg.GetPath()])
        if not found:
            wx.MessageBox("No KiCad boards in %s." % dlg.GetPath(), "PCB TestKit")
            return
        self._set_boards(self.boards + found)
        if not self.out.GetPath():
            self.out.SetPath(os.path.join(dlg.GetPath(), "test_environment"))

    def on_add_files(self, _e) -> None:
        with wx.FileDialog(self, "KiCad projects", wildcard="KiCad (*.kicad_pro;*.kicad_pcb)|*.kicad_pro;*.kicad_pcb",
                           style=wx.FD_OPEN | wx.FD_MULTIPLE | wx.FD_FILE_MUST_EXIST) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            found, _w = E.discover(dlg.GetPaths())
        self._set_boards(self.boards + found)

    def on_remove(self, _e) -> None:
        sel = set(self.list.GetSelections())
        self._set_boards([b for i, b in enumerate(self.boards) if i not in sel])

    def on_new_spec(self, _e) -> None:
        with wx.FileDialog(self, "New environment spec", defaultFile="environment-spec.json", wildcard="Spec (*.json)|*.json",
                           style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = dlg.GetPath()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.spec_overrides(full=True), fh, indent=2)
            fh.write("\n")
        self.spec_file.SetPath(path)
        _open(path)

    # ------------------------------------------------------------------ #
    def spec_overrides(self, full: bool = False) -> dict:
        """The dialog's settings as spec values (the whole spec with ``full``)."""
        over = {"name": self.name.GetValue().strip() or "Test environment", "max_stations": self.max_st.GetValue(),
                "plate_mm": [float(self.plate_w.GetValue()), float(self.plate_h.GetValue())],
                "max_batch": self.max_batch.GetValue(),
                "layout": self.layout_opts[max(0, self.layout.GetSelection())][0],
                "visuals": {"quality": self.q_opts[max(0, self.img_q.GetSelection())][0]},
                "video_quality": (self.q_opts[self.vid_q.GetSelection() - 1][0] if self.vid_q.GetSelection() > 0 else None)}
        pf = self.prio_file.GetPath()
        if pf and os.path.exists(pf):
            import csv
            pr = {}
            with open(pf, encoding="utf-8") as fh:
                for row in csv.reader(fh):
                    try:
                        pr[row[0].strip()] = float(row[1])
                    except (IndexError, ValueError):
                        continue
            over["priorities"] = pr
        if full:
            spec = E.default_spec()
            spec.update(over)
            return spec
        return over

    def request(self, action: str) -> dict:
        """The background job's request (see pcbtestkit.environment_job)."""
        env_dir = self.env_dir.GetPath() if self.mode_env.GetValue() else None
        if action == "build" and env_dir and self.into.GetValue():      # into the environment: environment add
            return {"action": "add", "env_dir": env_dir, "out": env_dir, "boards": list(self.boards),
                    "spec_over": {k: v for k, v in self.spec_overrides().items() if k in ("visuals", "video_quality")},
                    "new_stations": bool(self.extra.GetValue()), "jobs": 0, "visuals": self.o_visuals.GetValue(),
                    "route": self.o_route.GetValue(),
                    "video": "all" if self.o_boardvid.GetValue() else ("swap" if self.o_swap.GetValue() else "none"),
                    "video_boards": 8, "resume": True, "power": self.power(), "environment": env_dir, "stations": [],
                    "spec_file": None, "spec": self.spec_overrides()}
        station = self.st_file.GetPath() if self.mode_st.GetValue() else None
        return {"action": action, "boards": list(self.boards), "out": self.out.GetPath(),
                "spec_file": self.spec_file.GetPath() or None, "spec": self.spec_overrides(),
                "environment": env_dir or None, "stations": [station] if station else [],
                "new_stations": bool(self.mode_new.GetValue() or self.extra.GetValue()),
                "jobs": 0, "visuals": self.o_visuals.GetValue(), "route": self.o_route.GetValue(),
                "video": "all" if self.o_boardvid.GetValue() else ("swap" if self.o_swap.GetValue() else "none"),
                "video_boards": 8, "resume": True, "power": self.power()}

    def power(self) -> dict:
        """The long-run power choices (see pcbtestkit.power)."""
        return {"after": self.after_opts[max(0, self.p_after.GetSelection())][0], "keep_awake": self.p_awake.GetValue(),
                "lid": bool(self.p_awake.GetValue() and self.p_lid.GetValue()), "grace_s": self.p_grace}

    def problems(self, req: dict) -> list:
        out = []
        if not req["boards"] and not req["environment"]:
            out.append("Add the boards first (a folder or projects).")
        if not req["out"] and req.get("action") != "add":
            out.append("Pick the output folder.")
        if self.mode_env.GetValue() and not (req["environment"] and os.path.exists(os.path.join(req["environment"], "environment.json"))):
            out.append("The existing environment folder must contain environment.json (made by an earlier build).")
        if self.mode_st.GetValue() and not (req["stations"] and os.path.exists(req["stations"][0])):
            out.append("Pick the station board.")
        try:
            E.load_spec(req["spec_file"], req["spec"])
        except (OSError, ValueError) as exc:
            out.append("The settings are not valid: %s" % exc)
        return out

    def on_run(self, action: str) -> None:
        req = self.request(action)
        bad = self.problems(req)
        if bad:
            wx.MessageBox("\n".join(bad), "PCB TestKit", wx.ICON_WARNING)
            return
        from ..pcbtestkit.environment_job import launch
        from ..pcbtestkit import settings
        settings.remember_power(req["power"])
        res = launch(req)
        if not res.get("ok"):
            wx.MessageBox("Could not start: %s" % res.get("reason"), "PCB TestKit", wx.ICON_ERROR)
            return
        if action == "build" and self.o_remember.GetValue():     # every later board build uses it (once it is made)
            from ..pcbtestkit import settings
            settings.remember_environment({"path": req["environment"] if req.get("environment") and not req["boards"]
                                           else req["out"]})
        wx.MessageBox("%s %d board%s in the background%s.\n\nWhen it is done, open %s.%s" % (
            "Planning" if action == "plan" else "Building the test environment for", len(req["boards"]),
            "" if len(req["boards"]) == 1 else "s",
            " (a progress window shows how far it is)" if res.get("window") else " (progress: %s)" % res.get("log"),
            os.path.join(req["out"], "index.html"),
            "\n\nThe job %s." % res["power"] if res.get("power") else ""), "PCB TestKit")

    def on_upgrade(self, _e) -> None:
        start = self.env_dir.GetPath() if self.mode_env.GetValue() else self.out.GetPath()
        with wx.DirDialog(self, "The test environment to upgrade (a folder an environment build made)",
                          defaultPath=start or "") as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = dlg.GetPath()
        if not os.path.exists(os.path.join(path, "environment.json")):
            wx.MessageBox("%s has no environment.json (made by an environment build)." % path, "PCB TestKit", wx.ICON_WARNING)
            return
        UpgradeDialog(self, path, self.power()).ShowModal()


class UpgradeDialog(wx.Dialog):
    """A built environment's upgrade options: tick the ones you want (or a path, or changes of your own), see what
    they gain and cost, then make them in the background (the stations keep every pin: your fixtures still fit)."""

    def __init__(self, parent, env_dir: str, power: dict):
        super().__init__(parent, title="Upgrade %s" % os.path.basename(env_dir.rstrip(os.sep)), size=(900, 680),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        from ..pcbtestkit import upgrades as U
        with open(os.path.join(env_dir, "environment.json"), encoding="utf-8") as fh:
            self.env = json.load(fh)
        self.env_dir, self.power = env_dir, power
        with wx.BusyCursor():
            self.opt = U.options(self.env, top=30)
        cur = self.opt["currency"]
        now = self.opt["now"]
        root = wx.BoxSizer(wx.VERTICAL)
        head = wx.StaticText(self, label=("Now %d of %d boards fit, %d boards per full load. Tick the changes you want, or "
                                          "take a path; Show what it gains, then Make it. What limits each board: "
                                          "constraints.md in the environment's folder.") % (
                                              now["boards_fit"], now["boards"], now["slots"]))
        head.Wrap(860)
        root.Add(head, 0, wx.ALL, 10)
        self.lst = wx.CheckListBox(self, choices=["%s  %s   (%+d boards, %+d per load, cost %s %.0f, net %s %.0f)%s" % (
            o["id"], o["text"], o["gain"]["boards_fit"], o["gain"]["slots"], cur, o["gain"]["cost"], cur, o["gain"]["net"],
            ("  - " + "; ".join(o["flags"])) if o["flags"] else "") for o in self.opt["options"]])
        root.Add(self.lst, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        g = wx.FlexGridSizer(0, 2, 6, 10)
        g.AddGrowableCol(1)
        self.path = wx.Choice(self, choices=["no path", "best value", "most boards", "biggest batches"])
        self.path.SetSelection(0)
        self.own = wx.TextCtrl(self, value="")
        self.own.SetHint("station-1:can+1,ethernet+1,ain+8; supply_v=48")
        self.fit = wx.TextCtrl(self, value="")
        self.fit.SetHint("boards that must fit, comma separated")
        self.budget = wx.TextCtrl(self, value="")
        self.budget.SetHint("at most (%s), optional" % cur)
        for lab, ctl in (("Path", self.path), ("Changes of your own", self.own), ("Boards that must fit", self.fit),
                         ("Budget", self.budget)):
            g.Add(wx.StaticText(self, label=lab), 0, wx.ALIGN_CENTER_VERTICAL)
            g.Add(ctl, 1, wx.EXPAND)
        root.Add(g, 0, wx.EXPAND | wx.ALL, 10)
        self.out = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 150))
        root.Add(self.out, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        btns = wx.BoxSizer(wx.HORIZONTAL)
        b_show = wx.Button(self, label="Show what it gains")
        b_make = wx.Button(self, label="Make it (rebuild in the background)")
        b_close = wx.Button(self, wx.ID_CANCEL, "Close")
        btns.AddStretchSpacer()
        for b in (b_show, b_make, b_close):
            btns.Add(b, 0, wx.LEFT, 6)
        root.Add(btns, 0, wx.EXPAND | wx.ALL, 10)
        b_show.Bind(wx.EVT_BUTTON, lambda e: self.show())
        b_make.Bind(wx.EVT_BUTTON, lambda e: self.make())
        self.SetSizer(root)
        self.goal = None

    def plan(self):
        from ..pcbtestkit import upgrades as U
        chs = [dict(self.opt["options"][i]["change"]) for i in self.lst.GetCheckedItems()]
        pk = {1: "value", 2: "fit", 3: "batch"}.get(self.path.GetSelection())
        if pk:
            chs += [dict(st["change"]) for st in (self.opt["paths"].get(pk) or {}).get("steps") or []]
        if self.own.GetValue().strip():
            chs += U.parse_changes(self.own.GetValue())
        for ch in chs:
            if ch.get("kind") == "station" and "resources" in ch:
                ch["res"] = ch.pop("resources")
        fit = [x.strip() for x in self.fit.GetValue().split(",") if x.strip()]
        budget = float(self.budget.GetValue()) if self.budget.GetValue().strip() else None
        return U.plan_goal(self.env, fit=fit, budget=budget, changes=chs)

    def show(self) -> bool:
        try:
            with wx.BusyCursor():
                g = self.plan()
        except ValueError as exc:
            self.out.SetValue("Not valid: %s" % exc)
            return False
        cur = g["currency"]
        gain = g["gain"]
        lines = ["Changes:"] + ["  %s" % c["text"] for c in g["changes"]] if g["changes"] else ["No change is needed."]
        lines.append("Gain: %+d boards that fit, %+d boards per load%s" % (
            gain["boards_fit"], gain["slots"], (" (" + ", ".join(gain["fit"][:8]) + ")") if gain["fit"] else ""))
        lines.append("Cost: %s %.0f (new parts, programmers, new fixtures for the boards whose batch grows); "
                     "worth %s %.0f; net %s %.0f" % (cur, gain["cost"], cur, gain["worth"], cur, gain["net"]))
        lines += ["Cannot: %s (batch %d): %s" % (c["board"], c["batch"], c["reason"]) for c in g["cannot"]]
        lines += ["Unknown board: %s" % u for u in g["unknown"]] + ["Note: %s" % f for f in g["flags"]]
        self.out.SetValue("\n".join(lines))
        self.goal = g
        return True

    def make(self) -> None:
        if not self.show() or not self.goal or not self.goal["changes"]:
            return
        if wx.MessageBox("Make these changes and build the environment again in the background?\n\nEvery station "
                         "keeps the pins it has, so the fixtures you have still plug in; boards whose batch grows get "
                         "a new fixture.", "PCB TestKit", wx.YES_NO | wx.ICON_QUESTION) != wx.YES:
            return
        from ..pcbtestkit.environment_job import launch
        res = launch({"action": "upgrade", "env_dir": self.env_dir, "out": self.env_dir, "boards": [],
                      "changes": [{k: v for k, v in c.items() if k != "text"} for c in self.goal["changes"]],
                      "power": self.power})
        if not res.get("ok"):
            wx.MessageBox("Could not start: %s" % res.get("reason"), "PCB TestKit", wx.ICON_ERROR)
            return
        wx.MessageBox("Upgrading in the background%s." % (" (a progress window shows how far it is)" if res.get("window")
                                                          else " (progress: %s)" % res.get("log")), "PCB TestKit")
        self.EndModal(wx.ID_OK)

