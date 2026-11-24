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
