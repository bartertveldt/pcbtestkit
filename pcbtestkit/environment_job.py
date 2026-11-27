"""A test environment build as a background job, so KiCad stays usable while 100+ boards are worked on.

    python -m pcbtestkit.environment_job <request.json> [--no-window]

The request (written by the KiCad dialog, see plugin/environment_dialog.py) says what to do:
    {"action": "build" | "plan" | "add" | "upgrade", "boards": [...], "env_dir": "..." (add, upgrade), "changes": [...], "out": "...", "spec": {...} | null, "spec_file": "..." | null,
     "environment": "..." | null, "stations": [...], "new_stations": true, "jobs": 0, "visuals": true,
     "route": true, "video": "swap", "video_boards": 12,
     "power": {"after": "none" | "sleep" | "hibernate" | "shutdown", "keep_awake": true, "lid": false, "grace_s": 120}}
One process does it all (its worker processes do the boards). It shows one small window with the progress, a Cancel
button and, when done, "Open environment page"; without wxPython or a display it prints the progress to its log.
Status for other programs: <out>/environment-job.json (state, message, fraction, pid); its output:
<out>/environment-job.log. A cancelled build keeps what it made: the next build reuses every board that is done.
"power" (see pcbtestkit.power) keeps the computer awake while it runs, also with the lid closed, and sleeps, hibernates
or shuts it down once the build has finished or failed; a watcher process of its own does that, so a crash counts too.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS, LOG, REQUEST = "environment-job.json", "environment-job.log", "environment-job-request.json"


def status(out_dir: str) -> Optional[Dict[str, Any]]:
    try:
        with open(os.path.join(out_dir, STATUS), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def running(out_dir: str) -> bool:
    from .video_job import _alive
    st = status(out_dir)
    return bool(st and st.get("state") == "running" and _alive(int(st.get("pid") or 0)))


class Tracker:
    """Writes the job's status file (at most once a second) and tells the window."""

    def __init__(self, out_dir: str, on_update=None):
        self.path = os.path.join(out_dir, STATUS)
        os.makedirs(out_dir, exist_ok=True)
        self.on_update = on_update
        self.t0 = time.time()
        self.st: Dict[str, Any] = {"state": "running", "pid": os.getpid(), "message": "Starting", "fraction": 0.0,
                                   "started": time.strftime("%Y-%m-%d %H:%M:%S")}
        self._last = 0.0
        self.write(True)

    def update(self, msg: str, frac: float) -> None:
        self.st.update(message=msg, fraction=round(float(frac), 4), elapsed_s=int(time.time() - self.t0))
        self.write()

    def finish(self, state: str, message: str, summary: Optional[Dict[str, Any]] = None) -> None:
        self.st.update(state=state, message=message, fraction=1.0 if state == "done" else self.st.get("fraction", 0.0),
                       seconds=round(time.time() - self.t0, 1))
        if summary:
            self.st["summary"] = summary
        self.write(True)

    def write(self, force: bool = False) -> None:
        now = time.time()
        if force or now - self._last > 1.0:
            self._last = now
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.st, fh)
            os.replace(tmp, self.path)
        if self.on_update:
            self.on_update(dict(self.st))


def run(req: Dict[str, Any], tracker: Tracker) -> Dict[str, Any]:
    """Plan or build the environment the request asks for. Returns its summary."""
    from . import environment as E
    spec = E.load_spec(req.get("spec_file") or None, req.get("spec") or {})
    out = req["out"]
    common = dict(jobs=int(req.get("jobs") or 0), visuals=bool(req.get("visuals", True)), route=bool(req.get("route", True)),
                  video=str(req.get("video", "swap")), video_boards=int(req.get("video_boards") or 8),
                  resume=bool(req.get("resume", True)), progress=tracker.update)
    if req.get("action") == "add":
        return E.add_boards(req["env_dir"], req.get("boards") or [], req.get("spec_over") or {},
                            new_stations=bool(req.get("new_stations", True)), **common)["summary"]
    if req.get("action") == "upgrade":
        return E.upgrade_environment(req["env_dir"], req.get("changes") or [], req.get("spec_over") or {}, **common)["summary"]
    if req.get("action") == "plan":
        env = E.plan_environment(req["boards"], spec, req.get("environment"), req.get("stations") or [],
                                 bool(req.get("new_stations", True)), int(req.get("jobs") or 0), out, progress=tracker.update)
        env.pop("_facilities", None)
        E.write_docs(env, out)
    else:
        env = E.build_environment(req["boards"], out, spec, req.get("environment"), req.get("stations") or [],
                                  bool(req.get("new_stations", True)), int(req.get("jobs") or 0),
                                  visuals=bool(req.get("visuals", True)), route=bool(req.get("route", True)),
                                  video=str(req.get("video", "swap")), video_boards=int(req.get("video_boards") or 8),
                                  resume=bool(req.get("resume", True)), progress=tracker.update)
    return env["summary"]


def _line(summary: Dict[str, Any]) -> str:
    return "%d of %d boards fit (%s%%) on %d station(s); %d do not fit, %d could not be read." % (
        summary["compatible"], summary["boards"], summary["compatible_pct"], summary["stations"], summary["incompatible"],
        summary["errors"])


def _headless(req: Dict[str, Any]) -> int:
    last = [""]

    def show(st):
        if st.get("message") != last[0]:
            last[0] = st.get("message", "")
            print(time.strftime("%H:%M:%S"), "[%3d%%]" % int(100 * st.get("fraction", 0)), last[0], flush=True)
    tr = Tracker(req["out"], show)
    try:
        summ = run(req, tr)
    except Exception as exc:  # noqa: BLE001 - the status file must say what happened
        tr.finish("failed", "Not finished: %r" % exc)
        raise
    tr.finish("done", _line(summ), summ)
    return 0


def _kill_group() -> None:
    """Stop this job and its worker processes (Cancel)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(os.getpid())], capture_output=True, creationflags=0x08000000)
    else:
        try:
            os.killpg(os.getpgid(0), signal.SIGTERM)
        except OSError:
            os._exit(1)


def _window(req: Dict[str, Any]) -> int:
    import wx

    app = wx.App(False)
    n = len(req.get("boards") or [])
    frame = wx.Frame(None, title="PCB TestKit - test environment (%s)" % ("%d sources" % n if n != 1 else "1 source"),
                     size=(600, 210), style=wx.DEFAULT_FRAME_STYLE & ~(wx.RESIZE_BORDER | wx.MAXIMIZE_BOX))
    p = wx.Panel(frame)
    s = wx.BoxSizer(wx.VERTICAL)
    head = wx.StaticText(p, label=("Planning" if req.get("action") == "plan" else "Building") +
                         " the test environment. You can keep working in KiCad.")
    gauge = wx.Gauge(p, range=1000, size=(560, 18))
    line1 = wx.StaticText(p, label="Starting...")
    line2 = wx.StaticText(p, label="")
    from . import power as PW
    try:
        pcfg = PW.normalize(req.get("power"))
    except ValueError:
        pcfg = PW.normalize(None)
    line3 = wx.StaticText(p, label=("This " + PW.describe(pcfg) + ".") if PW.active(pcfg) else "")
    btns = wx.BoxSizer(wx.HORIZONTAL)
    b_stay = wx.Button(p, label="Stay on")
    b_stay.SetToolTip("Keep the computer on after this run")
    b_stay.Show(pcfg["after"] != "none")
    btns.Add(b_stay, 0, wx.RIGHT, 8)
    b_cancel = wx.Button(p, label="Cancel")
    b_open = wx.Button(p, label="Open environment page")
    b_folder = wx.Button(p, label="Open folder")
    b_open.Hide()
    b_folder.Hide()
    btns.Add(b_folder, 0, wx.RIGHT, 8)
    btns.Add(b_open, 0, wx.RIGHT, 8)
    btns.Add(b_cancel)
    for w_ in (head, gauge, line1, line2, line3):
        s.Add(w_, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, 10)
    s.Add(btns, 0, wx.ALL | wx.ALIGN_RIGHT, 10)
    p.SetSizer(s)
    result: Dict[str, Any] = {}

    def show(st):
        def ui():
            if not frame:
                return
            gauge.SetValue(int(1000 * max(0.0, min(1.0, st.get("fraction", 0.0)))))
            line1.SetLabel(st.get("message", "")[:110])
            el = int(st.get("elapsed_s") or 0)
            line2.SetLabel("%d min %02d s" % divmod(el, 60) if el else "")
        wx.CallAfter(ui)
    tr = Tracker(req["out"], show)

    def work():
        try:
            summ = run(req, tr)
            result.update(ok=True, summary=summ)
            tr.finish("done", _line(summ), summ)
        except Exception as exc:  # noqa: BLE001
            result.update(ok=False, reason=repr(exc))
            tr.finish("failed", "Not finished: %r" % exc)

        def done():
            if not frame:
                return
            head.SetLabel("Done." if result.get("ok") else "The test environment was not finished.")
            line1.SetLabel(tr.st.get("message", "")[:110])
            line2.SetLabel("" if result.get("ok") else "Details: %s" % LOG)
            if result.get("ok"):
                gauge.SetValue(1000)
                b_open.Show()
            b_folder.Show()
            b_cancel.SetLabel("Close")
            p.Layout()
            frame.RequestUserAttention()
            if pcfg["after"] != "none":
                timer.Start(500)
        wx.CallAfter(done)

    timer = wx.Timer(frame)

    def power_tick(_evt):
        ps = PW.status(req["out"]) or {}
        if not frame or ps.get("job_pid") != os.getpid():
            return
        state = ps.get("state")
        if state == "countdown":
            left = max(0, int(round(float(ps.get("at") or 0) - time.time())))
            line3.SetLabel("The computer will %s in %d s: Stay on keeps it on." % (
                PW.WILL.get(ps.get("action") or "sleep", "go to sleep"), left))
        elif state in ("kept-on", "done", "acting", "failed"):
            line3.SetLabel({"kept-on": "The computer stays on.", "failed": "The computer could not be put to sleep or "
                            "shut down: see %s." % PW.STATUS}.get(state, "The computer will %s now." % PW.WILL.get(
                                ps.get("action") or "sleep", "go to sleep")))
            b_stay.Hide()
            p.Layout()
            timer.Stop()

    def on_stay(_evt):
        PW.stay_on(req["out"], 0.0)
        line3.SetLabel("The computer stays on.")
        b_stay.Hide()
        p.Layout()

    frame.Bind(wx.EVT_TIMER, power_tick, timer)
    b_stay.Bind(wx.EVT_BUTTON, on_stay)

    def opener(target):
        if sys.platform == "darwin":
            subprocess.Popen(["open", target])
        elif os.name == "nt":
            os.startfile(target)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", target])

    def on_cancel(_evt):
        if b_cancel.GetLabel() == "Close":
            frame.Destroy()
            return
        if wx.MessageBox("Stop the build? What is done is kept: the next build goes on from there.\n\nThe computer "
                         "is not put to sleep or shut down after a cancelled build.", "PCB TestKit",
                         wx.YES_NO | wx.ICON_QUESTION) == wx.YES:
            tr.finish("cancelled", "Cancelled: the boards that are done are kept, the next build reuses them.")
            _kill_group()

    b_cancel.Bind(wx.EVT_BUTTON, on_cancel)
    b_open.Bind(wx.EVT_BUTTON, lambda e: opener(os.path.join(req["out"], "index.html")))
    b_folder.Bind(wx.EVT_BUTTON, lambda e: opener(req["out"]))
    threading.Thread(target=work, daemon=True).start()
    frame.Show()
    app.MainLoop()
    return 0 if result.get("ok") else 1


def launch(req: Dict[str, Any], window: bool = True, python: Optional[str] = None) -> Dict[str, Any]:
    """Write the request into its output folder and start the job in the background: {"ok", "pid", "log",
    "window", "power", "watcher"} or the reason it did not start. KiCad's Python (with wxPython) shows the progress
    window; with a "power" setting (by default: keep the computer awake) a watcher process looks after the power."""
    from . import power as PW
    out = os.path.abspath(req["out"])
    if running(out):
        return {"ok": False, "reason": "a test environment job for this folder is already running", "running": True}
    try:
        pcfg = PW.normalize(req.get("power"))
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}
    os.makedirs(out, exist_ok=True)
    if (PW.status(out) or {}).get("state") in ("watching", "countdown"):    # an earlier run's countdown: not now
        PW.stay_on(out)
    req = dict(req, out=out, power=pcfg)
    with open(os.path.join(out, REQUEST), "w", encoding="utf-8") as fh:
        json.dump(req, fh, indent=1)
    from .video import find_wx_python
    py = python or (find_wx_python() if window else None)
    use_window = bool(window and py)
    py = py or (sys.executable if os.path.basename(sys.executable).lower().startswith("python") else None)
    if not py:
        from .video import find_python
        py = find_python()
    if not py:
        return {"ok": False, "reason": "no Python found to run the job"}
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.dirname(HERE) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    cmd = [py, "-m", "pcbtestkit.environment_job", os.path.join(out, REQUEST)]
    if not use_window:
        cmd.append("--no-window")
    kw: Dict[str, Any] = {}
    if os.name == "nt":
        kw["creationflags"] = (0x08000000 if use_window else 0x00000010) | 0x00000200
    else:
        kw["start_new_session"] = True
    log = os.path.join(out, LOG)
    fh = open(log, "w", encoding="utf-8")
    try:
        p = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, cwd=out, **kw)
    except OSError as exc:
        fh.close()
        return {"ok": False, "reason": "could not start %s: %s" % (os.path.basename(py), exc)}
    fh.close()
    with open(os.path.join(out, STATUS + ".tmp"), "w", encoding="utf-8") as sf:
        json.dump({"state": "running", "pid": p.pid, "message": "Starting", "fraction": 0.0,
                   "started": time.strftime("%Y-%m-%d %H:%M:%S")}, sf)
    os.replace(os.path.join(out, STATUS + ".tmp"), os.path.join(out, STATUS))
    watcher = PW.launch_watcher(out, p.pid, py) if PW.active(pcfg) else None
    return {"ok": True, "pid": p.pid, "log": log, "window": use_window, "watcher": watcher,
            "power": PW.describe(pcfg) if PW.active(pcfg) else None,
            "power_warning": None if watcher or not PW.active(pcfg) else "the power watcher did not start"}


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m pcbtestkit.environment_job",
                                 description="Plan or build a test environment in the background, with a progress window.")
    ap.add_argument("request", help="the job's request file (JSON)")
    ap.add_argument("--no-window", action="store_true", help="print the progress instead of showing a window")
    a = ap.parse_args(argv)
    with open(a.request, encoding="utf-8") as fh:
        req = json.load(fh)
    if not a.no_window:
        try:
            import wx  # noqa: F401
            return _window(req)
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001 - no display: the log instead
            print("no progress window (%r); progress follows" % exc, flush=True)
    return _headless(req)


if __name__ == "__main__":
    sys.exit(main())
