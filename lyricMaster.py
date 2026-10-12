"""One lazy lyric worker; timed display updates are owned by MusicPlayer."""
import atexit
import json
from pathlib import Path
from queue import Queue, Empty, Full
import re
from threading import Event, RLock, Lock, Thread
from time import time
import requests
from playerUtils import TitleCleaner
from log_loader import log_loader

ll = log_loader("Lyrics", debugging=False)


class OptimizedJSONHandler:
    def __init__(self, filename=".lyricCache.json", batch_size=50, flush_interval=60, ttl_hours=168):
        self.filepath = Path(__file__).parent / filename
        self.ttl = ttl_hours * 3600
        self._lock = RLock()
        self._dirty = False
        try:
            with self.filepath.open(encoding="utf-8") as handle:
                data = json.load(handle)
            self._cache = {k: v for k, v in data.items()
                           if isinstance(v, dict) and time() - v.get("timestamp", 0) < self.ttl}
        except (OSError, ValueError):
            self._cache = {}
        atexit.register(self.close)

    @staticmethod
    def _make_key(artist, title):
        return f"{artist.strip().lower()}|{title.strip().lower()}"

    def get(self, artist, title):
        with self._lock:
            entry = self._cache.get(self._make_key(artist, title))
            if entry is None:
                return None
            # Shorter negative-cache TTL avoids refetching unavailable lyrics on every repeat.
            ttl = self.ttl if entry.get("lyrics") else 6 * 3600
            if time() - entry.get("timestamp", 0) >= ttl:
                return None
            return [(line["timestamp"], line["text"]) for line in entry.get("lyrics", [])]

    def add(self, artist, title, lyrics):
        with self._lock:
            self._cache[self._make_key(artist, title)] = {
                "timestamp": time(), "lyrics": [{"timestamp": ts, "text": text} for ts, text in lyrics]}
            if len(self._cache) > 1000:
                oldest = min(self._cache, key=lambda key: self._cache[key]["timestamp"])
                self._cache.pop(oldest, None)
            self._dirty = True

    def close(self):
        with self._lock:
            if not self._dirty:
                return
            try:
                temporary = self.filepath.with_suffix(".json.tmp")
                with temporary.open("w", encoding="utf-8") as handle:
                    json.dump(self._cache, handle, ensure_ascii=False)
                temporary.replace(self.filepath)
                self._dirty = False
            except OSError as exc:
                ll.warn(f"Lyric cache save: {exc}")


class OptimizedLyricHandler:
    def __init__(self, api_url="https://lrclib.net/api/get", max_workers=1,
                 request_timeout=6, batch_size=1, rate_limit_rps=1):
        self.cache = OptimizedJSONHandler()
        self.title_cleaner = TitleCleaner()
        self._API_URL = api_url
        self.request_timeout = request_timeout
        self._request_queue = Queue(maxsize=1)
        self._shutdown = Event()
        self._http_lock = Lock()
        self._session = None
        self._timestamp_regex = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\](.*)$")
        self._last_request = 0
        self._interval = max(1, 1 / max(rate_limit_rps, 0.1))
        self._worker = Thread(target=self._run, daemon=True, name="lyrics")
        self._worker.start()
        atexit.register(self.close)

    def request(self, artist, title, callback, song_id):
        if self._shutdown.is_set():
            return
        item = (artist, title, callback, song_id)
        try:
            self._request_queue.put_nowait(item)
        except Full:
            try:
                self._request_queue.get_nowait()
            except Empty:
                pass
            try:
                self._request_queue.put_nowait(item)
            except Full:
                pass

    def _run(self):
        try:
            while not self._shutdown.is_set():
                try:
                    item = self._request_queue.get(timeout=60)
                except Empty:
                    self.cache.close()
                    continue
                if item is None:
                    break
                artist, title, callback, identity = item
                lyrics = self.request_sync(artist, title)
                if not self._shutdown.is_set():
                    try:
                        callback(lyrics, identity)
                    except Exception as exc:
                        ll.debug(f"Lyric callback: {exc}")
                self.cache.close()
        finally:
            if self._session:
                self._session.close()

    def request_sync(self, artist, title):
        cached = self.cache.get(artist, title)
        if cached is not None:
            return cached
        lyrics = self._load_synced_lyrics_sync(artist, title) or []
        if not self._shutdown.is_set():
            self.cache.add(artist, title, lyrics)
        return lyrics

    def request_batch_sync(self, requests):
        return {pair: self.request_sync(*pair) for pair in requests}

    def _load_synced_lyrics_sync(self, artist, title, max_retries=1):
        with self._http_lock:
            if self._shutdown.wait(max(0, self._interval - (time() - self._last_request))):
                return []
            cleaned = self.title_cleaner.clean(f"{artist} - {title}")
            parts = cleaned.split(" - ", 1)
            artist_clean, title_clean = parts if len(parts) == 2 else (artist, cleaned)
            if self._session is None:
                self._session = requests.Session()
                self._session.headers["User-Agent"] = "MusicApp/lyrics"
            for attempt in range(max_retries + 1):
                if self._shutdown.is_set():
                    return []
                self._last_request = time()
                try:
                    with self._session.get(self._API_URL,
                                           params={"track_name": title_clean, "artist_name": artist_clean},
                                           timeout=(3, self.request_timeout)) as response:
                        if response.status_code == 404:
                            return []
                        if response.status_code == 429:
                            if self._shutdown.wait(2):
                                return []
                            continue
                        response.raise_for_status()
                        return self._parse_lyrics_timestamps(response.json().get("syncedLyrics", "").splitlines())
                except requests.RequestException:
                    if attempt < max_retries and not self._shutdown.wait(1):
                        continue
            return []

    def _parse_lyrics_timestamps(self, lines):
        result = []
        for line in lines:
            match = self._timestamp_regex.match(line)
            if match:
                result.append((int(match[1]) * 60 + float(match[2]), match[3].strip() or "🎵"))
        return sorted(result)

    def close(self):
        if self._shutdown.is_set():
            return
        self._shutdown.set()
        try:
            self._request_queue.put_nowait(None)
        except Full:
            pass
        self._worker.join(timeout=1)
        self.cache.close()


class lyricHandler(OptimizedLyricHandler):
    def __init__(self, api_url="https://lrclib.net/api/get", min_delay_between_calls=1):
        super().__init__(api_url=api_url, rate_limit_rps=1 / max(min_delay_between_calls, 1))
