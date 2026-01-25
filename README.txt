===========================================
        SaveSync - Game Save Manager
===========================================

Sync your game saves to Google Drive automatically.
Download saves before playing, upload after closing.

-------------------------------------------
REQUIREMENTS
-------------------------------------------
- Python 3.10+
- Dependencies: pip install pydrive2 winshell pywin32

-------------------------------------------
HOW TO RUN (Development)
-------------------------------------------
GUI Mode:    python savesync.py
CLI Mode:    python savesync.py --game <game_id>
Help:        python savesync.py --help

-------------------------------------------
HOW TO BUILD EXE
-------------------------------------------
1. Install PyInstaller:
   pip install pyinstaller

2. Build the executable:
   pyinstaller savesync.spec --noconfirm

3. Find EXE in: dist\SaveSync_v<version>.exe

-------------------------------------------
HOW TO UPDATE VERSION
-------------------------------------------
1. Edit savesync.py, line ~20:
   VERSION = "1.0.0"  -->  VERSION = "1.1.0"

2. Edit savesync.spec, update the version in:
   - Import statement: from savesync import VERSION
   - EXE name uses VERSION automatically

3. Rebuild the EXE (see above)

-------------------------------------------
DATA STORAGE
-------------------------------------------
All config files are stored in:
  %APPDATA%\saveSync\

Contents:
  - client_secrets.json   (Google API credentials)
  - credentials.txt       (OAuth tokens)
  - settings.json         (App settings)
  - games\*.json          (Per-game configs)

-------------------------------------------
USAGE WORKFLOW
-------------------------------------------
1. First run: Paste Google OAuth client secrets
2. Add games via "+ Add Game" button
3. Click shortcut icon (🔗) to create desktop shortcut
4. Use shortcut to sync & play automatically!

-------------------------------------------
SETTINGS
-------------------------------------------
Access via ⚙ Settings button in the GUI.

- Show log window when launching via shortcut
  When enabled (default), desktop shortcuts show a 
  progress window during sync. Disable for silent mode.

-------------------------------------------
SYNC CONFLICT HANDLING
-------------------------------------------
If local saves are NEWER than cloud, you'll be asked:
  - Keep Local: Skip download, upload after playing
  - Use Cloud:  Overwrite local with cloud saves
  - Cancel:     Abort sync

-------------------------------------------
FILES
-------------------------------------------
savesync.py     - Main application (GUI + CLI)
savesync.spec   - PyInstaller build config
README.txt      - This file
