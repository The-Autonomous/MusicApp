"""Real Tk widget layouts; no desktop capture, hooks, accounts or audio."""
import os
import gc
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import Mock, patch

from ghost import GhostOverlay, ThemeManager, WS_EX_NOACTIVATE
from ui_controls import ACTION_LABELS, build_controls_window
import youtube_sync


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


@unittest.skipUnless(os.name == "nt", "Windows Tk layout checks")
class WindowLayouts(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.theme = ThemeManager()
        self.theme.apply_ttk_styles(self.root)
        # The method only builds descriptions/bound callbacks. No Ghost constructor/hooks.
        prototype = GhostOverlay.__new__(GhostOverlay)
        prototype._define_default_key_actions()
        self.actions = prototype.key_actions

    def tearDown(self):
        root = self.root
        root.destroy()
        # Font/widget callback cycles can otherwise keep a destroyed Tcl
        # interpreter alive until a later download worker happens to trigger
        # collection. Tcl interpreter finalizers must run on their Tk thread.
        self.theme = self.actions = self.root = None
        gc.collect()
        self.assertEqual(self.errors, [])

    def inside(self, window, widget):
        self.assertTrue(widget.winfo_ismapped(), widget)
        x = widget.winfo_rootx() - window.winfo_rootx()
        y = widget.winfo_rooty() - window.winfo_rooty()
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + widget.winfo_width(), window.winfo_width() + 1, widget.cget("text"))
        self.assertLessEqual(y + widget.winfo_height(), window.winfo_height() + 1, widget.cget("text"))

    def controls(self, customize=False):
        callbacks = {name: Mock() for name in ("run_action", "on_edit", "on_alt_toggle", "on_reset")}
        popup, status = build_controls_window(self.root, self.theme.COLORS, self.actions,
                                              on_close=lambda *args: None,
                                              customize=customize, **callbacks)
        self.root.update()
        self.assertTrue(popup.overrideredirect())
        return popup, callbacks

    def test_quick_controls_are_visible_at_normal_and_larger_text_sizes(self):
        for scale in (1.3333, 1.6667, 2.0):
            with self.subTest(scale=scale):
                self.root.tk.call("tk", "scaling", scale)
                popup, callbacks = self.controls()
                buttons = [widget for widget in descendants(popup) if isinstance(widget, tk.Button)]
                for button in buttons:
                    self.inside(popup, button)
                    self.assertGreaterEqual(button.winfo_width(), button.winfo_reqwidth() - 2, button.cget("text"))
                labels = [widget.cget("text").split("\n")[0] for widget in buttons]
                self.assertIn(ACTION_LABELS["pause_play"], labels)
                next_button = next(button for button in buttons if button.cget("text").startswith("Next song\n"))
                next_button.invoke()
                self.assertEqual(callbacks["run_action"].call_args.args[0]["id"], "skip_next")
                popup.destroy()

    def test_editor_header_footer_and_scroll_area_fit(self):
        popup, callbacks = self.controls(customize=True)
        buttons = [widget for widget in descendants(popup) if isinstance(widget, tk.Button)]
        for label in ("Quick controls", "Shortcuts", "Done"):
            self.inside(popup, next(widget for widget in buttons if widget.cget("text") == label))
        canvas = next(widget for widget in descendants(popup) if isinstance(widget, tk.Canvas))
        self.assertGreater(canvas.winfo_height(), 100)
        self.assertTrue(canvas.cget("scrollregion"))
        change = next(widget for widget in buttons if widget.cget("text") == "Change")
        change.invoke()
        callbacks["on_edit"].assert_called_once()

    def test_readonly_dropdown_uses_dark_theme(self):
        style = ttk.Style(self.root)
        self.assertEqual(style.lookup("TCombobox", "fieldbackground", ("readonly",)), self.theme.COLORS["entry_bg"])
        self.assertEqual(style.lookup("TCombobox", "foreground", ("readonly",)), self.theme.COLORS["text"])

    def sync_card(self, phase, connected=False, remembered=True, busy=False):
        player = SimpleNamespace(auto_gaming=True, ytHandle=SimpleNamespace(configure=lambda **kwargs: None),
                                 _playback_ready=Event(), allow_playback=Mock(), set_hosting=Mock(), sync_playlists=Mock())
        player.add_downloaded_song = Mock()
        with patch.object(youtube_sync.YouTubeSync, "_read", return_value={}):
            sync = youtube_sync.YouTubeSync(player)
        sync.phase, sync.connected, sync.session_remembered = phase, connected, remembered
        sync.message = "YouTube couldn't connect. Check your connection and retry." if phase == "error" else "Saving your likes: 123 of 4567"
        if busy:
            sync._busy.set()
        sync.start = Mock(return_value=False)
        overlay = SimpleNamespace(root=self.root, theme=self.theme, _closing=False,
                                  post_ui=lambda callback: callback())
        sync.show(overlay, startup=True)
        self.root.update()
        self.assertTrue(sync.window.overrideredirect())
        return sync, overlay

    def test_minimize_during_startup_never_releases_playback(self):
        sync, overlay = self.sync_card("connecting", busy=True)
        self.inside(sync.window, sync.loading_helper.minus)
        sync.loading_helper.minus.invoke()
        self.root.update()
        self.assertTrue(sync.loading_helper.collapsed)
        self.inside(sync.window, sync.loading_helper.summary)
        sync.player.allow_playback.assert_not_called()
        self.assertFalse(sync.cancel.is_set())
        sync.loading_helper.restore()
        self.root.update()
        self.assertFalse(sync.loading_helper.collapsed)

    def test_minimized_helper_receives_live_download_progress(self):
        sync, overlay = self.sync_card("syncing", connected=True, busy=True)
        sync.loading_helper.collapse()
        sync.total, sync.saved = 8, 3
        sync.message = "3 of 8 songs ready\nDownloading a song with a longer title…"
        sync._observer(sync._status_text())
        self.root.update()
        self.assertTrue(sync.loading_helper.collapsed)
        self.inside(sync.window, sync.loading_helper.summary)
        self.assertEqual(sync.loading_helper.summary.cget("text"), sync.message)
        self.assertEqual(sync.loading_helper.progress.cget("value"), 3)
        self.assertEqual(sync.loading_helper.progress.cget("maximum"), 8)
        sync.player.allow_playback.assert_not_called()

    def test_preparation_failure_is_borderless_and_can_close(self):
        import startup
        observed = []

        def close_failed_card():
            windows = [widget for widget in self.root.winfo_children() if isinstance(widget, tk.Toplevel)]
            if not windows:
                self.root.after(10, close_failed_card)
                return
            window = windows[0]
            buttons = [widget for widget in descendants(window) if isinstance(widget, tk.Button)]
            close = next((widget for widget in buttons if widget.cget("text") == "Close"), None)
            if close is None:
                self.root.after(10, close_failed_card)
                return
            observed.append(window.overrideredirect())
            for widget in buttons:
                if widget.winfo_ismapped():
                    self.inside(window, widget)
            close.invoke()

        self.root.after(10, close_failed_card)
        with patch.object(startup, "missing_runtime_modules", return_value=("sounddevice",)), \
                patch.object(startup, "prepare_runtime", side_effect=RuntimeError("Access is denied")):
            self.assertFalse(startup.ensure_runtime(self.root))
        self.assertEqual(observed, [True])

    def test_signin_failure_and_memory_only_notices_fit(self):
        for phase, connected, remembered, busy in (("error", False, True, False),
                                                  ("connecting", False, True, True),
                                                  ("syncing", True, False, True)):
            with self.subTest(phase=phase):
                sync, overlay = self.sync_card(phase, connected, remembered, busy)
                for widget in descendants(sync.loading_helper.body):
                    if isinstance(widget, (tk.Button, tk.Label)):
                        self.inside(sync.window, widget)
                sync.window.destroy()

    def test_reopened_card_receives_progress_and_completion_without_polling(self):
        sync, overlay = self.sync_card("syncing", True, True, True)
        sync.window.destroy()
        sync.show(overlay)
        self.root.update()
        sync.message = "4567 likes found · 12 new songs saved"
        sync._busy.clear()
        sync._observer(sync._status_text())
        self.root.update()
        texts = [widget.cget("text") for widget in descendants(sync.window) if isinstance(widget, tk.Button)]
        self.assertIn("Sync music", texts)
        self.assertNotIn("Stop sync", texts)
        labels = [widget.cget("text") for widget in descendants(sync.window) if isinstance(widget, tk.Label)]
        self.assertIn(sync.message, labels)

    def test_passive_player_has_noactivate_style(self):
        import ctypes
        popup = tk.Toplevel(self.root)
        self.root.update()
        prototype = GhostOverlay.__new__(GhostOverlay)
        prototype.window = popup
        prototype.toggle_overlay_clickthrough(True)
        handle = ctypes.windll.user32.GetParent(popup.winfo_id()) or popup.winfo_id()
        style = ctypes.windll.user32.GetWindowLongPtrW(handle, -20)
        self.assertTrue(style & WS_EX_NOACTIVATE)
