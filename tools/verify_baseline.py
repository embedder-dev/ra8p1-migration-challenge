#!/usr/bin/env python3
"""Verify only the untouched FSP 5.9.0 baseline and its original metadata.

This tool does not download, generate, compare against, or apply any solution.
Run it before beginning the challenge; intentional baseline edits will fail it.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import xml.etree.ElementTree as ET
import zipfile

RELEASES = {
    "5.9.0": ("5e8fbb10c752e16272790bd3a2587485d0377835",
              "99c4abae6239a68171fcbff86070971f591f0a8945e90a50eb07d87843bee363"),
}


def git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], stderr=subprocess.PIPE,
        env=dict(os.environ, GIT_NO_REPLACE_OBJECTS="1"))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def contained_file(root: Path, relative: str) -> Path:
    candidate = root / relative
    require(not Path(relative).is_absolute() and ".." not in Path(relative).parts,
            f"Unsafe artifact path: {relative}")
    require(candidate.resolve().is_relative_to(root.resolve()), f"Artifact escapes bundle: {relative}")
    require(candidate.is_file() and not candidate.is_symlink(), f"Missing or symlinked artifact: {relative}")
    parent = candidate.parent
    while parent != root:
        require(not parent.is_symlink(), f"Symlinked artifact directory: {relative}")
        parent = parent.parent
    return candidate


def verify_sources(repo: Path, version: str = "5.9.0") -> dict:
    commit = RELEASES[version][0]
    require(git(repo, "rev-parse", "HEAD").decode().strip() == commit, f"Wrong {version} revision")
    require(not git(repo, "status", "--porcelain=v1", "--untracked-files=no"), f"Dirty tracked {version} sources")
    count = 0
    for record in filter(None, git(repo, "ls-tree", "-rz", "HEAD").split(b"\0")):
        header, name = record.split(b"\t", 1)
        mode, kind, blob = header.decode().split()
        require(kind == "blob", f"Unsupported source object {name!r}")
        path = repo / name.decode()
        if mode == "120000":
            require(path.is_symlink(), f"Missing symlink {path}")
            data = str(path.readlink()).encode()
        else:
            require(mode in ("100644", "100755"), f"Unsupported source mode {mode}")
            contained_file(repo, name.decode())
            require(bool(path.stat().st_mode & 0o111) == (mode == "100755"), f"Source mode differs: {name.decode()}")
            data = path.read_bytes()
        actual = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        require(actual == blob, f"Source bytes differ from {version}: {name.decode()}")
        count += 1
    return {"version": version, "commit": commit, "tracked_files_verified": count}


def archive_mdfs(archive: Path, version: str | None = None) -> tuple[dict[str, str], int, int]:
    """Derive metadata hashes directly from the original release archive."""
    mdfs: dict[str, str] = {}
    packs = xml_count = 0
    with zipfile.ZipFile(archive) as outer:
        for entry in sorted(outer.namelist()):
            if not entry.endswith(".pack"):
                continue
            packs += 1
            with zipfile.ZipFile(io.BytesIO(outer.read(entry))) as pack:
                for member in pack.infolist():
                    if member.is_dir():
                        continue
                    name = member.filename
                    if name.startswith(".module_descriptions/") and name.endswith(".xml"):
                        dest = name.removeprefix(".module_descriptions/")
                    elif name == "toolingSupport.xml" and entry.rsplit("/", 1)[-1] == f"Renesas.RA.{version}.pack":
                        dest = name
                    else:
                        continue
                    require(dest and "\\" not in dest and ":" not in dest and
                            not PurePosixPath(dest).is_absolute() and
                            all(part not in ("", ".", "..") for part in dest.split("/")),
                            f"Unsafe MDF path: {dest}")
                    require(stat.S_IFMT(member.external_attr >> 16) in (0, stat.S_IFREG),
                            f"Non-regular MDF: {dest}")
                    data = pack.read(member)
                    require(b"<!DOCTYPE" not in data.upper() and b"<!ENTITY" not in data.upper(),
                            f"DTD/entity in MDF: {dest}")
                    ET.fromstring(data)
                    xml_count += 1
                    digest = hashlib.sha256(data).hexdigest()
                    require(dest not in mdfs or mdfs[dest] == digest, f"Conflicting MDF in packs: {dest}")
                    mdfs[dest] = digest
    require(packs > 0 and bool(mdfs), "Archive contains no pack metadata")
    return mdfs, packs, xml_count


def verify_packs(root: Path) -> dict:
    version = "5.9.0"
    checksum = RELEASES[version][1]
    archive = contained_file(root, "reference/vendor/FSP_Packs_v5.9.0.zip")
    require(sha256(archive) == checksum, "Archive checksum mismatch")
    expected, packs, xml_count = archive_mdfs(archive, version)
    directory = root / "baseline/module_descriptions"
    require(directory.is_dir() and not directory.is_symlink(), "Missing or symlinked metadata directory")
    actual = {}
    for path in directory.rglob("*"):
        require(not path.is_symlink(), f"Symlink in metadata: {path}")
        if path.is_file():
            relative = path.relative_to(directory).as_posix()
            actual[relative] = sha256(contained_file(directory, relative))
    require(actual == expected, "Extracted metadata set or bytes differ from original packs")
    return {"version": version, "archive_sha256": checksum, "packs": packs,
            "xml_entries_parsed": xml_count, "metadata_files_verified": len(expected)}


def verify_baseline_additions(root: Path) -> dict:
    repo = root / "baseline"
    names = git(repo, "ls-files", "--others", "-z").decode().split("\0")
    names = list(filter(None, names))
    for name in names:
        require(name.startswith("module_descriptions/"), f"Unexpected baseline addition: {name}")
    return {"untracked_files_verified": len(names), "only_original_metadata_added": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--report", type=Path, help="Write the baseline-only JSON report to this path")
    args = parser.parse_args()
    root = args.root.resolve()
    results = {}
    for name, check in (
        ("baseline_sources", lambda: verify_sources(root / "baseline")),
        ("original_metadata", lambda: verify_packs(root)),
        ("baseline_additions", lambda: verify_baseline_additions(root)),
    ):
        try:
            results[name] = {"passed": True, "details": check()}
        except (ValueError, OSError, subprocess.CalledProcessError, zipfile.BadZipFile, ET.ParseError) as error:
            results[name] = {"passed": False, "error": str(error)}
    report = {"success": all(result["passed"] for result in results.values()),
              "scope": "FSP 5.9.0 baseline integrity only; no solution, build or hardware validation",
              "checks": results}
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.write_text(text)
    print(text, end="")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
