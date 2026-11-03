#!/usr/bin/env python3
"""Compile/link actual ARM fixtures against the packaged FSP linker script.

No generated project, installed pack or target is modified. Temporary images
exercise both absolute core symbols and unsafe payload rejection.
"""
from io import BytesIO
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "packaging/ra8p1/mcu/script/fsp.ld"
PARTITIONS = {
    0: ((0x02000000, 0x02080000), (0x22000000, 0x220C0000)),
    1: ((0x02080000, 0x020FF000), (0x220C0000, 0x22180000)),
}
MEMORY = "FLASH_START = 0x02000000; FLASH_LENGTH = 0x100000;\nRAM_START = 0x22000000; RAM_LENGTH = 0x1A0000;\n"
ASSEMBLY = """.syntax unified
.thumb
.section .fixed_vectors,"a",%progbits
.global __Vectors
__Vectors:
.word __StackTop
.word Reset_Handler
.section .text.Reset_Handler,"ax",%progbits
.global Reset_Handler
.type Reset_Handler,%function
.thumb_func
Reset_Handler:
ldr r0, =fixture_data
ldr r1, =fixture_bss
b .
.ltorg
.section .data,"aw",%progbits
fixture_data: .word 0x12345678
.section .bss.fixture,"aw",%nobits
fixture_bss: .space 24
.section .stack,"aw",%nobits
.space 256
"""
CORE_SOURCE = """#define STRING_(x) #x
#define STRING(x) STRING_(x)
__asm__(".global __ra8p1_core\\n.equ __ra8p1_core," STRING(CORE));
"""


def run(args, cwd=None):
    return subprocess.run([str(arg) for arg in args], cwd=cwd, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)


def compiler():
    override = os.environ.get("ARM_NONE_EABI_GCC")
    candidates = [Path(override)] if override else []
    if not override:
        path = shutil.which("arm-none-eabi-gcc")
        if path:
            candidates.append(Path(path))
        candidates.extend(sorted(Path("/Applications").rglob("arm-none-eabi-gcc"),
                                 key=lambda p: ("Renesas" not in str(p), str(p))))
    for candidate in candidates:
        if not candidate.is_file():
            continue
        if not candidate.with_name("arm-none-eabi-readelf").is_file():
            continue
        # Some installations contain only a compiler, not the required libraries.
        if all((result := run([candidate, "-print-file-name=" + name])).returncode == 0
               and Path(result.stdout.strip()).is_file() for name in ("libc.a", "libgcc.a", "libm.a")):
            return candidate.resolve()
    raise RuntimeError("No complete ARM GNU toolchain; set ARM_NONE_EABI_GCC")


class LinkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cc = compiler()
        cls.readelf = cls.cc.with_name("arm-none-eabi-readelf")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ra8p1-linker-")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)

    def build(self, core=0, extra="", memory=MEMORY, core_source=CORE_SOURCE, gc=True):
        flags = ["-mcpu=" + ("cortex-m33" if core == 1 else "cortex-m85"), "-mthumb"]
        (self.work / "memory_regions.ld").write_text(memory)
        (self.work / "fixture.S").write_text(ASSEMBLY + extra)
        (self.work / "core.c").write_text(core_source)
        objects = []
        for source in ("fixture.S", "core.c"):
            obj = self.work / (source + ".o")
            result = run([self.cc, *flags, "-DCORE=" + str(core), "-c", source, "-o", obj], self.work)
            self.assertEqual(result.returncode, 0, result.stdout)
            objects.append(obj)
        self.elf = self.work / "fixture.elf"
        command = [self.cc, *flags, "-nostdlib", "-L", self.work, "-T", SCRIPT,
                   *objects, "-Wl,-Map,fixture.map", "-o", self.elf]
        if gc:
            command.append("-Wl,--gc-sections")
        return run(command, self.work)

    def assert_rejected(self, result, message=None):
        self.assertNotEqual(result.returncode, 0, "Unsafe input linked successfully")
        if message:
            self.assertIn(message, result.stdout)

    def assert_safe_loads(self, core):
        result = run([self.readelf, "-lW", self.elf])
        self.assertEqual(result.returncode, 0, result.stdout)
        loads = re.findall(r"^\s*LOAD\s+(0x\w+)\s+(0x\w+)\s+(0x\w+)\s+(0x\w+)\s+(0x\w+)",
                           result.stdout, re.MULTILINE)
        self.assertTrue(loads, result.stdout)
        for _, virtual, physical, _, size in loads:
            size = int(size, 16)
            if not size:
                continue
            for address in (int(virtual, 16), int(physical, 16)):
                self.assertTrue(any(start <= address and address + size <= end
                                    for start, end in PARTITIONS[core]), result.stdout)
        self.assertEqual(int(loads[0][1], 16), PARTITIONS[core][0][0])

    def test_absolute_object_symbol_selects_each_core_partition(self):
        for core in (0, 1):
            with self.subTest(core=core):
                result = self.build(core)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assert_safe_loads(core)
                symbols = run([self.readelf, "-sW", self.elf]).stdout
                self.assertRegex(symbols, r"\b" + f"{core:08x}" + r"\s+0\s+NOTYPE\s+GLOBAL\s+DEFAULT\s+ABS\s+__ra8p1_core\b")

    def test_scratch_boundaries_are_absolute_and_outside_loads(self):
        for core in (0, 1):
            with self.subTest(core=core):
                result = self.build(core)
                self.assertEqual(result.returncode, 0, result.stdout)
                symbols = run([self.readelf, "-sW", self.elf]).stdout
                for name, address in (("__mram_scratch_start__", 0x020FF000),
                                      ("__mram_scratch_end__", 0x02100000)):
                    self.assertRegex(symbols, rf"\b{address:08x}\s+0\s+NOTYPE\s+GLOBAL\s+DEFAULT\s+ABS\s+{name}\b")
                self.assert_safe_loads(core)

    def test_absolute_symbol_pulls_bsp_object_from_archive(self):
        result = self.build(1)
        self.assertEqual(result.returncode, 0, result.stdout)
        archive = self.work / "libbsp.a"
        result = run([self.cc.with_name("arm-none-eabi-ar"), "rcs", archive, self.work / "core.c.o"])
        self.assertEqual(result.returncode, 0, result.stdout)
        result = run([self.cc, "-mcpu=cortex-m33", "-mthumb", "-nostdlib", "-L", self.work,
                      "-T", SCRIPT, self.work / "fixture.S.o", "-lbsp", "-Wl,--gc-sections",
                      "-o", self.elf], self.work)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assert_safe_loads(1)

    def test_missing_core_has_no_cpu0_fallback(self):
        self.assert_rejected(self.build(core_source=""), "RA8P1 BSP must define absolute __ra8p1_core")

    def test_invalid_core_symbol_is_rejected(self):
        for core in (-1, 2, 255):
            with self.subTest(core=core):
                self.assert_rejected(self.build(core), "RA8P1 __ra8p1_core must be 0 or 1")

    def test_c_variable_is_not_an_absolute_core_symbol(self):
        self.assert_rejected(self.build(core_source="int __ra8p1_core = 0;\n"))

    def test_minimal_fsp_memory_file_needs_no_optional_constants(self):
        self.assertEqual(set(re.findall(r"([A-Z_]+)\s*=", MEMORY)),
                         {"FLASH_START", "FLASH_LENGTH", "RAM_START", "RAM_LENGTH"})
        result = self.build(memory=MEMORY)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assert_safe_loads(0)

    @staticmethod
    def original_linker():
        archive = ROOT / "reference/vendor/FSP_Packs_v5.9.0.zip"
        with ZipFile(archive) as outer:
            with ZipFile(BytesIO(outer.read("internal/projectgen/ra/packs/Renesas.RA_mcu_ra8m1.5.9.0.pack"))) as pack:
                return pack.read("script/fsp.ld").decode("utf-8")

    def test_original_59_missing_optional_region_failure_is_resolved(self):
        original = self.work / "original-fsp.ld"
        original.write_text(self.original_linker())
        for core in (0, 1):
            with self.subTest(core=core):
                result = self.build(core)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assert_safe_loads(core)
                result = run([self.cc, "-mcpu=" + ("cortex-m33" if core else "cortex-m85"),
                              "-mthumb", "-nostdlib", "-L", self.work, "-T", original,
                              self.work / "fixture.S.o", self.work / "core.c.o",
                              "-Wl,--gc-sections", "-o", self.work / "original.elf"], self.work)
                self.assert_rejected(result, "undefined symbol `OPTION_SETTING_LENGTH'")
                self.assertIn("invalid length for memory region QSPI_FLASH", result.stdout)

    def test_original_59_general_sections_are_preserved(self):
        pattern = r"^\s*(\.[A-Za-z_][\w.]*)[^\n:]*:"
        original = set(re.findall(pattern, self.original_linker(), re.MULTILINE))
        current = set(re.findall(pattern, SCRIPT.read_text(), re.MULTILINE))
        forbidden = {name for name in original if name == ".id_code" or name.startswith(".option_setting")}
        self.assertTrue(forbidden)
        self.assertEqual(original - current, forbidden)
        self.assertEqual(current - original, {".ra8p1_forbidden_security"})

    def test_every_disabled_memory_region_is_zero_length(self):
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stdout)
        text = (self.work / "fixture.map").read_text()
        for name in ("ITCM", "DTCM", "DATA_FLASH", "QSPI_FLASH", "OSPI_DEVICE_0",
                     "OSPI_DEVICE_1", "OSPI_DEVICE_0_RAM", "OSPI_DEVICE_1_RAM", "SDRAM",
                     "OPTION_SETTING", "OPTION_SETTING_OFS", "OPTION_SETTING_SAS",
                     "OPTION_SETTING_S", "OPTION_SETTING_DATA_FLASH_S", "ID_CODE"):
            self.assertRegex(text, r"(?m)^" + name + r"\s+0x0+\s+0x0+\b")

    def test_nonzero_unsupported_region_lengths_are_rejected(self):
        names = ("ITCM_LENGTH", "DTCM_LENGTH", "DATA_FLASH_LENGTH", "QSPI_FLASH_LENGTH",
                 "QSPI_FLASH_SIZE", "OSPI_DEVICE_0_LENGTH", "OSPI_DEVICE_0_SIZE",
                 "OSPI_DEVICE_1_LENGTH", "OSPI_DEVICE_1_SIZE", "SDRAM_LENGTH",
                 "OPTION_SETTING_LENGTH", "OPTION_SETTING_S_LENGTH", "ID_CODE_LENGTH",
                 "OPTION_SETTING_DATA_FLASH_S_LENGTH")
        for name in names:
            with self.subTest(symbol=name):
                self.assert_rejected(self.build(memory=MEMORY + name + " = 8;\n"))

    def test_security_payloads_cannot_be_discarded_by_gc(self):
        for core in (0, 1):
            for section in (".option_setting_ofs0", ".option_setting_sas", ".id_code",
                            ".rom_registers", ".security.configuration"):
                with self.subTest(core=core, section=section):
                    extra = f'.section {section},"a",%progbits\n.word 0x12345678\n'
                    self.assert_rejected(self.build(core, extra), "forbids option and security payloads")

    def test_unsupported_memory_payloads_are_rejected(self):
        for section in (".itcm_data", ".dtcm_data", ".dtcm_bss", ".qspi_flash", ".code_in_qspi",
                        ".qspi_non_retentive", ".ospi_device_0", ".ospi_device_1",
                        ".ospi_device_0_no_load", ".ospi_device_1_no_load",
                        ".ospi_device_0_non_retentive", ".ospi_device_1_non_retentive",
                        ".sdram", ".frame", ".nocache_sdram", ".data_flash"):
            with self.subTest(section=section):
                extra = f'.section {section},"aw",%progbits\n.word 0x12345678\n'
                self.assert_rejected(self.build(extra=extra))

    def test_trustzone_settings_and_gateway_payloads_are_rejected(self):
        for name in ("PROJECT_SECURE", "PROJECT_NONSECURE", "RAM_NS_BUFFER_LENGTH"):
            with self.subTest(symbol=name):
                self.assert_rejected(self.build(memory=MEMORY + name + " = 1;\n"),
                                     "does not support TrustZone image partitioning")
        self.assert_rejected(self.build(extra='.section .gnu.sgstubs,"ax",%progbits\n.word 1\n'),
                             "does not support TrustZone image partitioning")

    def test_flash_overflow_cannot_reach_other_core_or_scratch(self):
        for core in (0, 1):
            with self.subTest(core=core):
                size = PARTITIONS[core][0][1] - PARTITIONS[core][0][0]
                extra = f'.section .text.Reset_Handler,"ax",%progbits\n.space {size}\n'
                self.assert_rejected(self.build(core, extra), "overflowed")

    def test_ram_overflow_cannot_reach_other_core_or_shared(self):
        for core in (0, 1):
            with self.subTest(core=core):
                extra = '.section .stack,"aw",%nobits\n.space 0xc0000\n'
                self.assert_rejected(self.build(core, extra), "overflowed")

    def test_initialized_ram_load_image_cannot_overflow_flash(self):
        for core in (0, 1):
            with self.subTest(core=core):
                size = PARTITIONS[core][0][1] - PARTITIONS[core][0][0]
                extra = f'.section .data,"aw",%progbits\n.space {size}\n'
                self.assert_rejected(self.build(core, extra), "overflowed")

    def test_image_override_cannot_bypass_core_partition(self):
        for core in (0, 1):
            for setting in ("FLASH_IMAGE_START = 0x020FF000;", "FLASH_IMAGE_LENGTH = 0x100000;"):
                with self.subTest(core=core, setting=setting):
                    self.assert_rejected(self.build(core, memory=MEMORY + setting),
                                         "must use its exact core MRAM partition")


if __name__ == "__main__":
    unittest.main()
