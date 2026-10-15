"""Exercise the real overlay drawing methods with an in-memory canvas."""
import ast
from math import cos, pi, sin
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]


class Canvas:
    def __init__(self):
        self.items = {}
        self.next_id = 0
        self.lowered = None

    def create_polygon(self, points, **options):
        self.next_id += 1
        self.items[self.next_id] = dict(options, points=points)
        return self.next_id

    def create_text(self, *args, **options):
        self.next_id += 1
        self.items[self.next_id] = options
        return self.next_id

    def delete(self, identity):
        self.items.pop(identity, None)

    def itemconfig(self, identity, **options):
        self.items[identity].update(options)

    def coords(self, identity, *position):
        self.items[identity]['position'] = position

    def tag_lower(self, identity):
        self.lowered = identity


class OverlayRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((ROOT / 'ghost.py').read_text(encoding='utf-8'))
        methods = {'_create_canvas_items_if_needed', 'update_display', 'wrap_text'}
        overlay = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'GhostOverlay')
        rounded = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'RoundedCanvas')
        rounded.bases = [ast.Name(id='Canvas', ctx=ast.Load())]
        theme = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'ThemeManager')
        theme.body = [node for node in theme.body if isinstance(node, ast.FunctionDef) and node.name == '__init__']
        stub = ast.ClassDef(name='Overlay', bases=[], keywords=[], decorator_list=[],
                            body=[node for node in overlay.body if isinstance(node, ast.FunctionDef) and node.name in methods])
        namespace = {'Canvas': Canvas, 'cos': cos, 'sin': sin, 'pi': pi, 'Iterator': object,
                     'tk': SimpleNamespace(N='n', CENTER='center', TclError=RuntimeError),
                     'll': Mock(), 'font': SimpleNamespace(Font=Mock())}
        # Replace only the annotation's Iterator[...] for this isolated compilation.
        namespace['Iterator'] = type('Iterator', (), {'__class_getitem__': classmethod(lambda cls, item: object)})
        exec(compile(ast.fix_missing_locations(ast.Module(body=[rounded, theme, stub], type_ignores=[])),
                     str(ROOT / 'ghost.py'), 'exec'), namespace)
        cls.Overlay, cls.RoundedCanvas, cls.Theme = namespace['Overlay'], namespace['RoundedCanvas'], namespace['ThemeManager']

    def test_black_backdrop_is_drawn_behind_text_again_after_reopening(self):
        overlay = self.Overlay()
        overlay.bg_color = '#000000'
        overlay.window = Mock()
        overlay.window.winfo_exists.return_value = True
        overlay.window.geometry.return_value = '100x100+20+20'
        overlay.overlay_corner_radius = overlay.overlay_text_padding = 15
        overlay.player_metric = {'player_text': 'Artist - Song', 'player_duration': '0:05 / 3:00', 'player_lyrics': ''}
        overlay.running_lyrics = False
        overlay.display_lyrics = True
        font = SimpleNamespace(measure=lambda text: len(text) * 8, metrics=lambda name: 16)
        overlay.theme = SimpleNamespace(FONTS={key: font for key in ('main', 'time', 'lyrics', 'hint')},
                                         COLORS={'text': '#fff', 'text_dark': '#aaa'})
        overlay.canvas_items = {'bg': None}
        overlay._last_position = (20, 20)
        overlay._place_overlay = Mock()
        for _ in range(2):
            overlay.canvas = self.RoundedCanvas()
            overlay._layout_key = None
            overlay.update_display(init_draw=True)
            background = overlay.canvas.items[overlay.canvas_items['bg']]
            self.assertEqual(background['fill'], '#000000')
            self.assertEqual(background['outline'], '')
            self.assertEqual(overlay.canvas.lowered, overlay.canvas_items['bg'])
            self.assertGreater(max(point[0] for point in background['points']), 100)
            self.assertGreater(max(point[1] for point in background['points']), 50)

    def test_transparency_key_is_distinct_from_black_and_all_visible_theme_colors(self):
        theme = self.Theme()
        transparent = theme.COLORS['transparent']
        self.assertNotEqual(transparent, '#000000')
        self.assertNotIn(transparent, [value for key, value in theme.COLORS.items() if key != 'transparent'])


if __name__ == '__main__':
    unittest.main()
