import json
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font

from .schemas import ExtractAndMapResponse, MultiFileBatchResponse
from .xero_payload_validator import validate_xero_draft_payload


def build_extraction_workbook(result: ExtractAndMapResponse) -> bytes:
    """导出单次提取结果为 Excel（二次确认用）。"""
    wb = Workbook()

    # Sheet1: 摘要字段
    ws = wb.active
    assert ws is not None
    ws.title = "summary"
    ws.append(["field", "value"])
    for c in ws[1]:
        c.font = Font(bold=True)

    inv = result.invoice
    rows = [
        ("target", result.target),
        ("model", result.model),
        ("method", result.meta.method),
        ("estimated_cost_usd", result.meta.usage.estimated_cost_usd if result.meta.usage else 0.0),
        ("input_tokens", result.meta.usage.input_tokens if result.meta.usage else 0),
        ("output_tokens", result.meta.usage.output_tokens if result.meta.usage else 0),
        ("vendor_name", inv.vendor_name),
        ("abn", inv.abn),
        ("invoice_num", inv.invoice_num),
        ("supplier_invoice_number", inv.supplier_invoice_number),
        ("date", inv.date),
        ("due_date", inv.due_date),
        ("reference", inv.reference),
        ("subtotal", inv.subtotal),
        ("gst", inv.gst),
        ("total", inv.total),
        ("currency", inv.currency),
        ("document_flow", inv.document_flow),
        ("flow_confidence", inv.flow_confidence),
        ("flow_overridden_by_user", inv.flow_overridden_by_user),
    ]
    for k, v in rows:
        ws.append([k, "" if v is None else v])

    # Sheet2: line_items
    ws2 = wb.create_sheet("line_items")
    ws2.append(["description", "quantity", "unit_price", "amount", "tax_amount", "account_code_hint", "tax_code_hint"])
    for c in ws2[1]:
        c.font = Font(bold=True)
    for li in inv.line_items:
        ws2.append([li.description, li.quantity, li.unit_price, li.amount, li.tax_amount, li.account_code_hint, li.tax_code_hint])

    # Sheet3: warnings
    ws3 = wb.create_sheet("warnings")
    ws3.append(["type", "message"])
    for c in ws3[1]:
        c.font = Font(bold=True)
    for w in result.validation_warnings:
        ws3.append(["validation", w])
    for w in result.meta.warnings:
        ws3.append(["meta", w])

    # Sheet4: trace
    ws4 = wb.create_sheet("trace")
    ws4.append(["step", "detail"])
    for c in ws4[1]:
        c.font = Font(bold=True)
    for i, t in enumerate(result.meta.trace, start=1):
        ws4.append([i, t])

    # Sheet5: draft_payload
    ws5 = wb.create_sheet("draft_payload")
    ws5.append(["key", "value"])
    for c in ws5[1]:
        c.font = Font(bold=True)
    for k, v in result.draft_payload.items():
        ws5.append([k, str(v)])

    # Sheet6: compulsory_fields（重点给用户审核 Xero/MYOB 必填）
    ws6 = wb.create_sheet("compulsory_fields")
    ws6.append(["system", "flow", "field", "value", "is_missing"])
    for c in ws6[1]:
        c.font = Font(bold=True)

    inv = result.invoice
    flow = inv.document_flow

    # 这里同时给出 AP 与 AR 的必填检查，便于用户审核后再上传
    compulsory = [
        ("xero", "ap", "Contact.Name", inv.vendor_name),
        ("xero", "ap", "Date", inv.date),
        ("xero", "ap", "Type", "ACCPAY"),
        ("xero", "ap", "LineItems or Total", "has_line_items" if inv.line_items else inv.total),
        ("xero", "ar", "Contact.Name", inv.vendor_name),
        ("xero", "ar", "Date", inv.date),
        ("xero", "ar", "Type", "ACCREC"),
        ("xero", "ar", "LineItems or Total", "has_line_items" if inv.line_items else inv.total),
        ("myob", "ap", "SupplierName", inv.vendor_name),
        ("myob", "ap", "Date", inv.date),
        ("myob", "ap", "Lines or Total", "has_lines" if inv.line_items else inv.total),
        ("myob", "ar", "CustomerName", inv.vendor_name),
        ("myob", "ar", "Date", inv.date),
        ("myob", "ar", "Lines or Total", "has_lines" if inv.line_items else inv.total),
    ]
    for system, cflow, field, value in compulsory:
        missing = value is None or value == ""
        # 当前流向优先显示在上面（通过排序权重实现）
        sort_bias = 0 if cflow == flow else 1
        ws6.append([system, cflow, field, "" if value is None else str(value), str(missing).lower(), sort_bias])

    # 去掉辅助排序列
    for r in range(2, ws6.max_row + 1):
        ws6.cell(row=r, column=6).value = None

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_multifile_extraction_workbook(batch: MultiFileBatchResponse) -> bytes:
    """导出多文件批量结果为单表（每行=一个文件/chunk）。"""
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "summary"

    headers = [
        "filename",
        "chunk_index",
        "invoice_count",
        "billable_invoice_count",
        "target",
        "model",
        "method",
        "estimated_cost_usd",
        "input_tokens",
        "output_tokens",
        "vendor_name",
        "abn",
        "invoice_num",
        "supplier_invoice_number",
        "date",
        "due_date",
        "reference",
        "subtotal",
        "gst",
        "total",
        "currency",
        "document_flow",
        "flow_confidence",
        "xero_ready",
        "missing_required_fields",
        "suggested_fixes",
        "warnings",
        "line_items",
        "trace",
        "draft_payload",
    ]
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True)

    for item in batch.items:
        if not item.ok or not item.batch_result:
            ws.append(
                [
                    item.file_name,
                    "",
                    0,
                    0,
                    "",
                    "",
                    "",
                    0.0,
                    0,
                    0,
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    False,
                    "",
                    "",
                    f"file_failed: {item.error or 'unknown error'}",
                    "",
                    "",
                    "",
                ]
            )
            continue

        for chunk in item.batch_result.chunks:
            r = chunk.result
            inv = r.invoice
            usage = r.meta.usage
            ws.append(
                [
                    item.file_name,
                    chunk.chunk_index,
                    item.batch_result.chunk_count,
                    item.batch_result.summary.get("billable_invoice_count", 0),
                    r.target,
                    r.model,
                    r.meta.method,
                    usage.estimated_cost_usd if usage else 0.0,
                    usage.input_tokens if usage else 0,
                    usage.output_tokens if usage else 0,
                    inv.vendor_name,
                    inv.abn,
                    inv.invoice_num,
                    inv.supplier_invoice_number,
                    inv.date,
                    inv.due_date,
                    inv.reference,
                    inv.subtotal,
                    inv.gst,
                    inv.total,
                    inv.currency,
                    inv.document_flow,
                    inv.flow_confidence,
                    r.xero_ready,
                    " | ".join(r.missing_required_fields),
                    " | ".join(r.suggested_fixes),
                    " | ".join(r.validation_warnings),
                    json.dumps([li.model_dump() for li in inv.line_items], ensure_ascii=False),
                    " | ".join(r.meta.trace),
                    json.dumps(r.draft_payload, ensure_ascii=False),
                ]
            )

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
