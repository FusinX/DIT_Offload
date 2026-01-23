# Professional DIT Transfer Tool (DIT_Offload)

A small, focused Windows GUI tool for reliable file transfers using rclone, with optional post-transfer verification (file size and/or checksums). The project is written in Python and uses CustomTkinter for the UI. It is intended to be packaged as a standalone Windows executable (single-file) for easy distribution to non-technical users.

This README explains what the tool does, how to run it from source, how to build a standalone EXE, configuration options, verification behavior, and troubleshooting tips.

---

Table of contents
- Overview
- Key features
- Requirements
- Running from source
- Building a standalone Windows EXE
- Quick usage guide (GUI)
- Configuration (dit_config.json)
- Verification details
- Logs and troubleshooting
- Packaging notes (rclone and dependencies)
- Contributing
- License

---

Overview
--------
The Professional DIT Transfer Tool provides a simple GUI for copying files or folders with rclone under the hood. After a transfer completes the application can automatically verify the destination files against the source using file-size checks, checksums (md5/sha1/sha256/sha512), or both. The tool is optimized for use on Windows and intended for Digital Imaging Technicians (DITs) and similar workflows where integrity is important.

Key features
------------
- Simple, single-window GUI with source/destination selection.
- Uses rclone to perform high-performance, resumable transfers (the tool requires the rclone executable).
- Real-time progress parsing and logging of rclone output.
- Optional post-transfer verification:
  - Verify file sizes (independent)
  - Verify checksums (md5/sha1/sha256/sha512)
  - Verification can run in parallel (thread pool)
- Lightweight JSON configuration persistence for last paths, transfer settings, and verification preferences.
- Small on-disk logger written to %APPDATA%\DIT_Pro_Tool\dit_transfer.log.
- Designed to be packaged into a single-file Windows executable using PyInstaller.

Requirements
------------
- Windows 10/11 recommended (UI and rclone handling use Windows-specific flags).
- Python 3.8+ to run from source.
- rclone executable (rclone.exe) must be bundled or placed next to the app/executable.
- Python dependencies:
  - customtkinter
  - (standard library modules used: tkinter, threading, subprocess, concurrent.futures, hashlib, json, pathlib, etc.)

Install dependencies (example)
- Create a venv (recommended)
  - python -m venv venv
  - venv\Scripts\activate
- Install customtkinter:
  - pip install customtkinter

Running from source
-------------------
1. Ensure `rclone.exe` is available on the system PATH or next to the script (the app prefers a bundled `rclone.exe` using the resource_path helper).
2. Install dependencies (see Requirements).
3. Launch:
   - python dit_offload.py

Note: Running from source will open the GUI. The app will write logs and configuration to:
- %APPDATA%\DIT_Pro_Tool\dit_transfer.log
- %APPDATA%\DIT_Pro_Tool\dit_config.json

Building a standalone Windows EXE
--------------------------------
The project is purposely structured for single-file distribution. An example PyInstaller build command (used by the author) is:

python -m PyInstaller --noconsole --onefile --collect-all customtkinter --add-binary "rclone.exe;." professional_dit.py

Notes:
- Replace `professional_dit.py` with `dit_offload.py` (or your chosen entrypoint name) if necessary.
- The `--add-binary "rclone.exe;."` flag bundles rclone.exe into the EXE root so resource_path("rclone.exe") will find it at runtime.
- Test the built EXE on a clean Windows machine to ensure rclone is located correctly and that no other runtime dependencies are missing.

Quick usage guide (GUI)
-----------------------
1. Source: Browse to a source directory (or file) to copy from.
2. Destination: Browse to a destination directory (rclone will copy the source into the destination).
3. Chunk Size: Set buffer size in MiB (used as rclone's --buffer-size).
4. Transfers: Number of concurrent transfers (rclone `--transfers`).
5. Verification:
   - Enable "Verify checksum after transfer" to compute checksums on source and destination for integrity checks.
   - Enable "Verify file sizes" to require exact file-size matches.
   - Choose hash algorithm: md5, sha1, sha256, sha512.
   - Note: File-size verification can be enabled independently of checksum verification — the two checks are not strictly coupled.
6. Start Transfer: Click "Start Transfer" to begin. The UI displays rclone logs, transfer progress, and verification progress (if enabled).
7. Stop: Use "Stop" to terminate the transfer. Partial files may remain — see Troubleshooting.

Configuration (dit_config.json)
-------------------------------
A tiny JSON config is saved to %APPDATA%\DIT_Pro_Tool\dit_config.json. Defaults are:

{
  "chunk_size": 10.0,
  "last_source": "",
  "last_destination": "",
  "transfers": 4,
  "verify_checksum": true,
  "checksum_algorithm": "md5",
  "verify_file_size": true
}

- chunk_size: float (MiB) used for rclone --buffer-size.
- transfers: int number of concurrent transfers.
- verify_checksum: boolean — enable checksum verification after transfer.
- checksum_algorithm: one of "md5", "sha1", "sha256", "sha512".
- verify_file_size: boolean — enable file-size verification.

Verification behavior
---------------------
- Verification runs after the transfer finishes (if the corresponding verification toggles are enabled).
- Supported algorithms: md5, sha1, sha256, sha512.
- The verification engine enumerates source files and compares them to destination files by relative path.
- File-size verification can be enabled/disabled independently. If file sizes differ, checksum may be skipped (depending on settings).
- Verification runs in parallel (thread pool). There is a timeout heuristic per-file based on file size (minimum ~300s total or scaled with size).
- Verification results are reported in the GUI and logged. If failures are found, a concise report is presented (the GUI shows the first up to 10 failures).

Logs and troubleshooting
------------------------
- Primary log file:
  - %APPDATA%\DIT_Pro_Tool\dit_transfer.log
- UI log window displays rclone output and internal INFO/ERROR messages.
- Common issues:
  - rclone.exe not found: The app will abort the transfer and log an error. Ensure rclone.exe is bundled with the EXE or available on PATH.
  - Source or destination path problems: The app runs a pre-flight check. Ensure the source exists and destination parent folder is writable.
  - Transfer stalls or slow: Check rclone output in the UI; network/back-end problems often manifest there.
  - Verification timeouts: Very large files can take long to hash; adjust thread counts or allow more time per file.
- If you see an uncaught exception, check the log file mentioned above for a traceback.

Packaging notes (rclone and dependencies)
----------------------------------------
- rclone is a required external binary. The simplest distribution method for Windows EXE is to bundle rclone.exe into the single-file EXE via PyInstaller's --add-binary flag (see Build instructions).
- customtkinter is required for the UI. The PyInstaller build should include it via --collect-all customtkinter (or equivalent hook).
- The application uses Windows-specific creation flags (CREATE_NO_WINDOW) when spawning rclone to avoid opening a console window. This is not portable to POSIX systems without modification.

Security & privacy
------------------
- Checksums and file sizes are calculated locally only; no network upload of verification metadata is performed by this tool.
- The app logs operation details to the local logger. Do not distribute logs that may contain sensitive file names or paths.

Contributing
------------
Contributions are welcome. If you plan to:
- Add features (e.g., more rclone flags exposed).
- Improve the verification heuristics (timeouts, thread tuning).
- Add error recovery or retry logic.

Please open an issue describing the proposed change or submit a PR with tests and a short description.

License
-------
Specify your license here (e.g., MIT). If none is included, consider adding a LICENSE file.

Contact / Author
----------------
Author: FusinX (GitHub: [FusinX](https://github.com/FusinX))

Changelog (high level)
----------------------
- v2.0 (current): Optimized verification handling, independent file-size verification, UI improvements, config rename (scale -> chunk_size), minor cleanup.

---

If you want, I can:
- Add a sample LICENSE (MIT) file.
- Produce a changelog or release notes draft.
- Create a simple CONTRIBUTING.md with development steps and PyInstaller build recipe.
