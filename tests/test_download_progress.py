"""Progress presentation and mid-song throttling without native UI windows."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from download_progress import DownloadReporter, LiveThrottle
from ui_loading import LoadingHelper, song_progress_display


class ProgressTests(unittest.TestCase):
    def test_worker_publishes_download_fraction_and_processing_phase(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "progress.json"
            reporter = DownloadReporter(path)
            reporter({'status': 'downloading', 'downloaded_bytes': 25, 'total_bytes': 100})
            self.assertEqual(json.loads(path.read_text())['progress'], 0.25)
            reporter.postprocess({'postprocessor': 'ModifyChapters'})
            value = json.loads(path.read_text())
            self.assertEqual(value['phase'], 'processing')
            self.assertIn('Removing non-music sections', song_progress_display(dict(value, title='Song'))[0])
            reporter.publish('finished', force=True)
            self.assertEqual(json.loads(path.read_text())['progress'], 1)

    def test_unknown_total_and_bad_numbers_do_not_display_false_completion(self):
        self.assertEqual(song_progress_display({'title': 'Song', 'progress': None})[1], None)
        self.assertEqual(song_progress_display({'title': 'Song', 'progress': float('nan')})[1], None)
        self.assertEqual(song_progress_display({'title': 'Song', 'progress': 1.5})[1], 1)

    def test_list_rows_follow_active_downloads_and_keep_each_fraction(self):
        helper = LoadingHelper.__new__(LoadingHelper)
        helper.download_frame = Mock()
        helper.download_rows = [(Mock(), Mock(), Mock()) for _ in range(3)]
        helper.footer = Mock()
        helper.collapsed = False
        reports = [{'title': f'Song {i}', 'progress': fraction} for i, fraction in enumerate((0.25, 0.5, 0.75))]
        helper.set_downloads(reports)
        for i, (row, label, bar) in enumerate(helper.download_rows):
            self.assertIn(f'Song {i}', label.configure.call_args.kwargs['text'])
            self.assertEqual(bar.configure.call_args.kwargs['value'], reports[i]['progress'] * 100)
        helper.set_downloads(reports[:1])
        helper.download_rows[1][0].pack_forget.assert_called()
        helper.download_rows[2][0].pack_forget.assert_called()
        helper.set_downloads([])
        helper.download_frame.pack_forget.assert_called_once()

    def test_throttle_recovers_during_wait_without_restarting_download(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'policy.json'
            path.write_text(json.dumps({'rate': 16 * 1024}))
            throttle = LiveThrottle(path)
            now = [0]
            waits = []

            def pause(duration):
                now[0] += duration
                waits.append(duration)
                if now[0] >= 0.25:
                    path.write_text(json.dumps({'rate': 8 * 1024 ** 2}))

            downloader = SimpleNamespace()
            with patch('download_progress.monotonic', side_effect=lambda: now[0]), \
                    patch('download_progress.sleep', side_effect=pause):
                throttle.slow_down(downloader, 0, 0, 1024 ** 2)
            self.assertLess(now[0], 1)
            self.assertTrue(waits)
            self.assertLessEqual(max(waits), 0.1)


if __name__ == '__main__':
    unittest.main()
