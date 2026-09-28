from io import BytesIO

from pypdf import PdfReader


def validate_pdf_bytes(file_bytes: bytes, max_pages: int, max_mb: int) -> None:
    size_limit = max_mb * 1024 * 1024
    if len(file_bytes) > size_limit:
        raise ValueError(f"File exceeds size limit of {max_mb}MB")

    reader = PdfReader(BytesIO(file_bytes))
    page_count = len(reader.pages)
    if page_count > max_pages:
        raise ValueError(f"PDF exceeds page limit of {max_pages} pages")
