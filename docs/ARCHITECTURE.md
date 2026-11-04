# Architecture

```
Browser (React/Vite)                       FastAPI (uvicorn), one process
  workspace token (localStorage)  ──HTTPS──▶  /api/*  ── auth: signed workspace token ──▶ routes
                                                             │
        ┌────────────────────────────────────────────────────┼──────────────────────────────────┐
        ▼                                                    ▼                                  ▼
  IngestService (thread pool)                         AnswerService                        ToolService
  validate → extract → normalise → chunk              route → rewrite/decompose →          summary (map-reduce), quiz,
  → embed (cached) → index → job status               hybrid retrieve → relevance gate →   flashcards, timeline, actions,
        │                                             grounded generation (JSON) →          contradictions, email, profile,
        ▼                                             citation + support verification      comparison
  SQLite (WAL) ◀──────────────────────────────────────────────┴──────────────────────────────────┘
  workspaces · sources · chunks (+ embeddings) · chunks_fts (FTS5/BM25) · embedding_cache · jobs · conversations · messages
```

## Request flow for a question
1. **Auth** – `Authorization: Bearer <workspace_id>.<hmac>`; the workspace id used by every query comes from the *verified* token, never from request parameters.
2. **Route** (`rag/router.py`, deterministic) – intent (`lookup`, `summarize`, `compare`, `meta`, `vague`), source scoping (selected sources, or sources named in the question), locator hints (`page 5`, `at 12:45`).
3. **Query expansion** – compound questions are decomposed into sub-queries; aggregate questions ("highest…", "total…") also target the per-column statistics blocks created at ingestion; follow-ups / long questions get one cheap LLM call for a standalone rewrite plus up to two keyword variants (`MULTI_QUERY`).
4. **Hybrid retrieval** (`rag/retrieval.py`) – cosine over stored embeddings + SQLite FTS5/BM25 (Porter-stemmed), reciprocal-rank fusion across all query variants, rerank by IDF-weighted query-term coverage + dense relevance (+ exact number matches, + locator boosts), MMR de-duplication, optional per-source balancing for comparisons, neighbour expansion for tables / transcript windows.
5. **Gates** – no passage above `MIN_RELEVANCE` → "not found" **without calling the LLM**; identifier-like tokens (`Q3`, `3.0`, `ef_construction`) that occur nowhere in the sources → "not found".
6. **Grounded generation** – evidence is wrapped in `<evidence id="S1" …>` blocks, framed as untrusted data; the model must return JSON `{answerable, answer, quotes, missing, followups}` with `[S#]` markers.
7. **Verification** (`rag/citations.py`) – unknown ids removed, quotes must exist in the cited passage, each cited sentence is checked for content-word coverage and numbers, uncited factual sentences are flagged, citations are renumbered and resolved to page / slide / sheet+rows / URL / timestamp deep-links. If nothing verifiable is left the answer is withheld ("unverified") and the closest passages are shown instead.
8. **Confidence** – derived from retrieval relevance of the *cited* passages and the fraction of supported sentences (not a probability); shown as a level plus the factors.

## Ingestion pipeline
`POST /api/sources/*` validates synchronously (type, size, URL safety, quotas, dedupe by content hash), creates a `source` + `job`, and returns immediately. A worker thread runs: **validating → extracting → chunking → embedding → indexing**, updating `jobs.progress/stage/message` (polled by the UI). Terminal states: `completed`, `partial` (searchable but some content skipped — see diagnostics), `failed` (nothing indexed; specific `error_code`), `cancelled`. On restart, orphaned jobs are marked failed.

| Format | Extraction | Preserves |
|---|---|---|
| PDF | pypdfium2 text, pdfplumber tables, outline → headings, repeated header/footer removal; OCR (tesseract or Gemini vision) for pages without a text layer | page numbers, headings, tables as markdown |
| DOCX | python-docx, body order | heading hierarchy, lists, tables (no page numbers — Word has none) |
| PPTX | python-pptx | slide numbers, titles, tables, speaker notes |
| XLSX / CSV / TSV | openpyxl / csv | per-sheet tables (25 rows per passage, header repeated), row ranges, **deterministic column statistics** |
| TXT / MD / HTML / JSON | decoding with fallbacks, markdown parser, trafilatura | headings, code blocks, tables |
| DOC / PPT / XLS / RTF / ODT | converted with LibreOffice if installed | as above |
| Web | SSRF-safe fetch, robots.txt, trafilatura main-content extraction (+ BeautifulSoup fallback), optional Playwright rendering, crawler with depth/page/path limits and sitemap discovery | URL per passage, heading path, canonical URL |
| YouTube | youtube-transcript-api → Supadata → yt-dlp → (opt-in) Gemini video transcription | exact start/end seconds, chapters as headings |

**Chunking** (`ingest/chunking.py`): never merges across headings, pages, slides or web pages; tables and code blocks are atomic (large tables split by rows with the header repeated); transcripts are windowed by time (~75 s) with one-segment overlap; ~1100 characters target with sentence-level overlap; each passage is embedded together with its title + heading path ("contextual retrieval" without extra LLM calls).

## Data model
SQLite in WAL mode with versioned migrations (`app/db.py`). Every user-content table carries `workspace_id`; DB helpers require it. Vectors are float32 BLOBs; an LRU cache (256 MB) keeps per-source matrices in memory. `embedding_cache` makes re-indexing and duplicate text free and is pruned when sources are deleted or refreshed.

## Providers (`app/providers`)
* `GeminiLLM` – `GEMINI_MODEL` (default `gemini-2.5-flash`). **Never switched silently**: an optional `GEMINI_FALLBACK_MODEL` is used only on a 404 and the answering model is reported in every response. `/api/health/ready` checks the configured model. Retry with backoff on 429/5xx/timeouts, concurrency cap, circuit breaker.
* `GeminiEmbedder` – `gemini-embedding-001`, 768 dims (Matryoshka truncation), L2-normalised, task types `RETRIEVAL_DOCUMENT` / `RETRIEVAL_QUERY`.
* `GroqLLM` – optional alternative chat provider (`LLM_PROVIDER=groq`).
* `FakeLLM`, `HashEmbedder`, `FakeOCR` – **test doubles only**; never selected automatically.

## Why no autonomous multi-agent loop?
The brief asked for specialised agents only where they add measurable value. Each stage above is a component with typed input/output, a timeout, and a trace entry (`trace` in every response). The LLM is never given tools or the ability to fetch URLs, so injected instructions have nothing to hijack; routing, retrieval and verification are deterministic and testable; the LLM is used for rewriting (cheap), answering, and tool generation. The original "self-reflection" step was removed: it let the model rewrite its answer without any grounding check, which could *add* unsupported claims. Verification now only removes or flags.

## Known scaling limits
* One process, SQLite: suitable for a single instance and thousands of users with modest concurrency, not horizontal scaling.
* Vector search is exact (brute force): ~O(chunks × dims) per query; comfortable up to ~50k passages per workspace.
* Rate limits are in-process; behind multiple instances use a shared limiter at the proxy.
* Ingestion uses 2 worker threads by default (`INGEST_WORKERS`); large PDFs with OCR or crawls take minutes.
