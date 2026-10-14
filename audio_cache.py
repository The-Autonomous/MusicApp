"""Flat, on-demand eviction for MusicApp's temporary YouTube audio files."""
import os
from pathlib import Path
import re
from time import time_ns
from urllib.parse import parse_qs, urlsplit

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_CACHE_NAME = re.compile(
    r"\.youtubeCached \[([A-Za-z0-9_-]{11})\]\.(mp3|m4a|webm|opus|ogg|wav|flac)\Z",
    re.IGNORECASE)
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_UTIME_OPTIONS = {"follow_symlinks": False} if os.utime in os.supports_follow_symlinks else {}


def youtube_video_id(url):
    """Return an exact video ID for supported YouTube URLs, never other hosts."""
    if not isinstance(url, str):
        return None
    if len(url) > 4096:
        return None
    try:
        parsed = urlsplit(url.strip())
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            return None
        if parsed.port not in {None, 80, 443}:
            return None
        host = (parsed.hostname or "").lower()
        parts = parsed.path.strip("/").split("/")
        if host in {"youtu.be", "www.youtu.be"} and len(parts) == 1:
            value = parts[0]
        elif host in _YOUTUBE_HOSTS and parsed.path.rstrip("/") == "/watch":
            values = parse_qs(parsed.query).get("v", [])
            value = values[0] if len(values) == 1 else ""
        elif (host in _YOUTUBE_HOSTS and len(parts) == 2
              and parts[0] in {"shorts", "embed", "live"}):
            value = parts[1]
        elif (host in {"youtube-nocookie.com", "www.youtube-nocookie.com"}
              and len(parts) == 2 and parts[0] == "embed"):
            value = parts[1]
        else:
            return None
        return value if _VIDEO_ID.fullmatch(value) else None
    except (ValueError, TypeError):
        return None


def _path_key(path):
    return os.path.normcase(os.path.abspath(os.fspath(path)))


class AudioCache:
    def __init__(self, directory, max_files=8, max_bytes=64 * 1024 * 1024):
        self.directory = Path(directory)
        self.max_files = max(1, int(max_files))
        self.max_bytes = max(1, int(max_bytes))

    def _entries(self):
        entries = []
        try:
            with os.scandir(self.directory) as files:
                for entry in files:
                    match = _CACHE_NAME.fullmatch(entry.name)
                    if not match:
                        continue
                    try:
                        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                            continue
                        stat = entry.stat(follow_symlinks=False)
                        if stat.st_size <= 0 or getattr(stat, "st_file_attributes", 0) & 0x400:
                            continue
                        entries.append((Path(entry.path), match[1], stat))
                    except OSError:
                        continue
        except OSError:
            pass
        return entries

    def find(self, video_id):
        if not isinstance(video_id, str) or not _VIDEO_ID.fullmatch(video_id):
            return None
        matches = [(path, stat) for path, identity, stat in self._entries() if identity == video_id]
        matches.sort(key=lambda item: max(item[1].st_atime_ns, item[1].st_mtime_ns), reverse=True)
        for path, stat in matches:
            try:
                # Touch access time only: cached metadata's modification time stays valid.
                os.utime(path, ns=(time_ns(), stat.st_mtime_ns), **_UTIME_OPTIONS)
                return path
            except (OSError, NotImplementedError):
                # A read-only audio file is still reusable; touching is optional.
                try:
                    if path.is_file() and not path.is_symlink():
                        return path
                except OSError:
                    pass
        return None

    def prune(self, protected=()):
        """Keep current/new tracks even when they temporarily exceed the budget."""
        protected_keys = {_path_key(path) for path in protected if path}
        entries = self._entries()
        count = len(entries)
        size = sum(stat.st_size for _, _, stat in entries)
        if count <= self.max_files and size <= self.max_bytes:
            return
        entries.sort(key=lambda item: max(item[2].st_atime_ns, item[2].st_mtime_ns))
        for path, _, stat in entries:
            if count <= self.max_files and size <= self.max_bytes:
                break
            if _path_key(path) in protected_keys:
                continue
            try:
                path.unlink()
            except OSError:
                continue
            count -= 1
            size -= stat.st_size
