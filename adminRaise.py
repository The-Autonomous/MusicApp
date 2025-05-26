import os, sys, ctypes

class Administrator:
    def __init__(self, require_admin=True):
        """When require_admin Is Set To True Will Prompt For UAC."""
        if require_admin and not self.is_admin():
            self.elevate()

    def is_admin(self) -> bool:
        try:
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False

    def elevate(self):
        # build args
        params = " ".join(f'"{arg}"' for arg in sys.argv)
        # point to pythonw.exe (no console) instead of python.exe
        exe = os.path.splitext(sys.executable)[0] + "w.exe"
        # SW_HIDE == 0, runas triggers UAC
        hinst = ctypes.windll.shell32.ShellExecuteW(
            None,                # hwnd
            "runas",             # verb
            exe,                 # pythonw.exe
            params,              # script + args
            None,                # cwd
            0                    # SW_HIDE -> invisible
        )
        if int(hinst) <= 32:
            print("⚠️ UAC elevation failed or was cancelled.")
        else:
            # parent just exits; elevated child runs hidden
            os._exit(0)