"""EASE: NAME ABSOLUTE_LOCATION [MODULE_ID ... | --spec ABSOLUTE_JSON]."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent))
from fsp_host import (build_project, configure_pins, generate_content, prepare_project,
                      select_stack, set_properties)
from fsp_manifest import safe_path

loadModule("/FSP/Configuration")
loadModule("/FSP/Build")
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("name")
parser.add_argument("location", type=Path)
parser.add_argument("modules", nargs="*")
parser.add_argument("--spec", type=Path)
args = parser.parse_args(list(sys.argv[1:]))
if not args.location.is_absolute() or (args.spec and not args.spec.is_absolute()):
    raise ValueError("Project/spec paths must be absolute")
if args.spec and args.modules:
    raise ValueError("Use module IDs or --spec, not both")
spec = json.loads(args.spec.read_text()) if args.spec else {"modules": [{"id": x} for x in args.modules]}
if "source" in spec and not args.spec:
    raise ValueError("Application source requires --spec")


def spec_path(value):
    path = Path(value)
    return path if path.is_absolute() else args.spec.parent / path


manifest = spec_path(spec["candidate_manifest"]) if spec.get("candidate_manifest") else None
previous = [spec_path(value) for value in spec.get("previous_manifests", [])]
# Persist debugger settings before opening the cached configuration model.
prepare_project(globals(), args.name, args.location, spec.get("expected_settings"), spec.get("debugger_base"))
configuration = openDDSCConfiguration(args.name)
threads = [thread for thread in configuration.getThreads() if thread.isHal()]
if len(threads) != 1:
    raise RuntimeError("Expected exactly one HAL/common thread")
set_properties(configuration.getBSP(), spec.get("bsp", {}))
for entry in spec.get("modules", []):
    properties = entry.get("properties", {})
    stack = select_stack(threads[0], entry["id"], properties)
    set_properties(stack, properties)
configure_pins(configuration, args.location, spec.get("pins", []))
configuration.save()
generate_content(globals(), args.name, args.location, manifest, previous)
for problem in configuration.getProblems():
    print("CONFIGURATION_PROBLEM " + str(problem))
if "source" in spec:
    source = spec_path(spec["source"])
    if not source.is_file() or source.suffix != ".c":
        raise ValueError("Missing C application fixture: " + str(source))
    safe_path(args.location, "src/hal_entry.c").write_bytes(source.read_bytes())
build_project(globals(), args.name, args.location, [
    ("CLEAN_BUILD", "drivers-clean.log"), ("FULL_BUILD", "drivers-build.log")])
