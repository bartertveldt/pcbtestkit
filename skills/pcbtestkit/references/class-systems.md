# Fab class systems

A class system describes what a fab (or a standard) can build. PCB TestKit measures the board once and
evaluates every system: the class the board needs and what drives it, whether it fits at all, violations of
the class you order (with x/y locations), process limits, and order notes for what a design can't show.

## Built in

| id | Classes |
|---|---|
| `eurocircuits-2024` (default) | pattern 3-9 x drill A-E, e.g. `6C` |
| `jlcpcb-2026` | `standard`, `small-via` |
| `pcbway-standard-2026` | `standard`, `extra-charge-holes` |
| `oshpark-2026` | `2-layer`, `2-layer-2oz-0.8mm`, `4-layer`, `6-layer` |
| `aisler-2025` | `2L-35um-HASL`, `2L-35um-ENIG`, `2L-70um-HASL`, `2L-70um-ENIG`, `4L-35um-ENIG`, `6L-35um-ENIG` |
| `wurth-basic-2022` | `standard`, `advanced` |
| `ipc-2221-voltage` | `B2`, `B3`, `B4`, `A5` (needs net voltages: `set_net_specs`) |
| `ipc-6012-ring` | `1`, `2`, `3` |

## Commands

```bash
pcbtestkit classes check P                                          # every system: needed class, fits?, blockers
pcbtestkit classes check P --system jlcpcb-2026 --target standard   # exit 1 + violations with locations
pcbtestkit classes show jlcpcb-2026 [--class standard]              # the limits
pcbtestkit api set_analysis_options --project P --args '{"class_system":"jlcpcb-2026","tech_class":"standard","rationale":"ordering at JLCPCB"}' --compact
```

The ordered class also sets the limits of the generic manufacturing checks.

## Adding a fab or standard (no code)

1. `pcbtestkit classes template --out myfab.json`.
2. Type the numbers exactly as the source writes them (`"4mil"`, `"100um"`, `0.1`).
   - One `dimensions` entry per independent axis; `choose: "cheapest"` for cheap-to-fine levels,
     `choose: "user"` for a choice such as an environment or product class.
   - Limits for every class go in `always`.
   - Board-dependent limits: `{"when": {"layers": {"max": 2}, "copper_um": {"max": 40}}, "value": "4mil"}`.
   - Feature-dependent limits: `{"items": {"finished": {"max": 0.45}}, "value": 0.2}`.
   - Per-voltage (or other key) limits: `tables` + `lookup`.
   - `hole_basis`: are rings/clearances measured from the finished hole or the drill tool?
   - `"note"` for anything the numbers can't express.
3. Add `tests`: measured values + the class you expect, especially at the boundaries.
4. `pcbtestkit classes validate myfab.json`: catches unknown metric names (with suggestions), units,
   expression names, condition keys, level order and failing test vectors.
5. Put the file in `~/.pcbtestkit/classsystems/` (or `$PCBTESTKIT_CLASSSYSTEMS`), in `<project>/classsystems/`,
   or store it through the API: `add_class_system` with `{"definition": {...}, "dry_run": true}` first.

`pcbtestkit classes catalog` lists every metric, alias and context key. `get_schema` with
`{"name":"class_system"}` returns the template, metric names and rule syntax in one answer.
