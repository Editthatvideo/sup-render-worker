"""Render pipeline:
   yt-dlp → ffmpeg trim → 9:16 reframe → Whisper transcription → caption burn-in → Drive upload."""
from __future__ import annotations

import base64
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

def _write_cookies_file(work_dir: Path) -> Path | None:
    """Decode YOUTUBE_COOKIES_B64 env var to a Netscape cookies.txt file."""
    settings = get_settings()
    if not settings.youtube_cookies_b64:
        return None
    cookie_path = work_dir / "cookies.txt"
    cookie_path.write_bytes(base64.b64decode(settings.youtube_cookies_b64))
    log.info("Wrote YouTube cookies to %s", cookie_path)
    return cookie_path


def _build_search_query(movie_show: str, scene: str, api_key: str) -> list[str]:
    """Use GPT to craft 2-3 smart YouTube search queries."""
    prompt = (
        f"You are a YouTube search expert. I need to find a specific movie/TV clip.\n\n"
        f"Movie/Show: {movie_show}\n"
        f"Scene: {scene}\n\n"
        f"Generate 3 YouTube search queries that would find this clip. "
        f"Keep them short (3-6 words). Use the show name + key scene words. "
        f"Think about what clip channels like Movieclips or Binge Society would title it.\n\n"
        f"Reply with ONLY the 3 queries, one per line. No numbers, no quotes."
    )
    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=100,
        temperature=0.5,
    )
    queries = [q.strip() for q in resp.choices[0].message.content.strip().splitlines() if q.strip()]
    log.info("AI generated search queries: %s", queries)
    return queries or [f"{movie_show} scene clip"]


def _youtube_api_search(query: str, yt_api_key: str, max_results: int = 5) -> list[dict]:
    """Search YouTube using the official Data API v3 — no cookies needed."""
    import httpx
    params = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "videoDuration": "short",  # under 4 min — clips, not full movies
        "maxResults": max_results,
        "key": yt_api_key,
    }
    resp = httpx.get("https://www.googleapis.com/youtube/v3/search", params=params, timeout=15)
    resp.raise_for_status()
    items = resp.json().get("items", [])
    return [
        {
            "id": item["id"]["videoId"],
            "title": item["snippet"]["title"],
            "url": f"https://youtube.com/watch?v={item['id']['videoId']}",
        }
        for item in items
        if item["id"].get("videoId")
    ]


def search_youtube(movie_show: str, scene: str, work_dir: Path, openai_key: str = "") -> str:
    """Search YouTube using official API + AI-crafted queries."""
    settings = get_settings()

    if not settings.youtube_api_key:
        raise ValueError("YOUTUBE_API_KEY not set — needed for YouTube search")

    # Build smart queries with GPT
    if openai_key:
        queries = _build_search_query(movie_show, scene, openai_key)
    else:
        queries = [f"{movie_show} {scene[:30]} scene clip"]
    queries.append(f"{movie_show} scene clip")

    for q in queries:
        log.info("YouTube API search: %s", q)
        try:
            results = _youtube_api_search(q, settings.youtube_api_key)
            if results:
                best = results[0]
                log.info("YouTube API found: %s (%s)", best["title"], best["url"])
                return best["url"]
        except Exception as e:
            log.warning("YouTube API search failed for %r: %s", q, e)
            continue

    raise ValueError(f"No YouTube results for: {movie_show} — {scene}")


def _try_ytdlp(url: str, out_path: Path, use_cookies: bool, player_clients: list[str]) -> Path:
    """Single yt-dlp attempt with given settings."""
    ydl_opts = {
        "format": "best[height<=1080]/best",  # single stream — avoids merge issues
        "outtmpl": str(out_path.with_suffix(".%(ext)s")),
        "merge_output_format": "mp4",
        "quiet": False,
        "noplaylist": True,
        "extractor_args": {"youtube": {"player_client": player_clients}},
        "socket_timeout": 30,
        "remote_components": "ejs:github",  # solve YouTube JS challenges via deno
    }
    if use_cookies:
        cookie_file = _write_cookies_file(out_path.parent)
        if cookie_file:
            ydl_opts["cookiefile"] = str(cookie_file)
            log.info("Using cookies file (%d bytes)", cookie_file.stat().st_size)
        else:
            log.warning("Cookies requested but YOUTUBE_COOKIES_B64 is empty — skipping")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        final = Path(ydl.prepare_filename(info)).with_suffix(".mp4")
        if not final.exists():
            candidates = list(out_path.parent.glob(out_path.stem + ".*"))
            if not candidates:
                raise FileNotFoundError("yt-dlp produced no file")
            final = candidates[0]
    return final


def download_youtube(url: str, out_path: Path) -> Path:
    """Download with multiple fallback strategies — cookies are LAST resort."""
    strategies = [
        # 1. No cookies, web client — works for most public clips
        {"use_cookies": False, "player_clients": ["web"]},
        # 2. No cookies, mobile web client
        {"use_cookies": False, "player_clients": ["mweb"]},
        # 3. With cookies, web client — last resort
        {"use_cookies": True, "player_clients": ["web", "mweb"]},
    ]

    last_error = None
    for i, strat in enumerate(strategies, 1):
        label = f"Strategy {i}: cookies={strat['use_cookies']}, clients={strat['player_clients']}"
        log.info("Download attempt — %s", label)
        try:
            final = _try_ytdlp(url, out_path, **strat)
            log.info("Downloaded to %s via %s", final, label)
            return final
        except Exception as e:
            last_error = e
            log.warning("Failed (%s): %s", label, e)
            # Clean up partial files before next attempt
            for f in out_path.parent.glob(out_path.stem + ".*"):
                try:
                    f.unlink()
                except OSError:
                    pass

    raise RuntimeError(f"All download strategies failed for {url}: {last_error}")


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


def _first_sentence(text: str) -> str:
    """Extract just the first sentence — the hook line."""
    # Split on sentence-ending punctuation, keep the punctuation
    m = re.match(r"^(.+?[.!?])\s", text.strip())
    if m:
        return m.group(1).strip()
    # If no sentence break found, take first ~6 words as a short hook
    words = text.strip().split()
    return " ".join(words[:6])


def _wrap_text(text: str, max_chars: int = 25) -> str:
    """Word-wrap text into lines of ~max_chars, joined by newlines."""
    words = text.split()
    lines, current = [], ""
    for w in words:
        if current and len(current) + 1 + len(w) > max_chars:
            lines.append(current)
            current = w
        else:
            current = f"{current} {w}" if current else w
    if current:
        lines.append(current)
    return "\n".join(lines)


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

    # Headline — one drawtext per line (avoids newline glyph rendering as □)
    if headline.strip():
        lines = _wrap_text(headline.upper(), max_chars=20).split("\n")
        line_height = settings.headline_font_size + 14
        y_start = 80
        # Fade in over 0.8s: if(lt(t,0.8), t/0.8, 1)
        # Commas escaped with \ for ffmpeg filter-graph parser
        alpha_expr = "if(lt(t\\,0.8)\\,t/0.8\\,1)"

        for i, line in enumerate(lines):
            line_file = src.parent / f"headline_{i}.txt"
            line_file.write_text(line, encoding="utf-8")
            y = y_start + i * line_height
            filters.append(
                f"drawtext=fontfile={settings.font_path}:"
                f"textfile={shlex.quote(str(line_file))}:"
                f"fontsize={settings.headline_font_size}:fontcolor=white:"
                f"borderw=4:bordercolor=black:"
                f"x=(w-text_w)/2:y={y}:"
                f"alpha='{alpha_expr}'"
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

def _fmt_srt_ts(seconds: float) -> str:
    """Format seconds as SRT timestamp: HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _segment_dialogue(full_text: str, api_key: str) -> list[str]:
    """Use GPT to split raw transcript into natural subtitle lines."""
    prompt = (
        f"Split this dialogue into short subtitle lines for a TikTok video. Rules:\n"
        f"- Max 6 words per line\n"
        f"- Break at natural pauses, speaker changes, and sentence ends\n"
        f"- Add proper punctuation\n"
        f"- Each line should feel like a beat — one thought, one reaction\n"
        f"- If someone asks a question, that's its own line\n"
        f"- If someone answers, that's its own line\n\n"
        f"Transcript: {full_text}\n\n"
        f"Reply with ONLY the subtitle lines, one per line. No numbers, no timestamps."
    )
    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=300,
        temperature=0.2,
    )
    lines = [l.strip() for l in resp.choices[0].message.content.strip().splitlines() if l.strip()]
    log.info("GPT segmented dialogue into %d lines", len(lines))
    return lines


def transcribe_to_srt(src: Path, srt_out: Path, api_key: str):
    """Transcribe with word timestamps, use GPT to segment into natural dialogue beats."""
    client = OpenAI(api_key=api_key)
    with open(src, "rb") as f:
        resp = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="verbose_json",
            timestamp_granularities=["word"],
        )

    words = resp.words or []
    full_text = (resp.text or "").strip()

    if not words or not full_text:
        srt_out.write_text("", encoding="utf-8")
        return srt_out

    # GPT segments the dialogue into natural subtitle beats
    lines = _segment_dialogue(full_text, api_key)

    # Map each GPT line to word-level timestamps
    chunks = []
    wi = 0
    for line in lines:
        n = len(line.split())
        if wi >= len(words):
            break
        start = words[wi].start
        end_idx = min(wi + n - 1, len(words) - 1)
        end = words[end_idx].end
        chunks.append({"start": start, "end": end, "text": line})
        wi += n

    # Catch any leftover words
    if wi < len(words):
        leftover = " ".join(w.word for w in words[wi:]).strip()
        if leftover:
            chunks.append({
                "start": words[wi].start,
                "end": words[-1].end,
                "text": leftover,
            })

    # Build SRT
    srt_lines = []
    for i, c in enumerate(chunks, 1):
        srt_lines.append(str(i))
        srt_lines.append(f"{_fmt_srt_ts(c['start'])} --> {_fmt_srt_ts(c['end'])}")
        srt_lines.append(c["text"])
        srt_lines.append("")

    srt_out.write_text("\n".join(srt_lines), encoding="utf-8")
    log.info("Generated %d subtitle lines from %d words", len(chunks), len(words))
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


# ---- Auto-Clip Picker -------------------------------------------------------

def _fetch_youtube_transcript(url: str, work_dir: Path) -> str | None:
    """Grab YouTube's auto-captions without downloading the video."""
    ydl_opts = {
        "skip_download": True,
        "writeautomaticsub": True,
        "writesubtitles": True,
        "subtitleslangs": ["en"],
        "subtitlesformat": "vtt",
        "outtmpl": str(work_dir / "subs"),
        "quiet": True,
        "noplaylist": True,
        "extractor_args": {"youtube": {"player_client": ["web"]}},
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=False)
    except Exception as e:
        log.warning("Could not fetch YouTube captions: %s", e)
        return None

    # Find the subtitle file yt-dlp wrote
    for ext in ("en.vtt", "en.srt"):
        sub_file = work_dir / f"subs.{ext}"
        if sub_file.exists():
            raw = sub_file.read_text(encoding="utf-8")
            # Strip VTT headers and timestamps, keep just the text lines
            lines = []
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("WEBVTT") or line.startswith("Kind:") \
                   or line.startswith("Language:") or "-->" in line or line.isdigit():
                    continue
                # Remove VTT tags like <00:00:01.440>
                cleaned = re.sub(r"<[^>]+>", "", line).strip()
                if cleaned and cleaned not in lines[-1:]:  # dedup consecutive
                    lines.append(cleaned)
            return " ".join(lines)
    return None


def auto_pick_clip(url: str, scene: str, work_dir: Path, api_key: str,
                   min_dur: int = 5, max_dur: int = 15) -> tuple[float, float]:
    """Use YouTube captions + GPT to pick the best clip window."""
    log.info("Auto-picking best clip from %s", url)

    transcript = _fetch_youtube_transcript(url, work_dir)
    if not transcript:
        # No captions available — default to first 10 seconds
        log.warning("No YouTube captions found, defaulting to 0-10s")
        return 0.0, 10.0

    prompt = (
        f"You are a viral TikTok/Reels editor. Given a video transcript, pick the single "
        f"most compelling {min_dur}-{max_dur} second clip for maximum engagement.\n\n"
        f"Scene context: {scene or 'not provided'}\n\n"
        f"Full transcript:\n{transcript[:3000]}\n\n"
        f"Pick the segment with the best hook — something surprising, funny, emotional, "
        f"or controversial that makes viewers stop scrolling.\n\n"
        f"Reply with ONLY two numbers on one line: START_SECONDS END_SECONDS\n"
        f"Example: 45 57\n"
        f"No other text."
    )

    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=20,
        temperature=0.3,
    )
    answer = resp.choices[0].message.content.strip()
    log.info("AI picked clip window: %s", answer)

    # Parse "45 57" or "45.5 57.2"
    parts = answer.split()
    if len(parts) >= 2:
        try:
            start = float(parts[0])
            end = float(parts[1])
            if end > start:
                return start, end
        except ValueError:
            pass

    log.warning("Could not parse AI clip response %r, defaulting to 0-10s", answer)
    return 0.0, 10.0


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

    # Auto-search YouTube if no URL provided
    youtube_url = req.get("youtube_url", "").strip()
    if not youtube_url:
        movie_show = req.get("movie_show", "").strip()
        scene_desc = req.get("scene_description", "").strip()
        if not movie_show and not scene_desc:
            raise ValueError("Need either a YouTube URL or Movie/Show + Scene Description")
        youtube_url = search_youtube(movie_show, scene_desc, work, settings.openai_api_key)
        log.info("[%s] Auto-found YouTube URL: %s", job_id, youtube_url)

    # Auto-pick clip timestamps if not provided
    clip_start_raw = req.get("clip_start", "").strip()
    clip_end_raw = req.get("clip_end", "").strip()

    if clip_start_raw and clip_end_raw:
        start_s = parse_timestamp(clip_start_raw)
        end_s = parse_timestamp(clip_end_raw)
        if end_s <= start_s:
            raise ValueError(f"clip_end ({end_s}) must be > clip_start ({start_s})")
    else:
        start_s, end_s = auto_pick_clip(
            youtube_url,
            req.get("scene_description", ""),
            work,
            settings.openai_api_key,
        )
        log.info("[%s] AI auto-picked clip: %.1fs - %.1fs", job_id, start_s, end_s)

    log.info("[%s] Downloading %s", job_id, youtube_url)
    src = download_youtube(youtube_url, raw)

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
