import subprocess
import sys
import time
from importlib.util import find_spec
import importlib # For invalidate_caches

# --- tqdm installation ---
# It's good to see output if this initial critical dependency fails
try:
    from tqdm import tqdm
    # print("✅ 'tqdm' is already available.") # Optional: confirmation
except ImportError:
    print("🛠️ 'tqdm' not found. Attempting to install 'tqdm' for progress bars...")
    try:
        # Show pip's output for this initial install for clarity if it fails
        subprocess.check_call([sys.executable, "-m", "pip", "install", "tqdm"])
        print("✅ 'tqdm' installed successfully.")
        from tqdm import tqdm
    except subprocess.CalledProcessError as e:
        print(f"❌ Critical Error: Failed to install 'tqdm'. tqdm is required for this script.")
        print(f"   Please install it manually: {sys.executable} -m pip install tqdm")
        print(f"   Error details: {e}")
        sys.exit(1) # Exit if tqdm cannot be installed
    except ImportError:
        print(f"❌ Critical Error: 'tqdm' was reportedly installed but still cannot be imported.")
        sys.exit(1)


class AutoDependencies:
    def __init__(self):
        """
        packages: dict mapping pip package name to import module name.
        Example: {"Requests": "requests", "yt-dlp": "yt_dlp"}
        Pip package names are case-insensitive for installation but using the
        canonical name (often capitalized as on PyPI) is good practice.
        Module names are case-sensitive and typically lowercase with underscores.
        """
        print(f"🐍 Running with Python interpreter: {sys.executable}")
        # For deeper debugging, you can uncomment the sys.path print:
        # import pprint
        # print(f"🐍 sys.path:")
        # pprint.pprint(sys.path)

        self.packages = {
            "requests": "requests",
            "pygame": "pygame",
            "mutagen": "mutagen",
            "yt-dlp": "yt_dlp", # pip install yt-dlp, import yt_dlp
            "Flask": "flask",    # pip install Flask, import flask
            "Flask-Compress": "flask_compress", # CORRECTED: pip install Flask-Compress, import flask_compress
            "pynput": "pynput",
            "aiohttp": "aiohttp",
            "numpy": "numpy",
            "psutil": "psutil",
            "waitress": "waitress",
            }
        
        self.missing_details = []
        print("\n🔍 Checking dependencies...")
        importlib.invalidate_caches() # Ensure a fresh view before checking
        for pkg_name, module_name in self.packages.items():
            spec = find_spec(module_name)
            if spec is None:
                print(f"  ❓ Module '{module_name}' (for package '{pkg_name}') -> NOT FOUND by find_spec.")
                self.missing_details.append({'pkg': pkg_name, 'mod': module_name})
            else:
                # Providing spec.origin can be long, conditionally print or shorten
                origin = spec.origin
                if origin and len(origin) > 70: # Heuristic for long paths
                    origin = "..." + origin[-67:]
                print(f"  ✅ Module '{module_name}' (for package '{pkg_name}') -> FOUND (origin: {origin})")
        
        self.missing_pkgs_to_install = [details['pkg'] for details in self.missing_details]
        if self.missing_pkgs_to_install:
            print(f"\n📋 Missing packages that will be targeted for installation: {', '.join(self.missing_pkgs_to_install)}")
        else:
            print("👍 All listed Python dependencies appear to be met based on initial check.")


    def install(self):
        if not self.missing_pkgs_to_install:
            # This message might be redundant if __init__ already stated all clear
            # print("✅ All dependencies were already satisfied (checked in install method).")
            return

        print(f"\n📦 Attempting to install {len(self.missing_pkgs_to_install)} missing package(s)...\n")

        for pkg_to_install in tqdm(self.missing_pkgs_to_install, desc="Setting up environment", bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt}"):
            # Find the module name associated with this package for post-install verification
            module_to_verify = next((item['mod'] for item in self.missing_details if item['pkg'] == pkg_to_install), None)

            # tqdm can sometimes interfere with subprocess stdout/stderr if not flushed.
            # A print before subprocess call can help.
            sys.stdout.flush() # Ensure "Setting up environment" line is fully printed
            print(f"\n  ⏳ Installing '{pkg_to_install}'...")
            sys.stdout.flush() 

            try:
                # Show pip's output directly for better diagnostics
                process = subprocess.Popen(
                    [sys.executable, "-m", "pip", "install", pkg_to_install],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True # Decodes output as text
                )
                stdout, stderr = process.communicate()

                if process.returncode == 0:
                    print(f"  ✅ Successfully ran pip install for '{pkg_to_install}'.")
                    # print(f"     Output:\n{stdout}") # Uncomment for full pip output
                    if stderr: # Sometimes pip puts warnings or non-fatal messages in stderr
                        print(f"     Messages from pip (stderr):\n{stderr}")
                else:
                    print(f"  ❌ pip install for '{pkg_to_install}' FAILED with return code {process.returncode}.")
                    print(f"     Stdout:\n{stdout}")
                    print(f"     Stderr:\n{stderr}")
                    # Continue to next package, but this one failed.
                    continue # Skip verification for this failed package

                importlib.invalidate_caches() # Crucial for recognizing the new module
                
                if module_to_verify:
                    spec_after_install = find_spec(module_to_verify)
                    if spec_after_install:
                        print(f"  👍 Verification successful: Module '{module_to_verify}' now found by find_spec.")
                    else:
                        print(f"  ⚠️ Verification FAILED: Module '{module_to_verify}' STILL NOT found by find_spec after install & cache invalidation.")
                        print(f"     This could indicate an issue with the package itself, your Python environment's PATH,")
                        print(f"     or the module name ('{module_to_verify}') might be incorrect for package '{pkg_to_install}'.")
                
                time.sleep(0.1) # Small pause, mainly for aesthetics with tqdm if many items
            except Exception as e: # Catch other exceptions like FileNotFoundError if pip isn't found
                print(f"  ❌ An unexpected error occurred while trying to install {pkg_to_install}: {e}")

        print("\n🏁 Dependency installation process finished.")
        
        # --- Final Verification ---
        print("\n🔁 Re-checking all dependencies after installation attempts:")
        final_still_missing_pkgs = []
        importlib.invalidate_caches() # One more invalidation before final checks
        for pkg, mod in self.packages.items():
            spec = find_spec(mod)
            if spec is None:
                print(f"  ❌ Post-install check: Module '{mod}' (for package '{pkg}') -> STILL NOT FOUND.")
                final_still_missing_pkgs.append(pkg)
            else:
                print(f"  ✅ Post-install check: Module '{mod}' (for package '{pkg}') -> FOUND.")
        
        if not final_still_missing_pkgs:
            print("\n🎉 All dependencies appear to be resolved now!")
        else:
            print(f"\n⚠️ Some dependencies might still be missing or not detectable: {', '.join(final_still_missing_pkgs)}")
            print(f"    If issues persist, please manually check their installation in your Python environment:")
            print(f"    Interpreter: {sys.executable}")
            print(f"    You can try: {sys.executable} -m pip install <package_name>")


if __name__ == "__main__":
    print("--- Starting Dependency Check ---")
    installer = AutoDependencies() # __init__ performs the initial check
    
    if installer.missing_pkgs_to_install: # If __init__ found missing packages
        installer.install()
    else:
        # Message already printed in __init__ if all good
        pass
    print("\n--- Dependency Check Complete ---")