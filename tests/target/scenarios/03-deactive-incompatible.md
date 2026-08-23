# Customization which is incompatible with OS is deactivated

## Objective

Verify that a customization which is incompatible with the OS is deactivated.

## Preconditions

- Target is running normally.
- No candidate customization is pending.
- SSH access is available.

## Test

Preparation: Install a good customization-set that changes `/etc/hosts` and passes the health check, set the core-os compatibility to the current OS version, and reboot to make it last-known-good.

Then patch the /etc/issue file to contain a different OS version, and reboot.
The last-known-good customization should be deactivated because it is no longer compatible with the OS.

`etc/hosts`:

```text
127.0.0.1 localhost
127.0.0.1 target-test-01-customized

```

Install it using the normal os-customization-set interface.

