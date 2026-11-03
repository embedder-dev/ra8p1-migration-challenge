"""Host tests for preserved/candidate evidence comparison."""
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from compare_older_device_regression import (adc_gain_equivalence, canonical, digest,
    extract_spec, file_comparisons, inspect_log, map_regions, pack_sources,
    read_xml, source_name, verify_sources)


class RegressionComparisonTests(unittest.TestCase):
    def test_specs_exactly_match_preserved_modules(self):
        root = Path(__file__).resolve().parents[1]
        for board in ("ra8m1", "ra6t2", "ra6m5"):
            with self.subTest(board=board):
                actual = json.loads((root / "tests/ra8p1" / ("regression-" + board + ".json")).read_text())
                self.assertEqual(actual, extract_spec(root / "validation/projects" / ("original_" + board)))

    def test_clean_only_empty_and_failed_logs_are_not_build_passes(self):
        good = "Building target: candidate.elf\nBuild Finished. 0 errors, 0 warnings"
        self.assertTrue(inspect_log(good, "candidate")["verified"])
        for text in ("", "Build Finished. 0 errors, 0 warnings", good.replace("candidate", "other"),
                     good + "\nerror: failed", good + "\nTraceback", good + "\nBuild Finished. 1 errors, 0 warnings"):
            self.assertFalse(inspect_log(text, "candidate")["verified"])

    def test_unused_llvm_manifest_diagnostic_not_gnu_failure(self):
        log = "Managed Build system manifest file error: Unable to resolve the category identifier com.renesas.cdt.managedbuild.llvm.core.optionCategory.compiler.includes\n"
        log += "Building target: example.elf\nBuild Finished. 0 errors, 0 warnings"
        self.assertTrue(inspect_log(log, "example")["verified"])

    def test_xml_reorders_attributes_and_children_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.xml"
            path.write_text('<a z="1" b="2"><c value="3"/><d/></a>')
            a = canonical(read_xml(path))
            path.write_text('<a b="2" z="1"><d/><c value="3"/></a>')
            self.assertEqual(a, canonical(read_xml(path)))
            path.write_text('<a b="2" z="1"><d/><c value="4"/></a>')
            self.assertNotEqual(a, canonical(read_xml(path)))
            path.write_text('<!DOCTYPE a><a/>')
            with self.assertRaises(ValueError):
                read_xml(path)

    def test_file_comparison_preserves_semantics_and_missing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            left, right = Path(directory) / "original", Path(directory) / "candidate"
            left.mkdir()
            right.mkdir()
            (left / "a.h").write_text("#define VALUE 1\n")
            (right / "a.h").write_text("#define VALUE 2\n")
            results = file_comparisons(left, right, ["a.h", "missing.h"])
            self.assertFalse(results["a.h"]["equal"])
            self.assertIn("-#define VALUE 1", results["a.h"]["diff"])
            self.assertEqual(len(results["missing.h"]["missing"]), 2)

    def test_map_regions_rejects_missing_section(self):
        text = "Memory Configuration\nName Origin Length\nRAM 0x20000000 0x1000\n\nLinker script and memory map\n"
        self.assertEqual(map_regions(text)[1], ["RAM", "0x20000000", "0x1000"])
        with self.assertRaises(ValueError):
            map_regions("nothing")

    def test_source_matching_rejects_changed_or_unknown_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ra").mkdir()
            (root / "ra/a.h").write_bytes(b"expected")
            cfg = {"components": [{"pack": "a.pack"}]}
            packs = {"a.pack": {"sha256": "packhash", "sources": {"ra/a.h": digest(b"expected")}}}
            self.assertTrue(verify_sources(root, cfg, packs)["verified"])
            (root / "ra/a.h").write_bytes(b"changed")
            self.assertFalse(verify_sources(root, cfg, packs)["verified"])
            (root / "ra/a.h").write_bytes(b"expected")
            (root / "ra/unknown.h").write_bytes(b"unknown")
            self.assertFalse(verify_sources(root, cfg, packs)["verified"])

    def test_manifest_and_pack_entry_hash_validation(self):
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as archive:
            archive.writestr("ra/a.h", b"expected")
        outer = io.BytesIO()
        with zipfile.ZipFile(outer, "w") as archive:
            archive.writestr("packs/a.pack", inner.getvalue())
        with tempfile.TemporaryDirectory() as directory, zipfile.ZipFile(outer) as archive:
            root = Path(directory)
            (root / "a.pack").write_bytes(inner.getvalue())
            manifest = {"packs": {"a.pack": {"sha256": digest(inner.getvalue()),
                                             "entries": {"ra/a.h": digest(b"expected")}}}}
            self.assertEqual(pack_sources(archive, "a.pack")["sources"]["ra/a.h"], digest(b"expected"))
            self.assertEqual(pack_sources(archive, "a.pack", root, manifest)["sources"]["ra/a.h"], digest(b"expected"))
            manifest["packs"]["a.pack"]["entries"]["ra/a.h"] = "wrong"
            with self.assertRaises(ValueError):
                pack_sources(archive, "a.pack", root, manifest)
            manifest["packs"]["a.pack"]["sha256"] = "wrong"
            with self.assertRaises(ValueError):
                pack_sources(archive, "a.pack", root, manifest)

    def test_adc_equivalence_rejects_unrelated_template_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            left, right = Path(directory) / "original", Path(directory) / "candidate"
            for path in (left / "ra_gen", right / "ra_gen"):
                path.mkdir(parents=True)
            old = "\n".join(f"R_ADC_B0_ADUGTR{i}_UGAINI_Pos R_ADC_B0_ADUGTR{i}_UGAINF_Msk" for i in range(8))
            new = "\n".join("R_ADC_B0_ADUGTR_UGAIN_INTEGER_Pos R_ADC_B0_ADUGTR_UGAIN_FRACTIONAL_Msk" for i in range(8))
            (left / "ra_gen/hal_data.c").write_text(old)
            (right / "ra_gen/hal_data.c").write_text(new + "\nunrelated_change")
            self.assertFalse(adc_gain_equivalence(left, right)["verified"])
            (left / "ra_gen/hal_data.c").write_text("unexpected")
            self.assertFalse(adc_gain_equivalence(left, right)["verified"])

    def test_cmsis_alias_mapping(self):
        self.assertEqual(source_name("ra~1/Source/system.c"),
                         "ra/fsp/src/bsp/cmsis/Device/RENESAS/Source/system.c")


if __name__ == "__main__":
    unittest.main()
