import os, random, platform, ast, requests, json, multiprocessing, ctypes
from functools import lru_cache
from multiprocessing import Array as MPArray
from threading import Event, Thread, Lock
from mutagen.mp3 import MP3
from mutagen import File
from pathlib import Path
from time import time, sleep

### IMPORTS ###

try:
    from ytHandle import ytHandle, DownloadPopup
    from lyricMaster import lyricHandler
    from radioIpScanner import SimpleRadioScan
    from radioClient import RadioClient
    from radioMaster import RadioHost
    from audio import AudioPlayer, AudioEcho
    from log_loader import log_loader
except:
    from .ytHandle import ytHandle
    from .lyricMaster import lyricHandler
    from .radioIpScanner import SimpleRadioScan
    from .radioClient import RadioClient
    from .radioMaster import RadioHost
    from .audio import AudioPlayer, AudioEcho
    from .log_loader import log_loader

#####################################################################################################

ll = log_loader("Music Player")

#####################################################################################################

save_playback_lock = Lock()  # Lock for saving playback state

#####################################################################################################

_REPARSE      = 0x0400   # OneDrive stub (older)
_OFFLINE      = 0x1000   # “offline” file
_RECALL_OPEN  = 0x04000  # new OneDrive smart-file flag
_PLACEHOLDER_MASK = _REPARSE | _OFFLINE | _RECALL_OPEN

#####################################################################################################

_SKIP_TIME_LENGTH_MAX = 15 * 60     # Skip 15 Minutes
_SKIP_TIME_LENGTH_MIN = 15          # Skip 15 Seconds
_RESTART_THRESHOLD_SECONDS = 3      # If song isnt within 3 seconds of the start restart instead of skipping

#####################################################################################################

class SharedString:
    """
    A thread-safe, process-safe shared string implementation using multiprocessing.Array
    This is the most efficient way to share strings between processes.
    """
    
    def __init__(self, initial_value="", max_size=256):
        """
        Initialize a shared string.
        
        Args:
            initial_value: Starting string value
            max_size: Maximum bytes the string can hold (default 256)
        """
        self.max_size = max_size
        self._array = MPArray(ctypes.c_char, max_size)
        self._lock = multiprocessing.Lock()
        
        # Set initial value
        if initial_value:
            self.set(initial_value)
        else:
            # Initialize with null bytes
            for i in range(max_size):
                self._array[i] = b'\x00'
    
    def set(self, value: str) -> None:
        """
        Set the shared string value.
        Thread-safe and process-safe.
        """
        with self._lock:
            # Encode string to bytes, truncate if needed
            encoded = value.encode('utf-8', errors='ignore')
            if len(encoded) >= self.max_size:
                encoded = encoded[:self.max_size - 1]
            
            # Clear the array first
            for i in range(self.max_size):
                self._array[i] = b'\x00'
            
            # Write the encoded bytes
            for i, byte in enumerate(encoded):
                self._array[i] = bytes([byte])
            
            # Ensure null termination
            self._array[len(encoded)] = b'\x00'
    
    def get(self) -> str:
        """
        Get the current string value.
        Thread-safe and process-safe.
        """
        with self._lock:
            # Find the null terminator
            result_bytes = []
            for i in range(self.max_size):
                if self._array[i] == b'\x00':
                    break
                result_bytes.append(self._array[i])
            
            # Convert bytes to string
            if result_bytes:
                return b''.join(result_bytes).decode('utf-8', errors='ignore')
            return ""
    
    def __str__(self):
        return self.get()
    
    def __repr__(self):
        return f"SharedString(value='{self.get()}', max_size={self.max_size})"

#####################################################################################################

class SmartShuffler:
    def __init__(self, cache=[], history_size=50, artist_spacing=2):
        self.cache = list(cache)
        self.history_size = history_size
        self.artist_spacing = artist_spacing
        self.history = []
        self.upcoming = []
        self.replay_queue = []

    def _refill_upcoming(self):
        songs = self.cache.copy()
        random.shuffle(songs)
        # enforce artist_spacing
        for i in range(len(songs)):
            for j in range(1, self.artist_spacing + 1):
                if i + j < len(songs) and songs[i]['artist'] == songs[i + j]['artist']:
                    # swap with a track further ahead
                    for k in range(i + self.artist_spacing + 1, len(songs)):
                        if songs[k]['artist'] != songs[i]['artist']:
                            songs[i + j], songs[k] = songs[k], songs[i + j]
                            break
        self.upcoming = songs

    def enqueue_replay(self, song):
        """
        Immediately queues a specific song to be played next, bypassing shuffle and spacing logic.
        This method inserts the song at the front of the replay queue. Songs in the replay queue
        take priority over all other playback logic, including the upcoming shuffle list and artist spacing rules.
        """
        ll.debug(AudioPlayer.__repr__(), self.__repr__())
        self.replay_queue.insert(0, song)

    def get_unique_song(self):
        if self.replay_queue:
            return self.replay_queue.pop(0)

        if not self.upcoming:
            self._refill_upcoming()

        while self.upcoming:
            song = self.upcoming.pop(0)
            if song['path'] in self.history:
                self.upcoming.append(song)
                continue
            # pass spacing/history
            self.history.append(song['path'])
            if len(self.history) > self.history_size:
                self.history.pop(0)
            return song

        # fallback
        return random.choice(self.cache) if self.cache else None

    def __repr__(self):
        return f"""
            <SmartShuffler(cache={len(self.cache)}, history_size={self.history_size}, artist_spacing={self.artist_spacing})>
            self.cache: {len(self.cache)} songs
            self.history: {len(self.history)} songs
            self.upcoming: {len(self.upcoming)} songs
            self.replay_queue: {len(self.replay_queue)} songs
            """

#####################################################################################################

class MusicPlayer:
    SAVE_STATE_FILE = ".musicapp_state.json"
    
    def __init__(self, directories, set_screen, set_duration, set_lyrics, set_ips):
        # Playback control events
        self.skip_flag = Event()
        self.pause_event = Event()
        self.repeat_event = Event()
        self.current_player_mode = Event()  # False = MusicPlayer, True = RadioPlayer
        
        # Initialize Multiprocess Popup
        self.close_event = multiprocessing.Event()
        self.downloadPopup = DownloadPopup()
        self.progress_value = multiprocessing.Value('d', 0.0)  # 'd' = double precision float
        self.current_video = SharedString(max_size=20)
        self.popup_proc = multiprocessing.Process(target=self.downloadPopup.popup_process, args=(self.close_event, self.progress_value, self.current_video))
        self.popup_proc.start()
        
        # Movement Debounce
        self.movementDebounce = [False, 0.2]  # [is_moving, debounce_time]
        self.movementDebounceTime = 1 # Time Allowed Between Movements In Seconds

        # UI callbacks
        self.set_screen = set_screen
        self.set_duration = set_duration
        self.set_lyrics = set_lyrics
        self.set_ips = set_ips

        # Initialize YouTube
        self.ytHandle = ytHandle(video_name_callback=self.current_video.set)
        self.songDownloadThreads = []
        
        # Initialize Lyric Handler
        self.lyricHandler = lyricHandler()

        # META Data
        self.META_FILE = ".musicapp_meta.json"
        self.meta = {}
        self.load_meta_cache()
        
        # Cache & Shuffler
        self.shuffler = SmartShuffler()
        self.initializer_thread = Thread(target=self.initialize_cache, args=(directories,), daemon=True)
        self.initializer_thread.start()
        self.songDownloadThreads.append(self.initializer_thread)
        self.wait_for_yt()

        # Playback state
        self.current_song = None
        self.song_elapsed_seconds = 0.0
        self.forward_stack = []
        self.current_index = -1
        self.current_volume = 0.1
        self.navigating_history = False

        # Radio system
        self.full_radio_ip_list = []
        self.current_radio_ip = "192.168.1.205"
        self.radio_client = RadioClient(AudioPlayer, ip=self.current_radio_ip)
        self.radio_master = RadioHost(self)
        self.radio_scanner = SimpleRadioScan()

#####################################################################################################

    def get_gaming_mode(self) -> bool:
        """
        Return whether gaming mode is currently enabled.
        In gaming mode, audio processing is bypassed for lower latency.
        """
        return AudioPlayer._gaming_mode
    
    def toggle_gaming_mode(self, enable: bool):
        """
        Enable or disable gaming mode, which bypasses audio processing for lower latency.
        """
        AudioPlayer._gaming_mode = enable
        ll.debug(f"Gaming mode {'enabled' if enable else 'disabled'}. Audio processing {'bypassed' if enable else 'active'}.")

    def set_band(self, freq_hz: int, gain_db: float, Q: float = 1.0):
        """
        Set the gain of one ISO-centre band.
        Q is ignored because AudioEQ uses a fixed constant-Q design.
        """
        eq = getattr(AudioPlayer, "eq", None)
        if eq:
            eq.set_gain(freq_hz, gain_db)

    def get_band(self, freq_hz: int, default: tuple[float, float] = (0.0, 1.0)):
        """
        Return (gain_dB, Q) for a single band.
        Falls back to `default` if the band or EQ is missing.
        """
        eq = getattr(AudioPlayer, "eq", None)
        if eq:
            return eq.get_band(freq_hz, default)
        return default

    def get_bands(self) -> dict[int, float]:
        """
        Return the full {centre_freq_Hz: gain_dB} map,
        or an empty dict when EQ isn't initialised.
        """
        eq = getattr(AudioPlayer, "eq", None)
        return eq.get_gains() if eq else {}

    def enable_echo(self, delay_ms: int = 350,
                    feedback: float = 0.35,
                    wet: float = 0.5):
        """
        Thin shim → AudioPlayer.enable_echo()
        """
        AudioPlayer.enable_echo(delay_ms, feedback, wet)


    def disable_echo(self):
        """
        Thin shim → AudioPlayer.disable_echo()
        """
        AudioPlayer.disable_echo()


    def set_echo(self, delay_ms: int | None = None,
                feedback: float | None = None,
                wet: float | None = None):
        """
        Delegates to AudioPlayer.set_echo(), but auto-enables or disables
        the effect when appropriate (delay>0 or wet>0 ⇒ enable, else disable).
        """
        # If echo line already exists, just tweak it
        if getattr(AudioPlayer, "echo", None):
            AudioPlayer.set_echo(delay_ms, feedback, wet)

            # auto-disable when both delay and wet end up at 0
            echo = AudioPlayer.echo
            if echo and echo.delay_ms == 0 and echo.wet == 0:
                AudioPlayer.disable_echo()
            return

        # If no echo yet, enable when meaningful params come in
        if (delay_ms or 0) > 0 or (wet or 0) > 0:
            AudioPlayer.enable_echo(delay_ms or 350,
                            feedback if feedback is not None else 0.35,
                            wet      if wet      is not None else 0.5)

#####################################################################################################

    def get_search_term(self, search_string: str):
        """
        Search for songs in cache based on the search string.
        Returns a list of tuples (display_name, path) sorted by relevance.
        Limits results to 50 for performance.
        """
        search_string = search_string.lower().strip()
        if not search_string:
            return []

        results = []
        seen_paths = set()
        MAX_RESULTS = 50

        # Helper to add result if not already seen
        def add_result(song, score=0):
            if song['path'] not in seen_paths:
                seen_paths.add(song['path'])
                display = f"{song['artist']} - {song['title']}"
                results.append((display, song['path'], score))
                return True
            return False

        # 1. Direct title matches (highest priority)
        for song in self.shuffler.cache:
            if search_string in song['title'].lower():
                if add_result(song, 100):
                    if len(results) >= MAX_RESULTS:
                        break

        if len(results) >= MAX_RESULTS:
            return [(r[0], r[1]) for r in sorted(results, key=lambda x: x[2], reverse=True)]

        # 2. Artist + Title matches
        for song in self.shuffler.cache:
            combined = f"{song['artist']} {song['title']}".lower()
            if search_string in combined:
                if add_result(song, 75):
                    if len(results) >= MAX_RESULTS:
                        break

        if len(results) >= MAX_RESULTS:
            return [(r[0], r[1]) for r in sorted(results, key=lambda x: x[2], reverse=True)]

        # 3. Artist matches
        for song in self.shuffler.cache:
            if search_string in song['artist'].lower():
                if add_result(song, 50):
                    if len(results) >= MAX_RESULTS:
                        break

        if len(results) >= MAX_RESULTS:
            return [(r[0], r[1]) for r in sorted(results, key=lambda x: x[2], reverse=True)]

        # 4. Path matches
        for song in self.shuffler.cache:
            if search_string in os.path.basename(song['path']).lower():
                if add_result(song, 25):
                    if len(results) >= MAX_RESULTS:
                        break

        if not results:
            # 5. Fuzzy matches (only if no other results)
            # Simple character-based similarity
            search_chars = set(search_string)
            for song in self.shuffler.cache:
                title_chars = set(song['title'].lower())
                artist_chars = set(song['artist'].lower())
                if len(search_chars & (title_chars | artist_chars)) >= len(search_chars) * 0.7:
                    if add_result(song, 10):
                        if len(results) >= MAX_RESULTS:
                            break

        # Sort by score and return just the display and path
        return [(r[0], r[1]) for r in sorted(results, key=lambda x: x[2], reverse=True)]

    def play_song(self, path_or_song):
        """
        Play a song either from a path string or a song dictionary.
        """
        # Convert path to song dict if needed
        if isinstance(path_or_song, str):
            song = next((s for s in self.shuffler.cache if s['path'] == path_or_song), None)
            if not song:
                ll.warn(f"Song not found in cache: {path_or_song}")
                return
        else:
            song = path_or_song

        # Clear the replay queue when explicitly playing a song.
        # This ensures that after this song plays, the shuffler will naturally
        # pick the next song from its upcoming queue, preventing unintended repeats.
        self.shuffler.replay_queue.clear()

        # Check if it's already playing the exact same song
        if self.current_song and self.current_song['path'] == song['path']:
            if self.pause_event.is_set():
                self.pause(True)  # Unpause if paused
            # If it's already playing, no need to re-queue or manipulate history.
            return

        # If it's a new song or a different instance of the same song, queue it and handle history
        self.skip_flag.set() # Signal core loop to pick up new song on next iteration
        self.shuffler.enqueue_replay(song) # Add to replay queue for immediate playback

        # Update history for direct plays (if not navigating history manually)
        if not self.navigating_history:
            # If we're playing a new song directly, add it to history and clear forward_stack
            self.shuffler.history = self.shuffler.history[:self.current_index+1]
            self.shuffler.history.append(song['path'])
            self.current_index = len(self.shuffler.history) - 1
            self.forward_stack.clear() # Clear forward stack on new direct play
        
#####################################################################################################

    def load_meta_cache(self):
        """
        Load persistent metadata (artist/title/duration) from disk.
        """
        try:
            if os.path.exists(self.META_FILE):
                with open(self.META_FILE, "r") as f:
                    self.meta = json.load(f)
            else:
                self.meta = {}
        except Exception as e:
            ll.warn(f"Failed to load metadata cache: {e}")
            self.meta = {}

    def save_meta_cache(self):
        """
        Save updated metadata cache to disk.
        """
        try:
            with open(self.META_FILE, "w") as f:
                json.dump(self.meta, f)
        except Exception as e:
            ll.error(f"Failed to save metadata cache: {e}")

#####################################################################################################

    def save_playback_state(self):
        """Save the current song path and elapsed time to a file."""
        global save_playback_lock
        if self.current_song:
            state = {
                "path": self.current_song["path"],
                "elapsed": self.song_elapsed_seconds,
                "paused": False if self.current_player_mode.is_set() else self.pause_event.is_set(),
                "repeat": self.repeat_event.is_set()
            }
            try:
                with save_playback_lock:
                    if self.current_song["path"] == state['path']:
                        with open(self.SAVE_STATE_FILE, "w") as f:
                            json.dump(state, f)
            except Exception as e:
                ll.error(f"Failed to save playback state: {e}")

    def load_playback_state(self):
        global save_playback_lock
        try:
            self.resume_pending = True
            with save_playback_lock:
                with open(self.SAVE_STATE_FILE, "r") as f:
                    state = json.load(f)
                path = state.get("path")
                elapsed = state.get("elapsed", 0)
                paused = state.get("paused", False)
                repeat = state.get("repeat", False)
                
                if path and os.path.exists(path):
                    # Find the song dict in cache
                    song = next((s for s in self.shuffler.cache if s["path"] == path), None)
                    if not song:
                        metadata = self.meta.get(path)
                        if metadata:
                            self.shuffler.cache.append(metadata)
                            self.shuffler._refill_upcoming()
                        
                    self.current_song = song
                    self._resume_position = float(elapsed)
                    self.shuffler.enqueue_replay(song)

                    # Restore repeat state
                    if repeat:
                        self.repeat_event.set()
                    else:
                        self.repeat_event.clear()

                    # Restore pause state
                    Thread(target=self.pause_after_mixer_ready, args=(paused,), daemon=True).start()

                    # Update UI to reflect restored state
                    self.set_screen(self.current_song['artist'], self.get_display_title())

                    return True
        except Exception as e:
            ll.warn(f"Failed to load playback state: {e}")
            self.resume_pending = False
        return False

    def initialize_cache(self, directories):
        supported_extensions = ('.mp3', '.wav', '.ogg', '.flac')
        unique_paths = set()  # Track unique paths to avoid duplicates
        for path in directories:
            if path.startswith('http'):
                newThread = Thread(target=self.ytDownload, args=(path, directories,))
                newThread.start()
                self.songDownloadThreads.append(newThread)
                continue
            for root, _, files in os.walk(path):
                for file in files:
                    full_path = os.path.join(root, file)
                    if full_path in unique_paths or not self.verify_file_ok(full_path):
                        continue
                    unique_paths.add(full_path)
                    if not file.lower().endswith(supported_extensions):
                        continue
                    
                    try:
                        st = os.stat(follow_symlinks=False)
                        mtime, size = st.st_mtime, st.st_size
                    except Exception:
                        mtime = size = None

                    # Use cached metadata if available
                    cached_metadata = self.meta.get(full_path)
                    if not cached_metadata or (mtime and cached_metadata.get('mtime') != mtime) or (size and cached_metadata.get('size') != size):
                        metadata = self.get_metadata(full_path)
                        metadata.update({'mtime': mtime, 'size': size})
                        self.meta[full_path] = metadata
                        cached_metadata = metadata

                    duration = cached_metadata.get('duration', 0.0)
                    if self.check_song_length(duration):
                        self.shuffler.cache.append({
                            'path': full_path,
                            'artist': cached_metadata.get('artist', 'Unknown Artist'),
                            'title': cached_metadata.get('title', os.path.splitext(file)[0]),
                            'duration': duration
                        })
                        
        # Remove cache entries for files that no longer exist
        removed = set(self.meta) - unique_paths
        for path in removed:
            del self.meta[path]
            
        # Refill upcoming queue after cache is populated
        self.shuffler._refill_upcoming()
        # Save cache
        self.save_meta_cache()
        # Load Playback State
        self.load_playback_state()

#####################################################################################################

    def ytDownload(self, url, possibleDirectories):
        returnedPaths = self.ytHandle.parseUrl(url, possibleDirectories)
        for path in returnedPaths:
            filename = os.path.basename(path)
            metadata = self.get_metadata(path)
            duration = metadata.get('duration', 0.0)
            if self.check_song_length(duration):
                self.shuffler.cache.append({
                    'path': path,
                    'artist': metadata.get('artist', 'Unknown Artist'),
                    'title': metadata.get('title', os.path.splitext(filename)[0]),
                    'duration': duration
                })
            else:
                ll.debug(f"🚨 File Duration ({duration}) Was Not Enough For It To Qualify")
        ll.debug(f"⏬ Download Completed: {url}")
    
    def wait_for_yt(self):
        ll.debug("Awaiting Youtube To Finish")

        # Wait for all downloads to complete
        currentThreadIndex = 0
        while currentThreadIndex < len(self.songDownloadThreads):
            try:
                if currentThreadIndex <= 0:
                    self.progress_value.value = 0
                else:
                    self.progress_value.value = currentThreadIndex / len(self.songDownloadThreads)
                self.songDownloadThreads[currentThreadIndex].join()
                currentThreadIndex += 1
            except Exception as E:
                ll.debug(f"Thread Waiting Error: {E}")
                continue

        # Close popup if it was shown
        if self.popup_proc:
            self.close_event.set()
            self.popup_proc.join()

        ll.debug("Finished Full Download List")

#####################################################################################################

    def get_metadata(self, file_path):
        """
        Pull artist, title, and duration (in seconds).  
        If we can't read length, duration=None.
        """
        try:
            audio = File(file_path, easy=True)
            # fallback title from filename
            title = audio.get('title', [os.path.splitext(os.path.basename(file_path))[0]])[0]
            artist = audio.get('artist', ['Unknown Artist'])[0]
            # try to get duration
            try:
                # MP3 has .info.length; other formats too
                duration = float(audio.info.length)
            except Exception:
                duration = 0.0
            return {'artist': artist, 'title': title, 'duration': duration}
        except Exception:
            return {
                'artist': 'Unknown Artist',
                'title': os.path.splitext(os.path.basename(file_path))[0],
                'duration': 0.0
            }

#####################################################################################################
    
    def pause(self, forcedState: bool = None):
        """forcedState: If provided, forces pause (False) or unpause (True); otherwise, toggles current pause state."""
        should_unpause = forcedState if forcedState is not None else self.pause_event.is_set()
        if should_unpause:
            self.pause_event.clear()
            AudioPlayer.unpause()
        else:
            self.pause_event.set()
            AudioPlayer.pause()
        self.set_screen(self.current_song['artist'], self.get_display_title())

    def repeat(self, forcedState: bool = None):
        should_repeat = forcedState if forcedState is not None else self.repeat_event.is_set()
        if self.pause_event.is_set() or self.movementDebounce[0]:
            return
        if should_repeat:
            self.repeat_event.clear()
        else:
            self.repeat_event.set()
        self.set_screen(self.current_song['artist'], self.get_display_title())

    def before_move(self):
        if self.movementDebounce[0] or (time() - self.movementDebounce[1]) < self.movementDebounceTime:
            return False
        self.movementDebounce = [True, time()]
        self.cachedRepeatValue = self.repeat_event.is_set()
        self.repeat_event.clear()
        
    def after_move(self):
        if self.cachedRepeatValue == True:
            self.repeat_event.set()
        self.cachedRepeatValue = False
        self.movementDebounce = [False, time()]
        
    def skip_next(self):
        if self.before_move() == False:
            return
        if self.forward_stack:
            self.navigating_history = True
            self.current_index += 1
            next_path = self.forward_stack.pop()
            next_song = next((s for s in self.shuffler.cache if s['path'] == next_path), None)
            if next_song:
                self._queue_song(next_song)
            self.navigating_history = False
        else:
            self._clear_for_new_track()
            new_song = self.get_unique_song()
            if new_song:
                self.shuffler.history = self.shuffler.history[:self.current_index+1]
                self.shuffler.history.append(new_song['path'])
                self.current_index += 1
                self.forward_stack.clear()
                self._queue_song(new_song)
        self.after_move()

    def skip_previous(self):
        if self.before_move() == False:
            return

        # We're in "navigation mode" if the user has already gone back at least once,
        # which we can tell by checking if the forward_stack has any songs.
        in_navigation_mode = bool(self.forward_stack)

        # The time-based restart should only apply when NOT in navigation mode.
        if not in_navigation_mode and self.song_elapsed_seconds > _RESTART_THRESHOLD_SECONDS:
            # We're in normal playback and past the time threshold, so just restart the current song.
            if self.current_song:
                self._queue_song(self.current_song)

        # This block handles both navigating backward and the initial "previous" press before the time threshold.
        elif self.current_index > 0:
            self.navigating_history = True
            self.forward_stack.append(self.shuffler.history[self.current_index])
            self.current_index -= 1
            prev_path = self.shuffler.history[self.current_index]
            prev_song = next((s for s in self.shuffler.cache if s['path'] == prev_path), None)
            if prev_song:
                self._queue_song(prev_song)
            self.navigating_history = False

        else:
            # If we're at the very first song in history, the only possible action is to restart it.
            if self.current_song:
                self._queue_song(self.current_song)

        self.after_move()
            
    def set_volume(self, direction: int = 0, set_directly: bool = False):
        """Set volume between 0.0 (silent) and 1.0 (full volume)"""
        if set_directly:
            self.current_volume = direction
        else:
            self.current_volume = round(sorted([0.0, self.current_volume + direction, 1.0])[1], 2)
        AudioPlayer.set_volume(self.current_volume)
        #ll.debug(f"🔊 {self.current_volume}")
        
    def get_volume(self):
        return self.current_volume
        
    def up_volume(self):
        self.set_volume(0.05)
        
    def dwn_volume(self):
        self.set_volume(-0.05)

#####################################################################################################

    @lru_cache(maxsize=256)
    def check_song_length(self, duration: float = 0.0):
        """Figures out if the duration is the right length to be kept. True if yes False if no"""
        return (duration >= _SKIP_TIME_LENGTH_MIN) and (duration <= _SKIP_TIME_LENGTH_MAX)
    
    @lru_cache(maxsize=256)
    def verify_file_ok(self, path: str) -> None:
        """
        One fast stat call:
        • raises FileNotFoundError if path missing / not regular file
        • raises OSError for 0-byte files or OneDrive placeholders
        """
        st = os.stat(path, follow_symlinks=False)
        if not os.path.isfile(path) or st.st_size == 0 or st.st_file_attributes & _PLACEHOLDER_MASK:
            ll.debug(f"{path} will not work with Media Player! Skipping.")
            return False
        return True

#####################################################################################################

    def get_display_title(self, specific_song=None):
        """Return the current song title with repeat and pause markers as needed."""
        if not specific_song: specific_song = self.current_song
        if not specific_song:
            return ""
        title = specific_song['title']
        if self.repeat_event.is_set():
            title += " *+*"
        if self.pause_event.is_set():
            title += " *=*"
        return title

    def pause_after_mixer_ready(self, paused):
        self.hold_thread_until_mixer()
        self.pause(not paused)

    def hold_thread_until_mixer(self):
        """
        Wait until the mixer is ready and playing music.
        """
        while not AudioPlayer.get_busy():
            sleep(1)
        return True

#####################################################################################################

    def _clear_for_new_track(self):
        self.skip_flag.set()
        self.forward_stack = []
        self.shuffler.replay_queue = []  # Clear queue for new selection
        if self.current_index < len(self.shuffler.history) - 1:
            self.shuffler.history = self.shuffler.history[:self.current_index+1]
    
    def _queue_song(self, song):
        self.skip_flag.set()
        #AudioPlayer.stop() # pygame.mixer.music.stop()
        self.shuffler.enqueue_replay(song)

    def get_unique_song(self):
        # Delegate to SmartShuffler
        return self.shuffler.get_unique_song()

#####################################################################################################

    def resetRadio(self):
        try:
            self.radio_client.stopListening()
        except:
            pass
        # Possibly important I dont know lol I just removed it and everything fixed lol ##
        del self.radio_client
        self.radio_client = RadioClient(AudioPlayer, ip=self.current_radio_ip)
        
    def load_radio_ips(self, seconds_to_scan: int = 60):
        """
        Every seconds_to_scan (Default 60) Scan The Full List Of Available Radios
        Run Only In A Seperate Daemon Thread
        """
        def handle_callback_ip(ip, title, location):
            if not ip in self.full_radio_ip_list and not ip.__contains__("0.0.0.0"):
                self.full_radio_ip_list.append(ip)
                if self.current_radio_ip == "0.0.0.0":
                    self.current_radio_ip = ip
                self.set_ips(self.full_radio_ip_list)
        
        while True:
            self.radio_scanner.scan_all(handle_callback_ip)
            sleep(seconds_to_scan)

    def set_radio_ip(self, new_ip):
        if new_ip in self.full_radio_ip_list:
            self.toggle_loop_cycle()
            self.current_radio_ip = new_ip
            self.toggle_loop_cycle()
            return True
        else:
            return False

#####################################################################################################

    def core_handler(self):
        Thread(target=self.core_player_loop, daemon=True).start()
        Thread(target=self.load_radio_ips, daemon=True).start()
        Thread(target=self.core_radio_loop, daemon=True).start()
        
    def toggle_loop_cycle(self, CycleType: bool = None):
        """Toggle Based On The Bool: True = RadioPlayer, False = MusicPlayer"""
        didReset = False
        if CycleType is None:
            didReset = True
            CycleType = self.current_player_mode.is_set()
        self.current_player_mode.set() if CycleType else self.current_player_mode.clear()
        #self.resetRadio()
        self.set_lyrics(False)
        pauseType = self.pause_event.is_set()
        if CycleType:
            if not pauseType:
                self.pause()
        else:
            try:
                AudioPlayer.stop()
            except:
                ll.warn("Couldn't Wait For Mixer. Continue...")
            AudioPlayer.load(self.current_song['path'])
            if pauseType:
                self.pause()
            else:
                AudioPlayer.play()
            try:
                AudioPlayer.set_pos(self.song_elapsed_seconds)
            except:
                try:
                    AudioPlayer.play()
                    AudioPlayer.set_pos(self.song_elapsed_seconds)
                    AudioPlayer.unpause()
                except:
                    ll.warn("Error In Loading Music In Radio. Retrying")
                    if not didReset:
                        return self.toggle_loop_cycle(CycleType)

    def core_radio_loop(self):
        def lyric_callback(unformatted_return_lyrics: str, return_dilation, local_song_id):
            self.current_radio_id = local_song_id
            try:
                attemptCount = 0
                attemptTries = 3
                while (attemptCount := attemptCount + 1) <= attemptTries:
                    try:
                        lyric_data = requests.get(unformatted_return_lyrics, timeout=2)
                        lyric_data.raise_for_status()
                        if lyric_data.content != "b''":
                            ll.debug(f"Lyrics downloaded.")
                            break
                        else:
                            sleep(1)
                    except:
                        sleep(1)
                return_lyrics = ast.literal_eval(lyric_data.content.decode('utf-8'))
                if len(return_lyrics) > 0:
                    self.set_lyrics(True, "🎵")
                    for lyric_pair in return_lyrics:
                        if not local_song_id == self.current_radio_id:
                            break
                        if not self.current_player_mode.is_set():
                            self.set_lyrics(False)
                            break
                        while AudioPlayer.get_pos() + return_dilation < lyric_pair[0]: # While Less Than Required Time For Lyrics To Show
                            if not self.current_player_mode.is_set() or not local_song_id == self.current_radio_id:
                                break
                            sleep(0.1)
                        self.set_lyrics(True, lyric_pair[1])
                self.set_lyrics(False)
            except Exception as E:
                ll.error(f"Radio Lyric Callback Error With Data: {unformatted_return_lyrics} And Dilation {return_dilation:.2f}s And Error {E}")
                
        while True:
            self.current_player_mode.wait()
            self.resetRadio()
            try:
                listeningIp = self.current_radio_ip
                self.current_radio_id = ""
                self.radio_client.listenTo(listeningIp, lyric_callback)
                ll.print(f"Listening To {listeningIp}.")
            except Exception as e:
                ll.error(f"Radio met unexpected exception {e}")
                break
            
            self.set_lyrics(False)
            
            while True:
                if not self.current_player_mode.is_set() or listeningIp != self.current_radio_ip:
                    ll.print("Exiting Radio Loop.")
                    break
                RadioData = self.radio_client.get_client_data()
                self.set_duration(*RadioData['radio_duration'])
                self.set_screen(*RadioData['radio_text'].split("![]!"))
                sleep(1)
            try:
                self.radio_client.stopListening()
                ll.print(f"Stopped listening To {listeningIp}.")
            except:
                pass

    def core_player_loop(self):
        prev_song = None
        while True:
            try:
                self.skip_flag.clear()
                if self.shuffler.replay_queue:
                    song = self.get_unique_song()
                elif self.skip_flag.is_set() or not self.repeat_event.is_set() or prev_song is None:
                    song = self.get_unique_song()
                else:
                    song = prev_song
                prev_song = song
                if not song:
                    sleep(0.5)
                    continue

                # history and played lists maintained in shuffler, so skip duplicates here
                if not self.navigating_history:
                    self.shuffler.history = self.shuffler.history[:self.current_index+1]
                    if not self.shuffler.history or self.shuffler.history[-1] != song['path']:
                        self.shuffler.history.append(song['path'])
                        self.current_index = len(self.shuffler.history) - 1

                self.current_song = song
                self.current_song_id = str(song['title']) + str(time())
                self.set_screen(song['artist'], self.get_display_title())
                self.current_song_lyrics = ""

                current_rotation_count, max_current_rotation = 0, 5
                fullTitle = f"{song['artist']}![]!{song['title']}"

                def lyric_callback(return_lyrics, local_song_id):
                    if not local_song_id == self.current_song_id:
                        return
                    self.current_song_lyrics = return_lyrics
                    if len(return_lyrics) > 0:
                        self.set_lyrics(True, "🎵")
                        for lyric_pair in return_lyrics:
                            if not local_song_id == self.current_song_id:
                                self.set_lyrics(False)
                                break
                            while self.song_elapsed_seconds < lyric_pair[0]: # While Less Than Required Time For Lyrics To Show
                                if not local_song_id == self.current_song_id:
                                    break
                                sleep(0.5)
                            self.set_lyrics(True, lyric_pair[1])
                    else:
                        self.set_lyrics(False)

                # lyric thread
                Thread(target=self.lyricHandler.request,
                       args=(song['artist'], song['title'], lyric_callback, self.current_song_id), daemon=True).start()

                try:
                    AudioPlayer.load(song['path']) # pygame.mixer.music.load(song['path'])
                    AudioPlayer.set_volume(self.current_volume) # pygame.mixer.music.set_volume(self.current_volume)
                    
                    # Simplified resume logic - if we're resuming and this is the correct song
                    # In core_player_loop, replace the resume block with:
                    if getattr(self, "resume_pending", False) and self.current_song and self.current_song['path'] == song['path']:
                        start_pos = getattr(self, '_resume_position', 0.0)
                        AudioPlayer.play() # pygame.mixer.music.play()
                        try:
                            AudioPlayer.set_pos(start_pos) # pygame.mixer.music.set_pos(start_pos)
                        except Exception as e:
                            try:
                                AudioPlayer.play(start=start_pos) # pygame.mixer.music.play(start=start_pos)
                                ll.debug(f"Used alternative method to start at {start_pos:.2f}s")
                            except Exception as e:
                                ll.error(f"Alternative method also failed: {e}")
                        
                        self.resume_pending = False
                        if hasattr(self, '_resume_position'):
                            del self._resume_position
                    else:
                        start_pos = 0.0
                        AudioPlayer.play() # pygame.mixer.music.play()
                        self.hold_thread_until_mixer()
                        start_time = time() # Reset start_time after mixer is ready

                    # Now update the screen, after the music has actually started
                    self.set_screen(song['artist'], self.get_display_title())

                    total_duration = song["duration"]
                    
                    # Ensure start_time reflects our position
                    start_time = time() - start_pos
                    paused_duration = 0
                    
                    if current_rotation_count == 0:
                        self.radio_master.initSong(
                            title = fullTitle,
                            mp3_song_file_path = song['path'],
                            current_mixer = AudioPlayer,
                            current_song_lyrics = self.current_song_lyrics
                        )
                            
                    current_rotation_count = (current_rotation_count + 1) % max_current_rotation # Add One Else Loop Back
                    
                    last_save_time = 0
                    while time() - start_time - paused_duration < total_duration:
                        if self.skip_flag.is_set(): break
                        if self.pause_event.is_set():
                            self.radio_master.initSong(
                                title = fullTitle,
                                mp3_song_file_path = song['path'],
                                current_mixer = AudioPlayer,
                                current_song_lyrics = self.current_song_lyrics
                            )
                            pause_start = time()
                            AudioPlayer.pause()
                            self.save_playback_state()
                            while self.pause_event.is_set():
                                if self.skip_flag.is_set(): break
                                sleep(0.25)
                            paused_duration += time() - pause_start
                            AudioPlayer.unpause()
                        self.song_elapsed_seconds = time() - start_time - paused_duration
                        self.set_duration(self.song_elapsed_seconds, total_duration)
                        self.set_screen(song['artist'], self.get_display_title())
                        if time() - last_save_time > 1:
                            self.save_playback_state()
                            last_save_time = time()
                        sleep(0.25)

                except Exception as e:
                    self.set_screen("Error", song['title'])
                    ll.error(e)
                    sleep(1)
                finally:
                    AudioPlayer.stop()
                    self.current_song = None
                    self.song_elapsed_seconds = 0.0

            except Exception as e:
                ll.error(f"Core Player failed with an unhandled exception: {e}")
                sleep(1)

#####################################################################################################

def get_auto_directories(candidate_urls=[]):
    """Automatically detects existing GTAV Enhanced User Music directories"""
    valid_dirs = candidate_urls
    
    # Determine base path based on OS
    if platform.system() == 'Windows':
        base_path = Path(os.environ.get('USERPROFILE', Path.home()))
    else:
        base_path = Path.home()

    # List of potential directory candidates
    candidate_paths = [
        base_path / "Documents" / "Rockstar Games" / "GTAV Enhanced" / "User Music",
        base_path / "OneDrive" / "Documents" / "Rockstar Games" / "GTAV Enhanced" / "User Music",
        Path("/Volumes/Games/Rockstar Games/GTAV Enhanced/User Music"),
        Path.home() / "Games" / "GTAV Enhanced" / "User Music"
    ]
    
    # Check which directories actually exist
    for path in candidate_paths:
        if path.exists() and path.is_dir():
            valid_dirs.append(str(path.resolve()))

    return valid_dirs