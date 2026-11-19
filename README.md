# 🧠 TalkDox

Chat with your **documents**, **websites** and **YouTube videos**. Answers are written only from your sources, every
claim carries a citation you can open (page, slide, sheet rows, URL section or the exact video moment), and when the
answer isn't in the sources TalkDox says so instead of guessing.

[Live demo](https://talkdox-ai.vercel.app) · [Architecture](docs/ARCHITECTURE.md) · [Security & privacy](docs/SECURITY.md) · [Deployment](docs/DEPLOYMENT.md) · [Evaluation](docs/EVALUATION.md) · [Testing](docs/TESTING.md) · [API](docs/API.md)

> **Status:** the pipeline, API and UI are covered by 256 automated backend tests and a browser smoke test, but the
> live Gemini, YouTube and public-web integrations could not be exercised in the build environment. Read
> [docs/TESTING.md](docs/TESTING.md) ("What is NOT verified") and [docs/EVALUATION.md](docs/EVALUATION.md) before a public launch.

## What it does
| Source | Support | Honest limits |
|---|---|---|
| **Documents** | PDF (text, tables, outline, repeated header/footer removal; scanned pages via OCR), DOCX, PPTX, XLSX/CSV/TSV (tables + computed column statistics), TXT/MD/HTML/JSON; legacy DOC/PPT/XLS/RTF/ODT if LibreOffice is installed | 25 MB / 1000 pages by default; password-protected, corrupt, empty or unsupported files fail with a specific reason; charts/diagrams inside images aren't read; Word has no page numbers (cites section headings) |
| **Websites** | main-content extraction (no nav/ads), canonical URLs, robots.txt, sitemap discovery, multi-page crawl with depth / page / path limits and de-duplication, PDF links, optional sandboxed JS rendering, SSRF-proof fetching | no logins, paywalls or CAPTCHAs (by design); sites that block bots or need JS (when rendering is off) are reported, not faked |
| **YouTube** | video, Shorts, playlists; manual-first captions in any language, chapters, exact timestamps with clickable deep links; fallbacks: Supadata → yt-dlp → (opt-in) AI-generated transcript, clearly labelled | transcript only — it does **not** see the picture; no captions + no fallback ⇒ explained failure; YouTube may block datacenter IPs (see DEPLOYMENT.md) |

**Trustworthy answers:** hybrid retrieval (semantic + BM25) → relevance gates → structured, citation-bound generation → deterministic verification of ids, quotes, numbers and per-sentence support → withheld (not guessed) when nothing can be verified. The UI shows evidence strength, flagged statements, the verified quote and a source viewer with the exact extracted text the AI saw.

**Study & work tools** (all cite their passages and report how much of the source they read): summary (map-reduce over the *whole* source), source profile, flashcards, quiz, timeline, action items, contradictions, email draft, multi-source comparison. Plus chat modes/perspectives, history & export, voice input, analytics.

**Workspace model:** every browser gets a private, token-protected workspace (no accounts); sources process in the background with real progress, partial results and a per-source extraction report.

## Quick start
```bash
# backend
cd backend && python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
cp .env.example .env              # set GEMINI_API_KEY
uvicorn main:app --reload --port 8000
# frontend (another terminal)
cd frontend && npm ci && npm run dev        # http://localhost:5173
```
No key? `python backend/scripts/run_offline_demo.py` runs the full stack with test doubles (UI shows a banner; answers are extracted sentences, not AI output).
Docker: `cp backend/.env.example backend/.env && docker compose up --build` → <http://localhost:8080>.
Tests: `cd backend && python -m pytest tests -q` · `cd frontend && npm run lint && npm run build`.

## Model & data
Default model `gemini-3.5-flash` with `gemini-3.1-flash-lite` as an explicit fallback when the primary is overloaded or unavailable (configurable via `GEMINI_MODEL` / `GEMINI_FALLBACK_MODEL`; check <https://ai.google.dev/gemini-api/docs/deprecations> — `/api/health/ready` reports whether the primary resolves, and the model that answered is shown with every response, never switched silently) and `gemini-embedding-001` embeddings. Your question and the retrieved passages are sent to Google; the list of what leaves your server is in [docs/SECURITY.md](docs/SECURITY.md) and in the app (*Insights → Privacy & data*). Original uploads are not stored; idle workspaces are deleted after `RETENTION_DAYS`.

## Project layout
```
backend/app/      config · db (SQLite+FTS5) · net (SSRF guard) · security · providers/ (gemini, groq, ocr, test doubles)
                  ingest/ (documents, htmlextract, web crawler, jsrender, youtube, chunking, pipeline) · rag/ (router, retrieval, answer, citations, prompts, tools) · api/routes
backend/tests/    ~256 tests incl. SSRF, security, Gemini-protocol stub and the labelled RAG evaluation (tests/eval)
backend/scripts/  run_offline_demo.py · eval_live.py
frontend/src/     pages (Landing, Upload, Chat) · components (AddSources, SourcesPanel, ChatPane, StudyTools, Citations viewer, Insights) · context/ · api.js
docs/             architecture, security/privacy, deployment, evaluation, testing, API
```

## What changed from v1 (honest summary)
v1 kept one global in-memory document for *all* visitors, answered with Llama via Groq while the README claimed Gemini, analysed only the first 4–6k characters for tools, used a fixed-formula "confidence", faked upload progress with timers, accepted any URL (SSRF), and removed nothing it couldn't verify. v2 replaces those with isolated persistent workspaces, evidence-bound answers, whole-source tools, real jobs and a safe fetcher. Details in the pull-request description / docs.

Built by **Vansh Mahajan** · MIT License
