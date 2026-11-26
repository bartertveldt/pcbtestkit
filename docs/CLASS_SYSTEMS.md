# Technology-class systems

PCB fabs and standards describe "what can be built" in very different ways:

* **Class tables**: Eurocircuits pattern class 3–9 × drill class A–E, or Würth BASIC Standard/Advanced.
* **Capability pages**: JLCPCB, PCBWay, OSH Park and AISLER, with limits that depend on layer count, copper weight and finish.
* **Standards**: IPC-2221 spacing per voltage, IPC-6012 class 1/2/3.

PCB TestKit handles all of them with one engine. Each system is a JSON file containing only data, and the engine, measurement and validator are shared. Adding a system means typing in its table; it needs no code.

```
board ──► measure.py ──► streams (tracks, gaps, holes, hole pairs, hole-to-copper, edge, mask, silk, ...)
                              │   measured once, independent of any class system
                              ▼
          catalog.py: 55 metrics = stream + item filter + value  (track_outer, ring_via_inner, hole_to_copper_inner, ...)
                              │
system JSON ──► validate.py ──► engine.py ──► class needed, ordered-class compliance, violations with x/y,
(levels, limits,  units, names,     │          process limits, order notes for what the design cannot show
 conditions,      level order,      ▼
 test vectors)    test vectors   findings, report, spec JSON, AI tools, CLI, KiCad dialog
```

## Using it

| Where | How |
|---|---|
| KiCad dialog | **Class system** + **Class to order** dropdowns; **Also check** boxes for IPC standards |
| CLI | `pcbtestkit classes list`, `classes check board.kicad_pcb`, `classes check board.kicad_pcb --system jlcpcb-2026 --target standard` (exit 1 on violations, for CI), `run ... --class-system jlcpcb-2026 --tech-class standard --check-class ipc-2221-voltage:B2` |
| Config | `"tech": {"system": "jlcpcb-2026", "class": "standard", "checks": ["ipc-2221-voltage:B2", "ipc-6012-ring:3"], "context": {"finish": "ENIG", "mask_color": "black"}}` |
| AI tools | `list_class_systems`, `check_classes`, `describe_class_system`, `set_analysis_options(class_system=…, tech_class=…, class_checks=…, tech_context=…)`, `add_class_system(definition)` |

The ordered class also sets the limits used by the generic `MFG-*` checks (track width, via ring, edge clearance, hole spacing, aspect ratio).

## Implemented systems

| id | What | Status | Source (checked) | Not encoded yet |
|---|---|---|---|---|
| `eurocircuits-2024` | Pattern class 3–9 × drill class A–E, pooling services, base-copper limits, IPI | verified | [Eurocircuits classification](https://www.eurocircuits.com/pcb-design-guidelines/classification/) (table 07/05/2024; checked 2026-11-19) | plating-index rule |
| `jlcpcb-2026` | Standard vs small-via (surcharge) tiers; track/space by layers and copper; rings, holes, slots, hole-to-copper, mask dam, silk, board size | verified | [JLCPCB capabilities](https://jlcpcb.com/capabilities/pcb-capabilities) (2026-11-20) | HDI, flex, aluminium; inner-copper-specific track/space |
| `pcbway-standard-2026` | Standard PCB table; hole-size surcharge tier | verified | [PCBWay capabilities](https://www.pcbway.com/capabilities.html) (2026-11-20) | Advanced table (see roadmap) |
| `oshpark-2026` | 2-layer, 2-layer 2 oz 0.8 mm, 4-layer, 6-layer services | verified | [OSH Park KiCad rules](https://docs.oshpark.com/design-tools/kicad/kicad-design-rules/) + service pages (2026-11-20) | Super Swift, Medium Run, After Dark |
| `aisler-2025` | All six published stack-ups: 2L 35 µm HASL/ENIG, 2L 70 µm HASL/ENIG, 4L 35 µm ENIG, 6L+ 35 µm ENIG; via/component-hole split at 0.45 mm; castellations only on ENIG; mask dam required on HASL | verified | [AISLER design rules](https://community.aisler.net/t/pcb-design-rules/41) index and its six pages (updated 2025-07-07 to 2025-12-05; checked 2026-11-21) | 6L+ board thickness and inner copper (not stated by AISLER) |
| `wurth-basic-2022` | BASIC design rules Standard/Advanced; spacing per starting foil | verified | [Würth BASIC poster v2.2 12.2022](https://www.we-online.com/files/pdf1/design-rules-poster-basic-cbt-en.pdf) (2026-11-20) | HDI, flex, heavy-copper rule sets |
| `seeed-fusion-2026` | Standard FR4 specification: track/space per copper weight (1/2/3 oz), 4-layer inner layers, via/PTH rings, hole spacing, edge, mask dam per colour, silk, board size | verified | [Seeed Fusion PCB specification](https://support.seeed.cc/portal/en/kb/articles/fusion-pcb-specification) (content updated 2026-11-20; checked 2026-11-25) | Advanced specification (inner/outer per copper weight up to 6 oz, HDI); the 8-16 layer thicknesses |
| `nextpcb-2026` | Standard capabilities: outer track/space per copper weight, 0.5 oz inner, ring per weight, 0.15 mm drill only up to 1.2 mm thick, hole-to-copper per hole type, edge (routed / V-cut), allowed thicknesses, board size by layers, mask dam per colour, silk | verified | [NextPCB PCB capabilities](https://www.nextpcb.com/pcb-capabilities) (checked 2026-11-25) | Advanced PCB page; inner 1/2 oz track/space (not stated) |
| `allpcb-2026` | 4 mil track/space, 6 mil ring, via spacing same/different net, component-hole spacing, aspect ratio 6:1 or 8:1 (one extra day), edge (routed / V-cut), thickness, board size, silk, mask bridge | verified | [ALLPCB capability](https://www.allpcb.com/standard_pcb_manufacturing_capability.html) (checked 2026-11-25) | Advanced capability pages; per-copper-weight values (not stated) |
| `multicb-2026` | Design parameters for 35 µm copper: free standard and special production (track/space, via drill, via pad, rings, aspect ratio, mask web, microvias), hole-to-copper, edge | verified | [Multi-CB design parameters](https://www.multi-circuit-boards.eu/en/pcb-design-aid/design-parameters.html) + [KiCad page](https://www.multi-circuit-boards.eu/en/support/pcb-data/kicad.html) (checked 2026-11-25) | other copper thicknesses (ask Multi-CB); layers, thickness and size limits (not stated) |
| `ipc-2221-voltage` | Table 6-1 spacing per voltage (B1–A7), environment B2/B3/B4/A5 | secondary | [EMA table](https://www.ema-eda.com/ema-resources/blog/pcb-clearance-and-creepage-distance-table/), [McGaw](https://philipmcgaw.com/creepage-and-clearance/), [Siemens](https://blogs.sw.siemens.com/electronic-systems-design/2025/04/29/pcb-high-voltage-spacing-what-every-engineer-should-know/) | check against your copy of IPC-2221B/C; the 251–300 V B2 cell is disputed (2.5 mm used) |
| `ipc-6012-ring` | Class 1/2/3 annular ring with the fab's registration; plating as an order note | partial | [Altium on IPC-6012 class 3 rings](https://resources.altium.com/p/meeting-standards-ipc-6012-class-3-annular-ring) | everything in IPC-6012 except ring and plating |

*Status* means: **verified**, every number was checked against the source on the stated date; **secondary**, reproduced from secondary sources because the standard is paywalled; **partial**, only part of the source is encoded.

## Roadmap: systems to add

Most of these are a JSON file plus test vectors. Rows marked *metric* first need a new measurement in `measure.py` / `catalog.py`.

| # | System | What is needed | Notes |
|---|---|---|---|
| 1 | PCBWay Advanced (Normal / Medium / High difficulty) | confirm the column mapping with PCBWay | The table on [capabilities.html](https://www.pcbway.com/capabilities.html) has ambiguous columns, and [advanced-pcb-capabilities.html](https://www.pcbway.com/advanced-pcb-capabilities.html) conflicts with it (e.g. via ring 3 mil vs 4–8 mil). The layout maps onto a `difficulty` dimension with copper-conditional limits |
| 2 | OSH Park Super Swift, Medium Run, After Dark | service pages | Probably the 2-layer rules with other materials or turnaround |
| 3 | JLCPCB HDI / flex / aluminium | capability page sections | HDI needs microvia metrics (*metric*: microvia drill and capture pad) |
| 4 | Multi-CB other copper thicknesses | Multi-CB's copper-thickness page | `multicb-2026` covers 35 µm only |
| 5 | Beta LAYOUT / PCB-POOL | capability page | |
| 6 | PCBgogo, Elecrow; the advanced tiers of NextPCB, Seeed Fusion and ALLPCB | capability pages | Elecrow's documents contradict each other (6 vs 4 mil tracks); confirm with Elecrow first |
| 7 | Eurocircuits services in detail (IMS, semi-flex, BINDI) | service pages | Service-specific limits beyond the pooling column |
| 8 | Würth HDI / flex / heavy copper | Würth design-rule documents | Same structure as `wurth-basic-2022` |
| 9 | IPC-2221 producibility levels A/B/C | the standard (paywalled) | Fabrication allowance per level for land size (hole + 2 × ring + allowance) |
| 10 | IPC-7351 density levels (most/nominal/least) | the standard | *metric*: footprint land-pattern geometry vs component; a different measurement from fab limits |
| 11 | IPC-2226 HDI types I–VI | the standard | *metric*: via structure (stacked/staggered microvias, buried cores) |
| 12 | IPC-6013 flex / rigid-flex classes | the standard | *metric*: bend areas, coverlay |
| 13 | IEC 60664-1 / IEC 62368-1 creepage and clearance | the standards | Context: pollution degree, material group, overvoltage category, working voltage. *metric*: creepage along the surface around slots (current gap = straight-line clearance) |
| 14 | Silkscreen-to-pad clearance | — | *metric*: now a manual order note; measure silk geometry against mask openings |

## Adding a system (about 15 minutes for a typical fab page)

1. `pcbtestkit classes template --out myfab.json`, then fill in the numbers exactly as the source writes them (`"4mil"`, `"100um"`, `0.1`). Add `"note"` for anything the numbers do not capture.
2. Pick the structure:
   * One `dimensions` entry per independent axis. Use `choose: "cheapest"` when levels run from cheap to fine, and `choose: "user"` when the level is a choice such as an environment or product class.
   * When the levels are alternatives in price order rather than a fineness ladder (stack-ups such as 2L HASL, 2L ENIG, 2L 70 µm, 4L), add `"monotonic": false`. The validator then stops warning that a later level is coarser, and a board that fits none is pointed at the level it misses by the least (the cheapest one on a tie).
   * Use `requires` for a level that is only sold with an order option: `"requires": {"finish": ["ENIG", null]}` (`null` = finish not set in the stack-up). A board that fits no level is still pointed at such a level when it is the nearest one. A different finish or mask colour counts as nearer than a different layer count, and the report names it: "surface finish HASL-LF at stackup 4L-35um-ENIG (limit ENIG)".
   * Put limits that apply to every class in `always`.
   * Use conditions for limits that depend on the board: `{"when": {"layers": {"max": 2}, "copper_um": {"max": 40}}, "value": "4mil"}`.
   * Use item filters for limits that depend on the feature: `{"items": {"finished": {"max": 0.45}}, "value": 0.2}`.
   * Use `tables` + `lookup` for limits per voltage (or any other key).
   * Set `hole_basis` to say whether rings and clearances are measured from the finished hole or the drill tool.
3. Add `tests`: a few measured values with the class you expect, especially the boundaries.
4. `pcbtestkit classes validate myfab.json` checks metric names (with suggestions), units, expression names, condition keys, table columns, level order and your test vectors.
5. Drop the file into `~/.pcbtestkit/classsystems/`, into `<project>/classsystems/`, or under `tech.systems` in `pcbtestkit.json`. An AI model can do the same through `add_class_system`, which validates the definition before storing it.

`pcbtestkit classes catalog` lists every metric, alias and context key. A metric that cannot be measured from the design (e.g. hole plating) can still be stated. It appears as an order note.
