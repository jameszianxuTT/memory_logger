#!/usr/bin/env python3
# pip install psutil matplotlib plotly
# python memory_logger.py --pid 12345 --interval 0.1 --csv memory.csv --png memory.png
# python memory_logger.py --name worker --interval 0.1 --csv memory.csv --png memory.png
# python memory_logger.py --pid 12345 --device-dram --png memory.png
# python memory_logger.py --pid 12345 --html memory.html
# python memory_logger.py --from-csv memory.csv --png memory.png
# python memory_logger.py --from-csv memory.csv --html memory.html

import argparse
import csv
import ctypes
import glob
import mmap
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import psutil
from matplotlib.ticker import AutoMinorLocator

# ---------------------------------------------------------------------------
# Device DRAM sampling via tt_metal SHM regions
# Mirrors DeviceMemoryRegion from tt_metal/impl/memory_tracking/memory_stats_shm.hpp
# ---------------------------------------------------------------------------

_DEVICE_MEMORY_REGION_VERSION = 3
_MAX_PROCESSES = 64
_MAX_CHIPS_PER_DEVICE = 16


class _ChipStats(ctypes.Structure):
    _fields_ = [
        ("chip_id",             ctypes.c_uint32),
        ("is_remote",           ctypes.c_uint32),
        ("dram_allocated",      ctypes.c_uint64),
        ("l1_allocated",        ctypes.c_uint64),
        ("l1_small_allocated",  ctypes.c_uint64),
        ("trace_allocated",     ctypes.c_uint64),
        ("cb_allocated",        ctypes.c_uint64),
    ]


class _ProcessStats(ctypes.Structure):
    _fields_ = [
        ("pid",                   ctypes.c_int32),
        ("_pad",                  ctypes.c_uint32),
        ("dram_allocated",        ctypes.c_uint64),
        ("l1_allocated",          ctypes.c_uint64),
        ("l1_small_allocated",    ctypes.c_uint64),
        ("trace_allocated",       ctypes.c_uint64),
        ("cb_allocated",          ctypes.c_uint64),
        ("last_update_timestamp", ctypes.c_uint64),
        ("process_name",          ctypes.c_char * 64),
    ]


class _DeviceMemoryRegion(ctypes.Structure):
    """
    Mirrors DeviceMemoryRegion (version 3).
    std::atomic<T> on Linux x86-64 is lock-free and same size/align as T.
    _pad fields reproduce the ABI padding the C++ compiler inserts.
    """
    _fields_ = [
        ("version",                  ctypes.c_uint32),
        ("num_active_processes",     ctypes.c_uint32),
        ("last_update_timestamp",    ctypes.c_uint64),
        ("reference_count",          ctypes.c_uint32),
        ("_pad1",                    ctypes.c_uint32),   # aligns board_serial to 8
        ("board_serial",             ctypes.c_uint64),
        ("asic_id",                  ctypes.c_uint64),
        ("device_id",                ctypes.c_uint32),
        ("_pad2",                    ctypes.c_uint32),   # aligns total_* to 8
        ("total_dram_allocated",     ctypes.c_uint64),
        ("total_l1_allocated",       ctypes.c_uint64),
        ("total_l1_small_allocated", ctypes.c_uint64),
        ("total_trace_allocated",    ctypes.c_uint64),
        ("total_cb_allocated",       ctypes.c_uint64),
        ("chip_stats",               _ChipStats * _MAX_CHIPS_PER_DEVICE),
        ("processes",                _ProcessStats * _MAX_PROCESSES),
    ]


class DeviceDramSampler:
    """
    Scans all /dev/shm/tt_device_*_memory regions at each sample tick and
    returns aggregate DRAM-allocated statistics across the full chip fleet.

    Counters reflect *allocated* bytes, not total DRAM capacity.
    Each SHM file is opened read-only and mmapped for a single snapshot;
    this is safe to call from any process regardless of whether tt-metal
    currently owns the device.
    """

    _REGION_SIZE = ctypes.sizeof(_DeviceMemoryRegion)
    _SHM_GLOB = "/dev/shm/tt_device_*_memory"

    def sample(self) -> dict | None:
        """
        Return aggregate DRAM stats in MiB, or None if no valid regions exist.

        Keys:
          chip_count      – number of chips with valid regions
          dram_total_mib  – sum of total_dram_allocated across all chips
          dram_avg_mib    – mean per chip
          dram_min_mib    – minimum single-chip value
          dram_max_mib    – maximum single-chip value
        """
        paths = glob.glob(self._SHM_GLOB)
        if not paths:
            return None

        values_mib = []
        for path in paths:
            mib = self._read_dram_mib(path)
            if mib is not None:
                values_mib.append(mib)

        if not values_mib:
            return None

        total = sum(values_mib)
        return {
            "chip_count":     len(values_mib),
            "dram_total_mib": round(total, 3),
            "dram_avg_mib":   round(total / len(values_mib), 3),
            "dram_min_mib":   round(min(values_mib), 3),
            "dram_max_mib":   round(max(values_mib), 3),
        }

    @classmethod
    def _read_dram_mib(cls, path: str) -> float | None:
        """Read total_dram_allocated from one SHM file. Returns MiB or None on any error."""
        try:
            fd = os.open(path, os.O_RDONLY)
        except (PermissionError, FileNotFoundError, OSError):
            return None
        try:
            if os.fstat(fd).st_size < cls._REGION_SIZE:
                return None
            mm = mmap.mmap(fd, cls._REGION_SIZE, access=mmap.ACCESS_READ)
            region = _DeviceMemoryRegion.from_buffer_copy(mm)
            mm.close()
        except OSError:
            return None
        finally:
            os.close(fd)

        if region.version != _DEVICE_MEMORY_REGION_VERSION:
            return None
        return region.total_dram_allocated / (1024 * 1024)


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------

def bytes_to_mib(num_bytes: int) -> float:
    return num_bytes / (1024 * 1024)


def get_oom_score(pid: int) -> int | None:
    """Read OOM score from /proc/[pid]/oom_score (Linux only, range 0-1000)."""
    try:
        with open(f"/proc/{pid}/oom_score") as f:
            return int(f.read().strip())
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, OSError):
        return None


def get_swap_bytes(pid: int) -> int:
    """Read VmSwap from /proc/[pid]/status and return bytes (Linux only)."""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmSwap:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
                    break
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, OSError):
        pass
    return 0


def apply_grid_with_subgrid(ax, y_minor_divisions: int = 2):
    ax.minorticks_on()
    ax.yaxis.set_minor_locator(AutoMinorLocator(y_minor_divisions))
    ax.grid(True, which="major", alpha=0.3)
    ax.grid(True, which="minor", alpha=0.15, linestyle=":")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Log RSS/swap/OOM for a process and optionally device DRAM via tt-metal SHM."
    )
    target_group = parser.add_mutually_exclusive_group(required=True)
    target_group.add_argument("--pid", type=int, help="Target process ID")
    target_group.add_argument(
        "--name",
        type=str,
        help="Attach to first process whose name exactly matches this string",
    )
    target_group.add_argument(
        "--from-csv",
        type=Path,
        help="Re-render a PNG from an existing CSV without attaching to a process",
    )
    parser.add_argument("--interval", type=float, default=1.0, help="Sampling interval in seconds")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.5,
        help="How often to poll for process name matches (seconds)",
    )
    parser.add_argument("--csv", type=Path, default=Path("pid_memory.csv"), help="CSV output path")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "--png",
        type=Path,
        nargs="?",
        const=Path("pid_memory.png"),
        default=None,
        help="Generate PNG plot (default output format).",
    )
    output_group.add_argument(
        "--html",
        type=Path,
        nargs="?",
        const=Path("pid_memory.html"),
        default=None,
        help="Generate interactive HTML plot via plotly. Device DRAM subplot not yet supported in HTML mode.",
    )
    parser.add_argument(
        "--device-dram",
        action="store_true",
        help=(
            "Sample device DRAM usage from /dev/shm/tt_device_*_memory SHM regions "
            "and add an avg/min/max subplot. Requires tt-metal to be running on this host."
        ),
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------

def find_process_by_name_exact(name_exact: str):
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if (proc.info.get("name") or "") == name_exact:
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return None


def resolve_target_process(args):
    if args.pid is not None:
        try:
            return psutil.Process(args.pid)
        except psutil.NoSuchProcess:
            print(f"PID {args.pid} does not exist.")
            return None

    print(f"Waiting for process '{args.name}' (polling every {args.poll_interval}s)...")
    while True:
        proc = find_process_by_name_exact(args.name)
        if proc is not None:
            return proc
        time.sleep(args.poll_interval)


# ---------------------------------------------------------------------------
# Plot
# ---------------------------------------------------------------------------

_DRAM_CSV_COLS = ("dram_chip_count", "dram_total_mib", "dram_avg_mib", "dram_min_mib", "dram_max_mib")


def generate_plot(csv_path: Path, png_path: Path, process_name: str):
    timestamps = []
    rss_mib = []
    swap_mib = []
    oom_scores = []
    dram_avg_mib = []
    dram_min_mib = []
    dram_max_mib = []
    dram_chip_count = []

    with csv_path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        has_dram = "dram_avg_mib" in fieldnames
        for row in reader:
            timestamps.append(datetime.fromisoformat(row["timestamp_utc"]))
            rss_mib.append(float(row["rss_mib"]))
            swap_mib.append(float(row["swap_mib"]))
            oom_scores.append(int(row["oom_score"]) if row.get("oom_score") else None)
            if has_dram:
                dram_avg_mib.append(float(row["dram_avg_mib"]) if row["dram_avg_mib"] else 0.0)
                dram_min_mib.append(float(row["dram_min_mib"]) if row["dram_min_mib"] else 0.0)
                dram_max_mib.append(float(row["dram_max_mib"]) if row["dram_max_mib"] else 0.0)
                dram_chip_count.append(int(row["dram_chip_count"]) if row["dram_chip_count"] else 0)

    if not timestamps:
        print("No samples collected, skipping plot.")
        return

    has_dram_data = has_dram and any(v > 0 for v in dram_avg_mib)

    nrows = 1 + int(has_dram_data)
    fig, axes = plt.subplots(nrows, 1, figsize=(12, 4.5 * nrows), sharex=True)
    if nrows == 1:
        axes = [axes]

    row_idx = 0

    # --- subplot 1: process RSS / swap / OOM ---
    ax_mem = axes[row_idx]
    row_idx += 1
    ax_mem.plot(timestamps, rss_mib, linewidth=2, label="RSS (MiB)", color="tab:blue")
    ax_mem.plot(timestamps, swap_mib, linewidth=2, label="Swap (MiB)", color="tab:orange")
    ax_mem.set_ylabel("Memory (MiB)")
    ax_mem.set_title(f"Process Memory Over Time ({process_name})")
    apply_grid_with_subgrid(ax_mem)

    has_oom_data = any(s is not None for s in oom_scores)
    if has_oom_data:
        ax_oom = ax_mem.twinx()
        ax_oom.plot(timestamps, oom_scores, linewidth=2, label="OOM Score", color="tab:red", linestyle="--")
        ax_oom.set_ylabel("OOM Score (0–1000)")
        ax_oom.set_ylim(0, 1000)
        lines1, labels1 = ax_mem.get_legend_handles_labels()
        lines2, labels2 = ax_oom.get_legend_handles_labels()
        ax_mem.legend(lines1 + lines2, labels1 + labels2, loc="upper left")
    else:
        ax_mem.legend(loc="upper left")

    # --- subplot N (optional): device DRAM ---
    if has_dram_data:
        ax_dram = axes[row_idx]
        n_chips = max(dram_chip_count) if dram_chip_count else 0
        ax_dram.fill_between(
            timestamps, dram_min_mib, dram_max_mib,
            alpha=0.2, color="tab:blue", label="Min–Max range",
        )
        ax_dram.plot(timestamps, dram_avg_mib, linewidth=2, color="tab:blue", label="Avg per chip (MiB)")
        ax_dram.plot(timestamps, dram_min_mib, linewidth=1, color="tab:cyan", linestyle="--", label="Min chip (MiB)")
        ax_dram.plot(timestamps, dram_max_mib, linewidth=1, color="tab:red", linestyle="--", label="Max chip (MiB)")
        ax_dram.set_ylabel("DRAM Allocated (MiB)")
        ax_dram.set_title(f"Device DRAM Allocated — {n_chips} chip(s), per-chip avg / min / max")
        apply_grid_with_subgrid(ax_dram)
        ax_dram.legend(loc="upper left")

    # Show x-axis label/ticks on every subplot for readability in stacked PNGs.
    for ax in axes:
        ax.set_xlabel("Time (UTC)")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
        ax.tick_params(axis="x", which="both", labelbottom=True)

    fig.autofmt_xdate()
    plt.tight_layout()
    plt.savefig(png_path, dpi=150)
    plt.close(fig)
    print(f"Saved plot: {png_path}")


def generate_plot_html(csv_path: Path, html_path: Path, process_name: str):
    """Interactive HTML via plotly. DRAM columns are present in the CSV but not rendered here."""
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        print("Interactive HTML output requires plotly: pip install plotly")
        return

    timestamps = []
    rss_mib = []
    swap_mib = []
    oom_scores = []

    with csv_path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            timestamps.append(datetime.fromisoformat(row["timestamp_utc"]))
            rss_mib.append(float(row["rss_mib"]))
            swap_mib.append(float(row["swap_mib"]))
            oom_scores.append(int(row["oom_score"]) if row.get("oom_score") else None)

    if not timestamps:
        print("No samples collected, skipping interactive plot.")
        return

    has_oom_data = any(s is not None for s in oom_scores)

    specs = [[{"secondary_y": has_oom_data}]]
    fig = make_subplots(rows=1, cols=1, shared_xaxes=True, vertical_spacing=0.08, specs=specs)

    fig.add_trace(go.Scatter(x=timestamps, y=rss_mib, mode="lines", name="RSS (MiB)", line={"width": 2, "color": "royalblue"}), row=1, col=1)
    fig.add_trace(go.Scatter(x=timestamps, y=swap_mib, mode="lines", name="Swap (MiB)", line={"width": 2, "color": "orange"}), row=1, col=1)
    if has_oom_data:
        fig.add_trace(
            go.Scatter(x=timestamps, y=oom_scores, mode="lines", name="OOM Score",
                       line={"width": 2, "color": "red", "dash": "dash"}),
            row=1, col=1, secondary_y=True,
        )
        fig.update_yaxes(title_text="OOM Score (0-1000)", range=[0, 1000], row=1, col=1, secondary_y=True)
    fig.update_yaxes(title_text="Memory (MiB)", row=1, col=1)
    fig.update_xaxes(title_text="Time (UTC)", row=1, col=1)

    fig.update_layout(
        title=f"Process Memory Over Time ({process_name})",
        hovermode="x unified",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "left", "x": 0},
        template="plotly_white",
        height=450,
    )
    fig.write_html(html_path, include_plotlyjs="cdn")
    print(f"Saved interactive plot: {html_path}")


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def infer_process_name_from_csv(csv_path: Path) -> str:
    with csv_path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        first_row = next(reader, None)
        if first_row is None:
            return csv_path.stem
        return (first_row.get("process_name") or "").strip() or csv_path.stem


# ---------------------------------------------------------------------------
# Monitoring loop
# ---------------------------------------------------------------------------

def run_monitor(args):
    try:
        proc = resolve_target_process(args)
    except KeyboardInterrupt:
        print("\nStopped before attaching to a process.")
        return
    if proc is None:
        return

    target_pid = proc.pid
    proc_name = proc.name()
    target_create_time = proc.create_time()

    dram_sampler = DeviceDramSampler() if args.device_dram else None

    # Warn early if --device-dram was requested but no SHM files are visible yet
    if dram_sampler is not None and not glob.glob(DeviceDramSampler._SHM_GLOB):
        print("WARNING: --device-dram enabled but no /dev/shm/tt_device_*_memory files found. "
              "Will keep trying each interval.")

    header = [
        "timestamp_utc", "pid", "process_name",
        "rss_bytes", "rss_mib",
        "swap_bytes", "swap_mib",
        "oom_score",
    ]
    if dram_sampler is not None:
        header += list(_DRAM_CSV_COLS)

    output_kind, output_path = _resolve_output(args, Path("pid_memory.png"), Path("pid_memory.html"))
    print(
        f"Sampling PID={target_pid} ({proc_name}) every {args.interval}s"
        + (" + device DRAM" if dram_sampler else "")
        + f"\nCSV → {args.csv}  |  {output_kind.upper()} → {output_path}\nCtrl+C to stop."
    )

    with args.csv.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)

        try:
            while True:
                try:
                    if not proc.is_running() or proc.create_time() != target_create_time:
                        print(f"PID {target_pid} exited/restarted; stopping sampler.")
                        break
                    # Avoid memory_full_info() — on Linux it parses /proc/[pid]/smaps
                    # which is expensive and perturbs high-frequency sampling.
                    mem_info = proc.memory_info()
                    rss_bytes = mem_info.rss
                    swap_bytes = get_swap_bytes(target_pid)
                    oom_score = get_oom_score(target_pid)
                except psutil.NoSuchProcess:
                    print(f"PID {target_pid} exited; stopping sampler.")
                    break

                ts = datetime.now(timezone.utc).isoformat()
                row = [
                    ts, target_pid, proc_name,
                    rss_bytes, round(bytes_to_mib(rss_bytes), 3),
                    swap_bytes, round(bytes_to_mib(swap_bytes), 3),
                    oom_score if oom_score is not None else "",
                ]

                if dram_sampler is not None:
                    stats = dram_sampler.sample()
                    if stats is not None:
                        row += [
                            stats["chip_count"],
                            stats["dram_total_mib"],
                            stats["dram_avg_mib"],
                            stats["dram_min_mib"],
                            stats["dram_max_mib"],
                        ]
                    else:
                        row += ["", "", "", "", ""]

                writer.writerow(row)
                f.flush()
                time.sleep(args.interval)

        except KeyboardInterrupt:
            print("\nStopping sampler...")

    _render(args.csv, output_kind, output_path, proc_name)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _resolve_output(args, default_png: Path, default_html: Path) -> tuple[str, Path]:
    if args.html is not None:
        return "html", args.html
    if args.png is not None:
        return "png", args.png
    return "png", default_png


def _render(csv_path: Path, output_kind: str, output_path: Path, process_name: str):
    if output_kind == "html":
        generate_plot_html(csv_path, output_path, process_name)
    else:
        generate_plot(csv_path, output_path, process_name)


def main():
    args = parse_args()

    if args.from_csv is not None:
        output_kind, output_path = _resolve_output(
            args,
            args.from_csv.with_suffix(".png"),
            args.from_csv.with_suffix(".html"),
        )
        process_name = infer_process_name_from_csv(args.from_csv)
        _render(args.from_csv, output_kind, output_path, process_name)
        return

    run_monitor(args)


if __name__ == "__main__":
    main()
