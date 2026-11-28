import base64
import datetime as dt
import ipaddress
import json
import os
import ssl
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from sender_gateway.app import IdentityVerifier, make_handler
from sender_gateway.job_store import JobStore
from session_ops.sender_gateway_client import SenderGatewayClient
from src.session_ops.mcp_event_collector import MCPEventCollector
from src.session_ops.mcp_events import MCPEventsStore


def _write_key(path: Path, key) -> None:
    path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))


def _write_cert(path: Path, cert) -> None:
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _certs(directory: Path):
    now = dt.datetime.now(dt.timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Events PoC Test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=7))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    _write_cert(directory / "ca.pem", ca_cert)

    def leaf(name: str, eku, san=None):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject).issuer_name(ca_name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=7))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
        )
        if san:
            builder = builder.add_extension(x509.SubjectAlternativeName(san), critical=False)
        cert = builder.sign(ca_key, hashes.SHA256())
        return key, cert

    server_key, server_cert = leaf(
        "127.0.0.1", ExtendedKeyUsageOID.SERVER_AUTH,
        [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))],
    )
    client_key, client_cert = leaf("mcp-events-test-client", ExtendedKeyUsageOID.CLIENT_AUTH)
    _write_key(directory / "server-key.pem", server_key)
    _write_cert(directory / "server-cert.pem", server_cert)
    _write_key(directory / "client-key.pem", client_key)
    _write_cert(directory / "client-cert.pem", client_cert)


class FullPipelineTests(unittest.TestCase):
    def test_job_completion_to_chatgpt_webhook_through_real_mtls_client(self):
        with tempfile.TemporaryDirectory(dir="/dev/shm") as tmp:
            root = Path(tmp)
            sender_db = root / "sender.sqlite3"
            job_store = JobStore(sender_db)
            job = job_store.enqueue(
                principal_id="user-a", tenant_id="tenant-a", operation="session.send",
                idempotency_key="4f6efb6c-66c4-4c98-b0a8-1d2d25f91234",
                payload={"conversation_id": "a" * 36, "prompt": "run task", "account": "acct1"},
            )
            job_id = job["job_id"]
            job_store.claim_next("sender-worker")
            job_store.complete(job_id, {"summary": "Sender completed the task."})
            with job_store.transaction() as txn:
                sender_event = txn.fetchone("SELECT event_id FROM job_event_outbox WHERE job_id=?", (job_id,))
            self.assertIsNotNone(sender_event)

            identity_key = b"i" * 32
            service_token = "mcp-sender-test-token"
            token_file = root / "service-token.txt"
            token_file.write_text(service_token, encoding="utf-8")
            os.chmod(token_file, 0o600)
            verifier = IdentityVerifier(str(sender_db), identity_key, service_token.encode())
            app = SimpleNamespace(store=job_store, verifier=verifier)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
            server.daemon_threads = True
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            _certs(root)
            server_context.load_cert_chain(str(root / "server-cert.pem"), str(root / "server-key.pem"))
            server_context.load_verify_locations(cafile=str(root / "ca.pem"))
            server_context.verify_mode = ssl.CERT_REQUIRED
            server.socket = server_context.wrap_socket(server.socket, server_side=True)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()

            callback_requests = []
            def callback(_url, body, headers):
                callback_requests.append((json.loads(body), dict(headers)))
                event = json.loads(body)
                if event.get("type") == "verification":
                    return 200, json.dumps({"challenge": event["challenge"]}).encode()
                return 204, b""

            events_store = MCPEventsStore(
                str(root / "mcp-events.sqlite3"),
                encryption_key=b"e" * 32,
                post_json=callback,
            )
            auth = SimpleNamespace(principal=SimpleNamespace(tenant_id="tenant-a", principal_id="user-a"))
            with patch("src.session_ops.mcp_events._resolve_validated_address", return_value="93.184.216.34"):
                events_store.subscribe(auth, {
                    "name": "chatgpt.job.completed",
                    "arguments": {"job_id": job_id},
                    "delivery": {
                        "mode": "webhook",
                        "url": "https://events.example.test/callback",
                        "secret": "whsec_" + base64.b64encode(b"w" * 32).decode(),
                    },
                })

            try:
                client = SenderGatewayClient(
                    url="https://127.0.0.1:%d" % server.server_port,
                    client_cert=str(root / "client-cert.pem"),
                    client_key=str(root / "client-key.pem"),
                    ca_file=str(root / "ca.pem"),
                    token_file=str(token_file),
                    identity_key=identity_key,
                    timeout_seconds=2,
                )
                collector = MCPEventCollector(client, events_store, worker_id="mcp-events-collector")
                self.assertEqual(collector.run_once(), 1)
                self.assertEqual(events_store.deliver_pending(), 1)

                with job_store.transaction() as txn:
                    sender_row = txn.fetchone("SELECT status FROM job_event_outbox WHERE job_id=?", (job_id,))
                self.assertEqual(sender_row["status"], "delivered")
                with events_store._db() as db:
                    event_row = db.execute("SELECT event_id, data_json FROM event_outbox").fetchone()
                    delivery_row = db.execute("SELECT status FROM event_deliveries").fetchone()
                self.assertEqual(delivery_row["status"], "delivered")
                self.assertEqual(event_row["event_id"], sender_event["event_id"])
                event_data = json.loads(event_row["data_json"])
                self.assertEqual(event_data["job_id"], job_id)
                self.assertEqual(event_data["conversation_id"], "a" * 36)
                self.assertEqual(callback_requests[-1][0]["eventId"], sender_event["event_id"])
                self.assertEqual(callback_requests[-1][0]["name"], "chatgpt.job.completed")
                self.assertEqual(callback_requests[-1][0]["data"]["job_id"], job_id)
                self.assertEqual(callback_requests[-1][0]["data"]["conversation_id"], "a" * 36)
                self.assertTrue(callback_requests[-1][1]["webhook-signature"].startswith("v1,"))
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=2)
                job_store.close()


if __name__ == "__main__":
    unittest.main()
