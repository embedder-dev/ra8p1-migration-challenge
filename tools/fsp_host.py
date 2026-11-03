"""Shared EASE orchestration. The caller supplies the host's injected API objects."""
import json
from pathlib import Path
import re
import time

from fsp_manifest import refresh_project_sources, safe_path, xml


def require_ok(status, step):
    def show(node, prefix=""):
        print(prefix + str(node))
        if node.getException() is not None:
            node.getException().printStackTrace()
        for child in node.getChildren():
            show(child, prefix + "  ")
    show(status)
    if status.getSeverity() >= 4:
        raise RuntimeError(step + " failed")


def prepare_project(api, name, location, expected=None, debugger_base=None, core=None):
    """Check caller-provided target settings; optionally persist the host-resolved debugger."""
    expected = dict(expected or {})
    if core is not None:
        if "Core" in expected and expected["Core"] != core:
            raise ValueError("Conflicting expected Core")
        expected["Core"] = core
    org, com = api["org"], api["com"]
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
        for key, value in expected.items():
            if key == "#DeviceCommand#" and debugger_base:
                continue
            if str(settings.getSettingString(key)) != str(value):
                raise RuntimeError("Project setting mismatch: " + key)
        if debugger_base:
            selected_core = str(settings.getSettingString("Core"))
            bundle = org.eclipse.core.runtime.Platform.getBundle("com.renesas.cdt.ra.contentgen")
            if bundle is None:
                raise RuntimeError("RA content-generation bundle unavailable")
            contributor = bundle.loadClass(
                "com.renesas.cdt.ra.ddsc.contentgen.idesettings.RaIdeSettingsContributor").newInstance()
            resolver = contributor.getDeviceFamilyIdeSettings(project, None, False)
            target = str(resolver.getMultiCoreDebugDeviceName(debugger_base, selected_core))
            if not target or target == "None":
                raise RuntimeError("Debugger resolver returned no target")
            if "#DeviceCommand#" in expected and expected["#DeviceCommand#"] != target:
                raise RuntimeError("Resolved debugger does not match expected target")
            expected["#DeviceCommand#"] = target
            settings.putSetting("#DeviceCommand#", target)
            if str(settings.getSettingString("#DeviceCommand#")) != target:
                raise RuntimeError("Debugger setting was not accepted")
            loader.save()
            print("RESOLVED_DEBUGGER " + selected_core + " " + target)
    finally:
        loader.dispose()
    saved = xml(safe_path(location, "configuration.xml").read_bytes())
    for key, value in expected.items():
        found = [node.get("value") for node in saved.findall("./generalSettings/option")
                 if node.get("key") == key]
        if found != [str(value)]:
            raise RuntimeError("Persisted setting mismatch: " + key)
    return project


def generate_content(api, name, location, manifest=None, previous=()):
    generate = lambda: api["generateProjectContent"](name, None)
    if manifest:
        manager = api["com"].renesas.cdt.ddsc.packs.io.PackIOManagerFactory.getInstance().getPackManager("ra")
        installed = Path(str(manager.getPacksFolder().toOSString()))
        print(json.dumps(refresh_project_sources(location, manifest, previous, installed, generate)))
    elif previous:
        raise ValueError("Previous manifests require a current manifest")
    else:
        generate()


def build_project(api, name, location, steps):
    started = time.time()
    artifact = safe_path(location, "Debug/" + name + ".elf")
    for kind_name, filename in steps:
        kind = api[kind_name]  # Use the objects exported by /FSP/Build, never numeric literals.
        log = safe_path(location, filename)
        log.unlink(missing_ok=True)
        print(api["buildProject"](name, kind, str(log)))
        if not log.is_file():
            raise RuntimeError("Missing build log: " + str(log))
        text = log.read_text()
        if re.search(r"(?<!\w)error:|fatal error|No rule to make target|Traceback|CoreException|BUILD FAILED", text):
            raise RuntimeError("Build failed: " + str(log))
        summaries = re.findall(r"Build Finished\. (\d+) errors, (\d+) warnings", text)
        if any(errors != "0" for errors, _ in summaries):
            raise RuntimeError("Nonzero build error summary: " + str(log))
        if kind_name == "CLEAN_BUILD":
            if artifact.exists():
                raise RuntimeError("Clean left previous ELF in place")
        elif not summaries:
            raise RuntimeError("Missing zero-error full-build summary")
    if not artifact.is_file() or artifact.stat().st_mtime < started:
        raise RuntimeError("Missing fresh build artifact: " + str(artifact))
    print("BUILD_ARTIFACT " + str(artifact))


def set_properties(owner, settings):
    for key, value in settings.items():
        prop = owner.getProperty(key)
        if prop is None:
            raise ValueError("Unavailable property: " + key)
        prop.setValue(str(value))
        if str(prop.getValue()) != str(value):
            raise RuntimeError("Property was not accepted: " + key)


def select_stack(thread, module_id, properties):
    names = [(key, str(value)) for key, value in properties.items()
             if key.startswith("module.") and key.endswith(".name")]
    if len(names) > 1:
        raise ValueError("Ambiguous requested module name")
    if names:
        key, value = names[0]
        matches = [stack for stack in thread.getStacks()
                   if re.fullmatch(re.escape(module_id) + r"\.[0-9]+", str(stack.getId()))
                   and any(str(prop.getId()) == key and str(prop.getValue()) == value
                           for prop in stack.getProperties())]
        if len(matches) > 1:
            raise RuntimeError("Ambiguous existing named stacks")
        if matches:
            return matches[0]
    if module_id not in list(thread.getCreatableModuleIds()):
        raise ValueError("Stack is unavailable: " + module_id)
    return thread.createStack(module_id)


def configure_pins(configuration, location, pins):
    if not pins:
        return
    saved = xml(safe_path(location, "configuration.xml").read_bytes())
    names = [node.get("name") for node in saved.findall("./raPinConfiguration/pincfg")
             if node.get("active") == "true" and node.get("symbol")]
    if len(names) != 1:
        raise RuntimeError("Expected one active named pin configuration")
    models = [item for item in configuration.getPinConfigurations() if str(item.getName()) == names[0]]
    if len(models) != 1:
        raise RuntimeError("Active pin configuration is not uniquely available")
    for entry in pins:
        component = models[0].getComponent(entry["component"])
        for field, getter in (("configs", component.getConfig), ("connections", component.getPin)):
            for key, value in entry.get(field, {}).items():
                option = getter(key)
                if option is None or value not in list(option.getOptions()):
                    raise ValueError("Unavailable pin option: " + key)
                option.setValue(value)
