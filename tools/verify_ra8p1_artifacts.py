#!/usr/bin/env python3
r"""Verify generated RA8P1 build artifacts, never runtime behavior.

Example (from the repository root)::

    python3 tools/verify_ra8p1_artifacts.py \
        --project validation/projects/candidate_ra8p1_cpu0 \
        --elf validation/projects/candidate_ra8p1_cpu0/Debug/candidate_ra8p1_cpu0.elf \
        --core CM85 --pretty

FSP omits content-only core configs when saving configuration.xml. The physical
part, Core and DeviceCommand must agree with the hash-verified candidate MDFs;
any explicit core config must match. Generated core headers must match the
ELF's absolute __ra8p1_core symbol. Load
ranges must stay inside that core's allocation, excluding scratch/shared memory.

Freshness requires the ELF to be no older than the manifest and every project
input under ra/, ra_cfg/, ra_gen/, src/ and script/, plus configuration.xml and
.cproject. --not-before FILE adds a caller's build-start marker. This is local
hash/mtime evidence, not a signed build attestation. No generated files are edited.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET


PART = "R7KA8P1KFLCAC"
VERSION = "5.9.0"
CORES = {"CM85": (0, "cortex-m85"), "CM33": (1, "cortex-m33")}
REGIONS = ((0x02000000, 0x02100000), (0x22000000, 0x221A0000))
PARTITIONS = {
    0: ((0x02000000, 0x02080000), (0x22000000, 0x220C0000)),
    1: ((0x02080000, 0x020FF000), (0x220C0000, 0x22180000)),
}
RESERVED = ((0x020FF000, 0x02100000), (0x22180000, 0x221A0000))
CMSIS = "ra/fsp/src/bsp/cmsis/Device/RENESAS/"
TARGET_PACKS = ("Renesas.RA_mcu_ra8p1.5.9.0.pack", "Renesas.RA_board_ra8p1_ek.5.9.0.pack")
CORE_MDFS = {
    "part": f"Renesas##BSP##ra8p1##device##{PART}##{VERSION}.xml",
    "device": f"Renesas##BSP##ra8p1##device####{VERSION}.xml",
    "fsp": f"Renesas##BSP##ra8p1##fsp####{VERSION}.xml",
}
FORBIDDEN = re.compile(r"^\.(?:option|id_code|rom_register|secur(?:e|ity)|otp|fuse|fsbl|pbps|bps|ofs)", re.I)
HEX = r"(?:0x)?[0-9a-fA-F]+"
LOAD = re.compile(r"^\s*LOAD\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+"
                  r"(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+([RWE ]+)\s+(0x[0-9a-fA-F]+)\s*$")
SECTION = re.compile(r"^\s*\[\s*(\d+)\]\s+(\S+)\s+(\S+)\s+(" + HEX + r")\s+(" + HEX +
                     r")\s+(" + HEX + r")\s+(" + HEX + r")\s+(.*?)\s+(\d+)\s+(\d+)\s+(\d+)\s*$")


class VerificationError(ValueError):
    """Missing, inconsistent or unsafe evidence."""


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def sha256(path):
    with path.open("rb") as stream:
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def regular_file(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), f"Missing regular evidence file: {path}")
    require(path.stat().st_size > 0, f"Empty evidence file: {path}")
    return path


def within(root, name):
    path = root / name
    require(path.resolve().is_relative_to(root.resolve()), f"Evidence path escapes root: {path}")
    return regular_file(path)


def xml_file(path):
    raw = regular_file(path).read_bytes()
    require(b"<!DOCTYPE" not in raw.upper() and b"<!ENTITY" not in raw.upper(), "XML DTD/entities forbidden")
    return ET.fromstring(raw)


def configuration(project, expected_core):
    root = xml_file(project / "configuration.xml")
    require(root.tag == "raConfiguration", "Not an FSP configuration.xml")
    settings = {}
    for node in root.findall("./generalSettings/option"):
        key = node.get("key")
        require(key not in settings, f"Duplicate configuration setting: {key}")
        settings[key] = node.get("value")
    expected = {"#FSPVersion#": VERSION, "#TargetName#": PART, "Core": expected_core,
                "#SELECTED_TOOLCHAIN#": "gcc-arm-embedded"}
    for key, value in expected.items():
        require(settings.get(key) == value, f"{key}: expected {value}, got {settings.get(key)!r}")
    cpu, architecture = CORES[expected_core]
    require(settings.get("#TargetARCHITECTURE#") == architecture, "Core/target architecture mismatch")
    require(settings.get("#DeviceCommand#") == f"R7KA8P1KF_CPU{cpu}",
            "Core/#DeviceCommand# mismatch or missing debugger device")
    tool_version = settings.get("#ToolchainVersion#", "")
    require(isinstance(tool_version, str), "Missing or invalid toolchain version evidence")
    match = re.match(r"^(\d+\.\d+\.\d+)(?:\.|$)", tool_version)
    require(match is not None, "Missing or invalid toolchain version evidence")
    require(not root.findall(".//property[@id='config.bsp.ra8p1.cpu']"),
            "Obsolete RA8P1 BSP core selection property")
    bsp = root.findall("./raBspConfiguration/config")
    selected = [node.get("id") for node in bsp if node.get("id", "").startswith("config.bsp.ra8p1.cpu")]
    require(not selected or selected == [f"config.bsp.ra8p1.cpu{cpu}"],
            f"Invalid, duplicate or mismatched RA8P1 BSP core selection: expected config.bsp.ra8p1.cpu{cpu}, got {selected}")
    require(sum(node.get("id") == f"config.bsp.ra8p1.{PART}" for node in bsp) == 1,
            "Expected exactly one physical RA8P1 part configuration")
    components = root.findall("./raComponentSelection/component")
    drivers = set()
    for node in components:
        if node.get("class") in ("BSP", "HAL Drivers"):
            require(node.get("version") == VERSION, "Selected BSP/driver is not FSP 5.9.0")
        if node.get("class") == "HAL Drivers":
            driver = node.get("subgroup", "")
            require(re.fullmatch(r"r_[a-z0-9_]+", driver), "Invalid selected driver name")
            drivers.add(driver)
    require(components, "Missing selected-component evidence")
    for subgroup, variant in (("device", PART), ("device", ""), ("fsp", "")):
        matches = [node for node in components if node.get("class") == "BSP" and
                   node.get("group") == "ra8p1" and node.get("subgroup") == subgroup and
                   node.get("variant", "") == variant]
        require(len(matches) == 1 and matches[0].findtext("originalPack") == TARGET_PACKS[0],
                f"Missing or duplicate candidate BSP component: {subgroup}/{variant}")
    return {"fsp_version": VERSION, "part": PART, "core": expected_core, "bsp_core": cpu,
            "bsp_config": f"config.bsp.ra8p1.cpu{cpu}", "bsp_config_serialized": bool(selected),
            "device_command": settings["#DeviceCommand#"], "architecture": architecture,
            "toolchain_version": match[1], "drivers": sorted(drivers)}


def generated_core(project, expected):
    directory = project / "ra_cfg/fsp_cfg/bsp"
    regular_file(directory / "bsp_cfg.h")
    core_path = regular_file(directory / "bsp_mcu_core_cfg.h")
    family_path = regular_file(directory / "bsp_mcu_family_cfg.h")
    definitions = {"BSP_CFG_CPU_CORE_FROM_DEVICE": [], "BSP_CFG_CPU_CORE": []}
    texts = {}
    for path in sorted(directory.rglob("*.h")):
        text = re.sub(r"/\*.*?\*/|//[^\n]*", "", within(project, path.relative_to(project)).read_text(), flags=re.S)
        texts[path] = text
        require(not re.search(r"^[ \t]*#[ \t]*undef[ \t]+BSP_CFG_CPU_CORE(?:_FROM_DEVICE)?\b", text, re.M),
                f"Unexpected BSP_CFG_CPU_CORE undefinition in {path}")
        for match in re.finditer(r"^[ \t]*#[ \t]*define[ \t]+(BSP_CFG_CPU_CORE(?:_FROM_DEVICE)?)\b([^\n]*)", text, re.M):
            require(match[2].startswith((" ", "\t")), f"Nonliteral or malformed {match[1]} in {path}")
            definitions[match[1]].append((path, match[2].strip()))
    for name, values in definitions.items():
        require(len(values) == 1, f"Expected exactly one generated {name} definition")
    path, literal = definitions["BSP_CFG_CPU_CORE_FROM_DEVICE"][0]
    require(path == core_path, "BSP_CFG_CPU_CORE_FROM_DEVICE must come from bsp_mcu_core_cfg.h")
    while literal.startswith("(") and literal.endswith(")"):
        literal = literal[1:-1].strip()
    match = re.fullmatch(r"([01])(?:U|UL)?", literal)
    require(match is not None, "Nonliteral BSP_CFG_CPU_CORE_FROM_DEVICE")
    value = int(match[1])
    require(value == expected, f"Generated BSP_CFG_CPU_CORE_FROM_DEVICE is {value}, expected {expected}")
    alias_path, alias = definitions["BSP_CFG_CPU_CORE"][0]
    require(alias_path == family_path and alias == "BSP_CFG_CPU_CORE_FROM_DEVICE",
            "BSP_CFG_CPU_CORE must alias BSP_CFG_CPU_CORE_FROM_DEVICE in bsp_mcu_family_cfg.h")
    lines = ["".join(line.split()) for line in texts[family_path].splitlines() if line.strip()]
    alias_line = "#defineBSP_CFG_CPU_COREBSP_CFG_CPU_CORE_FROM_DEVICE"
    index = lines.index(alias_line)
    require(index > 0 and lines[index - 1] == "#ifndefBSP_CFG_CPU_CORE" and
            index + 1 < len(lines) and lines[index + 1] == "#endif",
            "Missing BSP_CFG_CPU_CORE guarded alias")
    guard = "#ifdefined(BSP_CFG_CPU_CORE)&&BSP_CFG_CPU_CORE!=BSP_CFG_CPU_CORE_FROM_DEVICE"
    require(guard in lines, "Missing BSP_CFG_CPU_CORE conflict guard")
    guard_index = lines.index(guard)
    require(guard_index + 2 < index and lines[guard_index + 1].startswith("#error") and
            lines[guard_index + 2] == "#endif" and
            '#include"bsp_mcu_core_cfg.h"' in lines[:guard_index],
            "Invalid BSP_CFG_CPU_CORE conflict guard or missing core include")
    return {"path": str(core_path), "value": value, "sha256": sha256(core_path),
            "alias_path": str(family_path), "alias_sha256": sha256(family_path)}


def normalized_source(name):
    require(isinstance(name, str) and name, "Invalid manifest entry name")
    parsed = PurePosixPath(name)
    require(not parsed.is_absolute() and ".." not in parsed.parts and "\\" not in name and ":" not in name,
            f"Unsafe manifest path: {name}")
    if name.startswith("ra~1/"):
        name = CMSIS + name[len("ra~1/"):]
    return name


def hash_value(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), "Invalid manifest SHA256")
    return value


def core_metadata(baseline, packs, config):
    pack = packs[TARGET_PACKS[0]]
    require(isinstance(pack, dict) and isinstance(pack.get("entries"), dict), "Missing candidate MCU MDF inventory")
    roots, checked = {}, []
    for kind, name in CORE_MDFS.items():
        entry = ".module_descriptions/" + name
        require(entry in pack["entries"], f"Missing candidate MDF hash: {name}")
        digest = hash_value(pack["entries"][entry])
        path = within(baseline, "module_descriptions/" + name)
        require(sha256(path) == digest, f"Stale candidate MDF hash: {name}")
        roots[kind] = xml_file(path)
        require(roots[kind].tag == "raModuleDescription", f"Invalid candidate MDF: {name}")
        checked.append({"path": "module_descriptions/" + name, "sha256": digest})
    cpu = config["bsp_core"]
    devices = [node for node in roots["part"].findall("device")
               if node.get("device", node.get("id")) == PART and node.get("core") == config["core"]]
    require(len(devices) == 1, "Candidate MDF must select one physical device/core")
    selected = [node.get("bsp") for node in devices[0].findall("select")]
    require([name for name in selected if name and name.startswith("bsp.ra8p1.cpu")] == [f"bsp.ra8p1.cpu{cpu}"],
            "Candidate MDF device/core selection mismatch")
    require(f"bsp.ra8p1.{PART}" in selected and "bsp.ra8p1.fsp" in selected,
            "Candidate MDF missing physical part or family selection")
    configs = roots["device"].findall(f"config[@id='config.bsp.ra8p1.cpu{cpu}']")
    require(len(configs) == 1 and configs[0].get("path") == "fsp_cfg/bsp/bsp_mcu_core_cfg.h",
            "Missing candidate MDF core configuration")
    if not config["bsp_config_serialized"]:
        # BspConfiguration.saveBspContributionModuleConfig skips property-free configs.
        require(not configs[0].findall(".//property"), "Omitted BSP core config is not content-only")
    content = "".join(configs[0].findtext("content", "").split())
    require(content == f"#defineBSP_CFG_CPU_CORE_FROM_DEVICE({cpu})", "Candidate MDF core literal mismatch")
    selectors = roots["device"].findall(f"bsp[@id='bsp.ra8p1.cpu{cpu}']")
    require(len(selectors) == 1 and selectors[0].get("config") == config["bsp_config"],
            "Candidate MDF BSP/core config binding mismatch")
    family = roots["fsp"].findall("config[@id='config.bsp.ra8p1.fsp']")
    require(len(family) == 1 and family[0].get("path") == "fsp_cfg/bsp/bsp_mcu_family_cfg.h",
            "Missing candidate MDF family configuration")
    return checked


def sources(project, baseline, manifest_path, config):
    manifest = json.loads(regular_file(manifest_path).read_text())
    require(isinstance(manifest, dict), "Invalid manifest object")
    require(manifest.get("candidate") == "custom-5.9-derived-ra8p1", "Not a candidate RA8P1 pack manifest")
    packs = manifest.get("packs")
    changes = manifest.get("baseline_changes")
    require(isinstance(packs, dict) and all(name in packs for name in TARGET_PACKS), "Missing target pack evidence")
    require(isinstance(changes, dict) and changes, "Missing candidate baseline-change evidence")
    expected, required = {}, set()
    for pack_name, pack in packs.items():
        require(isinstance(pack, dict), f"Invalid pack record: {pack_name}")
        entries = pack.get("entries")
        require(isinstance(entries, dict), f"Missing entries in {pack_name}")
        for raw, digest in entries.items():
            name = normalized_source(raw)
            if not name.startswith("ra/"):
                continue
            hash_value(digest)
            if name in expected:
                require(expected[name] == digest, f"Conflicting pack hashes for {name}")
            expected[name] = digest
            if pack_name in TARGET_PACKS:
                required.add(name)
    require(required, "Empty target source inventory")
    # Detect a stale manifest even for a candidate driver not selected by this project.
    changed_sources = set()
    for raw, record in changes.items():
        name = normalized_source(raw)
        if not name.startswith("ra/"):
            continue
        require(isinstance(record, dict), f"Invalid change record: {name}")
        digest = hash_value(record.get("after_sha256"))
        path = within(baseline, name)
        require(sha256(path) == digest, f"Baseline changed since manifest: {name}")
        require(expected.get(name) == digest, f"Candidate change absent/stale in packs: {name}")
        changed_sources.add(name)
        if any(name.startswith(f"ra/fsp/src/{driver}/") or name == f"ra/fsp/inc/instances/{driver}.h"
               for driver in config["drivers"]):
            required.add(name)
    require(changed_sources, "No candidate-owned ra source evidence")
    required.add(CMSIS + "Include/R7KA8P1AD.h")
    for driver in config["drivers"]:
        required.add(f"ra/fsp/src/{driver}/{driver}.c")
    generated = {p.relative_to(project).as_posix(): p for p in (project / "ra").rglob("*") if p.is_file()}
    require(generated, "Missing generated ra/ source tree")
    missing = sorted(required - generated.keys())
    require(not missing, "Missing required generated sources: " + ", ".join(missing))
    checked = []
    for name, path in sorted(generated.items()):
        # Unmodified third-party packs need not be included in the candidate manifest.
        if name not in expected:
            require(not name.startswith(("ra/fsp/", "ra/board/")), f"No pack hash for generated source: {name}")
            continue
        within(project, name)
        digest = sha256(path)
        require(digest == expected[name], f"Stale or modified extraction: {name}")
        original = within(baseline, name)
        require(sha256(original) == digest, f"Baseline/pack disagreement: {name}")
        checked.append({"path": name, "sha256": digest})
    require(changed_sources.intersection(generated), "No candidate-owned generated sources checked")
    mdfs = core_metadata(baseline, packs, config)
    return {"manifest_sha256": sha256(manifest_path), "checked_files": checked, "core_mdfs": mdfs,
            "candidate_files_checked": len(changed_sources.intersection(generated))}


def run(command):
    result = subprocess.run([str(arg) for arg in command], capture_output=True, text=True,
                            timeout=30, env={**os.environ, "LC_ALL": "C"})
    require(result.returncode == 0, f"Command failed: {command[0]}: {result.stderr.strip()[:1000]}")
    require(not result.stderr.strip(), f"Diagnostic from {command[0]}: {result.stderr.strip()[:1000]}")
    require(result.stdout.strip(), f"Empty tool evidence: {command[0]}")
    return result.stdout


def discover_readelf(version, explicit=None):
    if explicit is not None:
        candidates = [Path(explicit)]
    else:
        candidates = []
        on_path = shutil.which("arm-none-eabi-readelf")
        if on_path:
            candidates.append(Path(on_path))
        applications = Path("/Applications")
        for base in applications.glob("*/toolchains"):
            candidates.extend(base.rglob("arm-none-eabi-readelf"))
        arm = applications / "ArmGNUToolchain"
        if arm.is_dir():
            candidates.extend(arm.rglob("arm-none-eabi-readelf"))
    matches = []
    for candidate in sorted(set(path.resolve() for path in candidates)):
        gcc = candidate.with_name("arm-none-eabi-gcc")
        if candidate.is_file() and gcc.is_file():
            actual = run([gcc, "-dumpfullversion"]).strip()
            if actual == version:
                matches.append({"readelf": str(candidate), "gcc": str(gcc), "version": actual})
    require(matches, f"No arm-none-eabi-readelf paired with GCC {version}; use --readelf")
    return matches[0]


def parse_loads(text):
    loads = []
    for line in text.splitlines():
        if not re.match(r"^\s*LOAD(?:\s|$)", line):
            continue
        match = LOAD.fullmatch(line)
        require(match is not None, "Unrecognized PT_LOAD output: " + line)
        offset, virtual, physical, filesz, memsz = (int(match[i], 16) for i in range(1, 6))
        require(filesz <= memsz, "PT_LOAD FileSiz exceeds MemSiz")
        loads.append({"index": len(loads), "offset": offset, "virtual": virtual,
                      "physical": physical, "filesz": filesz, "memsz": memsz,
                      "virtual_end": virtual + memsz, "physical_end": physical + memsz,
                      "file_load_end": physical + filesz,
                      "flags": match[6].replace(" ", ""), "align": int(match[7], 16)})
    require(loads and any(p["memsz"] for p in loads), "No nonempty PT_LOAD evidence")
    return loads


def parse_sections(text):
    sections = []
    for line in text.splitlines():
        if not re.match(r"^\s*\[\s*\d+\]", line):
            continue
        if re.match(r"^\s*\[\s*0\]", line):
            continue
        match = SECTION.fullmatch(line)
        require(match is not None, "Unrecognized section output: " + line)
        sections.append({"name": match[2], "type": match[3], "address": int(match[4], 16),
                         "offset": int(match[5], 16), "size": int(match[6], 16), "flags": match[8].strip()})
    require(sections, "Missing ELF section evidence")
    return sections


def overlap(first, second):
    return first[0] < second[1] and second[0] < first[1]


def core_symbol(text, expected=None):
    rows = [line for line in text.splitlines() if re.search(r"\b__ra8p1_core(?:\s|$)", line)]
    require(len(rows) == 1, "Expected exactly one ELF __ra8p1_core symbol")
    match = re.fullmatch(r"\s*\d+:\s+([0-9a-fA-F]+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+__ra8p1_core\s*", rows[0])
    require(match is not None, "Malformed ELF __ra8p1_core symbol")
    value = int(match[1], 16)
    require(match[2] == "0" and match[3] == "NOTYPE" and match[4] == "GLOBAL" and match[6] == "ABS",
            "ELF __ra8p1_core must be a global absolute assembler symbol")
    require(value in PARTITIONS, "ELF __ra8p1_core must be 0 or 1")
    require(expected is None or value == expected,
            f"ELF __ra8p1_core is {value}, expected {expected}")
    return {"name": "__ra8p1_core", "value": value, "binding": match[4], "index": match[6]}


def allowed(start, size, regions=REGIONS):
    return any(lo <= start and start + size <= hi for lo, hi in regions)


def ranges(loads):
    # Conservatively reserve MemSiz at both VMA and physical addresses, not only
    # file-backed bytes. Zero-file-size BSS must still not collide with another core.
    return sorted({(p[k], p[k] + p["memsz"]) for p in loads if p["memsz"] for k in ("virtual", "physical")})


def validate_loads(loads, file_size, reserved=(), core=None):
    for p in loads:
        if p["filesz"]:
            require(p["offset"] + p["filesz"] <= file_size, "PT_LOAD extends beyond ELF file")
        if not p["memsz"]:
            continue
        for key in ("virtual", "physical"):
            require(allowed(p[key], p["memsz"]), f"PT_LOAD {p['index']} {key} range outside internal memory")
            if core is not None:
                require(allowed(p[key], p["memsz"], PARTITIONS[core]),
                        f"PT_LOAD {p['index']} {key} range outside CPU{core} partition")
        for other in reserved:
            require(not any(overlap(current, other) for current in ranges([p])),
                    f"PT_LOAD {p['index']} overlaps reserved/other-core range {other}")
    for index, p in enumerate(loads):
        for other in loads[index + 1:]:
            require(not any(overlap(a, b) for a in ranges([p]) for b in ranges([other])),
                    "Overlapping PT_LOAD segments within image")


def elf_evidence(path, tool, evidence=None, expected_core=None):
    if evidence is None:
        evidence = {}
    regular_file(path)
    evidence["sha256"] = sha256(path)
    header = run([tool["readelf"], "-hW", path])
    require(re.search(r"Class:\s+ELF32\b", header) and re.search(r"Machine:\s+ARM\b", header)
            and re.search(r"Type:\s+EXEC\b", header), "Expected an ELF32 ARM executable")
    loads = parse_loads(run([tool["readelf"], "-lW", path]))
    evidence["loads"] = loads
    sections = parse_sections(run([tool["readelf"], "-SW", path]))
    allocated = [s for s in sections if s["size"] and "A" in s["flags"]]
    evidence["allocated_sections"] = allocated
    comment = run([tool["readelf"], "-p", ".comment", path])
    versions = set()
    for line in comment.splitlines():
        if "GCC:" in line:
            found = re.findall(r"\b(\d+\.\d+\.\d+)(?=\s|$)", line)
            if found:
                versions.add(found[-1])
    evidence["compiler_versions"] = sorted(versions)
    require(versions == {tool["version"]}, f"Missing/mismatched ELF compiler evidence: {sorted(versions)}")
    symbol = core_symbol(run([tool["readelf"], "-sW", path]), expected_core)
    evidence["core_symbol"] = symbol
    regions = PARTITIONS[symbol["value"]]
    evidence["partitions"] = regions
    validate_loads(loads, path.stat().st_size, core=symbol["value"])
    require(allocated, "No allocated section evidence")
    for section in allocated:
        require(not FORBIDDEN.match(section["name"]), f"Forbidden option/security section: {section['name']}")
        require(allowed(section["address"], section["size"]), f"Section outside internal memory: {section['name']}")
        require(allowed(section["address"], section["size"], regions),
                f"Section outside CPU{symbol['value']} partition: {section['name']}")
        require(any(p["virtual"] <= section["address"] and
                    section["address"] + section["size"] <= p["virtual"] + p["memsz"] for p in loads),
                f"Allocated section is not covered by PT_LOAD: {section['name']}")
    require(sha256(path) == evidence["sha256"], f"ELF changed during inspection: {path}")
    return evidence


def freshness(project, elf, manifest, not_before=None):
    inputs = [regular_file(project / "configuration.xml"), regular_file(project / ".cproject"),
              regular_file(manifest)]
    for name in ("ra", "ra_cfg", "ra_gen", "script", "src"):
        directory = project / name
        require(directory.is_dir() and not directory.is_symlink(), f"Missing input directory: {directory}")
        found = sorted(p for p in directory.rglob("*") if p.is_file())
        require(found, f"No input files: {directory}")
        inputs.extend(within(project, p.relative_to(project)) for p in found)
    for name in ("ra_gen/hal_data.c", "ra_gen/pin_data.c", "ra_gen/vector_data.c"):
        within(project, name)
    if not_before:
        inputs.append(regular_file(not_before))
    now = time.time_ns()
    stamp = elf.stat().st_mtime_ns
    require(0 < stamp <= now, "Invalid/future ELF timestamp")
    for path in inputs:
        value = path.stat().st_mtime_ns
        require(0 < value <= now, f"Invalid/future input timestamp: {path}")
        require(value <= stamp, f"Stale ELF: newer input {path}")
    newest = max(inputs, key=lambda path: path.stat().st_mtime_ns)
    return {"kind": "local_hash_and_mtime", "input_count": len(inputs), "elf_mtime_ns": stamp,
            "newest_input": str(newest), "newest_input_mtime_ns": newest.stat().st_mtime_ns,
            "limitation": "Not a signed build attestation; timestamps do not prove object provenance or runtime behavior"}


def parse_reserved(value):
    try:
        start, end = (int(v, 0) for v in value.split(":"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("Expected START:END (exclusive end, decimal or 0x hex)") from error
    if not (0 <= start < end <= 0x100000000):
        raise argparse.ArgumentTypeError("Invalid reservation interval")
    return start, end


def verify(args):
    report = {"success": False, "runtime_verified": False, "checks": {}, "errors": []}
    stage = "inputs"
    try:
        project = args.project.resolve()
        elf = regular_file(args.elf)
        initial_hash = sha256(elf)
        report["artifact"] = {"path": str(elf.resolve()), "sha256": initial_hash}
        stage = "configuration"
        config = configuration(project, args.core)
        report["checks"][stage] = config
        stage = "generated_core"
        report["checks"][stage] = generated_core(project, config["bsp_core"])
        stage = "sources"
        source_evidence = sources(project, args.baseline.resolve(), args.manifest, config)
        checked = source_evidence["checked_files"]
        source_evidence["checked_file_count"] = len(checked)
        source_evidence["checked_set_sha256"] = hashlib.sha256(
            json.dumps(checked, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if not getattr(args, "details", False):
            del source_evidence["checked_files"]
        report["checks"][stage] = source_evidence
        stage = "toolchain"
        tool = discover_readelf(config["toolchain_version"], args.readelf)
        report["checks"][stage] = tool
        stage = "elf"
        report["checks"][stage] = {}
        evidence = elf_evidence(elf, tool, report["checks"][stage], config["bsp_core"])
        stage = "overlap"
        reserved = list(RESERVED) + list(args.reserve)
        others = []
        for path in args.other_elf:
            other = elf_evidence(path, tool)
            require(other["sha256"] == sha256(path), f"Other ELF changed during verification: {path}")
            reserved.extend(ranges(other["loads"]))
            others.append({"path": str(path), "sha256": other["sha256"],
                           "core": other["core_symbol"]["value"], "ranges": ranges(other["loads"])})
        validate_loads(evidence["loads"], elf.stat().st_size, reserved)
        report["checks"][stage] = {"reserved_ranges": reserved, "other_images": others}
        stage = "freshness"
        report["checks"][stage] = freshness(project, elf, args.manifest, args.not_before)
        require(sha256(elf) == initial_hash == evidence["sha256"], "ELF changed during verification")
        report["success"] = True
    except (VerificationError, OSError, UnicodeError, ET.ParseError, json.JSONDecodeError, subprocess.TimeoutExpired) as error:
        report["errors"].append({"stage": stage, "message": str(error)})
    return report


def main(argv=None):
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--elf", type=Path, required=True)
    parser.add_argument("--core", choices=CORES, required=True)
    parser.add_argument("--baseline", type=Path, default=root / "baseline")
    parser.add_argument("--manifest", type=Path, default=root / "validation/packs/manifest.json")
    parser.add_argument("--readelf", type=Path, help="Override discovery; paired GCC version is still checked")
    parser.add_argument("--other-elf", type=Path, action="append", default=[], help="Reject overlap with this other core image")
    parser.add_argument("--reserve", type=parse_reserved, action="append", default=[], metavar="START:END",
                        help="Reject overlap with a scratch/reserved interval (exclusive end); repeatable")
    parser.add_argument("--not-before", type=Path, help="Optional caller build-start marker; ELF must be at least this new")
    parser.add_argument("--output", type=Path, help="Also create a JSON report file; refuses to overwrite existing files")
    parser.add_argument("--details", action="store_true", help="Include per-source-file hashes in the JSON report")
    parser.add_argument("--pretty", action="store_true", help="Indent JSON; default is one compact line")
    args = parser.parse_args(argv)
    report = verify(args)
    text = json.dumps(report, indent=2 if args.pretty else None, sort_keys=True)
    if args.output:
        try:
            with args.output.open("x") as stream:
                stream.write(text + "\n")
        except OSError as error:
            report["success"] = False
            report["errors"].append({"stage": "report", "message": str(error)})
            text = json.dumps(report, indent=2 if args.pretty else None, sort_keys=True)
    print(text)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
