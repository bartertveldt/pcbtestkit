# Changelog

All notable changes. Versions follow [Semantic Versioning](https://semver.org).

## 1.1.0 (unreleased)

### Batches
- `batch.count` boards are handled together (dialog: *Boards per batch*; CLI: `run --batch N`; tool:
  `set_batch_and_programming`). The fixture gets one site per board (S1..SN, columns automatic or `batch.cols`),
  each with its own pogo pins, header, power terminal and programming connector.
- The placement jig, holding jig and paste-print nest hold the whole batch side by side; the two stacking jigs share
  one board pitch, so they still close over every board (checked). When a jig for the whole batch does not fit the
  printer bed (`batch.bed_mm`), each jig holds fewer boards and the guide says how many sets to print. The SCAD
  repeats the single-board jig.
- The runner tests the boards of a batch one after the other: serial number, ADC inputs and power switch per site
  (`hardware.json` gets one power entry per site), a verdict per board and the site in the results; `--site N`
  tests one board. `fixture.panel` still works for a single-board batch.

### Programming on the fixture
- Programming / debug headers are found by what their pins carry (SWD, JTAG, AVR ISP, UPDI, PDI, PIC ICSP, UART
  bootloader with BOOT/EN) or by their name (SWD, JTAG, ISP, ICSP, PROG, DEBUG, Tag-Connect), or set in
  `program.headers`. Interfaces that merely look similar (SPI with a reset line, a UART on a larger connector) are not
  taken for one unless their name says so.
- Every pin of a programming header is probed: on the header's own pads, with finer P50 pins where the pads are
  closer than the fixture's pogo spacing (1.27 mm Tag-Connect / Cortex headers); a pin whose net is already probed is
  wired to that probe.
- Each fixture site gets a programming connector (`PRG1`, ...) wired to those probes: by default a copy of the
  board's own header footprint (its cable fits, a Tag-Connect cable included), or a standard pinout (`swd10`,
  `jtag20`, `avr_isp6`, `pic_icsp6`) or a labelled 1 x N header (`pins`).
- Test step `SP` programs each board: the runner runs `program.command` per board (`{site}`, `{serial}`; needs
  `--allow-commands`) or asks the operator to program it. The guide shows each connector's pinout.

### Routed fixture boards
- The fixture board comes out routed: a built-in two-layer router (standard library only, the same tracks on every
  run) with 45-degree tracks, vias, wider GND/VIN tracks and KiCad's default rules. Every track and via is checked
  with exact geometry before it is written; a connection it cannot make is listed in the warnings, the report and
  the guide and stays as ratsnest. KiCad 10's DRC finds no violations or unconnected items on the sample,
  pic_programmer and video fixtures, panels and two-plate fixtures.
- Divider pairs now sit above their own header pin (staggered over a few rows) with the probe on top and GND at the
  bottom, so channel nets run straight down to the header.
- Options `fixture.route`, `track_mm`, `power_track_mm`, `clearance_mm`, `via_mm`, `via_drill_mm` (dialog: *Route the
  fixture board*; CLI: `run --no-route`; tool: `set_fixture_options`). Routing stats are in `<board>-fixture.json`.

### Stacked boards
- Boards stacked on the board under test (daughter board, shield, display on headers or board-to-board
  connectors) are declared in `stack`, the new *Stacked boards* dialog tab or the `set_stacked_boards` tool:
  parts, outline, height, fitted during the test or after it. Missing outline/height are estimated and flagged.
- Fitted during the test: no probes under it, a window in the fixture plate when it is taller than the probe
  clearance, a visual check that it is seated, and `TEST-STACK-ACCESS` for nets it hides. Fitted after the test:
  the board is tested bare and the guide says when to fit it.
- Pin sockets, board-to-board/mezzanine connectors and board footprints (Arduino shield, Raspberry Pi HAT, ...)
  are offered as candidates in the open decisions and the dialog.

### Probing both sides
- `testplan.probe_side: "both"`: a two-plate (clamshell) fixture; each net is probed on the side with the best
  target, each plate is its own `.kicad_pcb` with its own header and probes CSV, channel numbers are shared.
  `auto` suggests it when rails can only be reached from the other side.

### Solder jigs for both sides
- Assembly passes: SMD side 1, SMD side 2, then one hand-solder pass per side. Hand-soldered parts on the bottom
  get their own placement and holding jig (`-jig-bottom*.stl`), made from the board turned over; a later pass has
  pockets for every part already fitted and reliefs for the soldered leads of the earlier one.
- Paste-print nest for the second SMD side (`-paste-nest-<side>.stl`): pockets for the first side's parts and a
  frame flush with the board for the stencil.
- New jig options `sides`, `smt_first`, `paste_nest`, `lead_relief_mm`, `nest_border_mm`; the dialog's jig tab
  lists parts on both sides. Bottom-side 3D model bodies are now mirrored like KiCad does.

### Guide renders
- The two-jig workflow in the guide has a KiCad render under each step: both jigs printed, the PCB top side up in
  the placement jig with the parts above their holes, the holding jig coming down, the stack turned over with the
  placement jig lifted off, and soldering from above. They come from the same scenes as the explainer video. The
  single-jig pictures (parts in pockets, PCB lowered onto them) are only shown for `jig.workflow: "single"`.
- "Board above the fixture" is rendered from a lower camera with the board higher up, so the pogo pins show under
  their test points instead of hiding behind the board.
- Jig render meshes have every wall facing outwards: a jig shown turned over (holding jig on top, the stack after
  turning, the placement jig lifted off) no longer renders with black walls, in the guide and in the video.

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
