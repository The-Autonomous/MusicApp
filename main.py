"""Connect YouTube before playback; keep maintenance separate from startup."""
import os
from pathlib import Path
import sys


def main():
    os.chdir(Path(__file__).parent)
    # These limits apply to this app and its children, never to other programs.
    os.environ.update(OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1",
                      NUMEXPR_NUM_THREADS="1", BLIS_NUM_THREADS="1")
    if "--setup" in sys.argv:
        from autoDependency import AutoDependencies
        AutoDependencies().install()
        return
    if "--update" in sys.argv:
        from autoUpdate import AutoUpdater
        AutoUpdater("https://github.com/The-Autonomous/MusicApp", branch="main").update()
        return

    import tkinter as tk
    from log_loader import OutputRedirector
    with OutputRedirector(enable_dual_logging=False):
        from startup import ensure_runtime
        root = tk.Tk()
        root.withdraw()
        print(f"[Startup] Python: {sys.executable}", flush=True)
        if not ensure_runtime(root):
            root.destroy()
            return

        from ghost import GhostOverlay
        from playerUtils import MusicOverlayController, ProgramShutdown
        shutdown = ProgramShutdown()
        shutdown.register_root(root)
        overlay = GhostOverlay(root)
        controller = MusicOverlayController(overlay, fast_load=Path(".fast_load.json").exists())
        shutdown.register_cleanup(controller.close)
        shutdown.register_cleanup(overlay.close)
        root.protocol("WM_DELETE_WINDOW", shutdown.shutdown)
        root.after(0, lambda: controller.youtube_sync.show(overlay, startup=True))
        try:
            root.mainloop()
        finally:
            controller.close()
            overlay.close()


if __name__ == "__main__":
    main()
