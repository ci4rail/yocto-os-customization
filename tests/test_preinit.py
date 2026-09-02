import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


PREINIT = Path(__file__).resolve().parents[1] / "sbin" / "os-customization-preinit.sh"


class PreinitCompatibilityTests(unittest.TestCase):
    def test_incompatible_customization_forces_reboot_before_real_init(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            selection_path = root / "boot-selection.json"
            selection_path.write_text("{}", encoding="utf-8")
            preinit_log = root / "preinit.log"
            preinit_log.write_text("", encoding="utf-8")
            reboot_marker = root / "reboot-called"
            real_init_marker = root / "real-init-called"
            manager = root / "manager"
            reboot = root / "reboot"
            real_init = root / "init.real"

            manager.write_text(
                """#!/bin/sh
printf '%s\\n' \\
  \"SELECTED_SLOT=''\" \\
  \"FACTORY_PATH=''\" \\
  \"STATE_ETC_PATH=/unused/state\" \\
  \"STATE_WORK_ETC_PATH=/unused/work\" \\
  \"SELECTION_REASON='last-good-incompatible-core-os'\"
""",
                encoding="utf-8",
            )
            reboot.write_text(f"#!/bin/sh\ntouch {reboot_marker}\n", encoding="utf-8")
            real_init.write_text(f"#!/bin/sh\ntouch {real_init_marker}\n", encoding="utf-8")
            for script in (manager, reboot, real_init):
                script.chmod(script.stat().st_mode | stat.S_IXUSR)

            environment = os.environ | {
                # /proc is already mounted on the test host, so preinit does
                # not attempt a real mount before boot preparation.
                "DATA_MOUNT": "/proc",
                "OS_CUSTOMIZATION_ROOT": str(root / "customization"),
                "BOOT_STATE_DIR": str(root / "boot-state"),
                "BOOT_SELECTION_PATH": str(selection_path),
                "MANAGER": str(manager),
                "REAL_INIT": str(real_init),
                "FALLBACK_REAL_INIT": str(real_init),
                "PREINIT_LOG": str(preinit_log),
                "REBOOT_COMMAND": str(reboot),
            }

            result = subprocess.run(
                ["/bin/sh", str(PREINIT)],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(reboot_marker.exists())
            self.assertFalse(real_init_marker.exists())
            self.assertIn("rebooting for Core OS rollback", preinit_log.read_text())
