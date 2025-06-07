import requests, re, os
from threading import Thread, Event, Timer, Lock
import numpy as np
from mutagen.mp3 import MP3
from time import time, sleep, monotonic

class RadioClient:
    def __init__(self, audio_player, ip: str = ""):
        self.client_data = {'radio_text': '', 'radio_duration': [0, 0]} # [current position, [current song position, current song duration]]
        self._running = Event()
        self._paused = False
        self._repeat = False
        self._pause_time = None
        self._channel_changed = False
        self.AudioPlayer = audio_player
        self._ip = ip
        self._callback = None
        self._handled = False
        self.update_interval = 0.5
        self.sync_threshold = 1.0
        self.temp_song_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache.mp3")
        self._start_time = None
        self._start_offset = 0.0
        self._total_pause_duration = 0.0
        self._pause_start_time = None
        self.static_noise = self.generate_static()


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
            print("⚠️ AudioPlayer not initialized or samplerate not set. Cannot generate static.")
            return np.array([]) # Return empty array if player not ready

        samplerate = self.AudioPlayer.samplerate
        channels = self.AudioPlayer.channels
        
        # Calculate number of frames for the desired duration
        num_frames = int(samplerate * (duration_ms / 1000.0))

        # Generate white noise (random samples between -1.0 and 1.0)
        # Reshape for stereo if channels > 1
        if channels == 1:
            static_data = np.random.uniform(-0.5, 0.5, size=num_frames).astype(np.float32)
        else:
            static_data = np.random.uniform(-0.5, 0.5, size=(num_frames, channels)).astype(np.float32)
        
        print(f"Generated {duration_ms}ms of static noise (Samplerate: {samplerate}, Channels: {channels}, Frames: {num_frames}).")
        return self.AudioPlayer.load_static_sound(static_data, self.AudioPlayer.samplerate, self.AudioPlayer.channels)

    def listenTo(self, ip, lyric_callback = None):
        self._ip = ip
        self._callback = lyric_callback
        if not self._running.is_set():
            if os.path.exists(self.temp_song_file):
                try:
                    os.remove(self.temp_song_file)
                except OSError as e:
                    print(f"Warning: Could not remove old temp file: {e}")

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
            client_pos = -1.0
            try:
                data = self._fetch_data()
                if not data:
                    sleep(self.update_interval)
                    continue

                server_pos = data['location']

                is_paused = data['title'].endswith("***[]*Paused")

                if is_paused and not self._paused:
                    self.AudioPlayer.pause() # pygame.mixer.music.pause()
                    self._pause_time = time() # This was already here for some reason, we'll use a new one
                    self._pause_start_time = time() # <<< NEW: Mark the start of this pause
                    self._paused = True
                elif not is_paused and self._paused:
                    self.AudioPlayer.unpause() # pygame.mixer.music.unpause()
                    # <<< NEW: Accumulate pause duration when unpausing
                    if self._pause_start_time is not None:
                        self._total_pause_duration += (time() - self._pause_start_time)
                    self._pause_start_time = None # Reset pause start time
                    self._pause_time = None
                    self._paused = False
                self._repeat = data['title'].endswith(" *+*")

                if data['title'] != self.client_data['radio_text'] and not self._repeat:
                    # When a new song starts, reset the pause duration
                    self._total_pause_duration = 0.0 # <<< NEW: Reset for new song
                    self._pause_start_time = None # Ensure this is also reset
                    self._handle_song_change(data, self._start_download_offset)
                elif self.AudioPlayer.get_busy() or self._paused: # pygame.mixer.music.get_busy() or self._paused:
                    try:
                        # Replacing get_pos with manual time tracking
                        if self._start_time is not None:
                            # <<< MODIFIED: Subtract total_pause_duration
                            # If currently paused, add the duration of the current pause to the total
                            current_effective_pause_duration = self._total_pause_duration
                            if self._paused and self._pause_start_time is not None:
                                current_effective_pause_duration += (time() - self._pause_start_time)

                            client_pos = self._start_offset + (time() - self._start_time) - current_effective_pause_duration
                            print(client_pos)
                        else:
                            client_pos = 0.0

                        if self._repeat and abs(self.client_data['radio_duration'][1] - (client_pos + 5)) < 5 and not self._handled:
                            self._handled = True
                            Timer(5.0, self._update_playback_position, args=(0,)).start()
                            continue

                        duration = self.client_data['radio_duration'][1]
                        if duration - self.sync_threshold - 1 >= client_pos and client_pos >= 0 and abs(server_pos - client_pos) > self.sync_threshold and not self._repeat and not self._paused:
                            print(f"Sync Triggered: Server={server_pos:.1f}s, Client={client_pos:.1f}s, Diff={abs(server_pos - client_pos):.1f}s")
                            self._update_playback_position(server_pos)
                            self.client_data['radio_duration'][0] = server_pos
                        else:
                            self.client_data['radio_duration'][0] = client_pos

                    except Exception as e:
                        print(f"Error getting position: {e}")

            except Exception as e:
                print(f"Error in update loop: {str(e)}")
                print(f"State at error: Server Pos={server_pos:.1f}s, Client Pos={client_pos:.1f}s")
                self.stopListening()

            sleep(self.update_interval)

    def _fetch_data(self):
        try:
            self._start_download_offset = monotonic()
            response = requests.get(f"http://{self._ip}:8080", timeout=1)
            response.raise_for_status()
            content = response.text

            title_match = re.search(r'<title>(.*?)</title>', content)
            location_match = re.search(r'<location>(.*?)</location>', content)

            if not all([title_match, location_match]):
                print(f"Error: Could not parse all required fields from server response: {content[:200]}...")
                return None

            title = title_match.group(1)
            location_str = location_match.group(1)

            try:
                location = float(location_str)
                if location < 0:
                    print(f"Warning: Received negative location: {location}")
                    return None
            except ValueError:
                print(f"Invalid location format received: {location_str}")
                return None

            return {
                'title': title,
                'location': location,
                'song_url': f"http://{self._ip}:8080/song",
                'lyric_url': f"http://{self._ip}:8080/lyrics"
            }
        except requests.exceptions.Timeout:
            print("Fetch error: Request timed out.")
            return None
        except requests.exceptions.RequestException as e:
            print(f"Fetch error: {str(e)}")
            return None
        except Exception as e:
            print(f"Data processing error: {str(e)}")
            return None

    def _handle_song_change(self, data, dilation_data):
        print(f"Song changed to {data['title']} with Dilation: {dilation_data:.2f}")
        self.client_data['radio_text'] = data['title']
        self._play_song(data['song_url'], data['lyric_url'], data['location'], dilation_data)

    def _play_song(self, song_url, lyric_url, start_position, buffer_time_frame):
        try:
            print(f"Downloading song: {song_url}, {lyric_url}")
            self.AudioPlayer.stop() # pygame.mixer.music.unload()
            if not self._channel_changed:
                self.static_noise.set_volume(0.01)
                self.static_noise.play(loops=-1)

            if os.path.exists(self.temp_song_file):
                try:
                    os.remove(self.temp_song_file)
                except OSError as e:
                    print(f"Warning: Could not remove old temp file before download: {e}")

            song_data = requests.get(song_url, stream=True, timeout=10)
            song_data.raise_for_status()

            with open(self.temp_song_file, "wb") as f:
                for chunk in song_data.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            print(f"Song downloaded.")
            
            if not self._channel_changed:
                self._channel_changed = True
                self.static_noise.stop()
            audio = MP3(self.temp_song_file)
            self.client_data['radio_duration'][1] = audio.info.length
            self.AudioPlayer.load(self.temp_song_file) # pygame.mixer.music.load(self.temp_song_file)
            self.AudioPlayer.play() # pygame.mixer.music.play()
            
            # Wait until music is actually playing before seeking
            waited = 0
            while not self.AudioPlayer.get_busy() and waited < 10: # not pygame.mixer.music.get_busy() and waited < 10:
                sleep(0.01)
                waited += 0.01

            if self.AudioPlayer.get_busy(): # pygame.mixer.music.get_busy():
                try:
                    self._start_offset = float(start_position + (self.AudioPlayer.radio_play(start_pos=start_position, buffer_time=buffer_time_frame) - buffer_time_frame))
                    print(f"Playing song from position: {self._start_offset:.2f}s")
                except Exception as e:
                    print(f"set_pos failed: {e}")
            else:
                print("Warning: Music did not start in time, skipping seek.")
            
            if self._paused:
                self.AudioPlayer.pause() # pygame.mixer.music.pause()

            # Set accurate timer values
            self._start_time = time()
            Thread(target=self._callback, args=(lyric_url, self._start_offset, self.client_data['radio_text'],), daemon=True).start()

        except requests.exceptions.RequestException as e:
            print(f"Song download error: {str(e)}")
            self.stopListening()
        except Exception as e:
            print(f"General playback error: {str(e)}")
            self.stopListening()

    def _update_playback_position(self, new_position):
        try:
            if self.AudioPlayer.get_busy(): # pygame.mixer.music.get_busy():
                if new_position < 0:
                    print(f"Skipping seek to invalid position: {new_position:.1f}s")
                    return

                self.AudioPlayer.set_pos(new_position) # pygame.mixer.music.set_pos(new_position)
                self._start_time = time()
                self._start_offset = new_position
                print(f"Jumped to position: {new_position:.1f}s")
                self._handled = False
            else:
                print("Skipping position update: Music not busy.")
        except Exception as e:
            print(f"General position update error: {str(e)}")

# Example usage
if __name__ == "__main__":
    def disconnect_handler(reason):
        print(f"Music disconnected: {reason}")

    client = RadioClient()
    client.listenTo("localhost", disconnect_handler)

    try:
        while True:
            sleep(1)
    except KeyboardInterrupt:
        print("\nStopping client...")
        client.stopListening()
        print("Client stopped.")
