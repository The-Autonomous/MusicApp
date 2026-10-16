"""Resource budgets and live slot limits, with no games or UI launched."""
from threading import Event, Thread
from collections import namedtuple
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from download_policy import (DownloadBudget, DownloadSlots, GIB, ResourceMonitor,
                             budget_for, is_game_process)


class BudgetTests(unittest.TestCase):
    def test_idle_machine_can_download_three_songs(self):
        self.assertEqual(budget_for(12 * GIB, 20, 10).workers, 3)

    def test_cpu_or_ram_used_by_any_app_reduces_activity(self):
        self.assertEqual(budget_for(5 * GIB, 40, 10).workers, 2)
        self.assertEqual(budget_for(12 * GIB, 40, 85).workers, 1)
        self.assertEqual(budget_for(1.5 * GIB, 80, 10).workers, 1)
        self.assertLess(budget_for(0.5 * GIB, 97, 10).bytes_per_second,
                        budget_for(1.5 * GIB, 80, 10).bytes_per_second)

    def test_games_reduce_work_even_with_cpu_and_ram_headroom(self):
        self.assertEqual(budget_for(12 * GIB, 20, 10, gaming=True).workers, 1)
        self.assertTrue(is_game_process("FarCry6.exe"))
        self.assertTrue(is_game_process("GTA5_Enhanced.exe"))
        self.assertTrue(is_game_process("NewGame.exe", r"E:\Steam\steamapps\common\NewGame\NewGame.exe"))
        self.assertFalse(is_game_process("steam.exe", r"E:\Steam\steam.exe"))

    def test_live_monitor_downshifts_immediately_and_recovers_without_restart(self):
        monitor = ResourceMonitor()
        with patch("download_policy.monotonic") as clock, patch("psutil.virtual_memory") as memory, \
                patch.object(monitor, 'cpu_load', return_value=10), patch("psutil.process_iter", return_value=[]):
            clock.return_value = 0
            memory.return_value = SimpleNamespace(available=12 * GIB, percent=20)
            self.assertEqual(monitor.snapshot().workers, 3)
            clock.return_value = 2
            memory.return_value = SimpleNamespace(available=1.5 * GIB, percent=88)
            self.assertEqual(monitor.snapshot().workers, 1)
            memory.return_value = SimpleNamespace(available=12 * GIB, percent=20)
            clock.return_value = 4
            self.assertEqual(monitor.snapshot().workers, 1)
            clock.return_value = 14
            self.assertEqual(monitor.snapshot().workers, 3)

    def test_cpu_samples_share_one_baseline_across_worker_threads(self):
        monitor = ResourceMonitor()
        times = namedtuple('CPU', 'user system idle')
        samples = [times(10, 10, 80), times(18, 18, 84)]
        psutil = SimpleNamespace(cpu_times=lambda: samples.pop(0), cpu_percent=lambda **kwargs: 0)
        self.assertEqual(monitor.cpu_load(psutil), 0)
        result = []
        thread = Thread(target=lambda: result.append(monitor.cpu_load(psutil)))
        thread.start()
        thread.join(1)
        self.assertEqual(result, [80])

    def test_failed_resource_probe_is_conservative(self):
        with patch("psutil.virtual_memory", side_effect=OSError("Unavailable")):
            self.assertEqual(ResourceMonitor().snapshot().workers, 1)

    def test_gate_blocks_new_admissions_when_limit_falls_below_running_count(self):
        current = [DownloadBudget(3, 1024 ** 2, 0, False, "Idle")]
        slots = DownloadSlots(lambda: current[0])
        cancel, shutdown = Event(), Event()
        for _ in range(3):
            self.assertTrue(slots.acquire(cancel, shutdown))
        current[0] = DownloadBudget(1, 1024, 0, True, "Low")
        admitted = Event()

        def attempt():
            if slots.acquire(cancel, shutdown):
                admitted.set()
                slots.release()

        thread = Thread(target=attempt)
        self.addCleanup(cancel.set)
        thread.start()
        try:
            slots.release()
            slots.release()
            self.assertFalse(admitted.wait(0.15))
            slots.release()
            self.assertTrue(admitted.wait(1))
        finally:
            cancel.set()
            thread.join(2)
        self.assertEqual(slots.count(), 0)


if __name__ == "__main__":
    unittest.main()
