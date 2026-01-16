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
class TransferEngine:
    def __init__(self, logger, ui_callback):
        self.logger = logger
        self.ui_callback = ui_callback
        self.process = None
        self.stopped = False
        
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
        if "Transferred:" in line:
            try:
                percentage_match = re.search(r'(\d+)%', line)
                percentage = int(percentage_match.group(1)) if percentage_match else 0
                
                speed_match = re.search(r'([\d.]+)\s*([KMG]i?B)/s', line)
                speed = f"{speed_match.group(1)} {speed_match.group(2)}/s" if speed_match else "0 MB/s"
                
                eta_match = re.search(r'ETA\s+(\S+)', line)
                eta = eta_match.group(1) if eta_match else "---"
                
                return ("progress", percentage, speed, eta)
            except Exception as e:
                self.logger.error(f"Error parsing progress: {e}")
        
        if ":" in line and "/" in line:
            file_match = re.search(r'([^/]+)$', line.strip())
            if file_match:
                filename = file_match.group(1).strip()
                if filename and len(filename) > 3 and "%" not in filename:
                    return ("file", filename)
        
        return None
    
    def run_rclone_copy(self, src, dst, transfers=4):
        """Execute rclone copy with real-time progress"""
        self.stopped = False
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
                if self.stopped:
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
            
            self.process.wait()
            return self.process.returncode
            
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
        """Stop current transfer"""
        self.stopped = True
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except:
                self.process.kill()
            self.ui_callback("log", "Transfer stopped by user", "WARNING")
            self.logger.warning("Transfer stopped by user")

# ==================== MAIN APPLICATION ====================
class ProfessionalDITApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        
        self.logger = DITLogger()
        self.config = ConfigManager.load()
        self.engine = None
        self.transfer_thread = None
        self.is_transferring = False
        
        self.check_dependencies()
        
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        
        self.title("DIT Pro v2.0 | Secure Media Transfer")
        self.geometry("1100x850")
        self.resizable(False, False)
        
        self.setup_ui()
        self.load_saved_config()
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        self.logger.info("="*60)
        self.logger.info("DIT Pro v2.0 Started")
        self.logger.info("="*60)
    
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
        header = ctk.CTkFrame(self, height=80, fg_color="#1a1a1a")
        header.pack(fill="x", pady=(0, 20))
        
        title = ctk.CTkLabel(header, text="DIT PRO", font=("Helvetica", 32, "bold"), text_color="#00d4ff")
        title.pack(side="left", padx=20, pady=20)
        
        subtitle = ctk.CTkLabel(header, text="Professional Rclone + ASC-MHL Workflow", font=("Helvetica", 12), text_color="#888")
        subtitle.pack(side="left", padx=(0, 20))
        
        version = ctk.CTkLabel(header, text="v2.0", font=("Courier", 10), text_color="#444")
        version.pack(side="right", padx=20)
        
        # Main container
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.pack(fill="both", expand=True, padx=30)
        
        # Left panel
        left_panel = ctk.CTkFrame(main, width=480, fg_color="#242424")
        left_panel.pack(side="left", fill="both", padx=(0, 15))
        
        # Source section
        ctk.CTkLabel(left_panel, text="SOURCE MEDIA", font=("Helvetica", 14, "bold")).pack(pady=(20, 5))
        self.src_display = ctk.CTkTextbox(left_panel, height=60, fg_color="#1a1a1a", font=("Courier", 10), wrap="word")
        self.src_display.pack(padx=20, pady=5, fill="x")
        self.src_display.insert("1.0", "No source selected")
        self.src_display.configure(state="disabled")
        
        ctk.CTkButton(left_panel, text="Browse Source", command=lambda: self.browse("src"), height=35,
                     fg_color="#2d5a7b", hover_color="#3a7099").pack(pady=5)
        
        self.src_info = ctk.CTkLabel(left_panel, text="0 files | 0 GB", text_color="#666", font=("Courier", 10))
        self.src_info.pack(pady=5)
        
        # Destination 1
        ctk.CTkLabel(left_panel, text="DESTINATION 1 (Primary)", font=("Helvetica", 14, "bold")).pack(pady=(30, 5))
        self.dst1_display = ctk.CTkTextbox(left_panel, height=60, fg_color="#1a1a1a", font=("Courier", 10), wrap="word")
        self.dst1_display.pack(padx=20, pady=5, fill="x")
        self.dst1_display.insert("1.0", "No destination selected")
        self.dst1_display.configure(state="disabled")
        
        ctk.CTkButton(left_panel, text="Browse Destination 1", command=lambda: self.browse("dst1"), height=35,
                     fg_color="#2d5a7b", hover_color="#3a7099").pack(pady=5)
        
        # Destination 2
        ctk.CTkLabel(left_panel, text="DESTINATION 2 (Backup)", font=("Helvetica", 14, "bold")).pack(pady=(30, 5))
        self.dst2_display = ctk.CTkTextbox(left_panel, height=60, fg_color="#1a1a1a", font=("Courier", 10), wrap="word")
        self.dst2_display.pack(padx=20, pady=5, fill="x")
        self.dst2_display.insert("1.0", "No destination selected")
        self.dst2_display.configure(state="disabled")
        
        ctk.CTkButton(left_panel, text="Browse Destination 2", command=lambda: self.browse("dst2"), height=35,
                     fg_color="#2d5a7b", hover_color="#3a7099").pack(pady=5)
        
        # Transfer options
        ctk.CTkLabel(left_panel, text="TRANSFER OPTIONS", font=("Helvetica", 14, "bold")).pack(pady=(30, 5))
        
        transfers_frame = ctk.CTkFrame(left_panel, fg_color="transparent")
        transfers_frame.pack(padx=20, pady=5, fill="x")
        ctk.CTkLabel(transfers_frame, text="Parallel Transfers:").pack(side="left")
        self.transfers_var = tk.StringVar(value="4")
        transfers_spinbox = ctk.CTkOptionMenu(transfers_frame, values=["1", "2", "4", "8", "16"], 
                                            variable=self.transfers_var, width=60)
        transfers_spinbox.pack(side="right")
        
        # Control buttons
        button_frame = ctk.CTkFrame(left_panel, fg_color="transparent")
        button_frame.pack(pady=30)
        
        self.start_btn = ctk.CTkButton(button_frame, text="START TRANSFER", command=self.start_transfer,
                                      height=45, fg_color="#28a745", hover_color="#218838", 
                                      font=("Helvetica", 14, "bold"))
        self.start_btn.pack(pady=5, fill="x")
        
        self.stop_btn = ctk.CTkButton(button_frame, text="STOP TRANSFER", command=self.stop_transfer,
                                     height=35, fg_color="#dc3545", hover_color="#c82333",
                                     state="disabled")
        self.stop_btn.pack(pady=5, fill="x")
        
        # Right panel
        right_panel = ctk.CTkFrame(main, fg_color="#242424")
        right_panel.pack(side="right", fill="both", expand=True)
        
        # Progress section
        ctk.CTkLabel(right_panel, text="TRANSFER PROGRESS", font=("Helvetica", 14, "bold")).pack(pady=(20, 5))
        
        self.progress_bar = ctk.CTkProgressBar(right_panel, height=20)
        self.progress_bar.pack(padx=20, pady=10, fill="x")
        self.progress_bar.set(0)
        
        self.progress_label = ctk.CTkLabel(right_panel, text="0%", font=("Helvetica", 16, "bold"))
        self.progress_label.pack()
        
        # Current file
        ctk.CTkLabel(right_panel, text="CURRENT FILE", font=("Helvetica", 12, "bold")).pack(pady=(20, 5))
        self.current_file_label = ctk.CTkLabel(right_panel, text="Waiting...", text_color="#888", 
                                              font=("Courier", 10), wraplength=400)
        self.current_file_label.pack(pady=5)
        
        # Stats frame
        stats_frame = ctk.CTkFrame(right_panel, fg_color="transparent")
        stats_frame.pack(pady=20, fill="x", padx=20)
        
        ctk.CTkLabel(stats_frame, text="SPEED:").pack(side="left", padx=5)
        self.speed_label = ctk.CTkLabel(stats_frame, text="0 MB/s", text_color="#00d4ff", font=("Courier", 12))
        self.speed_label.pack(side="left", padx=(0, 20))
        
        ctk.CTkLabel(stats_frame, text="ETA:").pack(side="left", padx=5)
        self.eta_label = ctk.CTkLabel(stats_frame, text="--:--:--", text_color="#00d4ff", font=("Courier", 12))
        self.eta_label.pack(side="left")
        
        # Log display
        ctk.CTkLabel(right_panel, text="TRANSFER LOG", font=("Helvetica", 14, "bold")).pack(pady=(20, 5))
        self.log_display = ctk.CTkTextbox(right_panel, height=300, fg_color="#1a1a1a", 
                                         font=("Courier", 10), wrap="word")
        self.log_display.pack(padx=20, pady=10, fill="both", expand=True)
        self.log_display.insert("1.0", "Log initialized. Ready for transfer.\n")
        self.log_display.configure(state="disabled")
        
        # Status bar
        status_bar = ctk.CTkFrame(self, height=40, fg_color="#1a1a1a")
        status_bar.pack(fill="x", side="bottom", pady=(10, 0))
        
        self.status_label = ctk.CTkLabel(status_bar, text="Ready", text_color="#00d4ff")
        self.status_label.pack(side="left", padx=20)
        
        self.log_path_label = ctk.CTkLabel(status_bar, text="", text_color="#666", font=("Courier", 9))
        self.log_path_label.pack(side="right", padx=20)
    
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
                self.progress_bar.set(percentage / 100)
                self.progress_label.configure(text=f"{percentage}%")
                self.speed_label.configure(text=speed)
                self.eta_label.configure(text=eta)
                
            elif action == "current_file":
                filename = args[0]
                self.current_file_label.configure(text=filename)
                
            elif action == "status":
                status = args[0]
                self.status_label.configure(text=status)
        
        self.after(0, update)
    
    def start_transfer(self):
        """Start the transfer process in a separate thread"""
        if self.is_transferring:
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
            
            # Update UI
            self.start_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            self.is_transferring = True
            
            # Start transfer in thread
            self.transfer_thread = threading.Thread(
                target=self.run_transfer,
                args=(src, dst1, transfers),
                daemon=True
            )
            self.transfer_thread.start()
            
        except Exception as e:
            messagebox.showerror("Error", f"Failed to start transfer: {str(e)}")
            self.logger.error(f"Start transfer error: {e}")
    
    def run_transfer(self, src, dst, transfers):
        """Run the transfer process (called from thread)"""
        try:
            # Preflight check
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
                
        except Exception as e:
            error_msg = f"Transfer failed: {str(e)}"
            self.ui_callback("log", error_msg, "ERROR")
            self.ui_callback("status", "Failed")
            messagebox.showerror("Error", error_msg)
            self.logger.error(f"Transfer error: {e}")
        finally:
            self.after(0, self.reset_ui)
    
    def stop_transfer(self):
        """Stop the current transfer"""
        if self.engine and self.is_transferring:
            if messagebox.askyesno("Confirm", "Stop the current transfer?"):
                self.engine.stop()
                self.ui_callback("log", "Transfer stopped by user", "WARNING")
                self.reset_ui()
    
    def reset_ui(self):
        """Reset UI after transfer completion"""
        self.is_transferring = False
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.progress_bar.set(0)
        self.progress_label.configure(text="0%")
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
            "transfers": self.transfers_var.get()
        }
        ConfigManager.save(config)
    
    def on_closing(self):
        """Handle window closing"""
        if self.is_transferring:
            if messagebox.askyesno("Confirm", "Transfer in progress. Close anyway?"):
                if self.engine:
                    self.engine.stop()
                self.destroy()
        else:
            self.destroy()

if __name__ == "__main__":
    app = ProfessionalDITApp()
    app.mainloop()
