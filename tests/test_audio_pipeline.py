"""Real decoding/effects checks using a fake output device and disposable PCM."""

import atexit
from pathlib import Path
import shutil
from threading import Event, Thread
from time import monotonic, sleep
import unittest
from unittest.mock import patch
import uuid
import wave

import numpy as np
import audio_eq

# Importing audio creates its shared player, but must not read user EQ settings.
with patch.object(audio_eq.AudioEQ, "_load_settings", return_value={}), patch.object(atexit, "register"):
    import audio


class FakeOutputStream:
    """Explicit callback pumping, with no PortAudio device or background timer."""
    def __init__(self, **options):
        self.options = options
        self.active = False
        self.closed = False
        self.starts = 0

    def start(self):
        if self.closed:
            raise RuntimeError("Closed fake device")
        self.active = True
        self.starts += 1

    def stop(self):
        self.active = False
        self.options["finished_callback"]()

    abort = stop

    def close(self):
        self.active = False
        self.closed = True

    def pump(self):
        data = np.empty((self.options["blocksize"], self.options["channels"]), dtype=np.float32)
        try:
            self.options["callback"](data, len(data), None, False)
        except audio.sd.CallbackStop:
            self.active = False
            self.options["finished_callback"]()
        return data


class BufferTests(unittest.TestCase):
    def test_wrapping_retains_sample_order_applies_volume_and_zero_fills(self):
        ring = audio.RobustAudioBuffer(1, 2, 2, 10)
        first = np.column_stack((np.arange(8), -np.arange(8))).astype(np.float32)
        second = np.column_stack((np.arange(8, 14), -np.arange(8, 14))).astype(np.float32)
        self.assertTrue(ring.write(first))
        initial = np.empty((6, 2), dtype=np.float32)
        self.assertEqual(ring.read_into(initial, 1), 6)
        np.testing.assert_array_equal(initial, first[:6])
        self.assertTrue(ring.write(second))
        output = np.full((10, 2), np.nan, dtype=np.float32)
        self.assertEqual(ring.read_into(output, 0.25), 8)
        np.testing.assert_array_equal(output[:8], np.vstack((first[6:], second)) * 0.25)
        np.testing.assert_array_equal(output[8:], 0)
        self.assertEqual(ring.available_samples, 0)

    def test_callback_returns_silence_promptly_if_producer_holds_lock(self):
        ring = audio.RobustAudioBuffer(1, 2, 2, 10)
        ring.write(np.ones((4, 2), dtype=np.float32))
        held, release = Event(), Event()

        def hold():
            with ring._condition:
                held.set()
                release.wait(2)

        thread = Thread(target=hold)
        thread.start()
        try:
            self.assertTrue(held.wait(1))
            output = np.full((4, 2), np.nan, dtype=np.float32)
            started = monotonic()
            count = ring.read_into(output, 1)
            self.assertLess(monotonic() - started, 0.2)
            self.assertEqual(count, -1)
            np.testing.assert_array_equal(output, 0)
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(ring.available_samples, 4)

    def test_closing_releases_a_writer_waiting_on_full_buffer(self):
        ring = audio.RobustAudioBuffer(1, 2, 2, 10)
        ring.write(np.ones((ring.buffer_size, 2), dtype=np.float32))
        started, finished = Event(), Event()
        results = []

        def write():
            started.set()
            results.append(ring.write(np.ones((2, 2), dtype=np.float32)))
            finished.set()

        thread = Thread(target=write)
        thread.start()
        try:
            self.assertTrue(started.wait(1))
            self.assertFalse(finished.wait(0.04))
            ring.close()
            self.assertTrue(finished.wait(1))
        finally:
            ring.close()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [False])


class DecodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = Path(__file__).resolve().parents[1]
        cls.fixture = cls.workspace / (".qa-audio-" + uuid.uuid4().hex)
        # Normal directory permissions inherit from the workspace on Windows.
        cls.fixture.mkdir()
        cls.mono_path = cls.fixture / "mono-48000.wav"
        cls.marked_path = cls.fixture / "seek-44100.wav"
        cls.short_path = cls.fixture / "short-44100.wav"
        cls.write_wave(cls.mono_path, 48000, np.full((12000, 1), 0.3, dtype=np.float32))
        marker = np.full((26460, 2), 0.25, dtype=np.float32)
        marker[13230:] = -0.25
        cls.write_wave(cls.marked_path, 44100, marker)
        cls.write_wave(cls.short_path, 44100, np.full((1764, 2), 0.2, dtype=np.float32))

    @staticmethod
    def write_wave(path, sample_rate, pcm):
        integer_pcm = np.round(np.clip(pcm, -1, 1) * 32767).astype("<i2")
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(pcm.shape[1])
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(integer_pcm.tobytes())

    @classmethod
    def tearDownClass(cls):
        target = cls.fixture.resolve()
        if target.parent != cls.workspace.resolve() or not target.name.startswith(".qa-audio-"):
            raise RuntimeError("Refusing cleanup outside the owned audio fixture")
        shutil.rmtree(target)

    def setUp(self):
        self.patchers = (
            patch.object(audio.sd, "OutputStream", FakeOutputStream),
            patch.object(audio_eq.AudioEQ, "_load_settings", return_value={}),
            patch.object(atexit, "register"),
        )
        for patcher in self.patchers:
            patcher.start()
        self.player = audio.AudioPlayerRoot(buffer_size_seconds=0.1)
        self.player.set_volume(1, set_directly=True)
        self.readers = []

    def tearDown(self):
        try:
            self.player.close()
            for thread in self.readers:
                self.assertFalse(thread.is_alive(), "Owned decoding thread survived cleanup")
        finally:
            for patcher in reversed(self.patchers):
                patcher.stop()

    def play(self, path, position=0, paused=False):
        self.assertTrue(self.player._start_playback_session(str(path), position, not paused))
        self.readers.append(self.player._reader_thread)
        return self.player._stream

    def drain(self):
        stream = self.player._stream
        decoded = []
        deadline = monotonic() + 3
        while stream.active and monotonic() < deadline:
            previous = self.player.get_pos()
            data = stream.pump()
            count = round((self.player.get_pos() - previous) * self.player.samplerate)
            if count:
                decoded.append(data[:count].copy())
            # Give the real decoder an opportunity to refill; this is not audio playback.
            sleep(0.001)
        self.assertFalse(stream.active, "The fake stream did not reach end of audio")
        self.assertFalse(self.player.get_busy())
        return np.vstack(decoded) if decoded else np.empty((0, 2), dtype=np.float32)

    def test_real_pyav_resamples_mono_to_stereo_and_drains_complete_audio(self):
        self.play(self.mono_path)
        decoded = self.drain()
        self.assertEqual(decoded.shape, (11025, 2))
        self.assertTrue(np.isfinite(decoded).all())
        np.testing.assert_allclose(decoded[:, 0], decoded[:, 1], atol=1e-6)
        self.assertGreater(float(np.mean(decoded)), 0.15)
        self.assertLess(float(np.max(np.abs(decoded))), 0.31)
        self.assertAlmostEqual(self.player.get_pos(), 0.25, places=5)
        self.assertAlmostEqual(self.player.get_duration(), 0.25, places=5)

    def test_seek_plays_the_requested_region_without_leading_seek_padding(self):
        self.play(self.marked_path, position=0.4)
        decoded = self.drain()
        self.assertEqual(len(decoded), 8820)
        self.assertLess(float(np.max(decoded)), -0.24)
        self.assertAlmostEqual(self.player.get_pos(), 0.6, places=5)

    def test_pause_keeps_position_and_device_resumes_without_reopening(self):
        stream = self.play(self.marked_path)
        stream.pump()
        before = self.player.get_pos()
        self.player.pause()
        self.assertFalse(self.player.get_busy())
        np.testing.assert_array_equal(stream.pump(), 0)
        self.assertEqual(self.player.get_pos(), before)
        self.player.unpause()
        self.assertTrue(self.player.get_busy())
        self.assertIs(self.player._stream, stream)
        self.assertEqual(stream.starts, 2)
        self.drain()

    def test_tracks_reuse_device_and_closing_stops_a_blocked_decoder(self):
        stream = self.play(self.marked_path)
        previous_reader = self.player._reader_thread
        second = self.play(self.short_path)
        self.assertIs(second, stream)
        self.assertFalse(previous_reader.is_alive())
        decoded = self.drain()
        self.assertEqual(len(decoded), 1764)
        self.play(self.marked_path, paused=True)
        blocked_reader = self.player._reader_thread
        deadline = monotonic() + 1
        while self.player._buffer.available_samples < self.player._buffer.buffer_size and monotonic() < deadline:
            sleep(0.001)
        self.assertEqual(self.player._buffer.available_samples, self.player._buffer.buffer_size)
        self.assertTrue(blocked_reader.is_alive())
        self.player.close()
        self.assertFalse(blocked_reader.is_alive())
        self.assertTrue(stream.closed)
        self.assertIsNone(self.player._stream)

    def test_seek_preserves_pause_and_reuses_the_output_device(self):
        stream = self.play(self.marked_path)
        self.player.pause()
        self.assertTrue(self.player.set_pos(0.4))
        self.readers.append(self.player._reader_thread)
        self.assertFalse(self.player.get_busy())
        self.assertIs(self.player._stream, stream)
        self.assertAlmostEqual(self.player.get_pos(), 0.4, places=5)
        self.player.unpause()
        self.assertEqual(len(self.drain()), 8820)


class EffectsTests(unittest.TestCase):
    def make_eq(self, samplerate=44100, chunk_size=64):
        with patch.object(audio_eq.AudioEQ, "_load_settings", return_value={}), patch.object(atexit, "register"):
            return audio_eq.AudioEQ(samplerate, 2, chunk_size)

    def test_flat_equalizer_bypasses_fft_without_copying_or_changing_samples(self):
        eq = self.make_eq()
        data = np.linspace(-0.5, 0.5, 128, dtype=np.float32).reshape(64, 2)
        with patch.object(eq, "_initialize", side_effect=AssertionError("Flat EQ initialized FFT")):
            self.assertIs(eq.process(data), data)

    def test_equalizer_handles_active_gains_and_partial_final_blocks(self):
        eq = self.make_eq(samplerate=16000)
        eq.set_gain(31, 12)
        eq.set_gain(1000, -12)
        eq.set_gain(16000, 6)
        data = np.sin(np.arange(128, dtype=np.float32) / 7).reshape(64, 2) * 0.2
        for block in (data, data, data[:17], np.zeros_like(data)):
            processed = eq.process(block)
            self.assertEqual(processed.shape, block.shape)
            self.assertTrue(np.isfinite(processed).all())
            self.assertLessEqual(float(np.max(np.abs(processed))), 1)
        self.assertGreater(float(np.max(np.abs(eq.process(data)))), 0)

    def test_echo_delay_feedback_and_wet_mix_across_block_boundaries(self):
        echo = audio_eq.AudioEcho(1000, 2, delay_ms=10, feedback=0.5, wet=0.5)
        data = np.zeros((31, 2), dtype=np.float32)
        data[0] = 1
        processed = np.vstack([echo.process(data[:7]), echo.process(data[7:18]), echo.process(data[18:])])
        expected = np.zeros_like(data)
        expected[0], expected[10], expected[20], expected[30] = 0.5, 0.5, 0.25, 0.125
        np.testing.assert_array_equal(processed, expected)
        echo.reset_state()
        np.testing.assert_array_equal(echo.process(np.zeros((10, 2), dtype=np.float32)), 0)

    def test_zero_wet_echo_bypasses_without_allocating_an_output(self):
        echo = audio_eq.AudioEcho(44100, 2, wet=0)
        data = np.ones((64, 2), dtype=np.float32)
        self.assertIs(echo.process(data), data)


if __name__ == "__main__":
    unittest.main()
