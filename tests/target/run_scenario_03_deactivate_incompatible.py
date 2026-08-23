#!/usr/bin/env python3

from __future__ import annotations

import json
from pathlib import Path

from scenario_lib import ScenarioError, TargetContext, build_parser, finalize


GOOD_HOSTS = """127.0.0.1 localhost
127.0.0.1 target-test-03-compatible
"""


def main() -> int:
    parser = build_parser("Run target scenario 03: deactivate incompatible customization")
    args = parser.parse_args()
    context: TargetContext | None = None
    original_issue: str | None = None
    try:
        context = TargetContext("03", "deactivate-incompatible", "Deactivate a last-known-good customization when Core OS compatibility changes", log_dir=Path(args.log_dir))
        context.require_ready()
        context.reset_to_factory()

        baseline_hosts = context.read_hosts()
        original_issue = context.read_issue()
        current_core_os = context.current_core_os_version()
        payload = context.create_payload(
            manifest={
                "version": "target-test-03-compatible-1.0.0",
                "compatible_core_os": current_core_os,
                "health_checks": [{"type": "path_exists", "path": "/etc/hosts"}],
            },
            etc_files={"hosts": GOOD_HOSTS},
        )
        context.install_payload(payload, "compatible")

        old_boot_id = context.current_boot_id()
        context.reboot("commit scenario 03 compatible payload")
        context.wait_for_candidate_resolution(
            old_boot_id=old_boot_id,
            description="scenario 03 compatible payload commit",
            success_predicate=lambda ctx: (
                ctx.read_hosts() == GOOD_HOSTS
                and ctx.status().get("candidate_slot") is None,
                json.dumps(ctx.status(), sort_keys=True),
            ),
            timeout=240,
        )

        incompatible_issue = "Scenario-Test-Image_v99.99.99.000000.20260823.0000\n"
        context.write_rootfs_file("/etc/issue", incompatible_issue, label="scenario-03-issue")

        changed_boot_id = context.current_boot_id()
        context.reboot("deactivate incompatible scenario 03 payload")
        context.wait_for(
            "scenario 03 incompatibility deactivation",
            lambda ctx: (
                ctx.current_boot_id() != changed_boot_id
                and ctx.read_hosts() == baseline_hosts
                and ctx.boot_selection().get("selected_slot") in {None, ""}
                and "last-good-incompatible-core-os" in ctx.boot_selection().get("selection_reason", ""),
                json.dumps(ctx.boot_selection(), sort_keys=True),
            ),
            timeout=240,
        )

        context.log("SCENARIO 03 PASS")
        return finalize(context, 0)
    except ScenarioError as exc:
        if context is not None:
            context.log(f"SCENARIO 03 FAIL: {exc}")
        else:
            print(f"SCENARIO 03 FAIL: {exc}")
        return finalize(context, 1)
    finally:
        if context is not None and original_issue is not None:
            try:
                context.write_rootfs_file("/etc/issue", original_issue, label="restore-issue")
                context.reset_to_factory()
            except ScenarioError as cleanup_exc:
                context.log(f"SCENARIO 03 CLEANUP FAIL: {cleanup_exc}")


if __name__ == "__main__":
    raise SystemExit(main())