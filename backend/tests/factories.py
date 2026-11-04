"""Generators for real binary test fixtures (PDF, scanned PDF, DOCX, PPTX, XLSX, ...). Nothing is mocked:
the extraction code under test parses genuine files produced by independent libraries."""
from __future__ import annotations

import io
from pathlib import Path


def make_pdf(path: Path, pages: list[list[str]], table_on_page: dict[int, list[list[str]]] | None = None,
             headings: bool = True) -> Path:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    st = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(path), pagesize=letter)
    el = []
    for i, paras in enumerate(pages):
        for j, p in enumerate(paras):
            if j == 0 and headings:
                el.append(Paragraph(p, st["Heading1"]))
            else:
                el.append(Paragraph(p, st["Normal"]))
                el.append(Spacer(1, 8))
        if table_on_page and i in table_on_page:
            el.append(Table(table_on_page[i], style=TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)])))
            el.append(Spacer(1, 12))
        if i < len(pages) - 1:
            el.append(PageBreak())
    doc.build(el)
    return path


def make_scanned_pdf(path: Path, lines_per_page: list[list[str]]) -> Path:
    """A PDF whose pages are pure raster images (no text layer) - a faithful 'scan'."""
    from PIL import Image, ImageDraw, ImageFont
    imgs = []
    for lines in lines_per_page:
        im = Image.new("RGB", (900, 1100), "white")
        d = ImageDraw.Draw(im)
        y = 80
        for ln in lines:
            d.text((60, y), ln, fill="black")
            y += 40
        imgs.append(im)
    imgs[0].save(str(path), "PDF", save_all=True, append_images=imgs[1:], resolution=100.0)
    return path


def encrypt_pdf(src: Path, dst: Path, password: str = "secret") -> Path:
    from pypdf import PdfReader, PdfWriter
    r = PdfReader(str(src))
    w = PdfWriter()
    for p in r.pages:
        w.add_page(p)
    w.encrypt(password)
    with open(dst, "wb") as f:
        w.write(f)
    return dst


def make_docx(path: Path) -> Path:
    from docx import Document
    d = Document()
    d.core_properties.title = "Quarterly Operations Guide"
    d.add_heading("Quarterly Operations Guide", level=0)
    d.add_heading("1. Staffing", level=1)
    d.add_paragraph("The operations team has twelve full-time engineers and three contractors as of March.")
    d.add_paragraph("Hiring freeze remains in effect until the end of the second quarter.", style="List Bullet")
    d.add_heading("2. Budget", level=1)
    t = d.add_table(rows=3, cols=3)
    for r, row in enumerate([["Department", "Budget (USD)", "Owner"], ["Platform", "480,000", "Priya"],
                             ["Support", "210,000", "Marcus"]]):
        for c, v in enumerate(row):
            t.cell(r, c).text = v
    d.add_heading("3. Risks", level=1)
    d.add_paragraph("The largest risk is dependence on a single cloud region for the payments service.")
    d.save(str(path))
    return path


def make_pptx(path: Path) -> Path:
    from pptx import Presentation
    from pptx.util import Inches
    prs = Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[1])
    s1.shapes.title.text = "Launch Plan"
    s1.placeholders[1].text = "Beta opens on June 3\nGeneral availability on September 9"
    s1.notes_slide.notes_text_frame.text = "Mention the waitlist has 4,200 people."
    s2 = prs.slides.add_slide(prs.slide_layouts[5])
    s2.shapes.title.text = "Pricing"
    tbl = s2.shapes.add_table(3, 2, Inches(1), Inches(2), Inches(6), Inches(1.5)).table
    for r, row in enumerate([["Plan", "Price"], ["Starter", "$9"], ["Team", "$29"]]):
        for c, v in enumerate(row):
            tbl.cell(r, c).text = v
    prs.save(str(path))
    return path


def make_xlsx(path: Path) -> Path:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sales"
    ws.append(["Region", "Quarter", "Revenue", "Units"])
    for i, (reg, q, rev, u) in enumerate([("North", "Q1", 1200.5, 40), ("North", "Q2", 1500, 52), ("South", "Q1", 900, 31),
                                          ("South", "Q2", 1750.25, 60), ("West", "Q1", 400, 12)]):
        ws.append([reg, q, rev, u])
    ws2 = wb.create_sheet("Staff")
    ws2.append(["Name", "Role", "Start date"])
    ws2.append(["Ana", "Engineer", "2021-03-01"])
    ws2.append(["Ben", "Designer", "2022-07-15"])
    wb.create_sheet("Empty")
    wb.save(str(path))
    return path


def make_csv(path: Path, rows: int = 50, delimiter: str = ",") -> Path:
    lines = [delimiter.join(["id", "city", "population"])]
    for i in range(rows):
        lines.append(delimiter.join([str(i + 1), f"City{i + 1}", str(1000 * (i + 1))]))
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def zip_bomb_docx(path: Path, mb: int = 40) -> Path:
    import zipfile
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document>" + "A" * 1000 + "</w:document>")
        z.writestr("word/bomb.bin", b"\0" * (mb * 1024 * 1024))
    return path
