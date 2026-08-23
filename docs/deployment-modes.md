# Deployment Modes

The test deploy helper is [scripts/deploy-test-machine.sh](../scripts/deploy-test-machine.sh).

Default mode:

1. Stage tools under `/data/os-customization-tools`.
2. Optionally install and enable the systemd units.
3. Do not modify `/sbin/init`.

Rootfs staging mode:

Run `deploy-test-machine.sh --rootfs-stage` to copy files into `/usr` and `/sbin` on the target root filesystem while leaving the active init path unchanged.

Live init switch:

Run `deploy-test-machine.sh --rootfs-stage --activate-init-wrapper` only when you explicitly want to replace `/sbin/init` with the preinit wrapper.

Recovery:

Use [scripts/restore-target-init.sh](../scripts/restore-target-init.sh) to restore the stock init path after a test.
