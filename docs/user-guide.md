# Core OS customization user guide

Core OS customization lets a device use customer-specific configuration without
modifying its immutable Core OS image. A customization is activated only at
boot. It can therefore be tested as a complete configuration and, if it fails,
the device returns to its previous known-good configuration on the next boot.

This guide describes the payload format, the configuration lifecycle, and the
programs supplied by this repository. It applies to format version 1.

## What a customization can change

A payload may provide configuration only below `/etc`. Examples include:

```text
etc/chrony.conf
etc/nftables.conf
etc/snmp/snmpd.conf
etc/ssh/authorized_keys/root
etc/systemd/system/customer.service
```

It must not contain application binaries or files outside `/etc`, such as
`usr/bin/`, `var/lib/`, or `boot/`. A payload is configuration, not a general
software-deployment package.

## Effective `/etc`

At boot, the preinit program mounts an OverlayFS over `/etc`. The effective
configuration has this precedence, from highest to lowest:

```text
STATE       persistent, writable device/application state
USER        selected customer customization (user-A or user-B)
FACTORY     factory customization
SYSROOT     /etc in the installed Core OS image
```

The first layer containing a path wins. `STATE` is reserved for explicitly
approved runtime state. Do not put a customer-managed file in STATE: it would
hide the USER, FACTORY, and SYSROOT versions and prevent an update or rollback
from changing that path.

Factory configuration remains on the persistent data partition and is always
available. It is not a user A/B slot.

The default persistent layout is:

```text
/data/os-customization/
├── factory/             factory manifest and etc/ tree
├── user-A/              one complete USER payload
├── user-B/              the other complete USER payload
├── staging/             temporary installation workspace
└── status.json          slot and candidate state
```

The writable STATE layer deliberately remains at the location used by older
images that predate `os-customization`:

```text
/data/overlay-etc/
├── upper/               writable OverlayFS upper directory
├── work/                OverlayFS work directory
└── lower/               legacy bind-mount directory; no longer used
```

This preserves existing changes under `/etc` when such a device is upgraded to
an image with `os-customization`: preinit reuses `upper` and `work` instead of
creating a new empty STATE layer.  `upper` is STATE, so it has higher precedence
than USER and FACTORY customizations.  A factory reset with `--wipe-state`
clears this legacy STATE directory as well.

### Factory customization directory

Provision the factory customization during manufacturing or another trusted
recovery procedure at `/data/os-customization/factory`. A complete factory set
has the same top-level layout as a USER payload:

```text
/data/os-customization/factory/
├── manifest.json
└── etc/
    └── ... factory configuration below /etc ...
```

Use at least the following manifest:

```json
{
  "format_version": 1,
  "version": "factory-1.0.0",
  "compatible_core_os": "v2.11.0"
}
```

`version` is reported as `factory_version` by `os-customization-set status`.
Keep `manifest.json` with the factory set even though the current boot code can
mount `factory/etc` without one; without the manifest, the factory version is
unknown and the set has no format metadata.

Install a trusted factory payload with:

```sh
os-customization-set install-factory /path/to/factory-payload
```

The operation stages the complete set and atomically replaces
`/data/os-customization/factory`. It does not use USER A/B slots, does not
create a candidate, and does not reboot; reboot after installation to make the
new FACTORY layer effective.

Factory installation deliberately does not run USER payload validation, Core
OS compatibility checks, built-in syntax validators, or health checks. It is
therefore restricted to trusted manufacturing and recovery environments.
`os-customization-set install` is not suitable for factory provisioning because
it only installs USER A/B candidates.

## Payload format

A payload is an already extracted directory. Its minimum layout is:

```text
my-customization/
├── manifest.json
└── etc/
    └── ... configuration files below /etc ...
```

For example:

```text
site-config-1.4.2/
├── manifest.json
└── etc/
    ├── chrony.conf
    ├── ssh/
    │   └── authorized_keys/
    │       └── root
    └── systemd/
        └── system/
            └── customer-example.service
```

### `manifest.json`

The manifest must be valid JSON and contain `format_version` and `version`.
`compatible_core_os` is optional, but strongly recommended for a payload that
depends on a particular image release. `health_checks` is optional.

```json
{
  "format_version": 1,
  "version": "1.4.2",
  "compatible_core_os": "v2.11.0",
  "health_checks": [
    {
      "type": "systemd_unit_active",
      "unit": "customer-example.service"
    },
    {
      "type": "path_exists",
      "path": "/etc/chrony.conf"
    }
  ]
}
```

`format_version` must be the number `1`. `version` is an identifier displayed
in status information; use a stable version string that identifies the payload.

#### Core OS compatibility

When `compatible_core_os` is set, it may be an exact version or a
comma-separated range. The installer obtains the installed Core OS version from
`/etc/issue`. For example, this image identifier:

```text
Moducop-CPU01_Standard-Image_v2.11.0.51609d8.20260513.1047
```

is interpreted as `v2.11.0`.

An exact version, such as `v2.11.0`, is equivalent to an equality constraint.
For a range, separate constraints with commas; every constraint must match.
Versions may use an optional `v` prefix and must contain a major and minor
component, with an optional patch component.

```json
"compatible_core_os": ">=v2.11.0,<v3.0.0"
```

Supported comparison operators are `=`, `==`, `>`, `>=`, `<`, and `<=`. Thus
`>=2.11,<3.0` accepts `v2.11.0`, while `>=2.12,<3.0` does not. An invalid range,
or a range that does not include the installed version, is rejected before the
payload can become a candidate. A constrained payload is also rejected when
the version cannot be read from `/etc/issue`.

`compatible_core_os: null` omits the compatibility check. It is useful only
when the payload is deliberately compatible with all supported Core OS images.

Compatibility is checked again during early boot, before the OverlayFS is
mounted. This protects independently deployed Core OS updates: a USER payload
that no longer matches the newly booted Core OS is excluded from `/etc`, and
boot continues using a compatible last-known-good USER payload or, if none is
compatible, FACTORY plus SYSROOT. An incompatible pending candidate is disabled
(but remains stored in its slot) so that it cannot block a later installation.
Factory customization is similarly excluded when its manifest declares an
incompatible Core OS range. The boot-selection marker and `status` report the
selection reason.

#### Health checks

Health checks run after the candidate payload has been selected at boot. Every
check must pass for the candidate to be committed. Supported checks are:

| Type | Required fields | Meaning |
| --- | --- | --- |
| `command` | `command` | Runs a non-empty command argument list. Exit status 0 passes. |
| `systemd_unit_active` | `unit` | Passes when `systemctl is-active --quiet <unit>` succeeds. |
| `path_exists` | `path` | Passes when the path exists after the overlay is mounted. |

Example command check:

```json
{
  "type": "command",
  "command": ["/usr/bin/customer-connectivity-check", "--quiet"]
}
```

Only reference commands supplied by the Core OS. A configuration-set must not
depend on executable files delivered by the payload.

### Files, directories, and symlinks

The `etc/` tree is copied to the selected USER slot. Regular files, directories,
and symlinks are supported. Other file types are rejected. All paths must
remain beneath `/etc`; traversal components and symlinks that resolve outside
that namespace are rejected.

Systemd unit files and enabling symlinks can be supplied below
`etc/systemd/system/`. Be careful: a unit that invokes arbitrary commands can
effectively become a software-deployment mechanism. Use units only for
configuration of trusted Core OS programs.

### Inherited paths

Omitting a path means **inherit** it from FACTORY or SYSROOT. Format version 1
does not support deleting inherited paths.

## Installation and activation lifecycle

Installing and activating are separate actions:

```text
validate payload
  -> copy it to the unused USER slot
  -> mark that slot as candidate
  -> reboot
  -> boot candidate configuration
  -> health checks
      -> pass: commit as active and last-known-good
      -> fail: select last-known-good and reboot
```

The manager never changes the running `/etc` during installation. It writes a
complete payload to a temporary directory, syncs it, and atomically replaces
only an unused slot. The active and last-known-good slots are protected from
being overwritten.

There are two USER slots, `user-A` and `user-B`. Their roles are selected by
the status file; neither slot has a permanent role. A new payload becomes a
candidate in the inactive slot. After a successful health check it becomes both
the active and last-known-good slot.

During early boot, each candidate boot increments its attempt count. The
default maximum is three attempts. If the candidate prevents the health check
from running repeatedly, it is exhausted and boot returns to the previous
last-known-good slot. If there is no last-known-good USER payload, the device
uses FACTORY and SYSROOT.

### Rollback and factory reset

Rollback and factory reset have different results ("Operation" column refers to the `os-customization-set` subcommand):

| Operation | Result |
| --- | --- |
| `rollback` command | Selects the previous last-known-good USER payload. Reboot to make the selection effective. |
| `factory-reset` command | Disables all USER payload selection and clears STATE when invoked with `--wipe-state`. Reboot to use FACTORY plus SYSROOT. |

The health-check helper reboots after an automatic rollback. The supplied
factory-reset systemd service also reboots after it has run.

Factory reset does not need to erase the A/B slot contents; those slots are
inactive and cannot affect `/etc` after the reset.

## Command-line interface

`os-customization-set` manages an extracted payload and its lifecycle. It is
independent of Mender and does not parse or authenticate Mender Artifacts.

```sh
# Verify structure, compatibility, and built-in configuration syntax.
os-customization-set validate /path/to/payload

# Install into the unused USER slot and mark it as a candidate. Reboot later.
os-customization-set install /path/to/payload

# Atomically replace the trusted factory set. Reboot later.
os-customization-set install-factory /path/to/factory-payload

# Inspect active, candidate, and last-known-good slots and versions.
os-customization-set status

# Commit the current candidate, normally called by the health-check service.
os-customization-set commit

# Select the last-known-good payload, normally followed by a reboot.
os-customization-set rollback

# Disable USER selection. Add --wipe-state to remove STATE /etc content.
os-customization-set factory-reset --wipe-state
```

Useful internal commands are `boot-prepare`, `boot-prepare-shell`,
`run-health-checks`, and `verify-candidate-activation`. They are intended for
preinit and the health-check service rather than normal manual use.

The global option `--root <path>` selects the persistent layout root; it
defaults to `/data/os-customization`. `--max-attempts <n>` changes the candidate
boot-attempt limit. `--issue-path <path>` changes the `/etc/issue` source used
for compatibility detection. The `validate` and `install` subcommands also
accept `--core-os-version <version>` as an explicit compatibility override for
test or recovery environments.

Built-in installation-time validation invokes `sshd -t`, `nft -c`, and
`systemd-analyze verify` when the corresponding configuration and validator are
present. These checks supplement, but do not replace, the runtime health checks.

## Mender field deployment

> **Status:** Standalone Mender operation has been tested on the development
> target. Managed Mender deployment and its post-reboot transaction hand-off
> remain to be tested end to end.

The normal customer field deployment path is:

```text
customer -> core-api-server -> Mender -> os-customization-set
```

### Required Mender integration

Yes: this design requires a small custom Mender Update Module, for example
named `os-customization`, installed in Mender's Update Module directory. Its
payload type must match that module name. Mender does not know how to turn a
generic Artifact payload into an `os-customization-set install` call without
such a module.

The Update Module is Mender-specific glue; the manager remains transport
independent. Its responsibilities are deliberately narrow:

| Mender action | Module action |
| --- | --- |
| `ArtifactInstall` | Obtain the one extracted customization payload, then call `os-customization-set install <directory>`. This marks an unused USER slot as candidate; it must not modify live `/etc`. |
| `NeedsArtifactReboot` | Return `Automatic`, so Mender performs the single required reboot after a successful installation. The module must not call `systemctl reboot` itself. |
| `SupportsRollback` | Return `Yes`. |
| `ArtifactRollback` | Call `os-customization-set rollback` if the candidate is still selected, thereby selecting the last-known-good USER slot. This operation must be idempotent. |
| `ArtifactCommit` | Do not independently declare the candidate healthy. The customization health-check path must first have committed it. |

The exact hand-off between Mender's `ArtifactVerifyReboot`/`ArtifactCommit`
states and `os-customization-check.service` must be implemented and tested as
one state machine. In particular, Mender must not record a successful Artifact
before the candidate's runtime health checks pass. A practical design is for
the module's post-reboot verification to wait for the customization manager to
report either `committed` or `rolled-back`; on rollback it returns failure so
Mender enters its rollback path. Do not use the current adapter's implicit
reboot when it is called by the module.

### Managed and standalone operation

The **same** `os-customization` Update Module must work in both Mender managed
mode and Mender standalone mode. In both modes, `ArtifactInstall` must invoke
the manager with `--no-reboot`, leaving a complete candidate in the inactive
USER slot. It must never write the payload directly to `/etc`.

In managed mode, the module returns `Automatic` for `NeedsArtifactReboot`, so
Mender performs the reboot and can use its post-reboot states to wait for the
customization health result.

In standalone mode, `mender-update install <artifact>` does not execute
Mender's reboot/verify-reboot states. Its successful return means only that
the Artifact was accepted and the candidate was installed; it does **not** mean
that the customization is active or healthy. The standalone caller—for example
`moducop-api-server`—must then request the reboot, reconnect, and poll
`os-customization-set status` until the candidate is committed or rolled back.
If the manager committed the candidate, it then calls `mender-update commit` to
finish the local Mender transaction. If the manager rolled it back, it calls
`mender-update rollback` to clear that transaction. At this point the Update
Module's `ArtifactRollback` is intentionally a no-op: it detects that the
manager already cleared `candidate_slot` and must not roll back the
last-known-good configuration again. It must report the final manager result
to its caller.

For a successful standalone installation, use this sequence:

```sh
mender-update install site-config-1.4.2.mender
reboot

# After reconnecting, wait for os-customization-check.service to finish.
os-customization-set status
```

Let `EXPECTED_VERSION` be the `version` from the customization-set manifest
packaged in the Artifact. Interpret the status output as follows:

| Status fields | Meaning | Required Mender action |
| --- | --- | --- |
| `candidate_slot` is `A` or `B`; `candidate_state` is `pending` | The candidate has not completed its health decision. This includes the period before reboot and while the post-boot health check is running. | Wait. Do not commit or roll back unless the deployment is being intentionally cancelled. |
| `candidate_slot` is `null`; `candidate_state` is `null`; `active_slot` equals `last_good_slot`; `active_version` and `last_good_version` both equal `EXPECTED_VERSION` | The new candidate passed health checks and became the active last-known-good customization. | Run `mender-update commit`. |
| `candidate_slot` is `null`; `candidate_state` is `rolled-back`, `exhausted`, or `incompatible-core-os` | The new candidate was not accepted. `active_version` may show the previous last-known-good customization, or be `null` for a first-ever candidate. | Run `mender-update rollback` to clear Mender's pending Artifact transaction. |
| Any other combination | The lifecycle state is incomplete or unexpected. | Do not commit. Preserve the status output and investigate before taking another Mender action. |

In practice, after reboot, keep running `os-customization-set status` until
`candidate_slot` becomes `null`. If `candidate_state` is `rolled-back`,
`exhausted`, or `incompatible-core-os`, run `mender-update rollback` at that
point. Do not roll back while `candidate_slot` is still `A` or `B` with
`candidate_state` `pending`, unless intentionally cancelling the deployment.

For example, an Artifact carrying manifest version `sample-good-1.1.1` is
committed only when the status shows:

```json
{
  "active_version": "sample-good-1.1.1",
  "last_good_version": "sample-good-1.1.1",
  "candidate_slot": null,
  "candidate_state": null
}
```

Then run:

```sh
mender-update commit
```

Do **not** use `mender-update install commit`; `commit` is a top-level Mender
command.

If the health check rolls the candidate back, clear Mender's pending transaction
with:

```sh
mender-update rollback
```

The same command also cancels an installed candidate before reboot. In that
case the Update Module asks `os-customization-set` to roll back the pending
candidate; it does not modify the running `/etc`.

This is intentional: `os-customization-check.service` and the manager retain
authority for runtime health and rollback in both modes. Standalone Mender's
local Artifact/`Provides` record is transport history only and must not be
presented as the active customization version.

### Artifact payload

The Mender Artifact should contain exactly one payload file: an archive of one
already extracted customization-set directory. Its archive root contains:

```text
manifest.json
etc/
```

The module extracts that archive into a private temporary directory, checks
that it has precisely this top-level layout, and passes that directory to the
manager. The manager remains responsible for validating the manifest, path
restrictions, symlinks, syntax, and Core OS compatibility. The module must
reject additional payload files and must use extraction rules that cannot write
outside its temporary directory.

For example, the Artifact is created with a custom payload type and explicit
customization version (replace the device type and paths as appropriate):

```sh
mender-artifact write module-image \
  -n site-config-1.4.2 \
  -t <device-type> \
  -T os-customization \
  --software-filesystem data-partition \
  --software-name os-customization \
  --software-version 1.4.2 \
  -f site-config-1.4.2.tar \
  -o site-config-1.4.2.mender
```

Mender authenticates the signed Artifact and verifies the payload it delivers
to the module. The customization manager separately validates the unpacked
content. These controls answer different questions.

### Version reporting and rollback

Mender's ordinary Artifact `Provides` value records the version of the last
Artifact it successfully processed. It is useful deployment metadata, but it
is **not** authoritative for the effective customization: a later automatic
rollback, incompatibility deactivation, or factory reset can change the active
USER slot without a new Mender Artifact transaction.

`os-customization-set status` is authoritative. It reports
`active_version`, `last_good_version`, `candidate_version`, and their slots,
as well as candidate state. `core-api-server` should query and expose this
status for the customer-facing active configuration. It should not infer it
from the Mender Artifact name or `show-provides`.

For Mender inventory, add an inventory script that reads the manager status and
reports separate values such as `os_customization_active_version`,
`os_customization_last_good_version`, and `os_customization_candidate_state`.
Those values are refreshed after a rollback, deactivation, or factory reset;
the static Artifact `Provides` value is not. If Mender deployments must avoid
reinstalling an Artifact after a rollback, make that policy explicit in the
server/API layer rather than treating the static `Provides` value as the live
version.

Do not bypass Mender for normal customer field deployment. Direct use of
`os-customization-set install` is intended for trusted manufacturing,
development, recovery, and automated test environments.

`os-customization-mender-install` is a useful helper for development and may
be used by the future Update Module with `--no-reboot`. Its current options
are:

```text
--root <path>              customization root (default: /data/os-customization)
--core-os-version <value>  explicit compatibility override
--issue-path <path>        issue file to inspect (default: /etc/issue)
--no-reboot                install only; do not request system reboot
```

The environment variables `OS_CUSTOMIZATION_ROOT`,
`OS_CUSTOMIZATION_CORE_OS_VERSION`, and `OS_CUSTOMIZATION_ISSUE_PATH` provide
the corresponding defaults.

## Boot and service programs

### `sbin/os-customization-preinit.sh`

This is the early-boot wrapper that makes a selected payload effective. It:

1. Mounts `/proc`, `/sys`, and `/run`.
2. Mounts the persistent data device at `/data`.
3. Calls `os-customization-set boot-prepare-shell` to select the USER layer and
   record `/run/os-customization/boot-selection.json`.
4. Creates a read-only bind mount of the original Core OS `/etc` at
   `/run/rootfs-etc`.
5. Mounts the OverlayFS over `/etc` with STATE as upper directory and USER,
   FACTORY, and SYSROOT as lower directories.
6. Starts the real init program.

If the data mount, manager, or overlay mount fails, the wrapper logs the failure
and continues with the unmodified Core OS `/etc`. The wrapper is an integration
component: it must be installed as the boot init path for candidate activation
and automatic rollback to be meaningful.

Its key environment overrides include `DATA_DEVICE`, `DATA_MOUNT`,
`OS_CUSTOMIZATION_ROOT`, `MANAGER`, `REAL_INIT`, and `FALLBACK_REAL_INIT`.

### `libexec/os-customization-check` and its systemd service

`os-customization-check.timer` queues `os-customization-check.service` during
boot. The service runs after `multi-user.target`, so services pulled in by that
target have completed their start jobs before manifest health checks run. When
a candidate is pending, its helper:

1. Verifies that the early boot marker says this candidate was actually
   selected.
2. Runs the manifest health checks.
3. Commits the candidate on success.
4. Rolls it back and requests a reboot on failure.

Set `OS_CUSTOMIZATION_REBOOT=0` when running the helper manually and you need
to inspect a failed candidate before rebooting. The helper accepts runtime
configuration through `OS_CUSTOMIZATION_ROOT`,
`OS_CUSTOMIZATION_MAX_ATTEMPTS`, and
`OS_CUSTOMIZATION_BOOT_SELECTION_PATH`.

### `os-customization-factory-reset.service`

This one-shot service runs only when
`/run/os-customization-factory-reset` exists. It invokes:

```sh
os-customization-set factory-reset --wipe-state
```

then removes the request marker and reboots. Enable the service only in systems
where the mechanism used to create that marker is appropriately authorized.


## Status and troubleshooting

`os-customization-set status` reports `active_slot`, `active_version`,
`last_good_slot`, `last_good_version`, `candidate_slot`, `candidate_version`,
`candidate_state`, `candidate_attempts`, and `factory_version`. Reported active,
last-known-good, and factory versions are read from the respective manifests.

Common problems:

| Symptom | Likely cause and action |
| --- | --- |
| Installation rejects the payload | Check `manifest.json`, the `etc/` tree, and `compatible_core_os`; run `validate` first. |
| Compatibility mismatch | Compare the manifest value with the version extracted from `/etc/issue`. |
| Candidate is not committed | Inspect the health-check service logs and the health-check definition. |
| Candidate repeatedly boots then disappears | It exceeded the boot-attempt limit; inspect preinit logs and the candidate configuration. |
| No overlay after reboot | Ensure the preinit wrapper is the active init path and that `/data` can mount. |
| A user update does not affect a file | Check whether that path exists in the higher-precedence STATE layer. |

The preinit wrapper writes messages prefixed `PREINIT:` to the console and,
when writable, to `/dev/kmsg`. The status file is stored at
`/data/os-customization/status.json`.
