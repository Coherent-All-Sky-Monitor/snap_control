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
#from tkinter import N, 

import numpy as np
import argparse
import logging
import sys
from pathlib import Path
from typing import Dict, List
from subprocess import Popen, PIPE
import re
import yaml  # PyYAML
from scipy.signal import savgol_filter
from concurrent.futures import ThreadPoolExecutor
import time

# CASM library import — errors out cleanly if missing
try:
    from casm_f import snap_fengine  # type: ignore
except ImportError as exc:  # pragma: no cover
    sys.exit(
        "casm_f not importable – install it in your Python environment first "
        "(see https://github.com/casm-project/casm_f)."
    )

from casperfpga import CasperFpga, TapcpTransport, KatcpTransport

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

def _set_gain(adc, gain): 
    # ADC is an HMCAD1511 object

    """
    Set the coarse gain of the ADC. Allowed values
    are 1, 1.25, 2, 2.5, 4, 5, 8, 10, 12.5, 16, 20, 25, 32, 50.
    """
    gain_map = {
            1    : 0b0000,
            1.25 : 0b0001,
            2    : 0b0010,
            2.5  : 0b0011,
            4    : 0b0100,
            5    : 0b0101,
            8    : 0b0110,
            10   : 0b0111,
            12.5 : 0b1000,
            16   : 0b1001,
            20   : 0b1010,
            25   : 0b1011,
            32   : 0b1100,
            50   : 0b1101
    }
    adc.write(gain_map[gain] * 0x1111, 0x2A) 

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

def level(snap, ncoeffs=512, default_coeff=2.5):
    """ Flattens the bandpass using the eq_coeffs. 
        Will only work if armed. Based on Vikram's DSA-110 code.

        https://github.com/dsa110/SNAP_control/blob/master/SNAP_control/dsaX_snap.py

    Args:
        snap: The snap object
        ncoeffs: The number of coefficients to use
        default_coeff: The default coefficient to use

    Returns:
        None
    """

    ### TODO: multithread this
    
    snap.corr.set_acc_len(50000)
    LOGGER.info("Set acc len to 50k")
    LOGGER.info("Starting level control iterating over %d inputs", snap.n_inputs)

    for st in range(snap.n_inputs):
        LOGGER.info("Setting EQ coefficients for stream "+str(st))
        data = np.zeros(4096)

        for ii in range(4):
            bp = np.real(snap.corr.get_new_corr(int(st),int(st)))
            bp[np.where(bp==0.0)] = np.median(bp)
            data += bp

        try:
            data_smooth = savgol_filter(data, 32, 3)[4::8]
            data_smooth[data_smooth<=0.0] = np.median(data_smooth[data_smooth>0.0])
            data_smooth_voltage = np.sqrt(data_smooth)

            coeffs = 2.5*4./data_smooth_voltage

            LOGGER.info("min "+str(coeffs.min())+" max "+str(coeffs.max()))
            snap.eq.set_coeffs(int(st),coeffs)
            LOGGER.info("Set coeffs for stream "+str(st))
        except Exception:
            LOGGER.error("Could not set Eq coeffs for input "+str(st))
            snap.eq.set_coeffs(int(st),default_coeff+np.zeros(ncoeffs))

    LOGGER.info("Finished level control")

def _configure_board(board: dict, common: dict, 
                     nchan_packet_cli: Optional[int], 
                     snap_ip: Optional[str], 
                     programmed: Optional[bool],
                     feng_id: Optional[int], 
                     test_mode: Optional[str],
                     adc_gain: Optional[int],
                     eq_coeffs: Optional[float],
                     fft_shift: Optional[int],
                     ) -> None:
    """Configure one SNAP board using *common* defaults + *board* overrides."""
    host = board["host"]

    # ---------- Common parameters ----------
    fpgfile = common["fpgfile"]
    source_port = int(common.get("source_port", 10000))
    nchan_packet = int(common.get("nchan_packet", nchan_packet_cli or 512))
    nchan_default = int(common.get("nchan", nchan_packet))

    if snap_ip:
        source_ip = snap_ip
        # Obtaining SNAP board mac
        pid = Popen(["arp","-n",source_ip],stdout=PIPE)
        s = pid.communicate()[0]
        mac = re.search(r"(([a-f\d]{1,2}\:){5}[a-f\d]{1,2})",str(s)).groups()[0]
        source_mac = int(mac.replace(":",""),16)
        feng_id = feng_id
    else:
        feng_id = int(board.get("feng_id", 0))
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
                "port": int(dest["dest_port"]),
                "start_chan": int(dest["start_chan"]),
                "nchan": int(dest.get("nchan", nchan_default)),
            }
        )
        macs[ip] = _mac_to_int(dest["mac"])

    # Connecting to the SNAP. This connects to the SNAP
    # and uploads the bitstream to the SNAP. We do this before casm_f.snap_fengine.SnapFengine
    # because it doesn't work with the max_time_delay error.
    if programmed is False:
        LOGGER.info("Connecting to %s at IP=%s …" % (host,source_ip))
        snap = CasperFpga(source_ip, transport=TapcpTransport)
        LOGGER.info("Using CasperFpga at first, to fix max_time_delay error")
        snap.upload_to_ram_and_program(fpgfile)

    time.sleep(10)
    snap = snap_fengine.SnapFengine(source_ip, use_microblaze=True)

    LOGGER.info(
        "Configuring %s (feng_id=%d) – src %s:%d → %d dests",
        host,
        feng_id,
        source_ip,
        source_port,
        len(dests),
    )

    time.sleep(10)
    # Programming the SNAP. This is the main function that programs the SNAP
    # and initializes the ADC.
    try:
        snap.program(fpgfile, initialize_adc=True)
    except Exception:
        print("Initial program failed. Attempting to initialize ADC.")
        snap.adc.initialize()
        snap.program(fpgfile, initialize_adc=True)

    if adc_gain is not None:
        LOGGER.info("Setting ADC gain to %d", adc_gain)
        _set_gain(snap.adc.adc.adc, adc_gain)

    # Configuring the SNAP. This is the main function that configures the SNAP
    # and begins the streaming of data to the destinations.
    snap.configure(
        source_ip=source_ip,
        source_port=source_port,
        program=False,
        dests=dests,
        macs=macs,
        nchan_packet=nchan_packet,
        enable_tx=False,
        feng_id=feng_id,
        fft_shift=fft_shift,
    )

    # Setting the EQ coefficients
    if eq_coeffs is not None:
        # Set to a fixed value
        [snap.eq.set_coeffs(ii, eq_coeffs*np.ones([512])) for ii in range(12)]
    else:
        # Flatten the bandpass
        level(snap, ncoeffs=512, default_coeff=2.5)

    # Setting the input mode
    if test_mode is not None:
        if test_mode == "zeros":
            snap.input.use_zero()
            LOGGER.info("Using zeros as input")
        elif test_mode == "noise":
            snap.input.use_noise()
            LOGGER.info("Using ones as input")
        elif test_mode == "counter":
            snap.input.use_counter()
            LOGGER.info("Using random as input")
            
    eth_status, flags = snap.eth.get_status()  # type: ignore[attr‑defined]
    LOGGER.info(
        "%s: tx %.2f Gb/s – packets %d pps – flags %s",
        host,
        eth_status["gbps"],
        eth_status["tx_ctr"],
        flags,
    )

    return snap

def concurrently(snaps, fn):
    """
    Run the given function concurrently on the given snaps
    """
    with ThreadPoolExecutor(max_workers=len(snaps)) as ex:
        return list(ex.map(fn, snaps))

def program_init(snaps, fpgfile):
    """
    Program the FPGAs and initialize the snaps using the given fpgfile
    """
    def one(s):
        s.program(fpgfile)                    # program .fpg (and ADC trains if enabled)
        s.initialize()         # init blocks + global reset
        return s.hostname
    return concurrently(snaps, one)

def pps_two_ticks_ok(s):
    # get_tt_of_pps(wait_for_sync=True) returns (tt, sync_number) for PPS:contentReference[oaicite:5]{index=5}
    tt0, n0 = s.sync.get_tt_of_pps(wait_for_sync=True)
    tt1, n1 = s.sync.get_tt_of_pps(wait_for_sync=True)
    return (tt0, n0), (tt1, n1), (n1 == n0 + 1)

def sync_time_using_update_telescope_time(snaps):
    # Robust PPS-locked load; uses count_pps to ensure no PPS during compute
    concurrently(snaps, lambda s: s.sync.update_telescope_time())
    # Wait for the PPS edge that *performs* the load
    snaps[0].sync.get_tt_of_pps(wait_for_sync=True)
    # Re-sync internal TT (which drives packet timestamps) on all boards
    concurrently(snaps, lambda s: s.sync.update_internal_time())

def verify(snaps):
    periods = concurrently(snaps, lambda s: s.sync.period_pps())
    lastpps = concurrently(snaps, lambda s: s.sync.get_tt_of_pps(wait_for_sync=True))
    # Check all boards saw the same PPS count
    counts = [n for (tt, n) in lastpps]
    if len(set(counts)) > 1:
        LOGGER.warning("PPS counts differ across boards: %s", counts)
    return periods, lastpps

# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Configure multiple CASM SNAP boards from YAML (common + boards schema)"
    )
    ap.add_argument("layout_yaml", type=Path, help="YAML layout file")
    ap.add_argument("--ip", type=str, nargs='+', help="IP address(es) of the SNAP to configure (single or multiple)", 
                    default=None)
    ap.add_argument("--nchan-packet", type=int, default=None, help="Override common.nchan_packet")
    ap.add_argument("--do_sync", action="store_true", help="Do sync after configuring")
    ap.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    ap.add_argument("--programmed", action="store_true", help="Skip CasperFpga pre-programming (board already programmed)")
    ap.add_argument("--test-mode", type=str, default=None, help="Test mode for the SNAP", 
    choices=["zeros", "noise", "counter"])
    ap.add_argument("--fft_shift", type=int, default=None, help="FFT shift for the SNAP")
    ap.add_argument("--feng_id", type=str, nargs='+', help="Feng ID(s) for the SNAP", 
                    default=None)
    ap.add_argument("--eq_coeffs", type=int, default=None, help="EQ coefficients for the SNAP")
    ap.add_argument("--adc_gain", type=float, default=None, 
    help="ADC gain for the SNAP must be one of: 1, 1.25, 2, 2.5, 4, 5, 8, 10, 12.5, 16, 20, 25, 32, 50.", 
    choices=[1, 1.25, 2, 2.5, 4, 5, 8, 10, 12.5, 16, 20, 25, 32, 50])
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
    
    # If IP addresses are provided, configure the board with the given IP address
    if args.ip is not None:
        snaps = []
        for kk, ip in enumerate(args.ip):
            if args.feng_id is not None:
                feng_id = int(args.feng_id[kk])
            else:
                feng_id = kk
            try:
                snap = _configure_board(boards[0], common, args.nchan_packet, 
                                 ip, programmed=args.programmed, 
                                 feng_id=feng_id, test_mode=args.test_mode,
                                 adc_gain=args.adc_gain,
                                 eq_coeffs=args.eq_coeffs,
                                 fft_shift=args.fft_shift, 
                                 )
                snaps.append(snap)

            except Exception:
                LOGGER.exception("Configuration failed for IP %s", ip)
                continue
        LOGGER.info(f"Configured {len(snaps)} boards")

    # If no IP addresses are provided, configure all boards from the yaml file
    elif args.ip is None:
        snaps = []
        for board in boards:
            try:
                snap = _configure_board(board, common, args.nchan_packet,
                                 snap_ip=None,
                                 programmed=args.programmed, test_mode=args.test_mode,
                                 adc_gain=args.adc_gain,
                                 eq_coeffs=args.eq_coeffs,
                                 fft_shift=args.fft_shift,
                                 feng_id=args.feng_id)
                snaps.append(snap)
            except Exception:
                LOGGER.exception("Configuration failed for board %s", board.get("host"))
                continue

    LOGGER.info(f"All requested boards processed. {len(snaps)} boards configured.")

    if args.do_sync is True:
        LOGGER.info("Doing sync")
        # PPS presence sanity
        checks = concurrently(snaps, pps_two_ticks_ok)
        for s, ((tt0,n0),(tt1,n1),ok) in zip(snaps, checks):
            print(f"{s.hostname}: PPS tick check ok={ok}  (tt,n): ({tt0},{n0}) -> ({tt1},{n1})")

        # Robust alignment to PPS-locked telescope time
        sync_time_using_update_telescope_time(snaps)

        # Verify
        periods, lastpps = verify(snaps)
        LOGGER.info("period_pps: %s", {s.hostname: p for s,p in zip(snaps, periods)})
        LOGGER.info("get_tt_of_pps: %s", {s.hostname: v for s,v in zip(snaps, lastpps)})

        # Convenience: print TT delta in clocks and seconds vs reference board
        tt_ref, n_ref = lastpps[0]
        for s, (tt, n), period in zip(snaps, lastpps, periods):
            dclks = tt - tt_ref
            LOGGER.info(
                "%s: PPS count=%d  TT delta vs %s [clks]: %d  [s]: %f",
                s.hostname, n, snaps[0].hostname, dclks, dclks / float(period),
            )

    concurrently(snaps, lambda s: s.eth.enable_tx())

if __name__ == "__main__":  # pragma: no cover
     main()
