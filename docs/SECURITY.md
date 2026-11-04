# Security & privacy

## Identity and isolation
* **Anonymous workspaces.** `POST /api/workspaces` returns a random id plus an HMAC-signed token. Every data route requires it; all SQL is scoped by the verified workspace id. Tests (`tests/test_security.py`) cover: missing/forged/tampered tokens on **every** route, cross-workspace reads/deletes/chat/tools/jobs/conversations, and retrieval isolation at the SQL level.
* **There are no user accounts.** A workspace lives in one browser's `localStorage`; losing it loses access (data is deleted after `RETENTION_DAYS`). If you need login, SSO, sharing or cross-device sync, put real authentication in front (this is the main product gap for a public multi-tenant release).
* Tokens are bearer credentials (no cookies ⇒ no CSRF). Anything that achieves XSS in the frontend could read them: the UI never uses `innerHTML`; model output is rendered by a React-element markdown renderer; deployment configs ship a strict CSP.

## Outbound requests (SSRF)
All user-influenced fetches go through `app/net.py`:
* http/https only, no credentials in URLs, ports 80/443 only, internal TLDs (`.local`, `.internal`, …) blocked;
* the hostname is resolved by us and **every** address must be globally routable (loopback, RFC1918, link-local incl. `169.254.169.254`, CGNAT, ULA, multicast, reserved, IPv4-mapped / 6to4 / Teredo / NAT64 forms and decimal/hex/octal IPv4 spellings are rejected);
* the TCP connection is **pinned to the validated IP** (Host header + TLS SNI preserve the name) — DNS rebinding between check and connect is impossible; tested with a resolver that changes its answer;
* redirects are followed manually (max 5), each hop re-validated and re-pinned, https→http downgrades refused;
* response size, *decoded* size (gzip bombs), content types and wall-clock time are capped; no cookies, no proxy env vars;
* robots.txt is respected (4xx = allow, 5xx/unreachable = disallow, per RFC 9309), the crawler identifies itself honestly (`TalkDoxBot`), rate-limits per host and never tries to bypass logins, paywalls or CAPTCHAs. (The previous "rotating user agents to bypass bot detection" was removed.)
* **JavaScript rendering** (optional, `ENABLE_JS_RENDER=1`): Chromium with every sub-request intercepted and checked by the same guard, images/media/fonts blocked. *Residual risk:* the browser resolves DNS itself, so DNS rebinding could in theory race the pre-check. Run the renderer in a network-isolated container (egress allow-list) for hostile-input deployments, or leave it off.
* YouTube access uses fixed Google/YouTube hosts built from a strictly validated 11-character id (the old regex accepted any URL containing `v=`).

## Untrusted content and prompt injection
Documents, pages and transcripts are untrusted data.
* The model has **no tools**, cannot fetch URLs and cannot see other workspaces, so "ignore your instructions and exfiltrate…" has nothing to act on.
* Evidence is delimited, framed as data in the system prompt, and any `<evidence>`/`<source>` tags inside content are escaped so content cannot forge evidence blocks.
* Passages that look like instructions are flagged at ingestion (`suspicious`), surfaced to the user, and still treated as data.
* Model output is sanitised (markdown images — a common exfiltration channel — and raw HTML are stripped) and rendered without `innerHTML`.
* Citations are verified deterministically; hallucinated ids/quotes are dropped.
* *Limits:* a sufficiently manipulative document can still bias the *content* of an answer about itself. Verification bounds this to what the cited text contains; it cannot make an LLM immune to persuasion.

## Uploads
Extension **and** magic-byte checks (mislabelled files rejected), size cap enforced while streaming (no full-memory buffering), page caps, ZIP-bomb guards for Office files (entry count, total uncompressed size, ratio), password-protected/corrupted/empty/binary detection with specific errors, sanitised filenames, temp files deleted after processing, **original files are not retained** (only extracted text, vectors and chat history).
LibreOffice (legacy formats) and tesseract run as subprocesses with timeouts; run the container as non-root (the Dockerfile does).

## Abuse and cost controls
Per-workspace limits for chat, ingestion and tools, per-IP limit for workspace creation, workspace source quota, question length cap, ingest timeouts, cancellable jobs, Gemini circuit breaker. Limits are in-process (see ARCHITECTURE.md for multi-instance notes). Set provider-side quotas/budget alerts as well — an anonymous public deployment spends *your* Gemini quota.

## HTTP hardening
Strict CORS (explicit origins; `*` refused in production), no credentials, `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, API CSP `default-src 'none'`, `Cache-Control: no-store`, HSTS in production, request-body cap, generic 500s with a request id (no stack traces), API docs disabled in production. Secrets are only read from the environment; log lines pass through a redacting filter; questions/answers are not logged.

## What leaves your server
| Provider | When | Data |
|---|---|---|
| Google Gemini (generation) | every question, summary and study tool | your question, recent chat turns, the retrieved passages (or sampled passages for whole-source tools) |
| Google Gemini (embeddings) | every ingest and every question | text of each indexed passage; the question |
| Google Gemini (vision OCR) | only for PDF pages without a text layer, when tesseract is not installed | page images |
| Google Gemini (video) | only if `ENABLE_GEMINI_VIDEO_FALLBACK=1` and a video has no captions | the public video URL |
| Supadata | only if `SUPADATA_API_KEY` is set and the primary transcript source fails | the video id |
| YouTube / Google Fonts / i.ytimg.com | transcript fetch from the server; fonts and thumbnails from the browser | video id; visitor IP |
Google's Gemini API terms govern how that data is handled and they differ between free and paid tiers — check the current terms for your plan before processing confidential material. The same list is shown to users in *Insights → Privacy & data*.

## Retention and deletion
`RETENTION_DAYS` (default 30) deletes idle workspaces with everything in them; users can delete a source, a conversation or the whole workspace. Deletion removes chunks, FTS entries **and** derived embedding-cache vectors (tested). SQLite WAL files may keep freed pages until checkpoint/vacuum; back-ups are outside the application's control.

## Not done / residual risks
* No account system, SSO, audit log or per-user quotas beyond the workspace token.
* In-process rate limiting; no WAF/bot protection (CAPTCHA) on workspace creation.
* Dependency risk from scraping libraries (`yt-dlp`, `youtube-transcript-api`) — pin and update regularly; YouTube may block datacenter IPs.
* No malware scanning of uploads (files are parsed, never executed, and not retained).
* The Gemini integration was verified against a protocol-level stub, not Google's live service (see TESTING.md).
