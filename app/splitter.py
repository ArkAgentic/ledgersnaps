from __future__ import annotations

import re
from io import BytesIO

from pypdf import PdfReader, PdfWriter


def split_pdf_auto(file_bytes: bytes) -> tuple[list[bytes], str, dict]:
    """自动拆分策略（通用规则，无 vendor hardcode）。

    返回: (chunks, decision, stats)
    - chunks: 每个子文档 PDF bytes
    - decision: split|single
    - stats: 用于 trace 的判定信息
    """
    reader = PdfReader(BytesIO(file_bytes))
    page_count = len(reader.pages)
    texts = [(p.extract_text() or "") for p in reader.pages]
    joined = "\n".join(texts)

    invoice_tokens = set(
        re.findall(
            r"(?:Invoice\s*(?:number|num(?:ber)?)|Reference\s*Number|Supplier\s*invoice\s*number)[:\s]*([A-Z0-9\-]{3,})",
            joined,
            flags=re.IGNORECASE,
        )
    )
    head_hits = len(re.findall(r"\b(tax\s*invoice|invoice|credit\s*note)\b", joined, flags=re.IGNORECASE))
    total_hits = len(re.findall(r"\b(total\s*(aud|amount\s*due|due)?)\b", joined, flags=re.IGNORECASE))

    should_split = False
    if page_count > 1 and len(invoice_tokens) >= 2:
        should_split = True
    if page_count == 1 and head_hits >= 2 and total_hits >= 2:
        # 单页多单据暂不做区域切分，这里只标注；仍按单文档返回
        should_split = False

    if not should_split:
        return [file_bytes], "single", {
            "page_count": page_count,
            "distinct_refs": len(invoice_tokens),
            "head_hits": head_hits,
            "total_hits": total_hits,
        }

    chunks: list[bytes] = []
    for p in reader.pages:
        w = PdfWriter()
        w.add_page(p)
        b = BytesIO()
        w.write(b)
        chunks.append(b.getvalue())

    return chunks, "split", {
        "page_count": page_count,
        "distinct_refs": len(invoice_tokens),
        "head_hits": head_hits,
        "total_hits": total_hits,
    }
