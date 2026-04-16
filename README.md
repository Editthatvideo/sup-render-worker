# SUP Render Worker

A small FastAPI service that n8n calls to turn a YouTube URL + timestamp into a 9:16 TikTok-style clip with Whisper captions and a headline card, then uploads the result to Google Drive.

## Pipeline

```
n8n  ──POST /render──►  worker
                        │
                        ├─ yt-dlp            (download source video)
                        ├─ ffmpeg trim       (clip_start → clip_end)
                        ├─ ffmpeg 9:16       (scale + center crop)
                        ├─ OpenAI Whisper    (generate SRT)
                        ├─ ffmpeg drawtext   (burn headline + subtitles)
                        └─ Google Drive      (upload final mp4)
                        │
                        └─POST webhook──►  n8n  (writes back to sheet)
```

## Local test

```bash
cd sup-render-worker
cp .env.example .env
# fill in the .env values (see sections below)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
# curl the worker
curl -X POST http://localhost:8000/render \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $WORKER_API_KEY" \
  -d '{
    "row_number": 5,
    "youtube_url": "https://www.youtube.com/watch?v=...",
    "clip_start": "1:30",
    "clip_end": "1:35",
    "headline": "When your pension wont cover rent in 2045",
    "movie_show": "The Big Short"
  }'
```

You need `ffmpeg` installed locally to test without Docker. On macOS: `brew install ffmpeg`.

## One-time setup

### 1. OpenAI API key
Create a key at https://platform.openai.com/api-keys → set `OPENAI_API_KEY`.

Whisper cost: ~$0.006/min of audio. 45 clips of 5s ≈ $0.02. Cheap.

### 2. Google Drive service account
1. Go to https://console.cloud.google.com → create a project (or reuse one).
2. APIs & Services → Library → enable **Google Drive API**.
3. APIs & Services → Credentials → **Create credentials → Service account**. Give it any name. Skip role grants.
4. On the new service account, **Keys → Add key → JSON**. Download the file. That's your `GDRIVE_SERVICE_ACCOUNT_JSON` (paste the whole JSON as one line, or commit it as a file and give the path).
5. In Google Drive, create the output folder (e.g. "SUP Rendered Clips"). **Share** that folder with the service account's email (looks like `xxx@project.iam.gserviceaccount.com`), give it **Editor**.
6. Open the folder in Drive, copy the ID from the URL (`.../folders/<THIS_PART>`). That's `GDRIVE_OUTPUT_FOLDER_ID`.

### 3. n8n callback webhook
In n8n, add a **Webhook** node (HTTP Method: POST, Path: `sup-render-done`, Respond: "Immediately"). Save & activate. The URL shown is your `N8N_CALLBACK_URL`. Optionally add a header-auth check via `N8N_CALLBACK_AUTH`.

## Deploy to Railway

1. `git init && git add . && git commit -m "init render worker"` (from this folder).
2. Push to a GitHub repo.
3. https://railway.app → **New Project → Deploy from GitHub repo**. Pick the repo.
4. Railway auto-detects the Dockerfile. In the service **Variables** tab, add every var from `.env.example`.
5. In the **Settings** tab, generate a public domain. That URL (e.g. `https://sup-render-worker-production.up.railway.app`) is what n8n will call.
6. Confirm `GET /health` returns `{"ok": true}`.

## Migrating to your local 3900x/3080ti box later

When your self-hosted box is ready:
1. Build and run this same Dockerfile there (or run `uvicorn` directly).
2. Expose it via Cloudflare Tunnel, Tailscale, or reverse proxy.
3. Swap the worker URL inside your n8n HTTP Request node. Done.

You can also add `-hwaccel cuda -c:v h264_nvenc` flags to `render.py` to use the 3080ti for faster ffmpeg encoding.

## Request contract

POST `/render`
Headers: `X-API-Key: <WORKER_API_KEY>`
Body:
```json
{
  "row_number": 5,
  "youtube_url": "https://youtube.com/watch?v=abc",
  "clip_start": "1:30",
  "clip_end": "1:35",
  "headline": "When you finally calculate how much you paid in taxes...",
  "movie_show": "The Big Short",
  "scene_description": "optional, unused server-side"
}
```

Response `202 Accepted`:
```json
{"job_id": "a1b2c3d4e5f6", "status": "queued"}
```

When done, the worker POSTs to `N8N_CALLBACK_URL`:
```json
{
  "job_id": "a1b2c3d4e5f6",
  "status": "done",
  "row_number": 5,
  "drive_file_id": "1AbCdEf...",
  "drive_view_link": "https://drive.google.com/file/d/1AbCdEf.../view",
  "drive_download_link": "https://drive.google.com/uc?id=1AbCdEf...&export=download",
  "duration_s": 5.0,
  "filename": "The_Big_Short_5.mp4"
}
```

On failure:
```json
{
  "job_id": "...",
  "status": "failed",
  "row_number": 5,
  "error": "message"
}
```
