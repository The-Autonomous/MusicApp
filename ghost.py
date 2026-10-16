import ctypes, os, random, sys
import tkinter as tk
from tkinter import font, messagebox, ttk
from threading import Lock, RLock, Thread
from queue import Queue, Empty, Full
from pynput import keyboard, mouse
from time import monotonic, sleep
from math import cos, pi, sin, ceil
from typing import Iterator
import json
from pathlib import Path
from overlay_state import OverlayPreferences, clamp_position, monitor_work_areas, place_window

try:
    from ui_controls import (ACTION_LABELS, build_controls_window, conflicting_action,
                             replacement_chord, shortcut_label)
except ImportError:
    from .ui_controls import (ACTION_LABELS, build_controls_window, conflicting_action,
                              replacement_chord, shortcut_label)

# Tk window handles are pointer-sized on 64-bit Windows.
if os.name == "nt":
    from ctypes import wintypes
    _user32 = ctypes.windll.user32
    _user32.GetParent.argtypes = [wintypes.HWND]
    _user32.GetParent.restype = wintypes.HWND
    _user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    _user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    _user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    _user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    _user32.SetWindowPos.restype = wintypes.BOOL

try:
    from log_loader import log_loader
    from playerUtils import TitleCleaner
    from audio_eq import EQKnob, PercentKnob, VolumeSlider
except ImportError:
    from .log_loader import log_loader
    from .playerUtils import TitleCleaner
    from .audio_eq import EQKnob, PercentKnob, VolumeSlider
    
# Windows API constants
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000 # Prevents window from stealing focus

WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST    = 0x00000008

# Add these SetWindowPos constants:
# SetWindowPos constants
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010 # Do not activate the window (critical for games)
SWP_ASYNCWINDOWPOS = 0x4000 # Places the window on the queue to be set

# Add these Window handles for SetWindowPos:
# Window handles for SetWindowPos
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2

# Layered Window Attributes constants (already there, but for context)
LWA_ALPHA = 0x00000002 # Use this flag with SetLayeredWindowAttributes for alpha transparency

### Logging Handle ###

ll = log_loader("Ghost", debugging = False)

class _MousePoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

class _LastInput(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint), ("tick", ctypes.c_uint)]

### Theming ###

class ThemeManager:
    """ Centralized class for managing UI theme, colors, and fonts. """
    def __init__(self):
        self.COLORS = {
            "window_bg": "#1d1f24",
            "frame_bg": "#23272e",
            "entry_bg": "#2c313a",
            "text": "#e0e0e0",
            "text_dark": "#aaaaaa",
            "placeholder": "#888888",
            "accent": "#00ffd5",
            "accent_active": "#00bfa5",
            "danger": "#ff4d4d",
            "danger_active": "#ff1a1a",
            "warning": "#ffae42",
            "warning_active": "#ff8c00",
            "button": "#555555",
            "button_active": "#777777",
            "border": "#555555",
            "transparent": "#ff00ff"
        }

        self.FONTS = {
            "main": font.Font(family='Segoe UI', size=14, weight='bold'),
            "time": font.Font(family='Segoe UI', size=12),
            "lyrics": font.Font(family='Segoe UI', size=11, weight='normal', slant='italic'),
            "ui_title": ("Segoe UI", 20, "bold"),
            "ui_normal": ("Segoe UI", 13),
            "ui_list": ("Segoe UI", 12),
            "ui_small_italic": ("Segoe UI", 10, "italic"),
            "fixed_width": ("Consolas", 12),
            "icon": ("Arial Unicode MS", 11),
            "hint": font.Font(family='Segoe UI', size=9)
        }

    def apply_ttk_styles(self, root):
        """ Applies all necessary ttk styles for the application. """
        style = ttk.Style(root)
        style.theme_use("clam")

        # --- General Widgets ---
        style.configure("TFrame", background=self.COLORS["frame_bg"])
        style.configure("Accent.TFrame", background=self.COLORS["window_bg"])
        style.configure("TLabel", background=self.COLORS["frame_bg"], foreground=self.COLORS["text"], font=self.FONTS["ui_normal"])
        style.configure("Header.TLabel", foreground=self.COLORS["accent"], font=self.FONTS["ui_title"])
        style.configure("Status.TLabel", foreground=self.COLORS["warning"], font=self.FONTS["ui_small_italic"])
        
        # --- Buttons ---
        style.configure("TButton",
            font=self.FONTS["ui_normal"],
            background=self.COLORS["button"],
            foreground=self.COLORS["text"],
            relief="flat",
            padding=8,
            borderwidth=0
        )
        style.map("TButton",
            background=[("active", self.COLORS["button_active"])]
        )
        
        style.configure("Accent.TButton",
            background=self.COLORS["accent"],
            foreground=self.COLORS["frame_bg"],
        )
        style.map("Accent.TButton",
            background=[("active", self.COLORS["accent_active"])]
        )
        
        style.configure("Danger.TButton",
            background=self.COLORS["danger"],
            foreground=self.COLORS["text"],
        )
        style.map("Danger.TButton",
            background=[("active", self.COLORS["danger_active"])]
        )
        
        style.configure("Warning.TButton",
            background=self.COLORS["warning"],
            foreground="#000000",
        )
        style.map("Warning.TButton",
            background=[("active", self.COLORS["warning_active"])]
        )

        # --- Checkbutton ---
        style.configure("TCheckbutton",
            background=self.COLORS["window_bg"],
            foreground=self.COLORS["text"],
            relief="flat",
            indicatorcolor=self.COLORS["entry_bg"],
            font=self.FONTS["ui_normal"]
        )
        style.map("TCheckbutton",
            indicatorcolor=[("selected", self.COLORS["accent"])],
            background=[("active", self.COLORS["frame_bg"])]
        )

        # --- Entry ---
        style.configure("TEntry",
            fieldbackground=self.COLORS["entry_bg"],
            foreground=self.COLORS["text"],
            insertcolor=self.COLORS["text"],
            bordercolor=self.COLORS["border"],
            relief="flat",
            padding=5
        )
        style.map("TEntry",
            bordercolor=[("focus", self.COLORS["accent"])],
            fieldbackground=[("!disabled", self.COLORS["entry_bg"])]
        )

        style.configure("TCombobox", fieldbackground=self.COLORS["entry_bg"],
                        background=self.COLORS["entry_bg"], foreground=self.COLORS["text"],
                        arrowcolor=self.COLORS["text_dark"], bordercolor=self.COLORS["border"],
                        selectbackground=self.COLORS["entry_bg"], selectforeground=self.COLORS["text"],
                        font=self.FONTS["ui_list"], padding=5)
        style.map("TCombobox", fieldbackground=[("readonly", self.COLORS["entry_bg"])],
                  foreground=[("readonly", self.COLORS["text"])],
                  bordercolor=[("focus", self.COLORS["accent"])])

        # --- OptionMenu ---
        style.configure("TMenubutton",
            background=self.COLORS["entry_bg"],
            foreground=self.COLORS["text"],
            relief="flat",
            padding=(8, 4, 12, 4)
        )
        style.map("TMenubutton",
            background=[("active", self.COLORS["button"])],
            arrowcolor=[("active", self.COLORS["accent"]), ("!disabled", self.COLORS["text_dark"])]
        )

### Utilities ###

class SettingsHandler:
    """
    Manages a UTF-8 JSON settings file.
    """
    def __init__(self, filename: str):
        try:
            # Try to get the script's directory
            script_dir = os.path.dirname(os.path.abspath(__file__))
        except NameError:
            # Fallback if __file__ is not defined (e.g., in some interactive environments)
            script_dir = os.getcwd()
        self.filepath = os.path.join(script_dir, filename)
        self._lock = RLock()
        self._settings = self._load()

    def _load(self) -> dict:
        if not os.path.isfile(self.filepath):
            return {}
        with self._lock:
            try:
                with open(self.filepath, 'r', encoding='utf-8') as f:
                    # Ensure we return an empty dict if the file is empty or malformed
                    content = f.read()
                    if not content.strip(): # File is empty or whitespace only
                        return {}
                    return json.loads(content)
            except (json.JSONDecodeError, FileNotFoundError):
                return {}

    def get_all_settings(self) -> dict:
        with self._lock:
            return self._settings.copy()

    def get_setting(self, key: str, default=None):
        with self._lock:
            return self._settings.get(key, default)

    def update_setting(self, key: str, value):
        with self._lock:
            self._settings[key] = value
            self._save()
            
    def update_multiple_settings(self, data: dict):
        with self._lock:
            self._settings.update(data)
            self._save()

    def _save(self):
        with self._lock:
            temp_path = self.filepath + ".tmp"
            try:
                with open(temp_path, 'w', encoding='utf-8') as f:
                    json.dump(self._settings, f, ensure_ascii=False, indent=2)
                os.replace(temp_path, self.filepath)
            except Exception as e:
                ll.error(f"Error saving settings: {e}")

    def reset_settings(self):
        with self._lock:
            self._settings = {}
            self._save()

class RoundedCanvas(tk.Canvas):
    minimum_steps = 10  # lower values give pixelated corners

    @staticmethod
    def get_cos_sin(radius: int) -> Iterator[tuple[float, float]]:
        steps = max(radius, RoundedCanvas.minimum_steps)
        for i in range(steps + 1):
            angle = pi * (i / steps) * 0.5
            yield (cos(angle) - 1) * radius, (sin(angle) - 1) * radius

    def create_rounded_box(self, x0: int, y0: int, x1: int, y1: int, radius: int, color: str) -> int:
        points = []
        cos_sin_r = tuple(self.get_cos_sin(radius))
        for cos_r, sin_r in cos_sin_r:
            points.append((x1 + sin_r, y0 - cos_r))
        for cos_r, sin_r in cos_sin_r:
            points.append((x1 + cos_r, y1 + sin_r))
        for cos_r, sin_r in cos_sin_r:
            points.append((x0 - sin_r, y1 + cos_r))
        for cos_r, sin_r in cos_sin_r:
            points.append((x0 - cos_r, y0 - sin_r))
        return self.create_polygon(points, fill=color, outline="")

class MouseTracker:
    def __init__(self, on_right_button=None):
        self.user32 = ctypes.windll.user32
        self._on_right_button = on_right_button
        self._right_button_pressed = False
        self._last_right_press_pos = None  # Stores coordinates of the last press
        self.window_proportions = [0, 0, 0, 0]
        self.mouse_tracking = {
            'CurrentPosition': (0, 0),
            'LastCheckedTime': monotonic(),
            "AFKTimeout": 60*60 # 1 hour in seconds
        }
        
        self.listener = mouse.Listener(
            on_click=self._on_click,
            daemon=True
        )
        self.listener.start()

    def calc_pos(self, x, y, a_x, a_y, b_x, b_y):
        """Returns True if point (x,y) is inside the rectangle defined by:
        - a: top-left corner (a_x, a_y)
        - b: bottom-right corner (b_x, b_y)
        """
        return (a_x <= x <= b_x) and (a_y <= y <= b_y)

    def update_window(self, *values):
        self.window_proportions = [*values]

    def _on_click(self, x, y, button, pressed):
        """Internal callback for pynput mouse events."""
        try:
            if pressed:
                self.mouse_tracking['LastCheckedTime'] = monotonic()
            if button == mouse.Button.right:
                self._right_button_pressed = pressed
                if pressed:
                    # Store the position of the press
                    self._last_right_press_pos = (x, y)
                else:
                    # Clear the position on release
                    self._last_right_press_pos = None
                if self._on_right_button:
                    self._on_right_button(x, y, pressed)
            ll.debug(f"Mouse tracker got key {'Pressed' if pressed else 'Released'} {button} at ({x}, {y})")
        except Exception as E:
            ll.warn(f"Mouse tracker met unexpected error {E}")

    def mouse_pos(self):
        """Returns [x, y] of mouse cursor (works in fullscreen games)."""
        pt = _MousePoint()
        self.user32.GetCursorPos(ctypes.byref(pt))
        prevPosition = self.mouse_tracking['CurrentPosition'] # Log The Current Position
        self.mouse_tracking['CurrentPosition'] = (pt.x, pt.y) # Set It To The New Position
        if self.mouse_tracking['CurrentPosition'] != prevPosition: # If They Dont Match
            self.mouse_tracking['LastCheckedTime'] = monotonic() # Update The Last Time The Mouse Moved
        return [pt.x, pt.y]

    def is_right_mouse_down(self):
        """Returns True if right mouse button is pressed."""
        return self._right_button_pressed
        
    def was_press_in_window(self):
        """Checks if the last right-click press occurred within the current window bounds."""
        if not self._last_right_press_pos:
            return False
        
        press_x, press_y = self._last_right_press_pos
        win_x, win_y, win_w, win_h = self.window_proportions
        
        return (win_x <= press_x < win_x + win_w) and (win_y <= press_y < win_y + win_h)

    def stop(self):
        """Stops the pynput listener if it was started and is still active."""
        if hasattr(self, 'listener') and self.listener.is_alive():
            ll.print("Stopping pynput listener thread.")
            self.listener.stop()
            self.listener.join(timeout=1)
            if self.listener.is_alive():
                ll.warn("Warning: pynput listener thread did not terminate cleanly.")

class GhostOverlay:
    
    EQ_PRESETS = {
        # Neutral
        "Flat":                              [ 0,  0,  0,  0,  0,  0,  0,  0,  0,  0],
        
        # Warm/Smooth Presets
        "Warm & Smooth":                     [-3, -2, -1,  0,  2,  2,  1,  0, -2, -3],
        "Lounge":                            [-3, -2, -1,  0,  2,  3,  2,  0, -1, -3],
        "Vintage Tape":                      [-2, -1,  0,  1,  2,  2,  1,  0, -1, -4],
        "Lo-fi Chill":                       [-5, -4, -2,  0,  1,  2,  1, -1, -3, -6],
        
        # Vocal/Speech Enhancement
        "Vocal Clarity":                     [-4, -4, -2,  0,  3,  5,  4,  2, -2, -4],
        "Speech Boost":                      [-5, -4, -2,  0,  3,  5,  4,  2,  0, -2],
        "Podcast":                           [-8, -6, -4, -2,  0,  2,  4,  5,  6,  8],
        
        # Genre-Specific Presets
        "Hip-Hop Punch":                     [ 6,  6,  4,  2, -2, -3,  1,  3,  2, -1],
        "Pop Hits":                          [ 4,  3,  1,  0, -1,  1,  3,  4,  3,  2],
        "Rock Arena":                        [ 4,  3,  2,  1,  0,  2,  3,  4,  3,  1],
        "Rock Metal":                        [ 5,  4,  3,  1,  0,  2,  4,  5,  4,  2],
        "Funk Groove":                       [ 3,  2,  1,  0,  1,  3,  4,  3,  2,  1],
        "Classical":                         [-2, -1,  0,  1,  3,  3,  2,  1,  0, -1],
        "Jazz Club":                         [-1,  0,  1,  2,  3,  2,  1,  0, -1, -2],
        "Acoustic":                          [-2, -1,  0,  1,  3,  4,  3,  1,  0, -1],
        
        # Electronic/Dance Presets
        "Dance Club":                        [ 5,  4,  2,  0, -2,  0,  3,  5,  4,  3],
        "EDM Festival":                      [ 7,  6,  4,  1, -1,  0,  3,  5,  6,  4],
        
        # Bass-Heavy Presets
        "Crazy Bass":                        [10,  8,  5,  0, -5, -6, -2,  3,  5,  4],
        "I Like Screaming":                  [ 8,  6,  4,  1, -3,  0,  4,  7,  6,  5],
        
        # Frequency Shaping
        "Treble Boost":                      [-5, -3,  0,  3,  5,  5,  4,  2,  0, -2],
        "Bass & Treble":                     [ 6,  4,  2,  0, -4, -5,  0,  2,  4,  6],
        "V Shape":                           [10,  8,  5,  0, -5, -6,  0,  5,  8, 10],
        "Inverted V":                        [-6, -5, -3,  0,  3,  3,  0, -3, -5, -6],
        
        # Frequency Cuts
        "Bass Cut":                          [-12,-12,-12,-12, 0,  0,  0,  0,  0,  0],
        "Treble Cut":                        [ 0,  0,  0,  0,-12,-12,-12,-12,-12,-12],
        
        # Special/Utility Presets
        "Night Mode":                        [ -6, -6, -4, -2, 0,  1,  1,  0, -1, -3],
        "Make Me Sleep":                     [-12,-10, -8, -5, 0,  1,  2,  1, -3, -8],
        
        # Experimental/Fun Presets
        "Loudness :D":                       [12, 12, 12, 12, 12, 12, 12, 12, 12, 12],
        "Every Other":                       [-6,  6, -6,  6, -6,  6, -6,  6, -6,  6],
        "AI Generated These Could You Tell": [ 2,  3,  1,  0, -1,  1,  3,  4,  3,  2],
    }
    
    MusicPlayer = None # Will be set externally after initialization
    
    def __init__(self, root, overlay_settings_file=None):
        ### Root ###
        self.root = root
        self._closing = False
        self._ui_queue = Queue(maxsize=256)
        self._display_dirty = False
        self._layout_key = None
        self._last_mouse_poll = 0.0
        self._authentication_active = False
        self._overlay_dragging = False
        self._drag_after_id = None
        self._drag_work_areas = None
        self._hint_animation_after_id = None
        self._hint_hide_after_id = None
        self._hint_opacity = 0.0
        self.theme = ThemeManager()
        self.theme.apply_ttk_styles(self.root)
        
        self.window = None
        self.canvas = None
        self.overlay_preferences = OverlayPreferences(overlay_settings_file or Path(__file__).with_name('.musicapp_overlay.json'))
        self._last_position = self.overlay_preferences.position
        self._overlay_visible = self.overlay_preferences.visible
        self._screen_check_due = 0
        self.key_hints_popup = None
        self.modification_status_label = None # For "Listening..." message
        
        ### Mouse ###
        self.mouseEvents = MouseTracker(
            on_right_button=lambda x, y, pressed: self.post_ui(self._on_overlay_mouse_button, x, y, pressed))
        self.clickThroughState = True # True To Click Through False To Click On
        
        ### Gaming Mode ###
        self.gaming_mode_checkbox = None

        ### Search ###
        self._search_after_id = None
        self._is_searching = False
        self.is_spinning_downloading = False
        
        ### Music Player ###
        self.overlay_text_padding = 15
        self.overlay_corner_radius = 15
        self.canvas_items = {
            'bg': None,
            'player_text': None,
            'duration_text': None,
            'lyrics_text': None,
            'controls_hint': None
        }
        self._update_scheduled = False

        ### Display Info ###
        self.triggerDebounce = [monotonic(), 1.0] # Reduced debounce for faster UI response
        self.text_lock = Lock()
        self.display_lyrics = True
        self.running_lyrics = False
        self.display_radio = False
        self.player_metric = {'player_text':'','player_duration':'', 'player_lyrics':''}
        self.radio_metric = {'current_ip':'0.0.0.0', 'availability':[]}
        self.bg_color = '#000000'
        self.last_toggle_state = False # Last toggle state for debouncing
        self.readyForKeys = False # True If Keys Are Ready False If Not
        self.playerState = self.overlay_preferences.player_enabled
        
        ### Overlay Dragging Variables ###
        self._win_start_size_width = 0
        self._win_start_size_height = 0

        ### Key Listening ###
        self.is_listening_for_modification = False
        self.action_id_being_modified = None

        ### Key Code ###
        self.bindings_handler = SettingsHandler(filename=".keyBindings.json")
        self._define_default_key_actions()
        self._load_custom_bindings()
        self._rebuild_key_maps() # Initial build
        self.hidden_keys = { # Handle Headphone Keys
            'media_play_pause': self._wireless_trigger_pause,
            'media_next': self._trigger_skip_next,
            'media_previous': self._trigger_skip_previous,
        }
        
        self.current_listener_key = monotonic() # Initialize current listener key for debounce
        
        self.listener = keyboard.Listener(
            on_press = lambda key: self._handle_key_press(key, self.current_listener_key),
            on_release = lambda key: self._handle_key_release(key, self.current_listener_key)
        )
        self.listener.start()
        self.readyForKeys = True
        self._reset_all_keys_pressed()
        
        ### Title Cleaning ###
        self.TitleCleaner = TitleCleaner()
        
        ### Finalization ###
        self.keep_overlay_on_top_init()
        self.root.after(100, self._ui_tick)
        self.root.after(0, self.display_overlay) # Start the overlay display process

#####################################################################################################

    def _define_default_key_actions(self):
        self.key_actions = [
            {
                'id': 'skip_previous',
                'required': ['alt', '-'],
                'alt_needed': True,
                'action': self._trigger_skip_previous,
                'hint': "Skip To The Previous Song",
                'modifiable': True
            },
            {
                'id': 'skip_next',
                'required': ['alt', '='],
                'alt_needed': True,
                'action': self._trigger_skip_next,
                'hint': "Skip To The Next Song",
                'modifiable': True
            },
            {
                'id': 'volume_down',
                'required': ['alt', '['],
                'alt_needed': True,
                'action': self._trigger_volume_dwn,
                'hint': "Turn Down Music Volume",
                'modifiable': True
            },
            {
                'id': 'volume_up',
                'required': ['alt', ']'],
                'alt_needed': True,
                'action': self._trigger_volume_up,
                'hint': "Turn Up Music Volume",
                'modifiable': True
            },
            {
                'id': 'pause_play',
                'required': ['alt', ';'],
                'alt_needed': True,
                'action': self._trigger_pause,
                'hint': "Pause / Play The Music", # Clarified hint
                'modifiable': True
            },
            {
                'id': 'repeat_toggle',
                'required': ['alt', '\''],
                'alt_needed': True,
                'action': self._trigger_repeat,
                'hint': "Enable / Disable Repeat Mode",
                'modifiable': True
            },
            {
                'id': 'lyrics_toggle_visibility',
                'required': ['alt', '/'],
                'alt_needed': True,
                'action': self._trigger_lyrics_toggle,
                'hint': "Show / Hide Lyrics If Available", # Clarified hint
                'modifiable': True
            },
            {
                'id': 'radio_enable_toggle',
                'required': ['alt', 'a'],
                'alt_needed': True,
                'action': self._trigger_radio_toggle,
                'hint': "Enable / Disable Radio Mode", # Clarified hint
                'modifiable': True
            },
            {
                'id': 'radio_scan_station',
                'required': ['alt', '`'],
                'alt_needed': True,
                'action': self._trigger_radio_station,
                'hint': "Scan For Next Radio Station", # Clarified hint
                'modifiable': True
            },
            {
                'id': 'show_search',
                'required': ['alt', '\\'],
                'alt_needed': True,
                'action': self.show_search_overlay,
                'hint': "Search Songs",
                'modifiable': True
            },
            {
                'id': 'show_eq_menu',
                'required': ['right alt', '\\'],
                'alt_needed': True,
                'action': self.show_eq_overlay,
                'hint': "EQ Menu",
                'modifiable': True
            },
            {
                'id': 'youtube_sync',
                'required': ['right alt', 'y'],
                'alt_needed': True,
                'action': lambda: self.youtube_sync.toggle(self),
                'hint': 'Connect YouTube / Sync Liked Songs',
                'modifiable': True
            },
            {
                'id': 'toggle_overlay',
                'required': ['alt', 'shift'],
                'alt_needed': True,
                'forbidden': ['ALL'],
                'action': self.toggle_overlay,
                'hint': "Show / Hide Music Player",
                'modifiable': True
            },
            {
                'id': 'player_on_off',
                'required': ['right alt', 'right shift'],
                'action': self.toggle_player,
                'hint': "Turn Music Player On / Off", # Clarified hint
                'modifiable': False # Specific, important
            },
            {
                'id': 'show_hints',
                'required': ['alt', 'right alt'],
                'action': self.show_key_hints,
                'hint': "Show This Controls Window", # Clarified hint
                'modifiable': False # Specific, important for help
            },
            {
                'id': 'player_restart',
                'required': ['right alt', '.'],
                'action': self.reboot_overlay,
                'hint': "Reboot Music Player", # Clarified hint
                'modifiable': False # Specific, important
            },
            {
                'id': 'kill_self',
                'required': ['right alt', 'ctrl'],
                'action': self.close_application,
                'hint': "Shutdown The Music Player", # Clarified hint
                'modifiable': False # Critical, potentially disruptive
            },
        ]

    def _load_custom_bindings(self):
        custom_bindings = self.bindings_handler.get_all_settings()
        if not custom_bindings:
            return

        for action in self.key_actions:
            action_id = action.get('id')
            if action_id and action.get('modifiable') and action_id in custom_bindings:
                new_required_keys = custom_bindings[action_id]
                if isinstance(new_required_keys, dict):
                    action['required'] = new_required_keys.get('required', action['required'])
                    action['alt_needed'] = new_required_keys.get('alt_needed', action.get('alt_needed', True))
                else:
                    ll.warn(f"Warning: Invalid custom binding for {action_id} in settings file. Using default.")
                    self.bindings_handler.update_setting(action_id, {'required': action['required'], 'alt_needed': action.get('alt_needed', True)})

    def _rebuild_key_maps(self):
        self.all_existing_keys = set()
        for act in self.key_actions:
            for k_raw in act['required']:
                self.all_existing_keys.add(k_raw.lower()) # Ensure lowercase
            forbidden = () if act.get('_forbid_other_keys') else act.get('forbidden', ())
            for f_raw in forbidden:
                if f_raw != 'ALL':
                    self.all_existing_keys.add(f_raw.lower()) # Ensure lowercase
        
        self.keys_pressed = {k: False for k in self.all_existing_keys}

        for act in self.key_actions:
            act['required'] = [key.lower() for key in act['required']] # Ensure required keys are lowercase
            if 'ALL' in act.get('forbidden', ()):
                act['_forbid_other_keys'] = True
            if act.get('_forbid_other_keys'):
                act['forbidden'] = [
                    k_norm for k_norm in self.all_existing_keys
                    if k_norm not in act['required']
                ]
            elif 'forbidden' in act:
                 act['forbidden'] = [key.lower() for key in act['forbidden']]
        
    def _handle_key_press(self, key, state=None):
        if not self.readyForKeys or not state == self.current_listener_key:
            return

        name = self._normalize_key(key)
        if not name: return # Unrecognized key
        
        # --- Hidden key detection ---
        if name in getattr(self, 'hidden_keys', {}):
            action = self.hidden_keys[name]
            if callable(action):
                self.post_ui(action)

        if self.is_listening_for_modification:
            if name == 'escape':
                self.post_ui(self._cancel_key_modification, refresh_hints=True)
                return

            # We are expecting 'alt' + new_key. 'alt' is implicit.
            # The 'name' here is the new_key.
            # Ignore standalone modifiers as the new distinguishing key, unless it's shift for e.g. Alt+Shift
            # For simplicity, we take any non-'alt' key as the new distinguishing key.
            if name in ('alt', 'right alt', 'ctrl'):
                return # Wait for a different key

            # If 'name' is 'shift', 'right shift', or 'right alt', it's a valid part of a combo
            # The design is Alt + `name`.
            self.post_ui(self.finalize_key_modification, name)
            return

        if name in self.keys_pressed:
            self.keys_pressed[name] = True
            self._check_toggle()

    def _handle_key_release(self, key, state=None):
        if not state == self.current_listener_key: return
        name = self._normalize_key(key)
        if not name: return

        if name in self.keys_pressed:
            self.keys_pressed[name] = False
            # Only reset last_toggle_state if the released key was part of a combo
            # This simple reset is fine for most cases.
            self.last_toggle_state = False
        
        # If we were listening for a combo that involved holding this key,
        # this is where we might reset self.is_listening_for_modification
        # but current design finalizes on press of the second key.

    def _normalize_key(self, key):
        s = str(key).lower()
        if hasattr(key, 'char') and key.char: # For standard alphanumeric keys
             name = key.char.lower()
        elif hasattr(key, 'name'): # For special keys like Key.alt or Key.shift
            name = key.name.lower()
        else: # Fallback for some other key types if necessary
            if s.startswith("key."):
                name = s.split(".", 1)[1]
            else:
                name = s.strip("'")

        if name == "alt_l" or name == "alt": return "alt"
        if name == "alt_r" or name == "alt_gr": return "right alt"
        if name == "shift_l": return "shift" # Differentiate left and right if needed elsewhere
        if name == "shift_r": return "right shift"
        if name in ("ctrl_l", "ctrl_r", "control"): return "ctrl" # 'control' is sometimes used
        
        # Characters that pynput names differently than direct input
        if name == '<space>': return 'space'
        # Add more specific normalizations if issues arise with certain keys
        # For example, punctuation might need careful handling if they have verbose names from pynput
        # The provided snippet in question used '-', '=', '[', ']', ';', '\'', '/'
        # These are usually captured correctly by key.char for on_press

        return name

    def _check_toggle(self):
        if self.is_listening_for_modification:
            return

        if self.last_toggle_state: # Debounce subsequent triggers until a key is released
            return

        for action in self.key_actions:
            # Ensure all required keys are currently pressed
            require_alt_to_act = action.get('alt_needed', True)
            required_met = all(True if k=='alt' and require_alt_to_act == False else self.keys_pressed.get(k, False) for k in action['required'])
            
            # Ensure no forbidden keys are pressed
            forbidden_met = True # Assume true unless a forbidden key is found pressed
            if action.get('forbidden'): # Check if 'forbidden' key exists and is not empty
                forbidden_met = not any(self.keys_pressed.get(k, False) for k in action['forbidden'])

            if required_met and forbidden_met:
                action_func = action.get('action')
                if callable(action_func):
                    self.post_ui(action_func)
                    self.last_toggle_state = True # Prevent immediate re-trigger
                    # Optional: More selective reset of keys_pressed if needed
                    # For example, keep 'alt' pressed but clear the action-specific key:
                    # for k_to_clear in action['required']:
                    #    if k_to_clear != 'alt': self.keys_pressed[k_to_clear] = False
                break

    def _reset_all_keys_pressed(self):
        """Set all tracked keys to not pressed (False)."""
        for k in self.keys_pressed:
            self.keys_pressed[k] = False
        self.last_toggle_state = False
        
    def background_key_reset(self):
        """Continuously reboots listeners for key presses that might get overshadowed."""
        self.current_listener_key = monotonic()
        old_listener = self.listener
        # Stop and join the old listener before starting a new one
        if old_listener and old_listener.running:
            old_listener.stop()
            old_listener.join(timeout=2)
        self.listener = keyboard.Listener(
            on_press=lambda key: self._handle_key_press(key, self.current_listener_key),
            on_release=lambda key: self._handle_key_release(key, self.current_listener_key)
        )
        self.listener.start()
        self._reset_all_keys_pressed()

#####################################################################################################

    def initiate_key_modification(self, action_id_to_modify):
        action = next((item for item in self.key_actions if item['id'] == action_id_to_modify), None)
        if not action or not action.get('modifiable'):
            return
        self.action_id_being_modified = action_id_to_modify
        self.is_listening_for_modification = True
        modifier = 'Right Alt' if 'right alt' in action['required'] and 'alt' not in action['required'] else 'Left Alt'
        if self.modification_status_label:
            self.modification_status_label.config(
                text=f"Changing {ACTION_LABELS.get(action['id'], action['hint'])}: press a key for {modifier} + key. Esc cancels.")
        if self.key_hints_popup and self.key_hints_popup.winfo_exists():
            self.key_hints_popup.bind('<Escape>', self._cancel_key_modification_event)

    def _cancel_key_modification_event(self, event=None):
        self._cancel_key_modification()
        return 'break'

    def _cancel_key_modification(self, refresh_hints=False):
        self.is_listening_for_modification = False
        self.action_id_being_modified = None
        if self.modification_status_label:
            try:
                self.modification_status_label.config(text='')
            except tk.TclError:
                pass
        if self.key_hints_popup and self.key_hints_popup.winfo_exists():
            self.key_hints_popup.bind('<Escape>', self._close_key_hints)

    def finalize_key_modification(self, new_distinguishing_key_name):
        action = next((item for item in self.key_actions if item['id'] == self.action_id_being_modified), None)
        if not action or not self.is_listening_for_modification:
            return
        if new_distinguishing_key_name in ('alt', 'right alt', 'ctrl'):
            return
        new_required = replacement_chord(action, new_distinguishing_key_name)
        candidate = dict(action, required=new_required)
        conflict = conflicting_action(self.key_actions, candidate, action['id'])
        if conflict:
            self.modification_status_label.config(
                text=f"{shortcut_label(candidate)} already controls {ACTION_LABELS.get(conflict['id'], conflict['hint'])}. Choose another key, or Esc to cancel.")
            return
        action['required'] = new_required
        self.bindings_handler.update_setting(action['id'], {
            'required': new_required, 'alt_needed': action.get('alt_needed', True)})
        self._rebuild_key_maps()
        self._cancel_key_modification()
        self._refresh_key_hints(f"Saved: {ACTION_LABELS.get(action['id'], action['hint'])} · {shortcut_label(action)}")

    def _confirm_reset_bindings(self):
        if not self.key_hints_popup or not self.key_hints_popup.winfo_exists():
            return
        if messagebox.askyesno('Restore shortcuts', 'Restore the original shortcuts? Your custom shortcuts will be replaced.',
                              parent=self.key_hints_popup):
            self.bindings_handler.reset_settings()
            self._define_default_key_actions()
            self._rebuild_key_maps()
            self._refresh_key_hints('Default shortcuts restored.')

    def _on_alt_toggle(self, action):
        if 'alt' not in action['required']:
            return
        candidate = dict(action, alt_needed=not action.get('alt_needed', True))
        conflict = conflicting_action(self.key_actions, candidate, action['id'])
        if conflict:
            self._refresh_key_hints(f"{shortcut_label(candidate)} is already used by {ACTION_LABELS.get(conflict['id'], conflict['hint'])}.")
            return
        action['alt_needed'] = candidate['alt_needed']
        self.bindings_handler.update_setting(action['id'], {
            'required': action['required'], 'alt_needed': action['alt_needed']})
        self._rebuild_key_maps()
        self._refresh_key_hints('Shortcut updated.')

    def _close_key_hints(self, event=None):
        popup, self.key_hints_popup = self.key_hints_popup, None
        self.is_listening_for_modification = False
        self.action_id_being_modified = None
        self.modification_status_label = None
        if popup:
            try:
                popup.destroy()
            except tk.TclError:
                pass
        return 'break'

    def _refresh_key_hints(self, status=''):
        customize = bool(self.key_hints_popup and getattr(self.key_hints_popup, '_customize_controls', False))
        geometry = self.key_hints_popup.geometry() if self.key_hints_popup else None
        self._close_key_hints()
        self.show_key_hints(force_state=True, customize=customize)
        if geometry:
            self.key_hints_popup.geometry(geometry)
        self.modification_status_label.config(text=status)
        self._layout_key = None
        self.schedule_update()

    def _run_controls_action(self, action):
        callback = action.get('action')
        if callable(callback):
            if action['id'] in ('show_search', 'show_eq_menu', 'youtube_sync', 'player_on_off'):
                self._close_key_hints()
            callback()

    def show_key_hints(self, force_state: bool = None, customize=False):
        if getattr(self, '_authentication_active', False) and force_state is not False:
            return
        if force_state is False:
            self._close_key_hints()
            return
        if self.key_hints_popup and self.key_hints_popup.winfo_exists():
            if force_state is not True:
                self._close_key_hints()
            return
        self.key_hints_popup, self.modification_status_label = build_controls_window(
            self.root, self.theme.COLORS, self.key_actions,
            run_action=self._run_controls_action, on_edit=self.initiate_key_modification,
            on_alt_toggle=self._on_alt_toggle, on_reset=self._confirm_reset_bindings,
            on_close=self._close_key_hints, customize=customize)

#####################################################################################################

    def get_gaming_mode(self):
        """Returns the current state of gaming mode."""
        try:
            return getattr(self, "MusicPlayer", None).get_gaming_mode()
        except Exception as E:
            return False

    def set_gaming_mode(self, is_gaming_mode: bool):
        """Sets the state of gaming mode and updates the UI."""
        try:
            getattr(self, "MusicPlayer", None).toggle_gaming_mode(is_gaming_mode)
            self._update_eq_ui_state()
        except Exception as E:
            return False

    def _update_eq_ui_state(self):
        """Disables/enables EQ controls and adds/removes a visual overlay based on gaming mode."""
        try:
            if not getattr(self, "_eq_window", None) or not self._eq_window.winfo_exists():
                return
        except Exception as E:
            ll.warn(f"Failed to update EQ window state: {E}")
            return

        is_disabled = self.get_gaming_mode()

        # Disable/enable knobs and update their appearance
        for knob in self.all_eq_knobs:
            knob.disable(is_disabled)
            
        # Disable/enable preset menu
        if self.eq_preset_menu:
            self.eq_preset_menu.config(state="disabled" if is_disabled else "normal")

    def show_eq_overlay(self):
        """ Pops a draggable EQ + Echo overlay with rotary knobs. """
        if getattr(self, '_authentication_active', False):
            return
        try:
            if getattr(self, "_eq_window", None) and self._eq_window.winfo_exists():
                self._eq_window.destroy(); return
        except Exception as E:
            ll.warn(f"Failed to toggle EQ window state: {E}")
            return
            
        _eq_target = getattr(self, "MusicPlayer", None)
        if _eq_target is None:
            ll.warn("No MusicPlayer with EQ/echo found."); return
        initial_gains = _eq_target.get_bands()
        bands = sorted(initial_gains)

        win = tk.Toplevel(self.root); win.overrideredirect(True)
        win.attributes("-topmost", True); win.configure(bg="#000")
        self._eq_window = win
        screen_w = self.root.winfo_screenwidth()
        per_knob = 64
        max_cols = max(1, (screen_w - 100)//per_knob)
        rows = ceil(len(bands) / max_cols)
        w = min(len(bands), max_cols)*per_knob + 40
        h = rows*110 + 190
        x = (self.root.winfo_screenwidth() - w) // 2
        y = 40
        win.geometry(f"{w}x{h}+{x}+{y}")
        
        card = tk.Canvas(win, width=w, height=h, bg=self.theme.COLORS["window_bg"], highlightthickness=0)
        card.pack(fill="both", expand=True)
        card.create_rectangle(0, 0, w, h, fill=self.theme.COLORS["window_bg"], outline="")
        
        grid = ttk.Frame(card, style="Accent.TFrame")
        grid.place(relx=0.5, rely=0.05, anchor="n")
        
        self.eq_knobs = {}
        self.all_eq_knobs = []
        fmax = 16000
        
        preset_var = tk.StringVar(value="Flat")
        
        self.EQ_PRESETS["Custom"] = None
        preset_map = { tuple(vals): name for name, vals in self.EQ_PRESETS.items() if vals is not None }

        def knob_changed(gain, freq):
            _eq_target.set_band(freq, gain)
            gains = _eq_target.get_bands()
            current = tuple(gains[f] for f in bands)
            preset_var.set(preset_map.get(current, "Custom"))

        for i, freq in enumerate(bands):
            col = ttk.Frame(grid, style="Accent.TFrame")
            col.grid(row=i//max_cols, column=i%max_cols, padx=6, pady=2)
            lbl = f"{freq//1000}k" if freq >= 1000 else str(freq)
            ttk.Label(col, background=self.theme.COLORS["window_bg"], text=lbl).pack()
            init = initial_gains.get(freq, 0.0)
            if isinstance(init, tuple): init = init[0]
            callback = lambda g, f=freq: knob_changed(g, f)
            knob = EQKnob(col, radius=26, init_gain=init, callback=callback, bg=self.theme.COLORS["window_bg"])
            knob.pack()
            self.eq_knobs[freq] = knob
            self.all_eq_knobs.append(knob)

        def apply_preset(name):
            if name == "Custom": return
            _eq_target.set_bands(dict(zip(bands, self.EQ_PRESETS[name])))
            for f, g in zip(bands, self.EQ_PRESETS[name]):
                self.eq_knobs[f].gain = g
                self.eq_knobs[f]._draw()
            preset_var.set(name)

        preset_menu = ttk.OptionMenu(card, preset_var, "Flat", *self.EQ_PRESETS.keys(), command=apply_preset, style="TMenubutton")
        preset_menu["menu"].config(tearoff=0, bg=self.theme.COLORS["entry_bg"], fg=self.theme.COLORS["text"],
                                   activebackground=self.theme.COLORS["button"], activeforeground=self.theme.COLORS["accent"], relief="flat")
        card.create_window(w//2, int(h*0.48), window=preset_menu, anchor="n")
        self.eq_preset_menu = preset_menu
        preset_var.set(preset_map.get(tuple(initial_gains[f] for f in bands), "Custom"))

        echo_frame = ttk.Frame(card, style="Accent.TFrame")
        echo_frame.place(relx=0.5, rely=0.63, anchor="n")

        delay_init = getattr(_eq_target, "echo", None)
        delay_ms   = delay_init.delay_ms if delay_init else 350
        wet_pct    = int(delay_init.wet*100) if delay_init else 0

        ttk.Label(echo_frame, background=self.theme.COLORS["window_bg"], text="Echo", font=(self.theme.FONTS["ui_normal"][0], 9, "bold")).grid(row=0, column=0, columnspan=2, pady=(0,3))

        def update_echo(_=None):
            if wet_pct == 0:
                _eq_target.disable_echo()
            elif not getattr(_eq_target, "echo", None):
                _eq_target.enable_echo(delay_ms=delay_ms, wet=wet_pct/100, feedback=0.35)
            else:
                _eq_target.set_echo(delay_ms=delay_ms, wet=wet_pct/100)

        def set_delay(value):
            nonlocal delay_ms
            delay_ms = max(10, int(value * 10))
            update_echo()

        def set_wet(value):
            nonlocal wet_pct
            wet_pct = max(0, int(value))
            update_echo()

        delay_knob = PercentKnob(echo_frame, radius=20, bg=self.theme.COLORS["window_bg"], init_gain=delay_ms / 10, callback=set_delay)
        ttk.Label(echo_frame, background=self.theme.COLORS["window_bg"], text="Delay ×10 ms").grid(row=1, column=0, padx=6, pady=2)
        delay_knob.grid(row=2, column=0, padx=6)
        self.all_eq_knobs.append(delay_knob)

        wet_knob = PercentKnob(echo_frame, radius=20, bg=self.theme.COLORS["window_bg"], init_gain=wet_pct, callback=set_wet)
        ttk.Label(echo_frame, background=self.theme.COLORS["window_bg"], text="Wet %").grid(row=1, column=1, padx=6, pady=2)
        wet_knob.grid(row=2, column=1, padx=6)
        self.all_eq_knobs.append(wet_knob)
        
        volume_knob = VolumeSlider(echo_frame, width=120, height=24, bg=self.theme.COLORS["window_bg"], init_volume=int(_eq_target.get_volume() * 100), callback=lambda v: _eq_target.set_volume(v / 100, True))
        ttk.Label(echo_frame, background=self.theme.COLORS["window_bg"], text="Volume %").grid(row=1, column=2, padx=6, pady=2)
        volume_knob.grid(row=2, column=2, padx=6)

        def toggle_gaming_mode_command():
            self.set_gaming_mode(self._gaming_mode_bool_var.get())

        gaming_mode_frame = ttk.Frame(card, style="Accent.TFrame")
        gaming_mode_frame.place(relx=0.1, rely=0.88, anchor="n")
        
        self._gaming_mode_bool_var = tk.BooleanVar(value=self.get_gaming_mode())
        self.gaming_mode_checkbox = ttk.Checkbutton(gaming_mode_frame, text="Gaming Mode", variable=self._gaming_mode_bool_var, command=toggle_gaming_mode_command, style="TCheckbutton")
        self.gaming_mode_checkbox.pack(padx=10, pady=(5, 0))
        
        sync_frame = ttk.Frame(card, style="Accent.TFrame")
        sync_frame.place(relx=0.1, rely=0.78, anchor="n")
        
        if hasattr(self.MusicPlayer, 'radio_client'):
            self._accept_eq_var = tk.BooleanVar(value=self.MusicPlayer.accepting_radio_eq())
            def toggle_host_eq(): self.MusicPlayer.set_accepting_radio_eq(self._accept_eq_var.get())
            ttk.Checkbutton(sync_frame, text="Play Radio EQ", variable=self._accept_eq_var, command=toggle_host_eq, style="TCheckbutton").pack(padx=10, pady=(5, 0))
            
        def start_mv(e): win._dx=e.x_root-win.winfo_x(); win._dy=e.y_root-win.winfo_y()
        def do_mv(e):    win.geometry(f"+{e.x_root-win._dx}+{e.y_root-win._dy}")
        card.bind("<Button-3>", start_mv); card.bind("<B3-Motion>", do_mv)
        win.bind("<Escape>", lambda *_: win.destroy())
        win.bind("<FocusOut>", lambda e: win.destroy() if not win.focus_displayof() else None)
        
        self.root.update_idletasks()
        win.update_idletasks()
        ow, oh = win.winfo_width(), win.winfo_height()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        x, y = (sw - ow) // 2, (sh - oh) // 2
        win.geometry(f"{ow}x{oh}+{x}+{y}")
        self._update_eq_ui_state()
        
#####################################################################################################

    def show_search_overlay(self):
        if getattr(self, '_authentication_active', False):
            return
        # Prevent opening if in radio mode or already open
        if self.display_radio or self._is_searching or self.is_spinning_downloading:
            return
        if getattr(self, 'search_overlay', None) and self.search_overlay.winfo_exists():
            self.close_search_overlay(getattr(self, "_was_main_overlay_open_before_search", True))
            return

        self.show_key_hints(force_state=False)
        self._was_main_overlay_open_before_search = bool(self.window and self.window.winfo_exists())
        if self._was_main_overlay_open_before_search:
            self.close_overlay(remember=False)
        
        search_recommendation = ""
        if hasattr(self, 'MusicPlayer'):
            searching_mechanism = self.MusicPlayer.recommend
            type_to_search = random.choice(['song', 'artist', 'search_history'])
            if type_to_search == 'song':
                choices = searching_mechanism.analyze_top_songs(top_n=5)
                search_result = random.choice(choices) if choices else ('', 0, '')
                clean_song_title = self.TitleCleaner.clean(search_result[0])
                split_song = clean_song_title.split(" - ", 1)
                if len(split_song) == 2:
                    search_recommendation = f"{split_song[1]} By {search_result[2]}".title()
                else:
                    search_recommendation = f"{clean_song_title} By {search_result[2]}".title()
                if not clean_song_title:
                    search_recommendation = ""
            elif type_to_search == 'artist':
                choices = searching_mechanism.analyze_top_artists(top_n=5)
                search_result = random.choice(choices) if choices else ('', 0)
                search_recommendation = search_result[0].title()
            elif type_to_search == 'search_history':
                search_recommendation = searching_mechanism.suggest_search_terms_str()

        # --- Search UI Setup ---
        self.search_overlay = tk.Toplevel(self.root)
        self.search_overlay.overrideredirect(True)
        self.search_overlay.attributes("-topmost", True)
        self.search_overlay.configure(bg=self.theme.COLORS["window_bg"])
        
        main_frame = ttk.Frame(self.search_overlay, style="Accent.TFrame", padding=20)
        main_frame.pack(fill="both", expand=True)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(1, weight=1)

        top_frame = ttk.Frame(main_frame, style="Accent.TFrame")
        top_frame.grid(row=0, column=0, sticky="ew", pady=(0, 15))
        top_frame.columnconfigure(0, weight=1)

        search_var = tk.StringVar()
        search_entry = ttk.Entry(
            top_frame,
            textvariable=search_var,
            font=self.theme.FONTS["ui_normal"],
            style="TEntry"
        )
        search_entry.grid(row=0, column=0, sticky="ew", ipady=5)
        
        # Frame for checkboxes
        checkbox_frame = ttk.Frame(top_frame, style="Accent.TFrame")
        checkbox_frame.grid(row=0, column=1, sticky="e", padx=(10, 0))

        if hasattr(self, 'MusicPlayer'):
            download_permanently_var = tk.BooleanVar(value=self.MusicPlayer.youtube_download_permanently)
            youtube_search_var = tk.BooleanVar(value=self.MusicPlayer.do_youtube_search)
        else:
            download_permanently_var = tk.BooleanVar(value=False)
            youtube_search_var = tk.BooleanVar(value=True)
            
        youtube_checkbox = ttk.Checkbutton(
            checkbox_frame,
            text="YouTube",
            variable=youtube_search_var,
            style="TCheckbutton"
        )
        youtube_checkbox.pack(side="left")
            
        download_checkbox = ttk.Checkbutton(
            checkbox_frame,
            text="Download",
            variable=download_permanently_var,
            command=lambda: setattr(self.MusicPlayer, 'youtube_download_permanently', download_permanently_var.get()) if hasattr(self, 'MusicPlayer') else None,
            style="TCheckbutton"
        )
        download_checkbox.pack(side="left", padx=(5,0))

        # --- Results List ---

        results_frame = ttk.Frame(main_frame, style="Accent.TFrame")
        results_frame.grid(row=1, column=0, sticky="nsew")
        results_frame.rowconfigure(0, weight=1)
        results_frame.columnconfigure(0, weight=1)

        results_list = tk.Listbox(
            results_frame, font=self.theme.FONTS["ui_list"], bg=self.theme.COLORS["entry_bg"],
            fg=self.theme.COLORS["text"], selectmode="single", activestyle="none",
            selectbackground=self.theme.COLORS["accent"], selectforeground=self.theme.COLORS["frame_bg"],
            relief="flat", borderwidth=0, highlightthickness=0
        )
        
        scrollbar = ttk.Scrollbar(results_frame, orient="vertical", command=results_list.yview)
        results_list.config(yscrollcommand=scrollbar.set)
        scrollbar.grid(row=0, column=1, sticky="ns")
        results_list.grid(row=0, column=0, sticky="nsew")

        # --- Loading & Animation Widgets ---
        search_spinner_label = ttk.Label(results_frame, text="", style="Status.TLabel", font=self.theme.FONTS["fixed_width"], background=self.theme.COLORS["entry_bg"])
        download_canvas = tk.Canvas(main_frame, bg=self.theme.COLORS["window_bg"], highlightthickness=0)
        
        current_results = []
        
        # --- Loading Animation Functions ---
        def _animate_search_spinner(spinner_chars, index=0):
            if self._is_searching:
                search_spinner_label.config(text=f"Searching {spinner_chars[index]}")
                self._spinner_after_id = self.search_overlay.after(150, _animate_search_spinner, spinner_chars, (index + 1) % len(spinner_chars))

        def _start_search_spinner():
            results_list.grid_remove()
            scrollbar.grid_remove()
            search_spinner_label.place(relx=0.5, rely=0.5, anchor="center")
            self._is_searching = True
            _animate_search_spinner("|/-\\")

        def _stop_search_spinner():
            self._is_searching = False
            search_spinner_label.place_forget()
            results_list.grid()
            scrollbar.grid()
            
        def _animate_downloading(angle=0):
            if not (hasattr(self, 'search_overlay') and self.search_overlay.winfo_exists()): return
            
            download_canvas.delete("all")
            w, h = download_canvas.winfo_width(), download_canvas.winfo_height()
            if w < 10 or h < 10: # Wait for canvas to be sized
                self._download_after_id = self.search_overlay.after(50, _animate_downloading, angle)
                return

            cx, cy = w // 2, h // 2
            r_outer, r_inner, r_label = min(cx, cy) * 0.7, min(cx, cy) * 0.3, min(cx, cy) * 0.25

            # Draw spinning record
            download_canvas.create_oval(cx-r_outer, cy-r_outer, cx+r_outer, cy+r_outer, fill="#111", outline=self.theme.COLORS["accent"], width=2)
            for i in range(12): # Grooves
                r = r_inner + (r_outer - r_inner) * (i / 12)
                download_canvas.create_oval(cx-r, cy-r, cx+r, cy+r, outline="#333", width=1)
            
            p1_angle = (angle + 45) * (pi / 180)
            p2_angle = (angle + 135) * (pi / 180)
            download_canvas.create_line(cx + r_inner * cos(p1_angle), cy + r_inner * sin(p1_angle), 
                                      cx + r_outer * cos(p1_angle), cy + r_outer * sin(p1_angle), 
                                      fill=self.theme.COLORS["accent"], width=3)
            download_canvas.create_line(cx + r_inner * cos(p2_angle), cy + r_inner * sin(p2_angle), 
                                      cx + r_outer * cos(p2_angle), cy + r_outer * sin(p2_angle), 
                                      fill=self.theme.COLORS["accent"], width=3)

            download_canvas.create_oval(cx-r_label, cy-r_label, cx+r_label, cy+r_label, fill=self.theme.COLORS["accent_active"], outline="")
            download_canvas.create_text(cx, cy, text="♪", font=("Segoe UI Symbol", int(r_label*1.2)), fill="#111")
            
            self._download_after_id = self.search_overlay.after(150, _animate_downloading, angle + 15)

        def _show_download_animation():
            top_frame.grid_remove()
            results_frame.grid_remove()
            
            # Set the window to be semi-transparent
            self.search_overlay.attributes('-alpha', 0.2)
            
            # Make the entire animation window ignore mouse clicks.
            try:
                window_id = self.search_overlay.winfo_id()
                hwnd = ctypes.windll.user32.GetParent(window_id) or window_id
                style = ctypes.windll.user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
                style = style | WS_EX_LAYERED | WS_EX_TRANSPARENT
                ctypes.windll.user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)
            except Exception as e:
                ll.error(f"Failed to set click-through style: {e}")
                
            # Make window background transparent for the animation
            self.search_overlay.attributes('-transparentcolor', self.theme.COLORS["window_bg"])
            download_canvas.grid(row=0, column=0, rowspan=2, sticky="nsew")
            _animate_downloading()
            
        # --- Search & Download Logic ---
        def _update_ui_with_results(results):
            if not (hasattr(self, 'search_overlay') and self.search_overlay.winfo_exists()): return
            _stop_search_spinner()
            results_list.delete(0, tk.END)
            current_results.clear()
            
            if results:
                for raw_title, path_or_url, song_type in results:
                    if song_type == 'title_only':
                        rlist_width = results_list.winfo_width()
                        font_size = font.Font(font=results_list.cget("font")).measure('0')
                        spacing_char_count = (rlist_width // font_size) - abs(len(raw_title) // 2) - 5
                        filler_spacing = " " * spacing_char_count
                        
                        results_list.insert(tk.END, f"{filler_spacing}{raw_title}")
                        current_results.append((raw_title, raw_title, song_type))
                        continue
                    if song_type == 'playlist':
                        results_list.insert(tk.END, f" [Playlist] {raw_title}")
                        current_results.append((raw_title, path_or_url, song_type))
                        continue
                    is_youtube = song_type == 'url'
                    cleaned_title = self.TitleCleaner.clean(raw_title) if not is_youtube else raw_title
                    type_tag = "" if not youtube_search_var.get() else ("[YouTube]" if is_youtube else "[Local]")
                    results_list.insert(tk.END, f" {type_tag} {cleaned_title}")
                    current_results.append((cleaned_title, path_or_url, song_type))
            else:
                results_list.insert(tk.END, "  No results found.")
                results_list.itemconfig(0, {'fg': self.theme.COLORS["placeholder"]})

        search_window = self.search_overlay

        def _search_thread_target(term, use_youtube, dont_log=False):
            try:
                if getattr(self, 'MusicPlayer', None) and not dont_log:
                    self.MusicPlayer.recommend.log_search(term)
                if use_youtube:
                    effective_term = f"{term} (OFFICIAL SONG)"
                    raw_results = self.MusicPlayer.get_search_term(term)
                    do_playlist = len(raw_results) > 5
                    raw_youtube = self.MusicPlayer.get_youtube_search(effective_term)
                    raw_results = [("---[ Local Songs ]---", None, 'title_only'), *raw_results, ("---[ YouTube Results ]---", None, 'title_only'), *raw_youtube]
                    
                    if do_playlist:
                        raw_results.insert(0, (term, term, 'playlist'))
                else:
                    raw_results = self.MusicPlayer.get_search_term(term)
            except Exception as e:
                ll.error(f"Search thread failed: {e}")
                raw_results = []
            finally:
                def finish_search():
                    if getattr(self, 'search_overlay', None) is search_window and search_window.winfo_exists():
                        _update_ui_with_results(raw_results)
                self.post_ui(finish_search)

        def _trigger_search(forced_term: str = None, dont_log: bool = False):
            if self._is_searching == True: return
            search_term = forced_term or search_var.get().strip()
            if not search_term:
                #results_list.delete(0, tk.END)
                #current_results.clear()
                return

            _start_search_spinner()
            Thread(target=_search_thread_target, args=(search_term,youtube_search_var.get(),dont_log), daemon=True).start()

        def youtube_command():
            if hasattr(self, 'MusicPlayer'):
                setattr(self.MusicPlayer, 'do_youtube_search', youtube_search_var.get())
            _trigger_search()
        youtube_checkbox.config(command=lambda:youtube_command())

        def on_key_release(_):
            if self._search_after_id:
                self.search_overlay.after_cancel(self._search_after_id)
            self._search_after_id = self.search_overlay.after(1000, _trigger_search)
        
        def _download_thread_target(path_or_url, song_type):
            try:
                if song_type =='url':
                    self.MusicPlayer.play_youtube_song(path_or_url)
                elif song_type == 'path':
                    self.MusicPlayer.play_song(path_or_url)
                elif song_type == 'playlist':
                    self.MusicPlayer.gather_playlist(path_or_url)
                else:
                    ll.error(f"Unknown song type for download: {song_type}")
            finally:
                def finish_download():
                    self.is_spinning_downloading = False
                    if getattr(self, 'search_overlay', None) is search_window and search_window.winfo_exists():
                        self.close_search_overlay(self._was_main_overlay_open_before_search)
                self.post_ui(finish_download)

        def handle_selection(_=None):
            selection_indices = results_list.curselection()
            if not selection_indices or not current_results: _trigger_search(); return
            index = selection_indices[0]
            if not (0 <= index < len(current_results)): return
            
            _, path_or_url, song_type = current_results[index]
            
            if song_type == 'title_only': # Skip Placeholders
                return
            
            self.is_spinning_downloading = True
            if song_type == 'url':
                _show_download_animation()
            
            Thread(target=_download_thread_target, args=(path_or_url, song_type), daemon=True).start()

        # --- Key Navigation & Bindings ---
        def handle_key_navigation(event):
            try:
                key, list_size = event.keysym, results_list.size()
                
                if key == "Escape": self.close_search_overlay(self._was_main_overlay_open_before_search); return "break"
                if key == "Return": handle_selection(); return "break"
                if list_size == 0 or key not in ("Down", "Up"): return

                current_selection = results_list.curselection()
                new_index = 0
                if current_selection:
                    current_index = current_selection[0]
                    new_index = min(current_index + 1, list_size - 1) if key == "Down" else max(current_index - 1, 0)
                
                results_list.selection_clear(0, tk.END)
                results_list.selection_set(new_index)
                results_list.activate(new_index)
                results_list.see(new_index)
            except Exception as e:
                ll.error(f"Key navigation error: {e}")
            return "break"

        search_entry.bind("<KeyRelease>", on_key_release)
        search_entry.bind("<Return>", handle_selection)
        search_entry.bind("<Down>", handle_key_navigation)
        search_entry.bind("<Up>", handle_key_navigation)
        
        results_list.bind("<Double-Button-1>", handle_selection)
        results_list.bind("<Return>", handle_selection)
        self.search_overlay.bind("<Escape>", lambda e: self.close_search_overlay(self._was_main_overlay_open_before_search))

        # --- Window Management ---
        def start_move(event): self.search_overlay._drag_start_x, self.search_overlay._drag_start_y = event.x_root, event.y_root
        def do_move(event):
            dx, dy = event.x_root - self.search_overlay._drag_start_x, event.y_root - self.search_overlay._drag_start_y
            self.search_overlay.geometry(f"+{self.search_overlay.winfo_x() + dx}+{self.search_overlay.winfo_y() + dy}")
            self.search_overlay._drag_start_x, self.search_overlay._drag_start_y = event.x_root, event.y_root
        
        main_frame.bind("<Button-1>", start_move); main_frame.bind("<B1-Motion>", do_move)
        top_frame.bind("<Button-1>", start_move); top_frame.bind("<B1-Motion>", do_move)

        # --- Finalize and Show ---
        self.search_overlay.update_idletasks()
        width, height = 500, 400
        x = (self.search_overlay.winfo_screenwidth() // 2) - (width // 2)
        y = (self.search_overlay.winfo_screenheight() // 2) - (height // 2)
        self.search_overlay.geometry(f"{width}x{height}+{x}+{y}")
        
        self.search_overlay.deiconify()
        self.search_overlay.lift()
        self.search_overlay.grab_set()
        results_frame.focus()
        search_entry.focus_force()
        
        if search_recommendation != "":
            ll.print(f"Search recommendation: {search_recommendation}")
            self.search_overlay.after(0, _trigger_search, search_recommendation, True)

    def close_search_overlay(self, restore_main_overlay=False):
        if hasattr(self, 'search_overlay') and self.search_overlay and self.search_overlay.winfo_exists():
            for attribute in ('_spinner_after_id', '_download_after_id'):
                identity = getattr(self, attribute, None)
                if identity:
                    try:
                        self.search_overlay.after_cancel(identity)
                    except tk.TclError:
                        pass
                    setattr(self, attribute, None)
            if self._search_after_id:
                try:
                    self.search_overlay.after_cancel(self._search_after_id)
                except tk.TclError:
                    pass
                self._search_after_id = None
            self._is_searching = False
            self.search_overlay.grab_release()
            self.search_overlay.destroy()
            self.search_overlay = None
            if restore_main_overlay: self.open_overlay()
            
#####################################################################################################

    def _wireless_trigger_pause(self):
        if not self.display_radio:
            self._trigger_pause()
        else:
            self._trigger_radio_toggle()

    def _trigger_skip_previous(self):
        try:
            if hasattr(self, 'MusicPlayer') and self.playerState and not self.display_radio:
                self.MusicPlayer.skip_previous()
        except Exception as e:
            ll.error(f"Error in skip previous trigger: {e}")

    def _trigger_skip_next(self):
        try:
            if hasattr(self, 'MusicPlayer') and self.playerState and not self.display_radio:
                self.MusicPlayer.skip_next()
        except Exception as e:
            ll.error(f"Error in skip next trigger: {e}")

    def _trigger_pause(self):
        if hasattr(self, 'MusicPlayer') and self.playerState and not self.display_radio:
            self.MusicPlayer.pause() # Assuming pause toggles
            
    def _trigger_volume_up(self):
        try:
            if getattr(self, 'MusicPlayer', None) and self.playerState:
                self.MusicPlayer.up_volume()
        except Exception as e:
            ll.error(f"Error in volume up trigger: {e}")
            
    def _trigger_volume_dwn(self):
        try:
            if getattr(self, 'MusicPlayer', None) and self.playerState:
                self.MusicPlayer.dwn_volume()
        except Exception as e:
            ll.error(f"Error in volume down trigger: {e}")
            
    def _trigger_repeat(self):
        if hasattr(self, 'MusicPlayer') and self.playerState and not self.display_radio:
            self.MusicPlayer.repeat()
            
    def _trigger_lyrics_toggle(self):
        self.display_lyrics = not self.display_lyrics
        if self.MusicPlayer:
            self.MusicPlayer.set_lyrics_enabled(self.display_lyrics)
        if self.window and self.running_lyrics:
            self.root.after(0, self.update_display)
                
    def _trigger_radio_toggle(self): # Enable/Disable Radio Mode
        if hasattr(self, 'MusicPlayer') and monotonic() - self.triggerDebounce[0] >= self.triggerDebounce[1] and self.playerState:
            self.triggerDebounce[0] = monotonic()
            self.display_radio = not self.display_radio
            ll.debug(f"Radio mode toggled: {'ON' if self.display_radio else 'OFF'}")
            if hasattr(self.MusicPlayer, 'toggle_loop_cycle'):
                 self.MusicPlayer.toggle_loop_cycle(self.display_radio)
            if self.window:
                self.root.after(0, self.update_display)
            
    def _trigger_radio_station(self, atmpt = 0, max_loop = 5): # Scan for next station
        if hasattr(self, 'MusicPlayer') and self.display_radio and self.playerState:
            if monotonic() - self.triggerDebounce[0] >= self.triggerDebounce[1]:
                self.triggerDebounce[0] = monotonic()
                ll.print("Scanning for radio station...")
                self.set_radio_channel()
                if hasattr(self.MusicPlayer, 'set_radio_ip'):
                    if not self.MusicPlayer.set_radio_ip(self.radio_metric['current_ip']):
                        if atmpt < max_loop : self._trigger_radio_station(atmpt + 1) 
                if self.window:
                    self.root.after(0, self.update_display)
            else:
                ll.warn("Radio scan debounce: please wait.")

    def toggle_overlay(self):
        # Showing controls is independent of whether audio was switched off.
        if (getattr(self, '_overlay_visible', False)
                or (self.window and self.window.winfo_exists() and self.window.state() != 'withdrawn')):
            self.close_overlay()
        else:
            self.open_overlay()

    def _save_overlay_state(self):
        self.overlay_preferences.save(self._overlay_visible, self.playerState, self._last_position)

    def _place_overlay(self, position=None, width=None, height=None):
        if not self.window or not self.window.winfo_exists():
            return
        width = max(1, int(width if width is not None else self.window.winfo_width()))
        height = max(1, int(height if height is not None else self.window.winfo_height()))
        position = position if position is not None else (
            (self.window.winfo_x(), self.window.winfo_y()) if self._last_position is not None else None)
        areas = getattr(self, '_drag_work_areas', None) if getattr(self, '_overlay_dragging', False) else None
        corrected = clamp_position(position, width, height, areas or monitor_work_areas(self.root))
        if (width != self.window.winfo_width() or height != self.window.winfo_height()
                or corrected != (self.window.winfo_x(), self.window.winfo_y())):
            place_window(self.window, corrected, width, height)
        if corrected != self._last_position:
            self._last_position = corrected
            if not getattr(self, '_overlay_dragging', False):
                self._save_overlay_state()

    def toggle_player(self): # Turns the whole media player functionality On/Off
        if monotonic() - self.triggerDebounce[0] >= self.triggerDebounce[1]:
            self.triggerDebounce[0] = monotonic()
            self.playerState = not self.playerState
            
            if self.playerState: # Transitioning to ON
                ll.print("Player enabled. Opening overlay.")
                self.open_overlay()
                if hasattr(self, 'MusicPlayer'):
                    self.MusicPlayer.set_enabled(True)
            else: # Transitioning to OFF
                ll.print("Player disabled. Closing overlay and pausing music.")
                if hasattr(self, 'MusicPlayer'):
                    if self.display_radio:
                        self.display_radio = False
                        self.MusicPlayer.toggle_loop_cycle(self.display_radio)
                    self.MusicPlayer.set_enabled(False)
                self.close_overlay()
                if self.key_hints_popup:
                    self.key_hints_popup.destroy()
                    self.key_hints_popup = None

    def reboot_overlay(self):
        """Reboot the overlay, closing and reopening it."""
        if messagebox.askyesno(
            "Restart Music Player",
            "Are you sure you want to restart the Music Player?"
        ):
            import subprocess
            import sys
            from pathlib import Path
            executable = sys.executable
            if executable.lower().endswith('python.exe'):
                windowless = Path(executable).with_name('pythonw.exe')
                if windowless.is_file():
                    executable = str(windowless)
            from playerUtils import ProgramShutdown
            ProgramShutdown().register_cleanup(lambda: subprocess.Popen(
                [executable, str(Path(__file__).with_name('main.py'))],
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0))
            self.close_application()

#####################################################################################################

    def toggle_overlay_clickthrough(self, mode: bool):
        """Toggle Whether The Mouse Ignores The Display Or Not"""
        if not self.window or not self.window.winfo_exists():
            return
        window_id = self.window.winfo_id()
        hwnd = ctypes.windll.user32.GetParent(window_id) or window_id
        current_style = ctypes.windll.user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        
        if mode: new_style = current_style | WS_EX_LAYERED | WS_EX_TRANSPARENT
        else: new_style = current_style & ~ WS_EX_TRANSPARENT
        new_style |= WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        ctypes.windll.user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, new_style)
        ctypes.windll.user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                         SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def _set_hint_opacity(self, opacity):
        self._hint_opacity = max(0.0, min(1.0, opacity))
        if not self.canvas or not self.window or not self.window.winfo_exists():
            return
        identity = self.canvas_items.get('controls_hint')
        if identity is None:
            return
        target = self.theme.COLORS['text_dark'].lstrip('#')
        background = self.bg_color.lstrip('#')
        color = '#' + ''.join(f'{round(int(background[i:i+2], 16) * (1-self._hint_opacity) + int(target[i:i+2], 16) * self._hint_opacity):02x}'
                              for i in (0, 2, 4))
        self.canvas.itemconfig(identity, fill=color,
                               state='normal' if self._hint_opacity > 0 else 'hidden')

    def _fade_controls_hint(self, target, started=None, initial=None):
        self._hint_animation_after_id = None
        if self._closing or not self.window or not self.window.winfo_exists():
            return
        started = monotonic() if started is None else started
        initial = self._hint_opacity if initial is None else initial
        fraction = min(1.0, (monotonic() - started) / 0.2)
        self._set_hint_opacity(initial + (target - initial) * fraction)
        if fraction < 1:
            self._hint_animation_after_id = self.root.after(
                16, self._fade_controls_hint, target, started, initial)

    def _show_controls_hint(self):
        for attribute in ('_hint_animation_after_id', '_hint_hide_after_id'):
            identity = getattr(self, attribute, None)
            if identity is not None:
                self.root.after_cancel(identity)
                setattr(self, attribute, None)
        self._fade_controls_hint(1.0)

    def _schedule_controls_hint_hide(self):
        identity = getattr(self, '_hint_hide_after_id', None)
        if identity is not None:
            self.root.after_cancel(identity)
        self._hint_hide_after_id = self.root.after(10000, self._hide_controls_hint)

    def _hide_controls_hint(self):
        self._hint_hide_after_id = None
        identity = getattr(self, '_hint_animation_after_id', None)
        if identity is not None:
            self.root.after_cancel(identity)
        self._fade_controls_hint(0.0)

    def _cancel_overlay_hint(self):
        for attribute in ('_hint_animation_after_id', '_hint_hide_after_id'):
            identity = getattr(self, attribute, None)
            if identity is not None:
                self.root.after_cancel(identity)
                setattr(self, attribute, None)
        self._set_hint_opacity(0.0)

    def _on_overlay_mouse_button(self, x, y, pressed):
        if not self.window or not self.window.winfo_exists() or self.window.state() == 'withdrawn':
            return
        if pressed:
            wx, wy = self.window.winfo_x(), self.window.winfo_y()
            if wx <= x < wx + self.window.winfo_width() and wy <= y < wy + self.window.winfo_height():
                self._begin_overlay_drag(x, y)
        else:
            if self._overlay_dragging:
                self._place_overlay((self._win_start_x + x - self._drag_start_x,
                                     self._win_start_y + y - self._drag_start_y))
            self._finish_overlay_drag()

    def _begin_overlay_drag(self, x, y):
        if self._overlay_dragging or not self.window or not self.window.winfo_exists():
            return
        self._drag_start_x, self._drag_start_y = x, y
        self._win_start_x, self._win_start_y = self.window.winfo_x(), self.window.winfo_y()
        self._win_start_size_width, self._win_start_size_height = self.window.winfo_width(), self.window.winfo_height()
        self._drag_work_areas = monitor_work_areas(self.root)
        self._overlay_dragging = True
        self.clickThroughState = False
        self.toggle_overlay_clickthrough(False)
        self._show_controls_hint()
        self._overlay_drag_tick()

    def _overlay_drag_tick(self):
        self._drag_after_id = None
        if self._closing or not self._overlay_dragging:
            return
        self.parse_mouse_over_overlay()
        if self._overlay_dragging:
            self._drag_after_id = self.root.after(16, self._overlay_drag_tick)

    def _finish_overlay_drag(self):
        identity = getattr(self, '_drag_after_id', None)
        if identity is not None:
            self.root.after_cancel(identity)
            self._drag_after_id = None
        if not getattr(self, '_overlay_dragging', False):
            return
        self._overlay_dragging = False
        self._drag_work_areas = None
        self.clickThroughState = True
        self.toggle_overlay_clickthrough(True)
        self._place_overlay()
        self._save_overlay_state()
        if self.window and self.window.winfo_exists() and self.window.state() != 'withdrawn':
            self._schedule_controls_hint_hide()

    def parse_mouse_over_overlay(self):
        if not self.window or not self.window.winfo_exists() or self.window.state() == 'withdrawn': return

        # Get current overlay and mouse geometry
        currentPosition = self.mouseEvents.mouse_pos()
        wx, wy = self.window.winfo_x(), self.window.winfo_y()
        ww, wh = self.window.winfo_width(), self.window.winfo_height()
        
        # Continuously update the mouse tracker with the window's current position and size.
        self.mouseEvents.update_window(wx, wy, ww, wh)
        
        # The condition for interactivity is now that the right mouse is down AND
        # the press that initiated it occurred within the overlay's bounds.
        if self.mouseEvents.is_right_mouse_down() and (self._overlay_dragging or self.mouseEvents.was_press_in_window()):
            if not self._overlay_dragging:
                self._begin_overlay_drag(*currentPosition)
                return
            new_x = self._win_start_x + currentPosition[0] - self._drag_start_x
            new_y = self._win_start_y + currentPosition[1] - self._drag_start_y
            self._place_overlay((new_x, new_y))
        else:
            if self._overlay_dragging:
                self._place_overlay((self._win_start_x + currentPosition[0] - self._drag_start_x,
                                     self._win_start_y + currentPosition[1] - self._drag_start_y))
            self._finish_overlay_drag()
                    
    def handle_overlay_background_process(self, time_dilation_for_key_reset: float = 300):
        """Loop To Handle Draggability - OPTIMIZED FOR GAMING"""
        thread_tick_size = 0.25
        ticks_per_second = int(1 / thread_tick_size) 
        time_dial = int(time_dilation_for_key_reset * ticks_per_second)
        time_tick = 0
        
        while not self._closing:
            try:
                self.post_ui(self.parse_mouse_over_overlay)
            except Exception as E:
                ll.error(f"Cannot Toggle Mouse-Over Overlay: {E}")
            
            time_tick = (time_tick + 1) % time_dial 
            if time_tick == 0:
                ll.debug(f"Resetting Key Events")
                self.background_key_reset()
            
            if time_tick % (ticks_per_second * 5) == 0:
                self.post_ui(self.keep_overlay_on_top)
            
            sleep(thread_tick_size)

#####################################################################################################

    def toggle_lyrics(self, state: bool):
        if self.running_lyrics == state: return

        with self.text_lock:
            self.running_lyrics = state
            if not state: self.player_metric['player_lyrics'] = ""
        
        self.schedule_update()

    def wrap_text(self, text: str, max_chars_line: int = 30) -> str:
        if not text: return ""
        words = text.split()
        if not words: return ""

        lines, current_line = [], ""
        for word in words:
            if not current_line: current_line = word
            elif len(current_line) + 1 + len(word) <= max_chars_line: current_line += " " + word
            else: lines.append(current_line); current_line = word
        if current_line: lines.append(current_line)
        
        if len(lines) > 2 :
            midpoint_char = len(text) // 2
            split_point = text.rfind(' ', 0, midpoint_char + len(text)//4)
            if split_point == -1: split_point = midpoint_char
            line1, line2 = text[:split_point].strip(), text[split_point:].strip()
            return f"{line1}\n{line2}" if line2 else line1
        
        return "\n".join(lines)
    
#####################################################################################################

    def display_overlay(self):
        self.refresh_overlay_visibility()

    def _player_has_loaded_track(self):
        player = getattr(self, 'MusicPlayer', None)
        if player is None:
            return False
        ready = getattr(player, '_playback_ready', None)
        if ready is None or not ready.is_set():
            return False
        radio_mode = getattr(player, 'current_player_mode', None)
        if radio_mode is not None and radio_mode.is_set():
            radio = getattr(player, 'radio_client', None)
            if not radio or not getattr(radio, '_playing_track_id', None):
                return False
        elif not getattr(player, 'current_song', None):
            return False
        # current_song is assigned before decoder startup. The audio session's
        # event distinguishes a successfully loaded track, including paused ones.
        module = sys.modules.get(type(player).__module__)
        audio = getattr(player, 'AudioPlayer', None) or getattr(module, 'AudioPlayer', None)
        active = getattr(audio, '_play_event', None)
        return active is not None and active.is_set()

    def refresh_overlay_visibility(self):
        """UI-thread-only temporary gating; never overwrite remembered visibility."""
        if getattr(self, '_closing', False):
            return
        search = getattr(self, 'search_overlay', None)
        blocked = (getattr(self, '_authentication_active', False)
                   or not self._player_has_loaded_track()
                   or bool(search and search.winfo_exists()))
        wanted = bool(self._overlay_visible)
        if blocked or not wanted:
            self._finish_overlay_drag()
            if (getattr(self, '_hint_opacity', 0) > 0
                    or getattr(self, '_hint_animation_after_id', None) is not None
                    or getattr(self, '_hint_hide_after_id', None) is not None):
                self._cancel_overlay_hint()
            if self.window and self.window.winfo_exists() and self.window.state() != 'withdrawn':
                self.window.withdraw()
            return
        if not self.window or not self.window.winfo_exists() or self.window.state() == 'withdrawn':
            self.open_overlay(remember=False)

    def keep_overlay_on_top_init(self):
        self.root.bind("<FocusIn>", self.keep_overlay_on_top)
        self.root.bind("<FocusOut>", self.keep_overlay_on_top)

    def keep_overlay_on_top(self, event = None):
        """Keep the overlay window on top of all other windows."""
        try:
            if self.window and self.window.winfo_exists() and self.window.state() != 'withdrawn' and not self.window.attributes('-topmost'):
                self.window.attributes('-topmost', True)
            
            if hasattr(self, 'key_hints_popup') and self.key_hints_popup and self.key_hints_popup.winfo_exists() and not self.key_hints_popup.attributes('-topmost'):
                self.key_hints_popup.attributes('-topmost', True)
        except tk.TclError: pass

#####################################################################################################

    def center_window(self):
        if not (self.window and self.window.winfo_exists()): return
        self.window.update_idletasks()
        width, height = self.window.winfo_width(), self.window.winfo_height()
        if width <= 1 or height <= 1: 
            self.root.after(100, self.center_window)
            return
        
        position = clamp_position(None, width, height, monitor_work_areas(self.root))
        self._place_overlay(position)

    def _create_canvas_items_if_needed(self, init_draw = False):
        if self.canvas_items.get('bg') is None or init_draw == True:
            self.canvas_items['bg'] = self.canvas.create_rounded_box(0, 0, 1, 1, radius=15, color=self.bg_color)
            self.canvas_items['player_text'] = self.canvas.create_text(0, 0, font=self.theme.FONTS["main"], fill=self.theme.COLORS["text"], anchor=tk.N, justify=tk.CENTER)
            self.canvas_items['duration_text'] = self.canvas.create_text(0, 0, font=self.theme.FONTS["time"], fill=self.theme.COLORS["text_dark"], anchor=tk.N, justify=tk.CENTER)
            self.canvas_items['lyrics_text'] = self.canvas.create_text(0, 0, font=self.theme.FONTS["lyrics"], fill=self.theme.COLORS["text"], anchor=tk.N, justify=tk.CENTER)
            self.canvas_items['controls_hint'] = self.canvas.create_text(
                0, 0, text='Both Alt keys · Controls', font=self.theme.FONTS['hint'],
                fill=self.theme.COLORS['text_dark'], anchor=tk.N, justify=tk.CENTER,
                state='hidden')

    def open_overlay(self, remember=True):
        if remember:
            self._overlay_visible = True
            self._save_overlay_state()
        if getattr(self, '_authentication_active', False) or not self._player_has_loaded_track():
            return
        if getattr(self, 'search_overlay', None) is not None:
            self.close_search_overlay(self._was_main_overlay_open_before_search)
            try: del self.search_overlay
            except AttributeError: pass
        if self.window and self.window.winfo_exists():
            self.window.deiconify()
            self._place_overlay(self._last_position)
            self.window.lift()
            return

        self.window = tk.Toplevel(self.root)
        self.window.title('MusicApp · Player')
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes('-alpha', 0.7)
        self.window.attributes('-topmost', True)
        
        transparent_color = self.theme.COLORS["transparent"]
        self.window.attributes('-transparentcolor', transparent_color) 
        self.window.config(bg=transparent_color)
        self.toggle_overlay_clickthrough(self.clickThroughState)

        self._layout_key = None
        self.canvas = RoundedCanvas(self.window, bg=transparent_color, highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        
        self.update_display(init_draw=True)
        
        self.window.deiconify()
        self.window.update_idletasks()
        if self._last_position is None:
            self.center_window()
        else:
            self._place_overlay(self._last_position)

        self._drag_start_x, self._drag_start_y = 0, 0
        self._win_start_x, self._win_start_y = 0, 0
        self._overlay_dragging = False
        # Global right-button input owns dragging, including its first click
        # through this passive window. Avoid a second Tk motion path that could
        # move/repaint the overlay again for every native mouse message.

    def close_overlay(self, remember=True):
        self._finish_overlay_drag()
        self._cancel_overlay_hint()
        if self.window:
            try:
                self._place_overlay()
                self._last_position = (self.window.winfo_x(), self.window.winfo_y())
            except (tk.TclError, AttributeError, ValueError): pass
            self.window.destroy()
            self.window, self.canvas = None, None
            self.clickThroughState = True
        if remember:
            self._overlay_visible = False
            self._save_overlay_state()
            
    def update_display(self, init_draw = False):
        if not (self.window and self.canvas and self.window.winfo_exists()): return

        try:
            self._create_canvas_items_if_needed(init_draw)
            key = (self.player_metric['player_text'],
                   self.player_metric['player_lyrics'] if self.running_lyrics and self.display_lyrics else '',
                   len(self.player_metric['player_duration']))
            if not init_draw and key == self._layout_key:
                self.canvas.itemconfig(self.canvas_items['duration_text'], text=self.player_metric['player_duration'])
                return
            self._layout_key = key

            wrapped_player_text = self.wrap_text(self.player_metric['player_text'], 35)
            num_player_text_lines = wrapped_player_text.count('\n') + 1

            display_lyrics_text = ""
            lyrics_visible = self.running_lyrics and self.display_lyrics and self.player_metric['player_lyrics']
            if lyrics_visible:
                display_lyrics_text = self.wrap_text(self.player_metric['player_lyrics'], 40)

            main_font = self.theme.FONTS["main"]
            time_font = self.theme.FONTS["time"]
            lyrics_font = self.theme.FONTS["lyrics"]
            hint_font = self.theme.FONTS['hint']

            main_width = max(main_font.measure(line) for line in wrapped_player_text.split('\n')) if wrapped_player_text else 0
            time_width = time_font.measure(self.player_metric['player_duration'])
            lyrics_width = max(lyrics_font.measure(line) for line in display_lyrics_text.split('\n')) if display_lyrics_text else 0

            total_width = max(main_width, time_width, lyrics_width,
                              hint_font.measure('Both Alt keys · Controls')) + 2 * self.overlay_text_padding
            
            height_for_main_text = main_font.metrics("linespace") * num_player_text_lines
            height_for_time = time_font.metrics("linespace")
            num_lyrics_lines = display_lyrics_text.count('\n') + 1 if lyrics_visible else 0
            height_for_lyrics = (lyrics_font.metrics("linespace") * num_lyrics_lines) + (self.overlay_text_padding / 2) if lyrics_visible else 0
            height_for_hint = hint_font.metrics('linespace') + self.overlay_text_padding
            total_height = height_for_main_text + height_for_time + height_for_lyrics + height_for_hint + (2 * self.overlay_text_padding)

            self.canvas.delete(self.canvas_items['bg'])
            self.canvas_items['bg'] = self.canvas.create_rounded_box(0, 0, total_width, total_height, radius=self.overlay_corner_radius, color=self.bg_color)
            self.canvas.tag_lower(self.canvas_items['bg'])
            current_y = self.overlay_text_padding

            self.canvas.itemconfig(self.canvas_items['player_text'], text=wrapped_player_text)
            self.canvas.coords(self.canvas_items['player_text'], total_width / 2, current_y)
            current_y += height_for_main_text + (self.overlay_text_padding / 2)
            
            self.canvas.itemconfig(self.canvas_items['duration_text'], text=self.player_metric['player_duration'])
            self.canvas.coords(self.canvas_items['duration_text'], total_width / 2, current_y)
            current_y += height_for_time + (self.overlay_text_padding / 2 if lyrics_visible else 0)

            if lyrics_visible:
                self.canvas.itemconfig(self.canvas_items['lyrics_text'], text=display_lyrics_text, state='normal')
                self.canvas.coords(self.canvas_items['lyrics_text'], total_width / 2, current_y)
            else:
                self.canvas.itemconfig(self.canvas_items['lyrics_text'], state='hidden')
            self.canvas.coords(self.canvas_items['controls_hint'], total_width / 2,
                               total_height - self.overlay_text_padding - hint_font.metrics('linespace'))

            if self.window and self.window.winfo_exists():
                self._place_overlay(self._last_position, total_width, total_height)
                    
            self._update_scheduled = False

        except tk.TclError as e:
            # This can happen if the window is destroyed while an update is pending.
            # It's generally safe to ignore, but we can log it for debugging.
            ll.debug(f"TclError in update_display (safe to ignore): {e}")
        except Exception as e:
            ll.error(f"Unexpected error in update_display: {e}")
            # Optionally, display an error message on the overlay itself
            try:
                self.canvas.itemconfig(self.canvas_items['player_text'], text="! OVERLAY ERROR !")
                self.canvas.itemconfig(self.canvas_items['duration_text'], text=str(e))
            except: pass # Avoid secondary errors if canvas is broken

#####################################################################################################

    def post_ui(self, callback, *args, **kwargs):
        if not self._closing:
            try:
                self._ui_queue.put_nowait((callback, args, kwargs))
            except Full:
                pass

    def _ui_tick(self):
        if self._closing:
            return
        for _ in range(64):
            try:
                callback, args, kwargs = self._ui_queue.get_nowait()
            except Empty:
                break
            try:
                callback(*args, **kwargs)
            except Exception as exc:
                ll.error(f"Interface update: {exc}")
        self.refresh_overlay_visibility()
        if self._display_dirty:
            self._display_dirty = False
            self.update_display()
        now = monotonic()
        if self.window and now >= self._screen_check_due:
            self._screen_check_due = now + 1
            try:
                self._place_overlay()
            except tk.TclError:
                pass
        if self.window and not self._overlay_dragging and self.mouseEvents.is_right_mouse_down():
            try:
                self.parse_mouse_over_overlay()
            except tk.TclError:
                pass
        self.root.after(100, self._ui_tick)

    def schedule_update(self):
        self._display_dirty = True

    def close(self):
        if self._closing:
            return
        self._finish_overlay_drag()
        self._cancel_overlay_hint()
        if self.window and self.window.winfo_exists():
            self._place_overlay()
        self._save_overlay_state()
        self.overlay_preferences.close()
        self._closing = True
        self.readyForKeys = False
        if self.listener:
            self.listener.stop()
        self.mouseEvents.stop()
            
    def set_text(self, text: str):
        with self.text_lock:
            if self.player_metric['player_text'] == text: return
            self.player_metric['player_text'] = text
        self.schedule_update()
        
    def is_afk(self) -> bool:
        activity = _LastInput()
        activity.size = ctypes.sizeof(activity)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(activity)):
            ticks = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
            idle_seconds = ((ticks - activity.tick) & 0xFFFFFFFF) / 1000
            return idle_seconds >= self.mouseEvents.mouse_tracking['AFKTimeout']
        return monotonic() - self.mouseEvents.mouse_tracking['LastCheckedTime'] >= self.mouseEvents.mouse_tracking['AFKTimeout']
                
    def set_duration(self, current_seconds: float, total_seconds: float):
        def format_time(seconds):
            minutes = int(seconds // 60)
            seconds = int(seconds % 60)
            return f"{minutes}:{seconds:02d}"
        
        full_str = f"{format_time(current_seconds)} / {format_time(total_seconds)}"
        
        with self.text_lock:
            if self.player_metric['player_duration'] == full_str: return
            self.player_metric['player_duration'] = full_str
        self.schedule_update()

    def set_lyrics(self, text: str):
        with self.text_lock:
            if self.player_metric['player_lyrics'] == (text or ""):
                return
            self.player_metric['player_lyrics'] = text if text else ""
        self.schedule_update()

#####################################################################################################

    def set_radio_ips(self, ip_list: list):
        with self.text_lock:
            self.radio_metric['availability'] = list(set(ip_list))
            if not self.radio_metric['availability'] or self.radio_metric['current_ip'] not in self.radio_metric['availability']:
                self.radio_metric['current_ip'] = self.radio_metric['availability'][0] if self.radio_metric['availability'] else ''
        if self.display_radio:
            self.schedule_update()
        
    def _get_next_(self, items: list, value):
        if not items: return ''
        try:
            current_index = items.index(value)
            return items[(current_index + 1) % len(items)]
        except ValueError: return items[0]
    
    def set_radio_channel(self):
        with self.text_lock:
            ip_list = self.radio_metric['availability']
            if len(ip_list) > 0:
                self.radio_metric['current_ip'] = self._get_next_(ip_list, self.radio_metric['current_ip'])
                ll.print(f"Radio IP set to: {self.radio_metric['current_ip']}")
            else:
                self.radio_metric['current_ip'] = ''
                ll.warn("No radio IPs available to select.")
        
    def close_application(self):
        """Properly close the entire application"""
        from playerUtils import ProgramShutdown
        ProgramShutdown().shutdown()

#####################################################################################################
