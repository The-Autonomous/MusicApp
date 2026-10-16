# Source parity audit

This records the native port's reviewed scope and the limits of verification.
It does not certify that every original behavior or every Windows game has been
validated. The frozen original remains the reference. The live Python source
received the user-authorized overlay/sign-in fixes described below; its earlier
snapshot remains intact.

The active port contains native Rust application logic, Tcl presentation code,
and native runtime libraries. Original reference code and the abandoned
compatibility service are archived in `legacy_reference`. They are not runtime or
build dependencies.

## Verified by native automated checks

The full Rust test run completed with **50 passing tests** at this audit point.
These include native audio/DSP, radio protocol, model, platform, lyric, library,
and native YouTube utility tests; consult the test output for the exact scope.

| Area | Evidence | Limit |
|---|---|---|
| Local library metadata | Real silent WAV scan/cache test | Does not establish correctness for every real-world container |
| Hidden playback cache | Real hidden WAV metadata regression, excluded from library scan | Native internet audio download is not covered |
| Identity / URL policy | Conservative artist/title keys and exact YouTube URL checks | Native Unicode case folding is not exhaustive |
| Resource policy | CPU/RAM/gaming budget boundary tests | Actual multi-process workload benchmarks are separate |
| Lyrics | Cache schema, positive/negative expiry, eviction, timestamp parser, latest-only queue, local HTTP retry | No live lyric-provider account/network guarantee |
| Native session encryption | Windows DPAPI round-trip, encrypted profile import API, exact in-memory cookie-header check, and synthetic Chrome/Edge connector checks | No saved Python session was found; successful live Chrome/Edge login remains unverified |
| Queue identity | Same-song upload aliases grouped, versions retained | Full authenticated likes library has not been exercised |
| SponsorBlock intervals | Invalid intervals rejected and overlaps merged | Actual provider data and final music-cut alignment remain unverified |

## Native YouTube implementation

The Rust service uses RustyPipe for YouTube/YouTube Music API calls, authenticated
likes, search, playlist continuation, and stream URL extraction. It stores the
entire API cache behind Windows DPAPI in the selected port state directory.
Plaintext account reports and external Botguard discovery are disabled.

The service implements up to three admitted downloads, dynamic system resource
limits, cancellation, two attempts, alternate upload IDs, 30-day deferrals,
checkpoints, native MP3 conversion/tags, SponsorBlock interval removal, and a
protected eight-file / 64 MiB temporary cache. These are implementation facts;
they have not all been exercised against a live signed-in account.

Sign-in selects Chrome or Edge, uses an app-owned persistent profile, a native
Windows process job, a loopback DevTools endpoint, visible account markers, and
YouTube-only cookies. The normal browser's cookie database is not inspected or
decrypted. `connect-browser.bat` can instead reuse an existing signed-in profile
through the bundled, user-installed Chrome/Edge extension and an explicit
Connect click. Its handoff is localhost-only, requires a random one-time nonce
and an extension Origin, bounds input sizes and socket timeouts, and verifies
the account before reporting success. The extension has no background worker.
Firefox fallback and native SQLite are retired. Browser-extension installation,
Google sign-in, and successful live account acceptance remain manual checks.

Material unresolved limits:

- YouTube changes its undocumented APIs and stream protection. Videos requiring
  a PO token may fail: there is no bundled Botguard generator or script fallback.
  Such failures are reported and follow retry/defer policy.
- Native private/age-restricted/premium/regional playback and very large likes
  lists require live validation. No successful account test is claimed here.
- The source's automatic downloader package repair has no equivalent external
  package installation; native extractor updates require rebuilding the app.
- Source updater/setup maintenance command parity must be evaluated separately
  from normal launch/build scripts; it is not established by the backend tests.

## Runtime acceptance still needed

| Area | Remaining verification |
|---|---|
| UI | Eight native presentation checks pass, including six source comparisons of fonts/layout/geometry plus actual hint fade/timeout and search/authentication visibility transitions. Pixel screenshots and unusual DPI configurations still need acceptance. |
| Global shortcuts | Native global-hook startup/shutdown smoke passed on this desktop. Model tests cover Alt-side bindings/conflicts; actual foreground-game input and media hardware remain unverified. |
| Topmost overlay | Normal desktop, elevated Task Manager, borderless games, exclusive fullscreen, focus changes |
| Radio | Two-device streaming, reconnection, fallback download, lyrics, host EQ/volume restore, cancellation |
| Persistence | Current personal profile imported into packaged state; semantic preferences match and copied settings hashes match. Restart during playback, failed sync, and radio persistence still require acceptance. |
| Performance | 99 trials compare unchanged Python UI/audio components, previous Rust and updated Rust. Warm-cache readiness improved in most states; steady CPU did not improve uniformly. Full controller/network/game/GPU and cold-boot workloads remain unmeasured. |

Parent integration fixes reported during source review included progress field
names, canonical search URLs, memory-only session retention, player-off gating,
temporary cache/history handling, local playlist search, redirected Documents,
radio state restoration, log pagination, and failed-track recovery. Each should
be checked in the final runtime; compilation alone does not prove its behavior.

## Native audio-only FFmpeg build

The Rust package now uses FFmpeg 7.1.5 compiled with existing Clang 20.1.7,
static LAME/zlib and Schannel HTTPS. The 153-check native compatibility gate
passed, including 21 fixture families, full decode and seeks, Unicode metadata,
MP3 encoding with/without SponsorBlock filters, video-container audio extraction,
HTTP ranges and local HTTPS. Five isolated muted runs through the packaged Rust
app passed for MP3 bypass/EQ/echo, AAC and Opus, with zero audio underflows.
The Rust executable and source/package UI hashes are unchanged. This does not
establish live authenticated streaming, every internet radio server, every audio
variant, game behavior or exhaustive one-to-one parity. Native decoder startup,
memory and sustained CPU are reported separately from the older UI/audio charts
in `benchmarks/results/ffmpeg-audio-final-performance` and the longer CPU run
`benchmarks/results/ffmpeg-audio-long-performance`.

The unchanged-hash statement above describes that earlier FFmpeg deployment.
The subsequent overlay fix builds a new Rust executable and UI. Earlier timing
reports continue to describe their recorded releases, not this new executable.

## October 14 overlay and sign-in corrections

Rust now coalesces native mouse movements, caches monitor geometry per gesture,
and avoids flushing Tk layout for every positional move. An 8 ms wake timer is
active only during dragging; it stops on release/hide. This addresses the prior
100 ms event-loop pacing during native dragging without adding idle polling.
It is a scheduling change, not a measured claim of universal frame rate.

Both implementations initially hide the controls hint, fade it in on right
drag, and hide the player while startup is incomplete or no audio is loaded.
Paused loaded tracks remain visible, and temporary hiding preserves user intent.
Authentication withdraws MusicApp windows before browser launch, restores prior
windows afterward, and normally maximizes Chrome/Edge. Rust requires a UI-hidden
acknowledgement before its browser worker can launch. Automated checks exercise
success, failure, cancellation, timeout and restoration; no live sign-in is claimed.

## Subsequent CPU tuning decision

The profile-guided FFmpeg candidate passed 153 compatibility checks but did not
prove an aggregate CPU benefit. The independent 15-repeat confirmation measured
0.77% less CPU with an interval crossing zero and 1.67% longer offline decode
time. It was not deployed. The validated slim FFmpeg remains the active helper.
See `native/ffmpeg/CPU-TUNING.md` and the retained `ffmpeg-pgo-confirm` report.
