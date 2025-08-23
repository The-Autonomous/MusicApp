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
        # However, it's still good practice to explicitly call .stop() for clean shutdown.
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
    
    def __init__(self, root):
        ### Root ###
        self.root = root
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
        
        ### Music Player ###
        self.main_font = font.Font(family='Helvetica', size=14, weight='bold')
        self.time_font = font.Font(family='Times', size=12)
        self.lyrics_font = font.Font(family='Helvetica', size=11, weight='normal', slant='italic') # Adjusted lyrics font
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
        self.triggerDebounce = [0, 1.0] # Reduced debounce for faster UI response
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
                'action': self._trigger_skip_previous,
                'hint': "Skip To The Previous Song",
                'modifiable': True
            },
            {
                'id': 'skip_next',
                'required': ['alt', '='],
                'action': self._trigger_skip_next,
                'hint': "Skip To The Next Song",
                'modifiable': True
            },
            {
                'id': 'volume_down',
                'required': ['alt', '['],
                'action': self._trigger_volume_dwn,
                'hint': "Turn Down Music Volume",
                'modifiable': True
            },
            {
                'id': 'volume_up',
                'required': ['alt', ']'],
                'action': self._trigger_volume_up,
                'hint': "Turn Up Music Volume",
                'modifiable': True
            },
            {
                'id': 'pause_play',
                'required': ['alt', ';'],
                'action': self._trigger_pause,
                'hint': "Pause / Play The Music", # Clarified hint
                'modifiable': True
            },
            {
                'id': 'repeat_toggle',
                'required': ['alt', '\''],
                'action': self._trigger_repeat,
                'hint': "Enable / Disable Repeat Mode",
                'modifiable': True
            },
            {
                'id': 'lyrics_toggle_visibility',
                'required': ['alt', '/'],
                'action': self._trigger_lyrics_toggle,
                'hint': "Show / Hide Lyrics If Available", # Clarified hint
                'modifiable': True
            },
            {
                'id': 'radio_enable_toggle',
                'required': ['alt', 'a'],
                'action': self._trigger_radio_toggle,
                'hint': "Enable / Disable Radio Mode", # Clarified hint
                'modifiable': True
            },
            {
                'id': 'radio_scan_station',
                'required': ['alt', '`'],
                'action': self._trigger_radio_station,
                'hint': "Scan For Next Radio Station", # Clarified hint
                'modifiable': True
            },
            {
                'id': 'show_search',
                'required': ['alt', '\\'],
                'action': self.show_search_overlay,
                'hint': "Search Songs",
                'modifiable': True
            },
            {
                'id': 'show_eq_menu',
                'required': ['right alt', '\\'],
                'action': self.show_eq_overlay,
                'hint': "EQ Menu",
                'modifiable': True
            },
            {
                'id': 'toggle_overlay',
                'required': ['alt', 'shift'],
                'forbidden': ['ALL'],
                'action': self.toggle_overlay,
                'hint': "Show / Hide Music Player",
                'modifiable': False # Core function, specific modifiers
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
                'id': 'kill_all_python',
                'required': ['right alt', 'ctrl'],
                'action': self.close_application,
                'hint': "EMERGENCY: Close Player & Python Tasks", # Clarified hint
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
            required_met = all(self.keys_pressed.get(k, False) for k in action['required'])
            
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

        if messagebox.askyesno("Confirm Key Change",
                                 f"Change binding for '{action_to_modify['hint']}'\n"
                                 f"From: {current_keys_str}\n"
                                 f"To:   {new_keys_str}\n\n"
                                 f"Are you sure?"):
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
            
    def show_key_hints(self, force_state: bool = None):
        """ Show a popup with all key hints and their actions. 
        If force_state is True, it will always show the popup.
        If force_state is False, it will close the popup if it's already open.
        If force_state is None, it will toggle the popup visibility.
        """
        # Toggle if already open
        def close_popup(event=None):
            if self.key_hints_popup:
                try:
                    self.key_hints_popup.destroy()
                except Exception:
                    pass
                self.key_hints_popup = None
        
        if force_state == False:
            close_popup()
            return
        elif self.key_hints_popup and self.key_hints_popup.winfo_exists() and force_state is not True:
            close_popup()
            return

        self.key_hints_popup = tk.Toplevel(self.root)
        self.key_hints_popup.withdraw()
        self.key_hints_popup.overrideredirect(True)
        self.key_hints_popup.configure(bg="#1e1e1e")
        self.key_hints_popup.attributes("-topmost", True)

        self.key_hints_name = "✨ Music Player Controls ✨"

        # Main container frame with padding
        main_frame = tk.Frame(self.key_hints_popup, bg="#2e2e2e", bd=3, relief="ridge")
        main_frame.pack(fill="both", expand=True, padx=20, pady=20)

        self.key_hints_popup.bind("<Escape>", close_popup)
        # Bind FocusOut robustly: only close if focus is not in a child widget
        def on_focus_out(event):
            # If focus is not in the popup or any of its children, close
            widget = event.widget
            try:
                # Get the widget that now has focus
                focus_widget = widget.focus_get()
                if focus_widget is None or not str(focus_widget).startswith(str(self.key_hints_popup)):
                    close_popup()
            except Exception:
                close_popup()
        self.key_hints_popup.bind("<FocusOut>", on_focus_out)

        title_label = tk.Label(
            main_frame,
            text=self.key_hints_name,
            font=("Segoe UI", 20, "bold"),
            bg="#2e2e2e",
            fg="#00ffd5"
        )
        title_label.pack(pady=(0, 15), anchor="center")

        separator = tk.Frame(main_frame, height=2, bg="#555555")
        separator.pack(fill="x", pady=(0, 15))

        # Scrollable hint list area
        list_container = tk.Frame(main_frame, bg="#2e2e2e")
        list_container.pack(fill="both", expand=True)

        canvas = tk.Canvas(list_container, bg="#2e2e2e", highlightthickness=0)
        scrollbar = tk.Scrollbar(list_container, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas, bg="#2e2e2e")

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        # Mouse wheel scrolling support
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1*(event.delta/120)), "units")
                
        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        for i, action in enumerate(self.key_actions):
            keys_display = " + ".join(k.upper() for k in action['required'])
            hint_text = action['hint']

            action_row_frame = tk.Frame(scrollable_frame, bg="#2e2e2e")
            action_row_frame.pack(fill="x", pady=2, padx=5)

            label_text = f"{keys_display:<20} →  {hint_text}"
            tk.Label(
                action_row_frame,
                text=label_text,
                font=("Consolas", 12),
                bg="#2e2e2e",
                fg="#ffffff",
                anchor="w",
                justify=tk.LEFT,
                wraplength=600
            ).pack(side="left", padx=(10, 5), fill="x", expand=True)

            if action.get('modifiable'):
                tk.Button(
                    action_row_frame,
                    text="⚙️",
                    font=("Arial Unicode MS", 11),
                    bg="#555555",
                    fg="#ffffff",
                    activebackground="#777777",
                    relief="flat",
                    command=lambda act_id=action['id']: self.initiate_key_modification(act_id)
                ).pack(side="right", padx=(0, 10))

        # Modification status label
        self.modification_status_label = tk.Label(
            scrollable_frame,
            text="",
            font=("Segoe UI", 10, "italic"),
            fg="yellow",
            bg="#2e2e2e",
            anchor="w",
            justify=tk.LEFT,
            wraplength=580
        )
        self.modification_status_label.pack(pady=(10, 5), padx=10, anchor="w")

        # Buttons section
        buttons_frame = tk.Frame(main_frame, bg="#2e2e2e")
        buttons_frame.pack(fill="x", pady=(15, 0), padx=10)

        reset_btn = tk.Button(
            buttons_frame,
            text="Reset Bindings",
            command=self._confirm_reset_bindings,
            font=("Segoe UI", 12, "bold"),
            bg="#ffae42", fg="#000000",
            activebackground="#ff8c00", activeforeground="#000000",
            relief="raised", bd=2, padx=10, pady=5
        )
        reset_btn.pack(side="left", fill="x", expand=True, padx=5)

        close_btn = tk.Button(
            buttons_frame,
            text="✖ Close",
            command=self.key_hints_popup.destroy,
            font=("Segoe UI", 13, "bold"),
            bg="#ff4d4d", fg="#ffffff",
            activebackground="#ff1a1a", activeforeground="#ffffff",
            relief="raised", bd=2, padx=10, pady=5
        )
        close_btn.pack(side="right", fill="x", expand=True, padx=5)

        # Smooth Drag Fix
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

        # Set a MINIMUM size to prevent "tiny box"
        self.key_hints_popup.update_idletasks()
        width = max(720, self.key_hints_popup.winfo_width())
        height = max(500, self.key_hints_popup.winfo_height())

        screen_width = self.key_hints_popup.winfo_screenwidth()
        screen_height = self.key_hints_popup.winfo_screenheight()
        x_coord = (screen_width // 2) - (width // 2)
        y_coord = (screen_height // 2) - (height // 2)

        self.key_hints_popup.geometry(f"{width}x{height}+{x_coord}+{y_coord}")
        self.key_hints_popup.deiconify()
        self.key_hints_popup.lift()
        #self.key_hints_popup.after_idle(self.key_hints_popup.attributes, "-topmost", False)

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
        if not getattr(self, "_eq_window", None) or not self._eq_window.winfo_exists():
            return

        is_disabled = self.get_gaming_mode()

        # Disable/enable knobs and update their appearance
        for knob in self.all_eq_knobs:
            knob.disable(is_disabled)
            
        # Disable/enable preset menu
        if self.eq_preset_menu:
            self.eq_preset_menu.config(state="disabled" if is_disabled else "normal")

    def show_eq_overlay(self):
        """ Pops a draggable EQ + Echo overlay with rotary knobs. Works with AudioEQ ±12 dB and AudioEcho on self.MusicPlayer. """
        # ── raise if already open ─────────────────────────────────────────┐
        if getattr(self, "_eq_window", None) and self._eq_window.winfo_exists():
            self._eq_window.deiconify(); self._eq_window.lift(); return
        # ── locate audio engine ───────────────────────────────────────────┘
        _eq_target = getattr(self, "MusicPlayer", None)
        if _eq_target is None:
            ll.warn("No MusicPlayer with EQ/echo found."); return
        bands = sorted(_eq_target.get_bands().keys()) # [31‥16 000]
        # ── theme / style (run once per Tk) ───────────────────────────────
        style = ttk.Style(self.root)
        style.theme_use("clam") # unlock colours
        
        # Check if the styles are already defined to prevent re-creation
        if "Neon.TFrame" not in style.element_names():
            style.configure("Neon.TFrame", background="#1d1f24")
            style.configure("Neon.TLabel", background="#1d1f24", foreground="#d0d0d0", font=("Segoe UI", 9))
            
            style.configure("Neon.TMenubutton", background="#272b33", foreground="#ddd", relief="flat", padding=(6,2,10,2))
            style.map("Neon.TMenubutton", background=[("active", "#313640")], arrowcolor=[("active", "#00e8d0"), ("!disabled", "#aaa")])
            style.configure("Neon.Menu", background="#272b33", foreground="#ddd", relief="flat")
            
            style.configure("Neon.TCheckbutton", background="#1d1f24", foreground="#ddd", relief="flat", indicatorcolor="#272b33")
            style.map("Neon.TCheckbutton", 
                      indicatorcolor=[("selected", "#00e8d0")],
                      background=[("active", "#313640")]
            )
        # ── window shell ──────────────────────────────────────────────────
        win = tk.Toplevel(self.root); win.overrideredirect(True)
        win.attributes("-topmost", True); win.configure(bg="#000")
        self._eq_window = win
        screen_w = self.root.winfo_screenwidth()
        per_knob = 64
        max_cols = max(1, (screen_w - 100)//per_knob)
        rows = ceil(len(bands) / max_cols)
        w = min(len(bands), max_cols)*per_knob + 40
        h = rows*110 + 190 # rows + echo + presets
        x = self.window.winfo_x() + self.window.winfo_width()//2 - w//2
        y = self.window.winfo_y() + self.window.winfo_height() + 20
        win.geometry(f"{w}x{h}+{x}+{y}")
        card = tk.Canvas(win, width=w, height=h, bg="#1d1f24", highlightthickness=0)
        card.pack(fill="both", expand=True)
        card.create_rectangle(0, 0, w, h, fill="#1d1f24", outline="")
        # ── EQ knob grid ──────────────────────────────────────────────────
        grid = ttk.Frame(card, style="Neon.TFrame")
        grid.place(relx=0.5, rely=0.05, anchor="n")
        
        self.eq_knobs = {}
        self.all_eq_knobs = []
        fmax = 16000
        
        preset_var = tk.StringVar(value="Flat")
        
        # ── preset values ─────────────────────────────────────────────────
        self.EQ_PRESETS["Custom"] = None
        preset_map = { tuple(vals): name for name, vals in self.EQ_PRESETS.items() if vals is not None }

        def knob_changed(gain, freq):
            _eq_target.set_band(freq, gain)
            current = tuple(_eq_target.get_band(f) for f in bands)
            preset_var.set(preset_map.get(current, "Custom"))

        # ── knobs ─────────────────────────────────────────────────────────
        for i, freq in enumerate(bands):
            col = ttk.Frame(grid, style="Neon.TFrame")
            col.grid(row=i//max_cols, column=i%max_cols, padx=6, pady=2)
            lbl = f"{freq//1000}k" if freq >= 1000 else str(freq)
            ttk.Label(col, text=lbl, style="Neon.TLabel").pack()
            init = _eq_target.get_band(freq, 0.0)
            if isinstance(init, tuple): init = init[0]
            callback = lambda g, f=freq: knob_changed(g, f)
            if freq >= fmax: callback = lambda g, f=freq: knob_changed(0, f) # Less sensitive for very high freqs
            knob = EQKnob(col, radius=26, init_gain=init, callback=callback, bg="#1d1f24")
            knob.pack()
            self.eq_knobs[freq] = knob
            self.all_eq_knobs.append(knob)

        # ── presets ───────────────────────────────────────────────────────
        def apply_preset(name):
            if name == "Custom": return
            for f, g in zip(bands, self.EQ_PRESETS[name]):
                self.eq_knobs[f].gain = g
                self.eq_knobs[f]._draw()
                if f < fmax:
                    _eq_target.set_band(f, g)
            preset_var.set(name)

        preset_menu = ttk.OptionMenu(card, preset_var, "Flat", *self.EQ_PRESETS.keys(), command=apply_preset, style="Neon.TMenubutton")
        preset_menu["menu"].config(tearoff=0, bg="#272b33", fg="#ddd", activebackground="#313640", activeforeground="#00e8d0", relief="flat")
        card.create_window(w//2, int(h*0.48), window=preset_menu, anchor="n")
        self.eq_preset_menu = preset_menu # Store reference
        preset_var.set(preset_map.get(tuple(_eq_target.get_band(f) for f in bands), "Custom"))

        # ── Echo section ──────────────────────────────────────────────────
        echo_frame = ttk.Frame(card, style="Neon.TFrame")
        echo_frame.place(relx=0.5, rely=0.63, anchor="n")

        delay_init = getattr(_eq_target, "echo", None)
        delay_ms   = delay_init.delay_ms if delay_init else 0
        wet_pct    = int(delay_init.wet*100) if delay_init else 0

        ttk.Label(echo_frame, text="Echo", style="Neon.TLabel",
                font=("Segoe UI", 9, "bold")).grid(row=0, column=0,
                                                    columnspan=2, pady=(0,3))

        def update_echo(_=None):
            if delay_ms == 0 and wet_pct == 0:
                _eq_target.disable_echo()
            elif not getattr(_eq_target, "echo", None):
                _eq_target.enable_echo(delay_ms=delay_ms,
                                    wet=wet_pct/100, feedback=0.35)
            else:
                _eq_target.set_echo(delay_ms=delay_ms, wet=wet_pct/100)

        delay_knob = PercentKnob(echo_frame, radius=20, bg="#1d1f24",
                                init_gain=delay_ms,
                                callback=lambda v: (globals().update(delay_ms=int(max(0,v))),
                                                    update_echo())[1])
        ttk.Label(echo_frame, text="Delay ms", style="Neon.TLabel"
                ).grid(row=1, column=0, padx=6, pady=2)
        delay_knob.grid(row=2, column=0, padx=6)
        self.all_eq_knobs.append(delay_knob)

        wet_knob = PercentKnob(echo_frame, radius=20, bg="#1d1f24",
                            init_gain=wet_pct,
                            callback=lambda v: (globals().update(wet_pct=int(max(0,v))),
                                                update_echo())[1])
        ttk.Label(echo_frame, text="Wet %", style="Neon.TLabel"
                ).grid(row=1, column=1, padx=6, pady=2)
        wet_knob.grid(row=2, column=1, padx=6)
        self.all_eq_knobs.append(wet_knob)
        
        volume_knob = VolumeSlider(echo_frame, width=120, height=24, bg="#1d1f24", init_volume=int(_eq_target.get_volume() * 100), callback=lambda v: _eq_target.set_volume(v / 100, True))
        ttk.Label(echo_frame, text="Volume %", style="Neon.TLabel"
                ).grid(row=1, column=2, padx=6, pady=2)
        volume_knob.grid(row=2, column=2, padx=6)

        # ── Gaming Mode Section (New) ───────────────────────────────
        def toggle_gaming_mode_command():
            current_state = self._gaming_mode_bool_var.get()
            self.set_gaming_mode(current_state)

        gaming_mode_frame = ttk.Frame(card, style="Neon.TFrame")
        gaming_mode_frame.place(relx=0.1, rely=0.88, anchor="n")
        
        self._gaming_mode_bool_var = tk.BooleanVar(value=self.get_gaming_mode())
        self.gaming_mode_checkbox = ttk.Checkbutton(
            gaming_mode_frame,
            text="Gaming Mode",
            variable=self._gaming_mode_bool_var,
            command=toggle_gaming_mode_command,
            style="Neon.TCheckbutton"
        )
        self.gaming_mode_checkbox.pack(padx=10, pady=(5, 0))
        
        sync_frame = ttk.Frame(card, style="Neon.TFrame")
        sync_frame.place(relx=0.5, rely=0.93, anchor="n")
        
        if hasattr(self.MusicPlayer, 'radio_client'):  # Is client
            self._accept_eq_var = tk.BooleanVar(
                value=getattr(self.MusicPlayer.radio_client, '_accept_host_eq', False)
            )
            
            def toggle_host_eq():
                # Use the setter method to properly handle state changes
                self.MusicPlayer.radio_client.set_accept_host_eq(self._accept_eq_var.get())
            
            ttk.Checkbutton(
                sync_frame,
                text="Use Radio Host's Settings",
                variable=self._accept_eq_var,
                command=toggle_host_eq,
                style="Neon.TCheckbutton"
            ).pack()
            
        # ── make draggable & closable ───────────────────────────────────
        def start_mv(e): win._dx=e.x_root-win.winfo_x(); win._dy=e.y_root-win.winfo_y()
        def do_mv(e):    win.geometry(f"+{e.x_root-win._dx}+{e.y_root-win._dy}")
        card.bind("<Button-3>", start_mv); card.bind("<B3-Motion>", do_mv)
        win.bind("<Escape>", lambda *_: win.destroy())
        win.bind("<FocusOut>", lambda e: win.destroy() if not win.focus_displayof() else None)
        
        # ── flush ui & get real sizes ─────────────────────────────────────
        self.root.update_idletasks()
        win.update_idletasks()

        # get real overlay size
        ow = win.winfo_width()
        oh = win.winfo_height()

        # get total screen size
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()

        # compute position to center on screen
        x = (sw - ow) // 2
        y = (sh - oh) // 2

        # finally place it
        win.geometry(f"{ow}x{oh}+{x}+{y}")
        
        cx = x + (ow // 2)
        cy = y + (oh // 2)

        # move mouse cursor to overlay center (Windows)
        ctypes.windll.user32.SetCursorPos(cx, cy)
        self._update_eq_ui_state() # Call new helper to set initial state
        
#####################################################################################################

    def show_search_overlay(self):
        if self.display_radio: return
        
        # Close existing search overlay if open
        self.show_key_hints(force_state=False)  # Close key hints if open
        
        if hasattr(self, 'search_overlay'):
            self.close_search_overlay(self._was_main_overlay_open_before_search)
            if hasattr(self, 'search_overlay'):
                del self.search_overlay  # Clean up the old overlay
            return

        self._was_main_overlay_open_before_search = bool(self.window and self.window.winfo_exists())
        if self._was_main_overlay_open_before_search:
            self.close_overlay()

        # --- Theme ---
        BG_WINDOW = "#23272e"
        BG_FRAME = "#23272e"
        BG_SEARCH = "#2c313a"
        FG_TEXT = "#e0e0e0"
        FG_PLACEHOLDER = "#888"
        ACCENT = "#00ffd5"
        BORDER = "#AAA"
        ENTRY_RADIUS = 18
        OVERLAY_RADIUS = 28
        LIST_RADIUS = 18
        FONT_NORMAL = ("Segoe UI", 13)
        FONT_LIST = ("Segoe UI", 12)
        FONT_CLOSE = ("Segoe UI", 13, "bold")

        # --- Overlay Window ---
        self.search_overlay = tk.Toplevel(self.root)
        self.search_overlay.withdraw()
        self.search_overlay.overrideredirect(True)
        self.search_overlay.configure(bg=BG_WINDOW)
        self.search_overlay.attributes("-topmost", True)

        # --- Rounded Canvas for Overlay Background (fully rounded) ---
        overlay_canvas = RoundedCanvas(self.search_overlay, bg=BG_WINDOW, highlightthickness=0, bd=0)
        overlay_canvas.pack(fill="both", expand=True)
        width, height = 440, 360
        overlay_canvas.create_rounded_box(
            0, 0, width, height, radius=OVERLAY_RADIUS, color=BG_FRAME
        )

        # --- Main Frame (on top of canvas) ---
        main_frame = tk.Frame(self.search_overlay, bg=BG_FRAME)
        main_frame.place(relx=0, rely=0, relwidth=1, relheight=1)

        # --- Shared padding ---
        PAD_X = 28

        # --- Search Bar Frame ---
        search_bar_frame = tk.Frame(main_frame, bg=BG_FRAME)
        search_bar_frame.pack(fill="x", pady=(28, 0), padx=PAD_X)

        # --- Search Entry with Fully Rounded Background ---
        entry_canvas = RoundedCanvas(search_bar_frame, height=36, bg=BG_FRAME, highlightthickness=0, bd=0)
        entry_canvas.pack(fill="x", expand=True, side="left")
        entry_canvas.update_idletasks()
        entry_w = search_bar_frame.winfo_reqwidth() or 320
        entry_h = 36
        entry_canvas.config(width=entry_w, height=entry_h)
        entry_canvas.create_rounded_box(
            0, 0, entry_w, entry_h, radius=ENTRY_RADIUS, color=BG_SEARCH
        )

        # --- Entry Widget (on top of rounded canvas) ---
        search_var = tk.StringVar()
        search_entry = tk.Entry(
            entry_canvas,
            font=FONT_NORMAL,
            bg=BG_SEARCH,
            fg=FG_TEXT,
            insertbackground=FG_TEXT,
            textvariable=search_var,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
        )
        # Place entry inside the rounded rectangle
        search_entry.place(x=14, y=4, relwidth=0.80, height=entry_h-8)

        # --- Close Button (inside entry, right-aligned) ---
        def close_search():
            self.close_search_overlay(self._was_main_overlay_open_before_search)
        close_btn = tk.Label(
            entry_canvas,
            text="✕",
            font=FONT_CLOSE,
            bg=BG_SEARCH,
            fg="#aaa",
            cursor="hand2"
        )
        close_btn.place(relx=0.90, y=6, width=24, height=24)
        def on_close_enter(e): close_btn.config(fg="#fff")
        def on_close_leave(e): close_btn.config(fg="#aaa")
        close_btn.bind("<Button-1>", lambda e: close_search())
        close_btn.bind("<Enter>", on_close_enter)
        close_btn.bind("<Leave>", on_close_leave)

        # --- Padding between search and results ---
        tk.Frame(main_frame, height=18, bg=BG_FRAME).pack(fill="x")

        # --- Results Listbox with Fully Rounded Background ---
        results_frame = tk.Frame(main_frame, bg=BG_FRAME)
        results_frame.pack(fill="both", expand=True, padx=PAD_X, pady=(0, 18))

        # Calculate list_h to fill the remaining space, accounting for paddings and search bar height
        list_h = height - (2 * PAD_X) - entry_h - 18  # PAD_X top, PAD_X bottom, entry_h, padding between search/results, bottom padding
        list_canvas = RoundedCanvas(results_frame, height=list_h, bg=BG_FRAME, highlightthickness=0, bd=0)
        list_canvas.pack(fill="both", expand=True, side="left")
        list_canvas.update_idletasks()
        list_w = results_frame.winfo_reqwidth() or (width - 2*PAD_X)
        list_canvas.config(width=list_w, height=list_h)
        list_canvas.create_rounded_box(
            0, 0, list_w, list_h, radius=LIST_RADIUS, color=BG_SEARCH
        )

        # --- Listbox Widget (on top of rounded canvas) ---
        results_list = tk.Listbox(
            list_canvas,
            font=FONT_LIST,
            bg=BG_SEARCH,
            fg=FG_TEXT,
            selectmode="single",
            height=8,
            activestyle="none",
            selectbackground=ACCENT,
            selectforeground="#23272e",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            highlightbackground=BG_SEARCH,
            highlightcolor=ACCENT,
            bd=0
        )
        # Place listbox inside the rounded rectangle
        results_list.place(x=10, y=8, width=list_w-20, height=list_h-16)

        # --- Results Data ---
        current_results = []

        def update_search(*args):
            search_term = search_var.get().strip()
            results_list.delete(0, tk.END)
            current_results.clear()
            if search_term:
                raw_search_results = self.MusicPlayer.get_search_term(search_term)
                for raw_title, path in raw_search_results:
                    cleaned_title = self.TitleCleaner.clean(raw_title)
                    results_list.insert(tk.END, f"  {cleaned_title}")
                    current_results.append((cleaned_title, path))
            if not current_results and search_term:
                results_list.insert(tk.END, "  No results found.")
                results_list.itemconfig(0, {'fg': FG_PLACEHOLDER})

        def handle_selection(event=None):
            selection = results_list.curselection()
            if selection and current_results:
                index = selection[0]
                if results_list.get(index).strip() == "No results found.":
                    return
                if 0 <= index < len(current_results):
                    _, path = current_results[index]
                    self.MusicPlayer.play_song(path)
                    self.close_search_overlay(self._was_main_overlay_open_before_search)

        def handle_key_in_entry(event):
            if event.keysym == "Escape":
                self.close_search_overlay(self._was_main_overlay_open_before_search)
                return "break"
            elif event.keysym == "Return":
                if results_list.size() > 0:
                    if not results_list.curselection():
                        if results_list.get(0).strip() != "No results found.":
                            results_list.selection_set(0)
                            results_list.activate(0)
                            results_list.see(0)
                handle_selection()
                return "break"
            elif event.keysym == "Down":
                if results_list.size() > 0:
                    current_selection = results_list.curselection()
                    next_index = 0
                    if current_selection:
                        next_index = min(current_selection[0] + 1, results_list.size() - 1)
                    if results_list.get(next_index).strip() == "No results found." and next_index == 0 and results_list.size() == 1:
                        pass
                    else:
                        results_list.selection_clear(0, tk.END)
                        results_list.selection_set(next_index)
                        results_list.activate(next_index)
                        results_list.see(next_index)
                return "break"
            elif event.keysym == "Up":
                if results_list.size() > 0:
                    current_selection = results_list.curselection()
                    prev_index = results_list.size() - 1
                    if current_selection:
                        prev_index = max(current_selection[0] - 1, 0)
                    results_list.selection_clear(0, tk.END)
                    results_list.selection_set(prev_index)
                    results_list.activate(prev_index)
                    results_list.see(prev_index)
                return "break"

        def handle_key_in_listbox(event):
            if event.keysym == "Return":
                handle_selection()
                return "break"
            elif event.keysym == "Escape":
                self.close_search_overlay(self._was_main_overlay_open_before_search)
                return "break"

        # --- Bindings ---
        search_var.trace_add("write", update_search)
        search_entry.bind("<Key>", handle_key_in_entry)
        results_list.bind("<Double-Button-1>", handle_selection)
        results_list.bind("<Return>", handle_key_in_listbox)
        results_list.bind("<Escape>", handle_key_in_listbox)
        self.search_overlay.bind("<Escape>", lambda e: self.close_search_overlay(self._was_main_overlay_open_before_search))

        # --- Mouse wheel scrolling for results ---
        def _on_mousewheel(event):
            results_list.yview_scroll(int(-1*(event.delta/120)), "units")
        results_list.bind("<MouseWheel>", _on_mousewheel)

        # --- Overlay Placement ---
        self.search_overlay.update_idletasks()
        x = (self.search_overlay.winfo_screenwidth() // 2) - (width // 2)
        y = (self.search_overlay.winfo_screenheight() // 2) - (height // 2)
        self.search_overlay.geometry(f"{width}x{height}+{x}+{y}")
        self.search_overlay.deiconify()
        self.search_overlay.grab_set()
        self.search_overlay.lift()
        self.search_overlay.focus_force()
        
        def focus_entry_with_click():
            self.search_overlay.update_idletasks()
            x = search_entry.winfo_rootx() + search_entry.winfo_width() // 2
            y = search_entry.winfo_rooty() + search_entry.winfo_height() // 2
            mouse_controller = mouse.Controller()
            mouse_controller.position = (x, y)
            mouse_controller.press(mouse.Button.left)
            mouse_controller.release(mouse.Button.left)
            search_entry.focus_set()
        
        self.search_overlay.after_idle(focus_entry_with_click)

        # --- Make overlay draggable by clicking anywhere on overlay_canvas or main_frame ---
        def start_move(event):
            self.search_overlay._drag_start_x = event.x_root - self.search_overlay.winfo_x()
            self.search_overlay._drag_start_y = event.y_root - self.search_overlay.winfo_y()
        def do_move(event):
            x = event.x_root - self.search_overlay._drag_start_x
            y = event.y_root - self.search_overlay._drag_start_y
            self.search_overlay.geometry(f"+{x}+{y}")
        overlay_canvas.bind("<Button-1>", start_move)
        overlay_canvas.bind("<B1-Motion>", do_move)
        main_frame.bind("<Button-1>", start_move)
        main_frame.bind("<B1-Motion>", do_move)

        def check_mouse_outside_overlay():
            if self.search_overlay and self.search_overlay.winfo_exists():
                x1 = self.search_overlay.winfo_rootx()
                y1 = self.search_overlay.winfo_rooty()
                x2 = x1 + self.search_overlay.winfo_width()
                y2 = y1 + self.search_overlay.winfo_height()
                margin = 50
                mx = self.search_overlay.winfo_pointerx()
                my = self.search_overlay.winfo_pointery()
                if (mx < x1 - margin or mx > x2 + margin or
                    my < y1 - margin or my > y2 + margin):
                    self.close_search_overlay(self._was_main_overlay_open_before_search)
                else:
                    # Check again after a short delay
                    self.search_overlay.after(100, check_mouse_outside_overlay)

        # Start the polling loop after the overlay is shown
        self.search_overlay.after(100, check_mouse_outside_overlay)

    def close_search_overlay(self, restore_main_overlay=False):
        if hasattr(self, 'search_overlay') and self.search_overlay and self.search_overlay.winfo_exists():
            self.search_overlay.grab_release() # Release grab before destroying
            self.search_overlay.destroy()
            self.search_overlay = None
            if restore_main_overlay:
                self.open_overlay()
            
#####################################################################################################

    def _wireless_trigger_pause(self):
        if not self.display_radio:
            self._trigger_pause()
        else:
            self._trigger_radio_toggle()

    def _trigger_skip_previous(self):
        if hasattr(self, 'MusicPlayer') and self.playerState and not self.display_radio:
            self.MusicPlayer.skip_previous()

    def _trigger_skip_next(self):
        if hasattr(self, 'MusicPlayer') and self.playerState and not self.display_radio:
            self.MusicPlayer.skip_next()

    def _trigger_pause(self):
        if hasattr(self, 'MusicPlayer') and self.playerState and not self.display_radio:
            self.MusicPlayer.pause() # Assuming pause toggles
            
    def _trigger_volume_up(self):
        if hasattr(self, 'MusicPlayer') and not getattr(self, "_eq_window", None) and not self._eq_window.winfo_exists() and self.playerState:
            self.MusicPlayer.up_volume()
            
    def _trigger_volume_dwn(self):
        if hasattr(self, 'MusicPlayer') and not getattr(self, "_eq_window", None) and not self._eq_window.winfo_exists() and self.playerState:
            self.MusicPlayer.dwn_volume()
            
    def _trigger_repeat(self):
        if hasattr(self, 'MusicPlayer') and self.playerState and not self.display_radio:
            self.MusicPlayer.repeat()
            
    def _trigger_lyrics_toggle(self):
        # This toggles the *display* of lyrics if they are available for the current song
        # It does not control fetching or processing, just visibility on the overlay
        self.display_lyrics = not self.display_lyrics
        if self.window and self.running_lyrics: # Only update if lyrics are conceptually "on"
            self.root.after(0, self.update_display)
                
    def _trigger_radio_toggle(self): # Enable/Disable Radio Mode
        if hasattr(self, 'MusicPlayer') and monotonic() - self.triggerDebounce[0] >= self.triggerDebounce[1] and self.playerState:
            self.triggerDebounce[0] = monotonic()
            self.display_radio = not self.display_radio
            # self.MusicPlayer.toggle_loop_cycle(self.display_radio) # Assuming this controls radio mode in player
            ll.debug(f"Radio mode toggled: {'ON' if self.display_radio else 'OFF'}")
            if hasattr(self.MusicPlayer, 'toggle_loop_cycle'):
                 self.MusicPlayer.toggle_loop_cycle(self.display_radio)
            if self.window:
                self.root.after(0, self.update_display) # Update display to reflect radio state if needed
            
    def _trigger_radio_station(self, atmpt = 0, max_loop = 5): # Scan for next station
        if hasattr(self, 'MusicPlayer') and self.display_radio and self.playerState: # Only if radio mode is ON
            if monotonic() - self.triggerDebounce[0] >= self.triggerDebounce[1]: # Debounce scan attempts
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
                    # Always schedule on main thread
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
                self.open_overlay() # Open overlay if not already open
                if hasattr(self, 'MusicPlayer'):
                    self.MusicPlayer.pause(True) # True to unpause/play
            else: # Transitioning to OFF
                ll.print("Player disabled. Closing overlay and pausing music.")
                if hasattr(self, 'MusicPlayer'):
                    if self.display_radio: # If radio was on, turn it off conceptually
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
        """Toggle Whether The Mouse Ignores The Display Or Not
        - True: Disable Mouse Clicking Of The Overlay
        - False: Enable Mouse Clicking Of The Overlay
        """
        hwnd = ctypes.windll.user32.GetParent(self.window.winfo_id())
        current_style = ctypes.windll.user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        
        if mode:
            # Enable click-through
            new_style = current_style | WS_EX_LAYERED | WS_EX_TRANSPARENT
        else:
            # Disable click-through
            new_style = current_style & ~ WS_EX_TRANSPARENT
        
        ctypes.windll.user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, new_style)

    def parse_mouse_over_overlay(self):
        if not self.window or not self.window.winfo_exists():
            return
        if self.mouseEvents.is_right_mouse_down():
            ## Get current mouse position
            ## Get window geometry (x, y, width, height)
            ##  Calculate rectangle bounds:
            ##  a = top-left (window_x, window_y)
            ##  b = bottom-right (window_x + width, window_y + height)
            #a_x, a_y = window_x, window_y
            #b_x, b_y = window_x + window_width, window_y + window_height
            ## Check if mouse is inside window
            #if self.calc_pos(*currentPosition, a_x, a_y, b_x, b_y):
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
                    hwnd = ctypes.windll.user32.GetParent(self.window.winfo_id()) # Get the actual window handle
                    # Try to bring the window to the foreground and activate it
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
                    ctypes.windll.user32.SetActiveWindow(hwnd)
                    ctypes.windll.user32.SetFocus(hwnd) # Explicitly set focus
                    ctypes.windll.user32.BringWindowToTop(hwnd) # Ensure it's on top of other windows
                    
                    # Just incase that didn't work force right click and left click twice to simulate the window to foreground over context windows
                    mouse_controller = mouse.Controller()
                    mouse_controller.press(mouse.Button.right)
                    mouse_controller.release(mouse.Button.right)
                    mouse_controller.press(mouse.Button.left)
                    mouse_controller.release(mouse.Button.left)
                    mouse_controller.press(mouse.Button.right)
        else:
            if not self.clickThroughState and not self._overlay_dragging:
                self.clickThroughState = True
                self.toggle_overlay_clickthrough(self.clickThroughState)
                try:
                    self.root.after(100, self.keep_overlay_on_top)
                except:
                    ll.error("Couldn't Load Root After.")
                    
    def handle_overlay_background_process(self, time_dilation_for_key_reset: float = 300):  # Increased from 60
        """Loop To Handle Draggability - OPTIMIZED FOR GAMING"""
        thread_tick_size = 0.25  # Increased from 0.1 (4x less frequent)
        ticks_per_second = int(1 / thread_tick_size) 
        time_dial = int(time_dilation_for_key_reset * ticks_per_second)
        time_tick = 0
        
        while True:
            try:
                # Only check mouse when actually needed
                if self.window and self.window.winfo_exists():
                    self.parse_mouse_over_overlay()
            except Exception as E:
                ll.error(f"Cannot Toggle Mouse-Over Overlay: {E}")
            
            time_tick = (time_tick + 1) % time_dial 
            if time_tick == 0:
                ll.debug(f"Resetting Key Events")
                self.background_key_reset()
            
            # Less frequent topmost updates
            if time_tick % (ticks_per_second * 5) == 0:  # Every 5 seconds instead of every second
                self.keep_overlay_on_top()
            
            sleep(thread_tick_size)

#####################################################################################################

    def toggle_lyrics(self, state: bool): # Master toggle for lyrics processing/fetching (conceptual)
        if self.running_lyrics == state: return # No change

        with self.text_lock:
            self.running_lyrics = state
            if not state: # If turning lyrics off
                self.player_metric['player_lyrics'] = "" # Clear current lyrics
        
        # This method controls the 'conceptual' state of lyrics being active.
        # The display_lyrics flag (toggled by user via hotkey) controls visibility.
        # Update display if overlay is open, to show/hide the lyrics section.
        if self.window and self.window.winfo_exists():
            self.root.after(0, self.update_display)

    def wrap_text(self, text: str, max_chars_line: int = 30) -> str:
        if not text: return ""
        words = text.split()
        if not words: return ""

        lines = []
        current_line = ""
        for word in words:
            if not current_line:
                current_line = word
            elif len(current_line) + 1 + len(word) <= max_chars_line:
                current_line += " " + word
            else:
                lines.append(current_line)
                current_line = word
        if current_line:
            lines.append(current_line)
        
        if len(lines) > 2 : # If more than 2 lines after basic wrap, try to force to 2
            # A simple split, might not be ideal for all cases
            midpoint_char = len(text) // 2
            split_point = text.rfind(' ', 0, midpoint_char + len(text)//4) # Look for space around middle
            if split_point == -1: # No space found, hard split
                 split_point = midpoint_char
            
            line1 = text[:split_point].strip()
            line2 = text[split_point:].strip()
            return f"{line1}\n{line2}" if line2 else line1 # Avoid trailing newline if line2 is empty
        
        return "\n".join(lines)
    
#####################################################################################################

    def display_overlay(self):
        while not hasattr(self, 'MusicPlayer'):
            sleep(1)
        self.root.after(0, self.open_overlay)

    def keep_overlay_on_top_init(self):
        self.root.bind("<FocusIn>", self.keep_overlay_on_top)
        self.root.bind("<FocusOut>", self.keep_overlay_on_top)
        #self.root.after(1, self.keep_overlay_on_top_loop)  # schedule again after 1 ms

    def keep_overlay_on_top(self, event = None):
        """Keep the overlay window on top of all other windows."""
        try:
            if self.window and self.window.winfo_exists() and self.window.state() != 'withdrawn':
                # Only set topmost if it's not already topmost
                if not self.window.attributes('-topmost'):
                    self.window.attributes('-topmost', True)
            
            # Skip other windows if they don't exist
            if hasattr(self, 'key_hints_popup') and self.key_hints_popup and self.key_hints_popup.winfo_exists():
                if not self.key_hints_popup.attributes('-topmost'):
                    self.key_hints_popup.attributes('-topmost', True)
        except tk.TclError:
            pass  # Window destroyed, ignore

#####################################################################################################

    def center_window(self):
        if not (self.window and self.window.winfo_exists()):
            return
        self.window.update_idletasks() # Ensure dimensions are calculated
        width = self.window.winfo_width()
        height = self.window.winfo_height()
        # If width/height are still 1, it means it hasn't drawn yet. Try again.
        if width <= 1 or height <= 1: 
            self.root.after(100, self.center_window)
            return
        
        screen_width = self.window.winfo_screenwidth()
        # screen_height = self.window.winfo_screenheight() # Not used for y
        x = (screen_width - width) // 2
        self.window.geometry(f'+{x}+20') # Default y=20 from top
        self._last_position = (x, 20)

    def _create_canvas_items_if_needed(self, init_draw = False):
        if self.canvas_items.get('bg') is None or init_draw == True:
            # Create items and store their IDs
            
            self.canvas_items['bg'] = self.canvas.create_rounded_box(0, 0, 1, 1, radius=15, color=self.bg_color)
            self.canvas_items['player_text'] = self.canvas.create_text(0, 0, font=self.main_font, fill='#FFFFFF', anchor=tk.N, justify=tk.CENTER)
            self.canvas_items['duration_text'] = self.canvas.create_text(0, 0, font=self.time_font, fill='#AAAAAA', anchor=tk.N, justify=tk.CENTER)
            self.canvas_items['lyrics_text'] = self.canvas.create_text(0, 0, font=self.lyrics_font, fill='#E0E0E0', anchor=tk.N, justify=tk.CENTER)

    def open_overlay(self):
        if hasattr(self, 'search_overlay'):
            self.close_search_overlay(self._was_main_overlay_open_before_search)
            try:
                del self.search_overlay
            except AttributeError:
                pass
        if self.window and self.window.winfo_exists(): # Already open
            self.window.lift()
            return

        self.window = tk.Toplevel(self.root)
        self.window.overrideredirect(True)
        self.window.attributes('-alpha', 0.7)
        self.window.attributes('-topmost', True)
        # Using a common color that's unlikely to be in content for transparency
        # 'gray1' is very dark, almost black. Ensure it's not your background.
        transparent_color = 'gray1' # Or another unique color
        self.window.attributes('-transparentcolor', transparent_color) 
        self.window.config(bg=transparent_color) # Set window bg to transparent color
        self.toggle_overlay_clickthrough(self.clickThroughState)

        self.canvas = RoundedCanvas(self.window, bg=transparent_color, highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        
        self.update_display(init_draw=True) # Initial draw
        
        if self._last_position:
            x, y = self._last_position
            self.window.geometry(f"+{x}+{y}")
        else:
            self.root.after(0, self.center_window) # Delay centering slightly for dimensions to finalize

        # Drag-to-move handlers
        self._drag_start_x = 0
        self._drag_start_y = 0
        self._win_start_x = 0
        self._win_start_y = 0
        self._overlay_dragging = False
        self._overlay_start_move = None

        def start_move(event):
            self._drag_start_x = event.x_root
            self._drag_start_y = event.y_root
            if self.window: # Ensure window exists
                self._overlay_dragging = True
                self._win_start_x = self.window.winfo_x()
                self._win_start_y = self.window.winfo_y()
                self._win_start_size_width = self.window.winfo_width()
                self._win_start_size_height = self.window.winfo_height()

        def do_move(event):
            if self.window: # Ensure window exists
                dx = event.x_root - self._drag_start_x
                dy = event.y_root - self._drag_start_y
                new_x = self._win_start_x + dx
                new_y = self._win_start_y + dy
                self.window.geometry(f"+{new_x}+{new_y}")
                self._last_position = (new_x, new_y)
                self.mouseEvents.update_window(new_x, new_y, self._win_start_size_width, self._win_start_size_height)

        def do_stop(event):
            self._overlay_dragging = False

        self._overlay_start_move = start_move

        self.canvas.bind("<Button-3>", start_move)
        self.canvas.bind("<B3-Motion>", do_move)
        self.canvas.bind("<ButtonRelease-3>", do_stop)

    def close_overlay(self):
        if self.window:
            try:
                current_pos_str = self.window.geometry().split('+')
                if len(current_pos_str) == 3: # e.g., "WxH+X+Y"
                    self._last_position = (int(current_pos_str[1]), int(current_pos_str[2]))
            except (tk.TclError, AttributeError, ValueError): # Window might be already destroyed or malformed string
                 pass # self._last_position remains as is
            self.window.destroy()
            self.window = None
            self.canvas = None # Clear canvas reference too
            self.clickThroughState = True # Update clickThroughState
            
    def update_display(self, init_draw = False):
        if not (self.window and self.canvas and self.window.winfo_exists()):
            return

        try:
            # Ensure all canvas items have been created
            self._create_canvas_items_if_needed(init_draw)

            # Wrap main text (song title/artist)
            wrapped_player_text = self.wrap_text(self.player_metric['player_text'], 35)
            num_player_text_lines = wrapped_player_text.count('\n') + 1

            display_lyrics_text = ""
            lyrics_visible = self.running_lyrics and self.display_lyrics and self.player_metric['player_lyrics']
            if lyrics_visible:
                wrapped_lyrics = self.wrap_text(self.player_metric['player_lyrics'], 40)
                display_lyrics_text = wrapped_lyrics

            main_width = max(self.main_font.measure(line) for line in wrapped_player_text.split('\n')) if wrapped_player_text else 0
            time_width = self.time_font.measure(self.player_metric['player_duration'])
            lyrics_width = max(self.lyrics_font.measure(line) for line in display_lyrics_text.split('\n')) if display_lyrics_text else 0

            total_width = max(main_width, time_width, lyrics_width) + 2 * self.overlay_text_padding
            
            height_for_main_text = self.main_font.metrics("linespace") * num_player_text_lines
            height_for_time = self.time_font.metrics("linespace")
            num_lyrics_lines = display_lyrics_text.count('\n') + 1 if lyrics_visible else 0
            height_for_lyrics = (self.lyrics_font.metrics("linespace") * num_lyrics_lines) + (self.overlay_text_padding / 2) if lyrics_visible else 0
            total_height = height_for_main_text + height_for_time + height_for_lyrics + (2 * self.overlay_text_padding)

            ### Background ###
            self.canvas.delete(self.canvas_items['bg'])
            self.canvas_items['bg'] = self.canvas.create_rounded_box(0, 0, total_width, total_height, radius=self.overlay_corner_radius, color=self.bg_color)
            self.canvas.tag_lower(self.canvas_items['bg']) # Ensure it's the bottom layer
            current_y = self.overlay_text_padding

            ### Player Label ###
            self.canvas.itemconfig(self.canvas_items['player_text'], text=wrapped_player_text)
            self.canvas.coords(self.canvas_items['player_text'], total_width / 2, current_y)
            current_y += height_for_main_text + (self.overlay_text_padding / 2)
            
            ### Player Lyrics ###
            self.canvas.itemconfig(self.canvas_items['duration_text'], text=self.player_metric['player_duration'])
            self.canvas.coords(self.canvas_items['duration_text'], total_width / 2, current_y)
            current_y += height_for_time + (self.overlay_text_padding / 2 if lyrics_visible else 0)

            if lyrics_visible:
                self.canvas.itemconfig(self.canvas_items['lyrics_text'], text=display_lyrics_text, state='normal')
                self.canvas.coords(self.canvas_items['lyrics_text'], total_width / 2, current_y)
            else:
                self.canvas.itemconfig(self.canvas_items['lyrics_text'], state='hidden')

            # Resize window, preserving position
            if self.window and self.window.winfo_exists():
                current_geometry = self.window.geometry() # "WxH+X+Y"
                parts = current_geometry.split('+')
                if len(parts) == 3: # If X and Y are available
                    x_pos, y_pos = parts[1], parts[2]
                    self.window.geometry(f'{int(total_width)}x{int(total_height)}+{x_pos}+{y_pos}')
                else: # Fallback if geometry string is unexpected
                    self.window.geometry(f'{int(total_width)}x{int(total_height)}')
                    
            self._update_scheduled = False # Reset the flag

        except tk.TclError as e:
            pass # Window or canvas likely destroyed
        except Exception as e:
            ll.error(f"Unexpected error in update_display: {e}")

#####################################################################################################

    def schedule_update(self):
        if self.window and not self._update_scheduled:
            self._update_scheduled = True
            self.root.after(100, self.update_display) # 100ms delay to batch updates
            
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
        
        current_str = format_time(current_seconds)
        total_str = format_time(total_seconds)
        full_str = f"{current_str} / {total_str}"
        
        with self.text_lock:
            # Only update if seconds changed (not every millisecond)
            if self.player_metric['player_duration'] == full_str:
                return
            self.player_metric['player_duration'] = full_str
        self.schedule_update()

    def set_lyrics(self, text: str):
        # This sets the raw lyric string. Wrapping happens in update_display.
        with self.text_lock:
            self.player_metric['player_lyrics'] = text if text else "" # Ensure it's a string
        self.schedule_update()

#####################################################################################################

    def set_radio_ips(self, ip_list: list):
        with self.text_lock:
            self.radio_metric['availability'] = list(set(ip_list)) # Ensure unique IPs
            if not self.radio_metric['availability'] or self.radio_metric['current_ip'] not in self.radio_metric['availability']:
                self.radio_metric['current_ip'] = self.radio_metric['availability'][0] if self.radio_metric['availability'] else ''
        if self.window and self.window.winfo_exists() and self.display_radio: # Update if radio is visible
             self.root.after(0, self.update_display) # To reflect new IP or availability if shown
        
    def _get_next_(self, items: list, value):
        if not items: return ''
        try:
            current_index = items.index(value)
            return items[(current_index + 1) % len(items)]
        except ValueError: # Value not in list, return first item
            return items[0]
    
    def set_radio_channel(self): # Skips to next available IP
        with self.text_lock:
            ip_list = self.radio_metric['availability']
            if len(ip_list) > 0: # Only change if there are options
                current_ip = self.radio_metric['current_ip']
                self.radio_metric['current_ip'] = self._get_next_(ip_list, current_ip)
                ll.print(f"Radio IP set to: {self.radio_metric['current_ip']}")
            else:
                self.radio_metric['current_ip'] = '' # No IPs available
                ll.warn("No radio IPs available to select.")
        # No automatic display update here, _trigger_radio_station will handle it after attempting connection.
        
    def close_application(self):
        """Properly close the entire application"""
        self.root.destroy()   # Destroy GUI first
        os._exit(0)          # Force exit all threads/processes

#####################################################################################################

def main():
    root = tk.Tk()
    root.withdraw() # Hide the main Tk window
    
    # Dummy MusicPlayer class for testing GhostOverlay standalone
    class DummyMusicPlayer:
        def __init__(self, overlay_ref):
            self.overlay = overlay_ref
            self.is_paused = True
            self.volume = 50
            self.is_repeating = False
            self.current_song_index = 0
            self.playlist = [
                ("Song A: The Phantom's Ballad", "Artist X", 185.0, "Line 1 of song A lyrics here\nLine 2 of song A, a bit longer perhaps"),
                ("Track B: Spooky Beats", "DJ Ghost", 220.0, "Just some spooky beats, no words to see\nRepeating in your head, can't break free"),
                ("Symphony of the Nightshade", "Nocturne Orchestra", 300.0, None), # No lyrics
                ("Whispers in the Static", "The Glitch Mobsters", 150.0, "Short one line lyric."),
            ]
            self.radio_ips = ["192.168.1.100", "10.0.0.5", "127.0.0.1"]
            self.overlay.set_radio_ips(self.radio_ips) # Initialize radio IPs
            self.overlay.toggle_lyrics(True) # Enable lyrics processing by default

        def _update_overlay_song_info(self):
            title, artist, duration, lyrics = self.playlist[self.current_song_index]
            self.overlay.set_text(f"{title} - {artist}")
            self.overlay.set_duration(0, duration) # Reset current time for new song
            self.overlay.set_lyrics(lyrics if lyrics else "")

        def skip_previous(self):
            ll.debug("DummyPlayer: Skip Previous")
            self.current_song_index = (self.current_song_index - 1 + len(self.playlist)) % len(self.playlist)
            self._update_overlay_song_info()
        def skip_next(self):
            ll.debug("DummyPlayer: Skip Next")
            self.current_song_index = (self.current_song_index + 1) % len(self.playlist)
            self._update_overlay_song_info()
        def pause(self, force_play=None): # True to play, False to pause, None to toggle
            if force_play is True: self.is_paused = False
            elif force_play is False: self.is_paused = True
            else: self.is_paused = not self.is_paused
            ll.debug(f"DummyPlayer: {'Playing' if not self.is_paused else 'Paused'}")
        def up_volume(self): self.volume = min(100, self.volume + 5); ll.debug(f"DummyPlayer: Vol {self.volume}")
        def dwn_volume(self): self.volume = max(0, self.volume - 5); ll.debug(f"DummyPlayer: Vol {self.volume}")
        def repeat(self): self.is_repeating = not self.is_repeating; ll.debug(f"DummyPlayer: Repeat {'On' if self.is_repeating else 'Off'}")
        def toggle_loop_cycle(self, radio_mode_on: bool): # For radio
            ll.debug(f"DummyPlayer: Radio mode {'Activated' if radio_mode_on else 'Deactivated'}")
        def set_radio_ip(self, ip:str):
            if ip: ll.debug(f"DummyPlayer: Attempting to stream from radio IP: {ip}"); return True # Simulate success
            ll.warn("DummyPlayer: No radio IP to stream from.")
            return False


    overlay = GhostOverlay(root)
    
    # --- For testing: Link a dummy player ---
    # This part would normally be handled by your main application logic
    # which creates both the music player and the overlay.
    player = DummyMusicPlayer(overlay)
    overlay.MusicPlayer = player # Allow overlay to call player methods
    player._update_overlay_song_info() # Set initial song

    # Simulate song progress for testing duration update
    current_pos = 0
    total_duration = player.playlist[player.current_song_index][2]
    def simulate_progress():
        nonlocal current_pos, total_duration
        if not player.is_paused and overlay.playerState: # only if playing and player is on
            current_pos +=1
            if current_pos > total_duration:
                current_pos = 0 # loop song for test
                # Or call player.skip_next() for playlist behavior
            overlay.set_duration(current_pos, total_duration)
        
        # Update total_duration if song changed
        new_total_duration = player.playlist[player.current_song_index][2]
        if new_total_duration != total_duration:
            total_duration = new_total_duration
            current_pos = 0 # Reset progress for new song

        root.after(1000, simulate_progress)
    
    root.after(1000, simulate_progress)
    # --- End For testing ---

    try:
        root.mainloop()
    except KeyboardInterrupt:
        ll.warn("Exiting GhostOverlay...")
    finally:
        if overlay.listener and overlay.listener.is_alive():
            overlay.listener.stop()
        # Consider if kill_all_python_processes is desired on normal exit
        # kill_all_python_processes(include_current=False) # Example cleanup


if __name__ == "__main__":
    main()