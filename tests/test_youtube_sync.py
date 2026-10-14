"""Isolated sync tests; no accounts, browser, installation, or audio playback."""
import importlib.util
import ctypes  # Keep native ctypes types loaded across the isolated module import.
import json
import os
from pathlib import Path
import shutil
import sys
from threading import Event, Lock
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
import uuid
from download_policy import DownloadBudget


ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_COOKIES = [{"name": "__Secure-3PAPISID", "value": "synthetic-session",
                      "domain": ".youtube.com", "path": "/", "secure": True}]


class FixtureJobError(RuntimeError):
    def __init__(self, message, code="operation_failed"):
        super().__init__(message)
        self.code = code


def isolated_sync_module():
    logger = ModuleType("log_loader")
    logger.log_loader = lambda *args, **kwargs: SimpleNamespace(warn=lambda message: None)
    runtime = ModuleType("runtime_policy")
    runtime.YouTubeJobError = FixtureJobError
    runtime.run_job = lambda *args, **kwargs: None
    specification = importlib.util.spec_from_file_location("_musicapp_sync_fixture", ROOT / "youtube_sync.py")
    module = importlib.util.module_from_spec(specification)
    # Restore previous entries immediately; other tests may import these real modules.
    with patch.dict(sys.modules, {"log_loader": logger, "runtime_policy": runtime}):
        specification.loader.exec_module(module)
    return module


class FakePlayer:
    def __init__(self, downloader):
        self.playback = Event()
        self.connection_callbacks = 0
        self.ytHandle = SimpleNamespace(_metadata_lock=Lock(), configure=lambda **kwargs: None,
                                        _download=downloader)
        self.add_downloaded_song = lambda *args, **kwargs: None
        self.local_songs = []
        self._directories = []
        self._playlist_urls = []
        self.get_metadata = lambda path: {}
        self.local_songs_for_sync = lambda cancel: iter(self.local_songs)

    def allow_playback(self):
        self.connection_callbacks += 1
        self.playback.set()


class YouTubeSyncFlow(unittest.TestCase):
    def setUp(self):
        self.module = isolated_sync_module()
        self.directory = (ROOT / "tests" / ("MusicApp-sync-test-" + uuid.uuid4().hex)).resolve()
        self.directory.mkdir()  # Inherit the workspace ACL for the restricted test token.
        self.addCleanup(self.clean_fixture)
        locations = {
            "APP_DIR": self.directory, "DATA_DIR": self.directory,
            "SESSION_FILE": self.directory / "session.bin",
            "SETTINGS_FILE": self.directory / "settings.json",
            "LIKES_FILE": self.directory / "likes.json",
            "DOWNLOADS_FILE": self.directory / "downloads.json",
            "DEFERRED_FILE": self.directory / "deferred.json",
        }
        paths = patch.multiple(self.module, **locations)
        paths.start()
        self.addCleanup(paths.stop)
        home = patch.object(Path, "home", return_value=self.directory)
        home.start()
        self.addCleanup(home.stop)
        crypt = patch.object(self.module, "_crypt", side_effect=self.fake_crypt)
        crypt.start()
        self.addCleanup(crypt.stop)
        self.jobs = []
        self.statuses = []
        self.failures = []
        self.finished = Event()
        self.tracks = [{"id": "synthetic-0", "title": "Fixture", "artist": "Fixture"}]
        self.download_count = 0
        self.playlists = {}
        self.player = FakePlayer(self.download)
        self.sync = self.module.YouTubeSync(self.player)
        self.addCleanup(self.sync.close)
        job = patch.object(self.module, "run_job", side_effect=self.run_job)
        job.start()
        self.addCleanup(job.stop)
        budget = patch.object(self.module, "download_budget", return_value=DownloadBudget(1, 1024 ** 2, 0, False, "Test"))
        budget.start()
        self.addCleanup(budget.stop)

    def clean_fixture(self):
        checked = self.directory.resolve()
        if checked.parent != (ROOT / "tests").resolve() or not checked.name.startswith("MusicApp-sync-test-"):
            raise RuntimeError("Unexpected test cleanup location")
        shutil.rmtree(checked)

    @staticmethod
    def fake_crypt(data, decrypt=False):
        return json.dumps(SYNTHETIC_COOKIES).encode() if decrypt else b"protected synthetic fixture"

    def cached_session(self):
        self.module.SESSION_FILE.write_bytes(b"protected synthetic fixture")

    def run_job(self, job, cancel=None, timeout=None):
        if cancel is not None and cancel.is_set():
            raise RuntimeError("Cancelled")
        self.jobs.append(dict(job))
        if job["action"] in {"prepare", "setup"}:
            return None
        if job["action"] in {"playlist", "music_likes"}:
            if job["action"] == "playlist" and job["url"] in self.playlists:
                return list(self.playlists[job["url"]])
            return list(self.tracks)
        if job["action"] == "login":
            return [dict(cookie) for cookie in SYNTHETIC_COOKIES]
        if job["action"] == "download":
            self.download_count += 1
            identifier = job["url"].rsplit("=", 1)[-1]
            path = self.directory / (identifier + ".mp3")
            path.write_bytes(b"synthetic download fixture")
            return str(path)
        raise AssertionError("Unexpected fixture action: " + job["action"])

    def download(self, url, directory, *, cookies, cancel, sponsorblock, progress):
        return self.module.run_job({"action": "download", "url": url, "directory": str(directory),
                                    "cookies": cookies, "sponsorblock": sponsorblock}, cancel, timeout=1)

    def start(self, *, sync=False, login=False, on_connected=None, source=None):
        self.assertTrue(self.sync.start(self.statuses.append, source=source, login=login, sync=sync,
                                        on_connected=on_connected,
                                        on_ready=self.player.allow_playback,
                                        on_failed=self.failures.append, on_finished=self.finished.set))

    def wait_finished(self):
        self.assertTrue(self.finished.wait(3), "The fixture sync did not finish")
        self.sync._thread.join(timeout=3)
        self.assertFalse(self.sync._thread.is_alive())

    def test_cached_session_is_verified_before_playback(self):
        self.cached_session()
        fetching = Event()
        release = Event()
        self.addCleanup(release.set)
        def request(job, cancel, timeout):
            if job["action"] == "music_likes":
                self.assertFalse(self.player.playback.is_set())
                fetching.set()
                if not release.wait(3):
                    raise RuntimeError("Fixture release timed out")
            return self.run_job(job, cancel, timeout)
        self.module.run_job.side_effect = request
        self.start()
        try:
            self.assertTrue(fetching.wait(3))
            self.assertFalse(self.sync.connected)
            self.assertFalse(self.player.playback.is_set())
        finally:
            release.set()
        self.wait_finished()
        self.assertTrue(self.sync.connected)
        self.assertEqual(self.player.connection_callbacks, 0)
        self.assertEqual([job["action"] for job in self.jobs], ["prepare", "music_likes"])

    def test_fetch_failure_keeps_playback_paused(self):
        self.cached_session()
        def request(job, cancel, timeout):
            if job["action"] == "music_likes":
                raise RuntimeError("Network unavailable")
            return self.run_job(job, cancel, timeout)
        self.module.run_job.side_effect = request
        self.start()
        self.wait_finished()
        self.assertFalse(self.sync.connected)
        self.assertFalse(self.player.playback.is_set())
        self.assertEqual(self.sync.phase, "error")
        self.assertEqual(len(self.failures), 1)
        self.assertFalse(self.module.LIKES_FILE.exists())

    def test_expired_stored_session_signs_in_and_verifies_again(self):
        self.cached_session()
        fetches = 0
        def request(job, cancel, timeout):
            nonlocal fetches
            if job["action"] == "music_likes":
                fetches += 1
                if fetches == 1:
                    self.jobs.append(dict(job))
                    raise FixtureJobError("401 unauthorized", "signin_failed")
            return self.run_job(job, cancel, timeout)
        self.module.run_job.side_effect = request
        self.start()
        self.wait_finished()
        self.assertTrue(self.sync.connected)
        self.assertEqual(self.player.connection_callbacks, 0)
        self.assertEqual([job["action"] for job in self.jobs],
                         ["prepare", "music_likes", "login", "music_likes"])

    def test_login_success_but_verification_failure_does_not_connect(self):
        def request(job, cancel, timeout):
            if job["action"] == "music_likes":
                raise RuntimeError("403 forbidden")
            return self.run_job(job, cancel, timeout)
        self.module.run_job.side_effect = request
        self.start(login=True)
        self.wait_finished()
        self.assertFalse(self.sync.connected)
        self.assertEqual(self.player.connection_callbacks, 0)
        self.assertEqual(self.sync.phase, "error")

    def test_cancel_before_fetch_never_connects(self):
        self.cached_session()
        def request(job, cancel, timeout):
            result = self.run_job(job, cancel, timeout)
            if job["action"] == "prepare":
                cancel.set()
            return result
        self.module.run_job.side_effect = request
        self.start()
        self.wait_finished()
        self.assertFalse(self.sync.connected)
        self.assertFalse(self.player.playback.is_set())
        self.assertFalse(self.module.LIKES_FILE.exists())
        self.assertEqual([job["action"] for job in self.jobs], ["prepare"])

    def test_cancel_after_fetch_never_connects_or_replaces_cache(self):
        self.cached_session()
        self.module.LIKES_FILE.write_text('{"sources":{"previous":{"tracks":[]}}}', encoding="utf-8")
        before = self.module.LIKES_FILE.read_bytes()
        def request(job, cancel, timeout):
            result = self.run_job(job, cancel, timeout)
            if job["action"] == "music_likes":
                cancel.set()
            return result
        self.module.run_job.side_effect = request
        self.start()
        self.wait_finished()
        self.assertFalse(self.sync.connected)
        self.assertFalse(self.player.playback.is_set())
        self.assertEqual(self.module.LIKES_FILE.read_bytes(), before)

    def test_dpapi_failure_uses_memory_only_and_displays_notice(self):
        with patch.object(self.module, "_crypt", side_effect=OSError("Synthetic DPAPI failure")):
            self.start(login=True)
            self.wait_finished()
        self.assertTrue(self.sync.connected)
        self.assertFalse(self.player.playback.is_set())
        self.assertFalse(self.sync.session_remembered)
        self.assertEqual(self.sync.cookies(), SYNTHETIC_COOKIES)
        self.assertFalse(self.module.SESSION_FILE.exists())
        self.assertTrue(any("Windows couldn't remember" in status for status in self.statuses))
        self.assertNotIn("synthetic-session", self.module.LIKES_FILE.read_text(encoding="utf-8"))

    def test_checkpoint_after_five_downloads_then_final_flush(self):
        self.cached_session()
        self.tracks = [{"id": f"synthetic-{index}", "title": "Fixture"} for index in range(7)]
        snapshots = []
        actual = self.module.atomic_json
        def save(path, data):
            if path == self.module.DOWNLOADS_FILE:
                snapshots.append(dict(data))
            actual(path, data)
        with patch.object(self.module, "atomic_json", side_effect=save):
            self.start(sync=True)
            self.wait_finished()
        self.assertEqual([len(snapshot) for snapshot in snapshots], [5, 7])
        self.assertEqual(len(json.loads(self.module.DOWNLOADS_FILE.read_text(encoding="utf-8"))), 7)
        self.assertEqual(self.download_count, 7)
        self.assertTrue(self.player.playback.is_set())
        self.assertTrue(self.sync.ready)

    def test_playback_waits_for_last_song_and_saved_checkpoint(self):
        self.cached_session()
        self.tracks = [{"id": f"synthetic-{index}", "title": "Fixture"} for index in range(2)]
        downloading_last, release = Event(), Event()
        self.addCleanup(release.set)

        def request(job, cancel, timeout):
            if job["action"] == "download":
                self.assertFalse(self.player.playback.is_set())
                if job["url"].endswith("synthetic-1"):
                    downloading_last.set()
                    if not release.wait(3):
                        raise RuntimeError("Fixture release timed out")
            return self.run_job(job, cancel, timeout)

        self.module.run_job.side_effect = request
        self.start(sync=True)
        try:
            self.assertTrue(downloading_last.wait(3))
            self.assertTrue(self.sync.connected)
            self.assertFalse(self.sync.ready)
            self.assertFalse(self.player.playback.is_set())
        finally:
            release.set()
        self.wait_finished()
        self.assertTrue(self.sync.ready)
        self.assertTrue(self.player.playback.is_set())
        self.assertEqual(len(json.loads(self.module.DOWNLOADS_FILE.read_text())), 2)

    def test_failed_download_retries_at_back_of_queue_before_playback(self):
        self.cached_session()
        self.tracks = [{"id": f"synthetic-{index}", "title": "Fixture"} for index in range(3)]
        attempts = []

        def request(job, cancel, timeout):
            if job["action"] == "download":
                self.assertFalse(self.player.playback.is_set())
                identifier = job["url"].rsplit("=", 1)[-1]
                attempts.append(identifier)
                if len(attempts) == 1:
                    raise RuntimeError("Synthetic temporary failure")
            return self.run_job(job, cancel, timeout)

        self.module.run_job.side_effect = request
        self.start(sync=True)
        self.wait_finished()
        self.assertEqual(attempts, ["synthetic-0", "synthetic-1", "synthetic-2", "synthetic-0"])
        self.assertTrue(self.player.playback.is_set())
        self.assertTrue(self.sync.ready)
        self.assertEqual(self.sync.saved, 3)
        self.assertFalse(self.module.DEFERRED_FILE.exists())

    def test_second_failure_defers_song_and_file_deletion_resets_it(self):
        self.cached_session()
        self.tracks = [{"id": f"synthetic-{index}", "title": "Fixture"} for index in range(2)]
        failures = []

        def request(job, cancel, timeout):
            if job["action"] == "download" and job["url"].endswith("synthetic-1"):
                failures.append(job["url"])
                raise RuntimeError("Synthetic unavailable video")
            return self.run_job(job, cancel, timeout)

        self.module.run_job.side_effect = request
        with patch.object(self.module, "time", return_value=1_000_000):
            self.start(sync=True)
            self.wait_finished()
        self.assertEqual(len(failures), 2)
        self.assertTrue(self.player.playback.is_set())
        self.assertTrue(self.sync.ready)
        self.assertEqual(self.sync.phase, "connected")
        self.assertEqual(self.sync.saved, 1)
        self.assertEqual(self.sync.deferred, 1)
        self.assertIn("1 deferred", self.sync.message)
        record = json.loads(self.module.DEFERRED_FILE.read_text())["synthetic-1"]
        self.assertEqual(record["retry_after"], 1_000_000 + 30 * 24 * 60 * 60)

        # A new controller still reads the persisted cooldown.
        self.sync.close()
        self.sync = self.module.YouTubeSync(self.player)
        self.addCleanup(self.sync.close)
        self.finished.clear()
        self.module.run_job.side_effect = self.run_job
        with patch.object(self.module, "time", return_value=1_000_001):
            self.start(sync=True)
            self.wait_finished()
        self.assertEqual(self.download_count, 1)
        self.assertEqual(self.sync.deferred, 1)

        # Removing only the ignored file resets the policy on the next sync.
        self.module.DEFERRED_FILE.unlink()
        self.finished.clear()
        self.start(sync=True)
        self.wait_finished()
        self.assertTrue(self.player.playback.is_set())
        self.assertEqual(self.sync.saved, 2)
        self.assertEqual(self.download_count, 2)

    def test_deferred_song_becomes_eligible_at_30_day_boundary(self):
        self.cached_session()
        retry_after = 1_000_000 + self.module.DOWNLOAD_COOLDOWN
        self.module.atomic_json(self.module.DEFERRED_FILE,
                                {"synthetic-0": {"retry_after": retry_after, "title": "Fixture"}})
        with patch.object(self.module, "time", return_value=retry_after - 1):
            self.start(sync=True)
            self.wait_finished()
        self.assertEqual(self.download_count, 0)
        self.assertEqual(self.sync.total, 0)
        self.assertEqual(self.sync.deferred, 1)
        self.assertTrue(self.sync.ready)
        self.finished.clear()
        with patch.object(self.module, "time", return_value=retry_after):
            self.start(sync=True)
            self.wait_finished()
        self.assertEqual(self.download_count, 1)
        self.assertEqual(self.sync.deferred, 0)
        self.assertEqual(json.loads(self.module.DEFERRED_FILE.read_text()), {})

    def test_cancelling_retry_does_not_defer_song_or_release_playback(self):
        self.cached_session()
        attempts = []

        def request(job, cancel, timeout):
            if job["action"] == "download":
                attempts.append(job["url"])
                if len(attempts) == 2:
                    cancel.set()
                raise RuntimeError("Synthetic interrupted download")
            return self.run_job(job, cancel, timeout)

        self.module.run_job.side_effect = request
        self.start(sync=True)
        self.wait_finished()
        self.assertEqual(len(attempts), 2)
        self.assertFalse(self.module.DEFERRED_FILE.exists())
        self.assertFalse(self.player.playback.is_set())
        self.assertFalse(self.sync.ready)

    def test_empty_download_result_also_retries_then_defers(self):
        self.cached_session()
        attempts = []

        def request(job, cancel, timeout):
            if job["action"] == "download":
                attempts.append(job["url"])
                return None
            return self.run_job(job, cancel, timeout)

        self.module.run_job.side_effect = request
        self.start(sync=True)
        self.wait_finished()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.sync.deferred, 1)
        self.assertTrue(self.sync.ready)

    def test_unwritable_cooldown_file_keeps_playback_held(self):
        self.cached_session()
        actual_save = self.module.atomic_json

        def save(path, data):
            if path == self.module.DEFERRED_FILE:
                raise OSError("Synthetic unwritable cooldown file")
            actual_save(path, data)

        def request(job, cancel, timeout):
            if job["action"] == "download":
                raise RuntimeError("Synthetic unavailable song")
            return self.run_job(job, cancel, timeout)

        self.module.run_job.side_effect = request
        with patch.object(self.module, "atomic_json", side_effect=save):
            self.start(sync=True)
            self.wait_finished()
        self.assertFalse(self.sync.ready)
        self.assertFalse(self.player.playback.is_set())
        self.assertEqual(self.sync.phase, "error")

    def test_empty_library_finishes_without_downloads(self):
        self.cached_session()
        self.tracks = []
        self.start(sync=True)
        self.wait_finished()
        self.assertTrue(self.sync.ready)
        self.assertTrue(self.player.playback.is_set())
        self.assertEqual(self.download_count, 0)

    def test_liked_downloads_use_gta_enhanced_directory(self):
        self.cached_session()
        preferred = self.directory / "Rockstar Games" / "GTAV Enhanced" / "User Music"
        fallback = self.directory / "MusicPlayerYoutubeDownloads"
        self.player._directories = [str(fallback), str(preferred)]
        self.start(sync=True)
        self.wait_finished()
        downloads = [job for job in self.jobs if job["action"] == "download"]
        self.assertEqual(len(downloads), 1)
        self.assertEqual(downloads[0]["directory"], str(preferred))
        self.assertTrue(preferred.is_dir())
        self.assertFalse(fallback.exists())

    def test_existing_gta_copy_replaces_personal_folder_mapping_without_download(self):
        self.cached_session()
        preferred = self.directory / "GTAV Enhanced" / "User Music"
        preferred.mkdir(parents=True)
        legacy = self.directory / "old.mp3"
        legacy.write_bytes(b"existing audio")
        gta = preferred / "song [synthetic-0].mp3"
        gta.write_bytes(b"existing audio")
        self.player._directories = [str(preferred)]
        self.player.local_songs = [{"title": "Fixture", "artist": "Fixture", "path": str(gta)}]
        self.module.atomic_json(self.module.DOWNLOADS_FILE, {"synthetic-0": str(legacy)})
        self.start(sync=True)
        self.wait_finished()
        self.assertEqual(self.download_count, 0)
        self.assertEqual(json.loads(self.module.DOWNLOADS_FILE.read_text())["synthetic-0"], str(gta))

    def test_duplicate_video_ids_are_queued_only_once_even_when_retrying(self):
        self.cached_session()
        self.tracks = [{"id": "synthetic-0", "title": "Original title"},
                       {"id": "synthetic-0", "title": "Duplicate title"},
                       {"id": "synthetic-1", "title": "Another song"}]
        attempts = []

        def request(job, cancel, timeout):
            if job["action"] == "download":
                identifier = job["url"].rsplit("=", 1)[-1]
                attempts.append(identifier)
                if len(attempts) == 1:
                    raise RuntimeError("Synthetic temporary failure")
            return self.run_job(job, cancel, timeout)

        self.module.run_job.side_effect = request
        self.start(sync=True)
        self.wait_finished()
        self.assertEqual(attempts, ["synthetic-0", "synthetic-1", "synthetic-0"])
        self.assertEqual(self.download_count, 2)
        self.assertEqual(self.sync.saved, 2)
        self.assertEqual(self.sync.total, 2)

    def test_existing_download_is_skipped_even_with_duplicate_or_changed_titles(self):
        self.cached_session()
        path = self.directory / "existing.mp3"
        path.write_bytes(b"synthetic existing download")
        self.module.atomic_json(self.module.DOWNLOADS_FILE, {"synthetic-0": str(path)})
        self.tracks = [{"id": "synthetic-0", "title": "Updated title"},
                       {"id": "synthetic-0", "title": "Duplicate title"}]
        self.start(sync=True)
        self.wait_finished()
        self.assertEqual(self.download_count, 0)
        self.assertEqual(self.sync.saved, 1)
        self.assertTrue(self.sync.ready)

    def local_song(self, title="Fixture", artist="Fixture", filename="existing.mp3"):
        path = self.directory / filename
        path.write_bytes(b"synthetic existing audio")
        song = {"title": title, "artist": artist, "path": str(path)}
        self.player.local_songs.append(song)
        return str(path)

    def test_unrecorded_library_match_is_reused_and_saved_before_playback(self):
        self.cached_session()
        path = self.local_song(title="Song (Official Audio)", artist="BoBurnham - Topic")
        self.tracks = [{"id": "synthetic-0", "title": "SONG", "artist": "Bo Burnham"}]
        self.module.atomic_json(self.module.DOWNLOADS_FILE, {"synthetic-0": str(self.directory / "moved.mp3")})
        self.module.atomic_json(self.module.DEFERRED_FILE,
                                {"synthetic-0": {"title": "Song", "retry_after": self.module.time() + 100}})
        ready = self.player.allow_playback

        def check_saved():
            self.assertEqual(json.loads(self.module.DOWNLOADS_FILE.read_text()), {"synthetic-0": path})
            ready()

        self.player.allow_playback = check_saved
        self.start(sync=True)
        self.wait_finished()
        self.assertEqual(self.download_count, 0)
        self.assertEqual((self.sync.saved, self.sync.total, self.sync.deferred), (1, 1, 0))
        self.assertTrue(self.player.playback.is_set())

    def test_filename_video_id_recovers_download_without_matching_tags(self):
        self.cached_session()
        path = self.local_song(title="Changed title", artist="Unknown Artist",
                               filename="old title [synthetic-0].mp3")
        self.start(sync=True)
        self.wait_finished()
        self.assertEqual(self.download_count, 0)
        self.assertEqual(json.loads(self.module.DOWNLOADS_FILE.read_text()), {"synthetic-0": path})

    def test_same_title_different_artist_or_version_still_downloads(self):
        self.cached_session()
        self.local_song(title="Fixture", artist="Someone Else")
        self.local_song(title="Fixture (Live)", artist="Fixture", filename="live.mp3")
        self.start(sync=True)
        self.wait_finished()
        self.assertEqual(self.download_count, 1)

    def test_distinct_liked_uploads_of_same_song_download_only_once(self):
        self.cached_session()
        self.tracks = [{"id": "synthetic-0", "title": "Song", "artist": "Artist"},
                       {"id": "synthetic-1", "title": "Artist - Song (Official Video)", "artist": "Artist - Topic"}]
        self.start(sync=True)
        self.wait_finished()
        self.assertEqual(self.download_count, 1)
        saved = json.loads(self.module.DOWNLOADS_FILE.read_text())
        self.assertEqual(saved["synthetic-0"], saved["synthetic-1"])
        self.assertEqual((self.sync.saved, self.sync.total), (1, 1))
        self.assertTrue(self.sync.ready)

    def test_invalid_links_and_known_unavailable_entries_are_not_downloaded(self):
        self.cached_session()
        self.tracks = [None, {}, {"id": "bad-id"}, {"id": ["synthetic-0"]},
                       {"id": "synthetic-1", "url": "https://example.com/watch?v=synthetic-1"},
                       {"id": "synthetic-2", "url": "https://youtu.be/synthetic-3"},
                       {"id": "synthetic-4", "is_available": False},
                       {"id": "synthetic-5", "availability": "private"},
                       {"id": "synthetic-6", "title": "[Deleted video]"},
                       {"id": "synthetic-7", "title": "Available", "url": "https://youtu.be/synthetic-7"}]
        self.start(sync=True)
        self.wait_finished()
        self.assertEqual(self.download_count, 1)
        self.assertEqual(self.sync.skipped, 9)
        self.assertEqual((self.sync.saved, self.sync.total), (1, 1))
        self.assertTrue(self.player.playback.is_set())
        self.assertIn("9 invalid or unavailable skipped", self.sync.message)

    def test_missing_empty_and_partial_files_do_not_block_download(self):
        self.cached_session()
        for filename in ("empty.mp3", "partial.mp3.part", ".youtubeCached [synthetic-0].mp3"):
            path = self.directory / filename
            path.write_bytes(b"" if filename == "empty.mp3" else b"unfinished audio")
            self.player.local_songs.append({"title": "Fixture", "artist": "Fixture", "path": str(path)})
        self.module.atomic_json(self.module.DOWNLOADS_FILE, {"synthetic-0": str(self.directory / "empty.mp3")})
        self.start(sync=True)
        self.wait_finished()
        self.assertEqual(self.download_count, 1)

    def test_cancellation_during_library_check_keeps_playback_held(self):
        self.cached_session()

        def scan(cancel):
            cancel.set()
            return []

        self.player.local_songs_for_sync = scan
        self.start(sync=True)
        self.wait_finished()
        self.assertEqual(self.download_count, 0)
        self.assertFalse(self.player.playback.is_set())
        self.assertFalse(self.sync.ready)

    def test_likes_and_multiple_playlists_share_one_deduplicated_queue(self):
        self.cached_session()
        first = "https://www.youtube.com/playlist?list=FixtureOne"
        second = "https://www.youtube.com/playlist?list=FixtureTwo"
        self.player._playlist_urls = [first, second, first]
        self.tracks = [{"id": "synthetic-0", "title": "Shared Song", "artist": "Artist"}]
        self.playlists = {
            first: [self.tracks[0], {"id": "synthetic-1", "title": "New Song", "artist": "Artist"}],
            second: [{"id": "synthetic-2", "title": "Shared Song (Official Audio)", "artist": "Artist - Topic"},
                     {"id": "synthetic-1", "title": "New Song", "artist": "Artist"}],
        }
        self.start(sync=True)
        self.wait_finished()
        self.assertEqual(self.download_count, 2)
        self.assertEqual((self.sync.saved, self.sync.total), (2, 2))
        self.assertEqual(len([job for job in self.jobs if job['action'] == 'playlist']), 2)
        self.assertTrue(all(job['sponsorblock'] for job in self.jobs if job['action'] == 'download'))
        saved = json.loads(self.module.DOWNLOADS_FILE.read_text())
        self.assertEqual(saved['synthetic-0'], saved['synthetic-2'])
        self.assertEqual(len(saved), 3)
        self.assertTrue(self.player.playback.is_set())

    def test_missing_download_record_is_rebuilt_from_files_across_all_sources(self):
        self.cached_session()
        self.local_song(title="Fixture", artist="Fixture")
        url = "https://www.youtube.com/playlist?list=Fixture"
        self.player._playlist_urls = [url]
        self.playlists[url] = [{"id": "synthetic-1", "title": "Fixture", "artist": "Fixture"}]
        self.assertFalse(self.module.DOWNLOADS_FILE.exists())
        self.start(sync=True)
        self.wait_finished()
        self.assertEqual(self.download_count, 0)
        self.assertEqual(len(json.loads(self.module.DOWNLOADS_FILE.read_text())), 2)
        self.assertEqual(self.sync.total, 1)

    def test_parallel_queue_reduces_to_one_then_recovers_to_three_live(self):
        self.cached_session()
        self.tracks = [{"id": f"synthetic-{i}", "title": f"Song {i}", "artist": "Artist"} for i in range(6)]
        initial_started = [Event() for _ in range(3)]
        release_initial = [Event() for _ in range(3)]
        later_started = [Event() for _ in range(3)]
        release_later = Event()
        for event in [*release_initial, release_later]:
            self.addCleanup(event.set)
        current = [DownloadBudget(3, 1024 ** 2, 0, False, "Idle")]
        actual_download = self.player.ytHandle._download

        def download(url, directory, **kwargs):
            number = int(url[-1])
            if number < 3:
                initial_started[number].set()
                if not release_initial[number].wait(3):
                    raise RuntimeError("Fixture release timed out")
            else:
                kwargs['progress']({'phase': 'downloading', 'progress': 0.5})
                later_started[number - 3].set()
                if not release_later.wait(3):
                    raise RuntimeError("Fixture release timed out")
            return actual_download(url, directory, **kwargs)

        self.player.ytHandle._download = download
        with patch.object(self.module, "download_budget", side_effect=lambda: current[0]):
            self.start(sync=True)
            try:
                for event in initial_started:
                    self.assertTrue(event.wait(2))
                current[0] = DownloadBudget(1, 64 * 1024, 0, True, "Low")
                release_initial[0].set()
                release_initial[1].set()
                self.assertFalse(later_started[0].wait(0.35))
                release_initial[2].set()
                self.assertTrue(later_started[0].wait(2))
                self.assertFalse(later_started[1].wait(0.35))
                self.assertFalse(self.player.playback.is_set())
                self.assertEqual(self.sync.download_progress()[0]['progress'], 0.5)
                current[0] = DownloadBudget(3, 1024 ** 2, 0, False, "Idle")
                self.assertTrue(later_started[1].wait(2))
                self.assertTrue(later_started[2].wait(2))
            finally:
                for event in [*release_initial, release_later]:
                    event.set()
            self.wait_finished()
        self.assertEqual(self.download_count, 6)
        self.assertTrue(self.player.playback.is_set())
        self.assertEqual(self.sync.download_progress(), [])

    def test_stopping_downloads_keeps_completed_progress(self):
        self.cached_session()
        self.tracks = [{"id": f"synthetic-{index}", "title": "Fixture"} for index in range(7)]
        def request(job, cancel, timeout):
            result = self.run_job(job, cancel, timeout)
            if job["action"] == "download" and self.download_count == 3:
                cancel.set()
            return result
        self.module.run_job.side_effect = request
        self.start(sync=True)
        self.wait_finished()
        saved = json.loads(self.module.DOWNLOADS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(len(saved), 3)
        self.assertEqual(self.download_count, 3)
        self.assertTrue(self.sync.connected)
        self.assertEqual(self.sync.phase, "error")
        self.assertFalse(self.player.playback.is_set())
        self.assertIn("saved songs are kept", self.sync.message)

    def test_video_source_and_missing_dependency_repair(self):
        self.cached_session()
        missing_once = True
        def request(job, cancel, timeout):
            nonlocal missing_once
            if job["action"] == "playlist" and missing_once:
                missing_once = False
                self.jobs.append(dict(job))
                raise FixtureJobError("Synthetic missing support", "missing_dependency")
            return self.run_job(job, cancel, timeout)
        self.module.run_job.side_effect = request
        self.start(source="YouTube liked videos")
        self.wait_finished()
        self.assertTrue(self.sync.connected)
        self.assertEqual([job["action"] for job in self.jobs], ["prepare", "playlist", "setup", "playlist"])
        self.assertFalse(next(job for job in self.jobs if job["action"] == "setup")["music"])


@unittest.skipUnless(os.name == "nt", "Windows DPAPI")
class NativeSessionProtection(unittest.TestCase):
    def test_native_dpapi_round_trip_in_memory(self):
        module = isolated_sync_module()
        harmless = b"MusicApp harmless native protection API fixture"
        encrypted = module._crypt(harmless)
        self.assertNotEqual(encrypted, harmless)
        self.assertNotIn(harmless, encrypted)
        self.assertEqual(module._crypt(encrypted, decrypt=True), harmless)


if __name__ == "__main__":
    unittest.main(verbosity=2)
