"""Windows FFmpeg subprocess benchmarks; stdlib orchestration, optional charts.

Use fixtures retained by verify_slim_ffmpeg.py --keep-fixtures. This measures
decoder launch/first PCM and unpaced decoding throughput, never live playback,
the Rust UI, games, accounts or network services. No binary is replaced.
"""
import argparse
import csv
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time

BASE = Path(__file__).resolve().parents[1]
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SHORT_CASES = {
    "WAV PCM16": "wav_pcm16_stereo48.wav",
    "MP3": "tagged café MP3.mp3",
    "FLAC": "flac24.flac",
    "AAC M4A": "m4a_aac.m4a",
    "Opus WebM": "webm_opus.webm",
    "Vorbis Ogg": "ogg_vorbis.ogg",
}
LONG_CODECS = {
    "WAV PCM16": ("wav", ["-c:a", "pcm_s16le"]),
    "MP3": ("mp3", ["-c:a", "libmp3lame", "-b:a", "192k"]),
    "FLAC": ("flac", ["-c:a", "flac"]),
    "AAC M4A": ("m4a", ["-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"]),
    "Opus WebM": ("webm", ["-c:a", "libopus", "-b:a", "96k"]),
}


class MemoryCounters(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t)]


def filetime_seconds(value):
    return ((value.dwHighDateTime << 32) | value.dwLowDateTime) / 10_000_000.


class NativeMetrics:
    def __init__(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        self.times = kernel.GetProcessTimes
        self.times.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        self.times.restype = wintypes.BOOL
        self.memory = psapi.GetProcessMemoryInfo
        self.memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
        self.memory.restype = wintypes.BOOL
        self.wait = kernel.WaitForSingleObject
        self.wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.wait.restype = wintypes.DWORD

    def cpu(self, handle):
        creation, exit_time, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not self.times(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel), ctypes.byref(user)):
            raise ctypes.WinError(ctypes.get_last_error())
        return {"cpu_kernel_seconds": filetime_seconds(kernel), "cpu_user_seconds": filetime_seconds(user),
                "cpu_seconds": filetime_seconds(kernel) + filetime_seconds(user)}

    def peak_memory(self, handle):
        counters = MemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not self.memory(handle, ctypes.byref(counters), counters.cb): return None
        return {"peak_working_set_mib": counters.PeakWorkingSetSize / 2**20,
                "peak_commit_mib": counters.PeakPagefileUsage / 2**20,
                "working_set_mib": counters.WorkingSetSize / 2**20,
                "private_commit_mib": counters.PrivateUsage / 2**20}


def decoder_arguments(path):
    return ["-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "1", "-filter_threads", "1", "-i", str(path),
            "-vn", "-sn", "-dn", "-map", "0:a:0", "-threads", "1", "-ac", "2", "-ar", "44100", "-f", "f32le", "pipe:1"]


def measure(binary, media, log, native, timeout, monitor_interval):
    stopped, timed_out = threading.Event(), threading.Event()
    peaks = {"peak_working_set_mib": 0., "peak_commit_mib": 0.}
    samples = [0]
    with log.open("wb") as stderr:
        started = time.perf_counter()
        process = subprocess.Popen([str(binary), *decoder_arguments(media)], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=stderr, creationflags=CREATE_NO_WINDOW, bufsize=0)
        # CPython's Windows Popen retains this kernel process handle after exit.
        # Keep an additional reference until final GetProcessTimes/MemoryInfo.
        retained_handle = process._handle
        handle = wintypes.HANDLE(int(retained_handle))
        def sample():
            memory = native.peak_memory(handle)
            if memory:
                for key in peaks: peaks[key] = max(peaks[key], memory[key])
                samples[0] += 1
        def monitor():
            while not stopped.is_set():
                sample()
                if native.wait(handle, 0) == 0: break
                if time.perf_counter() - started > timeout:
                    timed_out.set()
                    try: process.kill()
                    except OSError: pass
                    break
                stopped.wait(monitor_interval)
        worker = threading.Thread(target=monitor, name="ffmpeg-metrics", daemon=True)
        worker.start()
        try:
            first = process.stdout.read(8)
            first_at = time.perf_counter()
            byte_count = len(first)
            while True:
                block = process.stdout.read(65536)
                if not block: break
                byte_count += len(block)
            process.wait(timeout=5)
            ended = time.perf_counter()
            stopped.set(); worker.join(5)
            after_exit = native.peak_memory(handle)
            sample()
            cpu = native.cpu(handle)
            if timed_out.is_set(): raise RuntimeError(f"Decoder exceeded {timeout}s")
            if process.returncode: raise RuntimeError(f"Decoder exit {process.returncode}; see {log}")
            if len(first) != 8 or byte_count % 8: raise RuntimeError("Missing or incomplete stereo float PCM output")
            if peaks["peak_working_set_mib"] <= 0: raise RuntimeError("Could not obtain native peak process memory")
            return {"first_pcm_seconds": first_at-started, "end_to_end_seconds": ended-started,
                    **cpu, **peaks, "frames": byte_count//8, "decoded_audio_seconds": byte_count/(44100*8),
                    "output_bytes": byte_count, "memory_counter_samples": samples[0],
                    "post_exit_memory_api_succeeded": after_exit is not None,
                    "post_exit_peak_working_set_mib": after_exit["peak_working_set_mib"] if after_exit else None}
        finally:
            stopped.set()
            if process.poll() is None:
                process.kill(); process.wait(timeout=5)
            worker.join(5)
            process.stdout.close()


def command(binary, arguments):
    result = subprocess.run([str(binary), *map(str, arguments)], stdin=subprocess.DEVNULL, capture_output=True,
                            timeout=120, creationflags=CREATE_NO_WINDOW)
    if result.returncode: raise RuntimeError(result.stderr.decode("utf-8", "replace")[-2000:])
    return result


def summarize(runs):
    rows = []
    for phase in ("startup", "sustained"):
        for codec in dict.fromkeys(run["codec"] for run in runs if run["phase"] == phase):
            for implementation in ("Reference", "Candidate"):
                selected = [run for run in runs if run["phase"] == phase and run["codec"] == codec and run["implementation"] == implementation]
                if not selected: continue
                row = {"phase": phase, "codec": codec, "implementation": implementation, "repeats": len(selected)}
                for metric in ("first_pcm_seconds", "end_to_end_seconds", "cpu_seconds", "cpu_user_seconds", "cpu_kernel_seconds", "peak_working_set_mib", "peak_commit_mib"):
                    values = [run[metric] for run in selected]
                    row[metric + "_median"] = statistics.median(values)
                    row[metric + "_mean"] = statistics.mean(values)
                    row[metric + "_min"] = min(values)
                    row[metric + "_max"] = max(values)
                    row[metric + "_stddev"] = statistics.stdev(values) if len(values)>1 else 0.
                rows.append(row)
    return rows


def save(data, directory):
    data["summary"] = summarize(data["runs"])
    (directory / "benchmark.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    if data["runs"]:
        with (directory / "trials.csv").open("w", newline="", encoding="utf-8") as target:
            writer = csv.DictWriter(target, fieldnames=list(data["runs"][0])); writer.writeheader(); writer.writerows(data["runs"])
    if data["summary"]:
        with (directory / "summary.csv").open("w", newline="", encoding="utf-8") as target:
            writer = csv.DictWriter(target, fieldnames=list(data["summary"][0])); writer.writeheader(); writer.writerows(data["summary"])


def markdown(data, directory):
    lines = ["# Native FFmpeg measurements", "", "These are decoder subprocess measurements, not live playback, Rust UI, game or account benchmarks.",
             "Both executables receive the app's same single-threaded stereo 44.1 kHz float PCM decoder arguments. Output is drained in bounded blocks; all output remains local.", "",
             "First PCM includes process creation, native initialization, probing and the first stdout flush. It does not mean first audible sound. Sustained measurements decode synthetic audio as fast as possible, with no real-time pacing.", "",
             "Windows GetProcessTimes records actual child user/kernel CPU through a retained process handle. Peak working set and peak committed-memory counters come from GetProcessMemoryInfo, observed during execution and queried after exit where supported.", "",
             "Trials alternate executable order. File-system caches are warm, and each case receives equal untimed warmups. Medians, ranges and individual trials are retained; near-zero timings or small differences should not be treated as certain speedups.", "",
             "| Phase / codec | Executable | First PCM median ms | End-to-end median ms | Actual CPU median ms | Peak working set median MiB |", "|---|---|---:|---:|---:|---:|"]
    for row in data["summary"]:
        lines.append(f"| {row['phase']} / {row['codec']} | {row['implementation']} | {row['first_pcm_seconds_median']*1000:.3f} | {row['end_to_end_seconds_median']*1000:.3f} | {row['cpu_seconds_median']*1000:.3f} | {row['peak_working_set_mib_median']:.2f} |")
    lines += ["", f"Reference executable: {data['executables']['Reference']['size_bytes']:,} bytes; candidate: {data['executables']['Candidate']['size_bytes']:,} bytes.",
              "", "Executable hashes, exact fixtures/hashes, configuration, raw trials and errors are in benchmark.json. This test does not establish compatibility; run verify_slim_ffmpeg.py separately."]
    if data["errors"]: lines += ["", f"**{len(data['errors'])} trial(s) failed; do not treat missing measurements as zero.**"]
    (directory / "README.md").write_text("\n".join(lines)+"\n", encoding="utf-8")


def charts(data, directory):
    if importlib.util.find_spec("matplotlib") is None: return "matplotlib unavailable; JSON/CSV/Markdown were produced without installing anything"
    os.environ.setdefault("MPLCONFIGDIR", str(directory / "matplotlib-cache"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    colors = {"Reference": "#e9b44c", "Candidate": "#00d8b3"}
    for phase, metrics, title in (
        ("startup", [("first_pcm_seconds", 1000, "Launch to first PCM · milliseconds"), ("peak_working_set_mib", 1, "Peak working set · MiB")], "Native FFmpeg · short-file startup"),
        ("sustained", [("cpu_seconds", 1, "Actual child CPU · seconds"), ("end_to_end_seconds", 1, "Unpaced decode and pipe drain · seconds"), ("peak_working_set_mib", 1, "Peak working set · MiB")], "Native FFmpeg · sustained offline decoding")):
        codecs = list(dict.fromkeys(row["codec"] for row in data["summary"] if row["phase"] == phase))
        if not codecs: continue
        figure, axes = plt.subplots(len(metrics), 1, figsize=(14, 4.2*len(metrics)+1), layout="constrained", squeeze=False)
        figure.patch.set_facecolor("#141820")
        figure.suptitle(title + "\nMedian bars · min/max whiskers · every trial shown · lower is better", color="#e6edf7", fontsize=20)
        for ax, (metric, scale, label) in zip(axes[:,0], metrics):
            ax.set_facecolor("#1d2330")
            for series, implementation in enumerate(("Reference", "Candidate")):
                rows = [next((row for row in data["summary"] if row["phase"]==phase and row["codec"]==codec and row["implementation"]==implementation), None) for codec in codecs]
                values = [row[metric+"_median"]*scale if row else float("nan") for row in rows]
                low = [(row[metric+"_median"]-row[metric+"_min"])*scale if row else 0 for row in rows]
                high = [(row[metric+"_max"]-row[metric+"_median"])*scale if row else 0 for row in rows]
                positions = [index + (series-.5)*.36 for index in range(len(codecs))]
                ax.bar(positions, values, .33, color=colors[implementation], label=implementation, yerr=[low,high], capsize=3, error_kw={"ecolor":"#d2dae7"})
                for index, (x,value) in enumerate(zip(positions,values)):
                    trials = [run[metric]*scale for run in data["runs"] if run["phase"]==phase and run["codec"]==codecs[index] and run["implementation"]==implementation]
                    ax.scatter([x+(i-(len(trials)-1)/2)*.012 for i in range(len(trials))], trials, color="#141820", edgecolors="#d2dae7", s=14, linewidths=.5, zorder=5)
                    if math.isfinite(value): ax.annotate(f"{value:.2f}", (x,value+high[index]), xytext=(0,6),textcoords="offset points",ha="center",color="#d2dae7",fontsize=9)
            ax.set_xticks(range(len(codecs)),codecs)
            ax.tick_params(colors="#b8c5d8")
            ax.set_ylabel(label,color="#b8c5d8")
            ax.grid(axis="y",alpha=.2);ax.set_axisbelow(True);ax.set_ylim(bottom=0);ax.margins(y=.2)
            ax.spines[["top","right"]].set_visible(False)
            legend=ax.legend(facecolor="#1d2330",edgecolor="#536176")
            for text in legend.get_texts():text.set_color("#d2dae7")
        figure.supxlabel("Local synthetic fixtures · exact app decoder arguments · warm filesystem cache · no playback device or real-time pacing",color="#b8c5d8",fontsize=10)
        figure.savefig(directory/f"ffmpeg_{phase}.png",dpi=170,facecolor=figure.get_facecolor(),bbox_inches="tight")
        plt.close(figure)
    return "PNG charts produced using an already installed optional matplotlib"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference",type=Path,required=True)
    parser.add_argument("--candidate",type=Path,required=True)
    parser.add_argument("--fixture-generator",type=Path,help="Full FFmpeg used only to generate inputs; defaults to reference")
    parser.add_argument("--fixtures",type=Path,required=True,help="verify_slim_ffmpeg.py fixtures retained with --keep-fixtures")
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--startup-repeats",type=int,default=7)
    parser.add_argument("--sustained-repeats",type=int,default=5)
    parser.add_argument("--sustained-seconds",type=float,default=120)
    parser.add_argument("--warmups",type=int,default=1)
    parser.add_argument("--timeout",type=float,default=60)
    parser.add_argument("--monitor-interval",type=float,default=.002)
    parser.add_argument("--skip-charts",action="store_true")
    parser.add_argument("--keep-generated",action="store_true")
    args=parser.parse_args()
    if os.name!="nt":parser.error("Windows is required for actual process CPU/peak-memory counters")
    if min(args.startup_repeats,args.sustained_repeats)<1 or args.warmups<0 or args.sustained_seconds<=0 or args.timeout<=0 or args.monitor_interval<=0:parser.error("Invalid duration or repeat count")
    args.reference,args.candidate,args.fixtures,args.output=(path.resolve() for path in (args.reference,args.candidate,args.fixtures,args.output))
    generator=args.fixture_generator.resolve() if args.fixture_generator else args.reference
    if not args.output.is_relative_to(BASE):parser.error("Output must remain inside rust_musicapp")
    for binary in (args.reference,args.candidate,generator):
        if not binary.is_file():parser.error(f"Executable missing: {binary}")
    for filename in SHORT_CASES.values():
        if not(args.fixtures/filename).is_file():parser.error(f"Missing compatibility fixture: {filename}; rerun compatibility with --keep-fixtures")
    args.output.mkdir(parents=True)
    logs,generated=args.output/"logs",args.output/"generated"
    logs.mkdir();generated.mkdir()
    binaries={"Reference":args.reference,"Candidate":args.candidate}
    native=NativeMetrics()
    data={"schema_version":1,"created_utc":datetime.now(timezone.utc).isoformat(),"configuration":{key:getattr(args,key) for key in ("startup_repeats","sustained_repeats","sustained_seconds","warmups","timeout","monitor_interval")},
          "scope":"Exact app audio decoder subprocess; unpaced synthetic local input; no live playback/UI/game/account/network measurement.",
          "metrics":"Windows GetProcessTimes CPU and GetProcessMemoryInfo peaks through retained process handle; stdout drained in <=64KiB blocks.",
          "tool_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),"executables":{},"fixtures":[],"runs":[],"errors":[]}
    for implementation,binary in binaries.items():
        version=command(binary,["-version"])
        data["executables"][implementation]={"path":str(binary),"size_bytes":binary.stat().st_size,"sha256":hashlib.sha256(binary.read_bytes()).hexdigest(),"version":version.stdout.decode("utf-8","replace")}
    data["fixture_generator"]={"path":str(generator),"sha256":hashlib.sha256(generator.read_bytes()).hexdigest(),"timed":False}
    short={codec:args.fixtures/filename for codec,filename in SHORT_CASES.items()}
    sustained={}
    try:
        # Generate outside every timed interval, exclusively with the reference.
        for codec,(extension,options) in LONG_CODECS.items():
            path=generated/(codec.replace(" ","_")+"."+extension)
            command(generator,["-nostdin","-hide_banner","-loglevel","error","-y","-threads","1","-filter_threads","1","-stream_loop","-1","-i",short["WAV PCM16"],"-t",str(args.sustained_seconds),"-vn",*options,path])
            sustained[codec]=path
        for phase,cases,repeats in (("startup",short,args.startup_repeats),("sustained",sustained,args.sustained_repeats)):
            expected_frames={}
            for codec,path in cases.items():
                data["fixtures"].append({"phase":phase,"codec":codec,"filename":path.name,"sha256":hashlib.sha256(path.read_bytes()).hexdigest(),"size_bytes":path.stat().st_size})
                for implementation,binary in binaries.items():
                    for warmup in range(args.warmups):
                        measure(binary,path,logs/f"warmup-{phase}-{codec.replace(' ','_')}-{implementation}-{warmup}.log",native,args.timeout,args.monitor_interval)
            for repeat in range(1,repeats+1):
                order=("Reference","Candidate") if repeat%2 else ("Candidate","Reference")
                case_order=list(cases.items()) if repeat%2 else list(reversed(list(cases.items())))
                for codec,path in case_order:
                    for implementation in order:
                        print(f"{phase} / {codec} / {implementation} / {repeat}",flush=True)
                        try:
                            result=measure(binaries[implementation],path,logs/f"{phase}-{codec.replace(' ','_')}-{implementation}-{repeat}.log",native,args.timeout,args.monitor_interval)
                            if codec not in expected_frames:expected_frames[codec]=result["frames"]
                            if abs(result["frames"]-expected_frames[codec])>2:raise RuntimeError("Output frame counts differ; a faster incomplete decode is not accepted")
                            data["runs"].append({"phase":phase,"codec":codec,"implementation":implementation,"repeat":repeat,**result})
                            print(f"  first {result['first_pcm_seconds']*1000:.3f}ms / total {result['end_to_end_seconds']*1000:.3f}ms / CPU {result['cpu_seconds']*1000:.3f}ms / peak {result['peak_working_set_mib']:.2f}MiB",flush=True)
                        except Exception as error:
                            data["errors"].append({"phase":phase,"codec":codec,"implementation":implementation,"repeat":repeat,"error":str(error)})
                            print("  FAILED",str(error),flush=True)
                        save(data,args.output)
    except Exception as error:
        data["errors"].append({"phase":"setup","error":str(error)})
    finally:
        save(data,args.output)
        if not args.skip_charts:
            try:data["charts"]=charts(data,args.output)
            except Exception as error:data["charts"]="Chart generation failed: "+str(error)
        markdown(data,args.output);save(data,args.output)
        if not args.keep_generated:
            for path in sustained.values():path.unlink(missing_ok=True)
    print(args.output/"README.md",flush=True)
    raise SystemExit(1 if data["errors"] else 0)


if __name__=="__main__":main()
