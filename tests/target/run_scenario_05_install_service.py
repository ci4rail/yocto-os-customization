#!/usr/bin/env python3

from __future__ import annotations

import json
import shlex
from pathlib import Path

from scenario_lib import ScenarioError, TargetContext, build_parser, finalize


SERVICE_NAME = "os-customization-target-test-05.service"
SERVICE_BINARY = "/etc/os-customization-target-test-05/bin/sh"
MARKER_PATH = "/tmp/os-customization-target-test-05-ran"
MARKER_CONTENT = "scenario-05-service-ran\n"

SERVICE_UNIT = f"""[Unit]
Description=OS customization target scenario 05 service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={SERVICE_BINARY} -c 'echo scenario-05-service-ran > {MARKER_PATH}'

[Install]
WantedBy=multi-user.target
"""

def service_state(context: TargetContext, command: str) -> bool:
    return context.run_remote(command, check=False, label="service-state").returncode == 0


def main() -> int:
    parser = build_parser("Run target scenario 05: install a systemd service")
    args = parser.parse_args()
    context: TargetContext | None = None
    reset_for_test = False
    exit_code = 1
    try:
        context = TargetContext("05", "install-service", "Install, enable, and health-check a customization-provided systemd service", log_dir=Path(args.log_dir))
        context.require_ready()
        context.run_remote("command -v systemd-analyze >/dev/null", label="systemd-validator-present")
        context.reset_to_factory()
        reset_for_test = True
        context.assert_true(not context.remote_file_exists(SERVICE_BINARY), "test binary exists before installation")
        context.run_remote(f"rm -f {MARKER_PATH}", label="remove-service-marker")

        payload = context.create_payload(
            manifest={
                "version": "target-test-05-service-1.0.0",
                "compatible_core_os": None,
                "health_checks": [{"type": "systemd_unit_active", "unit": SERVICE_NAME}],
            },
            etc_files={
                f"systemd/system/{SERVICE_NAME}": SERVICE_UNIT,
            },
        )
        # Recursive scp may dereference local symlinks.  Construct the
        # enablement link in the uploaded payload so the installer receives
        # the same symlink a customer artifact would contain.
        remote_payload = context.upload_payload(payload, "service")
        # Use a target-native binary without requiring a cross compiler. Keep
        # the basename 'sh' so BusyBox shells select the correct applet. Copy
        # the executable itself, not /bin/sh's symlink: ExecStart must resolve
        # a file supplied by this payload during installation-time validation.
        remote_binary = f"{remote_payload}{SERVICE_BINARY}"
        context.run_remote(
            f"mkdir -p {shlex.quote(str(Path(remote_binary).parent))} && "
            f"cp -L /bin/sh {shlex.quote(remote_binary)} && "
            f"chmod 0755 {shlex.quote(remote_binary)} && "
            f"test -f {shlex.quote(remote_binary)} && test ! -L {shlex.quote(remote_binary)}",
            label="stage-service-binary",
        )
        remote_wants_dir = f"{remote_payload}/etc/systemd/system/multi-user.target.wants"
        remote_link = f"{remote_wants_dir}/{SERVICE_NAME}"
        context.run_remote(
            f"mkdir -p {remote_wants_dir} && rm -f {remote_link} && ln -s ../{SERVICE_NAME} {remote_link} && test -L {remote_link}",
            label="create-service-enablement-link",
        )
        install_result = json.loads(
            context.run_os_customization("install", remote_payload, label="install service").stdout
        )
        context.assert_true(not context.remote_file_exists(MARKER_PATH), "service ran before reboot")
        context.assert_true(not context.remote_file_exists(SERVICE_BINARY), "test binary became visible before reboot")
        pending_status = context.status()
        context.assert_true(pending_status.get("candidate_slot") == install_result.get("slot"), "candidate slot mismatch after install")
        context.assert_true(pending_status.get("candidate_state") == "pending", "candidate state should be pending after install")

        old_boot_id = context.current_boot_id()
        context.reboot("activate scenario 05 service customization")
        context.wait_for_candidate_resolution(
            old_boot_id=old_boot_id,
            description="scenario 05 service activation",
            success_predicate=lambda ctx: (
                service_state(ctx, f"systemctl is-enabled --quiet {SERVICE_NAME}")
                and service_state(ctx, f"test -x {SERVICE_BINARY}")
                and service_state(ctx, f"systemctl is-active --quiet {SERVICE_NAME}")
                and ctx.read_remote_file(MARKER_PATH) == MARKER_CONTENT
                and ctx.status().get("candidate_slot") is None
                and ctx.status().get("active_version") == "target-test-05-service-1.0.0",
                json.dumps(ctx.status(), sort_keys=True),
            ),
            timeout=240,
        )
        context.log("SCENARIO 05 PASS")
        exit_code = 0
    except ScenarioError as exc:
        if context is not None and reset_for_test:
            context.log(f"SCENARIO 05 FAIL: {exc}")
        else:
            print(f"SCENARIO 05 FAIL: {exc}")
    finally:
        if context is not None:
            try:
                context.reset_to_factory()
                context.run_remote(f"rm -f {MARKER_PATH}", label="remove-service-marker")
            except ScenarioError as cleanup_exc:
                context.log(f"SCENARIO 05 CLEANUP FAIL: {cleanup_exc}")
                exit_code = 1
    return finalize(context, exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
