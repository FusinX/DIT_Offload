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
        
        try:
            dst_stats = shutil.disk_usage(dst)
            dst_free = dst_stats.free
        except Exception as e:
            raise ValueError(f"Unable to determine destination disk usage: {e}")
        
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
                    # Percentage field usually looks like "61%%"
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
        try:
            m = re.search(r'(?P<path>.+?):\s*\d{1,3}%%', line)
            if m:
                path = m.group('path').strip()
                file_matches = re.findall(r'([^\s:][^:]*\.[A-Za-z0-9]{1,5})', path)
                if file_matches:
                    filename = os.path.basename(file_matches[-1])
                else:
                    filename = os.path.basename(path)
                if filename and len(filename) > 0:
                    return ("file", filename)
            
            file_matches = re.findall(r'([^\s:][^:]*\.[A-Za-z0-9]{1,5}):', line)
            if file_matches:
                filename = os.path.basename(file_matches[-1])
                if filename and len(filename) > 0:
                    return ("file", filename)
            
            if ("/" in line or "\\" in line) and ":" in line:
                try:
                    before_colon = line.rsplit(':', 1)[0]
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
                bufsize=1
            )
            
            # Use readline loop for more reliable streaming across platforms
            while True:
                line = self.process.stdout.readline()
                if line == '' and self.process.poll() is not None:
                    break
                if not line:
                    # no data right now, avoid busy-loop
                    time.sleep(0.01)
                    continue
                
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
        
        base_cmd = ["rclone", "check", src, dst, "-v"]
        
        def _run(cmd):
            return subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        
        try:
            # First try checksum-based verification (preferred)
            cmd_checksum = base_cmd + ["--checksum"]
            try:
                result = _run(cmd_checksum)
            except subprocess.TimeoutExpired:
                raise ValueError("Verification timed out")
            
            # If checksum run succeeded, great.
            if result.returncode == 0:
                self.ui_callback("log", "Verification passed - checksums match", "SUCCESS")
                self.logger.success("Verification passed (checksum)")
                return True
            
            # If checksum failed, inspect output to see if checksums/hashes are not available/supported.
            combined_output = (result.stderr or "") + "\n" + (result.stdout or "")
            self.logger.info(f"rclone check (checksum) exit {result.returncode}. Output:\n{combined_output}")
            
            if re.search(r'no .*hash|no .*checksum|hash .*not supported|cannot .*checksum|unable to compute|not supported', combined_output, re.I):
                self.logger.warning("Checksum verification not supported for these remotes/filesystems. Falling back to size/modtime check.")
                self.ui_callback("log", "Checksum verification not available; falling back to size/modtime verification", "WARNING")
                
                try:
                    result2 = _run(base_cmd)
                except subprocess.TimeoutExpired:
                    raise ValueError("Verification timed out")
                
                if result2.returncode == 0:
                    self.ui_callback("log", "Verification passed (size/modtime check)", "SUCCESS")
                    self.logger.success("Verification passed (size/modtime)")
                    return True
                else:
                    combined_output2 = (result2.stderr or "") + "\n" + (result2.stdout or "")
                    self.logger.error(f"rclone check (fallback) exit {result2.returncode}. Output:\n{combined_output2}")
                    raise ValueError("Verification failed: Files do not match")
            else:
                self.logger.error(f"rclone check reported mismatches or error:\n{combined_output}")
                raise ValueError("Verification failed: Files do not match")
            
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
        try:
            self.scale = float(self.config.get("scaling", 1.0))
        except Exception:
            self.scale = 1.0
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
        
        # state lock to avoid races between UI and worker threads
        self._state_lock = threading.Lock()
        
        self.apply_scaling()
        self.check_dependencies()
        
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        
        self.title("DIT Pro v2.0 | Secure Media Transfer")
        base_w, base_h = 1200, 820
        w = int(base_w * self.scale)
        h = int(base_h * self.scale)
        self.geometry(f"{w}x{h}")
        self.resizable(False, False)
        
        # Build a DaVinci Resolve-like UI: left media/destination column, center big job card, right log/inspectors
        self.setup_ui()
        self.load_saved_config()
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        try:
            self.log_path_label.configure(text=str(self.logger.log_file))
        except Exception:
            pass
        
        self.logger.info("="*60)
        self.logger.info("DIT Pro v2.0 Started")
        self.logger.info("="*60)
    
    def apply_scaling(self):
        try:
            if platform.system() == "Windows":
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
        try:
            self.tk.call('tk', 'scaling', self.scale)
        except Exception:
            pass
        try:
            if hasattr(ctk, "set_widget_scaling"):
                ctk.set_widget_scaling(self.scale)
        except Exception:
            pass
    
    def s(self, val):
        return max(1, int(round(val * self.scale)))
    
    def sf(self, val):
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
        """Create a DaVinci Resolve-like UI layout"""  
        # Top toolbar (minimal)
        toolbar = ctk.CTkFrame(self, height=self.s(48), fg_color="#111213")
        toolbar.pack(side="top", fill="x")
        title_lbl = ctk.CTkLabel(toolbar, text="DIT PRO — Proxy Generator Style", font=("Helvetica", self.sf(14), "bold"), text_color="#00d4ff")
        title_lbl.pack(side="left", padx=self.s(12))
        self.status_label = ctk.CTkLabel(toolbar, text="Ready", text_color="#00d4ff")
        self.status_label.pack(side="right", padx=self.s(12))

        # Main area
        main = ctk.CTkFrame(self, fg_color="#0f1112")
        main.pack(fill="both", expand=True, padx=self.s(12), pady=self.s(8))

        # Left: Browser/Media pool (like Resolve media tab)
        left = ctk.CTkFrame(main, width=self.s(300), fg_color="#121314")
        left.pack(side="left", fill="y", padx=(0, self.s(8)), pady=self.s(8))

        ctk.CTkLabel(left, text="MEDIA POOL", font=("Helvetica", self.sf(12), "bold"), text_color="#e6eef6").pack(pady=(self.s(10), 0))
        # Source card
        src_card = ctk.CTkFrame(left, fg_color="#161718", corner_radius=self.s(6))
        src_card.pack(fill="x", padx=self.s(10), pady=self.s(10))
        ctk.CTkLabel(src_card, text="Source", font=("Helvetica", self.sf(11), "bold")).pack(anchor="w", padx=self.s(8), pady=(self.s(8), 0))
        self.src_display = ctk.CTkTextbox(src_card, height=self.s(48), fg_color="#0b0c0d", font=("Courier", self.sf(10)))
        self.src_display.pack(fill="x", padx=self.s(8), pady=self.s(6))
        self.src_display.insert("1.0", "No source selected")
        self.src_display.configure(state="disabled")
        ctk.CTkButton(src_card, text="Browse...", command=lambda: self.browse("src"), width=self.s(120)).pack(padx=self.s(8), pady=(0, self.s(8)))
        self.src_info = ctk.CTkLabel(src_card, text="0 files | 0 GB", text_color="#9aa8b2", font=("Courier", self.sf(10)))
        self.src_info.pack(anchor="w", padx=self.s(8), pady=(0, self.s(8)))

        # Destinations
        dst_card = ctk.CTkFrame(left, fg_color="#161718", corner_radius=self.s(6))
        dst_card.pack(fill="x", padx=self.s(10), pady=self.s(10))
        ctk.CTkLabel(dst_card, text="Destinations", font=("Helvetica", self.sf(11), "bold")).pack(anchor="w", padx=self.s(8), pady=(self.s(8), 0))
        self.dst1_display = ctk.CTkTextbox(dst_card, height=self.s(48), fg_color="#0b0c0d", font=("Courier", self.sf(10)))
        self.dst1_display.pack(fill="x", padx=self.s(8), pady=self.s(6))
        self.dst1_display.insert("1.0", "No destination selected")
        self.dst1_display.configure(state="disabled")
        ctk.CTkButton(dst_card, text="Browse Primary", command=lambda: self.browse("dst1"), width=self.s(120)).pack(side="left", padx=self.s(8), pady=(0, self.s(8)))
        
        self.dst2_display = ctk.CTkTextbox(dst_card, height=self.s(48), fg_color="#0b0c0d", font=("Courier", self.sf(10)))
        self.dst2_display.pack(fill="x", padx=self.s(8), pady=self.s(6))
        self.dst2_display.insert("1.0", "No destination selected")
        self.dst2_display.configure(state="disabled")
        ctk.CTkButton(dst_card, text="Browse Backup", command=lambda: self.browse("dst2"), width=self.s(120)).pack(side="left", padx=self.s(8), pady=(0, self.s(8)))

        # Transfer options in left inspector
        options_card = ctk.CTkFrame(left, fg_color="#161718", corner_radius=self.s(6))
        options_card.pack(fill="x", padx=self.s(10), pady=self.s(10))
        ctk.CTkLabel(options_card, text="Transfer Options", font=("Helvetica", self.sf(11), "bold")).pack(anchor="w", padx=self.s(8), pady=(self.s(8), 0))
        transfers_frame = ctk.CTkFrame(options_card, fg_color="transparent")
        transfers_frame.pack(fill="x", padx=self.s(8), pady=self.s(8))
        ctk.CTkLabel(transfers_frame, text="Parallel Transfers:").pack(side="left")
        self.transfers_var = tk.StringVar(value="4")
        transfers_spinbox = ctk.CTkOptionMenu(transfers_frame, values=["1", "2", "4", "8", "16"], variable=self.transfers_var, width=self.s(80))
        transfers_spinbox.pack(side="right")

        # Control buttons stacked like Resolve render controls
        controls_card = ctk.CTkFrame(left, fg_color="#161718", corner_radius=self.s(6))
        controls_card.pack(fill="x", padx=self.s(10), pady=self.s(10))
        self.start_btn = ctk.CTkButton(controls_card, text="Start", command=self.start_transfer, fg_color="#1db954", hover_color="#14a34c", height=self.s(40), font=("Helvetica", self.sf(12), "bold"))
        self.start_btn.pack(fill="x", padx=self.s(8), pady=(self.s(8), self.s(6)))
        self.pause_btn = ctk.CTkButton(controls_card, text="Pause", command=self.toggle_pause, fg_color="#ffcc00", hover_color="#e0a800", height=self.s(36), state="disabled")
        self.pause_btn.pack(fill="x", padx=self.s(8), pady=self.s(6))
        self.abort_btn = ctk.CTkButton(controls_card, text="Abort", command=self.abort_transfer, fg_color="#e04b4b", hover_color="#c82333", height=self.s(36), state="disabled")
        self.abort_btn.pack(fill="x", padx=self.s(8), pady=(self.s(6), self.s(8)))

        # Center: Job Queue / Big progress
        center = ctk.CTkFrame(main, fg_color="#0f1112")
        center.pack(side="left", fill="both", expand=True, padx=(0, self.s(8)), pady=self.s(8))

        ctk.CTkLabel(center, text="RENDER QUEUE", font=("Helvetica", self.sf(12), "bold"), text_color="#e6eef6").pack(anchor="w", padx=self.s(6), pady=(self.s(8), 0))

        queue_card = ctk.CTkFrame(center, fg_color="#161718", corner_radius=self.s(6))
        queue_card.pack(fill="both", expand=True, padx=self.s(10), pady=self.s(10))

        # Big progress area
        progress_area = ctk.CTkFrame(queue_card, fg_color="#121314", height=self.s(220))
        progress_area.pack(fill="x", padx=self.s(12), pady=self.s(12))
        self.progress_label = ctk.CTkLabel(progress_area, text="0%", font=("Helvetica", self.sf(28), "bold"), text_color="#00d4ff")
        self.progress_label.pack(pady=(self.s(18), 0))
        self.progress_bar = ctk.CTkProgressBar(progress_area, height=self.s(24))
        self.progress_bar.pack(fill="x", padx=self.s(24), pady=self.s(12))
        self.progress_bar.set(0)

        # Current file and small stats under progress
        stats_frame = ctk.CTkFrame(queue_card, fg_color="transparent")
        stats_frame.pack(fill="x", padx=self.s(12), pady=self.s(6))
        ctk.CTkLabel(stats_frame, text="Current File:").pack(side="left")
        self.current_file_label = ctk.CTkLabel(stats_frame, text="Waiting...", text_color="#9aa8b2", font=("Courier", self.sf(10)))
        self.current_file_label.pack(side="left", padx=(self.s(8), self.s(20)))

        ctk.CTkLabel(stats_frame, text="Speed:").pack(side="left")
        self.speed_label = ctk.CTkLabel(stats_frame, text="0 MB/s", text_color="#00d4ff")
        self.speed_label.pack(side="left", padx=(self.s(8), self.s(20)))
        ctk.CTkLabel(stats_frame, text="ETA:").pack(side="left")
        self.eta_label = ctk.CTkLabel(stats_frame, text="--:--:--", text_color="#00d4ff")
        self.eta_label.pack(side="left", padx=self.s(8))

        # A queue listbox to emulate Resolve's render queue
        queue_list_frame = ctk.CTkFrame(queue_card, fg_color="#0b0c0d")
        queue_list_frame.pack(fill="both", expand=True, padx=self.s(12), pady=self.s(10))
        self.queue_list = tk.Listbox(queue_list_frame, bg="#0b0c0d", fg="#e6eef6", bd=0, highlightthickness=0, selectbackground="#1f6f8b")
        self.queue_list.pack(fill="both", expand=True, padx=self.s(6), pady=self.s(6))
        self.queue_list.insert("end", "Ready to start transfer")

        # Right: Log / Inspector
        right = ctk.CTkFrame(main, width=self.s(360), fg_color="#121314")
        right.pack(side="right", fill="y", padx=(self.s(8), 0), pady=self.s(8))

        ctk.CTkLabel(right, text="INSPECTOR", font=("Helvetica", self.sf(12), "bold"), text_color="#e6eef6").pack(pady=(self.s(8), 0))
        log_card = ctk.CTkFrame(right, fg_color="#161718", corner_radius=self.s(6))
        log_card.pack(fill="both", expand=True, padx=self.s(10), pady=self.s(10))

        ctk.CTkLabel(log_card, text="Transfer Log", font=("Helvetica", self.sf(11), "bold")).pack(anchor="w", padx=self.s(8), pady=(self.s(8), 0))
        self.log_display = ctk.CTkTextbox(log_card, height=self.s(220), fg_color="#0b0c0d", font=("Courier", self.sf(10)))
        self.log_display.pack(fill="both", expand=False, padx=self.s(8), pady=self.s(8))
        self.log_display.insert("1.0", "Log initialized. Ready for transfer.\n")
        self.log_display.configure(state="disabled")

        # Footer with log path
        status_bar = ctk.CTkFrame(self, height=self.s(36), fg_color="#0b0c0d")
        status_bar.pack(fill="x", side="bottom")
        self.log_path_label = ctk.CTkLabel(status_bar, text="", text_color="#9aa8b2", font=("Courier", self.sf(9)))
        self.log_path_label.pack(side="right", padx=self.s(12))
    
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
                try:
                    self.log_display.tag_config(level, foreground=color)
                except Exception:
                    pass
                self.log_display.configure(state="normal")
                self.log_display.insert("end", f"{msg}\n", level)
                self.log_display.see("end")
                self.log_display.configure(state="disabled")
                # Also push into queue list for visibility
                try:
                    self.queue_list.insert("end", msg)
                    self.queue_list.see("end")
                except Exception:
                    pass
            elif action == "progress":
                percentage, speed, eta = args
                try:
                    self.speed_label.configure(text=speed)
                    self.eta_label.configure(text=eta)
                except Exception:
                    pass
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
            elif action == "dialog":
                kind, title, message = args
                try:
                    if kind == "info":
                        messagebox.showinfo(title, message)
                    elif kind == "warning":
                        messagebox.showwarning(title, message)
                    elif kind == "error":
                        messagebox.showerror(title, message)
                except Exception:
                    pass
        
        self.after(0, update)

    def animate_progress_to(self, target_percentage, step_delay=8):
        self._target_percentage = target_percentage
        if self._progress_animating:
            return
        self._progress_animating = True
        def step():
            if self._current_percentage < self._target_percentage:
                self._current_percentage += 1
            elif self._current_percentage > self._target_percentage:
                self._current_percentage -= 1
            try:
                self.progress_bar.set(self._current_percentage / 100.0)
                self.progress_label.configure(text=f"{self._current_percentage}%")
            except Exception:
                pass
            if self._current_percentage == self._target_percentage:
                self._progress_animating = False
            else:
                self.after(step_delay, step)
        self.after(0, step)
    
    def start_transfer(self):
        """Start the transfer process in a separate thread"""  
        with self._state_lock:
            if self.is_transferring:
                messagebox.showwarning("Warning", "Transfer already in progress or paused. Resume or abort first.")
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
            self.engine = TransferEngine(self.logger, self.ui_callback)
            with self._state_lock:
                self.current_transfer_args = (src, dst1, transfers)
                self.is_transferring = True
                self.is_paused = False
            self.start_btn.configure(state="disabled")
            self.pause_btn.configure(state="normal", text="PAUSE")
            self.abort_btn.configure(state="normal")
            self._current_percentage = 0
            self._target_percentage = 0
            self.progress_bar.set(0)
            self.progress_label.configure(text="0%")
            self.queue_list.insert("end", f"Queued: {os.path.basename(src)} -> {os.path.basename(dst1)}")
            self.transfer_thread = threading.Thread(target=self.run_transfer, args=(src, dst1, transfers, False), daemon=True)
            self.transfer_thread.start()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to start transfer: {str(e)}")
            self.logger.error(f"Start transfer error: {e}")
            with self._state_lock:
                self.is_transferring = False
                self.current_transfer_args = None
    
    def run_transfer(self, src, dst, transfers, resume=False):
        """Run the transfer process (called from thread)"""
        try:
            if not resume:
                self.ui_callback("status", "Preflight check...")
                self.engine.preflight_check(src, dst)
            self.ui_callback("status", "Transferring...")
            return_code = self.engine.run_rclone_copy(src, dst, transfers)
            if return_code == 0:
                self.ui_callback("status", "Verifying...")
                self.engine.verify_transfer(src, dst)
                self.ui_callback("status", "Creating MHL...")
                self.engine.create_mhl(dst)
                self.ui_callback("log", "Transfer completed successfully!", "SUCCESS")
                self.ui_callback("status", "Complete")
                self.after(0, lambda: messagebox.showinfo("Success", "Transfer completed successfully!"))
            else:
                raise ValueError(f"Rclone exited with code {return_code}")
        except PauseRequested as p:
            with self._state_lock:
                self.is_paused = True
                self.is_transferring = True
            self.ui_callback("log", str(p), "INFO")
            self.ui_callback("status", "Paused")
            try:
                self.after(0, lambda: self.pause_btn.configure(text="RESUME"))
            except Exception:
                pass
            self.logger.info(f"Transfer paused: {p}")
        except AbortRequested as a:
            self.ui_callback("log", str(a), "WARNING")
            self.ui_callback("status", "Aborted")
            self.logger.warning(f"Transfer aborted: {a}")
            self.after(0, lambda: messagebox.showwarning("Aborted", "Transfer was aborted by user"))
            with self._state_lock:
                self.is_transferring = False
                self.is_paused = False
                self.current_transfer_args = None
        except Exception as e:
            error_msg = f"Transfer failed: {str(e)}"
            self.ui_callback("log", error_msg, "ERROR")
            self.ui_callback("status", "Failed")
            self.after(0, lambda: messagebox.showerror("Error", error_msg))
            self.logger.error(f"Transfer error: {e}")
        finally:
            with self._state_lock:
                paused = self.is_paused
            if not paused:
                self.after(0, self.reset_ui)
    
    def toggle_pause(self):
        with self._state_lock:
            engine = self.engine
            is_transferring = self.is_transferring
            is_paused = self.is_paused
        
        if not engine or not is_transferring:
            return
        
        if not is_paused:
            if messagebox.askyesno("Confirm", "Pause the current transfer?"):
                try:
                    engine.pause()
                except Exception as e:
                    self.ui_callback("log", f"Failed to pause: {e}", "ERROR")
                    self.logger.error(f"Pause error: {e}")
        else:
            try:
                engine.resume()
                with self._state_lock:
                    self.is_paused = False
                self.ui_callback("status", "Resuming...")
                self.pause_btn.configure(text="PAUSE")
                with self._state_lock:
                    args = self.current_transfer_args
                if args:
                    src, dst, transfers = args
                    self.transfer_thread = threading.Thread(target=self.run_transfer, args=(src, dst, transfers, True), daemon=True)
                    self.transfer_thread.start()
            except Exception as e:
                self.ui_callback("log", f"Failed to resume: {e}", "ERROR")
                self.logger.error(f"Resume error: {e}")
    
    def abort_transfer(self):
        with self._state_lock:
            engine = self.engine
            is_transferring = self.is_transferring
        
        if engine and is_transferring:
            if messagebox.askyesno("Confirm", "Abort the current transfer? This will stop now."):
                try:
                    engine.abort()
                    self.ui_callback("log", "Transfer aborted by user", "WARNING")
                    self.ui_callback("status", "Aborted")
                except Exception as e:
                    self.ui_callback("log", f"Failed to abort: {e}", "ERROR")
                    self.logger.error(f"Abort error: {e}")
                finally:
                    with self._state_lock:
                        self.is_paused = False
                        self.current_transfer_args = None
                        self.is_transferring = False
                    self.after(0, self.reset_ui)
    
    def reset_ui(self):
        with self._state_lock:
            self.is_transferring = False
            self.is_paused = False
            self.current_transfer_args = None
        self.start_btn.configure(state="normal")
        self.pause_btn.configure(state="disabled", text="PAUSE")
        self.abort_btn.configure(state="disabled")
        self.animate_progress_to(0)
        self.current_file_label.configure(text="Waiting...")
        self.speed_label.configure(text="0 MB/s")
        self.eta_label.configure(text="--:--:--")
    
    def load_saved_config(self):
        if self.config:
            src = self.config.get("src", "")
            dst1 = self.config.get("dst1", "")
            dst2 = self.config.get("dst2", "")
            transfers = self.config.get("transfers", "4")
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
        config = {
            "src": self.src_display.get("1.0", "end-1c").strip() if self.src_display.get("1.0", "end-1c") != "No source selected" else "",
            "dst1": self.dst1_display.get("1.0", "end-1c").strip() if self.dst1_display.get("1.0", "end-1c") != "No destination selected" else "",
            "dst2": self.dst2_display.get("1.0", "end-1c").strip() if self.dst2_display.get("1.0", "end-1c") != "No destination selected" else "",
            "transfers": self.transfers_var.get(),
            "scaling": self.scale
        }
        ConfigManager.save(config)
    
    def on_closing(self):
        with self._state_lock:
            is_transferring = self.is_transferring
            engine = self.engine
        if is_transferring:
            if messagebox.askyesno("Confirm", "Transfer in progress. Close anyway?"):
                if engine:
                    try:
                        engine.abort()
                    except Exception:
                        pass
                self.destroy()
        else:
            self.destroy()

if __name__ == "__main__":
    app = ProfessionalDITApp()
    app.mainloop()