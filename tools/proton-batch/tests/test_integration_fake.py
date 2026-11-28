# -*- coding: utf-8 -*-
"""离线集成：本地假 Proton 服务，验证 Pipeline 全链路与 HV 头传递。

不触网、不需要打码/接码/邮箱。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proton_batch.config import DEFAULTS, deep_merge
from proton_batch.pipeline import Pipeline


class _Handler(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def log_message(self, *args):  # 静默
        pass

    def _send(self, payload: dict, status: int = 200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        _Handler.calls.append({"method": "GET", "path": self.path,
                               "headers": dict(self.headers)})
        if "/users/available" in self.path:
            return self._send({"Code": 1000})
        return self._send({"Code": 9999, "Error": "unknown"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() if length else "{}"
        _Handler.calls.append({"method": "POST", "path": self.path,
                               "headers": dict(self.headers), "body": json.loads(raw or "{}")})
        if self.path.endswith("/auth/verification"):
            return self._send({"Code": 1000, "Details": {"Methods": ["captcha"]}})
        if self.path.endswith("/verification/captcha"):
            return self._send({"Code": 1000, "Details": {
                "HumanVerificationToken": "hv-abc",
                "HumanVerificationTokenType": "captcha"}})
        if self.path.endswith("/auth/v4/users"):
            if self.headers.get("x-pm-human-verification-token") != "hv-abc":
                return self._send({"Code": 9001, "Error": "missing HV"}, 400)
            return self._send({"Code": 1000, "UserID": "uid-1",
                               "User": {"ID": "uid-1"}})
        if self.path.endswith("/auth/v4"):
            body = json.loads(raw or "{}")
            return self._send({"Code": 1000, "UID": "u-1",
                               "User": {"ID": "uid-1"},
                               "AccessToken": "at-1",
                               "Echo": body.get("Username")})
        return self._send({"Code": 9999}, 404)


class TestPipelineAgainstFakeProton(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_full_proton_flow_reaches_verified(self):
        _Handler.calls.clear()
        with tempfile.TemporaryDirectory() as tmp:
            cfg = deep_merge(DEFAULTS, {
                "proton": {
                    "base_url": f"http://127.0.0.1:{self.port}/api",
                    "username_prefix": "oahcom",
                    "username_digits": 6,
                    "login_verify": True,
                },
                "storage": {
                    "data_dir": tmp,
                    "db_path": str(Path(tmp) / "t.db"),
                    "log_dir": str(Path(tmp) / "logs"),
                },
                "network": {"proxies": [], "retries": 0,
                            "delay_between_accounts_sec": [0, 0]},
            })
            cfg["_root"] = tmp

            pipeline = Pipeline(cfg, _noop_logger())

            # 用桩替换打码：只验证 HV 流程与状态机，不触网。
            # Pipeline 内部每次都会新建服务实例，所以在工厂层打补丁。
            import proton_batch.pipeline as pl
            original = pl.Pipeline._services

            def patched(self, tag):
                http, p = original(self, tag)
                p.solver.api_key = "test-key"          # 让 enabled=True
                p.solver.solve_hcaptcha = lambda *a, **k: "captcha-response"
                p.solver.solve_any = lambda fn, attempts=3: fn()
                return http, p

            pl.Pipeline._services = patched
            try:
                created = pipeline.run_proton(1)
            finally:
                pl.Pipeline._services = original

            self.assertEqual(len(created), 1)
            row = pipeline.store.get(created[0])
            self.assertEqual(row["proton_status"], "verified")
            self.assertEqual(row["proton_user_id"], "uid-1")

            signup = [c for c in _Handler.calls
                      if c["method"] == "POST" and c["path"].endswith("/auth/v4/users")]
            self.assertEqual(len(signup), 1)
            self.assertEqual(signup[0]["headers"].get("x-pm-human-verification-token"),
                             "hv-abc")
            self.assertEqual(signup[0]["headers"]
                             .get("x-pm-human-verification-token-type"), "captcha")


def _noop_logger():
    import logging
    logger = logging.getLogger("test_pipeline")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


if __name__ == "__main__":
    unittest.main()
