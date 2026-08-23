# Core OS Customization

This document describes the architecture, lifecycle, implementation constraints, and deployment integration for the Core OS customization mechanism.

## 1. Goal

The device uses an immutable/read-only A/B root filesystem.

Customers must be able to customize selected parts of the Core OS without modifying the root filesystem image.

For the current implementation:

> **All customizable files must reside below `/etc`.**

Typical customizable files include:

```text
/etc/chrony.conf
/etc/nftables.conf
/etc/snmp/snmpd.conf
/etc/ssh/authorized_keys/root
/etc/passwd
/etc/shadow
/etc/systemd/system/...
/etc/<application-specific>/*
```

A customization may also install additional systemd unit files below `/etc/systemd/system`.

The customization mechanism must support:

* factory customization;
* field/customer customization;
* versioning;
* A/B update of user customization;
* candidate activation on reboot;
* automatic rollback to the last-known-good customization;
* factory reset;
* health checking;
* identification of active, candidate and last-known-good versions;
* independence of the core customization lifecycle from Mender.

The normal customer-facing field deployment path is:

```text
Customer
   ↓
core-api-server
   ↓
Mender
   ↓
os-customization-set
```

Mender provides the authenticated deployment mechanism. The underlying `os-customization-set` implementation remains usable independently.

---

# 2. Architecture

`/etc` is composed using OverlayFS.

Conceptually, precedence is:

```text
highest precedence

STATE             writable persistent state
USER              active user customization, read-only
FACTORY           factory customization, read-only
SYSROOT /etc      Core OS defaults, read-only

lowest precedence
```

The application-visible `/etc` is therefore:

```text
                  application-visible /etc
                            │
                       OverlayFS
                            │
              ┌─────────────┼─────────────┐
              │             │             │
            STATE          USER         FACTORY
           writable       read-only      read-only
                            │
                       SYSROOT /etc
```

Prefer implementing this as **one OverlayFS with multiple lower directories**, rather than nested OverlayFS mounts.

Conceptually:

```text
upperdir = STATE

lowerdir =
    USER
    FACTORY
    SYSROOT-/etc
```

The leftmost lower directory has the highest precedence.

The original rootfs `/etc` should be made available through a separate read-only bind mount before mounting the resulting OverlayFS on `/etc`.

Example:

```sh
mount --bind /etc /run/rootfs-etc
mount -o remount,bind,ro /run/rootfs-etc

mount -t overlay overlay \
    -o upperdir=/data/os-customization/state/etc \
    -o workdir=/data/os-customization/state-work/etc \
    -o lowerdir=/data/os-customization/user-B/etc:/data/os-customization/factory/etc:/run/rootfs-etc \
    /etc
```

The actual USER slot is selected dynamically during early boot.

---

# 3. Persistent Storage Layout

Use `/data/os-customization` as the persistent root.

Suggested layout:

```text
/data/os-customization/
├── factory/
│   ├── manifest.json
│   └── etc/
│
├── user-A/
│   ├── manifest.json
│   └── etc/
│
├── user-B/
│   ├── manifest.json
│   └── etc/
│
├── state/
│   └── etc/
│
├── state-work/
│
└── status
```

`factory`, `user-A`, and `user-B` are immutable while being used as OverlayFS lower layers.

`state` is the writable OverlayFS upper layer.

---

# 4. Factory Customization

A `factory-customization-set` is installed during device production.

It is stored separately from the read-only rootfs:

```text
/data/os-customization/factory/
```

It represents the device-specific factory configuration.

Factory customization is always below USER in the OverlayFS precedence:

```text
USER
 ↓
FACTORY
 ↓
SYSROOT
```

If no USER customization is active, FACTORY becomes the effective customization layer.

A factory reset must:

1. disable/remove the active USER customization;
2. restore FACTORY as the highest customization layer;
3. reset appropriate persistent STATE;
4. reboot.

Factory reset and rollback are deliberately different operations:

```text
rollback
    → return to last-known-good USER customization

factory-reset
    → return to FACTORY customization
```

Rollback must not normally revert directly to FACTORY.

---

# 5. User Customization A/B

There are two USER slots:

```text
user-A
user-B
```

Do not assign permanent semantics such as "A is active" and "B is candidate".

Instead maintain explicit state such as:

```text
active_slot=A
last_good_slot=A
candidate_slot=B
candidate_attempts=0
```

The inactive slot is used when installing a new customization-set.

Example:

```text
Current:

A = v17, active + last known good
B = inactive
```

Installing v18 results in:

```text
A = v17, last known good
B = v18, candidate
```

After successful activation:

```text
A = v17
B = v18, active + last known good
```

The next update can overwrite A.

Never overwrite the active or last-known-good slot while installing a new candidate.

---

# 6. Customization-Set Installation

Installation and activation are separate operations.

The generic installation operation is conceptually:

```text
os-customization-set install <directory>
        │
        ▼
validate customization-set
        │
        ▼
determine inactive USER slot
        │
        ▼
prepare inactive slot
        │
        ▼
atomically finish installation
        │
        ▼
mark slot as candidate
```

Installing a customization-set must **not immediately modify the running `/etc`**.

The new set becomes effective only after reboot.

The implementation must be robust against power loss.

Do not modify an active customization slot in-place.

Use temporary directories/files followed by atomic `rename()` where appropriate, and use `fsync()` where required for persistence guarantees.

For example:

```text
user-B.tmp
    ↓
write complete customization
    ↓
fsync files/directories
    ↓
validate
    ↓
rename(user-B.tmp, user-B)
    ↓
fsync parent directory
```

Only after successful installation may the slot be marked as candidate.

---

# 7. Activation

Activation is deliberately reboot-based.

Do not attempt to replace or remount the live `/etc` during normal system operation.

During early boot, `/sbin/init` or an equivalent preinit component:

1. mounts `/data`;
2. reads customization status;
3. chooses the USER layer;
4. exposes the original rootfs `/etc` through a read-only bind mount;
5. constructs the `/etc` OverlayFS;
6. starts the real systemd init.

Selection is conceptually:

```text
if candidate exists:
    use candidate
else if last_good exists:
    use last_good
else:
    use FACTORY only
```

The effective filesystem is:

```text
STATE
  ↓
selected USER slot
  ↓
FACTORY
  ↓
SYSROOT /etc
```

The reboot provides an atomic configuration boundary:

```text
before reboot → complete old customization

after reboot  → complete candidate customization
```

Do not try to activate individual files independently.

---

# 8. Candidate Health Check

A newly installed USER customization is initially a **candidate**.

After booting the candidate, a systemd service such as:

```text
os-customization-check.service
```

performs functional validation.

The customization-set may provide a health-check definition or script.

Possible checks include:

```text
required systemd services active
network configuration functional
Core API reachable
required interfaces available
application-specific checks
```

There are two distinct validation stages.

## 8.1 Installation-Time Validation

Perform as much validation as possible before reboot.

Examples:

```text
bundle structure
manifest
allowed paths
permissions
ownership
file types
symlinks
whiteouts
OS compatibility
configuration syntax
```

Where applicable, use application-specific validators such as:

```text
sshd -t
nft -c
systemd-analyze verify
```

A configuration that can already be proven invalid must never become a candidate.

## 8.2 Runtime Health Validation

Checks requiring the candidate system to actually run belong in the post-boot health check.

The health check determines whether the candidate becomes last-known-good or is rolled back.

---

# 9. Commit

If the candidate passes its health check:

```text
candidate
    │
    ▼
mark candidate as last_good
    │
    ▼
clear candidate state
```

Example resulting state:

```text
active_slot=B
last_good_slot=B
candidate_slot=none
candidate_attempts=0
```

The customization is now committed.

The previous slot remains available and may be reused by the next installation.

---

# 10. Rollback

If the candidate fails its health check:

```text
candidate fails
       │
       ▼
mark candidate failed
       │
       ▼
select last_good
       │
       ▼
reboot
```

Do not attempt to modify `/etc` live to perform rollback.

After reboot, early boot constructs `/etc` using the last-known-good USER slot.

Rollback must return to the **last-known-good USER customization**, not normally to FACTORY.

Consider a boot-attempt counter so that repeated crashes before `os-customization-check.service` executes cannot permanently brick the device.

For example:

```text
candidate_attempts >= MAX_ATTEMPTS
        │
        ▼
disable candidate
        │
        ▼
select last_good
        │
        ▼
reboot
```

The boot-attempt state must be updated early enough to detect a candidate that prevents systemd or the health-check service from starting.

---

# 11. Factory Reset

Factory reset is separate from rollback.

A factory reset must result in:

```text
STATE as defined by factory-reset policy
        ↓
FACTORY
        ↓
SYSROOT
```

No USER customization is active after factory reset.

Factory reset should normally:

```text
clear candidate
clear active USER selection
clear last_good USER selection
reset appropriate STATE
reboot
```

Whether USER-A and USER-B are physically erased or merely deactivated is an implementation choice, provided they cannot influence the effective configuration after factory reset.

---

# 12. STATE Layer

`STATE` is the only writable `/etc` layer.

Its purpose is **persistent device/application state**, not customer configuration.

A critical ownership rule applies:

> A path may be owned by either the customization mechanism or the STATE layer, but never both.

For example, if:

```text
/data/os-customization/state/etc/chrony.conf
```

exists, it shadows:

```text
USER/etc/chrony.conf
FACTORY/etc/chrony.conf
SYSROOT/etc/chrony.conf
```

and therefore prevents customization switching and rollback from affecting that file.

Consequently, applications must not persistently modify customization-managed paths.

Possible STATE-managed content includes device-generated identity or other files that legitimately need persistent runtime modification.

Whenever possible, runtime state should live outside `/etc`, for example below:

```text
/var/lib
/data
/run
```

Do not put a file into STATE merely because an application happens to write it. Determine whether the file represents configuration or device state first.

Examples requiring an explicit ownership decision include:

```text
/etc/passwd
/etc/shadow
/etc/ssh/authorized_keys/*
```

If these are customization-managed, runtime tools must not persistently modify them.

If they are runtime-managed, they belong to STATE and must not simultaneously be supplied as USER customization.

---

# 13. Customization-Set Path Restrictions

For the current implementation:

> A customization-set may contain files only below `/etc`.

The installer must enforce this.

Reject paths such as:

```text
/usr/bin/foo
/usr/lib/foo
/var/lib/foo
/boot/foo
../../foo
```

Accept only normalized paths belonging to `/etc`.

Protection against path traversal is mandatory.

Symlinks require special validation so they cannot be used during installation to escape the permitted `/etc` namespace.

---

# 14. Customization Semantics

For every path, USER has three possible semantics.

## 14.1 Inherit

The path does not exist in USER.

The next lower version is visible:

```text
USER:       absent
FACTORY:    foo.conf
SYSROOT:    foo.conf

=> FACTORY/foo.conf
```

## 14.2 Override

USER contains the path:

```text
USER:       foo.conf
FACTORY:    foo.conf

=> USER/foo.conf
```

## 14.3 Delete

USER explicitly contains an OverlayFS whiteout for the path:

```text
USER:       whiteout(foo.conf)
FACTORY:    foo.conf
SYSROOT:    foo.conf

=> path does not exist
```

The customization-set format and installer must explicitly support whiteouts/deletions.

Do not represent deletion simply by omitting a file. Omission means inheritance.

---

# 15. Manifest

Every customization-set must contain metadata describing the set.

Example:

```json
{
    "format_version": 1,
    "version": "1.4.2",
    "compatible_core_os": ">=4.2,<5.0"
}
```

The final format may additionally describe:

```text
files
hashes
permissions
ownership
symlinks
whiteouts
health check
compatibility constraints
```

The manifest format must remain independent of the transport/deployment mechanism.

The system must be able to report at least:

```text
factory version
active USER version
last-good USER version
candidate version
candidate state
```

---

# 16. Core OS Compatibility

A customization-set may depend on files, services or behavior supplied by a particular Core OS version.

Therefore a customization-set should contain Core OS compatibility information.

Before accepting a candidate, verify that it is compatible with the currently installed Core OS.

The customization mechanism must also behave predictably across Core OS A/B updates because:

```text
SYSROOT /etc
```

may change while:

```text
FACTORY
USER-A
USER-B
STATE
```

persist.

Compatibility between persistent customization and a newly installed Core OS must therefore be considered as part of the OS update process.

---

# 17. Separation of Responsibilities

Maintain the following architectural boundary:

```text
+------------------------------------------------+
| Customer-facing layer                          |
|                                                |
| core-api-server                                |
| - REST API                                     |
| - receives Mender Artifact                     |
| - starts Mender deployment                     |
+-----------------------+------------------------+
                        |
                        ▼
+------------------------------------------------+
| Secure deployment layer                        |
|                                                |
| Mender                                         |
| - Artifact processing                          |
| - signature verification                       |
| - deployment transaction                       |
| - invokes customization integration            |
+-----------------------+------------------------+
                        |
                        ▼
+------------------------------------------------+
| OS customization layer                         |
|                                                |
| os-customization-set                           |
| - validates customization content              |
| - manages USER A/B                             |
| - candidate selection                          |
| - status                                       |
| - commit / rollback                            |
| - factory reset                                |
+-----------------------+------------------------+
                        |
                        ▼
+------------------------------------------------+
| Filesystem / boot layer                        |
|                                                |
| preinit + OverlayFS                            |
|                                                |
| STATE                                          |
| USER A/B                                       |
| FACTORY                                        |
| SYSROOT                                        |
+------------------------------------------------+
```

Each layer should expose a narrow interface to the layer above it.

Do not bypass these boundaries merely because doing so appears simpler for a particular feature.

---

# 18. Core API Server

`core-api-server` provides the customer-facing API for installing an OS customization-set in the field.

The API accepts the customization-set packaged as a **Mender Artifact**.

The normal field installation path is:

```text
Customer
    │
    │ upload Mender Artifact
    ▼
core-api-server
    │
    │ invoke Mender
    ▼
Mender
```

`core-api-server` must not implement the customization lifecycle itself.

In particular, it must not directly:

* write USER-A or USER-B;
* manipulate OverlayFS layers;
* select the active customization slot;
* commit a customization;
* implement rollback;
* extract customization files directly into `/etc`;
* call `os-customization-set install` for normal field deployments.

Those responsibilities belong to Mender and `os-customization-set`.

---

# 19. Mender Integration

Mender is the normal deployment mechanism for user customization-sets installed through `core-api-server`.

Mender is responsible for:

* accepting the Mender Artifact supplied through `core-api-server`;
* verifying the Mender Artifact signature;
* managing its deployment transaction;
* extracting/providing the customization payload;
* invoking the OS customization integration;
* coordinating reboot where required.

Conceptually:

```text
core-api-server
       │
       ▼
     Mender
       │
       ├── Artifact authentication/signature verification
       │
       ▼
os-customization-set
```

Only correctly signed Mender Artifacts shall be accepted through this field deployment path.

---

# 20. Mender Independence of `os-customization-set`

Although normal field installation uses Mender, the core `os-customization-set` mechanism must remain independent of Mender.

It must not:

* parse Mender Artifacts;
* verify Mender Artifact signatures;
* communicate with a Mender server;
* depend on `core-api-server`;
* assume that Mender is the caller.

Its input is an already extracted/prepared customization-set.

For example:

```text
os-customization-set install <directory>
```

Its responsibilities are:

```text
validate customization-set
        │
        ▼
determine inactive USER slot
        │
        ▼
install customization
        │
        ▼
mark candidate
```

The same interface may therefore also be used during:

```text
manufacturing
development
automated testing
recovery
future deployment mechanisms
```

The intended dependency direction is strictly:

```text
normal field deployment:

core-api-server
       ↓
     Mender
       ↓
os-customization-set
       ↓
preinit / OverlayFS
```

Do not introduce the inverse dependency:

```text
os-customization-set
       │
       X
       └──> Mender
```

---

# 21. Signature Verification

Signature verification belongs to the **Mender Artifact deployment path**.

The normal customer field installation is:

```text
Customer
    ↓
core-api-server
    ↓
Mender Artifact
    ↓
Mender signature verification
    ↓
Mender customization integration
    ↓
os-customization-set install <extracted-set>
```

`os-customization-set` does not repeat Mender signature verification.

It must nevertheless perform its own structural and semantic validation.

These responsibilities answer different questions:

```text
Mender signature verification:

    "Is this an authorized Mender Artifact?"


os-customization-set validation:

    "Is this a structurally valid, compatible and safe
     customization-set for this Core OS?"
```

Do not conflate the two.

Installation paths that do not use Mender do not automatically require a Mender signature. Their authentication/authorization requirements are defined by the respective trusted environment or caller.

---

# 22. Normal Field Update Sequence

The expected field update sequence is:

```text
1. Customer uploads customization Mender Artifact
                 │
                 ▼
2. core-api-server passes Artifact to Mender
                 │
                 ▼
3. Mender verifies Artifact/signature
                 │
                 ▼
4. Mender extracts/provides customization payload
                 │
                 ▼
5. Mender invokes:
      os-customization-set install <payload>
                 │
                 ▼
6. os-customization-set validates payload
                 │
                 ▼
7. Install into inactive USER A/B slot
                 │
                 ▼
8. Mark installed slot as candidate
                 │
                 ▼
9. Reboot
                 │
                 ▼
10. preinit selects candidate and constructs /etc
                 │
                 ▼
11. systemd starts
                 │
                 ▼
12. os-customization-check.service
          /               \
         /                 \
       OK                  FAIL
       │                     │
       ▼                     ▼
     commit              rollback
                             │
                             ▼
                           reboot
```

The precise interaction between Mender's deployment state machine and the customization commit/rollback state machine must be designed carefully, but the OS customization manager remains authoritative for determining whether the candidate configuration is functional.

---

# 23. Generic Command-Line Interface

Prefer a small transport-independent interface.

Possible commands:

```text
os-customization-set install <directory>

os-customization-set status

os-customization-set commit

os-customization-set rollback

os-customization-set factory-reset
```

Additional internal commands may be introduced for boot processing if needed.

The architecture must maintain separation between:

```text
delivery
    ↓
installation
    ↓
candidate selection
    ↓
reboot / activation
    ↓
health checking
    ↓
commit / rollback
```

Do not couple these stages unnecessarily.

---

# 24. Security and Input Validation

Customization input must be treated as untrusted from the perspective of filesystem processing even when transport authentication has already succeeded.

The installer must protect against at least:

* path traversal;
* absolute paths escaping the target;
* malicious symlinks;
* unsupported file types;
* malformed manifests;
* invalid ownership or permissions;
* extraction outside the inactive slot;
* modification of the active slot;
* modification of FACTORY;
* modification of STATE;
* modification outside `/etc`.

Do not rely on archive extraction tools without validating paths.

Mender signature verification does not replace these checks.

Conversely, `os-customization-set` content validation does not replace Mender Artifact authentication for the normal field deployment path.

---

# 25. Systemd Services

Customization-sets may provide additional systemd configuration below:

```text
/etc/systemd/system/
```

For example:

```text
/etc/systemd/system/customer-example.service
/etc/systemd/system/multi-user.target.wants/customer-example.service
```

Symlinks used to enable services must be represented and validated correctly.

Be aware that allowing arbitrary `ExecStart` commands pointing to executable content supplied by a customization-set effectively turns the mechanism into software deployment.

Unless explicitly required, treat customization-sets as **configuration**, not as a general-purpose application deployment mechanism.

---

# 26. Important Implementation Principles

When implementing or modifying this subsystem:

1. Keep the rootfs read-only.
2. Restrict customization to `/etc`.
3. Compose `/etc` using STATE → USER → FACTORY → SYSROOT precedence.
4. Never modify an active USER slot.
5. Install new USER customization into the inactive A/B slot.
6. Keep FACTORY independent from USER A/B.
7. Keep STATE independent from customization.
8. Never allow STATE and customization to own the same path.
9. Activate customization only during boot.
10. Do not perform live `/etc` switching.
11. Roll back by selecting the last-known-good slot and rebooting.
12. Treat factory reset separately from rollback.
13. Make persistent metadata updates power-loss safe.
14. Validate customization content before candidate activation.
15. Do not make assumptions that A or B has a permanent role.
16. Preserve enough state to recover automatically from an unbootable candidate.
17. Support explicit deletion of lower-layer files using OverlayFS whiteout semantics.
18. Consider interaction with Core OS A/B updates whenever changing the customization format or boot logic.
19. Keep `os-customization-set` independent of Mender.
20. For normal field deployment, enforce the dependency chain `core-api-server → Mender → os-customization-set`.
21. Perform Mender Artifact signature verification in Mender, not in `os-customization-set`.
22. Do not let `core-api-server` bypass Mender for normal customer field installation.
23. Treat Mender authentication and customization content validation as separate security controls.
24. Prefer simple, deterministic boot-time behavior over complex live filesystem manipulation.

---

# 27. Desired System Properties

The implementation should maintain the following invariant:

> At every successful boot, `/etc` consists of exactly one selected USER customization, the FACTORY customization, the current Core OS `/etc`, and explicitly permitted persistent STATE.

Additionally:

> A failed candidate must never destroy the previous last-known-good USER customization.

> A Core OS update must not modify the persistent customization slots.

> A customization update must not modify the Core OS rootfs.

> Rollback must return to the last-known-good USER customization; factory reset is a separate operation.

> STATE and customization must never independently own the same `/etc` path.

> For normal field installation, `core-api-server` calls Mender, and Mender calls `os-customization-set`.

> Mender is responsible for authenticated Artifact delivery; `os-customization-set` is responsible for customization validation and lifecycle management.

The resulting architecture is:

```text
Customer
   │
   │ REST API
   ▼
core-api-server
   │
   │ Mender Artifact
   ▼
Mender
   │
   ├── signature verification
   │
   ▼
os-customization-set
   │
   ├── validation
   ├── USER A/B installation
   ├── candidate management
   ├── commit / rollback
   └── factory reset
   │
   ▼
reboot
   │
   ▼
preinit
   │
   └── constructs /etc:
           STATE
             ↓
           USER A/B
             ↓
           FACTORY
             ↓
           SYSROOT
   │
   ▼
systemd
   │
   ▼
os-customization-check.service
        │
        ├── success → commit
        │
        └── failure → last-good → reboot
```


## Original /sbin/init

This is the original `/sbin/init` script used to construct `/etc` during early boot.

```
#!/bin/sh

echo "PREINIT: Start"

PATH=/sbin:/bin:/usr/sbin:/usr/bin
if false; then
    mount -o remount,rw /

    mkdir -p /proc
    mkdir -p /sys
    mkdir -p /run
    mkdir -p /var/run
    mkdir -p /data
fi

mount -t proc proc /proc
mount -t sysfs sysfs /sys

[ -z "$CONSOLE" ] && CONSOLE="/dev/console"

BASE_OVERLAY_ETC_DIR=/data/overlay-etc
UPPER_DIR=$BASE_OVERLAY_ETC_DIR/upper
WORK_DIR=$BASE_OVERLAY_ETC_DIR/work
LOWER_DIR=$BASE_OVERLAY_ETC_DIR/lower

if mount -n -t ext4 \
    -o defaults \
    /dev/mmcblk0p4 /data
then
    mkdir -p $UPPER_DIR
    mkdir -p $WORK_DIR

    if true; then
        mkdir -p $LOWER_DIR

        # provide read-only access to original /etc content
        mount -o bind,ro /etc $LOWER_DIR
    fi

    mount -n -t overlay \
        -o upperdir=$UPPER_DIR \
        -o lowerdir=/etc \
        -o workdir=$WORK_DIR \
        -o index=off,xino=off,redirect_dir=off,metacopy=off \
        $UPPER_DIR /etc || \
            echo "PREINIT: Mounting etc-overlay failed!"
else
    echo "PREINIT: Mounting </data> failed!"
fi

echo "PREINIT: done; starti
```

## Target Testing

Use a development target configured through the documented environment
variables. Install the implementation using `deploy-test-machine.sh`; see
`docs/deployment-modes.md`.

When asked to perform target tests:

- Act like a normal administrator/customer using the documented interfaces.
- Do not manually modify /data/os-customization internals unless explicitly
  testing recovery behavior.
- Prefer public CLI/API interfaces.
- Inspect state before and after each operation. 
- Keep target test logs free of target addresses, credentials, and other
  environment-specific identifiers.
- Reboots are permitted when required by the test.
- After reboot, reconnect via SSH and continue verification.
- Verify both the externally visible /etc state and internal status.
- Do not modify the read-only root filesystem unless for installation and recovery purposes.
- Report commands executed, expected result, actual result, and deviations.
