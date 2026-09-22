import zipfile
from pathlib import Path

from src.package import build_zip
from src.reply import compose_matter_reply, compose_not_found_reply
from src.scrape import DocRow, Downloaded, FetchResult, MatterInfo


def make_files(tmp_path: Path, sizes: dict[str, int]) -> list[Path]:
    files = []
    for name, size in sizes.items():
        p = tmp_path / name
        p.write_bytes(b"x" * size)
        files.append(p)
    return files


# ---- package ----

def test_build_zip_keeps_order_and_contents(tmp_path):
    files = make_files(tmp_path, {"a.pdf": 10, "b.pdf": 20})
    result = build_zip(files, tmp_path / "out" / "docs.zip")
    assert result.included == files and result.too_large == []
    with zipfile.ZipFile(result.path) as zf:
        assert zf.namelist() == ["a.pdf", "b.pdf"]
        assert zf.read("b.pdf") == b"x" * 20


def test_build_zip_empty_is_valid(tmp_path):
    result = build_zip([], tmp_path / "empty.zip")
    with zipfile.ZipFile(result.path) as zf:
        assert zf.namelist() == []


def test_build_zip_skips_files_over_cap_but_keeps_later_small_ones(tmp_path):
    files = make_files(tmp_path, {"a.pdf": 60, "big.pdf": 60, "c.pdf": 30})
    result = build_zip(files, tmp_path / "docs.zip", max_bytes=100)
    assert [p.name for p in result.included] == ["a.pdf", "c.pdf"]
    assert [p.name for p in result.too_large] == ["big.pdf"]


def test_build_zip_dedupes_names(tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "y").mkdir()
    f1, f2 = tmp_path / "x" / "a.pdf", tmp_path / "y" / "a.pdf"
    f1.write_bytes(b"1")
    f2.write_bytes(b"2")
    result = build_zip([f1, f2], tmp_path / "docs.zip")
    with zipfile.ZipFile(result.path) as zf:
        assert zf.namelist() == ["a.pdf", "a (1).pdf"]


# ---- reply ----

def info(counts=None, date_final="10/23/2025"):
    return MatterInfo(
        matter_number="M12205",
        title="Halifax Regional Water Commission - Windsor Street Exchange Redevelopment Project - $69,275,000",
        type="Capital Expenditure Approvals",
        status="Open",
        category="Water",
        date_received="04/07/2025",
        date_final=date_final,
        counts=counts
        or {"Exhibits": 13, "Key Documents": 6, "Other Documents": 43, "Transcripts": 0, "Recordings": 0},
    )


def download(doc_no, tmp_path, ok=True, downloadable=True):
    row = DocRow(doc_no, f"Title {doc_no}", "01/01/2026", "Public", ".pdf", downloadable)
    paths = []
    if ok:
        p = tmp_path / f"{doc_no}.pdf"
        p.write_bytes(b"pdf")
        paths = [p]
    return Downloaded(row, paths, None if ok else "boom")


def reply_for(tmp_path, downloads, counts=None, doc_type="Other Documents", total=43, **zip_kw):
    result = FetchResult(info(counts), doc_type, total, downloads)
    z = build_zip(result.files, tmp_path / "z.zip", **zip_kw)
    return compose_matter_reply("Sherry", result, z)


def test_reply_matches_example_format(tmp_path):
    downloads = [download(str(100 + i), tmp_path) for i in range(10)]
    body = reply_for(tmp_path, downloads)
    assert body == (
        "Hi Sherry,\n\n"
        "M12205 is about Halifax Regional Water Commission - Windsor Street Exchange "
        "Redevelopment Project - $69,275,000. It relates to Capital Expenditure Approvals "
        "within the Water category. The matter had an initial filing on April 7, 2025 and a "
        "final filing on October 23, 2025. I found 13 Exhibits, 6 Key Documents, 43 Other "
        "Documents, and no Transcripts or Recordings. I downloaded 10 out of the 43 Other "
        "Documents and am attaching them as a ZIP here.\n"
    )


def test_reply_all_downloaded_and_singular_counts(tmp_path):
    counts = {"Exhibits": 1, "Key Documents": 0, "Other Documents": 2, "Transcripts": 0, "Recordings": 0}
    body = reply_for(tmp_path, [download("1", tmp_path), download("2", tmp_path)], counts, total=2)
    assert "I found 1 Exhibit, 2 Other Documents, and no Key Documents, Transcripts, or Recordings." in body
    assert "I downloaded all 2 Other Documents and am attaching them as a ZIP here." in body


def test_reply_single_document(tmp_path):
    counts = {"Exhibits": 0, "Key Documents": 0, "Other Documents": 1, "Transcripts": 0, "Recordings": 0}
    body = reply_for(tmp_path, [download("1", tmp_path)], counts, total=1)
    assert "I downloaded the 1 Other Document and am attaching it as a ZIP here." in body


def test_reply_empty_tab(tmp_path):
    body = reply_for(tmp_path, [], doc_type="Transcripts", total=0)
    assert "This matter has no Transcripts, so the attached ZIP is empty." in body


def test_reply_no_final_date_and_no_documents_at_all(tmp_path):
    counts = {t: 0 for t in ("Exhibits", "Key Documents", "Other Documents", "Transcripts", "Recordings")}
    result = FetchResult(info(counts, date_final=None), "Exhibits", 0, [])
    body = compose_matter_reply(None, result, build_zip([], tmp_path / "z.zip"))
    assert body.startswith("Hi there,")
    assert "The matter had an initial filing on April 7, 2025. I found no documents in this matter." in body


def test_reply_lists_documents_it_could_not_include(tmp_path):
    downloads = [
        download("1", tmp_path),
        download("2", tmp_path, ok=False),
        download("3", tmp_path, ok=False, downloadable=False),
    ]
    body = reply_for(tmp_path, downloads, total=3)
    assert "I downloaded 1 out of the 3 Other Documents and am attaching it as a ZIP here." in body
    assert "- 2 (Title 2): the download failed after several attempts" in body
    assert "- 3 (Title 3): not available for download on the site" in body


def test_reply_reports_files_too_large(tmp_path):
    downloads = [download("1", tmp_path), download("2", tmp_path)]
    body = reply_for(tmp_path, downloads, total=2, max_bytes=4)  # each file is 3 bytes
    assert "I downloaded 1 out of the 2 Other Documents" in body
    assert "- 2 (Title 2): too large to fit in the email attachment" in body


def test_not_found_reply():
    body = compose_not_found_reply("Sherry", "M99999")
    assert "couldn't find a matter numbered M99999" in body
    assert "attached ZIP is empty" in body


def test_reply_explains_withheld_confidential_documents(tmp_path):
    public = download("H-1", tmp_path)
    secret_row = DocRow("H-4(C)", "RIR-1 CONFIDENTIAL", "05/22/2025", "Confidential", ".pdf", True)
    secret = Downloaded(secret_row, [], "Confidential: withheld by the Board")
    body = reply_for(tmp_path, [public, secret], doc_type="Exhibits", total=13)
    assert "I downloaded 1 out of the 13 Exhibits" in body
    assert "- H-4(C) (RIR-1 CONFIDENTIAL): marked Confidential by the Board" in body


def test_docrow_public_detection():
    def row(security):
        return DocRow("1", "t", "01/01/2026", security, ".pdf", True)

    assert row("Public").is_public and row("").is_public
    assert not row("Confidential").is_public
