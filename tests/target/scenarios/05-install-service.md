# Install a custom systemd service

## Objective

Verify that a custom systemd service can be installed, activated and checked through the os-customization-set interface.

## Preconditions

- Target is running normally.
- No candidate customization is pending.
- SSH access is available.

## Test

Install a customization-set that contains a custom systemd service unit file and passes the health check.

The service shall be enabled with an `/etc/systemd/system/multi-user.target.wants/`
symlink. It executes a shell script that is also part of the customization-set
and writes a file to `/tmp` when executed. The manifest uses a
`systemd_unit_active` health check for that service.

Verify that:
1. After customization set activation, the service is enabled and started.
2. The service executes the shell script and creates the expected file in `/tmp`.
   
