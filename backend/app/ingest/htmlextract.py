"""HTML -> structured blocks (main content only), link discovery and JS-rendering detection."""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..errors import IngestError
from ..net import normalize_url
from .models import Block, Extracted
from .textutil import md_table, normalize_text, parse_markdown

log = logging.getLogger("talkdox.html")

_BOILER = ["script", "style", "noscript", "template", "svg", "iframe", "form", "button", "canvas", "dialog",
           "nav", "footer", "aside", "header"]
_BOILER_RE = re.compile(r"(^|[\s_-])(nav|navbar|menu|sidebar|footer|header|breadcrumb|cookie|consent|banner|"
                        r"advert|ads?|promo|popup|modal|share|social|related|comments?|newsletter|subscribe|toc)($|[\s_-])", re.I)
_SPA_MARKERS = re.compile(r'id=["\'](root|app|__next|__nuxt|svelte)["\']|ng-app|data-reactroot|enable javascript|'
                          r"you need to enable javascript|requires javascript", re.I)


def _soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def page_title(soup: BeautifulSoup, fallback: str = "") -> str:
    for sel in (("meta", {"property": "og:title"}), ("meta", {"name": "twitter:title"})):
        t = soup.find(*sel)
        if t and t.get("content", "").strip():
            return t["content"].strip()[:200]
    if soup.title and soup.title.string and soup.title.string.strip():
        return re.sub(r"\s+", " ", soup.title.string).strip()[:200]
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(" ", strip=True)[:200]
    return fallback


def canonical_url(html_or_soup, url: str) -> str:
    """`<link rel=canonical>` honoured only when it stays on the same host (prevents a page from
    redirecting our identity/attribution to an unrelated domain)."""
    soup = html_or_soup if isinstance(html_or_soup, BeautifulSoup) else _soup(html_or_soup)
    link = soup.find("link", rel=lambda v: v and "canonical" in (v if isinstance(v, list) else [v]))
    if link and link.get("href"):
        cand = normalize_url(link["href"], base=url)
        if urlsplit(cand).hostname == urlsplit(url).hostname:
            return cand
    return normalize_url(url)


def find_links(html: str, base_url: str) -> list[str]:
    soup = _soup(html)
    base = soup.find("base", href=True)
    if base:
        base_url = urljoin(base_url, base["href"])
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        if (a.get("rel") and "nofollow" in a.get("rel")) and False:
            continue
        try:
            u = normalize_url(href, base=base_url)
        except Exception:
            continue
        if urlsplit(u).scheme in ("http", "https") and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def robots_noindex(html: str) -> bool:
    soup = _soup(html)
    m = soup.find("meta", attrs={"name": re.compile("^(robots|googlebot)$", re.I)})
    return bool(m and re.search(r"noindex|noai", m.get("content", ""), re.I))


def looks_js_rendered(html: str, extracted_chars: int) -> bool:
    if extracted_chars >= 800:
        return False
    return bool(_SPA_MARKERS.search(html)) or len(html) > 20000 and extracted_chars < 200


def extract_html(html: str, url: str | None, fallback_title: str = "") -> Extracted:
    soup = _soup(html)
    title = page_title(soup, fallback_title or (urlsplit(url).netloc if url else ""))
    blocks: list[Block] = []
    method = "trafilatura"
    try:
        import trafilatura
        md = trafilatura.extract(html, url=url, output_format="markdown", include_tables=True,
                                 include_comments=False, include_links=False, include_formatting=True,
                                 favor_recall=True, deduplicate=False)
        if md:
            blocks = parse_markdown(md, url=url)
    except Exception as e:  # trafilatura can raise on pathological markup
        log.debug("trafilatura failed: %s", type(e).__name__)
    chars = sum(len(b.text) for b in blocks)
    if chars < 400:  # trafilatura found little: try a semantic-container fallback and keep the richer result
        fb = _fallback_blocks(soup, url)
        if sum(len(b.text) for b in fb) > chars:
            blocks, method = fb, "bs4-fallback"
    blocks = _merge_title(blocks, title)
    if not blocks:
        raise IngestError("No readable content was found on this page.", code="empty_page")
    return Extracted(title=title, blocks=blocks, n_pages=1,
                     diagnostics={"extractor": method, "chars": sum(len(b.text) for b in blocks)})


def _merge_title(blocks: list[Block], title: str) -> list[Block]:
    return blocks


def _fallback_blocks(soup: BeautifulSoup, url: str | None) -> list[Block]:
    s = _soup(str(soup))
    for t in s(_BOILER):
        t.decompose()
    for t in s.find_all(attrs={"class": _BOILER_RE}) + s.find_all(attrs={"id": _BOILER_RE}):
        if t.name not in ("html", "body", "main", "article"):
            t.decompose()
    root = s.find("main") or s.find("article") or s.find(attrs={"role": "main"}) or s.body or s
    blocks: list[Block] = []
    for el in root.descendants:
        name = getattr(el, "name", None)
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            t = el.get_text(" ", strip=True)
            if t:
                blocks.append(Block("heading", t, level=int(name[1]), url=url))
        elif name == "p":
            if el.find_parent(["table", "li", "pre"]):
                continue
            t = el.get_text(" ", strip=True)
            if len(t) > 25:
                blocks.append(Block("paragraph", t, url=url))
        elif name in ("ul", "ol") and not el.find_parent(["li"]):
            items = [li.get_text(" ", strip=True) for li in el.find_all("li", recursive=False)]
            items = [i for i in items if i]
            if items:
                blocks.append(Block("list", "\n".join(f"- {i}" for i in items), url=url))
        elif name == "pre":
            blocks.append(Block("code", el.get_text("\n").strip("\n"), url=url))
        elif name == "table":
            rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])] for tr in el.find_all("tr")]
            md = md_table(rows)
            if md:
                blocks.append(Block("table", md, url=url))
        elif name == "blockquote":
            t = el.get_text(" ", strip=True)
            if t and not el.find("p"):
                blocks.append(Block("paragraph", t, url=url))
    return blocks
