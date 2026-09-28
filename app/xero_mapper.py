from .schemas import InvoiceData, XeroDraftBillPayload, XeroLineItem


def to_xero_accpay_draft(invoice: InvoiceData) -> XeroDraftBillPayload:
    """Xero AP Bill 映射（Type=ACCPAY）。"""
    line_items = []
    for li in invoice.line_items:
        line_items.append(
            XeroLineItem(
                Description=li.description or "Line item",
                Quantity=li.quantity,
                UnitAmount=li.unit_price,
                LineAmount=li.amount,
                AccountCode=li.account_code_hint,
                TaxType=li.tax_code_hint,
            )
        )

    if not line_items and invoice.total is not None:
        line_items.append(
            XeroLineItem(
                Description="Invoice total",
                Quantity=1,
                UnitAmount=invoice.total,
                LineAmount=invoice.total,
            )
        )

    return XeroDraftBillPayload(
        Type="ACCPAY" if invoice.document_flow != "ar" else "ACCREC",
        Contact={"Name": invoice.vendor_name or "Unknown Contact"},
        Date=invoice.date,
        DueDate=invoice.due_date,
        InvoiceNumber=invoice.invoice_num,
        Reference=invoice.reference,
        CurrencyCode=invoice.currency or "AUD",
        LineItems=line_items,
    )
