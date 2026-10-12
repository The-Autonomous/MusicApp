"""Start playback directly. Installation and source updates are explicit maintenance."""
import os
from pathlib import Path
import sys


def main():
    os.chdir(Path(__file__).parent)
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
    from ghost import GhostOverlay
    from playerUtils import MusicOverlayController, ProgramShutdown

    with OutputRedirector(enable_dual_logging=False):
        root = tk.Tk()
        root.withdraw()
        shutdown = ProgramShutdown()
        shutdown.register_root(root)
        overlay = GhostOverlay(root)
        controller = MusicOverlayController(overlay, fast_load=Path(".fast_load.json").exists())
        shutdown.register_cleanup(controller.close)
        shutdown.register_cleanup(overlay.close)
        root.protocol("WM_DELETE_WINDOW", shutdown.shutdown)
        root.after(600, lambda: controller.youtube_sync.show(overlay, startup=True))
        try:
            root.mainloop()
        finally:
            controller.close()
            overlay.close()


if __name__ == "__main__":
    main()
