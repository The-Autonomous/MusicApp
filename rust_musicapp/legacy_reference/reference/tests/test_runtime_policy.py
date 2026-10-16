"""Real subprocess protocol tests using harmless, owned fixture workers only."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch
import uuid

import psutil
from download_policy import DownloadBudget


ROOT = Path(__file__).resolve().parents[1]
WORKER = r'''
import json, os, pathlib, subprocess, sys, time
job = json.load(sys.stdin)
action = job["action"]
if action == "echo":
    result = {
        "value": job["value"], "gaming": job["gaming"],
        "stdin_encoding": sys.stdin.encoding, "stdout_encoding": sys.stdout.encoding,
        "environment": {name: os.environ.get(name) for name in (
            "PYTHONIOENCODING", "PYTHONUTF8", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")},
    }
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
elif action == "error":
    result = {"ok": False, "error": job["message"]}
    if "code" in job:
        result["code"] = job["code"]
    print(json.dumps(result, ensure_ascii=False))
elif action == "malformed":
    print("fixture output is not JSON")
elif action == "download":
    progress = pathlib.Path(job["progress_file"])
    policy = pathlib.Path(job["policy_file"])
    first = json.loads(policy.read_text(encoding="utf-8"))["rate"]
    progress.write_text(json.dumps({"phase": "downloading", "progress": 0.5,
                                   "downloaded_bytes": 50, "total_bytes": 100}), encoding="utf-8")
    time.sleep(2.6)
    final = json.loads(policy.read_text(encoding="utf-8"))["rate"]
    print(json.dumps({"ok": True, "result": {"first_rate": first, "final_rate": final,
                                            "channel": str(progress.parent)}}))
elif action == "block":
    flags = subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 0
    child = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    pathlib.Path(job["record"]).write_text(json.dumps({"worker": os.getpid(), "child": child.pid}), encoding="utf-8")
    time.sleep(60)
else:
    raise RuntimeError("Unknown fixture action")
'''


class NativeWorkerProtocol(unittest.TestCase):
    def setUp(self):
        self.fixture = (ROOT / "tests" / ("MusicApp-policy-test-" + uuid.uuid4().hex)).resolve()
        self.fixture.mkdir()  # Workspace ACL inheritance supports the restricted test token.
        self.addCleanup(self.clean_fixture)
        (self.fixture / "youtube_worker.py").write_text(WORKER, encoding="utf-8")
        specification = importlib.util.spec_from_file_location("_musicapp_policy_fixture", ROOT / "runtime_policy.py")
        self.module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(self.module)
        self.module.__file__ = str(self.fixture / "runtime_policy.py")
        detection = patch.object(self.module, "game_running", return_value=False)
        detection.start()
        self.addCleanup(detection.stop)
        self.processes = []
        actual_popen = subprocess.Popen
        def capture(*arguments, **keywords):
            process = actual_popen(*arguments, **keywords)
            self.processes.append(process)
            return process
        launching = patch.object(self.module.subprocess, "Popen", side_effect=capture)
        launching.start()
        self.addCleanup(launching.stop)
        self.addCleanup(self.stop_owned_processes)

    def clean_fixture(self):
        checked = self.fixture.resolve()
        if checked.parent != (ROOT / "tests").resolve() or not checked.name.startswith("MusicApp-policy-test-"):
            raise RuntimeError("Refusing cleanup outside the fixture directory")
        shutil.rmtree(checked)

    def stop_owned_processes(self):
        for process in self.processes:
            if process.poll() is None:
                self.module.stop_worker(process)
            process.wait(timeout=3)

    def wait_record(self, path):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                time.sleep(0.025)
        self.fail("Our fixture worker did not report its owned descendant")

    def test_unicode_json_round_trip_and_worker_environment(self):
        original = {"action": "echo", "value": "Björk — déjà vu 🎵 中文 العربية"}
        result = self.module.run_job(original, timeout=3)
        self.assertEqual(result["value"], original["value"])
        self.assertNotIn("gaming", original)
        self.assertFalse(result["gaming"])
        for stream in ("stdin_encoding", "stdout_encoding"):
            self.assertEqual(result[stream].lower().replace("-", ""), "utf8")
        self.assertEqual(result["environment"], {
            "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"})
        self.assertEqual(len(self.processes), 1)
        self.assertIsNotNone(self.processes[0].poll())
        self.assertTrue(self.processes[0].stdin.closed)
        self.assertTrue(self.processes[0].stdout.closed)

    def test_coded_unicode_worker_failure_propagates(self):
        message = "登录失败 — synthetic failure 🎵"
        with self.assertRaises(self.module.YouTubeJobError) as failure:
            self.module.run_job({"action": "error", "message": message, "code": "signin_failed"}, timeout=3)
        self.assertEqual(str(failure.exception), message)
        self.assertEqual(failure.exception.code, "signin_failed")

    def test_failure_without_code_uses_default(self):
        with self.assertRaises(self.module.YouTubeJobError) as failure:
            self.module.run_job({"action": "error", "message": "Synthetic error"}, timeout=3)
        self.assertEqual(failure.exception.code, "operation_failed")

    def test_invalid_worker_json_returns_clear_error(self):
        with self.assertRaisesRegex(RuntimeError, "worker failed"):
            self.module.run_job({"action": "malformed"}, timeout=3)

    def test_precancelled_job_starts_no_process(self):
        cancelled = Event()
        cancelled.set()
        with self.assertRaisesRegex(RuntimeError, "Cancelled"):
            self.module.run_job({"action": "echo", "value": "unused"}, cancelled, timeout=3)
        self.assertEqual(self.processes, [])

    def test_busy_policy_file_keeps_previous_budget_without_failing_job(self):
        path = self.fixture / 'policy.json'
        path.write_text('{"rate":65536}')
        with patch.object(self.module.os, 'replace', side_effect=PermissionError('Reader has file open')):
            self.module._write_channel(path, {'rate': 1024})
        self.assertEqual(json.loads(path.read_text())['rate'], 65536)

    def test_progress_channel_updates_policy_in_same_worker_and_is_cleaned(self):
        reports = []
        budgets = [DownloadBudget(3, 8 * 1024 ** 2, 0, False, 'Idle')]

        def progress(value):
            reports.append(value)
            budgets[0] = DownloadBudget(1, 64 * 1024, 0, True, 'Low')

        with patch.object(self.module, 'download_budget', side_effect=lambda: budgets[0]):
            result = self.module.run_job({'action': 'download'}, timeout=5, progress=progress)
        self.assertEqual(reports[0]['progress'], 0.5)
        self.assertEqual(result['first_rate'], 8 * 1024 ** 2)
        self.assertEqual(result['final_rate'], 64 * 1024)
        self.assertFalse(Path(result['channel']).exists())
        self.assertEqual(len(self.processes), 1)

    def test_timeout_terminates_worker_and_owned_descendant(self):
        record = self.fixture / "timeout.json"
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            self.module.run_job({"action": "block", "record": str(record)}, timeout=1)
        identities = self.wait_record(record)
        self.assertIsNotNone(self.processes[0].poll())
        # A Windows virtualenv's python.exe can redirect to a separate worker PID.
        self.assertFalse(psutil.pid_exists(identities["worker"]))
        self.assertFalse(psutil.pid_exists(identities["child"]))
        self.assertTrue(self.processes[0].stdin.closed)
        self.assertTrue(self.processes[0].stdout.closed)

    def test_cancel_terminates_exact_worker_and_owned_descendant(self):
        record = self.fixture / "cancel.json"
        cancellation = Event()
        errors = []
        finished = Event()
        def run():
            try:
                self.module.run_job({"action": "block", "record": str(record)}, cancellation, timeout=5)
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()
        thread = Thread(target=run, daemon=True, name="policy-test")
        self.addCleanup(cancellation.set)
        thread.start()
        try:
            identities = self.wait_record(record)
            parent = psutil.Process(self.processes[0].pid)
            owned = [parent, *parent.children(recursive=True)]
            self.assertIn(identities["worker"], {process.pid for process in owned})
            self.assertIn(identities["child"], {process.pid for process in owned})
            for process in owned:
                process.create_time()
            child = psutil.Process(identities["child"])
            child.create_time()
            self.assertTrue(child.is_running())
            cancellation.set()
            self.assertTrue(finished.wait(3))
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertEqual(str(errors[0]), "Cancelled")
            self.assertIsNotNone(self.processes[0].poll())
            self.assertFalse(child.is_running())
            self.assertTrue(all(not process.is_running() for process in owned))
        finally:
            cancellation.set()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
