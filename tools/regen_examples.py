#!/usr/bin/env python3
"""Regenerate the published examples in docs/examples (slow: about 35 minutes with the video).

    python3 tools/regen_examples.py                 all three, with the 3D-animated video
    python3 tools/regen_examples.py --no-video      minutes instead of half an hour
    python3 tools/regen_examples.py --only sample_sensor

Boards: the bundled sample (examples/sample_sensor) and KiCad's own demos pic_programmer and video, found
next to kicad-cli (macOS app bundle, /usr/share/kicad/demos, Windows install) or in --demos.
Needs KiCad 9+ (renders) and ffmpeg (video).
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


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
    else:
        print("KiCad demos not found (use --demos); only the sample board is regenerated")
    if not os.path.exists(boards["sample_sensor"]):
        subprocess.run([sys.executable, os.path.join(ROOT, "examples", "make_sample.py")], check=True)
    for name, board in boards.items():
        if a.only and name not in a.only:
            continue
        out = os.path.join(ROOT, "docs", "examples", name)
        shutil.rmtree(out, ignore_errors=True)
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


if __name__ == "__main__":
    sys.exit(main())
