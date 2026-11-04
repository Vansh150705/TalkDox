"""SQLite persistence (WAL mode) with FTS5 keyword index and versioned migrations.

Every row that holds user content carries `workspace_id`; every query helper takes the
workspace id as a mandatory argument so cross-workspace reads are structurally hard.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

import numpy as np

MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE workspaces (
        id TEXT PRIMARY KEY,
        created_at REAL NOT NULL,
        last_seen REAL NOT NULL
    );
    CREATE TABLE sources (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,                 -- document | web | youtube
        name TEXT NOT NULL,
        uri TEXT,
        content_hash TEXT,
        status TEXT NOT NULL,               -- queued|processing|completed|partial|failed|cancelled
        error TEXT,
        error_code TEXT,
        diagnostics TEXT NOT NULL DEFAULT '{}',
        meta TEXT NOT NULL DEFAULT '{}',
        n_chunks INTEGER NOT NULL DEFAULT 0,
        n_pages INTEGER NOT NULL DEFAULT 0,
        n_chars INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE INDEX idx_sources_ws ON sources(workspace_id, created_at);
    CREATE UNIQUE INDEX idx_sources_dedupe ON sources(workspace_id, content_hash)
        WHERE content_hash IS NOT NULL AND status IN ('completed','partial','queued','processing');
    CREATE TABLE chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
        workspace_id TEXT NOT NULL,
        ord INTEGER NOT NULL,
        kind TEXT NOT NULL,                 -- text | table | code | transcript
        text TEXT NOT NULL,
        ctx_text TEXT NOT NULL,             -- text actually embedded/indexed (title + heading path + text)
        page INTEGER,
        page_end INTEGER,
        heading TEXT,
        section TEXT,                       -- stable section key (parent id for small-to-big)
        url TEXT,
        t_start REAL,
        t_end REAL,
        text_hash TEXT NOT NULL,
        emb_key TEXT,                       -- embedding_cache key (for deletion of derived vectors)
        meta TEXT NOT NULL DEFAULT '{}',
        embedding BLOB
    );
    CREATE INDEX idx_chunks_src ON chunks(source_id, ord);
    CREATE INDEX idx_chunks_ws ON chunks(workspace_id);
    CREATE VIRTUAL TABLE chunks_fts USING fts5(
        ctx_text, workspace_id UNINDEXED, source_id UNINDEXED,
        tokenize = 'porter unicode61 remove_diacritics 2'
    );
    CREATE TABLE embedding_cache (
        key TEXT PRIMARY KEY,               -- sha256(model|dim|text)
        workspace_id TEXT NOT NULL,
        vec BLOB NOT NULL,
        created_at REAL NOT NULL
    );
    CREATE TABLE jobs (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
        source_id TEXT,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        stage TEXT NOT NULL DEFAULT 'queued',
        progress REAL NOT NULL DEFAULT 0,
        message TEXT,
        error TEXT,
        error_code TEXT,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE INDEX idx_jobs_ws ON jobs(workspace_id, created_at);
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE INDEX idx_conv_ws ON conversations(workspace_id, updated_at);
    CREATE TABLE messages (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        workspace_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL
    );
    CREATE INDEX idx_msg_conv ON messages(conversation_id, created_at);
    CREATE TABLE source_artifacts (
        source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY (source_id, kind)
    );
    """,
]


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:20]


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._conns: list[sqlite3.Connection] = []
        self._migrate()

    # -- connections ---------------------------------------------------
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(str(self.path), timeout=30, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
            self._conns.append(c)
        return c

    def close(self) -> None:
        with self._write_lock:                 # never close a connection mid-transaction
            for c in self._conns:
                try:
                    c.close()
                except Exception:
                    pass
            self._conns.clear()
            self._local = threading.local()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Serialised write transaction (SQLite permits one writer at a time)."""
        with self._write_lock:
            c = self.conn()
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            else:
                c.execute("COMMIT")

    def _migrate(self) -> None:
        c = self.conn()
        c.execute("CREATE TABLE IF NOT EXISTS schema_version (v INTEGER NOT NULL)")
        row = c.execute("SELECT MAX(v) AS v FROM schema_version").fetchone()
        current = row["v"] or 0
        for i, script in enumerate(MIGRATIONS, start=1):
            if i <= current:
                continue
            with self.tx() as tc:
                for stmt in _split_sql(script):
                    tc.execute(stmt)
                tc.execute("INSERT INTO schema_version(v) VALUES (?)", (i,))

    # -- generic helpers -----------------------------------------------
    def one(self, sql: str, args: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        return self.conn().execute(sql, tuple(args)).fetchone()

    def all(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, tuple(args)).fetchall()

    def run(self, sql: str, args: Iterable[Any] = ()) -> None:
        with self.tx() as c:
            c.execute(sql, tuple(args))

    # -- workspaces ----------------------------------------------------
    def create_workspace(self) -> str:
        wid = new_id("ws_")
        now = time.time()
        self.run("INSERT INTO workspaces(id, created_at, last_seen) VALUES (?,?,?)", (wid, now, now))
        return wid

    def workspace_exists(self, wid: str) -> bool:
        return self.one("SELECT 1 FROM workspaces WHERE id=?", (wid,)) is not None

    def touch_workspace(self, wid: str) -> None:
        now = time.time()
        row = self.one("SELECT last_seen FROM workspaces WHERE id=?", (wid,))
        if row and now - row["last_seen"] > 300:  # avoid a write per request
            self.run("UPDATE workspaces SET last_seen=? WHERE id=?", (now, wid))

    def delete_workspace(self, wid: str) -> None:
        """Hard-delete everything (rows, FTS entries, embeddings, cache)."""
        with self.tx() as c:
            c.execute("DELETE FROM chunks_fts WHERE workspace_id=?", (wid,))
            c.execute("DELETE FROM embedding_cache WHERE workspace_id=?", (wid,))
            c.execute("DELETE FROM chunks WHERE workspace_id=?", (wid,))
            c.execute("DELETE FROM messages WHERE workspace_id=?", (wid,))
            c.execute("DELETE FROM workspaces WHERE id=?", (wid,))  # cascades the rest

    def purge_expired(self, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        cutoff = time.time() - retention_days * 86400
        rows = self.all("SELECT id FROM workspaces WHERE last_seen < ?", (cutoff,))
        for r in rows:
            self.delete_workspace(r["id"])
        return len(rows)

    # -- sources -------------------------------------------------------
    def insert_source(self, wid: str, kind: str, name: str, uri: str | None = None,
                      content_hash: str | None = None, meta: dict | None = None,
                      status: str = "queued") -> str:
        sid = new_id("src_")
        now = time.time()
        self.run(
            "INSERT INTO sources(id, workspace_id, kind, name, uri, content_hash, status, meta, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sid, wid, kind, name, uri, content_hash, status, json.dumps(meta or {}), now, now),
        )
        return sid

    def get_source(self, wid: str, sid: str) -> Optional[dict]:
        r = self.one("SELECT * FROM sources WHERE id=? AND workspace_id=?", (sid, wid))
        return source_row(r) if r else None

    def list_sources(self, wid: str) -> list[dict]:
        return [source_row(r) for r in
                self.all("SELECT * FROM sources WHERE workspace_id=? ORDER BY created_at", (wid,))]

    def find_source_by_hash(self, wid: str, content_hash: str) -> Optional[dict]:
        r = self.one(
            "SELECT * FROM sources WHERE workspace_id=? AND content_hash=? "
            "AND status IN ('completed','partial','queued','processing')", (wid, content_hash))
        return source_row(r) if r else None

    def update_source(self, sid: str, **fields: Any) -> None:
        if not fields:
            return
        for k in ("diagnostics", "meta"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k], default=str)
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.run(f"UPDATE sources SET {cols} WHERE id=?", (*fields.values(), sid))

    def delete_source(self, wid: str, sid: str) -> bool:
        if not self.get_source(wid, sid):
            return False
        with self.tx() as c:
            keys = [r["emb_key"] for r in c.execute(
                "SELECT DISTINCT emb_key FROM chunks WHERE source_id=? AND workspace_id=? AND emb_key IS NOT NULL", (sid, wid))]
            c.execute("DELETE FROM chunks_fts WHERE source_id=? AND workspace_id=?", (sid, wid))
            c.execute("DELETE FROM chunks WHERE source_id=? AND workspace_id=?", (sid, wid))
            c.execute("DELETE FROM sources WHERE id=? AND workspace_id=?", (sid, wid))
            # derived vectors are personal data too: drop cache rows no other source still uses
            for k in keys:
                if not c.execute("SELECT 1 FROM chunks WHERE workspace_id=? AND emb_key=? LIMIT 1", (wid, k)).fetchone():
                    c.execute("DELETE FROM embedding_cache WHERE workspace_id=? AND key=?", (wid, k))
        return True

    def replace_chunks(self, wid: str, sid: str, chunks: list[dict], vectors: np.ndarray) -> None:
        """Atomically swap a source's chunk set (used for first index and for re-index)."""
        with self.tx() as c:
            old_keys = [r["emb_key"] for r in c.execute(
                "SELECT DISTINCT emb_key FROM chunks WHERE source_id=? AND workspace_id=? AND emb_key IS NOT NULL", (sid, wid))]
            c.execute("DELETE FROM chunks_fts WHERE source_id=? AND workspace_id=?", (sid, wid))
            c.execute("DELETE FROM chunks WHERE source_id=? AND workspace_id=?", (sid, wid))
            for i, ch in enumerate(chunks):
                cur = c.execute(
                    "INSERT INTO chunks(source_id, workspace_id, ord, kind, text, ctx_text, page, page_end, heading,"
                    " section, url, t_start, t_end, text_hash, emb_key, meta, embedding) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, wid, i, ch["kind"], ch["text"], ch["ctx_text"], ch.get("page"), ch.get("page_end"),
                     ch.get("heading"), ch.get("section"), ch.get("url"), ch.get("t_start"), ch.get("t_end"),
                     ch["text_hash"], ch.get("emb_key"), json.dumps(ch.get("meta") or {}),
                     np.asarray(vectors[i], dtype=np.float32).tobytes()),
                )
                c.execute("INSERT INTO chunks_fts(rowid, ctx_text, workspace_id, source_id) VALUES (?,?,?,?)",
                          (cur.lastrowid, ch["ctx_text"], wid, sid))
            for k in old_keys:      # vectors of text that no longer exists anywhere in the workspace
                if not c.execute("SELECT 1 FROM chunks WHERE workspace_id=? AND emb_key=? LIMIT 1", (wid, k)).fetchone():
                    c.execute("DELETE FROM embedding_cache WHERE workspace_id=? AND key=?", (wid, k))

    # -- embedding cache (dedupe + incremental re-index) --------------
    def cache_get(self, wid: str, keys: list[str]) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        for i in range(0, len(keys), 500):
            part = keys[i:i + 500]
            q = ",".join("?" * len(part))
            for r in self.all(f"SELECT key, vec FROM embedding_cache WHERE workspace_id=? AND key IN ({q})",
                              (wid, *part)):
                out[r["key"]] = np.frombuffer(r["vec"], dtype=np.float32)
        return out

    def cache_put(self, wid: str, items: dict[str, np.ndarray]) -> None:
        now = time.time()
        with self.tx() as c:
            for k, v in items.items():
                c.execute("INSERT OR REPLACE INTO embedding_cache(key, workspace_id, vec, created_at) VALUES (?,?,?,?)",
                          (k, wid, np.asarray(v, dtype=np.float32).tobytes(), now))

    # -- jobs ----------------------------------------------------------
    def create_job(self, wid: str, kind: str, source_id: str | None) -> str:
        jid = new_id("job_")
        now = time.time()
        self.run("INSERT INTO jobs(id, workspace_id, source_id, kind, status, created_at, updated_at)"
                 " VALUES (?,?,?,?,?,?,?)", (jid, wid, source_id, kind, "queued", now, now))
        return jid

    def get_job(self, wid: str, jid: str) -> Optional[dict]:
        r = self.one("SELECT * FROM jobs WHERE id=? AND workspace_id=?", (jid, wid))
        return dict(r) if r else None

    def update_job(self, jid: str, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.run(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), jid))

    def job_cancel_requested(self, jid: str) -> bool:
        r = self.one("SELECT cancel_requested FROM jobs WHERE id=?", (jid,))
        return bool(r and r["cancel_requested"])

    def fail_orphaned_jobs(self) -> int:
        """Called at start-up: jobs left 'processing' by a previous process can never finish."""
        rows = self.all("SELECT id, source_id FROM jobs WHERE status IN ('queued','processing')")
        for r in rows:
            self.update_job(r["id"], status="failed", stage="failed", error_code="interrupted",
                            error="Processing was interrupted by a server restart. Please add the source again.")
            if r["source_id"]:
                self.update_source(r["source_id"], status="failed", error_code="interrupted",
                                   error="Processing was interrupted by a server restart. Please add the source again.")
        return len(rows)

    # -- chunks (read) -------------------------------------------------
    def chunk_rows(self, wid: str, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        rows = self.all(f"SELECT c.*, s.name AS source_name, s.kind AS source_kind, s.uri AS source_uri, "
                        f"s.meta AS source_meta FROM chunks c JOIN sources s ON s.id=c.source_id "
                        f"WHERE c.workspace_id=? AND c.id IN ({q})", (wid, *ids))
        return [chunk_row(r) for r in rows]

    def source_chunks(self, wid: str, sid: str, with_embedding: bool = False) -> list[dict]:
        rows = self.all("SELECT c.*, s.name AS source_name, s.kind AS source_kind, s.uri AS source_uri, "
                        "s.meta AS source_meta FROM chunks c JOIN sources s ON s.id=c.source_id "
                        "WHERE c.workspace_id=? AND c.source_id=? ORDER BY c.ord", (wid, sid))
        return [chunk_row(r, with_embedding) for r in rows]

    def stats(self, wid: str) -> dict:
        r = self.one("SELECT COUNT(*) n, COALESCE(SUM(n_chunks),0) c FROM sources WHERE workspace_id=?", (wid,))
        return {"sources": r["n"], "chunks": r["c"]}


def _split_sql(script: str) -> list[str]:
    return [s.strip() for s in script.split(";\n") if s.strip()]


def source_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["diagnostics"] = json.loads(d.get("diagnostics") or "{}")
    d["meta"] = json.loads(d.get("meta") or "{}")
    return d


def chunk_row(r: sqlite3.Row, with_embedding: bool = False) -> dict:
    d = dict(r)
    d["meta"] = json.loads(d.get("meta") or "{}")
    if "source_meta" in d:
        d["source_meta"] = json.loads(d["source_meta"] or "{}")
    emb = d.pop("embedding", None)
    if with_embedding and emb is not None:
        d["embedding"] = np.frombuffer(emb, dtype=np.float32)
    return d
