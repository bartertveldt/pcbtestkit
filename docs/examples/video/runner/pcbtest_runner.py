#!/usr/bin/env python3
"""PCB TestKit production test runner (Raspberry Pi / Linux / MCU over serial).

    python3 pcbtest_runner.py check-hw                 read every channel once
    python3 pcbtest_runner.py test   [--serial SN]     run the test sequence on the board (each board of a batch)
    python3 pcbtest_runner.py test --site 2            only site 2 of a batch fixture
    python3 pcbtest_runner.py learn  --units 3         derive limits for 'learn' checks from golden units
    python3 pcbtest_runner.py test --simulate          dry run without hardware

Files (same folder by default): testplan.json (generated), hardware.json (your wiring).
Results: results/<serial>_<time>.json and results/results.csv (with the fixture site of each board).
Batches: the fixture has one site per board; the boards are tested one after the other, each with its own
channels and power switch (see hardware.json), serial number and verdict.
Programming: the 'program' step runs program.command for each board through its PROG connector, or asks
the operator to program it by hand.
Safety: power is always switched off at the end (also on errors / Ctrl-C);
'command' and programming steps only run programs with --allow-commands.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import os
import re
import statistics
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from hw import Hardware, HardwareError  # noqa: E402

GREEN, RED, YEL, DIM, RST = ("\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m") if sys.stdout.isatty() else ("",) * 5


class Abort(Exception):
    pass


def load(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


class Runner:
    def __init__(self, plan, hwcfg, args, site: int = 1, serial: str = ""):
        self.plan = plan
        self.args = args
        self.site, self.serial = site, serial
        self.hw = Hardware(hwcfg, plan, simulate=args.simulate, fail_channels=args.sim_fail or (),
                           interactive=not args.non_interactive)
        self.hw.select_site(site)
        self.results: List[Dict[str, Any]] = []
        self.powered = False
        self.learn_data: Dict[str, List[float]] = {}

    # ------------------------------------------------------------------ #
    def record(self, step, cid, status, value=None, lo=None, hi=None, unit="", note=""):
        r = {"step": step["id"], "check": cid, "status": status}
        if value is not None:
            r["value"] = round(value, 5) if isinstance(value, float) else value
        if lo is not None or hi is not None:
            r["limits"] = [lo, hi]
        if unit:
            r["unit"] = unit
        if note:
            r["note"] = note
        self.results.append(r)
        col = {"pass": GREEN, "fail": RED, "skip": DIM, "learn": YEL}.get(status, "")
        lim = ""
        if lo is not None or hi is not None:
            lim = " [%s .. %s]" % ("" if lo is None else "%g" % lo, "" if hi is None else "%g" % hi)
        val = ("%.4g %s" % (value, unit)) if isinstance(value, (int, float)) else ("" if value is None else str(value))
        print("    %s%-5s%s %-28s %s%s %s" % (col, status.upper(), RST, cid[:28], val, lim, DIM + note + RST if note else ""))
        return status

    def set_power(self, on: bool):
        if on != self.powered:
            self.hw.power(on)
            self.powered = on
            time.sleep(0.05)

    # ------------------------------------------------------------------ #
    def run(self, mode: str = "test") -> str:
        nsites = int((self.plan.get("batch") or {}).get("count") or 1)
        print("%s %s rev %s%s - %s" % ("LEARN" if mode == "learn" else "TEST", self.plan["board"]["name"],
                                      self.plan["board"].get("revision") or "-",
                                      " - site %d of %d" % (self.site, nsites) if nsites > 1 else "",
                                      "SIMULATED" if self.args.simulate else "hardware"))
        verdict = "PASS"
        try:
            for st in self.plan["sequence"]:
                if self.args.only and st["id"] not in self.args.only:
                    continue
                print("  %s %s" % (st["id"], st["name"]))
                want_power = st.get("power")
                if want_power in ("on", "off") and st["type"] not in ("power",):
                    self.set_power(want_power == "on")
                fails_before = sum(1 for r in self.results if r["status"] == "fail")
                getattr(self, "step_" + st["type"], self.step_unknown)(st, mode)
                failed = sum(1 for r in self.results if r["status"] == "fail") > fails_before
                if failed and (st.get("abort_on_fail") or self.args.stop_on_fail):
                    raise Abort("step %s failed" % st["id"])
        except Abort as e:
            print("  %sABORT%s %s" % (RED, RST, e))
            verdict = "FAIL"
        except KeyboardInterrupt:
            print("  interrupted")
            verdict = "ABORTED"
        except HardwareError as e:
            print("  %sHARDWARE ERROR%s %s" % (RED, RST, e))
            verdict = "ERROR"
        finally:
            try:
                self.hw.power(False)
                self.powered = False
            except Exception as e:  # pragma: no cover
                print("  !! could not switch power off: %s - SWITCH IT OFF MANUALLY" % e)
        if verdict == "PASS" and any(r["status"] == "fail" for r in self.results):
            verdict = "FAIL"
        return verdict

    # ------------------------------------------------------------------ #
    def step_manual(self, st, mode):
        items = st.get("items") or ([{"id": st["id"], "check": st.get("instruction", st["name"])}] if st.get("instruction") or st.get("custom") else [])
        if self.args.skip_manual or self.args.non_interactive or mode == "learn":
            for it in items:
                self.record(st, it["id"], "skip", note="manual check skipped")
            return
        for it in items:
            refs = it.get("refs") or []
            prompt = "    ? %s%s [y/n] " % (it["check"], (" (%s)" % ", ".join(refs[:15]) + (" ..." if len(refs) > 15 else "")) if refs else "")
            ans = input(prompt).strip().lower()
            self.record(st, it["id"], "pass" if ans.startswith("y") else "fail")

    def step_resistance(self, st, mode):
        if not self.hw.can_measure_resistance():
            for c in st.get("checks", []):
                self.record(st, c["id"], "skip", note="no resistance hardware")
            return
        self.set_power(False)
        for c in st.get("checks", []):
            r = self.hw.resistance(c["channel"])
            ok = r >= c["min_ohm"]
            self.record(st, c["id"], "pass" if ok else "fail", r if r != float("inf") else "open", c["min_ohm"], None, "ohm")
            if not ok and st.get("abort_on_fail", True):
                raise Abort("short on %s (%.1f ohm) - not powering the board" % (c["net"], r))

    def step_power(self, st, mode):
        if st.get("action") == "off":
            self.set_power(False)
            return
        lim = (st.get("current") or {}).get("limit_a")
        self.set_power(True)
        t0 = time.time()
        if lim is not None:
            while time.time() - t0 < 0.2:
                i = self.hw.current_a()
                if i is None:
                    break
                if i > lim:
                    self.set_power(False)
                    self.record(st, "I_INRUSH", "fail", i, None, lim, "A", "over current limit - power removed")
                    raise Abort("over-current at power on")
                time.sleep(0.02)
        time.sleep(st.get("settle_ms", 500) / 1000.0)
        cur = st.get("current") or {}
        i = self.hw.current_a()
        if i is not None and (cur.get("max_idle_a") is not None or cur.get("min_idle_a") is not None):
            lo, hi = cur.get("min_idle_a"), cur.get("max_idle_a")
            ok = (lo is None or i >= lo) and (hi is None or i <= hi)
            self.record(st, "I_IDLE", "pass" if ok else "fail", i, lo, hi, "A")
        elif i is not None:
            self.record(st, "I_IDLE", "pass", i, None, lim, "A", "no limits set")
        for c in st.get("checks", []) or []:
            self._voltage(st, c, mode)

    def _voltage(self, st, c, mode):
        if not c.get("channel"):
            return self.record(st, c["id"], "skip", note=c.get("unavailable", "no channel"))
        v = self.hw.read_v(c["channel"])
        if c.get("learn") and mode == "learn":
            self.learn_data.setdefault(c["id"], []).append(v)
            return self.record(st, c["id"], "learn", v, unit="V")
        lo, hi = c.get("min_v"), c.get("max_v")
        if lo is None and hi is None:
            return self.record(st, c["id"], "skip", v, unit="V", note="no limits (run learn)")
        ok = (lo is None or v >= lo) and (hi is None or v <= hi)
        return self.record(st, c["id"], "pass" if ok else "fail", v, lo, hi, "V")

    def step_voltage(self, st, mode):
        for c in st.get("checks", []):
            self._voltage(st, c, mode)

    def step_current(self, st, mode):
        i = self.hw.current_a()
        if i is None:
            return self.record(st, st["id"], "skip", note="no current sensor")
        lo, hi = st.get("min_a"), st.get("max_a")
        ok = (lo is None or i >= lo) and (hi is None or i <= hi)
        self.record(st, st["id"], "pass" if ok else "fail", i, lo, hi, "A")

    def step_wait(self, st, mode):
        time.sleep(int(st.get("wait_ms", 0)) / 1000.0)

    def step_gpio(self, st, mode):
        g = st["gpio"]
        self.hw.gpio_out(int(g["pin"]), int(g["level"]))
        if g.get("hold_ms"):
            time.sleep(int(g["hold_ms"]) / 1000.0)
        self.record(st, st["id"], "pass", note="GPIO%d=%d" % (g["pin"], g["level"]))

    def step_uart_expect(self, st, mode):
        u = st["uart"]
        if self.args.simulate:
            return self.record(st, st["id"], "skip", note="simulated")
        try:
            import serial  # type: ignore
        except ImportError:
            return self.record(st, st["id"], "fail", note="pyserial not installed")
        with serial.Serial(u.get("port", "/dev/ttyUSB0"), int(u.get("baud", 115200)), timeout=0.1) as ser:
            if u.get("send"):
                ser.write(u["send"].encode().decode("unicode_escape").encode())
            buf, deadline = "", time.time() + u.get("timeout_ms", 3000) / 1000.0
            rx = re.compile(u["expect"])
            while time.time() < deadline:
                buf += ser.read(256).decode(errors="replace")
                if rx.search(buf):
                    return self.record(st, st["id"], "pass", note=rx.search(buf).group(0)[:40])
        self.record(st, st["id"], "fail", note="expected %r, got %r" % (u["expect"], buf[-60:]))

    def step_command(self, st, mode):
        c = st["command"]
        if not self.args.allow_commands:
            return self.record(st, st["id"], "skip", note="command steps need --allow-commands")
        try:
            r = subprocess.run(c["argv"], capture_output=True, text=True, timeout=float(c.get("timeout_s", 30)))
            ok = r.returncode == int(c.get("expect_exit", 0))
            self.record(st, st["id"], "pass" if ok else "fail", r.returncode, note=(r.stdout or r.stderr).strip()[-80:])
        except (OSError, subprocess.TimeoutExpired) as e:
            self.record(st, st["id"], "fail", note=str(e)[:80])

    def step_program(self, st, mode):
        """Program the board through the fixture's PROG connector of this site: with program.command (run per
        board, {site} and {serial} filled in; needs --allow-commands), or by hand with a prompt."""
        if mode == "learn":
            return self.record(st, st["id"], "skip", note="not programmed while learning")
        cmd = st.get("command") or []
        if cmd:
            if self.args.simulate:
                return self.record(st, st["id"], "skip", note="simulated: %s not run" % cmd[0])
            if not self.args.allow_commands:
                return self.record(st, st["id"], "skip", note="programming command needs --allow-commands")
            argv = [str(a).replace("{site}", str(self.site)).replace("{serial}", self.serial or "") for a in cmd]
            try:
                r = subprocess.run(argv, capture_output=True, text=True, timeout=float(st.get("timeout_s", 180)))
                ok = r.returncode == 0
                return self.record(st, st["id"], "pass" if ok else "fail", r.returncode,
                                   note=(r.stdout + r.stderr).strip().splitlines()[-1][:80] if (r.stdout or r.stderr) else "")
            except (OSError, subprocess.TimeoutExpired) as e:
                return self.record(st, st["id"], "fail", note=str(e)[:80])
        if self.args.skip_manual or self.args.non_interactive:
            return self.record(st, st["id"], "skip", note="no program.command; program it by hand")
        where = "%s%s" % (", ".join(st.get("headers") or []), " (site %d)" % self.site if self.hw.nsites > 1 else "")
        ans = input("    ? Program the board through the fixture's PROG connector for %s now. Programmed OK? [y/n] " % where)
        return self.record(st, st["id"], "pass" if ans.strip().lower().startswith("y") else "fail")

    def step_unknown(self, st, mode):
        self.record(st, st["id"], "skip", note="unknown step type %r" % st.get("type"))


# --------------------------------------------------------------------------- #
def save_results(args, plan, runner, verdict, serial_no):
    os.makedirs(args.results, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    rec = {"board": plan["board"], "serial": serial_no, "site": runner.site, "time": ts, "verdict": verdict,
           "simulated": bool(args.simulate), "results": runner.results}
    with open(os.path.join(args.results, "%s_%s.json" % (re.sub(r"[^\w.-]", "_", serial_no), ts)), "w") as fh:
        json.dump(rec, fh, indent=1)
    csv_path = os.path.join(args.results, "results.csv")
    new = not os.path.exists(csv_path)
    with open(csv_path, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["time", "serial", "verdict", "failed_checks", "simulated", "site"])
        w.writerow([ts, serial_no, verdict, " ".join(r["check"] for r in runner.results if r["status"] == "fail"),
                    int(bool(args.simulate)), runner.site])


def batch_sites(plan, args) -> List[int]:
    """The fixture sites to test: --site, else every board of the batch."""
    n = int((plan.get("batch") or {}).get("count") or 1)
    if args.site:
        bad = [k for k in args.site if k < 1 or k > n]
        if bad:
            raise SystemExit("--site %s: the fixture has %d site(s)" % (" ".join(map(str, bad)), n))
        return list(args.site)
    return list(range(1, n + 1))


def do_learn(args, plan, hwcfg):
    data: Dict[str, List[float]] = {}
    site = batch_sites(plan, args)[0]
    for u in range(args.units):
        if not args.non_interactive:
            input("Insert GOLDEN unit %d/%d%s and press Enter " % (u + 1, args.units,
                                                                 " in site %d" % site if len(batch_sites(plan, args)) > 1 or site > 1 else ""))
        r = Runner(plan, hwcfg, args, site=site)
        v = r.run("learn")
        if v not in ("PASS", "FAIL"):
            print("learn run aborted")
            return 1
        for k, vals in r.learn_data.items():
            data.setdefault(k, []).extend(vals)
    learned = json.loads(json.dumps(plan))
    n = 0
    for st in learned["sequence"]:
        for c in st.get("checks", []) or []:
            vals = data.get(c["id"])
            if not vals:
                continue
            mean = statistics.mean(vals)
            sd = statistics.pstdev(vals) if len(vals) > 1 else 0.0
            band = max(float(c.get("learn_tolerance_v", 0.15)), 4 * sd, 0.03 * abs(mean))
            c["min_v"], c["max_v"] = round(mean - band, 4), round(mean + band, 4)
            c["learned"] = {"units": len(vals), "mean": round(mean, 4), "std": round(sd, 5)}
            c.pop("learn", None)
            n += 1
    out = os.path.splitext(args.plan)[0] + ".learned.json"
    with open(out, "w") as fh:
        json.dump(learned, fh, indent=1)
    print("learned limits for %d check(s) -> %s (review, then use --plan %s)" % (n, out, os.path.basename(out)))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["test", "learn", "check-hw"])
    ap.add_argument("--plan", default=os.path.join(HERE, "testplan.json"))
    ap.add_argument("--hardware", default=os.path.join(HERE, "hardware.json"))
    ap.add_argument("--results", default=os.path.join(HERE, "results"))
    ap.add_argument("--serial", help="board serial number (prompted if omitted)")
    ap.add_argument("--units", type=int, default=3, help="golden units for learn mode")
    ap.add_argument("--simulate", action="store_true", help="no hardware: plausible fake readings")
    ap.add_argument("--sim-fail", nargs="*", help="(simulate) channels that read low, e.g. CH02")
    ap.add_argument("--only", nargs="*", help="run only these step ids")
    ap.add_argument("--skip-manual", action="store_true")
    ap.add_argument("--non-interactive", action="store_true")
    ap.add_argument("--stop-on-fail", action="store_true")
    ap.add_argument("--allow-commands", action="store_true", help="allow 'command' and programming steps to run programs")
    ap.add_argument("--loop", action="store_true", help="test batch after batch until Ctrl-C")
    ap.add_argument("--site", nargs="*", type=int, help="test only these fixture sites (boards of a batch), e.g. --site 2 3")
    args = ap.parse_args(argv)

    plan = load(args.plan)
    hwcfg = load(args.hardware) if os.path.exists(args.hardware) else {"adc": {"type": "simulated"}}
    sites = batch_sites(plan, args)
    many = int((plan.get("batch") or {}).get("count") or 1) > 1
    if args.mode == "check-hw":
        hw = Hardware(hwcfg, plan, simulate=args.simulate, interactive=not args.non_interactive)
        for k in sites:
            hw.select_site(k)
            if many:
                print("site %d" % k)
            for p in plan["probes"]:
                if p.get("channel"):
                    print("  %-5s %-16s %8.4f V" % (p["channel"], p["net"][:16], hw.read_v(p["channel"])))
        return 0
    if args.mode == "learn":
        return do_learn(args, plan, hwcfg)
    while True:
        verdicts = []
        for k in sites:
            tag = " (site %d)" % k if many else ""
            if args.serial:
                sn = args.serial + ("-%d" % k if len(sites) > 1 else "")
            elif args.non_interactive:
                sn = "SIM-%d%s" % (int(time.time()), "-%d" % k if len(sites) > 1 else "")
            else:
                sn = input("Serial number%s: " % tag).strip() or "unknown"
            r = Runner(plan, hwcfg, args, site=k, serial=sn)
            verdict = r.run("test")
            save_results(args, plan, r, verdict, sn)
            col = GREEN if verdict == "PASS" else RED
            print("\n  %s==== %s ====%s  %s%s\n" % (col, verdict, RST, sn, tag))
            verdicts.append((k, sn, verdict))
        if len(verdicts) > 1:
            print("  batch: " + "  ".join("site %d %s%s%s" % (k, GREEN if v == "PASS" else RED, v, RST) for k, _sn, v in verdicts))
        if not args.loop:
            return 0 if all(v == "PASS" for _k, _sn, v in verdicts) else 1
        args.serial = None


if __name__ == "__main__":
    sys.exit(main())
