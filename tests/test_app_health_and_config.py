import os

from fastapi.testclient import TestClient

from app.main import app
from app.store import get_queue_item, get_job_owned, reset_all_jobs_for_tests
from app.storage_backend import artifact_exists_local
from app.worker import process_claimed_item, process_one_queued_job
from app import abn as abn_mod


client = TestClient(app)


def test_health_ok():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    checks = resp.json().get("checks", {})
    assert "aoai_configured" in checks
    assert "aoai" in checks
    assert "abr_guid_configured" in checks


def test_extract_requires_azure_settings_when_unconfigured():
    os.environ.pop("AZURE_OPENAI_ENDPOINT", None)
    os.environ.pop("AZURE_OPENAI_API_KEY", None)
    os.environ.pop("AZURE_OPENAI_DEPLOYMENT", None)

    resp = client.post(
        "/api/v1/extract",
        files={"file": ("invoice.jpg", b"fake-image", "image/jpeg")},
    )

    assert resp.status_code == 502
    assert "Missing Azure OpenAI configuration" in resp.json()["detail"]


def test_multi_batch_rejects_too_many_files():
    files = []
    for i in range(21):
        files.append(("files", (f"f{i}.pdf", b"%PDF-1.4\n", "application/pdf")))

    t = client.get("/api/v1/auth/dev-token?user_id=bulk-tester&tenant_id=t-default")
    assert t.status_code == 200
    token = t.json()["token"]

    resp = client.post(
        "/api/v1/extract-and-map/batch/multi?target=xero&flow_mode=auto",
        files=files,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert ("too_many_files" in detail) or ("invoice_count_exceeded" in detail)


def test_billing_endpoints_default_and_plan_switch():
    t = client.get("/api/v1/auth/dev-token?user_id=u-test&tenant_id=t-default")
    assert t.status_code == 200
    token = t.json()["token"]

    r = client.get("/api/v1/billing/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["account"]["plan_id"] == "none"

    r2 = client.post("/api/v1/billing/plan?plan_id=pro_29_95", headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200
    assert r2.json()["account"]["plan_id"] == "pro_29_95"
    assert r2.json()["account"]["plan"]["invoice_limit"] == 300


def test_auth_dev_token_then_billing_me_with_bearer():
    t = client.get("/api/v1/auth/dev-token?user_id=user-a&tenant_id=t1")
    assert t.status_code == 200
    token = t.json()["token"]

    r = client.get("/api/v1/billing/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["account"]["user_id"] == "user-a"
    assert r.json()["auth"]["tenant_id"] == "t1"
    assert r.json()["auth"]["source"] == "bearer"


def test_trial_claim_requires_phone_otp_and_blocks_phone_reuse():
    reset_all_jobs_for_tests()
    s1 = client.post("/api/v1/auth/phone/send-code?phone_e164=%2B61400111222")
    assert s1.status_code == 200
    code = s1.json().get("dev_code")
    assert code
    r1 = client.get(
        f"/api/v1/auth/dev-token?user_id=p1&tenant_id=t1&email=p1%40example.com&full_name=Person%201&phone_e164=%2B61400111222&phone_otp_code={code}&device_fingerprint=d1"
    )
    assert r1.status_code == 200

    # same phone should not be able to claim trial again for another account
    r2 = client.get(
        f"/api/v1/auth/dev-token?user_id=p2&tenant_id=t1&email=p2%40example.com&full_name=Person%202&phone_e164=%2B61400111222&phone_otp_code={code}&device_fingerprint=d2"
    )
    assert r2.status_code == 403
    assert r2.json()["detail"] == "trial_already_claimed_for_phone"


def test_trial_claim_requires_valid_phone_and_code():
    reset_all_jobs_for_tests()
    s = client.post("/api/v1/auth/phone/send-code?phone_e164=%2B61400999888")
    assert s.status_code == 200
    bad_code = client.get(
        "/api/v1/auth/dev-token?user_id=p3&tenant_id=t1&phone_e164=%2B61400999888&phone_otp_code=000000"
    )
    assert bad_code.status_code == 400
    assert bad_code.json()["detail"] == "phone_verification_required"

    bad_phone = client.get(
        "/api/v1/auth/dev-token?user_id=p4&tenant_id=t1&phone_e164=0400999888&phone_otp_code=123456"
    )
    assert bad_phone.status_code == 400
    assert bad_phone.json()["detail"] == "invalid_phone_e164"


def test_ip_can_only_register_once():
    reset_all_jobs_for_tests()
    s1 = client.post("/api/v1/auth/phone/send-code?phone_e164=%2B61411111111")
    c1 = s1.json()["dev_code"]
    r1 = client.get(
        f"/api/v1/auth/dev-token?user_id=ip-a&tenant_id=t1&email=a%40example.com&full_name=Alice&phone_e164=%2B61411111111&phone_otp_code={c1}&device_fingerprint=d1"
    )
    assert r1.status_code == 200

    s2 = client.post("/api/v1/auth/phone/send-code?phone_e164=%2B61422222222")
    c2 = s2.json()["dev_code"]
    r2 = client.get(
        f"/api/v1/auth/dev-token?user_id=ip-b&tenant_id=t1&email=b%40example.com&full_name=Bob&phone_e164=%2B61422222222&phone_otp_code={c2}&device_fingerprint=d2"
    )
    assert r2.status_code == 403
    assert r2.json()["detail"] == "ip_already_registered"


def test_registration_requires_email_phone_full_name():
    reset_all_jobs_for_tests()
    s = client.post("/api/v1/auth/phone/send-code?phone_e164=%2B61433333333")
    code = s.json()["dev_code"]

    no_email = client.get(
        f"/api/v1/auth/dev-token?user_id=u-no-email&tenant_id=t1&full_name=Alice&phone_e164=%2B61433333333&phone_otp_code={code}"
    )
    assert no_email.status_code == 403
    assert no_email.json()["detail"] == "email_required"

    no_name = client.get(
        f"/api/v1/auth/dev-token?user_id=u-no-name&tenant_id=t1&email=a%40example.com&phone_e164=%2B61433333333&phone_otp_code={code}"
    )
    assert no_name.status_code == 403
    assert no_name.json()["detail"] == "full_name_required"


def test_send_code_endpoint_returns_dev_code_in_dev_mode():
    r = client.post("/api/v1/auth/phone/send-code?phone_e164=%2B61400012345")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["provider"] == "dev"
    assert isinstance(body.get("dev_code"), str) and len(body["dev_code"]) == 6


def test_playground_contains_phone_otp_controls():
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "id='phone'" in html
    assert "id='otp'" in html
    assert "id='sendCode'" in html
    assert "/api/v1/auth/phone/send-code" in html


def test_xero_connect_requires_config_or_returns_url():
    t = client.get("/api/v1/auth/dev-token?user_id=xero-user&tenant_id=t1")
    h = {"Authorization": f"Bearer {t.json()['token']}"}
    r = client.get("/api/v1/xero/connect", headers=h)
    assert r.status_code in {200, 400}
    if r.status_code == 200:
        body = r.json()
        assert "url" in body and "state" in body
    else:
        assert "xero_config_missing" in r.json().get("detail", "")


def test_xero_connection_default_false_without_exchange():
    t = client.get("/api/v1/auth/dev-token?user_id=xero-user2&tenant_id=t1")
    h = {"Authorization": f"Bearer {t.json()['token']}"}
    r = client.get("/api/v1/xero/connection", headers=h)
    assert r.status_code == 200
    assert r.json().get("connected") is False


def test_xero_public_start_requires_config_or_returns_url():
    r = client.get("/api/v1/auth/xero/start")
    assert r.status_code in {200, 400}
    if r.status_code == 200:
        body = r.json()
        assert "url" in body and "state" in body
    else:
        assert "xero_config_missing" in r.json().get("detail", "")


def test_oauth_complete_signup_invalid_or_expired_session_token():
    r = client.post(
        "/api/v1/auth/oauth/complete-signup"
        "?oauth_session_token=missing-token"
        "&user_id=u-test"
        "&email=u%40example.com"
        "&full_name=User%20Test"
        "&phone_e164=%2B61400111222"
        "&phone_otp_code=123456"
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "oauth_session_expired"


def test_xero_draft_requires_connected_account():
    t = client.get("/api/v1/auth/dev-token?user_id=xero-user3&tenant_id=t1")
    h = {"Authorization": f"Bearer {t.json()['token']}"}
    r = client.post(
        "/api/v1/xero/drafts?target=xero&flow_mode=auto",
        headers=h,
        files={"file": ("a.jpg", b"fake-image", "image/jpeg")},
    )
    # mapping can fail earlier due to AOAI config, or xero_not_connected when mapping succeeds
    assert r.status_code in {400, 502}


def test_xero_autofill_draft_payload_min_fields():
    from app.main import _autofill_xero_draft_payload

    p = {
        "Type": "ACCPAY",
        "Contact": {"Name": "Test Supplier"},
        "Status": "DRAFT",
        "Date": "2026-09-29",
        "CurrencyCode": "AUD",
        "LineItems": [{"Description": "Line 1"}],
    }
    out = _autofill_xero_draft_payload(p)
    assert out.get("LineAmountTypes") == "Exclusive"
    assert out.get("DueDate")
    assert out.get("InvoiceNumber")
    assert out["LineItems"][0].get("AccountCode")


def test_abr_lookup_by_name_surfaces_unregistered_guid(monkeypatch):
    monkeypatch.setenv("ABR_GUID", "dummy-guid")

    async def _fake(_url: str):
        return {"Message": "The GUID entered is not recognised as a Registered Party", "Names": []}

    monkeypatch.setattr(abn_mod, "_abr_get_json", _fake)

    import asyncio

    out = asyncio.run(abn_mod.abr_lookup_by_name("Telstra"))
    assert out["available"] is False
    assert out["reason"] == "abr_guid_not_registered"


def test_abr_lookup_by_abn_surfaces_unregistered_guid(monkeypatch):
    monkeypatch.setenv("ABR_GUID", "dummy-guid")

    async def _fake(_url: str):
        return {"Message": "The GUID entered is not recognised as a Registered Party", "Abn": ""}

    monkeypatch.setattr(abn_mod, "_abr_get_json", _fake)

    import asyncio

    out = asyncio.run(abn_mod.abr_lookup_by_abn("33051775556"))
    assert out["available"] is False
    assert out["reason"] == "abr_guid_not_registered"


def test_auth_dev_token_sets_cookie_and_auth_me_works_with_cookie_only():
    t = client.get("/api/v1/auth/dev-token?user_id=user-cookie&tenant_id=t-cookie")
    assert t.status_code == 200
    assert "set-cookie" in {k.lower() for k in t.headers.keys()}

    r = client.get("/api/v1/auth/me", headers={"X-Dev-Token": t.json()["token"]})
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == "user-cookie"
    assert body["tenant_id"] == "t-cookie"
    assert body["source"] in {"cookie", "bearer"}


def test_billing_me_requires_bearer_now():
    c = TestClient(app)
    r = c.get("/api/v1/billing/me")
    assert r.status_code == 401
    assert r.json()["detail"] == "missing_or_invalid_bearer_token"


def test_jobs_isolation_by_user_scope():
    reset_all_jobs_for_tests()

    ta = client.get("/api/v1/auth/dev-token?user_id=user-a&tenant_id=t1")
    tb = client.get("/api/v1/auth/dev-token?user_id=user-b&tenant_id=t1")
    assert ta.status_code == 200 and tb.status_code == 200
    ha = {"Authorization": f"Bearer {ta.json()['token']}"}
    hb = {"Authorization": f"Bearer {tb.json()['token']}"}

    ca = client.post("/api/v1/jobs?file_count=2&invoice_estimated=3", headers=ha)
    cb = client.post("/api/v1/jobs?file_count=1&invoice_estimated=1", headers=hb)
    assert ca.status_code == 200 and cb.status_code == 200
    job_a = ca.json()["job"]["job_id"]

    la = client.get("/api/v1/jobs", headers=ha)
    lb = client.get("/api/v1/jobs", headers=hb)
    assert la.status_code == 200 and lb.status_code == 200
    assert la.json()["count"] == 1
    assert lb.json()["count"] == 1

    # user-b 不能读取 user-a 的 job
    rb = client.get(f"/api/v1/jobs/{job_a}", headers=hb)
    assert rb.status_code == 404
    assert rb.json()["detail"] == "job_not_found"


def test_job_result_owner_isolation_and_binding_to_batch_flow():
    reset_all_jobs_for_tests()

    ta = client.get("/api/v1/auth/dev-token?user_id=user-a&tenant_id=t1")
    tb = client.get("/api/v1/auth/dev-token?user_id=user-b&tenant_id=t1")
    ha = {"Authorization": f"Bearer {ta.json()['token']}"}
    hb = {"Authorization": f"Bearer {tb.json()['token']}"}

    j = client.post("/api/v1/jobs?file_count=1&invoice_estimated=1", headers=ha)
    assert j.status_code == 200
    job_id = j.json()["job"]["job_id"]

    # user-a 触发提取并绑定 job
    files = [("files", ("a.pdf", b"%PDF-1.4\n", "application/pdf"))]
    r = client.post(
        f"/api/v1/extract-and-map/batch/multi?target=xero&flow_mode=auto&job_id={job_id}",
        files=files,
        headers={**ha, "X-Client-File-Count": "1"},
    )
    assert r.status_code in {200, 400, 502}

    # user-a 可读取结果（若本次失败也应有结果行）
    ra = client.get(f"/api/v1/jobs/{job_id}/result", headers=ha)
    assert ra.status_code == 200

    # user-b 不可读取 user-a 的结果
    rb = client.get(f"/api/v1/jobs/{job_id}/result", headers=hb)
    assert rb.status_code == 404
    assert rb.json()["detail"] == "job_result_not_found"


def test_jobs_create_enqueues_and_queue_stats_visible():
    reset_all_jobs_for_tests()
    t = client.get("/api/v1/auth/dev-token?user_id=ops-user&tenant_id=t1")
    h = {"Authorization": f"Bearer {t.json()['token']}"}

    c = client.post("/api/v1/jobs?file_count=2&invoice_estimated=2", headers=h)
    assert c.status_code == 200

    s = client.get("/api/v1/queue/stats", headers=h)
    assert s.status_code == 200
    q = s.json()["queue"]
    assert q["queued"] >= 1
    assert q.get("running", 0) >= 0

    # queue row exists
    job_id = c.json()["job"]["job_id"]
    item = get_queue_item(job_id)
    assert item is not None
    assert item["status"] == "queued"


def test_jobs_create_rejects_invoice_estimate_over_per_job_limit():
    reset_all_jobs_for_tests()
    t = client.get("/api/v1/auth/dev-token?user_id=ops-user2&tenant_id=t1")
    h = {"Authorization": f"Bearer {t.json()['token']}"}

    r = client.post("/api/v1/jobs?file_count=1&invoice_estimated=21", headers=h)
    assert r.status_code == 400
    assert "invoice_count_exceeded" in r.json().get("detail", "")


def test_local_worker_processes_queued_job_end_to_end():
    reset_all_jobs_for_tests()
    t = client.get("/api/v1/auth/dev-token?user_id=worker-user&tenant_id=t1")
    h = {"Authorization": f"Bearer {t.json()['token']}"}

    c = client.post("/api/v1/jobs?file_count=1&invoice_estimated=1", headers=h)
    assert c.status_code == 200
    job_id = c.json()["job"]["job_id"]

    out = process_one_queued_job()
    assert out is not None
    assert out["job_id"] == job_id
    assert out["status"] == "completed"

    jr = client.get(f"/api/v1/jobs/{job_id}/result", headers=h)
    assert jr.status_code == 200
    assert jr.json()["job_result"]["status"] == "completed"


def test_worker_process_claimed_item_missing_artifact_sets_failed():
    reset_all_jobs_for_tests()
    t = client.get("/api/v1/auth/dev-token?user_id=worker-fail&tenant_id=t1")
    h = {"Authorization": f"Bearer {t.json()['token']}"}

    c = client.post("/api/v1/jobs?file_count=1&invoice_estimated=1", headers=h)
    assert c.status_code == 200
    job_id = c.json()["job"]["job_id"]

    # fabricate a claimed item with non-placeholder source but no input artifact
    item = {
        "job_id": job_id,
        "tenant_id": "t1",
        "user_id": "worker-fail",
        "payload": {
            "source": "upload",
            "target": "xero",
            "flow_mode": "auto",
            "filename": "x.pdf",
            "content_type": "application/pdf",
        },
    }
    out = process_claimed_item(item)
    assert out["status"] == "failed"
    assert "missing_input_artifact" in out["reason"]

    j = client.get(f"/api/v1/jobs/{job_id}", headers=h)
    assert j.status_code == 200
    assert j.json()["job"]["status"] == "failed"


def test_jobs_submit_upload_and_worker_cleanup_local_artifact():
    reset_all_jobs_for_tests()
    t = client.get("/api/v1/auth/dev-token?user_id=submit-user&tenant_id=t1")
    h = {"Authorization": f"Bearer {t.json()['token']}"}

    c = client.post("/api/v1/jobs?file_count=1&invoice_estimated=1", headers=h)
    assert c.status_code == 200
    job_id = c.json()["job"]["job_id"]

    s = client.post(
        f"/api/v1/jobs/{job_id}/submit?target=xero&flow_mode=auto",
        headers=h,
        files={"file": ("a.jpg", b"fake-image", "image/jpeg")},
    )
    assert s.status_code == 200
    assert s.json()["status"] == "queued"

    item = get_queue_item(job_id)
    assert item is not None
    artifact_ref = item["payload"].get("artifact_ref")
    assert artifact_ref
    assert artifact_exists_local(artifact_ref)

    out = process_one_queued_job()
    assert out is not None
    assert out["job_id"] == job_id
    assert out["status"] in {"completed", "failed"}

    assert artifact_exists_local(artifact_ref) is False

    jr = client.get(f"/api/v1/jobs/{job_id}/result", headers=h)
    assert jr.status_code == 200
    assert jr.json()["job_result"]["status"] in {"completed", "failed"}

    # job-level metrics are tracked
    job = get_job_owned("t1", "submit-user", job_id)
    assert job is not None
    assert int(job.get("invoice_extracted_count", 0)) >= 0
    assert float(job.get("extracted_total_amount", 0.0)) >= 0.0


def test_queue_backend_servicebus_without_config_fails_fast():
    from app.queue_backend import QueueBackend, QueueMessage

    qb = QueueBackend("servicebus")
    try:
        qb.enqueue(QueueMessage(job_id="j1", tenant_id="t1", user_id="u1", payload={}))
        assert False, "expected servicebus_not_configured"
    except RuntimeError as e:
        assert "servicebus_not_configured" in str(e)


def test_worker_retry_then_fail_after_max_attempts():
    import time
    import app.worker as worker_mod

    reset_all_jobs_for_tests()
    t = client.get("/api/v1/auth/dev-token?user_id=retry-user&tenant_id=t1")
    h = {"Authorization": f"Bearer {t.json()['token']}"}

    c = client.post("/api/v1/jobs?file_count=1&invoice_estimated=1", headers=h)
    assert c.status_code == 200
    job_id = c.json()["job"]["job_id"]

    s = client.post(
        f"/api/v1/jobs/{job_id}/submit?target=xero&flow_mode=auto",
        headers=h,
        files={"file": ("a.jpg", b"fake-image", "image/jpeg")},
    )
    assert s.status_code == 200

    # break artifact before worker consumes, to force retry/failure path
    q0 = get_queue_item(job_id)
    assert q0 is not None
    artifact_ref = q0["payload"].get("artifact_ref")
    assert artifact_ref and artifact_exists_local(artifact_ref)
    from pathlib import Path

    Path(artifact_ref.replace("localfs://", "", 1)).unlink()

    old_max = worker_mod.settings.max_job_attempts
    old_backoff = worker_mod.settings.retry_backoff_seconds
    worker_mod.settings.max_job_attempts = 2
    worker_mod.settings.retry_backoff_seconds = 1
    try:
        out1 = process_one_queued_job()
        assert out1 is not None and out1["status"] == "failed"
        q1 = get_queue_item(job_id)
        assert q1 is not None
        assert q1["status"] == "queued"
        assert q1.get("last_error")

        time.sleep(1.1)
        out2 = process_one_queued_job()
        assert out2 is not None and out2["status"] == "failed"
        q2 = get_queue_item(job_id)
        assert q2 is not None
        assert q2["status"] == "failed"
    finally:
        worker_mod.settings.max_job_attempts = old_max
        worker_mod.settings.retry_backoff_seconds = old_backoff


def test_root_page_contains_logged_in_text_for_client_a_flow():
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "build=auth-ui-v3" in html
    assert "Login as client A" in html
    assert "refreshAuthFromCookieOrStorage" in html
    assert "window.addEventListener('load', async function(){" in html
    assert "onclick=\"loginAs('client-a'); return false;\"" in html
    assert "Script loaded, waiting login..." in html
    assert r.headers.get("cache-control", "").startswith("no-store")


def test_billing_trial_and_topup_plan_pricing_contract():
    t = client.get("/api/v1/auth/dev-token?user_id=pricing-user&tenant_id=t1")
    h = {"Authorization": f"Bearer {t.json()['token']}"}

    me = client.get("/api/v1/billing/me", headers=h)
    assert me.status_code == 200
    body = me.json()
    assert body["account"]["trial"]["invoice_limit"] == 15
    assert body["account"]["trial"]["active"] is True

    p = client.post("/api/v1/billing/plan?plan_id=starter_14_95", headers=h)
    assert p.status_code == 200
    assert p.json()["account"]["plan_id"] == "starter_14_95"
    assert p.json()["account"]["price_aud"] == 14.95
    assert p.json()["account"]["plan"]["invoice_limit"] == 100

    p2 = client.post("/api/v1/billing/plan?plan_id=pro_29_95", headers=h)
    assert p2.status_code == 200
    assert p2.json()["account"]["plan_id"] == "pro_29_95"
    assert p2.json()["account"]["price_aud"] == 29.95
    assert p2.json()["account"]["plan"]["invoice_limit"] == 300

    # topup is blocked before paid plan
    t2 = client.get("/api/v1/auth/dev-token?user_id=pricing-trial&tenant_id=t1")
    h2 = {"Authorization": f"Bearer {t2.json()['token']}"}
    tp_block = client.post("/api/v1/billing/topup?packs=1", headers=h2)
    assert tp_block.status_code == 403
    assert tp_block.json()["detail"] == "topup_requires_paid_plan"

    # starter/pro can topup
    tp = client.post("/api/v1/billing/topup?packs=2", headers=h)
    assert tp.status_code == 200
    assert tp.json()["pack_size"] == 50
    assert tp.json()["pack_price_aud"] == 5.95
    assert tp.json()["added_invoices"] == 100
