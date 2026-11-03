"""Software-only qualification hook checks; no target access or MRAM programming."""
from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
FSP = ROOT / "baseline/ra/fsp"
FIXTURE = ROOT / "tests/ra8p1/fixtures/mram_qualification"
SOURCE = ROOT / "tests/ra8p1/qualification_mram.c"
DEVICE = FSP / "src/bsp/cmsis/Device/RENESAS/Include"


class MramQualificationTests(unittest.TestCase):
    def run_checked(self, args, **kwargs):
        result = subprocess.run(args, capture_output=True, text=True, timeout=60, **kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_platform_gates_and_restore(self):
        cc = shutil.which("clang") or shutil.which("cc")
        self.assertIsNotNone(cc)
        with tempfile.TemporaryDirectory(prefix="mram-qualification-host-") as tmp:
            tmp = Path(tmp)
            for core in ("core_cm85.h", "core_cm33.h"):
                (tmp / core).write_text("/* core intrinsics provided by host hal_data.h */\n")
            (tmp / "r_mram_cfg.h").write_text("#define MRAM_CFG_PARAM_CHECKING_ENABLE 1\n")
            for opt in ("-O0", "-O2"):
                exe = tmp / ("hooks" + opt)
                args = [cc, "-std=c99", opt, "-Wall", "-Wextra", "-Werror",
                        "-fsanitize=undefined", "-fno-sanitize-recover=all",
                        "-DBSP_CFG_CPU_CORE=0"]
                args += [a for d in (tmp, FIXTURE, DEVICE, FSP / "inc", FSP / "inc/api", FSP / "inc/instances")
                         for a in ("-I", str(d))]
                self.run_checked(args + [str(FIXTURE / "platform_harness.c"), "-o", str(exe)])
                self.assertIn("platform gates and restoration passed", self.run_checked([str(exe)]))

    def test_spec_references(self):
        spec_path = ROOT / "tests/ra8p1/mram.json"
        spec = json.loads(spec_path.read_text())
        self.assertEqual((spec_path.parent / spec["source"]).resolve(), SOURCE)
        descriptions = ROOT / "baseline/module_descriptions"
        mdf = ET.parse(descriptions / "Renesas##HAL Drivers##all##r_mram####5.9.0.xml").getroot()
        module = mdf.find("module")
        entry, = spec["modules"]
        self.assertEqual(entry["id"], module.get("id"))
        for key, value in entry["properties"].items():
            prop = module.find(f"property[@id='{key}']")
            self.assertIsNotNone(prop, key)
            options = prop.findall("option")
            if options:
                self.assertIn(value, [x.get("id") for x in options])
        self.assertEqual(entry["properties"]["module.driver.mram.mriclk_hz"], "8000000")
        self.assertEqual(entry["properties"]["module.driver.mram.mrpclk_hz"], "8000000")
        documents = [ET.parse(descriptions / n).getroot() for n in (
            "Renesas##BSP##ra8p1##fsp####5.9.0.xml", "Renesas##Common##all##fsp_common####5.9.0.xml")]
        for key, value in spec["bsp"].items():
            prop = next((doc.find(f".//property[@id='{key}']") for doc in documents
                         if doc.find(f".//property[@id='{key}']") is not None), None)
            self.assertIsNotNone(prop, key)
            if prop.findall("option"):
                self.assertIn(value, [x.get("id") for x in prop.findall("option")])
        pinmap = ET.parse(ROOT / "packaging/ra8p1/mcu/.mcu/.pinmapping/PinCfgR7KA8P1KFLCAC.xml").getroot()
        for pin in spec["pins"]:
            component = pinmap.find(f".//component[@id='{pin['component']}']")
            self.assertIsNotNone(component)
            for key, value in {**pin["configs"], **pin["connections"]}.items():
                config = component.find(f".//configuration[@id='{key}']")
                self.assertIsNotNone(config, key)
                self.assertIn(value, [x.get("id") for x in config.findall("alt")])

    def test_production_compile_and_ram_relocations(self):
        compilers = list(Path("/Applications").glob("**/arm-gnu-toolchain-13.2*/bin/arm-none-eabi-gcc"))
        project = ROOT / "validation/projects/candidate_ra8p1_cpu0"
        if not compilers or not (project / "ra_gen/hal_data.h").exists():
            self.skipTest("Installed Arm toolchain and generated CPU0 config required for production compile")
        cc = compilers[0]
        with tempfile.TemporaryDirectory(prefix="mram-qualification-arm-") as tmp:
            tmp = Path(tmp)
            (tmp / "r_mram_cfg.h").write_text("#define MRAM_CFG_PARAM_CHECKING_ENABLE 1\n")
            includes = [tmp, project / "ra_cfg/fsp_cfg/bsp", project / "ra_gen",
                        project / "ra_cfg/fsp_cfg", FSP / "inc", FSP / "inc/api", FSP / "inc/instances",
                        project / "ra/arm/CMSIS_6/CMSIS/Core/Include"]
            for opt in ("-O0", "-O2"):
                obj = tmp / ("qualification" + opt + ".o")
                args = [str(cc), "-std=c99", opt, "-Wall", "-Wextra", "-Werror", "-Wconversion", "-Wshadow",
                        "-mthumb", "-mfloat-abi=hard", "-mcpu=cortex-m85+nopacbti",
                        "-D_RENESAS_RA_", "-D_RA_CORE=CM85", "-D_RA_ORDINAL=1"]
                args += [a for d in includes for a in ("-I", str(d))]
                driver_obj = tmp / ("driver" + opt + ".o")
                self.run_checked(args + ["-c", str(SOURCE), "-o", str(obj)])
                self.run_checked(args + ["-c", str(FSP / "src/r_mram/r_mram.c"), "-o", str(driver_obj)])
                objdump = str(cc.with_name("arm-none-eabi-objdump"))
                symbols = self.run_checked([objdump, "-t", str(obj)])
                driver_symbols = self.run_checked([objdump, "-t", str(driver_obj)])
                ram_symbols = {line.split()[-1] for line in (symbols + driver_symbols).splitlines()
                               if ".code_in_ram" in line and "*UND*" not in line}
                data_symbols = {line.split()[-1] for line in (symbols + driver_symbols).splitlines()
                                if any(section in line.split() for section in (".bss", ".data"))}
                # Both objects must keep every busy-path call and literal out of MRAM/libc.
                for target in (obj, driver_obj):
                    relocations = self.run_checked([objdump, "-r", "-j", ".code_in_ram", str(target)])
                    for line in relocations.splitlines():
                        if "R_ARM_" not in line:
                            continue
                        symbol = line.split()[-1]
                        if "CALL" in line or "JUMP" in line:
                            self.assertIn(symbol, ram_symbols, line)
                        else:
                            self.assertIn(symbol, ram_symbols | data_symbols |
                                          {"__Code_In_RAM_Start", "__Code_In_RAM_End", "SystemCoreClock"}, line)
                # No weak platform fallback and all platform entry points actually land in RAM code.
                for name in ("validation_mram_platform_prepare", "validation_mram_platform_finish",
                             "validation_mram_platform_time_us", "mram_exception_park"):
                    line, = [x for x in symbols.splitlines() if x.split() and x.split()[-1] == name]
                    self.assertIn(".code_in_ram", line)
                    self.assertNotIn(" w ", line)


if __name__ == "__main__":
    unittest.main()
