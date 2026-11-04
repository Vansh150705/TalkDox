"""Text normalisation and markdown/table helpers shared by extractors."""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Sequence

from .models import Block

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​‌‍⁠﻿]")
_SENT_END = re.compile(r"[.!?:;。！？]['\")\]]*$")
_BULLET = re.compile(r"^\s*(?:[-*•▪◦●·–]|\d{1,3}[.)]|[a-zA-Z][.)])\s+")
_HEAD_NUM = re.compile(r"^\s*(?:\d{1,2}(?:\.\d{1,2}){0,3}\.?|[IVXLC]{1,6}\.|Chapter\s+\d+|CHAPTER\s+\d+|Section\s+\d+)\s+\S")


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = _CTRL.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)          # de-hyphenate line wraps
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def looks_like_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 100 or len(s.split()) > 12:
        return False
    if _SENT_END.search(s) and not _HEAD_NUM.match(s):
        return False
    if s.isupper() and len(s) > 3 and any(c.isalpha() for c in s):
        return True
    if _HEAD_NUM.match(s) and not s.endswith((",", ";")):
        return True
    words = [w for w in s.split() if w[:1].isalpha()]
    if len(words) >= 2 and sum(1 for w in words if w[0].isupper()) / len(words) >= 0.8 and len(s) < 70:
        return True
    return False


def heading_level(line: str) -> int:
    m = re.match(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,3})", line)
    if m:
        return min(m.group(1).count(".") + 1, 4)
    return 1 if line.strip().isupper() else 2


def reflow_to_blocks(text: str, page: int | None = None, detect_headings: bool = True) -> list[Block]:
    """Turn raw extracted text (hard-wrapped lines, as PDFs produce) into paragraph/heading/list blocks."""
    text = normalize_text(text)
    if not text:
        return []
    blocks: list[Block] = []
    for para in re.split(r"\n\s*\n", text):
        lines = [l for l in para.split("\n") if l.strip()]
        if not lines:
            continue
        cur: list[str] = []

        def flush():
            if cur:
                blocks.append(Block("paragraph", " ".join(cur).strip(), page=page))
                cur.clear()

        lens = sorted(len(l) for l in lines)
        median = lens[len(lens) // 2] if lens else 0
        for i, line in enumerate(lines):
            s = line.strip()
            if detect_headings and looks_like_heading(s) and (len(lines) == 1 or i == 0 or len(s) < 0.8 * median):
                flush()
                blocks.append(Block("heading", s, page=page, level=heading_level(s)))
                continue
            if _BULLET.match(s):
                flush()
                cur.append(s)
                continue
            if cur:
                prev = cur[-1]
                short_prev = len(prev) < 0.65 * median if median else False
                if _SENT_END.search(prev) and (short_prev or s[:1].isupper() and len(prev) < 0.5 * median):
                    flush()
            cur.append(s)
        flush()
    return blocks


def md_table(rows: Sequence[Sequence[object]], header: bool = True) -> str:
    """Render rows as a GitHub-flavoured markdown table (keeps row/column structure for the LLM)."""
    rows = [[_cell(c) for c in r] for r in rows if any(str(c or "").strip() for c in r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    head = rows[0] if header else [f"col{i + 1}" for i in range(width)]
    body = rows[1:] if header else rows
    out = ["| " + " | ".join(head) + " |", "| " + " | ".join("---" for _ in head) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(out)


def _cell(c: object) -> str:
    s = "" if c is None else str(c)
    return re.sub(r"\s+", " ", s).replace("|", "\\|").strip()


_MD_ESC = re.compile(r"\\([\\`*_{}\[\]()#+\-.!<>~$&:=@%^'\"/,;?])")


def unescape_md(text: str) -> str:
    """Markdown backslash escapes (e.g. trafilatura emits `ef\\_search`) must not leak into indexed text."""
    return _MD_ESC.sub(r"\1", text)


def parse_markdown(md: str, page: int | None = None, url: str | None = None) -> list[Block]:
    """Parse markdown (from .md files or trafilatura output) into structured blocks."""
    blocks: list[Block] = []
    lines = md.replace("\r\n", "\n").split("\n")
    i, n = 0, len(lines)
    para: list[str] = []

    def mk(kind, text, **kw):
        t = text.strip("\n") if kind == "code" else unescape_md(text.strip("\n"))
        if t.strip():
            blocks.append(Block(kind, t, page=page, url=url, **kw))

    def flush_para():
        if para:
            mk("paragraph", " ".join(s.strip() for s in para))
            para.clear()

    while i < n:
        line = lines[i]
        if line.strip().startswith(("```", "~~~")):
            flush_para()
            fence = line.strip()[:3]
            lang = line.strip()[3:].strip()
            i += 1
            code = []
            while i < n and not lines[i].strip().startswith(fence):
                code.append(lines[i])
                i += 1
            i += 1
            mk("code", "\n".join(code), meta={"lang": lang} if lang else {})
            continue
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m:
            flush_para()
            mk("heading", m.group(2), level=len(m.group(1)))
            i += 1
            continue
        if "|" in line and i + 1 < n and re.match(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$", lines[i + 1]):
            flush_para()
            tbl = [line, lines[i + 1]]
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                tbl.append(lines[i])
                i += 1
            mk("table", "\n".join(t.strip() for t in tbl))
            continue
        if re.match(r"^\s*([-*+]|\d+[.)])\s+", line):
            flush_para()
            items = []
            while i < n and (re.match(r"^\s*([-*+]|\d+[.)])\s+", lines[i]) or (lines[i].startswith("  ") and lines[i].strip())):
                items.append(lines[i].rstrip())
                i += 1
            mk("list", "\n".join(items))
            continue
        if not line.strip():
            flush_para()
        else:
            para.append(line)
        i += 1
    flush_para()
    return blocks


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?。！？])['\")\]]*\s+(?=[A-Z0-9\"'(\[“])", text)
    return [p.strip() for p in parts if p.strip()]


def fmt_ts(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+(?:'[^\W_]+)?", text.lower())


STOPWORDS = frozenset("""a an the and or but if then else of to in on at by for with without from as is are was were be been being
this that these those it its i you he she we they them his her our your their my me us do does did done have has had
what which who whom whose when where why how can could should would will shall may might must not no nor so than too very
also just about into over under between among per via vs""".split())


def content_words(text: str) -> list[str]:
    return [w for w in words(text) if w not in STOPWORDS and len(w) > 1]


def iter_pairs(seq: Iterable):
    prev = None
    for x in seq:
        if prev is not None:
            yield prev, x
        prev = x
