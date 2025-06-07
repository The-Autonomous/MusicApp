import os, socket, psutil, threading, time
from flask import Flask, send_from_directory, make_response, jsonify, request
from flask_compress import Compress
from waitress import serve

class RadioHost:
    def __init__(self, player, port=8080):
        # Flask app setup
        self.MusicPlayer = player
        self.app_pad_site = "app_pad"
        self.app = Flask(__name__, static_folder='app_pad')
        Compress(self.app)

        # Shared state
        self.current_data = {
            'title': '',
            'mp3_path': '',
            'lyrics': '',
            'mixer': None
        }

        def get_pos():
            """Get current song position."""
            try:
                mix = self.current_data['mixer']
                return mix.get_pos() if mix else 0
            except Exception:
                print("Error getting position from mixer")
                return 0

        # Routes
        @self.app.route('/')
        def index():
            resp = make_response(
                f"<title>{self.current_data['title']}</title>"
                f"<location>{get_pos()}</location>"
                f"<script>location.href='/{self.app_pad_site}';</script>"
            )
            resp.headers['Cache-Control'] = 'no-store'
            return resp

        @self.app.route(f'/{self.app_pad_site}') # App Pad Loader
        def app_pad():
            return self.app.send_static_file('index.html')
        
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
            return send_from_directory(directory, filename, mimetype='audio/mpeg')

        @self.app.route('/lyrics')
        def serve_lyrics():
            return self.current_data['lyrics'] or "No lyrics available"

        @self.app.after_request
        def add_no_cache_headers(response):
            response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            response.headers['Pragma']        = 'no-cache'
            response.headers['Expires']       = '0'
            return response

        # Start server in background
        host = self._get_local_ip()
        self._free_port(port)
        self._server_thread = threading.Thread(
            target=serve,
            args=(self.app,),
            kwargs={'host': host, 'port': port},
            daemon=True
        )
        self._server_thread.start()
        print(f"[RadioHost] Serving on http://{host}:{port}")

    def initSong(self, title, mp3_song_file_path, current_mixer, current_song_lyrics=""):
        """Call whenever you load a new track."""
        # Update data
        self.current_data.update({
            'title': title,
            'mp3_path': mp3_song_file_path,
            'lyrics': current_song_lyrics,
            'mixer': current_mixer
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
                    print(f"[RadioHost] Killing PID {pid} ({p.name()}) on port {port}")
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
        from audio import AudioPlayer
    except ImportError:
        from .audio import AudioPlayer

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
            print("MusicPlayer pause called", forcedState)
            
        def skip_next(self):
            # Dummy implementation for example
            print("MusicPlayer skip_next called")
            
        def skip_previous(self):
            # Dummy implementation for example
            print("MusicPlayer skip_previous called")
            
        def up_volume(self):
            # Dummy implementation for example
            print("MusicPlayer up_volume called")
            
        def dwn_volume(self):
            # Dummy implementation for example
            print("MusicPlayer dwn_volume called")
            
        def repeat(self):
            # Dummy implementation for example
            print("MusicPlayer repeat called")
            
        def play_song(self, path):
            # Dummy implementation for example
            print(f"MusicPlayer play_song called with path: {path}")
            
        def get_search_term(self, query):
            # Dummy implementation for example
            return [("Example Song", "example.mp3")]

    host = RadioHost(player=MusicPlayer(), port=8080)
    host.initSong(
        title="Example Song",
        mp3_song_file_path=os.path.abspath("example.mp3"),
        current_mixer=AudioPlayer, # Audio mixer instance
        current_song_lyrics="These are the lyrics..."
    )

    # Keep main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
