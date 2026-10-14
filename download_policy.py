"""Live resource budgets shared by all YouTube download callers."""
from dataclasses import dataclass
import os
from threading import Lock
from time import monotonic


GIB = 1024 ** 3


@dataclass(frozen=True)
class DownloadBudget:
    workers: int
    bytes_per_second: int
    gap: float
    limited: bool
    label: str
    available_gib: float = 0


def budget_for(available, memory_percent, cpu_percent, gaming=False):
    """RAM headroom and total CPU load protect any application, not just games."""
    free = available / GIB
    if free < 1 or memory_percent >= 94 or cpu_percent >= 95:
        return DownloadBudget(1, 64 * 1024, 15, True, "Very low resource use", free)
    if free < 2 or memory_percent >= 88 or cpu_percent >= 80:
        return DownloadBudget(1, 256 * 1024, 8, True, "Low resource use", free)
    if gaming or free < 4 or cpu_percent >= 60:
        return DownloadBudget(1, 512 * 1024, 4, True, "Low resource use", free)
    if free < 6 or cpu_percent >= 35:
        return DownloadBudget(2, 2 * 1024 ** 2, 1, False, "Balanced downloads", free)
    return DownloadBudget(3, 8 * 1024 ** 2, 0.5, False, "Idle downloads", free)


def is_game_process(name, executable=""):
    name = (name or "").casefold()
    path = (executable or "").replace("\\", "/").casefold()
    # Ignore launchers: keeping Steam open should not keep downloads slow.
    if name in {"steam.exe", "steamwebhelper.exe", "epicgameslauncher.exe", "upc.exe",
                "ubisoftconnect.exe", "eadesktop.exe"}:
        return False
    return (name.startswith(("gta5", "farcry", "fc6"))
            or any(part in path for part in ("/steamapps/common/", "/epic games/",
                                            "/ubisoft game launcher/games/", "/ea games/", "/games/")))


class ResourceMonitor:
    def __init__(self):
        self.lock = Lock()
        self.checked_at = None
        self.games_checked_at = None
        self.gaming = False
        self.current = None
        self.recovering = None
        self.recovering_at = 0
        self.cpu_previous = None
        self.cpu_last_percent = 0

    def cpu_load(self, psutil):
        # psutil's nonblocking cpu_percent baselines belong to the calling
        # thread. Own one system-wide baseline because several workers poll us.
        times = psutil.cpu_times()._asdict()
        total = sum(value for key, value in times.items() if key not in {"guest", "guest_nice"})
        busy = total - times.get("idle", 0) - times.get("iowait", 0)
        previous = self.cpu_previous
        self.cpu_previous = (total, busy)
        if previous is None:
            self.cpu_last_percent = psutil.cpu_percent(interval=None)
        elif total > previous[0]:
            self.cpu_last_percent = max(0, min(100, 100 * (busy - previous[1]) / (total - previous[0])))
        return self.cpu_last_percent

    def snapshot(self):
        with self.lock:
            now = monotonic()
            if self.checked_at is not None and now - self.checked_at < 2:
                return self.current
            self.checked_at = now
            try:
                import psutil
                memory = psutil.virtual_memory()
                cpu = self.cpu_load(psutil)
                if self.games_checked_at is None or now - self.games_checked_at >= 10:
                    self.games_checked_at = now
                    self.gaming = any(is_game_process(p.info.get("name"), p.info.get("exe"))
                                      for p in psutil.process_iter(["name", "exe"])
                                      if p.pid != os.getpid())
                candidate = budget_for(memory.available, memory.percent, cpu, self.gaming)
            except Exception:
                candidate = DownloadBudget(1, 256 * 1024, 8, True, "Low resource use")
            old = self.current
            # Slow down immediately; require ten stable seconds before speeding up.
            if old is None or candidate.bytes_per_second <= old.bytes_per_second:
                self.current = candidate
                self.recovering = None
            else:
                signature = (candidate.workers, candidate.bytes_per_second)
                if signature != self.recovering:
                    self.recovering, self.recovering_at = signature, now
                if now - self.recovering_at >= 10:
                    self.current = candidate
                    self.recovering = None
            return self.current


_monitor = ResourceMonitor()


def download_budget():
    return _monitor.snapshot()


class DownloadSlots:
    """Lower admission immediately without interrupting downloads already running."""
    def __init__(self, budget=download_budget):
        self.budget = budget
        self.lock = Lock()
        self.active = 0
        self.next_start = 0

    def acquire(self, cancel, shutdown):
        while not cancel.is_set() and not shutdown.is_set():
            budget = self.budget()
            with self.lock:
                now = monotonic()
                if self.active < budget.workers and now >= self.next_start:
                    self.active += 1
                    self.next_start = now + budget.gap
                    return True
            cancel.wait(0.1)
        return False

    def release(self):
        with self.lock:
            self.active -= 1

    def count(self):
        with self.lock:
            return self.active


DOWNLOAD_SLOTS = DownloadSlots()
