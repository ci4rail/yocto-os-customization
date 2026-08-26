import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from os_customization.manager import CustomizationError, CustomizationManager


class CoreOsCompatibilityTests(unittest.TestCase):
    def _payload(self, root: Path, compatible_core_os: str) -> Path:
        payload = root / "payload"
        (payload / "etc").mkdir(parents=True)
        (payload / "manifest.json").write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "version": "test",
                    "compatible_core_os": compatible_core_os,
                }
            ),
            encoding="utf-8",
        )
        return payload

    def test_reads_semantic_version_from_image_identifier(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            issue = root / "issue"
            issue.write_text(
                "TDX Wayland with XWayland 7.1.0-devel\\n"
                "Moducop-CPU01_Standard-Image_v2.11.0.51609d8.20260513.1047\\n",
                encoding="utf-8",
            )
            manager = CustomizationManager(root=root / "state", issue_path=issue)

            self.assertEqual(manager.current_core_os_version(), "v2.11.0")

    def test_rejects_incompatible_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            issue = root / "issue"
            issue.write_text("Moducop-CPU01_Standard-Image_v2.11.0.build\\n", encoding="utf-8")
            manager = CustomizationManager(root=root / "state", issue_path=issue)

            with self.assertRaisesRegex(CustomizationError, "current_core_os=v2.11.0"):
                manager.validate_payload(self._payload(root, "v2.12.0"))

    def test_accepts_matching_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            issue = root / "issue"
            issue.write_text("Moducop-CPU01_Standard-Image_v2.11.0.build\\n", encoding="utf-8")
            manager = CustomizationManager(root=root / "state", issue_path=issue)

            manager.validate_payload(self._payload(root, "v2.11.0"))

    def test_accepts_payload_with_compatible_core_os_range(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            issue = root / "issue"
            issue.write_text("Moducop-CPU01_Standard-Image_v2.11.0.build\\n", encoding="utf-8")
            manager = CustomizationManager(root=root / "state", issue_path=issue)

            manager.validate_payload(self._payload(root, ">=2.11,<3.0"))

    def test_rejects_payload_outside_compatible_core_os_range(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            issue = root / "issue"
            issue.write_text("Moducop-CPU01_Standard-Image_v2.11.0.build\\n", encoding="utf-8")
            manager = CustomizationManager(root=root / "state", issue_path=issue)

            with self.assertRaisesRegex(CustomizationError, "current_core_os=v2.11.0"):
                manager.validate_payload(self._payload(root, ">=2.12,<3.0"))

    def test_rejects_malformed_compatible_core_os_range(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            issue = root / "issue"
            issue.write_text("Moducop-CPU01_Standard-Image_v2.11.0.build\\n", encoding="utf-8")
            manager = CustomizationManager(root=root / "state", issue_path=issue)

            with self.assertRaisesRegex(CustomizationError, "invalid compatible_core_os range"):
                manager.validate_payload(self._payload(root, ">=2.11 || <3.0"))

    def test_rejects_constrained_payload_when_version_cannot_be_read(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manager = CustomizationManager(root=root / "state", issue_path=root / "missing-issue")

            with self.assertRaisesRegex(CustomizationError, "cannot determine current Core OS version"):
                manager.validate_payload(self._payload(root, "v2.11.0"))

    def test_boot_excludes_incompatible_candidate_and_uses_compatible_last_good(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            issue = root / "issue"
            issue.write_text("Moducop-CPU01_Standard-Image_v3.0.0.build\n", encoding="utf-8")
            manager = CustomizationManager(root=root / "state", issue_path=issue)
            manager.ensure_layout()
            manager.slot("A").manifest_path.write_text(
                json.dumps({"version": "last-good", "compatible_core_os": ">=3.0,<4.0"}),
                encoding="utf-8",
            )
            manager.slot("B").manifest_path.write_text(
                json.dumps({"version": "candidate", "compatible_core_os": ">=2.0,<3.0"}),
                encoding="utf-8",
            )
            status = manager.read_status()
            status.update({"active_slot": "A", "last_good_slot": "A", "candidate_slot": "B"})
            manager.write_status(status)

            result = manager.boot_prepare()

            self.assertEqual(result["selected_slot"], "A")
            self.assertEqual(
                result["selection_reason"],
                "candidate-incompatible-core-os; last-good-compatible",
            )
            self.assertEqual(result["status"]["candidate_state"], "incompatible-core-os")
            self.assertIsNone(result["status"]["candidate_slot"])
            self.assertTrue(manager.slot("B").manifest_path.exists())

    def test_boot_excludes_incompatible_last_good_and_factory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            issue = root / "issue"
            issue.write_text("Moducop-CPU01_Standard-Image_v3.0.0.build\n", encoding="utf-8")
            manager = CustomizationManager(root=root / "state", issue_path=issue)
            manager.ensure_layout()
            manager.slot("A").manifest_path.write_text(
                json.dumps({"version": "last-good", "compatible_core_os": "<3.0"}), encoding="utf-8"
            )
            (manager.factory_path / "manifest.json").write_text(
                json.dumps({"version": "factory", "compatible_core_os": "<3.0"}), encoding="utf-8"
            )
            status = manager.read_status()
            status.update({"active_slot": "A", "last_good_slot": "A"})
            manager.write_status(status)

            result = manager.boot_prepare()

            self.assertIsNone(result["selected_slot"])
            self.assertEqual(result["factory_path"], "")
            self.assertEqual(
                result["selection_reason"],
                "last-good-incompatible-core-os; factory-incompatible-core-os",
            )


class StatusVersionTests(unittest.TestCase):
    def test_status_reports_versions_for_active_and_last_good_slots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "state"
            manager = CustomizationManager(root=root)
            manager.ensure_layout()
            (manager.slot("A").manifest_path).write_text(
                json.dumps({"version": "user-a-1.0.0"}), encoding="utf-8"
            )
            (manager.slot("B").manifest_path).write_text(
                json.dumps({"version": "user-b-2.0.0"}), encoding="utf-8"
            )
            (manager.factory_path / "manifest.json").write_text(
                json.dumps({"version": "factory-3.0.0"}), encoding="utf-8"
            )
            status = manager.default_status()
            status["active_slot"] = "B"
            status["last_good_slot"] = "A"
            manager.write_status(status)

            observed = manager.read_status()

            self.assertEqual(observed["active_version"], "user-b-2.0.0")
            self.assertEqual(observed["last_good_version"], "user-a-1.0.0")
            self.assertEqual(observed["factory_version"], "factory-3.0.0")

    def test_commit_returns_updated_active_and_last_good_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "state"
            manager = CustomizationManager(root=root)
            manager.ensure_layout()
            (manager.slot("A").manifest_path).write_text(
                json.dumps({"version": "candidate-1.0.0"}), encoding="utf-8"
            )
            status = manager.default_status()
            status["candidate_slot"] = "A"
            manager.write_status(status)

            committed = manager.commit()

            self.assertEqual(committed["active_version"], "candidate-1.0.0")
            self.assertEqual(committed["last_good_version"], "candidate-1.0.0")

    def test_rollback_first_candidate_falls_back_to_factory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = CustomizationManager(root=Path(temp_dir) / "state")
            manager.ensure_layout()
            status = manager.read_status()
            status["candidate_slot"] = "A"
            status["candidate_version"] = "first-candidate-1.0.0"
            status["candidate_state"] = "pending"
            manager.write_status(status)

            rolled_back = manager.rollback(reason="health check #1 failed")

            self.assertIsNone(rolled_back["active_slot"])
            self.assertIsNone(rolled_back["last_good_slot"])
            self.assertIsNone(rolled_back["candidate_slot"])
            self.assertEqual(rolled_back["candidate_state"], "rolled-back")
            self.assertEqual(rolled_back["rollback_reason"], "health check #1 failed")


class FactoryInstallTests(unittest.TestCase):
    def test_install_factory_applies_whiteouts_without_user_payload_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = root / "factory-payload"
            (payload / "etc").mkdir(parents=True)
            (payload / "etc" / "chrony.conf").write_text("pool example.test\\n", encoding="utf-8")
            (payload / "manifest.json").write_text(
                json.dumps(
                    {
                        "format_version": 999,
                        "version": "factory-1.0.0",
                        "compatible_core_os": ">=99.0",
                        "health_checks": [{"type": "unsupported"}],
                    }
                ),
                encoding="utf-8",
            )
            (payload / "whiteouts.txt").write_text("/etc/obsolete.conf\n", encoding="utf-8")
            manager = CustomizationManager(root=root / "state", issue_path=root / "missing-issue")

            expected_marker = manager.staging_root / "factory.tmp" / "etc" / ".wh.obsolete.conf"
            with mock.patch("os_customization.manager.os.mknod") as mknod:
                result = manager.install_factory_payload(payload)

            self.assertEqual(result["version"], "factory-1.0.0")
            self.assertEqual(result["status"]["factory_version"], "factory-1.0.0")
            self.assertTrue((manager.factory_path / "etc" / "chrony.conf").is_file())
            mknod.assert_called_once_with(expected_marker, stat.S_IFCHR | 0o000, os.makedev(0, 0))


class PowerCutRecoveryTests(unittest.TestCase):
    def test_interrupted_factory_replacement_restores_previous_factory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "state"
            manager = CustomizationManager(root=root)
            manager.ensure_layout()
            (manager.factory_path / "etc" / "marker").write_text("old", encoding="utf-8")
            backup = manager.staging_root / "factory.backup"
            os.replace(manager.factory_path, backup)
            (manager.factory_path / "etc").mkdir(parents=True)
            (manager.factory_path / "etc" / "marker").write_text("new", encoding="utf-8")
            manager._write_transaction(
                {"kind": "factory-install", "temporary": "factory.tmp", "backup": "factory.backup"}
            )

            recovered = CustomizationManager(root=root)
            recovered.ensure_layout()

            self.assertEqual((recovered.factory_path / "etc" / "marker").read_text(encoding="utf-8"), "old")
            self.assertFalse(recovered.transaction_path.exists())

    def test_interrupted_precommit_state_wipe_is_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "state"
            manager = CustomizationManager(root=root)
            manager.ensure_layout()
            (manager.state_etc_path / "marker").write_text("old", encoding="utf-8")
            backup = manager.staging_root / "state.reset.backup"
            os.replace(root / "state", backup)
            manager.state_etc_path.mkdir(parents=True)
            manager._write_transaction(
                {"kind": "factory-reset", "phase": "prepared", "backup": "state.reset.backup"}
            )

            recovered = CustomizationManager(root=root)
            recovered.ensure_layout()

            self.assertEqual((recovered.state_etc_path / "marker").read_text(encoding="utf-8"), "old")
            self.assertFalse(recovered.transaction_path.exists())

    def test_interrupted_committed_reset_is_completed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "state"
            manager = CustomizationManager(root=root)
            manager.ensure_layout()
            (manager.state_etc_path / "marker").write_text("old", encoding="utf-8")
            status = manager.read_status()
            status.update({"active_slot": "A", "last_good_slot": "A"})
            manager.write_status(status)
            backup = manager.staging_root / "state.reset.backup"
            os.replace(root / "state", backup)
            manager.state_etc_path.mkdir(parents=True)
            manager._write_transaction(
                {"kind": "factory-reset", "phase": "commit-decided", "backup": "state.reset.backup"}
            )

            recovered = CustomizationManager(root=root)
            recovered.ensure_layout()

            self.assertFalse((recovered.state_etc_path / "marker").exists())
            self.assertIsNone(recovered.read_status()["active_slot"])
            self.assertFalse(recovered.transaction_path.exists())

    def test_candidate_gets_exactly_maximum_number_of_boot_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = CustomizationManager(root=Path(temp_dir) / "state", max_attempts=3)
            manager.ensure_layout()
            status = manager.read_status()
            status["candidate_slot"] = "A"
            manager.write_status(status)

            self.assertEqual(manager.boot_prepare()["selected_slot"], "A")
            self.assertEqual(manager.boot_prepare()["selected_slot"], "A")
            self.assertEqual(manager.boot_prepare()["selected_slot"], "A")
            self.assertIsNone(manager.boot_prepare()["selected_slot"])


if __name__ == "__main__":
    unittest.main()
