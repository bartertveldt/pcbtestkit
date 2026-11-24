# Changelog

All notable changes. Versions follow [Semantic Versioning](https://semver.org).

## 1.0.0 (unreleased)

First public release.

### KiCad plugin
- Toolbar action with a settings dialog (outputs, test & fixture, solder jig, technology class, AI assistant tabs).
- Installs through KiCad's Plugin and Content Manager (*Install from File...* with the release zip) or `install.py`.
- KiCad 9+/11 IPC-API entry point (`plugin.json`, `ipc_main.py`) for manual installs.

### Analysis and outputs
- Weak-point analysis: fab limits, acid traps, MLCCs near edges, tombstoning, via-in-pad, thermal relief, heat
  near electrolytics, SMD-only connectors, derating, decoupling, ESD, floating inputs, BOM gaps, testability;
  KiCad DRC/ERC merged in when `kicad-cli` is available.
- Technology-class framework with 8 built-in systems: Eurocircuits 2024, JLCPCB, PCBWay standard, OSH Park,
  AISLER (all six stack-ups), Würth BASIC, IPC-2221 voltage spacing, IPC-6012 annular ring. New fabs are JSON
  files with test vectors, validated by `pcbtestkit classes validate`.
- Good-unit spec JSON, test plan, pogo-pin fixture PCB (single or panel, rect or outline), 3D-printable solder
  jig (STL + OpenSCAD), Raspberry Pi / MCU test runner, HTML report, visual guide with KiCad 3D renders.
- Explainer video with real 3D animation rendered frame by frame by KiCad: parts placed, jig printed and loaded,
  PCB slid onto the jig, board turned over, pogo pins fitted, board pressed onto the fixture.

### AI and automation
- 31 validated tools (agent API) with dry runs, confirmations, an audit log and persistent undo.
- CLI: `pcbtestkit api <tool>`, `tools --brief/--name`, `guide` (agent quick reference), `classes`, `mcp`.
- MCP server: tool profiles (`full`, `core`, `readonly`), annotations, resources, resource templates,
  prompts with arguments, argument completion.
- Agent skill (`skills/pcbtestkit`), `AGENTS.md`, `llms.txt`, Claude Code plugin + marketplace manifests.
- Reference docs generated from the code (`tools/gen_docs.py`).
