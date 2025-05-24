import os, json, threading, requests, re
from urllib.parse import quote_plus

### Utilities ###

class JSONHandler:
    """
    Manages a UTF-8 JSON cache file for lyrics lookups.
    File: .lyricCache.json in the current working directory.
    """

    def __init__(self, filename: str = ".lyricCache.json"):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        # Construct absolute path relative to script location
        self.filepath = os.path.join(script_dir, filename)
        self._lock = threading.RLock()
        # Load or create the cache
        if not os.path.isfile(self.filepath):
            with open(self.filepath, 'w', encoding='utf-8') as f:
                json.dump({}, f, ensure_ascii=False, indent=2)
        with open(self.filepath, 'r', encoding='utf-8') as f:
            try:
                self._cache = json.load(f)
            except json.JSONDecodeError:
                # Corrupted file—reset to empty
                self._cache = {}
    
    def get(self, artist: str, title: str):
        """
        Return the cached entry for (artist, title) or False if missing.
        """
        key = self._make_key(artist, title)
        return self._cache.get(key, False)

    def add(self, artist: str, title: str, data):
        """
        Add or update the cache entry for (artist, title) with `data`,
        then persist to disk.
        """
        key = self._make_key(artist, title)
        with self._lock:
            self._cache[key] = data
            self._save()

    def _make_key(self, artist: str, title: str) -> str:
        # Normalize to lower-case, strip whitespace
        return f"{artist.strip().lower()}|{title.strip().lower()}"

    def _save(self):
        # Write atomically to avoid corruption
        temp_path = self.filepath + ".tmp"
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(self._cache, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, self.filepath)
        
### Lyric Handler ###

class lyricHandler:
    
    def __init__(self):
        self.json = JSONHandler()
        pass
    
    def request(self, artist: str, title: str, callback, local_song_id):
        """Check For Lyrics"""
        try:
            LibraryLyrics = self.json.get(artist, title)
            if not LibraryLyrics:
                LibraryLyrics = self._parse_lyrics_timestamps(self._load_synced_lyrics(artist, title))
                self.json.add(artist, title, LibraryLyrics)
            callback(LibraryLyrics, local_song_id)
        except Exception as E:
            if not str(E).__contains__("404"):
                print(f"⚠️ Lyrics Request Error: {str(E)}")
            callback("")
            
    def _clean_title_for_lyrics(self, title: str) -> tuple[str, str]:
        """Cleans title for lyric search, ignoring suffixes."""
        # Split and clean main content
        if ' - ' in title:
            artist, track = title.split(' - ', 1)
            track_clean = re.split(r'(?: - |\(|\||\[)', track, maxsplit=1)[0].strip()
            artist_clean = artist.strip()
        else:
            artist_clean = ''
            track_clean = re.split(r'(?: - |\(|\||\[)', title, maxsplit=1)[0].strip()
        
        return artist_clean, track_clean

    def _load_synced_lyrics(self, artist: str, title: str) -> list[str]: 
        """
        Fetch synced lyrics from lrclib.net using properly cleaned artist/title.
        """
        # 1) Auto-split "Artist - Title" if artist is missing
        if " - " in title:
            artist, title = [part.strip() for part in title.split(" - ", 1)]

        # 2) Proper clean
        artist_clean, title_clean = self._clean_title_for_lyrics(f"{artist} - {title}")

        # 3) Percent encode
        artist_q = quote_plus(artist_clean)
        title_q = quote_plus(title_clean)

        url = (
            "https://lrclib.net/api/get"
            f"?track_name={title_q}&artist_name={artist_q}"
        )

        headers = {"User-Agent": "LyricsFetcher/1.0"}
        try:
            resp = requests.get(url, headers=headers)
            if resp.status_code == 404:
                return []
            resp.raise_for_status()

            synced = resp.json().get("syncedLyrics") or ""
            return synced.splitlines()
        except Exception as E:
            if not str(E).__contains__("404"):
                print(f"⚠️ Lyrics Get Error: {str(E)}")
            return []
        
    def _parse_lyrics_timestamps(self, lyrics_list):
        parsed = []
        for entry in lyrics_list:
            try:
                # Remove the opening bracket and split into time/text parts
                time_part, text_part = entry[1:].split(']', 1)
                text_part = text_part.strip()

                if text_part == "" or text_part == " ":
                    text_part = "🎵"

                # Split time into minutes and seconds
                minutes, seconds = time_part.split(':', 1)

                # Clean minutes and seconds to contain only digits and periods
                minutes = re.sub(r'[^0-9.]', '', minutes)
                seconds = re.sub(r'[^0-9.]', '', seconds)
                
                try:
                    # Convert to total seconds as float
                    total_seconds = float(minutes) * 60 + float(seconds)
                    parsed.append((total_seconds, text_part))
                except:
                    print(f"⚠️ Lyrics Parse Error (Float Handler) With Entry: {entry}")
                    continue
            except Exception as E:
                print(f"⚠️ Lyrics Parse Error: {str(E)}")

        # Sort by timestamp (first element of the tuple) before returning
        return sorted(parsed, key=lambda x: x[0])