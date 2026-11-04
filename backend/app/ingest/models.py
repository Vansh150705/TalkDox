"""Data model shared by every extractor.

Extractors turn *anything* into an ordered list of `Block`s that keep structure (headings, tables,
code, page/slide numbers, URLs, timestamps). Chunking and citation both depend on this metadata
being faithful to the original source, so extractors must never invent it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Block:
    kind: str                     # heading | paragraph | list | table | code | transcript
    text: str
    page: Optional[int] = None    # PDF page / PPTX slide number (1-based). None when the format has no pages.
    level: int = 0                # heading level (1 = top)
    url: Optional[str] = None     # web: the page this block came from
    t_start: Optional[float] = None   # transcript seconds
    t_end: Optional[float] = None
    meta: dict = field(default_factory=dict)


@dataclass
class Extracted:
    title: str
    blocks: list[Block]
    n_pages: int = 0                       # pages / slides / sheets / crawled pages
    diagnostics: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    partial: bool = False                  # True when some content could not be extracted
    meta: dict = field(default_factory=dict)

    @property
    def n_chars(self) -> int:
        return sum(len(b.text) for b in self.blocks)


@dataclass
class ChunkDraft:
    kind: str
    text: str
    ctx_text: str
    page: Optional[int] = None
    page_end: Optional[int] = None
    heading: Optional[str] = None
    section: Optional[str] = None
    url: Optional[str] = None
    t_start: Optional[float] = None
    t_end: Optional[float] = None
    text_hash: str = ""
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return self.__dict__.copy()
