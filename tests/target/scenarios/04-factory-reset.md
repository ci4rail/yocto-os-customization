# Factory Reset

## Objective

Verify that the factory reset functionality works as expected, restoring the system to its original state.

## Preconditions

- Target is running normally.
- No candidate customization is pending.
- SSH access is available.

If the target has no factory customization, the scenario first deploys a small
example factory customization-set and uses it as the restoration baseline.

## Test

Install a factory customization-set that changes `/etc/hosts`.

Install a customization-set that changes `/etc/hosts` (different content than factory /etc/hosts) and passes the health check.

Create some arbitrary files in `/etc`

Then perform a factory reset and reboot the system.

Verify that:
1. The customization is removed and the factory version of `/etc/hosts` is restored
2. The arbitrary files in `/etc` are removed
