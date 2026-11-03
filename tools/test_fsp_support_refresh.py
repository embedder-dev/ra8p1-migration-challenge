"""Exercise scoped vendor calls and support-file journaling without launching an IDE."""
import io
import json
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest
from unittest.mock import Mock
import zipfile

from fsp_manifest import digest
from refresh_fsp_candidate import refresh


class JavaList(list):
    def add(self, value):
        self.append(value)


class Method:
    def __init__(self, name, count, callback):
        self.name, self.count, self.callback = name, count, callback

    def getName(self):
        return self.name

    def getParameterCount(self):
        return self.count

    def getParameterTypes(self):
        return [NS(getName=lambda: "java.util.EnumSet")] * self.count

    def setAccessible(self, value):
        pass

    def invoke(self, target, values):
        return self.callback(*values)


class SupportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.candidate = self.root / "candidate"
        self.installed = self.root / "installed"
        self.backup = self.root / "backup"
        self.candidate.mkdir()
        self.installed.mkdir()
        self.pack = "Example.Device.1.2.3.pack"
        self.board = "Example##Board####1.2.3##configuration.xml"
        self.files = {".module_descriptions/" + self.board: b"new board",
                      ".mcu/device.xml": b"new device", ".mcu/shared.xml": b"new shared"}
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            for name, data in self.files.items():
                archive.writestr(name, data)
        raw = stream.getvalue()
        (self.candidate / self.pack).write_bytes(raw)
        (self.installed / self.pack).write_bytes(raw)
        (self.installed / "Unselected.Device.7.8.9.pack").write_bytes(b"untouched")
        self.manifest = self.candidate / "manifest.json"
        self.manifest.write_text(json.dumps({"packs": {self.pack: {
            "sha256": digest(raw), "entries": {k: digest(v) for k, v in self.files.items()}}}}))
        self.targets = {".module_descriptions/" + self.board: self.root / "support/modules" / self.board,
                        ".mcu/device.xml": self.root / "support/1.2/device.xml",
                        ".mcu/shared.xml": self.root / "support/shared/shared.xml"}
        self.entries = []
        for source, target in self.targets.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"original")
            entry = Mock()
            entry.getVersion.return_value = "1.2.3"
            entry.getDestPathBase.return_value = "base"
            entry.getDestPathSupplementary.return_value = str(target.parent)
            entry.getSupportFileName.return_value = target.name
            entry.getPackBasedPath.return_value = source
            self.entries.append(entry)
        self.info = Mock()
        self.info.getFilename.return_value = str(self.installed / self.pack)
        self.info.getPackVersion.return_value = "1.2.3"
        entry = Mock()
        entry.getValue.return_value = self.info
        self.manager = Mock()
        self.manager.getPacksFolder.return_value.toOSString.return_value = str(self.installed)
        self.manager.getPackInfoMap.return_value.entrySet.return_value = [entry]
        self.support = self.manager.getPackSupportFileManager.return_value
        self.locations = Mock()
        self.locations.constructDestSupportFolder.side_effect = lambda base, folder, version, force: folder
        self.support.getClass.return_value.getDeclaredField.return_value.get.return_value = self.locations
        self.vendor_calls = []
        self.extracted = []
        self.methods = [Method("checkAndInstallInternal", 3, self.check),
                        Method("getToolingManifestEntries", 1, lambda tooling: self.entries),
                        Method("extractPackSupportFile", 4, self.extract)]
        self.support.getClass.return_value.getDeclaredMethods.side_effect = lambda: self.methods
        com = Mock()
        com.renesas.cdt.ddsc.packs.io.PackIOManagerFactory.getInstance.return_value.getPackManager.return_value = self.manager
        java = NS(util=NS(ArrayList=JavaList, EnumSet=NS(of=lambda value: value)),
                  lang=NS(Class=NS(forName=lambda name: name),
                          reflect=NS(Array=NS(newInstance=lambda kind, size: [None] * size))))
        self.api = {"com": com, "java": java}

    def check(self, selected, force, monitor):
        self.vendor_calls.append(list(selected))

    def extract(self, info, source, folder, flags):
        self.extracted.append(source)
        self.targets[source].write_bytes(self.files[source])
        return NS(getResult=lambda: 0)

    def run_refresh(self, apply=False):
        return refresh(self.api, self.manifest, self.installed, self.backup, apply)

    def test_dry_run_does_not_invoke_vendor_install_or_write_journal(self):
        self.assertFalse(self.run_refresh()["applied"])
        self.assertFalse(self.vendor_calls)
        self.assertFalse(self.backup.exists())
        self.assertTrue(all(p.read_bytes() == b"original" for p in self.targets.values()))

    def test_only_verified_pack_and_versioned_resources_are_refreshed(self):
        result = self.run_refresh(True)
        self.assertTrue(result["applied"])
        self.assertEqual(self.vendor_calls, [[self.info]])
        self.assertEqual(len(self.extracted), 2)
        self.assertIn(".module_descriptions/" + self.board, self.extracted)
        self.assertEqual(self.targets[".mcu/shared.xml"].read_bytes(), b"original")
        self.assertEqual((self.installed / "Unselected.Device.7.8.9.pack").read_bytes(), b"untouched")

    def test_journal_preserves_prior_bytes_and_second_refresh_is_idempotent(self):
        self.run_refresh(True)
        journal_path = self.backup / "journal.json"
        original = journal_path.read_bytes()
        journal = json.loads(original)
        for record in journal.values():
            self.assertEqual((self.backup / record["backup"]).read_bytes(), b"original")
        self.run_refresh(True)
        self.assertEqual(journal_path.read_bytes(), original)
        self.assertEqual(len(self.extracted), 2)

    def test_wrong_host_directory_or_pack_hash_stops_before_install(self):
        self.manager.getPacksFolder.return_value.toOSString.return_value = str(self.root / "other")
        with self.assertRaisesRegex(ValueError, "not this host"):
            self.run_refresh(True)
        self.assertFalse(self.vendor_calls)
        (self.installed / self.pack).write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "Installed pack hash"):
            self.run_refresh(True)

    def test_unsupported_internal_api_fails_instead_of_falling_back(self):
        self.methods = []
        with self.assertRaisesRegex(RuntimeError, "vendor method"):
            self.run_refresh(True)
        self.assertFalse(self.extracted)


if __name__ == "__main__":
    unittest.main()
