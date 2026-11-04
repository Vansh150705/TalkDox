"""Query understanding: deterministic routing, source targeting and locator hints.

A rules-based router is used on purpose: it is instant, free, testable and predictable. The LLM is
only used (in answer.py) for query rewriting / expansion where it adds measurable value.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .retrieval import Hints

_VAGUE = re.compile(r"^(explain|tell me|describe|go on|continue|more|info|information|details?|and|so|yes|no|ok|okay|sure|"
                    r"fine|alright|got it|i see|hmm+|cool|nice|thanks?|thank you|huh|what|why|how|help)$", re.I)
_SUMMARY = re.compile(r"\b(summari[sz]e|summary|tl;?dr|overview|gist|key (points|takeaways)|main (points|ideas)|"
                      r"what is (this|the) (document|paper|video|page|article|file|source)s? (about|on)|"
                      r"what('?s| is) it about)\b", re.I)
_COMPARE = re.compile(r"\b(compare|comparison|differenc\w*|differ\w*|versus|vs\.?|contrast|similarit\w*|similar|"
                      r"both|in common|agree|disagree|conflict\w*|contradict\w*)\b", re.I)
_META = re.compile(r"\bhow many (pages|slides|sheets|rows|chunks|passages) (?:are |does |do |is )?(?:there )?(?:in |on )?(?:this|the|my|that|each|every|uploaded|loaded)?\s*"
                   r"(?:document|file|pdf|source|presentation|deck|workbook|spreadsheet|video|transcript|page|csv)|"
                   r"\b(?:which|what) (?:sources|documents|files) (?:do i have|have i (?:uploaded|added)|are (?:loaded|available|selected))|"
                   r"\blist (?:all )?(?:my|the) (?:sources|documents|files)\b", re.I)
_PAGE = re.compile(r"\b(?:page|pg|p\.?|slide)\s*#?\s*(\d{1,4})\b", re.I)
_TS_COLON = re.compile(r"(?<!\d)(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?!\d)")
_TS_WORDS = re.compile(r"\b(?:at|around|near|minute|min)\s*(\d{1,3})\s*(?:min(?:ute)?s?)\b", re.I)


@dataclass
class Plan:
    intent: str                          # lookup | summarize | compare | meta | vague
    source_ids: list[str]
    hints: Hints = field(default_factory=Hints)
    note: str = ""
    balance: bool = False


def extract_hints(q: str, has_video: bool, has_paged: bool) -> Hints:
    h = Hints()
    if has_paged:
        m = _PAGE.search(q)
        if m:
            h.page = int(m.group(1))
    if has_video:
        m = _TS_COLON.search(q)
        if m:
            h.time_s = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        else:
            m = _TS_WORDS.search(q)
            if m:
                h.time_s = int(m.group(1)) * 60
    return h


def _name_tokens(name: str) -> set[str]:
    stem = re.sub(r"\.[a-z0-9]{2,5}$", "", name.lower())
    return {t for t in re.findall(r"[^\W_]+", stem) if len(t) >= 3}


def route(question: str, sources: list[dict], has_history: bool) -> Plan:
    q = question.strip()
    ids = [s["id"] for s in sources]
    if not has_history and _VAGUE.match(q.rstrip("?.! ")):
        return Plan("vague", ids)
    if _META.search(q):
        return Plan("meta", ids)
    n = len(q.split())
    wants_compare = len(sources) > 1 and bool(_COMPARE.search(q))
    # source targeting: if the question names one of the selected sources, narrow to it
    target = []
    ql = q.lower()
    for s in sources:
        toks = _name_tokens(s["name"])
        distinctive = {t for t in toks if t in ql}
        if toks and (len(distinctive) >= min(2, len(toks)) or s["name"].lower() in ql):
            target.append(s["id"])
    note = ""
    if target and len(target) < len(sources) and not wants_compare and not decompose(q):
        ids, note = target, "Scoped to the source(s) named in your question."
    if wants_compare:
        cids = target if len(target) >= 2 else [s["id"] for s in sources]
        return Plan("compare", cids, balance=True,
                    note="Comparing the sources named in your question." if len(target) >= 2 else "")
    if _SUMMARY.search(q) and n <= 14:
        return Plan("summarize", ids, note=note)
    has_video = any(s["kind"] == "youtube" for s in sources if s["id"] in ids)
    has_paged = any(s["kind"] != "youtube" for s in sources if s["id"] in ids)
    return Plan("lookup", ids, extract_hints(q, has_video, has_paged), note)


_FOLLOWUP = re.compile(r"^(and|also|what about|how about|why|but|so|then|ok(ay)?[, ]|can you|could you|more|elaborate|"
                       r"explain( that| more| it)?|give (me )?an example)\b|\b(it|that|this|they|them|those|these|he|she|"
                       r"its|their|the same|former|latter|above|previous)\b", re.I)


def needs_rewrite(question: str, has_history: bool) -> bool:
    return has_history and (len(question.split()) <= 8 or bool(_FOLLOWUP.search(question)))


_SPLIT = re.compile(r"\s*(?:;|\?\s+(?=\w)|\bas well as\b|\bplus\b|,?\s+\band\b\s+(?=(?:how|what|which|who|when|where|why|is|are|does|do|did|can)\b))\s*", re.I)


def decompose(question: str) -> list[str]:
    """Split a compound question ("What did AMER earn in Q1 and how many engineers are there?") into sub-queries so
    each information need is retrieved separately (results are fused with the full question)."""
    parts = [p.strip(" ?.,") for p in _SPLIT.split(question) if p and p.strip(" ?.,")]
    from ..ingest.textutil import content_words
    parts = [p for p in parts if len(content_words(p)) >= 2]
    return parts[:3] if len(parts) >= 2 else []


_SPECIFIC = re.compile(r"\b(?:[A-Za-z]+\d+[A-Za-z0-9]*|\d+[A-Za-z]+[A-Za-z0-9]*|\w+_\w+(?:_\w+)*|\d+(?:\.\d+)+)\b")
_LOCATOR = re.compile(r"^(?:\d{1,2}:\d{2}(?::\d{2})?|\d+(?:st|nd|rd|th)|\d+(?:s|m|h|k|kb|mb|gb|kg|mm|cm|ms|px|x)|\d{4}s?)$", re.I)


def specific_terms(question: str) -> list[str]:
    """Identifier-like tokens (Q3, ef_construction, 3.0, H7). If such a token occurs nowhere in the sources the
    question almost certainly asks about something the sources do not contain."""
    out = []
    for m in _SPECIFIC.finditer(question):
        t = m.group(0)
        if _LOCATOR.match(t) or t.lower() in out:
            continue
        out.append(t.lower())
    return out[:6]


_AGG = re.compile(r"\b(highest|lowest|maximum|minimum|max|min|largest|smallest|biggest|average|mean|median|total|sum|count|"
                  r"how many rows|how many entries|number of (rows|records|entries))\b", re.I)


def aggregate_query(question: str) -> str | None:
    """Questions about extremes/totals over tabular sources are answered from the deterministic per-column statistics
    block generated at ingestion; add a query that targets those blocks."""
    if _AGG.search(question):
        return "column statistics computed over all rows min max sum mean count " + question
    return None
