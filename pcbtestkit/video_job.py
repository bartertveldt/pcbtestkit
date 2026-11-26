"""The explainer video as a background job, so KiCad stays usable while the frames render.

    python -m pcbtestkit.video_job <board.kicad_pcb> --out DIR [--no-window]

One process does the whole video: it renders the frames with kicad-cli (several at a time, no window per call),
draws the captions, joins the MP4 and then writes the guide and report again with the video in them. It shows one
small window with the progress (frames done, speed, time left) and a Cancel button. Without wxPython or a display
it prints the progress to its log instead. Frames of a cancelled run are kept, so the next run goes on from there.

Status for other programs: ``<out>/visuals/video-job.json`` (state, message, done, total, eta_s, pid); the output
of the job: ``<out>/visuals/video-job.log``. The KiCad dialog and ``pcbtestkit run --video --background`` start it
with :func:`launch`, using KiCad's Python (which has wxPython).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS, LOG = "video-job.json", "video-job.log"


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"], capture_output=True, text=True,
                                 creationflags=0x08000000, timeout=10).stdout
            return str(pid) in out
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def status(out_dir: str) -> Optional[Dict[str, Any]]:
    """The last status the job wrote for ``out_dir``, or None."""
    try:
        with open(os.path.join(out_dir, "visuals", STATUS), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def running(out_dir: str) -> bool:
    st = status(out_dir)
    return bool(st and st.get("state") == "running" and _alive(int(st.get("pid") or 0)))


class Tracker:
    """Turns the pipeline's progress messages into frames done, speed and time left, and writes the status file."""

    def __init__(self, out_dir: str, on_update: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.path = os.path.join(out_dir, "visuals", STATUS)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.on_update = on_update
        self.t0 = time.time()
        self.first: Optional[tuple] = None              # (time, frames) when frame counting started
        self.st: Dict[str, Any] = {"state": "running", "pid": os.getpid(), "message": "Starting", "done": 0, "total": 0,
                                   "eta_s": None, "fps": None, "started": time.strftime("%Y-%m-%d %H:%M:%S")}
        self._last_write = 0.0
        self.write(force=True)

    def update(self, msg: str, frac: float) -> None:
        m = re.search(r"frame (\d+) of (\d+)", msg)
        self.st["message"] = msg
        self.st["fraction"] = round(float(frac), 4)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            now = time.time()
            if self.first is None:
                self.first = (now, done)
            dt, dn = now - self.first[0], done - self.first[1]
            rate = dn / dt if dt > 1.0 and dn > 0 else None
            self.st.update(done=done, total=total, fps=round(rate, 2) if rate else None,
                           eta_s=int((total - done) / rate) if rate else None)
        self.write()

    def finish(self, state: str, message: str, result: Optional[Dict[str, Any]] = None) -> None:
        self.st.update(state=state, message=message, eta_s=0, seconds=round(time.time() - self.t0, 1))
        if result:                                      # file names only: the status file stays in the output folder
            self.st["result"] = {"ok": bool(result.get("ok")), "video": os.path.basename(result.get("path") or "") or None,
                                 "reason": result.get("reason"), "reused": result.get("reused") or []}
        self.write(force=True)

    def write(self, force: bool = False) -> None:
        now = time.time()
        if force or now - self._last_write > 1.0:
            self._last_write = now
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.st, fh)
            os.replace(tmp, self.path)
        if self.on_update:
            self.on_update(dict(self.st))


def describe(st: Dict[str, Any]) -> str:
    """One line for a person: what is happening and how long it still takes."""
    if st.get("total"):
        line = "Frame %d of %d" % (st["done"], st["total"])
        if st.get("fps"):
            line += " - %.1f frames/s" % st["fps"]
        if st.get("eta_s") is not None and st.get("state") == "running":
            m, s = divmod(int(st["eta_s"]), 60)
            line += " - about %s left" % ("%d min %02d s" % (m, s) if m else "%d s" % s)
        return line
    return st.get("message", "")


def run(board: str, out_dir: str, tracker: Tracker, cancel: Optional[threading.Event] = None) -> Dict[str, Any]:
    """Make the video (and the guide and report with it). Returns the pipeline's video result."""
    from .pipeline import run_pipeline
    from .project import Design
    d = Design.load(board)
    res = run_pipeline(d, out_dir=out_dir, outputs={"video": True, "visuals": "reuse"}, progress=tracker.update,
                       cancel=cancel)
    return res.get("video") or {"ok": False, "reason": "the video was not made"}


def _headless(board: str, out_dir: str) -> int:
    last = [""]

    def show(st):
        line = st.get("message", "") + ("  (%s)" % describe(st) if st.get("total") else "")
        if line != last[0]:
            last[0] = line
            print(time.strftime("%H:%M:%S"), line, flush=True)
    tr = Tracker(out_dir, show)
    try:
        vid = run(board, out_dir, tr)
    except Exception as exc:  # noqa: BLE001 - the status file must say what happened
        tr.finish("failed", repr(exc))
        raise
    if vid.get("ok"):
        tr.finish("done", "Video written: %s" % os.path.basename(vid["path"]), vid)
        return 0
    tr.finish("failed", "Video not made: %s" % vid.get("reason"), vid)
    return 1


def _window(board: str, out_dir: str) -> int:
    import wx

    app = wx.App(False)
    name = os.path.splitext(os.path.basename(board))[0]
    frame = wx.Frame(None, title="PCB TestKit - explainer video for %s" % name, size=(520, 190),
                     style=wx.DEFAULT_FRAME_STYLE & ~(wx.RESIZE_BORDER | wx.MAXIMIZE_BOX))
    p = wx.Panel(frame)
    s = wx.BoxSizer(wx.VERTICAL)
    head = wx.StaticText(p, label="Rendering the 3D animation with KiCad. You can keep working in KiCad.")
    gauge = wx.Gauge(p, range=1000, size=(480, 18))
    line1 = wx.StaticText(p, label="Starting...")
    line2 = wx.StaticText(p, label="")
    btns = wx.BoxSizer(wx.HORIZONTAL)
    b_cancel = wx.Button(p, label="Cancel")
    b_open = wx.Button(p, label="Open folder")
    b_open.Hide()
    btns.Add(b_open, 0, wx.RIGHT, 8)
    btns.Add(b_cancel)
    for w_, f_ in ((head, 0), (gauge, 0), (line1, 0), (line2, 0)):
        s.Add(w_, f_, wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, 10)
    s.Add(btns, 0, wx.ALL | wx.ALIGN_RIGHT, 10)
    p.SetSizer(s)
    cancel = threading.Event()
    result: Dict[str, Any] = {}

    def show(st):
        def ui():
            if not frame:
                return
            frac = st["done"] / float(st["total"]) if st.get("total") else st.get("fraction", 0.0)
            gauge.SetValue(int(1000 * max(0.0, min(1.0, frac))))
            line1.SetLabel(st.get("message", ""))
            line2.SetLabel(describe(st) if st.get("total") else "")
        wx.CallAfter(ui)
    tr = Tracker(out_dir, show)

    def work():
        try:
            vid = run(board, out_dir, tr, cancel)
            result.update(vid)
            if vid.get("ok"):
                tr.finish("done", "Video written: %s" % os.path.basename(vid["path"]), vid)
            elif vid.get("cancelled"):
                tr.finish("cancelled", "Cancelled: the frames are kept, the next run goes on from there.", vid)
            else:
                tr.finish("failed", "Video not made: %s" % vid.get("reason"), vid)
        except Exception as exc:  # noqa: BLE001
            result.update(ok=False, reason=repr(exc))
            tr.finish("failed", "Video not made: %r" % exc)

        def done():
            if not frame:
                return
            head.SetLabel("Done." if result.get("ok") else "The video was not made.")
            line1.SetLabel(tr.st.get("message", ""))
            line2.SetLabel("Details: visuals/%s" % LOG if not result.get("ok") else "")
            gauge.SetValue(1000 if result.get("ok") else gauge.GetValue())
            b_cancel.SetLabel("Close")
            b_open.Show()
            p.Layout()
            frame.RequestUserAttention()
        wx.CallAfter(done)

    def on_cancel(_evt):
        if b_cancel.GetLabel() == "Close":
            frame.Destroy()
            return
        cancel.set()
        b_cancel.Disable()
        head.SetLabel("Cancelling: the frames in progress finish first...")

    def on_open(_evt):
        target = result.get("path") or out_dir
        folder = os.path.dirname(target) if os.path.isfile(target) else target
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", target] if os.path.isfile(target) else ["open", folder])
        elif os.name == "nt":
            os.startfile(folder)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", folder])

    b_cancel.Bind(wx.EVT_BUTTON, on_cancel)
    b_open.Bind(wx.EVT_BUTTON, on_open)
    frame.Bind(wx.EVT_CLOSE, lambda e: (cancel.set(), e.Skip()))
    threading.Thread(target=work, daemon=True).start()
    frame.Show()
    app.MainLoop()
    return 0 if result.get("ok") else 1


def launch(board: str, out_dir: str, window: bool = True, python: Optional[str] = None) -> Dict[str, Any]:
    """Start the video job in the background and return at once: {"ok", "pid", "log", "window"} or the reason it
    did not start. KiCad's Python (with wxPython) shows the progress window; otherwise the job writes its log."""
    if running(out_dir):
        st = status(out_dir) or {}
        return {"ok": False, "reason": "a video job for this folder is already running (%s)" % describe(st), "running": True}
    from .video import find_wx_python
    py = python or (find_wx_python() if window else None)
    use_window = bool(window and py)
    py = py or (sys.executable if os.path.basename(sys.executable).lower().startswith("python") else None)
    if not py:
        from .video import find_python
        py = find_python()
    os.makedirs(os.path.join(out_dir, "visuals"), exist_ok=True)
    log = os.path.join(out_dir, "visuals", LOG)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.dirname(HERE) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    cmd = [py, "-m", "pcbtestkit.video_job", os.path.abspath(board), "--out", os.path.abspath(out_dir)]
    if not use_window:
        cmd.append("--no-window")
    kw: Dict[str, Any] = {}
    if os.name == "nt":
        # with the progress window: no console at all; without it: one console that shows the progress lines
        kw["creationflags"] = (0x08000000 if use_window else 0x00000010) | 0x00000200   # NO_WINDOW / NEW_CONSOLE, NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    fh = open(log, "w", encoding="utf-8")
    try:
        p = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                             cwd=os.path.abspath(out_dir), **kw)
    except OSError as exc:
        fh.close()
        return {"ok": False, "reason": "could not start %s: %s" % (os.path.basename(py), exc)}
    fh.close()
    # say "running" at once, so a second start for the same folder is refused before the job writes its own status
    st_path = os.path.join(out_dir, "visuals", STATUS)
    with open(st_path + ".tmp", "w", encoding="utf-8") as sf:
        json.dump({"state": "running", "pid": p.pid, "message": "Starting", "done": 0, "total": 0, "eta_s": None,
                   "started": time.strftime("%Y-%m-%d %H:%M:%S")}, sf)
    os.replace(st_path + ".tmp", st_path)
    return {"ok": True, "pid": p.pid, "log": log, "window": use_window}


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m pcbtestkit.video_job",
                                 description="Render the explainer video in the background, with a progress window.")
    ap.add_argument("board", help=".kicad_pcb, .kicad_pro or project folder")
    ap.add_argument("--out", required=True, help="the output folder of the earlier run (its visuals/ are reused)")
    ap.add_argument("--no-window", action="store_true", help="print the progress instead of showing a window")
    a = ap.parse_args(argv)
    if running(a.out) and (status(a.out) or {}).get("pid") != os.getpid():
        print("a video job for %s is already running" % a.out, file=sys.stderr)
        return 2
    if not a.no_window:
        try:
            import wx  # noqa: F401
            return _window(a.board, a.out)
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001 - no display: fall back to the log
            print("no progress window (%r); progress follows" % exc, flush=True)
    return _headless(a.board, a.out)


if __name__ == "__main__":
    sys.exit(main())
