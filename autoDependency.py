import subprocess, urllib, sys, time, importlib, os, zipfile, platform
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from importlib.util import find_spec
from importlib.metadata import version, PackageNotFoundError
from typing import Dict, List, Optional, Tuple

# --- tqdm installation ---
try:
    from tqdm import tqdm
except ImportError:
    print("🛠️ 'tqdm' not found. Attempting to install 'tqdm' for progress bars...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "tqdm"], 
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("✅ 'tqdm' installed successfully.")
        from tqdm import tqdm
    except subprocess.CalledProcessError as e:
        print(f"❌ Critical Error: Failed to install 'tqdm'. Error: {e}")
        print(f"   Please install it manually: {sys.executable} -m pip install tqdm")
        sys.exit(1)
    except ImportError:
        print("❌ Critical Error: 'tqdm' was installed but cannot be imported.")
        sys.exit(1)

class AutoDependencies:
    def __init__(self, timeout: int = 30, max_retries: int = 3):
        """
        Enhanced dependency manager with better error handling and reliability.
        
        Args:
            timeout: Timeout in seconds for network operations
            max_retries: Maximum number of retry attempts for failed installations
        """
        self.timeout = timeout
        self.max_retries = max_retries
        
        print(f"🐍 Running with Python {sys.version}")
        print(f"🐍 Python interpreter: {sys.executable}")
        print(f"🔧 Platform: {platform.system()} {platform.release()}")
        
        # Enhanced package mapping with version constraints and alternatives
        self.packages = {
            "numpy": {"module": "numpy", "min_version": "1.20.0"},
            "av": {"module": "av", "min_version": None},
            "colorama": {"module": "colorama", "min_version": None},
            "requests": {"module": "requests", "min_version": "2.25.0"},
            "sounddevice": {"module": "sounddevice", "min_version": None},
            "mutagen": {"module": "mutagen", "min_version": None},
            "yt-dlp": {"module": "yt_dlp", "min_version": None},
            "Flask": {"module": "flask", "min_version": "2.0.0"},
            "Flask-Compress": {"module": "flask_compress", "min_version": None},
            "pynput": {"module": "pynput", "min_version": None},
            "aiohttp": {"module": "aiohttp", "min_version": "3.7.0"},
            "psutil": {"module": "psutil", "min_version": None},
            "waitress": {"module": "waitress", "min_version": None},
            "playwright": {"module": "playwright", "min_version": "1.40.0"},
            "ytmusicapi": {"module": "ytmusicapi", "min_version": "1.8.0"},
        }
        
        self.missing_details = []
        self.failed_installs = []
        
        # Check if we're in a virtual environment
        self._check_virtual_environment()
        
        # Perform initial dependency check
        self._initial_check()
        
    def ensure_build_tools(self) -> None:
        """Install MSVC Build Tools with timeout and better error handling."""
        if platform.system() != 'Windows':
            return  # only matters on Windows

        # Check for cl.exe in PATH
        for p in os.environ['PATH'].split(os.pathsep):
            if Path(p, 'cl.exe').exists():
                print("✅ [MSVC] Build Tools already present")
                return
        
        # Also check common VS installation paths
        vs_paths = [
            r"C:\Program Files (x86)\Microsoft Visual Studio\2019\BuildTools\VC\Tools\MSVC",
            r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Tools\MSVC",
            r"C:\Program Files\Microsoft Visual Studio\2019\BuildTools\VC\Tools\MSVC",
            r"C:\Program Files\Microsoft Visual Studio\2022\BuildTools\VC\Tools\MSVC",
        ]
        
        for vs_path in vs_paths:
            if Path(vs_path).exists():
                # Find cl.exe in subdirectories
                for root, dirs, files in os.walk(vs_path):
                    if 'cl.exe' in files:
                        print("✅ [MSVC] Build Tools found in VS installation")
                        return

        print("🔧 [MSVC] Build Tools missing, attempting installation...")
        
        # Try chocolatey first (much more reliable)
        if self._try_chocolatey_build_tools():
            return
        
        # Fallback to direct VS installer with strict timeout
        try:
            self._install_vs_build_tools_direct()
        except Exception as e:
            print(f"❌ [MSVC] Installation failed: {e}")
            print("Administrator privileges are required for this operation!")

    def _try_chocolatey_build_tools(self) -> bool:
        """Try installing build tools via chocolatey (more reliable)."""
        try:
            # Check if chocolatey is available
            result = subprocess.run(['choco', '--version'], 
                                  capture_output=True, timeout=5)
            if result.returncode != 0:
                print("📝 [MSVC] Chocolatey not available, trying direct installation")
                return False
            
            print("🍫 [MSVC] Installing via Chocolatey...")
            cmd = ['choco', 'install', 'visualstudio2022buildtools', 
                   '--package-parameters', '--add Microsoft.VisualStudio.Workload.VCTools', 
                   '-y']
            
            result = subprocess.run(cmd, timeout=600)  # 10 minute timeout
            if result.returncode == 0:
                print("✅ [MSVC] Build Tools installed via Chocolatey")
                return True
            else:
                print("❌ [MSVC] Chocolatey installation failed")
                return False
                
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            print(f"📝 [MSVC] Chocolatey method failed: {e}")
            return False

    def _install_vs_build_tools_direct(self) -> None:
        """Direct VS Build Tools installation with strict timeout."""
        url = "https://aka.ms/vs/17/release/vs_BuildTools.exe"
        exe = Path.cwd() / "vs_buildtools.exe"

        try:
            print("📥 [MSVC] Downloading Visual Studio Build Tools...")
            
            # Download with progress and timeout
            req = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urlopen(req, timeout=30) as response:
                total_size = int(response.headers.get('content-length', 0))
                with open(exe, 'wb') as out_file:
                    if total_size > 0:
                        with tqdm(total=total_size, unit='B', unit_scale=True, desc="Downloading") as pbar:
                            while True:
                                chunk = response.read(8192)
                                if not chunk:
                                    break
                                out_file.write(chunk)
                                pbar.update(len(chunk))
                    else:
                        out_file.write(response.read())

            print("🔧 [MSVC] Installing Build Tools (this may take 5-10 minutes)...")
            
            # More aggressive silent installation flags
            cmd = [
                str(exe), 
                "--quiet", "--wait", "--norestart", "--nocache",
                "--add", "Microsoft.VisualStudio.Workload.VCTools",
                "--add", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                "--add", "Microsoft.VisualStudio.Component.Windows10SDK.19041",
                "--includeRecommended",
                "--force"  # Force installation even if newer version exists
            ]
            
            # Run with strict timeout and process monitoring
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            try:
                # Wait for completion with timeout
                stdout, stderr = process.communicate(timeout=900)  # 15 minute timeout
                
                if process.returncode == 0:
                    print("✅ [MSVC] Build Tools installed successfully")
                elif process.returncode == 3010:  # Reboot required
                    print("✅ [MSVC] Build Tools installed (reboot required)")
                else:
                    print(f"⚠️ [MSVC] Installer returned code {process.returncode}")
                    print("    Build tools may still work. Continuing...")
                    
            except subprocess.TimeoutExpired:
                print("⏰ [MSVC] Installation timed out after 15 minutes")
                print("    Terminating installer process...")
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                
                print("⚠️ [MSVC] Installation may be incomplete")
                print("   You may need to:")
                print("   1. Restart your computer")
                print("   2. Install Visual Studio Build Tools manually")
                print("   3. Or install via: pip install scipy --no-build-isolation")
                
        except (URLError, HTTPError) as e:
            print(f"❌ [MSVC] Download failed: {e}")
            print("   Please install Build Tools manually from:")
            print("   https://visualstudio.microsoft.com/downloads/#build-tools-for-visual-studio-2022")
            
        except Exception as e:
            print(f"❌ [MSVC] Installation failed: {e}")
            print("   Consider installing scipy with pre-compiled wheels:")
            print(f"   {sys.executable} -m pip install scipy --no-build-isolation")
            
        finally:
            # Always cleanup the installer
            if exe.exists():
                try:
                    exe.unlink()
                except PermissionError:
                    print(f"⚠️ Could not delete {exe}, please remove manually")

    def _check_virtual_environment(self) -> None:
        """Check if running in a virtual environment and warn if not."""
        in_venv = (hasattr(sys, 'real_prefix') or 
                  (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix))
        
        if in_venv:
            print("✅ Running in virtual environment")
        else:
            print("⚠️  Not running in virtual environment - installations will be system-wide")
            print("   Consider using a virtual environment for better isolation")

    def _check_pip_availability(self) -> bool:
        """Verify pip is available and working."""
        try:
            result = subprocess.run([sys.executable, "-m", "pip", "--version"], 
                                  capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                print(f"✅ pip available: {result.stdout.strip()}")
                return True
            else:
                print(f"❌ pip check failed: {result.stderr}")
                return False
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"❌ pip not available: {e}")
            return False

    def _get_installed_version(self, package_name):
        try:
            return version(package_name)
        except PackageNotFoundError:
            return None

    def _version_compare(self, current: str, required: str) -> bool:
        """Simple version comparison. Returns True if current >= required."""
        try:
            current_parts = [int(x) for x in current.split('.')]
            required_parts = [int(x) for x in required.split('.')]
            
            # Pad shorter version with zeros
            max_len = max(len(current_parts), len(required_parts))
            current_parts.extend([0] * (max_len - len(current_parts)))
            required_parts.extend([0] * (max_len - len(required_parts)))
            
            return current_parts >= required_parts
        except ValueError:
            # If version parsing fails, assume it's okay
            return True

    def _initial_check(self) -> None:
        """Perform initial dependency check with version validation."""
        print("\n🔍 Checking dependencies...")
        
        if not self._check_pip_availability():
            print("❌ pip is not available. Cannot proceed with dependency management.")
            sys.exit(1)
            
        # Prefer binary wheels. A music player does not need a compiler installation.
        importlib.invalidate_caches()
        
        for pkg_name, pkg_info in self.packages.items():
            module_name = pkg_info["module"]
            min_version = pkg_info["min_version"]
            
            spec = find_spec(module_name)
            if spec is None:
                print(f"  ❓ Module '{module_name}' (package '{pkg_name}') -> NOT FOUND")
                self.missing_details.append({'pkg': pkg_name, 'mod': module_name, 'info': pkg_info})
            else:
                # Check version if specified
                version_ok = True
                installed_version = None
                
                if min_version:
                    installed_version = self._get_installed_version(pkg_name)
                    if installed_version:
                        version_ok = self._version_compare(installed_version, min_version)
                        if not version_ok:
                            print(f"  ⚠️  Module '{module_name}' found but version {installed_version} < {min_version}")
                            self.missing_details.append({'pkg': pkg_name, 'mod': module_name, 'info': pkg_info})
                            continue
                
                # Module found and version is acceptable
                version_info = f" (v{installed_version})" if installed_version else ""
                origin = spec.origin
                if origin and len(origin) > 70:
                    origin = "..." + origin[-67:]
                print(f"  ✅ Module '{module_name}' (package '{pkg_name}'){version_info} -> FOUND")
        
        self.missing_pkgs_to_install = [details['pkg'] for details in self.missing_details]
        
        if self.missing_pkgs_to_install:
            print(f"\n📋 Packages needing installation/upgrade: {', '.join(self.missing_pkgs_to_install)}")
        else:
            print("👍 All Python dependencies are satisfied!")
            self.ensure_ffmpeg()
            print("🎉 All dependencies are ready!")

    def ensure_ffmpeg(self) -> None:
        """Enhanced ffmpeg installation with better error handling."""
        try:
            result = subprocess.run(['ffprobe', '-version'], 
                                  stdout=subprocess.DEVNULL, 
                                  stderr=subprocess.DEVNULL, 
                                  timeout=5)
            if result.returncode == 0:
                print("✅ [FFMPEG/FFPROBE] Found in PATH")
                return
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        bundled = Path(__file__).parent / "ffmpeg-bin"
        binary = next(bundled.glob("**/ffmpeg.exe"), None) if bundled.exists() else None
        if binary and binary.with_name("ffprobe.exe").is_file():
            os.environ["PATH"] = str(binary.parent) + os.pathsep + os.environ["PATH"]
            print("✅ [FFMPEG] Using bundled executables")
            return

        print("🔧 [FFMPEG/FFPROBE] Not found. Attempting to install...")
        
        # Only attempt Windows installation for now
        if platform.system() != 'Windows':
            print("⚠️  Auto-installation only supported on Windows.")
            print("   Please install ffmpeg manually for your platform:")
            print("   - macOS: brew install ffmpeg")
            print("   - Linux: sudo apt install ffmpeg (Ubuntu/Debian)")
            print("   - Or download from: https://ffmpeg.org/download.html")
            return

        ffmpeg_dir = Path(__file__).parent / 'ffmpeg-bin'
        ffmpeg_dir.mkdir(exist_ok=True)
        zip_path = ffmpeg_dir / 'ffmpeg.zip'

        try:
            print("📥 Downloading ffmpeg (this may take a while)...")
            zip_url = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
            
            # Add user agent and handle potential network issues
            req = Request(zip_url, headers={'User-Agent': 'Mozilla/5.0'})
            
            with urlopen(req, timeout=self.timeout) as response:
                total_size = int(response.headers.get('content-length', 0))
                
                with open(zip_path, 'wb') as out_file:
                    if total_size > 0:
                        with tqdm(total=total_size, unit='B', unit_scale=True, desc="Downloading") as pbar:
                            while True:
                                chunk = response.read(8192)
                                if not chunk:
                                    break
                                out_file.write(chunk)
                                pbar.update(len(chunk))
                    else:
                        out_file.write(response.read())

            print("📦 Extracting ffmpeg...")
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                # Extract only the executables we need
                for member in tqdm(zip_ref.namelist(), desc="Extracting"):
                    if any(exe in member for exe in ['bin/ffmpeg.exe', 'bin/ffprobe.exe']):
                        zip_ref.extract(member, ffmpeg_dir)

            zip_path.unlink()  # Remove zip file

            # Find and add to PATH
            for root, _, files in os.walk(ffmpeg_dir):
                if 'ffmpeg.exe' in files:
                    bin_path = os.path.abspath(root)
                    os.environ['PATH'] = bin_path + os.pathsep + os.environ['PATH']
                    
                    # Verify installation
                    subprocess.run(['ffmpeg', '-version'], 
                                 check=True, 
                                 stdout=subprocess.DEVNULL, 
                                 stderr=subprocess.DEVNULL,
                                 timeout=5)
                    print("✅ [FFMPEG] Successfully installed and verified")
                    return

            raise FileNotFoundError("Failed to find ffmpeg.exe after extraction")

        except (URLError, HTTPError, subprocess.TimeoutExpired) as e:
            print(f"❌ [FFMPEG] Network/timeout error: {e}")
            print("   Please install ffmpeg manually from: https://ffmpeg.org/download.html")
        except Exception as e:
            print(f"❌ [FFMPEG] Installation failed: {e}")
            print("   Please install ffmpeg manually and add it to your PATH")
        finally:
            # Cleanup partial download
            if zip_path.exists():
                zip_path.unlink()

    def _install_scipy_with_fallbacks(self, attempt: int) -> bool:
        """Install scipy with multiple fallback strategies."""
        strategies = [
            ("binary wheel", ["scipy", "--only-binary=all"]),
            ("no build isolation", ["scipy", "--no-build-isolation"]),  
            ("pre-release", ["scipy", "--pre"]),
            ("force reinstall", ["scipy", "--force-reinstall", "--no-deps"])
        ]
        
        for strategy_name, extra_args in strategies:
            try:
                print(f"    📦 Trying scipy installation: {strategy_name}")
                
                cmd = [sys.executable, "-m", "pip", "install"] + extra_args
                if not (hasattr(sys, 'real_prefix') or 
                       (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix)):
                    cmd.append("--user")
                
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                
                if result.returncode == 0:
                    print(f"    ✅ scipy installed successfully using {strategy_name}")
                    return True
                else:
                    print(f"    ❌ {strategy_name} failed: {result.returncode}")
                    
            except subprocess.TimeoutExpired:
                print(f"    ⏰ {strategy_name} timed out")
                continue
            except Exception as e:
                print(f"    ❌ {strategy_name} error: {e}")
                continue
        
        print(f"  ❌ All scipy installation strategies failed")
        return False

    def _install_package(self, pkg_name: str, attempt: int = 1) -> bool:
        """Enhanced package installation with scipy-specific handling."""
        print(f"  ⏳ Installing '{pkg_name}' (attempt {attempt}/{self.max_retries})...")
        
        # Special handling for scipy
        if pkg_name.lower() == 'scipy':
            return self._install_scipy_with_fallbacks(attempt)
        
        # Regular installation for other packages
        try:
            cmd = [sys.executable, "-m", "pip", "install", "--only-binary=:all:", pkg_name]
            if not (hasattr(sys, 'real_prefix') or 
                   (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix)):
                cmd.append("--user")
            
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                #timeout=self.timeout * 2  # Does not work on Py 3.13
            )
            
            try:
                stdout, stderr = process.communicate(timeout=180)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
            
            if process.returncode == 0:
                print(f"  ✅ Successfully installed '{pkg_name}'")
                return True
            else:
                print(f"  ❌ Failed to install '{pkg_name}' (exit code: {process.returncode})")
                if stderr:
                    # Show only the most relevant part of the error
                    error_lines = stderr.strip().split('\n')
                    relevant_errors = [line for line in error_lines if 'ERROR' in line.upper()]
                    if relevant_errors:
                        print(f"     Error: {relevant_errors[-1]}")
                    else:
                        print(f"     Error: {error_lines[-1] if error_lines else 'Unknown error'}")
                return False
                
        except subprocess.TimeoutExpired:
            print(f"  ⏰ Installation of '{pkg_name}' timed out after {self.timeout * 2} seconds")
            return False
        except Exception as e:
            print(f"  ❌ Unexpected error installing '{pkg_name}': {e}")
            return False

    def install(self) -> None:
        """Install missing packages with enhanced error handling and retry logic."""
        if not self.missing_pkgs_to_install:
            return

        print(f"\n📦 Installing {len(self.missing_pkgs_to_install)} package(s)...\n")

        # Update pip first
        print("🔄 Updating pip...")
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"], 
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            print("✅ pip updated successfully")
        except Exception:
            print("⚠️  Could not update pip, continuing with current version")

        for pkg_to_install in tqdm(self.missing_pkgs_to_install, 
                                  desc="Installing packages", 
                                  bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt}"):
            
            # Find module name for verification
            module_to_verify = next((item['mod'] for item in self.missing_details 
                                   if item['pkg'] == pkg_to_install), None)
            
            success = False
            for attempt in range(1, self.max_retries + 1):
                if self._install_package(pkg_to_install, attempt):
                    success = True
                    break
                elif attempt < self.max_retries:
                    print(f"  🔄 Retrying in 2 seconds...")
                    time.sleep(2)
            
            if not success:
                print(f"  ❌ Failed to install '{pkg_to_install}' after {self.max_retries} attempts")
                self.failed_installs.append(pkg_to_install)
                continue
            
            # Verify installation
            importlib.invalidate_caches()
            time.sleep(0.5)  # Brief pause for filesystem sync
            
            if module_to_verify:
                spec_after_install = find_spec(module_to_verify)
                if spec_after_install:
                    print(f"  👍 Verification successful: '{module_to_verify}' is now available")
                else:
                    print(f"  ⚠️  Module '{module_to_verify}' still not found after installation")
                    self.failed_installs.append(pkg_to_install)

        self._final_verification()

    def _final_verification(self) -> None:
        """Comprehensive final verification of all dependencies."""
        print("\n🔁 Final dependency verification:")
        
        still_missing = []
        importlib.invalidate_caches()
        
        for pkg, pkg_info in self.packages.items():
            module_name = pkg_info["module"]
            min_version = pkg_info["min_version"]
            
            spec = find_spec(module_name)
            if spec is None:
                print(f"  ❌ Module '{module_name}' (package '{pkg}') -> STILL MISSING")
                still_missing.append(pkg)
            else:
                # Check version if required
                version_ok = True
                if min_version:
                    installed_version = self._get_installed_version(pkg)
                    if installed_version:
                        version_ok = self._version_compare(installed_version, min_version)
                
                if version_ok:
                    print(f"  ✅ Module '{module_name}' (package '{pkg}') -> OK")
                else:
                    print(f"  ⚠️  Module '{module_name}' version issue")
                    still_missing.append(pkg)

        # Summary
        if not still_missing and not self.failed_installs:
            print("\n🎉 All dependencies successfully installed and verified!")
            self.ensure_ffmpeg()
        else:
            if still_missing:
                print(f"\n⚠️  Still missing: {', '.join(still_missing)}")
            if self.failed_installs:
                print(f"⚠️  Failed installations: {', '.join(self.failed_installs)}")
            
            print(f"\n🔧 Troubleshooting tips:")
            print(f"   • Try running: {sys.executable} -m pip install --upgrade pip")
            print(f"   • Check your internet connection")
            print(f"   • Consider using a virtual environment")
            print(f"   • Manual install: {sys.executable} -m pip install <package_name>")

if __name__ == "__main__":
    print("🚀 Starting Enhanced Dependency Check")
    print("=" * 50)
    
    try:
        installer = AutoDependencies(timeout=60, max_retries=3)
        
        if installer.missing_pkgs_to_install:
            installer.install()
        
        print("\n" + "=" * 50)
        print("✅ Dependency check complete!")
        
    except KeyboardInterrupt:
        print("\n❌ Process interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        sys.exit(1)
