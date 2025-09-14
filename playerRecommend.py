import os
import json
import re
from collections import Counter
from threading import RLock

try:
    from log_loader import log_loader, OutputRedirector
except:
    from .log_loader import log_loader, OutputRedirector

# Instantiate the logger for use within the PlayerRecommender class
ll = log_loader("Recommendations", debugging=True)

class PlayerRecommender:
    """
    Logs user listening habits and search queries to provide analytics 
    and recommendations, persisting data across sessions.
    """
    
    # A basic set of stop words to filter from search queries for better analysis
    STOP_WORDS = set([
        'a', 'an', 'the', 'and', 'or', 'but', 'in', 'on', 'at', 'for', 'with', 
        'about', 'to', 'from', 'by', 'of', 'is', 'it', 'was', 'were'
    ])

    def __init__(self, filename: str = ".player_recommend_data.json"):
        """
        Initializes the recommender, loading existing data from the specified file.

        Args:
            filename (str): The name of the JSON file to store player data.
        """
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
        except NameError:
            script_dir = os.getcwd()
        self.filepath = os.path.join(script_dir, filename)
        
        self._lock = RLock()
        
        # Load data from file or initialize empty structures
        self._data = self._load()
        self.song_plays = self._data.get("song_plays", {})
        self.search_word_counts = Counter(self._data.get("search_word_counts", {}))

    def _load(self) -> dict:
        """Loads data from the JSON file in a thread-safe manner."""
        with self._lock:
            if not os.path.isfile(self.filepath):
                ll.warn(f"Data file not found at '{self.filepath}'. A new one will be created.")
                return {}
            try:
                with open(self.filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                    if not content.strip():
                        return {}
                    return json.loads(content)
            except (json.JSONDecodeError, FileNotFoundError) as e:
                ll.error(f"Could not load or parse data file: {e}. Starting with empty data.")
                return {}

    def _save(self):
        """Saves the current data to the JSON file atomically and thread-safely."""
        with self._lock:
            # Prepare data for serialization
            self._data['song_plays'] = self.song_plays
            self._data['search_word_counts'] = self.search_word_counts
            
            temp_path = self.filepath + ".tmp"
            try:
                with open(temp_path, 'w', encoding='utf-8') as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
                # Atomic rename operation
                os.replace(temp_path, self.filepath)
            except Exception as e:
                ll.error(f"FATAL: Could not save data to file: {e}")
                
    def log_song_play(self, artist: str, song: str):
        """
        Logs a single listen for a given song and artist.
        Inputs are normalized to prevent case-sensitivity issues.
        """
        if not artist or not song:
            ll.warn("log_song_play requires both artist and song.")
            return

        artist_norm = artist.strip().lower()
        song_norm = song.strip().lower()
        
        if artist_norm.__contains__('unknown'):
            ll.debug("Ignoring 'Unknown' artist entries.")
            return
        
        # Get artist's song dictionary, creating it if it doesn't exist
        artist_songs = self.song_plays.get(artist_norm, {})
        
        # Increment the play count for the song
        artist_songs[song_norm] = artist_songs.get(song_norm, 0) + 1
        
        # Update the main dictionary
        self.song_plays[artist_norm] = artist_songs
        
        ll.print(f"Logged play for '{song.strip()}' by '{artist.strip()}'.")
        self._save()

    def log_search(self, query: str):
        """
        Logs a search query, breaking it down into individual words
        and counting their frequency for future suggestions.
        """
        if not query or not query.strip():
            ll.warn("log_search requires a non-empty query.")
            return

        query_norm = query.strip().lower()
        
        if query_norm.__contains__('unknown'):
            ll.debug("Ignoring 'Unknown' artist entries.")
            return
        
        # Use regex to find all words, ignoring punctuation
        words = re.findall(r'\b\w+\b', query_norm)
        
        # Filter out common stop words for more meaningful analysis
        filtered_words = [word for word in words if word not in self.STOP_WORDS]
        
        self.search_word_counts.update(filtered_words)
        
        ll.print(f"Logged search query: '{query.strip()}'. Analyzed words: {filtered_words}")
        self._save()

    def analyze_top_artists(self, top_n: int = 5) -> list:
        """
        Analyzes the full play history to find the artists with the most total listens.

        Args:
            top_n (int): The number of top artists to return.

        Returns:
            list: A list of tuples, where each tuple is (artist_name, total_plays),
                  sorted in descending order of plays.
        """
        if not self.song_plays:
            ll.print("No play history available to analyze.")
            return []

        artist_totals = Counter()
        for artist, songs in self.song_plays.items():
            # Sum up all play counts for each song by the artist
            artist_totals[artist] = sum(songs.values())
            
        return artist_totals.most_common(top_n)

    def analyze_top_songs(self, top_n: int = 5) -> list:
        """
        Analyzes the full play history to find the songs with the most listens.

        Args:
            top_n (int): The number of top songs to return.

        Returns:
            list: A list of tuples, where each tuple is (song_name, total_plays, artist_name),
                  sorted in descending order of plays.
        """
        if not self.song_plays:
            ll.print("No play history available to analyze.")
            return []

        song_totals = Counter()
        song_to_artist = {}
        
        for artist, songs in self.song_plays.items():
            for song, plays in songs.items():
                song_totals[song] += plays
                song_to_artist[song] = artist
                
        top_songs = song_totals.most_common(top_n)
        
        # Return list of tuples (song_name, total_plays, artist_name)
        return [(song, plays, song_to_artist[song]) for song, plays in top_songs]

    def suggest_search_terms(self, current_query: str = "", top_n: int = 5) -> list:
        """
        Suggests other potential search terms based on the most frequently
        searched words in the user's history.

        Args:
            current_query (str): The user's current search query, to exclude its
                                 words from the suggestions.
            top_n (int): The number of suggestions to provide.

        Returns:
            list: A list of suggested search term strings.
        """
        if not self.search_word_counts:
            ll.print("No search history available for suggestions.")
            return []
            
        current_words = set(re.findall(r'\b\w+\b', current_query.strip().lower()))
        
        suggestions = []
        # Get the most common words from history
        for word, count in self.search_word_counts.most_common():
            if word not in current_words:
                suggestions.append(word)
            if len(suggestions) >= top_n:
                break
                
        return suggestions
    
    def suggest_search_terms_str(self):
        return " ".join([suggestion.title() for suggestion in self.suggest_search_terms(current_query="", top_n=2)])


if __name__ == "__main__":
    # Use the OutputRedirector context manager to handle logging to a file
    with OutputRedirector(enable_dual_logging=True):
        # Initialize the recommender. It will automatically load previous data if it exists.
        recommender = PlayerRecommender()

        # --- Perform Analysis ---
        ll.print("\n--- Analyzing Artist Playing Habits ---")
        top_artists = recommender.analyze_top_artists(top_n=20)
        
        if top_artists:
            print("\nYour Top Artists:")
            for i, (artist, plays) in enumerate(top_artists, 1):
                print(f"  {i}. {artist.title()} ({plays} total plays)")
                
        ll.print("\n--- Analyzing Song Playing Habits ---")
        top_songs = recommender.analyze_top_songs(top_n=20)
        
        if top_songs:
            print("\nYour Top Artists:")
            for i, (song, plays, artist) in enumerate(top_songs, 1):
                print(f"  {i}. {song.title()} by {artist.title()} ({plays} total plays)")
        
        ll.print(f"Recommended Search Term: {recommender.suggest_search_terms_str()}")
        
        while True:
            # --- Get Search Suggestions ---
            ll.print("\n--- Generating Search Suggestions ---")
            suggestions = recommender.suggest_search_terms(current_query=input(">>> "), top_n=3)
            
            if suggestions:
                print("\nBased on your history, you might also want to search for:")
                for term in suggestions:
                    print(f"  - {term}")