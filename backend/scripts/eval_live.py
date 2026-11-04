"""Run the labelled RAG evaluation with REAL Gemini models (answers + embeddings).

    GEMINI_API_KEY=... python scripts/eval_live.py [--out report.json]

NOT executed in the repository's CI and NOT verified by the authors of this change (no API access was available).
It reuses the same corpus, questions and metric code as tests/eval, so numbers are directly comparable with the
offline baseline in docs/EVALUATION.md. It also prints the distribution of retrieval relevance for answerable vs
unanswerable questions so MIN_RELEVANCE (and the Gemini cosine range in providers/embeddings.py) can be calibrated.
Cost: roughly 70 answer calls + ~70 rewrite calls + embeddings for ~100 passages.
"""
import argparse
import json
import os
import statistics
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from conftest import FixtureServer  # noqa: E402
from eval.harness import build_world, evaluate, questions  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.providers.embeddings import GeminiEmbedder  # noqa: E402
from app.providers.llm import GeminiLLM  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="eval_live_report.json")
    args = ap.parse_args()
    s = load_settings()
    if not s.gemini_api_key:
        sys.exit("Set GEMINI_API_KEY to run the live evaluation.")
    web = FixtureServer()
    try:
        with build_world(Path(tempfile.mkdtemp(prefix="talkdox-eval-")), web, lambda cfg: GeminiLLM(s), GeminiEmbedder(s),
                         {"gemini_api_key": s.gemini_api_key, "gemini_model": s.gemini_model,
                          "embedding_dim": s.embedding_dim, "embedding_model": s.embedding_model}) as world:
            m = evaluate(world)
            # calibration data
            pos, neg = [], []
            from eval.harness import run_all
            for r in run_all(world):
                br = [t for t in r["resp"].get("trace", []) if t["step"] == "retrieve"]
                if br:
                    (neg if r["q"].negative else pos).append(br[0]["best_relevance"])
            m["relevance_calibration"] = {
                "answerable": {"min": min(pos), "p10": sorted(pos)[len(pos) // 10], "median": statistics.median(pos)},
                "unanswerable": {"max": max(neg), "p90": sorted(neg)[int(len(neg) * 0.9) - 1], "median": statistics.median(neg)},
                "hint": "Set MIN_RELEVANCE between the answerable p10 and the unanswerable median/p90.",
            }
            m["model"] = {"llm": s.gemini_model, "embedding": f"{s.embedding_model}@{s.embedding_dim}"}
    finally:
        web.stop()
    Path(args.out).write_text(json.dumps(m, indent=2))
    print(json.dumps(m, indent=2))


if __name__ == "__main__":
    main()
