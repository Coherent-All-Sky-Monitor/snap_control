#!/usr/bin/env python3
"""
Convenience launcher for multi_snap_config.py using configs/snap_startup.yaml.

This:
  1. Reads preferred arguments from configs/snap_startup.yaml
  2. Translates them to the CLI flags expected by src/multi_snap_config.py
  3. Invokes multi_snap_config.main() directly (no subprocess).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml  # PyYAML

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import multi_snap_config  # type: ignore

def _load_startup_config(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    if not isinstance(cfg, dict):
        raise ValueError("snap_startup.yaml must contain a mapping at top level")
    return cfg


def _build_argv_from_yaml(cfg: Dict[str, Any]) -> List[str]:
    """Translate snap_startup.yaml into argv-style arguments for multi_snap_config."""

    layout_yaml = cfg.get("layout_yaml")
    if not layout_yaml:
        raise ValueError("snap_startup.yaml must define 'layout_yaml'")

    argv: List[str] = [str(layout_yaml)]

    # IPs
    ips: Optional[List[str]] = cfg.get("ips")
    if ips:
        argv.append("--ip")
        argv.extend(str(ip) for ip in ips)

    # Options block
    opts: Dict[str, Any] = cfg.get("options") or {}

    nchan_packet = opts.get("nchan_packet")
    if nchan_packet is not None:
        argv.extend(["--nchan-packet", str(nchan_packet)])

    log_level = opts.get("log_level")
    if log_level:
        argv.extend(["--log-level", str(log_level)])

    if opts.get("programmed"):
        argv.append("--programmed")

    test_mode = opts.get("test_mode")
    if test_mode:
        argv.extend(["--test-mode", str(test_mode)])

    fft_shift = opts.get("fft_shift")
    if fft_shift is not None:
        argv.extend(["--fft_shift", str(fft_shift)])

    eq_coeffs = opts.get("eq_coeffs")
    if eq_coeffs is not None:
        argv.extend(["--eq_coeffs", str(eq_coeffs)])

    adc_gain = opts.get("adc_gain")
    if adc_gain is not None:
        argv.extend(["--adc_gain", str(adc_gain)])

    return argv


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Start SNAP boards using configs/snap_startup.yaml"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "configs" / "snap_startup.yaml",
        help="Path to startup YAML (default: configs/snap_startup.yaml)",
    )
    args = parser.parse_args()

    cfg = _load_startup_config(args.config)
    argv = _build_argv_from_yaml(cfg)

    # Emulate command-line invocation of multi_snap_config.py
    sys.argv = ["multi_snap_config.py", *argv]
    multi_snap_config.main()


if __name__ == "__main__":
    main()


