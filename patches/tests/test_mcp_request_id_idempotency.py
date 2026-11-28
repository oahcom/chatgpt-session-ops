"""Run with PYTHONPATH=/opt/so-mcp against the patched live source."""
import uuid
from types import SimpleNamespace

from src.session_ops.authority import Principal
from src.session_ops.mcp_auth import AuthContext
from src.session_ops.mcp_server import (
    MCPToolService, TOOL_SCHEMAS, _mcp_session_id,
    _request_scoped_idempotency_key,
)


class FakeGateway:
    def __init__(self):
        self.calls = []

    def enqueue(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "job_id": "cgj_123e4567-e89b-42d3-a456-426614174000",
            "status": "queued",
            "created_at": "2026-11-28T13:00:00Z",
            "conversation_id": "123e4567-e89b-42d3-a456-426614174001",
        }

    def get_job(self, **kwargs): return {}
    def list_sessions(self, **kwargs): return {}
    def read_session(self, **kwargs): return {}


def _service():
    principal = Principal("so-mcp", "so-mcp:42d7b1b4-f41b-4388-a1cc-a320991b734a")
    auth = AuthContext(principal, ("session_ops.chatgpt.session.write",),
                       "issuer", "subject", "realm")
    gateway = FakeGateway()
    return MCPToolService(object(), sender_gateway=gateway), auth, gateway


def test_request_key_stable_and_namespaced():
    principal = Principal("so-mcp", "so-mcp:42d7b1b4-f41b-4388-a1cc-a320991b734a")
    key = _request_scoped_idempotency_key("chatgpt.session.send", principal,
                                          "req-1", "session-A")
    assert key == _request_scoped_idempotency_key(
        "chatgpt.session.send", principal, "req-1", "session-A")
    assert uuid.UUID(key).version == 4
    assert key != _request_scoped_idempotency_key(
        "chatgpt.session.send", principal, "req-2", "session-A")
    assert key != _request_scoped_idempotency_key(
        "chatgpt.session.send", principal, "req-1", "session-B")


def test_dispatch_replay_reuses_key_and_explicit_key_is_preserved():
    service, auth, gateway = _service()
    args = {"prompt": "same intent"}
    for request_id in ("req-1", "req-1", "req-2"):
        service.dispatch("chatgpt.session.send", args, auth,
                          request_id=request_id, request_session_id="session-A")
    keys = [call["idempotency_key"] for call in gateway.calls]
    assert keys[0] == keys[1] != keys[2]
    explicit = str(uuid.uuid4())
    service.dispatch("chatgpt.session.send",
                      {"prompt": "same intent", "idempotency_key": explicit},
                      auth, request_id="req-3", request_session_id="session-A")
    assert gateway.calls[3]["idempotency_key"] == explicit
    assert "idempotency_key" in TOOL_SCHEMAS["chatgpt.session.create"]["required"]
    assert "idempotency_key" in TOOL_SCHEMAS["chatgpt.session.send"]["required"]


def test_session_header_extraction():
    request = SimpleNamespace(scope={"headers": [(b"Mcp-Session-Id", b"session-A")]})
    assert _mcp_session_id(request) == "session-A"
    assert _mcp_session_id(SimpleNamespace(scope={"headers": []})) is None
