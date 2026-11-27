# Test environments: one to three stations for a whole collection of boards

A production line that makes many different boards wants as few testers as possible. `pcbtestkit environment`
takes every KiCad project you make (100 or more is fine), works out what each board needs from a tester, designs
the test station (or two or three) that as many of them as possible fit, and makes everything: the station's own
KiCad board, every board's fixture (with a nest and clamp per board of its batch), guide, jigs, runner, a page per
station and a video of swapping its fixtures and boards. Boards that cannot fit are listed with the reason.

```bash
pcbtestkit environment init --out env-spec.json                       # optional: the settings, to edit
pcbtestkit environment plan  ~/kicad-projects --spec env-spec.json    # minutes: stations, which board where, ranking
pcbtestkit environment build ~/kicad-projects --spec env-spec.json --out ~/test-environment
```

In KiCad: *Tools > External Plugins > PCB TestKit - test environment for many boards* (or *Many boards...* in the
PCB TestKit dialog). Add a folder (every project in it is found) or projects, optionally an existing environment or
station board, the output folder and the settings; *Plan only* or *Build everything* runs in the background with a
progress window, and KiCad stays usable.

## What comes out

```
<out>/
  index.html             start here: every station's needs, the ranking, what does not fit and why
  requirements.md        what each station must offer, exactly (to build or buy it)
  ranking.csv            every board: rank, station, batch, what limits it, what it needs, why not
  boards.csv             every board's profile: channels, supply, buses, programming, size, probes, coverage
  constraints.md         what limits every board, what one more per fixture needs and costs (.csv, .json too)
  upgrades.md            the upgrade options and paths with what they gain and cost (.json too)
  environment.json       all of it, machine-readable (no local paths), with the layouts compared
  environment-spec.json  the settings that were used: edit and build again
  sources.json           where every board came from, relative to this folder (environment add / upgrade)
  test-environment.mp4   every station's video one after the other
  stations/station-1/
    station-1.kicad_pcb  the station's top: plate, mounting holes, interface socket(s) J1 (J2), its electronics
    facility.json        the station as a test facility (what the fixtures are made for)
    station.html         capacity, every board type's pins on the socket(s), how to swap fixtures
    station-compatibility.mp4   every fixture sliding onto the station, its boards dropping into their nests
    station_video_parts/ its clips: a later build renders only new board types and joins them after these
    <board>/             one folder per board type: guide, report, fixture, nest and clamp parts, jigs, runner
  stations/station-2/ ...
```

## How the stations are designed

1. **Every board is profiled** (in parallel; cached, so a second run only redoes the boards that changed): its test
   plan on a station that has everything, so it says what the board asks for: measurement channels, a switched
   supply (and its voltage and current limit), which buses go through the fixture (CAN, UART, I2C, RS-485, USB,
   Ethernet), its programming interface (SWD, JTAG, AVR ISP, PIC ICSP), the size of one fixture site (the board, its
   nest and clamp, its divider block) sliding in from the front or from the side.
2. **A station is a plate and one or two interface sockets** (2 x 40 pins each by default). Its pins are analog
   inputs (in groups of four: one 4-channel ADC each), switched supplies, bus instances (CAN 2 pins, UART 2, I2C 2,
   RS-485 2, USB 2, Ethernet 4) and spare digital lines, with GND every six signals. Board *k* of a batch takes the
   *k*-th block of inputs, supply *k* and instance *k* of every bus it uses, so the batch of a board type on a station
   is the smallest of: inputs / its channels, the supplies, each of its buses, the sites that fit the plate, and
   `max_batch`.
3. **The designer spends the pins where they gain the most.** A board that fits counts 1 and a batch of *n* counts
   `1 + batch_weight x log2(n)`, times the board's priority. Starting from an empty station it adds, step by step,
   what lets one more board fit or one more board of a type join its batch, choosing the step with the most gain per
   pin, until no step gains anything or the sockets are full. Bus instances and supplies never go above `limits`.
4. **A further station** is designed for the boards the earlier ones cannot take (a bigger plate, other buses, more
   inputs), up to `max_stations`, and only when it takes at least `min_boards_per_station` more board types. The first
   station has `plate_mm`; a further one may get `large_plate_mm` for the big boards. A board goes to the first
   station that takes it: further stations are for compatibility, not for bigger batches. With `"rebalance": true`
   a board also moves to a later station that gives it a batch 1.5 times as big, and every station is designed once
   more knowing what the others take.
5. **Every station is drawn** as a KiCad board (`station_board`) and read back as the facility the fixtures are made
   for, and **every board is checked on its station with a real fixture layout** (the batch the plate really takes,
   the nests turned to the side when that fits more). A board whose fixture does not fit after all is tried on the
   other stations.
6. **build** makes every board type's outputs on its station (several boards at a time; a board whose outputs are
   up to date is skipped), each station's page and swap video, and the environment's documents.

## Boards that do not fit

Each one is listed with the reason, as precisely as it can be said:

| Reason | What to do |
|---|---|
| `needs a 48 V supply; the environment's supplies go to 24 V` | raise `supplies.max_v` if your station can, or test it elsewhere |
| `probed from both sides (two fixture plates)` | a station takes one-sided fixtures: set `testplan.probe_side` in the board's `pcbtestkit.json` |
| `no test point the fixture can reach` | add test pads (an antenna or a bare connector board has nothing to test) |
| `needs 40 analog inputs; a station has at most 32` | raise `limits.analog_inputs`, or `testplan.max_channels` for that board |
| `needs a CAN bus through the fixture; limits.can is 0` | allow the bus in `limits` |
| `one board with its nest, clamp and divider block takes W x H mm; station-1 leaves w x h mm` | a larger plate (`plate_mm`, `large_plate_mm`) or a further station |
| `could not be read: ...` | the board file could not be loaded; the message says why |

## A new board for the environment you already have

Once an environment exists (built last week or last year), every new board can be made for it: its fixture, jigs and
runner fit one of its stations, so nothing new has to be designed or ordered for the tester. Remember it once:

```bash
pcbtestkit environment use ~/test-environment      # every future build (all projects) is made for it
pcbtestkit run new_board/                           # station and batch chosen automatically
pcbtestkit environment show | forget
```

or per run: `pcbtestkit run new_board/ --environment ~/test-environment [--station station-2] [--batch auto|N]`
(`--remember-environment` remembers it, `--no-environment` ignores the remembered one). In KiCad: the PCB TestKit
dialog's *Test & fixture* tab has a *Test environment* box (a folder or a station board, the station, *as many boards
as the station allows*, *Remember this environment for every future build*); the many-boards dialog can remember the
environment it builds.

- **Station** `auto` (default): the station that takes the most boards of this type; or one by name.
- **Batch** `auto` (default): as many boards as the station allows safely: its analog inputs, switched supplies and
  bus instances, and the fixture sites (each with its nest and clamp) that fit its plate, checked with a real fixture
  layout; programming runs for the whole batch at once as usual. Or a number: that many, or fewer with a note when
  the station cannot take them.
- The choice is kept in the board's `pcbtestkit.json` as `"environment": {"path", "station", "batch"}`; `path` ""
  means the remembered one and `"none"` switches it off for that board. What the station gives the board (its
  facility, batch, columns, nest orientation) is worked out when the board is loaded, cached, and never written into
  `pcbtestkit.json`. A station board edited in KiCad is read again.
- A board that fits none of its stations is made as before, with a warning that says why (and the stations it tried).

## The spec

`pcbtestkit environment init` writes it with every setting; anything left out keeps its default.

| Key | Default | Meaning |
|---|---|---|
| `name` | Test environment | shown on the pages and the station boards |
| `max_stations` | 3 | at most this many stations (1 = everything on one) |
| `min_boards_per_station` | 1 | a further station only when it takes at least this many more board types |
| `rebalance` | false | true: boards also move to a later station for a batch 1.5 times as big |
| `plate_mm` | [300, 220] | the plate of the first station (and every other unless the large one is needed) |
| `large_plate_mm` | [400, 300] | a further station may get this plate for big boards; [] = never |
| `sockets` | max 2, pins 80, gnd_every 6 | interface sockets per station, pins per socket, GND between every N signals |
| `limits` | analog_inputs 96, supplies 8, can 4, uart 4, i2c 4, rs485 2, usb 2, ethernet 2 | the most of each a station may have (what you can buy or build) |
| `ain_group` | 4 | analog inputs come in groups (4-channel ADCs) |
| `dio` | 4 | spare digital lines on every station |
| `analog.max_v` | 10 | full scale of the analog inputs: the fixtures' dividers are sized for it |
| `supplies` | max_v 24, max_current_a 3 | what the station's supplies can do |
| `max_batch` | 12 | at most this many boards of one type per fixture |
| `batch_weight` | 0.35 | how much a bigger batch counts against fitting one more board type |
| `priorities` | {} | board name -> weight (default 1): how much it matters that a board fits; also `--priorities file.csv` |
| `nest` | true | a nest and toggle clamp per board (see the README) |
| `router` | weight 1.6, 2 passes, capped last resort | the fixture router's effort per board; {} = the thorough defaults |
| `visuals.quality` | best | the guides' 3D images: basic, standard, best (shadows, anti-aliasing), high (KiCad's own, about 6x slower); also `--image-quality` |
| `video_quality` | null | the videos' ray tracing (null: station videos basic, board explainers standard); also `--video-quality` |
| `layout` | auto | auto (worth minus cost), general, specialized-N, split-&lt;bus&gt; |
| `coverage_first` | true | auto never takes a layout that fits fewer boards |
| `costs` | EUR list prices | currency, value_per_board, station, socket, ain_group, supply, high_voltage_supply, every bus, programmer, plate_dm2, fixture, fixture_site |
| `exclude` | [] | board or folder names to leave out |

## Your own test environment

- **Edit the spec** for the station you can afford or already have: its plate, its sockets, `limits` per resource,
  the supplies' voltage range, the batch size.
- **Edit a station in KiCad.** Every station is a KiCad board: move the socket, change the pin map (net names
  `AIN<n>`, `VOUT<n>`, `DIO<n>`, `CAN<k>_H/L`, `UART<k>_TX/RX`, `I2C<k>_SDA/SCL`, `RS485_<k>_A/B`, `USB<k>_DP/DM`,
  `ETH<k>_TX_P/N`, `ETH<k>_RX_P/N`, GND), make the plate bigger. Then build again with `--environment <out>`: the
  stations are read back from their boards and kept as they are; new ones are only designed for the boards they
  cannot take (`--no-new-stations`: none). A build into the same folder keeps a station whose board you edited
  since the last build even without `--environment` (and says so), so an edit is never overwritten.
- **A station of your own** (its `.kicad_pcb`, a `facility.json` or an installed facility id): `--station FILE`,
  repeatable. See [FACILITIES.md](FACILITIES.md) for the facility format and the runner driver.

## Speed

Profiling and planning take about a second per board on 8 cores (128 boards: about two minutes); a second run
reuses the profiles. Building takes longer per board: the fixture is routed (a 4-board fixture about 40 s with the
environment's router settings) and the guide's renders are made with KiCad (10-20 s); with 7 boards at a time,
100 boards take roughly half an hour. Explainer videos per board (`--video all`) take several minutes each; the
station videos show the first 8 board types (`--video-boards`; about a minute of rendering each). `--no-visuals` and `--no-route` make a quick
build; a later build only redoes what changed.

## Layouts: one main station, or stations for groups of boards

When every station is new, the designer compares layouts and keeps the one worth the most for what it costs:

- **general**: one main station that takes as many boards as it can, further ones for the boards it cannot take;
- **specialized-N**: N stations, each made for a group of boards that need much the same (grouped with k-means on
  their buses, analog inputs, supply and size);
- **split-&lt;bus&gt;**: a station for the boards with that bus (`split-can`, `split-ethernet`, ...), one for the rest.

Each is estimated from the board profiles: what the boards that fit are worth (spec `costs.value_per_board` per
board type, more for bigger batches: 1 + `batch_weight` x log2 batch) minus what the stations (base, sockets, ADCs,
supplies, every bus instance, the plate), the programmers and the fixtures cost. With `coverage_first` (default) a
layout that fits fewer boards than another is never taken. The comparison is on `index.html` (*Layouts compared*),
in `requirements.md` and in `environment.json` `layouts`; `--layout NAME` (spec `layout`) takes another. For a
catalog that is half CAN nodes, half Ethernet gateways this is what makes a CAN station and an Ethernet station,
each with bigger batches, when that is worth its cost. The prices in `costs` are rough list prices in EUR: set your
own (and the currency).

## What limits each board, and upgrades

Every build writes, next to `index.html`:

- `constraints.md` (`.csv`, `.json`): per board, what each resource of its station allows (analog inputs, supplies,
  every bus, the plate, the batch limit), which ones bind, and what one more board per fixture would need and cost
  (the station's new parts and this board's new fixture). For a board that fits no station: what it lacks on each
  station and the cheapest change that would take it, or why no station change helps (probed from both sides, no
  reachable test point).
- `upgrades.md` (`.json`): single changes (one more CAN, 4 more analog inputs, a bigger plate, supplies up to 48 V,
  more boards per fixture, a further station), each with the boards it lets fit, the boards per load it adds, its
  worth, its cost and the net, best first, with an id (U1, U2, ...); and three paths: best value, most boards,
  biggest batches. Changes beyond the spec's `limits` or `sockets.max` are offered but marked.

```bash
pcbtestkit environment constraints ~/test-environment --board sensor_04      # one board
pcbtestkit environment options ~/test-environment                           # the upgrade options
pcbtestkit environment upgrade ~/test-environment --option U1 --option U4    # what they gain and cost
pcbtestkit environment upgrade ~/test-environment --path value --steps 2
pcbtestkit environment upgrade ~/test-environment --add "station-1:can+1,ethernet+1;supply_v=48"
pcbtestkit environment upgrade ~/test-environment --fit hv_driver_01,vme-wren --batch sensor_04=8 --budget 1500
pcbtestkit environment upgrade ~/test-environment --option U1 --apply --background   # make it
```

`upgrade` shows the changes with what they gain and cost; `--apply` makes them and builds the environment again.
**The fixtures you have keep working:** an upgraded station gets a new revision of its board (rev B, C, ...) that
keeps every pin of its sockets, every socket's position and every mounting hole; new signals go on new pins after
them (the last socket grows, aligned at pin 1, or a new socket goes behind), and a bigger plate grows to the sides
and the back with the old holes kept. A board whose station and batch stay keeps the fixture it has; a board whose
batch grows gets a new one. A station that is your own board (or edited in KiCad) is not changed: the upgrade says
what to change in it.

## Adding boards

```bash
pcbtestkit environment add ~/test-environment ~/kicad-projects/new-board ~/kicad-projects/another
```

The stations stay as they are; the boards the environment has are reused (their checks and outputs are cached:
only the new ones are checked and made); a further station is only designed for new boards no station takes
(within `max_stations`; `--no-new-stations-for-new`: never). Every station's swap video keeps the board types it
showed and gets the new ones after them: the video is kept as clips (`station_video_parts/`), so only the new board
types are rendered and the clips joined again (`--video-boards N`, default 8: the first build shows N board types, a
later one adds up to N of the boards new to it; a build without new boards leaves the video as it is).
`sources.json` (paths relative to the environment's folder) says where every board came from.

## Ask an AI assistant

The MCP server (and `pcbtestkit api`) has the environment as tools: `get_environment` (stations, layout, what limits
the boards, the best options; `board=` for one board's constraints), `plan_environment_upgrade` (option ids, a path,
changes of your own, or goals: boards that must fit, batches, a budget; nothing is changed),
`upgrade_environment` and `add_boards_to_environment` (start the rebuild in the background; they need
`confirm=true`), `get_environment_job` (how far it is). So "make the three 48 V boards fit and give sensor_04 a
batch of 8, under 1500 euro" is one `plan_environment_upgrade` call, then `upgrade_environment` with `confirm`.

## Long runs: in the background, lid closed, then sleep or shut down

100+ boards with every explainer video take 10-14 hours. Let it run on its own:

```bash
pcbtestkit environment build ~/kicad-projects --out ~/test-environment --video all \
    --background --lid --when-done shutdown          # or sleep / hibernate; --grace 300 for a 5-minute warning
pcbtestkit environment stay-on ~/test-environment     # changed your mind: the computer stays on afterwards
```

- **Background.** `--background` starts the build as a job of its own and returns: closing the terminal or KiCad does
  not stop it. The KiCad dialog always builds this way, with a progress window.
- **Awake.** While it runs the computer does not go to sleep (macOS `caffeinate`, Windows `SetThreadExecutionState`,
  Linux `systemd-inhibit`); `--no-keep-awake` lets it. The display may still turn off.
- **Lid closed** (`--lid`). macOS sleeps when the lid closes unless sleep is turned off for the whole system, which
  needs the administrator password: macOS asks for it once when the run starts, and a small helper turns sleep on
  again when the run ends, when the job's watcher stops, and when the battery drops under 10 %. Windows sets the
  power plan's lid action to *do nothing* and back when the run ends. On Linux `systemd-inhibit` holds the lid switch
  where the desktop allows it. Plug the charger in: a laptop running with its lid closed is a laptop on full power.
- **Afterwards** (`--when-done sleep | hibernate | shutdown`). Once the run has **finished or failed**, but never after
  you cancel it, the computer waits `--grace` seconds (default 120) and then sleeps, hibernates or shuts down. Until
  then it can be kept on: *Stay on* in the progress window, Ctrl+C for a build in the terminal, or
  `pcbtestkit environment stay-on DIR`. "Finished" is confirmed: the job's status says done and the environment page
  was written by this run; a job that crashes counts as failed, because a watcher process of its own does this, not
  the job. A Mac hibernates by sleeping (it keeps its memory on disk while it sleeps). A shutdown that an app with
  unsaved work stops is followed by sleep after 3 minutes; with `--lid` on a Mac the shutdown does not wait for apps.
- **What happened**: `DIR/environment-job-power.json` (the plan, a countdown, the commands run). If a run was killed
  before it could set the lid back, the next run does (Windows) or tells you the command (macOS:
  `sudo pmset -a disablesleep 0`).
- In KiCad: the dialog's *Long runs* box (keep awake, lid closed, *When it has finished or failed*); it remembers the
  choice.

