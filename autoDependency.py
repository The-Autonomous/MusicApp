import subprocess
import sys
import time
from importlib.util import find_spec

try:
    from tqdm import tqdm
except ImportError:
    print("📦 'tqdm' not found. Installing tqdm for progress bars...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "tqdm"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    from tqdm import tqdm

class AutoDependencies:
    def __init__(self):
        """
        packages: dict mapping pip package name to import module name.
        Example: {"requests": "requests", "yt-dlp": "yt_dlp"}
        """
        self.packages = {
            "requests": "requests",
            "pygame": "pygame",
            "mutagen": "mutagen",
            "yt-dlp": "yt_dlp",
            "flask": "flask",
            "flask-compress": "flask-compress",
            "pynput": "pynput",
            "aiohttp": "aiohttp",
            "numpy": "numpy",
            "psutil": "psutil",
            "waitress": "waitress",
            }
        self.missing = [pkg for pkg, mod in self.packages.items() if find_spec(mod) is None]

    def install(self):
        if not self.missing:
            print("✅ All dependencies are already satisfied.")
            return

        print("📦 Installing missing dependencies...\n")

        # Use tqdm to show a smooth progress bar for each missing package
        for pkg in tqdm(self.missing, desc="Setting up environment", bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt}"):
            try:
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", pkg],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                # small pause for a smoother animation
                time.sleep(0.2)
            except subprocess.CalledProcessError:
                print(f"❌ Failed to install {pkg}. Please install it manually.")

        print("\n✅ Done. All missing dependencies have been installed.")


if __name__ == "__main__":
    AutoDependencies().install()
