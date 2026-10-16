"""Repeatable local process-tree benchmarks and charts; never touches live settings.

Examples:
  python -B benchmarks/run_benchmarks.py
  python -B benchmarks/run_benchmarks.py --scenarios idle_hidden playback_eq --repeats 1 --seconds 4
  python -B benchmarks/run_benchmarks.py --render results/2026.../results.json

Dependencies for this optional benchmark tool: psutil, matplotlib, numpy.
CPU 100% means one fully busy logical processor. Memory includes child processes.
"""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
import wave

import psutil

BASE = Path(__file__).resolve().parents[1]
from profile_settings import (CONTROLLED_SCENARIOS, SCENARIOS, ISO_BANDS, CONTROLLED_GAINS, sanitized_profile, prepare_trial_profile, validate_audio)


def percentile(values, fraction):
    ordered = sorted(values)
    if not ordered: return 0.0
    index = (len(ordered) - 1) * fraction
    lo, hi = math.floor(index), math.ceil(index)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo)


def make_media(path, seconds):
    import numpy as np
    # Reproducible PCM, with tone movement so EQ/echo process real nonzero data.
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        for second in range(math.ceil(seconds)):
            t = (np.arange(44100) + second * 44100) / 44100
            left = np.sin(2 * math.pi * 440 * t) * 0.18 + np.sin(2 * math.pi * 110 * t) * 0.06
            right = np.sin(2 * math.pi * 660 * t) * 0.18 + np.sin(2 * math.pi * 220 * t) * 0.06
            pcm = np.column_stack([left, right])
            handle.writeframes(np.round(pcm * 32767).astype("<i2").tobytes())


class ProcessTree:
    def __init__(self, process):
        self.root = psutil.Process(process.pid)
        self.seen = {}
        self.cpu_last = {}
        self.cpu_seconds = 0.0
        self.peak_rss = self.peak_private = 0

    def snapshot(self):
        try: live = [self.root] + self.root.children(recursive=True)
        except psutil.Error: live = list(self.seen.values())
        rss = private = threads = 0
        alive = 0
        for process in live:
            try:
                identity = (process.pid, process.create_time())
                self.seen[identity] = process
                cpu = process.cpu_times()
                total = cpu.user + cpu.system
                previous = self.cpu_last.get(identity, 0.0)
                self.cpu_seconds += max(0.0, total - previous)
                self.cpu_last[identity] = total
                memory = process.memory_info()
                rss += memory.rss
                private += getattr(memory, "private", memory.rss)
                threads += process.num_threads()
                alive += 1
            except psutil.Error:
                pass
        self.peak_rss = max(self.peak_rss, rss)
        self.peak_private = max(self.peak_private, private)
        return {"cpu_seconds": self.cpu_seconds, "rss_mib": rss / 2**20, "private_mib": private / 2**20, "threads": threads, "processes": alive}


def stop_tree(process, tree=None):
    # Keep observed descendants even if their parent exited before cleanup.
    children = [p for p in (tree.seen.values() if tree else []) if p.pid != process.pid]
    try: children += psutil.Process(process.pid).children(recursive=True)
    except psutil.Error: pass
    unique = {}
    for child in children:
        try:
            if child.is_running(): unique[(child.pid, child.create_time())] = child
        except psutil.Error: pass
    children = list(unique.values())
    for child in children:
        try: child.terminate()
        except psutil.Error: pass
    if process.poll() is None: process.terminate()
    try: process.wait(timeout=3)
    except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=3)
    for child in children:
        try: child.wait(timeout=1)
        except (psutil.Error, psutil.TimeoutExpired):
            try: child.kill()
            except psutil.Error: pass


def run_one(implementation, scenario, repeat, directory, args, media):
    state = directory / "work" / f"{scenario}-{repeat}-{implementation}"
    state.mkdir(parents=True)
    expected = prepare_trial_profile(state, scenario, args.profile)
    runtime = args.seconds + args.warmup + 2
    command = ([str(args.python), "-B", str(BASE / "benchmarks" / "reference_harness.py")] if implementation == "Python"
               else [str(args.previous_rust_exe if implementation == "Rust previous" else args.rust_exe)])
    command += ["--benchmark", scenario, "--seconds", str(runtime), "--media", str(media), "--state-dir", str(state), "--no-hooks", "--no-network", "--mute"]
    if implementation == "Rust": command.append("--startup-profile")
    env = os.environ.copy()
    env.update({name: "1" for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "BLIS_NUM_THREADS")})
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    log_path = state / "process.log"
    started = time.perf_counter()
    samples = []
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=BASE, env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        tree = ProcessTree(process)
        try:
            while not (state / "ready").exists():
                tree.snapshot()
                if process.poll() is not None:
                    raise RuntimeError(f"Exited before readiness ({process.returncode}); see {log_path}")
                if time.perf_counter() - started > args.startup_timeout:
                    raise RuntimeError(f"Readiness timed out; see {log_path}")
                time.sleep(0.02)
            startup = time.perf_counter() - started
            tree.snapshot()
            startup_peak_rss, startup_peak_private = tree.peak_rss / 2**20, tree.peak_private / 2**20
            warmup_end = time.perf_counter() + args.warmup
            while time.perf_counter() < warmup_end:
                if process.poll() is not None: raise RuntimeError(f"Exited during warmup; see {log_path}")
                tree.snapshot()
                time.sleep(min(args.interval, max(0, warmup_end - time.perf_counter())))
            previous = tree.snapshot()
            previous_at = sample_started = time.perf_counter()
            initial_cpu = previous["cpu_seconds"]
            while time.perf_counter() - sample_started < args.seconds:
                time.sleep(min(args.interval, max(0, args.seconds - (time.perf_counter() - sample_started))))
                if process.poll() is not None: raise RuntimeError(f"Exited during measurement; see {log_path}")
                current = tree.snapshot()
                now = time.perf_counter()
                current["elapsed_seconds"] = now - sample_started
                current["cpu_one_core_percent"] = (current["cpu_seconds"] - previous["cpu_seconds"]) / (now - previous_at) * 100
                current["cpu_machine_percent"] = current["cpu_one_core_percent"] / (psutil.cpu_count() or 1)
                current["system_cpu_percent"] = psutil.cpu_percent(interval=None)
                samples.append(current)
                previous, previous_at = current, now
            elapsed = previous_at - sample_started
            # Allow the application's timer to shut it down and clean its children.
            try: process.wait(timeout=4)
            except subprocess.TimeoutExpired: stop_tree(process, tree)
            if process.returncode not in (0, None):
                raise RuntimeError(f"Exited after measurement with {process.returncode}; see {log_path}")
            diagnostic = None
            if scenario.startswith(("playback_", "paused_")):
                diagnostic_path = state / "benchmark_audio.json"
                if not diagnostic_path.is_file():
                    raise RuntimeError(f"Missing audio validation; refusing an unverified playback measurement: {diagnostic_path}")
                diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8-sig"))
                validate_audio(diagnostic, expected, scenario, args.seconds)
            return {"implementation": implementation, "scenario": scenario, "repeat": repeat, "startup_seconds": startup,
                    "sample_seconds": elapsed, "cpu_one_core_percent": (previous["cpu_seconds"] - initial_cpu) / elapsed * 100,
                    "cpu_machine_percent": (previous["cpu_seconds"] - initial_cpu) / elapsed * 100 / (psutil.cpu_count() or 1),
                    "cpu_sample_p95": percentile([s["cpu_one_core_percent"] for s in samples], .95),
                    "rss_mean_mib": statistics.mean(s["rss_mib"] for s in samples), "rss_peak_mib": max(s["rss_mib"] for s in samples),
                    "private_mean_mib": statistics.mean(s["private_mib"] for s in samples), "private_peak_mib": max(s["private_mib"] for s in samples),
                    "startup_peak_rss_mib": startup_peak_rss, "startup_peak_private_mib": startup_peak_private,
                    "trial_peak_rss_mib": tree.peak_rss / 2**20, "trial_peak_private_mib": tree.peak_private / 2**20,
                    "threads_mean": statistics.mean(s["threads"] for s in samples), "processes_max": max(s["processes"] for s in samples),
                    "expected_audio": expected, "audio_validation": diagnostic,
                    "startup_profile": json.loads((state / "startup-profile.json").read_text(encoding="utf-8-sig")) if (state / "startup-profile.json").exists() else None,
                    "samples": samples, "command": command, "log": str(log_path.relative_to(directory))}
        finally:
            stop_tree(process, tree)


def summarize(runs):
    output = []
    for scenario in dict.fromkeys(r["scenario"] for r in runs):
        for implementation in dict.fromkeys(run["implementation"] for run in runs):
            selected = [r for r in runs if r["scenario"] == scenario and r["implementation"] == implementation]
            if not selected: continue
            item = {"scenario": scenario, "implementation": implementation, "repeats": len(selected)}
            for metric in ("startup_seconds", "cpu_one_core_percent", "cpu_machine_percent", "cpu_sample_p95", "rss_mean_mib", "rss_peak_mib", "private_mean_mib", "private_peak_mib", "startup_peak_rss_mib", "startup_peak_private_mib", "trial_peak_rss_mib", "trial_peak_private_mib", "threads_mean", "processes_max"):
                values = [r[metric] for r in selected]
                item[metric] = statistics.mean(values)
                item[metric + "_stddev"] = statistics.stdev(values) if len(values) > 1 else 0
            output.append(item)
    return output


def render(data, directory):
    from charts import render_report
    render_report(data, directory)


def write_results(data, directory):
    data["summary"] = summarize(data["runs"])
    (directory / "results.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    if data["summary"]:
        with (directory / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(data["summary"][0]))
            writer.writeheader(); writer.writerows(data["summary"])
    with (directory / "samples.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = ["implementation", "scenario", "repeat", "elapsed_seconds", "cpu_one_core_percent", "cpu_machine_percent", "system_cpu_percent", "rss_mib", "private_mib", "threads", "processes", "cpu_seconds"]
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
        for run in data["runs"]:
            for sample in run["samples"]:
                writer.writerow({"implementation": run["implementation"], "scenario": run["scenario"], "repeat": run["repeat"], **sample})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rust-exe", type=Path, default=BASE / "target" / "release" / "rust_musicapp.exe")
    parser.add_argument("--previous-rust-exe", type=Path, help="Optional preserved earlier Rust release for a three-way comparison")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seconds", type=float, default=8)
    parser.add_argument("--warmup", type=float, default=2)
    parser.add_argument("--interval", type=float, default=.25)
    parser.add_argument("--startup-timeout", type=float, default=30)
    parser.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=CONTROLLED_SCENARIOS)
    parser.add_argument("--profile-dir", type=Path, help="Imported Rust state directory; clone only sanitized settings, never cookies/account state")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--render", type=Path)
    parser.add_argument("--skip-charts", action="store_true")
    args = parser.parse_args()
    if args.render:
        data = json.loads(args.render.read_text(encoding="utf-8")); render(data, args.render.resolve().parent); return
    if args.repeats < 1 or args.seconds <= 0 or args.warmup < 0 or args.interval <= 0: parser.error("Invalid measurement duration or repeat count")
    args.rust_exe = args.rust_exe.resolve(); args.python = args.python.resolve()
    if args.previous_rust_exe:
        args.previous_rust_exe = args.previous_rust_exe.resolve()
        if not args.previous_rust_exe.is_file(): parser.error("Previous Rust executable is missing")
    implementations = ["Python", "Rust previous", "Rust"] if args.previous_rust_exe else ["Python", "Rust"]
    if args.profile_dir:
        args.profile_dir = args.profile_dir.resolve()
        if "playback_profile" not in args.scenarios: args.scenarios = list(args.scenarios) + ["playback_profile"]
    elif "playback_profile" in args.scenarios:
        parser.error("playback_profile requires --profile-dir pointing to imported Rust state")
    try: args.profile = sanitized_profile(args.profile_dir)
    except (OSError, ValueError, TypeError) as error: parser.error(str(error))
    if not args.rust_exe.is_file(): parser.error(f"Build Rust release first; executable missing: {args.rust_exe}")
    directory = (args.output or BASE / "benchmarks" / "results" / datetime.now().strftime("%Y%m%d-%H%M%S")).resolve()
    directory.mkdir(parents=True)
    media = directory / "benchmark.wav"
    make_media(media, max(20, args.seconds + args.warmup + 5))
    profile_json = json.dumps(args.profile, sort_keys=True, separators=(",", ":"))
    (directory / "profile_manifest.json").write_text(profile_json, encoding="utf-8")
    app_root = next((parent for parent in args.rust_exe.parents if (parent / "ui" / "app.tcl").is_file()), BASE)
    ui_hashes = {str(path.relative_to(app_root)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in sorted((app_root / "ui").rglob("*")) if path.is_file()}
    data = {"schema_version": 3, "created_utc": datetime.now(timezone.utc).isoformat(),
        "configuration": {"repeats": args.repeats, "seconds": args.seconds, "warmup": args.warmup, "interval": args.interval, "scenarios": args.scenarios,
                          "profile_supplied": bool(args.profile_dir), "profile_settings": args.profile, "implementations": implementations,
                          "profile_scope": "Sanitized audio/UI settings only; deterministic media, enabled player, no account/cookies/history/personal paths. Controlled10states override effects; playback_profile retains imported gains/gaming. Volume muted."},
        "machine": {"platform": platform.platform(), "processor": platform.processor(), "logical_processors": psutil.cpu_count(), "physical_cores": psutil.cpu_count(logical=False), "ram_gib": psutil.virtual_memory().total / 2**30, "python": sys.version},
        "artifacts": {"rust_executable": str(args.rust_exe), "rust_executable_sha256": hashlib.sha256(args.rust_exe.read_bytes()).hexdigest(), "native_ui_sha256": ui_hashes,
                      "profile_manifest_sha256": hashlib.sha256(profile_json.encode()).hexdigest(),
                      "benchmark_tool_sha256": {name: hashlib.sha256((BASE / "benchmarks" / name).read_bytes()).hexdigest() for name in ("run_benchmarks.py", "reference_harness.py", "profile_settings.py", "charts.py")},
                      "python_source_manifest": json.loads((BASE / "legacy_reference" / "reference" / "source_hashes.json").read_text(encoding="utf-8"))},
        "limitations": ["Frozen Python UI/audio component workload excludes controller, library scan, accounts and recommendations.", "Rust output includes any actual child FFmpeg processes; Python PyAV decodes in process.", "Global hooks, network, game/fullscreen behavior, GPU cost, downloads and account sync are not measured.", "100% CPU is one logical core; near-idle CPU differences may be scheduling noise.", "Working-set sums can double count shared pages; private committed memory is also recorded.", "Short-lived child CPU before sampling may be missed; interval snapshots record persistent children.", "This is warm filesystem-cache startup; trials alternate implementation order. No cold-boot claims.", "Underflow counts are diagnostic: Rust counts buffer-starvation callbacks, Python counts sounddevice-reported output underflows; their definitions differ."],
        "runs": [], "errors": []}
    if args.previous_rust_exe:
        previous_root = next(parent for parent in args.previous_rust_exe.parents if (parent / "ui" / "app.tcl").is_file())
        data["artifacts"]["previous_rust"] = {"executable":str(args.previous_rust_exe), "sha256":hashlib.sha256(args.previous_rust_exe.read_bytes()).hexdigest(),
            "native_ui_sha256":{str(path.relative_to(previous_root)).replace("\\", "/"):hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted((previous_root/"ui").rglob("*")) if path.is_file()}}
    total = len(args.scenarios) * args.repeats * len(implementations)
    completed = 0
    for repeat in range(1, args.repeats + 1):
        for scenario in args.scenarios:
            offset = (repeat - 1) % len(implementations)
            order = implementations[offset:] + implementations[:offset]
            for implementation in order:
                completed += 1
                print(f"[{completed}/{total}] {scenario} / {implementation} / repeat {repeat}", flush=True)
                try:
                    result = run_one(implementation, scenario, repeat, directory, args, media)
                    data["runs"].append(result)
                    print(f"  {result['cpu_one_core_percent']:.3f}% core, {result['private_mean_mib']:.1f} MiB private, startup {result['startup_seconds']:.3f}s", flush=True)
                except Exception as error:
                    data["errors"].append({"scenario": scenario, "implementation": implementation, "repeat": repeat, "error": str(error)})
                    print(f"  FAILED: {error}", flush=True)
                write_results(data, directory)
    if not args.skip_charts and data["runs"]: render(data, directory)
    # The reproducible fixture can be regenerated; keep footprint low after tests.
    media.unlink(missing_ok=True)
    print(f"Results: {directory}", flush=True)
    if data["errors"]: raise SystemExit(1)


if __name__ == "__main__":
    main()
