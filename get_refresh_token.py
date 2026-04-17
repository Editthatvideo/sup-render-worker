"""One-time script: run on your Mac to get a Google Drive refresh token.

Usage:
    pip install google-auth-oauthlib
    python get_refresh_token.py YOUR_CLIENT_ID YOUR_CLIENT_SECRET

It will open a browser window. Sign in with mixmyalbum@gmail.com,
grant Drive access, and the script prints your refresh token.
Paste that token into Railway as GDRIVE_REFRESH_TOKEN.
"""
import sys
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

if len(sys.argv) != 3:
    print("Usage: python get_refresh_token.py CLIENT_ID CLIENT_SECRET")
    sys.exit(1)

client_id = sys.argv[1]
client_secret = sys.argv[2]

client_config = {
    "installed": {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}

flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
creds = flow.run_local_server(port=8090, prompt="consent", access_type="offline")

print("\n" + "=" * 60)
print("REFRESH TOKEN (paste this into Railway as GDRIVE_REFRESH_TOKEN):")
print("=" * 60)
print(creds.refresh_token)
print("=" * 60)
