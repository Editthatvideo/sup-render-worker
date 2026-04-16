"""Upload rendered clips to a shared Google Drive folder via a service account."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from .config import get_settings

log = logging.getLogger("render-worker.drive")

SCOPES = ["https://www.googleapis.com/auth/drive.file",
          "https://www.googleapis.com/auth/drive"]


def _credentials():
    settings = get_settings()
    raw = settings.gdrive_service_account_json
    # Accept either a JSON blob or a path
    if raw.strip().startswith("{"):
        info = json.loads(raw)
    else:
        info = json.loads(Path(raw).read_text())
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    # Impersonate the user so uploads count against their storage quota
    if settings.gdrive_impersonate_email:
        creds = creds.with_subject(settings.gdrive_impersonate_email)
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

    # Make link-accessible so n8n / you can view without ACL changes
    try:
        svc.permissions().create(
            fileId=f["id"],
            body={"role": "reader", "type": "anyone"},
            supportsAllDrives=True,
        ).execute()
    except Exception:
        log.warning("Could not set anyone-with-link permission; file is private to service account.")

    return f
