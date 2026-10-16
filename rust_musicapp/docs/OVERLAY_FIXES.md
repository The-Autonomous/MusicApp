# Overlay fixes and local build verification — October 14, 2026

The Rust release compiled successfully and is packaged at
`dist/MusicApp/MusicApp.exe`. Use `run.bat` to launch it and `build.bat` to
recompile. Keep the adjacent native runtime, tools and UI folders with the EXE.
The Rust application and compile scripts require no Python or Node runtime.

## Behavior changes

- Rust dragging now coalesces queued mouse movements, caches monitor work areas
  for each gesture, and avoids flushing Tk layout for each position update.
  The native event loop wakes every 8 ms during dragging instead of waiting for
  the ordinary 100 ms tick. That timer stops on release or hiding. Duplicate
  native/Tk drag paths were removed. This is not a measured frame-rate promise.
- In both implementations, the controls hint starts hidden, fades in during a
  right drag, stays visible throughout a long drag, and fades out ten seconds
  after release. Hiding the player cancels its drag/hint timers.
- The player stays hidden until startup is ready and an audio track is loaded.
  Loaded paused tracks remain visible. Temporary hiding preserves the user's
  saved visibility preference and position. Search closing checks current
  readiness, including playback that became ready while search was open.
- During interactive sign-in, MusicApp windows are withdrawn before the browser
  opens. Chromium sign-in uses normal maximization, not F11. Rust waits for an
  explicit UI-hidden acknowledgement before launch. Prior surviving windows
  return afterward, with the player still governed by its readiness preference.

The live Python source received the equivalent fixes at the user's request.
Its old source is retained in `legacy_reference/reference`; an immediate pre-fix
backup is in `local_backups/before-overlay-fixes`.

## Verification

| Check | Result |
|---|---|
| Complete Python unittest discovery | 210 passed |
| Rust tests in final build | 50 passed |
| Native presentation checks | 8 passed, including six source geometry comparisons, actual ten-second fade timing, search readiness and authentication restoration |
| Packaged executable smoke runs | 10 runs, zero errors |
| Bypass/EQ/echo playback | Clock advanced to about 4.36 seconds in each isolated muted run; no audio error or underflow |
| Paused visible/hidden workloads | Remained paused, zero underflows |
| Build/package EXE and UI hashes | Match |
| Frozen original sources | 50 checked, zero changes |
| Saved original profile files | 12 checked, zero changes |
| Python runtime files in Rust package | Zero |

The ten smoke states were idle visible/hidden, paused visible/hidden, playback
bypass/EQ/echo, controls, search and progress. Each used a fresh isolated state
directory. Raw results, state diagnostics and an HTML report are retained under
`benchmarks/results/2026-10-14-overlay-fixes-smoke`. These short, single-repeat
runs verify execution and audio state; their CPU readings cannot establish a
performance improvement. Historical comparison charts retain their original
binary/UI hashes and describe those earlier releases.

Final package EXE SHA-256:
`EC90A5C24814D1E84FC556CA6E57381C267243D96CAF27EC9A8B6452B4DCF144`.
The machine-readable file/hash checks are in
`local_backups/overlay-final-verification.json`.

The Python full-suite run initially exposed destroyed Tk test objects being
collected by later worker threads. Test-only cleanup now releases those objects
on their UI thread; no production shutdown workaround was added.

Live Google sign-in, foreground-game dragging and exclusive-fullscreen/topmost
behavior were not tested. No exhaustive one-to-one or universal speedup claim
is implied by these results.

## Source organization

`rust_musicapp` is the single Rust workspace folder, including full application
source, Tcl UI, browser connector, assets, pinned Cargo dependencies, native build
recipes, local runtime files and compile scripts. Archived material is now
inside `legacy_reference`. Root and port Git ignore rules retain source, tests,
manifests, locks and licenses while excluding binaries, build caches, personal
state/cookies, generated measurements and local backups. Existing ignored
runtime files remain available for local compilation.

## FFmpeg tuning outcome

The profile-guided candidate compiled and passed 153 compatibility checks, but
the independent confirmation did not prove an overall CPU gain: -0.77% CPU,
with a confidence interval crossing zero, and 1.67% slower offline decoding.
It was not deployed. The validated slim helper remains active; see
`native/ffmpeg/CPU-TUNING.md` for the retained evidence.

My assessment: fixing redundant UI work and window lifecycle behavior is the
useful improvement here. The app has a clearer path to smoother interaction
without continuous extra idle polling. The data does not justify swapping in
the PGO FFmpeg candidate or claiming every workload is faster.
