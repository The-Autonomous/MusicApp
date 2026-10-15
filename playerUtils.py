import re, threading, os, sys, signal, platform
from typing import List, Optional, Tuple
from pathlib import Path

try:
    from log_loader import log_loader
except:
    from .log_loader import log_loader

### Logging Handler ###

ll = log_loader("Player Utilities")

#######################

class TitleCleaner:
    """Clean raw track titles into a consistent 'Artist - Title' format, including dynamic suffix mapping."""
    _split_pattern = re.compile(r'(?: - |\(|\||\[)')
    SearchReplaceRule = Tuple[str, str]
    _defaults: List[SearchReplaceRule] = [("*=*", " -[Paused]-"), ("*+*", " -[Repeat]-")]

    def __init__(self, rules: Optional[List[SearchReplaceRule]] = None):
        self.rules = rules or self._defaults
        
    def clean(self, raw: str) -> str:
        """
        Cleans a raw song title string to extract and format artist and track.

        Handles various formats including:
        - Artist - Track
        - Track Only
        - Artist - Track - Artist (where start and end artists are the same)
        - Context - Artist - Track (attempts to identify actual artist and track)
        - Strips suffixes and applies general string replacements.
        """
        if not isinstance(raw, str):
            # Or raise TypeError, depending on desired error handling
            return "" 
        
        core_text = raw.strip()

        # 1. Detect and strip suffix (based on the original logic)
        #    A rule (old, new) from self.rules is considered a suffix rule if `raw.endswith(old)`.
        #    The `old` part is stripped as a suffix and reattached later.
        #    The original code takes the *first* matching suffix rule.
        #    To be more robust, one might prefer the *longest* suffix, but we'll stick to original's behavior.
        
        # Find the `old` part of the first rule in self.rules that raw ends with.
        suffix_to_strip = ""
        # Iterate through self.rules to find a potential suffix.
        # The original code uses `next(...)` which finds the first match.
        for old_pattern, _ in self.rules:
            if core_text.endswith(old_pattern):
                suffix_to_strip = old_pattern
                break # Found the first matching suffix as per original logic
        
        if suffix_to_strip:
            core_text = core_text[:-len(suffix_to_strip)].strip()

        # 2. Determine Artist and Track from the `core_text`
        final_artist = ""
        final_track = ""

        # Split `core_text` by " - " delimiter
        # Filter out empty strings that might result from multiple hyphens like "A -- B"
        parts = [p.strip() for p in core_text.split(' - ') if p.strip()]

        if not parts:
            # core_text was empty or became empty after stripping suffix
            pass # final_artist and final_track remain empty
        elif len(parts) == 1:
            # Only one segment, assume it's the track
            final_track = parts[0]
        elif len(parts) == 2:
            # Standard "Artist - Track"
            final_artist = parts[0]
            final_track = parts[1]
        elif len(parts) > 2:
            # More than two parts, e.g., "A - B - C", "A - B - C - D"
            # Case 1: "Artist - Title - Artist" (ends are the same, case-insensitive)
            if parts[0].lower() == parts[-1].lower():
                final_artist = parts[0] # Or parts[-1], they are the same
                final_track = ' - '.join(parts[1:-1])
            # Case 2: Specifically three parts "X - Y - Z" where X != Z
            # This handles "Random Name/Context - Actual Artist - Actual Track"
            elif len(parts) == 3:
                final_artist = parts[1] # Assumes Y is the artist
                final_track = parts[2]  # Assumes Z is the track
                                        # The first part (X, parts[0]) is effectively ignored as primary artist/track
            else:
                # Fallback for len(parts) > 3 and not (A - ... - A)
                # Default to: first part is artist, rest is track
                # This mirrors `core_text.partition(' - ')` behavior for multiple hyphens.
                final_artist = parts[0]
                final_track = ' - '.join(parts[1:])
        
        # 3. Isolate main track title from `final_track` using `_split_pattern`
        #    This pattern is intended to remove things like "(feat. XYZ)", "[Remix]", etc.
        #    from the end or middle of the track title.
        main_title = ""
        if final_track:
            # `_split_pattern.split(text, 1)[0]` gets the content before the first match.
            main_title_candidate = self._split_pattern.split(final_track, 1)[0].strip()
            
            if not main_title_candidate and final_track:
                # This occurs if `final_track` itself is or starts with the pattern
                # (e.g., track is "(Interlude)" and pattern matches it).
                # In such cases, the `final_track` (as is) should be the `main_title`.
                main_title = final_track
            else:
                main_title = main_title_candidate
        
        # 4. Construct the cleaned core string (artist - title)
        cleaned_core_parts = []
        if final_artist.strip():
            cleaned_core_parts.append(final_artist.strip())
        if main_title.strip():
            cleaned_core_parts.append(main_title.strip())
        
        cleaned_intermediate_result = " - ".join(cleaned_core_parts)

        # 5. Reattach the original suffix
        #    The `suffix_to_strip` is the exact string that was removed earlier.
        result_with_suffix = f"{cleaned_intermediate_result}{suffix_to_strip}" if cleaned_intermediate_result else suffix_to_strip
        if not cleaned_intermediate_result and not suffix_to_strip: # Both were empty
             result_with_suffix = ""


        # 6. Apply all general replacement rules from `self.rules`
        #    This applies to the entire string (which now includes the reattached suffix).
        #    A rule used for suffix stripping could also apply a replacement here if `new` is not empty.
        final_cleaned_str = result_with_suffix
        for old_pattern, new_replacement in self.rules:
            final_cleaned_str = final_cleaned_str.replace(old_pattern, new_replacement)
            
        return final_cleaned_str.strip() # Final strip for good measure

class MusicOverlayController:
    """Tie MusicPlayer updates to GhostOverlay in a clean, thread-safe way."""
    def __init__(self, overlay, fast_load: bool = False):
        try:
            from music import MusicPlayer
        except ImportError:
            from .music import MusicPlayer
        self.overlay = overlay
        self.cleaner = TitleCleaner()
        self.player = MusicPlayer(
            directories=self.get_auto_directories(self.load_playlists()),
            set_screen=self._update_text,
            set_duration=self._update_duration,
            set_lyrics=self._update_lyrics,
            set_ips=self._update_ips,
            is_afk=self.is_afk,
            fast_load=fast_load,
            startup_hold=True,
        )
        overlay.MusicPlayer = self.player
        if not overlay.playerState:
            self.player.set_enabled(False)
        from youtube_sync import YouTubeSync
        self.youtube_sync = YouTubeSync(self.player)
        overlay.youtube_sync = self.youtube_sync
        if self.youtube_sync.settings.get('host_radio', False):
            self.player.set_hosting(True)
        self.start()

    def close(self):
        self.youtube_sync.close()
        self.player.close()
    
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
            ll.warn(f"Playlist file '{file_path}' not found.")
            with open(absolute_path, 'w', encoding="utf-8") as f:
                f.write("")
            return []
        
    def get_auto_directories(self, candidate_urls=None):
        """Automatically detects existing GTAV Enhanced User Music directories"""
        valid_dirs = list(candidate_urls or [])
        
        # Determine base path based on OS
        if platform.system() == 'Windows':
            base_path = Path(os.environ.get('USERPROFILE', Path.home()))
        else:
            base_path = Path.home()

        # List of potential directory candidates
        candidate_paths = [
            base_path / "Documents" / "Rockstar Games" / "GTAV Enhanced" / "User Music",
            base_path / "OneDrive" / "Documents" / "Rockstar Games" / "GTAV Enhanced" / "User Music",
            Path("/Volumes/Games/Rockstar Games/GTAV Enhanced/User Music"),
            Path.home() / "Games" / "GTAV Enhanced" / "User Music"
        ]
        if platform.system() == 'Windows':
            # Documents may be redirected to another drive or OneDrive.
            try:
                import ctypes
                documents = ctypes.create_unicode_buffer(260)
                if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, documents) == 0:
                    candidate_paths.insert(0, Path(documents.value) / "Rockstar Games" / "GTAV Enhanced" / "User Music")
                    candidate_paths.insert(1, Path(documents.value) / "Rockstar Games" / "GTA V" / "User Music")
            except (AttributeError, OSError):
                pass
        
        # Check which directories actually exist
        for path in candidate_paths:
            if path.exists() and path.is_dir():
                valid_dirs.append(str(path.resolve()))

        forcedPaths = [
            os.path.expanduser("~/MusicPlayerYoutubeDownloads"),
        ]
        
        # Ensure forced paths exist
        for path in forcedPaths:
            os.makedirs(path, exist_ok=True)

        return list(dict.fromkeys([*valid_dirs, *forcedPaths]))

    def _update_text(self, artist: str = '', title: str = ''):
        text = self.cleaner.clean(f"{artist} - {title}" if artist != "Unknown Artist" and not artist in title else title)
        self.overlay.set_text(text)

    def _update_duration(self, current: float, total: float):
        self.overlay.set_duration(current, total)

    def _update_lyrics(self, show: bool = True, lyrics: str = ''):
        self.overlay.toggle_lyrics(show)
        if lyrics:
            self.overlay.set_lyrics(lyrics)
    
    def is_afk(self) -> bool:
        return self.overlay.is_afk()
        
    def _update_ips(self, ip_list):
        self.overlay.set_radio_ips(ip_list)

    def start(self):
        self.player.core_handler()
        
class ProgramShutdown:
    """Finish background work without bypassing saves or blocking Tk's event loop."""
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance.root = None
                cls._instance.cleanup_callbacks = []
                cls._instance.shutdown_event = threading.Event()
            return cls._instance

    def register_root(self, root):
        self.root = root

    def register_cleanup(self, callback):
        self.cleanup_callbacks.append(callback)

    def shutdown(self, exit_code=0, reason=""):
        if self.shutdown_event.is_set():
            return
        self.shutdown_event.set()
        finished = threading.Event()

        def cleanup():
            try:
                for callback in self.cleanup_callbacks:
                    try:
                        callback()
                    except Exception as exc:
                        ll.warn(f"Cleanup: {exc}")
            finally:
                finished.set()

        if self.root:
            # The overlay invokes shutdown from the Tk thread.
            for window in self.root.winfo_children():
                try:
                    window.withdraw()
                except Exception:
                    pass
            threading.Thread(target=cleanup, daemon=True, name="shutdown").start()

            def finish():
                if finished.is_set():
                    self.root.quit()
                    self.root.destroy()
                else:
                    self.root.after(100, finish)
            self.root.after(100, finish)
        else:
            cleanup()
