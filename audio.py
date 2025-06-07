import os
import platform
import subprocess
import zipfile
import io
import json
import numpy as np
import sounddevice as sd
import soundfile as sf
from threading import Event, Thread
from urllib.request import urlopen
from collections import deque

class AudioPlayerRoot:
    """
    A robust, thread-safe audio player class that handles streaming of various
    audio formats (including MP3, WAV, FLAC) using ffmpeg and soundfile.

    It uses a double-buffering mechanism with a dedicated reader thread to
    ensure smooth, gapless playback.
    """
    def __init__(self, buffer_size_seconds=10):
        self.ensure_ffmpeg()
        self.stream = None
        self.samplerate = 44100
        self.channels = 2
        self.chunk_size = 1024 # The number of frames per buffer
        self.filepath = None

        # --- Playback State ---
        self.paused = Event()
        self.stop_event = Event() # Used to signal all threads to stop
        self.play_event = Event() # Indicates that audio is actively playing
        self.volume = 0.1
        self.position_frames = 0
        self.total_frames = 0
        self.duration = 0

        # --- Buffering ---
        # A thread-safe double-ended queue to hold chunks of audio data.
        self.buffer = deque()
        self.buffer_size_seconds = buffer_size_seconds

        # --- Threads ---
        self._playback_thread = None
        self._reader_thread = None

    def ensure_ffmpeg(self):
        """
        Checks for the presence of ffprobe. If not found, attempts to download
        and set it up for Windows systems.
        """
        try:
            # Use DEVNULL to hide console output for a clean check.
            subprocess.run(['ffprobe', '-version'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except FileNotFoundError:
            print("[FFMPEG/FFPROBE] Not found. Attempting to install...")

        # Automatic installation is provided for Windows only.
        if platform.system() != 'Windows':
            print("[FFMPEG] Auto-install is only for Windows. Please install ffmpeg and ffprobe manually.")
            raise SystemExit(1)

        ffmpeg_dir = os.path.join(os.path.dirname(__file__), 'ffmpeg-bin')
        os.makedirs(ffmpeg_dir, exist_ok=True)
        
        zip_url = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
        print(f"[FFMPEG] Downloading from {zip_url}...")
        zip_path = os.path.join(ffmpeg_dir, 'ffmpeg.zip')
        
        try:
            with urlopen(zip_url) as resp, open(zip_path, 'wb') as out_file:
                out_file.write(resp.read())
            
            print("[FFMPEG] Extracting...")
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                # Extract only the necessary executables to a known path.
                for member in zip_ref.namelist():
                    if 'bin/ffmpeg.exe' in member or 'bin/ffprobe.exe' in member:
                        zip_ref.extract(member, ffmpeg_dir)

            os.remove(zip_path)

            # Locate the extracted bin directory and add it to the PATH
            for root, _, files in os.walk(ffmpeg_dir):
                if 'ffmpeg.exe' in files:
                    bin_path = os.path.abspath(root)
                    os.environ['PATH'] = bin_path + os.pathsep + os.environ['PATH']
                    print(f"[FFMPEG] Installed to: {bin_path}")
                    # Verify installation
                    subprocess.run(['ffmpeg', '-version'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return
            
            raise FileNotFoundError("Failed to find ffmpeg.exe after extraction.")

        except Exception as e:
            print(f"[FFMPEG] Installation failed: {e}. Please install ffmpeg manually and add it to your PATH.")
            raise SystemExit(1)

    def _get_audio_info(self, filepath):
        """
        Retrieves audio metadata using ffprobe.
        """
        cmd = [
            'ffprobe', '-v', 'error', '-print_format', 'json',
            '-show_format', '-show_streams', filepath
        ]
        # FIX: Specify UTF-8 encoding to prevent UnicodeDecodeError on Windows.
        # The 'charmap' codec error occurs when ffprobe outputs characters
        # that the default system encoding (like cp1252) cannot handle.
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
        """Pre-buffers an audio file without starting playback."""
        self._start_playback_session(filepath, start_pos=0.0, play_immediately=False)

    def play(self, filepath=None, start_pos=0.0):
        """Plays a file directly, or resumes a pre-loaded/paused file."""
        if filepath:
            self._start_playback_session(filepath, start_pos=start_pos, play_immediately=True)
        elif self.filepath and self.paused.is_set():
            self.unpause()
        elif not self.filepath:
            print("[AudioPlayer] No file loaded to play.")

    def _start_playback_session(self, filepath, start_pos, play_immediately):
        self.stop() # Ensure any previous session is fully terminated
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

        # Clear events and set initial state
        self.stop_event.clear()
        if play_immediately:
            self.paused.clear()
        else:
            self.paused.set()

        # Start the master playback thread, which controls the stream and reader
        self._playback_thread = Thread(target=self._run_stream, args=[start_pos], daemon=True)
        self._playback_thread.start()

    def _run_stream(self, start_pos):
        """
        Manages the audio stream and reader thread. This is the core of playback.
        """
        try:
            # The reader thread is responsible for decoding audio and filling the buffer.
            self._reader_thread = Thread(target=self._read_audio_chunks, args=[start_pos], daemon=True)
            self._reader_thread.start()

            # Wait until the buffer has some data before starting the audio stream.
            # This prevents stuttering at the beginning of playback.
            while len(self.buffer) < 5 and self._reader_thread.is_alive():
                if self.stop_event.is_set(): return
                sd.sleep(20) # A short sleep to prevent a busy-wait loop

            # If the stop event was set during buffering, exit.
            if self.stop_event.is_set():
                return

            self.stream = sd.OutputStream(
                samplerate=self.samplerate,
                channels=self.channels,
                dtype='float32',
                blocksize=self.chunk_size,
                latency='low',
                callback=self.callback
            )
            self.stream.start()
            self.play_event.set() # Signal that playback is active

            # The stream runs in the background. This thread just waits for a stop signal.
            self.stop_event.wait()

        finally:
            self.play_event.clear() # Signal that playback is no longer active
            if self.stream:
                self.stream.close()
                self.stream = None
            # Ensure the reader thread is cleaned up properly.
            if self._reader_thread and self._reader_thread.is_alive():
                self._reader_thread.join(timeout=0.1)
    
    def _read_audio_chunks(self, start_pos):
        """
        A unified reader function. It decodes audio from any format (MP3 via ffmpeg,
        others via soundfile) and pushes numpy arrays into the shared buffer.
        """
        max_buffer_chunks = (self.samplerate * self.buffer_size_seconds) // self.chunk_size

        try:
            is_mp3 = self.filepath.lower().endswith('.mp3')
            
            if is_mp3:
                # --- FFMPEG pipeline for MP3s ---
                ffmpeg_cmd = [
                    'ffmpeg', '-ss', str(start_pos), '-i', self.filepath,
                    '-loglevel', 'error', '-f', 'f32le', '-acodec', 'pcm_f32le',
                    '-ar', str(self.samplerate), '-ac', str(self.channels), 'pipe:1'
                ]
                process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                
                bytes_per_frame = 4 * self.channels # float32 is 4 bytes
                chunk_size_bytes = self.chunk_size * bytes_per_frame

                while not self.stop_event.is_set():
                    # Pause reading if the buffer is full to conserve memory.
                    if len(self.buffer) > max_buffer_chunks:
                        sd.sleep(10)
                        continue
                    
                    raw_audio = process.stdout.read(chunk_size_bytes)
                    if not raw_audio:
                        break # End of stream
                    
                    chunk = np.frombuffer(raw_audio, dtype=np.float32)
                    if self.channels > 1:
                        chunk = chunk.reshape(-1, self.channels)
                    self.buffer.append(chunk)

                process.kill()
                process.wait()

            else:
                # --- SoundFile pipeline for other formats (WAV, FLAC, etc.) ---
                with sf.SoundFile(self.filepath, 'r') as f:
                    f.seek(int(start_pos * f.samplerate))
                    while not self.stop_event.is_set():
                        if len(self.buffer) > max_buffer_chunks:
                            sd.sleep(10)
                            continue
                        
                        chunk = f.read(self.chunk_size, dtype='float32', always_2d=True)
                        if len(chunk) == 0:
                            break # End of file
                        self.buffer.append(chunk)

        except Exception as e:
            print(f"[ReaderThread] Error: {e}")
        
    def callback(self, outdata, frames, time, status):
        """
        This is the heart of the audio output, called by the sounddevice stream.
        It must be very fast and non-blocking.
        """
        if status:
            print(f"[AudioPlayer] Stream status: {status}", flush=True)

        if self.paused.is_set():
            outdata.fill(0)
            return

        try:
            # Get the next chunk of audio from the buffer.
            chunk = self.buffer.popleft()
            
            # FIX: Do not modify the output array in-place.
            # The 'ValueError: output array is read-only' happens because `outdata`
            # cannot be modified directly with `*=`. Instead, we create a new
            # modified chunk and copy it into `outdata`.
            modified_chunk = chunk * self.volume
            
            # Copy the chunk to the output buffer.
            outdata[:len(modified_chunk)] = modified_chunk

            # If the chunk is smaller than the buffer, fill the rest with silence.
            if len(chunk) < len(outdata):
                outdata[len(chunk):] = 0
            
            # Update our playback position.
            self.position_frames += frames

        except IndexError:
            # This happens when the buffer runs empty.
            outdata.fill(0)
            self.stop_event.set() # Signal the end of playback
            raise sd.CallbackStop # Stop the stream

    def stop(self):
        """Stops playback and cleans up all resources."""
        if self.stop_event.is_set(): return

        self.stop_event.set() # Signal all threads and loops to stop
        self.paused.set() # Ensure any waiting loops exit
        
        if self._playback_thread and self._playback_thread.is_alive():
            self._playback_thread.join(timeout=0.5)

        self.buffer.clear()
        self.filepath = None
        self.position_frames = 0
        self.paused.clear() # Reset for the next run

    def pause(self):
        self.paused.set()
    
    def unpause(self):
        self.paused.clear()

    def set_pos(self, seconds):
        """Seeks to a new position in the audio."""
        if self.filepath:
            is_paused_before_seek = self.paused.is_set()
            # The cleanest way to seek is to restart the stream from the new position.
            self._start_playback_session(
                self.filepath, 
                start_pos=seconds, 
                play_immediately=not is_paused_before_seek
            )

    def get_pos(self):
        """Returns the current playback position in seconds."""
        return self.position_frames / self.samplerate if self.samplerate else 0

    def get_duration(self):
        """Returns the total duration of the current file in seconds."""
        return self.duration
    
    def set_volume(self, volume):
        """Sets the playback volume between 0.0 and 1.0."""
        self.volume = max(0.0, min(1.0, float(volume)))

    def get_busy(self):
        """Returns True if the audio stream is actively playing."""
        return self.play_event.is_set() and not self.paused.is_set()

# Create a single, global instance of the player.
AudioPlayer = AudioPlayerRoot()
