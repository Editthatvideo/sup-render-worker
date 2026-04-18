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


def download_youtube(url: str, out_path: Path) -> Path:
    """Download via yt-dlp CLI with --remote-components for JS challenge solving."""
    cookie_file = _write_cookies_file(out_path.parent)
    out_template = str(out_path.with_suffix(".%(ext)s"))

    cmd = [
        "yt-dlp",
        "--remote-components", "ejs:github",
        "-f", "bestvideo[height<=1080]+bestaudio/bestvideo+bestaudio/best[height>=480]/best",
        "-o", out_template,
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--socket-timeout", "30",
        "--extractor-args", "youtube:player_client=web,tv_embedded",
    ]
    if cookie_file:
        cmd.extend(["--cookies", str(cookie_file)])
        log.info("Using cookies file (%d bytes)", cookie_file.stat().st_size)
    cmd.append(url)

    log.info("Downloading: %s", " ".join(shlex.quote(c) for c in cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    log.info("yt-dlp stdout: %s", result.stdout[-2000:] if result.stdout else "(empty)")
    if result.stderr:
        log.info("yt-dlp stderr: %s", result.stderr[-2000:])

    if result.returncode != 0:
        # If cookies caused the failure, retry without them
        if cookie_file and ("no longer valid" in (result.stderr or "") or "Requested format" in (result.stderr or "")):
            log.warning("Retrying download WITHOUT cookies (they may be causing issues)")
            cmd_no_cookies = [c for c in cmd if c != "--cookies" and c != str(cookie_file)]
            result = subprocess.run(cmd_no_cookies, capture_output=True, text=True, timeout=300)
            log.info("yt-dlp retry stdout: %s", result.stdout[-2000:] if result.stdout else "(empty)")
            if result.stderr:
                log.info("yt-dlp retry stderr: %s", result.stderr[-2000:])
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp failed (exit {result.returncode}): {result.stderr[-500:]}")

    # Find the output file
    final = out_path.with_suffix(".mp4")
    if not final.exists():
        candidates = list(out_path.parent.glob(out_path.stem + ".*"))
        candidates = [c for c in candidates if c.suffix not in (".txt", ".part")]
        if not candidates:
            raise FileNotFoundError(f"yt-dlp produced no file. stdout: {result.stdout[-300:]}")
        final = candidates[0]

    # Log source resolution so we can diagnose quality issues
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,codec_name", "-of", "csv=p=0", str(final)],
            capture_output=True, text=True, timeout=10,
        )
        log.info("Downloaded source info: %s  file=%s", probe.stdout.strip(), final)
    except Exception:
        pass

    log.info("Downloaded to %s", final)
    return final


# ---- FFmpeg ops -------------------------------------------------------------

def _detect_face_x(src: Path, start_s: float, duration: float) -> float | None:
    """Sample a few frames and detect where faces are. Returns avg X ratio (0.0-1.0) or None."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        log.warning("OpenCV not available — falling back to center crop")
        return None

    cascade_paths = [
        "/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml",
        "/usr/local/share/opencv4/haarcascades/haarcascade_frontalface_default.xml",
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml",
    ]
    cascade_file = None
    for p in cascade_paths:
        if Path(p).exists():
            cascade_file = p
            break
    if not cascade_file:
        log.warning("No haarcascade file found — falling back to center crop")
        return None

    face_cascade = cv2.CascadeClassifier(cascade_file)

    # Sample 3 frames spread across the clip
    sample_times = [start_s + duration * t for t in (0.2, 0.5, 0.8)]
    all_face_x = []

    for t in sample_times:
        # Extract a single frame
        frame_path = src.parent / f"_face_probe_{t:.1f}.jpg"
        probe_cmd = [
            "ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", str(src),
            "-frames:v", "1", "-q:v", "5", str(frame_path),
        ]
        r = subprocess.run(probe_cmd, capture_output=True, timeout=10)
        if r.returncode != 0 or not frame_path.exists():
            continue

        img = cv2.imread(str(frame_path))
        if img is None:
            frame_path.unlink(missing_ok=True)
            continue

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        img_h, img_w = img.shape[:2]
        faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(40, 40))

        for (fx, fy, fw, fh) in faces:
            # Only count faces that are at least 8% of frame width (meaningful size)
            face_ratio = fw / img_w
            if face_ratio < 0.08:
                continue
            # Weight larger faces more (main character vs background extra)
            center_x = (fx + fw / 2) / img_w
            # Add multiple times based on size — bigger face = more weight
            weight = max(1, int(face_ratio * 20))
            all_face_x.extend([center_x] * weight)

        frame_path.unlink(missing_ok=True)

    if not all_face_x:
        log.info("No significant faces detected — using center crop")
        return None

    avg_x = float(np.mean(all_face_x))
    # If face is very close to center (0.4-0.6), just use center crop — don't overcorrect
    if 0.4 <= avg_x <= 0.6:
        log.info("Face near center (%.2f) — using standard center crop", avg_x)
        return None

    log.info("Detected faces, weighted avg X position: %.2f", avg_x)
    return avg_x


def _get_video_width(src: Path) -> int | None:
    """Get video width via ffprobe."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width", "-of", "csv=p=0", str(src)],
            capture_output=True, text=True, timeout=10,
        )
        return int(r.stdout.strip())
    except Exception:
        return None


def trim_and_reframe(src: Path, dst: Path, start_s: float, end_s: float, w: int, h: int):
    """Trim [start_s, end_s] and reframe to w x h with face-aware cropping."""
    duration = max(0.1, end_s - start_s)

    # Get source dimensions first — needed for both face crop and quality logging
    src_w, src_h = None, None
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(src)],
            capture_output=True, text=True, timeout=10,
        )
        parts = r.stdout.strip().split(",")
        src_w, src_h = int(parts[0]), int(parts[1])
        log.info("Source dimensions: %dx%d → target %dx%d", src_w, src_h, w, h)
        if src_h < 480:
            log.warning("LOW QUALITY SOURCE: only %dp — output will look blurry!", src_h)
    except Exception:
        log.warning("Could not probe source dimensions")

    # Detect face position for smart crop
    face_x = _detect_face_x(src, start_s, duration)

    if face_x is not None and src_w and src_h:
        # After scaling to fill target height, width becomes:
        scaled_w = int(src_w * (h / src_h))
        # Make sure scaled_w is even (ffmpeg requirement)
        scaled_w = scaled_w + (scaled_w % 2)
        # Calculate crop X: center on face position, clamp to valid range
        crop_x = int(face_x * scaled_w - w / 2)
        crop_x = max(0, min(crop_x, scaled_w - w))
        vf = f"scale={scaled_w}:{h},crop={w}:{h}:{crop_x}:0"
        log.info("Smart crop — face at %.0f%%, scaled_w=%d, crop_x=%d", face_x * 100, scaled_w, crop_x)
    else:
        # Fallback: center crop
        vf = f"scale='if(gt(a,{w}/{h}),-2,{w})':'if(gt(a,{w}/{h}),{h},-2)',crop={w}:{h}"
        log.info("Using center crop fallback")

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start_s:.3f}",
        "-i", str(src),
        "-t", f"{duration:.3f}",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "17",
        "-c:a", "aac", "-b:a", "192k",
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
        line_height = settings.headline_font_size + 18
        y_start = 150
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
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "17",
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
    """Grab YouTube's auto-captions via CLI (supports --remote-components)."""
    cookie_file = _write_cookies_file(work_dir)
    sub_out = str(work_dir / "subs")

    cmd = [
        "yt-dlp",
        "--remote-components", "ejs:github",
        "--skip-download",
        "--write-auto-sub",
        "--write-sub",
        "--sub-lang", "en",
        "--sub-format", "vtt",
        "-o", sub_out,
        "--no-playlist",
        "--extractor-args", "youtube:player_client=web,tv_embedded",
    ]
    if cookie_file:
        cmd.extend(["--cookies", str(cookie_file)])
    cmd.append(url)

    log.info("Fetching transcript: %s", " ".join(shlex.quote(c) for c in cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            log.warning("Transcript fetch failed (exit %d): %s", result.returncode, result.stderr[-500:])
            # Retry without cookies if they might be the issue
            if cookie_file:
                log.info("Retrying transcript fetch without cookies")
                cmd_no_cookies = [c for c in cmd if c != "--cookies" and c != str(cookie_file)]
                result = subprocess.run(cmd_no_cookies, capture_output=True, text=True, timeout=60)
                if result.returncode != 0:
                    log.warning("Transcript retry also failed (exit %d)", result.returncode)
    except Exception as e:
        log.warning("Could not fetch YouTube captions: %s", e)
        return None

    # Find the subtitle file yt-dlp wrote
    for pattern in ("subs.en.vtt", "subs.en.srt", "subs*.en.vtt", "subs*.en.srt"):
        matches = list(work_dir.glob(pattern))
        if matches:
            raw = matches[0].read_text(encoding="utf-8")
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
            transcript = " ".join(lines)
            log.info("Got transcript (%d chars): %s...", len(transcript), transcript[:200])
            return transcript if transcript else None

    # Nothing found with cookies — try once more without if we haven't already
    if cookie_file:
        log.info("No subs found — trying transcript fetch without cookies")
        cmd_no_cookies = [c for c in cmd if c != "--cookies" and c != str(cookie_file)]
        try:
            subprocess.run(cmd_no_cookies, capture_output=True, text=True, timeout=60)
        except Exception:
            pass
        for pattern in ("subs.en.vtt", "subs.en.srt", "subs*.en.vtt", "subs*.en.srt"):
            matches = list(work_dir.glob(pattern))
            if matches:
                raw = matches[0].read_text(encoding="utf-8")
                lines = []
                for line in raw.splitlines():
                    line = line.strip()
                    if not line or line.startswith("WEBVTT") or line.startswith("Kind:") \
                       or line.startswith("Language:") or "-->" in line or line.isdigit():
                        continue
                    cleaned = re.sub(r"<[^>]+>", "", line).strip()
                    if cleaned and cleaned not in lines[-1:]:
                        lines.append(cleaned)
                transcript = " ".join(lines)
                log.info("Got transcript on cookieless retry (%d chars)", len(transcript))
                return transcript if transcript else None

    log.warning("No subtitle files found after yt-dlp transcript fetch")
    return None


def auto_pick_clip(url: str, scene: str, work_dir: Path, api_key: str,
                   min_dur: int = 5, max_dur: int = 15) -> tuple[float, float]:
    """Use YouTube captions + GPT to pick the best clip window."""
    log.info("Auto-picking best clip from %s", url)

    transcript = _fetch_youtube_transcript(url, work_dir)
    if not transcript:
        # No captions available — skip past likely establishing shot
        log.warning("No YouTube captions found — defaulting to 8-20s to skip establishing shots")
        return 8.0, 20.0

    prompt = (
        f"You are a viral TikTok/Reels editor. Given a video transcript, pick the single "
        f"most compelling {min_dur}-{max_dur} second clip for maximum engagement.\n\n"
        f"Scene context: {scene or 'not provided'}\n\n"
        f"Full transcript:\n{transcript[:3000]}\n\n"
        f"CRITICAL RULES:\n"
        f"- Pick a segment where someone is TALKING — we need faces on screen\n"
        f"- NEVER pick the first 5 seconds (usually establishing shots with no people)\n"
        f"- Pick dialogue, reactions, arguments — moments with visible emotion\n"
        f"- The best hook is a person saying something surprising, funny, or controversial\n\n"
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
                # Safety: if AI picked the very start, bump forward (establishing shots)
                if start < 3.0:
                    log.warning("AI picked near-start clip (%.1f-%.1f), bumping to avoid establishing shot", start, end)
                    start = 5.0
                    end = max(end, start + min_dur)
                return start, end
        except ValueError:
            pass

    log.warning("Could not parse AI clip response %r, defaulting to 5-15s", answer)
    return 5.0, 15.0


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
