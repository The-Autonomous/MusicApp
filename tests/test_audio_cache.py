"""Temporary cache checks use dummy files and mocked downloader workers."""
import os
from pathlib import Path
import sys
import shutil
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from audio_cache import AudioCache, youtube_video_id
from ytHandle import ytHandle

VIDEO_ID = "Abc_123-xYz"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


class YouTubeIDTests(unittest.TestCase):
    def test_supported_video_urls(self):
        for url in [URL, f"https://youtu.be/{VIDEO_ID}?t=5",
                    f"https://music.youtube.com/watch?v={VIDEO_ID}&list=LM",
                    f"https://m.youtube.com/shorts/{VIDEO_ID}",
                    f"https://youtube.com/live/{VIDEO_ID}",
                    f"https://www.youtube.com/embed/{VIDEO_ID}",
                    f"https://www.youtube-nocookie.com/embed/{VIDEO_ID}"]:
            with self.subTest(url=url):
                self.assertEqual(youtube_video_id(url), VIDEO_ID)

    def test_invalid_ids_and_unrelated_hosts_are_rejected(self):
        for url in [None, 123, VIDEO_ID, f"https://example.com/watch?v={VIDEO_ID}",
                    f"https://youtube.com.example.com/watch?v={VIDEO_ID}",
                    f"https://youtube.com@evil.invalid/watch?v={VIDEO_ID}",
                    f"https://user@youtube.com/watch?v={VIDEO_ID}",
                    f"https://youtu.be/{VIDEO_ID}/other", "https://youtu.be/short",
                    f"https://youtube.com/watch?v={VIDEO_ID}0",
                    f"https://youtube.com/watch?v={VIDEO_ID}&v={VIDEO_ID}",
                    "https://youtube.com/watch?v=bad!1234567",
                    f"ftp://youtube.com/watch?v={VIDEO_ID}",
                    f"https://youtube.com:8000/watch?v={VIDEO_ID}",
                    "https://[broken", "https://youtu.be/" + "a" * 5000]:
            with self.subTest(url=url):
                self.assertIsNone(youtube_video_id(url))


class WorkspaceTemporaryFiles(unittest.TestCase):
    def setUp(self):
        # Python 3.13 mkdtemp uses a private Windows ACL that excludes restricted
        # sandbox tokens. A normal mkdir inherits the workspace's usable ACL.
        self.directory = (ROOT / f"cache-test-{uuid4().hex}").resolve()
        self.assertEqual(self.directory.parent, ROOT.resolve())
        self.directory.mkdir()
        self.cache = AudioCache(self.directory)

    def tearDown(self):
        # Verify the recursive cleanup's exact target remains in this workspace.
        self.assertEqual(self.directory.parent, ROOT.resolve())
        self.assertTrue(self.directory.name.startswith("cache-test-"))
        shutil.rmtree(self.directory)

    def file(self, identity=VIDEO_ID, extension="webm", size=4, age=1):
        path = self.directory / f".youtubeCached [{identity}].{extension}"
        path.write_bytes(b"x" * size)
        os.utime(path, ns=(age * 1_000_000_000, age * 1_000_000_000))
        return path


class AudioCacheTests(WorkspaceTemporaryFiles):
    def test_reuse_preserves_metadata_modification_time(self):
        path = self.file()
        modified = path.stat().st_mtime_ns
        self.assertEqual(self.cache.find(VIDEO_ID), path)
        self.assertEqual(path.stat().st_mtime_ns, modified)
        self.assertGreater(path.stat().st_atime_ns, modified)

    def test_read_only_touch_failure_still_reuses_audio(self):
        path = self.file()
        with patch("audio_cache.os.utime", side_effect=PermissionError("read-only")):
            self.assertEqual(self.cache.find(VIDEO_ID), path)

    def test_empty_partial_legacy_user_files_and_nested_files_are_ignored(self):
        empty = self.file(size=0)
        names = [f".youtubeCached [{VIDEO_ID}].webm.part", ".youtubeCached.mp3",
                 "favorite.mp3", f".youtubeCached [{VIDEO_ID}].json"]
        others = [self.directory / name for name in names]
        for path in others:
            path.write_bytes(b"data")
        nested = self.directory / "nested"
        nested.mkdir()
        nested_song = nested / f".youtubeCached [{VIDEO_ID}].webm"
        nested_song.write_bytes(b"data")
        self.assertIsNone(self.cache.find(VIDEO_ID))
        self.cache.prune()
        self.assertTrue(all(path.exists() for path in [empty, nested_song] + others))

    def test_oldest_unprotected_files_are_evicted_to_count_budget(self):
        files = [self.file(identity=f"Video{i:06d}", age=i + 1) for i in range(10)]
        self.cache.prune(protected=(files[0], files[-1]))
        self.assertTrue(files[0].exists())
        self.assertTrue(files[-1].exists())
        self.assertFalse(files[1].exists())
        self.assertFalse(files[2].exists())
        self.assertEqual(sum(path.exists() for path in files), 8)

    def test_byte_budget_and_oversized_active_track(self):
        self.cache.max_bytes = 8
        active = self.file(identity="Active00001", size=12, age=1)
        old = self.file(identity="Older000001", size=4, age=2)
        incoming = self.file(identity="Newest00001", size=4, age=3)
        self.cache.prune(protected=(active, incoming))
        self.assertTrue(active.exists())
        self.assertTrue(incoming.exists())
        self.assertFalse(old.exists())
        self.cache.prune(protected=(incoming,))
        self.assertFalse(active.exists())
        self.assertTrue(incoming.exists())

    def test_symbolic_links_are_not_reused_or_evicted(self):
        target = self.directory / "owned-by-user.webm"
        target.write_bytes(b"data")
        link = self.directory / f".youtubeCached [{VIDEO_ID}].webm"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("Windows symlink creation is unavailable")
        self.assertIsNone(self.cache.find(VIDEO_ID))
        self.cache.max_bytes = 1
        self.cache.prune()
        self.assertTrue(link.is_symlink())
        self.assertTrue(target.exists())


class DownloaderCacheTests(WorkspaceTemporaryFiles):
    def setUp(self):
        super().setUp()
        self.handle = ytHandle()
        self.handle._audio_cache = self.cache
        self.cookies = Mock(return_value=[])
        self.handle.configure(cookies_provider=self.cookies)

    def test_cached_song_bypasses_worker_policy_and_credentials(self):
        path = self.file()
        with patch("ytHandle.run_job") as worker, patch("ytHandle.game_running") as policy:
            self.assertEqual(self.handle.download_single_song_to_cache(URL), str(path))
        worker.assert_not_called()
        policy.assert_not_called()
        self.cookies.assert_not_called()

    def test_hit_is_immediate_while_another_download_owns_lock(self):
        path = self.file()
        self.handle._download_lock.acquire()
        try:
            with patch("ytHandle.run_job") as worker:
                self.assertEqual(self.handle.download_single_song_to_cache(URL), str(path))
            worker.assert_not_called()
        finally:
            self.handle._download_lock.release()

    def test_download_rechecks_cache_after_waiting_for_lock(self):
        entered = Event()
        done = Event()
        results = []
        self.handle._download_lock.acquire()
        held_by_test = True

        def call():
            entered.set()
            results.append(self.handle._download(URL, self.directory, mp3=False,
                                                 cache_video_id=VIDEO_ID))
            done.set()

        with patch("ytHandle.run_job") as worker, patch("ytHandle.game_running") as policy:
            thread = Thread(target=call)
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                path = self.file()
                self.handle._download_lock.release()
                held_by_test = False
                self.assertTrue(done.wait(2))
            finally:
                self.handle.close()
                if held_by_test:
                    self.handle._download_lock.release()
                thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [str(path)])
        worker.assert_not_called()
        policy.assert_not_called()
        self.cookies.assert_not_called()

    def test_new_native_download_prunes_only_unprotected_owned_cache(self):
        old = [self.file(identity=f"Video{i:06d}", age=i + 1) for i in range(8)]
        self.handle.configure(active_path_provider=lambda: str(old[0]))

        def download(job, cancel, timeout):
            self.assertFalse(job["mp3"])
            path = self.file(age=100)
            return {"path": str(path), "title": "Dummy"}

        with patch("ytHandle.run_job", side_effect=download), patch("ytHandle.game_running", return_value=False):
            result = self.handle.download_single_song_to_cache(URL)
        self.assertEqual(result, str(self.directory / f".youtubeCached [{VIDEO_ID}].webm"))
        self.assertTrue(old[0].exists())
        self.assertFalse(old[1].exists())
        self.assertEqual(len(self.cache._entries()), 8)
        self.cookies.assert_called_once()

    def test_cancelled_request_does_not_start_worker_or_read_credentials(self):
        self.handle.cancel.set()
        with patch("ytHandle.run_job") as worker:
            self.assertIsNone(self.handle.download_single_song_to_cache(URL))
        worker.assert_not_called()
        self.cookies.assert_not_called()

    def test_failure_releases_lock_and_preserves_existing_files(self):
        legacy = self.directory / ".youtubeCached.mp3"
        legacy.write_bytes(b"legacy")
        with patch("ytHandle.run_job", side_effect=RuntimeError("mock failure")), patch(
                "ytHandle.game_running", return_value=False):
            self.assertIsNone(self.handle.download_single_song_to_cache(URL))
        self.assertTrue(self.handle._download_lock.acquire(blocking=False))
        self.handle._download_lock.release()
        self.assertEqual(legacy.read_bytes(), b"legacy")


if __name__ == "__main__":
    unittest.main()
