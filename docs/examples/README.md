# Example outputs

Complete PCB TestKit exports for four boards and one test station, generated with `tools/regen_examples.py`, which runs:

```bash
python3 -m pcbtestkit run <board>.kicad_pcb --video
```

| Folder | Board | Why it is interesting |
|---|---|---|
| `sample_sensor/` | The bundled sample (60 × 45 mm, 2 layers) | It has seeded weak points. Its 0.45/0.30 mm vias leave a 0.025 mm ring on the drill tool, which is finer than any Eurocircuits pool class. The report shows what to widen and which other fabs accept the board as drawn. The probes use the bottom test pads, and the jig holds 5 THT parts. The fixture is routed (26 connections, 10 vias). |
| `pic_programmer/` | KiCad demo *pic_programmer* (2 layers, through-hole) | It needs only Eurocircuits class 3A. The jig holds 55 hand-soldered parts. The fixture is routed (51 connections, 34 vias). |
| `tiny_tapeout/` | KiCad demo *tiny_tapeout* (Tiny Tapeout demo board: RP2040, 104 × 81 mm, 4 layers) | **The showcase.** A batch of four boards: one fixture with four sites around one connector in the middle, jigs that hold two boards each (two sets for the printer bed), and a runner that programs all four RP2040s over SWD at the same time (`program.command` with a probe per board) while it measures the rails, then the checks that need the firmware. The guide has the production timing (timeline per board, time per board by batch size). The video shows every step, ending with the batch on the fixture and the tested copper lighting up next to the runner's output. Settings: `SHOWCASE` in `tools/regen_examples.py`. |
| `station/` | The example test station (`examples/make_station.py`: 260 × 190 mm, a 2 × 40 socket with 32 analog inputs, 4 supplies, 2 CAN, 2 UART, 4 I2C) with four board types: the sample and the KiCad demos *stickhub*, *royalblue54L_feather* and *pic_programmer* | **Swapping products on one tester.** `station.html` shows how many boards of each type one fixture takes and what limits it (supplies, UART buses, the plate), every type's pin map on the station's connector and how to swap fixtures. Every fixture has the station's outline, holes and the mating connector in the same place; every board in it has its own nest and toggle clamp. `station-compatibility.mp4` slides each fixture onto the station with its boards; each board type's folder has its own guide, outputs and video. Made with `pcbtestkit station run`. |
| `video/` | KiCad demo *video* (312 mm PCI card, 4 layers) | It needs Eurocircuits class 4B. The class overview shows that its zone fills come within 0.011 mm of the edge, which JLCPCB, OSH Park and Würth flag. The fixture has 32 measurement channels, each divider pair above its own header pin, and is routed (134 connections, 128 vias). |

## How to use the outputs

Each folder holds:

* **`<board>-guide.html`** — start here. It's the step-by-step visual guide, readable on a phone or tablet at the workstation:
  1. What to fix before ordering, the technology class to order, and which fabs' class systems the board fits.
  2. Building the fixture: parts list, build steps, and wiring from the header to the ADS1115 boards and the Raspberry Pi.
  3. Running a test.
  4. Hand-soldering with the two jigs: the six steps with a KiCad render under each (both jigs printed, the parts going into the placement jig, the holding jig coming down, the stack turned over, soldering), the placement jig and the pocket map.
* **`<board>-explainer.mp4`** — the same story as a short video for operators, with 3D animation rendered by KiCad: assembly, printing both jigs, fitting the parts in the placement jig, closing the holding jig, turning the stack over, soldering, building the fixture, and pressing the board onto the pins.
* **`<board>-fixture.kicad_pcb`** (+ `.kicad_pro`) — open it in KiCad, run DRC and order it (since 1.1.0 it comes out routed; route anything the warnings list). The pogo pins, every board's nest and its clamp have 3D models, so press Alt+3 to see them.
* **`<board>-fixture-nest.stl`**, **`-fixture-press-plate.stl`** and **`-fixture-clamp-adapter.stl`** — every board's nest (it slides in on the rails, above the pogo pins), the press plate for the toggle clamp's spindle and the clamp's adapter: print one of each per board. The guide's fixture section has the parts list, the heights and the assembly steps.
* **`<board>-jig-placement.stl`** and **`<board>-jig.stl`** — the two solder jigs; print them as they are. Fit the parts with the PCB top side up in the placement jig, put the holding jig on top, turn the stack over, lift off the placement jig and solder. The holding jig also comes as `.scad` for OpenSCAD.
* **`runner/`** — copy it to the Raspberry Pi. `testplan.json` is already inside; describe your wiring in `hardware.json`.
* **`<board>-spec.json`, `<board>-testplan.json`, `<board>-report.html`** — the machine-readable specification and test plan, and the full findings report.
* **`visuals/`** — the KiCad 3D renders used in the guide.
