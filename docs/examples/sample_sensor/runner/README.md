# PCB TestKit test runner

Runs the generated test plan on a Raspberry Pi (or any Linux computer) with an ADC front-end, or on a PC with
an RP2040/ESP32 over USB serial. Standard library only in simulated mode; real hardware needs one small
package (below).

```bash
python3 pcbtest_runner.py test --simulate              # dry run, no hardware
python3 pcbtest_runner.py check-hw                     # read every channel once
python3 pcbtest_runner.py learn --units 3              # golden units -> testplan.learned.json
python3 pcbtest_runner.py test --plan testplan.learned.json --loop
```

Files in this folder: `testplan.json` (generated: probes, channels, limits, sequence) and `hardware.json`
(your wiring). Results go to `results/<serial>_<time>.json` and `results/results.csv`.

Useful options: `--serial SN`, `--only <step ids>`, `--stop-on-fail`, `--skip-manual`, `--non-interactive`,
`--sim-fail CH03` (simulate a failing channel), `--allow-commands` (needed for `command` steps).

## Safety

- DUT power is switched off at the end of every run, also on errors and Ctrl-C.
- An over-current at power-up aborts the run before any other step.
- `command` steps (shell commands in the plan) only run with `--allow-commands`.

## `hardware.json`

| Key | Options |
|---|---|
| `adc.type` | `ads1115` (I2C, 4 channels per address, up to 16 with 0x48-0x4B; `pip install smbus2`), `mcp3008` (SPI, 8 channels per chip select; `pip install spidev`), `serial` (RP2040/ESP32 with `firmware/probe_fw.py`; `pip install pyserial`), `simulated` |
| `channel_map` | `"auto"` (channels in order across the ADCs) or explicit `{"CH01": {"device": "0x48", "input": 0}}` |
| `power` | `{"type": "gpio", "pin": 17, "active_high": true}` (relay/MOSFET), `serial` (via the MCU), `manual` (the operator is prompted) |
| `current_sensor` | `none`, `ina219` (I2C; inrush limit and idle-current window), `serial` |
| `resistance` | `none`, or `source_resistor` for the unpowered short check (below) |
| `adc_max_v` | The ADC's input range; the fixture's dividers were sized for it |

## Short check circuit (`source_resistor`)

The unpowered short check needs a current source: a GPIO drives each rail through a known resistor R_ref, the
runner measures the rail voltage and computes the rail's resistance to GND, with the loading of the fixture's
dividers removed. Configure `{"type": "source_resistor", "gpio": 27, "r_ref_ohm": 1000, "v_source": 3.3}`.
Without it the step is reported as skipped.

## MCU front-end (`firmware/probe_fw.py`)

MicroPython for an RP2040 (Pico) or ESP32, copied to the board as `main.py`. It answers a line protocol on USB
serial (115200 baud): `ID?`, `READ <n>`, `PWR <0|1>`, `GPIO <pin> <v>`, `CUR`. Up to 16 extra channels through
a 74HC4067 multiplexer. Configure the pins at the top of the file.

## Batches (several boards per fixture)

A batch fixture has one site per board, each with its own header and power terminal. `test` asks the serial number
of every board, then follows the test plan's phases:

1. every board: visual and unpowered short check, then power-up with the supply checks;
2. every board is programmed **at the same time** (`program.command` once per board, `{site}` and `{serial}`
   filled in, needs `--allow-commands`) and, while the programmers run, the checks that need no firmware are
   measured board by board (rails from regulators, signals the MCU does not drive);
3. the checks that need the firmware (the MCU's GPIOs, rails it switches on, buses, functional steps); a board whose
   programming failed skips them and fails;
4. every board off.

`--sequential` tests one board after the other instead (each programmed in turn). Every result file has the time
each step took (`timings`), so the production timing in the guide can be checked. Each board is measured on its own
ADC inputs and only its own power is switched.
With `channel_map: "auto"`, site k uses the inputs right after site k-1's; `power` is a list with one entry per
site (generated for you). Override a site with `"sites": {"2": {"channel_map": {...}, "power": {...}}}`.
`--site 2 3` tests only those sites; results record the site of every board.

## Programming (step SP)

The fixture has a programming connector per board, wired to pogo pins on the board's programming header. With
`program.command` in `pcbtestkit.json` (copied into the plan) the runner runs it for every board, filling in
`{site}` and `{serial}`, e.g. `["openocd", "-f", "interface/cmsis-dap.cfg", "-f", "target/stm32g0x.cfg", "-c",
"program fw.elf verify reset exit"]`; it only runs with `--allow-commands` and fails the step on a non-zero exit
code. Without a command the operator programs the board through that site's connector and confirms.

## Your own tester (test facility driver)

When the fixture was made for a test facility with a driver (`pcbtestkit run ... --facility <id>`), this folder has
`facility_driver.py` and a `hardware.json` from that facility (`"facility": {"driver": "facility_driver.py", ...}`).
The runner then hands measuring (`read_v`), the supply (`power`, `current_a`) and the bus checks (`bus_test`, steps
`SB1`, ...) to the driver instead of the ADC and GPIO backends above; `--simulate` still runs without it. Set how to
reach the tester in `hardware.json` (for the example driver: `connection.host` / `port` and the SCPI `commands`).
A bus step without a driver method asks the operator (skipped with `--non-interactive`). The driver interface is in
`docs/FACILITIES.md` of PCB TestKit.
