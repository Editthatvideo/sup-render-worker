"""Upload rendered clips to Google Drive via OAuth2 refresh token (personal Gmail compatible)."""
from __future__ import annotations

import logging
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from .config import get_settings

log = logging.getLogger("render-worker.drive")

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def _credentials():
    settings = get_settings()
    creds = Credentials(
        token=None,
        refresh_token=settings.gdrive_refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.gdrive_client_id,
        client_secret=settings.gdrive_client_secret,
        scopes=SCOPES,
    )
    return creds


def _service():
    return build("drive", "v3", credentials=_credentials(), cache_discovery=False)


def upload_file(local_path: Path, name: str) -> dict:
    settings = get_settings()
    svc = _service()
    body = {"name": name, "parents": [settings.gdrive_output_folder_id]}
    media = MediaFileUpload(str(local_path), mimetype="video/mp4", resumable=True)
    f = svc.files().create(
        body=body,
        media_body=media,
        fields="id,name,webViewLink,webContentLink",
        supportsAllDrives=True,
    ).execute()

    # Make link-accessible
    try:
        svc.permissions().create(
            fileId=f["id"],
            body={"role": "reader", "type": "anyone"},
            supportsAllDrives=True,
        ).execute()
    except Exception:
        log.warning("Could not set anyone-with-link permission; file may be private.")

    return f
