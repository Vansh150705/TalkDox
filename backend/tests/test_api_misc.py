from __future__ import annotations

import httpx
import pytest

from app.config import Settings, finalize
from app.errors import ProviderRateLimited
from app.providers.llm import GroqLLM
from conftest import ingest_file
from factories import make_pdf


def test_health_ready_and_config(client):
    r = client.get("/api/health/ready").json()
    assert r["status"] == "ready" and r["database"] and r["embeddings"]["ok"] and r["llm"]["ok"]
    c = client.get("/api/config").json()
    assert ".pdf" in c["formats"] and c["demo"] is True and "problems" in c
    assert c["limits"]["max_upload_mb"] == client.state.settings.max_upload_mb
    assert all("key" not in k.lower() for k in c["limits"])                    # no secrets in the public config
    assert c["privacy"]["retention_days"] == client.state.settings.retention_days


def test_ready_degraded_without_llm(client):
    client.state.llm = None
    r = client.get("/api/health/ready").json()
    assert r["status"] == "degraded" and client.get("/api/config").json()["problems"]


def test_source_content_paging_and_conversation_lifecycle(client, auth, tmp_path):
    pages = [[f"Section {i}", f"Page {i} has the unique keyword zebra{i} and plenty of other words to index."] for i in range(6)]
    sid, _ = ingest_file(client, auth, make_pdf(tmp_path / "p.pdf", pages))
    a = client.get(f"/api/sources/{sid}/content", params={"limit_": 2}, headers=auth).json()
    b = client.get(f"/api/sources/{sid}/content", params={"limit_": 2, "offset": 2}, headers=auth).json()
    assert a["total"] == 6 and len(a["chunks"]) == 2 and a["chunks"][0]["id"] != b["chunks"][0]["id"]
    focus = client.get(f"/api/sources/{sid}/content", params={"chunk_id": b["chunks"][0]["id"]}, headers=auth).json()
    assert 1 <= len(focus["chunks"]) <= 3 and any(c["id"] == b["chunks"][0]["id"] for c in focus["chunks"])
    assert client.get(f"/api/sources/{sid}/content", params={"chunk_id": 999999}, headers=auth).status_code == 404
    d = client.post("/api/chat", json={"question": "Which page has zebra3?", "source_ids": [sid]}, headers=auth).json()
    cid = d["conversation_id"]
    assert [c["id"] for c in client.get("/api/conversations", headers=auth).json()["conversations"]] == [cid]
    assert client.delete(f"/api/conversations/{cid}", headers=auth).status_code == 200
    assert client.get(f"/api/conversations/{cid}", headers=auth).status_code == 404


def test_analytics_empty(client, auth):
    a = client.get("/api/analytics", headers=auth).json()
    assert a["questions"] == 0 and a["avg_confidence"] is None


def test_groq_provider_request_and_error_mapping(tmp_path, monkeypatch):
    s = finalize(Settings(env="test", data_dir=tmp_path / "d", groq_api_key="g-key", llm_max_retries=0))
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, json=json, headers=headers)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{\"a\": 1}"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})
    monkeypatch.setattr(httpx, "post", fake_post)
    llm = GroqLLM(s)
    res = llm.generate("hi", system="sys", schema=dict)
    assert seen["json"]["response_format"] == {"type": "json_object"} and seen["json"]["messages"][0]["role"] == "system"
    assert seen["headers"]["Authorization"] == "Bearer g-key" and res.usage["total_tokens"] == 5
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(429, text="slow down"))
    with pytest.raises(ProviderRateLimited):
        llm.generate("hi")
