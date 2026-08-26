#!/bin/sh

set -eu

TARGET_HOST=${TARGET_HOST:-}
TARGET_USER=${TARGET_USER:-root}
TARGET_PREFIX=${TARGET_PREFIX:-/usr}
TARGET_ROOT=${TARGET_ROOT:-/data/os-customization}
PAYLOAD_ROOT=${PAYLOAD_ROOT:-/data/os-customization-e2e}
SSH=${SSH:-ssh}
SCP=${SCP:-scp}
REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
GOOD_PAYLOAD=${GOOD_PAYLOAD:-$REPO_ROOT/samples/payload-good}
BAD_PAYLOAD=${BAD_PAYLOAD:-$REPO_ROOT/samples/payload-bad}

usage() {
    cat <<'EOF'
Usage: e2e-test-machine.sh

Runs an install -> reboot -> health-check -> commit flow with the good payload,
then an install -> reboot -> health-check -> rollback flow with the bad payload.

Prerequisites:
  - The os-customization tools are already deployed to the target.
  - The target boot path is wired to os-customization-preinit.sh so that
    /run/os-customization/boot-selection.json exists after boot.
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage
    exit 0
fi

if [ -z "$TARGET_HOST" ]; then
    echo "set TARGET_HOST to the test device address" >&2
    exit 1
fi

remote="$TARGET_USER@$TARGET_HOST"

remote_json_field() {
    field=$1
    payload=$2
    printf '%s' "$payload" | python3 -c 'import json,sys; payload=json.load(sys.stdin); value=payload
for part in sys.argv[1].split("."):
    value = value.get(part) if isinstance(value, dict) else None
print("" if value is None else value)' "$field"
}

run_remote() {
    $SSH "$remote" "$1"
}

wait_for_reboot_cycle() {
    old_boot_id=$1
    down_seen=0
    attempts=0
    while [ "$attempts" -lt 90 ]; do
        if boot_id=$(run_remote 'cat /proc/sys/kernel/random/boot_id' 2>/dev/null); then
            if [ "$down_seen" -eq 1 ] && [ "$boot_id" != "$old_boot_id" ]; then
                printf '%s\n' "$boot_id"
                return 0
            fi
        else
            down_seen=1
        fi
        attempts=$((attempts + 1))
        sleep 2
    done
    echo "timed out waiting for reboot cycle" >&2
    return 1
}

require_boot_integration() {
    if ! run_remote 'test -f /run/os-customization/boot-selection.json'; then
        echo "target boot path is not yet integrated with os-customization-preinit.sh" >&2
        echo "refusing to run live candidate test because commit/rollback would not be meaningful" >&2
        exit 1
    fi
}

run_phase() {
    payload_local=$1
    expected_candidate_state=$2
    remote_payload_dir="$PAYLOAD_ROOT/$(basename "$payload_local")"

    run_remote "mkdir -p $PAYLOAD_ROOT"
    run_remote "rm -rf $remote_payload_dir"
    $SCP -r "$payload_local" "$remote:$remote_payload_dir"

    old_boot_id=$(run_remote 'cat /proc/sys/kernel/random/boot_id')
    run_remote "PYTHONPATH=$TARGET_PREFIX/lib/os-customization/python $TARGET_PREFIX/libexec/os-customization-mender-install --root $TARGET_ROOT --no-reboot $remote_payload_dir"
    run_remote 'systemctl reboot' >/dev/null 2>&1 || true
    new_boot_id=$(wait_for_reboot_cycle "$old_boot_id")

    status_json=$(run_remote "PYTHONPATH=$TARGET_PREFIX/lib/os-customization/python $TARGET_PREFIX/bin/os-customization-set --root $TARGET_ROOT status")
    candidate_slot=$(remote_json_field candidate_slot "$status_json")
    candidate_state=$(remote_json_field candidate_state "$status_json")
    active_slot=$(remote_json_field active_slot "$status_json")
    last_good_slot=$(remote_json_field last_good_slot "$status_json")

    echo "boot_id=$new_boot_id"
    echo "candidate_slot=$candidate_slot"
    echo "candidate_state=$candidate_state"
    echo "active_slot=$active_slot"
    echo "last_good_slot=$last_good_slot"

    if [ "$expected_candidate_state" = "commit" ]; then
        if [ -n "$candidate_slot" ] || [ -z "$active_slot" ] || [ "$active_slot" != "$last_good_slot" ]; then
            echo "expected committed candidate after reboot" >&2
            return 1
        fi
    else
        if [ -n "$candidate_slot" ] || [ "$candidate_state" != "rolled-back" ]; then
            echo "expected rolled-back candidate after reboot" >&2
            return 1
        fi
    fi
}

require_boot_integration
run_phase "$GOOD_PAYLOAD" commit
run_phase "$BAD_PAYLOAD" rollback
