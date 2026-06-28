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
import logging
import tempfile
import re
import time
import ssl
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from pathlib import Path
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError
from urllib.parse import quote
# Heavy imports (pydrive2, pystray, PIL) are lazy-loaded inside
# get_drive() and run_cli_with_gui_window() for faster startup.

# --- CONSTANTS ---
VERSION = "1.4.2"
APP_NAME = "saveSync"
APP_DATA_DIR = Path(os.getenv('APPDATA')) / APP_NAME
GAMES_DIR = APP_DATA_DIR / "games"
CLIENT_SECRETS_FILE = APP_DATA_DIR / "client_secrets.json"
CREDENTIALS_FILE = APP_DATA_DIR / "credentials.txt"
SETTINGS_FILE = APP_DATA_DIR / "settings.json"
DEBUG_LOG_FILE = APP_DATA_DIR / "debug.log"

# --- AUTO-UPDATE ---
GITHUB_REPO = "BernaLang/savesync"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
UPDATE_DIR = APP_DATA_DIR / "update_temp"

# --- DEBUG LOGGING SETUP ---
def setup_debug_logging():
    """Set up file-based debug logging."""
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger('savesync')
    logger.setLevel(logging.DEBUG)
    # Remove existing handlers to avoid duplicates
    logger.handlers.clear()
    fh = logging.FileHandler(str(DEBUG_LOG_FILE), mode='a', encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    return logger

debug_log = setup_debug_logging()

# Default settings
DEFAULT_SETTINGS = {
    "show_log_window": True,  # Show GUI log window in CLI/shortcut mode
    "default_gdrive_folder": "",  # Default GDrive folder path for new games
    "skipped_update_version": "",  # Version the user chose to skip
    "device_gamelist_sv_path": ""  # GDrive path to save gamelist JSON (e.g., SaveSync/device1.json)
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
    from pydrive2.auth import GoogleAuth
    from pydrive2.drive import GoogleDrive

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
        # Parse the modifiedDate from Google Drive (UTC / ISO format)
        modified_str = cloud_file.get('modifiedDate', '')
        debug_log.info(f"Raw cloud modifiedDate: {modified_str!r}")
        if modified_str:
            # Format: 2024-01-25T10:30:00.000Z  (always UTC)
            try:
                # Parse as UTC, then convert to local time for comparison
                cloud_time_utc = datetime.strptime(modified_str[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
                cloud_time_local = cloud_time_utc.astimezone().replace(tzinfo=None)
                debug_log.info(f"Cloud time UTC: {cloud_time_utc}, Local: {cloud_time_local}")
                return cloud_time_local, cloud_file
            except ValueError:
                pass
        return None, cloud_file
    return None, None


def format_time(dt):
    """Format datetime for display."""
    if dt is None:
        return "No saves found"
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def _is_transient_error(exc):
    """Check if an exception is a transient network/SSL error worth retrying."""
    transient_phrases = [
        'eof occurred',
        'ssl',
        'connection reset',
        'connection aborted',
        'broken pipe',
        'timed out',
        'timeout',
        'connection refused',
        'temporary failure',
        'name resolution',
    ]
    msg = str(exc).lower()
    if any(phrase in msg for phrase in transient_phrases):
        return True
    if isinstance(exc, (ssl.SSLError, ConnectionError, TimeoutError, OSError)):
        return True
    return False


def _retry_drive_op(operation, max_retries=3, log_func=print, op_name="operation"):
    """
    Retry a Google Drive operation with exponential backoff on transient errors.
    
    operation: callable that performs the drive operation.
    Returns the result of operation() on success.
    Raises the last exception if all retries are exhausted.
    """
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            return operation()
        except Exception as e:
            last_exc = e
            if _is_transient_error(e) and attempt < max_retries:
                wait = 2 ** attempt  # 2s, 4s, 8s
                log_func(f"⚠️ {op_name} failed (attempt {attempt}/{max_retries}): {e}")
                log_func(f"   Retrying in {wait}s...")
                debug_log.warning(f"{op_name} attempt {attempt} failed: {e}. Retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise
    raise last_exc


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
        _retry_drive_op(
            lambda: file_list[0].GetContentFile(str(temp_zip)),
            log_func=log_func,
            op_name="Download"
        )
        
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

    def _do_upload():
        f.Upload()
        f.content = None

    _retry_drive_op(_do_upload, log_func=log_func, op_name="Upload")
    
    temp_zip.unlink()
    log_func("Sync Successful! Cloud is up to date.")


def _sync_op_with_error_retry(operation, error_callback, log_func=print, op_name="Sync"):
    """
    Run a sync operation, and if it fails, invoke error_callback to ask the user
    whether to retry or cancel. Loops until success or the user cancels.
    
    operation: callable that performs the sync operation.
    error_callback: Optional function(error_message, op_name) -> 'retry' | 'cancel'
                    If None, the error is raised immediately.
    """
    while True:
        try:
            operation()
            return  # Success
        except Exception as e:
            debug_log.error(f"{op_name} failed: {type(e).__name__}: {e}", exc_info=True)
            if error_callback:
                log_func(f"\n❌ {op_name} error: {e}")
                choice = error_callback(str(e), op_name)
                if choice == 'retry':
                    log_func(f"Retrying {op_name.lower()}...")
                    debug_log.info(f"User chose to retry {op_name}")
                    continue
                else:
                    log_func(f"{op_name} cancelled by user.")
                    debug_log.info(f"User cancelled {op_name} after error")
                    return
            else:
                raise


def sync_game(game_id, run_game=True, log_func=print, conflict_callback=None, error_callback=None):
    """
    Sync a game's saves and optionally run the game.
    
    conflict_callback: Optional function(local_time, cloud_time) -> 'local' | 'cloud' | 'cancel'
                       If not provided, defaults to CLI prompt or auto-download.
    error_callback: Optional function(error_message, op_name) -> 'retry' | 'cancel'
                    If provided, sync errors show a retry dialog instead of raising.
    """
    debug_log.info(f"sync_game called: game_id={game_id!r}, run_game={run_game}")
    config = load_game_config(game_id)
    if not config:
        debug_log.error(f"Game config not found for game_id={game_id!r}")
        raise Exception(f"Game '{game_id}' not found.")
    
    debug_log.info(f"Game config loaded: {json.dumps(config, indent=2)}")
    
    drive = get_drive()
    log_func("Authentication successful.")
    debug_log.info("Google Drive authentication successful")
    
    folder_id = get_or_create_folder(drive, config.get('gdrive_folder', ''))
    remote_zip_name = config.get('remote_zip_name', 'saves.zip')
    local_save_dir = config['local_save_dir']
    
    # Get modification times
    local_time = get_local_save_time(local_save_dir)
    cloud_time, cloud_file = get_cloud_save_info(drive, folder_id, remote_zip_name)
    
    log_func(f"Local saves: {format_time(local_time)}")
    log_func(f"Cloud saves: {format_time(cloud_time)}")
    debug_log.info(f"Local saves: {format_time(local_time)}, Cloud saves: {format_time(cloud_time)}")
    
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
        _sync_op_with_error_retry(
            lambda: download_and_extract(drive, folder_id, config, log_func),
            error_callback, log_func, op_name="Download"
        )
    
    if run_game:
        game_exe = config['game_exe']
        log_func(f"\nStarting Game: {game_exe}")
        log_func("Waiting for game to close...")
        
        debug_log.info(f"About to launch game exe: {game_exe!r}")
        debug_log.info(f"game_exe type: {type(game_exe).__name__}")
        debug_log.info(f"game_exe exists: {os.path.exists(game_exe)}")
        debug_log.info(f"game_exe is file: {os.path.isfile(game_exe) if os.path.exists(game_exe) else 'N/A'}")
        debug_log.info(f"game_exe absolute: {os.path.abspath(game_exe)}")
        debug_log.info(f"Current working dir: {os.getcwd()}")
        
        # Check if exe path has forward slashes and try to normalize
        normalized_exe = os.path.normpath(game_exe)
        debug_log.info(f"Normalized exe path: {normalized_exe!r}")
        debug_log.info(f"Normalized exists: {os.path.exists(normalized_exe)}")
        
        try:
            game_cwd = os.path.dirname(os.path.abspath(game_exe))
            debug_log.info(f"Calling subprocess.Popen({game_exe!r}, cwd={game_cwd!r})")
            process = subprocess.Popen(game_exe, cwd=game_cwd)
            debug_log.info(f"Popen returned, PID={process.pid}")
            log_func(f"Game process started (PID: {process.pid})")
            
            debug_log.info("Waiting for game process to exit...")
            exit_code = process.wait()
            debug_log.info(f"Game process exited with code: {exit_code}")
            log_func(f"Game exited with code: {exit_code}")
        except FileNotFoundError as e:
            debug_log.error(f"FileNotFoundError launching game: {e}")
            log_func(f"\n❌ Game executable not found: {game_exe}")
            raise
        except OSError as e:
            debug_log.error(f"OSError launching game: {e}")
            log_func(f"\n❌ OS error launching game: {e}")
            raise
        except Exception as e:
            debug_log.error(f"Unexpected error launching game: {type(e).__name__}: {e}")
            log_func(f"\n❌ Error launching game: {e}")
            raise
        
        log_func("\nGame closed. Uploading saves...")
        # Re-authenticate to get a fresh connection - the old one may have
        # gone stale during the game session (causes SSL EOF errors).
        try:
            drive = get_drive()
            folder_id = get_or_create_folder(drive, config.get('gdrive_folder', ''))
            debug_log.info("Re-authenticated Google Drive for post-game upload")
        except Exception as e:
            debug_log.warning(f"Re-auth failed, using existing connection: {e}")
        # Always upload after game closes
        _sync_op_with_error_retry(
            lambda: zip_and_upload(drive, folder_id, config, log_func),
            error_callback, log_func, op_name="Upload"
        )
    else:
        # Sync-only mode: upload only if we didn't download (local is newer or no cloud saves)
        if not should_download:
            _sync_op_with_error_retry(
                lambda: zip_and_upload(drive, folder_id, config, log_func),
                error_callback, log_func, op_name="Upload"
            )
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


def sync_all_games(log_func=print, conflict_callback=None, error_callback=None):
    """Sync all configured games (download and upload only, no game launch)."""
    games = list_games()
    if not games:
        log_func("No games configured.")
        return
    
    for game in games:
        log_func(f"\n=== Syncing: {game['name']} ===")
        try:
            sync_game(game['id'], run_game=False, log_func=log_func, conflict_callback=conflict_callback, error_callback=error_callback)
        except Exception as e:
            log_func(f"Error syncing {game['name']}: {e}")


def upload_gamelist_to_gdrive(log_func=print):
    """Upload the full games list JSON to Google Drive at the configured path."""
    settings = load_settings()
    sv_path = settings.get('device_gamelist_sv_path', '').strip()
    if not sv_path:
        return

    debug_log.info(f"Uploading gamelist to GDrive path: {sv_path}")

    try:
        drive = get_drive()

        # Split into folder path and filename
        normalized = sv_path.replace('\\', '/').strip('/')
        if '/' in normalized:
            folder_path = normalized.rsplit('/', 1)[0]
            filename = normalized.rsplit('/', 1)[1]
        else:
            folder_path = ''
            filename = normalized

        # Collect all game configs
        games = list_games()
        gamelist_data = []
        for game in games:
            gamelist_data.append({
                'id': game.get('id', ''),
                'name': game.get('name', ''),
                'local_save_dir': game.get('local_save_dir', ''),
                'game_exe': game.get('game_exe', ''),
                'gdrive_folder': game.get('gdrive_folder', ''),
                'remote_zip_name': game.get('remote_zip_name', '')
            })

        json_content = json.dumps(gamelist_data, indent=2)

        # Get or create the folder on GDrive
        folder_id = get_or_create_folder(drive, folder_path)

        # Check if file already exists
        query = f"title = '{filename}' and '{folder_id}' in parents and trashed = false"
        file_list = drive.ListFile({'q': query}).GetList()

        if file_list:
            f = file_list[0]
        else:
            f = drive.CreateFile({'title': filename, 'parents': [{'id': folder_id}]})

        f.SetContentString(json_content)
        f.Upload()

        log_func(f"Gamelist saved to GDrive: {sv_path}")
        debug_log.info(f"Gamelist uploaded successfully to {sv_path}")
    except Exception as e:
        log_func(f"\u26a0\ufe0f Failed to upload gamelist: {e}")
        debug_log.error(f"Gamelist upload failed: {e}", exc_info=True)


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
# AUTO-UPDATE FUNCTIONS
# ============================================================

def parse_version(version_str):
    """Parse a version string like '1.2.3' or 'v1.2.3' into a comparable tuple."""
    v = version_str.strip().lstrip('v')
    parts = []
    for part in v.split('.'):
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def check_for_update():
    """
    Check GitHub for a newer release.
    Returns (new_version, download_url, release_notes) or None.
    """
    try:
        req = Request(GITHUB_API_URL, headers={'User-Agent': f'SaveSync/{VERSION}'})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        
        tag = data.get('tag_name', '')
        remote_version = parse_version(tag)
        local_version = parse_version(VERSION)
        
        if remote_version > local_version:
            # Find the zip asset
            download_url = None
            for asset in data.get('assets', []):
                name = asset.get('name', '')
                if name.endswith('.zip'):
                    download_url = asset.get('browser_download_url')
                    break
            
            if download_url:
                release_notes = data.get('body', '') or ''
                new_version = tag.lstrip('v')
                debug_log.info(f"Update available: {VERSION} -> {new_version}")
                return new_version, download_url, release_notes
        
        debug_log.info(f"No update available (local={VERSION}, remote={tag})")
        return None
    except Exception as e:
        debug_log.warning(f"Update check failed: {e}")
        return None


def download_and_apply_update(download_url, new_version, log_func=print):
    """
    Download the update zip, extract it, and create a batch script
    that replaces the current exe + _internal folder after exit.
    """
    if not getattr(sys, 'frozen', False):
        log_func("Auto-update is only supported for the built EXE.")
        return False
    
    current_exe = Path(sys.executable)
    current_dir = current_exe.parent
    
    try:
        # Clean up any previous update attempt
        if UPDATE_DIR.exists():
            shutil.rmtree(UPDATE_DIR)
        UPDATE_DIR.mkdir(parents=True, exist_ok=True)
        
        # Download the zip
        zip_path = UPDATE_DIR / f"SaveSync_v{new_version}.zip"
        log_func(f"Downloading SaveSync v{new_version}...")
        debug_log.info(f"Downloading update from {download_url}")
        
        req = Request(download_url, headers={'User-Agent': f'SaveSync/{VERSION}'})
        with urlopen(req, timeout=120) as resp:
            total = int(resp.headers.get('Content-Length', 0))
            downloaded = 0
            with open(zip_path, 'wb') as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = int(downloaded / total * 100)
                        log_func(f"Downloading... {pct}%")
        
        log_func("Download complete. Extracting...")
        
        # Extract zip
        extract_dir = UPDATE_DIR / "extracted"
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(extract_dir)
        
        # Find the inner folder
        source_dir = None
        for item in extract_dir.iterdir():
            if item.is_dir() and (item / "SaveSync.exe").exists():
                source_dir = item
                break
        
        if source_dir is None:
            # Maybe the exe is directly in extract_dir
            if (extract_dir / "SaveSync.exe").exists():
                source_dir = extract_dir
            else:
                log_func("❌ Could not find SaveSync.exe in the update package.")
                debug_log.error(f"Update extraction failed: no SaveSync.exe found in {extract_dir}")
                return False
        
        log_func("Preparing update...")
        debug_log.info(f"Update source dir: {source_dir}")
        debug_log.info(f"Current exe dir: {current_dir}")
        
        # Create batch script to replace files after this process exits
        bat_path = UPDATE_DIR / "update.bat"
        bat_content = f'''@echo off
echo Waiting for SaveSync to close...
:waitloop
tasklist /FI "PID eq {os.getpid()}" 2>NUL | find "{os.getpid()}" >NUL
if not errorlevel 1 (
    timeout /t 1 /nobreak >NUL
    goto waitloop
)
echo Applying update...
xcopy /E /Y /I "{source_dir}\\*" "{current_dir}\\"
echo Update complete! Starting SaveSync...
start "" "{current_exe}"
echo Cleaning up...
rd /S /Q "{UPDATE_DIR}"
(goto) 2>nul & del "%~f0"
'''
        with open(bat_path, 'w') as f:
            f.write(bat_content)
        
        log_func("Launching updater and restarting...")
        debug_log.info(f"Launching update batch script: {bat_path}")
        
        # Launch the batch script (hidden window)
        subprocess.Popen(
            ['cmd', '/c', str(bat_path)],
            creationflags=subprocess.CREATE_NO_WINDOW,
            cwd=str(UPDATE_DIR)
        )
        
        # Exit the current process
        debug_log.info("Exiting for update...")
        sys.exit(0)
        
    except SystemExit:
        raise  # Don't catch sys.exit()
    except Exception as e:
        log_func(f"❌ Update failed: {e}")
        debug_log.error(f"Update failed: {e}", exc_info=True)
        return False


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
        self.geometry("520x500")
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
        
        # Device Gamelist SV Path
        gamelist_frame = ttk.Frame(settings_frame)
        gamelist_frame.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(gamelist_frame, text="Device Gamelist SV Path:").pack(side=tk.LEFT)
        self.gamelist_sv_var = tk.StringVar(value=current_settings.get('device_gamelist_sv_path', ''))
        gamelist_entry = ttk.Entry(gamelist_frame, textvariable=self.gamelist_sv_var, width=30)
        gamelist_entry.pack(side=tk.LEFT, padx=(5, 0))
        ToolTip(gamelist_entry, "GDrive path to save the games list config (must end in .json, e.g., SaveSync/my_device.json)")
        
        # Check for Updates button
        update_frame = ttk.Frame(settings_frame)
        update_frame.pack(fill=tk.X, pady=(15, 0))
        update_btn = ttk.Button(
            update_frame, text="🔄 Check for Updates",
            command=self._check_for_updates
        )
        update_btn.pack(anchor='w')
        self._update_btn = update_btn
        ToolTip(update_btn, f"Current version: v{VERSION}. Check GitHub for a newer release.")
        
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
    
    def _check_for_updates(self):
        """Trigger manual update check from settings dialog."""
        # Delegate to the main app's manual check method
        parent = self.master
        if hasattr(parent, '_manual_check_for_update'):
            self.destroy()
            parent._manual_check_for_update()
    
    def _save(self):
        # Validate device gamelist sv path
        gamelist_path = self.gamelist_sv_var.get().strip()
        if gamelist_path and not gamelist_path.lower().endswith('.json'):
            messagebox.showerror("Invalid Path", "Device Gamelist SV Path must end with .json")
            return
        
        # Save app settings (preserve skipped_update_version)
        current = load_settings()
        settings = {
            'show_log_window': self.show_log_var.get(),
            'default_gdrive_folder': self.default_gdrive_var.get().strip(),
            'skipped_update_version': current.get('skipped_update_version', ''),
            'device_gamelist_sv_path': gamelist_path
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


# ============================================================
# PCGAMINGWIKI AUTOCOMPLETE
# ============================================================

# Mapping of PCGamingWiki {{p|...}} path variables to Windows equivalents
_PCGW_PATH_MAP = {
    'appdata':              os.environ.get('APPDATA', ''),
    'localappdata':         os.environ.get('LOCALAPPDATA', ''),
    'userprofile':          os.environ.get('USERPROFILE', ''),
    'userprofile\\documents': os.path.join(os.environ.get('USERPROFILE', ''), 'Documents'),
    'userprofile/documents': os.path.join(os.environ.get('USERPROFILE', ''), 'Documents'),
    'userprofile\\appdata\\locallow': os.path.join(os.environ.get('USERPROFILE', ''), 'AppData', 'LocalLow'),
    'userprofile/appdata/locallow': os.path.join(os.environ.get('USERPROFILE', ''), 'AppData', 'LocalLow'),
    'userprofile\\appdata\\roaming': os.environ.get('APPDATA', ''),
    'userprofile/appdata/roaming': os.environ.get('APPDATA', ''),
    'userprofile\\appdata\\local': os.environ.get('LOCALAPPDATA', ''),
    'userprofile/appdata/local': os.environ.get('LOCALAPPDATA', ''),
    'userprofile\\saved games': os.path.join(os.environ.get('USERPROFILE', ''), 'Saved Games'),
    'userprofile/saved games': os.path.join(os.environ.get('USERPROFILE', ''), 'Saved Games'),
    'public':               os.environ.get('PUBLIC', ''),
    'programdata':          os.environ.get('PROGRAMDATA', ''),
    'programfiles':         os.environ.get('PROGRAMFILES', ''),
    'programfiles(x86)':    os.environ.get('PROGRAMFILES(X86)', ''),
    'windir':               os.environ.get('WINDIR', ''),
    'username':             os.environ.get('USERNAME', ''),
}

_PCGW_API_BASE = 'https://www.pcgamingwiki.com/w/api.php'


def _pcgw_api_request(params):
    """Make a GET request to the PCGamingWiki MediaWiki API."""
    query = '&'.join(f"{k}={quote(str(v))}" for k, v in params.items())
    url = f"{_PCGW_API_BASE}?{query}"
    req = Request(url, headers={'User-Agent': f'SaveSync/{VERSION}'})
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode('utf-8'))


def pcgw_search_game(query):
    """
    Search PCGamingWiki for games matching the query string.
    Returns a list of page titles.
    """
    try:
        data = _pcgw_api_request({
            'action': 'opensearch',
            'search': query,
            'limit': '10',
            'format': 'json',
        })
        # OpenSearch returns: [query, [titles], [descriptions], [urls]]
        if isinstance(data, list) and len(data) >= 2:
            return data[1]
        return []
    except Exception as e:
        debug_log.warning(f"PCGamingWiki search failed: {e}")
        return []


def _pcgw_resolve_path(wiki_path):
    """
    Resolve a PCGamingWiki wikitext path to a real Windows path.
    Handles {{p|appdata}}, {{p|userprofile}}, {{p|uid}} etc.
    """
    def _replace_var(match):
        var_name = match.group(1).strip().lower()
        if var_name == 'uid':
            return '<steamid>'
        if var_name == 'steam':
            return _detect_steam_path()
        return _PCGW_PATH_MAP.get(var_name, match.group(0))

    resolved = re.sub(r'\{\{p\|([^}]+)\}\}', _replace_var, wiki_path, flags=re.IGNORECASE)
    # Normalize slashes
    resolved = resolved.replace('/', '\\')
    # Remove trailing backslash
    resolved = resolved.rstrip('\\')
    return resolved


def _detect_steam_path():
    """Try to detect the Steam installation path from the registry or common locations."""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam')
        steam_path, _ = winreg.QueryValueEx(key, 'SteamPath')
        winreg.CloseKey(key)
        return str(Path(steam_path))
    except Exception:
        pass
    # Fallback to common paths
    for candidate in [
        Path(os.environ.get('PROGRAMFILES(X86)', '')) / 'Steam',
        Path(os.environ.get('PROGRAMFILES', '')) / 'Steam',
        Path('C:/Program Files (x86)/Steam'),
    ]:
        if candidate.exists():
            return str(candidate)
    return '<steam>'


def pcgw_get_save_path(page_title):
    """
    Fetch the Windows save game path for a game from PCGamingWiki.
    Returns the resolved path string, or None if not found.
    """
    try:
        # Step 1: Get page sections to find "Save game data location"
        sections_data = _pcgw_api_request({
            'action': 'parse',
            'page': page_title,
            'prop': 'sections',
            'format': 'json',
        })

        sections = sections_data.get('parse', {}).get('sections', [])
        save_section_index = None
        for section in sections:
            if section.get('line', '').lower().strip() == 'save game data location':
                save_section_index = section.get('index')
                break

        if save_section_index is None:
            debug_log.info(f"No 'Save game data location' section found for {page_title}")
            return None

        # Step 2: Fetch that section's wikitext
        wikitext_data = _pcgw_api_request({
            'action': 'parse',
            'page': page_title,
            'prop': 'wikitext',
            'section': save_section_index,
            'format': 'json',
        })

        wikitext = wikitext_data.get('parse', {}).get('wikitext', {}).get('*', '')
        if not wikitext:
            return None

        # Step 3: Parse Windows save paths from {{Game data/saves|Windows|...}}
        # The template contains nested {{p|...}} so we can't use a simple regex.
        # Instead, find the start marker and then manually balance braces.
        marker = '{{Game data/saves|Windows|'
        marker_lower = marker.lower()
        wikitext_lower = wikitext.lower()
        start = wikitext_lower.find(marker_lower)
        if start == -1:
            return None

        # Position right after the marker (start of the paths content)
        content_start = start + len(marker)
        # Walk forward balancing {{ and }} to find the matching closing }}
        depth = 2  # We're inside {{Game data/saves and {{Game data|
        pos = content_start
        while pos < len(wikitext) - 1:
            if wikitext[pos:pos+2] == '{{':
                depth += 1
                pos += 2
            elif wikitext[pos:pos+2] == '}}':
                depth -= 1
                if depth <= 1:  # Back to the {{Game data| level
                    break
                pos += 2
            else:
                pos += 1

        paths_raw = wikitext[content_start:pos]
        # Split by | (but not inside nested {{...}}) - simple split works
        # because {{p|...}} inner | is inside braces.
        # We need to split only on top-level pipes.
        paths = []
        current = []
        brace_depth = 0
        for ch in paths_raw:
            if ch == '{':
                brace_depth += 1
                current.append(ch)
            elif ch == '}':
                brace_depth -= 1
                current.append(ch)
            elif ch == '|' and brace_depth == 0:
                paths.append(''.join(current).strip())
                current = []
            else:
                current.append(ch)
        if current:
            paths.append(''.join(current).strip())
        paths = [p for p in paths if p]

        if not paths:
            return None

        # Use the first path and resolve wiki variables
        return _pcgw_resolve_path(paths[0])

    except Exception as e:
        debug_log.warning(f"PCGamingWiki save path lookup failed for {page_title}: {e}")
        return None


class GameConfigDialog(tk.Toplevel):
    """Dialog to add/edit a game configuration."""
    
    def __init__(self, parent, config=None):
        super().__init__(parent)
        self.title("Add Game" if config is None else "Edit Game")
        self.geometry("550x360")
        self.resizable(False, False)
        self.result = None
        self.config = config or {}
        self._pcgw_thread = None  # Track background lookup thread
        
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
        
        # Game Executable
        ttk.Label(frame, text="Game Executable:").grid(row=0, column=0, sticky='w', pady=5)
        self.exe_var = tk.StringVar(value=self.config.get('game_exe', ''))
        ttk.Entry(frame, textvariable=self.exe_var, width=40).grid(row=0, column=1, sticky='w', pady=5)
        ttk.Button(frame, text="Browse...", command=self._browse_exe).grid(row=0, column=2, padx=5)
        
        # Game Name
        ttk.Label(frame, text="Game Name:").grid(row=1, column=0, sticky='w', pady=5)
        self.name_var = tk.StringVar(value=self.config.get('name', ''))
        ttk.Entry(frame, textvariable=self.name_var, width=50).grid(row=1, column=1, columnspan=2, sticky='w', pady=5)
        
        # Local Save Directory
        ttk.Label(frame, text="Save Directory:").grid(row=2, column=0, sticky='w', pady=5)
        self.save_dir_var = tk.StringVar(value=self.config.get('local_save_dir', ''))
        ttk.Entry(frame, textvariable=self.save_dir_var, width=40).grid(row=2, column=1, sticky='w', pady=5)
        ttk.Button(frame, text="Browse...", command=self._browse_save_dir).grid(row=2, column=2, padx=5)
        
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
        
        # PCGamingWiki lookup status label
        self._status_var = tk.StringVar(value='')
        self._status_label = ttk.Label(frame, textvariable=self._status_var, foreground='gray', font=('Segoe UI', 8))
        self._status_label.grid(row=6, column=0, columnspan=3, sticky='w', pady=(5, 0))
        
        # Buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=7, column=0, columnspan=3, pady=(15, 0), sticky='e')
        
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=(5, 0))
        ttk.Button(btn_frame, text="Save", command=self._save).pack(side=tk.RIGHT)
    
    def _browse_save_dir(self):
        # Start the dialog at the current save dir path if it exists
        initial = self.save_dir_var.get().strip()
        kwargs = {'title': 'Select Save Directory'}
        if initial and os.path.isdir(initial):
            kwargs['initialdir'] = initial
        path = filedialog.askdirectory(**kwargs)
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
            # Trigger PCGamingWiki lookup in background
            self._pcgw_lookup(Path(path).stem)
    
    def _pcgw_lookup(self, exe_stem):
        """Look up game info from PCGamingWiki in a background thread."""
        # Build a search query from the exe stem:
        # 1. Replace underscores/dashes with spaces
        # 2. Split camelCase (e.g. "NovaRoma" -> "Nova Roma")
        search_query = exe_stem.replace('_', ' ').replace('-', ' ')
        search_query = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', search_query)
        search_query = re.sub(r'(?<=[A-Z])(?=[A-Z][a-z])', ' ', search_query)

        self._status_var.set('🔍 Looking up on PCGamingWiki...')
        self._status_label.configure(foreground='gray')

        def _do_lookup():
            try:
                results = pcgw_search_game(search_query)
                if not results:
                    self.after(0, lambda: self._pcgw_on_result(None, None))
                    return

                page_title = results[0]
                save_path = pcgw_get_save_path(page_title)
                self.after(0, lambda pt=page_title, sp=save_path: self._pcgw_on_result(pt, sp))
            except Exception as e:
                debug_log.warning(f"PCGamingWiki lookup error: {e}")
                self.after(0, lambda: self._pcgw_on_result(None, None))

        self._pcgw_thread = threading.Thread(target=_do_lookup, daemon=True)
        self._pcgw_thread.start()

    def _pcgw_on_result(self, page_title, save_path):
        """Handle PCGamingWiki lookup result on the main thread."""
        if page_title:
            self.name_var.set(page_title)
            # Also update zip filename to match
            game_id = page_title.lower().replace(' ', '_').replace('-', '_')
            game_id = ''.join(c for c in game_id if c.isalnum() or c == '_')
            self.zip_var.set(f"{game_id}.zip")

            if save_path:
                self.save_dir_var.set(save_path)
                self._status_var.set(f'✅ Found: {page_title}')
                self._status_label.configure(foreground='#228B22')
            else:
                self._status_var.set(f'✅ Found: {page_title}  (save path not available)')
                self._status_label.configure(foreground='#B8860B')
        else:
            self._status_var.set('⚠️ Game not found on PCGamingWiki')
            self._status_label.configure(foreground='#CC6600')

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


class ErrorRetryDialog(tk.Toplevel):
    """Dialog shown when a sync error occurs, offering Retry or Cancel."""
    
    def __init__(self, parent, error_message, operation_name="Sync"):
        super().__init__(parent)
        self.title("Sync Error")
        self.geometry("480x220")
        self.resizable(False, False)
        self.result = None  # 'retry' or 'cancel'
        
        self.transient(parent)
        self.grab_set()
        
        self._create_widgets(error_message, operation_name)
        self.center_window()
        
        # Handle window close button
        self.protocol("WM_DELETE_WINDOW", self._cancel)
    
    def center_window(self):
        self.update_idletasks()
        x = (self.winfo_screenwidth() - self.winfo_width()) // 2
        y = (self.winfo_screenheight() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")
    
    def _create_widgets(self, error_message, operation_name):
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)
        
        # Error icon and title
        ttk.Label(frame, text=f"❌ {operation_name} Failed", font=('Segoe UI', 12, 'bold')).pack(anchor='w')
        
        # Error message
        ttk.Label(
            frame,
            text=str(error_message),
            wraplength=430,
            foreground='#cc0000'
        ).pack(anchor='w', pady=(10, 5))
        
        ttk.Label(
            frame,
            text="Would you like to retry?",
            font=('Segoe UI', 10)
        ).pack(anchor='w', pady=(15, 10))
        
        # Buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, pady=(10, 0))
        
        ttk.Button(
            btn_frame, text="Retry", width=15,
            command=self._retry
        ).pack(side=tk.LEFT, padx=(0, 5))
        
        ttk.Button(
            btn_frame, text="Cancel", width=15,
            command=self._cancel
        ).pack(side=tk.LEFT, padx=5)
    
    def _retry(self):
        self.result = 'retry'
        self.destroy()
    
    def _cancel(self):
        self.result = 'cancel'
        self.destroy()


class CompareSavesDialog(tk.Toplevel):
    """Dialog for comparing local and cloud save timestamps."""
    
    def __init__(self, parent, game_name, local_time, cloud_time):
        super().__init__(parent)
        self.title(f"Compare Saves - {game_name}")
        self.geometry("450x260")
        self.resizable(False, False)
        self.result = None  # 'local', 'cloud', or None (cancel)
        
        self.transient(parent)
        self.grab_set()
        
        self._create_widgets(game_name, local_time, cloud_time)
        self.center_window()
        
        self.protocol("WM_DELETE_WINDOW", self._cancel)
    
    def center_window(self):
        self.update_idletasks()
        x = (self.winfo_screenwidth() - self.winfo_width()) // 2
        y = (self.winfo_screenheight() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")
    
    def _create_widgets(self, game_name, local_time, cloud_time):
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)
        
        ttk.Label(frame, text=f"Save Comparison", font=('Segoe UI', 12, 'bold')).pack(anchor='w')
        ttk.Label(frame, text=game_name, foreground='gray').pack(anchor='w', pady=(2, 15))
        
        # Determine which is newer
        local_newer = local_time and cloud_time and local_time > cloud_time
        cloud_newer = local_time and cloud_time and cloud_time > local_time
        
        info_frame = ttk.Frame(frame)
        info_frame.pack(fill=tk.X, pady=5)
        
        # Local row
        ttk.Label(info_frame, text="💾 Local saves:", font=('Segoe UI', 9, 'bold')).grid(row=0, column=0, sticky='w')
        local_text = format_time(local_time)
        if local_newer:
            local_text += "  ← newer"
        local_lbl = ttk.Label(info_frame, text=local_text)
        if local_newer:
            local_lbl.configure(foreground='#228B22')
        local_lbl.grid(row=0, column=1, sticky='w', padx=10)
        
        # Cloud row
        ttk.Label(info_frame, text="☁️ Cloud saves:", font=('Segoe UI', 9, 'bold')).grid(row=1, column=0, sticky='w', pady=2)
        cloud_text = format_time(cloud_time)
        if cloud_newer:
            cloud_text += "  ← newer"
        cloud_lbl = ttk.Label(info_frame, text=cloud_text)
        if cloud_newer:
            cloud_lbl.configure(foreground='#228B22')
        cloud_lbl.grid(row=1, column=1, sticky='w', padx=10)
        
        if local_time and cloud_time and not local_newer and not cloud_newer:
            ttk.Label(frame, text="Both saves are the same age.", foreground='gray').pack(anchor='w', pady=(10, 0))
        
        ttk.Label(frame, text="What would you like to do?", font=('Segoe UI', 10)).pack(anchor='w', pady=(20, 10))
        
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, pady=(5, 0))
        
        local_btn = ttk.Button(btn_frame, text="Use Local ⬆", width=15, command=self._use_local)
        local_btn.pack(side=tk.LEFT, padx=(0, 5))
        ToolTip(local_btn, "Upload local saves to cloud (overwrites cloud)")
        if local_time is None:
            local_btn.configure(state='disabled')
        
        cloud_btn = ttk.Button(btn_frame, text="Use Cloud ⬇", width=15, command=self._use_cloud)
        cloud_btn.pack(side=tk.LEFT, padx=5)
        ToolTip(cloud_btn, "Download cloud saves to local (overwrites local)")
        if cloud_time is None:
            cloud_btn.configure(state='disabled')
        
        ttk.Button(btn_frame, text="Cancel", width=10, command=self._cancel).pack(side=tk.RIGHT)
    
    def _use_local(self):
        self.result = 'local'
        self.destroy()
    
    def _use_cloud(self):
        self.result = 'cloud'
        self.destroy()
    
    def _cancel(self):
        self.result = None
        self.destroy()


class UpdateDialog(tk.Toplevel):
    """Dialog shown when a new version is available."""
    
    def __init__(self, parent, current_version, new_version, release_notes, download_url):
        super().__init__(parent)
        self.title("Update Available")
        self.geometry("480x320")
        self.resizable(False, False)
        self.result = None  # 'update', 'skip', or None (remind later)
        self.download_url = download_url
        self.new_version = new_version
        
        self.transient(parent)
        self.grab_set()
        
        self._create_widgets(current_version, new_version, release_notes)
        self.center_window()
        
        self.protocol("WM_DELETE_WINDOW", self._later)
    
    def center_window(self):
        self.update_idletasks()
        x = (self.winfo_screenwidth() - self.winfo_width()) // 2
        y = (self.winfo_screenheight() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")
    
    def _create_widgets(self, current_version, new_version, release_notes):
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)
        
        ttk.Label(frame, text="🔄 Update Available!", font=('Segoe UI', 13, 'bold')).pack(anchor='w')
        
        version_frame = ttk.Frame(frame)
        version_frame.pack(fill=tk.X, pady=(12, 5))
        
        ttk.Label(version_frame, text="Current version:", font=('Segoe UI', 9, 'bold')).grid(row=0, column=0, sticky='w')
        ttk.Label(version_frame, text=f"v{current_version}").grid(row=0, column=1, sticky='w', padx=10)
        
        ttk.Label(version_frame, text="New version:", font=('Segoe UI', 9, 'bold')).grid(row=1, column=0, sticky='w', pady=2)
        ttk.Label(version_frame, text=f"v{new_version}", foreground='#228B22').grid(row=1, column=1, sticky='w', padx=10)
        
        if release_notes:
            ttk.Label(frame, text="Release notes:", font=('Segoe UI', 9, 'bold')).pack(anchor='w', pady=(10, 3))
            notes_text = scrolledtext.ScrolledText(frame, height=6, width=50, font=('Segoe UI', 9), state='normal')
            notes_text.pack(fill=tk.BOTH, expand=True)
            notes_text.insert('1.0', release_notes)
            notes_text.configure(state='disabled')
        else:
            # Add spacing if no release notes
            ttk.Frame(frame).pack(expand=True)
        
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, pady=(15, 0))
        
        update_btn = ttk.Button(btn_frame, text="⬆ Update Now", command=self._update)
        update_btn.pack(side=tk.LEFT, padx=(0, 5))
        ToolTip(update_btn, "Download and install the update (app will restart)")
        
        skip_btn = ttk.Button(btn_frame, text="Skip This Version", command=self._skip)
        skip_btn.pack(side=tk.LEFT, padx=5)
        ToolTip(skip_btn, "Don't show this update again")
        
        later_btn = ttk.Button(btn_frame, text="Remind Me Later", command=self._later)
        later_btn.pack(side=tk.RIGHT)
        ToolTip(later_btn, "Ask again next time")
    
    def _update(self):
        self.result = 'update'
        self.destroy()
    
    def _skip(self):
        self.result = 'skip'
        self.destroy()
    
    def _later(self):
        self.result = None
        self.destroy()


class SaveSyncApp(tk.Tk):
    """Main application window."""
    
    def __init__(self):
        super().__init__()
        
        self.title("SaveSync - Game Save Manager")
        self.geometry("600x450")
        self.minsize(500, 350)
        
        # Update state
        self._update_info = None  # (new_version, download_url, release_notes)
        
        # Set window icon
        self._set_icon()
        
        self._setup_styles()
        self._create_widgets()
        self._check_client_secrets()
        self._refresh_games()
        self.center_window()
        
        # Check for updates in background (2s delay so GUI loads first)
        self.after(2000, self._check_for_update_bg)
    
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
        style.configure('GameBtn.TButton', font=('Segoe UI Emoji', 10), padding=(4, 2))
    
    def _create_widgets(self):
        # Main container
        main = ttk.Frame(self, padding=15)
        main.pack(fill=tk.BOTH, expand=True)
        
        # Header
        header = ttk.Frame(main)
        header.pack(fill=tk.X, pady=(0, 15))
        
        ttk.Label(header, text="🎮 SaveSync", style='Title.TLabel').pack(side=tk.LEFT)
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
        
        # Version + update indicator (bottom right)
        version_frame = ttk.Frame(btn_frame)
        version_frame.pack(side=tk.RIGHT)
        
        # Update indicator (hidden by default, shown when update is available)
        self._update_label = tk.Label(
            version_frame, text="⬆", font=('Segoe UI', 10, 'bold'),
            fg='#228B22', cursor='hand2'
        )
        self._update_label.bind('<Button-1>', lambda e: self._show_update_dialog())
        # Don't pack yet — shown only when update is detected
        
        ttk.Label(version_frame, text=f"v{VERSION}", foreground='gray',
                  font=('Segoe UI', 9)).pack(side=tk.RIGHT)
    
    def _on_canvas_configure(self, event):
        """Update games_frame width when canvas is resized."""
        self.canvas.itemconfig(self.canvas_window, width=event.width)
    
    def _on_mousewheel(self, event):
        """Handle mouse wheel scrolling on the games list canvas."""
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
    
    def _bind_mousewheel_recursive(self, widget):
        """Bind mousewheel event to a widget and all its descendants."""
        widget.bind("<MouseWheel>", self._on_mousewheel)
        for child in widget.winfo_children():
            self._bind_mousewheel_recursive(child)
    
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
    
    def _check_for_update_bg(self):
        """Check for updates in a background thread (non-blocking)."""
        def _check():
            result = check_for_update()
            if result:
                self._update_info = result
                # Schedule UI update on the main thread
                self.after(0, self._on_update_found)
        
        thread = threading.Thread(target=_check, daemon=True)
        thread.start()
    
    def _on_update_found(self):
        """Called on main thread when an update is found."""
        if not self._update_info:
            return
        new_version, download_url, release_notes = self._update_info
        
        # Check if user has skipped this version
        settings = load_settings()
        if settings.get('skipped_update_version', '') == new_version:
            debug_log.info(f"Update v{new_version} was skipped by user.")
            return
        
        # Show the update indicator icon (packs to left of version text)
        self._update_label.pack(side=tk.RIGHT, padx=(0, 5))
        ToolTip(self._update_label, f"Update available: v{new_version} (click to update)")
    
    def _show_update_dialog(self):
        """Show the update dialog."""
        if not self._update_info:
            return
        new_version, download_url, release_notes = self._update_info
        
        dialog = UpdateDialog(self, VERSION, new_version, release_notes, download_url)
        self.wait_window(dialog)
        
        if dialog.result == 'update':
            self._perform_update(download_url, new_version)
        elif dialog.result == 'skip':
            settings = load_settings()
            settings['skipped_update_version'] = new_version
            save_settings(settings)
            # Hide the update indicator
            self._update_label.pack_forget()
            debug_log.info(f"User skipped update v{new_version}")
    
    def _perform_update(self, download_url, new_version):
        """Run the update with a progress window."""
        win = tk.Toplevel(self)
        win.title("Updating SaveSync...")
        win.geometry("500x300")
        win.transient(self)
        win.grab_set()
        
        text = scrolledtext.ScrolledText(win, state='disabled', font=('Consolas', 10))
        text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        def log(msg):
            text.configure(state='normal')
            text.insert(tk.END, msg + '\n')
            text.see(tk.END)
            text.configure(state='disabled')
            win.update()
        
        def run():
            download_and_apply_update(download_url, new_version, log_func=log)
            # If we get here, the update failed (sys.exit didn't happen)
            ttk.Button(win, text="Close", command=win.destroy).pack(pady=10)
        
        win.after(100, run)
    
    def _manual_check_for_update(self):
        """Manually trigger an update check (from Settings)."""
        self._update_info = None
        
        win = tk.Toplevel(self)
        win.title("Checking for updates...")
        win.geometry("400x150")
        win.transient(self)
        win.grab_set()
        
        frame = ttk.Frame(win, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)
        
        status_label = ttk.Label(frame, text="Checking for updates...", font=('Segoe UI', 10))
        status_label.pack(pady=(10, 15))
        
        def _check():
            result = check_for_update()
            if result:
                self._update_info = result
                new_version = result[0]
                self.after(0, lambda: [
                    status_label.configure(text=f"Update available: v{new_version}"),
                    self._on_update_found(),
                    win.destroy(),
                    self._show_update_dialog()
                ])
            else:
                self.after(0, lambda: [
                    status_label.configure(text="✅ You're running the latest version!"),
                    ttk.Button(frame, text="OK", command=win.destroy).pack(pady=5)
                ])
        
        thread = threading.Thread(target=_check, daemon=True)
        thread.start()
    
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
        
        # Bind mousewheel scrolling to canvas and all child widgets
        self._bind_mousewheel_recursive(self.canvas)
    
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
        
        play_btn = ttk.Button(btn_frame, text="▶️", width=4, style='GameBtn.TButton',
                              command=lambda g=game: self._sync_and_play(g))
        play_btn.pack(side=tk.LEFT, padx=2)
        ToolTip(play_btn, "Download saves, launch game, upload after closing")
        
        #sync_btn = ttk.Button(btn_frame, text="🔄", width=4, style='GameBtn.TButton',
        #                      command=lambda g=game: self._sync_only(g))
        #sync_btn.pack(side=tk.LEFT, padx=2)
        #ToolTip(sync_btn, "Sync saves without launching game")

        compare_btn = ttk.Button(btn_frame, text="🔀", width=4, style='GameBtn.TButton',
                                  command=lambda g=game: self._compare_saves(g))
        compare_btn.pack(side=tk.LEFT, padx=2)
        ToolTip(compare_btn, "Compare local & cloud saves")
        
        upload_btn = ttk.Button(btn_frame, text="⬆️", width=4, style='GameBtn.TButton',
                                command=lambda g=game: self._force_upload(g))
        upload_btn.pack(side=tk.LEFT, padx=2)
        ToolTip(upload_btn, "Force upload local saves to cloud")
        
        download_btn = ttk.Button(btn_frame, text="⬇️", width=4, style='GameBtn.TButton',
                                  command=lambda g=game: self._force_download(g))
        download_btn.pack(side=tk.LEFT, padx=2)
        ToolTip(download_btn, "Force download cloud saves to local")
        
        edit_btn = ttk.Button(btn_frame, text="✏️", width=4, style='GameBtn.TButton',
                              command=lambda g=game: self._edit_game(g))
        edit_btn.pack(side=tk.LEFT, padx=2)
        ToolTip(edit_btn, "Edit game configuration")
        
        shortcut_btn = ttk.Button(btn_frame, text="🔗", width=4, style='GameBtn.TButton',
                                  command=lambda g=game: self._create_shortcut(g))
        shortcut_btn.pack(side=tk.LEFT, padx=2)
        ToolTip(shortcut_btn, "Create desktop shortcut")
        
        delete_btn = ttk.Button(btn_frame, text="🗑️", width=4, style='GameBtn.TButton',
                                command=lambda g=game: self._delete_game(g))
        delete_btn.pack(side=tk.LEFT, padx=2)
        ToolTip(delete_btn, "Remove game from SaveSync")
        
        ttk.Separator(self.games_frame, orient='horizontal').pack(fill=tk.X, pady=5)
    
    def _upload_gamelist_bg(self):
        """Upload gamelist to GDrive in background thread (if configured)."""
        settings = load_settings()
        if not settings.get('device_gamelist_sv_path', '').strip():
            return
        
        def _upload():
            upload_gamelist_to_gdrive(log_func=lambda msg: debug_log.info(msg))
        
        thread = threading.Thread(target=_upload, daemon=True)
        thread.start()
    
    def _add_game(self):
        """Add a new game."""
        dialog = GameConfigDialog(self)
        self.wait_window(dialog)
        
        if dialog.result:
            game_id = dialog.result.pop('id')
            save_game_config(game_id, dialog.result)
            self._refresh_games()
            self._upload_gamelist_bg()
    
    def _edit_game(self, game):
        """Edit an existing game."""
        dialog = GameConfigDialog(self, game)
        self.wait_window(dialog)
        
        if dialog.result:
            game_id = dialog.result.pop('id')
            save_game_config(game_id, dialog.result)
            self._refresh_games()
            self._upload_gamelist_bg()
    
    def _delete_game(self, game):
        """Delete a game configuration."""
        if messagebox.askyesno("Confirm Delete", f"Delete '{game['name']}' from SaveSync?\n\n(This won't delete your save files)"):
            delete_game_config(game['id'])
            self._refresh_games()
            self._upload_gamelist_bg()
    
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
    
    def _force_upload(self, game):
        """Force upload local saves to cloud."""
        if messagebox.askyesno("Force Upload", f"Upload local saves for '{game['name']}' to cloud?\n\nThis will overwrite the cloud save."):
            self._run_force_sync_window(game, mode='upload')
    
    def _force_download(self, game):
        """Force download cloud saves to local."""
        if messagebox.askyesno("Force Download", f"Download cloud saves for '{game['name']}'?\n\nThis will overwrite your local saves."):
            self._run_force_sync_window(game, mode='download')
    
    def _compare_saves(self, game):
        """Compare local and cloud save timestamps, let user choose action."""
        win = tk.Toplevel(self)
        win.title(f"Comparing saves - {game['name']}")
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
        
        def run():
            try:
                config = load_game_config(game['id'])
                if not config:
                    log(f"❌ Game '{game['id']}' not found.")
                    ttk.Button(win, text="Close", command=win.destroy).pack(pady=10)
                    return
                
                log("Authenticating...")
                drive = get_drive()
                log("Authentication successful.")
                
                folder_id = get_or_create_folder(drive, config.get('gdrive_folder', ''))
                remote_zip_name = config.get('remote_zip_name', 'saves.zip')
                
                log("Fetching save timestamps...")
                local_time = get_local_save_time(config['local_save_dir'])
                cloud_time, cloud_file = get_cloud_save_info(drive, folder_id, remote_zip_name)
                
                log(f"Local saves: {format_time(local_time)}")
                log(f"Cloud saves: {format_time(cloud_time)}")
                
                # Show comparison dialog
                dialog = CompareSavesDialog(win, game['name'], local_time, cloud_time)
                win.wait_window(dialog)
                
                choice = dialog.result
                if choice == 'local':
                    log("\nUser chose: Use LOCAL saves (uploading to cloud)...")
                    zip_and_upload(drive, folder_id, config, log_func=log)
                elif choice == 'cloud':
                    log("\nUser chose: Use CLOUD saves (downloading to local)...")
                    download_and_extract(drive, folder_id, config, log_func=log)
                else:
                    log("\nCancelled.")
                    win.destroy()
                    return
                
                log("\n✅ Done!")
            except Exception as e:
                log(f"\n❌ Error: {e}")
            
            ttk.Button(win, text="Close", command=win.destroy).pack(pady=10)
        
        win.after(100, run)
    
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
        
        def gui_error_callback(error_message, op_name):
            """GUI callback for error retry."""
            dialog = ErrorRetryDialog(win, error_message, op_name)
            win.wait_window(dialog)
            return dialog.result or 'cancel'
        
        def run():
            try:
                if sync_all:
                    sync_all_games(log_func=log, conflict_callback=gui_conflict_callback, error_callback=gui_error_callback)
                else:
                    sync_game(game_id, run_game=run_game, log_func=log, conflict_callback=gui_conflict_callback, error_callback=gui_error_callback)
                log("\n✅ Done!")
            except Exception as e:
                log(f"\n❌ Error: {e}")
            
            ttk.Button(win, text="Close", command=win.destroy).pack(pady=10)
        
        win.after(100, run)
    
    def _run_force_sync_window(self, game, mode='upload'):
        """Open a window for forced upload or download."""
        action = "Uploading" if mode == 'upload' else "Downloading"
        win = tk.Toplevel(self)
        win.title(f"{action}...")
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
        
        def gui_error_callback(error_message, op_name):
            """GUI callback for error retry."""
            dialog = ErrorRetryDialog(win, error_message, op_name)
            win.wait_window(dialog)
            return dialog.result or 'cancel'
        
        def run():
            try:
                config = load_game_config(game['id'])
                if not config:
                    log(f"❌ Game '{game['id']}' not found.")
                    return
                
                drive = get_drive()
                log("Authentication successful.")
                
                folder_id = get_or_create_folder(drive, config.get('gdrive_folder', ''))
                
                if mode == 'upload':
                    log(f"Force uploading saves for {game['name']}...")
                    _sync_op_with_error_retry(
                        lambda: zip_and_upload(drive, folder_id, config, log_func=log),
                        gui_error_callback, log, op_name="Upload"
                    )
                else:
                    log(f"Force downloading saves for {game['name']}...")
                    _sync_op_with_error_retry(
                        lambda: download_and_extract(drive, folder_id, config, log_func=log),
                        gui_error_callback, log, op_name="Download"
                    )
                
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
    import pystray
    from PIL import Image

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
    
    # Thread-safe variables for error retry dialog
    error_event = threading.Event()
    error_result = [None]  # 'retry' or 'cancel'
    error_data = [None, None]  # error_message, op_name
    
    def check_dialog_requests():
        """Check if background thread needs a dialog (runs on main thread)."""
        # Check for conflict dialog request
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
        
        # Check for error retry dialog request
        if error_data[0] is not None:
            err_msg, op_name = error_data
            error_data[0] = None
            error_data[1] = None
            
            # Make sure window is visible for error dialog
            show_window()
            dialog = ErrorRetryDialog(root, err_msg, op_name)
            root.wait_window(dialog)
            error_result[0] = dialog.result or 'cancel'
            error_event.set()
        
        # Keep checking while sync is running
        if not sync_complete:
            root.after(100, check_dialog_requests)
    
    def gui_conflict_callback(local_time, cloud_time):
        """GUI callback for conflict resolution (called from background thread)."""
        conflict_event.clear()
        conflict_data[0] = local_time
        conflict_data[1] = cloud_time
        # Wait for main thread to handle the dialog
        conflict_event.wait()
        return conflict_result[0]
    
    def gui_error_callback(error_message, op_name):
        """GUI callback for error retry (called from background thread)."""
        error_event.clear()
        error_data[0] = error_message
        error_data[1] = op_name
        # Wait for main thread to handle the dialog
        error_event.wait()
        return error_result[0]
    
    def thread_safe_log(msg):
        """Log message in a thread-safe way."""
        root.after(0, lambda: log(msg))
    
    def run_sync():
        nonlocal sync_complete
        try:
            thread_safe_log(f"SaveSync - Syncing: {game_id}")
            thread_safe_log("=" * 40)
            result = sync_game(game_id, run_game=not sync_only, log_func=thread_safe_log, conflict_callback=gui_conflict_callback, error_callback=gui_error_callback)
            if result:
                thread_safe_log("\n✅ Done!")
        except Exception as e:
            thread_safe_log(f"\n❌ Error: {e}")
        
        # Check for updates (CLI/shortcut mode - log only)
        try:
            update_result = check_for_update()
            if update_result:
                new_ver = update_result[0]
                thread_safe_log(f"\n🔄 Update available: v{VERSION} → v{new_ver}")
                thread_safe_log("   Open SaveSync to update.")
        except Exception:
            pass  # Don't fail on update check errors
        
        sync_complete = True
        # Add close button after sync completes (on main thread)
        root.after(0, lambda: ttk.Button(root, text="Close", command=quit_app).pack(pady=10))
        
        # Show window when sync is complete (so user can see the result)
        root.after(0, show_window)
    
    # Start minimized if requested
    if start_minimized:
        root.withdraw()
    
    # Start dialog checker (handles both conflict and error dialogs)
    root.after(100, check_dialog_requests)
    
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
    debug_log.info("=" * 60)
    debug_log.info("SaveSync starting")
    debug_log.info(f"Version: {VERSION}")
    debug_log.info(f"Python: {sys.version}")
    debug_log.info(f"Executable: {sys.executable}")
    debug_log.info(f"Frozen: {getattr(sys, 'frozen', False)}")
    if getattr(sys, 'frozen', False):
        debug_log.info(f"MEIPASS: {getattr(sys, '_MEIPASS', 'N/A')}")
    debug_log.info(f"sys.argv: {sys.argv}")
    debug_log.info(f"CWD: {os.getcwd()}")
    debug_log.info(f"APP_DATA_DIR: {APP_DATA_DIR}")
    
    parser = argparse.ArgumentParser(description="SaveSync - Game Save Synchronization")
    parser.add_argument('--game', '-g', help='Game ID to sync and launch (CLI mode)')
    parser.add_argument('--sync-only', action='store_true', help='Only sync, do not launch game')
    args = parser.parse_args()
    
    debug_log.info(f"Parsed args: game={args.game!r}, sync_only={args.sync_only}")
    
    ensure_app_dirs()
    
    if args.game:
        debug_log.info(f"Entering CLI mode for game: {args.game!r}")
        # CLI mode - always use tray-enabled window
        # Setting controls whether window starts visible or minimized
        settings = load_settings()
        start_minimized = not settings.get('show_log_window', True)
        debug_log.info(f"Settings: show_log_window={settings.get('show_log_window')}, start_minimized={start_minimized}")
        
        # Validate game config exists before proceeding
        config = load_game_config(args.game)
        if config:
            debug_log.info(f"Game config found: name={config.get('name')}, exe={config.get('game_exe')}")
            game_exe = config.get('game_exe', '')
            debug_log.info(f"Game exe exists: {os.path.exists(game_exe)}")
            if os.path.exists(game_exe):
                debug_log.info(f"Game exe size: {os.path.getsize(game_exe)} bytes")
        else:
            debug_log.error(f"Game config NOT found for id: {args.game!r}")
            debug_log.info(f"Available game configs: {[f.stem for f in GAMES_DIR.glob('*.json')]}")
        
        try:
            run_cli_with_gui_window(args.game, sync_only=args.sync_only, start_minimized=start_minimized)
        except Exception as e:
            debug_log.error(f"Fatal error in CLI mode: {type(e).__name__}: {e}", exc_info=True)
            raise
    else:
        debug_log.info("Entering GUI mode")
        # GUI mode
        app = SaveSyncApp()
        app.mainloop()
    
    debug_log.info("SaveSync exiting normally")


if __name__ == "__main__":
    main()

