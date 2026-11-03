"""Host-model tests of generic EASE orchestration; live IDE evidence is separate."""
import contextlib
from enum import IntEnum
import io
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

import fsp_host as host


class BuildKind(IntEnum):
    FULL = 6
    CLEAN = 15


class Harness:
    def __init__(self, root, core="CM4"):
        self.root, self.core = root, core
        self.name = "example"
        self.events = []
        self.settings = {"Core": core, "#TargetName#": "EXAMPLE_PART", "#FSPVersion#": "5.9.0",
                         "#DeviceCommand#": "EXAMPLE_CPU0"}
        self.write_settings()
        self.loader = Mock()
        self.loader.isInitialised.return_value = True
        settings = self.loader.getConfigurationSettings.return_value
        settings.getSettingString.side_effect = self.settings.get
        settings.putSetting.side_effect = lambda key, value: self.settings.__setitem__(key, value)
        self.loader.save.side_effect = self.write_settings
        self.api = {"com": Mock(), "org": Mock(), "loadModule": Mock(),
                    "FULL_BUILD": BuildKind.FULL, "CLEAN_BUILD": BuildKind.CLEAN,
                    "buildProject": self.build, "generateProjectContent": self.generate,
                    "projectGenerationFor": Mock(), "openDDSCConfiguration": self.open_configuration}
        com, org = self.api["com"], self.api["org"]
        self.workspace_project = org.eclipse.core.resources.ResourcesPlugin.getWorkspace.return_value.getRoot.return_value.getProject.return_value
        self.workspace_project.exists.return_value = True
        self.adapter = org.eclipse.core.runtime.Platform.getAdapterManager.return_value
        self.project = object()
        self.adapter.loadAdapter.return_value = self.project
        com.renesas.cdt.ddsc.contentgen.internal.config.DDSCProjectConfigurationLoader.return_value = self.loader
        self.bundle = org.eclipse.core.runtime.Platform.getBundle.return_value
        self.resolver = self.bundle.loadClass.return_value.newInstance.return_value.getDeviceFamilyIdeSettings.return_value
        self.resolver.getMultiCoreDebugDeviceName.side_effect = lambda base, selected: base + "_" + selected
        self.generation = self.api["projectGenerationFor"].return_value
        self.generation.getAvailableCores.return_value = [core]
        for status in [self.generation.validate.return_value, self.generation.generate.return_value]:
            status.getChildren.return_value = []
            status.getException.return_value = None
            status.getSeverity.return_value = 0
        self.configuration = Mock()
        self.configuration.getProblems.return_value = []
        self.thread = Mock()
        self.thread.isHal.return_value = True
        self.thread.getStacks.return_value = []
        self.thread.getCreatableModuleIds.return_value = ["module.example"]
        self.configuration.getThreads.return_value = [self.thread]
        self.configuration.save.side_effect = lambda: self.events.append("configuration-save")
        self.log_text = None
        self.artifact_mode = "fresh"

    def write_settings(self):
        root = ET.Element("raConfiguration")
        general = ET.SubElement(root, "generalSettings")
        for key, value in self.settings.items():
            ET.SubElement(general, "option", key=key, value=value)
        (self.root / "configuration.xml").write_bytes(ET.tostring(root))

    def generate(self, name, monitor):
        self.events.append("generate")
        assert name == self.name and monitor is None

    def open_configuration(self, name):
        self.events.append("open-configuration")
        return self.configuration

    def build(self, name, kind, filename):
        self.events.append(kind)
        assert kind is BuildKind.FULL or kind is BuildKind.CLEAN
        assert name == self.name
        artifact = self.root / "Debug/example.elf"
        log = Path(filename)
        assert not log.exists(), "Old build log must be removed"
        text = self.log_text if self.log_text is not None else (
            "make clean\n" if kind is BuildKind.CLEAN else "Build Finished. 0 errors, 0 warnings.\n")
        log.write_text(text)
        if kind is BuildKind.CLEAN:
            if self.artifact_mode != "keep":
                artifact.unlink(missing_ok=True)
        elif self.artifact_mode != "missing":
            artifact.parent.mkdir(exist_ok=True)
            artifact.write_bytes(b"host fixture, not firmware")
            if self.artifact_mode == "stale":
                os.utime(artifact, (100, 100))


class HostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.h = Harness(self.root)
        self.stdout = contextlib.redirect_stdout(io.StringIO())
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)

    def test_debugger_uses_bundle_loader_and_readback(self):
        host.prepare_project(self.h.api, "example", self.root,
                             {"#DeviceCommand#": "BASE_CM4"}, "BASE", "CM4")
        self.h.bundle.loadClass.assert_called_once_with(
            "com.renesas.cdt.ra.ddsc.contentgen.idesettings.RaIdeSettingsContributor")
        self.h.adapter.loadAdapter.assert_called_once_with(
            self.h.workspace_project, "com.renesas.cdt.ddsc.project.IDDSCProject")
        self.assertEqual(self.h.settings["#DeviceCommand#"], "BASE_CM4")
        self.h.loader.dispose.assert_called_once()

    def test_wrong_target_or_resolver_stops_before_save(self):
        with self.assertRaisesRegex(RuntimeError, "setting mismatch"):
            host.prepare_project(self.h.api, "example", self.root, {"#TargetName#": "WRONG"})
        self.h.loader.save.assert_not_called()
        self.h.loader.dispose.assert_called_once()
        with self.assertRaisesRegex(RuntimeError, "expected target"):
            host.prepare_project(self.h.api, "example", self.root, {"#DeviceCommand#": "WRONG"}, "BASE")
        self.h.loader.save.assert_not_called()

    def test_unpersisted_setting_fails_readback(self):
        self.h.loader.save.side_effect = None
        with self.assertRaisesRegex(RuntimeError, "Persisted setting"):
            host.prepare_project(self.h.api, "example", self.root, debugger_base="BASE")

    def test_no_debugger_override_does_not_save_the_model(self):
        host.prepare_project(self.h.api, "example", self.root, {"#TargetName#": "EXAMPLE_PART"})
        self.h.loader.save.assert_not_called()
        self.h.bundle.loadClass.assert_not_called()

    def test_actual_exported_build_objects_and_summaryless_clean(self):
        host.build_project(self.h.api, "example", self.root, [
            ("FULL_BUILD", "first.log"), ("CLEAN_BUILD", "clean.log"), ("FULL_BUILD", "last.log")])
        self.assertEqual(self.h.events, [BuildKind.FULL, BuildKind.CLEAN, BuildKind.FULL])

    def test_build_failures_cannot_reuse_old_artifact(self):
        for text in ["Build Finished. 1 errors, 0 warnings.\n", "Traceback: failed\n",
                     "make: No rule to make target x\n", "", "make says nothing\n"]:
            self.h.log_text = text
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                host.build_project(self.h.api, "example", self.root, [("FULL_BUILD", "build.log")])
        self.h.log_text = None
        for mode in ["stale", "missing"]:
            (self.root / "Debug/example.elf").unlink(missing_ok=True)
            self.h.artifact_mode = mode
            with self.subTest(mode=mode), self.assertRaisesRegex(RuntimeError, "fresh build"):
                host.build_project(self.h.api, "example", self.root, [("FULL_BUILD", "build.log")])

    def test_clean_must_remove_previous_elf(self):
        self.h.build("example", BuildKind.FULL, str(self.root / "initial.log"))
        self.h.artifact_mode = "keep"
        with self.assertRaisesRegex(RuntimeError, "Clean left"):
            host.build_project(self.h.api, "example", self.root, [("CLEAN_BUILD", "clean.log")])

    def test_named_stack_reuse_does_not_require_factory_availability(self):
        stack = Mock()
        stack.getId.return_value = "module.example.1"
        prop = Mock()
        prop.getId.return_value = "module.example.name"
        prop.getValue.return_value = "sensor"
        stack.getProperties.return_value = [prop]
        self.h.thread.getStacks.return_value = [stack]
        self.h.thread.getCreatableModuleIds.return_value = []
        self.assertIs(host.select_stack(self.h.thread, "module.example", {"module.example.name": "sensor"}), stack)
        self.h.thread.getStacks.return_value = [stack, stack]
        with self.assertRaisesRegex(RuntimeError, "Ambiguous"):
            host.select_stack(self.h.thread, "module.example", {"module.example.name": "sensor"})

    def test_unavailable_properties_and_stacks_fail(self):
        owner = Mock()
        owner.getProperty.return_value = None
        with self.assertRaisesRegex(ValueError, "Unavailable property"):
            host.set_properties(owner, {"missing": "value"})
        with self.assertRaisesRegex(ValueError, "unavailable"):
            host.select_stack(self.h.thread, "module.missing", {})

    def test_active_pin_table_is_chosen_instead_of_first_table(self):
        root = ET.parse(self.root / "configuration.xml").getroot()
        node = ET.SubElement(root, "raPinConfiguration")
        ET.SubElement(node, "pincfg", name="inactive", active="false", symbol="")
        ET.SubElement(node, "pincfg", name="active", active="true", symbol="pins")
        (self.root / "configuration.xml").write_bytes(ET.tostring(root))
        inactive, active = Mock(), Mock()
        inactive.getName.return_value, active.getName.return_value = "inactive", "active"
        active.getComponent.return_value.getConfig.return_value.getOptions.return_value = ["enabled"]
        self.h.configuration.getPinConfigurations.return_value = [inactive, active]
        host.configure_pins(self.h.configuration, self.root, [{"component": "port", "configs": {"mode": "enabled"}}])
        inactive.getComponent.assert_not_called()
        active.getComponent.return_value.getConfig.return_value.setValue.assert_called_once_with("enabled")

    def test_generation_script_accepts_different_cores_and_no_target_hardcoding(self):
        for core in ["CM4", "CM33"]:
            self.h.core = core
            self.h.settings["Core"] = core
            self.h.write_settings()
            self.h.generation.getAvailableCores.return_value = [core]
            args = [str(Path(__file__).with_name("generate_fsp_validation.py")), "example",
                    str(self.root), "EXAMPLE_BOARD", core, "example-toolchain"]
            with patch.object(sys, "argv", tuple(args)):
                exec(compile(Path(args[0]).read_text(), args[0], "exec"),
                     dict(self.h.api, __name__="__main__", __file__=args[0]))
            self.h.generation.core.assert_called_with(core)

    def test_configure_script_saves_before_generation(self):
        script = Path(__file__).with_name("configure_fsp_validation.py")
        with patch.object(sys, "argv", [str(script), "example", str(self.root)]):
            runpy.run_path(str(script), init_globals=self.h.api)
        self.assertLess(self.h.events.index("configuration-save"), self.h.events.index("generate"))


if __name__ == "__main__":
    unittest.main()
