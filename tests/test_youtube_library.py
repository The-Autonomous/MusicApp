"""Song identity checks without network, accounts or audio playback."""
import unittest
from unittest.mock import patch

from youtube_library import audio_file_exists, eligible_tracks, song_keys


class SongIdentityTests(unittest.TestCase):
    def test_tags_ignore_case_spacing_promotion_and_topic_suffix(self):
        local = {"title": "Artist — Don't Stop (Official Music Video) [Lyrics]",
                 "artist": "ARTIST - Topic"}
        liked = {"title": "dont stop", "artist": "Artist"}
        self.assertTrue(song_keys(local) & song_keys(liked))

    def test_meaningful_versions_remain_distinct(self):
        original = song_keys({"title": "Song", "artist": "Artist"})
        for version in ("Live", "Remix", "Instrumental", "Acoustic", "Sped Up", "Remastered"):
            with self.subTest(version=version):
                self.assertFalse(original & song_keys({"title": f"Song ({version})", "artist": "Artist"}))

    def test_different_performers_are_not_inferred_from_composer_in_title(self):
        title = "Debussy - Clair de Lune"
        self.assertFalse(song_keys({"title": title, "artist": "Rousseau"})
                         & song_keys({"title": title, "artist": "Another Pianist"}))

    def test_missing_artist_can_use_explicit_artist_in_title(self):
        self.assertTrue(song_keys({"title": "Artist - Song", "artist": "Unknown Artist"})
                        & song_keys({"title": "Song", "artist": "Artist"}))

    def test_generic_metadata_does_not_match_by_title_alone(self):
        for artist in (None, "YouTube", "Unknown Artist", ""):
            with self.subTest(artist=artist):
                self.assertEqual(song_keys({"title": "Song", "artist": artist}), set())

    def test_unicode_and_punctuation_without_erasing_word_boundaries(self):
        self.assertTrue(song_keys({"title": "Ｄéjà Vu!", "artist": "Björk"})
                        & song_keys({"title": "déjà vu", "artist": "BJÖRK"}))
        self.assertFalse(song_keys({"title": "Now Here", "artist": "Artist"})
                         & song_keys({"title": "Nowhere", "artist": "Artist"}))

    def test_flat_video_ids_and_valid_urls_are_accepted_but_mismatches_are_skipped(self):
        entries = [{"id": "synthetic-0", "url": "synthetic-0"},
                   {"id": "synthetic-0", "title": "duplicate"},
                   {"id": "synthetic-1", "url": "https://music.youtube.com/watch?v=synthetic-1"},
                   {"id": "synthetic-2", "url": "https://youtu.be/synthetic-3"}]
        tracks, skipped = eligible_tracks(entries)
        self.assertEqual([track["id"] for track in tracks], ["synthetic-0", "synthetic-1"])
        self.assertEqual(skipped, 1)

    def test_cloud_placeholder_is_not_treated_as_local_audio(self):
        with patch("youtube_library.Path.stat", return_value=type("Stat", (), {
                "st_mode": 0o100644, "st_size": 100, "st_file_attributes": 0x400000})()):
            self.assertFalse(audio_file_exists("song.mp3"))


if __name__ == "__main__":
    unittest.main()
