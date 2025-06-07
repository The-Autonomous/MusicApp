import subprocess, json
import numpy as np
import sounddevice as sd
import soundfile as sf
from threading import Event, Thread, Lock
from collections import deque
from time import sleep

class AudioPlayerRoot:
    def __init__(self, buffer_size_seconds=10):
        self.stream = None
        self.samplerate = 44100
        self.channels = 2
        self.chunk_size = 1024
        #self.root_reading_thread = Lock()
        self.filepath = None

        self.paused = Event()
        self.stop_event = Event()
        self.play_event = Event() # Indicates if playback is active (stream running)
        self.volume = 0.1
        self.position_frames = 0
        self.total_frames = 0
        self.duration = 0
        
        self.stop_active = False # To track if stop was called to avoid redundant actions

        self.buffer = deque()
        self.buffer_size_seconds = buffer_size_seconds

        self._playback_thread = None
        self._reader_thread = None
        self._current_process = None # To hold the ffmpeg process

        # Add a lock for buffer access if multiple threads interact with it
        # (though in this design, reader adds, callback pops, so usually fine)
        # self._buffer_lock = Lock()

    def _get_audio_info(self, filepath):
        cmd = ['ffprobe', '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', filepath]
        creationflags = subprocess.CREATE_NO_WINDOW
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE

        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding='utf-8', errors='ignore',
                                creationflags=creationflags, startupinfo=startupinfo)
        if result.returncode != 0:
            raise RuntimeError(f"ffprobe error: {result.stderr}")
        info = json.loads(result.stdout)
        audio_stream = next((s for s in info['streams'] if s['codec_type'] == 'audio'), None)
        if audio_stream is None:
            raise ValueError("No audio stream found in file.")
        return {
            'samplerate': int(audio_stream['sample_rate']),
            'channels': int(audio_stream['channels']),
            'duration': float(info['format'].get('duration', 0.0))
        }

    def load(self, filepath):
        if filepath:
            # Load doesn't play immediately, so pause it right away
            self._start_playback_session(filepath, start_pos=0.0, play_immediately=False)

    def unload(self):
        self.stop()

    def play(self, filepath=None, start_pos=0.0):
        if filepath:
            self._start_playback_session(filepath, start_pos=start_pos, play_immediately=True)
        elif self.filepath:
            if self.paused.is_set():
                self.unpause()
            else: # If already playing or paused with no new file, restart from start_pos
                self._start_playback_session(self.filepath, start_pos=start_pos, play_immediately=True)
        else:
            print('No file loaded to play.')

    def _start_playback_session(self, filepath, start_pos, play_immediately):
        self.stop() # Ensure previous session is stopped and cleaned up

        self.filepath = filepath
        try:
            audio_info = self._get_audio_info(filepath)
            self.samplerate = audio_info['samplerate']
            self.channels = audio_info['channels']
            self.total_frames = int(audio_info['duration'] * self.samplerate)
            self.duration = audio_info['duration']
        except Exception as e:
            self.stop() # Clean up if info gathering fails
            return

        self.position_frames = int(start_pos * self.samplerate)
        self.buffer.clear() # Clear buffer for new session

        self.stop_event.clear() # Clear stop event for new session
        self.paused.set()       # Start paused until buffer is ready or explicit play is called

        # Start playback thread (which will start reader thread)
        self._playback_thread = Thread(target=self._run_stream, daemon=True)
        self._playback_thread.start()

        # Wait for reader thread to initialize and start filling
        while not self._reader_thread or not self._reader_thread.is_alive():
            sd.sleep(10) # Small sleep to avoid busy-waiting

        # Crucial addition for quick seeks to prevent underruns
        # Wait for a minimum buffer fill before allowing playback to truly start
        min_buffer_chunks = int(self.buffer_size_seconds * self.samplerate / self.chunk_size * 0.2) # Wait for 20% of buffer
        if min_buffer_chunks < 2: min_buffer_chunks = 2 # Ensure at least 2 chunks
        while len(self.buffer) < min_buffer_chunks and not self.stop_event.is_set() and self._reader_thread.is_alive():
            sd.sleep(10) # Small sleep to avoid busy-waiting

        if play_immediately:
            self.unpause() # This will truly start the audio stream output
        else:
            self.pause() # Keep paused as requested by load
        Thread(target=self.set_movement, args=(False,), daemon=True).start() # Reset movement state on stop

    def _run_stream(self):
        # This thread's main job is to manage the audio output stream
        # and ensure the reader thread is running.

        try:
            # Start reader thread here, passing the current position
            self._reader_thread = Thread(target=self._read_audio_chunks, daemon=True)
            self._reader_thread.start()

            # Wait for some initial buffer to be filled before starting the stream
            # This helps prevent initial underruns, especially on fresh starts or seeks.
            initial_buffer_wait_chunks = int(self.buffer_size_seconds * self.samplerate / self.chunk_size * 0.1) # 10% of buffer
            if initial_buffer_wait_chunks < 2: initial_buffer_wait_chunks = 2
            while len(self.buffer) < initial_buffer_wait_chunks and not self.stop_event.is_set() and self._reader_thread.is_alive():
                sd.sleep(20) # A bit longer sleep for initial fill

            if self.stop_event.is_set():
                return

            self.stream = sd.OutputStream(samplerate=self.samplerate, channels=self.channels,
                                          dtype='float32', blocksize=self.chunk_size,
                                          latency='low', callback=self.callback)
            self.stream.start()
            self.play_event.set() # Indicate that the stream is active
            self.stop_event.wait() # Block until stop is called

        except Exception as e:
            print(f"[PlaybackThread] Error in _run_stream: {e}")
        finally:
            self.play_event.clear() # Clear play event
            if self.stream:
                try:
                    self.stream.stop()
                    self.stream.close()
                except Exception as e:
                    print(f"Error stopping/closing stream in finally: {e}")
                self.stream = None
            if self._reader_thread and self._reader_thread.is_alive():
                self._reader_thread.join(timeout=0.5) # Give reader a moment to finish

    def _read_audio_chunks(self):
        max_buffer_chunks = (self.samplerate * self.buffer_size_seconds) // self.chunk_size
        try:
            if not self.filepath:
                return

            is_mp3 = self.filepath.lower().endswith('.mp3')
            if is_mp3:
                # Start ffmpeg from the current position_frames
                start_time_seconds = self.position_frames / self.samplerate
                ffmpeg_cmd = [
                    'ffmpeg', '-ss', str(start_time_seconds), '-i', self.filepath,
                    '-loglevel', 'error', '-f', 'f32le', '-acodec', 'pcm_f32le',
                    '-ar', str(self.samplerate), '-ac', str(self.channels), 'pipe:1'
                ]
                creationflags = subprocess.CREATE_NO_WINDOW
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE

                process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE,
                                           creationflags=creationflags,
                                           startupinfo=startupinfo)
                self._current_process = process
                bytes_per_frame = 4 * self.channels
                chunk_size_bytes = self.chunk_size * bytes_per_frame

                while not self.stop_event.is_set():
                    # --- Dynamic buffer management ---
                    current_buffer_len = len(self.buffer)
                    if current_buffer_len >= max_buffer_chunks:
                        # Buffer is full, slow down reading, but don't stop entirely
                        sd.sleep(50) # Sleep longer to let callback consume
                        continue
                    elif current_buffer_len > max_buffer_chunks * 0.8: # Nearing full, just a small pause
                        sd.sleep(10)
                        continue

                    raw_audio = process.stdout.read(chunk_size_bytes)
                    if not raw_audio:
                        break
                    chunk = np.frombuffer(raw_audio, dtype=np.float32)
                    if self.channels > 1:
                        chunk = chunk.reshape(-1, self.channels)
                    self.buffer.append(chunk)

                if process.poll() is None: # Check if process is still running
                    process.kill()
                    process.wait(timeout=0.1) # Give it a moment to terminate
                self._current_process = None
            else:
                with sf.SoundFile(self.filepath, 'r') as f:
                    f.seek(self.position_frames) # Seek to current position
                    while not self.stop_event.is_set():
                        current_buffer_len = len(self.buffer)
                        if current_buffer_len >= max_buffer_chunks:
                            sd.sleep(50)
                            continue
                        elif current_buffer_len > max_buffer_chunks * 0.8:
                            sd.sleep(10)
                            continue

                        chunk = f.read(self.chunk_size, dtype='float32', always_2d=True)
                        if len(chunk) == 0:
                            break
                        self.buffer.append(chunk)
        except Exception as e:
            print(f"[ReaderThread] Error: {e}")
        finally:
            # If reader finishes because of EOF and not stop_event, signal end of playback
            if not self.stop_event.is_set() and len(self.buffer) == 0:
                 self.stop_event.set() # Signal playback thread to stop

    def callback(self, outdata, frames, time, status):
        if status:
            print(f"Stream status: {status}")
        if self.paused.is_set() or not self.play_event.is_set(): # Check play_event too
            outdata.fill(0)
            return

        try:
            chunk = self.buffer.popleft()
            modified_chunk = chunk * self.volume
            outdata[:len(modified_chunk)] = modified_chunk
            if len(chunk) < len(outdata):
                # If the chunk was smaller than requested frames (e.g., end of file)
                outdata[len(chunk):] = 0
            self.position_frames += frames
        except IndexError:
            outdata.fill(0)
            self.stop_event.set() # This will stop the stream, ensuring clean exit
            raise sd.CallbackStop # Signal SoundDevice to stop the stream

    def stop(self):
        if self.get_movement():
            return
        Thread(target=self.set_movement, args=(True, False,), daemon=True).start() # Reset movement state on stop
        if self._current_process:
            try:
                self._current_process.kill()
                self._current_process.wait(timeout=0.1)
            except Exception as e:
                print(f"Error killing ffmpeg process: {e}")
            self._current_process = None

        if self.stop_event.is_set():
            return

        self.stop_event.set() # Signal threads to stop
        self.paused.set()     # Ensure it's paused if not already

        # Give threads a chance to finish cleanly
        if self._playback_thread and self._playback_thread.is_alive():
            self._playback_thread.join(timeout=1.0) # Give it a bit more time

        # Reader thread is joined by playback thread's finally block, but for safety:
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=0.5)

        self.buffer.clear()
        self.position_frames = 0
        self.paused.clear() # Clear for next playback session
        self.play_event.clear() # Clear play event

    def set_movement(self, active: bool = None, do_sleep: bool = True):
        """ Set the movement active state.
        If `active` is None, it defaults to False.
        If `do_sleep` is True, it will sleep for a short duration to allow the state to propagate.
        If `do_sleep` is False, it will not sleep, allowing immediate state change.
        This method is thread-safe and can be called from any thread.
        """
        self.stop_active = active or not self.stop_active # Default to False if None
        if do_sleep:
            sleep(0.1)

    def pause(self):
        if not self.paused.is_set():
            self.paused.set()
            # self.play_event.clear() # Don't clear play_event, stream is still running, just outputting silence

    def unpause(self):
        if self.paused.is_set():
            self.paused.clear()
            # self.play_event.set() # Don't set, it should already be set if stream is active

    def set_pos(self, seconds):
        if self.filepath:
            is_paused = self.paused.is_set()
            # Restart the session from the new position
            self._start_playback_session(self.filepath, start_pos=seconds, play_immediately=not is_paused)
        else:
            print("Cannot set position: no file loaded.")

    def get_pos(self):
        return self.position_frames / self.samplerate if self.samplerate else 0

    def get_duration(self):
        return self.duration

    def set_volume(self, volume):
        self.volume = max(0.0, min(1.0, float(volume)))

    def get_busy(self):
        # A bit more robust check: stream active AND not explicitly stopped/paused
        return self.play_event.is_set() and not self.paused.is_set() and self.stream and self.stream.active

    def get_movement(self):
        # Check if the stream is active and not paused
        return self.stop_active and self.play_event.is_set() and self.stream and self.stream.active

    def load_static_sound(self, audio_data=None, samplerate=None, channels=None):
        return AudioSound(audio_data=audio_data, samplerate=samplerate, channels=channels)
    
    def __repr__(self):
        return f"<AudioPlayerRoot: {self.filepath or 'No file loaded'}, Volume: {self.volume}, Position: {self.get_pos():.2f}s, Duration: {self.get_duration():.2f}s>"

class AudioSound:
    def __init__(self, filepath=None, audio_data=None, samplerate=None, channels=None):
        self._audio_data = None
        self._samplerate = 0
        self._channels = 0
        self._stream = None
        self._volume = 1.0
        self._current_frame = 0
        self._loop_count = 0
        self._stop_event = Event()
        self._playback_thread = None

        if filepath:
            self._load_from_file(filepath)
        elif audio_data is not None and samplerate is not None and channels is not None:
            self._load_from_array(audio_data, samplerate, channels)
        else:
            raise ValueError("Either 'filepath' or ('audio_data', 'samplerate', 'channels') must be provided.")

    def _load_from_file(self, filepath):
        try:
            data, samplerate = sf.read(filepath, dtype='float32')
            if data.ndim == 1:
                data = data[:, np.newaxis]
            self._audio_data = data
            self._samplerate = samplerate
            self._channels = data.shape[1]
            print(f"AudioSound: Loaded '{filepath}' ({self._audio_data.shape[0]} frames, {self._channels} channels, {self._samplerate} Hz)")
        except Exception as e:
            print(f"AudioSound: Error loading audio from file '{filepath}': {e}")
            self._audio_data = np.array([])

    def _load_from_array(self, audio_data, samplerate, channels):
        if audio_data.dtype != np.float32:
            audio_data = audio_data.astype(np.float32)
        if audio_data.ndim == 1:
            audio_data = audio_data[:, np.newaxis]
            if channels != 1:
                print(f"Warning: 1D audio_data provided, but channels={channels}.")
                channels = 1
        self._audio_data = audio_data
        self._samplerate = samplerate
        self._channels = channels
        print(f"AudioSound: Loaded raw audio data ({self._audio_data.shape[0]} frames, {self._channels} channels, {self._samplerate} Hz)")

    def play(self, loops=0, volume=None):
        if self._audio_data.size == 0:
            print("AudioSound: Cannot play, no audio data loaded.")
            return
        self.stop()
        self._loop_count = loops
        self._current_frame = 0
        self._stop_event.clear()
        if volume is not None:
            self._volume = max(0.0, min(1.0, volume))
        try:
            self._stream = sd.OutputStream(
                samplerate=self._samplerate,
                channels=self._channels,
                callback=self._playback_callback,
                finished_callback=lambda: self._stop_event.set() # Sets stop event when stream finishes naturally
            )
            self._stream.start()
            print(f"AudioSound: Playing (loops={loops}, volume={self._volume})")
            # Start a separate thread to wait for the stop event
            self._playback_thread = Thread(target=self._monitor_playback_finished, daemon=True)
            self._playback_thread.start()
        except Exception as e:
            print(f"AudioSound: Error starting playback: {e}")
            self._stop_event.set() # Ensure stop event is set on error

    def _playback_callback(self, outdata, frames, time_info, status):
        if status:
            pass
        if self._stop_event.is_set():
            outdata.fill(0)
            raise sd.CallbackStop # Stop the stream if stop event is set

        frames_to_fill = frames
        current_fill_pos = 0

        while frames_to_fill > 0:
            remaining_audio_data = self._audio_data.shape[0] - self._current_frame
            chunk_to_copy_len = min(frames_to_fill, remaining_audio_data)

            if chunk_to_copy_len > 0:
                outdata[current_fill_pos : current_fill_pos + chunk_to_copy_len] = \
                    self._audio_data[self._current_frame : self._current_frame + chunk_to_copy_len] * self._volume
                self._current_frame += chunk_to_copy_len
                current_fill_pos += chunk_to_copy_len
                frames_to_fill -= chunk_to_copy_len
            else: # No more data in current loop iteration
                if self._loop_count == -1: # Infinite loop
                    self._current_frame = 0 # Rewind
                    continue # Try filling again from beginning
                elif self._loop_count > 0:
                    self._loop_count -= 1
                    self._current_frame = 0 # Rewind
                    continue # Try filling again from beginning
                else: # No more loops, end of audio
                    outdata[current_fill_pos:frames] = 0 # Fill remaining with silence
                    self._stop_event.set() # Signal playback finished
                    raise sd.CallbackStop # Stop the SoundDevice stream

    def _monitor_playback_finished(self):
        # This thread just waits for the stop event
        self._stop_event.wait()
        if self._stream and self._stream.active:
            try:
                self._stream.stop()
                self._stream.close()
                self._stream = None
                print("AudioSound: Playback finished/stopped.")
            except Exception as e:
                print(f"AudioSound: Error stopping/closing stream in monitor: {e}")

    def stop(self):
        if self._stop_event.is_set():
            return
        self._stop_event.set() # Signal the callback and monitor thread to stop
        if self._playback_thread and self._playback_thread.is_alive():
            self._playback_thread.join(timeout=0.5) # Give monitor thread a chance to clean up

    def set_volume(self, volume):
        self._volume = max(0.0, min(1.0, volume))

    def get_busy(self):
        # A bit more robust check for AudioSound as well
        return self._stream and self._stream.active and not self._stop_event.is_set()

AudioPlayer = AudioPlayerRoot()