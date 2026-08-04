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
import io
import json
import mmap
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import psutil
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
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


def _row_float(row: dict[str, str], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return default


def _row_int_or_none(row: dict[str, str], *keys: str) -> int | None:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


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


def _build_png_legend_handles(has_dram_data: bool, has_oom_data: bool):
    handles = [
        Line2D([0], [0], color="tab:blue", lw=1.5, label="RSS (MiB)"),
        Line2D([0], [0], color="tab:orange", lw=1.5, label="Swap (MiB)"),
    ]
    if has_dram_data:
        handles.extend(
            [
                Patch(facecolor="tab:green", alpha=0.12, edgecolor="none", label="DRAM Min–Max"),
                Line2D([0], [0], color="tab:green", lw=1.8, label="DRAM Avg/chip (MiB)"),
                Line2D([0], [0], color="seagreen", lw=0.8, ls="--", label="DRAM Min/chip (MiB)"),
                Line2D([0], [0], color="darkgreen", lw=0.8, ls="--", label="DRAM Max/chip (MiB)"),
            ]
        )
    if has_oom_data:
        handles.append(Line2D([0], [0], color="tab:red", lw=1.5, ls="--", label="OOM Score"))
    return handles


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
        action="append",
        help=(
            "Re-render plot(s) from existing CSV(s) without attaching to a process. "
            "Repeat flag to stack multiple CSVs as subplots with a unified time axis."
        ),
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
        help="Generate interactive HTML plot via plotly.",
    )
    parser.add_argument(
        "--device-dram",
        action="store_true",
        help=(
            "Sample device DRAM usage from /dev/shm/tt_device_*_memory SHM regions "
            "and add an avg/min/max subplot. Requires tt-metal to be running on this host."
        ),
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Serve a live Plotly dashboard that tails the output CSV.",
    )
    parser.add_argument(
        "--live-host",
        type=str,
        default="127.0.0.1",
        help="Live dashboard bind host (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--live-port",
        type=int,
        default=8765,
        help="Live dashboard bind port (default: 8765).",
    )
    parser.add_argument(
        "--live-refresh-ms",
        type=int,
        default=1000,
        help="Live dashboard refresh interval in milliseconds (default: 1000).",
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
    dram_present_flags = []

    with csv_path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        has_dram = "dram_avg_mib" in fieldnames
        for row in reader:
            timestamps.append(datetime.fromisoformat(row["timestamp_utc"]))
            rss_mib.append(_row_float(row, "rss_mib"))
            swap_mib.append(_row_float(row, "swap_mib"))
            oom_scores.append(_row_int_or_none(row, "oom_score"))
            if has_dram:
                dram_avg_mib.append(_row_float(row, "dram_avg_mib"))
                dram_min_mib.append(_row_float(row, "dram_min_mib"))
                dram_max_mib.append(_row_float(row, "dram_max_mib"))
                dram_chip_count.append(_row_int_or_none(row, "dram_chip_count") or 0)
                dram_present_flags.append(
                    row.get("dram_avg_mib") not in (None, "")
                    or row.get("dram_min_mib") not in (None, "")
                    or row.get("dram_max_mib") not in (None, "")
                )

    if not timestamps:
        print("No samples collected, skipping plot.")
        return

    has_dram_data = has_dram and any(dram_present_flags)

    fig, ax_mem = plt.subplots(1, 1, figsize=(12, 5.2))
    ax_mem.plot(timestamps, rss_mib, linewidth=1.5, label="RSS (MiB)", color="tab:blue")
    ax_mem.plot(timestamps, swap_mib, linewidth=1.5, label="Swap (MiB)", color="tab:orange")

    if has_dram_data:
        n_chips = max(dram_chip_count) if dram_chip_count else 0
        ax_mem.fill_between(
            timestamps,
            dram_min_mib,
            dram_max_mib,
            alpha=0.12,
            color="tab:green",
            label=f"DRAM Min–Max ({n_chips} chip(s))",
        )
        ax_mem.plot(
            timestamps,
            dram_avg_mib,
            linewidth=1.8,
            color="tab:green",
            label="DRAM Avg/chip (MiB)",
        )
        ax_mem.plot(
            timestamps,
            dram_min_mib,
            linewidth=0.8,
            color="seagreen",
            linestyle="--",
            label="DRAM Min/chip (MiB)",
        )
        ax_mem.plot(
            timestamps,
            dram_max_mib,
            linewidth=0.8,
            color="darkgreen",
            linestyle="--",
            label="DRAM Max/chip (MiB)",
        )

    ax_mem.set_ylabel("Memory (MiB)")
    ax_mem.set_title(f"Process Memory Over Time ({process_name})")
    apply_grid_with_subgrid(ax_mem)

    has_oom_data = any(s is not None for s in oom_scores)
    if has_oom_data:
        ax_oom = ax_mem.twinx()
        ax_oom.plot(timestamps, oom_scores, linewidth=1.5, label="OOM Score", color="tab:red", linestyle="--")
        ax_oom.set_ylabel("OOM Score (0–1000)")
        ax_oom.set_ylim(0, 1000)

    ax_mem.set_xlabel("Time (UTC)")
    ax_mem.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
    ax_mem.tick_params(axis="x", which="both", labelbottom=True)

    legend_handles = _build_png_legend_handles(has_dram_data, has_oom_data)
    fig.legend(
        handles=legend_handles,
        loc="lower center",
        bbox_to_anchor=(0.5, 0.01),
        ncol=4,
        fontsize=8,
        framealpha=0.9,
        columnspacing=1.2,
        handlelength=2.0,
    )

    fig.autofmt_xdate()
    plt.tight_layout(rect=[0.0, 0.12, 1.0, 1.0])
    plt.savefig(png_path, dpi=150)
    plt.close(fig)
    print(f"Saved plot: {png_path}")


def generate_plot_multi_csv(csv_paths: list[Path], png_path: Path):
    datasets = []

    for csv_path in csv_paths:
        timestamps = []
        rss_mib = []
        swap_mib = []
        oom_scores = []
        dram_avg_mib = []
        dram_min_mib = []
        dram_max_mib = []
        dram_chip_count = []
        dram_present_flags = []

        with csv_path.open("r", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            has_dram_cols = "dram_avg_mib" in fieldnames
            for row in reader:
                timestamps.append(datetime.fromisoformat(row["timestamp_utc"]))
                rss_mib.append(_row_float(row, "rss_mib"))
                swap_mib.append(_row_float(row, "swap_mib"))
                oom_scores.append(_row_int_or_none(row, "oom_score"))
                if has_dram_cols:
                    dram_avg_mib.append(_row_float(row, "dram_avg_mib"))
                    dram_min_mib.append(_row_float(row, "dram_min_mib"))
                    dram_max_mib.append(_row_float(row, "dram_max_mib"))
                    dram_chip_count.append(_row_int_or_none(row, "dram_chip_count") or 0)
                    dram_present_flags.append(
                        row.get("dram_avg_mib") not in (None, "")
                        or row.get("dram_min_mib") not in (None, "")
                        or row.get("dram_max_mib") not in (None, "")
                    )

        if not timestamps:
            print(f"No samples in {csv_path}, skipping.")
            continue

        has_dram_data = has_dram_cols and any(dram_present_flags)
        datasets.append(
            {
                "csv_path": csv_path,
                "process_name": infer_process_name_from_csv(csv_path),
                "timestamps": timestamps,
                "rss_mib": rss_mib,
                "swap_mib": swap_mib,
                "oom_scores": oom_scores,
                "dram_avg_mib": dram_avg_mib,
                "dram_min_mib": dram_min_mib,
                "dram_max_mib": dram_max_mib,
                "dram_chip_count": dram_chip_count,
                "has_dram_data": has_dram_data,
            }
        )

    if not datasets:
        print("No samples collected across input CSVs, skipping plot.")
        return

    nrows = len(datasets)
    fig, axes = plt.subplots(nrows, 1, figsize=(12, 4.5 * nrows), sharex=True)
    if nrows == 1:
        axes = [axes]

    global_start = min(min(d["timestamps"]) for d in datasets)
    global_end = max(max(d["timestamps"]) for d in datasets)

    for ax, data in zip(axes, datasets):
        timestamps = data["timestamps"]
        rss_mib = data["rss_mib"]
        swap_mib = data["swap_mib"]
        oom_scores = data["oom_scores"]
        has_dram_data = data["has_dram_data"]
        dram_avg_mib = data["dram_avg_mib"]
        dram_min_mib = data["dram_min_mib"]
        dram_max_mib = data["dram_max_mib"]
        dram_chip_count = data["dram_chip_count"]

        ax.plot(timestamps, rss_mib, linewidth=1.5, label="RSS (MiB)", color="tab:blue")
        ax.plot(timestamps, swap_mib, linewidth=1.5, label="Swap (MiB)", color="tab:orange")

        if has_dram_data:
            n_chips = max(dram_chip_count) if dram_chip_count else 0
            ax.fill_between(
                timestamps,
                dram_min_mib,
                dram_max_mib,
                alpha=0.12,
                color="tab:green",
                label=f"DRAM Min–Max ({n_chips} chip(s))",
            )
            ax.plot(
                timestamps,
                dram_avg_mib,
                linewidth=1.8,
                color="tab:green",
                label="DRAM Avg/chip (MiB)",
            )
            ax.plot(
                timestamps,
                dram_min_mib,
                linewidth=0.8,
                color="seagreen",
                linestyle="--",
                label="DRAM Min/chip (MiB)",
            )
            ax.plot(
                timestamps,
                dram_max_mib,
                linewidth=0.8,
                color="darkgreen",
                linestyle="--",
                label="DRAM Max/chip (MiB)",
            )

        ax.set_ylabel("Memory (MiB)")
        ax.set_title(f"{data['process_name']} — {data['csv_path'].name}")
        apply_grid_with_subgrid(ax)
        ax.set_xlim(global_start, global_end)

        has_oom_data = any(score is not None for score in oom_scores)
        if has_oom_data:
            ax_oom = ax.twinx()
            ax_oom.plot(
                timestamps,
                oom_scores,
                linewidth=1.5,
                label="OOM Score",
                color="tab:red",
                linestyle="--",
            )
            ax_oom.set_ylabel("OOM Score (0–1000)")
            ax_oom.set_ylim(0, 1000)
            # OOM plotted on secondary axis; legend is managed at figure level.

    for ax in axes:
        ax.set_xlabel("Time (UTC)")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
        ax.tick_params(axis="x", which="both", labelbottom=True)

    has_any_dram_data = any(d["has_dram_data"] for d in datasets)
    has_any_oom_data = any(any(score is not None for score in d["oom_scores"]) for d in datasets)
    legend_handles = _build_png_legend_handles(has_any_dram_data, has_any_oom_data)
    fig.legend(
        handles=legend_handles,
        loc="lower center",
        bbox_to_anchor=(0.5, 0.01),
        ncol=4,
        fontsize=8,
        framealpha=0.9,
        columnspacing=1.2,
        handlelength=2.0,
    )

    fig.autofmt_xdate()
    plt.tight_layout(rect=[0.0, 0.12, 1.0, 1.0])
    plt.savefig(png_path, dpi=150)
    plt.close(fig)
    print(f"Saved plot: {png_path}")


def generate_plot_html(csv_path: Path, html_path: Path, process_name: str):
    """Interactive HTML via plotly."""
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
    dram_avg_mib = []
    dram_min_mib = []
    dram_max_mib = []
    dram_chip_count = []
    dram_present_flags = []

    with csv_path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        has_dram = "dram_avg_mib" in fieldnames
        for row in reader:
            timestamps.append(datetime.fromisoformat(row["timestamp_utc"]))
            rss_mib.append(_row_float(row, "rss_mib"))
            swap_mib.append(_row_float(row, "swap_mib"))
            oom_scores.append(_row_int_or_none(row, "oom_score"))
            if has_dram:
                dram_avg_mib.append(_row_float(row, "dram_avg_mib"))
                dram_min_mib.append(_row_float(row, "dram_min_mib"))
                dram_max_mib.append(_row_float(row, "dram_max_mib"))
                dram_chip_count.append(_row_int_or_none(row, "dram_chip_count") or 0)
                dram_present_flags.append(
                    row.get("dram_avg_mib") not in (None, "")
                    or row.get("dram_min_mib") not in (None, "")
                    or row.get("dram_max_mib") not in (None, "")
                )

    if not timestamps:
        print("No samples collected, skipping interactive plot.")
        return

    has_dram_data = has_dram and any(dram_present_flags)
    has_oom_data = any(s is not None for s in oom_scores)

    specs = [[{"secondary_y": has_oom_data}]]
    fig = make_subplots(rows=1, cols=1, shared_xaxes=True, vertical_spacing=0.08, specs=specs)

    fig.add_trace(go.Scatter(x=timestamps, y=rss_mib, mode="lines", name="RSS (MiB)", line={"width": 2, "color": "royalblue"}), row=1, col=1)
    fig.add_trace(go.Scatter(x=timestamps, y=swap_mib, mode="lines", name="Swap (MiB)", line={"width": 2, "color": "orange"}), row=1, col=1)
    if has_dram_data:
        n_chips = max(dram_chip_count) if dram_chip_count else 0
        fig.add_trace(
            go.Scatter(
                x=timestamps,
                y=dram_max_mib,
                mode="lines",
                line={"width": 0, "color": "rgba(0,0,0,0)"},
                hoverinfo="skip",
                showlegend=False,
                name=f"DRAM Min–Max ({n_chips} chip(s))",
            ),
            row=1,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=timestamps,
                y=dram_min_mib,
                mode="lines",
                fill="tonexty",
                fillcolor="rgba(44, 160, 44, 0.12)",
                line={"width": 0, "color": "rgba(0,0,0,0)"},
                name=f"DRAM Min–Max ({n_chips} chip(s))",
            ),
            row=1,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=timestamps,
                y=dram_avg_mib,
                mode="lines",
                name="DRAM Avg/chip (MiB)",
                line={"width": 1.8, "color": "green"},
            ),
            row=1,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=timestamps,
                y=dram_min_mib,
                mode="lines",
                name="DRAM Min/chip (MiB)",
                line={"width": 0.8, "color": "seagreen", "dash": "dash"},
            ),
            row=1,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=timestamps,
                y=dram_max_mib,
                mode="lines",
                name="DRAM Max/chip (MiB)",
                line={"width": 0.8, "color": "darkgreen", "dash": "dash"},
            ),
            row=1,
            col=1,
        )
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
# Live dashboard
# ---------------------------------------------------------------------------

def _build_live_dashboard_html(process_name: str, refresh_ms: int) -> str:
    safe_name = json.dumps(process_name)
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Live Memory Dashboard</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    body {{
      font-family: sans-serif;
      margin: 0;
      padding: 12px 16px;
      background: #fafafa;
    }}
    #header {{
      display: flex;
      gap: 12px;
      align-items: baseline;
      flex-wrap: wrap;
      margin-bottom: 8px;
    }}
    #plot {{
      width: 100%;
      height: 78vh;
      min-height: 520px;
      background: white;
      border: 1px solid #ddd;
      border-radius: 6px;
    }}
    .muted {{
      color: #666;
    }}
  </style>
</head>
<body>
  <div id="header">
    <h2 style="margin: 0;">Live Memory Dashboard</h2>
    <div id="proc" class="muted"></div>
    <div id="status" class="muted">Connecting...</div>
  </div>
  <div id="plot"></div>
  <script>
    const processName = {safe_name};
    const refreshMs = {refresh_ms};
    let lastTimestamp = null;
    let hasOomData = false;
    let hasDramData = false;

    const traces = [
      {{ name: "RSS (MiB)", x: [], y: [], mode: "lines", line: {{ width: 1.5, color: "royalblue" }}, yaxis: "y" }},
      {{ name: "Swap (MiB)", x: [], y: [], mode: "lines", line: {{ width: 1.5, color: "orange" }}, yaxis: "y" }},
      {{ name: "DRAM Avg/chip (MiB)", x: [], y: [], mode: "lines", line: {{ width: 1.8, color: "green" }}, yaxis: "y", visible: "legendonly" }},
      {{ name: "DRAM Min/chip (MiB)", x: [], y: [], mode: "lines", line: {{ width: 0.8, color: "seagreen", dash: "dash" }}, yaxis: "y", visible: "legendonly" }},
      {{ name: "DRAM Max/chip (MiB)", x: [], y: [], mode: "lines", line: {{ width: 0.8, color: "darkgreen", dash: "dash" }}, yaxis: "y", visible: "legendonly" }},
      {{ name: "OOM Score", x: [], y: [], mode: "lines", line: {{ width: 1.5, color: "red", dash: "dash" }}, yaxis: "y2", visible: "legendonly" }},
    ];

    const layout = {{
      title: `Process Memory Over Time (${{processName}})`,
      margin: {{ t: 48, r: 56, b: 56, l: 70 }},
      xaxis: {{ title: "Time (UTC)" }},
      yaxis: {{ title: "Memory (MiB)" }},
      yaxis2: {{ title: "OOM Score (0-1000)", overlaying: "y", side: "right", range: [0, 1000], visible: false }},
      legend: {{ orientation: "h", x: 0, y: 1.1 }},
      hovermode: "x unified",
      template: "plotly_white"
    }};
    Plotly.newPlot("plot", traces, layout, {{ responsive: true }});

    function setStatus(text) {{
      document.getElementById("status").textContent = text;
    }}

    function applyVisibility() {{
      Plotly.restyle("plot", {{ visible: hasDramData ? true : "legendonly" }}, [2]);
      Plotly.restyle("plot", {{ visible: hasDramData ? true : "legendonly" }}, [3]);
      Plotly.restyle("plot", {{ visible: hasDramData ? true : "legendonly" }}, [4]);
      Plotly.restyle("plot", {{ visible: hasOomData ? true : "legendonly" }}, [5]);
      Plotly.relayout("plot", {{ "yaxis2.visible": hasOomData }});
    }}

    function queueSample(sample, xUpdates, yUpdates) {{
      const t = sample.timestamp_utc;
      xUpdates[0].push(t); yUpdates[0].push(sample.rss_mib);
      xUpdates[1].push(t); yUpdates[1].push(sample.swap_mib);

      if (sample.oom_score !== null) {{
        hasOomData = true;
        xUpdates[5].push(t); yUpdates[5].push(sample.oom_score);
      }}
      if (sample.dram_avg_mib !== null || sample.dram_min_mib !== null || sample.dram_max_mib !== null) {{
        hasDramData = true;
        if (sample.dram_avg_mib !== null) {{ xUpdates[2].push(t); yUpdates[2].push(sample.dram_avg_mib); }}
        if (sample.dram_min_mib !== null) {{ xUpdates[3].push(t); yUpdates[3].push(sample.dram_min_mib); }}
        if (sample.dram_max_mib !== null) {{ xUpdates[4].push(t); yUpdates[4].push(sample.dram_max_mib); }}
      }}

      lastTimestamp = sample.timestamp_utc;
    }}

    async function refreshLoop() {{
      try {{
        const q = lastTimestamp ? `?since=${{encodeURIComponent(lastTimestamp)}}` : "";
        const resp = await fetch(`/api/samples${{q}}`, {{ cache: "no-store" }});
        if (!resp.ok) {{
          setStatus(`Error: HTTP ${{resp.status}}`);
          return;
        }}

        const payload = await resp.json();
        const xUpdates = [[], [], [], [], [], []];
        const yUpdates = [[], [], [], [], [], []];
        for (const sample of payload.samples) {{
          queueSample(sample, xUpdates, yUpdates);
        }}

        const traceIndices = [];
        const update = {{ x: [], y: [] }};
        for (let i = 0; i < xUpdates.length; i += 1) {{
          if (xUpdates[i].length > 0) {{
            traceIndices.push(i);
            update.x.push(xUpdates[i]);
            update.y.push(yUpdates[i]);
          }}
        }}
        if (traceIndices.length > 0) {{
          Plotly.extendTraces("plot", update, traceIndices);
        }}

        applyVisibility();
        const doneText = payload.sampling_done ? " (sampling finished)" : "";
        setStatus(`Updated: ${{payload.sample_count}} samples${{doneText}}`);
      }} catch (err) {{
        setStatus(`Error: ${{err}}`);
      }}
    }}

    async function init() {{
      try {{
        const metaResp = await fetch("/api/meta", {{ cache: "no-store" }});
        const meta = await metaResp.json();
        document.getElementById("proc").textContent = `PID=${{meta.pid}}  CSV=${{meta.csv_path}}`;
      }} catch (_e) {{
        document.getElementById("proc").textContent = "Metadata unavailable";
      }}
      await refreshLoop();
      setInterval(refreshLoop, refreshMs);
    }}

    init();
  </script>
</body>
</html>
"""


def _build_csv_header(include_dram: bool) -> list[str]:
    header = [
        "timestamp_utc", "pid", "process_name",
        "rss_bytes", "rss_mib",
        "swap_bytes", "swap_mib",
        "oom_score",
    ]
    if include_dram:
        header += list(_DRAM_CSV_COLS)
    return header


def _collect_sample_row(
    proc: psutil.Process,
    target_pid: int,
    proc_name: str,
    target_create_time: float,
    dram_sampler: DeviceDramSampler | None,
) -> list[str | int | float] | None:
    try:
        if not proc.is_running() or proc.create_time() != target_create_time:
            print(f"PID {target_pid} exited/restarted; stopping sampler.")
            return None
        # Avoid memory_full_info() — on Linux it parses /proc/[pid]/smaps
        # which is expensive and perturbs high-frequency sampling.
        mem_info = proc.memory_info()
        rss_bytes = mem_info.rss
        swap_bytes = get_swap_bytes(target_pid)
        oom_score = get_oom_score(target_pid)
    except psutil.NoSuchProcess:
        print(f"PID {target_pid} exited; stopping sampler.")
        return None

    ts = datetime.now(timezone.utc).isoformat()
    row: list[str | int | float] = [
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

    return row


class LiveCsvState:
    def __init__(self, csv_path: Path, process_name: str, pid: int, refresh_ms: int):
        self.csv_path = csv_path
        self.process_name = process_name
        self.pid = pid
        self.refresh_ms = refresh_ms
        self.lock = threading.Lock()
        self.file_offset = 0
        self.partial_line = ""
        self.fieldnames: list[str] | None = None
        self.samples: list[dict[str, str | int | float | None]] = []
        self.has_oom_data = False
        self.has_dram_data = False
        self.sampling_done = False

    def mark_sampling_done(self):
        with self.lock:
            self.sampling_done = True

    def ingest_new_rows(self):
        try:
            with self.csv_path.open("r", newline="") as f:
                f.seek(self.file_offset)
                chunk = f.read()
                self.file_offset = f.tell()
        except FileNotFoundError:
            return

        if not chunk:
            return

        text = self.partial_line + chunk
        if not text:
            return
        if not text.endswith("\n"):
            if "\n" not in text:
                self.partial_line = text
                return
            text, self.partial_line = text.rsplit("\n", 1)
        else:
            self.partial_line = ""

        lines = text.splitlines()
        if not lines:
            return

        start_idx = 0
        if self.fieldnames is None:
            self.fieldnames = next(csv.reader([lines[0]]), [])
            start_idx = 1

        if start_idx >= len(lines) or not self.fieldnames:
            return

        reader = csv.DictReader(io.StringIO("\n".join(lines[start_idx:])), fieldnames=self.fieldnames)
        new_samples = []
        has_oom_data = False
        has_dram_data = False

        for row in reader:
            timestamp_utc = (row.get("timestamp_utc") or "").strip()
            if not timestamp_utc:
                continue

            oom_score = _row_int_or_none(row, "oom_score")
            dram_avg = row.get("dram_avg_mib")
            dram_min = row.get("dram_min_mib")
            dram_max = row.get("dram_max_mib")
            dram_avg_mib = _row_float(row, "dram_avg_mib") if dram_avg not in (None, "") else None
            dram_min_mib = _row_float(row, "dram_min_mib") if dram_min not in (None, "") else None
            dram_max_mib = _row_float(row, "dram_max_mib") if dram_max not in (None, "") else None

            sample = {
                "timestamp_utc": timestamp_utc,
                "rss_mib": _row_float(row, "rss_mib"),
                "swap_mib": _row_float(row, "swap_mib"),
                "oom_score": oom_score,
                "dram_avg_mib": dram_avg_mib,
                "dram_min_mib": dram_min_mib,
                "dram_max_mib": dram_max_mib,
            }
            new_samples.append(sample)

            if oom_score is not None:
                has_oom_data = True
            if dram_avg_mib is not None or dram_min_mib is not None or dram_max_mib is not None:
                has_dram_data = True

        if not new_samples:
            return

        with self.lock:
            self.samples.extend(new_samples)
            self.has_oom_data = self.has_oom_data or has_oom_data
            self.has_dram_data = self.has_dram_data or has_dram_data

    def get_meta(self) -> dict[str, str | int | bool]:
        with self.lock:
            return {
                "process_name": self.process_name,
                "pid": self.pid,
                "csv_path": str(self.csv_path),
                "refresh_ms": self.refresh_ms,
                "has_oom_data": self.has_oom_data,
                "has_dram_data": self.has_dram_data,
                "sampling_done": self.sampling_done,
            }

    def get_samples_since(self, since: str | None) -> dict[str, object]:
        with self.lock:
            if since:
                samples = [sample for sample in self.samples if str(sample["timestamp_utc"]) > since]
            else:
                samples = list(self.samples)
            return {
                "samples": samples,
                "sample_count": len(self.samples),
                "sampling_done": self.sampling_done,
                "has_oom_data": self.has_oom_data,
                "has_dram_data": self.has_dram_data,
            }


def _make_live_handler(state: LiveCsvState):
    class _LiveHandler(BaseHTTPRequestHandler):
        def _send(self, code: int, content_type: str, body: str):
            payload = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/":
                html = _build_live_dashboard_html(state.process_name, state.refresh_ms)
                self._send(200, "text/html; charset=utf-8", html)
                return

            if parsed.path == "/api/meta":
                self._send(200, "application/json; charset=utf-8", json.dumps(state.get_meta()))
                return

            if parsed.path == "/api/samples":
                since = parse_qs(parsed.query).get("since", [None])[0]
                payload = state.get_samples_since(since)
                self._send(200, "application/json; charset=utf-8", json.dumps(payload))
                return

            self._send(404, "text/plain; charset=utf-8", "Not found")

        def log_message(self, format: str, *args):
            return

    return _LiveHandler


def _sampling_worker(
    proc: psutil.Process,
    target_pid: int,
    proc_name: str,
    target_create_time: float,
    args,
    dram_sampler: DeviceDramSampler | None,
    stop_event: threading.Event,
    live_state: LiveCsvState | None = None,
):
    try:
        with args.csv.open("a", newline="") as f:
            writer = csv.writer(f)
            while not stop_event.is_set():
                row = _collect_sample_row(
                    proc, target_pid, proc_name, target_create_time, dram_sampler
                )
                if row is None:
                    break
                writer.writerow(row)
                f.flush()
                if stop_event.wait(args.interval):
                    break
    finally:
        if live_state is not None:
            live_state.mark_sampling_done()


def _live_tail_worker(live_state: LiveCsvState, stop_event: threading.Event):
    poll_seconds = max(0.2, live_state.refresh_ms / 1000.0)
    while not stop_event.is_set():
        live_state.ingest_new_rows()
        stop_event.wait(poll_seconds)
    live_state.ingest_new_rows()


def run_live_monitor(args):
    if args.live_refresh_ms <= 0:
        print("--live-refresh-ms must be > 0.")
        return

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

    if dram_sampler is not None and not glob.glob(DeviceDramSampler._SHM_GLOB):
        print("WARNING: --device-dram enabled but no /dev/shm/tt_device_*_memory files found. "
              "Will keep trying each interval.")

    with args.csv.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(_build_csv_header(dram_sampler is not None))
        f.flush()

    live_state = LiveCsvState(args.csv, proc_name, target_pid, args.live_refresh_ms)
    stop_event = threading.Event()

    handler_cls = _make_live_handler(live_state)
    try:
        server = ThreadingHTTPServer((args.live_host, args.live_port), handler_cls)
    except OSError as exc:
        print(f"Failed to bind live server on {args.live_host}:{args.live_port}: {exc}")
        return

    sample_thread = threading.Thread(
        target=_sampling_worker,
        args=(proc, target_pid, proc_name, target_create_time, args, dram_sampler, stop_event, live_state),
        daemon=True,
    )
    tail_thread = threading.Thread(
        target=_live_tail_worker,
        args=(live_state, stop_event),
        daemon=True,
    )
    sample_thread.start()
    tail_thread.start()

    print(
        f"Live mode enabled for PID={target_pid} ({proc_name}) every {args.interval}s"
        + (" + device DRAM" if dram_sampler else "")
        + f"\nCSV → {args.csv}"
        + f"\nLive dashboard → http://{args.live_host}:{args.live_port}"
        + f"\nTunnel (run on local machine): ssh -L {args.live_port}:localhost:{args.live_port} user@remote-host"
        + "\nCtrl+C to stop."
    )

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping live dashboard...")
    finally:
        stop_event.set()
        server.shutdown()
        server.server_close()
        sample_thread.join(timeout=2.0)
        tail_thread.join(timeout=2.0)


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

    header = _build_csv_header(dram_sampler is not None)

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
                row = _collect_sample_row(
                    proc, target_pid, proc_name, target_create_time, dram_sampler
                )
                if row is None:
                    break

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

    if args.live and args.from_csv:
        print("--live cannot be combined with --from-csv.")
        return

    if args.from_csv:
        csv_paths = args.from_csv
        if len(csv_paths) > 1:
            output_kind, output_path = _resolve_output(
                args,
                csv_paths[0].with_name("merged_pid_memory.png"),
                csv_paths[0].with_name("merged_pid_memory.html"),
            )
            if output_kind == "html":
                print("Multi-CSV rendering is currently PNG-only. Use --png or omit --html.")
                return
            generate_plot_multi_csv(csv_paths, output_path)
            return

        output_kind, output_path = _resolve_output(
            args,
            csv_paths[0].with_suffix(".png"),
            csv_paths[0].with_suffix(".html"),
        )
        process_name = infer_process_name_from_csv(csv_paths[0])
        _render(csv_paths[0], output_kind, output_path, process_name)
        return

    if args.live:
        run_live_monitor(args)
        return

    run_monitor(args)


if __name__ == "__main__":
    main()
