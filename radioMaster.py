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
            'position': 0.0,
            'mixer': None
        }
        self._pos_thread = None
        self._pos_thread_stop = threading.Event()

        # Routes
        @self.app.route('/')
        def index():
            resp = make_response(
                f"<title>{self.current_data['title']}</title>"
                f"<location>{self.current_data['position']:.2f}</location>"
                f"<script>location.href='/{self.app_pad_site}';</script>"
            )
            resp.headers['Cache-Control'] = 'no-store'
            return resp

        @self.app.route(f'/{self.app_pad_site}') # App Pad Loader
        def app_pad():
            return self.app.send_static_file('index.html')
        
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
                
            elif action == 'status':
                pass  # just return the current state

            else:
                return jsonify({"code": "error", "message": "Invalid action"}), 400

            return jsonify({
                "code": "success",
                "title": self.current_data["title"],
                "position": round(self.current_data['position'], 2),
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

    def initSong(self, title, mp3_song_file_path, current_pymixer, current_song_lyrics=""):
        """Call whenever you load a new track."""
        # Stop old position thread
        if self._pos_thread and self._pos_thread.is_alive():
            self._pos_thread_stop.set()
            self._pos_thread.join()

        # Update data
        self.current_data.update({
            'title': title,
            'mp3_path': mp3_song_file_path,
            'lyrics': current_song_lyrics,
            'mixer': current_pymixer,
            'position': 0.0
        })

        # Start new position-updater thread
        self._pos_thread_stop.clear()
        self._pos_thread = threading.Thread(target=self._update_position_loop, daemon=True)
        self._pos_thread.start()

    def _update_position_loop(self):
        """Background loop: polls mixer.get_pos() and stores it."""
        mix = self.current_data['mixer']
        while mix and not self._pos_thread_stop.is_set():
            try:
                # pygame.mixer returns ms
                ms = mix.get_pos()
                self.current_data['position'] = ms / 1000.0 if ms >= 0 else 0.0
            except Exception:
                pass
            time.sleep(0.2)  # adjust frequency as needed

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
    import pygame
    pygame.mixer.init()
    mixer = pygame.mixer.music
    pygame.mixer.music.load("example.mp3")
    pygame.mixer.music.play()

    host = RadioHost(port=8080)
    host.initSong(
        title="Example Song",
        mp3_song_file_path=os.path.abspath("example.mp3"),
        current_pymixer=mixer,
        current_song_lyrics="These are the lyrics..."
    )

    # Keep main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
