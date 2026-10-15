"""Audio effects are initialized on demand and processed by the decoder."""
import atexit
import json
import os
from threading import RLock
from math import sin, cos, radians, atan2, degrees
from time import time
import tkinter as tk
import numpy as np


class AudioEQ:
    SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "musicapp_eq.json")
    ISO_BANDS = (31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)

    def __init__(self, samplerate, channels, chunk_size, gains_db=None):
        self.sr, self.ch, self.chunk_size = int(samplerate), int(channels), int(chunk_size)
        self.lock = RLock()
        self._process_lock = RLock()
        loaded = self._load_settings()
        self.gains_db = [float(loaded.get(str(f), (gains_db or [0] * 10)[i]))
                         for i, f in enumerate(self.ISO_BANDS)]
        self._ready = False
        self._curve_gains = None
        self._reset_pending = False
        self._settings_dirty = False
        atexit.register(self._save_settings)

    def _initialize(self):
        self._hop_size = self.chunk_size
        self._fft_size = self.chunk_size * 2
        self._window = np.hanning(self._fft_size + 1)[:-1].astype(np.float32)
        self._overlap = np.zeros((self.chunk_size, self.ch), dtype=np.float32)
        self._input_buffer = np.zeros((self._fft_size, self.ch), dtype=np.float32)
        self._work = np.empty_like(self._input_buffer)
        self._freq_bins = np.fft.rfftfreq(self._fft_size, d=1.0 / self.sr)
        try:
            import pyfftw
            self._fft_in = pyfftw.empty_aligned(self._work.shape, dtype="float32")
            self._fft_out = pyfftw.empty_aligned((self.chunk_size + 1, self.ch), dtype="complex64")
            self._plan_rfft = pyfftw.builders.rfft(self._fft_in, axis=0, threads=1,
                                                  planner_effort="FFTW_ESTIMATE")
            self._plan_irfft = pyfftw.builders.irfft(self._fft_out, n=self._fft_size,
                                                    axis=0, threads=1,
                                                    planner_effort="FFTW_ESTIMATE")
            self._fftw = True
        except ImportError:
            self._fftw = False
        self._ready = True

    def reset_state(self):
        with self.lock:
            self._reset_pending = True

    def set_gain(self, freq_hz, gain_db):
        self.set_gains({freq_hz: gain_db})

    def set_gains(self, gains):
        """Publish controls quickly; the decoder owns all FFT and buffer work."""
        with self.lock:
            for freq_hz, gain_db in gains.items():
                if freq_hz not in self.ISO_BANDS:
                    continue
                idx = self.ISO_BANDS.index(freq_hz)
                gain = max(-12.0, min(12.0, float(gain_db)))
                if self.gains_db[idx] != gain:
                    self.gains_db[idx] = gain
                    self._settings_dirty = True
            if not any(self.gains_db):
                self._reset_pending = True

    def get_gains(self):
        with self.lock:
            return dict(zip(self.ISO_BANDS, self.gains_db))

    def get_band(self, freq_hz, default=0.0):
        return self.get_gains().get(freq_hz, default)

    def process(self, chunk):
        with self._process_lock:
            with self.lock:
                gains = tuple(self.gains_db)
                reset = self._reset_pending
                self._reset_pending = False
            if reset and self._ready:
                self._overlap.fill(0)
                self._input_buffer.fill(0)
            if not any(gains) or not len(chunk):
                return chunk
            if not self._ready:
                self._initialize()
            if self._curve_gains != gains:
                self._rebuild_gain_curve(gains)
                self._curve_gains = gains
            length = len(chunk)
            if length < self.chunk_size:
                chunk = np.pad(chunk, ((0, self.chunk_size - length), (0, 0)))
            self._input_buffer[:-self.chunk_size] = self._input_buffer[self.chunk_size:]
            self._input_buffer[-self.chunk_size:] = chunk
            np.multiply(self._input_buffer, self._window[:, None], out=self._work)
            if self._fftw:
                self._fft_in[:] = self._work
                spectrum = self._plan_rfft()
            else:
                spectrum = np.fft.rfft(self._work, axis=0)
            spectrum *= self._gain_curve[:, None]
            if self._fftw:
                self._fft_out[:] = spectrum
                signal = self._plan_irfft()
            else:
                signal = np.fft.irfft(spectrum, n=self._fft_size, axis=0)
            out = (signal[:self.chunk_size] + self._overlap).astype(np.float32, copy=False)
            self._overlap[:] = signal[self.chunk_size:]
            np.clip(out, -1, 1, out=out)
            return out[:length]

    def _rebuild_gain_curve(self, gains_db):
        gains = np.power(10, np.asarray(gains_db) / 20)
        # Ignore bands above Nyquist instead of creating unsorted interpolation points.
        pairs = [(f, g) for f, g in zip(self.ISO_BANDS, gains) if f < self.sr / 2]
        freqs = [0] + [f for f, _ in pairs] + [self.sr / 2]
        values = [gains[0]] + [g for _, g in pairs] + [pairs[-1][1]]
        self._gain_curve = np.interp(np.log10(self._freq_bins + 1e-6),
                                    np.log10(np.asarray(freqs) + 1e-6), values).astype(np.float32)

    def _load_settings(self):
        try:
            with open(self.SETTINGS_FILE, encoding="utf-8") as handle:
                loaded = json.load(handle)
                return loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_settings(self):
        with self.lock:
            if not self._settings_dirty:
                return
            gains = tuple(self.gains_db)
        try:
            temporary = self.SETTINGS_FILE + ".tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(dict(zip(map(str, self.ISO_BANDS), gains)), handle)
            os.replace(temporary, self.SETTINGS_FILE)
            with self.lock:
                if tuple(self.gains_db) == gains:
                    self._settings_dirty = False
        except OSError:
            pass


class AudioEcho:
    def __init__(self, samplerate, channels, delay_ms=350, feedback=0.35, wet=0.5):
        self.sr, self.ch = int(samplerate), int(channels)
        self.lock = RLock()
        self._process_lock = RLock()
        self._buf = None
        self._idx = 0
        self._reset_pending = False
        self.set_params(delay_ms, feedback, wet)

    def set_params(self, delay_ms=None, feedback=None, wet=None):
        with self.lock:
            if delay_ms is not None:
                self.delay_ms = max(10, min(5000, float(delay_ms)))
            if feedback is not None:
                self.feedback = max(0, min(0.95, float(feedback)))
            if wet is not None:
                self.wet = max(0, min(1, float(wet)))

    def reset_state(self):
        with self.lock:
            self._reset_pending = True

    def process(self, x):
        with self._process_lock:
            with self.lock:
                delay_ms, feedback, wet = self.delay_ms, self.feedback, self.wet
                reset = self._reset_pending
                self._reset_pending = False
            if reset and self._buf is not None:
                self._buf.fill(0)
                self._idx = 0
            if not wet or not len(x):
                return x
            length = max(1, int(self.sr * delay_ms / 1000))
            if self._buf is None or len(self._buf) != length:
                self._buf = np.zeros((length, self.ch), dtype=np.float32)
                self._idx = 0
            out = np.empty_like(x)
            offset = 0
            while offset < len(x):
                count = min(len(x) - offset, len(self._buf) - self._idx)
                source = x[offset:offset + count]
                delayed = self._buf[self._idx:self._idx + count].copy()
                self._buf[self._idx:self._idx + count] = source + delayed * feedback
                out[offset:offset + count] = source * (1 - wet) + delayed * wet
                self._idx = (self._idx + count) % len(self._buf)
                offset += count
            return out

class EQKnob(tk.Canvas):
    """
    Rotary dB-gain knob for a graphic EQ.
    • Range  : -12 dB ↔ +12 dB
    • Dead-zone of 60° at the bottom so the pointer never flips
    • Callback fires at most every 10 ms while dragging,
      plus once on mouse-up (exact final value).
    """

    def __init__(self, master, radius=32, callback=None,
                 init_gain=0.0, bg=None, **kw):
        size = radius * 2 + 4
        super().__init__(master,
                         width=size, height=size,
                         bg=bg or master.cget("bg"),
                         highlightthickness=0, **kw)

        # public
        self.cb   = callback                 # func(gain_db)
        self.gain = max(-12, min(12, init_gain))

        # private
        self.r        = radius
        self._last_cb = 0.0                  # last callback time stamp
        self.ring = None

        # bind mouse
        self.disable(False)

        self._draw()

    # ───────────────────────────────────────────────────────── internal ──
    def _draw(self):
        """Keep canvas items and update only their position and value."""
        if self.ring is None:
            self.ring = self.create_oval(2, 2, 2 + self.r * 2, 2 + self.r * 2,
                                         fill="#222", outline="#555", width=2)
            self._pointer = self.create_line(0, 0, 0, 0, fill="#2ee", width=3, capstyle="round")
            self._label = self.create_text(self.r + 2, self.r + 2,
                                           fill="#ddd", font=("Segoe UI", 8, "bold"))
        angle = radians(self._gain_to_angle(self.gain))
        x = self.r + 2 + self.r * 0.75 * sin(angle)
        y = self.r + 2 - self.r * 0.75 * cos(angle)
        self.coords(self._pointer, self.r + 2, self.r + 2, x, y)
        self.itemconfig(self._label, text=f"{self.gain:+.1f} dB")
        self.itemconfig(self.ring, fill="#555" if self._disabled else "#222")

    def _start(self, ev):
        self._drag(ev)                      # update instantly

    def disable(self, disabled: bool):
        """Disables or enables the knob and updates its appearance."""
        self._disabled = bool(disabled)
        if disabled:
            self.configure(state="disabled")
            self.unbind("<Button-1>")
            self.unbind("<B1-Motion>")
            self.unbind("<ButtonRelease-1>")
            if self.ring is not None:
                self.itemconfig(self.ring, fill="#555")
        else:
            self.configure(state="normal")
            self.bind("<Button-1>", self._start)
            self.bind("<B1-Motion>", self._drag)
            self.bind("<ButtonRelease-1>", self._commit)
            if self.ring is not None:
                self.itemconfig(self.ring, fill="#222")
        self._draw()

    def _drag(self, ev):
        dx = ev.x - (self.r+2)
        dy = (self.r+2) - ev.y
        angle = degrees(atan2(dx, dy))      # 0° at top
        angle_clamped = max(-150, min(150, angle))    # dead-zone 60°
        self.gain = round(self._angle_to_gain(angle_clamped), 1)
        self._draw()

        # throttle to 10 ms
        now = time()
        if self.cb and (now - self._last_cb) >= 0.010:
            self._last_cb = now
            self.cb(self.gain)

    def _commit(self, _ev):
        """Always push final value at mouse-up."""
        if self.cb:
            self.cb(self.gain)

    # helpers
    @staticmethod
    def _angle_to_gain(angle):
        return angle / 150 * 12            # ±150° → ±12 dB

    @staticmethod
    def _gain_to_angle(gain):
        return gain / 12 * 150             # inverse map

class PercentKnob(tk.Canvas):
    """
    Rotary dB-gain knob for a graphic EQ.
    • Range  : -100 % ↔ +100 %
    • Dead-zone of 60° at the bottom so the pointer never flips
    • Callback fires at most every 10 ms while dragging,
      plus once on mouse-up (exact final value).
    """

    def __init__(self, master, radius=32, callback=None,
                 init_gain=0.0, bg=None, **kw):
        size = radius * 2 + 4
        super().__init__(master,
                         width=size, height=size,
                         bg=bg or master.cget("bg"),
                         highlightthickness=0, **kw)

        # public
        self.cb   = callback                 # func(gain_db)
        self.gain = max(-100, min(100, init_gain))
        self.ring = None

        # private
        self.r        = radius
        self._last_cb = 0.0                  # last callback time stamp

        # bind mouse
        self.disable(False)

        self._draw()

    # ───────────────────────────────────────────────────────── internal ──
    def _draw(self):
        """Keep canvas items and update only their position and value."""
        if self.ring is None:
            self.ring = self.create_oval(2, 2, 2 + self.r * 2, 2 + self.r * 2,
                                         fill="#222", outline="#555", width=2)
            self._pointer = self.create_line(0, 0, 0, 0, fill="#2ee", width=3, capstyle="round")
            self._label = self.create_text(self.r + 2, self.r + 2,
                                           fill="#ddd", font=("Segoe UI", 8, "bold"))
        angle = radians(self._gain_to_angle(self.gain))
        x = self.r + 2 + self.r * 0.75 * sin(angle)
        y = self.r + 2 - self.r * 0.75 * cos(angle)
        self.coords(self._pointer, self.r + 2, self.r + 2, x, y)
        self.itemconfig(self._label, text=f"{self.gain:+.1f}%")
        self.itemconfig(self.ring, fill="#555" if self._disabled else "#222")

    def _start(self, ev):
        self._drag(ev)                      # update instantly

    def disable(self, disabled: bool):
        """Disables or enables the knob and updates its appearance."""
        self._disabled = bool(disabled)
        if disabled:
            self.configure(state="disabled")
            self.unbind("<Button-1>")
            self.unbind("<B1-Motion>")
            self.unbind("<ButtonRelease-1>")
            if self.ring is not None:
                self.itemconfig(self.ring, fill="#555")
        else:
            self.configure(state="normal")
            self.bind("<Button-1>", self._start)
            self.bind("<B1-Motion>", self._drag)
            self.bind("<ButtonRelease-1>", self._commit)
            if self.ring is not None:
                self.itemconfig(self.ring, fill="#222")
        self._draw()

    def _drag(self, ev):
        dx = ev.x - (self.r+2)
        dy = (self.r+2) - ev.y
        angle = degrees(atan2(dx, dy))      # 0° at top
        angle_clamped = max(-150, min(150, angle))    # dead-zone 60°
        self.gain = round(self._angle_to_gain(angle_clamped), 1)
        self._draw()

        # throttle to 10 ms
        now = time()
        if self.cb and (now - self._last_cb) >= 0.010:
            self._last_cb = now
            self.cb(self.gain)

    def _commit(self, _ev):
        """Always push final value at mouse-up."""
        if self.cb:
            self.cb(self.gain)

    # helpers
    @staticmethod
    def _angle_to_gain(angle):
        return angle / 150 * 100            # ±150° → ±12 dB

    @staticmethod
    def _gain_to_angle(gain):
        return gain / 100 * 150             # inverse map

class VolumeSlider(tk.Canvas):
    """
    Horizontal volume slider on Canvas.
    Range: 0 (left) to 100 (right).
    Callback fires at most every 10 ms during drag and once on release.
    """

    def __init__(self, master, width=200, height=30, callback=None, init_volume=50, bg=None, **kw):
        size_w = width
        size_h = height
        # Determine background: explicit bg wins, else inherit parent's bg
        parent_bg = None
        self.ring = None
        try:
            parent_bg = master.cget('background')
        except Exception:
            try:
                parent_bg = master.cget('bg')
            except Exception:
                parent_bg = None
        actual_bg = bg if bg is not None else parent_bg

        super().__init__(master,
                         width=size_w, height=size_h,
                         **({'bg': actual_bg} if actual_bg is not None else {}),
                         highlightthickness=0, **kw)

        # Public
        self.cb      = callback                 # func(volume:int)
        # Clamp and store volume
        self.volume  = max(0, min(100, init_volume))

        # Internal
        self._last_cb      = 0.0                # timestamp of last callback
        self._dragging     = False
        self._thumb_radius = size_h // 2 - 2
        self._track_y      = size_h // 2
        self._track_height = max(4, size_h // 4)

        # Bind mouse
        self.disable(False)

        # Initial draw
        self._draw()
        # Sync initial value
        if self.cb:
            self.cb(self.volume)

    def _draw(self):
        width = int(self['width'])
        if self.ring is None:
            self.create_line(self._thumb_radius, self._track_y,
                             width - self._thumb_radius, self._track_y,
                             fill="#555", width=self._track_height, capstyle="round")
            self.ring = self.create_oval(0, 0, 0, 0, fill="#2ee", outline="")
            self._label = self.create_text(width // 2, self._track_y, fill="#ddd",
                                           font=("Segoe UI", 8, "bold"))
        position = self._value_to_pos(self.volume)
        self.coords(self.ring, position - self._thumb_radius, self._track_y - self._thumb_radius,
                    position + self._thumb_radius, self._track_y + self._thumb_radius)
        self.itemconfig(self._label, text=f"{self.volume:.0f}%")
        self.itemconfig(self.ring, fill="#555" if self._disabled else "#2ee")

    def disable(self, disabled: bool):
        """Disables or enables the knob and updates its appearance."""
        self._disabled = bool(disabled)
        if disabled:
            self.configure(state="disabled")
            self.unbind("<Button-1>")
            self.unbind("<B1-Motion>")
            self.unbind("<ButtonRelease-1>")
            if self.ring is not None:
                self.itemconfig(self.ring, fill="#555")
        else:
            self.configure(state="normal")
            self.bind("<Button-1>", self._start)
            self.bind("<B1-Motion>", self._drag)
            self.bind("<ButtonRelease-1>", self._commit)
            if self.ring is not None:
                self.itemconfig(self.ring, fill="#2ee")
        self._draw()

    def _value_to_pos(self, value: float) -> float:
        """Convert volume value (0-100) to x-coordinate on canvas; inverted mapping."""
        w = int(self['width'])
        min_x = self._thumb_radius
        max_x = w - self._thumb_radius
        # Inverted: 0 -> left (min_x), 100 -> right (max_x)
        return min_x + (max_x - min_x) * (value / 100)

    def _pos_to_value(self, pos: float) -> float:
        """Convert x-coordinate to volume value (0-100); inverted mapping."""
        w = int(self['width'])
        min_x = self._thumb_radius
        max_x = w - self._thumb_radius
        x = max(min_x, min(max_x, pos))
        return (x - min_x) / (max_x - min_x) * 100

    def _start(self, event):
        """Begin dragging and update position."""
        self._dragging = True
        self._drag(event)

    def _drag(self, event):
        """Handle mouse movement during drag."""
        if not self._dragging:
            return
        self.volume = round(self._pos_to_value(event.x))
        self._draw()
        now = time()
        if self.cb and (now - self._last_cb) >= 0.010:
            self._last_cb = now
            self.cb(self.volume)

    def _commit(self, event):
        """Finish drag and send final value."""
        self._dragging = False
        if self.cb:
            self.cb(self.volume)
