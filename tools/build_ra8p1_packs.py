#!/usr/bin/env python3
"""Build custom 5.9-derived packs without reading an installed/newer FSP.

The original archive is immutable input. New target-specific pack resources live
in packaging/ra8p1/{mcu,board}; missing resources are errors, not stock fallbacks.
This command does not install packs or claim that generated firmware works.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import stat
import xml.etree.ElementTree as ET
import zipfile

from verify_baseline import RELEASES, archive_mdfs, git, require

ARCHIVE_SHA256 = "99c4abae6239a68171fcbff86070971f591f0a8945e90a50eb07d87843bee363"
VERSION = "5.9.0"
NEW_DRIVERS = {"r_mram": "r_flash_api.h", "r_dac_b": "r_dac_api.h", "r_pdm": "r_pdm_api.h"}
CMSIS_PREFIX = "ra/fsp/src/bsp/cmsis/Device/RENESAS/"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_name(name: str) -> str:
    p = PurePosixPath(name)
    if not name or p.is_absolute() or ".." in p.parts or "\\" in name or ":" in name:
        raise ValueError(f"Unsafe archive path: {name}")
    return name


def read_zip(data: bytes) -> dict[str, bytes]:
    result = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for entry in archive.infolist():
            safe_name(entry.filename)
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError(f"Unsupported archive entry: {entry.filename}")
            if entry.is_dir():
                continue
            if entry.filename in result:
                raise ValueError(f"Duplicate archive entry: {entry.filename}")
            result[entry.filename] = archive.read(entry)
    return result


def xml(data: bytes) -> ET.Element:
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("XML entities/DTDs are not supported")
    return ET.fromstring(data)


def xml_bytes(root: ET.Element) -> bytes:
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def pack_bytes(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(files.items()):
            safe_name(name)
            info = zipfile.ZipInfo(name, date_time=(2025, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return output.getvalue()


def source_path(root: Path, name: str) -> Path | None:
    if name.startswith("ra~1/"):
        name = CMSIS_PREFIX + name[len("ra~1/"):]
    elif name.startswith(".module_descriptions/"):
        name = "module_descriptions/" + name[len(".module_descriptions/"):]
    else:
        if not name.startswith("ra/"):
            return None
    candidate = root / safe_name(name)
    if candidate.is_symlink() or not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Source is outside baseline: {candidate}")
    return candidate if candidate.is_file() else None


def overlay_sources(files: dict[str, bytes], baseline: Path,
                    changed_paths: set[str] | None = None) -> dict[str, bytes]:
    result = dict(files)
    for name in files:
        source = source_path(baseline, name)
        if source is not None:
            relative = source.relative_to(baseline).as_posix()
            if changed_paths is None or relative in changed_paths:
                result[name] = source.read_bytes()
    return result


def add_drivers(files: dict[str, bytes], baseline: Path) -> None:
    pdsc = xml(files["Renesas.RA.pdsc"])
    components = pdsc.find("components")
    if components is None:
        raise ValueError("Main pack has no components")
    tooling = xml(files["toolingSupport.xml"])
    for driver, api in NEW_DRIVERS.items():
        if any(c.get("Csub") == driver for c in components):
            raise ValueError(f"Driver already exists in original pack: {driver}")
        component = ET.SubElement(components, "component", Cvendor="Renesas", Cclass="HAL Drivers",
                                  Cgroup="all", Csub=driver, Cversion=VERSION)
        ET.SubElement(component, "description").text = f"Custom 5.9-derived RA8P1 {driver}"
        group = ET.SubElement(component, "files")
        for name, category in [(f"ra/fsp/src/{driver}/{driver}.c", "source"),
                               (f"ra/fsp/inc/instances/{driver}.h", "header"),
                               (f"ra/fsp/inc/api/{api}", "header")]:
            source = source_path(baseline, name)
            if source is None:
                raise ValueError(f"Missing driver source: {name}")
            files[name] = source.read_bytes()
            ET.SubElement(group, "file", category=category, name=name, version=VERSION)
        mdf = f"Renesas##HAL Drivers##all##{driver}####{VERSION}.xml"
        data = (baseline / "module_descriptions" / mdf).read_bytes()
        xml(data)
        files[".module_descriptions/" + mdf] = data
        ET.SubElement(tooling, "file", supportType="Modules", version=VERSION).text = ".module_descriptions/" + mdf
    cgc = next(c for c in components if c.get("Csub") == "r_cgc")
    include = "ra/fsp/src/r_cgc/r_cgc_ra8p1.inc"
    source = source_path(baseline, include)
    if source is None:
        raise ValueError("Missing RA8P1 CGC implementation include")
    files[include] = source.read_bytes()
    ET.SubElement(cgc.find("files"), "file", category="header", name=include, version=VERSION)
    pdsc.find("description").text = "Custom 5.9-derived RA8P1 support candidate; not an official Renesas release"
    files["Renesas.RA.pdsc"] = xml_bytes(pdsc)
    files["toolingSupport.xml"] = xml_bytes(tooling)


def authored_pack(directory: Path, baseline: Path, kind: str) -> dict[str, bytes]:
    if not directory.is_dir():
        raise ValueError(f"Missing {kind} pack resources: {directory}")
    files = {}
    for p in sorted(directory.rglob("*")):
        if p.is_symlink():
            raise ValueError(f"Symlink in pack resources: {p}")
        if p.is_file():
            files[safe_name(p.relative_to(directory).as_posix())] = p.read_bytes()
    pdscs = [n for n in files if n.endswith(".pdsc")]
    if len(pdscs) != 1 or "toolingSupport.xml" not in files:
        raise ValueError(f"{kind} requires one PDSC and toolingSupport.xml")
    pdsc = xml(files[pdscs[0]])
    # The PDSC is the authoritative source list, including common BSP files.
    for entry in pdsc.findall(".//files/file"):
        name = entry.get("name", "")
        if not name or name.endswith("/"):
            continue
        if name not in files:
            source = source_path(baseline, name)
            if source is None:
                raise ValueError(f"Missing {kind} component resource: {name}")
            files[name] = source.read_bytes()
    tooling = xml(files["toolingSupport.xml"])
    for entry in tooling.findall("file"):
        name = (entry.text or "").strip()
        if name not in files:
            source = source_path(baseline, name)
            if source is None:
                raise ValueError(f"Missing {kind} tooling resource: {name}")
            files[name] = source.read_bytes()
    for name, data in files.items():
        if name.endswith((".xml", ".pdsc", ".xmi", ".pincfg", ".rzone")):
            xml(data)
    return files


def baseline_changes(root: Path) -> dict[str, dict]:
    """Account for intentional changes without weakening the pristine verifier."""
    baseline = root / "baseline"
    commit = RELEASES[VERSION][0]
    require(git(baseline, "rev-parse", "HEAD").decode().strip() == commit,
            "Preserved baseline HEAD changed")
    metadata, _, _ = archive_mdfs(root / "reference/vendor/FSP_Packs_v5.9.0.zip", VERSION)
    changes = {}
    tracked_changes = filter(None, git(baseline, "diff", "--name-only", "-z", "HEAD").decode().split("\0"))
    for name in tracked_changes:
        before = digest(git(baseline, "show", commit + ":" + name))
        source = baseline / safe_name(name)
        if source.is_symlink():
            raise ValueError(f"Symlinked candidate source: {name}")
        after = digest(source.read_bytes()) if source.is_file() else None
        changes[name] = {"before_sha256": before, "after_sha256": after}
    additions = filter(None, git(baseline, "ls-files", "--others", "-z").decode().split("\0"))
    for name in additions:
        source = baseline / safe_name(name)
        if source.is_symlink():
            raise ValueError(f"Symlinked candidate source: {name}")
        after = digest(source.read_bytes())
        before = metadata.get(name.removeprefix("module_descriptions/")) if name.startswith("module_descriptions/") else None
        if before != after:
            changes[name] = {"before_sha256": before, "after_sha256": after}
    for name, before in metadata.items():
        relative = "module_descriptions/" + name
        if not (baseline / relative).is_file():
            changes[relative] = {"before_sha256": before, "after_sha256": None}
    return dict(sorted(changes.items()))


def changed_existing_packs(original: dict[str, bytes], baseline: Path,
                           changed_paths: set[str] | None = None) -> dict[str, bytes]:
    """Overlay every source-owning pack, including shared BSP copies in MCU packs."""
    result = {}
    prefix = "internal/projectgen/ra/packs/"
    for path, raw in sorted(original.items()):
        if not path.startswith(prefix) or not path.endswith(".pack"):
            continue
        name = path[len(prefix):]
        if "/" in name:
            raise ValueError(f"Unexpected nested pack: {path}")
        files = read_zip(raw)
        overlaid = overlay_sources(files, baseline, changed_paths)
        if files != overlaid:
            result[name] = pack_bytes(overlaid)
    return result


def build(root: Path, output: Path) -> dict:
    archive = root / "reference/vendor/FSP_Packs_v5.9.0.zip"
    raw = archive.read_bytes()
    if digest(raw) != ARCHIVE_SHA256:
        raise ValueError("Original FSP 5.9.0 archive checksum mismatch")
    original = read_zip(raw)
    main_name = f"Renesas.RA.{VERSION}.pack"
    main_original = original["internal/projectgen/ra/packs/" + main_name]
    changes = baseline_changes(root)
    changed_paths = set(changes)
    main = overlay_sources(read_zip(main_original), root / "baseline", changed_paths)
    add_drivers(main, root / "baseline")
    packs = changed_existing_packs(original, root / "baseline", changed_paths)
    packs[main_name] = pack_bytes(main)
    for kind, name in [("mcu", f"Renesas.RA_mcu_ra8p1.{VERSION}.pack"),
                       ("board", f"Renesas.RA_board_ra8p1_ek.{VERSION}.pack")]:
        packs[name] = pack_bytes(authored_pack(root / "packaging/ra8p1" / kind, root / "baseline", kind))
    manifest = {"candidate": "custom-5.9-derived-ra8p1", "source_archive_sha256": ARCHIVE_SHA256,
                "baseline_commit": RELEASES[VERSION][0], "baseline_changes": changes,
                "firmware_verified": False, "packs": {}}
    # All resources must validate before any output pack is written.
    output.mkdir(parents=True, exist_ok=True)
    for name, data in sorted(packs.items()):
        (output / name).write_bytes(data)
        entries = read_zip(data)
        manifest["packs"][name] = {"sha256": digest(data), "entries": {n: digest(d) for n, d in sorted(entries.items())}}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("validation/packs"))
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output if args.output.is_absolute() else root / args.output
    manifest = build(root, output)
    print(json.dumps({"candidate": manifest["candidate"], "packs": {
        n: v["sha256"] for n, v in manifest["packs"].items()}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
