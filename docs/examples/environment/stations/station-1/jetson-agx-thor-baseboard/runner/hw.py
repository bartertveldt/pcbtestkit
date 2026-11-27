"""Hardware backends for the test runner (Raspberry Pi / Linux SBC / MCU over serial).

Only the backends you configure are imported, so the runner works with just
the Python standard library in 'simulated' mode.

ADC     : ads1115 (I2C, 4 ch/device), mcp3008 (SPI, 8 ch/device), serial (MCU firmware), simulated
Power   : gpio (relay/MOSFET), serial (MCU), manual (operator prompt), simulated
Current : ina219 (I2C), serial, none
Facility: a test facility's own driver (hardware.json "facility": {"driver": "facility_driver.py", "class": ...})
          measures, switches power and runs the bus checks instead of all of the above.
"""
from __future__ import annotations

import importlib.util
import os
import random
import time
from typing import Any, Dict, Optional, Tuple


class HardwareError(RuntimeError):
    pass


def _int(v) -> int:
    return int(v, 0) if isinstance(v, str) else int(v)


# --------------------------------------------------------------------------- #
# ADCs
# --------------------------------------------------------------------------- #
class Ads1115:
    PGA = {6.144: 0, 4.096: 1, 2.048: 2, 1.024: 3, 0.512: 4, 0.256: 5}

    def __init__(self, bus: int = 1, addresses=("0x48",), fsr_v: float = 4.096, samples: int = 4):
        try:
            from smbus2 import SMBus
        except ImportError:
            try:
                from smbus import SMBus  # type: ignore
            except ImportError:
                raise HardwareError("ads1115 needs 'smbus2' (pip install smbus2) and I2C enabled (raspi-config)")
        self.bus = SMBus(bus)
        self.addresses = [_int(a) for a in addresses]
        self.fsr = float(fsr_v)
        self.pga = self.PGA.get(self.fsr, 1)
        self.samples = samples

    def channels(self) -> int:
        return 4 * len(self.addresses)

    def read_raw(self, addr: int, inp: int) -> float:
        cfg = (1 << 15) | ((4 + inp) << 12) | (self.pga << 9) | (1 << 8) | (4 << 5) | 3  # single shot, 128 SPS
        self.bus.write_i2c_block_data(addr, 0x01, [(cfg >> 8) & 0xFF, cfg & 0xFF])
        deadline = time.time() + 0.1
        while time.time() < deadline:
            hi, lo = self.bus.read_i2c_block_data(addr, 0x01, 2)
            if hi & 0x80:
                break
            time.sleep(0.002)
        hi, lo = self.bus.read_i2c_block_data(addr, 0x00, 2)
        raw = (hi << 8) | lo
        if raw & 0x8000:
            raw -= 1 << 16
        return raw * self.fsr / 32768.0

    def read(self, index: int, address: Optional[int] = None, inp: Optional[int] = None) -> float:
        if address is None:
            address, inp = self.addresses[index // 4], index % 4
        vals = [self.read_raw(address, inp) for _ in range(self.samples)]
        return sum(vals) / len(vals)


class Mcp3008:
    def __init__(self, spi_bus: int = 0, devices=(0,), vref: float = 3.3, samples: int = 8):
        try:
            import spidev  # type: ignore
        except ImportError:
            raise HardwareError("mcp3008 needs 'spidev' and SPI enabled (raspi-config)")
        self.spis = []
        for dev in devices:
            s = spidev.SpiDev()
            s.open(spi_bus, int(dev))
            s.max_speed_hz = 1000000
            self.spis.append(s)
        self.vref = float(vref)
        self.samples = samples

    def channels(self) -> int:
        return 8 * len(self.spis)

    def read(self, index: int, device: Optional[int] = None, inp: Optional[int] = None) -> float:
        if device is None:
            device, inp = index // 8, index % 8
        s = self.spis[device]
        tot = 0
        for _ in range(self.samples):
            r = s.xfer2([1, (8 + inp) << 4, 0])
            tot += ((r[1] & 3) << 8) | r[2]
        return tot / float(self.samples) * self.vref / 1023.0


class SerialLink:
    """Line protocol spoken by runner/firmware/probe_fw.py (MicroPython) or your own firmware:
       READ <n>  -> OK <volts>        PWR <0|1> -> OK        CUR -> OK <amps>
       GPIO <pin> <0|1> -> OK         ID? -> OK PCBTESTKIT-FW <ver>"""

    def __init__(self, port: str = "/dev/ttyACM0", baud: int = 115200, timeout: float = 2.0):
        try:
            import serial  # type: ignore
        except ImportError:
            raise HardwareError("serial backend needs 'pyserial' (pip install pyserial)")
        self.ser = serial.Serial(port, baud, timeout=timeout)
        time.sleep(0.3)
        self.ser.reset_input_buffer()

    def cmd(self, line: str) -> str:
        self.ser.write((line + "\n").encode())
        resp = self.ser.readline().decode(errors="replace").strip()
        if not resp.startswith("OK"):
            raise HardwareError("MCU replied %r to %r" % (resp, line))
        return resp[2:].strip()

    def channels(self) -> int:
        return 64

    def read(self, index: int, **_) -> float:
        return float(self.cmd("READ %d" % (index + 1)))


class Simulated:
    """Returns plausible values from the test plan (nominal or the middle of the limits) +/- noise."""

    def __init__(self, plan: Dict[str, Any], fail_channels=(), seed: int = 1):
        self.expect: Dict[str, float] = {}
        self.ratio: Dict[str, float] = {}
        for p in plan.get("probes", []):
            ch = p.get("channel")
            if not ch:
                continue
            e = p.get("expected") or {}
            v = e.get("nominal_v")
            if v is None and e.get("min_v") is not None:
                v = (e["min_v"] + e["max_v"]) / 2
            self.expect[ch] = 3.3 if v is None else v
            self.ratio[ch] = (p.get("divider") or {}).get("ratio", 1.0)
        for st in plan.get("sequence", []):
            for c in st.get("checks", []) or []:
                if c.get("channel") and c.get("min_v") is not None and c.get("max_v") is not None:
                    self.expect[c["channel"]] = (c["min_v"] + c["max_v"]) / 2
        self.fail = set(fail_channels)
        self.rng = random.Random(seed)
        self.order = sorted(self.expect)

    def channels(self) -> int:
        return 512

    def read_channel(self, ch: str) -> float:
        v = self.expect.get(ch, 0.0)
        if ch in self.fail:
            v *= 0.6
        v += self.rng.uniform(-0.01, 0.01)
        return v / self.ratio.get(ch, 1.0)


# --------------------------------------------------------------------------- #
# Power / current / GPIO
# --------------------------------------------------------------------------- #
class Gpio:
    def __init__(self):
        self.impl = None
        try:
            import RPi.GPIO as G  # type: ignore
            G.setmode(G.BCM)
            G.setwarnings(False)
            self.impl = ("rpi", G)
        except Exception:
            try:
                import lgpio  # type: ignore
                self.impl = ("lgpio", lgpio, lgpio.gpiochip_open(0))
            except Exception:
                self.impl = None

    def write(self, pin: int, level: int) -> None:
        if self.impl is None:
            raise HardwareError("no GPIO library (install RPi.GPIO or lgpio) - use power.type 'manual' instead")
        if self.impl[0] == "rpi":
            G = self.impl[1]
            G.setup(pin, G.OUT)
            G.output(pin, G.HIGH if level else G.LOW)
        else:
            lg, h = self.impl[1], self.impl[2]
            lg.gpio_claim_output(h, pin)
            lg.gpio_write(h, pin, 1 if level else 0)


class Ina219:
    def __init__(self, bus: int = 1, address="0x40", shunt_ohm: float = 0.1):
        try:
            from smbus2 import SMBus
        except ImportError:
            raise HardwareError("ina219 needs 'smbus2'")
        self.bus = SMBus(bus)
        self.addr = _int(address)
        self.shunt = float(shunt_ohm)
        self.bus.write_i2c_block_data(self.addr, 0x00, [0x39, 0x9F])  # 32V, /8 gain, 12-bit, continuous

    def _reg(self, r: int) -> int:
        hi, lo = self.bus.read_i2c_block_data(self.addr, r, 2)
        return (hi << 8) | lo

    def current_a(self) -> float:
        raw = self._reg(0x01)
        if raw & 0x8000:
            raw -= 1 << 16
        return raw * 10e-6 / self.shunt

    def bus_v(self) -> float:
        return (self._reg(0x02) >> 3) * 0.004


def load_driver(cfg: Dict[str, Any]):
    """The test facility's driver class from the runner folder (hardware.json "facility"), or None."""
    fac = cfg.get("facility") or {}
    if not fac.get("driver"):
        return None
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), fac["driver"])
    if not os.path.exists(path):
        raise HardwareError("facility driver %s not found next to the runner" % fac["driver"])
    spec = importlib.util.spec_from_file_location("facility_driver", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    cls = getattr(mod, fac.get("class", "Driver"), None)
    if cls is None:
        raise HardwareError("%s has no class %s" % (fac["driver"], fac.get("class", "Driver")))
    return cls


def facility_inputs(plan: Dict[str, Any], site: int) -> Dict[str, str]:
    """What board ``site`` uses on the test facility, by its own signal names: each channel's input ("CH01":
    "AIN3"), its supply output ("VIN": "VOUT2") and its bus lines ("CAN_H": "CAN2_H"), from the facility's
    connector; or each channel's pin on the generated header ("PIN4") when the facility has no connector.
    A station's shared connector gives every board of the batch its own inputs, supply and bus instance."""
    allc = (plan.get("facility") or {}).get("connectors") or []
    shared = [c for c in allc if c.get("shared")]
    if shared:
        return {q["signal"]: q["function"] for c in shared for q in c["pins"]
                if q.get("site") == site and q["signal"] not in ("GND", "NC")}
    conns = [c for c in allc if int(c.get("site", 1)) == site]
    if conns:
        return {q["signal"]: q["function"] for c in conns for q in c["pins"] if q["signal"] not in ("GND", "NC")}
    return {p["channel"]: "PIN%d" % p["header_pin"] for p in plan.get("probes", []) if p.get("channel") and p.get("header_pin")}


class Hardware:
    """Facade used by the runner: read_v(channel) returns the voltage at the DUT (divider undone).

    Batches: the fixture has one site per board, each with its own header. select_site(k) points the channels
    and the power switch at site k: with channel_map "auto", site k uses the ADC inputs after those of site k-1;
    "power" may be a list with one entry per site; "sites": {"2": {"channel_map": ..., "power": ...}} overrides."""

    def __init__(self, cfg: Dict[str, Any], plan: Dict[str, Any], simulate: bool = False, fail_channels=(), interactive=True):
        self.cfg = cfg
        self.plan = plan
        self.interactive = interactive
        self.site = 1
        self.nsites = int((plan.get("batch") or {}).get("count") or 1)
        self.ratio = {p["channel"]: (p.get("divider") or {}).get("ratio", 1.0) for p in plan.get("probes", []) if p.get("channel")}
        self.driver = None
        cls = None if simulate else load_driver(cfg)
        if cls is not None:                     # the test facility's driver does the measuring and switching
            self.driver = cls(cfg, plan)
            self.sim = self.serial = self.adc = self.gpio = self.cur = None
            self.map, self.power_list, self.power_cfg, self.power_type = {}, None, {}, "driver"
            self.res_cfg = dict(cfg.get("resistance") or {})
            self.select_site(1)
            return
        adc = dict(cfg.get("adc") or {})
        self.sim = None
        self.serial = None
        if simulate or adc.get("type") == "simulated":
            self.sim = Simulated(plan, fail_channels)
            self.adc = self.sim
        elif adc.get("type") == "ads1115":
            self.adc = Ads1115(adc.get("i2c_bus", 1), adc.get("addresses", ["0x48"]), adc.get("fsr_v", 4.096))
        elif adc.get("type") == "mcp3008":
            self.adc = Mcp3008(adc.get("spi_bus", 0), adc.get("devices", [0]), adc.get("vref", 3.3))
        elif adc.get("type") == "serial":
            self.serial = SerialLink(adc.get("port", "/dev/ttyACM0"), adc.get("baud", 115200))
            self.adc = self.serial
        else:
            raise HardwareError("unknown adc.type %r" % adc.get("type"))
        self.map = self._channel_map(cfg.get("channel_map", "auto"))
        raw = cfg.get("power") or {}
        self.power_list = [dict(p or {}) for p in raw] if isinstance(raw, list) else None
        pw = dict(self.power_list[0] if self.power_list else raw)
        self.power_type = "simulated" if self.sim else pw.get("type", "manual")
        self.power_cfg = pw
        uses_gpio = any(p.get("type") == "gpio" for p in (self.power_list or [pw])) or any(
            (s or {}).get("power", {}).get("type") == "gpio" for s in (cfg.get("sites") or {}).values())
        self.gpio = Gpio() if (not self.sim and (uses_gpio or cfg.get("resistance", {}).get("type") == "source_resistor")) else None
        cs = dict(cfg.get("current_sensor") or {})
        self.cur = None
        if not self.sim and cs.get("type") == "ina219":
            self.cur = Ina219(cs.get("i2c_bus", 1), cs.get("address", "0x40"), cs.get("shunt_ohm", 0.1))
        self.res_cfg = dict(cfg.get("resistance") or {})

    def select_site(self, k: int) -> None:
        """Measure and power board k of a batch (fixture site k)."""
        if k < 1 or k > self.nsites:
            raise HardwareError("site %d: the fixture has %d site(s)" % (k, self.nsites))
        self.site = int(k)
        if self.driver is not None:
            if hasattr(self.driver, "select_site"):
                self.driver.select_site(self.site, facility_inputs(self.plan, self.site))
            return
        sc = (self.cfg.get("sites") or {}).get(str(k)) or {}
        m = sc.get("channel_map", self.cfg.get("channel_map", "auto"))
        self.map = self._channel_map(m, offset=(k - 1) * len(self.ratio) if (m == "auto" or not m) else 0)
        if sc.get("power"):
            pw = dict(sc["power"])
        elif self.power_list:
            pw = dict(self.power_list[min(k, len(self.power_list)) - 1])
        else:
            pw = dict(self.cfg.get("power") or {})
        self.power_cfg = pw
        self.power_type = "simulated" if self.sim else pw.get("type", "manual")

    def _channel_map(self, m, offset: int = 0) -> Dict[str, Tuple]:
        chans = sorted(self.ratio)
        out: Dict[str, Tuple] = {}
        if m == "auto" or not m:
            if offset + len(chans) > self.adc.channels():
                raise HardwareError("%d channels needed (site %d) but the ADC config has %d" % (
                    offset + len(chans), self.site, self.adc.channels()))
            for i, ch in enumerate(chans):
                out[ch] = (offset + i,)
        else:
            for ch in chans:
                if ch not in m:
                    raise HardwareError("channel %s missing in hardware.json channel_map" % ch)
                e = m[ch]
                if isinstance(e, dict):
                    out[ch] = (None, _int(e.get("device", 0)), int(e.get("input", 0)))
                else:
                    out[ch] = (int(e),)
        return out

    def read_v(self, ch: str) -> float:
        if self.driver is not None:
            raw = float(self.driver.read_v(ch))
        elif self.sim:
            raw = self.sim.read_channel(ch)
        else:
            loc = self.map[ch]
            if len(loc) == 1:
                raw = self.adc.read(loc[0])
            else:
                raw = self.adc.read(0, loc[1], loc[2])
        return raw * self.ratio.get(ch, 1.0)

    def power(self, on: bool) -> None:
        if self.driver is not None:
            self.driver.power(on)
            return
        if self.power_type == "simulated":
            return
        if self.power_type == "gpio":
            lvl = 1 if on == bool(self.power_cfg.get("active_high", True)) else 0
            self.gpio.write(int(self.power_cfg["pin"]), lvl)
        elif self.power_type == "serial":
            (self.serial or SerialLink(self.power_cfg.get("port", "/dev/ttyACM0"))).cmd("PWR %d" % (1 if on else 0))
        else:
            if self.interactive:
                input("  >> Turn DUT power %s%s and press Enter " % ("ON" if on else "OFF",
                                                                   " (site %d)" % self.site if self.nsites > 1 else ""))

    def current_a(self) -> Optional[float]:
        if self.driver is not None:
            f = getattr(self.driver, "current_a", None)
            return None if f is None else f()
        if self.sim:
            return 0.02
        if self.cur:
            return self.cur.current_a()
        if self.serial and (self.cfg.get("current_sensor") or {}).get("type") == "serial":
            return float(self.serial.cmd("CUR"))
        return None

    def can_measure_resistance(self) -> bool:
        if self.driver is not None:
            return hasattr(self.driver, "resistance")
        return bool(self.sim) or self.res_cfg.get("type") == "source_resistor"

    def bus_test(self, step: Dict[str, Any]) -> Optional[Tuple[bool, str]]:
        """A bus check (CAN, USB, Ethernet, UART, I2C, RS-485) by the facility driver: (passed, note), or None
        when nothing here can run it (the operator then checks it)."""
        if self.sim:
            return True, "simulated"
        f = getattr(self.driver, "bus_test", None) if self.driver is not None else None
        if f is None:
            return None
        ok, note = f(step)
        return bool(ok), str(note or "")

    def close(self) -> None:
        f = getattr(self.driver, "close", None) if self.driver is not None else None
        if f is not None:
            f()

    def resistance(self, ch: str, settle_s: float = 0.3) -> float:
        """Drive the rail through R_ref from a GPIO, read the node, compute DUT resistance to ground."""
        if self.driver is not None:
            return float(self.driver.resistance(ch))
        if self.sim:
            return 5000.0
        r_ref = float(self.res_cfg.get("r_ref_ohm", 1000))
        vs = float(self.res_cfg.get("v_source", 3.3))
        pin = int(self.res_cfg["gpio"])
        self.gpio.write(pin, 1)
        try:
            time.sleep(settle_s)
            v = self.read_v(ch)
        finally:
            self.gpio.write(pin, 0)
        if v >= vs * 0.999:
            return float("inf")
        r_par = r_ref * v / max(1e-9, vs - v)
        div = next((p.get("divider") for p in self.plan.get("probes", []) if p.get("channel") == ch), None)
        if div:
            r_div = div["r_top_ohm"] + div["r_bottom_ohm"]
            if r_par >= r_div * 0.999:
                return float("inf")
            return 1.0 / (1.0 / r_par - 1.0 / r_div)
        return r_par

    def gpio_out(self, pin: int, level: int) -> None:
        if self.driver is not None:
            f = getattr(self.driver, "gpio_out", None)
            if f is None:
                raise HardwareError("the facility driver has no gpio_out")
            f(pin, level)
            return
        if self.sim:
            return
        if self.serial:
            self.serial.cmd("GPIO %d %d" % (pin, level))
        else:
            (self.gpio or Gpio()).write(pin, level)
