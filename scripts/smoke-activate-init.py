#!/usr/bin/env python3

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent

SERIAL_MONITOR_SCRIPT = r'''
import os
import select
import sys
import termios
import time

device = sys.argv[1]
timeout = float(sys.argv[2])

fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
try:
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0
    attrs[1] = 0
    attrs[2] = termios.CLOCAL | termios.CREAD | termios.CS8
    attrs[3] = 0
    attrs[4] = termios.B115200
    attrs[5] = termios.B115200
    termios.tcsetattr(fd, termios.TCSANOW, attrs)

    deadline = time.time() + timeout if timeout > 0 else None
    while deadline is None or time.time() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.5)
        if fd not in ready:
            continue
        data = os.read(fd, 4096)
        if not data:
            continue
        os.write(1, data)
finally:
    os.close(fd)
'''

SERIAL_RESTORE_SCRIPT = r'''
import os
import select
import sys
import termios
import time

device = sys.argv[1]
password = sys.argv[2]
timeout = float(sys.argv[3])

fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)

def write_line(text):
    os.write(fd, text.encode("utf-8") + b"\n")

def read_until(markers, deadline):
    buffer = b""
    while time.time() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.5)
        if fd in ready:
            chunk = os.read(fd, 4096)
            if chunk:
                buffer += chunk
                os.write(1, chunk)
                for marker in markers:
                    if marker in buffer:
                        return marker, buffer
    raise SystemExit("timed out waiting for serial prompt")

try:
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0
    attrs[1] = 0
    attrs[2] = termios.CLOCAL | termios.CREAD | termios.CS8
    attrs[3] = 0
    attrs[4] = termios.B115200
    attrs[5] = termios.B115200
    termios.tcsetattr(fd, termios.TCSANOW, attrs)

    deadline = time.time() + timeout
    marker, _ = read_until([b"login:", b"# ", b"#\r", b"#\n"], deadline)
    if marker == b"login:":
        write_line("root")
        marker, _ = read_until([b"Password:", b"# ", b"#\r", b"#\n"], deadline)
        if marker == b"Password:":
            write_line(password)
            read_until([b"# ", b"#\r", b"#\n"], deadline)

    commands = [
        "mount -o remount,rw /",
        "if [ -e /sbin/init.stock ]; then rm -f /sbin/init; cp -a /sbin/init.stock /sbin/init; elif [ -x /usr/lib/systemd/systemd ]; then rm -f /sbin/init; ln -s ../lib/systemd/systemd /sbin/init; else echo no-known-init >&2; exit 1; fi",
        "mount -o remount,ro /",
        "reboot",
    ]
    for command in commands:
        write_line(command)
        if command != "reboot":
            read_until([b"# ", b"#\r", b"#\n"], deadline)
finally:
    os.close(fd)
'''


class SmokeFailure(RuntimeError):
    pass


def run_command(command, *, env=None, check=True, capture_output=False):
    return subprocess.run(
        command,
        env=env,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
    )


def build_target_env(password: str | None) -> dict:
    env = os.environ.copy()
    if password:
        env["SSHPASS"] = password
        ssh = "sshpass -e ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
        scp = "sshpass -e scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
        env["SSH"] = ssh
        env["SCP"] = scp
    return env


def ssh_base_command(host: str, user: str, password: str | None) -> tuple[list[str], dict]:
    env = os.environ.copy()
    command = ["ssh"]
    if password:
        env["SSHPASS"] = password
        command = ["sshpass", "-e", "ssh"]
    command.extend(["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", f"{user}@{host}"])
    return command, env


def scp_base_command(password: str | None) -> tuple[list[str], dict]:
    env = os.environ.copy()
    command = ["scp"]
    if password:
        env["SSHPASS"] = password
        command = ["sshpass", "-e", "scp"]
    command.extend(["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"])
    return command, env


def run_ssh(host: str, user: str, password: str | None, remote_command: str, *, check=True, capture_output=False):
    command, env = ssh_base_command(host, user, password)
    command.append(remote_command)
    return run_command(command, env=env, check=check, capture_output=capture_output)


def copy_to_remote(host: str, user: str, password: str | None, local_path: Path, remote_path: str) -> None:
    command, env = scp_base_command(password)
    command.extend(["-r", str(local_path), f"{user}@{host}:{remote_path}"])
    run_command(command, env=env)


def start_serial_monitor(helper_host: str, helper_user: str, serial_device: str, timeout: int, log_file: Path):
    log_handle = log_file.open("wb")
    command = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        f"{helper_user}@{helper_host}",
        "python3",
        "-c",
        SERIAL_MONITOR_SCRIPT,
        serial_device,
        str(timeout),
    ]
    process = subprocess.Popen(command, stdout=log_handle, stderr=subprocess.STDOUT)
    return process, log_handle


def stop_serial_monitor(process: subprocess.Popen, log_handle) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    log_handle.close()


def serial_restore(helper_host: str, helper_user: str, serial_device: str, root_password: str, timeout: int) -> None:
    command = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        f"{helper_user}@{helper_host}",
        "python3",
        "-c",
        SERIAL_RESTORE_SCRIPT,
        serial_device,
        root_password,
        str(timeout),
    ]
    run_command(command)


def wait_for_ssh_and_marker(host: str, user: str, password: str | None, timeout: int, boot_marker: str) -> str:
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        probe = run_ssh(
            host,
            user,
            password,
            f"cat /proc/sys/kernel/random/boot_id && test -f {boot_marker}",
            check=False,
            capture_output=True,
        )
        if probe.returncode == 0:
            return probe.stdout.strip().splitlines()[0]
        last_error = (probe.stdout or "") + (probe.stderr or "")
        time.sleep(3)
    raise SmokeFailure(f"timed out waiting for SSH and boot marker {boot_marker}: {last_error or 'no response'}")


def read_status(host: str, user: str, password: str | None, target_root: str) -> dict:
    result = run_ssh(
        host,
        user,
        password,
        f"PYTHONPATH=/usr/lib/os-customization/python /usr/bin/os-customization-set --root {target_root} status",
        capture_output=True,
    )
    return json.loads(result.stdout)


def wait_for_payload_outcome(
    host: str,
    user: str,
    password: str | None,
    target_root: str,
    expected_outcome: str,
    expected_slot: str,
    baseline_last_good: str | None,
    timeout: int,
) -> dict:
    deadline = time.time() + timeout
    last_status = None
    while time.time() < deadline:
        try:
            status = read_status(host, user, password, target_root)
            last_status = status
        except Exception:
            time.sleep(3)
            continue

        candidate_slot = status.get("candidate_slot")
        active_slot = status.get("active_slot")
        last_good_slot = status.get("last_good_slot")
        candidate_state = status.get("candidate_state")

        if expected_outcome == "commit":
            if candidate_slot is None and active_slot == expected_slot and last_good_slot == expected_slot:
                return status
        elif expected_outcome == "rollback":
            if (
                candidate_slot is None
                and candidate_state == "rolled-back"
                and active_slot == baseline_last_good
                and last_good_slot == baseline_last_good
            ):
                return status

        time.sleep(3)

    raise SmokeFailure(f"timed out waiting for payload outcome {expected_outcome}: {last_status}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="smoke-activate-init.py")
    parser.add_argument("--target-host", default=os.environ.get("TARGET_HOST"))
    parser.add_argument("--target-user", default=os.environ.get("TARGET_USER", "root"))
    parser.add_argument("--target-password", default=os.environ.get("TARGET_PASSWORD"))
    parser.add_argument("--helper-host", default=os.environ.get("SERIAL_HELPER_HOST"))
    parser.add_argument("--helper-user", default=os.environ.get("SERIAL_HELPER_USER"))
    parser.add_argument("--serial-device", default=os.environ.get("SERIAL_DEVICE"))
    parser.add_argument("--serial-root-password", default=os.environ.get("SERIAL_ROOT_PASSWORD"))
    parser.add_argument("--boot-marker", default="/run/os-customization/boot-selection.json")
    parser.add_argument("--boot-timeout", type=int, default=180)
    parser.add_argument("--serial-timeout", type=int, default=240)
    parser.add_argument("--serial-log", default=str(REPO_ROOT / "smoke-activate-init.serial.log"))
    parser.add_argument("--target-root", default="/data/os-customization")
    parser.add_argument("--payload-remote-root", default="/data/os-customization-smoke")
    parser.add_argument("--payload")
    parser.add_argument("--expected-outcome", choices=["commit", "rollback", "boot-only"], default="boot-only")
    parser.add_argument("--keep-wrapper-on-success", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.target_host:
        raise SystemExit("target host must be provided via --target-host or TARGET_HOST")
    target_password = args.target_password
    serial_root_password = args.serial_root_password or target_password

    if not target_password:
        raise SystemExit("target password must be provided via --target-password or TARGET_PASSWORD")
    if not serial_root_password:
        raise SystemExit("serial root password must be provided via --serial-root-password or SERIAL_ROOT_PASSWORD")

    target_env = build_target_env(target_password)
    serial_log_path = Path(args.serial_log)
    payload_path = Path(args.payload).resolve() if args.payload else None

    monitor_process = None
    monitor_log_handle = None
    wrapper_activated = False
    success = False
    try:
        deploy_command = [
            "sh",
            str(REPO_ROOT / "scripts" / "deploy-test-machine.sh"),
            "--rootfs-stage",
            "--activate-init-wrapper",
            "--activate-services",
        ]
        run_command(deploy_command, env=target_env)
        wrapper_activated = True

        old_boot = run_ssh(
            args.target_host,
            args.target_user,
            target_password,
            "cat /proc/sys/kernel/random/boot_id",
            capture_output=True,
        ).stdout.strip()

        baseline_status = read_status(args.target_host, args.target_user, target_password, args.target_root)
        expected_slot = None

        if payload_path is not None:
            remote_payload_dir = f"{args.payload_remote_root}/{payload_path.name}"
            run_ssh(args.target_host, args.target_user, target_password, f"mkdir -p {args.payload_remote_root} && rm -rf {remote_payload_dir}")
            copy_to_remote(args.target_host, args.target_user, target_password, payload_path, remote_payload_dir)
            run_ssh(
                args.target_host,
                args.target_user,
                target_password,
                f"PYTHONPATH=/usr/lib/os-customization/python /usr/libexec/os-customization-mender-install --root {args.target_root} --no-reboot {remote_payload_dir}",
            )
            expected_slot = read_status(args.target_host, args.target_user, target_password, args.target_root).get("candidate_slot")

        monitor_process, monitor_log_handle = start_serial_monitor(
            args.helper_host,
            args.helper_user,
            args.serial_device,
            args.serial_timeout,
            serial_log_path,
        )

        run_ssh(args.target_host, args.target_user, target_password, "systemctl reboot", check=False)

        new_boot = wait_for_ssh_and_marker(
            args.target_host,
            args.target_user,
            target_password,
            args.boot_timeout,
            args.boot_marker,
        )
        if new_boot == old_boot:
            raise SmokeFailure("target reported the same boot ID after reboot")

        if payload_path is not None and args.expected_outcome != "boot-only":
            final_status = wait_for_payload_outcome(
                args.target_host,
                args.target_user,
                target_password,
                args.target_root,
                args.expected_outcome,
                expected_slot or "",
                baseline_status.get("last_good_slot"),
                args.boot_timeout,
            )
            print(json.dumps(final_status, indent=2, sort_keys=True))

        success = True
        print(f"smoke activation succeeded: boot_id={new_boot}")
        return 0
    except Exception as exc:
        print(f"smoke activation failed: {exc}", file=sys.stderr)
        if wrapper_activated:
            try:
                if monitor_process and monitor_log_handle:
                    stop_serial_monitor(monitor_process, monitor_log_handle)
                    monitor_process = None
                    monitor_log_handle = None
                serial_restore(
                    args.helper_host,
                    args.helper_user,
                    args.serial_device,
                    serial_root_password,
                    args.serial_timeout,
                )
                wait_for_ssh_and_marker(
                    args.target_host,
                    args.target_user,
                    target_password,
                    args.boot_timeout,
                    "/proc/sys/kernel/random/boot_id",
                )
                print("stock init restored after failed smoke activation", file=sys.stderr)
            except Exception as restore_exc:
                print(f"automatic serial restore failed: {restore_exc}", file=sys.stderr)
        return 1
    finally:
        if monitor_process and monitor_log_handle:
            stop_serial_monitor(monitor_process, monitor_log_handle)

        if success and wrapper_activated and not args.keep_wrapper_on_success:
            try:
                restore_command = ["sh", str(REPO_ROOT / "scripts" / "restore-target-init.sh")]
                run_command(restore_command, env=target_env)
                run_ssh(
                    args.target_host,
                    args.target_user,
                    target_password,
                    "mount -o remount,rw / && systemctl disable --now os-customization-check.service os-customization-factory-reset.service 2>/dev/null || true && systemctl daemon-reload && mount -o remount,ro /",
                    check=False,
                )
                print("stock init restored after successful smoke activation")
            except Exception as exc:
                print(f"warning: could not restore stock init after success: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
