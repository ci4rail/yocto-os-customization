import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from os_customization.manager import CustomizationError, CustomizationManager


class SystemdValidationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("systemd-analyze"), "requires systemd-analyze")
    def test_systemd_resolves_absolute_executable_against_validation_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "etc/bin/os-customization-validation-example"
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
            unit = root / "etc/systemd/system/os-customization-validation-example.service"
            unit.parent.mkdir(parents=True)
            unit.write_text(
                "[Unit]\nDefaultDependencies=no\n[Service]\n"
                "ExecStart=/etc/bin/os-customization-validation-example\n"
            )
            original = subprocess.run(
                ["systemd-analyze", "verify", str(unit)], capture_output=True, text=True
            )
            self.assertNotEqual(original.returncode, 0)
            self.assertIn("/etc/bin/os-customization-validation-example", original.stderr)
            corrected = subprocess.run(
                ["systemd-analyze", "--root=" + str(root), "--generators=no", "--man=no",
                 "verify", "/etc/systemd/system/" + unit.name], capture_output=True, text=True
            )
            self.assertEqual(corrected.returncode, 0, corrected.stderr)

    def test_validation_failure_includes_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            etc = root / "payload" / "etc"
            etc.mkdir(parents=True)
            manager = CustomizationManager(root=root / "layout")
            with mock.patch("shutil.which", return_value="/usr/bin/unshare"), \
                 mock.patch.dict(os.environ, {"ROOTFS_ETC_BIND": str(etc)}), \
                 mock.patch("subprocess.run", return_value=subprocess.CompletedProcess(
                     [], 1, "", "example.service: Command /etc/bin/example is not executable"
                 )):
                with self.assertRaisesRegex(CustomizationError, "/etc/bin/example is not executable"):
                    manager._verify_systemd_units(etc, ["/etc/systemd/system/example.service"])

    def test_missing_namespace_tool_fails_closed(self):
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaisesRegex(CustomizationError, "requires util-linux unshare"):
                CustomizationManager()._verify_systemd_units(Path("/unused"), ["example.service"])


class SystemdValidationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not all(shutil.which(tool) for tool in ("unshare", "mount", "systemd-analyze")):
            raise unittest.SkipTest("requires unshare, mount and systemd-analyze")
        probe = subprocess.run(
            ["unshare", "--mount", "--propagation", "private", "true"], capture_output=True
        )
        if probe.returncode:
            raise unittest.SkipTest("requires mount namespace privileges")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.etc = self.root / "payload" / "etc"
        (self.etc / "systemd/system").mkdir(parents=True)
        self.base = self.root / "sysroot-etc"
        self.base.mkdir()
        self.manager = CustomizationManager(root=self.root / "layout")
        self.environment = mock.patch.dict(os.environ, {"ROOTFS_ETC_BIND": str(self.base)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def executable(self, layer, mode=0o755):
        path = layer / "bin/os-customization-validation-example"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(mode)
        return path

    def unit(self, executable="/etc/bin/os-customization-validation-example"):
        path = self.etc / "systemd/system/os-customization-validation-example.service"
        path.write_text(
            "[Unit]\nDefaultDependencies=no\n[Service]\nType=oneshot\nExecStart="
            + executable + "\n"
        )
        return path

    def test_candidate_executable_and_absolute_enablement_symlink(self):
        self.executable(self.etc)
        unit = self.unit()
        wants = unit.parent / "multi-user.target.wants"
        wants.mkdir()
        (wants / unit.name).symlink_to("/etc/systemd/system/" + unit.name)
        self.manager._run_builtin_validators(self.etc)
        self.assertFalse(Path("/etc/bin/os-customization-validation-example").exists())

    def test_inherited_factory_and_sysroot_executables(self):
        self.unit()
        for layer in (self.base, self.manager.factory_path / "etc"):
            with self.subTest(layer=layer):
                executable = self.executable(layer)
                self.manager._run_builtin_validators(self.etc)
                executable.unlink()

    def test_system_executable_remains_available(self):
        self.unit(shutil.which("true"))
        self.manager._run_builtin_validators(self.etc)

    def test_missing_and_nonexecutable_candidate_are_rejected(self):
        self.unit()
        with self.assertRaisesRegex(CustomizationError, "not executable"):
            self.manager._run_builtin_validators(self.etc)
        self.executable(self.etc, 0o644)
        with self.assertRaisesRegex(CustomizationError, "not executable"):
            self.manager._run_builtin_validators(self.etc)

    def test_candidate_overrides_factory_and_state_has_highest_precedence(self):
        self.unit()
        self.executable(self.manager.factory_path / "etc", 0o644)
        self.executable(self.etc)
        self.manager._run_builtin_validators(self.etc)
        self.executable(self.manager.state_etc_path, 0o644)
        with self.assertRaisesRegex(CustomizationError, "not executable"):
            self.manager._run_builtin_validators(self.etc)


if __name__ == "__main__":
    unittest.main()
