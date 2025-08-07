# snap_control

Utility configs for configuring multiple CASM SNAP boards and starting the 
stream to destinations defined in configs/casm_feng_layout.yaml

## Quick‑start

### Installation

```bash
# Clone the repository
git clone https://github.com/Coherent-All-Sky-Monitor/snap_control.git
cd snap_control

# Install dependencies using Poetry
poetry install

# Or install with pip
pip install -e .
```

### Basic Usage

Configure all SNAP boards defined in the YAML configuration file:

```bash
python src/multi_snap_config.py configs/casm_feng_layout.yaml
```

Configure specific SNAP boards by IP address:

```bash
python src/multi_snap_config.py configs/casm_feng_layout.yaml --ip 192.168.0.56 192.168.0.127 192.168.0.101
```

Configure with custom channel packet size:

```bash
python src/multi_snap_config.py configs/casm_feng_layout.yaml --nchan-packet 256
```

If the SNAPs are already programmed and you don't wanna go through all that:

```bash
python src/multi_snap_config.py configs/casm_feng_layout.yaml --programmed
```

### Configuration File Structure

The YAML configuration file (`configs/casm_feng_layout.yaml`) contains:

**Common Settings** (applied to all boards):
- `fpgfile`: Path to FPGA bitstream file
- `source_port`: UDP source port (default: 10000)
- `dest_port`: UDP destination port (default: 13000)
- `nchan`: Channels per destination (default: 512)
- `nchan_packet`: Channels per UDP packet (default: 512)
- `destinations`: List of downstream targets with IP, MAC, and channel ranges

**Board-Specific Settings**:
- `host`: Hostname or management IP
- `source_ip`: 10-GbE source IP address
- `source_mac`: Source MAC address
- `feng_id`: F-engine ID for this board

### Example Configuration

```yaml
common:
  fpgfile: /path/to/firmware.snap_f_12i_4kc.fpg
  source_port: 10000
  dest_port: 13000
  nchan: 512
  nchan_packet: 512
  destinations:
    - ip: 192.168.100.3
      mac: c4:70:bd:74:2e:3c
      start_chan: 0
    - ip: 192.168.100.4
      mac: c4:70:bd:74:2e:3d
      start_chan: 512

boards:
  - name: snap01
    host: snap01
    source_ip: 192.168.0.56
    source_mac: 00:25:90:c2:b1:a0
    feng_id: 0
  - name: snap02
    host: snap02
    source_ip: 10.10.0.2
    source_mac: 00:25:90:c2:b1:a1
    feng_id: 1
```

### Command-Line Options

- `layout_yaml`: Path to YAML configuration file (required)
- `--ip`: IP address(es) of specific SNAP boards to configure
- `--nchan-packet`: Override the number of channels per packet
- `--programmed`: Program the FPGA before configuration
- `--log-level`: Set logging level (DEBUG, INFO, WARNING, ERROR)

### Requirements

- Python ≥ 3.8
- `casm_f` library installed (CASM SNAP F-Engine interface)
- Network connectivity to all SNAP boards
- PyYAML for configuration parsing

### What It Does

1. Reads board-specific networking and firmware parameters from YAML
2. Establishes control connections to each SNAP board
3. Programs the FPGA with the specified bitstream (if needed)
4. Configures each board to stream its part of the 4096-channel band
5. Provides status summary with throughput and packet rate information
