#!/usr/bin/env python3
"""Failure-case checks for the baseline-only integrity verifier."""
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

import verify_baseline as verify


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def source(self):
        data = b"original\r\n"
        (self.root / "file.c").write_bytes(data)
        blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        tree = f"100644 blob {blob}\tfile.c\0".encode()

        def fake_git(repo, *args):
            if args[0] == "rev-parse":
                return (verify.RELEASES["5.9.0"][0] + "\n").encode()
            if args[0] == "status":
                return b""
            return tree

        return fake_git

    def test_exact_source_bytes_pass(self):
        with patch.object(verify, "git", side_effect=self.source()):
            self.assertEqual(verify.verify_sources(self.root, "5.9.0")["tracked_files_verified"], 1)

    def test_changed_source_bytes_fail_even_with_clean_status(self):
        fake = self.source()
        (self.root / "file.c").write_bytes(b"changed\n")
        with patch.object(verify, "git", side_effect=fake), self.assertRaisesRegex(ValueError, "Source bytes differ"):
            verify.verify_sources(self.root, "5.9.0")

    def test_wrong_revision_fails(self):
        with patch.object(verify, "git", return_value=b"wrong\n"), self.assertRaisesRegex(ValueError, "revision"):
            verify.verify_sources(self.root, "5.9.0")

    def test_symlink_cannot_replace_regular_source(self):
        fake = self.source()
        (self.root / "elsewhere.c").write_bytes((self.root / "file.c").read_bytes())
        (self.root / "file.c").unlink()
        (self.root / "file.c").symlink_to("elsewhere.c")
        with patch.object(verify, "git", side_effect=fake), self.assertRaisesRegex(ValueError, "symlinked"):
            verify.verify_sources(self.root, "5.9.0")

    def archive(self, entries):
        out = self.root / "packs.zip"
        with zipfile.ZipFile(out, "w") as outer:
            for index, files in enumerate(entries):
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w") as inner:
                    for name, data in files.items():
                        inner.writestr(name, data)
                outer.writestr(f"packs/p{index}.pack", buf.getvalue())
        return out

    def test_identical_duplicate_metadata_collapses(self):
        path = self.archive([{'.module_descriptions/a.xml': '<module />'}] * 2)
        files, packs, entries = verify.archive_mdfs(path)
        self.assertEqual((len(files), packs, entries), (1, 2, 2))

    def test_conflicting_duplicate_metadata_fails(self):
        path = self.archive([{'.module_descriptions/a.xml': '<a />'}, {'.module_descriptions/a.xml': '<b />'}])
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            verify.archive_mdfs(path)

    def test_only_main_pack_tooling_is_copied_to_mdf_directory(self):
        archive = self.root / "packs.zip"
        with zipfile.ZipFile(archive, "w") as outer:
            for name, content in [("Renesas.RA.5.9.0.pack", b"<main />"),
                                  ("Renesas.Other.5.9.0.pack", b"<other />")]:
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w") as inner:
                    inner.writestr("toolingSupport.xml", content)
                outer.writestr("packs/" + name, buf.getvalue())
        files, packs, entries = verify.archive_mdfs(archive, "5.9.0")
        self.assertEqual((packs, entries), (2, 1))
        self.assertEqual(files, {"toolingSupport.xml": hashlib.sha256(b"<main />").hexdigest()})

    def test_malformed_xml_fails(self):
        path = self.archive([{'.module_descriptions/a.xml': '<a>'}])
        with self.assertRaises(ET.ParseError):
            verify.archive_mdfs(path)

    def test_metadata_traversal_fails(self):
        path = self.archive([{'.module_descriptions/../a.xml': '<a />'}])
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            verify.archive_mdfs(path)

    def test_artifact_path_cannot_escape_bundle(self):
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            verify.contained_file(self.root, "../outside")

    def test_artifact_symlink_cannot_escape_bundle(self):
        with tempfile.TemporaryDirectory() as outside:
            other = Path(outside) / "outside.txt"
            other.write_text("outside")
            (self.root / "link.txt").symlink_to(other)
            with self.assertRaisesRegex(ValueError, "escapes"):
                verify.contained_file(self.root, "link.txt")

    def test_wrong_source_mode_fails(self):
        fake = self.source()
        (self.root / "file.c").chmod(0o755)
        with patch.object(verify, "git", side_effect=fake), self.assertRaisesRegex(ValueError, "mode differs"):
            verify.verify_sources(self.root)

    def test_wrong_archive_checksum_fails(self):
        directory = self.root / "reference/vendor"
        directory.mkdir(parents=True)
        (directory / "FSP_Packs_v5.9.0.zip").write_bytes(b"not the release archive")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            verify.verify_packs(self.root)

    def test_only_metadata_additions_allowed(self):
        with patch.object(verify, "git", return_value=b"module_descriptions/module.xml\0"):
            self.assertEqual(verify.verify_baseline_additions(self.root)["untracked_files_verified"], 1)
        with patch.object(verify, "git", return_value=b"unexpected.c\0"):
            with self.assertRaisesRegex(ValueError, "Unexpected baseline addition"):
                verify.verify_baseline_additions(self.root)

    def test_ignored_baseline_additions_are_not_skipped(self):
        with patch.object(verify, "git", return_value=b"") as mocked:
            verify.verify_baseline_additions(self.root)
        self.assertNotIn("--exclude-standard", mocked.call_args.args)

    def test_xml_entities_are_rejected(self):
        path = self.archive([{'.module_descriptions/a.xml': '<!DOCTYPE a><a />'}])
        with self.assertRaisesRegex(ValueError, "DTD/entity"):
            verify.archive_mdfs(path)


if __name__ == "__main__":
    unittest.main()
