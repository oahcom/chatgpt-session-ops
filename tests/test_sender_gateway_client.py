"""Tests for Sender client key-path configuration in the unified runtime."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.session_ops.sender_gateway_client import (
    IDENTITY_KEY_FILE_ENV,
    SenderGatewayClient,
    _read_identity_key,
)


class SenderGatewayClientConfigTests(unittest.TestCase):
    def test_identity_key_file_requires_exact_32_byte_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "sender-identity.key"
            key_path.write_bytes(b"i" * 32)
            key_path.chmod(0o600)
            self.assertEqual(_read_identity_key(str(key_path)), b"i" * 32)

    def test_from_env_uses_unified_repository_key_path(self):
        config = {
            "url": "https://sender.example.test",
            "client_cert": "/tmp/client.crt",
            "client_key": "/tmp/client.key",
            "ca_file": "/tmp/ca.crt",
            "token_file": "/tmp/sender.token",
        }
        environment = {
            IDENTITY_KEY_FILE_ENV: "/etc/session-ops/sender-identity.key",
        }
        with (
            patch(
                "src.session_ops.sender_gateway_client.load_sender_gateway_config",
                return_value=config,
            ),
            patch(
                "src.session_ops.sender_gateway_client._read_identity_key",
                return_value=b"i" * 32,
            ) as read_key,
            patch.object(SenderGatewayClient, "__init__", return_value=None),
        ):
            client = SenderGatewayClient.from_env(environment)

        self.assertIsInstance(client, SenderGatewayClient)
        read_key.assert_called_once_with(environment[IDENTITY_KEY_FILE_ENV])


if __name__ == "__main__":
    unittest.main()
