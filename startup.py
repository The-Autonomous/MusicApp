"""Prepare missing runtime packages before loading hooks or the audio player."""

import importlib
import importlib.util
from queue import Empty, Queue
from threading import Event, Thread


CORE_MODULES = ("numpy", "av", "sounddevice", "mutagen", "pynput", "requests",
                "aiohttp", "flask", "flask_compress", "waitress", "psutil")


def missing_runtime_modules(modules=CORE_MODULES):
    missing = []
    for module in modules:
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, AttributeError, ValueError):
            found = False
        if not found:
            missing.append(module)
    return tuple(missing)


def prepare_runtime(cancel, *, runner=None, probe=None):
    """The worker owns installation; the parent only verifies module discovery."""
    if cancel.is_set():
        raise RuntimeError("Preparation stopped.")
    if runner is None:
        from runtime_policy import run_job
        runner = run_job
    probe = probe or missing_runtime_modules
    runner({"action": "prepare_app"}, cancel, timeout=270)
    if cancel.is_set():
        raise RuntimeError("Preparation stopped.")
    importlib.invalidate_caches()
    missing = probe()
    if missing:
        raise RuntimeError("Some essentials are still unavailable: " + ", ".join(missing))


def preparation_error(exc):
    message = " ".join(str(exc).split()) or "Please check your connection and try again."
    return "Couldn't finish preparing MusicApp.\n" + message[:360]


def ensure_runtime(root):
    """Return immediately when ready; otherwise show one cancellable setup card."""
    if not missing_runtime_modules():
        return True

    import tkinter as tk

    window = tk.Toplevel(root)
    window.withdraw()
    window.title("MusicApp")
    window.resizable(False, False)
    background, card, text, accent = "#1d1f24", "#23272e", "#e0e0e0", "#00ffd5"
    window.configure(bg=background)
    frame = tk.Frame(window, bg=background, padx=26, pady=23)
    frame.pack(fill="both", expand=True)
    heading = tk.Label(frame, text="Preparing MusicApp", bg=background, fg=accent,
                       font=("Segoe UI", 18, "bold"), anchor="w")
    heading.pack(fill="x")
    from ui_window import style_card
    style_card(window, heading)
    message = tk.StringVar(master=window)
    tk.Label(frame, textvariable=message, bg=background, fg=text,
             font=("Segoe UI", 11), anchor="w", justify="left",
             wraplength=382).pack(fill="x", pady=(14, 20))
    buttons = tk.Frame(frame, bg=background)
    buttons.pack(fill="x", side="bottom")
    from ui_loading import LoadingHelper
    helper = LoadingHelper(window, frame, heading, message,
                           {"window_bg": background, "frame_bg": card,
                            "text": text, "text_dark": "#aaaaaa", "accent": accent})
    cancel = Event()
    completion = Queue(maxsize=1)
    thread = None
    poll_id = None
    closing = False
    result = False

    def fit_window():
        if helper.collapsed:
            return
        window.update_idletasks()
        width, height = max(440, frame.winfo_reqwidth()), max(245, frame.winfo_reqheight())
        x = max(0, (window.winfo_screenwidth() - width) // 2)
        y = max(0, (window.winfo_screenheight() - height) // 2)
        window.geometry(f"{width}x{height}+{x}+{y}")

    def finish(success):
        nonlocal result, poll_id
        result = success
        if poll_id is not None:
            window.after_cancel(poll_id)
            poll_id = None
        window.destroy()

    def close():
        nonlocal closing
        closing = True
        cancel.set()
        if thread is not None and thread.is_alive():
            heading.configure(text="Closing MusicApp")
            message.set("Stopping preparation…")
            retry_button.pack_forget()
            close_button.configure(state="disabled")
        else:
            finish(False)

    def poll():
        nonlocal poll_id
        poll_id = None
        try:
            error = completion.get_nowait()
        except Empty:
            poll_id = window.after(100, poll)
            return
        if closing:
            finish(False)
        elif error is None:
            finish(True)
        else:
            print(f"[Startup] {type(error).__name__}: {error}", flush=True)
            heading.configure(text="Let's try that again")
            message.set(preparation_error(error))
            helper.set_progress(active=False)
            retry_button.pack(side="left")
            close_button.configure(text="Close", state="normal")
            fit_window()

    def begin():
        nonlocal thread, poll_id, closing
        if thread is not None and thread.is_alive():
            return
        closing = False
        cancel.clear()
        heading.configure(text="Preparing MusicApp")
        message.set("Getting a few things ready. This only happens when something is missing.")
        helper.set_progress()
        retry_button.pack_forget()
        close_button.configure(text="Cancel", state="normal")
        fit_window()

        def work():
            try:
                prepare_runtime(cancel)
                error = None
            except Exception as exc:
                error = exc
            completion.put(error)

        thread = Thread(target=work, daemon=True, name="startup-preparation")
        thread.start()
        poll_id = window.after(100, poll)

    retry_button = tk.Button(buttons, text="Retry", command=begin, font=("Segoe UI", 10),
                             bg=accent, fg=background, activebackground="#00bfa5",
                             activeforeground=background, relief="flat", bd=0,
                             padx=18, pady=8, cursor="hand2")
    close_button = tk.Button(buttons, text="Cancel", command=close, font=("Segoe UI", 10),
                             bg=card, fg=text, activebackground="#2c313a", activeforeground=text,
                             relief="flat", bd=0, padx=18, pady=8, cursor="hand2")
    close_button.pack(side="right")
    window.protocol("WM_DELETE_WINDOW", close)
    begin()
    window.deiconify()
    try:
        root.wait_window(window)
    finally:
        cancel.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
    return result
