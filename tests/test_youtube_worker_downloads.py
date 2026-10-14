"""Worker options and actual segment removal on short, silent fixture audio."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch
import wave

import youtube_worker


ROOT = Path(__file__).resolve().parents[1]
CATEGORIES = ['sponsor', 'intro', 'outro', 'selfpromo', 'preview', 'filler', 'music_offtopic']


class WorkerDownloadTests(unittest.TestCase):
    def test_download_applies_removal_before_mp3_conversion_and_reports_each_stage(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'fixture.mp3'
            path.write_bytes(b'fixture audio')
            progress = Path(folder) / 'progress.json'
            captured = {}

            class Downloader:
                def __init__(self, options):
                    captured.update(options)
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    pass
                def extract_info(self, url, download):
                    captured['progress_hooks'][0]({'status': 'downloading', 'downloaded_bytes': 50, 'total_bytes': 100})
                    captured['postprocessor_hooks'][0]({'postprocessor': 'ModifyChapters', 'status': 'started'})
                    return {'title': 'Fixture', 'artist': 'Artist'}
                def prepare_filename(self, info):
                    return str(path)

            module = ModuleType('yt_dlp')
            module.YoutubeDL = Downloader
            with patch.dict(sys.modules, {'yt_dlp': module}):
                result = youtube_worker.execute({'action': 'download', 'directory': folder, 'url': 'fixture',
                                                  'sponsorblock': CATEGORIES, 'mp3': True, 'progress_file': str(progress)})
            processors = captured['postprocessors']
            self.assertEqual([item['key'] for item in processors], ['SponsorBlock', 'ModifyChapters', 'FFmpegExtractAudio'])
            self.assertEqual(processors[1]['remove_sponsor_segments'], CATEGORIES)
            self.assertEqual(captured['concurrent_fragment_downloads'], 1)
            self.assertEqual(result['path'], str(path))
            self.assertEqual(json.loads(progress.read_text())['phase'], 'finished')

    def test_real_non_music_intro_filler_and_outro_segments_are_removed(self):
        from yt_dlp import YoutubeDL
        from yt_dlp.postprocessor.modify_chapters import ModifyChaptersPP
        binary = shutil.which('ffmpeg') or next((ROOT / 'ffmpeg-bin').glob('**/ffmpeg.exe'), None)
        if not binary:
            self.skipTest('FFmpeg is not installed')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'silent.wav'
            with wave.open(str(path), 'wb') as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16000)
                audio.writeframes(b'\0\0' * 16000 * 9)
            options = {'quiet': True, 'no_warnings': True, 'logger': youtube_worker.QuietLogger(),
                       'ffmpeg_location': str(Path(binary).parent),
                       'postprocessor_args': {'ffmpeg': ['-threads', '1', '-filter_threads', '1']}}
            with YoutubeDL(options) as downloader:
                processor = ModifyChaptersPP(downloader, remove_sponsor_segments=CATEGORIES)
                _, info = processor.run({'filepath': str(path), 'title': 'Silent fixture', 'duration': 9,
                                         'ext': 'wav', 'vcodec': 'none', 'acodec': 'pcm_s16le',
                                         'sponsorblock_chapters': [
                                             {'start_time': 0, 'end_time': 2, 'category': 'intro'},
                                             {'start_time': 4, 'end_time': 5, 'category': 'filler'},
                                             {'start_time': 7, 'end_time': 9, 'category': 'music_offtopic'}]})
            self.assertEqual(info['duration'], 4)
            with wave.open(str(path), 'rb') as audio:
                self.assertAlmostEqual(audio.getnframes() / audio.getframerate(), 4, delta=0.15)


if __name__ == '__main__':
    unittest.main()
