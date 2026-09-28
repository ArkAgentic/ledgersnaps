from __future__ import annotations

import argparse
import json
import os
import time

from app.config import settings
from app.queue_backend import QueueBackend
from app.worker import process_one_queued_job


def main() -> None:
    p = argparse.ArgumentParser(description="LedgerSnaps local queue worker")
    p.add_argument("--once", action="store_true", help="process at most one queued job and exit")
    p.add_argument("--interval", type=float, default=1.0, help="poll interval seconds")
    args = p.parse_args()

    backend = QueueBackend(os.getenv("QUEUE_BACKEND", settings.queue_backend))

    def run_once():
        if backend.kind == "servicebus":
            return backend.consume_once()
        return process_one_queued_job()

    if args.once:
        out = run_once()
        print(json.dumps({"processed": bool(out), "result": out}, ensure_ascii=False))
        return

    while True:
        out = run_once()
        if out:
            print(json.dumps({"processed": True, "result": out}, ensure_ascii=False), flush=True)
        time.sleep(max(0.1, args.interval))


if __name__ == "__main__":
    main()
