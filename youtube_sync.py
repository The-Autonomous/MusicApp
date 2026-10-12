"""Local YouTube session storage and incremental liked-song synchronization."""
import ctypes
import json
import os
from pathlib import Path
from threading import Event, Lock, Thread
from time import time

from runtime_policy import run_job, game_running

APP_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")) / "MusicApp"
SESSION_FILE = DATA_DIR / "youtube_session.bin"
SETTINGS_FILE = APP_DIR / ".youtube_sync.json"
LIKES_FILE = APP_DIR / ".youtube_likes.json"
DOWNLOADS_FILE = APP_DIR / ".youtube_downloads.json"


def atomic_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    os.replace(temporary, path)


def _crypt(data, decrypt=False):
    if os.name != "nt":
        raise RuntimeError("Saved YouTube login currently requires Windows account encryption")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    function = (ctypes.windll.crypt32.CryptUnprotectData if decrypt
                else ctypes.windll.crypt32.CryptProtectData)
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


class YouTubeSync:
    SOURCES = {"YouTube Music liked songs": "https://music.youtube.com/playlist?list=LM",
               "YouTube liked videos": "https://www.youtube.com/playlist?list=LL"}

    def __init__(self, player):
        self.player = player
        self.cancel = Event()
        self._lock = Lock()
        self._thread = None
        self._cookies = None
        self.settings = self._read(SETTINGS_FILE, {"auto_sync": False,
                              "source": "YouTube Music liked songs", "host_radio": False})
        player.auto_gaming = self.settings.get("auto_gaming", True)
        player.ytHandle.configure(cookies_provider=self.cookies, track_callback=player.add_downloaded_song)

    @staticmethod
    def _read(path, default):
        try:
            with path.open(encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return default.copy()

    def cookies(self):
        with self._lock:
            if self._cookies is None:
                try:
                    self._cookies = json.loads(_crypt(SESSION_FILE.read_bytes(), decrypt=True))
                except (OSError, ValueError, RuntimeError):
                    self._cookies = []
            return [dict(cookie) for cookie in self._cookies]

    def save_cookies(self, cookies, cancel=None):
        if not cookies or not any(c.get("name") in {"SAPISID", "__Secure-3PAPISID", "__Secure-1PAPISID"}
                                  for c in cookies):
            raise RuntimeError("No signed-in YouTube session found. Sign in to YouTube in the selected browser.")
        encrypted = _crypt(json.dumps(cookies).encode("utf-8"))
        with self._lock:
            if cancel is not None and cancel.is_set():
                raise RuntimeError("Cancelled")
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            temporary = SESSION_FILE.with_suffix(".tmp")
            temporary.write_bytes(encrypted)
            os.replace(temporary, SESSION_FILE)
            self._cookies = cookies

    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, status, source=None, login=False, browser=None, sync=True):
        if self.busy():
            return False
        self.cancel.clear()
        source = source or self.settings.get("source", "YouTube Music liked songs")

        def work():
            try:
                if login or browser:
                    status("Sign in in the browser window, then return here." if login
                           else "Reading your selected browser's YouTube login…")
                    cookies = run_job({"action": "login" if login else "browser_cookies",
                                       "browser": browser}, self.cancel, timeout=360)
                    if self.cancel.is_set():
                        return
                    self.save_cookies(cookies, self.cancel)
                if not sync:
                    status("YouTube login saved on this Windows account.")
                    return
                cookies = self.cookies()
                if not cookies:
                    raise RuntimeError("Connect your YouTube login first.")
                status("Fetching all available liked songs…")
                # Use the same metadata lock as searches, but this operation has its own cancellation.
                with self.player.ytHandle._metadata_lock:
                    tracks = run_job({"action": "music_likes" if source == "YouTube Music liked songs" else "playlist",
                                      "url": self.SOURCES[source],
                                      "cookies": cookies}, self.cancel, timeout=300)
                previous = self._read(LIKES_FILE, {"downloaded": {}, "sources": {}})
                downloaded = self._read(DOWNLOADS_FILE, previous.get("downloaded", {}))
                sources = previous.get("sources", {})
                sources[source] = {"fetched_at": time(), "tracks": tracks}
                manifest = {"sources": sources}
                atomic_json(LIKES_FILE, manifest)
                atomic_json(DOWNLOADS_FILE, downloaded)
                directory = Path.home() / "MusicPlayerYoutubeDownloads"
                directory.mkdir(parents=True, exist_ok=True)
                completed = failed = 0
                last_error = ''
                missing = [entry for entry in tracks
                           if not Path(downloaded.get(entry["id"], "")).is_file()]
                for number, entry in enumerate(missing, 1):
                    if self.cancel.is_set():
                        break
                    status(f"Downloading {number}/{len(missing)}: {entry['title']}"
                           + (" · GTA mode: one low-priority download" if game_running() else ""))
                    try:
                        path = self.player.ytHandle._download(
                            f"https://www.youtube.com/watch?v={entry['id']}", directory,
                            cookies=cookies, cancel=self.cancel)
                        if path:
                            downloaded[entry["id"]] = path
                            completed += 1
                            # Do not serialize the entire liked-song list after every download.
                            atomic_json(DOWNLOADS_FILE, downloaded)
                    except RuntimeError as exc:
                        if self.cancel.is_set():
                            break
                        failed += 1
                        last_error = str(exc)[:350]
                if self.cancel.is_set():
                    status("Sync stopped. Completed downloads are saved for the next sync.")
                else:
                    status(f"Sync complete: {len(tracks)} likes, {completed} new downloads, {failed} unavailable."
                           + (f" Last error: {last_error}" if last_error else '')
                           if tracks else "No songs were returned for this account and source.")
            except Exception as exc:
                status(f"YouTube sync: {exc}")

        self._thread = Thread(target=work, daemon=True, name="youtube-sync")
        self._thread.start()
        return True

    def forget_login(self):
        self.cancel.set()
        with self._lock:
            self._cookies = []
            SESSION_FILE.unlink(missing_ok=True)

    def install_support(self, status):
        if self.busy():
            status('Stop the current sync before updating YouTube support.')
            return
        self.cancel.clear()

        def work():
            status('Installing / updating YouTube support at low priority…')
            try:
                status(run_job({'action': 'setup'}, self.cancel, timeout=600))
            except Exception as exc:
                status(f'YouTube setup: {exc}')

        self._thread = Thread(target=work, daemon=True, name='youtube-setup')
        self._thread.start()

    def close(self):
        self.cancel.set()

    def show(self, overlay, startup=False):
        # This method is always called by the main Tk thread.
        import tkinter as tk
        from tkinter import ttk
        existing = getattr(self, "window", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return
        win = self.window = tk.Toplevel(overlay.root)
        win.title("YouTube Sync")
        win.configure(bg=overlay.theme.COLORS["window_bg"])
        win.geometry("680x600")
        frame = ttk.Frame(win, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Your liked songs", style="Header.TLabel").pack(anchor="w")
        ttk.Label(frame, text="Connect YouTube once. Your saved login stays on this Windows account.\n"
                  "Local music keeps playing; downloads run one at a time.", wraplength=560).pack(anchor="w", pady=10)
        source = tk.StringVar(value=self.settings.get("source", "YouTube Music liked songs"))
        ttk.Combobox(frame, textvariable=source, values=list(self.SOURCES), state="readonly").pack(fill="x")
        automatic = tk.BooleanVar(value=self.settings.get("auto_sync", False))
        message = tk.StringVar(value="Saved YouTube login available." if self.cookies()
                               else "Connect a login to sync your liked songs.")

        def save_settings():
            self.settings.update(auto_sync=automatic.get(), source=source.get())
            atomic_json(SETTINGS_FILE, self.settings)

        def status(text):
            def update():
                if win.winfo_exists():
                    message.set(text)
            overlay.post_ui(update)

        def start(login=False, browser=None):
            save_settings()
            if not self.start(status, source.get(), login=login, browser=browser):
                message.set("A sync is already running. Stop it before reconnecting.")

        ttk.Checkbutton(frame, text="Sync automatically on startup", variable=automatic,
                        command=save_settings).pack(anchor="w", pady=8)
        hosting = tk.BooleanVar(value=self.settings.get("host_radio", False))

        def change_hosting():
            self.settings["host_radio"] = hosting.get()
            save_settings()
            self.player.set_hosting(hosting.get())

        ttk.Checkbutton(frame, text="Enable radio hosting / phone controls", variable=hosting,
                        command=change_hosting).pack(anchor="w")
        gaming = tk.BooleanVar(value=self.player.auto_gaming)

        def change_gaming():
            self.player.auto_gaming = gaming.get()
            self.settings["auto_gaming"] = gaming.get()
            save_settings()

        ttk.Checkbutton(frame, text="Automatically bypass audio effects while GTA is running",
                        variable=gaming, command=change_gaming).pack(anchor="w")
        ttk.Button(frame, text="Sync playlists from Playlists.txt",
                   command=self.player.sync_playlists).pack(anchor="w", pady=5)
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=8)
        ttk.Button(row, text="Connect YouTube", command=lambda: start(login=True)).pack(side="left", padx=3)
        ttk.Button(row, text="Sync saved login", command=start).pack(side="left", padx=3)
        ttk.Button(row, text="Stop sync", command=self.cancel.set).pack(side="left", padx=3)
        row = ttk.Frame(frame)
        row.pack(fill="x")
        browser = tk.StringVar(value="edge")
        ttk.Combobox(row, textvariable=browser, values=["edge", "chrome", "firefox"],
                     state="readonly", width=10).pack(side="left", padx=3)
        ttk.Button(row, text="Use existing browser login", command=lambda: start(browser=browser.get())).pack(side="left", padx=3)

        def forget():
            self.forget_login()
            automatic.set(False)
            save_settings()
            message.set("Saved login removed. Downloaded music is kept.")

        ttk.Button(row, text="Forget login", command=forget).pack(side="left", padx=3)

        def import_cookies():
            if self.busy():
                message.set("Stop the current sync before importing a login.")
                return
            from tkinter import filedialog
            from http.cookiejar import MozillaCookieJar
            filename = filedialog.askopenfilename(parent=win, title="Import YouTube cookies",
                                                  filetypes=[("Cookie files", "*.txt"), ("All files", "*.*")])
            if not filename:
                return
            try:
                jar = MozillaCookieJar(filename)
                jar.load(ignore_discard=True, ignore_expires=False)
                cookies = [{"name": c.name, "value": c.value, "domain": c.domain,
                            "path": c.path, "expires": c.expires or -1,
                            "secure": c.secure, "httpOnly": True}
                           for c in jar if c.domain.lstrip('.') == 'youtube.com'
                           or c.domain.endswith('.youtube.com')]
                self.save_cookies(cookies)
                message.set("Imported login saved. Choose Sync saved login to continue.")
            except Exception as exc:
                message.set(f"Cookie import: {exc}")

        ttk.Button(frame, text="Import exported YouTube cookies", command=import_cookies).pack(anchor="w", pady=5)
        ttk.Button(frame, text="Install / update YouTube support",
                   command=lambda: self.install_support(status)).pack(anchor="w", pady=5)
        ttk.Label(frame, textvariable=message, wraplength=560, style="Status.TLabel").pack(anchor="w", pady=12)
        ttk.Button(frame, text="Keep playing / close", command=win.destroy).pack(anchor="e")
        if startup and automatic.get() and self.cookies():
            win.after(300, start)
