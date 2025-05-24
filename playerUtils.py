import re, threading, os
from typing import List, Optional, Tuple

try:
    from ghost import GhostOverlay
    from music import MusicPlayer, get_auto_directories
except ImportError:
    from .ghost import GhostOverlay
    from .music import MusicPlayer, get_auto_directories
    
class TitleCleaner:
    """Clean raw track titles into a consistent 'Artist - Title' format, including dynamic suffix mapping."""
    _split_pattern = re.compile(r'(?: - |\(|\||\[)')
    SearchReplaceRule = Tuple[str, str]
    _defaults: List[SearchReplaceRule] = [("***[]*Paused", " -[Paused]-"), ("*=*", " -[Paused]-"), ("*+*", " -[Repeat]-")]

    def __init__(self, rules: Optional[List[SearchReplaceRule]] = None):
        self.rules = rules or self._defaults

    def clean(self, raw: str) -> str:
        # Detect and strip suffix
        suffix = next((old for old, _ in self.rules if raw.endswith(old)), '')
        core = raw[:-len(suffix)].strip() if suffix else raw

        # Split artist and track
        artist, sep, track = core.partition(' - ')
        if not sep:
            track = core
            artist = ''

        # Isolate main track title
        main = self._split_pattern.split(track, 1)[0].strip()
        cleaned = f"{artist.strip()} - {main}" if artist else main

        # Reattach suffix and apply replacements
        result = f"{cleaned}{suffix}"
        for old, new in self.rules:
            result = result.replace(old, new)
        return result

class MusicOverlayController:
    """Tie MusicPlayer updates to GhostOverlay in a clean, thread-safe way."""
    def __init__(self, overlay: GhostOverlay):
        self.overlay = overlay
        self.cleaner = TitleCleaner()
        self.player = MusicPlayer(
            directories=get_auto_directories(self.load_playlists()),
            set_screen=self._update_text,
            set_duration=self._update_duration,
            set_lyrics=self._update_lyrics,
            set_ips=self._update_ips,
        )
        overlay.MusicPlayer = self.player
    
    def load_playlists(self, file_path: str = 'Playlists.txt') -> List[str]:
        """
        Load playlist URLs from a text file, one URL per line.
        Empty lines and lines starting with '#' are ignored.
        """
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            # Construct absolute path relative to script location
            absolute_path = os.path.join(script_dir, file_path)
            with open(absolute_path, 'r', encoding='utf-8') as f:
                lines = [line.strip() for line in f if line.strip() and not line.startswith('#')]
            return lines
        except FileNotFoundError:
            print(f"Playlist file '{file_path}' not found.")
            return []

    def _update_text(self, artist: str = '', title: str = ''):
        text = self.cleaner.clean(f"{artist} - {title}" if artist != "Unknown Artist" and not artist in title else title)
        self.overlay.set_text(text)

    def _update_duration(self, current: float, total: float):
        self.overlay.set_duration(current, total)

    def _update_lyrics(self, show: bool = True, lyrics: str = ''):
        self.overlay.toggle_lyrics(show)
        if lyrics:
            self.overlay.set_lyrics(lyrics)
        
    def _update_ips(self, ip_list):
        self.overlay.set_radio_ips(ip_list)

    def start(self):
        threading.Thread(target=self.player.core_handler, daemon=True).start()