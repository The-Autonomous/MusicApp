import os, json, threading, requests, re
from time import time, sleep
from urllib.parse import quote_plus

try:
    from playerUtils import TitleCleaner
except ImportError:
    from .playerUtils import TitleCleaner

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
    
    def __init__(self, api_url: str = "https://lrclib.net/api/get", min_delay_between_calls: float = 1.0):
        """
        Initializes the lyric handler.

        Args:
            api_url (str): The base URL for the lyrics API.
            min_delay_between_calls (float): Minimum delay in seconds between API calls to prevent rate limiting.
        """
        self.json = JSONHandler()
        self.title_cleaner = TitleCleaner() # Initialize TitleCleaner
        self._API_URL = api_url
        self.last_api_call_time = 0.0
        self.min_delay_between_calls = min_delay_between_calls
    
    def request(self, artist: str, title: str, callback, local_song_id):
        """
        Requests lyrics for a given artist and title.
        It first checks the local cache. If not found, it attempts to fetch from the API.

        Args:
            artist (str): The artist's name.
            title (str): The song title.
            callback (callable): A function to call with the retrieved lyrics data
                                  (list of (timestamp, lyric_line) tuples) and local_song_id.
            local_song_id: An identifier for the current song playback session.
        """
        try:
            # Attempt to retrieve lyrics from local cache first
            cached_lyrics = self.json.get(artist, title)

            if cached_lyrics:
                # If lyrics are found in cache, use them directly
                lyrics_to_return = cached_lyrics
                # print(f"🎵 Lyrics for '{artist} - {title}' loaded from cache.")
            else:
                # If not in cache, try to load from external API
                # _load_synced_lyrics will return a list of lines or None on error/404
                lyrics_raw_lines = self._load_synced_lyrics(artist, title)

                if lyrics_raw_lines is not None:
                    # If lines were successfully fetched (not None), parse them
                    parsed_lyrics = self._parse_lyrics_timestamps(lyrics_raw_lines)
                    # Add to cache for future use
                    self.json.add(artist, title, parsed_lyrics)
                    lyrics_to_return = parsed_lyrics
                else:
                    # If _load_synced_lyrics returned None (e.g., 404, network error)
                    lyrics_to_return = [] # Indicate no lyrics found

            # Call the provided callback with the lyrics data
            callback(lyrics_to_return, local_song_id)

        except Exception as E:
            # Catch any unexpected errors during the process (e.g., issues with JSONHandler)
            print(f"⚠️ Lyrics Request Error (during processing/caching): {str(E)}")
            # Always call the callback, even on error, to avoid blocking the main thread
            callback([], local_song_id) # Pass empty list on general error
            
    def _clean_title_for_lyrics(self, artist: str, title: str) -> tuple[str, str]:
        """
        Cleans the combined artist and title string using TitleCleaner.

        Args:
            artist (str): The artist's name.
            title (str): The song title.

        Returns:
            tuple[str, str]: A tuple containing the cleaned artist and title.
                             (Note: TitleCleaner generally returns a combined string,
                             this method will parse it back into artist/title for API query).
        """
        # Combine and clean the title using TitleCleaner
        # The TitleCleaner typically produces a "Artist - Title" format
        combined_cleaned_title = self.title_cleaner.clean(f"{artist} - {title}")

        # Re-split the cleaned title into artist and track for the API query parameters
        # This assumes TitleCleaner's output is consistently "Artist - Title" or "Title"
        parts = [p.strip() for p in combined_cleaned_title.split(' - ', 1) if p.strip()]
        if len(parts) == 2:
            return parts[0], parts[1]
        elif len(parts) == 1:
            # If only one part after splitting (e.g., "Title Only"), assume artist is empty
            return "", parts[0]
        return artist.strip(), title.strip() # Fallback to original if cleaning yields unexpected results

    def _load_synced_lyrics(self, artist: str, title: str) -> list[str] | None: 
        """
        Fetch synced lyrics from lrclib.net using properly cleaned artist/title.
        Implements rate-limiting and robust error handling.

        Args:
            artist (str): The artist's name.
            title (str): The song title.

        Returns:
            list[str]: A list of raw lyric lines (e.g., ["[00:01.23]Lyric text"]) if successful.
            None: If fetching fails due to HTTP errors (including 404), network issues, or invalid JSON.
        """
        # Proper clean using the internal helper that utilizes TitleCleaner
        artist_clean, title_clean = self._clean_title_for_lyrics(artist, title)

        # Percent encode for URL
        artist_q = quote_plus(artist_clean)
        title_q = quote_plus(title_clean)

        # Construct the API URL
        # The specific endpoint for lrclib.net's API is /api/get for synced lyrics.
        # Original: https://lrclib.net/api/get?track_name={title_q}&artist_name={artist_q}
        # If _API_URL is "https://apiseek.com", you might need a different path here.
        # Assuming lrclib.net is the target based on previous context.
        # If using a generic API_URL like "https://apiseek.com", you might need a specific endpoint like:
        # url = f"{self._API_URL}/lyrics?track_name={title_q}&artist_name={artist_q}"
        # For now, sticking to lrclib.net's structure as per original code.
        url = (
            "https://lrclib.net/api/get"
            f"?track_name={title_q}&artist_name={artist_q}"
        )

        headers = {"User-Agent": "LyricsFetcher/1.0"} # Good practice to identify your client

        try:
            # Implement simple rate limiting before the API call
            current_time = time()
            elapsed = current_time - self.last_api_call_time
            if elapsed < self.min_delay_between_calls:
                sleep_needed = self.min_delay_between_calls - elapsed
                sleep(sleep_needed)
            self.last_api_call_time = time() # Update timestamp after potential sleep

            print(f"🎵 Fetching lyrics from '{url}'...") # Informative print
            resp = requests.get(url, headers=headers, timeout=10) # Added a timeout for network requests
            
            # Check for specific 404 status code directly
            if resp.status_code == 404:
                print(f"❌ Lyrics Not Found (HTTP 404) for '{artist} - {title}' on lrclib.net.")
                return None # Explicitly return None for 404
            
            resp.raise_for_status() # Raise an HTTPError for other bad responses (4xx or 5xx)

            # Attempt to parse the JSON response and get the syncedLyrics field
            synced_lyrics = resp.json().get("syncedLyrics") or ""
            return synced_lyrics.splitlines() # Return a list of lines

        except requests.exceptions.HTTPError as http_err:
            # Catch HTTP errors other than 404
            print(f"❌ HTTP Error fetching lyrics for '{artist} - {title}' (Status: {http_err.response.status_code}): {http_err}")
            return None
        except requests.exceptions.RequestException as req_err:
            # Catch network-related errors (e.g., connection issues, timeouts, DNS failures)
            print(f"❌ Network Error fetching lyrics for '{artist} - {title}': {req_err}")
            return None
        except json.JSONDecodeError as json_err:
            # Catch errors if the response content is not valid JSON
            print(f"❌ JSON Decode Error for lyrics response: {json_err}. Content: {resp.text[:100]}...") # Print partial content for debugging
            return None
        except Exception as E:
            # Catch any other unexpected exceptions
            print(f"⚠️ An unexpected error occurred while fetching lyrics for '{artist} - {title}': {str(E)}")
            return None
        
    def _parse_lyrics_timestamps(self, lyrics_list: list[str]) -> list[tuple[float, str]]:
        """
        Parses a list of raw lyric lines (e.g., ["[00:01.23]Lyric text"]) into
        a list of (timestamp_seconds, lyric_text) tuples.

        Args:
            lyrics_list (list[str]): A list of raw lyric lines.

        Returns:
            list[tuple[float, str]]: A sorted list of parsed lyric tuples.
        """
        parsed = []
        for entry in lyrics_list:
            try:
                # Remove the opening bracket and split into time/text parts
                # e.g., "[00:01.23]Lyric text" -> "00:01.23", "Lyric text"
                if not entry.startswith('['): # Ensure it's a timestamped line
                    continue # Skip lines that don't conform to expected format

                time_part, text_part = entry[1:].split(']', 1)
                text_part = text_part.strip()

                # Replace empty lyric lines with a musical note emoji
                if not text_part: # Checks for empty string after strip()
                    text_part = "🎵"

                # Split time into minutes and seconds
                # e.g., "00:01.23" -> "00", "01.23"
                minutes, seconds = time_part.split(':', 1)

                # Clean minutes and seconds to contain only digits and periods
                # This helps against malformed timestamps like "[00:01.23abc]"
                minutes = re.sub(r'[^0-9.]', '', minutes)
                seconds = re.sub(r'[^0-9.]', '', seconds)
                
                try:
                    # Convert to total seconds as float
                    total_seconds = float(minutes) * 60 + float(seconds)
                    parsed.append((total_seconds, text_part))
                except ValueError: # Explicitly catch ValueError for float conversion errors
                    print(f"⚠️ Lyrics Parse Error (Invalid timestamp format): '{time_part}' in entry: '{entry}'")
                    continue
            except IndexError: # Catch if split fails (e.g., no ']' found or empty string)
                print(f"⚠️ Lyrics Parse Error (Malformed entry format): '{entry}'")
                continue
            except Exception as E:
                # Catch any other unexpected errors during parsing of a single entry
                print(f"⚠️ Lyrics Parse Error (General): {str(E)} for entry: '{entry}'")

        # Sort by timestamp (first element of the tuple) before returning
        return sorted(parsed, key=lambda x: x[0])

if __name__ == "__main__":
    # Example usage
    handler = lyricHandler()
    
    def print_lyrics(lyrics, song_id):
        if lyrics:
            print(f"🎶 Lyrics for song ID {song_id}:")
            for timestamp, line in lyrics:
                print(f"[{timestamp:.2f}] {line}")
        else:
            print(f"No lyrics found for song ID {song_id}.")

    # Test with a sample artist and title
    handler.request("Boogersforbrains", "Sleep Token - Levitate", print_lyrics, local_song_id=1)