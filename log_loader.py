import colorama
import sys
import threading
import time
from collections import deque
import os

class OutputRedirector:
    filename='.logging.txt'
    
    def __init__(self, enable_dual_logging=False, buffer_size=64*1024, flush_interval=10, max_file_size=1024*1024):  # 1MB default
        self.buffer_size = buffer_size
        self.flush_interval = flush_interval
        self.max_file_size = max_file_size
        self.dual_log = enable_dual_logging
        
        # Ensure directory exists
        os.makedirs(os.path.dirname(os.path.abspath(self.filename)), exist_ok=True)
        
        # CLEAR THE LOG FILE ON STARTUP
        try:
            with open(self.filename, 'w', encoding='utf-8') as f:
                f.write('')
        except Exception as e:
            # If for some reason we can't clear it, just let it be.
            print(f"Warning: Could not clear log file on startup: {e}", file=sys.__stderr__)
        
        # Double buffer system
        self.front_buffer = deque()
        self.back_buffer = deque()
        self.buffer_length = 0
        self.buffer_lock = threading.Lock()
        
        # Control flags
        self.shutdown = False
        self.flush_requested = False
        
        # Store original streams
        self.original_stdout = sys.stdout
        self.original_stderr = sys.stderr
        
        # Background flush thread
        self.flush_thread = threading.Thread(target=self._background_flush, daemon=True)
        self.flush_thread.start()
        
        # Redirect streams
        sys.stdout = self
        sys.stderr = self
        
    def write(self, text):
        """Write text to buffer with minimal locking.
        If dual_log is enabled, also write to original stdout/stderr."""
        if not text or self.shutdown:
            return len(text) if text else 0

        # Dual log: write to original output as well
        if self.dual_log:
            try:
                if sys.stdout is self:
                    self.original_stdout.write(text)
                    self.original_stdout.flush()
                if sys.stderr is self:
                    self.original_stderr.write(text)
                    self.original_stderr.flush()
            except Exception:
                pass

        # Lock-free path for common case
        if not self.flush_requested and self.buffer_length < self.buffer_size:
            with self.buffer_lock:
                if not self.flush_requested and self.buffer_length < self.buffer_size:
                    self.front_buffer.append(text)
                    self.buffer_length += len(text)
                    return len(text)

        # Contended path
        with self.buffer_lock:
            if self.shutdown:
                return len(text)
            self.front_buffer.append(text)
            self.buffer_length += len(text)

            # Trigger flush if buffer full
            if self.buffer_length >= self.buffer_size:
                self.flush_requested = True

        return len(text)
    
    def flush(self):
        """Request immediate buffer flush."""
        with self.buffer_lock:
            if not self.flush_requested and self.buffer_length > 0:
                self.flush_requested = True
    
    def _check_and_truncate_file(self):
        """Check file size and truncate if too large."""
        try:
            if os.path.exists(self.filename):
                file_size = os.path.getsize(self.filename)
                if file_size > self.max_file_size:
                    # Read last portion of file (keep last ~75% after truncation)
                    keep_size = int(self.max_file_size * 0.75)
                    
                    with open(self.filename, 'rb') as f:
                        f.seek(-keep_size, 2)  # Seek from end
                        # Skip to next newline to avoid partial lines
                        f.readline()  # Skip potentially partial first line
                        remaining_data = f.read()
                    
                    # Rewrite file with remaining data
                    with open(self.filename, 'wb') as f:
                        f.write(remaining_data)
                        
        except (OSError, IOError):
            # If truncation fails, just continue - not critical
            pass
    
    def _background_flush(self):
        """Background thread handling buffer swapping and file writes."""
        file = None
        last_active = time.monotonic()
        
        while not self.shutdown:
            # Check if we should swap buffers
            with self.buffer_lock:
                should_swap = (
                    self.flush_requested or 
                    (self.buffer_length > 0 and time.monotonic() - last_active > self.flush_interval)
                )
                
                if not should_swap:
                    # Release lock while waiting
                    self.buffer_lock.release()
                    time.sleep(0.01)
                    self.buffer_lock.acquire()
                    continue
                
                # Swap buffers
                self.front_buffer, self.back_buffer = self.back_buffer, self.front_buffer
                buffer_length = self.buffer_length
                self.buffer_length = 0
                self.flush_requested = False
            
            # Write back buffer to file
            if self.back_buffer:
                # Open file if needed
                if file is None:
                    try:
                        file = open(self.filename, 'a', encoding='utf-8', buffering=8192)
                    except OSError as e:
                        print(f"Warning: Could not open log file {self.filename}: {e}", file=sys.__stderr__)
                        file = None
                
                # Write buffer content
                if file:
                    try:
                        while self.back_buffer:
                            chunk = self.back_buffer.popleft()
                            file.write(chunk)
                        file.flush()
                        last_active = time.monotonic()
                        
                        # Check file size periodically and truncate if needed
                        self._check_and_truncate_file()
                        
                    except OSError as e:
                        print(f"Warning: Error writing to log file: {e}", file=sys.__stderr__)
                        try:
                            file.close()
                        except Exception:
                            pass
                        file = None
            
            # Clear back buffer
            self.back_buffer.clear()
        
        # Cleanup on shutdown
        if file:
            try:
                file.close()
            except Exception:
                pass
    
    def restore(self):
        """Restore original streams and clean up resources."""
        if self.shutdown:
            return
        
        # Signal shutdown
        self.shutdown = True
        if self.flush_thread.is_alive():
            self.flush_thread.join(timeout=1.0)  # Add timeout to prevent hanging
        
        # Final flush of remaining data
        with self.buffer_lock:
            remaining = list(self.front_buffer)
            self.front_buffer.clear()
            self.buffer_length = 0
        
        if remaining:
            try:
                with open(self.filename, 'a', encoding='utf-8') as f:
                    for chunk in remaining:
                        f.write(chunk)
            except OSError:
                pass
        
        # Restore original streams
        sys.stdout = self.original_stdout
        sys.stderr = self.original_stderr
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.restore()
    
    def __del__(self):
        self.restore()
        
colorama.init()

class log_loader:
    def __init__(self, proj_name = "", custom_colors = None, debugging = True):
        self.proj_name = proj_name
        self.do_debug = debugging

        self.default_colors = {
            "INFO": "#00FF00",
            "WARN": "#FFFF00",
            "ERROR": "#FF0000",
            "DEBUG": "#999999"
        }

        self.colors = {**self.default_colors, **(custom_colors or {})}

    def _hex_to_rgb(self, hex_color):
        hex_color = hex_color.lstrip('#')
        return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))

    def cprint(self, *values, color="#FFFFFF", **kwargs):
        r, g, b = self._hex_to_rgb(color)
        ansi_color = f"\033[38;2;{r};{g};{b}m"
        reset = "\033[0m"
        # Fixed the join issue - properly handle multiple values
        txt = " ".join(str(v) for v in values)
        print(f"{ansi_color} {txt} {reset}", **kwargs)

    def print(self, *values, **kwargs):
        self.cprint(f"[{self.proj_name}]", *values, color=self.default_colors["INFO"], **kwargs)
        
    def warn(self, *values, **kwargs):
        self.cprint(f"[{self.proj_name}]", *values, color=self.default_colors["WARN"], **kwargs)
        
    def debug(self, *values, **kwargs):
        if not self.do_debug: return
        self.cprint(f"[{self.proj_name}]", *values, color=self.default_colors["DEBUG"], **kwargs)
        
    def error(self, *values, **kwargs):
        self.cprint(f"[{self.proj_name}]", *values, color=self.default_colors["ERROR"], **kwargs)
        
    def alert(self, color_code, *values, **kwargs):
        """Call a custom default color action for printing"""
        color_final = self.default_colors.get(color_code, self.default_colors["INFO"])
        self.cprint(f"[{self.proj_name}]", *values, color=color_final, **kwargs)