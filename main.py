import os

DevMode = os.path.exists(".developer_options.json")

try:
    from adminRaise import Administrator
    from autoDependency import AutoDependencies
    from autoUpdate import AutoUpdater
    from log_loader import log_loader, OutputRedirector
except:
    from .adminRaise import Administrator
    from .autoDependency import AutoDependencies
    from .autoUpdate import AutoUpdater
    from .log_loader import log_loader, OutputRedirector

### Install Handler ###

if not DevMode:
    AutoUpdater("https://github.com/The-Autonomous/MusicApp", branch="main").update()
    Administrator()
    AutoDependencies().install()

### Logging Handler ###

ll = log_loader("Main", debugging = False)
OutputRedirector(enable_dual_logging = DevMode)
ll.debug(f"Executing With Developer Mode: {DevMode}")

#######################

import tkinter as tk

try:
    from ghost import GhostOverlay
    from playerUtils import MusicOverlayController, ProgramShutdown
except ImportError:
    from .ghost import GhostOverlay
    from .playerUtils import MusicOverlayController, ProgramShutdown

def main():
    root = tk.Tk()
    root.withdraw()
    
    # Create and register shutdown handler #
    shutdown_handler = ProgramShutdown()
    shutdown_handler.register_root(root)
    
    overlay = GhostOverlay(root)
    controller = MusicOverlayController(overlay)
    controller.start()
    overlay.set_text("Initializing...")
    root.mainloop()

if __name__ == '__main__':
    main()