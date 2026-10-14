"""Conservative local-song matching for the YouTube likes download queue."""
import os
from pathlib import Path
import re
import stat
import unicodedata

from audio_cache import youtube_video_id


AUDIO_SUFFIXES = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".opus", ".webm"}
_FILE_ID = re.compile(r" \[([A-Za-z0-9_-]{11})\]$")
_PROMOTION = re.compile(
    r"\s*[\[(](?:official(?: music)? (?:video|audio)|music video|audio|"
    r"lyric(?:s)?(?: video)?|official lyric(?:s)?(?: video)?|visuali[sz]er)[\])]\s*$",
    re.IGNORECASE)
_UNKNOWN = {"", "youtube", "unknownartist", "unknown", "untitled"}


def audio_file_exists(value):
    """Ignore missing files, partial downloads and Windows cloud placeholders."""
    if not isinstance(value, (str, os.PathLike)) or not value:
        return False
    path = Path(value)
    if path.suffix.lower() not in AUDIO_SUFFIXES or path.name.startswith("."):
        return False
    try:
        info = path.stat()
        return (stat.S_ISREG(info.st_mode) and info.st_size > 0
                and not getattr(info, "st_file_attributes", 0) & (0x1000 | 0x40000 | 0x400000))
    except (OSError, ValueError):
        return False


def eligible_tracks(tracks):
    """Keep one entry per valid video; reject known unavailable entries up front."""
    result = {}
    skipped = 0
    for entry in tracks:
        if not isinstance(entry, dict):
            skipped += 1
            continue
        identifier = entry.get("id")
        url = entry.get("url")
        canonical = f"https://www.youtube.com/watch?v={identifier}"
        if (not isinstance(identifier, str) or youtube_video_id(canonical) != identifier
                or (url and url != identifier and youtube_video_id(url) != identifier)
                or entry.get("is_available") is False
                or entry.get("availability") in ("private", "premium_only", "subscriber_only", "needs_auth")
                or entry.get("title") in ("[Deleted video]", "[Private video]")):
            skipped += 1
            continue
        # Preserve the first entry's metadata rather than replacing it with a duplicate.
        result.setdefault(identifier, entry)
    return list(result.values()), skipped


def _text(value):
    return unicodedata.normalize("NFKC", value).casefold().strip() if isinstance(value, str) else ""


def _artist(value):
    value = re.sub(r"\s*-\s*topic$", "", _text(value))
    return "".join(character for character in value if character.isalnum())


def _title(value):
    value = _text(value)
    while True:
        cleaned = _PROMOTION.sub("", value)
        if cleaned == value:
            break
        value = cleaned
    value = value.replace("&", " and ").replace("'", "").replace("’", "")
    return " ".join("".join(character if character.isalnum() else " "
                            for character in value).split())


def song_keys(track):
    """Normalize tags and redundant artist prefixes without guessing performers."""
    title = track.get("title")
    artist = _artist(track.get("artist"))
    pairs = []
    if isinstance(title, str):
        parts = re.split(r"\s+[-–—]\s+", title, maxsplit=1)
        if len(parts) == 2:
            embedded_artist, embedded_title = parts
            if _artist(embedded_artist) == artist:
                title = embedded_title
            elif artist in _UNKNOWN:
                pairs.append((_artist(embedded_artist), _title(embedded_title)))
        pairs.append((artist, _title(title)))
    return {pair for pair in pairs if pair[0] not in _UNKNOWN and pair[1]
            and pair[1].replace(" ", "") not in _UNKNOWN}


class LocalSongIndex:
    def __init__(self):
        self.by_id = {}
        self.by_song = {}

    def add(self, track):
        path = track.get("path")
        if not audio_file_exists(path):
            return
        match = _FILE_ID.search(Path(path).stem)
        if match:
            self.by_id.setdefault(match[1], str(path))
        for key in song_keys(track):
            self.by_song.setdefault(key, str(path))

    def find(self, track, downloaded):
        candidates = [downloaded.get(track["id"]), self.by_id.get(track["id"])]
        candidates.extend(self.by_song.get(key) for key in sorted(song_keys(track)))
        return next((str(path) for path in candidates if audio_file_exists(path)), None)
