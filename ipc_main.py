"""IPC-API entry point (KiCad 9+ plugin.json action; required for KiCad 11, where SWIG is gone).

Runs the full export for the board open in the PCB editor using the settings
in pcbtestkit.json and opens the HTML report. Interactive settings live in
the SWIG dialog (KiCad 6-10) or are made by editing pcbtestkit.json / via the
AI agent tools.
"""
import os
import sys
import pathlib
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def board_path() -> str:
    from kipy import KiCad  # provided by requirements.txt (kicad-python)
    kicad = KiCad(client_name="pcbtestkit")
    board = kicad.get_board()
    name = board.name
    if os.path.isabs(name) and os.path.exists(name):
        return name
    try:
        proj = board.get_project()
    except Exception:
        proj = kicad.get_project(board.document)
    base = proj.path
    if base.endswith(".kicad_pro"):
        base = os.path.dirname(base)
    cand = os.path.join(base, os.path.basename(name))
    if not cand.endswith(".kicad_pcb"):
        cand += ".kicad_pcb"
    return cand


def main() -> int:
    from pcbtestkit.pipeline import run_pipeline
    from pcbtestkit.project import Design
    path = board_path()
    if not os.path.exists(path):
        print("PCB TestKit: board file not found (save the board first): %s" % path)
        return 1
    res = run_pipeline(Design.load(path), progress=lambda m, f: print("[%3d%%] %s" % (int(f * 100), m)))
    print("PCB TestKit: %s, score %d -> %s" % (res["summary"]["verdict"], res["summary"]["score"], res["out_dir"]))
    page = res["paths"].get("guide") or res["paths"].get("report")
    if page:                        # a proper file:// URL (Windows drive letters, spaces in folder names)
        webbrowser.open(pathlib.Path(page).resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
