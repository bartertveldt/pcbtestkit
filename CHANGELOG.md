# Changelog

All notable changes. Versions follow [Semantic Versioning](https://semver.org).

## 1.0.0 (2026-11-24)

First public release.

### KiCad plugin
- Toolbar action with a settings dialog: outputs and technology class, test & fixture, rails, solder jig, weak
  points, and an optional AI assistant tab. Every decision can be made in the dialog without AI: fixture mode,
  rail voltages and tolerances, rails to test, supply and idle-current limits, part heights, accepted weak points.
- Installs through KiCad's Plugin and Content Manager (*Install from File...* with the release zip) or `install.py`.
- KiCad 9+/11 IPC-API entry point (`plugin.json`, `ipc_main.py`) for manual installs.

### Analysis and outputs
- Weak-point analysis: fab limits, acid traps, MLCCs near edges, tombstoning, via-in-pad, thermal relief, heat
  near electrolytics, SMD-only connectors, derating, decoupling, ESD, floating inputs, BOM gaps, testability;
  KiCad DRC/ERC merged in when `kicad-cli` is available.
- Technology-class framework with 8 built-in systems: Eurocircuits 2024, JLCPCB, PCBWay standard, OSH Park,
  AISLER (all six stack-ups), Würth BASIC, IPC-2221 voltage spacing, IPC-6012 annular ring. New fabs are JSON
  files with test vectors, validated by `pcbtestkit classes validate`. When a board fits no class, the report
  names the nearest one and what blocks it, including order options such as "4 layers only with ENIG".
- Good-unit spec JSON, test plan, pogo-pin fixture PCB (single or panel, rect or outline), Raspberry Pi / MCU
  test runner, HTML report, visual guide with KiCad 3D renders.
- Fixture modes: generate the fixture board, design your own from a probe-point CSV (`fixture.mode: "own"`), or
  no fixture with a manual test (`"none"`).
- Deterministic outputs: the same board and settings give byte-identical jig STLs and fixture boards on every run
  (no random UUIDs); `SOURCE_DATE_EPOCH` also pins the generation time in the spec and report.
- Two-jig soldering workflow (default): a placement jig holds the PCB top side up (lead wells, locating stops,
  bottom-part pockets) while the parts are fitted; the holding jig (STL + OpenSCAD) closes over them with rim
  notches at the stops; the stack is turned over and soldered. Every export checks that the two jigs never
  touch. `jig.workflow: "single"` keeps the one-jig method.
- Explainer video with real 3D animation rendered frame by frame by KiCad: parts placed, both jigs printed,
  parts fitted in the placement jig, holding jig closed, stack turned over, placement jig lifted off, leads
  soldered with an iron, pogo pins fitted, board pressed onto the fixture.

### AI and automation
- 31 validated tools (agent API) with dry runs, confirmations, an audit log and persistent undo.
- CLI: `pcbtestkit api <tool>`, `tools --brief/--name`, `guide` (agent quick reference), `classes`, `mcp`.
- MCP server: tool profiles (`full`, `core`, `readonly`), annotations, resources, resource templates,
  prompts with arguments, argument completion.
- Agent skill (`skills/pcbtestkit`), `AGENTS.md`, `llms.txt`, Claude Code plugin + marketplace manifests.
- Reference docs generated from the code (`tools/gen_docs.py`).
