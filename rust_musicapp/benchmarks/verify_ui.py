"""Development-only parity checks. Does not launch playback or global hooks."""
from pathlib import Path
import json
import sys
import time
import tkinter as tk
import unittest
from types import SimpleNamespace
from threading import Event
from unittest.mock import Mock, patch

PORT = Path(__file__).resolve().parents[1]
SOURCE = PORT / "legacy_reference" / "reference"
sys.path.insert(0, str(SOURCE))

from ghost import GhostOverlay, ThemeManager, RoundedCanvas
from ui_controls import build_controls_window


class NativePresentation(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.errors = []
        self.actions = []
        self.root.createcommand("rust_action", lambda *args: self.actions.append(args) or "")
        self.root.createcommand("bgerror", lambda msg: self.errors.append(msg))
        self.root.tk.call("source", "-encoding", "utf-8", str(PORT / "ui" / "app.tcl"))
        # Geometry comparisons explicitly represent a ready, loaded player.
        self.root.tk.call("set", "::music::overlay_allowed", 1)

    def tearDown(self):
        self.root.update()
        errors = self.errors[:]
        self.root.destroy()
        self.assertEqual(errors, [])

    def call(self, *args):
        result = self.root.tk.call(*args)
        self.root.update()
        return result

    def test_all_cards_and_progress_states(self):
        for command in [
            ("::music::set_display", "Artist - Test Song", 30, 180, ""),
            ("::music::show_overlay",),
            ("::music::controls",), ("destroy", ".controls"),
            ("::music::controls", 1), ("destroy", ".controls"),
            ("::music::search",),
            ("::music::search_submit", "A song"),
            ("::music::results", (("Local Song", "C:/file.mp3", "path"),
                                    ("Online Song", "https://example.invalid", "url"))),
            ("::music::search_nav", 1),
            ("::music::select_result",), ("::music::close_search",),
            ("::music::eq",),
            ("::music::eq_state", (0,) * 10, 50, 350, 0, 0, 0),
            ("::music::knob_drag", ".eq.card.grid.f31.k", 40, 15, 1),
            ("set", "::music::preset", "Warm & Smooth"),
            ("::music::apply_preset",),
            ("::music::volume_drag", 60), ("destroy", ".eq"),
            ("::music::youtube", 0),
            ("::music::youtube_state", "connecting", "Preparing music", 0, 0, 1, 0, ""),
            ("::music::youtube_state", "syncing", "3 of 8 songs ready", 1, 0, 1, 3, 8),
            ("::music::youtube_downloads", (("A song · 30%", .3), ("Preparing", ""))),
            ("::music::youtube_collapse",),
            ("::music::youtube_restore",),
            ("::music::youtube_options",), ("destroy", ".youtube_options"),
            ("::music::youtube_state", "complete", "Music ready", 1, 1, 0, 8, 8),
            ("::music::youtube_state", "error", "Check connection and retry", 0, 0, 0, 0, ""),
        ]:
            with self.subTest(command=command[0]):
                self.call(*command)
        self.assertIn(("search", "A song", "1", "0"), self.actions)
        self.assertTrue(any(a[0] == "eq_band" for a in self.actions))
        self.assertTrue(any(a[0] == "eq_preset" for a in self.actions))
        self.assertIn(("volume", "50"), self.actions)

    def test_hint_fades_resets_and_stops_drag_timer(self):
        self.call("::music::show_overlay")
        self.assertEqual(self.call(".overlay.c", "itemcget", "hint", "-state"), "hidden")
        self.call("::music::overlay_drag_begin")
        def pump(seconds):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                self.root.update()
                time.sleep(.005)
        pump(.3)
        self.assertEqual(float(self.call("set", "::music::hint_opacity")), 1.0)
        self.call("::music::overlay_drag_end")
        first_timer = self.call("set", "::music::hint_hide_timer")
        self.assertEqual(str(self.call("set", "::music::drag_pulse_timer")), "")
        self.call("::music::overlay_drag_begin")
        with self.assertRaises(tk.TclError):
            self.call("after", "info", first_timer)
        self.call("::music::overlay_drag_end")
        pump(9.7)
        self.assertEqual(self.call(".overlay.c", "itemcget", "hint", "-state"), "normal")
        pump(.7)
        self.assertEqual(self.call(".overlay.c", "itemcget", "hint", "-state"), "hidden")
        self.assertEqual(str(self.call("set", "::music::hint_fade_timer")), "")
        self.assertEqual(str(self.call("set", "::music::drag_pulse_timer")), "")

    def test_readiness_during_search_and_authentication(self):
        self.call("::music::overlay_available", 0)
        self.call("::music::show_overlay")
        self.assertEqual(int(self.call("winfo", "exists", ".overlay")), 0)
        self.call("::music::search")
        self.call("::music::overlay_available", 1)
        self.assertEqual(int(self.call("winfo", "exists", ".overlay")), 0)
        self.call("::music::close_search")
        self.assertEqual(self.call("wm", "state", ".overlay"), "normal")
        self.call("::music::controls")
        self.call("::music::authentication_ui", 1)
        self.call("::music::youtube", 0)
        for window in (".overlay", ".controls", ".youtube"):
            self.assertEqual(self.call("wm", "state", window), "withdrawn")
        self.call("::music::overlay_available", 0)
        self.call("::music::authentication_ui", 0)
        self.assertEqual(self.call("wm", "state", ".overlay"), "withdrawn")
        self.assertEqual(self.call("wm", "state", ".controls"), "normal")
        self.call("::music::overlay_available", 1)
        self.assertEqual(self.call("wm", "state", ".overlay"), "normal")

    def collect_buttons(self, window):
        records = {}
        tkapp = self.root.tk
        def walk(widget):
            for child in tkapp.splitlist(tkapp.call("winfo", "children", widget)):
                if tkapp.call("winfo", "class", child) == "Button":
                    text = tkapp.call(child, "cget", "-text")
                    records.setdefault(text, []).append(tuple(int(tkapp.call("winfo", dim, child))
                                                              for dim in ("width", "height", "reqwidth", "reqheight")))
                walk(child)
        walk(window)
        return records

    def test_controls_match_original_widget_sizes(self):
        theme = ThemeManager()
        prototype = GhostOverlay.__new__(GhostOverlay)
        prototype._define_default_key_actions()
        for scaling in (1.3333, 1.6667, 2.0):
            self.call("tk", "scaling", scaling)
            for customize in (False, True):
                with self.subTest(scaling=scaling, customize=customize):
                    old, _ = build_controls_window(self.root, theme.COLORS,
                        prototype.key_actions, run_action=lambda _: None,
                        on_edit=lambda _: None, on_alt_toggle=lambda _: None,
                        on_reset=lambda: None, on_close=lambda: None, customize=customize)
                    self.call("::music::controls", int(customize))
                    old_buttons = self.collect_buttons(str(old))
                    new_buttons = self.collect_buttons(".controls")
                    self.assertEqual(old_buttons, new_buttons)
                    self.assertEqual((old.winfo_width(), old.winfo_height()),
                        (int(self.call("winfo", "width", ".controls")),
                         int(self.call("winfo", "height", ".controls"))))
                    old.destroy(); self.call("destroy", ".controls")

    def test_overlay_exact_canvas_layout(self):
        theme = ThemeManager()
        old = GhostOverlay.__new__(GhostOverlay)
        old.window = tk.Toplevel(self.root)
        old.canvas = RoundedCanvas(old.window, bg="#ff00ff", highlightthickness=0)
        old.canvas.pack(fill="both", expand=True)
        old.canvas_items = {}
        old.theme = theme
        old.bg_color = "#000000"
        old.overlay_text_padding = old.overlay_corner_radius = 15
        old.running_lyrics = old.display_lyrics = True
        old._last_position = None
        old._layout_key = None
        old._place_overlay = lambda position, width, height: old.window.geometry(f"{int(width)}x{int(height)}")
        self.call("::music::show_overlay")
        for title, lyrics in [
            ("", ""),
            ("A Song", ""),
            ("A Long Song Title That Must Wrap At Thirty Five Characters", "These are the first lyrics"),
            ("Unicode ♪ 日本語 title", "These lyrics have enough words to wrap into two lines to compare measurements"),
            ("A Very Long Title Which Has Such A Large Number Of Words That More Than Two Lines Would Normally Be Required", ""),
        ]:
            with self.subTest(title=title):
                old.player_metric = {"player_text": title, "player_duration": "0:30 / 3:00", "player_lyrics": lyrics}
                old.update_display(init_draw=not bool(old.canvas_items))
                self.call("::music::set_display", title, 30, 180, lyrics)
                for old_name, new_name in [("player_text", "title"), ("duration_text", "duration"),
                                            ("lyrics_text", "lyric"), ("controls_hint", "hint")]:
                    item = old.canvas_items[old_name]
                    self.assertEqual(old.canvas.itemcget(item, "text"),
                        self.call(".overlay.c", "itemcget", new_name, "-text"))
                    self.assertEqual(old.canvas.coords(item),
                        [float(x) for x in self.call(".overlay.c", "coords", new_name)])
                self.assertEqual((old.window.winfo_width(), old.window.winfo_height()),
                    (int(self.call("winfo", "width", ".overlay")),
                     int(self.call("winfo", "height", ".overlay"))))
                expected = old.canvas.coords(old.canvas_items["bg"])
                actual = [float(x) for x in self.call(".overlay.c", "coords", "bg")]
                self.assertEqual(len(expected), len(actual))
                for a, b in zip(expected, actual): self.assertAlmostEqual(a, b, places=7)
        old.window.destroy()

    def test_eq_knob_geometry_matches_original(self):
        theme = ThemeManager()
        theme.apply_ttk_styles(self.root)
        prototype = GhostOverlay.__new__(GhostOverlay)
        prototype.root = self.root
        prototype.theme = theme
        prototype.MusicPlayer = SimpleNamespace(
            get_bands=lambda: dict.fromkeys((31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000), 0),
            get_volume=lambda: .5, get_gaming_mode=lambda: False,
            set_volume=lambda *args: None, radio_client=True,
            accepting_radio_eq=lambda: False)
        prototype.show_eq_overlay()
        self.call("::music::eq")
        original = prototype._eq_window
        self.assertEqual((original.winfo_width(), original.winfo_height()),
            (int(self.call("winfo", "width", ".eq")), int(self.call("winfo", "height", ".eq"))))
        for freq, knob in prototype.eq_knobs.items():
            new = f".eq.card.grid.f{freq}.k"
            self.assertEqual((knob.winfo_width(), knob.winfo_height()),
                (int(self.call("winfo", "width", new)), int(self.call("winfo", "height", new))))
            self.assertEqual(knob.coords(knob._pointer),
                [float(x) for x in self.call(new, "coords", "pointer")])
            self.assertEqual(knob.itemcget(knob._label, "text"), self.call(new, "itemcget", "value", "-text"))
            for gain in (-12, -3, 0, 3, 12):
                knob.gain = gain; knob._draw()
                self.call("dict", "set", f"::music::knob({new})", "value", gain)
                self.call("::music::knob_draw", new)
                for a, b in zip(knob.coords(knob._pointer), self.call(new, "coords", "pointer")):
                    self.assertAlmostEqual(a, float(b), places=7)
        self.call("::music::eq_state", (0.0,) * 10, 50, 350, 0, 0, 0)
        self.assertEqual(self.call("set", "::music::preset"), "Flat")
        original.destroy()

    def test_search_default_geometry_matches_original(self):
        theme = ThemeManager()
        theme.apply_ttk_styles(self.root)
        old = GhostOverlay.__new__(GhostOverlay)
        old.root, old.theme = self.root, theme
        old.display_radio = old._is_searching = old.is_spinning_downloading = False
        old.window = old.search_overlay = old._search_after_id = None
        old.show_key_hints = lambda **kwargs: None
        old.TitleCleaner = SimpleNamespace(clean=lambda text: text)
        old.MusicPlayer = SimpleNamespace(
            youtube_download_permanently=False, do_youtube_search=True,
            recommend=SimpleNamespace(analyze_top_songs=lambda **kwargs: [],
                                      analyze_top_artists=lambda **kwargs: [],
                                      suggest_search_terms_str=lambda: ""))
        old.show_search_overlay()
        self.call("::music::search")
        def geometry(window):
            records = []
            def walk(widget):
                for child in self.root.tk.splitlist(self.call("winfo", "children", widget)):
                    kind = self.call("winfo", "class", child)
                    if kind in ("TEntry", "TCheckbutton", "Listbox", "TScrollbar"):
                        records.append((kind, int(self.call("winfo", "width", child)),
                                       int(self.call("winfo", "height", child)),
                                       int(self.call("winfo", "rootx", child))-int(self.call("winfo", "rootx", window)),
                                       int(self.call("winfo", "rooty", child))-int(self.call("winfo", "rooty", window))))
                    walk(child)
            walk(str(window))
            return records
        self.assertEqual(geometry(old.search_overlay), geometry(".search"))
        old.close_search_overlay()

    def test_youtube_card_dimensions_and_controls_match_original(self):
        from youtube_sync import YouTubeSync
        theme = ThemeManager()
        theme.apply_ttk_styles(self.root)
        ready = Event(); ready.set()
        player = SimpleNamespace(auto_gaming=True, ytHandle=Mock(), _playback_ready=ready)
        player.allow_playback = player.set_hosting = player.sync_playlists = player.add_downloaded_song = Mock()
        with patch.object(YouTubeSync, "_read", return_value={}):
            sync = YouTubeSync(player)
        sync.start = Mock(return_value=False)
        overlay = SimpleNamespace(root=self.root, theme=theme, _closing=False,
                                  post_ui=lambda callback: callback())
        for phase, connected, busy, done in [("idle", False, False, False),
                                            ("connecting", False, True, False),
                                            ("syncing", True, True, False),
                                            ("complete", True, False, True),
                                            ("error", False, False, False)]:
            with self.subTest(phase=phase):
                sync.phase, sync.connected, sync.ready = phase, connected, done
                sync.message = "This is a test status message"
                (sync._busy.set if busy else sync._busy.clear)()
                sync.show(overlay)
                self.call("::music::youtube", 0)
                self.call("::music::youtube_state", phase, sync.message, int(connected), int(done), int(busy), 0, "")
                try:
                    self.assertEqual(self.collect_buttons(str(sync.window)), self.collect_buttons(".youtube"))
                    self.assertEqual((sync.window.winfo_width(), sync.window.winfo_height()),
                        (int(self.call("winfo", "width", ".youtube")), int(self.call("winfo", "height", ".youtube"))))
                finally:
                    sync.window.destroy(); self.call("destroy", ".youtube")


if __name__ == "__main__":
    unittest.main(verbosity=2)
