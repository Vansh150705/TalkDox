from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("TALKDOX_ENV", "test")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings, finalize  # noqa: E402
from app.main import create_app  # noqa: E402
from app.net import SafeFetcher  # noqa: E402
from app.providers.embeddings import HashEmbedder  # noqa: E402
from app.providers.llm import FakeLLM  # noqa: E402
from app.providers.ocr import FakeOCR  # noqa: E402
from app.rag.retrieval import _stems  # noqa: E402
from app.state import build_state  # noqa: E402


# ------------------------------------------------------------------ deterministic "reader" LLM (test double)
def _words(s: str) -> set[str]:
    return set(_stems(s))


EV = re.compile(r'<evidence id="(S\d+)"[^>]*>\n(.*?)\n</evidence>', re.S)


def extractive_handler(prompt: str, system, schema) -> str:
    """Stand-in for an LLM: answers by quoting the best-matching evidence sentences with [S#] citations.
    It exercises retrieval, prompting, structured output, verification and citation resolution
    end-to-end, but says NOTHING about real-model faithfulness (see docs/EVALUATION.md)."""
    name = getattr(schema, "__name__", "")
    if name == "RewriteOut":
        m = re.search(r"Latest message: (.*?)\n\nReturn", prompt, re.S)
        q = (m.group(1) if m else "").strip()
        hist = re.search(r"Conversation \(context only\):\n(.*?)\n\nLatest", prompt, re.S)
        last_user = [l[6:] for l in (hist.group(1) if hist else "").splitlines() if l.startswith("User: ")]
        standalone = q
        if last_user and re.search(r"\b(it|that|this|they|them|its|their|those|these)\b", q, re.I):
            standalone = f"{last_user[-1]} {q}"
        return json.dumps({"standalone": standalone, "variants": []})
    if name == "AnswerOut":
        q = re.search(r"User question: (.*?)(?:\n\(Interpreted as: (.*?)\))?(?:\n\n|\Z)", prompt, re.S)
        question = ((q.group(2) or q.group(1)) if q else "").strip()
        qw = _words(question)
        best = []
        for sid, text in EV.findall(prompt):
            units = []
            lines = text.split("\n")
            if len(lines) > 2 and lines[0].startswith("|") and lines[1].startswith("| ---"):
                units = [lines[0] + "\n" + ln for ln in lines[2:] if ln.startswith("|")]    # header + one row
            else:
                units = re.split(r"(?<=[.!?])\s+|\n+", text)
            for sent in units:
                sw = _words(sent)
                if len(sw) < 2:
                    continue
                score = len(qw & sw) / max(len(qw), 1)
                best.append((score, sid, sent.strip()))
        best.sort(key=lambda b: (-b[0], int(b[1][1:])))     # ties -> highest-ranked evidence first
        if not best or best[0][0] < 0.2:
            return json.dumps({"answerable": False, "answer": "The sources do not contain this information.",
                               "missing": "the requested detail", "quotes": [], "followups": []})
        chosen = [b for b in best[:3] if b[0] >= max(0.2, best[0][0] * 0.6)]
        answer = " ".join(f"{s} [{sid}]" for _, sid, s in chosen)
        quotes = [{"id": sid, "quote": " ".join(s.split()[:15])} for _, sid, s in chosen]
        return json.dumps({"answerable": True, "answer": answer, "quotes": quotes, "missing": None,
                           "followups": ["Can you give more detail?"]})
    return "{}"


# ------------------------------------------------------------------ local fixture web server
class FixtureServer:
    def __init__(self):
        self.routes: dict[str, tuple] = {}
        self.hits: list[str] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits.append(self.path)
                route = outer.routes.get(self.path.split("#")[0]) or outer.routes.get(self.path.split("?")[0])
                if route is None:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b"not found")
                    return
                if callable(route):
                    route = route(self)
                status, headers, body = route
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                if isinstance(body, str):
                    body = body.encode()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def html(self, path: str, body: str, status: int = 200, headers: dict | None = None):
        self.routes[path] = (status, {"Content-Type": "text/html; charset=utf-8", **(headers or {})}, body)

    def text(self, path: str, body: str, ctype: str = "text/plain"):
        self.routes[path] = (200, {"Content-Type": ctype}, body)

    def stop(self):
        self.httpd.shutdown()


@pytest.fixture(scope="session")
def web():
    s = FixtureServer()
    yield s
    s.stop()


# ------------------------------------------------------------------ app fixtures
@pytest.fixture()
def settings(tmp_path):
    return finalize(Settings(env="test", data_dir=tmp_path / "data", allow_private_network=True, crawl_delay_s=0.0,
                             retention_days=0, ingest_workers=2, rate_chat_per_min=1000, rate_ingest_per_hour=1000,
                             rate_tools_per_hour=1000, rate_workspace_create_per_hour=1000, respect_robots=True,
                             embedding_dim=512, ocr_engine="none", llm_max_retries=0))


@pytest.fixture()
def fake_ocr():
    return FakeOCR()


@pytest.fixture()
def llm(settings):
    return FakeLLM(settings, extractive_handler)


@pytest.fixture()
def app_state(settings, llm, fake_ocr):
    return build_state(settings, llm=llm, embedder=HashEmbedder(512), ocr=fake_ocr,
                       fetcher=SafeFetcher(settings))


@pytest.fixture()
def client(app_state):
    app = create_app(state=app_state)
    with TestClient(app) as c:
        c.state = app_state
        yield c


@pytest.fixture()
def auth(client):
    r = client.post("/api/workspaces").json()
    return {"Authorization": "Bearer " + r["token"]}


def new_workspace(client):
    return {"Authorization": "Bearer " + client.post("/api/workspaces").json()["token"]}


def wait_job(client, headers, job_id, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/api/jobs/{job_id}", headers=headers).json()
        if j["status"] in ("completed", "partial", "failed", "cancelled"):
            return j
        time.sleep(0.1)
    raise AssertionError("job did not finish: " + json.dumps(j))


def upload(client, headers, path: Path, name: str | None = None):
    with open(path, "rb") as f:
        r = client.post("/api/sources/files", files=[("files", (name or path.name, f))], headers=headers)
    return r


def ingest_file(client, headers, path: Path, name: str | None = None):
    r = upload(client, headers, path, name)
    assert r.status_code == 200, r.text
    res = r.json()["results"][0]
    assert "error" not in res, res
    job = wait_job(client, headers, res["job_id"])
    return res["source_id"], job
