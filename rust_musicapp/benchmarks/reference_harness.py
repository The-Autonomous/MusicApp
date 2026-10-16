"""Controlled frozen Python renderer/audio workload, without account or game input.

Imports the unchanged reference sources. This deliberately excludes controller,
account, library scan and network work; results describe these matched components.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "BLIS_NUM_THREADS"):
    os.environ[name] = "1"
sys.dont_write_bytecode = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--seconds", type=float, default=12)
    parser.add_argument("--media", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--profile-dir", type=Path, help="Imported Rust settings; sanitized settings only, never credentials")
    parser.add_argument("--no-hooks", action="store_true")
    parser.add_argument("--no-network", action="store_true")
    parser.add_argument("--mute", action="store_true")
    args = parser.parse_args()
    args.state_dir = args.state_dir.resolve()
    args.media = args.media.resolve()
    args.state_dir.mkdir(parents=True, exist_ok=True)
    if args.profile_dir:
        from profile_settings import sanitized_profile, prepare_trial_profile
        prepare_trial_profile(args.state_dir, args.benchmark, sanitized_profile(args.profile_dir))
    preferences_path = args.state_dir / "preferences.json"
    preferences = json.loads(preferences_path.read_text(encoding="utf-8-sig")) if preferences_path.is_file() else {}
    os.chdir(args.state_dir)
    reference = Path(__file__).resolve().parents[1] / "legacy_reference" / "reference"
    sys.path.insert(0, str(reference))
    import tkinter as tk
    from tkinter import ttk
    import ghost
    import audio_eq

    class NoopListener:
        def __init__(self, *args, **kwargs): pass
        def start(self): pass
        def stop(self): pass
        def join(self, *args, **kwargs): pass
        def is_alive(self): return False

    # Equal to Rust --no-hooks. Mouse position reads and normal Tk timers remain.
    ghost.keyboard.Listener = ghost.mouse.Listener = NoopListener
    audio_eq.AudioEQ.SETTINGS_FILE = str(args.state_dir / "musicapp_eq.json")
    import audio
    player = audio.AudioPlayer
    player.set_volume(0.0, set_directly=True)
    player._gaming_mode = bool(preferences.get("gaming_mode", True))

    class Recommendations:
        def analyze_top_songs(self, **kwargs): return []
        def analyze_top_artists(self, **kwargs): return []
        def suggest_search_terms_str(self): return ""
        def log_search(self, *args): pass

    class PlayerView:
        recommend = Recommendations()
        youtube_download_permanently = bool(preferences.get("youtube_download_permanently", False))
        do_youtube_search = bool(preferences.get("do_youtube_search", True))
        radio_client = None
        echo = None
        def get_gaming_mode(self): return player._gaming_mode
        def toggle_gaming_mode(self, enabled): player._gaming_mode = bool(enabled)
        def get_bands(self): return player.eq.get_gains()
        def set_band(self, frequency, value): player.eq.set_gain(frequency, value)
        def get_volume(self): return player.get_volume()
        def get_search_term(self, term): return []
        def accepting_radio_eq(self): return False

    root = tk.Tk()
    root.withdraw()
    overlay = ghost.GhostOverlay(root, overlay_settings_file=args.state_dir / "overlay.json")
    overlay.MusicPlayer = PlayerView()
    overlay.set_text("Benchmark Artist - Benchmark Track")
    overlay.set_duration(0, 60)
    root.update()

    scenario = args.benchmark
    if scenario.startswith("playback_") or scenario.startswith("paused_"):
        if scenario in ("playback_eq", "playback_echo"):
            player._gaming_mode = False
        if scenario == "playback_eq":
            player.eq.set_gains(dict(zip(audio_eq.AudioEQ.ISO_BANDS, [3, -2, 6, -7, 2, -3, 10, -4, 5, 8])))
        if scenario == "playback_echo":
            player.enable_echo(350, 0.35, 0.5)
        if not player.play(str(args.media)):
            raise RuntimeError("Python audio could not start")
        if scenario.startswith("paused_"):
            player.pause()
            overlay.set_text("Benchmark Artist - Benchmark Track -[Paused]-")
    if scenario.endswith("hidden"):
        overlay.close_overlay()
    if scenario == "controls":
        overlay.show_key_hints(force_state=True)
    if scenario == "search":
        overlay.show_search_overlay()

    loading = None
    if scenario == "progress":
        from ui_loading import LoadingHelper
        from ui_window import style_card
        window = tk.Toplevel(root)
        window.title("MusicApp")
        colors = overlay.theme.COLORS
        window.configure(bg=colors["window_bg"])
        body = tk.Frame(window, bg=colors["window_bg"], padx=26, pady=23)
        body.pack(fill="both", expand=True)
        heading = tk.Label(body, text="Downloading your music", bg=colors["window_bg"], fg=colors["accent"], font=("Segoe UI", 18, "bold"), anchor="w")
        heading.pack(fill="x")
        message = tk.StringVar(value="Benchmark: synthetic progress, no network")
        tk.Label(body, textvariable=message, bg=colors["window_bg"], fg=colors["text"], font=("Segoe UI", 11), wraplength=382, justify="left").pack(fill="x", pady=(14, 20))
        footer = tk.Frame(body, bg=colors["window_bg"])
        footer.pack(fill="x", side="bottom")
        tk.Button(footer, text="Stop").pack(side="right")
        style_card(window, heading)
        loading = LoadingHelper(window, body, heading, message, colors)
        window.geometry("440x420+100+100")

    started = time.monotonic()
    tick_count = 0
    def update():
        nonlocal tick_count
        tick_count += 1
        if scenario.startswith("playback_"):
            overlay.set_duration(player.get_pos(), player.get_duration())
        if loading:
            loading.set_progress(tick_count % 100, 100)
            loading.set_downloads([{"title": f"Benchmark song {i + 1}", "progress": ((tick_count + i * 17) % 100) / 100, "phase": "downloading"} for i in range(3)])
        if time.monotonic() - started >= args.seconds:
            root.quit()
            return
        root.after(100, update)
    update()
    root.update_idletasks()
    (args.state_dir / "ready").write_text("ready", encoding="utf-8")
    try:
        root.mainloop()
    finally:
        diagnostic = {"scenario": scenario, "position_seconds": player.get_pos(), "duration_seconds": player.get_duration(),
                      "is_playing": player.get_busy(), "is_paused": player._paused.is_set(),
                      "underflows": player._underflow_count, "last_error": None,
                      "gaming_mode": player._gaming_mode, "eq_gains": list(player.eq.get_gains().values()),
                      "echo": ({"delay_ms": player.echo.delay_ms, "feedback": player.echo.feedback, "wet": player.echo.wet} if player.echo else None),
                      "volume": player.get_volume()}
        (args.state_dir / "benchmark_audio.json").write_text(json.dumps(diagnostic), encoding="utf-8")
        player.close()
        overlay.close()
        root.destroy()


if __name__ == "__main__":
    main()
