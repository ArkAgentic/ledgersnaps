import os

from fastapi.testclient import TestClient

from app.main import app
from app.store import get_queue_item, reset_all_jobs_for_tests
from app.worker import process_claimed_item, process_one_queued_job


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
    assert r.json()["account"]["plan_id"] == "trial"

    r2 = client.post("/api/v1/billing/plan?plan_id=pro_39_9", headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200
    assert r2.json()["account"]["plan_id"] == "pro_39_9"
    assert r2.json()["account"]["invoice_limit"] == 500


def test_auth_dev_token_then_billing_me_with_bearer():
    t = client.get("/api/v1/auth/dev-token?user_id=user-a&tenant_id=t1")
    assert t.status_code == 200
    token = t.json()["token"]

    r = client.get("/api/v1/billing/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["account"]["user_id"] == "user-a"
    assert r.json()["auth"]["tenant_id"] == "t1"
    assert r.json()["auth"]["source"] == "bearer"


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


def test_queue_backend_servicebus_without_config_fails_fast():
    from app.queue_backend import QueueBackend, QueueMessage

    qb = QueueBackend("servicebus")
    try:
        qb.enqueue(QueueMessage(job_id="j1", tenant_id="t1", user_id="u1", payload={}))
        assert False, "expected servicebus_not_configured"
    except RuntimeError as e:
        assert "servicebus_not_configured" in str(e)


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
