import os
import sys
import requests
from urllib.parse import urljoin
import tkinter as tk
from tkinter import messagebox
import shutil
from datetime import datetime

try:
    from log_loader import log_loader
except:
    from .log_loader import log_loader

### Logging Handler ###

ll = log_loader("Auto Update", debugging = True)

#######################

class AutoUpdater:
    def __init__(self, github_repo_url, branch="main"):
        """
        Initializes the auto-updater.

        Parameters:
            github_repo_url (str): e.g. "https://github.com/user/repo"
            branch (str): branch name (default: "main")
        """
        self.repo_url = github_repo_url.rstrip('/')
        parts = self.repo_url.split('/')
        self.owner, self.repo = parts[-2], parts[-1]
        self.branch = branch
        self.api_base = f"https://api.github.com/repos/{self.owner}/{self.repo}/contents"
        self.raw_base = f"https://raw.githubusercontent.com/{self.owner}/{self.repo}/{self.branch}/"
        self.session = requests.Session()
        self.local_dir = os.path.dirname(os.path.abspath(__file__))
        self.backup_dir = os.path.join(self.local_dir, 'backup')
        if not os.path.isdir(self.backup_dir):
            os.makedirs(self.backup_dir)
        self.files_updated = []  # Track which files were actually updated

    def show_rate_limit_alert(self):
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("Update Warning", "GitHub rate limit exceeded. You may be running an older version.")
        root.destroy()

    def list_py_files(self, path=""):
        """
        Recursively list all .py files under given path in repo.
        """
        url = f"{self.api_base}/{path}" if path else self.api_base
        params = {'ref': self.branch}
        
        try:
            resp = self.session.get(url, params=params)
            
            # Rate-limit detection
            if resp.status_code == 403 and 'X-RateLimit-Remaining' in resp.headers and resp.headers['X-RateLimit-Remaining'] == '0':
                ll.warn("❌ Rate limit exceeded. Can't continue updates.")
                self.show_rate_limit_alert()
                sys.exit(1)
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            ll.error(f"❌ Error fetching file list: {e}")
            return []

        files = []
        try:
            for item in resp.json():
                if item['type'] == 'file' and item['name'].endswith('.py'):
                    files.append(item['path'])
                elif item['type'] == 'dir':
                    files.extend(self.list_py_files(item['path']))
        except Exception as e:
            ll.error(f"❌ Error parsing directory contents: {e}")
            
        return files

    def fetch_and_update(self, path):
        """
        Fetch a remote file, compare to local, backup + update if different or missing.
        """
        raw_url = urljoin(self.raw_base, path)
        ll.debug(f"🔍 Checking: {path}")
        
        try:
            r = self.session.get(raw_url)
            
            # Rate-limit detection
            if r.status_code == 403 and 'X-RateLimit-Remaining' in r.headers and r.headers['X-RateLimit-Remaining'] == '0':
                ll.warn("❌ Rate limit exceeded during file fetch. Can't continue updates.")
                self.show_rate_limit_alert()
                return False
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            ll.error(f"❌ Error fetching {path}: {e}")
            return False

        remote_content = r.text
        local_path = os.path.join(self.local_dir, path.replace('/', os.sep))

        # Determine if update needed
        needs_update = True
        if os.path.isfile(local_path):
            try:
                with open(local_path, 'r', encoding='utf-8') as f:
                    local_content = f.read()
                needs_update = (local_content != remote_content)
            except Exception as e:
                ll.error(f"❌ Error reading local file {local_path}: {e}")
                needs_update = True  # If we can't read it, assume it needs updating

        if needs_update:
            # Create backup path preserving directory structure
            backup_path = os.path.join(self.backup_dir, path.replace('/', os.sep))
            backup_dir = os.path.dirname(backup_path)
            
            # Create backup directory structure if it doesn't exist
            if not os.path.exists(backup_dir):
                os.makedirs(backup_dir, exist_ok=True)
            
            # Backup existing file if it exists
            if os.path.isfile(local_path):
                try:
                    # Add timestamp to backup filename to avoid conflicts
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    backup_name = f"{os.path.splitext(os.path.basename(backup_path))[0]}_{timestamp}.py"
                    backup_path = os.path.join(backup_dir, backup_name)
                    shutil.copy2(local_path, backup_path)
                    ll.debug(f"💾 Backed up old {path} to {backup_path}")
                except Exception as e:
                    ll.error(f"❌ Error backing up {path}: {e}")

            # Create local directory structure if it doesn't exist
            local_dir = os.path.dirname(local_path)
            if not os.path.exists(local_dir):
                os.makedirs(local_dir, exist_ok=True)

            # Write new file
            try:
                with open(local_path, 'w', encoding='utf-8') as f:
                    f.write(remote_content)
                ll.debug(f"✅ Updated {path}")
                self.files_updated.append(path)
                return True
            except Exception as e:
                ll.error(f"❌ Error writing {local_path}: {e}")
                return False
        else:
            ll.debug(f"⚪ {path} is up-to-date.")
            return False

    def update(self):
        """
        For each .py in repo, checks and updates if needed.
        Then restarts if any updates occurred.
        """
        ll.debug(f"🚀 Starting update check for {self.repo_url}")
        
        py_files = self.list_py_files()
        if not py_files:
            ll.warn("❌ No Python files found in the repo!")
            return

        ll.debug(f"📋 Found {len(py_files)} Python files to check")
        
        # Reset the updated files list
        self.files_updated = []
        
        for path in py_files:
            self.fetch_and_update(path)

        if self.files_updated:
            ll.debug(f"♻️ {len(self.files_updated)} files updated:")
            for file in self.files_updated:
                ll.debug(f"   - {file}")
            ll.warn("Restarting script...")
            os.execv(sys.executable, ['python'] + sys.argv)
        else:
            ll.print("✅ All files are current. No restart needed.")


if __name__ == "__main__":
    AutoUpdater("https://github.com/The-Autonomous/MusicApp", branch="main").update()
    ll.debug("Code Completed")