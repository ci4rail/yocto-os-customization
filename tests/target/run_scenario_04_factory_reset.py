#!/usr/bin/env python3

from __future__ import annotations

import json
from pathlib import Path

from scenario_lib import ScenarioError, TargetContext, build_parser, finalize


FACTORY_HOSTS = """127.0.0.1 localhost
127.0.0.1 target-test-04-factory
"""

USER_HOSTS = """127.0.0.1 localhost
127.0.0.1 target-test-04-user
"""


def main() -> int:
    parser = build_parser("Run target scenario 04: factory reset")
    args = parser.parse_args()
    context: TargetContext | None = None
    factory_snapshot = None
    exit_code = 1
    try:
        context = TargetContext("04", "factory-reset", "Restore factory configuration and wipe STATE content", log_dir=Path(args.log_dir))
        context.require_ready()
        factory_snapshot = context.snapshot_factory_payload()
        context.reset_to_factory()

        factory_payload = context.create_payload(
            manifest={
                "version": "target-test-04-factory-1.0.0",
                "compatible_core_os": None,
            },
            etc_files={"hosts": FACTORY_HOSTS},
        )
        context.install_factory_payload(factory_payload, "factory")

        user_payload = context.create_payload(
            manifest={
                "version": "target-test-04-user-1.0.0",
                "compatible_core_os": None,
                "health_checks": [{"type": "path_exists", "path": "/etc/hosts"}],
            },
            etc_files={"hosts": USER_HOSTS},
        )
        context.install_payload(user_payload, "user")
        old_boot_id = context.current_boot_id()
        context.reboot("commit scenario 04 user payload")
        context.wait_for_candidate_resolution(
            old_boot_id=old_boot_id,
            description="scenario 04 user payload commit",
            success_predicate=lambda ctx: (
                ctx.read_hosts() == USER_HOSTS
                and ctx.status().get("candidate_slot") is None,
                json.dumps(ctx.status(), sort_keys=True),
            ),
            timeout=240,
        )

        context.run_remote("mkdir -p /etc/target-test-04-dir && printf 'temporary\n' > /etc/target-test-04-dir/note && printf 'temporary\n' > /etc/target-test-04-extra", label="create-state-files")
        context.assert_true(context.remote_file_exists("/etc/target-test-04-dir/note"), "expected test state file to exist before factory reset")

        reset_boot_id = context.current_boot_id()
        context.run_os_customization("factory-reset", "--wipe-state", label="factory-reset")
        context.reboot("apply scenario 04 factory reset")
        context.wait_for(
            "scenario 04 factory reset",
            lambda ctx: (
                ctx.current_boot_id() != reset_boot_id
                and ctx.read_hosts() == FACTORY_HOSTS
                and not ctx.remote_file_exists("/etc/target-test-04-dir/note")
                and not ctx.remote_file_exists("/etc/target-test-04-extra")
                and ctx.status().get("active_slot") is None
                and ctx.status().get("last_good_slot") is None,
                json.dumps(ctx.status(), sort_keys=True),
            ),
            timeout=240,
        )

        context.log("SCENARIO 04 PASS")
        exit_code = 0
    except ScenarioError as exc:
        if context is not None:
            context.log(f"SCENARIO 04 FAIL: {exc}")
        else:
            print(f"SCENARIO 04 FAIL: {exc}")
    finally:
        if context is not None and factory_snapshot is not None:
            try:
                context.restore_factory_payload(factory_snapshot)
            except ScenarioError as cleanup_exc:
                context.log(f"SCENARIO 04 CLEANUP FAIL: {cleanup_exc}")
    return finalize(context, exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
