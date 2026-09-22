import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from os_customization.manager import CustomizationError, CustomizationManager


HEALTH_CHECK_HELPER = Path(__file__).resolve().parents[1] / "libexec" / "os-customization-check"


class HealthCheckValidationTests(unittest.TestCase):
    def _payload(self, root: Path, health_checks: object) -> Path:
        payload = root / "payload"
        (payload / "etc").mkdir(parents=True)
        (payload / "manifest.json").write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "version": "test",
                    "compatible_core_os": None,
                    "health_checks": health_checks,
                }
            ),
            encoding="utf-8",
        )
        return payload

    def test_install_rejects_command_string(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = CustomizationManager(root=root / "state")

            with self.assertRaisesRegex(
                CustomizationError,
                "health check #1 must provide a non-empty command list",
            ):
                manager.install_payload(
                    self._payload(root, [{"type": "command", "command": "echo test"}])
                )

            self.assertIsNone(manager.read_status()["candidate_slot"])

    def test_install_accepts_command_argument_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = CustomizationManager(root=root / "state")

            result = manager.install_payload(
                self._payload(root, [{"type": "command", "command": ["echo", "test"]}])
            )

            self.assertEqual(result["status"]["candidate_slot"], "A")

    def test_helper_rolls_back_an_already_installed_invalid_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            customization_root = root / "state"
            manager = CustomizationManager(root=customization_root)
            manager.ensure_layout()
            manager.slot("A").manifest_path.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "version": "test",
                        "health_checks": [{"type": "command", "command": "echo test"}],
                    }
                ),
                encoding="utf-8",
            )
            status = manager.read_status()
            status.update(
                {
                    "candidate_slot": "A",
                    "candidate_version": "test",
                    "candidate_state": "pending",
                }
            )
            manager.write_status(status)
            boot_selection = root / "boot-selection.json"
            boot_selection.write_text(json.dumps({"selected_slot": "A"}), encoding="utf-8")

            environment = os.environ | {
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "python"),
                "OS_CUSTOMIZATION_ROOT": str(customization_root),
                "OS_CUSTOMIZATION_BOOT_SELECTION_PATH": str(boot_selection),
                "OS_CUSTOMIZATION_REBOOT": "0",
            }
            result = subprocess.run(
                [sys.executable, str(HEALTH_CHECK_HELPER)],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            rolled_back = manager.read_status()
            self.assertIsNone(rolled_back["candidate_slot"])
            self.assertEqual(rolled_back["candidate_state"], "rolled-back")
            self.assertIn("invalid health checks", rolled_back["rollback_reason"])
            self.assertIn("health check #1 must provide a non-empty command list", result.stderr)


if __name__ == "__main__":
    unittest.main()
