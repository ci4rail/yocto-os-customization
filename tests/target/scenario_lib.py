#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional


DEFAULT_ROOT = "/data/os-customization"
DEFAULT_REMOTE_PAYLOAD_ROOT = "/tmp/os-customization-target-tests"
DEFAULT_OS_CUSTOMIZATION_SET = "/usr/bin/os-customization-set"
BOOT_SELECTION_PATH = "/run/os-customization/boot-selection.json"
CORE_OS_VERSION_PATTERN = re.compile(r"(?:^|_)v(\d+\.\d+\.\d+)(?=[.+\s]|$)")
DEFAULT_SSH_TIMEOUT = 20.0
DEFAULT_SCP_TIMEOUT = 120.0
DEFAULT_BOOT_PROBE_TIMEOUT = 10.0


class ScenarioError(RuntimeError):
    pass


def _timeout_from_environment(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        timeout = float(value)
    except ValueError as exc:
        raise ScenarioError(f"{name} must be a positive number of seconds") from exc
    if timeout <= 0:
        raise ScenarioError(f"{name} must be a positive number of seconds")
    return timeout


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


class ScenarioLogger:
    def __init__(self, path: Path, target_label: str = "<target>"):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.target_label = target_label
        self._handle = path.open("w", encoding="utf-8")

    def close(self) -> None:
        self._handle.close()

    def _sanitize(self, text: str) -> str:
        target_host = os.environ.get("TARGET_HOST", "")
        target_user = os.environ.get("TARGET_USER", "root")
        sanitized = text
        if target_host:
            sanitized = sanitized.replace(target_host, self.target_label)
            sanitized = sanitized.replace(f"{target_user}@{target_host}", self.target_label)
        password = os.environ.get("TARGET_PASSWORD") or os.environ.get("SSHPASS")
        if password:
            sanitized = sanitized.replace(password, "<redacted>")
        return sanitized

    def write(self, message: str) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = f"[{timestamp}] {self._sanitize(message)}"
        self._handle.write(line)
        if not line.endswith("\n"):
            self._handle.write("\n")
        self._handle.flush()
        sys.stdout.write(line)
        if not line.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()


class TargetContext:
    def __init__(self, scenario_id: str, slug: str, description: str, log_dir: Optional[Path] = None):
        self.scenario_id = scenario_id
        self.slug = slug
        self.description = description
        self.repo_root = Path(__file__).resolve().parents[2]
        self.base_dir = Path(__file__).resolve().parent
        self.log_dir = log_dir or (self.base_dir / "logs")
        self.log_path = self.log_dir / f"{scenario_id}-{slug}.log"
        self.logger = ScenarioLogger(self.log_path)
        self.target_host = os.environ.get("TARGET_HOST", "")
        self.target_user = os.environ.get("TARGET_USER", "root")
        self.target_root = os.environ.get("TARGET_ROOT", DEFAULT_ROOT)
        self.remote_payload_root = os.environ.get("TARGET_REMOTE_PAYLOAD_ROOT", DEFAULT_REMOTE_PAYLOAD_ROOT)
        self.os_customization_set = os.environ.get("TARGET_OS_CUSTOMIZATION_SET", DEFAULT_OS_CUSTOMIZATION_SET)
        self.remote = f"{self.target_user}@{self.target_host}" if self.target_host else ""
        self._temp_dirs: list[tempfile.TemporaryDirectory[str]] = []
        self._common_env = os.environ.copy()
        self._use_sshpass = False
        self.ssh_timeout = _timeout_from_environment("TARGET_SSH_TIMEOUT", DEFAULT_SSH_TIMEOUT)
        self.scp_timeout = _timeout_from_environment("TARGET_SCP_TIMEOUT", DEFAULT_SCP_TIMEOUT)
        self.boot_probe_timeout = _timeout_from_environment(
            "TARGET_BOOT_PROBE_TIMEOUT", DEFAULT_BOOT_PROBE_TIMEOUT
        )
        self._ssh_options = [
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ConnectionAttempts=1",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=1",
            "-o",
            "NumberOfPasswordPrompts=1",
        ]

        password = os.environ.get("TARGET_PASSWORD")
        if password and "SSHPASS" not in self._common_env:
            self._common_env["SSHPASS"] = password
        if self._common_env.get("SSHPASS"):
            if shutil.which("sshpass") is None:
                raise ScenarioError("SSHPASS or TARGET_PASSWORD is set, but sshpass is not installed")
            self._use_sshpass = True

    def close(self) -> None:
        for temp_dir in reversed(self._temp_dirs):
            temp_dir.cleanup()
        self.logger.close()

    def log(self, message: str) -> None:
        self.logger.write(message)

    def _wrap_transport(self, base_command: list[str]) -> list[str]:
        if self._use_sshpass:
            return ["sshpass", "-e", *base_command]
        return base_command

    def _run(self, command: list[str], *, check: bool, label: str, timeout: Optional[float] = None) -> CommandResult:
        self.log(f"COMMAND {label}: {' '.join(shlex.quote(part) for part in command)}")
        process = subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._common_env,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            returncode = process.returncode
        except subprocess.TimeoutExpired:
            self.log(f"TIMEOUT {label}: exceeded {timeout:g}s; terminating command")
            os.killpg(process.pid, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
            returncode = 124
        if stdout:
            self.log(f"STDOUT {label}:\n{stdout}")
        if stderr:
            self.log(f"STDERR {label}:\n{stderr}")
        self.log(f"RESULT {label}: exit={returncode}")
        if check and returncode != 0:
            raise ScenarioError(f"command failed ({label}): exit={returncode}")
        return CommandResult(stdout, stderr, returncode)

    def run_local(self, command: list[str], *, check: bool = True, label: str = "local") -> CommandResult:
        return self._run(command, check=check, label=label)

    def run_remote(
        self,
        shell_command: str,
        *,
        check: bool = True,
        label: str = "remote",
        timeout: Optional[float] = None,
    ) -> CommandResult:
        if not self.target_host:
            raise ScenarioError("TARGET_HOST is required")
        ssh_command = self._wrap_transport(
            [
                "ssh",
                "-o",
                "LogLevel=ERROR",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                *self._ssh_options,
                self.remote,
                shell_command,
            ]
        )
        return self._run(ssh_command, check=check, label=label, timeout=timeout or self.ssh_timeout)

    def scp_to_remote(self, source: Path, destination: str, *, recursive: bool = False, label: str = "scp-upload") -> None:
        command = [
            "scp",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            *self._ssh_options,
        ]
        if recursive:
            command.append("-r")
        command.extend([str(source), f"{self.remote}:{destination}"])
        self._run(self._wrap_transport(command), check=True, label=label, timeout=self.scp_timeout)

    def scp_from_remote(self, source: str, destination: Path, *, recursive: bool = False, label: str = "scp-download") -> None:
        command = [
            "scp",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            *self._ssh_options,
        ]
        if recursive:
            command.append("-r")
        command.extend([f"{self.remote}:{source}", str(destination)])
        self._run(self._wrap_transport(command), check=True, label=label, timeout=self.scp_timeout)

    def run_os_customization(self, *args: str, check: bool = True, label: str = "os-customization-set") -> CommandResult:
        remote_command = " ".join(
            [
                shlex.quote(self.os_customization_set),
                "--root",
                shlex.quote(self.target_root),
                *[shlex.quote(arg) for arg in args],
            ]
        )
        return self.run_remote(remote_command, check=check, label=label)

    def status(self) -> dict:
        result = self.run_os_customization("status", label="status")
        return json.loads(result.stdout)

    def boot_selection(self) -> dict:
        result = self.run_remote(f"cat {shlex.quote(BOOT_SELECTION_PATH)}", label="boot-selection")
        return json.loads(result.stdout)

    def current_boot_id(self, *, check: bool = True) -> str:
        return self.run_remote(
            "cat /proc/sys/kernel/random/boot_id",
            label="boot-id",
            check=check,
            timeout=self.boot_probe_timeout,
        ).stdout.strip()

    def read_remote_file(self, path: str) -> str:
        return self.run_remote(f"cat {shlex.quote(path)}", label=f"read {path}").stdout

    def remote_file_exists(self, path: str) -> bool:
        result = self.run_remote(f"test -e {shlex.quote(path)}", check=False, label=f"exists {path}")
        return result.returncode == 0

    def assert_true(self, condition: bool, message: str) -> None:
        if not condition:
            raise ScenarioError(message)

    def require_ready(self) -> None:
        self.log(f"SCENARIO {self.scenario_id}: {self.description}")
        if not self.target_host:
            raise ScenarioError("TARGET_HOST is required")
        self.run_remote("true", label="connectivity")
        self.run_remote(f"test -x {shlex.quote(self.os_customization_set)}", label="cli-present")
        self.run_remote(f"test -f {shlex.quote(BOOT_SELECTION_PATH)}", label="boot-integration")
        service_state = self.run_remote(
            "systemctl is-enabled os-customization-check.timer",
            check=False,
            label="health-timer-enabled",
        )
        self.assert_true(
            service_state.returncode == 0,
            "os-customization-check.timer must be enabled before running target scenarios",
        )

    def create_payload(
        self,
        *,
        manifest: dict,
        etc_files: Optional[dict[str, str]] = None,
        whiteouts: Optional[list[str]] = None,
    ) -> Path:
        temp_dir = tempfile.TemporaryDirectory(prefix=f"{self.scenario_id}-{self.slug}-")
        self._temp_dirs.append(temp_dir)
        payload_dir = Path(temp_dir.name)
        (payload_dir / "etc").mkdir(parents=True, exist_ok=True)
        manifest_path = payload_dir / "manifest.json"
        manifest_payload = {"format_version": 1, **manifest}
        manifest_path.write_text(json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        for relative_path, content in (etc_files or {}).items():
            target_path = payload_dir / "etc" / relative_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(content, encoding="utf-8")
        if whiteouts:
            (payload_dir / "whiteouts.txt").write_text("\n".join(whiteouts) + "\n", encoding="utf-8")
        return payload_dir

    def upload_payload(self, payload_dir: Path, name: str) -> str:
        remote_dir = f"{self.remote_payload_root}/{self.scenario_id}-{name}"
        self.run_remote(f"rm -rf {shlex.quote(remote_dir)} && mkdir -p {shlex.quote(self.remote_payload_root)}", label="prepare-upload-dir")
        self.scp_to_remote(payload_dir, remote_dir, recursive=True, label=f"upload {name}")
        return remote_dir

    def install_payload(self, payload_dir: Path, name: str) -> dict:
        remote_dir = self.upload_payload(payload_dir, name)
        result = self.run_os_customization("install", remote_dir, label=f"install {name}")
        return json.loads(result.stdout)

    def install_factory_payload(self, payload_dir: Path, name: str) -> dict:
        remote_dir = self.upload_payload(payload_dir, name)
        result = self.run_os_customization("install-factory", remote_dir, label=f"install-factory {name}")
        return json.loads(result.stdout)

    def reboot(self, reason: str) -> None:
        self.log(f"ACTION reboot: {reason}")
        self.run_remote("systemctl reboot >/dev/null 2>&1 || reboot || true", label="reboot", check=False)

    def health_service_enabled(self) -> bool:
        result = self.run_remote(
            "systemctl is-enabled os-customization-check.timer",
            check=False,
            label="health-timer-enabled",
        )
        return result.returncode == 0

    def health_service_active_state(self) -> str:
        result = self.run_remote(
            "systemctl is-active os-customization-check.service",
            check=False,
            label="health-service-active",
        )
        return result.stdout.strip() or result.stderr.strip() or "unknown"

    def health_service_boot_journal(self) -> str:
        result = self.run_remote(
            "journalctl -u os-customization-check.service -b --no-pager -n 20",
            check=False,
            label="health-service-journal",
        )
        return result.stdout.strip()

    def candidate_resolution_failure_reason(self) -> Optional[str]:
        status = self.status()
        if not status.get("candidate_slot"):
            return None
        if not self.health_service_enabled():
            return "os-customization-check.timer is disabled after reboot, so the candidate cannot be committed or rolled back"
        active_state = self.health_service_active_state()
        journal = self.health_service_boot_journal()
        if active_state in {"inactive", "failed", "unknown"} and not journal:
            return f"os-customization-check.service did not run during this boot (state={active_state})"
        return None

    def wait_for_candidate_resolution(
        self,
        *,
        old_boot_id: str,
        description: str,
        success_predicate: Callable[["TargetContext"], tuple[bool, str] | bool],
        timeout: int = 300,
        grace_period: int = 20,
    ) -> None:
        deadline = time.time() + timeout
        boot_changed_at: Optional[float] = None
        last_detail = "predicate never returned success"
        while time.time() < deadline:
            try:
                current_boot_id = self.current_boot_id(check=False)
                if not current_boot_id:
                    last_detail = "boot-id unavailable while reboot is in progress"
                    time.sleep(2)
                    continue
                if current_boot_id != old_boot_id and boot_changed_at is None:
                    boot_changed_at = time.time()

                try:
                    verdict = success_predicate(self)
                except ScenarioError as exc:
                    last_detail = str(exc)
                    time.sleep(2)
                    continue
                if isinstance(verdict, tuple):
                    matched, detail = verdict
                else:
                    matched, detail = verdict, ""
                last_detail = detail
                if matched:
                    self.log(f"WAIT satisfied: {description}: {detail}")
                    return

                if boot_changed_at is not None and time.time() - boot_changed_at >= grace_period:
                    try:
                        failure_reason = self.candidate_resolution_failure_reason()
                    except ScenarioError as exc:
                        last_detail = str(exc)
                        time.sleep(2)
                        continue
                    if failure_reason:
                        raise ScenarioError(f"{description}: {failure_reason}")
            except Exception as exc:  # noqa: BLE001
                last_detail = str(exc)
            time.sleep(2)
        raise ScenarioError(f"timed out waiting for {description}: {last_detail}")

    def wait_for(self, description: str, predicate: Callable[["TargetContext"], tuple[bool, str] | bool], timeout: int = 300) -> None:
        deadline = time.time() + timeout
        last_detail = "predicate never returned success"
        while time.time() < deadline:
            try:
                verdict = predicate(self)
                if isinstance(verdict, tuple):
                    matched, detail = verdict
                else:
                    matched, detail = verdict, ""
                last_detail = detail
                if matched:
                    self.log(f"WAIT satisfied: {description}: {detail}")
                    return
            except Exception as exc:  # noqa: BLE001
                last_detail = str(exc)
            time.sleep(2)
        raise ScenarioError(f"timed out waiting for {description}: {last_detail}")

    def read_hosts(self) -> str:
        return self.read_remote_file("/etc/hosts")

    def wait_for_boot_id_change(self, old_boot_id: str, description: str, timeout: int = 180) -> None:
        def predicate(ctx: TargetContext) -> tuple[bool, str]:
            current = ctx.current_boot_id()
            return current != old_boot_id, f"boot_id={current}"

        self.wait_for(description, predicate, timeout=timeout)

    def reset_to_factory(self) -> None:
        self.log("ACTION factory reset with state wipe")
        old_boot_id = self.current_boot_id()
        self.run_os_customization("factory-reset", "--wipe-state", label="factory-reset")
        self.reboot("apply factory reset")
        self.wait_for(
            "factory-reset stabilization",
            lambda ctx: (
                ctx.current_boot_id() != old_boot_id
                and ctx.status().get("active_slot") is None
                and ctx.status().get("last_good_slot") is None
                and ctx.status().get("candidate_slot") is None,
                json.dumps(ctx.status(), sort_keys=True),
            ),
            timeout=240,
        )

    def snapshot_factory_payload(self) -> Path:
        temp_dir = tempfile.TemporaryDirectory(prefix=f"{self.scenario_id}-factory-backup-")
        self._temp_dirs.append(temp_dir)
        snapshot_root = Path(temp_dir.name)
        self.scp_from_remote(f"{self.target_root}/factory", snapshot_root, recursive=True, label="backup-factory")
        candidate = snapshot_root / "factory"
        if not (candidate / "manifest.json").is_file():
            raise ScenarioError("factory snapshot does not contain manifest.json and cannot be restored with install-factory")
        return candidate

    def restore_factory_payload(self, snapshot_dir: Path) -> None:
        self.log("ACTION restore factory payload snapshot")
        self.install_factory_payload(snapshot_dir, "restore-factory")
        self.reset_to_factory()

    def read_issue(self) -> str:
        return self.read_remote_file("/etc/issue")

    def current_core_os_version(self) -> str:
        issue = self.read_issue()
        match = CORE_OS_VERSION_PATTERN.search(issue)
        if not match:
            raise ScenarioError("could not determine current Core OS version from /etc/issue")
        return f"v{match.group(1)}"

    def write_rootfs_file(self, path: str, content: str, label: str) -> None:
        if not path.startswith("/"):
            raise ScenarioError(f"rootfs path must be absolute: {path}")
        temp_dir = tempfile.TemporaryDirectory(prefix=f"{self.scenario_id}-rootfs-")
        self._temp_dirs.append(temp_dir)
        local_file = Path(temp_dir.name) / Path(path).name
        local_file.write_text(content, encoding="utf-8")
        remote_temp = f"/tmp/{local_file.name}"
        self.scp_to_remote(local_file, remote_temp, label=f"upload {label}")
        escaped_path = shlex.quote(path)
        escaped_temp = shlex.quote(remote_temp)
        rootfs_bind_path = shlex.quote(f"/run/rootfs-etc{path.removeprefix('/etc')}")
        can_use_rootfs_etc_bind = path == "/etc" or path.startswith("/etc/")
        bind_condition = "grep -Fqs ' /run/rootfs-etc ' /proc/mounts" if can_use_rootfs_etc_bind else "false"
        self.run_remote(
            "set -eu; "
            f"if {bind_condition} && [ -e {rootfs_bind_path} ]; then "
            "mount -o remount,rw /; "
            "mount -o remount,bind,rw /run/rootfs-etc; "
            f"cp {escaped_temp} {rootfs_bind_path}; "
            "mount -o remount,bind,ro /run/rootfs-etc; "
            "mount -o remount,ro /; "
            "else "
            f"mount -o remount,rw /; cp {escaped_temp} {escaped_path}; mount -o remount,ro /; "
            "fi; "
            f"rm -f {escaped_temp}",
            label=f"write {label}",
        )


def build_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--log-dir",
        default=str(Path(__file__).resolve().parent / "logs"),
        help="Directory for scenario logs",
    )
    return parser


def finalize(context: Optional[TargetContext], exit_code: int) -> int:
    if context is not None:
        context.close()
    return exit_code
