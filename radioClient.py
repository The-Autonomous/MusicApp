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
        self.client_data = {'radio_text': '', 'radio_text_clean': '', 'radio_duration': [0, 0]} # [current position, total song duration]
        self._running = Event()
        self._paused = False
        self._repeat = False
        self._channel_changed = False
        self.AudioPlayer = audio_player
        self._ip = ip
        self._callback = None
        self._handled = False
        self._accept_host_eq = True
        self._original_eq_state = None  # Will store original EQ when we start accepting
        self._original_volume = None  # Will store original volume
        self._has_stored_original = False  # Track if we've saved the original state
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

    def get_client_data(self):
        return self.client_data

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
        """
        Enhanced stop that restores local EQ before clearing the running flag.
        """
        # Restore local EQ if we were accepting host EQ
        if self._accept_host_eq:
            self._restore_local_eq()
            self._accept_host_eq = False  # Reset to not accepting
        
        # Clear the running flag to stop the update loop
        self._running.clear()
        ll.debug("Stopped listening and restored local EQ")

    def _update_radio_title(self, title: str, duration: float = 0.0):
        """
        Update the client data with the current song title and state.
        """
        self.client_data['radio_text_clean'] = title
        self.client_data['radio_text'] = f"{title} {'*=*' if self._paused else ''} {"*+*" if self._repeat else ""}"
        self.client_data['radio_duration'][1] = duration
        
    def _apply_host_eq(self, eq_data, volume):
        """
        Apply host's EQ settings, storing original values on first application.
        Hot-patches the audio without modifying saved user preferences.
        """
        # Skip if client hasn't opted in or no EQ data
        if not self._accept_host_eq or not eq_data:
            return
        
        # Grace period: Skip EQ updates for 1.5 seconds after song starts
        if self._current_song_start_time and \
           (monotonic() - self._current_song_start_time) < 1.5:
            return
        
        # Skip during active downloads to prevent glitches
        if self._download_start_time and \
           (monotonic() - self._download_start_time) < 2.0:
            return
        
        try:
            # Validate EQ data ranges
            validated_eq = {
                int(freq): float(val) for freq, val in eq_data.items()
                if 20 <= int(freq) <= 15999 and -12 <= float(val) <= 12 and int(freq) != 16000
            }
            
            if not validated_eq:
                return

            # Store original state on first application
            if not self._has_stored_original:
                self._store_original_eq_state()
                self._has_stored_original = True

            current_eq = self.AudioPlayer.eq.get_gains() if hasattr(self.AudioPlayer, 'eq') and hasattr(self.AudioPlayer.eq, 'get_gains') else {}
            
            # This is the key change: create a new dictionary from current_eq with only the relevant keys
            verified_current_eq = {
                freq: gain for freq, gain in current_eq.items() if freq in validated_eq
            }
            
            # Apply the volume update if necessary
            if self.AudioPlayer.volume != volume and hasattr(self.AudioPlayer, 'set_volume') and 0 <= volume <= 1:
                self.AudioPlayer.set_volume(volume, set_directly=True)

            if validated_eq == verified_current_eq:
                #ll.debug("Skipping EQ and Volume update: settings are the same as current for all verified bands.")
                return

            # Apply the EQ update if necessary
            if hasattr(self.AudioPlayer, 'eq') and self.AudioPlayer.eq:
                for freq, gain_db in validated_eq.items():
                    self.AudioPlayer.eq.set_gain(freq, gain_db)
                
        except Exception as e:
            ll.error(f"Error applying host EQ: {e}")
    
    def _store_original_eq_state(self):
        """
        Store the current local EQ and volume settings before applying host settings.
        Only called once when starting to accept host EQ.
        """
        try:
            # Store original EQ bands
            if hasattr(self.AudioPlayer, 'eq') and self.AudioPlayer.eq:
                self._original_eq_state = {}
                # Get all available bands from the EQ
                if hasattr(self.AudioPlayer.eq, 'get_gains'):
                    self._original_eq_state = self.AudioPlayer.eq.get_gains().copy()
                else:
                    # Fallback: manually get common bands
                    for freq in [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000]:
                        if hasattr(self.AudioPlayer.eq, 'get_band'):
                            gain = self.AudioPlayer.eq.get_band(freq, (0.0, 1.0))
                            self._original_eq_state[freq] = gain[0] if isinstance(gain, tuple) else gain
            
            # Store original volume
            self._original_volume = self.AudioPlayer.volume
                
            ll.debug(f"Stored original EQ state: {len(self._original_eq_state)} bands, volume: {self._original_volume}")
            
        except Exception as e:
            ll.error(f"Error storing original EQ state: {e}")
    
    def _restore_local_eq(self):
        """
        Restore local EQ settings when disconnecting from host or disabling host EQ.
        """
        try:
            if not self._has_stored_original:
                ll.debug("No original EQ state stored; nothing to restore.")
                return  # Nothing to restore
            
            # Restore original EQ values
            if self._original_eq_state and hasattr(self.AudioPlayer, 'eq') and self.AudioPlayer.eq:
                ll.debug(f"Restoring {len(self._original_eq_state)} EQ bands to original values")
                for freq, original_gain in self._original_eq_state.items():
                    self.AudioPlayer.eq.set_gain(freq, original_gain)
                ll.debug(f"Restored {len(self._original_eq_state)} EQ bands to original values")
            
            # Restore original volume
            if self._original_volume is not None and hasattr(self.AudioPlayer, 'set_volume'):
                self.AudioPlayer.set_volume(self._original_volume, set_directly=True)
                ll.debug(f"Restored volume to {self._original_volume}")
            
            # Clear stored state
            self._original_eq_state = None
            self._original_volume = None
            self._has_stored_original = False
            
        except Exception as e:
            ll.error(f"Error restoring local EQ: {e}")
    
    def set_accept_host_eq(self, accept: bool):
        """
        Toggle whether to accept host's EQ settings.
        Automatically restores local EQ when disabling.
        """
        if self._accept_host_eq == accept:
            ll.debug("Host EQ acceptance state unchanged; no action taken.")
            return  # No change
        
        if not accept and self._accept_host_eq:
            # Switching from accepting to not accepting - restore local
            self._restore_local_eq()
        
        self._accept_host_eq = accept
        ll.debug(f"Host EQ acceptance set to: {accept}")
        
    def _update_loop(self):
        first_run = True
        while self._running.is_set():
            server_pos, data = -1.0, None
            try:
                data = self._fetch_data()
                if not data:
                    ll.warn("No data received from radio host. Retrying...")
                    sleep(self.update_interval)
                    continue

                if self._accept_host_eq:
                    self._apply_host_eq(data['eq'], data['volume'])
                    
                server_pos = data['location']

                is_paused = data['paused']
                if first_run: # Horrid Spaghetti to ensure first run sets repeat state
                    self._repeat = False
                    first_run = False
                else:
                    self._repeat = data['repeat']

                # Handle pause/unpause state transitions
                if is_paused and not self._paused:
                    self.AudioPlayer.pause()
                    self._pause_start_time = monotonic()
                    self._paused = True

                elif not is_paused and self._paused:
                    self.AudioPlayer.unpause()
                    if self._pause_start_time is not None:
                        # Add total duration of that completed pause
                        self._total_pause_duration_for_current_song += (
                            monotonic() - self._pause_start_time
                        )
                    self._pause_start_time = None
                    self._paused = False

                # Handle song change
                if data['title'] != self.client_data['radio_text_clean']:
                    self._total_pause_duration_for_current_song = 0.0
                    self._pause_start_time = None
                    self._paused = False
                    self._current_song_start_time = None
                    self._current_song_start_server_pos = 0.0
                    self._handle_song_change(data)

                # Update title/duration
                self._update_radio_title(data['title'], data['duration'])

                # Playback position logic
                client_pos = self.client_data['radio_duration'][0]  # keep last known by default
                if self._current_song_start_time is not None and not self._paused:
                    elapsed_active_time = (
                        (monotonic() - self._current_song_start_time)
                        - self._total_pause_duration_for_current_song
                    )
                    client_pos = self._current_song_start_server_pos + elapsed_active_time

                    # Clamp
                    if data['duration'] > 0:
                        client_pos = min(client_pos, data['duration'])
                        client_pos = max(client_pos, 0.0)

                    # Re-sync if drift detected
                    if (
                        abs(client_pos - server_pos) > self.sync_threshold
                        and abs(client_pos - server_pos)
                        <= data['duration'] - self.sync_threshold
                    ):
                        ll.debug(
                            f"🔄 Resyncing due to drift: "
                            f"Client {client_pos:.2f}s, Server {server_pos:.2f}s "
                            f"(Diff: {abs(client_pos - server_pos):.2f}s)"
                        )
                        self._resync_playback(data['url'], server_pos, data['buffered_at'])
                        client_pos = server_pos  # snap after resync

                # Update display position
                self.client_data['radio_duration'][0] = client_pos
                
            except requests.exceptions.ConnectionError:
                ll.warn(
                    f"Connection to radio host at {self._ip} lost. Retrying in {self.update_interval}s..."
                )
                self.AudioPlayer.pause()
                self._paused = True
            except Exception as e:
                ll.error(f"Error in _update_loop: {e}")

            sleep(self.update_interval)
            
    def _fetch_data(self):
        try:
            if self._ip == "0.0.0.0": return None
            response = requests.get(f"http://{self._ip}:8080", timeout=self.update_interval)
            response.raise_for_status()
            content = response.text

            def extract(pattern, default):
                match = re.search(pattern, content)
                return match.group(1) if match else default

            title = extract(r"<title>(.*?)</title>", "Unknown Song")
            paused = extract(r"<paused>(.*?)</paused>", "False") == "True"
            repeat = extract(r"<repeat>(.*?)</repeat>", "False") == "True"
            location = float(extract(r"<location>(.*?)</location>", "0") or 0)
            duration = float(extract(r"<duration>(.*?)</duration>", "0") or 0)
            url = extract(r"<url>(.*?)</url>", "/song")
            buffered_at = float(extract(r"<buffered_at>(.*?)</buffered_at>", "0") or 0)
            eq_string = extract(r"<eq>(.*?)</eq>", "")
            volume = float(extract(r"<volume>(.*?)</volume>", "1.0"))

            eq_data = {}
            if len(eq_string) > 0:
                for pair in eq_string.split(','):
                    if ':' in pair:
                        freq, val = pair.split(':')
                        eq_data[int(freq)] = float(val)
                        
            return {
                "title": title,
                "repeat": repeat,
                "paused": paused,
                "location": location,
                "duration": duration,
                "url": url,
                "buffered_at": buffered_at,
                "eq": eq_data,
                "volume": volume
            }

        except requests.exceptions.Timeout:
            ll.warn("Request to radio host timed out.")
            return None
        except Exception as e:
            ll.error(f"Error fetching data: {e}")
            return None

    def _handle_song_change(self, data):
        ll.debug(f"🎵 New song: {data['title']} at server position: {data['location']:.2f}s, buffered at: {data['buffered_at']:.2f}s")
        
        # Update client data
        self._update_radio_title(data['title'], data['duration'])

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
                    sleep(1)

            with open(self.temp_song_file, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if not self._running.is_set(): # Allow stopping during download
                        ll.warn("Download interrupted: client stopped.")
                        return
                    f.write(chunk)
            ll.debug(f"Download complete: {self.temp_song_file}")

            # Calculate how much time has elapsed since we got the server data
            time_elapsed_during_download = monotonic() - self._download_start_time
            
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
                buffer_time=None
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

        # Re-play from the corrected server location
        self._current_song_start_time = self.AudioPlayer.radio_play(
            filepath=self.temp_song_file, 
            start_pos=corrected_resync_pos, 
            buffer_time=buffered_at
        )
        self._current_song_start_server_pos = corrected_resync_pos