"""Runner driver for the example bench tester (copied into the runner folder as facility_driver.py).

The runner calls a facility driver for everything it measures or switches. This one talks to the tester with
SCPI-style text commands over TCP (standard library only); change the commands in hardware.json, or this file,
for your tester. The runner's own --simulate mode does not load the driver.

The interface (only read_v and power are required):

    Driver(hardware, plan)            hardware = hardware.json, plan = testplan.json
    select_site(site, inputs)         board `site` of a batch; inputs = {"CH01": "AIN3", ...} for that site
    read_v(channel) -> float          volts at the tester input (the runner undoes the fixture's divider)
    power(on)                         switch the board's supply
    current_a() -> float or None      supply current
    bus_test(step) -> (ok, note)      a CAN / USB / Ethernet / UART / I2C check; step has "bus", "via", "signals",
                                      "check" (the facility's settings for that bus)
    close()
"""
import socket


class Driver:
    def __init__(self, hardware, plan):
        self.hw = hardware
        self.cmds = dict(hardware.get("commands") or {})
        con = hardware.get("connection") or {}
        self.sock = socket.create_connection((con.get("host", "127.0.0.1"), int(con.get("port", 5025))),
                                             timeout=float(con.get("timeout_s", 3.0)))
        self.buf = b""
        self.site, self.inputs = 1, {}

    # -- transport --------------------------------------------------------------------------------
    def query(self, line):
        self.sock.sendall((line + "\n").encode())
        while b"\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise IOError("the tester closed the connection")
            self.buf += chunk
        reply, self.buf = self.buf.split(b"\n", 1)
        return reply.decode(errors="replace").strip()

    def send(self, line):
        self.sock.sendall((line + "\n").encode())

    # -- the runner's interface -------------------------------------------------------------------------
    def select_site(self, site, inputs):
        self.site, self.inputs = site, dict(inputs)

    def read_v(self, channel):
        inp = self.inputs.get(channel, channel)               # "AIN3" -> input 3
        num = "".join(c for c in inp if c.isdigit()) or inp
        return float(self.query(self.cmds.get("volt", "MEAS:VOLT? (@{input})").format(input=num, site=self.site)))

    def power(self, on):
        self.send(self.cmds.get("power", "OUTP{output} {state}").format(output=1, state="ON" if on else "OFF",
                                                                          site=self.site))

    def current_a(self):
        if "current" not in self.cmds:
            return None
        return float(self.query(self.cmds["current"].format(output=1, site=self.site)))

    def bus_test(self, step):
        args = " ".join("%s=%s" % kv for kv in sorted((step.get("check") or {}).items()))
        reply = self.query(self.cmds.get("bus", "BUS:{bus}:TEST? {args}").format(bus=step["bus"].upper(), args=args,
                                                                                 site=self.site))
        return reply.upper().startswith(("OK", "PASS", "1")), reply[:60]

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
