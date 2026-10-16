"""Detailed static benchmark figures. No application/runtime dependency."""
import html
import json
import math
from pathlib import Path
import statistics


def render_report(data, directory):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch
    import numpy as np

    directory = Path(directory)
    summaries, runs = data["summary"], data["runs"]
    scenarios = list(dict.fromkeys(row["scenario"] for row in summaries))
    implementations = data["configuration"].get("implementations", ["Python", "Rust"])
    colors = {"Python": "#edb750", "Rust previous": "#91a6bd", "Rust": "#00d7b4"}
    background, panel, text, muted = "#121821", "#1b2431", "#edf3fa", "#b4c2d6"
    lookup = {(row["scenario"], row["implementation"]): row for row in summaries}
    labels = ["Imported profile\nplayback" if s == "playback_profile" else s.replace("_", "\n") for s in scenarios]
    configuration = data["configuration"]
    repeats, seconds, warmup = (configuration[k] for k in ("repeats", "seconds", "warmup"))
    interval = configuration["interval"]
    context = f"{repeats} fresh trials per state · {seconds:g}s sampled after {warmup:g}s warmup · all observed child processes included"
    scope = "Frozen Python UI/audio components vs Rust · muted real output · no hooks, network, account sync, library scan or game/GPU workload"
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11, "svg.fonttype": "none"})

    def create(rows, title, height=None):
        fig, axes = plt.subplots(rows, 1, figsize=(18, height or rows * 4.1 + 1.3), squeeze=False, layout="constrained")
        fig.patch.set_facecolor(background)
        fig.suptitle(title + "\n" + context, fontsize=22, color=text, ha="left", x=.04)
        for ax in axes[:, 0]:
            ax.set_facecolor(panel)
            ax.tick_params(colors=muted, labelsize=10)
            ax.grid(axis="y", color="#70849f", alpha=.18)
            ax.set_axisbelow(True)
            ax.spines[["top", "right"]].set_visible(False)
            ax.spines[["left", "bottom"]].set_color("#52647d")
            ax.yaxis.label.set_color(muted)
        fig.supxlabel(scope, fontsize=10, color=muted)
        return fig, axes[:, 0]

    def finish(fig, name, svg=True):
        for ax in fig.axes: ax.set_ylim(bottom=0)
        for extension in (["png", "svg"] if svg else ["png"]):
            fig.savefig(directory / f"{name}.{extension}", dpi=175, facecolor=background, bbox_inches="tight")
        plt.close(fig)

    def legend(ax, extra=None):
        handles = [Patch(facecolor=colors[i], label=i) for i in implementations]
        if extra: handles.extend(extra)
        result = ax.legend(handles=handles, facecolor=panel, edgecolor="#52647d", loc="upper right", fontsize=10)
        for label in result.get_texts(): label.set_color(text)

    def bars(ax, metric, title, ylabel, decimals=2, peaks=None):
        x = np.arange(len(scenarios))
        for series, implementation in enumerate(implementations):
            values = [lookup.get((s, implementation), {}).get(metric, float("nan")) for s in scenarios]
            deviations = [lookup.get((s, implementation), {}).get(metric + "_stddev", 0) for s in scenarios]
            width = .76 / len(implementations)
            positions = x + (series - (len(implementations)-1)/2) * width
            plotted = ax.bar(positions, values, width=width*.92, color=colors[implementation], alpha=.95,
                            yerr=None if peaks else deviations, capsize=3, error_kw={"ecolor": text, "linewidth": 1.2})
            for index, (position, value, bar) in enumerate(zip(positions, values, plotted)):
                if not math.isfinite(value): continue
                matches = [run for run in runs if run["scenario"] == scenarios[index] and run["implementation"] == implementation]
                samples = [run[metric] for run in matches if metric in run]
                if samples:
                    offsets = np.linspace(-.055, .055, len(samples))
                    ax.scatter(position + offsets, samples, s=18, color=background, edgecolors=text, linewidths=.6, zorder=5)
                annotation_height = value + deviations[index]
                if peaks:
                    high = max((run.get(peaks, run.get(metric, 0)) for run in matches), default=value)
                    ax.plot([position, position], [value, high], color=text, linewidth=1.2)
                    ax.scatter([position], [high], marker="_", s=100, color=text, linewidth=1.5, zorder=6)
                    annotation_height = high
                ax.annotate(f"{value:.{decimals}f}", (position, annotation_height), xytext=(0, 6), textcoords="offset points", ha="center", color=text, fontsize=9)
        ax.set_xticks(x, labels)
        ax.set_ylabel(ylabel, labelpad=12)
        ax.set_title(title, color=text, fontsize=15, loc="left", pad=15)
        ax.margins(y=.20)
        legend(ax)

    fig, axes = create(3, "MusicApp · measured CPU, memory and startup", 15)
    bars(axes[0], "cpu_one_core_percent", "Steady workload CPU · bars: repeat mean, whiskers: repeat SD, dots: each trial", "% of one logical processor")
    bars(axes[1], "private_mean_mib", "Private committed memory · source and decoder processes summed", "MiB", 1)
    bars(axes[2], "startup_seconds", "Launch to ready · includes imports, UI creation and audio preparation", "Seconds")
    finish(fig, "comparison")

    fig, axes = create(2, "Memory · steady use and observed peaks", 10.5)
    bars(axes[0], "private_mean_mib", "Private committed memory · bars: steady mean, thin lines: largest whole-trial peak", "MiB", 1, "trial_peak_private_mib")
    bars(axes[1], "rss_mean_mib", "Working set · shared pages can be counted in multiple processes", "MiB", 1, "trial_peak_rss_mib")
    finish(fig, "memory_detail")

    fig, axes = create(2, "Startup · readiness time and startup memory", 10.5)
    bars(axes[0], "startup_seconds", "Launch to readiness · dots show all repeats; warm filesystem cache", "Seconds")
    bars(axes[1], "startup_peak_private_mib", "Highest private memory observed from process launch to readiness", "MiB", 1)
    finish(fig, "startup_detail")

    fig, axes = create(2, "Variation · every measured sample", 11)
    for ax, metric, title, ylabel in zip(axes, ["cpu_one_core_percent", "private_mib"],
        [f"CPU distribution · {interval:g}s snapshots pooled across repeats; coarse clock increments can produce zero samples", "Private memory distribution · pooled snapshots, not startup allocations"],
        ["% of one logical processor", "MiB"]):
        for series, implementation in enumerate(implementations):
            observations = [[sample[metric] for run in runs if run["scenario"] == scenario and run["implementation"] == implementation for sample in run["samples"]] or [float("nan")] for scenario in scenarios]
            width = .76 / len(implementations)
            positions = np.arange(len(scenarios)) + (series - (len(implementations)-1)/2) * width
            ax.boxplot(observations, positions=positions, widths=width*.85, manage_ticks=False, patch_artist=True,
                       boxprops={"facecolor": colors[implementation], "edgecolor": colors[implementation], "alpha": .7},
                       medianprops={"color": text, "linewidth": 1.8}, whiskerprops={"color": colors[implementation]},
                       capprops={"color": colors[implementation]}, flierprops={"marker": ".", "markersize": 3, "markerfacecolor": colors[implementation], "markeredgecolor": colors[implementation], "alpha": .5})
        ax.set_xticks(range(len(scenarios)), labels)
        ax.set_title(title, color=text, fontsize=14, loc="left", pad=15)
        ax.set_ylabel(ylabel)
        legend(ax)
    finish(fig, "sample_distributions")

    fig, axes = create(len(scenarios), "CPU traces · all states, all repeats", len(scenarios) * 2.4 + 1.8)
    for scenario, ax in zip(scenarios, axes):
        for run in runs:
            if run["scenario"] != scenario: continue
            ax.plot([sample["elapsed_seconds"] for sample in run["samples"]], [sample["cpu_one_core_percent"] for sample in run["samples"]],
                    color=colors[run["implementation"]], linestyle=["-", "--", ":"][(run["repeat"]-1) % 3], alpha=.8, linewidth=1.3)
        ax.set_title(scenario.replace("_", " "), color=text, loc="left", fontsize=13)
        ax.set_ylabel("CPU % / core")
        legend(ax)
    axes[-1].set_xlabel("Seconds after warmup", color=muted)
    finish(fig, "cpu_timeseries", svg=False)

    state_directory = directory / "states"
    state_directory.mkdir(exist_ok=True)
    for scenario in scenarios:
        fig, axes = create(3, f"State detail · {scenario.replace('_', ' ')}", 11.5)
        for ax, metric, title, ylabel in zip(axes,
            ["cpu_one_core_percent", "private_mib", "rss_mib"],
            ["CPU · actual interval samples", "Private committed memory · app and observed descendants", "Working set · app and observed descendants"],
            ["% of one logical processor", "MiB", "MiB"]):
            for run in runs:
                if run["scenario"] != scenario: continue
                ax.plot([sample["elapsed_seconds"] for sample in run["samples"]], [sample[metric] for sample in run["samples"]],
                        color=colors[run["implementation"]], linestyle=["-", "--", ":"][(run["repeat"]-1)%3], alpha=.8, linewidth=1.5)
            ax.set_title(title, color=text, loc="left", pad=10)
            ax.set_ylabel(ylabel)
            legend(ax)
        axes[-1].set_xlabel("Seconds after warmup · solid=repeat 1, dashed=2, dotted=3", color=muted)
        for ax in axes: ax.set_ylim(bottom=0)
        fig.savefig(state_directory / f"{scenario}.png", dpi=160, facecolor=background, bbox_inches="tight")
        plt.close(fig)

    rows = []
    for row in summaries:
        selected = [run for run in runs if run["scenario"] == row["scenario"] and run["implementation"] == row["implementation"]]
        underflows = [run["audio_validation"]["underflows"] for run in selected if run.get("audio_validation")]
        cells = [row["scenario"], row["implementation"], f"{row['cpu_one_core_percent']:.3f}", f"{row['cpu_sample_p95']:.3f}",
                 f"{row['private_mean_mib']:.1f}", f"{max((run.get('trial_peak_private_mib', 0) for run in selected), default=0):.1f}",
                 f"{row['rss_mean_mib']:.1f}", f"{row['startup_seconds']:.3f}", str(underflows) if underflows else "—"]
        rows.append("<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in cells) + "</tr>")
    profile = configuration.get("profile_settings", {})
    profile_text = json.dumps({"preferences": profile.get("preferences", {}), "eq_gains": profile.get("eq_gains", [])}, indent=2)
    links = "".join(f'<li><a href="states/{scenario}.png">{html.escape(scenario.replace("_", " "))}</a></li>' for scenario in scenarios)
    limits = "".join(f"<li>{html.escape(limit)}</li>" for limit in data["limitations"])
    artifacts = data.get("artifacts", {})
    executable_hash = artifacts.get("rust_executable_sha256", "not recorded")
    body = f"""<!doctype html><html><head><meta charset="utf-8"><title>MusicApp detailed benchmark</title>
<style>body{{font:16px Segoe UI,Arial,sans-serif;color:#edf3fa;background:#121821;max-width:1500px;margin:30px auto;padding:24px}}p,li{{line-height:1.6;color:#b4c2d6}}a{{color:#00d7b4}}img{{width:100%;height:auto}}table{{border-collapse:collapse;font-size:13px;width:100%}}td,th{{padding:9px;text-align:left;border-bottom:1px solid #52647d}}pre{{overflow:auto;background:#1b2431;padding:18px}}code{{overflow-wrap:anywhere}}</style></head><body>
<h1>MusicApp · detailed measured benchmark</h1><p>{html.escape(context)}<br>{html.escape(scope)}</p>
<p>These figures measure the UI/audio component workload, rather than complete account, library or game behavior. CPU 100% means one occupied logical processor. Resource numbers include observed decoder children. Failed trials are listed in JSON and are never replaced with zero.</p>
<img src="comparison.png" alt="CPU, private memory and readiness overview"><h2>Memory and peaks</h2><img src="memory_detail.png" alt="Private memory and working-set peaks">
<h2>Startup</h2><img src="startup_detail.png" alt="Startup timing and startup private-memory peaks"><h2>Variation</h2><img src="sample_distributions.png" alt="CPU and memory sample distributions">
<h2>Measured table</h2><div style="overflow:auto"><table><tr><th>State</th><th>App</th><th>CPU mean %/core</th><th>CPU sample p95</th><th>Private mean MiB</th><th>Private peak MiB</th><th>Working set MiB</th><th>Startup seconds</th><th>Underflows per trial*</th></tr>{''.join(rows)}</table></div>
<p>*Underflow definitions differ between audio backends; these are diagnostics, not identical failure metrics. Individual profile/effects/clock validation is saved with every audio trial.</p>
<h2>Imported audio/UI profile</h2><p>Only whitelisted settings were cloned. Account state, cookies, personal track paths and history were never copied into benchmark folders. Controlled scenarios override effects; the imported-profile playback case retains the imported gains and gaming mode. Output stays muted.</p><pre>{html.escape(profile_text)}</pre>
<h2>Per-state CPU/RAM traces</h2><ul>{links}</ul><p><a href="cpu_timeseries.png">All CPU traces</a> · <a href="results.json">Complete raw JSON</a> · <a href="summary.csv">Summary CSV</a> · <a href="samples.csv">Every sample CSV</a></p>
<h2>Reproducibility</h2><p>Measured executable SHA-256:<br><code>{html.escape(executable_hash)}</code></p><p>UI asset hashes, benchmark-tool hashes, frozen-source hashes and sanitized profile hash are recorded in results.json.</p>
<h2>Scope and limitations</h2><ul>{limits}</ul></body></html>"""
    (directory / "report.html").write_text(body, encoding="utf-8")
