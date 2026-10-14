"""A collapsible loading card with app-drawn controls and live progress."""
import tkinter as tk
from tkinter import ttk


class LoadingHelper:
    def __init__(self, window, body, heading, message, colors):
        self.window, self.body = window, body
        self.collapsed = False
        self.full_geometry = None
        self._animation = None
        self._indeterminate = True
        self.mini = tk.Frame(window, bg=colors["window_bg"], padx=16, pady=12)
        self.summary = tk.Label(self.mini, textvariable=message, bg=colors["window_bg"],
                                fg=colors["text"], font=("Segoe UI", 10),
                                justify="left", anchor="w", wraplength=260)
        self.summary.pack(side="left", fill="both", expand=True)
        style = ttk.Style(window)
        style.configure("MusicApp.Horizontal.TProgressbar", troughcolor=colors["frame_bg"],
                        background=colors["accent"], borderwidth=0, thickness=5)
        footer = body.winfo_children()[-1]
        self.progress = ttk.Progressbar(body, style="MusicApp.Horizontal.TProgressbar",
                                       mode="indeterminate", maximum=100)
        self.progress.pack(fill="x", before=footer, pady=(0, 16))
        self.progress.bind("<Destroy>", lambda event: self._stop_animation())
        self._animate()
        self.minus = tk.Button(body, text="−", command=self.collapse, bg=colors["window_bg"],
                               fg=colors["text_dark"], activebackground=colors["frame_bg"],
                               activeforeground=colors["accent"], font=("Segoe UI", 16),
                               relief="flat", bd=0, cursor="hand2", padx=4, pady=0)
        self.minus.place(relx=1, x=0, y=-8, anchor="ne")
        heading.configure(padx=0)
        # Leave space for the minus control in longer headings.
        heading.pack_configure(padx=(0, 32))
        tk.Button(self.mini, text="↗", command=self.restore, bg=colors["window_bg"],
                  fg=colors["accent"], activebackground=colors["frame_bg"], relief="flat",
                  bd=0, font=("Segoe UI", 15), cursor="hand2", padx=8).pack(side="right")
        from ui_window import style_card
        style_card(window, self.summary)
        window.bind("<Escape>", lambda event: self.collapse())

    def collapse(self):
        if self.collapsed:
            return
        self.window.update_idletasks()
        self.full_geometry = self.window.geometry()
        self.collapsed = True
        self._stop_animation()
        self.body.pack_forget()
        self.mini.pack(fill="both", expand=True)
        self.window.update_idletasks()
        self.window.geometry(f"330x{max(64, self.mini.winfo_reqheight())}")

    def restore(self):
        if not self.collapsed:
            return
        self.collapsed = False
        self.mini.pack_forget()
        self.body.pack(fill="both", expand=True)
        self.window.geometry(self.full_geometry)
        self._animate()

    def _stop_animation(self):
        if self._animation is not None:
            self.window.after_cancel(self._animation)
            self._animation = None

    def _animate(self):
        self._animation = None
        if self._indeterminate and not self.collapsed:
            self.progress.step(3)
            self._animation = self.window.after(80, self._animate)

    def set_progress(self, saved=None, total=None, active=True):
        self._stop_animation()
        self._indeterminate = total is None and active
        if total is None:
            self.progress.configure(mode="indeterminate")
            self._animate()
        else:
            self.progress.configure(mode="determinate", maximum=max(1, total), value=saved or 0)
        if self.collapsed:
            self.window.update_idletasks()
            self.window.geometry(f"330x{max(64, self.mini.winfo_reqheight())}")
