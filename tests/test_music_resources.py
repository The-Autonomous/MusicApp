"""Playback control/resource checks with isolated audio and service doubles."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
from threading import Event
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def isolated_music_module():
    audio = ModuleType("audio")
    audio.AudioPlayer = SimpleNamespace(
        eq=SimpleNamespace(reset_state=Mock(), _save_settings=Mock()),
        echo=None, _gaming_mode=True, set_volume=Mock(),
        _start_playback_session=Mock(return_value=True), get_pos=Mock(return_value=10.0),
        get_busy=Mock(return_value=True), pause=Mock(), unpause=Mock(),
        stop=Mock(), close=Mock())
    recommender = ModuleType("playerRecommend")
    recommender.PlayerRecommender = Mock(side_effect=lambda: SimpleNamespace(
        close=Mock(), log_song_play=Mock()))
    downloader = ModuleType("ytHandle")
    downloader.ytHandle = Mock(side_effect=lambda: SimpleNamespace(configure=Mock(), close=Mock()))
    logger = ModuleType("log_loader")
    logger.log_loader = Mock(return_value=Mock())
    runtime = ModuleType("runtime_policy")
    runtime.game_running = Mock(return_value=False)
    metadata = ModuleType("mutagen")
    metadata.File = Mock(side_effect=AssertionError("No real media should be opened"))
    specification = importlib.util.spec_from_file_location(
        "_musicapp_music_resource_fixture", ROOT / "music.py")
    module = importlib.util.module_from_spec(specification)
    # These replacements exist only while importing this private fixture module.
    # Test discovery can import the real modules without inheriting our doubles.
    with patch.dict(sys.modules, {"audio": audio, "playerRecommend": recommender,
                                 "ytHandle": downloader, "log_loader": logger,
                                 "runtime_policy": runtime, "mutagen": metadata}):
        specification.loader.exec_module(module)
    return module


class TracedEvent(Event):
    def __init__(self):
        super().__init__()
        self.waits = []
        self.entered = Event()

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.entered.set()
        return super().wait(timeout)


class ScriptedWake:
    """End a synchronous core-loop check after a chosen number of waits."""
    def __init__(self, player, waits=1, on_wait=None):
        self.player = player
        self.limit = waits
        self.on_wait = on_wait
        self.waits = []
        self.flag = False

    def set(self):
        self.flag = True

    def clear(self):
        self.flag = False

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.on_wait:
            self.on_wait(len(self.waits), timeout)
        if len(self.waits) >= self.limit:
            self.player._shutdown.set()
        return self.flag


class MusicFixture(unittest.TestCase):
    def setUp(self):
        self.module = isolated_music_module()
        self.directory = (ROOT / "tests" / f"MusicApp-music-test-{uuid4().hex}").resolve()
        self.directory.mkdir()  # Inherit the workspace ACL, including sandbox access.
        self.addCleanup(self.clean_directory)
        self.module.APP_DIR = self.directory
        self.module.MusicPlayer.SAVE_STATE_FILE = str(self.directory / "state.json")
        self.players = []
        self.addCleanup(self.close_players)
        self.player = self.make_player()

    def clean_directory(self):
        checked = self.directory.resolve()
        if checked.parent != (ROOT / "tests").resolve() or not checked.name.startswith("MusicApp-music-test-"):
            raise AssertionError("Unexpected cleanup target")
        shutil.rmtree(checked)

    def close_players(self):
        for player in self.players:
            if player._core_thread and player._core_thread.is_alive():
                player._shutdown.set()
                player._wake.set()
                player._core_thread.join(2)
            player.close()
            self.assertFalse(player.initializer_thread.is_alive())
            self.assertFalse(player._core_thread and player._core_thread.is_alive())

    def make_player(self, state=None, startup_hold=True):
        if state is not None:
            Path(self.module.MusicPlayer.SAVE_STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
        player = self.module.MusicPlayer([], Mock(), Mock(), Mock(), Mock(), lambda: False,
                                         startup_hold=startup_hold)
        self.players.append(player)
        player.initializer_thread.join(2)
        self.assertFalse(player.initializer_thread.is_alive())
        player.lyrics_enabled = False
        return player

    def song(self, title="Blue Red", artist="Artist", identity="one"):
        return {"path": str(self.directory / f"{identity}.mp3"), "artist": artist,
                "title": title, "duration": 300.0,
                "_search": f"{artist} {title}".lower()}

    def add(self, song):
        with patch.object(self.player, "_song_from_path", return_value=dict(song)):
            self.player.add_downloaded_song(song["path"])

    def start_core_once(self, waits=1, on_wait=None):
        self.player.allow_playback()
        wake = ScriptedWake(self.player, waits, on_wait)
        self.player._wake = wake
        with patch.object(self.player, "save_playback_state"), patch.object(self.player, "save_meta_cache"):
            self.player.core_player_loop()
        self.module.ll.error.assert_not_called()
        return wake


class StartupAndControlTests(MusicFixture):
    def test_player_enable_intent_is_preserved_while_setup_blocks_playback(self):
        self.player.set_enabled(False)
        self.assertTrue(self.player.pause_event.is_set())
        self.player.set_enabled(True)
        self.assertFalse(self.player.pause_event.is_set())
        self.assertFalse(self.player._playback_ready.is_set())
        self.assertTrue(self.player._commands.empty())
        self.module.AudioPlayer._start_playback_session.assert_not_called()

    def test_hold_preserves_saved_pause_and_position_and_rejects_play(self):
        for paused in (False, True):
            with self.subTest(paused=paused):
                path = str(self.directory / "resume.mp3")
                player = self.make_player({"path": path, "elapsed": 37.5, "paused": paused})
                song = self.song()
                player.pause()
                player.play_song(song)
                self.assertFalse(player._start_song(song))
                self.assertTrue(player._commands.empty())
                self.assertEqual(player.pause_event.is_set(), paused)
                self.module.AudioPlayer._start_playback_session.assert_not_called()
                player.save_playback_state()
                saved = json.loads(Path(player.SAVE_STATE_FILE).read_text(encoding="utf-8"))
                self.assertEqual(saved["elapsed"], 37.5)
                self.assertEqual(saved["paused"], paused)

    def test_allow_playback_starts_a_saved_paused_track_without_unpausing(self):
        self.player.pause_event.set()
        self.player.allow_playback()
        song = self.song()
        self.assertTrue(self.player._start_song(song, 37.5))
        self.module.AudioPlayer._start_playback_session.assert_called_once_with(
            song["path"], 37.5, False)
        self.assertTrue(self.player.pause_event.is_set())

    def test_hold_waits_without_polling_and_shutdown_wakes_owned_thread(self):
        wake = TracedEvent()
        self.player._wake = wake
        self.player.core_handler()
        self.assertTrue(wake.entered.wait(1))
        self.assertEqual(wake.waits, [None])
        self.module.AudioPlayer._start_playback_session.assert_not_called()
        self.player.close()
        self.assertFalse(self.player._core_thread.is_alive())

    def test_allow_playback_arriving_before_held_wait_is_not_lost(self):
        waits = []

        class ReadyOnWait:
            def clear(inner):
                pass

            def set(inner):
                pass

            def wait(inner, timeout=None):
                waits.append(timeout)
                if len(waits) == 1:
                    self.player.allow_playback()
                else:
                    self.player._shutdown.set()
                return True

        self.player._wake = ReadyOnWait()
        with patch.object(self.player, "save_playback_state"):
            self.player.core_player_loop()
        self.assertEqual(waits, [None, 15.0])

    def test_all_command_batches_finish_before_waiting(self):
        self.player.allow_playback()
        for value in range(70):
            self.player._command("fixture", value)
        handled = []
        self.player._handle_command = lambda action, value: handled.append(value)
        observed = []
        self.start_core_once(on_wait=lambda count, timeout: observed.append(len(handled)))
        self.assertEqual(handled, list(range(70)))
        self.assertEqual(observed, [70])

    def test_command_arriving_during_processing_preserves_wake(self):
        self.player.allow_playback()
        self.player._command("fixture", "first")
        handled = []

        def handle(action, value):
            handled.append(value)
            if value == "first":
                self.player._command("fixture", "arrived")

        self.player._handle_command = handle
        wake = self.start_core_once()
        self.assertEqual(handled, ["first", "arrived"])
        self.assertTrue(wake.flag)


class IdleAndLibraryTests(MusicFixture):
    def test_idle_intervals_cover_pause_lyrics_audio_and_radio(self):
        self.assertEqual(self.player._idle_wait_interval(), 15.0)
        self.player.current_song = self.song()
        self.player._lyric_lines = [(0, "line")]
        self.player.lyrics_enabled = True
        self.assertEqual(self.player._idle_wait_interval(), 0.25)
        self.player.pause_event.set()
        self.assertEqual(self.player._idle_wait_interval(), 15.0)
        self.player.pause_event.clear()
        self.player.lyrics_enabled = False
        self.assertEqual(self.player._idle_wait_interval(), 1.0)
        self.player.current_player_mode.set()
        self.player.pause_event.set()
        self.assertEqual(self.player._idle_wait_interval(), 1.0)

    def test_paused_core_uses_long_wait_even_with_lyrics(self):
        self.player.current_song = self.song()
        self.player._played_logged = False
        self.player.pause_event.set()
        self.player.lyrics_enabled = True
        self.player._lyric_lines = [(0, "line")]
        self.assertEqual(self.start_core_once().waits, [15.0])

    def test_paused_core_never_writes_recurring_playback_checkpoints(self):
        self.player.allow_playback()
        self.player.current_song = self.song()
        self.player._played_logged = True
        self.player.pause_event.set()
        self.player._save_due = False
        self.player._last_save = 0
        self.player._last_meta_save = 10000
        wake = ScriptedWake(self.player, waits=3)
        self.player._wake = wake
        with patch.object(self.module, "monotonic", return_value=1000), patch.object(
                self.player, "save_playback_state") as save:
            self.player.core_player_loop()
        # The single final save is shutdown, not a periodic write while paused.
        self.assertEqual(save.call_count, 1)
        self.assertEqual(wake.waits, [15.0, 15.0, 15.0])

    def test_active_playback_keeps_periodic_resume_checkpoint(self):
        self.player.allow_playback()
        self.player.current_song = self.song()
        self.player._played_logged = True
        self.player._save_due = False
        self.player._last_save = 0
        self.player._last_meta_save = 10000
        self.player._wake = ScriptedWake(self.player)
        with patch.object(self.module, "monotonic", return_value=1000), patch.object(
                self.player, "save_playback_state") as save:
            self.player.core_player_loop()
        self.assertEqual(save.call_count, 2)  # Active checkpoint plus final shutdown save.

    def test_library_scan_batches_notifications(self):
        names = [f"song-{number}.mp3" for number in range(130)]
        notifications = []
        self.player._wake = SimpleNamespace(set=lambda: notifications.append(len(self.player.shuffler.cache)))
        with patch.object(self.module.os, "walk", return_value=[(str(self.directory), [], names)]), patch.object(
                self.player, "_song_from_path", side_effect=lambda path: self.song(identity=Path(path).stem)):
            self.player.initialize_cache([str(self.directory)])
        self.assertEqual(notifications, [1, 64, 128, 130])
        self.assertEqual(len(self.player.shuffler.cache), 130)

    def test_rejected_metadata_does_not_repeat_initial_library_notification(self):
        names = [f"song-{number}.mp3" for number in range(20)]
        notifications = []
        self.player._wake = SimpleNamespace(set=lambda: notifications.append(len(self.player.shuffler.cache)))
        with patch.object(self.module.os, "walk", return_value=[(str(self.directory), [], names)]), patch.object(
                self.player, "_song_from_path", side_effect=[self.song()] + [None] * 19):
            self.player.initialize_cache([str(self.directory)])
        self.assertEqual(notifications, [1, 1])  # First valid file and completion only.

    def test_sync_library_scan_includes_files_beyond_fast_load_and_deduplicates_paths(self):
        first = self.song(identity="first")
        self.add(first)
        self.player._directories = [str(self.directory), str(self.directory)]
        paths = []

        def metadata(path):
            paths.append(Path(path).name)
            return self.song(identity=Path(path).stem)

        names = ["first.mp3", "later.flac", ".youtubeCached.mp3", "partial.mp3.part"]
        with patch.object(self.module.os, "walk", return_value=[(str(self.directory), [], names)]), patch.object(
                self.player, "_song_from_path", side_effect=metadata):
            songs = list(self.player.local_songs_for_sync(Event()))
        self.assertEqual(paths, ["first.mp3", "later.flac"])
        self.assertEqual(len(songs), 2)

    def test_sync_library_scan_can_be_cancelled_while_startup_scan_is_pending(self):
        self.player._cache_ready.clear()
        cancel = Event()
        cancel.set()
        with self.assertRaisesRegex(RuntimeError, "Cancelled"):
            list(self.player.local_songs_for_sync(cancel))

    def test_permanent_youtube_downloads_share_gta_enhanced_target(self):
        enhanced = str(self.directory / "GTAV Enhanced" / "User Music")
        with patch.object(self.module.MusicPlayer, "initialize_cache"):
            player = self.module.MusicPlayer([str(self.directory), enhanced], Mock(), Mock(), Mock(),
                                             Mock(), lambda: False, startup_hold=True)
        self.players.append(player)
        player.initializer_thread.join(2)
        self.assertEqual(player.ytHandle.target_dir, Path(enhanced))


class LocalSearchTests(MusicFixture):
    def test_search_requires_all_tokens_and_ignores_common_stopwords(self):
        matching = self.song(title="Blue", artist="Red", identity="match")
        self.add(matching)
        self.add(self.song(title="Blue", artist="Other", identity="miss"))
        result = self.player.get_search_term("by the red and blue")
        self.assertEqual(result, [("Red - Blue", matching["path"], "path")])
        self.assertEqual(self.player.get_search_term("by the and in ft"), [])

    def test_ranking_cap_and_tie_order_are_stable(self):
        for index, title in enumerate(["Blue Long Name", "Blue", "Blue Two", "Blue End"]):
            self.add(self.song(title=title, artist="A", identity=str(index)))
        result = self.player.get_search_term("blue", max_results=3)
        self.assertEqual([item[0] for item in result], ["A - Blue", "A - Blue Two", "A - Blue End"])
        self.assertEqual(self.player.get_search_term("blue", max_results=0), [])

    def test_returned_results_cannot_mutate_cached_list(self):
        self.add(self.song())
        result = self.player.get_search_term("blue")
        result.clear()
        self.assertEqual(len(self.player.get_search_term("blue")), 1)

    def test_cached_query_does_not_scan_library_again(self):
        self.add(self.song())
        expected = self.player.get_search_term("blue")

        class ForbiddenIteration:
            def __iter__(self):
                raise AssertionError("A cached query scanned the library")

        self.player.shuffler.cache = ForbiddenIteration()
        self.assertEqual(self.player.get_search_term("BLUE"), expected)
        self.player.shuffler.cache = []

    def test_new_song_invalidates_a_cached_empty_result(self):
        self.assertEqual(self.player.get_search_term("blue"), [])
        self.add(self.song())
        self.assertEqual(len(self.player.get_search_term("blue")), 1)

    def test_metadata_update_invalidates_cached_search_without_duplicate_song(self):
        song = self.song()
        self.add(song)
        generation = self.player._library_generation
        self.assertEqual(len(self.player.get_search_term("blue")), 1)
        updated = dict(song, title="Green", _search="artist green")
        self.add(updated)
        self.assertGreater(self.player._library_generation, generation)
        self.assertEqual(self.player.get_search_term("blue"), [])
        self.assertEqual(self.player.get_search_term("green"), [("Artist - Green", song["path"], "path")])
        self.assertEqual(len(self.player.shuffler.cache), 1)

    def test_search_cache_stays_bounded_and_distinguishes_result_caps(self):
        self.add(self.song())
        for number in range(20):
            self.player.get_search_term(f"fixture {number}")
        self.assertLessEqual(len(self.player._search_cache), 8)
        self.assertEqual(self.player.get_search_term("blue", max_results=0), [])
        self.assertEqual(len(self.player.get_search_term("blue", max_results=1)), 1)
        self.assertLessEqual(len(self.player._search_cache), 8)

    def test_explicit_result_list_does_not_reuse_library_cache(self):
        self.add(self.song())
        self.player.get_search_term("blue")
        provided = [("Other Blue", "https://example.invalid/track", "url")]
        self.assertEqual(self.player.get_search_term("blue", provided), provided)


if __name__ == "__main__":
    unittest.main()
