#!/usr/bin/env python3

import argparse
import fcntl
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional


STATUS_FILENAME = "status.json"
MANIFEST_FILENAME = "manifest.json"
WHITEOUTS_FILENAME = "whiteouts.txt"
FORMAT_VERSION = 1
DEFAULT_LAYOUT_ROOT = Path("/data/os-customization")
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_ISSUE_PATH = Path("/etc/issue")
CORE_OS_VERSION_PATTERN = re.compile(r"v?(\d+)\.(\d+)(?:\.(\d+))?")
CORE_OS_CONSTRAINT_PATTERN = re.compile(r"(<=|>=|==|=|<|>)?\s*(v?\d+\.\d+(?:\.\d+)?)")
SUPPORTED_REGULAR_FILE_MODES = stat.S_IFREG | stat.S_IFLNK | stat.S_IFDIR


class CustomizationError(RuntimeError):
    pass


@dataclass(frozen=True)
class SlotInfo:
    name: str
    path: Path

    @property
    def etc_path(self) -> Path:
        return self.path / "etc"

    @property
    def manifest_path(self) -> Path:
        return self.path / MANIFEST_FILENAME


class CustomizationManager:
    def __init__(
        self,
        root: Path = DEFAULT_LAYOUT_ROOT,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        issue_path: Path = DEFAULT_ISSUE_PATH,
    ):
        self.root = Path(root)
        self.max_attempts = max_attempts
        self.issue_path = Path(issue_path)
        self._recovering_transaction = False

    @property
    def status_path(self) -> Path:
        return self.root / STATUS_FILENAME

    @property
    def staging_root(self) -> Path:
        return self.root / "staging"

    @property
    def transaction_path(self) -> Path:
        return self.root / "transaction.json"

    @property
    def lock_path(self) -> Path:
        return self.root / ".lifecycle.lock"

    @property
    def factory_path(self) -> Path:
        return self.root / "factory"

    @property
    def state_etc_path(self) -> Path:
        return self.root / "state" / "etc"

    @property
    def state_work_etc_path(self) -> Path:
        return self.root / "state-work" / "etc"

    def slot(self, slot_name: str) -> SlotInfo:
        if slot_name not in {"A", "B"}:
            raise CustomizationError(f"unsupported slot {slot_name!r}")
        return SlotInfo(slot_name, self.root / f"user-{slot_name}")

    def ensure_layout(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.staging_root.mkdir(parents=True, exist_ok=True)
        if not self._recovering_transaction:
            self._recover_transaction()
        self.factory_path.mkdir(parents=True, exist_ok=True)
        (self.factory_path / "etc").mkdir(parents=True, exist_ok=True)
        self.state_etc_path.mkdir(parents=True, exist_ok=True)
        self.state_work_etc_path.mkdir(parents=True, exist_ok=True)
        for slot_name in ("A", "B"):
            self.slot(slot_name).etc_path.mkdir(parents=True, exist_ok=True)
            if not self.slot(slot_name).manifest_path.exists():
                self._write_json_atomic(
                    self.slot(slot_name).manifest_path,
                    {
                        "format_version": FORMAT_VERSION,
                        "version": None,
                        "compatible_core_os": None,
                        "installed": False,
                    },
                )
        if not self.status_path.exists():
            self.write_status(self.default_status())

    @contextmanager
    def operation_lock(self):
        """Serialize lifecycle state transitions across CLI and service callers."""
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def default_status(self) -> dict:
        factory_version = self._load_manifest_version(self.factory_path / MANIFEST_FILENAME)
        return {
            "format_version": FORMAT_VERSION,
            "active_slot": None,
            "active_version": None,
            "last_good_slot": None,
            "last_good_version": None,
            "candidate_slot": None,
            "candidate_version": None,
            "candidate_state": None,
            "candidate_attempts": 0,
            "rollback_reason": None,
            "boot_id_last_seen": None,
            "factory_version": factory_version,
        }

    def read_status(self) -> dict:
        self.ensure_layout()
        with self.status_path.open("r", encoding="utf-8") as handle:
            status = json.load(handle)
        return self._add_status_versions(status)

    def write_status(self, payload: dict) -> None:
        self._add_status_versions(payload)
        self._write_json_atomic(self.status_path, payload)

    def _add_status_versions(self, status: dict) -> dict:
        """Derive reported versions from the current slot manifests."""
        status["active_version"] = self._load_slot_version(status.get("active_slot"))
        status["last_good_version"] = self._load_slot_version(status.get("last_good_slot"))
        status["factory_version"] = self._load_manifest_version(self.factory_path / MANIFEST_FILENAME)
        return status

    def _load_slot_version(self, slot_name: object) -> Optional[str]:
        if slot_name not in {"A", "B"}:
            return None
        return self._load_manifest_version(self.slot(slot_name).manifest_path)

    def inactive_slot(self, status: Optional[dict] = None) -> str:
        status = status or self.read_status()
        active = status.get("active_slot")
        last_good = status.get("last_good_slot")
        candidate = status.get("candidate_slot")
        protected = {slot for slot in (active, last_good, candidate) if slot in {"A", "B"}}
        for slot_name in ("A", "B"):
            if slot_name not in protected:
                return slot_name
        raise CustomizationError("no inactive slot available; active/last-good/candidate consume both user slots")

    def selected_slot(self, status: Optional[dict] = None) -> Optional[str]:
        status = status or self.read_status()
        candidate = status.get("candidate_slot")
        if candidate:
            attempts = int(status.get("candidate_attempts", 0))
            if attempts < self.max_attempts:
                return candidate
        last_good = status.get("last_good_slot")
        if last_good:
            return last_good
        return None

    def boot_prepare(self, boot_id: Optional[str] = None) -> dict:
        status = self.read_status()
        candidate = status.get("candidate_slot")
        selected = None
        selection_reason = "no-user-customization"

        if candidate and int(status.get("candidate_attempts", 0)) >= self.max_attempts:
            status["candidate_state"] = "exhausted"
            status["candidate_slot"] = None
            status["candidate_version"] = None
            status["candidate_attempts"] = 0
            candidate = None

        if candidate:
            compatible, reason = self._slot_is_compatible(candidate)
            if not compatible:
                # This candidate was validated against an earlier Core OS.  It
                # must not prevent a new customization from being installed
                # after an independent Core OS update.
                status["candidate_state"] = "incompatible-core-os"
                status["candidate_slot"] = None
                status["candidate_version"] = None
                status["candidate_attempts"] = 0
                selection_reason = f"candidate-{reason}"
                candidate = None
            else:
                selected = candidate
                selection_reason = "candidate-compatible"
                status["candidate_attempts"] = int(status.get("candidate_attempts", 0)) + 1
                if boot_id:
                    status["boot_id_last_seen"] = boot_id

        if selected is None:
            last_good = status.get("last_good_slot")
            if last_good:
                compatible, reason = self._slot_is_compatible(last_good)
                if compatible:
                    selected = last_good
                    selection_reason = (
                        "last-good-compatible"
                        if selection_reason == "no-user-customization"
                        else f"{selection_reason}; last-good-compatible"
                    )
                else:
                    last_good_reason = f"last-good-{reason}"
                    selection_reason = (
                        last_good_reason
                        if selection_reason == "no-user-customization"
                        else f"{selection_reason}; {last_good_reason}"
                    )

        factory_compatible, factory_reason = self._factory_is_compatible()
        if not factory_compatible:
            selection_reason = f"{selection_reason}; factory-{factory_reason}"

        status["boot_selection_reason"] = selection_reason
        self.write_status(status)
        return {
            "selected_slot": selected,
            "factory_path": str(self.factory_path) if factory_compatible else "",
            "state_etc_path": str(self.state_etc_path),
            "state_work_etc_path": str(self.state_work_etc_path),
            "selection_reason": selection_reason,
            "status": status,
        }

    def boot_prepare_shell(self, boot_id: Optional[str] = None, boot_selection_path: Optional[Path] = None) -> str:
        payload = self.boot_prepare(boot_id=boot_id)
        if boot_selection_path is not None:
            self._write_json_atomic(boot_selection_path, payload)

        shell_values = {
            "SELECTED_SLOT": payload.get("selected_slot") or "",
            "FACTORY_PATH": payload.get("factory_path") or "",
            "STATE_ETC_PATH": payload.get("state_etc_path") or "",
            "STATE_WORK_ETC_PATH": payload.get("state_work_etc_path") or "",
            "SELECTION_REASON": payload.get("selection_reason") or "",
        }
        return "\n".join(f"{key}={shlex.quote(value)}" for key, value in shell_values.items())

    def _slot_is_compatible(self, slot_name: object) -> tuple[bool, str]:
        if slot_name not in {"A", "B"}:
            return False, "invalid-slot"
        return self._manifest_is_compatible(self.slot(slot_name).manifest_path)

    def _factory_is_compatible(self) -> tuple[bool, str]:
        return self._manifest_is_compatible(self.factory_path / MANIFEST_FILENAME)

    def _write_transaction(self, payload: dict) -> None:
        self._write_json_atomic(self.transaction_path, payload)

    def _clear_transaction(self) -> None:
        try:
            self.transaction_path.unlink()
        except FileNotFoundError:
            return
        self._fsync_dir(self.transaction_path.parent)

    def _remove_tree_and_sync(self, path: Path) -> None:
        if path.exists() or path.is_symlink():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            self._fsync_dir(path.parent)

    def _restore_backup(self, live: Path, backup: Path, discard: Path) -> None:
        """Restore a pre-operation tree, preserving it across every rename boundary."""
        if not backup.exists():
            return
        if live.exists():
            self._remove_tree_and_sync(discard)
            os.replace(live, discard)
            self._fsync_dir(live.parent)
        os.replace(backup, live)
        self._fsync_dir(live.parent)
        self._remove_tree_and_sync(discard)

    def _reset_status_payload(self) -> dict:
        status = self.read_status()
        status["active_slot"] = None
        status["last_good_slot"] = None
        status["candidate_slot"] = None
        status["candidate_version"] = None
        status["candidate_state"] = None
        status["candidate_attempts"] = 0
        status["rollback_reason"] = None
        return status

    def _recover_transaction(self) -> None:
        """Complete or roll back an interrupted persistent lifecycle operation.

        Transaction state is written and synced before every destructive rename.
        An interrupted slot/factory replacement rolls back to the old tree; an
        interrupted reset is completed only after its durable commit decision.
        """
        if not self.transaction_path.exists():
            return
        self._recovering_transaction = True
        try:
            try:
                with self.transaction_path.open("r", encoding="utf-8") as handle:
                    transaction = json.load(handle)
            except (OSError, json.JSONDecodeError) as exc:
                raise CustomizationError(f"cannot recover malformed transaction record: {exc}") from exc

            kind = transaction.get("kind")
            if kind in {"user-install", "factory-install"}:
                live = self.slot(transaction["slot"]).path if kind == "user-install" else self.factory_path
                backup = self.staging_root / transaction["backup"]
                temporary = self.staging_root / transaction["temporary"]
                discard = self.staging_root / f"{transaction['temporary']}.recovery-discard"
                # The replacement was not referenced by status yet. Prefer the
                # prior tree even if the cut happened after the new rename.
                self._restore_backup(live, backup, discard)
                self._remove_tree_and_sync(temporary)
                self._clear_transaction()
                return

            if kind == "factory-reset":
                backup = self.staging_root / transaction["backup"]
                phase = transaction.get("phase")
                if phase == "commit-decided":
                    # The operation is now authoritative even if power failed
                    # before status.json was updated.
                    self.write_status(self._reset_status_payload())
                    self._remove_tree_and_sync(backup)
                else:
                    self._restore_backup(
                        self.root / "state", backup, self.staging_root / "state.reset.discard"
                    )
                self._clear_transaction()
                return

            raise CustomizationError(f"cannot recover unknown transaction kind: {kind!r}")
        finally:
            self._recovering_transaction = False

    def _manifest_is_compatible(self, manifest_path: Path) -> tuple[bool, str]:
        """Check an installed manifest without allowing a bad one to block boot."""
        if not manifest_path.exists():
            return True, "no-constraint"
        try:
            with manifest_path.open("r", encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return False, "invalid-manifest"

        constraint = manifest.get("compatible_core_os")
        if constraint is None:
            return True, "no-constraint"
        current_core_os = self.current_core_os_version()
        if not current_core_os:
            return False, "core-os-version-unavailable"
        try:
            compatible = self._core_os_version_matches(constraint, current_core_os)
            return compatible, "compatible" if compatible else "incompatible-core-os"
        except CustomizationError:
            return False, "invalid-compatibility-constraint"

    def validate_payload(self, payload_dir: Path, current_core_os: Optional[str] = None) -> dict:
        payload_dir = Path(payload_dir)
        manifest_path = payload_dir / MANIFEST_FILENAME
        if not manifest_path.exists():
            raise CustomizationError(f"missing manifest: {manifest_path}")
        with manifest_path.open("r", encoding="utf-8") as handle:
            manifest = json.load(handle)

        if manifest.get("format_version") != FORMAT_VERSION:
            raise CustomizationError(f"unsupported format_version: {manifest.get('format_version')}")
        if "version" not in manifest:
            raise CustomizationError("manifest must contain version")

        compatible_core_os = manifest.get("compatible_core_os")
        if compatible_core_os is not None:
            current_core_os = current_core_os or self.current_core_os_version()
            if not current_core_os:
                raise CustomizationError(
                    f"cannot determine current Core OS version from {self.issue_path}"
                )
            if not self._core_os_version_matches(compatible_core_os, current_core_os):
                raise CustomizationError(
                    f"payload expects compatible_core_os={compatible_core_os}, current_core_os={current_core_os}"
                )

        etc_root = payload_dir / "etc"
        if not etc_root.exists() or not etc_root.is_dir():
            raise CustomizationError("payload must contain etc/ directory")

        for path in etc_root.rglob("*"):
            relative = path.relative_to(etc_root)
            self._validate_relative_etc_path(relative)
            self._validate_path_type(path)
            if path.is_symlink():
                self._validate_symlink_target(relative, path)

        whiteouts = self._read_whiteouts(payload_dir / WHITEOUTS_FILENAME)
        for whiteout in whiteouts:
            self._validate_absolute_whiteout_path(whiteout)

        self._run_builtin_validators(etc_root)
        return manifest

    def current_core_os_version(self) -> Optional[str]:
        """Return the Core OS version advertised by the image line in /etc/issue."""
        try:
            issue = self.issue_path.read_text(encoding="utf-8")
        except OSError:
            return None

        # Example: Moducop-CPU01_Standard-Image_v2.11.0.51609d8.20260513.1047
        match = re.search(r"(?:^|_)v(\d+\.\d+\.\d+)(?=[.+\s]|$)", issue)
        if not match:
            return None
        return f"v{match.group(1)}"

    def _core_os_version_matches(self, constraint: object, current_version: str) -> bool:
        if not isinstance(constraint, str):
            raise CustomizationError("compatible_core_os must be a string or null")

        current = self._parse_core_os_version(current_version, "current Core OS version")
        clauses = [clause.strip() for clause in constraint.split(",")]
        if not clauses or any(not clause for clause in clauses):
            raise CustomizationError(f"invalid compatible_core_os range: {constraint!r}")

        for clause in clauses:
            match = CORE_OS_CONSTRAINT_PATTERN.fullmatch(clause)
            if not match:
                raise CustomizationError(f"invalid compatible_core_os range: {constraint!r}")
            operator = match.group(1) or "="
            expected = self._parse_core_os_version(match.group(2), "compatible_core_os constraint")
            if not self._compare_core_os_versions(current, operator, expected):
                return False
        return True

    @staticmethod
    def _parse_core_os_version(version: str, source: str) -> tuple[int, int, int]:
        match = CORE_OS_VERSION_PATTERN.fullmatch(version.strip())
        if not match:
            raise CustomizationError(f"invalid {source}: {version!r}")
        return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))

    @staticmethod
    def _compare_core_os_versions(
        current: tuple[int, int, int], operator: str, expected: tuple[int, int, int]
    ) -> bool:
        comparisons = {
            "=": current == expected,
            "==": current == expected,
            ">": current > expected,
            ">=": current >= expected,
            "<": current < expected,
            "<=": current <= expected,
        }
        return comparisons[operator]

    def install_payload(self, payload_dir: Path, current_core_os: Optional[str] = None) -> dict:
        self.ensure_layout()
        manifest = self.validate_payload(payload_dir, current_core_os=current_core_os)
        status = self.read_status()
        slot_name = self.inactive_slot(status)
        slot = self.slot(slot_name)

        tmp_dir = self.staging_root / f"user-{slot_name}.tmp"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True)
        (tmp_dir / "etc").mkdir()

        self._copy_tree(Path(payload_dir) / "etc", tmp_dir / "etc")
        self._materialize_whiteouts(tmp_dir / "etc", self._read_whiteouts(Path(payload_dir) / WHITEOUTS_FILENAME))
        self._write_json_atomic(tmp_dir / MANIFEST_FILENAME, manifest)
        self._fsync_tree(tmp_dir)

        old_slot_path = slot.path
        backup_slot_path = self.staging_root / f"user-{slot_name}.backup"
        if backup_slot_path.exists():
            shutil.rmtree(backup_slot_path)
            self._fsync_dir(backup_slot_path.parent)
        self._write_transaction(
            {
                "kind": "user-install",
                "slot": slot_name,
                "temporary": tmp_dir.name,
                "backup": backup_slot_path.name,
            }
        )
        if old_slot_path.exists():
            os.replace(old_slot_path, backup_slot_path)
            self._fsync_dir(old_slot_path.parent)
        os.replace(tmp_dir, old_slot_path)
        self._fsync_dir(old_slot_path.parent)
        if backup_slot_path.exists():
            shutil.rmtree(backup_slot_path)
            self._fsync_dir(backup_slot_path.parent)

        # The slot is now complete and durable.  Its replacement transaction
        # is finished before candidate metadata can reference this slot.
        self._clear_transaction()

        status["candidate_slot"] = slot_name
        status["candidate_version"] = manifest.get("version")
        status["candidate_state"] = "pending"
        status["candidate_attempts"] = 0
        status["rollback_reason"] = None
        self.write_status(status)
        return {
            "slot": slot_name,
            "version": manifest.get("version"),
            "status": status,
        }

    def install_factory_payload(self, payload_dir: Path) -> dict:
        """Install a trusted factory set without USER candidate validation or activation."""
        self.ensure_layout()
        payload_dir = Path(payload_dir)
        manifest_path = payload_dir / MANIFEST_FILENAME
        etc_root = payload_dir / "etc"
        if not manifest_path.is_file():
            raise CustomizationError(f"missing factory manifest: {manifest_path}")
        if not etc_root.is_dir():
            raise CustomizationError("factory payload must contain etc/ directory")

        tmp_dir = self.staging_root / "factory.tmp"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True)
        shutil.copy2(manifest_path, tmp_dir / MANIFEST_FILENAME, follow_symlinks=False)
        (tmp_dir / "etc").mkdir()
        self._copy_tree(etc_root, tmp_dir / "etc")
        self._materialize_whiteouts(tmp_dir / "etc", self._read_whiteouts(payload_dir / WHITEOUTS_FILENAME))
        self._fsync_tree(tmp_dir)

        backup_dir = self.staging_root / "factory.backup"
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
            self._fsync_dir(backup_dir.parent)
        self._write_transaction(
            {
                "kind": "factory-install",
                "temporary": tmp_dir.name,
                "backup": backup_dir.name,
            }
        )
        if self.factory_path.exists():
            os.replace(self.factory_path, backup_dir)
            self._fsync_dir(self.factory_path.parent)
        os.replace(tmp_dir, self.factory_path)
        self._fsync_dir(self.factory_path.parent)
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
            self._fsync_dir(backup_dir.parent)

        self._clear_transaction()
        status = self.read_status()
        self.write_status(status)
        return {
            "version": self._load_manifest_version(self.factory_path / MANIFEST_FILENAME),
            "status": status,
        }

    def commit(self) -> dict:
        status = self.read_status()
        candidate_slot = status.get("candidate_slot")
        if not candidate_slot:
            raise CustomizationError("no candidate slot to commit")
        status["active_slot"] = candidate_slot
        status["last_good_slot"] = candidate_slot
        status["candidate_slot"] = None
        status["candidate_version"] = None
        status["candidate_state"] = None
        status["candidate_attempts"] = 0
        self.write_status(status)
        return status

    def get_effective_manifest(self, slot_name: Optional[str]) -> dict:
        if not slot_name:
            return {}
        manifest_path = self.slot(slot_name).manifest_path
        if not manifest_path.exists():
            return {}
        with manifest_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def run_health_checks(self, slot_name: Optional[str] = None) -> dict:
        status = self.read_status()
        slot_name = slot_name or status.get("candidate_slot") or self.selected_slot(status)
        if not slot_name:
            return {"slot": None, "checks": [], "passed": True}

        manifest = self.get_effective_manifest(slot_name)
        checks = manifest.get("health_checks", [])
        results = []
        for index, check in enumerate(checks, start=1):
            results.append(self._run_one_health_check(index, check))
        passed = all(item["passed"] for item in results)
        return {"slot": slot_name, "checks": results, "passed": passed}

    def rollback(self, reason: Optional[str] = None) -> dict:
        status = self.read_status()
        # A first-ever USER candidate has no last-known-good USER slot.  Its
        # safe rollback target is FACTORY plus SYSROOT, represented by no USER
        # selection.  This also lets the health-check service recover the
        # first candidate without needing Mender-specific handling.
        status["active_slot"] = status.get("last_good_slot")
        status["candidate_slot"] = None
        status["candidate_version"] = None
        status["candidate_state"] = "rolled-back"
        status["candidate_attempts"] = 0
        status["rollback_reason"] = reason or "manual rollback requested"
        self.write_status(status)
        return status

    def candidate_activation_matches(self, boot_selection_path: Path) -> tuple[bool, str]:
        status = self.read_status()
        candidate_slot = status.get("candidate_slot")
        if not candidate_slot:
            return True, "no candidate pending"
        if not boot_selection_path.exists():
            return False, f"missing boot selection marker: {boot_selection_path}"

        with boot_selection_path.open("r", encoding="utf-8") as handle:
            boot_payload = json.load(handle)

        selected_slot = boot_payload.get("selected_slot")
        if selected_slot != candidate_slot:
            return False, f"candidate slot {candidate_slot} was not selected during boot (selected={selected_slot})"
        return True, f"candidate slot {candidate_slot} was selected during boot"

    def factory_reset(self, wipe_state: bool = False) -> dict:
        status = self.read_status()
        backup = self.staging_root / "state.reset.backup"
        if backup.exists():
            self._remove_tree_and_sync(backup)
        self._write_transaction(
            {"kind": "factory-reset", "phase": "prepared", "backup": backup.name}
        )
        if wipe_state and (self.root / "state").exists():
            os.replace(self.root / "state", backup)
            self._fsync_dir(self.root)
            self.state_etc_path.mkdir(parents=True, exist_ok=True)
            self._fsync_dir(self.state_etc_path)
            self._fsync_dir(self.state_etc_path.parent)

        # Once this record is durable recovery must finish the reset, even if
        # power is lost before status.json is replaced.
        self._write_transaction(
            {"kind": "factory-reset", "phase": "commit-decided", "backup": backup.name}
        )
        status = self._reset_status_payload()
        self.write_status(status)
        self._remove_tree_and_sync(backup)
        self._clear_transaction()
        return status

    def _validate_relative_etc_path(self, relative: Path) -> None:
        parts = relative.parts
        if any(part in {"", ".", ".."} for part in parts):
            raise CustomizationError(f"invalid relative path below etc/: {relative}")

    def _validate_absolute_whiteout_path(self, path_str: str) -> None:
        path = Path(path_str)
        if not path.is_absolute():
            raise CustomizationError(f"whiteout entry must be absolute: {path_str}")
        if not str(path).startswith("/etc/") and str(path) != "/etc":
            raise CustomizationError(f"whiteout outside /etc is not allowed: {path_str}")
        normalized = Path(os.path.normpath(str(path)))
        if not str(normalized).startswith("/etc"):
            raise CustomizationError(f"whiteout escapes /etc: {path_str}")

    def _validate_path_type(self, path: Path) -> None:
        mode = path.lstat().st_mode
        if stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
            return
        raise CustomizationError(f"unsupported file type in payload: {path}")

    def _validate_symlink_target(self, relative: Path, path: Path) -> None:
        target = os.readlink(path)
        if os.path.isabs(target):
            normalized = Path(os.path.normpath(target))
        else:
            normalized = Path(os.path.normpath(str((Path("/etc") / relative.parent / target))))
        if not str(normalized).startswith("/etc"):
            raise CustomizationError(f"symlink escapes /etc namespace: {relative} -> {target}")

    def _run_builtin_validators(self, etc_root: Path) -> None:
        validators = [
            (etc_root / "ssh" / "sshd_config", ["sshd", "-t", "-f"]),
            (etc_root / "nftables.conf", ["nft", "-c", "-f"]),
        ]
        for config_path, command in validators:
            if config_path.exists() and shutil.which(command[0]):
                subprocess.run(command + [str(config_path)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        systemd_root = etc_root / "systemd" / "system"
        if systemd_root.exists() and shutil.which("systemd-analyze"):
            units = sorted(str(path) for path in systemd_root.rglob("*.service") if path.is_file())
            if units:
                subprocess.run(
                    ["systemd-analyze", "verify", *units],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

    def _run_one_health_check(self, index: int, check: dict) -> dict:
        check_type = check.get("type")
        if check_type == "command":
            command = check.get("command")
            if not isinstance(command, list) or not command:
                raise CustomizationError(f"health check #{index} must provide a non-empty command list")
            completed = subprocess.run(command, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            return {
                "index": index,
                "type": check_type,
                "passed": completed.returncode == 0,
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        if check_type == "systemd_unit_active":
            unit = check.get("unit")
            if not unit:
                raise CustomizationError(f"health check #{index} must provide a unit")
            completed = subprocess.run(
                ["systemctl", "is-active", unit],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            unit_state = subprocess.run(
                [
                    "systemctl",
                    "show",
                    "--property=LoadState",
                    "--property=UnitFileState",
                    "--property=FragmentPath",
                    "--property=ActiveState",
                    "--property=SubState",
                    "--property=Result",
                    unit,
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            unit_enabled = subprocess.run(
                ["systemctl", "is-enabled", unit],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            return {
                "index": index,
                "type": check_type,
                "unit": unit,
                "passed": completed.returncode == 0,
                "returncode": completed.returncode,
                "stdout": completed.stdout + unit_state.stdout,
                "stderr": completed.stderr + unit_state.stderr,
                "is_enabled_returncode": unit_enabled.returncode,
                "is_enabled_stdout": unit_enabled.stdout,
                "is_enabled_stderr": unit_enabled.stderr,
            }
        if check_type == "path_exists":
            path = check.get("path")
            if not path:
                raise CustomizationError(f"health check #{index} must provide a path")
            exists = Path(path).exists()
            return {
                "index": index,
                "type": check_type,
                "path": path,
                "passed": exists,
                "returncode": 0 if exists else 1,
                "stdout": "",
                "stderr": "",
            }
        raise CustomizationError(f"unsupported health check type: {check_type}")

    def _read_whiteouts(self, whiteouts_path: Path) -> list[str]:
        if not whiteouts_path.exists():
            return []
        with whiteouts_path.open("r", encoding="utf-8") as handle:
            entries = []
            for line in handle:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                entries.append(stripped)
            return entries

    def _materialize_whiteouts(self, etc_root: Path, whiteouts: Iterable[str]) -> None:
        for entry in whiteouts:
            relative = Path(entry).relative_to("/etc")
            self._validate_relative_etc_path(relative)
            marker = etc_root / relative.parent / f".wh.{relative.name}"
            marker.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.mknod(marker, stat.S_IFCHR | 0o000, os.makedev(0, 0))
            except PermissionError as exc:
                raise CustomizationError(
                    f"creating OverlayFS whiteout requires permission to create character devices: {marker}"
                ) from exc
            except OSError as exc:
                raise CustomizationError(f"failed to create OverlayFS whiteout {marker}: {exc}") from exc

    def _copy_tree(self, source: Path, destination: Path) -> None:
        for item in source.iterdir():
            target = destination / item.name
            if item.is_symlink():
                os.symlink(os.readlink(item), target)
            elif item.is_dir():
                target.mkdir(exist_ok=True)
                shutil.copystat(item, target, follow_symlinks=False)
                self._copy_tree(item, target)
            else:
                shutil.copy2(item, target, follow_symlinks=False)

    def _write_json_atomic(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # ``tempfile.mkstemp()`` obtains random bytes for its file name.  This
        # method is called by preinit, before the kernel CSPRNG is necessarily
        # initialized.  The parent directories are root-controlled and all
        # lifecycle operations hold ``operation_lock``, so a deterministic
        # exclusive name is sufficient here.  O_EXCL also safely handles stale
        # temporary files left by a power loss.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = -1
        tmp_path: Optional[Path] = None
        for sequence in range(1024):
            candidate = path.with_name(f".{path.name}.tmp-{os.getpid()}-{sequence}")
            try:
                fd = os.open(candidate, flags, 0o600)
                tmp_path = candidate
                break
            except FileExistsError:
                continue
        if fd < 0 or tmp_path is None:
            raise CustomizationError(f"unable to create atomic temporary file for {path}")

        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
            self._fsync_dir(path.parent)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def _load_manifest_version(self, manifest_path: Path) -> Optional[str]:
        if not manifest_path.exists():
            return None
        with manifest_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
            return payload.get("version")

    def _fsync_tree(self, root: Path) -> None:
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                continue
            if path.is_file():
                with path.open("rb") as handle:
                    os.fsync(handle.fileno())
            elif path.is_dir():
                self._fsync_dir(path)
        self._fsync_dir(root)

    def _fsync_dir(self, path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="os-customization-set")
    parser.add_argument("--root", default=str(DEFAULT_LAYOUT_ROOT), help="persistent customization root")
    parser.add_argument("--max-attempts", default=DEFAULT_MAX_ATTEMPTS, type=int)
    parser.add_argument("--issue-path", default=str(DEFAULT_ISSUE_PATH), help="Core OS /etc/issue path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("directory")
    validate_parser.add_argument("--core-os-version")

    install_parser = subparsers.add_parser("install")
    install_parser.add_argument("directory")
    install_parser.add_argument("--core-os-version")

    install_factory_parser = subparsers.add_parser("install-factory")
    install_factory_parser.add_argument("directory")

    subparsers.add_parser("status")
    subparsers.add_parser("commit")
    subparsers.add_parser("rollback")
    health_parser = subparsers.add_parser("run-health-checks")
    health_parser.add_argument("--slot")

    factory_reset_parser = subparsers.add_parser("factory-reset")
    factory_reset_parser.add_argument("--wipe-state", action="store_true")

    boot_parser = subparsers.add_parser("boot-prepare")
    boot_parser.add_argument("--boot-id")

    boot_shell_parser = subparsers.add_parser("boot-prepare-shell")
    boot_shell_parser.add_argument("--boot-id")
    boot_shell_parser.add_argument("--boot-selection-path")

    activation_parser = subparsers.add_parser("verify-candidate-activation")
    activation_parser.add_argument("--boot-selection-path", default="/run/os-customization/boot-selection.json")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    manager = CustomizationManager(
        root=Path(args.root),
        max_attempts=args.max_attempts,
        issue_path=Path(args.issue_path),
    )
    lifecycle_lock = manager.operation_lock()
    try:
        lifecycle_lock.__enter__()
        if args.command == "validate":
            result = manager.validate_payload(Path(args.directory), current_core_os=args.core_os_version)
        elif args.command == "install":
            result = manager.install_payload(Path(args.directory), current_core_os=args.core_os_version)
        elif args.command == "install-factory":
            result = manager.install_factory_payload(Path(args.directory))
        elif args.command == "status":
            result = manager.read_status()
        elif args.command == "commit":
            result = manager.commit()
        elif args.command == "rollback":
            result = manager.rollback()
        elif args.command == "run-health-checks":
            result = manager.run_health_checks(slot_name=args.slot)
        elif args.command == "factory-reset":
            result = manager.factory_reset(wipe_state=args.wipe_state)
        elif args.command == "boot-prepare":
            result = manager.boot_prepare(boot_id=args.boot_id)
        elif args.command == "boot-prepare-shell":
            boot_selection_path = Path(args.boot_selection_path) if args.boot_selection_path else None
            print(manager.boot_prepare_shell(boot_id=args.boot_id, boot_selection_path=boot_selection_path))
            return 0
        elif args.command == "verify-candidate-activation":
            matched, reason = manager.candidate_activation_matches(Path(args.boot_selection_path))
            result = {"matched": matched, "reason": reason}
            if not matched:
                parser.exit(status=1, message=json.dumps(result) + "\n")
        else:
            raise CustomizationError(f"unsupported command {args.command}")
    except (CustomizationError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        parser.exit(status=1, message=f"error: {exc}\n")
    finally:
        lifecycle_lock.__exit__(None, None, None)

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
