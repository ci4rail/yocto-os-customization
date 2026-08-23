# Mender Integration Notes

> Standalone Mender operation is implemented and tested on the development
> target. Managed Mender deployment still requires end-to-end testing. The existing
> [libexec/os-customization-mender-install](../libexec/os-customization-mender-install)
> program is an adapter, not an Update Module.

Install an `os-customization` Mender Update Module whose payload type is also
`os-customization`. It receives one archive containing the extracted
customization-set directory (`manifest.json`, `etc/`, and optional
`whiteouts.txt`), extracts it safely to a temporary directory, and invokes:

```sh
os-customization-mender-install --no-reboot <payload-dir>
```

The adapter calls `os-customization-set install <payload-dir>`, which creates
a candidate only. It does not modify live `/etc`.

This repository supplies the module at
`mender/modules/os-customization`; install it as
`/usr/share/mender/modules/v3/os-customization`. It also supplies
`mender/inventory/os-customization`, to install as
`/usr/share/mender/inventory/os-customization`. The inventory script reports
the manager's effective active, last-known-good, candidate, and factory state.

The module must support both forms of Mender operation:

* **Managed:** Return `Automatic` to `NeedsArtifactReboot`. Mender reboots and
  the module's post-reboot handling waits for the manager's health result
  before Mender commits the Artifact.
* **Standalone:** `mender-update install <artifact>` does not perform the
  reboot/verify-reboot sequence. Its caller (for example `moducop-api-server`)
  requests the reboot after a successful installation, then reads
  `os-customization-set status` after reconnecting until the candidate commits
  or rolls back. It calls `mender-update commit` only after a manager commit,
  or `mender-update rollback` after a manager rollback, to finish the local
  Mender transaction. In the latter case `ArtifactRollback` is a no-op because
  the manager has already cleared the candidate; the call only clears Mender's
  pending transaction.

In both modes, the manager's status is authoritative for active and
last-known-good versions. Mender Artifact `Provides` is only the last Artifact
transaction and can be stale after a manager-driven rollback, deactivation, or
factory reset.

When a payload specifies `compatible_core_os`, the adapter obtains the installed
Core OS version from `/etc/issue`. For example,
`Moducop-CPU01_Standard-Image_v2.11.0.51609d8.20260513.1047` yields `v2.11.0`.
The manifest may use this exact version or a range such as
`>=v2.11.0,<v3.0.0`.
`--core-os-version` remains available as an explicit override for controlled
test or recovery environments.

The adapter deliberately does not parse Mender Artifacts or implement any
deployment state machine.
