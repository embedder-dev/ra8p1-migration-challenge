"""Host-only regression tests for the EASE validation scripts; no IDE is launched."""
import ast
import base64
from contextlib import redirect_stdout
from enum import IntEnum
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET
import zipfile
import zlib


# Exact historical candidate header. A synthetic old file would not exercise the
# production SHA256 ownership gate. This is test input, never firmware output.
OLD_ELC = zlib.decompress(base64.b64decode(
    "eJx9ldFu2jAUhu/zFEfiroI2ISSlnXYRYqNmCpAGaHcXUWJGJBYQCVurae8+mxzTCR/gjnyfbJ/ff+DuBqYJ+96Ji6UoK9GJclHWxaoQ+0cYTFnH7YSbxaESFtxAGvQTB8QvaUDEqkdIbWf+5Nh+j3+zHdeG2eJtI8Dp3Xq3yp+Umw+o1wJ2233dht+LernOtz/akLARLMocAhbictXhrRI1FBWI9922EjmsxV6oVe6sVrEqc7GSx0kyHofZk9WSX4tS/PfEqj92QkmiPPwEkYnNMjuunO0X/Z1j/bFAfpTLX/h4lo0nYw5fwW6fPY9e2SybjxlPh/HkVRnv9kP33JKSbVguYTmG1Tu3ZBYZC2Z2wwfDC9xpeGicGHkXuUPxKeMz5MYsivM0xf1DYwrken/y/JLr/b1zLu84i6NRGCfRUXF7A0oZRsPJ5GWISkgpAYts5OwCd5DzC7yL3EgZudtwz0gZeQ+5kTJyD7mRMnIfuZEy8nvkRsrI+8jJlFWEKX/GjDz/ioM5efdXHMzK619xdF4PVxydWXDF0bldrIZ0dHZkN9DR+ZH9QEdnSHYkHCW6ZB5ZEiVgej7ZklAaI1zCJ3uijPlJIatyeiFdn6xKyqfyZdEK2ZYwiPmYaYUsg5oFb9knm6AEvGKfrMFxWB0HWYJmWK2QHTj9urg+ef/NsFohr78ZVivyZv/C5z9A/cWyWqLMi5X1Dyykxpk="))
ELC_PATH = "ra/fsp/src/bsp/mcu/ra8p1/bsp_elc.h"
MCU_PACK = "Renesas.RA_mcu_ra8p1.5.9.0.pack"


class BuildKind(IntEnum):
    FULL_BUILD = 6
    CLEAN_BUILD = 15


SCRIPTS = {
    "generate_fsp_validation.py": (
        (BuildKind.FULL_BUILD, "full-build.log"),
        (BuildKind.CLEAN_BUILD, "clean-build.log"),
        (BuildKind.FULL_BUILD, "rebuild.log"),
    ),
    "configure_fsp_validation.py": (
        (BuildKind.CLEAN_BUILD, "drivers-clean.log"),
        (BuildKind.FULL_BUILD, "drivers-build.log"),
    ),
}
ZERO_ERRORS = "Build Finished. 0 errors, 0 warnings.\n"
STARTED = 1_700_000_000


def stack_fixture(module_id, instance_id, properties=None):
    module = Mock(spec=["getId", "getProperty", "getProperties"])
    module.getId.return_value = module_id + "." + str(instance_id)
    values = {}

    def property_for(property_id):
        if property_id not in values:
            prop = Mock(spec=["getId", "getValue", "setValue"])
            prop.getId.return_value = property_id
            prop.getValue.return_value = ""
            prop.setValue.side_effect = lambda value: setattr(prop.getValue, "return_value", value)
            values[property_id] = prop
        return values[property_id]

    for key, value in (properties or {}).items():
        property_for(key).getValue.return_value = value
    module.getProperty.side_effect = property_for
    module.getProperties.side_effect = lambda: list(values.values())
    return module


class BuildHarness:
    def __init__(self, root, script, logs=None, artifacts=None, pin_configurations=None, settings=None,
                 save_mode="normal", stacks=None, creatable_module_ids=None):
        self.root = root
        self.script = Path(__file__).with_name(script)
        self.steps = SCRIPTS[script]
        self.logs = logs or {}
        self.artifacts = artifacts or {}
        self.pin_configurations = pin_configurations or []
        self.stacks = list(stacks or [])
        self.thread = Mock(spec=["isHal", "getCreatableModuleIds", "getStacks", "createStack"])
        self.thread.isHal.return_value = True
        self.thread.getCreatableModuleIds.return_value = (
            ["module.fixture"] if creatable_module_ids is None else creatable_module_ids)
        self.thread.getStacks.side_effect = lambda: list(self.stacks)
        self.thread.createStack.side_effect = self.create_stack
        self.name = "host_fixture"
        self.elf = root / "Debug" / (self.name + ".elf")
        self.events = []
        self.calls = []
        self.output = io.StringIO()
        self.core_events = []
        self.save_mode = save_mode
        self.settings = {"#TargetName#": "R7KA8P1KFLCAC", "#Board#": "board.ra8p1ek", "CPU": "RA8P1",
                         "Core": "CM85", "#TargetARCHITECTURE#": "cortex-m85", "#FSPVersion#": "5.9.0",
                         "#DeviceCommand#": "R7KA8P1KF_CPU0"}
        self.settings.update(settings or {})
        self.write_settings(self.settings)
        self.candidate = root / "validation/packs"
        self.installed = root / "installed-packs"
        self.candidate.mkdir(parents=True)
        self.installed.mkdir()
        self.source = root / ELC_PATH
        self.source.parent.mkdir(parents=True)
        self.latest = OLD_ELC + b"\n#define BSP_PRV_VECT_ENUM(event, id) event = id,\n"
        self.source.write_bytes(self.latest)
        self.candidate_files = {ELC_PATH: self.latest}
        self.candidate_entries = {ELC_PATH: {"category": "header", "name": ELC_PATH}}
        self.write_candidate()
        self.settings_api = Mock(spec=["getSettingString", "putSetting"])
        self.settings_api.getSettingString.side_effect = lambda key: self.settings.get(key)
        self.settings_api.putSetting.side_effect = self.put_setting
        self.loader = Mock(spec=["init", "isInitialised", "getConfigurationSettings", "save", "dispose"])
        self.loader.init.side_effect = lambda monitor: self.core_events.append("loader-init")
        self.loader.isInitialised.return_value = True
        self.loader.getConfigurationSettings.return_value = self.settings_api
        self.loader.save.side_effect = self.save_model
        self.loader.dispose.side_effect = lambda: self.core_events.append("loader-dispose")
        self.resolver = Mock(spec=["getMultiCoreDebugDeviceName"])
        self.resolver.getMultiCoreDebugDeviceName.side_effect = self.resolve_command
        self.project = object()
        self.workspace_project = Mock(spec=["exists"])
        self.workspace_project.exists.return_value = True
        self.org = Mock()
        self.workspace_root = self.org.eclipse.core.resources.ResourcesPlugin.getWorkspace.return_value.getRoot.return_value
        self.workspace_root.getProject.return_value = self.workspace_project
        self.adapter_manager = self.org.eclipse.core.runtime.Platform.getAdapterManager.return_value
        self.adapter_manager.loadAdapter.return_value = self.project
        self.com = Mock()
        ddsc = self.com.renesas.cdt.ddsc
        ddsc.scripting = Mock(spec=[])
        ddsc.contentgen.internal.config.DDSCProjectConfigurationLoader.return_value = self.loader
        self.pack_manager = ddsc.packs.io.PackIOManagerFactory.getInstance.return_value.getPackManager.return_value
        self.pack_manager.getPacksFolder.return_value.toOSString.return_value = str(self.installed)
        self.com.renesas.cdt.ra.ddsc.contentgen.idesettings = Mock(spec=[])
        self.contributor = Mock(spec=["getDeviceFamilyIdeSettings"])
        self.contributor.getDeviceFamilyIdeSettings.return_value = self.resolver
        self.contributor_class = Mock(spec=["newInstance"])
        self.contributor_class.newInstance.return_value = self.contributor
        self.ra_bundle = Mock(spec=["loadClass"])
        self.ra_bundle.loadClass.return_value = self.contributor_class
        self.org.eclipse.core.runtime.Platform.getBundle.return_value = self.ra_bundle
        self.write_artifact("stale")
        for _, filename in self.steps:
            (root / filename).write_text(ZERO_ERRORS)

    def write_candidate(self):
        pdsc = ET.Element("package")
        component = ET.SubElement(ET.SubElement(pdsc, "components"), "component",
                                  Cclass="BSP", Cgroup="ra8p1", Csub="fsp", Cversion="5.9.0")
        files = ET.SubElement(component, "files")
        for attributes in self.candidate_entries.values():
            ET.SubElement(files, "file", attributes)
        contents = dict(self.candidate_files, **{"fixture.pdsc": ET.tostring(pdsc)})
        archive_path = self.candidate / MCU_PACK
        with zipfile.ZipFile(archive_path, "w") as archive:
            for name, data in contents.items():
                archive.writestr(name, data)
        data = archive_path.read_bytes()
        (self.installed / MCU_PACK).write_bytes(data)
        self.manifest = {"candidate": "custom-5.9-derived-ra8p1", "packs": {MCU_PACK: {
            "sha256": hashlib.sha256(data).hexdigest(),
            "entries": {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}}}}
        self.write_manifest()
        saved = ET.parse(self.root / "configuration.xml").getroot()
        for node in saved.findall("raComponentSelection"):
            saved.remove(node)
        components = ET.SubElement(saved, "raComponentSelection")
        item = ET.SubElement(components, "component", {"class": "BSP", "group": "ra8p1",
                            "subgroup": "fsp", "variant": "", "version": "5.9.0"})
        ET.SubElement(item, "originalPack").text = MCU_PACK
        ET.ElementTree(saved).write(self.root / "configuration.xml")

    def write_manifest(self):
        (self.candidate / "manifest.json").write_text(json.dumps(self.manifest))

    def create_stack(self, module_id):
        module = stack_fixture(module_id, 1000 + len(self.stacks))
        self.stacks.append(module)
        return module

    def write_settings(self, settings, mode="normal"):
        path = self.root / "configuration.xml"
        root = ET.parse(path).getroot() if path.exists() else ET.Element("raConfiguration")
        for node in root.findall("generalSettings"):
            root.remove(node)
        general = ET.SubElement(root, "generalSettings")
        for key, value in settings.items():
            if value is not None and not (mode == "missing" and key == "#DeviceCommand#"):
                if mode == "stale" and key == "#DeviceCommand#":
                    value = "R7KA8P1KF"
                ET.SubElement(general, "option", key=key, value=value)
        if mode == "duplicate":
            ET.SubElement(general, "option", key="#DeviceCommand#", value=settings["#DeviceCommand#"])
        if root.find("raComponentSelection") is None:
            components = ET.SubElement(root, "raComponentSelection")
            item = ET.SubElement(components, "component", {"class": "BSP", "group": "ra8p1",
                                "subgroup": "fsp", "variant": "", "version": "5.9.0"})
            ET.SubElement(item, "originalPack").text = MCU_PACK
        ET.ElementTree(root).write(path)

    def put_setting(self, key, value):
        self.core_events.append("put-setting")
        self.settings[key] = value

    def save_model(self):
        self.core_events.append("loader-save")
        self.write_settings(self.settings, self.save_mode)

    def resolve_command(self, base, core):
        self.core_events.append("resolve-command")
        return base + {"CM85": "_CPU0", "CM33": "_CPU1"}[core]

    def open_configuration(self, name):
        if name != self.name:
            raise AssertionError("Unexpected configuration name")
        self.core_events.append("open-configuration")
        # Model helpers keep a snapshot; opening before the correction would restore stale settings.
        snapshot = {node.get("key"): node.get("value") for node in
                    ET.parse(self.root / "configuration.xml").findall("./generalSettings/option")}
        self.configuration.save.side_effect = lambda: self.write_settings(snapshot)
        return self.configuration

    def write_artifact(self, action):
        if action == "remove":
            if self.elf.exists():
                self.elf.unlink()
        elif action != "keep":
            self.elf.parent.mkdir(parents=True, exist_ok=True)
            self.elf.write_bytes(b"host ELF fixture, not firmware")
            timestamp = STARTED + (1 if action == "fresh" else -1)
            os.utime(self.elf, (timestamp, timestamp))

    def generate_content(self, name, monitor):
        if name != self.name or monitor is not None:
            raise AssertionError("Unexpected Generate Project Content arguments")
        self.events.append("generate-content")
        for source, data in self.candidate_files.items():
            if not source.startswith("ra/"):
                continue
            path = self.root / source
            if not path.exists() and not path.is_symlink():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)

    def build(self, name, kind, filename):
        index = len(self.calls)
        expected_kind, expected_filename = self.steps[index]
        # Identity detects hardcoded 6/15 as well as the old incorrect 1/4.
        if kind is not expected_kind:
            raise AssertionError("Build must use the exported " + expected_kind.name)
        if name != self.name or Path(filename) != self.root / expected_filename:
            raise AssertionError("Unexpected build project or log path")
        log = Path(filename)
        if log.exists():
            raise AssertionError("Previous build log was not removed")
        self.calls.append(kind)
        self.events.append("build")
        text = self.logs.get(index, "make clean\n" if kind is BuildKind.CLEAN_BUILD else ZERO_ERRORS)
        if text is not None:
            log.write_text(text)
        action = self.artifacts.get(index, "remove" if kind is BuildKind.CLEAN_BUILD else "fresh")
        self.write_artifact(action)
        # EASE may return no console text even when the log contains the result.
        return ""

    def run(self, extra_args=None, argv_type=tuple):
        status = Mock()
        status.getSeverity.return_value = 0
        status.getException.return_value = None
        status.getChildren.return_value = []
        generation = Mock()
        generation.getAvailableCores.return_value = ["CM85", "CM33"]
        generation.validate.return_value = status
        generation.generate.return_value = status
        configuration = self.configuration = Mock()
        configuration.getThreads.return_value = [self.thread]
        configuration.getProblems.return_value = []
        configuration.getPinConfigurations.return_value = self.pin_configurations
        # EASE uses argv[0] for the requested script, not its bootstrap __file__.
        arguments = [str(self.root / "tools" / self.script.name), self.name, str(self.root)]
        if extra_args is not None:
            arguments += extra_args
        elif self.script.name == "generate_fsp_validation.py":
            arguments += ["EK-RA8P1", "CM85", "13.2.1"]
        else:
            arguments += ["module.fixture"]
        exports = {
            "__name__": "__main__",
            "__file__": str(self.script),
            "FULL_BUILD": BuildKind.FULL_BUILD,
            "CLEAN_BUILD": BuildKind.CLEAN_BUILD,
            "loadModule": Mock(),
            "projectGenerationFor": Mock(return_value=generation),
            "openDDSCConfiguration": Mock(side_effect=self.open_configuration),
            "com": self.com,
            "org": self.org,
            "generateProjectContent": self.generate_content,
            "buildProject": self.build,
        }
        # runpy mutates argv[0], so execute the AST to preserve EASE's tuple argv.
        tree = ast.parse(self.script.read_text(), filename=str(self.script))
        with patch.object(sys, "argv", argv_type(arguments)), patch("time.time", return_value=STARTED), \
                redirect_stdout(self.output):
            exec(compile(tree, str(self.script), "exec"), exports)


class FspBuildHarnessTests(unittest.TestCase):
    def harness(self, script, **kwargs):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return BuildHarness(Path(temporary.name), script, **kwargs)

    def test_ra8p1_command_is_resolved_saved_and_used_for_both_cores(self):
        for script in SCRIPTS:
            for core, architecture, target in (("CM85", "cortex-m85", "R7KA8P1KF_CPU0"),
                                                ("CM33", "cortex-m33", "R7KA8P1KF_CPU1")):
                for old in ("R7KA8P1KF", "R7KA8P1KF_CPU0", "R7KA8P1KF_CPU1"):
                    with self.subTest(script=script, core=core, old=old):
                        harness = self.harness(script, settings={"Core": core,
                            "#TargetARCHITECTURE#": architecture, "#DeviceCommand#": old})
                        arguments = (["EK-RA8P1", core, "13.2.1"]
                                     if script == "generate_fsp_validation.py" else None)
                        harness.run(extra_args=arguments)
                        harness.resolver.getMultiCoreDebugDeviceName.assert_called_once_with("R7KA8P1KF", core)
                        harness.settings_api.putSetting.assert_called_once_with("#DeviceCommand#", target)
                        harness.loader.save.assert_called_once_with()
                        harness.loader.dispose.assert_called_once_with()
                        self.assertEqual(harness.core_events[:5], ["loader-init", "resolve-command",
                            "put-setting", "loader-save", "loader-dispose"])
                        if script == "configure_fsp_validation.py":
                            self.assertEqual(harness.core_events[5:], ["open-configuration"])
                        saved = ET.parse(harness.root / "configuration.xml")
                        values = saved.findall("./generalSettings/option[@key='#DeviceCommand#']")
                        self.assertEqual([node.get("value") for node in values], [target])
                        self.assertIn("RESOLVED_DEBUGGER " + core + " " + target, harness.output.getvalue())
                        factory = harness.com.renesas.cdt.ddsc.contentgen.internal.config
                        factory.DDSCProjectConfigurationLoader.assert_called_once_with(harness.project)

    def test_scripts_use_public_workspace_adapter_not_unexported_script_utils(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                harness = self.harness(script)
                harness.run()
                harness.workspace_root.getProject.assert_called_once_with(harness.name)
                harness.workspace_project.exists.assert_called_once_with()
                harness.adapter_manager.loadAdapter.assert_called_once_with(
                    harness.workspace_project, "com.renesas.cdt.ddsc.project.IDDSCProject")
                factory = harness.com.renesas.cdt.ddsc.contentgen.internal.config
                factory.DDSCProjectConfigurationLoader.assert_called_once_with(harness.project)

    def test_scripts_load_unexported_contributor_from_owning_bundle(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                harness = self.harness(script)
                harness.run()
                harness.org.eclipse.core.runtime.Platform.getBundle.assert_called_once_with(
                    "com.renesas.cdt.ra.contentgen")
                harness.ra_bundle.loadClass.assert_called_once_with(
                    "com.renesas.cdt.ra.ddsc.contentgen.idesettings.RaIdeSettingsContributor")
                harness.contributor_class.newInstance.assert_called_once_with()
                harness.contributor.getDeviceFamilyIdeSettings.assert_called_once_with(
                    harness.project, None, False)

    def test_unavailable_or_unloadable_contributor_fails_before_settings_mutation(self):
        for script in SCRIPTS:
            for failure in ("bundle", "class", "constructor"):
                with self.subTest(script=script, failure=failure):
                    harness = self.harness(script)
                    if failure == "bundle":
                        harness.org.eclipse.core.runtime.Platform.getBundle.return_value = None
                        message = "bundle is unavailable"
                    elif failure == "class":
                        harness.ra_bundle.loadClass.side_effect = RuntimeError("class load failed")
                        message = "class load failed"
                    else:
                        harness.contributor_class.newInstance.side_effect = RuntimeError("constructor failed")
                        message = "constructor failed"
                    with self.assertRaisesRegex(RuntimeError, message):
                        harness.run()
                    harness.settings_api.putSetting.assert_not_called()
                    harness.loader.save.assert_not_called()
                    harness.loader.dispose.assert_called_once_with()
                    self.assertEqual(harness.events, [])

    def test_scripts_reject_missing_or_non_fsp_workspace_project(self):
        for script in SCRIPTS:
            for exists in (False, True):
                with self.subTest(script=script, exists=exists):
                    harness = self.harness(script)
                    harness.workspace_project.exists.return_value = exists
                    harness.adapter_manager.loadAdapter.return_value = None
                    message = "not an FSP project" if exists else "not registered"
                    with self.assertRaisesRegex(RuntimeError, message):
                        harness.run()
                    if not exists:
                        harness.adapter_manager.loadAdapter.assert_not_called()
                    factory = harness.com.renesas.cdt.ddsc.contentgen.internal.config
                    factory.DDSCProjectConfigurationLoader.assert_not_called()
                    self.assertEqual(harness.events, [])

    def test_wrong_part_core_architecture_or_version_fail_before_mutation(self):
        cases = (("#TargetName#", "R7KA8P1KFLCAC_CPU1", "physical part"),
                 ("#TargetName#", "R7FA8M1AHECBD", "physical part"),
                 ("#TargetName#", None, "physical part"),
                 ("Core", "CM4", "selected core"), ("Core", None, "selected core"),
                 ("#TargetARCHITECTURE#", "cortex-m33", "setting mismatch"),
                 ("#TargetARCHITECTURE#", None, "setting mismatch"),
                 ("#FSPVersion#", "6.6.0", "setting mismatch"),
                 ("#FSPVersion#", None, "setting mismatch"))
        for script in SCRIPTS:
            for key, value, message in cases:
                with self.subTest(script=script, key=key, value=value):
                    harness = self.harness(script, settings={key: value})
                    with self.assertRaisesRegex(RuntimeError, message):
                        harness.run()
                    harness.settings_api.putSetting.assert_not_called()
                    harness.loader.save.assert_not_called()
                    harness.loader.dispose.assert_called_once_with()
                    self.assertEqual(harness.events, [])

    def test_generated_core_must_match_requested_core(self):
        harness = self.harness("generate_fsp_validation.py", settings={"Core": "CM33",
                               "#TargetARCHITECTURE#": "cortex-m33"})
        with self.assertRaisesRegex(RuntimeError, "selected core"):
            harness.run(extra_args=["EK-RA8P1", "CM85", "13.2.1"])
        harness.settings_api.putSetting.assert_not_called()
        self.assertEqual(harness.calls, [])

    def test_original_other_board_workflow_is_not_retargeted(self):
        settings = {"#TargetName#": "R7FA8M1AHECBD", "#Board#": "board.ra8m1ek",
                    "CPU": "RA8M1", "#DeviceCommand#": "R7FA8M1AH"}
        for script in SCRIPTS:
            with self.subTest(script=script):
                harness = self.harness(script, settings=settings)
                arguments = ["EK-RA8M1", "CM85", "13.2.1"] if script == "generate_fsp_validation.py" else None
                harness.run(extra_args=arguments)
                harness.resolver.getMultiCoreDebugDeviceName.assert_not_called()
                harness.settings_api.putSetting.assert_not_called()
                harness.loader.save.assert_not_called()
                harness.loader.dispose.assert_called_once_with()
                self.assertEqual(harness.settings["#DeviceCommand#"], "R7FA8M1AH")
                self.assertEqual(harness.calls, [kind for kind, _ in SCRIPTS[script]])

    def test_requested_ra8p1_cannot_silently_generate_other_board(self):
        harness = self.harness("generate_fsp_validation.py", settings={
            "#TargetName#": "R7FA8M1AHECBD", "#Board#": "board.ra8m1ek", "CPU": "RA8M1"})
        with self.assertRaisesRegex(RuntimeError, "physical part"):
            harness.run()
        harness.loader.save.assert_not_called()
        self.assertEqual(harness.events, [])

    def test_uninitialized_loader_and_wrong_resolver_fail_closed(self):
        for script in SCRIPTS:
            with self.subTest(script=script, failure="initialization"):
                harness = self.harness(script)
                harness.loader.isInitialised.return_value = False
                with self.assertRaisesRegex(RuntimeError, "did not initialize"):
                    harness.run()
                harness.settings_api.putSetting.assert_not_called()
                harness.loader.dispose.assert_called_once_with()
                self.assertEqual(harness.events, [])
            with self.subTest(script=script, failure="resolver"):
                harness = self.harness(script)
                harness.resolver.getMultiCoreDebugDeviceName.side_effect = None
                harness.resolver.getMultiCoreDebugDeviceName.return_value = "R7KA8P1KF_CPU1"
                with self.assertRaisesRegex(RuntimeError, "resolver mismatch"):
                    harness.run()
                harness.settings_api.putSetting.assert_not_called()
                harness.loader.save.assert_not_called()
                harness.loader.dispose.assert_called_once_with()
                self.assertEqual(harness.events, [])

    def test_model_exceptions_dispose_without_building(self):
        for script in SCRIPTS:
            for method in ("init", "save"):
                with self.subTest(script=script, method=method):
                    harness = self.harness(script)
                    getattr(harness.loader, method).side_effect = RuntimeError("host API failure")
                    with self.assertRaisesRegex(RuntimeError, "host API failure"):
                        harness.run()
                    harness.loader.dispose.assert_called_once_with()
                    self.assertEqual(harness.events, [])

    def test_rejected_or_unpersisted_command_stops_before_content_generation(self):
        for script in SCRIPTS:
            with self.subTest(script=script, failure="setter"):
                harness = self.harness(script, settings={"#DeviceCommand#": "R7KA8P1KF"})
                harness.settings_api.putSetting.side_effect = None
                with self.assertRaisesRegex(RuntimeError, "not accepted"):
                    harness.run()
                harness.loader.save.assert_not_called()
                harness.loader.dispose.assert_called_once_with()
                self.assertEqual(harness.events, [])
            for mode in ("missing", "duplicate", "stale"):
                with self.subTest(script=script, mode=mode):
                    harness = self.harness(script, save_mode=mode)
                    with self.assertRaisesRegex(RuntimeError, "persisted setting mismatch: #DeviceCommand#"):
                        harness.run()
                    harness.loader.dispose.assert_called_once_with()
                    self.assertEqual(harness.events, [])

    def test_persistence_revalidates_part_core_and_architecture(self):
        for script in SCRIPTS:
            for key, value in (("#TargetName#", "R7FA8M1AHECBD"), ("Core", "CM33"),
                               ("#TargetARCHITECTURE#", "cortex-m33"), ("#FSPVersion#", "6.6.0")):
                with self.subTest(script=script, key=key):
                    harness = self.harness(script)

                    def save_changed_model():
                        harness.settings[key] = value
                        harness.save_model()

                    harness.loader.save.side_effect = save_changed_model
                    with self.assertRaisesRegex(RuntimeError, "persisted setting mismatch"):
                        harness.run()
                    self.assertEqual(harness.events, [])
                    harness.loader.dispose.assert_called_once_with()

    def test_standalone_script_correction_functions_stay_identical(self):
        functions = []
        for script in SCRIPTS:
            tree = ast.parse(Path(__file__).with_name(script).read_text())
            functions.append(ast.dump(next(node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "correct_ra8p1_debugger")))
        self.assertEqual(functions[0], functions[1])

    def test_standalone_source_refresh_functions_stay_identical(self):
        functions = []
        for script in SCRIPTS:
            tree = ast.parse(Path(__file__).with_name(script).read_text())
            functions.append(ast.dump(next(node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "generate_candidate_content")))
        self.assertEqual(functions[0], functions[1])

    def test_same_version_sources_are_refreshed_by_configurator_for_both_cores(self):
        self.assertEqual(hashlib.sha256(OLD_ELC).hexdigest(),
                         "55d21b7f4d13006d3ceee452030f27ba011de9dc910ecf484fc5e2d242ae1a41")
        for script in SCRIPTS:
            for core, architecture in (("CM85", "cortex-m85"), ("CM33", "cortex-m33")):
                with self.subTest(script=script, core=core):
                    harness = self.harness(script, settings={"Core": core, "#TargetARCHITECTURE#": architecture})
                    harness.source.write_bytes(OLD_ELC)
                    original_generation = harness.generate_content

                    def extract(name, monitor):
                        self.assertFalse(harness.source.exists())
                        original_generation(name, monitor)

                    harness.generate_content = extract
                    args = ["EK-RA8P1", core, "13.2.1"] if script == "generate_fsp_validation.py" else None
                    harness.run(extra_args=args)
                    self.assertEqual(harness.source.read_bytes(), harness.latest)
                    backups = list((harness.root / ".candidate-source-backup").rglob("bsp_elc.h"))
                    self.assertEqual(len(backups), 1)
                    self.assertEqual(backups[0].read_bytes(), OLD_ELC)
                    self.assertIn("VERIFIED_CANDIDATE_SOURCE " + ELC_PATH, harness.output.getvalue())
                    self.assertEqual(harness.events[0], "generate-content")

    def test_current_sources_are_not_removed_or_rewritten(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                harness = self.harness(script)
                before = harness.source.stat().st_mtime_ns
                harness.run()
                self.assertEqual(harness.source.stat().st_mtime_ns, before)
                self.assertFalse((harness.root / ".candidate-source-backup").exists())
                self.assertNotIn("REFRESH_CANDIDATE_SOURCE", harness.output.getvalue())

    def test_source_refresh_retry_keeps_backup_and_skips_current_bytes(self):
        harness = self.harness("configure_fsp_validation.py")
        harness.source.write_bytes(OLD_ELC)
        harness.run()
        harness.calls.clear()
        harness.run()
        self.assertEqual(harness.output.getvalue().count("REFRESH_CANDIDATE_SOURCE"), 1)
        self.assertEqual(harness.source.read_bytes(), harness.latest)

    def test_modified_sources_stop_before_generation_or_any_removal(self):
        for script in SCRIPTS:
            for changed_path in (ELC_PATH, "ra/a_user_edit.h"):
                with self.subTest(script=script, changed_path=changed_path):
                    harness = self.harness(script)
                    harness.candidate_files["ra/a_user_edit.h"] = b"original"
                    harness.candidate_entries["ra/a_user_edit.h"] = {"name": "ra/a_user_edit.h", "category": "header"}
                    harness.write_candidate()
                    other = harness.root / "ra/a_user_edit.h"
                    other.write_bytes(b"original")
                    harness.source.write_bytes(OLD_ELC)
                    changed = harness.root / changed_path
                    changed.write_bytes(b"user edited\n")
                    with self.assertRaisesRegex(RuntimeError, "Preserving modified/unrecognized"):
                        harness.run()
                    self.assertEqual(changed.read_bytes(), b"user edited\n")
                    self.assertEqual(harness.events, [])
                    self.assertFalse((harness.root / ".candidate-source-backup").exists())
                    if changed_path != ELC_PATH:
                        self.assertEqual(harness.source.read_bytes(), OLD_ELC)

    def test_historical_hash_does_not_authorize_another_path(self):
        harness = self.harness("configure_fsp_validation.py")
        other_name = "ra/unrecognized.h"
        harness.candidate_files[other_name] = b"latest content"
        harness.candidate_entries[other_name] = {"name": other_name}
        harness.write_candidate()
        other = harness.root / other_name
        other.write_bytes(OLD_ELC)
        with self.assertRaisesRegex(RuntimeError, "Preserving modified/unrecognized"):
            harness.run()
        self.assertEqual(other.read_bytes(), OLD_ELC)
        self.assertEqual(harness.events, [])

    def test_non_candidate_board_does_not_require_candidate_manifest(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                harness = self.harness(script, settings={"#TargetName#": "R7FA8M1AHECBD",
                    "#Board#": "board.ra8m1ek", "CPU": "RA8M1"})
                (harness.candidate / "manifest.json").unlink()
                args = ["EK-RA8M1", "CM85", "13.2.1"] if script == "generate_fsp_validation.py" else None
                harness.run(extra_args=args)
                self.assertEqual(harness.events[0], "generate-content")
                harness.pack_manager.getPacksFolder.assert_not_called()

    def test_unowned_and_template_files_are_not_considered_for_refresh(self):
        harness = self.harness("configure_fsp_validation.py")
        user = harness.root / "ra/user_file.h"
        user.write_bytes(b"user edit")
        template = harness.root / "ra/template.h"
        template.write_bytes(b"user template edit")
        harness.candidate_files["ra/template.h"] = b"template default"
        harness.candidate_entries["ra/template.h"] = {"name": "ra/template.h", "attr": "template"}
        harness.write_candidate()
        harness.run()
        self.assertEqual(user.read_bytes(), b"user edit")
        self.assertEqual(template.read_bytes(), b"user template edit")

    def test_corrupt_or_mismatched_pack_and_manifest_fail_closed(self):
        for target in ("installed", "archive", "manifest", "entry", "pdsc"):
            with self.subTest(target=target):
                harness = self.harness("configure_fsp_validation.py")
                harness.source.write_bytes(OLD_ELC)
                if target == "installed":
                    (harness.installed / MCU_PACK).write_bytes(b"not the candidate")
                elif target == "archive":
                    (harness.candidate / MCU_PACK).write_bytes(b"not the candidate")
                elif target == "manifest":
                    harness.manifest["candidate"] = "another pack"
                    harness.write_manifest()
                else:
                    name = ELC_PATH if target == "entry" else "fixture.pdsc"
                    harness.manifest["packs"][MCU_PACK]["entries"][name] = "0" * 64
                    harness.write_manifest()
                with self.assertRaises(ValueError):
                    harness.run()
                self.assertEqual(harness.source.read_bytes(), OLD_ELC)
                self.assertEqual(harness.events, [])

    def test_source_and_backup_symlinks_are_rejected_without_touching_target(self):
        for link_kind in ("file", "parent", "backup", "pack"):
            with self.subTest(link=link_kind):
                harness = self.harness("configure_fsp_validation.py")
                harness.source.write_bytes(OLD_ELC)
                outside = harness.root / "outside"
                outside.mkdir()
                sentinel = outside / "sentinel.h"
                sentinel.write_bytes(OLD_ELC)
                if link_kind == "file":
                    harness.source.unlink()
                    harness.source.symlink_to(sentinel)
                elif link_kind == "parent":
                    harness.source.unlink()
                    harness.source.parent.rmdir()
                    harness.source.parent.symlink_to(outside, target_is_directory=True)
                elif link_kind == "backup":
                    (harness.root / ".candidate-source-backup").symlink_to(outside, target_is_directory=True)
                else:
                    path = harness.installed / MCU_PACK
                    path.unlink()
                    path.symlink_to(harness.candidate / MCU_PACK)
                with self.assertRaisesRegex(ValueError, "symlinked"):
                    harness.run()
                self.assertEqual(sentinel.read_bytes(), OLD_ELC)
                self.assertEqual(harness.events, [])

    def test_unsafe_manifest_source_paths_are_rejected(self):
        for name in ("ra/../outside.h", "ra//outside.h", "ra/./outside.h", "ra/C:outside.h", "ra/back\\\\slash.h"):
            with self.subTest(name=name):
                harness = self.harness("configure_fsp_validation.py")
                harness.candidate_files[name] = b"unsafe"
                harness.candidate_entries[name] = {"name": name}
                harness.write_candidate()
                harness.source.write_bytes(OLD_ELC)
                with self.assertRaisesRegex(ValueError, "Unsafe candidate source path"):
                    harness.run()
                self.assertEqual(harness.source.read_bytes(), OLD_ELC)
                self.assertEqual(harness.events, [])

    def test_failed_extraction_restores_missing_original_and_does_not_build(self):
        for failure in ("exception", "missing", "wrong-bytes", "cancelled"):
            with self.subTest(failure=failure):
                harness = self.harness("configure_fsp_validation.py")
                harness.source.write_bytes(OLD_ELC)

                def generate(name, monitor):
                    self.assertFalse(harness.source.exists())
                    if failure == "exception":
                        raise RuntimeError("API failure")
                    if failure == "cancelled":
                        raise KeyboardInterrupt()
                    if failure == "wrong-bytes":
                        harness.source.write_bytes(b"unexpected generated data")

                harness.generate_content = generate
                with self.assertRaises((RuntimeError, KeyboardInterrupt)):
                    harness.run()
                expected = b"unexpected generated data" if failure == "wrong-bytes" else OLD_ELC
                self.assertEqual(harness.source.read_bytes(), expected)
                backups = list((harness.root / ".candidate-source-backup").rglob("bsp_elc.h"))
                self.assertEqual(backups[0].read_bytes(), OLD_ELC)
                self.assertEqual(harness.calls, [])

    def test_backup_conflict_is_preserved_and_stops_refresh(self):
        harness = self.harness("configure_fsp_validation.py")
        harness.source.write_bytes(OLD_ELC)
        backup = harness.root / ".candidate-source-backup" / hashlib.sha256(OLD_ELC).hexdigest() / ELC_PATH
        backup.parent.mkdir(parents=True)
        backup.write_bytes(b"conflicting backup")
        with self.assertRaisesRegex(RuntimeError, "backup conflict"):
            harness.run()
        self.assertEqual(backup.read_bytes(), b"conflicting backup")
        self.assertEqual(harness.source.read_bytes(), OLD_ELC)
        self.assertEqual(harness.events, [])

    def test_exported_build_kinds_and_summaryless_clean_succeed(self):
        for script, steps in SCRIPTS.items():
            with self.subTest(script=script):
                harness = self.harness(script)
                harness.run()
                self.assertEqual(harness.calls, [kind for kind, _ in steps])
                self.assertEqual(harness.events, ["generate-content"] + ["build"] * len(steps))
                self.assertGreater(harness.elf.stat().st_mtime, STARTED)
                self.assertIn("BUILD_ARTIFACT " + str(harness.elf), harness.output.getvalue())
                for kind, filename in steps:
                    if kind is BuildKind.CLEAN_BUILD:
                        self.assertNotIn("errors", (harness.root / filename).read_text())

    def test_json_spec_is_selected_with_tuple_and_list_argv(self):
        for argv_type in (tuple, list):
            with self.subTest(argv_type=argv_type.__name__):
                harness = self.harness("configure_fsp_validation.py")
                source = harness.root / "fixture.c"
                source.write_text("void hal_entry(void) {}\n")
                (harness.root / "src").mkdir()
                spec = harness.root / "spec.json"
                spec.write_text(json.dumps({"modules": [{"id": "module.fixture"}], "source": source.name}))
                harness.run(extra_args=["--spec", str(spec)], argv_type=argv_type)
                self.assertEqual((harness.root / "src/hal_entry.c").read_bytes(), source.read_bytes())
                self.assertIn("ADDED_STACK module.fixture module.fixture.1000", harness.output.getvalue())
                self.assertIn("APPLICATION_FIXTURE " + str(source.resolve()), harness.output.getvalue())
                self.assertEqual(harness.calls, [BuildKind.CLEAN_BUILD, BuildKind.FULL_BUILD])

    def module_spec(self, harness, modules):
        spec = harness.root / "modules.json"
        spec.write_text(json.dumps({"modules": modules}))
        return ["--spec", str(spec)]

    def test_saved_pdm_stack_is_reused_and_properties_are_updated(self):
        module_id = "module.driver.pdm_on_pdm"
        name_id = "module.driver.pdm.name"
        existing = stack_fixture(module_id, 1172195241, {name_id: "g_pdm0",
                                                      "module.driver.pdm.channel": "0"})
        harness = self.harness("configure_fsp_validation.py", stacks=[existing],
                               creatable_module_ids=[module_id])
        args = self.module_spec(harness, [{"id": module_id, "properties": {
            name_id: "g_pdm0", "module.driver.pdm.channel": "2"}}])
        harness.run(extra_args=args)
        harness.thread.createStack.assert_not_called()
        existing.getProperty("module.driver.pdm.channel").setValue.assert_called_once_with("2")
        self.assertIn("REUSED_STACK " + module_id + " " + module_id + ".1172195241",
                      harness.output.getvalue())
        self.assertEqual(harness.stacks, [existing])
        self.assertEqual(harness.events, ["generate-content", "build", "build"])

    def test_failed_build_retry_does_not_duplicate_named_stack(self):
        harness = self.harness("configure_fsp_validation.py",
                               logs={1: "source.c:1: error: failed build\n"})
        args = self.module_spec(harness, [{"id": "module.fixture", "properties": {
            "module.fixture.name": "g_fixture0"}}])
        with self.assertRaisesRegex(RuntimeError, "Build failed"):
            harness.run(extra_args=args)
        harness.configuration.save.assert_called_once_with()
        self.assertEqual(len(harness.stacks), 1)
        original = harness.stacks[0]
        harness.logs.clear()
        harness.calls.clear()
        harness.run(extra_args=args)
        harness.thread.createStack.assert_called_once_with("module.fixture")
        self.assertEqual(harness.stacks, [original])
        self.assertIn("REUSED_STACK module.fixture " + original.getId(), harness.output.getvalue())
        self.assertEqual(harness.calls, [BuildKind.CLEAN_BUILD, BuildKind.FULL_BUILD])

    def test_matching_requires_exact_module_type_and_requested_name(self):
        for other_type, other_name in (("module.fixture_on_other", "g_fixture0"),
                                       ("module.fixture.extra", "g_fixture0"),
                                       ("module.fixture", "g_fixture1"),
                                       ("module.fixture", None)):
            with self.subTest(other_type=other_type, other_name=other_name):
                properties = {} if other_name is None else {"module.fixture.name": other_name}
                existing = stack_fixture(other_type, 1, properties)
                harness = self.harness("configure_fsp_validation.py", stacks=[existing])
                args = self.module_spec(harness, [{"id": "module.fixture", "properties": {
                    "module.fixture.name": "g_fixture0"}}])
                harness.run(extra_args=args)
                harness.thread.createStack.assert_called_once_with("module.fixture")
                self.assertEqual(len(harness.stacks), 2)
                existing.getProperty.assert_not_called()

    def test_distinct_requested_names_keep_independent_instances_on_retry(self):
        harness = self.harness("configure_fsp_validation.py")
        args = self.module_spec(harness, [
            {"id": "module.fixture", "properties": {"module.fixture.name": name}}
            for name in ("g_fixture0", "g_fixture1")])
        harness.run(extra_args=args)
        self.assertEqual(harness.thread.createStack.call_count, 2)
        self.assertEqual([stack.getProperty("module.fixture.name").getValue()
                          for stack in harness.stacks], ["g_fixture0", "g_fixture1"])
        harness.calls.clear()
        harness.run(extra_args=args)
        self.assertEqual(harness.thread.createStack.call_count, 2)
        self.assertEqual(len(harness.stacks), 2)
        self.assertEqual(harness.output.getvalue().count("REUSED_STACK"), 2)

    def test_multiple_existing_exact_matches_are_rejected_without_mutation(self):
        stacks = [stack_fixture("module.fixture", index, {"module.fixture.name": "g_fixture0"})
                  for index in (1, 2)]
        harness = self.harness("configure_fsp_validation.py", stacks=stacks)
        args = self.module_spec(harness, [{"id": "module.fixture", "properties": {
            "module.fixture.name": "g_fixture0"}}])
        with self.assertRaisesRegex(RuntimeError, "Ambiguous existing stacks"):
            harness.run(extra_args=args)
        harness.thread.createStack.assert_not_called()
        harness.configuration.save.assert_not_called()
        for stack in stacks:
            stack.getProperty.assert_not_called()
        self.assertEqual(harness.events, [])

    def test_existing_match_need_not_be_creatable_again(self):
        existing = stack_fixture("module.fixture", 1, {"module.fixture.name": "g_fixture0"})
        harness = self.harness("configure_fsp_validation.py", stacks=[existing], creatable_module_ids=[])
        args = self.module_spec(harness, [{"id": "module.fixture", "properties": {
            "module.fixture.name": "g_fixture0"}}])
        harness.run(extra_args=args)
        harness.thread.createStack.assert_not_called()
        self.assertEqual(harness.stacks, [existing])

    def test_unavailable_new_module_is_rejected(self):
        harness = self.harness("configure_fsp_validation.py", creatable_module_ids=[])
        args = self.module_spec(harness, [{"id": "module.fixture", "properties": {
            "module.fixture.name": "g_fixture0"}}])
        with self.assertRaisesRegex(ValueError, "Stack is unavailable"):
            harness.run(extra_args=args)
        harness.thread.createStack.assert_not_called()
        self.assertEqual(harness.events, [])

    def test_unnamed_requests_do_not_guess_which_existing_stack_to_reuse(self):
        existing = stack_fixture("module.fixture", 1, {"module.fixture.name": "g_fixture0"})
        harness = self.harness("configure_fsp_validation.py", stacks=[existing])
        harness.run()
        harness.thread.createStack.assert_called_once_with("module.fixture")
        self.assertEqual(len(harness.stacks), 2)
        existing.getProperty.assert_not_called()

    def test_ambiguous_requested_name_properties_are_rejected(self):
        harness = self.harness("configure_fsp_validation.py")
        args = self.module_spec(harness, [{"id": "module.fixture", "properties": {
            "module.fixture.name": "g_fixture0", "module.other.name": "g_other"}}])
        with self.assertRaisesRegex(ValueError, "Ambiguous requested module name"):
            harness.run(extra_args=args)
        harness.thread.createStack.assert_not_called()
        self.assertEqual(harness.events, [])

    def test_invalid_spec_arguments_are_rejected_before_build(self):
        for argv_type in (tuple, list):
            for arguments in (["--spec"], ["--spec", "relative.json"],
                              ["--spec", "/spec.json", "unexpected"]):
                with self.subTest(argv_type=argv_type.__name__, arguments=arguments):
                    harness = self.harness("configure_fsp_validation.py")
                    with self.assertRaisesRegex(ValueError, "--spec requires one absolute JSON path"):
                        harness.run(extra_args=arguments, argv_type=argv_type)
                    self.assertEqual(harness.events, [])
                    self.assertEqual(harness.calls, [])

    def pin_harness(self, tables, model_names):
        models = []
        for name in model_names:
            model = Mock()
            model.getName.return_value = name
            component = model.getComponent.return_value
            component.getConfig.return_value.getOptions.return_value = ["output"]
            component.getPin.return_value.getOptions.return_value = ["P303"]
            models.append(model)
        harness = self.harness("configure_fsp_validation.py", pin_configurations=models)
        root = ET.Element("raConfiguration")
        configurations = ET.SubElement(root, "raPinConfiguration")
        for attributes in tables:
            ET.SubElement(configurations, "pincfg", attributes)
        ET.ElementTree(root).write(harness.root / "configuration.xml")
        spec = harness.root / "pins.json"
        spec.write_text(json.dumps({"pins": [{"component": "port",
                                             "configs": {"mode": "output"},
                                             "connections": {"signal": "P303"}}]}))
        return harness, models, spec

    def assert_pin_selection_rejected(self, tables, model_names, message):
        harness, models, spec = self.pin_harness(tables, model_names)
        with self.assertRaisesRegex(RuntimeError, message):
            harness.run(extra_args=["--spec", str(spec)])
        for model in models:
            model.getComponent.assert_not_called()
        harness.configuration.save.assert_not_called()
        self.assertEqual(harness.events, [])
        self.assertEqual(harness.calls, [])

    def test_active_pin_table_selected_with_inactive_sibling(self):
        tables = [{"name": "device_default", "active": "false", "symbol": "g_device_pins"},
                  {"name": "board_pins", "active": "true", "symbol": "g_board_pins"}]
        for names in (["device_default", "board_pins"], ["board_pins", "device_default"]):
            with self.subTest(model_names=names):
                harness, models, spec = self.pin_harness(tables, names)
                harness.run(extra_args=["--spec", str(spec)])
                active = models[names.index("board_pins")]
                inactive = models[names.index("device_default")]
                active.getComponent.assert_called_once_with("port")
                component = active.getComponent.return_value
                component.getConfig.assert_called_once_with("mode")
                component.getConfig.return_value.setValue.assert_called_once_with("output")
                component.getPin.assert_called_once_with("signal")
                component.getPin.return_value.setValue.assert_called_once_with("P303")
                inactive.getComponent.assert_not_called()
                harness.configuration.save.assert_called_once_with()
                self.assertEqual(harness.events, ["generate-content", "build", "build"])
                self.assertIn("SET_PIN port", harness.output.getvalue())

    def test_missing_active_named_pin_table_is_rejected(self):
        for tables in ([],
                       [{"name": "board_pins", "active": "false", "symbol": "g_board_pins"}],
                       [{"name": "board_pins", "active": "true", "symbol": ""}],
                       [{"name": "board_pins", "active": "true"}]):
            with self.subTest(tables=tables):
                self.assert_pin_selection_rejected(tables, ["board_pins"],
                                                   "Expected exactly one active named pin configuration")

    def test_ambiguous_active_pin_tables_are_rejected(self):
        for second_name in ("other_pins", "board_pins"):
            with self.subTest(second_name=second_name):
                tables = [{"name": "board_pins", "active": "true", "symbol": "g_board_pins"},
                          {"name": second_name, "active": "true", "symbol": "g_other_pins"}]
                self.assert_pin_selection_rejected(tables, ["board_pins", "other_pins"],
                                                   "Expected exactly one active named pin configuration")

    def test_active_pin_model_must_be_uniquely_available(self):
        tables = [{"name": "board_pins", "active": "true", "symbol": "g_board_pins"}]
        for names in ([], ["device_default"], ["board_pins", "board_pins"]):
            with self.subTest(model_names=names):
                self.assert_pin_selection_rejected(tables, names,
                                                   "Active pin configuration is not uniquely available in the model")

    def test_clean_must_remove_previous_elf(self):
        for script, steps in SCRIPTS.items():
            clean = next(index for index, (kind, _) in enumerate(steps) if kind is BuildKind.CLEAN_BUILD)
            with self.subTest(script=script):
                harness = self.harness(script, artifacts={clean: "keep"})
                with self.assertRaisesRegex(RuntimeError, "Clean left the previous ELF"):
                    harness.run()
                self.assertEqual(len(harness.calls), clean + 1)
                self.assertTrue(harness.elf.exists())

    def test_explicit_build_failures_are_rejected_at_every_step(self):
        failures = (
            "source.c:17: error: undeclared identifier\n" + ZERO_ERRORS,
            "make: *** No rule to make target 'clean'. Stop.\n" + ZERO_ERRORS,
        )
        for script, steps in SCRIPTS.items():
            for index in range(len(steps)):
                for text in failures:
                    with self.subTest(script=script, step=index, log=text):
                        harness = self.harness(script, logs={index: text})
                        with self.assertRaisesRegex(RuntimeError, "Build failed"):
                            harness.run()
                        self.assertEqual(len(harness.calls), index + 1)

    def test_every_full_build_requires_zero_error_summary(self):
        for script, steps in SCRIPTS.items():
            for index, (kind, _) in enumerate(steps):
                if kind is not BuildKind.FULL_BUILD:
                    continue
                for text in ("", "Build Finished.\n", "1 errors, 0 warnings.\n", "10 errors, 0 warnings.\n"):
                    with self.subTest(script=script, step=index, log=text):
                        harness = self.harness(script, logs={index: text})
                        with self.assertRaisesRegex(RuntimeError, "did not finish with zero errors"):
                            harness.run()
                        self.assertEqual(len(harness.calls), index + 1)

    def test_missing_logs_cannot_reuse_previous_success(self):
        for script, steps in SCRIPTS.items():
            for index, (_, filename) in enumerate(steps):
                with self.subTest(script=script, step=index):
                    harness = self.harness(script, logs={index: None})
                    with self.assertRaises(FileNotFoundError):
                        harness.run()
                    self.assertEqual(len(harness.calls), index + 1)
                    self.assertFalse((harness.root / filename).exists())

    def test_final_build_must_produce_a_fresh_artifact(self):
        for script, steps in SCRIPTS.items():
            for action in ("remove", "stale"):
                with self.subTest(script=script, artifact=action):
                    harness = self.harness(script, artifacts={len(steps) - 1: action})
                    with self.assertRaisesRegex(RuntimeError, "fresh.*artifact"):
                        harness.run()
                    self.assertEqual(len(harness.calls), len(steps))
                    self.assertNotIn("BUILD_ARTIFACT ", harness.output.getvalue())


if __name__ == "__main__":
    unittest.main()
