"""Structure-aware chunking.

Rules (each one exists because naive fixed-size splitting breaks citations or answers):
* never merge text across a heading boundary - the heading path becomes chunk metadata
* never merge across pages/slides, and never across different web pages - page/URL stays exact
* tables are atomic; oversized tables are split by rows with the header row repeated
* code blocks are atomic (split by lines only when huge)
* transcript segments are grouped into time windows keeping exact start/end seconds
* size-based splits carry a small sentence-level overlap so facts straddling a boundary survive
* every chunk gets `ctx_text` = "Title > heading path" + text; that is what is embedded and indexed
  (deterministic contextual retrieval - no extra LLM calls)
"""
from __future__ import annotations

import hashlib
import re

from ..config import Settings
from ..errors import IngestError
from .models import Block, ChunkDraft, Extracted
from .textutil import sentences

TRANSCRIPT_WINDOW_S = 75.0


def _hash(t: str) -> str:
    return hashlib.sha1(t.encode("utf-8", "ignore")).hexdigest()


def split_long(text: str, max_chars: int) -> list[str]:
    """Split text that is longer than max_chars on sentence boundaries, then on words."""
    if len(text) <= max_chars:
        return [text]
    out, cur = [], ""
    for s in sentences(text) or [text]:
        if len(s) > max_chars:
            if cur:
                out.append(cur)
                cur = ""
            words, piece = s.split(" "), ""
            for w in words:
                if len(piece) + len(w) + 1 > max_chars:
                    out.append(piece.strip())
                    piece = ""
                piece += w + " "
            if piece.strip():
                out.append(piece.strip())
            continue
        if len(cur) + len(s) + 1 > max_chars:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        out.append(cur)
    return out


def split_table(md: str, max_chars: int) -> list[str]:
    lines = md.split("\n")
    if len(md) <= max_chars or len(lines) <= 3:
        return [md]
    header, body = lines[:2], lines[2:]
    parts, cur, size = [], [], sum(len(l) + 1 for l in header)
    for row in body:
        if cur and size + len(row) + 1 > max_chars:
            parts.append("\n".join(header + cur))
            cur, size = [], sum(len(l) + 1 for l in header)
        cur.append(row)
        size += len(row) + 1
    if cur:
        parts.append("\n".join(header + cur))
    return parts


def _tail_overlap(text: str, overlap: int) -> str:
    if overlap <= 0:
        return ""
    ss = sentences(text)
    tail = ""
    for s in reversed(ss):
        if len(tail) + len(s) > overlap:
            break
        tail = f"{s} {tail}".strip()
    return tail


class _Chunker:
    def __init__(self, title: str, cfg: Settings):
        self.title = title
        self.cfg = cfg
        self.out: list[ChunkDraft] = []
        self.path: list[tuple[int, str]] = []
        self.section_no = 0
        self.section = "s0"
        self.buf: list[str] = []
        self.buf_len = 0
        self.cur_page: int | None = None
        self.cur_url: str | None = None
        self.kind = "text"
        self.carry = ""
        self.segs: list[tuple[str, float | None, float | None]] = []

    # ---- helpers
    def heading_str(self) -> str:
        return " › ".join(h for _, h in self.path)

    def _emit(self, text: str, kind: str, page, page_end=None, t0=None, t1=None, url=None, meta=None) -> None:
        text = text.strip()
        if not text:
            return
        heading = self.heading_str()
        prefix = " › ".join(x for x in (self.title, heading) if x)
        ctx = f"{prefix}\n{text}" if prefix else text
        self.out.append(ChunkDraft(
            kind=kind, text=text, ctx_text=ctx, page=page, page_end=page_end if page_end != page else None,
            heading=heading or None, section=self.section, url=url, t_start=t0, t_end=t1,
            text_hash=_hash(text), meta=meta or {}))
        if len(self.out) > self.cfg.max_chunks_per_source:
            raise IngestError("This source is too large to index in full.", code="too_large",
                              detail={"max_chunks": self.cfg.max_chunks_per_source})

    def flush(self, overlap: bool = False) -> None:
        if self.kind == "transcript":
            self._flush_transcript()
            return
        if not self.buf:
            return
        text = "\n\n".join(self.buf)
        body = f"{self.carry} {text}" if self.carry else text
        meta = {"overlap_prefix": len(self.carry)} if self.carry else {}
        self._emit(body, self.kind, self.cur_page, url=self.cur_url, meta=meta)
        self.carry = _tail_overlap(text, self.cfg.chunk_overlap_chars) if overlap else ""
        self.buf, self.buf_len = [], 0

    def boundary(self) -> None:
        self.flush(overlap=False)
        self.carry = ""

    # ---- block handlers
    def add(self, b: Block) -> None:
        # hard boundaries (checked first, headings included): page change or web page change
        if (b.page != self.cur_page and (b.page is not None or self.cur_page is not None)) or b.url != self.cur_url:
            self.boundary()
            if b.url != self.cur_url:
                self.path = []          # headings never leak from one web page into the next
            self.cur_page, self.cur_url = b.page, b.url
        if b.kind == "heading":
            self.boundary()
            level = b.level or 2
            while self.path and self.path[-1][0] >= level:
                self.path.pop()
            self.path.append((level, b.text.strip()))
            if level <= 3:
                self.section_no += 1
                self.section = f"s{self.section_no}"
            return
        if b.kind == "table":
            self.boundary()
            for i, part in enumerate(split_table(b.text, self.cfg.chunk_max_chars)):
                self._emit(part, "table", b.page, url=b.url, meta={**b.meta, "table": True, "part": i})
            return
        if b.kind == "code":
            self.boundary()
            code = b.text
            if len(code) <= self.cfg.chunk_max_chars:
                self._emit(code, "code", b.page, url=b.url, meta=b.meta)
            else:
                cur = ""
                for line in code.split("\n"):
                    if len(cur) + len(line) + 1 > self.cfg.chunk_max_chars and cur:
                        self._emit(cur, "code", b.page, url=b.url, meta=b.meta)
                        cur = ""
                    cur += line + "\n"
                self._emit(cur, "code", b.page, url=b.url, meta=b.meta)
            return
        if b.kind == "transcript":
            self._add_transcript(b)
            return
        # paragraph / list
        kind = "text"
        if self.kind != kind:
            self.boundary()
            self.kind = kind
        pieces = split_long(b.text, self.cfg.chunk_max_chars)
        for piece in pieces:
            if self.buf and self.buf_len + len(piece) > self.cfg.chunk_target_chars:
                self.flush(overlap=True)
            self.buf.append(piece)
            self.buf_len += len(piece) + 2

    def _add_transcript(self, b: Block) -> None:
        if self.kind != "transcript":
            self.boundary()
            self.kind = "transcript"
        if self.segs:
            chars = sum(len(t) + 1 for t, _, _ in self.segs)
            span = (b.t_end if b.t_end is not None else (b.t_start or 0)) - (self.segs[0][1] or 0)
            if chars + len(b.text) > self.cfg.chunk_target_chars or span > TRANSCRIPT_WINDOW_S:
                self._flush_transcript(keep_last=True)
        self.segs.append((b.text, b.t_start, b.t_end if b.t_end is not None else b.t_start))

    def _flush_transcript(self, keep_last: bool = False) -> None:
        if not self.segs:
            return
        text = " ".join(t for t, _, _ in self.segs)
        t0 = self.segs[0][1]
        t1 = self.segs[-1][2]
        self._emit(text, "transcript", None, t0=t0, t1=t1, url=self.cur_url,
                   meta={"segments": len(self.segs)})
        # one-segment overlap keeps sentences that straddle a window boundary retrievable
        self.segs = [self.segs[-1]] if keep_last and len(self.segs) > 1 else []


def chunk_extracted(ex: Extracted, cfg: Settings) -> list[ChunkDraft]:
    ch = _Chunker(ex.title, cfg)
    for b in ex.blocks:
        if not b.text or not b.text.strip():
            continue
        ch.add(b)
    ch.boundary()
    # stitch page ranges for chunks that were assembled from several consecutive blocks
    return ch.out


def is_noise(text: str) -> bool:
    t = re.sub(r"[\W_]+", "", text)
    return len(t) < 3
