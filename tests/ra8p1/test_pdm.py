"""Compile and exercise the candidate PDM C driver, without target hardware.

The MMIO fixture uses exact SVD offsets. Each injected FIFO burst has a constant
sample word; silicon's read-pop and asynchronous pointer synchronization are not
emulated. These tests establish software control flow, bounds and conversions,
not microphone audio quality or target timing.
"""
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "baseline"
FIXTURE = Path(__file__).parent / "fixtures" / "pdm"
MDF = BASE / "module_descriptions" / "Renesas##HAL Drivers##all##r_pdm####5.9.0.xml"


class PdmTests(unittest.TestCase):
    def test_c_driver(self):
        compiler = shutil.which("cc")
        self.assertIsNotNone(compiler, "A C11 compiler is required for PDM host tests")
        for checking in (0, 1):
            for secure in (0, 1):
                with self.subTest(parameter_checks=checking, secure=secure), tempfile.TemporaryDirectory() as tmp:
                    exe = Path(tmp) / "pdm-test"
                    cmd = [compiler, "-std=c11", "-Wall", "-Wextra", "-Wshadow", "-Werror", "-pedantic",
                           "-fsanitize=undefined", "-fno-sanitize-recover=all", "-g",
                           f"-DPDM_CFG_PARAM_CHECKING_ENABLE={checking}", f"-DBSP_TZ_SECURE_BUILD={secure}",
                           "-include", str(FIXTURE / "bsp_api.h"),
                           "-I", str(FIXTURE), "-I", str(BASE / "ra/fsp/inc/api"),
                           "-I", str(BASE / "ra/fsp/inc/instances"), "-I", str(BASE / "ra/fsp/inc"),
                           "-I", str(BASE / "ra/fsp/src/bsp/mcu/all"),
                           str(BASE / "ra/fsp/src/r_pdm/r_pdm.c"), str(FIXTURE / "test_pdm.c"),
                           "-o", str(exe)]
                    compiled = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                    self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
                    ran = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
                    self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
                    self.assertIn("PDM host paths passed", ran.stdout)

    def test_production_warning_flags(self):
        # Reuse the BSP's real-header/core matrix without changing its shared test file.
        spec = importlib.util.spec_from_file_location("pdm_bsp_checks", ROOT / "tests/ra8p1/test_bsp.py")
        bsp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bsp)
        run = bsp.run
        compiled = []
        warning_flags = ("-Wunused", "-Wuninitialized", "-Wmissing-declarations", "-Wconversion",
                         "-Wpointer-arith", "-Wshadow", "-Wlogical-op", "-Waggregate-return", "-Wfloat-equal")

        def compile_with_warnings(*args):
            if "-c" in args and str(BASE / "ra/fsp/src/r_pdm/r_pdm.c") in args:
                compiled.append(args)
                args = (*args, *warning_flags)
            return run(*args)

        with patch.object(bsp, "run", side_effect=compile_with_warnings):
            bsp.BspTests("test_compile_bsp_both_cores").test_compile_bsp_both_cores()
        for cpu in ("cortex-m85", "cortex-m33"):
            for checking in (0, 1):
                self.assertTrue(any(f"-mcpu={cpu}" in args and
                                    f"-DPDM_CFG_PARAM_CHECKING_ENABLE={checking}" in args
                                    for args in compiled))

    def test_svd_register_contract(self):
        with (ROOT / ".embedder/svd-cache/R7KA8P1AD.json").open() as stream:
            device = json.load(stream)
        pdm = next(p for p in device["peripherals"] if p["name"] == "PDMIF")
        regs = {r["name"]: r for r in pdm["registers"]}
        expected = {"PDCSTRTR": 0x00, "PDCSTPTR": 0x04, "PDCDRCR": 0x24,
                    "PDICRCH0-2": 0x10C, "PDSDCRCH0-2": 0x110, "PDSRCH0-2": 0x114,
                    "PDSCRCH0-2": 0x118, "PDMDSRCH0-2": 0x120, "PDSFCRCH0-2": 0x124,
                    "PDDBCRCH0-2": 0x1C0, "PDDRCRCH0-2": 0x1E0,
                    "PDDRRCH0-2": 0x1E8, "PDDSRCH0-2": 0x1EC}
        for name, offset in expected.items():
            self.assertEqual(regs[name]["addressOffset"], offset, name)
        fields = {f["name"]: f["bitOffset"] for f in regs["PDSRCH0-2"]["fields"]}
        self.assertEqual(fields, {"STATE": 0, "SDF": 1, "DRF": 2, "SCDF": 16,
                                  "OVLDF": 17, "OVUDF": 18, "BFOWDF": 27})
        self.assertEqual(regs["PDSFCRCH0-2"]["resetValue"], 0x057C0000)
        # Check every filter coefficient, not just the endpoints used by the C fixture.
        text = (BASE / "ra/fsp/src/r_pdm/r_pdm.c").read_text()
        initializer = text.split("g_pdm_coefficients[] =", 1)[1].split("};", 1)[0]
        actual = [int(v, 16) for v in re.findall(r"0x[0-9A-F]+", initializer)]
        coefficients = sorted((r for r in pdm["registers"] if 0x128 <= r["addressOffset"] <= 0x1B4),
                              key=lambda r: r["addressOffset"])
        self.assertEqual(actual, [r["resetValue"] for r in coefficients])

    def test_mdf_contract(self):
        tree = ET.parse(MDF)
        module = tree.find("module")
        self.assertEqual(module.attrib["id"], "module.driver.pdm_on_pdm")
        declarations = module.findtext("declarations")
        self.assertIn("g_pdm_on_pdm", declarations)
        self.assertIn("pdm_extended_cfg_t", declarations)
        self.assertIn(".p_context = NULL", declarations)
        interrupts = {(i.attrib["event"], i.attrib["isr"]) for i in module.findall(".//interrupt")}
        self.assertIn(("event.pdm.dat${module.driver.pdm.channel}", "pdm_dat_isr"), interrupts)
        self.assertIn(("event.pdm.err${module.driver.pdm.channel}", "pdm_err_isr"), interrupts)
        self.assertNotIn("transfer", ET.tostring(module, encoding="unicode").lower())

    def test_device_header_contract(self):
        compiler = shutil.which("cc")
        self.assertIsNotNone(compiler)
        device = BASE / "ra/fsp/src/bsp/cmsis/Device/RENESAS/Include"
        source = '#include <stddef.h>\n#include "R7KA8P1AD.h"\n'
        for name, offset in (("PDCSTRTR", 0), ("PDCSTPTR", 4), ("PDCDRCR", 0x24)):
            source += (f'_Static_assert(offsetof(R_PDMIF_Type, {name}) == {offset}, "{name} offset");\n'
                       f'_Static_assert(sizeof(((R_PDMIF_Type *)0)->{name}) == 4, "{name} width");\n')
        source += 'int main(void) { return (uintptr_t)R_PDMIF != 0x40256000U; }\n'
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            for core in ("core_cm33.h", "core_cm85.h"):
                (temp / core).write_text("#define __IM volatile const\n#define __OM volatile\n"
                                         "#define __IOM volatile\n")
            exe = temp / "pdm-layout"
            for core in (0, 1):
                cmd = [compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
                       f"-DBSP_CFG_CPU_CORE={core}", "-I", str(temp), "-I", str(device),
                       "-x", "c", "-", "-o", str(exe)]
                compiled = subprocess.run(cmd, input=source, capture_output=True, text=True, timeout=60)
                self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
                ran = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
                self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)

    def test_bsp_mdf_contract(self):
        descriptions = BASE / "module_descriptions"
        device = ET.parse(descriptions / "Renesas##BSP##ra8p1##device####5.9.0.xml")
        interfaces = {p.attrib["interface"] for p in device.findall(".//provides")}
        module = ET.parse(MDF).find("module")
        for requirement in module.findall("requires"):
            self.assertIn(requirement.attrib["interface"], interfaces)
        events = ET.parse(descriptions / "Renesas##BSP##ra8p1##events####5.9.0.xml")
        events_by_id = {event.attrib["id"]: event.attrib["value"] for event in events.findall(".//event")}
        elc = (BASE / "ra/fsp/src/bsp/mcu/ra8p1/bsp_elc.h").read_text()
        declarations = module.findtext("declarations")
        for channel in range(3):
            replace_channel = lambda text: text.replace("${module.driver.pdm.channel}", str(channel))
            vectors = set(re.findall(r"VECTOR_NUMBER_\w+", replace_channel(declarations)))
            resolved_events = set()
            for interrupt in module.findall(".//interrupt"):
                event_id = replace_channel(interrupt.attrib["event"])
                self.assertIn(event_id, events_by_id)
                value = events_by_id[event_id]
                resolved_events.add(value)
                self.assertRegex(elc, rf"\bELC_EVENT_{value}\s*=")
            self.assertEqual(vectors, {f"VECTOR_NUMBER_{value}" for value in resolved_events})

    def test_mono_input2_binding(self):
        module = ET.parse(MDF).find("module")
        substitute = lambda text: text.replace("${module.driver.pdm.channel}", "2")
        peripheral = module.find("peripheral")
        self.assertEqual(substitute(peripheral.attrib["name"]), "PDM2")
        self.assertEqual(substitute(peripheral.attrib["component"]), "pdm2")
        pinmap = ET.parse(ROOT / "packaging/ra8p1/mcu/.mcu/.pinmapping/PinCfgR7KA8P1KFLCAC.xml")
        component = pinmap.find(".//component[@id='pdm2']")
        self.assertIsNotNone(component)
        for signal, port in (("pdmdat2", "p502"), ("pdmclk2", "p812")):
            self.assertIsNotNone(component.find(f".//alt[@id='pdm2.{signal}.{port}']"))
        primary = module.find("property[@id='module.driver.pdm.ipl']")
        self.assertEqual({substitute(i.attrib["event"]) for i in primary.findall("interrupt")},
                         {"event.pdm.dat2", "event.pdm.err2"})
        declarations = substitute(module.findtext("declarations"))
        self.assertIn(".channel = 2,", declarations)
        self.assertIn("VECTOR_NUMBER_PDM_DAT2", declarations)
        self.assertIn("VECTOR_NUMBER_PDM_ERR2", declarations)
        self.assertEqual(module.find("property[@id='module.driver.pdm.falling_ipl']").attrib["default"],
                         "_disabled")

    def test_bsp_module_stop_contract(self):
        compiler = shutil.which("cc")
        self.assertIsNotNone(compiler)
        source = r"""
#include <assert.h>
#include <stdint.h>
#define FSP_HEADER
#define FSP_FOOTER
#define BSP_MCU_GROUP_RA8P1 1
#define FSP_CRITICAL_SECTION_DEFINE
#define FSP_CRITICAL_SECTION_ENTER
#define FSP_CRITICAL_SECTION_EXIT
#define FSP_REGISTER_READ(reg) ((void) (reg))
#define BSP_CFG_CLOCK_SETTLING_DELAY_US 1U
#define BSP_DELAY_UNITS_MICROSECONDS 0U
#define R_BSP_SoftwareDelay(delay, units) ((void) (delay), (void) (units))
static struct { uint32_t MSTPCRC; } module;
#define R_MSTP (&module)
#include "bsp_module_stop.h"
int main(void)
{
    /* FSP_IP_PDM is deliberately not an enum: these macros use token pasting. */
    module.MSTPCRC = UINT32_MAX;
    R_BSP_MODULE_START(FSP_IP_PDM, 0);
    assert(module.MSTPCRC == (UINT32_MAX & ~(1U << 24U)));
    R_BSP_MODULE_STOP(FSP_IP_PDM, 0);
    assert(module.MSTPCRC == UINT32_MAX);
    return 0;
}
"""
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "pdm-module-stop"
            for delay in (0, 1):
                cmd = [compiler, "-std=c11", "-Wall", "-Wextra", "-Werror", "-pedantic",
                       f"-DBSP_CFG_MSTP_CHANGE_DELAY_ENABLE={delay}",
                       "-I", str(BASE / "ra/fsp/src/bsp/mcu/all"), "-x", "c", "-", "-o", str(exe)]
                compiled = subprocess.run(cmd, input=source, capture_output=True, text=True, timeout=60)
                self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
                ran = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
                self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)


if __name__ == "__main__":
    unittest.main()
