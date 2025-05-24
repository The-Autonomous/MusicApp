import os, re, sys, subprocess, yt_dlp
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1
from yt_dlp.postprocessor import SponsorBlockPP

class ytHandle:
    def __init__(self, max_workers=4, sponsorblock_categories=None):
        self._check_dependencies()
        self.max_workers = max_workers
        self.max_filename_length = 120
        self.sponsorblock_categories = sponsorblock_categories or [
            'sponsor', 'intro', 'outro', 'selfpromo', 
            'preview', 'filler', 'music_offtopic'
        ]
        print(f"Initialized download handler with {max_workers} parallel workers")

    def _check_dependencies(self):
        try:
            __import__('mutagen')
        except ImportError:
            subprocess.check_call(
                [sys.executable, '-m', 'pip', 'install', 'mutagen'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

    def parseUrl(self, url, possible_directories):
        """Process a YouTube playlist and return downloaded file paths"""
        print(f"\n▶ Processing playlist: {url}")
        target_dir = self._find_valid_directory(possible_directories)
        
        if not target_dir:
            print("⚠️ No valid download directory found!")
            return []
            
        print(f"📂 Using directory: {target_dir}")
        
        existing = self._get_existing_filenames(possible_directories)
        print(f"🔍 Found {len(existing)} existing tracks")
        
        try:
            tracks = self._get_playlist_tracks(url)
            print(f"🎵 Playlist has {len(tracks)} tracks")
        except Exception as e:
            print(f"🚨 Playlist error: {str(e)}")
            return []
            
        new_tracks = [t for t in tracks if t['safe_name'] not in existing]
        print(f"🆕 Found {len(new_tracks)} new tracks\n")
        
        if not new_tracks:
            return []

        with ThreadPoolExecutor(self.max_workers) as executor:
            futures = [executor.submit(self._download_track, t, target_dir) for t in new_tracks]
            results = []
            
            for i, future in enumerate(futures, 1):
                result = future.result()
                if result:
                    print(f"✅ Downloaded ({i}/{len(new_tracks)}): {Path(result).name}")
                    results.append(result)
                else:
                    print(f"⚠️ Failed ({i}/{len(new_tracks)}): {new_tracks[i-1]['safe_name']}")
            
            print(f"\n🔥 Success: {len(results)}/{len(new_tracks)} downloaded")
            return results

    def _find_valid_directory(self, dirs):
        """Find first writable directory"""
        for d in dirs:
            if isinstance(d, str) and ('://' in d or not d.strip()):
                continue
            path = Path(d).expanduser().resolve()
            try:
                path.mkdir(parents=True, exist_ok=True)
                if path.is_dir() and os.access(path, os.W_OK):
                    return path
            except:
                continue
        return None

    def _get_existing_filenames(self, dirs):
        """Get existing track names"""
        existing = set()
        for d in dirs:
            if isinstance(d, str) and '://' in d:
                continue
            dir_path = Path(d).expanduser()
            if dir_path.exists():
                existing.update(f.stem for f in dir_path.glob('*.mp3'))
        return existing

    def _get_playlist_tracks(self, url):
        """Fetch and clean playlist data"""
        with yt_dlp.YoutubeDL({'extract_flat': True, 'quiet': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            return [self._clean_track_data(e) for e in info.get('entries', []) if e]

    def _clean_track_data(self, entry):
        """Sanitize track information"""
        clean = re.sub(r'[^\w _\-()&]', '', entry.get('title', 'Untitled'))
        clean = re.sub(r'\s+', ' ', clean).strip()
        return {
            'url': f"https://youtube.com/watch?v={entry['id']}",
            'title': entry.get('title', 'Untitled'),
            'safe_name': clean[:self.max_filename_length],
            'uploader': entry.get('uploader')
        }

    def _download_track(self, track, target_dir):
        """Download track with SponsorBlock skipping"""
        mp3_path = target_dir / f"{track['safe_name']}.mp3"
        if mp3_path.exists():
            return None

        try:
            print(f"⏬ Starting: {track['safe_name']}")
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': str(mp3_path.with_suffix('')),
                'postprocessors': [
                    {
                        'key': 'SponsorBlock',
                        'categories': self.sponsorblock_categories
                    },
                    {
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                    }
                ],
                'quiet': True,
                'sponsorblock_mark': 'all',  # Remove instead of mark
                'sponsorblock_remove': 'all',  # Actually remove segments
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Add SponsorBlock processor
                ydl.add_post_processor(SponsorBlockPP(ydl))
                ydl.download([track['url']])
            
            if mp3_path.exists():
                self._set_basic_tags(mp3_path, track)
                return str(mp3_path)
            return None
        except Exception as e:
            print(f"💥 Error: {track['safe_name']} - {str(e)}")
            return None
        
    def _set_basic_tags(self, path, track):
        """Add ID3 metadata"""
        try:
            audio = MP3(path, ID3=ID3)
            audio.tags.add(TIT2(encoding=3, text=track['title']))
            if track.get('uploader'):
                audio.tags.add(TPE1(encoding=3, text=track['uploader']))
            audio.save()
        except:
            pass