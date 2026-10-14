"""A small sign-in flow, protected session storage and background likes sync."""
import ctypes
import json
import os
from collections import deque
from math import isfinite
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic, time

from log_loader import log_loader
from runtime_policy import run_job, YouTubeJobError

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
        self._cookies = None
        self._closed = False
        self.connected = False
        self.ready = False
        self.total = None
        self.saved = 0
        self.deferred = 0
        self.session_remembered = True
        self.phase = "idle"
        self.message = "Connect YouTube to start listening."
        self.settings = self._read(SETTINGS_FILE, {})
        self.settings.setdefault("source", "YouTube Music liked songs")
        player.auto_gaming = self.settings.get("auto_gaming", True)
        player.ytHandle.configure(cookies_provider=self.cookies, track_callback=player.add_downloaded_song)

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
              on_connected=None, on_ready=None, on_failed=None, on_finished=None):
        with self._lock:
            if self.busy() or self._closed:
                return False
            self.cancel.clear()
            self._busy.set()
        self.ready = False
        self.total, self.saved = None, 0
        self.deferred = 0
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
            downloaded = None
            dirty = False
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
                    update("Sign in to YouTube in your default browser. Music will wait.", "connecting")
                    cookies = request({"action": "login"}, timeout=360)
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
                atomic_json(LIKES_FILE, {"sources": sources})
                downloaded = self._read(DOWNLOADS_FILE, previous.get("downloaded", {}))
                if self.cancel.is_set():
                    raise RuntimeError("Cancelled")
                if not sync:
                    update("YouTube connected.", "connected")
                    return
                directory = Path.home() / "MusicPlayerYoutubeDownloads"
                directory.mkdir(parents=True, exist_ok=True)
                # Re-read on every sync so deleting the file resets the cooldowns.
                deferred = {identifier: record for identifier, record in self._read(DEFERRED_FILE, {}).items()
                            if isinstance(record, dict)
                            and isinstance(record.get("retry_after"), (int, float))
                            and isfinite(record["retry_after"]) and record["retry_after"] > time()}
                download_tracks = list({entry["id"]: entry for entry in tracks}.values())
                missing = [entry for entry in download_tracks
                           if not Path(downloaded.get(entry["id"], "")).is_file()]
                self.deferred = sum(entry["id"] in deferred for entry in missing)
                self.total = len(download_tracks) - self.deferred
                self.saved = len(download_tracks) - len(missing)
                queue = deque((entry, 1) for entry in missing if entry["id"] not in deferred)
                completed = 0
                saved_at = monotonic()
                while queue:
                    if self.cancel.is_set():
                        break
                    entry, attempt = queue.popleft()
                    update(f"{self.saved} of {self.total} songs ready\n"
                           + ("Retrying " if attempt == 2 else "Downloading ")
                           + f"{entry.get('title', 'Untitled')}…", "syncing")
                    try:
                        path = self.player.ytHandle._download(
                            f"https://www.youtube.com/watch?v={entry['id']}", directory,
                            cookies=cookies, cancel=self.cancel)
                        if not path or not Path(path).is_file():
                            raise RuntimeError("Downloaded audio file was not found")
                    except (RuntimeError, OSError) as exc:
                        if self.cancel.is_set():
                            break
                        ll.warn(f"Download attempt {attempt} failed for {entry['id']}: {exc}")
                        if attempt == 1:
                            queue.append((entry, 2))
                        else:
                            deferred[entry["id"]] = {"title": entry.get("title", "Untitled"),
                                                       "retry_after": time() + DOWNLOAD_COOLDOWN}
                            atomic_json(DEFERRED_FILE, deferred)
                            self.deferred += 1
                            self.total -= 1
                        continue
                    downloaded[entry["id"]] = path
                    completed += 1
                    self.saved += 1
                    dirty = True
                    deferred.pop(entry["id"], None)
                    if completed % 5 == 0 or monotonic() - saved_at >= 30:
                        atomic_json(DOWNLOADS_FILE, downloaded)
                        dirty = False
                        saved_at = monotonic()
                if self.cancel.is_set():
                    update("Sync stopped. Your saved songs are kept. Resume setup to start listening.", "error")
                else:
                    if DEFERRED_FILE.exists() or deferred:
                        atomic_json(DEFERRED_FILE, deferred)
                    if dirty:
                        atomic_json(DOWNLOADS_FILE, downloaded)
                        dirty = False
                    if self.cancel.is_set() or self._closed:
                        raise RuntimeError("Cancelled")
                    self.ready = True
                    update(f"{self.saved} songs ready · {completed} new songs saved"
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
                if downloaded is not None and dirty:
                    try:
                        atomic_json(DOWNLOADS_FILE, downloaded)
                    except OSError:
                        ll.warn("Couldn't save the latest sync progress.")
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

    def _status_text(self):
        return self.message + ("\nSign in next time: Windows couldn't remember this login."
                               if self.connected and not self.session_remembered else "")

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

    def show(self, overlay, startup=False):
        import tkinter as tk
        startup = startup or not self.player._playback_ready.is_set()
        existing = getattr(self, "window", None)
        if existing is not None and existing.winfo_exists():
            self.loading_helper.restore()
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
            signature = (self.phase == "error", self.busy(), self.connected, self.ready)
            if signature == rendered:
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
                    button("Sync likes" if self.connected else "Sign in", lambda: begin(not self.connected), True).pack(side="left")
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
