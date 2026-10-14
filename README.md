# MusicApp

A Python music overlay for Windows, with the original keyboard controls, EQ,
lyrics, local-library search, YouTube playback, radio and phone control panel.

## Controls

Press **both Alt keys together** to open the controls card from another window.
The player also shows this shortcut beneath the song title. **Quick controls**
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

## Start and connect YouTube

Start `main.py` using your usual shortcut. A small sign-in card opens automatically.
Music waits until the YouTube connection is verified and every eligible missing
liked song has downloaded or been deferred after two failures. The loading card
shows song counts and the current
download; its custom minus button collapses it into a small status strip while
setup continues. Use the restore arrow to expand it. Reopen the card
with **Right Alt + Y**, or through the controls window.

- Without a working saved login, your Windows default browser opens directly
  at YouTube sign-in. Sign in yourself; MusicApp never asks for your password.
  Supported Chromium browsers (Chrome, Edge, Brave, Opera, Vivaldi and Chromium)
  use a separate temporary sign-in profile and Playwright connects to that
  window. This avoids copying/decrypting your regular browser's locked cookies.
  Only MusicApp's sign-in browser is closed afterward. Firefox opens normally
  and its persisted YouTube login is checked at a low polling rate; it may need
  a retry after the browser has saved its cookies. Other browsers open normally
  but report clearly if automatic confirmation is unsupported.
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
- Startup automatically reuses and verifies the saved login, then syncs likes.
  Expired sessions reopen sign-in. **Stop sync** preserves completed downloads;
  **Forget YouTube login** under Options removes the saved session without
  deleting music. Progress is saved periodically and when a sync stops.

Liked songs download to `~/MusicPlayerYoutubeDownloads`, which is included in
the library automatically. Existing downloads are not deleted when unliked.
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
Recovered matches are saved to the download mapping before playback starts.
Invalid YouTube links and entries already marked private, deleted or unavailable
are skipped before downloading. Other failures still use the retry policy above.
The queue contains each video ID once and also reuses a matching song downloaded
earlier in the same sync, even when another liked upload has a different video ID.

## Playing alongside GTA

Downloads share one queue, with one encoder and one fragment download at a time.
Windows runs the worker below normal priority; while GTA is detected, the worker
uses idle priority, limits downloads to 512 KiB/s and leaves a longer gap between
tracks. Child encoders inherit the worker's priority and use one encoding thread.
Process checks are cached for 15 seconds. This reduces contention but does not
guarantee a particular frame rate.

Automatic gaming audio bypasses EQ/echo while GTA runs and restores the chosen
mode afterward. It can be disabled under YouTube Options. Manual Gaming Mode remains
in the EQ window. Audio playback itself is not assigned idle priority.

Playlist downloads from `Playlists.txt` are now explicit: use **Sync Playlists.txt**
under YouTube Options. Radio discovery runs only when radio is selected or scanned,
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

All 149 automated checks passed on Windows with Python 3.13 in about 29
seconds. Run them with `python -B -m unittest discover -s tests` after installing
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
