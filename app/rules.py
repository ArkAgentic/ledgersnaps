import re
from io import BytesIO
from typing import Optional

from pypdf import PdfReader

from .schemas import InvoiceData

# 金额匹配（支持 $1,234.56）
_MONEY_RE = re.compile(r"\$\s*([0-9][0-9,]*\.?[0-9]{0,2})")
# 通用短日期匹配（如 19/06/2024）
_DATE_RE = re.compile(r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})")
# 明显不是发票号的噪音词
_BAD_INVOICE_TOKENS = {"date", "amount", "total", "invoice", "tax", "bill"}
# 明显不是供应商名称的噪音片段
_VENDOR_NOISE_HINTS = ("website:", "need help", "faults", "emergencies", "page ")


def _to_float(money_text: str) -> Optional[float]:
    """把金额字符串转成 float。"""
    try:
        return float(money_text.replace(",", ""))
    except Exception:
        return None


def _extract_pdf_text(file_bytes: bytes, max_pages: int = 2) -> str:
    """Step A: 从 PDF 文本层提取前 N 页内容，作为低成本解析输入。"""
    reader = PdfReader(BytesIO(file_bytes))
    parts = []
    for i, p in enumerate(reader.pages):
        if i >= max_pages:
            break
        parts.append(p.extract_text() or "")
    return "\n".join(parts)


def _find_first(pattern: re.Pattern, text: str) -> Optional[str]:
    m = pattern.search(text)
    return m.group(1).strip() if m else None


def _find_total(text: str) -> Optional[float]:
    """优先使用强标签提取 Total，最后才回退到最后一个金额（保守策略）。"""
    labels = [
        r"Total Amount Due\s*\$\s*([0-9,]+\.?[0-9]{0,2})",
        r"Total amount due\s*\$\s*([0-9,]+\.?[0-9]{0,2})",
        r"Total amount owing on this bill\s*\$\s*([0-9,]+\.?[0-9]{0,2})",
        r"Your total for this bill\s*\$\s*([0-9,]+\.?[0-9]{0,2})",
        r"Amount due\s*\$\s*([0-9,]+\.?[0-9]{0,2})",
    ]
    for p in labels:
        m = re.search(p, text, flags=re.IGNORECASE)
        if m:
            return _to_float(m.group(1))

    monies = _MONEY_RE.findall(text)
    if monies:
        return _to_float(monies[-1])
    return None


def _find_gst(text: str) -> Optional[float]:
    m = re.search(r"GST[^\n\$]{0,40}\$\s*([0-9,]+\.?[0-9]{0,2})", text, flags=re.IGNORECASE)
    if m:
        return _to_float(m.group(1))
    return None


def _find_invoice_num(text: str) -> Optional[str]:
    """提取发票号，并做基础清洗（过滤 Date/Total 这类误命中）。"""
    patterns = [
        r"Invoice\s*(?:number|num(?:ber)?)[:\s]*([A-Z0-9\-]+)",
        r"Tax invoice\s*([A-Z0-9\-]+)",
        r"Supplier invoice number[:\s]*([A-Z0-9\-]+)",
    ]
    for p in patterns:
        m = re.search(p, text, flags=re.IGNORECASE)
        if m:
            token = m.group(1).strip()
            if token.lower() in _BAD_INVOICE_TOKENS:
                continue
            # 至少包含一个数字，避免纯文本误识别
            if not re.search(r"\d", token):
                continue
            return token
    return None


def _find_date(text: str) -> Optional[str]:
    for p in [r"Issue date\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})", r"Date of issue\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})"]:
        m = re.search(p, text, flags=re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return _find_first(_DATE_RE, text)


def _find_due_date(text: str) -> Optional[str]:
    # 覆盖常见 9 类格式：
    # 1) 10/10/2026  2) 10-10-2026  3) 2026-10-10  4) 2026/10/10
    # 5) 10 Oct 2026 6) 10 October 2026 7) Oct 10, 2026
    # 8) 2Oct2026    9) 2nd October 2026
    patterns = [
        r"(?:Due Date|Payment Due Date|Payment Due|Due)[:\s]*([0-9]{1,2}[/-][0-9]{1,2}[/-][0-9]{2,4})",
        r"(?:Due Date|Payment Due Date|Payment Due|Due)[:\s]*([0-9]{4}[/-][0-9]{1,2}[/-][0-9]{1,2})",
        r"(?:Due Date|Payment Due Date|Payment Due|Due)[:\s]*([0-9]{1,2}\s*[A-Za-z]{3,9}\s*[0-9]{2,4})",
        r"(?:Due Date|Payment Due Date|Payment Due|Due)[:\s]*([A-Za-z]{3,9}\s*[0-9]{1,2},\s*[0-9]{4})",
        r"(?:Due Date|Payment Due Date|Payment Due|Due)[:\s]*([0-9]{1,2}(?:st|nd|rd|th)\s*[A-Za-z]{3,9}\s*[0-9]{4})",
    ]
    for p in patterns:
        m = re.search(p, text, flags=re.IGNORECASE)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
    return None


def _find_vendor_name(text: str) -> Optional[str]:
    """供应商识别：优先公司实体行，剔除明显噪音行。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    for ln in lines[:30]:
        ll = ln.lower()
        if any(h in ll for h in _VENDOR_NOISE_HINTS):
            continue
        if re.search(r"(Pty Ltd|Legal|Energy|Water|Ltd|Corporation|Trust|Agency|Services|Books)", ln, flags=re.IGNORECASE):
            return ln

    for ln in lines[:15]:
        ll = ln.lower()
        if not any(h in ll for h in _VENDOR_NOISE_HINTS):
            return ln

    return lines[0] if lines else None


def parse_invoice_from_text(text: str, *, warnings: Optional[list[str]] = None) -> tuple[InvoiceData, bool, int]:
    """对 OCR/PDF 文本执行同一套 Step A 规则解析。"""
    local_warnings = warnings if warnings is not None else []
    if not text.strip():
        local_warnings.append("empty extracted text; escalate to llm")
        return InvoiceData(), False, 0

    vendor_name = _find_vendor_name(text) or ""
    invoice_num = _find_invoice_num(text)
    date = _find_date(text)
    due_date = _find_due_date(text)
    total = _find_total(text)
    gst = _find_gst(text)

    subtotal = None
    if total is not None and gst is not None:
        subtotal = round(total - gst, 2)

    invoice = InvoiceData(
        vendor_name=vendor_name,
        invoice_num=invoice_num,
        supplier_invoice_number=invoice_num,
        date=date,
        due_date=due_date,
        total=total,
        gst=gst,
        gst_extracted=gst,
        gst_source="extracted" if gst is not None else "none",
        gst_confidence=0.9 if gst is not None else 0.0,
        subtotal=subtotal,
    )

    confident, score = _is_rules_confident(invoice)
    if not confident:
        local_warnings.append(f"rules confidence low (score={score}/4); escalate to llm")
    return invoice, confident, score


def _is_rules_confident(invoice: InvoiceData) -> tuple[bool, int]:
    """Step A 质量门：必须命中核心字段，且发票号质量可接受。"""
    score = 0
    if invoice.vendor_name:
        score += 1
    if invoice.date:
        score += 1
    if invoice.total is not None:
        score += 1
    if invoice.invoice_num:
        score += 1

    strong_vendor = bool(invoice.vendor_name and not any(h in invoice.vendor_name.lower() for h in _VENDOR_NOISE_HINTS))
    good_invoice_num = bool(invoice.invoice_num and invoice.invoice_num.lower() not in _BAD_INVOICE_TOKENS)
    has_due_or_gst = invoice.due_date is not None or invoice.gst is not None

    # 新阈值：复杂账单更容易升级到 LLM，避免错误落在 rules
    confident = score >= 4 and strong_vendor and good_invoice_num and has_due_or_gst
    return confident, score


def _document_split_hints(file_bytes: bytes) -> list[str]:
    """文档边界提示（仅提示，不阻塞）：
    - 一页多单据
    - 多页单单据（不建议拆）
    - 多页多单据（建议拆）
    """
    reader = PdfReader(BytesIO(file_bytes))
    page_count = len(reader.pages)
    if page_count <= 0:
        return []

    texts = [(p.extract_text() or "") for p in reader.pages]
    joined = "\n".join(texts)

    # 一页多单据：同页出现多个 invoice/credit-note 标题且伴随多个总额提示
    if page_count == 1:
        head_hits = len(re.findall(r"\b(tax\s*invoice|invoice|credit\s*note)\b", joined, flags=re.IGNORECASE))
        total_hits = len(re.findall(r"\b(total\s*(aud|amount\s*due|due)?)\b", joined, flags=re.IGNORECASE))
        if head_hits >= 2 and total_hits >= 2:
            return [
                "split decision(auto): single-page multi-document suspected; apply automatic region segmentation"
            ]
        return ["split decision(auto): single-page appears single document; no split"]

    # 多页：看是否存在多个不同编号
    invoice_tokens = set(
        re.findall(
            r"(?:Invoice\s*(?:number|num(?:ber)?)|Reference\s*Number|Supplier\s*invoice\s*number)[:\s]*([A-Z0-9\-]{3,})",
            joined,
            flags=re.IGNORECASE,
        )
    )
    page_marker = bool(re.search(r"Page\s*\d+\s*(?:of|/)\s*\d+", joined, flags=re.IGNORECASE))

    if len(invoice_tokens) >= 2:
        return [
            f"split decision(auto): multi-page multi-document suspected ({len(invoice_tokens)} distinct refs); split into per-page/per-boundary jobs"
        ]

    if page_marker:
        return ["split decision(auto): multi-page single document (page x of y); keep as one job"]

    return ["split decision(auto): multi-page but no strong multi-doc signal; keep as one job"]


def extract_invoice_rules(file_bytes: bytes, filename: str, content_type: str) -> tuple[InvoiceData, list[str], bool]:
    """
    Step A 规则提取：
    - 先走低成本文本解析
    - 命中质量门则直接返回
    - 否则交给 Step B (LLM)
    """
    warnings: list[str] = []
    if content_type.startswith("image/"):
        return InvoiceData(), ["rules parser skipped for image; escalate to llm"], False

    text = _extract_pdf_text(file_bytes)
    warnings.extend(_document_split_hints(file_bytes))
    if not text.strip():
        return InvoiceData(), ["empty PDF text layer; escalate to llm"], False

    invoice, confident, _ = parse_invoice_from_text(text, warnings=warnings)
    return invoice, warnings, confident
