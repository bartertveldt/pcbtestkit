"""PCB technology-class systems: one engine, many class tables.

    from pcbtestkit import classsys as CS
    systems, warnings = CS.load_systems(cfg, project_dir)
    result = CS.evaluate(systems["eurocircuits-2024"], measurement, context, target="6C")

Layers:
* catalog.py   metrics every system is written in (measured once per board)
* values.py    units ("4mil"), expressions, conditions
* engine.py    required class, target compliance, violations with locations
* validate.py  definition checks + test vectors (run before a system is used)
* registry.py  built-in / user / project / config systems
* systems/     one JSON file per class system
"""
from .catalog import ALIASES, CONTEXT_KEYS, METRICS, STREAMS  # noqa: F401
from .engine import System, SystemError_, describe, evaluate, fab_limits, limits_for  # noqa: F401
from .registry import DEFAULT_SYSTEM, from_legacy_table, load_systems, system_id_from_config  # noqa: F401
from .validate import TEMPLATE, validate  # noqa: F401
