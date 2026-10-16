"""Manual YouTube sign-in in the registered Windows browser, on demand only."""
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
from time import monotonic, sleep
from urllib.parse import urlsplit
from urllib.request import urlopen


LIKES_URL = "https://www.youtube.com/playlist?list=LL"
SIGN_IN_URL = ("https://accounts.google.com/ServiceLogin?service=youtube&continue="
               "https%3A%2F%2Fwww.youtube.com%2Fplaylist%3Flist%3DLL")
AUTH_NAMES = {"SAPISID", "__Secure-3PAPISID", "__Secure-1PAPISID"}


class LoginError(RuntimeError):
    def __init__(self, message, code="signin_failed"):
        super().__init__(message)
        self.code = code


def _command_executable(command):
    # Read just the executable, never execute a registry command or its arguments.
    expanded = os.path.expandvars(command).strip()
    match = re.match(r'^"([^"\r\n]+\.exe)"(?:\s|$)', expanded, re.I)
    if match is None:
        match = re.match(r'^([^"\r\n]+?\.exe)(?:\s|$)', expanded, re.I)
    if match is None:
        raise LoginError("Windows did not provide a usable default browser.", "default_browser")
    executable = Path(match.group(1))
    if not executable.is_absolute() or not executable.is_file():
        raise LoginError("Your default browser is unavailable. Check Windows Default apps.",
                         "default_browser")
    return str(executable)


def default_browser():
    """Read the actual HTTPS association; never search or read browser profiles."""
    if os.name != "nt":
        raise LoginError("Automatic browser selection requires Windows.", "default_browser")
    import winreg
    try:
        try:
            association = (r"Software\Microsoft\Windows\Shell\Associations"
                           r"\UrlAssociations\https\UserChoice")
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, association) as key:
                progid = winreg.QueryValueEx(key, "ProgId")[0]
        except FileNotFoundError:
            progid = "https"
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid + r"\shell\open\command") as key:
            command = winreg.QueryValueEx(key, None)[0]
        executable = _command_executable(command)
    except LoginError:
        raise
    except OSError:
        raise LoginError("Cannot read your default browser. Check Windows Default apps.",
                         "default_browser") from None
    identity = (Path(executable).name + " " + progid).casefold()
    browsers = (
        ("firefox", "Firefox", "firefox", False),
        ("msedge", "Microsoft Edge", "edge", True),
        ("brave", "Brave", "brave", True),
        ("opera", "Opera", "opera", True),
        ("vivaldi", "Vivaldi", "vivaldi", True),
        ("whale", "Whale", "whale", True),
        ("chromium", "Chromium", "chromium", True),
        ("chrome", "Google Chrome", "chrome", True),
    )
    for token, name, cookie_browser, chromium in browsers:
        if token in identity:
            return {"name": name, "executable": executable,
                    "cookie_browser": cookie_browser, "chromium": chromium}
    return {"name": Path(executable).stem, "executable": executable,
            "cookie_browser": None, "chromium": False}


def _youtube_domain(domain):
    domain = domain.lstrip(".").casefold()
    return domain == "youtube.com" or domain.endswith(".youtube.com")


def _signed_cookies(cookies):
    return any(cookie.get("name") in AUTH_NAMES and cookie.get("value") for cookie in cookies)


def _close_owned_browser(process, browser, owned):
    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass
    if owned is not None:
        import psutil
        try:
            if owned.is_running():
                children = owned.children(recursive=True)
                for child in reversed(children):
                    try:
                        child.terminate()
                    except psutil.Error:
                        pass
                owned.terminate()
                _, alive = psutil.wait_procs(children + [owned], timeout=1)
                for child in alive:
                    try:
                        child.kill()
                    except psutil.Error:
                        pass
        except psutil.Error:
            pass
    if process is not None and process.poll() is None:
        from runtime_policy import stop_worker
        stop_worker(process)
    if process is not None and hasattr(process, "close"):
        process.close()


def _owns_browser(browser, process, profile):
    """Confirm the CDP endpoint belongs to the profile/process we just launched."""
    import psutil
    connection = browser.new_browser_cdp_session()
    try:
        processes = connection.send("SystemInfo.getProcessInfo")["processInfo"]
        browser_pid = next(int(item["id"]) for item in processes if item["type"] == "browser")
        owned = psutil.Process(browser_pid)
        owned.create_time()
        if browser_pid == process.pid:
            return owned
        command = owned.cmdline()
        expected = os.path.normcase(str(profile))
        if any(argument.startswith("--user-data-dir=") and
               os.path.normcase(argument.split("=", 1)[1].strip('"')) == expected
               for argument in command):
            return owned
        return None
    finally:
        connection.detach()


class _OwnedWindowsBrowser:
    """A suspended browser assigned to a kill-on-close job before it can spawn."""
    def __init__(self, arguments):
        import ctypes
        from ctypes import wintypes

        class StartupInfo(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
                        ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
                        ("hStdError", wintypes.HANDLE)]

        class ProcessInfo(ctypes.Structure):
            _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                        ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

        class StartupInfoEx(ctypes.Structure):
            _fields_ = [("StartupInfo", StartupInfo), ("lpAttributeList", ctypes.c_void_p)]

        class SecurityAttributes(ctypes.Structure):
            _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                        ("bInheritHandle", wintypes.BOOL)]

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IOCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IOCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                          wintypes.DWORD], wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "CreateFileW": ([wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(SecurityAttributes), wintypes.DWORD,
                              wintypes.DWORD, wintypes.HANDLE], wintypes.HANDLE),
            "InitializeProcThreadAttributeList": ([ctypes.c_void_p, wintypes.DWORD,
                                                    wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)],
                                                   wintypes.BOOL),
            "UpdateProcThreadAttribute": ([ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t,
                                            ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                                            ctypes.c_void_p], wintypes.BOOL),
            "DeleteProcThreadAttributeList": ([ctypes.c_void_p], None),
            "CreateProcessW": ([wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
                                 ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
                                 ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(StartupInfo),
                                 ctypes.POINTER(ProcessInfo)], wintypes.BOOL),
            "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
            "GetExitCodeProcess": ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (parameters, result) in signatures.items():
            function = getattr(api, name)
            function.argtypes, function.restype = parameters, result
        self._api, self._ctypes, self._wintypes = api, ctypes, wintypes
        self._process = self._job = None
        self.returncode = None
        info = ProcessInfo()
        null_handle = None
        attributes = None
        attributes_initialized = False
        try:
            self._job = api.CreateJobObjectW(None, None)
            if not self._job:
                raise OSError("create browser job")
            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not api.SetInformationJobObject(self._job, 9, ctypes.byref(limits),
                                               ctypes.sizeof(limits)):
                raise OSError("configure browser job")
            security = SecurityAttributes()
            security.nLength, security.bInheritHandle = ctypes.sizeof(security), True
            null_handle = api.CreateFileW("NUL", 0xC0000000, 3, ctypes.byref(security),
                                          3, 0, None)
            if null_handle in (None, ctypes.c_void_p(-1).value):
                null_handle = None
                raise OSError("open browser standard handles")
            size = ctypes.c_size_t()
            api.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
            if not size.value:
                raise OSError("prepare browser handle list")
            attributes = ctypes.create_string_buffer(size.value)
            if not api.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(size)):
                raise OSError("initialize browser handle list")
            attributes_initialized = True
            handles = (wintypes.HANDLE * 1)(null_handle)
            if not api.UpdateProcThreadAttribute(attributes, 0, 0x20002, ctypes.byref(handles),
                                                 ctypes.sizeof(handles), None, None):
                raise OSError("set browser handle list")
            startup = StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = 0x100 | 0x1  # standard handles + explicit normal maximization
            startup.StartupInfo.wShowWindow = 3  # SW_SHOWMAXIMIZED, never exclusive/fullscreen
            startup.StartupInfo.hStdInput = null_handle
            startup.StartupInfo.hStdOutput = null_handle
            startup.StartupInfo.hStdError = null_handle
            startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline(arguments))
            # The GUI browser remains visible; NO_WINDOW only suppresses console windows.
            flags = 0x4 | 0x4000 | 0x08000000 | 0x80000
            base_startup = ctypes.cast(ctypes.byref(startup), ctypes.POINTER(StartupInfo))
            if not api.CreateProcessW(arguments[0], command, None, None, True, flags,
                                      None, None, base_startup, ctypes.byref(info)):
                raise OSError("create browser process")
            self._process, self.pid = info.hProcess, info.dwProcessId
            if not api.AssignProcessToJobObject(self._job, self._process):
                raise OSError("assign browser job")
            if api.ResumeThread(info.hThread) == 0xFFFFFFFF:
                raise OSError("start browser process")
        except OSError:
            # Never run an uncontained browser if job assignment fails.
            self.kill()
            self.close()
            raise LoginError("Windows could not open a safely managed sign-in window. Please retry.",
                             "browser_access") from None
        finally:
            if info.hThread:
                api.CloseHandle(info.hThread)
            if attributes_initialized:
                api.DeleteProcThreadAttributeList(attributes)
            if null_handle:
                api.CloseHandle(null_handle)

    def poll(self):
        if self._process is None:
            return self.returncode if self.returncode is not None else 0
        code = self._wintypes.DWORD()
        if self._api.GetExitCodeProcess(self._process, self._ctypes.byref(code)):
            if code.value != 259:  # STILL_ACTIVE
                self.returncode = code.value
        return self.returncode

    def kill(self):
        if self._job:
            self._api.TerminateJobObject(self._job, 1)
        if self._process:
            # Also covers a still-suspended process whose job assignment failed.
            self._api.TerminateProcess(self._process, 1)

    def close(self):
        if self._job:
            self._api.CloseHandle(self._job)
            self._job = None
        if self._process:
            self._api.WaitForSingleObject(self._process, 1000)
            self.poll()
            self._api.CloseHandle(self._process)
            self._process = None


def _chromium_login(choice, timeout, profile_dir=None):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise LoginError("YouTube sign-in support needs to be installed.",
                         "missing_dependency") from None
    temp_root = Path(tempfile.gettempdir()).resolve()
    if profile_dir:
        profile = Path(profile_dir).resolve()
        if (profile.parent != temp_root or not profile.name.startswith("MusicApp-signin-")
                or not profile.is_dir()):
            raise LoginError("The sign-in window could not be prepared. Please retry.", "browser_access")
    else:
        profile = Path(tempfile.mkdtemp(prefix="MusicApp-signin-", dir=str(temp_root))).resolve()
    process = browser = owned = None
    try:
        # A nonzero port is assigned by the OS. Port=0 changes Chrome's automation state.
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        arguments = [choice["executable"], "--user-data-dir=" + str(profile),
                     f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
                     "--no-first-run", "--no-default-browser-check", "--start-maximized", "--new-window", SIGN_IN_URL]
        if os.name == "nt":
            process = _OwnedWindowsBrowser(arguments)
        else:
            process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = monotonic() + min(30, timeout)
        endpoint = None
        while monotonic() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.5) as response:
                    candidate = json.loads(response.read(16384))["webSocketDebuggerUrl"]
                address = urlsplit(candidate)
                if (address.scheme == "ws" and address.hostname in {"127.0.0.1", "localhost"}
                        and address.port == port and address.path.startswith("/devtools/browser/")):
                    endpoint = candidate
                    break
            except (OSError, ValueError, KeyError):
                pass
            sleep(0.25)
        if endpoint is None:
            raise LoginError(f"Could not connect to {choice['name']}. Close the sign-in window and retry.",
                             "browser_access")
        with sync_playwright() as playwright:
            try:
                candidate = playwright.chromium.connect_over_cdp(endpoint, timeout=10000)
                owned = _owns_browser(candidate, process, profile)
                if owned is None:
                    raise LoginError("The sign-in browser could not be verified.", "browser_access")
                browser = candidate
            except Exception:
                raise LoginError(f"Could not connect to {choice['name']}. Please retry.",
                                 "browser_access") from None
            if not browser.contexts:
                raise LoginError("The sign-in browser has no available window.", "browser_access")
            _maximize_owned_browser(browser)
            context = browser.contexts[0]
            deadline = monotonic() + timeout
            while monotonic() < deadline:
                try:
                    pages = [page for page in context.pages if not page.is_closed()]
                    if not pages:
                        raise LoginError("The sign-in window was closed. Please retry.")
                    for page in pages:
                        address = urlsplit(page.url)
                        if (address.hostname == "accounts.google.com" and
                                any(marker in address.path.casefold()
                                    for marker in ("deniedsigninrejected", "/signin/rejected"))):
                            raise LoginError("Google declined this sign-in. Close the sign-in window and retry.")
                    cookies = [cookie for cookie in context.cookies(
                        ["https://www.youtube.com", "https://music.youtube.com"])
                        if _youtube_domain(cookie["domain"])]
                    if _signed_cookies(cookies):
                        for page in pages:
                            if not _youtube_domain(urlsplit(page.url).hostname or ""):
                                continue
                            # Cookies alone may appear before account selection has finished.
                            signed_in = page.evaluate(
                                "() => Boolean(window.ytcfg && window.ytcfg.get('LOGGED_IN') === true)")
                            if signed_in and page.locator("#avatar-btn").first.is_visible():
                                page.goto("https://www.youtube.com/robots.txt",
                                          wait_until="domcontentloaded", timeout=15000)
                                return [cookie for cookie in context.cookies(
                                    ["https://www.youtube.com", "https://music.youtube.com"])
                                    if _youtube_domain(cookie["domain"])]
                except LoginError:
                    raise
                except Exception:
                    if not browser.is_connected():
                        raise LoginError("The sign-in browser closed. Please retry.") from None
                    # A human navigating or completing Google's challenge can replace a page.
                sleep(1.5)
            raise LoginError("YouTube sign-in timed out. Please retry when you are ready.",
                             "signin_timeout")
    except OSError:
        raise LoginError(f"Could not open {choice['name']}. Check Windows Default apps.",
                         "browser_access") from None
    finally:
        _close_owned_browser(process, browser, owned)
        # This is our fresh temporary profile, never the user's real browser profile.
        if profile.parent == temp_root and profile.name.startswith("MusicApp-signin-"):
            shutil.rmtree(profile, ignore_errors=True)


def _maximize_owned_browser(browser):
    """Normally maximize our verified sign-in window; leave other browsers alone."""
    session = None
    try:
        context = browser.contexts[0]
        page = next(page for page in context.pages if not page.is_closed())
        session = context.new_cdp_session(page)
        window = session.send("Browser.getWindowForTarget")
        session.send("Browser.setWindowBounds", {
            "windowId": window["windowId"], "bounds": {"windowState": "maximized"}})
    except Exception:
        # --start-maximized and STARTUPINFO already request the same state.
        # CDP versions which omit window bounds must not block sign-in.
        pass
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass


class _SilentCookieLogger:
    def debug(self, *args, **kwargs):
        pass

    info = warning = error = debug


def _firefox_login(timeout):
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except ImportError:
        raise LoginError("YouTube sign-in support needs to be installed.",
                         "missing_dependency") from None
    import http.cookiejar
    from urllib.request import HTTPCookieProcessor, Request, build_opener
    try:
        os.startfile(SIGN_IN_URL)
    except OSError:
        raise LoginError("Could not open your default browser.", "browser_access") from None
    deadline = monotonic() + timeout
    readable = False
    while monotonic() < deadline:
        try:
            # The registered Firefox browser is used, with no Chromium DPAPI extraction.
            # Keep only YouTube cookies; do not save the browser's other site credentials.
            extracted = extract_cookies_from_browser("firefox", logger=_SilentCookieLogger(),
                                                    container="none")
            jar = http.cookiejar.CookieJar()
            cookies = []
            for cookie in extracted:
                if _youtube_domain(cookie.domain) and not cookie.is_expired():
                    jar.set_cookie(cookie)
                    cookies.append({"name": cookie.name, "value": cookie.value,
                                    "domain": cookie.domain, "path": cookie.path,
                                    "expires": cookie.expires if cookie.expires is not None else -1,
                                    "secure": cookie.secure})
            readable = True
            if _signed_cookies(cookies):
                request = Request(LIKES_URL, headers={"User-Agent": "Mozilla/5.0"})
                with build_opener(HTTPCookieProcessor(jar)).open(request, timeout=10) as response:
                    page = response.read(1024 * 1024).decode("utf-8", errors="replace")
                    if (_youtube_domain(urlsplit(response.url).hostname or "")
                            and re.search(r'"LOGGED_IN"\s*:\s*true', page)):
                        return cookies
        except Exception:
            pass
        sleep(min(10, max(0, deadline - monotonic())))
    if not readable:
        raise LoginError("Firefox opened, but its YouTube login could not be read. Close Firefox and retry.",
                         "cookie_access")
    raise LoginError("YouTube sign-in was not confirmed. Finish signing in, then retry.",
                     "signin_timeout")


def _preferred_signin_browser():
    """Prefer Chrome/Edge without changing Windows defaults or real profiles."""
    choice = default_browser()
    if choice.get("cookie_browser") in {"chrome", "edge"}:
        return choice
    if os.name == "nt":
        for relative, name, cookie_browser in (
                ("Microsoft/Edge/Application/msedge.exe", "Microsoft Edge", "edge"),
                ("Google/Chrome/Application/chrome.exe", "Google Chrome", "chrome")):
            for variable in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
                root = os.environ.get(variable)
                path = Path(root) / relative if root else None
                if path is not None and path.is_file():
                    return {"name": name, "executable": str(path),
                            "cookie_browser": cookie_browser, "chromium": True}
    return choice


def browser_login(job):
    choice = _preferred_signin_browser()
    timeout = max(30, min(300, float(job.get("signin_timeout", 300))))
    if choice["chromium"]:
        return _chromium_login(choice, timeout, job.get("profile_dir"))
    if choice["cookie_browser"] == "firefox":
        return _firefox_login(timeout)
    try:
        os.startfile(SIGN_IN_URL)
    except OSError:
        raise LoginError("Could not open your default browser.", "browser_access") from None
    raise LoginError(f"YouTube opened in {choice['name']}, but this browser does not support automatic sign-in confirmation.",
                     "browser_access")
