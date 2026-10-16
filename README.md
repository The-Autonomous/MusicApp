# MusicApp

A Python music overlay for Windows, with the original keyboard controls, EQ,
lyrics, local-library search, YouTube playback, radio and phone control panel.

The separate native Rust version and its complete local build workspace are in
[`rust_musicapp`](rust_musicapp/README.md). Run `rust_musicapp/run.bat`, or compile
with `rust_musicapp/build.bat`. Its executable has no Python/Node runtime dependency.

## Controls

Press **both Alt keys together** to open the controls card from another window.
Right-dragging fades this shortcut in beneath the song title; it fades out ten
seconds after release. **Quick controls**
offers buttons for playback, search, effects and YouTube; **Shortcuts** shows the
actual saved bindings. Select **Change**, then press a key to remap an action.
The existing Left or Right Alt side is kept, conflicting shortcuts are rejected,
and **Restore default shortcuts** resets the bindings. **Esc** closes the card.

These are the defaults; saved remaps take precedence:

| Action | Shortcut |
| --- | --- |
| Open controls | Both Alt keys |
| Previous / next song | Left Alt + `-` / `=` |
| Play / pause | Left Alt + `;` |
| Volume down / up | Left Alt + `[` / `]` |
| Repeat | Left Alt + `'` |
| Show / hide lyrics | Left Alt + `/` |
| Find a song | Left Alt + `\` |
| Sound and effects | Right Alt + `\` |
| YouTube sign-in and sync | Right Alt + `Y` |
| Show / hide player | Left Alt + Left Shift |
| Player on / off | Right Alt + Right Shift |
| Radio on / off | Left Alt + `A` |
| Find a radio station | Left Alt + the backtick key |
| Restart / quit | Right Alt + `.` / Ctrl |

Media playback keys work too. Right-drag the passive player overlay to move it.
Its window stays above other windows without taking keyboard focus. Use
**borderless fullscreen** in GTA to keep it visible; a normal Windows overlay
may be hidden by exclusive fullscreen. Interactive controls and sign-in cards
accept focus when opened.
The player retains its translucent black background when hidden and reopened.
Left Alt + Left Shift changes its remembered visibility preference. The player
stays hidden until startup finishes and audio is loaded; paused tracks remain
visible. Temporary startup/sign-in hiding does not erase that preference.
Visibility, the player on/off setting and its screen position are saved in
`.musicapp_overlay.json`. The player restores those preferences on launch and
keeps its window inside a connected monitor's usable area, including monitors
with negative desktop coordinates. It recovers automatically after a saved
off-screen position, a monitor disconnection or a layout change.
EQ and echo controls publish changes without waiting for audio processing or
buffer setup; presets update all bands together.

## Start and connect YouTube

Start `main.py` using your usual shortcut. A small sign-in card opens automatically.
Music waits until the YouTube connection is verified and every eligible missing
song from likes and `Playlists.txt` has downloaded or been deferred after two
failures. The loading card shows one overall progress bar for the combined queue
and a list of active songs, each with its own progress bar and conversion/removal
status. Its custom minus button collapses it into a small status strip while
setup continues. Use the restore arrow to expand it. Reopen the card
with **Right Alt + Y**, or through the controls window. Press **Right Alt + Y**
again to hide the YouTube card; downloads continue while it is hidden.

- Without a working saved login, your Windows default browser opens directly
  at YouTube sign-in. Sign in yourself; MusicApp never asks for your password.
  Supported Chromium browsers (Chrome, Edge, Brave, Opera, Vivaldi and Chromium)
  use a separate temporary sign-in profile and Playwright connects to that
  window. This avoids copying/decrypting your regular browser's locked cookies.
  Only MusicApp's sign-in browser is closed afterward. Firefox opens normally
  and its persisted YouTube login is checked at a low polling rate; it may need
  a retry after the browser has saved its cookies. Other browsers open normally
  but report clearly if automatic confirmation is unsupported.
- During interactive sign-in, MusicApp hides its windows until the browser step
  finishes. Supported Chromium sign-in windows are normally maximized to fit the
  screen; this does not use F11 fullscreen. Previously visible app windows return
  afterward, with the player still subject to its playback readiness preference.

- Login cookies are encrypted with Windows DPAPI under the current Windows
  account in `%LOCALAPPDATA%\MusicApp\youtube_session.bin`. If Windows cannot
  remember the login, the app explains this and uses it for this session only.
  A corrupt or moved saved login triggers fresh sign-in instead of crashing.
- Failed setup offers **Retry setup** and **Quit**. Completed downloads are kept;
  retry downloads the remaining songs. Cancelling setup keeps playback blocked.
  A failed song moves to the back of the queue for one retry. If it fails again,
  it is skipped for 30 days so setup and playback can finish.
  Esc minimizes the loading card without stopping setup.
- Under **Options**, choose **YouTube Music liked songs** or **YouTube liked videos**. Sync fetches
  every entry YouTube makes available, saves the list locally and downloads
  missing tracks after checking the local library. It cannot recover likes that YouTube does not
  expose, or download deleted/private/unavailable videos. Unavailable downloads
  are counted and become eligible again on the first sync after their cooldown.
- Startup automatically reuses and verifies the saved login, then syncs likes and playlists.
  Expired sessions reopen sign-in. **Stop sync** preserves completed downloads;
  **Forget YouTube login** under Options removes the saved session without
  deleting music. Progress is saved periodically and when a sync stops.

Permanent YouTube downloads, including liked songs, go to the detected
`GTAV Enhanced/User Music` folder. The original GTA V User Music folder is the
next choice; without either GTA folder, the configured local library is used,
falling back to `~/MusicPlayerYoutubeDownloads`. That older downloads folder
remains included in the library so completed songs are reused. Existing downloads
are not deleted when unliked.
`.youtube_likes.json` holds the lists, `.youtube_downloads.json` holds the
completed-file mapping, and `.youtube_sync.json` holds preferences. Login cookies
are not stored in these files.
`.youtube_deferred_downloads.json` stores deferred video IDs, titles and retry
timestamps, and is ignored by Git. Delete it to make those songs eligible again
on the next sync.
Before downloading, sync scans the full local library, including songs omitted by
fast loading. It first reuses recorded downloads or files with the same video ID
in their filename, then checks title and artist together. Matching ignores case,
spacing, punctuation, artist `- Topic` suffixes and labels such as `(Official
Video)` or `[Lyrics]`; live, remix, instrumental and other version labels remain
distinct. Missing, empty, partial and cloud-only files do not count as downloads.
Recovered matches are saved to the download mapping before playback starts;
deleting or losing that mapping does not force existing audio to download again.
Invalid YouTube links and entries already marked private, deleted or unavailable
are skipped before downloading. Other failures still use the retry policy above.
The queue contains each video ID once and groups matching titles and artists
across every playlist and the likes list before starting parallel downloads.
Each video's ID is recorded against the shared audio file. A song present in
several sources counts once in the overall progress total.

New permanent downloads run SponsorBlock and remove reported sponsor segments,
intros, outros, self-promotion, previews, filler and non-music sections before MP3
conversion. This uses community-provided segment markers; songs without markers
are saved normally. Existing music is reused without rewriting it.

## Playing alongside GTA

Downloads share one queue and a live resource budget. Free RAM, memory pressure
and total CPU load are sampled every two seconds, protecting other workloads
such as Python as well as games. GTA, Far Cry and executables in common game
installation directories also select a conservative budget; process checks are
cached for ten seconds.

With healthy memory usage, at least 6 GiB available and CPU below 35%, up to three songs download
together. Moderate load allows two. Games, less than 4 GiB available or CPU at
60% and above use one. Higher pressure further reduces the shared bandwidth
budget and increases the gap between starts; very low RAM uses 64 KiB/s overall.
Bandwidth is divided among active downloads rather than multiplied by their
count. Limits fall immediately and rise after ten stable seconds of recovery.
Active downloads adjust their speed while running; existing jobs finish before
the queue admits more at a lower concurrency. No app restart is needed as
resources change. Windows adjusts the owned workers and encoders between below
normal and idle priority. Each encoder and fragment downloader uses one thread.
These limits reduce contention but cannot guarantee a particular frame rate.

Automatic gaming audio bypasses EQ/echo while GTA runs and restores the chosen
mode afterward. It can be disabled under YouTube Options. Manual Gaming Mode remains
in the EQ window. Audio playback itself is not assigned idle priority.

Playlist downloads from `Playlists.txt` join the startup queue automatically;
**Sync Playlists.txt** under YouTube Options and **Sync music** refresh the same
combined queue. Radio discovery runs only when radio is selected or scanned,
with at most four request workers and bounded response sizes. Radio clients own
their status/download threads, sessions and responses; stopping radio or closing
the app cancels those requests and releases the owned resources.
Enable **radio hosting / phone controls** to start the server on port 8080;
the address is printed in the log. Occupied ports produce an error and no other
process is terminated. The existing web interface remains at `/app_pad`.
Radio playback streams from hosts that support seeking, with bounded buffering
and network timeouts. It falls back to a size-limited temporary download when
streaming cannot start or drops out.

Temporary YouTube playback keeps the downloaded audio format to avoid MP3
conversion and reuses a cached video before starting a downloader. The temporary
cache has an eight-file, 64 MiB budget; oldest unprotected files are removed on
cache use. The active song and newly requested song are protected, so they can
temporarily exceed that budget. Only MusicApp's recognized temporary audio files
are eligible for removal. Permanent copies use MP3 for compatibility with GTA's
User Music and are outside this cache.

Codecs and the output-device runtime load on demand, and the output device opens
when playback starts. Audio uses a bounded half-second buffer, one decoding
thread and one codec thread; decoding and effects run outside the output callback.
The output device is reused between tracks. Numerical-library thread limits
apply only to MusicApp and its children, leaving other programs' settings alone.
While sign-in holds playback, the playback controller sleeps until an event wakes
it. Paused playback uses a longer wait and does not repeatedly write resume
checkpoints; changes, active playback and shutdown still save state.

## Setup and maintenance

Python 3.12 or newer is recommended. Normal startup checks for missing core
dependencies **before loading keyboard/mouse hooks or starting playback**. If
something is missing, a small preparation card installs the required binary
packages in a cancellable low-priority worker. It offers Retry or Close if setup
fails. Existing dependencies do not trigger installation on every launch.
Setup failures show the installer's error and record it in `.logging.txt`.
Setup, YouTube and controls cards use the app's borderless style; drag their
headings to move them. Esc minimizes loading cards and closes the controls card.

For optional full setup, including FFmpeg for permanent MP3 downloads, run:

```powershell
python main.py --setup
```

Full setup installs missing binary packages and FFmpeg. YouTube sign-in prepares
missing YouTube packages automatically in a cancellable low-priority worker,
without compilers or browser downloads. Installed packages are not upgraded on
every launch. If a dependency fails to import, one automatic repair is attempted.
The actual default browser is used, so no separate Playwright browser setup or
browser selection is needed. Google or browser policies may still reject a
connection; failures leave music paused and offer Retry setup or Quit.
Music likes are fetched through the Music client's
paginated playlist API, without keeping a browser open or scrolling thousands
of rendered rows. The application reads account data; it does not change likes
or playlists in your YouTube account.
Current yt-dlp may also need a supported JavaScript runtime for YouTube's player
challenges; the downloader enables Node.js when it is available on PATH. The sync
window counts downloads that could not be saved and offers a later retry.

`python main.py --update` explicitly checks the upstream repository and backs
up files before updating. Use it only when intending to replace local source
with the upstream version.

## Validation of this revision

All 182 checks outside the native interface layout suite passed on Windows with
Python 3.13. These include live resource changes, parallel downloads, combined
playlist/likes matching, progress reporting, worker cleanup and real segment
removal on short silent fixture audio, overlay background drawing, YouTube
window toggling and effect-control responsiveness during blocked DSP work.
Live keyboard input against the real overlay confirmed showing and hiding while
the player was disabled, automatic recovery from `(50000, 50000)`, and restoring
hidden/disabled state and position across restarts. The live tests used a separate
preferences file and silent effects processing without opening an audio device.
Windows Computer Use screenshot capture timed out; window mapping and actual
native coordinates were verified through the live test's state instead.
Temporary progress-file reader collisions are also covered and cannot fail an
audio download. The native interface suite was left out
to avoid opening and closing windows during an active download session; progress
list behavior was checked with simulated widgets instead.
Run the full suite with `python -B -m unittest discover -s tests` after installing
the dependencies; test media, sessions and servers use disposable fixtures.
The full suite opens and closes temporary windows for interface layout checks.
For download matching checks without those windows, run
`python -B -m unittest discover -s tests -p 'test_youtube*.py'`.

Automated checks cover startup preparation and cancellation, sign-in verification,
playback gating until the final download and checkpoint, retrying failed downloads,
minimized loading progress, encrypted-session fallback, shortcut remapping, controls
layout, cache reuse and eviction, real audio decoding/resampling/seek, effects,
pause/resume and output-device reuse, and radio limits and resource cleanup.
Most audio checks use a fake output device and disposable audio, without playing
user music. A short silent check of the real output-device path also passed.
Source syntax and patch formatting were checked too.

An isolated sign-in UI preview used about 42 MiB of resident memory, compared
with about 58 MiB before the latest lazy audio loading. Windows recorded no CPU
time in its six-second idle sample at the available timer resolution. That
preview had an empty library and disabled global input hooks; these numbers
are not a guarantee for playback, downloading, a large library or a real game.

Real Google sign-in, access to the account's complete available likes, and GTA
frame-time impact remain unverified. The automated browser checks do not sign in
to a real account, and the silent device check does not establish audible quality
or performance while gaming.
