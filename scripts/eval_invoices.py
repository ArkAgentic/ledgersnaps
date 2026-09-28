#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app.main import app


def _norm_text(v: Any) -> str:
    return str(v or "").strip().lower()


def _num_equal(a: Any, b: Any, tol: float = 0.05) -> bool:
    try:
        return math.isclose(float(a), float(b), abs_tol=tol)
    except Exception:
        return False


def _field_match(field: str, expected: Any, got: dict[str, Any]) -> bool:
    actual = got.get(field)
    if field in {"total", "gst", "subtotal", "flow_confidence", "gst_extracted", "gst_inferred", "gst_confidence"}:
        return _num_equal(actual, expected)
    return _norm_text(actual) == _norm_text(expected)


def _load_manifest(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"manifest parse error line {i}: {e}") from e
            if "file" not in row:
                raise ValueError(f"manifest line {i} missing required key: file")
            rows.append(row)
    return rows


def _record_checks(expected: dict[str, Any], invoice: dict[str, Any], checks: dict[str, Any], field_total: dict[str, int], field_hit: dict[str, int]) -> None:
    for field, exp in expected.items():
        hit = _field_match(field, exp, invoice)
        checks[field] = {"expected": exp, "actual": invoice.get(field), "pass": hit}
        field_total[field] = field_total.get(field, 0) + 1
        if hit:
            field_hit[field] = field_hit.get(field, 0) + 1


def run_eval(manifest_path: Path, target: str, flow_mode: str, batch: bool) -> dict[str, Any]:
    client = TestClient(app)
    rows = _load_manifest(manifest_path)

    method_counts: dict[str, int] = {}
    field_total: dict[str, int] = {}
    field_hit: dict[str, int] = {}
    costs: list[float] = []
    results: list[dict[str, Any]] = []

    endpoint = "/api/v1/extract-and-map/batch" if batch else "/api/v1/extract-and-map"

    for row in rows:
        fp = Path(row["file"]).expanduser()
        if not fp.exists():
            results.append({"file": str(fp), "status": "missing_file"})
            continue

        ctype = "application/pdf" if fp.suffix.lower() == ".pdf" else "image/jpeg"
        with fp.open("rb") as f:
            resp = client.post(
                f"{endpoint}?target={target}&flow_mode={flow_mode}",
                files={"file": (fp.name, f.read(), ctype)},
            )

        if resp.status_code != 200:
            results.append({"file": str(fp), "status": "http_error", "code": resp.status_code, "detail": resp.text[:200]})
            continue

        body = resp.json()

        if not batch:
            invoice = body.get("invoice", {})
            method = (body.get("meta", {}) or {}).get("method", "unknown")
            cost = (((body.get("meta", {}) or {}).get("usage", {}) or {}).get("estimated_cost_usd", 0.0))
            costs.append(float(cost or 0.0))
            method_counts[method] = method_counts.get(method, 0) + 1

            expected = row.get("expected", {}) or {}
            per_file = {
                "file": str(fp),
                "status": "ok",
                "method": method,
                "cost_usd": cost,
                "checks": {},
            }
            _record_checks(expected, invoice, per_file["checks"], field_total, field_hit)
            results.append(per_file)
            continue

        # batch mode
        expected_chunks = row.get("expected_chunks", [])
        per_file = {
            "file": str(fp),
            "status": "ok",
            "split_decision": body.get("split_decision"),
            "chunk_count": body.get("chunk_count", 0),
            "summary": body.get("summary", {}),
            "chunks": [],
        }

        chunks = body.get("chunks", [])
        for c in chunks:
            idx = c.get("chunk_index")
            r = c.get("result", {})
            invoice = r.get("invoice", {})
            method = (r.get("meta", {}) or {}).get("method", "unknown")
            cost = (((r.get("meta", {}) or {}).get("usage", {}) or {}).get("estimated_cost_usd", 0.0))
            costs.append(float(cost or 0.0))
            method_counts[method] = method_counts.get(method, 0) + 1

            expected = {}
            for ec in expected_chunks:
                if int(ec.get("chunk_index", -1)) == int(idx):
                    expected = ec.get("expected", {}) or {}
                    break

            row_checks: dict[str, Any] = {}
            if expected:
                _record_checks(expected, invoice, row_checks, field_total, field_hit)

            per_file["chunks"].append(
                {
                    "chunk_index": idx,
                    "method": method,
                    "cost_usd": cost,
                    "checks": row_checks,
                }
            )

        results.append(per_file)

    field_accuracy = {
        k: {
            "hit": field_hit.get(k, 0),
            "total": v,
            "accuracy": round((field_hit.get(k, 0) / v), 4) if v else None,
        }
        for k, v in field_total.items()
    }

    summary = {
        "mode": "batch" if batch else "single",
        "files_in_manifest": len(rows),
        "evaluated_files": sum(1 for r in results if r.get("status") == "ok"),
        "method_counts": method_counts,
        "avg_cost_usd": round(sum(costs) / len(costs), 6) if costs else 0.0,
        "field_accuracy": field_accuracy,
    }

    return {"summary": summary, "results": results}


def main() -> None:
    p = argparse.ArgumentParser(description="LedgerSnaps extraction regression harness")
    p.add_argument("--manifest", required=True, help="JSONL manifest path")
    p.add_argument("--target", default="xero", choices=["xero", "myob"])
    p.add_argument("--flow-mode", default="auto", choices=["auto", "ap", "ar"])
    p.add_argument("--batch", action="store_true", help="use /extract-and-map/batch endpoint")
    p.add_argument("--out", default="", help="optional output json file")
    args = p.parse_args()

    report = run_eval(Path(args.manifest), args.target, args.flow_mode, args.batch)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"report saved: {out}")


if __name__ == "__main__":
    main()
