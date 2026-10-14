"""On-demand controls card, with no player, hooks, or recurring timers."""

import tkinter as tk
from tkinter import ttk


CONTROL_GROUPS = (
    ("Playback", ("skip_previous", "pause_play", "skip_next", "volume_down", "repeat_toggle", "volume_up")),
    ("Find & tune", ("show_search", "show_eq_menu", "youtube_sync")),
    ("Player", ("lyrics_toggle_visibility", "toggle_overlay", "player_on_off")),
    ("Radio", ("radio_enable_toggle", "radio_scan_station")),
    ("App", ("show_hints", "player_restart", "kill_self")),
)

ACTION_LABELS = {
    "skip_previous": "Previous song", "skip_next": "Next song", "pause_play": "Play / pause",
    "volume_down": "Volume down", "volume_up": "Volume up", "repeat_toggle": "Repeat",
    "show_search": "Find a song", "show_eq_menu": "Sound & effects", "youtube_sync": "YouTube",
    "lyrics_toggle_visibility": "Show / hide lyrics", "toggle_overlay": "Show / hide player",
    "player_on_off": "Player on / off", "radio_enable_toggle": "Radio on / off",
    "radio_scan_station": "Find a station", "show_hints": "Open controls",
    "player_restart": "Restart app", "kill_self": "Quit app",
}


def effective_chord(action):
    return tuple(key.lower() for key in action["required"]
                 if key.lower() != "alt" or action.get("alt_needed", True))


def shortcut_label(action):
    keys = effective_chord(action)
    if set(keys) == {"alt", "right alt"}:
        return "Both Alt keys"
    names = {"alt": "Left Alt", "right alt": "Right Alt", "shift": "Left Shift",
             "right shift": "Right Shift", "ctrl": "Ctrl", "escape": "Esc",
             "space": "Space", "enter": "Enter", "backspace": "Backspace",
             "delete": "Delete", "up": "↑", "down": "↓", "left": "←", "right": "→"}
    return " + ".join(names.get(key, key.upper()) for key in keys)


def replacement_chord(action, key):
    """Keep the existing Alt side when a shortcut is reassigned."""
    modifier = "right alt" if "right alt" in action["required"] and "alt" not in action["required"] else "alt"
    return [modifier, key.lower()]


def conflicting_action(actions, candidate, exclude_id):
    chord = frozenset(effective_chord(candidate))
    return next((action for action in actions if action["id"] != exclude_id
                 and frozenset(effective_chord(action)) == chord), None)


def build_controls_window(root, colors, actions, *, run_action, on_edit, on_alt_toggle,
                          on_reset, on_close, customize=False):
    """Build once on request. Callbacks belong to the Tk thread."""
    popup = tk.Toplevel(root)
    popup.withdraw()
    popup.title("MusicApp · Controls")
    popup.configure(bg=colors["window_bg"])
    popup.attributes("-topmost", True)
    popup.protocol("WM_DELETE_WINDOW", on_close)
    popup.bind("<Escape>", on_close)
    popup._binding_variables = []
    font_normal = ("Segoe UI", 11)
    font_small = ("Segoe UI", 10)
    by_id = {action["id"]: action for action in actions}

    body = tk.Frame(popup, bg=colors["window_bg"], padx=22, pady=18)
    body.pack(fill="both", expand=True)
    heading = tk.Label(body, text="Music, at your fingertips", bg=colors["window_bg"],
                       fg=colors["text"], font=("Segoe UI", 20, "bold"), anchor="w")
    heading.pack(fill="x")
    from ui_window import style_card
    style_card(popup, heading)
    help_action = by_id.get("show_hints")
    help_shortcut = shortcut_label(help_action) if help_action else "Both Alt keys"
    tk.Label(body, text=f"{help_shortcut} opens this card from anywhere.  Esc closes it.",
             bg=colors["window_bg"], fg=colors["text_dark"], font=font_small,
             anchor="w").pack(fill="x", pady=(6, 14))

    tabs = tk.Frame(body, bg=colors["window_bg"])
    tabs.pack(fill="x", pady=(0, 14))
    content = tk.Frame(body, bg=colors["window_bg"])
    content.pack(fill="both", expand=True)
    quick_page = tk.Frame(content, bg=colors["window_bg"])
    shortcut_page = tk.Frame(content, bg=colors["window_bg"])
    status = tk.Label(body, text="", bg=colors["window_bg"], fg=colors["warning"],
                      font=font_small, anchor="w", justify="left", wraplength=600)
    status.pack(fill="x", pady=(10, 0))
    footer = tk.Frame(body, bg=colors["window_bg"])
    footer.pack(fill="x", pady=(12, 0))
    footer_hint = tk.Label(footer, text="Media keys work too. Right-drag to move the player.\n"
                          "In games, borderless fullscreen keeps the player visible.",
             bg=colors["window_bg"], fg=colors["text_dark"], font=("Segoe UI", 9),
             anchor="w", justify="left", wraplength=480)
    done_button = tk.Button(footer, text="Done", command=on_close, bg=colors["accent"],
              fg=colors["window_bg"], activebackground=colors["accent_active"],
              activeforeground=colors["window_bg"], relief="flat", borderwidth=0,
              font=font_normal, padx=18, pady=7, cursor="hand2")
    done_button.pack(side="right", padx=(12, 0))
    footer_hint.pack(side="left", fill="x", expand=True)
    footer.bind("<Configure>", lambda event: footer_hint.configure(
        wraplength=max(160, event.width - done_button.winfo_reqwidth() - 12)))

    for group, ids in CONTROL_GROUPS[:3]:
        tk.Label(quick_page, text=group.upper(), bg=colors["window_bg"],
                 fg=colors["text_dark"], font=("Segoe UI", 9, "bold"),
                 anchor="w").pack(fill="x", pady=(0 if group == "Playback" else 12, 6))
        grid = tk.Frame(quick_page, bg=colors["window_bg"])
        grid.pack(fill="x")
        for column in range(3):
            grid.columnconfigure(column, weight=1, uniform="controls")
        for index, action_id in enumerate(ids):
            action = by_id.get(action_id)
            if not action:
                continue
            button = tk.Button(grid, text=f"{ACTION_LABELS[action_id]}\n{shortcut_label(action)}",
                               command=lambda item=action: run_action(item),
                               bg=colors["frame_bg"], fg=colors["text"],
                               activebackground=colors["entry_bg"], activeforeground=colors["accent"],
                               relief="flat", borderwidth=0, font=font_normal,
                               padx=6, pady=9, cursor="hand2", takefocus=True)
            button.grid(row=index // 3, column=index % 3, sticky="ew", padx=3, pady=3)

    editor_built = False

    def build_editor():
        nonlocal editor_built
        if editor_built:
            return
        editor_built = True
        tk.Label(shortcut_page, text="Choose Change, then press a key. Your Alt side stays the same.",
                 bg=colors["window_bg"], fg=colors["text_dark"], font=font_small,
                 anchor="w").pack(fill="x", pady=(0, 10))
        scroll_frame = tk.Frame(shortcut_page, bg=colors["window_bg"])
        scroll_frame.pack(fill="both", expand=True)
        canvas = tk.Canvas(scroll_frame, bg=colors["window_bg"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(scroll_frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        rows = tk.Frame(canvas, bg=colors["window_bg"])
        window_id = canvas.create_window((0, 0), window=rows, anchor="nw")
        rows.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))
        popup.bind("<MouseWheel>", lambda event: canvas.yview_scroll(-int(event.delta / 120), "units"))
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        for group, ids in CONTROL_GROUPS:
            tk.Label(rows, text=group.upper(), bg=colors["window_bg"], fg=colors["text_dark"],
                     font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x", pady=(12, 6))
            for action_id in ids:
                action = by_id.get(action_id)
                if not action:
                    continue
                row = tk.Frame(rows, bg=colors["frame_bg"], padx=10, pady=8)
                row.pack(fill="x", pady=2)
                row.columnconfigure(0, weight=1)
                tk.Label(row, text=ACTION_LABELS[action_id], bg=colors["frame_bg"],
                         fg=colors["text"], font=font_normal, anchor="w").grid(row=0, column=0, sticky="w")
                tk.Label(row, text=shortcut_label(action), bg=colors["frame_bg"],
                         fg=colors["accent"], font=font_small, anchor="w").grid(row=1, column=0, sticky="w", pady=(2, 0))
                if action.get("modifiable"):
                    if "alt" in action["required"]:
                        variable = tk.BooleanVar(master=popup, value=action.get("alt_needed", True))
                        popup._binding_variables.append(variable)
                        tk.Checkbutton(row, text="Require Left Alt", variable=variable,
                                       command=lambda item=action: on_alt_toggle(item),
                                       bg=colors["frame_bg"], fg=colors["text_dark"],
                                       activebackground=colors["frame_bg"], activeforeground=colors["text"],
                                       selectcolor=colors["entry_bg"], font=font_small,
                                       borderwidth=0).grid(row=0, column=1, rowspan=2, padx=6)
                    tk.Button(row, text="Change", command=lambda item_id=action_id: on_edit(item_id),
                              bg=colors["entry_bg"], fg=colors["text"],
                              activebackground=colors["button_active"], activeforeground=colors["text"],
                              relief="flat", borderwidth=0, font=font_small,
                              padx=12, pady=6, cursor="hand2").grid(row=0, column=2, rowspan=2)
                else:
                    tk.Label(row, text="Fixed", bg=colors["frame_bg"], fg=colors["text_dark"],
                             font=font_small).grid(row=0, column=2, rowspan=2, padx=12)
        tk.Button(shortcut_page, text="Restore default shortcuts", command=on_reset,
                  bg=colors["window_bg"], fg=colors["text_dark"],
                  activebackground=colors["frame_bg"], activeforeground=colors["text"],
                  relief="flat", borderwidth=0, font=font_small, pady=8,
                  cursor="hand2").pack(anchor="w", pady=(8, 0))

    tab_buttons = []

    def select_tab(index):
        popup._customize_controls = bool(index)
        if index:
            build_editor()
        for page in (quick_page, shortcut_page):
            page.pack_forget()
        (shortcut_page if index else quick_page).pack(fill="both", expand=True)
        for tab_index, button in enumerate(tab_buttons):
            button.configure(bg=colors["accent"] if tab_index == index else colors["frame_bg"],
                             fg=colors["window_bg"] if tab_index == index else colors["text_dark"])

    for index, label in enumerate(("Quick controls", "Shortcuts")):
        button = tk.Button(tabs, text=label, command=lambda item=index: select_tab(item),
                           relief="flat", borderwidth=0, font=font_normal, padx=16, pady=8,
                           activebackground=colors["accent_active"], cursor="hand2")
        button.pack(side="left", padx=(0, 6))
        tab_buttons.append(button)
    select_tab(int(customize))
    popup.update_idletasks()
    width = min(max(380, popup.winfo_screenwidth() - 40), max(660, popup.winfo_reqwidth()))
    height = min(max(400, popup.winfo_screenheight() - 100), max(610, popup.winfo_reqheight()))
    x = max(0, (popup.winfo_screenwidth() - width) // 2)
    y = max(0, (popup.winfo_screenheight() - height) // 2)
    popup.geometry(f"{width}x{height}+{x}+{y}")
    popup.minsize(min(580, width), min(480, height))
    popup.deiconify()
    return popup, status
