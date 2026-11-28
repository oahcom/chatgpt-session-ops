from pathlib import Path


def test_deploy_manifest_contains_only_live_mcp_event_modules():
    root = Path(__file__).resolve().parents[1]
    paths = {
        line.strip()
        for line in (root / "deploy" / "paths.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert {
        "scripts/run_mcp_server.py",
        "scripts/chatgpt_send.py",
        "scripts/pow_solver.py",
        "src/session_ops/mcp_events.py",
        "src/session_ops/chatgpt_direct_client.py",
        "src/session_ops/mcp_event_dispatcher.py",
    } <= paths
    assert "src/session_ops/mcp_event_collector.py" not in paths
    assert "src/session_ops/sender_gateway_client.py" not in paths


def test_service_uses_the_immutable_runtime_artifact():
    root = Path(__file__).resolve().parents[1]
    service = (root / "deploy" / "so-mcp.service").read_text()
    assert "WorkingDirectory=/opt/so-mcp" in service
    assert "ExecStart=/opt/so-mcp/.venv/bin/python -m scripts.run_mcp_server" in service
    assert "SenderGatewayClient" not in service
    assert "Environment=CHATGPT_SESSION_OPS_ACCOUNTS_DIR=/var/lib/chatgpt-session-ops/accounts" in service
    assert "Environment=CHATGPT_SESSION_OPS_RUNTIME_DIR=/var/lib/so-mcp/chatgpt-session-ops" in service
    assert "ReadWritePaths=/var/lib/so-mcp /var/lib/chatgpt-session-ops/accounts" in service
    assert "TimeoutStopSec=600" in service
    assert "UMask=0077" in service


def test_deploy_runbook_documents_events_opt_in_and_oauth_scope_gate():
    root = Path(__file__).resolve().parents[1]
    docs = (root / "docs" / "DEPLOY-so-mcp.md").read_text()
    assert "SO_MCP_EVENTS_KEY_FILE" in docs
    assert "SO_MCP_EVENTS_DB" in docs
    assert "session_ops.chatgpt.session.read" in docs
    assert "session_ops.chatgpt.session.write" in docs
    assert "does not prove" in docs
    assert "real ChatGPT Work" in docs
    assert "immutable for the lifetime" in docs
    assert "SQLite's online backup API" in docs
    recovery = (root / "docs" / "mcp-events-poc.md").read_text()
    assert "DELETE FROM event_subscriptions" in recovery
    assert "event_key_metadata WHERE singleton=1" in recovery
