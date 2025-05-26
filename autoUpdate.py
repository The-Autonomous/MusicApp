import os
import sys
import requests
from urllib.parse import urljoin
import tkinter as tk
from tkinter import messagebox

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
        resp = self.session.get(url, params=params)

        # Rate-limit detection
        if resp.status_code == 403 and 'X-RateLimit-Remaining' in resp.headers and resp.headers['X-RateLimit-Remaining'] == '0':
            print("❌ Rate limit exceeded. Can't continue updates.")
            self.show_rate_limit_alert()
            sys.exit(1)
        resp.raise_for_status()

        files = []
        for item in resp.json():
            if item['type'] == 'file' and item['name'].endswith('.py'):
                files.append(item['path'])
            elif item['type'] == 'dir':
                files.extend(self.list_py_files(item['path']))
        return files

    def fetch_and_update(self, path):
        """
        Fetch a remote file, compare to local, backup + update if different or missing.
        """
        raw_url = urljoin(self.raw_base, path)
        print(f"🔍 Checking: {path}")
        r = self.session.get(raw_url)

        # Rate-limit detection
        if r.status_code == 403 and 'X-RateLimit-Remaining' in r.headers and r.headers['X-RateLimit-Remaining'] == '0':
            print("❌ Rate limit exceeded during file fetch. Can't continue updates.")
            self.show_rate_limit_alert()
            #sys.exit(1)
        r.raise_for_status()

        remote_content = r.text
        local_path = os.path.join(self.local_dir, path.replace('/', os.sep))

        # Determine if update needed
        needs_update = True
        if os.path.isfile(local_path):
            with open(local_path, 'r', encoding='utf-8') as f:
                local_content = f.read()
            needs_update = (local_content != remote_content)

        if needs_update:
            # Backup existing
            if os.path.isfile(local_path):
                backup_path = os.path.join(self.backup_dir, os.path.basename(path))
                os.replace(local_path, backup_path)
                print(f"💾 Backed up old {path} to {backup_path}")

            # Write new file
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, 'w', encoding='utf-8') as f:
                f.write(remote_content)
            print(f"✅ Updated {path}")
        else:
            print(f"⚪ {path} is up-to-date.")

    def update(self):
        """
        For each .py in repo, checks and updates if needed.
        Then restarts if any updates occurred.
        """
        py_files = self.list_py_files()
        if not py_files:
            print("❌ No Python files found in the repo!")
            return

        updated = False
        for path in py_files:
            # Track prior updated count
            before = os.path.getmtime(self.backup_dir)
            self.fetch_and_update(path)
            # If backup dir timestamp changed, we updated a file
            if os.path.getmtime(self.backup_dir) != before:
                updated = True

        if updated:
            print("♻️ Changes detected; restarting script...")
            os.execv(sys.executable, ['python'] + sys.argv)
        else:
            print("✅ All files are current. No restart needed.")


if __name__ == "__main__":
    updater = AutoUpdater(
        "https://github.com/The-Autonomous/MusicApp",
        branch="main"
    )
    updater.update()
    print("Code Completed")