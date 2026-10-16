"""Overlay lifecycle/timers without Tk windows, accounts, audio devices or hooks."""
import ast
from pathlib import Path
from queue import Queue
import sys
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from overlay_state import clamp_position


class Clock:
    def __init__(self):
        self.now = 0.0
        self.tasks = {}
        self.serial = 0

    def after(self, milliseconds, callback, *args):
        self.serial += 1
        self.tasks[self.serial] = (self.now + milliseconds / 1000, callback, args)
        return self.serial

    def after_cancel(self, identity):
        self.tasks.pop(identity, None)

    def advance(self, milliseconds):
        target = self.now + milliseconds / 1000
        while self.tasks:
            identity = min(self.tasks, key=lambda key: self.tasks[key][0])
            due, callback, args = self.tasks[identity]
            if due > target:
                break
            del self.tasks[identity]
            self.now = due
            callback(*args)
        self.now = target


class Window:
    def __init__(self):
        self.x, self.y, self.width, self.height = 20, 30, 280, 110
        self.visibility = 'normal'
    def winfo_exists(self): return True
    def winfo_x(self): return self.x
    def winfo_y(self): return self.y
    def winfo_width(self): return self.width
    def winfo_height(self): return self.height
    def state(self): return self.visibility
    def withdraw(self): self.visibility = 'withdrawn'


class OverlayLifecycle(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'ghost.py').read_text(encoding='utf-8'))
        original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'GhostOverlay')
        names = {'_player_has_loaded_track', 'refresh_overlay_visibility', 'display_overlay',
                 '_set_hint_opacity', '_fade_controls_hint', '_show_controls_hint',
                 '_schedule_controls_hint_hide', '_hide_controls_hint', '_cancel_overlay_hint', '_on_overlay_mouse_button',
                 '_begin_overlay_drag', '_overlay_drag_tick', '_finish_overlay_drag',
                 'parse_mouse_over_overlay', '_place_overlay', '_ui_tick', 'post_ui'}
        self.clock = Clock()
        self.monitors = Mock(return_value=[(-1920, 0, 0, 1040), (0, 0, 1920, 1040)])
        self.moves = []
        def place(window, position, width=None, height=None):
            window.x, window.y = position
            if width is not None: window.width = width
            if height is not None: window.height = height
            self.moves.append(position)
        namespace = {'sys': sys, 'monotonic': lambda: self.clock.now,
                     'monitor_work_areas': self.monitors, 'clamp_position': clamp_position,
                     'place_window': place, 'tk': SimpleNamespace(TclError=RuntimeError),
                     'Empty': __import__('queue').Empty, 'Full': __import__('queue').Full, 'll': Mock()}
        cls = ast.ClassDef(name='Overlay', bases=[], keywords=[], decorator_list=[],
                           body=[node for node in original.body if isinstance(node, ast.FunctionDef) and node.name in names])
        exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), 'ghost.py', 'exec'), namespace)
        self.overlay = state = namespace['Overlay']()
        state.root = self.clock
        state.window = Window()
        state.canvas = SimpleNamespace(itemconfig=Mock())
        state.canvas_items = {'controls_hint': 1}
        state.theme = SimpleNamespace(COLORS={'text_dark': '#aaaaaa'})
        state.bg_color = '#000000'
        state._closing = state._authentication_active = state._overlay_dragging = False
        state._overlay_visible = True
        state._drag_after_id = state._drag_work_areas = None
        state._hint_animation_after_id = state._hint_hide_after_id = None
        state._hint_opacity = 0.0
        state._last_position = (20, 30)
        state._save_overlay_state = Mock()
        state.toggle_overlay_clickthrough = Mock()
        state.clickThroughState = True
        state.open_overlay = Mock()
        state._ui_queue = Queue()
        state._display_dirty = False
        state._screen_check_due = 100
        state.mouseEvents = SimpleNamespace(mouse_pos=lambda: self.cursor,
            is_right_mouse_down=lambda: self.right_down, update_window=Mock(),
            was_press_in_window=lambda: False)
        self.cursor = (30, 40)
        self.right_down = False
        ready, active = Event(), Event()
        ready.set(); active.set()
        state.MusicPlayer = SimpleNamespace(_playback_ready=ready, current_song={'path': 'song'},
            AudioPlayer=SimpleNamespace(_play_event=active), pause_event=Event())

    def test_startup_requires_ready_and_successfully_loaded_track(self):
        state = self.overlay
        state.MusicPlayer._playback_ready.clear()
        state.refresh_overlay_visibility()
        self.assertEqual(state.window.state(), 'withdrawn')
        state.MusicPlayer._playback_ready.set()
        state.MusicPlayer.AudioPlayer._play_event.clear()
        state.refresh_overlay_visibility()
        state.open_overlay.assert_not_called()
        state.MusicPlayer.AudioPlayer._play_event.set()
        state.MusicPlayer.pause_event.set()
        state.refresh_overlay_visibility()
        state.open_overlay.assert_called_once_with(remember=False)
        self.assertTrue(state._overlay_visible)
        state._save_overlay_state.assert_not_called()

    def test_auth_and_user_hidden_preferences_do_not_override_each_other(self):
        state = self.overlay
        state._authentication_active = True
        state.refresh_overlay_visibility()
        self.assertEqual(state.window.state(), 'withdrawn')
        self.assertTrue(state._overlay_visible)
        state._authentication_active = False
        state._overlay_visible = False
        state.refresh_overlay_visibility()
        state.open_overlay.assert_not_called()
        state._save_overlay_state.assert_not_called()

    def test_ready_without_song_stays_hidden_and_radio_needs_playing_identity(self):
        state = self.overlay
        state.MusicPlayer.current_song = None
        self.assertFalse(state._player_has_loaded_track())
        state.MusicPlayer.current_player_mode = Event()
        state.MusicPlayer.current_player_mode.set()
        state.MusicPlayer.radio_client = SimpleNamespace(_playing_track_id=None)
        self.assertFalse(state._player_has_loaded_track())
        state.MusicPlayer.radio_client._playing_track_id = 'radio-track'
        self.assertTrue(state._player_has_loaded_track())

    def test_release_applies_final_pointer_position_before_saving(self):
        state = self.overlay
        self.right_down = True
        state._on_overlay_mouse_button(30, 40, True)
        self.right_down = False
        state._on_overlay_mouse_button(200, 250, False)
        self.assertEqual((state.window.x, state.window.y), (190, 240))
        self.assertFalse(state._overlay_dragging)
        state._save_overlay_state.assert_called()

    def test_search_temporarily_suppresses_auto_reopening(self):
        self.overlay.search_overlay = SimpleNamespace(winfo_exists=lambda: True)
        self.overlay.refresh_overlay_visibility()
        self.assertEqual(self.overlay.window.state(), 'withdrawn')
        self.overlay.open_overlay.assert_not_called()
        self.assertTrue(self.overlay._overlay_visible)

    def test_hint_fades_in_then_out_without_permanent_animation(self):
        state = self.overlay
        state._show_controls_hint()
        self.assertEqual(state._hint_opacity, 0)
        self.clock.advance(224)
        self.assertEqual(state._hint_opacity, 1)
        self.assertIsNone(state._hint_animation_after_id)
        self.assertEqual(self.clock.tasks, {})
        state._schedule_controls_hint_hide()
        self.assertEqual(len(self.clock.tasks), 1)
        self.clock.advance(10224)
        self.assertEqual(state._hint_opacity, 0)
        self.assertEqual(self.clock.tasks, {})
        self.assertEqual(state.canvas.itemconfig.call_args.kwargs['state'], 'hidden')

    def test_next_drag_resets_hint_timeout_and_close_cancels_timers(self):
        state = self.overlay
        state._show_controls_hint(); self.clock.advance(224)
        state._schedule_controls_hint_hide(); self.clock.advance(5000)
        first = state._hint_hide_after_id
        state._show_controls_hint()
        self.assertNotIn(first, self.clock.tasks)
        self.clock.advance(12000)
        self.assertEqual(state._hint_opacity, 1)
        self.assertEqual(self.clock.tasks, {})
        state._schedule_controls_hint_hide()
        self.clock.advance(5000)
        state._cancel_overlay_hint()
        self.assertEqual(self.clock.tasks, {})
        self.assertEqual(state._hint_opacity, 0)

    def test_long_drag_keeps_hint_until_ten_seconds_after_release(self):
        state = self.overlay
        self.right_down = True
        state._on_overlay_mouse_button(30, 40, True)
        self.clock.advance(12000)
        self.assertEqual(state._hint_opacity, 1)
        self.assertIsNone(state._hint_hide_after_id)
        self.right_down = False
        state._on_overlay_mouse_button(30, 40, False)
        self.clock.advance(9900)
        self.assertEqual(state._hint_opacity, 1)
        self.clock.advance(400)
        self.assertEqual(state._hint_opacity, 0)
        self.assertEqual(self.clock.tasks, {})

    def test_auth_hiding_cancels_drag_hint_even_if_service_withdrew_first(self):
        state = self.overlay
        self.right_down = True
        state._on_overlay_mouse_button(30, 40, True)
        self.clock.advance(224)
        state.window.withdraw()
        state._authentication_active = True
        state.refresh_overlay_visibility()
        self.assertEqual(state._hint_opacity, 0)
        self.assertEqual(self.clock.tasks, {})
        self.assertFalse(state._overlay_dragging)
        self.assertTrue(state._overlay_visible)

    def test_drag_ticks_only_during_drag_and_reuses_monitor_snapshot(self):
        state = self.overlay
        self.right_down = True
        state._on_overlay_mouse_button(30, 40, True)
        self.monitors.assert_called_once()
        self.cursor = (160, 190)
        self.clock.advance(48)
        self.assertEqual((state.window.x, state.window.y), (150, 180))
        self.monitors.assert_called_once()
        self.assertTrue(state._overlay_dragging)
        self.right_down = False
        state._on_overlay_mouse_button(160, 190, False)
        self.assertFalse(state._overlay_dragging)
        self.assertIsNone(state._drag_after_id)
        state.toggle_overlay_clickthrough.assert_called_with(True)
        self.assertEqual(self.monitors.call_count, 2)
        count = len(self.moves)
        self.clock.advance(100)
        self.assertEqual(len(self.moves), count)

    def test_right_click_outside_does_not_start_drag_or_hint(self):
        self.overlay._on_overlay_mouse_button(1000, 1000, True)
        self.assertFalse(self.overlay._overlay_dragging)
        self.assertEqual(self.clock.tasks, {})
        self.monitors.assert_not_called()

    def test_idle_ui_does_not_poll_cursor_or_create_drag_animation(self):
        state = self.overlay
        state.mouseEvents.mouse_pos = Mock()
        state._ui_tick()
        state.mouseEvents.mouse_pos.assert_not_called()
        self.assertIsNone(state._drag_after_id)
        self.assertEqual(len(self.clock.tasks), 1)


if __name__ == '__main__':
    unittest.main()
