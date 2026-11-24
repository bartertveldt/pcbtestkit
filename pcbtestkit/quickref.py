"""The agent quick reference: the cheapest way for an AI model to drive PCB TestKit.

One text, three outlets: ``pcbtestkit guide`` (CLI), the ``pcbtestkit://cli`` MCP resource, and the
skill in skills/pcbtestkit. Keep it short - every agent that reads it pays for it in tokens.
"""
from __future__ import annotations

from . import __version__

QUICK_REFERENCE = """PCB TestKit {version} - agent quick reference (cheapest path first)

Run as `pcbtestkit ...` (pip install) or `python3 -m pcbtestkit ...` (repo/plugin folder on PYTHONPATH).
P = a .kicad_pcb, .kicad_pro or project folder. Exit codes: 0 ok, 1 findings/violations/tool error, 2 bad usage.
All `api` calls print one JSON object {{"ok": true|false, ...}}; always pass --compact.

1  LOOK (read-only, ~1 KB answers)
   pcbtestkit api get_overview --project P --compact
   pcbtestkit api get_open_decisions --project P --compact          # each item has apply_with + example_args
   pcbtestkit api list_findings --project P --args '{{"min_severity":"major","limit":10}}' --compact
   pcbtestkit classes check P                                       # which fab class systems the board fits
2  DECIDE (writes go to P's pcbtestkit.json with an audit entry; undo_last reverts, 20 steps, across calls)
   pcbtestkit api set_supply --project P --args '{{"net":"VIN_12V","voltage":12,"current_limit_a":0.5,"rationale":"J3 input"}}' --compact
   pcbtestkit api set_net_specs --project P --args '{{"updates":[{{"net":"+3V3","nominal_v":3.3,"tolerance_pct":5}}],"rationale":"LDO datasheet"}}' --compact
   Batch: one set_net_specs call takes up to 200 nets. Add "dry_run":true to preview effects.
   CONFIRM_REQUIRED -> resend with "confirm":true only if the change is intended.
3  CHECK      pcbtestkit api validate --project P --compact          # fix errors, warnings are advisory
4  GENERATE   pcbtestkit run P -q --no-visuals                       # seconds: spec, plan, fixture, jig, report, runner
              pcbtestkit run P -q                                    # + KiCad 3D renders in the guide (about 1-2 min)
              pcbtestkit run P -q --video                            # + 3D-animated explainer MP4 (5-20 min)

DISCOVER (only when needed)
   pcbtestkit tools --brief                    one line per tool
   pcbtestkit tools --name set_test_options    one tool's full JSON schema
   pcbtestkit api get_schema --args '{{"name":"test_step"}}' --compact     field docs for complex writes
   pcbtestkit classes list | classes show jlcpcb-2026 | classes catalog

TOKEN RULES
 - Never cat generated files (*-spec.json, *-report.html, *-guide.html, *.kicad_pcb, *.stl): they are large.
   Read slices: pcbtestkit api read_output --project P --args '{{"file":"spec","pointer":"/electrical/supply","max_chars":1500}}' --compact
 - Prefer `limit`/`max_items`/`min_severity` arguments over reading everything; page with `offset`.
 - Use names exactly as returned by the tools (nets, refs, finding uids); never invent them.
 - Don't re-run `run` to check a decision: validate and the write's "effects" already report coverage/channels.
 - Videos and renders cost minutes of CPU, not tokens; only request them when the user wants them.

MCP instead of CLI: `pcbtestkit mcp --project P [--profile core|full|readonly] [--read-only]`.
The same tools; `core` (16 tools) halves the tool-list tokens of `full` (31).
"""


def quick_reference() -> str:
    return QUICK_REFERENCE.format(version=__version__)
