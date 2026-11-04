"""Website ingestion: polite, deduplicating, bounded crawler on top of the SSRF-safe fetcher."""
from __future__ import annotations

import hashlib
import logging
import re
import tempfile
import time
import urllib.robotparser
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from ..config import Settings
from ..errors import AppError, Cancelled, IngestError
from ..net import SafeFetcher, dedupe_key, normalize_url
from .htmlextract import canonical_url, extract_html, find_links, looks_js_rendered, _soup
from .models import Block, Extracted

log = logging.getLogger("talkdox.web")

HTML_TYPES = ["text/html", "application/xhtml+xml", "text/plain", "text/markdown", "application/pdf"]
_SKIP_EXT = re.compile(r"\.(png|jpe?g|gif|webp|svg|ico|css|js|mjs|json|xml|zip|gz|tar|tgz|rar|7z|mp3|mp4|webm|avi|mov|"
                       r"woff2?|ttf|eot|exe|dmg|iso|bin|apk|docx?|xlsx?|pptx?|csv|rss|atom)(\?|$)", re.I)


@dataclass
class CrawlOptions:
    crawl: bool = False
    max_depth: int = 1
    max_pages: int = 20
    path_prefix: str | None = None
    include_subdomains: bool = False
    use_sitemap: bool = True


@dataclass
class PageRecord:
    url: str
    status: str                  # ok | failed | skipped
    code: str | None = None
    message: str | None = None
    title: str | None = None
    chars: int = 0
    via: str = "http"            # http | js | pdf
    depth: int = 0
    canonical: str | None = None


class Robots:
    """robots.txt per origin (cached). RFC 9309 semantics: 4xx -> allow all, 5xx/unreachable -> disallow all."""

    def __init__(self, fetcher: SafeFetcher, s: Settings):
        self.f, self.s = fetcher, s
        self._cache: dict[str, tuple[urllib.robotparser.RobotFileParser | None, bool, list[str]]] = {}

    def _load(self, origin: str):
        if origin in self._cache:
            return self._cache[origin]
        rp = urllib.robotparser.RobotFileParser()
        allow_all, sitemaps = False, []
        try:
            r = self.f.get(origin + "/robots.txt", max_bytes=512 * 1024, accept="text/plain,*/*",
                           allowed_types=None, timeout=8)
            lines = r.text.splitlines()
            rp.parse(lines)
            sitemaps = [l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("sitemap:")]
            res = (rp, False, sitemaps)
        except AppError as e:
            st = (e.detail or {}).get("status")
            if st and 400 <= st < 500:
                res = (None, True, [])           # no robots.txt -> everything allowed
            elif e.code in ("blocked_url", "invalid_url"):
                raise
            else:
                res = (None, False, [])          # unreachable / 5xx -> be conservative (disallow)
        self._cache[origin] = res
        return res

    def allowed(self, url: str) -> bool:
        if not self.s.respect_robots:
            return True
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        rp, allow_all, _ = self._load(origin)
        if allow_all:
            return True
        if rp is None:
            return False
        return rp.can_fetch(self.s.user_agent.split("/")[0], url) and rp.can_fetch("*", url)

    def crawl_delay(self, url: str) -> float:
        p = urlsplit(url)
        rp, _, _ = self._load(f"{p.scheme}://{p.netloc}")
        try:
            d = rp.crawl_delay("*") if rp else None
        except Exception:
            d = None
        return min(float(d), 10.0) if d else 0.0

    def sitemaps(self, url: str) -> list[str]:
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        _, _, sm = self._load(origin)
        return sm or [origin + "/sitemap.xml"]


def _same_scope(url: str, seed: str, opts: CrawlOptions) -> bool:
    u, s = urlsplit(url), urlsplit(seed)
    uh, sh = (u.hostname or "").removeprefix("www."), (s.hostname or "").removeprefix("www.")
    if uh != sh and not (opts.include_subdomains and uh.endswith("." + sh)):
        return False
    if opts.path_prefix and not u.path.startswith(opts.path_prefix):
        return False
    return True


def _default_prefix(seed: str) -> str:
    p = urlsplit(seed).path or "/"
    if p.endswith("/"):
        return p
    return p.rsplit("/", 1)[0] + "/"


def parse_sitemap(xml_text: str) -> tuple[list[str], list[str]]:
    """returns (page urls, child sitemap urls)"""
    try:
        root = ET.fromstring(xml_text.encode("utf-8", "ignore"))
    except ET.ParseError:
        return [], []
    ns = re.match(r"\{.*\}", root.tag)
    pre = ns.group(0) if ns else ""
    locs = [e.text.strip() for e in root.iter(pre + "loc") if e.text]
    if root.tag.endswith("sitemapindex"):
        return [], locs
    return locs, []


def ingest_web(seeds: list[str], opts: CrawlOptions, fetcher: SafeFetcher, s: Settings,
               progress: Callable[[float, str], None] = lambda f, m: None,
               check_cancel: Callable[[], None] = lambda: None, ocr=None) -> Extracted:
    from .documents import extract_file
    from .jsrender import render_html

    robots = Robots(fetcher, s)
    max_pages = max(1, min(opts.max_pages, s.crawl_max_pages)) if opts.crawl else len(seeds)
    max_depth = max(0, min(opts.max_depth, s.crawl_max_depth)) if opts.crawl else 0
    t_end = time.time() + s.crawl_timeout_s
    seen: set[str] = set()
    seen_canon: set[str] = set()
    seen_hash: set[str] = set()
    records: list[PageRecord] = []
    blocks: list[Block] = []
    title = None
    last_fetch: dict[str, float] = {}

    q: deque[tuple[str, int, str]] = deque()
    for sd in seeds:
        q.append((sd, 0, sd))

    sitemap_added = False
    processed = 0
    while q and processed < max_pages:
        check_cancel()
        if time.time() > t_end:
            records.append(PageRecord(url="", status="skipped", code="crawl_timeout",
                                      message="Crawl time limit reached; remaining pages were skipped."))
            break
        url, depth, seed = q.popleft()
        try:
            norm = normalize_url(url)
            key = dedupe_key(url)
        except Exception:
            continue
        if key in seen:
            continue
        seen.add(key)
        rec = PageRecord(url=norm, status="failed", depth=depth)
        records.append(rec)
        processed += 1
        progress(min(0.9, processed / max_pages), f"Fetching page {processed}: {urlsplit(norm).path or '/'}")
        try:
            if not robots.allowed(norm):
                raise AppError("This page is disallowed for automated access by the site's robots.txt.",
                               code="robots_disallowed")
            host = urlsplit(norm).hostname or ""
            delay = max(s.crawl_delay_s, robots.crawl_delay(norm))
            wait = last_fetch.get(host, 0) + delay - time.time()
            if wait > 0 and processed > 1:
                time.sleep(min(wait, 10))
            last_fetch[host] = time.time()
            res = fetcher.get(norm, allowed_types=HTML_TYPES)
            final = normalize_url(res.url)
            if dedupe_key(final) != key:
                if dedupe_key(final) in seen:
                    rec.status, rec.code, rec.message = "skipped", "duplicate", f"Redirects to already-processed {final}"
                    continue
                seen.add(dedupe_key(final))
                if not robots.allowed(final):
                    raise AppError("The redirect target is disallowed by robots.txt.", code="robots_disallowed")
            page_blocks, page_title, via, html = _extract_response(res, final, s, fetcher, ocr, extract_file)
            chars = sum(len(b.text) for b in page_blocks)
            if chars == 0 and html is not None and not looks_js_rendered(html, 0) and not s.enable_js_render:
                raise AppError("Not enough readable content on this page (it may be empty, blocked or behind a "
                               "login).", code="empty_page")
            if chars < 120 and html is not None and looks_js_rendered(html, chars):
                if s.enable_js_render:
                    html = render_html(final, fetcher, s)
                    ex = extract_html(html, final)
                    page_blocks, page_title, via = ex.blocks, ex.title, "js"
                    chars = sum(len(b.text) for b in page_blocks)
                else:
                    raise AppError("This page builds its content with JavaScript, which isn't enabled on this "
                                   "server, so no readable text was found.", code="needs_js")
            if chars < (80 if via in ("http", "js") and html is not None else 20):
                raise AppError("Not enough readable content on this page (it may be empty, blocked or behind a "
                               "login).", code="empty_page")
            canon = canonical_url(html, final) if html else final
            rec.canonical = canon
            ckey = dedupe_key(canon)
            h = hashlib.sha1(re.sub(r"\s+", " ", " ".join(b.text for b in page_blocks)).lower().encode()).hexdigest()
            if ckey in seen_canon or h in seen_hash:
                rec.status, rec.code, rec.message = "skipped", "duplicate", "Duplicate of a page already included."
                if html and opts.crawl and depth < max_depth:
                    _enqueue(q, html, final, depth, seed, opts, seen)
                continue
            seen_canon.add(ckey)
            seen_hash.add(h)
            rec.status, rec.title, rec.chars, rec.via = "ok", page_title, chars, via
            rec.url = canon
            title = title or page_title
            blocks.append(Block("heading", page_title or canon, level=1, url=canon))
            for b in page_blocks:
                b.url = canon
                if b.kind == "heading":
                    b.level = min(max(b.level, 1) + 1, 6)   # page title is level 1
            blocks.extend(page_blocks)
            if html and opts.crawl and depth < max_depth:
                _enqueue(q, html, final, depth, seed, opts, seen)
            if opts.crawl and opts.use_sitemap and not sitemap_added and depth == 0:
                sitemap_added = True
                for u in _sitemap_urls(robots, fetcher, final, opts, s, seed)[: max_pages * 3]:
                    q.append((u, 1, seed))
        except IngestError as e:
            rec.code, rec.message = e.code, e.message
        except AppError as e:
            rec.code, rec.message = e.code, e.message
        except Cancelled:
            raise
        except Exception as e:  # never let one hostile page kill the crawl
            log.warning("page failed: %s %s", norm, type(e).__name__)
            rec.code, rec.message = "extract_failed", "This page could not be processed."

    ok = [r for r in records if r.status == "ok"]
    failed = [r for r in records if r.status == "failed"]
    diag = {"extractor": "web", "pages": [r.__dict__ for r in records], "pages_ok": len(ok), "pages_failed": len(failed),
            "crawl": opts.crawl, "max_depth": max_depth, "max_pages": max_pages}
    if not ok:
        first = failed[0] if failed else None
        raise IngestError(first.message if first else "Nothing could be retrieved from that URL.",
                          code=first.code if first and first.code else "fetch_failed", detail=diag)
    warnings = [f"{r.url}: {r.message}" for r in failed][:20]
    return Extracted(title=title or urlsplit(seeds[0]).netloc, blocks=blocks, n_pages=len(ok), diagnostics=diag,
                     warnings=warnings, partial=bool(failed) and opts.crawl or (bool(failed) and len(seeds) > 1))


def _extract_response(res, final_url, s, fetcher, ocr, extract_file):
    if res.content_type == "application/pdf":
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "page.pdf"
            p.write_bytes(res.content)
            ex = extract_file(p, Path(urlsplit(final_url).path).name or "page.pdf", s, ocr)
        return ex.blocks, ex.title, "pdf", None
    html = res.text
    if res.content_type in ("text/plain", "text/markdown"):
        from .textutil import parse_markdown
        bl = parse_markdown(html)
        return bl, urlsplit(final_url).path.rsplit("/", 1)[-1] or final_url, "http", None
    try:
        ex = extract_html(html, final_url)
    except IngestError as e:
        if e.code != "empty_page":
            raise
        return [], "", "http", html          # may still be recoverable by JS rendering
    return ex.blocks, ex.title, "http", html


def _enqueue(q, html, base, depth, seed, opts, seen):
    prefix = opts.path_prefix
    eff = CrawlOptions(**{**opts.__dict__, "path_prefix": prefix or _default_prefix(seed)})
    for link in find_links(html, base):
        if dedupe_key(link) in seen or _SKIP_EXT.search(urlsplit(link).path):
            continue
        if _same_scope(link, seed, eff):
            q.append((link, depth + 1, seed))


def _sitemap_urls(robots: Robots, fetcher: SafeFetcher, url: str, opts: CrawlOptions, s: Settings, seed: str) -> list[str]:
    eff = CrawlOptions(**{**opts.__dict__, "path_prefix": opts.path_prefix or _default_prefix(seed)})
    out: list[str] = []
    todo = list(robots.sitemaps(url))[:3]
    fetched = 0
    while todo and fetched < 6 and len(out) < 500:
        sm = todo.pop(0)
        fetched += 1
        try:
            r = fetcher.get(sm, max_bytes=3 * 1024 * 1024, accept="application/xml,text/xml,*/*", timeout=10)
            text = r.text
        except AppError:
            continue
        pages, children = parse_sitemap(text)
        todo.extend(children[:3])
        for u in pages:
            if _same_scope(u, seed, eff) and not _SKIP_EXT.search(urlsplit(u).path):
                out.append(u)
    return out
