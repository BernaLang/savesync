===========================================
        SaveSync - Game Save Manager
===========================================

Sync your game saves to Google Drive automatically.
Download saves before playing, upload after closing.

# Instalation and usage
- Get the latest release from the github Releases
- On the app, add your google client credentials generated in the google console
- Configure a game + create a shortcut that auto runs the app and the game
- Enjoy savefiles synced across PCs :)


## DEV

-------------------------------------------
REQUIREMENTS
-------------------------------------------
- Python 3.10+
- Dependencies: pip install pydrive2 winshell pywin32 OR pip install -r requirements.txt

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
   WSL: python3.exe -m PyInstaller ./savesync.spec

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
AUTO-UPDATE
-------------------------------------------
SaveSync checks for updates from GitHub Releases
on every launch (GUI and CLI/shortcut modes).

GUI Mode:
  - A green ⬆ icon appears next to the version
    in the title bar when an update is available.
  - Click the icon to see release notes and choose:
    - Update Now: Downloads and installs automatically
    - Skip This Version: Won't prompt for this version
    - Remind Me Later: Will check again next launch
  - You can also check manually via Settings >
    🔄 Check for Updates

CLI/Shortcut Mode:
  - Update availability is shown in the log after
    sync completes. Open SaveSync GUI to install.

Note: Auto-update only works for the built EXE.
When running as a Python script, updates are skipped.

-------------------------------------------
FILES
-------------------------------------------
savesync.py     - Main application (GUI + CLI)
savesync.spec   - PyInstaller build config
README.txt      - This file
