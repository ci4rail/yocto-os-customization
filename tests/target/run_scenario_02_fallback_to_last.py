#!/usr/bin/env python3

from __future__ import annotations

import json
from pathlib import Path

from scenario_lib import ScenarioError, TargetContext, build_parser, finalize


GOOD_HOSTS = """127.0.0.1 localhost
127.0.0.1 target-test-02-good
"""

BAD_HOSTS = """127.0.0.1 localhost
127.0.0.1 target-test-02-bad
"""


def main() -> int:
    parser = build_parser("Run target scenario 02: fallback to last-known-good")
    args = parser.parse_args()
    context: TargetContext | None = None
    try:
        context = TargetContext("02", "fallback-to-last", "Roll back to the last-known-good customization after a failed health check", log_dir=Path(args.log_dir))
        context.require_ready()
        context.reset_to_factory()

        good_payload = context.create_payload(
            manifest={
                "version": "target-test-02-good-1.0.0",
                "compatible_core_os": None,
                "health_checks": [{"type": "path_exists", "path": "/etc/hosts"}],
            },
            etc_files={"hosts": GOOD_HOSTS},
        )
        context.install_payload(good_payload, "good")
        good_boot_id = context.current_boot_id()
        context.reboot("commit scenario 02 good payload")
        context.wait_for_candidate_resolution(
            old_boot_id=good_boot_id,
            description="scenario 02 good payload commit",
            success_predicate=lambda ctx: (
                ctx.read_hosts() == GOOD_HOSTS
                and ctx.status().get("candidate_slot") is None
                and ctx.status().get("active_version") == "target-test-02-good-1.0.0",
                json.dumps(ctx.status(), sort_keys=True),
            ),
            timeout=240,
        )

        good_status = context.status()
        good_slot = good_status.get("last_good_slot")
        context.assert_true(good_slot in {"A", "B"}, "good payload did not become last-known-good")

        bad_payload = context.create_payload(
            manifest={
                "version": "target-test-02-bad-1.0.0",
                "compatible_core_os": None,
                "health_checks": [{"type": "path_exists", "path": "/etc/does-not-exist-target-test-02"}],
            },
            etc_files={"hosts": BAD_HOSTS},
        )
        install_result = context.install_payload(bad_payload, "bad")
        context.assert_true(install_result.get("slot") != good_slot, "bad payload should install into the inactive slot")

        old_boot_id = context.current_boot_id()
        context.reboot("activate scenario 02 bad payload")
        context.wait_for_candidate_resolution(
            old_boot_id=old_boot_id,
            description="scenario 02 rollback",
            success_predicate=lambda ctx: (
                ctx.read_hosts() == GOOD_HOSTS
                and ctx.status().get("candidate_slot") is None
                and ctx.status().get("candidate_state") == "rolled-back"
                and ctx.status().get("last_good_slot") == good_slot,
                json.dumps(ctx.status(), sort_keys=True),
            ),
            timeout=360,
        )

        context.log("SCENARIO 02 PASS")
        return finalize(context, 0)
    except ScenarioError as exc:
        if context is not None:
            context.log(f"SCENARIO 02 FAIL: {exc}")
        else:
            print(f"SCENARIO 02 FAIL: {exc}")
        return finalize(context, 1)


if __name__ == "__main__":
    raise SystemExit(main())