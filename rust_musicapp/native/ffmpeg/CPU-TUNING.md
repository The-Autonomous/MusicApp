# CPU tuning experiment

The pre-tuning slim release is preserved at
`reference/ffmpeg-slim-before-cpu-tuning.exe`. Its baseline is the actual current
Rust helper, rather than the much larger historical full FFmpeg. The original
Python application, Rust application binary, UI and personal settings are not
changed by these build experiments.

The first candidate uses Clang's IR profile-guided optimization and explicitly
passes `-fno-math-errno` through clang-cl. FFmpeg 7.1.5's MSVC flag filter drops
that flag even when the underlying compiler is Clang. This experiment retains
floating-point precision options, normal sample rate/bitrate, runtime CPU
dispatch and the same component allowlist. No fast-math or CPU-specific minimum
instruction set is enabled.

Research sources:

- Clang PGO and representative-input guidance:
  https://clang.llvm.org/docs/UsersManual.html#profile-guided-optimization
- Windows profiling runtime linkage:
  https://clang.llvm.org/docs/UsersManual.html#finding-clang-runtime-libraries
- ThinLTO and COFF linking (a possible later experiment, not part of this first
  candidate): https://clang.llvm.org/docs/ThinLTO.html
- The exact upstream `sources/ffmpeg-7.1.5/configure` MSVC flag filter,
  `check_optflags` and LTO checks were inspected locally. The native flag path
  is checked in the generated `ffbuild/config.mak` for every build.

Training uses the full compatibility workload plus separate 45-second varied
stereo/mono/chirp/amplitude-modulated fixtures in nine formats. Timed workloads
use a different signal. Only instrumented-executable profiles under
`profiles/training` are merged; configure probes are excluded. No user media or
account data are used for training. The trained release contains optimized
native code; no profile collection or Python runtime is part of application
execution. The profile file and build flags are retained for reproducibility.

`benchmarks/Run-FFmpegCpuTuning.ps1` chains training, release build, compatibility,
screening and confirmation. It never deploys a candidate itself. Both native
builds receive the app's same decoder arguments. A separate full FFmpeg creates
fixtures before timed work, because the slim reference deliberately lacks the
fixture encoders. Every executable/input hash is recorded.

Before looking at the candidate measurements, the CPU gate was defined as:
at least 2% lower aggregate mean CPU, a paired approximate 95% t interval for
CPU differences wholly below zero, no individual format mean CPU regression
above 5%, and no MP3 mean CPU regression above 2%. Five-repeat screening must be
confirmed independently with nine sustained repeats per format, plus seven
startup repeats. Per-format confidence intervals are descriptive, and these
component experiments cannot establish every real-world playback workload.

Only a candidate that passes compatibility and both CPU gates is eligible for
parent review, isolated actual Rust audio tests and deployment. A smaller or
faster-looking binary alone is insufficient.

The initial five-repeat screen returned a 2.36% aggregate CPU reduction and
6.84% MP3 reduction, but the aggregate paired interval crossed zero. It therefore
did not pass the original screening gate. This result is retained unchanged.
Because the point estimates were promising, an independent confirmation with
15 sustained repeats per format was selected before seeing confirmation data.
`Confirm-FFmpegPgo.ps1` runs that follow-up and applies the original numerical CPU
thresholds to the independent confirmation. A later acceptance must explicitly
report the inconclusive screen rather than claim both original gates passed.

## Final decision

The independent 15-repeat confirmation did not meet the CPU gate. Aggregate
CPU was 27.07396 seconds for the slim reference and 26.86458 seconds for the PGO
candidate: -0.77%, with a paired 95% interval of [-0.48282, +0.06407] seconds.
Offline end-to-end decode was 1.67% slower, with its paired interval above zero.
WAV and MP3 improved in this workload, but Opus regressed; the aggregate result
does not justify replacing the active helper. These are offline component
measurements, not real-time player CPU percentages.

The PGO candidate compiled and passed 153 compatibility checks but was **not
deployed**. `tools/ffmpeg.exe` and the packaged helper retain the validated slim
release, SHA-256 `BE9446B0C7C15CDC96986FD8630B925525A2099DCFC274C291745BC4B9EB149A`.
The screen, independent confirmation, profiles, candidate and prior release are
preserved locally. Charts, raw trials and CPU assessment are in
`benchmarks/results/ffmpeg-pgo-confirm` (relative to the Rust project root).
