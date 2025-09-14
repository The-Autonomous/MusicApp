import os, json, re, atexit, threading
from collections import Counter
from time import sleep

try:
    from log_loader import log_loader, OutputRedirector
except ImportError:
    from .log_loader import log_loader, OutputRedirector

# Instantiate the logger for use within the PlayerRecommender class
ll = log_loader("Recommendations", debugging=True)

class PlayerRecommender:
    """
    Logs user listening habits and search queries to provide analytics 
    and recommendations, persisting data across sessions with optimized,
    batched disk I/O to improve performance.
    """
    
    # A basic set of stop words to filter from search queries for better analysis
    STOP_WORDS = set([
        'a', 'an', 'the', 'and', 'or', 'but', 'in', 'on', 'at', 'for', 'with', 
        'about', 'to', 'from', 'by', 'of', 'is', 'it', 'was', 'were'
    ])

    def __init__(self, filename: str = ".player_recommend_data.json", save_interval: int = 300):
        """
        Initializes the recommender, loading existing data and starting a
        background save process.

        Args:
            filename (str): The name of the JSON file to store player data.
            save_interval (int): Seconds between automatic background saves.
        """
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
        except NameError:
            script_dir = os.getcwd()
        self.filepath = os.path.join(script_dir, filename)
        
        # Threading controls
        self._lock = threading.RLock()
        self._dirty = False  # Flag to track if there are unsaved changes
        self._shutdown_event = threading.Event()
        self._save_interval = save_interval

        # Load data from file or initialize empty structures
        self._data = self._load()
        self.song_plays = self._data.get("song_plays", {})
        self.search_word_counts = Counter(self._data.get("search_word_counts", {}))
        
        # Start the periodic background saving thread
        self._save_thread = threading.Thread(target=self._periodic_save_loop, daemon=True)
        self._save_thread.start()

        # Register the final save operation to run on program exit
        atexit.register(self.close)

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
        """
        Saves the current data to the JSON file atomically and thread-safely,
        but only if there are pending changes.
        """
        with self._lock:
            # If there's nothing to save, just return.
            if not self._dirty:
                return

            # Prepare data for serialization
            self._data['song_plays'] = self.song_plays
            self._data['search_word_counts'] = self.search_word_counts
            
            temp_path = self.filepath + ".tmp"
            try:
                with open(temp_path, 'w', encoding='utf-8') as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
                # Atomic rename operation is safer than writing directly
                os.replace(temp_path, self.filepath)
                self._dirty = False  # Reset the dirty flag after a successful save
                ll.debug("Player recommendation data saved to disk.")
            except Exception as e:
                ll.error(f"FATAL: Could not save data to file: {e}")
    
    def _periodic_save_loop(self):
        """A simple loop that runs in a background thread to save data periodically."""
        while not self._shutdown_event.wait(self._save_interval):
            self._save()

    def close(self):
        """A cleanup method to be called on application exit."""
        ll.print("Shutdown signal received, performing final save of recommendation data...")
        self._shutdown_event.set()
        self._save() # Perform one last save
        if self._save_thread.is_alive():
            self._save_thread.join(timeout=2.0) # Wait for the thread to finish
                
    def log_song_play(self, artist: str, song: str):
        """
        Logs a single listen for a given song and artist. This updates the in-memory
        data and marks it as 'dirty' for the next save cycle.
        """
        if not artist or not song:
            ll.warn("log_song_play requires both artist and song.")
            return

        with self._lock:
            artist_norm = artist.strip().lower()
            song_norm = song.strip().lower()
            
            if 'unknown' in artist_norm:
                ll.debug("Ignoring 'Unknown' artist entries.")
                return
            
            artist_songs = self.song_plays.get(artist_norm, {})
            artist_songs[song_norm] = artist_songs.get(song_norm, 0) + 1
            self.song_plays[artist_norm] = artist_songs
            
            self._dirty = True  # Mark data as changed

        ll.print(f"Logged play for '{song.strip()}' by '{artist.strip()}'.")

    def log_search(self, query: str):
        """
        Logs a search query, breaking it down into individual words
        and counting their frequency. Marks data as 'dirty' for the next save.
        """
        if not query or not query.strip():
            ll.warn("log_search requires a non-empty query.")
            return

        with self._lock:
            query_norm = query.strip().lower()
            
            if 'unknown' in query_norm:
                ll.debug("Ignoring 'Unknown' artist entries.")
                return
            
            words = re.findall(r'\b\w+\b', query_norm)
            filtered_words = [word for word in words if word not in self.STOP_WORDS]
            
            self.search_word_counts.update(filtered_words)
            self._dirty = True # Mark data as changed

        ll.print(f"Logged search query: '{query.strip()}'. Analyzed words: {filtered_words}")

    def analyze_top_artists(self, top_n: int = 5) -> list:
        """
        Analyzes the full play history to find the artists with the most total listens.

        Args:
            top_n (int): The number of top artists to return.

        Returns:
            list: A list of tuples, where each tuple is (artist_name, total_plays),
                  sorted in descending order of plays.
        """
        with self._lock:
            if not self.song_plays:
                ll.print("No play history available to analyze.")
                return []

            artist_totals = Counter()
            for artist, songs in self.song_plays.items():
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
        with self._lock:
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
        with self._lock:
            if not self.search_word_counts:
                ll.print("No search history available for suggestions.")
                return []
                
            current_words = set(re.findall(r'\b\w+\b', current_query.strip().lower()))
            
            suggestions = []
            for word, count in self.search_word_counts.most_common():
                if word not in current_words:
                    suggestions.append(word)
                if len(suggestions) >= top_n:
                    break
                    
            return suggestions
    
    def suggest_search_terms_str(self):
        """Returns the top 2 search suggestions as a single title-cased string."""
        with self._lock:
            suggestions = self.suggest_search_terms(current_query="", top_n=2)
            return " ".join([suggestion.title() for suggestion in suggestions])


if __name__ == "__main__":
    # Use the OutputRedirector context manager to handle logging to a file
    with OutputRedirector(enable_dual_logging=True):
        # Initialize the recommender. It will automatically load previous data.
        recommender = PlayerRecommender()

        # The rest of the main block can remain for testing purposes.
        # It demonstrates how to use the analysis methods.
        ll.print("\n--- Analyzing Artist Playing Habits ---")
        top_artists = recommender.analyze_top_artists(top_n=20)
        
        if top_artists:
            print("\nYour Top Artists:")
            for i, (artist, plays) in enumerate(top_artists, 1):
                print(f"  {i}. {artist.title()} ({plays} total plays)")
                
        ll.print("\n--- Analyzing Song Playing Habits ---")
        top_songs = recommender.analyze_top_songs(top_n=20)
        
        if top_songs:
            print("\nYour Top Songs:")
            for i, (song, plays, artist) in enumerate(top_songs, 1):
                print(f"  {i}. {song.title()} by {artist.title()} ({plays} total plays)")
        
        ll.print(f"Recommended Search Term: {recommender.suggest_search_terms_str()}")
        
        try:
            while True:
                # --- Get Search Suggestions ---
                ll.print("\n--- Generating Search Suggestions ---")
                current_search = input(">>> ")
                if current_search.lower() in ['exit', 'quit']:
                    break
                
                # You can log this search if desired
                # recommender.log_search(current_search)
                
                suggestions = recommender.suggest_search_terms(current_query=current_search, top_n=3)
                
                if suggestions:
                    print("\nBased on your history, you might also want to search for:")
                    for term in suggestions:
                        print(f"  - {term}")
        except KeyboardInterrupt:
            print("\nExiting.")