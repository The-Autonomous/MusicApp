import requests, re, os
from threading import Thread, Event, Timer, Lock
import numpy as np
from mutagen.mp3 import MP3
from time import time, sleep, monotonic

try:
    from log_loader import log_loader
except:
    from .log_loader import log_loader

### Logging Handler ###

ll = log_loader("Radio Client")

#######################

class RadioClient:
    def __init__(self, audio_player, ip: str = ""):
        self.client_data = {'radio_text': '', 'radio_duration': [0, 0]} # [current position, total song duration]
        self._running = Event()
        self._paused = False
        self._repeat = False
        self._channel_changed = False
        self.AudioPlayer = audio_player
        self._ip = ip
        self._callback = None
        self._handled = False
        self.update_interval = 0.5
        self.sync_threshold = 1.0 # Threshold for re-syncing client position to server position
        self.temp_song_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache.mp3")

        # New/Revised time tracking variables for robust radio client playback
        self._current_song_start_time = None # Monotonic time when the *current song* started playing locally
        self._current_song_start_server_pos = 0.0 # The server's position when _current_song_start_time was recorded
        self._total_pause_duration_for_current_song = 0.0 # Accumulated pause time for the current song
        self._pause_start_time = None # Monotonic time when the *current pause* started
        
        # FIXED: Add timing synchronization variables
        self._download_start_time = None  # When we started downloading
        self._server_time_at_download = None  # Server's buffered_at when we started downloading


    def generate_static(self, duration_ms: int = 500) -> np.ndarray:
        """
        Generate white-noise static matching the AudioPlayer's settings.
        Will produce mono or stereo noise automatically, never mismatching channel count.
        
        Args:
            duration_ms (int): Duration of the static noise in milliseconds.
        
        Returns:
            np.ndarray: A NumPy array containing the generated static audio data (float, -1.0 to 1.0).
        """
        
        # Ensure AudioPlayer is initialized and has samplerate and channels
        if not self.AudioPlayer or not self.AudioPlayer.samplerate:
            ll.error("⚠️ AudioPlayer not initialized or samplerate not set. Cannot generate static.")
            return np.array([]) # Return empty array if player not ready

        samplerate = self.AudioPlayer.samplerate
        channels = self.AudioPlayer.channels
        
        # Calculate number of frames for the desired duration
        num_frames = int(samplerate * (duration_ms / 1000.0))

        # Generate white noise (random samples between -0.5 and 0.5)
        # Reshape for stereo if channels > 1
        if channels == 1:
            static_data = np.random.uniform(-0.5, 0.5, size=num_frames).astype(np.float32)
        else:
            static_data = np.random.uniform(-0.5, 0.5, size=(num_frames, channels)).astype(np.float32)
        
        ll.debug(f"Generated {duration_ms}ms of static noise (Samplerate: {samplerate}, Channels: {channels}, Frames: {num_frames}).")
        return self.AudioPlayer.load_static_sound(static_data, self.AudioPlayer.samplerate, self.AudioPlayer.channels)

    def listenTo(self, ip, lyric_callback = None):
        self._ip = ip
        self._callback = lyric_callback
        if not self._running.is_set():
            if os.path.exists(self.temp_song_file):
                try:
                    os.remove(self.temp_song_file)
                except OSError as e:
                    ll.warn(f"Warning: Could not remove old temp file: {e}")

            self._running.set()
            Thread(target=self._update_loop, daemon=True).start()

    def remTmpFile(self):
        if os.path.exists(self.temp_song_file):
            try:
                os.remove(self.temp_song_file)
                return True
            except:
                return False

    def stopListening(self):
        self._running.clear()

    def _update_loop(self):
        while self._running.is_set():
            server_pos = -1.0
            data = None # Initialize data to None
            try:
                data = self._fetch_data()
                if not data:
                    sleep(self.update_interval)
                    continue

                server_pos = data['location']

                is_paused = data['paused']

                # Handle pause/unpause state transitions
                if is_paused and not self._paused:
                    self.AudioPlayer.pause()
                    self._pause_start_time = monotonic() # Mark the start of THIS pause
                    self._paused = True
                elif not is_paused and self._paused:
                    self.AudioPlayer.unpause()
                    if self._pause_start_time is not None:
                        self._total_pause_duration_for_current_song += (monotonic() - self._pause_start_time)
                    self._pause_start_time = None
                    self._paused = False

                self._repeat = data['title'].endswith(" *+*")

                # Check for song change
                if data['title'] != self.client_data['radio_text'] and not self._repeat:
                    # New song detected
                    self._total_pause_duration_for_current_song = 0.0 # Reset for new song
                    self._pause_start_time = None # Ensure this is also reset
                    self._paused = False # Ensure client is not marked as paused when new song starts
                    self._current_song_start_time = None # Indicate no playback until _download_and_play sets it
                    self._current_song_start_server_pos = 0.0 # Reset server start pos
                    self._handle_song_change(data) # Call updated method

                # Update client_data radio_text and total duration regardless of song change
                self.client_data['radio_text'] = data['title'] if not is_paused else f"{data['title']} *=*"
                self.client_data['radio_duration'][1] = data['duration']

                # Calculate client_pos based on local playback and server sync
                client_pos = 0.0
                if self._current_song_start_time is not None:
                    # Calculate elapsed time on client, accounting for pauses
                    elapsed_active_time = (monotonic() - self._current_song_start_time) - self._total_pause_duration_for_current_song
                    if self._paused and self._pause_start_time is not None:
                        # If currently paused, subtract the duration of the *current, ongoing* pause from the elapsed time
                        # to get the position as if the song is paused.
                        elapsed_active_time -= (monotonic() - self._pause_start_time)

                    client_pos = self._current_song_start_server_pos + elapsed_active_time

                    # Clamping client_pos to song duration:
                    if data['duration'] > 0:
                        client_pos = min(client_pos, data['duration'])
                        client_pos = max(client_pos, 0.0) # Ensure it doesn't go below 0

                    # Re-sync logic: If client position deviates too much from server position
                    # This is crucial for handling repeated songs or desynchronization
                    if abs(client_pos - server_pos) > self.sync_threshold and abs(client_pos - server_pos) <= data['duration'] - self.sync_threshold:
                        ll.debug(f"🔄 Resyncing due to drift: Client {client_pos:.2f}s, Server {server_pos:.2f}s (Diff: {abs(client_pos - server_pos):.2f}s)")
                        self._resync_playback(data['url'], server_pos, data['buffered_at'])
                        # After resync, client_pos will be updated on the next loop iteration based on new _current_song_start_time
                        # For this iteration, we can just use the server_pos or re-calculate.
                        client_pos = server_pos # Assume instant sync for this display update

                self.client_data['radio_duration'][0] = client_pos # Update displayed current position

            except requests.exceptions.ConnectionError:
                ll.warn(f"Connection to radio host at {self._ip} lost. Retrying in {self.update_interval}s...")
                self.AudioPlayer.pause()
                self._paused = True # Mark as paused if connection is lost
            except Exception as e:
                ll.error(f"Error in _update_loop: {e}")
                # Consider adding self.stopListening() if critical error

            sleep(self.update_interval)

    def _fetch_data(self):
        try:
            response = requests.get(f"http://{self._ip}:8080", timeout=self.update_interval)
            response.raise_for_status()
            content = response.text
            title_match = re.search(r"<title>(.*?)</title>", content)
            paused_match = re.search(r"<paused>(.*?)</paused>", content)
            location_match = re.search(r"<location>(.*?)</location>", content)
            duration_match = re.search(r"<duration>(.*?)</duration>", content)
            url_match = re.search(r"<url>(.*?)</url>", content)
            buffered_at_match = re.search(r"<buffered_at>(.*?)</buffered_at>", content)

            if title_match and paused_match and location_match and duration_match and url_match and buffered_at_match:
                title = title_match.group(1)
                location = float(location_match.group(1))
                duration = float(duration_match.group(1))
                paused = bool(paused_match.group(1) == 'True')
                url = url_match.group(1)
                buffered_at = float(buffered_at_match.group(1))
                return {'title': title, 'paused': paused, 'location': location, 'duration': duration, 'url': url, 'buffered_at': buffered_at}
            return None
        except requests.exceptions.Timeout:
            ll.warn("Request to radio host timed out.")
            return None
        except Exception as e:
            ll.error(f"Error fetching data: {e}")
            return None

    def _handle_song_change(self, data):
        ll.debug(f"🎵 New song: {data['title']} at server position: {data['location']:.2f}s, buffered at: {data['buffered_at']:.2f}s")
        # Update client data
        self.client_data['radio_text'] = data['title']
        self.client_data['radio_duration'][1] = data['duration'] # Update total duration

        # FIXED: Record timing when we start the download/play process
        self._download_start_time = monotonic()
        self._server_time_at_download = data['buffered_at']

        # Download the new song in a separate thread
        Thread(target=self._download_and_play, args=(data['url'], data['location'], data['buffered_at']), daemon=True).start()

    def _download_and_play(self, url, server_location, buffered_at):
        try:
            if not self._running.is_set():
                ll.warn("Download cancelled: client stopped.")
                return

            if self.AudioPlayer.get_busy() or self._paused:
                self.AudioPlayer.stop() # Stop current playback if any

            ll.debug(f"Downloading: {url}")
            # Ensure the directory exists
            os.makedirs(os.path.dirname(self.temp_song_file), exist_ok=True)
            
            headers = {'Range': 'bytes=0-'} # Request the entire file
            for attempt in range(3): # Retry up to 3 times
                try:
                    response = requests.get(url, headers=headers, stream=True, timeout=10) # Added timeout for download
                    response.raise_for_status()
                    break
                except:
                    ll.error(f"Failed to download song from {url}; Attempt {attempt + 1}/3")
                    time.sleep(1)

            with open(self.temp_song_file, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if not self._running.is_set(): # Allow stopping during download
                        ll.warn("Download interrupted: client stopped.")
                        return
                    f.write(chunk)
            ll.debug(f"Download complete: {self.temp_song_file}")

            # FIXED: Calculate the correct start position accounting for download time
            download_completion_time = monotonic()
            
            # Calculate how much time has elapsed since we got the server data
            time_elapsed_during_download = download_completion_time - self._download_start_time
            
            # Calculate the corrected start position
            # The server was at 'server_location' when we got the data
            # Now we need to add the time that has passed during download
            corrected_start_pos = server_location + time_elapsed_during_download
            
            ll.debug(f"Timing correction: server_pos={server_location:.3f}, "
                    f"download_time={time_elapsed_during_download:.3f}, "
                    f"corrected_start_pos={corrected_start_pos:.3f}")

            # Play the song with corrected timing - don't pass buffered_at as buffer_time
            # since it's from a different time system (server vs client monotonic clocks)
            self._current_song_start_time = self.AudioPlayer.radio_play(
                filepath=self.temp_song_file, 
                start_pos=corrected_start_pos, 
                buffer_time=None  # FIXED: Don't pass server's buffered_at
            )
            
            self._current_song_start_server_pos = corrected_start_pos # Record the corrected starting point
            self._total_pause_duration_for_current_song = 0.0 # Reset for new song
            self._pause_start_time = None # Ensure it's reset
            self._paused = False # Ensure client is not marked as paused when new song starts playing
            
            ll.debug(f"Started playback from corrected position: {corrected_start_pos:.2f}s at client monotonic time: {self._current_song_start_time:.2f}s")

            # Callback for lyrics if available
            if self._callback and url.startswith("http"):
                try:
                    song_length = MP3(self.temp_song_file).info.length
                    self._callback(url.replace("song", "lyrics"), song_length, f"{self._current_song_start_time}")
                except Exception as e:
                    ll.warn(f"Warning: Could not get song length for lyrics callback: {e}")
        except Exception as e:
            ll.error(f"Error in _download_and_play: {e}")
            self.stopListening() # Stop if download/play fails

    # New method to handle resync, similar to _download_and_play but ensures current temp file is used
    def _resync_playback(self, url, new_server_location, buffered_at):
        ll.debug(f"Resyncing playback to {new_server_location:.2f}s using existing temp file.")
        
        # FIXED: Calculate timing for resync just like in _download_and_play
        resync_time = monotonic()
        
        # Since we're resyncing, we don't have download time, but we need to account 
        # for any time that may have passed since the server reported this position
        # For resync, we can assume minimal delay and use the server position directly
        corrected_resync_pos = new_server_location
        
        # Ensure AudioPlayer stops and is ready for a new play command
        self.AudioPlayer.stop() # This should cleanly stop the current audio stream

        # Reset pause tracking for the resync (as we are effectively starting fresh from this point)
        self._total_pause_duration_for_current_song = 0.0
        self._pause_start_time = None
        self._paused = False

        # Re-play from the corrected server location - don't pass server's buffered_at
        self._current_song_start_time = self.AudioPlayer.radio_play(
            filepath=self.temp_song_file, 
            start_pos=corrected_resync_pos, 
            buffer_time=None  # FIXED: Don't pass server's buffered_at
        )
        self._current_song_start_server_pos = corrected_resync_pos