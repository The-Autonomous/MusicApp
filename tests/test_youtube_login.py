"""Small isolated tests: no browser launch, account access, or dependency installs."""
import ctypes
from ctypes import wintypes
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import time
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import youtube_login as login
FIXTURE_ROOT = str(Path(__file__).resolve().parent)


@contextmanager
def fixture_directory(prefix="MusicApp-test-"):
    # Inherit the workspace ACL. Python's 0700 TemporaryDirectory ACL excludes the
    # restricted token used by the Windows Codex sandbox, even for its own files.
    root = Path(FIXTURE_ROOT).resolve()
    directory = root / (prefix + uuid.uuid4().hex)
    directory.mkdir()
    try:
        yield str(directory)
    finally:
        checked = directory.resolve()
        if checked.parent != root or not checked.name.startswith(("MusicApp-test-", "MusicApp-signin-")):
            raise RuntimeError("Refusing to clean a fixture outside its test directory")
        if checked.exists():
            shutil.rmtree(checked)


class LoginHelpers(unittest.TestCase):
    def test_owned_signin_window_requests_normal_maximization(self):
        calls = []
        session = SimpleNamespace(send=lambda method, *args: (
            calls.append((method, args)) or {"windowId": 47}), detach=lambda: calls.append(("detach", ())))
        page = SimpleNamespace(is_closed=lambda: False)
        context = SimpleNamespace(pages=[page], new_cdp_session=lambda target: session)
        login._maximize_owned_browser(SimpleNamespace(contexts=[context]))
        self.assertEqual(calls[0][0], "Browser.getWindowForTarget")
        self.assertEqual(calls[1], ("Browser.setWindowBounds", ({"windowId":47,"bounds":{"windowState":"maximized"}},)))
        self.assertEqual(calls[-1][0], "detach")

    def test_preferred_browser_preserves_registered_chrome_or_edge(self):
        choice = {"name":"Microsoft Edge","executable":"fixture.exe","cookie_browser":"edge","chromium":True}
        with patch.object(login, "default_browser", return_value=choice):
            self.assertIs(login._preferred_signin_browser(), choice)

    def test_browser_command_parsing(self):
        with fixture_directory() as directory:
            executable = Path(directory) / "Browser with spaces.exe"
            executable.touch()
            expected = str(executable)
            self.assertEqual(login._command_executable(f'"{expected}" --url "%1"'), expected)
            self.assertEqual(login._command_executable(expected + " --single-argument %1"), expected)
            with patch.dict(os.environ, {"MUSICAPP_TEST_BROWSER": expected}):
                self.assertEqual(login._command_executable('"%MUSICAPP_TEST_BROWSER%" "%1"'), expected)
            for command in ("relative.exe %1", "cmd /c browser", '"missing.exe" %1', ""):
                with self.subTest(command=command), self.assertRaises(login.LoginError) as failure:
                    login._command_executable(command)
                self.assertEqual(failure.exception.code, "default_browser")

    @unittest.skipUnless(os.name == "nt", "Windows registry association")
    def test_registered_browser_selection_without_reading_registry(self):
        with fixture_directory() as directory:
            executable = Path(directory) / "chrome.exe"
            executable.touch()
            class RegistryKey:
                def __init__(self, path):
                    self.path = path
                def __enter__(self):
                    return self
                def __exit__(self, *arguments):
                    pass
            registry = SimpleNamespace(HKEY_CURRENT_USER=1, HKEY_CLASSES_ROOT=2)
            registry.OpenKey = lambda root, path: RegistryKey(path)
            registry.QueryValueEx = lambda key, value: (
                "ChromeHTML" if value == "ProgId" else f'"{executable}" "%1"', 1)
            with patch.dict(sys.modules, {"winreg": registry}):
                result = login.default_browser()
            self.assertEqual(result, {"name": "Google Chrome", "executable": str(executable),
                                      "cookie_browser": "chrome", "chromium": True})

    def test_cookie_domain_boundaries(self):
        for domain in ("youtube.com", ".youtube.com", "music.youtube.com", ".Music.YouTube.com"):
            self.assertTrue(login._youtube_domain(domain))
        for domain in ("", "google.com", "youtube.com.evil.example", "notyoutube.com"):
            self.assertFalse(login._youtube_domain(domain))
        self.assertFalse(login._signed_cookies([{"name": "SID", "value": "example"}]))
        self.assertFalse(login._signed_cookies([{"name": "SAPISID", "value": ""}]))
        self.assertTrue(login._signed_cookies([{"name": "__Secure-3PAPISID", "value": "synthetic"}]))

    def test_cdp_process_identity_and_profile_verification(self):
        class Connection:
            detached = False
            def send(self, method):
                return {"processInfo": [{"type": "browser", "id": 2345}]}
            def detach(self):
                self.detached = True
        connection = Connection()
        browser = SimpleNamespace(new_browser_cdp_session=lambda: connection)
        candidate = SimpleNamespace(create_time=lambda: 1, cmdline=lambda: ["chrome.exe", "--user-data-dir=other"])
        fake_psutil = SimpleNamespace(Process=lambda pid: candidate)
        with patch.dict(sys.modules, {"psutil": fake_psutil}):
            self.assertIs(login._owns_browser(browser, SimpleNamespace(pid=2345), Path("ours")), candidate)
            self.assertIsNone(login._owns_browser(browser, SimpleNamespace(pid=999), Path("ours")))
            candidate.cmdline = lambda: ["chrome.exe", "--user-data-dir=" + str(Path("ours"))]
            self.assertIs(login._owns_browser(browser, SimpleNamespace(pid=999), Path("ours")), candidate)
        self.assertTrue(connection.detached)

    def fake_signin(self, *, signed_page=True, avatar=True, page_url=login.LIKES_URL):
        class Clock:
            value = 0
            def now(self):
                return self.value
            def sleep(self, duration):
                self.value += duration
        class Manager:
            def __init__(self, value):
                self.value = value
            def __enter__(self):
                return self.value
            def __exit__(self, *arguments):
                pass
        class FakeSocket:
            def __enter__(self):
                return self
            def __exit__(self, *arguments):
                pass
            def bind(self, address):
                pass
            def getsockname(self):
                return ("127.0.0.1", 32123)
        page = SimpleNamespace(url=page_url, is_closed=lambda: False,
                               evaluate=lambda expression: signed_page,
                               locator=lambda selector: SimpleNamespace(
                                   first=SimpleNamespace(is_visible=lambda: avatar)))
        page.goto = lambda url, **keywords: setattr(page, "url", url)
        cookies = [{"name": "__Secure-3PAPISID", "value": "synthetic", "domain": ".youtube.com"},
                   {"name": "unrelated", "value": "synthetic", "domain": ".unrelated.example"}]
        context = SimpleNamespace(pages=[page], cookies=lambda urls: cookies)
        browser = SimpleNamespace(contexts=[context], is_connected=lambda: True)
        playwright = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda *args, **kwargs: browser))
        response = SimpleNamespace(read=lambda limit: json.dumps({
            "webSocketDebuggerUrl": "ws://127.0.0.1:32123/devtools/browser/synthetic"}).encode())
        clock = Clock()
        with fixture_directory("MusicApp-signin-") as profile:
            with (patch.dict(sys.modules, {"playwright": SimpleNamespace(),
                                          "playwright.sync_api": SimpleNamespace(
                                              sync_playwright=lambda: Manager(playwright))}),
                  patch.object(login.tempfile, "gettempdir", return_value=FIXTURE_ROOT),
                  patch.object(login.socket, "socket", return_value=FakeSocket()),
                  patch.object(login, "urlopen", return_value=Manager(response)),
                  patch.object(login, "monotonic", side_effect=clock.now),
                  patch.object(login, "sleep", side_effect=clock.sleep),
                  patch.object(login, "_OwnedWindowsBrowser", return_value=SimpleNamespace(pid=123)),
                  patch.object(login, "_owns_browser", return_value=SimpleNamespace()),
                  patch.object(login, "_close_owned_browser") as close):
                try:
                    return login._chromium_login({"name": "Synthetic", "executable": "synthetic.exe"},
                                                  3, profile)
                finally:
                    close.assert_called_once()

    def test_signin_requires_page_account_marker_and_returns_only_youtube(self):
        cookies = self.fake_signin()
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0]["domain"], ".youtube.com")
        for values in ({"signed_page": False}, {"avatar": False}):
            with self.subTest(values=values), self.assertRaises(login.LoginError) as failure:
                self.fake_signin(**values)
            self.assertEqual(failure.exception.code, "signin_timeout")

    def test_google_rejection_returns_plain_failure(self):
        with self.assertRaises(login.LoginError) as failure:
            self.fake_signin(page_url="https://accounts.google.com/signin/v2/deniedsigninrejected")
        self.assertEqual(failure.exception.code, "signin_failed")
        self.assertIn("Google declined", str(failure.exception))

    def test_missing_playwright_is_reported_before_browser_launch(self):
        with patch.dict(sys.modules, {"playwright.sync_api": None}), patch.object(
                login, "_OwnedWindowsBrowser") as launch:
            with self.assertRaises(login.LoginError) as failure:
                login._chromium_login({"name": "Synthetic", "executable": "synthetic.exe"}, 3)
            self.assertEqual(failure.exception.code, "missing_dependency")
            launch.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Native Windows process/job APIs")
class NativeBrowserJob(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, parameters, result in (
            ("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            ("GetPriorityClass", [wintypes.HANDLE], wintypes.DWORD),
            ("WaitForSingleObject", [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
            ("GetCurrentProcess", [], wintypes.HANDLE),
            ("GetProcessHandleCount", [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        ):
            function = getattr(cls.api, name)
            function.argtypes, function.restype = parameters, result

    def process_handle(self, pid):
        handle = self.api.OpenProcess(0x100000 | 0x1000, False, pid)
        self.assertTrue(handle, f"Could not open our test child PID {pid}")
        self.addCleanup(self.api.CloseHandle, handle)
        return handle

    def wait_record(self, path):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                time.sleep(0.025)
        self.fail("Our harmless test child did not write its process record")

    def sleeper_arguments(self):
        return [sys.executable, "-I", "-B", "-c", "import time; time.sleep(30)"]

    def test_real_child_start_priority_and_close(self):
        child = login._OwnedWindowsBrowser(self.sleeper_arguments())
        self.addCleanup(child.close)
        handle = self.process_handle(child.pid)
        self.assertIsNone(child.poll())
        self.assertEqual(self.api.GetPriorityClass(child._process), 0x4000)
        child.close()
        self.assertEqual(self.api.WaitForSingleObject(handle, 3000), 0)
        self.assertIsNotNone(child.poll())
        child.close()  # Idempotent; no process selected by a reused PID.

    def test_job_closes_detached_descendant_after_launcher_exits(self):
        with fixture_directory() as directory:
            record = Path(directory) / "child.json"
            code = ("import subprocess,sys,json,pathlib; "
                    "child=subprocess.Popen([sys.executable,'-I','-B','-c','import time; time.sleep(30)'],"
                    "creationflags=subprocess.CREATE_NO_WINDOW); "
                    f"pathlib.Path({str(record)!r}).write_text(json.dumps({{'pid':child.pid}}))")
            launcher = login._OwnedWindowsBrowser([sys.executable, "-I", "-B", "-c", code])
            self.addCleanup(launcher.close)
            descendant = self.process_handle(self.wait_record(record)["pid"])
            deadline = time.monotonic() + 3
            while launcher.poll() is None and time.monotonic() < deadline:
                time.sleep(0.025)
            self.assertIsNotNone(launcher.poll())
            self.assertEqual(self.api.WaitForSingleObject(descendant, 0), 258)
            launcher.close()
            self.assertEqual(self.api.WaitForSingleObject(descendant, 3000), 0)

    def test_worker_termination_closes_owned_child_job(self):
        with fixture_directory() as directory:
            record = Path(directory) / "worker.json"
            module_directory = str(Path(login.__file__).parent)
            code = ("import sys,json,pathlib,time; "
                    f"sys.path.insert(0,{module_directory!r}); "
                    "from youtube_login import _OwnedWindowsBrowser; "
                    "child=_OwnedWindowsBrowser([sys.executable,'-I','-B','-c','import time; time.sleep(30)']); "
                    f"pathlib.Path({str(record)!r}).write_text(json.dumps({{'pid':child.pid}})); "
                    "time.sleep(30)")
            worker = subprocess.Popen([sys.executable, "-I", "-B", "-c", code],
                                      creationflags=subprocess.CREATE_NO_WINDOW,
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
            try:
                child = self.process_handle(self.wait_record(record)["pid"])
                worker.terminate()  # This is the exact worker created above, never a PID search.
                worker.wait(timeout=3)
                self.assertEqual(self.api.WaitForSingleObject(child, 3000), 0)
            finally:
                if worker.poll() is None:
                    worker.kill()
                worker.wait(timeout=3)

    def test_failed_job_assignment_never_resumes_child(self):
        actual_api = self.api
        recorded = {}
        class NativeFunction:
            def __init__(self, name):
                object.__setattr__(self, "name", name)
            def __setattr__(self, name, value):
                setattr(getattr(actual_api, self.name), name, value)
            def __call__(self, *arguments):
                if self.name == "AssignProcessToJobObject":
                    return False
                if self.name == "ResumeThread":
                    recorded["resumed"] = True
                result = getattr(actual_api, self.name)(*arguments)
                if self.name == "CreateProcessW" and result:
                    recorded["pid"] = arguments[-1]._obj.dwProcessId
                    recorded["handle"] = self_test.process_handle(recorded["pid"])
                return result
        class APIProxy:
            def __getattr__(self, name):
                return NativeFunction(name)
        self_test = self
        with patch.object(ctypes, "WinDLL", return_value=APIProxy()):
            with self.assertRaises(login.LoginError) as failure:
                login._OwnedWindowsBrowser(self.sleeper_arguments())
        self.assertEqual(failure.exception.code, "browser_access")
        self.assertNotIn("resumed", recorded)
        self.assertEqual(self.api.WaitForSingleObject(recorded["handle"], 3000), 0)

    def test_invalid_executable_failure_does_not_leak_handles(self):
        count = wintypes.DWORD()
        current = self.api.GetCurrentProcess()
        self.api.GetProcessHandleCount(current, ctypes.byref(count))
        before = count.value
        with self.assertRaises(login.LoginError):
            login._OwnedWindowsBrowser([str(Path(FIXTURE_ROOT) / "MusicApp-missing.exe")])
        self.api.GetProcessHandleCount(current, ctypes.byref(count))
        self.assertEqual(count.value, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
