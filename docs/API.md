# HTTP API

Base path `/api`. Interactive OpenAPI docs are served at `/api/docs` when `TALKDOX_ENV` ≠ `production`.
All routes except `health*`, `config` and `POST /workspaces` need `Authorization: Bearer <token>`.
Errors always look like `{"error": "<safe message>", "code": "<stable_code>", "detail": {…}?}`.

| Route | Purpose |
|---|---|
| `GET /health` · `GET /health/ready` | liveness · readiness (database, Gemini model check, embedder) |
| `GET /config` | public capabilities, limits, active model names, config `problems`, privacy summary |
| `POST /workspaces` | create an anonymous workspace → `{workspace_id, token}` (rate-limited per IP) |
| `GET /workspace` · `DELETE /workspace` | stats · **delete everything** (rows, FTS, vectors) |
| `POST /sources/files` | multipart `files[]` (≤10). Returns per-file `{source_id, job_id, duplicate}` or `{error, code}` |
| `POST /sources/web` | `{urls[], crawl, max_depth, max_pages, path_prefix, include_subdomains, use_sitemap, combine}` |
| `POST /sources/youtube` | `{url, langs[]}` – video or playlist |
| `GET /sources` · `GET /sources/{id}` | sources with status, `warnings`, `diagnostics`, running `job` progress |
| `DELETE /sources/{id}` · `POST /sources/{id}/refresh` | delete (incl. vectors) · re-fetch web/YouTube (unchanged passages reuse embeddings; failure keeps the old index) |
| `GET /sources/{id}/content?page=&chunk_id=&offset=&limit_=` | the extracted passages exactly as indexed (viewer / verification) |
| `GET /jobs/{id}` · `POST /jobs/{id}/cancel` | `queued → processing(stage, progress, message) → completed \| partial \| failed \| cancelled` |
| `POST /chat` · `POST /chat/stream` | `{question, source_ids?, conversation_id?, mode, persona}`; the stream sends SSE `stage` events, then `final` (same payload as `/chat`) and `end`; closing the connection cancels between steps |
| `GET/DELETE /conversations[/{id}]` · `GET /conversations/{id}/export` | history, Markdown export with citations |
| `POST /tools/{summary\|dna\|flashcards\|quiz\|timeline\|actions\|contradictions\|email}` | `{source_ids, options}` – every item carries a verified citation; `coverage` says how much of the source was read |
| `POST /compare` | `{source_ids (≥2), question?}` – overview or a cross-source question |
| `GET /analytics` | per-workspace answer statistics |

## Chat response
```jsonc
{
  "answer": "… [S1] … [S2]",            // markers renumbered by first use
  "status": "answered | partial | no_evidence | unverified | clarify | meta",
  "answerable": true,
  "citations": [{
    "id": "S1", "source_id": "…", "source_name": "handbook.pdf", "source_kind": "document|web|youtube",
    "label": "p. 5 | Slide 3 | Sheet 'Revenue', rows 1-6 | 12:30–13:45",
    "page": 5, "url": null, "t_start": null, "t_end": null,
    "deep_link": "https://www.youtube.com/watch?v=…&t=750s | https://site/page#:~:text=…",
    "snippet": "…", "quote": "…", "quote_verified": true, "highlight": [120, 188], "suspicious": false
  }],
  "flags": [{"type": "uncited_claim|unsupported_claim|numeric_mismatch|weak_support|quote_unverified|invalid_citation_removed", "sentence": "…"}],
  "confidence": {"level": "high|medium|low|none", "score": 0-100, "factors": {…}},   // evidence strength, not a probability
  "followups": ["…"], "related": [/* closest passages when nothing could be verified */],
  "scope_note": "Scoped to the source(s) named in your question.",
  "model": {"provider": "gemini", "name": "gemini-3.5-flash"},   // the model that actually answered
  "trace": [{"step": "route|rewrite|retrieve|term_check|generate|verify", "ms": 12, …}],
  "message_id": "…", "conversation_id": "…"
}
```
