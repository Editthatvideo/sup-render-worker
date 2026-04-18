"""Environment-backed configuration."""
import os
from functools import lru_cache
from pydantic import BaseModel


class Settings(BaseModel):
    # Auth for incoming n8n calls
    worker_api_key: str

    # OpenAI (Whisper)
    openai_api_key: str

    # n8n callback
    n8n_callback_url: str           # POST here when render done
    n8n_callback_auth: str = ""     # optional Bearer token n8n expects

    # Google Drive (OAuth2)
    gdrive_client_id: str
    gdrive_client_secret: str
    gdrive_refresh_token: str
    gdrive_output_folder_id: str       # target folder

    # Rendering
    output_width: int = 1080
    output_height: int = 1920
    headline_font_size: int = 52
    caption_font_size: int = 42
    font_path: str = "/usr/share/fonts/truetype/custom/Anton-Regular.ttf"

    # YouTube
    youtube_api_key: str = ""       # Data API v3 for search
    youtube_cookies_b64: str = ""   # cookies for yt-dlp download

    # Runtime
    work_dir: str = "/tmp/renders"


@lru_cache
def get_settings() -> Settings:
    return Settings(
        worker_api_key=os.environ["WORKER_API_KEY"],
        openai_api_key=os.environ["OPENAI_API_KEY"],
        n8n_callback_url=os.environ["N8N_CALLBACK_URL"],
        n8n_callback_auth=os.environ.get("N8N_CALLBACK_AUTH", ""),
        gdrive_client_id=os.environ["GDRIVE_CLIENT_ID"],
        gdrive_client_secret=os.environ["GDRIVE_CLIENT_SECRET"],
        gdrive_refresh_token=os.environ["GDRIVE_REFRESH_TOKEN"],
        gdrive_output_folder_id=os.environ["GDRIVE_OUTPUT_FOLDER_ID"],
        output_width=int(os.environ.get("OUTPUT_WIDTH", 1080)),
        output_height=int(os.environ.get("OUTPUT_HEIGHT", 1920)),
        headline_font_size=int(os.environ.get("HEADLINE_FONT_SIZE", 52)),
        caption_font_size=int(os.environ.get("CAPTION_FONT_SIZE", 42)),
        font_path=os.environ.get("FONT_PATH", "/usr/share/fonts/truetype/custom/Anton-Regular.ttf"),
        youtube_api_key=os.environ.get("YOUTUBE_API_KEY", ""),
        youtube_cookies_b64=os.environ.get("YOUTUBE_COOKIES_B64", ""),
        work_dir=os.environ.get("WORK_DIR", "/tmp/renders"),
    )
