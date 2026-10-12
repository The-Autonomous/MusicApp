"""On-demand worker. The player never imports yt-dlp or Playwright at startup."""
import contextlib
from http.cookiejar import Cookie
import json
import os
from pathlib import Path
import sys
import shutil
import subprocess
from time import monotonic, sleep


class QuietLogger:
    def debug(self, message):
        pass

    warning = debug

    def error(self, message):
        self.last_error = str(message)


def browser_login(job):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("Browser sign-in needs Playwright. Run Setup when you are ready.") from None
    with sync_playwright() as playwright:
        browser = None
        for channel in ("msedge", "chrome", None):
            try:
                browser = playwright.chromium.launch(headless=False, channel=channel)
                break
            except Exception:
                continue
        if browser is None:
            raise RuntimeError("Install Chrome or Edge, or install Playwright's Chromium browser.")
        try:
            context = browser.new_context(viewport={"width": 960, "height": 720})
            # Avoid audio/video playback during sign-in; leave login/challenge resources intact.
            context.route("**/*", lambda route: route.abort() if route.request.resource_type == "media"
                          else route.continue_())
            page = context.new_page()
            page.goto("https://www.youtube.com/playlist?list=LL", wait_until="domcontentloaded")
            deadline = monotonic() + 300
            while monotonic() < deadline:
                if page.is_closed():
                    raise RuntimeError("Sign-in window closed")
                cookies = context.cookies(["https://www.youtube.com", "https://music.youtube.com"])
                signed_in = any(c["name"] in {"SAPISID", "__Secure-3PAPISID", "__Secure-1PAPISID"}
                                for c in cookies)
                if signed_in and "youtube.com" in page.url:
                    # Stop cookie rotation before taking the session snapshot.
                    page.goto("https://www.youtube.com/robots.txt", wait_until="domcontentloaded")
                    return context.cookies(["https://www.youtube.com", "https://music.youtube.com"])
                page.wait_for_timeout(1500)
            raise RuntimeError("Sign-in timed out; you can try again from YouTube Sync")
        finally:
            browser.close()


def execute(job):
    if job["action"] == "login":
        return browser_login(job)
    if job['action'] == 'setup':
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--upgrade', '--only-binary=:all:',
                        'yt-dlp[default]', 'playwright', 'ytmusicapi'], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return 'YouTube support installed / updated. You can connect or retry sync now.'
    if job['action'] == 'music_likes':
        try:
            from ytmusicapi import YTMusic
            from ytmusicapi.helpers import initialize_headers, get_authorization
        except ImportError:
            raise RuntimeError('Use Install / update YouTube support to enable YouTube Music liked songs.') from None
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
        response = {"ok": False, "error": str(exc)}
    sys.stdout.write(json.dumps(response, ensure_ascii=False))
