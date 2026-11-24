#!/usr/bin/env python3
"""Install PCB TestKit into KiCad's plugin folders.

    python3 install.py                 install for every KiCad version found (6.0 ... 10.0, 11 = IPC)
    python3 install.py --version 9.0   install for one version (folder is created if needed)
    python3 install.py --uninstall

SWIG action plugin (KiCad 6-10): <documents>/KiCad/<ver>/scripting/plugins/PCBTestKit
IPC plugin (KiCad 9+, needed on 11): <documents>/KiCad/<ver>/plugins/PCBTestKit
On Linux the base folder is ~/.local/share/kicad/<ver>.
After installing: PCB Editor > Tools > External Plugins > Refresh Plugins (or restart KiCad).
"""
import argparse
import glob
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "PCBTestKit"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "tests", "examples", "docs", "production_test", ".git*", "*.egg-info",
                                "skills", "tools", "pcm", "dist", "build", ".claude-plugin", ".pcbtestkit")


def bases():
    home = os.path.expanduser("~")
    if sys.platform.startswith("linux"):
        return [os.path.join(home, ".local", "share", "kicad")]
    docs = [os.path.join(home, "Documents", "KiCad")]
    if sys.platform == "win32":
        od = os.path.join(home, "OneDrive", "Documents", "KiCad")
        docs.append(od)
    return docs


def versions(base):
    out = []
    for d in glob.glob(os.path.join(base, "*")):
        v = os.path.basename(d)
        try:
            float(v)
            out.append(v)
        except ValueError:
            pass
    return sorted(out, key=float)


def copy(dst):
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(HERE, dst, ignore=IGNORE)
    print("  installed ->", dst)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", help="KiCad version folder, e.g. 9.0")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--no-ipc", action="store_true", help="skip the IPC plugin for KiCad 9/10")
    a = ap.parse_args()
    done = 0
    for base in bases():
        vers = [a.version] if a.version else versions(base)
        for v in vers:
            major = float(v)
            targets = []
            if major < 11:
                targets.append(os.path.join(base, v, "scripting", "plugins", NAME))
            if major >= 11 or (major >= 9 and not a.no_ipc):
                targets.append(os.path.join(base, v, "plugins", NAME))
            for t in targets:
                if a.uninstall:
                    if os.path.isdir(t):
                        shutil.rmtree(t)
                        print("  removed", t)
                else:
                    os.makedirs(os.path.dirname(t), exist_ok=True)
                    copy(t)
                done += 1
    if not done:
        print("No KiCad version folders found. Start KiCad once, or pass --version 9.0 (for example).")
        return 1
    print("Done. In the PCB Editor: Tools > External Plugins > Refresh Plugins.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
