#!/bin/sh

set -eu
set -x

TARGET_HOST=${TARGET_HOST:-}
TARGET_USER=${TARGET_USER:-root}
TARGET_PREFIX=${TARGET_PREFIX:-/data/os-customization-tools}
TARGET_PASSWORD=${TARGET_PASSWORD:-}
SSH=${SSH:-}
SCP=${SCP:-}
REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

if [ -n "$TARGET_PASSWORD" ]; then
    if ! command -v sshpass >/dev/null 2>&1; then
        echo "TARGET_PASSWORD requires sshpass to be installed" >&2
        exit 1
    fi
    export SSHPASS=${SSHPASS:-$TARGET_PASSWORD}
    SSH=${SSH:-sshpass -e ssh -o LogLevel=ERROR -o StrictHostKeyChecking=no}
    SCP=${SCP:-sshpass -e scp -o LogLevel=ERROR -o StrictHostKeyChecking=no}
fi

SSH=${SSH:-ssh}
SCP=${SCP:-scp}

usage() {
    cat <<'EOF'
Usage: deploy-test-machine.sh [--skip-services] [--activate-services] [--rootfs-stage] [--activate-init-wrapper]

Environment:
    TARGET_PASSWORD       Optional SSH password. When set, default SSH/SCP use sshpass.
  SSH / SCP              Override transport commands.
  TARGET_HOST            Target hostname or IP.
  TARGET_USER            Target SSH user.
  TARGET_PREFIX          Development-staging prefix on target (default:
                         /data/os-customization-tools). Production rootfs
                         staging always installs under /usr and /sbin.

This copies the implementation to a test machine but does not replace /sbin/init.
For immutable-root systems it stages binaries below /data by default.
Use --rootfs-stage to copy into /usr and /sbin on the target rootfs without switching init.
Use --activate-init-wrapper only together with --rootfs-stage if you explicitly want /sbin/init replaced.
EOF
}

skip_services=0
activate_services=0
rootfs_stage=0
activate_init_wrapper=0

while [ $# -gt 0 ]; do
    case "$1" in
        --skip-services)
            skip_services=1
            ;;
        --activate-services)
            activate_services=1
            ;;
        --rootfs-stage)
            rootfs_stage=1
            ;;
        --activate-init-wrapper)
            activate_init_wrapper=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
    shift
done

if [ "$activate_init_wrapper" -eq 1 ] && [ "$rootfs_stage" -eq 0 ]; then
    echo "--activate-init-wrapper requires --rootfs-stage" >&2
    exit 1
fi

if [ -z "$TARGET_HOST" ]; then
    echo "set TARGET_HOST to the test device address" >&2
    exit 1
fi

remote="$TARGET_USER@$TARGET_HOST"
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
service_python_prefix=$TARGET_PREFIX/lib/os-customization/python
service_libexec_prefix=$TARGET_PREFIX/libexec
service_bin_prefix=$TARGET_PREFIX/bin
service_etc_root=/etc
service_etc_bind_rw_opened=0

if [ "$rootfs_stage" -eq 1 ]; then
    service_python_prefix=/usr/lib/os-customization/python
    service_libexec_prefix=/usr/libexec
    service_bin_prefix=/usr/bin
fi

rootfs_rw_opened=0

open_rootfs_rw() {
    if [ "$rootfs_rw_opened" -eq 1 ]; then
        return 0
    fi
    if ! $SSH "$remote" "mount -o remount,rw /"; then
        echo "failed to remount the target root filesystem read-write" >&2
        return 1
    fi
    rootfs_rw_opened=1
}

close_rootfs_ro() {
    if [ "$rootfs_rw_opened" -eq 0 ]; then
        return 0
    fi
    # An ext4 read-only remount can transiently return EBUSY while services
    # finish writes caused by the deployment.  Flush first and retry instead
    # of leaving the target root filesystem writable without an explanation.
    if ! $SSH "$remote" 'sync; attempt=1; while [ "$attempt" -le 3 ]; do if mount -o remount,ro /; then exit 0; fi; attempt=$((attempt + 1)); sleep 1; done; echo "failed to restore / as read-only" >&2; exit 1'; then
        echo "target root filesystem remains read-write; inspect its mount state before rebooting" >&2
        return 1
    fi
    rootfs_rw_opened=0
}

open_service_etc_rw() {
    if [ "$service_etc_root" != /run/rootfs-etc ] || [ "$service_etc_bind_rw_opened" -eq 1 ]; then
        return 0
    fi
    $SSH "$remote" "mount -o remount,bind,rw /run/rootfs-etc"
    service_etc_bind_rw_opened=1
}

close_service_etc_ro() {
    if [ "$service_etc_root" != /run/rootfs-etc ] || [ "$service_etc_bind_rw_opened" -eq 0 ]; then
        return 0
    fi
    $SSH "$remote" "mount -o remount,bind,ro /run/rootfs-etc"
    service_etc_bind_rw_opened=0
}

$SSH "$remote" "mkdir -p $TARGET_PREFIX/bin $TARGET_PREFIX/lib/os-customization/python/os_customization $TARGET_PREFIX/libexec $TARGET_PREFIX/sbin"

# Development staging intentionally uses the live /etc.  Rootfs staging must
# never do so: when the init wrapper is active, /etc is the STATE OverlayFS
# upper layer and files copied there would disappear on a state wipe.
if [ "$rootfs_stage" -eq 0 ]; then
    $SSH "$remote" "mkdir -p /etc/systemd/system"
fi

$SCP "$REPO_ROOT/bin/os-customization-set" "$remote:$TARGET_PREFIX/bin/os-customization-set"
$SCP "$REPO_ROOT/libexec/os-customization-check" "$remote:$TARGET_PREFIX/libexec/os-customization-check"
$SCP "$REPO_ROOT/libexec/os-customization-mender-install" "$remote:$TARGET_PREFIX/libexec/os-customization-mender-install"
$SCP "$REPO_ROOT/sbin/os-customization-preinit.sh" "$remote:$TARGET_PREFIX/sbin/os-customization-preinit.sh"
$SCP "$REPO_ROOT/python/os_customization/__init__.py" "$remote:$TARGET_PREFIX/lib/os-customization/python/os_customization/__init__.py"
$SCP "$REPO_ROOT/python/os_customization/manager.py" "$remote:$TARGET_PREFIX/lib/os-customization/python/os_customization/manager.py"

$SSH "$remote" "chmod 0755 $TARGET_PREFIX/bin/os-customization-set $TARGET_PREFIX/libexec/os-customization-check $TARGET_PREFIX/libexec/os-customization-mender-install $TARGET_PREFIX/sbin/os-customization-preinit.sh"

if [ "$skip_services" -eq 0 ]; then
    if [ "$rootfs_stage" -eq 1 ]; then
        open_rootfs_rw
        if ! $SSH "$remote" "grep -Fqs ' /run/rootfs-etc ' /proc/mounts && test -d /run/rootfs-etc"; then
            echo "rootfs staging requires the read-only SYSROOT /etc bind mount at /run/rootfs-etc" >&2
            echo "boot the target through os-customization-preinit before using --rootfs-stage" >&2
            close_rootfs_ro || true
            exit 1
        fi
        service_etc_root=/run/rootfs-etc
        open_service_etc_rw
    fi
    sed "s#/usr/lib/os-customization/python#$service_python_prefix#g; s#/usr/libexec#$service_libexec_prefix#g; s#/usr/bin#$service_bin_prefix#g" \
        "$REPO_ROOT/systemd/os-customization-check.service" > "$tmpdir/os-customization-check.service"
    cp "$REPO_ROOT/systemd/os-customization-check.timer" "$tmpdir/os-customization-check.timer"
    sed "s#/usr/lib/os-customization/python#$service_python_prefix#g; s#/usr/libexec#$service_libexec_prefix#g; s#/usr/bin#$service_bin_prefix#g" \
        "$REPO_ROOT/systemd/os-customization-factory-reset.service" > "$tmpdir/os-customization-factory-reset.service"
    $SSH "$remote" "mkdir -p $service_etc_root/systemd/system"
    $SCP "$tmpdir/os-customization-check.service" "$remote:$service_etc_root/systemd/system/os-customization-check.service"
    $SCP "$tmpdir/os-customization-check.timer" "$remote:$service_etc_root/systemd/system/os-customization-check.timer"
    $SCP "$tmpdir/os-customization-factory-reset.service" "$remote:$service_etc_root/systemd/system/os-customization-factory-reset.service"
    if [ "$activate_services" -eq 1 ]; then
        if [ "$service_etc_root" = /etc ]; then
            $SSH "$remote" "systemctl disable os-customization-check.service || true; systemctl daemon-reload && systemctl enable os-customization-check.timer os-customization-factory-reset.service"
        else
            $SSH "$remote" "set -eu; rm -f $service_etc_root/systemd/system/multi-user.target.wants/os-customization-check.service; mkdir -p $service_etc_root/systemd/system/timers.target.wants $service_etc_root/systemd/system/multi-user.target.wants && ln -snf ../os-customization-check.timer $service_etc_root/systemd/system/timers.target.wants/os-customization-check.timer && ln -snf ../os-customization-factory-reset.service $service_etc_root/systemd/system/multi-user.target.wants/os-customization-factory-reset.service && systemctl daemon-reload"
        fi
    fi
fi

close_service_etc_ro

if [ "$rootfs_stage" -eq 1 ]; then
    open_rootfs_rw
    $SSH "$remote" "set -eu; mkdir -p /usr/bin /usr/lib/os-customization/python/os_customization /usr/libexec /sbin /usr/share/mender/modules/v3 /usr/share/mender/inventory"
    $SCP "$REPO_ROOT/bin/os-customization-set" "$remote:/usr/bin/os-customization-set"
    $SCP "$REPO_ROOT/libexec/os-customization-check" "$remote:/usr/libexec/os-customization-check"
    $SCP "$REPO_ROOT/libexec/os-customization-mender-install" "$remote:/usr/libexec/os-customization-mender-install"
    $SCP "$REPO_ROOT/mender/modules/os-customization" "$remote:/usr/share/mender/modules/v3/os-customization"
    $SCP "$REPO_ROOT/mender/inventory/os-customization" "$remote:/usr/share/mender/inventory/os-customization"
    $SCP "$REPO_ROOT/sbin/os-customization-preinit.sh" "$remote:/sbin/init.os-customization"
    $SCP "$REPO_ROOT/python/os_customization/__init__.py" "$remote:/usr/lib/os-customization/python/os_customization/__init__.py"
    $SCP "$REPO_ROOT/python/os_customization/manager.py" "$remote:/usr/lib/os-customization/python/os_customization/manager.py"
    $SSH "$remote" "chmod 0755 /usr/bin/os-customization-set /usr/libexec/os-customization-check /usr/libexec/os-customization-mender-install /usr/share/mender/modules/v3/os-customization /usr/share/mender/inventory/os-customization /sbin/init.os-customization && if [ ! -e /sbin/init.stock ]; then cp -a /sbin/init /sbin/init.stock; fi"

    if [ "$activate_init_wrapper" -eq 1 ]; then
        $SSH "$remote" "set -eu; rm -f /sbin/init; ln -s init.os-customization /sbin/init"
    fi
fi

close_rootfs_ro
