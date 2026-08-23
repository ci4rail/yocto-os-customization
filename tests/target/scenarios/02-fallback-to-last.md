# Fallback to last-known-good

## Objective

Verify that configuration can be rolled back to the last-known-good version if a health check fails.

## Preconditions

- Target is running normally.
- No candidate customization is pending.
- SSH access is available.

## Test

Install a good customization-set that changes `/etc/hosts` and passes the health check.

Then install a second customization-set that changes `/etc/hosts` but fails the health check.

`etc/hosts`:

```text
127.0.0.1 localhost
127.0.0.1 target-test-01-customized

```

Install it using the normal os-customization-set interface.

Verify that:

1. The good customization is installed and becomes last-known-good.
2. The bad customization is installed and becomes the candidate.
3. After reboot, the system falls back to the last-known-good `/etc/hosts`.
4. The good customization remains last-known-good.
5. `os-customization-set status` reports the expected state.

