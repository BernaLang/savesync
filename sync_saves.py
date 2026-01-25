import os
import zipfile
import subprocess
import shutil
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive

# --- CONFIGURATION (Update these paths!) ---
# Use 'r' before the string to handle Windows backslashes correctly
# laptop
# desktop
LOCAL_SAVE_DIR = r""
GAME_EXE = r""

REMOTE_ZIP_NAME = "cyberknights.zip"
TEMP_ZIP = "cyberknights_temp_sync.zip"

CLIENT_SECRETS_DATA = {}

creds_path = os.path.join(os.getenv('APPDATA'), 'my_game_sync_creds.txt')

# Google Drive folder path (use forward slashes, e.g., "SaveSync/CyberKnights")
# Leave empty or set to None to use root folder
GDRIVE_FOLDER_PATH = "xauto/CyberKnights"

def get_drive():
    """Handles Google OAuth authentication."""
    # Configure settings to use embedded client config
    settings = {
        "client_config_backend": "settings",
        "client_config": CLIENT_SECRETS_DATA,
        "save_credentials": True,
        "save_credentials_backend": "file",
        "save_credentials_file": creds_path,
        "get_refresh_token": True,
        "oauth_scope": ["https://www.googleapis.com/auth/drive"]
    }
    
    gauth = GoogleAuth(settings=settings)

    # Attempts to load saved credentials; otherwise opens browser
    gauth.LoadCredentialsFile(creds_path)
    if gauth.credentials is None:
        gauth.LocalWebserverAuth()
    elif gauth.access_token_expired:
        gauth.Refresh()
    else:
        gauth.Authorize()
    gauth.SaveCredentialsFile(creds_path)
    return GoogleDrive(gauth)

def get_or_create_folder(drive, folder_path):
    """
    Gets or creates a folder by path (e.g., "SaveSync/CyberKnights").
    Returns the folder ID, or 'root' if no path is specified.
    """
    if not folder_path:
        return 'root'
    
    parent_id = 'root'
    folders = folder_path.strip('/').split('/')
    
    for folder_name in folders:
        # Search for existing folder
        query = f"title = '{folder_name}' and '{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        file_list = drive.ListFile({'q': query}).GetList()
        
        if file_list:
            parent_id = file_list[0]['id']
        else:
            # Create the folder
            folder = drive.CreateFile({
                'title': folder_name,
                'mimeType': 'application/vnd.google-apps.folder',
                'parents': [{'id': parent_id}]
            })
            folder.Upload()
            parent_id = folder['id']
            print(f"Created folder: {folder_name}")
    
    return parent_id

def download_and_extract(drive, folder_id):
    print("Checking Google Drive for cloud saves...")
    query = f"title = '{REMOTE_ZIP_NAME}' and '{folder_id}' in parents and trashed = false"
    file_list = drive.ListFile({'q': query}).GetList()
    
    if file_list:
        print("Cloud save found. Downloading...")
        file_list[0].GetContentFile(TEMP_ZIP)
        
        # Clear local folder before extracting to ensure a clean sync
        if os.path.exists(LOCAL_SAVE_DIR):
            shutil.rmtree(LOCAL_SAVE_DIR)
        os.makedirs(LOCAL_SAVE_DIR)
        
        with zipfile.ZipFile(TEMP_ZIP, 'r') as zip_ref:
            zip_ref.extractall(LOCAL_SAVE_DIR)
        
        os.remove(TEMP_ZIP)
        print("Local saves updated from Cloud.")
    else:
        print("No cloud save found. Proceeding with local files.")

def zip_and_upload(drive, folder_id):
    print("Zipping local saves...")
    with zipfile.ZipFile(TEMP_ZIP, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(LOCAL_SAVE_DIR):
            for file in files:
                rel_path = os.path.relpath(os.path.join(root, file), LOCAL_SAVE_DIR)
                zipf.write(os.path.join(root, file), rel_path)

    print("Uploading to Google Drive...")
    query = f"title = '{REMOTE_ZIP_NAME}' and '{folder_id}' in parents and trashed = false"
    file_list = drive.ListFile({'q': query}).GetList()
    
    # Update existing or create new
    if file_list:
        f = file_list[0]
    else:
        f = drive.CreateFile({'title': REMOTE_ZIP_NAME, 'parents': [{'id': folder_id}]})
    f.SetContentFile(TEMP_ZIP)
    f.Upload()
    
    # Release the file handle before deleting
    f.content = None
    
    os.remove(TEMP_ZIP)
    print("Sync Successful! Cloud is up to date.")

if __name__ == "__main__":
    try:
        drive_instance = get_drive()
        print("Authentication successful.")
        
        # Get or create the target folder
        folder_id = get_or_create_folder(drive_instance, GDRIVE_FOLDER_PATH)

        # 1. Pull from Cloud
        download_and_extract(drive_instance, folder_id)

        # 2. Run Game
        print(f"\nStarting Game: {GAME_EXE}")
        print("Script is now waiting for you to finish playing...")
        process = subprocess.Popen(GAME_EXE)
        process.wait() 

        # 3. Push to Cloud
        print("\nGame closed. Initializing post-game sync...")
        zip_and_upload(drive_instance, folder_id)
        
    except Exception as e:
        print(f"\n[ERROR] {e}")
        input("Press Enter to close...")