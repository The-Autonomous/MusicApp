"""On-demand worker. The player never imports yt-dlp or Playwright at startup."""
import contextlib
from http.cookiejar import Cookie
import json
import os
from pathlib import Path
import sys
import shutil
import subprocess
from time import monotonic


class QuietLogger:
    def debug(self, message):
        pass

    warning = debug

    def error(self, message):
        self.last_error = str(message)


def browser_login(job):
    from youtube_login import browser_login as open_default_browser
    return open_default_browser(job)


def prepare_support(job):
    from importlib.util import find_spec
    from runtime_policy import YouTubeJobError
    if job["action"] == "prepare_app":
        packages = {"numpy": "numpy", "av": "av", "sounddevice": "sounddevice",
                    "mutagen": "mutagen", "pynput": "pynput", "requests": "requests",
                    "aiohttp": "aiohttp", "flask": "Flask", "flask_compress": "Flask-Compress",
                    "waitress": "waitress"}
    else:
        packages = {"yt_dlp": "yt-dlp[default]", "playwright": "playwright"}
        if job.get("music", True):
            packages["ytmusicapi"] = "ytmusicapi"
    missing = [package for module, package in packages.items() if find_spec(module) is None]
    if not missing and job["action"] != "setup":
        return
    requested = list(packages.values()) if job["action"] == "setup" else missing
    support = "MusicApp essentials" if job["action"] == "prepare_app" else "YouTube support"
    command = [sys.executable, "-m", "pip", "install", "--only-binary=:all:",
               "--disable-pip-version-check", "--no-input", "--timeout", "15", "--retries", "1"]
    if job["action"] == "setup":
        command.append("--upgrade")
    try:
        result = subprocess.run([*command, *requested], stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", timeout=240)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise YouTubeJobError(f"{support} couldn't be installed: {exc}",
                              "setup_failed") from None
    if result.returncode:
        details = [line.strip() for line in result.stderr.splitlines() if line.strip()]
        errors = [line for line in details if line.startswith("ERROR:")]
        reason = " ".join(errors or details[-2:])[:500] or "The package installer failed."
        raise YouTubeJobError(f"{support} couldn't be installed ({', '.join(requested)}). {reason}",
                              "setup_failed")
    return f"{support} is ready."


def execute(job):
    if job["action"] == "login":
        return browser_login(job)
    if job['action'] in {'prepare', 'setup', 'prepare_app'}:
        return prepare_support(job)
    if job['action'] == 'music_likes':
        try:
            from ytmusicapi import YTMusic
            from ytmusicapi.helpers import initialize_headers, get_authorization
        except ImportError:
            from runtime_policy import YouTubeJobError
            raise YouTubeJobError('YouTube support needs repairing. Retry to repair it automatically.',
                                  'missing_dependency') from None
        import requests
        from functools import partial
        cookies = {cookie['name']: cookie['value'] for cookie in job.get('cookies', [])}
        sapisid = cookies.get('__Secure-3PAPISID')
        if not sapisid:
            raise RuntimeError('Reconnect YouTube to refresh the YouTube Music login cookie.')
        headers = initialize_headers()
        headers.pop('content-encoding', None)
        headers.update({'cookie': '; '.join(f'{key}={value}' for key, value in cookies.items()),
                        'authorization': get_authorization(sapisid + ' https://music.youtube.com'),
                        'x-origin': 'https://music.youtube.com', 'x-goog-authuser': '0'})
        with requests.Session() as session:
            session.request = partial(session.request, timeout=(3, 12))
            client = YTMusic(auth=json.dumps(dict(headers)), requests_session=session)
            playlist = client.get_playlist('LM', limit=None)
            return [{'id': track['videoId'], 'title': track.get('title', 'Untitled'),
                     'artist': ', '.join(artist.get('name', '') for artist in track.get('artists', [])) or 'YouTube',
                     'duration': track.get('duration_seconds'),
                     'is_available': track.get('isAvailable', True),
                     'url': 'https://www.youtube.com/watch?v=' + track['videoId']}
                    for track in playlist.get('tracks', []) if track.get('videoId')]
    import yt_dlp
    logger = QuietLogger()
    options = {
        "quiet": True, "no_warnings": True, "noprogress": True, "logger": logger,
        "socket_timeout": 12, "retries": 2, "fragment_retries": 2,
        "concurrent_fragment_downloads": 1, "noplaylist": True,
        "cachedir": False,
    }
    node = shutil.which('node')
    if node:
        options['js_runtimes'] = {'deno': {}, 'node': {'path': node}}
    if job.get("browser"):
        options["cookiesfrombrowser"] = (job["browser"],)
    action = job["action"]
    if action in {"playlist", "search"}:
        options.update(extract_flat="in_playlist", skip_download=True, noplaylist=False,
                       ignoreerrors=False, lazy_playlist=True)
    if action == "download":
        target = Path(job["directory"])
        target.mkdir(parents=True, exist_ok=True)
        options.update({
            "format": "bestaudio/best", "noplaylist": True,
            "outtmpl": str(target / (job.get("template") or "%(title).100s [%(id)s].%(ext)s")),
            "windowsfilenames": True,
            "overwrites": False,
            "postprocessor_args": {"ffmpeg": ["-threads", "1", "-filter_threads", "1"]},
            "ratelimit": 512 * 1024 if job.get("gaming") else 8 * 1024 * 1024,
        })
        if job.get("mp3", True):
            options["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                          "preferredquality": "192"}]
        if job.get("archive"):
            options["download_archive"] = job["archive"]
        if job.get("sponsorblock"):
            options["postprocessors"] = [
                {"key": "SponsorBlock", "categories": job["sponsorblock"]},
                {"key": "ModifyChapters", "remove_sponsor_segments": job["sponsorblock"]},
                *options.get("postprocessors", []),
            ]
        ffmpeg_root = Path(__file__).parent / "ffmpeg-bin"
        if ffmpeg_root.exists():
            binary = next(ffmpeg_root.glob("**/ffmpeg.exe"), None)
            if binary:
                options["ffmpeg_location"] = str(binary.parent)
        last_policy_check = [0.0]

        def adapt_to_game(progress):
            if monotonic() - last_policy_check[0] < 15:
                return
            last_policy_check[0] = monotonic()
            from runtime_policy import game_running
            playing = game_running()
            downloader.params["ratelimit"] = 512 * 1024 if playing else 8 * 1024 * 1024
            try:
                import psutil
                overloaded = psutil.cpu_percent(interval=None) >= 80
                if os.name == "nt":
                    psutil.Process().nice(psutil.IDLE_PRIORITY_CLASS if playing or overloaded
                                          else psutil.BELOW_NORMAL_PRIORITY_CLASS)
            except Exception:
                pass

        options["progress_hooks"] = [adapt_to_game]
    with yt_dlp.YoutubeDL(options) as downloader:
        for cookie in job.get("cookies") or []:
            expires = cookie.get("expires", -1)
            domain = cookie["domain"]
            downloader.cookiejar.set_cookie(Cookie(
                version=0, name=cookie["name"], value=cookie["value"], port=None,
                port_specified=False, domain=domain, domain_specified=True,
                domain_initial_dot=domain.startswith("."), path=cookie.get("path", "/"),
                path_specified=True, secure=cookie.get("secure", False),
                expires=int(expires) if expires and expires > 0 else None,
                discard=not expires or expires <= 0, comment=None, comment_url=None,
                rest={"HttpOnly": cookie.get("httpOnly", False)}, rfc2109=False))
        if action == "browser_cookies":
            return [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
                     "expires": c.expires or -1, "secure": c.secure, "httpOnly": True}
                    for c in downloader.cookiejar if c.domain.endswith("youtube.com")]
        if action in {"playlist", "search"}:
            info = downloader.extract_info(job["url"], download=False)
            if not info:
                raise RuntimeError("YouTube returned no playlist information")
            return [{"id": entry.get("id"), "title": entry.get("title") or "Untitled",
                     "artist": entry.get("artist") or entry.get("uploader") or "YouTube",
                     "duration": entry.get("duration"),
                     "availability": entry.get("availability"),
                     "url": entry.get("url") or entry.get("webpage_url")}
                    for entry in info.get("entries", []) if entry and entry.get("id")]
        if action == "download":
            info = downloader.extract_info(job["url"], download=True)
            if not info:
                return None  # Already in the successful-download archive.
            original = Path(downloader.prepare_filename(info))
            path = original.with_suffix(".mp3") if job.get("mp3", True) else original
            if not path.is_file():
                requested = info.get("requested_downloads") or []
                path = Path(requested[0].get("filepath", path)) if requested else path
            if not path.is_file():
                raise RuntimeError("Downloaded audio file was not found")
            return {"path": str(path), "title": info.get("track") or info.get("title", "YouTube"),
                    "artist": info.get("artist") or info.get("uploader", "YouTube")}
        raise RuntimeError("Unknown YouTube operation")


if __name__ == "__main__":
    job = json.load(sys.stdin)
    try:
        with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet):
            result = execute(job)
        response = {"ok": True, "result": result}
    except Exception as exc:
        response = {"ok": False, "error": str(exc)[:1000],
                    "code": getattr(exc, "code", "missing_dependency" if isinstance(exc, ImportError)
                                    else "operation_failed")}
    sys.stdout.write(json.dumps(response, ensure_ascii=False))
