"""Host tests of the real FSP 5.9 driver/API sources; no target is accessed.

Peripheral layouts are extracted from the checked-in device headers, not hand
replaced with driver-shaped structs. The shim models clock and calibration
completion; it cannot prove analog accuracy, electrical timing, or watchdog reset.
"""
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
FSP = ROOT / "baseline/ra/fsp"
FIXTURE = Path(__file__).parent / "fixtures/analog_wdt"
DEVICE = FSP / "src/bsp/cmsis/Device/RENESAS/Include"
MDF = ROOT / "baseline/module_descriptions"


def register_header(legacy_adc=False):
    text = (DEVICE / "R7KA8P1AD.h").read_text()
    result = ["#define __IOM volatile", "#define __IM volatile", "#define __OM volatile"]
    for name in ("ADC_B", "DAC0_B", "MRAM", "WDT0", "ICU", "TSN", "TSD", "SYSC"):
        source = text
        if name == "ADC_B" and legacy_adc:
            name = "ADC_B0"
            source = (DEVICE / "R7FA6T2BD.h").read_text()
        end = source.index("} R_" + name + "_Type;") + len("} R_" + name + "_Type;")
        start = max(source.rfind("typedef union", 0, end), source.rfind("typedef struct", 0, end))
        result.append(source[start:end])
        result += re.findall(r"^[ \t]*#define R_" + name + r"_\w+_(?:Pos|Msk)\b[^\n]+", source, re.M)
        if name == "ADC_B0":
            result.append("typedef R_ADC_B0_Type R_ADC_B_Type;")
    result += ["extern R_ADC_B_Type test_adc;", "#define R_ADC_B (&test_adc)",
               "extern R_DAC0_B_Type test_dac[2];", "#define R_DAC0_B (&test_dac[0])",
               "#define R_DAC1_B (&test_dac[1])", "extern R_MRAM_Type test_mram;",
                "R_MRAM_Type * test_mram_access(void);", "#define R_MRAM (test_mram_access())",
                "extern R_WDT0_Type test_wdt[2];",
               "#define R_WDT0 (&test_wdt[0])", "#define R_WDT1 (&test_wdt[1])",
               "#define R_WDT R_WDT0", "extern R_ICU_Type test_icu;", "#define R_ICU (&test_icu)",
               "extern R_TSN_Type test_tsn;", "#define R_TSN_CTRL (&test_tsn)",
               "extern R_TSD_Type test_tsd;", "#define R_TSN_CAL (&test_tsd)",
               "extern R_SYSC_Type test_system;", "#define R_SYSTEM (&test_system)"]
    # Use the production compatibility aliases; never synthesize missing ones.
    if not legacy_adc:
        result += re.findall(r"^#define R_ADC_B0_\w+[^\n]+", text, re.M)
    result += ["extern uint32_t test_ofs[4];",
               "#define R_OFS0 ((volatile const uint32_t *) &test_ofs[0])",
               "#define R_OFS3 ((volatile const uint32_t *) &test_ofs[1])",
               "#define R_OFS3_SEC ((volatile const uint32_t *) &test_ofs[2])",
               "#define R_OFS3_SEL ((volatile const uint32_t *) &test_ofs[3])"]
    return "\n".join(result) + "\n"


def adc_types_header():
    """Require production RA8P1 types even in the simulated behavior tests."""
    return (FSP / "src/bsp/mcu/ra8p1/r_adc_device_types.h").read_text()


class AnalogWatchdogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cc = shutil.which("clang") or shutil.which("cc")
        if not cls.cc:
            raise RuntimeError("C compiler required")

    def compile_run(self, driver, flags=(), legacy_adc=False, compile_only=False):
        with tempfile.TemporaryDirectory(prefix="analog-wdt-") as tmp:
            tmp = Path(tmp)
            (tmp / "analog_registers.h").write_text(register_header(legacy_adc))
            types = (FSP / "src/bsp/mcu/ra6t2/r_adc_device_types.h").read_text() if legacy_adc else adc_types_header()
            (tmp / "r_adc_device_types.h").write_text(types)
            for name in ("r_dac_b_cfg.h", "r_adc_b_cfg.h", "r_wdt_cfg.h", "bsp_cfg.h"):
                (tmp / name).write_text("#pragma once\n")
            args = [self.cc, "-std=c11", "-O1", "-g", "-Wall", "-Wextra", "-Werror",
                    "-Wno-unused-const-variable", "-Wno-int-to-pointer-cast",
                    "-fsanitize=undefined,address", "-fno-omit-frame-pointer",
                    "-I", str(tmp), "-I", str(FIXTURE), "-I", str(FSP / "inc"),
                    "-I", str(FSP / "inc/api"), "-I", str(FSP / "inc/instances"),
                    "-I", str(FSP / "src/bsp/mcu/ra6t2"),
                    *flags,
                    str(FSP / f"src/r_{driver}/r_{driver}.c")]
            if compile_only:
                args += ["-fsyntax-only"]
            else:
                args += [str(FIXTURE / f"{driver}_harness.c"), str(FIXTURE / "host.c"),
                         "-o", str(tmp / "test")]
            compiled = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            if compile_only:
                return
            run = subprocess.run([str(tmp / "test")], capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("passed", run.stdout)

    def test_dac_behavior(self):
        for checking in (0, 1):
            with self.subTest(checking=checking):
                self.compile_run("dac_b", [f"-DDAC_B_CFG_PARAM_CHECKING_ENABLE={checking}"])

    def test_adc_behavior(self):
        for checking in (0, 1):
            with self.subTest(checking=checking):
                self.compile_run("adc_b", [f"-DADC_B_CFG_PARAM_CHECKING_ENABLE={checking}"])

    def test_legacy_adc_compiles(self):
        for checking in (0, 1):
            with self.subTest(checking=checking):
                self.compile_run("adc_b", ["-DBSP_FEATURE_ADC_B_CLOCK_SOURCE_ADCCLK=0",
                                           f"-DADC_B_CFG_PARAM_CHECKING_ENABLE={checking}"],
                                 legacy_adc=True, compile_only=True)

    def test_wdt_both_cores_and_legacy(self):
        for count, core in ((2, 0), (2, 1), (1, 0)):
            for checking in (0, 1):
                for nmi in (0, 1):
                    with self.subTest(count=count, core=core, checking=checking, nmi=nmi):
                        self.compile_run("wdt", [f"-DBSP_FEATURE_WDT_INSTANCE_COUNT={count}",
                                                f"-DBSP_CFG_CPU_CORE={core}",
                                                f"-DWDT_CFG_PARAM_CHECKING_ENABLE={checking}",
                                                f"-DWDT_CFG_REGISTER_START_NMI_SUPPORTED={nmi}"])

    def test_wdt_runtime_options(self):
        for core, selection in ((0, "ofs0"), (1, "secure"), (1, "nonsecure"), (1, "mixed")):
            for nmi in (0, 1):
                for checking in (0, 1):
                    value = 0x4F100000 | (0 if nmi else 0x10000000)
                    options = [0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0]
                    if core == 0:
                        options[0] = value
                    elif selection == "secure":
                        options[2] = value
                    elif selection == "nonsecure":
                        options[1], options[3] = value, 0x5FFE0000
                    else:
                        # Start/timeout from SEC, clock/reset from nonsecure.
                        # Neither complete option word describes the result.
                        options[1] = value | 0x20000
                        options[2] = 0x4F400000 | (0x10000000 if nmi else 0)
                        options[3] = 0x10F00000
                    with self.subTest(core=core, selection=selection, nmi=nmi, checking=checking):
                        self.compile_run("wdt", [f"-DBSP_CFG_CPU_CORE={core}",
                            f"-DWDT_CFG_PARAM_CHECKING_ENABLE={checking}",
                            "-DWDT_CFG_REGISTER_START_NMI_SUPPORTED=0", "-DTEST_AUTOSTART=1",
                            f"-DTEST_AUTOSTART_NMI={nmi}",
                            *[f"-DTEST_{name}={value}U" for name, value in zip(
                                ("OFS0", "OFS3", "OFS3_SEC", "OFS3_SEL"), options)]])

    def test_wdt_legacy_autostart(self):
        for nmi in (0, 1):
            value = 0x4F100000 | (0 if nmi else 0x10000000)
            self.compile_run("wdt", ["-DBSP_FEATURE_WDT_INSTANCE_COUNT=1",
                f"-DBSP_CFG_ROM_REG_OFS0={value}U", "-DTEST_AUTOSTART=1",
                f"-DTEST_AUTOSTART_NMI={nmi}", "-DWDT_CFG_PARAM_CHECKING_ENABLE=1",
                "-DWDT_CFG_REGISTER_START_NMI_SUPPORTED=0"])

    def test_production_arm_compile(self):
        candidates = [os.environ.get("ARM_NONE_EABI_GCC")] if os.environ.get("ARM_NONE_EABI_GCC") else [
            shutil.which("arm-none-eabi-gcc"),
            *map(str, sorted(Path("/Applications/ArmGNUToolchain").glob(
                "*/arm-none-eabi/bin/arm-none-eabi-gcc"), reverse=True))]
        compiler = next((path for path in candidates if path and subprocess.run(
            [path, "-E", "-xc", "-include", "assert.h", os.devnull],
            capture_output=True).returncode == 0), None)
        self.assertIsNotNone(compiler, "Complete Arm GCC required; set ARM_NONE_EABI_GCC")
        cmsis = list((ROOT / "baseline/ra").rglob("core_cm85.h"))
        if not cmsis:
            cmsis = list((ROOT / "validation/projects/original_ra8m1/ra").rglob("core_cm85.h"))
        self.assertTrue(cmsis, "Preserved FSP 5.9 CMSIS headers required")
        with tempfile.TemporaryDirectory(prefix="ra8p1-analog-arm-") as directory:
            directory = Path(directory)
            # Only project configuration is rendered here. BSP, device, API and
            # instance headers all come from production paths, never host fixtures.
            core_configs = {}
            for path in MDF.glob("*ra8p1*.xml"):
                for config in ET.parse(path).getroot().findall("config"):
                    content = config.find("content")
                    if content is None:
                        continue
                    text = content.text or ""
                    for prop in config.findall("property"):
                        default = prop.get("default", "")
                        option = next((o for o in prop.findall("option") if o.get("id") == default), None)
                        value = option.get("value") if option is not None else default
                        text = text.replace("${" + prop.get("id") + "}", value)
                    if "${" not in text:
                        text = text.replace("../../../ra/", str(ROOT / "baseline/ra") + "/")
                        name = Path(config.get("path")).name
                        if name == "bsp_mcu_core_cfg.h":
                            core_configs[config.get("id")] = text
                        else:
                            (directory / name).write_text(text)
            (directory / "bsp_cfg.h").write_text('''
#include "bsp_mcu_family_cfg.h"
#include "board_cfg.h"
#include "vector_data.h"
#define BSP_ICU_VECTOR_NUM_ENTRIES 96
#define BSP_CFG_RTOS 0
#define BSP_CFG_ASSERT 0
#define BSP_CFG_STACK_MAIN_BYTES 1024
#define BSP_CFG_HEAP_BYTES 0
#define BSP_CFG_PFS_PROTECT 1
#define BSP_CFG_C_RUNTIME_INIT 1
#define BSP_CFG_EARLY_INIT 0
#define BSP_CFG_STARTUP_CLOCK_REG_NOT_RESET 0
''')
            (directory / "vector_data.h").write_text("#define VECTOR_DATA_IRQ_COUNT 0\n")
            for driver in ("adc_b", "dac_b", "wdt"):
                prefix = driver.upper()
                (directory / f"r_{driver}_cfg.h").write_text(
                    f"#define {prefix}_CFG_PARAM_CHECKING_ENABLE BSP_CFG_PARAM_CHECKING_ENABLE\n" +
                    ("#define WDT_CFG_REGISTER_START_NMI_SUPPORTED 1\n" if driver == "wdt" else ""))
            paths = [directory, FSP / "inc", FSP / "inc/api",
                     FSP / "inc/instances", cmsis[0].parent]
            contract = directory / "contract.c"
            contract.write_text('''
#include "r_adc_b.h"
#include "r_dac_b.h"
#include "r_wdt.h"
#include <stddef.h>
_Static_assert(ADC_B_VIRTUAL_CHANNEL_COUNT == 33, "virtual channels");
_Static_assert(ADC_CHANNEL_LAST_EXTERNAL == 22, "physical channels");
_Static_assert(ADC_CHANNEL_TEMPERATURE == 100 && ADC_CHANNEL_DA0 == 104, "extended channels");
_Static_assert(ADC_CHANNEL_LAST_INTERNAL == 118, "last extended channel");
_Static_assert(BSP_FEATURE_ADC_B_CLOCK_SOURCE_ADCCLK == 1, "ADC clock variant");
_Static_assert(BSP_FEATURE_ADC_B_UNIT_0_CHANNELS == 0x00070371007F503FULL, "ADC0 mask");
_Static_assert(BSP_FEATURE_ADC_B_UNIT_1_CHANNELS == 0x00700372007FAFC0ULL, "ADC1 mask");
_Static_assert(BSP_FEATURE_WDT_INSTANCE_COUNT == 2, "watchdogs");
_Static_assert(R_ADC_B0_ADFIFODCR_FIFODCn_Msk == 0x1FFU, "FIFO clear mask");
_Static_assert(R_ADC_B0_ADOVFCHSCR0_OVFCHCn_Msk == 0x7FFFFFU, "overflow clear mask");
_Static_assert(R_TSN_CTRL_BASE == R_TSN_BASE && R_TSN_CAL_BASE == R_TSD_BASE, "TSN aliases");
_Static_assert(offsetof(R_ADC_B_Type, ADDR0) == 0x2000, "ADC data offset");
''')
            with contract.open("a") as stream:
                for index in range(8):
                    stream.write(f'_Static_assert(R_ADC_B0_ADLIMTR{index}_LIMU_Pos == 16U, "limiter shift");\n')
                for index in range(4):
                    stream.write(f'_Static_assert(R_ADC_B0_ADDFSR1_DFSEL{index}_Pos == {8 * index}U, "filter shift");\n')
                    stream.write(f'_Static_assert(R_ADC_B0_ADDFSR1_DFSEL{index}_Msk == (3UL << {8 * index}), "filter mask");\n')
                for name, address in (("OFS0", 0x02C9F040), ("OFS3", 0x12C9F4C4),
                                      ("OFS3_SEC", 0x02C9F0C4), ("OFS3_SEL", 0x02C9F124)):
                    stream.write(f'_Static_assert(R_{name}_ADDRESS == {address}UL, "option address");\n')
                    stream.write(f'_Static_assert(__builtin_types_compatible_p(__typeof__(*R_{name}), volatile const uint32_t), "read-only option");\n')
            generated = ROOT / "validation/projects/qual_analog/ra_gen/hal_data.c"
            sources = {driver: FSP / f"src/r_{driver}/r_{driver}.c" for driver in ("adc_b", "dac_b", "wdt")}
            sources["contract"] = contract
            if generated.exists():
                # Compile the failing real configurator output without editing its
                # .cproject, sources, or injecting a family include directory.
                sources["generated_hal_data"] = generated
                paths += [generated.parent,
                          generated.parent.parent / "ra_cfg/fsp_cfg",
                          generated.parent.parent / "ra_cfg/fsp_cfg/bsp"]
            for core, cpu, fpu in ((0, "cortex-m85", "fpv5-d16"), (1, "cortex-m33", "fpv5-sp-d16")):
                (directory / "bsp_mcu_core_cfg.h").write_text(core_configs[f"config.bsp.ra8p1.cpu{core}"])
                for checking in (0, 1):
                    for driver, source in sources.items():
                        with self.subTest(core=core, checking=checking, driver=driver):
                            command = [compiler, "-std=c11", "-mthumb", "-mcpu=" + cpu,
                                       "-mfloat-abi=hard", "-mfpu=" + fpu,
                                       "-Wall", "-Wextra", "-Werror", "-DBSP_CFG_CPU_CORE=" + str(core),
                                       "-DBSP_CFG_PARAM_CHECKING_ENABLE=" + str(checking)]
                            command += ["-I" + str(path) for path in paths]
                            command += ["-c", str(source),
                                        "-o", str(directory / f"{driver}-{core}-{checking}.o")]
                            result = subprocess.run(command, capture_output=True, text=True)
                            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_metadata_is_well_formed(self):
        for driver in ("dac_b", "adc_b", "wdt"):
            ET.parse(MDF / f"Renesas##HAL Drivers##all##r_{driver}####5.9.0.xml")

    def test_adc_metadata_resolves_all_enumerations(self):
        adc = ET.parse(MDF / "Renesas##HAL Drivers##all##r_adc_b####5.9.0.xml")
        bsp = ET.parse(MDF / "Renesas##BSP##ra8p1##fsp####5.9.0.xml")
        definitions = bsp.findall(".//enum")
        enums = {element.get("id"): element for element in definitions}
        self.assertEqual(len(definitions), len(enums), "Duplicate enum IDs")
        references = {element.get("enum") for element in adc.findall(".//select")}
        self.assertFalse(references - enums.keys(), "ADC_B enum references must resolve on RA8P1")
        for ident in references:
            with self.subTest(enum=ident):
                enum = enums[ident]
                options = enum.findall("option")
                ids = [option.get("id") for option in options]
                self.assertTrue(options)
                self.assertEqual(len(ids), len(set(ids)))
                if enum.get("default"):
                    self.assertIn(enum.get("default"), ids)
        for prop in adc.findall(".//property"):
            selection = prop.find("select")
            if selection is not None and prop.get("default"):
                options = enums[selection.get("enum")].findall("option")
                self.assertIn(prop.get("default"), [option.get("id") for option in options],
                              prop.get("id"))

    def test_adc_metadata_preserves_target_limits(self):
        bsp = ET.parse(MDF / "Renesas##BSP##ra8p1##fsp####5.9.0.xml")
        enums = {element.get("id"): element for element in bsp.findall(".//enum")}
        values = lambda ident: {option.get("value") for option in enums[ident].findall("option")}
        self.assertEqual(values("enum.driver.adc.adc_clock_source"),
                         {"ADC_B_CLOCK_SOURCE_ADCCLK", "ADC_B_CLOCK_SOURCE_GPT", "ADC_B_CLOCK_SOURCE_PCLKA"})
        self.assertEqual(values("enum.driver.adc.adc_clock_divider"),
                         {"ADC_B_CLOCK_DIV_" + str(divisor) for divisor in range(1, 9)})
        types = adc_types_header()
        channels = set(re.findall(r"^\s*(ADC_CHANNEL_\w+)\s*=", types, re.M))
        channels -= {"ADC_CHANNEL_LAST_EXTERNAL", "ADC_CHANNEL_FIRST_INTERNAL", "ADC_CHANNEL_LAST_INTERNAL"}
        self.assertEqual(len(channels), 36)
        header_symbols = set(re.findall(r"\bADC_(?:B_)?[A-Z][A-Z0-9_]*\b",
                                       types + (FSP / "inc/instances/r_adc_b.h").read_text()))
        for ident, enum in enums.items():
            for option in enum.findall("option"):
                symbols = set(re.findall(r"\bADC_(?:B_)?[A-Z][A-Z0-9_]*\b", option.get("value", "")))
                self.assertFalse(symbols - header_symbols, (ident, symbols - header_symbols))
        for channel in range(37):
            with self.subTest(channel=channel):
                groups = values(f"enum.mcu.adc_b.virtual_channel_{channel}_scan_groups")
                expected = {f"({group})" for group in range(10)} if channel < 33 else {"(0)"}
                self.assertEqual(groups, expected, "Channels 33..36 have no RA8P1 register")
                inputs = values(f"enum.mcu.adc.virtual_channel_{channel}_channel_select")
                self.assertEqual(inputs, channels if channel < 33 else {"ADC_CHANNEL_0"})
        for group in range(9):
            self.assertEqual(values(f"enum.mcu.adc.scan_group_{group}_gpt_trigger_enable_mask"),
                             {f"ADC_B_GPT_TRIGGER_{request}{channel}"
                              for request in ("A", "B") for channel in range(14)})
        self.assertEqual(values("enum.mcu.nvic.priorities.mandatory"),
                         {f"({priority})" for priority in range(16)})
        self.assertEqual(values("enum.mcu.nvic.priorities.optional"),
                         {f"({priority})" for priority in range(16)} | {"(BSP_IRQ_DISABLED)"})

    def test_analog_spec_enum_selections(self):
        bsp = ET.parse(MDF / "Renesas##BSP##ra8p1##fsp####5.9.0.xml")
        enums = {element.get("id"): element for element in bsp.findall(".//enum")}
        adc = ET.parse(MDF / "Renesas##HAL Drivers##all##r_adc_b####5.9.0.xml")
        properties = {element.get("id"): element for element in adc.findall(".//property")}
        spec = json.loads((ROOT / "tests/ra8p1/analog.json").read_text())
        module = next(module for module in spec["modules"] if module["id"] == "module.driver.adc_on_adc_b")
        for ident, value in module["properties"].items():
            with self.subTest(property=ident):
                self.assertIn(ident, properties)
                prop = properties[ident]
                selection = prop.find("select")
                options = prop.findall("option") if selection is None else enums[selection.get("enum")].findall("option")
                if options and value:
                    self.assertIn(value, {option.get("id") for option in options})

    def test_ra8p1_metadata_schema(self):
        validator = shutil.which("xmllint")
        if validator is None:
            self.skipTest("xmllint is required for FSP XSD validation")
        schema = ROOT / "validation/schema/ddscModuleDescription.xsd"
        if not schema.exists():
            self.skipTest("Preserved FSP XSD is not installed")
        environment = dict(os.environ, XML_CATALOG_FILES=str(schema.parent / "catalog.xml"))
        result = subprocess.run([validator, "--noout", "--schema", str(schema),
                                 str(MDF / "Renesas##BSP##ra8p1##fsp####5.9.0.xml")],
                                env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_dac_metadata_module_order_matches_59(self):
        module = ET.parse(MDF / "Renesas##HAL Drivers##all##r_dac_b####5.9.0.xml").find("module")
        tags = [child.tag for child in module]
        for driver in ("dac", "dac8"):
            with self.subTest(reference=driver):
                reference = ET.parse(MDF / f"Renesas##HAL Drivers##all##r_{driver}####5.9.0.xml").find("module")
                order = list(dict.fromkeys(child.tag for child in reference))
                self.assertTrue(set(tags).issubset(order))
                positions = [order.index(tag) for tag in tags]
                self.assertEqual(positions, sorted(positions), "FSP module elements must follow the 5.9 schema order")


if __name__ == "__main__":
    unittest.main()
