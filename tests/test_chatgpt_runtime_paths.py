import json
import os
import subprocess
import sys
from pathlib import Path

from scripts import chatgpt_send as m


def test_runtime_and_auth_roots_are_separate(tmp_path):
    state_dir = tmp_path / "runtime"
    auth_dir = tmp_path / "auth"
    env = os.environ.copy()
    env["CHATGPT_STATE_DIR"] = str(state_dir)
    env["CHATGPT_AUTH_DIR"] = str(auth_dir)
    root = Path(__file__).resolve().parents[1]
    code = """
import json
import scripts.chatgpt_send as m
print(json.dumps({
    'base': m.BASE,
    'rotation': m.ROTATION_FILE,
    'conversation_map': m.CONV_MAP_FILE,
    'anon_sessions': m.ANON_SESSIONS_FILE,
    'proxy': m.PROXY_FILE,
    'auth': m._auth_base(),
    'anon_devices': m.anon_device_dir(),
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    paths = json.loads(result.stdout)
    assert paths["base"] == str(state_dir)
    assert paths["rotation"] == str(state_dir / "account-rotation.json")
    assert paths["conversation_map"] == str(state_dir / "conv-accounts.json")
    assert paths["anon_sessions"] == str(state_dir / "anon-sessions.json")
    assert paths["proxy"] == str(state_dir / "proxy.json")
    assert paths["auth"] == str(auth_dir)
    assert paths["anon_devices"] == str(auth_dir / "accounts" / "nologin" / "devices")


def test_acct1_prefers_explicit_per_account_vault_but_keeps_legacy_fallback(tmp_path, monkeypatch):
    auth_root = tmp_path / "vault"
    acct1 = auth_root / "accounts" / "acct1"
    acct1.mkdir(parents=True)
    (acct1 / "cookies.json").write_text("[]", encoding="utf-8")
    (acct1 / "access_token.txt").write_text("token", encoding="utf-8")
    monkeypatch.setenv("CHATGPT_AUTH_DIR", str(auth_root))
    monkeypatch.setattr(m, "require_proxy", lambda: "http://127.0.0.1:8080")
    monkeypatch.setattr(m, "resolve_proxy_info", lambda: {"source": "test"})
    monkeypatch.setattr(m, "build_opener_for", lambda *args: object())

    api = m.ChatGPTAPI(account="acct1")
    assert Path(api.base_dir) == acct1

    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    (legacy_root / "cookies.json").write_text("[]", encoding="utf-8")
    (legacy_root / "access_token.txt").write_text("token", encoding="utf-8")
    monkeypatch.setenv("CHATGPT_AUTH_DIR", str(legacy_root))
    legacy_api = m.ChatGPTAPI(account="acct1")
    assert Path(legacy_api.base_dir) == legacy_root
