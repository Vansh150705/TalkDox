"""Embedding providers.

`GeminiEmbedder` uses gemini-embedding-001 (configurable) with Matryoshka output truncation and
L2 normalisation (required by Google for dimensions < 3072).
`HashEmbedder` is a deterministic feature-hashing embedder used ONLY as an offline test double and
for keyless local smoke tests; it has lexical (not semantic) similarity and is never auto-selected.
"""
from __future__ import annotations

import hashlib
import logging
import random
import re
import time
from typing import Optional

import numpy as np

from ..config import Settings
from ..errors import NotConfigured, ProviderError
from .llm import _status_to_error, ProviderRateLimited, ProviderUnavailable

log = logging.getLogger("talkdox.embed")

_BATCH = 64


def _normalize(m: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1
    return (m / n).astype(np.float32)


class BaseEmbedder:
    name = "base"
    model = ""
    dim = 0
    # Cosine range mapped onto [0,1] for relevance gating. Provider specific; the Gemini values are
    # conservative defaults that must be re-calibrated with scripts/eval_live.py on real data.
    cos_floor = 0.40
    cos_ceil = 0.80

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        raise NotImplementedError

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed_documents([text])[0]

    @property
    def fingerprint(self) -> str:
        return f"{self.name}|{self.model}|{self.dim}"


class GeminiEmbedder(BaseEmbedder):
    name = "gemini"

    def __init__(self, settings: Settings):
        if not settings.gemini_api_key:
            raise NotConfigured("GEMINI_API_KEY is not set.")
        from google import genai
        from google.genai import types
        self._types = types
        self._client = genai.Client(api_key=settings.gemini_api_key,
                                    http_options=types.HttpOptions(timeout=int(settings.llm_timeout_s * 1000)))
        self.model = settings.embedding_model
        self.dim = settings.embedding_dim
        self.retries = settings.llm_max_retries

    def _embed(self, texts: list[str], task: str) -> np.ndarray:
        from google.genai import errors as gerr
        t = self._types
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self._client.models.embed_content(
                    model=self.model, contents=texts,
                    config=t.EmbedContentConfig(task_type=task, output_dimensionality=self.dim))
                vecs = np.array([e.values for e in resp.embeddings], dtype=np.float32)
                if vecs.shape[0] != len(texts):
                    raise ProviderError("Embedding provider returned an unexpected number of vectors.",
                                        code="embedding_mismatch")
                return _normalize(vecs)
            except gerr.APIError as e:
                err = _status_to_error(getattr(e, "code", None), str(e))
                if not isinstance(err, (ProviderRateLimited, ProviderUnavailable)) or attempt >= self.retries:
                    raise err from e
                last = err
            except Exception as e:  # network errors
                if attempt >= self.retries:
                    raise ProviderUnavailable("Embedding provider unreachable.", code="embedding_unavailable") from e
                last = e
            time.sleep(min(2 ** attempt + random.random(), 20))
        raise ProviderUnavailable("Embedding provider unavailable.", code="embedding_unavailable") from last

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        out = []
        for i in range(0, len(texts), _BATCH):
            out.append(self._embed(texts[i:i + _BATCH], "RETRIEVAL_DOCUMENT"))
        return np.vstack(out) if out else np.zeros((0, self.dim), dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        return self._embed([text], "RETRIEVAL_QUERY")[0]


_TOKEN = re.compile(r"[A-Za-z0-9À-￿]+")


class HashEmbedder(BaseEmbedder):
    """Deterministic hashed bag-of-words + bigrams. TEST DOUBLE / offline smoke only."""
    name = "hash"
    model = "feature-hash-v1"
    cos_floor = 0.03
    cos_ceil = 0.45

    def __init__(self, dim: int = 512):
        self.dim = dim

    def _vec(self, text: str) -> np.ndarray:
        toks = [t.lower() for t in _TOKEN.findall(text)]
        v = np.zeros(self.dim, dtype=np.float32)
        feats = toks + [a + "_" + b for a, b in zip(toks, toks[1:])]
        for f in feats:
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
            v[(h >> 1) % self.dim] += 1.0 if (h & 1) else -1.0
        return v

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return _normalize(np.vstack([self._vec(t) for t in texts]))


def build_embedder(settings: Settings) -> Optional[BaseEmbedder]:
    p = settings.resolved_embedding_provider
    if p == "gemini":
        return GeminiEmbedder(settings)
    if p == "hash":
        return HashEmbedder(min(settings.embedding_dim, 1024))
    return None


def cache_key(embedder: BaseEmbedder, text: str) -> str:
    return hashlib.sha256(f"{embedder.fingerprint}\x00{text}".encode()).hexdigest()
