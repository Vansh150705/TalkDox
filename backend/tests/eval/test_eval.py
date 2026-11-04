"""End-to-end RAG evaluation over the labelled corpus (offline: hash embedder + extractive reader).

What this measures: retrieval, routing, prompting plumbing, structured-output handling, citation resolution /
verification and honest refusal behaviour of the real pipeline over real PDF/DOCX/XLSX/MD/web/transcript files.
What it does NOT measure: the answer quality or faithfulness of Gemini - run `scripts/eval_live.py` with a key.

Run:  pytest tests/eval -s      (writes tests/eval/last_report.json)
"""
from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

import pytest

from conftest import wait_job
from eval.corpus import Q, build, questions, talk_segments, BLOG
from app.ingest.youtube import Seg, Transcript, VideoMeta

REPORT = Path(__file__).with_name("last_report.json")
NORM = lambda s: " ".join(s.lower().replace(",", "").split())


class Prov:
    name = "fake"

    def fetch(self, vid, langs):
        return Transcript([Seg(t, s, 5.0) for t, s in talk_segments()], "en", "manual", "fake", ["en"])


@pytest.fixture(scope="module")
def world(tmp_path_factory, web):
    # module-scoped copy of the app (fixtures in conftest are function-scoped)
    from fastapi.testclient import TestClient
    from app.config import Settings, finalize
    from app.main import create_app
    from app.net import SafeFetcher
    from app.providers.embeddings import HashEmbedder
    from app.providers.llm import FakeLLM
    from app.state import build_state
    from conftest import extractive_handler, new_workspace
    tmp = tmp_path_factory.mktemp("eval")
    s = finalize(Settings(env="test", data_dir=tmp / "data", allow_private_network=True, crawl_delay_s=0, retention_days=0,
                          rate_chat_per_min=10 ** 6, rate_ingest_per_hour=10 ** 6, rate_tools_per_hour=10 ** 6,
                          embedding_dim=512, ocr_engine="none", llm_max_retries=0))
    st = build_state(s, llm=FakeLLM(s, extractive_handler), embedder=HashEmbedder(512), fetcher=SafeFetcher(s),
                     yt_providers=[Prov()], yt_meta_fn=lambda v: VideoMeta(v, "Scaling Retrieval Systems", "Helios", "", 2400))
    web.routes.clear()
    web.text("/robots.txt", "User-agent: *\nAllow: /\n")
    for path, (title, body) in BLOG.items():
        web.html(path, f"<html><head><title>{title}</title></head><body><main>{body}</main></body></html>")
    app = create_app(state=st)
    with TestClient(app) as c:
        auth = new_workspace(c)
        files = build(tmp, web.base)
        ids = {}
        for key, path in files.items():
            if key == "blog":
                r = c.post("/api/sources/web", json={"urls": [path], "crawl": True, "max_depth": 1, "max_pages": 5,
                                                     "path_prefix": "/eng/"}, headers=auth).json()["results"][0]
            elif key == "talk":
                r = c.post("/api/sources/youtube", json={"url": "https://youtu.be/AAAAAAAAAAA"}, headers=auth).json()["results"][0]
            else:
                with open(path, "rb") as f:
                    r = c.post("/api/sources/files", files=[("files", (Path(path).name, f))], headers=auth).json()["results"][0]
            job = wait_job(c, auth, r["job_id"])
            assert job["status"] in ("completed", "partial"), (key, job)
            ids[key] = r["source_id"]
        wid = c.get("/api/workspace", headers=auth).json()["workspace_id"]
        yield {"c": c, "auth": auth, "ids": ids, "wid": wid, "st": st}


SEED_Q = {q.id: q.question for q in questions() if q.type == "followup_seed"}


def _retrieve(st, wid, ids, q, **kw):
    """Retrieval exactly as the chat pipeline performs it (routing hints, decomposition, balancing)."""
    from app.rag.router import decompose, route
    srcs = [st.db.get_source(wid, i) for i in ids.values()]
    plan = route(q.question, srcs, bool(q.followup_of))
    from app.rag.router import aggregate_query
    base = (SEED_Q.get(q.followup_of, "") + " " if q.followup_of else "") + q.question      # follow-ups: standalone form
    queries = [base] + decompose(q.question) + ([aggregate_query(q.question)] if aggregate_query(q.question) else [])
    return st.retriever.search(wid, plan.source_ids, queries, hints=plan.hints, balance=plan.balance, **kw)


def relevant(chunk: dict, facts: list[str]) -> bool:
    t = NORM(chunk["text"])
    return any(NORM(f) in t for f in facts)


def run_all(world):
    c, auth, ids, st, wid = world["c"], world["auth"], world["ids"], world["st"], world["wid"]
    qs = questions()
    seeds = {q.id: q for q in qs if q.type == "followup_seed"}
    conv: dict[str, str] = {}
    rows = []
    for q in qs:
        src_ids = [ids[k] for k in q.sources] if q.sources else None
        body = {"question": q.question, **({"source_ids": src_ids} if src_ids else {})}
        if q.followup_of:
            body["conversation_id"] = conv[q.followup_of]
        t0 = time.time()
        d = c.post("/api/chat", json=body, headers=auth).json()
        lat = (time.time() - t0) * 1000
        if q.type == "followup_seed":
            conv[q.id] = d["conversation_id"]
        # retrieval-level metrics: same query, no LLM
        res = _retrieve(st, wid, ids, q) if not q.negative else None
        rows.append({"q": q, "resp": d, "lat": lat, "retrieval": res})
    return rows


def metrics(rows):
    ans_rows = [r for r in rows if not r["q"].negative and r["q"].type != "followup_seed"]
    neg_rows = [r for r in rows if r["q"].negative]
    m = {"n_answerable": len(ans_rows), "n_negative": len(neg_rows)}
    rec, prec, mrr, hit1, k_used = [], [], [], [], []
    for r in ans_rows:
        q, res = r["q"], r["retrieval"]
        hits = [h for h in res.hits if not h.context_only]
        flags = [relevant(h.chunk, q.facts) for h in hits]
        covered = sum(1 for f in q.facts if any(NORM(f) in NORM(h.chunk["text"]) for h in hits))
        rec.append(covered / len(q.facts))
        prec.append(sum(flags) / len(flags) if flags else 0.0)
        first = next((i for i, fl in enumerate(flags) if fl), None)
        mrr.append(1 / (first + 1) if first is not None else 0.0)
        hit1.append(1.0 if flags and flags[0] else 0.0)
    m["retrieval"] = {"recall@8": statistics.mean(rec), "precision@8": statistics.mean(prec), "mrr": statistics.mean(mrr),
                      "hit@1": statistics.mean(hit1), "full_recall_rate": sum(1 for x in rec if x == 1.0) / len(rec)}
    corr, cit_p, cit_r, support, flagged, answered = [], [], [], [], 0, 0
    by_type: dict[str, list[float]] = {}
    for r in ans_rows:
        q, d = r["q"], r["resp"]
        text = NORM(d.get("answer", ""))
        ok = d["status"] in ("answered", "partial") and all(NORM(t) in text for t in q.answer_has)
        corr.append(1.0 if ok else 0.0)
        by_type.setdefault(q.type, []).append(1.0 if ok else 0.0)
        if d["citations"]:
            answered += 1
            good = [bool(any(NORM(f) in NORM(c["snippet"]) or NORM(f) in NORM(_chunk_text(r, c)) for f in q.facts)) for c in d["citations"]]
            cit_p.append(sum(good) / len(good))
            cov = sum(1 for f in q.facts if any(NORM(f) in NORM(_chunk_text(r, c)) for c in d["citations"]))
            cit_r.append(cov / len(q.facts))
            flagged += 1 if d.get("flags") else 0
            if q.t_range:     # timestamp citations must land inside the labelled window
                cit_p[-1] = 1.0 if any(q.t_range[0] <= (c["t_start"] or -1) <= q.t_range[1] for c in d["citations"]) else 0.0
        else:
            cit_p.append(0.0)
            cit_r.append(0.0)
    m["answer_correctness"] = statistics.mean(corr)
    m["answer_correctness_by_type"] = {k: round(statistics.mean(v), 3) for k, v in sorted(by_type.items())}
    m["citation_precision"] = statistics.mean(cit_p)
    m["citation_recall"] = statistics.mean(cit_r)
    m["answers_with_flags"] = flagged / max(answered, 1)
    refused = [r for r in neg_rows if r["resp"]["status"] in ("no_evidence", "unverified", "partial") and not r["resp"]["answerable"]]
    m["negatives"] = {"correct_refusal_rate": len(refused) / len(neg_rows),
                      "false_answers": [r["q"].id for r in neg_rows if r["resp"]["status"] == "answered"],
                      "refused_without_llm_call": sum(1 for r in neg_rows if r["resp"]["status"] == "no_evidence"
                                                        and not any(t["step"] == "generate" for t in r["resp"]["trace"]))}
    fu = [r for r in rows if r["q"].followup_of]
    m["followup_accuracy"] = statistics.mean(
        1.0 if all(NORM(t) in NORM(r["resp"]["answer"]) for t in r["q"].answer_has) else 0.0 for r in fu)
    lats = sorted(r["lat"] for r in rows)
    m["latency_ms_offline"] = {"p50": round(statistics.median(lats), 1), "p95": round(lats[int(0.95 * len(lats)) - 1], 1)}
    return m


def _chunk_text(r, c):
    return next((h.chunk["text"] for h in (r["retrieval"].hits if r["retrieval"] else []) if h.chunk["id"] == c["chunk_id"]), c["snippet"])


def ablation(world):
    c, ids, st, wid = world["c"], world["ids"], world["st"], world["wid"]
    qs = [q for q in questions() if not q.negative and q.type != "followup_seed" and not q.followup_of]
    out = {}
    for name, kw in {"bm25_only": dict(mode="bm25", rerank=False), "dense_only": dict(mode="dense", rerank=False),
                     "hybrid_rrf": dict(mode="hybrid", rerank=False), "hybrid_rrf+rerank+mmr (production)": dict(mode="hybrid", rerank=True)}.items():
        rec, mrr, p = [], [], []
        for q in qs:
            res = _retrieve(st, wid, ids, q, **kw)
            hits = [h for h in res.hits if not h.context_only]
            rec.append(sum(1 for f in q.facts if any(NORM(f) in NORM(h.chunk["text"]) for h in hits)) / len(q.facts))
            fl = [relevant(h.chunk, q.facts) for h in hits]
            first = next((i for i, x in enumerate(fl) if x), None)
            mrr.append(1 / (first + 1) if first is not None else 0.0)
            p.append(sum(fl) / len(fl) if fl else 0.0)
        out[name] = {"recall@8": round(statistics.mean(rec), 3), "mrr": round(statistics.mean(mrr), 3), "precision@8": round(statistics.mean(p), 3)}
    return out


def test_rag_evaluation(world):
    rows = run_all(world)
    m = metrics(rows)
    m["ablation_retrieval"] = ablation(world)
    m["embedder"] = "feature-hash (offline test double: lexical, NOT semantic) - see docs/EVALUATION.md"
    m["reader"] = "extractive test double (not an LLM)"
    REPORT.write_text(json.dumps(m, indent=2))
    print("\n" + json.dumps(m, indent=2))
    r = m["retrieval"]
    # Regression guards for the offline setup (values chosen just below what the pipeline achieves today).
    assert r["recall@8"] >= 0.93, "retrieval recall regressed"
    assert r["mrr"] >= 0.80 and r["hit@1"] >= 0.75
    assert m["citation_recall"] >= 0.80
    assert m["answer_correctness"] >= 0.75
    assert m["negatives"]["correct_refusal_rate"] >= 0.45          # reader double has no judgement; see docs
    assert m["followup_accuracy"] >= 0.8
    assert m["answers_with_flags"] <= 0.25, "verifier flags legitimate extractive answers"
    ab = m["ablation_retrieval"]
    assert ab["hybrid_rrf+rerank+mmr (production)"]["recall@8"] >= ab["dense_only"]["recall@8"]
