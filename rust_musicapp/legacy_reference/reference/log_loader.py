"""Bounded logging with recoverable rotation and a draining shutdown."""
import os
from pathlib import Path
import sys
import threading
from collections import deque

try:
    import colorama
    colorama.init()
except ImportError:
    pass


class OutputRedirector:
    filename = str(Path(__file__).parent / ".logging.txt")

    def __init__(self, enable_dual_logging=False, buffer_size=64 * 1024,
                 flush_interval=10, max_file_size=1024 * 1024):
        self.buffer_size = buffer_size
        self.flush_interval = flush_interval
        self.max_file_size = max_file_size
        self.dual_log = enable_dual_logging
        self.buffer = deque()
        self.buffer_length = 0
        self.dropped_messages = 0
        self.buffer_condition = threading.Condition()
        self.shutdown = False
        self.original_stdout, self.original_stderr = sys.stdout, sys.stderr
        self.file = None
        self.flush_thread = threading.Thread(target=self._background_flush,
                                             daemon=True, name="log-writer")
        self.flush_thread.start()
        sys.stdout = sys.stderr = self

    def write(self, text):
        if not text or self.shutdown:
            return len(text or "")
        if self.dual_log:
            try:
                self.original_stdout.write(text)
            except Exception:
                pass
        with self.buffer_condition:
            if self.buffer_length + len(text) > self.buffer_size * 2:
                self.dropped_messages += 1
                return len(text)
            self.buffer.append(text)
            self.buffer_length += len(text)
            if self.buffer_length >= self.buffer_size:
                self.buffer_condition.notify()
        return len(text)

    def flush(self):
        with self.buffer_condition:
            self.buffer_condition.notify()

    def _rotate_file(self):
        if os.path.isfile(self.filename) and os.path.getsize(self.filename) >= self.max_file_size:
            if self.file:
                self.file.close()
                self.file = None
            os.replace(self.filename, self.filename + ".1")

    def _background_flush(self):
        try:
            while True:
                with self.buffer_condition:
                    if not self.shutdown and self.buffer_length < self.buffer_size:
                        self.buffer_condition.wait(timeout=self.flush_interval)
                    chunks = self.buffer
                    self.buffer = deque()
                    self.buffer_length = 0
                    dropped, self.dropped_messages = self.dropped_messages, 0
                    stopping = self.shutdown
                if chunks or dropped:
                    try:
                        self._rotate_file()
                        if self.file is None:
                            self.file = open(self.filename, "a", encoding="utf-8", buffering=8192)
                        if dropped:
                            self.file.write(f"[Logging] Dropped {dropped} messages to limit memory.\n")
                        self.file.writelines(chunks)
                        self.file.flush()
                    except OSError:
                        if self.file:
                            try:
                                self.file.close()
                            except OSError:
                                pass
                        self.file = None
                if stopping:
                    break
        finally:
            if self.file:
                self.file.close()

    def restore(self):
        if self.shutdown:
            return
        with self.buffer_condition:
            self.shutdown = True
            self.buffer_condition.notify()
        self.flush_thread.join(timeout=2)
        sys.stdout, sys.stderr = self.original_stdout, self.original_stderr

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.restore()

class log_loader:
    """
    A simple wrapper for formatted and colored console and file output.
    """
    def __init__(self, proj_name="", custom_colors=None, debugging=False):
        self.proj_name = proj_name
        self.do_debug = debugging

        self.default_colors = {
            "INFO": "#00FF00",
            "WARN": "#FFFF00",
            "ERROR": "#FF0000",
            "DEBUG": "#999999"
        }

        self.colors = {**self.default_colors, **(custom_colors or {})}
        
        # Pre-calculate ANSI color strings for efficiency
        self._ansi_colors = {
            name: self._hex_to_ansi(hex_color) for name, hex_color in self.colors.items()
        }
        self._ansi_reset = "\033[0m"

    def _hex_to_rgb(self, hex_color):
        hex_color = hex_color.lstrip('#')
        return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
        
    def _hex_to_ansi(self, hex_color):
        r, g, b = self._hex_to_rgb(hex_color)
        return f"\033[38;2;{r};{g};{b}m"

    def cprint(self, *values, color_name="INFO", **kwargs):
        """Prints a message with a custom color based on a color name."""
        ansi_color = self._ansi_colors.get(color_name.upper(), self._ansi_reset)
        txt = " ".join(str(v) for v in values)
        print(f"{ansi_color} {txt} {self._ansi_reset}", **kwargs)

    def print(self, *values, **kwargs):
        """Logs a standard INFO message."""
        self.cprint(f"[{self.proj_name}]", *values, color_name="INFO", **kwargs)
        
    def warn(self, *values, **kwargs):
        """Logs a WARNING message."""
        self.cprint(f"[{self.proj_name}]", *values, color_name="WARN", **kwargs)
        
    def debug(self, *values, **kwargs):
        """Logs a DEBUG message if debugging is enabled."""
        if not self.do_debug:
            return
        self.cprint(f"[{self.proj_name}]", *values, color_name="DEBUG", **kwargs)
        
    def error(self, *values, **kwargs):
        """Logs an ERROR message."""
        self.cprint(f"[{self.proj_name}]", *values, color_name="ERROR", **kwargs)

if __name__ == "__main__":
    with OutputRedirector(enable_dual_logging=True) as log_redirector:
        log_writer = log_loader(proj_name="Log Loader Test", debugging=True)
        log_writer.print("Application started.")
        log_writer.warn("This is a warning message.")
        log_writer.debug("This is a debug message.")
        log_writer.error("An error occurred!")
