"""
SaveSync - Game Save Synchronization via Google Drive
Single application with GUI management and CLI direct-launch modes.
"""
import os
import sys
import json
import zipfile
import subprocess
import shutil
import argparse
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from pathlib import Path
from datetime import datetime
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
import pystray
from PIL import Image

# --- CONSTANTS ---
VERSION = "1.0.7"
APP_NAME = "saveSync"
APP_DATA_DIR = Path(os.getenv('APPDATA')) / APP_NAME
GAMES_DIR = APP_DATA_DIR / "games"
CLIENT_SECRETS_FILE = APP_DATA_DIR / "client_secrets.json"
CREDENTIALS_FILE = APP_DATA_DIR / "credentials.txt"
SETTINGS_FILE = APP_DATA_DIR / "settings.json"

# Default settings
DEFAULT_SETTINGS = {
    "show_log_window": True,  # Show GUI log window in CLI/shortcut mode
    "default_gdrive_folder": ""  # Default GDrive folder path for new games
}


def ensure_app_dirs():
    """Create application directories if they don't exist."""
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    GAMES_DIR.mkdir(parents=True, exist_ok=True)


def load_client_secrets():
    """Load client secrets from file."""
    if CLIENT_SECRETS_FILE.exists():
        with open(CLIENT_SECRETS_FILE, 'r') as f:
            return json.load(f)
    return None


def save_client_secrets(secrets_data):
    """Save client secrets to file."""
    ensure_app_dirs()
    with open(CLIENT_SECRETS_FILE, 'w') as f:
        json.dump(secrets_data, f)


def load_settings():
    """Load app settings from file."""
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, 'r') as f:
                saved = json.load(f)
                # Merge with defaults (in case new settings are added)
                return {**DEFAULT_SETTINGS, **saved}
        except (json.JSONDecodeError, KeyError):
            pass
    return DEFAULT_SETTINGS.copy()


def save_settings(settings):
    """Save app settings to file."""
    ensure_app_dirs()
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(settings, f, indent=2)


def load_game_config(game_id):
    """Load a game configuration by its ID."""
    config_file = GAMES_DIR / f"{game_id}.json"
    if config_file.exists():
        with open(config_file, 'r') as f:
            return json.load(f)
    return None


def save_game_config(game_id, config):
    """Save a game configuration."""
    ensure_app_dirs()
    config_file = GAMES_DIR / f"{game_id}.json"
    with open(config_file, 'w') as f:
        json.dump(config, f, indent=2)


def delete_game_config(game_id):
    """Delete a game configuration."""
    config_file = GAMES_DIR / f"{game_id}.json"
    if config_file.exists():
        config_file.unlink()


def list_games():
    """List all configured games."""
    ensure_app_dirs()
    games = []
    for config_file in GAMES_DIR.glob("*.json"):
        try:
            with open(config_file, 'r') as f:
                config = json.load(f)
                config['id'] = config_file.stem
                games.append(config)
        except (json.JSONDecodeError, KeyError):
            continue
    return games


def get_drive():
    """Handles Google OAuth authentication."""
    client_secrets = load_client_secrets()
    if not client_secrets:
        raise Exception("Client secrets not configured. Please run the GUI to set up.")
    
    settings = {
        "client_config_backend": "settings",
        "client_config": client_secrets,
        "save_credentials": True,
        "save_credentials_backend": "file",
        "save_credentials_file": str(CREDENTIALS_FILE),
        "get_refresh_token": True,
        "oauth_scope": ["https://www.googleapis.com/auth/drive"]
    }
    
    gauth = GoogleAuth(settings=settings)
    gauth.LoadCredentialsFile(str(CREDENTIALS_FILE))
    
    try:
        if gauth.credentials is None:
            gauth.LocalWebserverAuth()
        elif gauth.access_token_expired:
            gauth.Refresh()
        else:
            gauth.Authorize()
    except Exception as e:
        # Token refresh/auth failed (expired/revoked) - delete credentials and re-auth
        error_msg = str(e).lower()
        if 'invalid_grant' in error_msg or 'token' in error_msg or 'expired' in error_msg or 'revoked' in error_msg:
            print("⚠️ Access token expired or revoked. Re-authenticating...")
            if CREDENTIALS_FILE.exists():
                CREDENTIALS_FILE.unlink()
            gauth = GoogleAuth(settings=settings)
            gauth.LocalWebserverAuth()
        else:
            raise
    
    gauth.SaveCredentialsFile(str(CREDENTIALS_FILE))
    return GoogleDrive(gauth)


def get_or_create_folder(drive, folder_path):
    """Gets or creates a folder by path (e.g., 'SaveSync/CyberKnights')."""
    if not folder_path:
        return 'root'
    
    parent_id = 'root'
    folders = folder_path.strip('/').split('/')
    
    for folder_name in folders:
        query = f"title = '{folder_name}' and '{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        file_list = drive.ListFile({'q': query}).GetList()
        
        if file_list:
            parent_id = file_list[0]['id']
        else:
            folder = drive.CreateFile({
                'title': folder_name,
                'mimeType': 'application/vnd.google-apps.folder',
                'parents': [{'id': parent_id}]
            })
            folder.Upload()
            parent_id = folder['id']
            print(f"Created folder: {folder_name}")
    
    return parent_id


def get_local_save_time(local_save_dir):
    """Get the most recent modification time from local save files."""
    if not os.path.exists(local_save_dir):
        return None
    
    latest_time = None
    for root, dirs, files in os.walk(local_save_dir):
        for file in files:
            file_path = os.path.join(root, file)
            try:
                mtime = os.path.getmtime(file_path)
                file_time = datetime.fromtimestamp(mtime)
                if latest_time is None or file_time > latest_time:
                    latest_time = file_time
            except OSError:
                continue
    return latest_time


def get_cloud_save_info(drive, folder_id, remote_zip_name):
    """Get cloud save file info (modification time and file reference)."""
    query = f"title = '{remote_zip_name}' and '{folder_id}' in parents and trashed = false"
    file_list = drive.ListFile({'q': query}).GetList()
    
    if file_list:
        cloud_file = file_list[0]
        # Parse the modifiedDate from Google Drive (ISO format)
        modified_str = cloud_file.get('modifiedDate', '')
        if modified_str:
            # Format: 2024-01-25T10:30:00.000Z
            try:
                cloud_time = datetime.strptime(modified_str[:19], '%Y-%m-%dT%H:%M:%S')
                return cloud_time, cloud_file
            except ValueError:
                pass
        return None, cloud_file
    return None, None


def format_time(dt):
    """Format datetime for display."""
    if dt is None:
        return "No saves found"
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def download_and_extract(drive, folder_id, config, log_func=print):
    """Download saves from Google Drive and extract to local directory."""
    log_func("Checking Google Drive for cloud saves...")
    remote_zip_name = config.get('remote_zip_name', 'saves.zip')
    local_save_dir = config['local_save_dir']
    temp_zip = APP_DATA_DIR / "temp_sync.zip"
    
    query = f"title = '{remote_zip_name}' and '{folder_id}' in parents and trashed = false"
    file_list = drive.ListFile({'q': query}).GetList()
    
    if file_list:
        log_func("Cloud save found. Downloading...")
        file_list[0].GetContentFile(str(temp_zip))
        
        if os.path.exists(local_save_dir):
            shutil.rmtree(local_save_dir)
        os.makedirs(local_save_dir)
        
        with zipfile.ZipFile(temp_zip, 'r') as zip_ref:
            zip_ref.extractall(local_save_dir)
        
        temp_zip.unlink()
        log_func("Local saves updated from Cloud.")
    else:
        log_func("No cloud save found. Proceeding with local files.")


def zip_and_upload(drive, folder_id, config, log_func=print):
    """Zip local saves and upload to Google Drive."""
    log_func("Zipping local saves...")
    remote_zip_name = config.get('remote_zip_name', 'saves.zip')
    local_save_dir = config['local_save_dir']
    temp_zip = APP_DATA_DIR / "temp_sync.zip"
    
    with zipfile.ZipFile(temp_zip, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(local_save_dir):
            for file in files:
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, local_save_dir)
                zipf.write(file_path, rel_path)
    
    log_func("Uploading to Google Drive...")
    query = f"title = '{remote_zip_name}' and '{folder_id}' in parents and trashed = false"
    file_list = drive.ListFile({'q': query}).GetList()
    
    if file_list:
        f = file_list[0]
    else:
        f = drive.CreateFile({'title': remote_zip_name, 'parents': [{'id': folder_id}]})
    
    f.SetContentFile(str(temp_zip))
    f.Upload()
    f.content = None
    
    temp_zip.unlink()
    log_func("Sync Successful! Cloud is up to date.")


def sync_game(game_id, run_game=True, log_func=print, conflict_callback=None):
    """
    Sync a game's saves and optionally run the game.
    
    conflict_callback: Optional function(local_time, cloud_time) -> 'local' | 'cloud' | 'cancel'
                       If not provided, defaults to CLI prompt or auto-download.
    """
    config = load_game_config(game_id)
    if not config:
        raise Exception(f"Game '{game_id}' not found.")
    
    drive = get_drive()
    log_func("Authentication successful.")
    
    folder_id = get_or_create_folder(drive, config.get('gdrive_folder', ''))
    remote_zip_name = config.get('remote_zip_name', 'saves.zip')
    local_save_dir = config['local_save_dir']
    
    # Get modification times
    local_time = get_local_save_time(local_save_dir)
    cloud_time, cloud_file = get_cloud_save_info(drive, folder_id, remote_zip_name)
    
    log_func(f"Local saves: {format_time(local_time)}")
    log_func(f"Cloud saves: {format_time(cloud_time)}")
    
    # Determine sync action
    should_download = True
    
    if local_time and cloud_time:
        # Both exist - check for conflict
        if local_time > cloud_time:
            log_func("\n⚠️ CONFLICT: Local saves are NEWER than cloud!")
            if conflict_callback:
                choice = conflict_callback(local_time, cloud_time)
            else:
                choice = cli_conflict_prompt(local_time, cloud_time)
            
            if choice == 'local':
                log_func("User chose: Keep LOCAL saves (skip download)")
                should_download = False
            elif choice == 'cloud':
                log_func("User chose: Use CLOUD saves (overwrite local)")
                should_download = True
            else:  # cancel
                log_func("Sync cancelled by user.")
                return False
        else:
            log_func("Cloud is newer or same - downloading...")
    elif local_time and not cloud_time:
        log_func("No cloud saves found. Will upload local after game.")
        should_download = False
    elif not local_time and cloud_time:
        log_func("No local saves found. Downloading from cloud...")
        should_download = True
    else:
        log_func("No saves found anywhere. Starting fresh.")
        should_download = False
    
    # Download from cloud (if needed)
    if should_download and cloud_file:
        download_and_extract(drive, folder_id, config, log_func)
    
    if run_game:
        game_exe = config['game_exe']
        log_func(f"\nStarting Game: {game_exe}")
        log_func("Waiting for game to close...")
        
        process = subprocess.Popen(game_exe)
        process.wait()
        
        log_func("\nGame closed. Uploading saves...")
        # Always upload after game closes
        zip_and_upload(drive, folder_id, config, log_func)
    else:
        # Sync-only mode: upload only if we didn't download (local is newer or no cloud saves)
        if not should_download:
            zip_and_upload(drive, folder_id, config, log_func)
        else:
            log_func("Downloaded cloud saves - no upload needed.")
    return True


def cli_conflict_prompt(local_time, cloud_time):
    """CLI prompt for sync conflict resolution."""
    print("\n" + "=" * 50)
    print("  SYNC CONFLICT DETECTED")
    print("=" * 50)
    print(f"  Local saves: {format_time(local_time)}")
    print(f"  Cloud saves: {format_time(cloud_time)}")
    print("\nYour local saves are NEWER than the cloud.")
    print("What would you like to do?\n")
    print("  [1] Keep LOCAL saves (upload to cloud after playing)")
    print("  [2] Use CLOUD saves (download and overwrite local)")
    print("  [3] Cancel sync")
    print()
    
    while True:
        choice = input("Enter choice (1/2/3): ").strip()
        if choice == '1':
            return 'local'
        elif choice == '2':
            return 'cloud'
        elif choice == '3':
            return 'cancel'
        else:
            print("Invalid choice. Please enter 1, 2, or 3.")


def sync_all_games(log_func=print, conflict_callback=None):
    """Sync all configured games (download and upload only, no game launch)."""
    games = list_games()
    if not games:
        log_func("No games configured.")
        return
    
    for game in games:
        log_func(f"\n=== Syncing: {game['name']} ===")
        try:
            sync_game(game['id'], run_game=False, log_func=log_func, conflict_callback=conflict_callback)
        except Exception as e:
            log_func(f"Error syncing {game['name']}: {e}")


def create_desktop_shortcut(game_id, config):
    """Create a desktop shortcut for the game."""
    try:
        import winshell
        from win32com.client import Dispatch
    except ImportError:
        raise Exception("winshell and pywin32 are required for shortcut creation. Install with: pip install winshell pywin32")
    
    desktop = winshell.desktop()
    shortcut_name = f"{config['name']} (Cloud Save).lnk"
    shortcut_path = os.path.join(desktop, shortcut_name)
    
    # Get the executable path (either frozen or script)
    if getattr(sys, 'frozen', False):
        exe_path = sys.executable
    else:
        exe_path = sys.executable  # Python interpreter
        # For script mode, we'll create a batch file approach
    
    shell = Dispatch('WScript.Shell')
    shortcut = shell.CreateShortCut(shortcut_path)
    
    if getattr(sys, 'frozen', False):
        shortcut.Targetpath = exe_path
        shortcut.Arguments = f'--game "{game_id}"'
    else:
        # Script mode: point to python with script
        script_path = os.path.abspath(__file__)
        shortcut.Targetpath = exe_path
        shortcut.Arguments = f'"{script_path}" --game "{game_id}"'
    
    shortcut.WorkingDirectory = str(APP_DATA_DIR)
    shortcut.IconLocation = config.get('game_exe', exe_path)
    shortcut.save()
    
    return shortcut_path


# ============================================================
# GUI APPLICATION
# ============================================================

class ToolTip:
    """Simple tooltip widget for tkinter."""
    
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tooltip_window = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)
    
    def _show(self, event=None):
        if self.tooltip_window:
            return
        x, y, _, _ = self.widget.bbox("insert") if hasattr(self.widget, 'bbox') else (0, 0, 0, 0)
        x += self.widget.winfo_rootx() + 25
        y += self.widget.winfo_rooty() + 25
        
        self.tooltip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        
        label = tk.Label(
            tw, text=self.text, justify='left',
            background="#ffffe0", relief='solid', borderwidth=1,
            font=('Segoe UI', 9), padx=5, pady=3
        )
        label.pack()
    
    def _hide(self, event=None):
        if self.tooltip_window:
            self.tooltip_window.destroy()
            self.tooltip_window = None

class SettingsDialog(tk.Toplevel):
    """Dialog for app settings and Google Client Secrets."""
    
    def __init__(self, parent):
        super().__init__(parent)
        self.title("Settings")
        self.geometry("520x450")
        self.resizable(False, False)
        self.result = None
        
        self.transient(parent)
        self.grab_set()
        
        self._create_widgets()
        self.center_window()
    
    def center_window(self):
        self.update_idletasks()
        x = (self.winfo_screenwidth() - self.winfo_width()) // 2
        y = (self.winfo_screenheight() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")
    
    def _create_widgets(self):
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)
        
        # === App Settings Section ===
        ttk.Label(frame, text="App Settings", font=('Segoe UI', 12, 'bold')).pack(anchor='w')
        
        settings_frame = ttk.Frame(frame)
        settings_frame.pack(fill=tk.X, pady=(10, 15))
        
        # Load current settings
        current_settings = load_settings()
        
        self.show_log_var = tk.BooleanVar(value=current_settings.get('show_log_window', True))
        log_check = ttk.Checkbutton(
            settings_frame, 
            text="Show log window when launching via shortcut",
            variable=self.show_log_var
        )
        log_check.pack(anchor='w')
        ToolTip(log_check, "When enabled, shows window on startup. When disabled, starts minimized to system tray. Minimize window anytime to hide to tray.")
        
        # Default GDrive Folder
        gdrive_frame = ttk.Frame(settings_frame)
        gdrive_frame.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(gdrive_frame, text="Default GDrive Folder:").pack(side=tk.LEFT)
        self.default_gdrive_var = tk.StringVar(value=current_settings.get('default_gdrive_folder', ''))
        gdrive_entry = ttk.Entry(gdrive_frame, textvariable=self.default_gdrive_var, width=30)
        gdrive_entry.pack(side=tk.LEFT, padx=(5, 0))
        ToolTip(gdrive_entry, "Pre-filled when adding new games (e.g., SaveSync/Games)")
        
        ttk.Separator(frame, orient='horizontal').pack(fill=tk.X, pady=10)
        
        # === Google API Section ===
        ttk.Label(frame, text="Google API Client Secrets", font=('Segoe UI', 12, 'bold')).pack(anchor='w')
        ttk.Label(frame, text="Paste your client secrets JSON below:", wraplength=450).pack(anchor='w', pady=(5, 10))
        
        self.text = scrolledtext.ScrolledText(frame, height=10, width=55)
        self.text.pack(fill=tk.BOTH, expand=True)
        
        # Pre-populate with existing secrets if available
        existing_secrets = load_client_secrets()
        if existing_secrets:
            self.text.insert("1.0", json.dumps(existing_secrets, indent=2))
        
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, pady=(15, 0))
        
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=(5, 0))
        ttk.Button(btn_frame, text="Save", command=self._save).pack(side=tk.RIGHT)
    
    def _save(self):
        # Save app settings
        settings = {
            'show_log_window': self.show_log_var.get(),
            'default_gdrive_folder': self.default_gdrive_var.get().strip()
        }
        save_settings(settings)
        
        # Save client secrets if provided
        secrets_text = self.text.get("1.0", tk.END).strip()
        if secrets_text:
            try:
                data = json.loads(secrets_text)
                # Handle both wrapped and unwrapped formats
                if 'installed' in data:
                    data = data['installed']
                elif 'web' in data:
                    data = data['web']
                
                # Validate required fields
                required = ['client_id', 'client_secret']
                for field in required:
                    if field not in data:
                        raise ValueError(f"Missing required field: {field}")
                
                save_client_secrets(data)
            except json.JSONDecodeError:
                messagebox.showerror("Invalid JSON", "Please enter valid JSON data for Google API.")
                return
            except ValueError as e:
                messagebox.showerror("Invalid Data", str(e))
                return
        
        self.result = True
        self.destroy()


class GameConfigDialog(tk.Toplevel):
    """Dialog to add/edit a game configuration."""
    
    def __init__(self, parent, config=None):
        super().__init__(parent)
        self.title("Add Game" if config is None else "Edit Game")
        self.geometry("550x320")
        self.resizable(False, False)
        self.result = None
        self.config = config or {}
        
        self.transient(parent)
        self.grab_set()
        
        self._create_widgets()
        self.center_window()
    
    def center_window(self):
        self.update_idletasks()
        x = (self.winfo_screenwidth() - self.winfo_width()) // 2
        y = (self.winfo_screenheight() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")
    
    def _create_widgets(self):
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)
        
        # Game Name
        ttk.Label(frame, text="Game Name:").grid(row=0, column=0, sticky='w', pady=5)
        self.name_var = tk.StringVar(value=self.config.get('name', ''))
        ttk.Entry(frame, textvariable=self.name_var, width=50).grid(row=0, column=1, columnspan=2, sticky='w', pady=5)
        
        # Local Save Directory
        ttk.Label(frame, text="Save Directory:").grid(row=1, column=0, sticky='w', pady=5)
        self.save_dir_var = tk.StringVar(value=self.config.get('local_save_dir', ''))
        ttk.Entry(frame, textvariable=self.save_dir_var, width=40).grid(row=1, column=1, sticky='w', pady=5)
        ttk.Button(frame, text="Browse...", command=self._browse_save_dir).grid(row=1, column=2, padx=5)
        
        # Game Executable
        ttk.Label(frame, text="Game Executable:").grid(row=2, column=0, sticky='w', pady=5)
        self.exe_var = tk.StringVar(value=self.config.get('game_exe', ''))
        ttk.Entry(frame, textvariable=self.exe_var, width=40).grid(row=2, column=1, sticky='w', pady=5)
        ttk.Button(frame, text="Browse...", command=self._browse_exe).grid(row=2, column=2, padx=5)
        
        # Google Drive Folder - pre-fill with default for new games
        ttk.Label(frame, text="GDrive Folder:").grid(row=3, column=0, sticky='w', pady=5)
        default_gdrive = self.config.get('gdrive_folder', '')
        if not default_gdrive and not self.config:  # New game
            settings = load_settings()
            default_gdrive = settings.get('default_gdrive_folder', '')
        self.gdrive_var = tk.StringVar(value=default_gdrive)
        ttk.Entry(frame, textvariable=self.gdrive_var, width=50).grid(row=3, column=1, columnspan=2, sticky='w', pady=5)
        ttk.Label(frame, text="(e.g., SaveSync/GameName)", foreground='gray').grid(row=4, column=1, sticky='w')
        
        # Remote Zip Name
        ttk.Label(frame, text="Zip Filename:").grid(row=5, column=0, sticky='w', pady=5)
        self.zip_var = tk.StringVar(value=self.config.get('remote_zip_name', ''))
        ttk.Entry(frame, textvariable=self.zip_var, width=50).grid(row=5, column=1, columnspan=2, sticky='w', pady=5)
        
        # Buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=6, column=0, columnspan=3, pady=(20, 0), sticky='e')
        
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=(5, 0))
        ttk.Button(btn_frame, text="Save", command=self._save).pack(side=tk.RIGHT)
    
    def _browse_save_dir(self):
        path = filedialog.askdirectory(title="Select Save Directory")
        if path:
            self.save_dir_var.set(path)
    
    def _browse_exe(self):
        path = filedialog.askopenfilename(
            title="Select Game Executable",
            filetypes=[("Executable", "*.exe"), ("All files", "*.*")]
        )
        if path:
            self.exe_var.set(path)
            # Auto-fill name and zip if empty
            if not self.name_var.get():
                name = Path(path).stem.replace('_', ' ').replace('-', ' ').title()
                self.name_var.set(name)
            if not self.zip_var.get():
                self.zip_var.set(f"{Path(path).stem.lower()}.zip")
    
    def _save(self):
        name = self.name_var.get().strip()
        save_dir = self.save_dir_var.get().strip()
        exe = self.exe_var.get().strip()
        
        if not name:
            messagebox.showerror("Error", "Game name is required.")
            return
        if not save_dir:
            messagebox.showerror("Error", "Save directory is required.")
            return
        if not exe:
            messagebox.showerror("Error", "Game executable is required.")
            return
        
        # Generate ID from name
        game_id = name.lower().replace(' ', '_').replace('-', '_')
        game_id = ''.join(c for c in game_id if c.isalnum() or c == '_')
        
        self.result = {
            'id': self.config.get('id', game_id),
            'name': name,
            'local_save_dir': save_dir,
            'game_exe': exe,
            'gdrive_folder': self.gdrive_var.get().strip(),
            'remote_zip_name': self.zip_var.get().strip() or f"{game_id}.zip"
        }
        self.destroy()


class ConflictDialog(tk.Toplevel):
    """Dialog for resolving sync conflicts between local and cloud saves."""
    
    def __init__(self, parent, local_time, cloud_time):
        super().__init__(parent)
        self.title("Sync Conflict")
        self.geometry("450x250")
        self.resizable(False, False)
        self.result = None  # 'local', 'cloud', or 'cancel'
        
        self.transient(parent)
        self.grab_set()
        
        self._create_widgets(local_time, cloud_time)
        self.center_window()
        
        # Handle window close button
        self.protocol("WM_DELETE_WINDOW", self._cancel)
    
    def center_window(self):
        self.update_idletasks()
        x = (self.winfo_screenwidth() - self.winfo_width()) // 2
        y = (self.winfo_screenheight() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")
    
    def _create_widgets(self, local_time, cloud_time):
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)
        
        # Warning icon and title
        ttk.Label(frame, text="⚠️ Sync Conflict Detected", font=('Segoe UI', 12, 'bold')).pack(anchor='w')
        
        ttk.Label(
            frame,
            text="Your local saves are NEWER than the cloud saves.",
            wraplength=400,
            foreground='#cc6600'
        ).pack(anchor='w', pady=(10, 15))
        
        # Time comparison
        info_frame = ttk.Frame(frame)
        info_frame.pack(fill=tk.X, pady=5)
        
        ttk.Label(info_frame, text="💾 Local saves:", font=('Segoe UI', 9, 'bold')).grid(row=0, column=0, sticky='w')
        ttk.Label(info_frame, text=format_time(local_time)).grid(row=0, column=1, sticky='w', padx=10)
        
        ttk.Label(info_frame, text="☁️ Cloud saves:", font=('Segoe UI', 9, 'bold')).grid(row=1, column=0, sticky='w', pady=2)
        ttk.Label(info_frame, text=format_time(cloud_time)).grid(row=1, column=1, sticky='w', padx=10)
        
        ttk.Label(frame, text="What would you like to do?", font=('Segoe UI', 10)).pack(anchor='w', pady=(20, 10))
        
        # Buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, pady=(10, 0))
        
        ttk.Button(
            btn_frame, text="Keep Local", width=15,
            command=self._keep_local
        ).pack(side=tk.LEFT, padx=(0, 5))
        
        ttk.Button(
            btn_frame, text="Use Cloud", width=15,
            command=self._use_cloud
        ).pack(side=tk.LEFT, padx=5)
        
        ttk.Button(
            btn_frame, text="Cancel", width=10,
            command=self._cancel
        ).pack(side=tk.RIGHT)
    
    def _keep_local(self):
        self.result = 'local'
        self.destroy()
    
    def _use_cloud(self):
        self.result = 'cloud'
        self.destroy()
    
    def _cancel(self):
        self.result = 'cancel'
        self.destroy()


class SaveSyncApp(tk.Tk):
    """Main application window."""
    
    def __init__(self):
        super().__init__()
        
        self.title("SaveSync - Game Save Manager")
        self.geometry("600x450")
        self.minsize(500, 350)
        
        # Set window icon
        self._set_icon()
        
        self._setup_styles()
        self._create_widgets()
        self._check_client_secrets()
        self._refresh_games()
        self.center_window()
    
    def center_window(self):
        self.update_idletasks()
        x = (self.winfo_screenwidth() - self.winfo_width()) // 2
        y = (self.winfo_screenheight() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")
    
    def _set_icon(self):
        """Set window icon from icon.png."""
        try:
            # Try to find icon.png relative to script/exe location
            if getattr(sys, 'frozen', False):
                # Running as PyInstaller bundle
                icon_path = Path(sys._MEIPASS) / "icon.png"
            else:
                # Running as script
                icon_path = Path(__file__).parent / "icon.png"
            
            if icon_path.exists():
                icon = tk.PhotoImage(file=str(icon_path))
                self.iconphoto(True, icon)
                self._icon = icon  # Keep reference to prevent garbage collection
        except Exception:
            pass  # Ignore if icon loading fails
    
    def _setup_styles(self):
        style = ttk.Style()
        style.configure('Title.TLabel', font=('Segoe UI', 14, 'bold'))
        style.configure('Game.TFrame', padding=10)
    
    def _create_widgets(self):
        # Main container
        main = ttk.Frame(self, padding=15)
        main.pack(fill=tk.BOTH, expand=True)
        
        # Header
        header = ttk.Frame(main)
        header.pack(fill=tk.X, pady=(0, 15))
        
        ttk.Label(header, text=f"🎮 SaveSync v{VERSION}", style='Title.TLabel').pack(side=tk.LEFT)
        ttk.Button(header, text="⚙ Settings", command=self._show_settings).pack(side=tk.RIGHT)
        
        # Games list frame
        list_frame = ttk.LabelFrame(main, text="Configured Games", padding=10)
        list_frame.pack(fill=tk.BOTH, expand=True)
        
        # Scrollable game list
        self.canvas = tk.Canvas(list_frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.canvas.yview)
        self.games_frame = ttk.Frame(self.canvas)
        
        self.games_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        
        self.canvas_window = self.canvas.create_window((0, 0), window=self.games_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        
        # Bind canvas resize to update games_frame width
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # Bottom buttons
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill=tk.X, pady=(15, 0))
        
        add_btn = ttk.Button(btn_frame, text="+ Add Game", command=self._add_game)
        add_btn.pack(side=tk.LEFT)
        ToolTip(add_btn, "Add a new game to sync")
        
        sync_all_btn = ttk.Button(btn_frame, text="🔄 Sync All", command=self._sync_all)
        sync_all_btn.pack(side=tk.LEFT, padx=10)
        ToolTip(sync_all_btn, "Sync all games without launching")
    
    def _on_canvas_configure(self, event):
        """Update games_frame width when canvas is resized."""
        self.canvas.itemconfig(self.canvas_window, width=event.width)
    
    def _check_client_secrets(self):
        """Check if client secrets are configured, prompt if not."""
        if not load_client_secrets():
            messagebox.showinfo(
                "Welcome to SaveSync",
                "Before using SaveSync, you need to configure your Google API credentials.\n\n"
                "Please paste your client secrets JSON in the next dialog."
            )
            self._show_settings()
    
    def _show_settings(self):
        """Show settings dialog."""
        dialog = SettingsDialog(self)
        self.wait_window(dialog)
        
        if dialog.result:
            messagebox.showinfo("Success", "Settings saved successfully!")
    
    def _refresh_games(self):
        """Refresh the games list."""
        for widget in self.games_frame.winfo_children():
            widget.destroy()
        
        games = list_games()
        
        if not games:
            ttk.Label(
                self.games_frame,
                text="No games configured yet.\nClick '+ Add Game' to get started!",
                foreground='gray'
            ).pack(pady=30)
            return
        
        for game in games:
            self._create_game_row(game)
    
    def _create_game_row(self, game):
        """Create a row for a game in the list."""
        row = ttk.Frame(self.games_frame, style='Game.TFrame')
        row.pack(fill=tk.X, pady=2, expand=True)
        
        # Game info
        info = ttk.Frame(row)
        info.pack(side=tk.LEFT, fill=tk.X, expand=True)
        
        ttk.Label(info, text=f"🎮 {game['name']}", font=('Segoe UI', 10, 'bold')).pack(anchor='w')
        ttk.Label(info, text=game.get('gdrive_folder', 'Root'), foreground='gray').pack(anchor='w')
        
        # Buttons with tooltips
        btn_frame = ttk.Frame(row)
        btn_frame.pack(side=tk.RIGHT)
        
        play_btn = ttk.Button(btn_frame, text="▶ Play", width=8,
                              command=lambda g=game: self._sync_and_play(g))
        play_btn.pack(side=tk.LEFT, padx=2)
        ToolTip(play_btn, "Download saves, launch game, upload after closing")
        
        sync_btn = ttk.Button(btn_frame, text="🔄 Sync", width=8,
                              command=lambda g=game: self._sync_only(g))
        sync_btn.pack(side=tk.LEFT, padx=2)
        ToolTip(sync_btn, "Sync saves without launching game")
        
        edit_btn = ttk.Button(btn_frame, text="✏", width=3,
                              command=lambda g=game: self._edit_game(g))
        edit_btn.pack(side=tk.LEFT, padx=2)
        ToolTip(edit_btn, "Edit game configuration")
        
        shortcut_btn = ttk.Button(btn_frame, text="🔗", width=3,
                                  command=lambda g=game: self._create_shortcut(g))
        shortcut_btn.pack(side=tk.LEFT, padx=2)
        ToolTip(shortcut_btn, "Create desktop shortcut")
        
        delete_btn = ttk.Button(btn_frame, text="🗑", width=3,
                                command=lambda g=game: self._delete_game(g))
        delete_btn.pack(side=tk.LEFT, padx=2)
        ToolTip(delete_btn, "Remove game from SaveSync")
        
        ttk.Separator(self.games_frame, orient='horizontal').pack(fill=tk.X, pady=5)
    
    def _add_game(self):
        """Add a new game."""
        dialog = GameConfigDialog(self)
        self.wait_window(dialog)
        
        if dialog.result:
            game_id = dialog.result.pop('id')
            save_game_config(game_id, dialog.result)
            self._refresh_games()
    
    def _edit_game(self, game):
        """Edit an existing game."""
        dialog = GameConfigDialog(self, game)
        self.wait_window(dialog)
        
        if dialog.result:
            game_id = dialog.result.pop('id')
            save_game_config(game_id, dialog.result)
            self._refresh_games()
    
    def _delete_game(self, game):
        """Delete a game configuration."""
        if messagebox.askyesno("Confirm Delete", f"Delete '{game['name']}' from SaveSync?\n\n(This won't delete your save files)"):
            delete_game_config(game['id'])
            self._refresh_games()
    
    def _create_shortcut(self, game):
        """Create a desktop shortcut for the game."""
        try:
            path = create_desktop_shortcut(game['id'], game)
            messagebox.showinfo("Shortcut Created", f"Desktop shortcut created:\n{path}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to create shortcut:\n{e}")
    
    def _sync_and_play(self, game):
        """Sync and launch a game (opens in console window)."""
        self._run_sync_window(game['id'], run_game=True)
    
    def _sync_only(self, game):
        """Sync a game without launching."""
        self._run_sync_window(game['id'], run_game=False)
    
    def _sync_all(self):
        """Sync all games."""
        self._run_sync_window(None, run_game=False, sync_all=True)
    
    def _run_sync_window(self, game_id, run_game=False, sync_all=False):
        """Open a sync progress window."""
        win = tk.Toplevel(self)
        win.title("Syncing...")
        win.geometry("500x300")
        win.transient(self)
        
        text = scrolledtext.ScrolledText(win, state='disabled', font=('Consolas', 10))
        text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        def log(msg):
            text.configure(state='normal')
            text.insert(tk.END, msg + '\n')
            text.see(tk.END)
            text.configure(state='disabled')
            win.update()
        
        def gui_conflict_callback(local_time, cloud_time):
            """GUI callback for conflict resolution."""
            dialog = ConflictDialog(win, local_time, cloud_time)
            win.wait_window(dialog)
            return dialog.result or 'cancel'
        
        def run():
            try:
                if sync_all:
                    sync_all_games(log_func=log, conflict_callback=gui_conflict_callback)
                else:
                    sync_game(game_id, run_game=run_game, log_func=log, conflict_callback=gui_conflict_callback)
                log("\n✅ Done!")
            except Exception as e:
                log(f"\n❌ Error: {e}")
            
            ttk.Button(win, text="Close", command=win.destroy).pack(pady=10)
        
        win.after(100, run)


# ============================================================
# CLI MODE
# ============================================================

def run_cli_with_gui_window(game_id, sync_only=False, start_minimized=False):
    """Run CLI mode with a GUI log window and system tray support."""
    root = tk.Tk()
    root.title(f"SaveSync - {game_id}")
    root.geometry("550x350")
    
    # Variables to track state
    tray_icon = None
    is_visible = not start_minimized
    sync_complete = False
    
    # Load icon for both window and tray
    icon_image = None
    try:
        if getattr(sys, 'frozen', False):
            icon_path = Path(sys._MEIPASS) / "icon.png"
            ico_path = Path(sys._MEIPASS) / "icon.ico"
        else:
            icon_path = Path(__file__).parent / "icon.png"
            ico_path = Path(__file__).parent / "icon.ico"
        
        if icon_path.exists():
            tk_icon = tk.PhotoImage(file=str(icon_path))
            root.iconphoto(True, tk_icon)
            root._icon = tk_icon  # Keep reference
            icon_image = Image.open(str(icon_path))
        elif ico_path.exists():
            icon_image = Image.open(str(ico_path))
    except Exception:
        pass
    
    # Create a default icon if none loaded
    if icon_image is None:
        icon_image = Image.new('RGB', (64, 64), color='#4a90d9')
    
    # Center window
    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_width()) // 2
    y = (root.winfo_screenheight() - root.winfo_height()) // 2
    root.geometry(f"+{x}+{y}")
    
    text = scrolledtext.ScrolledText(root, state='disabled', font=('Consolas', 10))
    text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
    
    def log(msg):
        text.configure(state='normal')
        text.insert(tk.END, msg + '\n')
        text.see(tk.END)
        text.configure(state='disabled')
        try:
            root.update()
        except tk.TclError:
            pass  # Window may be destroyed
    
    def show_window():
        """Show the window from tray."""
        nonlocal is_visible
        root.deiconify()
        root.lift()
        root.focus_force()
        is_visible = True
    
    def hide_to_tray():
        """Hide window to system tray."""
        nonlocal is_visible
        root.withdraw()
        is_visible = False
    
    def quit_app():
        """Quit the application."""
        nonlocal tray_icon
        if tray_icon:
            tray_icon.stop()
        root.quit()
        root.destroy()
    
    def on_tray_click(icon, item):
        """Handle tray icon click."""
        root.after(0, show_window)
    
    def on_minimize(event):
        """Handle window minimize - hide to tray instead."""
        if root.state() == 'iconic':
            root.after(10, hide_to_tray)
    
    # Create system tray icon
    tray_menu = pystray.Menu(
        pystray.MenuItem("Show", on_tray_click, default=True),
        pystray.MenuItem("Quit", lambda icon, item: root.after(0, quit_app))
    )
    
    tray_icon = pystray.Icon(
        "SaveSync",
        icon_image.resize((64, 64)),
        f"SaveSync - {game_id}",
        tray_menu
    )
    
    # Run tray icon in separate thread
    def run_tray():
        tray_icon.run()
    
    tray_thread = threading.Thread(target=run_tray, daemon=True)
    tray_thread.start()
    
    # Bind minimize event
    root.bind('<Unmap>', on_minimize)
    
    # Handle window close button
    def on_close():
        if sync_complete:
            quit_app()
        else:
            hide_to_tray()
    
    root.protocol("WM_DELETE_WINDOW", on_close)
    
    # Thread-safe variables for conflict dialog
    conflict_event = threading.Event()
    conflict_result = [None]  # Use list to allow modification from nested function
    conflict_data = [None, None]  # local_time, cloud_time
    
    def check_conflict_request():
        """Check if background thread needs conflict dialog (runs on main thread)."""
        if conflict_data[0] is not None:
            local_time, cloud_time = conflict_data
            conflict_data[0] = None
            conflict_data[1] = None
            
            # Make sure window is visible for conflict dialog
            show_window()
            dialog = ConflictDialog(root, local_time, cloud_time)
            root.wait_window(dialog)
            conflict_result[0] = dialog.result or 'cancel'
            conflict_event.set()
        
        # Keep checking while sync is running
        if not sync_complete:
            root.after(100, check_conflict_request)
    
    def gui_conflict_callback(local_time, cloud_time):
        """GUI callback for conflict resolution (called from background thread)."""
        conflict_event.clear()
        conflict_data[0] = local_time
        conflict_data[1] = cloud_time
        # Wait for main thread to handle the dialog
        conflict_event.wait()
        return conflict_result[0]
    
    def thread_safe_log(msg):
        """Log message in a thread-safe way."""
        root.after(0, lambda: log(msg))
    
    def run_sync():
        nonlocal sync_complete
        try:
            thread_safe_log(f"SaveSync - Syncing: {game_id}")
            thread_safe_log("=" * 40)
            result = sync_game(game_id, run_game=not sync_only, log_func=thread_safe_log, conflict_callback=gui_conflict_callback)
            if result:
                thread_safe_log("\n✅ Done!")
        except Exception as e:
            thread_safe_log(f"\n❌ Error: {e}")
        
        sync_complete = True
        # Add close button after sync completes (on main thread)
        root.after(0, lambda: ttk.Button(root, text="Close", command=quit_app).pack(pady=10))
        
        # Show window when sync is complete (so user can see the result)
        root.after(0, show_window)
    
    # Start minimized if requested
    if start_minimized:
        root.withdraw()
    
    # Start conflict dialog checker
    root.after(100, check_conflict_request)
    
    # Run sync in background thread
    sync_thread = threading.Thread(target=run_sync, daemon=True)
    sync_thread.start()
    root.mainloop()
    
    # Cleanup tray icon
    if tray_icon:
        try:
            tray_icon.stop()
        except Exception:
            pass


def cli_mode(game_id):
    """Run in CLI mode - sync and launch game directly (console output)."""
    print(f"SaveSync - Launching: {game_id}")
    print("=" * 40)
    
    try:
        sync_game(game_id, run_game=True)
    except Exception as e:
        print(f"\n[ERROR] {e}")
        input("Press Enter to close...")
        sys.exit(1)


# ============================================================
# MAIN ENTRY POINT
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="SaveSync - Game Save Synchronization")
    parser.add_argument('--game', '-g', help='Game ID to sync and launch (CLI mode)')
    parser.add_argument('--sync-only', action='store_true', help='Only sync, do not launch game')
    args = parser.parse_args()
    
    ensure_app_dirs()
    
    if args.game:
        # CLI mode - always use tray-enabled window
        # Setting controls whether window starts visible or minimized
        settings = load_settings()
        start_minimized = not settings.get('show_log_window', True)
        run_cli_with_gui_window(args.game, sync_only=args.sync_only, start_minimized=start_minimized)
    else:
        # GUI mode
        app = SaveSyncApp()
        app.mainloop()


if __name__ == "__main__":
    main()

