from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class InvoiceLineItem(BaseModel):
    description: str = ""
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    amount: Optional[float] = None
    tax_amount: Optional[float] = None
    account_code_hint: Optional[str] = None
    tax_code_hint: Optional[str] = None


class InvoiceData(BaseModel):
    vendor_name: str = ""
    abn: Optional[str] = None
    invoice_num: Optional[str] = None
    supplier_invoice_number: Optional[str] = None
    date: Optional[str] = None
    due_date: Optional[str] = None
    reference: Optional[str] = None
    subtotal: Optional[float] = None
    gst: Optional[float] = None
    total: Optional[float] = None
    currency: str = Field(default="AUD")
    line_items: List[InvoiceLineItem] = Field(default_factory=list)
    document_flow: Literal["ap", "ar", "unknown"] = "unknown"
    flow_confidence: float = 0.0
    flow_reasons: List[str] = Field(default_factory=list)
    flow_overridden_by_user: bool = False
    gst_extracted: Optional[float] = None
    gst_inferred: Optional[float] = None
    gst_source: Literal["extracted", "inferred", "none"] = "none"
    gst_confidence: float = 0.0


class UsageCost(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0


class ExtractionMeta(BaseModel):
    method: Literal["rules", "llm"]
    usage: Optional[UsageCost] = None
    warnings: List[str] = Field(default_factory=list)
    trace: List[str] = Field(default_factory=list)


class ExtractResponse(BaseModel):
    invoice: InvoiceData
    model: str
    meta: ExtractionMeta


class XeroLineItem(BaseModel):
    Description: str
    Quantity: Optional[float] = None
    UnitAmount: Optional[float] = None
    LineAmount: Optional[float] = None
    AccountCode: Optional[str] = None
    TaxType: Optional[str] = None


class XeroDraftBillPayload(BaseModel):
    Type: Literal["ACCPAY", "ACCREC"] = "ACCPAY"
    Contact: dict
    Date: Optional[str] = None
    DueDate: Optional[str] = None
    InvoiceNumber: Optional[str] = None
    Reference: Optional[str] = None
    Status: Literal["DRAFT"] = "DRAFT"
    CurrencyCode: str = "AUD"
    LineItems: List[XeroLineItem] = Field(default_factory=list)


class MyobBillLine(BaseModel):
    Description: str
    Total: Optional[float] = None
    UnitCount: Optional[float] = None
    UnitPrice: Optional[float] = None


class MyobDraftBillPayload(BaseModel):
    SupplierName: str
    SupplierInvoiceNumber: Optional[str] = None
    Date: Optional[str] = None
    DueDate: Optional[str] = None
    IsTaxInclusive: bool = False
    Lines: List[MyobBillLine] = Field(default_factory=list)


class ExtractAndMapResponse(BaseModel):
    invoice: InvoiceData
    target: Literal["xero", "myob"]
    draft_payload: dict
    xero_ready: Optional[bool] = None
    missing_required_fields: List[str] = Field(default_factory=list)
    suggested_fixes: List[str] = Field(default_factory=list)
    validation_warnings: List[str] = Field(default_factory=list)
    model: str
    meta: ExtractionMeta


class ExtractAndMapChunkResult(BaseModel):
    chunk_index: int
    result: ExtractAndMapResponse


class BatchExtractAndMapResponse(BaseModel):
    split_decision: Literal["single", "split"]
    chunk_count: int
    chunks: List[ExtractAndMapChunkResult] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)


class MultiFileBatchItem(BaseModel):
    file_name: str
    ok: bool
    batch_result: Optional[BatchExtractAndMapResponse] = None
    error: Optional[str] = None


class MultiFileBatchResponse(BaseModel):
    file_count: int
    items: List[MultiFileBatchItem] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)
