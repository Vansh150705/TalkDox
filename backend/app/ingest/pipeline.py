"""Ingestion orchestration: Upload -> Validation -> Extraction -> Normalisation -> Chunking -> Metadata
-> Embedding -> Indexing, as a background job with persisted progress, cancellation and diagnostics.

States: queued -> processing -> completed | partial | failed | cancelled.
`partial` = searchable, but some content could not be extracted (reported in diagnostics/warnings).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from ..config import Settings
from ..db import Database
from ..errors import AppError, Cancelled, IngestError, RateLimited, NotConfigured
from ..net import SafeFetcher, normalize_url
from ..providers.embeddings import BaseEmbedder, cache_key
from ..providers.ocr import BaseOCR
from ..security import looks_like_injection
from .chunking import chunk_extracted
from .documents import extract_file
from .models import Extracted
from .web import CrawlOptions, ingest_web
from .youtube import YTRef, build_providers, expand_playlist, ingest_youtube, parse_youtube_url

log = logging.getLogger("talkdox.ingest")

MAX_ACTIVE_JOBS_PER_WORKSPACE = 6


def safe_filename(name: str) -> str:
    name = os.path.basename((name or "upload").replace("\\", "/"))
    name = re.sub(r"[\x00-\x1f\x7f<>:\"|?*]", "", name).strip(". ") or "upload"
    return name[:120]


class IngestService:
    def __init__(self, s: Settings, db: Database, embedder: Optional[BaseEmbedder], ocr: Optional[BaseOCR],
                 fetcher: SafeFetcher, yt_providers: list | None = None, yt_meta_fn: Callable | None = None,
                 on_change: Callable[[str], None] | None = None):
        self.s, self.db, self.embedder, self.ocr, self.fetcher = s, db, embedder, ocr, fetcher
        self.yt_providers, self.yt_meta_fn = yt_providers, yt_meta_fn
        self.pool = ThreadPoolExecutor(max_workers=max(1, s.ingest_workers), thread_name_prefix="ingest")
        self.on_change = on_change or (lambda sid: None)
        self._stopping = False

    # ------------------------------------------------------------------ submission
    def _guard(self, wid: str) -> None:
        if self.embedder is None:
            raise NotConfigured("No embedding provider is configured on this server (set GEMINI_API_KEY).")
        n = self.db.one("SELECT COUNT(*) c FROM sources WHERE workspace_id=?", (wid,))["c"]
        if n >= self.s.max_sources_per_workspace:
            raise AppError(f"This workspace already has {n} sources (limit {self.s.max_sources_per_workspace}). "
                           f"Delete some before adding more.", code="workspace_full", status=409)
        active = self.db.one("SELECT COUNT(*) c FROM jobs WHERE workspace_id=? AND status IN ('queued','processing')",
                             (wid,))["c"]
        if active >= MAX_ACTIVE_JOBS_PER_WORKSPACE:
            raise RateLimited("Too many sources are still processing. Please wait for them to finish.",
                              code="too_many_jobs")

    def _create(self, wid: str, kind: str, name: str, uri: str | None, content_hash: str, meta: dict):
        """Create source+job, or return the existing one for identical content (dedupe)."""
        existing = self.db.find_source_by_hash(wid, content_hash)
        if existing:
            job = self.db.one("SELECT id FROM jobs WHERE source_id=? ORDER BY created_at DESC LIMIT 1", (existing["id"],))
            return existing["id"], job["id"] if job else None, True
        sid = self.db.insert_source(wid, kind, name, uri, content_hash, meta)
        jid = self.db.create_job(wid, "ingest", sid)
        return sid, jid, False

    def submit_file(self, wid: str, path: Path, filename: str, sha256: str) -> dict:
        self._guard(wid)
        fname = safe_filename(filename)
        sid, jid, dup = self._create(wid, "document", fname, None, "file:" + sha256,
                                     {"filename": fname, "size": path.stat().st_size})
        if dup:
            path.unlink(missing_ok=True)
            return {"source_id": sid, "job_id": jid, "duplicate": True}
        self.pool.submit(self._run, wid, sid, jid, lambda cb, cc: self._extract_file(path, fname, cb, cc),
                         lambda: path.unlink(missing_ok=True))
        return {"source_id": sid, "job_id": jid, "duplicate": False}

    def submit_web(self, wid: str, urls: list[str], opts: CrawlOptions, combine: bool = False) -> list[dict]:
        self._guard(wid)
        for u in urls:
            self.fetcher.validate(u)           # fail fast, synchronously, with a clear message
        groups = [urls] if combine else [[u] for u in urls]
        out = []
        for g in groups:
            norm = [normalize_url(u) for u in g]
            key = "web:" + hashlib.sha256(("|".join(sorted(norm)) + f"|{opts.crawl}|{opts.max_depth}|{opts.max_pages}|{opts.path_prefix}").encode()).hexdigest()
            name = norm[0] if len(norm) == 1 else f"{len(norm)} web pages"
            meta = {"urls": norm, "crawl": opts.__dict__}
            sid, jid, dup = self._create(wid, "web", name, norm[0], key, meta)
            if not dup:
                self.pool.submit(self._run, wid, sid, jid,
                                 lambda cb, cc, g=g: ingest_web(g, opts, self.fetcher, self.s, cb, cc, self.ocr), None)
            out.append({"source_id": sid, "job_id": jid, "duplicate": dup})
        return out

    def submit_youtube(self, wid: str, url: str, langs: list[str] | None = None) -> list[dict]:
        self._guard(wid)
        ref: YTRef = parse_youtube_url(url)
        ids = [ref.video_id] if ref.kind == "video" else expand_playlist(ref.playlist_id, self.s)
        out = []
        for vid in ids:
            key = f"yt:{vid}:{','.join(langs or [])}"
            sid, jid, dup = self._create(wid, "youtube", f"YouTube {vid}", f"https://www.youtube.com/watch?v={vid}", key,
                                         {"video_id": vid, "langs": langs or []})
            if not dup:
                self.pool.submit(self._run, wid, sid, jid,
                                 lambda cb, cc, vid=vid: ingest_youtube(vid, self.s, self.fetcher, langs,
                                                                        self.yt_providers or build_providers(self.s, self.fetcher),
                                                                        self.yt_meta_fn, cb), None)
            out.append({"source_id": sid, "job_id": jid, "duplicate": dup, "video_id": vid})
        return out

    def submit_reindex(self, wid: str, sid: str) -> dict:
        """Re-fetch a web/YouTube source. Unchanged chunks reuse cached embeddings (incremental)."""
        src = self.db.get_source(wid, sid)
        if not src:
            raise AppError("Source not found.", code="not_found", status=404)
        if src["kind"] == "document":
            raise AppError("Uploaded files can't be refreshed - upload the new version instead.", code="not_refreshable")
        self._guard_embedder()
        jid = self.db.create_job(wid, "reindex", sid)
        self.db.update_source(sid, status="queued", error=None, error_code=None)
        if src["kind"] == "web":
            c = src["meta"].get("crawl", {})
            opts = CrawlOptions(**{k: v for k, v in c.items() if k in CrawlOptions.__dataclass_fields__})
            urls = src["meta"]["urls"]
            fn = lambda cb, cc: ingest_web(urls, opts, self.fetcher, self.s, cb, cc, self.ocr)
        else:
            vid, langs = src["meta"]["video_id"], src["meta"].get("langs") or None
            fn = lambda cb, cc: ingest_youtube(vid, self.s, self.fetcher, langs,
                                               self.yt_providers or build_providers(self.s, self.fetcher),
                                               self.yt_meta_fn, cb)
        self.pool.submit(self._run, wid, sid, jid, fn, None, True)
        return {"source_id": sid, "job_id": jid}

    def _guard_embedder(self):
        if self.embedder is None:
            raise NotConfigured("No embedding provider is configured on this server (set GEMINI_API_KEY).")

    # ------------------------------------------------------------------ execution
    def _extract_file(self, path: Path, fname: str, cb, cc) -> Extracted:
        return extract_file(path, fname, self.s, self.ocr, cb, cc)

    def _run(self, wid: str, sid: str, jid: str, extract_fn: Callable, cleanup: Callable | None,
             reindex: bool = False) -> None:
        deadline = time.time() + self.s.ingest_timeout_s
        timings: dict[str, float] = {}

        def check_cancel():
            if self._stopping:
                raise Cancelled()
            if self.db.job_cancel_requested(jid):
                raise Cancelled()
            if time.time() > deadline:
                raise IngestError("Processing took too long and was stopped.", code="timeout")

        def stage(name: str, frac: float, msg: str):
            self.db.update_job(jid, status="processing", stage=name, progress=round(frac, 3), message=msg)

        try:
            self.db.update_source(sid, status="processing")
            stage("validating", 0.02, "Validating")
            check_cancel()
            t0 = time.time()
            stage("extracting", 0.05, "Extracting content")
            ex: Extracted = extract_fn(lambda f, m: stage("extracting", 0.05 + 0.5 * min(max(f, 0), 1), m), check_cancel)
            timings["extract_s"] = round(time.time() - t0, 2)
            check_cancel()

            stage("chunking", 0.58, "Splitting into searchable passages")
            t0 = time.time()
            drafts = chunk_extracted(ex, self.s)
            if not drafts:
                raise IngestError("No searchable text was found in this source.", code="no_text")
            chunks = [d.as_dict() for d in drafts]
            suspicious = 0
            for c in chunks:
                if looks_like_injection(c["text"]):
                    c["meta"]["suspicious"] = True
                    suspicious += 1
            timings["chunk_s"] = round(time.time() - t0, 2)
            check_cancel()

            stage("embedding", 0.62, f"Embedding {len(chunks)} passages")
            t0 = time.time()
            vectors, reused = self._embed_chunks(wid, chunks, lambda f: stage("embedding", 0.62 + 0.33 * f,
                                                 f"Embedding passages ({int(f * 100)}%)"), check_cancel)
            timings["embed_s"] = round(time.time() - t0, 2)
            check_cancel()

            stage("indexing", 0.96, "Building search index")
            self.db.replace_chunks(wid, sid, chunks, vectors)

            warnings = list(ex.warnings)
            if suspicious:
                warnings.append(f"{suspicious} passage(s) contain text that looks like instructions to an AI. "
                                f"They are treated as untrusted content and cannot change TalkDox's behaviour.")
            status = "partial" if (ex.partial or any(r.get("status") == "failed" for r in ex.diagnostics.get("pages", []))) else "completed"
            diag = {**ex.diagnostics, "warnings": warnings, "chunks": len(chunks), "embeddings_reused": reused,
                    "timings": timings, "suspicious_chunks": suspicious, "embedder": self.embedder.fingerprint,
                    "chunk_kinds": _count([c["kind"] for c in chunks])}
            src = self.db.get_source(wid, sid)
            old_hash = (src or {}).get("diagnostics", {}).get("text_fingerprint")
            fp = hashlib.sha256("".join(c["text_hash"] for c in chunks).encode()).hexdigest()
            diag["text_fingerprint"] = fp
            if reindex:
                diag["changed_since_last_index"] = (old_hash != fp)
            meta = {**(src or {}).get("meta", {}), **ex.meta, "format": ex.diagnostics.get("format") or ex.diagnostics.get("extractor")}
            name = ex.title if (src or {}).get("kind") != "document" or not ex.title else (src["name"])
            self.db.update_source(sid, status=status, name=(name or src["name"])[:200], n_chunks=len(chunks),
                                  n_pages=ex.n_pages, n_chars=ex.n_chars, diagnostics=diag, meta=meta,
                                  error=None, error_code=None)
            self.db.update_job(jid, status=status, stage="done", progress=1.0,
                               message="Ready" if status == "completed" else "Ready (some content was skipped)")
            self.on_change(sid)
        except Cancelled:
            self._finish_failed(sid, jid, "cancelled", "cancelled", "Processing was cancelled.", status="cancelled")
        except IngestError as e:
            self._finish_failed(sid, jid, e.code, e.message, e.message, detail=e.detail)
        except AppError as e:
            self._finish_failed(sid, jid, e.code, e.message, e.message, detail=e.detail)
        except Exception as e:     # last-resort: never leave a job hanging in 'processing'
            log.exception("ingest crashed for %s", sid)
            self._finish_failed(sid, jid, "internal_error", "Something went wrong while processing this source.",
                                "Internal error")
        finally:
            if cleanup:
                try:
                    cleanup()
                except Exception:
                    pass

    def _finish_failed(self, sid, jid, code, message, jmsg, status="failed", detail=None):
        if status == "failed":
            log.info("ingest %s failed: %s", sid, code)
        # on a failed *re*-index keep the previous index searchable
        has_chunks = self.db.one("SELECT 1 FROM chunks WHERE source_id=? LIMIT 1", (sid,)) is not None
        diag = {"failure": {"code": code, **({"detail": detail} if detail else {})}}
        if has_chunks and status == "failed":
            self.db.update_source(sid, status="partial", error=f"Refresh failed: {message}", error_code=code)
        else:
            self.db.update_source(sid, status=status, error=message, error_code=code, diagnostics=diag)
        self.db.update_job(jid, status=status, stage=status, error=message, error_code=code, message=jmsg)

    # ------------------------------------------------------------------ embedding
    def _embed_chunks(self, wid: str, chunks: list[dict], progress: Callable[[float], None],
                      check_cancel: Callable[[], None]) -> tuple[np.ndarray, int]:
        emb = self.embedder
        keys = [cache_key(emb, c["ctx_text"]) for c in chunks]
        for c, k in zip(chunks, keys):
            c["emb_key"] = k
        cached = self.db.cache_get(wid, list(dict.fromkeys(keys)))
        todo_idx = [i for i, k in enumerate(keys) if k not in cached]
        # embed each distinct text once
        uniq: dict[str, int] = {}
        for i in todo_idx:
            uniq.setdefault(keys[i], i)
        order = list(uniq.items())
        new_vecs: dict[str, np.ndarray] = {}
        B = 64
        for start in range(0, len(order), B):
            check_cancel()
            part = order[start:start + B]
            vecs = emb.embed_documents([chunks[i]["ctx_text"][:8000] for _, i in part])
            for (k, _), v in zip(part, vecs):
                new_vecs[k] = v
            self.db.cache_put(wid, {k: new_vecs[k] for k, _ in part})
            progress(min(1.0, (start + B) / max(len(order), 1)))
        allv = {**cached, **new_vecs}
        mat = np.vstack([allv[k] for k in keys]).astype(np.float32)
        return mat, len(chunks) - len(todo_idx)

    def shutdown(self, timeout: float = 20.0):
        """Stop accepting work, ask running jobs to stop at their next checkpoint and wait for them, so the
        database is never closed underneath a worker thread."""
        self._stopping = True
        t = threading.Thread(target=lambda: self.pool.shutdown(wait=True, cancel_futures=True), daemon=True)
        t.start()
        t.join(timeout)


def _count(items: list[str]) -> dict:
    d: dict[str, int] = {}
    for i in items:
        d[i] = d.get(i, 0) + 1
    return d
