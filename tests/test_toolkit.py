"""End-to-end tests on the generated sample project (stdlib unittest).

    python3 -m unittest discover -s tests -v
"""
import glob
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "examples"))

import make_sample  # noqa: E402
from pcbtestkit import sexpr  # noqa: E402
from pcbtestkit.agent import Workspace, tool_definitions  # noqa: E402
from pcbtestkit.analysis import run_analysis, summarize  # noqa: E402
from pcbtestkit.fixture import build_fixture  # noqa: E402
from pcbtestkit.jig import build_jig  # noqa: E402
from pcbtestkit.pcb import load_board  # noqa: E402
from pcbtestkit.pipeline import run_pipeline  # noqa: E402
from pcbtestkit.project import Design  # noqa: E402
from pcbtestkit.testplan import build_testplan, design_divider  # noqa: E402


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pcbtk_")
        cls.proj = os.path.join(cls.tmp, "sample_sensor")
        make_sample.main(cls.proj)
        cls.pcb = os.path.join(cls.proj, "sample_sensor.kicad_pcb")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def design(self, **over):
        return Design.load(self.pcb, overrides=over or None)


class TestParsing(Base):
    def test_sexpr_roundtrip(self):
        node = sexpr.parse('(a "x \\"q\\"" (b 1 2) (c yes))')
        self.assertEqual(node[1], 'x "q"')
        self.assertEqual(sexpr.parse(sexpr.dumps(node)), node)

    def test_board_and_schematic(self):
        d = self.design()
        self.assertEqual(len(d.board.footprints), 30)
        self.assertEqual(len(d.board.edge_segs), 40)
        self.assertIn("+3V3", d.power_symbol_nets)
        self.assertEqual(d.sch.by_ref["U1"].fields.get("MPN"), "LM7805CT")   # from the sub-sheet
        self.assertEqual(d.board.by_ref["U1"].get_field("MPN"), "LM7805CT")
        self.assertEqual(d.supply()["net"], "VIN_12V")


class TestAnalysis(Base):
    def test_seeded_weak_points_found(self):
        ids = {(f.id, f.severity) for f in run_analysis(self.design(), include_kicad=False)}
        for expected in [("MFG-TRACK-WIDTH", "critical"), ("MFG-VIA", "critical"), ("REL-RES-POWER", "critical"),
                         ("ASM-MLCC-EDGE", "major"), ("REL-ELCAP-HEAT", "major"), ("REL-SMD-CONNECTOR", "major"),
                         ("ASM-THT-SOLID-ZONE", "major"), ("ASM-TOMBSTONE", "major"), ("ELEC-FLOATING", "major"),
                         ("ELEC-ESD", "minor"), ("REL-THERMAL-PAD", "minor"), ("MFG-ACID-TRAP", "minor")]:
            self.assertIn(expected, ids)

    def test_no_false_heat_on_mcu(self):
        f = [x for x in run_analysis(self.design(), include_kicad=False) if x.id == "REL-ELCAP-HEAT"][0]
        self.assertNotIn("U3", {i["heat_source"] for i in f.items})


class TestPlanFixtureJig(Base):
    def test_testplan(self):
        tp = build_testplan(self.design(), [])
        nets = {p["net"]: p for p in tp["probes"] if p.get("channel")}
        self.assertEqual(set(nets), {"VIN_12V", "+3V3", "+5V", "/SDA", "/SCL"})
        self.assertAlmostEqual(nets["VIN_12V"]["divider"]["ratio"], 4.3, places=3)
        ids = [p["id"] for p in tp["probes"]]
        self.assertEqual(len(ids), len(set(ids)))
        for a in tp["probes"]:
            for b in tp["probes"]:
                if a is not b:
                    self.assertGreaterEqual(((a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2) ** 0.5, tp["pogo"]["min_spacing_mm"] - 1e-9)
        self.assertIn("VBUS", [u["net"] for u in tp["coverage"]["untestable"]])

    def test_divider(self):
        d = design_divider(12.6, 3.3, 10000)
        self.assertLessEqual(12.6 / d["ratio"], 3.3 * 0.9 + 1e-9)

    def test_fixture_modes_reparse(self):
        for over in ({}, {"fixture": {"outline": "outline"}}, {"fixture": {"panel": {"cols": 2, "rows": 2}}},
                     {"testplan": {"probe_side": "top"}}):
            d = self.design(**over)
            fx = build_fixture(d, build_testplan(d, []))
            path = os.path.join(self.tmp, "fx.kicad_pcb")
            with open(path, "w") as fh:
                fh.write(fx["kicad_pcb"])
            b = load_board(path)
            pogos = [f for f in b.footprints if f.lib_id.startswith("PCBTestKit:Pogo")]
            self.assertEqual(len(pogos), fx["pogo_count"])
            self.assertTrue(b.edge_segs)

    def test_jig_watertight(self):
        r = build_jig(self.design(), os.path.join(self.tmp, "j.stl"), os.path.join(self.tmp, "j.scad"))
        self.assertTrue(r["mesh"]["watertight"])
        self.assertGreater(r["mesh"]["volume_mm3"], 0)
        self.assertEqual(sorted(r["parts_to_place"]), ["C1", "C2", "J2", "J3", "U1"])
        holds = {p["ref"]: p for p in r["pockets"] if p["type"] == "hold"}
        self.assertAlmostEqual(holds["U1"]["depth_mm"], holds["U1"]["part_height_mm"] + 0.1, places=2)


class TestAgentAPI(Base):
    def ws(self):
        return Workspace(self.pcb, persist=False)

    def test_guardrails(self):
        ws = self.ws()
        self.assertEqual(ws.call("set_net_specs", {"updates": [{"net": "3V3", "nominal_v": 3.3}]})["error"]["did_you_mean"], ["+3V3"])
        self.assertEqual(ws.call("set_net_specs", {"updates": [{"net": "+3V3", "nominal_v": 5}]})["error"]["code"], "CONFIRM_REQUIRED")
        self.assertEqual(ws.call("set_net_specs", {"updates": [{"net": "+3V3", "min_v": 3.5, "max_v": 3.1}]})["error"]["code"], "BAD_LIMITS")
        self.assertEqual(ws.call("add_test_step", {"step": {"name": "x", "type": "voltage", "checks": [{"net": "/LED", "min_v": 0, "max_v": 1}]}})["error"]["code"], "NET_NOT_PROBED")
        self.assertEqual(ws.call("set_supply", {"voltage": 120})["error"]["code"], "CONFIRM_REQUIRED")
        self.assertEqual(ws.call("get_overview", {"bogus": 1})["error"]["code"], "UNKNOWN_ARGUMENT")
        self.assertEqual(ws.call("add_test_step", {"step": {"name": "c", "type": "command", "command": {"argv": ["true"]}}})["error"]["code"], "CONFIRM_REQUIRED")

    def test_decisions_and_writes(self):
        ws = self.ws()
        dec = ws.call("get_open_decisions")
        topics = {d["topic"] for d in dec["decisions"]}
        self.assertTrue({"supply", "confirm_rails", "test_access", "part_heights"} <= topics)
        r = ws.call("set_net_specs", {"updates": [{"net": "SDA", "min_v": 2.9, "max_v": 3.5}], "rationale": "pull-up"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["effects"]["checks_learn"], [2, 1])
        self.assertTrue(ws.call("waive_finding", {"check_id": "ASM-FIDUCIALS", "reason": "fiducials on panel rails"})["ok"])
        self.assertTrue(ws.call("undo_last")["ok"])
        v = ws.call("validate")
        self.assertTrue(v["ok"])

    def test_tool_exports(self):
        for fmt in ("mcp", "anthropic", "openai"):
            defs = tool_definitions(fmt)
            self.assertGreater(len(defs), 20)
            json.dumps(defs)

    def test_mcp_stdio(self):
        from pcbtestkit.mcp_server import serve
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_overview", "arguments": {}}}]
        out = io.StringIO()
        serve(self.pcb, stdin=io.StringIO("\n".join(json.dumps(m) for m in msgs) + "\n"), stdout=out)
        lines = [json.loads(l) for l in out.getvalue().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["result"]["serverInfo"]["name"], "pcbtestkit")
        self.assertFalse(lines[1]["result"]["isError"])


class TestClassSystems(Base):
    """The class-system framework: definitions, engine features, validator, board results, AI tools, CLI."""

    def test_builtin_systems_validate(self):
        from pcbtestkit.classsys import validate
        from pcbtestkit.classsys.registry import builtin_raw
        raw = builtin_raw()
        self.assertGreaterEqual(len(raw), 8)
        for sid, r in raw.items():
            res = validate(r)
            self.assertTrue(res["ok"], "%s: %s" % (sid, res["errors"]))
            self.assertGreaterEqual(len(res["tests"]), 3, sid)
            self.assertIn(r["status"], ("verified", "secondary", "partial"), sid)
            self.assertTrue(r["source"].startswith("https://"), sid)

    def test_engine_rule_features(self):
        from pcbtestkit.classsys import System, evaluate, validate
        raw = {"id": "t", "name": "T", "status": "draft", "parameters": {"reg": 0.05},
               "tables": {"sp": {"key": "dv", "rows": [{"upto": 50, "X": 0.1}, {"upto": 100, "X": 0.5}],
                                 "beyond": {"from": 100, "per_unit": {"X": 0.01}}}},
               "dimensions": [{"id": "tier", "levels": [
                   {"id": "a", "limits": {"ring": 0.2, "ring_via_outer": 0.15,
                                          "track": [{"when": {"layers": {"max": 2}}, "value": "6mil"}, {"value": "5mil"}],
                                          "via_pad": [{"items": {"finished": {"max": 0.3}}, "value": "finished + 0.3"}]}},
                   {"id": "b", "requires": {"finish": ["ENIG"]}, "limits": {"ring": "reg", "track": "3mil"}}]},
                   {"id": "env", "choose": "user", "default": "u", "levels": [
                       {"id": "u", "limits": {"voltage_gap_outer": {"lookup": "sp", "column": "X"}}}]}],
               "always": {"microvias": False, "layers_allowed": [2, 4]}}
        self.assertTrue(validate(raw, run_tests=False)["ok"])
        s = System(raw)
        ev = lambda meas, ctx=None, target=None: evaluate(s, None, dict({"layers": 2}, **(ctx or {})), target=target, synthetic=meas)
        # specific metric beats its alias: via ring 0.16 passes 'a' although 'ring' says 0.2
        self.assertEqual(ev({"ring_via_outer": 0.16, "track_outer": 0.16})["required"]["class"], "a-u")
        # conditional limit: 6 mil on 2 layers, 5 mil on 4 layers
        self.assertTrue(ev({"track_outer": 0.13}, {"layers": 4})["required"]["fits"])
        self.assertIsNone(ev({"track_outer": 0.13})["required"]["class"])          # 'b' needs ENIG
        self.assertEqual(ev({"track_outer": 0.13}, {"finish": "ENIG"})["required"]["class"], "b-u")
        # per-item expression: pad >= finished + 0.3 only for holes <= 0.3 mm
        r = ev({"via_pad": [{"value": 0.5, "finished": 0.25}, {"value": 0.4, "finished": 0.35}]}, target="a-u")
        self.assertEqual(r["target"]["violations"]["via_pad"]["count"], 1)
        # lookup table with per-volt growth beyond the last row: 150 V -> 0.5 + 50 * 0.01 = 1.0 mm
        r = ev({"voltage_gap_outer": [{"value": 0.9, "dv": 150}, {"value": 0.2, "dv": 40}]}, target="a-u")
        self.assertEqual(r["target"]["violations"]["voltage_gap_outer"]["items"][0]["limit"], 1.0)
        # features and allowed values are process limits for every class
        r = ev({"microvias": 3, "layers_allowed": 6})
        self.assertEqual(sorted(r["process"]["violations"]), ["layers_allowed", "microvias"])
        self.assertFalse(r["required"]["fits"])

    def test_alternative_levels(self):
        """Stack-up style dimensions: monotonic false, layers_min, and which level a board that fits none is told to aim for."""
        from pcbtestkit.classsys import System, evaluate, validate
        raw = {"id": "alt", "name": "alt", "status": "user", "dimensions": [{"id": "s", "monotonic": False, "levels": [
            {"id": "cheap", "limits": {"layers_allowed": [2], "track": 0.2, "ring": 0.3}},
            {"id": "fine", "limits": {"layers_allowed": [2], "track": 0.125, "ring": 0.3}},
            {"id": "heavy", "limits": {"layers_allowed": [2], "track": 0.175, "ring": 0.3}},
            {"id": "multi", "limits": {"layers_min": 6, "track": 0.1, "ring": 0.3}}]}]}
        res = validate(raw, run_tests=False)
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(res["warnings"], [])                       # coarser 'heavy' after 'fine' is fine here
        self.assertTrue(validate(dict(raw, dimensions=[dict(raw["dimensions"][0], monotonic=None)]), run_tests=False)["warnings"])
        s = System(raw)
        ev = lambda meas: evaluate(s, None, {"layers": 2}, synthetic=dict({"layers_allowed": 2, "layers_min": 2}, **meas))
        self.assertEqual(ev({"track_outer": 0.15})["required"]["class"], "fine")
        self.assertEqual(evaluate(s, None, {"layers": 8}, synthetic={"layers_min": 8, "track_outer": 0.1})["required"]["class"], "multi")
        # nothing fits: aim for the level with the smallest shortfall, not the last one in the list
        r = ev({"track_outer": 0.1})["required"]
        self.assertEqual({d["level"] for d in r["drivers"]}, {"fine"})
        # equal shortfall everywhere: the cheapest alternative
        r = ev({"ring_pth_outer": [{"value": 0.25, "finished": 0.8}]})["required"]
        self.assertEqual({d["level"] for d in r["drivers"]}, {"cheap"})

    def test_requires_mismatch_is_reported(self):
        """A level ruled out by 'requires' (4 layers only with ENIG) is the nearest option for a 4-layer HASL board:
        an order option beats a redesign of the layer count, and the finish is named as the blocker."""
        from pcbtestkit.classsys import System, evaluate, validate
        raw = {"id": "fin", "name": "fin", "status": "user", "dimensions": [{"id": "s", "monotonic": False, "levels": [
            {"id": "2L-HASL", "requires": {"finish": ["HASL", None]}, "limits": {"layers_allowed": [2], "ring": 0.3}},
            {"id": "4L-ENIG", "requires": {"finish": ["ENIG", None]}, "limits": {"layers_allowed": [4], "ring": 0.3}},
            {"id": "6L", "requires": {"layers": {"min": 6}}, "limits": {"ring": 0.2}}]}]}
        self.assertTrue(validate(raw, run_tests=False)["ok"])
        s = System(raw)
        meas = {"layers_allowed": 4, "ring_pth_outer": [{"value": 0.25, "finished": 0.8}]}
        r = evaluate(s, None, {"layers": 4, "finish": "HASL"}, synthetic=meas)
        req = r["required"]
        self.assertIsNone(req["class"])
        self.assertEqual({d["level"] for d in req["drivers"]}, {"4L-ENIG"})      # not 2L-HASL (layer count) or 6L
        fin = [d for d in req["drivers"] if d["metric"] == "_requires"]
        self.assertEqual([(d["label"], d["measured"], d["limit"]) for d in fin], [("surface finish", "HASL", "ENIG")])
        # the same board with ENIG: no requires driver, the ring is what blocks 4L-ENIG
        req = evaluate(s, None, {"layers": 4, "finish": "ENIG"}, synthetic=meas)["required"]
        self.assertEqual([(d["level"], d["metric"]) for d in req["drivers"]], [("4L-ENIG", "ring_pth_outer")])
        # ordering 4L-ENIG for the HASL board names the finish in plain words
        tg = evaluate(s, None, {"layers": 4, "finish": "HASL"}, target="4L-ENIG", synthetic=meas)["target"]
        self.assertEqual(tg["violations"]["_requires_s"]["label"], "s 4L-ENIG needs surface finish ENIG (board: HASL)")

    def test_validator_catches_mistakes(self):
        from pcbtestkit.classsys import validate
        bad = {"id": "Bad Id", "name": "x", "status": "verified",
               "dimensions": [{"id": "p", "levels": [
                   {"id": "1", "limits": {"trak": 0.1, "gap": "4 milz", "ring": 0.004, "drill_via": "0.2 + foo",
                                          "track_outer": [{"when": {"colour": "red"}, "value": 0.1}]}},
                   {"id": "2", "limits": {"gap": 0.2}}]},
                   {"id": "q", "levels": [{"id": "x", "limits": {"gap_outer": 0.1}}]}]}
        errs = " | ".join(validate(bad)["errors"])
        for needle in ("lower-case slug", "needs a source", "unknown metric 'trak'", "did you mean", "neither a quantity",
                       "below 10 um", "unknown name(s) foo", "unknown context key 'colour'", "limited by two dimensions"):
            self.assertIn(needle, errs)
        warn = validate({"id": "m", "name": "m", "dimensions": [{"id": "p", "levels": [
            {"id": "1", "limits": {"gap": 0.1}}, {"id": "2", "limits": {"gap": 0.2}}]}]}, run_tests=False)["warnings"]
        self.assertTrue(any("coarser at 2" in w for w in warn))
        wrong = {"id": "w", "name": "w", "dimensions": [{"id": "p", "levels": [{"id": "1", "limits": {"gap": 0.1}}]}],
                 "tests": [{"name": "t", "measured": {"gap_outer": 0.05}, "expect": {"required": "1"}}]}
        self.assertIn("test t failed", " ".join(validate(wrong)["errors"]))

    def test_legacy_table_still_works(self):
        from pcbtestkit.classsys import System, evaluate, from_legacy_table, validate
        legacy = {"name": "old", "pth_toolsize_offset": 0.1, "aspect_ratio": 8,
                  "pattern": {"5": {"otw": 0.175, "ott": 0.175, "oar": 0.15}, "6": {"otw": 0.15, "ott": 0.15, "oar": 0.125}},
                  "drill": {"B": {"pth": 0.35, "npth": 0.45, "toolsize": 0.45}, "C": {"pth": 0.25, "npth": 0.35, "toolsize": 0.35}}}
        raw = from_legacy_table("oldfab", legacy)
        self.assertTrue(validate(raw, run_tests=False)["ok"])
        r = evaluate(System(raw), None, {"layers": 2, "thickness_mm": 1.6},
                     synthetic={"track_outer": 0.15, "gap_outer": 0.16, "ring_via_outer": 0.13, "drill_via": 0.25})
        self.assertEqual(r["required"]["class"], "6C")
        d = self.design(tech={"system": "oldfab", "class": "6C", "tables": {"oldfab": legacy}})
        self.assertEqual(d.technology()["system"], "oldfab")

    def test_board_classification_and_violations(self):
        d = self.design(tech={"class": "6C"})
        t = d.technology()
        self.assertIsNone(t["required"]["class"])                 # 0.45/0.30 mm via: 0.025 mm ring on tool size
        self.assertIn("ring_via_outer", [x["metric"] for x in t["required"]["drivers"]])
        self.assertIn("ring_via_outer", t["target_result"]["violations"])
        self.assertTrue(d.cfg["fab"]["source"].startswith("technology class 6C"))
        self.assertAlmostEqual(d.cfg["fab"]["min_via_annular"], 0.175)
        ids = {f.id for f in run_analysis(d, include_kicad=False)}
        self.assertTrue({"MFG-TECH-CLASS", "MFG-COPPER-SHORT"} <= ids)

    def test_other_systems_checks_and_voltage(self):
        d = self.design(tech={"system": "jlcpcb-2026", "class": "standard", "checks": ["ipc-2221-voltage", "ipc-6012-ring:3"]},
                        nets={"VIN_12V": {"nominal": 300}})
        t = d.technology()
        self.assertEqual(t["system"], "jlcpcb-2026")
        self.assertEqual([c["system"] for c in t["checks"]], ["ipc-2221-voltage", "ipc-6012-ring"])
        v = t["checks"][0]["target"]["violations"]["voltage_gap_outer"]
        self.assertEqual(v["items"][0]["limit"], 2.5)               # 300 V, uncoated: 2.5 mm
        self.assertEqual(v["items"][0]["dv"], 300.0)
        self.assertEqual(len(d.class_overview()), len(d.class_systems()))
        ids = {f.id for f in run_analysis(d, include_kicad=False)}
        self.assertTrue({"ELEC-VOLTAGE-SPACING", "MFG-IPC-CLASS", "MFG-TECH-CLASS"} <= ids)

    def test_agent_class_tools(self):
        ws = Workspace(self.pcb, persist=False)
        r = ws.call("list_class_systems")
        self.assertIn("oshpark-2026", [s["id"] for s in r["systems"]])
        r = ws.call("set_analysis_options", {"class_system": "oshpark", "tech_class": "2L"})
        self.assertEqual(r["error"]["code"], "UNKNOWN_CLASS_SYSTEM")
        r = ws.call("set_analysis_options", {"class_system": "oshpark-2026", "tech_class": "2l"})
        self.assertEqual(r["error"]["code"], "CONFIRM_REQUIRED")      # the sample breaks OSH Park rules
        r = ws.call("set_analysis_options", {"class_system": "oshpark-2026", "tech_class": "2l", "confirm": True,
                                             "class_checks": ["ipc-6012-ring:class3"], "tech_context": {"registration_mm": 0.05}})
        self.assertTrue(r["ok"], r)
        cfg = ws.design.user_config["tech"]
        self.assertEqual((cfg["system"], cfg["class"], cfg["checks"]), ("oshpark-2026", "2-layer", ["ipc-6012-ring:3"]))
        self.assertEqual(ws.call("get_overview")["tech"]["ordered"], "2-layer")
        r = ws.call("check_classes", {"systems": ["jlcpcb-2026"], "target": "standard"})
        self.assertIn("violations", r)
        r = ws.call("add_class_system", {"definition": {"id": "x", "name": "x", "dimensions": [{"id": "t", "levels": [
            {"id": "s", "limits": {"trak": 0.1}}]}]}})
        self.assertEqual(r["error"]["code"], "INVALID_DEFINITION")
        from pcbtestkit.classsys import TEMPLATE
        r = ws.call("add_class_system", {"definition": TEMPLATE})
        self.assertTrue(r["ok"], r)
        self.assertIn("myfab-2026", [s["id"] for s in ws.call("list_class_systems")["systems"]])
        self.assertIn("template", ws.call("get_schema", {"name": "class_system"}))

    def test_cli_classes(self):
        r = subprocess.run([sys.executable, "-m", "pcbtestkit", "classes", "validate"], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = subprocess.run([sys.executable, "-m", "pcbtestkit", "classes", "check", self.pcb, "--json"],
                           capture_output=True, text=True, cwd=ROOT)
        rows = json.loads(r.stdout)
        self.assertIn("eurocircuits-2024", [x["system"] for x in rows])
        r = subprocess.run([sys.executable, "-m", "pcbtestkit", "classes", "check", self.pcb, "--system", "eurocircuits-2024",
                            "--target", "6C"], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 1)
        self.assertIn("via annular ring", r.stdout)


class TestAnimation(Base):
    """The video's motion shots: transforms, per-frame board edits, the printed jig, the storyboard."""

    def test_part_flip_matches_pcb_flip(self):
        import math
        import random
        from pcbtestkit import animate as A

        def apply(m, v):
            return [sum(m[i][k] * v[k] for k in range(3)) for i in range(3)]

        rnd = random.Random(7)
        for _ in range(200):
            r = [rnd.choice([0, 90, 180, -90, rnd.uniform(-180, 180)]) for _ in range(3)]
            th, fx, fy, oy, sup = rnd.uniform(-180, 180), rnd.uniform(0, 100), rnd.uniform(0, 80), rnd.uniform(-50, 50), 12.0
            o, p = [rnd.uniform(-3, 3) for _ in range(3)], [rnd.uniform(-5, 5) for _ in range(3)]
            # on the DUT (3D frame, y up), then the PCB turned over onto the jig: (x, y, z) -> (x, -y - 2*oy, sup - z)
            q = apply(A._rz(math.radians(th)), [a + b for a, b in zip(o, apply(A.kicad_rotation(r), p))])
            q = [q[0] + fx, q[1] - fy, q[2]]
            want = [q[0], -q[1] - 2 * oy, sup - q[2]]
            # what part_in_jig asks KiCad for: footprint at (fx, 2*oy - fy), angle -th, rotation F*R, offset F*o + sup
            a = A.kicad_angles(A._mul(A.FLIP_X, A.kicad_rotation(r)))
            o2 = [o[0], -o[1], -o[2] + sup]
            g = apply(A._rz(math.radians(-th)), [u + w for u, w in zip(o2, apply(A.kicad_rotation(a), p))])
            got = [g[0] + fx, g[1] - (2 * oy - fy), g[2]]
            for u, w in zip(got, want):
                self.assertAlmostEqual(u, w, places=6)

    def test_template_edits_and_jig_print(self):
        from pcbtestkit import animate as A
        with open(self.pcb, encoding="utf-8") as fh:
            text = fh.read()
        tpl = A.Template(text)
        self.assertTrue(tpl.refs)
        self.assertEqual(tpl.text(), text)                         # no edits: byte-identical board
        ref = tpl.refs[0]
        out = tpl.text({ref: (12.5, True)})
        self.assertIn("(hide yes)", out)
        self.assertEqual(len(load_board_text(out).footprints), len(load_board(self.pcb).footprints))
        legacy = A._edit_model('(model "x.wrl" (at (xyz 0 0 0)) (scale (xyz 1 1 1)) (rotate (xyz 0 0 0)))', 25.4, False)
        self.assertIn("(at (xyz 0 0 1.00000))", legacy)            # KiCad 5 offsets are in inches
        out_dir = os.path.join(self.tmp, "anim_jig")
        os.makedirs(out_dir, exist_ok=True)
        jig = build_jig(self.design(), os.path.join(out_dir, "j.stl"))
        mesh = A.JigMesh(os.path.join(out_dir, "j.stl"))
        half = mesh.zmax / 2
        wrl = mesh.wrl(half)
        zs = [float(l.split()[2]) * 2.54 for l in wrl.split("point [")[1].split("]")[0].strip().split(",\n")]
        self.assertLessEqual(max(zs), half + 1e-3)
        self.assertGreater(len(jig["parts_to_place"]), 0)

    def test_storyboard_uses_shots_in_production_order(self):
        from pcbtestkit.pipeline import analyze
        from pcbtestkit.spec import build_spec
        from pcbtestkit.video import storyboard
        d = self.design()
        findings, plan = analyze(d, include_kicad=False)
        spec = build_spec(d, findings, plan)
        out_dir = os.path.join(self.tmp, "anim_story")
        os.makedirs(out_dir, exist_ok=True)
        jig = build_jig(d, os.path.join(out_dir, "j.stl"))
        shot = {"images": ["a.png", "b.png"], "fps": 24, "hold": 1.0, "meta": {"parts": ["C1"], "smd": 1}}
        names = ("orbit", "place", "print", "load", "slide", "lift", "pogo", "press")
        scenes = storyboard(d, spec, plan, build_fixture(d, plan), jig, {"images": {}}, out_dir, out_dir,
                            {k: dict(shot) for k in names})
        titles = [s.get("title", "") for s in scenes if s["kind"] == "anim"]
        self.assertEqual(len(titles), len(names))
        self.assertEqual(scenes[0].get("overlay"), "card")
        order = ["Assemble the board", "Print the solder jig", "Load the jig", "Lower the PCB onto the jig",
                 "Lift out the finished board", "Build the fixture", "Test a board"]
        self.assertEqual(titles[1:], order)
        still = storyboard(d, spec, plan, None, None, {"images": {}}, out_dir, out_dir, {})
        self.assertFalse([s for s in still if s["kind"] == "anim"])


class TestTwoJigWorkflow(Base):
    """Placement jig + holding jig: geometry that must fit together, and the animation that shows it."""

    def test_jigs_fit_together(self):
        from pcbtestkit.jig import build_jig, build_jigs, build_placement_jig, outline_segments, stack_collisions, stop_anchors
        d = self.design()
        out = os.path.join(self.tmp, "two_jig")
        os.makedirs(out, exist_ok=True)
        grids = {}
        jig = build_jigs(d, os.path.join(out, "j.stl"), None, None, os.path.join(out, "p.stl"), None, grids=grids)
        self.assertEqual(jig["workflow"], "two_jig")
        pj = jig["placement"]
        self.assertTrue(jig["mesh"]["watertight"])
        self.assertTrue(pj["mesh"]["watertight"])
        self.assertGreaterEqual(len(pj["stops"]), 3)
        self.assertGreater(pj["lead_wells"], 0)
        self.assertEqual(stack_collisions(d, grids), [])
        # the check is real: a holding jig without rim notches collides with the placement jig's stops
        stops = stop_anchors(d, outline_segments(d), 8.0)
        g2 = {"holding": {}, "placement": {}}
        build_jig(d, stops=None, grid_out=g2["holding"])
        build_placement_jig(d, stops, grid_out=g2["placement"])
        self.assertTrue([c for c in stack_collisions(d, g2) if c[2] == "rim"])
        # every lead well reaches the floor under each through-hole of a part the jig holds
        A = grids["placement"]
        floor = int(round(float(d.cfg["jig"]["floor_mm"]) * 100))
        for ref in jig["parts_to_place"]:
            for pd in d.board.by_ref[ref].pads:
                if pd.drill_w > 0:
                    i, j = int((pd.x - A["ax0"]) / A["res"]), int((A["ay1"] - pd.y) / A["res"])
                    self.assertEqual(A["H"][j][i], floor, "%s pad %s" % (ref, pd.number))
        single = build_jigs(self.design(jig={"workflow": "single"}))
        self.assertNotIn("placement", single)

    def test_transforms_and_render_mesh(self):
        import math
        import random
        from pcbtestkit import animate as A
        from pcbtestkit.jig import build_jigs, render_mesh
        rnd = random.Random(3)
        for _ in range(50):
            a = A.Xf(A.kicad_rotation([rnd.uniform(-180, 180) for _ in range(3)]), [rnd.uniform(-50, 50) for _ in range(3)])
            b = A.Xf(A.kicad_rotation([rnd.uniform(-180, 180) for _ in range(3)]), [rnd.uniform(-50, 50) for _ in range(3)])
            p = [rnd.uniform(-10, 10) for _ in range(3)]
            for u, w in zip((a * b).inv().apply(p), (b.inv() * a.inv()).apply(p)):
                self.assertAlmostEqual(u, w, places=6)
            back = A.kicad_rotation(A.kicad_angles(a.R))
            for i in range(3):
                for k in range(3):
                    self.assertAlmostEqual(back[i][k], a.R[i][k], places=6)
        # the stack turned over lands exactly in the soldering pose (the same composition shot_flip uses)
        up = A.Xf.T(10, -20, 12)
        final = A.Xf.T(30, 5, 15) * A.Xf.Rx(math.pi)
        u, f = up.t, final.t
        G = A.Xf.T(*f) * A.Xf.Rx(math.pi) * A.Xf.T(-u[0], -u[1], -u[2])
        for q in ((0, 0, 0), (5, 7, -3)):
            for x, y in zip((G * up).apply(q), final.apply(q)):
                self.assertAlmostEqual(x, y, places=9)
        grids = {}
        build_jigs(self.design(), grids=grids)
        g = grids["placement"]
        verts, faces = render_mesh(g["H"], g["nx"], g["ny"], g["res"])
        self.assertAlmostEqual(max(v[2] for v in verts), max(max(r) for r in g["H"]) / 100.0, places=6)
        self.assertAlmostEqual(max(v[0] for v in verts), g["nx"] * g["res"], places=6)
        self.assertLess(len(faces), 20000)

    def test_render_mesh_faces_point_out(self):
        """Every face of the render mesh is wound outwards (KiCad shades by winding: a wall wound inwards renders
        black once the jig is shown turned over)."""
        import math
        from pcbtestkit.jig import render_mesh
        H = [[300, 300, 300, 300, 300], [300, 50, 50, 300, 300], [300, 50, 50, 120, 300], [300, 300, 300, 300, 300]]
        nx, ny = 5, 4
        verts, faces = render_mesh(H, nx, ny, 1.0)
        for f in faces:
            p = [verts[i] for i in f]
            n = [0.0, 0.0, 0.0]
            for k in range(len(p)):                          # Newell normal: points to the side the face is seen from
                a, b = p[k], p[(k + 1) % len(p)]
                n[0] += (a[1] - b[1]) * (a[2] + b[2])
                n[1] += (a[2] - b[2]) * (a[0] + b[0])
                n[2] += (a[0] - b[0]) * (a[1] + b[1])
            ln = math.sqrt(sum(x * x for x in n))
            c = [sum(q[i] for q in p) / len(p) for i in range(3)]
            o = [c[i] + 0.01 * n[i] / ln for i in range(3)]
            i_, j_ = int(math.floor(o[0])), int(math.floor(o[1]))
            h = H[j_][i_] / 100.0 if 0 <= i_ < nx and 0 <= j_ < ny else 0.0
            self.assertTrue(o[2] < 0 or o[2] > h, "face %r points into the material" % (p,))

    def test_two_jig_step_stills_and_guide(self):
        """The guide shows one render per step of the two-jig workflow (made from the video's scenes)."""
        from pcbtestkit.animate import Stage
        from pcbtestkit.guide import build_guide
        from pcbtestkit.jig import build_jigs
        from pcbtestkit.pipeline import analyze
        from pcbtestkit.spec import build_spec
        from pcbtestkit.visuals import Renderer
        d = self.design()
        out = os.path.join(self.tmp, "steps")
        os.makedirs(out, exist_ok=True)
        paths = {"jig_stl": os.path.join(out, "j.stl"), "jig_place_stl": os.path.join(out, "p.stl")}
        jig = build_jigs(d, paths["jig_stl"], None, None, paths["jig_place_stl"], None)
        findings, plan = analyze(d, include_kicad=False)
        st = Stage(d, plan, None, jig, paths, Renderer(out), out)
        st.dut_wrl = lambda without=(): os.path.join(out, "dut.wrl")     # no kicad-cli needed: scenes only
        shot = st.step_stills()
        names = [os.path.basename(j["png"]) for j in shot.jobs]
        self.assertEqual(names, ["jig2_print.png", "jig2_fit.png", "jig2_cover.png", "jig2_unload.png", "jig2_solder.png"])
        for job in shot.jobs:
            text, _files = job["make"](os.path.join(out, "scene"))
            self.assertIn("JIG_", text)
        # the guide puts each render under its step
        imgs = {}
        for n in ("jig2_print", "jig2_fit", "jig2_cover", "jig2_unload", "jig2_solder"):
            imgs[n] = os.path.join(out, n + ".png")
            with open(imgs[n], "wb") as fh:
                fh.write(b"\x89PNG\r\n\x1a\n" + n.encode())
        html = build_guide(d, build_spec(d, findings, plan), plan, None, jig, {"images": imgs}, paths)
        for cap in ("The two printed jigs", "PCB top side up in the placement jig", "The holding jig comes down",
                    "placement jig lifted off", "Solder from above"):
            self.assertIn(cap, html)
        self.assertNotIn("Parts in their pockets, PCB coming down", html)     # the single-jig pictures

    def test_two_jig_storyboard(self):
        from pcbtestkit.pipeline import analyze
        from pcbtestkit.spec import build_spec
        from pcbtestkit.jig import build_jigs
        from pcbtestkit.video import storyboard
        d = self.design()
        findings, plan = analyze(d, include_kicad=False)
        spec = build_spec(d, findings, plan)
        jig = build_jigs(d)
        shot = {"images": ["a.png", "b.png"], "fps": 24, "hold": 1.0, "meta": {"parts": ["C1"], "smd": 1, "joints": 12}}
        names = ("orbit", "place", "print2", "place2", "cover", "flip", "unload", "solder", "lift2", "pogo", "press")
        scenes = storyboard(d, spec, plan, build_fixture(d, plan), jig, {"images": {}}, self.tmp, self.tmp,
                            {k: dict(shot) for k in names})
        titles = [s.get("title", "") for s in scenes if s["kind"] == "anim"]
        self.assertEqual(titles[2:9], ["Print the two jigs", "Fit the parts in the placement jig", "Put the holding jig on top",
                                       "Turn the stack over", "Lift off the placement jig", "Solder from above",
                                       "Lift out the finished board"])
        # one-sided board: one jig set, captions without a side, and shots of another side change nothing
        from pcbtestkit.animate import later_jig_sets
        self.assertEqual(later_jig_sets(jig), [])
        self.assertEqual([s for s in scenes if "-side" in s.get("title", "") + s.get("caption", "")], [])
        more = dict({k: dict(shot) for k in names}, **{k + "_bottom": dict(shot) for k in names})
        self.assertEqual(storyboard(d, spec, plan, build_fixture(d, plan), jig, {"images": {}}, self.tmp, self.tmp, more), scenes)


def load_board_text(text):
    fd, path = tempfile.mkstemp(suffix=".kicad_pcb")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    try:
        return load_board(path)
    finally:
        os.remove(path)


class TestPackagingAndAgents(Base):
    """Publishing artefacts and the agent-facing surface: CLI discovery, MCP, docs, KiCad package, skill."""

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "pcbtestkit"] + list(args), capture_output=True, text=True, cwd=ROOT)

    def test_cli_discovery_for_agents(self):
        from pcbtestkit import __version__
        from pcbtestkit.agent import TOOLS
        self.assertIn(__version__, self.cli("--version").stdout)
        guide = self.cli("guide").stdout
        self.assertIn("TOKEN RULES", guide)
        self.assertLess(len(guide), 4000)                       # the guide itself must stay cheap
        brief = self.cli("tools", "--brief").stdout.strip().splitlines()
        self.assertEqual(len(brief), len(TOOLS))
        one = json.loads(self.cli("tools", "--name", "set_supply").stdout)
        self.assertEqual(one["name"], "set_supply")
        bad = self.cli("tools", "--name", "set_suply")
        self.assertEqual(bad.returncode, 2)
        self.assertIn("set_supply", bad.stderr)

    def test_mcp_profiles_resources_prompts(self):
        from pcbtestkit.mcp_server import PROMPTS, Server
        srv = Server(self.pcb, profile="core")
        init = srv.dispatch("initialize", {"protocolVersion": "2025-06-18"})
        self.assertIn("completions", init["capabilities"])
        names = [t["name"] for t in srv.dispatch("tools/list", {})["tools"]]
        self.assertEqual(len(names), 16)
        self.assertIn("generate_outputs", names)
        ann = {t["name"]: t["annotations"] for t in srv.dispatch("tools/list", {})["tools"]}
        self.assertTrue(ann["generate_outputs"]["destructiveHint"])
        self.assertTrue(ann["get_overview"]["readOnlyHint"])
        res = srv.dispatch("tools/call", {"name": "get_overview", "arguments": {}})
        self.assertNotIn("structuredContent", res)               # the result is sent once
        self.assertTrue(json.loads(res["content"][0]["text"])["ok"])
        blocked = srv.dispatch("tools/call", {"name": "set_jig_options", "arguments": {}})
        self.assertIn("NOT_IN_PROFILE", blocked["content"][0]["text"])
        uris = [r["uri"] for r in srv.dispatch("resources/list", {})["resources"]]
        self.assertIn("pcbtestkit://project/overview", uris)
        for uri in uris + ["pcbtestkit://class-systems/jlcpcb-2026"]:
            body = srv.dispatch("resources/read", {"uri": uri})["contents"][0]["text"]
            self.assertTrue(body, uri)
        for name, p in PROMPTS.items():
            args = {a["name"]: "x" for a in p["arguments"] if a.get("required")}
            msg = srv.dispatch("prompts/get", {"name": name, "arguments": args})["messages"][0]["content"]["text"]
            self.assertTrue(msg, name)
            self.assertNotRegex(msg, r"\{[a-z_]+\}", name)          # every placeholder was filled in
        comp = srv.dispatch("completion/complete", {"ref": {"type": "ref/prompt", "name": "choose_fab_class"},
                                                    "argument": {"name": "system", "value": "jlc"}})
        self.assertEqual(comp["completion"]["values"], ["jlcpcb-2026"])
        broken = Server("/nonexistent/board.kicad_pcb")         # starts anyway
        self.assertIn("No board open", broken.instructions())

    def test_undo_survives_between_cli_calls(self):
        from pcbtestkit.agent import Workspace
        proj = os.path.join(self.tmp, "undo_proj")
        shutil.copytree(self.proj, proj)
        pcb = os.path.join(proj, "sample_sensor.kicad_pcb")
        ws = Workspace(pcb)
        before = json.dumps(ws.design.user_config, sort_keys=True)
        self.assertTrue(ws.call("set_supply", {"net": "+5V", "voltage": 5, "current_limit_a": 0.4, "rationale": "t"})["ok"])
        ws2 = Workspace(pcb)                                     # a later, separate call
        self.assertEqual(len(ws2.history), 1)
        self.assertTrue(ws2.call("undo_last", {})["ok"])
        ws3 = Workspace(pcb)
        self.assertEqual(json.dumps({k: v for k, v in ws3.design.user_config.items() if k != "schema_version"}, sort_keys=True),
                         json.dumps({k: v for k, v in json.loads(before).items() if k != "schema_version"}, sort_keys=True))
        self.assertEqual(ws3.call("undo_last", {})["error"]["code"], "NOTHING_TO_UNDO")

    def test_generated_docs_are_current(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "gen_docs.py"), "--check"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_kicad_pcm_package(self):
        import zipfile
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import build_pcm
        out = os.path.join(self.tmp, "dist")
        res = build_pcm.build(out)
        z = zipfile.ZipFile(res["zip"])
        names = z.namelist()
        self.assertEqual({n.split("/")[0] for n in names}, {"metadata.json", "plugins", "resources"})
        for need in ("plugins/__init__.py", "plugins/plugin/action_plugin.py", "plugins/pcbtestkit/cli.py",
                     "plugins/pcbtestkit/runner/pcbtest_runner.py", "plugins/pcbtestkit/classsys/systems/jlcpcb-2026.json",
                     "plugins/resources/icon.png", "resources/icon.png"):
            self.assertIn(need, names)
        self.assertFalse([n for n in names if "__pycache__" in n or n.endswith(".pyc")])
        meta = json.loads(z.read("metadata.json"))
        self.assertEqual(build_pcm.validate(meta, os.path.join(ROOT, "pcm", "icon.png")), [])
        from pcbtestkit import __version__
        self.assertEqual(meta["versions"][0]["version"], __version__)
        with open(res["repository_entry"]) as fh:
            entry = json.load(fh)
        self.assertEqual(entry["versions"][0]["download_sha256"], res["sha256"])
        # the archive passes KiCad's layout rules; a stray top-level file or download fields inside do not
        with open(res["zip"], "rb") as fh:
            data = fh.read()
        self.assertEqual(build_pcm.check_archive(data, __version__, meta["identifier"]), [])
        import io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zz:
            bad = json.loads(json.dumps(meta))
            bad["versions"][0]["download_sha256"] = "0" * 64
            zz.writestr("metadata.json", json.dumps(bad))
            zz.writestr("plugins/__init__.py", "")
            zz.writestr("README.txt", "stray")
        errs = build_pcm.check_archive(buf.getvalue(), __version__, meta["identifier"])
        self.assertTrue(any("top level" in e for e in errs) and any("download fields" in e for e in errs), errs)

    def test_owner_script_dry_run_changes_nothing(self):
        before = open(os.path.join(ROOT, "pcm", "metadata.template.json")).read()
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "set_owner.py"), "--owner", "octo-cat", "--dry-run"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("com.github.octo-cat.pcbtestkit", r.stdout)
        self.assertEqual(open(os.path.join(ROOT, "pcm", "metadata.template.json")).read(), before)

    def test_skill_and_plugin_manifests(self):
        from pcbtestkit import __version__
        text = open(os.path.join(ROOT, "skills", "pcbtestkit", "SKILL.md"), encoding="utf-8").read()
        self.assertTrue(text.startswith("---\n"))
        front = text.split("---")[1]
        keys = [l.split(":")[0] for l in front.strip().splitlines() if l and not l.startswith(" ")]
        self.assertEqual(set(keys) - {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}, set())
        desc = [l for l in front.splitlines() if l.startswith("description:")][0]
        self.assertLess(len(desc), 1024)
        for ref in ("tools.md", "cli.md", "config.md", "outputs.md", "class-systems.md", "troubleshooting.md"):
            self.assertTrue(os.path.exists(os.path.join(ROOT, "skills", "pcbtestkit", "references", ref)), ref)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "skills", "pcbtestkit", "scripts", "ptk.py"), "--version"],
                           capture_output=True, text=True)
        self.assertIn(__version__, r.stdout)
        plugin = json.load(open(os.path.join(ROOT, ".claude-plugin", "plugin.json")))
        market = json.load(open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")))
        self.assertEqual(plugin["version"], __version__)
        self.assertEqual(market["plugins"][0]["name"], plugin["name"])


class TestPipelineAndRunner(Base):
    def test_pipeline_and_runner(self):
        out = os.path.join(self.tmp, "out")
        res = run_pipeline(self.design(), out_dir=out, include_kicad=False, outputs={"visuals": False})
        for k in ("spec", "testplan", "fixture", "jig_stl", "report", "runner", "guide"):
            self.assertTrue(os.path.exists(res["paths"][k]), k)
        with open(res["paths"]["spec"]) as fh:
            spec = json.load(fh)
        self.assertEqual(spec["schema"], "pcbtestkit/spec@1")
        # shared outputs must not contain absolute paths (they would leak the author's folders)
        for k in ("spec", "jig_meta", "testplan", "fixture_meta"):
            with open(res["paths"][k], encoding="utf-8") as fh:
                self.assertNotIn(self.tmp, fh.read(), k)
        r = subprocess.run([sys.executable, os.path.join(out, "runner", "pcbtest_runner.py"), "test", "--simulate",
                            "--non-interactive", "--serial", "T1"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = subprocess.run([sys.executable, os.path.join(out, "runner", "pcbtest_runner.py"), "test", "--simulate",
                            "--non-interactive", "--serial", "T2", "--sim-fail", "CH03"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAIL", r.stdout)


class TestWithoutAI(Base):
    """Everything an AI can decide is possible by hand, and every generated file is deterministic."""

    def _run(self, out, **cfg):
        old = os.environ.get("SOURCE_DATE_EPOCH")
        os.environ["SOURCE_DATE_EPOCH"] = "1764000000"
        try:
            return run_pipeline(self.design(**cfg), out_dir=out, include_kicad=False,
                                outputs={"visuals": False, "video": False, "guide": True})
        finally:
            if old is None:
                os.environ.pop("SOURCE_DATE_EPOCH", None)
            else:
                os.environ["SOURCE_DATE_EPOCH"] = old

    def test_outputs_are_identical_run_after_run(self):
        def read_all(d):
            out = {}
            for root, _, files in os.walk(d):
                for f in files:
                    with open(os.path.join(root, f), "rb") as fh:
                        out[os.path.relpath(os.path.join(root, f), d)] = fh.read()
            return out
        a, b = os.path.join(self.tmp, "det_a"), os.path.join(self.tmp, "det_b")
        self._run(a)
        self._run(b)
        fa, fb = read_all(a), read_all(b)
        self.assertEqual(sorted(fa), sorted(fb))
        for k in ("sample_sensor-jig.stl", "sample_sensor-jig-placement.stl", "sample_sensor-fixture.kicad_pcb"):
            self.assertIn(k, fa)
        self.assertEqual([k for k in fa if fa[k] != fb[k]], [])

    def test_fixture_modes(self):
        own = self._run(os.path.join(self.tmp, "own"), fixture={"mode": "own"})
        self.assertNotIn("fixture", own["paths"])
        self.assertIn("runner", own["paths"])
        with open(own["paths"]["probe_points"], encoding="utf-8") as fh:
            rows = fh.read().splitlines()
        self.assertTrue(rows[0].startswith("probe,dut_net,role,probe_side,x_mm,y_mm"))
        self.assertGreater(len(rows), 5)
        with open(own["paths"]["guide"], encoding="utf-8") as fh:
            self.assertIn("Your test fixture", fh.read())
        none = self._run(os.path.join(self.tmp, "none"), fixture={"mode": "none"})
        for k in ("fixture", "probe_points", "runner"):
            self.assertNotIn(k, none["paths"])
        self.assertIn("jig_stl", none["paths"])
        with open(none["paths"]["guide"], encoding="utf-8") as fh:
            self.assertIn("No fixture is made", fh.read())
        ws = Workspace(self.pcb, persist=False)
        self.assertTrue(ws.call("set_fixture_options", {"mode": "own", "rationale": "own fixture"})["ok"])
        self.assertFalse(ws.call("set_fixture_options", {"mode": "printed"})["ok"])

    def test_rail_and_waiver_edits(self):
        from pcbtestkit.manual import apply_rail_edits, apply_waivers, rail_rows, waiver_rows
        d = self.design()
        before = rail_rows(d)
        nets = [r["net"] for r in before]
        self.assertIn("+3V3", nets)
        after = [dict(r) for r in before]
        i3, i5 = nets.index("+3V3"), nets.index("+5V")
        after[i3]["nominal_v"], after[i3]["tolerance_pct"] = "3,25", "3"     # comma decimals are accepted
        after[i5]["test"] = False
        u = {}
        self.assertEqual(apply_rail_edits(u, before, after), [])
        self.assertEqual(u["nets"], {"+3V3": {"nominal": 3.25, "tolerance_pct": 3.0}})   # unchanged rails are not stored
        self.assertEqual(u["testplan"]["exclude_nets"], ["+5V"])
        spec = Design.load(self.pcb, overrides=u).net_spec("+3V3")
        self.assertAlmostEqual(spec["min_v"], 3.1525)
        rng = [dict(r) for r in before]
        rng[i3]["min_v"], rng[i3]["max_v"] = "3.0", "3.6"
        u2 = {}
        apply_rail_edits(u2, before, rng)
        self.assertEqual(u2["nets"]["+3V3"]["min"], 3.0)
        bad = [dict(r) for r in before]
        bad[i3]["min_v"], bad[i3]["max_v"] = "3.6", "3.0"
        self.assertTrue(apply_rail_edits({}, before, bad))
        rows = waiver_rows(run_analysis(d, include_kicad=False))
        self.assertTrue(rows)
        u3 = {"analysis": {"waivers": [{"id": "OTHER-CHECK", "refs": [], "reason": "kept"}]}}
        apply_waivers(u3, rows, [True] + [False] * (len(rows) - 1), "prototype only")
        ws = u3["analysis"]["waivers"]
        self.assertEqual([w["id"] for w in ws], ["OTHER-CHECK", rows[0]["id"]])
        self.assertEqual(ws[1]["reason"], "prototype only")
        waived = [f for f in run_analysis(Design.load(self.pcb, overrides=u3), include_kicad=False) if f.waived]
        self.assertIn(rows[0]["id"], [f.id for f in waived])


if __name__ == "__main__":
    unittest.main()


class TestStackedAndTwoSided(unittest.TestCase):
    """Stacked boards (daughter board on J6 over the MCU area) and parts on both sides (J7 on the bottom)."""

    DISPLAY = {"name": "display", "refs": ["J6"], "outline": [126.5, 100, 148, 121.5], "height_mm": 14}

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pcbtk_stack_")
        cls.proj = os.path.join(cls.tmp, "sample_sensor")
        make_sample.main(cls.proj, variant="stacked")
        cls.pcb = os.path.join(cls.proj, "sample_sensor.kicad_pcb")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def design(self, **over):
        return Design.load(self.pcb, overrides=over or None)

    def test_turned_over_view(self):
        from pcbtestkit.sides import other, view
        d = self.design()
        v = view(d, "bottom")
        self.assertIs(view(d, "top"), d)
        for fp in d.components():
            fv = v.board.by_ref[fp.ref]
            self.assertEqual(fv.side, other(fp.side))
            a, b = fp.courtyard_bbox(), fv.courtyard_bbox()
            for p, q in ((a.x0, b.x0), (a.x1, b.x1), (-a.y1, b.y0), (-a.y0, b.y1)):
                self.assertAlmostEqual(p, q, places=6)
            for pa, pb in zip(fp.pads, fv.pads):
                self.assertEqual((pa.x, -pa.y), (pb.x, pb.y))
                self.assertEqual(pa.on_copper("top"), pb.on_copper("bottom"))
            self.assertEqual(v.height_of(fv), d.height_of(fp))
        bb, bv = d.board.outline_bbox(), v.board.outline_bbox()
        self.assertAlmostEqual(bv.y0, -bb.y1)
        self.assertEqual(len(v.components()), len(d.components()))

    def test_jig_set_per_side(self):
        from pcbtestkit.jig import assembly_passes, build_jigs, jig_of, stack_collisions
        from pcbtestkit.sides import view
        d = self.design()
        passes, notes = assembly_passes(d)
        self.assertEqual([(p["process"], p["side"]) for p in passes],
                         [("smt", "bottom"), ("smt", "top"), ("hand", "top"), ("hand", "bottom")])
        self.assertEqual(passes[3]["parts"], ["J7"])
        out = os.path.join(self.tmp, "jigs")
        os.makedirs(out, exist_ok=True)
        grids = {}
        jig = build_jigs(d, os.path.join(out, "j.stl"), None, None, os.path.join(out, "p.stl"), None, grids=grids,
                         file_for=lambda tag, ext: os.path.join(out, "%s.%s" % (tag, ext)))
        self.assertEqual(jig["side"], "top")
        self.assertNotIn("J7", jig["parts_to_place"])
        self.assertNotIn("J7", jig["placement"]["bottom_pockets"])      # fitted later: not on the board yet
        bot = jig_of(jig, "hand_bottom")
        self.assertEqual(bot["parts_to_place"], ["J7"])
        self.assertEqual(bot["side"], "bottom")
        for j in (jig, jig["placement"], bot, bot["placement"]):
            self.assertTrue(j["mesh"]["watertight"])
        self.assertEqual(stack_collisions(d, grids), [])
        self.assertEqual(stack_collisions(view(d, "bottom"), grids["bottom"]), [])
        # the top-side parts are soldered by now: pockets for their bodies, reliefs for their leads
        self.assertTrue({"J3", "U1", "C1", "J2", "J6"} <= set(bot["placement"]["bottom_pockets"]))
        self.assertEqual({p["ref"] for p in bot["pockets"] if p["type"] == "lead"}, {"J3", "U1", "C1", "C2", "J2", "J6"})
        # J7's hold pocket sits over its body (bottom-side footprints are stored mirrored by KiCad)
        hold = [p for p in bot["pockets"] if p["type"] == "hold"][0]
        y_board = -(hold["center"][1] + grids["bottom"]["holding"]["oy"])
        self.assertAlmostEqual(y_board, (113 + 110.46) / 2, delta=0.4)
        # every lead well of the bottom pass reaches the floor under J7's holes
        A = grids["bottom"]["placement"]
        floor = int(round(float(d.cfg["jig"]["floor_mm"]) * 100))
        for pd in d.board.by_ref["J7"].pads:
            i, j = int((pd.x - A["ax0"]) / A["res"]), int((A["ay1"] + pd.y) / A["res"])
            self.assertEqual(A["H"][j][i], floor)
        self.assertTrue(os.path.exists(os.path.join(out, "jig-bottom.stl")))
        self.assertTrue(os.path.exists(os.path.join(out, "jig-bottom-placement.stl")))
        # paste-print nest for the second SMD side: flat frame flush with the PCB surface
        nest = jig_of(jig, "nest_top")
        self.assertEqual(nest["kind"], "paste_nest")
        self.assertEqual(nest["down_side_pockets"], ["C9"])
        g = grids["nest_top"]
        self.assertAlmostEqual(max(max(r) for r in g["H"]) / 100.0, g["rest"] + d.board.thickness, places=2)
        only_top = build_jigs(self.design(jig={"sides": ["top"], "paste_nest": "none"}))
        self.assertEqual(only_top["extra"], {})
        self.assertTrue(any("get no jig" in n for n in only_top["notes"]))
        self.assertEqual(build_jigs(self.design(jig={"sides": ["bottom", "top"]}))["side"], "bottom")

    def test_video_shows_both_jig_sets(self):
        """Hand-soldered parts on both sides: the video runs the jig shots again for the bottom set, with the board
        turned over, and the captions name the side."""
        from pcbtestkit import animate as A
        from pcbtestkit.jig import build_jigs, jig_of
        from pcbtestkit.pipeline import analyze
        from pcbtestkit.spec import build_spec
        from pcbtestkit.video import storyboard
        from pcbtestkit.visuals import Renderer
        d = self.design()
        out = os.path.join(self.tmp, "video2")
        os.makedirs(out, exist_ok=True)
        paths = {"jig_stl": os.path.join(out, "j.stl"), "jig_place_stl": os.path.join(out, "p.stl")}
        jig = build_jigs(d, paths["jig_stl"], None, None, paths["jig_place_stl"], None,
                         file_for=lambda tag, ext: os.path.join(out, "b-%s.%s" % (tag, ext)))
        bot = jig_of(jig, "hand_bottom")
        paths.update(jig_hand_bottom_stl=bot["stl"], jig_hand_bottom_place_stl=bot["placement"]["stl"])
        self.assertEqual([(p["jig"], j["side"]) for p, j in A.later_jig_sets(jig)], [("hand_bottom", "bottom")])
        self.assertEqual(A.later_parts(jig), ["J7"])
        findings, plan = analyze(d, include_kicad=False)
        rd = Renderer(out)
        top = A.Stage(d, plan, None, jig, paths, rd, out)
        ep = dict(paths, jig_stl=bot["stl"], jig_place_stl=bot["placement"]["stl"])
        st = A.Stage(d, plan, None, bot, ep, rd, out, tag="_bottom", later=A.later_parts(jig, "hand_bottom"))
        seen = []
        for s_ in (top, st):
            s_.dut_wrl = lambda without=(), seen=seen: seen.append(tuple(sorted(without))) or os.path.join(out, "dut.wrl")
        self.assertEqual((top.side, top.later, st.side, st.jig_parts, st.later), ("top", ["J7"], "bottom", ["J7"], []))
        # J7 is fitted in the bottom pass: the top pass shows the board without it
        top.bare_wrl()
        top.done_wrl()
        self.assertEqual(seen, [tuple(sorted(top.jig_parts + ["J7"])), ("J7",)])
        # the exported DUT turns over with the bottom view: a real point lands where the view has it
        bb, vbb, t = st.bb, st.vbb, st.t
        for x, y, z, zd in ((120.0, 105.0, t / 2, -t), (131.3, 117.2, -t / 2, 0.0)):
            got = st.dut_xf().apply((x - bb.cx, -(y - bb.cy), z))
            for u, w in zip(got, (x - vbb.cx, -(-y - vbb.cy), zd)):
                self.assertAlmostEqual(u, w, places=9)
        self.assertEqual(top.dut_xf().apply((1.0, 2.0, 0.0)), (1.0, 2.0, -t / 2))
        # J7 in the soldering pose sits in its pocket of the bottom holding jig (world = holding jig frame)
        fp = st.vb.by_ref["J7"]
        hold = [p for p in bot["pockets"] if p["type"] == "hold" and p["ref"] == "J7"][0]
        pin1 = (st.two()["final"] * A.Xf.T(fp.x - vbb.cx, -(fp.y - vbb.cy), 0)).apply((0, 0, 0))
        self.assertLess(abs(pin1[0] - hold["center"][0]) + abs(pin1[1] - hold["center"][1]), 2.0)
        for name in A.JIG_SHOTS_TWO_JIG:
            sh = getattr(st, "shot_" + name)()
            self.assertTrue(sh.jobs, name)
            self.assertTrue(os.path.basename(sh.jobs[0]["png"]).startswith(name + "_bottom_"))
            text, _ = sh.jobs[-1]["make"](os.path.join(out, "scene"))
            self.assertIn("JIG_", text)
        self.assertNotEqual(top.two()["wb"], st.two()["wb"])            # each set has its own meshes
        # storyboard: the bottom set's sequence follows the top set's, captions name the side
        spec = build_spec(d, findings, plan)
        shot = {"images": ["a.png", "b.png"], "fps": 24, "hold": 1.0, "meta": {"joints": 4}}
        shots = dict({k: dict(shot) for k in A.SHOTS_TWO_JIG},
                     **{k + "_bottom": dict(shot, meta={"parts": ["J7"], "joints": 2}) for k in A.JIG_SHOTS_TWO_JIG})
        scenes = storyboard(d, spec, plan, None, jig, {"images": {}}, out, out, shots)
        titles = [s_.get("title", "") for s_ in scenes if s_["kind"] == "anim"]
        seq = ["Put the holding jig on top", "Turn the stack over", "Lift off the placement jig", "Solder from above"]
        self.assertEqual(titles[2:], ["Print the top-side jigs", "Fit the top-side parts in the placement jig"] + seq +
                         ["Lift out the board", "Print the bottom-side jigs", "Fit the bottom-side parts in the placement jig"] +
                         seq + ["Lift out the finished board", "Build the fixture", "Test a board"])
        caps = [s_.get("caption", "") for s_ in scenes]
        self.assertIn("PCB bottom side up, then J7 into its holes.", caps)
        self.assertIn("Hold both jigs together and turn them over as one: the top side now faces up.", caps)
        self.assertIn("All 7 hand-soldered parts sit flush and square.", caps)
        self.assertIn("b-jig-bottom.stl - bottom-side solder jig", scenes[-1]["bullets"])     # the file build_jigs wrote
        # without animation the bottom pass still gets a scene with its steps
        still = [s_ for s_ in storyboard(d, spec, plan, None, jig, {"images": {}}, out, out, {})
                 if s_.get("title", "").startswith("Then the bottom-side parts")]
        self.assertEqual(len(still), 1)
        self.assertIn("PCB bottom side up in the placement jig, fit J7", still[0]["bullets"])

    def test_stacked_board_blocks_probes(self):
        import math
        st = dict(self.DISPLAY, outline=[125, 99.5, 148, 121.5])
        d = self.design(stack=[st], testplan={"probe_side": "top"})
        plan = build_testplan(d)
        x0, y0, x1, y1 = st["outline"]
        self.assertTrue(plan["probes"])
        for p in plan["probes"]:
            self.assertFalse(x0 - 1 <= p["x"] <= x1 + 1 and y0 - 1 <= p["y"] <= y1 + 1, p)
        win = [k for k in plan["fixture_keepouts"] if k.get("kind") == "stack"]
        self.assertEqual([k["ref"] for k in win], ["display"])
        self.assertIn("only reachable under stacked board display (top side)",
                      [u["reason"] for u in plan["coverage"]["untestable"]])
        from pcbtestkit.testplan import testability_findings
        ids = {f.id for f in testability_findings(d, plan)}
        self.assertIn("TEST-STACK-ACCESS", ids)
        fx = build_fixture(d, plan)
        self.assertIn("CUT display", fx["kicad_pcb"])
        # fitted after the test: the board is tested bare, the guide says when to fit it
        d2 = self.design(stack=[dict(st, fitted_at_test=False)], testplan={"probe_side": "top"})
        p2 = build_testplan(d2)
        self.assertEqual(p2["after_test"], [{"fit": "display", "refs": ["J6"], "side": "top"}])
        self.assertFalse([k for k in p2["fixture_keepouts"] if k.get("kind") == "stack"])
        self.assertGreaterEqual(len(p2["probes"]), len(plan["probes"]))
        # without outline/height they are estimated from J6 and flagged
        d3 = self.design(stack=[{"name": "display", "refs": ["J6"]}])
        p3 = build_testplan(d3)
        self.assertTrue(p3["stack"][0]["outline_source"].startswith("estimate"))
        self.assertIn("TEST-STACK-ESTIMATE", {f.id for f in testability_findings(d3, p3)})
        self.assertTrue(math.isclose(p3["stack"][0]["height_mm"], d3.height_of(d3.board.by_ref["J6"])[0] + 4.6, abs_tol=0.01))

    def test_two_plate_fixture(self):
        d = self.design(stack=[self.DISPLAY], testplan={"probe_side": "both"})
        plan = build_testplan(d)
        self.assertEqual(plan["plates"], ["bottom", "top"])
        sides = {p["side"] for p in plan["probes"]}
        self.assertEqual(sides, {"bottom", "top"})
        for s_ in plan["plates"]:
            chans = [h["signal"] for h in plan["headers"][s_] if h["signal"].startswith("CH")]
            self.assertEqual(sorted(chans), sorted(p["channel"] for p in plan["probes"] if p["side"] == s_ and p.get("channel")))
        fx = build_fixture(d, plan)
        self.assertEqual({k: v["pogo_count"] for k, v in fx["plates"].items()},
                         {s_: sum(1 for p in plan["probes"] if p["side"] == s_) for s_ in plan["plates"]})
        self.assertIn("CUT display", fx["plates"]["top"]["kicad_pcb"])
        out = os.path.join(self.tmp, "both")
        old = os.environ.get("SOURCE_DATE_EPOCH")
        os.environ["SOURCE_DATE_EPOCH"] = "1764000000"
        try:
            runs = []
            for k in ("a", "b"):
                runs.append(run_pipeline(self.design(stack=[self.DISPLAY], testplan={"probe_side": "both"}),
                                         out_dir=os.path.join(out, k), include_kicad=False,
                                         outputs={"visuals": False, "video": False}))
        finally:
            if old is None:
                os.environ.pop("SOURCE_DATE_EPOCH", None)
            else:
                os.environ["SOURCE_DATE_EPOCH"] = old
        names = sorted(os.listdir(os.path.join(out, "a")))
        for f in ("sample_sensor-fixture-top.kicad_pcb", "sample_sensor-fixture-bottom.kicad_pcb",
                  "sample_sensor-fixture-top-probes.csv", "sample_sensor-jig-bottom.stl",
                  "sample_sensor-jig-bottom-placement.stl", "sample_sensor-paste-nest-top.stl"):
            self.assertIn(f, names)
        for f in names:
            pa, pb = os.path.join(out, "a", f), os.path.join(out, "b", f)
            if os.path.isfile(pa):
                with open(pa, "rb") as fa, open(pb, "rb") as fb:
                    self.assertEqual(fa.read(), fb.read(), f)
        with open(runs[0]["paths"]["guide"], encoding="utf-8") as fh:
            g = fh.read()
        for txt in ("Two plates (clamshell)", "Stacked boards", "Bottom-side parts (pass 4)", "Paste-print nest for the top side"):
            self.assertIn(txt, g)
        with open(runs[0]["paths"]["fixture_meta"], encoding="utf-8") as fh:
            self.assertEqual(sorted(json.load(fh)["plates"]), ["bottom", "top"])

    def test_decisions_tools_and_dialog_rows(self):
        from pcbtestkit.manual import apply_stack_edits, stack_rows
        ws = Workspace(self.pcb, persist=False)
        dec = {x["topic"]: x for x in ws.call("get_open_decisions")["decisions"]}
        self.assertIn("J6", [c["ref"] for c in dec["stacked_boards"]["items"]])
        self.assertIn("J7", [i["ref"] for i in dec["part_heights"]["items"]])
        self.assertEqual(ws.call("set_stacked_boards", {"boards": [{"name": "x", "refs": ["J99"]}]})["error"]["code"], "UNKNOWN_REF")
        self.assertEqual(ws.call("set_stacked_boards", {"boards": [{"name": "x", "refs": ["J6"], "outline": [1, 2, 3]}]})["error"]["code"],
                         "BAD_VALUE")
        r = ws.call("set_stacked_boards", {"boards": [dict(self.DISPLAY)], "rationale": "display board on J6"})
        self.assertTrue(r["ok"], r)
        topics = {x["topic"] for x in ws.call("get_open_decisions")["decisions"]}
        self.assertNotIn("stacked_boards", topics)
        self.assertEqual(ws.call("get_testplan")["stacked_boards"][0]["name"], "display")
        self.assertTrue(ws.call("set_test_options", {"probe_side": "both"})["ok"])
        self.assertEqual(ws.call("get_testplan")["plates"], ["bottom", "top"])
        self.assertFalse(ws.call("set_jig_options", {"sides": ["top", "top"]})["ok"])
        self.assertTrue(ws.call("set_jig_options", {"sides": ["bottom", "top"], "paste_nest": "none"})["ok"])
        # the dialog's Stacked boards tab, without wx
        d = self.design()
        rows = stack_rows(d)
        self.assertEqual([(r["refs"], r["use"]) for r in rows], [("J6", False)])
        rows[0].update(use=True, name="display", outline="126.5, 100, 148, 121.5", height_mm="14")
        u = {}
        self.assertEqual(apply_stack_edits(u, rows, set(d.board.by_ref)), [])
        self.assertEqual(u["stack"], [{"name": "display", "refs": ["J6"], "side": "top", "outline": [126.5, 100.0, 148.0, 121.5],
                                       "height_mm": 14.0, "fitted_at_test": True}])
        self.assertEqual(stack_rows(self.design(**u))[0]["use"], True)
        bad = [dict(rows[0], outline="1, 2")]
        u2 = {"stack": ["keep"]}
        self.assertTrue(apply_stack_edits(u2, bad, set(d.board.by_ref)))
        self.assertEqual(u2["stack"], ["keep"])                # nothing written when there are problems


class TestFixtureRouting(Base):
    """The fixture board comes out routed: every connection made, clearances kept, same file every run."""

    def _board(self, fx, name="routed"):
        path = os.path.join(self.tmp, name + ".kicad_pcb")
        with open(path, "w") as fh:
            fh.write(fx["kicad_pcb"])
        return load_board(path, pro_path="")

    def _check(self, b, clearance=0.2, edge=0.5):
        """Independent of the router: union-find connectivity per net and pairwise copper clearance."""
        from pcbtestkit.geometry import pt_seg_dist, seg_seg_dist
        from pcbtestkit.router import pad_shape, point_dist, seg_dist
        pads = [p for p in b.all_pads() if p.net and p.has_copper]
        segs = [(t.layer, t.x1, t.y1, t.x2, t.y2, t.width, t.net) for t in b.tracks]
        vias = [(v.x, v.y, v.size, v.net) for v in b.vias]
        self.assertTrue(segs)
        lay = {"F.Cu": "top", "B.Cu": "bottom"}
        # ---- connectivity ----
        items = [("pad", p) for p in pads] + [("seg", s) for s in segs] + [("via", v) for v in vias]
        parent = list(range(len(items)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def touch(a, b_):
            order = {"seg": 0, "via": 1, "pad": 2}
            if order[a[0]] > order[b_[0]]:
                a, b_ = b_, a
            (ka, A), (kb, B) = a, b_
            if ka == "seg":
                ends = [(A[1], A[2]), (A[3], A[4])]
                if kb == "pad":
                    return B.on_copper(lay[A[0]]) and any(point_dist(pad_shape(B), x, y) <= 1e-6 for x, y in ends)
                if kb == "via":
                    return any(abs(x - B[0]) < 1e-6 and abs(y - B[1]) < 1e-6 for x, y in ends)
                return B[0] == A[0] and (any(pt_seg_dist(x, y, B[1], B[2], B[3], B[4]) < 1e-6 for x, y in ends) or
                                         any(pt_seg_dist(x, y, A[1], A[2], A[3], A[4]) < 1e-6
                                             for x, y in [(B[1], B[2]), (B[3], B[4])]))
            if ka == "via" and kb == "pad":
                return point_dist(pad_shape(B), A[0], A[1]) <= 1e-6
            return False

        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                ni = items[i][1].net if items[i][0] == "pad" else items[i][1][-1]
                nj = items[j][1].net if items[j][0] == "pad" else items[j][1][-1]
                if ni == nj and touch(items[i], items[j]):
                    parent[find(i)] = find(j)
        groups = {}
        for i, (k, it) in enumerate(items):
            if k == "pad":
                groups.setdefault(it.net, set()).add(find(i))
        split = {n: len(g) for n, g in groups.items() if len(g) > 1}
        self.assertEqual(split, {}, "nets not fully connected")
        # ---- clearance (copper of different nets on the same layer, and to the board edge) ----
        for s in segs:
            half = s[5] / 2
            for p in pads:
                if p.net != s[6] and p.on_copper(lay[s[0]]):
                    self.assertGreaterEqual(seg_dist(pad_shape(p), s[1], s[2], s[3], s[4]) - half, clearance - 1e-6)
            for t in segs:
                if t[6] != s[6] and t[0] == s[0]:
                    self.assertGreaterEqual(seg_seg_dist(s[1:5], t[1:5]) - half - t[5] / 2, clearance - 1e-6)
            for v in vias:
                if v[3] != s[6]:
                    self.assertGreaterEqual(pt_seg_dist(v[0], v[1], *s[1:5]) - half - v[2] / 2, clearance - 1e-6)
            for e in b.edge_segs:
                self.assertGreaterEqual(seg_seg_dist(s[1:5], e) - half, edge - 1e-6)

    def test_sample_fixture_is_routed_and_clean(self):
        d = self.design()
        plan = build_testplan(d, [])
        fx = build_fixture(d, plan)
        st = fx["routing"]
        self.assertEqual(st["unrouted_connections"], 0, st)
        self.assertGreater(st["connections"], 10)
        self.assertFalse([w for w in fx["warnings"] if "router" in w])
        b = self._board(fx)
        self._check(b)
        self.assertTrue(any(t.width >= 0.5 for t in b.tracks if t.net == "GND"))      # power width where it fits
        # each divider pair sits above its own header pin, so the channel nets run straight down
        hdr = next(f for f in b.footprints if f.lib_id.startswith("Connector_PinHeader"))
        for p in hdr.pads:
            if p.net.startswith("CH"):
                rs = [q for q in b.all_pads() if q.net == p.net and q.ref.startswith("R")]
                for q in rs:
                    self.assertLess(abs(q.x - p.x), 1.3 + 1e-6)
                    self.assertLess(q.y, p.y)

    def test_routing_is_deterministic_and_optional(self):
        d = self.design()
        plan = build_testplan(d, [])
        self.assertEqual(build_fixture(d, plan)["kicad_pcb"], build_fixture(d, plan)["kicad_pcb"])
        off = self.design(fixture={"route": False})
        fx = build_fixture(off, build_testplan(off, []))
        self.assertIsNone(fx["routing"])
        self.assertNotIn("(segment", fx["kicad_pcb"])

    def test_panel_and_two_plates_are_routed(self):
        d = self.design(fixture={"panel": {"cols": 2, "rows": 2}})
        fx = build_fixture(d, build_testplan(d, []))
        self.assertEqual(fx["routing"]["unrouted_connections"], 0)
        self._check(self._board(fx, "panel"))
        d2 = self.design(testplan={"probe_side": "both"})
        fx2 = build_fixture(d2, build_testplan(d2, []))
        self.assertEqual(set(fx2["routing"]["plates"]), set(fx2["plates"]))
        self.assertEqual(fx2["routing"]["unrouted_connections"], 0)
        for side, f in fx2["plates"].items():
            self._check(self._board(f, "plate_" + side))

    def test_unroutable_connections_are_reported_not_forced(self):
        # a clearance so large that most connections cannot be made: the router reports them and never writes a
        # track that breaks the rule
        d = self.design(fixture={"clearance_mm": 2.0, "power_track_mm": 0.25})
        fx = build_fixture(d, build_testplan(d, []))
        st = fx["routing"]
        self.assertGreater(st["unrouted_connections"], 0)
        self.assertTrue(any("router left" in w for w in fx["warnings"]))
        b = self._board(fx, "tight")
        if b.tracks:
            self._check_clearance_only(b, 2.0)

    def _check_clearance_only(self, b, clearance):
        from pcbtestkit.geometry import seg_seg_dist
        from pcbtestkit.router import pad_shape, seg_dist
        lay = {"F.Cu": "top", "B.Cu": "bottom"}
        for t in b.tracks:
            for p in b.all_pads():
                if p.net and p.net != t.net and p.has_copper and p.on_copper(lay[t.layer]):
                    self.assertGreaterEqual(seg_dist(pad_shape(p), t.x1, t.y1, t.x2, t.y2) - t.width / 2, clearance - 1e-6)
            for u in b.tracks:
                if u.net != t.net and u.layer == t.layer:
                    self.assertGreaterEqual(seg_seg_dist((t.x1, t.y1, t.x2, t.y2), (u.x1, u.y1, u.x2, u.y2)) - (t.width + u.width) / 2,
                                            clearance - 1e-6)

    def test_cli_no_route(self):
        out = os.path.join(self.tmp, "noroute")
        r = subprocess.run([sys.executable, "-m", "pcbtestkit", "run", self.pcb, "--out", out, "-q", "--no-visuals",
                            "--no-jig", "--no-route"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        with open(os.path.join(out, "sample_sensor-fixture.kicad_pcb")) as fh:
            self.assertNotIn("(segment", fh.read())


def add_swd_header(pcb, ref="J9", x=106.0, y=100.0):
    """Give the sample board an SWD programming header: a Tag-Connect TC2030 footprint on the bottom side (1.27 mm
    pads), wired to new SWDIO/SWCLK nets on the MCU, its reset and +3V3."""
    import re
    from pcbtestkit import kicad_writer as W
    from pcbtestkit.visuals import append_items
    with open(pcb) as fh:
        text = fh.read()
    ids = {name: int(i) for i, name in re.findall(r'^\t\(net (\d+) "([^"]*)"\)', text, re.M)}
    nmax = max(ids.values())
    new = []
    for name in ("/SWDIO", "/SWCLK", "/SWO"):
        nmax += 1
        ids[name] = nmax
        new.append('\t(net %d "%s")\n' % (nmax, name))
    last = list(re.finditer(r'^\t\(net \d+ "[^"]*"\)\n', text, re.M))[-1]
    text = text[:last.end()] + "".join(new) + text[last.end():]
    for pad, net in (("25", "/SWDIO"), ("26", "/SWCLK")):
        text = re.sub(r'\(net \d+ "unconnected-\(U3-P%s-Pad%s\)"\)' % (pad, pad), '(net %d "%s")' % (ids[net], net), text)
    pins = {1: "+3V3", 2: "/SWDIO", 3: "/NRST", 4: "/SWCLK", 5: "GND", 6: "/SWO"}
    items = [W.fp_rect(-2.6, -1.6, 2.6, 1.6, "B.CrtYd", 0.05)]
    for n, net in pins.items():
        col, row = (n - 1) // 2, (n - 1) % 2
        items.append(W.pad(str(n), "smd", "circle", -1.27 + 1.27 * col, 0.635 if row == 0 else -0.635, 0.787, 0.787,
                           ["B.Cu", "B.Mask"], net=(ids[net], net), pintype="passive"))
    for hx, hy in ((-2.54, 0.0), (2.54, 1.016), (2.54, -1.016)):
        items.append(W.pad("", "np_thru_hole", "circle", hx, hy, 0.991, 0.991, ["*.Cu", "*.Mask"], drill=0.991))
    fp = W.footprint("Connector:Tag-Connect_TC2030-IDC-NL_2x03_P1.27mm_Vertical", x, y, ref, "TC2030 SWD", items,
                     layer="B.Cu", attr=("smd", "board_only", "exclude_from_pos_files", "exclude_from_bom"))
    with open(pcb, "w") as fh:
        fh.write(append_items(text, [fp]))


class TestBatchAndProgramming(unittest.TestCase):
    """Batches (one fixture site and one jig position per board) and programming the board on the fixture."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pcbtk_prog_")
        cls.proj = os.path.join(cls.tmp, "sample_sensor")
        make_sample.main(cls.proj)
        cls.pcb = os.path.join(cls.proj, "sample_sensor.kicad_pcb")
        add_swd_header(cls.pcb)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def design(self, **over):
        over.setdefault("testplan", {"probe_side": "bottom"})
        return Design.load(self.pcb, overrides=over)

    def test_detect_and_probe_programming_header(self):
        import math
        from pcbtestkit.program import detect
        d = self.design()
        found = detect(d)
        self.assertEqual([(h["ref"], h["protocol"]) for h in found], [("J9", "swd")])
        plan = build_testplan(d, [])
        hd = plan["program"]["headers"][0]
        self.assertEqual(hd["missing"], [])
        by_id = {p["id"]: p for p in plan["probes"]}
        for pin in hd["pins"]:                   # every pin reached; new probes sit on the header's own pads
            self.assertIn(pin["probe"], by_id, pin)
        prog = [p for p in plan["probes"] if p["role"] == "program"]
        self.assertEqual(sorted(p["net"] for p in prog), ["/NRST", "/SWCLK", "/SWDIO", "/SWO"])
        self.assertTrue(all(p["pogo"]["family"] == "P50" and p["channel"] is None for p in prog))
        for a in plan["probes"]:                 # spacing: the finer family only between fine-pitch probes
            for b in plan["probes"]:
                if a is b or a["side"] != b["side"]:
                    continue
                need = max((a.get("pogo") or plan["pogo"])["min_spacing_mm"], (b.get("pogo") or plan["pogo"])["min_spacing_mm"])
                self.assertGreaterEqual(math.hypot(a["x"] - b["x"], a["y"] - b["y"]), need - 1e-6)
        self.assertNotIn("/SWDIO", [u["net"] for u in plan["coverage"]["untestable"]])
        self.assertIn("SP", [s["id"] for s in plan["sequence"]])
        # the plain sample board has no programming header (its I2C header and USB socket are not one)
        plain = os.path.join(self.tmp, "plain")
        make_sample.main(plain)
        self.assertEqual(detect(Design.load(os.path.join(plain, "sample_sensor.kicad_pcb"))), [])

    def test_batch_has_one_central_connector(self):
        """A batch fixture gets ONE connector in the middle with every board's supply, channels and programming
        lines; the boards sit on both sides of it with their dividers facing it (even trace lengths)."""
        from pcbtestkit.fixture import probes_csv
        d = self.design(batch={"count": 2})
        plan = build_testplan(d, [])
        fx = build_fixture(d, plan)
        self.assertEqual(fx["routing"]["unrouted_connections"], 0)
        ch = fx["central_header"]
        self.assertEqual({st["header"] for st in fx["sites"]}, {ch["ref"]})
        self.assertEqual([st["power_terminal"] for st in fx["sites"]], [None, None])
        self.assertEqual([st["program"] for st in fx["sites"]], [[], []])
        sig = {(q["site"], q["signal"]): q for q in ch["pins"]}
        for k in (1, 2):
            self.assertEqual(sig[(k, "VIN")]["net"], "S%d_VIN" % k)                  # a supply per board
            self.assertEqual(sig[(k, "SWDIO")]["net"], "S%d_TP_SWDIO" % k)
            self.assertIn((k, "CH01"), sig)
        self.assertEqual([q["signal"] for q in ch["pins"] if q["pin"] % 10 == 0 or q["pin"] == 1], ["GND"] * len(
            [q for q in ch["pins"] if q["pin"] % 10 == 0 or q["pin"] == 1]))
        for cm in fx["channel_map"]:                                                     # runner and CSV point at it
            self.assertEqual(cm["pin"], sig[(cm["site"], cm["channel"])]["pin"])
        rows = [r.split(",") for r in probes_csv(plan, fx).splitlines()[1:]]
        ch01 = {r[0]: r[9] for r in rows if r[8] == "CH01"}
        self.assertNotEqual(ch01["1"], ch01["2"])
        b = load_board_text(fx["kicad_pcb"])
        self.assertEqual([f.ref for f in b.footprints if f.ref.startswith("PRG")], [])
        hdr = b.by_ref[ch["ref"]]
        self.assertEqual(len(hdr.pads), len(ch["pins"]))
        oy = (fx["outline"]["y_min"] + fx["outline"]["y_max"]) / 2.0                     # in the middle of the fixture
        self.assertLess(abs(hdr.y - 1.27 - oy), 0.2 * fx["outline"]["height"])

    def test_fixture_programming_connectors_for_a_batch(self):
        d = self.design(batch={"count": 2}, fixture={"batch_connector": "per_site"})
        plan = build_testplan(d, [])
        fx = build_fixture(d, plan)
        self.assertNotIn("central_header", fx)
        self.assertEqual(len(fx["sites"]), 2)
        self.assertEqual(fx["routing"]["unrouted_connections"], 0)
        for st in fx["sites"]:
            pc = st["program"][0]
            self.assertEqual(pc["connector"], "same")
            nets = {q["pin"]: q["net"] for q in pc["pins"]}
            pre = "S%d_" % st["site"]
            self.assertEqual(nets["2"], pre + "TP_SWDIO")
            self.assertEqual(nets["5"], "GND")
            self.assertEqual({q["pin"]: q["dut_pad"] for q in pc["pins"]}["4"], "4")
        b = load_board_text(fx["kicad_pcb"])
        prg = [f for f in b.footprints if f.ref.startswith("PRG")]
        self.assertEqual(len(prg), 2)
        # a copy of the header as designed: the bottom-side footprint is un-mirrored, so its cable fits
        dut = d.board.by_ref["J9"]
        want = sorted((p.number, round(p.local_x, 3), round(-p.local_y, 3)) for p in dut.pads if p.number)
        self.assertEqual(sorted((p.number, round(p.local_x, 3), round(p.local_y, 3)) for p in prg[0].pads if p.number), want)
        self.assertTrue(all("F.Cu" in p.layers for p in prg[0].pads if p.number))
        # a standard connector that cannot carry SWD falls back to a copy of the header, with a warning
        plan2 = build_testplan(self.design(program={"connector": "avr_isp6"}), [])
        self.assertEqual(plan2["program"]["headers"][0]["connector"], "same")
        self.assertTrue(any("avr_isp6" in w for w in plan2["warnings"]))
        fx3 = build_fixture(self.design(program={"connector": "swd10"}), build_testplan(self.design(program={"connector": "swd10"}), []))
        pins = {q["pin"]: q["signal"] for q in fx3["sites"][0]["program"][0]["pins"]}
        self.assertEqual((pins["1"], pins["2"], pins["4"], pins["10"]), ("VREF", "SWDIO", "SWCLK", "NRST"))

    def test_batch_layout_and_bed(self):
        from pcbtestkit.batch import auto_cols, fit_on_bed, layout
        self.assertEqual(auto_cols(4, 60, 45, 10), 2)
        self.assertEqual(auto_cols(3, 100, 20, 0), 1)
        lay = layout({"batch": {"count": 5}}, 60, 45)
        self.assertEqual((lay["count"], lay["cols"], lay["rows"]), (5, 2, 3))
        self.assertEqual(layout({"fixture": {"panel": {"cols": 2, "rows": 1}}}, 60, 45)["count"], 2)
        self.assertEqual(fit_on_bed(6, 0, [(70, 55)], (220, 220)), (6, 2, 3))       # 140 x 165 mm: squarer than 3 x 2
        self.assertEqual(fit_on_bed(6, 0, [(70, 55)], (150, 120))[0], 4)

    def test_batch_jigs(self):
        from pcbtestkit.jig import build_jigs, stack_collisions
        out = os.path.join(self.tmp, "jigs")
        os.makedirs(out, exist_ok=True)
        grids = {}
        d = self.design(batch={"count": 4})
        j = build_jigs(d, os.path.join(out, "j.stl"), os.path.join(out, "j.scad"), None, os.path.join(out, "p.stl"), None,
                       grids=grids, file_for=lambda tag, ext: os.path.join(out, "%s.%s" % (tag, ext)))
        self.assertEqual(j["batch"]["boards_per_jig"], 4)
        self.assertEqual(len(grids["holding"]["boards"]), 4)
        self.assertTrue(j["mesh"]["watertight"] and j["placement"]["mesh"]["watertight"])
        self.assertEqual(stack_collisions(d, grids), [])               # the jigs close over every board
        with open(os.path.join(out, "j.scad")) as fh:
            self.assertIn("module one_board()", fh.read())
        small = build_jigs(self.design(batch={"count": 4, "bed_mm": [150, 150]}))
        self.assertLess(small["batch"]["boards_per_jig"], 4)
        self.assertGreater(small["batch"]["sets"], 1)

    def test_runner_tests_the_batch_and_programs_each_board(self):
        out = os.path.join(self.tmp, "run")
        with open(os.path.join(self.proj, "pcbtestkit.json"), "w") as fh:
            json.dump({"testplan": {"probe_side": "bottom"}, "batch": {"count": 2},
                       "program": {"command": [sys.executable, "-c", "import sys; sys.exit(0 if sys.argv[1] == '2' else 3)",
                                               "{site}"]}}, fh)
        try:
            res = run_pipeline(Design.load(self.pcb), out_dir=out, include_kicad=False,
                               outputs={"visuals": False, "guide": True, "jig": False})
        finally:
            os.remove(os.path.join(self.proj, "pcbtestkit.json"))
        self.assertEqual(res["batch"]["count"], 2)
        self.assertEqual(res["program"][0]["header"], "J9")
        rdir = res["paths"]["runner"]
        with open(os.path.join(rdir, "hardware.json")) as fh:
            self.assertEqual(len(json.load(fh)["power"]), 2)              # one power switch per site
        hw = os.path.join(self.tmp, "hw_sim.json")
        with open(hw, "w") as fh:
            json.dump({"adc": {"type": "simulated"}}, fh)                 # simulated ADC, real program command
        r = subprocess.run([sys.executable, os.path.join(rdir, "pcbtest_runner.py"), "test", "--non-interactive",
                            "--allow-commands", "--hardware", hw, "--serial", "SN"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout[-1500:] + r.stderr[-1500:])     # site 1's programmer fails
        with open(os.path.join(rdir, "results", "results.csv")) as fh:
            rows = [ln.strip().split(",") for ln in fh.readlines()[1:]]
        self.assertEqual([(row[1], row[2], row[5]) for row in rows], [("SN-1", "FAIL", "1"), ("SN-2", "PASS", "2")])
        self.assertIn("SP", rows[0][3])
        with open(res["paths"]["guide"], encoding="utf-8") as fh:
            g = fh.read()
        self.assertIn("<h3>Programming</h3>", g)
        self.assertIn("put a board in each of the 2 sites", g)

    def test_agent_tool(self):
        ws = Workspace(self.pcb)
        r = ws.call("set_batch_and_programming", {"program_command": ["openocd", "-f", "x.cfg"], "rationale": "flash"})
        self.assertEqual(r["error"]["code"], "CONFIRM_REQUIRED")
        self.assertTrue(ws.call("set_batch_and_programming", {"batch_count": 3, "program_connector": "swd10",
                                                              "rationale": "3 boards per batch", "dry_run": True})["ok"])
        self.assertFalse(ws.call("set_batch_and_programming", {"program_headers": ["J99"]})["ok"])


class TestVideoRecovery(Base):
    """A video that fails after its frames are rendered says why, keeps the frames and reuses them next time."""

    def test_filter_graph_and_kept_frames(self):
        from pcbtestkit.video import _kept_shots, filter_graph, video_fingerprint, video_settings
        g, last, t = filter_graph([2.0, 3.0, 1.5], (640, 360), 24, 0.5, True)
        self.assertEqual(last, "x2")
        self.assertAlmostEqual(t, 5.5)                                   # two 0.5 s cross-fades overlap
        g2, last2, t2 = filter_graph([2.0, 3.0, 1.5], (640, 360), 24, 0.5, False)
        self.assertIn("concat=n=3", g2)                                  # old ffmpeg without xfade: plain cuts
        self.assertNotIn("xfade", g2)
        self.assertEqual((last2, t2), ("cat", 6.5))
        d = self.design()
        fp = video_fingerprint(d, {}, video_settings(d.cfg), (640, 360))
        self.assertEqual(fp, video_fingerprint(self.design(), {}, video_settings(d.cfg), (640, 360)))
        d12 = self.design(video={"fps": 12})
        self.assertNotEqual(fp, video_fingerprint(d12, {}, video_settings(d12.cfg), (640, 360)))
        anim = os.path.join(self.tmp, "anim_kept")
        os.makedirs(anim, exist_ok=True)
        frame = os.path.join(anim, "orbit_0000.png")
        with open(frame, "wb") as fh:
            fh.write(b"png")
        with open(os.path.join(anim, "shots.json"), "w") as fh:
            json.dump({"fingerprint": fp, "shots": {"orbit": {"images": [frame], "fps": 24, "hold": 1, "meta": {}}},
                       "stats": {}}, fh)
        self.assertIsNotNone(_kept_shots(anim, fp))
        self.assertIsNone(_kept_shots(anim, "another board or setting"))
        os.remove(frame)
        self.assertIsNone(_kept_shots(anim, fp))                          # a frame is gone: render again

    @unittest.skipIf(os.name == "nt", "a shell script stands in for ffmpeg")
    def test_ffmpeg_without_libx264_or_xfade(self):
        from pcbtestkit.video import ffmpeg_caps
        ff = os.path.join(self.tmp, "ffmpeg")
        with open(ff, "w") as fh:
            fh.write('#!/bin/sh\ncase "$2" in\n -encoders) printf " V....D libopenh264 OpenH264\\n V.S..D mpeg4 MPEG-4\\n";;\n'
                     ' -filters) printf " ... concat N->N Concatenate\\n";;\nesac\n')
        os.chmod(ff, 0o755)
        caps = ffmpeg_caps(ff)
        self.assertEqual([e[0] for e in caps["encoders"]], ["libopenh264", "mpeg4"])
        self.assertFalse(caps["xfade"])

    def test_failed_video_is_reported_and_frames_kept(self):
        from unittest import mock
        out = os.path.join(self.tmp, "video_fail")
        kept = os.path.join(out, "visuals", "scenes", "anim", "orbit_0000.png")
        os.makedirs(os.path.dirname(kept), exist_ok=True)
        with open(kept, "wb") as fh:
            fh.write(b"png")
        with mock.patch("pcbtestkit.video.find_ffmpeg", return_value=None):
            res = run_pipeline(self.design(video={"animate": False}), out_dir=out, include_kicad=False,
                               outputs={"visuals": False, "video": True, "jig": False})
        self.assertFalse(res["video"]["ok"])
        self.assertIn("ffmpeg not found", res["video"]["reason"])
        self.assertTrue(os.path.exists(res["paths"]["video_error"]))
        self.assertTrue(os.path.exists(kept))                             # not cleaned up after a failure
        with open(res["paths"]["guide"], encoding="utf-8") as fh:
            self.assertIn("The video could not be made", fh.read())
        with open(res["paths"]["report"], encoding="utf-8") as fh:
            self.assertIn(os.path.basename(res["paths"]["video_error"]), fh.read())
        # how to finish it from the kept frames: in the folder, and named in the error file
        work = os.path.join(out, "visuals", "scenes", "video")
        with open(res["paths"]["video_error"], encoding="utf-8") as fh:
            self.assertIn("visuals/scenes/video/BUILD-VIDEO.md".replace("/", os.sep), fh.read())
        for name in ("BUILD-VIDEO.md", "video-recipe.json", "storyboard.json"):
            with open(os.path.join(work, name), encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn(out, text, name)                             # relative paths only
        with open(os.path.join(work, "video-recipe.json"), encoding="utf-8") as fh:
            recipe = json.load(fh)
        self.assertEqual(recipe["output"], "../../../sample_sensor-explainer.mp4")
        self.assertEqual([st["step"] for st in recipe["steps"]], ["3d_frames", "captions", "mp4"])
        self.assertEqual(recipe["finish"], ["python", "-m", "pcbtestkit.video", "."])
        mp4 = recipe["steps"][2]
        self.assertEqual(mp4["with_captions"] is not None, recipe["steps"][1]["done"])   # drawn when wxPython is here
        if mp4["with_captions"]:
            self.assertEqual(mp4["with_captions"]["command"][0], "ffmpeg")

    @unittest.skipIf(os.name == "nt", "a shell script stands in for ffmpeg")
    def test_finish_video_from_kept_frames(self):
        """python -m pcbtestkit.video <folder> joins kept frames with ffmpeg, which runs in the folder with relative
        paths (short command lines, any user folder name on Windows)."""
        from unittest import mock
        from pcbtestkit import video as V
        work = os.path.join(self.tmp, "finish", "visuals", "scenes", "video")
        anim = os.path.join(self.tmp, "finish", "visuals", "scenes", "anim")
        os.makedirs(work, exist_ok=True)
        os.makedirs(anim, exist_ok=True)
        frames = []
        for i in range(3):
            frames.append(os.path.join(anim, "orbit_%04d.png" % i))
            with open(frames[-1], "wb") as fh:
                fh.write(b"png")
        job = {"size": [640, 360], "fps": 24, "fade": 0.5, "output": "../../../b-explainer.mp4", "frames_dir": ".",
               "scenes": [{"kind": "anim", "images": ["../anim/orbit_%04d.png" % i for i in range(3)], "fps": 24, "hold": 1.0,
                           "title": "Orbit"}, {"kind": "card", "bg": "../anim/orbit_0002.png", "duration": 2.0, "title": "End"},
                          {"kind": "map", "duration": 3.0, "title": "Probes"}]}
        with open(os.path.join(work, "storyboard.json"), "w", encoding="utf-8") as fh:
            json.dump(job, fh)
        ff = os.path.join(self.tmp, "finish", "ff.sh")
        log = os.path.join(self.tmp, "finish", "ff.log")
        with open(ff, "w") as fh:          # lists its capabilities, records how it was called, writes the MP4
            fh.write('#!/bin/sh\ncase "$2" in\n -encoders) printf " V....D libx264 H.264\\n"; exit 0;;\n'
                     ' -filters) printf " ... xfade VV->V Cross fade\\n"; exit 0;;\nesac\n'
                     'pwd > "%s"; printf "%%s\\n" "$@" >> "%s"\nfor a; do last="$a"; done\n: > "$last"\n' % (log, log))
        os.chmod(ff, 0o755)
        with mock.patch("sys.stdout", io.StringIO()) as printed:
            self.assertEqual(V.main([work, "--no-captions", "--ffmpeg", ff]), 0)
        self.assertIn("video written", printed.getvalue())
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "finish", "b-explainer.mp4")))
        with open(log) as fh:
            lines = fh.read().splitlines()
        self.assertEqual(os.path.realpath(lines[0]), os.path.realpath(work))     # ran in the folder
        self.assertIn("plain_00.txt", lines)
        self.assertIn("../anim/orbit_0002.png", lines)                    # the card's still, relative
        self.assertFalse([a for a in lines[1:] if a.startswith(self.tmp)])  # no absolute path on the command line
        with open(os.path.join(work, "plain_00.txt"), encoding="utf-8") as fh:
            self.assertIn("file '../anim/orbit_0000.png'", fh.read())
        # build notes for the same folder: every step with its command, the scenes with their timing
        md = V.write_build_notes(work, "ffmpeg not found")
        with open(md, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("python -m pcbtestkit.video .", text)
        self.assertIn("python -m pcbtestkit.video_frames storyboard.json", text)
        self.assertIn("| 0 | anim | Orbit |", text)

    def test_windows_paths_in_board_text(self):
        from pcbtestkit.kicad_cli import sexpr_path
        from pcbtestkit.sexpr import parse
        text = '(model "${KIPRJMOD}/3d/x.wrl")'.replace("${KIPRJMOD}", sexpr_path("C:\\Users\\builder\\new"))
        self.assertEqual(parse(text)[1], "C:/Users/builder/new/3d/x.wrl")     # no \b or \n read as escapes


class TestFacilities(Base):
    """Test facilities: the tester a fixture plugs into (its channels, fixed plate and connector, buses, driver)."""

    def test_builtin_facilities_template_and_validation(self):
        from pcbtestkit import facility as F
        found, warns = F.load_facilities()
        self.assertEqual(warns, [])
        self.assertTrue({"raspberry-pi", "bench-tester-example"} <= set(found))
        self.assertEqual(F.analog_limits(found["raspberry-pi"]), (None, None))     # the test plan's own settings
        self.assertEqual(F.analog_limits(found["bench-tester-example"]), (16, 10.0))
        self.assertTrue(F.validate(dict(F.TEMPLATE))["ok"])
        bad = json.loads(json.dumps(F.TEMPLATE))
        bad["fixture"]["connectors"][0]["pins"].update({"13": "AIN99X", "41": "GND"})
        bad["buses"]["rs485"] = {"via": "fixture"}
        errs = " | ".join(F.validate(bad)["errors"])
        self.assertIn("'AIN99X' is not a pin function", errs)
        self.assertIn("pin 41 is not on a 2 x 10 connector", errs)
        self.assertIn("buses.rs485 goes through the fixture but site 1's connector has no RS485_A, RS485_B pin", errs)
        # a facility of your own: PCBTESTKIT_FACILITIES, a folder with facility.json + driver.py
        from unittest import mock
        own = os.path.join(self.tmp, "facilities", "my-tester")
        os.makedirs(own, exist_ok=True)
        with open(os.path.join(own, "facility.json"), "w") as fh:
            json.dump(F.TEMPLATE, fh)
        with open(os.path.join(own, "driver.py"), "w") as fh:
            fh.write("class Driver: pass\n")
        with mock.patch.dict(os.environ, {"PCBTESTKIT_FACILITIES": os.path.dirname(own)}):
            self.assertIn("my-tester", F.load_facilities()[0])
            d = self.design(facility="my-tester")
            self.assertEqual(F.facility_for(d)["id"], "my-tester")
        d = self.design(facility="no-such-tester")
        self.assertEqual(F.facility_for(d)["id"], "raspberry-pi")
        self.assertTrue(any("no-such-tester" in w for w in d.warnings))

    def test_bus_nets_by_name(self):
        from pcbtestkit.facility import _NET_RE
        def sig(name):
            return [s for s, rx in _NET_RE.items() if rx.match(name)]
        for name, want in (("CANH", "CAN_H"), ("CAN1_L", "CAN_L"), ("USB_D+", "USB_DP"), ("USB_DM", "USB_DM"), ("D-", "USB_DM"),
                           ("ETH_TX_P", "ETH_TX_P"), ("RD-", "ETH_RX_N"), ("UART1_TX", "UART_TX"), ("RXD", "UART_RX"),
                           ("I2C_SDA", "I2C_SDA"), ("SCL", "I2C_SCL"), ("RS485_B", "RS485_B")):
            self.assertEqual(sig(name), [want], name)
        for name in ("VBUS", "TX+5V", "GND", "LED"):
            self.assertEqual(sig(name), [], name)
        from pcbtestkit.facility import bus_nets
        self.assertEqual(bus_nets(self.design()), {"i2c": {"I2C_SDA": "/SDA", "I2C_SCL": "/SCL"},
                                                   "usb": {"USB_DP": "/D+", "USB_DM": "/D-"}})

    def test_fixed_connector_plan_and_fixture(self):
        """The bench-tester example: 160 x 120 mm plate, fixed holes, its 2 x 20 connector carrying the channels,
        the supply and the I2C bus; USB is a cable check."""
        from pcbtestkit.testplan import build_testplan
        d = self.design(facility="bench-tester-example")
        plan = build_testplan(d)
        fac = plan["facility"]
        self.assertEqual((fac["id"], fac["analog_inputs"], plan["adc"]["max_input_v"]), ("bench-tester-example", 16, 10.0))
        self.assertEqual([(b["bus"], b["via"]) for b in fac["bus_checks"]], [("i2c", "fixture"), ("usb", "cable")])
        pins = {q["pin"]: q for q in fac["connectors"][0]["pins"]}
        self.assertEqual(len(pins), 40)
        self.assertEqual((pins[3]["function"], pins[3]["signal"]), ("AIN1", "CH01"))
        self.assertEqual((pins[23]["signal"], pins[29]["signal"], pins[30]["signal"]), ("VIN", "I2C_SDA", "I2C_SCL"))
        self.assertEqual(plan["header"], [dict(q, **({"net": n} if n else {})) for q, n in
                                          ((dict(pin=q["pin"], signal=q["signal"], function=q["function"]),
                                            {p["channel"]: p["net"] for p in plan["probes"] if p.get("channel")}.get(q["signal"])
                                            or {"I2C_SDA": "/SDA", "I2C_SCL": "/SCL"}.get(q["signal"]))
                                           for q in fac["connectors"][0]["pins"])])
        sda = next(p for p in plan["probes"] if p["net"] == "/SDA")
        self.assertEqual((sda["role"], sda["channel"], sda["header_pin"]), ("bus", None, 29))
        steps = {s_["id"]: s_ for s_ in plan["sequence"]}
        self.assertEqual((steps["SB1"]["type"], steps["SB1"]["bus"]), ("bus", "i2c"))
        self.assertIn("USB cable", steps["SB2"]["prompt"])
        fx = build_fixture(d, plan)
        self.assertEqual((fx["outline"]["width"], fx["outline"]["height"]), (160.0, 120.0))
        self.assertEqual(fx["facility"]["connectors"], [{"ref": fx["sites"][0]["header"], "name": "TESTER", "site": 1}])
        self.assertIsNone(fx["sites"][0]["power_terminal"])              # the facility supplies the board
        self.assertEqual(fx["routing"]["unrouted_connections"], 0)
        b = load_board_text(fx["kicad_pcb"])
        j = b.by_ref[fx["sites"][0]["header"]]
        self.assertEqual(len(j.pads), 40)
        net = {pd.number: pd.net for pd in j.pads}
        self.assertEqual((net["1"], net["23"], net["29"]), ("GND", "VIN", "TP_SDA"))
        self.assertEqual(net["37"], "")                                   # NC stays open
        ox, oy = fx["outline"]["x_min"], fx["outline"]["y_min"]
        self.assertAlmostEqual(j.x - ox, 55.87, places=2)               # pin 1 where the facility puts it
        self.assertAlmostEqual(j.y - oy, 114.0, places=2)
        holes = sorted((round(fp.x - ox, 2), round(fp.y - oy, 2)) for fp in b.footprints if fp.ref.startswith("H"))
        self.assertEqual(holes, [(5.0, 5.0), (5.0, 115.0), (155.0, 5.0), (155.0, 115.0)])

    def test_runner_drives_the_facility(self):
        """The runner hands measuring, power and bus checks to the facility's driver (here: SCPI over TCP)."""
        import socket
        import threading
        out = os.path.join(self.tmp, "fac_run")
        run_pipeline(self.design(facility="bench-tester-example"), out_dir=out, include_kicad=False,
                     outputs={"visuals": False, "jig": False, "guide": False, "report": False})
        rdir = os.path.join(out, "runner")
        self.assertTrue(os.path.exists(os.path.join(rdir, "facility_driver.py")))
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        seen = []

        def serve():
            con, _ = srv.accept()
            buf = b""
            with con:
                while True:
                    data = con.recv(4096)
                    if not data:
                        return
                    buf += data
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        cmd = line.decode()
                        seen.append(cmd)
                        if cmd.startswith("MEAS:VOLT?"):
                            con.sendall(b"0.33\n")
                        elif cmd.startswith("MEAS:CURR?"):
                            con.sendall(b"0.05\n")
                        elif cmd.startswith("BUS:"):
                            con.sendall(b"OK 2 devices\n")
        with open(os.path.join(rdir, "hardware.json")) as fh:
            hw = json.load(fh)
        hw["connection"].update(host="127.0.0.1", port=srv.getsockname()[1])
        with open(os.path.join(rdir, "hardware.json"), "w") as fh:
            json.dump(hw, fh)
        runs = []
        for only in ([], ["--only", "SB1", "SB2"]):      # the whole sequence (it stops at the 12 V check), the bus checks
            th = threading.Thread(target=serve, daemon=True)
            th.start()
            runs.append(subprocess.run([sys.executable, "pcbtest_runner.py", "test", "--non-interactive", "--serial", "F1"] + only,
                                       cwd=rdir, capture_output=True, text=True, timeout=120))
            th.join(5)
        srv.close()
        self.assertIn("OUTP1 ON", seen)
        self.assertIn("OUTP1 OFF", seen)                                  # switched off again at the end
        self.assertIn("MEAS:VOLT? (@1)", seen)                            # CH01 is on AIN1
        self.assertIn("FAIL", runs[0].stdout)                             # 0.33 V is not the 12 V supply
        self.assertTrue(any(c.startswith("BUS:I2C:TEST?") for c in seen), seen)
        self.assertTrue(any(c.startswith("BUS:USB:TEST?") for c in seen), seen)
        self.assertIn("OK 2 devices", runs[1].stdout)
        self.assertIn("PASS", runs[1].stdout)

    def test_set_facility_tool(self):
        ws = Workspace(self.pcb, persist=False)
        r = ws.call("get_schema", {"name": "facilities"})
        self.assertEqual(r["selected"], "raspberry-pi")
        self.assertIn("bench-tester-example", [f["id"] for f in r["facilities"]])
        self.assertEqual(ws.call("set_facility", {"facility": "nope"})["error"]["code"], "UNKNOWN_FACILITY")
        r = ws.call("set_facility", {"facility": "bench-tester-example", "rationale": "our bench tester"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(ws.call("get_testplan", {"section": "summary"})["ok"], True)
        from pcbtestkit.facility import facility_for
        self.assertEqual(facility_for(ws.design)["id"], "bench-tester-example")


class TestBatchFlowAndVideoJob(unittest.TestCase):
    """A batch programmed at the same time while the checks that need no firmware run; production timing; the test
    shot; the video's background job, render presets and resume."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pcbtk_flow_")
        cls.proj = os.path.join(cls.tmp, "sample_sensor")
        make_sample.main(cls.proj)
        cls.pcb = os.path.join(cls.proj, "sample_sensor.kicad_pcb")
        add_swd_header(cls.pcb)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def design(self, **over):
        over.setdefault("testplan", {"probe_side": "bottom"})
        return Design.load(self.pcb, overrides=over)

    def test_firmware_phases_and_timing(self):
        from pcbtestkit.timing import production_timing
        d = self.design(batch={"count": 3})
        plan = build_testplan(d, [])
        self.assertEqual(plan["firmware"]["programmed"], ["U3"])
        fw = {x["net"] for x in plan["firmware"]["firmware_nets"]}
        self.assertIn("/SWDIO", fw)
        self.assertIn("/LED", fw)                                    # through R3 from an MCU pin
        self.assertNotIn("+3V3", fw)                                 # a regulator output, no enable from the MCU
        phases = [st["phase"] for st in plan["sequence"]]
        order = ["unpowered", "power", "program", "during_program", "after_program", "end"]
        self.assertEqual(sorted(set(phases), key=order.index), [p for p in order if p in phases])
        self.assertEqual(phases, sorted(phases, key=order.index))   # in phase order
        prog = next(st for st in plan["sequence"] if st["phase"] == "program")
        during = [st["id"] for st in plan["sequence"] if st["phase"] == "during_program"]
        self.assertEqual(prog["concurrent"], during)
        rails = [c["net"] for st in plan["sequence"] if st["phase"] == "during_program" for c in st.get("checks") or []]
        self.assertIn("+3V3", rails)
        after = [c["net"] for st in plan["sequence"] if st["phase"] == "after_program" for c in st.get("checks") or []]
        self.assertTrue(set(after) <= fw)
        t = production_timing(d, plan, None)
        self.assertEqual(t["batch"], 3)
        self.assertLess(t["test"]["batch_s"], t["test"]["sequential_s"])        # programmed at the same time
        per = [r["per_board_s"] for r in t["by_batch_size"]]
        self.assertEqual(per, sorted(per, reverse=True))
        self.assertEqual(len(t["test"]["timeline"]), 3)
        self.assertTrue(all(any(sg["phase"] == "programming" for sg in ln["segments"]) for ln in t["test"]["timeline"]))
        plain = build_testplan(self.design(program={"headers": []}), [])          # nothing to program: no phase for it
        self.assertNotIn("program", [st["phase"] for st in plain["sequence"]])

    def test_runner_programs_the_batch_at_the_same_time(self):
        out = os.path.join(self.tmp, "flow")
        prog = os.path.join(self.tmp, "programmer.py")
        with open(prog, "w") as fh:
            fh.write("import json, sys, time\nt0 = time.time()\ntime.sleep(1.5)\n"
                     "open(sys.argv[2] + '/prog_%s.json' % sys.argv[1], 'w').write(json.dumps([t0, time.time()]))\n"
                     "print('verified site', sys.argv[1])\n")
        d = self.design(batch={"count": 3}, program={"command": [sys.executable, prog, "{site}", out]})
        run_pipeline(d, out_dir=out, include_kicad=False, outputs={"visuals": False, "jig": False, "guide": False, "report": False})
        rdir = os.path.join(out, "runner")
        with open(os.path.join(rdir, "hardware.json"), "w") as fh:      # simulated readings, real programmers
            json.dump({"adc": {"type": "simulated"}}, fh)
        t0 = time.time()
        r = subprocess.run([sys.executable, "pcbtest_runner.py", "test", "--non-interactive", "--allow-commands",
                            "--serial", "B"], cwd=rdir, capture_output=True, text=True, timeout=120)
        took = time.time() - t0
        spans = []
        for k in (1, 2, 3):
            with open(os.path.join(out, "prog_%d.json" % k)) as fh:
                spans.append(json.load(fh))
        self.assertLess(max(s[0] for s in spans), min(s[1] for s in spans), spans)   # all three at the same time
        self.assertLess(took, 3 * 1.5 + 8, r.stdout)
        self.assertIn("Programming 3 board(s) at the same time", r.stdout)
        self.assertIn("Checks while the boards are programmed", r.stdout)
        res = sorted(glob.glob(os.path.join(rdir, "results", "B-*.json")))
        self.assertEqual(len(res), 3)
        for p in res:
            with open(p) as fh:
                rec = json.load(fh)
            sp = [x for x in rec["results"] if x["step"] == "SP"]
            self.assertEqual(sp[0]["status"], "pass", rec)
            steps = [x["step"] for x in rec["timings"]]
            self.assertLess(steps.index("S4"), steps.index("SP"))      # rails measured while programming
            self.assertTrue(all(x["seconds"] >= 0 for x in rec["timings"]))

    def test_glow_mesh_and_test_scene(self):
        from pcbtestkit import animate as A
        from pcbtestkit.video import _live_test_scene
        b = self.design().board
        w = A.glow_wrl(b, ["+3V3"], A.GLOW["during_program"], 1.6)
        self.assertIn("emissiveColor", w)
        self.assertIn("coordIndex", w)
        self.assertGreater(len(A.glow_wrl(b, ["+3V3"], (1, 1, 1), 1.6, outline=True)), len(w))
        self.assertEqual(A.glow_wrl(b, ["no such net"], (1, 1, 1), 1.6), "#VRML V2.0 utf8\n")
        # KiCad's ray tracer draws a face only from the side its winding points to: every lit face points away from
        # the board on the side it lights (+z on top, -z under a board that lies bottom side up on the fixture)
        import re
        for side, sign in (("top", 1), ("bottom", -1)):
            w = A.glow_wrl(b, ["+3V3", "GND"], (1, 1, 1), 1.6, side, outline=True)
            pts = [tuple(map(float, ln.split())) for ln in re.search(r"point \[\n(.*?) \]", w, re.S).group(1).split(",\n")]
            for face in re.search(r"coordIndex \[\n(.*?) \]", w, re.S).group(1).split(",-1"):
                ix = [int(i) for i in face.replace("\n", "").split(",") if i.strip()]
                if len(ix) < 3:
                    continue
                area = sum(pts[ix[k]][0] * pts[ix[(k + 1) % len(ix)]][1] - pts[ix[(k + 1) % len(ix)]][0] * pts[ix[k]][1]
                           for k in range(len(ix)))
                self.assertGreaterEqual(area * sign, -1e-9, (side, ix))
        lines = ["== Unpowered checks ==", "  [S1] S2 Short check", "    [S1] PASS R_+3V3", "  [S2] S2 Short check",
                 "    [S2] PASS R_+3V3", "== Programming 2 board(s) at the same time", "  [S1] S4 Rails", "  [S2] S4 Rails"]
        shot = {"images": ["test_%04d.png" % i for i in range(8)], "fps": 24, "hold": 1.0,
                "meta": {"sites": 2, "events": [
                    {"label": "Board 1: short check", "phase": "unpowered", "sites": [1], "steps": ["S2"], "f0": 0, "f1": 1},
                    {"label": "Board 2: short check", "phase": "unpowered", "sites": [2], "steps": ["S2"], "f0": 2, "f1": 3},
                    {"label": "Board 1: rails", "phase": "during_program", "sites": [1], "steps": ["S4"], "f0": 4, "f1": 5},
                    {"label": "Board 2: rails", "phase": "during_program", "sites": [2], "steps": ["S4"], "f0": 6, "f1": 7}]}}
        sc = _live_test_scene(shot, lines, {"sequence": [{"phase": "program"}]})
        self.assertEqual(sc["kind"], "anim_live")
        self.assertEqual(sc["line_at"], sorted(sc["line_at"]))
        self.assertEqual(sc["caption_at"], [0, 0, 1, 1, 2, 2, 3, 3])
        self.assertEqual(sc["line_at"][2], 4)                         # board 2's short check starts at its own line
        self.assertEqual(sc["title"], "Test the batch of 2 boards")

    def test_render_presets_resume_and_cancel(self):
        import threading
        from pcbtestkit import animate as A
        from pcbtestkit.visuals import RAYTRACE_PRESETS, Renderer
        rd = Renderer(os.path.join(self.tmp, "presets"), 64, 36)
        self.assertIs(rd.env_for("basic"), rd.env)
        env = rd.env_for("standard")
        cfg_dirs = glob.glob(os.path.join(env["KICAD_CONFIG_HOME"], "*", "3d_viewer.json"))
        with open(cfg_dirs[0]) as fh:
            render = json.load(fh)["render"]
        self.assertTrue(render["raytrace_shadows"] and render["raytrace_post_processing"])
        self.assertFalse(render["raytrace_reflections"] or render["raytrace_anti_aliasing"])
        self.assertTrue(RAYTRACE_PRESETS["best"]["raytrace_anti_aliasing"])

        class FakeRd:
            scene_dir = os.path.join(self.tmp, "fake_scenes")
            log = []
            calls = []

            def render(self, scene, png, **cam):
                FakeRd.calls.append(png)
                with open(png, "wb") as fh:
                    fh.write(b"png")
                return True
        os.makedirs(FakeRd.scene_dir, exist_ok=True)
        frames = os.path.join(self.tmp, "fake_frames")
        os.makedirs(frames, exist_ok=True)
        sh = A.Shot("s", 24)
        for i in range(6):
            sh.add(os.path.join(frames, "s_%04d.png" % i), {}, scene="x.kicad_pcb")
        with open(sh.jobs[0]["png"], "wb") as fh:
            fh.write(b"kept")
        r = A.render_shots([sh], FakeRd(), workers=2, resume=True)
        self.assertEqual((r["kept"], r["rendered"], len(FakeRd.calls)), (1, 5, 5))
        self.assertEqual(len(sh.files), 6)
        stop = threading.Event()
        stop.set()
        sh2 = A.Shot("s2", 24)
        for i in range(20):
            sh2.add(os.path.join(frames, "c_%04d.png" % i), {}, scene="x.kicad_pcb")
        with self.assertRaises(A.Cancelled):
            A.render_shots([sh2], FakeRd(), workers=1, cancel=stop)

    def test_video_job_progress_and_single_instance(self):
        from unittest import mock
        from pcbtestkit import video_job as J
        out = os.path.join(self.tmp, "job")
        seen = []
        tr = J.Tracker(out, seen.append)
        with mock.patch("pcbtestkit.video_job.time.time", side_effect=[100.0, 100.0, 110.0, 110.0, 111.0]):
            tr.first = None
            tr.update("Rendering video frame 10 of 100", 0.9)
            tr.update("Rendering video frame 60 of 100", 0.92)
        self.assertEqual((tr.st["done"], tr.st["total"], tr.st["fps"], tr.st["eta_s"]), (60, 100, 5.0, 8))
        self.assertIn("Frame 60 of 100 - 5.0 frames/s - about 8 s left", J.describe(tr.st))
        tr.finish("done", "Video written", {"ok": True, "path": os.path.join(out, "b-explainer.mp4")})
        st = J.status(out)
        self.assertEqual((st["state"], st["result"]["video"]), ("done", "b-explainer.mp4"))   # no absolute path
        self.assertFalse(J.running(out))
        with open(os.path.join(out, "visuals", J.STATUS), "w") as fh:     # a job of this process "is running"
            json.dump({"state": "running", "pid": os.getpid(), "message": "Starting"}, fh)
        self.assertTrue(J.running(out))
        r = J.launch(self.pcb, out, window=False)
        self.assertFalse(r["ok"])
        self.assertIn("already running", r["reason"])


class TestBatchProgrammingEdgeCases(unittest.TestCase):
    """The batch flow's programmers: chatty, shared, missing and hung ones (a review of run_batch)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pcbtk_progedge_")
        proj = os.path.join(cls.tmp, "sample_sensor")
        make_sample.main(proj)
        cls.pcb = os.path.join(proj, "sample_sensor.kicad_pcb")
        add_swd_header(cls.pcb)
        cls.out = os.path.join(cls.tmp, "out")
        d = Design.load(cls.pcb, overrides={"testplan": {"probe_side": "bottom"}, "batch": {"count": 2},
                                            "program": {"command": ["placeholder"]}})
        run_pipeline(d, out_dir=cls.out, include_kicad=False, outputs={"visuals": False, "jig": False, "guide": False,
                                                                        "report": False})
        cls.rdir = os.path.join(cls.out, "runner")
        with open(os.path.join(cls.rdir, "hardware.json"), "w") as fh:
            json.dump({"adc": {"type": "simulated"}}, fh)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_with(self, cmd, timeout_s=60, serial="E"):
        with open(os.path.join(self.rdir, "testplan.json")) as fh:
            plan = json.load(fh)
        for st in plan["sequence"]:
            if st["type"] == "program":
                st["command"], st["timeout_s"] = cmd, timeout_s
        with open(os.path.join(self.rdir, "testplan.json"), "w") as fh:
            json.dump(plan, fh)
        shutil.rmtree(os.path.join(self.rdir, "results"), ignore_errors=True)
        t0 = time.time()
        r = subprocess.run([sys.executable, "pcbtest_runner.py", "test", "--non-interactive", "--allow-commands",
                            "--serial", serial], cwd=self.rdir, capture_output=True, text=True, timeout=180)
        recs = {}
        for p in glob.glob(os.path.join(self.rdir, "results", "*.json")):
            with open(p) as fh:
                rec = json.load(fh)
            recs[rec["site"]] = rec
        return r, recs, time.time() - t0

    def script(self, name, body):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            fh.write(body)
        return path

    def test_chatty_programmer_does_not_stall(self):
        p = self.script("chatty.py", "import sys\nfor i in range(4000):\n    print('x' * 80)\nprint('verified', sys.argv[1])\n")
        r, recs, took = self.run_with([sys.executable, p, "{site}"])
        for k in (1, 2):
            sp = [x for x in recs[k]["results"] if x["step"] == "SP"][0]
            self.assertEqual(sp["status"], "pass", r.stdout[-2000:])
            self.assertIn("verified %d" % k, sp.get("note", ""))
        self.assertLess(took, 60)

    def test_command_without_site_programs_one_after_the_other(self):
        p = self.script("shared.py", "import json, sys, time\nt0 = time.time()\ntime.sleep(0.8)\n"
                                     "open(sys.argv[1] + '/shared_%d.json' % int(t0 * 1000), 'w').write(json.dumps([t0, time.time()]))\n")
        r, recs, _ = self.run_with([sys.executable, p, self.tmp])
        self.assertIn("no {site}: the boards are programmed one after the other", r.stdout)
        spans = []
        for f in sorted(glob.glob(os.path.join(self.tmp, "shared_*.json"))):
            with open(f) as fh:
                spans.append(json.load(fh))
        self.assertEqual(len(spans), 2)
        self.assertLessEqual(spans[0][1], spans[1][0] + 0.05)                       # never at the same time

    def test_missing_programmer_skips_the_firmware_checks(self):
        r, recs, _ = self.run_with(["/no/such/programmer-{site}"])
        for k in (1, 2):
            res = recs[k]["results"]
            self.assertEqual([x["status"] for x in res if x["step"] == "SP"], ["fail"])
            after = [x for x in res if x.get("note") == "not programmed"]
            self.assertTrue(after, res)
            self.assertEqual(recs[k]["verdict"], "FAIL")

    def test_hung_programmer_is_killed_and_its_output_kept(self):
        p = self.script("hung.py", "import sys, time\nprint('connecting to probe', sys.argv[1], flush=True)\ntime.sleep(60)\n")
        r, recs, took = self.run_with([sys.executable, p, "{site}"], timeout_s=2)
        self.assertLess(took, 40)
        for k in (1, 2):
            sp = [x for x in recs[k]["results"] if x["step"] == "SP"][0]
            self.assertEqual(sp["status"], "fail")
            self.assertIn("timed out after 2 s", sp["note"])
            self.assertIn("connecting to probe %d" % k, sp["note"])
