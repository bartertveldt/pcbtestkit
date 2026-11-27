"""Settings that hold for every project: ~/.pcbtestkit/settings.json (PCBTESTKIT_HOME moves the folder).

    {"environment": {"path": "/.../test-environment", "station": "auto", "batch": "auto"},
     "power": {"after": "sleep", "keep_awake": true, "lid": false, "grace_s": 120}}

The remembered test environment is used by every board whose pcbtestkit.json does not choose one itself
(``environment.path`` "" = the remembered one, "none" = none for this board). ``power`` is what the KiCad dialog
for many boards chose last for long runs (see pcbtestkit.power).
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, Optional


def home() -> str:
    return os.environ.get("PCBTESTKIT_HOME") or os.path.join(os.path.expanduser("~"), ".pcbtestkit")


def path() -> str:
    return os.path.join(home(), "settings.json")


def load() -> Dict[str, Any]:
    try:
        with open(path(), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save(d: Dict[str, Any]) -> None:
    os.makedirs(home(), exist_ok=True)
    tmp = path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path())


def remembered_environment() -> Optional[Dict[str, Any]]:
    env = load().get("environment")
    return env if isinstance(env, dict) and env.get("path") else None


def remember_environment(env: Optional[Dict[str, Any]]) -> None:
    """Remember a test environment for every future build (None forgets it)."""
    d = load()
    if env and env.get("path"):
        d["environment"] = {"path": os.path.abspath(env["path"]), "station": env.get("station") or "auto",
                            "batch": env.get("batch") if env.get("batch") not in (None, "") else "auto"}
    else:
        d.pop("environment", None)
    save(d)


def power_setting() -> Dict[str, Any]:
    """The power choices for long runs the KiCad dialog used last ({} before the first)."""
    p = load().get("power")
    return dict(p) if isinstance(p, dict) else {}


def remember_power(cfg: Dict[str, Any]) -> None:
    d = load()
    d["power"] = {k: cfg[k] for k in ("after", "keep_awake", "lid", "grace_s") if k in cfg}
    save(d)


def cache_file(kind: str, key: str) -> str:
    """A cache file under the settings folder (nothing is written into the projects)."""
    return os.path.join(home(), "cache", kind, hashlib.sha1(key.encode()).hexdigest()[:20] + ".json")
