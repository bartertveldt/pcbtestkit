"""MicroPython firmware for an RP2040 (Pico) or ESP32 used as the tester's ADC/IO front-end.

Copy to the board as main.py. Line protocol on USB serial (115200):
    ID?            -> OK PCBTESTKIT-FW 1
    READ <n>       -> OK <volts>   (channel n = 1-based index into CHANNELS)
    PWR <0|1>      -> OK           (DUT power relay/MOSFET)
    GPIO <pin> <v> -> OK
    CUR            -> OK <amps>    (only if CURRENT_ADC is configured)

Channels beyond the MCU's own ADC pins can go through a 74HC4067 16:1 mux:
set MUX_SELECT pins and MUX_ADC; channel numbers above len(CHANNELS) then select mux inputs.
"""
import sys
import time

import machine

# ---- configure for your wiring -------------------------------------------------
VREF = 3.3
CHANNELS = [26, 27, 28]          # Pico ADC0..2 (GP26..28); ESP32 e.g. [32, 33, 34, 35, 36, 39]
MUX_SELECT = []                  # e.g. [2, 3, 4, 5] for a 74HC4067 (S0..S3)
MUX_ADC = None                   # e.g. 26 if the mux output goes to GP26
POWER_PIN = 15
POWER_ACTIVE_HIGH = True
CURRENT_ADC = None               # e.g. 28 with a current-sense amplifier
CURRENT_A_PER_V = 1.0
SAMPLES = 32
# ---------------------------------------------------------------------------------

IS_ESP32 = sys.platform == "esp32"


def make_adc(pin):
    a = machine.ADC(machine.Pin(pin))
    if IS_ESP32:
        a.atten(machine.ADC.ATTN_11DB)
    return a


adcs = [make_adc(p) for p in CHANNELS]
mux_sel = [machine.Pin(p, machine.Pin.OUT) for p in MUX_SELECT]
mux_adc = make_adc(MUX_ADC) if MUX_ADC is not None else None
power = machine.Pin(POWER_PIN, machine.Pin.OUT, value=0 if POWER_ACTIVE_HIGH else 1)
cur_adc = make_adc(CURRENT_ADC) if CURRENT_ADC is not None else None


def volts(adc):
    tot = 0
    for _ in range(SAMPLES):
        tot += adc.read_u16()
    return tot / SAMPLES * VREF / 65535


def read_channel(n):
    if 1 <= n <= len(adcs):
        return volts(adcs[n - 1])
    k = n - len(adcs) - 1
    if mux_adc is None or not (0 <= k < 16):
        raise ValueError("no such channel")
    for bit, pin in enumerate(mux_sel):
        pin.value((k >> bit) & 1)
    time.sleep_ms(2)
    return volts(mux_adc)


def handle(line):
    parts = line.strip().split()
    if not parts:
        return None
    cmd = parts[0].upper()
    if cmd == "ID?":
        return "OK PCBTESTKIT-FW 1"
    if cmd == "READ":
        return "OK %.4f" % read_channel(int(parts[1]))
    if cmd == "PWR":
        on = parts[1] == "1"
        power.value(1 if on == POWER_ACTIVE_HIGH else 0)
        return "OK"
    if cmd == "GPIO":
        machine.Pin(int(parts[1]), machine.Pin.OUT).value(int(parts[2]))
        return "OK"
    if cmd == "CUR" and cur_adc is not None:
        return "OK %.5f" % (volts(cur_adc) * CURRENT_A_PER_V)
    return "ERR unknown command"


while True:
    try:
        line = sys.stdin.readline()
        resp = handle(line)
        if resp:
            print(resp)
    except Exception as e:  # keep serving
        print("ERR %s" % e)
