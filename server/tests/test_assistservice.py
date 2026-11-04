"""Real TLS transport plus bounded admission/cache/recovery. No native speed claims."""
from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
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
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import assistservice as service
import peerwork

TOKEN = "test-only-not-a-production-token-123456789"
IDENTITY = {"schema": 1, "code": "synthetic-service-contract"}
SETTINGS = service.DEFAULT_SETTINGS


def valid(header, settings):
    if set(header) != {"op", "key"} or header["op"] != "test" or type(header["key"]) is not int:
        raise ValueError("invalid test request")
    return dict(header)


def reply(request):
    return {"ok": True, "cacheable": True, "key": request["key"]}, [b"terrain-fixture"]


def hanging_process_job(request):
    if request["key"] == 0:
        time.sleep(60)
    return reply(request)


class BrokerTests(unittest.TestCase):
    def broker(self, runner=reply, **kwargs):
        executor = ThreadPoolExecutor(max_workers=4)
        broker = service.Broker(SETTINGS, "unused", executor=executor, runner=runner,
                                validator=valid, identity=IDENTITY, **kwargs)
        self.addCleanup(executor.shutdown, wait=True, cancel_futures=True)
        self.addCleanup(broker.close, 0.1)
        return broker

    def test_same_job_single_flight_and_cached(self):
        started, release = threading.Event(), threading.Event()
        calls = []
        def run(request):
            calls.append(request)
            started.set()
            release.wait(2)
            return reply(request)
        broker = self.broker(run)
        with ThreadPoolExecutor(2) as clients:
            first = clients.submit(broker.request, {"op": "test", "key": 1})
            self.assertTrue(started.wait(1))
            second = clients.submit(broker.request, {"op": "test", "key": 1})
            limit = time.monotonic() + 1
            while broker.snapshot()["joined"] == 0 and time.monotonic() < limit:
                time.sleep(.005)
            release.set()
            self.assertEqual(first.result(), second.result())
        self.assertTrue(broker.request({"op": "test", "key": 1})[0]["ok"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(broker.snapshot()["cache_hits"], 1)

    def test_timeout_keeps_admission_until_actual_work_finishes(self):
        release = threading.Event()
        def run(request):
            release.wait(2)
            return reply(request)
        broker = self.broker(run, max_jobs=1)
        try:
            self.assertEqual(broker.request({"op": "test", "key": 1}, .02)[0]["error"], "pending")
            self.assertEqual(broker.snapshot()["inflight"], 1)
            self.assertEqual(broker.request({"op": "test", "key": 2})[0]["error"], "busy")
        finally:
            release.set()
        self.assertTrue(broker.request({"op": "test", "key": 1})[0]["ok"])
        self.assertEqual(broker.snapshot()["inflight"], 0)

    def test_failures_and_incomplete_replies_are_not_cached(self):
        for response in (({"ok": False, "error": "incomplete"}, []), ({"ok": True}, [])):
            with self.subTest(response=response):
                calls = []
                def run(request):
                    calls.append(request)
                    return response
                broker = self.broker(run)
                broker.request({"op": "test", "key": 1})
                broker.request({"op": "test", "key": 1})
                self.assertEqual(len(calls), 2)
                self.assertEqual(broker.snapshot()["cache_entries"], 0)

    def test_cache_byte_and_ttl_limits(self):
        broker = self.broker(cache_bytes=100, cache_ttl=.04)
        broker.request({"op": "test", "key": 1})
        broker.request({"op": "test", "key": 2})
        self.assertLessEqual(broker.snapshot()["cache_bytes"], 100)
        self.assertEqual(broker.snapshot()["cache_entries"], 1)
        time.sleep(.06)
        broker.request({"op": "test", "key": 2})
        self.assertEqual(broker.snapshot()["submitted"], 3)

    def test_unknown_input_never_submitted(self):
        broker = self.broker()
        self.assertEqual(broker.request({"op": "os.system", "key": 1})[0]["error"], "invalid_request")
        self.assertEqual(broker.snapshot()["submitted"], 0)

    def test_worker_exception_is_sanitized_and_fallback_can_retry(self):
        count = [0]
        def run(request):
            count[0] += 1
            if count[0] == 1:
                raise ValueError("private-path token=" + TOKEN)
            return reply(request)
        broker = self.broker(run)
        first = broker.request({"op": "test", "key": 1})
        self.assertEqual(first, service.failure("worker_failed", 2))
        self.assertNotIn(TOKEN, repr(first))
        self.assertTrue(broker.request({"op": "test", "key": 1})[0]["ok"])

    def test_storage_limit_still_serves_cached_results(self):
        allowed = [True]
        broker = self.broker(admission=lambda: allowed[0])
        broker.request({"op": "test", "key": 1})
        allowed[0] = False
        self.assertTrue(broker.request({"op": "test", "key": 1})[0]["ok"])
        self.assertEqual(broker.request({"op": "test", "key": 2})[0]["error"], "storage_unavailable")

    def test_hard_timeout_requests_recycle_without_fake_cancellation(self):
        released = threading.Event()
        recycled = threading.Event()
        def run(request):
            released.wait(2)
            return reply(request)
        broker = self.broker(run, max_jobs=1, hard_timeout=.05)
        broker.executor.recycle = lambda reason: recycled.set()
        try:
            broker.request({"op": "test", "key": 1}, .01)
            self.assertTrue(recycled.wait(1))
            self.assertEqual(broker.snapshot()["inflight"], 1)
            self.assertEqual(broker.request({"op": "test", "key": 2})[0]["error"], "busy")
        finally:
            released.set()

    def test_close_refuses_new_work(self):
        broker = self.broker()
        broker.close()
        self.assertEqual(broker.request({"op": "test", "key": 1})[0]["error"], "unavailable")

    def test_hung_real_worker_is_killed_and_replaced(self):
        import pools
        executor = pools.RespawningPool("assist-test", 1, mp_context=multiprocessing.get_context("spawn"))
        broker = service.Broker(SETTINGS, "unused", workers=1, max_jobs=1, hard_timeout=1.0,
                                executor=executor, runner=hanging_process_job,
                                validator=valid, identity=IDENTITY)
        broker._owned_executor = True  # exercise production termination on this real isolated process pool
        try:
            self.assertEqual(broker.request({"op": "test", "key": 0}, .05)[0]["error"], "pending")
            deadline = time.monotonic() + 6
            while broker.snapshot()["inflight"] and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(broker.snapshot()["inflight"], 0)
            self.assertEqual(executor.respawns, 1)
            broker.hard_timeout = 5
            self.assertTrue(broker.request({"op": "test", "key": 1}, 5)[0]["ok"])
        finally:
            broker.close(.1)

    def test_configuration_and_reply_limits(self):
        for kwargs in ({"workers": 0}, {"workers": True}, {"max_jobs": 0}, {"cache_ttl": float("nan")}):
            with self.assertRaises(ValueError):
                self.broker(**kwargs)
        with self.assertRaises(ValueError):
            service.checked_reply(({"ok": True}, [b"x" * 100]), 64)
        with self.assertRaises(ValueError):
            service.checked_reply(({"ok": True}, [b""] * 9))
        with self.assertRaises(ValueError):
            service.checked_reply(({"number": float("nan")}, []))

    def test_actual_job_schema_and_fixed_settings_gate(self):
        import assistjobs
        calls = []
        def run(request):
            calls.append(request)
            return {"ok": True, "op": request["op"]}, []
        executor = ThreadPoolExecutor(1)
        broker = service.Broker(SETTINGS, "unused", executor=executor, runner=run,
                                validator=assistjobs.validate_request, identity=IDENTITY)
        context = {"airport": None, "sources": {key: None for key in
                   ("pipeline", "overture", "worldcover", "canopy", "rails", "lanes", "gradesep")}}
        request = {"op": "plan_cell", "cell": [14, 2812, 6552], "settings": dict(SETTINGS), "context": context}
        try:
            self.assertEqual(broker.request(request)[0], {"ok": True, "op": "plan_cell"})
            request["settings"]["road_widths"] = not SETTINGS["road_widths"]
            self.assertEqual(broker.request(request)[0]["error"], "invalid_request")
            self.assertEqual(len(calls), 1)
        finally:
            broker.close(.1)
            executor.shutdown()


class TLSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        executable = shutil.which("openssl") or "C:/Program Files/Git/usr/bin/openssl.exe"
        if not Path(executable).exists():
            raise unittest.SkipTest("openssl is required to generate disposable TLS test certificates")
        cls.directory = tempfile.TemporaryDirectory(prefix="earth-assist-tls-test-")
        cls.cert = str(Path(cls.directory.name) / "cert.pem")
        cls.key = str(Path(cls.directory.name) / "key.pem")
        subprocess.run([executable, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", cls.key, "-out", cls.cert, "-days", "1", "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=IP:127.0.0.1", "-addext", "basicConstraints=critical,CA:TRUE"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        cls.context = service.tls_context(cls.cert, cls.key)
        cls.client_context = ssl.create_default_context(cafile=cls.cert)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        self.executor = ThreadPoolExecutor(2)
        self.broker = service.Broker(SETTINGS, "unused", executor=self.executor, runner=reply,
                                    validator=valid, identity=IDENTITY)
        self.server = service.AssistanceServer(("127.0.0.1", 0), TOKEN, self.broker, self.context,
                                               max_connections=2, frame_timeout=.3, request_wait=.1)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.broker.close(.1)
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.thread.join(1)

    def connect(self, token=TOKEN, identity=IDENTITY):
        raw = socket.create_connection(self.server.server_address, timeout=1)
        sock = self.client_context.wrap_socket(raw, server_hostname="127.0.0.1")
        peerwork.send_msg(sock, {"op": "hello", "protocol": 1, "token": token, "identity": identity})
        return sock, peerwork.recv_msg(sock)[0]

    def test_real_tls_hello_and_job_round_trip(self):
        sock, head = self.connect()
        with sock:
            self.assertTrue(head["ok"])
            self.assertEqual(head["identity"], IDENTITY)
            peerwork.send_msg(sock, {"op": "test", "key": 3})
            response, blobs = peerwork.recv_msg(sock)
            self.assertEqual(response["key"], 3)
            self.assertEqual(blobs, [b"terrain-fixture"])

    def test_actual_client_and_service_interoperate(self):
        import assistclient
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "token"
            token_path.write_text(TOKEN, encoding="ascii")
            config = assistclient.Config("127.0.0.1", self.server.server_address[1], self.cert,
                                         str(token_path), slots=1, timeout_s=2)
            client = assistclient.Client(config)
            try:
                with mock.patch("assistjobs.identity", return_value=IDENTITY):
                    head, blobs = client.call({"op": "test", "key": 7})
                self.assertEqual(head["key"], 7)
                self.assertEqual(blobs, [b"terrain-fixture"])
            finally:
                client.close()

    def test_wrong_token_or_identity_never_reaches_worker(self):
        for kwargs in ({"token": "wrong-token"}, {"identity": {"code": "different"}}):
            sock, head = self.connect(**kwargs)
            with sock:
                self.assertEqual(head["error"], "unauthorized_or_incompatible")
        self.assertEqual(self.broker.snapshot()["submitted"], 0)

    def test_input_blobs_rejected(self):
        sock, _ = self.connect()
        with sock:
            peerwork.send_msg(sock, {"op": "test", "key": 3}, [b"unwanted"])
            head, _ = peerwork.recv_msg(sock)
            self.assertEqual(head["error"], "invalid_request")
        self.assertEqual(self.broker.snapshot()["submitted"], 0)

    def test_oversized_declared_blob_is_refused_before_body(self):
        sock, _ = self.connect()
        with sock:
            header = json.dumps({"op": "test", "key": 3}).encode()
            sock.sendall(b"BNGE" + struct.pack("<I", len(header)) + header + struct.pack("<HQ", 1, 60 << 20))
            self.assertEqual(sock.recv(1), b"")
        self.assertEqual(self.broker.snapshot()["submitted"], 0)

    def test_connection_limit_counts_unfinished_tls_handshakes(self):
        a = socket.create_connection(self.server.server_address, timeout=1)
        b = socket.create_connection(self.server.server_address, timeout=1)
        try:
            deadline = time.monotonic() + 1
            while len(self.server.active_sockets) < 2 and time.monotonic() < deadline:
                time.sleep(.005)
            with socket.create_connection(self.server.server_address, timeout=1) as c:
                self.assertEqual(c.recv(1), b"")
        finally:
            a.close()
            b.close()

    def test_absolute_frame_deadline_defeats_slow_drip(self):
        sock, _ = self.connect()
        with sock:
            sock.sendall(b"B")
            time.sleep(.17)
            sock.sendall(b"N")
            time.sleep(.18)
            self.assertEqual(sock.recv(1), b"")

    def test_untrusted_certificate_fails_client_verification(self):
        with socket.create_connection(self.server.server_address, timeout=1) as raw:
            with self.assertRaises(ssl.SSLCertVerificationError):
                ssl.create_default_context().wrap_socket(raw, server_hostname="127.0.0.1")


class ConfigurationTests(unittest.TestCase):
    def test_plain_remote_bind_refused(self):
        with self.assertRaisesRegex(ValueError, "TLS"):
            service.AssistanceServer(("0.0.0.0", 0), TOKEN, None, None, allow_plain_loopback=True)

    def test_token_is_not_exposed_in_validation_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text("private-short-token")
            with self.assertRaises(ValueError) as caught:
                service.read_token(path)
            self.assertNotIn("private-short-token", str(caught.exception))
            path.write_text(TOKEN)
            self.assertEqual(service.read_token(path), TOKEN)

    def test_disk_budget_refuses_full_cache_without_deleting_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tile"
            path.write_bytes(b"x" * 100)
            guard = service.DiskAdmission(directory, max_bytes=50, min_free_bytes=1)
            try:
                guard.scan()
                self.assertFalse(guard())
                self.assertEqual(path.read_bytes(), b"x" * 100)
            finally:
                guard.close()


if __name__ == "__main__":
    unittest.main()
