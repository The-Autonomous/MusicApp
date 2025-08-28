import sys, threading, os, colorama
from collections import deque

# Initialize colorama to enable ANSI escape sequences on Windows terminals
colorama.init()

class OutputRedirector:
    """
    Redirects sys.stdout and sys.stderr to an internal buffer and a log file
    in a separate, non-blocking thread.
    
    This design is complex but offers granular control over logging behavior.
    """
    filename = '.logging.txt'
    
    def __init__(self, enable_dual_logging=False, buffer_size=64*1024, flush_interval=10, max_file_size=1024*1024):
        """
        Initializes the output redirection system.

        Args:
            enable_dual_logging (bool): If True, output is also written to the original
                                        stdout/stderr in real-time.
            buffer_size (int): The size in bytes of the buffer before a flush is triggered.
            flush_interval (int): The maximum time in seconds between flushes.
            max_file_size (int): The size in bytes at which the log file will be rotated.
        """
        self.buffer_size = buffer_size
        self.flush_interval = flush_interval
        self.max_file_size = max_file_size
        self.dual_log = enable_dual_logging
        
        # Ensure the log file directory exists
        os.makedirs(os.path.dirname(os.path.abspath(self.filename)), exist_ok=True)
        
        # Clear the log file on startup to avoid appending to old data
        try:
            with open(self.filename, 'w', encoding='utf-8') as f:
                f.write('')
        except Exception as e:
            sys.__stderr__.write(f"Warning: Could not clear log file on startup: {e}\n")
        
        # The main buffer for incoming writes
        self.buffer = deque()
        self.buffer_length = 0
        
        # A threading.Condition object for signaling between threads
        self.buffer_condition = threading.Condition()
        
        # Control flags
        self.shutdown = False
        
        # Store original streams to restore them later
        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr
        
        # Background flush thread handles writing to the file
        self.flush_thread = threading.Thread(target=self._background_flush, daemon=True)
        self.flush_thread.start()
        
        # Redirect standard streams
        sys.stdout = self
        sys.stderr = self
        
    def write(self, text):
        """
        Handles incoming text from print statements. Appends it to the buffer.
        """
        if not text or self.shutdown:
            return len(text) if text else 0

        # Dual log: write to original output as well
        if self.dual_log:
            try:
                # Use a raw write to avoid infinite recursion
                self.original_stdout.write(text)
                self.original_stdout.flush()
            except Exception:
                pass

        # Use the condition variable to manage access to the buffer
        with self.buffer_condition:
            self.buffer.append(text)
            self.buffer_length += len(text)
            
            # If the buffer is full, wake the background thread to flush
            if self.buffer_length >= self.buffer_size:
                self.buffer_condition.notify()

        return len(text)
    
    def flush(self):
        """
        Requests an immediate buffer flush. Wakes the background thread.
        """
        with self.buffer_condition:
            if self.buffer_length > 0:
                self.buffer_condition.notify()
    
    def _rotate_file(self):
        """
        Rotates the log file when it exceeds the maximum size.
        """
        if os.path.exists(self.filename):
            file_size = os.path.getsize(self.filename)
            if file_size > self.max_file_size:
                # Close the current file handle before renaming
                self.file.close()
                os.rename(self.filename, f"{self.filename}.1")
                # Re-open the file to continue writing to the new empty log
                self.file = open(self.filename, 'a', encoding='utf-8')
    
    def _background_flush(self):
        """
        Background thread for writing buffer content to the log file.
        """
        self.file = None
        
        try:
            # Open the file once and keep the handle open
            self.file = open(self.filename, 'a', encoding='utf-8', buffering=8192)
            
            while not self.shutdown:
                # Wait for a notification or timeout
                with self.buffer_condition:
                    self.buffer_condition.wait(timeout=self.flush_interval)
                    
                    # Swap the current buffer to a local buffer for writing
                    local_buffer = self.buffer
                    self.buffer = deque()
                    self.buffer_length = 0

                # Write the local buffer content to the file
                if local_buffer:
                    for chunk in local_buffer:
                        self.file.write(chunk)
                    self.file.flush()
                
                # Check file size and rotate if needed
                self._rotate_file()
                
        except (OSError, IOError) as e:
            sys.__stderr__.write(f"Warning: Error writing to log file: {e}\n")
        finally:
            # Ensure the file is closed on shutdown or error
            if self.file:
                try:
                    self.file.close()
                except Exception:
                    pass
    
    def restore(self):
        """
        Restores original streams and cleans up resources. This is CRITICAL
        to call before the application exits.
        """
        if self.shutdown:
            return
        
        self.shutdown = True
        
        # Wake the flushing thread to perform a final flush
        with self.buffer_condition:
            self.buffer_condition.notify()
        
        # Wait for the flush thread to finish its work
        self.flush_thread.join(timeout=2.0)
        
        # Restore the original streams
        sys.stdout = self.original_stdout
        sys.stderr = self.original_stderr
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.restore()

class log_loader:
    """
    A simple wrapper for formatted and colored console and file output.
    """
    def __init__(self, proj_name="", custom_colors=None, debugging=True):
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