"""Bounded audio playback. Decoding and effects stay off the output callback."""
from threading import Condition, Event, RLock, Thread
from time import monotonic
import av
import numpy as np
import sounddevice as sd
from audio_eq import AudioEQ, AudioEcho
from log_loader import log_loader

ll = log_loader("Audio", debugging=False)


class RobustAudioBuffer:
    def __init__(self, capacity_seconds, chunk_size, channels, sample_rate):
        self.channels = channels
        self.sample_rate = sample_rate
        self.buffer_size = max(int(capacity_seconds * sample_rate), chunk_size * 4)
        self._data = np.empty((self.buffer_size, channels), dtype=np.float32)
        self._read_pos = self._write_pos = self._available = 0
        self._closed = False
        self._condition = Condition()

    def write(self, data):
        offset = 0
        while offset < len(data):
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._available < self.buffer_size)
                if self._closed:
                    return False
                count = min(len(data) - offset, self.buffer_size - self._available,
                            self.buffer_size - self._write_pos)
                self._data[self._write_pos:self._write_pos + count] = data[offset:offset + count]
                self._write_pos = (self._write_pos + count) % self.buffer_size
                self._available += count
                offset += count
                self._condition.notify_all()
        return True

    def read_into(self, outdata, volume):
        """Do not allocate an audio block or wait for the producer's lock."""
        if not self._condition.acquire(blocking=False):
            outdata.fill(0)
            return -1
        try:
            count = min(len(outdata), self._available)
            first = min(count, self.buffer_size - self._read_pos)
            if first:
                np.multiply(self._data[self._read_pos:self._read_pos + first], volume,
                            out=outdata[:first])
            if count > first:
                np.multiply(self._data[:count - first], volume, out=outdata[first:count])
            if count < len(outdata):
                outdata[count:].fill(0)
            self._read_pos = (self._read_pos + count) % self.buffer_size
            self._available -= count
            self._condition.notify()
            return count
        finally:
            self._condition.release()

    @property
    def available_samples(self):
        with self._condition:
            return self._available

    @property
    def available_seconds(self):
        return self.available_samples / self.sample_rate

    @property
    def fill_ratio(self):
        return self.available_samples / self.buffer_size

    def wait_ready(self, samples, finished, timeout=2):
        with self._condition:
            self._condition.wait_for(lambda: self._closed or self._available >= samples
                                     or finished.is_set(), timeout=timeout)

    def notify_finished(self):
        with self._condition:
            self._condition.notify_all()

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()


class AudioPlayerRoot:
    def __init__(self, buffer_size_seconds=0.5):
        # A stable output format avoids device and EQ changes between tracks.
        self.samplerate = 44100
        self.channels = 2
        self.chunk_size = 1024
        self.buffer_size_seconds = buffer_size_seconds
        self.eq = AudioEQ(self.samplerate, self.channels, self.chunk_size)
        self.echo = None
        self._gaming_mode = True
        self._state_lock = RLock()
        self._filepath = self._stream = self._container = self._buffer = None
        self._stop_event = Event()
        self._paused = Event()
        self._play_event = Event()
        self._finished = Event()
        self._drained = Event()
        self._reader_thread = None
        self._volume = 0.1
        self._position_frames = 0
        self._duration = 0.0
        self._underflow_count = 0

    def load(self, filepath):
        """Record a path; play opens and buffers it only once."""
        self.stop()
        self._filepath = filepath
        return True

    def play(self, filepath=None, start_pos=0.0):
        with self._state_lock:
            if filepath is None and self._stream and self._play_event.is_set():
                self.unpause()
                return True
            target = filepath or self._filepath
            return self._start_playback_session(target, start_pos, True) if target else False

    def _start_playback_session(self, filepath, start_pos=0.0, play_immediately=True,
                                buffer_time=None, radio_mode=False):
        with self._state_lock:
            self.stop()
            self._filepath = filepath
            self.eq.reset_state()
            if self.echo:
                self.echo.reset_state()
            try:
                remote = str(filepath).startswith(('http://', 'https://'))
                self._container = av.open(filepath, timeout=(3, 1)) if remote else av.open(filepath)
                stream = next(s for s in self._container.streams if s.type == "audio")
                self._duration = (float(stream.duration * stream.time_base)
                                  if stream.duration is not None else
                                  float(self._container.duration or 0) / av.time_base)
                start_pos = max(0.0, min(float(start_pos), self._duration or float(start_pos)))
                if start_pos:
                    self._container.seek(int(start_pos / float(stream.time_base)),
                                         stream=stream, backward=True)
                self._position_frames = int(start_pos * self.samplerate)
                # Each reader owns its events, so a slow cancelled network read cannot
                # complete or resume a newer playback session.
                self._stop_event = Event()
                self._finished = Event()
                self._drained.clear()
                self._paused.clear() if play_immediately else self._paused.set()
                self._buffer = RobustAudioBuffer(self.buffer_size_seconds, self.chunk_size,
                                                self.channels, self.samplerate)
                if self._stream is None:
                    self._stream = sd.OutputStream(samplerate=self.samplerate, channels=self.channels,
                                                   dtype="float32", blocksize=self.chunk_size,
                                                   latency=0.08, callback=self._audio_callback,
                                                   finished_callback=self._stream_finished)
                self._reader_thread = Thread(target=self._read_audio_data,
                                             args=(self._container, stream, self._buffer, start_pos,
                                                   self._stop_event, self._finished),
                                             daemon=True, name="audio-decode")
                self._reader_thread.start()
                self._buffer.wait_ready(self.chunk_size * 2, self._finished)
                if not self._buffer.available_samples:
                    raise RuntimeError("No audio samples could be decoded")
                self._play_event.set()
                if play_immediately:
                    self._stream.start()
                return monotonic() if radio_mode else True
            except Exception as exc:
                ll.error(f"Cannot play audio: {exc}")
                self.stop()
                return False

    def _read_audio_data(self, container, stream, buffer, start_pos, cancel, finished):
        try:
            # Explicit resampling handles packed/integer/mono inputs correctly.
            resampler = av.AudioResampler(format="fltp", layout="stereo", rate=self.samplerate)
            fifo = av.AudioFifo()

            def consume(frame):
                pcm = frame.to_ndarray().T
                if not len(pcm):
                    return True
                length = len(pcm)
                if not self._gaming_mode:
                    pcm = self.eq.process(pcm)[:length]
                    echo = self.echo
                    if echo and echo.wet > 0:
                        pcm = echo.process(pcm)
                return buffer.write(pcm)

            def enqueue(converted):
                if converted.pts is not None:
                    frame_start = float(converted.pts * converted.time_base)
                    trim = min(converted.samples, max(0, round((start_pos - frame_start) * self.samplerate)))
                    if trim == converted.samples:
                        return
                    if trim:
                        pcm = np.ascontiguousarray(converted.to_ndarray()[:, trim:])
                        converted = av.AudioFrame.from_ndarray(pcm, format="fltp", layout="stereo")
                        converted.sample_rate = self.samplerate
                # FIFO expects a continuous local timeline, independent of demux seek timestamps.
                converted.pts = None
                fifo.write(converted)

            for frame in container.decode(stream):
                if cancel.is_set():
                    break
                for converted in resampler.resample(frame):
                    enqueue(converted)
                    while fifo.samples >= self.chunk_size:
                        if not consume(fifo.read(self.chunk_size)):
                            return
            if not cancel.is_set():
                for converted in resampler.resample(None):
                    enqueue(converted)
                while fifo.samples:
                    if not consume(fifo.read(self.chunk_size, partial=True)):
                        return
        except Exception as exc:
            if not cancel.is_set():
                ll.error(f"Audio decoding failed: {exc}")
        finally:
            finished.set()
            buffer.notify_finished()
            try:
                container.close()
            except Exception:
                pass

    def _audio_callback(self, outdata, frames, time_info, status):
        buffer = self._buffer
        if self._paused.is_set() or buffer is None:
            outdata.fill(0)
            return
        if status and status.output_underflow:
            self._underflow_count += 1
        count = buffer.read_into(outdata, self._volume)
        self._position_frames += max(0, count)
        if 0 <= count < frames and self._finished.is_set():
            self._drained.set()
            raise sd.CallbackStop

    def _stream_finished(self):
        # A user pause also stops the device; it must remain resumable.
        if self._stop_event.is_set() or self._drained.is_set():
            self._play_event.clear()

    def stop(self):
        with self._state_lock:
            self._stop_event.set()
            if self._buffer:
                self._buffer.close()
            if self._stream:
                try:
                    self._stream.abort()
                except Exception:
                    pass
            if self._reader_thread and self._reader_thread.is_alive():
                self._reader_thread.join(timeout=2)
            if self._container and not (self._reader_thread and self._reader_thread.is_alive()):
                try:
                    self._container.close()
                except Exception:
                    pass
            self._container = self._buffer = self._reader_thread = None
            self._play_event.clear()
            self._paused.clear()
            self._position_frames = 0

    def close(self):
        self.stop()
        with self._state_lock:
            if self._stream:
                self._stream.close()
                self._stream = None

    def pause(self):
        with self._state_lock:
            self._paused.set()
            if self._stream and self._stream.active:
                self._stream.stop()

    def unpause(self):
        with self._state_lock:
            self._paused.clear()
            if self._stream and not self._stream.active and self._play_event.is_set():
                self._stream.start()

    def set_pos(self, seconds):
        if self._filepath:
            return self._start_playback_session(self._filepath, seconds, not self._paused.is_set())
        return False

    def get_pos(self):
        return self._position_frames / self.samplerate

    def get_duration(self):
        return self._duration

    def get_busy(self):
        return self._play_event.is_set() and not self._paused.is_set()

    def set_volume(self, volume, set_directly=False):
        value = float(volume)
        if value > 1:
            value /= 100
        self._volume = max(0.0, min(1.0, value if set_directly else self._volume + value))

    def get_volume(self):
        return self._volume

    volume = property(get_volume, lambda self, value: self.set_volume(value, True))

    def enable_echo(self, delay_ms=350, feedback=0.35, wet=0.5):
        self.echo = AudioEcho(self.samplerate, self.channels, delay_ms, feedback, wet)

    def set_echo(self, delay_ms=None, feedback=None, wet=None):
        if self.echo:
            self.echo.set_params(delay_ms, feedback, wet)

    def disable_echo(self):
        self.echo = None

    def radio_play(self, filepath, start_pos=0.0, buffer_time=None):
        return self._start_playback_session(filepath, start_pos, True, radio_mode=True)


AudioPlayer = AudioPlayerRoot()
