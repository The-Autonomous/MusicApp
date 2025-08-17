import os, sys, ctypes, subprocess, psutil

class Administrator:
    def __init__(self, require_admin=True):
        """When require_admin is True → prompt for UAC.
        If lower_priority is True → set BELOW_NORMAL priority so GTA gets more CPU love.
        """
        self.lower_process_priority()
        if require_admin and not self.is_admin():
            self.elevate()

    def is_admin(self) -> bool:
        try:
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False

    def elevate(self, w_o_admin=False):
        params = " ".join(f'"{arg}"' for arg in sys.argv)
        exe = os.path.splitext(sys.executable)[0] + "w.exe"
        hinst = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            exe,
            params,
            None,
            0  # SW_HIDE
        )
        if int(hinst) <= 32:
            print("⚠️ UAC elevation failed or was cancelled.")
        else:
            if w_o_admin:
                self.elevate_w_o_admin()
            else:
                sys.exit(0)

    def elevate_w_o_admin(self):
        params = [sys.executable.replace("python.exe", "pythonw.exe")] + sys.argv
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
        try:
            subprocess.Popen(params, startupinfo=startupinfo)
        except Exception as e:
            print(f"Failed to relaunch hidden: {e}")

    def lower_process_priority(self):
        """Drops current process to BELOW_NORMAL priority."""
        try:
            p = psutil.Process(os.getpid())
            p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            print("✅ Process priority lowered (BELOW_NORMAL).")
        except Exception as e:
            print(f"⚠️ Failed to lower process priority: {e}")
