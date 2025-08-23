import os, socket, psutil, threading, time
from flask import Flask, send_from_directory, make_response, jsonify, request
from flask_compress import Compress
from waitress import serve
from mutagen.mp3 import MP3 # Import MP3 to get audio duration
from mutagen.wave import WAVE # Import WAVE to get audio duration for WAV files
from itertools import islice

try:
    from log_loader import log_loader, OutputRedirector
except:
    from .log_loader import log_loader, OutputRedirector

### Logging Handler ###

ll = log_loader("Radio Master")

#######################

class RadioHost:
    def __init__(self, player, port=8080, ip=None, fake_load=False):
        # Flask app setup
        self.MusicPlayer = player
        self.app_pad_site = "app_pad"
        self.app = Flask(__name__, static_folder='app_pad')
        Compress(self.app)

        # Shared state
        self.current_data = {
            'title': '',
            'mp3_path': '', # Renamed for clarity to include WAV files later
            'lyrics': '',
            'mixer': None,
            'duration': 0.0, # Add duration to current_data
            'buffered_at': time.time() # Add buffered_at timestamp
        }

        def get_pos():
            """Get current song position."""
            try:
                mix = self.current_data['mixer']
                return mix.get_pos() if mix else 0
            except Exception as e:
                ll.debug(f"Exception getting position from mixer: {e}")
                return 0

        # Routes
        @self.app.route('/')
        def index():
            # Construct the full URL for the song
            song_url = f"http://{self._get_local_ip()}:{port}/song"
            eq_data = self.MusicPlayer.get_bands() if hasattr(self.MusicPlayer, 'get_bands') else {}
            eq_string = ','.join(f"{k}:{v}" for k,v in eq_data.items())

            resp = make_response(
                f"<title>{self.current_data['title']}</title>"
                f"<paused>{self.MusicPlayer.pause_event.is_set()}</paused>"
                f"<repeat>{self.MusicPlayer.repeat_event.is_set()}</repeat>"
                f"<eq>{eq_string}</eq>"
                f"<volume>{self.MusicPlayer.current_volume}</volume>"
                f"<location>{get_pos()}</location>"
                f"<duration>{self.current_data['duration']}</duration>" # Added duration
                f"<url>{song_url}</url>" # Added song URL
                f"<buffered_at>{self.current_data['buffered_at']}</buffered_at>" # Added buffered_at
                f"<script>location.href='/{self.app_pad_site}';</script>"
            )
            resp.headers['Cache-Control'] = 'no-store'
            return resp

        @self.app.route(f'/{self.app_pad_site}') # App Pad Loader
        def app_pad():
            return self.app.send_static_file('index.html')
        
        @self.app.route(f'/{self.app_pad_site}/logs') # App Pad Loader
        def app_log_pad():
            return self.app.send_static_file('log.html')
        
        serve_log_path = OutputRedirector.filename
        @self.app.route('/logs/api')
        def serve_log_chunk():
            if not os.path.isfile(serve_log_path):
                return jsonify({'error': 'Log file not found'}), 404

            try:
                start = int(request.args.get('start', 0))
                count = min(int(request.args.get('count', 100)), 5000)  # max 5000 lines
                if start < 0 or count < 1:
                    return jsonify({'error': 'Invalid start or count'}), 400

                with open(serve_log_path, 'r', encoding='utf-8', errors='replace') as f:
                    lines = list(islice(f, start, start + count))

                has_more = len(lines) == count

                return jsonify({
                    'lines': [line.rstrip('\n') for line in lines],
                    'start': start,
                    'count': len(lines),
                    'has_more': has_more
                })

            except Exception as e:
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/search', methods=['POST'])
        def handle_search():
            data = request.get_json() or {}
            query = data.get('query', '').strip()
            if not query:
                return jsonify({ 'code': 'error', 'message': 'Empty search query' }), 400

            # Perform search via your MusicPlayer helper
            raw_results = self.MusicPlayer.get_search_term(query)
            # raw_results is list of (title, path)
            results = [{ 'title': title, 'path': path } for title, path in raw_results]
            return jsonify({ 'code': 'success', 'results': results })
        
        @self.app.route('/action', methods=['POST'])
        def handle_action():
            data = request.get_json()
            action = data.get('action', '').lower()

            if action == 'pause':
                self.MusicPlayer.pause()
                
            elif action == 'play':
                self.MusicPlayer.pause(forcedState=True)  # Explicitly unpause

            elif action == 'skip':
                self.MusicPlayer.skip_next()

            elif action == 'previous':
                self.MusicPlayer.skip_previous()
                
            elif action == 'volume_up':
                self.MusicPlayer.up_volume()
                
            elif action == 'volume_down':
                self.MusicPlayer.dwn_volume()
            
            elif action == 'repeat':
                self.MusicPlayer.repeat()
                
            elif action == 'play_search':
                path = data.get('path')
                if not path:
                    return jsonify({ 'code': 'error', 'message': 'No path provided' }), 400
                self.MusicPlayer.play_song(path)
                
            elif action == 'status':
                pass  # just return the current state

            else:
                return jsonify({"code": "error", "message": "Invalid action"}), 400

            return jsonify({
                "code": "success",
                "title": self.current_data["title"],
                "position": round(get_pos(), 2),
                "paused": self.MusicPlayer.pause_event.is_set(),
                "repeat": self.MusicPlayer.repeat_event.is_set(),
                "volume": round(self.MusicPlayer.current_volume, 2)
            })

        @self.app.route('/song')
        def serve_song():
            mp3_path = self.current_data['mp3_path']
            if not mp3_path or not os.path.isfile(mp3_path):
                return "No song loaded", 404
            directory, filename = os.path.split(mp3_path)
            return send_from_directory(directory, filename, mimetype='audio/mpeg' if filename.lower().endswith('.mp3') else 'audio/wav')

        @self.app.route('/lyrics')
        def serve_lyrics():
            return self.current_data['lyrics'] or "No lyrics available"

        @self.app.after_request
        def add_no_cache_headers(response):
            response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            response.headers['Pragma']        = 'no-cache'
            response.headers['Expires']       = '0'
            return response

        if not fake_load:
            if not ip:
                self._free_port(port)
                host = self._get_local_ip()
            else:
                host = ip
            self._server_thread = threading.Thread(
                target=serve,
                args=(self.app,),
                kwargs={
                    'host': host, 
                    'port': port,
                    'threads': 2,  # Reduce from default 4-6 threads
                    'connection_limit': 10,  # Limit concurrent connections
                    'cleanup_interval': 15,  # Cleanup inactive connections every 15s
                    'channel_timeout': 120,  # Timeout for inactive channels
                    'log_untrusted_proxy_headers': False,  # Reduce logging overhead
                    'send_bytes': 8192,  # Optimize buffer size
                    'recv_bytes': 8192,
                    'asyncore_use_poll': True,  # Use poll instead of select (Linux/Mac)
                },
                daemon=True
            )
            self._server_thread.start()
            ll.debug(f"Serving on http://{host}:{port}")

    def initSong(self, title, mp3_song_file_path, current_mixer, current_song_lyrics=""):
        """Call whenever you load a new track."""
        song_duration = 0.0
        if os.path.exists(mp3_song_file_path):
            try:
                if mp3_song_file_path.lower().endswith('.mp3'):
                    audio = MP3(mp3_song_file_path)
                    song_duration = audio.info.length
                elif mp3_song_file_path.lower().endswith('.wav'):
                    # For WAV files, you can use WAVE from mutagen or scipy.io.wavfile.read
                    # Using WAVE from mutagen for consistency with MP3 duration fetching
                    audio = WAVE(mp3_song_file_path)
                    song_duration = audio.info.length
            except Exception as e:
                ll.error(f"Error getting duration for {mp3_song_file_path}: {e}")

        # Update data
        self.current_data.update({
            'title': title,
            'mp3_path': mp3_song_file_path,
            'lyrics': current_song_lyrics,
            'mixer': current_mixer,
            'duration': song_duration, # Set the actual duration
            'buffered_at': time.time() # Update buffered_at to current time on new song
        })

    def _get_local_ip(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('10.255.255.255', 1))
            ip = s.getsockname()[0]
        except Exception:
            ip = '127.0.0.1'
        finally:
            s.close()
        return ip

    def _free_port(self, port):
        """Kill any process listening on `port` (requires admin/sudo)."""
        for conn in psutil.net_connections(kind='tcp'):
            if conn.laddr and conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                pid = conn.pid
                if not pid:
                    continue
                try:
                    p = psutil.Process(pid)
                    ll.debug(f"Killing PID {pid} ({p.name()}) on port {port}")
                    p.terminate()
                    p.wait(timeout=1.0)
                except psutil.TimeoutExpired:
                    p.kill()
                except Exception:
                    pass

# Example usage:
if __name__ == '__main__':
    # This block is only if you run radio_host.py directly.
    # Replace these stubs with your actual pygame mixer and file paths:

    try:
        from audio import AudioPlayerRoot
    except ImportError:
        from .audio import AudioPlayerRoot

    class MusicPlayer:
        def __init__(self, directories=None, set_screen=None, set_duration=None, set_lyrics=None, set_ips=None):
            self.directories = directories or []
            self.set_screen = set_screen
            self.set_duration = set_duration
            self.set_lyrics = set_lyrics
            self.set_ips = set_ips
            self.pause_event = threading.Event()
            self.repeat_event = threading.Event()
            self.current_volume = 1.0
        
        def pause(self, forcedState=False):
            # Dummy implementation for example
            ll.debug("MusicPlayer pause called", forcedState)
            
        def skip_next(self):
            # Dummy implementation for example
            ll.debug("MusicPlayer skip_next called")
            
        def skip_previous(self):
            # Dummy implementation for example
            ll.debug("MusicPlayer skip_previous called")
            
        def up_volume(self):
            # Dummy implementation for example
            ll.debug("MusicPlayer up_volume called")
            
        def dwn_volume(self):
            # Dummy implementation for example
            ll.debug("MusicPlayer dwn_volume called")
            
        def repeat(self):
            # Dummy implementation for example
            ll.debug("MusicPlayer repeat called")
            
        def play_song(self, path):
            # Dummy implementation for example
            ll.debug(f"MusicPlayer play_song called with path: {path}")
            
        def get_search_term(self, query):
            # Dummy implementation for example
            return [("Example Song", "example.mp3")]

    # Create a dummy WAV file for the example usage within radioMaster.py's __main__ block
    import tempfile, atexit
    import numpy as np
    from scipy.io.wavfile import write as write_wav

    temp_dir_for_test = tempfile.gettempdir()
    test_wav_path = os.path.join(temp_dir_for_test, "test_radio_host_file.wav")
    samplerate_test = 44100
    duration_test = 10 # seconds
    num_samples_test = int(duration_test * samplerate_test)
    silent_data_test = np.zeros(num_samples_test, dtype=np.float32)
    write_wav(test_wav_path, samplerate_test, silent_data_test)
    ll.debug(f"Created test WAV file: {test_wav_path}")
    atexit.register(lambda: os.remove(test_wav_path) if os.path.exists(test_wav_path) else None)

    host = RadioHost(player=MusicPlayer(), port=8080)
    host.initSong(
        title="Example Song![]!Mock Artist", # Use the expected format
        mp3_song_file_path=test_wav_path, # Use the generated WAV file
        current_mixer=AudioPlayerRoot(), # Audio mixer instance
        current_song_lyrics="These are the lyrics..."
    )

    # Keep main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
