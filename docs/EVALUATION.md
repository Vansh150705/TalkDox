# Retrieval, answer and citation evaluation

> **Read this first.** No Gemini key or outbound network access was available while this was built. Everything below
> was measured with **offline test doubles**: a *lexical* hashing embedder (not semantic) and an *extractive* reader
> (copies the best-matching sentences with `[S#]` citations — it is not an LLM and has no judgement). The numbers
> therefore measure the **retrieval, routing, verification and refusal machinery** of the pipeline over real files, and
> say **nothing** about Gemini's answer quality, faithfulness or hallucination rate. `backend/scripts/eval_live.py`
> repeats the same evaluation with real Gemini models (unverified by the authors) so you can obtain those numbers.

## Method
* **Corpus** (`backend/tests/eval/corpus.py`) — fictional company "Helios Robotics" so no model can answer from memory: an 8-page PDF handbook with a ruled expense table, a DOCX spec with a table, a two-sheet XLSX, a Markdown minutes file, a 3-page crawled website, and a 40-minute YouTube transcript with facts at known timestamps (provider faked). All sources are ingested through the real pipeline into one workspace, so every question competes against **all** sources (no source filtering).
* **71 labelled questions** (+5 opening turns for the follow-up pairs): 15 handbook lookups, 9 table/numeric, 10 spec/minutes lookups, 4 web, 6 video (2 with explicit timestamps), 3 page-referenced, 3 scattered (2 facts each), 3 multi-source, 1 comparison, 5 two-turn follow-ups, and **12 unanswerable** questions (unrelated, plausible-but-absent such as "price of the H7", absent versions such as "firmware 3.0", wrong-quarter such as "EMEA Q3").
* **Relevance label:** a retrieved passage is relevant if it contains the gold fact string (e.g. `| Hotel | 220 USD |`, `eleven percent`). Video answers must cite a passage whose start time falls in a labelled window.
* **Metrics:** recall@8 (share of gold facts present in the top-8 passages), MRR, hit@1, citation precision/recall, answer correctness (answer contains the gold terms), share of answers with verifier flags, refusal behaviour on negatives, follow-up accuracy, latency (offline, so only pipeline overhead).

## Results (offline, `pytest tests/eval -s`, 59 answerable + 12 unanswerable)
| Metric | Value |
|---|---|
| Retrieval recall@8 | **0.983** (97% of questions had *all* gold facts in the top 8) |
| MRR / hit@1 | **0.917 / 0.881** |
| Citation recall / precision | 0.864 / 0.774 (precision is dragged down by the naive reader citing several sentences) |
| Answers carrying any verifier flag | 12% (all `quote_unverified`: the reader quotes a table header + a distant row, which is correctly rejected as non-contiguous) |
| Follow-up accuracy (5 pairs) | 1.00 |
| Unanswerable questions correctly declined | 9 / 12 (0.75); 5 of them declined *without calling the LLM* (relevance / missing-identifier gates). Wrongly answered by the reader double: n04 "retail price of the H7", n05 "employees in Marketing", n10 "sabbatical leave" — absent *plain words*, which in production are handled by the LLM (the prompt lists question words that occur nowhere in the sources) and are **not** measured here |
| Answer correctness by type (reader double) | lookup 0.96, web 1.0, page 1.0, compare 1.0, follow-up 1.0, scatter 0.67, video 0.67, table 0.56, multi 0.33 — dominated by the naive reader (no synthesis across passages), not by retrieval |
| Offline latency p50 / p95 | 23 ms / 40 ms (no network; real latency is dominated by Gemini calls) |

### Retrieval ablation (same 59 questions; lexical-hash embedder)
| Configuration | recall@8 | MRR |
|---|---|---|
| BM25 (FTS5) only | 0.981 | **0.946** |
| Dense only (hashed bag-of-words) | 0.787 | 0.720 |
| Hybrid RRF | 0.972 | 0.844 |
| **Production**: hybrid + IDF-weighted rerank + MMR | **0.981** | 0.910 |

**Honest reading:** with a *lexical* stand-in for embeddings, BM25 alone ranks best (MRR 0.946 vs 0.910), so this experiment does **not** demonstrate that dense retrieval helps. The hybrid design is kept because paraphrase / synonym questions ("how often are laptops replaced" → "refresh cycle") are exactly what a real semantic embedder adds and this proxy cannot measure; the rerank step is shown to recover most of the RRF → BM25 gap. The live script will tell you whether Gemini embeddings justify the dense half; if not, lower the dense weight.

## Defects this evaluation found and fixed
These were found by the harness, not by inspection:
1. trafilatura emitted markdown-escaped text (`ef\_search`), so identifiers were un-searchable → unescaping in the markdown parser.
2. Whole-workspace per-source balancing pushed irrelevant passages into the top-k for ordinary lookups → balancing now only applies to comparisons and only with relevant passages.
3. Plain term-overlap ranked a descriptive sentence ("robot arm …") above the table row holding the answer → IDF-weighted coverage.
4. Numeric column statistics were rendered as `1.65e+06` → plain decimals.
5. "How many documents does the indexing job process?" was routed to the metadata intent → intent regexes tightened.
6. Citations placed after the full stop (`… leave. [S1] Next sentence`) were attached to the *next* sentence, producing false "unsupported" flags (36 flags → 0 false flags) → marker normalisation.
7. A question naming one source ("… from the engineering blog") dropped the other half of a compound question → compound questions are never narrowed by source name, and comparisons honour all named sources.
8. Ranking ignored explicit locators (`at 12:45`, `page 5`) → locator boosts.

## Not measured (and why)
Answer correctness/faithfulness/hallucination frequency of Gemini, token consumption, real latency, multilingual quality, OCR accuracy, large-scale (>10k passages) retrieval quality, long multi-turn drift. The citation *verifier* is lexical: it bounds fabricated ids, quotes and numbers but cannot prove semantic faithfulness; an LLM second-opinion verifier was considered but not implemented (it would add latency and cost without a measured benefit).

## Reproduce
```bash
cd backend
python -m pytest tests/eval -q -s                      # offline baseline (regression guard thresholds in tests/eval/test_eval.py)
GEMINI_API_KEY=... python scripts/eval_live.py         # real Gemini; also prints relevance calibration for MIN_RELEVANCE
```
