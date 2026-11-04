from __future__ import annotations

import json
import re

import pytest

from conftest import extractive_handler, ingest_file
from factories import make_docx, make_pdf


def scripted(client, responses):
    """responses: schema class name -> callable(prompt) -> dict"""
    def h(prompt, system, schema):
        name = getattr(schema, "__name__", "")
        if name in responses:
            return json.dumps(responses[name](prompt))
        return extractive_handler(prompt, system, schema)
    client.state.llm.handler = h


def ev_ids(prompt):
    return re.findall(r'<evidence id="(S\d+)"', prompt)


@pytest.fixture()
def doc(client, auth, tmp_path):
    (tmp_path / "plan.md").write_text(
        "# Project Plan\n\n## Schedule\n\nThe kickoff meeting is on 2024-03-04. The beta release is due 2024-06-15 and the final "
        "launch is planned for September 9, 2024.\n\n## Responsibilities\n\nMaria must prepare the budget by April 1. The team "
        "should review the security checklist before launch.\n\n## Notes\n\nThe office is closed on 2024-12-25. Budget is 50,000 "
        "dollars in the first section but later the document says the budget is 80,000 dollars.\n")
    sid, _ = ingest_file(client, auth, tmp_path / "plan.md")
    return sid


def run(client, auth, tool, sid, **opts):
    r = client.post(f"/api/tools/{tool}", json={"source_ids": [sid], "options": opts}, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def test_timeline_items_are_cited_and_unsupported_dropped(client, auth, doc):
    scripted(client, {"TimelineOut": lambda p: {"timeline": [
        {"date": "2024-03-04", "sort_key": "2024-03-04", "event": "Kickoff meeting", "cite": ev_ids(p)[0]},
        {"date": "2024-06-15", "sort_key": "2024-06-15", "event": "Beta release", "cite": ev_ids(p)[0]},
        {"date": "2025-01-01", "sort_key": "2025-01-01", "event": "Invented event", "cite": "S77"},
        {"date": "2024-01-01", "sort_key": "2024-01-01", "event": "No cite at all"}]}})
    d = run(client, auth, "timeline", doc)
    assert [e["event"] for e in d["items"]] == ["Kickoff meeting", "Beta release"]
    assert d["dropped_unsupported"] == 2 and d["citations"] and d["items"][0]["cite"] == "C1"
    assert d["coverage"]["chunks_used"] >= 1 and d["model"]["provider"] == "fake"
    assert "kickoff" in d["citations"][0]["snippet"].lower()
    assert run(client, auth, "timeline", doc)["cached"] is True


def test_flashcards_and_quiz_validated(client, auth, doc):
    scripted(client, {
        "CardsOut": lambda p: {"flashcards": [{"question": "When is the beta due?", "answer": "2024-06-15", "difficulty": "easy",
                                               "topic": "Schedule", "cite": ev_ids(p)[0]},
                                              {"question": "Made up?", "answer": "yes", "cite": "S50"}]},
        "QuizOut": lambda p: {"questions": [
            {"question": "Who prepares the budget?", "options": ["Maria", "Tom", "Ann", "Bo"], "correct_index": 0, "cite": ev_ids(p)[0]},
            {"question": "bad index", "options": ["a", "b"], "correct_index": 5, "cite": ev_ids(p)[0]},
            {"question": "no cite", "options": ["a", "b"], "correct_index": 0, "cite": ""}]}})
    f = run(client, auth, "flashcards", doc, count=5)
    assert len(f["items"]) == 1 and f["dropped_unsupported"] == 1
    q = run(client, auth, "quiz", doc, count=3)
    assert [x["question"] for x in q["items"]] == ["Who prepares the budget?"]


def test_actions_and_contradictions(client, auth, doc):
    scripted(client, {
        "ActionsOut": lambda p: {"actions": [{"action": "Prepare the budget", "owner": "Maria", "deadline": "April 1",
                                              "priority": "high", "cite": ev_ids(p)[0]}]},
        "ContraOut": lambda p: {"contradictions": [
            {"summary": "Budget differs", "claim_a": "50,000", "cite_a": ev_ids(p)[0], "claim_b": "80,000", "cite_b": ev_ids(p)[-1]},
            {"summary": "fake", "claim_a": "x", "cite_a": ev_ids(p)[0], "claim_b": "y", "cite_b": "S99"}]}})
    a = run(client, auth, "actions", doc)
    assert a["items"][0]["owner"] == "Maria"
    c = run(client, auth, "contradictions", doc)
    assert len(c["items"]) == 1 and c["dropped_unsupported"] == 1 and c["items"][0]["cite_a"].startswith("C")


def test_timeline_and_actions_only_send_relevant_passages(client, auth, doc):
    seen = {}
    scripted(client, {"TimelineOut": lambda p: seen.setdefault("p", p) and {"timeline": []}})
    run(client, auth, "timeline", doc, refresh=True)
    assert "2024" in seen["p"]


def test_large_source_is_sampled_and_coverage_reported(client, auth, tmp_path):
    paras = "\n\n".join(f"## Part {i}\n\n" + (f"Statement number {i} gives the figure {i * 3} for item {i}. " * 40) for i in range(120))
    (tmp_path / "huge.md").write_text(paras)
    sid, _ = ingest_file(client, auth, tmp_path / "huge.md")
    scripted(client, {"CardsOut": lambda p: {"flashcards": [{"question": "q", "answer": "a", "cite": ev_ids(p)[0]}]}})
    d = run(client, auth, "flashcards", sid)
    cov = d["coverage"]
    assert cov["complete"] is False and cov["chunks_used"] < cov["chunks_total"] and "evenly sampled" in d["note"]
    # sampling is spread over the whole source, not just the beginning (the original bug)
    prompt = client.state.llm.calls[-1]["prompt"]
    nums = [int(n) for n in re.findall(r"Statement number (\d+)", prompt)]
    assert min(nums) < 10 and max(nums) > 100


def test_summary_small_document_single_call_with_citations(client, auth, doc):
    scripted(client, {"AnswerOut": lambda p: {"answerable": True, "quotes": [], "followups": [],
                                              "answer": f"The plan has a kickoff on 2024-03-04 and a beta release due 2024-06-15 [{ev_ids(p)[0]}]."}})
    d = run(client, auth, "summary", doc)
    assert d["status"] == "answered" and d["citations"] and d["coverage"]["complete"]
    n = len(client.state.llm.calls)
    assert run(client, auth, "summary", doc)["cached"] is True and len(client.state.llm.calls) == n


def test_summary_map_reduce_for_large_source(client, auth, tmp_path):
    body = "\n\n".join(f"## Chapter {i}\n\n" + (f"Chapter {i} explains topic{i} and states fact{i} clearly. " * 25) for i in range(60))
    (tmp_path / "book.md").write_text(body)
    sid, _ = ingest_file(client, auth, tmp_path / "book.md")
    calls = {"map": 0, "reduce": 0}

    def answer(p):
        ids = ev_ids(p)
        if ids:
            calls["map"] += 1
            return {"answerable": True, "answer": f"Key facts about the part [{ids[0]}].", "quotes": [], "followups": []}
        calls["reduce"] += 1
        markers = re.findall(r"\[S\d+\]", p)
        return {"answerable": True, "answer": f"Overview of the book: it covers many chapters {markers[0]} and {markers[-1]}.", "quotes": [], "followups": []}
    scripted(client, {"AnswerOut": answer})
    d = client.post("/api/chat", json={"question": "Summarize this", "source_ids": [sid]}, headers=auth).json()
    assert calls["map"] >= 2 and calls["reduce"] == 1
    assert d["status"] == "answered" and len(d["citations"]) >= 2 and d["intent"] == "summarize"
    assert "evenly sampled" not in d.get("note", "") or d["coverage"]["complete"] is False


def test_profile_has_deterministic_stats(client, auth, doc):
    scripted(client, {"Profile": lambda p: {"title": "Project Plan", "domain": "Project management", "tone": "Formal",
                                            "key_themes": ["schedule"], "key_entities": ["Maria"], "language": "English",
                                            "one_line_summary": "A plan."}})
    d = run(client, auth, "dna", doc)
    assert d["profile"]["domain"] == "Project management" and d["stats"]["reading_time_min"] >= 1
    assert "AI estimates" in d["note"]


def test_email_tool_cites(client, auth, doc):
    scripted(client, {"AnswerOut": lambda p: {"answerable": True, "quotes": [], "followups": [],
                                              "answer": f"Subject: Plan update\n\nThe beta release is due 2024-06-15 [{ev_ids(p)[0]}]."}})
    d = run(client, auth, "email", doc, goal="ask for approval")
    assert d["answer"].startswith("Subject:") and d["citations"]


def test_compare_overview_requires_two_sources(client, auth, doc, tmp_path):
    r = client.post("/api/compare", json={"source_ids": [doc, doc]}, headers=auth)
    assert r.status_code == 400
    other, _ = ingest_file(client, auth, make_docx(tmp_path / "ops.docx"))
    scripted(client, {"AnswerOut": lambda p: {"answerable": True, "quotes": [], "followups": [],
                                              "answer": f"Both documents cover planning [{ev_ids(p)[0]}][{ev_ids(p)[-1]}]."}})
    d = client.post("/api/compare", json={"source_ids": [doc, other]}, headers=auth).json()
    assert {c["source_id"] for c in d["citations"]} == {doc, other}


def test_tool_errors(client, auth, doc):
    assert client.post("/api/tools/hack", json={"source_ids": [doc]}, headers=auth).status_code == 404
    assert client.post("/api/tools/summary", json={"source_ids": ["src_nope"]}, headers=auth).status_code == 400
    assert client.post("/api/tools/summary", json={"source_ids": []}, headers=auth).status_code == 422


def test_tools_unavailable_without_llm(client, auth, doc):
    client.state.tools.llm = None
    r = client.post("/api/tools/summary", json={"source_ids": [doc]}, headers=auth)
    assert r.status_code == 503 and r.json()["code"] == "not_configured"
