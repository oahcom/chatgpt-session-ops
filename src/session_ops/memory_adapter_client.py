"""Client for the Memory Adapter (MA) JSON Lines protocol, contract v0.1.

SO owns WorkItem identity and lifecycle; MA owns memory persistence and
retrieval. This client only speaks the frozen protocol in
``references/memory-adapter-contract-v0.1.md``.

No shell, no retry, no network, no MCP, no third-party dependency.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass
from typing import Any, Sequence

CONTRACT_VERSION = "0.1"

# MA error codes that are part of the frozen contract. A recognized code is
# preserved verbatim; anything else is not trusted as a protocol code.
KNOWN_ERROR_CODES: frozenset[str] = frozenset(
    {
        "INVALID_REQUEST",
        "UNSUPPORTED_VERSION",
        "INVALID_OPERATION",
        "INVALID_ID",
        "INVALID_SCOPE",
        "SCOPE_VIOLATION",
        "INVALID_CONTENT",
        "INVALID_PROVENANCE",
        "STORAGE_ERROR",
        "SERIALIZATION_ERROR",
        "INTERNAL_ERROR",
        "ADAPTER_UNAVAILABLE",
        "ADAPTER_TIMEOUT",
        "ADAPTER_CRASHED",
    }
)

_STDERR_LIMIT = 500


@dataclass(frozen=True)
class MemoryProvenance:
    source_type: str
    source_id: str
    captured_at: str


@dataclass(frozen=True)
class StoredMemory:
    memory_id: str
    work_item_id: str
    session_id: str


@dataclass(frozen=True)
class RetrievedMemory:
    memory_id: str
    work_item_id: str
    session_id: str
    content: str
    provenance: dict[str, Any]
    created_at: str
    score: float


@dataclass(frozen=True)
class RetrieveResult:
    entries: tuple[RetrievedMemory, ...]


class MemoryAdapterError(Exception):
    def __init__(
        self,
        code: str,
        request_id: str | None,
        message: str,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.message = message


def _bounded(text: str | None) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= _STDERR_LIMIT:
        return text
    return text[-_STDERR_LIMIT:]


class MemoryAdapterClient:
    def __init__(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.command = tuple(command)
        self.timeout_seconds = timeout_seconds

    # -- protocol ---------------------------------------------------------

    def _invoke(
        self,
        operation: str,
        payload: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        request = {
            "contract_version": CONTRACT_VERSION,
            "request_id": request_id,
            "operation": operation,
            "payload": payload,
        }
        request_json = json.dumps(request, ensure_ascii=False)

        try:
            completed = subprocess.run(
                list(self.command),
                input=request_json + "\n",
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise MemoryAdapterError(
                "ADAPTER_UNAVAILABLE",
                request_id,
                "memory adapter executable not found: %s" % exc,
            )
        except subprocess.TimeoutExpired as exc:
            raise MemoryAdapterError(
                "ADAPTER_TIMEOUT",
                request_id,
                "memory adapter exceeded %ss timeout"
                % self.timeout_seconds,
            )
        except OSError as exc:
            raise MemoryAdapterError(
                "ADAPTER_UNAVAILABLE",
                request_id,
                "memory adapter could not be started: %s" % exc,
            )

        if completed.returncode != 0:
            raise MemoryAdapterError(
                "ADAPTER_CRASHED",
                request_id,
                "memory adapter exited with code %d: %s"
                % (completed.returncode, _bounded(completed.stderr)),
            )

        lines = [
            line for line in (completed.stdout or "").splitlines() if line.strip()
        ]
        if not lines:
            raise MemoryAdapterError(
                "SERIALIZATION_ERROR",
                request_id,
                "memory adapter produced no protocol response",
            )
        if len(lines) > 1:
            raise MemoryAdapterError(
                "SERIALIZATION_ERROR",
                request_id,
                "memory adapter produced %d protocol lines, expected 1"
                % len(lines),
            )

        try:
            response = json.loads(lines[0])
        except ValueError as exc:
            raise MemoryAdapterError(
                "SERIALIZATION_ERROR",
                request_id,
                "memory adapter response is not valid JSON: %s" % exc,
            )
        if not isinstance(response, dict):
            raise MemoryAdapterError(
                "SERIALIZATION_ERROR",
                request_id,
                "memory adapter response is not a JSON object",
            )
        if response.get("request_id") != request_id:
            raise MemoryAdapterError(
                "SERIALIZATION_ERROR",
                request_id,
                "memory adapter response request_id does not match request",
            )
        if response.get("contract_version") != CONTRACT_VERSION:
            raise MemoryAdapterError(
                "UNSUPPORTED_VERSION",
                request_id,
                "memory adapter contract_version %r is not supported"
                % (response.get("contract_version"),),
            )

        if response.get("ok") is not True:
            self._raise_protocol_error(response, request_id)
        result = response.get("result")
        if not isinstance(result, dict):
            raise MemoryAdapterError(
                "SERIALIZATION_ERROR",
                request_id,
                "memory adapter response has no result object",
            )
        return result

    @staticmethod
    def _raise_protocol_error(
        response: dict[str, Any], request_id: str
    ) -> None:
        error = response.get("error")
        if not isinstance(error, dict):
            raise MemoryAdapterError(
                "SERIALIZATION_ERROR",
                request_id,
                "memory adapter reported failure without an error object",
            )
        code = error.get("code")
        message = error.get("message")
        if not isinstance(code, str) or code not in KNOWN_ERROR_CODES:
            raise MemoryAdapterError(
                "INTERNAL_ERROR",
                request_id,
                "memory adapter returned unrecognized error code %r"
                % (code,),
            )
        raise MemoryAdapterError(
            code,
            request_id,
            message if isinstance(message, str) else code,
        )

    # -- operations -------------------------------------------------------

    def store(
        self,
        *,
        work_item_id: str,
        session_id: str,
        content: str,
        provenance: MemoryProvenance,
        metadata: dict[str, Any] | None = None,
    ) -> StoredMemory:
        request_id = str(uuid.uuid4())
        payload = {
            "work_item_id": work_item_id,
            "session_id": session_id,
            "content": content,
            "provenance": {
                "source_type": provenance.source_type,
                "source_id": provenance.source_id,
                "captured_at": provenance.captured_at,
            },
            "metadata": {} if metadata is None else metadata,
        }
        result = self._invoke("store", payload, request_id)
        memory_id = result.get("memory_id")
        if not isinstance(memory_id, str) or not memory_id:
            raise MemoryAdapterError(
                "SERIALIZATION_ERROR",
                request_id,
                "memory adapter store response has no memory_id",
            )
        return StoredMemory(
            memory_id=memory_id,
            work_item_id=work_item_id,
            session_id=session_id,
        )

    def retrieve(
        self,
        *,
        work_item_id: str,
        query: str,
        session_id: str | None = None,
        limit: int = 10,
        threshold: float = 0.34,
    ) -> RetrieveResult:
        request_id = str(uuid.uuid4())
        payload = {
            "work_item_id": work_item_id,
            "query": query,
            "session_id": session_id,
            "limit": limit,
            "threshold": threshold,
        }
        result = self._invoke("retrieve", payload, request_id)
        raw_entries = result.get("entries")
        if not isinstance(raw_entries, list):
            raise MemoryAdapterError(
                "SERIALIZATION_ERROR",
                request_id,
                "memory adapter retrieve response has no entries list",
            )

        entries: list[RetrievedMemory] = []
        for raw in raw_entries:
            if not isinstance(raw, dict):
                raise MemoryAdapterError(
                    "SERIALIZATION_ERROR",
                    request_id,
                    "memory adapter returned a non-object entry",
                )
            entry_work_item_id = raw.get("work_item_id")
            if entry_work_item_id != work_item_id:
                raise MemoryAdapterError(
                    "SCOPE_VIOLATION",
                    request_id,
                    "memory adapter returned an entry outside the requested "
                    "work item scope",
                )

            # A supplied Session ID is an exact response boundary, not merely
            # a best-effort Adapter filter. Validate every entry before any
            # result can be returned to the caller.
            if session_id is not None:
                entry_session_id = raw.get("session_id")
                if (
                    not isinstance(entry_session_id, str)
                    or not entry_session_id
                    or entry_session_id.strip() != entry_session_id
                    or entry_session_id != session_id
                ):
                    raise MemoryAdapterError(
                        "SCOPE_VIOLATION",
                        request_id,
                        "memory adapter returned an entry outside the "
                        "requested session scope",
                    )
            else:
                # Preserve v0.1 unscoped-within-WorkItem retrieval behavior.
                entry_session_id = raw["session_id"]

            try:
                entries.append(
                    RetrievedMemory(
                        memory_id=str(raw["memory_id"]),
                        work_item_id=entry_work_item_id,
                        session_id=str(entry_session_id),
                        content=str(raw["content"]),
                        provenance=dict(raw.get("provenance") or {}),
                        created_at=str(raw["created_at"]),
                        score=float(raw["score"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise MemoryAdapterError(
                    "SERIALIZATION_ERROR",
                    request_id,
                    "memory adapter entry is malformed: %s" % exc,
                )
        return RetrieveResult(entries=tuple(entries))
