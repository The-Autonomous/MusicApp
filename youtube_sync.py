"""A small sign-in flow, protected session storage and background likes sync."""
import ctypes
import json
import os
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from math import isfinite
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic, time

from log_loader import log_loader
from runtime_policy import run_job, YouTubeJobError
from youtube_library import (LocalSongIndex, eligible_tracks, permanent_download_directory,
                             group_tracks, valid_playlist_url, audio_file_exists)
from download_policy import download_budget

APP_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")) / "MusicApp"
SESSION_FILE = DATA_DIR / "youtube_session.bin"
SETTINGS_FILE = APP_DIR / ".youtube_sync.json"
LIKES_FILE = APP_DIR / ".youtube_likes.json"
DOWNLOADS_FILE = APP_DIR / ".youtube_downloads.json"
DEFERRED_FILE = APP_DIR / ".youtube_deferred_downloads.json"
DOWNLOAD_COOLDOWN = 30 * 24 * 60 * 60
ll = log_loader("YouTube", debugging=False)


def atomic_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    os.replace(temporary, path)


def _crypt(data, decrypt=False):
    if os.name != "nt":
        raise RuntimeError("Remembering this login requires Windows.")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
    free.argtypes, free.restype = [ctypes.c_void_p], ctypes.c_void_p
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        free(ctypes.cast(result.data, ctypes.c_void_p))


class YouTubeSync:
    SOURCES = {"YouTube Music liked songs": "https://music.youtube.com/playlist?list=LM",
               "YouTube liked videos": "https://www.youtube.com/playlist?list=LL"}

    def __init__(self, player):
        self.player = player
        self.cancel = Event()
        self._lock = Lock()
        self._thread = None
        self._busy = Event()
        self._observer = None
        self._progress_lock = Lock()
        self._download_progress = {}
        self._cookies = None
        self._closed = False
        self.connected = False
        self.ready = False
        self.total = None
        self.saved = 0
        self.deferred = 0
        self.skipped = 0
        self.session_remembered = True
        self.phase = "idle"
        self.message = "Connect YouTube to start listening."
        self.settings = self._read(SETTINGS_FILE, {})
        self.settings.setdefault("source", "YouTube Music liked songs")
        player.youtube_sync = self
        player.auto_gaming = self.settings.get("auto_gaming", True)
        player.ytHandle.configure(cookies_provider=self.cookies, track_callback=player.add_downloaded_song,
                                  batch_callback=lambda urls: self.start(self._observer or (lambda _: None),
                                                                         playlist_urls=urls,
                                                                         on_ready=player.allow_playback))

    @staticmethod
    def _read(path, default):
        try:
            with path.open(encoding="utf-8") as handle:
                result = json.load(handle)
                return result if isinstance(result, dict) else default.copy()
        except (OSError, ValueError):
            return default.copy()

    def cookies(self):
        with self._lock:
            if self._cookies is None:
                try:
                    saved = json.loads(_crypt(SESSION_FILE.read_bytes(), decrypt=True))
                    self._cookies = ([cookie for cookie in saved if isinstance(cookie, dict)
                                      and all(key in cookie for key in ("name", "value", "domain"))]
                                     if isinstance(saved, list) else [])
                except (OSError, ValueError, RuntimeError):
                    # A moved/corrupted session needs a fresh sign-in, not a startup crash.
                    self._cookies = []
            return [dict(cookie) for cookie in self._cookies]

    def save_cookies(self, cookies):
        if not cookies or not any(c.get("name") in {"SAPISID", "__Secure-3PAPISID", "__Secure-1PAPISID"}
                                  for c in cookies):
            raise YouTubeJobError("YouTube didn't finish signing in. Please retry.", "signin_failed")
        with self._lock:
            if self.cancel.is_set() or self._closed:
                raise RuntimeError("Cancelled")
            self._cookies = cookies
            try:
                encrypted = _crypt(json.dumps(cookies).encode("utf-8"))
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                temporary = SESSION_FILE.with_suffix(".tmp")
                temporary.write_bytes(encrypted)
                os.replace(temporary, SESSION_FILE)
                return True
            except (OSError, RuntimeError):
                # Keep the working session in memory; never fall back to plaintext storage.
                ll.warn("Windows couldn't remember the login; this session can still be used.")
                return False

    def busy(self):
        return self._busy.is_set()

    @staticmethod
    def _needs_login(error):
        message = str(error).lower()
        return any(word in message for word in ("sign in", "signed in", "logged in", "login cookie",
                                                "authorization", "authentication", "401", "403", "unauthorized"))

    @staticmethod
    def _friendly_error(error):
        code = getattr(error, "code", "operation_failed")
        if code != "operation_failed":
            return str(error)
        if YouTubeSync._needs_login(error):
            return "YouTube couldn't confirm this login. Retry to sign in again."
        if "timed out" in str(error).lower() or "timeout" in str(error).lower():
            return "YouTube took too long to respond. Check your connection and retry."
        return "YouTube couldn't connect. Check your connection and retry."

    def start(self, status, source=None, login=False, browser=None, sync=True,
              on_connected=None, on_ready=None, on_failed=None, on_finished=None, playlist_urls=None,
              on_authentication=None):
        with self._lock:
            if self.busy() or self._closed:
                return False
            self.cancel.clear()
            self._busy.set()
        self.ready = False
        self.total, self.saved = None, 0
        self.deferred = 0
        self.skipped = 0
        with self._progress_lock:
            self._download_progress.clear()
        if login:
            self.connected = False
        source = source or self.settings.get("source")
        if source not in self.SOURCES:
            source = "YouTube Music liked songs"

        def update(message, phase=None):
            if self._closed:
                return
            if phase:
                self.phase = phase
            self.message = message
            display = self._status_text()
            status(display)
            observer = self._observer
            if observer is not None and observer is not status:
                observer(display)

        def fetch(cookies):
            update("Checking your YouTube likes…", "connecting")
            with self.player.ytHandle._metadata_lock:
                return request({"action": "music_likes" if source == "YouTube Music liked songs" else "playlist",
                                "url": self.SOURCES[source], "cookies": cookies}, timeout=600)

        repaired = False

        def request(job, timeout):
            nonlocal repaired
            try:
                return run_job(job, self.cancel, timeout=timeout)
            except YouTubeJobError as exc:
                if exc.code != "missing_dependency" or repaired or self.cancel.is_set():
                    raise
                repaired = True
                update("Repairing YouTube support…", "connecting")
                run_job({"action": "setup", "music": source == "YouTube Music liked songs"},
                        self.cancel, timeout=270)
                return run_job(job, self.cancel, timeout=timeout)

        def work():
            try:
                update("Preparing YouTube…", "connecting")
                request({"action": "prepare", "music": source == "YouTube Music liked songs"}, timeout=270)
                cookies = [] if login else self.cookies()
                tracks = None
                remembered = self.session_remembered if cookies else True
                if cookies:
                    try:
                        tracks = fetch(cookies)
                    except RuntimeError as exc:
                        if self.cancel.is_set() or not self._needs_login(exc):
                            raise
                        cookies = []
                if not cookies:
                    update("Sign in to YouTube in your browser. Music will wait.", "browser")
                    authentication = on_authentication
                    overlay = getattr(self, "_authentication_overlay", None)
                    if authentication is None and overlay is not None:
                        authentication = lambda active: self._authentication_visibility(overlay, active)
                    try:
                        if authentication:
                            authentication(True)
                        if self.cancel.is_set() or self._closed:
                            raise RuntimeError("Cancelled")
                        cookies = request({"action": "login"}, timeout=360)
                    finally:
                        if authentication:
                            authentication(False)
                    remembered = self.save_cookies(cookies)
                    tracks = fetch(cookies)
                if self._closed:
                    return
                if self.cancel.is_set():
                    raise RuntimeError("Cancelled")
                self.connected = True
                self.session_remembered = remembered
                update("YouTube connected. Preparing your music…", "connecting")
                if on_connected:
                    on_connected()
                previous = self._read(LIKES_FILE, {})
                sources = previous.get("sources", {})
                if not isinstance(sources, dict):
                    sources = {}
                sources[source] = {"fetched_at": time(), "tracks": tracks}
                combined = list(tracks)
                if sync:
                    urls = list(dict.fromkeys([*self.player._playlist_urls, *(playlist_urls or [])]))
                    for number, url in enumerate(urls, 1):
                        if self.cancel.is_set():
                            raise RuntimeError("Cancelled")
                        if not valid_playlist_url(url):
                            self.skipped += 1
                            continue
                        update(f"Checking playlist {number} of {len(urls)}…", "connecting")
                        with self.player.ytHandle._metadata_lock:
                            entries = request({"action": "playlist", "url": url, "cookies": cookies}, timeout=600)
                        sources[url] = {"fetched_at": time(), "tracks": entries}
                        combined.extend(entries)
                atomic_json(LIKES_FILE, {"sources": sources})
                downloaded = self._read(DOWNLOADS_FILE, previous.get("downloaded", {}))
                if self.cancel.is_set():
                    raise RuntimeError("Cancelled")
                if not sync:
                    update("YouTube connected.", "connected")
                    return
                directory = permanent_download_directory(self.player._directories)
                directory.mkdir(parents=True, exist_ok=True)
                completed = self._download_all(combined, cookies, directory, downloaded, update)
                if self.cancel.is_set():
                    update("Sync stopped. Your saved songs are kept. Resume setup to start listening.", "error")
                else:
                    if self.cancel.is_set() or self._closed:
                        raise RuntimeError("Cancelled")
                    self.ready = True
                    update(f"{self.saved} songs ready · {completed} new songs saved"
                           + (f" · {self.skipped} invalid or unavailable skipped" if self.skipped else "")
                           + (f" · {self.deferred} deferred for up to 30 days" if self.deferred else ""), "connected")
                    if on_ready:
                        on_ready()
            except Exception as exc:
                if not self._closed:
                    if self.cancel.is_set():
                        message = "Sync stopped. Your music is kept." if self.connected else "Sign-in stopped. Music is still paused."
                    elif self.connected and isinstance(exc, OSError):
                        message = "Windows couldn't save the music setup. Retry to finish before playback starts."
                    else:
                        message = self._friendly_error(exc)
                    update(message, "error")
                    ll.warn(f"Connection failed ({getattr(exc, 'code', type(exc).__name__)}): {message}")
                    if on_failed:
                        on_failed(message)
            finally:
                self._busy.clear()
                observer = self._observer
                if observer is not None and not self._closed:
                    observer(self._status_text())
                if on_finished and not self._closed:
                    on_finished()

        self._thread = Thread(target=work, daemon=True, name="youtube-sync")
        try:
            self._thread.start()
        except RuntimeError:
            self._busy.clear()
            raise
        return True

    def download_progress(self):
        with self._progress_lock:
            return [dict(report) for report in self._download_progress.values()]

    def _download_all(self, tracks, cookies, directory, downloaded, update):
        """One owner of the library index, retries, checkpoints and combined queue."""
        dirty = False
        completed = 0
        saved_at = monotonic()
        deferred = {identifier: record for identifier, record in self._read(DEFERRED_FILE, {}).items()
                    if isinstance(record, dict)
                    and isinstance(record.get("retry_after"), (int, float))
                    and isfinite(record["retry_after"]) and record["retry_after"] > time()}
        update("Checking songs already in your library…", "syncing")
        eligible, skipped = eligible_tracks(tracks)
        self.skipped += skipped
        groups = group_tracks(eligible)
        library = LocalSongIndex()
        for song in self.player.local_songs_for_sync(self.cancel):
            library.add(song)
        queue = deque()
        self.total, self.saved = len(groups), 0

        def reuse(group):
            return next((path for entry in group
                         if (path := library.find(entry, downloaded, directory))), None)

        def remember(group, path):
            nonlocal dirty
            for entry in group:
                if downloaded.get(entry["id"]) != path:
                    downloaded[entry["id"]] = path
                    dirty = True
                library.add(dict(entry, path=path))
                deferred.pop(entry["id"], None)

        def report(entry, value):
            with self._progress_lock:
                self._download_progress[entry["id"]] = dict(value, id=entry["id"], title=entry.get("title", "Untitled"))

        def download(entry):
            return self.player.ytHandle._download(
                f"https://www.youtube.com/watch?v={entry['id']}", directory,
                cookies=cookies, cancel=self.cancel, sponsorblock=True,
                progress=lambda value: report(entry, value))

        def show_progress(budget):
            reports = self.download_progress()
            lines = [f"{self.saved} of {self.total} songs ready · likes and playlists",
                     f"{budget.label} · up to {budget.workers} at a time"]
            if len(reports) > budget.workers:
                lines[1] = f"{budget.label} · finishing {len(reports)} songs; next limit {budget.workers}"
            if getattr(getattr(self, "loading_helper", None), "collapsed", False):
                for item in reports:
                    phase = item.get("phase")
                    fraction = item.get("progress")
                    detail = ("Removing non-music / converting" if phase == "processing" else
                              f"{round(fraction * 100)}%" if isinstance(fraction, (int, float)) else "Preparing")
                    lines.append(f"{item['title'][:75]} · {detail}")
            update("\n".join(lines), "syncing")

        active = {}
        try:
            for group in groups:
                if self.cancel.is_set():
                    raise RuntimeError("Cancelled")
                path = reuse(group)
                if path:
                    remember(group, path)
                    self.player.add_downloaded_song(path)
                    self.saved += 1
                else:
                    available = [entry for entry in group if entry["id"] not in deferred]
                    if available:
                        queue.append((group, available, 1))
                    else:
                        self.deferred += 1
                        self.total -= 1
            # Persist recovered records even if a later source/download fails.
            if dirty:
                atomic_json(DOWNLOADS_FILE, downloaded)
                dirty = False
            with ThreadPoolExecutor(max_workers=3, thread_name_prefix="youtube-download") as executor:
                last_display = None
                while queue or active:
                    budget = download_budget()
                    if self.cancel.is_set():
                        queue.clear()
                    while queue and not self.cancel.is_set() and len(active) < budget.workers:
                        group, available, attempt = queue.popleft()
                        path = reuse(group)
                        if path:
                            remember(group, path)
                            self.saved += 1
                            continue
                        entry = available[(attempt - 1) % len(available)]
                        future = executor.submit(download, entry)
                        active[future] = (group, available, entry, attempt)
                    signature = (self.saved, self.total, budget.label, budget.workers,
                                 getattr(getattr(self, "loading_helper", None), "collapsed", False),
                                 tuple((r['id'], r.get('phase'), r.get('progress'), r.get('processor'))
                                       for r in self.download_progress()))
                    if signature != last_display:
                        show_progress(budget)
                        last_display = signature
                    if not active:
                        continue
                    finished, _ = wait(active, timeout=0.25, return_when=FIRST_COMPLETED)
                    for future in finished:
                        group, available, entry, attempt = active.pop(future)
                        with self._progress_lock:
                            self._download_progress.pop(entry["id"], None)
                        try:
                            path = future.result()
                            if not audio_file_exists(path):
                                raise RuntimeError("Downloaded audio file was not found")
                        except (RuntimeError, OSError) as exc:
                            if self.cancel.is_set():
                                continue
                            ll.warn(f"Download attempt {attempt} failed for {entry['id']}: {exc}")
                            if attempt == 1:
                                queue.append((group, available, 2))
                            else:
                                for alias in group:
                                    deferred[alias["id"]] = {"title": alias.get("title", "Untitled"),
                                                             "retry_after": time() + DOWNLOAD_COOLDOWN}
                                atomic_json(DEFERRED_FILE, deferred)
                                self.deferred += 1
                                self.total -= 1
                            continue
                        remember(group, path)
                        metadata = self.player.get_metadata(path)
                        library.add(dict(metadata, path=path))
                        completed += 1
                        self.saved += 1
                        if completed % 5 == 0 or monotonic() - saved_at >= 30:
                            atomic_json(DOWNLOADS_FILE, downloaded)
                            dirty = False
                            saved_at = monotonic()
            if DEFERRED_FILE.exists() or deferred:
                atomic_json(DEFERRED_FILE, deferred)
            if dirty:
                atomic_json(DOWNLOADS_FILE, downloaded)
                dirty = False
            return completed
        finally:
            # The executor waits for all owned jobs before any final checkpoint.
            with self._progress_lock:
                self._download_progress.clear()
            if dirty:
                try:
                    atomic_json(DOWNLOADS_FILE, downloaded)
                except OSError:
                    ll.warn("Couldn't save the latest sync progress.")

    def _status_text(self):
        return self.message + ("\nSign in next time: Windows couldn't remember this login."
                               if self.connected and not self.session_remembered else "")

    def _authentication_visibility(self, overlay, active):
        """Temporarily withdraw this app's windows without changing preferences."""
        finished = Event()
        prepared = [False]
        failures = []

        def update():
            try:
                if getattr(overlay, "_closing", False):
                    return
                if active:
                    if not getattr(overlay, "_authentication_active", False):
                        overlay._authentication_active = True
                        saved = []
                        pending = [overlay.root]
                        while pending:
                            window = pending.pop()
                            pending.extend(window.winfo_children())
                            if window.winfo_toplevel() is not window:
                                continue
                            state = window.state()
                            if state != "withdrawn":
                                saved.append((window, state))
                                window.withdraw()
                        self._authentication_windows = saved
                        helper = getattr(self, "loading_helper", None)
                        if helper is not None:
                            helper.suspend()
                else:
                    overlay._authentication_active = False
                    for window, state in getattr(self, "_authentication_windows", []):
                        try:
                            # The player has separate readiness and user-intent
                            # gates; restoring it directly could bypass them.
                            if window is not getattr(overlay, "window", None) and window.winfo_exists():
                                window.state(state)
                        except Exception:
                            pass
                    self._authentication_windows = []
                    helper = getattr(self, "loading_helper", None)
                    if helper is not None:
                        helper.resume()
                refresh = getattr(overlay, "refresh_overlay_visibility", None)
                if refresh:
                    refresh()
                prepared[0] = True
            except Exception as exc:
                failures.append(exc)
            finally:
                finished.set()

        overlay.post_ui(update)
        # Acknowledge the actual UI-thread hide before starting the browser.
        # Never launch behind stale topmost app windows if the UI has stopped.
        if active:
            deadline = monotonic() + 5
            while not finished.wait(0.1):
                if self.cancel.is_set() or self._closed:
                    raise RuntimeError("Cancelled")
                if monotonic() >= deadline:
                    raise RuntimeError("MusicApp couldn't prepare the sign-in window. Retry setup.")
            if not prepared[0] or failures:
                raise RuntimeError("MusicApp couldn't prepare the sign-in window. Retry setup.")

    def forget_login(self):
        self.cancel.set()
        with self._lock:
            self._cookies = []
            try:
                SESSION_FILE.unlink(missing_ok=True)
            except OSError:
                ll.warn("Couldn't remove the old saved login.")
        self.connected = False

    def close(self):
        self._closed = True
        self._observer = None
        self.cancel.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def toggle(self, overlay):
        """Hide only the card; downloads and playback gating keep running."""
        if getattr(overlay, "_authentication_active", False):
            return
        existing = getattr(self, "window", None)
        if existing is not None and existing.winfo_exists() and existing.state() != "withdrawn":
            options = getattr(self, "options_window", None)
            if options is not None and options.winfo_exists():
                options.destroy()
            existing.withdraw()
        else:
            self.show(overlay)

    def show(self, overlay, startup=False):
        import tkinter as tk
        self._authentication_overlay = overlay
        if getattr(overlay, "_authentication_active", False):
            return
        startup = startup or not self.player._playback_ready.is_set()
        existing = getattr(self, "window", None)
        if existing is not None and existing.winfo_exists():
            self.loading_helper.restore()
            existing.deiconify()
            existing.lift()
            return
        colors = overlay.theme.COLORS
        win = self.window = tk.Toplevel(overlay.root)
        win.title("MusicApp · YouTube")
        win.configure(bg=colors["window_bg"])
        win.resizable(False, False)
        win.geometry("440x245")
        frame = tk.Frame(win, bg=colors["window_bg"], padx=26, pady=23)
        frame.pack(fill="both", expand=True)
        heading = tk.Label(frame, text="Connect YouTube", font=("Segoe UI", 18, "bold"),
                           fg=colors["accent"], bg=colors["window_bg"], anchor="w")
        heading.pack(fill="x")
        from ui_window import style_card
        style_card(win, heading)
        message = tk.StringVar(value=self.message)
        tk.Label(frame, textvariable=message, font=("Segoe UI", 11), justify="left",
                 wraplength=382, fg=colors["text"], bg=colors["window_bg"]).pack(fill="x", pady=(14, 20))
        buttons = tk.Frame(frame, bg=colors["window_bg"])
        buttons.pack(side="bottom", fill="x")
        from ui_loading import LoadingHelper
        helper = self.loading_helper = LoadingHelper(win, frame, heading, message, colors)

        def button(text, command, primary=False):
            return tk.Button(buttons, text=text, command=command, font=("Segoe UI", 10),
                             bg=colors["accent"] if primary else colors["frame_bg"],
                             fg=colors["window_bg"] if primary else colors["text"],
                             activebackground=colors["accent_active"], relief="flat", bd=0,
                             padx=13, pady=8, cursor="hand2", takefocus=True)

        def alive():
            try:
                return not overlay._closing and win.winfo_exists()
            except tk.TclError:
                return False

        def status(text):
            def update():
                if alive():
                    message.set(text)
                    render()
            overlay.post_ui(update)

        self._observer = status
        rendered = None

        def render():
            nonlocal rendered
            if not alive():
                return
            message.set(self._status_text())
            helper.set_progress(self.saved, self.total, self.busy())
            helper.set_downloads(self.download_progress())
            signature = (self.phase == "error", self.busy(), self.connected, self.ready)
            if signature == rendered:
                win.update_idletasks()
                if not helper.collapsed:
                    win.geometry(f"{max(440, frame.winfo_reqwidth())}x{max(245, frame.winfo_reqheight())}")
                return
            rendered = signature
            for widget in buttons.winfo_children():
                widget.destroy()
            if self.phase == "error":
                heading.config(text="Setup needs attention" if startup else "Couldn't finish")
                retry = button("Retry setup" if startup else "Retry", lambda: begin(not self.connected), True)
                retry.pack(side="left")
                if self.busy():
                    retry.config(state="disabled")
                button("Quit" if startup else "Close", overlay.root.quit if startup else win.destroy).pack(side="right")
            elif self.busy() and not self.connected:
                heading.config(text="Preparing your music")
                button("Cancel", self.cancel.set).pack(side="right")
            else:
                heading.config(text="Downloading your music" if self.busy() else
                               "Your music is ready" if self.ready else "Connect YouTube")
                if self.busy():
                    button("Stop sync", self.cancel.set).pack(side="left")
                else:
                    button("Sync music" if self.connected else "Sign in", lambda: begin(not self.connected), True).pack(side="left")
                button("Options", lambda: self._show_options(overlay, win)).pack(side="left", padx=8)
                button("Minimize" if startup and not self.ready else "Close",
                       helper.collapse if startup and not self.ready else win.destroy).pack(side="right")
            # Let larger Windows text settings and longer notices fit the card.
            win.update_idletasks()
            if not helper.collapsed:
                win.geometry(f"{max(440, frame.winfo_reqwidth())}x{max(245, frame.winfo_reqheight())}")

        def ready():
            self.player.allow_playback()
            def update():
                render()
                # Leave a notice visible if Windows couldn't remember the session.
                if startup and alive() and self.session_remembered:
                    win.after(1200, lambda: win.destroy() if alive() and self.ready else None)
            overlay.post_ui(update)

        def begin(reconnect=False):
            if self.start(status, login=reconnect, on_ready=ready,
                          on_failed=lambda _: overlay.post_ui(render),
                          on_finished=lambda: overlay.post_ui(render)):
                render()

        def close_window():
            if startup and not self.player._playback_ready.is_set():
                helper.collapse()
            else:
                win.destroy()

        win.protocol("WM_DELETE_WINDOW", close_window)
        def detached(event):
            if event.widget is win and self._observer is status:
                self._observer = None
        win.bind("<Destroy>", detached, add="+")
        render()
        if startup:
            win.after(0, begin)

    def _show_options(self, overlay, parent):
        import tkinter as tk
        from tkinter import ttk
        if getattr(overlay, "_authentication_active", False):
            return
        existing = getattr(self, "options_window", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return
        win = self.options_window = tk.Toplevel(parent)
        win.title("YouTube options")
        win.configure(bg=overlay.theme.COLORS["window_bg"])
        win.resizable(False, False)
        frame = ttk.Frame(win, padding=20)
        frame.pack(fill="both", expand=True)
        source = tk.StringVar(value=self.settings.get("source"))
        heading = ttk.Label(frame, text="Sync likes from")
        heading.pack(anchor="w")
        from ui_window import style_card
        style_card(win, heading)
        win.bind("<Escape>", lambda event: win.destroy())
        selector = ttk.Combobox(frame, textvariable=source, values=list(self.SOURCES), state="readonly", width=32)
        selector.pack(fill="x", pady=8)
        if self.busy():
            selector.config(state="disabled")
        hosting = tk.BooleanVar(value=self.settings.get("host_radio", False))
        gaming = tk.BooleanVar(value=self.player.auto_gaming)

        def save():
            self.settings.update(source=source.get(), host_radio=hosting.get(), auto_gaming=gaming.get())
            self.player.auto_gaming = gaming.get()
            self.player.set_hosting(hosting.get())
            try:
                atomic_json(SETTINGS_FILE, self.settings)
            except OSError:
                ll.warn("Couldn't save YouTube options.")

        selector.bind("<<ComboboxSelected>>", lambda _: save())
        ttk.Checkbutton(frame, text="Phone controls / radio hosting", variable=hosting, command=save).pack(anchor="w", pady=6)
        ttk.Checkbutton(frame, text="Reduce effects while GTA is running", variable=gaming, command=save).pack(anchor="w", pady=6)
        ttk.Button(frame, text="Sync Playlists.txt", command=self.player.sync_playlists).pack(fill="x", pady=8)
        ttk.Button(frame, text="Forget YouTube login", command=lambda: (self.forget_login(), win.destroy())).pack(fill="x", pady=4)
        ttk.Button(frame, text="Done", command=win.destroy).pack(anchor="e", pady=(10, 0))
