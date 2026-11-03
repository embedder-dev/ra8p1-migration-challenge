"""Host tests for candidate packaging and scoped installation, not firmware tests."""
import ast
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

import build_ra8p1_packs as build
import install_ra8p1_packs as install


class PackagingTests(unittest.TestCase):
    def test_refresh_includes_board_profiles_but_excludes_other_versions(self):
        source = Path(__file__).with_name("refresh_fsp_candidate.py").read_text()
        tree = ast.parse(source)
        guards = [node for node in ast.walk(tree) if isinstance(node, ast.If)
                  and "destination.name.endswith" in ast.unparse(node.test)]
        self.assertEqual(len(guards), 1)
        guard = compile(ast.Expression(guards[0].test), "refresh-filter", "eval")
        for name, skipped in (
                ("/support/modules/Renesas##BSP##Board##ra8p1_ek####5.9.0.xml", False),
                ("/support/modules/Renesas##BSP##Board##ra8p1_ek####5.9.0##configuration.xml", False),
                ("/support/5.9/devices/target.rzone", False),
                ("/support/modules/Renesas##BSP##Board##ra8p1_ek####6.6.0##configuration.xml", True),
                ("/support/6.6/devices/target.rzone", True)):
            with self.subTest(name=name):
                self.assertEqual(eval(guard, {}, {"destination": Path(name)}), skipped)

    def test_archive_paths(self):
        for name in ("../outside", "/absolute", "x/../../outside", "x\\outside", "C:outside"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                build.safe_name(name)
        self.assertEqual(build.safe_name(".mcu/device.xml"), ".mcu/device.xml")

    def test_xml_entities_rejected(self):
        with self.assertRaises(ValueError):
            build.xml(b'<!DOCTYPE x [<!ENTITY a "x">]><x/>')

    def test_pack_is_deterministic(self):
        files = {"b": b"two", "a": b"one"}
        self.assertEqual(build.pack_bytes(files), build.pack_bytes(dict(reversed(list(files.items())))))
        self.assertEqual(build.read_zip(build.pack_bytes(files)), files)

    def test_symlink_archive_rejected(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as z:
            info = zipfile.ZipInfo("link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, "../outside")
        with self.assertRaises(ValueError):
            build.read_zip(output.getvalue())

    def test_cmsis_alias_and_source_overlay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / build.CMSIS_PREFIX / "Source/startup.c"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"candidate")
            files = {"ra~1/Source/startup.c": b"original", "unrelated.txt": b"unchanged"}
            result = build.overlay_sources(files, root)
            self.assertEqual(result["ra~1/Source/startup.c"], b"candidate")
            self.assertEqual(result["unrelated.txt"], b"unchanged")
            self.assertEqual(files["ra~1/Source/startup.c"], b"original")
            self.assertEqual(build.overlay_sources(files, root, set()), files)
            self.assertEqual(build.overlay_sources(files, root, {build.CMSIS_PREFIX + "Source/startup.c"}), result)

    def test_shared_bsp_updates_each_owning_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline = Path(tmp)
            source = baseline / build.CMSIS_PREFIX / "Source/system.c"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"guarded candidate")
            prefix = "internal/projectgen/ra/packs/"
            old = build.pack_bytes({build.CMSIS_PREFIX + "Source/system.c": b"original"})
            unchanged = build.pack_bytes({"unrelated": b"same"})
            original = {prefix + "Renesas.RA_mcu_ra8m1.5.9.0.pack": old,
                        prefix + "Renesas.RA_mcu_ra6m5.5.9.0.pack": old,
                        prefix + "unaffected.pack": unchanged}
            result = build.changed_existing_packs(original, baseline)
            self.assertEqual(set(result), {"Renesas.RA_mcu_ra8m1.5.9.0.pack",
                                           "Renesas.RA_mcu_ra6m5.5.9.0.pack"})
            for packed in result.values():
                self.assertEqual(build.read_zip(packed)[build.CMSIS_PREFIX + "Source/system.c"],
                                 b"guarded candidate")

    def test_change_inventory_distinguishes_original_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline"
            (baseline / "module_descriptions").mkdir(parents=True)
            (baseline / "driver.c").write_bytes(b"changed")
            (baseline / "added.h").write_bytes(b"new")
            (baseline / "module_descriptions/original.xml").write_bytes(b"same")
            (baseline / "module_descriptions/changed.xml").write_bytes(b"updated")
            metadata = {"original.xml": build.digest(b"same"),
                        "changed.xml": build.digest(b"prior"), "removed.xml": build.digest(b"gone")}

            def fake_git(repo, *args):
                if args[0] == "rev-parse":
                    return build.RELEASES[build.VERSION][0].encode()
                if args[0] == "diff":
                    return b"driver.c\0"
                if args[0] == "show":
                    return b"original"
                return b"added.h\0module_descriptions/original.xml\0module_descriptions/changed.xml\0"

            with patch.object(build, "git", side_effect=fake_git), \
                    patch.object(build, "archive_mdfs", return_value=(metadata, 1, 3)):
                changes = build.baseline_changes(root)
            self.assertEqual(set(changes), {"driver.c", "added.h", "module_descriptions/changed.xml",
                                            "module_descriptions/removed.xml"})
            self.assertEqual(changes["driver.c"]["before_sha256"], build.digest(b"original"))
            self.assertIsNone(changes["added.h"]["before_sha256"])
            self.assertIsNone(changes["module_descriptions/removed.xml"]["after_sha256"])

    def test_absent_target_resources_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "Missing mcu"):
                build.authored_pack(Path(tmp) / "missing", Path(tmp), "mcu")

    def test_missing_pdsc_source_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "candidate.pdsc").write_text(
                '<package><components><component><files><file name="missing.c"/>'
                '</files></component></components></package>')
            (directory / "toolingSupport.xml").write_text('<toolingSupport/>')
            with self.assertRaisesRegex(ValueError, "Missing mcu component"):
                build.authored_pack(directory, directory / "baseline", "mcu")

    def test_new_driver_manifest_and_mdf_added_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline = Path(tmp)
            for driver, api in build.NEW_DRIVERS.items():
                for name in [f"ra/fsp/src/{driver}/{driver}.c", f"ra/fsp/inc/instances/{driver}.h",
                             f"ra/fsp/inc/api/{api}"]:
                    p = baseline / name
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text("fixture")
                p = baseline / f"module_descriptions/Renesas##HAL Drivers##all##{driver}####5.9.0.xml"
                p.parent.mkdir(exist_ok=True)
                p.write_text("<raModuleDescription/>")
            cgc = baseline / "ra/fsp/src/r_cgc/r_cgc_ra8p1.inc"
            cgc.parent.mkdir(parents=True, exist_ok=True)
            cgc.write_text("fixture")
            files = {"Renesas.RA.pdsc": b'<package><description>Original</description><components>'
                     b'<component Csub="r_cgc"><files/></component></components></package>',
                     "toolingSupport.xml": b"<toolingSupport/>"}
            build.add_drivers(files, baseline)
            self.assertEqual(len(ET.fromstring(files["Renesas.RA.pdsc"]).find("components")), 4)
            self.assertIn("ra/fsp/src/r_cgc/r_cgc_ra8p1.inc", files)
            self.assertEqual(len(ET.fromstring(files["toolingSupport.xml"])), 3)
            for driver in build.NEW_DRIVERS:
                self.assertIn(f"ra/fsp/src/{driver}/{driver}.c", files)


class InstallationTests(unittest.TestCase):
    def test_dry_run_and_idempotent_install_preserve_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "host-packs"
            destination.mkdir()
            (destination / "Renesas.RA.6.6.0.pack").write_bytes(b"do not touch")
            old = destination / "Renesas.RA.5.9.0.pack"
            old.write_bytes(b"prior host bytes")
            archive = root / "reference/vendor/FSP_Packs_v5.9.0.zip"
            archive.parent.mkdir(parents=True)
            payload = build.pack_bytes({"internal/projectgen/ra/packs/Renesas.RA.5.9.0.pack": b"original 5.9",
                                        "internal/projectgen/ra/packs/SEGGER.JLink.8.12.6.pack": b"not installed"})
            archive.write_bytes(payload)
            with patch.object(install, "ARCHIVE_SHA256", build.digest(payload)):
                result = install.install(root, destination, None, False)
                self.assertEqual(len(result["changes"]), 1)
                self.assertEqual(old.read_bytes(), b"prior host bytes")
                self.assertFalse((root / "validation").exists())
                result = install.install(root, destination, None, True)
                journal = json.loads(Path(result["rollback_journal"]).read_text())
                record = journal["originals"][old.name]
                self.assertEqual(record["sha256"], build.digest(b"prior host bytes"))
                self.assertEqual((Path(result["rollback_journal"]).parent / record["backup"]).read_bytes(),
                                 b"prior host bytes")
                self.assertEqual(install.install(root, destination, None, True)["changes"], [])
                self.assertEqual((destination / "Renesas.RA.6.6.0.pack").read_bytes(), b"do not touch")
                self.assertFalse((destination / "SEGGER.JLink.8.12.6.pack").exists())


if __name__ == "__main__":
    unittest.main()
