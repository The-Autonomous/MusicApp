"""Small, cached resource checks and cancellable low-priority worker jobs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event, Lock
from time import monotonic

_lock = Lock()
_checked_at = 0.0
_gaming = False


class YouTubeJobError(RuntimeError):
    def __init__(self, message, code="operation_failed"):
        super().__init__(message)
        self.code = code


def game_running():
    global _checked_at, _gaming
    with _lock:
        if monotonic() - _checked_at < 15:
            return _gaming
        _checked_at = monotonic()
        try:
            import psutil
            names = {"gta5.exe", "gta5_enhanced.exe", "gta5 enhanced.exe", "playgtav.exe"}
            _gaming = any((p.info.get("name") or "").lower() in names
                          or ((p.info.get("name") or "").lower().startswith("gta5")
                              and (p.info.get("name") or "").lower().endswith(".exe"))
                          for p in psutil.process_iter(["name"]))
        except Exception:
            # A failed process query should choose conservative download limits.
            _gaming = True
        return _gaming


def stop_worker(process):
    """Only stop descendants of the worker we created, including its encoder."""
    try:
        import psutil
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        for child in reversed(children):
            try:
                child.terminate()
            except psutil.Error:
                pass
        parent.terminate()
        _, alive = psutil.wait_procs(children + [parent], timeout=1)
        for child in alive:
            try:
                child.kill()
            except psutil.Error:
                pass
    except Exception:
        if process.poll() is None:
            if os.name == "nt":
                try:
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=3, creationflags=subprocess.CREATE_NO_WINDOW)
                except (OSError, subprocess.TimeoutExpired):
                    process.kill()
            else:
                process.kill()


def run_job(job, cancel=None, timeout=300):
    cancel = cancel or Event()
    if cancel.is_set():
        raise RuntimeError("Cancelled")
    worker = Path(__file__).with_name("youtube_worker.py")
    executable = sys.executable
    if executable.lower().endswith("pythonw.exe"):
        executable = str(Path(executable).with_name("python.exe"))
    environment = os.environ.copy()
    # Both ends of the JSON pipe must use UTF-8 even on a Windows locale whose
    # default console encoding cannot represent a song title or search query.
    environment.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
                       OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1",
                       NUMEXPR_NUM_THREADS="1", BLIS_NUM_THREADS="1")
    job = dict(job, gaming=game_running())
    login_profile = None
    if job["action"] == "login":
        # Parent ownership also removes a cancelled sign-in's temporary files.
        login_profile = tempfile.TemporaryDirectory(prefix="MusicApp-signin-", ignore_cleanup_errors=True)
        job["profile_dir"] = login_profile.name
    flags = 0
    if os.name == "nt":
        priority = subprocess.IDLE_PRIORITY_CLASS if job['gaming'] and job['action'] != 'login' else subprocess.BELOW_NORMAL_PRIORITY_CLASS
        flags = subprocess.CREATE_NO_WINDOW | priority
    try:
        process = subprocess.Popen([executable, str(worker)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, encoding="utf-8", env=environment,
                                   creationflags=flags)
    except OSError:
        if login_profile:
            login_profile.cleanup()
        raise
    deadline = monotonic() + timeout
    payload = json.dumps(job, ensure_ascii=False)
    try:
        while True:
            if cancel.is_set() or monotonic() >= deadline:
                stop_worker(process)
                raise RuntimeError("Cancelled" if cancel.is_set() else "YouTube operation timed out")
            try:
                output, _ = process.communicate(payload, timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                payload = None
        try:
            result = json.loads(output)
        except (ValueError, TypeError):
            raise RuntimeError("YouTube worker failed; check that yt-dlp is installed") from None
        if not result.get("ok"):
            raise YouTubeJobError(result.get("error", "YouTube operation failed"),
                                  result.get("code", "operation_failed"))
        return result.get("result")
    finally:
        if process.poll() is None:
            stop_worker(process)
        for stream in (process.stdin, process.stdout):
            if stream:
                stream.close()
        if login_profile:
            login_profile.cleanup()
