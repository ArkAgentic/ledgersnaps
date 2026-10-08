from io import BytesIO
import os
import secrets
import time
import hashlib
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal
from typing import Optional
from typing import cast

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile, Request
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from .abn import abr_lookup_by_name, compliance_warnings_for_abn, lookup_abn_context
from .auth import CurrentUser, bearer_scheme, issue_dev_token, resolve_current_user
from .billing import PLAN_CATALOG, TOPUP_PACK_PRICE_AUD, TOPUP_PACK_SIZE, account_snapshot, add_topup, can_consume, consume_invoices, set_plan
from .config import settings
from .exporter import build_multifile_extraction_workbook
from .flow import classify_document_flow
from .llm import parse_invoice_with_azure_openai
from .myob_mapper import to_myob_draft_bill
from .otp import otp_provider
from .queue_backend import QueueBackend, QueueMessage
from .risk_control import check_trial_eligibility, record_trial_claim, validate_phone_e164
from .reset_mailer import send_password_reset_email, send_signup_verification_email
from .rules import extract_invoice_rules
from .schemas import (
    BatchExtractAndMapResponse,
    ExtractAndMapChunkResult,
    ExtractionMeta,
    ExtractAndMapResponse,
    ExtractResponse,
    InvoiceData,
    MultiFileBatchItem,
    MultiFileBatchResponse,
    UsageCost,
)
from .splitter import split_pdf_auto
from .storage_backend import StorageBackend
from .store import (
    append_signup_audit,
    create_job,
    create_password_reset_token,
    create_pending_signup_token,
    delete_xero_connection,
    get_oauth_identity,
    get_password_reset_token,
    get_pending_signup_token,
    get_latest_pending_signup_by_email,
    get_user_by_email,
    get_user_by_id,
    get_user_by_phone,
    get_local_credential,
    get_job_owned,
    get_job_result_owned,
    list_jobs_owned,
    mark_password_reset_token_used,
    mark_pending_signup_token_email_verified,
    mark_pending_signup_token_used,
    upsert_oauth_identity,
    upsert_local_credential,
    set_job_status_owned,
    upsert_job_result_owned,
    update_job_submission_owned,
    upsert_xero_connection,
)
from .store import list_queue_counts
from .validators import validate_pdf_bytes
from .xero_mapper import to_xero_accpay_draft
from .xero_oauth import (
    build_public_connect_url,
    build_connect_url,
    create_draft_invoice,
    exchange_code_public,
    exchange_code,
    get_connection_status,
    get_connection_status_live,
    set_active_tenant,
)
from .xero_payload_validator import validate_xero_draft_payload

app = FastAPI(title="LedgerSnaps API", version="0.4.1")

_ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"
if _ASSETS_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(_ASSETS_DIR)), name="assets")
queue_backend = QueueBackend(settings.queue_backend)
_OAUTH_SIGNUP_CACHE: dict[str, dict] = {}


def _hash_password(password: str, *, salt_hex: Optional[str] = None, rounds: int = 120_000) -> tuple[str, str]:
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return salt.hex(), digest.hex()


def _verify_password(password: str, *, salt_hex: str, digest_hex: str) -> bool:
    _salt, computed = _hash_password(password, salt_hex=salt_hex)
    return secrets.compare_digest(computed, digest_hex)


def _normalize_signup_phone(phone_local: str) -> str:
    digits = "".join(ch for ch in str(phone_local or "") if ch.isdigit())
    if len(digits) != 9:
        raise ValueError("invalid_phone_local")
    return f"+61{digits}"


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    dev_token: Optional[str] = Header(default=None, alias="X-Dev-Token"),
) -> CurrentUser:
    return resolve_current_user(credentials, x_user_id, dev_token)


def _normalize_upload_inputs(files: Optional[list[UploadFile]], file: Optional[UploadFile]) -> list[UploadFile]:
    """统一入口：优先使用 files，兼容旧前端传 file。"""
    if files and len(files) > 0:
        return files
    if file is not None:
        return [file]
    raise HTTPException(status_code=422, detail="missing upload: provide files[] (preferred) or file")


def _raise_invoice_limit_exceeded(estimated: int, max_invoices: int = 20) -> None:
    raise HTTPException(
        status_code=400,
        detail=(
            f"invoice_count_exceeded: estimated={estimated}, limit={max_invoices}. "
            f"Please upload at most {max_invoices} invoices/bills per run."
        ),
    )


def _autofill_xero_draft_payload(payload: dict) -> dict:
    """Autofill minimal Xero-required fields to reduce draft upload failures."""
    out = dict(payload)

    if not out.get("LineAmountTypes"):
        out["LineAmountTypes"] = "Exclusive"

    inv_date = out.get("Date")
    if not inv_date:
        inv_date = date.today().isoformat()
        out["Date"] = inv_date

    if not out.get("DueDate"):
        try:
            d = datetime.strptime(str(inv_date), "%Y-%m-%d").date()
            out["DueDate"] = (d + timedelta(days=14)).isoformat()
        except Exception:
            out["DueDate"] = (date.today() + timedelta(days=14)).isoformat()

    if not out.get("InvoiceNumber"):
        out["InvoiceNumber"] = f"LS-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

    default_account = os.getenv("XERO_DEFAULT_ACCOUNT_CODE", "400").strip() or "400"
    line_items = out.get("LineItems") or []
    if isinstance(line_items, list):
        for li in line_items:
            if isinstance(li, dict) and not li.get("AccountCode"):
                li["AccountCode"] = default_account
    out["LineItems"] = line_items
    return out


def _estimate_invoices_from_upload(filename: str, content_type: str, file_bytes: bytes) -> int:
    """提取前的快速发票数估计：图片=1；PDF 用 max(split chunk, page count)。"""
    name = (filename or "").lower()
    if content_type.startswith("image/"):
        return 1
    if name.endswith(".pdf"):
        split_count = 1
        page_count = 1
        try:
            chunks, _, _ = split_pdf_auto(file_bytes)
            split_count = max(1, len(chunks))
        except Exception:
            pass
        try:
            from pypdf import PdfReader

            page_count = max(1, len(PdfReader(BytesIO(file_bytes)).pages))
        except Exception:
            pass
        return max(split_count, page_count)
    return 1


def _validate_upload(file_bytes: bytes, filename: str, content_type: str) -> None:
    """L0: 上传预检（格式、大小、页数）。"""
    suffix = Path(filename).suffix.lower()
    max_bytes = settings.max_file_mb * 1024 * 1024
    if len(file_bytes) > max_bytes:
        raise HTTPException(status_code=400, detail=f"File exceeds size limit of {settings.max_file_mb}MB")

    if suffix == ".pdf":
        try:
            validate_pdf_bytes(file_bytes, max_pages=settings.max_pdf_pages, max_mb=settings.max_file_mb)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    elif not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image files or PDF are supported")


def _apply_flow(invoice: InvoiceData, override: Literal["auto", "ap", "ar"]) -> InvoiceData:
    """
    L0/L4: 文档流向（AP/AR）决策。
    - auto: 系统自动分类
    - ap/ar: 用户强制覆盖
    """
    if override == "ap":
        invoice.document_flow = "ap"
        invoice.flow_confidence = 1.0
        invoice.flow_reasons = ["user_override"]
        invoice.flow_overridden_by_user = True
        return invoice
    if override == "ar":
        invoice.document_flow = "ar"
        invoice.flow_confidence = 1.0
        invoice.flow_reasons = ["user_override"]
        invoice.flow_overridden_by_user = True
        return invoice

    flow, conf, reasons = classify_document_flow(invoice)
    invoice.document_flow = flow
    invoice.flow_confidence = conf
    invoice.flow_reasons = reasons
    invoice.flow_overridden_by_user = False
    return invoice


def _is_tax_invoice_like(invoice: InvoiceData, filename: str) -> bool:
    """文档类型粗判：用于识别 quote/proforma/statement 风险。"""
    text = " ".join(
        [
            (invoice.reference or ""),
            (invoice.invoice_num or ""),
            (invoice.supplier_invoice_number or ""),
            filename,
        ]
    ).lower()
    bad_hints = ["quote", "proforma", "statement"]
    if any(h in text for h in bad_hints):
        return False
    return True


def _build_validation_warnings(target: Literal["xero", "myob"], invoice: InvoiceData) -> list[str]:
    """L3: 映射前校验（按目标系统必填字段 + 金额一致性）。"""
    warnings: list[str] = []

    # AP / AR 核心必填（与系统要求对齐）
    if invoice.document_flow == "ap":
        if not invoice.vendor_name:
            warnings.append(f"{target}: missing supplier/contact name (AP compulsory)")
        if not invoice.date:
            warnings.append(f"{target}: missing bill/invoice date (AP compulsory)")
        if not invoice.line_items and invoice.total is None:
            warnings.append(f"{target}: missing line items and total (AP compulsory)")
    elif invoice.document_flow == "ar":
        if not invoice.vendor_name:
            warnings.append(f"{target}: missing customer/contact name (AR compulsory)")
        if not invoice.date:
            warnings.append(f"{target}: missing invoice date (AR compulsory)")
        if not invoice.line_items and invoice.total is None:
            warnings.append(f"{target}: missing line items and total (AR compulsory)")

    # accounting consistency warnings
    if invoice.subtotal is not None and invoice.gst is not None and invoice.total is not None:
        expected = round(invoice.subtotal + invoice.gst, 2)
        if abs(expected - round(invoice.total, 2)) > 0.05:
            warnings.append(
                f"amount mismatch: subtotal({invoice.subtotal}) + gst({invoice.gst}) != total({invoice.total})"
            )

    return warnings


@app.get("/auth/xero/callback", response_class=HTMLResponse)
async def xero_callback_page() -> HTMLResponse:
    html = """
<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Xero Sign-in</title></head>
<body style='font-family:system-ui;max-width:680px;margin:40px auto;padding:0 16px;color:#111'>
<h2>Connecting Xero...</h2>
<p id='msg' style='color:#555'>Please wait while we complete sign-in.</p>
<script>
(async function(){
  const params = new URLSearchParams(location.search);
  const code = params.get('code');
  const state = params.get('state');
  const msg = document.getElementById('msg');
  if(!code || !state){ msg.textContent='Missing OAuth code/state.'; return; }
  try{
    const r = await fetch(`/api/v1/auth/xero/callback/public?code=${encodeURIComponent(code)}&state=${encodeURIComponent(state)}`);
    const j = await r.json();
    if(!r.ok){ throw new Error(j.detail || 'xero_callback_failed'); }

    if(j.status === 'signed_in' && j.token){
      try { localStorage.setItem('ledgersnaps_dev_token', j.token); } catch(e) {}
      document.cookie = 'ledgersnaps_dev_token='+encodeURIComponent(j.token)+'; path=/; max-age='+(24*3600)+'; samesite=lax';
      msg.textContent='Signed in. Redirecting...';
      location.href = j.redirect || '/';
      return;
    }

    if(j.status === 'signup_required' && j.oauth_session_token){
      const q = new URLSearchParams();
      q.set('oauth_session_token', j.oauth_session_token);
      if(j.prefill && j.prefill.email) q.set('email', j.prefill.email);
      if(j.prefill && j.prefill.full_name) q.set('full_name', j.prefill.full_name);
      location.href = '/signup/complete?' + q.toString();
      return;
    }

    throw new Error('unexpected_callback_payload');
  }catch(e){
    msg.textContent = 'Xero sign-in failed: ' + String(e.message || e);
  }
})();
</script>
</body></html>
"""
    return HTMLResponse(content=html)


@app.get("/signup/complete", response_class=HTMLResponse)
async def signup_complete_page() -> HTMLResponse:
    html = """
<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Complete Signup</title></head>
<body style='font-family:system-ui;max-width:680px;margin:40px auto;padding:0 16px;color:#111'>
<h2>Complete your account setup</h2>
<p style='color:#555'>We need your Australian mobile verification before starting trial.</p>
<form id='f' style='display:grid;gap:10px;max-width:460px'>
  <input id='user_id' placeholder='User ID (e.g. your email prefix)' required />
  <input id='email' placeholder='Email' type='email' required />
  <input id='full_name' placeholder='Full name' required />
  <input id='phone' placeholder='+61400111222' required />
  <div style='display:flex;gap:8px'>
    <input id='otp' placeholder='OTP code' required style='flex:1' />
    <button id='send' type='button'>Send OTP</button>
  </div>
  <button type='submit'>Complete Signup</button>
</form>
<p id='msg' style='margin-top:12px;color:#555'></p>
<script>
const params = new URLSearchParams(location.search);
const tok = params.get('oauth_session_token') || '';
if(params.get('email')) document.getElementById('email').value = params.get('email');
if(params.get('full_name')) document.getElementById('full_name').value = params.get('full_name');

const msg = document.getElementById('msg');

document.getElementById('send').addEventListener('click', async ()=>{
  const phone = document.getElementById('phone').value.trim();
  if(!phone){ msg.textContent='Phone required'; return; }
  const r = await fetch(`/api/v1/auth/phone/send-code?phone_e164=${encodeURIComponent(phone)}`, {method:'POST'});
  const j = await r.json();
  if(!r.ok){ msg.textContent='Send OTP failed: '+(j.detail||''); return; }
  msg.textContent = 'OTP sent.' + (j.dev_code ? (' Dev code: '+j.dev_code) : '');
  if(j.dev_code) document.getElementById('otp').value = j.dev_code;
});

document.getElementById('f').addEventListener('submit', async (e)=>{
  e.preventDefault();
  if(!tok){ msg.textContent='Missing oauth session token'; return; }
  const q = new URLSearchParams({
    oauth_session_token: tok,
    user_id: document.getElementById('user_id').value.trim(),
    email: document.getElementById('email').value.trim(),
    full_name: document.getElementById('full_name').value.trim(),
    phone_e164: document.getElementById('phone').value.trim(),
    phone_otp_code: document.getElementById('otp').value.trim(),
    device_fingerprint: 'web-signup-complete'
  });
  const r = await fetch('/api/v1/auth/oauth/complete-signup?'+q.toString(), {method:'POST'});
  const j = await r.json();
  if(!r.ok){ msg.textContent = 'Signup failed: '+(j.detail||''); return; }
  if(j.token){
    try { localStorage.setItem('ledgersnaps_dev_token', j.token); } catch(e) {}
    document.cookie = 'ledgersnaps_dev_token='+encodeURIComponent(j.token)+'; path=/; max-age='+(24*3600)+'; samesite=lax';
  }
  msg.textContent = 'Signup complete. Redirecting...';
  location.href = j.redirect || '/';
});
</script>
</body></html>
"""
    return HTMLResponse(content=html)



def _render_template_or_500(name: str, missing_code: str) -> HTMLResponse:
    templates_dir = Path(__file__).resolve().parents[1] / "app" / "templates"
    template = templates_dir / name
    if not template.exists():
        raise HTTPException(status_code=500, detail=missing_code)
    html = template.read_text(encoding="utf-8")
    footer_partial = templates_dir / "partials" / "footer-shared.html"
    if "{{FOOTER_SHARED}}" in html and footer_partial.exists():
        html = html.replace("{{FOOTER_SHARED}}", footer_partial.read_text(encoding="utf-8"))
    return HTMLResponse(content=html)


@app.get("/", response_class=HTMLResponse)
async def landing_page() -> HTMLResponse:
    return _render_template_or_500("landing-main.html", "landing_template_missing")


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page() -> HTMLResponse:
    return _render_template_or_500("privacy.html", "privacy_template_missing")


@app.get("/terms", response_class=HTMLResponse)
async def terms_page() -> HTMLResponse:
    return _render_template_or_500("terms.html", "terms_template_missing")


@app.get("/playground", response_class=HTMLResponse)
async def playground() -> HTMLResponse:
    html = """
<!doctype html><html><body style='font-family:system-ui;max-width:860px;margin:24px auto;padding:0 12px;'>
<h2>LedgerSnaps Phase-1 Extract Test</h2>
<div style='margin:6px 0;color:#888;font-size:12px;'>build=auth-ui-v3</div>
<p>Upload invoices/bills and run unified batch extract+map.</p>
<div id='auth' style='margin:8px 0;color:#222;'>Not logged in</div>
<div style='margin:8px 0;padding:8px;border:1px solid #ddd;'>
  <div style='font-size:12px;color:#666;margin-bottom:6px;'>Phone verification (trial claim)</div>
  <input id='phone' placeholder='+61400111222' style='width:180px;' />
  <button id='sendCode'>Send Code</button>
  <input id='otp' placeholder='OTP code' style='width:100px;' />
</div>
<button id='loginA' onclick="loginAs('client-a'); return false;">Login as client A</button>
<button id='loginB' onclick="loginAs('client-b'); return false;">Login as client B</button>
<div id='debug' style='margin:8px 0;color:#666;font-size:12px;white-space:pre-wrap;'></div>
<input id='file' type='file' accept='image/*,.pdf' multiple />
<select id='target'><option value='xero'>xero</option><option value='myob'>myob</option></select>
<select id='flow'><option value='auto'>auto-flow</option><option value='ap'>force-ap</option><option value='ar'>force-ar</option></select>
<button id='runBatch'>Extract+Map Batch</button>
<button id='runExport'>Export XLSX</button>
<div id='selected' style='margin-top:8px;color:#333;'></div>
<div id='billing' style='margin-top:8px;color:#555;'></div>
<pre id='out' style='white-space:pre-wrap;border:1px solid #ddd;padding:12px;min-height:180px'></pre>
<script>
var out=document.getElementById('out');
var billing=document.getElementById('billing');
var selected=document.getElementById('selected');
var fileInput=document.getElementById('file');
var auth=document.getElementById('auth');
var debug=document.getElementById('debug');
var authToken='';
var authUser='';
auth.textContent='Script loaded, waiting login...';

function logDebug(msg){
  if(!debug){ return; }
  var now = new Date().toLocaleTimeString();
  debug.textContent = '['+now+'] '+msg+'\\n' + (debug.textContent || '');
}

async function loginAs(userId, tenantId='default'){
  logDebug('loginAs called: '+userId+' tenant='+tenantId);
  var phone = (document.getElementById('phone').value || '').trim();
  var otp = (document.getElementById('otp').value || '').trim();
  var q = `/api/v1/auth/dev-token?user_id=${encodeURIComponent(userId)}&tenant_id=${encodeURIComponent(tenantId)}`;
  if(phone){
    q += `&phone_e164=${encodeURIComponent(phone)}`;
    if(otp){ q += `&phone_otp_code=${encodeURIComponent(otp)}`; }
    q += `&device_fingerprint=${encodeURIComponent('browser-local')}`;
  }
  const r = await fetch(q);
  logDebug('token endpoint status='+r.status);
  if(!r.ok){
    auth.textContent='Login failed';
    logDebug('login failed');
    return;
  }
  const j=await r.json();
  authToken=j.token;
  authUser=j.user_id;
  try { localStorage.setItem('ledgersnaps_dev_token', authToken); localStorage.setItem('ledgersnaps_user', authUser); } catch(e) {}
  try { document.cookie = 'ledgersnaps_dev_token='+encodeURIComponent(authToken)+'; path=/; max-age='+(24*3600)+'; samesite=lax'; } catch(e) {}
  auth.textContent=`Logged in as ${authUser}`;
  logDebug('login ok as '+authUser);
}

document.getElementById('sendCode').addEventListener('click', async function(){
  var phone = (document.getElementById('phone').value || '').trim();
  if(!phone){
    logDebug('phone is required');
    return;
  }
  const r = await fetch(`/api/v1/auth/phone/send-code?phone_e164=${encodeURIComponent(phone)}`, {method:'POST'});
  const j = await r.json();
  logDebug('send-code status='+r.status+' provider='+(j.provider||'n/a'));
  if(j.dev_code){
    document.getElementById('otp').value = j.dev_code;
    logDebug('dev_code auto-filled for local testing');
  }
});

async function refreshAuthFromCookieOrStorage(){
  try{
    var me = await fetch('/api/v1/auth/me', {headers:{'X-Dev-Token': authToken}, credentials:'same-origin'});
    if(me.ok){
      var m = await me.json();
      authUser = m.user_id || authUser;
      auth.textContent='Logged in as '+authUser;
      logDebug('auth/me ok source='+m.source+' user='+authUser);
      return true;
    }
  }catch(e){
    logDebug('auth/me request error');
  }
  try{
    var t = localStorage.getItem('ledgersnaps_dev_token') || '';
    var u = localStorage.getItem('ledgersnaps_user') || '';
    if(t){
      authToken=t;
      authUser=u || authUser;
      var me2 = await fetch('/api/v1/auth/me', {headers:{'X-Dev-Token': authToken}, credentials:'same-origin'});
      if(me2.ok){
        var m2 = await me2.json();
        authUser = m2.user_id || authUser;
      }
      auth.textContent='Logged in as '+(authUser || 'cached-user');
      logDebug('restored token from localStorage');
      return true;
    }
  }catch(e){
    logDebug('localStorage unavailable');
  }
  return false;
}

document.getElementById('loginA').addEventListener('click', function(){ logDebug('click loginA'); });
document.getElementById('loginB').addEventListener('click', function(){ logDebug('click loginB'); });
function renderSelected(){
  const files=[...fileInput.files];
  if(files.length===0){ selected.textContent='Selected files: 0'; return; }
  selected.textContent=`Selected files: ${files.length} -> ${files.map(f=>f.name).join(', ')}`;
}
fileInput.addEventListener('change', renderSelected);
renderSelected();
function costLine(meta){
  if(!meta||!meta.usage) return 'cost: n/a';
  return `cost_estimate_usd=${meta.usage.estimated_cost_usd} (in=${meta.usage.input_tokens}, out=${meta.usage.output_tokens})`;
}
async function run(path){
  var files=Array.prototype.slice.call(document.getElementById('file').files || []);
  if(files.length===0){out.textContent='Please choose file(s)';return;}
  out.textContent=`Submitting ${files.length} file(s)...`;
  var fd=new FormData();
  if(path.includes('/batch/multi') || path.includes('/unified/export.xlsx') || path.includes('/extract-and-map/export.xlsx')){
    for(var i=0;i<files.length;i++){ fd.append('files', files[i]); }
  }else{
    fd.append('file', files[0]);
  }
  var target=document.getElementById('target').value;
  var flow=document.getElementById('flow').value;
  var q=path.indexOf('extract-and-map')>=0?('?target='+encodeURIComponent(target)+'&flow_mode='+encodeURIComponent(flow)):'';
  if(!authToken){
    out.textContent='Please login first (client A/client B)';
    return;
  }
  let r;
  try{
    r=await fetch(path+q,{method:'POST',body:fd,headers:{'Authorization':`Bearer ${authToken}`,'X-Client-File-Count':String(files.length)}});
  }catch(err){
    out.textContent='Network error: '+String(err);
    return;
  }

  if(path.includes('export.xlsx')){
    if(!r.ok){
      const j=await r.json();
      out.textContent=JSON.stringify({status:r.status,...j},null,2);
      return;
    }
    var blob = await r.blob();
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    var many = files.length > 1;
    a.download = many ? `batch-extraction-${files.length}-files.xlsx` : (files[0].name.replace(/\.[^.]+$/, '') || 'invoice') + '-extraction.xlsx';
    a.click();
    URL.revokeObjectURL(url);
    var received = r.headers.get('X-Received-Files');
    out.textContent = 'xlsx exported: '+a.download+' | selected='+files.length+' | received='+(received ? received : 'n/a');
    return;
  }

  var j;
  try{
    j=await r.json();
  }catch(err){
    out.textContent='Response parse error (non-JSON): status='+r.status;
    return;
  }
  out.textContent = costLine(j.meta) + '\\n\\n' + JSON.stringify({status:r.status,...j},null,2);
  try {
    const b = await fetch('/api/v1/billing/me', {headers:{'Authorization':`Bearer ${authToken}`}}).then(x=>x.json());
    const a = b.account;
    const t = a.trial || {};
    const p = a.plan || {};
    const top = a.topup || {};
    billing.textContent = `User=${authUser} | Plan=${a.plan_id} | Remaining=${a.remaining_invoices} | Trial=${t.remaining_invoices||0}/${t.invoice_limit||0} | Plan=${p.remaining_invoices||0}/${p.invoice_limit||0} | Topup=${top.remaining_invoices||0}`;
  } catch (e) {}
}
document.getElementById('runBatch').onclick=function(){ run('/api/v1/extract-and-map/batch/multi'); };
document.getElementById('runExport').onclick=function(){ run('/api/v1/extract-and-map/unified/export.xlsx'); };
// 页面初始化
auth.textContent='Initializing...';
window.addEventListener('load', async function(){
  logDebug('window load');
  var ok = await refreshAuthFromCookieOrStorage();
  if(!ok){
    loginAs('client-a');
  }
});
</script></body></html>
"""
    return HTMLResponse(
        content=html,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/health")
async def health() -> dict:
    endpoint_ok = bool(os.getenv("AZURE_OPENAI_ENDPOINT", "").strip())
    key_ok = bool(os.getenv("AZURE_OPENAI_API_KEY", "").strip())
    deployment_ok = bool(os.getenv("AZURE_OPENAI_DEPLOYMENT", "").strip())
    abr_guid_ok = bool(os.getenv("ABR_GUID", "").strip())
    aoai_configured = endpoint_ok and key_ok and deployment_ok

    return {
        "status": "ok",
        "checks": {
            "aoai_configured": aoai_configured,
            "aoai": {
                "endpoint": endpoint_ok,
                "api_key": key_ok,
                "deployment": deployment_ok,
            },
            "abr_guid_configured": abr_guid_ok,
        },
    }


@app.post("/api/v1/extract", response_model=ExtractResponse)
async def extract_invoice(file: UploadFile = File(...)) -> ExtractResponse:
    filename = file.filename or "upload"
    content_type = (file.content_type or "").lower()
    file_bytes = await file.read()

    _validate_upload(file_bytes, filename, content_type)

    rules_invoice, rules_warnings, rules_confident = extract_invoice_rules(file_bytes=file_bytes, filename=filename, content_type=content_type)
    if rules_confident:
        return ExtractResponse(
            invoice=rules_invoice,
            model="rules",
            meta=ExtractionMeta(
                method="rules",
                usage=UsageCost(),
                warnings=rules_warnings,
                trace=["L0 upload validated", "L1 StepA rules extracted", "L1 quality gate passed", "L2 LLM skipped"],
            ),
        )

    try:
        invoice, usage = await parse_invoice_with_azure_openai(file_bytes=file_bytes, filename=filename, content_type=content_type)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    return ExtractResponse(
        invoice=invoice,
        model="gpt-4o",
        meta=ExtractionMeta(
            method="llm",
            usage=usage,
            warnings=rules_warnings,
            trace=["L0 upload validated", "L1 StepA rules extracted", "L1 quality gate failed", "L2 LLM extraction executed"],
        ),
    )


async def _extract_and_map_core(
    target: Literal["xero", "myob"],
    flow_mode: Literal["auto", "ap", "ar"],
    file: UploadFile,
) -> ExtractAndMapResponse:
    filename = file.filename or "upload"
    content_type = (file.content_type or "").lower()
    file_bytes = await file.read()
    return await _extract_and_map_from_bytes(
        target=target,
        flow_mode=flow_mode,
        filename=filename,
        content_type=content_type,
        file_bytes=file_bytes,
    )


async def _extract_and_map_from_bytes(
    target: Literal["xero", "myob"],
    flow_mode: Literal["auto", "ap", "ar"],
    filename: str,
    content_type: str,
    file_bytes: bytes,
) -> ExtractAndMapResponse:
    _validate_upload(file_bytes, filename, content_type)

    split_chunks = [file_bytes]
    split_decision = "single"
    split_stats = {}
    if filename.lower().endswith(".pdf"):
        split_chunks, split_decision, split_stats = split_pdf_auto(file_bytes)

    if len(split_chunks) > 1:
        trace_prefix = [
            f"L0 split(auto) decision={split_decision}",
            f"L0 split(auto) chunks={len(split_chunks)} stats={split_stats}",
        ]
        file_bytes = split_chunks[0]
    else:
        trace_prefix = [f"L0 split(auto) decision={split_decision} stats={split_stats}"]

    if content_type.startswith("image/"):
        warnings: list[str] = []
        try:
            invoice, usage = await parse_invoice_with_azure_openai(file_bytes=file_bytes, filename=filename, content_type=content_type)
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
        model = "gpt-4o"
        method = "llm"
        trace = ["L0 upload validated", *trace_prefix, "L1 StepA rules skipped for image", "L2 LLM extraction executed"]
    else:
        rules_invoice, rules_warnings, rules_confident = extract_invoice_rules(
            file_bytes=file_bytes,
            filename=filename,
            content_type=content_type,
        )
        if rules_confident:
            invoice = rules_invoice
            model = "rules"
            usage = UsageCost()
            method = "rules"
            warnings = rules_warnings
            trace = ["L0 upload validated", *trace_prefix, "L1 StepA rules extracted", "L1 quality gate passed", "L2 LLM skipped"]
        else:
            try:
                invoice, usage = await parse_invoice_with_azure_openai(file_bytes=file_bytes, filename=filename, content_type=content_type)
            except RuntimeError as e:
                raise HTTPException(status_code=502, detail=str(e)) from e
            model = "gpt-4o"
            method = "llm"
            warnings = rules_warnings
            trace = ["L0 upload validated", *trace_prefix, "L1 StepA rules extracted", "L1 quality gate failed", "L2 LLM extraction executed"]

    invoice = _apply_flow(invoice, flow_mode)
    if invoice.document_flow == "ar" and target == "myob":
        warnings.append("classified as AR; current MYOB mapper still uses AP-bill payload shape")

    draft_payload = to_xero_accpay_draft(invoice).model_dump() if target == "xero" else to_myob_draft_bill(invoice).model_dump()

    xero_ready = None
    missing_required_fields: list[str] = []
    suggested_fixes: list[str] = []
    if target == "xero":
        xero_ready, missing_required_fields, suggested_fixes = validate_xero_draft_payload(draft_payload)

    validation_warnings = warnings + _build_validation_warnings(target, invoice)
    abr_context = await lookup_abn_context(invoice.abn, invoice.vendor_name)
    is_tax_invoice_like = _is_tax_invoice_like(invoice, filename)

    if not abr_context.get("available"):
        validation_warnings.append(
            "compliance: ABR real-time check pending (ABR_GUID not configured); GST registration validation is provisional"
        )

    if invoice.gst is not None and invoice.gst_source == "none":
        invoice.gst_extracted = invoice.gst
        invoice.gst_source = "extracted"
        invoice.gst_confidence = max(invoice.gst_confidence, 0.8)

    if invoice.gst is None:
        invoice.gst_extracted = None
        invoice.gst_inferred = None
        invoice.gst_source = "none"
        invoice.gst_confidence = 0.0
        validation_warnings.append(
            "compliance: GST not explicitly extracted from document; pending ABR/GST-registration-assisted validation"
        )

    validation_warnings.extend(
        compliance_warnings_for_abn(
            abn_raw=invoice.abn,
            gst_present=invoice.gst is not None,
            total=invoice.total,
            is_tax_invoice_like=is_tax_invoice_like,
            abr_context={**abr_context, "vendor_name": invoice.vendor_name},
            vendor_name=invoice.vendor_name,
        )
    )

    if invoice.document_flow == "unknown":
        validation_warnings.append("flow classification is unknown; user confirmation required before posting")

    return ExtractAndMapResponse(
        invoice=invoice,
        target=target,
        draft_payload=draft_payload,
        xero_ready=xero_ready,
        missing_required_fields=missing_required_fields,
        suggested_fixes=suggested_fixes,
        abr_available=abr_context.get("available"),
        abr_abn=abr_context.get("abn"),
        abr_entity_name=abr_context.get("entity_name"),
        abr_gst_registered=abr_context.get("gst_registered"),
        abr_reason=abr_context.get("reason") or abr_context.get("message"),
        validation_warnings=validation_warnings,
        model=model,
        meta=ExtractionMeta(method=method, usage=usage, warnings=validation_warnings, trace=trace),
    )


async def _extract_and_map_batch_core(
    target: Literal["xero", "myob"],
    flow_mode: Literal["auto", "ap", "ar"],
    filename: str,
    content_type: str,
    file_bytes: bytes,
    user_id: str,
) -> BatchExtractAndMapResponse:
    # 图片天然按单文档处理，避免混合上传时被 PDF split 流程吞掉
    if content_type.startswith("image/"):
        if not can_consume(user_id, 1):
            return BatchExtractAndMapResponse(
                split_decision="single",
                chunk_count=1,
                chunks=[],
                summary={"ok": 0, "failed": 1, "split_stats": {}, "billable_invoice_count": 0},
            )
        result = await _extract_and_map_from_bytes(
            target=target,
            flow_mode=flow_mode,
            filename=filename,
            content_type=content_type,
            file_bytes=file_bytes,
        )
        result.meta.trace = [
            "L0 split(batch) decision=single stats={}",
            "L0 split(batch) chunk_index=1/1",
            *result.meta.trace,
        ]
        consume_invoices(user_id, 1)
        return BatchExtractAndMapResponse(
            split_decision="single",
            chunk_count=1,
            chunks=[ExtractAndMapChunkResult(chunk_index=1, result=result)],
            summary={"ok": 1, "failed": 0, "split_stats": {}, "billable_invoice_count": 1},
        )

    split_chunks = [file_bytes]
    split_decision: Literal["single", "split"] = "single"
    split_stats = {}
    if filename.lower().endswith(".pdf"):
        split_chunks, split_decision_raw, split_stats = split_pdf_auto(file_bytes)
        split_decision = cast(Literal["single", "split"], split_decision_raw)

    chunks: list[ExtractAndMapChunkResult] = []
    ok = 0
    failed = 0
    for i, chunk_bytes in enumerate(split_chunks, start=1):
        if not can_consume(user_id, 1):
            failed += (len(split_chunks) - i + 1)
            break
        uf = UploadFile(filename=f"{Path(filename).stem}__chunk{i}.pdf", file=BytesIO(chunk_bytes), headers=None)
        try:
            result = await _extract_and_map_core(target=target, flow_mode=flow_mode, file=uf)
            # 统一把 batch 拆分判定写入每个 chunk trace
            result.meta.trace = [
                f"L0 split(batch) decision={split_decision} stats={split_stats}",
                f"L0 split(batch) chunk_index={i}/{len(split_chunks)}",
                *result.meta.trace,
            ]
            chunks.append(ExtractAndMapChunkResult(chunk_index=i, result=result))
            ok += 1
            consume_invoices(user_id, 1)
        except Exception:  # noqa: BLE001
            failed += 1

    return BatchExtractAndMapResponse(
        split_decision=split_decision,
        chunk_count=len(split_chunks),
        chunks=chunks,
        summary={
            "ok": ok,
            "failed": failed,
            "split_stats": split_stats,
            "billable_invoice_count": ok,
        },
    )


@app.post("/api/v1/auth/signup")
async def auth_signup(
    request: Request,
    full_name: str = Query(..., min_length=1),
    email: str = Query(..., min_length=3),
    phone_local: str = Query(..., min_length=9),
    password: str = Query(..., min_length=8),
    accept_terms: bool = Query(...),
) -> dict:
    if not accept_terms:
        raise HTTPException(status_code=400, detail="terms_not_accepted")

    email_norm = str(email or "").strip().lower()
    if not email_norm:
        raise HTTPException(status_code=400, detail="email_required")

    try:
        phone_e164 = _normalize_signup_phone(phone_local)
        _ = validate_phone_e164(phone_e164)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    if get_user_by_email(email_norm):
        raise HTTPException(status_code=409, detail="email_already_registered")
    if get_user_by_phone(phone_e164):
        raise HTTPException(status_code=409, detail="phone_already_registered")

    signup_ip = request.client.host if request and request.client else None
    user_agent = request.headers.get("user-agent") if request else None
    accepted_at = datetime.utcnow().isoformat()
    salt_hex, digest_hex = _hash_password(password)

    raw_token = secrets.token_urlsafe(24)
    token_hash = _sha256_hex(raw_token)
    expires_at = (datetime.utcnow() + timedelta(minutes=30)).isoformat()

    create_pending_signup_token(
        token_hash=token_hash,
        email=email_norm,
        full_name=full_name,
        phone_e164=phone_e164,
        password_salt=salt_hex,
        password_hash=digest_hex,
        terms_version="v1",
        accepted_at=accepted_at,
        signup_ip=signup_ip,
        user_agent=user_agent,
        expires_at=expires_at,
    )

    verify_code = f"{int(token_hash[:12], 16) % 1000000:06d}"
    try:
        host = (request.headers.get("host") if request else "") or "ledgersnaps.com"
        scheme = (request.url.scheme if request else "https") or "https"
        verify_link = f"{scheme}://{host}/api/v1/auth/signup/verify?token={raw_token}"
        send_signup_verification_email(to_email=email_norm, verify_link=verify_link, verify_code=verify_code)
    except Exception:
        pass

    out = {"status": "verification_sent"}
    if os.getenv("RESET_EMAIL_PROVIDER", "dev").strip().lower() == "dev":
        out["dev_verify_code"] = verify_code
    return out


@app.get("/api/v1/auth/signup/verify")
async def auth_signup_verify(request: Request, token: str = Query(..., min_length=12)) -> dict:
    token_hash = _sha256_hex(token)
    rec = get_pending_signup_token(token_hash)
    if not rec or str(rec.get("status")) != "pending":
        raise HTTPException(status_code=400, detail="invalid_or_expired_signup_token")

    exp = str(rec.get("expires_at") or "")
    if not exp:
        raise HTTPException(status_code=400, detail="invalid_or_expired_signup_token")
    try:
        if datetime.utcnow() > datetime.fromisoformat(exp):
            raise HTTPException(status_code=400, detail="invalid_or_expired_signup_token")
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_or_expired_signup_token")

    email_norm = str(rec.get("email") or "").strip().lower()
    phone_e164 = str(rec.get("phone_e164") or "").strip()
    full_name = str(rec.get("full_name") or "").strip()

    if get_user_by_email(email_norm) or get_user_by_phone(phone_e164):
        mark_pending_signup_token_used(token_hash)
        raise HTTPException(status_code=409, detail="account_already_exists")

    user_id = f"u-{secrets.token_hex(8)}"
    try:
        record_trial_claim(
            user_id,
            phone_e164=phone_e164,
            device_fingerprint="landing-signup-verified",
            signup_ip=None,
            email=email_norm,
            full_name=full_name,
        )
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e

    upsert_local_credential(
        user_id,
        password_salt=str(rec.get("password_salt") or ""),
        password_hash=str(rec.get("password_hash") or ""),
    )

    append_signup_audit(
        user_id=user_id,
        email=email_norm,
        phone_e164=phone_e164,
        accepted_terms=True,
        terms_version=str(rec.get("terms_version") or "v1"),
        accepted_at=str(rec.get("accepted_at") or datetime.utcnow().isoformat()),
        signup_ip=str(rec.get("signup_ip") or "") or None,
        user_agent=str(rec.get("user_agent") or "") or None,
    )
    mark_pending_signup_token_used(token_hash)

    token_out = issue_dev_token(user_id=user_id, tenant_id="default")
    return {"status": "signed_up", "user_id": user_id, "token": token_out, "redirect": "/dashboard/upload"}


@app.post("/api/v1/auth/signup/resend-code")
async def auth_signup_resend_code(
    request: Request,
    email: str = Query(..., min_length=3),
) -> dict:
    email_norm = str(email or "").strip().lower()
    pending = get_latest_pending_signup_by_email(email_norm, status="pending")
    if not pending:
        return {"status": "accepted"}

    created_at = str(pending.get("created_at") or "")
    try:
        if created_at:
            created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            now_dt = datetime.now(created_dt.tzinfo) if created_dt.tzinfo else datetime.utcnow()
            if (now_dt - created_dt).total_seconds() < 60:
                raise HTTPException(status_code=429, detail="resend_cooldown")
    except ValueError:
        pass

    token_hash = str(pending.get("token_hash") or "")
    verify_code = f"{int(token_hash[:12], 16) % 1000000:06d}" if token_hash else ""
    if not verify_code:
        return {"status": "accepted"}

    try:
        host = (request.headers.get("host") if request else "") or "ledgersnaps.com"
        scheme = (request.url.scheme if request else "https") or "https"
        verify_link = f"{scheme}://{host}/"
        send_signup_verification_email(to_email=email_norm, verify_link=verify_link, verify_code=verify_code)
    except Exception:
        pass

    return {"status": "resent"}


@app.post("/api/v1/auth/signup/verify-code")
async def auth_signup_verify_code(
    email: str = Query(..., min_length=3),
    code: str = Query(..., min_length=6, max_length=6),
) -> dict:
    email_norm = str(email or "").strip().lower()
    pending = get_latest_pending_signup_by_email(email_norm, status="pending")
    if not pending or str(pending.get("status")) != "pending":
        raise HTTPException(status_code=400, detail="invalid_or_expired_signup_code")

    exp = str(pending.get("expires_at") or "")
    if not exp:
        raise HTTPException(status_code=400, detail="invalid_or_expired_signup_code")
    try:
        if datetime.utcnow() > datetime.fromisoformat(exp):
            raise HTTPException(status_code=400, detail="invalid_or_expired_signup_code")
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_or_expired_signup_code")

    token_hash = str(pending.get("token_hash") or "")
    expected = f"{int(token_hash[:12], 16) % 1000000:06d}" if token_hash else ""
    if not expected or str(code).strip() != expected:
        raise HTTPException(status_code=400, detail="invalid_or_expired_signup_code")

    mark_pending_signup_token_email_verified(token_hash)
    return {"status": "email_verified"}


@app.post("/api/v1/auth/signup/phone/verify")
async def auth_signup_phone_verify(
    email: str = Query(..., min_length=3),
    phone_otp_code: str = Query(..., min_length=4),
) -> dict:
    email_norm = str(email or "").strip().lower()
    rec = get_latest_pending_signup_by_email(email_norm, status="email_verified")
    if not rec:
        raise HTTPException(status_code=400, detail="email_verification_required")

    exp = str(rec.get("expires_at") or "")
    if not exp:
        raise HTTPException(status_code=400, detail="invalid_or_expired_signup_code")
    try:
        if datetime.utcnow() > datetime.fromisoformat(exp):
            raise HTTPException(status_code=400, detail="invalid_or_expired_signup_code")
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_or_expired_signup_code")

    phone_e164 = str(rec.get("phone_e164") or "").strip()
    if not otp_provider.verify_code(phone_e164, phone_otp_code):
        raise HTTPException(status_code=400, detail="phone_verification_required")

    if get_user_by_email(email_norm) or get_user_by_phone(phone_e164):
        mark_pending_signup_token_used(str(rec.get("token_hash") or ""))
        raise HTTPException(status_code=409, detail="account_already_exists")

    user_id = f"u-{secrets.token_hex(8)}"
    full_name = str(rec.get("full_name") or "").strip()

    try:
        record_trial_claim(
            user_id,
            phone_e164=phone_e164,
            device_fingerprint="landing-signup-verified",
            signup_ip=None,
            email=email_norm,
            full_name=full_name,
        )
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e

    upsert_local_credential(
        user_id,
        password_salt=str(rec.get("password_salt") or ""),
        password_hash=str(rec.get("password_hash") or ""),
    )

    append_signup_audit(
        user_id=user_id,
        email=email_norm,
        phone_e164=phone_e164,
        accepted_terms=True,
        terms_version=str(rec.get("terms_version") or "v1"),
        accepted_at=str(rec.get("accepted_at") or datetime.utcnow().isoformat()),
        signup_ip=str(rec.get("signup_ip") or "") or None,
        user_agent=str(rec.get("user_agent") or "") or None,
    )
    mark_pending_signup_token_used(str(rec.get("token_hash") or ""))

    token_out = issue_dev_token(user_id=user_id, tenant_id="default")
    return {"status": "signed_up", "user_id": user_id, "token": token_out, "redirect": "/dashboard/upload"}


@app.post("/api/v1/auth/signin/local")
async def auth_signin_local(
    email: str = Query(..., min_length=3),
    password: str = Query(..., min_length=1),
) -> dict:
    email_norm = str(email or "").strip().lower()
    user = get_user_by_email(email_norm)
    if not user:
        raise HTTPException(status_code=401, detail="invalid_credentials")

    cred = get_local_credential(str(user.get("user_id") or ""))
    if not cred:
        raise HTTPException(status_code=401, detail="invalid_credentials")

    if not _verify_password(password, salt_hex=str(cred.get("password_salt") or ""), digest_hex=str(cred.get("password_hash") or "")):
        raise HTTPException(status_code=401, detail="invalid_credentials")

    token = issue_dev_token(user_id=str(user.get("user_id")), tenant_id="default")
    return {
        "status": "signed_in",
        "user_id": str(user.get("user_id")),
        "token": token,
        "redirect": "/dashboard/upload",
    }



@app.post("/api/v1/auth/password/forgot")
async def auth_password_forgot(
    request: Request,
    email: str = Query(..., min_length=3),
) -> dict:
    email_norm = str(email or "").strip().lower()
    user = get_user_by_email(email_norm)
    # Always return generic response to avoid account enumeration.
    if not user:
        return {"ok": True, "status": "accepted"}

    raw_token = secrets.token_urlsafe(24)
    token_hash = _sha256_hex(raw_token)
    expires_at = (datetime.utcnow() + timedelta(minutes=20)).isoformat()
    create_password_reset_token(token_hash=token_hash, user_id=str(user.get("user_id")), expires_at=expires_at)

    # Send via email when account exists; keep response generic.
    try:
        host = (request.headers.get("host") if request else "") or "ledgersnaps.com"
        scheme = (request.url.scheme if request else "https") or "https"
        reset_link = f"{scheme}://{host}/reset-password?token={raw_token}"
        send_password_reset_email(to_email=str(user.get("email") or email_norm), reset_link=reset_link)
    except Exception:
        # keep outward response non-enumerating
        pass

    resp = {"ok": True, "status": "accepted"}
    if os.getenv("RESET_EMAIL_PROVIDER", "dev").strip().lower() == "dev":
        # local dev visibility only
        resp["dev_reset_token"] = raw_token
    return resp


@app.post("/api/v1/auth/password/reset")
async def auth_password_reset(
    reset_token: str = Query(..., min_length=12),
    new_password: str = Query(..., min_length=8),
) -> dict:
    token_hash = _sha256_hex(reset_token)
    rec = get_password_reset_token(token_hash)
    if not rec or str(rec.get("status")) != "pending":
        raise HTTPException(status_code=400, detail="invalid_or_expired_reset_token")

    exp = str(rec.get("expires_at") or "")
    if not exp:
        raise HTTPException(status_code=400, detail="invalid_or_expired_reset_token")
    try:
        if datetime.utcnow() > datetime.fromisoformat(exp):
            raise HTTPException(status_code=400, detail="invalid_or_expired_reset_token")
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_or_expired_reset_token")

    user_id = str(rec.get("user_id") or "")
    if not user_id or not get_user_by_id(user_id):
        raise HTTPException(status_code=400, detail="invalid_or_expired_reset_token")

    salt_hex, digest_hex = _hash_password(new_password)
    upsert_local_credential(user_id, password_salt=salt_hex, password_hash=digest_hex)
    mark_password_reset_token_used(token_hash)

    return {"ok": True, "status": "password_updated"}




@app.get("/api/v1/auth/dev-token")
async def auth_dev_token(
    request: Request,
    user_id: str = Query(..., min_length=1),
    tenant_id: str = Query("default", min_length=1),
    email: Optional[str] = Query(None),
    full_name: Optional[str] = Query(None),
    phone_e164: Optional[str] = Query(None),
    phone_otp_code: Optional[str] = Query(None),
    device_fingerprint: Optional[str] = Query(None),
) -> Response:
    # Trial entitlement gate (phone-bound). In dev mode OTP uses fixed code 123456.
    if phone_e164:
        try:
            _ = validate_phone_e164(phone_e164)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if not phone_otp_code or not otp_provider.verify_code(phone_e164, phone_otp_code):
            raise HTTPException(status_code=400, detail="phone_verification_required")
        try:
            check = check_trial_eligibility(phone_e164)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if not check.eligible:
            raise HTTPException(status_code=403, detail=check.reason)
        try:
            record_trial_claim(
                user_id,
                phone_e164=phone_e164,
                device_fingerprint=device_fingerprint,
                signup_ip=(request.client.host if request and request.client else None),
                email=email,
                full_name=full_name,
            )
        except ValueError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e

    token = issue_dev_token(user_id=user_id, tenant_id=tenant_id)
    body = {"token": token, "token_type": "bearer", "user_id": user_id, "tenant_id": tenant_id}
    resp = Response(
        content=__import__("json").dumps(body),
        media_type="application/json",
    )
    resp.set_cookie(
        key="ledgersnaps_dev_token",
        value=token,
        httponly=False,
        samesite="lax",
        secure=False,
        max_age=24 * 3600,
        path="/",
    )
    return resp


@app.post("/api/v1/auth/phone/send-code")
async def auth_phone_send_code(phone_e164: str = Query(..., min_length=8)) -> dict:
    try:
        out = otp_provider.send_code(phone_e164)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return out


def _issue_oauth_signup_token(payload: dict) -> str:
    tok = secrets.token_urlsafe(24)
    _OAUTH_SIGNUP_CACHE[tok] = {**payload, "created_at": int(time.time())}
    return tok


def _consume_oauth_signup_token(token: str, *, max_age_seconds: int = 600) -> dict:
    rec = _OAUTH_SIGNUP_CACHE.get(token)
    if not rec:
        raise HTTPException(status_code=400, detail="oauth_session_expired")
    created = int(rec.get("created_at", 0) or 0)
    if created <= 0 or int(time.time()) - created > max_age_seconds:
        _OAUTH_SIGNUP_CACHE.pop(token, None)
        raise HTTPException(status_code=400, detail="oauth_session_expired")
    _OAUTH_SIGNUP_CACHE.pop(token, None)
    return rec


@app.get("/api/v1/auth/xero/start")
async def auth_xero_start() -> dict:
    try:
        out = build_public_connect_url()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return out


@app.get("/api/v1/auth/xero/callback/public")
async def auth_xero_callback_public(code: str = Query(...), state: str = Query(...)) -> dict:
    try:
        oauth = await exchange_code_public(code=code, state=state)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    provider_subject_id = str(oauth.get("provider_subject_id") or "")
    provider_email = str(oauth.get("provider_email") or "").strip() or None
    full_name = str(oauth.get("full_name") or "").strip() or None

    bound = get_oauth_identity(provider="xero", provider_subject_id=provider_subject_id)
    if bound:
        user_id = str(bound.get("user_id"))
        tenant_id = str(oauth.get("tenant_id") or "")
        if tenant_id:
            upsert_xero_connection(
                user_id,
                tenant_id=tenant_id,
                access_token=str(oauth.get("access_token") or ""),
                refresh_token=str(oauth.get("refresh_token") or ""),
                token_type=oauth.get("token_type"),
                scope=oauth.get("scope"),
                expires_at=str(oauth.get("expires_at") or ""),
            )
        token = issue_dev_token(user_id=user_id, tenant_id="default")
        return {"status": "signed_in", "user_id": user_id, "token": token, "redirect": "/dashboard/upload"}

    if provider_email:
        by_email = get_user_by_email(provider_email)
        if by_email:
            user_id = str(by_email.get("user_id"))
            upsert_oauth_identity(
                user_id=user_id,
                provider="xero",
                provider_subject_id=provider_subject_id,
                provider_email=provider_email,
                provider_tenant_id=str(oauth.get("tenant_id") or "") or None,
                provider_tenant_name=str(oauth.get("tenant_name") or "") or None,
            )
            upsert_xero_connection(
                user_id,
                tenant_id=str(oauth.get("tenant_id") or ""),
                access_token=str(oauth.get("access_token") or ""),
                refresh_token=str(oauth.get("refresh_token") or ""),
                token_type=oauth.get("token_type"),
                scope=oauth.get("scope"),
                expires_at=str(oauth.get("expires_at") or ""),
            )
            token = issue_dev_token(user_id=user_id, tenant_id="default")
            return {"status": "signed_in", "user_id": user_id, "token": token, "redirect": "/dashboard/upload"}

    session_token = _issue_oauth_signup_token(
        {
            "provider": "xero",
            "provider_subject_id": provider_subject_id,
            "provider_email": provider_email,
            "full_name": full_name,
            "tenant_id": str(oauth.get("tenant_id") or "") or None,
            "tenant_name": str(oauth.get("tenant_name") or "") or None,
            "access_token": str(oauth.get("access_token") or ""),
            "refresh_token": str(oauth.get("refresh_token") or ""),
            "token_type": oauth.get("token_type"),
            "scope": oauth.get("scope"),
            "expires_at": str(oauth.get("expires_at") or ""),
        }
    )
    return {
        "status": "signup_required",
        "provider": "xero",
        "prefill": {"email": provider_email, "full_name": full_name},
        "oauth_session_token": session_token,
        "redirect": "/signup/complete",
    }


@app.post("/api/v1/auth/oauth/complete-signup")
async def auth_oauth_complete_signup(
    request: Request,
    oauth_session_token: str = Query(..., min_length=8),
    user_id: str = Query(..., min_length=1),
    email: str = Query(..., min_length=3),
    full_name: str = Query(..., min_length=1),
    phone_e164: str = Query(..., min_length=8),
    phone_otp_code: str = Query(..., min_length=4),
    device_fingerprint: Optional[str] = Query(None),
) -> dict:
    rec = _consume_oauth_signup_token(oauth_session_token)
    if str(rec.get("provider")) != "xero":
        raise HTTPException(status_code=400, detail="unsupported_oauth_provider")

    try:
        _ = validate_phone_e164(phone_e164)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not otp_provider.verify_code(phone_e164, phone_otp_code):
        raise HTTPException(status_code=400, detail="phone_verification_required")

    try:
        check = check_trial_eligibility(phone_e164)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not check.eligible:
        raise HTTPException(status_code=403, detail=check.reason)

    try:
        record_trial_claim(
            user_id,
            phone_e164=phone_e164,
            device_fingerprint=device_fingerprint,
            signup_ip=(request.client.host if request and request.client else None),
            email=email,
            full_name=full_name,
        )
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e

    upsert_oauth_identity(
        user_id=user_id,
        provider="xero",
        provider_subject_id=str(rec.get("provider_subject_id") or ""),
        provider_email=str(rec.get("provider_email") or email),
        provider_tenant_id=rec.get("tenant_id"),
        provider_tenant_name=rec.get("tenant_name"),
    )
    upsert_xero_connection(
        user_id,
        tenant_id=str(rec.get("tenant_id") or ""),
        access_token=str(rec.get("access_token") or ""),
        refresh_token=str(rec.get("refresh_token") or ""),
        token_type=rec.get("token_type"),
        scope=rec.get("scope"),
        expires_at=str(rec.get("expires_at") or ""),
    )

    token = issue_dev_token(user_id=user_id, tenant_id="default")
    return {"status": "signed_in", "user_id": user_id, "token": token, "redirect": "/dashboard/upload"}


@app.get("/api/v1/billing/me")
async def billing_me(user: CurrentUser = Depends(_current_user)) -> dict:
    return {
        "plans": PLAN_CATALOG,
        "account": account_snapshot(user.user_id),
        "auth": {"user_id": user.user_id, "tenant_id": user.tenant_id, "source": user.auth_source},
    }


@app.get("/api/v1/auth/me")
async def auth_me(user: CurrentUser = Depends(_current_user)) -> dict:
    return {"user_id": user.user_id, "tenant_id": user.tenant_id, "source": user.auth_source}


@app.post("/api/v1/billing/plan")
async def billing_set_plan(
    plan_id: str = Query(...),
    user: CurrentUser = Depends(_current_user),
) -> dict:
    try:
        set_plan(user.user_id, plan_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"account": account_snapshot(user.user_id)}


@app.post("/api/v1/billing/topup")
async def billing_topup(
    packs: int = Query(1, ge=1, le=20),
    user: CurrentUser = Depends(_current_user),
) -> dict:
    # Top-up only allowed for paid plans (starter/pro)
    acc = account_snapshot(user.user_id)
    if acc.get("plan_id") not in {"starter_14_95", "pro_29_95"}:
        raise HTTPException(status_code=403, detail="topup_requires_paid_plan")

    st = add_topup(user.user_id, packs=packs)
    return {
        "ok": True,
        "packs": packs,
        "pack_size": TOPUP_PACK_SIZE,
        "pack_price_aud": TOPUP_PACK_PRICE_AUD,
        "added_invoices": packs * TOPUP_PACK_SIZE,
        "account": account_snapshot(st.user_id),
    }


@app.post("/api/v1/compliance/abn-lookup")
async def compliance_abn_lookup(name: str = Query(..., min_length=2)) -> dict:
    """按公司名实时查询 ABR 候选（用于 Step A 补全 ABN）。"""
    result = await abr_lookup_by_name(name)
    return result


@app.get("/api/v1/xero/connect")
async def xero_connect(user: CurrentUser = Depends(_current_user)) -> dict:
    try:
        out = build_connect_url(user_id=user.user_id)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return out


@app.get("/callback")
async def xero_callback(code: Optional[str] = None, state: Optional[str] = None) -> dict:
    if not code or not state:
        raise HTTPException(status_code=400, detail="xero_callback_missing_code_or_state")
    try:
        out = await exchange_code(code=code, state=state)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, **out}


@app.post("/api/v1/xero/token/exchange")
async def xero_token_exchange(
    code: str = Query(..., min_length=1),
    state: str = Query(..., min_length=1),
    user: CurrentUser = Depends(_current_user),
) -> dict:
    try:
        out = await exchange_code(code=code, state=state)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if out.get("user_id") != user.user_id:
        raise HTTPException(status_code=403, detail="xero_state_user_mismatch")
    return {"ok": True, **out}


@app.get("/api/v1/xero/connection")
async def xero_connection(user: CurrentUser = Depends(_current_user)) -> dict:
    # UI bootstrap endpoint for Xero status card:
    # - connected flag
    # - active tenant
    # - selectable tenant list (when available)
    # This is used before rendering "Upload to Xero" actions.
    return await get_connection_status_live(user_id=user.user_id)


@app.post("/api/v1/xero/disconnect")
async def xero_disconnect(user: CurrentUser = Depends(_current_user)) -> dict:
    # Explicit account reset for "Switch Xero account" UX:
    # frontend should call this before starting a new /xero/connect flow.
    delete_xero_connection(user.user_id)
    return {"ok": True, "connected": False}


@app.post("/api/v1/xero/switch-tenant")
async def xero_switch_tenant(
    tenant_id: str = Query(..., min_length=1),
    user: CurrentUser = Depends(_current_user),
) -> dict:
    # Company switch within the SAME Xero login.
    # This avoids re-auth when user only wants a different tenant/org.
    try:
        out = set_active_tenant(user_id=user.user_id, tenant_id=tenant_id)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, **out}


@app.post("/api/v1/xero/drafts")
async def xero_create_draft(
    target: Literal["xero", "myob"] = Query("xero"),
    flow_mode: Literal["auto", "ap", "ar"] = Query("auto"),
    file: UploadFile = File(...),
    user: CurrentUser = Depends(_current_user),
) -> dict:
    if target != "xero":
        raise HTTPException(status_code=400, detail="target_must_be_xero")

    mapped = await _extract_and_map_core(target="xero", flow_mode=flow_mode, file=file)

    # Autofill missing required Xero fields so "one-click upload" succeeds
    # for common invoices without manual data entry.
    draft_payload = _autofill_xero_draft_payload(mapped.draft_payload)
    xero_ready, missing_required_fields, _ = validate_xero_draft_payload(draft_payload)
    if not xero_ready:
        raise HTTPException(status_code=400, detail={
            "error": "xero_payload_not_ready",
            "missing_required_fields": missing_required_fields,
            "validation_warnings": mapped.validation_warnings,
        })
    try:
        xero_resp = await create_draft_invoice(user_id=user.user_id, draft_payload=draft_payload)
    except RuntimeError as e:
        msg = str(e)
        if msg.startswith("xero_not_connected"):
            # Frontend behavior contract:
            # 1) show modal "Authorize with Xero"
            # 2) open detail.connect.url in popup/new tab
            # 3) after callback success, retry same upload automatically
            try:
                connect = build_connect_url(user_id=user.user_id)
            except Exception:
                connect = None
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "xero_auth_required",
                    "message": "Xero authorization required before upload",
                    "connect": connect,
                },
            ) from e
        if msg.startswith("xero_token_refresh_failed"):
            # Session expired and refresh failed -> force full re-auth.
            try:
                connect = build_connect_url(user_id=user.user_id)
            except Exception:
                connect = None
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "xero_reauth_required",
                    "message": "Xero session expired, please re-authorize",
                    "connect": connect,
                },
            ) from e
        raise HTTPException(status_code=400, detail=msg) from e
    return {
        "ok": True,
        "target": "xero",
        "xero": xero_resp,
        "invoice": mapped.invoice.model_dump(),
    }


@app.post("/api/v1/jobs")
async def jobs_create(
    file_count: int = Query(0, ge=0),
    invoice_estimated: int = Query(0, ge=0),
    user: CurrentUser = Depends(_current_user),
) -> dict:
    if invoice_estimated > settings.max_invoices_per_job:
        raise HTTPException(
            status_code=400,
            detail=(
                f"invoice_count_exceeded: estimated={invoice_estimated}, "
                f"limit={settings.max_invoices_per_job}."
            ),
        )
    job = create_job(
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        file_count=file_count,
        invoice_estimated=invoice_estimated,
    )
    queue_backend.enqueue(
        QueueMessage(
            job_id=job["job_id"],
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            payload={"target": "xero", "flow_mode": "auto", "source": "jobs_create"},
        )
    )
    return {"job": job}


@app.post("/api/v1/jobs/{job_id}/submit")
async def jobs_submit(
    job_id: str,
    target: Literal["xero", "myob"] = Query("xero"),
    flow_mode: Literal["auto", "ap", "ar"] = Query("auto"),
    file: UploadFile = File(...),
    user: CurrentUser = Depends(_current_user),
) -> dict:
    owned = get_job_owned(tenant_id=user.tenant_id, user_id=user.user_id, job_id=job_id)
    if not owned:
        raise HTTPException(status_code=404, detail="job_not_found")

    filename = file.filename or "upload"
    content_type = (file.content_type or "").lower()
    file_bytes = await file.read()

    _validate_upload(file_bytes, filename, content_type)
    estimated = _estimate_invoices_from_upload(filename, content_type, file_bytes)
    if estimated > settings.max_invoices_per_job:
        _raise_invoice_limit_exceeded(estimated, max_invoices=settings.max_invoices_per_job)

    storage = StorageBackend()
    stored = storage.put_bytes(
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        job_id=job_id,
        filename=filename,
        content=file_bytes,
    )

    # persist estimated count metadata on the job row for observability
    ok = update_job_submission_owned(
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        job_id=job_id,
        file_count=1,
        invoice_estimated=int(estimated),
        metadata={
            "artifact_ref": stored.artifact_ref,
            "artifact_backend": stored.backend,
            "artifact_size_bytes": stored.size_bytes,
            "filename": filename,
            "content_type": content_type,
            "invoice_estimated": estimated,
        },
    )
    if not ok:
        raise HTTPException(status_code=404, detail="job_not_found")

    set_job_status_owned(user.tenant_id, user.user_id, job_id, "queued")
    queue_backend.enqueue(
        QueueMessage(
            job_id=job_id,
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            payload={
                "source": "upload",
                "target": target,
                "flow_mode": flow_mode,
                "filename": filename,
                "content_type": content_type,
                "artifact_ref": stored.artifact_ref,
                "artifact_backend": stored.backend,
                "invoice_estimated": estimated,
            },
        )
    )
    return {
        "job_id": job_id,
        "status": "queued",
        "artifact_backend": stored.backend,
        "artifact_size_bytes": stored.size_bytes,
        "invoice_estimated": estimated,
    }


@app.get("/api/v1/jobs")
async def jobs_list(user: CurrentUser = Depends(_current_user)) -> dict:
    jobs = list_jobs_owned(tenant_id=user.tenant_id, user_id=user.user_id)
    return {"jobs": jobs, "count": len(jobs)}


@app.get("/api/v1/jobs/{job_id}")
async def jobs_get(job_id: str, user: CurrentUser = Depends(_current_user)) -> dict:
    job = get_job_owned(tenant_id=user.tenant_id, user_id=user.user_id, job_id=job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_not_found")
    return {"job": job}


@app.get("/api/v1/jobs/{job_id}/result")
async def jobs_result_get(job_id: str, user: CurrentUser = Depends(_current_user)) -> dict:
    row = get_job_result_owned(tenant_id=user.tenant_id, user_id=user.user_id, job_id=job_id)
    if not row:
        raise HTTPException(status_code=404, detail="job_result_not_found")
    return {"job_result": row}


@app.get("/api/v1/queue/stats")
async def queue_stats(user: CurrentUser = Depends(_current_user)) -> dict:
    # owner token required; current stats is global aggregate for operators
    _ = user
    return {"queue": list_queue_counts()}


@app.post("/api/v1/extract-and-map", response_model=ExtractAndMapResponse)
async def extract_and_map_invoice(
    target: Literal["xero", "myob"] = Query(...),
    flow_mode: Literal["auto", "ap", "ar"] = Query("auto"),
    files: Optional[list[UploadFile]] = File(None),
    file: Optional[UploadFile] = File(None),
) -> ExtractAndMapResponse:
    uploads = _normalize_upload_inputs(files, file)
    return await _extract_and_map_core(target=target, flow_mode=flow_mode, file=uploads[0])


@app.post("/api/v1/extract-and-map/batch", response_model=MultiFileBatchResponse)
async def extract_and_map_invoice_batch(
    target: Literal["xero", "myob"] = Query(...),
    flow_mode: Literal["auto", "ap", "ar"] = Query("auto"),
    files: Optional[list[UploadFile]] = File(None),
    file: Optional[UploadFile] = File(None),
    user: CurrentUser = Depends(_current_user),
) -> MultiFileBatchResponse:
    uploads = _normalize_upload_inputs(files, file)
    if len(uploads) == 1:
        f = uploads[0]
        filename = f.filename or "upload"
        content_type = (f.content_type or "").lower()
        file_bytes = await f.read()
        # batch 入口对超页 PDF 放宽限制，由 split(auto) 处理
        suffix = Path(filename).suffix.lower()
        max_bytes = settings.max_file_mb * 1024 * 1024
        if len(file_bytes) > max_bytes:
            raise HTTPException(status_code=400, detail=f"File exceeds size limit of {settings.max_file_mb}MB")
        if suffix != ".pdf" and not content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="Only image files or PDF are supported")
        br = await _extract_and_map_batch_core(
            target=target,
            flow_mode=flow_mode,
            filename=filename,
            content_type=content_type,
            file_bytes=file_bytes,
            user_id=user.user_id,
        )
        return MultiFileBatchResponse(
            file_count=1,
            items=[MultiFileBatchItem(file_name=filename, ok=True, batch_result=br)],
            summary={
                "ok_files": 1,
                "failed_files": 0,
                "total_chunks": br.chunk_count,
                "max_batch_files": settings.max_batch_files,
                "max_batch_total_mb": settings.max_batch_total_mb,
                "account": account_snapshot(user.user_id),
                "auth_user_id": user.user_id,
            },
        )

    # n-file 统一入口（n<20）
    return await extract_and_map_invoice_batch_multi(
        target=target,
        flow_mode=flow_mode,
        files=uploads,
        user=user,
        client_file_count=len(uploads),
    )


@app.post("/api/v1/extract-and-map/batch/multi", response_model=MultiFileBatchResponse)
async def extract_and_map_invoice_batch_multi(
    target: Literal["xero", "myob"] = Query(...),
    flow_mode: Literal["auto", "ap", "ar"] = Query("auto"),
    files: list[UploadFile] = File(...),
    user: CurrentUser = Depends(_current_user),
    job_id: Optional[str] = None,
    client_file_count: Optional[int] = Header(default=None, alias="X-Client-File-Count"),
) -> MultiFileBatchResponse:
    if job_id:
        owned = get_job_owned(tenant_id=user.tenant_id, user_id=user.user_id, job_id=job_id)
        if not owned:
            raise HTTPException(status_code=404, detail="job_not_found")
        set_job_status_owned(user.tenant_id, user.user_id, job_id, "running")

    uploads = _normalize_upload_inputs(files, None)
    # 先读取一次用于“提取前 invoice 数量预估”与后续处理复用
    prepared: list[tuple[UploadFile, str, str, bytes]] = []
    estimated_total = 0
    total_bytes = 0
    for f in uploads:
        filename = f.filename or "upload"
        content_type = (f.content_type or "").lower()
        file_bytes = await f.read()
        prepared.append((f, filename, content_type, file_bytes))

        total_bytes += len(file_bytes)
        if total_bytes > settings.max_batch_total_mb * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"total_size_exceeded: max {settings.max_batch_total_mb}MB")

        estimated_total += _estimate_invoices_from_upload(filename, content_type, file_bytes)
        if estimated_total > settings.max_invoices_per_job:
            _raise_invoice_limit_exceeded(estimated_total, max_invoices=settings.max_invoices_per_job)

    if client_file_count is not None and client_file_count != len(uploads):
        raise HTTPException(
            status_code=400,
            detail=f"count_mismatch: client={client_file_count}, server={len(uploads)}",
        )

    if len(uploads) > settings.max_batch_files:
        raise HTTPException(status_code=400, detail=f"too_many_files: max {settings.max_batch_files}")

    items: list[MultiFileBatchItem] = []
    ok = 0
    failed = 0
    total_chunks = 0

    for f, filename, content_type, file_bytes in prepared:

        try:
            # 单文件大小限制与类型限制
            suffix = Path(filename).suffix.lower()
            if len(file_bytes) > settings.max_file_mb * 1024 * 1024:
                raise ValueError(f"file exceeds {settings.max_file_mb}MB")
            if suffix != ".pdf" and not content_type.startswith("image/"):
                raise ValueError("Only image files or PDF are supported")

            br = await _extract_and_map_batch_core(
                target=target,
                flow_mode=flow_mode,
                filename=filename,
                content_type=content_type,
                file_bytes=file_bytes,
                user_id=user.user_id,
            )
            # 二次硬校验：按真实 split chunk 数累加，超过 20 立即终止
            total_chunks += br.chunk_count
            if total_chunks > settings.max_invoices_per_job:
                _raise_invoice_limit_exceeded(total_chunks, max_invoices=settings.max_invoices_per_job)
            items.append(MultiFileBatchItem(file_name=filename, ok=True, batch_result=br))
            ok += 1
        except Exception as e:  # noqa: BLE001
            items.append(MultiFileBatchItem(file_name=filename, ok=False, error=str(e)))
            failed += 1

    result = MultiFileBatchResponse(
        file_count=len(uploads),
        items=items,
        summary={
            "ok_files": ok,
            "failed_files": failed,
            "total_chunks": total_chunks,
            "max_batch_files": settings.max_batch_files,
            "max_batch_total_mb": settings.max_batch_total_mb,
            "account": account_snapshot(user.user_id),
            "auth_user_id": user.user_id,
        },
    )
    if job_id:
        upsert_job_result_owned(
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            job_id=job_id,
            status="completed",
            result=result.model_dump(),
        )
        set_job_status_owned(user.tenant_id, user.user_id, job_id, "completed")
    return result


@app.post("/api/v1/extract-and-map/batch/multi/export.xlsx")
async def extract_and_map_invoice_batch_multi_export_xlsx(
    target: Literal["xero", "myob"] = Query(...),
    flow_mode: Literal["auto", "ap", "ar"] = Query("auto"),
    files: list[UploadFile] = File(...),
    user: CurrentUser = Depends(_current_user),
    client_file_count: Optional[int] = Header(default=None, alias="X-Client-File-Count"),
) -> Response:
    batch_result = await extract_and_map_invoice_batch_multi(
        target=target,
        flow_mode=flow_mode,
        files=files,
        user=user,
        client_file_count=client_file_count,
    )
    xlsx_bytes = build_multifile_extraction_workbook(batch_result)
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": "attachment; filename=batch-extraction.xlsx",
            "X-Received-Files": str(batch_result.file_count),
        },
    )


@app.post("/api/v1/extract-and-map/export.xlsx")
async def extract_and_map_export_xlsx(
    target: Literal["xero", "myob"] = Query(...),
    flow_mode: Literal["auto", "ap", "ar"] = Query("auto"),
    files: Optional[list[UploadFile]] = File(None),
    file: Optional[UploadFile] = File(None),
    user: CurrentUser = Depends(_current_user),
    client_file_count: Optional[int] = Header(default=None, alias="X-Client-File-Count"),
) -> Response:
    uploads = _normalize_upload_inputs(files, file)
    # 统一成 n-file 导出 schema，避免单/多格式分叉
    batch_result = await extract_and_map_invoice_batch_multi(
        target=target,
        flow_mode=flow_mode,
        files=uploads,
        user=user,
        client_file_count=client_file_count if client_file_count is not None else len(uploads),
    )
    xlsx_bytes = build_multifile_extraction_workbook(batch_result)
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": "attachment; filename=batch-extraction.xlsx",
            "X-Received-Files": str(batch_result.file_count),
        },
    )


@app.post("/api/v1/extract-and-map/unified/export.xlsx")
async def extract_and_map_unified_export_xlsx(
    target: Literal["xero", "myob"] = Query(...),
    flow_mode: Literal["auto", "ap", "ar"] = Query("auto"),
    files: list[UploadFile] = File(...),
    user: CurrentUser = Depends(_current_user),
    client_file_count: Optional[int] = Header(default=None, alias="X-Client-File-Count"),
) -> Response:
    """统一导出入口：无论单张/多张都走同一张 summary 表。"""
    if len(files) == 0:
        raise HTTPException(status_code=400, detail="no files uploaded")

    batch_result = await extract_and_map_invoice_batch_multi(
        target=target,
        flow_mode=flow_mode,
        files=files,
        user=user,
        client_file_count=client_file_count,
    )
    xlsx_bytes = build_multifile_extraction_workbook(batch_result)
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": "attachment; filename=batch-extraction.xlsx",
            "X-Received-Files": str(batch_result.file_count),
        },
    )
