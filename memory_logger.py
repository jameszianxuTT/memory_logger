#!/usr/bin/env python3
# pip install psutil matplotlib
# python memory_logger.py --pid 12345 --interval 0.1 --csv memory.csv --png memory.png
# python memory_logger.py --name worker --interval 0.1 --csv memory.csv --png memory.png
# python memory_logger.py --from-csv memory.csv --png memory.png
# python memory_logger.py --from-csv memory.csv --html memory.html
# python memory_logger.py --top 10 --interval 0.5 --csv top_memory.csv --png top_memory.png

import argparse
import csv
import time
from datetime import datetime, timezone
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import psutil
from matplotlib.ticker import AutoMinorLocator


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
                    # VmSwap is reported as: "VmSwap:\t<value> kB"
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
                    break
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, OSError):
        pass
    return 0


def get_vmstat_swap() -> tuple[int, int]:
    """Read pswpin and pswpout from /proc/vmstat (in pages, typically 4KB each)."""
    pswpin = 0
    pswpout = 0
    try:
        with open("/proc/vmstat") as f:
            for line in f:
                if line.startswith("pswpin "):
                    pswpin = int(line.split()[1])
                elif line.startswith("pswpout "):
                    pswpout = int(line.split()[1])
    except (FileNotFoundError, PermissionError, ValueError, OSError):
        pass
    return pswpin, pswpout


def pages_to_mib(pages: int) -> float:
    """Convert pages to MiB (assumes 4KB page size)."""
    return (pages * 4096) / (1024 * 1024)


def apply_grid_with_subgrid(ax, y_minor_divisions: int = 2):
    """Enable major and minor grid lines for readability."""
    ax.minorticks_on()
    ax.yaxis.set_minor_locator(AutoMinorLocator(y_minor_divisions))
    ax.grid(True, which="major", alpha=0.3)
    ax.grid(True, which="minor", alpha=0.15, linestyle=":")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Log RSS, swap, and OOM score for a specific PID and plot it, or render plot output(s) from an existing CSV."
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
        help="Read an existing CSV and generate plot output(s) without attaching to a process",
    )
    target_group.add_argument(
        "--top",
        type=int,
        metavar="N",
        help="Monitor top N memory-consuming processes system-wide",
    )
    parser.add_argument("--interval", type=float, default=1.0, help="Sampling interval seconds")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.5,
        help="How often to poll for process name matches",
    )
    parser.add_argument("--csv", type=Path, default=Path("pid_memory.csv"), help="CSV output path")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "--png",
        type=Path,
        nargs="?",
        const=Path("pid_memory.png"),
        default=None,
        help="Generate PNG plot at this path (default if no output option is given).",
    )
    output_group.add_argument(
        "--html",
        type=Path,
        nargs="?",
        const=Path("pid_memory.html"),
        default=None,
        help="Generate interactive HTML plot at this path.",
    )
    return parser.parse_args()


def resolve_output_target(args, default_png_path: Path) -> tuple[str, Path]:
    if args.html is not None:
        return "html", args.html
    if args.png is not None:
        return "png", args.png
    return "png", default_png_path


def output_target_text(output_kind: str, output_path: Path) -> str:
    if output_kind == "html":
        return f"interactive HTML: {output_path}"
    return f"PNG: {output_path}"


def find_process_by_name_exact(name_exact: str):
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            info = proc.info
            proc_name = info.get("name") or ""
            if proc_name == name_exact:
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

    print(
        f"Waiting for process with exact name '{args.name}' "
        f"(poll every {args.poll_interval}s)..."
    )
    while True:
        proc = find_process_by_name_exact(args.name)
        if proc is not None:
            return proc
        time.sleep(args.poll_interval)


def generate_plot(csv_path: Path, png_path: Path | None, process_name: str):
    timestamps = []
    rss_mib = []
    swap_mib = []
    oom_scores = []
    pswpin_delta_mib = []
    pswpout_delta_mib = []

    with csv_path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        has_vmstat = "pswpin_delta_mib" in fieldnames
        for row in reader:
            timestamps.append(datetime.fromisoformat(row["timestamp_utc"]))
            rss_mib.append(float(row["rss_mib"]))
            swap_mib.append(float(row["swap_mib"]))
            oom_scores.append(int(row["oom_score"]) if row["oom_score"] else None)
            if has_vmstat:
                pswpin_delta_mib.append(float(row["pswpin_delta_mib"]))
                pswpout_delta_mib.append(float(row["pswpout_delta_mib"]))

    if not timestamps:
        print("No samples collected, skipping plot.")
        return

    # Determine subplot layout based on available data
    has_vmstat_data = has_vmstat and any(v > 0 for v in pswpin_delta_mib + pswpout_delta_mib)
    nrows = 2 if has_vmstat_data else 1
    fig, axes = plt.subplots(nrows, 1, figsize=(10, 4.5 * nrows), sharex=True)
    if nrows == 1:
        axes = [axes]
    ax1 = axes[0]

    # Memory on left y-axis
    ax1.plot(timestamps, rss_mib, linewidth=2, label="RSS (MiB)", color="tab:blue")
    ax1.plot(timestamps, swap_mib, linewidth=2, label="Swap (MiB)", color="tab:orange")
    ax1.set_ylabel("Memory (MiB)")
    apply_grid_with_subgrid(ax1)

    # OOM score on right y-axis (if available)
    has_oom_data = any(s is not None for s in oom_scores)
    if has_oom_data:
        ax2 = ax1.twinx()
        ax2.plot(timestamps, oom_scores, linewidth=2, label="OOM Score", color="tab:red", linestyle="--")
        ax2.set_ylabel("OOM Score (0-1000)")
        ax2.set_ylim(0, 1000)
        # Combine legends from both axes
        lines1, labels1 = ax1.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left")
    else:
        ax1.legend(loc="upper left")

    ax1.set_title(f"Process Memory Over Time ({process_name})")

    # Vmstat swap I/O subplot
    if has_vmstat_data:
        ax_vmstat = axes[1]
        ax_vmstat.plot(timestamps, pswpin_delta_mib, linewidth=2, label="Swap In (MiB)", color="tab:green")
        ax_vmstat.plot(timestamps, pswpout_delta_mib, linewidth=2, label="Swap Out (MiB)", color="tab:purple")
        ax_vmstat.set_ylabel("Cumulative Swap I/O (MiB)")
        ax_vmstat.set_xlabel("Time (UTC)")
        apply_grid_with_subgrid(ax_vmstat)
        ax_vmstat.legend(loc="upper left")
        ax_vmstat.set_title("System-wide Swap I/O (since monitoring started)")
        ax_vmstat.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
    else:
        ax1.set_xlabel("Time (UTC)")
        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))

    fig.autofmt_xdate()
    plt.tight_layout()
    if png_path is None:
        plt.close(fig)
        return

    plt.savefig(png_path, dpi=150)
    plt.close(fig)
    print(f"Saved plot: {png_path}")


def generate_plot_html(csv_path: Path, html_path: Path, process_name: str):
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        print("Interactive HTML output requires plotly. Install with: pip install plotly")
        return

    timestamps = []
    rss_mib = []
    swap_mib = []
    oom_scores = []
    pswpin_delta_mib = []
    pswpout_delta_mib = []

    with csv_path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        has_vmstat = "pswpin_delta_mib" in fieldnames
        for row in reader:
            timestamps.append(datetime.fromisoformat(row["timestamp_utc"]))
            rss_mib.append(float(row["rss_mib"]))
            swap_mib.append(float(row["swap_mib"]))
            oom_scores.append(int(row["oom_score"]) if row["oom_score"] else None)
            if has_vmstat:
                pswpin_delta_mib.append(float(row["pswpin_delta_mib"]))
                pswpout_delta_mib.append(float(row["pswpout_delta_mib"]))

    if not timestamps:
        print("No samples collected, skipping interactive plot.")
        return

    has_oom_data = any(s is not None for s in oom_scores)
    has_vmstat_data = has_vmstat and any(v > 0 for v in pswpin_delta_mib + pswpout_delta_mib)
    nrows = 2 if has_vmstat_data else 1

    specs = [[{"secondary_y": has_oom_data}]]
    if has_vmstat_data:
        specs.append([{"secondary_y": False}])
    fig = make_subplots(rows=nrows, cols=1, shared_xaxes=True, vertical_spacing=0.08, specs=specs)

    fig.add_trace(go.Scatter(x=timestamps, y=rss_mib, mode="lines", name="RSS (MiB)", line={"width": 2, "color": "royalblue"}), row=1, col=1)
    fig.add_trace(go.Scatter(x=timestamps, y=swap_mib, mode="lines", name="Swap (MiB)", line={"width": 2, "color": "orange"}), row=1, col=1)
    if has_oom_data:
        fig.add_trace(
            go.Scatter(
                x=timestamps,
                y=oom_scores,
                mode="lines",
                name="OOM Score",
                line={"width": 2, "color": "red", "dash": "dash"},
            ),
            row=1,
            col=1,
            secondary_y=True,
        )
        fig.update_yaxes(title_text="OOM Score (0-1000)", range=[0, 1000], row=1, col=1, secondary_y=True)

    fig.update_yaxes(title_text="Memory (MiB)", row=1, col=1)

    if has_vmstat_data:
        fig.add_trace(
            go.Scatter(x=timestamps, y=pswpin_delta_mib, mode="lines", name="Swap In (MiB)", line={"width": 2, "color": "green"}),
            row=2,
            col=1,
        )
        fig.add_trace(
            go.Scatter(x=timestamps, y=pswpout_delta_mib, mode="lines", name="Swap Out (MiB)", line={"width": 2, "color": "purple"}),
            row=2,
            col=1,
        )
        fig.update_yaxes(title_text="Cumulative Swap I/O (MiB)", row=2, col=1)
        fig.update_xaxes(title_text="Time (UTC)", row=2, col=1)
    else:
        fig.update_xaxes(title_text="Time (UTC)", row=1, col=1)

    fig.update_layout(
        title=f"Process Memory Over Time ({process_name})",
        hovermode="x unified",
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "left", "x": 0},
        template="plotly_white",
        height=450 if nrows == 1 else 800,
    )
    fig.write_html(html_path, include_plotlyjs="cdn")
    print(f"Saved interactive plot: {html_path}")


def get_top_processes(n: int) -> list[dict]:
    """Get top N processes by RSS memory usage."""
    procs = []
    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            info = proc.info
            mem_info = info.get("memory_info")
            if mem_info is None:
                continue
            procs.append({
                "pid": info["pid"],
                "name": info["name"] or "unknown",
                "rss": mem_info.rss,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    # Sort by RSS descending and return top N
    procs.sort(key=lambda x: x["rss"], reverse=True)
    return procs[:n]


def generate_top_plot(csv_path: Path, png_path: Path | None):
    """Generate a plot showing RSS over time for multiple processes."""
    # Read CSV and organize by process key (pid:name)
    from collections import defaultdict

    process_data = defaultdict(lambda: {"timestamps": [], "rss_mib": []})
    vmstat_data = {"timestamps": [], "pswpin_delta_mib": [], "pswpout_delta_mib": []}
    seen_timestamps = set()

    with csv_path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        has_vmstat = "pswpin_delta_mib" in fieldnames
        for row in reader:
            ts = datetime.fromisoformat(row["timestamp_utc"])
            # Use pid:name as key to handle pid reuse
            proc_key = f"{row['pid']}:{row['process_name']}"
            process_data[proc_key]["timestamps"].append(ts)
            process_data[proc_key]["rss_mib"].append(float(row["rss_mib"]))
            process_data[proc_key]["name"] = row["process_name"]
            process_data[proc_key]["pid"] = row["pid"]

            # Collect vmstat data (only once per timestamp since it's system-wide)
            if has_vmstat and ts not in seen_timestamps:
                seen_timestamps.add(ts)
                vmstat_data["timestamps"].append(ts)
                vmstat_data["pswpin_delta_mib"].append(float(row["pswpin_delta_mib"]))
                vmstat_data["pswpout_delta_mib"].append(float(row["pswpout_delta_mib"]))

    if not process_data:
        print("No samples collected, skipping plot.")
        return

    # Find processes with highest peak RSS for legend ordering
    peak_rss = {k: max(v["rss_mib"]) for k, v in process_data.items()}
    sorted_procs = sorted(process_data.keys(), key=lambda k: peak_rss[k], reverse=True)

    # Determine subplot layout based on available data
    has_vmstat_data = has_vmstat and any(v > 0 for v in vmstat_data["pswpin_delta_mib"] + vmstat_data["pswpout_delta_mib"])
    nrows = 2 if has_vmstat_data else 1
    fig, axes = plt.subplots(nrows, 1, figsize=(14, 6 * nrows), sharex=True)
    if nrows == 1:
        axes = [axes]
    ax = axes[0]

    # Limit legend to top 15 processes, group rest as "others"
    max_legend = 15

    # Use a colormap that handles many processes
    colors = plt.cm.tab20.colors + plt.cm.tab20b.colors + plt.cm.tab20c.colors

    for i, proc_key in enumerate(sorted_procs[:max_legend]):
        data = process_data[proc_key]
        color = colors[i % len(colors)]
        label = f"{data['name']} (pid {data['pid']}, peak {peak_rss[proc_key]:.0f} MiB)"
        ax.plot(data["timestamps"], data["rss_mib"], linewidth=1.5, label=label, color=color)

    # Plot remaining processes with thin gray lines (no legend)
    for proc_key in sorted_procs[max_legend:]:
        data = process_data[proc_key]
        ax.plot(data["timestamps"], data["rss_mib"], linewidth=0.5, color="gray", alpha=0.3)

    ax.set_ylabel("RSS (MiB)")
    ax.set_title(f"Top Memory Consumers Over Time ({len(process_data)} processes tracked)")
    apply_grid_with_subgrid(ax)

    # Place legend outside plot
    ax.legend(loc="upper left", bbox_to_anchor=(1.02, 1), fontsize=8)

    # Vmstat swap I/O subplot
    if has_vmstat_data:
        ax_vmstat = axes[1]
        ax_vmstat.plot(vmstat_data["timestamps"], vmstat_data["pswpin_delta_mib"], linewidth=2, label="Swap In (MiB)", color="tab:green")
        ax_vmstat.plot(vmstat_data["timestamps"], vmstat_data["pswpout_delta_mib"], linewidth=2, label="Swap Out (MiB)", color="tab:purple")
        ax_vmstat.set_ylabel("Cumulative Swap I/O (MiB)")
        ax_vmstat.set_xlabel("Time (UTC)")
        apply_grid_with_subgrid(ax_vmstat)
        ax_vmstat.legend(loc="upper left")
        ax_vmstat.set_title("System-wide Swap I/O (since monitoring started)")
        ax_vmstat.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
    else:
        ax.set_xlabel("Time (UTC)")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))

    fig.autofmt_xdate()
    plt.tight_layout()
    if png_path is None:
        plt.close(fig)
        return

    plt.savefig(png_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved plot: {png_path}")


def generate_top_plot_html(csv_path: Path, html_path: Path):
    """Generate an interactive HTML plot showing RSS over time for multiple processes."""
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        print("Interactive HTML output requires plotly. Install with: pip install plotly")
        return

    from collections import defaultdict

    process_data = defaultdict(lambda: {"timestamps": [], "rss_mib": []})
    vmstat_data = {"timestamps": [], "pswpin_delta_mib": [], "pswpout_delta_mib": []}
    seen_timestamps = set()

    with csv_path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        has_vmstat = "pswpin_delta_mib" in fieldnames
        for row in reader:
            ts = datetime.fromisoformat(row["timestamp_utc"])
            proc_key = f"{row['pid']}:{row['process_name']}"
            process_data[proc_key]["timestamps"].append(ts)
            process_data[proc_key]["rss_mib"].append(float(row["rss_mib"]))
            process_data[proc_key]["name"] = row["process_name"]
            process_data[proc_key]["pid"] = row["pid"]

            if has_vmstat and ts not in seen_timestamps:
                seen_timestamps.add(ts)
                vmstat_data["timestamps"].append(ts)
                vmstat_data["pswpin_delta_mib"].append(float(row["pswpin_delta_mib"]))
                vmstat_data["pswpout_delta_mib"].append(float(row["pswpout_delta_mib"]))

    if not process_data:
        print("No samples collected, skipping interactive plot.")
        return

    peak_rss = {k: max(v["rss_mib"]) for k, v in process_data.items()}
    sorted_procs = sorted(process_data.keys(), key=lambda k: peak_rss[k], reverse=True)
    has_vmstat_data = has_vmstat and any(v > 0 for v in vmstat_data["pswpin_delta_mib"] + vmstat_data["pswpout_delta_mib"])
    nrows = 2 if has_vmstat_data else 1

    specs = [[{"secondary_y": False}]]
    if has_vmstat_data:
        specs.append([{"secondary_y": False}])
    fig = make_subplots(rows=nrows, cols=1, shared_xaxes=True, vertical_spacing=0.08, specs=specs)

    max_legend = 15
    colors = plt.cm.tab20.colors + plt.cm.tab20b.colors + plt.cm.tab20c.colors

    for i, proc_key in enumerate(sorted_procs[:max_legend]):
        data = process_data[proc_key]
        color = colors[i % len(colors)]
        label = f"{data['name']} (pid {data['pid']}, peak {peak_rss[proc_key]:.0f} MiB)"
        fig.add_trace(
            go.Scatter(
                x=data["timestamps"],
                y=data["rss_mib"],
                mode="lines",
                name=label,
                line={"width": 1.5, "color": f"rgb({int(color[0] * 255)}, {int(color[1] * 255)}, {int(color[2] * 255)})"},
            ),
            row=1,
            col=1,
        )

    for proc_key in sorted_procs[max_legend:]:
        data = process_data[proc_key]
        fig.add_trace(
            go.Scatter(
                x=data["timestamps"],
                y=data["rss_mib"],
                mode="lines",
                name=f"{data['name']} (pid {data['pid']})",
                showlegend=False,
                line={"width": 0.5, "color": "rgba(128,128,128,0.3)"},
            ),
            row=1,
            col=1,
        )

    fig.update_yaxes(title_text="RSS (MiB)", row=1, col=1)

    if has_vmstat_data:
        fig.add_trace(
            go.Scatter(
                x=vmstat_data["timestamps"],
                y=vmstat_data["pswpin_delta_mib"],
                mode="lines",
                name="Swap In (MiB)",
                line={"width": 2, "color": "green"},
            ),
            row=2,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=vmstat_data["timestamps"],
                y=vmstat_data["pswpout_delta_mib"],
                mode="lines",
                name="Swap Out (MiB)",
                line={"width": 2, "color": "purple"},
            ),
            row=2,
            col=1,
        )
        fig.update_yaxes(title_text="Cumulative Swap I/O (MiB)", row=2, col=1)
        fig.update_xaxes(title_text="Time (UTC)", row=2, col=1)
    else:
        fig.update_xaxes(title_text="Time (UTC)", row=1, col=1)

    fig.update_layout(
        title=f"Top Memory Consumers Over Time ({len(process_data)} processes tracked)",
        hovermode="x unified",
        template="plotly_white",
        height=600 if nrows == 1 else 950,
    )
    fig.write_html(html_path, include_plotlyjs="cdn")
    print(f"Saved interactive plot: {html_path}")


def run_top_monitor(args):
    """Monitor top N memory-consuming processes system-wide."""
    seen_processes = set()  # Track all processes we've seen
    output_kind, output_path = resolve_output_target(args, Path("top_memory.png"))

    # Capture initial vmstat values before monitoring starts
    initial_pswpin, initial_pswpout = get_vmstat_swap()

    with args.csv.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp_utc", "pid", "process_name", "rss_bytes", "rss_mib", "swap_bytes", "swap_mib", "pswpin_delta_mib", "pswpout_delta_mib"])

        print(
            f"Monitoring top {args.top} memory-consuming processes every {args.interval}s.\n"
            f"Writing CSV: {args.csv}\nPress Ctrl+C to stop and generate {output_target_text(output_kind, output_path)}."
        )

        try:
            while True:
                ts = datetime.now(timezone.utc).isoformat()
                top_procs = get_top_processes(args.top)

                # Get vmstat deltas (same for all processes in this sample)
                current_pswpin, current_pswpout = get_vmstat_swap()
                pswpin_delta_mib = pages_to_mib(current_pswpin - initial_pswpin)
                pswpout_delta_mib = pages_to_mib(current_pswpout - initial_pswpout)

                for proc in top_procs:
                    pid = proc["pid"]
                    name = proc["name"]
                    rss_bytes = proc["rss"]
                    swap_bytes = get_swap_bytes(pid)

                    seen_processes.add(f"{pid}:{name}")

                    writer.writerow([
                        ts,
                        pid,
                        name,
                        rss_bytes,
                        round(bytes_to_mib(rss_bytes), 3),
                        swap_bytes,
                        round(bytes_to_mib(swap_bytes), 3),
                        round(pswpin_delta_mib, 3),
                        round(pswpout_delta_mib, 3),
                    ])

                f.flush()
                time.sleep(args.interval)

        except KeyboardInterrupt:
            print(f"\nStopping sampler... tracked {len(seen_processes)} unique processes.")

    if output_kind == "html":
        generate_top_plot_html(args.csv, output_path)
    else:
        generate_top_plot(args.csv, output_path)


def infer_process_name_from_csv(csv_path: Path) -> str:
    with csv_path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        first_row = next(reader, None)
        if first_row is None:
            return csv_path.stem
        process_name = (first_row.get("process_name") or "").strip()
        return process_name or csv_path.stem


def main():
    args = parse_args()

    if args.from_csv is not None:
        output_kind, output_path = resolve_output_target(args, args.from_csv.with_suffix(".png"))
        # Detect if this is a top-monitor CSV (no oom_score column) or single-process CSV
        with args.from_csv.open("r", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
        if "oom_score" not in fieldnames:
            if output_kind == "html":
                generate_top_plot_html(args.from_csv, output_path)
            else:
                generate_top_plot(args.from_csv, output_path)
        else:
            process_name = infer_process_name_from_csv(args.from_csv)
            if output_kind == "html":
                generate_plot_html(args.from_csv, output_path, process_name)
            else:
                generate_plot(args.from_csv, output_path, process_name)
        return

    if args.top is not None:
        run_top_monitor(args)
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
    output_kind, output_path = resolve_output_target(args, Path("pid_memory.png"))

    # Capture initial vmstat values before monitoring starts
    initial_pswpin, initial_pswpout = get_vmstat_swap()

    with args.csv.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp_utc", "pid", "process_name", "rss_bytes", "rss_mib", "swap_bytes", "swap_mib", "oom_score", "pswpin_delta_mib", "pswpout_delta_mib"])

        print(
            f"Sampling RSS, swap, and OOM score for PID={target_pid} ({proc_name}) every {args.interval}s.\n"
            f"Writing CSV: {args.csv}\nPress Ctrl+C to stop and generate {output_target_text(output_kind, output_path)}."
        )

        try:
            while True:
                # Process may exit between iterations
                try:
                    if not proc.is_running() or proc.create_time() != target_create_time:
                        print(f"PID {target_pid} exited/restarted; stopping sampler.")
                        break
                    # Do not use memory_full_info() in this hot loop: on Linux it
                    # can be expensive (often parsing detailed memory maps), which
                    # distorts high-frequency sampling and perturbs the target app.
                    mem_info = proc.memory_info()
                    rss_bytes = mem_info.rss
                    swap_bytes = get_swap_bytes(target_pid)
                    oom_score = get_oom_score(target_pid)
                except psutil.NoSuchProcess:
                    print(f"PID {target_pid} exited; stopping sampler.")
                    break

                # Get vmstat deltas
                current_pswpin, current_pswpout = get_vmstat_swap()
                pswpin_delta_mib = pages_to_mib(current_pswpin - initial_pswpin)
                pswpout_delta_mib = pages_to_mib(current_pswpout - initial_pswpout)

                ts = datetime.now(timezone.utc).isoformat()
                writer.writerow([
                    ts,
                    target_pid,
                    proc_name,
                    rss_bytes,
                    round(bytes_to_mib(rss_bytes), 3),
                    swap_bytes,
                    round(bytes_to_mib(swap_bytes), 3),
                    oom_score if oom_score is not None else "",
                    round(pswpin_delta_mib, 3),
                    round(pswpout_delta_mib, 3),
                ])
                f.flush()
                time.sleep(args.interval)

        except KeyboardInterrupt:
            print("\nStopping sampler...")

    if output_kind == "html":
        generate_plot_html(args.csv, output_path, proc_name)
    else:
        generate_plot(args.csv, output_path, proc_name)


if __name__ == "__main__":
    main()
