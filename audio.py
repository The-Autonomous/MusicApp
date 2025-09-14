import json, gc, weakref, os
from threading import Event, Thread, Lock, RLock
from typing import Optional, Union
from time import sleep, monotonic
from functools import lru_cache
from collections import deque
import sounddevice as sd
import numpy as np
import av

try:
    from log_loader import log_loader
    from audio_eq import AudioEQ, AudioEcho
except:
    from .log_loader import log_loader
    from .audio_eq import AudioEQ, AudioEcho

ll = log_loader("Audio", debugging=True)

class RobustAudioBuffer:
    """High-performance lock-free circular buffer designed for real-time audio."""
    def __init__(self, capacity_seconds: float, chunk_size: int, channels: int, sample_rate: int):
        self.chunk_size = chunk_size
        self.channels = channels
        self.sample_rate = sample_rate
        
        # Calculate buffer size in samples, ensure it's large enough
        total_samples = int(capacity_seconds * sample_rate)
        self.buffer_size = max(total_samples, chunk_size * 200)  # At least 200 chunks
        
        # Ring buffer
        self._data = np.zeros((self.buffer_size, channels), dtype=np.float32)
        self._write_pos = 0
        self._read_pos = 0
        self._available = 0
        self._lock = Lock()
        self._closed = False
        
        ll.debug(f"Buffer created: {self.buffer_size} samples ({self.buffer_size/sample_rate:.2f}s)")

    def write(self, data: np.ndarray) -> bool:
        """Write data to buffer. Returns True if successful."""
        if self._closed or data is None or data.size == 0:
            return False
            
        # Ensure correct shape
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        if data.shape[1] != self.channels:
            if data.shape[1] == 1:
                data = np.repeat(data, self.channels, axis=1)
            else:
                data = data[:, :self.channels]
                
        samples_to_write = data.shape[0]
        
        with self._lock:
            if self._closed:
                return False
                
            # Check available space
            free_space = self.buffer_size - self._available
            
            if samples_to_write > free_space:
                # Make room by advancing read position (drop old data)
                samples_to_drop = samples_to_write - free_space
                self._read_pos = (self._read_pos + samples_to_drop) % self.buffer_size
                self._available -= samples_to_drop
                if samples_to_drop > self.chunk_size:
                    ll.debug(f"Dropped {samples_to_drop} samples to make room")
            
            # Write data in two parts if it wraps around
            write_end = self._write_pos + samples_to_write
            if write_end <= self.buffer_size:
                # No wrap
                self._data[self._write_pos:write_end] = data
            else:
                # Wrap around
                first_part = self.buffer_size - self._write_pos
                self._data[self._write_pos:] = data[:first_part]
                self._data[:samples_to_write - first_part] = data[first_part:]
            
            self._write_pos = write_end % self.buffer_size
            self._available += samples_to_write
            
        return True

    def read(self, samples: int) -> Optional[np.ndarray]:
        """Read samples from buffer. Returns None if not enough data."""
        with self._lock:
            if self._closed or self._available < samples:
                return None
                
            # Allocate output
            output = np.zeros((samples, self.channels), dtype=np.float32)
            
            # Read in two parts if it wraps around
            read_end = self._read_pos + samples
            if read_end <= self.buffer_size:
                # No wrap
                output[:] = self._data[self._read_pos:read_end]
            else:
                # Wrap around
                first_part = self.buffer_size - self._read_pos
                output[:first_part] = self._data[self._read_pos:]
                output[first_part:] = self._data[:samples - first_part]
            
            self._read_pos = read_end % self.buffer_size
            self._available -= samples
            
            return output

    @property
    def available_samples(self) -> int:
        with self._lock:
            return self._available
    
    @property
    def available_seconds(self) -> float:
        return self.available_samples / self.sample_rate
    
    @property
    def fill_ratio(self) -> float:
        return self.available_samples / self.buffer_size
    
    def clear(self):
        with self._lock:
            self._write_pos = 0
            self._read_pos = 0
            self._available = 0
            self._closed = False
    
    def close(self):
        with self._lock:
            self._closed = True

class AudioPlayerRoot:
    def __init__(self, buffer_size_seconds: float = 3.0):
        self.samplerate = 44100
        self.channels = 2
        self.chunk_size = 1024  # Small chunks for low latency
        self.buffer_size_seconds = buffer_size_seconds
        self.eq = AudioEQ(self.samplerate, self.channels, self.chunk_size)
        self.echo = None
        self._gaming_mode = True
        self._state_lock = RLock()
        self._filepath = None
        self._stream = None
        self._container = None
        self._audio_stream = None
        self._stop_event = Event()
        self._paused = Event()
        self._play_event = Event()
        self._reader_thread = None
        self._volume = 0.1
        self._position_frames = 0
        self._total_frames = 0
        self._duration = 0.0
        self._buffer = None
        self._underflow_count = 0
        self._callback_errors = 0

    def _open_container(self, filepath: str):
        try:
            self._container = av.open(filepath)
            self._audio_stream = next((s for s in self._container.streams if s.type == "audio"), None)
            if not self._audio_stream:
                raise ValueError("No audio stream found")
            
            self._duration = float(self._container.duration or 0) / av.time_base
            self.samplerate = self._audio_stream.rate
            self.channels = self._audio_stream.channels
            self._total_frames = int(self._duration * self.samplerate) if self._duration else 0
            
            # Create buffer with actual audio parameters
            self._buffer = RobustAudioBuffer(
                self.buffer_size_seconds, 
                self.chunk_size, 
                self.channels, 
                self.samplerate
            )
            
            ll.debug(f"Opened {filepath}: {self.samplerate}Hz, {self.channels}ch, {self._duration:.2f}s")
        except Exception as e:
            ll.error(f"Failed to open {filepath}: {e}")
            raise

    def _seek(self, seconds: float):
        if self._container and self._audio_stream:
            try:
                # Use the audio stream time_base when available (more robust)
                try:
                    tb = float(self._audio_stream.time_base)
                    ts = int(seconds / tb)
                except Exception:
                    # Fallback to legacy av.time_base multiply
                    ts = int(seconds * av.time_base)
                ll.debug(f"Seeking to {seconds:.3f}s -> ts={ts}")
                self._container.seek(ts, stream=self._audio_stream, any_frame=False, backward=True)
                self._position_frames = int(seconds * self.samplerate)
            except Exception as e:
                ll.error(f"Seek failed: {e}")

    def _process(self, chunk):
        chunk = self.eq.process(chunk)
        if self.echo:
            chunk = self.echo.process(chunk)
        return chunk

    def enable_echo(self, delay_ms=350, feedback=0.35, wet=0.5):
        if not self.echo:
            self.echo = AudioEcho(self.samplerate, self.channels, delay_ms, feedback, wet)

    def disable_echo(self):
        self.echo = None

    def load(self, filepath: str) -> bool:
        return self._start_playback_session(filepath, start_pos=0.0, play_immediately=False)

    def play(self, filepath: Optional[str] = None, start_pos: float = 0.0) -> bool:
        self.eq.reset_state()
        if filepath:
            return self._start_playback_session(filepath, start_pos=start_pos, play_immediately=True)
        elif self._filepath:
            if self._paused.is_set():
                self.unpause()
                return True
            else:
                return self._start_playback_session(self._filepath, start_pos=start_pos, play_immediately=True)
        else:
            ll.warn("No file loaded to play.")
            return False

    def _start_playback_session(
        self,
        filepath: str,
        start_pos: float,
        play_immediately: bool,
        buffer_time: Optional[float] = None,
        radio_mode: bool = False
    ) -> Union[bool, float]:
        """
        Start a playback session. Compatible with radio_play() calls.
        buffer_time and radio_mode are accepted for compatibility with old API.
        """
        self.stop()
        
        with self._state_lock:
            self._filepath = filepath
            try:
                self._open_container(filepath)
            except Exception as e:
                return False
            
            # Radio-mode correction (for sync)
            final_position = start_pos
            if radio_mode and buffer_time is not None:
                now = monotonic()
                time_since_buffer = now - buffer_time
                final_position = start_pos + time_since_buffer
                ll.debug(f"Radio timing correction: start={start_pos:.3f}, elapsed={time_since_buffer:.3f}, corrected={final_position:.3f}")

            # Set position frames, then perform an immediate seek so demux starts at right place
            self._position_frames = int(final_position * self.samplerate)
            if self._position_frames > 0:
                try:
                    # Do a direct seek here to ensure reader thread begins at the right spot
                    self._seek(self._position_frames / self.samplerate)
                except Exception as e:
                    ll.debug(f"Seek during startup failed (will try in reader): {e}")
            self._stop_event.clear()
            self._underflow_count = 0
            self._callback_errors = 0
            
            if not play_immediately: 
                self._paused.set() 
            else: 
                self._paused.clear()

            try:
                # Start audio stream first
                self._stream = sd.OutputStream(
                    samplerate=self.samplerate, 
                    channels=self.channels,
                    dtype=np.float32, 
                    blocksize=self.chunk_size,
                    callback=self._audio_callback,
                    latency='low',
                    prime_output_buffers_using_stream_callback=False,
                )
                
                # Start reader thread
                self._reader_thread = Thread(target=self._read_audio_data, daemon=True)
                self._reader_thread.start()
                
                # Wait for some buffer before starting playback
                start_time = monotonic()
                while (self._buffer.available_seconds < 0.5 and 
                       monotonic() - start_time < 2.0 and 
                       not self._stop_event.is_set()):
                    sleep(0.01)
                
                if self._buffer.available_seconds < 0.5:
                    ll.warn("Starting with minimal buffer")
                
                self._stream.start()
                self._play_event.set()
                
                ll.debug(f"Playback started with {self._buffer.available_seconds:.2f}s buffer")
                return monotonic() if radio_mode else True
                
            except Exception as e:
                ll.error(f"Failed to start playback: {e}")
                self.stop()
                return False

    def _read_audio_data(self):
        """Optimized audio reader (normalization disabled)."""
        try:
            if self._position_frames > 0:
                self._seek(self._position_frames / self.samplerate)

            # Process larger batches for efficiency
            batch_samples = self.samplerate // 4  # 0.25 second batches

            # Normalization disabled
            norm_gain = 1.0\

            for packet in self._container.demux(self._audio_stream):
                if self._stop_event.is_set():
                    break

                try:
                    frames = packet.decode()
                    for frame in frames:
                        if self._stop_event.is_set():
                            break

                        # Get audio data
                        pcm = frame.to_ndarray()

                        pcm = pcm.astype(np.float32)
                        # Normalize integer data # Skipping for now
                        #if np.issubdtype(pcm.dtype, np.integer):
                        #    max_val = np.iinfo(pcm.dtype).max
                        #    pcm = pcm.astype(np.float32) / max_val
                        #else:
                        #    pcm = pcm.astype(np.float32)

                        # Handle channel layout
                        if pcm.ndim == 1:
                            pcm = pcm.reshape(-1, 1)
                        if pcm.shape[0] == self.channels and pcm.shape[1] > pcm.shape[0]:
                            pcm = pcm.T

                        # Adjust channels
                        if pcm.shape[1] < self.channels:
                            pcm = np.repeat(pcm, self.channels, axis=1)
                        elif pcm.shape[1] > self.channels:
                            pcm = pcm[:, :self.channels]

                        # Apply gain (no-op since norm_gain = 1.0)
                        pcm *= norm_gain

                        # --- Buffer write loop with throttling ---
                        offset = 0
                        while offset < len(pcm) and not self._stop_event.is_set():
                            # Throttle if buffer is too full
                            while self._buffer.fill_ratio > 0.90 and not self._stop_event.is_set():
                                sleep(0.01)

                            batch_end = min(offset + batch_samples, len(pcm))
                            batch = pcm[offset:batch_end]

                            if not self._buffer.write(batch) and not self._stop_event.is_set():
                                sleep(0.001)

                            offset = batch_end

                except Exception as e:
                    ll.error(f"Error processing audio frame: {e}")
                    continue

        except Exception as e:
            ll.error(f"Critical error in audio reader: {e}")
        finally:
            ll.debug("Audio reader thread finished")


    def _audio_callback(self, outdata: np.ndarray, frames: int, time_info, status):
        """High-performance audio callback."""
        try:
            outdata.fill(0.0)
            
            if status and status.output_underflow:
                self._underflow_count += 1
                if self._underflow_count <= 5 or self._underflow_count % 20 == 0:
                    ll.warn(f"Audio underflow #{self._underflow_count}, buffer: {self._buffer.available_seconds:.3f}s")
            
            if self._paused.is_set() or not self._buffer:
                return
            
            # Read exactly the number of frames requested
            audio_data = self._buffer.read(frames)
            if audio_data is None:
                # Not enough data - this will cause underflow but won't crash
                return
            
            # Apply effects if needed
            if not self._gaming_mode:
                audio_data = self._process(audio_data)
            
            # Apply volume and output
            scaled = audio_data * self._volume

            if self._volume in (0.0, 1.0):
                rms = np.sqrt(np.mean(scaled**2)) if scaled.size else 0.0

            outdata[:] = scaled

            self._position_frames += frames
            
        except Exception as e:
            self._callback_errors += 1
            if self._callback_errors <= 3:
                ll.error(f"Audio callback error #{self._callback_errors}: {e}")

    def stop(self):
        ll.debug("Stop method called")
        self._stop_event.set()
        
        # Stop stream first
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                ll.error(f"Error stopping stream: {e}")
            finally:
                self._stream = None
        
        # Wait for reader thread
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=1.0)
        
        # Clean up resources
        with self._state_lock:
            if self._container:
                try:
                    self._container.close()
                except:
                    pass
            self._container = None
            
            if self._buffer:
                self._buffer.close()
                self._buffer = None
            
            self._position_frames = 0
            self._paused.clear()
            self._play_event.clear()
        
        gc.collect()
        ll.debug("Stop method completed")

    def pause(self):
        self._paused.set()

    def unpause(self):
        self._paused.clear()

    def set_pos(self, seconds: float):
        if self._filepath:
            current_paused = self._paused.is_set()
            self._start_playback_session(self._filepath, start_pos=seconds, play_immediately=not current_paused)
        else:
            ll.warn("No file loaded to seek.")

    def get_pos(self) -> float:
        return self._position_frames / self.samplerate if self.samplerate else 0.0

    def get_duration(self) -> float:
        return self._duration
    
    def set_volume(self, volume: float, set_directly=False):
        """
        Set playback volume.
        Accepts either 0.0-1.0 (scalar) or 0-100 (percent).
        """
        # Normalize to 0.0–1.0
        if volume > 1.0:
            volume = volume / 100.0

        if set_directly:
            self._volume = max(0.0, min(1.0, float(volume)))
        else:
            self._volume = max(0.0, min(1.0, self._volume + float(volume)))

        ll.debug(f"self._volume={self._volume:.2f}")

    @property
    def volume(self) -> float:
        return self._volume

    @volume.setter
    def volume(self, value: float):
        try:
            raw = value
            value = float(value)
        except Exception:
            ll.error(f"[Audio] Invalid volume input: {value!r}")
            return

        # Normalize percent → 0.0–1.0
        if value > 1.0:
            value = value / 100.0

        # Clamp
        self._volume = max(0.0, min(1.0, value))

    def get_volume(self) -> float:
        return self._volume

    def get_busy(self) -> bool:
        return self._play_event.is_set() and not self._paused.is_set()

    def radio_play(self, filepath: str, start_pos: float, buffer_time: Optional[float] = None) -> float:
        """
        Radio playback mode: just a wrapper for _start_playback_session with radio_mode=True.
        Returns monotonic() timestamp when started.
        """
        if filepath:
            return self._start_playback_session(
                filepath,
                start_pos=start_pos,
                play_immediately=True,
                buffer_time=buffer_time,
                radio_mode=True
            ) or monotonic()
        elif self._filepath:
            if self._paused.is_set():
                self.unpause()
                return monotonic()
            else:
                return self._start_playback_session(
                    self._filepath,
                    start_pos=start_pos,
                    play_immediately=True,
                    buffer_time=buffer_time,
                    radio_mode=True
                ) or monotonic()
        else:
            ll.warn("No file loaded for radio play.")
            return 0.0

AudioPlayer = AudioPlayerRoot()