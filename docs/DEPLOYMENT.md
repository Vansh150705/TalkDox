# Deployment guide

## 1. Local development
```bash
# backend (Python 3.12+)
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # put GEMINI_API_KEY in .env
uvicorn main:app --reload --port 8000

# frontend (Node 22+)
cd frontend
npm ci
npm run dev                      # http://localhost:5173, proxies /api to http://localhost:8000
```
No API key? Run the **offline demo** (hashed embeddings + an extractive "reader"; the UI shows a banner saying so):
```bash
cd backend && python scripts/run_offline_demo.py
```

## 2. Docker / docker-compose (self-hosting)
```bash
cp backend/.env.example backend/.env      # set GEMINI_API_KEY (and SECRET_KEY for >1 instance)
docker compose up --build                  # http://localhost:8080  (nginx serves the SPA and proxies /api)
```
Build args for the API image: `WITH_OCR=1` (tesseract, default), `WITH_LIBREOFFICE=1` (legacy `.doc/.ppt/.xls/.rtf/.odt`), `WITH_JS=1` (Playwright + Chromium; also set `ENABLE_JS_RENDER=1` and read the SSRF note in SECURITY.md). Data lives in the `talkdox-data` volume (`/data`).

## 3. Current production topology (Vercel + Hugging Face Space)
* **Backend → Hugging Face Space (Docker)**: the `backend/` folder as the Space. Set secrets `GEMINI_API_KEY`, `SECRET_KEY`, `CORS_ORIGINS=https://<your-vercel-domain>`, `TALKDOX_ENV=production`. The Space listens on `7860`.
  * **Persistence:** a free Space has ephemeral disk — sources, chats and tokens vanish on restart (users then see a "new workspace" notice). Attach persistent storage and set `DATA_DIR=/data` for durable data.
  * Free Spaces sleep; the UI shows a real "connecting" state with retries.
* **Frontend → Vercel**: `frontend/vercel.json` rewrites `/api/*` to the Space (edit the destination if you rename it) and adds security headers. Vercel rewrites have request-duration limits; if long answers/ingests get cut off, set `VITE_API_URL` to the backend URL and add the Vercel origin to `CORS_ORIGINS` so the browser talks to the API directly.

## 4. Configuration reference (environment variables)
See `backend/.env.example` for the annotated list. Essentials:

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | – | Required for answers **and** embeddings |
| `GEMINI_MODEL` | `gemini-3.5-flash` | Check <https://ai.google.dev/gemini-api/docs/deprecations>: model availability is time-limited (`gemini-2.5-*` is already closed to new API keys); `/api/health/ready` reports whether the configured model resolves. |
| `GEMINI_FALLBACK_MODEL` | `gemini-3.1-flash-lite` | Used only on 404 / 429 / 5xx / timeout of the primary and reported in every response; set it empty to disable. The lite model is faster and more available but less accurate on multi-row calculations. |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | `gemini-embedding-001` / 768 | Changing either requires re-adding sources (vectors are not comparable) |
| `TALKDOX_ENV` | `development` | `production` enables strict checks (no `*` CORS, no private-network fetching) and HSTS |
| `CORS_ORIGINS` | `http://localhost:5173` | Comma-separated explicit origins |
| `DATA_DIR`, `SECRET_KEY` | `./data`, auto | SQLite + temp files; token signing secret (auto-generated into `DATA_DIR/.secret_key`) |
| `RETENTION_DAYS` | 30 | Idle workspaces are deleted (0 = never) |
| `MAX_UPLOAD_MB`, `MAX_PDF_PAGES`, `MAX_SOURCES_PER_WORKSPACE`, `RATE_*` | 25, 1000, 40, … | Limits and cost controls |
| `SUPADATA_API_KEY`, `YOUTUBE_API_KEY`, `YOUTUBE_PROXY_URL` | – | YouTube resilience (see below) |
| `ENABLE_JS_RENDER`, `OCR_ENGINE`, `ENABLE_GEMINI_VIDEO_FALLBACK` | off / auto / off | Optional capabilities |

**Startup validation:** the server logs missing keys at boot, `GET /api/config` returns `problems` (shown as a banner in the UI), and `GET /api/health/ready` returns `ready` / `degraded` with provider checks (the Gemini model check is cached 5 min).

## 5. Operations
* **Health:** `GET /api/health` (liveness), `GET /api/health/ready` (database, LLM model, embedder). The Docker image has a `HEALTHCHECK`.
* **Logs:** one structured line per request with a request id; keys are redacted; questions and answers are never logged. Ship stdout to your log platform; alert on 5xx rate and `provider_*` error codes.
* **Backups:** the entire state is `DATA_DIR/talkdox.sqlite3`. Use `sqlite3 talkdox.sqlite3 ".backup backup.sqlite3"` (safe while running). Also keep `DATA_DIR/.secret_key` (or your `SECRET_KEY`), otherwise all workspace tokens become invalid. Restore = stop, replace the file, start.
* **Upgrades:** schema migrations run on start (`app/db.py: MIGRATIONS`). The pre-2.0 service kept everything in process memory, so there is nothing to migrate.
* **Reverse proxy:** forward `X-Forwarded-For` and set `TRUST_PROXY_HEADERS=1` plus `TRUSTED_PROXY_HOPS` (number of proxies that append to the header; the client address is taken that many entries from the *end*, so forged left-most entries are ignored). Without it every visitor behind the proxy shares the proxy's IP and the per-IP workspace-creation limit applies to all of them together. For Vercel → Hugging Face use `TRUSTED_PROXY_HOPS=2` (verify against the actual header in your logs); disable response buffering for `/api/chat/stream`; set `client_max_body_size` ≥ `MAX_UPLOAD_MB`.
* **Cost control:** each question = 1 embedding + 0–1 rewrite + 1 answer call; whole-source tools cost more (large sources use map-reduce). Set Gemini quota/budget alerts and tune `RATE_CHAT_PER_MIN`, `MAX_SOURCES_PER_WORKSPACE`, `MAX_PDF_PAGES`. `OCR_ENGINE=gemini` spends tokens per scanned page (`MAX_OCR_PAGES`, default 60); prefer tesseract for bulk scans.

## 6. YouTube on cloud hosts
YouTube frequently blocks transcript requests from datacenter IPs (including Hugging Face / many VPS ranges). The fallback chain is: `youtube-transcript-api` → Supadata (`SUPADATA_API_KEY`) → `yt-dlp` → opt-in Gemini video transcription. If every step fails the user sees the specific reason (`blocked_by_youtube`, `captions_disabled`, `video_unavailable`, `age_restricted`, `live_stream`, `no_transcript`). For reliability on a cloud host configure Supadata or `YOUTUBE_PROXY_URL` (a residential/rotating proxy you are licensed to use). None of these paths could be exercised against live YouTube in the development environment — verify them in yours.

## 7. Capacity and limits (be realistic)
* Single process + SQLite: one instance. Expect comfortable operation for tens of concurrent users; ingestion is the bottleneck (2 worker threads, I/O-bound on embeddings).
* ≤ ~50k passages per workspace for sub-100 ms retrieval (exact vector search).
* A 1000-page text PDF indexes in minutes; scanned PDFs are limited by OCR speed/cost; JS rendering adds seconds per page.
* To scale horizontally you would need: Postgres (pgvector) instead of SQLite, a shared queue/worker pool, Redis rate limiting, object storage for temp files. Not implemented.
