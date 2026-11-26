# Test facilities: your own tester

PCB TestKit generates a fixture for the tester it plugs into. Out of the box that is a Raspberry Pi with an ADC
(`raspberry-pi`): the fixture gets a 2 x N header and a power terminal, and the runner reads ADS1115 / MCP3008 ADCs
or an MCU over serial. If you test with something else, such as a rack tester, a bench tester built around a DAQ card
or a board of your own with CAN, USB and Ethernet, describe it once as a **test facility**. Every fixture PCB
TestKit then makes plugs straight into it:

- **what it measures and supplies:** analog inputs and their range (the test plan uses no more channels than it has
  and sizes the voltage dividers for its range), switched supplies, current sense;
- **the buses it talks:** CAN, USB, Ethernet, UART, I2C, RS-485. Each one is either wired through the fixture (pogo pins
  on the board's bus nets, onto the facility's connector) or plugged in by cable into the board's own connector;
  either way it becomes a test step;
- **the fixture it takes (optional):** a fixed plate size, fixed mounting holes and fixed interface connector(s)
  with a pin map, the same on every fixture;
- **batches:** how many boards it tests on one fixture, with a connector per board;
- **the runner driver (optional):** a Python file the runner calls to measure, switch power and run the bus checks,
  and the `hardware.json` it starts from.

A facility is data: a JSON file, plus a driver if you need one. You do not change PCB TestKit to add a tester, and
you can share a facility with your team as a folder.

## Quick start

```bash
pcbtestkit facilities list                                   # what PCB TestKit finds
pcbtestkit facilities template --out ~/.pcbtestkit/facilities/my-tester   # facility.json + driver.py to edit
pcbtestkit facilities validate ~/.pcbtestkit/facilities/my-tester/facility.json
pcbtestkit run my_board.kicad_pcb --facility my-tester
```

Or pick it in the KiCad dialog (*Test facility*), with `"facility": "my-tester"` in `pcbtestkit.json`, or with the
agent tool `set_facility` (`get_schema('facilities')` lists them).

PCB TestKit looks for facilities in its own `pcbtestkit/facilities/` folder, then `~/.pcbtestkit/facilities` (or
the folders in `PCBTESTKIT_FACILITIES`, separated like `PATH`), then `<project>/facilities` and the folders in
`facility_dirs` in `pcbtestkit.json`. A facility is `<id>.json` or a folder `<id>/facility.json` with its driver next
to it. A later one with the same id replaces an earlier one. `"facility"` in `pcbtestkit.json` may also be the
definition itself (an object), for a one-off.

The built-in `bench-tester-example` is a complete worked example: a 160 x 120 mm plate with four M3 holes, a 2 x 20
IDC connector on the front edge with 16 analog inputs, a switched supply, CAN, UART and I2C, USB and Ethernet by
cable, and a driver that talks SCPI over the network. Try it on a board with
`pcbtestkit run my_board.kicad_pcb --facility bench-tester-example`.

## facility.json

```json
{
 "schema": "pcbtestkit-facility/1",
 "id": "my-tester",
 "name": "My tester",
 "description": "Shown in the dialog and the guide.",
 "analog": {"inputs": 16, "max_v": 10.0},
 "power": {"outputs": 1, "max_v": 24.0, "max_a": 2.0, "current_sense": true},
 "batch": {"max_sites": 1},
 "buses": {
  "can": {"via": "fixture", "check": {"bitrate": 500000}},
  "usb": {"via": "cable", "note": "Plug the tester's USB cable into the board's USB port."}
 },
 "fixture": {
  "plate_mm": [160, 120],
  "holes": [{"at_mm": [5, 5], "d_mm": 3.2}, {"at_mm": [155, 5], "d_mm": 3.2}],
  "connectors": [{
   "name": "TESTER", "site": 1, "at_mm": [55.87, 114.0], "angle": 90,
   "pins_across": 2, "pins_along": 20, "pitch_mm": 2.54, "numbering": "zigzag",
   "footprint": "Connector_IDC:IDC-Header_2x20_P2.54mm_Vertical",
   "model": "${KICAD8_3DMODEL_DIR}/Connector_IDC.3dshapes/IDC-Header_2x20_P2.54mm_Vertical.wrl",
   "pins": {"1": "GND", "2": "GND", "3": "AIN1", "4": "AIN2", "23": "VOUT1", "25": "CAN_H", "26": "CAN_L"}
  }]
 },
 "runner": {"driver": "driver.py", "class": "Driver",
            "hardware": {"connection": {"type": "tcp", "host": "192.168.1.50", "port": 5025}}}
}
```

| Key | Meaning |
|---|---|
| `analog.inputs`, `analog.max_v` | Channels per board and their full-scale voltage. Left out, the test plan's own `max_channels` and `adc_max_v` apply. With a fixed connector the channels are also limited to its `AIN` pins. |
| `power` | What the facility supplies (informative; the supply step uses the board's `supply` settings). A connector pin `VOUT1` carries the board's supply, and the fixture then gets no power terminal. |
| `batch.max_sites` | Boards per fixture it can test. A larger `batch.count` gives a warning. |
| `buses.<bus>.via` | `fixture`: the board's bus nets get pogo pins wired to the connector pins of that bus (they must exist). `cable`: the operator plugs the facility's cable into the board's own connector; no probes. |
| `buses.<bus>.check`, `.note` | Passed to the driver's `bus_test` as `step["check"]`; `note` is the operator's prompt. |
| `fixture.plate_mm` | Fixed fixture size `[width, height]`. The boards (and their divider block) are centred in the part of the plate the connectors leave free. If they do not fit, you get a warning, with a `batch.cols` that fits when there is one. |
| `fixture.holes` | Fixed mounting holes (`at_mm` from the plate's top-left corner, `d_mm`, `label`); they replace the four corner holes. |
| `fixture.connectors` | Fixed interface connector(s). `at_mm` is pin 1 from the plate's top-left corner (KiCad's Y points down). The geometry is as in KiCad's libraries: at `angle` 0 the connector runs down the board, `pins_along` positions long and `pins_across` pins wide. `numbering` `zigzag` = 1 and 2 side by side, then 3 and 4 (2 x N headers, IDC); `rows` = down the first column, then the next. `footprint` and `model` name KiCad's library footprint and 3D model; `pad_mm` / `drill_mm` override the pad size. `site` = which board of a batch it serves (one connector per board). `shared: true`: one connector for every board of the batch (a station, below). |
| `runner` | `driver`: the driver file next to `facility.json`, copied into the output's `runner/` as `facility_driver.py`. `class`: its class (default `Driver`). `hardware`: the `hardware.json` the runner starts from. |

### Pin functions

| Function | What the fixture puts on it |
|---|---|
| `GND` | Ground |
| `AIN1` .. `AIN<n>` | Measurement channels: CH01 on AIN1, CH02 on AIN2, ... (after the fixture's divider, if the net needs one) |
| `VOUT1` | The board's supply (the supply probes) |
| `CAN_H` `CAN_L` · `USB_DP` `USB_DM` · `ETH_TX_P` `ETH_TX_N` `ETH_RX_P` `ETH_RX_N` · `UART_TX` `UART_RX` · `I2C_SDA` `I2C_SCL` · `RS485_A` `RS485_B` | That bus signal of the board, when the facility takes the bus through the fixture. Signals are named as the board sees them: `UART_TX` is the board's transmit line. |
| `CAN2_H`, `UART2_TX`, `I2C3_SDA`, `RS485_2_A`, ... | Bus instance 2, 3, ... (`CAN_H` and `CAN1_H` are instance 1). On a shared connector board *k* of a batch gets instance *k*. |
| `VOUT<k>` | On a shared connector: board *k*'s supply. On a connector of its own: `VOUT1` is the supply, the rest stay open. |
| `DIO<n>`, `NC` | Left open (yours to wire) |

### How the board's bus nets are found

By name: `CANH`/`CAN_H`/`CAN1_L`, `USB_D+`/`USB_DP`/`D-`, `ETH_TX_P`/`TX+`/`RD-`, `UART1_TX`/`TXD`/`RX`,
`SDA`/`I2C_SCL`, `RS485_A`/`485_B`. Only complete buses count (both CAN wires, all four Ethernet wires). For names it
does not recognise, set them in `pcbtestkit.json`:

```json
"testplan": {"bus_nets": {"can": {"CAN_H": "/CAN_HI_ISO", "CAN_L": "/CAN_LO_ISO"}}}
```

A bus the board has and the facility takes through the fixture gets one probe per net, wired to its connector pins,
instead of a measurement channel. Every bus the board and the facility share gets a step (`SB1`, `SB2`, ...) after the
signal levels.

## The runner driver

```python
class Driver:
    def __init__(self, hardware, plan): ...      # hardware.json and testplan.json as dicts
    def select_site(self, site, inputs): ...     # board `site` of a batch; inputs = what it uses on the facility,
                                                 # {"CH01": "AIN3", "VIN": "VOUT2", "CAN_H": "CAN2_H", ...}
    def read_v(self, channel): ...               # volts at the facility input; the runner undoes the divider
    def power(self, on): ...                     # switch the board's supply
    def current_a(self): ...                     # optional: supply current in A, or None
    def bus_test(self, step): ...                # optional: (passed, note) for a bus step
    def resistance(self, channel): ...           # optional: for the unpowered short check
    def gpio_out(self, pin, level): ...          # optional: for gpio steps
    def close(self): ...                         # optional
```

Only `read_v` and `power` are required. Without `bus_test` the runner asks the operator whether the bus works, or
skips the step with `--non-interactive`. The runner's `--simulate` mode does not load the driver, so a test plan can
be tried without the tester. The example driver (`pcbtestkit/facilities/bench-tester-example/driver.py`) uses only
the standard library: it sends SCPI-style lines over TCP, with the commands set in `hardware.json`.

## Batches

Give a facility one connector per board (`"site": 1`, `"site": 2`, ...) at their fixed places, and `batch.max_sites`.
With `batch.count` boards, every site gets its pogo pins, its dividers and its own connector, and the runner tests
the boards one after the other (`select_site(k, inputs)` before each). Programming connectors and the jigs work as
for any batch.

## Stations: one connector for every board, several board types

A **station** is a facility whose connector is `"shared": true`: the fixture plugs onto it once and every board of
the batch is wired to it. Board *k* gets the *k*-th block of `AIN` pins (as many as it has channels), `VOUT<k>` for
its supply and instance *k* of every bus. So the batch of a board type is limited by the station's inputs, supplies,
complete bus instances and plate; `pcbtestkit station plan` works it out per board type and names the limit:

```bash
pcbtestkit station plan examples/station/test_station.kicad_pcb boardA/ boardB/ --json
pcbtestkit station run  examples/station/test_station.kicad_pcb boardA/ boardB/ --batch boardA=2 --out out --video
```

`station run` writes each board type's outputs into `out/<board>/`, `station.json`, `station.html` (capacity, every
board type's pin map, swapping fixtures) and with `--video` `station-compatibility.mp4`. The plate check builds the
real fixture, with every board's nest and clamp; when the boards fit better turned (sliding in from the side), the
plan says so (`entry`) and the run uses it.

### A station from its own KiCad board

```bash
pcbtestkit facilities from-pcb station.kicad_pcb --out ~/.pcbtestkit/facilities/my-station
```

reads the station's board: the interface connector (the footprint with the most station-like net names, or
`--connector J1`), each pad's function from its net (`GND`, `AIN<n>`/`ADC<n>`/`CH<n>`, `VOUT<n>`/`VSUP<n>`,
`DIO<n>`/`GPIO<n>`, `CAN<n>_H/L`, `UART<n>_TX/RX`, `I2C<n>_SDA/SCL`, `RS485_<n>_A/B`, `USB_DP/DM`), the pad grid
(pitch, rows, numbering, rotation), its position from the outline's top-left corner, the plate (the outline) and the
holes (mounting holes and non-plated holes of 2 mm or more). The fixture gets the mating part (socket <-> header).
A station given straight to `station plan/run` as a `.kicad_pcb` is read the same way. `examples/make_station.py`
generates the example: 260 x 190 mm, a 2 x 40 socket with 32 analog inputs, 4 supplies, 2 CAN, 2 UART, 4 I2C and
8 DIO, and four M3 holes.

## What it changes, and what it does not

- The default `raspberry-pi` facility changes nothing: same plan, fixture and runner as before.
- The test plan records the facility (`facility` in `<board>-testplan.json`): its id, the connector pin map with what
  each pin carries, and the bus checks. With a fixed connector, the plan's `header` is that connector.
- The guide shows the facility's pin map instead of the Raspberry Pi wiring, and tells you where the runner goes.
- Not yet: a two-plate (clamshell) fixture gets the facility connector on its first plate only; a connector shared
  by several boards (pin groups per site) is not supported (give each board its own connector).
