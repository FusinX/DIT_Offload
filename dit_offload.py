# (updated for display scaling)
import customtkinter as ctk
from tkinter import filedialog, messagebox
import tkinter as tk
import subprocess
import threading
import shutil
import os
import json
import logging
from datetime import datetime
from pathlib import Path
import re
import sys
import platform
import ctypes
import signal
import time

# ==================== LOGGER ====================
class DITLogger:
    def __init__(self, log_dir="./dit_logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = self.log_dir / f"transfer_{timestamp}.log"
        
        logging.basicConfig(
            filename=str(self.log_file),
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        self.logger = logging.getLogger()
    
    def info(self, msg):
        self.logger.info(msg)
    
    def error(self, msg):
        self.logger.error(msg)
    
    def success(self, msg):
        self.logger.info(f"SUCCESS: {msg}")
    
    def warning(self, msg):
        self.logger.warning(msg)

# ==================== CONFIG MANAGER ====================
class ConfigManager:
    CONFIG_FILE = "dit_config.json"
    
    @staticmethod
    def load():
        if os.path.exists(ConfigManager.CONFIG_FILE):
            try:
                with open(ConfigManager.CONFIG_FILE, 'r') as f:
                    return json.load(f)
            except Exception as e:
                print(f"Error loading config: {e}")
                return {}
        return {}
    
    @staticmethod
    def save(config):
        try:
            with open(ConfigManager.CONFIG_FILE, 'w') as f:
                json.dump(config, f, indent=2)
        except Exception as e:
            print(f"Error saving config: {e}")

# ==================== TRANSFER ENGINE ====================
class PauseRequested(Exception):
    pass

class AbortRequested(Exception):
    pass

class TransferEngine:
    def __init__(self, logger, ui_callback):
        self.logger = logger
        self.ui_callback = ui_callback
        self.process = None
        self.stopped = False
        self.paused = False
        self.aborted = False
        
    def preflight_check(self, src, dst):
        """Verify paths and disk space before transfer"""
        if not os.path.exists(src):
            raise ValueError(f"Source path does not exist: {src}")
        if not os.path.isdir(src):
            raise ValueError(f"Source is not a directory: {src}")
        if not os.listdir(src):
            raise ValueError("Source directory is empty")
        
        if not os.path.exists(dst):
            raise ValueError(f"Destination path does not exist: {dst}")
        if not os.path.isdir(dst):
            raise ValueError(f"Destination is not a directory: {dst}")
        
        src_size = 0
        file_count = 0
        for root, dirs, files in os.walk(src):
            for file in files:
                try:
                    file_path = os.path.join(root, file)
                    src_size += os.path.getsize(file_path)
                    file_count += 1
                except OSError:
                    pass
        
        if file_count == 0:
            raise ValueError("No files found in source directory")
        
        dst_stats = shutil.disk_usage(dst)
        dst_free = dst_stats.free
        
        if src_size > dst_free * 0.95:
            raise ValueError(
                f"Insufficient space.\n"
                f"Required: {src_size/1e9:.2f} GB\n"
                f"Available: {dst_free/1e9:.2f} GB"
            )
        
        self.logger.info(f"Preflight check passed: {file_count} files, {src_size/1e9:.2f} GB")
        return src_size, file_count
    
    def parse_rclone_progress(self, line):
        """Extract progress info from rclone output"""
        # First, handle the aggregated "Transferred:" line which usually contains
        # overall percentage, speed, and ETA separated by commas.
        if "Transferred:" in line:
            try:
                # Split on commas and trim
                parts = [p.strip() for p in line.split(',')]
                percentage = None
                speed = None
                eta = None
                
                for p in parts:
                    # Percentage field usually looks like "61%"
                    m_pct = re.search(r'(\d{1,3})\%', p)
                    if m_pct and percentage is None:
                        try:
                            percentage = int(m_pct.group(1))
                        except Exception:
                            percentage = 0
                        continue
                    
                    # Speed usually contains "/s"
                    if '/s' in p and speed is None:
                        speed = p
                        continue
                    
                    # ETA may be prefixed with "ETA" or be a time-like token
                    if p.upper().startswith('ETA') or re.match(r'^\d{1,2}:\d{2}(:\d{2})?$', p):
                        # normalize "ETA 00:12:34" or "00:12:34"
                        m_eta = re.search(r'ETA\s*[:\-]?\s*(\S+)', p, re.IGNORECASE)
                        if m_eta:
                            eta = m_eta.group(1)
                        else:
                            eta = p
                        continue
                
                # Fallback defaults
                if percentage is None:
                    percentage = 0
                if speed is None:
                    speed = "0 B/s"
                if eta is None:
                    eta = "---"
                
                # Normalize speed string a bit (optional)
                speed = speed.replace("MBytes", "MB").replace("KBytes", "KB").replace("GBytes", "GB")
                
                return ("progress", percentage, speed, eta)
            except Exception as e:
                self.logger.error(f"Error parsing progress line '{line}': {e}")
                return None
        
        # Next, attempt to detect per-file progress lines.
        # Examples:
        #   path/to/file.mp4:   12% /123M, 1.234M/s, 00:02:34
        #   2021/.. INFO  : path/to/file.mp4: Copied (new)
        # We'll try several patterns and take the best match.
        try:
            # 1) If there's a file progress with a percent (e.g. "file.mp4: 12%")
            m = re.search(r'(?P<path>.+?):\s*\d{1,3}%', line)
            if m:
                path = m.group('path').strip()
                # path may include log prefix; choose last path-like segment
                # find any path-like tokens with extension before a colon
                file_matches = re.findall(r'([^\s:][^:]*\.[A-Za-z0-9]{1,5})', path)
                if file_matches:
                    filename = os.path.basename(file_matches[-1])
                else:
                    # fallback to basename of the captured path
                    filename = os.path.basename(path)
                if filename and len(filename) > 0:
                    return ("file", filename)
            
            # 2) Lines that mention "Copied" or similar often contain filename before the last colon.
            #    We'll look for something that resembles a filename with an extension followed by a colon.
            file_matches = re.findall(r'([^\s:][^:]*\.[A-Za-z0-9]{1,5})\:', line)
            if file_matches:
                filename = os.path.basename(file_matches[-1])
                if filename and len(filename) > 0:
                    return ("file", filename)
            
            # 3) As a last resort, if the line contains a slash and ends with a filename-like token, grab that.
            if ("/" in line or "\\" in line) and ":" in line:
                # Take segment before the final ':' and try to get basename
                try:
                    before_colon = line.rsplit(':', 1)[0]
                    # find last token that looks like a filename
                    toks = re.split(r'\s+', before_colon.strip())
                    for tok in reversed(toks):
                        if '.' in tok:
                            filename = os.path.basename(tok.strip())
                            if filename:
                                return ("file", filename)
                except Exception:
                    pass
        except Exception as e:
            self.logger.error(f"Error extracting file from line '{line}': {e}")
        
        return None
    
    def run_rclone_copy(self, src, dst, transfers=4):
        """Execute rclone copy with real-time progress"""
        self.stopped = False
        self.paused = False  # each run starts as not paused (pausing is external)
        self.aborted = False
        cmd = [
            "rclone", "copy", src, dst,
            "--checksum",
            "--transfers", str(transfers),
            "--progress",
            "--stats", "1s",
            "-v"
        ]
        
        self.logger.info(f"Starting rclone: {' '.join(cmd)}")
        self.ui_callback("log", "Starting file transfer with rclone...", "INFO")
        
        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )
            
            for line in self.process.stdout:
                # If an abort or pause was requested externally, break and let the caller handle
                if self.aborted or self.paused or self.stopped:
                    break
                
                line = line.strip()
                if line:
                    progress_data = self.parse_rclone_progress(line)
                    if progress_data:
                        if progress_data[0] == "file":
                            self.ui_callback("current_file", progress_data[1])
                        elif progress_data[0] == "progress":
                            _, percentage, speed, eta = progress_data
                            self.ui_callback("progress", percentage, speed, eta)
            
            # Make sure process has ended
            try:
                self.process.wait(timeout=5)
            except Exception:
                # if it didn't exit gracefully, try to terminate
                try:
                    self.process.terminate()
                    self.process.wait(timeout=5)
                except Exception:
                    try:
                        self.process.kill()
                    except Exception:
                        pass
            
            # After process ends, handle pause/abort states
            if self.aborted:
                self.logger.warning("Rclone aborted by user")
                raise AbortRequested("Transfer aborted by user")
            if self.paused:
                self.logger.info("Rclone paused by user")
                raise PauseRequested("Transfer paused by user")
            
            # normal completion
            return self.process.returncode
            
        except PauseRequested:
            raise
        except AbortRequested:
            raise
        except Exception as e:
            self.logger.error(f"Rclone execution error: {e}")
            raise
    
    def verify_transfer(self, src, dst):
        """Independent verification using rclone check"""
        self.ui_callback("log", "Running independent verification pass...", "INFO")
        self.logger.info("Starting verification with rclone check")
        
        cmd = ["rclone", "check", src, dst, "--checksum", "-v"]
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3600
            )
            
            if result.returncode != 0:
                self.logger.error(f"Verification failed:\n{result.stderr}")
                raise ValueError("Verification failed: Files do not match")
            
            self.ui_callback("log", "Verification passed - all checksums match", "SUCCESS")
            self.logger.success("Verification passed")
            return True
            
        except subprocess.TimeoutExpired:
            raise ValueError("Verification timed out")
        except Exception as e:
            self.logger.error(f"Verification error: {e}")
            raise
    
    def create_mhl(self, dst):
        """Generate ASC-MHL manifest"""
        self.ui_callback("log", "Generating ASC-MHL manifest...", "INFO")
        self.logger.info("Creating MHL manifest")
        
        if not shutil.which("ascmhl"):
            raise FileNotFoundError("ascmhl not found in PATH. Please install ASC-MHL tools.")
        
        try:
            result = subprocess.run(
                ["ascmhl", "create", dst],
                capture_output=True,
                text=True,
                timeout=3600
            )
            
            if result.returncode != 0:
                raise ValueError(f"MHL creation failed: {result.stderr}")
            
            self.ui_callback("log", "MHL manifest created successfully", "SUCCESS")
            self.logger.success("MHL manifest created")
            
        except subprocess.TimeoutExpired:
            raise ValueError("MHL creation timed out")
        except Exception as e:
            self.logger.error(f"MHL creation error: {e}")
            raise
    
    def verify_mhl(self, dst):
        """Verify ASC-MHL manifest"""
        self.ui_callback("log", "Verifying MHL manifest integrity...", "INFO")
        self.logger.info("Verifying MHL manifest")
        
        try:
            result = subprocess.run(
                ["ascmhl", "verify", dst],
                capture_output=True,
                text=True,
                timeout=3600
            )
            
            if result.returncode != 0:
                raise ValueError(f"MHL verification failed: {result.stderr}")
            
            self.ui_callback("log", "MHL verification passed", "SUCCESS")
            self.logger.success("MHL verification passed")
            
        except subprocess.TimeoutExpired:
            raise ValueError("MHL verification timed out")
        except Exception as e:
            self.logger.error(f"MHL verification error: {e}")
            raise
    
    def stop(self):
        """Stop current transfer (graceful stop). Kept for backward compatibility."""
        self.stopped = True
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.ui_callback("log", "Transfer stopped by user", "WARNING")
            self.logger.warning("Transfer stopped by user")

    def pause(self):
        """Request a pause: terminate running rclone; the run method will raise PauseRequested."""
        self.paused = True
        if self.process and self.process.poll() is None:
            try:
                # try to terminate gracefully
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
        self.ui_callback("log", "Pause requested - transfer will pause shortly", "INFO")
        self.logger.info("Pause requested by user")

    def resume(self):
        """Clear paused flag. The caller should restart the copy operation in a new thread."""
        self.paused = False
        self.ui_callback("log", "Resuming transfer...", "INFO")
        self.logger.info("Resume requested by user")

    def abort(self):
        """Request an abort: attempt to terminate process and signal abortion."""
        self.aborted = True
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
        self.ui_callback("log", "Abort requested - transfer will stop", "WARNING")
        self.logger.warning("Abort requested by user")

# ==================== MAIN APPLICATION ====================
class ProfessionalDITApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        
        self.logger = DITLogger()
        self.config = ConfigManager.load()
        # read scaling from config (default 1.0)
        try:
            self.scale = float(self.config.get("scaling", 1.0))
        except Exception:
            self.scale = 1.0
        # clamp scale to reasonable range
        self.scale = max(0.5, min(self.scale, 3.0))
        
        self.engine = None
        self.transfer_thread = None
        self.is_transferring = False

        # track paused state at UI level
        self.is_paused = False

        # store current transfer args for resume
        self.current_transfer_args = None

        # progress animation state (for 0-100 steps)
        self._current_percentage = 0
        self._target_percentage = 0
        self._progress_animating = False
        
        self.apply_scaling()
        self.check_dependencies()
        
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        
        self.title("DIT Pro v2.0 | Secure Media Transfer")
        # scale geometry
        base_w, base_h = 1100, 850
        w = int(base_w * self.scale)
        h = int(base_h * self.scale)
        self.geometry(f"{w}x{h}")
        self.resizable(False, False)
        
        self.setup_ui()
        self.load_saved_config()
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        self.logger.info("="*60)
        self.logger.info("DIT Pro v2.0 Started")
        self.logger.info("="*60)
    
    def apply_scaling(self):
        """Apply DPI / Tk scaling and attempt to use customtkinter widget scaling."""
        # Set Windows DPI awareness where possible
        try:
            if platform.system() == "Windows":
                # Try SetProcessDpiAwarenessContext (Windows 10+)
                try:
                    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
                    ctypes.windll.user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
                except Exception:
                    try:
                        ctypes.windll.user32.SetProcessDPIAware()
                    except Exception:
                        pass
        except Exception:
            pass
        
        # set tkinter scaling (affects fonts and many widget sizes)
        try:
            # tk scaling is typically 1.0 = 72 dpi baseline, so use our scale directly
            self.tk.call('tk', 'scaling', self.scale)
        except Exception:
            pass
        
        # If customtkinter provides a widget scaling helper, try to call it (best-effort)
        try:
            if hasattr(ctk, "set_widget_scaling"):
                ctk.set_widget_scaling(self.scale)
        except Exception:
            pass
    
    def s(self, val):
        """Scale integer sizes (heights, widths, paddings)."""
        return max(1, int(round(val * self.scale)))
    
    def sf(self, val):
        """Scale font sizes."""
        return max(1, int(round(val * self.scale)))
    
    def check_dependencies(self):
        missing = []
        if not shutil.which("rclone"):
            missing.append("rclone")
        if not shutil.which("ascmhl"):
            missing.append("ascmhl")
        
        if missing:
            msg = (
                f"Missing required dependencies:\n\n"
                f"{chr(10).join('• ' + tool for tool in missing)}\n\n"
                f"Please install these tools before using DIT Pro.\n\n"
                f"Installation:\n"
                f"• rclone: https://rclone.org/downloads/\n"
                f"• ascmhl: https://github.com/ascmitc/mhl"
            )
            messagebox.showwarning("Missing Dependencies", msg)
    
    def setup_ui(self):
        # Header
        header = ctk.CTkFrame(self, height=self.s(80), fg_color="#1a1a1a")
        header.pack(fill="x", pady=(0, self.s(20)))
        
        title = ctk.CTkLabel(header, text="DIT PRO", font=("Helvetica", self.sf(32), "bold"), text_color="#00d4ff")
        title.pack(side="left", padx=self.s(20), pady=self.s(20))
        
        subtitle = ctk.CTkLabel(header, text="Professional Rclone + ASC-MHL Workflow", font=("Helvetica", self.sf(12)), text_color="#888")
        subtitle.pack(side="left", padx=(0, self.s(20)))
        
        version = ctk.CTkLabel(header, text="v2.0", font=("Courier", self.sf(10)), text_color="#444")
        version.pack(side="right", padx=self.s(20))
        
        # Main container
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.pack(fill="both", expand=True, padx=self.s(30))
        
        # Left panel
        left_panel = ctk.CTkFrame(main, width=self.s(480), fg_color="#242424")
        left_panel.pack(side="left", fill="both", padx=(0, self.s(15)))
        
        # Source section
        ctk.CTkLabel(left_panel, text="SOURCE MEDIA", font=("Helvetica", self.sf(14), "bold")).pack(pady=(self.s(20), self.s(5)))
        self.src_display = ctk.CTkTextbox(left_panel, height=self.s(60), fg_color="#1a1a1a", font=("Courier", self.sf(10)), wrap="word")
        self.src_display.pack(padx=self.s(20), pady=self.s(5), fill="x")
        self.src_display.insert("1.0", "No source selected")
        self.src_display.configure(state="disabled")
        
        ctk.CTkButton(left_panel, text="Browse Source", command=lambda: self.browse("src"), height=self.s(35),
                     fg_color="#2d5a7b", hover_color="#3a7099").pack(pady=self.s(5))
        
        self.src_info = ctk.CTkLabel(left_panel, text="0 files | 0 GB", text_color="#666", font=("Courier", self.sf(10)))
        self.src_info.pack(pady=self.s(5))
        
        # Destination 1
        ctk.CTkLabel(left_panel, text="DESTINATION 1 (Primary)", font=("Helvetica", self.sf(14), "bold")).pack(pady=(self.s(30), self.s(5)))
        self.dst1_display = ctk.CTkTextbox(left_panel, height=self.s(60), fg_color="#1a1a1a", font=("Courier", self.sf(10)), wrap="word")
        self.dst1_display.pack(padx=self.s(20), pady=self.s(5), fill="x")
        self.dst1_display.insert("1.0", "No destination selected")
        self.dst1_display.configure(state="disabled")
        
        ctk.CTkButton(left_panel, text="Browse Destination 1", command=lambda: self.browse("dst1"), height=self.s(35),
                     fg_color="#2d5a7b", hover_color="#3a7099").pack(pady=self.s(5))
        
        # Destination 2
        ctk.CTkLabel(left_panel, text="DESTINATION 2 (Backup)", font=("Helvetica", self.sf(14), "bold")).pack(pady=(self.s(30), self.s(5)))
        self.dst2_display = ctk.CTkTextbox(left_panel, height=self.s(60), fg_color="#1a1a1a", font=("Courier", self.sf(10)), wrap="word")
        self.dst2_display.pack(padx=self.s(20), pady=self.s(5), fill="x")
        self.dst2_display.insert("1.0", "No destination selected")
        self.dst2_display.configure(state="disabled")
        
        ctk.CTkButton(left_panel, text="Browse Destination 2", command=lambda: self.browse("dst2"), height=self.s(35),
                     fg_color="#2d5a7b", hover_color="#3a7099").pack(pady=self.s(5))
        
        # Transfer options
        ctk.CTkLabel(left_panel, text="TRANSFER OPTIONS", font=("Helvetica", self.sf(14), "bold")).pack(pady=(self.s(30), self.s(5)))
        
        transfers_frame = ctk.CTkFrame(left_panel, fg_color="transparent")
        transfers_frame.pack(padx=self.s(20), pady=self.s(5), fill="x")
        ctk.CTkLabel(transfers_frame, text="Parallel Transfers:").pack(side="left")
        self.transfers_var = tk.StringVar(value="4")
        transfers_spinbox = ctk.CTkOptionMenu(transfers_frame, values=["1", "2", "4", "8", "16"], 
                                            variable=self.transfers_var, width=self.s(60))
        transfers_spinbox.pack(side="right")
        
        # Control buttons
        button_frame = ctk.CTkFrame(left_panel, fg_color="transparent")
        button_frame.pack(pady=self.s(30))
        
        self.start_btn = ctk.CTkButton(button_frame, text="START TRANSFER", command=self.start_transfer,
                                      height=self.s(45), fg_color="#28a745", hover_color="#218838", 
                                      font=("Helvetica", self.sf(14), "bold"))
        self.start_btn.pack(pady=self.s(5), fill="x")
        
        # Pause/Resume button
        self.pause_btn = ctk.CTkButton(button_frame, text="PAUSE", command=self.toggle_pause,
                                      height=self.s(35), fg_color="#ffc107", hover_color="#e0a800",
                                      state="disabled")
        self.pause_btn.pack(pady=self.s(5), fill="x")
        
        # Abort button
        self.abort_btn = ctk.CTkButton(button_frame, text="ABORT", command=self.abort_transfer,
                                      height=self.s(35), fg_color="#dc3545", hover_color="#c82333",
                                      state="disabled")
        self.abort_btn.pack(pady=self.s(5), fill="x")
        
        # Right panel
        right_panel = ctk.CTkFrame(main, fg_color="#242424")
        right_panel.pack(side="right", fill="both", expand=True)
        
        # Progress section
        ctk.CTkLabel(right_panel, text="TRANSFER PROGRESS", font=("Helvetica", self.sf(14), "bold")).pack(pady=(self.s(20), self.s(5)))
        
        self.progress_bar = ctk.CTkProgressBar(right_panel, height=self.s(20))
        self.progress_bar.pack(padx=self.s(20), pady=self.s(10), fill="x")
        self.progress_bar.set(0)
        
        self.progress_label = ctk.CTkLabel(right_panel, text="0%", font=("Helvetica", self.sf(16), "bold"))
        self.progress_label.pack()
        
        # Current file
        ctk.CTkLabel(right_panel, text="CURRENT FILE", font=("Helvetica", self.sf(12), "bold")).pack(pady=(self.s(20), self.s(5)))
        self.current_file_label = ctk.CTkLabel(right_panel, text="Waiting...", text_color="#888", 
                                              font=("Courier", self.sf(10)), wraplength=self.s(400))
        self.current_file_label.pack(pady=self.s(5))
        
        # Stats frame
        stats_frame = ctk.CTkFrame(right_panel, fg_color="transparent")
        stats_frame.pack(pady=self.s(20), fill="x", padx=self.s(20))
        
        ctk.CTkLabel(stats_frame, text="SPEED:").pack(side="left", padx=self.s(5))
        self.speed_label = ctk.CTkLabel(stats_frame, text="0 MB/s", text_color="#00d4ff", font=("Courier", self.sf(12)))
        self.speed_label.pack(side="left", padx=(0, self.s(20)))
        
        ctk.CTkLabel(stats_frame, text="ETA:").pack(side="left", padx=self.s(5))
        self.eta_label = ctk.CTkLabel(stats_frame, text="--:--:--", text_color="#00d4ff", font=("Courier", self.sf(12)))
        self.eta_label.pack(side="left")
        
        # Log display
        ctk.CTkLabel(right_panel, text="TRANSFER LOG", font=("Helvetica", self.sf(14), "bold")).pack(pady=(self.s(20), self.s(5)))
        self.log_display = ctk.CTkTextbox(right_panel, height=self.s(300), fg_color="#1a1a1a", 
                                         font=("Courier", self.sf(10)), wrap="word")
        self.log_display.pack(padx=self.s(20), pady=self.s(10), fill="both", expand=True)
        self.log_display.insert("1.0", "Log initialized. Ready for transfer.\n")
        self.log_display.configure(state="disabled")
        
        # Status bar
        status_bar = ctk.CTkFrame(self, height=self.s(40), fg_color="#1a1a1a")
        status_bar.pack(fill="x", side="bottom", pady=(self.s(10), 0))
        
        self.status_label = ctk.CTkLabel(status_bar, text="Ready", text_color="#00d4ff")
        self.status_label.pack(side="left", padx=self.s(20))
        
        self.log_path_label = ctk.CTkLabel(status_bar, text="", text_color="#666", font=("Courier", self.sf(9)))
        self.log_path_label.pack(side="right", padx=self.s(20))
    
    def browse(self, target):
        """Browse for directory"""
        dir_path = filedialog.askdirectory(title=f"Select {target} directory")
        if dir_path:
            if target == "src":
                self.src_display.configure(state="normal")
                self.src_display.delete("1.0", "end")
                self.src_display.insert("1.0", dir_path)
                self.src_display.configure(state="disabled")
                self.update_source_info(dir_path)
            elif target == "dst1":
                self.dst1_display.configure(state="normal")
                self.dst1_display.delete("1.0", "end")
                self.dst1_display.insert("1.0", dir_path)
                self.dst1_display.configure(state="disabled")
            elif target == "dst2":
                self.dst2_display.configure(state="normal")
                self.dst2_display.delete("1.0", "end")
                self.dst2_display.insert("1.0", dir_path)
                self.dst2_display.configure(state="disabled")
            
            self.save_config()
    
    def update_source_info(self, src_path):
        """Update source file count and size info"""
        try:
            if os.path.exists(src_path) and os.path.isdir(src_path):
                total_size = 0
                file_count = 0
                for root, dirs, files in os.walk(src_path):
                    for file in files:
                        try:
                            file_path = os.path.join(root, file)
                            total_size += os.path.getsize(file_path)
                            file_count += 1
                        except OSError:
                            pass
                
                size_gb = total_size / 1e9
                self.src_info.configure(text=f"{file_count} files | {size_gb:.2f} GB")
        except Exception as e:
            self.src_info.configure(text="Error calculating size")
    
    def ui_callback(self, action, *args):
        """Thread-safe UI updates from transfer engine"""
        def update():
            if action == "log":
                msg, level = args
                color = {
                    "INFO": "#ffffff",
                    "SUCCESS": "#28a745",
                    "WARNING": "#ffc107",
                    "ERROR": "#dc3545"
                }.get(level, "#ffffff")
                
                self.log_display.configure(state="normal")
                self.log_display.insert("end", f"{msg}\n", level)
                self.log_display.see("end")
                self.log_display.configure(state="disabled")
                
                # Tag for coloring
                self.log_display.tag_config(level, foreground=color)
                
            elif action == "progress":
                percentage, speed, eta = args
                # Update speed and ETA immediately
                try:
                    self.speed_label.configure(text=speed)
                    self.eta_label.configure(text=eta)
                except Exception:
                    pass
                # Animate progress bar in single-percentage steps from current to target
                try:
                    target = max(0, min(100, int(percentage)))
                except Exception:
                    target = 0
                self.animate_progress_to(target)
                
            elif action == "current_file":
                filename = args[0]
                self.current_file_label.configure(text=filename)
                
            elif action == "status":
                status = args[0]
                self.status_label.configure(text=status)
        
        self.after(0, update)

    def animate_progress_to(self, target_percentage, step_delay=8):
        """
        Animate the progress bar from the current percentage to the target_percentage
        in single-percentage steps to provide 100 discrete steps (0-100).
        step_delay is in milliseconds per percentage increment (default 8ms).
        """
        # Cancel any existing animation by setting the target; the running animator
        # will pick up the new target on the next tick.
        self._target_percentage = target_percentage

        # If already animating, don't start another loop; the current loop will move toward new target
        if self._progress_animating:
            return

        self._progress_animating = True

        def step():
            # move current one step toward target
            if self._current_percentage < self._target_percentage:
                self._current_percentage += 1
            elif self._current_percentage > self._target_percentage:
                self._current_percentage -= 1

            # update UI
            try:
                self.progress_bar.set(self._current_percentage / 100.0)
                self.progress_label.configure(text=f"{self._current_percentage}%")
            except Exception:
                pass

            if self._current_percentage == self._target_percentage:
                # reached target; stop animation
                self._progress_animating = False
            else:
                # schedule next increment/decrement
                self.after(step_delay, step)

        # kick off animation
        self.after(0, step)
    
    def start_transfer(self):
        """Start the transfer process in a separate thread"""
        if self.is_transferring and not self.is_paused:
            messagebox.showwarning("Warning", "Transfer already in progress")
            return
        
        src = self.src_display.get("1.0", "end-1c").strip()
        dst1 = self.dst1_display.get("1.0", "end-1c").strip()
        
        if not src or src == "No source selected":
            messagebox.showerror("Error", "Please select a source directory")
            return
        
        if not dst1 or dst1 == "No destination selected":
            messagebox.showerror("Error", "Please select at least one destination")
            return
        
        try:
            transfers = int(self.transfers_var.get())
            
            # Create transfer engine
            self.engine = TransferEngine(self.logger, self.ui_callback)
            
            # store current transfer args for resume
            self.current_transfer_args = (src, dst1, transfers)
            
            # Update UI
            self.start_btn.configure(state="disabled")
            self.pause_btn.configure(state="normal", text="PAUSE")
            self.abort_btn.configure(state="normal")
            self.is_transferring = True
            self.is_paused = False
            # reset progress animation variables
            self._current_percentage = 0
            self._target_percentage = 0
            self.progress_bar.set(0)
            self.progress_label.configure(text="0%")
            
            # Start transfer in thread
            self.transfer_thread = threading.Thread(
                target=self.run_transfer,
                args=(src, dst1, transfers, False),  # resume=False
                daemon=True
            )
            self.transfer_thread.start()
            
        except Exception as e:
            messagebox.showerror("Error", f"Failed to start transfer: {str(e)}")
            self.logger.error(f"Start transfer error: {e}")
    
    def run_transfer(self, src, dst, transfers, resume=False):
        """Run the transfer process (called from thread)"""
        try:
            # Preflight check (skip on resume)
            if not resume:
                self.ui_callback("status", "Preflight check...")
                self.engine.preflight_check(src, dst)
            
            # Run rclone copy
            self.ui_callback("status", "Transferring...")
            return_code = self.engine.run_rclone_copy(src, dst, transfers)
            
            if return_code == 0:
                self.ui_callback("status", "Verifying...")
                # Verify transfer
                self.engine.verify_transfer(src, dst)
                
                # Create MHL
                self.ui_callback("status", "Creating MHL...")
                self.engine.create_mhl(dst)
                
                self.ui_callback("log", "Transfer completed successfully!", "SUCCESS")
                self.ui_callback("status", "Complete")
                
                messagebox.showinfo("Success", "Transfer completed successfully!")
            else:
                raise ValueError(f"Rclone exited with code {return_code}")
                
        except PauseRequested as p:
            # Pause was requested: keep transfer state so user may resume
            self.is_paused = True
            self.is_transferring = True  # transfer logically still in-progress but paused
            self.ui_callback("log", str(p), "INFO")
            self.ui_callback("status", "Paused")
            # Update pause button to show Resume
            try:
                self.pause_btn.configure(text="RESUME")
            except Exception:
                pass
            self.logger.info(f"Transfer paused: {p}")
        except AbortRequested as a:
            # Abort requested: stop and reset UI
            self.ui_callback("log", str(a), "WARNING")
            self.ui_callback("status", "Aborted")
            self.logger.warning(f"Transfer aborted: {a}")
            messagebox.showwarning("Aborted", "Transfer was aborted by user")
        except Exception as e:
            error_msg = f"Transfer failed: {str(e)}"
            self.ui_callback("log", error_msg, "ERROR")
            self.ui_callback("status", "Failed")
            messagebox.showerror("Error", error_msg)
            self.logger.error(f"Transfer error: {e}")
        finally:
            # If transfer was paused, do not fully reset UI (allow resume)
            if not self.is_paused:
                self.after(0, self.reset_ui)
    
    def toggle_pause(self):
        """Toggle pause/resume"""
        if not self.engine or not self.is_transferring:
            return
        
        if not self.is_paused:
            # Request pause
            if messagebox.askyesno("Confirm", "Pause the current transfer?"):
                try:
                    self.engine.pause()
                    # The engine.pause() will cause the running transfer thread to raise PauseRequested
                    # UI updates will be handled in run_transfer's exception handler
                except Exception as e:
                    self.ui_callback("log", f"Failed to pause: {e}", "ERROR")
                    self.logger.error(f"Pause error: {e}")
        else:
            # Resume
            try:
                self.engine.resume()
                self.is_paused = False
                self.ui_callback("status", "Resuming...")
                self.pause_btn.configure(text="PAUSE")
                # start a new thread to resume transfer; use stored args
                if self.current_transfer_args:
                    src, dst, transfers = self.current_transfer_args
                    self.transfer_thread = threading.Thread(
                        target=self.run_transfer,
                        args=(src, dst, transfers, True),  # resume=True
                        daemon=True
                    )
                    self.transfer_thread.start()
            except Exception as e:
                self.ui_callback("log", f"Failed to resume: {e}", "ERROR")
                self.logger.error(f"Resume error: {e}")
    
    def abort_transfer(self):
        """Abort the current transfer entirely"""
        if self.engine and self.is_transferring:
            if messagebox.askyesno("Confirm", "Abort the current transfer? This will stop now."):
                try:
                    self.engine.abort()
                    # engine.abort() will cause the running transfer thread to raise AbortRequested
                    # Ensure UI resets after abort
                    self.ui_callback("log", "Transfer aborted by user", "WARNING")
                    self.ui_callback("status", "Aborted")
                except Exception as e:
                    self.ui_callback("log", f"Failed to abort: {e}", "ERROR")
                    self.logger.error(f"Abort error: {e}")
                finally:
                    # reset UI state
                    self.is_paused = False
                    self.current_transfer_args = None
                    self.after(0, self.reset_ui)
    
    def reset_ui(self):
        """Reset UI after transfer completion"""
        self.is_transferring = False
        self.is_paused = False
        self.start_btn.configure(state="normal")
        self.pause_btn.configure(state="disabled", text="PAUSE")
        self.abort_btn.configure(state="disabled")
        # animate back to 0 for clarity
        self.animate_progress_to(0)
        self.current_file_label.configure(text="Waiting...")
        self.speed_label.configure(text="0 MB/s")
        self.eta_label.configure(text="--:--:--")
    
    def load_saved_config(self):
        """Load saved configuration"""
        if self.config:
            src = self.config.get("src", "")
            dst1 = self.config.get("dst1", "")
            dst2 = self.config.get("dst2", "")
            transfers = self.config.get("transfers", "4")
            # update scale from config in case it changed
            try:
                self.scale = float(self.config.get("scaling", self.scale))
            except Exception:
                pass
            self.scale = max(0.5, min(self.scale, 3.0))
            
            if src:
                self.src_display.configure(state="normal")
                self.src_display.delete("1.0", "end")
                self.src_display.insert("1.0", src)
                self.src_display.configure(state="disabled")
                self.update_source_info(src)
            
            if dst1:
                self.dst1_display.configure(state="normal")
                self.dst1_display.delete("1.0", "end")
                self.dst1_display.insert("1.0", dst1)
                self.dst1_display.configure(state="disabled")
            
            if dst2:
                self.dst2_display.configure(state="normal")
                self.dst2_display.delete("1.0", "end")
                self.dst2_display.insert("1.0", dst2)
                self.dst2_display.configure(state="disabled")
            
            self.transfers_var.set(transfers)
    
    def save_config(self):
        """Save current configuration"""
        config = {
            "src": self.src_display.get("1.0", "end-1c").strip() if self.src_display.get("1.0", "end-1c") != "No source selected" else "",
            "dst1": self.dst1_display.get("1.0", "end-1c").strip() if self.dst1_display.get("1.0", "end-1c") != "No destination selected" else "",
            "dst2": self.dst2_display.get("1.0", "end-1c").strip() if self.dst2_display.get("1.0", "end-1c") != "No destination selected" else "",
            "transfers": self.transfers_var.get(),
            "scaling": self.scale
        }
        ConfigManager.save(config)
    
    def on_closing(self):
        """Handle window closing"""
        if self.is_transferring:
            if messagebox.askyesno("Confirm", "Transfer in progress. Close anyway?"):
                if self.engine:
                    # If paused, it's safe to close. If running, attempt to abort
                    try:
                        self.engine.abort()
                    except Exception:
                        pass
                self.destroy()
        else:
            self.destroy()

if __name__ == "__main__":
    app = ProfessionalDITApp()
    app.mainloop()
