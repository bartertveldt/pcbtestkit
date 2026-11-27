#!/usr/bin/env python3
"""Regenerate the published examples in docs/examples (slow: about 35 minutes with the video).

    python3 tools/regen_examples.py                 all three, with the 3D-animated video
    python3 tools/regen_examples.py --no-video      minutes instead of half an hour
    python3 tools/regen_examples.py --only sample_sensor
    python3 tools/regen_examples.py --only station  the example test station with four board types
    python3 tools/regen_examples.py --only environment   a test environment for 128 boards (about an hour)

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
    ap.add_argument("--environment-from", help="environment: publish an existing build of the catalog (its folder) "
                                                "instead of building it again")
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
    if demos and (not a.only or "environment" in a.only):
        environment(demos, a.no_video, a.environment_from)
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
        if name != "sample_sensor":             # the fit-check STLs of a big board are large: one example has them
            cmd.append("--no-jig-fit-files")
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
            [os.path.join(out, "station_video"), os.path.join(out, "station_video_parts")]:
        shutil.rmtree(junk, ignore_errors=True)
    for prl in glob.glob(os.path.join(out, "*", "*.kicad_prl")) + glob.glob(os.path.join(out, "*", ".*.kicad_prl")) + \
            glob.glob(os.path.join(out, "*", ".pcbtestkit-station.json")):
        os.remove(prl)                          # KiCad's local settings, the run's resume stamps
    print("== station", "OK" if r.returncode in (0, 1) else "FAILED (exit %d)" % r.returncode)


# the environment example: 110 generated boards + KiCad's demos; the repository keeps the environment and station
# level and a few complete board folders (all 119 boards' outputs are about 2.7 GB)
ENV_KEEP = 3


def environment(demos: str, no_video: bool, built: str = None) -> None:
    out = os.path.join(ROOT, "docs", "examples", "environment")
    if not built:
        sys.path.insert(0, os.path.join(ROOT, "examples"))
        import make_collection
        col = os.path.join(tempfile.mkdtemp(prefix="pcbtk_catalog_"), "generated")
        make_collection.main(col, count=110, seed=7)
        built = tempfile.mkdtemp(prefix="pcbtk_env_")
        vid = ["--video", "none"] if no_video else []
        print("== environment: 110 generated boards (about an hour), then KiCad's demos added ...", flush=True)
        subprocess.run([sys.executable, "-m", "pcbtestkit", "environment", "build", col, "--out", built,
                        "--name", "PCB TestKit catalog", "-q"] + vid, cwd=ROOT)
        subprocess.run([sys.executable, "-m", "pcbtestkit", "environment", "add", built, demos, "-q"] + vid, cwd=ROOT)
    publish_environment(built, out, ENV_KEEP)
    print("== environment OK:", out)


def publish_environment(src: str, out: str, keep: int) -> None:
    """Copy an environment build for the repository: every environment and station level file, and the first
    ``keep`` board folders of each station by rank; the pages link only to the boards that are there."""
    from pcbtestkit import environment as E
    from pcbtestkit.station import station_html
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    with open(os.path.join(src, "environment.json"), encoding="utf-8") as fh:
        env = json.load(fh)
    for f in os.listdir(src):
        p = os.path.join(src, f)
        if os.path.isfile(p) and not f.startswith("environment-job") and f != "sources.json":   # sources: local paths
            shutil.copy2(p, os.path.join(out, f))
    rank = {r["folder"]: r["rank"] for r in env["ranking"]}
    kept = set()
    for st in env["stations"]:
        sdir = os.path.join(src, "stations", st["name"])
        odir = os.path.join(out, "stations", st["name"])
        os.makedirs(odir)
        for f in os.listdir(sdir):
            p = os.path.join(sdir, f)
            if os.path.isfile(p) and not f.endswith(".kicad_prl"):
                shutil.copy2(p, os.path.join(odir, f))
        mine = sorted(st["boards"], key=lambda b: rank.get(b, 10 ** 6))[:keep]
        for b in mine:
            shutil.copytree(os.path.join(sdir, b), os.path.join(odir, b),
                            ignore=shutil.ignore_patterns("*.kicad_prl", "results", "__pycache__", ".pcbtestkit*"))
            kept.add(b)
        with open(os.path.join(odir, "station.json"), encoding="utf-8") as fh:
            sj = json.load(fh)
        for row in sj["boards"]:
            if row.get("folder") not in kept:
                row.pop("outputs", None)
        with open(os.path.join(odir, "station.json"), "w", encoding="utf-8") as fh:
            json.dump(sj, fh, indent=2)
        with open(os.path.join(odir, "station.html"), "w", encoding="utf-8") as fh:
            fh.write(station_html(sj))
    for b in env["boards"]:
        if b["folder"] not in kept:
            b.pop("guide", None)
            b.pop("outputs_dir", None)
    with open(os.path.join(out, "environment.json"), "w", encoding="utf-8") as fh:
        json.dump(env, fh, indent=1)
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(E.environment_html(env))
    with open(os.path.join(out, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("# A test environment for 128 boards\n\n"
                 "Made with `pcbtestkit environment build` for 110 boards generated by `examples/make_collection.py` "
                 "(seed 7), then KiCad's demo projects added with `pcbtestkit environment add` (the station video "
                 "shows the demos after the boards it showed first). Start with [index.html](index.html), "
                 "[requirements.md](requirements.md), [constraints.md](constraints.md) (what limits every board) and "
                 "[upgrades.md](upgrades.md) (what an upgrade would gain and cost).\n\n"
                 "Everything at the environment and station level is here: the stations' KiCad boards, pages and "
                 "videos, the ranking and every board's profile. Of the boards' own outputs (guide, fixture, nest and "
                 "clamp parts, jigs, runner: about 23 MB per board, 2.7 GB in all) only the first %d of each station "
                 "are kept; `python3 tools/regen_examples.py --only environment` makes all of them.\n" % keep)


if __name__ == "__main__":
    sys.exit(main())
