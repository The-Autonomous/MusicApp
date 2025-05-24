import os, time, random, platform, pygame, ast, requests
from threading import Event, Thread
from mutagen.mp3 import MP3
from mutagen import File
from pathlib import Path

### YT HANDLER IMPORTS ###

try:
    from ytHandle import ytHandle
    from lyricMaster import lyricHandler
    from radioIpScanner import SimpleRadioScan
    from radioClient import RadioClient
    from radioMaster import RadioHost
except:
    from .ytHandle import ytHandle
    from .lyricMaster import lyricHandler
    from .radioIpScanner import SimpleRadioScan
    from .radioClient import RadioClient
    from .radioMaster import RadioHost

##########################

class SmartShuffler:
    def __init__(self, cache, history_size=50, artist_spacing=2):
        self.cache = list(cache)
        self.history_size = history_size
        self.artist_spacing = artist_spacing
        self.history = []
        self.upcoming = []
        self.replay_queue = []

    def _refill_upcoming(self):
        songs = self.cache.copy()
        random.shuffle(songs)
        # enforce artist_spacing
        for i in range(len(songs)):
            for j in range(1, self.artist_spacing + 1):
                if i + j < len(songs) and songs[i]['artist'] == songs[i + j]['artist']:
                    # swap with a track further ahead
                    for k in range(i + self.artist_spacing + 1, len(songs)):
                        if songs[k]['artist'] != songs[i]['artist']:
                            songs[i + j], songs[k] = songs[k], songs[i + j]
                            break
        self.upcoming = songs

    def enqueue_replay(self, song):
        """
        Immediately queues a specific song to be played next, bypassing shuffle and spacing logic.
        This method inserts the song at the front of the replay queue. Songs in the replay queue
        take priority over all other playback logic, including the upcoming shuffle list and artist spacing rules.
        """
        self.replay_queue.insert(0, song)

    def inject_song(self, song, position=0):
        """
        Injects a song directly into the upcoming queue at the specified position.
        Position 0 means it will be played next (unless a replay is queued).
        """
        if song not in self.upcoming:
            self.upcoming.insert(position, song)

    def get_unique_song(self):
        if self.replay_queue:
            return self.replay_queue.pop(0)

        if not self.upcoming:
            self._refill_upcoming()

        while self.upcoming:
            song = self.upcoming.pop(0)
            if song['path'] in self.history:
                self.upcoming.append(song)
                continue
            # pass spacing/history
            self.history.append(song['path'])
            if len(self.history) > self.history_size:
                self.history.pop(0)
            return song

        # fallback
        return random.choice(self.cache) if self.cache else None

class MusicPlayer:
    def __init__(self, directories, set_screen, set_duration, set_lyrics, set_ips):
        pygame.mixer.init()

        # Playback control events
        self.skip_flag = Event()
        self.pause_event = Event()
        self.repeat_event = Event()
        self.movement_event = Event()
        self.current_player_mode = Event()  # False = MusicPlayer, True = RadioPlayer

        # UI callbacks
        self.set_screen = set_screen
        self.set_duration = set_duration
        self.set_lyrics = set_lyrics
        self.set_ips = set_ips

        # Initialize YouTube and lyric handlers
        self.ytHandle = ytHandle()
        self.lyricHandler = lyricHandler()

        # Cache & Shuffler
        self.cache = []
        self.initialize_cache(directories)
        self.shuffler = SmartShuffler(self.cache)

        # Playback state
        self.current_song = None
        self.song_elapsed_seconds = 0.0
        self.history = []
        self.forward_stack = []
        self.replay_queue = []
        self.current_index = -1
        self.current_volume = 0.1

        # Radio system
        self.full_radio_ip_list = []
        self.current_radio_ip = "0.0.0.0"
        self.radio_client = RadioClient(ip=self.current_radio_ip)
        self.radio_master = RadioHost()
        self.radio_scanner = SimpleRadioScan()

    def initialize_cache(self, directories):
        supported_extensions = ('.mp3', '.wav', '.ogg', '.flac')
        unique_paths = set()  # Track unique paths to avoid duplicates
        for path in directories:
            if path.startswith('http'):
                Thread(target=self.ytDownload, args=(path, directories,)).start()
                continue
            
            if os.path.exists(path):
                for root, _, files in os.walk(path):
                    for file in files:
                        full_path = os.path.join(root, file)
                        if full_path in unique_paths:
                            continue  # Skip duplicates
                        unique_paths.add(full_path)
                        
                        if file.lower().endswith(supported_extensions):
                            metadata = self.get_metadata(full_path)
                            self.cache.append({
                                'path': full_path,
                                'artist': metadata.get('artist', 'Unknown Artist'),
                                'title': metadata.get('title', os.path.splitext(file)[0])
                            })
                            
    def ytDownload(self, url, possibleDirectories):
        returnedPaths = self.ytHandle.parseUrl(url, possibleDirectories)
        for path in returnedPaths:
            filename = os.path.basename(path)
            metadata = self.get_metadata(path)
            self.cache.append({
                'path': path,
                'artist': metadata.get('artist', 'Unknown Artist'),
                'title': metadata.get('title', os.path.splitext(filename)[0])
            })
        print(f"⏬ Download Completed: {url}")

    def get_metadata(self, file_path):
        try:
            audio = File(file_path, easy=True)
            return {
                'artist': audio.get('artist', ['Unknown Artist'])[0],
                'title': audio.get('title', [os.path.splitext(os.path.basename(file_path))[0]])[0]
            }
        except:
            return {
                'artist': 'Unknown Artist',
                'title': os.path.splitext(os.path.basename(file_path))[0]
            }
 
    def pause(self):
        if self.pause_event.is_set():
            self.pause_event.clear()
            pygame.mixer.music.unpause()
            title = self.current_song['title'] if not self.repeat_event.is_set() else f"{self.current_song['title']} *+*" # Handle Pause Edgecase
            self.set_screen(self.current_song['artist'], title)
        else:
            self.pause_event.set()
            pygame.mixer.music.pause()
            self.set_screen(self.current_song['artist'], f"{self.current_song['title']} *=*")
            
    def repeat(self):
        if self.pause_event.is_set() or self.movement_event.is_set():
            return
        if self.repeat_event.is_set():
            self.repeat_event.clear()
            self.set_screen(self.current_song['artist'], self.current_song['title'])
        else:
            self.repeat_event.set()
            self.set_screen(self.current_song['artist'], f"{self.current_song['title']} *+*")

    def before_move(self):
        if self.movement_event.is_set():
            return False
        self.movement_event.set()
        self.cachedRepeatValue = self.repeat_event.is_set()
        self.repeat_event.clear()
        
    def after_move(self):
        time.sleep(0.2)
        if self.cachedRepeatValue == True:
            self.repeat_event.set()
        self.cachedRepeatValue = False
        self.movement_event.clear()

    def skip_next(self):
        if self.before_move() == False:
            return
        if self.forward_stack:
            next_song = self.forward_stack.pop()
            self.current_index += 1
            self._queue_song(next_song)
        else:
            self._clear_for_new_track()
        self.after_move()

    def skip_previous(self):
        if self.before_move() == False:
            return
        if self.current_index > 0:
            self.forward_stack.append(self.history[self.current_index])
            self.current_index -= 1
            self._queue_song(self.history[self.current_index])
        self.after_move()
            
    def set_volume(self, direction: int = 0):
        """Set volume between 0.0 (silent) and 1.0 (full volume)"""
        self.current_volume = round(sorted([0.0, self.current_volume + direction, 1.0])[1], 2)
        pygame.mixer.music.set_volume(self.current_volume)
        print(f"🔊 {self.current_volume}")
        
    def up_volume(self):
        self.set_volume(0.05)
        
    def dwn_volume(self):
        self.set_volume(-0.05)


    def _clear_for_new_track(self):
        self.skip_flag.set()
        pygame.mixer.music.stop()
        self.forward_stack = []
        self.replay_queue = []  # Clear queue for new selection
        if self.current_index < len(self.history) - 1:
            self.history = self.history[:self.current_index+1]

    
    def _queue_song(self, song):
        self.skip_flag.set()
        pygame.mixer.music.stop()
        self.shuffler.enqueue_replay(song)

    def get_unique_song(self):
        # Delegate to SmartShuffler
        return self.shuffler.get_unique_song()

    def resetRadio(self):
        try:
            self.radio_client.stopListening()
        except:
            pass
        del self.radio_client
        self.radio_client = RadioClient(ip=self.current_radio_ip)

    def core_handler(self):
        Thread(target=self.core_player_loop, daemon=True).start()
        Thread(target=self.load_radio_ips, daemon=True).start()
        Thread(target=self.core_radio_loop, daemon=True).start()
        
    def toggle_loop_cycle(self, CycleType: bool = None):
        """Toggle Based On The Bool: True = RadioPlayer, False = MusicPlayer"""
        didReset = False
        if CycleType is None:
            didReset = True
            CycleType = self.current_player_mode.is_set()
        self.current_player_mode.set() if CycleType else self.current_player_mode.clear()
        self.resetRadio()
        self.set_lyrics(False)
        pauseType = self.pause_event.is_set()
        if CycleType:
            if not pauseType:
                self.pause()
        else:
            with self.radio_client._can_have_pygame:
                try:
                    pygame.mixer.music.stop()
                    pygame.mixer.music.unload()
                except:
                    print("Couldn't Wait For Pygame Mixer. Continue...")
                pygame.mixer.music.load(self.current_song['path'])
                self.pause()
                #pygame.mixer.music.set_volume(self.current_volume) I dont think this is necessary
                try:
                    pygame.mixer.music.set_pos(self.song_elapsed_seconds)
                except:
                    try:
                        pygame.mixer.music.play()
                        pygame.mixer.music.set_pos(self.song_elapsed_seconds)
                        pygame.mixer.music.unpause()
                    except:
                        print("Error In Loading Music In Radio. Retrying")
                        if not didReset:
                            return self.toggle_loop_cycle(CycleType)
            
    def load_radio_ips(self, seconds_to_scan: int = 60):
        """
        Every seconds_to_scan (Default 60) Scan The Full List Of Available Radios
        Run Only In A Seperate Daemon Thread
        """
        def handle_callback_ip(ip, title, location):
            if not ip in self.full_radio_ip_list and not ip.__contains__("0.0.0.0"):
                self.full_radio_ip_list.append(ip)
                if self.current_radio_ip == "0.0.0.0":
                    self.current_radio_ip = ip
                self.set_ips(self.full_radio_ip_list)
        
        while True:
            self.radio_scanner.scan_all(handle_callback_ip)
            time.sleep(seconds_to_scan)

    def set_radio_ip(self, new_ip):
        if new_ip in self.full_radio_ip_list:
            self.toggle_loop_cycle()
            self.current_radio_ip = new_ip
            self.toggle_loop_cycle()
            return True
        else:
            return False

    def core_radio_loop(self):
        def lyric_callback(unformatted_return_lyrics: str, return_dilation, local_song_id):
            self.current_radio_id = local_song_id
            try:
                attemptCount = 0
                while (attemptCount := attemptCount + 1) <= 5:
                    try:
                        lyric_data = requests.get(unformatted_return_lyrics, timeout=2)
                        lyric_data.raise_for_status()
                        if lyric_data.content != "b''":
                            print(f"Lyrics downloaded.")
                            break
                        else:
                            time.sleep(1)
                    except:
                        time.sleep(1)
                return_lyrics = ast.literal_eval(lyric_data.content.decode('utf-8'))
                if len(return_lyrics) > 0:
                    self.set_lyrics(True, "🎵")
                    for lyric_pair in return_lyrics:
                        if not local_song_id == self.current_radio_id:
                            break
                        if not self.current_player_mode.is_set():
                            self.set_lyrics(False)
                            break
                        while pygame.mixer.music.get_pos()/1000 + return_dilation < lyric_pair[0]: # While Less Than Required Time For Lyrics To Show
                            if not self.current_player_mode.is_set() or not local_song_id == self.current_radio_id:
                                break
                            time.sleep(0.1)
                        self.set_lyrics(True, lyric_pair[1])
                else:
                    self.set_lyrics(False)
            except Exception as E:
                print(f"Radio Lyric Callback Error With Data: {unformatted_return_lyrics} And Dilation {return_dilation:.2f}s And Error {E}")
                
        while True:
            while not self.current_player_mode.is_set() or self.current_radio_ip == "0.0.0.0":
                time.sleep(0.1)
            while True:
                try:
                    listeningIp = self.current_radio_ip
                    self.current_radio_id = ""
                    self.radio_client.listenTo(listeningIp, lyric_callback)
                    break
                except:
                    time.sleep(0.1)
            
            
            self.set_lyrics(False)
            
            while True:
                if not self.current_player_mode.is_set() or listeningIp != self.current_radio_ip:
                    break
                RadioData = self.radio_client.client_data
                self.set_duration(*RadioData['radio_duration'])
                self.set_screen(*RadioData['radio_text'].split("![]!"))
                time.sleep(0.1)
            try:
                self.radio_client.stopListening()
            except:
                pass

    def core_player_loop(self):
        prev_song = None
        while True:
            self.skip_flag.clear()
            song = self.get_unique_song() if not self.repeat_event.is_set() or prev_song is None else prev_song
            prev_song = song
            if not song:
                time.sleep(0.5)
                continue

            # history and played lists maintained in shuffler, so skip duplicates here
            self.current_song = song
            self.current_song_id = str(song['title']) + str(time.time()) # Create A Unique Song Idea For Lyrics To Track
            title = song['title']
            self.set_screen(song['artist'], title)
            self.song_elapsed_seconds = 0.0
            self.current_song_lyrics = ""
            
            current_rotation_count, max_current_rotation = 0, 5 # Count How Many Rotations Of Loop Have Occured Before Syncing The Radio Host With The Internal Media Player
            
            fullTitle = f"{song['artist']}![]!{title}"
            
            def lyric_callback(return_lyrics, local_song_id):
                if not local_song_id == self.current_song_id:
                    return
                self.current_song_lyrics = return_lyrics
                if len(return_lyrics) > 0:
                    self.set_lyrics(True, "🎵")
                    for lyric_pair in return_lyrics:
                        if not local_song_id == self.current_song_id:
                            self.set_lyrics(False)
                            break
                        while self.song_elapsed_seconds < lyric_pair[0]: # While Less Than Required Time For Lyrics To Show
                            if not local_song_id == self.current_song_id:
                                break
                            time.sleep(0.1)
                        self.set_lyrics(True, lyric_pair[1])
                else:
                    self.set_lyrics(False)
                    
            # lyric thread
            Thread(target=self.lyricHandler.request,
                   args=(song['artist'], song['title'], lyric_callback, self.current_song_id)).start()

            try:
                pygame.mixer.music.load(song['path'])
                pygame.mixer.music.set_volume(self.current_volume)
                pygame.mixer.music.play()
                audio = MP3(song['path'])
                total_duration = audio.info.length

                start_time = time.time()
                paused_duration = 0
                
                if current_rotation_count == 0:
                    self.radio_master.initSong(
                        title = fullTitle,
                        mp3_song_file_path = song['path'],
                        current_pymixer = pygame.mixer.music,
                        current_song_lyrics=self.current_song_lyrics
                    )
                        
                current_rotation_count = (current_rotation_count + 1) % max_current_rotation # Add One Else Loop Back
                
                while time.time() - start_time - paused_duration < total_duration:
                    if self.skip_flag.is_set(): break
                    if self.pause_event.is_set():
                        self.radio_master.initSong(
                            title = f"{fullTitle}***[]*Paused",
                            mp3_song_file_path = song['path'],
                            current_pymixer = pygame.mixer.music,
                            current_song_lyrics=self.current_song_lyrics
                        )
                        pause_start = time.time()
                        pygame.mixer.music.pause()
                        while self.pause_event.is_set():
                            if self.skip_flag.is_set(): break
                            time.sleep(0.1)
                        paused_duration += time.time() - pause_start
                        pygame.mixer.music.unpause()
                    self.song_elapsed_seconds = time.time() - start_time - paused_duration
                    self.set_duration(self.song_elapsed_seconds, total_duration)
                    time.sleep(0.1)

            except Exception as e:
                self.set_screen("Error", song['title'])
                print(e)
                time.sleep(1)
            finally:
                pygame.mixer.music.stop()
                self.current_song = None
                            
def get_auto_directories(candidate_urls=[]):
    """Automatically detects existing GTAV Enhanced User Music directories"""
    valid_dirs = candidate_urls
    
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
    
    # Check which directories actually exist
    for path in candidate_paths:
        if path.exists() and path.is_dir():
            valid_dirs.append(str(path.resolve()))

    return valid_dirs