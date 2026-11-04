from __future__ import annotations

import time

import pytest

from conftest import wait_job

LOREM = ("This paragraph contains substantive documentation text that explains how the system behaves in detail, "
         "including configuration, limits and examples. ")


def page(title, body, extra_head="", nav=""):
    return (f"<html><head><title>{title}</title>{extra_head}</head><body>"
            f"<header><nav>{nav or '<a href=/>Home</a> <a href=/pricing>Pricing</a> <a href=/login>Login</a>'}</nav></header>"
            f"<main><article>{body}</article></main>"
            f"<aside class='ad advert'>BUY CHEAP WIDGETS NOW sponsored</aside>"
            f"<footer>Copyright 2020 Acme Corp. All rights reserved. Privacy Terms Cookies</footer></body></html>")


def add(client, auth, url, **kw):
    r = client.post("/api/sources/web", json={"urls": [url], **kw}, headers=auth)
    assert r.status_code == 200, r.text
    res = r.json()["results"][0]
    return res, wait_job(client, auth, res["job_id"])


def chunks(client, auth, sid, **p):
    return client.get(f"/api/sources/{sid}/content", params={"limit_": 100, **p}, headers=auth).json()["chunks"]


def src(client, auth, sid):
    return client.get(f"/api/sources/{sid}", headers=auth).json()


@pytest.fixture(autouse=True)
def deep_crawls(client):
    client.state.settings.crawl_max_depth = 5
    yield


@pytest.fixture(autouse=True)
def clean_server(web):
    web.routes.clear()
    web.hits.clear()
    web.text("/robots.txt", "User-agent: *\nAllow: /\n")


def test_article_boilerplate_removed(client, auth, web):
    body = "<h1>How caching works</h1>" + "".join(f"<p>{LOREM}Section {i} specific detail zebra{i}.</p>" for i in range(6))
    web.html("/post", page("How caching works - Acme Blog", body))
    res, job = add(client, auth, web.base + "/post")
    assert job["status"] == "completed"
    s = src(client, auth, res["source_id"])
    assert "caching" in s["name"].lower() and s["uri"].endswith("/post")
    text = " ".join(c["text"] for c in chunks(client, auth, res["source_id"]))
    assert "zebra3" in text
    for junk in ("BUY CHEAP", "Copyright 2020", "Login", "Pricing"):
        assert junk not in text


def test_code_tables_lists_preserved(client, auth, web):
    body = ("<h1>Install</h1><p>" + LOREM * 2 + "</p><h2>Steps</h2><ol><li>Download the package</li><li>Run the installer</li></ol>"
            "<pre><code>pip install acme --upgrade\nacme init --force</code></pre>"
            "<table><tr><th>Flag</th><th>Meaning</th></tr><tr><td>--force</td><td>overwrite existing config</td></tr></table>")
    web.html("/install", page("Install", body))
    res, job = add(client, auth, web.base + "/install")
    ch = chunks(client, auth, res["source_id"])
    assert any(c["kind"] == "code" and "acme init --force" in c["text"] for c in ch)
    assert any(c["kind"] == "table" and "| --force | overwrite existing config |" in c["text"] for c in ch)
    assert any("Run the installer" in c["text"] for c in ch)
    assert all(c["url"] == web.base + "/install" for c in ch)


def test_long_documentation_page_chunks_with_heading_paths(client, auth, web):
    parts = []
    for i in range(12):
        parts.append(f"<h2>Chapter {i}</h2>" + "".join(f"<p>{LOREM} Fact {i}.{j} is quokka{i}{j}.</p>" for j in range(8)))
    web.html("/long", page("Handbook", "<h1>Handbook</h1>" + "".join(parts)))
    res, job = add(client, auth, web.base + "/long")
    ch = chunks(client, auth, res["source_id"])
    assert len(ch) > 20
    target = next(c for c in ch if "quokka73" in c["text"])
    assert "Chapter 7" in target["heading"]


def test_malformed_html(client, auth, web):
    html = ("<html><head><title>Broken</title></head><body><div><p>Unclosed paragraph " + LOREM * 3 + "<div><b>bold <i>italic</b></i>"
            "<p>Second paragraph with kiwi data " + LOREM * 2 + "<script>alert(1)</script><table><tr><td>cell")
    web.html("/broken", html)
    res, job = add(client, auth, web.base + "/broken")
    assert job["status"] == "completed"
    text = " ".join(c["text"] for c in chunks(client, auth, res["source_id"]))
    assert "kiwi data" in text and "alert(1)" not in text


def test_non_utf8_page(client, auth, web):
    body = "<h1>Café</h1><p>" + ("Le café résumé naïve façade. " * 12) + "</p>"
    web.routes["/latin"] = (200, {"Content-Type": "text/html; charset=iso-8859-1"},
                            page("Café", body).encode("latin-1"))
    res, job = add(client, auth, web.base + "/latin")
    assert "résumé" in " ".join(c["text"] for c in chunks(client, auth, res["source_id"]))


@pytest.mark.parametrize("status,code", [(404, "not_found"), (403, "blocked_by_site"), (401, "login_required"),
                                         (429, "site_rate_limited"), (500, "site_error")])
def test_inaccessible_pages_report_reason(client, auth, web, status, code):
    web.html("/bad", "nope", status=status)
    res, job = add(client, auth, web.base + "/bad")
    assert job["status"] == "failed" and job["error_code"] == code
    assert client.get("/api/sources", headers=auth).json()["sources"][0]["n_chunks"] == 0


def test_empty_page_and_blocked_placeholder(client, auth, web):
    web.html("/empty", "<html><body></body></html>")
    res, job = add(client, auth, web.base + "/empty")
    assert job["status"] == "failed" and job["error_code"] in ("empty_page", "needs_js")
    web.html("/captcha", "<html><body><h1>Just a moment...</h1><p>Checking your browser</p></body></html>")
    res, job = add(client, auth, web.base + "/captcha")
    assert job["status"] == "failed"      # too little real content -> never indexed as if it were the page


def test_js_only_page_explained_when_rendering_disabled(client, auth, web):
    web.html("/spa", "<html><head><title>App</title></head><body><div id='root'></div><noscript>You need to enable "
                     "JavaScript to run this app.</noscript><script src='/app.js'></script></body></html>")
    res, job = add(client, auth, web.base + "/spa")
    assert job["status"] == "failed" and job["error_code"] == "needs_js"
    assert "JavaScript" in job["error"]


def test_js_rendered_page_with_browser(client, auth, web):
    pw = pytest.importorskip("playwright.sync_api")
    import os
    os.environ.setdefault("CHROMIUM_PATH", "/opt/pw-browsers/chromium-1243/chrome-linux64/chrome")
    if not os.path.exists(os.environ["CHROMIUM_PATH"]):
        pytest.skip("chromium not available")
    client.state.settings.enable_js_render = True
    web.html("/spa2", "<html><head><title>Dyn</title></head><body><div id='root'></div><script>"
             "document.getElementById('root').innerHTML='<h1>Rendered title</h1><p>' + 'Dynamic content paragraph about "
             "mangoes and their cultivation in tropical regions. '.repeat(6) + '</p>';</script></body></html>")
    res, job = add(client, auth, web.base + "/spa2")
    assert job["status"] == "completed", job
    s = src(client, auth, res["source_id"])
    assert s["diagnostics"]["pages"][0]["via"] == "js"
    assert "mangoes" in " ".join(c["text"] for c in chunks(client, auth, res["source_id"]))


def test_redirect_followed_and_recorded(client, auth, web):
    web.routes["/old"] = (301, {"Location": "/new"}, "")
    web.html("/new", page("New page", "<h1>New</h1><p>" + LOREM * 4 + "</p>"))
    res, job = add(client, auth, web.base + "/old")
    assert job["status"] == "completed"
    s = src(client, auth, res["source_id"])
    assert s["diagnostics"]["pages"][0]["url"].endswith("/new")


def test_non_html_content_type_rejected(client, auth, web):
    web.routes["/img"] = (200, {"Content-Type": "image/png"}, b"\x89PNG\r\n\x1a\n")
    res, job = add(client, auth, web.base + "/img")
    assert job["error_code"] == "unsupported_content_type"


def test_pdf_url_is_extracted(client, auth, web, tmp_path):
    from factories import make_pdf
    p = make_pdf(tmp_path / "w.pdf", [["Whitepaper", "The reactor operates at four hundred degrees under nominal load."]])
    web.routes["/paper.pdf"] = (200, {"Content-Type": "application/pdf"}, p.read_bytes())
    res, job = add(client, auth, web.base + "/paper.pdf")
    assert job["status"] == "completed"
    assert "four hundred degrees" in " ".join(c["text"] for c in chunks(client, auth, res["source_id"]))


def test_robots_txt_respected(client, auth, web):
    web.text("/robots.txt", "User-agent: *\nDisallow: /private/\n")
    web.html("/private/secret", page("Secret", "<h1>Secret</h1><p>" + LOREM * 4 + "</p>"))
    res, job = add(client, auth, web.base + "/private/secret")
    assert job["status"] == "failed" and job["error_code"] == "robots_disallowed"
    assert "/private/secret" not in web.hits                       # never even requested


def test_robots_unreachable_is_conservative(client, auth, web):
    web.html("/robots.txt", "boom", status=503)
    web.html("/p", page("P", "<h1>P</h1><p>" + LOREM * 4 + "</p>"))
    res, job = add(client, auth, web.base + "/p")
    assert job["status"] == "failed" and job["error_code"] == "robots_disallowed"


def test_missing_robots_means_allowed(client, auth, web):
    web.routes.pop("/robots.txt", None)
    web.html("/p", page("P", "<h1>P</h1><p>" + LOREM * 4 + "</p>"))
    res, job = add(client, auth, web.base + "/p")
    assert job["status"] == "completed"


def build_docs_site(web):
    pass
    nav = "<a href=/docs/>Docs</a>"
    web.html("/docs/", page("Docs home", "<h1>Docs home</h1><p>" + LOREM * 3 + "</p><a href='/docs/a'>A</a> "
             "<a href='/docs/b/'>B</a> <a href='/blog/post'>Blog</a> <a href='https://other.example/x'>ext</a> "
             "<a href='/docs/a?utm_source=news'>A again</a> <a href='/docs/a/'>A slash</a>", nav=nav))
    web.html("/docs/a", page("Topic A", "<h1>Topic A</h1><p>" + LOREM * 3 + " Alpha facts: ocelot.</p><a href='/docs/b/c'>C</a>",
                              extra_head="<link rel='canonical' href='/docs/a'>"))
    web.html("/docs/b/", page("Topic B", "<h1>Topic B</h1><p>" + LOREM * 3 + " Bravo facts: narwhal.</p><a href='/docs/b/c'>C</a>"))
    web.html("/docs/b/c", page("Topic C", "<h1>Topic C</h1><p>" + LOREM * 3 + " Charlie facts: pangolin.</p><a href='/docs/d'>D</a>"))
    web.html("/docs/d", page("Topic D", "<h1>Topic D</h1><p>" + LOREM * 3 + " Delta facts: axolotl.</p>"))
    web.html("/docs/hidden", page("Hidden", "<h1>Only in sitemap</h1><p>" + LOREM * 3 + " Echo facts: tapir.</p>"))
    web.html("/blog/post", page("Blog", "<h1>Blog</h1><p>" + LOREM * 3 + " outside prefix.</p>"))
    web.text("/sitemap.xml", f"<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'><url><loc>{web.base}/docs/hidden</loc></url>"
                             f"<url><loc>{web.base}/blog/post</loc></url></urlset>", "application/xml")


def test_crawl_nested_docs_with_dedupe_scope_and_sitemap(client, auth, web):
    build_docs_site(web)
    res, job = add(client, auth, web.base + "/docs/", crawl=True, max_depth=3, max_pages=20)
    assert job["status"] in ("completed", "partial"), job
    s = src(client, auth, res["source_id"])
    urls = [p["url"] for p in s["diagnostics"]["pages"] if p["status"] == "ok"]
    assert sorted(u.replace(web.base, "").rstrip("/") for u in urls) == ["/docs", "/docs/a", "/docs/b", "/docs/b/c", "/docs/d",
                                                             "/docs/hidden"]
    assert not any("/blog" in u for u in urls)            # outside default path scope
    assert not any("other.example" in h for h in web.hits)
    assert len(urls) == len(set(urls))                    # utm / trailing-slash variants deduplicated
    assert web.hits.count("/docs/a") <= 1
    ch = chunks(client, auth, res["source_id"])
    pango = next(c for c in ch if "pangolin" in c["text"])
    assert pango["url"].endswith("/docs/b/c") and pango["heading"].startswith("Topic C")   # page attribution
    assert "ocelot" in " ".join(c["text"] for c in ch) and "tapir" in " ".join(c["text"] for c in ch)


def test_crawl_depth_and_page_limits(client, auth, web):
    build_docs_site(web)
    res, job = add(client, auth, web.base + "/docs/", crawl=True, max_depth=1, max_pages=20, use_sitemap=False)
    ok = {p["url"].replace(web.base, "").rstrip("/") for p in src(client, auth, res["source_id"])["diagnostics"]["pages"] if p["status"] == "ok"}
    assert ok == {"/docs", "/docs/a", "/docs/b"}
    res2, job2 = add(client, auth, web.base + "/docs/", crawl=True, max_depth=5, max_pages=3, use_sitemap=False)
    assert src(client, auth, res2["source_id"])["diagnostics"]["pages_ok"] <= 3


def test_crawl_partial_when_some_pages_fail(client, auth, web):
    build_docs_site(web)
    web.html("/docs/b/c", "gone", status=404)
    res, job = add(client, auth, web.base + "/docs/", crawl=True, max_depth=3, max_pages=20, use_sitemap=False)
    assert job["status"] == "partial"
    s = src(client, auth, res["source_id"])
    assert s["diagnostics"]["pages_failed"] >= 1 and any("not found" in w.lower() for w in s["warnings"])


def test_duplicate_url_submission_deduplicated(client, auth, web):
    web.html("/p", page("P", "<h1>P</h1><p>" + LOREM * 4 + "</p>"))
    r1, _ = add(client, auth, web.base + "/p")
    r2 = client.post("/api/sources/web", json={"urls": [web.base + "/p?utm_campaign=x#frag"]}, headers=auth).json()["results"][0]
    assert r2["duplicate"] is True and r2["source_id"] == r1["source_id"]


def test_same_content_under_two_urls_collapsed_in_crawl(client, auth, web):
    body = "<h1>Same</h1><p>" + LOREM * 4 + "</p>"
    web.html("/docs/", page("Home", "<h1>Home</h1><p>" + LOREM * 3 + "</p><a href='/docs/x'>x</a><a href='/docs/y'>y</a>"))
    web.html("/docs/x", page("Same", body))
    web.html("/docs/y", page("Same", body))
    res, job = add(client, auth, web.base + "/docs/", crawl=True, max_depth=2, max_pages=10, use_sitemap=False)
    s = src(client, auth, res["source_id"])
    assert s["diagnostics"]["pages_ok"] == 2
    assert any(p["code"] == "duplicate" for p in s["diagnostics"]["pages"])


def test_collection_of_urls(client, auth, web):
    for i in range(3):
        web.html(f"/c{i}", page(f"C{i}", f"<h1>C{i}</h1><p>{LOREM * 3} marker{i}.</p>"))
    r = client.post("/api/sources/web", json={"urls": [f"{web.base}/c{i}" for i in range(3)], "combine": True}, headers=auth)
    res = r.json()["results"]
    assert len(res) == 1
    job = wait_job(client, auth, res[0]["job_id"])
    assert job["status"] == "completed"
    s = src(client, auth, res[0]["source_id"])
    assert s["n_pages"] == 3
    r = client.post("/api/sources/web", json={"urls": [f"{web.base}/c{i}" for i in range(3)]}, headers=auth)
    assert len(r.json()["results"]) == 3                         # separate sources by default


def test_refresh_incremental_reindex_detects_change_and_reuses_embeddings(client, auth, web):
    body = "<h1>Policy</h1>" + "".join(f"<p>{LOREM} Rule {i} is lemur{i}.</p>" for i in range(10))
    web.html("/policy", page("Policy", body))
    res, job = add(client, auth, web.base + "/policy")
    sid = res["source_id"]
    first = src(client, auth, sid)
    changed = body.replace("lemur9", "gecko9")
    web.html("/policy", page("Policy", changed))
    time.sleep(0.01)
    r = client.post(f"/api/sources/{sid}/refresh", headers=auth).json()
    job = wait_job(client, auth, r["job_id"])
    assert job["status"] == "completed"
    s = src(client, auth, sid)
    assert s["diagnostics"]["changed_since_last_index"] is True
    assert s["diagnostics"]["embeddings_reused"] >= 1                 # unchanged chunks not re-embedded
    assert "gecko9" in " ".join(c["text"] for c in chunks(client, auth, sid))


def test_refresh_failure_keeps_previous_index(client, auth, web):
    web.html("/live", page("Live", "<h1>Live</h1><p>" + LOREM * 4 + " original wombat.</p>"))
    res, _ = add(client, auth, web.base + "/live")
    sid = res["source_id"]
    web.html("/live", "gone", status=404)
    job = wait_job(client, auth, client.post(f"/api/sources/{sid}/refresh", headers=auth).json()["job_id"])
    assert job["status"] == "failed"
    s = src(client, auth, sid)
    assert s["status"] == "partial" and "Refresh failed" in s["error"]
    assert "wombat" in " ".join(c["text"] for c in chunks(client, auth, sid))     # still answerable from old copy


def test_connection_refused_reported(client, auth):
    res, job = add(client, auth, "http://127.0.0.1:9/")
    assert job["status"] == "failed" and job["error_code"] in ("connection_failed", "robots_disallowed")


def test_slow_server_times_out(client, auth, web):
    client.state.settings.fetch_timeout_s = 0.5
    client.state.settings.respect_robots = False

    def slow(h):
        time.sleep(1.6)
        return (200, {"Content-Type": "text/html"}, "late")
    web.routes["/slow"] = slow
    res, job = add(client, auth, web.base + "/slow")
    assert job["status"] == "failed" and job["error_code"] == "timeout"
