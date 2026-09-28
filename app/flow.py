import re
from typing import Literal

from .schemas import InvoiceData


def classify_document_flow(invoice: InvoiceData) -> tuple[Literal["ap", "ar", "unknown"], float, list[str]]:
    """
    文档流向分类（AP/AR）：
    - AP: 应付（供应商账单）
    - AR: 应收（客户发票）
    - unknown: 信号不足
    """
    text_blob = " ".join(
        [
            invoice.vendor_name or "",
            invoice.reference or "",
            invoice.invoice_num or "",
            invoice.supplier_invoice_number or "",
        ]
    ).lower()

    ap_score = 0
    ar_score = 0
    reasons: list[str] = []

    ap_terms = ["tax invoice", "bill", "amount due", "supplier", "account balance", "statement"]
    ar_terms = ["accounts receivable", "invoice to", "customer", "please remit", "payment received from"]

    for t in ap_terms:
        if t in text_blob:
            ap_score += 1
            reasons.append(f"ap:+{t}")
    for t in ar_terms:
        if t in text_blob:
            ar_score += 1
            reasons.append(f"ar:+{t}")

    # 发票号存在时，通常更偏向 AP 抓取场景（后续可按真实数据校正）
    if invoice.supplier_invoice_number:
        ap_score += 1
        reasons.append("ap:+supplier_invoice_number")

    # 供应商名称像公用事业/律所等，偏 AP
    if re.search(r"(energy|water|legal|pty ltd|corp|corporation)", invoice.vendor_name.lower() if invoice.vendor_name else ""):
        ap_score += 1
        reasons.append("ap:+vendor_pattern")

    if ap_score == 0 and ar_score == 0:
        return "unknown", 0.0, ["no strong flow signals"]

    if ap_score > ar_score:
        confidence = min(0.95, 0.5 + (ap_score - ar_score) * 0.15)
        return "ap", round(confidence, 2), reasons
    if ar_score > ap_score:
        confidence = min(0.95, 0.5 + (ar_score - ap_score) * 0.15)
        return "ar", round(confidence, 2), reasons

    return "unknown", 0.4, reasons
