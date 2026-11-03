"""Exercise real pack inventories, ownership checks and rollback in temporary directories."""
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

import fsp_manifest as fsp
import install_fsp_packs as installer


def zip_bytes(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return stream.getvalue()


PACK = "Example.Device.1.2.3.pack"
SOURCE = "ra/fsp/src/example/example.c"


def candidate(directory, data, entry=SOURCE):
    directory.mkdir(parents=True, exist_ok=True)
    pdsc = ET.Element("package")
    component = ET.SubElement(ET.SubElement(pdsc, "components"), "component",
                              Cclass="HAL Drivers", Cgroup="all", Csub="example", Cversion="1.2.3")
    ET.SubElement(ET.SubElement(component, "files"), "file", name=entry, category="source")
    files = {"Example.pdsc": ET.tostring(pdsc), entry: data}
    raw = zip_bytes(files)
    (directory / PACK).write_bytes(raw)
    manifest = {"packs": {PACK: {"sha256": fsp.digest(raw), "entries": {
        name: fsp.digest(value) for name, value in files.items()}}}}
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return directory / "manifest.json"


def project(directory):
    directory.mkdir(parents=True, exist_ok=True)
    root = ET.Element("raConfiguration")
    node = ET.SubElement(ET.SubElement(root, "raComponentSelection"), "component", {
        "class": "HAL Drivers", "group": "all", "subgroup": "example", "version": "1.2.3"})
    ET.SubElement(node, "originalPack").text = PACK
    (directory / "configuration.xml").write_bytes(ET.tostring(root))
    path = directory / SOURCE
    path.parent.mkdir(parents=True)
    return path


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.current = candidate(self.root / "current", b"new")
        self.previous = candidate(self.root / "previous", b"old")
        self.source = project(self.root / "project")
        self.source.write_bytes(b"old")
        self.installed = self.root / "installed"
        self.installed.mkdir()
        (self.installed / PACK).write_bytes((self.current.parent / PACK).read_bytes())

    def refresh(self, generate=None, previous=None):
        return fsp.refresh_project_sources(self.source.parents[4], self.current,
            [self.previous] if previous is None else previous, self.installed,
            generate or (lambda: self.source.write_bytes(b"new")))

    def test_owned_old_source_is_backed_up_reextracted_and_verified(self):
        result = self.refresh()
        self.assertEqual(result["refreshed_sources"], [SOURCE])
        self.assertEqual(self.source.read_bytes(), b"new")
        backups = list((self.root / "project/.fsp-source-backup").rglob("example.c"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"old")

    def test_current_source_needs_no_historical_exception(self):
        self.source.write_bytes(b"new")
        result = self.refresh(lambda: None, previous=[])
        self.assertEqual(result["refreshed_sources"], [])
        self.assertFalse((self.root / "project/.fsp-source-backup").exists())

    def test_unknown_edit_is_preserved_before_generation(self):
        self.source.write_bytes(b"user change")
        with self.assertRaisesRegex(ValueError, "unknown local"):
            self.refresh(lambda: self.fail("Must not generate"))
        self.assertEqual(self.source.read_bytes(), b"user change")

    def test_old_source_requires_a_verified_previous_archive(self):
        with self.assertRaisesRegex(ValueError, "unknown local"):
            self.refresh(previous=[])
        (self.previous.parent / PACK).write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "Pack hash mismatch"):
            self.refresh()
        self.assertEqual(self.source.read_bytes(), b"old")

    def test_failed_generation_restores_missing_original(self):
        def fail():
            self.assertFalse(self.source.exists())
            raise RuntimeError("generation failed")
        with self.assertRaisesRegex(RuntimeError, "generation failed"):
            self.refresh(fail)
        self.assertEqual(self.source.read_bytes(), b"old")

    def test_wrong_generated_bytes_are_preserved_and_block_the_build(self):
        with self.assertRaisesRegex(ValueError, "Stale extraction"):
            self.refresh(lambda: self.source.write_bytes(b"unexpected output"))
        self.assertEqual(self.source.read_bytes(), b"unexpected output")

    def test_missing_replacement_restores_original(self):
        with self.assertRaisesRegex(ValueError, "did not replace"):
            self.refresh(lambda: None)
        self.assertEqual(self.source.read_bytes(), b"old")

    def test_installed_mismatch_stops_before_mutation(self):
        (self.installed / PACK).write_bytes(b"other pack")
        with self.assertRaisesRegex(ValueError, "Installed pack hash"):
            self.refresh()
        self.assertEqual(self.source.read_bytes(), b"old")

    def test_inventory_hash_and_extra_entries_are_checked(self):
        data = json.loads(self.current.read_text())
        data["packs"][PACK]["entries"][SOURCE] = "0" * 64
        self.current.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "entry hash"):
            fsp.read_manifest(self.current)
        del data["packs"][PACK]["entries"][SOURCE]
        self.current.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "inventory mismatch"):
            fsp.read_manifest(self.current)

    def test_symlinked_project_source_is_never_replaced(self):
        self.source.unlink()
        target = self.root / "outside"
        target.write_bytes(b"old")
        self.source.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "Symlink"):
            self.refresh()
        self.assertEqual(target.read_bytes(), b"old")

    def test_source_alias_and_linker_paths(self):
        self.assertEqual(fsp.project_source("ra~1/Source/system.c"),
                         "ra/fsp/src/bsp/cmsis/Device/RENESAS/Source/system.c")
        self.assertEqual(fsp.project_source("script/fsp.ld"), "script/fsp.ld")
        self.assertIsNone(fsp.project_source(".module_descriptions/module.xml"))

    def test_unsafe_paths_and_archive_symlinks(self):
        for name in ["../escape", "/absolute", "a/../escape", "C:drive", "a\\b", "a//b"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                fsp.safe_name(name)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            info = zipfile.ZipInfo("link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "../outside")
        with self.assertRaisesRegex(ValueError, "Unsupported archive"):
            fsp.read_zip(stream.getvalue())

    def test_support_scoping_uses_declared_version_including_board_profiles(self):
        for version in ["1.2.3", "7.8.9"]:
            short = ".".join(version.split(".")[:2])
            for value in ["/support/" + short + "/device.xmi",
                          "/support/Board##" + version + ".xml",
                          "/support/Board##" + version + "##configuration.xml"]:
                self.assertTrue(fsp.versioned_support_path(Path(value), version), value)
            self.assertFalse(fsp.versioned_support_path(Path("/support/shared/device.xml"), version))
            self.assertFalse(fsp.versioned_support_path(Path("/support/99.0/device.xml"), version))


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.destination = self.root / "installed"
        self.destination.mkdir()
        self.backup = self.root / "backup"

    def test_dry_run_idempotence_and_original_rollback_point(self):
        target = self.destination / PACK
        target.write_bytes(b"original")
        installer.install({PACK: b"first"}, self.destination, self.backup)
        self.assertFalse(self.backup.exists())
        self.assertEqual(target.read_bytes(), b"original")
        installer.install({PACK: b"first"}, self.destination, self.backup, True)
        installer.install({PACK: b"second"}, self.destination, self.backup, True)
        self.assertEqual((self.backup / (PACK + ".original")).read_bytes(), b"original")
        self.assertEqual(installer.install({PACK: b"second"}, self.destination, self.backup, True)["changes"], [])

    def test_all_targets_are_checked_before_any_install(self):
        first = self.destination / "A.pack"
        first.write_bytes(b"original")
        (self.destination / "Z.pack").symlink_to(self.root / "outside")
        with self.assertRaises(ValueError):
            installer.install({"A.pack": b"new", "Z.pack": b"new"}, self.destination, self.backup, True)
        self.assertEqual(first.read_bytes(), b"original")
        self.assertFalse(self.backup.exists())

    def test_corrupt_backup_and_wrong_destination_are_rejected(self):
        (self.destination / PACK).write_bytes(b"original")
        installer.install({PACK: b"first"}, self.destination, self.backup, True)
        (self.backup / (PACK + ".original")).write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "backup hash mismatch"):
            installer.install({PACK: b"second"}, self.destination, self.backup, True)
        self.assertEqual((self.destination / PACK).read_bytes(), b"first")
        other = self.root / "other"
        other.mkdir()
        with self.assertRaisesRegex(ValueError, "another installation"):
            installer.install({PACK: b"second"}, other, self.backup, True)

    def test_release_archive_hash_and_version_filter(self):
        pack = zip_bytes({"example.txt": b"original"})
        raw = zip_bytes({"internal/projectgen/ra/packs/Example.1.2.3.pack": pack,
                         "internal/projectgen/ra/packs/Example.7.8.9.pack": pack})
        archive = self.root / "release.zip"
        archive.write_bytes(raw)
        self.assertEqual(set(installer.archive_packs(archive, fsp.digest(raw), "1.2.3")), {"Example.1.2.3.pack"})
        with self.assertRaisesRegex(ValueError, "archive hash"):
            installer.archive_packs(archive, "0" * 64, "1.2.3")


if __name__ == "__main__":
    unittest.main()
