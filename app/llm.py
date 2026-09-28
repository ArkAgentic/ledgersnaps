import base64
import json
import os
from io import BytesIO

import httpx
from pypdf import PdfReader

from .schemas import InvoiceData, InvoiceLineItem, UsageCost


SYSTEM_PROMPT = """
You are an invoice extraction engine.
Return ONLY valid JSON with keys:
vendor_name, abn, invoice_num, supplier_invoice_number, date, due_date, reference,
subtotal, gst, total, currency, line_items.
line_items is an array of objects: description, quantity, unit_price, amount, tax_amount,
account_code_hint, tax_code_hint.
If unknown, use null for optional fields and empty string for vendor_name.
No markdown, no prose.
""".strip()


def _azure_openai_base_url(endpoint: str) -> str:
    if endpoint.endswith("/openai/v1"):
        return endpoint
    return endpoint.rstrip("/") + "/openai/v1"


def _response_text(payload: dict) -> str:
    if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
        return payload["output_text"].strip()
    for item in payload.get("output", []):
        for c in item.get("content", []):
            if c.get("type") in {"output_text", "text"} and c.get("text"):
                return c["text"].strip()
    raise RuntimeError("No text content in model response")


def _parse_invoice_json(text: str) -> InvoiceData:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.replace("json", "", 1).strip()
    data = json.loads(text)
    line_items = [InvoiceLineItem(**item) for item in data.get("line_items", [])]
    return InvoiceData(
        vendor_name=data.get("vendor_name", "") or "",
        abn=data.get("abn"),
        invoice_num=data.get("invoice_num"),
        supplier_invoice_number=data.get("supplier_invoice_number"),
        date=data.get("date"),
        due_date=data.get("due_date"),
        reference=data.get("reference"),
        subtotal=data.get("subtotal"),
        gst=data.get("gst"),
        total=data.get("total"),
        currency=data.get("currency", "AUD") or "AUD",
        line_items=line_items,
    )


def _pdf_first_page_text(file_bytes: bytes) -> str:
    reader = PdfReader(BytesIO(file_bytes))
    if len(reader.pages) == 0:
        return ""
    return (reader.pages[0].extract_text() or "").strip()


def _estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    # User-specified pricing: $2.5 / 1M input, $10 / 1M output
    return round((input_tokens / 1_000_000) * 2.5 + (output_tokens / 1_000_000) * 10.0, 6)


def _usage_from_response(payload: dict) -> UsageCost:
    usage = payload.get("usage", {}) if isinstance(payload, dict) else {}
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    total_tokens = int(usage.get("total_tokens", input_tokens + output_tokens) or (input_tokens + output_tokens))
    return UsageCost(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        estimated_cost_usd=_estimate_cost_usd(input_tokens, output_tokens),
    )


async def parse_invoice_with_azure_openai(file_bytes: bytes, filename: str, content_type: str) -> tuple[InvoiceData, UsageCost]:
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip()
    api_key = os.getenv("AZURE_OPENAI_API_KEY", "").strip()
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o").strip()

    if not endpoint or not api_key or not deployment:
        raise RuntimeError("Missing Azure OpenAI configuration")

    base_url = _azure_openai_base_url(endpoint)
    url = f"{base_url}/responses"
    headers = {"api-key": api_key, "Content-Type": "application/json"}

    if content_type.startswith("image/"):
        b64 = base64.b64encode(file_bytes).decode("utf-8")
        data_url = f"data:{content_type};base64,{b64}"
        user_input = [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Extract invoice fields from this image."},
                    {"type": "input_image", "image_url": data_url},
                ],
            }
        ]
    else:
        page_text = _pdf_first_page_text(file_bytes)
        user_input = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Extract invoice fields from this single-page PDF text:\n\n" + page_text,
                    }
                ],
            }
        ]

    payload = {
        "model": deployment,
        "input": user_input,
        "instructions": SYSTEM_PROMPT,
        "temperature": 0,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(f"Azure OpenAI parse failed: {resp.status_code} {resp.text[:300]}")
        data = resp.json()

    text = _response_text(data)
    usage_cost = _usage_from_response(data)
    try:
        return _parse_invoice_json(text), usage_cost
    except Exception as e:
        raise RuntimeError(f"Failed to parse model JSON: {e}") from e


