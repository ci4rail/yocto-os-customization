# yocto-os-customization

Tools and integration scripts for delivering customer-specific `/etc`
configuration on an immutable Core OS image.

The repository implements an A/B customization flow with:

- boot-time OverlayFS composition of `STATE -> USER -> FACTORY -> SYSROOT`
- inactive-slot installation and reboot-based activation
- candidate health checks, commit, rollback, and factory reset
- optional field deployment through Mender

The main entry points are:

- `bin/os-customization-set`: CLI for validation, installation, status,
  commit, rollback, factory reset, and boot preparation
- `sbin/os-customization-preinit.sh`: early-boot setup for the `/etc` overlay
- `libexec/os-customization-check`: post-boot candidate health-check runner
- `libexec/os-customization-mender-install`: Mender-side installation helper

## Repository layout

- `python/os_customization/`: core lifecycle and validation logic
- `docs/`: user guide, deployment modes, Mender integration, and test notes
- `systemd/`: service units for health checks and factory reset
- `samples/`: example good and bad payloads
- `tests/`: unit tests and target test scenarios
- `scripts/`: deployment and target-test helpers

## Typical workflow

1. Build or prepare an extracted payload containing `manifest.json` and `etc/`.
2. Validate or install it with `os-customization-set`.
3. Reboot so preinit selects the candidate slot and mounts the overlay.
4. Let `os-customization-check.service` verify the candidate.
5. Commit on success or roll back to the last-known-good slot on failure.

## Commands

Common CLI commands:

- `os-customization-set validate <payload-dir>`
- `os-customization-set install <payload-dir>`
- `os-customization-set install-factory <payload-dir>`
- `os-customization-set status`
- `os-customization-set commit`
- `os-customization-set rollback`
- `os-customization-set factory-reset`

## Documentation

- [docs/user-guide.md](docs/user-guide.md): payload format, lifecycle, and CLI
- [docs/mender-integration.md](docs/mender-integration.md): Mender deployment path
- [docs/deployment-modes.md](docs/deployment-modes.md): development and target deployment modes

## Development

Run the local Python test suite from the repository root:

```sh
pytest
```

Target validation is separate from `pytest`:

- `tests/target/scenarios/`: Markdown scenario procedures for manual or agent-driven target testing
- `tests/target/run_all_scenarios.py`: Python wrapper that runs the per-scenario target scripts and writes logs under `tests/target/logs/`

Run test with a target device by setting the following environment variables:
e.g.
```
export TARGET_HOST=192.168.24.11
export TARGET_USER=root
export TARGET_PASSWORD=xxx
```
Before running the tests
```
scripts/deploy-test-machine.sh --rootfs-stage --activate-services
```

Production helpers load `os_customization` only from their installed `/usr`
locations. `/data/os-customization-tools` is supported solely as a
development-staging prefix for the deploy helper.
