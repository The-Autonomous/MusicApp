"""Visibility preferences and placement on real-world multi-monitor layouts."""
import ast
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from overlay_state import OverlayPreferences, clamp_position, valid_position


class PlacementTests(unittest.TestCase):
    def test_offscreen_position_returns_whole_player_to_work_area(self):
        self.assertEqual(clamp_position((50000, 50000), 300, 100, [(0, 0, 1920, 1040)]), (1620, 940))
        self.assertEqual(clamp_position((-50000, -50000), 300, 100, [(0, 0, 1920, 1040)]), (0, 0))

    def test_negative_coordinates_on_connected_secondary_monitor_are_valid(self):
        areas = [(0, 0, 1920, 1040), (-1920, 0, 0, 1040)]
        self.assertEqual(clamp_position((-1700, 100), 300, 100, areas), (-1700, 100))
        self.assertEqual(clamp_position((-1700, 100), 300, 100, areas[:1]), (0, 100))

    def test_window_growth_and_monitor_gaps_cannot_leave_player_partially_hidden(self):
        self.assertEqual(clamp_position((1700, 1000), 600, 300, [(0, 0, 1920, 1040)]), (1320, 740))
        areas = [(0, 0, 1920, 1040), (2200, 0, 4120, 1040)]
        self.assertEqual(clamp_position((2000, 100), 100, 100, areas), (1820, 100))

    def test_bad_coordinates_get_a_safe_default(self):
        for value in ([float('nan'), 2], [True, 2], [1], '50000,50000', [float('inf'), 2]):
            self.assertIsNone(valid_position(value))
        self.assertEqual(clamp_position(None, 300, 100, [(0, 0, 1920, 1040)]), (810, 20))


class PreferenceTests(unittest.TestCase):
    def test_hidden_and_disabled_state_and_position_survive_restart(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'overlay.json'
            preferences = OverlayPreferences(path)
            preferences.save(False, False, (-1500, 200))
            preferences.close()
            restored = OverlayPreferences(path)
            self.assertFalse(restored.visible)
            self.assertFalse(restored.player_enabled)
            self.assertEqual(restored.position, (-1500, 200))
            restored.close()

    def test_latest_rapid_change_is_the_one_saved(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'overlay.json'
            preferences = OverlayPreferences(path)
            for number in range(25):
                preferences.save(bool(number % 2), True, (number, 200))
            preferences.save(True, False, (300, 400))
            preferences.close()
            self.assertEqual(json.loads(path.read_text()), {'visible': True, 'player_enabled': False, 'position': [300, 400]})

    def test_corrupt_preferences_do_not_hide_or_break_player(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'overlay.json'
            path.write_text('{bad json')
            preferences = OverlayPreferences(path)
            self.assertTrue(preferences.visible)
            self.assertTrue(preferences.player_enabled)
            self.assertIsNone(preferences.position)
            preferences.close()


class ShortcutTests(unittest.TestCase):
    def test_visibility_hotkey_opens_even_if_audio_player_is_disabled(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'ghost.py').read_text(encoding='utf-8'))
        overlay = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'GhostOverlay')
        method = next(node for node in overlay.body if isinstance(node, ast.FunctionDef) and node.name == 'toggle_overlay')
        namespace = {}
        exec(compile(ast.Module(body=[method], type_ignores=[]), 'ghost.py', 'exec'), namespace)
        state = SimpleNamespace(playerState=False, window=None, open_overlay=Mock(), close_overlay=Mock())
        namespace['toggle_overlay'](state)
        state.open_overlay.assert_called_once()
        self.assertFalse(state.playerState)
        state.window = Mock()
        state.window.winfo_exists.return_value = True
        state.window.state.return_value = 'normal'
        namespace['toggle_overlay'](state)
        state.close_overlay.assert_called_once()


if __name__ == '__main__':
    unittest.main()
