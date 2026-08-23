#!/bin/sh

set -eu

TARGET_HOST=${TARGET_HOST:-}
TARGET_USER=${TARGET_USER:-root}
SSH=${SSH:-ssh}

usage() {
    cat <<'EOF'
Usage: restore-target-init.sh

Restores /sbin/init from /sbin/init.stock when present, otherwise to the stock
systemd symlink used on the current test machine.
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

$SSH "$remote" 'set -eu; mount -o remount,rw /; if [ -e /sbin/init.stock ]; then rm -f /sbin/init; cp -a /sbin/init.stock /sbin/init; elif [ -x /usr/lib/systemd/systemd ]; then rm -f /sbin/init; ln -s ../lib/systemd/systemd /sbin/init; else echo "no known stock init found" >&2; exit 1; fi; mount -o remount,ro /'
