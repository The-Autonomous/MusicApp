import json, os, atexit
import numpy as np
from threading import Lock
from scipy.signal import sosfilt, sosfilt_zi, get_window # Kept for AudioEcho and now used for windowing
from math import sin, cos, pi, radians, atan2, degrees, log10
import tkinter as tk
from time import time

class AudioEQ:
    """
    High-performance 10-band graphic equalizer using FFT.
    This implementation replaces a series of CPU-intensive biquad filters
    with a much more efficient frequency-domain approach.
    """

    SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "musicapp_eq.json")
    ISO_BANDS = (31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)

    def __init__(self, samplerate: int, channels: int, chunk_size: int, gains_db=None):
        self.sr = int(samplerate)
        self.ch = int(channels)
        self.chunk_size = chunk_size
        self.lock = Lock()

        # FFT processing parameters for overlap-add method
        self._fft_size = chunk_size * 2  # Use 2x chunk size for FFT to handle overlap
        self._hop_size = chunk_size      # Step size is the original chunk size
        self._window = get_window('hann', self._fft_size, fftbins=True)

        # 🔑 Normalize window for perfect overlap-add reconstruction
        self._window /= np.sum(self._window) / self._hop_size

        # Overlap buffer, needs to match channel count
        self._overlap = np.zeros((self._hop_size, self.ch), dtype=np.float32)

        # Pre-calculate frequency bins for the FFT
        self._freq_bins = np.fft.rfftfreq(self._fft_size, d=1./self.sr)
        
        # Load persisted gains (if user didn't explicitly pass gains_db)
        if os.path.isfile(self.SETTINGS_FILE):
            loaded = self._load_settings()
            gains_db = [loaded.get(str(f), 0.0) for f in self.ISO_BANDS]
        else:
            gains_db = gains_db or [0.0] * len(self.ISO_BANDS)
        
        self.gains_db = list(map(float, gains_db))
        self._gain_curve = None # This will hold the calculated frequency response
        self._rebuild_gain_curve()

        atexit.register(self._save_settings)

    # ---------- Public API (remains compatible with old version) ----------
    def set_gain(self, freq_hz: int, gain_db: float):
        """Set gain (dB) for the band and rebuild the FFT gain curve."""
        with self.lock:
            try:
                idx = self.ISO_BANDS.index(freq_hz)
                self.gains_db[idx] = float(gain_db)
                self._rebuild_gain_curve()
            except ValueError:
                # Frequency not in our ISO bands, ignore it
                pass

    def get_gains(self) -> dict:
        """Returns a dictionary of the current band gains."""
        return dict(zip(self.ISO_BANDS, self.gains_db))

    def get_band(self, freq_hz: int, default: float = 0.0) -> float:
        """Return gain in dB for one centre frequency."""
        return self.get_gains().get(freq_hz, default)

    def process(self, chunk: np.ndarray) -> np.ndarray:
        """
        Processes an audio chunk using proper overlap-add FFT.
        Always returns hop_size samples, no skips.
        """
        if chunk.size == 0:
            return chunk

        if chunk.ndim == 1 and self.ch > 1:
            chunk = np.column_stack([chunk] * self.ch)

        if chunk.shape[0] < self._hop_size:
            padding = np.zeros((self._hop_size - chunk.shape[0], self.ch), dtype=np.float32)
            chunk = np.vstack((chunk, padding))

        with self.lock:
            # --- Step 1: maintain rolling buffer ---
            if not hasattr(self, "_input_buffer"):
                self._input_buffer = np.zeros((self._fft_size, self.ch), dtype=np.float32)
            
            # shift left by hop_size
            self._input_buffer[:-self._hop_size] = self._input_buffer[self._hop_size:]
            # append new chunk at the end
            self._input_buffer[-self._hop_size:] = chunk

            # --- Step 2: window ---
            fft_buffer = self._input_buffer * self._window[:, None]

            # --- Step 3: FFT ---
            freq_domain_data = np.fft.rfft(fft_buffer, axis=0)

            # --- Step 4: EQ ---
            freq_domain_data *= self._gain_curve[:, None]

            # --- Step 5: iFFT ---
            time_domain_data = np.fft.irfft(freq_domain_data, axis=0)

            # --- Step 6: overlap-add reconstruction ---
            output_chunk = time_domain_data[:self._hop_size] + self._overlap
            self._overlap = time_domain_data[self._hop_size:]

            # --- Step 7: clip ---
            np.clip(output_chunk, -1.0, 1.0, out=output_chunk)
            return output_chunk.astype(np.float32)

    # ---------- Internals for FFT Processing ----------
    def _rebuild_gain_curve(self):
        """
        Calculates the target gain for each FFT frequency bin based on the
        10 user-defined ISO band gains. Uses interpolation for smooth transitions.
        """
        # Convert dB gains to linear amplitude multipliers
        gains_linear = [10**(g / 20.0) for g in self.gains_db]

        # Add boundary points for smoother interpolation at edges
        extended_freqs = [0] + list(self.ISO_BANDS) + [self.sr / 2]
        extended_gains = [gains_linear[0]] + gains_linear + [gains_linear[-1]]
        
        # Logarithmic interpolation is more natural for audio frequencies
        # We need to handle the log(0) case for the first frequency bin
        log_freqs = np.log10(np.array(extended_freqs, dtype=float) + 1e-6) # Add small epsilon to avoid log(0)
        log_bins = np.log10(self._freq_bins + 1e-6)
        
        # np.interp is highly optimized for this kind of operation
        interpolated_gains = np.interp(log_bins, log_freqs, extended_gains)
        
        self._gain_curve = interpolated_gains.astype(np.float32)

    # ---------- Persistence (unchanged) ----------
    def _load_settings(self) -> dict:
        """Load {freq: gain_db} from JSON file."""
        try:
            with open(self.SETTINGS_FILE, 'r', encoding='utf-8') as f:
                # Ensure keys are loaded as strings to match what gets saved
                return {str(k): float(v) for k, v in json.load(f).items()}
        except Exception:
            return {}

    def _save_settings(self):
        """Write current gains to JSON, atomically if possible."""
        # Save keys as strings to be JSON compliant
        data = {str(f): g for f, g in zip(self.ISO_BANDS, self.gains_db)}
        with open(self.SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=None)

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
            