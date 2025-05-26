import ctypes, psutil, os
import tkinter as tk
from tkinter import font, messagebox # Added messagebox
from threading import Lock, RLock # Added RLock for SettingsHandler
from pynput import keyboard
from time import monotonic
import json # Added json

# Radio Direct Link
try:
    from radioIpScanner import SimpleRadioScan
except ImportError: # More specific exception
    from .radioIpScanner import SimpleRadioScan

# Windows API constants
WS_EX_LAYERED = 0x00080000
# WS_EX_TRANSPARENT = 0x00000020 # Not currently used after change
GWL_EXSTYLE = -20

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
                print(f"Error saving settings: {e}")


    def reset_settings(self):
        with self._lock:
            self._settings = {}
            self._save()

def kill_all_python_processes(include_current: bool = True):
    my_pid = os.getpid()
    for proc in psutil.process_iter(['pid', 'name', 'exe', 'cmdline']):
        pid = proc.info['pid']
        if pid == my_pid and not include_current:
            continue
        name = (proc.info['name'] or "").lower()
        exe  = (os.path.basename(proc.info.get('exe') or "")).lower()
        cmd  = " ".join(proc.info.get('cmdline') or []).lower()
        if any(keyword in name for keyword in ('python',)) \
           or any(keyword in exe  for keyword in ('python',)) \
           or cmd.startswith('python'):
            try:
                proc.terminate()
                proc.wait(timeout=1) # Reduced timeout for faster attempts
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except psutil.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=1) # Wait after kill as well
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
                
def create_rounded_rectangle(self, x1, y1, x2, y2, radius=25, **kwargs):
    points = []
    points.extend([x1 + radius, y1, x2 - radius, y1])
    points.extend([x2 - radius/2, y1, x2, y1, x2, y1 + radius/2])
    points.extend([x2, y1 + radius, x2, y2 - radius])
    points.extend([x2, y2 - radius/2, x2, y2, x2 - radius/2, y2])
    points.extend([x2 - radius, y2, x1 + radius, y2])
    points.extend([x1 + radius/2, y2, x1, y2, x1, y2 - radius/2])
    points.extend([x1, y2 - radius, x1, y1 + radius])
    points.extend([x1, y1 + radius/2, x1, y1, x1 + radius/2, y1])
    return self.create_polygon(*points, smooth=True, **kwargs)

tk.Canvas.create_rounded_rectangle = create_rounded_rectangle

class GhostOverlay:
    def __init__(self, root):
        self.root = root
        self.window = None
        self.canvas = None
        self._last_position = None
        self.key_hints_popup = None
        self.key_hints_list_frame = None # For updating status
        self.modification_status_label = None # For "Listening..." message

        self.triggerDebounce = [0, 1.0] # Reduced debounce for faster UI response
        self.text_lock = Lock()
        self.display_lyrics = True
        self.running_lyrics = False
        self.display_radio = False
        self.player_metric = {'player_text':'','player_duration':'', 'player_lyrics':''}
        self.radio_metric = {'current_ip':'0.0.0.0', 'availability':[]}
        self.bg_color = '#000000'
        self.corner_radius = 15
        self.padding = 15
        self.last_toggle_state = False
        self.readyForKeys = False
        self.playerState = True

        self.is_listening_for_modification = False
        self.action_id_being_modified = None

        self.bindings_handler = SettingsHandler(filename=".keyBindings.json")
        self._define_default_key_actions()
        self._load_custom_bindings()
        self._rebuild_key_maps() # Initial build
        
        self.VK_CODE = {'alt': 0x12}
            
        self.listener = keyboard.Listener(
            on_press=self._handle_key_press,
            on_release=self._handle_key_release
        )
        self.listener.start()
        self.check_keyboard()
        self.readyForKeys = True

    def _define_default_key_actions(self):
        self.key_actions = [
            {
                'id': 'toggle_overlay',
                'required': ['alt', 'shift'],
                'forbidden': ['ALL'],
                'action': self.toggle_overlay,
                'hint': "Show / Hide Music Player",
                'modifiable': False # Core function, specific modifiers
            },
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
                'id': 'show_hints',
                'required': ['alt', 'right alt'],
                'action': self.show_key_hints,
                'hint': "Show This Controls Window", # Clarified hint
                'modifiable': False # Specific, important for help
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
                'id': 'player_on_off',
                'required': ['right alt', 'right shift'],
                'action': self.toggle_player,
                'hint': "Turn Music Player On / Off", # Clarified hint
                'modifiable': False # Specific, important
            },
            {
                'id': 'kill_all_python',
                'required': ['right alt', 'shift'], # Kept as per original
                'action': lambda: kill_all_python_processes(include_current=False), # Ensure current isn't killed if not intended
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
                    print(f"Warning: Invalid custom binding for {action_id} in settings file. Using default.")


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


    def check_keyboard(self):
        # This can be intensive if called too frequently without need.
        # _sync_key_states is called within _check_toggle, which is event-driven.
        # If this is for another purpose, ensure it's necessary.
        # For now, pynput handles events, and GetAsyncKeyState syncs onPress.
        self.root.after(100, self.check_keyboard) # Original interval
        
    def _handle_key_press(self, key):
        if not self.readyForKeys:
            return

        name = self._normalize_key(key)
        if not name: return # Unrecognized key

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

        if name in self.keys_pressed: # This check might be redundant if _sync_key_states is robust
            self.keys_pressed[name] = True
            self._check_toggle()

    def _handle_key_release(self, key):
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


    def _sync_key_states(self):
        # This method might be less reliable than pynput's own state tracking
        # for cross-platform consistency, especially for modifiers.
        # pynput's on_press/on_release should be the primary source of truth for keys_pressed.
        # However, GetAsyncKeyState is Windows-specific and can be useful for an immediate check.
        # For now, relying on pynput's state maintained by on_press/on_release for keys_pressed.
        # If GetAsyncKeyState is strictly needed for 'alt':
        if 'alt' in self.VK_CODE: # Check if alt is a key we monitor this way
            try:
                if ctypes.windll.user32.GetAsyncKeyState(self.VK_CODE['alt']) & 0x8000:
                    self.keys_pressed['alt'] = True
                else:
                    self.keys_pressed['alt'] = False
            except Exception: # ctypes might not be available or fail
                pass


    def _check_toggle(self):
        if self.is_listening_for_modification:
            return

        #self._sync_key_states() # Sync 'alt' state just before checking, if using GetAsyncKeyState

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
                    action_func()
                    self.last_toggle_state = True # Prevent immediate re-trigger
                    # Optional: More selective reset of keys_pressed if needed
                    # For example, keep 'alt' pressed but clear the action-specific key:
                    # for k_to_clear in action['required']:
                    #    if k_to_clear != 'alt': self.keys_pressed[k_to_clear] = False
                break

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

        self.action_id_being_modified = None # Ensure reset


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


    def show_key_hints(self):
        if not self.playerState:
            return
        
        if self.key_hints_popup:
            self.key_hints_popup.destroy()
            self.key_hints_popup = None
        
        self.key_hints_popup = tk.Toplevel(self.root) # Make it child of root
        self.key_hints_popup.overrideredirect(True)
        self.key_hints_popup.configure(bg="#1e1e1e")
        self.key_hints_popup.attributes("-topmost", True)

        main_frame = tk.Frame(self.key_hints_popup, bg="#2e2e2e", bd=2, relief="ridge")
        main_frame.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        self.key_hints_popup.grid_rowconfigure(0, weight=1)
        self.key_hints_popup.grid_columnconfigure(0, weight=1)

        tk.Label(main_frame, text="✨ Music Player Controls ✨", font=("Helvetica", 18, "bold"), bg="#2e2e2e", fg="#00ffd5") \
            .grid(row=0, column=0, columnspan=2, pady=(5,10)) # columnspan for title

        separator = tk.Frame(main_frame, height=2, bg="#444444")
        separator.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0,10))

        self.key_hints_list_frame = tk.Frame(main_frame, bg="#2e2e2e") # Store for status label
        self.key_hints_list_frame.grid(row=2, column=0, columnspan=2, sticky="nsew")
        main_frame.grid_rowconfigure(2, weight=1)

        for i, action in enumerate(self.key_actions):
            keys_display = " + ".join(k.upper() for k in action['required'])
            hint_text = action['hint']
            
            action_row_frame = tk.Frame(self.key_hints_list_frame, bg="#2e2e2e")
            action_row_frame.grid(row=i, column=0, sticky="ew", pady=1)

            label_text = f"{keys_display:<20} →  {hint_text}" # Pad keys for alignment
            tk.Label(action_row_frame, text=label_text, font=("Consolas", 11), bg="#2e2e2e", fg="#ffffff", anchor="w", justify=tk.LEFT) \
                .pack(side=tk.LEFT, padx=10, fill=tk.X, expand=True)

            if action.get('modifiable'):
                modify_button = tk.Button(
                    action_row_frame, text="⚙️", font=("Arial Unicode MS", 10), # Gear emoji
                    bg="#555555", fg="#ffffff", activebackground="#777777", relief="flat",
                    command=lambda act_id=action['id']: self.initiate_key_modification(act_id)
                )
                modify_button.pack(side=tk.RIGHT, padx=(0,10))

        # Status Label for modification instructions
        self.modification_status_label = tk.Label(
            self.key_hints_list_frame, text="", font=("Helvetica", 10, "italic"),
            fg="yellow", bg="#2e2e2e", anchor="w", justify=tk.LEFT, wraplength=380 # Adjust wraplength
        )
        self.modification_status_label.grid(row=len(self.key_actions), column=0, sticky="ew", pady=(10,5), padx=10)


        # Buttons Frame
        buttons_frame = tk.Frame(main_frame, bg="#2e2e2e")
        buttons_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10,0))
        buttons_frame.columnconfigure(0, weight=1) # Make buttons expand
        buttons_frame.columnconfigure(1, weight=1)


        reset_btn = tk.Button(
            buttons_frame, text="Reset Bindings", command=self._confirm_reset_bindings,
            font=("Helvetica", 12), bg="#ffae42", fg="#000000", # Orange
            activebackground="#ff8c00", activeforeground="#000000", relief="raised", bd=2, padx=10, pady=5
        )
        reset_btn.grid(row=0, column=0, sticky="ew", padx=5, pady=(5,0))
        
        close_btn = tk.Button(
            buttons_frame, text="✖ Close", command=self.key_hints_popup.destroy,
            font=("Helvetica", 14, "bold"), bg="#ff4d4d", fg="#ffffff",
            activebackground="#ff1a1a", activeforeground="#ffffff", relief="raised", bd=2, padx=10, pady=5
        )
        close_btn.grid(row=0, column=1, sticky="ew", padx=5, pady=(5,0))


        self.key_hints_popup.bind("<Escape>", lambda e: self.key_hints_popup.destroy())
        
        # Drag-to-move (bind to main_frame and title for better coverage)
        def start_move(e): self.key_hints_popup._x, self.key_hints_popup._y = e.x, e.y
        def do_move(e): self.key_hints_popup.geometry(f"+{e.x_root - self.key_hints_popup._x}+{e.y_root - self.key_hints_popup._y}")
        
        main_frame.bind("<Button-1>", start_move)
        main_frame.bind("<B1-Motion>", do_move)
        # Also bind title label if it's prominent
        # title_label.bind("<Button-1>", start_move)
        # title_label.bind("<B1-Motion>", do_move)


        # Center and lift
        self.key_hints_popup.update_idletasks()
        popup_width = self.key_hints_popup.winfo_width()
        popup_height = self.key_hints_popup.winfo_height()
        screen_width = self.key_hints_popup.winfo_screenwidth()
        screen_height = self.key_hints_popup.winfo_screenheight()
        x_coord = (screen_width // 2) - (popup_width // 2)
        y_coord = (screen_height // 2) - (popup_height // 2)
        self.key_hints_popup.geometry(f"{popup_width}x{popup_height}+{x_coord}+{y_coord}")

        self.key_hints_popup.lift()
        # self.key_hints_popup.after_idle(self.key_hints_popup.attributes, "-topmost", False) # Reconsider this if it causes issues

    # --- (Rest of your GhostOverlay methods: _trigger_*, toggle_overlay, toggle_player, open_overlay, etc.) ---
    # --- Make sure they are not altered unless necessary for the key binding changes ---

    def _trigger_skip_previous(self):
        if hasattr(self, 'MusicPlayer') and self.playerState:
            self.MusicPlayer.skip_previous()

    def _trigger_skip_next(self):
        if hasattr(self, 'MusicPlayer') and self.playerState:
            self.MusicPlayer.skip_next()

    def _trigger_pause(self):
        if hasattr(self, 'MusicPlayer') and self.playerState:
            self.MusicPlayer.pause() # Assuming pause toggles
            
    def _trigger_volume_up(self):
        if hasattr(self, 'MusicPlayer') and self.playerState:
            self.MusicPlayer.up_volume()
            
    def _trigger_volume_dwn(self):
        if hasattr(self, 'MusicPlayer') and self.playerState:
            self.MusicPlayer.dwn_volume()
            
    def _trigger_repeat(self):
        if hasattr(self, 'MusicPlayer') and self.playerState:
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
            print(f"Radio mode toggled: {'ON' if self.display_radio else 'OFF'}")
            if hasattr(self.MusicPlayer, 'toggle_loop_cycle'):
                 self.MusicPlayer.toggle_loop_cycle(self.display_radio)
            if self.window:
                self.root.after(0, self.update_display) # Update display to reflect radio state if needed
            
    def _trigger_radio_station(self, atmpt = 0, max_loop = 5): # Scan for next station
        if hasattr(self, 'MusicPlayer') and self.display_radio and self.playerState: # Only if radio mode is ON
            if monotonic() - self.triggerDebounce[0] >= self.triggerDebounce[1]: # Debounce scan attempts
                self.triggerDebounce[0] = monotonic()
                print("Scanning for radio station...")
                # The logic for set_radio_channel and MusicPlayer.set_radio_ip should handle actual scanning
                # This is just the trigger.
                # Example:
                # self.set_radio_channel() # Gets next IP from available list
                # success = self.MusicPlayer.set_radio_ip(self.radio_metric['current_ip'])
                # if not success and atmpt < max_loop:
                #     self.root.after(100, lambda: self._trigger_radio_station(atmpt + 1, max_loop)) # Retry after delay
                # if self.window:
                #     self.root.after(0, self.update_display)
                # For now, direct call as in original:
                self.set_radio_channel()
                if hasattr(self.MusicPlayer, 'set_radio_ip'):
                    if not self.MusicPlayer.set_radio_ip(self.radio_metric['current_ip']):
                        if atmpt < max_loop : self._trigger_radio_station(atmpt + 1) 
                if self.window:
                    self.root.after(0, self.update_display)
            else:
                print("Radio scan debounce: please wait.")


    def toggle_overlay(self):
        if self.playerState: # Only if player is generally active
            if self.window and self.window.winfo_exists():
                self.close_overlay()
            else:
                # Check if MusicPlayer is initialized before trying to use it
                if not hasattr(self, 'MusicPlayer'):
                    print("MusicPlayer not initialized yet. Cannot open overlay properly.")
                    # Potentially initialize or wait for MusicPlayer here if it's late-loaded
                    # For now, just preventing error:
                    # return 
                try:
                    self.open_overlay()
                except tk.TclError as e: # Catch potential errors if root window is not ready
                    print(f"Could not open overlay yet (TclError): {e}")
                    self.root.after(1000, self.toggle_overlay) # Retry after a delay
                except Exception as e:
                    print(f"An unexpected error occurred trying to open overlay: {e}")


    def toggle_player(self): # Turns the whole media player functionality On/Off
        if monotonic() - self.triggerDebounce[0] >= self.triggerDebounce[1]:
            self.triggerDebounce[0] = monotonic()
            self.playerState = not self.playerState
            
            if self.playerState: # Transitioning to ON
                print("Player enabled. Opening overlay.")
                self.open_overlay() # Open overlay if not already open
                if hasattr(self, 'MusicPlayer'):
                    self.MusicPlayer.pause(True) # True to unpause/play
            else: # Transitioning to OFF
                print("Player disabled. Closing overlay and pausing music.")
                if hasattr(self, 'MusicPlayer'):
                    if self.display_radio: # If radio was on, turn it off conceptually
                        self.display_radio = False
                        self.MusicPlayer.toggle_loop_cycle(self.display_radio)
                    self.MusicPlayer.pause(False) # False to pause
                self.close_overlay()


    def open_overlay(self):
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

        # Layered window for click-through (if desired and working correctly)
        # Be cautious with WS_EX_TRANSPARENT as it makes the window non-interactive.
        # If you need interaction (like dragging), WS_EX_LAYERED is for alpha, not click-through.
        # The dragging is handled by Tkinter binds, so WS_EX_TRANSPARENT should not be set on the main window.
        # The current setup uses -transparentcolor which should allow interaction with non-transparent parts.
        
        # hwnd = ctypes.windll.user32.GetParent(self.window.winfo_id())
        # style = ctypes.windll.user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        # ctypes.windll.user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)
        # ^ This makes the window itself transparent but not necessarily click-through for underlying windows.
        #   The -transparentcolor attribute is usually sufficient for making parts of the Tkinter window transparent.

        self.canvas = tk.Canvas(self.window, bg=transparent_color, highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        
        self.update_display() # Initial draw
        
        if self._last_position:
            x, y = self._last_position
            self.window.geometry(f"+{x}+{y}")
        else:
            self.root.after(50, self.center_window) # Delay centering slightly for dimensions to finalize

        # Drag-to-move handlers
        self._drag_start_x = 0
        self._drag_start_y = 0
        self._win_start_x = 0
        self._win_start_y = 0

        def start_move(event):
            self._drag_start_x = event.x_root
            self._drag_start_y = event.y_root
            if self.window: # Ensure window exists
                self._win_start_x = self.window.winfo_x()
                self._win_start_y = self.window.winfo_y()

        def do_move(event):
            if self.window: # Ensure window exists
                dx = event.x_root - self._drag_start_x
                dy = event.y_root - self._drag_start_y
                new_x = self._win_start_x + dx
                new_y = self._win_start_y + dy
                self.window.geometry(f"+{new_x}+{new_y}")
                self._last_position = (new_x, new_y)

        self.canvas.bind("<Button-1>", start_move)
        self.canvas.bind("<B1-Motion>", do_move)
        # Also allow dragging by the rounded rectangle background if it's a distinct item
        # If the canvas background IS the rounded rectangle, the above is fine.


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


    def update_display(self):
        if not (self.window and self.canvas and self.window.winfo_exists()):
            return
        
        try:
            self.canvas.delete("all")
            main_font = font.Font(family='Arial', size=14, weight='bold')
            time_font = font.Font(family='Arial', size=12)
            lyrics_font = font.Font(family='Arial', size=11, weight='normal', slant='italic') # Adjusted lyrics font

            # Wrap main text (song title/artist)
            wrapped_player_text = self.wrap_text(self.player_metric['player_text'], max_chars_line=35) # Adjust max_chars_line as needed
            num_player_text_lines = wrapped_player_text.count('\n') + 1

            main_text_bbox = main_font.measure(wrapped_player_text) # May not be accurate for multiline, use line height
            
            main_width = 0
            for line in wrapped_player_text.split('\n'):
                main_width = max(main_width, main_font.measure(line))

            time_width = time_font.measure(self.player_metric['player_duration'])
            
            # Lyrics wrapping and measurement
            display_lyrics_text = ""
            num_lyrics_lines = 0
            lyrics_width = 0
            if self.running_lyrics and self.display_lyrics and self.player_metric['player_lyrics']:
                wrapped_lyrics = self.wrap_text(self.player_metric['player_lyrics'], max_chars_line=40) # Adjust max_chars
                display_lyrics_text = wrapped_lyrics
                num_lyrics_lines = display_lyrics_text.count('\n') + 1
                for line in display_lyrics_text.split('\n'):
                    lyrics_width = max(lyrics_width, lyrics_font.measure(line))


            total_width = max(main_width, time_width, lyrics_width) + 2 * self.padding
            
            main_text_line_height = main_font.metrics("linespace")
            time_text_line_height = time_font.metrics("linespace")
            lyrics_text_line_height = lyrics_font.metrics("linespace")

            height_for_main_text = main_text_line_height * num_player_text_lines
            height_for_time = time_text_line_height
            height_for_lyrics = 0
            if num_lyrics_lines > 0:
                 height_for_lyrics = (lyrics_text_line_height * num_lyrics_lines) + (self.padding / 2 if num_lyrics_lines >0 else 0)


            total_height = height_for_main_text + height_for_time + height_for_lyrics + (2 * self.padding)
            if num_player_text_lines > 1 : total_height += self.padding /2 # Extra padding for multiline title
            if num_lyrics_lines > 0: total_height += self.padding /2 # Extra padding before/after lyrics


            self.canvas.create_rounded_rectangle(
                0, 0, total_width, total_height,
                radius=self.corner_radius,
                fill=self.bg_color, # The actual background of your content
                outline='#777777', # Optional outline for the content box
                width=1
            )

            current_y = self.padding

            # Main text (potentially multiline)
            # Shadow (optional, can be heavy for multiline)
            # self.canvas.create_text(
            #    total_width/2 +1, current_y +1, text=wrapped_player_text, fill='#000000',
            #    font=main_font, anchor=tk.N, justify=tk.CENTER
            # )
            self.canvas.create_text(
                total_width/2, current_y,
                text=wrapped_player_text, fill='#FFFFFF',
                font=main_font, anchor=tk.N, justify=tk.CENTER # Anchor N for top alignment
            )
            current_y += height_for_main_text + (self.padding / (2 if num_player_text_lines > 1 else 1) )


            # Time text
            self.canvas.create_text(
                total_width/2, current_y,
                text=self.player_metric['player_duration'], fill='#AAAAAA',
                font=time_font, anchor=tk.N, justify=tk.CENTER
            )
            current_y += height_for_time + (self.padding /2 if num_lyrics_lines > 0 else 0)


            # Lyrics text (if active and visible)
            if self.running_lyrics and self.display_lyrics and display_lyrics_text:
                self.canvas.create_text(
                    total_width/2, current_y,
                    text=display_lyrics_text, fill='#E0E0E0', # Slightly dimmer white for lyrics
                    font=lyrics_font, anchor=tk.N, justify=tk.CENTER
                )
                # current_y += height_for_lyrics # Not needed if it's the last element

            # Resize window, preserving position
            if self.window and self.window.winfo_exists():
                current_geometry = self.window.geometry() # "WxH+X+Y"
                parts = current_geometry.split('+')
                if len(parts) == 3: # If X and Y are available
                    x_pos, y_pos = parts[1], parts[2]
                    self.window.geometry(f'{int(total_width)}x{int(total_height)}+{x_pos}+{y_pos}')
                else: # Fallback if geometry string is unexpected
                    self.window.geometry(f'{int(total_width)}x{int(total_height)}')

        except tk.TclError as e:
            if "invalid command name" in str(e): # Window or canvas likely destroyed
                # print("GhostOverlay: Update display failed, window/canvas gone.")
                pass
            else:
                print(f"GhostOverlay: TclError in update_display: {e}")
        except Exception as e:
            print(f"GhostOverlay: Unexpected error in update_display: {e}")


    def set_text(self, text: str):
        with self.text_lock:
            self.player_metric['player_text'] = text
        if self.window and self.window.winfo_exists():
            self.root.after(0, self.update_display)
                
    def set_duration(self, current_seconds: float, total_seconds: float):
        def format_time(seconds):
            minutes = int(seconds // 60)
            seconds = int(seconds % 60)
            return f"{minutes}:{seconds:02d}"
        
        current_str = format_time(current_seconds)
        total_str = format_time(total_seconds)
        
        with self.text_lock:
            self.player_metric['player_duration'] = f"{current_str} / {total_str}"
        if self.window and self.window.winfo_exists():
            self.root.after(0, self.update_display)

    def set_lyrics(self, text: str):
        # This sets the raw lyric string. Wrapping happens in update_display.
        with self.text_lock:
            self.player_metric['player_lyrics'] = text if text else "" # Ensure it's a string
        if self.window and self.window.winfo_exists() and self.running_lyrics and self.display_lyrics:
            self.root.after(0, self.update_display)
                    
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
                print(f"Radio IP set to: {self.radio_metric['current_ip']}")
            else:
                self.radio_metric['current_ip'] = '' # No IPs available
                print("No radio IPs available to select.")
        # No automatic display update here, _trigger_radio_station will handle it after attempting connection.


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
            print("DummyPlayer: Skip Previous")
            self.current_song_index = (self.current_song_index - 1 + len(self.playlist)) % len(self.playlist)
            self._update_overlay_song_info()
        def skip_next(self):
            print("DummyPlayer: Skip Next")
            self.current_song_index = (self.current_song_index + 1) % len(self.playlist)
            self._update_overlay_song_info()
        def pause(self, force_play=None): # True to play, False to pause, None to toggle
            if force_play is True: self.is_paused = False
            elif force_play is False: self.is_paused = True
            else: self.is_paused = not self.is_paused
            print(f"DummyPlayer: {'Playing' if not self.is_paused else 'Paused'}")
        def up_volume(self): self.volume = min(100, self.volume + 5); print(f"DummyPlayer: Vol {self.volume}")
        def dwn_volume(self): self.volume = max(0, self.volume - 5); print(f"DummyPlayer: Vol {self.volume}")
        def repeat(self): self.is_repeating = not self.is_repeating; print(f"DummyPlayer: Repeat {'On' if self.is_repeating else 'Off'}")
        def toggle_loop_cycle(self, radio_mode_on: bool): # For radio
            print(f"DummyPlayer: Radio mode {'Activated' if radio_mode_on else 'Deactivated'}")
        def set_radio_ip(self, ip:str):
            if ip: print(f"DummyPlayer: Attempting to stream from radio IP: {ip}"); return True # Simulate success
            print("DummyPlayer: No radio IP to stream from.")
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
        print("Exiting GhostOverlay...")
    finally:
        if overlay.listener and overlay.listener.is_alive():
            overlay.listener.stop()
        # Consider if kill_all_python_processes is desired on normal exit
        # kill_all_python_processes(include_current=False) # Example cleanup


if __name__ == "__main__":
    main()