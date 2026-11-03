"""Source checks for the partial RA8P1 pinmap; no installed packs or target needed."""

import io
import json
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET
import zipfile


RESOURCES = Path(__file__).resolve().parent
ROOT = RESOURCES.parents[1]
MCU = RESOURCES / "mcu"
PINMAP = MCU / ".mcu/.pinmapping/PinCfgR7KA8P1KFLCAC.xml"
ROUTES = {"p502": ("pdmdat2", "in", "T7"), "p812": ("pdmclk2", "out", "P8")}


class PinmapTests(unittest.TestCase):
    def setUp(self):
        self.root = ET.parse(PINMAP).getroot()
        self.configs = {node.get("id"): node for node in self.root.iter("configuration")}
        self.alts = {node.get("id"): node for node in self.root.iter("alt")}
        self.provenance = json.loads((MCU / "resource-provenance.json").read_text())

    def test_reference_closure(self):
        for tag in ("configuration", "alt", "connection", "connectionSet", "portConfigSet"):
            nodes = list(self.root.iter(tag))
            self.assertEqual(len(nodes), len({node.get("id") for node in nodes}), tag)
        for ref_tag, target_tag in (("altRef", "alt"), ("configRef", "configuration"),
                                    ("connectionSetRef", "connectionSet"),
                                    ("portConfigSetRef", "portConfigSet")):
            targets = {node.get("id") for node in self.root.iter(target_tag)}
            for ref in self.root.iter(ref_tag):
                value = ref.get("refId")
                if value.startswith("@port_id."):
                    continue
                self.assertIn(value, targets, (ref_tag, value))
        for dependency in self.root.iter("dependency"):
            config = self.configs[dependency.get("configRef")]
            self.assertIsNotNone(config.find("alt[@id='%s']" % dependency.get("altRef")))
        for alt in self.root.iter("alt"):
            for ref in alt.findall("portConfigSetRef"):
                port = alt.get("id").split(".")[0]
                config_set = self.root.find(".//portConfigSet[@id='%s']" % ref.get("refId"))
                for target in config_set.iter("altRef"):
                    self.assertIn(target.get("refId").replace("@port_id", port), self.alts)

    def test_package_balls_and_provenance(self):
        balls = {node.get("ref"): node.get("name")
                 for node in self.root.findall(".//pinLayout/pin")}
        self.assertEqual(balls, self.provenance["gpio_ball_map"])
        self.assertEqual(len(balls), 19)
        self.assertEqual(len(set(balls.values())), 19)
        for port, (_, _, ball) in ROUTES.items():
            self.assertEqual(balls[port], ball)

    def test_default_configuration_keeps_swd_and_pdm_disabled(self):
        defaults = ET.parse(MCU / ".mcu/.pinconfig/R7KA8P1KFLCAC.pincfg").getroot()
        selected = set()
        for node in defaults.iter():
            if "configurationId" in node.attrib:
                self.assertIn(node.get("altId"), {
                    alt.get("id") for alt in self.configs[node.get("configurationId")].findall("alt")})
            if "altId" in node.attrib:
                self.assertIn(node.get("altId"), self.alts)
                selected.add(node.get("altId"))
        self.assertIn("jtag_fslash_swd.swdio.p210", selected)
        self.assertIn("jtag_fslash_swd.swclk.p211", selected)
        self.assertFalse(any("pdm" in value for value in selected))
        self.assertEqual(self.configs["pdm2.mode"].find("alt[@default='true']").get("id"),
                         "pdm2.mode.disabled")
        for port in ROUTES:
            self.assertEqual(self.configs[port].find("alt[@default='true']").get("id"), port + ".none")

    def test_board_defaults_generate_named_pin_table(self):
        board = ROOT / "baseline/module_descriptions/Renesas##BSP##Board##ra8p1_ek####5.9.0##configuration.xml"
        tables = ET.parse(board).getroot().findall("raPinConfiguration/pincfg")
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].get("symbol"), "g_bsp_pin_cfg")
        self.assertEqual(tables[0].get("active"), "true")
        self.assertEqual(tables[0].get("selected"), "true")
        for setting in tables[0].findall("configSetting"):
            config = self.configs[setting.get("configurationId")]
            self.assertIsNotNone(config.find("alt[@id='%s']" % setting.get("altId")))

    def test_only_verified_pdm_channel_and_reciprocal_connections(self):
        components = [node.get("id") for node in self.root.iter("component")
                      if node.get("id", "").startswith("pdm")]
        self.assertEqual(components, ["pdm2"])
        for port, (signal, direction, _) in ROUTES.items():
            local = f"{port}.pdm2.{signal}"
            remote = f"pdm2.{signal}.{port}"
            pin = self.root.find(f".//component[@id='pdm2']/pins/pin[@id='pdm2.{signal}']")
            self.assertEqual(pin.get("direction"), direction)
            self.assertEqual({alt.get("id") for alt in self.configs[f"pdm2.{signal}"].findall("alt")},
                             {f"pdm2.{signal}.none", remote})
            connection = self.root.find(f".//connection[@id='{port}.bind.pdm2.{signal}']")
            self.assertEqual([node.get("refId") for node in connection], [local, remote])
        pdm_port_alts = {value for value in self.alts if ".pdm" in value and value.startswith("p")
                         and not value.startswith("pdm")}
        self.assertEqual(pdm_port_alts, {f"{port}.pdm2.{signal}" for port, (signal, _, _) in ROUTES.items()})

    def test_psel_and_pmr_encoding_matches_ra8p1_tables(self):
        evidence = self.provenance["pdm_pinmux"]
        self.assertEqual(evidence["psel"], 0b11011)
        self.assertEqual(evidence["psel_bits"], "28:24")
        self.assertEqual(evidence["pmr_bit"], 16)
        for port, (signal, direction, ball) in ROUTES.items():
            alt = self.alts[f"{port}.pdm2.{signal}"]
            self.assertEqual(alt.find("portConfigSetRef").get("refId"), "mode.peripheral.speed.none")
            selector = int(alt.find("registerSetting").get("value"), 0)
            self.assertEqual(selector, 0b11011 << 24)
            mode = self.alts[f"{port}.gpio_mode.gpio_mode_peripheral"]
            self.assertEqual(mode.find("registerSetting").get("value"), "IOPORT_CFG_PERIPHERAL_PIN")
            self.assertIsNotNone(mode.find(f"dependencies/dependency[@altRef='{port}.pdm2.{signal}']"))
            self.assertEqual(selector | (1 << 16), int(evidence["enabled_pfs_value"], 0))
            self.assertEqual(evidence["routes"][port],
                             {"signal": signal.upper(), "direction": direction, "ball": ball})

    def test_enabled_and_disabled_modes_select_both_pins(self):
        for mode, suffixes in (("enabled.free", ("pdmdat2.p502", "pdmclk2.p812")),
                               ("disabled", ("pdmdat2.none", "pdmclk2.none"))):
            alt = self.alts["pdm2.mode." + mode]
            ref = alt.find("connectionSetRef").get("refId")
            group = self.root.find(f".//connectionSet[@id='{ref}']")
            self.assertEqual(group.get("lock"), "true")
            self.assertEqual(group.get("operator"), "and")
            self.assertEqual({node.get("refId") for node in group},
                             {"pdm2." + suffix for suffix in suffixes})

    def test_original_59_schema_vocabulary(self):
        with zipfile.ZipFile(ROOT / "reference/vendor/FSP_Packs_v5.9.0.zip") as archive:
            path = next(name for name in archive.namelist()
                        if name.endswith("/Renesas.RA_mcu_ra8m1.5.9.0.pack"))
            with zipfile.ZipFile(io.BytesIO(archive.read(path))) as pack:
                path = next(name for name in pack.namelist() if "PinCfg" in name and name.endswith(".xml"))
                original = ET.fromstring(pack.read(path))
        vocabulary = {}
        for node in original.iter():
            vocabulary.setdefault(node.tag, set()).update(node.attrib)
        for node in self.root.iter():
            self.assertIn(node.tag, vocabulary)
            self.assertLessEqual(set(node.attrib), vocabulary[node.tag], node.tag)
        for attribute in ("direction", "operator", "type", "category"):
            original_values = {node.get(attribute) for node in original.iter() if attribute in node.attrib}
            values = {node.get(attribute) for node in self.root.iter() if attribute in node.attrib}
            self.assertLessEqual(values, original_values, attribute)


if __name__ == "__main__":
    unittest.main()
