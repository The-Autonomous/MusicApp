import subprocess, json, gc, weakref, os, tempfile, uuid
from threading import Event, Thread, Lock, RLock
from typing import Optional, Union
from time import sleep, monotonic
from functools import lru_cache
from collections import deque
import sounddevice as sd
import soundfile as sf
import numpy as np

try:
    from log_loader import log_loader
    from audio_eq import AudioEQ, AudioEcho
except:
    from .log_loader import log_loader
    from .audio_eq import AudioEQ, AudioEcho

### Logging Handler ###

ll = log_loader("Audio", debugging = False)

#######################

class OptimizedAudioBuffer:
    """Memory-efficient circular buffer with pre-allocated arrays"""
    
    def __init__(self, max_chunks: int, chunk_size: int, channels: int):
        self.max_chunks = max_chunks
        self.chunk_size = chunk_size
        self.channels = channels
        
        # Pre-allocate buffer memory to avoid constant allocation/deallocation
        self._buffer = np.zeros((max_chunks, chunk_size, channels), dtype=np.float32)
        self._chunk_sizes = np.zeros(max_chunks, dtype=np.int32)  # Track actual chunk sizes
        self._write_idx = 0
        self._read_idx = 0
        self._count = 0
        self._lock = Lock()
        
    def append(self, chunk: np.ndarray) -> bool:
        """Add chunk to buffer. Returns False if buffer is full."""
        with self._lock:
            if self._count >= self.max_chunks:
                return False
                
            actual_size = min(len(chunk), self.chunk_size)
            self._buffer[self._write_idx, :actual_size] = chunk[:actual_size]
            self._chunk_sizes[self._write_idx] = actual_size
            
            self._write_idx = (self._write_idx + 1) % self.max_chunks
            self._count += 1
            return True
    
    def popleft(self) -> Optional[np.ndarray]:
        """Remove and return oldest chunk."""
        with self._lock:
            if self._count == 0:
                return None
                
            chunk_size = self._chunk_sizes[self._read_idx]
            chunk = self._buffer[self._read_idx, :chunk_size].copy()
            
            self._read_idx = (self._read_idx + 1) % self.max_chunks
            self._count -= 1
            return chunk
    
    def clear(self):
        """Clear the buffer."""
        with self._lock:
            self._write_idx = 0
            self._read_idx = 0
            self._count = 0
    
    def __len__(self):
        return self._count
    
    @property
    def is_full(self):
        return self._count >= self.max_chunks
    
    @property
    def fill_ratio(self):
        return self._count / self.max_chunks

class AudioPlayerRoot:
    """Optimized audio player with efficient memory management and CPU usage"""
    
    def __init__(self, buffer_size_seconds: float = 10):
        # Core audio settings
        self.samplerate = 22050
        self.channels = 2
        self.chunk_size = 8192
        self.buffer_size_seconds = buffer_size_seconds
        self.eq = AudioEQ(self.samplerate, self.channels)
        self.echo = None  # off by default
        self._gaming_mode = True  # Bypass processing in gaming mode
        
        # State management
        self._state_lock = RLock()  # Use RLock for nested locking
        self._filepath = None
        self._stream = None
        self._current_process = None
        
        # Threading events - more efficient event handling
        self._stop_event = Event()
        self._paused = Event()
        self._play_event = Event()
        self._buffer_ready = Event()
        self._eof_event = Event()
        
        # Thread management with weak references to prevent memory leaks
        self._playback_thread = None
        self._reader_thread = None
        self._thread_pool = []  # Keep track of all threads for cleanup
        
        # Audio state
        self._volume = 0.1
        self._position_frames = 0
        self._total_frames = 0
        self._duration = 0.0
        
        # Performance tracking
        self._performance_stats = {
            'load_times': deque(maxlen=50),  # Keep only recent measurements
            'underruns': 0,
            'buffer_efficiency': 0.0
        }
        
        # Optimized buffer
        max_buffer_chunks = int((self.samplerate * buffer_size_seconds) / self.chunk_size)
        self._buffer = OptimizedAudioBuffer(max_buffer_chunks, self.chunk_size, self.channels)
        
        # FFmpeg process pool for better resource management
        self._process_startupinfo = self._create_startupinfo()
        
        # Movement state for thread synchronization
        self._movement_active = False
        self._movement_lock = Lock()

    @staticmethod
    def _create_startupinfo():
        """Create reusable startup info for subprocess calls"""
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        return startupinfo

    @lru_cache(maxsize=128)
    def _probe_audio_info(self, filepath: str, mtime: int = 0) -> dict:
        if mtime != 0:
            cmd = [
                'ffprobe', '-v', 'error', '-print_format', 'json',
                '-show_format', '-show_streams', filepath
            ]

            try:
                result = subprocess.run(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding='utf-8', errors='ignore',
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    startupinfo=self._process_startupinfo,
                    timeout=10
                )

                if result.returncode == 0:
                    info = json.loads(result.stdout)
                    audio = next((s for s in info['streams']
                                if s['codec_type'] == 'audio'), None)
                    if audio is None:
                        ll.error("No audio stream found.")
                        return
                    return {
                        'samplerate': int(audio['sample_rate']),
                        'channels'  : int(audio['channels']),
                        'duration'  : float(info['format'].get('duration', 0.0))
                    }

                ll.error(f"ffprobe error: {result.stderr}")

            except Exception as e:
                ll.error(f"ffprobe failed: {e}")

            # ---------- FALLBACK ----------
            try:
                with sf.SoundFile(filepath) as f:
                    return {
                        'samplerate': int(f.samplerate),
                        'channels'  : int(f.channels),
                        'duration'  : len(f) / f.samplerate
                    }
            except Exception as e2:
                ll.error(f"SoundFile probe failed: {e2}")

        # Last-ditch defaults (keep the app alive)
        return {'samplerate': 44100, 'channels': 2, 'duration': 0.0}

    def _get_audio_info(self, filepath: str) -> dict:
        return self._probe_audio_info(filepath, os.path.getmtime(filepath))

    def _process(self, chunk):
        chunk = self.eq.process(chunk)
        if self.echo:
            chunk = self.echo.process(chunk)
        return chunk
    
    def enable_echo(self, delay_ms=350, feedback=0.35, wet=0.5):
        if not self.echo:
            self.echo = AudioEcho(self.samplerate, self.channels,
                                  delay_ms, feedback, wet)

    def disable_echo(self):
        self.echo = None

    def set_echo(self, delay_ms=None, feedback=None, wet=None):
        if self.echo:
            self.echo.set_params(delay_ms, feedback, wet)

    def load(self, filepath: str) -> bool:
        """Load audio file without starting playback"""
        return self._start_playback_session(filepath, start_pos=0.0, play_immediately=False)

    def unload(self):
        """Unload current audio and free resources"""
        self.stop()
        with self._state_lock:
            self._filepath = None
            # Clear cache for the unloaded file
            self._get_audio_info.cache_clear()

    def play(self, filepath: Optional[str] = None, start_pos: float = 0.0) -> bool:
        """Play audio file or resume current playback"""
        if filepath:
            return self._start_playback_session(filepath, start_pos=start_pos, play_immediately=True)
        elif self._filepath:
            if self._paused.is_set():
                self.unpause()
                return True
            else:
                return self._start_playback_session(self._filepath, start_pos=start_pos, play_immediately=True)
        else:
            ll.warn('No file loaded to play.')
            return False

    def _start_playback_session(self, filepath: str, start_pos: float, play_immediately: bool, 
                            buffer_time: Optional[float] = None, radio_mode: bool = False) -> Union[bool, float]:
        """Optimized playback session initialization with fixed radio timing"""
        self.stop()  # Clean shutdown of previous session
        
        start_time = monotonic()
        
        with self._state_lock:
            self._filepath = filepath
            
            try:
                audio_info = self._get_audio_info(filepath)
                self.samplerate = audio_info['samplerate']
                self.channels = audio_info['channels']
                self._total_frames = int(audio_info['duration'] * self.samplerate)
                self._duration = audio_info['duration']
            except Exception as e:
                ll.error(f"Failed to get audio info: {e}")
                self.stop()
                return False if not radio_mode else 0.0

            # Calculate final position
            final_position = start_pos + 0.1
            solved_monotonic = monotonic()
            
            if radio_mode and buffer_time is not None:
                # FIXED: Radio mode timing calculation
                # Instead of mixing different time scales, calculate the time difference
                # between when the server buffered the data and now
                current_time = monotonic()
                time_since_buffer = current_time - buffer_time
                
                # Add the elapsed time since buffering to the start position
                # This accounts for the time that has passed since the server
                # buffered the audio data
                final_position = start_pos + time_since_buffer
                
                ll.debug(f"Radio timing: start_pos={start_pos:.3f}, "
                        f"buffer_time={buffer_time:.3f}, current_time={current_time:.3f}, "
                        f"time_since_buffer={time_since_buffer:.3f}, "
                        f"final_position={final_position:.3f}")
            
            # Use final_position for frame calculation
            self._position_frames = int(final_position * self.samplerate)
            
            # Reset state for new session
            self._buffer.clear()
            self._stop_event.clear()
            self._paused.set() if not play_immediately else self._paused.clear()
            self._buffer_ready.clear()
            
            # Start threads
            self._start_playback_threads(radio_mode)
            
            # Wait for buffer initialization
            if not radio_mode:
                self._wait_for_buffer_ready()
            
            if play_immediately:
                self.unpause()
            
            # Update performance stats
            load_time = monotonic() - start_time
            self._performance_stats['load_times'].append(load_time)
            
            # Start movement state management
            self._reset_movement_state()
            
            return solved_monotonic if radio_mode else True

    def radio_play(self, filepath, start_pos,
                  buffer_time: Optional[float] = None) -> float:
        """Optimized radio playback mode"""
        if filepath:
            return self._start_playback_session(
                filepath, start_pos=start_pos, play_immediately=True, 
                buffer_time=buffer_time, radio_mode=True
            )
        elif self._filepath:
            if self._paused.is_set():
                self.unpause()
                return monotonic()
            else:
                return self._start_playback_session(
                    self._filepath, start_pos=start_pos, play_immediately=True,
                    buffer_time=buffer_time, radio_mode=True
                )
        else:
            ll.warn('No file loaded for radio play.')
            return 0.0

    def _start_playback_threads(self, radio_mode: bool = False):
        """Start optimized playback threads"""
        # Start reader thread first
        self._reader_thread = Thread(
            target=self._read_audio_chunks_optimized,
            name="AudioReader",
            daemon=True
        )
        self._reader_thread.start()
        self._thread_pool.append(weakref.ref(self._reader_thread))
        
        # Start playback thread
        self._playback_thread = Thread(
            target=self._run_stream_optimized,
            args=(radio_mode,),
            name="AudioPlayback",
            daemon=True
        )
        self._playback_thread.start()
        self._thread_pool.append(weakref.ref(self._playback_thread))

    def _wait_for_buffer_ready(self):
        """Wait for minimum buffer fill before allowing playback"""
        min_buffer_ratio = 0.2  # Wait for 20% buffer fill
        timeout = 5.0  # Maximum wait time
        start_time = monotonic()
        
        while (self._buffer.fill_ratio < min_buffer_ratio and 
               not self._stop_event.is_set() and 
               self._reader_thread and self._reader_thread.is_alive() and
               (monotonic() - start_time) < timeout):
            sleep(0.01)  # Optimized sleep interval
        
        self._buffer_ready.set()

    def _run_stream_optimized(self, radio_mode: bool = False):
        """Optimized audio stream management"""
        try:
            # Wait for reader thread to be ready
            while (not self._reader_thread or not self._reader_thread.is_alive()) and not self._stop_event.is_set():
                sleep(0.001)
            
            if self._stop_event.is_set():
                return
            
            # Wait for initial buffer fill (except in radio mode)
            if not radio_mode:
                self._buffer_ready.wait(timeout=5.0)
            
            if self._stop_event.is_set():
                return
            
            # Create optimized audio stream
            self._stream = sd.OutputStream(
                samplerate=self.samplerate,
                channels=self.channels,
                dtype=np.float32,
                blocksize=self.chunk_size,
                latency='low',
                callback=self._audio_callback_optimized
            )
            
            self._stream.start()
            self._play_event.set()
            
            # Wait for stop signal
            self._stop_event.wait()
            
        except Exception as e:
            ll.error(f"Error in stream management: {e}")
        finally:
            self._cleanup_stream()

    def _audio_callback_optimized(self, outdata: np.ndarray, frames: int, time_info, status):
        """Highly optimized audio callback with minimal allocations and gaming mode support"""
        if status:
            ll.debug(f"Audio stream status: {status}")
            
        # Fast path for paused state - single check
        if self._paused.is_set() or not self._play_event.is_set():
            outdata.fill(0.0)
            return
        
        try:
            chunk = self._buffer.popleft()
            if chunk is None:
                # Buffer underrun handling
                outdata.fill(0.0)
                self._performance_stats['underruns'] += 1
                if not self._stop_event.is_set():
                    ll.debug("Buffer underrun detected")
                return
            
            # Channel handling with pre-computed conditions
            if chunk.ndim == 1:  # mono to stereo/multi
                chunk = chunk.reshape(-1, 1)
                if self.channels > 1:
                    # Use broadcasting instead of repeat for better performance
                    chunk = np.broadcast_to(chunk, (chunk.shape[0], self.channels)).copy()
            elif chunk.shape[1] != self.channels:
                if chunk.shape[1] == 1 and self.channels > 1:
                    # Broadcasting is faster than repeat
                    chunk = np.broadcast_to(chunk, (chunk.shape[0], self.channels)).copy()
                else:
                    # Slice to match channels (drops extras or pads with zeros)
                    chunk = chunk[:, :self.channels]
            
            # Gaming mode bypass - skip processing for minimal latency
            if not self._gaming_mode:
                chunk = self._process(chunk)
            
            # Optimized output handling
            chunk_len = len(chunk)
            if chunk_len >= frames:
                # Direct multiplication into output buffer
                np.multiply(chunk[:frames], self._volume, out=outdata)
            else:
                # Split operation to avoid temporary array creation
                outdata[:chunk_len] = chunk
                outdata[:chunk_len] *= self._volume
                # Only zero the remainder if needed
                if chunk_len < frames:
                    outdata[chunk_len:].fill(0.0)
            
            # Update position counter
            self._position_frames += frames
            
        except IndexError:
            # Buffer empty - more specific exception handling
            outdata.fill(0.0)
            self._performance_stats['underruns'] += 1
        except Exception as e:
            # General error handling
            ll.error(f"Error in audio callback: {e}")
            outdata.fill(0.0)

    #def _get_file_extension(self, filepath: str) -> str:
    #    """
    #    Extracts the file extension from a given file path.
    #    """
    #    return os.path.splitext(filepath)[1]

    def _read_audio_chunks_optimized(self):
        """Optimized audio reading with better memory management"""
        try:
            if not self._filepath:
                return
            
            is_mp3 = self._filepath.lower().endswith(('.mp3', '.m4a', '.aac'))
            
            if is_mp3:
                self._read_with_ffmpeg_memmap()
            else:
                self._read_with_soundfile()
                
        except Exception as e:
            ll.error(f"Error in audio reader: {e}")
        finally:
            # Signal end of file if not stopped explicitly
            if not self._stop_event.is_set() and len(self._buffer) == 0:
                self._stop_event.set()

    def _read_with_ffmpeg_memmap(self):
        """FFmpeg decode → temporary .f32 file → numpy memmap"""
        start_time_seconds = self._position_frames / self.samplerate
        tmp_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4().hex}.f32")

        ffmpeg_cmd = [
            "ffmpeg",
            "-ss", str(start_time_seconds),
            "-i", self._filepath,
            "-vn", "-sn", "-dn",
            "-c:a", "mp3_mpg123",
            "-f", "f32le",
            "-acodec", "pcm_f32le",
            "-ar", str(self.samplerate),
            "-ac", str(self.channels),
            "-y", tmp_path
        ]

        try:
            subprocess.run(
                ffmpeg_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
                startupinfo=self._process_startupinfo,
                check=True
            )

            # Now safe to mmap
            mm = np.memmap(tmp_path, dtype=np.float32, mode="r")
            frames = mm.size // self.channels
            audio_data = mm.reshape(frames, self.channels)

            frame_idx = 0
            while not self._stop_event.is_set() and frame_idx < frames:
                if self._buffer.is_full:
                    sleep(0.02)
                    continue

                chunk = audio_data[frame_idx:frame_idx + self.chunk_size]
                frame_idx += len(chunk)

                if len(chunk) == 0:
                    break
                if not self._buffer.append(chunk):
                    sleep(0.001)

        except Exception as e:
            ll.error(f"Error in ffmpeg memmap reader: {e}")

        finally:
            try:
                del mm
                gc.collect()
                os.unlink(tmp_path)
            except Exception as cleanup_e:
                ll.debug(f"Cleanup issue for {tmp_path}: {cleanup_e}")

    def _read_with_ffmpeg(self):
        """Optimized FFmpeg-based reading"""
        start_time_seconds = self._position_frames / self.samplerate
        
        ffmpeg_cmd = [
            'ffmpeg',
            '-ss',str(start_time_seconds), '-i', self._filepath,
            '-loglevel', 'panic',
            '-f', 'f32le',
            '-acodec', 'pcm_f32le',
            '-ar', str(self.samplerate), '-ac', str(self.channels),
            '-avoid_negative_ts', 'make_zero',  # Optimize timestamp handling
            #'-c:a', f'{self._get_file_extension(self._filepath)}_nvdec',
            'pipe:1'
        ]
        
        try:
            process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
                startupinfo=self._process_startupinfo,
                bufsize=self.chunk_size * 4 * self.channels * 64  # Larger buffer
            )
            
            self._current_process = process
            bytes_per_frame = 4 * self.channels
            chunk_size_bytes = self.chunk_size * bytes_per_frame
            
            # Pre-allocate read buffer
            read_buffer = bytearray(chunk_size_bytes)
            
            while not self._stop_event.is_set():
                # Dynamic buffer management
                if self._buffer.is_full:
                    sleep(0.02)  # Adaptive sleep
                    continue
                elif self._buffer.fill_ratio > 0.8:
                    sleep(0.005)
                    continue
                
                bytes_read = process.stdout.readinto(read_buffer)
                if not bytes_read:
                    break
                
                # Convert raw bytes to NumPy ---------------------------------
                chunk = np.frombuffer(read_buffer[:bytes_read], dtype=np.float32)

                # Always reshape to the stream's channel count
                frames_read = chunk.size // self.channels      # self.channels == -ac value
                chunk = chunk[:frames_read * self.channels].reshape(frames_read,
                                                                     self.channels)

                # Up-mix a mono file if the output stream is stereo
                if chunk.shape[1] == 1 and self.channels == 2:
                    chunk = np.repeat(chunk, 2, axis=1)        # (N,1) → (N,2)

                # -------------------------------------------------------------
                if not self._buffer.append(chunk):
                    sleep(0.001)  # buffer full – back off a hair

        finally:
            if self._current_process and self._current_process.poll() is None:
                try:
                    self._current_process.terminate()
                    self._current_process.wait(timeout=1.0)
                except:
                    self._current_process.kill()
            self._current_process = None

    def _read_with_soundfile(self):
        """Optimized SoundFile-based reading"""
        try:
            with sf.SoundFile(self._filepath, 'r') as f:
                f.seek(self._position_frames)
                
                while not self._stop_event.is_set():
                    if self._buffer.is_full:
                        sleep(0.02)
                        continue
                    elif self._buffer.fill_ratio > 0.8:
                        sleep(0.005)
                        continue
                    
                    chunk = f.read(self.chunk_size, dtype=np.float32, always_2d=True)
                    if self.channels == 2 and chunk.shape[1] == 1:      # mono file
                        chunk = np.repeat(chunk, 2, axis=1)

                    if len(chunk) == 0:
                        break
                    
                    if not self._buffer.append(chunk):
                        sleep(0.001)
                        
        except Exception as e:
            ll.error(f"Error reading with SoundFile: {e}")

    def stop(self):
        """Optimized stop with proper resource cleanup"""
        if self.get_movement():
            return
            
        self._set_movement_state(True, False)
        
        # Stop FFmpeg process first
        if self._current_process:
            try:
                self._current_process.terminate()
                self._current_process.wait(timeout=0.5)
            except:
                try:
                    self._current_process.kill()
                except:
                    pass
            self._current_process = None
        
        # Signal stop to all threads
        self._stop_event.set()
        self._paused.set()
        
        # Clean up threads with timeout
        threads_to_join = [self._playback_thread, self._reader_thread]
        for thread in threads_to_join:
            if thread and thread.is_alive():
                thread.join(timeout=1.0)
        
        # Clean up stream
        self._cleanup_stream()
        
        # Reset state
        with self._state_lock:
            self._buffer.clear()
            self._position_frames = 0
            self._paused.clear()
            self._play_event.clear()
            self._buffer_ready.clear()
        
        # Force garbage collection for memory cleanup
        gc.collect()

    def _cleanup_stream(self):
        """Clean up audio stream resources"""
        self._play_event.clear()
        if self._stream:
            try:
                if self._stream.active:
                    self._stream.stop()
                self._stream.close()
            except Exception as e:
                ll.error(f"Error cleaning up stream: {e}")
            finally:
                self._stream = None

    def _set_movement_state(self, active: bool, do_sleep: bool = True):
        """Thread-safe movement state management"""
        with self._movement_lock:
            self._movement_active = active
        if do_sleep:
            sleep(0.05)  # Reduced sleep time

    def _reset_movement_state(self):
        """Reset movement state in a separate thread"""
        Thread(target=self._set_movement_state, args=(False,), daemon=True).start()

    def pause(self):
        """Pause playback"""
        if not self._paused.is_set():
            self._paused.set()

    def unpause(self):
        """Resume playback"""
        if self._paused.is_set():
            self._paused.clear()

    def set_pos(self, seconds: float):
        """Seek to specific position"""
        if self._filepath:
            is_paused = self._paused.is_set()
            self._start_playback_session(self._filepath, start_pos=seconds, play_immediately=not is_paused)
        else:
            ll.warn("Cannot set position: no file loaded.")

    def get_pos(self) -> float:
        """Get current playback position in seconds"""
        return self._position_frames / self.samplerate if self.samplerate else 0.0

    def get_duration(self) -> float:
        """Get total duration in seconds"""
        return self._duration

    def set_volume(self, volume: float):
        """Set playback volume (0.0 to 1.0)"""
        self._volume = max(0.0, min(1.0, float(volume)))

    @property
    def volume(self) -> float:
        """Get current volume"""
        return self._volume

    @property
    def filepath(self) -> Optional[str]:
        """Get current file path"""
        return self._filepath

    def get_busy(self) -> bool:
        """Check if audio is currently playing"""
        return (self._play_event.is_set() and 
                not self._paused.is_set() and 
                self._stream and 
                self._stream.active)

    def get_movement(self) -> bool:
        """Check if movement/seeking is active"""
        with self._movement_lock:
            return (self._movement_active and 
                    self._play_event.is_set() and 
                    self._stream and 
                    self._stream.active)

    def get_performance_stats(self) -> dict:
        """Get performance statistics"""
        avg_load_time = (sum(self._performance_stats['load_times']) / 
                        len(self._performance_stats['load_times']) 
                        if self._performance_stats['load_times'] else 0.0)
        
        return {
            'average_load_time': avg_load_time,
            'buffer_underruns': self._performance_stats['underruns'],
            'buffer_fill_ratio': self._buffer.fill_ratio,
            'cache_size': self._get_audio_info.cache_info().currsize
        }

    def load_static_sound(self, audio_data=None, samplerate=None, channels=None):
        """Load static sound effect"""
        return OptimizedAudioSound(audio_data=audio_data, samplerate=samplerate, channels=channels)

    def __repr__(self) -> str:
        return (f"<OptimizedAudioPlayer: {self._filepath or 'No file loaded'}, "
                f"Volume: {self._volume:.2f}, Position: {self.get_pos():.2f}s, "
                f"Duration: {self.get_duration():.2f}s>")

    def __del__(self):
        """Cleanup on destruction"""
        try:
            self.stop()
        except:
            pass


class OptimizedAudioSound:
    """Optimized static sound player with efficient memory usage"""
    
    def __init__(self, filepath: Optional[str] = None, audio_data=None, 
                 samplerate: Optional[int] = None, channels: Optional[int] = None):
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

    def _load_from_file(self, filepath: str):
        """Load audio from file with error handling"""
        try:
            data, samplerate = sf.read(filepath, dtype=np.float32)
            if data.ndim == 1:
                data = data[:, np.newaxis]
            self._audio_data = data
            self._samplerate = samplerate
            self._channels = data.shape[1]
            ll.debug(f"Loaded '{filepath}' "
                       f"({self._audio_data.shape[0]} frames, {self._channels} channels, {self._samplerate} Hz)")
        except Exception as e:
            ll.error(f"Error loading audio from file '{filepath}': {e}")
            self._audio_data = np.array([], dtype=np.float32)

    def _load_from_array(self, audio_data: np.ndarray, samplerate: int, channels: int):
        """Load audio from numpy array"""
        if audio_data.dtype != np.float32:
            audio_data = audio_data.astype(np.float32)
        if audio_data.ndim == 1:
            audio_data = audio_data[:, np.newaxis]
            if channels != 1:
                ll.debug(f"1D audio_data provided, but channels={channels}. Using channels=1.")
                channels = 1
        self._audio_data = audio_data
        self._samplerate = samplerate
        self._channels = channels
        ll.debug(f"Loaded raw audio data "
                   f"({self._audio_data.shape[0]} frames, {self._channels} channels, {self._samplerate} Hz)")

    def play(self, loops: int = 0, volume: Optional[float] = None):
        """Play the sound with optional looping"""
        if self._audio_data.size == 0:
            ll.warn("Cannot play, no audio data loaded.")
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
                callback=self._playback_callback_optimized,
                finished_callback=lambda: self._stop_event.set(),
                dtype=np.float32
            )
            self._stream.start()
            ll.print(f"Playing (loops={loops}, volume={self._volume})")
            
            self._playback_thread = Thread(target=self._monitor_playback, daemon=True)
            self._playback_thread.start()
            
        except Exception as e:
            ll.error(f"Error starting playback: {e}")
            self._stop_event.set()

    def _playback_callback_optimized(self, outdata: np.ndarray, frames: int, time_info, status):
        """Optimized playback callback"""
        if status:
            ll.debug(f"OptimizedAudioSound stream status: {status}")
            
        if self._stop_event.is_set():
            outdata.fill(0.0)
            raise sd.CallbackStop
        
        frames_to_fill = frames
        output_pos = 0
        
        while frames_to_fill > 0:
            remaining_frames = self._audio_data.shape[0] - self._current_frame
            chunk_frames = min(frames_to_fill, remaining_frames)
            
            if chunk_frames > 0:
                # Use efficient array operations
                chunk_data = self._audio_data[self._current_frame:self._current_frame + chunk_frames]
                np.multiply(chunk_data, self._volume, out=outdata[output_pos:output_pos + chunk_frames])
                
                self._current_frame += chunk_frames
                output_pos += chunk_frames
                frames_to_fill -= chunk_frames
            else:
                # Handle looping
                if self._loop_count == -1:  # Infinite loop
                    self._current_frame = 0
                    continue
                elif self._loop_count > 0:
                    self._loop_count -= 1
                    self._current_frame = 0
                    continue
                else:
                    # End of playback
                    outdata[output_pos:].fill(0.0)
                    self._stop_event.set()
                    raise sd.CallbackStop

    def _monitor_playback(self):
        """Monitor playback completion"""
        self._stop_event.wait()
        if self._stream and self._stream.active:
            try:
                self._stream.stop()
                self._stream.close()
                self._stream = None
                ll.print("Playback finished/stopped.")
            except Exception as e:
                ll.error(f"Error stopping stream: {e}")

    def stop(self):
        """Stop playback"""
        if self._stop_event.is_set():
            return
            
        self._stop_event.set()
        if self._playback_thread and self._playback_thread.is_alive():
            self._playback_thread.join(timeout=0.5)

    def set_volume(self, volume: float):
        """Set playback volume"""
        self._volume = max(0.0, min(1.0, volume))

    def get_busy(self) -> bool:
        """Check if sound is currently playing"""
        return (self._stream and 
                self._stream.active and 
                not self._stop_event.is_set())

    def __repr__(self) -> str:
        return (f"<OptimizedAudioSound: {self._audio_data.shape if self._audio_data is not None else 'No data'}, "
                f"Volume: {self._volume:.2f}>")

    def __del__(self):
        """Cleanup on destruction"""
        try:
            self.stop()
        except:
            pass


# Create optimized global instance
AudioPlayer = AudioPlayerRoot()