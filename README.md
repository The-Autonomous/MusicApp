# MusicApp

A Python music overlay for Windows, with the original keyboard controls, EQ,
lyrics, local-library search, YouTube playback, radio and phone control panel.

## Start and connect YouTube

Start `main.py` using your usual shortcut. Local music starts without waiting
for downloads. A YouTube Sync window appears at startup; closing it keeps music
playing. Reopen it with **Right Alt + Y**, or through the controls window.

- **Connect YouTube** opens a short-lived Playwright browser. Sign in yourself;
  the application never asks for or stores your password. The browser closes
  after sign-in. Cookies are encrypted with Windows DPAPI under the current
  Windows account in `%LOCALAPPDATA%\MusicApp\youtube_session.bin`.
- **Use existing browser login** reads the selected browser's YouTube session.
  Google may reject browser automation, and Chrome/Edge can restrict cookie
  access. **Import exported YouTube cookies** is the fallback for those cases.
- Choose **YouTube Music liked songs** or **YouTube liked videos**. Sync fetches
  every entry YouTube makes available, saves the list locally and downloads
  missing tracks by video ID. It cannot recover likes that YouTube does not
  expose, or download deleted/private/unavailable videos. Unavailable downloads
  are counted and can be retried on the next sync.
- **Sync automatically on startup** reuses the saved login without reopening
  a browser. Expired sessions require reconnecting. **Stop sync** cancels the
  current job and preserves completed downloads; **Forget login** removes the
  saved session without deleting music.

Liked songs download to `~/MusicPlayerYoutubeDownloads`, which is included in
the library automatically. Existing downloads are not deleted when unliked.
`.youtube_likes.json` holds the lists, `.youtube_downloads.json` holds the
completed-file mapping, and `.youtube_sync.json` holds preferences. Login cookies
are not stored in these files.

## Playing alongside GTA

Downloads share one queue, with one encoder and one fragment download at a time.
Windows runs the worker below normal priority; while GTA is detected, the worker
uses idle priority, limits downloads to 512 KiB/s and leaves a longer gap between
tracks. Child encoders inherit the worker's priority and use one encoding thread.
Process checks are cached for 15 seconds. This reduces contention but does not
guarantee a particular frame rate.

Automatic gaming audio bypasses EQ/echo while GTA runs and restores the chosen
mode afterward. It can be disabled in YouTube Sync. Manual Gaming Mode remains
in the EQ window. Audio playback itself is not assigned idle priority.

Playlist downloads from `Playlists.txt` are now explicit: use **Sync playlists**
in YouTube Sync. Radio discovery runs only when radio is selected or scanned.
Enable **radio hosting / phone controls** to start the server on port 8080;
the address is printed in the log. Occupied ports produce an error and no other
process is terminated. The existing web interface remains at `/app_pad`.
Radio playback streams from hosts that support seeking, with bounded buffering
and network timeouts. It falls back to a size-limited temporary download when
streaming cannot start or drops out.

Temporary YouTube playback keeps the downloaded audio format to avoid MP3
conversion. Permanent copies use MP3 for compatibility with GTA's User Music.

## Setup and maintenance

Python 3.12 or newer is recommended. If dependencies are missing, run:

```powershell
python main.py --setup
```

Setup installs missing binary packages and FFmpeg. It is intentionally separate
from ordinary startup: no package installation, compiler installation or source
updates happen while starting the player. Playwright uses installed Edge or
Chrome; if neither is available, install its browser separately:

```powershell
python -m playwright install chromium
```

The **Install / update YouTube support** button installs or updates Playwright,
yt-dlp and the YouTube Music client in a cancellable low-priority worker. This
only runs when clicked. Music likes are fetched through the Music client's
paginated playlist API, without keeping a browser open or scrolling thousands
of rendered rows. The application reads account data; it does not change likes
or playlists in your YouTube account.
Current yt-dlp may also need a supported JavaScript runtime for YouTube's player
challenges; the downloader enables Node.js when it is available on PATH. Errors
from the most recent failed download are shown in the sync window.

`python main.py --update` explicitly checks the upstream repository and backs
up files before updating. Use it only when intending to replace local source
with the upstream version.

## Validation of this revision

Source syntax and patch formatting were checked without starting playback,
opening browsers, installing dependencies or running application tests.
Live audio/device behavior, Google sign-in and GTA frame-time impact still need
verification on the user's machine when gaming is finished.
