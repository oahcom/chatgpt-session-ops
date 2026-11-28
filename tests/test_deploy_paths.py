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
