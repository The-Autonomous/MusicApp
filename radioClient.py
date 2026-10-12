"""Radio uses two bounded workers, reusable connections, and cancellable downloads."""
import ast
from html import unescape
import json
import os
from pathlib import Path
import re
from threading import Event, Lock, RLock, Thread
from time import monotonic, time
from uuid import uuid4
import requests
from log_loader import log_loader

ll = log_loader("Radio Client", debugging=False)


class TimeSync:
    def __init__(self):
        self.offset = 0
        self.last_sync = 0

    def sync_with_host(self, host_ip, port=8080):
        samples = []
        with requests.Session() as session:
            for _ in range(3):
                try:
                    start = time()
                    with session.get(f"http://{host_ip}:{port}/time", timeout=1) as response:
                        response.raise_for_status()
                        host_time = float(response.text)
                    end = time()
                    samples.append((end - start, host_time - (start + end) / 2))
                except (requests.RequestException, ValueError):
                    break
        if samples:
            self.offset = min(samples)[1]
            self.last_sync = time()
            return True
        return False

    def get_synced_time(self):
        return time() + self.offset

    def is_sync_stale(self):
        return time() - self.last_sync > 120


class RadioClient:
    def __init__(self, audio_player, ip=""):
        self.AudioPlayer = audio_player
        self._ip = ip
        self._running = Event()
        self._stop = Event()
        self._download_wake = Event()
        self._lifecycle = RLock()
        self._data_lock = Lock()
        self._generation = 0
        self._pending = None
        self._track_id = None
        self._playing_path = None
        self._playing_track_id = None
        self._download_busy = False
        self._retry_due = 0.0
        self._retry_delay = 2.0
        self._stream_failed = False
        self._original_eq_state = self._original_volume = None
        self._accept_host_eq = False
        self._paused = False
        self._last_resync = 0
        self.update_interval = 1
        self.client_data = {"radio_text": "Radio![]!Connecting…", "radio_text_clean": "",
                            "radio_duration": [0, 0], "lyrics": []}
        self._threads = []
        self.time_sync = TimeSync()

    def get_client_data(self):
        with self._data_lock:
            return dict(self.client_data, radio_duration=list(self.client_data["radio_duration"]))

    def listenTo(self, ip, lyric_callback=None):
        self._ip = ip
        if self._running.is_set():
            return
        self._stop.clear()
        self._running.set()
        self._threads = [Thread(target=self._update_loop, daemon=True, name="radio-status"),
                         Thread(target=self._download_loop, daemon=True, name="radio-download")]
        for thread in self._threads:
            thread.start()

    def stopListening(self):
        with self._lifecycle:
            self._running.clear()
            self._stop.set()
            self._generation += 1
            self._download_wake.set()
            self.AudioPlayer.stop()
            self._restore_local_eq()
        for thread in self._threads:
            thread.join(timeout=1)
        if self._playing_path:
            try:
                Path(self._playing_path).unlink(missing_ok=True)
            except OSError:
                pass
            self._playing_path = None

    def _restore_local_eq(self):
        if self._original_eq_state is not None:
            for frequency, gain in self._original_eq_state.items():
                self.AudioPlayer.eq.set_gain(frequency, gain)
            self.AudioPlayer.set_volume(self._original_volume, True)
            self._original_eq_state = self._original_volume = None

    def set_accept_host_eq(self, accept):
        with self._lifecycle:
            if not accept:
                self._restore_local_eq()
            self._accept_host_eq = bool(accept)

    def _apply_host_eq(self, data):
        with self._lifecycle:
            if not self._accept_host_eq:
                return
            if self._original_eq_state is None:
                self._original_eq_state = self.AudioPlayer.eq.get_gains()
                self._original_volume = self.AudioPlayer.volume
            current = self.AudioPlayer.eq.get_gains()
            for frequency, gain in data["eq"].items():
                if frequency in current and current[frequency] != gain and -12 <= gain <= 12:
                    self.AudioPlayer.eq.set_gain(frequency, gain)
            self.AudioPlayer.set_volume(max(0, min(1, data["volume"])), True)

    def _fetch_data(self, session):
        start = monotonic()
        with session.get(f"http://{self._ip}:8080/", timeout=(2, 2)) as response:
            response.raise_for_status()
            content = response.text
        received = monotonic()

        def extract(tag, default=""):
            match = re.search(f"<{tag}>(.*?)</{tag}>", content, re.DOTALL)
            return unescape(match[1]) if match else default

        title = extract("title", "Radio![]!Unknown song")
        eq = {}
        for pair in extract("eq").split(","):
            if ":" in pair:
                frequency, gain = pair.split(":", 1)
                try:
                    eq[int(frequency)] = float(gain)
                except ValueError:
                    continue
        return {"title": title, "track_id": extract("track_id", title),
                "paused": extract("paused") == "True", "repeat": extract("repeat") == "True",
                "location": float(extract("location", "0")),
                "duration": float(extract("duration", "0")),
                "volume": float(extract("volume", "0.5")), "eq": eq,
                "url": extract("url", f"http://{self._ip}:8080/song"),
                "received": received, "latency": min((received - start) / 2, 0.25)}

    def _update_loop(self):
        backoff = 1
        with requests.Session() as session:
            while self._running.is_set():
                try:
                    data = self._fetch_data(session)
                    if not self._running.is_set():
                        break
                    self._apply_host_eq(data)
                    with self._data_lock:
                        suffix = (" *=*" if data["paused"] else "") + (" *+*" if data["repeat"] else "")
                        self.client_data.update(radio_text=data["title"] + suffix,
                                                radio_text_clean=data["title"], track_id=data['track_id'])
                        self.client_data["radio_duration"] = [self.AudioPlayer.get_pos(), data["duration"]]
                        if data["track_id"] != self._track_id:
                            self._track_id = data["track_id"]
                            self._generation += 1
                            self._stream_failed = False
                            self._retry_delay = 2
                            self._pending = (self._generation, data)
                            self.client_data["lyrics"] = []
                            self._download_wake.set()
                        elif (not self._download_busy and monotonic() >= self._retry_due
                              and (self._playing_track_id != data['track_id'] or
                                   (not self._paused and not self.AudioPlayer.get_busy()
                                    and data['location'] < data['duration'] - 1))):
                            self._stream_failed = self._playing_track_id == data['track_id'] or self._stream_failed
                            self._pending = (self._generation, data)
                            self._download_wake.set()
                    with self._lifecycle:
                        if self._paused != data["paused"]:
                            self._paused = data["paused"]
                            self.AudioPlayer.pause() if self._paused else self.AudioPlayer.unpause()
                        if self._playing_track_id == data['track_id'] and not self._paused:
                            target = data["location"] + data["latency"]
                            drift = abs(self.AudioPlayer.get_pos() - target)
                            if drift > 1.5 and monotonic() - self._last_resync >= 30:
                                self._last_resync = monotonic()
                                self.AudioPlayer.set_pos(target)
                    backoff = 1
                    self._stop.wait(self.update_interval)
                except (requests.RequestException, ValueError, OSError) as exc:
                    if backoff == 1:
                        ll.warn(f"Radio connection interrupted: {exc}")
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 30)

    def _download_loop(self):
        with requests.Session() as session:
            while self._running.is_set():
                self._download_wake.wait()
                self._download_wake.clear()
                with self._data_lock:
                    pending = self._pending
                    self._download_busy = True
                if not pending or not self._running.is_set():
                    self._download_busy = False
                    continue
                generation, data = pending
                path = None
                try:
                    played = False
                    # Range-capable hosts can be played directly with the same bounded PCM
                    # buffer as local files, without downloading the complete song first.
                    if not self._stream_failed:
                        position = data["location"] + (0 if data["paused"] else
                                   monotonic() - data["received"] + data["latency"])
                        with self._lifecycle:
                            if self._running.is_set() and generation == self._generation:
                                played = bool(self.AudioPlayer._start_playback_session(
                                    data["url"], position, not self._paused))
                    if not played:
                        self._stream_failed = True
                        path = Path(__file__).parent / f".radio-{uuid4().hex}.audio"
                        with session.get(data["url"], stream=True, timeout=(3, 6)) as response:
                            response.raise_for_status()
                            total = 0
                            with path.open("wb") as output:
                                for block in response.iter_content(64 * 1024):
                                    if not self._running.is_set() or generation != self._generation:
                                        break
                                    total += len(block)
                                    if total > 64 * 1024 * 1024:
                                        raise RuntimeError("Radio song exceeds the cache limit")
                                    output.write(block)
                        if not self._running.is_set() or generation != self._generation:
                            continue
                        position = data["location"] + (0 if data["paused"] else
                                   monotonic() - data["received"] + data["latency"])
                        with self._lifecycle:
                            if not self._running.is_set() or generation != self._generation:
                                continue
                            played = bool(self.AudioPlayer._start_playback_session(
                                str(path), position, not self._paused))
                    if not played:
                        raise RuntimeError("Radio audio could not start")
                    with self._lifecycle:
                        if not self._running.is_set() or generation != self._generation:
                            continue
                        old_path = self._playing_path
                        self._playing_path = str(path) if path else None
                        self._playing_track_id = data["track_id"]
                        self._last_resync = monotonic()
                        self._retry_due = monotonic() + 5
                        self._retry_delay = 2
                        if old_path:
                            try:
                                Path(old_path).unlink(missing_ok=True)
                            except OSError:
                                pass
                    try:
                        with session.get(f"http://{self._ip}:8080/lyrics", timeout=3) as response:
                            response.raise_for_status()
                            raw = response.text[:128 * 1024]
                        try:
                            lyrics = json.loads(raw)
                        except ValueError:
                            lyrics = ast.literal_eval(raw)
                        if generation == self._generation and isinstance(lyrics, list):
                            with self._data_lock:
                                self.client_data["lyrics"] = sorted(
                                    (float(line[0]), str(line[1])) for line in lyrics
                                    if isinstance(line, (list, tuple)) and len(line) == 2)
                    except (requests.RequestException, ValueError, SyntaxError, TypeError):
                        pass
                except Exception as exc:
                    if self._running.is_set():
                        ll.warn(f"Radio playback: {exc}")
                        self._retry_due = monotonic() + self._retry_delay
                        self._retry_delay = min(self._retry_delay * 2, 30)
                finally:
                    with self._data_lock:
                        self._download_busy = False
                    if path and str(path) != self._playing_path:
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            pass

    def remTmpFile(self):
        if self._playing_path:
            try:
                Path(self._playing_path).unlink(missing_ok=True)
                return True
            except OSError:
                return False
        return True
