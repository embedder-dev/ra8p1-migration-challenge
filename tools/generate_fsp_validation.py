"""EASE: NAME ABSOLUTE_LOCATION BOARD CORE TOOLCHAIN_VERSION [options].

Target facts come from the selected pack and optional --expected-settings JSON.
No board, part number, memory map or candidate fingerprint is embedded here.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent))
from fsp_host import build_project, generate_content, prepare_project, require_ok

loadModule("/FSP/ProjectGen")
loadModule("/FSP/Configuration")
loadModule("/FSP/Build")

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("name")
parser.add_argument("location", type=Path)
parser.add_argument("board")
parser.add_argument("core")
parser.add_argument("toolchain_version")
parser.add_argument("--fsp-version", default="5.9.0")
parser.add_argument("--expected-settings", type=Path)
parser.add_argument("--debugger-base")
parser.add_argument("--manifest", type=Path)
parser.add_argument("--previous-manifest", type=Path, action="append", default=[])
args = parser.parse_args(list(sys.argv[1:]))
if not args.location.is_absolute():
    raise ValueError("Project location must be absolute")
if Path(args.name).name != args.name or "\\" in args.name or ":" in args.name:
    raise ValueError("Project name must be a basename")
expected = json.loads(args.expected_settings.read_text()) if args.expected_settings else {}
if "#FSPVersion#" in expected and expected["#FSPVersion#"] != args.fsp_version:
    raise ValueError("Conflicting expected FSP version")
expected["#FSPVersion#"] = args.fsp_version
generation = projectGenerationFor("ra", args.fsp_version)
generation.projectName(args.name)
generation.projectLocation(str(args.location))
generation.board(args.board)
if args.core not in list(generation.getAvailableCores()):
    raise ValueError("Requested core is unavailable: " + args.core)
generation.core(args.core)
generation.rtos("No RTOS")
generation.projectType("FLAT")
generation.template("Bare Metal - Minimal")
generation.toolchain("GNU ARM Embedded", args.toolchain_version)
generation.debugger("com.renesas.hardwaredebug.rz.jlink")
require_ok(generation.validate(), "validate")
require_ok(generation.generate(None), "generate")
prepare_project(globals(), args.name, args.location, expected, args.debugger_base, args.core)
generate_content(globals(), args.name, args.location, args.manifest, args.previous_manifest)
build_project(globals(), args.name, args.location, [
    ("FULL_BUILD", "full-build.log"), ("CLEAN_BUILD", "clean-build.log"),
    ("FULL_BUILD", "rebuild.log")])
