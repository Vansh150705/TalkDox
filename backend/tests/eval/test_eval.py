"""End-to-end RAG evaluation over the labelled corpus (offline: hash embedder + extractive reader).

What this measures: retrieval, routing, prompting plumbing, structured-output handling, citation resolution /
verification and honest refusal behaviour of the real pipeline over real PDF/DOCX/XLSX/MD/web/transcript files.
What it does NOT measure: the answer quality or faithfulness of Gemini - run `python scripts/eval_live.py` with a key.

Run:  pytest tests/eval -s      (writes tests/eval/last_report.json)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import extractive_handler
from eval.harness import build_world, evaluate
from app.providers.embeddings import HashEmbedder
from app.providers.llm import FakeLLM

REPORT = Path(__file__).with_name("last_report.json")


@pytest.fixture(scope="module")
def world(tmp_path_factory, web):
    with build_world(tmp_path_factory.mktemp("eval"), web, lambda s: FakeLLM(s, extractive_handler), HashEmbedder(512),
                     {"embedding_dim": 512}) as w:
        yield w


def test_rag_evaluation(world):
    m = evaluate(world)
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
