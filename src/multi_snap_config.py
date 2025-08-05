#!/usr/bin/env python3
"""
Multi‑SNAP board configuration utility for CASM
================================================

This script automates the following workflow for an arbitrary number of SNAP
boards described in a YAML configuration file:

1. Reads board‑specific networking and firmware parameters from YAML.
2. Establishes a control connection to each SNAP (via the Raspberry‑Pi/KATCP or
   MicroBlaze interface – whichever the firmware presents).
3. Programs the FPGA with the requested *.fpg* bitstream (unless already
   programmed), including automatic ADC link training and block initialisation.
4. Executes :py:meth:`casm_f.snap_fengine.SnapFengine.configure` with the
   appropriate ``source`` and ``destination`` parameters so that each board
   streams its part of the 4096‑channel band to the requested downstream
   NIC(s) and frequency ranges.
5. Prints a concise per‑board status summary (GB/s, packet‑rate, error flags).

The top‑level key **boards** is a list so you can describe any number of SNAPs.

Usage
-----

.. code:: bash

   $ ./multi_snap_config.py casm_feng_layout.yaml [--nchan-packet 512]

Requirements
~~~~~~~~~~~~
* Python ≥ 3.8
* ``casm_f`` installed (see CASM SNAP F‑Engine docs).
* Network reachability to all Raspberry‑Pis.

"""
from __future__ import annotations

import argparse
import ipaddress
import logging
import sys
from pathlib import Path
from typing import Dict, List

import yaml  # PyYAML

# CASM library import — errors out cleanly if missing
try:
    from casm_f import snap_fengine  # type: ignore
except ImportError as exc:  # pragma: no cover
    sys.exit(
        "casm_f not importable – install it in your Python environment first "
        "(see https://github.com/casm-project/casm_f)."
    )

LOGGER = logging.getLogger("multi_snap_config")


# -----------------------------------------------------------------------------
# Helper utilities
# -----------------------------------------------------------------------------

def _mac_to_int(mac_str: str | int) -> int:
    """Convert a MAC address expressed as *xx:xx:xx:xx:xx:xx*, *0xABCDEF* or
    already‑integer into an integer suitable for *SnapFengine.configure*.
    """
    if isinstance(mac_str, int):
        return mac_str
    mac_str = mac_str.strip()
    if mac_str.startswith("0x"):
        return int(mac_str, 16)
    return int(mac_str.replace(":", ""), 16)


def _load_layout(path: Path) -> List[dict]:
    """Parse the YAML layout file and do some basic validation."""
    with path.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if not isinstance(cfg, dict) or "boards" not in cfg:
        raise ValueError("YAML does not contain top‑level 'boards' key")
    if not isinstance(cfg["boards"], list):
        raise ValueError("'boards' must be a list")
    return cfg["boards"]


# -----------------------------------------------------------------------------
# Main per‑board routine
# -----------------------------------------------------------------------------

def _configure_board(board_def: dict, nchan_packet: int) -> None:
    """Configure a single SNAP board according to *board_def*."""

    host = board_def["host"]
    feng_id = int(board_def.get("feng_id", 0))
    fpgfile = board_def.get("fpgfile")

    # Build the mac‑lookup table (destinations + source)
    macs: Dict[str, int] = {}

    src = board_def["source"]
    source_ip = str(src["ip"])
    source_port = int(src.get("port", 10000))
    macs[source_ip] = _mac_to_int(src["mac"])

    dests: List[dict] = []
    for dest in board_def["destinations"]:
        ip = str(dest["ip"])
        macs[ip] = _mac_to_int(dest["mac"])
        dests.append(
            {
                "ip": ip,
                "port": int(dest.get("port", 10000)),
                "start_chan": int(dest["start_chan"]),
                "nchan": int(dest["nchan"]),
            }
        )
    return macs, dests

    LOGGER.info("Connecting to %s …", host)
    feng = snap_fengine.SnapFengine(host)

    LOGGER.info("Configuring %s (feng_id=%d) – source %s:%s", host, feng_id, source_ip, source_port)

    feng.configure(
        source_ip=source_ip,
        source_port=source_port,
        program=True,
        fpgfile=fpgfile,
        dests=dests,
        macs=macs,
        nchan_packet=nchan_packet,
        sw_sync=True,  # Software PPS for lab‑style bring‑up; disable in production
        enable_tx=True,
        feng_id=feng_id,
    )

    # Quick status summary
    eth_status, flags = feng.eth.get_status()  # type: ignore[attr-defined]
    LOGGER.info(
        "%s: tx %.2f Gb/s – packets %d pps – flags %s", host, eth_status["gbps"], eth_status["tx_ctr"], flags
    )


# -----------------------------------------------------------------------------
# CLI entry point
# -----------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Configure multiple CASM SNAP boards from YAML")
    ap.add_argument("layout_yaml", type=Path, help="Board/NIC/frequency layout file (YAML)")
    ap.add_argument("--nchan-packet", type=int, default=512, help="Number of frequency channels per UDP packet")
    ap.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return ap.parse_args()


def main() -> None:  # pragma: no cover
    args = _parse_args()
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=args.log_level)

    try:
        boards = _load_layout(args.layout_yaml)
    except Exception as exc:
        LOGGER.error("Failed to parse YAML layout: %s", exc)
        sys.exit(1)

    for board_def in boards:
        try:
            _configure_board(board_def, nchan_packet=args.nchan_packet)
        except Exception:
            LOGGER.exception("Configuration failed for board %s", board_def.get("host"))
            continue

    LOGGER.info("All requested boards processed.")


if __name__ == "__main__":  # pragma: no cover
    main()
