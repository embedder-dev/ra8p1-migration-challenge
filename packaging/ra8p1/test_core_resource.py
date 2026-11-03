#!/usr/bin/env python3
"""Check physical-device discovery, debugger routing, and partition metadata.

The optional installed-host test invokes only the pure debugger-name resolver,
not project generation or hardware. Regenerated-project checks remain required.
"""

import io
import os
import shutil
import subprocess
import tempfile

from pathlib import Path
import unittest
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[2]
MCU = ROOT / "packaging/ra8p1/mcu"
BASELINE = ROOT / "baseline/module_descriptions"
PART = "R7KA8P1KFLCAC"
DEBUG_BASE = "R7KA8P1KF"
CORES = {
    "CM85": {"architecture": "Cortex-M85", "clock": 1000000000,
             "bsp": "bsp.ra8p1.cpu0", "record": PART,
             "rom": (0x02000000, 0x02080000), "ram": (0x22000000, 0x220C0000)},
    "CM33": {"architecture": "Cortex-M33", "clock": 250000000,
             "bsp": "bsp.ra8p1.cpu1", "record": PART + "_CPU1",
             "rom": (0x02080000, 0x020FF000), "ram": (0x220C0000, 0x22180000)},
}
RESERVED = ((0x020FF000, 0x02100000), (0x22180000, 0x221A0000))


def parse(relative):
    return ET.parse(MCU / relative).getroot()


def pg_targets():
    result = {}
    for cpu in parse(".mcu/.devices/pg/pgra8p1.xmi").findall(".//Cpu"):
        for device in cpu.findall(".//Device"):
            name = device.get("shortName")
            if name in result:
                raise AssertionError("Duplicate PG device: " + name)
            result[name] = (cpu, device)
    return result


def pg_memory():
    result = {}
    for path in (MCU / ".mcu/.devices/pg").glob("memory*.xmi"):
        for memory in ET.parse(path).getroot().findall("memoryMap"):
            name = memory.get("device")
            if name in result:
                raise AssertionError("Duplicate memory map: " + name)
            result[name] = {
                area.get("type"): (int(area.get("startAddress"), 0),
                                   int(area.get("endAddress"), 0))
                for area in memory.findall("memoryArea")
            }
    return result


class CoreResourceTests(unittest.TestCase):
    def test_pg_keeps_one_physical_device_and_existing_default(self):
        actual = pg_targets()
        self.assertEqual(set(actual), {PART})
        cpu, device = actual[PART]
        self.assertEqual(cpu.get("shortName"), "RA8P1")
        self.assertEqual(cpu.get("compilerCpuType"), "cortex-m85")
        # The host appends the selected core; a suffix here produces CPU0_CPU1.
        self.assertEqual(device.get("DeviceCommand"), DEBUG_BASE)
        self.assertEqual(cpu.find("PinCountRZTC").get("PinNumber"), "289")
        self.assertEqual(cpu.find("BuilderOptions").get("value"), "renesaspincfg")

    def test_each_core_has_matching_debugger_resources(self):
        gdb = parse(".mcu/.devices/gdb/ra8p1.5.9.0.xml").findall(".//Device")
        dbg = parse(".mcu/.devices/dbg/dbgra8p1.xml").findall("device")
        expected = {DEBUG_BASE + "_CPU0": "CM85", DEBUG_BASE + "_CPU1": "CM33"}
        self.assertEqual({node.findtext("name") for node in dbg}, set(expected))
        self.assertEqual({node.get("cpuName"): node.get("armFamily") for node in gdb}, expected)
        for node in dbg:
            self.assertEqual(node.findtext("server/params/param"), "-t " + node.findtext("name"))

    def test_same_board_selects_the_physical_device(self):
        path = BASELINE / "Renesas##BSP##Board##ra8p1_ek####5.9.0.xml"
        board = ET.parse(path).getroot().find("board[@id='board.ra8p1ek']")
        self.assertEqual(board.get("device"), PART)
        self.assertEqual(board.get("display"), "RA8|RA8P1|EK-RA8P1")
        self.assertIn(board.get("device"), pg_targets())

    def test_same_physical_device_and_selected_core_bind_exactly_one_bsp(self):
        path = BASELINE / ("Renesas##BSP##ra8p1##device##" + PART + "##5.9.0.xml")
        root = ET.parse(path).getroot()
        for core, expected in CORES.items():
            with self.subTest(core=core):
                matches = [node for node in root.findall("device")
                           if node.get("device") == PART and node.get("core") == core]
                self.assertEqual(len(matches), 1)
                self.assertEqual(matches[0].get("id"), expected["record"])
                selected = {node.get("bsp") for node in matches[0].findall("select")}
                self.assertIn("bsp.ra8p1." + PART, selected)
                self.assertEqual(selected & {item["bsp"] for item in CORES.values()}, {expected["bsp"]})

    def test_physical_part_and_sizes_remain_unchanged(self):
        path = BASELINE / ("Renesas##BSP##ra8p1##device##" + PART + "##5.9.0.xml")
        root = ET.parse(path).getroot()
        config = root.find("config[@id='config.bsp.ra8p1." + PART + "']")
        self.assertEqual(config.find("property[@id='config.bsp.part_number']/option").get("value"), PART)
        self.assertEqual(int(config.find("property[@id='config.bsp.rom_size_bytes']/option").get("value")), 0x100000)
        self.assertEqual(int(config.find("property[@id='config.bsp.ram_size_bytes']/option").get("value")), 0x1A0000)
        self.assertIn("#define BSP_MCU_${config.bsp.part_number}", config.findtext("content"))

    def test_physical_rzone_exposes_both_cores_for_discovery(self):
        device = parse(".mcu/.devices/zone/" + PART + ".rzone").find("device")
        self.assertEqual(device.get("Dname"), PART)
        cores = device.findall("processor")
        self.assertEqual(len(cores), len(CORES))
        self.assertEqual({core.get("Pname") for core in cores}, set(CORES))
        for core in cores:
            expected = CORES[core.get("Pname")]
            self.assertEqual(core.get("Dcore"), expected["architecture"])
            self.assertEqual(int(core.get("Dclock")), expected["clock"])

    def test_physical_rzone_is_capacity_not_per_core_allocation(self):
        root = parse(".mcu/.devices/zone/" + PART + ".rzone")
        memories = {node.get("name"): node for node in root.findall("resources/memories/memory")}
        self.assertEqual(set(memories), {"RAM", "FLASH"})
        for name, start, size in (("RAM", 0x22000000, 0x1A0000), ("FLASH", 0x02000000, 0x100000)):
            self.assertEqual(int(memories[name].get("start"), 0), start)
            self.assertEqual(int(memories[name].get("size"), 0), size)

    def test_default_pg_map_retains_the_cpu0_partition(self):
        self.assertEqual(pg_memory(), {PART: {"InternalRom": CORES["CM85"]["rom"],
                                             "InternalRam": CORES["CM85"]["ram"]}})

    def test_requested_partition_contract_excludes_scratch_and_shared(self):
        regions = [core[field] for core in CORES.values() for field in ("rom", "ram")]
        for i, (start, end) in enumerate(regions):
            self.assertLess(start, end)
            for other_start, other_end in regions[i + 1:] + list(RESERVED):
                self.assertFalse(start < other_end and other_start < end)
        self.assertEqual(CORES["CM85"]["rom"][1], CORES["CM33"]["rom"][0])
        self.assertEqual(CORES["CM85"]["ram"][1], CORES["CM33"]["ram"][0])
        self.assertEqual(CORES["CM33"]["rom"][1], RESERVED[0][0])
        self.assertEqual(CORES["CM33"]["ram"][1], RESERVED[1][0])

    def test_tooling_registers_existing_physical_resources_once(self):
        entries = parse("toolingSupport.xml").findall("file")
        self.assertFalse(any("_CPU1" in node.text or "_cpu1" in node.text for node in entries))
        for directory, support in (("pg", "PGDevice"), ("zone", "CMSISZoneResource")):
            for path in (MCU / ".mcu/.devices" / directory).iterdir():
                relative = path.relative_to(MCU).as_posix()
                matching = [entry for entry in entries if entry.text == relative]
                self.assertEqual(len(matching), 1, relative)
                self.assertEqual(matching[0].get("supportType"), support)
                self.assertEqual(matching[0].get("version"), "5.9.0")
        for entry in entries:
            if entry.text.startswith(".mcu/"):
                self.assertTrue((MCU / entry.text).is_file(), entry.text)

    def test_single_physical_pin_family_and_valid_default(self):
        extension = parse(".mcu/.pinmapping/pluginra8p1.xml").find("extension")
        families = {node.get("id") for node in extension.findall("family")}
        mappings = extension.findall("mapping")
        self.assertEqual(families, {"renesas.ra8p1"})
        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings[0].get("family"), "renesas.ra8p1")
        self.assertEqual(mappings[0].get("device"), "renesas.ra8p1_ac")
        self.assertEqual(mappings[0].get("package"), "renesas.289bga")
        self.assertTrue(mappings[0].get("path").endswith("/PinCfg" + PART + ".xml"))
        settings = parse(".mcu/.pinconfig/" + PART + ".pincfg")
        device = settings.find(".//{*}deviceSetting")
        self.assertEqual(device.get("id"), "renesas.ra8p1_ac")
        self.assertEqual(device.find("{*}packageSetting").get("id"), "renesas.289bga")
        model = parse(".mcu/.pinmapping/PinCfg" + PART + ".xml")
        configurations = {node.get("id"): node for node in model.findall(".//configuration")}
        for setting in settings.findall(".//{*}configSetting"):
            config = configurations[setting.get("configurationId")]
            self.assertIn(setting.get("altId"), {alt.get("id") for alt in config.findall("alt")})

    def test_pds_uses_only_the_physical_component(self):
        root = parse("Renesas.RA_mcu_ra8p1.pdsc")
        variants = {node.get("Cvariant") for node in root.findall("components/component") if node.get("Cvariant")}
        self.assertEqual(variants, {PART})

    def test_resource_vocabulary_comes_from_original_fsp_59(self):
        archive = ROOT / "reference/vendor/FSP_Packs_v5.9.0.zip"
        with zipfile.ZipFile(archive) as outer:
            member = "internal/projectgen/ra/packs/Renesas.RA_mcu_ra8m1.5.9.0.pack"
            with zipfile.ZipFile(io.BytesIO(outer.read(member))) as pack:
                names = pack.namelist()
                groups = {
                    "pg": [n for n in names if n.startswith(".mcu/.devices/pg/") and n.endswith(".xmi")],
                    "zone": [n for n in names if n.endswith(".rzone")],
                    "plugin": [n for n in names if n.startswith(".mcu/.pinmapping/plugin") and n.endswith(".xml")],
                    "tooling": ["toolingSupport.xml"],
                }
                checks = {
                    "pg": list((MCU / ".mcu/.devices/pg").glob("*.xmi")),
                    "zone": list((MCU / ".mcu/.devices/zone").glob("*.rzone")),
                    "plugin": [MCU / ".mcu/.pinmapping/pluginra8p1.xml"],
                    "tooling": [MCU / "toolingSupport.xml"],
                }
                for group, originals in groups.items():
                    vocabulary = {}
                    for name in originals:
                        for node in ET.fromstring(pack.read(name)).iter():
                            vocabulary.setdefault(node.tag, set()).update(node.attrib)
                    for path in checks[group]:
                        for node in ET.parse(path).getroot().iter():
                            self.assertIn(node.tag, vocabulary, str(path))
                            self.assertLessEqual(set(node.attrib), vocabulary[node.tag], str(path))


class HostMulticoreResolverTests(unittest.TestCase):
    def test_installed_host_resolves_unsuffixed_command_for_each_core(self):
        java = shutil.which("java")
        applications = Path("/Applications")
        candidates = sorted(applications.glob("Renesas*/Renesas*.app/Contents/eclipse/plugins"))
        plugins = next((path for path in candidates if list(path.glob("com.renesas.cdt.ra.contentgen_*.jar"))), None)
        if java is None or plugins is None:
            self.skipTest("Installed Renesas host and Java are required for the resolver check")
        classpath = os.pathsep.join(str(path) for path in sorted(plugins.iterdir())
                                   if path.is_dir() or path.suffix == ".jar")
        source = """public class CoreResolverProbe {
    public static void main(String[] args) throws Exception {
        Class<?> type = Class.forName("com.renesas.cdt.ra.ddsc.contentgen.idesettings.RaIdeSettingsContributor");
        Object contributor = type.getConstructor().newInstance();
        java.lang.reflect.Method create = java.util.Arrays.stream(type.getMethods())
            .filter(method -> method.getName().equals("getDeviceFamilyIdeSettings"))
            .findFirst().orElseThrow();
        Object settings = create.invoke(contributor, null, null, false);
        java.lang.reflect.Method resolve = Class.forName(
            "com.renesas.cdt.ddsc.contentgen.projectsettings.IDeviceFamilyIdeSettings")
            .getMethod("getMultiCoreDebugDeviceName", String.class, String.class);
        for (String core : new String[]{"CM85", "CM33"}) {
            System.out.println(core + "=" + resolve.invoke(settings, args[0], core));
        }
    }
}
"""
        _, device = pg_targets()[PART]
        with tempfile.TemporaryDirectory(prefix="ra8p1-core-resolver-") as temporary:
            probe = Path(temporary) / "CoreResolverProbe.java"
            probe.write_text(source)
            result = subprocess.run([java, "-cp", classpath, str(probe), device.get("DeviceCommand")],
                                    capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["CM85=" + DEBUG_BASE + "_CPU0",
                                                     "CM33=" + DEBUG_BASE + "_CPU1"])


if __name__ == "__main__":
    unittest.main()
