"""Real TLS transport, resource bounds, deadlines and optional configuration."""
import json
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import assistclient
import peerwork

IDENTITY = {"code": "test-only", "native": 116}
TOKEN = "test-only-token-not-for-real-services-12345"


class TLSHelper:
    def __init__(self, cert, key, response=None, hello=None, delay=0):
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert, key)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.listener.settimeout(.1)
        self.port = self.listener.getsockname()[1]
        self.response = response or ({"ok": True}, [b"payload"])
        self.hello = hello
        self.delay = delay
        self.requests = []
        self.opened = threading.Event()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            try:
                raw, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self.handle, args=(raw,), daemon=True).start()

    def handle(self, raw):
        try:
            raw.settimeout(2)
            with self.context.wrap_socket(raw, server_side=True) as sock:
                h, _ = peerwork.recv_msg(sock)
                if h.get("token") != TOKEN:
                    peerwork.send_msg(sock, {"ok": False, "error": "refused " + TOKEN})
                    return
                peerwork.send_msg(sock, self.hello or {"ok": True, "protocol": 1,
                                                     "identity": IDENTITY, "workers": 2})
                h, _ = peerwork.recv_msg(sock)
                self.requests.append(h)
                self.opened.set()
                if self.stop.wait(self.delay):
                    return
                peerwork.send_msg(sock, *self.response)
        except (OSError, ConnectionError, ValueError):
            raw.close()

    def close(self):
        self.stop.set()
        self.listener.close()
        self.thread.join(2)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "assist.json"
        self.fields = {"schema": 1, "host": "127.0.0.1", "port": 47623,
                       "ca_file": str(self.root / "ca.pem"), "token_file": str(self.root / "token")}

    def load(self, **over):
        self.path.write_text(json.dumps(dict(self.fields, **over)))
        return assistclient.Config.load(self.path)

    def test_defaults_and_ip_validation(self):
        config = self.load()
        self.assertEqual((config.slots, config.timeout_s), (2, 4.0))
        self.assertEqual(self.load(host="::1").host, "::1")
        for value in ("localhost", "https://127.0.0.1", "0.0.0.0", "224.0.0.1", "fe80::1%1", True):
            with self.assertRaises(ValueError):
                self.load(host=value)

    def test_limits_and_unknown_fields(self):
        for over in ({"schema": True}, {"schema": 2}, {"slots": 0}, {"slots": 5}, {"slots": True},
                     {"port": 0}, {"port": 65536}, {"port": True}, {"timeout_s": .1}, {"timeout_s": 16},
                     {"timeout_s": float("nan")}, {"ca_file": "ca.pem"}, {"token": TOKEN}):
            with self.assertRaises(ValueError, msg=str(over)):
                self.load(**over)
        self.path.write_text('{"schema":1,"schema":1}')
        with self.assertRaises(ValueError):
            assistclient.Config.load(self.path)
        with self.assertRaises(ValueError):
            assistclient.Config.load("relative.json")

    def test_worker_offline_pin_and_disabled_never_read_config(self):
        for env in ({}, {"BNGE_ASSIST_CONFIG": "x", "BNGE_ASSIST_WORKER": "1"},
                    {"BNGE_ASSIST_CONFIG": "x", "BNGE_ASSIST_OFFLINE": "1"},
                    {"BNGE_ASSIST_CONFIG": "x", "BNGE_WORLD_BUNDLE": "x"}):
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(assistclient.Config, "load") as read:
                self.assertIsNone(assistclient.get_client())
                read.assert_not_called()

    def test_invalid_config_warns_once_without_secret_or_path(self):
        with mock.patch.dict(os.environ, {"BNGE_ASSIST_CONFIG": str(self.path)}, clear=True), \
                mock.patch.object(assistclient, "_cached_key", None), \
                mock.patch.object(assistclient, "_cached_client", None), \
                mock.patch.object(assistclient.Config, "load", side_effect=ValueError(TOKEN)), \
                self.assertLogs(assistclient.log, level="WARNING") as logs:
            self.assertIsNone(assistclient.get_client())
            self.assertIsNone(assistclient.get_client())
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn(TOKEN, str(logs.output))
        self.assertNotIn(str(self.path), str(logs.output))


class FrameTest(unittest.TestCase):
    def frame(self, data, limit=assistclient.MAX_FRAME):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        a.sendall(data)
        return assistclient.recv_frame(b, time.monotonic() + .5, limit)

    def test_roundtrip_and_aggregate_limit(self):
        message = peerwork.encode_msg({"ok": True}, [b"a" * 20, b"b" * 20])
        self.assertEqual(self.frame(message), ({"ok": True}, [b"a" * 20, b"b" * 20]))
        with self.assertRaises(ValueError):
            self.frame(message, len(message) - 1)

    def test_rejects_huge_blob_before_allocating_or_waiting(self):
        prefix = peerwork.MAGIC + struct.pack("<I", 2) + b"{}" + struct.pack("<HQ", 1, 1 << 50)
        with self.assertRaises(ValueError):
            self.frame(prefix)
        with self.assertRaises(ValueError):
            self.frame(peerwork.MAGIC + struct.pack("<I", assistclient.MAX_HEADER + 1))

    def test_rejects_duplicate_and_nonfinite_json(self):
        for data in (b'{"x":1,"x":2}', b'{"x":NaN}', b'[]'):
            with self.assertRaises(ValueError):
                self.frame(peerwork.MAGIC + struct.pack("<I", len(data)) + data + b"\0\0")

    def test_dribbling_bytes_cannot_extend_absolute_deadline(self):
        a, b = socket.socketpair()
        stop = threading.Event()

        def drip():
            try:
                for byte in peerwork.encode_msg({"ok": True}):
                    a.sendall(bytes([byte]))
                    if stop.wait(.03):
                        return
            except OSError:
                pass
        worker = threading.Thread(target=drip)
        worker.start()
        started = time.monotonic()
        try:
            with self.assertRaises(TimeoutError):
                assistclient.recv_frame(b, started + .12)
            self.assertLess(time.monotonic() - started, .5)
        finally:
            stop.set()
            a.close()
            b.close()
            worker.join(1)


class TLSTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        openssl = shutil.which("openssl")
        if not openssl and Path("C:/Program Files/Git/usr/bin/openssl.exe").exists():
            openssl = "C:/Program Files/Git/usr/bin/openssl.exe"
        if not openssl:
            raise unittest.SkipTest("OpenSSL needed to create an ephemeral test certificate")
        cls.tmp = tempfile.TemporaryDirectory(prefix="assist-test-cert-")
        cls.addClassCleanup(cls.tmp.cleanup)
        root = Path(cls.tmp.name)
        cls.cert, cls.key = root / "cert.pem", root / "key.pem"
        cls.token = root / "token"
        cls.token.write_text(TOKEN)
        # Disposable P-256 keys avoid variable RSA prime-search time on loaded CI hosts.
        subprocess.run([openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt",
                        "ec_paramgen_curve:prime256v1", "-nodes", "-batch", "-days", "1",
                        "-keyout", str(cls.key), "-out", str(cls.cert), "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=IP:127.0.0.1"], check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)

    def setUp(self):
        self.jobs = mock.patch.dict(sys.modules, {"assistjobs": types.SimpleNamespace(identity=lambda: IDENTITY)})
        self.jobs.start()
        self.addCleanup(self.jobs.stop)

    def client(self, **server_options):
        server = TLSHelper(self.cert, self.key, **server_options)
        self.addCleanup(server.close)
        config = assistclient.Config("127.0.0.1", server.port, str(self.cert), str(self.token), 1, .5)
        client = assistclient.Client(config)
        self.addCleanup(client.close)
        return client, server

    def test_real_tls_roundtrip_and_slot_capacity(self):
        client, server = self.client()
        self.assertTrue(client.try_acquire())
        self.assertFalse(client.try_acquire())
        self.assertEqual(client.call({"op": "far_global_geometry"}, reserved=True), ({"ok": True}, [b"payload"]))
        self.assertEqual(server.requests, [{"op": "far_global_geometry"}])
        self.assertTrue(client.try_acquire())
        client.release()
        self.assertEqual(client.stats["completed"], 1)
        self.assertFalse(client._sockets)

    def test_real_service_interoperability_and_cache_reuse(self):
        import assistservice
        calls = []

        def run(header):
            calls.append(header)
            return {"ok": True, "cacheable": True}, [b"fixture-result"]
        with ThreadPoolExecutor(1) as executor:
            broker = assistservice.Broker(dict.fromkeys(peerwork.SETTINGS, True), self.tmp.name, workers=1,
                                           executor=executor, runner=run, validator=lambda h, s: h, identity=IDENTITY)
            server = assistservice.AssistanceServer(("127.0.0.1", 0), TOKEN, broker,
                                                     assistservice.tls_context(self.cert, self.key))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            client = assistclient.Client(assistclient.Config("127.0.0.1", server.server_address[1],
                                         str(self.cert), str(self.token), 1, 1.0))
            try:
                for _ in range(2):
                    self.assertEqual(client.call({"op": "fixture"})[1], [b"fixture-result"])
                self.assertEqual(len(calls), 1)
                self.assertEqual(broker.snapshot()["cache_hits"], 1)
            finally:
                client.close()
                server.shutdown()
                server.server_close()
                broker.close()
                thread.join(1)

    def test_identity_mismatch_never_submits_job(self):
        client, server = self.client(hello={"ok": True, "protocol": 1, "identity": {"code": "wrong"}, "workers": 1})
        with self.assertRaises(assistclient.Unavailable):
            client.call({"op": "far_geometry"})
        self.assertEqual(server.requests, [])
        self.assertFalse(client.try_acquire())

    def test_untrusted_certificate_sends_no_token_or_job(self):
        client, server = self.client()
        client._context = ssl.create_default_context()
        with self.assertRaises(assistclient.Unavailable):
            client.call({"op": "far_geometry"})
        self.assertEqual(server.requests, [])

    def test_timeout_backoff_and_recovery(self):
        client, server = self.client(delay=1)
        started = time.monotonic()
        with self.assertRaises(assistclient.Unavailable):
            client.call({"op": "far_geometry"}, timeout_s=.25)
        self.assertLess(time.monotonic() - started, .8)
        self.assertFalse(client.try_acquire())
        client._retry_at = 0
        server.delay = 0
        self.assertEqual(client.call({"op": "far_geometry"})[0], {"ok": True})
        self.assertEqual(client.stats["failed"], 1)

    def test_remote_error_is_sanitized(self):
        client, _ = self.client(response=({"ok": False, "error": TOKEN}, []))
        with self.assertRaises(assistclient.Unavailable) as raised:
            client.call({"op": "far_geometry"})
        self.assertNotIn(TOKEN, str(raised.exception))

    def test_expected_declines_do_not_open_circuit_breaker(self):
        for error in ("pending", "busy", "source_mismatch", "incomplete_inputs"):
            client, _ = self.client(response=({"ok": False, "error": error}, []))
            self.assertEqual(client.call({"op": "plan_cell"}), ({"ok": False, "error": error}, []))
            self.assertEqual(client.stats["failed"], 0)
            self.assertTrue(client.try_acquire())
            client.release()

    def test_close_interrupts_active_call(self):
        client, server = self.client(delay=5)
        failures = []

        def run():
            try:
                client.call({"op": "far_geometry"}, timeout_s=5)
            except assistclient.Unavailable as exc:
                failures.append(exc)
        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(server.opened.wait(2))
        client.close()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertFalse(client.try_acquire())


if __name__ == "__main__":
    unittest.main()
