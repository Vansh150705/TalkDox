"""SSRF defences. Uses STRICT settings (allow_private_network=False), a scripted resolver and an httpx
MockTransport so no real network is touched and DNS answers can change between calls (rebinding)."""
from __future__ import annotations

import gzip
import socket

import httpx
import pytest

from app.config import Settings, finalize
from app.errors import AppError
from app.net import SafeFetcher, normalize_url


@pytest.fixture()
def strict(tmp_path):
    return finalize(Settings(env="test", data_dir=tmp_path / "d", allow_private_network=False))


def resolver(mapping):
    calls = []

    def fn(host, port, type=0, **kw):
        calls.append(host)
        if host not in mapping:
            try:                                   # emulate libc: "127.1", "0x7f000001", "2130706433" ...
                ans = [socket.inet_ntoa(socket.inet_aton(host))]
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ans[0], port))]
            except OSError:
                raise socket.gaierror("unknown host")
        ans = mapping[host]
        if isinstance(ans, list) and ans and isinstance(ans[0], list):   # successive answers (rebinding)
            ans = ans[min(len(calls) - 1, len(ans) - 1)]
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ans]
    fn.calls = calls
    return fn


BLOCKED = [
    "http://127.0.0.1/", "http://127.1/", "http://localhost/", "http://LOCALHOST:80/", "http://0.0.0.0/",
    "http://169.254.169.254/latest/meta-data/", "http://[fd00:ec2::254]/", "http://metadata.google.internal/",
    "http://10.0.0.5/", "http://172.16.3.4/", "http://192.168.1.1/", "http://100.64.0.1/",
    "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://[::ffff:7f00:1]/", "http://[64:ff9b::7f00:1]/",
    "http://2130706433/", "http://0x7f000001/", "http://017700000001/", "http://0177.0.0.1/",
    "http://foo.internal/", "http://printer.local/", "http://intranet.corp/",
    "http://user:pass@example.com/", "ftp://example.com/", "file:///etc/passwd", "gopher://example.com/",
    "javascript:alert(1)", "http://example.com:22/", "http://example.com:8080/", "http://example.com:6379/",
    "", "   ", "http://", "http://exa mple.com/", "http://example.com/\r\nHost: evil",
]


@pytest.mark.parametrize("url", BLOCKED)
def test_blocked_urls(strict, url):
    f = SafeFetcher(strict, resolver=resolver({"example.com": ["93.184.216.34"]}))
    with pytest.raises(AppError) as e:
        f.validate(url)
    assert e.value.code in ("blocked_url", "invalid_url", "dns_failed")


def test_public_url_allowed(strict):
    f = SafeFetcher(strict, resolver=resolver({"example.com": ["93.184.216.34"]}))
    assert f.validate("https://example.com/path?q=1")[1:3] == ("example.com", 443)


def test_hostname_resolving_to_private_ip_blocked(strict):
    f = SafeFetcher(strict, resolver=resolver({"evil.example": ["10.1.2.3"]}))
    with pytest.raises(AppError) as e:
        f.validate("http://evil.example/")
    assert e.value.code == "blocked_url"


def test_mixed_public_and_private_answers_blocked(strict):
    f = SafeFetcher(strict, resolver=resolver({"mixed.example": ["93.184.216.34", "127.0.0.1"]}))
    with pytest.raises(AppError):
        f.validate("http://mixed.example/")


def _fetcher(strict, mapping, handler):
    return SafeFetcher(strict, resolver=resolver(mapping), transport=httpx.MockTransport(handler))


def test_connection_is_pinned_to_validated_ip_and_resolved_once(strict, monkeypatch):
    """DNS rebinding: the resolver answers public first, then private. We must connect to the first
    (validated) address and never look the name up again for that hop."""
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["host"] = req.headers["host"]
        seen["sni"] = req.extensions.get("sni_hostname")
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>ok</html>")
    r = resolver({"rebind.example": [["93.184.216.34"], ["127.0.0.1"]]})
    f = SafeFetcher(strict, resolver=r, transport=httpx.MockTransport(handler))
    res = f.get("https://rebind.example/page?x=1")
    assert res.status == 200
    assert seen["url"].startswith("https://93.184.216.34/page")      # connected by IP, not by name
    assert seen["host"] == "rebind.example" and seen["sni"] == "rebind.example"
    assert r.calls == ["rebind.example"]                              # resolved exactly once


def test_redirect_to_metadata_endpoint_blocked(strict):
    def handler(req):
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
    f = _fetcher(strict, {"example.com": ["93.184.216.34"]}, handler)
    with pytest.raises(AppError) as e:
        f.get("http://example.com/")
    assert e.value.code == "blocked_url"


def test_redirect_to_hostname_that_resolves_private_blocked(strict):
    def handler(req):
        return httpx.Response(301, headers={"location": "http://sneaky.example/admin"})
    f = _fetcher(strict, {"example.com": ["93.184.216.34"], "sneaky.example": ["192.168.0.10"]}, handler)
    with pytest.raises(AppError) as e:
        f.get("http://example.com/")
    assert e.value.code == "blocked_url"


def test_https_to_http_downgrade_blocked(strict):
    f = _fetcher(strict, {"example.com": ["93.184.216.34"]},
                 lambda req: httpx.Response(302, headers={"location": "http://example.com/"}))
    with pytest.raises(AppError) as e:
        f.get("https://example.com/")
    assert e.value.code == "blocked_url"


def test_redirect_loop_limited(strict):
    f = _fetcher(strict, {"example.com": ["93.184.216.34"]},
                 lambda req: httpx.Response(302, headers={"location": "/loop"}))
    with pytest.raises(AppError) as e:
        f.get("http://example.com/loop")
    assert e.value.code == "too_many_redirects"


def test_redirects_followed_when_safe(strict):
    def handler(req):
        if req.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>final</p>")
    f = _fetcher(strict, {"example.com": ["93.184.216.34"]}, handler)
    res = f.get("http://example.com/old")
    assert res.url == "http://example.com/new" and res.redirects == ["http://example.com/new"]


def test_response_size_limit(strict):
    f = _fetcher(strict, {"example.com": ["93.184.216.34"]},
                 lambda req: httpx.Response(200, headers={"content-type": "text/html"}, content=b"a" * 3_000_000))
    with pytest.raises(AppError) as e:
        f.get("http://example.com/", max_bytes=1_000_000)
    assert e.value.code == "too_large"


def test_gzip_bomb_limited_by_decoded_size(strict):
    bomb = gzip.compress(b"0" * 40_000_000)
    f = _fetcher(strict, {"example.com": ["93.184.216.34"]},
                 lambda req: httpx.Response(200, headers={"content-type": "text/html", "content-encoding": "gzip"},
                                            content=bomb))
    assert len(bomb) < 100_000
    with pytest.raises(AppError) as e:
        f.get("http://example.com/", max_bytes=1_000_000)
    assert e.value.code == "too_large"


def test_disallowed_content_type(strict):
    f = _fetcher(strict, {"example.com": ["93.184.216.34"]},
                 lambda req: httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG"))
    with pytest.raises(AppError) as e:
        f.get("http://example.com/x.png", allowed_types=["text/html"])
    assert e.value.code == "unsupported_content_type"


@pytest.mark.parametrize("status,code", [(401, "login_required"), (402, "paywalled"), (403, "blocked_by_site"),
                                         (404, "not_found"), (429, "site_rate_limited"), (503, "site_error")])
def test_http_errors_are_classified(strict, status, code):
    f = _fetcher(strict, {"example.com": ["93.184.216.34"]}, lambda req: httpx.Response(status, text="no"))
    with pytest.raises(AppError) as e:
        f.get("http://example.com/")
    assert e.value.code == code


def test_no_credentials_or_cookies_forwarded(strict):
    seen = {}

    def handler(req):
        seen.update(req.headers)
        return httpx.Response(200, headers={"content-type": "text/html", "set-cookie": "a=b"}, text="x")
    f = _fetcher(strict, {"example.com": ["93.184.216.34"]}, handler)
    f.get("http://example.com/")
    f.get("http://example.com/")
    assert "cookie" not in seen and "authorization" not in seen
    assert "talkdoxbot" in seen["user-agent"].lower()          # honest, identifiable user agent


def test_normalize_url():
    assert normalize_url("HTTP://Example.COM:80/a//b/?utm_source=x&b=2&a=1#frag") == "http://example.com/a//b/?a=1&b=2"
    assert normalize_url("https://example.com") == "https://example.com/"
    assert normalize_url("../x", base="https://example.com/a/b/c") == "https://example.com/a/x"
    from app.net import dedupe_key
    assert dedupe_key("https://example.com/docs/") == dedupe_key("https://example.com/docs?utm_source=x")
    assert normalize_url("https://example.com/p?fbclid=abc&id=3") == "https://example.com/p?id=3"


def test_api_rejects_private_urls_synchronously(client, auth):
    """The API validates before queueing, using the *strict* guard (not the test-relaxed one)."""
    strict = SafeFetcher(finalize(Settings(env="test", data_dir=client.state.settings.data_dir)))
    client.state.ingest.fetcher = strict
    for u in ["http://127.0.0.1:80/", "http://169.254.169.254/", "http://localhost/admin"]:
        r = client.post("/api/sources/web", json={"urls": [u]}, headers=auth)
        assert r.status_code == 400 and r.json()["code"] == "blocked_url", (u, r.text)
    assert client.get("/api/sources", headers=auth).json()["sources"] == []


@pytest.mark.parametrize("url", ["http://127.1/", "http://2130706433/", "http://0x7f000001/", "http://017700000001/",
                                 "http://0177.0.0.1/"])
def test_numeric_obfuscation_blocked_with_real_resolver(strict, url):
    with pytest.raises(AppError) as e:
        SafeFetcher(strict).validate(url)      # real socket.getaddrinfo, no network needed for numeric hosts
    assert e.value.code in ("blocked_url", "dns_failed")
