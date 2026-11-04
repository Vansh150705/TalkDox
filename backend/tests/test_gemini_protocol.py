"""Exercises the REAL google-genai SDK code paths in app/providers against a local stub that speaks the Gemini REST
protocol. It verifies request construction (model, system instruction, JSON mime type, thinking config, task type,
dimensionality), response parsing, retry/backoff, error mapping, the explicit fallback model and OCR image parts.
It does NOT prove behaviour of Google's live service - that needs a real key (docs/TESTING.md, live verification)."""
from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest

from app.config import Settings, finalize
from app.errors import ProviderError, ProviderRateLimited, ProviderUnavailable
from app.providers.embeddings import GeminiEmbedder
from app.providers.llm import GeminiLLM, generate_structured
from app.providers.ocr import GeminiOCR
from app.rag.prompts import AnswerOut


class Stub:
    def __init__(self):
        self.requests: list[dict] = []
        self.script: list = []          # queue of (status, body) overrides, consumed first
        self.models_missing: set[str] = set()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body, headers=None):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.requests.append({"method": "GET", "path": self.path})
                name = self.path.split("/models/")[-1].split("?")[0]
                if name in outer.models_missing:
                    return self._send(404, {"error": {"code": 404, "message": "models/x is not found", "status": "NOT_FOUND"}})
                self._send(200, {"name": f"models/{name}", "displayName": "Stub Model"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.requests.append({"method": "POST", "path": self.path, "body": body, "key": self.headers.get("x-goog-api-key")})
                if outer.script:
                    status, resp = outer.script.pop(0)
                    return self._send(status, resp, {"Retry-After": "0"} if status == 429 else None)
                model = self.path.split("/models/")[-1].split(":")[0]
                if model in outer.models_missing:
                    return self._send(404, {"error": {"code": 404, "message": f"models/{model} is no longer available", "status": "NOT_FOUND"}})
                if ":generateContent" in self.path:
                    return self._send(200, {"candidates": [{"content": {"role": "model", "parts": [{"text": '{"answerable": true, "answer": "ok [S1]", "quotes": [], "followups": []}'}]},
                                                            "finishReason": "STOP"}],
                                            "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 7, "totalTokenCount": 18}})
                if ":batchEmbedContents" in self.path:
                    reqs = body["requests"]
                    dim = reqs[0].get("outputDimensionality", 8)
                    return self._send(200, {"embeddings": [{"values": [float(i + 1)] * dim} for i, _ in enumerate(reqs)]})
                if ":embedContent" in self.path:
                    dim = body.get("outputDimensionality", 8)
                    return self._send(200, {"embedding": {"values": [1.0] * dim}})
                self._send(404, {"error": {"code": 404, "message": "unknown", "status": "NOT_FOUND"}})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


@pytest.fixture()
def stub():
    s = Stub()
    yield s
    s.httpd.shutdown()


@pytest.fixture()
def gs(tmp_path, stub):
    return finalize(Settings(env="test", data_dir=tmp_path / "d", gemini_api_key="test-key-123", gemini_base_url=stub.url,
                             llm_max_retries=2, embedding_dim=16, llm_timeout_s=10))


def posts(stub):
    return [r for r in stub.requests if r["method"] == "POST"]


def test_generate_request_shape_and_response_parsing(gs, stub):
    llm = GeminiLLM(gs)
    res = llm.generate("hello", system="be brief", schema=AnswerOut, temperature=0.1, max_tokens=500)
    r = posts(stub)[0]
    assert r["path"].endswith("/models/gemini-2.5-flash:generateContent") and r["key"] == "test-key-123"
    body = r["body"]
    assert body["systemInstruction"]["parts"][0]["text"] == "be brief"
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["maxOutputTokens"] == 500 and body["generationConfig"]["temperature"] == 0.1
    assert res.text.startswith("{") and res.usage["total_tokens"] == 18 and res.model == "gemini-2.5-flash" and res.provider == "gemini"


def test_thinking_budget_forwarded_only_for_25_family(gs, stub):
    gs.gemini_thinking_budget = 0
    GeminiLLM(gs).generate("x")
    tc = posts(stub)[0]["body"]["generationConfig"]["thinkingConfig"]
    assert (tc.get("thinkingBudget", tc.get("thinking_budget"))) == 0          # the SDK may emit camel- or snake-case
    gs.gemini_model = "gemini-3-flash"
    GeminiLLM(gs).generate("x")
    assert "thinkingConfig" not in posts(stub)[1]["body"].get("generationConfig", {})


def test_structured_output_end_to_end(gs, stub):
    obj, res = generate_structured(GeminiLLM(gs), "Q", AnswerOut)
    assert obj.answerable and obj.answer == "ok [S1]"


def test_retries_transient_errors_then_succeeds(gs, stub, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    stub.script = [(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}),
                   (503, {"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}})]
    res = GeminiLLM(gs).generate("hi")
    assert res.text and len(posts(stub)) == 3


def test_rate_limit_exhausted_maps_to_provider_rate_limited(gs, stub, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    stub.script = [(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})] * 5
    with pytest.raises(ProviderRateLimited) as e:
        GeminiLLM(gs).generate("hi")
    assert e.value.code == "provider_rate_limited" and e.value.status == 503 and len(posts(stub)) == 3


def test_auth_and_bad_request_not_retried(gs, stub):
    stub.script = [(403, {"error": {"code": 403, "message": "API key not valid", "status": "PERMISSION_DENIED"}})]
    with pytest.raises(ProviderError) as e:
        GeminiLLM(gs).generate("hi")
    assert e.value.code == "provider_auth" and len(posts(stub)) == 1 and "API key not valid" not in e.value.message


def test_circuit_breaker_opens_after_repeated_failures(gs, stub, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    gs.llm_max_retries = 0
    llm = GeminiLLM(gs)
    stub.script = [(503, {"error": {"code": 503, "message": "down", "status": "UNAVAILABLE"}})] * 5
    for _ in range(5):
        with pytest.raises(ProviderUnavailable):
            llm.generate("hi")
    n = len(posts(stub))
    with pytest.raises(ProviderUnavailable) as e:
        llm.generate("hi")
    assert e.value.code == "provider_circuit_open" and len(posts(stub)) == n      # fails fast, no upstream call


def test_unavailable_model_without_fallback_is_a_clear_error(gs, stub):
    stub.models_missing = {"gemini-2.5-flash"}
    with pytest.raises(ProviderError) as e:
        GeminiLLM(gs).generate("hi")
    assert e.value.code == "model_not_found" and "GEMINI_MODEL" in e.value.message


def test_explicit_fallback_model_is_used_and_reported(gs, stub):
    stub.models_missing = {"gemini-2.5-flash"}
    gs.gemini_fallback_model = "gemini-3-flash"
    res = GeminiLLM(gs).generate("hi")
    assert res.model == "gemini-3-flash"                         # never silent: the response says which model answered
    assert [r["path"].split("/models/")[1].split(":")[0] for r in posts(stub)] == ["gemini-2.5-flash", "gemini-3-flash"]


def test_health_check(gs, stub):
    h = GeminiLLM(gs).health()
    assert h["ok"] and h["model"] == "gemini-2.5-flash"
    stub.models_missing = {"gemini-2.5-flash"}
    gs2 = finalize(Settings(env="test", data_dir=gs.data_dir, gemini_api_key="k", gemini_base_url=stub.url))
    h = GeminiLLM(gs2).health()
    assert not h["ok"] and "not available" in h["detail"]


def test_empty_candidates_reported(gs, stub):
    stub.script = [(200, {"candidates": [{"finishReason": "SAFETY"}]})]
    with pytest.raises(ProviderError) as e:
        GeminiLLM(gs).generate("hi")
    assert e.value.code == "empty_response"


def test_embeddings_batching_task_type_dim_and_normalisation(gs, stub):
    emb = GeminiEmbedder(gs)
    vecs = emb.embed_documents([f"text {i}" for i in range(130)])
    assert vecs.shape == (130, 16)
    assert np.allclose(np.linalg.norm(vecs, axis=1), 1.0, atol=1e-5)           # L2-normalised (required below 3072 dims)
    ps = posts(stub)
    assert len(ps) == 3 and all(":batchEmbedContents" in p["path"] for p in ps)  # 64 + 64 + 2
    r0 = ps[0]["body"]["requests"][0]
    assert r0["taskType"] == "RETRIEVAL_DOCUMENT" and r0["outputDimensionality"] == 16 and r0["model"].endswith("gemini-embedding-001")
    q = emb.embed_query("what?")
    assert q.shape == (16,) and posts(stub)[-1]["body"]["requests"][0]["taskType"] == "RETRIEVAL_QUERY"


def test_embedding_retry(gs, stub, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    stub.script = [(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})]
    assert GeminiEmbedder(gs).embed_documents(["a"]).shape == (1, 16)


def test_ocr_sends_page_image(gs, stub):
    from PIL import Image
    stub.script = [(200, {"candidates": [{"content": {"parts": [{"text": "# Scanned\nHello"}]}, "finishReason": "STOP"}]})]
    text = GeminiOCR(gs).page_to_text(Image.new("RGB", (40, 40), "white"))
    assert text.startswith("# Scanned")
    parts = posts(stub)[0]["body"]["contents"][0]["parts"]
    inline = next(p for p in parts if "inlineData" in p)["inlineData"]
    assert inline.get("mimeType", inline.get("mime_type")) == "image/png"
    assert base64.b64decode(inline["data"].replace("-", "+").replace("_", "/"))[:4] == b"\x89PNG"
    assert any("Transcribe ALL text" in p.get("text", "") for p in parts)
