# MusicApp for Windows — Rust port

This is a separate Windows application with its own executable and state.
The original Python application remains available; the October 14 overlay and
sign-in fixes were also applied to that source at the user's request. Previous
source snapshots are preserved locally. Builds do not replace original settings,
shortcuts, credentials, or installation.
Application logic, audio, library, lyrics, radio, keyboard hooks and YouTube
services run in Rust. The existing native C Tcl/Tk rendering engine provides the
UI through Tcl presentation definitions; FFmpeg provides native codecs. No Python runtime,
Python DLL, yt-dlp executable, or Python helper is used by the application.
Python files in `benchmarks` are optional comparison/verification tools only.

## Run and build

- Double-click `run.bat` to run the packaged version.
- Double-click `build.bat` to compile and package a Windows executable.
- Run `powershell -NoProfile -File Build.ps1 -Check` to test before packaging.
- The output is `dist/MusicApp/MusicApp.exe`. Keep its adjacent `runtime`,
  `tools`, `ui`, and `app_pad` directories with it.

The installed Rust 1.87 MSVC toolchain is sufficient; this script does not
upgrade it. The project pins compatible dependencies in `Cargo.lock`. Native
runtime components are local to this project. It targets Windows 10/11 x64;
testing on this workstation does not certify every Windows/audio-device version.

Normal startup opens the YouTube setup card and holds playback until account
verification and sync finish, as in the source. Sign in yourself in Chrome or
Microsoft Edge. MusicApp uses the default browser when it is Chrome/Edge, then
falls back to installed Edge or Chrome. `run.bat --browser chrome` and
`run.bat --browser edge` select one explicitly. The app's separate browser profile
is retained, so later sign-ins can reuse that session. You enter Google credentials
directly in the browser. Sessions are protected by Windows DPAPI under this port's
`state` directory. Firefox is not selected or inspected.

To reuse the account already signed in to your normal Chrome/Edge profile,
run `connect-browser.bat` (optionally `--browser chrome` or `--browser edge`). The
local connection page explains the one-time installation of the bundled
`browser_connector` extension using the browser's Load unpacked control. Open
its popup on that connection tab and click Connect. Only YouTube cookies from
that profile are handed to MusicApp; the extension has no background worker.
The app verifies the session and remembers it encrypted. Your normal browser is
not restarted, closed, decrypted, or modified. Extension installation and Google
sign-in are manual browser actions. Chrome's default-profile debugging restriction
is respected; no security policy or browser protection is disabled.

Factory defaults come from the original source. An explicit profile import
copies the current personal settings into the packaged app's separate state:

```powershell
powershell -NoProfile -File Import-Profile.ps1
```

This reads the original profile without changing it, backs up replaced port
settings, and writes `dist/MusicApp/state/profile_import_report.json`. It imports
playback preferences, overlay preferences when present, shortcuts, the canonical
`musicapp_eq.json`, recommendations, lyrics and YouTube caches, and playlists.
An existing app session is decrypted in memory and stored separately with Windows
DPAPI; reports never contain cookie values. If no app session exists, connect an
existing Chrome/Edge profile or use normal sign-in. The report identifies its
origin and whether the server accepted it. A missing
or expired session requires manual sign-in; a copied file alone is not proof of
a working account. Imports are explicit; builds preserve existing port settings.

The port discovers the original GTA music folders for reading and for explicit
permanent downloads. Factory `Playlists.txt` starts empty; profile import can
replace it with the original playlist list.

Both Alt keys open the controls card. Left Alt + Backslash opens search;
Right Alt + Backslash opens effects; Right Alt + Y opens YouTube. All source
default bindings, Alt sides, media keys, remapping and conflict checking are
implemented. The player uses the same 0.7 alpha, black rounded background,
magenta transparency key, Segoe UI fonts, theme, and native widgets. The passive
window is click-through and does not activate when moved or raised.

The player remains hidden during startup until setup finishes and a track is
loaded for playback. Paused loaded tracks remain visible. Temporary hiding does
not change the remembered visibility preference. Right-button dragging reveals
the controls hint, which fades out ten seconds after release. During interactive
sign-in all MusicApp windows are withdrawn while Chrome/Edge is normally maximized.

## Diagnostics and comparisons

`MusicApp.exe --diagnostics` reports local runtime availability. A local-only,
muted run is available using `--no-network --mute --library C:\Music`; this is a
diagnostic mode that bypasses the normal sign-in gate. `--state-dir` selects a
separate settings directory. `--seconds` exits after the requested duration.

See [benchmarks/README.md](benchmarks/README.md) for repeatable Python/Rust
component comparisons, raw time series, charts and their limits. Benchmarks
include observed child processes and reject failed audio playback rather than
counting it as an improvement. Real downloads, sign-in and game frame times are
separate from those controlled workloads.

The Rust package uses an audio-focused FFmpeg 7.1.5 build. It retains the app's
audio decoding, MP3 downloads, SponsorBlock filters and HTTP/HTTPS support while
removing video/GPU codecs and unrelated programs. Its compiler comparison,
compatibility checks and exact native decoder measurements are documented in
`benchmarks/results/ffmpeg-audio-final-performance`. Rebuild that native tool with
`Build-FFmpeg.ps1`; see [native build notes](native/ffmpeg/README.md). This does not
rebuild or change the original Python app's FFmpeg. A candidate is validated
before deployment; smaller size does not imply lower CPU in every codec.

## Verification limits

Read [docs/PARITY_AUDIT.md](docs/PARITY_AUDIT.md). This is a working port under
validation, not a certification of exhaustive one-to-one behavior. Live Google
login, full real-account likes, protected streams, game input and exclusive
fullscreen visibility require real-world acceptance tests. YouTube videos that
require an unavailable PO token can fail and follow retry/defer policy. There is
no hidden script downloader fallback. Native extractor updates use a rebuild;
the old Python source updater is not used to overwrite this application.

Ordinary topmost windows cannot promise visibility above every game or elevated
system window. The port does not alter Windows security settings, install an
accessibility certificate, inject into a game, or take exclusive display control.

Everything for the Rust source and local build lives in this `rust_musicapp`
folder: `src`, `ui`, `app_pad`, `browser_connector`, native build recipes, pinned
dependencies, and `build.bat` / `Build.ps1`. Local native runtime files and build
outputs stay here too; Git excludes them. The original frozen source and prior
development material are under `legacy_reference`; the immediate pre-fix backup
is under `local_backups/before-overlay-fixes`. The original application remains
available at `../main.py`. Optional Python benchmark/reference files are not
part of the Rust application or its compile path.

## Licensing

Original MIT notices remain intact. The combined Rust package links GPL-licensed
RustyPipe; its aggregate package metadata is GPL-3.0-only. See
[THIRD_PARTY.md](THIRD_PARTY.md), `Cargo.lock`, and `licenses` before redistributing.
