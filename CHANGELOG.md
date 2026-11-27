# Changelog

All notable changes. Versions follow [Semantic Versioning](https://semver.org).

## 1.1.2 (2026-11-27)

### Test environments: 100+ boards, one to three testers
- `pcbtestkit environment plan | build | init`: give it every KiCad project you make (folders are searched, or
  `.kicad_pro`/`.kicad_pcb` files, or `@list.txt`). It profiles every board in parallel (cached), designs the test
  station that most of them fit, and further stations (up to `max_stations`, 3) for the boards the earlier ones
  cannot take, and checks every board on its station with a real fixture layout. A greedy designer spends each
  station's socket pins (one or two 2 x 40 sockets) on analog inputs, switched supplies and bus instances where they
  let the most boards fit and the batches grow, within the spec's `limits`; a board goes to the first station that
  takes it (`rebalance: true` also moves boards to a later station for a batch 1.5 times as big).
- `build --out DIR` makes each station's KiCad board (`pcbtestkit/station_board.py`: plate, holes, sockets with every
  signal named by net, its electronics as parts) and reads it back as the facility, every board type's outputs on its
  station (several at a time; boards whose outputs are up to date are skipped), each station's page and swap video,
  and `index.html`, `requirements.md` (exactly what each station needs: inputs and range, supplies with voltages and
  current, buses with the boards that use them, programmers per interface, plate, sockets), `ranking.csv`,
  `boards.csv`, `environment.json`, `environment-spec.json`. Boards that do not fit are listed with the reason
  (supply voltage, probed from both sides, no reachable test point, too many channels, a bus not allowed, too big).
- Your own environment: the spec (`environment init`), stations edited in KiCad and kept with `--environment`, or
  stations of your own with `--station`; priorities per board (`--priorities file.csv`).
- KiCad: *Tools > External Plugins > PCB TestKit - test environment for many boards* (and *Many boards...* in the
  dialog): pick a folder or projects, optionally an existing environment or station board, the output folder and the
  settings; it plans or builds in a background job with a progress window (`pcbtestkit/environment_job.py`).
- New boards for an existing environment: `pcbtestkit environment use DIR` remembers it (`~/.pcbtestkit/settings.json`,
  `PCBTESTKIT_HOME`), or `run --environment DIR [--station S] [--batch N|auto]` per run; the KiCad dialog's *Test &
  fixture* tab has a *Test environment* box with *Remember for every future build*. The board's fixture, jigs and
  runner are made for the station that takes most of them, with as many boards as it allows (inputs, supplies, buses,
  plate, checked with a real layout) or a set batch; nothing is written into `pcbtestkit.json` but the choice.
- `examples/make_collection.py` generates a collection of varied boards (sensor nodes, CAN nodes, RS-485 I/O, USB
  dongles, gateways, power boards, large controllers, AVR boards, and a few that cannot fit by design).

### Growing and upgrading an environment
- **Layouts by worth and cost.** With every station new, the designer compares one main station (general) with
  stations made for groups of boards (`specialized-N`, k-means on what the boards need; `split-<bus>`, e.g. a CAN
  station and an Ethernet station) and keeps the one worth the most for what it costs: the boards that fit (spec
  `costs.value_per_board`, more for bigger batches) minus stations, programmers and fixtures (spec `costs`: rough
  EUR prices, set your own). `--layout` / spec `layout` takes another; `coverage_first` never trades boards that fit
  for cost. *Layouts compared* on `index.html`, `layouts` in `environment.json`.
- **Constraints per board** (`constraints.md`/`.csv`/`.json`, `environment constraints DIR [--board B]`): what each
  resource of its station allows, which bind, what one more board per fixture needs and costs; for a board that
  fits none, what it lacks on every station and the cheapest change (or why none helps).
- **Upgrade options** (`upgrades.md`/`.json`, `environment options DIR`): single changes ranked by net worth (id
  U1...) and three paths (best value, most boards, biggest batches). `environment upgrade DIR --option U1 | --path
  value [--steps N] | --add "station-1:can+1,ain+8;supply_v=48" | --fit a,b --batch c=6 --budget 1500` shows what it
  gains and costs; `--apply` makes it: the station's board gets a new revision that keeps every pin, socket position
  and mounting hole (the plate grows around them), so every fixture made for it still plugs in, and boards whose
  batch stays keep their fixture (`pcbtestkit/upgrades.py`, `station_board.upgrade_sockets`).
- **Adding boards** (`environment add DIR BOARDS...`): the stations stay, the boards there are reused (their checks
  are cached too), a further station only for new boards none takes; every station video is kept as clips
  (`station_video_parts/`) and the new board types are rendered and joined after the ones it showed.
  `sources.json` (relative paths) records where each board came from.
- **AI assistants:** MCP/API tools `get_environment`, `plan_environment_upgrade` (goals solved at the lowest cost),
  `upgrade_environment`, `add_boards_to_environment` (background jobs, `confirm` needed) and `get_environment_job`
  (40 tools in all).
- A station board's second socket with only a bus or two on it is read (an upgraded station's new socket).

### Solder jigs that fit exactly
- **Cut to the parts' real 3D shapes.** The jigs come from KiCad's own 3D export of the board (`kicad-cli pcb export
  glb`, KiCad 8+, cached; `pcbtestkit/parts3d.py`): every model, STEP or VRML, with its offset and rotation, as
  KiCad's 3D viewer and STL export draw it. Boards made with an older KiCad find their models too (old library
  variables and `.wrl` names resolve to the installed STEP models). Without kicad-cli: the VRML model, else a box.
- **Every hand-soldered part touches its pocket the moment the board lands.** A hold pocket's floor is the part's own
  top surface (`jig.hold_depth_offset_mm` is now 0, was +0.1 mm), shaped to it (`jig.pocket_floor: "flat"` for one
  depth per part), grown sideways by `jig.hold_clearance_mm` (now 0.15 mm, guaranteed: every cell is cut wherever its
  square comes that close). Before, pockets were boxes from the courtyard or fab outline and the model's points: on
  the 128-board catalog all 432 hand-soldered parts sat 0.1 to 0.6 mm loose.
- **Nothing touches on the way in.** The placement jig is cut away wherever a lead, a bottom-side part or a connector
  reaching past the board's edge would meet it (`jig.overhang_clearance_mm`, `jig.bottom_part_clearance_mm`), and its
  stops keep away from them; the holding jig's rim is cut over overhanging parts; the bare board clears the rim, stops
  and posts by their clearances (whole cells, not cell centres); a post stops short of a part under its mounting
  hole; on thin boards the rim ends above the board's underside. Before: 224 parts reached into a placement jig, 66
  into a holding jig, and on 37 boards the bare board touched a jig.
- **Held on all four sides.** Every hand-soldered part can slide at least `jig.hold_clearance_mm` and less than that
  plus one grid cell (`jig.resolution_mm`) in +x, -x, +y and -y before it meets a wall, or the part beside it where
  the two are too close for a wall between them (`held_by_parts`); the check measures it (`side_play`) and fails a
  part that nothing holds. Clearance pockets near a hand-soldered part never take its floor or walls.
- **Checked on every export** (`pcbtestkit/jigfit.py`, written apart from the builder): each part's triangles against
  each jig's surface, exactly; the result is `fit` in `*-jig.json`, a note in the guide, and which parts only had an
  estimated shape. The whole 128-board catalog passes (every jig set, both sides, paste nests).
- **`jig-fit-check/`**: the board with its parts and every jig in its working position, in KiCad's 3D coordinates
  (open them together, or with KiCad's own STL export). The OpenSCAD file now has exactly the STL's shape.
- **Room for heat-resistant inserts.** Hand-soldered parts that get hot all over while soldered (a heavy ground pin,
  joined solidly to a copper zone or big, on a metal body: shielded connectors, coaxial and barrel jacks, TO-220 tabs,
  crystal cans) are detected (`pcbtestkit/heat.py`) and their holding-jig pockets get `jig.heat_insert_mm` (1.0 mm) of
  room all round for an insert (silicone, PTFE, FR-4); the guide has the step and the reasons, the fit check allows
  for the insert. `jig.hot_parts` ("auto", "none", a list), `--jig-heat-insert`, an open decision for AI assistants.
- **Room of your own** per part: `jig.room` (`{"J1": 1.5, "C*": 0.5}`), `--jig-room`, *Extra room...* in the
  KiCad dialog.
- **Leave parts out of the jigs** (no pocket, no 3D model, no stand-in): `jig.ignore` (references, values or
  footprint names, wildcards), `jig.ignore_models` (3D model files, e.g. an enclosure), `--jig-ignore` /
  `--jig-ignore-models`, *Leave out of the jigs* in the KiCad dialog, `set_jig_options` for AI assistants.
  Footprints without pads are never in the jigs.
- A D-sub footprint named "..._MountingHolesOffset..." is no longer taken for a mounting hole.

### Long runs: background, lid closed, then sleep or shut down
- `environment plan/build --when-done sleep|hibernate|shutdown [--grace 120]`, `--lid`, `--no-keep-awake`,
  `environment stay-on DIR` (`pcbtestkit/power.py`). A run keeps the computer awake (by default), also with the lid
  closed (`--lid`: on macOS with one administrator password prompt, set back when the run ends or the battery is under
  10 %; on Windows the lid action, set back afterwards), and once it has finished or failed (not when cancelled) puts
  the computer to sleep, hibernates or shuts it down after a warning that *Stay on* (progress window),
  Ctrl+C or `stay-on` can stop. A background job's power is handled by a watcher process, so a crash counts as a
  failure. The KiCad dialog has a *Long runs* box and remembers its choice.

### 3D images and rendering
- The guide's 3D images default to the *best* preset (shadows, anti-aliasing) instead of KiCad's *high*: hardly a
  visible difference and 6x faster (15 images: 17 s instead of 102 s). `run --image-quality`, `visuals.quality`, the
  dialog's *3D images* choice and the environment spec/dialog set it; the images render several at a time.
- `environment plan/build --background` runs as a background job; environment builds share the cores between the
  boards worked on at once; a station video's frames are deleted once its MP4 is made.

### Stations
- A board's bus counts against a station only when every line of it has a probe (an unprobed USB no longer takes a
  station's USB port). On a station fixture each board's dividers are packed under it (no header or terminal space),
  which leaves more of the plate for more boards and routes better.
- A station may have two interface sockets; its pins are assigned as one pool. Station boards are read with every
  socket that carries station nets, Ethernet nets (`ETH<k>_TX_P` ...) included; only mounting holes count as holes.
- `station run` takes a ready plan, works on several boards at a time, keeps boards whose outputs are up to date and
  can show only the first N board types in its video; the station page shows every socket's pin map.
- The fixture router takes search settings from `fixture.router` (`weight`, `max_passes`, `last_resort_expansions`):
  a 4-board station fixture routes in about 40 s instead of minutes with the environment's settings.
- Fixtures with nests put the sites 3 mm apart (`fixture.nest.site_gap_mm`): the nests keep their own clearance.

### Fixes
- A nest post's solder mask opening is on the top only: tracks that pass under its screw head on the bottom stay
  covered (KiCad's DRC reported them as solder mask bridges). A test runs KiCad's DRC on a nest fixture.
- The fixture's title moves along the edge when a nest post is where it would go (no silkscreen clipped by mask).
- A programming connector that is an edge-mount part (pads on both copper layers, like an edge DB9) gets a pin header
  with its pinout on the fixture instead of a copy with every pad on one layer (which shorted them).

## 1.1.1 (2026-11-26)

### Test stations: several board types on one tester
- A station is a test facility whose interface connector is *shared*: every board of a batch gets the next block of
  analog inputs, its own switched supply (`VOUT<k>`) and its own instance of every bus (`CAN2_H`, `UART2_TX`,
  `I2C3_SDA`, `RS485_2_A`). Mark a connector `"shared": true` in `facility.json`; the pin grammar takes bus instances.
- Describe the station by its own KiCad board: `pcbtestkit facilities from-pcb station.kicad_pcb` reads the interface
  connector (found by its net names: `GND`, `AIN<n>`, `VOUT<n>`, `CAN<n>_H`, ...), its grid, rotation and position,
  the plate (the board outline) and its mounting holes, and writes the facility. Every fixture made for it has the
  same outline, holes and the mating connector at the same place, so swapping board types is swapping fixtures.
- `pcbtestkit station plan STATION PROJECT...` works out, per board type, how many boards one fixture takes: what
  the station's inputs, supplies, buses and plate allow (and which of them limits it), the plate checked with a real
  fixture layout (another column count or nest orientation when that fits more). `station run` makes every board
  type's outputs (guide, fixture, jigs, runner, optionally its video) plus `station.html` (capacity, pin map per
  board type, how to swap fixtures) and a station video: every fixture slides onto the station in turn, its boards
  drop in. `--batch BOARD=N` sets a batch; the exit code is 1 when a board type does not fit.
- `examples/make_station.py` generates an example station (260 x 190 mm, a 2 x 40 socket with 32 analog inputs,
  4 supplies, 2 CAN, 2 UART, 4 I2C, 8 DIO). The runner hands each board's own supply output and bus instance to the
  facility driver (`select_site(site, inputs)`, the example driver switches `VOUT<k>` and tests bus `{inst}`).
- Board renders can use the stackup colours (`kicad_writer.stackup`, `--use-board-stackup-colors`).

### Board nests and clamps: load, clamp and test every board on its own
- Every board on the fixture gets a printed nest and a clamp (`fixture.nest`, on by default for single-sided
  fixtures with a rectangular outline). The nest floats on four sprung M3 standoffs: two rails guide the board's
  edges, an end stop locates it and the rails run on past its front edge with a flared mouth. Ledges under the
  board's edges carry it above the pogo tips while it slides in, so the tips never scrape the pads; ledges and walls
  leave room wherever a part or a through-hole lead slides through, and the gap above the tips grows to clear them.
  Boards whose underside would sweep over a pin get the drop-in style (four chamfered walls) instead.
- A vertical toggle clamp (a bought part, e.g. GH-101-A) on a printed adapter behind each nest carries a printed
  press plate: push fingers press on bare spots of the board's other side next to the probes and clear its tallest
  part. Closing the clamp pushes board and nest down by the nest's travel; the pins are compressed by their working
  stroke. The adapter's cross slots take clamp base patterns within 4 mm of `clamp_holes_mm`; only the adapter is
  reprinted for another clamp.
- New outputs: `<board>-fixture-nest.stl`, `-fixture-press-plate.stl`, `-fixture-clamp-adapter.stl` with previews
  (one of each per board), heights and parts list in the fixture JSON and the guide (a new section with the assembly
  steps). The fixture board gets the standoff and clamp holes per board (copper keep-outs, so tracks stay clear)
  and the nest, standoffs and clamp as 3D models; the guide's renders show a board in its nest and clamped, and the
  explainer video slides it in, closes the clamp and tests it under a see-through press plate.
- Fixture sites get the room their nests need on every side; when the boards do not fit a facility's fixed plate,
  the nests are turned (the boards slide in from the side). `fixture.nest.entry` front | back | left | right,
  `style` auto | slide | drop, `clamp` toggle | none; agents: `set_fixture_options` (`nest_*`).
- The rails replace the guide pins: fixtures with nests have no GUIDE holes.

### Batches: one connector, programmed at the same time, tested meanwhile
- A batch fixture has one connector instead of a header, power terminal and programming connector per board: a
  2 x N header in a band across the middle of the fixture carries every board's supply (its own pin, for its own
  power switch), channels and programming lines, with GND first and every ten pins. The boards sit on both sides
  of it with their dividers on the corner facing it, so the traces from every board run about as far (and route
  faster). The pin map is in the guide and the fixture JSON (`central_header`); the runner's channel map and the
  probes CSV point at its pins. `fixture.batch_connector: "per_site"` keeps the old layout.
- The test plan works out what needs the firmware: the programmed MCU is the IC on the programming header's nets;
  checks on its pins (directly or through a series resistor) and on rails whose regulator or load switch it enables
  need the firmware, everything else does not (`firmware` in the test plan; force a check with `needs_firmware`).
  The sequence is in phases: unpowered, power, program, during_program, after_program, end.
- The runner tests a batch in those phases: every board checked unpowered and powered up, then all boards
  programmed at the same time (`program.command` once per board) while the checks that need no firmware are
  measured board by board, then the checks that need the firmware. A board whose programming fails skips them.
  `--sequential` keeps the old one-board-after-the-other flow. Result files record how long each step took.
- Production timing (`timing` in the test plan, a section in the guide): how long a batch takes to assemble with
  the jigs, program and test, as a timeline per board, and the time per board for every batch size with the size
  past which a bigger batch saves little. Durations in the new `timing` config section (`program.estimate_s` too).

### Explainer video: in the background, ray-traced, the test made visible
- From the KiCad dialog the video renders in a background job after the other outputs: one process, no window per
  frame, one small progress window (frames done, speed, time left, Cancel) and KiCad stays usable.
  `run --video --background` does the same; status in `visuals/video-job.json`. A cancelled or interrupted run
  goes on from its kept frames instead of starting again.
- Ray-tracing presets (`video.quality`, `run --video-quality`, the dialog): basic, standard (new default: shadows and ambient
  occlusion), best (adds anti-aliasing), high. They run kicad-cli with a private settings folder, so your KiCad
  settings are untouched. As many frames render at once as the computer has cores (measured: start-up and scene
  loading are about 0.5 s of every kicad-cli call, and KiCad renders one frame per call).
- New shot "Test the batch": the boards on the fixture while the runner tests them, the copper of what is being
  measured lighting up (by phase colour, board by board, every board's programming nets while they are
  programmed), beside the runner's output scrolling in step.
- Batches in the jig shots: every board the jigs hold rides in the stack, is soldered and lifted out.

### CI
- New jobs render the sample board's 3D views, animation and MP4 with a real KiCad on Windows (KiCad and ffmpeg from
  Chocolatey) and on Linux (KiCad's own image), also through the background job, and keep the video as an artifact.
  CI now also runs for `feature/**` branches and by hand.
- Fixed (found by testing on Linux in KiCad's own image): the ray-tracing presets crashed on a machine without
  kicad-cli; the captions failed on a machine without a display (now drawn under `xvfb-run` when it is installed,
  with a hint when it is not); ffmpeg 7 refused the cross-fades ("inputs needs to be a constant frame rate") because
  the frame rate was set before the timestamps were reset.
- Batch programming made robust (a review of the runner): programmer output goes to a file instead of a pipe (a
  chatty programmer no longer stalls), programmers still running after an error or Ctrl-C are killed, a command
  without `{site}` for several boards (one probe or mux) programs them one after the other, a programmer that
  cannot start skips that board's firmware checks, and a hung one is killed with its last output kept.

### Technology classes: four more fabs
- `seeed-fusion-2026` (Seeed Fusion, track/space per copper weight), `nextpcb-2026` (NextPCB), `allpcb-2026` (ALLPCB,
  with the 8:1 aspect-ratio option) and `multicb-2026` (Multi-CB, free standard and special production for 35 µm
  copper), each with its source, the date it was checked and test vectors. 12 class systems are built in now.

### Examples
- `docs/examples/tiny_tapeout`: KiCad's Tiny Tapeout demo board as the showcase: a batch of four on one fixture and
  in the jigs, programmed over SWD at the same time while the rails are checked, production timing, and the video
  with every step. `tools/regen_examples.py` builds it from a copy of the demo with the settings in `SHOWCASE`.

### Test facilities: your own tester
- A test facility describes the tester a fixture plugs into, as data (`facility.json`, plus a runner driver if you
  want one), so not every fixture has to be made for a Raspberry Pi: analog inputs and their range, switched
  supplies, buses, and optionally a fixed plate size, fixed mounting holes and fixed interface connector(s) with a
  pin map, the same on every fixture. Pick one with `"facility"` in `pcbtestkit.json`, `run --facility`, the dialog
  (*Test facility*) or the `set_facility` tool (`get_schema('facilities')` lists them). Facilities live in
  `~/.pcbtestkit/facilities`, `PCBTESTKIT_FACILITIES`, `<project>/facilities` or `facility_dirs`.
- The test plan uses no more channels than the facility has and sizes the dividers for its input range. The board's
  CAN, USB, Ethernet, UART, I2C and RS-485 nets are found by name (or `testplan.bus_nets`). A bus the facility takes
  through the fixture gets pogo pins wired to its connector pins; a bus by cable gets an operator prompt; each one
  becomes a test step (`SB1`, ...).
- The fixture gets the facility's connector at its fixed position instead of the generated header (and no power
  terminal when the facility supplies the board), on the facility's plate with its holes; batches get one connector
  per board. When the boards do not fit the plate, the warning names a `batch.cols` that fits.
- The runner hands measuring, power switching and the bus checks to the facility's driver (copied into `runner/` as
  `facility_driver.py`) with the facility's `hardware.json`; the guide shows the facility's pin map.
- `pcbtestkit facilities list | show | validate | template`. Built in: `raspberry-pi` (the default, unchanged) and
  `bench-tester-example`, a worked example with a 2 x 20 IDC connector, CAN/UART/I2C through the fixture,
  USB/Ethernet by cable and a driver that speaks SCPI over TCP. Format and driver interface: `docs/FACILITIES.md`.

### Explainer video
- Boards with hand-soldered parts on both sides show both jig sets: after the top side's sequence (print, fit, close
  the holding jig, turn over, lift off, solder) the same sequence runs for the bottom side's jig set, with the PCB
  bottom side up and captions that name the side. The top side's shots leave out the parts fitted later. Single-sided
  boards get the same video as before.
- When the MP4 cannot be made, `visuals/scenes/video/BUILD-VIDEO.md` and `video-recipe.json` say what is there and
  give the exact commands (every path relative to that folder), so the video can be finished from the kept frames by
  hand or by an AI assistant; `python -m pcbtestkit.video <folder>` does it in one go, and `--no-captions` needs only
  ffmpeg. A missing ffmpeg or wxPython no longer stops the frames being rendered, so only the last step is left.
- Windows: project folders with backslashes in their path no longer break the 3D models in the animation scenes
  (KiCad read `\b`, `\n` in them as escapes); no console window flashes up for each kicad-cli, ffmpeg or Python call;
  the concat lists are UTF-8 and ffmpeg runs in the frames folder with relative paths (folder names with non-ASCII
  characters, short command lines); tool output is decoded as UTF-8; ffmpeg is also found in the winget, Chocolatey
  and Scoop folders and `C:\ffmpeg`; the runner demo uses KiCad's Python instead of `python3` (the Microsoft Store
  placeholder).
- When the video cannot be made after its frames are rendered, the frames are no longer deleted, and the reason is
  shown: in the dialog's final message, the guide, the report and `<board>-video-error.txt` (the whole error output).
  Before, the reason only reached the command line, and the frames disappeared.
- Kept frames are reused: generating again with the same board, fixture, jigs, settings and version skips the 3D
  animation (and the captions when the storyboard is unchanged) and only makes the MP4 again.
- The MP4 step asks ffmpeg what it has: when libx264 is missing it uses another H.264 encoder (OpenH264,
  VideoToolbox, Media Foundation, VA-API) or MPEG-4, and an ffmpeg without the xfade filter joins the clips with
  plain cuts instead of failing.

## 1.1.0 (2026-11-25)

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
