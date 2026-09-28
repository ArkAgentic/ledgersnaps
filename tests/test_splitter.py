import io

from pypdf import PdfWriter

from app.splitter import split_pdf_auto


def make_pdf(page_count: int = 1) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=72, height=72)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_splitter_keeps_single_blank_pdf():
    pdf = make_pdf(1)
    chunks, decision, stats = split_pdf_auto(pdf)
    assert decision == "single"
    assert len(chunks) == 1
    assert stats["page_count"] == 1


def test_splitter_keeps_multi_page_without_signals():
    pdf = make_pdf(3)
    chunks, decision, stats = split_pdf_auto(pdf)
    assert decision == "single"
    assert len(chunks) == 1
    assert stats["page_count"] == 3
