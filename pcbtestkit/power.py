"""Long runs: keep the computer awake (also with the lid closed), then sleep, hibernate or shut it down.

A test environment for 100+ boards with every explainer video takes 10-14 hours. A run given a power setting

    {"after": "none" | "sleep" | "hibernate" | "shutdown", "keep_awake": true, "lid": false, "grace_s": 120}

keeps the computer from sleeping while it works (``lid``: also with the lid closed) and, once the run has finished
or failed (never when you cancelled it), waits ``grace_s`` seconds and then does ``after``. During that wait it can
still be kept on: the progress window's "Stay on" button, ``pcbtestkit environment stay-on OUT``, or any file named
OUT/environment-job.stay-on. A background job's power is handled by a watcher process of its own
(``python -m pcbtestkit.power watch OUT --pid N``), so a job that crashes still counts as failed.

Per system:
  macOS    awake: caffeinate. Lid: ``pmset disablesleep 1`` through a small administrator helper (macOS asks for the
           password once); the helper sets it back when the run ends, when the watcher stops, and when the battery
           drops under 10 %. Sleep: ``pmset sleepnow``. Hibernate: sleep (a Mac keeps its memory on disk while it
           sleeps). Shut down: System Events (an app with unsaved work can stop it: the Mac then sleeps instead);
           with the lid helper, ``shutdown -h now``.
  Windows  awake: SetThreadExecutionState. Lid: the power plan's lid action set to "do nothing" and back afterwards.
           Sleep: SetSuspendState. Hibernate: ``shutdown /h``. Shut down: ``shutdown /s``.
  Linux    awake and lid: systemd-inhibit. ``systemctl suspend`` / ``hibernate`` / ``poweroff``.

The power status is OUT/environment-job-power.json. PCBTESTKIT_POWER_DRYRUN=1 logs every command instead of running
it (the tests use it).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional

ACTIONS = ("none", "sleep", "hibernate", "shutdown")
DEFAULTS: Dict[str, Any] = {"after": "none", "keep_awake": True, "lid": False, "grace_s": 120}
STATUS, STAY = "environment-job-power.json", "environment-job.stay-on"
VERBS = {"none": "leave the computer on", "sleep": "put the computer to sleep", "hibernate": "hibernate the computer",
         "shutdown": "shut the computer down"}
WILL = {"none": "stay on", "sleep": "go to sleep", "hibernate": "hibernate", "shutdown": "shut down"}
ALIASES = {"off": "shutdown", "poweroff": "shutdown", "power-off": "shutdown", "shut-down": "shutdown",
           "shut down": "shutdown", "standby": "sleep", "suspend": "sleep", "stand-by": "sleep", "": "none",
           "nothing": "none", "on": "none"}
FALLBACK_S = 180            # a shutdown that has not happened by then (an app with unsaved work stopped it): sleep
LOW_BATTERY = 10            # macOS lid helper: below this (on battery) the Mac may sleep again
_NO_WINDOW = 0x08000000
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_./-]+$")

# The macOS lid helper, run as root by "do shell script ... with administrator privileges" (in the background).
# Only the watcher's pid and a private folder's path are put in; the decision file is read as a word, never run.
MAC_HELPER = r"""(F='%(flag)s'; W=%(pid)d
pmset -a disablesleep 1
while kill -0 $W 2>/dev/null && [ ! -s "$F" ]; do
  b=$(pmset -g batt)
  case "$b" in *discharging*)
    p=$(printf '%%s' "$b" | grep -Eo '[0-9]+%%' | head -n 1 | tr -d '%%')
    if [ -n "$p" ] && [ "$p" -lt %(low)d ]; then break; fi;;
  esac
  sleep 5
done
pmset -a disablesleep 0
d=$(head -c 16 "$F" 2>/dev/null)
case "$d" in shutdown*) shutdown -h now;; sleep*) pmset sleepnow;; esac) > /dev/null 2>&1 &"""


def dry_run() -> bool:
    return os.environ.get("PCBTESTKIT_POWER_DRYRUN", "") not in ("", "0")


def normalize(cfg: Any = None) -> Dict[str, Any]:
    """The power setting with its defaults (a string is the action). ValueError for what is not valid."""
    c = dict(DEFAULTS)
    if isinstance(cfg, str):
        cfg = {"after": cfg}
    for k, v in (cfg or {}).items():
        if k in c and v is not None:
            c[k] = v
    a = str(c["after"]).strip().lower()
    a = ALIASES.get(a, a)
    if a not in ACTIONS:
        raise ValueError("after the run: %r is not one of %s" % (c["after"], ", ".join(ACTIONS)))
    c["after"] = a
    c["lid"] = bool(c["lid"])
    c["keep_awake"] = bool(c["keep_awake"]) or c["lid"]
    try:
        c["grace_s"] = max(0, min(3600, int(c["grace_s"])))
    except (TypeError, ValueError):
        raise ValueError("grace_s: %r is not a number of seconds" % (c["grace_s"],))
    return c


def active(cfg: Dict[str, Any]) -> bool:
    return cfg["after"] != "none" or cfg["keep_awake"] or cfg["lid"]


def _span(s: float) -> str:
    s = int(round(s))
    if s <= 0:
        return "at once"
    if s < 90:
        return "%d s" % s
    return "%d min" % round(s / 60.0) if s % 60 == 0 or s >= 600 else "%d min %02d s" % divmod(s, 60)


def describe(cfg: Any) -> str:
    """One line: what the run does with the computer's power."""
    c = normalize(cfg)
    parts = []
    if c["lid"]:
        parts.append("keeps the computer running while it works, also with the lid closed")
    elif c["keep_awake"]:
        parts.append("keeps the computer awake while it works")
    if c["after"] != "none":
        parts.append("will %s %s after the run has finished or failed" % (
            VERBS[c["after"]], "at once" if not c["grace_s"] else _span(c["grace_s"])))
    return " and ".join(parts) or "leaves the computer's power alone"


def commands(action: str, platform: Optional[str] = None) -> List[List[str]]:
    """The command(s) that do ``action`` on this system, tried in order."""
    p = platform or sys.platform
    if action == "none":
        return []
    if p == "darwin":
        if action == "shutdown":
            return [["osascript", "-e", 'tell application "System Events" to shut down']]
        return [["pmset", "sleepnow"]]
    if p.startswith("win"):
        if action == "shutdown":
            return [["shutdown", "/s", "/t", "0"]]
        if action == "hibernate":
            return [["shutdown", "/h"]]
        return [["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Add-Type -AssemblyName System.Windows.Forms; "
                 "[void][System.Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false)"],
                ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]]
    if action == "shutdown":
        return [["systemctl", "poweroff"]]
    if action == "hibernate":
        return [["systemctl", "hibernate"], ["systemctl", "suspend"]]
    return [["systemctl", "suspend"]]


def alive(pid: int) -> bool:
    """Is the process running (a zombie that its parent has not collected yet is not)?"""
    from .video_job import _alive
    if not _alive(int(pid or 0)):
        return False
    if os.name != "nt":
        try:
            r = subprocess.run(["ps", "-o", "stat=", "-p", str(int(pid))], capture_output=True, text=True, timeout=10)
            if r.stdout.strip().startswith("Z"):
                return False
        except (OSError, subprocess.SubprocessError):
            pass
    return True


def status(out_dir: str) -> Optional[Dict[str, Any]]:
    try:
        with open(os.path.join(out_dir, STATUS), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def stay_on(out_dir: str, wait_s: float = 3.0) -> Dict[str, Any]:
    """Keep the computer on after the run in ``out_dir`` (the sleep or shutdown is not done). Returns the power status."""
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, STAY), "w", encoding="utf-8") as fh:
        fh.write("keep the computer on after this run\n")
    end = time.time() + wait_s
    st = status(out_dir) or {}
    while time.time() < end and st.get("state") in ("watching", "countdown") and alive(int(st.get("pid") or 0)):
        time.sleep(0.2)
        st = status(out_dir) or {}
    return st


def _mac_sleep_disabled() -> bool:
    try:
        out = subprocess.run(["pmset", "-g"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(re.search(r"SleepDisabled\s+1", out))


class Session:
    """The power of one run: ``start()`` before it, ``finish(outcome)`` after it (done, failed or cancelled)."""

    def __init__(self, cfg: Any, out_dir: Optional[str], log: Optional[Callable[[str], None]] = None,
                 job_pid: Optional[int] = None, hint: str = ""):
        self.cfg = normalize(cfg)
        self.out = os.path.abspath(out_dir) if out_dir else ""
        self._log = log
        self.hint = hint
        self.procs: List[subprocess.Popen] = []
        self._win_awake = False
        self._win_lid: Optional[List[int]] = None
        self._mac_dir: Optional[str] = None
        self.st: Dict[str, Any] = {"pid": os.getpid(), "job_pid": job_pid or os.getpid(), "state": "watching",
                                   "after": self.cfg["after"], "keep_awake": self.cfg["keep_awake"],
                                   "lid": self.cfg["lid"], "grace_s": self.cfg["grace_s"], "dry_run": dry_run(),
                                   "describe": describe(self.cfg), "log": []}

    # ---- status ------------------------------------------------------------------------------------
    def log(self, msg: str) -> None:
        self.st["log"] = (self.st["log"] + ["%s %s" % (time.strftime("%H:%M:%S"), msg)])[-60:]
        self.write()
        if self._log:
            self._log(msg)

    def write(self) -> None:
        if not self.out:
            return
        os.makedirs(self.out, exist_ok=True)
        p = os.path.join(self.out, STATUS)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.st, fh, indent=1)
        os.replace(tmp, p)

    def stayed_on(self) -> bool:
        return bool(self.out) and os.path.exists(os.path.join(self.out, STAY))

    def _run(self, cmd: List[str], timeout: float = 60) -> bool:
        if dry_run():
            self.log("would run: %s" % " ".join(cmd))
            return True
        try:
            kw: Dict[str, Any] = {"creationflags": _NO_WINDOW} if os.name == "nt" else {}
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
        except (OSError, subprocess.SubprocessError) as exc:
            self.log("%s did not run: %s" % (cmd[0], exc))
            return False
        if r.returncode != 0:
            self.log("%s failed: %s" % (" ".join(cmd[:2]), (r.stderr or r.stdout or "exit %d" % r.returncode).strip()[:200]))
            return False
        return True

    # ---- while it runs -----------------------------------------------------------------------------
    def start(self) -> "Session":
        try:
            if self.out:
                os.remove(os.path.join(self.out, STAY))     # a stay-on from an earlier run does not count
        except OSError:
            pass
        self._stale()
        if self.cfg["keep_awake"]:
            self._keep_awake()
        if self.cfg["lid"]:
            self._lid()
        if self.cfg["after"] == "shutdown" and sys.platform == "darwin" and not self._mac_dir and not dry_run():
            # ask now, while someone is at the computer: macOS asks once whether this app may use System Events
            if not self._run(["osascript", "-e", 'tell application "System Events" to return name'], timeout=120):
                self.log("macOS did not allow PCB TestKit to shut the Mac down (System Settings > Privacy & Security >"
                         " Automation); the Mac will sleep instead")
                self.cfg["after"] = self.st["after"] = "sleep"
        self.log("started: %s" % describe(self.cfg))
        return self

    def _keep_awake(self) -> None:
        me = os.getpid()
        if dry_run():
            self.log("would keep the computer awake")
            return
        try:
            if sys.platform == "darwin":
                self.procs.append(subprocess.Popen(["caffeinate", "-i", "-s", "-m", "-w", str(me)],
                                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            elif os.name == "nt":
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)  # type: ignore[attr-defined]
                self._win_awake = True
            elif shutil.which("systemd-inhibit"):
                what = "idle:sleep" + (":handle-lid-switch" if self.cfg["lid"] else "")
                self.procs.append(subprocess.Popen(
                    ["systemd-inhibit", "--what=" + what, "--who=PCB TestKit", "--why=a test environment is being built",
                     "--mode=block", "sh", "-c", "while kill -0 %d 2>/dev/null; do sleep 5; done" % me],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            else:
                self.log("nothing here keeps the computer awake (no systemd-inhibit): set it not to sleep yourself")
                return
            self.log("keeping the computer awake")
        except (OSError, AttributeError) as exc:
            self.log("could not keep the computer awake: %s" % exc)

    def _lid(self) -> None:
        if sys.platform == "darwin":
            self._mac_lid()
        elif os.name == "nt":
            self._windows_lid()
        elif shutil.which("systemd-inhibit"):
            self.log("closing the lid does not stop the run (where the desktop allows it)")
        else:
            self.log("closing the lid may stop the run: nothing here can hold it (no systemd-inhibit)")

    def _mac_lid(self) -> None:
        if dry_run():
            self.log("would ask for the administrator password to keep running with the lid closed")
            return
        d = tempfile.mkdtemp(prefix="pcbtestkit-power-")
        flag = os.path.join(d, "decision")
        if not _SAFE_PATH.match(flag):
            shutil.rmtree(d, ignore_errors=True)
            self.log("closing the lid will make the Mac sleep (the temporary folder's name is unusual)")
            return
        script = MAC_HELPER % {"flag": flag, "pid": os.getpid(), "low": LOW_BATTERY}
        apple = 'do shell script "%s" with administrator privileges with prompt "%s"' % (
            script.replace("\\", "\\\\").replace('"', '\\"'),
            "PCB TestKit keeps the Mac running with the lid closed until the test environment is built.")
        try:
            r = subprocess.run(["osascript", "-e", apple], capture_output=True, text=True, timeout=600)
            ok = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        if ok:
            for _ in range(40):
                if _mac_sleep_disabled():
                    break
                time.sleep(0.25)
            ok = _mac_sleep_disabled()
        if not ok:
            shutil.rmtree(d, ignore_errors=True)
            self.log("no administrator rights: closing the lid makes the Mac sleep (the run goes on when it wakes)")
            return
        self._mac_dir = d
        self._mark({"mac": True})
        self.log("the Mac keeps running with the lid closed (until the run ends, or the battery is under %d %%)"
                 % LOW_BATTERY)

    def _windows_lid(self) -> None:
        q = ["powercfg", "/query", "SCHEME_CURRENT", "SUB_BUTTONS", "LIDACTION"]
        if dry_run():
            self.log("would set the lid action to do nothing: powercfg /setacvalueindex ... LIDACTION 0")
            return
        try:
            out = subprocess.run(q, capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        vals = re.findall(r"0x([0-9a-fA-F]{8})", out)
        if len(vals) < 2:
            self.log("could not read the lid action (powercfg): closing the lid may stop the run")
            return
        old = [int(vals[-2], 16), int(vals[-1], 16)]
        if self._set_lid(0, 0):
            self._win_lid = old
            self._mark({"lid_ac": old[0], "lid_dc": old[1]})
            self.log("closing the lid does nothing until the run ends (it was %s)" % ("/".join(map(str, old))))
        else:
            self.log("could not change the lid action: closing the lid may stop the run")

    def _set_lid(self, ac: int, dc: int) -> bool:
        base = ["SCHEME_CURRENT", "SUB_BUTTONS", "LIDACTION"]
        return (self._run(["powercfg", "/setacvalueindex"] + base + [str(ac)])
                and self._run(["powercfg", "/setdcvalueindex"] + base + [str(dc)])
                and self._run(["powercfg", "/setactive", "SCHEME_CURRENT"]))

    def _mark(self, d: Optional[Dict[str, Any]]) -> None:
        """Note what must be set back (settings.json), for a run that never got to it."""
        from . import settings
        s = settings.load()
        if d:
            s["power_restore"] = dict(d, pid=os.getpid())
        else:
            s.pop("power_restore", None)
        settings.save(s)

    def _stale(self) -> None:
        """Set back what an earlier run could not (it was killed, the computer restarted)."""
        if dry_run():
            return
        from . import settings
        r = settings.load().get("power_restore")
        if not isinstance(r, dict) or alive(int(r.get("pid") or 0)):
            return
        if "lid_ac" in r and os.name == "nt":
            if self._set_lid(int(r["lid_ac"]), int(r.get("lid_dc", r["lid_ac"]))):
                self.log("set the lid action back to what it was before an earlier run")
        if r.get("mac") and sys.platform == "darwin" and _mac_sleep_disabled():
            self.log("sleep is still off from an earlier run: in Terminal, sudo pmset -a disablesleep 0 turns it on")
        self._mark(None)

    # ---- after the run -----------------------------------------------------------------------------
    def release(self) -> None:
        """Let the computer sleep again and set the lid back."""
        for p in self.procs:
            try:
                p.terminate()
            except OSError:
                pass
        self.procs = []
        if self._win_awake:
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)  # type: ignore[attr-defined]
            except (OSError, AttributeError):
                pass
            self._win_awake = False
        if self._win_lid is not None:
            if self._set_lid(*self._win_lid):
                self._mark(None)
            self._win_lid = None
        if self._mac_dir:
            d = self._decide("restore")
            for _ in range(80):                                 # the helper looks every 5 s
                if not _mac_sleep_disabled():
                    break
                time.sleep(0.25)
            if d and not _mac_sleep_disabled():
                shutil.rmtree(d, ignore_errors=True)
            self._mark(None)

    def _decide(self, what: str) -> Optional[str]:
        """Tell the macOS lid helper what to do as it ends: restore, sleep or shutdown (it reads the word only)."""
        d, self._mac_dir = self._mac_dir, None
        if d:
            try:
                with open(os.path.join(d, "decision"), "w", encoding="utf-8") as fh:
                    fh.write(what + "\n")
            except OSError:
                pass
        return d

    def finish(self, outcome: str, reason: str = "") -> str:
        """After the run: "cancelled" only lets the computer sleep again; "done" and "failed" also do ``after`` once
        the grace period has passed without a stay-on. Returns what happened."""
        a = self.cfg["after"]
        self.st.update(outcome=outcome, reason=reason or None)
        words = {"done": "has finished", "failed": "has failed", "cancelled": "was cancelled"}.get(outcome, outcome)
        if outcome == "cancelled" or a == "none":
            self.release()
            self.st["state"] = "done"
            self.log("the run %s: the computer stays on" % words)
            return "none"
        end = time.time() + self.cfg["grace_s"]
        self.st.update(state="countdown", action=a, at=round(end, 1))
        self.log("the run %s: will %s %s (%s)" % (
            words, VERBS[a], "now" if not self.cfg["grace_s"] else "in " + _span(self.cfg["grace_s"]),
            self.hint or ("to keep it on: pcbtestkit environment stay-on \"%s\"" % self.out if self.out else
                          "Ctrl+C keeps it on")))
        kept = False
        try:
            while time.time() < end and not self.stayed_on():
                time.sleep(max(0.0, min(0.5, end - time.time())))
        except KeyboardInterrupt:
            kept = True
        if kept or self.stayed_on():
            self.release()
            self.st["state"] = "kept-on"
            self.log("kept on: the computer was not %s" % {"sleep": "put to sleep", "hibernate": "hibernated",
                                                             "shutdown": "shut down"}[a])
            return "kept-on"
        return self.act(a)

    def act(self, action: str) -> str:
        """Sleep, hibernate or shut down now."""
        self.st["state"] = "acting"
        for p in self.procs:                                   # caffeinate / systemd-inhibit would hold it awake
            try:
                p.terminate()
            except OSError:
                pass
        self.procs = []
        if self._mac_dir:                                      # the root helper sets sleep back on, then does it
            self.log("%s (the administrator helper sets sleep back on first)" % VERBS[action])
            self._decide("shutdown" if action == "shutdown" else "sleep")
            self._mark(None)
            self.st["state"] = "done"
            self.write()
            return action
        self.release()
        cmds = commands(action)
        self.log("%s: %s" % (VERBS[action], " ".join(cmds[0]) if cmds else "nothing to do"))
        ok = any(self._run(c) for c in cmds)
        if not ok:
            self.st["state"] = "failed"
            self.log("could not %s" % VERBS[action])
            return "failed"
        self.st["state"] = "done"
        self.write()
        if action == "shutdown" and not dry_run():
            time.sleep(FALLBACK_S)                             # still here: an app with unsaved work said no
            self.log("the computer was not shut down (an app with unsaved work?): it sleeps instead")
            for c in commands("sleep"):
                if self._run(c):
                    break
        return action


# ---- the background job's watcher --------------------------------------------------------------------
def job_outcome(out_dir: str, pid: int, since: float = 0.0) -> Optional[str]:
    """done / failed / cancelled once the environment job in ``out_dir`` has ended, else None. "done" is only
    confirmed when its environment page was written by this run; a job that died without a word has failed."""
    from .environment_job import status as job_status
    st = job_status(out_dir) or {}
    s = st.get("state")
    if s == "done":
        page = os.path.join(out_dir, "index.html")
        return "done" if os.path.exists(page) and os.path.getmtime(page) >= since - 5 else "failed"
    if s in ("failed", "cancelled"):
        return s
    if not alive(pid):
        time.sleep(1.0)                                         # its last status write may be on the way
        st = job_status(out_dir) or {}
        if st.get("state") in ("done", "failed", "cancelled"):
            return job_outcome(out_dir, pid, since)
        return "failed"
    return None


def watch(out_dir: str, pid: int, cfg: Any = None, poll_s: float = 5.0) -> str:
    """Keep the computer awake while the job ``pid`` runs, then do what ``cfg`` says. Returns what happened."""
    out_dir = os.path.abspath(out_dir)
    if cfg is None:
        try:
            with open(os.path.join(out_dir, "environment-job-request.json"), encoding="utf-8") as fh:
                cfg = json.load(fh).get("power")
        except (OSError, ValueError):
            cfg = None
    t0 = time.time()
    ses = Session(cfg, out_dir, job_pid=pid).start()
    warned = False
    try:
        while True:
            oc = job_outcome(out_dir, pid, t0)
            if oc:
                break
            if ses.cfg["lid"] and sys.platform == "darwin" and ses._mac_dir and not warned and not _mac_sleep_disabled():
                ses.log("sleep is back on (battery under %d %%?): closing the lid makes the Mac sleep" % LOW_BATTERY)
                warned = True
            time.sleep(poll_s)
    except KeyboardInterrupt:
        oc = "cancelled"
    from .environment_job import status as job_status
    return ses.finish(oc, ((job_status(out_dir) or {}).get("message") or "") if oc != "cancelled" else "")


def launch_watcher(out_dir: str, pid: int, python: str) -> Optional[int]:
    """Start the watcher for the job ``pid`` in the background (its own session: it outlives KiCad and the job)."""
    env = dict(os.environ)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = here + os.pathsep + env.get("PYTHONPATH", "")
    kw: Dict[str, Any] = {}
    if os.name == "nt":
        kw["creationflags"] = _NO_WINDOW | 0x00000200
    else:
        kw["start_new_session"] = True
    try:
        p = subprocess.Popen([python, "-m", "pcbtestkit.power", "watch", out_dir, "--pid", str(pid)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             cwd=out_dir, env=env, **kw)
    except OSError:
        return None
    return p.pid


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m pcbtestkit.power", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("watch", help="keep the computer awake while an environment job runs, then do its 'power' setting")
    w.add_argument("out", help="the job's output folder")
    w.add_argument("--pid", type=int, required=True, help="the job's process id")
    s = sub.add_parser("stay-on", help="keep the computer on after the run in OUT")
    s.add_argument("out")
    a = ap.parse_args(argv)
    if a.cmd == "stay-on":
        print(json.dumps(stay_on(a.out), indent=1))
        return 0
    watch(a.out, a.pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
