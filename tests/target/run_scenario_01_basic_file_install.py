#!/usr/bin/env python3

from __future__ import annotations

import json
from pathlib import Path

from scenario_lib import ScenarioError, TargetContext, build_parser, finalize


HOSTS_CONTENT = """127.0.0.1 localhost
127.0.0.1 target-test-01-customized

# Scenario 01: user customization override
"""


def main() -> int:
    parser = build_parser("Run target scenario 01: basic file install")
    args = parser.parse_args()
    context: TargetContext | None = None
    try:
        context = TargetContext("01", "basic-file-install", "Install a basic /etc/hosts customization", log_dir=Path(args.log_dir))
        context.require_ready()
        context.reset_to_factory()

        baseline_hosts = context.read_hosts()
        payload = context.create_payload(
            manifest={
                "version": "target-test-01-hosts-1.0.0",
                "compatible_core_os": None,
                "health_checks": [{"type": "path_exists", "path": "/etc/hosts"}],
            },
            etc_files={"hosts": HOSTS_CONTENT},
        )
        install_result = context.install_payload(payload, "good")
        context.assert_true(context.read_hosts() == baseline_hosts, "/etc/hosts changed before reboot")

        pending_status = context.status()
        context.assert_true(pending_status.get("candidate_slot") == install_result.get("slot"), "candidate slot mismatch after install")
        context.assert_true(pending_status.get("candidate_state") == "pending", "candidate state should be pending after install")

        old_boot_id = context.current_boot_id()
        context.reboot("activate scenario 01 candidate")
        context.wait_for_candidate_resolution(
            old_boot_id=old_boot_id,
            description="scenario 01 activation",
            success_predicate=lambda ctx: (
                ctx.read_hosts() == HOSTS_CONTENT
                and ctx.status().get("candidate_slot") is None
                and ctx.status().get("active_version") == "target-test-01-hosts-1.0.0",
                json.dumps(ctx.status(), sort_keys=True),
            ),
            timeout=240,
        )

        final_status = context.status()
        context.assert_true(final_status.get("active_slot") == final_status.get("last_good_slot"), "active slot should equal last-good slot after commit")
        context.log("SCENARIO 01 PASS")
        return finalize(context, 0)
    except ScenarioError as exc:
        if context is not None:
            context.log(f"SCENARIO 01 FAIL: {exc}")
        else:
            print(f"SCENARIO 01 FAIL: {exc}")
        return finalize(context, 1)


if __name__ == "__main__":
    raise SystemExit(main())