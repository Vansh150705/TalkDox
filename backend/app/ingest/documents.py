"""File extraction: PDF (text, tables, outline, OCR for scanned pages), DOCX, PPTX, XLSX, CSV/TSV,
TXT/MD/HTML/JSON and (via LibreOffice, if installed) DOC/PPT/XLS/RTF/ODT/ODP/ODS.

Contract: either return an `Extracted` that honestly reports what was and was not extracted
(`diagnostics`, `warnings`, `partial`), or raise `IngestError` with a specific `code`.
Nothing here ever returns an empty result silently.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
import zipfile
from collections import Counter
from pathlib import Path
from typing import Callable, Optional

from ..config import Settings
from ..errors import Cancelled, IngestError
from ..providers.ocr import BaseOCR
from .models import Block, Extracted
from .textutil import md_table, normalize_text, parse_markdown, reflow_to_blocks

log = logging.getLogger("talkdox.documents")

Progress = Callable[[float, str], None]

SUPPORTED = {
    ".pdf": "pdf", ".docx": "docx", ".pptx": "pptx", ".xlsx": "xlsx", ".xlsm": "xlsx",
    ".csv": "csv", ".tsv": "tsv", ".txt": "txt", ".text": "txt", ".log": "txt", ".rst": "txt",
    ".md": "md", ".markdown": "md", ".html": "html", ".htm": "html", ".xhtml": "html",
    ".json": "code", ".yaml": "code", ".yml": "code", ".xml": "code",
    ".doc": "legacy:docx", ".rtf": "legacy:docx", ".odt": "legacy:docx",
    ".ppt": "legacy:pptx", ".odp": "legacy:pptx",
    ".xls": "legacy:xlsx", ".ods": "legacy:xlsx",
}
SUPPORTED_LABEL = "PDF, DOCX, PPTX, XLSX, CSV, TSV, TXT, MD, HTML, JSON (and DOC/PPT/XLS/RTF/ODT when LibreOffice is installed)"
_soffice_lock = threading.Lock()


def _noop(_f: float, _m: str) -> None:
    pass


# --------------------------------------------------------------------------- detection
def detect_kind(path: Path, filename: str) -> str:
    ext = Path(filename).suffix.lower()
    head = path.open("rb").read(4096)
    is_pdf = b"%PDF-" in head[:1024]
    is_zip = head[:4] in (b"PK\x03\x04", b"PK\x05\x06")
    is_ole = head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    kind = SUPPORTED.get(ext)
    if is_pdf:
        if kind not in (None, "pdf"):
            raise IngestError(f"'{filename}' has a {ext} extension but its contents are a PDF.", code="type_mismatch")
        return "pdf"
    if kind is None:
        raise IngestError(f"Unsupported file type '{ext or filename}'. Supported: {SUPPORTED_LABEL}.",
                          code="unsupported_type")
    if kind == "pdf":
        raise IngestError("This file is not a valid PDF (missing PDF header). It may be corrupted or mis-named.",
                          code="corrupted")
    if kind in ("docx", "pptx", "xlsx"):
        if not is_zip:
            raise IngestError(f"This {ext} file is not a valid Office document (it may be corrupted, or an old "
                              f"binary format with a new extension).", code="corrupted" if not is_ole else "type_mismatch")
        _zip_guard(path, kind)
    if kind.startswith("legacy:"):
        if not (is_ole or is_zip or ext in (".rtf",)):
            raise IngestError(f"This {ext} file looks corrupted.", code="corrupted")
    if kind in ("csv", "tsv", "txt", "md", "html", "code") and b"\x00" in head[:2048] and not head.startswith(
            (b"\xff\xfe", b"\xfe\xff")):
        raise IngestError("This file looks like binary data, not text.", code="corrupted")
    return kind


_ZIP_PARTS = {"docx": "word/", "pptx": "ppt/", "xlsx": "xl/"}


def _zip_guard(path: Path, kind: str, s: Settings | None = None) -> None:
    s = s or _settings_cache[0]
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            if len(infos) > s.max_zip_entries:
                raise IngestError("This document contains too many internal files.", code="zip_bomb")
            total = sum(i.file_size for i in infos)
            if total > s.max_uncompressed_mb * 1024 * 1024:
                raise IngestError("This document expands to an unreasonable size and was rejected.", code="zip_bomb")
            for i in infos:
                if i.compress_size and i.file_size / max(i.compress_size, 1) > 300 and i.file_size > 5 * 1024 * 1024:
                    raise IngestError("This document has suspicious compression and was rejected.", code="zip_bomb")
            names = [i.filename for i in infos]
            if not any(n.startswith(_ZIP_PARTS[kind]) for n in names):
                raise IngestError(f"This file is a ZIP archive but not a valid .{kind} document.", code="type_mismatch")
            if any(n.endswith("EncryptedPackage") for n in names) or "EncryptedPackage" in names:
                raise IngestError("This document is password-protected. Remove the password and upload again.",
                                  code="password_protected")
    except zipfile.BadZipFile:
        raise IngestError("This file is corrupted (invalid ZIP structure).", code="corrupted")


_settings_cache: list[Settings] = [Settings()]


# --------------------------------------------------------------------------- entry point
def extract_file(path: Path, filename: str, s: Settings, ocr: Optional[BaseOCR] = None,
                 progress: Progress = _noop, check_cancel: Callable[[], None] = lambda: None) -> Extracted:
    _settings_cache[0] = s
    if path.stat().st_size == 0:
        raise IngestError("This file is empty.", code="empty")
    kind = detect_kind(path, filename)
    title = Path(filename).stem
    if kind == "pdf":
        ex = extract_pdf(path, title, s, ocr, progress, check_cancel)
    elif kind == "docx":
        ex = extract_docx(path, title)
    elif kind == "pptx":
        ex = extract_pptx(path, title)
    elif kind == "xlsx":
        ex = extract_xlsx(path, title, s)
    elif kind in ("csv", "tsv"):
        ex = extract_csv(path, title, s, "\t" if kind == "tsv" else None)
    elif kind.startswith("legacy:"):
        ex = extract_legacy(path, filename, kind.split(":")[1], s, title)
    else:
        ex = extract_text_file(path, title, kind, s)
    ex.diagnostics.setdefault("format", kind)
    if not ex.blocks or ex.n_chars < 1:
        raise IngestError("No text could be extracted from this file.", code="no_text")
    return ex


# --------------------------------------------------------------------------- PDF
def extract_pdf(path: Path, title: str, s: Settings, ocr: Optional[BaseOCR], progress: Progress,
                check_cancel: Callable[[], None]) -> Extracted:
    import pypdfium2 as pdfium
    from pypdfium2 import PdfiumError

    try:
        pdf = pdfium.PdfDocument(str(path))
    except PdfiumError as e:
        msg = str(e).lower()
        if "password" in msg:
            raise IngestError("This PDF is password-protected. Remove the password and upload again.",
                              code="password_protected")
        raise IngestError("This PDF is corrupted or malformed and can't be read.", code="corrupted")
    n = len(pdf)
    if n == 0:
        raise IngestError("This PDF has no pages.", code="empty")
    if n > s.max_pdf_pages:
        raise IngestError(f"This PDF has {n} pages; the limit is {s.max_pdf_pages}.", code="too_many_pages",
                          detail={"pages": n, "limit": s.max_pdf_pages})

    warnings: list[str] = []
    meta_title = None
    try:
        meta_title = (pdf.get_metadata_dict() or {}).get("Title", "").strip() or None
    except Exception:
        pass
    toc: dict[int, list[tuple[int, str]]] = {}
    try:
        for item in pdf.get_toc():
            if item.page_index is not None and item.title.strip():
                toc.setdefault(item.page_index, []).append((item.level + 1, item.title.strip()))
    except Exception:
        pass

    page_text: list[str] = []
    for i in range(n):
        check_cancel()
        page = pdf[i]
        try:
            tp = page.get_textpage()
            page_text.append(tp.get_text_range() or "")
            tp.close()
        except Exception:
            page_text.append("")
            warnings.append(f"Page {i + 1}: text layer could not be read.")
        page.close()
        if i % 25 == 0:
            progress(0.3 * i / n, f"Reading page {i + 1} of {n}")

    # repeated headers / footers (page numbers, running titles) pollute every chunk -> drop
    removed = _strip_repeating_lines(page_text) if n >= 4 else 0

    # tables: pdfplumber, within a time budget
    tables_by_page: dict[int, list[tuple[tuple, str]]] = {}
    text_by_page_override: dict[int, str] = {}
    t_budget = time.time() + 150
    try:
        import pdfplumber
        with pdfplumber.open(str(path)) as plumb:
            for i in range(n):
                check_cancel()
                if time.time() > t_budget:
                    warnings.append("Table detection stopped early on a very large PDF; later pages may have "
                                    "tables flattened to text.")
                    break
                if n > 300 and not _maybe_ruled(page_text[i]):
                    continue
                try:
                    pg = plumb.pages[i]
                    tbls = [t for t in pg.find_tables() if t.bbox]
                    good = []
                    for t in tbls:
                        rows = t.extract()
                        nonempty = [r for r in rows if any((c or "").strip() for c in r)]
                        if len(nonempty) >= 2 and max(len(r) for r in nonempty) >= 2:
                            good.append((t.bbox, md_table(nonempty)))
                    if good:
                        good.sort(key=lambda x: x[0][1])
                        tables_by_page[i] = good
                        text_by_page_override[i] = _banded_text(pg, [b for b, _ in good])
                    pg.flush_cache()
                except Exception as e:  # one bad page must not sink the document
                    log.debug("table extraction failed on page %d: %s", i + 1, e)
                if i % 10 == 0:
                    progress(0.3 + 0.3 * i / n, f"Detecting tables ({i + 1}/{n})")
    except ImportError:
        warnings.append("pdfplumber not installed; tables were not preserved.")
    except Exception as e:
        warnings.append(f"Table detection failed: {type(e).__name__}.")

    # OCR for pages with (almost) no text layer
    ocr_pages: list[int] = []
    empty_pages: list[int] = []
    ocr_text: dict[int, str] = {}
    need = [i for i in range(n) if len((text_by_page_override.get(i) or page_text[i]).strip()) < 20 and i not in tables_by_page]
    if need:
        if ocr is not None:
            budget = s.max_ocr_pages
            for k, i in enumerate(need):
                check_cancel()
                if k >= budget:
                    empty_pages.extend(need[k:])
                    warnings.append(f"OCR limit reached ({budget} pages); {len(need) - budget} scanned pages were not read.")
                    break
                try:
                    page = pdf[i]
                    img = page.render(scale=2.0).to_pil()
                    page.close()
                    txt = ocr.page_to_text(img)
                    if txt.strip():
                        ocr_text[i] = txt
                        ocr_pages.append(i + 1)
                    else:
                        empty_pages.append(i)
                except Exception as e:
                    empty_pages.append(i)
                    log.warning("OCR failed on page %d: %s", i + 1, type(e).__name__)
                progress(0.6 + 0.4 * (k + 1) / max(len(need), 1), f"OCR page {i + 1} ({k + 1}/{len(need)})")
        else:
            empty_pages = need

    # assemble blocks
    blocks: list[Block] = []
    for i in range(n):
        pno = i + 1
        for lvl, t in toc.get(i, []):
            blocks.append(Block("heading", t, page=pno, level=min(lvl, 4), meta={"outline": True}))
        if i in ocr_text:
            blocks.extend(_ocr_blocks(ocr_text[i], pno))
            continue
        text = text_by_page_override.get(i) if i in text_by_page_override else page_text[i]
        # the banded text for table pages interleaves tables via a marker
        if i in tables_by_page:
            parts = (text or "").split("\u0001")
            tb = tables_by_page[i]
            for j, part in enumerate(parts):
                blocks.extend(_dedupe_headings(reflow_to_blocks(part, page=pno), toc.get(i, [])))
                if j < len(tb):
                    blocks.append(Block("table", tb[j][1], page=pno))
        else:
            blocks.extend(_dedupe_headings(reflow_to_blocks(text or "", page=pno), toc.get(i, [])))
    pdf.close()

    with_text = sum(1 for i in range(n) if (page_text[i].strip() or i in ocr_text or i in tables_by_page))
    diag = {"extractor": "pdfium+pdfplumber", "pages_total": n, "pages_with_text": with_text,
            "pages_ocr": ocr_pages, "ocr_engine": ocr.name if ocr and ocr_pages else None,
            "pages_without_text": [p + 1 for p in empty_pages][:200], "tables_found": sum(len(v) for v in tables_by_page.values()),
            "repeated_lines_removed": removed, "outline_entries": sum(len(v) for v in toc.values())}
    if with_text == 0:
        raise IngestError(
            "This PDF contains no selectable text - it looks like a scan. " +
            ("OCR was attempted but found no readable text." if ocr else
             "OCR is not configured on this server (set GEMINI_API_KEY, or install tesseract)."),
            code="scanned_no_ocr", detail=diag)
    partial = bool(empty_pages)
    if partial:
        warnings.append(f"{len(empty_pages)} page(s) have no extractable text and are not searchable "
                        f"(pages {', '.join(str(p + 1) for p in empty_pages[:12])}{'…' if len(empty_pages) > 12 else ''}).")
    return Extracted(title=meta_title or title, blocks=blocks, n_pages=n, diagnostics=diag, warnings=warnings,
                     partial=partial)


def _ocr_blocks(text: str, page: int) -> list[Block]:
    blocks = parse_markdown(text, page=page)
    return blocks or reflow_to_blocks(text, page=page)


def _dedupe_headings(blocks: list[Block], outline: list[tuple[int, str]]) -> list[Block]:
    if not outline:
        return blocks
    titles = {re.sub(r"\W+", "", t).lower() for _, t in outline}
    return [b for b in blocks if not (b.kind == "heading" and re.sub(r"\W+", "", b.text).lower() in titles)]


def _maybe_ruled(text: str) -> bool:
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 4:
        return False
    numeric = sum(1 for l in lines if len(re.findall(r"\d", l)) > len(l) * 0.25)
    return numeric >= 3


def _banded_text(pg, bboxes: list[tuple]) -> str:
    """Text of a page with each detected table cut out and replaced by a \\x01 marker, preserving order."""
    w, h = pg.width, pg.height
    parts, y = [], 0.0
    for (x0, top, x1, bottom) in bboxes:
        top, bottom = max(top, 0), min(bottom, h)
        if top > y + 1:
            parts.append(pg.crop((0, y, w, top)).extract_text() or "")
        else:
            parts.append("")
        y = max(y, bottom)
    parts.append(pg.crop((0, y, w, h)).extract_text() or "" if y < h - 1 else "")
    return "\u0001".join(parts)


_PAGENUM = re.compile(r"^[\W\d_]*(?:page|pg\.?|p\.)?\s*\d+\s*(?:of|/|de)?\s*\d*[\W\d_]*$", re.I)


def _line_key(l: str) -> str:
    k = l.strip().lower()
    return "#pagenum#" if _PAGENUM.match(k) and len(k) < 24 else k


def _strip_repeating_lines(pages: list[str]) -> int:
    """Drop running headers/footers/page numbers: a line that sits in the first/last 3 lines of >=40% of
    pages. Exact-text match (digits are only normalised for pure page-number lines) so templated body text
    is never mistaken for boilerplate; skipped entirely if it would remove most of the document."""
    counts: Counter = Counter()
    for t in pages:
        lines = [l for l in t.splitlines() if l.strip()]
        counts.update(set(_line_key(l) for l in lines[:3] + lines[-3:] if len(l.strip()) < 100))
    thresh = max(3, int(0.4 * len(pages)))
    junk = {k for k, c in counts.items() if c >= thresh and k}
    if not junk:
        return 0
    new_pages, removed, before, after = [], 0, sum(len(p) for p in pages), 0
    for t in pages:
        lines = t.splitlines()
        keep = []
        for idx, l in enumerate(lines):
            edge = idx < 3 or idx >= len(lines) - 3
            if edge and len(l.strip()) < 100 and _line_key(l) in junk:
                removed += 1
                continue
            keep.append(l)
        new_pages.append("\n".join(keep))
        after += len(new_pages[-1])
    if before and after < 0.5 * before:
        return 0
    pages[:] = new_pages
    return removed


# --------------------------------------------------------------------------- DOCX
def extract_docx(path: Path, title: str) -> Extracted:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        doc = Document(str(path))
    except Exception:
        raise IngestError("This Word document is corrupted and can't be read.", code="corrupted")
    blocks: list[Block] = []
    list_buf: list[str] = []
    n_tables = n_images = 0

    def flush_list():
        if list_buf:
            blocks.append(Block("list", "\n".join(list_buf)))
            list_buf.clear()

    body = doc.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, doc)
            text = p.text.strip()
            n_images += len(child.xpath(".//pic:pic")) if hasattr(child, "xpath") else 0
            if not text:
                continue
            style = (p.style.name if p.style is not None else "") or ""
            m = re.match(r"Heading (\d)", style)
            if style == "Title":
                flush_list()
                blocks.append(Block("heading", text, level=1))
            elif m:
                flush_list()
                blocks.append(Block("heading", text, level=min(int(m.group(1)) + (0 if title else 0), 6)))
            elif style.lower().startswith("list") or child.xpath(".//w:numPr"):
                list_buf.append(f"- {text}")
            else:
                flush_list()
                blocks.append(Block("paragraph", text))
        elif tag == "tbl":
            flush_list()
            t = Table(child, doc)
            rows = [[c.text for c in r.cells] for r in t.rows]
            md = md_table(_dedupe_merged(rows))
            if md:
                n_tables += 1
                blocks.append(Block("table", md))
    flush_list()
    warnings = []
    if n_images:
        warnings.append(f"{n_images} embedded image(s) are not searchable (text inside images is not extracted).")
    props_title = (doc.core_properties.title or "").strip()
    return Extracted(title=props_title or title, blocks=blocks, n_pages=0,
                     diagnostics={"extractor": "python-docx", "tables_found": n_tables, "images_ignored": n_images,
                                  "pagination": "not available for Word files; citations use section headings"},
                     warnings=warnings)


def _dedupe_merged(rows: list[list[str]]) -> list[list[str]]:
    out = []
    for r in rows:
        nr, prev = [], None
        for c in r:
            nr.append("" if (c == prev and c) else c)
            prev = c
        out.append(nr)
    return out


# --------------------------------------------------------------------------- PPTX
def extract_pptx(path: Path, title: str) -> Extracted:
    from pptx import Presentation
    from pptx.util import Emu

    try:
        prs = Presentation(str(path))
    except Exception:
        raise IngestError("This presentation is corrupted and can't be read.", code="corrupted")
    blocks: list[Block] = []
    n_pics = 0
    slides = list(prs.slides)

    def walk(shapes):
        for sh in sorted(shapes, key=lambda x: (getattr(x, "top", 0) or 0, getattr(x, "left", 0) or 0)):
            yield sh
            if sh.shape_type == 6:  # group
                yield from walk(sh.shapes)

    for idx, slide in enumerate(slides, start=1):
        title_text = ""
        try:
            if slide.shapes.title is not None and slide.shapes.title.has_text_frame:
                title_text = slide.shapes.title.text_frame.text.strip()
        except Exception:
            pass
        blocks.append(Block("heading", title_text or f"Slide {idx}", page=idx, level=1, meta={"slide": idx}))
        for sh in walk(slide.shapes):
            if sh.shape_type == 13:
                n_pics += 1
            if getattr(sh, "has_table", False) and sh.has_table:
                rows = [[c.text for c in r.cells] for r in sh.table.rows]
                md = md_table(rows)
                if md:
                    blocks.append(Block("table", md, page=idx))
            elif sh.has_text_frame and sh != getattr(slide.shapes, "title", None):
                paras = [p.text.strip() for p in sh.text_frame.paragraphs if p.text.strip()]
                if paras:
                    kind = "list" if len(paras) > 1 else "paragraph"
                    blocks.append(Block(kind, "\n".join(f"- {p}" for p in paras) if kind == "list" else paras[0], page=idx))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                blocks.append(Block("paragraph", f"Speaker notes: {notes}", page=idx, meta={"notes": True}))
    warnings = [f"{n_pics} picture(s) on slides are not searchable."] if n_pics else []
    return Extracted(title=title, blocks=blocks, n_pages=len(slides),
                     diagnostics={"extractor": "python-pptx", "slides": len(slides), "images_ignored": n_pics},
                     warnings=warnings)


# --------------------------------------------------------------------------- tabular
ROWS_PER_BLOCK = 25


def _tabular_blocks(sheet_name: str, rows_iter, s: Settings, page: int | None, label: str = "Sheet") -> tuple[list[Block], dict]:
    header: list[str] | None = None
    buf: list[list[str]] = []
    blocks: list[Block] = []
    start_row = 0
    n = 0
    truncated = False
    col_vals: list[list] = []

    def flush(end_row):
        nonlocal buf, start_row
        if not buf:
            return
        md = md_table([header or []] + buf)
        blocks.append(Block("table", md, page=page, meta={"sheet": sheet_name, "rows": f"{start_row}-{end_row}",
                                                          "label_kind": label}))
        buf = []

    for r in rows_iter:
        cells = ["" if c is None else (c.isoformat() if hasattr(c, "isoformat") else str(c)) for c in r]
        if not any(c.strip() for c in cells):
            continue
        if header is None:
            if all(not _is_num(c) for c in cells if c.strip()):
                header = [c.strip() or f"col{i + 1}" for i, c in enumerate(cells)]
                col_vals = [[] for _ in header]
                continue
            header = [f"col{i + 1}" for i in range(len(cells))]
            col_vals = [[] for _ in header]
        n += 1
        if n > s.max_sheet_rows:
            truncated = True
            break
        if not buf:
            start_row = n
        buf.append(cells[:len(header)] + [""] * max(0, len(header) - len(cells)))
        for i, c in enumerate(cells[:len(header)]):
            if c.strip() and len(col_vals[i]) < 100000:
                col_vals[i].append(c)
        if len(buf) >= ROWS_PER_BLOCK:
            flush(n)
    flush(n)
    stats = _profile(header or [], col_vals, n)
    info = {"rows": n, "columns": len(header or []), "truncated": truncated}
    if blocks:
        blocks.insert(0, Block("paragraph", f"{label} '{sheet_name}' has {n} data rows and {len(header or [])} columns: "
                               f"{', '.join(header or [])}.\n{stats}", page=page, meta={"sheet": sheet_name, "profile": True}))
    return blocks, info


def _is_num(s: str) -> bool:
    try:
        float(s.replace(",", ""))
        return True
    except ValueError:
        return False


def _fmt_num(x: float) -> str:
    """Plain decimal notation: '%g' would turn 1650000 into '1.65e+06', which neither users nor the LLM can match."""
    if float(x).is_integer() and abs(x) < 1e15:
        return str(int(x))
    return f"{x:.10g}" if abs(x) < 1e15 and "e" not in f"{x:.10g}" else f"{x:.6f}".rstrip("0").rstrip(".")


def _profile(header, col_vals, n) -> str:
    """Deterministic column statistics: lets the model answer count/min/max/average questions
    correctly instead of guessing from a few retrieved rows."""
    lines = []
    for h, vals in zip(header, col_vals):
        if not vals:
            continue
        nums = [float(v.replace(",", "")) for v in vals if _is_num(v)]
        if len(nums) >= 0.8 * len(vals) and nums:
            lines.append(f"- {h}: numeric, count={len(nums)}, min={_fmt_num(min(nums))}, max={_fmt_num(max(nums))}, "
                         f"sum={_fmt_num(sum(nums))}, mean={_fmt_num(round(statistics.fmean(nums), 4))}")
        else:
            d = Counter(vals)
            top = ", ".join(f"{k} ({c})" for k, c in d.most_common(5))
            lines.append(f"- {h}: text, {len(d)} distinct values; most common: {top}")
    return "Column statistics (computed over all rows):\n" + "\n".join(lines[:60]) if lines else ""


def extract_xlsx(path: Path, title: str, s: Settings) -> Extracted:
    import openpyxl
    try:   # file object: openpyxl otherwise insists on a recognised file extension
        wb = openpyxl.load_workbook(open(path, "rb"), read_only=True, data_only=True)
    except Exception as e:
        if "encrypt" in str(e).lower() or "password" in str(e).lower():
            raise IngestError("This workbook is password-protected.", code="password_protected")
        raise IngestError("This spreadsheet is corrupted and can't be read.", code="corrupted")
    blocks: list[Block] = []
    warnings: list[str] = []
    sheets = []
    partial = False
    for idx, ws in enumerate(wb.worksheets, start=1):
        if getattr(ws, "sheet_state", "visible") != "visible":
            warnings.append(f"Hidden sheet '{ws.title}' was skipped.")
            continue
        blocks.append(Block("heading", f"Sheet: {ws.title}", page=idx, level=1))
        bl, info = _tabular_blocks(ws.title, ws.iter_rows(values_only=True), s, idx)
        if not bl:
            warnings.append(f"Sheet '{ws.title}' is empty.")
            blocks.pop()
            continue
        if info["truncated"]:
            partial = True
            warnings.append(f"Sheet '{ws.title}' was truncated at {s.max_sheet_rows} rows.")
        blocks.extend(bl)
        sheets.append({"name": ws.title, **info})
    wb.close()
    return Extracted(title=title, blocks=blocks, n_pages=len(sheets), partial=partial, warnings=warnings,
                     diagnostics={"extractor": "openpyxl", "sheets": sheets,
                                  "note": "formula cells use cached values saved by the spreadsheet application"})


def _decode_file(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode("cp1252")
        except UnicodeDecodeError:
            return raw.decode("latin-1")


def extract_csv(path: Path, title: str, s: Settings, delimiter: str | None) -> Extracted:
    text = _decode_file(path)
    if not text.strip():
        raise IngestError("This file is empty.", code="empty")
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:20000], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    csv.field_size_limit(10_000_000)
    rows = csv.reader(io.StringIO(text), delimiter=delimiter)
    blocks, info = _tabular_blocks(title, rows, s, None, label="Table")
    if not blocks:
        raise IngestError("This file has no data rows.", code="no_text")
    warnings = [f"Truncated at {s.max_sheet_rows} rows."] if info["truncated"] else []
    return Extracted(title=title, blocks=blocks, n_pages=1, partial=info["truncated"], warnings=warnings,
                     diagnostics={"extractor": "csv", "delimiter": delimiter, **info})


# --------------------------------------------------------------------------- text-like
def extract_text_file(path: Path, title: str, kind: str, s: Settings) -> Extracted:
    text = _decode_file(path)
    if len(text) > s.max_text_chars:
        raise IngestError("This text file is too large.", code="too_large")
    if not text.strip():
        raise IngestError("This file is empty.", code="empty")
    if kind == "html":
        from .htmlextract import extract_html
        ex = extract_html(text, url=None, fallback_title=title)
        return ex
    if kind == "md":
        blocks = parse_markdown(text)
        h1 = next((b.text for b in blocks if b.kind == "heading" and b.level == 1), None)
        return Extracted(title=h1 or title, blocks=blocks, n_pages=1, diagnostics={"extractor": "markdown"})
    if kind == "code":
        blocks = [Block("code", chunk) for chunk in _split_lines(text, 1500)]
        return Extracted(title=title, blocks=blocks, n_pages=1, diagnostics={"extractor": "raw-text"})
    blocks = reflow_to_blocks(text, detect_headings=False)
    return Extracted(title=title, blocks=blocks, n_pages=1, diagnostics={"extractor": "plain-text"})


def _split_lines(text: str, size: int) -> list[str]:
    out, cur = [], ""
    for line in text.splitlines():
        if len(cur) + len(line) + 1 > size and cur:
            out.append(cur)
            cur = ""
        cur += line[:size] + "\n"
    if cur.strip():
        out.append(cur)
    return out


# --------------------------------------------------------------------------- legacy via LibreOffice
def extract_legacy(path: Path, filename: str, target: str, s: Settings, title: str) -> Extracted:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    ext = Path(filename).suffix.lower()
    if ext == ".rtf" and not soffice:
        text = _decode_file(path)
        text = re.sub(r"\\par[d]?", "\n", text)
        text = re.sub(r"\{\\\*[^{}]*\}|\\[a-z]+-?\d* ?|[{}]", "", text)
        blocks = reflow_to_blocks(text, detect_headings=False)
        return Extracted(title=title, blocks=blocks, diagnostics={"extractor": "rtf-basic"},
                         warnings=["RTF was converted with a basic parser; formatting and tables may be lost."])
    if not soffice:
        raise IngestError(f"{ext} files require LibreOffice on the server, which is not installed. "
                          f"Please re-save the file as .{target} and upload again.", code="legacy_unsupported")
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / ("in" + ext)
        shutil.copy(path, src)
        outdir = Path(td) / "out"
        outdir.mkdir()
        with _soffice_lock:
            try:
                subprocess.run([soffice, "--headless", "--norestore", f"-env:UserInstallation=file://{td}/profile",
                                "--convert-to", target, "--outdir", str(outdir), str(src)],
                               capture_output=True, timeout=180, check=True)
            except (subprocess.SubprocessError, OSError):
                raise IngestError(f"Couldn't convert this {ext} file. It may be corrupted or password-protected.",
                                  code="corrupted")
        outs = list(outdir.glob(f"*.{target}"))
        if not outs:
            raise IngestError(f"Couldn't convert this {ext} file. It may be corrupted or password-protected.",
                              code="corrupted")
        fn = {"docx": extract_docx, "pptx": extract_pptx}.get(target)
        ex = fn(outs[0], title) if fn else extract_xlsx(outs[0], title, s)
        ex.diagnostics["converted_with"] = "libreoffice"
        return ex
