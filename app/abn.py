from __future__ import annotations

import json
import os
import re
from typing import Any, Optional
from urllib.parse import quote_plus

import httpx


# ABN = 11 digits, checksum per ABR rule
_WEIGHTS = [10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19]


def normalize_abn(text: Optional[str]) -> Optional[str]:
    """提取并规范化 ABN（仅保留数字）。"""
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    return digits if len(digits) == 11 else None


def validate_abn_checksum(abn: Optional[str]) -> bool:
    """ABN checksum 校验。"""
    if not abn or len(abn) != 11 or not abn.isdigit():
        return False
    digits = [int(c) for c in abn]
    digits[0] -= 1
    weighted_sum = sum(d * w for d, w in zip(digits, _WEIGHTS))
    return weighted_sum % 89 == 0


def company_name_matches(vendor_name: str, abr_name: str) -> bool:
    """公司名弱匹配：用于判断供应商名与 ABR 实体名是否大致一致。"""
    if not vendor_name or not abr_name:
        return False

    def _tokens(s: str) -> set[str]:
        s = re.sub(r"[^a-zA-Z0-9 ]", " ", s.lower())
        toks = [t for t in s.split() if len(t) >= 3 and t not in {"pty", "ltd", "limited", "australia", "australian"}]
        return set(toks)

    a = _tokens(vendor_name)
    b = _tokens(abr_name)
    if not a or not b:
        return vendor_name.strip().lower() == abr_name.strip().lower()
    overlap = len(a & b)
    return overlap >= 1 and overlap / max(1, len(a)) >= 0.34


def _parse_jsonp(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if raw.startswith("{"):
        return json.loads(raw)
    m = re.search(r"\((\{.*\})\)\s*;?$", raw, flags=re.S)
    if not m:
        raise ValueError("invalid ABR JSONP response")
    return json.loads(m.group(1))


async def _abr_get_json(url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=8) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        text = resp.text
    return _parse_jsonp(text)


async def abr_lookup_by_abn(abn: str) -> dict[str, Any]:
    """实时 ABR 查询（按 ABN）。需要 ABR_GUID 环境变量。"""
    guid = os.getenv("ABR_GUID", "").strip()
    if not guid:
        return {"available": False, "reason": "ABR_GUID not configured"}

    url = (
        "https://abr.business.gov.au/json/AbnDetails.aspx?"
        f"abn={quote_plus(abn)}&callback=cb&authenticationGuid={quote_plus(guid)}"
    )
    try:
        data = await _abr_get_json(url)
        return {
            "available": True,
            "method": "abn",
            "abn": normalize_abn(str(data.get("Abn", ""))),
            "entity_name": data.get("EntityName") or data.get("MainName") or "",
            "abn_status": data.get("AbnStatus") or "",
            "gst_registered": bool(data.get("Gst") or data.get("GstRegistered") or False),
            "raw": data,
        }
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": f"ABR lookup failed: {e}"}


async def abr_lookup_by_name(name: str) -> dict[str, Any]:
    """实时 ABR 查询（按公司名，返回首个候选）。需要 ABR_GUID 环境变量。"""
    guid = os.getenv("ABR_GUID", "").strip()
    if not guid:
        return {"available": False, "reason": "ABR_GUID not configured"}

    url = (
        "https://abr.business.gov.au/json/MatchingNames.aspx?"
        f"name={quote_plus(name)}&maxResults=5&callback=cb&authenticationGuid={quote_plus(guid)}"
    )
    try:
        data = await _abr_get_json(url)
        names = data.get("Names") or data.get("names") or []
        if not names:
            return {"available": True, "method": "name", "matched": False, "reason": "no ABR candidates"}
        c = names[0]
        candidate_abn = normalize_abn(str(c.get("Abn") or c.get("abn") or ""))
        candidate_name = str(c.get("Name") or c.get("name") or "")
        return {
            "available": True,
            "method": "name",
            "matched": bool(candidate_abn),
            "abn": candidate_abn,
            "entity_name": candidate_name,
            "raw": data,
        }
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": f"ABR name lookup failed: {e}"}


async def lookup_abn_context(abn_raw: Optional[str], vendor_name: str) -> dict[str, Any]:
    """统一 ABR 查询：优先 ABN，其次 vendor_name 反查。"""
    abn = normalize_abn(abn_raw)
    if abn:
        return await abr_lookup_by_abn(abn)
    if vendor_name.strip():
        return await abr_lookup_by_name(vendor_name.strip())
    return {"available": False, "reason": "missing abn and vendor_name"}


def compliance_warnings_for_abn(
    abn_raw: Optional[str],
    gst_present: Optional[bool],
    total: Optional[float] = None,
    is_tax_invoice_like: Optional[bool] = None,
    abr_context: Optional[dict[str, Any]] = None,
    vendor_name: str = "",
) -> list[str]:
    """合规提醒：本地 ABN + 可选 ABR 实时状态。"""
    warnings: list[str] = []
    abn = normalize_abn(abn_raw)

    if not abn:
        warnings.append("compliance: ABN missing or invalid length (11 digits required)")
        if total is not None and total > 75:
            warnings.append("compliance: no valid ABN for amount > 75 AUD; No ABN withholding rule may apply (47%)")
        if gst_present:
            warnings.append("compliance: GST amount present but ABN missing/invalid; manual review required")
    elif not validate_abn_checksum(abn):
        warnings.append("compliance: ABN checksum failed; verify supplier ABN before posting")
        if gst_present:
            warnings.append("compliance: GST claimed while ABN checksum failed; tax code should be reviewed")

    if is_tax_invoice_like is False:
        warnings.append("compliance: document appears to be quote/proforma/statement (not final tax invoice)")

    if abr_context and abr_context.get("available"):
        entity_name = str(abr_context.get("entity_name") or "")
        if vendor_name and entity_name and not company_name_matches(vendor_name, entity_name):
            warnings.append(
                f"compliance: supplier name mismatch with ABR record ('{vendor_name}' vs '{entity_name}')"
            )
        gst_registered = abr_context.get("gst_registered")
        if gst_registered is False and gst_present:
            warnings.append("compliance: GST present on document but ABR indicates supplier is not GST-registered")

    return warnings
