# memory_logger

Out-of-band host memory logger for forge workloads, with optional device DRAM sampling.

## Metrics

- Process RSS over time
- Swap usage over time
- OOM score over time
- Optional device DRAM usage over time (from tt-metal SHM regions)

## Output modes

- PNG (`--png`): static plots for quick capture/sharing
- HTML (`--html`): interactive Plotly figure
- Live dashboard (`--live`): browser-based live Plotly view served from the remote machine

## Install

```bash
pip install psutil matplotlib plotly
```

## Common usage

### 1) Monitor a PID and write PNG

```bash
python3 memory_logger.py --pid 12345 --interval 1.0 --csv pid_memory.csv --png pid_memory.png
```

### 2) Monitor by exact process name

```bash
python3 memory_logger.py --name worker --interval 1.0 --csv worker_memory.csv --png worker_memory.png
```

### 3) Enable device DRAM sampling

```bash
python3 memory_logger.py --pid 12345 --device-dram --csv pid_memory.csv --png pid_memory.png
```

Warning: device DRAM SHM files can be stale if a previous process exited uncleanly. If DRAM numbers look wrong (e.g. in SPMD mode, all devices should show lockstep allocation so min/max/avg should be the same), clean up stale regions before re-running:

```bash
rm -rf /dev/shm/tt_device_*_memory
```

### 4) Render from an existing CSV

```bash
python3 memory_logger.py --from-csv pid_memory.csv --png
python3 memory_logger.py --from-csv pid_memory.csv --html
```

### 5) Render multiple CSVs in one PNG (stacked subplots, unified x-axis)

```bash
python3 memory_logger.py --from-csv run_a.csv --from-csv run_b.csv --png merged_pid_memory.png
```

## Live mode

Live mode monitors a single process and serves a local web dashboard from the remote machine:

```bash
python3 memory_logger.py --pid 12345 --live
```

Then access from your local browser via SSH tunnel.

Optional advanced flags (defaults shown): `--live-host 127.0.0.1`, `--live-port 8765`, `--live-refresh-ms 1000`.

### SSH tunnel (direct)

```bash
ssh -L 8765:localhost:8765 <user>@<remote-host>
```

Open:

```text
http://localhost:8765
```

### SSH tunnel via jump host

```bash
ssh -J <jump-user>@<jump-host> -L 8765:localhost:8765 <user>@<remote-host>
```

### Exabox example (with extra jump)

```bash
ssh -J exabox -L 8765:localhost:8765 jameszianxu@bh-glx-110-c01u02.exabox.tenstorrent.com
```

## Notes

- `--live` is for active monitoring mode and cannot be combined with `--from-csv`.
- Multi-CSV render currently supports PNG output.
- Legacy CSVs with extra columns are tolerated during parsing.

## Example output

Plotly HTML:

<img width="1895" height="440" alt="image" src="https://github.com/user-attachments/assets/c3a4b18b-ed3b-4134-b9a8-4c9a9483f2a4" />

PNG:

<img width="760" height="619" alt="image" src="https://github.com/user-attachments/assets/510a0b5a-6673-4346-8ccd-1a858658b209" />
