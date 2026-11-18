"""SSRF-safe outbound HTTP.

Every outbound request made on behalf of a user (web pages, robots.txt, sitemaps, oEmbed, subtitle
files) goes through `SafeFetcher`. Defences:

* only http/https, no embedded credentials, allow-listed ports
* hostname is resolved by us; *every* returned address must be globally routable
  (blocks loopback, RFC1918, link-local incl. 169.254.169.254, CGNAT, ULA, multicast, reserved,
  IPv4-mapped/6to4/Teredo forms of those)
* the connection is pinned to the validated IP (Host header + TLS SNI preserve the original name),
  which closes the DNS-rebinding window between "check" and "connect"
* redirects are followed manually, each hop re-validated and re-pinned, max N hops, no https->http
* response size, decoded size and total wall-clock time are capped; content types are allow-listed
* no cookies, no proxy env vars, no credentials ever forwarded
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from .config import Settings
from .errors import AppError, UnsafeURLError

log = logging.getLogger("talkdox.net")

_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet", ".corp", ".private")
# Hostnames made only of decimal / hex / octal numeric labels ("2130706433", "0x7f000001", "127.1", "0177.0.0.1").
_NUMERIC_HOST = re.compile(r"^(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+))*$", re.IGNORECASE)
_BLOCKED_HOSTS = {"localhost", "metadata", "metadata.google.internal", "instance-data"}
_TRACKING = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "yclid", "_hsenc", "_hsmi", "ref_src"}
_REDIRECT = {301, 302, 303, 307, 308}


def _addr_is_public(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return _addr_is_public(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return _addr_is_public(ip.sixtofour)
        if ip.teredo is not None:
            return all(_addr_is_public(x) for x in ip.teredo)
        # NAT64 well-known prefix 64:ff9b::/96 embeds an IPv4 address
        if ip in ipaddress.ip_network("64:ff9b::/96"):
            return _addr_is_public(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return bool(ip.is_global) and not ip.is_multicast


@dataclass
class FetchResult:
    url: str
    status: int
    content: bytes
    content_type: str
    headers: dict
    redirects: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    truncated: bool = False

    @property
    def text(self) -> str:
        return decode_bytes(self.content, self.headers.get("content-type", ""))


def decode_bytes(b: bytes, content_type: str = "") -> str:
    import re
    enc = None
    m = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
    if m:
        enc = m.group(1)
    if enc is None:
        head = b[:4096].decode("ascii", "ignore")
        m = re.search(r'<meta[^>]+charset=["\']?([\w\-]+)', head, re.I)
        if m:
            enc = m.group(1)
    if b.startswith(b"\xef\xbb\xbf"):
        enc = "utf-8-sig"
    elif b.startswith((b"\xff\xfe", b"\xfe\xff")):
        enc = "utf-16"
    for e in [enc, "utf-8", "cp1252", "latin-1"]:
        if not e:
            continue
        try:
            return b.decode(e)
        except (LookupError, UnicodeDecodeError):
            continue
    return b.decode("utf-8", "replace")


def normalize_url(url: str, base: str | None = None) -> str:
    """Canonical form used for deduplication: lowercase scheme/host, default ports dropped,
    fragment removed, tracking params stripped, query sorted, trailing '/index.html' kept as is."""
    if base:
        url = urljoin(base, url)
    p = urlsplit(url.strip())
    scheme = p.scheme.lower()
    host = (p.hostname or "").lower().rstrip(".")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    port = p.port
    netloc = host
    if ":" in host:
        netloc = f"[{host}]"
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc += f":{port}"
    path = quote(p.path or "/", safe="/%:@!$&'()*+,;=-._~")   # trailing slash kept: /docs/ and /docs may differ
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if not k.lower().startswith("utm_") and k.lower() not in _TRACKING]
    q.sort()
    return urlunsplit((scheme, netloc, path, urlencode(q), ""))


def dedupe_key(url: str) -> str:
    """Identity used to detect 'same page' (ignores a trailing slash, which is only safe for dedupe, not fetching)."""
    n = normalize_url(url)
    p = urlsplit(n)
    path = p.path.rstrip("/") or "/"
    return urlunsplit((p.scheme, p.netloc, path, p.query, ""))


def registrable_like(host: str) -> str:
    """Cheap eTLD+1 approximation (no public-suffix dependency): last two labels, three for
    common second-level suffixes like co.uk."""
    parts = host.lower().split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "gov", "ac", "edu"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


class SafeFetcher:
    def __init__(self, settings: Settings,
                 resolver: Callable[..., list] | None = None, transport: httpx.BaseTransport | None = None):
        self.s = settings
        self._resolve = resolver or socket.getaddrinfo
        self._transport = transport          # test hook (httpx.MockTransport)

    # ----------------------------------------------------------- validation
    def validate(self, url: str) -> tuple[str, str, int, list[str]]:
        """Returns (scheme, host, port, [validated public ips])."""
        url = (url or "").strip()
        if not url or len(url) > 2048 or any(c.isspace() or ord(c) < 32 for c in url):
            raise UnsafeURLError("That doesn't look like a valid URL.", code="invalid_url")
        p = urlsplit(url)
        if p.scheme.lower() not in ("http", "https"):
            raise UnsafeURLError("Only http:// and https:// URLs are supported.", code="invalid_url")
        if p.username or p.password:
            raise UnsafeURLError("URLs containing credentials are not allowed.", code="blocked_url")
        host = (p.hostname or "").lower().rstrip(".")
        if not host:
            raise UnsafeURLError("That doesn't look like a valid URL.", code="invalid_url")
        try:
            port = p.port or (443 if p.scheme.lower() == "https" else 80)
        except ValueError:
            raise UnsafeURLError("That doesn't look like a valid URL.", code="invalid_url")
        scheme = p.scheme.lower()
        if self.s.allow_private_network:  # tests only
            return scheme, host, port, []
        if port not in self.s.allowed_ports:
            raise UnsafeURLError("That port is not allowed.", code="blocked_url")
        if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_HOST_SUFFIXES):
            raise UnsafeURLError("That address points to a private or internal network and can't be fetched.",
                                 code="blocked_url")
        # literal IP?
        literal = None
        try:
            literal = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            pass
        ips: list[str] = []
        if literal is None and _NUMERIC_HOST.match(host):
            # Integer / hex / octal / short-dotted IPv4 spellings (2130706433, 0x7f000001, 017700000001, 127.1).
            # Different OS resolvers interpret these differently (Linux: loopback; some Windows/ISP resolvers:
            # a public address), and no real public hostname is made only of numeric labels. Refuse outright
            # instead of depending on the platform's resolver.
            raise UnsafeURLError("Numeric IP address shortcuts aren't allowed. Use the site's normal address.",
                                 code="blocked_url")
        if literal is not None:
            ips = [str(literal)]
        else:
            try:
                infos = self._resolve(host, port, type=socket.SOCK_STREAM)
            except socket.gaierror:
                raise AppError("Couldn't find that website. Please check the address.", code="dns_failed")
            ips = list(dict.fromkeys(i[4][0] for i in infos))
        if not ips:
            raise AppError("Couldn't find that website. Please check the address.", code="dns_failed")
        for ip in ips:
            if not _addr_is_public(ipaddress.ip_address(ip.split("%")[0])):
                raise UnsafeURLError("That address points to a private or internal network and can't be fetched.",
                                     code="blocked_url")
        return scheme, host, port, ips

    def is_safe(self, url: str) -> bool:
        try:
            self.validate(url)
            return True
        except AppError:
            return False

    # ----------------------------------------------------------- fetching
    def get(self, url: str, *, max_bytes: int | None = None, accept: str = "text/html,application/xhtml+xml,*/*;q=0.5",
            allowed_types: Iterable[str] | None = None, timeout: float | None = None,
            headers: dict | None = None) -> FetchResult:
        max_bytes = max_bytes or self.s.fetch_max_bytes
        timeout = timeout or self.s.fetch_timeout_s
        deadline = time.time() + timeout * 2
        chain: list[str] = []
        cur = url.strip()
        t0 = time.time()
        for hop in range(self.s.fetch_max_redirects + 1):
            scheme, host, port, ips = self.validate(cur)
            res = self._once(cur, scheme, host, port, ips, max_bytes, accept, timeout, deadline, headers or {},
                             allowed_types)
            if res.status in _REDIRECT and res.headers.get("location"):
                nxt = urljoin(cur, res.headers["location"])
                if scheme == "https" and urlsplit(nxt).scheme.lower() == "http":
                    raise UnsafeURLError("The website redirected to an insecure (http) address.", code="blocked_url")
                chain.append(nxt)
                cur = nxt
                continue
            res.redirects = chain
            res.url = cur
            res.elapsed_s = time.time() - t0
            return res
        raise AppError("The website redirected too many times.", code="too_many_redirects")

    def _once(self, url, scheme, host, port, ips, max_bytes, accept, timeout, deadline, extra_headers,
              allowed_types) -> FetchResult:
        p = urlsplit(url)
        path = (p.path or "/") + (f"?{p.query}" if p.query else "")
        hdrs = {"User-Agent": self.s.user_agent, "Accept": accept, "Accept-Language": "en;q=0.9,*;q=0.5",
                "Accept-Encoding": "gzip, deflate"}
        hdrs.update(extra_headers)
        ext: dict = {}
        pinned = bool(ips) and not self.s.outbound_proxy and not self.s.allow_private_network
        if pinned:
            ip = ips[0]
            netloc = f"[{ip}]" if ":" in ip else ip
            default = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
            target = f"{scheme}://{netloc}{'' if default else f':{port}'}{path}"
            hdrs["Host"] = host if default else f"{host}:{port}"
            if scheme == "https":
                ext["sni_hostname"] = host
        else:
            target = url
        client_kwargs: dict = dict(follow_redirects=False, trust_env=False, verify=True,
                                   timeout=httpx.Timeout(timeout, connect=min(timeout, 10)),
                                   limits=httpx.Limits(max_connections=2))
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        elif self.s.outbound_proxy:
            client_kwargs["proxy"] = self.s.outbound_proxy
        try:
            with httpx.Client(**client_kwargs) as client:
                with client.stream("GET", target, headers=hdrs, extensions=ext) as r:
                    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    if r.status_code in _REDIRECT:
                        return FetchResult(url, r.status_code, b"", ctype, dict(r.headers))
                    if r.status_code >= 400:
                        raise _http_error(r.status_code, url)
                    if allowed_types is not None and ctype and not any(
                            ctype == t or (t.endswith("/*") and ctype.startswith(t[:-1])) for t in allowed_types):
                        raise AppError(f"This URL serves {ctype or 'an unsupported content type'}, which can't be "
                                       f"processed.", code="unsupported_content_type", detail={"content_type": ctype})
                    clen = r.headers.get("content-length")
                    if clen and clen.isdigit() and int(clen) > max_bytes:
                        raise AppError("That page is too large to process.", code="too_large")
                    buf = bytearray()
                    truncated = False
                    for chunk in r.iter_bytes():
                        buf.extend(chunk)
                        if len(buf) > max_bytes:
                            raise AppError("That page is too large to process.", code="too_large")
                        if time.time() > deadline:
                            raise AppError("The website is too slow to respond.", code="timeout")
                    return FetchResult(url, r.status_code, bytes(buf), ctype, dict(r.headers), truncated=truncated)
        except AppError:
            raise
        except httpx.TimeoutException:
            raise AppError("The website took too long to respond.", code="timeout")
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError, httpx.UnsupportedProtocol) as e:
            log.info("connect failure for %s: %s", host, type(e).__name__)
            raise AppError("Couldn't connect to that website.", code="connection_failed")
        except httpx.HTTPError as e:
            log.info("http failure for %s: %s", host, type(e).__name__)
            raise AppError("Couldn't retrieve that page.", code="fetch_failed")


def _http_error(status: int, url: str) -> AppError:
    if status in (401, 407):
        return AppError("This page requires a login and can't be accessed publicly.", code="login_required",
                        detail={"status": status})
    if status == 402:
        return AppError("This page is behind a paywall and can't be accessed.", code="paywalled",
                        detail={"status": status})
    if status == 403:
        return AppError("This website blocks automated access to this page. TalkDox does not bypass access "
                        "controls - try downloading the page as a PDF and uploading it.", code="blocked_by_site",
                        detail={"status": status})
    if status in (404, 410):
        return AppError("Page not found. Please check the URL.", code="not_found", detail={"status": status})
    if status == 429:
        return AppError("The website is rate-limiting requests. Please try again later.", code="site_rate_limited",
                        detail={"status": status})
    if status >= 500:
        return AppError(f"The website returned a server error ({status}).", code="site_error",
                        detail={"status": status})
    return AppError(f"The website returned an error ({status}).", code="http_error", detail={"status": status})
