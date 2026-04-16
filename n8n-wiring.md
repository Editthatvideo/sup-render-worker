# n8n wiring

Two small additions to your existing sheet + two small workflows.

## New sheet columns (add to row 1 of "Pop Culture Content Bank")

| Column               | Purpose                                              |
| -------------------- | ---------------------------------------------------- |
| `Chosen URL`         | You paste the winning YouTube URL from Auto-Found Clips |
| `Clip Start`         | `1:30`  (timestamp to begin)                          |
| `Clip End`           | `1:35`  (timestamp to end)                            |
| `Headline`           | Short overlay text (pick a Caption Idea to use)       |
| `Render Status`      | `Queued` / `Rendering` / `Done` / `Failed`            |
| `Rendered Clip URL`  | Worker fills this (Drive link)                        |
| `Render Error`       | Worker fills this on failure                          |

Workflow: when you've decided on a clip, set **Chosen URL / Clip Start / Clip End / Headline** and set **Render Status = Queued**.

## Workflow 1 — "SUP — Render Queue" (new workflow)

Trigger: **Schedule Trigger** (every 5 min) OR keep it **Manual Trigger** and run on demand.

Nodes:

1. **Read Sheet** (Google Sheets, Get Row(s))
   - Document / Sheet: same as before
   - Filter: `Render Status` = `Queued`
   - Header Row 1, First Data Row 3

2. **If: has URL + timestamps** (IF node)
   - All of: `Chosen URL` notEmpty, `Clip Start` notEmpty, `Clip End` notEmpty

3. **Set: Mark Rendering** (Google Sheets, Append or Update, match on `row_number`)
   - Map only: `row_number`, `Render Status` = `Rendering`

4. **HTTP Request — POST /render** (HTTP Request node)
   - Method: POST
   - URL: `https://YOUR-RAILWAY-URL/render`
   - Auth: Header → `X-API-Key` = `{{ $env.WORKER_API_KEY }}`  (or paste the key)
   - Body (JSON):
     ```json
     {
       "row_number": {{ $json.row_number }},
       "youtube_url": "{{ $json['Chosen URL'] }}",
       "clip_start": "{{ $json['Clip Start'] }}",
       "clip_end": "{{ $json['Clip End'] }}",
       "headline": "{{ $json['Headline'] }}",
       "movie_show": "{{ $json['Movie / Show'] }}",
       "scene_description": "{{ $json['Scene Description'] }}"
     }
     ```
   - The worker returns `202` immediately with `{ job_id, status: "queued" }`. That's fine — the real result comes via the callback.

## Workflow 2 — "SUP — Render Done Callback" (new workflow)

Trigger: **Webhook** node
- HTTP Method: POST
- Path: `sup-render-done`  (full URL is what you paste into worker's `N8N_CALLBACK_URL`)
- Respond: Immediately (200)

Nodes:

1. **Webhook** (above)

2. **IF: status == done**
   - `{{ $json.body.status }}` equals `done`

3. **True branch → Update Sheet**
   - Google Sheets, Append or Update, match on `row_number`
   - Values:
     - `row_number` = `{{ $json.body.row_number }}`
     - `Render Status` = `Done`
     - `Rendered Clip URL` = `{{ $json.body.drive_view_link }}`
     - `Render Error` = `""`  (clear any prior error)

4. **False branch → Update Sheet (failed)**
   - Match on `row_number`, values:
     - `row_number` = `{{ $json.body.row_number }}`
     - `Render Status` = `Failed`
     - `Render Error` = `{{ $json.body.error }}`

Save and **activate** the webhook workflow (it must be active to receive POSTs).

## Flow end-to-end

1. You set `Chosen URL`, timestamps, and `Headline` on a sheet row, then set `Render Status = Queued`.
2. Workflow 1 runs (on schedule or manually), picks up Queued rows, marks them `Rendering`, POSTs to worker, moves on.
3. Worker downloads → trims → reframes → transcribes → burns captions → uploads.
4. Worker POSTs completion to Workflow 2's webhook.
5. Workflow 2 updates the sheet: `Render Status = Done`, `Rendered Clip URL` filled.
6. You open the Drive link, review, post to TikTok.

## Concurrency notes

- FastAPI's BackgroundTasks runs jobs inside the same worker process. Railway's default is 1 replica — that's fine to start.
- If you want parallelism later: bump Railway replicas (each has its own in-memory job store, that's OK because jobs are keyed by row_number via sheet state), or swap the in-memory store for Redis.
- Whisper, yt-dlp, and ffmpeg are all blocking — they run in `asyncio.to_thread` so FastAPI can still accept new jobs while one is encoding.

## Costs (at 45 clips/month, 30s each)

- Railway (1 small service): ~$5/mo
- OpenAI Whisper: 45 × 30s ≈ 22 min × $0.006 = $0.14/mo
- Google Drive storage: free (fits in the 15GB personal allotment easily)
- YouTube search (already wired): free tier quota

Total: ~$5.15/mo until you switch to self-hosted.
