#!/usr/bin/env python3
"""Compare preserved and candidate FSP 5.9 builds; never qualify runtime behavior.

--write-specs extracts complete stack properties from the preserved models.
Normal mode reads evidence only and writes a new JSON report (exclusive create).
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import io
import json
from pathlib import Path
import re
import shlex
import struct
import subprocess
import xml.etree.ElementTree as ET
import zipfile

BOARDS = {"ra8m1": ("EK-RA8M1", "CM85"), "ra6t2": ("MCK-RA6T2", "CM33"),
          "ra6m5": ("EK-RA6M5", "CM33")}
CMSIS = "ra/fsp/src/bsp/cmsis/Device/RENESAS/"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_xml(path):
    data = path.read_bytes()
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("XML entities forbidden: " + str(path))
    return ET.fromstring(data)


def properties(node):
    pairs = [(item.attrib["id"], item.attrib["value"]) for item in node.findall("property")]
    if len(dict(pairs)) != len(pairs):
        raise ValueError("Duplicate property")
    return dict(pairs)


def extract_spec(project):
    root = read_xml(project / "configuration.xml")
    modules = [{"id": re.sub(r"\.\d+$", "", node.attrib["id"]), "properties": properties(node)}
               for node in root.findall("./raModuleConfiguration/module")]
    return {"description": "Select the preserved named default stacks. Some serialized defaults (empty masks/display labels) are not legal setter inputs. Every preserved property below, plus BSP/clocks/pins, must match the generated candidate. No runtime qualification.",
            "modules": [{"id": module["id"], "properties": {
                key: value for key, value in module["properties"].items() if key.endswith(".name")}}
                        for module in modules],
            "preserved_modules": modules}


def canonical(node):
    return [node.tag, sorted(node.attrib.items()), (node.text or "").strip(),
            sorted([canonical(child) for child in node], key=lambda value: json.dumps(value, sort_keys=True))]


def configuration(project):
    root = read_xml(project / "configuration.xml")
    modules = [{"id": re.sub(r"\.\d+$", "", node.attrib["id"]), "properties": properties(node)}
               for node in root.findall("./raModuleConfiguration/module")]
    return {
        "general": {item.attrib["key"]: item.attrib["value"] for item in root.findall("./generalSettings/option")},
        "bsp": {node.attrib["id"]: properties(node) for node in root.findall("./raBspConfiguration/config")},
        "clocks": canonical(root.find("raClockConfiguration")),
        "components": sorted([dict(node.attrib, pack=node.findtext("originalPack"))
                              for node in root.findall("./raComponentSelection/component")],
                             key=lambda value: json.dumps(value, sort_keys=True)),
        "modules": sorted(modules, key=lambda value: json.dumps(value, sort_keys=True)),
        "driver_config": {node.attrib["id"]: properties(node) for node in root.findall("./raModuleConfiguration/config")},
        "pins": canonical(root.find("raPinConfiguration")),
        "elc": canonical(root.find("raElcConfiguration")),
        "icu": canonical(root.find("raIcuConfiguration")),
    }


def compare_values(left, right):
    return {"equal": left == right, "preserved_sha256": digest(json.dumps(left, sort_keys=True).encode()),
            "candidate_sha256": digest(json.dumps(right, sort_keys=True).encode()),
            **({} if left == right else {"preserved": left, "candidate": right})}


def normalize(text, project):
    # Names/paths differ by construction, not by target configuration.
    text = text.replace(str(project), "<PROJECT>").replace(project.name, "<PROJECT_NAME>")
    return text


def source_name(name):
    return CMSIS + name[5:] if name.startswith("ra~1/") else name


def elf_sections(path):
    data = path.read_bytes()
    if data[:6] != b"\x7fELF\x01\x01" or struct.unpack_from("<H", data, 18)[0] != 40:
        raise ValueError("Expected little-endian ELF32 ARM: " + str(path))
    header = struct.unpack_from("<16sHHIIIIIHHHHHH", data)
    offset, stride, count, string_index = header[6], header[11], header[12], header[13]
    headers = [struct.unpack_from("<IIIIIIIIII", data, offset + stride * i) for i in range(count)]
    strings_header = headers[string_index]
    strings = data[strings_header[4]:strings_header[4] + strings_header[5]]
    result = {}
    for item in headers[1:]:
        name = strings[item[0]:].split(b"\0", 1)[0].decode()
        if item[2] & 2:  # SHF_ALLOC, includes NOBITS so RAM allocation is compared.
            payload = b"" if item[1] == 8 else data[item[4]:item[4] + item[5]]
            result[name] = {"address": item[3], "size": item[5], "flags": item[2],
                            "type": item[1], "sha256": digest(payload), "bytes": payload.hex()}
    return result


def map_regions(text):
    match = re.search(r"Memory Configuration\s*\n(.*?)\nLinker script and memory map", text, re.S)
    if not match:
        raise ValueError("Missing map memory configuration")
    return [line.split() for line in match[1].splitlines() if line.strip()]


def inspect_log(text, project_name):
    # The host emits an unrelated unused LLVM manifest diagnostic even for GNU builds.
    failures = [line for line in text.splitlines() if not line.startswith(
        "Managed Build system manifest file error: Unable to resolve the category identifier com.renesas.cdt.managedbuild.llvm.") and re.search(
        r"(?<!\w)error:|fatal error|Traceback|Java Stacktrace:|CoreException:|No rule to make target|BUILD FAILED|[1-9][0-9]* errors", line)]
    summaries = re.findall(r"Build Finished\. (\d+) errors, (\d+) warnings", text)
    target = "Building target: " + project_name + ".elf"
    full_build = target in text and bool(re.search(
        r"Build Finished\. 0 errors, \d+ warnings", text.split(target)[-1]))
    return {"summaries": summaries, "failure_lines": failures, "full_build": full_build,
            "verified": full_build and not failures and all(errors == "0" for errors, warnings in summaries)}


def build_evidence(project, log_paths, expected_core):
    logs = []
    for path in log_paths:
        logs.append({"path": str(path), "sha256": digest(path.read_bytes()),
                     **inspect_log(path.read_text(), project.name)})
    commands = [item for item in json.loads((project / "Debug/compile_commands.json").read_text())
                if Path(item["file"]).suffix.lower() in (".c", ".s", ".cpp")]
    compiler = Path(shlex.split(commands[0]["command"])[0])
    version = subprocess.run([str(compiler), "--version"], check=True, capture_output=True, text=True).stdout.splitlines()[0]
    architecture = "cortex-m85" if expected_core == "CM85" else "cortex-m33"
    correct_commands = bool(commands) and all("-mcpu=" + architecture in item["command"] for item in commands)
    elf = project / "Debug" / (project.name + ".elf")
    inputs = [project / "configuration.xml", project / ".cproject"]
    inputs += [p for folder in ("ra", "ra_cfg", "ra_gen", "src", "script")
               for p in (project / folder).rglob("*") if p.is_file()]
    newer = [str(p.relative_to(project)) for p in inputs if p.stat().st_mtime_ns > elf.stat().st_mtime_ns]
    compiled = sorted({Path(item["file"]).resolve().relative_to(project.resolve()).as_posix()
                       for item in commands})
    nm = compiler.with_name("arm-none-eabi-nm")
    symbols = subprocess.run([str(nm), "-S", "--defined-only", str(elf)], check=True,
                             capture_output=True, text=True).stdout
    functions = [line.split()[-1] for line in symbols.splitlines()
                 if re.match(r"^[0-9a-f]+ [0-9a-f]+ [Tt] ", line)]
    return {"logs": logs, "verified": any(log["verified"] for log in logs) and
            not any(log["failure_lines"] for log in logs) and correct_commands and not newer and "13.2.1" in version,
            "compiler": str(compiler), "compiler_version": version, "correct_cpu_flags": correct_commands,
            "compile_units": compiled,
            "normalized_compile_commands": sorted(normalize(item["command"], project) for item in commands),
            "retained_functions": sorted(functions),
            "elf": str(elf), "elf_sha256": digest(elf.read_bytes()),
            "elf_mtime_ns": elf.stat().st_mtime_ns, "newer_inputs": newer}


def pack_sources(outer, pack_name, candidate_dir=None, manifest=None):
    if candidate_dir is not None and pack_name in manifest["packs"]:
        record = manifest["packs"][pack_name]
        data = (candidate_dir / pack_name).read_bytes()
        if digest(data) != record["sha256"]:
            raise ValueError("Candidate pack hash mismatch: " + pack_name)
        expected = record["entries"]
    else:
        names = [name for name in outer.namelist() if name.rsplit("/", 1)[-1] == pack_name]
        if len(names) != 1:
            raise ValueError("Original pack is not unique: " + pack_name)
        data = outer.read(names[0])
        expected = None
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        values = {}
        for name in archive.namelist():
            relative = source_name(name)
            if not relative.startswith("ra/") or name.endswith("/"):
                continue
            value = digest(archive.read(name))
            if expected is not None and expected.get(name) != value:
                raise ValueError("Manifest entry mismatch: " + pack_name + ":" + name)
            values[relative] = value
        return {"sha256": digest(data), "sources": values}


def verify_sources(project, config, packs):
    expected = {}
    selected = sorted({node["pack"] for node in config["components"]})
    for pack in selected:
        for path, value in packs[pack]["sources"].items():
            if path in expected and expected[path] != value:
                raise ValueError("Conflicting source owners: " + path)
            expected[path] = value
    records = {}
    for path in sorted((project / "ra").rglob("*")):
        if path.is_file():
            relative = path.relative_to(project).as_posix()
            value = digest(path.read_bytes())
            records[relative] = {"sha256": value, "expected_sha256": expected.get(relative),
                                 "verified": value == expected.get(relative)}
    return {"verified": bool(records) and all(item["verified"] for item in records.values()),
            "packs": {name: packs[name]["sha256"] for name in selected}, "files": records}


def file_comparisons(left, right, paths):
    comparisons = {}
    for relative in paths:
        a, b = left / relative, right / relative
        if not a.is_file() or not b.is_file():
            comparisons[relative] = {"equal": False, "missing": [str(p) for p in (a, b) if not p.is_file()]}
            continue
        x, y = a.read_text(), b.read_text()
        comparisons[relative] = {"equal": normalize(x, left) == normalize(y, right),
                                 "preserved_sha256": digest(a.read_bytes()), "candidate_sha256": digest(b.read_bytes())}
        if not comparisons[relative]["equal"]:
            comparisons[relative]["diff"] = list(difflib.unified_diff(normalize(x, left).splitlines(),
                normalize(y, right).splitlines(), fromfile="preserved/" + relative, tofile="candidate/" + relative))
    return comparisons


def adc_gain_equivalence(left, right):
    """Recognize only the eight ADC_B gain aliases, then compile value assertions."""
    old = (left / "ra_gen/hal_data.c").read_text()
    new = (right / "ra_gen/hal_data.c").read_text()
    expected = old
    assertions = []
    for index in range(8):
        for suffix, alias in (("UGAINI_Pos", "UGAIN_INTEGER_Pos"), ("UGAINF_Msk", "UGAIN_FRACTIONAL_Msk")):
            before = f"R_ADC_B0_ADUGTR{index}_{suffix}"
            after = f"R_ADC_B0_ADUGTR_{alias}"
            if expected.count(before) != 1:
                return {"verified": False, "reason": "Unexpected ADC gain expression"}
            expected = expected.replace(before, after)
            assertions.append(f'_Static_assert({before} == {after}, "gain{index}_{suffix}");')
    if expected != new:
        return {"verified": False, "reason": "Additional generated differences beyond gain aliases"}
    commands = json.loads((right / "Debug/compile_commands.json").read_text())
    command = next(item for item in commands if item["file"].endswith("/ra_gen/hal_data.c"))
    args = shlex.split(command["command"])
    clean = []
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg == "-o":
            skip = True
        elif arg != "-c" and not arg.endswith(".c"):
            clean.append(arg)
    source = '#include "hal_data.h"\n' + "\n".join(assertions) + "\n"
    result = subprocess.run(clean + ["-fsyntax-only", "-x", "c", "-"], input=source,
                            cwd=right / "Debug", text=True, capture_output=True)
    return {"verified": result.returncode == 0, "command": clean + ["-fsyntax-only", "-x", "c", "-"],
            "source": source, "exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
            "reason": "Only eight gain alias substitutions; compiler checks all 16 position/mask values against original names"}


def compare_board(root, board, manifest, original_packs, candidate_packs,
                  candidate_prefix="candidate_", workspace_prefix="ws-regression-"):
    left, right = [root / "validation/projects" / (prefix + board) for prefix in ("original_", candidate_prefix)]
    lc, rc = configuration(left), configuration(right)
    config = {key: compare_values(lc[key], rc[key]) for key in lc}
    core = BOARDS[board][1]
    original_log = root / "validation" / ("original-" + board + ("-drivers.log" if board == "ra8m1" else "-clean.log"))
    lb = build_evidence(left, [original_log], core)
    rb = build_evidence(right, [right / "drivers-clean.log", right / "drivers-build.log",
        root / "validation" / (workspace_prefix + board + "-configuration.log")], core)
    ls = verify_sources(left, lc, original_packs)
    rs = verify_sources(right, rc, candidate_packs)
    source_changes = {name: {"preserved": ls["files"].get(name), "candidate": rs["files"].get(name)}
                      for name in sorted(ls["files"].keys() | rs["files"].keys())
                      if ls["files"].get(name, {}).get("sha256") != rs["files"].get(name, {}).get("sha256")}
    generated = sorted({p.relative_to(project).as_posix() for project in (left, right)
                        for folder in ("ra_cfg", "ra_gen", "src", "script")
                        for p in (project / folder).rglob("*") if p.is_file()})
    files = file_comparisons(left, right, generated)
    if board == "ra6t2" and not files["ra_gen/hal_data.c"]["equal"]:
        files["ra_gen/hal_data.c"]["value_equivalence"] = adc_gain_equivalence(left, right)
    api_paths = sorted(name for name in ls["files"] if name.startswith("ra/fsp/inc/"))
    api = file_comparisons(left, right, api_paths)
    le = elf_sections(left / "Debug" / (left.name + ".elf"))
    re_ = elf_sections(right / "Debug" / (right.name + ".elf"))
    options = {name: compare_values(le.get(name), re_.get(name)) for name in le.keys() | re_.keys()
               if re.search("option|rom_register|ofs|id_code|fsbl|bps", name, re.I)}
    section_deltas = {name: {"preserved": {k: v for k, v in le.get(name, {}).items() if k != "bytes"},
                             "candidate": {k: v for k, v in re_.get(name, {}).items() if k != "bytes"}}
                      for name in sorted(le.keys() | re_.keys()) if le.get(name) != re_.get(name)}
    lm = (left / "Debug" / (left.name + ".map")).read_text()
    rm = (right / "Debug" / (right.name + ".map")).read_text()
    maps = {"preserved_sha256": digest(lm.encode()), "candidate_sha256": digest(rm.encode()),
            "regions": compare_values(map_regions(lm), map_regions(rm)),
            "normalized_equal": normalize(lm, left) == normalize(rm, right),
            "normalized_diff": list(difflib.unified_diff(normalize(lm, left).splitlines(),
                normalize(rm, right).splitlines(), fromfile="preserved.map", tofile="candidate.map")),
            "allocated_sections_identical": not section_deltas,
            "section_deltas": section_deltas}
    linked_objects = sorted(set(re.findall(r"ra/fsp/src/[^\s()]+\.o", rm)))
    api_delta = [name for name, result in api.items() if not result["equal"]]
    command_comparison = compare_values(lb.pop("normalized_compile_commands"), rb.pop("normalized_compile_commands"))
    attempt = root / "validation" / (workspace_prefix + board + "-configuration-attempt1.log")
    attempts = [{"path": str(attempt), "sha256": digest(attempt.read_bytes()),
                 **inspect_log(attempt.read_text(), right.name)}] if attempt.exists() else []
    compatible = (command_comparison["equal"] and all(result["equal"] for result in config.values()) and lb["verified"] and rb["verified"] and
                  ls["verified"] and rs["verified"] and all(
                      result["equal"] or result.get("value_equivalence", {}).get("verified", False)
                      for result in files.values()) and
                  all(result["equal"] for result in options.values()) and maps["regions"]["equal"] and
                  lb["compile_units"] == rb["compile_units"] and not section_deltas)
    return {"board": BOARDS[board][0], "core": core, "status": "passed_build_comparison" if compatible else "blocked_or_mismatch",
            "configuration": config, "preserved_build": lb, "candidate_build": rb,
            "configuration_xml_sha256": {"preserved": digest((left / "configuration.xml").read_bytes()),
                                          "candidate": digest((right / "configuration.xml").read_bytes())},
            "spec_sha256": digest((root / "tests/ra8p1" / ("regression-" + board + ".json")).read_bytes()),
            "first_configuration_attempt": attempts, "compiler_options": command_comparison,
            "preserved_sources": ls, "candidate_sources": rs, "source_changes": source_changes,
            "generated_files": files, "api_headers": api, "changed_api_headers": api_delta,
            "option_sections": options, "link_map": maps, "map_driver_objects_including_discarded": linked_objects,
            "compiled_units_equal": compare_values(lb["compile_units"], rb["compile_units"]),
            "runtime_verified": False,
            "qualification_limit": "Compile/link and configuration comparison only. Minimal hal_entry does not call the selected drivers; --gc-sections can discard unused APIs (see retained_functions). No older-board hardware supplied; no runtime/peripheral/electrical qualification. API changes are reported, not hidden."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--write-specs", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--candidate-prefix", default="candidate_")
    parser.add_argument("--workspace-prefix", default="ws-regression-")
    parser.add_argument("--expected-manifest-sha256")
    args = parser.parse_args()
    for value in (args.candidate_prefix, args.workspace_prefix):
        if re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
            parser.error("Project and workspace prefixes must be simple names")
    root = args.root.resolve()
    if args.write_specs:
        for board in BOARDS:
            destination = root / "tests/ra8p1" / ("regression-" + board + ".json")
            with destination.open("x") as stream:
                json.dump(extract_spec(root / "validation/projects" / ("original_" + board)), stream, indent=2)
                stream.write("\n")
        return 0
    if args.output is None:
        parser.error("--output is required for comparison")
    manifest_path = root / "validation/packs/manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest_sha256 = digest(manifest_bytes)
    if args.expected_manifest_sha256 and manifest_sha256 != args.expected_manifest_sha256:
        raise ValueError("Current manifest differs from the requested frozen candidate")
    manifest = json.loads(manifest_bytes)
    archive = root / "reference/vendor/FSP_Packs_v5.9.0.zip"
    if digest(archive.read_bytes()) != manifest["source_archive_sha256"]:
        raise ValueError("Preserved archive hash mismatch")
    selected = set()
    for board in BOARDS:
        selected.update(item["pack"] for item in configuration(root / "validation/projects" / ("original_" + board))["components"])
        selected.update(item["pack"] for item in configuration(root / "validation/projects" / (args.candidate_prefix + board))["components"])
    with zipfile.ZipFile(archive) as outer:
        original = {name: pack_sources(outer, name) for name in selected}
        candidate = {name: pack_sources(outer, name, manifest_path.parent, manifest) for name in selected}
    boards = {board: compare_board(root, board, manifest, original, candidate,
                                   args.candidate_prefix, args.workspace_prefix) for board in BOARDS}
    if digest(manifest_path.read_bytes()) != manifest_sha256:
        raise ValueError("Candidate manifest changed during comparison")
    result = {"schema_version": 1, "candidate": manifest["candidate"], "manifest_sha256": manifest_sha256,
              "candidate_project_prefix": args.candidate_prefix, "workspace_prefix": args.workspace_prefix,
              "comparison_utility_sha256": digest(Path(__file__).read_bytes()),
              "generation_helper_sha256": digest((root / "tools/generate_fsp_validation.py").read_bytes()),
              "configuration_helper_sha256": digest((root / "tools/configure_fsp_validation.py").read_bytes()),
              "baseline_commit": manifest["baseline_commit"], "source_archive_sha256": manifest["source_archive_sha256"],
              "runtime_verified": False, "hardware_accessed": False,
              "status": "passed_build_comparison" if all(item["status"] == "passed_build_comparison" for item in boards.values()) else "blocked_or_mismatch",
              "boards": boards}
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({"status": result["status"], "boards": {key: value["status"] for key, value in boards.items()},
                      "report": str(args.output), "runtime_verified": False}, indent=2))
    return 0 if result["status"] == "passed_build_comparison" else 1


if __name__ == "__main__":
    raise SystemExit(main())
