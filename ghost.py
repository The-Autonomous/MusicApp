import ctypes, os
import tkinter as tk
from tkinter import font, messagebox, ttk
from threading import Lock, RLock, Thread
from pynput import keyboard, mouse
from time import monotonic, sleep
from math import cos, pi, sin, ceil
from typing import Iterator
import json

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
            "transparent": "gray1"
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
            "icon": ("Arial Unicode MS", 11)
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
        return self.create_polygon(points, fill=color)

class MouseTracker:
    def __init__(self):
        self.user32 = ctypes.windll.user32
        self._right_button_pressed = False
        self.window_proportions = [0, 0, 0, 0]
        
        # Initialize the pynput listener.
        # Crucially, set daemon=True. This means the thread will automatically exit
        # when the main program exits, even if you forget to call .stop().
        self.listener = mouse.Listener(
            on_click=self._on_click,
            daemon=True # This makes the thread a daemon thread
        )
        self.listener.start() # Start the listener thread immediately upon initialization

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
            if button == mouse.Button.right and self.calc_pos(x, y, *self.window_proportions):
                self._right_button_pressed = pressed
            ll.debug(f"Mouse tracker got key {'Pressed' if pressed else 'Released'} {button} at ({x}, {y})") # Uncomment for detailed pynput debugging
        except Exception as E:
            ll.warn(f"Mouse tracker met unexpected error {E}")

    def mouse_pos(self):
        """Returns [x, y] of mouse cursor (works in fullscreen games).
        This method typically works without admin rights for basic cursor position,
        so it can use ctypes in both admin and non-admin modes.
        """
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
        pt = POINT()
        self.user32.GetCursorPos(ctypes.byref(pt))
        return [pt.x, pt.y]

    def is_right_mouse_down(self):
        """Returns True if right mouse button is pressed."""
        return self._right_button_pressed

    def stop(self):
        """Stops the pynput listener if it was started and is still active."""
        if hasattr(self, 'listener') and self.listener.is_alive():
            ll.print("Stopping pynput listener thread.")
            self.listener.stop()
            # Use .join() only if you need to ensure the thread has completely terminated
            # before proceeding, otherwise, daemon=True is often enough for exit.
            # If your app needs a very specific shutdown order, keep .join().
            self.listener.join(timeout=1) # Give it a short timeout to terminate
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
    
    def __init__(self, root):
        ### Root ###
        self.root = root
        self.theme = ThemeManager()
        self.theme.apply_ttk_styles(self.root)
        
        self.window = None
        self.canvas = None
        self._last_position = None
        self.key_hints_popup = None
        self.modification_status_label = None # For "Listening..." message
        
        ### Mouse ###
        self.mouseEvents = MouseTracker()
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
            'lyrics_text': None
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
        self.playerState = True # True If Player Is On False If Player Is Off
        
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
        Thread(target=self.handle_overlay_background_process, daemon=True).start() # Handle Dragability (Needs Seperate Thread For It To Work Even When No Display Updates Occur)
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
                if isinstance(new_required_keys, list) and len(new_required_keys) == 2 and new_required_keys[0] == 'alt':
                    action['required'] = new_required_keys
                else:
                    ll.warn(f"Warning: Invalid custom binding for {action_id} in settings file. Using default.")

    def _rebuild_key_maps(self):
        self.all_existing_keys = set()
        for act in self.key_actions:
            for k_raw in act['required']:
                self.all_existing_keys.add(k_raw.lower()) # Ensure lowercase
            for f_raw in act.get('forbidden', []):
                if f_raw != 'ALL':
                    self.all_existing_keys.add(f_raw.lower()) # Ensure lowercase
        
        self.keys_pressed = {k: False for k in self.all_existing_keys}

        for act in self.key_actions:
            act['required'] = [key.lower() for key in act['required']] # Ensure required keys are lowercase
            if 'forbidden' in act and 'ALL' in act['forbidden']:
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
                action()

        if self.is_listening_for_modification:
            if name == 'escape':
                self._cancel_key_modification(refresh_hints=True)
                return

            # We are expecting 'alt' + new_key. 'alt' is implicit.
            # The 'name' here is the new_key.
            # Ignore standalone modifiers as the new distinguishing key, unless it's shift for e.g. Alt+Shift
            # For simplicity, we take any non-'alt' key as the new distinguishing key.
            if name in ('alt', 'ctrl'): # Cannot use alt or ctrl as the distinguishing key with 'alt'
                messagebox.showwarning("Invalid Key", f"Cannot use '{name.upper()}' as the distinguishing key with ALT. Try another key.")
                return # Wait for a different key

            # If 'name' is 'shift', 'right shift', or 'right alt', it's a valid part of a combo
            # The design is Alt + `name`.
            self.finalize_key_modification(name)
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
                    try:
                        self.root.after(0, action_func)
                    except Exception as e:
                        self.root.after(2000, action_func) # Retry after a short delay
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
        self.action_id_being_modified = action_id_to_modify
        self.is_listening_for_modification = True

        action_hint = "this action"
        for act in self.key_actions:
            if act['id'] == action_id_to_modify:
                action_hint = f"'{act['hint']}'"
                break
        
        if self.key_hints_popup and self.key_hints_popup.winfo_exists() and self.modification_status_label:
            self.modification_status_label.config(
                text=f"Press desired key to combine with ALT for {action_hint}.\n(e.g., press 'P' for ALT+P). Esc to cancel."
            )
            self.key_hints_popup.bind("<Escape>", self._cancel_key_modification_event)
        else:
            messagebox.showinfo("Modify Key", f"Listening for new key for {action_hint}.\nPress the key you want to use with ALT. Press Esc in this message box to cancel (if main window doesn't catch it).")
            # Fallback if popup isn't ideal for this state

    def _cancel_key_modification_event(self, event=None): # For tkinter event binding
        self._cancel_key_modification(refresh_hints=True)

    def _cancel_key_modification(self, refresh_hints=False):
        self.is_listening_for_modification = False
        self.action_id_being_modified = None
        if self.modification_status_label:
            self.modification_status_label.config(text="")
        
        if self.key_hints_popup and self.key_hints_popup.winfo_exists():
            self.key_hints_popup.unbind("<Escape>") # Unbind specific escape
            self.key_hints_popup.bind("<Escape>", lambda e: self.key_hints_popup.destroy()) # Rebind general close
            if refresh_hints: # If cancel came from an actual modification attempt
                self.key_hints_popup.destroy()
                self.key_hints_popup = None
                self.show_key_hints()


    def finalize_key_modification(self, new_distinguishing_key_name):
        self.is_listening_for_modification = False # Moved here, set immediately
        if self.modification_status_label:
            self.modification_status_label.config(text="")

        action_to_modify = next((a for a in self.key_actions if a['id'] == self.action_id_being_modified), None)
        if not action_to_modify:
            self._cancel_key_modification()
            return

        original_keys = list(action_to_modify['required'])
        
        # New binding is always ['alt', new_distinguishing_key_name]
        # Ensure new_distinguishing_key_name is not 'alt' itself.
        if new_distinguishing_key_name == 'alt':
            messagebox.showerror("Invalid Key", "'ALT' itself cannot be the distinguishing key when 'ALT' is already the base. Modification cancelled.")
            self._cancel_key_modification(refresh_hints=True)
            return

        new_required_keys = ['alt', new_distinguishing_key_name.lower()]

        # Check for conflicts
        for action in self.key_actions:
            if action['id'] != self.action_id_being_modified and action['required'] == new_required_keys:
                messagebox.showerror("Conflict", f"The combination '{' + '.join(k.upper() for k in new_required_keys)}' is already used by '{action['hint']}'.")
                self._cancel_key_modification(refresh_hints=True)
                return
            
        current_keys_str = " + ".join(k.upper() for k in original_keys)
        new_keys_str = " + ".join(k.upper() for k in new_required_keys)

        self.key_hints_popup.withdraw() # Hide while dialog is open
        try:
            # Adding parent=self.key_hints_popup also helps position the box correctly
            confirmed = messagebox.askyesno("Confirm Key Change",
                                            f"Change binding for '{action_to_modify['hint']}'\n"
                                            f"From: {current_keys_str}\n"
                                            f"To:   {new_keys_str}\n\n"
                                            f"Are you sure?",
                                            parent=self.key_hints_popup)
        finally:
            # This block ALWAYS runs, ensuring the hints window is visible again
            self.key_hints_popup.lift()

        if confirmed:
            action_to_modify['required'] = new_required_keys
            self.bindings_handler.update_setting(action_to_modify['id'], new_required_keys)
            self._rebuild_key_maps()
            
            if self.key_hints_popup and self.key_hints_popup.winfo_exists():
                self.key_hints_popup.destroy()
                self.key_hints_popup = None
            self.show_key_hints() # Reopen with updated bindings
        else:
            self._cancel_key_modification(refresh_hints=True) # User said no, refresh hints to clear state

        self.action_id_being_modified = None # Ensure reset after modification attempt

    def _confirm_reset_bindings(self):
        if messagebox.askyesno("Reset Key Bindings", 
                               "Are you sure you want to reset all modifiable key bindings to their defaults?\nThis cannot be undone.",
                               icon='warning'):
            self.bindings_handler.reset_settings()
            self._define_default_key_actions() # Reloads hardcoded defaults into self.key_actions
            # self._load_custom_bindings() # Not strictly needed as JSON is empty, but good for consistency
            self._rebuild_key_maps()
            
            if self.key_hints_popup and self.key_hints_popup.winfo_exists():
                self.key_hints_popup.destroy()
                self.key_hints_popup = None
            self.show_key_hints()
            
    def _on_alt_toggle(self, action):
        """Handles the logic for the 'Alt Not Required' checkbox."""
        # 1. Update the action's state in memory
        is_alt_needed = not action.get('alt_needed', True)
        action['alt_needed'] = is_alt_needed
        self.bindings_handler.update_setting(action['alt_needed'], is_alt_needed)

        # 2. Rebuild the key maps with the new setting
        self._rebuild_key_maps()
        
        # 3. Correctly refresh the hints window
        if self.key_hints_popup and self.key_hints_popup.winfo_exists():
            self.key_hints_popup.destroy()
            self.key_hints_popup = None  # Reset the variable

        self.show_key_hints(force_state=True) # Re-open with the new info
            
    def show_key_hints(self, force_state: bool = None):
        """ Show a popup with all key hints and their actions. """
        def close_popup(event=None):
            if self.key_hints_popup:
                try: self.key_hints_popup.destroy()
                except Exception: pass
                self.key_hints_popup = None
        
        if force_state is False:
            close_popup()
            return
        elif self.key_hints_popup and self.key_hints_popup.winfo_exists() and force_state is not True:
            close_popup()
            return

        self.key_hints_popup = tk.Toplevel(self.root)
        self.key_hints_popup.withdraw()
        self.key_hints_popup.overrideredirect(True)
        self.key_hints_popup.configure(bg=self.theme.COLORS["window_bg"])
        self.key_hints_popup.attributes("-topmost", True)

        main_frame = ttk.Frame(self.key_hints_popup, style="TFrame", padding=20)
        main_frame.pack(fill="both", expand=True)

        self.key_hints_popup.bind("<Escape>", close_popup)
        
        title_label = ttk.Label(main_frame, text="Music Player Controls", style="Header.TLabel")
        title_label.pack(pady=(0, 15), anchor="center")

        separator = tk.Frame(main_frame, height=2, bg=self.theme.COLORS["border"])
        separator.pack(fill="x", pady=(0, 15))

        list_container = ttk.Frame(main_frame)
        list_container.pack(fill="both", expand=True)

        canvas = tk.Canvas(list_container, bg=self.theme.COLORS["frame_bg"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_container, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)
        
        self.modification_status_label = ttk.Label(main_frame, text="", style="Status.TLabel", anchor="w", justify=tk.LEFT, wraplength=580)
        self.modification_status_label.pack(pady=(10, 5), padx=10, anchor="w")

        scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), "units"))
        
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        for i, action in enumerate(self.key_actions):
            keys_display = " + ".join(k.upper() for k in action['required'])
            
            # If alt is not needed, remove it from display (less clunky than a for loop)
            if not action.get('alt_needed', True):
                keys_display = keys_display.removeprefix("ALT + ")
            
            hint_text = action['hint']

            action_row_frame = ttk.Frame(scrollable_frame, style="TFrame")
            action_row_frame.pack(fill="x", pady=2, padx=5)

            # Use grid for better alignment
            action_row_frame.columnconfigure(0, weight=1)
            action_row_frame.columnconfigure(1, weight=3)
            action_row_frame.columnconfigure(2, weight=0)
            action_row_frame.columnconfigure(3, weight=0)

            key_label = ttk.Label(action_row_frame, text=keys_display, font=self.theme.FONTS["fixed_width"], anchor="w")
            key_label.grid(row=0, column=0, sticky="ew", padx=(10, 5))

            hint_label = ttk.Label(action_row_frame, text=f"→  {hint_text}", anchor="w", wraplength=400)
            hint_label.grid(row=0, column=1, sticky="ew", padx=5)

            if action.get('modifiable'):
                edit_btn = ttk.Button(
                    action_row_frame,
                    text="⚙️",
                    style="TButton",
                    command=lambda act_id=action['id']: self.initiate_key_modification(act_id)
                )
                edit_btn.grid(row=0, column=2, sticky="e", padx=(0, 10))
                
                alt_required_check = ttk.Checkbutton(
                    action_row_frame,
                    text=f" ALT{"" if action.get('alt_needed', True) else " Not"} Needed",
                    style="TCheckbutton",
                    variable=tk.BooleanVar(value=not action.get('alt_needed', True)),
                    command=lambda act=action: self._on_alt_toggle(act)
                )
                alt_required_check.grid(row=0, column=3, sticky="e", padx=(0, 10))
            
            # Hover effect
            widgets_in_row = [action_row_frame, key_label, hint_label]
            #if action.get('modifiable'): widgets_in_row.append(edit_btn)
            
            def on_enter(e, widgets=widgets_in_row):
                for w in widgets: w.configure(style="Hover.TFrame" if isinstance(w, ttk.Frame) else "Hover.TLabel")
            def on_leave(e, widgets=widgets_in_row):
                for w in widgets: w.configure(style="TFrame" if isinstance(w, ttk.Frame) else "TLabel")
            
            action_row_frame.bind("<Enter>", on_enter)
            action_row_frame.bind("<Leave>", on_leave)

        buttons_frame = ttk.Frame(main_frame)
        buttons_frame.pack(fill="x", pady=(15, 0), padx=10)
        buttons_frame.columnconfigure(0, weight=1)
        buttons_frame.columnconfigure(1, weight=1)

        reset_btn = ttk.Button(buttons_frame, text="Reset Bindings", command=self._confirm_reset_bindings, style="Warning.TButton")
        reset_btn.grid(row=0, column=0, sticky="ew", padx=5)

        close_btn = ttk.Button(buttons_frame, text="✖ Close", command=close_popup, style="Danger.TButton")
        close_btn.grid(row=0, column=1, sticky="ew", padx=5)

        def start_move(e):
            self.key_hints_popup._offset_x = e.x_root - self.key_hints_popup.winfo_rootx()
            self.key_hints_popup._offset_y = e.y_root - self.key_hints_popup.winfo_rooty()

        def do_move(e):
            x = e.x_root - self.key_hints_popup._offset_x
            y = e.y_root - self.key_hints_popup._offset_y
            self.key_hints_popup.geometry(f"+{x}+{y}")

        title_label.bind("<Button-1>", start_move)
        title_label.bind("<B1-Motion>", do_move)
        main_frame.bind("<Button-1>", start_move)
        main_frame.bind("<B1-Motion>", do_move)
        
        self.key_hints_popup.update_idletasks()
        width = max(720, self.key_hints_popup.winfo_reqwidth())
        height = max(500, self.key_hints_popup.winfo_reqheight())
        x_coord = (self.key_hints_popup.winfo_screenwidth() // 2) - (width // 2)
        y_coord = (self.key_hints_popup.winfo_screenheight() // 2) - (height // 2)

        self.key_hints_popup.geometry(f"{width}x{height}+{x_coord}+{y_coord}")
        self.key_hints_popup.deiconify()
        self.key_hints_popup.lift()

#####################################################################################################

    def get_gaming_mode(self):
        """Returns the current state of gaming mode."""
        try:
            print("Checking gaming mode state...")
            return getattr(self, "MusicPlayer", None).get_gaming_mode()
        except Exception as E:
            return False

    def set_gaming_mode(self, is_gaming_mode: bool):
        """Sets the state of gaming mode and updates the UI."""
        try:
            print(f"Setting gaming mode to {'ON' if is_gaming_mode else 'OFF'}...")
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
        try:
            if getattr(self, "_eq_window", None) and self._eq_window.winfo_exists():
                self._eq_window.destroy(); return
        except Exception as E:
            ll.warn(f"Failed to toggle EQ window state: {E}")
            return
            
        _eq_target = getattr(self, "MusicPlayer", None)
        if _eq_target is None:
            ll.warn("No MusicPlayer with EQ/echo found."); return
        bands = sorted(_eq_target.get_bands().keys())

        win = tk.Toplevel(self.root); win.overrideredirect(True)
        win.attributes("-topmost", True); win.configure(bg="#000")
        self._eq_window = win
        screen_w = self.root.winfo_screenwidth()
        per_knob = 64
        max_cols = max(1, (screen_w - 100)//per_knob)
        rows = ceil(len(bands) / max_cols)
        w = min(len(bands), max_cols)*per_knob + 40
        h = rows*110 + 190
        x = self.window.winfo_x() + self.window.winfo_width()//2 - w//2
        y = self.window.winfo_y() + self.window.winfo_height() + 20
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
            current = tuple(_eq_target.get_band(f) for f in bands)
            preset_var.set(preset_map.get(current, "Custom"))

        for i, freq in enumerate(bands):
            col = ttk.Frame(grid, style="Accent.TFrame")
            col.grid(row=i//max_cols, column=i%max_cols, padx=6, pady=2)
            lbl = f"{freq//1000}k" if freq >= 1000 else str(freq)
            ttk.Label(col, background=self.theme.COLORS["window_bg"], text=lbl).pack()
            init = _eq_target.get_band(freq, 0.0)
            if isinstance(init, tuple): init = init[0]
            callback = lambda g, f=freq: knob_changed(g, f)
            if freq >= fmax: callback = lambda g, f=freq: knob_changed(0, f)
            knob = EQKnob(col, radius=26, init_gain=init, callback=callback, bg=self.theme.COLORS["window_bg"])
            knob.pack()
            self.eq_knobs[freq] = knob
            self.all_eq_knobs.append(knob)

        def apply_preset(name):
            if name == "Custom": return
            for f, g in zip(bands, self.EQ_PRESETS[name]):
                self.eq_knobs[f].gain = g
                self.eq_knobs[f]._draw()
                if f < fmax:
                    _eq_target.set_band(f, g)
            preset_var.set(name)

        preset_menu = ttk.OptionMenu(card, preset_var, "Flat", *self.EQ_PRESETS.keys(), command=apply_preset, style="TMenubutton")
        preset_menu["menu"].config(tearoff=0, bg=self.theme.COLORS["entry_bg"], fg=self.theme.COLORS["text"],
                                   activebackground=self.theme.COLORS["button"], activeforeground=self.theme.COLORS["accent"], relief="flat")
        card.create_window(w//2, int(h*0.48), window=preset_menu, anchor="n")
        self.eq_preset_menu = preset_menu
        preset_var.set(preset_map.get(tuple(_eq_target.get_band(f) for f in bands), "Custom"))

        echo_frame = ttk.Frame(card, style="Accent.TFrame")
        echo_frame.place(relx=0.5, rely=0.63, anchor="n")

        delay_init = getattr(_eq_target, "echo", None)
        delay_ms   = delay_init.delay_ms if delay_init else 0
        wet_pct    = int(delay_init.wet*100) if delay_init else 0

        ttk.Label(echo_frame, background=self.theme.COLORS["window_bg"], text="Echo", font=(self.theme.FONTS["ui_normal"][0], 9, "bold")).grid(row=0, column=0, columnspan=2, pady=(0,3))

        def update_echo(_=None):
            if delay_ms == 0 and wet_pct == 0:
                _eq_target.disable_echo()
            elif not getattr(_eq_target, "echo", None):
                _eq_target.enable_echo(delay_ms=delay_ms, wet=wet_pct/100, feedback=0.35)
            else:
                _eq_target.set_echo(delay_ms=delay_ms, wet=wet_pct/100)

        delay_knob = PercentKnob(echo_frame, radius=20, bg=self.theme.COLORS["window_bg"], init_gain=delay_ms, callback=lambda v: (globals().update(delay_ms=int(max(0,v))), update_echo())[1])
        ttk.Label(echo_frame, background=self.theme.COLORS["window_bg"], text="Delay ms").grid(row=1, column=0, padx=6, pady=2)
        delay_knob.grid(row=2, column=0, padx=6)
        self.all_eq_knobs.append(delay_knob)

        wet_knob = PercentKnob(echo_frame, radius=20, bg=self.theme.COLORS["window_bg"], init_gain=wet_pct, callback=lambda v: (globals().update(wet_pct=int(max(0,v))), update_echo())[1])
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
        ctypes.windll.user32.SetCursorPos(x + (ow // 2), y + (oh // 2))
        self._update_eq_ui_state()
        
#####################################################################################################

    def show_search_overlay(self):
        # Prevent opening if in radio mode or already open
        if self.display_radio or self._is_searching or self.is_spinning_downloading:
            return
        if hasattr(self, 'search_overlay') and self.search_overlay.winfo_exists():
            self.close_search_overlay(getattr(self, "_was_main_overlay_open_before_search", True))
            return

        self.show_key_hints(force_state=False)
        self._was_main_overlay_open_before_search = bool(self.window and self.window.winfo_exists())
        if self._was_main_overlay_open_before_search:
            self.close_overlay()
            
        search_recommendation = getattr(self, 'MusicPlayer', None).recommend.analyze_top_artists(top_n=1)[0][0].title() if hasattr(self, 'MusicPlayer') else ""

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
                self.search_overlay.after(150, _animate_search_spinner, spinner_chars, (index + 1) % len(spinner_chars))

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
                self.search_overlay.after(50, _animate_downloading, angle)
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
            
            self.search_overlay.after(25, _animate_downloading, angle + 5)

        def _show_download_animation():
            top_frame.grid_remove()
            results_frame.grid_remove()
            
            # Set the window to be semi-transparent
            self.search_overlay.attributes('-alpha', 0.2)
            
            # Make the entire animation window ignore mouse clicks.
            try:
                hwnd = ctypes.windll.user32.GetParent(self.search_overlay.winfo_id())
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
                for raw_title, path_or_url, path_or_youtube in results:
                    is_youtube = path_or_youtube == 'url'
                    cleaned_title = self.TitleCleaner.clean(raw_title) if not is_youtube else raw_title
                    type_tag = "" if not youtube_search_var.get() else ("[YouTube]" if is_youtube else "[Local]")
                    results_list.insert(tk.END, f" {type_tag} {cleaned_title}")
                    current_results.append((cleaned_title, path_or_url, path_or_youtube))
            else:
                results_list.insert(tk.END, "  No results found.")
                results_list.itemconfig(0, {'fg': self.theme.COLORS["placeholder"]})

        def _search_thread_target(term, dont_log=False):
            try:
                if getattr(self, 'MusicPlayer', None) and not dont_log:
                    self.MusicPlayer.recommend.log_search(term)
                if youtube_search_var.get():
                    effective_term = f"{term} (OFFICIAL SONG)"
                    raw_results = self.MusicPlayer.get_search_term(term)
                    raw_results = [*raw_results, *self.MusicPlayer.get_youtube_search(effective_term)]
                    
                    # Reorganize to have most relative results at the top
                    if len(raw_results) > 10:
                        raw_results = self.MusicPlayer.get_search_term(term, raw_results, max_results=len(raw_results))
                else:
                    raw_results = self.MusicPlayer.get_search_term(term)
            except Exception as e:
                ll.error(f"Search thread failed: {e}")
                raw_results = []
            finally:
                if hasattr(self, 'search_overlay') and self.search_overlay.winfo_exists():
                    self.search_overlay.after(0, _update_ui_with_results, raw_results)

        def _trigger_search():
            if self._is_searching == True: return
            search_term = search_var.get().strip()
            if not search_term:
                results_list.delete(0, tk.END)
                current_results.clear()
                return

            _start_search_spinner()
            Thread(target=_search_thread_target, args=(search_term,), daemon=True).start()

        def youtube_command():
            if hasattr(self, 'MusicPlayer'):
                setattr(self.MusicPlayer, 'do_youtube_search', youtube_search_var.get())
            _trigger_search()
        youtube_checkbox.config(command=lambda:youtube_command())

        def on_key_release(_):
            if self._search_after_id:
                self.search_overlay.after_cancel(self._search_after_id)
            self._search_after_id = self.search_overlay.after(1000, _trigger_search)
        
        def _download_thread_target(path_or_url, is_youtube):
            try:
                if is_youtube:
                    self.MusicPlayer.play_youtube_song(path_or_url)
                else:
                    self.MusicPlayer.play_song(path_or_url)
            finally:
                if hasattr(self, 'search_overlay') and self.search_overlay.winfo_exists():
                    self.is_spinning_downloading = False
                    self.search_overlay.after(0, self.close_search_overlay, self._was_main_overlay_open_before_search)

        def handle_selection(_=None):
            selection_indices = results_list.curselection()
            if not selection_indices or not current_results: _trigger_search(); return
            index = selection_indices[0]
            if not (0 <= index < len(current_results)): return
            
            _, path_or_url, is_youtube = current_results[index]
            self.is_spinning_downloading = True
            _show_download_animation()
            
            Thread(target=_download_thread_target, args=(path_or_url, is_youtube=='url'), daemon=True).start()

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
        search_entry.focus_force()
        
        if search_recommendation != "":
            Thread(target=_search_thread_target, args=(search_recommendation, True,), daemon=True).start()

    def close_search_overlay(self, restore_main_overlay=False):
        if hasattr(self, 'search_overlay') and self.search_overlay and self.search_overlay.winfo_exists():
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
            if hasattr(self, 'MusicPlayer') and not getattr(self, "_eq_window", None) and not self._eq_window.winfo_exists() and self.playerState:
                self.MusicPlayer.up_volume()
        except Exception as e:
            ll.error(f"Error in volume up trigger: {e}")
            
    def _trigger_volume_dwn(self):
        try:
            if hasattr(self, 'MusicPlayer') and not getattr(self, "_eq_window", None) and not self._eq_window.winfo_exists() and self.playerState:
                self.MusicPlayer.dwn_volume()
        except Exception as e:
            ll.error(f"Error in volume down trigger: {e}")
            
    def _trigger_repeat(self):
        if hasattr(self, 'MusicPlayer') and self.playerState and not self.display_radio:
            self.MusicPlayer.repeat()
            
    def _trigger_lyrics_toggle(self):
        self.display_lyrics = not self.display_lyrics
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
        if self.playerState:
            if self.window and self.window.winfo_exists():
                self.close_overlay()
            else:
                if not hasattr(self, 'MusicPlayer'):
                    ll.warn("MusicPlayer not initialized yet. Cannot open overlay properly.")
                    return
                try:
                    self.root.after(0, self.open_overlay)
                except tk.TclError as e:
                    ll.warn(f"Could not open overlay yet (TclError): {e}")
                except Exception as e:
                    ll.error(f"An unexpected error occurred trying to open overlay: {e}")

    def toggle_player(self): # Turns the whole media player functionality On/Off
        if monotonic() - self.triggerDebounce[0] >= self.triggerDebounce[1]:
            self.triggerDebounce[0] = monotonic()
            self.playerState = not self.playerState
            
            if self.playerState: # Transitioning to ON
                ll.print("Player enabled. Opening overlay.")
                self.open_overlay()
                if hasattr(self, 'MusicPlayer'):
                    self.MusicPlayer.pause(True) # True to unpause/play
            else: # Transitioning to OFF
                ll.print("Player disabled. Closing overlay and pausing music.")
                if hasattr(self, 'MusicPlayer'):
                    if self.display_radio:
                        self.display_radio = False
                        self.MusicPlayer.toggle_loop_cycle(self.display_radio)
                    self.MusicPlayer.pause(False) # False to pause
                self.close_overlay()
                if self.key_hints_popup:
                    self.key_hints_popup.destroy()
                    self.key_hints_popup = None

    def reboot_overlay(self):
        """Reboot the overlay, closing and reopening it."""
        if messagebox.askyesno(
            "Reboot Overlay",
            "Are you sure you want to reboot the Music Player?\nThis will restart the everything and you may lose the song you are actively listening to!"
        ):
            try:
                from adminRaise import Administrator
            except:
                from .adminRaise import Administrator
                
            ll.debug("Rebooting overlay and music player...")
            Administrator.elevate(True)

#####################################################################################################

    def toggle_overlay_clickthrough(self, mode: bool):
        """Toggle Whether The Mouse Ignores The Display Or Not"""
        hwnd = ctypes.windll.user32.GetParent(self.window.winfo_id())
        current_style = ctypes.windll.user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        
        if mode: new_style = current_style | WS_EX_LAYERED | WS_EX_TRANSPARENT
        else: new_style = current_style & ~ WS_EX_TRANSPARENT
        
        ctypes.windll.user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, new_style)

    def parse_mouse_over_overlay(self):
        if not self.window or not self.window.winfo_exists(): return
        if self.mouseEvents.is_right_mouse_down():
            currentPosition = self.mouseEvents.mouse_pos()
            self.clickThroughState = False
            self.toggle_overlay_clickthrough(self.clickThroughState)
            if not self._overlay_dragging and self._overlay_start_move:
                self._drag_start_x = currentPosition[0]
                self._drag_start_y = currentPosition[1]
                self._overlay_dragging = True
                self._win_start_x = self.window.winfo_x()
                self._win_start_y = self.window.winfo_y()
                
                if self.window and self.window.winfo_exists():
                    hwnd = ctypes.windll.user32.GetParent(self.window.winfo_id())
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
                    ctypes.windll.user32.SetActiveWindow(hwnd)
                    ctypes.windll.user32.SetFocus(hwnd)
                    ctypes.windll.user32.BringWindowToTop(hwnd)
                    
                    mouse_controller = mouse.Controller()
                    mouse_controller.press(mouse.Button.right); mouse_controller.release(mouse.Button.right)
                    mouse_controller.press(mouse.Button.left); mouse_controller.release(mouse.Button.left)
                    mouse_controller.press(mouse.Button.right)
        else:
            if not self.clickThroughState and not self._overlay_dragging:
                self.clickThroughState = True
                self.toggle_overlay_clickthrough(self.clickThroughState)
                try: self.root.after(100, self.keep_overlay_on_top)
                except: ll.error("Couldn't Load Root After.")
                    
    def handle_overlay_background_process(self, time_dilation_for_key_reset: float = 300):
        """Loop To Handle Draggability - OPTIMIZED FOR GAMING"""
        thread_tick_size = 0.25
        ticks_per_second = int(1 / thread_tick_size) 
        time_dial = int(time_dilation_for_key_reset * ticks_per_second)
        time_tick = 0
        
        while True:
            try:
                if self.window and self.window.winfo_exists():
                    self.parse_mouse_over_overlay()
            except Exception as E:
                ll.error(f"Cannot Toggle Mouse-Over Overlay: {E}")
            
            time_tick = (time_tick + 1) % time_dial 
            if time_tick == 0:
                ll.debug(f"Resetting Key Events")
                self.background_key_reset()
            
            if time_tick % (ticks_per_second * 5) == 0:
                self.keep_overlay_on_top()
            
            sleep(thread_tick_size)

#####################################################################################################

    def toggle_lyrics(self, state: bool):
        if self.running_lyrics == state: return

        with self.text_lock:
            self.running_lyrics = state
            if not state: self.player_metric['player_lyrics'] = ""
        
        if self.window and self.window.winfo_exists():
            self.root.after(0, self.update_display)

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
        while not hasattr(self, 'MusicPlayer'): sleep(1)
        self.root.after(0, self.open_overlay)

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
        
        x = (self.window.winfo_screenwidth() - width) // 2
        self.window.geometry(f'+{x}+20')
        self._last_position = (x, 20)

    def _create_canvas_items_if_needed(self, init_draw = False):
        if self.canvas_items.get('bg') is None or init_draw == True:
            self.canvas_items['bg'] = self.canvas.create_rounded_box(0, 0, 1, 1, radius=15, color=self.bg_color)
            self.canvas_items['player_text'] = self.canvas.create_text(0, 0, font=self.theme.FONTS["main"], fill=self.theme.COLORS["text"], anchor=tk.N, justify=tk.CENTER)
            self.canvas_items['duration_text'] = self.canvas.create_text(0, 0, font=self.theme.FONTS["time"], fill=self.theme.COLORS["text_dark"], anchor=tk.N, justify=tk.CENTER)
            self.canvas_items['lyrics_text'] = self.canvas.create_text(0, 0, font=self.theme.FONTS["lyrics"], fill=self.theme.COLORS["text"], anchor=tk.N, justify=tk.CENTER)

    def open_overlay(self):
        if hasattr(self, 'search_overlay'):
            self.close_search_overlay(self._was_main_overlay_open_before_search)
            try: del self.search_overlay
            except AttributeError: pass
        if self.window and self.window.winfo_exists():
            self.window.lift()
            return

        self.window = tk.Toplevel(self.root)
        self.window.overrideredirect(True)
        self.window.attributes('-alpha', 0.7)
        self.window.attributes('-topmost', True)
        
        transparent_color = self.theme.COLORS["transparent"]
        self.window.attributes('-transparentcolor', transparent_color) 
        self.window.config(bg=transparent_color)
        self.toggle_overlay_clickthrough(self.clickThroughState)

        self.canvas = RoundedCanvas(self.window, bg=transparent_color, highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        
        self.update_display(init_draw=True)
        
        if self._last_position: self.window.geometry(f"+{self._last_position[0]}+{self._last_position[1]}")
        else: self.root.after(0, self.center_window)

        self._drag_start_x, self._drag_start_y = 0, 0
        self._win_start_x, self._win_start_y = 0, 0
        self._overlay_dragging = False
        self._overlay_start_move = None

        def start_move(event):
            self._drag_start_x, self._drag_start_y = event.x_root, event.y_root
            if self.window:
                self._overlay_dragging = True
                self._win_start_x, self._win_start_y = self.window.winfo_x(), self.window.winfo_y()
                self._win_start_size_width, self._win_start_size_height = self.window.winfo_width(), self.window.winfo_height()

        def do_move(event):
            if self.window:
                dx, dy = event.x_root - self._drag_start_x, event.y_root - self._drag_start_y
                new_x, new_y = self._win_start_x + dx, self._win_start_y + dy
                self.window.geometry(f"+{new_x}+{new_y}")
                self._last_position = (new_x, new_y)
                self.mouseEvents.update_window(new_x, new_y, self._win_start_size_width, self._win_start_size_height)

        def do_stop(event): self._overlay_dragging = False

        self._overlay_start_move = start_move
        self.canvas.bind("<Button-3>", start_move)
        self.canvas.bind("<B3-Motion>", do_move)
        self.canvas.bind("<ButtonRelease-3>", do_stop)

    def close_overlay(self):
        if self.window:
            try:
                parts = self.window.geometry().split('+')
                if len(parts) == 3: self._last_position = (int(parts[1]), int(parts[2]))
            except (tk.TclError, AttributeError, ValueError): pass
            self.window.destroy()
            self.window, self.canvas = None, None
            self.clickThroughState = True
            
    def update_display(self, init_draw = False):
        if not (self.window and self.canvas and self.window.winfo_exists()): return

        try:
            self._create_canvas_items_if_needed(init_draw)

            wrapped_player_text = self.wrap_text(self.player_metric['player_text'], 35)
            num_player_text_lines = wrapped_player_text.count('\n') + 1

            display_lyrics_text = ""
            lyrics_visible = self.running_lyrics and self.display_lyrics and self.player_metric['player_lyrics']
            if lyrics_visible:
                display_lyrics_text = self.wrap_text(self.player_metric['player_lyrics'], 40)

            main_font = self.theme.FONTS["main"]
            time_font = self.theme.FONTS["time"]
            lyrics_font = self.theme.FONTS["lyrics"]

            main_width = max(main_font.measure(line) for line in wrapped_player_text.split('\n')) if wrapped_player_text else 0
            time_width = time_font.measure(self.player_metric['player_duration'])
            lyrics_width = max(lyrics_font.measure(line) for line in display_lyrics_text.split('\n')) if display_lyrics_text else 0

            total_width = max(main_width, time_width, lyrics_width) + 2 * self.overlay_text_padding
            
            height_for_main_text = main_font.metrics("linespace") * num_player_text_lines
            height_for_time = time_font.metrics("linespace")
            num_lyrics_lines = display_lyrics_text.count('\n') + 1 if lyrics_visible else 0
            height_for_lyrics = (lyrics_font.metrics("linespace") * num_lyrics_lines) + (self.overlay_text_padding / 2) if lyrics_visible else 0
            total_height = height_for_main_text + height_for_time + height_for_lyrics + (2 * self.overlay_text_padding)

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

            if self.window and self.window.winfo_exists():
                parts = self.window.geometry().split('+')
                if len(parts) == 3:
                    self.window.geometry(f'{int(total_width)}x{int(total_height)}+{parts[1]}+{parts[2]}')
                else:
                    self.window.geometry(f'{int(total_width)}x{int(total_height)}')
                    
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

    def schedule_update(self):
        if self.window and not self._update_scheduled:
            self._update_scheduled = True
            self.root.after(100, self.update_display)
            
    def set_text(self, text: str):
        with self.text_lock:
            if self.player_metric['player_text'] == text: return
            self.player_metric['player_text'] = text
        self.schedule_update()
                
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
            self.player_metric['player_lyrics'] = text if text else ""
        self.schedule_update()

#####################################################################################################

    def set_radio_ips(self, ip_list: list):
        with self.text_lock:
            self.radio_metric['availability'] = list(set(ip_list))
            if not self.radio_metric['availability'] or self.radio_metric['current_ip'] not in self.radio_metric['availability']:
                self.radio_metric['current_ip'] = self.radio_metric['availability'][0] if self.radio_metric['availability'] else ''
        if self.window and self.window.winfo_exists() and self.display_radio:
             self.root.after(0, self.update_display)
        
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
        self.root.destroy()
        os._exit(0)

#####################################################################################################