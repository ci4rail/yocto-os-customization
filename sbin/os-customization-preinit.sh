#!/bin/sh

set -u

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

DATA_DEVICE=${DATA_DEVICE:-/dev/mmcblk0p4}
DATA_MOUNT=${DATA_MOUNT:-/data}
OS_CUSTOMIZATION_ROOT=${OS_CUSTOMIZATION_ROOT:-/data/os-customization}
ROOTFS_ETC_BIND=${ROOTFS_ETC_BIND:-/run/rootfs-etc}
REAL_INIT=${REAL_INIT:-/sbin/init.real}
FALLBACK_REAL_INIT=${FALLBACK_REAL_INIT:-/usr/sbin/init.orig}
PYTHONPATH_DIR=${PYTHONPATH_DIR:-/usr/lib/os-customization/python}
MANAGER=${MANAGER:-/usr/bin/os-customization-set}
BOOT_STATE_DIR=${BOOT_STATE_DIR:-/run/os-customization}
BOOT_SELECTION_PATH=${BOOT_SELECTION_PATH:-/run/os-customization/boot-selection.json}
PREINIT_LOG=${PREINIT_LOG:-/dev/kmsg}

log() {
    message="PREINIT: $*"
    echo "$message"
    if [ -w "$PREINIT_LOG" ]; then
        echo "$message" > "$PREINIT_LOG" 2>/dev/null || true
    fi
}

is_mounted() {
    grep -Fqs " $1 " /proc/mounts
}

ensure_dir() {
    [ -d "$1" ] || mkdir -p "$1"
}

safe_run() {
    "$@"
}

manager_available() {
    [ -x "$MANAGER" ] || return 1
    # CPython otherwise waits for the kernel CSPRNG to seed its hash secret.
    # Preinit only processes root-controlled lifecycle metadata at this point.
    PYTHONHASHSEED=0 PYTHONPATH="$PYTHONPATH_DIR" "$MANAGER" --root "$OS_CUSTOMIZATION_ROOT" status >/dev/null 2>&1
}

mount_api_fs() {
    ensure_dir /proc || return 1
    ensure_dir /sys || return 1
    ensure_dir /run || return 1
    is_mounted /proc || safe_run mount -t proc proc /proc || return 1
    is_mounted /sys || safe_run mount -t sysfs sysfs /sys || return 1
    is_mounted /run || safe_run mount -t tmpfs -o mode=0755,nodev,nosuid tmpfs /run || return 1
}

mount_data() {
    ensure_dir "$DATA_MOUNT" || return 1
    if is_mounted "$DATA_MOUNT"; then
        return 0
    fi
    safe_run mount -n -t ext4 -o defaults "$DATA_DEVICE" "$DATA_MOUNT"
}

mount_overlay_etc() {
    manager_available || {
        log "manager unavailable at $MANAGER"
        return 1
    }
    ensure_dir "$BOOT_STATE_DIR" || {
        log "failed to create boot state dir $BOOT_STATE_DIR"
        return 1
    }
    boot_id=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)
    boot_env=$(PYTHONHASHSEED=0 PYTHONPATH="$PYTHONPATH_DIR" "$MANAGER" --root "$OS_CUSTOMIZATION_ROOT" boot-prepare-shell --boot-id "$boot_id" --boot-selection-path "$BOOT_SELECTION_PATH" 2>/dev/null) || {
        log "boot-prepare-shell failed"
        return 1
    }
    [ -n "$boot_env" ] || {
        log "boot-prepare-shell returned empty payload"
        return 1
    }

    eval "$boot_env" || {
        log "failed to evaluate boot environment"
        return 1
    }

    [ -f "$BOOT_SELECTION_PATH" ] || {
        log "boot selection marker missing at $BOOT_SELECTION_PATH"
        return 1
    }

    [ -n "$STATE_ETC_PATH" ] || {
        log "boot-prepare missing state_etc_path"
        return 1
    }
    [ -n "$STATE_WORK_ETC_PATH" ] || {
        log "boot-prepare missing state_work_etc_path"
        return 1
    }
    ensure_dir "$ROOTFS_ETC_BIND" || return 1
    ensure_dir "$STATE_ETC_PATH" || return 1
    ensure_dir "$STATE_WORK_ETC_PATH" || return 1

    is_mounted "$ROOTFS_ETC_BIND" || safe_run mount --bind /etc "$ROOTFS_ETC_BIND" || return 1
    safe_run mount -o remount,bind,ro "$ROOTFS_ETC_BIND" || return 1

    lowerdirs="$ROOTFS_ETC_BIND"
    if [ -n "$FACTORY_PATH" ]; then
        lowerdirs="$FACTORY_PATH/etc:$lowerdirs"
    else
        log "factory customization excluded ($SELECTION_REASON)"
    fi
    if [ -n "$SELECTED_SLOT" ]; then
        lowerdirs="$OS_CUSTOMIZATION_ROOT/user-$SELECTED_SLOT/etc:$lowerdirs"
    else
        log "user customization excluded ($SELECTION_REASON)"
    fi

    safe_run mount -n -t overlay overlay \
        -o "upperdir=$STATE_ETC_PATH,workdir=$STATE_WORK_ETC_PATH,lowerdir=$lowerdirs,index=off,xino=off,redirect_dir=off,metacopy=off" \
        /etc
}

start_real_init() {
    if [ -x "$REAL_INIT" ]; then
        exec "$REAL_INIT"
    fi
    if [ -x "$FALLBACK_REAL_INIT" ]; then
        exec "$FALLBACK_REAL_INIT"
    fi
    log "real init missing at $REAL_INIT and $FALLBACK_REAL_INIT"
    exec /usr/sbin/init.orig
}

log "start"
if ! mount_api_fs; then
    log "failed to mount early API filesystems"
fi

if mount_data; then
    if mount_overlay_etc; then
        log "overlay /etc ready"
    else
        log "overlay mount failed; continuing without customization"
    fi
else
    log "mounting $DATA_MOUNT failed; continuing without customization"
fi

start_real_init
