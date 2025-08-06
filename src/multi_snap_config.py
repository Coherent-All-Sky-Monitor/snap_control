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
* Network reachability to all SNAPs.

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
# YAML helpers
# -----------------------------------------------------------------------------

def _mac_to_int(mac: Union[str, int]) -> int:
    """Convert a MAC address given as *str* (colon or hex) or *int* -> int."""
    if isinstance(mac, int):
        return mac
    mac = mac.strip()
    if mac.startswith("0x"):
        return int(mac, 16)
    return int(mac.replace(":", ""), 16)


def _load_layout(path_like: Union[str, Path]) -> Tuple[dict, List[dict]]:
    """Load YAML from *path_like* (``str`` or ``Path``).

    Returns ``(common_cfg, boards_list)`` after basic validation so callers can
    use either ``_load_layout('file.yaml')`` or ``_load_layout(Path('file.yaml'))``.
    """
    path = Path(path_like)
    with path.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if not isinstance(cfg, dict):
        raise ValueError("Top‑level YAML must be a mapping (dict)")

    common = cfg.get("common")
    boards = cfg.get("boards")
    if common is None or boards is None:
        raise ValueError("YAML requires both 'common' and 'boards' blocks")
    if not isinstance(boards, list):
        raise ValueError("'boards' must be a list")
    return common, boards

# -----------------------------------------------------------------------------
# Configuration logic
# -----------------------------------------------------------------------------

def _configure_board(board: dict, common: dict, nchan_packet_cli: Optional[int], snap_ip: Optional[str]) -> None:
    """Configure one SNAP board using *common* defaults + *board* overrides."""

    host = board["host"]
    feng_id = int(board.get("feng_id", 0))

    # ---------- Common parameters ----------
    fpgfile = common["fpgfile"]
    source_port = int(common.get("source_port", 10000))
    dest_port = int(common.get("dest_port", 13000))
    nchan_packet = int(common.get("nchan_packet", nchan_packet_cli or 512))
    nchan_default = int(common.get("nchan", nchan_packet))

    
    if snap_ip:
        source_ip = snap_ip
    else:
        # ---------- Per‑board overrides ----------
        source_ip = board["source_ip"]

    source_mac = board["source_mac"]

    macs: Dict[str, int] = {source_ip: _mac_to_int(source_mac)}
    dests: List[dict] = []

    for dest in common["destinations"]:
        ip = dest["ip"]
        dests.append(
            {
                "ip": ip,
                "port": dest_port,
                "start_chan": int(dest["start_chan"]),
                "nchan": int(dest.get("nchan", nchan_default)),
            }
        )
        macs[ip] = _mac_to_int(dest["mac"])

    LOGGER.info("Connecting to %s at IP=%s …" % (host,source_ip))

    try:
        snap = snap_fengine.SnapFengine(source_ip, use_microblaze=True)
    except RuntimeError:
        print("time delay thing")
        exit()
        
    LOGGER.info(
        "Configuring %s (feng_id=%d) – src %s:%d → %d dests",
        host,
        feng_id,
        source_ip,
        source_port,
        len(dests),
    )

    snap.configure(
        source_ip=source_ip,
        source_port=source_port,
        program=True,
        fpgfile=fpgfile,
        dests=dests,
        macs=macs,
        nchan_packet=nchan_packet,
        sw_sync=False,
        enable_tx=True,
        feng_id=feng_id,
    )

    eth_status, flags = snap.eth.get_status()  # type: ignore[attr‑defined]
    LOGGER.info(
        "%s: tx %.2f Gb/s – packets %d pps – flags %s",
        host,
        eth_status["gbps"],
        eth_status["tx_ctr"],
        flags,
    )

# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Configure multiple CASM SNAP boards from YAML (common + boards schema)"
    )
    ap.add_argument("layout_yaml", type=Path, help="YAML layout file")
    ap.add_argument("--ip", type=str, nargs='+', help="IP address(es) of the SNAP to configure (single or multiple)", default=None)
    ap.add_argument("--nchan-packet", type=int, default=None, help="Override common.nchan_packet")
    ap.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return ap.parse_args()


def main() -> None:  # pragma: no cover
    args = _parse_args()
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=args.log_level
    )

    try:
        common, boards = _load_layout(args.layout_yaml)
    except Exception as exc:
        LOGGER.error("Failed to parse YAML layout: %s", exc)
        sys.exit(1)

    for board in boards:
        # If IP addresses are provided, configure the board with the given IP address
        if args.ip is not None:
            # Handle multiple IP addresses
            for ip in args.ip:
                try:
                    _configure_board(board, common, args.nchan_packet, ip)
                except Exception:
                    LOGGER.exception("Configuration failed for board %s with IP %s", board.get("host"), ip)
                    continue
            continue
        
        # If no IP addresses are provided, configure all boards from the yaml file
        elif args.ip is None:
            try:
                _configure_board(board, common, args.nchan_packet)
            except Exception:
                LOGGER.exception("Configuration failed for board %s", board.get("host"))
                continue
            else:
                print("Invalid argument")
                exit()
            break

    LOGGER.info("All requested boards processed.")


if __name__ == "__main__":  # pragma: no cover
     main()
