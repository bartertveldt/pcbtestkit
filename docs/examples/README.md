# Example outputs

Complete PCB TestKit exports for three boards, generated with:

```bash
python3 -m pcbtestkit run <board>.kicad_pcb --video
```

| Folder | Board | Why it is interesting |
|---|---|---|
| `sample_sensor/` | The bundled sample (60 × 45 mm, 2 layers) | It has seeded weak points. Its 0.45/0.30 mm vias leave a 0.025 mm ring on the drill tool, which is finer than any Eurocircuits pool class. The report shows what to widen and which other fabs accept the board as drawn. The probes use the bottom test pads, and the jig holds 5 THT parts. |
| `pic_programmer/` | KiCad demo *pic_programmer* (2 layers, through-hole) | It needs only Eurocircuits class 3A. The jig holds 55 hand-soldered parts. |
| `video/` | KiCad demo *video* (312 mm PCI card, 4 layers) | It needs Eurocircuits class 4B. The class overview shows that its zone fills come within 0.011 mm of the edge, which JLCPCB, OSH Park and Würth flag. The fixture has 32 measurement channels with wrapped divider rows. |

## How to use the outputs

Each folder holds:

* **`<board>-guide.html`** — start here. It's the step-by-step visual guide, readable on a phone or tablet at the workstation:
  1. What to fix before ordering, the technology class to order, and which fabs' class systems the board fits.
  2. Building the fixture: parts list, build steps, and wiring from the header to the ADS1115 boards and the Raspberry Pi.
  3. Running a test.
  4. Hand-soldering with the two jigs: the six steps, the placement jig, the pocket map and exploded views.
* **`<board>-explainer.mp4`** — the same story as a short video for operators, with 3D animation rendered by KiCad: assembly, printing both jigs, fitting the parts in the placement jig, closing the holding jig, turning the stack over, soldering, building the fixture, and pressing the board onto the pins.
* **`<board>-fixture.kicad_pcb`** (+ `.kicad_pro`) — open it in KiCad, run DRC and order it (since 1.1.0 it comes out routed; route anything the warnings list). The pogo pins have 3D models, so press Alt+3 to see them.
* **`<board>-jig-placement.stl`** and **`<board>-jig.stl`** — the two solder jigs; print them as they are. Fit the parts with the PCB top side up in the placement jig, put the holding jig on top, turn the stack over, lift off the placement jig and solder. The holding jig also comes as `.scad` for OpenSCAD.
* **`runner/`** — copy it to the Raspberry Pi. `testplan.json` is already inside; describe your wiring in `hardware.json`.
* **`<board>-spec.json`, `<board>-testplan.json`, `<board>-report.html`** — the machine-readable specification and test plan, and the full findings report.
* **`visuals/`** — the KiCad 3D renders used in the guide.
