import os, platform, subprocess, zipfile, json
import numpy as np
import sounddevice as sd
import soundfile as sf
from threading import Event, Thread
from urllib.request import urlopen
from collections import deque

class AudioPlayerRoot:
    def __init__(self, buffer_size_seconds=10):
        self.ensure_ffmpeg()
        self.stream = None
        self.samplerate = 44100
        self.channels = 2
        self.chunk_size = 1024
        self.filepath = None

        self.paused = Event()
        self.stop_event = Event()
        self.play_event = Event()
        self.volume = 0.1
        self.position_frames = 0
        self.total_frames = 0
        self.duration = 0

        self.buffer = deque()
        self.buffer_size_seconds = buffer_size_seconds

        self._playback_thread = None
        self._reader_thread = None

    def ensure_ffmpeg(self):
        try:
            subprocess.run(['ffprobe', '-version'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except FileNotFoundError:
            print("[FFMPEG/FFPROBE] Not found. Attempting to install...")

        if platform.system() != 'Windows':
            print("[FFMPEG] Auto-install is only for Windows. Please install ffmpeg and ffprobe manually.")
            raise SystemExit(1)

        ffmpeg_dir = os.path.join(os.path.dirname(__file__), 'ffmpeg-bin')
        os.makedirs(ffmpeg_dir, exist_ok=True)
        zip_url = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
        zip_path = os.path.join(ffmpeg_dir, 'ffmpeg.zip')

        try:
            with urlopen(zip_url) as resp, open(zip_path, 'wb') as out_file:
                out_file.write(resp.read())

            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                for member in zip_ref.namelist():
                    if 'bin/ffmpeg.exe' in member or 'bin/ffprobe.exe' in member:
                        zip_ref.extract(member, ffmpeg_dir)

            os.remove(zip_path)

            for root, _, files in os.walk(ffmpeg_dir):
                if 'ffmpeg.exe' in files:
                    bin_path = os.path.abspath(root)
                    os.environ['PATH'] = bin_path + os.pathsep + os.environ['PATH']
                    subprocess.run(['ffmpeg', '-version'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return

            raise FileNotFoundError("Failed to find ffmpeg.exe after extraction.")

        except Exception as e:
            print(f"[FFMPEG] Installation failed: {e}. Please install ffmpeg manually and add it to your PATH.")
            raise SystemExit(1)

    def _get_audio_info(self, filepath):
        cmd = ['ffprobe', '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', filepath]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='ignore')
        if result.returncode != 0:
            raise RuntimeError(f"ffprobe error: {result.stderr}")
        info = json.loads(result.stdout)
        audio_stream = next((s for s in info['streams'] if s['codec_type'] == 'audio'), None)
        if audio_stream is None:
            raise ValueError("No audio stream found in file.")
        return {
            'samplerate': int(audio_stream['sample_rate']),
            'channels': int(audio_stream['channels']),
            'duration': float(info['format'].get('duration', 0.0))
        }

    def load(self, filepath):
        self._start_playback_session(filepath, start_pos=0.0, play_immediately=False)

    def unload(self):
        self.stop()

    def play(self, filepath=None, start_pos=0.0):
        if filepath:
            self._start_playback_session(filepath, start_pos=start_pos, play_immediately=True)
        elif self.filepath and self.paused.is_set():
            self.unpause()
        elif not self.filepath:
            print("[AudioPlayer] No file loaded to play.")

    def _start_playback_session(self, filepath, start_pos, play_immediately):
        self.stop()
        self.filepath = filepath

        try:
            info = self._get_audio_info(filepath)
            self.samplerate = info['samplerate']
            self.channels = info['channels']
            self.duration = info['duration']
            self.total_frames = int(self.duration * self.samplerate)
        except (RuntimeError, ValueError) as e:
            print(f"[AudioPlayer] Error loading file info: {e}")
            return

        self.position_frames = int(start_pos * self.samplerate)
        self.stop_event.clear()
        self.paused.clear() if play_immediately else self.paused.set()

        self._playback_thread = Thread(target=self._run_stream, args=[start_pos], daemon=True)
        self._playback_thread.start()

    def _run_stream(self, start_pos):
        try:
            self._reader_thread = Thread(target=self._read_audio_chunks, args=[start_pos], daemon=True)
            self._reader_thread.start()

            while len(self.buffer) < 5 and self._reader_thread.is_alive():
                if self.stop_event.is_set(): return
                sd.sleep(20)

            if self.stop_event.is_set(): return

            self.stream = sd.OutputStream(
                samplerate=self.samplerate,
                channels=self.channels,
                dtype='float32',
                blocksize=self.chunk_size,
                latency='low',
                callback=self.callback
            )
            self.stream.start()
            self.play_event.set()
            self.stop_event.wait()

        finally:
            self.play_event.clear()
            if self.stream:
                self.stream.close()
                self.stream = None
            if self._reader_thread and self._reader_thread.is_alive():
                self._reader_thread.join(timeout=0.1)

    def _read_audio_chunks(self, start_pos):
        max_buffer_chunks = (self.samplerate * self.buffer_size_seconds) // self.chunk_size
        try:
            is_mp3 = self.filepath.lower().endswith('.mp3')
            if is_mp3:
                ffmpeg_cmd = [
                    'ffmpeg', '-ss', str(start_pos), '-i', self.filepath,
                    '-loglevel', 'error', '-f', 'f32le', '-acodec', 'pcm_f32le',
                    '-ar', str(self.samplerate), '-ac', str(self.channels), 'pipe:1'
                ]
                creationflags = 0
                startupinfo = None
                if platform.system() == 'Windows':
                    creationflags = subprocess.CREATE_NO_WINDOW
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

                process = subprocess.Popen(
                    ffmpeg_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=creationflags,
                    startupinfo=startupinfo
                )
                bytes_per_frame = 4 * self.channels
                chunk_size_bytes = self.chunk_size * bytes_per_frame
                while not self.stop_event.is_set():
                    if len(self.buffer) > max_buffer_chunks:
                        sd.sleep(10)
                        continue
                    raw_audio = process.stdout.read(chunk_size_bytes)
                    if not raw_audio:
                        break
                    chunk = np.frombuffer(raw_audio, dtype=np.float32)
                    if self.channels > 1:
                        chunk = chunk.reshape(-1, self.channels)
                    self.buffer.append(chunk)
                process.kill()
                process.wait()
            else:
                with sf.SoundFile(self.filepath, 'r') as f:
                    f.seek(int(start_pos * f.samplerate))
                    while not self.stop_event.is_set():
                        if len(self.buffer) > max_buffer_chunks:
                            sd.sleep(10)
                            continue
                        chunk = f.read(self.chunk_size, dtype='float32', always_2d=True)
                        if len(chunk) == 0:
                            break
                        self.buffer.append(chunk)
        except Exception as e:
            print(f"[ReaderThread] Error: {e}")

    def callback(self, outdata, frames, time, status):
        if status:
            print(f"[AudioPlayer] Stream status: {status}", flush=True)
        if self.paused.is_set():
            outdata.fill(0)
            return
        try:
            chunk = self.buffer.popleft()
            modified_chunk = chunk * self.volume
            outdata[:len(modified_chunk)] = modified_chunk
            if len(chunk) < len(outdata):
                outdata[len(chunk):] = 0
            self.position_frames += frames
        except IndexError:
            outdata.fill(0)
            self.stop_event.set()
            raise sd.CallbackStop

    def stop(self):
        if self.stop_event.is_set(): return
        self.stop_event.set()
        self.paused.set()
        if self._playback_thread and self._playback_thread.is_alive():
            self._playback_thread.join(timeout=0.5)
        self.buffer.clear()
        self.filepath = None
        self.position_frames = 0
        self.paused.clear()

    def pause(self):
        self.paused.set()

    def unpause(self):
        self.paused.clear()

    def set_pos(self, seconds):
        if self.filepath:
            is_paused = self.paused.is_set()
            self._start_playback_session(self.filepath, start_pos=seconds, play_immediately=not is_paused)

    def get_pos(self):
        return self.position_frames / self.samplerate if self.samplerate else 0

    def get_duration(self):
        return self.duration

    def set_volume(self, volume):
        self.volume = max(0.0, min(1.0, float(volume)))

    def get_busy(self):
        return self.play_event.is_set() and not self.paused.is_set()

AudioPlayer = AudioPlayerRoot()