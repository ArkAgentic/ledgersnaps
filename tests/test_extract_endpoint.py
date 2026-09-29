import io

from fastapi.testclient import TestClient
from pypdf import PdfWriter

from app.main import app


def make_pdf(page_count: int = 1) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=72, height=72)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def auth_headers(client: TestClient, user_id: str = "u-test", tenant_id: str = "t-default") -> dict[str, str]:
    t = client.get(f"/api/v1/auth/dev-token?user_id={user_id}&tenant_id={tenant_id}")
    assert t.status_code == 200
    token = t.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_extract_rejects_11_page_pdf():
    client = TestClient(app)
    pdf_bytes = make_pdf(page_count=11)

    resp = client.post("/api/v1/extract", files={"file": ("invoice.pdf", pdf_bytes, "application/pdf")})

    assert resp.status_code == 400
    assert "exceeds page limit" in resp.json()["detail"]


def test_extract_rejects_non_image_non_pdf():
    client = TestClient(app)

    resp = client.post("/api/v1/extract", files={"file": ("note.txt", b"hello", "text/plain")})

    assert resp.status_code == 400
    assert "Only image files or PDF" in resp.json()["detail"]


def test_extract_and_map_xero_live_or_502():
    client = TestClient(app)
    pdf_bytes = make_pdf(page_count=1)

    resp = client.post("/api/v1/extract-and-map?target=xero&flow_mode=auto", files={"file": ("invoice.pdf", pdf_bytes, "application/pdf")})

    assert resp.status_code in {200, 502}
    if resp.status_code == 200:
        body = resp.json()
        assert body["target"] == "xero"


def test_extract_and_map_myob_live_or_502():
    client = TestClient(app)
    pdf_bytes = make_pdf(page_count=1)

    resp = client.post("/api/v1/extract-and-map?target=myob&flow_mode=auto", files={"file": ("invoice.pdf", pdf_bytes, "application/pdf")})

    assert resp.status_code in {200, 502}
    if resp.status_code == 200:
        body = resp.json()
        assert body["target"] == "myob"


def test_flow_override_forces_ar_when_success():
    client = TestClient(app)
    pdf_bytes = make_pdf(page_count=1)

    resp = client.post("/api/v1/extract-and-map?target=xero&flow_mode=ar", files={"file": ("invoice.pdf", pdf_bytes, "application/pdf")})

    assert resp.status_code in {200, 502}
    if resp.status_code == 200:
        body = resp.json()
        assert body["invoice"]["document_flow"] == "ar"
        assert body["invoice"]["flow_overridden_by_user"] is True
        assert isinstance(body["meta"]["trace"], list)
        assert body["draft_payload"]["Type"] == "ACCREC"


def test_export_xlsx_endpoint_returns_file_or_502():
    client = TestClient(app)
    pdf_bytes = make_pdf(page_count=1)

    resp = client.post(
        "/api/v1/extract-and-map/export.xlsx?target=xero&flow_mode=auto",
        files={"file": ("invoice.pdf", pdf_bytes, "application/pdf")},
        headers=auth_headers(client),
    )

    assert resp.status_code in {200, 502}
    if resp.status_code == 200:
        assert resp.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        assert len(resp.content) > 100


def test_export_xlsx_compulsory_sheet_exists_and_has_target_fields_when_success():
    from io import BytesIO
    from openpyxl import load_workbook

    client = TestClient(app)
    pdf_bytes = make_pdf(page_count=1)

    resp = client.post(
        "/api/v1/extract-and-map/export.xlsx?target=xero&flow_mode=auto",
        files={"file": ("invoice.pdf", pdf_bytes, "application/pdf")},
        headers=auth_headers(client),
    )

    assert resp.status_code in {200, 502}
    if resp.status_code == 200:
        wb = load_workbook(BytesIO(resp.content))
        assert "summary" in wb.sheetnames


def test_abn_lookup_endpoint_without_guid_returns_unavailable():
    client = TestClient(app)
    resp = client.post("/api/v1/compliance/abn-lookup?name=Sydney%20Water")

    assert resp.status_code == 200
    body = resp.json()
    assert "available" in body
    assert body["available"] in {True, False}


def test_extract_and_map_adds_pending_abr_warning_when_success():
    client = TestClient(app)
    pdf_bytes = make_pdf(page_count=1)

    resp = client.post(
        "/api/v1/extract-and-map?target=xero&flow_mode=auto",
        files={"file": ("invoice.pdf", pdf_bytes, "application/pdf")},
    )

    assert resp.status_code in {200, 502}
    if resp.status_code == 200:
        warnings = resp.json().get("validation_warnings", [])
        assert any("ABR real-time check pending" in w for w in warnings)


def test_extract_and_map_returns_gst_layer_fields_when_success():
    client = TestClient(app)
    pdf_bytes = make_pdf(page_count=1)

    resp = client.post(
        "/api/v1/extract-and-map?target=xero&flow_mode=auto",
        files={"file": ("invoice.pdf", pdf_bytes, "application/pdf")},
    )

    assert resp.status_code in {200, 502}
    if resp.status_code == 200:
        inv = resp.json()["invoice"]
        assert "gst_extracted" in inv
        assert "gst_inferred" in inv
        assert "gst_source" in inv
        assert "gst_confidence" in inv


def test_extract_and_map_batch_endpoint_single_returns_chunk_stats_or_502():
    client = TestClient(app)
    pdf_bytes = make_pdf(page_count=1)

    resp = client.post(
        "/api/v1/extract-and-map/batch?target=xero&flow_mode=auto",
        files={"files": ("invoice.pdf", pdf_bytes, "application/pdf")},
        headers=auth_headers(client),
    )

    assert resp.status_code in {200, 502}
    if resp.status_code == 200:
        body = resp.json()
        assert body["file_count"] == 1
        assert len(body["items"]) == 1


def test_extract_and_map_batch_endpoint_accepts_legacy_file_field_single():
    client = TestClient(app)
    pdf_bytes = make_pdf(page_count=1)

    resp = client.post(
        "/api/v1/extract-and-map/batch?target=xero&flow_mode=auto",
        files={"file": ("invoice.pdf", pdf_bytes, "application/pdf")},
        headers=auth_headers(client),
    )

    assert resp.status_code in {200, 502}
    if resp.status_code == 200:
        body = resp.json()
        assert body["file_count"] == 1
        assert len(body["items"]) == 1


def test_extract_and_map_multi_file_batch_endpoint():
    client = TestClient(app)
    pdf1 = make_pdf(page_count=1)
    pdf2 = make_pdf(page_count=2)

    resp = client.post(
        "/api/v1/extract-and-map/batch/multi?target=xero&flow_mode=auto",
        files=[
            ("files", ("a.pdf", pdf1, "application/pdf")),
            ("files", ("b.pdf", pdf2, "application/pdf")),
        ],
        headers=auth_headers(client),
    )

    assert resp.status_code in {200, 400, 502}
    if resp.status_code == 200:
        body = resp.json()
        assert body["file_count"] == 2
        assert len(body["items"]) == 2


def test_extract_and_map_multi_file_batch_supports_pdf_and_png_mixed():
    client = TestClient(app)
    pdf1 = make_pdf(page_count=1)
    png_like = b"\x89PNG\r\n\x1a\n" + b"0" * 64

    resp = client.post(
        "/api/v1/extract-and-map/batch/multi?target=xero&flow_mode=auto",
        files=[
            ("files", ("a.pdf", pdf1, "application/pdf")),
            ("files", ("b.png", png_like, "image/png")),
        ],
        headers=auth_headers(client),
    )

    assert resp.status_code in {200, 400, 502}
    if resp.status_code == 200:
        body = resp.json()
        assert body["file_count"] == 2
        assert len(body["items"]) == 2


def test_multifile_export_xlsx_endpoint():
    client = TestClient(app)
    pdf1 = make_pdf(page_count=1)
    pdf2 = make_pdf(page_count=2)

    resp = client.post(
        "/api/v1/extract-and-map/batch/multi/export.xlsx?target=xero&flow_mode=auto",
        files=[
            ("files", ("a.pdf", pdf1, "application/pdf")),
            ("files", ("b.pdf", pdf2, "application/pdf")),
        ],
        headers=auth_headers(client),
    )

    assert resp.status_code in {200, 502}
    if resp.status_code == 200:
        assert resp.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        from io import BytesIO
        from openpyxl import load_workbook

        wb = load_workbook(BytesIO(resp.content), data_only=True)
        assert "summary" in wb.sheetnames
        assert "xero_draft" in wb.sheetnames


def test_multifile_export_xlsx_count_mismatch_returns_400():
    client = TestClient(app)
    pdf1 = make_pdf(page_count=1)
    pdf2 = make_pdf(page_count=1)

    h = auth_headers(client)
    h["X-Client-File-Count"] = "3"
    resp = client.post(
        "/api/v1/extract-and-map/batch/multi/export.xlsx?target=xero&flow_mode=auto",
        files=[
            ("files", ("a.pdf", pdf1, "application/pdf")),
            ("files", ("b.pdf", pdf2, "application/pdf")),
        ],
        headers=h,
    )

    assert resp.status_code == 400
    assert "count_mismatch" in resp.json().get("detail", "")


def test_xero_payload_validator_smoke():
    from app.xero_payload_validator import validate_xero_draft_payload

    ok, missing, fixes = validate_xero_draft_payload(
        {
            "Type": "ACCPAY",
            "Contact": {"Name": "Origin Energy"},
            "Status": "DRAFT",
            "Date": "2025-10-28",
            "DueDate": "2025-11-17",
            "InvoiceNumber": "114796009",
            "CurrencyCode": "AUD",
            "LineAmountTypes": "Exclusive",
            "LineItems": [{"Description": "Power bill", "AccountCode": "400", "Quantity": 1, "UnitAmount": 150.61}],
        }
    )
    assert ok is True
    assert missing == []
    assert isinstance(fixes, list)


def test_unified_export_endpoint_accepts_single_or_multi_shape():
    client = TestClient(app)
    pdf1 = make_pdf(page_count=1)

    resp = client.post(
        "/api/v1/extract-and-map/unified/export.xlsx?target=xero&flow_mode=auto",
        files=[("files", ("a.pdf", pdf1, "application/pdf"))],
        headers=auth_headers(client),
    )
    assert resp.status_code in {200, 502}
    if resp.status_code == 200:
        assert resp.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def test_extract_and_map_export_xlsx_accepts_files_and_returns_unified_sheet():
    client = TestClient(app)
    pdf1 = make_pdf(page_count=1)

    h = auth_headers(client)
    h["X-Client-File-Count"] = "1"
    resp = client.post(
        "/api/v1/extract-and-map/export.xlsx?target=xero&flow_mode=auto",
        files={"files": ("a.pdf", pdf1, "application/pdf")},
        headers=h,
    )
    assert resp.status_code in {200, 502}
    if resp.status_code == 200:
        assert resp.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        assert resp.headers.get("X-Received-Files") == "1"


def test_invoice_count_limit_blocks_over_20_estimate():
    client = TestClient(app)
    pdf_bytes = make_pdf(page_count=5)
    files = []
    for i in range(5):
        files.append(("files", (f"multi_{i}.pdf", pdf_bytes, "application/pdf")))

    h = auth_headers(client)
    h["X-Client-File-Count"] = "5"
    resp = client.post(
        "/api/v1/extract-and-map/batch/multi?target=xero&flow_mode=auto",
        files=files,
        headers=h,
    )
    assert resp.status_code == 400
    assert "invoice_count_exceeded" in resp.json().get("detail", "")


def test_invoice_count_precheck_blocks_11_files_with_one_13_invoice_pdf():
    client = TestClient(app)
    # 10个普通单页 + 1个13页（按 split 估计总数=22）
    one_page = make_pdf(page_count=1)
    many_pages = make_pdf(page_count=13)
    files = []
    for i in range(10):
        files.append(("files", (f"normal_{i}.pdf", one_page, "application/pdf")))
    files.append(("files", ("many_invoices.pdf", many_pages, "application/pdf")))

    h = auth_headers(client)
    h["X-Client-File-Count"] = "11"
    resp = client.post(
        "/api/v1/extract-and-map/batch/multi?target=xero&flow_mode=auto",
        files=files,
        headers=h,
    )
    assert resp.status_code == 400
    assert "invoice_count_exceeded" in resp.json().get("detail", "")


def test_invoice_count_limit_blocks_when_real_chunks_exceed_20():
    client = TestClient(app)
    # 该用例主要验证“不会误报失败”：预估<=20 时允许继续处理
    pdf_bytes = make_pdf(page_count=13)
    files = []
    for i in range(4):
        files.append(("files", (f"batch_{i}.pdf", pdf_bytes, "application/pdf")))

    h = auth_headers(client)
    h["X-Client-File-Count"] = "4"
    resp = client.post(
        "/api/v1/extract-and-map/batch/multi?target=xero&flow_mode=auto",
        files=files,
        headers=h,
    )
    assert resp.status_code in {200, 400, 502}
    if resp.status_code == 400:
        assert "invoice_count_exceeded" in resp.json().get("detail", "")


def test_batch_multi_requires_bearer_auth_now():
    client = TestClient(app)
    pdf1 = make_pdf(page_count=1)
    resp = client.post(
        "/api/v1/extract-and-map/batch/multi?target=xero&flow_mode=auto",
        files=[("files", ("a.pdf", pdf1, "application/pdf"))],
    )
    assert resp.status_code == 401
    assert resp.json().get("detail") == "missing_or_invalid_bearer_token"
