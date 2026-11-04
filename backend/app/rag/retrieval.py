"""Hybrid retrieval.

dense (cosine over stored embeddings)  +  keyword (SQLite FTS5 / BM25, porter stemmed)
   -> reciprocal-rank fusion across all query variants
   -> deterministic rerank (dense relevance + query-term coverage + exact number/phrase hits)
   -> MMR de-duplication, per-source balancing for multi-source questions
   -> small-to-big neighbour expansion for tables / transcript windows

Isolation: every SQL statement is constrained by `workspace_id`; the embedding cache is keyed by
(source_id, updated_at) and only ever filled from workspace-scoped queries.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from ..config import Settings
from ..db import Database
from ..ingest.textutil import content_words
from ..providers.embeddings import BaseEmbedder

log = logging.getLogger("talkdox.retrieval")

RRF_K = 60


@dataclass
class Hints:
    page: Optional[int] = None
    time_s: Optional[float] = None


@dataclass
class Hit:
    chunk: dict
    dense: float = 0.0
    bm25_rank: Optional[int] = None
    dense_rank: Optional[int] = None
    rrf: float = 0.0
    coverage: float = 0.0
    relevance: float = 0.0
    score: float = 0.0
    context_only: bool = False      # added by neighbour expansion

    @property
    def id(self) -> int:
        return self.chunk["id"]


@dataclass
class RetrievalResult:
    hits: list[Hit]
    candidates: int
    best_relevance: float
    per_source: dict[str, int]
    queries: list[str]
    timing_ms: dict = field(default_factory=dict)


class _MatrixCache:
    """LRU cache of per-source embedding matrices bounded by bytes."""

    def __init__(self, max_bytes: int = 256 * 1024 * 1024):
        self.max = max_bytes
        self.d: OrderedDict[tuple, tuple[np.ndarray, np.ndarray]] = OrderedDict()
        self.bytes = 0
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            v = self.d.get(key)
            if v is not None:
                self.d.move_to_end(key)
            return v

    def put(self, key, ids, mat):
        with self.lock:
            self.d[key] = (ids, mat)
            self.bytes += mat.nbytes
            while self.bytes > self.max and len(self.d) > 1:
                _, (_, m) = self.d.popitem(last=False)
                self.bytes -= m.nbytes


_cache = _MatrixCache()


class Retriever:
    def __init__(self, db: Database, embedder: BaseEmbedder, s: Settings):
        self.db, self.emb, self.s = db, embedder, s
        self._qcache: OrderedDict[str, np.ndarray] = OrderedDict()

    # ---------------------------------------------------------------- helpers
    def _embed_query(self, q: str) -> np.ndarray:
        if q in self._qcache:
            return self._qcache[q]
        v = self.emb.embed_query(q)
        self._qcache[q] = v
        if len(self._qcache) > 512:
            self._qcache.popitem(last=False)
        return v

    def _matrix(self, wid: str, src: dict) -> tuple[np.ndarray, np.ndarray]:
        key = (wid, src["id"], src["updated_at"], self.emb.fingerprint)
        hit = _cache.get(key)
        if hit is not None:
            return hit
        rows = self.db.all("SELECT id, embedding FROM chunks WHERE workspace_id=? AND source_id=? ORDER BY ord",
                           (wid, src["id"]))
        if not rows:
            ids, mat = np.zeros(0, dtype=np.int64), np.zeros((0, self.emb.dim), dtype=np.float32)
        else:
            ids = np.array([r["id"] for r in rows], dtype=np.int64)
            mat = np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
        _cache.put(key, ids, mat)
        return ids, mat

    def _fts(self, wid: str, source_ids: list[str], query: str, limit: int) -> list[tuple[int, float]]:
        toks = [t for t in re.findall(r"[^\W_]+", query.lower()) if len(t) > 1]
        toks = [t for t in dict.fromkeys(toks) if t not in _STOP]
        if not toks:
            return []
        match = " OR ".join(f'"{t}"' for t in toks[:24])
        q = ",".join("?" * len(source_ids))
        try:
            rows = self.db.all(
                f"SELECT rowid, bm25(chunks_fts) AS sc FROM chunks_fts WHERE chunks_fts MATCH ? "
                f"AND workspace_id=? AND source_id IN ({q}) ORDER BY sc LIMIT ?", (match, wid, *source_ids, limit))
        except Exception as e:
            log.debug("fts query failed: %s", e)
            return []
        return [(r["rowid"], r["sc"]) for r in rows]

    def term_weights(self, wid: str, source_ids: list[str], queries: list[str]) -> dict[str, float]:
        """IDF per query stem over the selected sources (document frequency from the FTS index). Rare, discriminative
        words ("payload") then dominate coverage over words every passage shares ("robot arm"); words that occur
        nowhere keep the maximum weight, which correctly drags the relevance of every candidate down."""
        q = ",".join("?" * len(source_ids))
        n = self.db.one(f"SELECT COUNT(*) c FROM chunks WHERE workspace_id=? AND source_id IN ({q})", (wid, *source_ids))["c"] or 1
        out: dict[str, float] = {}
        for text in queries:
            for tok in dict.fromkeys(re.findall(r"[^\W_]+", text.lower())):
                if tok in _STOP or len(tok) < 2:
                    continue
                stem = tok[:5] if len(tok) > 5 else tok
                try:
                    df = self.db.one(f"SELECT COUNT(*) c FROM chunks_fts WHERE chunks_fts MATCH ? AND workspace_id=? "
                                     f"AND source_id IN ({q})", (f'"{tok}"', wid, *source_ids))["c"]
                except Exception:
                    df = 0
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                out[stem] = max(out.get(stem, 0.0), idf)
        return out

    def missing_terms(self, wid: str, source_ids: list[str], terms: list[str]) -> list[str]:
        """Terms (or phrases such as '3.0' / 'ef_construction') that occur in NONE of the given sources."""
        if not terms or not source_ids:
            return []
        q = ",".join("?" * len(source_ids))
        out = []
        for t in terms:
            toks = re.findall(r"[^\W_]+", t.lower())
            if not toks:
                continue
            phrase = '"' + " ".join(toks) + '"'
            try:
                row = self.db.one(f"SELECT 1 FROM chunks_fts WHERE chunks_fts MATCH ? AND workspace_id=? "
                                  f"AND source_id IN ({q}) LIMIT 1", (phrase, wid, *source_ids))
            except Exception:
                continue
            if row is None:
                out.append(t)
        return out

    # ---------------------------------------------------------------- main API
    def search(self, wid: str, source_ids: list[str], queries: list[str], k: int | None = None,
               hints: Hints | None = None, balance: bool = False, mode: str = "hybrid",
               rerank: bool = True) -> RetrievalResult:
        """`mode` (hybrid|dense|bm25) and `rerank` exist for the ablation study in tests/eval; production uses defaults."""
        t0 = time.time()
        k = k or self.s.retrieval_top_k
        N = self.s.retrieval_candidates
        hints = hints or Hints()
        sources = {sid: self.db.get_source(wid, sid) for sid in source_ids}
        sources = {sid: s for sid, s in sources.items()
                   if s and s["status"] in ("completed", "partial") and s["n_chunks"] > 0}
        if not sources:
            return RetrievalResult([], 0, 0.0, {}, queries)
        stale = [s["name"] for s in sources.values()
                 if (s["diagnostics"].get("embedder") or self.emb.fingerprint) != self.emb.fingerprint]
        if stale:
            log.warning("sources indexed with a different embedding model: %s", stale)
        sids = list(sources)

        dense: dict[int, float] = {}
        dense_rank: dict[int, int] = {}
        bm_rank: dict[int, int] = {}
        rrf: dict[int, float] = {}
        t_emb = time.time()
        qvecs = [self._embed_query(q) for q in queries]
        t_emb = time.time() - t_emb
        mats = [(sid, *self._matrix(wid, s)) for sid, s in sources.items()]
        all_ids = np.concatenate([m[1] for m in mats]) if mats else np.zeros(0, dtype=np.int64)
        all_mat = np.vstack([m[2] for m in mats if len(m[1])]) if any(len(m[1]) for m in mats) else None
        for q, qv in zip(queries, qvecs):
            if all_mat is not None and mode in ("hybrid", "dense"):
                sims = all_mat @ qv
                top = np.argsort(-sims)[:N]
                for r, idx in enumerate(top):
                    cid = int(all_ids[idx])
                    dense[cid] = max(dense.get(cid, -1.0), float(sims[idx]))
                    dense_rank[cid] = min(dense_rank.get(cid, 10**9), r)
                    rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (RRF_K + r)
            for r, (cid, _) in enumerate(self._fts(wid, sids, q, N) if mode in ("hybrid", "bm25") else []):
                bm_rank[cid] = min(bm_rank.get(cid, 10**9), r)
                rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (RRF_K + r)
        # a chunk found by BOTH retrievers must have a dense score for relevance; compute it directly
        missing = [cid for cid in rrf if cid not in dense]
        if missing and all_mat is not None:
            pos = {int(c): i for i, c in enumerate(all_ids)}
            for cid in missing:
                if cid in pos:
                    dense[cid] = max(float(all_mat[pos[cid]] @ qv) for qv in qvecs)

        cand_ids = sorted(rrf, key=lambda c: -rrf[c])[: N * 2]
        chunks = {c["id"]: c for c in self.db.chunk_rows(wid, cand_ids)}
        weights = self.term_weights(wid, sids, queries)
        terms_sets = [set(_stems(q)) for q in queries]
        nums = set(n for q in queries for n in re.findall(r"\d[\d,.]*\d|\d", q))
        top_rrf = max(rrf.values()) if rrf else 1.0

        hits: list[Hit] = []
        for cid in cand_ids:
            ch = chunks.get(cid)
            if ch is None:
                continue
            if hints.page is not None and ch["kind"] != "transcript":
                if ch["page"] is not None and not (ch["page"] <= hints.page <= (ch["page_end"] or ch["page"])):
                    continue
            if hints.time_s is not None and ch["kind"] == "transcript" and ch["t_start"] is not None:
                if not (ch["t_start"] - 60 <= hints.time_s <= (ch["t_end"] or ch["t_start"]) + 60):
                    continue
            ctext = ch["ctx_text"].lower()
            cstems = set(_stems(ctext))
            cov = max((_wcov(ts, cstems, weights)) for ts in terms_sets)
            num_hit = any(n.replace(",", "") in ctext.replace(",", "") for n in nums) if nums else False
            cos = dense.get(cid, 0.0)
            cos_n = float(np.clip((cos - self.emb.cos_floor) / (self.emb.cos_ceil - self.emb.cos_floor), 0, 1))
            rel = 0.55 * cos_n + 0.45 * cov
            if num_hit:
                rel = min(1.0, rel + 0.08)
            h = Hit(ch, dense=cos, bm25_rank=bm_rank.get(cid), dense_rank=dense_rank.get(cid), rrf=rrf[cid],
                    coverage=cov, relevance=rel)
            h.score = (0.5 * (rrf[cid] / top_rrf) + 0.5 * rel) if rerank else rrf[cid] / top_rrf
            # an explicit locator in the question ("page 5", "at 12:30") is strong evidence: rank exact hits first
            if hints.time_s is not None and ch["kind"] == "transcript" and ch["t_start"] is not None:
                t_end = ch["t_end"] or ch["t_start"]
                if ch["t_start"] <= hints.time_s <= t_end:
                    h.score += 0.6
                    h.relevance = max(h.relevance, 0.6)
                else:
                    h.score += 0.3 * max(0.0, 1 - min(abs(hints.time_s - ch["t_start"]), abs(hints.time_s - t_end)) / 60)
            if hints.page is not None and ch["page"] is not None and ch["page"] <= hints.page <= (ch["page_end"] or ch["page"]):
                h.score += 0.4
                h.relevance = max(h.relevance, 0.5)
            hits.append(h)
        hits.sort(key=lambda h: -h.score)

        # explicit page / timestamp hints: if nothing survived the hard filter, fall back unfiltered
        if not hits and (hints.page is not None or hints.time_s is not None):
            return self.search(wid, source_ids, queries, k, Hints(), balance, mode, rerank)

        selected = self._select(wid, hits, k, balance, sources) if rerank else hits[:k]
        selected = self._expand(wid, selected, sources)
        per_source: dict[str, int] = {}
        for h in selected:
            per_source[h.chunk["source_id"]] = per_source.get(h.chunk["source_id"], 0) + 1
        best = max((h.relevance for h in selected if not h.context_only), default=0.0)
        return RetrievalResult(selected, len(hits), best, per_source, queries,
                               {"embed_ms": int(t_emb * 1000), "total_ms": int((time.time() - t0) * 1000)})

    # ---------------------------------------------------------------- selection
    def _select(self, wid: str, hits: list[Hit], k: int, balance: bool, sources: dict) -> list[Hit]:
        if not hits:
            return []
        # MMR over text similarity (token Jaccard: no extra embedding fetches required)
        chosen: list[Hit] = []
        pool = hits[: max(k * 4, 24)]
        toks = {h.id: set(_stems(h.chunk["text"])) for h in pool}

        def jacc(a, b):
            u = len(a | b)
            return len(a & b) / u if u else 0.0

        def pick(candidates: list[Hit], n: int):
            cand = list(candidates)
            while cand and n > 0:
                best, best_v = None, -1.0
                for h in cand:
                    red = max((jacc(toks[h.id], toks[c.id]) for c in chosen), default=0.0)
                    v = 0.75 * h.score - 0.25 * red
                    if red > 0.85:           # near-duplicate passage (same text in two places)
                        continue
                    if v > best_v:
                        best, best_v = h, v
                if best is None:
                    break
                chosen.append(best)
                cand.remove(best)
                n -= 1

        if balance and len({h.chunk["source_id"] for h in pool}) > 1:
            by_src: dict[str, list[Hit]] = {}
            for h in pool:
                by_src.setdefault(h.chunk["source_id"], []).append(h)
            quota = max(2, k // max(len(by_src), 1))
            best = max(h.relevance for h in pool)
            for sid, lst in by_src.items():
                # guarantee each source a voice, but only with passages that are actually relevant
                good = [h for h in lst if h.relevance >= max(self.s.min_relevance, 0.5 * best)]
                pick(good, min(quota, len(good)))
        pick([h for h in pool if h not in chosen], k - len(chosen))
        chosen.sort(key=lambda h: -h.score)
        return chosen

    def _expand(self, wid: str, selected: list[Hit], sources: dict) -> list[Hit]:
        """Small-to-big: pull the adjacent chunk for the strongest hits when the unit of meaning spans
        chunks (continued tables, adjacent transcript windows)."""
        have = {h.id for h in selected}
        extra: list[Hit] = []
        budget = max(0, self.s.context_char_budget - sum(len(h.chunk["text"]) for h in selected))
        for h in selected[:3]:
            ch = h.chunk
            if ch["kind"] not in ("table", "transcript"):
                continue
            row = self.db.one(
                "SELECT id FROM chunks WHERE workspace_id=? AND source_id=? AND ord=?",
                (wid, ch["source_id"], ch["ord"] + 1))
            if not row or row["id"] in have:
                continue
            nxt = self.db.chunk_rows(wid, [row["id"]])
            if not nxt or nxt[0]["kind"] != ch["kind"] or len(nxt[0]["text"]) > budget:
                continue
            if ch["kind"] == "table" and nxt[0]["page"] != ch["page"]:
                continue
            budget -= len(nxt[0]["text"])
            have.add(row["id"])
            extra.append(Hit(nxt[0], relevance=h.relevance * 0.7, score=h.score * 0.5, context_only=True))
        return selected + extra


def _wcov(terms: set, have: set, weights: dict) -> float:
    if not terms:
        return 0.0
    tot = sum(weights.get(t, 1.0) for t in terms)
    return sum(weights.get(t, 1.0) for t in terms if t in have) / tot if tot else 0.0


_STOP = frozenset("""a an the and or but if of to in on at by for with from as is are was were be been this that these those it
its what which who whom when where why how do does did can could should would will about into over than then so such
i you he she we they them his her our your their my me us""".split())


def _stems(text: str) -> list[str]:
    """Crude stemmer (first 5 letters of words > 5 chars) good enough for coverage overlap in many languages."""
    out = []
    for w in re.findall(r"[^\W_]+", text.lower()):
        if w in _STOP or len(w) < 2:
            continue
        out.append(w[:5] if len(w) > 5 else w)
    return out
