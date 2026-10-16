# Reproducible process benchmarks

The port executable and its build have no Python dependency. The Python scripts in this directory are optional comparison tools for the preserved Python application, as explicitly permitted for benchmarking. A Windows/.NET-only runner is also available:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File benchmarks/Run-Benchmarks.ps1
```

That runner measures Rust by default and creates standalone `report.html`, `comparison.svg`, JSON, and CSV files. It can accept a separately supplied baseline executable implementing the same benchmark protocol; it never invents missing baseline values. It needs no plotting package. Both runners validate actual playback clock/duration/error state and retain audio underflow counts, so failed playback cannot become a claimed performance improvement. Rust counts buffer-starvation callbacks; Python counts output underflows reported by sounddevice. These diagnostic counters have different definitions and should not be treated as identical metrics.

Run from `rust_musicapp` after building the release executable:

```powershell
python -B benchmarks/run_benchmarks.py
```

To compare the imported personal audio/UI settings as an additional eleventh
state, measure the packaged executable directly:

```powershell
python -B benchmarks/run_benchmarks.py --rust-exe dist/MusicApp/MusicApp.exe --profile-dir dist/MusicApp/state
```

This is 66 trials. Only whitelisted preferences, canonical EQ values, and
shortcuts are copied into isolated trial directories. Cookies, account caches,
personal media paths, and history are excluded. Controlled states override
effects; `playback_profile` retains the imported gains and gaming mode. The
runner verifies effective EQ, echo, gaming mode, muted output, and playback clock
against the prescribed workload before accepting each audio measurement.

An optional `--previous-rust-exe <preserved-executable>` adds the earlier Rust
release as a third implementation. Three repeats of all eleven states then
produce 99 trials. Implementation order rotates between repeats, so each runs
first once. The preserved executable needs its own original `ui` folder and
native resources; its binary/UI hashes are recorded separately. The new Rust
release also records cumulative startup-stage timings for diagnosis.

The detailed `report.html` includes overview, memory peaks, startup, sample
distributions, per-state traces, and raw CSV/JSON. Repeat dots and standard
deviation whiskers show variation; executable, UI, tool, and sanitized-profile
hashes identify what was measured.

This optional tool needs `psutil`, `matplotlib`, and `numpy` in the Python environment. It does not install packages or alter global settings. The default run takes roughly 12–15 minutes. A quick integration check is:

```powershell
python -B benchmarks/run_benchmarks.py --repeats 1 --seconds 3 --warmup 1 --scenarios idle_visible playback_bypass progress
```

Each implementation gets three fresh processes per state, two seconds of warmup, and eight seconds of measurement sampled every 250 ms. Trial order alternates to reduce order effects. Startup includes imports, UI creation, and playback preparation up to the readiness marker; startup is measured with a warm filesystem cache. A generated stereo PCM track keeps input identical. Playback uses the actual default sound device at volume zero.

The state directory, temporary media, logs, and results stay under this project's benchmark directory. Every trial uses fresh app settings. The runner tracks the app and all observed descendants, including the Rust decoder's FFmpeg process. CPU is reported both as percentage of one logical processor and of the whole machine. It records private committed memory, working set, threads, process count, time series, and launch-to-ready time. A shared-page working-set sum may double count pages; private memory is provided alongside it.

The Python harness uses the unchanged snapshot's real Tk overlay and PyAV/sounddevice audio engine. Its player stub supplies display state without starting the full Python music controller, library scanner, account manager, recommendations, keyboard hooks, or networking. This comparison therefore describes the displayed UI/audio components; it must not be presented as a full-application, YouTube, download, game, or GPU benchmark. Near-idle CPU differences can be scheduler noise. Short-lived processes that exit between snapshots can have unobserved CPU time.

States:

| State | Workload |
|---|---|
| `idle_visible` / `idle_hidden` | Idle overlay with no audio device opened |
| `paused_visible` / `paused_hidden` | Open decoder/device, paused playback with bounded read-ahead |
| `playback_bypass` | Default gaming mode, EQ/echo bypassed |
| `playback_eq` | Ten active frequency gains, FFT overlap-add |
| `playback_echo` | Flat EQ, 350 ms echo, 0.35 feedback, 0.5 wet |
| `controls` | Controls card open |
| `search` | Search card open, no network request |
| `progress` | Three synthetic download progress rows updated at 10 Hz |

Results include the Rust executable SHA-256 and frozen Python source hashes, configuration, per-trial samples and summary statistics. `comparison.png` and `comparison.svg` show raw values with repeat standard deviation. `cpu_timeseries.png` exposes momentary CPU spikes. CSV and JSON preserve the measurements for independent analysis. The generated WAV is removed after completion and can be reproduced by the runner.

If a state fails, its failure and log path are retained; failed trials are not converted to zero usage. The runner returns a failure status when any trial fails. Do not compare an older chart with a newer executable without recording that distinction.

## Audio-only FFmpeg comparison

`verify_slim_ffmpeg.py` compares original/full and candidate native FFmpeg with
the app's exact PCM decoder and MP3/SponsorBlock conversion arguments. Its local
HTTP/HTTPS fixtures exercise byte-range seeking without sign-in or trust-store
changes. Retain fixtures with `--keep-fixtures` for `benchmark_ffmpeg.py`, which
records first PCM, end-to-end offline decode time, Windows process CPU and peak
working set/commit. Neither development script ships in the app.

`Run-FFmpegValidation.ps1 -Compiler clang -ReportPrefix ffmpeg-audio-final` waits
for the native build, runs compatibility and then the alternating benchmark.
It does not install the candidate. `Deploy-SlimFFmpeg.ps1` checks the measured
binary hashes before copying it into the Rust app and exercising its real audio
path in five isolated, muted smoke runs. Original Python sources/settings and
the Rust executable/UI are verified against their saved hashes.

Results: `results/ffmpeg-audio-final-compat`, `results/ffmpeg-audio-final-performance`,
`results/ffmpeg-audio-final-smoke`, and `results/ffmpeg-audio-long-performance`.
The long run decodes 30-minute fixtures to reduce CPU-timer rounding effects.
These remain native decoder component measurements; they do not replace full
app/game/account acceptance or prove all possible codec/container combinations.
