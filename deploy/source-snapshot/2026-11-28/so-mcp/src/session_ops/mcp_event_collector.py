"""MCP-side polling bridge from the Sender Gateway outbox into MCP Events."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any


class MCPEventCollector:
    """Poll sender outbox, persist locally, then acknowledge the sender lease.

    The sender client is intentionally duck-typed so the collector can be
    tested independently from TLS setup. Production supplies the existing
    SenderGatewayClient with claim_events() and ack_event() methods.
    """

    def __init__(
        self,
        sender_client: Any,
        events_store: Any,
        *,
        worker_id: str,
        batch_size: int = 2,
    ) -> None:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id is required")
        # Sender leases are deliberately short. Keep each claim small enough
        # that later items cannot expire behind slow earlier ingestions.
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 2:
            raise ValueError("batch_size must be in 1..2")
        self.sender_client = sender_client
        self.events_store = events_store
        self.worker_id = worker_id
        self.batch_size = batch_size

    def run_once(self) -> int:
        response = self.sender_client.claim_events(
            worker_id=self.worker_id,
            limit=self.batch_size,
        )
        if not isinstance(response, Mapping) or not isinstance(response.get("items"), list):
            raise ValueError("Sender Gateway returned an invalid event batch")

        acknowledged = 0
        for item in response["items"]:
            event_id = item.get("event_id") if isinstance(item, Mapping) else None
            try:
                if not isinstance(item, Mapping):
                    raise ValueError("Sender Gateway returned a malformed event")
                event_id = item.get("event_id")
                job_id = item.get("job_id")
                tenant_id = item.get("tenant_id")
                principal_id = item.get("principal_id")
                lease_token = item.get("lease_token")
                payload = item.get("payload")
                if item.get("name") != "chatgpt.job.completed" or not isinstance(payload, Mapping):
                    raise ValueError("Sender Gateway returned an unsupported event")
                if payload.get("job_id") != job_id or payload.get("status") not in ("succeeded", "failed"):
                    raise ValueError("Sender Gateway event owner or terminal state does not match its payload")
                summary = payload.get("summary")
                completed_at = payload.get("completed_at")
                if not all(isinstance(value, str) and value for value in (event_id, job_id, tenant_id, principal_id, lease_token, summary, completed_at)):
                    raise ValueError("Sender Gateway event is missing required fields")

                # Persist before ack. A lost ack is safe because event_id is stable.
                self.events_store.publish_job_completed(
                    tenant_id,
                    principal_id,
                    job_id,
                    summary,
                    event_id=event_id,
                    completed_at=completed_at,
                    status=payload["status"],
                    error_code=payload.get("error_code"),
                    outcome_unknown=payload.get("outcome_unknown"),
                )
                result = self.sender_client.ack_event(event_id=event_id, lease_token=lease_token)
                if not isinstance(result, Mapping) or result.get("acknowledged") is not True:
                    raise RuntimeError("Sender Gateway did not acknowledge the event lease")
                acknowledged += 1
            except Exception:
                # Isolate poison/failed rows. Never ack an event that was not
                # durably ingested, but do not starve unrelated events in batch.
                logging.exception("MCP Sender event ingestion failed (event_id=%s)", event_id)
                continue
        return acknowledged
