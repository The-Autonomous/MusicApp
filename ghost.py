import ctypes, signal, psutil, os
import tkinter as tk
from tkinter import font
from threading import Lock
from pynput import keyboard
from pynput.keyboard import Key
from time import sleep, monotonic

# Radio Direct Link

try:
    from radioIpScanner import SimpleRadioScan
except:
    from .radioIpScanner import SimpleRadioScan

# Windows API constants
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
GWL_EXSTYLE = -20

def kill_all_python_processes(include_current: bool = True):
    """
    Kill every python process on the system.

    :param include_current: if True, this process will also be terminated.
                            Default is False so this script can finish cleanup.
    """
    my_pid = os.getpid()

    for proc in psutil.process_iter(['pid', 'name', 'exe', 'cmdline']):
        pid = proc.info['pid']
        if pid == my_pid and not include_current:
            continue

        name = (proc.info['name'] or "").lower()
        exe  = (os.path.basename(proc.info.get('exe') or "")).lower()
        cmd  = " ".join(proc.info.get('cmdline') or []).lower()

        # Identify python processes (handles virtualenv, python3, pythonw.exe, etc.)
        if any(keyword in name for keyword in ('python',)) \
           or any(keyword in exe  for keyword in ('python',)) \
           or cmd.startswith('python'):
            try:
                # Try a graceful terminate first
                proc.terminate()
                proc.wait(timeout=3)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except psutil.TimeoutExpired:
                # Force kill if it didn’t die
                try:
                    proc.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
                
# Add rounded rectangle method to Canvas
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
    
    return self.create_polygon(
        *points,
        smooth=True,
        **kwargs
    )

tk.Canvas.create_rounded_rectangle = create_rounded_rectangle

class GhostOverlay:
    def __init__(self, root):
        """
            Example Metrics:
                Player Metric: 
                    'player_text': ''
                    'player_duration': ''
                    'player_lyrics': ''
                    
                Radio Metric:
                    'current_ip': '0.0.0.0',
                    'availability': [
                            '127.0.0.1',
                            '192.168.1.0',
                        ]
            
        """
        
        self.root = root
        self.window = None
        self.canvas = None
        self._last_position = None
        self.key_hints_popup = None
        self.radioTriggerDebounce = [0, 2.5] # Current Time, Time Between Channel Changes In Seconds
        self.text_lock = Lock()
        self.display_lyrics = True # Toggle To Show Lyrics
        self.running_lyrics = False  # New: lyrics visibility flag
        self.display_radio = False
        self.player_metric = {'player_text':'','player_duration':'', 'player_lyrics':''}
        self.radio_metric = {'current_ip':'0.0.0.0', 'availability':[]} #, 'radio_player': {'radio_text':'', 'radio_duration':'', 'radio_lyrics':''}}
        self.bg_color = '#000000'
        self.corner_radius = 15
        self.padding = 15
        self.last_toggle_state = False
        self.readyForKeys = False

        # Define key actions with required keys, optional forbidden keys, and action
        self.key_actions = [
            {
                'required': ['alt', 'shift'],
                'forbidden': ['ALL'],
                'action': self.toggle_overlay,
                'hint': "Show / Hide Music Player"
            },
            {
                'required': ['alt', '-'],
                'action': self._trigger_skip_previous,
                'hint': "Skip To The Previous Song"
            },
            {
                'required': ['alt', '='],
                'action': self._trigger_skip_next,
                'hint': "Skip To The Next Song"
            },
            {
                'required': ['alt', '['],
                'action': self._trigger_volume_dwn,
                'hint': "Turn Down Music Volume"
            },
            {
                'required': ['alt', ']'],
                'action': self._trigger_volume_up,
                'hint': "Turn Up Music Volume"
            },
            {
                'required': ['alt', ';'],
                'action': self._trigger_pause,
                'hint': "Pause The Music Player"
            },
            {
                'required': ['alt', '\''],
                'action': self._trigger_repeat,
                'hint': "Enable / Disable Repeat Mode"
            },
            {
                'required': ['alt', '/'],
                'action': self._trigger_lyrics_toggle,
                'hint': "Show / Hide Lyrics"
            },
            {
                'required': ['alt', 'right alt'],
                'action': self.show_key_hints,
                'hint': "Show Controls"
            },
            {
                'required': ['alt', 'a'],
                'forbidden': ['ALL'],
                'action': self._trigger_radio_toggle,
                'hint': "Enable / Disable Radio"
            },
            {
                'required': ['alt', '`'],
                'action': self._trigger_radio_station,
                'hint': "Scan For A Radio Station"
            },
            {
                'required': ['right alt', 'shift'],
                'action': kill_all_python_processes,
                'hint': "Close The Media Player Entirely (Closes All Python Tasks)"
            },
        ]

        # Build the universe of keys (required + any static forbidden, skipping 'ALL')
        all_existing_keys = {
            k
            for act in self.key_actions
            for k in (
                act['required'] +
                [f for f in act.get('forbidden', []) if f != 'ALL']
            )
        }

        # Initialize your pressed-state map
        self.keys_pressed = {k: False for k in all_existing_keys}
        
        self.VK_CODE = {'alt': 0x12}

        # Now resolve any 'ALL' entries against that universe
        for act in self.key_actions:
            if 'forbidden' in act and 'ALL' in act['forbidden']:
                act['forbidden'] = [
                    k for k in all_existing_keys
                    if k not in act['required']
                ]
            
        # Register generic handlers for all relevant keys
        self.listener = keyboard.Listener(
            on_press=self._handle_key_press,
            on_release=self._handle_key_release
        )
        self.listener.start()

        self.check_keyboard()
        
        self.readyForKeys = True # Allow Keypresses Now

    def check_keyboard(self):
        self.root.after(100, self.check_keyboard)
        
    def _handle_key_press(self, key):
        if not self.readyForKeys:
            return
        name = self._normalize_key(key)
        if name in self.keys_pressed:
            self.keys_pressed[name] = True
            self._check_toggle()

    def _handle_key_release(self, key):
        name = self._normalize_key(key)
        if name in self.keys_pressed:
            self.keys_pressed[name] = False
            self.last_toggle_state = False

    def _normalize_key(self, key):
        # Turn the key into a string like 'alt_l', 'shift', 'a', '1', etc.
        s = str(key).lower()
        if s.startswith("key."):
            name = s.split(".", 1)[1]
        else:
            # for KeyCode prints like "'a'"
            name = s.strip("'")
        
        # map left/right to generic
        if name == "alt_l":
            return "alt"
        if name == "alt_r":
            return "right alt"
        if name == "alt_gr":
            return "right alt"
        if name in ("shift_l", "shift_r"):
            return "shift"
        if name in ("ctrl_l", "ctrl_r", "ctrl"):
            return "ctrl"
        # leave everything else (letters, digits, punctuation) as-is
        return name

    # 2) Helper to sync your dict with real state
    def _sync_key_states(self):
        for name in self.keys_pressed:
            vk = self.VK_CODE.get(name)
            if vk is not None:
                # high bit set == currently down
                self.keys_pressed[name] = bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)

    def _check_toggle(self):
        # sync against real keyboard
        self._sync_key_states()

        if self.last_toggle_state:
            return

        for action in self.key_actions:
            required_met = all(self.keys_pressed.get(k, False) for k in action['required'])
            forbidden_met = not any(self.keys_pressed.get(k, False) for k in action.get('forbidden', []))
            if required_met and forbidden_met:
                action['action']()
                self.last_toggle_state = True

                # clear only non-modifiers so you can keep holding Alt/Shift
                for k in list(self.keys_pressed):
                    if k not in ('alt'):
                        self.keys_pressed[k] = False
                break

    def _trigger_skip_previous(self):
        if hasattr(self, 'MusicPlayer'):
            self.MusicPlayer.skip_previous()

    def _trigger_skip_next(self):
        if hasattr(self, 'MusicPlayer'):
            self.MusicPlayer.skip_next()

    def _trigger_pause(self):
        if hasattr(self, 'MusicPlayer'):
            self.MusicPlayer.pause()
            
    def _trigger_volume_up(self):
        if hasattr(self, 'MusicPlayer'):
            self.MusicPlayer.up_volume()
            
    def _trigger_volume_dwn(self):
        if hasattr(self, 'MusicPlayer'):
            self.MusicPlayer.dwn_volume()
            
    def _trigger_repeat(self):
        if hasattr(self, 'MusicPlayer'):
            self.MusicPlayer.repeat()
            
    def _trigger_lyrics_toggle(self):
        if hasattr(self, 'MusicPlayer'):
            self.display_lyrics = not self.display_lyrics
            if self.window:
                self.root.after(0, self.update_display)
                
    def _trigger_radio_toggle(self):
        if hasattr(self, 'MusicPlayer') and monotonic() - self.radioTriggerDebounce[0] >= self.radioTriggerDebounce[1]:
            self.radioTriggerDebounce[0] = monotonic()
            self.display_radio = not self.display_radio
            self.MusicPlayer.toggle_loop_cycle(self.display_radio)
            
    def _trigger_radio_station(self, atmpt = 0, max_loop = 5):
        if hasattr(self, 'MusicPlayer') and not atmpt >= max_loop:
            self.set_radio_channel()
            if not self.MusicPlayer.set_radio_ip(self.radio_metric['current_ip']):
                self._trigger_radio_station(atmpt + 1) # If It Doesnt Work Retry For max_loop Tries
            if self.window:
                self.root.after(0, self.update_display)

    def toggle_overlay(self):
        try:
            if self.window and self.window.winfo_exists():
                self.close_overlay()
            else:
                self.open_overlay()
        except:
            print("Open Not Ready Yet")
            try:
                sleep(3)
                self.toggle_overlay()
            except:
                print("Could Not Persist Opening Overlay")

    def show_key_hints(self):
        if self.key_hints_popup:
            self.key_hints_popup.destroy()
            self.key_hints_popup = None
        
        self.key_hints_popup = tk.Toplevel()
        self.key_hints_popup.overrideredirect(True)         # No title bar
        self.key_hints_popup.configure(bg="#1e1e1e")

        # Main frame
        frame = tk.Frame(self.key_hints_popup, bg="#2e2e2e", bd=2, relief="ridge")
        frame.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        self.key_hints_popup.grid_rowconfigure(0, weight=1)
        self.key_hints_popup.grid_columnconfigure(0, weight=1)

        # Title
        tk.Label(
            frame, text="✨ Music Player Controls ✨",
            font=("Helvetica", 18, "bold"),
            bg="#2e2e2e", fg="#00ffd5"
        ).grid(row=0, column=0, pady=(0,5))

        # Separator
        tk.Frame(frame, height=2, bg="#444444")\
        .grid(row=1, column=0, sticky="ew", pady=(0,10))

        # List of shortcuts (row=2 expands)
        list_frame = tk.Frame(frame, bg="#2e2e2e")
        list_frame.grid(row=2, column=0, sticky="nsew")
        frame.grid_rowconfigure(2, weight=1)

        for i, action in enumerate(self.key_actions):
            keys = " + ".join(
                k.upper() if k.isalnum() else f"'{k}'"
                for k in action['required']
            )
            hint = action['hint']
            tk.Label(
                list_frame,
                text=f"{keys} → {hint}",
                font=("Helvetica", 12),
                bg="#2e2e2e", fg="#ffffff",
                anchor="w", padx=10, pady=2
            ).grid(row=i, column=0, sticky="w")

        # Big, always-visible Close button (row=3)
        close_btn = tk.Button(
            frame, text="✖ Close",
            command=self.key_hints_popup.destroy,
            font=("Helvetica", 16, "bold"),
            bg="#ff4d4d", fg="#ffffff",
            activebackground="#ff1a1a", activeforeground="#ffffff",
            relief="raised", bd=2, padx=20, pady=10
        )
        close_btn.grid(row=3, column=0, sticky="ew", pady=(10,0))

        # ESC = close
        self.key_hints_popup.bind("<Escape>", lambda e: self.key_hints_popup.destroy())

        # Drag-to-move
        def start_move(e):
            self.key_hints_popup._x, self.key_hints_popup._y = e.x, e.y
        def do_move(e):
            self.key_hints_popup.geometry(f"+{e.x_root - self.key_hints_popup._x}+{e.y_root - self.key_hints_popup._y}")
        frame.bind("<Button-1>", start_move)
        frame.bind("<B1-Motion>", do_move)

        # Show on top briefly
        self.key_hints_popup.lift()
        self.key_hints_popup.attributes("-topmost", True)
        self.key_hints_popup.after_idle(self.key_hints_popup.attributes, "-topmost", False)
        
    def open_overlay(self):
        self.window = tk.Toplevel(self.root)
        self.window.overrideredirect(True)
        self.window.attributes('-alpha', 0.7)
        self.window.attributes('-topmost', True)
        self.window.attributes('-transparentcolor', 'gray1')
        self.window.config(bg='gray1')

        hwnd = ctypes.windll.user32.GetParent(self.window.winfo_id())
        style = ctypes.windll.user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        #ctypes.windll.user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, 
        #                                     style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
        ctypes.windll.user32.SetWindowLongPtrW(
            hwnd, GWL_EXSTYLE,
            style | WS_EX_LAYERED
        )

        self.canvas = tk.Canvas(self.window, bg='gray1', highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.update_display()
        
        if self._last_position:
            x, y = self._last_position
            self.window.geometry(f"+{x}+{y}")
        else:
            self.center_window()
        
        # --- DRAG-TO-MOVE HANDLERS ---
        # store these on self so they stick around after the function ends
        def start_move(event):
            # record the mouse’s screen-coords and the window’s current position
            self._drag_start_x = event.x_root
            self._drag_start_y = event.y_root
            self._win_start_x = self.window.winfo_x()
            self._win_start_y = self.window.winfo_y()

        def do_move(event):
            # how far we’ve moved since the click
            dx = event.x_root - self._drag_start_x
            dy = event.y_root - self._drag_start_y
            # update window pos
            new_x = self._win_start_x + dx
            new_y = self._win_start_y + dy
            self.window.geometry(f"+{new_x}+{new_y}")
            self._last_position = (new_x, new_y)

        # bind to the canvas (you can bind to self.window.bind() if you prefer)
        self.canvas.bind("<Button-1>", start_move)
        self.canvas.bind("<B1-Motion>", do_move)

    def center_window(self):
        self.window.update_idletasks()
        width = self.window.winfo_width()
        height = self.window.winfo_height()
        x = (self.window.winfo_screenwidth() - width) // 2
        self.window.geometry(f'+{x}+20')

    def close_overlay(self):
        if self.window:
            self.window.destroy()
            self.window = None
            
    def wrap_text(self, text: str) -> str:
        """
        Wrap text into exactly two lines, splitting at word boundaries.
        Adjust width as needed to make the lines even.
        """
        words = text.split()
        
        if len(words) <= 2:
            return text  # No point splitting tiny inputs
        
        # Find the halfway point (favoring the first line being slightly longer if odd)
        midpoint = (len(words) + 1) // 2
        
        line1 = " ".join(words[:midpoint])
        line2 = " ".join(words[midpoint:])
        
        return f"{line1}\n{line2}"

    def update_display(self):
        if not self.window or not self.canvas:
            return
        try:
            self.canvas.delete("all")
            main_font = font.Font(family='Arial', size=14, weight='bold')
            time_font = font.Font(family='Arial', size=12)
            lyrics_font = font.Font(family='Arial', size=12, weight='bold')
            
            # Existing measurements for main text and time
            main_width = main_font.measure(self.player_metric['player_text'])
            time_width = time_font.measure(self.player_metric['player_duration'])
            
            # New: Handle lyrics wrapping
            lyrics_width = lyrics_font.measure(self.player_metric['player_lyrics']) if self.running_lyrics and self.display_lyrics else 0

            # Calculate total width including lyrics
            total_width = max(main_width, time_width, lyrics_width) + 2*self.padding
            
            # Calculate heights based on lyrics visibility
            main_height = main_font.metrics("linespace") * 2
            time_height = time_font.metrics("linespace")
            
            total_height = main_height + time_height + (time_height if self.running_lyrics and self.display_lyrics else 0) + 2*self.padding # Same Equation But With Lyrics

            self.canvas.create_rounded_rectangle(
                0, 0, total_width, total_height,
                radius=self.corner_radius,
                fill=self.bg_color,
                outline=''
            )

            # Adjust positions based on lyrics visibility
            if self.running_lyrics and self.display_lyrics:
                main_y = (total_height / 5)
                time_y = (total_height - 10)
                lyrics_y = (total_height * 3 / 5)
            else:
                main_y = (total_height / 3)
                time_y = (total_height * 2 / 3)
                lyrics_y = 0  # Not used

            # Main text shadow
            for dx, dy in [(-1,-1), (-1,1), (1,-1), (1,1)]:
                self.canvas.create_text(
                    total_width/2 + dx, main_y + dy,
                    text=self.player_metric['player_text'],
                    fill='#000000',
                    font=main_font,
                    anchor=tk.CENTER
                )
                
            # Main text
            self.canvas.create_text(
                total_width/2, main_y,
                text=self.player_metric['player_text'],
                fill='#FFFFFF',
                font=main_font,
                anchor=tk.CENTER
            )

            # Time text
            self.canvas.create_text(
                total_width/2, time_y,
                text=self.player_metric['player_duration'],
                fill='#AAAAAA',
                font=time_font,
                anchor=tk.CENTER
            )

            # New: Lyrics text (only when running_lyrics is True)
            if self.running_lyrics and self.player_metric['player_lyrics']:
                self.canvas.create_text(
                    total_width/2, lyrics_y,
                    text=self.player_metric['player_lyrics'] if self.display_lyrics else "",
                    fill='#FFFFFF',
                    font=lyrics_font,
                    anchor=tk.CENTER,  # West (left) anchor
                    justify=tk.CENTER
                )
            
            x = self.window.winfo_x()
            y = self.window.winfo_y()

            # resize but keep the same x/y
            self.window.geometry(f'{int(total_width)}x{int(total_height)}+{x}+{y}')
        except:
            pass

    def set_text(self, text: str):
        with self.text_lock:
            self.player_metric['player_text'] = text
            if self.window:
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
            if self.window:
                self.root.after(0, self.update_display)

    def set_lyrics(self, text: str):
        if not text == "":
            with self.text_lock:
                self.player_metric['player_lyrics'] = self.wrap_text(text)
                if self.window:
                    self.root.after(0, self.update_display)
                    
    def set_radio_ips(self, ip_list: list):
        """
        Sets The Radios Available To Be Streamed From
        """
        with self.text_lock:
            self.radio_metric['availability'] = ip_list
            if len(ip_list) <= 0 or not self.radio_metric['current_ip'] in ip_list:
                self.radio_metric['current_ip'] = ''
        
    def _get_next_(self, items: list, value):
        try:
            current_index = items.index(value)
            return items[(current_index + 1) % len(items)]
        except ValueError:
            return items[0]
    
    def set_radio_channel(self):
        """
        Skips the current ip to the next available one
        """
        
        with self.text_lock:
            ip_list = self.radio_metric['availability']
            if len(ip_list) >= 1:
                current_ip = self.radio_metric['current_ip']
                self.radio_metric['current_ip'] = self._get_next_(ip_list, current_ip)

    # New: Method to toggle lyrics visibility
    def toggle_lyrics(self, state: bool):
        if not self.running_lyrics == state: # Ignore Toggle If Already Set
            with self.text_lock:
                self.running_lyrics = state
                if not state:
                    self.current_lyrics = ""
                if self.window:
                    self.root.after(0, self.update_display)

def main():
    root = tk.Tk()
    root.withdraw()
    overlay = GhostOverlay(root)
    root.mainloop()

if __name__ == "__main__":
    main()