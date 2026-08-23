# Whiteouts

## Objective

Verify that whiteouts are handled correctly.

## Preconditions

- Target is running normally.
- No candidate customization is pending.
- SSH access is available.

## Test

Install a customization-set that whites out `/etc/wgetrc`. Reboot the system and verify that `/etc/wgetrc` is gone from /etc.

Then perform a factory reset and reboot the system.

Verify that `/etc/wgetrc` is restored.
