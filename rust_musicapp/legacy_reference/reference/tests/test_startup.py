from threading import Event
import unittest
from unittest.mock import Mock, patch

import startup


class StartupTests(unittest.TestCase):
    def test_probe_only_reports_missing_modules(self):
        with patch("startup.importlib.util.find_spec", side_effect=[object(), None, object()]) as lookup:
            self.assertEqual(startup.missing_runtime_modules(("a", "b", "c")), ("b",))
        self.assertEqual([call.args[0] for call in lookup.call_args_list], ["a", "b", "c"])

    def test_failed_discovery_is_treated_as_missing(self):
        with patch("startup.importlib.util.find_spec", side_effect=ValueError("missing spec")):
            self.assertEqual(startup.missing_runtime_modules(("a",)), ("a",))

    def test_existing_runtime_never_starts_worker_or_creates_window(self):
        root = Mock()
        with patch("startup.missing_runtime_modules", return_value=()):
            self.assertTrue(startup.ensure_runtime(root))
        self.assertEqual(root.mock_calls, [])

    def test_installation_is_a_bounded_worker_job(self):
        cancel, runner, probe = Event(), Mock(), Mock(return_value=())
        with patch("startup.importlib.invalidate_caches") as invalidate:
            startup.prepare_runtime(cancel, runner=runner, probe=probe)
        runner.assert_called_once_with({"action": "prepare_app"}, cancel, timeout=270)
        invalidate.assert_called_once_with()
        probe.assert_called_once_with()

    def test_cancellation_before_installation_prevents_worker(self):
        cancel, runner, probe = Event(), Mock(), Mock()
        cancel.set()
        with self.assertRaisesRegex(RuntimeError, "stopped"):
            startup.prepare_runtime(cancel, runner=runner, probe=probe)
        runner.assert_not_called()
        probe.assert_not_called()

    def test_cancellation_after_worker_prevents_success(self):
        cancel, probe = Event(), Mock()
        runner = Mock(side_effect=lambda *args, **kwargs: cancel.set())
        with self.assertRaisesRegex(RuntimeError, "stopped"):
            startup.prepare_runtime(cancel, runner=runner, probe=probe)
        probe.assert_not_called()

    def test_missing_modules_after_installation_prevent_success(self):
        with self.assertRaisesRegex(RuntimeError, "still unavailable: sounddevice"):
            startup.prepare_runtime(Event(), runner=Mock(), probe=Mock(return_value=("sounddevice",)))

    def test_installer_error_identifies_core_packages_and_actual_failure(self):
        import youtube_worker
        failure = Mock(returncode=1, stderr="WARNING: retrying\nERROR: Access is denied\n")
        with patch("importlib.util.find_spec", return_value=None), \
                patch.object(youtube_worker.subprocess, "run", return_value=failure):
            with self.assertRaisesRegex(RuntimeError, "MusicApp essentials.*sounddevice.*Access is denied"):
                youtube_worker.prepare_support({"action": "prepare_app"})

    def test_worker_error_is_preserved(self):
        probe = Mock()
        with self.assertRaisesRegex(RuntimeError, "network error"):
            startup.prepare_runtime(Event(), runner=Mock(side_effect=RuntimeError("network error")), probe=probe)
        probe.assert_not_called()

    def test_error_card_bounds_long_messages(self):
        text = startup.preparation_error(RuntimeError("x " * 1000))
        self.assertLessEqual(len(text), 402)
        self.assertTrue(text.startswith("Couldn't finish preparing MusicApp."))


if __name__ == "__main__":
    unittest.main()
