import json, os, atexit
import numpy as np
from threading import Lock
from scipy.signal import get_window
from math import sin, cos, radians, atan2, degrees
import tkinter as tk
from time import time

try:
    from log_loader import log_loader
except:
    from .log_loader import log_loader
    
###################################
    
ll = log_loader("Music Player")

###################################

# Get PyFFTW
try:
    import pyfftw
    _HAVE_PYFFTW = True
except Exception:
    import numpy.fft as npfft
    _HAVE_PYFFTW = False

class AudioEQ:
    """
    High-performance 10-band graphic equalizer using FFT overlap-add.
    Uses pyFFTW with planned transforms if available, falls back to numpy.fft.
    """

    SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "musicapp_eq.json")
    ISO_BANDS = (31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)

    def __init__(self, samplerate: int, channels: int, chunk_size: int, gains_db=None):
        self.sr = int(samplerate)
        self.ch = int(channels)
        self.chunk_size = int(chunk_size)
        self.lock = Lock()

        # FFT/OLA params
        self._hop_size = self.chunk_size
        self._fft_size = self._hop_size * 2  # 50% overlap
        self._window = get_window("hann", self._fft_size, fftbins=True).astype(np.float32)

        # Normalize window for perfect overlap-add
        norm_factor = np.sum(self._window) / float(self._hop_size)
        self._window /= norm_factor

        # Buffers
        self._overlap = np.zeros((self._hop_size, self.ch), dtype=np.float32)
        self._input_buffer = np.zeros((self._fft_size, self.ch), dtype=np.float32)

        # Frequency bins
        self._rfft_bins = self._fft_size // 2 + 1
        self._freq_bins = np.fft.rfftfreq(self._fft_size, d=1.0 / self.sr)

        # FFT implementation
        if _HAVE_PYFFTW:
            # Build FFTW plans once
            self._fft_in = pyfftw.empty_aligned((self._fft_size, self.ch), dtype="float32")
            self._fft_out = pyfftw.empty_aligned((self._rfft_bins, self.ch), dtype="complex64")

            self._plan_rfft = pyfftw.builders.rfft(
                self._fft_in,
                axis=0,
                threads=os.cpu_count(),
                planner_effort="FFTW_MEASURE",
                overwrite_input=True
            )
            self._plan_irfft = pyfftw.builders.irfft(
                self._fft_out,
                n=self._fft_size,
                axis=0,
                threads=os.cpu_count(),
                planner_effort="FFTW_MEASURE"
            )
        else:
            # Fallback to numpy.fft
            self._plan_rfft = lambda x: npfft.rfft(x, axis=0)
            self._plan_irfft = lambda X: npfft.irfft(X, n=self._fft_size, axis=0)

        # Load gains
        if os.path.isfile(self.SETTINGS_FILE):
            loaded = self._load_settings()
            gains_db = [loaded.get(str(f), 0.0) for f in self.ISO_BANDS]
        else:
            gains_db = gains_db or [0.0] * len(self.ISO_BANDS)

        self.gains_db = list(map(float, gains_db))
        self._gain_curve = None
        self._rebuild_gain_curve()

        atexit.register(self._save_settings)

        # Debug info
        if _HAVE_PYFFTW:
            ll.debug(f"AudioEQ: Using pyFFTW ({os.cpu_count()} threads)")
        else:
            ll.debug("AudioEQ: Using numpy.fft fallback")

    def reset_state(self):
        """Clear overlap and input buffers to avoid bleed from previous track."""
        with self.lock:
            self._overlap.fill(0.0)
            self._input_buffer.fill(0.0)

    # ---------- Public API ----------
    def set_gain(self, freq_hz: int, gain_db: float):
        with self.lock:
            try:
                idx = self.ISO_BANDS.index(freq_hz)
                self.gains_db[idx] = float(gain_db)
                self._rebuild_gain_curve()
            except ValueError:
                pass

    def get_gains(self) -> dict:
        return dict(zip(self.ISO_BANDS, self.gains_db))

    def get_band(self, freq_hz: int, default: float = 0.0) -> float:
        return self.get_gains().get(freq_hz, default)

    # ---------- Core processing ----------
    def process(self, chunk: np.ndarray) -> np.ndarray:
        if chunk is None or chunk.size == 0:
            return np.zeros((0, self.ch), dtype=np.float32)

        # Ensure shape (n, ch)
        if chunk.ndim == 1 and self.ch > 1:
            chunk = np.column_stack([chunk] * self.ch)
        elif chunk.ndim == 1:
            chunk = chunk.reshape(-1, 1)

        if chunk.shape[0] < self._hop_size:
            pad_len = self._hop_size - chunk.shape[0]
            pad = np.zeros((pad_len, self.ch), dtype=np.float32)
            chunk = np.vstack((chunk, pad))

        chunk = chunk.astype(np.float32, copy=False)

        with self.lock:
            # Slide input buffer
            self._input_buffer[:-self._hop_size] = self._input_buffer[self._hop_size:]
            self._input_buffer[-self._hop_size:] = chunk

            # Apply window
            fft_buffer = self._input_buffer * self._window[:, None]

            # FFT
            if _HAVE_PYFFTW:
                self._fft_in[:] = fft_buffer
                freq_domain = self._plan_rfft()
            else:
                freq_domain = self._plan_rfft(fft_buffer)

            # Apply EQ
            freq_domain *= self._gain_curve[:, None]

            # iFFT
            if _HAVE_PYFFTW:
                self._fft_out[:] = freq_domain
                time_domain = self._plan_irfft()
            else:
                time_domain = self._plan_irfft(freq_domain)

            # Overlap-add
            out = time_domain[:self._hop_size] + self._overlap
            self._overlap = time_domain[self._hop_size:].astype(np.float32, copy=True)

            # Clip
            np.clip(out, -1.0, 1.0, out=out)
            return out.astype(np.float32, copy=False)

    # ---------- Internals ----------
    def _rebuild_gain_curve(self):
        gains_linear = [10 ** (g / 20.0) for g in self.gains_db]
        extended_freqs = [0.0] + list(self.ISO_BANDS) + [self.sr / 2.0]
        extended_gains = [gains_linear[0]] + gains_linear + [gains_linear[-1]]
        log_ext_freqs = np.log10(np.array(extended_freqs) + 1e-6)
        log_bins = np.log10(self._freq_bins + 1e-6)
        interp = np.interp(log_bins, log_ext_freqs, extended_gains)
        self._gain_curve = np.asarray(interp, dtype=np.complex64)

    def _load_settings(self) -> dict:
        try:
            with open(self.SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {str(k): float(v) for k, v in data.items()}
        except Exception:
            return {}

    def _save_settings(self):
        data = {str(f): g for f, g in zip(self.ISO_BANDS, self.gains_db)}
        try:
            with open(self.SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception:
            pass

class AudioEcho:
    """
    One-tap echo / delay line.
      • delay_ms  - echo delay time
      • feedback  - 0 (no repeats) … 0.9 (lots of repeats)
      • wet       - 0 (dry only) … 1.0 (echo only)
    """
    def __init__(self, samplerate, channels,
                 delay_ms=350, feedback=0.35, wet=0.5):
        self.sr   = int(samplerate)
        self.ch   = int(channels)
        self.set_params(delay_ms, feedback, wet)

    # ― public ----------------------------------------------------------
    def set_params(self, delay_ms=None, feedback=None, wet=None):
        if delay_ms  is not None: self.delay_ms = max(10,  delay_ms)
        if feedback  is not None: self.feedback = np.clip(feedback, 0, 0.95)
        if wet       is not None: self.wet      = np.clip(wet,      0, 1)

        # resize delay buffer if time changed
        dlen = int(self.sr * self.delay_ms / 1000)
        if not hasattr(self, "_buf") or dlen != self._buf.shape[0]:
            self._buf   = np.zeros((dlen, self.ch), dtype=np.float32)
            self._idx   = 0  # write pointer

    def process(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:        # edge case
            return x
        out   = x.copy()
        n     = x.shape[0]
        buf   = self._buf
        idx   = self._idx
        wet   = self.wet
        fb    = self.feedback

        # sample-by-sample circular buffer (vectorised)
        for i in range(n):
            echo          = buf[idx]
            buf[idx]      = x[i] + echo * fb   # write input + feedback
            out[i]        = x[i]*(1-wet) + echo*wet   # mix
            idx           = (idx + 1) % buf.shape[0]

        self._idx = idx
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
        """Redraw the knob face + pointer + text."""
        self.delete("all")

        # shell
        self.ring = self.create_oval(2, 2, 2+self.r*2, 2+self.r*2,
                         fill="#222", outline="#555", width=2)

        # pointer
        ang = radians(self._gain_to_angle(self.gain))
        x   = self.r + 2 + self.r*0.75 * sin(ang)
        y   = self.r + 2 - self.r*0.75 * cos(ang)
        self.create_line(self.r+2, self.r+2, x, y,
                         fill="#2ee", width=3, capstyle="round")

        # gain text
        self.create_text(self.r+2, self.r+2,
                         text=f"{self.gain:+.1f} dB",
                         fill="#ddd", font=("Segoe UI", 8, "bold"))

    def _start(self, ev):
        self._drag(ev)                      # update instantly

    def disable(self, disabled: bool):
        """Disables or enables the knob and updates its appearance."""
        if disabled:
            self.configure(state="disabled")
            self.unbind("<Button-1>")
            self.unbind("<B1-Motion>")
            self.unbind("<ButtonRelease-1>")
            self.itemconfig(self.ring, fill="#555")
        else:
            self.configure(state="normal")
            self.bind("<Button-1>", self._start)
            self.bind("<B1-Motion>", self._drag)
            self.bind("<ButtonRelease-1>", self._commit)
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
        """Redraw the knob face + pointer + text."""
        self.delete("all")

        # shell
        self.ring = self.create_oval(2, 2, 2+self.r*2, 2+self.r*2,
                         fill="#222", outline="#555", width=2)

        # pointer
        ang = radians(self._gain_to_angle(self.gain))
        x   = self.r + 2 + self.r*0.75 * sin(ang)
        y   = self.r + 2 - self.r*0.75 * cos(ang)
        self.create_line(self.r+2, self.r+2, x, y,
                         fill="#2ee", width=3, capstyle="round")

        # gain text
        self.create_text(self.r+2, self.r+2,
                         text=f"{self.gain:+.1f}%",
                         fill="#ddd", font=("Segoe UI", 8, "bold"))

    def _start(self, ev):
        self._drag(ev)                      # update instantly

    def disable(self, disabled: bool):
        """Disables or enables the knob and updates its appearance."""
        if disabled:
            self.configure(state="disabled")
            self.unbind("<Button-1>")
            self.unbind("<B1-Motion>")
            self.unbind("<ButtonRelease-1>")
            self.itemconfig(self.ring, fill="#555")
        else:
            self.configure(state="normal")
            self.bind("<Button-1>", self._start)
            self.bind("<B1-Motion>", self._drag)
            self.bind("<ButtonRelease-1>", self._commit)
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
        """Draw track, thumb, and percentage text."""
        self.delete("all")
        w = int(self['width'])
        # Draw track line
        self.create_line(
            self._thumb_radius, self._track_y,
            w - self._thumb_radius, self._track_y,
            fill="#555", width=self._track_height, capstyle="round"
        )
        # Draw thumb circle
        pos = self._value_to_pos(self.volume)
        self.ring = self.create_oval(
            pos - self._thumb_radius, self._track_y - self._thumb_radius,
            pos + self._thumb_radius, self._track_y + self._thumb_radius,
            fill="#2ee", outline=""
        )
        # Draw volume text
        self.create_text(
            w // 2, self._track_y,
            text=f"{self.volume:.0f}%", fill="#ddd",
            font=("Segoe UI", 8, "bold")
        )

    def disable(self, disabled: bool):
        """Disables or enables the knob and updates its appearance."""
        if disabled:
            self.configure(state="disabled")
            self.unbind("<Button-1>")
            self.unbind("<B1-Motion>")
            self.unbind("<ButtonRelease-1>")
            self.itemconfig(self.ring, fill="#555")
        else:
            self.configure(state="normal")
            self.bind("<Button-1>", self._start)
            self.bind("<B1-Motion>", self._drag)
            self.bind("<ButtonRelease-1>", self._commit)
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
            