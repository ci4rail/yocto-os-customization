#!/usr/bin/env python3

from __future__ import annotations

import json
from pathlib import Path

from scenario_lib import ScenarioError, TargetContext, build_parser, finalize


def main() -> int:
    parser = build_parser("Run target scenario 05: whiteouts")
    args = parser.parse_args()
    context: TargetContext | None = None
    try:
        context = TargetContext("05", "whiteouts", "Verify whiteout-based deletion and restoration after factory reset", log_dir=Path(args.log_dir))
        context.require_ready()
        context.reset_to_factory()
        context.assert_true(context.remote_file_exists("/etc/wgetrc"), "/etc/wgetrc must exist before running scenario 05")

        payload = context.create_payload(
            manifest={
                "version": "target-test-05-whiteout-1.0.0",
                "compatible_core_os": None,
            },
            whiteouts=["/etc/wgetrc"],
        )
        context.install_payload(payload, "whiteout")

        old_boot_id = context.current_boot_id()
        context.reboot("commit scenario 05 whiteout payload")
        context.wait_for_candidate_resolution(
            old_boot_id=old_boot_id,
            description="scenario 05 whiteout activation",
            success_predicate=lambda ctx: (
                not ctx.remote_file_exists("/etc/wgetrc")
                and ctx.status().get("candidate_slot") is None,
                json.dumps(ctx.status(), sort_keys=True),
            ),
            timeout=240,
        )

        reset_boot_id = context.current_boot_id()
        context.run_os_customization("factory-reset", "--wipe-state", label="factory-reset")
        context.reboot("apply scenario 05 factory reset")
        context.wait_for(
            "scenario 05 restore after factory reset",
            lambda ctx: (
                ctx.current_boot_id() != reset_boot_id
                and ctx.remote_file_exists("/etc/wgetrc")
                and ctx.status().get("active_slot") is None,
                json.dumps(ctx.status(), sort_keys=True),
            ),
            timeout=240,
        )

        context.log("SCENARIO 05 PASS")
        return finalize(context, 0)
    except ScenarioError as exc:
        if context is not None:
            context.log(f"SCENARIO 05 FAIL: {exc}")
        else:
            print(f"SCENARIO 05 FAIL: {exc}")
        return finalize(context, 1)


if __name__ == "__main__":
    raise SystemExit(main())