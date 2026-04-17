"""FastAPI entrypoint. Accepts render jobs, runs them in the background,
POSTs results to the n8n callback webhook when done."""
import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from pydantic import BaseModel

from .config import get_settings
from .render import run_render_job

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("render-worker")

# In-memory job store (fine for single-process worker; swap for Redis later if needed)
JOBS: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_settings()  # fail fast if env vars missing
    yield


app = FastAPI(title="SUP Render Worker", lifespan=lifespan)


class RenderRequest(BaseModel):
    row_number: int | str
    youtube_url: str
    clip_start: str           # e.g. "1:30" or "0:01:30"
    clip_end: str             # e.g. "1:35"
    headline: str = ""        # overlay text — if empty, AI picks from caption_ideas
    caption_idea_1: str = ""
    caption_idea_2: str = ""
    caption_idea_3: str = ""
    movie_show: str = ""      # for filename
    scene_description: str = ""


class RenderAccepted(BaseModel):
    job_id: str
    status: str = "queued"


def _check_auth(x_api_key: Optional[str]):
    if x_api_key != get_settings().worker_api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


async def _run_and_report(job_id: str, req: RenderRequest):
    settings = get_settings()
    JOBS[job_id]["status"] = "running"
    try:
        result = await asyncio.to_thread(run_render_job, job_id, req.model_dump(mode="json"))
        JOBS[job_id].update(status="done", result=result)
        payload = {
            "job_id": job_id,
            "status": "done",
            "row_number": req.row_number,
            **result,
        }
    except Exception as exc:
        log.exception("Job %s failed", job_id)
        JOBS[job_id].update(status="failed", error=str(exc))
        payload = {
            "job_id": job_id,
            "status": "failed",
            "row_number": req.row_number,
            "error": str(exc),
        }

    # POST to n8n callback
    headers = {}
    if settings.n8n_callback_auth:
        headers["Authorization"] = f"Bearer {settings.n8n_callback_auth}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(settings.n8n_callback_url, json=payload, headers=headers)
    except Exception:
        log.exception("Callback POST failed for job %s", job_id)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/render", response_model=RenderAccepted, status_code=202)
async def render(
    req: RenderRequest,
    background: BackgroundTasks,
    x_api_key: Optional[str] = Header(default=None),
):
    _check_auth(x_api_key)
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "request": req.model_dump(mode="json")}
    background.add_task(_run_and_report, job_id, req)
    log.info("Accepted job %s for row %s", job_id, req.row_number)
    return RenderAccepted(job_id=job_id)


@app.get("/status/{job_id}")
def status(job_id: str, x_api_key: Optional[str] = Header(default=None)):
    _check_auth(x_api_key)
    if job_id not in JOBS:
        raise HTTPException(404, "unknown job")
    return {"job_id": job_id, **JOBS[job_id]}
