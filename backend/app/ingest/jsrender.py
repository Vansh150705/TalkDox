"""Optional JavaScript rendering with Playwright/Chromium (ENABLE_JS_RENDER=1).

Every sub-request the browser makes is intercepted and checked with the same SSRF guard used for
plain fetches, and heavyweight resources (images/media/fonts) are blocked. Residual risk: the browser
resolves DNS itself, so a DNS-rebinding attacker could race the pre-check. Run the renderer in a
network-isolated container for hostile-input deployments (see docs/SECURITY.md).
"""
from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit

from ..config import Settings
from ..errors import AppError
from ..net import SafeFetcher

log = logging.getLogger("talkdox.jsrender")

_BLOCK_TYPES = {"image", "media", "font", "stylesheet", "other", "websocket", "eventsource", "manifest"}


def js_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


def render_html(url: str, fetcher: SafeFetcher, s: Settings) -> str:
    if not js_available():
        raise AppError("JavaScript rendering is enabled but Playwright is not installed.", code="js_unavailable")
    from playwright.sync_api import sync_playwright, Error as PWError

    blocked: list[str] = []

    def handler(route):
        req = route.request
        u = req.url
        if urlsplit(u).scheme not in ("http", "https"):
            return route.abort()
        if req.resource_type in _BLOCK_TYPES:
            return route.abort()
        if not fetcher.is_safe(u):
            blocked.append(urlsplit(u).hostname or "")
            return route.abort()
        return route.continue_()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"],
                                        executable_path=os.getenv("CHROMIUM_PATH") or None)
            try:
                ctx = browser.new_context(user_agent=s.user_agent, service_workers="block",
                                          java_script_enabled=True, accept_downloads=False)
                ctx.route("**/*", handler)
                page = ctx.new_page()
                page.set_default_timeout(s.js_render_timeout_s * 1000)
                page.goto(url, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=min(8000, s.js_render_timeout_s * 1000))
                except PWError:
                    pass
                html = page.content()
            finally:
                browser.close()
    except PWError as e:
        log.info("js render failed: %s", str(e)[:120])
        raise AppError("The page could not be rendered with JavaScript.", code="js_render_failed")
    if blocked:
        log.info("js render blocked %d unsafe sub-requests", len(blocked))
    return html
