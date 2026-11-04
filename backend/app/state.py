"""Application state container (dependency wiring in one place; tests can inject doubles)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .config import Settings, load_settings
from .db import Database
from .ingest.pipeline import IngestService
from .net import SafeFetcher
from .providers.embeddings import BaseEmbedder, build_embedder
from .providers.llm import BaseLLM, build_llm
from .providers.ocr import BaseOCR, build_ocr
from .rag.answer import AnswerService
from .rag.retrieval import Retriever
from .rag.tools import ToolService
from .security import RateLimiter

log = logging.getLogger("talkdox.state")


@dataclass
class AppState:
    settings: Settings
    db: Database
    llm: Optional[BaseLLM]
    embedder: Optional[BaseEmbedder]
    ocr: Optional[BaseOCR]
    fetcher: SafeFetcher
    ingest: IngestService
    retriever: Optional[Retriever]
    answers: AnswerService
    tools: ToolService
    limiter: RateLimiter = field(default_factory=RateLimiter)


def build_state(settings: Settings | None = None, *, llm: BaseLLM | None = None, embedder: BaseEmbedder | None = None,
                ocr: BaseOCR | None = None, fetcher: SafeFetcher | None = None, yt_providers=None,
                yt_meta_fn=None) -> AppState:
    s = settings or load_settings()
    db = Database(s.db_path)
    try:
        llm = llm if llm is not None else build_llm(s)
    except Exception as e:
        log.error("LLM provider failed to initialise: %s", e)
        llm = None
    try:
        embedder = embedder if embedder is not None else build_embedder(s)
    except Exception as e:
        log.error("Embedding provider failed to initialise: %s", e)
        embedder = None
    try:
        ocr = ocr if ocr is not None else build_ocr(s)
    except Exception as e:
        log.error("OCR failed to initialise: %s", e)
        ocr = None
    fetcher = fetcher or SafeFetcher(s)
    ingest = IngestService(s, db, embedder, ocr, fetcher, yt_providers, yt_meta_fn)
    retriever = Retriever(db, embedder, s) if embedder else None
    tools = ToolService(s, db, llm)
    answers = AnswerService(s, db, llm, retriever, tools)
    return AppState(s, db, llm, embedder, ocr, fetcher, ingest, retriever, answers, tools)
