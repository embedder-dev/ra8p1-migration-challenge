"""Generate and build a baseline/candidate smoke project through EASE.

Run inside e2 studio: script NAME ABSOLUTE_LOCATION BOARD CORE TOOLCHAIN_VERSION.
It deliberately does not hand-edit application configuration.xml or driver files.
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

loadModule("/FSP/ProjectGen")
loadModule("/FSP/Configuration")
loadModule("/FSP/Build")


def require_ok(status, step):
    def show(node, prefix=""):
        print(prefix + str(node))
        if node.getException() is not None:
            node.getException().printStackTrace()
        for child in node.getChildren():
            show(child, prefix + "  ")
    print(step + ":")
    show(status)
    if status.getSeverity() >= 4:
        raise RuntimeError(step + " failed")


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


name, location, board, core, tool_version = sys.argv[1:6]
if not os.path.isabs(location):
    raise ValueError("Project location must be absolute")
generation = projectGenerationFor("ra", "5.9.0")
generation.projectName(name)
generation.projectLocation(location)
generation.board(board)
if core not in list(generation.getAvailableCores()):
    raise ValueError("Requested core is not provided by this board: " + core)
generation.core(core)
generation.rtos("No RTOS")
generation.projectType("FLAT")
generation.template("Bare Metal - Minimal")
generation.toolchain("GNU ARM Embedded", tool_version)
generation.debugger("com.renesas.hardwaredebug.rz.jlink")
require_ok(generation.validate(), "validate")
require_ok(generation.generate(None), "generate")
candidate_project = correct_ra8p1_debugger(name, location, expected_core=core, required="RA8P1" in board.upper())
generate_candidate_content(candidate_project, name, location)
print("GENERATED_PROJECT " + name)
started = time.time()
for kind, filename in ((FULL_BUILD, "full-build.log"), (CLEAN_BUILD, "clean-build.log"),
                       (FULL_BUILD, "rebuild.log")):
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
artifact = os.path.join(location, "Debug", name + ".elf")
if not os.path.isfile(artifact) or os.path.getmtime(artifact) < started:
    raise RuntimeError("Build did not produce a fresh expected artifact: " + artifact)
print("BUILD_ARTIFACT " + artifact)
print("BUILD_INVOCATIONS_COMPLETE " + name)
