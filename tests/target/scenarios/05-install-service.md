# Install a custom systemd service

## Objective

Verify that a custom systemd service can be installed, activated and checked through the os-customization-set interface.

## Preconditions

- Target is running normally.
- No candidate customization is pending.
- SSH access is available.
- `systemd-analyze` is installed so installation-time unit validation runs.

## Test

Install a customization-set that contains a custom systemd service unit file and passes the health check.

The service shall be enabled with an `/etc/systemd/system/multi-user.target.wants/`
symlink. Copy the target's `/bin/sh` binary (dereferencing any symlink) into
the staged payload at `etc/os-customization-target-test-05/bin/sh`, with mode
0755. Keep the basename `sh` for BusyBox compatibility. The service's
`ExecStart` directly references `/etc/os-customization-target-test-05/bin/sh`
and supplies a `-c` command that writes the expected marker to `/tmp`.
This exercises validation of an executable supplied by the candidate before
it becomes visible in the running `/etc`. The manifest uses a
`systemd_unit_active` health check for that service.

Verify that:
1. The binary is absent from the running `/etc` before installation and remains
   absent after installation until reboot; the marker is also absent before reboot.
2. After customization set activation, the binary is executable and the service
   is enabled and started.
3. The service executes the payload binary and creates the expected file in `/tmp`.
   
