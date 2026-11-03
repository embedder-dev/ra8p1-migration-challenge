"""Standalone qualifier checks; host/compile evidence only, never hardware."""
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/ra8p1/qualification_wdt.c"
COMPILER = Path("/Applications/Renesas e2 studio with RA FSP v6.6.0/toolchains/"
                "arm-gnu-toolchain-13.2.Rel1-darwin-arm64-arm-none-eabi/bin/arm-none-eabi-gcc")


class QualificationBlockerTests(unittest.TestCase):
    def test_analog_spec_has_no_rejected_empty_selections(self):
        spec = json.loads((ROOT / "tests/ra8p1/analog.json").read_text())
        for module in spec["modules"]:
            for key, value in module["properties"].items():
                self.assertNotEqual(value, "", key)

    def test_omitted_analog_masks_generate_disabled_values(self):
        generated = (ROOT / "validation/projects/qual_analog/ra_gen/hal_data.c").read_text()
        for name, expression in (
            ("sample_and_hold_enable_mask", r"\(\s*ADC_B_SAMPLE_AND_HOLD_MASK_NONE\s*\)"),
            ("external_trigger_enable_mask", r"\(\s*ADC_B_EXTERNAL_TRIGGER_NONE\s*\)"),
            ("elc_trigger_enable_mask", r"\(elc_peripheral_t\)\(\s*0x00\s*\)"),
            ("gpt_trigger_enable_mask", r"\(\s*ADC_B_GPT_TRIGGER_NONE\s*\)"),
        ):
            with self.subTest(field=name):
                values = re.findall(r"\." + name + r"\s*=\s*([^,]+),", generated)
                self.assertTrue(values, name)
                for value in values:
                    self.assertRegex(value, "^" + expression + "$")

    def test_wdt_refresh_wait_covers_count_clock_synchronization(self):
        source = FIXTURE.read_text()
        macro = re.search(r"^#define WDT_REFRESH_SETTLE_US.*$", source, re.M)
        self.assertIsNotNone(macro)
        self.assertEqual(source.count("R_BSP_SoftwareDelay(WDT_REFRESH_SETTLE_US("), 2)
        cc = shutil.which("clang") or shutil.which("cc")
        self.assertIsNotNone(cc)
        with tempfile.TemporaryDirectory(prefix="wdt-refresh-") as temp:
            src = Path(temp) / "test.c"
            exe = Path(temp) / "test"
            src.write_text("#include <assert.h>\n#include <stdint.h>\n" + macro.group() + '''
int main(void) {
    uint32_t frequencies[] = {488U, 3662U, 31250U, 1000000U, 31250000U};
    for (unsigned i = 0; i < sizeof(frequencies)/sizeof(frequencies[0]); ++i) {
        uint32_t hz = frequencies[i];
        uint32_t delay = WDT_REFRESH_SETTLE_US(hz);
        assert((uint64_t)delay * hz >= 5000000U);
        assert((uint64_t)(delay - 1U) * hz < 5000000U);
    }
    assert(WDT_REFRESH_SETTLE_US(3662U) > 100U);
}
''')
            subprocess.run([cc, "-Wall", "-Wextra", "-Werror", str(src), "-o", str(exe)], check=True)
            subprocess.run([str(exe)], check=True)

    def test_wdt_effective_option_selection(self):
        """Compile the fixture's exact selector with a read-only host MMIO stub."""
        source = FIXTURE.read_text()
        selector = source.split("static bool wdt_register_start_selected(void)", 1)[1]
        selector = "static bool wdt_register_start_selected(void)" + selector.split(
            "static fsp_err_t wdt_refresh_cycle", 1)[0]
        constants = "\n".join(re.findall(r"^#define WDT_OPTION_.*$", source, re.M))
        cc = shutil.which("clang") or shutil.which("cc")
        self.assertIsNotNone(cc)
        with tempfile.TemporaryDirectory(prefix="wdt-qualifier-") as temp:
            temp = Path(temp)
            for core in (0, 1):
                with self.subTest(core=core):
                    text = '''#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
''' + constants + f"\n#define BSP_CFG_CPU_CORE {core}\n" + '''
static struct { uint32_t option_address, option_value, option_selection; } g_validation_wdt;
static uint32_t options[4], reads;
#define ofs0 options[0]
#define ofs3 options[1]
#define ofs3_sec options[2]
#define sel options[3]
static uint32_t wdt_read_option(uintptr_t address)
{
    reads++;
#if BSP_CFG_CPU_CORE == 0
    assert(address == WDT_OPTION_OFS0_ADDRESS);
    return ofs0;
#else
    if (address == WDT_OPTION_OFS3_SEL_ADDRESS) { return sel; }
    if (sel & WDT_OPTION_START_MASK)
    {
        assert(address == WDT_OPTION_OFS3_ADDRESS);
        return ofs3;
    }
    assert(address == WDT_OPTION_OFS3_SEC_ADDRESS);
    return ofs3_sec;
#endif
}
''' + selector + '''
int main(void)
{
    for (uint32_t bits = 0; bits < 16; bits++)
    {
        ofs0 = (bits & 1U) ? WDT_OPTION_START_MASK : 0;
        ofs3 = (bits & 2U) ? WDT_OPTION_START_MASK : 0;
        ofs3_sec = (bits & 4U) ? WDT_OPTION_START_MASK : 0;
        sel = (bits & 8U) ? WDT_OPTION_START_MASK : 0;
        /* Unrelated option fields must not influence the start-mode decision. */
        ofs0 |= 0xA5A00000U;
        ofs3 |= 0x5A500000U;
        ofs3_sec |= 0x0F000000U;
        sel |= 0x88800000U;
        reads = 0;
#if BSP_CFG_CPU_CORE == 0
        uint32_t expected = ofs0;
#else
        uint32_t expected = (sel & WDT_OPTION_START_MASK) ? ofs3 : ofs3_sec;
#endif
        assert(wdt_register_start_selected() == ((expected & WDT_OPTION_START_MASK) != 0));
        assert(g_validation_wdt.option_value == expected);
        assert(reads == (BSP_CFG_CPU_CORE ? 2U : 1U));
    }
    return 0;
}
'''
                    path = temp / f"selector{core}.c"
                    path.write_text(text)
                    executable = temp / f"selector{core}"
                    result = subprocess.run([cc, "-std=c11", "-Wall", "-Wextra", "-Werror",
                                             str(path), "-o", str(executable)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    result = subprocess.run([str(executable)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_wdt_runtime_gate_precedes_commands_and_open(self):
        source = FIXTURE.read_text()
        entry = source.split("void hal_entry(void)", 1)[1]
        self.assertLess(entry.index("if (!wdt_register_start_selected())"),
                        entry.index("command = g_wdt_validation_command"))
        self.assertLess(entry.index("if (!wdt_register_start_selected())"), entry.index("R_WDT_Open("))
        self.assertNotRegex(source, r"#\s*define\s+BSP_CFG_ROM_REG_OFS[03]")
        self.assertIn("return *(uint32_t const volatile *) address;", source)

    def test_wdt_fixture_compiles_and_rejects_explicit_autostart(self):
        compiler = os.environ.get("ARM_NONE_EABI_GCC", str(COMPILER))
        self.assertTrue(Path(compiler).is_file(), "GNU 13.2 compiler required")
        for core, cpu, fpu, macro in (
            (0, "cortex-m85", "fpv5-d16", "BSP_CFG_ROM_REG_OFS0"),
            (1, "cortex-m33", "fpv5-sp-d16", "BSP_CFG_ROM_REG_OFS3"),
        ):
            project = ROOT / f"validation/projects/qual_wdt{core}"
            includes = [project / name for name in (
                "ra_cfg/fsp_cfg/bsp", "ra_gen", "ra_cfg/fsp_cfg", "src", "ra/fsp/inc",
                "ra/fsp/inc/api", "ra/fsp/inc/instances", "ra/arm/CMSIS_6/CMSIS/Core/Include")]
            command = [compiler, "-std=c99", "-mthumb", "-mcpu=" + cpu,
                       "-mfloat-abi=hard", "-mfpu=" + fpu, "-Wall", "-Wextra", "-Werror",
                       *["-I" + str(path) for path in includes], "-fsyntax-only", str(FIXTURE)]
            for definition, expected in (([], 0), (["-D" + macro + "=0"], 1),
                                         (["-D" + macro + "=0x00020000UL"], 0)):
                with self.subTest(core=core, definitions=definition):
                    result = subprocess.run(command + definition, capture_output=True, text=True)
                    if expected:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("Qualification requires register-start WDT", result.stderr)
                    else:
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
