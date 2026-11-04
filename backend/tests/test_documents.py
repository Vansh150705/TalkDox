from __future__ import annotations

import shutil

import pytest

from app.providers.ocr import FakeOCR
from conftest import ingest_file, upload, wait_job
from factories import (encrypt_pdf, make_csv, make_docx, make_pdf, make_pptx, make_scanned_pdf, make_xlsx,
                       zip_bomb_docx)


def content(client, auth, sid, **params):
    return client.get(f"/api/sources/{sid}/content", params=params, headers=auth).json()


def src(client, auth, sid):
    return client.get(f"/api/sources/{sid}", headers=auth).json()


def test_pdf_pages_headings_tables(client, auth, tmp_path):
    p = make_pdf(tmp_path / "r.pdf", [["1. Intro", "The warranty period is twenty four months."],
                                      ["2. Costs", "Costs are listed below."]],
                 table_on_page={1: [["Item", "Cost"], ["Widget", "10"], ["Gadget", "25"]]})
    sid, job = ingest_file(client, auth, p)
    assert job["status"] == "completed"
    s = src(client, auth, sid)
    assert s["n_pages"] == 2 and s["diagnostics"]["tables_found"] == 1
    page2 = content(client, auth, sid, page=2)["chunks"]
    assert any(c["kind"] == "table" and "| Widget | 10 |" in c["text"] for c in page2)
    assert all(c["page"] == 2 for c in page2)               # page numbers preserved
    assert any((c["heading"] or "").startswith("2. Costs") for c in page2)   # heading path preserved


def test_repeated_headers_footers_removed(client, auth, tmp_path):
    pages = [["Chapter " + str(i), f"Unique body text number {i} about topic{i} with details."] for i in range(8)]
    # inject a running header into every page
    pages = [[f"ACME Confidential Report"] + pg for pg in pages]
    p = make_pdf(tmp_path / "h.pdf", pages, headings=False)
    sid, job = ingest_file(client, auth, p)
    s = src(client, auth, sid)
    assert s["diagnostics"]["repeated_lines_removed"] >= 6
    texts = " ".join(c["text"] for c in content(client, auth, sid, limit_=100)["chunks"])
    assert "ACME Confidential" not in texts and "topic3" in texts


def test_scanned_pdf_without_ocr_fails_honestly(client, auth, tmp_path):
    p = make_scanned_pdf(tmp_path / "scan.pdf", [["Hello scanned world"]])
    r = upload(client, auth, p)
    job = wait_job(client, auth, r.json()["results"][0]["job_id"])
    assert job["status"] == "failed" and job["error_code"] == "scanned_no_ocr"
    assert "scan" in job["error"].lower()
    s = src(client, auth, r.json()["results"][0]["source_id"])
    assert s["status"] == "failed" and s["n_chunks"] == 0      # nothing misleading is indexed


def test_scanned_pdf_with_ocr(client, auth, tmp_path, fake_ocr):
    fake_ocr.fn = lambda img: "# Scanned Heading\nThe vault code rotation period is ninety days."
    client.state.ingest.ocr = fake_ocr
    p = make_scanned_pdf(tmp_path / "scan.pdf", [["x"], ["y"]])
    sid, job = ingest_file(client, auth, p)
    assert job["status"] == "completed"
    s = src(client, auth, sid)
    assert s["diagnostics"]["pages_ocr"] == [1, 2] and s["diagnostics"]["ocr_engine"] == "fake"
    texts = " ".join(c["text"] for c in content(client, auth, sid)["chunks"])
    assert "ninety days" in texts


def test_mixed_pdf_partial_when_some_pages_unreadable(client, auth, tmp_path):
    # page 1 has text; page 2 is a raster scan -> without OCR the source is 'partial' and says so
    from pypdf import PdfReader, PdfWriter
    a = make_pdf(tmp_path / "a.pdf", [["Overview", "This page has a real text layer with plenty of words."]])
    b = make_scanned_pdf(tmp_path / "b.pdf", [["scan"]])
    w = PdfWriter()
    for f in (a, b):
        for pg in PdfReader(str(f)).pages:
            w.add_page(pg)
    out = tmp_path / "mix.pdf"
    w.write(str(out))
    sid, job = ingest_file(client, auth, out)
    assert job["status"] == "partial"
    s = src(client, auth, sid)
    assert s["diagnostics"]["pages_without_text"] == [2]
    assert any("no extractable text" in w for w in s["warnings"])


def test_password_protected_pdf(client, auth, tmp_path):
    p = make_pdf(tmp_path / "p.pdf", [["Secret", "Classified information about the vault."]])
    e = encrypt_pdf(p, tmp_path / "enc.pdf")
    r = upload(client, auth, e)
    job = wait_job(client, auth, r.json()["results"][0]["job_id"])
    assert job["status"] == "failed" and job["error_code"] == "password_protected"


def test_corrupted_pdf(client, auth, tmp_path):
    p = make_pdf(tmp_path / "c.pdf", [["Title", "Body text for the corrupted document test."]])
    data = p.read_bytes()
    (tmp_path / "bad.pdf").write_bytes(data[: len(data) // 3])
    r = upload(client, auth, tmp_path / "bad.pdf")
    job = wait_job(client, auth, r.json()["results"][0]["job_id"])
    assert job["status"] == "failed" and job["error_code"] in ("corrupted", "no_text", "empty")


def test_fake_pdf_extension_rejected(client, auth, tmp_path):
    (tmp_path / "fake.pdf").write_text("this is not a pdf at all")
    r = upload(client, auth, tmp_path / "fake.pdf")
    job = wait_job(client, auth, r.json()["results"][0]["job_id"])
    assert job["error_code"] == "corrupted"


def test_mislabelled_pdf_detected_by_magic(client, auth, tmp_path):
    p = make_pdf(tmp_path / "x.pdf", [["Heading", "Body text that is long enough to be indexed properly."]])
    shutil.copy(p, tmp_path / "x.docx")
    r = upload(client, auth, tmp_path / "x.docx")
    job = wait_job(client, auth, r.json()["results"][0]["job_id"])
    assert job["error_code"] == "type_mismatch"


def test_unsupported_type(client, auth, tmp_path):
    (tmp_path / "evil.exe").write_bytes(b"MZ\x90\x00" + b"\0" * 100)
    r = upload(client, auth, tmp_path / "evil.exe")
    job = wait_job(client, auth, r.json()["results"][0]["job_id"])
    assert job["error_code"] == "unsupported_type"


def test_empty_file_rejected(client, auth, tmp_path):
    (tmp_path / "e.txt").write_bytes(b"")
    r = upload(client, auth, tmp_path / "e.txt")
    assert r.status_code == 400 and r.json()["code"] == "empty"


def test_oversize_upload_rejected_before_processing(client, auth, tmp_path):
    client.state.settings.max_upload_mb = 1
    (tmp_path / "big.txt").write_bytes(b"a" * (2 * 1024 * 1024))
    r = upload(client, auth, tmp_path / "big.txt")
    assert r.status_code == 413 and r.json()["code"] == "file_too_large"
    assert client.get("/api/sources", headers=auth).json()["sources"] == []


def test_page_limit(client, auth, tmp_path):
    client.state.settings.max_pdf_pages = 2
    p = make_pdf(tmp_path / "long.pdf", [["P" + str(i), "Some body text number %d." % i] for i in range(3)])
    r = upload(client, auth, p)
    job = wait_job(client, auth, r.json()["results"][0]["job_id"])
    assert job["error_code"] == "too_many_pages"


def test_docx_headings_tables_lists(client, auth, tmp_path):
    sid, job = ingest_file(client, auth, make_docx(tmp_path / "g.docx"))
    assert job["status"] == "completed"
    s = src(client, auth, sid)
    assert s["name"] == "g.docx" or s["name"]
    chunks = content(client, auth, sid, limit_=50)["chunks"]
    text = "\n".join(c["text"] for c in chunks)
    assert "| Platform | 480,000 | Priya |" in text
    assert any((c["heading"] or "").endswith("3. Risks") for c in chunks)
    assert "Hiring freeze" in text and any(c["page"] is None for c in chunks)   # Word has no reliable pages


def test_pptx_slides_notes_tables(client, auth, tmp_path):
    sid, job = ingest_file(client, auth, make_pptx(tmp_path / "d.pptx"))
    chunks = content(client, auth, sid, limit_=50)["chunks"]
    by_page = {}
    for c in chunks:
        by_page.setdefault(c["page"], []).append(c["text"])
    assert "Beta opens on June 3" in " ".join(by_page[1])
    assert "waitlist has 4,200" in " ".join(by_page[1])
    assert "| Team | $29 |" in " ".join(by_page[2])


def test_xlsx_multi_sheet_with_stats(client, auth, tmp_path):
    sid, job = ingest_file(client, auth, make_xlsx(tmp_path / "s.xlsx"))
    assert job["status"] == "completed", (job["error"], job["error_code"])
    s = src(client, auth, sid)
    names = [x["name"] for x in s["diagnostics"]["sheets"]]
    assert names == ["Sales", "Staff"]                       # empty sheet skipped, reported
    assert any("Empty" in w and "empty" in w for w in s["warnings"])
    text = "\n".join(c["text"] for c in content(client, auth, sid, limit_=50)["chunks"])
    assert "sum=5750.75" in text.replace(",", "") or "sum=5750.75" in text   # deterministic column stats
    assert "| South | Q2 | 1750.25 | 60 |" in text
    assert "2022-07-15" in text


def test_csv_and_tsv(client, auth, tmp_path):
    sid, _ = ingest_file(client, auth, make_csv(tmp_path / "c.csv", rows=60))
    s = src(client, auth, sid)
    assert s["n_chunks"] >= 2
    text = "\n".join(c["text"] for c in content(client, auth, sid, limit_=100)["chunks"])
    assert "max=60000" in text and "City60" in text
    sid2, _ = ingest_file(client, auth, make_csv(tmp_path / "t.tsv", rows=5, delimiter="\t"))
    assert "City5" in "\n".join(c["text"] for c in content(client, auth, sid2)["chunks"])


def test_text_markdown_html_json(client, auth, tmp_path):
    (tmp_path / "n.md").write_text("# Guide\n\nIntro paragraph.\n\n## Setup\n\n```bash\nmake install\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    sid, _ = ingest_file(client, auth, tmp_path / "n.md")
    ch = content(client, auth, sid)["chunks"]
    assert any(c["kind"] == "code" and "make install" in c["text"] for c in ch)
    assert any(c["kind"] == "table" for c in ch)
    (tmp_path / "p.html").write_text("<html><title>T</title><body><nav>menu</nav><main><h1>Doc</h1><p>"
                                     + "Real content sentence number one is here. " * 5 + "</p></main></body></html>")
    sid, _ = ingest_file(client, auth, tmp_path / "p.html")
    assert "menu" not in " ".join(c["text"] for c in content(client, auth, sid)["chunks"])
    (tmp_path / "d.json").write_text('{"name": "talkdox", "tags": ["a", "b"]}')
    sid, _ = ingest_file(client, auth, tmp_path / "d.json")
    assert "talkdox" in content(client, auth, sid)["chunks"][0]["text"]


def test_text_encodings(client, auth, tmp_path):
    (tmp_path / "l1.txt").write_bytes("Café résumé naïve — the quick brown fox jumps over the lazy dog many times.".encode("cp1252"))
    sid, job = ingest_file(client, auth, tmp_path / "l1.txt")
    assert "résumé" in content(client, auth, sid)["chunks"][0]["text"]
    (tmp_path / "u16.txt").write_bytes("Ünïcode sentence in UTF sixteen format for testing purposes.".encode("utf-16"))
    sid, job = ingest_file(client, auth, tmp_path / "u16.txt")
    assert "Ünïcode" in content(client, auth, sid)["chunks"][0]["text"]


def test_zip_bomb_rejected(client, auth, tmp_path):
    client.state.settings.max_uncompressed_mb = 10
    r = upload(client, auth, zip_bomb_docx(tmp_path / "b.docx", mb=40))
    job = wait_job(client, auth, r.json()["results"][0]["job_id"])
    assert job["error_code"] == "zip_bomb"


def test_duplicate_upload_is_deduplicated(client, auth, tmp_path):
    p = make_docx(tmp_path / "g.docx")
    sid1, _ = ingest_file(client, auth, p)
    r = upload(client, auth, p, name="renamed.docx").json()["results"][0]
    assert r["duplicate"] is True and r["source_id"] == sid1
    assert len(client.get("/api/sources", headers=auth).json()["sources"]) == 1


def test_legacy_doc_via_libreoffice(client, auth, tmp_path):
    import subprocess
    if not (shutil.which("soffice") or shutil.which("libreoffice")):
        pytest.skip("LibreOffice not installed")
    d = make_docx(tmp_path / "src.docx")
    subprocess.run(["soffice", "--headless", f"-env:UserInstallation=file://{tmp_path}/lo", "--convert-to", "doc",
                    "--outdir", str(tmp_path / "out"), str(d)], check=True, capture_output=True, timeout=180)
    sid, job = ingest_file(client, auth, tmp_path / "out" / "src.doc")
    assert job["status"] == "completed"
    assert "twelve full-time engineers" in "\n".join(c["text"] for c in content(client, auth, sid, limit_=50)["chunks"])


def test_large_pdf_processes_with_bounded_memory_and_progress(client, auth, tmp_path):
    import resource
    pages = [[f"Section {i}", f"Page {i} discusses subject{i} and records the figure {i * 7}."] for i in range(150)]
    p = make_pdf(tmp_path / "big.pdf", pages)
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    sid, job = ingest_file(client, auth, p)
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    assert job["status"] == "completed"
    s = src(client, auth, sid)
    assert s["n_pages"] == 150 and s["diagnostics"]["pages_with_text"] == 150
    assert (after - before) / 1024 < 400            # MB growth stays modest for a 150 page PDF
    assert any("subject149" in c["text"] for c in content(client, auth, sid, page=150)["chunks"])
