"""Bounded progress reporting and a rate limiter that reacts mid-download."""
from contextlib import contextmanager
import json
from math import isfinite
import os
from pathlib import Path
from time import monotonic, sleep


def _number(value):
    return value if isinstance(value, (int, float)) and isfinite(value) and value >= 0 else 0


class DownloadReporter:
    def __init__(self, path):
        self.path = Path(path) if path else None
        self.last_write = 0
        self.last_phase = None
        self.downloaded = 0
        self.total = 0

    def __call__(self, event):
        phase = event.get("status", "downloading")
        self.downloaded = _number(event.get("downloaded_bytes", self.downloaded))
        self.total = _number(event.get("total_bytes") or event.get("total_bytes_estimate") or self.total)
        self.publish("processing" if phase == "finished" else "downloading")

    def postprocess(self, event):
        self.publish("processing", event.get("postprocessor", ""), force=True)

    def publish(self, phase, processor="", force=False):
        if not self.path:
            return
        now = monotonic()
        if not force and phase == self.last_phase and now - self.last_write < 0.25:
            return
        self.last_write, self.last_phase = now, phase
        fraction = min(1, self.downloaded / self.total) if self.total else None
        if phase in {"processing", "finished"}:
            fraction = 1
        value = {"phase": phase, "processor": processor, "progress": fraction,
                 "downloaded_bytes": self.downloaded, "total_bytes": self.total}
        temporary = self.path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(value), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            # Windows may briefly deny replacement while the parent reads the
            # last report. Progress is optional and must never fail the audio job.
            pass


class LiveThrottle:
    def __init__(self, policy_file):
        self.path = Path(policy_file)
        self.checked_at = None
        self.rate = 256 * 1024

    def current_rate(self):
        now = monotonic()
        if self.checked_at is None or now - self.checked_at >= 0.2:
            self.checked_at = now
            try:
                report = json.loads(self.path.read_text(encoding="utf-8"))
                rate = _number(report.get("rate"))
                if rate:
                    self.rate = max(16 * 1024, rate)
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        return self.rate

    def slow_down(self, downloader, start_time, now, byte_counter):
        # Use each block's bytes, rather than the entire song's average. This
        # avoids a long, uninterruptible sleep when a new game needs resources.
        current = monotonic()
        previous_bytes, previous_time = getattr(downloader, "_musicapp_throttle", (0, current))
        amount = max(0, byte_counter - previous_bytes)
        while amount and monotonic() - previous_time < amount / self.current_rate():
            sleep(min(0.1, max(0, amount / self.current_rate() - (monotonic() - previous_time))))
        downloader._musicapp_throttle = (byte_counter, monotonic())


@contextmanager
def live_throttle(policy_file):
    if not policy_file:
        yield
        return
    # This substitution is confined to the disposable downloader process.
    from yt_dlp.downloader.common import FileDownloader
    original = FileDownloader.slow_down
    throttle = LiveThrottle(policy_file)
    FileDownloader.slow_down = lambda downloader, start, now, count: throttle.slow_down(downloader, start, now, count)
    try:
        yield
    finally:
        FileDownloader.slow_down = original
