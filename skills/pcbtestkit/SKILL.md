---
name: pcbtestkit
description: Use for KiCad boards (.kicad_pcb/.kicad_pro) when the user wants a DFM or weak-point review, to know which fab or technology class to order (Eurocircuits, JLCPCB, PCBWay, OSH Park, AISLER, Würth, IPC-2221/6012), a production test plan, a pogo-pin test fixture PCB, a 3D-printed solder jig, a Raspberry Pi test runner, or an assembly/test explainer video. Drives the PCB TestKit CLI with small JSON calls.
license: MIT
---

# PCB TestKit

PCB TestKit analyses a KiCad board and generates its production-test kit. You make the judgement calls
(rail voltages, supply, what to test, which weak points to accept); the tool does all geometry and file
generation. Work through the CLI below: it costs no tokens until used and every answer is compact JSON.

## Run it

```bash
pcbtestkit <command> ...                                  # if installed (pip install pcbtestkit)
python3 <this-skill-dir>/scripts/ptk.py <command> ...    # otherwise (repo clone or Claude Code plugin)
```

`P` below = the board: a `.kicad_pcb`, `.kicad_pro` or the project folder. Always add `--compact` to `api` calls.
Exit codes: 0 ok, 1 findings/violations/tool error (the JSON says which), 2 bad usage.

## Pick the shortest path for the request

| User wants | Do exactly this |
|---|---|
| "Is this board OK / what's wrong?" | `api get_overview` then `api list_findings --args '{"min_severity":"major","limit":10}'`. Summarise; stop. |
| "Which fab / class should I order?" | `classes check P` (all systems), or `classes check P --system jlcpcb-2026 --target standard` (exit 1 = violations, listed with locations). |
| "Prepare production test / fixture / jig" | The full workflow below. |
| "Just generate the files" | `api validate`; if no errors: `run P -q --no-visuals` (seconds). Add renders by dropping `--no-visuals`. |
| "Make the explainer video" | Confirm first: it renders 3D animation with KiCad for 5-20 min. Then `run P -q --video`. |
| "Add fab X's design rules" | See [references/class-systems.md](references/class-systems.md). |

## Full workflow (production test)

1. `pcbtestkit api get_overview --project P --compact` → board, rails, supply, readiness, `open_decisions`.
2. `pcbtestkit api get_open_decisions --project P --compact` → each item has `question`, evidence, `apply_with` (the tool) and ready `example_args`.
3. Resolve each decision with its tool. Batch: one `set_net_specs` takes many nets. Give a one-sentence `rationale`. Use part datasheet knowledge for voltages/tolerances; ask the user when a value is genuinely unknown.
   ```bash
   pcbtestkit api set_supply --project P --args '{"net":"VIN_12V","voltage":12,"current_limit_a":0.5,"rationale":"main input J3"}' --compact
   pcbtestkit api set_net_specs --project P --args '{"updates":[{"net":"+3V3","nominal_v":3.3,"tolerance_pct":5},{"net":"+5V","nominal_v":5,"tolerance_pct":5}],"rationale":"regulator datasheets"}' --compact
   ```
   Each write answers with `effects` (e.g. coverage before/after): read them instead of re-querying.
4. `pcbtestkit api validate --project P --compact` → fix every error; warnings are advisory.
5. `pcbtestkit run P -q --no-visuals` (or without `--no-visuals` for the visual guide's renders).
6. Report in a few lines: decisions made, coverage, output folder (`<project>/production_test`), what a human must check.

## Rules that save tokens and avoid mistakes

- Never `cat`/read generated files (`*-spec.json`, `*-report.html`, `*-guide.html`, `*.kicad_pcb`, `*.stl`): they are large. Read slices: `api read_output --args '{"file":"spec","pointer":"/electrical/supply","max_chars":1500}'`. Pointer map: [references/outputs.md](references/outputs.md).
- Use `limit`, `offset`, `min_severity`, `max_items` instead of listing everything.
- Use net names, refs and finding uids exactly as the tools return them. Unknown names fail with `did_you_mean`.
- `CONFIRM_REQUIRED`: resend with `"confirm":true` only if the change is really intended. `"dry_run":true` previews any write.
- `undo_last` reverts the previous write (20 steps, kept across calls). Decisions live in `P/pcbtestkit.json` with an audit log.
- Don't re-run `run` to check a decision. Don't request renders/video unless the user wants them (CPU minutes).
- Unsure of a tool's arguments: `pcbtestkit tools --name <tool>` (one schema) rather than the full reference.

## References (open only when needed)

- [references/tools.md](references/tools.md): every tool and argument (generated from the code).
- [references/cli.md](references/cli.md): every command and option, incl. `run`, `classes`, `mcp`.
- [references/config.md](references/config.md): every `pcbtestkit.json` key with defaults.
- [references/outputs.md](references/outputs.md): what each output file holds and JSON pointers into it.
- [references/class-systems.md](references/class-systems.md): fab class systems; adding a fab without code.
- [references/troubleshooting.md](references/troubleshooting.md): error codes, missing KiCad/ffmpeg, Windows.
