from io import BytesIO

import pytest
from pypdf import PdfWriter

from app.validators import validate_pdf_bytes


def make_pdf_bytes(page_count: int) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=72, height=72)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def test_validate_pdf_rejects_more_than_10_pages():
    pdf_bytes = make_pdf_bytes(11)

    with pytest.raises(ValueError, match="exceeds page limit"):
        validate_pdf_bytes(pdf_bytes, max_pages=10, max_mb=25)


def test_validate_pdf_rejects_more_than_25mb():
    payload = b"0" * (25 * 1024 * 1024 + 1)

    with pytest.raises(ValueError, match="exceeds size limit"):
        validate_pdf_bytes(payload, max_pages=10, max_mb=25)
