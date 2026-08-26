#!/usr/bin/env python3

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


SCENARIOS = [
    "run_scenario_01_basic_file_install.py",
    "run_scenario_02_fallback_to_last.py",
    "run_scenario_03_deactivate_incompatible.py",
    "run_scenario_04_factory_reset.py",
    "run_scenario_05_install_service.py",
]


def main() -> int:
    base_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Run all target scenario scripts")
    parser.add_argument(
        "--log-dir",
        default=str(base_dir / "logs"),
        help="Directory for scenario logs",
    )
    args = parser.parse_args()
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []
    for script_name in SCENARIOS:
        script_path = base_dir / script_name
        command = [sys.executable, str(script_path), "--log-dir", str(log_dir)]
        completed = subprocess.run(command, check=False)
        if completed.returncode != 0:
            failures.append(script_name)

    if failures:
        print("FAILED:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("All target scenarios passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
