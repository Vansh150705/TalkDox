"""Typed errors. `code` is a stable machine-readable identifier the frontend can switch on;
`message` is always safe to show to end users (never contains stack traces or secrets)."""
from __future__ import annotations


class AppError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message: str, code: str | None = None, status: int | None = None, detail: dict | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status:
            self.status = status
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {"error": self.message, "code": self.code, **({"detail": self.detail} if self.detail else {})}


class IngestError(AppError):
    """A source could not be turned into searchable content. Never swallowed into an empty index."""
    code = "ingest_failed"


class UnsafeURLError(AppError):
    code = "unsafe_url"


class NotConfigured(AppError):
    status = 503
    code = "not_configured"


class RateLimited(AppError):
    status = 429
    code = "rate_limited"


class ProviderError(AppError):
    """Upstream model/provider failure."""
    status = 502
    code = "provider_error"


class ProviderRateLimited(ProviderError):
    status = 503
    code = "provider_rate_limited"


class ProviderUnavailable(ProviderError):
    status = 503
    code = "provider_unavailable"


class Cancelled(Exception):
    pass
