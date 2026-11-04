"""Citation resolution and answer verification.

The model is asked to cite evidence ids ([S1] ...). We never trust that blindly:
  * ids that were not in the evidence set are removed and flagged
  * each quoted span must exist (verbatim or near-verbatim) inside the cited passage
  * each cited sentence is checked against the passages it cites (content-word coverage + numbers)
  * uncited factual sentences are flagged
  * citations are renumbered by first use and resolved to page / slide / sheet / URL / timestamp
These checks are deterministic and cheap; they bound hallucination but are lexical, so they are
documented as a safety net, not a proof of faithfulness (see docs/EVALUATION.md).
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from urllib.parse import quote

from ..ingest.textutil import fmt_ts, sentences
from .retrieval import _stems

_MARK = re.compile(r"\[\s*S(\d+)(?:\s*[,;]\s*S(\d+))*\s*\]|\[S(\d+)\]", re.I)
_MARK_ANY = re.compile(r"\[\s*((?:S\d+\s*[,;]?\s*)+)\]", re.I)
_REFUSAL = re.compile(r"^(i (could not|couldn'?t|can'?t|cannot|was unable)|the (provided |selected )?(sources?|documents?|"
                      r"evidence|text|transcript|video|page)s? (do(es)? not|doesn'?t|don'?t)|there is no|no (relevant )?"
                      r"(information|mention|evidence)|not (found|mentioned|stated|specified))", re.I)
_NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?%?")


@dataclass
class Flag:
    type: str
    sentence: str = ""
    detail: str = ""

    def as_dict(self):
        return {"type": self.type, "sentence": self.sentence[:300], "detail": self.detail}


@dataclass
class Verified:
    text: str
    citations: list[dict]
    flags: list[Flag]
    supported: int = 0
    weak: int = 0
    unsupported: int = 0
    uncited: int = 0
    checked: int = 0
    limited: bool = False         # lexical checks skipped (non-Latin answer)

    @property
    def support_ratio(self) -> float:
        n = self.supported + self.weak + self.unsupported
        return (self.supported + 0.5 * self.weak) / n if n else 0.0


def location_label(ch: dict) -> str:
    fmt = (ch.get("source_meta") or {}).get("format")
    kind = ch.get("source_kind")
    if kind == "youtube" or ch["kind"] == "transcript":
        a = fmt_ts(ch.get("t_start"))
        b = fmt_ts(ch.get("t_end"))
        return f"{a}–{b}" if b and b != a else a
    meta = ch.get("meta") or {}
    if meta.get("sheet"):
        return f"Sheet '{meta['sheet']}', rows {meta.get('rows', '')}".strip(", ")
    if kind == "web":
        return ch.get("heading") or (ch.get("url") or "")
    if ch.get("page"):
        unit = "Slide" if fmt == "pptx" else "p."
        s = f"{unit} {ch['page']}" if unit == "p." else f"{unit} {ch['page']}"
        if ch.get("page_end"):
            s += f"–{ch['page_end']}"
        return s
    if meta.get("rows"):
        return f"rows {meta['rows']}"
    return ch.get("heading") or ""


def deep_link(ch: dict, quote_text: str | None) -> str | None:
    if ch.get("source_kind") == "youtube" or ch["kind"] == "transcript":
        vid = (ch.get("source_meta") or {}).get("video_id")
        if vid and ch.get("t_start") is not None:
            return f"https://www.youtube.com/watch?v={vid}&t={int(ch['t_start'])}s"
        return None
    if ch.get("source_kind") == "web" and ch.get("url"):
        frag = ""
        basis = quote_text or ""
        if basis and ch["kind"] == "text":
            words = basis.split()[:8]
            frag = "#:~:text=" + quote(" ".join(words), safe="")
        return ch["url"] + frag
    return None


def _norm_tokens(s: str) -> list[str]:
    return re.findall(r"[^\W_]+", s.lower())


def find_quote(chunk_text: str, q: str) -> tuple[bool, tuple[int, int] | None]:
    """(verified, (start,end)) - exact match ignoring case/punctuation/whitespace, else fuzzy >= 0.9 on tokens."""
    qt = _norm_tokens(q)
    if len(qt) < 3:
        return False, None
    pat = r"[\W_]*".join(re.escape(t) for t in qt)
    m = re.search(pat, chunk_text, re.I)
    if m:
        return True, (m.start(), m.end())
    ct = _norm_tokens(chunk_text)
    n = len(qt)
    best = 0.0
    for i in range(0, max(len(ct) - n + 1, 1)):
        win = ct[i:i + n]
        if not win or win[0] != qt[0] and (len(ct) > 400):
            continue
        r = difflib.SequenceMatcher(None, qt, win, autojunk=False).ratio()
        best = max(best, r)
        if best >= 0.9:
            return True, None
    return False, None


def _clean_for_score(sent: str) -> str:
    s = _MARK_ANY.sub(" ", sent)
    s = re.sub(r"[*_`#>|]", " ", s)
    s = re.sub(r"^\s*(?:[-+•]|\d+[.)])\s+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _split_units(text: str) -> list[str]:
    units = []
    table: list[str] = []
    for line in text.split("\n"):
        if line.strip().startswith("|"):
            table.append(line)          # a markdown table is one unit (its header row is not a separate claim)
            continue
        if table:
            units.append("\n".join(table))
            table = []
        if not line.strip():
            continue
        if re.match(r"^\s*(?:[-*+•]|\d+[.)])\s+", line) or line.lstrip().startswith("#"):
            units.append(line)
        else:
            units.extend(sentences(line) or [line])
    if table:
        units.append("\n".join(table))
    # models often put the marker after the full stop ("... date. [S1]"): glue it back onto its sentence
    merged: list[str] = []
    for u in units:
        if merged and re.fullmatch(r"\s*(?:\[\s*S\d+\s*\]\s*)+[.,;]?\s*", u):
            merged[-1] = merged[-1].rstrip() + " " + u.strip()
        else:
            merged.append(u)
    return merged


def _is_latin(s: str) -> bool:
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return True
    return sum(1 for c in letters if ord(c) < 0x250) / len(letters) > 0.8


def verify_answer(answer: str, quotes: dict[str, str], evidence: dict[str, dict]) -> Verified:
    """`evidence` maps 'S3' -> chunk dict (with source_name etc.). `quotes` maps 'S3' -> model quote."""
    flags: list[Flag] = []
    # 1. normalise markers: [S1, S2] -> [S1][S2]; drop unknown ids
    def expand(m):
        ids = re.findall(r"S\d+", m.group(1), re.I)
        out = []
        for i in ids:
            i = i.upper()
            if i in evidence:
                out.append(f"[{i}]")
            else:
                flags.append(Flag("invalid_citation_removed", detail=f"{i} was not part of the retrieved evidence"))
        return "".join(out)

    text = _MARK_ANY.sub(expand, answer)
    # citations that trail the full stop ("... assembly. [S1] Next sentence") belong to the sentence before them
    text = re.sub(r"([.!?])[ \t]*((?:\[S\d+\][ \t]*)+)(?=\s|$)",
                  lambda m: " " + m.group(2).strip() + m.group(1) + " ", text)
    text = re.sub(r"[ \t]+([.!?,;])", r"\1", text)
    text = re.sub(r"(?<=\])(?=[A-Za-z0-9])", " ", text)

    # 2. renumber by first use
    order: list[str] = []
    for m in re.finditer(r"\[(S\d+)\]", text):
        if m.group(1) not in order:
            order.append(m.group(1))
    remap = {old: f"S{i + 1}" for i, old in enumerate(order)}
    text = re.sub(r"\[(S\d+)\]", lambda m: f"[{remap[m.group(1)]}]", text)

    # 3. sentence level support
    v = Verified(text=text, citations=[], flags=flags)
    inv = {new: old for old, new in remap.items()}
    latin_answer = _is_latin(re.sub(r"\[S\d+\]", "", text))
    v.limited = not latin_answer
    for unit in _split_units(text):
        ids = [inv[i] for i in re.findall(r"\[(S\d+)\]", unit) if i in inv]
        clean = _clean_for_score(unit)
        words = _stems(clean)
        if len(words) < 4 or _REFUSAL.match(clean):
            continue
        if not ids:
            if len(words) >= 4 and not clean.endswith("?"):
                v.uncited += 1
                flags.append(Flag("uncited_claim", unit, "This statement has no citation."))
            continue
        if not latin_answer:
            continue
        v.checked += 1
        ev = " ".join(evidence[i]["text"] for i in ids)
        ev_stems = set(_stems(ev))
        sw = set(words)
        cov = len(sw & ev_stems) / len(sw)
        ev_norm = ev.replace(",", "")
        bad_nums = [n for n in _NUM.findall(clean) if len(n.strip("%")) >= 2
                    and n.replace(",", "").strip("%") not in ev_norm]
        if bad_nums:
            v.unsupported += 1
            flags.append(Flag("numeric_mismatch", unit, f"Number(s) not found in cited text: {', '.join(bad_nums[:4])}"))
        elif cov >= 0.5:
            v.supported += 1
        elif cov >= 0.3:
            v.weak += 1
            flags.append(Flag("weak_support", unit, "Only partly matches the cited passage."))
        else:
            v.unsupported += 1
            flags.append(Flag("unsupported_claim", unit, "Not found in the cited passage."))

    # 4. citation objects
    for old in order:
        ch = evidence[old]
        new = remap[old]
        q = (quotes.get(old) or "").strip()
        verified, span = (False, None)
        if q:
            verified, span = find_quote(ch["text"], q)
            if not verified:
                flags.append(Flag("quote_unverified", q[:200], f"Quoted text for [{new}] was not found in the source."))
                q = ""
        snippet = _snippet(ch["text"], span, v.text, new)
        v.citations.append(citation_for(ch, new, q, verified, span, snippet))
    return v


def citation_for(ch: dict, cid: str, q: str = "", verified: bool = False, span=None, snippet: str | None = None) -> dict:
    snippet = snippet if snippet is not None else ch["text"][:420].strip() + ("…" if len(ch["text"]) > 420 else "")
    return {
        "id": cid, "chunk_id": ch["id"], "source_id": ch["source_id"], "source_name": ch.get("source_name"),
        "source_kind": ch.get("source_kind"), "label": location_label(ch), "page": ch.get("page"),
        "heading": ch.get("heading"), "url": ch.get("url"), "t_start": ch.get("t_start"), "t_end": ch.get("t_end"),
        "deep_link": deep_link(ch, q or snippet), "quote": q or None, "quote_verified": bool(q) and verified,
        "snippet": snippet, "highlight": list(span) if span else None, "chunk_kind": ch["kind"],
        "suspicious": bool((ch.get("meta") or {}).get("suspicious")),
    }


def _snippet(text: str, span, answer: str, cid: str, width: int = 420) -> str:
    if span:
        s = max(0, span[0] - 120)
        return ("…" if s else "") + text[s:s + width].strip() + ("…" if s + width < len(text) else "")
    # best-matching sentence for the answer sentences citing this id
    sents = re.split(r"(?<=[.!?])\s+|\n+", text)
    cited = " ".join(_clean_for_score(u) for u in _split_units(answer) if f"[{cid}]" in u)
    cw = set(_stems(cited))
    if not cw or not sents:
        return text[:width].strip() + ("…" if len(text) > width else "")
    best = max(range(len(sents)), key=lambda i: len(cw & set(_stems(sents[i]))))
    lo = max(0, best - 1)
    out = " ".join(sents[lo:best + 2]).strip()
    return out[:width] + ("…" if len(out) > width else "")
