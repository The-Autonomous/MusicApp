"""YouTube jobs share one bounded queue and run in a low-priority child process."""
import json
import os
from pathlib import Path
import re
from threading import Event, Lock
from time import monotonic
import tkinter as tk
from tkinter import ttk
from log_loader import log_loader
from runtime_policy import run_job, game_running

ll = log_loader("YouTube", debugging=False)

class DownloadPopup:
    def __init__(self):
        pass
    
    def popup_process(self, close_event, progress_value, current_song):
        """
        Display download progress popup.
        
        Args:
            close_event: multiprocessing.Event to signal closing
            progress_value: multiprocessing.Value (float) for progress 0.0-1.0
            current_song: SharedString instance with current song name
        """
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes('-topmost', True)
        root.configure(bg="#111")

        width, height = 360, 120
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        x = (screen_width - width) // 2
        y = (screen_height - height) // 2
        root.geometry(f"{width}x{height}+{x}+{y}")

        canvas = tk.Canvas(root, width=width, height=height, bg="#111", highlightthickness=0)
        canvas.pack(fill='both', expand=True)

        canvas.create_rectangle(5, 5, width-5, height-5, fill="#1c1c1c", outline="#333", width=2)

        frame = tk.Frame(canvas, bg="#1c1c1c")
        frame.place(relwidth=0.95, relheight=0.9, relx=0.025, rely=0.05)

        label = tk.Label(frame, text="Downloading Music...", fg="#00FFAA", bg="#1c1c1c", 
                        font=("Segoe UI", 12, "bold"))
        label.pack(pady=(10, 0))

        percent_label = tk.Label(frame, text="0% ~ Waiting...", fg="#AAAAAA", bg="#1c1c1c", 
                                font=("Segoe UI", 10))
        percent_label.pack()

        style = ttk.Style()
        style.theme_use('clam')
        style.configure("Custom.Horizontal.TProgressbar",
                        troughcolor="#2e2e2e",
                        bordercolor="#2e2e2e",
                        background="#00FFAA",
                        lightcolor="#00FFAA",
                        darkcolor="#00FFAA",
                        thickness=10)

        progress = ttk.Progressbar(frame, mode="determinate", length=280, 
                                  style="Custom.Horizontal.TProgressbar")
        progress.pack(pady=(5, 10))
        progress['maximum'] = 100

        def update_progress():
            if close_event.is_set():
                root.destroy()
            else:
                # Get progress value
                value = max(0, min(1, progress_value.value)) * 100
                progress['value'] = value
                
                # Get current song name from SharedString
                song_name = current_song.get()
                
                # Update label
                if song_name:
                    # Truncate song name if too long for display
                    display_name = song_name[:20] + "..." if len(song_name) > 20 else song_name
                    percent_label.config(text=f"{int(value)}% ~ {display_name}")
                else:
                    percent_label.config(text=f"{int(value)}% ~ Waiting...")
                
                root.after(100, update_progress)

        # Start update loop
        root.after(100, update_progress)
        root.mainloop()


class ytHandle:
    def __init__(self, max_workers=1, sponsorblock_categories=None, video_name_callback=None):
        self.max_workers = 1
        self.video_name_callback = video_name_callback
        self.sponsorblock_categories = sponsorblock_categories or [
            "sponsor", "intro", "outro", "selfpromo", "preview", "filler", "music_offtopic"]
        self._download_lock = Lock()
        self._metadata_lock = Lock()
        self.cancel = Event()
        self._last_download = 0.0
        self._clean_regex = re.compile(r'[^\w _\-()&]')
        self._space_regex = re.compile(r'\s+')
        self._cookies_provider = lambda: []
        self._track_callback = None
        self.target_dir = Path.home() / "MusicPlayerYoutubeDownloads"

    def configure(self, cookies_provider=None, track_callback=None):
        if cookies_provider:
            self._cookies_provider = cookies_provider
        if track_callback:
            self._track_callback = track_callback

    def close(self):
        self.cancel.set()

    def playlist_tracks(self, url, cookies=None):
        with self._metadata_lock:
            return run_job({"action": "playlist", "url": url,
                            "cookies": self._cookies_provider() if cookies is None else cookies},
                           self.cancel, timeout=300)

    def search_youtube(self, query, limit=5):
        if not query or self.cancel.is_set():
            return []
        try:
            with self._metadata_lock:
                entries = run_job({"action": "search", "url": f"ytsearch{min(limit, 20)}:{query}"},
                                  self.cancel, timeout=45)
            return [[e["title"], f"https://www.youtube.com/watch?v={e['id']}", "url"]
                    for e in entries]
        except RuntimeError as exc:
            ll.warn(f"YouTube search: {exc}")
            return []

    def _download(self, url, directory, template=None, mp3=True, archive=None,
                  sponsorblock=False, cookies=None, cancel=None):
        cancel = cancel or self.cancel
        while not self._download_lock.acquire(timeout=0.5):
            if cancel.is_set() or self.cancel.is_set():
                return None
        try:
            # Rate-limit across ALL playlists, not per playlist or per worker.
            delay = 8.0 if game_running() else 5.0
            if cancel.wait(max(0, delay - (monotonic() - self._last_download))):
                return None
            if self.video_name_callback:
                self.video_name_callback("Downloading")
            try:
                result = run_job({"action": "download", "url": url,
                                  "directory": str(directory), "template": template,
                                  "mp3": mp3, "archive": str(archive) if archive else None,
                                  "sponsorblock": self.sponsorblock_categories if sponsorblock else None,
                                  "cookies": cookies or []}, cancel, timeout=600)
                if result:
                    path = Path(result["path"])
                    if mp3:
                        self._set_basic_tags_optimized(path, result)
                    if self._track_callback and not path.name.startswith(".youtubeCached"):
                        self._track_callback(str(path))
                    return str(path) if result else None
            finally:
                self._last_download = monotonic()
        finally:
            self._download_lock.release()
        return None

    def download_single_song_to_cache(self, url, youtube_download_permanently=False, target_dir=None):
        directory = self._find_valid_directory([target_dir or self.target_dir]) if youtube_download_permanently else Path(__file__).parent
        if directory is None:
            return None
        try:
            # Keep temporary playback in its source format; only GTA-compatible copies need MP3.
            return self._download(url, directory,
                                  template=None if youtube_download_permanently else ".youtubeCached [%(id)s].%(ext)s",
                                  mp3=youtube_download_permanently,
                                  cookies=self._cookies_provider())
        except RuntimeError as exc:
            ll.warn(f"YouTube download: {exc}")
            return None

    def parseUrl(self, url, possible_directories):
        directory = self._find_valid_directory(possible_directories)
        if directory is None:
            return []
        self.target_dir = directory
        existing = {path.stem for path in directory.glob("*.mp3")}
        results = []
        try:
            tracks = self.playlist_tracks(url, cookies=[])
            archive = directory / ".youtube_download_archive.txt"
            for entry in tracks:
                if self.cancel.is_set():
                    break
                # Honor older title-based downloads while using video IDs for new files.
                clean = self._space_regex.sub(" ", self._clean_regex.sub("", entry["title"])).strip()[:120]
                if clean in existing:
                    continue
                try:
                    path = self._download(f"https://www.youtube.com/watch?v={entry['id']}",
                                          directory, archive=archive, sponsorblock=True)
                    if path:
                        results.append(path)
                except RuntimeError as exc:
                    ll.warn(f"Playlist download skipped: {exc}")
        except RuntimeError as exc:
            ll.warn(f"Playlist sync: {exc}")
        return results

    def _find_valid_directory(self, directories):
        for value in directories:
            if not value or "://" in str(value):
                continue
            try:
                path = Path(value).expanduser().resolve()
                path.mkdir(parents=True, exist_ok=True)
                if path.is_dir() and os.access(path, os.W_OK):
                    return path
            except OSError:
                continue
        return None

    def _set_basic_tags_optimized(self, path, track):
        try:
            from mutagen.mp3 import MP3
            from mutagen.id3 import ID3, TIT2, TPE1
            audio = MP3(path, ID3=ID3)
            if not audio.tags:
                audio.add_tags()
            audio.tags.add(TIT2(encoding=3, text=track.get("title", "YouTube")))
            audio.tags.add(TPE1(encoding=3, text=track.get("artist") or "YouTube"))
            audio.save(v2_version=3)
        except Exception as exc:
            ll.debug(f"Could not tag downloaded audio: {exc}")
