# Deployment Modes

The test deploy helper is [scripts/deploy-test-machine.sh](../scripts/deploy-test-machine.sh).

Production/rootfs mode:

1. Install tools below `/usr` and `/sbin`.
2. Optionally install and enable the systemd units.
3. Do not modify `/sbin/init` unless `--activate-init-wrapper` is supplied.

Development staging mode:

Without `--rootfs-stage`, `deploy-test-machine.sh` stages tools below the
configurable `TARGET_PREFIX` (default `/data/os-customization-tools`). This
mode is for development only: production runtime helpers do not search that
location for Python modules.

Run `deploy-test-machine.sh --rootfs-stage` to install the production paths in
`/usr` and `/sbin` while leaving the active init path unchanged.

Live init switch:

Run `deploy-test-machine.sh --rootfs-stage --activate-init-wrapper` only when you explicitly want to replace `/sbin/init` with the preinit wrapper.

Recovery:

Use [scripts/restore-target-init.sh](../scripts/restore-target-init.sh) to restore the stock init path after a test.
