"""Google Drive integration for PostKaro.

Enables PostKaro to fetch, stream, and manage short-form video clips directly from
the user's Google Drive folder ('ClipKaro Clips' / 1ZR0GzM9uIQsjbI4zI7gMuOEU6Ju-WC1T)
when running in cloud environments like Render.
"""
import io
import os
import sys
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from . import db, paths, vault

SCOPES = ['https://www.googleapis.com/auth/drive.file']
DEFAULT_FOLDER_ID = '1ZR0GzM9uIQsjbI4zI7gMuOEU6Ju-WC1T'


def folder_id():
    return os.environ.get('GDRIVE_FOLDER_ID') or DEFAULT_FOLDER_ID


def get_credentials():
    # 1. Environment variables (ideal for Render / cloud)
    client_id = os.environ.get('GOOGLE_CLIENT_ID')
    client_secret = os.environ.get('GOOGLE_CLIENT_SECRET')
    refresh_token = os.environ.get('GDRIVE_REFRESH_TOKEN')

    # 2. Vault fallback
    app = vault.get('app:google') or {}
    client_id = client_id or app.get('client_id')
    client_secret = client_secret or app.get('client_secret')

    # 3. Check for local token file
    token_file = Path(__file__).resolve().parent.parent / 'gdrive_token.json'
    if not token_file.exists():
        # check clipkaro sibling directory
        alt = Path(__file__).resolve().parent.parent.parent / 'clipkaro' / 'gdrive_token.json'
        if alt.exists():
            token_file = alt

    if token_file.exists():
        try:
            return Credentials.from_authorized_user_file(str(token_file), SCOPES)
        except Exception:
            pass

    if refresh_token and client_id and client_secret:
        return Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri='https://oauth2.googleapis.com/token',
            client_id=client_id,
            client_secret=client_secret,
            scopes=SCOPES
        )

    # 4. Fallback to youtube refresh token if drive scope was granted
    for row in db.accounts('youtube'):
        acc_vault = vault.get('youtube:' + row['account_id']) or {}
        if acc_vault.get('refresh_token'):
            return Credentials(
                token=None,
                refresh_token=acc_vault['refresh_token'],
                token_uri='https://oauth2.googleapis.com/token',
                client_id=client_id or acc_vault.get('client_id'),
                client_secret=client_secret or acc_vault.get('client_secret'),
                scopes=SCOPES
            )

    return None


def get_service():
    creds = get_credentials()
    if not creds:
        return None
    try:
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        print(f"Error building Drive service: {e}")
        return None


def list_clips(target_folder_id=None):
    fid = target_folder_id or folder_id()
    service = get_service()
    if not service:
        return []

    try:
        res = service.files().list(
            q=f"'{fid}' in parents and trashed = false",
            fields="files(id, name, size, mimeType, modifiedTime, webViewLink, thumbnailLink)"
        ).execute()
        return [f for f in res.get('files', []) if f['name'].lower().endswith(('.mp4', '.mov'))]
    except Exception as e:
        print(f"Error listing Google Drive clips: {e}")
        return []


def ensure_local_file(file_ref):
    """
    Given a clip file path or 'gdrive:<file_id>:<filename>', returns a local Path
    where the file is available, downloading it from Drive if necessary.
    """
    p = Path(file_ref)
    if p.exists():
        return p

    service = get_service()
    if not service:
        return None

    clips_cache = paths.data_dir() / 'clips'
    clips_cache.mkdir(parents=True, exist_ok=True)

    file_id = None
    target_name = p.name

    if str(file_ref).startswith('gdrive:'):
        parts = str(file_ref).split(':', 2)
        file_id = parts[1]
        target_name = parts[2] if len(parts) > 2 else f"{file_id}.mp4"
    else:
        # Search by filename in Google Drive folder
        clips = list_clips()
        for c in clips:
            if c['name'] == p.name:
                file_id = c['id']
                target_name = c['name']
                break

    if not file_id:
        return None

    local_dest = clips_cache / target_name
    if local_dest.exists() and local_dest.stat().st_size > 0:
        return local_dest

    # Download from Google Drive
    print(f"Downloading clip from Google Drive: {target_name} ({file_id})...")
    req = service.files().get_media(fileId=file_id)
    with open(local_dest, 'wb') as fh:
        downloader = MediaIoBaseDownload(fh, req, chunksize=10 * 1024 * 1024)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                print(f"Download progress: {int(status.progress() * 100)}%")

    print(f"Downloaded successfully to {local_dest}")
    return local_dest


def sync_drive_to_batch():
    """
    Inspects Google Drive folder, registers any new video clips in PostKaro's
    batches and clips database so they appear in the UI ready to schedule.
    """
    clips = list_clips()
    if not clips:
        return {'imported': 0, 'total': 0, 'message': 'No clips found in Google Drive folder.'}

    # Find or create a Google Drive batch
    batch = db.one("SELECT id FROM batches WHERE project='Google Drive Clips'")
    if not batch:
        batch_id = db.run(
            "INSERT INTO batches (kit, project, title, created) VALUES (?, ?, ?, ?)",
            ('gdrive:' + folder_id(), 'Google Drive Clips', 'Clips from Google Drive', db.iso(db.now()))
        )
    else:
        batch_id = batch['id']

    existing_files = {r['file'] for r in db.rows("SELECT file FROM clips WHERE batch_id=?", (batch_id,))}
    existing_titles = {r['title'] for r in db.rows("SELECT title FROM clips WHERE batch_id=?", (batch_id,))}

    imported = 0
    next_no = db.one("SELECT COALESCE(MAX(no), 0) + 1 AS n FROM clips WHERE batch_id=?", (batch_id,))['n']

    for c in clips:
        ref = f"gdrive:{c['id']}:{c['name']}"
        raw_name = c['name'].rsplit('.', 1)[0].replace('_', ' ').replace('-', ' ')
        title = raw_name.title()

        if ref in existing_files or title in existing_titles or c['name'] in existing_files:
            continue

        size = int(c.get('size', 0))
        clip_id = db.run(
            "INSERT INTO clips (batch_id, no, file, duration, size, score, title, caption) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (batch_id, next_no, ref, 30.0, size, 8.5, title, f"{title} #Shorts #Reels")
        )
        next_no += 1

        s = settings.load()
        for plat in ('youtube', 'instagram', 'facebook'):
            plat_enabled = 1 if plat == 'youtube' else int(bool(s.get(f"{plat}_enabled", False)))
            db.run(
                "INSERT INTO posts (clip_id, platform, enabled, title, body, state) VALUES (?, ?, ?, ?, ?, 'draft')",
                (clip_id, plat, plat_enabled, title, f"{title}\n\n#Shorts #Viral #Gaming")
            )
        imported += 1

    return {
        'imported': imported,
        'total': len(clips),
        'batch_id': batch_id,
        'message': f"Synced with Google Drive: {imported} new clip(s) imported, {len(clips)} total in Drive."
    }
