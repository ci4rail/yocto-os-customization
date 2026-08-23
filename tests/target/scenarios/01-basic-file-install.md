# Install basic customization

## Objective

Verify that a user can install a customization-set that overrides
an existing file in /etc.

## Preconditions

- Target is running normally.
- No candidate customization is pending.
- SSH access is available.

## Test

Create a customization-set that changes `/etc/hosts`.

Use this payload content for the target test:

`manifest.json`:

```json
{
  "format_version": 1,
  "version": "target-test-01-hosts-1.0.0",
  "compatible_core_os": null,
  "health_checks": [
    {
      "type": "path_exists",
      "path": "/etc/hosts"
    }
  ]
}
```

`etc/hosts`:

```text
127.0.0.1 localhost
127.0.0.1 target-test-01-customized

# Scenario 01: user customization override
```

Install it using the normal os-customization-set interface.

Verify that:

1. The currently running `/etc/hosts` is not changed immediately.
2. The new customization becomes the candidate.
3. After reboot, the customized `/etc/hosts` is visible.
4. The health check succeeds.
5. The customization becomes last-known-good.
6. `os-customization-set status` reports the expected state.

## Cleanup

Restore the original configuration or perform the appropriate reset.
