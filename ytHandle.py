import os, re, sys, subprocess, yt_dlp
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1
from yt_dlp.postprocessor import SponsorBlockPP
from functools import lru_cache
import tkinter as tk
from tkinter import ttk

try:
    from log_loader import log_loader
except:
    from .log_loader import log_loader

### Logging Handler ###

ll = log_loader("YT Download")

#######################

class DownloadPopup:
    def __init__(self):
        pass
    
    def popup_process(self, close_event, progress_value):
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes('-topmost', True)
        root.configure(bg="#111")

        width, height = 360, 120
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        x = (screen_width - width) // 2
        y = (screen_height - height) // 2
        root.geometry(f"{width}x{height}+{x}+{y}")

        canvas = tk.Canvas(root, width=width, height=height, bg="#111", highlightthickness=0)
        canvas.pack(fill='both', expand=True)

        canvas.create_rectangle(5, 5, width-5, height-5, fill="#1c1c1c", outline="#333", width=2)

        frame = tk.Frame(canvas, bg="#1c1c1c")
        frame.place(relwidth=0.95, relheight=0.9, relx=0.025, rely=0.05)

        label = tk.Label(frame, text="Downloading Music...", fg="#00FFAA", bg="#1c1c1c", font=("Segoe UI", 12, "bold"))
        label.pack(pady=(10, 0))

        percent_label = tk.Label(frame, text="0%", fg="#AAAAAA", bg="#1c1c1c", font=("Segoe UI", 10))
        percent_label.pack()

        style = ttk.Style()
        style.theme_use('clam')
        style.configure("Custom.Horizontal.TProgressbar",
                        troughcolor="#2e2e2e",
                        bordercolor="#2e2e2e",
                        background="#00FFAA",
                        lightcolor="#00FFAA",
                        darkcolor="#00FFAA",
                        thickness=10)

        progress = ttk.Progressbar(frame, mode="determinate", length=280, style="Custom.Horizontal.TProgressbar")
        progress.pack(pady=(5, 10))
        progress['maximum'] = 100

        def update_progress():
            if close_event.is_set():
                root.destroy()
            else:
                value = max(0, min(1, progress_value.value)) * 100
                progress['value'] = value
                percent_label.config(text=f"{int(value)}%")
                root.after(100, update_progress)

        root.after(100, update_progress())
        root.mainloop()

class ytHandle:
    def __init__(self, max_workers=8, sponsorblock_categories=None):
        self._check_dependencies()
        self.max_workers = max_workers
        self.max_filename_length = 120
        self.sponsorblock_categories = sponsorblock_categories or [
            'sponsor', 'intro', 'outro', 'selfpromo', 
            'preview', 'filler', 'music_offtopic'
        ]
        
        # Pre-compile regex patterns for better performance
        self._clean_regex = re.compile(r'[^\w _\-()&]')
        self._space_regex = re.compile(r'\s+')
        
        # Cache for YoutubeDL instances to avoid recreation overhead
        self._ydl_cache = {}
        
        ll.debug(f"Initialized download handler with {max_workers} parallel workers")

    def _check_dependencies(self):
        try:
            __import__('mutagen')
        except ImportError:
            subprocess.check_call(
                [sys.executable, '-m', 'pip', 'install', 'mutagen'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

    @lru_cache(maxsize=32)
    def _get_cached_ydl_opts(self, extract_flat=False):
        """Cache YoutubeDL options to avoid dict recreation"""
        if extract_flat:
            return {'extract_flat': True, 'quiet': True, 'no_warnings': True}
        
        return {
            'format': 'bestaudio/best',
            'postprocessors': [
                {
                    'key': 'SponsorBlock',
                    'categories': self.sponsorblock_categories
                },
                {
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',  # Fixed quality for consistency
                }
            ],
            'quiet': True,
            'no_warnings': True,
            'sponsorblock_mark': 'all',
            'sponsorblock_remove': 'all',
        }

    def parseUrl(self, url, possible_directories):
        """Process a YouTube playlist and return downloaded file paths"""
        ll.debug(f"\n▶ Processing playlist: {url}")
        
        # Early validation and setup
        target_dir = self._find_valid_directory(possible_directories)
        if not target_dir:
            ll.error("⚠️ No valid download directory found!")
            return []
            
        ll.debug(f"📂 Using directory: {target_dir}")
        
        # Parallel directory scanning for existing files
        existing = self._get_existing_filenames_parallel(possible_directories)
        ll.debug(f"🔍 Found {len(existing)} existing tracks")
        
        try:
            # Batch fetch playlist data with optimized settings
            tracks = self._get_playlist_tracks_optimized(url)
            ll.debug(f"🎵 Playlist has {len(tracks)} tracks")
        except Exception as e:
            ll.error(f"🚨 Playlist error: {str(e)}")
            return []
        
        # Filter new tracks with set intersection (faster than list comprehension)
        track_names = {t['safe_name'] for t in tracks}
        new_track_names = track_names - existing
        new_tracks = [t for t in tracks if t['safe_name'] in new_track_names]
        
        ll.debug(f"🆕 Found {len(new_tracks)} new tracks\n")
        
        if not new_tracks:
            return []

        # Optimized parallel downloading with better resource management
        return self._download_tracks_optimized(new_tracks, target_dir)

    def _find_valid_directory(self, dirs):
        """Find first writable directory with improved validation"""
        for d in dirs:
            # Skip invalid directory inputs
            if not d:
                continue
            if isinstance(d, str) and ('://' in d or not d.strip()):
                continue
                
            try:
                path = Path(d).expanduser().resolve()
                
                # Create directory if it doesn't exist
                path.mkdir(parents=True, exist_ok=True)
                
                # Check if it's a directory and writable using os.access (more reliable)
                if path.is_dir() and os.access(path, os.W_OK):
                    ll.debug(f"✅ Valid directory found: {path}")
                    return path
                else:
                    ll.debug(f"❌ Directory not writable: {path}")
                    
            except Exception as e:
                ll.debug(f"❌ Directory error for '{d}': {str(e)}")
                continue
                
        ll.error(f"❌ No valid directories found from: {dirs}")
        return None

    def _get_existing_filenames_parallel(self, dirs):
        """Parallel scan of directories for existing files"""
        def scan_directory(d):
            if isinstance(d, str) and '://' in d:
                return set()
            try:
                dir_path = Path(d).expanduser()
                if dir_path.exists() and dir_path.is_dir():
                    # Use glob with stem extraction in one pass
                    return {f.stem for f in dir_path.glob('*.mp3')}
            except:
                pass
            return set()
        
        # Use smaller thread pool for I/O operations
        with ThreadPoolExecutor(max_workers=min(4, len(dirs))) as executor:
            futures = [executor.submit(scan_directory, d) for d in dirs]
            existing = set()
            for future in as_completed(futures):
                existing.update(future.result())
        
        return existing

    def _get_playlist_tracks_optimized(self, url):
        """Optimized playlist data fetching"""
        opts = self._get_cached_ydl_opts(extract_flat=True)
        
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get('entries', [])
            
            # Batch process entries with pre-compiled regex
            tracks = []
            for entry in entries:
                if entry and entry.get('id'):
                    track = self._clean_track_data_optimized(entry)
                    if track:
                        tracks.append(track)
            
            return tracks

    def _clean_track_data_optimized(self, entry):
        """Optimized track data cleaning with pre-compiled regex"""
        title = entry.get('title', 'Untitled')
        if not title:
            return None
            
        # Use pre-compiled regex patterns
        clean = self._clean_regex.sub('', title)
        clean = self._space_regex.sub(' ', clean).strip()
        
        if not clean:
            clean = 'Untitled'
            
        return {
            'url': f"https://youtube.com/watch?v={entry['id']}",
            'title': title,
            'safe_name': clean[:self.max_filename_length],
            'uploader': entry.get('uploader'),
            'id': entry['id']  # Keep ID for potential caching
        }

    def _download_tracks_optimized(self, tracks, target_dir):
        """Optimized parallel downloading with better resource management"""
        results = []
        
        # Use as_completed for better progress tracking and early results
        try:
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                # Submit all tasks with error handling
                future_to_track = {}
                for track in tracks:
                    try:
                        future = executor.submit(self._download_track_optimized, track, target_dir)
                        future_to_track[future] = track
                    except RuntimeError as e:
                        if "interpreter shutdown" in str(e):
                            ll.error("🛑 Interpreter shutting down, stopping downloads")
                            break
                        else:
                            ll.error(f"⚠️ Failed to submit: {track['safe_name']} - {str(e)}")
                
                # Process completed downloads as they finish
                completed_count = 0
                for future in as_completed(future_to_track):
                    completed_count += 1
                    track = future_to_track[future]
                    try:
                        result = future.result(timeout=30)  # Add timeout
                        if result:
                            ll.debug(f"✅ Downloaded ({completed_count}/{len(future_to_track)}): {Path(result).name}")
                            results.append(result)
                        else:
                            ll.error(f"⚠️ Failed ({completed_count}/{len(future_to_track)}): {track['safe_name']}")
                    except Exception as e:
                        ll.error(f"💥 Exception ({completed_count}/{len(future_to_track)}): {track['safe_name']} - {str(e)}")
                
                ll.debug(f"\n🔥 Success: {len(results)}/{len(future_to_track)} downloaded")
                
        except Exception as e:
            ll.error(f"🚨 Download pool error: {str(e)}")
            
        return results

    def _download_track_optimized(self, track, target_dir):
        """Optimized single track download"""
        mp3_path = target_dir / f"{track['safe_name']}.mp3"
        
        # Quick existence check
        if mp3_path.exists():
            return None

        try:
            ll.debug(f"⏬ Starting: {track['safe_name']}")
            
            # Use cached options and set output template
            ydl_opts = self._get_cached_ydl_opts().copy()
            ydl_opts['outtmpl'] = str(mp3_path.with_suffix(''))
            
            # Create YoutubeDL instance with optimized settings
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Add SponsorBlock processor
                ydl.add_post_processor(SponsorBlockPP(ydl))
                ydl.download([track['url']])
            
            # Verify download and add metadata
            if mp3_path.exists():
                self._set_basic_tags_optimized(mp3_path, track)
                return str(mp3_path)
            else:
                ll.error(f"⚠️ File not found after download: {track['safe_name']}")
                return None
                
        except yt_dlp.DownloadError as e:
            error_msg = str(e).lower()
            if "private" in error_msg or "unavailable" in error_msg:
                ll.error(f"🔒 Video unavailable: {track['safe_name']}")
            elif "copyright" in error_msg:
                ll.error(f"📵 Copyright issue: {track['safe_name']}")
            elif "age" in error_msg:
                ll.error(f"🔞 Age restricted: {track['safe_name']}")
            else:
                ll.error(f"🚫 Download failed: {track['safe_name']} - {str(e)}")
            return None
        except Exception as e:
            ll.error(f"💥 Unexpected error: {track['safe_name']} - {str(e)}")
            return None
        
    def _set_basic_tags_optimized(self, path, track):
        """Optimized metadata setting with error handling"""
        try:
            audio = MP3(path, ID3=ID3)
            
            # Only add tags if they don't exist to avoid overwrites
            if not audio.tags:
                audio.add_tags()
            
            # Set title
            if track.get('title'):
                audio.tags.add(TIT2(encoding=3, text=track['title']))
            
            # Set artist if available
            if track.get('uploader'):
                audio.tags.add(TPE1(encoding=3, text=track['uploader']))
            
            audio.save(v2_version=3)
            
        except Exception as e:
            ll.debug(f"⚠️ Metadata warning for {path.name}: {str(e)}")