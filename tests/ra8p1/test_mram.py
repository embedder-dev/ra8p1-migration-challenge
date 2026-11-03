"""Compile and execute the actual MRAM driver against an ordered host bus model.

The C harness includes the unchanged FSP 5.9 flash_api_t and common error codes.
No physical device is accessed. A compile-only pass also uses real volatile stores
instead of the model hooks; host execution cannot prove silicon timing or retention.
"""
from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
FSP = ROOT / "baseline/ra/fsp"
FIXTURE = Path(__file__).parent / "fixtures/mram"
DRIVER = FSP / "src/r_mram/r_mram.c"
MDF = ROOT / "baseline/module_descriptions/Renesas##HAL Drivers##all##r_mram####5.9.0.xml"


class MramHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cc = shutil.which("clang") or shutil.which("cc")
        if not cls.cc:
            raise RuntimeError("A C compiler is required for the MRAM behavior tests")

    def run_compile(self, args):
        result = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def compile_args(self, checking, nonsecure):
        return [self.cc, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                f"-DMRAM_CFG_PARAM_CHECKING_ENABLE={checking}",
                f"-DBSP_TZ_NONSECURE_BUILD={nonsecure}",
                "-I", str(FIXTURE), "-I", str(FSP / "inc"),
                "-I", str(FSP / "inc/api"), "-I", str(FSP / "inc/instances")]

    def test_driver_behavior_all_parameter_and_security_modes(self):
        with tempfile.TemporaryDirectory(prefix="mram-host-") as temp:
            for checking in (0, 1):
                for nonsecure in (0, 1):
                    with self.subTest(parameter_checks=checking, nonsecure=nonsecure):
                        output = Path(temp) / f"mram-{checking}-{nonsecure}"
                        args = self.compile_args(checking, nonsecure)
                        self.run_compile(args + ["-include", str(FIXTURE / "mram_mock.h"), str(DRIVER),
                                                 str(FIXTURE / "mram_harness.c"), "-o", str(output)])
                        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=10)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertIn("MRAM C behavior tests passed", result.stdout)
                        # Compile the production memory accesses too, not just the instrumented bus model.
                        self.run_compile(args + ["-c", str(DRIVER), "-o", str(output) + ".o"])

    def test_register_fixture_matches_authoritative_svd(self):
        svd_path = ROOT / ".embedder/svd-cache/R7KA8P1AD.json"
        if not svd_path.exists():
            self.skipTest("Full project SVD cache is not installed; C behavior tests still run")
        svd = json.loads(svd_path.read_text())
        peripheral = next(p for p in svd["peripherals"] if p["name"] == "MRAM")
        registers = {r["name"]: r for r in peripheral["registers"]}
        for name, offset, bits in (("MRCPC0", 0x3000, 16), ("MRCPC1", 0x3004, 16),
                                   ("MRCPS", 0x3010, 8), ("MRCFLR", 0x3030, 16),
                                   ("MRCEECC", 0x3804, 16), ("MRCRAES", 0x18, 8)):
            with self.subTest(register=name):
                self.assertEqual(registers[name]["addressOffset"], offset)
                self.assertEqual(registers[name]["size"], bits)
        fields = {f["name"]: f["bitOffset"] for f in registers["MRCPS"]["fields"]}
        self.assertEqual(fields, {"PRGERRC": 0, "ECCERRC": 1, "ABUFEMP": 5,
                                  "ABUFFULL": 6, "PRGBSYC": 7})

    def test_generated_device_header_mram_layout(self):
        device = FSP / "src/bsp/cmsis/Device/RENESAS/Include"
        registers = (("MRCPFB", 0x0, 1), ("MRCFREQ", 0x4, 4),
                     ("MRCRAES", 0x18, 1), ("MRCPC0", 0x3000, 2),
                     ("MRCPC1", 0x3004, 2), ("MRCPS", 0x3010, 1),
                     ("MRCFLR", 0x3030, 2), ("MRCEECC", 0x3804, 2))
        with tempfile.TemporaryDirectory(prefix="mram-device-header-") as temp:
            temp = Path(temp)
            # Only CMSIS access qualifiers are stubbed; register types come from the real device header.
            for core in ("core_cm33.h", "core_cm85.h"):
                (temp / core).write_text("#define __IM volatile const\n#define __OM volatile\n"
                                         "#define __IOM volatile\n")
            content = '#include <stddef.h>\n#include "R7KA8P1AD.h"\n'
            for name, offset, size in registers:
                content += (f'_Static_assert(offsetof(R_MRAM_Type, {name}) == {offset}, "{name} offset");\n'
                            f'_Static_assert(sizeof(((R_MRAM_Type *)0)->{name}) == {size}, "{name} width");\n')
            content += ('int main(void) { return !((uintptr_t)R_MRAM == 0x4013C000U && '
                        '(uintptr_t)R_MRAM_NS == 0x5013C000U); }\n')
            source = temp / "layout.c"
            source.write_text(content)
            for core in (0, 1):
                with self.subTest(cpu_core=core):
                    output = temp / f"layout-{core}"
                    self.run_compile([self.cc, "-std=c11", "-Wall", "-Wextra", "-Werror",
                                      f"-DBSP_CFG_CPU_CORE={core}", "-I", str(temp), "-I", str(device),
                                      str(source), "-o", str(output)])
                    result = subprocess.run([str(output)], capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_mdf_generation_elements_follow_fsp_59_order(self):
        generation_elements = {"header", "includes", "declarations"}
        expected = ["header", "includes", "declarations"]
        # FSP 5.9 requires header before includes; parsing XML and compiling C do not check this.
        for path in (MDF, *(MDF.parent / f"Renesas##HAL Drivers##all##{name}####5.9.0.xml"
                            for name in ("r_flash_lp", "r_flash_hp"))):
            with self.subTest(module=path.name):
                module = ET.parse(path).getroot().find("module")
                self.assertEqual([child.tag for child in module if child.tag in generation_elements],
                                 expected)

    def test_mdf_generated_declarations_compile(self):
        root = ET.parse(MDF).getroot()
        module = root.find("module")
        self.assertEqual(module.get("id"), "module.driver.flash_on_mram")
        properties = {p.get("id"): p.get("default") for p in module.findall("property")}
        properties.update({"module.driver.mram.name": "g_mram0",
                           "module.driver.mram.alias": "MRAM_CODE_NONSECURE_START",
                           "module.driver.mram.mriclk_hz": "250000000",
                           "module.driver.mram.mrpclk_hz": "125000000",
                           "module.driver.mram.enter": "test_enter",
                           "module.driver.mram.exit": "test_exit",
                           "module.driver.mram.time": "test_time"})
        content = module.findtext("includes") + "\n" + module.findtext("header") + "\n" + module.findtext("declarations")
        for key, value in properties.items():
            content = content.replace("${" + key + "}", value)
        self.assertNotIn("${", content)
        with tempfile.TemporaryDirectory(prefix="mram-mdf-") as temp:
            source = Path(temp) / "generated.c"
            source.write_text("#define BSP_IRQ_DISABLED 0\n#define FSP_INVALID_VECTOR (-1)\n" + content)
            self.run_compile(self.compile_args(1, 0) + ["-c", str(source), "-o", str(source) + ".o"])


if __name__ == "__main__":
    unittest.main()
