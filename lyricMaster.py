import os, json, threading, requests, re, atexit, hashlib
from time import sleep, time
from urllib.parse import quote_plus
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from dataclasses import dataclass
from typing import Optional, List, Tuple, Callable, Dict, Any
from queue import Queue, Empty
from threading import Thread

try:
    from playerUtils import TitleCleaner
    from log_loader import log_loader
except ImportError:
    from .playerUtils import TitleCleaner
    from .log_loader import log_loader

### Logging Handler ###
ll = log_loader("Lyric Master", debugging=False)

### Data Structures ###

@dataclass
class LyricEntry:
    """Immutable lyric entry with timestamp and text."""
    timestamp: float
    text: str

@dataclass
class CacheEntry:
    """Cache entry with metadata for TTL and validation."""
    lyrics: List[LyricEntry]
    timestamp: float
    hash_key: str

class OptimizedJSONHandler:
    """
    High-performance JSON cache with batching, TTL, and background writes.
    Uses threading for non-blocking I/O operations.
    """
    
    def __init__(self, filename: str = ".lyricCache.json", 
                 batch_size: int = 50, flush_interval: float = 5.0,
                 ttl_hours: int = 168):  # 1 week TTL
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self.filepath = os.path.join(script_dir, filename)
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.ttl_seconds = ttl_hours * 3600
        
        # Thread-safe operations
        self._lock = threading.RLock()
        self._cache: Dict[str, CacheEntry] = {}
        self._pending_writes: Dict[str, CacheEntry] = {}
        self._last_flush = time()
        
        # Background writer thread
        self._write_queue = Queue()
        self._writer_thread = None
        self._shutdown = threading.Event()
        
        # Initialize cache and start background writer
        self._load_cache()
        self._start_writer_thread()
        
        # Register cleanup on exit
        atexit.register(self.close)
        
    def _start_writer_thread(self):
        """Start background writer thread."""
        if self._writer_thread is None or not self._writer_thread.is_alive():
            self._writer_thread = threading.Thread(
                target=self._background_writer, daemon=True
            )
            self._writer_thread.start()
    
    def _background_writer(self):
        """Background thread for batched writes."""
        while not self._shutdown.is_set():
            try:
                # Wait for flush signal or timeout
                try:
                    self._write_queue.get(timeout=self.flush_interval)
                except Empty:
                    pass
                
                # Flush if needed
                current_time = time()
                with self._lock:
                    if (self._pending_writes and 
                        (len(self._pending_writes) >= self.batch_size or
                         current_time - self._last_flush > self.flush_interval)):
                        self._flush_to_disk()
                        
            except Exception as e:
                ll.error(f"Background writer error: {e}")
                sleep(1)
    
    def _load_cache(self):
        """Load cache from disk."""
        if not os.path.isfile(self.filepath):
            self._save_cache_sync()
            return
            
        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                raw_cache = json.load(f)
                
            # Convert to CacheEntry objects and validate TTL
            current_time = time()
            for key, entry_data in raw_cache.items():
                if isinstance(entry_data, dict) and 'timestamp' in entry_data:
                    # Check TTL
                    if current_time - entry_data['timestamp'] < self.ttl_seconds:
                        lyrics = [LyricEntry(l['timestamp'], l['text']) 
                                for l in entry_data.get('lyrics', [])]
                        self._cache[key] = CacheEntry(
                            lyrics=lyrics,
                            timestamp=entry_data['timestamp'],
                            hash_key=entry_data.get('hash_key', '')
                        )
                        
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            ll.warn(f"Cache file corrupted, starting fresh: {e}")
            self._cache = {}
    
    def get(self, artist: str, title: str) -> Optional[List[LyricEntry]]:
        """Get cached lyrics with TTL validation."""
        key = self._make_key(artist, title)
        
        with self._lock:
            entry = self._cache.get(key)
            if not entry:
                return None
                
            # Check TTL
            if time() - entry.timestamp > self.ttl_seconds:
                del self._cache[key]
                return None
                
            return entry.lyrics
    
    def add(self, artist: str, title: str, lyrics: List[LyricEntry]):
        """Add lyrics to cache with batched writes."""
        key = self._make_key(artist, title)
        hash_key = self._generate_hash(artist, title)
        
        entry = CacheEntry(
            lyrics=lyrics,
            timestamp=time(),
            hash_key=hash_key
        )
        
        with self._lock:
            self._cache[key] = entry
            self._pending_writes[key] = entry
            
            # Signal writer if batch is full
            if len(self._pending_writes) >= self.batch_size:
                try:
                    self._write_queue.put_nowait("flush")
                except:
                    pass  # Queue full, writer will catch up
    
    def _flush_to_disk(self):
        """Flush pending writes to disk (called from writer thread)."""
        if not self._pending_writes:
            return
            
        # Convert cache to serializable format
        cache_data = {}
        for key, entry in self._cache.items():
            cache_data[key] = {
                'lyrics': [{'timestamp': l.timestamp, 'text': l.text} 
                          for l in entry.lyrics],
                'timestamp': entry.timestamp,
                'hash_key': entry.hash_key
            }
        
        # Atomic write
        temp_path = self.filepath + ".tmp"
        try:
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.filepath)
            self._pending_writes.clear()
            self._last_flush = time()
        except Exception as e:
            ll.error(f"Failed to flush cache: {e}")
    
    def _save_cache_sync(self):
        """Synchronous cache save for initialization."""
        with open(self.filepath, 'w', encoding='utf-8') as f:
            json.dump({}, f, ensure_ascii=False, indent=2)
    
    def _make_key(self, artist: str, title: str) -> str:
        """Generate cache key with normalization."""
        normalized = f"{artist.strip().lower()}|{title.strip().lower()}"
        if len(normalized) > 200:
            return hashlib.md5(normalized.encode()).hexdigest()
        return normalized
    
    def _generate_hash(self, artist: str, title: str) -> str:
        """Generate content hash for validation."""
        return hashlib.md5(f"{artist}|{title}".encode()).hexdigest()
    
    def close(self):
        """Clean shutdown."""
        self._shutdown.set()
        
        # Final flush
        with self._lock:
            if self._pending_writes:
                self._flush_to_disk()
        
        # Wait for writer thread
        if self._writer_thread and self._writer_thread.is_alive():
            self._writer_thread.join(timeout=2)

class OptimizedLyricHandler:
    """
    High-performance lyric handler using threading for concurrency.
    No asyncio required - works in any synchronous application.
    """
    
    def __init__(self, 
                 api_url: str = "https://lrclib.net/api/get",
                 max_workers: int = 5,  # Reduced to prevent overwhelming API
                 request_timeout: float = 15.0,  # Increased timeout
                 batch_size: int = 3,  # Smaller batches for better reliability
                 rate_limit_rps: float = 3.0):  # More conservative rate limiting
        
        self.cache = OptimizedJSONHandler()
        self.title_cleaner = TitleCleaner()
        self._API_URL = api_url
        self.max_workers = max_workers
        self.request_timeout = request_timeout
        self.batch_size = batch_size
        
        # Rate limiting
        self._rate_limit_interval = 1.0 / rate_limit_rps
        self._last_request_time = 0.0
        self._rate_lock = threading.Lock()
        
        # Thread pool for concurrent requests
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        
        # Session for connection reuse
        self._session = None
        self._session_lock = threading.Lock()
        
        # Request batching
        self._request_queue = Queue()
        self._batch_processor = None
        self._shutdown = threading.Event()
        
        # Title cleaning cache
        self._clean_cache = {}
        self._clean_cache_lock = threading.Lock()
        
        # Compiled regex for timestamp parsing
        self._timestamp_regex = re.compile(r'^\[(\d+):(\d+(?:\.\d+)?)\](.*)$')
        
        self._start_batch_processor()
        atexit.register(self.close)
    
    def _get_session(self):
        """Get or create HTTP session with connection pooling."""
        if self._session is None:
            with self._session_lock:
                if self._session is None:
                    self._session = requests.Session()
                    # Configure for better performance
                    adapter = requests.adapters.HTTPAdapter(
                        pool_connections=self.max_workers,
                        pool_maxsize=self.max_workers * 2,
                        max_retries=2
                    )
                    self._session.mount('http://', adapter)
                    self._session.mount('https://', adapter)
                    self._session.headers.update({
                        'User-Agent': 'OptimizedLyricsFetcher/2.0'
                    })
        return self._session
    
    def _start_batch_processor(self):
        """Start background batch processor."""
        if self._batch_processor is None or not self._batch_processor.is_alive():
            self._batch_processor = threading.Thread(
                target=self._batch_processing_loop, daemon=True
            )
            self._batch_processor.start()
    
    def _batch_processing_loop(self):
        """Background thread for batch processing requests."""
        batch = []
        
        while not self._shutdown.is_set():
            try:
                # Collect batch
                while len(batch) < self.batch_size:
                    try:
                        item = self._request_queue.get(timeout=0.5)  # Increased timeout
                        batch.append(item)
                    except Empty:
                        break
                
                # Process batch if we have items
                if batch:
                    self._process_batch(batch)
                    batch.clear()
                else:
                    sleep(0.1)
                    
            except Exception as e:
                ll.error(f"Batch processor error: {e}")
                # Clear batch on error to prevent infinite loops
                batch.clear()
                sleep(1)
    
    def request(self, artist: str, title: str, callback: Callable, song_id: Any):
        """Queue a lyric request for batch processing."""
        try:
            self._request_queue.put((artist, title, callback, song_id), timeout=1.0)
        except Exception as e:
            ll.error(f"Failed to queue request for {artist} - {title}: {e}")
            # Execute callback with empty result to prevent hanging
            try:
                callback([], song_id)
            except Exception as callback_error:
                ll.error(f"Callback error after queue failure: {callback_error}")
    
    def request_sync(self, artist: str, title: str) -> List[Tuple[float, str]]:
        """Synchronous request that returns lyrics directly."""
        # Check cache first
        cached = self.cache.get(artist, title)
        if cached:
            return [(l.timestamp, l.text) for l in cached]
        
        # Fetch from API with retry logic
        lyrics = self._load_synced_lyrics_sync(artist, title)
        if lyrics:
            # Cache the results
            lyric_entries = [LyricEntry(ts, text) for ts, text in lyrics]
            self.cache.add(artist, title, lyric_entries)
            return lyrics
        
        return []
    
    def request_batch_sync(self, requests: List[Tuple[str, str]]) -> Dict[Tuple[str, str], List[Tuple[float, str]]]:
        """Synchronous batch processing that returns all results."""
        results = {}
        
        # Check cache for all requests
        cache_misses = []
        for artist, title in requests:
            cached = self.cache.get(artist, title)
            if cached:
                results[(artist, title)] = [(l.timestamp, l.text) for l in cached]
            else:
                cache_misses.append((artist, title))
        
        # Process cache misses concurrently
        if cache_misses:
            futures = []
            for artist, title in cache_misses:
                future = self._executor.submit(self._load_synced_lyrics_sync, artist, title)
                futures.append((future, artist, title))
            
            # Collect results with better timeout handling
            for future, artist, title in futures:
                try:
                    lyrics = future.result(timeout=self.request_timeout + 5)
                    if lyrics:
                        lyric_entries = [LyricEntry(ts, text) for ts, text in lyrics]
                        self.cache.add(artist, title, lyric_entries)
                        results[(artist, title)] = lyrics
                    else:
                        results[(artist, title)] = []
                except TimeoutError:
                    ll.error(f"Timeout processing {artist} - {title}")
                    results[(artist, title)] = []
                except Exception as e:
                    ll.error(f"Error processing {artist} - {title}: {e}")
                    results[(artist, title)] = []
        
        return results
    
    def _process_batch(self, batch: List[Tuple[str, str, Callable, Any]]):
        """Process a batch of requests with improved error handling."""
        if not batch:
            return
            
        # Separate cache hits and misses
        cache_hits = []
        api_requests = []
        
        for artist, title, callback, song_id in batch:
            try:
                cached = self.cache.get(artist, title)
                if cached:
                    cache_hits.append((cached, callback, song_id))
                else:
                    api_requests.append((artist, title, callback, song_id))
            except Exception as e:
                ll.error(f"Error checking cache for {artist} - {title}: {e}")
                # Treat as cache miss
                api_requests.append((artist, title, callback, song_id))
        
        # Process cache hits immediately
        for lyrics, callback, song_id in cache_hits:
            try:
                Thread(target=lambda: callback([(l.timestamp, l.text) for l in lyrics], song_id)).start()
            except Exception as e:
                ll.error(f"Callback error for cached lyrics (song_id: {song_id}): {e}")
        
        # Process API requests concurrently with better timeout handling
        if api_requests:
            futures = []
            for artist, title, callback, song_id in api_requests:
                future = self._executor.submit(
                    self._fetch_and_callback, artist, title, callback, song_id
                )
                futures.append((future, artist, title, callback, song_id))
            
            # Wait for completion with more generous timeout
            completed_futures = []
            total_timeout = self.request_timeout + 20
            try:
                completed_futures = list(as_completed(
                    [f[0] for f in futures], 
                    timeout=total_timeout
                ))
            except TimeoutError:
                ll.warn(f"Batch processing timeout after {total_timeout}s - some requests may not complete")
            
            # Handle any unfinished futures
            unfinished_count = 0
            for future, artist, title, callback, song_id in futures:
                if future not in completed_futures:
                    if not future.done():
                        unfinished_count += 1
                        ll.warn(f"Unfinished request for {artist} - {title} (song_id: {song_id})")
                        # Cancel the future if possible
                        future.cancel()
                    
                    # Execute callback with empty result to prevent hanging
                    try:
                        Thread(target=lambda: callback([], song_id)).start()
                    except Exception as e:
                        ll.error(f"Callback error for unfinished request: {e}")
            
            if unfinished_count > 0:
                ll.warn(f"{unfinished_count} (of {len(futures)}) futures unfinished")
            
            # Collect results from completed futures
            for future in completed_futures:
                try:
                    future.result(timeout=1.0)  # Should be immediate since already completed
                except Exception as e:
                    ll.error(f"Error collecting completed future result: {e}")
    
    def _fetch_and_callback(self, artist: str, title: str, callback: Callable, song_id: Any):
        """Fetch lyrics and execute callback with improved error handling."""
        try:
            lyrics = self._load_synced_lyrics_sync(artist, title)
            
            if lyrics:
                # Cache the results
                lyric_entries = [LyricEntry(ts, text) for ts, text in lyrics]
                self.cache.add(artist, title, lyric_entries)
                Thread(target=lambda:callback(lyrics, song_id)).start()
            else:
                Thread(target=lambda:callback([], song_id)).start()
                
        except Exception as e:
            ll.error(f"Error fetching lyrics for {artist} - {title} (song_id: {song_id}): {e}")
            try:
                Thread(target=lambda:callback([], song_id)).start()
            except Exception as callback_error:
                ll.error(f"Callback error after fetch failure: {callback_error}")
    
    def _load_synced_lyrics_sync(self, artist: str, title: str, max_retries: int = 2) -> Optional[List[Tuple[float, str]]]:
        """Synchronous lyrics fetching with rate limiting and retry logic."""
        # Clean titles
        artist_clean, title_clean = self._clean_title_for_lyrics(artist, title)
        
        # Build URL
        artist_q = quote_plus(artist_clean)
        title_q = quote_plus(title_clean)
        url = f"https://lrclib.net/api/get?track_name={title_q}&artist_name={artist_q}"
        
        last_error = None
        
        for attempt in range(max_retries + 1):
            try:
                # Rate limiting
                with self._rate_lock:
                    current_time = time()
                    time_since_last = current_time - self._last_request_time
                    if time_since_last < self._rate_limit_interval:
                        sleep(self._rate_limit_interval - time_since_last)
                    self._last_request_time = time()
                
                session = self._get_session()
                
                # Progressive timeout increase for retries
                timeout = self.request_timeout + (attempt * 5)
                response = session.get(url, timeout=timeout)
                
                if response.status_code == 404:
                    return None
                elif response.status_code == 429:  # Rate limited
                    if attempt < max_retries:
                        wait_time = 2 ** attempt  # Exponential backoff
                        ll.warn(f"Rate limited for {artist} - {title}, waiting {wait_time}s")
                        sleep(wait_time)
                        continue
                    else:
                        ll.error(f"Rate limited for {artist} - {title}, giving up")
                        return None
                
                response.raise_for_status()
                data = response.json()
                
                synced_lyrics = data.get("syncedLyrics", "")
                if synced_lyrics:
                    return self._parse_lyrics_timestamps(synced_lyrics.splitlines())
                return None
                
            except requests.exceptions.Timeout as e:
                last_error = e
                if attempt < max_retries:
                    wait_time = 1 + attempt
                    ll.warn(f"Timeout for {artist} - {title} (attempt {attempt + 1}/{max_retries + 1}), retrying in {wait_time}s")
                    sleep(wait_time)
                    continue
                else:
                    ll.error(f"Final timeout for {artist} - {title} after {max_retries + 1} attempts")
                    
            except requests.exceptions.ConnectionError as e:
                last_error = e
                if attempt < max_retries:
                    wait_time = 2 + attempt
                    ll.warn(f"Connection error for {artist} - {title} (attempt {attempt + 1}/{max_retries + 1}), retrying in {wait_time}s")
                    sleep(wait_time)
                    continue
                else:
                    ll.error(f"Final connection error for {artist} - {title} after {max_retries + 1} attempts")
                    
            except requests.exceptions.RequestException as e:
                last_error = e
                ll.error(f"Request error fetching lyrics for {artist} - {title}: {e}")
                break  # Don't retry on other request errors
                
            except Exception as e:
                last_error = e
                ll.error(f"Unexpected error fetching lyrics for {artist} - {title}: {e}")
                break  # Don't retry on unexpected errors
        
        return None
    
    def _clean_title_for_lyrics(self, artist: str, title: str) -> Tuple[str, str]:
        """Clean title with caching."""
        cache_key = f"{artist}|{title}"
        
        with self._clean_cache_lock:
            if cache_key in self._clean_cache:
                return self._clean_cache[cache_key]
            
            combined = self.title_cleaner.clean(f"{artist} - {title}")
            parts = [p.strip() for p in combined.split(' - ', 1) if p.strip()]
            
            if len(parts) == 2:
                result = (parts[0], parts[1])
            elif len(parts) == 1:
                result = ("", parts[0])
            else:
                result = (artist.strip(), title.strip())
            
            # Cache with size limit
            if len(self._clean_cache) > 1000:
                # Remove oldest half
                items = list(self._clean_cache.items())
                self._clean_cache = dict(items[500:])
            
            self._clean_cache[cache_key] = result
            return result
    
    def _parse_lyrics_timestamps(self, lyrics_list: List[str]) -> List[Tuple[float, str]]:
        """Optimized timestamp parsing."""
        parsed = []
        for line in lyrics_list:
            match = self._timestamp_regex.match(line)
            if match:
                try:
                    minutes = int(match.group(1))
                    seconds = float(match.group(2))
                    text = match.group(3).strip() or "🎵"
                    
                    total_seconds = minutes * 60 + seconds
                    parsed.append((total_seconds, text))
                except (ValueError, IndexError):
                    continue
        
        return sorted(parsed, key=lambda x: x[0])
    
    def close(self):
        """Clean shutdown with improved cleanup."""
        ll.debug("Shutting down OptimizedLyricHandler...")
        
        # Signal shutdown
        self._shutdown.set()
        
        # Close HTTP session
        if self._session:
            try:
                self._session.close()
            except Exception as e:
                ll.error(f"Error closing session: {e}")
        
        # Shutdown executor with timeout
        try:
            self._executor.shutdown(wait=True, timeout=10)
        except Exception as e:
            ll.error(f"Error shutting down executor: {e}")
        
        # Close cache
        try:
            self.cache.close()
        except Exception as e:
            ll.error(f"Error closing cache: {e}")
        
        # Wait for batch processor
        if self._batch_processor and self._batch_processor.is_alive():
            try:
                self._batch_processor.join(timeout=5)
                if self._batch_processor.is_alive():
                    ll.warn("Batch processor thread did not shut down cleanly")
            except Exception as e:
                ll.error(f"Error joining batch processor: {e}")
        
        ll.debug("OptimizedLyricHandler shutdown complete")

# Backwards compatibility - drop-in replacement
class lyricHandler(OptimizedLyricHandler):
    """Drop-in replacement for the original lyricHandler."""
    
    def __init__(self, api_url: str = "https://lrclib.net/api/get", 
                 min_delay_between_calls: float = 1.0):
        # Convert old parameters to new ones
        rate_limit_rps = 1.0 / min_delay_between_calls if min_delay_between_calls > 0 else 10.0
        super().__init__(api_url=api_url, rate_limit_rps=rate_limit_rps)

# Example usage - no asyncio required!
if __name__ == "__main__":
    handler = lyricHandler()
    
    def print_lyrics(lyrics, song_id):
        if lyrics:
            ll.debug(f"🎶 Lyrics for song ID {song_id}:")
            for timestamp, line in lyrics[:3]:  # Show first 3 lines
                ll.debug(f"[{timestamp:.2f}] {line}")
        else:
            ll.warn(f"No lyrics found for song ID {song_id}.")
    
    # Method 1: Async-style requests (non-blocking)
    handler.request("Sleep Token", "Levitate", print_lyrics, 1)
    handler.request("Radiohead", "Creep", print_lyrics, 2)
    
    # Method 2: Synchronous requests (blocking)
    lyrics = handler.request_sync("The Beatles", "Yesterday")
    print(f"Found {len(lyrics)} lyric lines")
    
    # Method 3: Batch synchronous requests
    requests = [("Sleep Token", "Levitate"), ("Radiohead", "Creep")]
    results = handler.request_batch_sync(requests)
    for (artist, title), lyrics in results.items():
        print(f"{artist} - {title}: {len(lyrics)} lines")
    
    # Give async requests time to complete
    sleep(3)  # Increased wait time
    
    handler.close()