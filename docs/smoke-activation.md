# Smoke Activation

The live init-switch smoke harness is [scripts/smoke-activate-init.py](../scripts/smoke-activate-init.py).

What it does:

1. Stages the current implementation into the target root filesystem.
2. Replaces `/sbin/init` with the preinit wrapper.
3. Enables the health-check service for the smoke run.
4. Optionally installs a customization payload before reboot.
5. Starts a serial log capture through the helper host.
6. Reboots the target.
7. Waits for SSH and `/run/os-customization/boot-selection.json` to come back.
8. When a payload is supplied, waits for either commit or rollback status.
9. If boot does not come back cleanly, logs in over serial and restores stock init automatically.
10. By default, restores stock init even after a successful smoke run.

Required inputs:

1. Target root password through `TARGET_PASSWORD` or `--target-password`.
2. Serial console root password through `SERIAL_ROOT_PASSWORD` or `--serial-root-password`.
3. SSH access to the serial helper host.

Example:

```sh
TARGET_PASSWORD=... SERIAL_ROOT_PASSWORD=... python3 scripts/smoke-activate-init.py
```

Commit example:

```sh
TARGET_PASSWORD=... SERIAL_ROOT_PASSWORD=... \
python3 scripts/smoke-activate-init.py \
	--payload samples/payload-good \
	--expected-outcome commit
```

Rollback example:

```sh
TARGET_PASSWORD=... SERIAL_ROOT_PASSWORD=... \
python3 scripts/smoke-activate-init.py \
	--payload samples/payload-bad \
	--expected-outcome rollback
```

Use `--keep-wrapper-on-success` only if you explicitly want the wrapper left active after the smoke run.
