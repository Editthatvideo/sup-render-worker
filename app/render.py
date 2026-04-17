"""Render pipeline:
   yt-dlp → ffmpeg trim → 9:16 reframe → Whisper transcription → caption burn-in → Drive upload."""
from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
from pathlib import Path

import yt_dlp
from openai import OpenAI

from .config import get_settings
from .drive import upload_file

log = logging.getLogger("render-worker.pipeline")

# ---- Timestamp helpers ------------------------------------------------------

_TS_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})(?:\.(\d+))?$")


def parse_timestamp(ts: str) -> float:
    """Parse '1:35', '1:35.5', or '0:01:35' to seconds (float)."""
    ts = ts.strip().lstrip("~").strip()
    # also accept plain numbers (seconds)
    if ts.replace(".", "", 1).isdigit():
        return float(ts)
    m = _TS_RE.match(ts)
    if not m:
        raise ValueError(f"Unparseable timestamp: {ts!r}")
    h, mn, s, frac = m.groups()
    total = (int(h) * 3600 if h else 0) + int(mn) * 60 + int(s)
    if frac:
        total += float(f"0.{frac}")
    return float(total)


# ---- Shell helper -----------------------------------------------------------

def run(cmd: list[str], check=True):
    log.info("$ %s", " ".join(shlex.quote(c) for c in cmd))
    r = subprocess.run(cmd, check=check, capture_output=True, text=True)
    if r.stdout:
        log.debug(r.stdout)
    if r.stderr:
        log.debug(r.stderr)
    return r


# ---- Download ---------------------------------------------------------------

def download_youtube(url: str, out_path: Path) -> Path:
    ydl_opts = {
        "format": "bestvideo[height<=1080]+bestaudio/best",
        "outtmpl": str(out_path.with_suffix(".%(ext)s")),
        "merge_output_format": "mp4",
        "quiet": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        # yt-dlp may pick a different extension; find the merged file
        final = Path(ydl.prepare_filename(info)).with_suffix(".mp4")
        if not final.exists():
            # fallback: pick whichever file it wrote next to out_path
            candidates = list(out_path.parent.glob(out_path.stem + ".*"))
            if not candidates:
                raise FileNotFoundError("yt-dlp produced no file")
            final = candidates[0]
    log.info("Downloaded to %s", final)
    return final


# ---- FFmpeg ops -------------------------------------------------------------

def trim_and_reframe(src: Path, dst: Path, start_s: float, end_s: float, w: int, h: int):
    """Trim [start_s, end_s] and reframe to w x h (center-crop after scale)."""
    duration = max(0.1, end_s - start_s)
    # scale so smaller dimension fills, then center-crop
    vf = f"scale='if(gt(a,{w}/{h}),-2,{w})':'if(gt(a,{w}/{h}),{h},-2)',crop={w}:{h}"
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start_s:.3f}",
        "-i", str(src),
        "-t", f"{duration:.3f}",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        str(dst),
    ]
    run(cmd)
    return dst


def burn_captions(src: Path, srt: Path, headline: str, dst: Path, settings):
    """Burn SRT subtitles at bottom + a headline card at top."""
    filters = []

    # Subtitles (force_style overrides the SRT defaults)
    if srt.exists() and srt.stat().st_size > 0:
        style = (
            f"FontName=DejaVu Sans Bold,FontSize=14,PrimaryColour=&H00FFFFFF,"
            f"OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,"
            f"Alignment=2,MarginV=80"
        )
        filters.append(f"subtitles={shlex.quote(str(srt))}:force_style='{style}'")

    # Headline via drawtext (top of frame, word-wrapped manually)
    if headline.strip():
        safe = (
            headline.replace("\\", "\\\\")
                    .replace(":", "\\:")
                    .replace("'", "\u2019")
        )
        filters.append(
            f"drawtext=fontfile={settings.font_path}:"
            f"text='{safe}':"
            f"fontsize={settings.headline_font_size}:fontcolor=white:"
            f"box=1:boxcolor=black@0.55:boxborderw=24:"
            f"x=(w-text_w)/2:y=120:"
            f"line_spacing=10"
        )

    vf = ",".join(filters) if filters else "null"
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(dst),
    ]
    run(cmd)
    return dst


# ---- Whisper ----------------------------------------------------------------

def transcribe_to_srt(src: Path, srt_out: Path, api_key: str):
    client = OpenAI(api_key=api_key)
    with open(src, "rb") as f:
        resp = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="srt",
        )
    srt_out.write_text(resp, encoding="utf-8")
    return srt_out


# ---- AI Caption Picker ------------------------------------------------------

def pick_best_caption(ideas: list[str], scene: str, api_key: str) -> str:
    """Use GPT to pick the best TikTok/Reels caption from up to 3 ideas."""
    ideas = [i.strip() for i in ideas if i.strip()]
    if not ideas:
        return ""
    if len(ideas) == 1:
        return ideas[0]

    numbered = "\n".join(f"{i+1}. {c}" for i, c in enumerate(ideas))
    prompt = (
        f"You are a viral social-media expert. Pick the single best caption for a "
        f"TikTok/Reels short video.\n\n"
        f"Scene: {scene}\n\n"
        f"Caption options:\n{numbered}\n\n"
        f"Reply with ONLY the winning caption text — no number, no quotes, no explanation."
    )

    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=120,
        temperature=0.3,
    )
    chosen = resp.choices[0].message.content.strip().strip('"').strip("'")
    log.info("AI picked caption: %s", chosen)
    return chosen


# ---- Orchestration ----------------------------------------------------------

def _slug(s: str) -> str:
    s = re.sub(r"[^\w\-]+", "_", s).strip("_")
    return s[:48] or "clip"


def run_render_job(job_id: str, req: dict) -> dict:
    settings = get_settings()
    work = Path(settings.work_dir) / job_id
    work.mkdir(parents=True, exist_ok=True)

    raw = work / "raw"
    trimmed = work / "trimmed.mp4"
    srt = work / "captions.srt"
    final = work / f"{_slug(req.get('movie_show', 'clip'))}_{req['row_number']}.mp4"

    log.info("[%s] Downloading %s", job_id, req["youtube_url"])
    src = download_youtube(req["youtube_url"], raw)

    start_s = parse_timestamp(req["clip_start"])
    end_s = parse_timestamp(req["clip_end"])
    if end_s <= start_s:
        raise ValueError(f"clip_end ({end_s}) must be > clip_start ({start_s})")

    log.info("[%s] Trimming %.2fs-%.2fs and reframing", job_id, start_s, end_s)
    trim_and_reframe(src, trimmed, start_s, end_s,
                     settings.output_width, settings.output_height)

    log.info("[%s] Transcribing", job_id)
    transcribe_to_srt(trimmed, srt, settings.openai_api_key)

    # Pick the best caption via AI if no explicit headline given
    headline = req.get("headline", "").strip()
    if not headline:
        ideas = [
            req.get("caption_idea_1", ""),
            req.get("caption_idea_2", ""),
            req.get("caption_idea_3", ""),
        ]
        headline = pick_best_caption(ideas, req.get("scene_description", ""), settings.openai_api_key)

    log.info("[%s] Burning captions + headline: %s", job_id, headline)
    burn_captions(trimmed, srt, headline, final, settings)

    log.info("[%s] Uploading to Drive", job_id)
    drive_result = upload_file(final, f"{final.name}")

    # cleanup large intermediates; keep srt for debugging via logs if needed
    try:
        src.unlink(missing_ok=True)
        trimmed.unlink(missing_ok=True)
    except Exception:
        pass

    return {
        "drive_file_id": drive_result["id"],
        "drive_view_link": drive_result["webViewLink"],
        "drive_download_link": drive_result.get("webContentLink"),
        "duration_s": end_s - start_s,
        "filename": final.name,
    }
