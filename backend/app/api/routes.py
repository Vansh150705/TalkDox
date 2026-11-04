"""HTTP API. Every data route depends on `workspace()`, which verifies the signed bearer token."""
from __future__ import annotations

import hashlib
import json
import logging
import queue
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..errors import AppError
from ..ingest.documents import SUPPORTED, SUPPORTED_LABEL
from ..ingest.jsrender import js_available
from ..ingest.web import CrawlOptions
from ..rag.tools import TOOLS
from ..security import client_ip, sign_workspace, verify_token
from ..state import AppState

log = logging.getLogger("talkdox.api")


# --------------------------------------------------------------------------- request models
class WebReq(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=20)
    crawl: bool = False
    max_depth: int = Field(1, ge=0, le=5)
    max_pages: int = Field(20, ge=1, le=500)
    path_prefix: Optional[str] = Field(None, max_length=300)
    include_subdomains: bool = False
    use_sitemap: bool = True
    combine: bool = False


class YouTubeReq(BaseModel):
    url: str = Field(max_length=500)
    langs: list[str] = Field(default_factory=list, max_length=5)


class ChatReq(BaseModel):
    question: str = Field(min_length=1, max_length=20000)
    source_ids: Optional[list[str]] = Field(None, max_length=100)
    conversation_id: Optional[str] = Field(None, max_length=60)
    mode: Literal["qa", "eli5", "executive", "debate"] = "qa"
    persona: Literal["default", "lawyer", "doctor", "financial", "teacher", "journalist"] = "default"


class ToolReq(BaseModel):
    source_ids: list[str] = Field(min_length=1, max_length=100)
    options: dict = Field(default_factory=dict)


class CompareReq(BaseModel):
    source_ids: list[str] = Field(min_length=2, max_length=10)
    question: Optional[str] = Field(None, max_length=4000)
    mode: Literal["qa", "eli5", "executive", "debate"] = "qa"


# --------------------------------------------------------------------------- serializers
def public_source(src: dict) -> dict:
    m = src.get("meta", {})
    d = src.get("diagnostics", {})
    return {
        "id": src["id"], "kind": src["kind"], "name": src["name"], "uri": src.get("uri"), "status": src["status"],
        "error": src.get("error"), "error_code": src.get("error_code"), "n_chunks": src["n_chunks"],
        "n_pages": src["n_pages"], "n_chars": src["n_chars"], "created_at": src["created_at"],
        "updated_at": src["updated_at"], "warnings": d.get("warnings", []),
        "meta": {k: m.get(k) for k in ("video_id", "channel", "duration", "language", "transcript_kind", "modality",
                                       "format", "filename", "size") if m.get(k) is not None},
        "diagnostics": {k: v for k, v in d.items() if k in (
            "extractor", "format", "pages_total", "pages_with_text", "pages_ocr", "ocr_engine", "pages_without_text",
            "tables_found", "repeated_lines_removed", "sheets", "pages", "pages_ok", "pages_failed", "provider",
            "attempts", "language", "transcript_kind", "available_languages", "segments", "chapters", "duration_s",
            "covered_s", "chunks", "chunk_kinds", "timings", "suspicious_chunks", "embeddings_reused",
            "changed_since_last_index", "failure", "images_ignored", "slides", "crawl")},
    }


def public_job(j: dict) -> dict:
    return {k: j.get(k) for k in ("id", "source_id", "kind", "status", "stage", "progress", "message", "error",
                                  "error_code", "created_at", "updated_at")}


def create_router(st: AppState) -> APIRouter:
    r = APIRouter(prefix="/api")
    s, db = st.settings, st.db

    # ------------------------------------------------------------------ auth
    def workspace(request: Request) -> str:
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        wid = verify_token(s.secret_key, token)
        if not wid or not db.workspace_exists(wid):
            raise AppError("Your session is missing or has expired. A new workspace will be created.",
                           code="invalid_token", status=401)
        db.touch_workspace(wid)
        return wid

    def limit(key: str, n: int, window: float):
        st.limiter.check(key, n, window)

    # ------------------------------------------------------------------ meta / health
    @r.get("/health")
    def health():
        return {"status": "ok"}

    @r.get("/health/ready")
    def ready():
        checks = {"database": True}
        try:
            db.one("SELECT 1")
        except Exception:
            checks["database"] = False
        llm = st.llm.health() if st.llm else {"ok": False, "detail": "not configured"}
        emb = {"ok": st.embedder is not None, "provider": st.embedder.name if st.embedder else None,
               "model": st.embedder.model if st.embedder else None}
        ok = checks["database"] and llm.get("ok") and emb["ok"]
        return {"status": "ready" if ok else "degraded", "database": checks["database"], "llm": llm, "embeddings": emb}

    @r.get("/config")
    def config():
        problems = []
        if st.llm is None:
            problems.append("No AI model is configured: set GEMINI_API_KEY (or GROQ_API_KEY). Chat and AI tools are disabled.")
        if st.embedder is None:
            problems.append("No embedding provider is configured: set GEMINI_API_KEY. Adding sources is disabled.")
        return {
            "formats": sorted(SUPPORTED.keys()), "formats_label": SUPPORTED_LABEL, "tools": TOOLS,
            "limits": s.public_dict(),
            "features": {"js_render": s.enable_js_render and js_available(), "ocr": st.ocr.name if st.ocr else None,
                         "playlists": True, "gemini_video_fallback": s.enable_gemini_video_fallback and bool(s.gemini_api_key),
                         "supadata": bool(s.supadata_api_key)},
            "model": {"provider": st.llm.provider if st.llm else None, "name": st.llm.model if st.llm else None,
                      "embedding": st.embedder.model if st.embedder else None},
            "problems": problems,
            "demo": bool(st.llm and st.llm.provider == "fake"),
            "privacy": {"sent_to": _providers(st), "retention_days": s.retention_days},
        }

    # ------------------------------------------------------------------ workspaces
    @r.post("/workspaces")
    def create_workspace(request: Request):
        limit("ws:" + client_ip(request, s.trust_proxy_headers, s.trusted_proxy_hops), s.rate_workspace_create_per_hour, 3600)
        wid = db.create_workspace()
        return {"workspace_id": wid, "token": sign_workspace(s.secret_key, wid)}

    @r.get("/workspace")
    def workspace_info(wid: str = Depends(workspace)):
        return {"workspace_id": wid, **db.stats(wid), "retention_days": s.retention_days}

    @r.delete("/workspace")
    def delete_workspace(wid: str = Depends(workspace)):
        db.delete_workspace(wid)
        return {"deleted": True}

    # ------------------------------------------------------------------ sources
    @r.post("/sources/files")
    def upload_files(files: list[UploadFile] = File(...), wid: str = Depends(workspace)):
        if len(files) > 10:
            raise AppError("Upload at most 10 files at a time.", code="too_many_files")
        limit("ing:" + wid, s.rate_ingest_per_hour, 3600)
        tmpdir = s.data_dir / "tmp"
        tmpdir.mkdir(parents=True, exist_ok=True)
        results, max_bytes = [], s.max_upload_mb * 1024 * 1024
        for f in files:
            name = f.filename or "upload"
            h, size = hashlib.sha256(), 0
            suffix = Path(name).suffix.lower()
            suffix = suffix if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix) else ""
            fd = tempfile.NamedTemporaryFile(dir=tmpdir, delete=False, prefix="up_", suffix=suffix)
            path = Path(fd.name)
            try:
                with fd:
                    while True:
                        chunk = f.file.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > max_bytes:
                            raise AppError(f"'{name}' is larger than the {s.max_upload_mb} MB limit.",
                                           code="file_too_large", status=413)
                        h.update(chunk)
                        fd.write(chunk)
                if size == 0:
                    raise AppError(f"'{name}' is empty.", code="empty")
                res = st.ingest.submit_file(wid, path, name, h.hexdigest())
                results.append({"filename": name, **res})
            except AppError as e:
                path.unlink(missing_ok=True)
                results.append({"filename": name, "error": e.message, "code": e.code, "status": e.status})
            except Exception:
                path.unlink(missing_ok=True)
                raise
        if all("error" in x for x in results) and len(results) == 1:
            e = results[0]
            raise AppError(e["error"], code=e["code"], status=e.get("status", 400))
        return {"results": results}

    @r.post("/sources/web")
    def add_web(body: WebReq, wid: str = Depends(workspace)):
        limit("ing:" + wid, s.rate_ingest_per_hour, 3600)
        opts = CrawlOptions(crawl=body.crawl, max_depth=body.max_depth, max_pages=body.max_pages,
                            path_prefix=body.path_prefix, include_subdomains=body.include_subdomains,
                            use_sitemap=body.use_sitemap)
        return {"results": st.ingest.submit_web(wid, body.urls, opts, body.combine)}

    @r.post("/sources/youtube")
    def add_youtube(body: YouTubeReq, wid: str = Depends(workspace)):
        limit("ing:" + wid, s.rate_ingest_per_hour, 3600)
        return {"results": st.ingest.submit_youtube(wid, body.url, [l for l in body.langs if re.fullmatch(r"[A-Za-z-]{2,10}", l)])}

    def with_job(src: dict, wid: str) -> dict:
        out = public_source(src)
        if src["status"] in ("queued", "processing"):
            j = db.one("SELECT * FROM jobs WHERE source_id=? AND workspace_id=? ORDER BY created_at DESC LIMIT 1", (src["id"], wid))
            if j:
                out["job"] = public_job(dict(j))
        return out

    @r.get("/sources")
    def list_sources(wid: str = Depends(workspace)):
        return {"sources": [with_job(x, wid) for x in db.list_sources(wid)]}

    @r.get("/sources/{sid}")
    def get_source(sid: str, wid: str = Depends(workspace)):
        src = db.get_source(wid, sid)
        if not src:
            raise AppError("Source not found.", code="not_found", status=404)
        return with_job(src, wid)

    @r.delete("/sources/{sid}")
    def delete_source(sid: str, wid: str = Depends(workspace)):
        src = db.get_source(wid, sid)
        if not src:
            raise AppError("Source not found.", code="not_found", status=404)
        if src["status"] in ("queued", "processing"):
            db.run("UPDATE jobs SET cancel_requested=1 WHERE source_id=? AND workspace_id=?", (sid, wid))
            time.sleep(0.3)
        db.delete_source(wid, sid)
        return {"deleted": True}

    @r.post("/sources/{sid}/refresh")
    def refresh_source(sid: str, wid: str = Depends(workspace)):
        limit("ing:" + wid, s.rate_ingest_per_hour, 3600)
        return st.ingest.submit_reindex(wid, sid)

    @r.get("/sources/{sid}/content")
    def source_content(sid: str, page: Optional[int] = None, chunk_id: Optional[int] = None, offset: int = 0,
                       limit_: int = 20, wid: str = Depends(workspace)):
        """Extracted text exactly as indexed (what the AI sees) - powers the source viewer and citation checks."""
        from ..rag.citations import location_label
        src = db.get_source(wid, sid)
        if not src:
            raise AppError("Source not found.", code="not_found", status=404)
        chunks = db.source_chunks(wid, sid)
        if chunk_id is not None:
            idx = next((i for i, c in enumerate(chunks) if c["id"] == chunk_id), None)
            if idx is None:
                raise AppError("Passage not found.", code="not_found", status=404)
            sel = chunks[max(0, idx - 1): idx + 2]
        elif page is not None:
            sel = [c for c in chunks if c["page"] == page or (c["page"] and c.get("page_end") and c["page"] <= page <= c["page_end"])]
        else:
            sel = chunks[offset: offset + max(1, min(limit_, 100))]
        return {"source": public_source(src), "total": len(chunks), "offset": offset,
                "chunks": [{"id": c["id"], "ord": c["ord"], "kind": c["kind"], "text": c["text"], "page": c["page"],
                            "heading": c["heading"], "url": c["url"], "t_start": c["t_start"], "t_end": c["t_end"],
                            "label": location_label(c), "suspicious": bool(c["meta"].get("suspicious"))} for c in sel]}

    # ------------------------------------------------------------------ jobs
    @r.get("/jobs/{jid}")
    def get_job(jid: str, wid: str = Depends(workspace)):
        j = db.get_job(wid, jid)
        if not j:
            raise AppError("Job not found.", code="not_found", status=404)
        return public_job(j)

    @r.post("/jobs/{jid}/cancel")
    def cancel_job(jid: str, wid: str = Depends(workspace)):
        j = db.get_job(wid, jid)
        if not j:
            raise AppError("Job not found.", code="not_found", status=404)
        db.run("UPDATE jobs SET cancel_requested=1 WHERE id=? AND workspace_id=?", (jid, wid))
        return {"cancelling": True}

    # ------------------------------------------------------------------ chat
    @r.post("/chat")
    def chat(body: ChatReq, wid: str = Depends(workspace)):
        limit("chat:" + wid, s.rate_chat_per_min, 60)
        return st.answers.ask(wid, body.question, body.source_ids, body.conversation_id, body.mode, body.persona)

    @r.post("/chat/stream")
    def chat_stream(body: ChatReq, wid: str = Depends(workspace)):
        limit("chat:" + wid, s.rate_chat_per_min, 60)
        q: queue.Queue = queue.Queue()
        cancel = threading.Event()

        def work():
            try:
                payload = st.answers.ask(wid, body.question, body.source_ids, body.conversation_id, body.mode,
                                         body.persona, on_stage=lambda k, m: q.put(("stage", {"stage": k, "message": m})),
                                         cancelled=cancel.is_set)
                q.put(("final", payload))
            except AppError as e:
                q.put(("error", e.to_dict()))
            except Exception:
                log.exception("chat stream failed")
                q.put(("error", {"error": "Something went wrong while answering.", "code": "internal_error"}))
            finally:
                q.put(("end", {}))

        threading.Thread(target=work, daemon=True).start()

        def gen():
            try:
                while True:
                    try:
                        name, data = q.get(timeout=15)
                    except queue.Empty:
                        yield ": keepalive\n\n"
                        continue
                    yield f"event: {name}\ndata: {json.dumps(data, default=str)}\n\n"
                    if name == "end":
                        return
            finally:
                cancel.set()      # client went away -> stop between steps

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------ conversations
    @r.get("/conversations")
    def conversations(wid: str = Depends(workspace)):
        rows = db.all("SELECT id, title, created_at, updated_at FROM conversations WHERE workspace_id=? "
                      "ORDER BY updated_at DESC LIMIT 100", (wid,))
        return {"conversations": [dict(x) for x in rows]}

    @r.get("/conversations/{cid}")
    def conversation(cid: str, wid: str = Depends(workspace)):
        c = db.one("SELECT id, title, created_at, updated_at FROM conversations WHERE id=? AND workspace_id=?", (cid, wid))
        if not c:
            raise AppError("Conversation not found.", code="not_found", status=404)
        msgs = db.all("SELECT id, role, content, payload, created_at FROM messages WHERE conversation_id=? AND workspace_id=? "
                      "ORDER BY created_at", (cid, wid))
        out = []
        for m in msgs:
            p = json.loads(m["payload"] or "{}")
            out.append({"id": m["id"], "role": m["role"], "content": m["content"], "created_at": m["created_at"],
                        **({k: p.get(k) for k in ("citations", "flags", "confidence", "followups", "status", "related",
                                                  "answerable", "scope_note", "sources_used", "model", "intent", "note")}
                           if m["role"] == "assistant" else {})})
        return {**dict(c), "messages": out}

    @r.delete("/conversations/{cid}")
    def delete_conversation(cid: str, wid: str = Depends(workspace)):
        if not db.one("SELECT 1 FROM conversations WHERE id=? AND workspace_id=?", (cid, wid)):
            raise AppError("Conversation not found.", code="not_found", status=404)
        with db.tx() as c:
            c.execute("DELETE FROM messages WHERE conversation_id=? AND workspace_id=?", (cid, wid))
            c.execute("DELETE FROM conversations WHERE id=? AND workspace_id=?", (cid, wid))
        return {"deleted": True}

    @r.get("/conversations/{cid}/export", response_class=PlainTextResponse)
    def export(cid: str, wid: str = Depends(workspace)):
        data = conversation(cid, wid)
        lines = [f"# {data['title']}", f"_Exported from TalkDox · {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}_", ""]
        for m in data["messages"]:
            if m["role"] == "user":
                lines += [f"**Q:** {m['content']}", ""]
            else:
                lines += [m["content"], ""]
                for c in m.get("citations") or []:
                    link = f" - {c['deep_link']}" if c.get("deep_link") else ""
                    lines.append(f"- [{c['id']}] {c['source_name']} · {c['label']}{link}")
                lines.append("")
        return PlainTextResponse("\n".join(lines), headers={"Content-Disposition": 'attachment; filename="talkdox-chat.md"'})

    # ------------------------------------------------------------------ tools
    @r.post("/tools/{tool}")
    def run_tool(tool: str, body: ToolReq, wid: str = Depends(workspace)):
        if tool not in TOOLS:
            raise AppError("Unknown tool.", code="unknown_tool", status=404)
        limit("tools:" + wid, s.rate_tools_per_hour, 3600)
        return st.tools.run(wid, tool, body.source_ids, body.options)

    @r.post("/compare")
    def compare(body: CompareReq, wid: str = Depends(workspace)):
        limit("chat:" + wid, s.rate_chat_per_min, 60)
        if body.question:
            return st.answers.ask(wid, body.question, body.source_ids, None, body.mode, "default")
        limit("tools:" + wid, s.rate_tools_per_hour, 3600)
        return st.tools.compare_overview(wid, body.source_ids)

    # ------------------------------------------------------------------ analytics
    @r.get("/analytics")
    def analytics(wid: str = Depends(workspace)):
        rows = db.all("SELECT role, content, payload FROM messages WHERE workspace_id=? ORDER BY created_at", (wid,))
        q = [x for x in rows if x["role"] == "user"]
        a = [json.loads(x["payload"] or "{}") for x in rows if x["role"] == "assistant"]
        scores = [p["confidence"]["score"] for p in a if p.get("confidence") and p["confidence"].get("level") not in (None, "none")]
        stat = {}
        for p in a:
            stat[p.get("status", "answered")] = stat.get(p.get("status", "answered"), 0) + 1
        lat = [sum(t.get("ms", 0) for t in p.get("trace", [])) for p in a if p.get("trace")]
        words: dict[str, int] = {}
        from ..ingest.textutil import content_words
        for x in q:
            for w in content_words(x["content"]):
                words[w] = words.get(w, 0) + 1
        levels = {}
        for p in a:
            lv = (p.get("confidence") or {}).get("level")
            if lv:
                levels[lv] = levels.get(lv, 0) + 1
        return {"questions": len(q), "avg_confidence": round(sum(scores) / len(scores)) if scores else None,
                "confidence_levels": levels, "outcomes": stat, "avg_latency_ms": int(sum(lat) / len(lat)) if lat else None,
                "flagged_answers": sum(1 for p in a if p.get("flags")), "scores": scores[-50:],
                "top_terms": sorted(words.items(), key=lambda kv: -kv[1])[:8]}

    return r


def _providers(st: AppState) -> list[dict]:
    out = []
    if st.llm and st.llm.provider != "fake":
        out.append({"provider": st.llm.provider, "purpose": "answer generation, summaries and study tools",
                    "data": "the passages retrieved for each question, your question and recent chat turns"})
    if st.embedder and st.embedder.name != "hash":
        out.append({"provider": st.embedder.name, "purpose": "semantic search embeddings",
                    "data": "the text of every indexed passage (and your questions)"})
    if st.ocr and st.ocr.name == "gemini":
        out.append({"provider": "gemini", "purpose": "OCR of scanned PDF pages", "data": "images of pages without a text layer"})
    if st.settings.supadata_api_key:
        out.append({"provider": "supadata", "purpose": "YouTube transcript fallback", "data": "the video id"})
    return out
