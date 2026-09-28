from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Optional

from .store import enqueue_job_owned


@dataclass
class QueueMessage:
    job_id: str
    tenant_id: str
    user_id: str
    payload: dict[str, Any]


class QueueBackend:
    """Queue abstraction. Keep API contract stable while swapping backend.

    Backends:
    - sqlite (default): local persistence via job_queue table
    - servicebus (phase 2+): Azure Service Bus producer path
    """

    def __init__(self, kind: Optional[str] = None) -> None:
        self.kind = (kind or os.getenv("QUEUE_BACKEND", "sqlite")).strip().lower()

    def enqueue(self, msg: QueueMessage) -> dict[str, Any]:
        if self.kind == "servicebus":
            return self._enqueue_servicebus(msg)
        # default sqlite
        return self._enqueue_sqlite(msg)

    def consume_once(self) -> Optional[dict[str, Any]]:
        if self.kind == "servicebus":
            return self._consume_servicebus_once()
        return None

    def _enqueue_sqlite(self, msg: QueueMessage) -> dict[str, Any]:
        enqueue_job_owned(
            tenant_id=msg.tenant_id,
            user_id=msg.user_id,
            job_id=msg.job_id,
            payload=msg.payload,
        )
        return {"backend": "sqlite", "enqueued": True}

    def _enqueue_servicebus(self, msg: QueueMessage) -> dict[str, Any]:
        """Phase 2 producer stub.

        We intentionally fail fast when service bus is selected but missing creds,
        so operators don't assume jobs are queued when they are not.
        """
        connection = os.getenv("AZURE_SERVICEBUS_CONNECTION_STRING", "").strip()
        queue_name = os.getenv("AZURE_SERVICEBUS_QUEUE_NAME", "").strip()
        if not connection or not queue_name:
            raise RuntimeError("servicebus_not_configured: set AZURE_SERVICEBUS_CONNECTION_STRING and AZURE_SERVICEBUS_QUEUE_NAME")

        # Keep dependency optional for local dev.
        try:
            from azure.servicebus import ServiceBusClient, ServiceBusMessage  # type: ignore
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("servicebus_sdk_missing: pip install azure-servicebus") from e

        payload = {
            "job_id": msg.job_id,
            "tenant_id": msg.tenant_id,
            "user_id": msg.user_id,
            "payload": msg.payload,
        }
        body = json.dumps(payload, ensure_ascii=False)

        with ServiceBusClient.from_connection_string(connection) as client:
            sender = client.get_queue_sender(queue_name=queue_name)
            with sender:
                sender.send_messages(ServiceBusMessage(body))

        return {"backend": "servicebus", "enqueued": True, "queue": queue_name}

    def _consume_servicebus_once(self) -> Optional[dict[str, Any]]:
        connection = os.getenv("AZURE_SERVICEBUS_CONNECTION_STRING", "").strip()
        queue_name = os.getenv("AZURE_SERVICEBUS_QUEUE_NAME", "").strip()
        if not connection or not queue_name:
            raise RuntimeError("servicebus_not_configured: set AZURE_SERVICEBUS_CONNECTION_STRING and AZURE_SERVICEBUS_QUEUE_NAME")

        try:
            from azure.servicebus import ServiceBusClient  # type: ignore
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("servicebus_sdk_missing: pip install azure-servicebus") from e

        with ServiceBusClient.from_connection_string(connection) as client:
            receiver = client.get_queue_receiver(queue_name=queue_name, max_wait_time=2)
            with receiver:
                msgs = receiver.receive_messages(max_message_count=1, max_wait_time=2)
                if not msgs:
                    return None
                msg = msgs[0]
                try:
                    body_text = b"".join([bytes(x) for x in msg.body]).decode("utf-8")
                    envelope = json.loads(body_text)
                    item = {
                        "job_id": envelope["job_id"],
                        "tenant_id": envelope["tenant_id"],
                        "user_id": envelope["user_id"],
                        "payload": envelope.get("payload", {}),
                    }
                    from .worker import process_claimed_item

                    out = process_claimed_item(item, finalize_queue=False)
                    receiver.complete_message(msg)
                    return {"backend": "servicebus", **out}
                except Exception:
                    receiver.dead_letter_message(msg)
                    raise
