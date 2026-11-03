"""Run through e2 studio's EASE Py4J engine, not standalone Python.

argv: this_script, FSP version, optional board name.
Only discovers choices; it does not create projects or change installed packs.
"""
import json
import sys

loadModule("/FSP/ProjectGen")
version = sys.argv[1] if len(sys.argv) > 1 else "5.9.0"
generation = projectGenerationFor("ra", version)
print("FSP_DISCOVERY_BEGIN " + version)
print("BOARDS " + json.dumps(list(generation.getAvailableBoards())))
print("TOOLCHAINS " + json.dumps(list(getAvailableToolchains(True))))
print("GNU_VERSIONS " + json.dumps(list(getAvailableToolchainVersions("GNU ARM Embedded", True))))
if len(sys.argv) > 2:
    generation.board(sys.argv[2])
    print("DEVICES " + json.dumps(list(generation.getAvailableDevices())))
    print("SELECTED_DEVICE " + str(generation.device()))
    print("CORES " + json.dumps(list(generation.getAvailableCores())))
    generation.toolchain("GNU ARM Embedded", list(getAvailableToolchainVersions("GNU ARM Embedded", True))[0])
    print("DEBUGGERS " + json.dumps(list(generation.getAvailableDebuggers("GNU ARM Embedded"))))
    print("GENERATION_METHODS " + json.dumps(sorted(set(str(m.getName()) for m in generation.getClass().getMethods()))))
    print("TEMPLATES " + json.dumps(list(generation.getAvailableTemplateNames())))
    print("RTOS " + json.dumps(list(generation.getAvailableRtosNames())))
    print("PROJECT_TYPES " + json.dumps(list(generation.getProjectTypes())))
print("FSP_DISCOVERY_END")
