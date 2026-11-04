# Testing

```bash
cd backend && pip install -r requirements-dev.txt
python -m pytest tests -q                 # everything (~45 s)
python -m pytest tests/eval -q -s         # offline RAG evaluation, prints and writes tests/eval/last_report.json
cd ../frontend && npm run lint && npm run build
# browser test against the real UI (offline demo backend, no keys):
(cd ../backend && python scripts/run_offline_demo.py &) ; npx vite --host 127.0.0.1 & 
npm install --no-save playwright && node e2e/smoke.mjs http://127.0.0.1:5173
```

## What is covered (256 backend tests + browser smoke test)
| Area | File | Highlights |
|---|---|---|
| Documents | `test_documents.py` | real PDFs (text, tables, headings, 150 pages), scanned PDF without/with OCR, mixed scanned/text PDF → `partial`, password-protected, truncated/corrupt, mislabelled and fake extensions, unsupported types, empty files, oversize upload, page limit, DOCX, PPTX (notes, tables), multi-sheet XLSX with statistics, CSV/TSV, MD/HTML/JSON, encodings (cp1252, UTF-16), ZIP bomb, duplicate upload, legacy `.doc` through LibreOffice, bounded memory |
| Web | `test_web.py` | boilerplate removal, code/tables/lists, long docs with heading paths, malformed HTML, non-UTF-8, 401/403/404/429/500, empty/CAPTCHA pages, JS-only page (explained) and **real Chromium rendering**, redirects, non-HTML types, PDF URLs, robots.txt (disallow / 5xx / missing), nested documentation crawl with scope, depth, page limits, canonical + content de-duplication and sitemap discovery, partial crawls, URL collections, incremental re-index, failed refresh keeps old index, timeouts, refused connections |
| SSRF | `test_ssrf.py` | ~40 hostile URL forms, real and scripted DNS, mixed answers, **DNS-rebinding pinning proof**, redirect-to-metadata, https→http, loops, size and gzip-bomb caps, content types, no cookies/credentials, URL normalisation |
| YouTube | `test_youtube.py` | 12 accepted / 12 rejected URL forms (incl. look-alike hosts), start-times, playlists, chapter parsing rules, fallback chain, terminal failures (disabled, unavailable, age-restricted, live, blocked, empty), auto-generated / language warnings, coverage warning, AI-generated transcript labelling, 5.5-hour video with correct timestamp deep link, timestamp-scoped questions, library error mapping, Supadata & json3 parsing |
| Answering | `test_chat.py` | verifier unit tests (invented ids, wrong numbers, unsupported claims, quote checks, marker variants, tables), grounded answers with page citations, tables, refusal without LLM call, hallucinated-citation withholding, image/HTML stripping, scoping, multi-source, comparison, follow-ups, vague/meta questions, rate-limit/outage handling, malformed model JSON repair, SSE stream, prompt contents |
| Security | `test_security.py` | auth on every route, forged tokens, cross-workspace isolation (HTTP and SQL level), deletion completeness incl. vectors, retention purge, prompt-injection documents (forged evidence tags), XSS-laden pages, filename sanitising, log redaction, rate limits, quotas, headers, CORS, error shape, production config refusal, orphaned jobs, cancellation |
| Tools | `test_tools.py` | items without verifiable citations are dropped, sampling spread over the whole source (regression for the "first 4000 characters" bug), map-reduce summaries, caching, profile statistics |
| Gemini protocol | `test_gemini_protocol.py` | the real `google-genai` SDK against a local Gemini-protocol stub: request shape, thinking config, retry/backoff, 429/403/404 mapping, circuit breaker, explicit fallback model, health check, embedding batching/dimension/normalisation/task type, OCR image parts |
| RAG evaluation | `tests/eval` | see EVALUATION.md |
| UI | `frontend/e2e/smoke.mjs` | onboarding, upload with live progress, extraction report, cited answer, citation → source viewer, honest refusal, tools, insights/privacy, mobile layout, no console errors |

## What is NOT verified (needs credentials / network not available during development)
* **Live Gemini** behaviour: answer quality, JSON-mode reliability, real embedding similarity ranges (the cosine floor/ceiling and `MIN_RELEVANCE` are conservative defaults — calibrate with `scripts/eval_live.py`), quota behaviour, whether `gemini-2.5-flash` is still served for your key.
* **Live YouTube / Supadata / yt-dlp**, **live websites**, real-world bot protection, Hugging Face / Vercel deployment.
* Tesseract OCR (binary not installed here; the OCR plumbing is tested with doubles and the Gemini OCR request shape with the stub).
* Docker images (`backend/Dockerfile`, `frontend/Dockerfile`, `docker-compose.yml`) and the GitHub Actions workflow were written but **not built/run** (no Docker or CI runner available).
* Load tests: no multi-user soak test was run; concurrency is covered by the job queue tests only.

## Test doubles (all clearly named, never auto-selected)
`HashEmbedder` (lexical hashing), `FakeLLM` + an *extractive reader* in `tests/conftest.py` (copies the best-matching sentences with `[S#]` citations), `FakeOCR`, scripted DNS resolvers, `httpx.MockTransport`, a local fixture web server, and a Gemini protocol stub.
