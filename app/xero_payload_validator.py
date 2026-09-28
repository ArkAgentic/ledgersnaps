from __future__ import annotations

from typing import Any


def validate_xero_draft_payload(payload: dict[str, Any]) -> tuple[bool, list[str], list[str]]:
    """校验 Xero Draft payload 最小可靠字段集。

    返回: (xero_ready, missing_required_fields, suggested_fixes)
    """
    missing: list[str] = []
    fixes: list[str] = []

    # 顶层必填
    if payload.get("Type") not in {"ACCPAY", "ACCREC"}:
        missing.append("Type")
        fixes.append("Set Type to ACCPAY (bill) or ACCREC (sales invoice)")

    contact = payload.get("Contact") or {}
    if not (contact.get("ContactID") or contact.get("Name")):
        missing.append("Contact.Name|ContactID")
        fixes.append("Provide ContactID or Contact.Name")

    if not payload.get("Status"):
        missing.append("Status")
        fixes.append("Set Status='DRAFT'")

    if not payload.get("Date"):
        missing.append("Date")
        fixes.append("Provide invoice/bill date YYYY-MM-DD")

    if not payload.get("DueDate"):
        missing.append("DueDate")
        fixes.append("Provide due date YYYY-MM-DD")

    if not payload.get("InvoiceNumber"):
        missing.append("InvoiceNumber")
        fixes.append("Provide original invoice number for dedupe")

    if not payload.get("CurrencyCode"):
        missing.append("CurrencyCode")
        fixes.append("Set CurrencyCode e.g. AUD")

    if not payload.get("LineAmountTypes"):
        missing.append("LineAmountTypes")
        fixes.append("Set LineAmountTypes='Exclusive' or 'Inclusive'")

    line_items = payload.get("LineItems") or []
    if not isinstance(line_items, list) or len(line_items) == 0:
        missing.append("LineItems")
        fixes.append("Provide at least one line item")
    else:
        for i, li in enumerate(line_items, start=1):
            if not li.get("Description"):
                missing.append(f"LineItems[{i}].Description")
                fixes.append(f"Line {i}: add Description")
            if not li.get("AccountCode"):
                missing.append(f"LineItems[{i}].AccountCode")
                fixes.append(f"Line {i}: add AccountCode from tenant chart of accounts")
            # TaxType 强烈建议带上，避免静默继承错误
            if not li.get("TaxType"):
                fixes.append(f"Line {i}: consider explicit TaxType to avoid default-tax mismatch")

    return len(missing) == 0, missing, fixes
