import ast
from pathlib import Path
from types import SimpleNamespace
import unittest

from ui_controls import conflicting_action, effective_chord, replacement_chord, shortcut_label


def action(action_id, keys, alt_needed=True):
    return {"id": action_id, "required": keys, "alt_needed": alt_needed}


class ControlsTests(unittest.TestCase):
    def test_remapping_preserves_right_alt(self):
        self.assertEqual(replacement_chord(action("eq", ["right alt", "\\"]), "p"), ["right alt", "p"])

    def test_remapping_preserves_left_alt(self):
        self.assertEqual(replacement_chord(action("pause", ["alt", ";"]), "p"), ["alt", "p"])

    def test_alt_optional_label_matches_actual_binding(self):
        item = action("pause", ["alt", "p"], alt_needed=False)
        self.assertEqual(effective_chord(item), ("p",))
        self.assertEqual(shortcut_label(item), "P")

    def test_right_alt_is_still_required_when_left_alt_is_optional(self):
        item = action("eq", ["right alt", "p"], alt_needed=False)
        self.assertEqual(shortcut_label(item), "Right Alt + P")

    def test_controls_chord_has_recognizable_label(self):
        self.assertEqual(shortcut_label(action("help", ["alt", "right alt"])), "Both Alt keys")

    def test_conflicts_ignore_key_order(self):
        saved = action("next", ["p", "alt"])
        self.assertIs(conflicting_action([saved], action("pause", ["alt", "p"]), "pause"), saved)

    def test_conflicts_account_for_optional_alt(self):
        saved = action("next", ["alt", "p"], alt_needed=False)
        self.assertIs(conflicting_action([saved], action("pause", ["p"]), "pause"), saved)

    def test_left_and_right_alt_are_distinct(self):
        saved = action("next", ["right alt", "p"])
        self.assertIsNone(conflicting_action([saved], action("pause", ["alt", "p"]), "pause"))

    def test_current_action_is_excluded_from_conflicts(self):
        item = action("pause", ["alt", "p"])
        self.assertIsNone(conflicting_action([item], item, "pause"))


class KeyMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Extract this pure method without importing keyboard hooks, audio, or the app.
        source = Path(__file__).resolve().parents[1] / "ghost.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        overlay = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "GhostOverlay")
        method = next(node for node in overlay.body if isinstance(node, ast.FunctionDef) and node.name == "_rebuild_key_maps")
        namespace = {}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)
        cls.rebuild = staticmethod(namespace["_rebuild_key_maps"])

    def test_forbidden_keys_update_after_another_shortcut_is_remapped(self):
        overlay_action = dict(action("overlay", ["alt", "shift"]), forbidden=["ALL"])
        pause = action("pause", ["alt", ";"])
        state = SimpleNamespace(key_actions=[overlay_action, pause])
        self.rebuild(state)
        self.assertIn(";", overlay_action["forbidden"])
        pause["required"] = ["alt", "p"]
        self.rebuild(state)
        self.assertIn("p", overlay_action["forbidden"])
        self.assertNotIn(";", overlay_action["forbidden"])
        self.assertNotIn(";", state.keys_pressed)

    def test_overlay_remapping_does_not_forbid_its_new_modifier(self):
        overlay_action = dict(action("overlay", ["alt", "shift"]), forbidden=["ALL"])
        state = SimpleNamespace(key_actions=[overlay_action, action("eq", ["right alt", "\\"])])
        self.rebuild(state)
        overlay_action["required"] = ["right alt", "shift"]
        self.rebuild(state)
        self.assertNotIn("right alt", overlay_action["forbidden"])
        self.assertNotIn("shift", overlay_action["forbidden"])
        self.assertIn("\\", overlay_action["forbidden"])


if __name__ == "__main__":
    unittest.main()
