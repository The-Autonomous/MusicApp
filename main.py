try:
    from autoDependency import AutoDependencies
except:
    from .autoDependency import AutoDependencies

AutoDependencies().install()

import tkinter as tk

try:
    from ghost import GhostOverlay
    from playerUtils import MusicOverlayController
    from adminRaise import Administrator
except ImportError:
    from .ghost import GhostOverlay
    from .playerUtils import MusicOverlayController
    from .adminRaise import Administrator

def main():
    root = tk.Tk()
    root.withdraw()
    overlay = GhostOverlay(root)
    controller = MusicOverlayController(overlay)
    controller.start()
    overlay.set_text("Initializing...")
    root.mainloop()


if __name__ == '__main__':
    Administrator()
    main()