#!/usr/bin/env python3
"""Regenerate the published examples in docs/examples (slow: about 35 minutes with the video).

    python3 tools/regen_examples.py                 all three, with the 3D-animated video
    python3 tools/regen_examples.py --no-video      minutes instead of half an hour
    python3 tools/regen_examples.py --only sample_sensor
    python3 tools/regen_examples.py --only station  the example test station with four board types

Boards: the bundled sample (examples/sample_sensor) and KiCad's own demos pic_programmer, video and
tiny_tapeout, found next to kicad-cli (macOS app bundle, /usr/share/kicad/demos, Windows install) or in --demos.
tiny_tapeout is the showcase: a batch of four boards on one fixture, programmed at the same time over SWD while
the checks that need no firmware run, the production timing and the test shot with the tested copper lighting
up. It runs on a copy of the demo with the settings in SHOWCASE. station: the example test station
(examples/make_station.py) with the sample and three demos, each with its batch, and the station video.
Needs KiCad 9+ (renders) and ffmpeg (video).
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# settings written next to a copy of the demo before it is generated (pcbtestkit.json)
SHOWCASE = {
    "tiny_tapeout": {
        "batch": {"count": 4},
        "testplan": {"max_channels": 12},
        "program": {"command": ["openocd", "-f", "interface/cmsis-dap.cfg", "-c", "adapter serial PROBE-{site}",
                                "-f", "target/rp2040.cfg", "-c", "program firmware.elf verify reset exit"],
                    "estimate_s": 25},
        "video": {"quality": "standard"},
    },
}


def demos_dir(explicit=None):
    if explicit:
        return explicit
    from pcbtestkit.kicad_cli import find_kicad_cli
    cands = []
    cli = find_kicad_cli()
    if cli:
        app = os.path.dirname(os.path.dirname(cli))
        cands += [os.path.join(app, "SharedSupport", "demos"), os.path.join(os.path.dirname(app), "demos"),
                  os.path.join(os.path.dirname(os.path.dirname(app)), "demos"),
                  os.path.join(os.path.dirname(cli), "..", "share", "kicad", "demos")]
    cands += glob.glob("/Volumes/KiCad*/demos") + ["/usr/share/kicad/demos", "/usr/local/share/kicad/demos"]
    for c in cands:
        if os.path.isdir(os.path.join(c, "pic_programmer")):
            return os.path.normpath(c)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--demos", help="KiCad demos folder")
    ap.add_argument("--no-video", action="store_true")
    ap.add_argument("--only", action="append")
    a = ap.parse_args()
    demos = demos_dir(a.demos)
    boards = {"sample_sensor": os.path.join(ROOT, "examples", "sample_sensor", "sample_sensor.kicad_pcb")}
    if demos:
        boards["pic_programmer"] = os.path.join(demos, "pic_programmer", "pic_programmer.kicad_pcb")
        boards["video"] = os.path.join(demos, "video", "video.kicad_pcb")
        boards["tiny_tapeout"] = os.path.join(demos, "tiny_tapeout")
    else:
        print("KiCad demos not found (use --demos); only the sample board is regenerated")
    if not os.path.exists(boards["sample_sensor"]):
        subprocess.run([sys.executable, os.path.join(ROOT, "examples", "make_sample.py")], check=True)
    if demos and (not a.only or "station" in a.only):
        station(demos, a.no_video)
    for name, board in boards.items():
        if a.only and name not in a.only:
            continue
        out = os.path.join(ROOT, "docs", "examples", name)
        shutil.rmtree(out, ignore_errors=True)
        if name in SHOWCASE:                    # a copy of the demo with the showcase settings
            tmp = tempfile.mkdtemp(prefix="pcbtk_%s_" % name)
            src = board if os.path.isdir(board) else os.path.dirname(board)
            shutil.copytree(src, os.path.join(tmp, name))
            with open(os.path.join(tmp, name, "pcbtestkit.json"), "w", encoding="utf-8") as fh:
                json.dump(SHOWCASE[name], fh, indent=1)
            board = os.path.join(tmp, name)
        cmd = [sys.executable, "-m", "pcbtestkit", "run", board, "-q", "--no-kicad", "--out", out]
        if not a.no_video:
            cmd.append("--video")
        print("==", name, "...", flush=True)
        r = subprocess.run(cmd, cwd=ROOT)
        for junk in ("runner/results", "runner/__pycache__", ".pcbtestkit"):
            shutil.rmtree(os.path.join(out, junk), ignore_errors=True)
        for prl in glob.glob(os.path.join(out, "*.kicad_prl")):
            os.remove(prl)
        print("==", name, "OK" if r.returncode == 0 else "FAILED (exit %d)" % r.returncode)
    return 0


# the boards on the example station: four board types, each with the batch the station takes
STATION_BOARDS = ("stickhub", "royalblue54L_feather", "pic_programmer")


def station(demos: str, no_video: bool) -> None:
    """docs/examples/station: one test station, four board types, a fixture (and batch) per type."""
    st = os.path.join(ROOT, "examples", "station", "test_station.kicad_pcb")
    if not os.path.exists(st):
        subprocess.run([sys.executable, os.path.join(ROOT, "examples", "make_station.py")], check=True)
    out = os.path.join(ROOT, "docs", "examples", "station")
    shutil.rmtree(out, ignore_errors=True)
    projects = [os.path.join(ROOT, "examples", "sample_sensor")] + [os.path.join(demos, b) for b in STATION_BOARDS]
    cmd = [sys.executable, "-m", "pcbtestkit", "station", "run", st] + projects + ["--out", out, "-q"]
    if not no_video:
        cmd += ["--video", "all"]                 # every board type's explainer and the station video
    print("== station ...", flush=True)
    r = subprocess.run(cmd, cwd=ROOT)
    for junk in glob.glob(os.path.join(out, "*", "runner", "results")) + glob.glob(os.path.join(out, "*", ".pcbtestkit")) + \
            [os.path.join(out, "station_video")]:
        shutil.rmtree(junk, ignore_errors=True)
    for prl in glob.glob(os.path.join(out, "*", "*.kicad_prl")):
        os.remove(prl)
    print("== station", "OK" if r.returncode in (0, 1) else "FAILED (exit %d)" % r.returncode)


if __name__ == "__main__":
    sys.exit(main())
