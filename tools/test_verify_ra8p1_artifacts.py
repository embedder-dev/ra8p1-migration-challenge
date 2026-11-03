"""Host-only fixtures for generated-project provenance and ELF safety checks."""
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import verify_ra8p1_artifacts as verify


HEADER = "Class: ELF32\nType: EXEC (Executable file)\nMachine: ARM\n"
LOADS = """Program Headers:
  Type Offset VirtAddr PhysAddr FileSiz MemSiz Flg Align
  LOAD 0x000040 0x02000000 0x02000000 0x00010 0x00010 R E 0x4
  LOAD 0x000060 0x22000000 0x02000010 0x00008 0x00008 RW 0x4
  LOAD 0x000000 0x22000008 0x22000008 0x00000 0x00020 RW 0x4
"""
SECTIONS = """Section Headers:
  [ 0] NULL 00000000 000000 000000 00 0 0 0
  [ 1] .text PROGBITS 02000000 000040 000010 00 AX 0 0 4
  [ 2] .data PROGBITS 22000000 000060 000008 00 WA 0 0 4
  [ 3] .bss NOBITS 22000008 000000 000020 00 WA 0 0 4
  [ 4] .itcm_data PROGBITS 00000000 000068 000000 00 W 0 0 4
  [ 5] .comment PROGBITS 00000000 000068 000010 01 MS 0 0 1
"""
COMMENT = "String dump of section '.comment':\n  [ 0] GCC: (Arm GNU Toolchain 13.2.rel1 (Build arm-13.7)) 13.2.1 20231009\n"
SYMBOLS = """Symbol table '.symtab' contains 2 entries:
   Num:    Value  Size Type    Bind   Vis      Ndx Name
     0: 00000000     0 NOTYPE  LOCAL  DEFAULT  UND
     1: 00000000     0 NOTYPE  GLOBAL DEFAULT  ABS __ra8p1_core
"""
CORE_HEADER = """#ifndef BSP_MCU_CORE_CFG_H_
#define BSP_MCU_CORE_CFG_H_
#define BSP_CFG_CPU_CORE_FROM_DEVICE (0)
#endif
"""
FAMILY_HEADER = """#ifndef BSP_MCU_FAMILY_CFG_H_
#define BSP_MCU_FAMILY_CFG_H_
#include "bsp_mcu_core_cfg.h"
#if defined(BSP_CFG_CPU_CORE) && BSP_CFG_CPU_CORE != BSP_CFG_CPU_CORE_FROM_DEVICE
#error "RA8P1 core macro conflicts with the selected device target"
#endif
#ifndef BSP_CFG_CPU_CORE
#define BSP_CFG_CPU_CORE BSP_CFG_CPU_CORE_FROM_DEVICE
#endif
#include "../../../ra/fsp/src/bsp/mcu/ra8p1/bsp_mcu_family.h"
#endif
"""


def digest(data):
    return hashlib.sha256(data).hexdigest()


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.baseline = self.root / "baseline"
        self.manifest = self.root / "manifest.json"
        self.elf = self.project / "Debug/test.elf"
        self.settings = {"#FSPVersion#": "5.9.0", "#TargetName#": verify.PART,
                          "Core": "CM85", "#TargetARCHITECTURE#": "cortex-m85", "#DeviceCommand#": "R7KA8P1KF_CPU0",

                         "#SELECTED_TOOLCHAIN#": "gcc-arm-embedded", "#ToolchainVersion#": "13.2.1.arm-13-7"}
        self.write_configuration()
        self.files = {verify.CMSIS + "Include/R7KA8P1AD.h": b"candidate header\n",
                      "ra/fsp/src/bsp/mcu/ra8p1/bsp_feature.h": b"candidate feature\n",
                      "ra/board/ra8p1_ek/board.h": b"candidate board\n",
                      "ra/fsp/src/r_pdm/r_pdm.c": b"candidate pdm\n"}
        for name, data in self.files.items():
            self.write(self.baseline / name, data)
            self.write(self.project / name, data)
        entries = {name: digest(data) for name, data in self.files.items()}
        self.data = {"candidate": "custom-5.9-derived-ra8p1",
                     "baseline_changes": {name: {"after_sha256": value} for name, value in entries.items()},
                     "packs": {verify.TARGET_PACKS[0]: {"entries": {name: value for name, value in entries.items()
                                                                 if "/bsp/" in name}},
                               verify.TARGET_PACKS[1]: {"entries": {"ra/board/ra8p1_ek/board.h": entries["ra/board/ra8p1_ek/board.h"]}},
                               "Renesas.RA.5.9.0.pack": {"entries": entries}}}
        part = ET.Element("raModuleDescription")
        device = ET.Element("raModuleDescription")
        for core, name in ((0, "CM85"), (1, "CM33")):
            target = ET.SubElement(part, "device", id=verify.PART, device=verify.PART, core=name)
            for bsp in ("bsp.ra8p1." + verify.PART, "bsp.ra8p1.fsp", f"bsp.ra8p1.cpu{core}"):
                ET.SubElement(target, "select", bsp=bsp)
            cfg = ET.SubElement(device, "config", id=f"config.bsp.ra8p1.cpu{core}", path="fsp_cfg/bsp/bsp_mcu_core_cfg.h")
            ET.SubElement(cfg, "content").text = f"#define BSP_CFG_CPU_CORE_FROM_DEVICE ({core})"
            ET.SubElement(device, "bsp", id=f"bsp.ra8p1.cpu{core}", config=f"config.bsp.ra8p1.cpu{core}")
        family = ET.Element("raModuleDescription")
        cfg = ET.SubElement(family, "config", id="config.bsp.ra8p1.fsp", path="fsp_cfg/bsp/bsp_mcu_family_cfg.h")
        ET.SubElement(cfg, "content").text = FAMILY_HEADER
        for kind, root in (("part", part), ("device", device), ("fsp", family)):
            data = ET.tostring(root)
            name = verify.CORE_MDFS[kind]
            self.write(self.baseline / "module_descriptions" / name, data)
            self.data["packs"][verify.TARGET_PACKS[0]]["entries"][".module_descriptions/" + name] = digest(data)
        self.save_manifest()
        directory = self.project / "ra_cfg/fsp_cfg/bsp"
        self.bsp = directory / "bsp_cfg.h"
        self.core_header = directory / "bsp_mcu_core_cfg.h"
        self.family_header = directory / "bsp_mcu_family_cfg.h"
        self.write(self.bsp, b'#include "bsp_mcu_family_cfg.h"\n')
        self.write(self.core_header, CORE_HEADER.encode())
        self.write(self.family_header, FAMILY_HEADER.encode())
        for name in (".cproject", "ra_gen/hal_data.c", "ra_gen/pin_data.c", "ra_gen/vector_data.c",
                     "script/fsp.ld", "src/hal_entry.c"):
            self.write(self.project / name, b"fixture\n")
        self.write(self.elf, b"\x7fELF" + b"\0" * 508)
        now = time.time_ns()
        for path in self.root.rglob("*"):
            if path.is_file():
                os.utime(path, ns=(now - 10_000_000_000, now - 10_000_000_000))
        os.utime(self.elf, ns=(now - 1_000_000_000, now - 1_000_000_000))
        self.args = argparse.Namespace(project=self.project, elf=self.elf, core="CM85", baseline=self.baseline,
                                       manifest=self.manifest, readelf=None, other_elf=[], reserve=[], not_before=None)
        self.outputs = {"-hW": HEADER, "-lW": LOADS, "-SW": SECTIONS, "-p": COMMENT, "-sW": SYMBOLS}
        self.tool = {"readelf": "/fixture/bin/arm-none-eabi-readelf", "gcc": "/fixture/bin/arm-none-eabi-gcc",
                     "version": "13.2.1"}

    @staticmethod
    def write(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def write_configuration(self, core_value="config.bsp.ra8p1.cpu0"):
        root = ET.Element("raConfiguration")
        general = ET.SubElement(root, "generalSettings")
        for key, value in self.settings.items():
            ET.SubElement(general, "option", key=key, value=value)
        bsp = ET.SubElement(root, "raBspConfiguration")
        ET.SubElement(bsp, "config", id="config.bsp.ra8p1." + verify.PART)
        if core_value is not None:
            ET.SubElement(bsp, "config", id=core_value)
        components = ET.SubElement(root, "raComponentSelection")
        ET.SubElement(components, "component", {"class": "HAL Drivers", "subgroup": "r_pdm", "version": "5.9.0"})
        for subgroup, variant in (("device", verify.PART), ("device", ""), ("fsp", "")):
            node = ET.SubElement(components, "component", {"class": "BSP", "group": "ra8p1",
                                "subgroup": subgroup, "variant": variant, "version": "5.9.0"})
            ET.SubElement(node, "originalPack").text = verify.TARGET_PACKS[0]
        self.write(self.project / "configuration.xml", ET.tostring(root))

    def save_manifest(self):
        self.write(self.manifest, json.dumps(self.data).encode())

    def omit_serialized_core(self):
        self.write_configuration(None)
        stamp = self.elf.stat().st_mtime_ns - 1
        os.utime(self.project / "configuration.xml", ns=(stamp, stamp))

    def resave_mdf(self, kind, root):
        name = verify.CORE_MDFS[kind]
        data = ET.tostring(root)
        self.write(self.baseline / "module_descriptions" / name, data)
        self.data["packs"][verify.TARGET_PACKS[0]]["entries"][".module_descriptions/" + name] = digest(data)
        self.save_manifest()

    def run_verifier(self):
        with patch.object(verify, "discover_readelf", return_value=self.tool), \
                patch.object(verify, "run", side_effect=lambda args: self.outputs[args[1]]):
            return verify.verify(self.args)

    def assert_failure(self, phrase, stage=None):
        result = self.run_verifier()
        self.assertFalse(result["success"], result)
        self.assertFalse(result["runtime_verified"])
        self.assertIn(phrase, result["errors"][0]["message"])
        if stage:
            self.assertEqual(result["errors"][0]["stage"], stage)
        return result

    def test_valid_artifact_records_hash_ranges_and_no_runtime_claim(self):
        report = self.run_verifier()
        self.assertTrue(report["success"], report)
        self.assertFalse(report["runtime_verified"])
        self.assertEqual(report["artifact"]["sha256"], digest(self.elf.read_bytes()))
        self.assertEqual(len(report["checks"]["elf"]["loads"]), 3)
        self.assertEqual(report["checks"]["sources"]["candidate_files_checked"], 4)
        self.assertGreater(report["checks"]["freshness"]["input_count"], 4)

    def test_wrong_configuration_identity(self):
        for key, value in (("#FSPVersion#", "6.6.0"), ("#TargetName#", "R7FA8M1AHECBD"),
                           ("Core", "CM33"), ("#TargetARCHITECTURE#", "cortex-m33"),
                           ("#SELECTED_TOOLCHAIN#", "llvm")):
            with self.subTest(key=key):
                original = self.settings[key]
                self.settings[key] = value
                self.write_configuration()
                self.assert_failure("mismatch" if key == "#TargetARCHITECTURE#" else key, "configuration")
                self.settings[key] = original

    def test_wrong_bsp_core_selection(self):
        for selection in ("config.bsp.ra8p1.cpu1", "config.bsp.ra8p1.cpu.0"):
            with self.subTest(selection=selection):
                self.write_configuration(selection)
                self.assert_failure("BSP core selection", "configuration")

    def test_omitted_content_only_core_has_full_evidence(self):
        self.omit_serialized_core()
        report = self.run_verifier()
        self.assertTrue(report["success"], report)
        self.assertFalse(report["checks"]["configuration"]["bsp_config_serialized"])
        self.assertEqual(len(report["checks"]["sources"]["core_mdfs"]), 3)
        self.assertEqual(report["checks"]["elf"]["core_symbol"]["value"], 0)

    def test_omitted_core_requires_matching_debugger_device(self):
        for value in (None, "R7KA8P1KF_CPU1", "R7KA8P1KF"):
            with self.subTest(value=value):
                if value is None:
                    self.settings.pop("#DeviceCommand#")
                else:
                    self.settings["#DeviceCommand#"] = value
                self.omit_serialized_core()
                self.assert_failure("DeviceCommand", "configuration")

    def test_omitted_core_does_not_bypass_header_symbol_or_partition_checks(self):
        self.omit_serialized_core()
        self.core_header.write_text(CORE_HEADER.replace("(0)", "(1)"))
        self.assert_failure("expected 0", "generated_core")
        self.core_header.write_text(CORE_HEADER)
        self.outputs["-sW"] = SYMBOLS.replace("1: 00000000", "1: 00000001")
        self.assert_failure("expected 0", "elf")
        self.outputs["-sW"] = SYMBOLS
        self.outputs["-lW"] = LOADS.replace("0x02000000", "0x02080000")
        self.assert_failure("outside CPU0 partition", "elf")

    def test_omitted_core_requires_all_mdf_hashes_and_current_bytes(self):
        self.omit_serialized_core()
        entries = self.data["packs"][verify.TARGET_PACKS[0]]["entries"]
        for name in verify.CORE_MDFS.values():
            with self.subTest(name=name):
                key = ".module_descriptions/" + name
                saved = entries.pop(key)
                self.save_manifest()
                self.assert_failure("Missing candidate MDF hash", "sources")
                entries[key] = saved
                self.save_manifest()
                path = self.baseline / "module_descriptions" / name
                data = path.read_bytes()
                path.write_bytes(data + b"\n")
                self.assert_failure("Stale candidate MDF hash", "sources")
                path.write_bytes(data)

    def test_omitted_core_mdf_must_be_property_free(self):
        self.omit_serialized_core()
        root = ET.parse(self.baseline / "module_descriptions" / verify.CORE_MDFS["device"]).getroot()
        cfg = root.find("config[@id='config.bsp.ra8p1.cpu0']")
        ET.SubElement(cfg, "property", id="required.setting")
        self.resave_mdf("device", root)
        self.assert_failure("not content-only", "sources")

    def test_candidate_mdf_cannot_select_the_other_core(self):
        self.omit_serialized_core()
        root = ET.parse(self.baseline / "module_descriptions" / verify.CORE_MDFS["part"]).getroot()
        root.find("device[@core='CM85']/select[@bsp='bsp.ra8p1.cpu0']").set("bsp", "bsp.ra8p1.cpu1")
        self.resave_mdf("part", root)
        self.assert_failure("device/core selection mismatch", "sources")

    def test_candidate_mdf_literal_matches_generated_core(self):
        self.omit_serialized_core()
        root = ET.parse(self.baseline / "module_descriptions" / verify.CORE_MDFS["device"]).getroot()
        root.find("config[@id='config.bsp.ra8p1.cpu0']/content").text = "#define BSP_CFG_CPU_CORE_FROM_DEVICE (1)"
        self.resave_mdf("device", root)
        self.assert_failure("MDF core literal mismatch", "sources")

    def test_candidate_mdf_component_selection_is_required(self):
        self.omit_serialized_core()
        path = self.project / "configuration.xml"
        root = ET.parse(path).getroot()
        root.find("raComponentSelection/component[@class='BSP']/originalPack").text = "Renesas.RA_mcu_ra8m1.5.9.0.pack"
        path.write_bytes(ET.tostring(root))
        self.assert_failure("candidate BSP component", "configuration")

    def test_obsolete_core_property_is_rejected_even_with_new_selector(self):
        path = self.project / "configuration.xml"
        root = ET.parse(path).getroot()
        ET.SubElement(root.find("raBspConfiguration"), "property",
                      id="config.bsp.ra8p1.cpu", value="config.bsp.ra8p1.cpu.0")
        path.write_bytes(ET.tostring(root))
        self.assert_failure("Obsolete", "configuration")

    def test_duplicate_or_conflicting_core_configurations_are_rejected(self):
        for extra in ("config.bsp.ra8p1.cpu0", "config.bsp.ra8p1.cpu1"):
            with self.subTest(extra=extra):
                self.write_configuration()
                path = self.project / "configuration.xml"
                root = ET.parse(path).getroot()
                ET.SubElement(root.find("raBspConfiguration"), "config", id=extra)
                path.write_bytes(ET.tostring(root))
                self.assert_failure("duplicate or mismatched", "configuration")

    def test_missing_or_duplicate_physical_configuration_is_rejected(self):
        for count in (0, 2):
            with self.subTest(count=count):
                self.write_configuration()
                path = self.project / "configuration.xml"
                root = ET.parse(path).getroot()
                bsp = root.find("raBspConfiguration")
                part = bsp.find("config[@id='config.bsp.ra8p1." + verify.PART + "']")
                bsp.remove(part)
                for _ in range(count):
                    ET.SubElement(bsp, "config", id="config.bsp.ra8p1." + verify.PART)
                path.write_bytes(ET.tostring(root))
                self.assert_failure("physical RA8P1 part", "configuration")

    def test_duplicate_general_core_setting_is_rejected(self):
        path = self.project / "configuration.xml"
        root = ET.parse(path).getroot()
        ET.SubElement(root.find("generalSettings"), "option", key="Core", value="CM85")
        path.write_bytes(ET.tostring(root))
        self.assert_failure("Duplicate configuration setting", "configuration")

    def test_missing_toolchain_version(self):
        del self.settings["#ToolchainVersion#"]
        self.write_configuration()
        self.assert_failure("toolchain version evidence")

    def test_toolchain_option_without_value_fails_cleanly(self):
        path = self.project / "configuration.xml"
        root = ET.parse(path).getroot()
        del root.find("./generalSettings/option[@key='#ToolchainVersion#']").attrib["value"]
        path.write_bytes(ET.tostring(root))
        self.assert_failure("toolchain version evidence", "configuration")

    def test_cpu1_requires_both_correct_configuration_and_macro(self):
        self.settings.update(Core="CM33", **{"#TargetARCHITECTURE#": "cortex-m33", "#DeviceCommand#": "R7KA8P1KF_CPU1"})
        self.write_configuration("config.bsp.ra8p1.cpu1")
        self.assertEqual(verify.configuration(self.project, "CM33")["bsp_core"], 1)
        with self.assertRaisesRegex(verify.VerificationError, "expected 1"):
            verify.generated_core(self.project, 1)
        self.core_header.write_text(CORE_HEADER.replace("(0)", "((1))"))
        self.assertEqual(verify.generated_core(self.project, 1)["value"], 1)
        self.write_configuration(None)
        self.assertFalse(verify.configuration(self.project, "CM33")["bsp_config_serialized"])

    def test_missing_mismatched_or_nonliteral_generated_core(self):
        for value in ("#define WRONG 0\n", "#define BSP_CFG_CPU_CORE_FROM_DEVICE 1\n",
                      "#define BSP_CFG_CPU_CORE_FROM_DEVICE SOME_MACRO\n", "#define BSP_CFG_CPU_CORE (0)\n",
                      "#define BSP_CFG_CPU_CORE_FROM_DEVICE(0)\n"):
            with self.subTest(value=value):
                self.core_header.write_text(value)
                self.assert_failure("BSP_CFG_CPU_CORE", "generated_core")

    def test_missing_core_or_family_header_is_rejected(self):
        for path in (self.core_header, self.family_header):
            with self.subTest(path=path):
                text = path.read_text()
                path.unlink()
                self.assert_failure("Missing regular evidence file", "generated_core")
                path.write_text(text)

    def test_duplicate_generated_macros_are_rejected(self):
        for line in ("#define BSP_CFG_CPU_CORE_FROM_DEVICE 0\n",
                     "#define BSP_CFG_CPU_CORE BSP_CFG_CPU_CORE_FROM_DEVICE\n"):
            with self.subTest(line=line):
                self.bsp.write_text(line)
                self.assert_failure("exactly one generated", "generated_core")

    def test_generated_alias_and_conflict_guard_are_required(self):
        replacements = (
            ("#define BSP_CFG_CPU_CORE BSP_CFG_CPU_CORE_FROM_DEVICE", "#define BSP_CFG_CPU_CORE 0", "must alias"),
            ("#ifndef BSP_CFG_CPU_CORE\n", "", "guarded alias"),
            ("#if defined(BSP_CFG_CPU_CORE) && BSP_CFG_CPU_CORE != BSP_CFG_CPU_CORE_FROM_DEVICE", "#if 0", "conflict guard"),
            ('#error "RA8P1 core macro conflicts with the selected device target"', "", "conflict guard"),
            ('#include "bsp_mcu_core_cfg.h"', "", "core include"),
        )
        for old, new, message in replacements:
            with self.subTest(message=message):
                self.family_header.write_text(FAMILY_HEADER.replace(old, new))
                self.assert_failure(message, "generated_core")

    def test_generated_macro_undefinition_is_rejected(self):
        self.bsp.write_text("#undef BSP_CFG_CPU_CORE\n")
        self.assert_failure("undefinition", "generated_core")

    def test_stale_extraction(self):
        (self.project / "ra/fsp/src/r_pdm/r_pdm.c").write_text("old installed pack\n")
        self.assert_failure("Stale or modified extraction", "sources")

    def test_stale_manifest_baseline(self):
        (self.baseline / "ra/fsp/src/r_pdm/r_pdm.c").write_text("new candidate\n")
        self.assert_failure("Baseline changed since manifest", "sources")

    def test_missing_required_source(self):
        (self.project / (verify.CMSIS + "Include/R7KA8P1AD.h")).unlink()
        self.assert_failure("Missing required generated sources", "sources")

    def test_missing_selected_driver(self):
        (self.project / "ra/fsp/src/r_pdm/r_pdm.c").unlink()
        self.assert_failure("Missing required generated sources", "sources")

    def test_conflicting_pack_hashes(self):
        path = verify.CMSIS + "Include/R7KA8P1AD.h"
        self.data["packs"][verify.TARGET_PACKS[0]]["entries"][path] = "0" * 64
        self.save_manifest()
        self.assert_failure("Conflicting pack hashes", "sources")

    def test_missing_target_pack_and_manifest(self):
        del self.data["packs"][verify.TARGET_PACKS[1]]
        self.save_manifest()
        self.assert_failure("Missing target pack evidence", "sources")
        self.manifest.unlink()
        self.assert_failure("Missing regular evidence file", "sources")

    def test_missing_change_inventory(self):
        self.data["baseline_changes"] = {}
        self.save_manifest()
        self.assert_failure("baseline-change evidence", "sources")

    def test_manifest_path_traversal_rejected(self):
        self.data["packs"][verify.TARGET_PACKS[0]]["entries"]["ra/../../outside"] = "0" * 64
        self.save_manifest()
        self.assert_failure("Unsafe manifest path", "sources")

    def test_ra_tilde_one_alias_normalization(self):
        self.assertEqual(verify.normalized_source("ra~1/Include/R7KA8P1AD.h"),
                         verify.CMSIS + "Include/R7KA8P1AD.h")

    def test_newer_generated_input_is_stale(self):
        self.bsp.touch()
        self.assert_failure("Stale ELF", "freshness")

    def test_newer_manifest_is_stale(self):
        self.manifest.touch()
        self.assert_failure("Stale ELF", "freshness")

    def test_missing_generated_freshness_evidence(self):
        (self.project / "ra_gen/pin_data.c").unlink()
        self.assert_failure("Missing regular evidence file", "freshness")

    def test_missing_or_newer_explicit_marker(self):
        self.args.not_before = self.root / "build-start"
        self.assert_failure("Missing regular evidence file", "freshness")
        self.args.not_before.write_text("start\n")
        self.assert_failure("Stale ELF", "freshness")

    def test_future_elf_timestamp_fails(self):
        future = time.time_ns() + 60_000_000_000
        os.utime(self.elf, ns=(future, future))
        self.assert_failure("future ELF timestamp", "freshness")

    def test_external_runtime_or_physical_range_fails(self):
        for old, new in (("0x02000000 0x02000000", "0x00000000 0x02000000"),
                         ("0x02000000 0x02000000", "0x02000000 0x0300a000"),
                         ("0x22000008 0x22000008", "0x20000000 0x20000000")):
            with self.subTest(new=new):
                self.outputs["-lW"] = LOADS.replace(old, new)
                report = self.assert_failure("outside internal memory", "elf")
                self.assertEqual(len(report["checks"]["elf"]["loads"]), 3)
                self.assertIn("virtual_end", report["checks"]["elf"]["loads"][0])

    def test_filesize_and_elf_file_bounds(self):
        for old, new, error in (("0x00010 0x00010", "0x00011 0x00010", "FileSiz exceeds"),
                                ("0x000040", "0x100000", "beyond ELF file")):
            with self.subTest(error=error):
                self.outputs["-lW"] = LOADS.replace(old, new)
                self.assert_failure(error, "elf")

    def test_no_or_malformed_loads(self):
        for text in ("No program headers", "LOAD broken", "LOAD\tbroken"):
            with self.subTest(text=text):
                self.outputs["-lW"] = text
                self.assert_failure("PT_LOAD", "elf")

    def test_option_section_inside_allowed_mram_still_fails(self):
        self.outputs["-SW"] = SECTIONS.replace(".text", ".option_setting_ofs")
        self.assert_failure("Forbidden option/security section", "elf")

    def test_section_and_compiler_evidence_required(self):
        self.outputs["-SW"] = "No sections"
        self.assert_failure("Missing ELF section evidence", "elf")
        self.outputs["-SW"] = SECTIONS
        self.outputs["-p"] = COMMENT.replace("13.2.1", "15.2.1")
        self.assert_failure("ELF compiler evidence", "elf")

    def test_arm_executable_required(self):
        self.outputs["-hW"] = HEADER.replace("ARM", "X86-64")
        self.assert_failure("ELF32 ARM executable", "elf")

    def test_elf_core_marker_missing_duplicate_or_invalid(self):
        row = "     1: 00000000     0 NOTYPE  GLOBAL DEFAULT  ABS __ra8p1_core"
        cases = (
            ("No symbols\n", "exactly one"),
            (SYMBOLS + row + "\n", "exactly one"),
            (SYMBOLS.replace(row, "broken __ra8p1_core"), "Malformed"),
            (SYMBOLS.replace("ABS __ra8p1_core", "1 __ra8p1_core"), "global absolute"),
            (SYMBOLS.replace("GLOBAL", "LOCAL"), "global absolute"),
            (SYMBOLS.replace("GLOBAL", "WEAK"), "global absolute"),
            (SYMBOLS.replace("1: 00000000", "1: 00000002"), "must be 0 or 1"),
            (SYMBOLS.replace("1: 00000000", "1: 00000001"), "expected 0"),
        )
        for text, message in cases:
            with self.subTest(message=message):
                self.outputs["-sW"] = text
                self.assert_failure(message, "elf")

    def test_core0_rejects_core1_and_reserved_regions(self):
        for old, addresses in (("0x02000000 0x02000000", ("0x02080000", "0x020ff000")),
                               ("0x22000008 0x22000008", ("0x220c0000", "0x22180000"))):
            for address in addresses:
                with self.subTest(address=address):
                    self.outputs["-lW"] = LOADS.replace(old, address + " " + address)
                    self.assert_failure("outside CPU0 partition", "elf")

    def test_per_core_partition_boundaries(self):
        for core, regions in verify.PARTITIONS.items():
            for start, end in regions:
                with self.subTest(core=core, start=start):
                    self.assertTrue(verify.allowed(start, end - start, regions))
                    self.assertFalse(verify.allowed(start, end - start + 1, regions))
                    self.assertFalse(verify.allowed(start - 1, 1, regions))

    def test_complete_cpu1_artifact_and_core0_partition_rejection(self):
        self.args.core = "CM33"
        self.settings.update(Core="CM33", **{"#TargetARCHITECTURE#": "cortex-m33", "#DeviceCommand#": "R7KA8P1KF_CPU1"})
        self.write_configuration(None)
        self.core_header.write_text(CORE_HEADER.replace("(0)", "(1)"))
        self.outputs["-sW"] = SYMBOLS.replace("1: 00000000", "1: 00000001")
        for flag in ("-lW", "-SW"):
            self.outputs[flag] = self.outputs[flag].replace("020000", "020800").replace("220000", "220c00")
        stamp = self.elf.stat().st_mtime_ns - 1
        for path in (self.project / "configuration.xml", self.core_header):
            os.utime(path, ns=(stamp, stamp))
        report = self.run_verifier()
        self.assertTrue(report["success"], report)
        self.assertEqual(report["checks"]["elf"]["core_symbol"]["value"], 1)
        self.outputs["-lW"] = LOADS
        self.assert_failure("outside CPU1 partition", "elf")

    def test_reserved_overlap_and_adjacent_boundary(self):
        self.args.reserve = [(0x22000020, 0x22000040)]
        self.assert_failure("overlaps reserved", "overlap")
        self.args.reserve = [(0x22000028, 0x22000040)]
        self.assertTrue(self.run_verifier()["success"])

    def test_other_core_overlap(self):
        other = self.root / "other.elf"
        self.write(other, self.elf.read_bytes())
        self.args.other_elf = [other]
        self.assert_failure("overlaps reserved", "overlap")

    def test_other_core_disjoint_partition_is_accepted(self):
        other = self.root / "core1.elf"
        self.write(other, self.elf.read_bytes())
        self.args.other_elf = [other]
        other_outputs = dict(self.outputs)
        for flag in ("-lW", "-SW"):
            other_outputs[flag] = other_outputs[flag].replace("020000", "020800").replace("220000", "220c00")
        other_outputs["-sW"] = SYMBOLS.replace("1: 00000000", "1: 00000001")
        with patch.object(verify, "discover_readelf", return_value=self.tool), \
                patch.object(verify, "run", side_effect=lambda args: (
                    other_outputs if Path(args[-1]) == other else self.outputs)[args[1]]):
            report = verify.verify(self.args)
        self.assertTrue(report["success"], report)
        self.assertEqual(report["checks"]["overlap"]["other_images"][0]["core"], 1)
        self.assertEqual(report["checks"]["overlap"]["reserved_ranges"][:2], list(verify.RESERVED))

    def test_missing_other_elf_is_not_silently_skipped(self):
        self.args.other_elf = [self.root / "missing.elf"]
        self.assert_failure("Missing regular evidence file", "overlap")

    def test_load_overlap_and_allowed_end_boundary(self):
        loads = verify.parse_loads(LOADS)
        loads[1]["physical"] = 0x02000008
        with self.assertRaisesRegex(verify.VerificationError, "Overlapping PT_LOAD"):
            verify.validate_loads(loads, 512)
        self.assertTrue(verify.allowed(0x020ffff0, 16))
        self.assertFalse(verify.allowed(0x020ffff0, 17))
        self.assertTrue(verify.allowed(0x2219fff0, 16))
        self.assertFalse(verify.allowed(0x2219fff0, 17))

    def test_zero_file_size_bss_offset_does_not_imply_file_bytes(self):
        loads = verify.parse_loads(LOADS)
        loads[2]["offset"] = 0x100000
        verify.validate_loads(loads, 512)

    def test_reservation_parser(self):
        self.assertEqual(verify.parse_reserved("0x22000000:0x22000010"), (0x22000000, 0x22000010))
        for value in ("bad", "1:1", "2:1", "-1:2", "0:0x100000001"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                verify.parse_reserved(value)

    def test_explicit_toolchain_must_match(self):
        readelf = self.root / "bin/arm-none-eabi-readelf"
        self.write(readelf, b"tool\n")
        self.write(readelf.with_name("arm-none-eabi-gcc"), b"compiler\n")
        with patch.object(verify, "run", return_value="13.2.1\n"):
            self.assertEqual(verify.discover_readelf("13.2.1", readelf)["version"], "13.2.1")
        with patch.object(verify, "run", return_value="15.2.1\n"), \
                self.assertRaisesRegex(verify.VerificationError, "No arm-none-eabi-readelf"):
            verify.discover_readelf("13.2.1", readelf)

    def test_json_failure_and_output_does_not_overwrite(self):
        output = self.root / "report.json"
        output.write_text("keep me")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = verify.main(["--project", str(self.project), "--elf", str(self.root / "missing.elf"),
                                "--core", "CM85", "--output", str(output)])
        report = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(report["runtime_verified"])
        self.assertEqual(output.read_text(), "keep me")
        self.assertEqual(report["errors"][-1]["stage"], "report")

    def test_malformed_manifest_objects_fail_with_json_evidence(self):
        self.manifest.write_text("[]")
        self.assert_failure("Invalid manifest object", "sources")
        self.data["packs"][verify.TARGET_PACKS[0]] = []
        self.save_manifest()
        self.assert_failure("Invalid pack record", "sources")

    def test_compact_and_detailed_source_reports(self):
        compact = self.run_verifier()
        self.assertNotIn("checked_files", compact["checks"]["sources"])
        self.assertEqual(compact["checks"]["sources"]["checked_file_count"], 4)
        self.args.details = True
        detailed = self.run_verifier()
        self.assertEqual(len(detailed["checks"]["sources"]["checked_files"]), 4)
        self.assertEqual(compact["checks"]["sources"]["checked_set_sha256"],
                         detailed["checks"]["sources"]["checked_set_sha256"])

    def test_secure_settings_section_is_rejected(self):
        self.outputs["-SW"] = SECTIONS.replace(".text", ".secure_settings")
        self.assert_failure("Forbidden option/security section", "elf")

    def test_tool_diagnostics_are_not_silently_accepted(self):
        for returncode, stderr in ((1, "invalid ELF"), (0, "warning: truncated")):
            with self.subTest(returncode=returncode), \
                    patch.object(verify.subprocess, "run", return_value=argparse.Namespace(
                        returncode=returncode, stderr=stderr, stdout="partial output")), \
                    self.assertRaises(verify.VerificationError):
                verify.run(["readelf", "-lW", "file.elf"])

    def test_xml_entity_rejected(self):
        (self.project / "configuration.xml").write_text('<!DOCTYPE x [<!ENTITY a "bad">]><raConfiguration/>')
        self.assert_failure("DTD/entities forbidden", "configuration")


if __name__ == "__main__":
    unittest.main()
