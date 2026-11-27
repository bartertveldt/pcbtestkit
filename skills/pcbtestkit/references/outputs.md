# Outputs and how to read them cheaply

`pcbtestkit run P` (or the `generate_outputs` tool) writes to `<project>/production_test/` unless `--out` /
`out_dir` says otherwise. The command prints the paths; it never prints file contents.

| File | What it is | How an agent should read it |
|---|---|---|
| `<board>-spec.json` | "Good unit" specification: board, technology class, fab limits and measured stats, BOM, nets, supply, acceptance criteria, findings, decision log | `read_output` with a pointer (below) |
| `<board>-testplan.json` | Probes, channels, dividers, header pinout, ordered test sequence, coverage | `read_output file=testplan`, or `api get_testplan --args '{"section":"summary"}'` |
| `<board>-fixture.kicad_pcb` (+ `.kicad_pro`) | Pogo-pin fixture PCB, placed and routed, with every board's nest and clamp holes | Don't read. Open in KiCad. |
| `<board>-fixture-nest.stl`, `-press-plate.stl`, `-clamp-adapter.stl` (+ `.png`) | Every board's nest, press plate and toggle-clamp adapter (print one each per board) | `read_output file=fixture pointer=/nest` |
| `<board>-fixture.json`, `-fixture-probes.csv` | Fixture sites, channel map, every pogo position | `read_output file=fixture` |
| `<board>-jig.stl`, `.scad`, `.png`, `.json` | Holding jig (3D print), editable OpenSCAD, pocket map, metadata of both jigs | `read_output file=jig` |
| `<board>-jig-placement.stl`, `.png` | Placement jig of the two-jig workflow (PCB top side up while fitting parts) | `read_output file=jig pointer=/placement` |
| `jig-fit-check/*.stl` | The board with its parts and each jig in its working position, in KiCad's 3D coordinates (for viewing) | `read_output file=jig pointer=/fit` (the check itself) |
| `<board>-report.html` | All findings with a board map | Don't read (large). Use `list_findings`. |
| `<board>-guide.html` | Visual step-by-step guide with KiCad renders | Don't read (large, embedded images). |
| `<board>-explainer.mp4` | 3D-animated explainer (only with `--video`) | Binary. |
| `runner/` | Raspberry Pi / MCU test runner + `testplan.json` + `hardware.json` | Read `runner/README.md` if the user sets up the tester. |
| `visuals/` | KiCad 3D renders used by the guide | Binary. |
| `visuals/video-job.json` | Status of a background video job: state, frames done/total, time left | Small: read it to report progress. |
| `visuals/scenes/video/BUILD-VIDEO.md` | Only when the video failed: what is there and the exact commands to finish it | Small: read and follow it. |

## `read_output` pointers

```bash
pcbtestkit api read_output --project P --args '{"file":"spec","pointer":"/electrical/supply","max_chars":1500}' --compact
```

| File | Useful pointers (small) | Large (add `max_chars`, or page into them) |
|---|---|---|
| spec | `/board`, `/summary`, `/technology/required`, `/technology/ordered`, `/manufacturing/fab_limits`, `/electrical/supply`, `/electrical/power_nets`, `/acceptance_criteria` | `/assembly/components`, `/electrical/nets`, `/findings`, `/manufacturing/measured`, `/decision_log` |
| testplan | `/probe_side`, `/probe_side_reason`, `/pogo`, `/adc`, `/supply`, `/coverage`, `/header`, `/warnings`, `/batch`, `/program`, `/facility`, `/firmware/programmed`, `/timing/per_board_s`, `/timing/suggested_batch`, `/timing/test`, `/timing/assembly` | `/probes`, `/sequence` (use `/probes/0`, `/sequence/2`, ...), `/firmware/firmware_nets`, `/timing/by_batch_size`, `/timing/test/timeline` |
| fixture | `/pogo_count`, `/outline_mode`, `/warnings`, `/sites`, `/nest/heights`, `/nest/bom` | `/channel_map`, `/nets` |
| jig | `/fit/ok`, `/fit/summary`, `/workflow`, `/jig_size_mm`, `/support_height_mm`, `/parts_to_place`, `/notes`, `/mesh`, `/placement/lead_clearance_mm`, `/placement/stops` | `/pockets`, `/posts`, `/fit/side_play` |

Pointers follow RFC 6901: `/a/b/0`. A missing pointer returns `BAD_POINTER` with the keys that exist.
Prefer the live tools over files when possible: `get_overview`, `list_findings`, `get_testplan`, `check_classes`
answer from the board directly and don't need a prior `run`.

## A test environment's folder (`environment build --out DIR`)

Read it with the tools, not the files: `get_environment` (stations, layout, what limits the boards, the best
upgrade options, about 4 KB; `board=NAME` for one board), `plan_environment_upgrade` for what-ifs. The files, if
you must: `environment.json` (`summary`, `stations[].requirements`, `layouts`, `boards[]`), `constraints.json`
(`boards[]`: `limits`, `limited_by`, `next`, `to_fit`), `upgrades.json` (`options[]` with `id`, `change`, `gain`;
`paths`), `ranking.csv`. Never read `index.html`, the station boards or anything under `stations/` whole.
