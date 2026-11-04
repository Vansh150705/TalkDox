"""Workspace tokens, rate limiting and content-safety helpers.

Identity model: anonymous, server-issued workspaces. `POST /api/workspaces` returns an unguessable
bearer token `<workspace_id>.<hmac>`; every data endpoint requires it and every DB query is scoped
by the workspace id taken from the *verified* token, never from request parameters.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import threading
import time
from collections import defaultdict, deque

from fastapi import Request

from .errors import AppError, RateLimited


def sign_workspace(secret: str, wid: str) -> str:
    sig = hmac.new(secret.encode(), wid.encode(), hashlib.sha256).hexdigest()[:40]
    return f"{wid}.{sig}"


def verify_token(secret: str, token: str) -> str | None:
    if not token or "." not in token or len(token) > 200:
        return None
    wid, _, sig = token.rpartition(".")
    expected = hmac.new(secret.encode(), wid.encode(), hashlib.sha256).hexdigest()[:40]
    return wid if hmac.compare_digest(sig, expected) else None


class RateLimiter:
    """In-process sliding-window limiter. Sufficient for a single instance; use a shared store
    (Redis) in front of multiple instances - documented in docs/DEPLOYMENT.md."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._last_gc = time.time()

    def check(self, key: str, limit: int, window_s: float) -> None:
        if limit <= 0:
            return
        now = time.time()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - window_s:
                q.popleft()
            if len(q) >= limit:
                retry = int(q[0] + window_s - now) + 1
                raise RateLimited(f"Too many requests. Please wait about {retry}s and try again.",
                                  detail={"retry_after": retry})
            q.append(now)
            if now - self._last_gc > 600:  # bound memory
                self._last_gc = now
                for k in [k for k, v in self._hits.items() if not v or v[-1] < now - 7200]:
                    del self._hits[k]


def client_ip(request: Request, trust_proxy: bool, hops: int = 1) -> str:
    """With TRUST_PROXY_HEADERS the client is the entry `hops` from the END of X-Forwarded-For: each trusted proxy appends
    the address it saw, so the left-most entries (which a client can forge) are ignored."""
    if trust_proxy:
        xff = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if xff:
            return xff[-hops] if len(xff) >= hops else xff[0]
    return request.client.host if request.client else "unknown"


# --------------------------------------------------------------- prompt-injection awareness
_INJECTION = [
    r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions|prompts|messages)",
    r"disregard (all |any |the )?(previous|prior|above|earlier|system)",
    r"you are now (?:a|an|in)\b",
    r"(reveal|print|show|output|repeat) (your |the )?(system|hidden|initial) (prompt|instructions)",
    r"new instructions?\s*:",
    r"</?(system|assistant|evidence|instructions?)>",
    r"\bBEGIN (SYSTEM|ADMIN) (PROMPT|MESSAGE)\b",
    r"(send|post|exfiltrate|upload) .{0,60}(api[_ -]?key|password|secret|token|credentials)",
]
_INJ_RE = re.compile("|".join(_INJECTION), re.I)


def looks_like_injection(text: str) -> bool:
    return bool(_INJ_RE.search(text))


def neutralize_for_prompt(text: str) -> str:
    """Escape sequences that could be mistaken for our own prompt delimiters inside untrusted text."""
    return (text.replace("<evidence", "&lt;evidence").replace("</evidence", "&lt;/evidence")
                .replace("<source", "&lt;source").replace("</source", "&lt;/source"))


_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_TAG = re.compile(r"<\s*/?\s*(script|iframe|object|embed|img|style|link|meta|form|svg|math)[^>]*>", re.I)


def sanitize_model_output(text: str) -> str:
    """Remove constructs that could exfiltrate data or execute in a renderer (images with
    attacker-controlled URLs, raw HTML). The frontend additionally renders without innerHTML."""
    text = _IMG.sub("", text)
    return _HTML_TAG.sub("", text)


def redact(text: str) -> str:
    """Mask things that look like API keys before they can reach logs."""
    return re.sub(r"(AIza[0-9A-Za-z_\-]{20,}|gsk_[0-9A-Za-z]{20,}|sk-[0-9A-Za-z]{20,})", "[REDACTED]", text)


class RedactingFilter:
    def filter(self, record) -> bool:  # logging.Filter protocol
        try:
            record.msg = redact(str(record.msg))
            if record.args:
                record.args = tuple(redact(str(a)) if isinstance(a, str) else a for a in
                                    (record.args if isinstance(record.args, tuple) else (record.args,)))
        except Exception:
            pass
        return True


__all__ = ["sign_workspace", "verify_token", "RateLimiter", "client_ip", "looks_like_injection",
           "neutralize_for_prompt", "sanitize_model_output", "redact", "RedactingFilter", "AppError"]
