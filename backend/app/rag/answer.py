"""Question answering orchestration.

Pipeline (deterministic, each step timed into `trace`):
  route -> (rewrite/expand) -> hybrid retrieve -> relevance gate -> grounded generation (structured JSON)
  -> citation + support verification -> confidence -> persist

Single LLM call for the answer; at most one cheap extra call for rewrite/expansion; no free-form
"agent loops" with tool access, so injected instructions have nothing to hijack.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Callable, Optional

from ..config import Settings
from ..db import Database, new_id
from ..errors import AppError, NotConfigured, ProviderError
from ..providers.llm import BaseLLM, generate_structured
from ..security import sanitize_model_output
from .citations import Verified, location_label, verify_answer
from .prompts import (AnswerOut, REWRITE_PROMPT, RewriteOut, evidence_block, history_block, system_prompt)
from .retrieval import Hit, Retriever
from .router import Plan, aggregate_query, decompose, needs_rewrite, route, specific_terms

log = logging.getLogger("talkdox.answer")

NO_EVIDENCE = "I couldn't find information about that in the selected sources."


class Trace:
    def __init__(self):
        self.steps: list[dict] = []
        self._t = time.time()

    def step(self, name: str, **info):
        now = time.time()
        self.steps.append({"step": name, "ms": int((now - self._t) * 1000), **info})
        self._t = now


class AnswerService:
    def __init__(self, s: Settings, db: Database, llm: Optional[BaseLLM], retriever: Optional[Retriever],
                 tools=None):
        self.s, self.db, self.llm, self.retriever, self.tools = s, db, llm, retriever, tools

    # ------------------------------------------------------------------ helpers
    def usable_sources(self, wid: str, source_ids: list[str] | None) -> list[dict]:
        all_src = self.db.list_sources(wid)
        ok = [x for x in all_src if x["status"] in ("completed", "partial") and x["n_chunks"] > 0]
        if source_ids:
            wanted = set(source_ids)
            missing = wanted - {x["id"] for x in all_src}
            if missing:
                raise AppError("One or more selected sources don't exist.", code="not_found", status=404)
            ok = [x for x in ok if x["id"] in wanted]
        return ok

    def history(self, wid: str, conv_id: str | None) -> list[dict]:
        if not conv_id:
            return []
        rows = self.db.all("SELECT role, content FROM messages WHERE conversation_id=? AND workspace_id=? "
                           "ORDER BY created_at DESC LIMIT 8", (conv_id, wid))
        out = [{"role": r["role"], "content": re.sub(r"\[S\d+\]", "", r["content"])} for r in reversed(rows)]
        return out

    def ensure_conversation(self, wid: str, conv_id: str | None, first_question: str) -> str:
        if conv_id:
            if not self.db.one("SELECT 1 FROM conversations WHERE id=? AND workspace_id=?", (conv_id, wid)):
                raise AppError("Conversation not found.", code="not_found", status=404)
            return conv_id
        cid = new_id("conv_")
        now = time.time()
        title = re.sub(r"\s+", " ", first_question).strip()[:80]
        self.db.run("INSERT INTO conversations(id, workspace_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
                    (cid, wid, title, now, now))
        return cid

    def save_message(self, wid: str, cid: str, role: str, content: str, payload: dict | None = None) -> str:
        mid = new_id("msg_")
        now = time.time()
        self.db.run("INSERT INTO messages(id, conversation_id, workspace_id, role, content, payload, created_at) "
                    "VALUES (?,?,?,?,?,?,?)", (mid, cid, wid, role, content, json.dumps(payload or {}, default=str), now))
        self.db.run("UPDATE conversations SET updated_at=? WHERE id=?", (now, cid))
        return mid

    # ------------------------------------------------------------------ main
    def ask(self, wid: str, question: str, source_ids: list[str] | None = None, conversation_id: str | None = None,
            mode: str = "qa", persona: str = "default", on_stage: Callable[[str, str], None] | None = None,
            cancelled: Callable[[], bool] | None = None) -> dict:
        stage = on_stage or (lambda k, m: None)
        cancelled = cancelled or (lambda: False)
        question = (question or "").strip()
        if not question:
            raise AppError("Please type a question.", code="empty_question")
        if len(question) > self.s.max_question_chars:
            raise AppError(f"Questions are limited to {self.s.max_question_chars} characters.", code="question_too_long")
        sources = self.usable_sources(wid, source_ids)
        if not sources:
            raise AppError("Add at least one source (and wait for it to finish processing) before asking questions.",
                           code="no_sources")
        if self.llm is None or self.retriever is None:
            raise NotConfigured("No AI model is configured on this server (set GEMINI_API_KEY).")
        trace = Trace()
        cid = self.ensure_conversation(wid, conversation_id, question)
        hist = self.history(wid, cid)
        plan = route(question, sources, bool(hist))
        trace.step("route", intent=plan.intent, sources=len(plan.source_ids))
        by_id = {x["id"]: x for x in sources}

        if plan.intent == "vague":
            payload = self._simple(cid, "clarify", "Could you be more specific? Tell me what you'd like to know about "
                                   "your sources - for example a topic, a section, or a question about a detail.",
                                   plan, sources)
        elif plan.intent == "meta":
            payload = self._simple(cid, "meta", self._meta_answer(sources), plan, sources)
        elif plan.intent == "summarize" and self.tools is not None:
            stage("summarizing", "Reading through the whole source…")
            payload = self.tools.summarize_for_chat(wid, plan.source_ids, mode, persona, trace)
            payload.update({"conversation_id": cid, "intent": "summarize", "scope_note": plan.note})
        else:
            payload = self._answer_lookup(wid, cid, question, plan, by_id, hist, mode, persona, trace, stage, cancelled)

        payload["trace"] = trace.steps
        payload.setdefault("model", {"provider": self.llm.provider, "name": self.llm.model})
        self.save_message(wid, cid, "user", question, {"source_ids": plan.source_ids})
        payload["message_id"] = self.save_message(wid, cid, "assistant", payload["answer"], payload)
        payload["conversation_id"] = cid
        return payload

    # ------------------------------------------------------------------ lookup / compare
    def _answer_lookup(self, wid, cid, question, plan: Plan, by_id, hist, mode, persona, trace, stage, cancelled):
        queries = [question]
        if self.s.multi_query and (needs_rewrite(question, bool(hist)) or len(question.split()) >= 6):
            stage("rewriting", "Understanding your question…")
            queries = self._rewrite(question, hist) or queries
            trace.step("rewrite", queries=queries)
        extra = decompose(question) + ([aggregate_query(question)] if aggregate_query(question) else [])
        for sub in extra:
            if sub not in queries:
                queries.append(sub)
        if cancelled():
            raise AppError("Cancelled.", code="cancelled", status=499)
        stage("retrieving", "Searching your sources…")
        k = self.s.retrieval_top_k + (4 if plan.intent == "compare" else 0)
        res = self.retriever.search(wid, plan.source_ids, queries, k=k, hints=plan.hints, balance=plan.balance)
        trace.step("retrieve", candidates=res.candidates, selected=len(res.hits), best_relevance=round(res.best_relevance, 3),
                   per_source=res.per_source)
        used = [{"id": sid, "name": by_id[sid]["name"]} for sid in plan.source_ids if sid in by_id]
        missing = self.retriever.missing_terms(wid, plan.source_ids, specific_terms(question))
        trace.step("term_check", missing=missing)
        if missing:
            # an identifier-like token (Q3, 3.0, ef_construction) that occurs nowhere in the sources: answering from
            # the nearest passages would silently answer a different question (e.g. Q2 figures for a Q3 question)
            names = ", ".join(f"'{m}'" for m in missing)
            related = [self._cite_like(h.chunk, i + 1, "") for i, h in enumerate(res.hits[:3]) if not h.context_only]
            return {"answer": f"{NO_EVIDENCE} The sources never mention {names}. If it appears under a different name, try rephrasing.", "answerable": False,
                    "status": "no_evidence", "citations": [], "flags": [],
                    "confidence": {"level": "none", "score": 0, "factors": {"missing_terms": missing}},
                    "followups": [], "related": related, "intent": plan.intent, "scope_note": plan.note,
                    "sources_used": used}
        if not res.hits or res.best_relevance < self.s.min_relevance:
            related = [self._cite_like(h.chunk, 1, "") for h in res.hits[:3] if not h.context_only]
            return {"answer": NO_EVIDENCE + " Try rephrasing, or check that the right sources are selected.",
                    "answerable": False, "status": "no_evidence", "citations": [], "flags": [],
                    "confidence": {"level": "none", "score": 0, "factors": {"best_relevance": round(res.best_relevance, 3)}},
                    "followups": [], "related": related, "intent": plan.intent, "scope_note": plan.note,
                    "sources_used": used}
        hits = res.hits
        budget = self.s.context_char_budget
        chunks, total = [], 0
        for h in hits:
            n = min(len(h.chunk["text"]), 2600)
            if total + n > budget and chunks:
                break
            chunks.append(h.chunk)
            total += n
        evidence, ev_text = evidence_block(chunks)
        prompt = (f"Conversation so far (context only, NOT evidence):\n{history_block(hist) or '(none)'}\n\n"
                  f"Evidence:\n{ev_text}\n\nUser question: {question}"
                  + (f"\n(Interpreted as: {queries[0]})" if queries[0] != question else ""))
        absent = self.retriever.missing_terms(wid, plan.source_ids, _question_terms(question)[:8])
        if absent:
            prompt += ("\n\nNote: these words from the question do not appear anywhere in the selected sources: "
                       + ", ".join(f"'{a}'" for a in absent) + ". Unless the evidence clearly covers them (for example under a "
                       "synonym), answer with answerable=false and say they are not covered.")
        if plan.intent == "compare":
            names = ", ".join(f'"{u["name"]}"' for u in used)
            prompt += (f"\n\nThis is a comparison across these sources: {names}. Organise the answer by source or in a "
                       f"table, make clear which source each point comes from, and call out differences explicitly.")
        stage("generating", "Writing a grounded answer…")
        try:
            out, res_llm = generate_structured(self.llm, prompt, AnswerOut, system=system_prompt(mode, persona),
                                               temperature=0.2, thinking=True)
        except ProviderError:
            raise
        trace.step("generate", tokens=res_llm.usage, model=res_llm.model)
        stage("verifying", "Checking citations…")
        return self._finish(out, evidence, hits, plan, used, trace, res_llm)

    def _finish(self, out: AnswerOut, evidence, hits: list[Hit], plan: Plan, used, trace, res_llm) -> dict:
        answer = sanitize_model_output(out.answer or "").strip()
        quotes = {q.id.upper(): q.quote for q in out.quotes}
        v = verify_answer(answer, quotes, evidence)
        trace.step("verify", supported=v.supported, weak=v.weak, unsupported=v.unsupported, uncited=v.uncited,
                   flags=len(v.flags))
        by_chunk = {h.chunk["id"]: h.relevance for h in hits}
        cited_rel = [by_chunk.get(c["chunk_id"], 0.0) for c in v.citations]
        status = "answered"
        if not out.answerable:
            status = "no_evidence" if not v.citations else "partial"
        if out.answerable and not v.citations:
            # Nothing the model said could be tied to retrieved evidence: do not show it.
            related = [self._cite_like(h.chunk, i + 1, "") for i, h in enumerate(hits[:3]) if not h.context_only]
            return {"answer": "I wasn't able to ground an answer to that in your sources, so I won't guess. "
                              "The closest passages I found are listed below.",
                    "answerable": False, "status": "unverified", "citations": [], "flags": [f.as_dict() for f in v.flags],
                    "confidence": {"level": "none", "score": 0, "factors": {"reason": "no verifiable citations"}},
                    "followups": [], "related": related, "intent": plan.intent, "scope_note": plan.note,
                    "sources_used": used}
        conf = confidence(cited_rel, v, out.answerable)
        text = v.text
        if out.missing and out.missing.strip() and (not out.answerable or status == "partial"):
            text += f"\n\n**Not found in the sources:** {out.missing.strip()}"
        return {"answer": text, "answerable": out.answerable, "status": status, "citations": v.citations,
                "flags": [f.as_dict() for f in v.flags], "confidence": conf,
                "followups": [f.strip() for f in out.followups if f.strip()][:3], "related": [], "intent": plan.intent,
                "scope_note": plan.note, "sources_used": used, "usage": res_llm.usage, "citation_check_limited": v.limited,
                # the model that actually answered (differs from the configured one after a fallback)
                "model": {"provider": res_llm.provider, "name": res_llm.model}}

    def _cite_like(self, ch: dict, n: int, quote: str) -> dict:
        return {"id": f"R{n}", "chunk_id": ch["id"], "source_id": ch["source_id"], "source_name": ch.get("source_name"),
                "source_kind": ch.get("source_kind"), "label": location_label(ch), "snippet": ch["text"][:300],
                "deep_link": None}

    def _rewrite(self, question: str, hist: list[dict]) -> list[str] | None:
        try:
            out, _ = generate_structured(self.llm, REWRITE_PROMPT.format(history=history_block(hist) or "(none)",
                                                                           question=question),
                                         RewriteOut, temperature=0.0, thinking=False, repair_attempts=0, max_tokens=300)
        except Exception as e:
            log.info("rewrite skipped: %s", type(e).__name__)
            return None
        qs = [out.standalone.strip()[:400]] + [v.strip()[:400] for v in out.variants[:2]]
        qs = [q for q in dict.fromkeys(qs) if q]
        if question not in qs and not needs_rewrite(question, bool(hist)):
            qs.append(question)
        return qs or None

    def _simple(self, cid, status, text, plan, sources) -> dict:
        return {"answer": text, "answerable": status == "meta", "status": status, "citations": [], "flags": [],
                "confidence": {"level": "none", "score": 0, "factors": {}}, "followups": [], "related": [],
                "intent": plan.intent, "scope_note": "", "sources_used": [{"id": x["id"], "name": x["name"]} for x in sources]}

    def _meta_answer(self, sources: list[dict]) -> str:
        lines = ["Here is what's loaded (from the extraction metadata, not generated by the AI):"]
        for s_ in sources:
            unit = {"youtube": "transcript segments", "web": "pages"}.get(s_["kind"], "pages/sheets/slides")
            lines.append(f"- **{s_['name']}** - {s_['n_pages']} {unit}, {s_['n_chunks']} searchable passages, "
                         f"status: {s_['status']}")
        return "\n".join(lines)


def _question_terms(question: str) -> list[str]:
    from ..ingest.textutil import content_words
    generic = {"many", "much", "does", "using", "used", "say", "says", "tell", "give", "please", "need", "want", "know",
               "describe", "explain", "list", "show", "mention", "mentioned", "document", "documents", "source", "sources",
               "page", "talk", "video", "file", "text", "according", "regarding", "about"}
    return [w for w in dict.fromkeys(content_words(question)) if w not in generic and not w.isdigit() and len(w) > 3]


def confidence(cited_relevance: list[float], v: Verified, answerable: bool) -> dict:
    """Evidence-derived, not decorative: combines retrieval relevance of the cited passages with the
    fraction of cited sentences that pass the support check, then applies penalties."""
    retr = max(cited_relevance) if cited_relevance else 0.0
    support = v.support_ratio if (v.supported + v.weak + v.unsupported) else (0.6 if v.limited else 0.0)
    score = 100 * (0.45 * retr + 0.55 * support)
    score -= 8 * min(v.uncited, 3) + 12 * min(v.unsupported, 3)
    if not answerable:
        score = min(score, 40)
    score = int(max(0, min(100, round(score))))
    level = "high" if score >= 70 and v.unsupported == 0 else "medium" if score >= 45 else "low"
    return {"level": level, "score": score,
            "factors": {"retrieval_relevance": round(retr, 2), "citation_support": round(support, 2),
                        "unsupported_sentences": v.unsupported, "uncited_sentences": v.uncited,
                        "checks_limited": v.limited}}
