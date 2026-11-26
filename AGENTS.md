# AGENTS.md

Instructions for AI coding agents (Claude Code, Codex, Cursor, Copilot, Gemini CLI, ...) working with this
repository. People: see [README.md](README.md).

PCB TestKit is a KiCad plugin + CLI + MCP server (Python standard library only) that turns a KiCad board into a
production-test kit: weak-point analysis, fab class checks, test plan, pogo-pin fixture PCB, solder jig,
Raspberry Pi runner, HTML report/guide and a 3D-animated explainer video.

## 1. If you were asked to *use* it on a board

Don't read the source. Run `python3 -m pcbtestkit guide` (about 750 tokens) and follow it, or read
[skills/pcbtestkit/SKILL.md](skills/pcbtestkit/SKILL.md). In short:

```bash
python3 -m pcbtestkit api get_overview --project BOARD --compact          # ~1 KB
python3 -m pcbtestkit api get_open_decisions --project BOARD --compact    # what needs judgement + example_args
python3 -m pcbtestkit api <set_* tool> --project BOARD --args '<json>' --compact
python3 -m pcbtestkit api validate --project BOARD --compact
python3 -m pcbtestkit run BOARD -q --no-visuals                          # seconds
```

Never read generated files whole (`*-spec.json`, `*.html`, `*.kicad_pcb`, `*.stl`, `docs/examples/**`); use
`read_output` with a JSON pointer. Use names exactly as the tools return them.

## 2. If you are changing the code

### Commands

| Task | Command |
|---|---|
| Run all tests (about 30 s) | `python3 -m unittest discover -s tests` |
| One test class | `python3 -m unittest tests.test_toolkit.TestAnimation -v` |
| Regenerate reference docs after changing CLI options, tools or config defaults | `python3 tools/gen_docs.py` (CI runs `--check`) |
| Validate class-system JSON | `python3 -m pcbtestkit classes validate` |
| Build the KiCad Plugin Manager zip | `python3 tools/build_pcm.py` (`--check` validates only) |
| Install into the local KiCad for a live test | `python3 install.py --version 10.0 --no-ipc`, then Tools > External Plugins > Refresh Plugins |
| Regenerate the published examples (slow: about 35 min with video) | see CONTRIBUTING.md |

### Repository map

| Path | What |
|---|---|
| `__init__.py`, `plugin/` | KiCad SWIG action plugin (toolbar button + wx dialog). Relative imports only. |
| `ipc_main.py`, `plugin.json` | KiCad 9+/11 IPC-API entry point (not in the PCM zip yet). |
| `pcbtestkit/pcb.py`, `sexpr.py`, `schematic.py` | KiCad file parsers (S-expressions, KiCad 6-10). |
| `pcbtestkit/project.py`, `config.py` | `Design` (board + schematic + `pcbtestkit.json`), defaults. |
| `pcbtestkit/analysis.py`, `classify.py`, `measure.py`, `geometry.py` | Weak-point checks, part/net classification, board measurement streams. |
| `pcbtestkit/classsys/` | Fab class-system engine; one JSON per system in `systems/`. Data, not code. |
| `pcbtestkit/testplan.py`, `fixture.py`, `router.py`, `jig.py`, `kicad_writer.py` | Test plan, fixture PCB writer, its two-layer autorouter, solder-jig mesher, KiCad file writer. |
| `pcbtestkit/facility.py`, `facilities/` | Test facilities (the tester a fixture plugs into: connector, plate, buses, runner driver). Data, not code; see `docs/FACILITIES.md`. |
| `pcbtestkit/spec.py`, `report.py`, `guide.py`, `visuals.py` | Spec JSON, HTML report, visual guide, KiCad 3D renders. |
| `pcbtestkit/animate.py`, `video.py`, `video_frames.py`, `video_job.py` | 3D-animated explainer (kicad-cli per frame, ray-tracing presets), storyboard, wx captions, the background job with its progress window. |
| `pcbtestkit/phases.py`, `timing.py` | What needs the firmware (test phases, batch programmed at once); production timing per batch size. |
| `pcbtestkit/agent.py` | The tool registry (`@tool`), `Workspace`, guardrails, profiles. 1,500 lines: grep, don't read whole. |
| `pcbtestkit/mcp_server.py`, `cli.py`, `quickref.py` | MCP server, CLI, the agent quick reference. |
| `pcbtestkit/runner/` | Raspberry Pi / MCU test runner copied into every output. |
| `skills/pcbtestkit/` | Agent skill. `references/{cli,tools,config}.md` are generated. |
| `.claude-plugin/` | Claude Code plugin + marketplace manifests (skill + MCP server). |
| `pcm/`, `tools/` | KiCad package metadata + icon; build, docs, icon and owner scripts. |
| `docs/` | Human docs; `docs/reference/` is generated; `docs/examples/` are generated outputs. |
| `tests/test_toolkit.py`, `examples/make_sample.py` | Tests on a generated sample board with seeded weak points. |

### Rules

- **Standard library only** in `pcbtestkit/`, `plugin/` and `tools/`. KiCad's bundled Python has no extra packages.
- **Python 3.9 compatible**: the plugin runs inside KiCad's Python 3.9. No `match`, no `X | Y` at runtime, no
  3.10+ stdlib APIs. `from __future__ import annotations` is fine.
- **Relative imports only** inside the plugin: the KiCad Plugin Manager installs it under a package name derived
  from the identifier (`com_github_<owner>_pcbtestkit`).
- **No absolute paths in output files.** Outputs are shared; write file names relative to the project/output folder.
- **Tools never raise**: `Workspace.call` returns `{"ok": false, "error": {"code", "message", "hint"?}}`. New tools
  use the `@tool` decorator in `agent.py`, validate their arguments, and keep descriptions to one or two sentences
  (every MCP client sends the tool list with each request).
- **Writes go through `Workspace.apply`** (dry_run, confirm, audit log, persistent undo). Never edit
  `pcbtestkit.json` directly from a tool.
- **Docs are generated** where marked: change the code, then run `python3 tools/gen_docs.py`.
- **Class systems are data**: a new fab is a JSON file with test vectors, validated by `classes validate`.
  Engine changes belong in `pcbtestkit/classsys/engine.py` with a test.
- Video/animation code may only import `wx` in `video_frames.py` and in `video_job.py`'s own process (both run as
  separate processes with KiCad's Python; never import wx from the pipeline or the plugin's code paths).

### Definition of done

1. `python3 -m unittest discover -s tests` passes.
2. `python3 tools/gen_docs.py --check` and `python3 tools/build_pcm.py --check` pass.
3. New behaviour has a test; user-visible changes are in `CHANGELOG.md` and the README.
4. No personal data or absolute paths in committed files.

## 3. Token budget (for any agent)

- `python3 -m pcbtestkit tools --brief` (one line per tool) and `tools --name X` (one schema) instead of
  `docs/reference/tools.md` (18 KB) unless you need everything.
- MCP: `--profile core` exposes 16 tools (about 3k tokens of tool definitions) instead of 34 (about 6k).
- Large files to avoid reading whole: `pcbtestkit/agent.py`, `pcbtestkit/analysis.py`, `pcbtestkit/pcb.py`,
  everything under `docs/examples/`, any `.kicad_pcb`, `.stl`, `.mp4`, `.html`.
