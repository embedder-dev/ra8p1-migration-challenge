"""Add requested driver stacks through EASE, regenerate and build a test project.

Arguments: PROJECT_NAME ABSOLUTE_PROJECT_LOCATION MODULE_ID [...], or
PROJECT_NAME ABSOLUTE_PROJECT_LOCATION --spec ABSOLUTE_JSON_SPEC.
Use an isolated workspace in which the project is already registered. A JSON
spec selects stacks, properties, pins and an application fixture. All model
changes use configurator APIs, never hand-authored application XML.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import time
import xml.etree.ElementTree as ET
import zipfile

loadModule("/FSP/Configuration")
loadModule("/FSP/Build")

name, location = sys.argv[1:3]
if not os.path.isabs(location):
    raise ValueError("Project location must be absolute")
if len(sys.argv) > 3 and str(sys.argv[3]) == "--spec":
    if len(sys.argv) != 5 or not os.path.isabs(sys.argv[4]):
        raise ValueError("--spec requires one absolute JSON path")
    spec_path = Path(sys.argv[4])
    spec = json.loads(spec_path.read_text())
else:
    spec_path = None
    spec = {"modules": [{"id": value} for value in sys.argv[3:]]}


def correct_ra8p1_debugger(name, location, expected_core=None, required=False):
    workspace_project = org.eclipse.core.resources.ResourcesPlugin.getWorkspace().getRoot().getProject(name)
    if not workspace_project.exists():
        raise RuntimeError("Project is not registered: " + name)
    project = org.eclipse.core.runtime.Platform.getAdapterManager().loadAdapter(
        workspace_project, "com.renesas.cdt.ddsc.project.IDDSCProject")
    if project is None:
        raise RuntimeError("Project is not an FSP project: " + name)
    loader = com.renesas.cdt.ddsc.contentgen.internal.config.DDSCProjectConfigurationLoader(project)
    try:
        loader.init(None)
        if not loader.isInitialised():
            raise RuntimeError("Configuration model did not initialize")
        settings = loader.getConfigurationSettings()
        part = str(settings.getSettingString("#TargetName#"))
        board_id = str(settings.getSettingString("#Board#"))
        cpu = str(settings.getSettingString("CPU"))
        if not (required or part.startswith("R7KA8P1") or "ra8p1" in board_id.lower() or cpu == "RA8P1"):
            return
        if part != "R7KA8P1KFLCAC":
            raise RuntimeError("Unexpected RA8P1 physical part: " + part)
        core = str(settings.getSettingString("Core"))
        architectures = {"CM85": "cortex-m85", "CM33": "cortex-m33"}
        if core not in architectures or (expected_core is not None and core != expected_core):
            raise RuntimeError("Unexpected RA8P1 selected core: " + core)
        expected = {"#TargetName#": part, "Core": core,
                    "#TargetARCHITECTURE#": architectures[core], "#FSPVersion#": "5.9.0"}
        for key, value in expected.items():
            if str(settings.getSettingString(key)) != value:
                raise RuntimeError("RA8P1 setting mismatch: " + key)
        bundle = org.eclipse.core.runtime.Platform.getBundle("com.renesas.cdt.ra.contentgen")
        if bundle is None:
            raise RuntimeError("RA content-generation bundle is unavailable")
        contributor = bundle.loadClass(
            "com.renesas.cdt.ra.ddsc.contentgen.idesettings.RaIdeSettingsContributor").newInstance()
        resolver = contributor.getDeviceFamilyIdeSettings(project, None, False)
        target = str(resolver.getMultiCoreDebugDeviceName("R7KA8P1KF", core))
        expected["#DeviceCommand#"] = "R7KA8P1KF_CPU" + ("0" if core == "CM85" else "1")
        if target != expected["#DeviceCommand#"]:
            raise RuntimeError("RA8P1 debugger resolver mismatch: " + target)
        settings.putSetting("#DeviceCommand#", target)
        if str(settings.getSettingString("#DeviceCommand#")) != target:
            raise RuntimeError("RA8P1 debugger setting was not accepted")
        loader.save()
    finally:
        loader.dispose()

    # Read back the serialized model before any other helper can cache its settings.
    saved = ET.parse(Path(location) / "configuration.xml").getroot()
    for key, value in expected.items():
        values = [node.get("value") for node in saved.findall("./generalSettings/option")
                  if node.get("key") == key]
        if values != [value]:
            raise RuntimeError("RA8P1 persisted setting mismatch: " + key)
    print("RESOLVED_DEBUGGER " + core + " " + target)
    return project


def generate_candidate_content(project, name, location):
    """Refresh only hash-proven candidate sources, then let FSP extract them."""
    if project is None:
        generateProjectContent(name, None)
        return

    # Same-version extraction uses OVERWRITE_NONE in PackIOExtraction. The
    # builder's boolean overloads do not force source replacement. This one
    # historical candidate fingerprint predates BSP_PRV_VECT_ENUM; never infer
    # ownership from a filename, timestamp, generated banner, or CRC alone.
    historical = {
        "ra/fsp/src/bsp/mcu/ra8p1/bsp_elc.h": {
            "55d21b7f4d13006d3ceee452030f27ba011de9dc910ecf484fc5e2d242ae1a41"},
    }
    root = Path(location)
    candidate = Path(sys.argv[0]).resolve().parent.parent / "validation" / "packs"

    def safe_path(base, relative):
        parsed = PurePosixPath(relative)
        if (not relative or parsed.is_absolute() or ".." in parsed.parts or
                "\\" in relative or ":" in relative or str(parsed) != relative):
            raise ValueError("Unsafe candidate source path: " + relative)
        path = base
        for part in parsed.parts:
            path = path / part
            if path.is_symlink():
                raise ValueError("Refusing symlinked candidate path: " + str(path))
        if base.is_symlink() or base.resolve() not in path.resolve().parents:
            raise ValueError("Candidate path escapes root: " + str(path))
        return path

    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def hash_value(value):
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("Invalid candidate SHA256")
        return value

    manifest_path = safe_path(candidate, "manifest.json")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("candidate") != "custom-5.9-derived-ra8p1":
        raise ValueError("Not an RA8P1 candidate manifest")
    saved = ET.parse(root / "configuration.xml").getroot()
    components = saved.findall("./raComponentSelection/component")
    selected = {}
    for component in components:
        pack = component.findtext("originalPack")
        if pack in manifest["packs"]:
            selected.setdefault(pack, []).append(component)
    if "Renesas.RA_mcu_ra8p1.5.9.0.pack" not in selected:
        raise ValueError("Candidate MCU pack is not selected")
    manager = com.renesas.cdt.ddsc.packs.io.PackIOManagerFactory.getInstance().getPackManager("ra")
    installed = Path(str(manager.getPacksFolder().toOSString()))
    expected = {}
    for pack_name, selections in sorted(selected.items()):
        if not pack_name.endswith("5.9.0.pack"):
            raise ValueError("Not a candidate 5.9 pack: " + pack_name)
        record = manifest["packs"][pack_name]
        archive_path = safe_path(candidate, pack_name)
        installed_path = safe_path(installed, pack_name)
        pack_hash = hash_value(record["sha256"])
        if digest(archive_path) != pack_hash or digest(installed_path) != pack_hash:
            raise ValueError("Installed candidate hash mismatch: " + pack_name)
        with zipfile.ZipFile(archive_path) as archive:
            pdsc_names = [item.filename for item in archive.infolist() if item.filename.endswith(".pdsc")]
            if len(pdsc_names) != 1:
                raise ValueError("Expected one candidate PDSC: " + pack_name)
            pdsc_name = pdsc_names[0]
            pdsc_data = archive.read(pdsc_name)
            if hashlib.sha256(pdsc_data).hexdigest() != hash_value(record["entries"][pdsc_name]):
                raise ValueError("Candidate PDSC hash mismatch: " + pack_name)
            if b"<!DOCTYPE" in pdsc_data.upper() or b"<!ENTITY" in pdsc_data.upper():
                raise ValueError("Candidate PDSC entities are forbidden")
            pdsc = ET.fromstring(pdsc_data)
            for selection in selections:
                attributes = (("class", "Cclass"), ("group", "Cgroup"),
                              ("subgroup", "Csub"), ("variant", "Cvariant"), ("version", "Cversion"))
                matches = [item for item in pdsc.findall(".//component") if all(
                    item.get(pack_key, "") == selection.get(config_key, "")
                    for config_key, pack_key in attributes)]
                if len(matches) != 1:
                    raise ValueError("Candidate component is not unique: " + str(selection.attrib))
                for entry in matches[0].findall("./files/file"):
                    source = entry.get("name", "")
                    if entry.get("attr") == "template" or source.endswith("/"):
                        continue
                    if source.startswith("ra~1/"):
                        relative = "ra/fsp/src/bsp/cmsis/Device/RENESAS/" + source[5:]
                    elif source.startswith("ra/"):
                        relative = source
                    else:
                        continue
                    safe_path(root, relative)
                    info = archive.getinfo(source)
                    if stat.S_IFMT(info.external_attr >> 16) not in (0, stat.S_IFREG):
                        raise ValueError("Not a regular candidate source: " + source)
                    source_hash = hash_value(record["entries"][source])
                    if hashlib.sha256(archive.read(info)).hexdigest() != source_hash:
                        raise ValueError("Candidate source hash mismatch: " + source)
                    if relative in expected and expected[relative] != source_hash:
                        raise ValueError("Conflicting selected source owners: " + relative)
                    expected[relative] = source_hash

    # Validate the entire change set before removing anything. Unknown bytes
    # remain untouched, and no build may run against a partially refreshed tree.
    changes = []
    for relative, source_hash in sorted(expected.items()):
        path = safe_path(root, relative)
        if not path.exists():
            continue  # The configurator decides which conditional files to add.
        old_hash = digest(path)
        if old_hash == source_hash:
            continue
        if old_hash not in historical.get(relative, set()):
            raise RuntimeError("Preserving modified/unrecognized candidate source: " + relative + " " + old_hash)
        backup = safe_path(root, ".candidate-source-backup/" + old_hash + "/" + relative)
        if backup.exists() and digest(backup) != old_hash:
            raise RuntimeError("Candidate source backup conflict: " + str(backup))
        changes.append((relative, path, backup, old_hash, source_hash))
    removed = []
    try:
        for relative, path, backup, old_hash, source_hash in changes:
            backup.parent.mkdir(parents=True, exist_ok=True)
            safe_path(root, relative)
            if digest(path) != old_hash:
                raise RuntimeError("Candidate source changed during refresh: " + relative)
            if not backup.exists():
                with backup.open("xb") as stream:
                    stream.write(path.read_bytes())
            if digest(backup) != old_hash:
                raise RuntimeError("Candidate source backup verification failed: " + relative)
            path.unlink()
            removed.append((relative, path, backup, old_hash, source_hash))
            print("REFRESH_CANDIDATE_SOURCE " + relative + " " + old_hash + " -> " + source_hash)
        generateProjectContent(name, None)
        for relative, path, backup, old_hash, source_hash in removed:
            safe_path(root, relative)
            if not path.is_file() or digest(path) != source_hash:
                raise RuntimeError("Configurator did not refresh candidate source: " + relative)
            print("VERIFIED_CANDIDATE_SOURCE " + relative + " " + source_hash)
    except BaseException:
        for relative, path, backup, old_hash, source_hash in removed:
            # Never overwrite a file created or edited while the API was running.
            safe_path(root, relative)
            if not path.exists():
                with path.open("xb") as stream:
                    stream.write(backup.read_bytes())
        raise


candidate_project = correct_ra8p1_debugger(name, location)
configuration = openDDSCConfiguration(name)
threads = [thread for thread in configuration.getThreads() if thread.isHal()]
if len(threads) != 1:
    raise RuntimeError("Expected exactly one HAL/common thread")
thread = threads[0]


def set_properties(owner, settings):
    for property_id, value in settings.items():
        prop = owner.getProperty(property_id)
        if prop is None:
            raise ValueError("Unavailable property: " + property_id)
        prop.setValue(str(value))
        if str(prop.getValue()) != str(value):
            raise RuntimeError("Property was not accepted: " + property_id)
        print("SET_PROPERTY " + property_id + "=" + str(value))


def select_stack(thread, module_id, properties):
    names = [(key, str(value)) for key, value in properties.items()
             if key.startswith("module.") and key.endswith(".name")]
    if len(names) > 1:
        raise ValueError("Ambiguous requested module name: " + module_id)
    if names:
        name_id, requested_name = names[0]
        matches = []
        for stack in thread.getStacks():
            # The host generates instance IDs as the exact factory ID plus a decimal suffix.
            if re.fullmatch(re.escape(module_id) + r"\.[0-9]+", str(stack.getId())) is None:
                continue
            if any(str(prop.getId()) == name_id and str(prop.getValue()) == requested_name
                   for prop in stack.getProperties()):
                matches.append(stack)
        if len(matches) > 1:
            raise RuntimeError("Ambiguous existing stacks: " + module_id + " named " + requested_name)
        if matches:
            return matches[0], False
    # Requests without a name retain the add-stack behavior; there is no identity to match.
    if module_id not in list(thread.getCreatableModuleIds()):
        raise ValueError("Stack is unavailable for this target: " + module_id)
    return thread.createStack(module_id), True


set_properties(configuration.getBSP(), spec.get("bsp", {}))
for entry in spec.get("modules", []):
    module_id = entry["id"]
    properties = entry.get("properties", {})
    module, created = select_stack(thread, module_id, properties)
    set_properties(module, properties)
    print(("ADDED_STACK " if created else "REUSED_STACK ") + module_id + " " + str(module.getId()))

pins = spec.get("pins", [])
if pins:
    # Board profiles may keep an inactive device-default table alongside the active table.
    saved_model = ET.parse(Path(location) / "configuration.xml").getroot()
    active_names = [node.get("name") for node in saved_model.findall("./raPinConfiguration/pincfg")
                    if node.get("active") == "true" and node.get("symbol")]
    if len(active_names) != 1:
        raise RuntimeError("Expected exactly one active named pin configuration")
    configurations = [item for item in configuration.getPinConfigurations()
                      if str(item.getName()) == active_names[0]]
    if len(configurations) != 1:
        raise RuntimeError("Active pin configuration is not uniquely available in the model")
    pin_configuration = configurations[0]
    for entry in pins:
        component = pin_configuration.getComponent(entry["component"])
        for config_id, value in entry.get("configs", {}).items():
            config = component.getConfig(config_id)
            if value not in list(config.getOptions()):
                raise ValueError("Unavailable pin option: " + value)
            config.setValue(value)
        for pin_id, value in entry.get("connections", {}).items():
            pin = component.getPin(pin_id)
            if value not in list(pin.getOptions()):
                raise ValueError("Unavailable pin connection: " + value)
            pin.setValue(value)
        print("SET_PIN " + entry["component"])
configuration.save()
generate_candidate_content(candidate_project, name, location)
for problem in configuration.getProblems():
    print("CONFIGURATION_PROBLEM " + str(problem))

if "source" in spec:
    source = (spec_path.parent / spec["source"]).resolve()
    if not source.is_file() or source.suffix != ".c":
        raise ValueError("Missing C application fixture: " + str(source))
    destination = Path(location) / "src" / "hal_entry.c"
    if destination.is_symlink():
        raise ValueError("Refusing symlinked application source")
    destination.write_bytes(source.read_bytes())
    print("APPLICATION_FIXTURE " + str(source))

started = time.time()
for kind, filename in ((CLEAN_BUILD, "drivers-clean.log"), (FULL_BUILD, "drivers-build.log")):
    log = Path(location) / filename
    if log.exists():
        log.unlink()
    print(buildProject(name, kind, str(log)))
    text = log.read_text()
    if re.search(r"(?<!\w)error:|No rule to make target", text):
        raise RuntimeError("Build failed: " + str(log))
    if kind == CLEAN_BUILD:
        if (Path(location) / "Debug" / (name + ".elf")).exists():
            raise RuntimeError("Clean left the previous ELF in place: " + str(log))
    elif not re.search(r"\b0 errors(?:,|\b)", text):
        raise RuntimeError("Build did not finish with zero errors: " + str(log))
artifact = Path(location) / "Debug" / (name + ".elf")
if not artifact.is_file() or artifact.stat().st_mtime < started:
    raise RuntimeError("Missing fresh build artifact: " + str(artifact))
print("DRIVER_BUILD_ARTIFACT " + str(artifact))
