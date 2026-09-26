# LedgerSnaps Backend (Phase 1)

## Local run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Open: `http://127.0.0.1:8000/`

## 模块地图（中文）

- `app/main.py`
  - 主编排入口，负责 L0-L4：上传预检、Step A/Step B 路由、校验、映射输出。
  - 额外提供 `extract-and-map/export.xlsx` 导出接口。
- `app/rules.py`
  - Step A 低成本规则提取：从 PDF 文本层提取关键字段并做质量门判断。
- `app/llm.py`
  - Step B 大模型提取：规则不足时调用 gpt-4o，并返回 token/cost 统计。
- `app/flow.py`
  - AP/AR/unknown 自动分类与理由输出，支持用户覆盖。
- `app/abn.py`
  - ABN 本地校验 + ABR 实时查询（按 ABN / 按公司名）。
- `app/exporter.py`
  - 导出 Excel 结果文件（summary/line_items/warnings/trace/draft_payload）。
- `app/xero_mapper.py`
  - 映射为 Xero Draft（当前 AP/ACCPAY 形态）。
- `app/myob_mapper.py`
  - 映射为 MYOB Draft Bill 形态。
- `app/schemas.py`
  - 统一输入输出模型（invoice/meta/warnings/flow/usage/trace）。

## L0-L5 当前实现状态

- L0 上传预检：已实现（格式/大小/页数）
- L1 Step A 规则提取：已实现（含质量门）
- L2 Step B LLM 提取：已实现（按需升级）
- L3 校验：已实现（必填、金额一致性、ABN/GST 本地+ABR实时提醒）
- L4 映射：已实现（xero/myob），支持 flow_mode 覆盖
- L5 学习闭环：未实现（后续接用户修正日志）

## API 清单

- `POST /api/v1/extract`
- `POST /api/v1/extract-and-map?target=xero|myob&flow_mode=auto|ap|ar`
- `POST /api/v1/extract-and-map/export.xlsx?target=xero|myob&flow_mode=auto|ap|ar`
- `POST /api/v1/compliance/abn-lookup?name=...`

## Xero Demo 样本导出（立刻可执行）

1) 登录 Xero 后切到 **Demo Company**（My Xero 里可进入，28天会自动重置）。
2) 在 Demo Company 里准备两类样本：
   - **AP Bills**：Business → Bills to pay（Draft / Awaiting payment）
   - **AR Invoices**：Business → Invoices（Draft / Awaiting payment）
3) 先用列表页 **Export (CSV)** 导出结构化字段（做 ground truth 对照）。
4) 再逐张打开单据，下载 PDF（用于视觉/版式提取测试）。
5) 把 PDF 放到本地目录（例如 `tests/fixtures/real_invoices/`），并在 `tests/fixtures/manifest.jsonl` 填 expected 字段。

参考：Xero Central 的 Demo Company 说明（Use the demo company）。

## Test Harness（把现有 rules/llm 流程变成可量化回归）

你说得对：harness 不是替代 rules，而是**批量调用现有 `/extract-and-map` 流程**做回归评分。

### 目录

- `scripts/eval_invoices.py`
- `tests/fixtures/manifest.template.jsonl`
- `tests/fixtures/manifest.jsonl`（你按模板复制生成）

### 运行

```bash
cd /Users/charleszhang/Documents/GitHub/ledgersnaps
source .venv/bin/activate

# 复制模板并填入你导出的 PDF 绝对路径与 expected 字段
cp tests/fixtures/manifest.template.jsonl tests/fixtures/manifest.jsonl

# 跑评估（真实链路）
python scripts/eval_invoices.py \
  --manifest tests/fixtures/manifest.jsonl \
  --target xero \
  --flow-mode auto \
  --out .hermes/reports/invoice-eval-report.json
```

输出包含：
- `method_counts`（rules/llm 走了多少）
- `avg_cost_usd`
- 字段级准确率（vendor_name/abn/date/total/gst/document_flow 等）

## Test your sample invoice files

```bash
curl -sS -X POST "http://127.0.0.1:8000/api/v1/extract?mock=true" \
  -F "file=@/Users/charleszhang/Desktop/LedgerSnaps/Testing\ Invoices/INV-25146\ -\ Brightstone\ Legal\ Invoice.pdf"

curl -sS -X POST "http://127.0.0.1:8000/api/v1/extract-and-map?target=xero&flow_mode=auto&mock=true" \
  -F "file=@/Users/charleszhang/Desktop/LedgerSnaps/Testing\ Invoices/A-8F116502-origin-statement-2025-10-27.pdf"

curl -sS -X POST "http://127.0.0.1:8000/api/v1/extract-and-map/export.xlsx?target=xero&flow_mode=auto&mock=true" \
  -F "file=@/Users/charleszhang/Desktop/LedgerSnaps/Testing\ Invoices/INV-25146\ -\ Brightstone\ Legal\ Invoice.pdf" \
  -o /Users/charleszhang/Desktop/LedgerSnaps/Testing\ Invoices/INV-25146-extraction.xlsx
```

Then switch `mock=false` in playground or query string for real model extraction.

## Tests

```bash
source .venv/bin/activate
pytest -q
```

## Environment variables

建议用本地 `.env`：

```bash
cd /Users/charleszhang/Documents/GitHub/ledgersnaps
cp .env.example .env
# 然后编辑 .env 填入真实 key
set -a
source .env
set +a
```

变量说明：
- `AZURE_OPENAI_ENDPOINT`（`https://arkagentic.openai.azure.com/openai/v1`）
- `AZURE_OPENAI_API_KEY`
- `AZURE_OPENAI_DEPLOYMENT`（`gpt-4o`）
- `ABR_GUID`（可选；ABR 实时查询用）

`AZURE_OPENAI_API_VERSION` 在 `/openai/v1` 模式下不需要。
