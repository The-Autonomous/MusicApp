import requests, pygame, re, os
from threading import Thread, Event, Timer, Lock
import numpy as np
from mutagen.mp3 import MP3
from time import time, sleep, monotonic

class RadioClient:
    def __init__(self, ip: str = ""):
        self.client_data = {'radio_text': '', 'radio_duration': [0, 0]}
        self._running = Event()
        self._paused = False
        self._repeat = False
        self._pause_time = None
        self._channel_changed = False
        self._can_have_pygame = Lock()
        self._ip = ip
        self._callback = None
        self._handled = False
        self.update_interval = 0.5
        self.sync_threshold = 1.0
        self.temp_song_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache.mp3")
        self._start_time = None
        self._start_offset = 0.0
        self.static_noise = self.generate_static()

    def generate_static(self, duration_ms=500):
        """
        Generate white-noise static matching the currently initialized mixer settings.
        Will produce mono or stereo noise automatically, never mismatching channel count.
        """
        # 1) Make sure mixer is init’d; if not, init with defaults
        init = pygame.mixer.get_init()
        if init is None:
            pygame.mixer.init()  
            init = pygame.mixer.get_init()
        
        freq, size, channels = init    # e.g. (44100, -16, 2)
        bits = abs(size)               # 16
        max_amp = 2**(bits - 1) - 1    # 32767 for 16-bit
        
        # 2) Compute sample count from the mixer’s actual sample rate
        num_samples = int(freq * (duration_ms / 1000.0))
        
        # 3) Generate mono float32 noise
        mono = np.random.uniform(-1.0, 1.0, size=num_samples).astype(np.float32)
        
        # 4) Duplicate into the correct # of channels (underfit if channels>2 by reusing mono)
        if channels == 1:
            data = mono
        else:
            # for stereo (2) or more, tile the mono across channels
            data = np.tile(mono[:, None], (1, channels))
        
        # 5) Scale to integer PCM range and appropriate dtype
        dtype = np.int16 if bits > 8 else np.int8
        pcm = (data * max_amp).astype(dtype)
        
        # 6) Wrap in a Sound and apply volume
        sound = pygame.sndarray.make_sound(pcm)
        return sound

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
        with self._can_have_pygame:
            self._running.clear()
            #if self.remTmpFile():
            #    return
            #else:
            #    pygame.mixer.music.stop()
            #    pygame.mixer.music.unload()
            #    self.remTmpFile()
            #    return

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
                pause_dilation = 0
                
                if data['title'].endswith("***[]*Paused"):
                    pygame.mixer.music.pause()
                    if not self._pause_time:
                        self._pause_time = time()
                    pause_dilation = time() - self._pause_time
                    self._paused = True
                elif self._paused:
                    pygame.mixer.unpause()
                    self._pause_time = None
                    self._paused = False
                self._repeat = data['title'].endswith(" *+*")

                if data['title'] != self.client_data['radio_text'] and not self._repeat:
                    self._handle_song_change(data, self._start_download_offset)
                elif pygame.mixer.music.get_busy() or self._paused:
                    try:
                        # Replacing get_pos with manual time tracking
                        if self._start_time is not None:
                            client_pos = self._start_offset + (time() - self._start_time) - pause_dilation
                        else:
                            client_pos = 0.0

                        if self._repeat and abs(self.client_data['radio_duration'][1] - (client_pos + 5)) < 5 and not self._handled:
                            self._handled = True
                            Timer(5.0, self._update_playback_position, args=(0,)).start()
                            continue

                        if client_pos >= 0 and abs(server_pos - client_pos) > self.sync_threshold and not self._repeat and not self._paused:
                            print(f"Sync Triggered: Server={server_pos:.1f}s, Client={client_pos:.1f}s, Diff={abs(server_pos - client_pos):.1f}s")
                            self._update_playback_position(server_pos)
                            self.client_data['radio_duration'][0] = server_pos
                        else:
                            self.client_data['radio_duration'][0] = client_pos

                    except pygame.error as e:
                        print(f"Pygame error getting position: {e}")

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
            pygame.mixer.music.unload()
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
            pygame.mixer.music.load(self.temp_song_file)
            audio = MP3(self.temp_song_file)
            self.client_data['radio_duration'][1] = audio.info.length
            pygame.mixer.music.play()
            timeDilation = (monotonic() - buffer_time_frame)
            startPositionMath = start_position + timeDilation
            print(f"Playing song from position: {startPositionMath:.2f}s from dilation {timeDilation:.2f}s")
            pygame.mixer.music.set_pos(startPositionMath)
            
            if self._paused:
                pygame.mixer.music.pause()

            # Set accurate timer values
            self._start_time = time()
            self._start_offset = startPositionMath
            Thread(target=self._callback, args=(lyric_url, self._start_offset, self.client_data['radio_text'],), daemon=True).start()

        except requests.exceptions.RequestException as e:
            print(f"Song download error: {str(e)}")
            self.stopListening()
        except pygame.error as e:
            print(f"Pygame playback error: {str(e)}")
            self.stopListening()
        except Exception as e:
            print(f"General playback error: {str(e)}")
            self.stopListening()

    def _update_playback_position(self, new_position):
        try:
            if pygame.mixer.music.get_busy():
                if new_position < 0:
                    print(f"Skipping seek to invalid position: {new_position:.1f}s")
                    return

                pygame.mixer.music.set_pos(new_position)
                self._start_time = time()
                self._start_offset = new_position
                print(f"Jumped to position: {new_position:.1f}s")
                self._handled = False
            else:
                print("Skipping position update: Music not busy.")
        except pygame.error as e:
            print(f"Pygame position update error (set_pos): {str(e)}")
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
