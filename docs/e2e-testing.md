# End-to-End Testing

Sample payloads are in [samples/payload-good](../samples/payload-good) and [samples/payload-bad](../samples/payload-bad).

The target test driver is [scripts/e2e-test-machine.sh](../scripts/e2e-test-machine.sh).

What it verifies:

1. A valid payload installs into the inactive slot.
2. The machine reboots.
3. Boot integration records `/run/os-customization/boot-selection.json`.
4. The health-check service commits the good candidate.
5. A failing payload installs into the other slot.
6. The machine reboots again.
7. The health-check service rolls the bad candidate back.

Important limitation:

This script intentionally refuses to run unless the target boot path is already wired to [sbin/os-customization-preinit.sh](../sbin/os-customization-preinit.sh). Without that, a reboot would not actually activate the candidate overlay and the result would not be meaningful.
