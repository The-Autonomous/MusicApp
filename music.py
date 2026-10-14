"""Single-owner playback control with incremental local-library loading."""
import json
import os
from pathlib import Path
from collections import deque
from queue import Queue, Empty, Full
import random
import re
from heapq import nsmallest
from math import isfinite
from threading import Event, RLock, Thread
from time import monotonic
from mutagen import File
from audio import AudioPlayer
from log_loader import log_loader
from playerRecommend import PlayerRecommender
from ytHandle import ytHandle
from runtime_policy import game_running
from youtube_library import permanent_download_directory

ll = log_loader("Music Player", debugging=False)
APP_DIR = Path(__file__).parent
_SUPPORTED = (".mp3", ".wav", ".ogg", ".flac", ".m4a", ".opus", ".webm")
_PLACEHOLDER_MASK = 0x1000 | 0x40000 | 0x400000


class SmartShuffler:
    def __init__(self, cache=None, history_size=50, artist_spacing=2):
        self.cache = cache if cache is not None else []
        self.history = deque(maxlen=history_size)
        self.artist_history = deque(maxlen=artist_spacing)
        self.upcoming_indices = deque()
        self.replay_queue = deque()

    def _refill_upcoming(self):
        indices = list(range(len(self.cache)))
        random.shuffle(indices)
        self.upcoming_indices = deque(indices)

    def enqueue_replay(self, song):
        if song:
            self.replay_queue.appendleft(song)

    def get_unique_song(self):
        if self.replay_queue:
            return self.replay_queue.popleft()
        if not self.upcoming_indices:
            self._refill_upcoming()
        for _ in range(len(self.upcoming_indices)):
            index = self.upcoming_indices.popleft()
            if index >= len(self.cache):
                continue
            song = self.cache[index]
            artist = song.get('artist')
            if artist and artist.lower() == 'unknown artist':
                artist = None
            if song["path"] not in self.history and (not artist or artist not in self.artist_history):
                if artist:
                    self.artist_history.append(artist)
                return song
            self.upcoming_indices.append(index)
        if self.upcoming_indices:
            song = self.cache[self.upcoming_indices.popleft()]
            self.artist_history.append(song.get("artist"))
            return song
        return None


class MusicPlayer:
    SAVE_STATE_FILE = str(APP_DIR / ".musicapp_state.json")

    def __init__(self, directories, set_screen, set_duration, set_lyrics, set_ips, is_afk,
                 fast_load=False, fast_load_limit=20, startup_hold=False):
        self.set_screen, self.set_duration, self.set_lyrics = set_screen, set_duration, set_lyrics
        self.set_ips, self.is_afk = set_ips, is_afk
        self.skip_flag, self.pause_event, self.repeat_event = Event(), Event(), Event()
        self.current_player_mode, self.downloading_youtube_song = Event(), Event()
        self._shutdown, self._wake, self._cache_ready = Event(), Event(), Event()
        self._playback_ready = Event()
        if not startup_hold:
            self._playback_ready.set()
        self._commands = Queue(maxsize=128)
        self._library_lock = RLock()
        self._state_lock = RLock()
        self._library = {}
        self._library_generation = 0
        self._failed_paths = {}
        self.shuffler = SmartShuffler()
        self.meta = {}
        self.META_FILE = str(APP_DIR / ".musicapp_meta.json")
        self._meta_dirty = False
        self._search_cache = {}
        self._youtube_search_cache = {}
        self._playlist_urls = [p for p in directories if str(p).startswith("http")]
        self._directories = [p for p in directories if not str(p).startswith("http")]
        self._download_thread = None
        self._last_move = 0.0
        self.movementDebounce = [False, 0]
        self.movementDebounceTime = 0.25
        self.current_song = None
        self.current_song_id = 0
        self.current_song_lyrics = []
        self.song_elapsed_seconds = 0.0
        self.current_volume = 0.5
        self.current_index = -1
        self.forward_stack = []
        self.navigating_history = False
        self.youtube_download_permanently = False
        self.do_youtube_search = True
        self.lyrics_enabled = True
        self.full_radio_ip_list = []
        self.current_radio_ip = "0.0.0.0"
        self.radio_client = self.radio_master = self.radio_scanner = None
        self._scan_thread = None
        self._hosting_requested = False
        self._accept_host_eq = False
        self.auto_gaming = True
        self._preferred_gaming_mode = True
        self._lyric_lines = []
        self._lyric_index = -1
        self.lyricHandler = None
        self._resume_position = 0.0
        self._resume_path = None
        self._save_due = True
        self._last_save = self._last_meta_save = monotonic()
        self.AudioPlayer = AudioPlayer
        self.ytHandle = ytHandle()
        self.ytHandle.target_dir = permanent_download_directory(self._directories)
        self.ytHandle.configure(track_callback=self.add_downloaded_song,
                                active_path_provider=lambda: self.current_song["path"] if self.current_song else None)
        self.recommend = PlayerRecommender()
        self.load_meta_cache()
        self.load_playback_state()
        self.initializer_thread = Thread(target=self.initialize_cache,
                                         args=(self._directories, fast_load, fast_load_limit),
                                         daemon=True, name="library-scan")
        self.initializer_thread.start()
        self._core_thread = None

    def _command(self, action, value=None):
        if self._shutdown.is_set():
            return
        if not self._playback_ready.is_set() and action in {"play", "playlist", "pause", "next", "previous", "radio"}:
            return
        try:
            self._commands.put_nowait((action, value))
            self._wake.set()
        except Full:
            ll.warn("Playback controls are busy; try again shortly.")

    def allow_playback(self):
        self._playback_ready.set()
        self._wake.set()

    def close(self):
        if self._shutdown.is_set():
            return
        self._shutdown.set()
        self.ytHandle.close()
        self._wake.set()
        if self.initializer_thread.is_alive():
            self.initializer_thread.join(timeout=2)
        if self._scan_thread and self._scan_thread.is_alive():
            self._scan_thread.join(timeout=2)
        if self._core_thread and self._core_thread.is_alive():
            self._core_thread.join(timeout=3)
        else:
            AudioPlayer.stop()
        if self.radio_client:
            self.radio_client.stopListening()
        if self.radio_master:
            self.radio_master.close()
        if self.lyricHandler:
            self.lyricHandler.close()
        self.save_playback_state()
        self.save_meta_cache()
        self.recommend.close()
        AudioPlayer.eq._save_settings()
        AudioPlayer.close()

    def get_gaming_mode(self):
        return AudioPlayer._gaming_mode

    def toggle_gaming_mode(self, enable):
        self._preferred_gaming_mode = bool(enable)
        AudioPlayer._gaming_mode = bool(enable)
        self._save_due = True

    def accepting_radio_eq(self):
        return self._accept_host_eq

    def set_accepting_radio_eq(self, accept):
        self._accept_host_eq = bool(accept)
        if self.radio_client:
            self.radio_client.set_accept_host_eq(accept)
        self._save_due = True

    def set_band(self, freq_hz, gain_db, Q=1.0):
        AudioPlayer.eq.set_gain(freq_hz, gain_db)

    def get_band(self, freq_hz, default=0.0):
        return AudioPlayer.eq.get_band(freq_hz, default)

    def get_bands(self):
        return AudioPlayer.eq.get_gains()

    @property
    def echo(self):
        return AudioPlayer.echo

    def enable_echo(self, delay_ms=350, feedback=0.35, wet=0.5):
        AudioPlayer.enable_echo(delay_ms, feedback, wet)

    def disable_echo(self):
        AudioPlayer.disable_echo()

    def set_echo(self, delay_ms=None, feedback=None, wet=None):
        if wet is not None and wet <= 0:
            self.disable_echo()
        elif AudioPlayer.echo:
            AudioPlayer.set_echo(delay_ms, feedback, wet)
        else:
            self.enable_echo(delay_ms or 350, feedback if feedback is not None else 0.35,
                             wet if wet is not None else 0.5)

    def set_lyrics_enabled(self, enabled):
        self.lyrics_enabled = bool(enabled)
        if not enabled:
            self.set_lyrics(False)
        elif self.current_song and not self.current_player_mode.is_set():
            self._request_lyrics()

    def set_volume(self, direction=0, set_directly=False):
        self.current_volume = max(0.0, min(1.0, float(direction) if set_directly
                                          else self.current_volume + float(direction)))
        AudioPlayer.set_volume(self.current_volume, True)
        self._save_due = True

    def get_volume(self):
        return self.current_volume

    def up_volume(self):
        self.set_volume(0.05)

    def dwn_volume(self):
        self.set_volume(-0.05)

    def pause(self, forcedState=None):
        if not self._playback_ready.is_set():
            return
        should_play = self.pause_event.is_set() if forcedState is None else forcedState
        self.pause_event.clear() if should_play else self.pause_event.set()
        self._command("pause", not should_play)

    def repeat(self, forcedState=None):
        enabled = not self.repeat_event.is_set() if forcedState is None else bool(forcedState)
        self.repeat_event.set() if enabled else self.repeat_event.clear()
        self._save_due = True
        self._wake.set()

    def get_display_title(self, specific_song=None):
        song = specific_song or self.current_song
        if not song:
            return ""
        return song["title"] + (" *+*" if self.repeat_event.is_set() else "") + (
            " *=*" if self.pause_event.is_set() else "")

    def play_song(self, path_or_song):
        with self._library_lock:
            song = self._library.get(os.path.normcase(os.path.abspath(path_or_song))) if isinstance(path_or_song, str) else path_or_song
        if song:
            self._command("play", dict(song))

    def get_unique_song(self):
        with self._library_lock:
            for _ in range(max(1, len(self.shuffler.cache) + len(self.shuffler.replay_queue))):
                song = self.shuffler.get_unique_song()
                if song is None or self._failed_paths.get(song['path'], 0) <= monotonic():
                    return song
            return None

    def skip_next(self):
        if monotonic() - self._last_move < self.movementDebounceTime:
            return
        self._last_move = monotonic()
        self._command("next")

    def skip_previous(self):
        if monotonic() - self._last_move < self.movementDebounceTime:
            return
        self._last_move = monotonic()
        self._command("previous")

    def gather_playlist(self, search_term, forced_search_results=None):
        results = forced_search_results if forced_search_results is not None else self.get_search_term(search_term, max_results=250)
        with self._library_lock:
            songs = [self._library[os.path.normcase(os.path.abspath(path))]
                     for _, path, kind in results
                     if kind == "path" and os.path.normcase(os.path.abspath(path)) in self._library]
        if songs:
            random.shuffle(songs)
            self._command("playlist", songs)

    def get_search_term(self, search_string, search_list=None, max_results=100):
        query = search_string.lower().strip()
        tokens = [t for t in re.split(r"[\s.,_-]+", query)
                  if t and t not in {"by", "the", "a", "an", "in", "on", "ft", "feat", "and", "&"}]
        if not tokens:
            return []
        with self._library_lock:
            cache_key = (query, max_results, self._library_generation) if search_list is None else None
            cached = self._search_cache.get(cache_key) if cache_key is not None else None
            if cached is not None:
                return list(cached)
            songs = list(self.shuffler.cache) if search_list is None else list(search_list)
        def matches():
            for index, song in enumerate(songs):
                if isinstance(song, dict):
                    clean = song.get("_search")
                    if clean is None:
                        clean = f"{song.get('artist', '')} - {song.get('title', '')}".lower()
                else:
                    display, path, kind = song
                    clean = re.sub(r"[^\w\s]", "", display.lower())
                if all(t in clean for t in tokens):
                    yield (abs(len(clean) - len(query)), index, song)
        results = [(f"{song.get('artist', '')} - {song.get('title', '')}", song["path"], "path")
                   if isinstance(song, dict) else tuple(song)
                   for _, _, song in nsmallest(max_results, matches())]
        if cache_key is not None:
            with self._library_lock:
                if len(self._search_cache) >= 8:
                    self._search_cache.pop(next(iter(self._search_cache)))
                self._search_cache[cache_key] = results
        return list(results)

    def get_youtube_search(self, search_term):
        if not search_term:
            return []
        cached = self._youtube_search_cache.get(search_term)
        if cached and monotonic() - cached[0] < 600:
            return cached[1]
        results = self.ytHandle.search_youtube(search_term)
        if len(self._youtube_search_cache) >= 32:
            self._youtube_search_cache.pop(next(iter(self._youtube_search_cache)))
        self._youtube_search_cache[search_term] = (monotonic(), results)
        return results

    def play_youtube_song(self, url):
        if not self._playback_ready.is_set() or self.downloading_youtube_song.is_set():
            return
        self.downloading_youtube_song.set()
        try:
            path = self.ytHandle.download_single_song_to_cache(url, self.youtube_download_permanently)
            if path:
                song = self._song_from_path(path)
                if song:
                    self.play_song(song)
            else:
                self.set_screen("YouTube", "Download failed; local music keeps playing")
        finally:
            self.downloading_youtube_song.clear()

    def get_metadata(self, file_path):
        try:
            audio = File(file_path, easy=True)
            return {"artist": (audio.get("artist") or ["Unknown Artist"])[0],
                    "title": (audio.get("title") or [Path(file_path).stem])[0],
                    "duration": float(audio.info.length)}
        except Exception:
            # AV covers downloaded containers whose tags mutagen does not recognize.
            try:
                import av
                with av.open(file_path) as container:
                    stream = next(s for s in container.streams if s.type == "audio")
                    duration = float(stream.duration * stream.time_base) if stream.duration is not None else float(container.duration or 0) / av.time_base
                    tags = dict(container.metadata)
                    return {"artist": tags.get("artist", "YouTube"),
                            "title": tags.get("title", Path(file_path).stem), "duration": duration}
            except Exception:
                return {"artist": "Unknown Artist", "title": Path(file_path).stem, "duration": 0}

    def _song_from_path(self, path):
        path = os.path.abspath(path)
        try:
            stat = os.stat(path, follow_symlinks=False)
            if not self.verify_file_ok(path, stat):
                return None
            with self._library_lock:
                cached = self.meta.get(path)
            if cached and cached.get("mtime") == stat.st_mtime_ns and cached.get("size") == stat.st_size:
                data = dict(cached)
            else:
                data = self.get_metadata(path)
                data.update(path=path, mtime=stat.st_mtime_ns, size=stat.st_size)
                with self._library_lock:
                    self.meta[path] = dict(data)
                    self._meta_dirty = True
            if not self.check_song_length(data.get("duration", 0)):
                return None
            data["path"] = path
            data["_search"] = re.sub(r"[^\w\s]", "", f"{data.get('artist', '')} {data.get('title', '')}".lower())
            return data
        except OSError:
            return None

    def add_downloaded_song(self, path, wake=True):
        song = self._song_from_path(path)
        if not song:
            return
        with self._library_lock:
            key = os.path.normcase(song["path"])
            self._library_generation += 1
            if key in self._library:
                self._library[key].update(song)
                return
            self._library[key] = song
            self.shuffler.cache.append(song)
            self.shuffler.upcoming_indices.append(len(self.shuffler.cache) - 1)
        if wake:
            self._wake.set()

    def local_songs_for_sync(self, cancel):
        """Read the full library, including files omitted by optional fast loading."""
        while not self._cache_ready.wait(0.1):
            if cancel.is_set() or self._shutdown.is_set():
                raise RuntimeError("Cancelled")
        with self._library_lock:
            songs = [dict(song) for song in self._library.values()]
        seen = set()
        for song in songs:
            if cancel.is_set() or self._shutdown.is_set():
                raise RuntimeError("Cancelled")
            seen.add(os.path.normcase(song["path"]))
            # Refresh metadata if the file was changed after startup.
            current = self._song_from_path(song["path"])
            if current:
                yield current
        for directory in self._directories:
            for root, dirs, files in os.walk(directory):
                if cancel.is_set() or self._shutdown.is_set():
                    raise RuntimeError("Cancelled")
                dirs[:] = [name for name in dirs if not name.startswith(".")]
                for name in files:
                    if cancel.is_set() or self._shutdown.is_set():
                        raise RuntimeError("Cancelled")
                    if name.startswith(".") or not name.lower().endswith(_SUPPORTED):
                        continue
                    path = os.path.abspath(os.path.join(root, name))
                    key = os.path.normcase(path)
                    if key in seen:
                        continue
                    seen.add(key)
                    song = self._song_from_path(path)
                    if song:
                        yield song

    def initialize_cache(self, directories, fast_load=False, fast_load_limit=20):
        seen = set()
        original_paths = set(self.meta)
        try:
            if self._resume_path:
                self.add_downloaded_song(self._resume_path)
            for directory in directories:
                for root, dirs, files in os.walk(directory):
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                    for name in files:
                        if self._shutdown.is_set():
                            return
                        if not name.lower().endswith(_SUPPORTED) or name.startswith("."):
                            continue
                        path = os.path.abspath(os.path.join(root, name))
                        key = os.path.normcase(path)
                        if key in seen:
                            continue
                        seen.add(key)
                        previous_count = len(self.shuffler.cache)
                        self.add_downloaded_song(path, wake=False)
                        count = len(self.shuffler.cache)
                        if count > previous_count and (count == 1 or count % 64 == 0):
                            self._wake.set()
                        if fast_load and len(self.shuffler.cache) >= fast_load_limit:
                            return
            # Prune only after a complete scan; partial scans must not erase metadata.
            with self._library_lock:
                for path in original_paths:
                    if os.path.normcase(path) not in seen:
                        self.meta.pop(path, None)
                        self._meta_dirty = True
        finally:
            self._cache_ready.set()
            if self._resume_path:
                self.add_downloaded_song(self._resume_path)
            with self._library_lock:
                self.shuffler._refill_upcoming()
            self.save_meta_cache()
            self._wake.set()

    @staticmethod
    def check_song_length(duration=0):
        # Valid interludes and long liked songs should be playable too.
        return isinstance(duration, (int, float)) and isfinite(duration) and duration > 0

    @staticmethod
    def verify_file_ok(path, stat=None):
        stat = stat or os.stat(path, follow_symlinks=False)
        import stat as stat_module
        return (stat_module.S_ISREG(stat.st_mode) and stat.st_size > 0
                and not (getattr(stat, "st_file_attributes", 0) & _PLACEHOLDER_MASK))

    def load_meta_cache(self):
        try:
            with open(self.META_FILE, encoding="utf-8") as handle:
                data = json.load(handle)
                self.meta = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self.meta = {}

    def save_meta_cache(self):
        with self._library_lock:
            if not self._meta_dirty:
                return
            try:
                temporary = self.META_FILE + ".tmp"
                with open(temporary, "w", encoding="utf-8") as handle:
                    json.dump(self.meta, handle, ensure_ascii=False)
                os.replace(temporary, self.META_FILE)
                self._meta_dirty = False
            except OSError as exc:
                ll.warn(f"Metadata save: {exc}")

    def save_playback_state(self):
        with self._state_lock:
            state = {"path": self.current_song["path"] if self.current_song else self._resume_path,
                     "elapsed": self.song_elapsed_seconds if self._playback_ready.is_set() else self._resume_position,
                     "paused": self.pause_event.is_set(),
                     "repeat": self.repeat_event.is_set(), "volume": self.current_volume,
                     "gaming_mode": self._preferred_gaming_mode, "accept_radio_eq": self._accept_host_eq,
                     "current_radio_ip": self.current_radio_ip,
                     "youtube_download_permanently": self.youtube_download_permanently,
                     "do_youtube_search": self.do_youtube_search}
            try:
                temporary = self.SAVE_STATE_FILE + ".tmp"
                with open(temporary, "w", encoding="utf-8") as handle:
                    json.dump(state, handle)
                os.replace(temporary, self.SAVE_STATE_FILE)
                self._save_due = False
                self._last_save = monotonic()
            except OSError as exc:
                ll.warn(f"Playback save: {exc}")

    def load_playback_state(self):
        try:
            with open(self.SAVE_STATE_FILE, encoding="utf-8") as handle:
                state = json.load(handle)
            self._resume_path = state.get("path")
            self._resume_position = max(0, float(state.get("elapsed", 0)))
            self.set_volume(state.get("volume", 0.5), True)
            self.toggle_gaming_mode(state.get("gaming_mode", True))
            self._accept_host_eq = bool(state.get("accept_radio_eq", False))
            self.current_radio_ip = state.get("current_radio_ip", "0.0.0.0")
            self.youtube_download_permanently = bool(state.get("youtube_download_permanently", False))
            self.do_youtube_search = bool(state.get("do_youtube_search", True))
            if state.get("paused"):
                self.pause_event.set()
            if state.get("repeat"):
                self.repeat_event.set()
            return True
        except (OSError, ValueError, TypeError):
            return False

    def sync_playlists(self):
        sync = getattr(self, "youtube_sync", None)
        if sync is not None and not self._shutdown.is_set():
            return sync.start(sync._observer or (lambda _: None), on_ready=self.allow_playback)
        return False

    def load_radio_ips(self, seconds_to_scan=None):
        if self._scan_thread and self._scan_thread.is_alive():
            return
        def work():
            try:
                from radioIpScanner import SimpleRadioScan
                scanner = SimpleRadioScan(timeout=2, concurrency=4, cancel=self._shutdown)
                found = []
                def found_station(ip, title, location):
                    if ip not in found:
                        found.append(ip)
                        self.full_radio_ip_list = list(found)
                        self.set_ips(list(found))
                        if self.current_radio_ip == "0.0.0.0":
                            self.current_radio_ip = ip
                scanner.scan_all(found_station)
                self.full_radio_ip_list = found
                self.set_ips(found)
            except Exception as exc:
                ll.warn(f"Radio discovery: {exc}")
            finally:
                self._wake.set()
        self._scan_thread = Thread(target=work, daemon=True, name="radio-discovery")
        self._scan_thread.start()

    def set_radio_ip(self, new_ip):
        if new_ip in self.full_radio_ip_list:
            self.current_radio_ip = new_ip
            self._command("station", new_ip)
            return True
        self.load_radio_ips()
        return False

    def toggle_loop_cycle(self, CycleType=None):
        mode = not self.current_player_mode.is_set() if CycleType is None else bool(CycleType)
        self._command("radio", mode)

    def set_hosting(self, enabled):
        self._command("hosting", bool(enabled))

    def _request_lyrics(self):
        if not self.lyrics_enabled or not self.current_song:
            return
        if self.lyricHandler is None:
            from lyricMaster import lyricHandler
            self.lyricHandler = lyricHandler()
        song_id = self.current_song_id
        self.lyricHandler.request(self.current_song["artist"], self.current_song["title"],
                                 lambda lines, identity: self._command("lyrics", (identity, lines)),
                                 song_id)

    def _start_song(self, song, position=0, record=True):
        if not self._playback_ready.is_set() or not song:
            return False
        self.current_song_id += 1
        self._lyric_lines, self._lyric_index = [], -1
        self.current_song_lyrics = []
        self.set_lyrics(False)
        self.current_song = song
        self._resume_path = song["path"]
        self.song_elapsed_seconds = position
        if record and not Path(song["path"]).name.startswith(".youtubeCached"):
            with self._library_lock:
                if self.current_index < len(self.shuffler.history) - 1:
                    while len(self.shuffler.history) > self.current_index + 1:
                        self.shuffler.history.pop()
                if not self.shuffler.history or self.shuffler.history[-1] != song["path"]:
                    self.shuffler.history.append(song["path"])
                self.current_index = len(self.shuffler.history) - 1
        if not AudioPlayer._start_playback_session(song["path"], position,
                                                   not self.pause_event.is_set()):
            self.set_screen("Cannot play", song["title"])
            self.current_song = None
            self._failed_paths[song['path']] = monotonic() + 60
            self._resume_path = None
            return False
        self._played_logged = False
        self._save_due = True
        self._request_lyrics()
        self._update_host()
        return True

    def _update_host(self):
        if self.radio_master and self.current_song and not self.current_player_mode.is_set():
            self.radio_master.initSong(f"{self.current_song['artist']}![]!{self.current_song['title']}",
                                       self.current_song["path"], AudioPlayer,
                                       json.dumps(self.current_song_lyrics),
                                       duration=self.current_song["duration"],
                                       track_id=str(self.current_song_id))

    def _set_radio(self, enabled):
        if enabled == self.current_player_mode.is_set():
            return
        if enabled:
            self.song_elapsed_seconds = AudioPlayer.get_pos()
            AudioPlayer.stop()
            self.current_player_mode.set()
            self.set_lyrics(False)
            self.load_radio_ips()
            if self.current_radio_ip != "0.0.0.0":
                self._connect_radio()
            if self.radio_master:
                # Suspend broadcasting the local track while it is not actually playing.
                self.radio_master.current_data = dict(self.radio_master.current_data,
                                                     mixer=None, title='Radio![]!Listening to another station',
                                                     mp3_path='', lyrics='[]', track_id='')
        else:
            if self.radio_client:
                self.radio_client.stopListening()
            self.current_player_mode.clear()
            if self.current_song:
                self._start_song(self.current_song, self.song_elapsed_seconds, record=False)

    def _connect_radio(self):
        if not self._playback_ready.is_set():
            return
        if self.radio_client:
            self.radio_client.stopListening()
        from radioClient import RadioClient
        self.radio_client = RadioClient(AudioPlayer, self.current_radio_ip)
        self.radio_client.set_accept_host_eq(self._accept_host_eq)
        self.radio_client.listenTo(self.current_radio_ip)

    def _handle_command(self, action, value):
        if action == "hosting":
            self._hosting_requested = value
            if value and self.radio_master is None:
                from radioMaster import RadioHost
                self.radio_master = RadioHost(self)
                self._update_host()
            elif not value and self.radio_master:
                self.radio_master.close()
                self.radio_master = None
        elif action == "radio":
            self._set_radio(value)
        elif action == "station" and self.current_player_mode.is_set():
            self._connect_radio()
        elif action == "lyrics":
            identity, lines = value
            if identity == self.current_song_id:
                self._lyric_lines = list(lines or [])
                self.current_song_lyrics = self._lyric_lines
                self._lyric_index = -1
                self._update_host()
        elif not self.current_player_mode.is_set():
            if action == "pause":
                AudioPlayer.pause() if value else AudioPlayer.unpause()
                self._save_due = True
            elif action == "play":
                self.forward_stack.clear()
                self.shuffler.replay_queue.clear()
                self.pause_event.clear()
                self._start_song(value)
            elif action == "playlist":
                self.forward_stack.clear()
                self.shuffler.replay_queue = deque(value[1:])
                self.pause_event.clear()
                self._start_song(value[0])
            elif action == "next":
                if self.forward_stack:
                    path = self.forward_stack.pop()
                    self.current_index = min(self.current_index + 1, len(self.shuffler.history) - 1)
                    song = self._library.get(os.path.normcase(path))
                    self._start_song(song, record=False)
                else:
                    self._start_song(self.get_unique_song())
            elif action == "previous":
                if self.song_elapsed_seconds > 3 and not self.forward_stack:
                    self._start_song(self.current_song, record=False)
                elif self.current_index > 0:
                    if self.current_song:
                        self.forward_stack.append(self.current_song["path"])
                    self.current_index -= 1
                    self._start_song(self._library.get(os.path.normcase(
                        self.shuffler.history[self.current_index])), record=False)
                elif self.current_song:
                    self._start_song(self.current_song, record=False)

    def core_handler(self):
        if self._core_thread and self._core_thread.is_alive():
            return
        self._core_thread = Thread(target=self.core_player_loop, daemon=True, name="playback-control")
        self._core_thread.start()

    def core_player_loop(self):
        try:
            while not self._shutdown.is_set():
                self._wake.clear()
                if not self._playback_ready.is_set():
                    if self._shutdown.is_set():
                        break
                    self._wake.wait()
                    continue
                try:
                    for _ in range(32):
                        try:
                            action, value = self._commands.get_nowait()
                        except Empty:
                            break
                        self._handle_command(action, value)
                    if self.current_player_mode.is_set():
                        if (self.radio_client is None and self.current_radio_ip != "0.0.0.0"):
                            self._connect_radio()
                        if self.radio_client:
                            data = self.radio_client.get_client_data()
                            parts = data["radio_text"].split("![]!", 1)
                            self.set_screen(parts[0], parts[1] if len(parts) > 1 else "Connecting…")
                            self.set_duration(*data["radio_duration"])
                            if self.lyrics_enabled and data.get("lyrics"):
                                position = data["radio_duration"][0]
                                line = next((text for stamp, text in reversed(data["lyrics"])
                                             if stamp <= position), "🎵")
                                self.set_lyrics(True, line)
                        else:
                            self.set_screen("Radio", "Looking for stations…")
                    else:
                        if self.current_song is None and self.shuffler.cache:
                            with self._library_lock:
                                resumed = self._library.get(os.path.normcase(self._resume_path or ""))
                            if self._resume_path and not resumed and not self._cache_ready.is_set():
                                pass
                            else:
                                self._start_song(resumed or self.get_unique_song(),
                                                 self._resume_position if resumed else 0)
                                self._resume_position = 0
                        elif self.current_song:
                            self.song_elapsed_seconds = AudioPlayer.get_pos()
                            duration = self.current_song["duration"]
                            self.set_duration(self.song_elapsed_seconds, duration)
                            self.set_screen(self.current_song["artist"], self.get_display_title())
                            if self.lyrics_enabled and self._lyric_lines:
                                index = self._lyric_index
                                while index + 1 < len(self._lyric_lines) and self._lyric_lines[index + 1][0] <= self.song_elapsed_seconds:
                                    index += 1
                                if index != self._lyric_index:
                                    self._lyric_index = index
                                    self.set_lyrics(True, self._lyric_lines[index][1])
                            if not self._played_logged and self.song_elapsed_seconds >= duration / 2:
                                if not self.is_afk():
                                    self.recommend.log_song_play(self.current_song["artist"], self.current_song["title"])
                                self._played_logged = True
                            if not self.pause_event.is_set() and not AudioPlayer.get_busy():
                                self._start_song(self.current_song if self.repeat_event.is_set()
                                                 else self.get_unique_song(), record=not self.repeat_event.is_set())
                    now = monotonic()
                    gaming = self._preferred_gaming_mode or (self.auto_gaming and game_running())
                    if AudioPlayer._gaming_mode != gaming:
                        AudioPlayer.eq.reset_state()
                        if AudioPlayer.echo:
                            AudioPlayer.echo.reset_state()
                        AudioPlayer._gaming_mode = gaming
                    active = self.current_song is not None and not self.pause_event.is_set()
                    if self._save_due or (active and now - self._last_save >= 20):
                        self.save_playback_state()
                    if now - self._last_meta_save >= 60:
                        self.save_meta_cache()
                        self._last_meta_save = now
                except Exception as exc:
                    ll.error(f"Playback: {exc}")
                if not self._commands.empty():
                    continue
                self._wake.wait(self._idle_wait_interval())
        finally:
            self.save_playback_state()
            AudioPlayer.stop()

    def _idle_wait_interval(self):
        if self.current_player_mode.is_set():
            return 1.0
        if self.current_song is not None and not self.pause_event.is_set():
            return 0.25 if self.lyrics_enabled and self._lyric_lines else 1.0
        return 15.0
