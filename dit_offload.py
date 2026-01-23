#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Professional DIT Transfer Tool (stand-alone Windows executable) - OPTIMIZED VERSION
Build with: python -m PyInstaller --noconsole --onefile --collect-all customtkinter --add-binary "rclone.exe;." professional_dit.py
Optimizations:
- Fixed overlapping progress frames during verification.
- Removed non-functional pause feature to simplify UI and avoid misleading users.
- Allowed independent file size verification without requiring checksum verification.
- Renamed 'scale' to 'chunk_size' in config for clarity.
- Minor code cleanups and consistency improvements.
"""
import os
import sys
import time
import threading
import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox
import customtkinter as ctk
from queue import Queue, Empty
from pathlib import Path
from datetime import datetime
import re
import hashlib
from typing import Dict, List, Tuple, Optional
import concurrent.futures
import json

# ----------------------------------------------------------------------
# Helper – locate a resource that is bundled with the .exe (PyInstaller)
# ----------------------------------------------------------------------
def resource_path(relative_path: str) -> str:
    """
    Return an absolute path for ``relative_path`` relative to the executable.
    Works when the script runs normally and when it has been packaged
    into an EXE by PyInstaller.
    """
    try:
        base_path = sys._MEIPASS
    except AttributeError:
        base_path = os.path.abspath(".")
  
    return os.path.join(base_path, relative_path)

# ----------------------------------------------------------------------
# Very small logger – writes to a file in the user's AppData folder.
# ----------------------------------------------------------------------
class DITLogger:
    """Simple logger that writes to ``%APPDATA%\DIT_Pro_Tool\dit_transfer.log``."""
    def __init__(self, log_dir: str):
        os.makedirs(log_dir, exist_ok=True)
        self._path = Path(log_dir) / "dit_transfer.log"
        self._file = None
        self._open_file()
  
    def _open_file(self):
        """Open log file for appending."""
        try:
            self._file = open(self._path, "a", encoding="utf-8")
        except Exception:
            self._path = Path(os.getcwd()) / "dit_transfer.log"
            self._file = open(self._path, "a", encoding="utf-8")
  
    def _write_log(self, level: str, msg: str):
        """Write a single log entry with automatic recovery."""
        for attempt in range(2):
            try:
                if self._file is None or self._file.closed:
                    self._open_file()
              
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                self._file.write(f"{ts} | {level:8} | {msg}\n")
                self._file.flush()
                break
            except Exception:
                if attempt == 0:
                    try:
                        if self._file and not self._file.closed:
                            self._file.close()
                    except Exception:
                        pass
                    self._file = None
  
    def info(self, msg: str):
        self._write_log("INFO", msg)
  
    def warning(self, msg: str):
        self._write_log("WARNING", msg)
  
    def error(self, msg: str):
        self._write_log("ERROR", msg)
  
    def success(self, msg: str):
        self._write_log("SUCCESS", msg)
  
    def close(self):
        """Close the log file."""
        if self._file and not self._file.closed:
            self._file.close()

# ----------------------------------------------------------------------
# Minimal JSON configuration manager
# ----------------------------------------------------------------------
class ConfigManager:
    """Load / save a tiny JSON config (chunk_size, source/destination)."""
    DEFAULTS = {
        "chunk_size": 10.0,
        "last_source": "",
        "last_destination": "",
        "transfers": 4,
        "verify_checksum": True,
        "checksum_algorithm": "md5",
        "verify_file_size": True
    }
    def __init__(self, cfg_dir: str):
        self.cfg_path = Path(cfg_dir) / "dit_config.json"
        self.data = self.DEFAULTS.copy()
        self.load()
  
    def load(self):
        """Load config from file."""
        try:
            if self.cfg_path.exists():
                with open(self.cfg_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    for key in self.DEFAULTS:
                        if key in loaded:
                            self.data[key] = loaded[key]
        except Exception as e:
            print(f"Failed to load config: {e}")
  
    def save(self):
        """Write current values back to the JSON file."""
        try:
            self.cfg_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cfg_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=4)
        except Exception as e:
            print(f"Failed to save config: {e}")
    @property
    def chunk_size(self) -> float:
        return float(self.data.get("chunk_size", self.DEFAULTS["chunk_size"]))
    @chunk_size.setter
    def chunk_size(self, value: float):
        try:
            self.data["chunk_size"] = float(value)
        except (ValueError, TypeError):
            self.data["chunk_size"] = self.DEFAULTS["chunk_size"]
  
    @property
    def transfers(self) -> int:
        return int(self.data.get("transfers", self.DEFAULTS["transfers"]))
  
    @transfers.setter
    def transfers(self, value: int):
        try:
            self.data["transfers"] = int(value)
        except (ValueError, TypeError):
            self.data["transfers"] = self.DEFAULTS["transfers"]
  
    @property
    def last_source(self) -> str:
        return self.data.get("last_source", "")
  
    @last_source.setter
    def last_source(self, path: str):
        self.data["last_source"] = str(path) if path else ""
  
    @property
    def last_destination(self) -> str:
        return self.data.get("last_destination", "")
  
    @last_destination.setter
    def last_destination(self, path: str):
        self.data["last_destination"] = str(path) if path else ""
  
    @property
    def verify_checksum(self) -> bool:
        return bool(self.data.get("verify_checksum", self.DEFAULTS["verify_checksum"]))
  
    @verify_checksum.setter
    def verify_checksum(self, value: bool):
        self.data["verify_checksum"] = bool(value)
  
    @property
    def checksum_algorithm(self) -> str:
        return str(self.data.get("checksum_algorithm", self.DEFAULTS["checksum_algorithm"]))
  
    @checksum_algorithm.setter
    def checksum_algorithm(self, value: str):
        if value in ["md5", "sha1", "sha256", "sha512"]:
            self.data["checksum_algorithm"] = value
  
    @property
    def verify_file_size(self) -> bool:
        return bool(self.data.get("verify_file_size", self.DEFAULTS["verify_file_size"]))
  
    @verify_file_size.setter
    def verify_file_size(self, value: bool):
        self.data["verify_file_size"] = bool(value)

# ----------------------------------------------------------------------
# Checksum Verification Engine
# ----------------------------------------------------------------------
class ChecksumVerifier:
    """Handles checksum verification after file transfer."""
  
    SUPPORTED_ALGORITHMS = {
        "md5": hashlib.md5,
        "sha1": hashlib.sha1,
        "sha256": hashlib.sha256,
        "sha512": hashlib.sha512
    }
  
    def __init__(self, app_reference, logger: DITLogger):
        self.app = app_reference
        self.logger = logger
        self.running = False
        self.verified_count = 0
        self.failed_count = 0
        self.total_files = 0
        self.current_file = ""
  
    def verify_transfer(self, src_path: str, dst_path: str,
                       algorithm: str = "md5",
                       verify_checksum: bool = True,
                       verify_size: bool = True,
                       max_workers: int = 4) -> Tuple[int, int, List[str]]:
        """Verify that files in destination match source files."""
        self.running = True
        self.verified_count = 0
        self.failed_count = 0
        self.total_files = 0
        self.current_file = ""
      
        try:
            if algorithm not in self.SUPPORTED_ALGORITHMS:
                algorithm = "md5"
            hash_func = self.SUPPORTED_ALGORITHMS[algorithm]
          
            source_files = self._get_file_list(src_path)
            destination_files = self._get_file_list(dst_path)
          
            self.total_files = len(source_files)
            self.app._event_queue.put(("verify_start", self.total_files))
            self.logger.info(f"Starting verification for {self.total_files} files")
            self.logger.info(f"Using algorithm: {algorithm}")
            self.logger.info(f"Verify checksum: {verify_checksum}, Verify size: {verify_size}")
          
            if self.total_files == 0:
                self.logger.warning("No files found for verification")
                self.app._event_queue.put(("verify_complete", 0, 0, []))
                return 0, 0, []
          
            failed_files = []
          
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_file = {}
              
                for src_file in source_files:
                    rel_path = os.path.relpath(src_file, src_path)
                    dst_file = os.path.join(dst_path, rel_path)
                  
                    future = executor.submit(
                        self._verify_file,
                        src_file, dst_file, hash_func, verify_checksum, verify_size
                    )
                    future_to_file[future] = (src_file, dst_file)
              
                for future in concurrent.futures.as_completed(future_to_file):
                    if not self.running:
                        break
                  
                    src_file, dst_file = future_to_file[future]
                    rel_path = os.path.relpath(src_file, src_path)
                    self.current_file = rel_path
                  
                    try:
                        file_size = os.path.getsize(src_file)
                        timeout = max(300, file_size / (10 * 1024 * 1024)) # 10MB/s minimum
                      
                        result = future.result(timeout=timeout)
                        if result["success"]:
                            self.verified_count += 1
                            self.app._event_queue.put(("verify_progress",
                                                      self.verified_count,
                                                      self.failed_count,
                                                      self.total_files,
                                                      self.current_file))
                            self.logger.info(f"Verified: {rel_path}")
                        else:
                            self.failed_count += 1
                            failed_files.append({
                                "file": rel_path,
                                "error": result["error"],
                                "source_hash": result.get("source_hash"),
                                "dest_hash": result.get("dest_hash"),
                                "source_size": result.get("source_size"),
                                "dest_size": result.get("dest_size")
                            })
                            self.app._event_queue.put(("verify_progress",
                                                      self.verified_count,
                                                      self.failed_count,
                                                      self.total_files,
                                                      self.current_file))
                            self.logger.error(f"Verification failed: {rel_path} - {result['error']}")
                    except concurrent.futures.TimeoutError:
                        self.failed_count += 1
                        failed_files.append({
                            "file": rel_path,
                            "error": f"Verification timeout ({timeout:.0f}s)",
                            "source_hash": None,
                            "dest_hash": None
                        })
                        self.logger.error(f"Verification timeout: {rel_path}")
                    except Exception as e:
                        self.failed_count += 1
                        failed_files.append({
                            "file": rel_path,
                            "error": str(e),
                            "source_hash": None,
                            "dest_hash": None
                        })
                        self.logger.error(f"Verification error: {rel_path} - {e}")
          
            self.app._event_queue.put(("verify_complete",
                                      self.verified_count,
                                      self.failed_count,
                                      failed_files))
          
            if self.failed_count == 0:
                self.logger.success(f"All {self.verified_count} files verified successfully!")
            else:
                self.logger.error(f"Verification completed with {self.failed_count} failures out of {self.total_files} files")
          
            return self.verified_count, self.failed_count, failed_files
          
        except Exception as e:
            self.logger.error(f"Verification process failed: {str(e)}")
            self.app._event_queue.put(("verify_error", str(e)))
            return self.verified_count, self.failed_count, []
        finally:
            self.running = False
  
    def _get_file_list(self, path: str) -> List[str]:
        """Get list of all files in a directory recursively."""
        if os.path.isfile(path):
            return [path]
      
        file_list = []
        for root, dirs, files in os.walk(path):
            for file in files:
                file_list.append(os.path.join(root, file))
        return file_list
  
    def _calculate_hash(self, filepath: str, hash_func) -> Optional[str]:
        """Calculate hash of a file."""
        try:
            h = hash_func()
            with open(filepath, 'rb') as f:
                for chunk in iter(lambda: f.read(8192), b''):
                    if not self.running:
                        return None
                    h.update(chunk)
            return h.hexdigest()
        except Exception as e:
            raise Exception(f"Failed to calculate hash: {str(e)}")
  
    def _verify_file(self, src_file: str, dst_file: str, hash_func, verify_checksum: bool, verify_size: bool) -> Dict:
        """Verify a single file."""
        result = {
            "success": False,
            "error": "",
            "source_hash": None,
            "dest_hash": None,
            "source_size": None,
            "dest_size": None
        }
      
        try:
            if not os.path.exists(dst_file):
                result["error"] = f"Destination file not found: {dst_file}"
                return result
          
            src_size = os.path.getsize(src_file)
            dst_size = os.path.getsize(dst_file)
            result["source_size"] = src_size
            result["dest_size"] = dst_size
          
            if verify_size and src_size != dst_size:
                result["error"] = f"File size mismatch: source={src_size}, dest={dst_size}"
                return result
          
            if verify_checksum:
                src_hash = self._calculate_hash(src_file, hash_func)
                dst_hash = self._calculate_hash(dst_file, hash_func)
              
                result["source_hash"] = src_hash
                result["dest_hash"] = dst_hash
              
                if src_hash is None or dst_hash is None:
                    result["error"] = "Failed to calculate hash"
                    return result
              
                if src_hash != dst_hash:
                    result["error"] = f"Hash mismatch: source={src_hash}, dest={dst_hash}"
                    return result
          
            result["success"] = True
            return result
          
        except Exception as e:
            result["error"] = str(e)
            return result
  
    def stop(self):
        """Stop verification process."""
        self.running = False

# ----------------------------------------------------------------------
# Transfer engine – talks to rclone via a subprocess
# ----------------------------------------------------------------------
class TransferEngine:
    """Handles rclone execution and output parsing."""
  
    def __init__(self, app_reference, logger: DITLogger):
        self.app = app_reference
        self.logger = logger
        self.process = None
        self.running = False
  
    def run_transfer(self, src_path: str, dst_path: str, transfers: int, chunk_size: float):
        """Launch rclone transfer."""
        self.running = True
      
        try:
            if not self._pre_flight_check(src_path, dst_path):
                self.logger.error("Pre-flight check failed")
                self.app._event_queue.put(("log", "ERROR", "Pre-flight check failed"))
                self.running = False
                return
          
            rclone_path = resource_path("rclone.exe")
            if not os.path.isfile(rclone_path):
                self.logger.error(f"rclone.exe not found at {rclone_path}")
                self.app._event_queue.put(("log", "ERROR", f"rclone.exe not found at {rclone_path}"))
                self.running = False
                return
          
            cmd = [
                rclone_path,
                "copy",
                src_path,
                dst_path,
                "--progress",
                "--stats-one-line",
                "--stats=1s",  # Added for real-time progress updates every 1 second
                f"--transfers={transfers}",
                f"--buffer-size={chunk_size}Mi",
                "--retries=3",
                "--retries-sleep=5s",
                "--ignore-existing"
            ]
          
            if self.app.cfg_mgr.verify_checksum:
                cmd.append("--checksum")
          
            self.logger.info(f"Starting transfer: {src_path} -> {dst_path}")
            self.logger.info(f"Command: {' '.join(cmd)}")
          
            # Use subprocess.Popen with no console window
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
          
            self._monitor_output()
          
        except Exception as e:
            self.logger.error(f"Transfer failed: {str(e)}")
            self.app._event_queue.put(("log", "ERROR", f"Transfer failed: {str(e)}"))
        finally:
            # Ensure cleanup even on exception
            if self.process and not self.process.poll():
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            self.running = False
            self.app._event_queue.put(("finished",))
  
    def _pre_flight_check(self, src_path: str, dst_path: str) -> bool:
        """Validate source and destination paths."""
        try:
            src = Path(src_path)
            dst = Path(dst_path)
          
            if not src.exists():
                self.app._event_queue.put(("log", "ERROR", f"Source does not exist: {src_path}"))
                return False
          
            if not dst.parent.exists():
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                except Exception as e:
                    self.app._event_queue.put(("log", "ERROR", f"Cannot create destination directory: {e}"))
                    return False
          
            if src.resolve() == dst.resolve():
                self.app._event_queue.put(("log", "ERROR", "Source and destination are the same"))
                return False
          
            return True
          
        except Exception as e:
            self.app._event_queue.put(("log", "ERROR", f"Pre-flight error: {e}"))
            return False
  
    def _monitor_output(self):
        """Read rclone output line by line."""
        while self.running and self.process and self.process.poll() is None:
            if getattr(self.app, "aborted", False):
                self.process.terminate()
                break
          
            line = self.process.stdout.readline()
            if line:
                line = line.strip()
                if line:
                    self._process_rclone_line(line)
          
            time.sleep(0.01)
      
        # Drain remaining output
        if self.process:
            out, _err = self.process.communicate()
            for line in out.split("\n"):
                stripped = line.strip()
                if stripped:
                    self._process_rclone_line(stripped)
  
    def _process_rclone_line(self, line: str):
        """Parse and process a single line of rclone output."""
        # Send raw log to UI
        self.app._event_queue.put(("log", "INFO", line))
      
        progress_match = self._parse_progress(line)
        if progress_match:
            percent, speed, eta = progress_match
            self.app._event_queue.put(("progress", percent, speed, eta))
      
        # Flag error lines for special handling
        if any(error_indicator in line.lower() for error_indicator in
               ["error", "failed", "fatal", "cannot", "unable"]):
            self.app._event_queue.put(("log", "ERROR", line))
  
    def _parse_progress(self, line: str):
        """Parse rclone progress output with robust handling."""
        try:
            percent_match = re.search(r'(\d+)%', line)
            if not percent_match:
                return None
          
            percent = int(percent_match.group(1))
          
            speed_match = re.search(r'([\d.]+)\s*([KMG]?i?B)/s', line)
            speed = speed_match.group(0) if speed_match else "N/A"
          
            eta_match = re.search(r'ETA\s*([\dhms]+)', line)
            eta = eta_match.group(1) if eta_match else "N/A"
          
            return percent, speed, eta
        except Exception:
            # Log parsing error but continue
            self.app._event_queue.put(("log", "DEBUG", f"Failed to parse progress: {line}"))
            return None
  
    def stop(self):
        """Terminate the transfer."""
        if not self.running or not self.process:
            return
        try:
            self.process.terminate()
            # Give a short timeout for graceful shutdown
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()

# ----------------------------------------------------------------------
# Main GUI class
# ----------------------------------------------------------------------
class ProfessionalDITApp(ctk.CTk):
    """Main application window."""
  
    def __init__(self):
        super().__init__()
      
        appdata = os.getenv("APPDATA", os.path.expanduser("~"))
        self.log_dir = os.path.join(appdata, "DIT_Pro_Tool")
        os.makedirs(self.log_dir, exist_ok=True)
      
        # Initialise logger and config manager
        self.logger = DITLogger(self.log_dir)
        self.cfg_mgr = ConfigManager(self.log_dir)
      
        # Queue for background events (thread-safe)
        self._event_queue: Queue = Queue()
        self.aborted = False
        self.is_transferring = False
        self.is_verifying = False
      
        # Engine and verifier instances
        self.engine = None
        self.verifier = None
      
        # Scheduling flag for config save (debounced)
        self._config_save_scheduled: Optional[int] = None
      
        # UI setup
        self._setup_appearance()
        self._create_widgets()
        self._setup_layout()
      
        self._start_event_processor()
      
        self.protocol("WM_DELETE_WINDOW", self.on_close)
    def _setup_appearance(self):
        """Configure the application appearance."""
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
      
        self.title("Professional DIT Transfer Tool v2.0")
        self.geometry("900x750")
        self.minsize(800, 650)
  
    def _create_widgets(self):
        """Create all UI widgets."""
        # ------------------------------------------------- Source ----------
        self.source_frame = ctk.CTkFrame(self)
        self.source_label = ctk.CTkLabel(self.source_frame,
                                         text="Source:",
                                         font=("Arial", 14, "bold"))
        self.source_entry = ctk.CTkEntry(self.source_frame, height=35)
        self.source_browse_btn = ctk.CTkButton(self.source_frame,
                                               text="Browse...",
                                               width=100,
                                               command=self.browse_source)
      
        # ------------------------------------------------- Destination -----
        self.dest_frame = ctk.CTkFrame(self)
        self.dest_label = ctk.CTkLabel(self.dest_frame,
                                       text="Destination:",
                                       font=("Arial", 14, "bold"))
        self.dest_entry = ctk.CTkEntry(self.dest_frame, height=35)
        self.dest_browse_btn = ctk.CTkButton(self.dest_frame,
                                             text="Browse...",
                                             width=100,
                                             command=self.browse_destination)
      
        # ------------------------------------------------- Settings -------
        self.settings_frame = ctk.CTkFrame(self)
        self.chunk_label = ctk.CTkLabel(self.settings_frame,
                                        text="Chunk Size (MiB):")
        self.chunk_entry = ctk.CTkEntry(self.settings_frame, width=100)
        self.chunk_entry.insert(0, str(self.cfg_mgr.chunk_size))
      
        self.transfers_label = ctk.CTkLabel(self.settings_frame,
                                            text="Transfers:",
                                            font=("Arial", 14, "bold"))
        self.transfers_slider = ctk.CTkSlider(self.settings_frame,
                                              from_=1, to=8,
                                              number_of_steps=7,
                                              command=self.on_transfers_change)
        self.transfers_slider.set(self.cfg_mgr.transfers)
        self.transfers_value = ctk.CTkLabel(self.settings_frame,
                                            text=str(self.cfg_mgr.transfers))
      
        # ------------------------------------------------- Verification ----
        self.verify_frame = ctk.CTkFrame(self)
        self.verify_label = ctk.CTkLabel(self.verify_frame,
                                         text="Verification Settings:",
                                         font=("Arial", 12, "bold"))
      
        self.verify_checksum_var = ctk.BooleanVar(value=self.cfg_mgr.verify_checksum)
        self.verify_checksum_cb = ctk.CTkCheckBox(self.verify_frame,
                                                  text="Verify checksum after transfer",
                                                  variable=self.verify_checksum_var,
                                                  command=self.on_verify_checksum_change)
      
        self.verify_size_var = ctk.BooleanVar(value=self.cfg_mgr.verify_file_size)
        self.verify_size_cb = ctk.CTkCheckBox(self.verify_frame,
                                              text="Verify file sizes",
                                              variable=self.verify_size_var,
                                              command=self.on_verify_size_change)
      
        self.algorithm_label = ctk.CTkLabel(self.verify_frame,
                                            text="Hash Algorithm:")
        self.algorithm_menu = ctk.CTkOptionMenu(self.verify_frame,
                                                values=["md5", "sha1", "sha256", "sha512"],
                                                command=self.on_algorithm_change)
        self.algorithm_menu.set(self.cfg_mgr.checksum_algorithm)
      
        # ------------------------------------------------- Buttons -------
        self.button_frame = ctk.CTkFrame(self)
      
        self.start_btn = ctk.CTkButton(self.button_frame,
                                       text="Start Transfer",
                                       command=self.start_transfer,
                                       fg_color="#2E7D32",
                                       hover_color="#1B5E20",
                                       height=40,
                                       font=("Arial", 14, "bold"))
        self.stop_btn = ctk.CTkButton(self.button_frame,
                                      text="Stop",
                                      command=self.stop_transfer,
                                      fg_color="#D32F2F",
                                      hover_color="#B71C1C",
                                      height=35,
                                      state="disabled")
      
        # ------------------------------------------------- Progress ------
        self.progress_frame = ctk.CTkFrame(self)
        self.progress_bar = ctk.CTkProgressBar(self.progress_frame, height=20)
        self.progress_text = ctk.CTkLabel(self.progress_frame, text="Ready",
                                          font=("Arial", 12))
      
        # ------------------------------------------------- Verification progress frame (hidden initially)
        self.verify_progress_frame = ctk.CTkFrame(self)
        self.verify_progress_label = ctk.CTkLabel(self.verify_progress_frame,
                                                 text="Verification Progress:",
                                                 font=("Arial", 12, "bold"))
        self.verify_progress_bar = ctk.CTkProgressBar(self.verify_progress_frame,
                                                       height=20)
        self.verify_progress_text = ctk.CTkLabel(self.verify_progress_frame,
                                                text="Waiting to start verification...",
                                                font=("Arial", 11))
        self.verify_current_file = ctk.CTkLabel(self.verify_progress_frame,
                                               text="", font=("Arial", 10))
      
        # ------------------------------------------------- Log ----------
        self.log_frame = ctk.CTkFrame(self)
        self.log_label = ctk.CTkLabel(self.log_frame,
                                      text="Transfer Log:",
                                      font=("Arial", 14, "bold"))
        self.log_text = ctk.CTkTextbox(self.log_frame,
                                       font=("Consolas", 10),
                                       wrap="word")
        self.log_text.configure(state="disabled")
        self.clear_log_btn = ctk.CTkButton(self.log_frame,
                                           text="Clear Log",
                                           width=100,
                                           command=self.clear_log)
      
        # ------------------------------------------------- Status bar
        self.status_bar = ctk.CTkLabel(self,
                                       text="Ready",
                                       font=("Arial", 10),
                                       anchor="w")
    def _setup_layout(self):
        """Arrange widgets in the window."""
        # Root grid: give extra weight to log frame (row 6)
        self.grid_columnconfigure(0, weight=1) # single column for simplicity
        self.grid_rowconfigure(5, weight=1) # row index 5 is log_frame
      
        # ------------------------------------------------- Source ----------
        self.source_frame.grid(row=0, column=0,
                               padx=20, pady=(20, 10), sticky="ew")
        self.source_frame.grid_columnconfigure((0, 1), weight=1) # label + entry+button
        self.source_label.grid(row=0, column=0, padx=(10,5),
                              pady=10, sticky="w")
        self.source_entry.grid(row=0, column=1, padx=5,
                               pady=10, sticky="ew")
        self.source_browse_btn.grid(row=0, column=2,
                                    padx=(5,10), pady=10, sticky="e")
      
        if self.cfg_mgr.last_source and Path(self.cfg_mgr.last_source).exists():
            self.source_entry.insert(0, self.cfg_mgr.last_source)
      
        # ------------------------------------------------- Destination -----
        self.dest_frame.grid(row=1, column=0,
                              padx=20, pady=(10, 10), sticky="ew")
        self.dest_frame.grid_columnconfigure((0, 1), weight=1)
        self.dest_label.grid(row=0, column=0, padx=(10,5),
                             pady=10, sticky="w")
        self.dest_entry.grid(row=0, column=1, padx=5,
                             pady=10, sticky="ew")
        self.dest_browse_btn.grid(row=0, column=2,
                                  padx=(5,10), pady=10, sticky="e")
      
        if self.cfg_mgr.last_destination:
            self.dest_entry.insert(0, self.cfg_mgr.last_destination)
      
        # ------------------------------------------------- Settings -------
        self.settings_frame.grid(row=2, column=0,
                                 padx=20, pady=(10, 10), sticky="ew")
        self.chunk_label.grid(row=0, column=0, padx=(10,5),
                              pady=10, sticky="w")
        self.chunk_entry.grid(row=0, column=1, padx=5,
                              pady=10, sticky="ew")
        # leave an empty cell for spacing
        # transfers label and slider
        self.transfers_label.grid(row=0, column=2, padx=(20,5),
                                  pady=10, sticky="w")
        self.transfers_slider.grid(row=0, column=3,
                                   padx=5, pady=10, sticky="ew")
        self.transfers_value.grid(row=0, column=4, padx=(5,10), pady=10, sticky="w")
        # ------------------------------------------------- Verification ----
        self.verify_frame.grid(row=3, column=0,
                               padx=20, pady=(10, 10), sticky="ew")
        self.verify_frame.grid_columnconfigure((0, 1, 2), weight=1) # three columns: checkbox pair + menu
        self.verify_label.grid(row=0, column=0, padx=10,
                               pady=(5, 10), sticky="w", columnspan=3)
      
        self.verify_checksum_cb.grid(row=1, column=0, padx=(10,5),
                                    pady=5, sticky="w")
        self.verify_size_cb.grid(row=1, column=1, padx=5,
                                 pady=5, sticky="w")
        # hash algorithm
        self.algorithm_label.grid(row=2, column=0, padx=(10,5),
                                  pady=(10, 5), sticky="w")
        self.algorithm_menu.grid(row=2, column=1, padx=5,
                                 pady=(10, 5), sticky="ew", columnspan=2)
        # ------------------------------------------------- Button frame ----
        self.button_frame.grid(row=4, column=0,
                               padx=20, pady=(10, 10), sticky="ew")
        # two equal-weight columns for start/stop buttons
        self.button_frame.grid_columnconfigure((0, 1), weight=1)
      
        self.start_btn.grid(row=0, column=0, padx=10,
                            pady=10, sticky="ew")
        self.stop_btn.grid(row=0, column=1, padx=10,
                            pady=10, sticky="ew")
        # ------------------------------------------------- Progress ------
        self.progress_frame.grid(row=5, column=0,
                                 padx=20, pady=(10, 10), sticky="ew")
        self.progress_frame.grid_columnconfigure(0, weight=1)
      
        self.progress_bar.grid(row=0, column=0, padx=10, pady=5,
                               sticky="ew")
        self.progress_text.grid(row=1, column=0, padx=10,
                                pady=(0, 5), sticky="w")
        # ------------------------------------------------- Verification progress (initially hidden)
        self.verify_progress_frame.grid_remove()
        self.verify_progress_label.grid(row=0, column=0, padx=10,
                                        pady=(5,2), sticky="w")
        self.verify_progress_bar.grid(row=1, column=0, padx=10, pady=2,
                                     sticky="ew")
        self.verify_progress_text.grid(row=2, column=0, padx=10,
                                      pady=2, sticky="w")
        self.verify_current_file.grid(row=3, column=0, padx=10,
                                     pady=(2,5), sticky="w")
        # ------------------------------------------------- Log ----------
        self.log_frame.grid(row=6, column=0, padx=20, pady=(0, 10),
                            sticky="nsew")
        self.log_frame.grid_columnconfigure(0, weight=1)
        self.log_frame.grid_rowconfigure(1, weight=1) # log_text takes most vertical space
      
        self.log_label.grid(row=0, column=0, padx=10,
                            pady=(10,5), sticky="w")
        self.clear_log_btn.grid(row=0, column=1, padx=10,
                                pady=(10,5), sticky="e")
        self.log_text.grid(row=1, column=0, columnspan=2,
                           padx=10, pady=(0, 10),
                           sticky="nsew")
        # ------------------------------------------------- Status bar
        self.status_bar.grid(row=7, column=0, padx=20, pady=(0,10),
                             sticky="ew")
  
    def _start_event_processor(self):
        """Background thread that pulls events and updates the UI."""
        def event_loop():
            while True:
                try:
                    # Get an item from queue with a short timeout
                    ev = self._event_queue.get(timeout=0.1)
                    # Put it in main thread queue so after() can execute safely
                    self.after(0, lambda e=ev: self.process_event(e))
                except Empty:
                    continue # nothing to do
                except Exception as exc:
                    self.logger.error(f"Event loop error: {exc}")
        threading.Thread(target=event_loop, daemon=True).start()
  
    def process_event(self, event):
        """Dispatch incoming events."""
        ev_type = event[0]
        if ev_type == "log":
            level, msg = event[1], event[2]
            self.append_log(level, msg)
        elif ev_type == "progress":
            percent, speed, eta = event[1], event[2], event[3]
            self.update_progress(percent, speed, eta)
        elif ev_type == "finished":
            self.on_transfer_finished()
        elif ev_type == "verify_start":
            total_files = event[1]
            self.on_verification_start(total_files)
        elif ev_type == "verify_progress":
            verified, failed, total, current_file = event[1], event[2], event[3], event[4]
            self.update_verification_progress(verified, failed, total, current_file)
        elif ev_type == "verify_complete":
            verified, failed, failures = event[1], event[2], event[3]
            self.on_verification_complete(verified, failed, failures)
        elif ev_type == "verify_error":
            err_msg = event[1]
            self.append_log("ERROR", f"Verification error: {err_msg}")
  
    # --------------------------------------------------------------------
    # Logging
    # --------------------------------------------------------------------
    def append_log(self, level: str, message: str):
        """Append a log line to the UI."""
        colors = {
            "ERROR": "#FF5252",
            "WARNING": "#FFB74D",
            "INFO": "#B3E5FC",
            "SUCCESS": "#C8E6C9"
        }
        self.log_text.configure(state="normal")
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] [{level.upper():>8}] {message}\n"
        self.log_text.insert("end", line)
      
        start_idx = f"end-{len(line)}c"
        end_idx = "end"
        self.log_text.tag_add(level, start_idx, end_idx)
        self.log_text.tag_config(level, foreground=colors.get(level.upper(), "#FFFFFF"))
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
      
        # Update status bar for important levels
        if level in ("ERROR", "SUCCESS"):
            self.status_bar.configure(text=f"{level.lower()}: {message[:25]}")
  
    def update_progress(self, percent: int, speed: str, eta: str):
        """Update the transfer progress UI."""
        # Clamp percentages and avoid negative values
        if not (0 <= percent <= 100):
            return
        self.progress_bar.set(percent / 100.0)
        # Format speed nicely; strip extra spaces
        speed_str = re.sub(r'\s+', '', speed)
        self.progress_text.configure(
            text=f"{max(0, int(percent))}% | Speed: {speed_str} | ETA: {eta}"
        )
  
    def update_verification_progress(self, verified: int, failed: int,
                                     total: int, current_file: str):
        """Update verification progress UI."""
        if not self.is_verifying or total == 0:
            return
        progress = (verified + failed) / max(total, 1)
        percent = min(100, int(progress * 100))
        self.verify_progress_bar.set(percent / 100.0)
        file_display = current_file if len(current_file) <= 80 else f"{current_file[:80]}..."
        self.verify_progress_text.configure(
            text=f"Verified: {verified} | Failed: {failed} | Total: {total} | "
                 f"Progress: {percent}%"
        )
        self.verify_current_file.configure(text=file_display)
  
    # --------------------------------------------------------------------
    # UI Actions
    # --------------------------------------------------------------------
    def browse_source(self):
        src_path = filedialog.askdirectory(
            title="Select Source Directory",
            initialdir=self.cfg_mgr.last_source if self.cfg_mgr.last_source else "."
        )
        if not src_path:
            src_path = filedialog.askopenfilename(
                title="Select Source File",
                initialdir=self.cfg_mgr.last_source if self.cfg_mgr.last_source else "."
            )
        if src_path:
            self.source_entry.delete(0, "end")
            self.source_entry.insert(0, src_path)
            self.cfg_mgr.last_source = src_path
            self._schedule_config_save()
  
    def browse_destination(self):
        dst_path = filedialog.askdirectory(
            title="Select Destination Directory",
            initialdir=self.cfg_mgr.last_destination if self.cfg_mgr.last_destination else "."
        )
        if dst_path:
            self.dest_entry.delete(0, "end")
            self.dest_entry.insert(0, dst_path)
            self.cfg_mgr.last_destination = dst_path
            self._schedule_config_save()
  
    def _schedule_config_save(self):
        """Delay config save for 500 ms to avoid rapid writes."""
        if self._config_save_scheduled is not None:
            self.after_cancel(self._config_save_scheduled)
        self._config_save_scheduled = self.after(500, self._do_config_save)
  
    def _do_config_save(self):
        try:
            self.cfg_mgr.save()
        except Exception as exc:
            self.logger.error(f"Failed to save config: {exc}")
        finally:
            self._config_save_scheduled = None
  
    # ------------------- Settings UI handlers -------------------------
    def on_transfers_change(self, value):
        transfers = int(value)
        self.transfers_value.configure(text=str(transfers))
        self.cfg_mgr.transfers = transfers
        self._schedule_config_save()
  
    def on_verify_checksum_change(self):
        enable = self.verify_checksum_var.get()
        self.cfg_mgr.verify_checksum = enable
        self._schedule_config_save()
        # Enable/disable related controls based on checkbox state
        if enable:
            self.algorithm_menu.configure(state="normal")
        else:
            self.algorithm_menu.configure(state="disabled")
  
    def on_verify_size_change(self):
        self.cfg_mgr.verify_file_size = self.verify_size_var.get()
        self._schedule_config_save()
  
    def on_algorithm_change(self, choice):
        self.cfg_mgr.checksum_algorithm = choice
        self._schedule_config_save()
  
    # ------------------- Transfer controls ---------------------------
    def start_transfer(self):
        source = self.source_entry.get().strip()
        destination = self.dest_entry.get().strip()
      
        if not source or not destination:
            messagebox.showwarning("Input Error", "Both source and destination must be specified.")
            return
      
        if source == destination:
            messagebox.showwarning("Input Error", "Source and destination cannot be the same.")
            return
      
        try:
            chunk_size = float(self.chunk_entry.get().strip())
            if not (0 < chunk_size <= 1024):
                raise ValueError()
        except Exception as e:
            messagebox.showwarning("Input Error",
                                   f"Invalid chunk size: {e}")
            return
      
        transfers = int(self.transfers_slider.get())
      
        # Persist settings
        self.cfg_mgr.chunk_size = chunk_size
        self.cfg_mgr.transfers = transfers
        self.cfg_mgr.save()
      
        # Show UI feedback
        self.is_transferring = True
        self.aborted = False
      
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
      
        # Lock source/destination fields
        self.source_entry.configure(state="readonly")
        self.dest_entry.configure(state="readonly")
      
        self.progress_bar.set(0)
        self.progress_text.configure(text="Starting transfer...")
        self.status_bar.configure(text="Transfer in progress...")
      
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
      
        # Launch background thread
        def launch_transfer():
            try:
                self.engine = TransferEngine(self, self.logger)
                self.engine.run_transfer(source,
                                         destination,
                                         transfers,
                                         chunk_size)
            finally:
                self.is_transferring = False
  
        threading.Thread(target=launch_transfer, daemon=True).start()
  
    def stop_transfer(self):
        """Prompt and abort the current transfer."""
        if not (self.is_transferring and self.engine):
            return
      
        reply = messagebox.askyesno(
            "Stop Transfer",
            "Are you sure you want to stop the transfer? \nPartial writes may remain on disk."
        )
        if not reply:
            return
      
        self.aborted = True
        self.stop_btn.configure(state="disabled")
      
        # Stop engine
        self.engine.stop()
  
    def on_transfer_finished(self):
        """Callback invoked when the TransferEngine signals completion."""
        if not self.engine:
            return
      
        self.is_transferring = False
        self.aborted = False
  
        # Clean UI states
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
  
        self.source_entry.configure(state="normal")
        self.dest_entry.configure(state="normal")
      
        source = self.source_entry.get().strip()
        destination = self.dest_entry.get().strip()
      
        if (self.cfg_mgr.verify_checksum or self.cfg_mgr.verify_file_size) and \
           source and destination and os.path.exists(source) and os.path.isdir(destination):
            # Show start verification message
            self.append_log("INFO", "Transfer completed. Starting verification...")
            self.status_bar.configure(text="Starting verification...")
          
            def launch_verification():
                try:
                    if not self.verifier:
                        self.verifier = ChecksumVerifier(self, self.logger)
                  
                    self.is_verifying = True
                    self.verifier.verify_transfer(source,
                                                  destination,
                                                  algorithm=self.cfg_mgr.checksum_algorithm,
                                                  verify_checksum=self.cfg_mgr.verify_checksum,
                                                  verify_size=self.cfg_mgr.verify_file_size,
                                                  max_workers=4)
                finally:
                    self.is_verifying = False
          
            threading.Thread(target=launch_verification, daemon=True).start()
        else:
            # Transfer completed without verification
            self.append_log("SUCCESS", "Transfer completed successfully")
            self.status_bar.configure(text="Transfer complete")
            messagebox.showinfo("Transfer Complete",
                               f"All files have been copied.\n"
                               f"Verification was not performed.")
  
    # ------------------- Verification UI handlers ---------------------
    def on_verification_start(self, total_files: int):
        """Show verification progress frame."""
        self.is_verifying = True
        self.progress_frame.grid_remove()
        self.verify_progress_frame.grid(row=5, column=0,
                                 padx=20, pady=(10, 10), sticky="ew")
        self.verify_progress_bar.set(0)
        self.verify_progress_text.configure(text=f"Starting verification of {total_files} files...")
        self.append_log("INFO", f"Started verification for {total_files} files")
        self.status_bar.configure(text="Verification in progress...")
      
        # Disable start/stop buttons
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
  
    def on_verification_complete(self, verified: int,
                                 failed: int,
                                 failures: List[dict]):
        """Hide verification frame and report results."""
        self.is_verifying = False
        self.verify_progress_frame.grid_remove()
        self.progress_frame.grid(row=5, column=0,
                                 padx=20, pady=(10, 10), sticky="ew")
      
        # Reset start/stop buttons appropriately
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
  
        if failed == 0:
            self.append_log("SUCCESS",
                            f"All {verified} files verified successfully!")
            self.status_bar.configure(text=f"All verification succeeded")
            messagebox.showinfo("Verification Success",
                                "All file verifications match!\nTransfer is complete and accurate.")
        else:
            self.append_log("ERROR",
                             f"Verification failed: {failed} out of {verified+failed}")
            self.status_bar.configure(text="Verification error")
            # Build a human‑readable report (only first 10 failures shown)
            report = "Verification Results:\n\n"
            report += f"✓ Verified successfully : {verified}\n"
            report += f"✗ Failed verification : {failed}\n\n"
            for i, entry in enumerate(failures[:min(10, len(failures))]):
                filename = entry["file"]
                report += f"{i+1}. {filename}\n"
                if "error" in entry:
                    report += f" Error : {entry['error']}\n"
                if entry.get("source_hash") and entry.get("dest_hash"):
                    report += f" Source hash : {entry['source_hash']}\n"
                    report += f" Destination hash: {entry['dest_hash']}\n"
                elif entry.get("source_size") and entry.get("dest_size"):
                    report += f" Size mismatch (src={entry['source_size']}B, dest={entry['dest_size']}B)\n"
            if len(failures) > 10:
                report += f"... plus {len(failures)-10} more files\n"
            report += "\nA detailed log has been saved."
            messagebox.showerror("Verification Failed", report)
  
    # ------------------- Log handling --------------------------------
    def clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.append_log("INFO", "Log cleared")
  
    # --------------------------------------------------------------------
    # Window management / cleanup
    # --------------------------------------------------------------------
    def on_close(self):
        """Clean shutdown sequence."""
        # Clean up transfer first (if running)
        if self.is_transferring and self.engine:
            try:
                self.engine.stop()
                self._event_queue.put(("finished",))
                # Wait a bit for process to exit
                time.sleep(0.5)
            except Exception:
                pass
      
        # Clean up verification if needed
        if self.is_verifying and self.verifier:
            stop_verification = messagebox.askyesno(
                "Exit",
                ("A verification is currently running.\n"
                 "Do you want to abort it and exit the program?")
            )
            if not stop_verification:
                return
            self.verifier.stop()
      
        # Close logger
        if self.logger:
            self.logger.close()
      
        try:
            self.destroy()
        except Exception:
            pass

# ----------------------------------------------------------------------
if __name__ == "__main__":
    app = ProfessionalDITApp()
    app.mainloop()
