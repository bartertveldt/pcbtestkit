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
# the same part shapes on every machine (KiCad's 3D export only where a test asks for it: TestKiCadShapes)
os.environ.setdefault("PCBTESTKIT_NO_KICAD_3D", "1")
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
        self.assertAlmostEqual(holds["C2"]["depth_mm"], holds["C2"]["part_height_mm"], places=2)   # exact: no offset
        # U1 (TO-220, its ground pin joined solidly to the ground pour) gets room for a heat-resistant insert
        self.assertAlmostEqual(holds["U1"]["depth_mm"], holds["U1"]["part_height_mm"] + 1.0, places=2)


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
        build = [s for s in scenes if s.get("title") == "Build the fixture"][0]
        self.assertIn("Fit a nest and a toggle clamp for every board", build["bullets"])     # nests, not guide pins
        press = [s for s in scenes if s.get("title") == "Test a board"][0]
        self.assertIn("into its nest", press["caption"])
        still = storyboard(d, spec, plan, None, None, {"images": {}}, out_dir, out_dir, {})
        self.assertFalse([s for s in still if s["kind"] == "anim"])


def _cyl_x(ref, cx, cy, r, length, z0, n=24):
    """A cylinder lying along x (an axial part) on the plane z0, as a part mesh."""
    import math
    from pcbtestkit.parts3d import PartMesh
    tris, zc = [], z0 + r
    ring = [(math.cos(2 * math.pi * k / n) * r, math.sin(2 * math.pi * k / n) * r) for k in range(n)]
    x0, x1 = cx - length / 2.0, cx + length / 2.0
    for k in range(n):
        (ya, za), (yb, zb) = ring[k], ring[(k + 1) % n]
        p = [(x0, cy + ya, zc + za), (x1, cy + ya, zc + za), (x1, cy + yb, zc + zb), (x0, cy + yb, zc + zb)]
        tris += [p[0] + p[1] + p[2], p[0] + p[2] + p[3], (x0, cy, zc) + p[0] + p[3], (x1, cy, zc) + p[2] + p[1]]
    return PartMesh(ref, tris, "vrml")


def _box(ref, x0, y0, x1, y1, z0, z1):
    from pcbtestkit.parts3d import box_mesh
    m = box_mesh(ref, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], z0, z1)
    m.source = "vrml"                       # count it as a real shape
    return m


class TestJigFit(Base):
    """Jigs cut to the parts' real 3D shapes: every hand-soldered part touches its pocket the moment the board lands,
    nothing else touches anything on the way in (pcbtestkit.parts3d, pcbtestkit.jigfit)."""

    def _with(self, d, **shapes):
        from pcbtestkit import parts3d as P3
        ms = P3.part_meshes(d)
        ms.update(shapes)
        return d

    def _depth(self, g, x, y):
        """mm below the support plane of the holding jig at board point (x, y)."""
        i, j = int((x - g["ox"]) / g["res"]), int((y - g["oy"]) / g["res"])
        return (int(round(g["support"] * 100)) - g["H"][j][i]) / 100.0

    def test_every_part_touches_nothing_collides(self):
        from pcbtestkit.jig import build_jigs
        d = self.design()
        j = build_jigs(d, grids={})
        f = j["fit"]
        self.assertTrue(f["ok"], f["summary"])
        self.assertEqual(sorted(f["touching"]), sorted(j["parts_to_place"]))
        self.assertEqual(f["holding_collisions"], {})
        self.assertEqual(f["placement_collisions"], {})
        self.assertEqual(f["board_contacts"], {"holding": 0, "placement": 0})
        self.assertEqual(f["stack_collisions"], 0)
        for key, e in j["extra"].items():
            self.assertTrue(e["fit"]["ok"], "%s: %s" % (key, e["fit"]["summary"]))
        # held sideways: every part slides at least its clearance and less than clearance + one grid cell
        self.assertEqual(sorted(f["side_play"]), sorted(j["parts_to_place"]))
        for ref, p in f["side_play"].items():
            for way, v in p.items():                          # less any room asked for an insert
                self.assertGreaterEqual(v - f["room"].get(ref, 0.0), 0.15, (ref, way))
                self.assertLess(v - f["room"].get(ref, 0.0), f["side_play_limit_mm"], (ref, way))
        self.assertEqual(f["not_held_sideways"], [])
        self.assertIn("Fit check", j["notes"][-1] if "Fit check" in j["notes"][-1] else " ".join(j["notes"]))

    def test_pocket_follows_the_shape(self):
        """An axial part lying down: the pocket is a cradle (deepest under the top of the cylinder, shallower on its
        flanks) and the part touches it along the top; "flat" pockets are one depth all over."""
        from pcbtestkit.jig import build_jigs
        for floor in ("shaped", "flat"):
            d = self.design(jig={"pocket_floor": floor})
            fp = d.board.by_ref["C1"]
            t = d.board.thickness
            self._with(d, C1=_cyl_x("C1", fp.x, fp.y, 3.0, 10.0, t))
            grids = {}
            j = build_jigs(d, grids=grids)
            self.assertTrue(j["fit"]["ok"], j["fit"]["summary"])
            self.assertIn("C1", j["fit"]["touching"])
            g = grids["holding"]
            self.assertAlmostEqual(self._depth(g, fp.x, fp.y), 6.0, delta=0.011)
            flank = self._depth(g, fp.x, fp.y + 2.2)
            if floor == "shaped":
                self.assertLess(flank, 5.7)
                self.assertGreater(flank, 5.0)
            else:
                self.assertAlmostEqual(flank, 6.0, delta=0.011)

    def test_the_check_finds_errors(self):
        from pcbtestkit import jigfit as F
        from pcbtestkit.jig import Q, build_jigs, present_parts, select_parts, view_meshes
        loose = build_jigs(self.design(jig={"hold_depth_offset_mm": 0.1, "heat_insert_mm": 0}))["fit"]
        self.assertFalse(loose["ok"])
        self.assertEqual(sorted(loose["loose"]), sorted(loose["touching"] + list(loose["loose"])))
        self.assertTrue(all(abs(v - 0.1) < 0.012 for v in loose["loose"].values()))
        tight = build_jigs(self.design(jig={"hold_depth_offset_mm": -0.1, "heat_insert_mm": 0}))["fit"]
        self.assertEqual(len(tight["holding_collisions"]), 5)
        self.assertIn("FIT CHECK FAILED", tight["summary"])
        # one cell raised by 0.05 mm under the top of a part is found
        d = self.design(jig={"heat_insert_mm": 0})
        grids = {}
        j = build_jigs(d, grids=grids)
        g = grids["holding"]
        sel = select_parts(d)[0]
        ms = view_meshes(d, present_parts(d, sel, None))
        top = max(ms["U1"].tris, key=lambda tr: max(tr[2], tr[5], tr[8]))
        i, k = int((top[0] - g["ox"]) / g["res"]), int((top[1] - g["oy"]) / g["res"])
        g["H"][k][i] += 5
        hit = F.check_holding(d, g, ms, list(ms), [p.ref for p in sel], Q)
        self.assertTrue(0.04 <= hit["collisions"]["U1"] <= 0.05, hit["collisions"])     # less the pocket's rounding
        self.assertTrue(j["fit"]["ok"])
        g["H"][k][i] -= 5
        # a wall cut away on one side: the part slides that way, and only that way
        x1 = max(v for tr in ms["U1"].tris for v in (tr[0], tr[3], tr[6]))
        for jj in range(g["ny"]):
            for ii in range(g["nx"]):
                if g["ox"] + ii * g["res"] > x1:
                    g["H"][jj][ii] = 0
        p, by = F.side_play(d, g, ms["U1"], Q, 0.15, 0.45)
        self.assertEqual((p["+x"], by["+x"]), (0.45, ""))                    # nothing holds it that way
        self.assertLess(p["-x"], 0.45)
        self.assertEqual(by["-x"], "jig")

    def test_hot_parts_get_room_for_an_insert(self):
        """A part that gets hot all over while soldered (heavy ground pin on a metal body) gets room all round for a
        heat-resistant insert in the holding jig, and touches the insert as the board lands; jig.room gives any part
        room of your own; hot_parts / heat_insert_mm turn it off."""
        from pcbtestkit import heat
        from pcbtestkit.jig import build_jigs, select_parts
        d = self.design()
        hot = heat.hot_parts(d, select_parts(d)[0])
        self.assertEqual(sorted(hot), ["U1"])
        self.assertIn("metal body (to-220)", hot["U1"])
        self.assertTrue(any("joined solidly to the GND zone" in w for w in hot["U1"]))
        j = build_jigs(self.design(jig={"room": {"C1": 0.5}}), grids={})
        f = j["fit"]
        self.assertTrue(f["ok"], f["summary"])
        self.assertEqual(f["room"], {"U1": 1.0, "C1": 0.5})
        self.assertIn("U1", f["touching"])                       # through its 1 mm insert
        p = {q["ref"]: q for q in j["pockets"] if q["type"] == "hold"}
        self.assertAlmostEqual(p["U1"]["depth_mm"] - p["U1"]["part_height_mm"], 1.0, places=2)
        self.assertAlmostEqual(p["C1"]["depth_mm"] - p["C1"]["part_height_mm"], 0.5, places=2)
        self.assertEqual((p["U1"]["room_for"], p["C1"]["room_for"]), ("heat insert", "jig.room"))
        self.assertTrue(all(v >= 1.15 for v in f["side_play"]["U1"].values()))     # room sideways too
        self.assertTrue(any(n.startswith("Heat: U1 get hot") for n in j["notes"]))
        for over in ({"hot_parts": "none"}, {"heat_insert_mm": 0}, {"room": {"U1": 0}}):
            self.assertEqual(build_jigs(self.design(jig=over))["room"], {}, over)
        self.assertEqual(sorted(build_jigs(self.design(jig={"hot_parts": ["C*"]}))["room"]), ["C1", "C2"])
        from pcbtestkit.agent import Workspace
        ws = Workspace(self.pcb, persist=False)
        self.assertEqual(ws.call("set_jig_options", {"room": {"J3": 20}})["error"]["code"], "OUT_OF_RANGE")
        self.assertTrue(ws.call("set_jig_options", {"room": {"J3": 1.5}, "hot_parts": "none"})["ok"])
        dec = {x["topic"] for x in Workspace(self.pcb, persist=False).call("get_open_decisions")["decisions"]}
        self.assertIn("hot_parts", dec)

    def test_overhanging_connector(self):
        """A part reaching 5 mm past the board's edge and 3 mm below its face: the placement jig is cut away under
        it, no locating stop sits there, and the holding jig's rim is cut over it."""
        from pcbtestkit.jig import build_jigs
        d = self.design()
        t = d.board.thickness
        self._with(d, J1=_box("J1", 95.0, 100.0, 106.0, 108.0, t - 3.0, t + 3.0))
        grids = {}
        j = build_jigs(d, grids=grids)
        self.assertTrue(j["fit"]["ok"], j["fit"]["summary"])
        A = grids["placement"]
        i, k = int((97.0 - A["ax0"]) / A["res"]), int((A["ay1"] - 104.0) / A["res"])
        self.assertLessEqual(A["H"][k][i] / 100.0, A["rest"] + t - 3.0 - 0.5 + 1e-6)
        for s in j["placement"]["stops"]:
            if s["side"] == "left":                         # a stop reaches about 8 mm along the edge
                self.assertFalse(100.0 - 8.0 < s["at"][1] < 108.0 + 8.0, s)
        self.assertGreater(self._depth(grids["holding"], 97.0, 104.0), 3.0)       # cut through the rim

    def test_ignored_parts_and_models(self):
        from pcbtestkit.jig import build_jigs, jig_ignored
        d = self.design(jig={"ignore": ["C1", "*PinHeader*"], "ignore_models": ["*TO-220*"]})
        ign = jig_ignored(d)
        self.assertEqual(ign["C1"], "jig.ignore")
        self.assertEqual(ign["J2"], "jig.ignore")                       # by footprint name
        self.assertEqual(ign["U1"], "its 3D model is in jig.ignore_models")
        j = build_jigs(d)
        self.assertEqual(sorted(j["parts_to_place"]), ["C2", "J3"])
        self.assertFalse({"C1", "J2", "U1"} & {p["ref"] for p in j["pockets"]})
        self.assertTrue(j["fit"]["ok"])
        self.assertEqual(j["fit"]["parts_checked"], len([f for f in d.components() if not f.ref.startswith("H")]) - 3)
        self.assertEqual(set(j["ignored"]), {"C1", "J2", "U1"})
        from pcbtestkit.agent import Workspace
        ws = Workspace(self.pcb, persist=False)
        self.assertEqual(ws.call("set_jig_options", {"ignore": ["NOPE*"]})["error"]["code"], "UNKNOWN_PART")
        self.assertTrue(ws.call("set_jig_options", {"ignore": ["C1"], "ignore_models": ["*case*"]})["ok"])

    def test_post_stops_short_of_a_part_under_the_hole(self):
        from pcbtestkit.jig import build_jigs
        d = self.design()
        self._with(d, C9=_box("C9", 103.0, 83.0, 105.0, 85.0, -1.0, 0.0))       # under H1, on the bottom side
        grids = {}
        j = build_jigs(d, grids=grids)
        self.assertTrue(j["fit"]["ok"], j["fit"]["summary"])
        self.assertIn("H1", [p["hole"] for p in j["posts"]])
        g = grids["holding"]
        S = int(round(g["support"] * 100))
        i, k = int((104.0 - g["ox"]) / g["res"]), int((84.0 - g["oy"]) / g["res"])
        self.assertLessEqual(g["H"][k][i], S + int(round((d.board.thickness - 0.2) * 100)))   # ends 0.2 mm above C9
        i, k = int((156.0 - g["ox"]) / g["res"]), int((84.0 - g["oy"]) / g["res"])
        self.assertEqual(g["H"][k][i], S + int(round((d.board.thickness + 1.0) * 100)))      # H2: full length

    def test_thin_board_rim(self):
        from pcbtestkit.jig import build_jigs
        d = self.design()
        d.board.thickness = 1.0
        j = build_jigs(d, grids={})
        self.assertTrue(j["fit"]["ok"], j["fit"]["summary"])
        self.assertTrue(any("rim height 0.70 mm" in n for n in j["notes"]))

    def test_fit_files_line_up_in_kicad_coordinates(self):
        import struct
        from pcbtestkit import parts3d as P3
        from pcbtestkit.jig import build_jigs
        d = self.design()
        out = os.path.join(self.tmp, "fitfiles")
        j = build_jigs(d, grids={}, fit_dir=out)
        names = sorted(os.listdir(out))
        self.assertEqual(names, sorted(["README.txt", "sample_sensor-top-board.stl", "sample_sensor-top-holding-jig.stl",
                                        "sample_sensor-top-placement-jig.stl", "sample_sensor-paste-nest-top-board.stl",
                                        "sample_sensor-paste-nest-top-nest.stl"]))

        def zrange(name):
            with open(os.path.join(out, name), "rb") as fh:
                b = fh.read()
            n = struct.unpack_from("<I", b, 80)[0]
            zs = [struct.unpack_from("<12f", b, 84 + 50 * k)[z] for k in range(n) for z in (5, 8, 11)]
            return min(zs), max(zs)
        top, bot = P3.kicad_seats(d)
        t = d.board.thickness
        lo, hi = zrange("sample_sensor-top-holding-jig.stl")
        self.assertAlmostEqual(hi, top + j["support_height_mm"], delta=0.01)       # its base, on top of the stack
        self.assertAlmostEqual(lo, top - t - 1.0, delta=0.011)                      # the posts' tips
        lo, hi = zrange("sample_sensor-top-placement-jig.stl")
        self.assertAlmostEqual(hi, bot + t - 0.2, delta=0.011)                      # the stops
        self.assertAlmostEqual(lo, bot - j["placement"]["pcb_rest_height_mm"], delta=0.011)
        self.assertEqual(j["fit_files"][0], "sample_sensor-top-board.stl")

    def test_board_text_for_kicad_export(self):
        from pcbtestkit.parts3d import board_for_export, model_matches
        txt = ('(footprint "X" (model "${KIPRJMOD}/3d/Case_Top.STEP" (offset (xyz 0 0 0)))\n'
               ' (model "rel/thing.wrl" (offset (xyz 0 0 0))) (model "${NOT_SET}/a.step" (hide yes)))')
        out = board_for_export(txt, "/proj", ["case*"])
        self.assertNotIn("Case_Top", out)
        self.assertIn('(model "/proj/rel/thing.wrl" (offset (xyz 0 0 0)))', out)
        self.assertIn('(model "${NOT_SET}/a.step" (hide yes))', out)
        self.assertTrue(model_matches("${X}/a/b/Case_Top.STEP", ["case*"]))
        self.assertFalse(model_matches("${X}/a/b/R_0603.step", ["case*"]))

    def test_read_kicad_glb(self):
        """The reader of KiCad's 3D export (binary glTF): node transforms and KiCad's seating planes, top and bottom."""
        import json
        import struct
        from pcbtestkit import parts3d as P3
        d = self.design()
        fps = {r: [d.board.by_ref[r]] for r in ("R1", "C9")}
        r1, c9 = fps["R1"][0], fps["C9"][0]
        pos = struct.pack("<9f", 0, 0, 0, 0.001, 0, 0, 0, 0.002, 0)
        idx = struct.pack("<3H", 0, 1, 2) + b"\0\0"
        blob = pos + idx
        js = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}],
              "nodes": [{"children": [1, 3]},
                        {"name": "R1", "translation": [r1.x / 1000.0, 0.001595, r1.y / 1000.0], "children": [2]},
                        {"mesh": 0},
                        {"name": "C9", "translation": [c9.x / 1000.0, -0.000085, c9.y / 1000.0],
                         "rotation": [1.0, 0.0, 0.0, 0.0], "children": [2]}],
              "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "mode": 4}]}],
              "buffers": [{"byteLength": len(blob)}],
              "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 36}, {"buffer": 0, "byteOffset": 36, "byteLength": 6}],
              "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3"},
                            {"bufferView": 1, "componentType": 5123, "count": 3, "type": "SCALAR"}]}
        jb = json.dumps(js).encode()
        jb += b" " * (-len(jb) % 4)
        data = struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(jb) + 8 + len(blob)) + struct.pack("<II", len(jb), 0x4E4F534A) + jb \
            + struct.pack("<II", len(blob), 0x004E4942) + blob
        path = os.path.join(self.tmp, "t.glb")
        with open(path, "wb") as fh:
            fh.write(data)
        nodes = P3.read_glb(path)
        self.assertEqual([n[0] for n in nodes], ["R1", "C9"])
        ms = P3._from_glb(d, path, fps)
        t = d.board.thickness
        a = ms[id(r1)].tris[0]
        self.assertAlmostEqual(a[0], r1.x, places=4)
        self.assertAlmostEqual(a[2], t, places=4)                 # on the top face
        self.assertAlmostEqual(a[8], t + 2.0, places=4)           # 2 mm up
        b = ms[id(c9)].tris[0]
        self.assertAlmostEqual(b[2], 0.0, places=4)               # on the bottom face
        self.assertAlmostEqual(b[8], -2.0, places=4)              # 2 mm down (turned over about X)


class TestKiCadShapes(Base):
    """With kicad-cli (KiCad 8+): the shapes come from KiCad's own 3D export and the jigs fit them."""

    def test_jigs_fit_kicads_shapes(self):
        from unittest import mock
        from pcbtestkit.kicad_cli import find_kicad_cli
        if not find_kicad_cli():
            self.skipTest("kicad-cli not found")
        env = {k: v for k, v in os.environ.items() if k != "PCBTESTKIT_NO_KICAD_3D"}
        env["PCBTESTKIT_HOME"] = os.path.join(self.tmp, "home3d")
        with mock.patch.dict(os.environ, env, clear=True):
            from pcbtestkit import parts3d as P3
            from pcbtestkit.jig import build_jigs
            d = self.design()
            ms = P3.part_meshes(d)
            kicad = [r for r, m in ms.items() if m.source == "kicad"]
            if not kicad:
                self.skipTest("KiCad's 3D library is not installed")
            j = build_jigs(d)
            self.assertTrue(j["fit"]["ok"], j["fit"]["summary"])
            self.assertTrue(set(j["fit"]["touching"]) >= {p for p in j["parts_to_place"] if p in kicad})


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

    def test_every_jig_set_fits(self):
        """Both sides' jig sets and the paste nest pass the fit check, the bottom set's parts included (the check
        works in the turned-over board's frame, like the jigs)."""
        from pcbtestkit.jig import build_jigs, jig_of
        jig = build_jigs(self.design())
        self.assertTrue(jig["fit"]["ok"], jig["fit"]["summary"])
        self.assertEqual(sorted(jig["fit"]["touching"]), sorted(jig["parts_to_place"]))
        bot = jig_of(jig, "hand_bottom")
        self.assertTrue(bot["fit"]["ok"], bot["fit"]["summary"])
        self.assertEqual(bot["fit"]["touching"], ["J7"])
        self.assertEqual(sorted(bot["fit"]["side_play"]), ["J7"])
        self.assertTrue(jig_of(jig, "nest_top")["fit"]["ok"])
        # the bottom set is really checked: its pocket made 0.2 mm too shallow is found
        tight = jig_of(build_jigs(self.design(jig={"hold_depth_offset_mm": -0.2})), "hand_bottom")["fit"]
        self.assertAlmostEqual(tight["holding_collisions"].get("J7", 0), 0.2, delta=0.012)

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
        self.assertIn("slide a board into each of the 2 nests and close its clamp", g)

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
        self.assertAlmostEqual(j.x - ox, 55.87, delta=0.011)            # pin 1 where the facility puts it (the
        self.assertAlmostEqual(j.y - oy, 114.0, delta=0.011)            # outline is reported to 0.01 mm)
        self.assertTrue(fx["plate_fit"])
        self.assertTrue(any("nests are turned" in w for w in fx["warnings"]), fx["warnings"])
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


class TestNest(Base):
    """Every board's nest (rails, end stop, sprung standoffs) and toggle clamp on the fixture."""

    def plan_and_nest(self, **over):
        from pcbtestkit.nest import plan_nest
        from pcbtestkit.pipeline import analyze
        d = self.design(**over)
        _f, plan = analyze(d, include_kicad=False)
        return d, plan, plan_nest(d, plan)

    def test_heights_and_parts_are_consistent(self):
        from pcbtestkit import nest as N
        d, plan, nest = self.plan_and_nest()
        self.assertTrue(nest["enabled"])
        self.assertEqual((nest["style"], nest["entry"], nest["clamp"]), ("slide", "front", "toggle"))
        H = nest["heights"]
        clear = float(plan["pogo"]["probe_clearance_mm"])
        self.assertAlmostEqual(H["work"], clear)
        self.assertAlmostEqual(H["tips"], clear + N.PRELOAD)
        self.assertAlmostEqual(H["rest"], H["tips"] + H["gap"], places=2)
        self.assertAlmostEqual(H["travel"], H["rest"] - H["work"], places=2)
        self.assertGreaterEqual(H["gap"], 1.0)                            # the board slides in above the tips
        self.assertGreaterEqual(H["ledge"], 2.0)
        self.assertGreaterEqual(H["bottom_work"] + 1e-6, nest["spring"]["solid_mm"] + 0.5)   # springs never go solid
        self.assertIn(H["standoff"], N.STANDOFFS)
        self.assertGreaterEqual(H["standoff"], H["bottom_rest"] + H["wall"])
        self.assertLess(H["bottom_rest"] + 0.5, nest["spring"]["free_mm"])   # the springs lift it to the washers
        if nest["gap_reason"]:                                            # a lead or part sweeps over a pin
            self.assertGreaterEqual(H["gap"], N.LEAD_MM + 0.3 - 1e-6)
        # standoffs and clamp screws stay clear of the board and its pins; fingers press on bare board
        bb = d.board.outline_bbox()
        for x, y in nest["posts"] + nest["adapter_holes"]:
            self.assertFalse(bb.grow(0.5).contains(x, y), (x, y))
            for p in plan["probes"]:
                self.assertGreater(((x - p["x"]) ** 2 + (y - p["y"]) ** 2) ** 0.5, 5.0)
        self.assertGreaterEqual(len(nest["fingers"]), 3)
        other = [fp for fp in d.components() if fp.side != plan["probe_side"]]
        for x, y in nest["fingers"]:
            self.assertTrue(bb.contains(x, y))
            for fp in other:
                self.assertFalse(fp.courtyard_bbox().grow(0.5).contains(x, y), fp.ref)
        self.assertGreaterEqual(H["finger"], max(d.height_of(fp)[0] for fp in other) + 0.9)
        # the three printed parts: watertight, the pins and every part that sweeps in stay free
        out = os.path.join(self.tmp, "nest_parts")
        os.makedirs(out, exist_ok=True)
        res = N.write_parts(nest, out, d.board.name)
        for tag in ("nest", "press", "adapter"):
            self.assertTrue(res["mesh"][tag]["watertight"], tag)
            self.assertGreater(res["mesh"][tag]["volume_mm3"], 100.0, tag)
            self.assertTrue(os.path.exists(os.path.join(out, res["files"][tag]["stl"])))
            self.assertTrue(os.path.exists(os.path.join(out, res["files"][tag]["preview_png"])))
        for f in ("nest", "post", "clamp"):
            self.assertTrue(os.path.exists(os.path.join(out, "pcbtestkit-models", res["models"][f])), f)
        g = res["_grids"]["nest"]

        def at(s, t):
            i, j = int((s - g["s0"]) / g["res"]), int((t - g["t0"]) / g["res"])
            return g["H"][j][i] if 0 <= i < g["nx"] and 0 <= j < g["ny"] else 0
        for ps, pt in nest["pins_local"]:
            self.assertEqual(at(ps, pt), 0)
        L = nest["local"]
        for s0, t0, s1, t1, _h in nest["under_local"]:            # nothing stands where a part slides through
            for s in (s0 - L["lead"] + 0.5, (s0 + s1) / 2.0, s1 - 0.3):
                self.assertEqual(at(s, (t0 + t1) / 2.0), 0, (s, t0, t1))
        ledges = sum(1 for row in g["H"] for v in row if v == int(round(nest["heights"]["ledge"] * 100)))
        self.assertGreater(ledges * g["res"] ** 2, 40.0)            # the board rests on ledges
        self.assertGreater(at(L["s_lo"] + 2.0, L["t_in"] + 2.0), 0)  # the rails run on into the lead-in
        self.assertEqual(at(0.0, 0.0), 0)                            # open in the middle for the pins

    def test_fixture_gets_holes_models_and_no_guide_pins(self):
        d, plan, _n = self.plan_and_nest()
        fx = build_fixture(d, plan)
        self.assertEqual(len(fx["nest_refs"]), 1)
        refs = fx["nest_refs"][0]
        self.assertEqual((len(refs["posts"]), len(refs["adapter"])), (4, 4))
        text = fx["kicad_pcb"]
        self.assertEqual(text.count('(footprint "PCBTestKit:NestPost_M3"'), 4)
        self.assertEqual(text.count('(footprint "PCBTestKit:ClampScrew_M3"'), 4)
        self.assertIn("pcbtestkit-models/nest_sample_sensor.wrl", text)
        self.assertIn("pcbtestkit-models/clamp_sample_sensor.wrl", text)
        self.assertNotIn('"GUIDE', text)                                     # the rails locate the board
        self.assertFalse([w for w in fx["warnings"] if "guide-pin" in w or "nest" in w], fx["warnings"])
        o = fx["outline"]
        for x, y in fx["nest"]["posts"] + fx["nest"]["adapter_holes"]:
            self.assertTrue(o["x_min"] + 4 < x < o["x_max"] - 4 and o["y_min"] + 4 < y < o["y_max"] - 4)
        self.assertEqual((fx["routing"] or {}).get("unrouted_connections", 0), 0)
        # tracks may pass under a nest post on the bottom layer, under the solder mask: no opening on B.Mask
        import re as _re
        posts = _re.findall(r'\(footprint "PCBTestKit:NestPost_M3".*?\(layers ([^)]*)\)', text, _re.S)
        self.assertEqual(len(posts), 4)
        for layers in posts:
            self.assertNotIn("*.Mask", layers)
            self.assertNotIn("B.Mask", layers)
        from pcbtestkit.kicad_cli import find_kicad_cli, run_drc
        cli = find_kicad_cli()
        if cli:                                                              # KiCad's own DRC on the fixture
            tmp = tempfile.mkdtemp(prefix="pcbtk_drc_")
            try:
                pcb = os.path.join(tmp, "fx.kicad_pcb")
                with open(pcb, "w", encoding="utf-8") as fh:
                    fh.write(text)
                r = run_drc(cli, pcb, parity=False) or {}
                kinds = {v.get("type") for v in r.get("violations") or []}
                self.assertFalse(kinds & {"solder_mask_bridge", "clearance", "hole_clearance", "shorting_items",
                                          "tracks_crossing", "copper_edge_clearance"}, kinds)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        # nests off: guide pins as before
        d2 = self.design(fixture={"nest": {"enabled": False}})
        fx2 = build_fixture(d2, build_testplan(d2, run_analysis(d2, include_kicad=False)))
        self.assertFalse(fx2["nest"]["enabled"])
        self.assertNotIn("NestPost_M3", fx2["kicad_pcb"])
        self.assertLess(fx2["outline"]["height"], o["height"])

    def test_a_nest_and_clamp_per_board_of_a_batch(self):
        from pcbtestkit.nest import BOSS_R
        d = self.design(batch={"count": 3})
        plan = build_testplan(d, run_analysis(d, include_kicad=False))
        fx = build_fixture(d, plan)
        self.assertEqual([r["site"] for r in fx["nest_refs"]], [1, 2, 3])
        self.assertEqual(fx["kicad_pcb"].count('(footprint "PCBTestKit:Nest"'), 3)
        posts = []
        for st in fx["sites"]:
            dx, dy = st["offset"]
            posts += [(x + dx, y + dy) for x, y in fx["nest"]["posts"]]
        for i, a in enumerate(posts):                                     # no two bosses overlap
            for b in posts[i + 1:]:
                self.assertGreater(((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5, 2 * BOSS_R)
        self.assertFalse([w for w in fx["warnings"] if "mounting hole" in w], fx["warnings"])

    def test_entries_styles_and_when_there_is_no_nest(self):
        from pcbtestkit.nest import ENTRY, Frame
        for e, (sv, tv) in ENTRY.items():                                  # proper rotations: no mirrored nests
            self.assertAlmostEqual(sv[0] * tv[1] - sv[1] * tv[0], 1.0)
            fr = Frame(e, (10.0, 20.0))
            x, y = fr.fix(3.0, -4.0)
            self.assertEqual(tuple(round(v, 6) for v in fr.loc(x, y)), (3.0, -4.0))
        ext = {}
        for e in ("front", "back", "left", "right"):
            _d, _p, n = self.plan_and_nest(fixture={"nest": {"entry": e}})
            ext[e] = n["extents"]
        self.assertGreater(ext["front"]["top"], ext["front"]["bottom"])     # clamp behind, lead-in in front
        self.assertGreater(ext["back"]["bottom"], ext["back"]["top"])
        self.assertGreater(ext["left"]["right"], ext["left"]["left"])
        self.assertGreater(ext["right"]["left"], ext["right"]["right"])
        _d, _p, drop = self.plan_and_nest(fixture={"nest": {"style": "drop"}})
        self.assertEqual((drop["style"], drop["local"]["lead"]), ("drop", 0.0))
        self.assertAlmostEqual(drop["heights"]["gap"], 1.0)
        _d, _p, top = self.plan_and_nest(testplan={"probe_side": "top"})
        self.assertTrue(top["enabled"])
        for over, why in (({"testplan": {"probe_side": "both"}}, "both sides"), ({"fixture": {"outline": "outline"}}, "outline"),
                          ({"fixture": {"nest": {"enabled": False}}}, "enabled")):
            _d, _p, n = self.plan_and_nest(**over)
            self.assertFalse(n["enabled"])
            self.assertIn(why, n["reason"])
        _d, _p, low = self.plan_and_nest(testplan={"probe_clearance_mm": 4.0})
        self.assertTrue(any("probe clearance" in w for w in low["warnings"]))

    def test_guide_scenes_and_animation(self):
        from pcbtestkit import animate as A
        from pcbtestkit.nest import clamp_shapes, scene_text
        from pcbtestkit.visuals import Renderer, write_wrl
        out = os.path.join(self.tmp, "nest_out")
        res = run_pipeline(self.design(), out_dir=out, include_kicad=False, outputs={"visuals": False, "video": False})
        for k in ("nest_stl", "nest_press_stl", "nest_adapter_stl", "nest_preview"):
            self.assertTrue(os.path.exists(res["paths"][k]), k)
        with open(res["paths"]["guide"], encoding="utf-8") as fh:
            guide = fh.read()
        self.assertIn("Board nest and clamp", guide)
        self.assertIn("vertical toggle clamp", guide)
        self.assertIn("slide it into its nest", guide)
        with open(res["paths"]["fixture_meta"], encoding="utf-8") as fh:
            meta = json.load(fh)
        self.assertTrue(meta["nest"]["enabled"])
        self.assertNotIn("under_local", meta["nest"])                      # a summary, not the geometry
        self.assertTrue(all(m["watertight"] for m in meta["nest"]["mesh"].values()))
        with open(res["paths"]["fixture"], encoding="utf-8") as fh:
            fx_text = fh.read()
        d, plan, nest = self.plan_and_nest()
        sd = os.path.join(self.tmp, "nest_scene")
        down = scene_text(fx_text, nest, d.board.name, sd, True, 0.0)
        self.assertNotIn("pcbtestkit-models/nest_sample_sensor.wrl", down)
        self.assertIn("nest_sample_sensor_down_0.wrl", down)
        self.assertEqual(len(load_board_text(down).footprints), len(load_board_text(fx_text).footprints))
        z = lambda sh: max(v[2] for s_ in sh for v in s_[0])                # noqa: E731
        self.assertGreater(z(clamp_shapes(nest, 95.0)), z(clamp_shapes(nest, 0.0)) + 20.0)   # open: arm and plate up
        ghost = os.path.join(sd, "ghost.wrl")
        write_wrl(ghost, clamp_shapes(nest, 0.0, ghost=True))
        with open(ghost) as fh:
            self.assertIn("transparency 0.72", fh.read())
        # the explainer's press shot: down onto the lead-in, back to the stop, clamp shut, nest down
        rd = Renderer(os.path.join(self.tmp, "nest_vis"), 320, 200, quality="basic")
        with open(res["paths"]["testplan"], encoding="utf-8") as fh:
            tplan = json.load(fh)
        st = A.Stage(d, tplan, meta, None, {"fixture": res["paths"]["fixture"]}, rd, os.path.join(self.tmp, "nest_anim"), fps=4)
        st.dut_wrl = lambda *a, **k: "dut.wrl"
        sh = st.shot_press()
        self.assertEqual(sh.meta.get("nest"), "slide")
        first, last = sh.jobs[0]["make"]("x")[0], sh.jobs[-1]["make"]("x")[0]
        nref = st._fx_nest[0]
        self.assertIn("CLAMP1", first)
        self.assertEqual(len(st._fx_pogo), len(plan["probes"]))
        travel = nest["heights"]["travel"]
        blk = lambda txt: txt[txt.index('"%s"' % nref):]                    # noqa: E731
        self.assertIn("%.5f" % -travel, blk(last)[:4000])                  # the nest pressed down at the end
        self.assertIn("(hide yes)", last)                                  # the fixture's own (open) clamp hidden
        # the station video moves nests and clamps itself: its fixture export has their models hidden
        from pcbtestkit.station_video import _without_nests
        copy = _without_nests(res["paths"]["fixture"])
        try:
            with open(copy, encoding="utf-8") as fh:
                hidden = fh.read()
            self.assertEqual(hidden.count("(hide yes)") - fx_text.count("(hide yes)"), 2)   # nest + clamp, not the posts
            self.assertEqual(os.path.dirname(copy), os.path.dirname(res["paths"]["fixture"]))
        finally:
            os.remove(copy)

    def test_set_fixture_options_for_nests(self):
        proj = os.path.join(self.tmp, "nest_opts")
        make_sample.main(proj)
        ws = Workspace(os.path.join(proj, "sample_sensor.kicad_pcb"))
        r = ws.call("set_fixture_options", {"nest_entry": "left", "nest_clamp_holes_mm": [24, 18], "nest_tip_gap_mm": 1.5,
                                            "confirm": True})
        self.assertTrue(r["ok"], r)
        with open(os.path.join(proj, "pcbtestkit.json")) as fh:
            self.assertEqual(json.load(fh)["fixture"]["nest"], {"entry": "left", "clamp_holes_mm": [24.0, 18.0],
                                                                 "tip_gap_mm": 1.5})
        for bad in ({"nest_style": "sideways"}, {"nest_clamp_holes_mm": [24]}, {"nest_tip_gap_mm": 9}):
            r = ws.call("set_fixture_options", dict(bad, dry_run=True))
            self.assertFalse(r["ok"], bad)


class TestStation(unittest.TestCase):
    """A test station: one tester, a fixture per board type, every board of a batch on its own inputs, supply and bus."""

    @classmethod
    def setUpClass(cls):
        import make_station
        cls.tmp = tempfile.mkdtemp(prefix="pcbtk_st_")
        cls.station = make_station.main(os.path.join(cls.tmp, "station"))
        cls.proj = os.path.join(cls.tmp, "sample_sensor")
        make_sample.main(cls.proj)
        cls.proj2 = os.path.join(cls.tmp, "second", "sample_sensor")     # the same board again: two folders
        make_sample.main(cls.proj2)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def fac(self):
        from pcbtestkit.facility import facility_from_pcb
        fac, warns = facility_from_pcb(self.station)
        self.assertEqual(warns, [])
        return fac

    def test_bus_pin_grammar(self):
        from pcbtestkit.facility import bus_function, bus_pin, is_pin_function
        for fn, want in (("CAN_H", ("CAN_H", 1)), ("CAN2_H", ("CAN_H", 2)), ("CAN1_L", ("CAN_L", 1)),
                         ("UART2_TX", ("UART_TX", 2)), ("I2C3_SDA", ("I2C_SDA", 3)), ("RS485_2_A", ("RS485_A", 2))):
            self.assertEqual(bus_pin(fn), want, fn)
            self.assertEqual(bus_pin(bus_function(*want)), want)
        for fn in ("AIN3", "VOUT2", "DIO8", "GND", "NC", "CAN2_H", "I2C4_SCL"):
            self.assertTrue(is_pin_function(fn), fn)
        for fn in ("AIN", "CAN_X", "VOUT", "FOO1"):
            self.assertFalse(is_pin_function(fn), fn)

    def test_station_from_its_pcb(self):
        from pcbtestkit import facility as F
        fac = self.fac()
        self.assertTrue(F.validate(fac)["ok"], F.validate(fac))
        c = fac["fixture"]["connectors"][0]
        self.assertTrue(c.get("shared"))
        fns = list(c["pins"].values())
        self.assertEqual(sum(1 for f in fns if f.startswith("AIN")), 32)
        self.assertEqual(sum(1 for f in fns if f.startswith("VOUT")), 4)
        self.assertEqual(fns.count("GND"), 12)
        self.assertIn("PinHeader", c.get("footprint", ""))                # the mate of the station's socket
        self.assertEqual([round(v) for v in fac["fixture"]["plate_mm"]], [260, 190])
        holes = sorted((round(h["at_mm"][0]), round(h["at_mm"][1])) for h in fac["fixture"]["holes"])
        self.assertEqual(holes, [(5, 5), (5, 185), (255, 5), (255, 185)])
        cap = F.capacity(fac)
        self.assertEqual((cap["inputs"], cap["supplies"]), (32, 4))
        self.assertEqual({k: cap["buses"][k] for k in ("can", "uart", "i2c")}, {"can": 2, "uart": 2, "i2c": 4})
        self.assertEqual(F.analog_limits(fac, 4)[0], 8)

    def test_shared_pins_and_limits(self):
        from pcbtestkit import facility as F
        fac = self.fac()
        buses = [{"bus": "can", "via": "fixture", "signals": ["CAN_H", "CAN_L"]}]
        conns = F.assign_pins(fac, ["CH%02d" % i for i in range(1, 7)], True, buses, sites=2)
        pins = conns[0]["pins"]
        self.assertTrue(conns[0]["shared"])
        used = [q for q in pins if q.get("site")]
        self.assertEqual(len({q["pin"] for q in used}), len(used))          # no pin twice
        by = {(q["site"], q["signal"]): q["function"] for q in used}
        self.assertEqual(by[(1, "CH01")], "AIN1")
        self.assertEqual(by[(2, "CH01")], "AIN7")
        self.assertEqual((by[(1, "VIN")], by[(2, "VIN")]), ("VOUT1", "VOUT2"))
        self.assertEqual((F.bus_pin(by[(1, "CAN_H")]), F.bus_pin(by[(2, "CAN_L")])), (("CAN_H", 1), ("CAN_L", 2)))
        self.assertTrue(all(q["site"] is None for q in pins if q["signal"] == "GND"))
        cap = F.capacity(fac)
        for need, n, limit in (({"channels": 6, "supply": True, "buses": []}, 4, "supplies"),
                               ({"channels": 12, "supply": True, "buses": []}, 2, "inputs"),
                               ({"channels": 2, "supply": True, "buses": ["uart"]}, 2, "uart buses"),
                               ({"channels": 0, "supply": False, "buses": []}, None, None)):
            r = F.max_sites(cap, need)
            if n is not None:
                self.assertEqual((r["count"], r["limit"]), (n, limit), need)
            else:
                self.assertGreaterEqual(r["count"], 1)

    def test_runner_gives_every_board_its_own_lines(self):
        from unittest import mock
        from pcbtestkit.runner.hw import facility_inputs
        plan = {"facility": {"connectors": [{"name": "J1", "site": 1, "shared": True, "sites": 2, "pins": [
            {"pin": 3, "function": "AIN1", "signal": "CH01", "site": 1}, {"pin": 4, "function": "AIN2", "signal": "CH01", "site": 2},
            {"pin": 37, "function": "VOUT1", "signal": "VIN", "site": 1}, {"pin": 38, "function": "VOUT2", "signal": "VIN", "site": 2},
            {"pin": 45, "function": "CAN2_H", "signal": "CAN_H", "site": 2}, {"pin": 1, "function": "GND", "signal": "GND", "site": None}]}]}}
        self.assertEqual(facility_inputs(plan, 2), {"CH01": "AIN2", "VIN": "VOUT2", "CAN_H": "CAN2_H"})
        self.assertEqual(facility_inputs(plan, 1), {"CH01": "AIN1", "VIN": "VOUT1"})
        drv_path = os.path.join(ROOT, "pcbtestkit", "facilities", "bench-tester-example", "driver.py")
        import importlib.util
        spec = importlib.util.spec_from_file_location("bench_driver", drv_path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        sent = []
        sock = mock.MagicMock()
        sock.sendall.side_effect = lambda b: sent.append(b.decode().strip())
        sock.recv.return_value = b"OK\n"
        with mock.patch("socket.create_connection", return_value=sock):
            drv = m.Driver({"commands": {"bus": "BUS:{bus}{inst}:TEST?"}}, {})
        drv.select_site(2, facility_inputs(plan, 2))
        drv.power(True)
        self.assertEqual(sent[-1], "OUTP2 ON")                               # board 2's own supply
        ok, _note = drv.bus_test({"bus": "can", "signals": ["CAN_H", "CAN_L"]})
        self.assertTrue(ok)
        self.assertEqual(sent[-1], "BUS:CAN2:TEST?")                         # ...and its own bus
        for line, want in (("CAN2_H", "2"), ("I2C3_SDA", "3"), ("RS485_2_A", "2"), ("CAN_H", "1"), ("", "1")):
            self.assertEqual(m._instance(line), want, line)

    def test_plan_and_run_the_station(self):
        from pcbtestkit.runner.hw import facility_inputs
        from pcbtestkit.station import plan_station, run_station
        st = plan_station(self.station, [self.proj, self.proj2], batches={"sample_sensor": 2})
        self.assertEqual(len(st["boards"]), 2)
        self.assertEqual([b["folder"] for b in st["boards"]], ["sample_sensor-1", "sample_sensor-2"])
        b = st["boards"][0]
        self.assertEqual((b["status"], b["batch"]), ("ok", 2))
        self.assertEqual(b["inputs_used"], 2 * b["channels"])
        out = os.path.join(self.tmp, "run")
        res = run_station(self.station, [self.proj], out, batches={"sample_sensor": 2}, visuals=False, route=False)
        for f in ("station.json", "station.html"):                            # shared files: no local paths
            with open(os.path.join(out, f), encoding="utf-8") as fh:
                txt = fh.read()
            self.assertIn("sample_sensor", txt)
            self.assertNotIn(self.tmp, txt, f)
        board = res["boards"][0]
        self.assertEqual(board["status"], "ok", board)
        fac = self.fac()
        bdir = os.path.join(out, board["folder"])
        with open(glob.glob(os.path.join(bdir, "*-testplan.json"))[0], encoding="utf-8") as fh:
            plan = json.load(fh)
        self.assertEqual(facility_inputs(plan, 1)["VIN"], "VOUT1")
        self.assertEqual(facility_inputs(plan, 2)["VIN"], "VOUT2")
        self.assertNotEqual(facility_inputs(plan, 1)["CH01"], facility_inputs(plan, 2)["CH01"])
        # the fixture's connector sits exactly where the station's socket is, every board on its own nets
        from pcbtestkit.facility import pin_xy
        pcb = load_board(glob.glob(os.path.join(bdir, "*-fixture.kicad_pcb"))[0])
        conn = [fp for fp in pcb.footprints if len(fp.pads) == 80][0]
        sp = load_board(self.station)
        sj = [fp for fp in sp.footprints if fp.ref == "J1"][0]
        ob, sb = pcb.outline_bbox(), sp.outline_bbox()
        for pd in conn.pads:
            spd = [q for q in sj.pads if q.number == pd.number][0]
            self.assertAlmostEqual(pd.x - ob.x0, spd.x - sb.x0, places=2)
            self.assertAlmostEqual(pd.y - ob.y0, spd.y - sb.y0, places=2)
        nets = {pd.number: pd.net for pd in conn.pads}
        by_fn = {fn: nets.get(num) for num, fn in fac["fixture"]["connectors"][0]["pins"].items()}
        self.assertEqual(by_fn["VOUT1"], "S1_VIN")
        self.assertEqual(by_fn["VOUT2"], "S2_VIN")
        self.assertEqual(by_fn["GND"], "GND")
        self.assertIn(by_fn["VOUT3"] or "", ("", None))                     # a third supply nobody uses
        # every board has its own nest and clamp on the station's plate
        with open(glob.glob(os.path.join(bdir, "*-fixture.json"))[0], encoding="utf-8") as fh:
            meta = json.load(fh)
        self.assertEqual(len(meta["nest"]["refs"]), 2)
        self.assertEqual([round(v) for v in (meta["outline"]["width"], meta["outline"]["height"])], [260, 190])
        r = subprocess.run([sys.executable, os.path.join(bdir, "runner", "pcbtest_runner.py"), "test", "--simulate",
                            "--non-interactive", "--serial", "S1"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_cli_station_and_from_pcb(self):
        out = os.path.join(self.tmp, "fac.json")
        r = subprocess.run([sys.executable, "-m", "pcbtestkit", "facilities", "from-pcb", self.station, "--out", out],
                           capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(out) as fh:
            fac = json.load(fh)
        self.assertEqual(len(fac["fixture"]["connectors"][0]["pins"]), 80)
        r = subprocess.run([sys.executable, "-m", "pcbtestkit", "station", "plan", self.station, self.proj, "--json"],
                           capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout)["boards"][0]["status"], "ok")
        small = dict(fac, id="tiny-station")                               # a plate too small for the board
        small["fixture"] = dict(fac["fixture"], plate_mm=[60.0, 40.0], holes=[])
        small["fixture"]["connectors"] = [dict(fac["fixture"]["connectors"][0], at_mm=[2.0, 36.0])]
        tiny = os.path.join(self.tmp, "tiny.json")
        with open(tiny, "w") as fh:
            json.dump(small, fh)
        r = subprocess.run([sys.executable, "-m", "pcbtestkit", "station", "plan", tiny, self.proj, "--json"],
                           capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout)["boards"][0]["status"], "does not fit")


def _fake_profile(name, ch=4, sup=True, v=5.0, buses=(), site=(70.0, 90.0), issues=(), prio=1.0):
    return {"name": name, "folder": name, "pcb": "/x/%s.kicad_pcb" % name, "status": "ok", "issues": list(issues),
            "channels": ch, "buses": list(buses), "supply": {"needed": sup, "voltage": v, "current_a": 0.5},
            "programming": [{"protocol": "swd"}], "probe_side": "bottom", "probes": ch + 2,
            "site": {"front": list(site), "left": [site[1], site[0]]}, "spacing_mm": 3.0, "priority": prio,
            "size_mm": [site[0] - 30, site[1] - 60]}


class TestEnvironmentDesign(unittest.TestCase):
    """The environment designer on made-up boards: limits, pins, stations, assignment, ranking (no KiCad files)."""

    def spec(self, **over):
        from pcbtestkit.environment import load_spec
        return load_spec(None, over)

    def test_spec_defaults_and_validation(self):
        from pcbtestkit.environment import default_spec, load_spec, validate_spec
        self.assertEqual(validate_spec(default_spec()), [])
        bad = default_spec()
        bad.update(max_stations=0, plate_mm=[10, 10], limits={"analog_inputs": 2000, "warp": 1}, priorities={"a": -1})
        errs = " | ".join(validate_spec(bad))
        for frag in ("max_stations", "plate_mm[0]", "limits.analog_inputs", "limits.warp", "priorities.a"):
            self.assertIn(frag, errs)
        with self.assertRaises(ValueError):
            load_spec(None, {"max_batch": 0})
        s = load_spec(None, {"limits": {"can": 1}, "priorities": {"x": 3}})
        self.assertEqual((s["limits"]["can"], s["limits"]["uart"], s["priorities"]), (1, 4, {"x": 3}))   # merged, not replaced

    def test_design_respects_limits_and_pins(self):
        from pcbtestkit import environment as E
        from pcbtestkit.station_board import socket_pins
        spec = self.spec(limits={"analog_inputs": 32, "supplies": 4, "can": 2, "uart": 2, "i2c": 2, "rs485": 1, "usb": 1,
                                 "ethernet": 1}, large_plate_mm=[])
        profs = [_fake_profile("s%d" % i, ch=5) for i in range(6)] + \
                [_fake_profile("can%d" % i, ch=6, v=24.0, buses=["can"]) for i in range(3)] + \
                [_fake_profile("eth", ch=8, v=12.0, buses=["ethernet", "usb"], site=(150, 160))]
        dz = E.design(profs, spec)
        self.assertEqual(len(dz["stations"]), 1)                       # one station takes them all
        m = dz["stations"][0]
        r = m["res"]
        self.assertLessEqual(r["ain"], 32)
        self.assertEqual(r["ain"] % 4, 0)                              # whole 4-channel ADCs
        self.assertLessEqual(r["vout"], 4)
        for bus, n in r["buses"].items():
            self.assertLessEqual(n, spec["limits"][bus])
        socks = socket_pins(r, 6, 80)
        self.assertLessEqual(len(socks), 2)
        self.assertTrue(all(len(s_) <= 80 for s_ in socks))
        where = E.assign(profs, dz["stations"], dz["estimates"])
        self.assertTrue(all(v == 0 for v in where.values()))
        est = {p["folder"]: dz["estimates"][p["folder"]][0] for p in profs}
        self.assertGreaterEqual(est["s0"]["batch"], 2)
        self.assertEqual(est["eth"]["batch"], 1)
        self.assertLessEqual(est["can0"]["batch"], 2)                  # 2 CAN instances at most

    def test_further_stations_and_hard_limits(self):
        from pcbtestkit import environment as E
        spec = self.spec(plate_mm=[260, 190], large_plate_mm=[420, 320])
        profs = [_fake_profile("small%d" % i) for i in range(4)] + \
                [_fake_profile("huge", ch=12, site=(300, 250))] + \
                [_fake_profile("hv", v=48.0), _fake_profile("both", issues=["probed from both sides"])]
        dz = E.design(profs, spec)
        self.assertEqual(len(dz["stations"]), 2)                       # the huge board gets a large station
        self.assertEqual(dz["stations"][1]["plate"], [420.0, 320.0])
        where = E.assign(profs, dz["stations"], dz["estimates"])
        self.assertEqual(where["huge"], 1)
        self.assertIsNone(where["hv"])
        self.assertIn("48 V", E.hard_issue(profs[5], spec))
        self.assertIn("both sides", E.hard_issue(profs[6], spec))
        one = E.design(profs, self.spec(plate_mm=[260, 190], large_plate_mm=[420, 320], max_stations=1))
        self.assertEqual(len(one["stations"]), 1)
        nobig = E.design(profs, self.spec(plate_mm=[260, 190], large_plate_mm=[]))
        self.assertEqual(len(nobig["stations"]), 1)                    # no plate takes the huge board: no 2nd station
        est = E.estimate(profs[4], nobig["stations"][0], spec)
        self.assertEqual(est["batch"], 0)
        self.assertIn("300 x 250 mm", est["reason"])

    def test_priorities_rank_first(self):
        from pcbtestkit import environment as E
        boards = [{"name": n, "folder": n, "status": st, "priority": pr, "batch": b, "channels": 4,
                   "supply": {"needed": True, "voltage": 5.0}, "buses": [], "programming": []}
                  for n, st, pr, b in (("a", "compatible", 1.0, 8), ("b", "compatible", 5.0, 1), ("c", "incompatible", 9.0, 0),
                                       ("d", "compatible", 1.0, 2))]
        names = [r["name"] for r in E.rank(boards, E.default_spec())]
        self.assertEqual(names, ["b", "a", "d", "c"])                # priority, then batch; misfits last

    def test_station_board_round_trip_with_two_sockets(self):
        from pcbtestkit import facility as F
        from pcbtestkit.station_board import pin_count, socket_pins, write_station
        res = {"ain": 72, "vout": 8, "buses": {"can": 4, "uart": 4, "i2c": 2, "ethernet": 2, "usb": 1, "rs485": 1}, "dio": 8}
        socks = socket_pins(res, 6, 80)
        self.assertEqual(len(socks), 2)
        for s_ in socks:
            self.assertLessEqual(len(s_), 80)
            for i, f in enumerate(s_):                                  # a bus instance's lines stay side by side
                if f.endswith("_TX_P"):
                    self.assertEqual(s_[i + 1], f.replace("_TX_P", "_TX_N"))
        self.assertGreater(pin_count(res), sum(1 for _ in res))
        tmp = tempfile.mkdtemp(prefix="pcbtk_stb_")
        try:
            pcb = write_station({"name": "st2", "title": "Two sockets", "plate_mm": [320, 240], "resources": res}, tmp)
            fac, warns = F.facility_from_pcb(pcb)
            self.assertEqual(warns, [])
            self.assertEqual([c["name"] for c in fac["fixture"]["connectors"]], ["J1", "J2"])
            cap = F.capacity(fac)
            self.assertEqual((cap["inputs"], cap["supplies"], cap["dio"]), (72, 8, 8))
            self.assertEqual(cap["buses"], res["buses"])
            self.assertEqual(len(fac["fixture"]["holes"]), 6)              # mounting holes only (not the jacks' pegs)
            conns = F.assign_pins(fac, ["CH%02d" % i for i in range(1, 13)], True,
                                  [{"bus": "ethernet", "via": "fixture", "signals": ["ETH_TX_P", "ETH_TX_N", "ETH_RX_P", "ETH_RX_N"]}],
                                  sites=6)
            used = [(c["name"], q["pin"], q["site"], q["signal"]) for c in conns for q in c["pins"] if q.get("site")]
            self.assertEqual(len({(u[0], u[1]) for u in used}), len(used))   # every pin once across both sockets
            self.assertEqual(len({(u[2], u[3]) for u in used if u[3].startswith("CH")}), 6 * 12)
            eth = {(u[2], u[3]) for u in used if u[3].startswith("ETH")}
            self.assertEqual({k for k, _s in eth}, {1, 2})                   # two Ethernet instances: boards 1 and 2
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_discover_skips_outputs_and_reads_lists(self):
        from pcbtestkit.environment import discover
        tmp = tempfile.mkdtemp(prefix="pcbtk_disc_")
        try:
            for rel in ("a/a.kicad_pcb", "a/a.kicad_pro", "b/sub/b.kicad_pcb", "c/c.kicad_pcb", "c/c.kicad_pro",
                        "c/old.kicad_pcb", "out/x/x-fixture.kicad_pcb", "out/x/x.kicad_pcb", "a-backups/a.kicad_pcb",
                        "st/station.json", "st/b/b2.kicad_pcb"):
                p = os.path.join(tmp, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                open(p, "w").close()
            got, warns = discover([tmp])
            self.assertEqual(sorted(os.path.relpath(g, tmp) for g in got), ["a/a.kicad_pcb", "b/sub/b.kicad_pcb", "c/c.kicad_pcb"])
            lst = os.path.join(tmp, "boards.txt")
            with open(lst, "w") as fh:
                fh.write("# my boards\na/a.kicad_pro\nnope\nc\n")
            got, warns = discover(["@" + lst, os.path.join(tmp, "a", "a.kicad_pcb")])
            self.assertEqual(sorted(os.path.relpath(g, tmp) for g in got), ["a/a.kicad_pcb", "c/c.kicad_pcb"])
            self.assertTrue(any("nope" in w for w in warns))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestEnvironmentBuild(unittest.TestCase):
    """A small collection end to end: plan, build, build again (resume), keep the stations, the CLI."""

    @classmethod
    def setUpClass(cls):
        import make_collection
        cls.tmp = tempfile.mkdtemp(prefix="pcbtk_env_")
        cls.col = os.path.join(cls.tmp, "boards")
        make_collection.main(cls.col, count=5, seed=3)
        os.makedirs(os.path.join(cls.col, "hv"), exist_ok=True)
        b, cfg = make_collection.make_board("hv", [f for f in make_collection.FAMILIES if f[0] == "hv_driver"][0],
                                            __import__("random").Random(1))
        with open(os.path.join(cls.col, "hv", "hv.kicad_pcb"), "w") as fh:
            fh.write(b.text())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_plan_build_resume_and_keep(self):
        from pcbtestkit.environment import build_environment, load_spec
        out = os.path.join(self.tmp, "env")
        spec = load_spec(None, {"name": "Lab A"})
        env = build_environment([self.col], out, spec, jobs=1, visuals=False, route=False, video="none")
        s = env["summary"]
        self.assertEqual(s["boards"], 6)
        self.assertGreaterEqual(s["compatible"], 4)
        hv = [b for b in env["boards"] if b["name"] == "hv"][0]
        self.assertEqual(hv["status"], "incompatible")
        self.assertIn("48 V", hv["reason"])
        for f in ("index.html", "requirements.md", "ranking.csv", "boards.csv", "environment.json", "environment-spec.json"):
            self.assertTrue(os.path.exists(os.path.join(out, f)), f)
        with open(os.path.join(out, "environment.json"), encoding="utf-8") as fh:
            txt = fh.read()
        self.assertNotIn(self.tmp, txt)                                  # shared: no local paths
        data = json.loads(txt)
        st = data["stations"][0]
        self.assertTrue(os.path.exists(os.path.join(out, st["board"])))
        self.assertTrue(os.path.exists(os.path.join(out, st["page"])))
        for b in data["boards"]:
            if b["status"] == "compatible":
                self.assertTrue(os.path.exists(os.path.join(out, b["guide"])), b["name"])
                self.assertGreaterEqual(b["batch"], 1)
        with open(os.path.join(out, "requirements.md"), encoding="utf-8") as fh:
            req = fh.read()
        self.assertIn("Analog inputs", req)
        self.assertIn("needs a 48 V supply", req)
        with open(os.path.join(out, "ranking.csv"), encoding="utf-8") as fh:
            rows = list(__import__("csv").DictReader(fh))
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[-1]["board"], "hv")
        # again: every board's outputs are up to date and reused
        t0 = time.time()
        env2 = build_environment([self.col], out, spec, jobs=1, visuals=False, route=False, video="none")
        self.assertEqual(env2["summary"]["compatible"], s["compatible"])
        self.assertLess(time.time() - t0, 60.0)
        # a station board edited in KiCad since: the next build keeps it instead of designing it again
        pcb = os.path.join(out, st["board"])
        with open(pcb, encoding="utf-8") as fh:
            txt = fh.read()
        with open(pcb, "w", encoding="utf-8") as fh:
            fh.write(txt.replace("FIXTURE CONNECTOR J1", "FIXTURE CONNECTOR J1 (edited)"))
        env4 = build_environment([self.col], out, spec, jobs=1, visuals=False, route=False, video="none")
        self.assertTrue(env4["stations"][0]["fixed"])
        self.assertTrue(any("edited since the last build" in w for w in env4["warnings"]), env4["warnings"])
        with open(pcb, encoding="utf-8") as fh:
            self.assertIn("(edited)", fh.read())
        # an existing environment: its station is kept as it is (fixed), even when the boards change
        env3 = build_environment([self.col], os.path.join(self.tmp, "env2"), spec, environment=out, jobs=1, visuals=False,
                                 route=False, video="none")
        self.assertTrue(env3["stations"][0]["fixed"])
        self.assertEqual(env3["stations"][0]["resources"], data["stations"][0]["resources"])

    def test_cli(self):
        spec = os.path.join(self.tmp, "spec.json")
        r = subprocess.run([sys.executable, "-m", "pcbtestkit", "environment", "init", "--out", spec], capture_output=True,
                           text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(spec) as fh:
            self.assertEqual(json.load(fh)["max_stations"], 3)
        r = subprocess.run([sys.executable, "-m", "pcbtestkit", "environment", "plan", self.col, "--spec", spec, "--json",
                            "--jobs", "2"], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        env = json.loads(r.stdout)
        self.assertEqual(env["summary"]["boards"], 6)
        self.assertNotIn("_facilities", env)
        r = subprocess.run([sys.executable, "-m", "pcbtestkit", "environment", "plan", self.col, "--strict", "-q", "--jobs", "2"],
                           capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 1)                                 # hv does not fit
        self.assertIn("Do not fit", r.stdout)
        r = subprocess.run([sys.executable, "-m", "pcbtestkit", "environment", "build", self.col], capture_output=True,
                           text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 2)                                 # build needs --out


class TestEnvironmentReuse(unittest.TestCase):
    """A board made for an existing (remembered) test environment: its station, the largest safe batch or a fixed
    one, without designing anything new."""

    @classmethod
    def setUpClass(cls):
        import make_collection
        from unittest import mock
        from pcbtestkit.environment import build_environment, load_spec
        cls.tmp = tempfile.mkdtemp(prefix="pcbtk_reuse_")
        cls.home = mock.patch.dict(os.environ, {"PCBTESTKIT_HOME": os.path.join(cls.tmp, "home")})
        cls.home.start()
        col = os.path.join(cls.tmp, "made")
        make_collection.main(col, count=3, seed=5)
        cls.env = os.path.join(cls.tmp, "env")
        build_environment([col], cls.env, load_spec(None, {"name": "Lab B"}), jobs=1, visuals=False, route=False, video="none")
        cls.proj = os.path.join(cls.tmp, "sample_sensor")              # a board the environment was not made for
        make_sample.main(cls.proj)
        cls.hv = os.path.join(cls.tmp, "hv")
        os.makedirs(cls.hv)
        b, _cfg = make_collection.make_board("hv", [f for f in make_collection.FAMILIES if f[0] == "hv_driver"][0],
                                             __import__("random").Random(1))
        with open(os.path.join(cls.hv, "hv.kicad_pcb"), "w") as fh:
            fh.write(b.text())

    @classmethod
    def tearDownClass(cls):
        cls.home.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        from pcbtestkit import settings
        settings.remember_environment(None)

    def test_remembered_environment_auto_and_fixed_batch(self):
        from pcbtestkit import settings
        from pcbtestkit.environment import board_setup
        self.assertIsNone(Design.load(self.proj).environment)          # nothing remembered: as before
        settings.remember_environment({"path": self.env})
        d = Design.load(self.proj)
        env = d.environment
        self.assertTrue(env["ok"], env)
        self.assertEqual((env["environment"], env["station"]), ("env", "station-1"))
        self.assertTrue(env["auto"])
        self.assertGreaterEqual(env["batch"], 1)
        self.assertEqual(d.cfg["batch"]["count"], env["batch"])
        self.assertEqual(d.cfg["facility"]["id"], "station-1")       # the station's own board, read back
        self.assertNotIn("facility", d.user_config)                   # pcbtestkit.json is not changed
        # the largest batch: one more board would not fit what limits it
        from pcbtestkit.station import plan_board
        from pcbtestkit.environment import environment_stations
        (_name, fac), = environment_stations(self.env)[0]
        row = plan_board(d.board.path, fac, env["batch"] + 1)
        self.assertLessEqual(row["batch"], env["batch"])
        # a fixed batch, a station by name, none for this board
        d2 = Design.load(self.proj, overrides={"environment": {"batch": 1, "station": "station-1"}})
        self.assertEqual((d2.environment["batch"], d2.cfg["batch"]["count"], d2.environment["auto"]), (1, 1, False))
        d3 = Design.load(self.proj, overrides={"environment": {"path": "none"}})
        self.assertIsNone(d3.environment)
        self.assertNotEqual((d3.cfg.get("facility") or "raspberry-pi"), d.cfg["facility"])
        bad = Design.load(self.proj, overrides={"environment": {"station": "station-9"}})
        self.assertFalse(bad.environment["ok"])
        self.assertTrue(any("no station station-9" in w for w in bad.warnings), bad.warnings)
        # a board no station takes: told why, made without the environment
        hv = Design.load(self.hv)
        self.assertFalse(hv.environment["ok"])
        self.assertTrue(any("48 V" in w or "fits none" in w for w in hv.warnings), hv.warnings)
        self.assertEqual(hv.cfg["batch"]["count"], 1)
        # cached: instant, and a changed station board is read again
        t0 = time.time()
        Design.load(self.proj)
        self.assertLess(time.time() - t0, 2.0)                        # uncached: seconds (a real fixture layout)
        st_pcb = [f for f in glob.glob(os.path.join(self.env, "stations", "station-1", "*.kicad_pcb"))][0]
        with open(st_pcb, encoding="utf-8") as fh:
            txt = fh.read()
        with open(st_pcb, "w", encoding="utf-8") as fh:
            fh.write(txt.replace("FIXTURE CONNECTOR J1", "FIXTURE CONNECTOR J1 "))
        again = board_setup(d.board.path, {"path": self.env, "station": "auto", "batch": "auto"})
        self.assertTrue(again["ok"])
        with open(st_pcb, "w", encoding="utf-8") as fh:
            fh.write(txt)

    def test_outputs_and_cli(self):
        out = os.path.join(self.tmp, "run_out")
        home = {"PCBTESTKIT_HOME": os.environ["PCBTESTKIT_HOME"]}
        r = subprocess.run([sys.executable, "-m", "pcbtestkit", "environment", "use", self.env], capture_output=True, text=True,
                           cwd=ROOT, env=dict(os.environ, **home))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("every build is made for the test environment", r.stdout)
        r = subprocess.run([sys.executable, "-m", "pcbtestkit", "run", self.proj, "--out", out, "-q", "--no-visuals",
                            "--no-kicad", "--batch", "auto"], capture_output=True, text=True, cwd=ROOT, env=dict(os.environ, **home))
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        self.assertIn("test environment env: station-1", r.stdout)
        with open(glob.glob(os.path.join(out, "*-fixture.json"))[0], encoding="utf-8") as fh:
            meta = json.load(fh)
        with open(glob.glob(os.path.join(out, "*-testplan.json"))[0], encoding="utf-8") as fh:
            plan = json.load(fh)
        n = plan["batch"]["count"]
        self.assertEqual(len(meta["sites"]), n)
        self.assertEqual(meta["facility"]["id"], "station-1")
        self.assertTrue(meta["plate_fit"])
        self.assertEqual(len(meta["nest"]["refs"]), n)                 # a nest and clamp per board of the batch
        r = subprocess.run([sys.executable, "-m", "pcbtestkit", "run", self.proj, "--out", out + "2", "-q", "--no-visuals",
                            "--no-kicad", "--no-environment", "--no-jig"], capture_output=True, text=True, cwd=ROOT,
                           env=dict(os.environ, **home))
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        self.assertNotIn("test environment", r.stdout)
        r = subprocess.run([sys.executable, "-m", "pcbtestkit", "environment", "forget"], capture_output=True, text=True,
                           cwd=ROOT, env=dict(os.environ, **home))
        self.assertIn("no test environment is remembered", r.stdout)

    def test_agent_tool(self):
        from pcbtestkit import settings
        proj = os.path.join(self.tmp, "tool_proj")
        make_sample.main(proj)
        ws = Workspace(os.path.join(proj, "sample_sensor.kicad_pcb"))
        r = ws.call("set_test_environment", {"path": self.env, "batch": "auto", "remember": True, "confirm": True})
        self.assertTrue(r["ok"], r)
        self.assertTrue(any("station station-1" in w for w in r["warnings"]), r)
        with open(os.path.join(proj, "pcbtestkit.json")) as fh:
            self.assertEqual(json.load(fh)["environment"]["path"], os.path.abspath(self.env))
        self.assertEqual(settings.remembered_environment()["path"], os.path.abspath(self.env))
        r = ws.call("set_test_environment", {"path": os.path.join(self.tmp, "nowhere"), "dry_run": True})
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "NOT_FOUND")


class TestPower(unittest.TestCase):
    """Long runs: keep the computer awake, then sleep / hibernate / shut down. A dry run: nothing is really done."""

    def setUp(self):
        from unittest import mock
        self.tmp = tempfile.mkdtemp(prefix="pcbtk_power_")
        self.env = mock.patch.dict(os.environ, {"PCBTESTKIT_POWER_DRYRUN": "1",
                                                "PCBTESTKIT_HOME": os.path.join(self.tmp, "home")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_setting_and_commands(self):
        from pcbtestkit import power as P
        self.assertEqual(P.normalize("standby")["after"], "sleep")
        self.assertEqual(P.normalize({"after": "Shut down"})["after"], "shutdown")
        self.assertTrue(P.normalize({"lid": True, "keep_awake": False})["keep_awake"])     # the lid needs it awake
        with self.assertRaises(ValueError):
            P.normalize("reboot")
        with self.assertRaises(ValueError):
            P.normalize({"grace_s": "soon"})
        self.assertFalse(P.active(P.normalize({"keep_awake": False})))
        self.assertIn("hibernate the computer 2 min after the run has finished or failed", P.describe("hibernate"))
        self.assertEqual(P.commands("sleep", "darwin"), [["pmset", "sleepnow"]])
        self.assertEqual(P.commands("hibernate", "darwin"), [["pmset", "sleepnow"]])
        self.assertIn("System Events", P.commands("shutdown", "darwin")[0][2])
        self.assertEqual(P.commands("hibernate", "win32"), [["shutdown", "/h"]])
        self.assertEqual(P.commands("shutdown", "win32")[0][:2], ["shutdown", "/s"])
        self.assertIn("SetSuspendState", " ".join(P.commands("sleep", "win32")[0]))
        self.assertEqual(P.commands("shutdown", "linux"), [["systemctl", "poweroff"]])
        self.assertEqual(P.commands("none", "linux"), [])
        # the macOS lid helper only gets a pid and a plain path, and reads its decision as a word
        script = P.MAC_HELPER % {"flag": "/tmp/pcbtestkit-power-x/decision", "pid": 123, "low": P.LOW_BATTERY}
        self.assertIn("pmset -a disablesleep 0", script)
        self.assertNotIn("$d)", script.replace("\"$d\"", ""))
        self.assertEqual(subprocess.run(["sh", "-n", "-c", script]).returncode, 0)

    def test_session(self):
        import threading
        from pcbtestkit import power as P
        out = os.path.join(self.tmp, "env")
        ses = P.Session({"after": "sleep", "grace_s": 0}, out).start()
        self.assertEqual(P.status(out)["state"], "watching")
        self.assertEqual(ses.finish("done"), "sleep")
        st = P.status(out)
        self.assertEqual((st["state"], st["outcome"]), ("done", "done"))
        self.assertTrue(any("would run: " + " ".join(P.commands("sleep")[0]) in ln for ln in st["log"]), st["log"])
        ses = P.Session({"after": "shutdown", "grace_s": 0}, out).start()
        self.assertEqual(ses.finish("cancelled"), "none")                   # cancelled: the computer stays on
        self.assertFalse(any("would run" in ln for ln in P.status(out)["log"]))
        ses = P.Session({"after": "shutdown", "grace_s": 30}, out).start()
        threading.Timer(0.5, lambda: P.stay_on(out, 0)).start()             # "Stay on" during the countdown
        t = time.time()
        self.assertEqual(ses.finish("failed"), "kept-on")
        self.assertLess(time.time() - t, 10)
        self.assertEqual(P.status(out)["state"], "kept-on")
        ses = P.Session({"after": "sleep", "grace_s": 0}, out).start()      # an old stay-on does not count
        self.assertEqual(ses.finish("done"), "sleep")

    def test_watcher(self):
        import threading
        from pcbtestkit import environment_job as J
        from pcbtestkit import power as P
        out = os.path.join(self.tmp, "job")
        os.makedirs(out)

        def job_status(**st):
            with open(os.path.join(out, J.STATUS), "w", encoding="utf-8") as fh:
                json.dump(st, fh)
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        job_status(state="running", pid=dead.pid)                         # died without a word: it failed
        self.assertEqual(P.watch(out, dead.pid, {"after": "hibernate", "grace_s": 0}, poll_s=0.1), "hibernate")
        self.assertEqual(P.status(out)["outcome"], "failed")
        job = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            job_status(state="running", pid=job.pid)

            def finish():
                with open(os.path.join(out, "index.html"), "w") as fh:
                    fh.write("<html></html>")
                job_status(state="done", pid=job.pid, message="5 of 5 boards fit")
            threading.Timer(0.6, finish).start()
            self.assertEqual(P.watch(out, job.pid, {"after": "sleep", "grace_s": 0}, poll_s=0.1), "sleep")
            st = P.status(out)
            self.assertEqual((st["outcome"], st["reason"]), ("done", "5 of 5 boards fit"))
            self.assertEqual(st["job_pid"], job.pid)
            os.utime(os.path.join(out, "index.html"), (1000, 1000))           # "done" without this run's page
            self.assertEqual(P.job_outcome(out, job.pid, time.time()), "failed")
            job_status(state="cancelled", pid=job.pid)
            self.assertEqual(P.watch(out, job.pid, {"after": "shutdown", "grace_s": 0}, poll_s=0.1), "none")
        finally:
            job.kill()
            job.wait()

    def test_cli(self):
        from unittest import mock
        from pcbtestkit import cli
        from pcbtestkit import power as P
        out = os.path.join(self.tmp, "cli_env")
        os.makedirs(out)
        env = {"summary": {"compatible": 2, "boards": 2}}
        args = ["environment", "plan", "x.kicad_pcb", "--out", out, "--when-done", "sleep", "--grace", "0", "-q"]
        with mock.patch.object(cli, "_environment_run", return_value=env), \
                mock.patch.object(cli, "environment_text", return_value=""), \
                mock.patch.object(cli, "_env_spec", return_value={}):
            with open(os.path.join(out, "index.html"), "w") as fh:
                fh.write("<html></html>")
            self.assertEqual(cli.main(args), 0)
            st = P.status(out)
            self.assertEqual((st["outcome"], st["state"]), ("done", "done"))
            self.assertTrue(any("would run" in ln for ln in st["log"]))
        with mock.patch.object(cli, "_environment_run", side_effect=RuntimeError("boom")), \
                mock.patch.object(cli, "_env_spec", return_value={}):
            with self.assertRaises(RuntimeError):
                cli.main(args)
            self.assertEqual(P.status(out)["outcome"], "failed")
        with mock.patch.object(cli, "_environment_run", side_effect=KeyboardInterrupt), \
                mock.patch.object(cli, "_env_spec", return_value={}):
            with self.assertRaises(KeyboardInterrupt):
                cli.main(args)
            self.assertEqual(P.status(out)["outcome"], "cancelled")
        self.assertEqual(cli.main(["environment", "stay-on", out]), 0)
        self.assertTrue(os.path.exists(os.path.join(out, P.STAY)))
        with self.assertRaises(SystemExit):
            cli.main(["environment", "plan", "x", "--when-done", "reboot"])


def _fake_env(profs, models, spec):
    """An environment as environment.json has it, from fake profiles and station models (no fixture is built)."""
    from pcbtestkit import environment as E
    cache = {}
    ests = {p["folder"]: [E.estimate(p, m, spec, cache) for m in models] for p in profs}
    where = E.assign(profs, models, ests)
    rows = {}
    for p in profs:
        i = where[p["folder"]]
        if i is not None:
            e = ests[p["folder"]][i]
            rows[p["folder"]] = {"name": p["name"], "status": "ok", "batch": e["batch"], "limit": e["limit"],
                                 "station": models[i]["name"], "max_by": dict(e["by"])}
    env = E._environment(profs, models, ests, rows, spec, {m["name"]: {} for m in models},
                         {m["name"]: "/x/%s.kicad_pcb" % m["name"] for m in models}, [], "/x")
    env.pop("_facilities")
    return json.loads(json.dumps(E.shareable(env)))


class TestUpgrades(unittest.TestCase):
    """What limits every board, what an upgrade gains and costs, goals solved at the lowest cost."""

    def setUp(self):
        from pcbtestkit import environment as E
        self.E = E
        self.spec = E.load_spec(None, {"max_stations": 1, "max_batch": 8})
        self.profs = [_fake_profile("can_%d" % i, ch=2, buses=["can"], site=(60.0, 70.0)) for i in range(3)]
        self.profs += [_fake_profile("hv", ch=2, v=48.0, site=(60.0, 70.0)),
                       _fake_profile("dual", issues=["probed from both sides (two fixture plates)"]),
                       _fake_profile("analog", ch=20, site=(60.0, 70.0))]
        m = E.model_from_resources({"ain": 24, "vout": 4, "buses": {"can": 2}, "dio": 0}, [300.0, 220.0], self.spec,
                                   "station-1")
        self.env = _fake_env(self.profs, [m], self.spec)

    def test_constraints(self):
        from pcbtestkit import upgrades as U
        cons = {c["board"]: c for c in U.constraints(self.env)}
        self.assertEqual(cons["can_0"]["batch"], 2)
        self.assertIn("CAN instances", cons["can_0"]["limited_by"])
        self.assertTrue(any("1 more CAN" in t for t in cons["can_0"]["next"]["changes"]), cons["can_0"]["next"])
        self.assertGreater(cons["can_0"]["next"]["cost"], 0)
        self.assertEqual(cons["hv"]["to_fit"]["change"][0], {"kind": "supply_v", "to": 48.0})
        self.assertIn("no station change helps", cons["dual"]["to_fit"]["note"])
        self.assertEqual(cons["analog"]["batch"], 1)                          # 20 of 24 inputs
        self.assertIn("analog inputs", cons["analog"]["limited_by"])
        md = U.constraints_md(self.env, list(cons.values()))
        self.assertIn("| can_0 | station-1 | 2 |", md)
        self.assertIn("**CAN instances 2**", md)

    def test_options_and_paths(self):
        from pcbtestkit import upgrades as U
        opt = U.options(self.env)
        self.assertEqual([o["id"] for o in opt["options"]], ["U%d" % (i + 1) for i in range(len(opt["options"]))])
        can = [o for o in opt["options"] if o["change"].get("bus") == "can"]
        self.assertTrue(can and can[0]["gain"]["slots"] == 3, [o["text"] for o in opt["options"]])
        hv = [o for o in opt["options"] if o["change"]["kind"] == "supply_v"]
        self.assertEqual(hv[0]["gain"]["fit"], ["hv"])
        nets = [o["gain"]["net"] for o in opt["options"]]
        self.assertEqual(nets, sorted(nets, reverse=True))                    # best first
        for o in opt["options"]:
            self.assertAlmostEqual(o["gain"]["net"], o["gain"]["worth"] - o["gain"]["cost"], delta=1.0)
        self.assertEqual(set(opt["paths"]), {"value", "fit", "batch"})
        self.assertTrue(opt["paths"]["fit"]["steps"])
        md = U.upgrades_md(self.env, opt)
        self.assertIn("| U1 |", md)
        self.assertIn("--path value", md)

    def test_changes_and_goals(self):
        from pcbtestkit import upgrades as U
        chs = U.parse_changes("station-1:can+1,ain+5,plate=400x300; supply_v=48; max_batch=16")
        self.assertEqual([c["kind"] for c in chs], ["bus", "ain", "plate", "supply_v", "max_batch"])
        for bad in ("station-1:warp+1", "can+1", "station-1:plate=40", "station-1:can+x"):
            with self.assertRaises(ValueError):
                U.parse_changes(bad)
        ctx = U.Context(self.env)
        res = U.resolve(chs[:2], ctx.models, ctx.spec)
        self.assertEqual(res[0]["to"], 3)
        self.assertEqual(res[1]["to"], 32)                                    # 24 + 5, in groups of 4
        with self.assertRaises(ValueError):
            U.resolve([{"kind": "bus", "bus": "can", "station": "station-9", "add": 1}], ctx.models, ctx.spec)
        g = U.plan_goal(self.env, fit=["hv"], batch={"can_1": 3})
        self.assertEqual({c["kind"] for c in g["changes"]}, {"supply_v", "bus"})
        self.assertEqual(len(g["met"]), 2)
        self.assertIn("hv", g["gain"]["fit"])
        self.assertGreater(g["gain"]["cost"], 0)
        g = U.plan_goal(self.env, fit=["dual", "nosuch"], batch={"can_1": 3}, budget=1)
        self.assertEqual(g["unknown"], ["nosuch"])
        self.assertEqual({c["board"] for c in g["cannot"]}, {"dual", "can_1"})
        self.assertIn("over the budget", [c for c in g["cannot"] if c["board"] == "can_1"][0]["reason"])
        g = U.plan_goal(self.env, changes=U.parse_changes("station-1:can+2"))
        self.assertEqual(g["gain"]["slots"], 6)                               # three CAN boards, 2 -> 4 each

    def test_spec_costs(self):
        bad = self.E.validate_spec(dict(self.spec, costs={"widgets": 3, "station": -1, "currency": ""}))
        self.assertTrue(any("costs.widgets" in e for e in bad), bad)
        self.assertTrue(any("costs.station" in e for e in bad), bad)
        self.assertTrue(any("costs.currency" in e for e in bad), bad)
        self.assertEqual(self.E.validate_spec(dict(self.spec, costs={"station": 900, "currency": "USD"})), [])


class TestLayouts(unittest.TestCase):
    """Stations for groups of boards against one main station, by worth minus cost."""

    def setUp(self):
        from pcbtestkit import environment as E
        self.E = E
        self.spec = E.load_spec(None, {"max_stations": 2, "max_batch": 6})
        self.profs = [_fake_profile("can_%d" % i, ch=2, buses=["can"], site=(60.0, 70.0)) for i in range(6)]
        self.profs += [_fake_profile("eth_%d" % i, ch=2, buses=["ethernet"], site=(60.0, 70.0)) for i in range(6)]

    def test_groups_and_choice(self):
        E = self.E
        free = 300.0 * 220.0
        groups = E.kmeans(self.profs, 2, free)
        self.assertEqual(sorted(sorted(p["name"][:3] for p in g) for g in groups), [["can"] * 6, ["eth"] * 6])
        self.assertEqual(E.focus(groups[0]) in ("CAN", "Ethernet"), True)
        lays = E.layouts(self.profs, self.spec)
        names = [x["name"] for x in lays]
        for n in ("general", "specialized-2", "split-can", "split-ethernet"):
            self.assertIn(n, names)
        for x in lays:
            self.assertAlmostEqual(x["net"], x["worth"] - x["cost"], delta=1.0)
            self.assertEqual(len(x["stations"]), len(x["models"]))
        best = E.choose(lays, self.spec)
        top = max(x["fit_weight"] for x in lays)
        self.assertEqual(best["fit_weight"], top)
        self.assertEqual(best["net"], max(x["net"] for x in lays if x["fit_weight"] == top))
        self.assertEqual(E.choose(lays, dict(self.spec, layout="split-can"))["name"], "split-can")
        only = E.layouts(self.profs, dict(self.spec, layout="general"))
        self.assertEqual([x["name"] for x in only], ["general"])
        self.assertTrue(any("layout must be" in e for e in E.validate_spec(dict(self.spec, layout="bogus"))))
        self.assertEqual(E.validate_spec(dict(self.spec, layout="specialized-2")), [])
        summ = E.layout_summary(lays, best)
        self.assertEqual(sum(1 for x in summ if x["chosen"]), 1)
        self.assertNotIn("where", json.dumps(summ))


class TestStationUpgrade(unittest.TestCase):
    """An upgraded station keeps every pin, socket and hole it had: the fixtures made for it still plug in."""

    def test_pins_and_holes_stay(self):
        from pcbtestkit import facility as F
        from pcbtestkit import station_board as SB
        tmp = tempfile.mkdtemp(prefix="pcbtk_stup_")
        try:
            res = {"ain": 8, "vout": 2, "buses": {"can": 1, "uart": 1}, "dio": 2}
            st = {"name": "station-1", "title": "T station 1", "plate_mm": [300.0, 220.0], "resources": res,
                  "gnd_every": 6, "max_pins": 80}
            pcb = SB.write_station(st, os.path.join(tmp, "a"))
            fac, _w = F.facility_from_pcb(pcb, ident="station-1", name="T station 1")
            keep = SB.keep_of(fac)
            res2 = {"ain": 12, "vout": 3, "buses": {"can": 2, "uart": 1, "ethernet": 1}, "dio": 2}
            pcb2 = SB.write_station(dict(st, resources=res2, plate_mm=[400.0, 300.0], keep=keep, rev="B"),
                                    os.path.join(tmp, "b"))
            fac2, _w = F.facility_from_pcb(pcb2, ident="station-1", name="T station 1")
            old_c, new_c = fac["fixture"]["connectors"], fac2["fixture"]["connectors"]
            dx, dy = 50.0, 80.0                                              # (400 - 300) / 2, 300 - 220
            for a, b in zip(old_c, new_c):
                for num, fn in a["pins"].items():
                    self.assertEqual(b["pins"][num], fn, "pin %s" % num)
                self.assertAlmostEqual(b["at_mm"][0], a["at_mm"][0] + dx, delta=0.05)
                self.assertAlmostEqual(b["at_mm"][1], a["at_mm"][1] + dy, delta=0.05)
            fns = {f for c in new_c for f in c["pins"].values()}
            self.assertTrue({"AIN12", "VOUT3", "CAN2_H", "ETH_RX_N"} <= fns, fns)
            holes = [tuple(h["at_mm"]) for h in fac2["fixture"]["holes"]]
            for h in fac["fixture"]["holes"]:
                self.assertTrue(any(abs(x - h["at_mm"][0] - dx) < 0.05 and abs(y - h["at_mm"][1] - dy) < 0.05
                                    for x, y in holes), h)
            with open(pcb2, encoding="utf-8") as fh:
                self.assertIn('(rev "B")', fh.read())
            cap = F.capacity(fac2)
            self.assertEqual((cap["inputs"], cap["supplies"], cap["buses"].get("can"), cap["buses"].get("ethernet")),
                             (12, 3, 2, 1))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestEnvironmentGrow(unittest.TestCase):
    """A built environment gets new boards (its stations stay, its boards are reused) and an upgrade."""

    @classmethod
    def setUpClass(cls):
        import make_collection
        cls.tmp = tempfile.mkdtemp(prefix="pcbtk_grow_")
        cls.col = os.path.join(cls.tmp, "boards")
        make_collection.main(cls.col, count=4, seed=5)
        cls.more = os.path.join(cls.tmp, "more")
        rnd = __import__("random").Random(11)
        fam = [f for f in make_collection.FAMILIES if f[0] == "can_node"][0]
        os.makedirs(os.path.join(cls.more, "extra_can"))
        b, _cfg = make_collection.make_board("extra_can", fam, rnd)
        with open(os.path.join(cls.more, "extra_can", "extra_can.kicad_pcb"), "w") as fh:
            fh.write(b.text())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_add_and_upgrade(self):
        from pcbtestkit import environment as E
        from pcbtestkit import upgrades as U
        out = os.path.join(self.tmp, "env")
        kw = dict(jobs=1, visuals=False, route=False, video="none")
        env = E.build_environment([self.col], out, E.load_spec(None, {"name": "Grow", "max_stations": 1}), **kw)
        n0 = env["summary"]["compatible"]
        self.assertGreaterEqual(n0, 3)
        with open(os.path.join(out, "sources.json"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn(self.tmp, src)                                    # relative to the environment's folder
        self.assertEqual(len(E.environment_sources(out)[0]), env["summary"]["boards"])
        for f in ("constraints.md", "constraints.json", "constraints.csv", "upgrades.md", "upgrades.json"):
            self.assertTrue(os.path.exists(os.path.join(out, f)), f)
        with open(os.path.join(out, "index.html"), encoding="utf-8") as fh:
            page = fh.read()
        self.assertIn("What limits the boards", page)
        self.assertIn("Upgrade options", page)
        sha = env["stations"][0]["board_sha"]
        t0 = time.time()
        env2 = E.add_boards(out, [self.more], **kw)
        self.assertEqual(env2["added"], ["extra_can"])
        self.assertEqual(env2["summary"]["boards"], env["summary"]["boards"] + 1)
        self.assertTrue(env2["stations"][0]["fixed"])
        self.assertEqual(E.board_sha(os.path.join(out, env2["stations"][0]["board"])), sha)   # the station stays
        self.assertLess(time.time() - t0, 90.0)
        with self.assertRaises(ValueError):
            E.add_boards(os.path.join(self.tmp, "nowhere"), [self.more], **kw)
        # an upgrade: one more CAN; the station keeps its pins, boards whose batch stays keep their fixture
        with open(os.path.join(out, "stations", "station-1", "facility.json"), encoding="utf-8") as fh:
            before = json.load(fh)
        can_before = int(env2["stations"][0]["resources"]["buses"].get("can", 0))
        env3 = E.upgrade_environment(out, U.parse_changes("station-1:can+1"), **kw)
        st = env3["stations"][0]
        self.assertEqual(st["rev"], "B")
        self.assertEqual(int(st["resources"]["buses"]["can"]), can_before + 1)
        with open(os.path.join(out, "stations", "station-1", "facility.json"), encoding="utf-8") as fh:
            after = json.load(fh)
        for a, b in zip(before["fixture"]["connectors"], after["fixture"]["connectors"]):
            for num, fn in a["pins"].items():
                self.assertEqual(b["pins"][num], fn)
        self.assertTrue(any(b.get("kept_fixture") for b in env3["boards"]), [b["name"] for b in env3["boards"]])
        self.assertTrue(env3["upgraded"] and "more CAN" in env3["upgraded"][0]["text"])
        # a plain build into the same folder keeps the upgraded station (it is hardware by now)
        env5 = E.build_environment([self.col, self.more], out, E.load_spec(None, {"name": "Grow", "max_stations": 1}), **kw)
        self.assertEqual(env5["stations"][0]["resources"]["buses"]["can"], can_before + 1)
        self.assertEqual(env5["stations"][0]["rev"], "B")
        self.assertTrue(any("kept as it is" in w for w in env5["warnings"]), env5["warnings"])
        # a station that is your own board is not changed
        with open(os.path.join(out, "environment.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        data["stations"][0]["own"] = True
        with open(os.path.join(out, "environment.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        with self.assertRaises(ValueError) as cm:
            E.upgrade_environment(out, U.parse_changes("station-1:uart+1"), **kw)
        self.assertIn("your own station board", str(cm.exception))
        r = subprocess.run([sys.executable, "-m", "pcbtestkit", "environment", "upgrade", out, "--add", "station-1:can+1"],
                           capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        self.assertIn("more CAN", r.stdout)
        self.assertIn("--apply", r.stdout)
        r = subprocess.run([sys.executable, "-m", "pcbtestkit", "environment", "constraints", out, "--board", "extra_can",
                            "--json"], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        self.assertEqual(json.loads(r.stdout)[0]["board"], "extra_can")
        r = subprocess.run([sys.executable, "-m", "pcbtestkit", "environment", "options", out], capture_output=True,
                           text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        self.assertIn("upgrade options", r.stdout)

    def test_agent_tools(self):
        out = os.path.join(self.tmp, "env_tools")
        from pcbtestkit import environment as E
        E.build_environment([self.col], out, E.load_spec(None, {"max_stations": 1}), jobs=1, visuals=False, route=False,
                            video="none")
        ws = Workspace(None)
        r = ws.call("get_environment", {"path": out})
        self.assertTrue(r["ok"], r)
        self.assertIn("options", r)
        self.assertLess(len(json.dumps(r)), 12000)
        with open(os.path.join(out, "environment.json"), encoding="utf-8") as fh:
            b = json.load(fh)["boards"][0]["name"]
        r = ws.call("get_environment", {"path": out, "board": b})
        self.assertEqual(r["board"]["board"], b)
        r = ws.call("plan_environment_upgrade", {"path": out, "changes": "station-1:uart+1"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["changes"][0]["kind"], "bus")
        r = ws.call("upgrade_environment", {"path": out, "changes": "station-1:uart+1"})
        self.assertFalse(r["started"])
        self.assertIn("CONFIRM", r["note"])
        r = ws.call("add_boards_to_environment", {"path": out, "boards": [self.more], "dry_run": True})
        self.assertEqual(r["new"], ["extra_can"])
        self.assertFalse(r["started"])
        r = ws.call("plan_environment_upgrade", {"path": out, "options": ["U999"]})
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "NOT_FOUND")
        r = ws.call("get_environment_job", {"path": out})
        self.assertFalse(r["ok"])
