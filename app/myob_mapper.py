from .schemas import InvoiceData, MyobDraftBillPayload, MyobBillLine


def to_myob_draft_bill(invoice: InvoiceData) -> MyobDraftBillPayload:
    """MYOB AP Bill 映射（当前阶段）。"""
    lines = []
    for li in invoice.line_items:
        lines.append(
            MyobBillLine(
                Description=li.description or "Line item",
                Total=li.amount,
                UnitCount=li.quantity,
                UnitPrice=li.unit_price,
            )
        )

    if not lines and invoice.total is not None:
        lines.append(MyobBillLine(Description="Invoice total", Total=invoice.total, UnitCount=1, UnitPrice=invoice.total))

    return MyobDraftBillPayload(
        SupplierName=invoice.vendor_name or "Unknown Supplier",
        SupplierInvoiceNumber=invoice.supplier_invoice_number or invoice.invoice_num,
        Date=invoice.date,
        DueDate=invoice.due_date,
        Lines=lines,
    )
