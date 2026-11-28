# -*- coding: utf-8 -*-
"""离线单测：不触网、不需要任何密钥。

运行：
    cd tools/proton-batch
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proton_batch import passwords, usernamegen
from proton_batch.config import DEFAULTS, deep_merge, load_config
from proton_batch.mailbox import extract_code, message_text
from proton_batch.proxy import ProxyPool, to_requests_map
from proton_batch.store import Store, decrypt_secret, encrypt_secret


class TestConfig(unittest.TestCase):
    def test_deep_merge_keeps_defaults(self):
        merged = deep_merge(DEFAULTS, {"proton": {"username_prefix": "abc"}})
        self.assertEqual(merged["proton"]["username_prefix"], "abc")
        self.assertEqual(merged["proton"]["base_url"], DEFAULTS["proton"]["base_url"])

    def test_load_config_without_file_uses_defaults(self):
        cfg = load_config("definitely-missing-config.json")
        self.assertFalse(cfg["_config_found"])
        self.assertIn("network", cfg)


class TestUsername(unittest.TestCase):
    def test_shape_and_unique(self):
        names = usernamegen.candidates("oahcom", 6, 20)
        self.assertEqual(len(set(names)), 20)
        for name in names:
            self.assertTrue(name.startswith("oahcom"))
            self.assertEqual(len(name), 12)
            self.assertNotEqual(name[6], "0")


class TestPasswords(unittest.TestCase):
    def test_strength(self):
        value = passwords.generate(24)
        self.assertEqual(len(value), 24)
        self.assertTrue(passwords.looks_strong(value))

    def test_short_rejected(self):
        with self.assertRaises(ValueError):
            passwords.generate(4)


class TestStore(unittest.TestCase):
    def test_secret_roundtrip(self):
        raw = "Pa$$w0rd-\u4e2d\u6587"
        self.assertEqual(decrypt_secret(encrypt_secret(raw)), raw)
        self.assertIsNone(encrypt_secret(None))

    def test_crud_and_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            aid = store.add_proton("oahcom123456", "oahcom123456@proton.me", "pw-1")
            row = store.get(aid)
            self.assertEqual(row["proton_status"], "pending")
            self.assertEqual(decrypt_secret(row["proton_password"]), "pw-1")

            store.update(aid, proton_status="verified", rogue_column="x")
            self.assertEqual(store.get(aid)["proton_status"], "verified")
            store.event(aid, "proton", "info", "ok")
            # add_proton 自带一条「创建本地记录」事件
            self.assertEqual(len(store.events(aid)), 2)

            stats = store.stats()
            self.assertEqual(stats["total"], 1)
            self.assertEqual(stats["proton"]["verified"], 1)

    def test_duplicate_username_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            store.add_proton("dup", "dup@proton.me", "pw")
            with self.assertRaises(Exception):
                store.add_proton("dup", "dup@proton.me", "pw2")


class TestProxy(unittest.TestCase):
    def test_rotation_and_cooldown(self):
        pool = ProxyPool(["http://a:1", "http://b:1"], cooldown_sec=999)
        first = pool.acquire()
        second = pool.acquire()
        self.assertNotEqual(first, second)
        pool.report(first, False)
        seen = {pool.acquire() for _ in range(4)}
        self.assertNotIn(first, seen)

    def test_map(self):
        self.assertEqual(to_requests_map(None), None)
        self.assertEqual(to_requests_map("1.2.3.4:8080")["http"], "http://1.2.3.4:8080")
        self.assertIn("socks5h://x:1", to_requests_map("socks5h://x:1")["https"])


class TestMailParsing(unittest.TestCase):
    def test_code_extraction(self):
        text = "Your verification code is 482913. It expires in 10 minutes."
        self.assertEqual(extract_code(text, r"(?<!\d)(\d{6})(?!\d)"), "482913")
        self.assertIsNone(extract_code("no code here", r"(?<!\d)(\d{6})(?!\d)"))

    def test_message_text_plain(self):
        from email import message_from_string
        msg = message_from_string(
            "From: a@b.c\nSubject: code\nContent-Type: text/plain; charset=utf-8\n\n"
            "Code: 123456\n")
        self.assertIn("123456", message_text(msg))


class TestProtonVerificationOrder(unittest.TestCase):
    def test_auto_order_stops_after_success(self):
        """auto 模式在 captcha 成功后不应继续尝试 email/sms。"""
        from proton_batch.proton_api import ProtonClient

        cfg = {
            "proton": {**DEFAULTS["proton"], "verification": "auto"},
            "user_agent": DEFAULTS["user_agent"],
        }
        client = ProtonClient(cfg, mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock(),
                              mock.Mock())
        client.hv = None

        calls = []

        def fake_captcha():
            calls.append("captcha")
            client.hv = {"token": "t", "type": "captcha"}

        client.solve_captcha = fake_captcha
        client.solve_email = lambda *a, **k: calls.append("email")
        client.solve_sms = lambda *a, **k: calls.append("sms")

        client.ensure_human_verification(methods=["captcha", "email", "sms"])
        self.assertEqual(calls, ["captcha"])


if __name__ == "__main__":
    unittest.main()
