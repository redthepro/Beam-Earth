"""Authenticated, bounded world-generation assistance; separate from BeamMP.

Only assistjobs' validated operations enter the worker pool. A client timeout does
not release a running job's admission slot: its result can warm the reply cache.
"""
from collections import OrderedDict
from concurrent.futures import Future, TimeoutError, wait
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import multiprocessing
import os
from pathlib import Path
import shutil
import socket
import socketserver
import ssl
import threading
import time

import peerwork

log = logging.getLogger("earthd.assistservice")
PROTOCOL = 1
MAX_FRAME = 64 << 20
MAX_REQUEST = (64 << 10) + 10
DEFAULT_SETTINGS = dict(roads=True, grading=True, road_widths=True,
                        airports=True, sea=True, roads_in_ground=False)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _positive(value, name, maximum, integer=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 < value <= maximum
            or (integer and not isinstance(value, int))):
        raise ValueError("invalid " + name)
    return value


def read_token(path=None, env_name="BNGE_ASSIST_TOKEN"):
    """The value is never included in errors, logs, command arguments or reprs."""
    if path:
        with open(path, "rb") as stream:
            raw = stream.read(513)
        try:
            token = raw.decode("ascii").strip()
        except UnicodeError as exc:
            raise ValueError("invalid assistance token file") from exc
    else:
        token = os.environ.get(env_name, "")
    if not isinstance(token, str) or not 32 <= len(token) <= 512 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("assistance token must contain 32 to 512 non-whitespace ASCII characters")
    return token


def tls_context(cert, key):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=cert, keyfile=key)
    return context


class DeadlineSocket:
    """Absolute frame deadline and aggregate read limit around the existing wire format."""
    def __init__(self, sock, seconds, limit=MAX_FRAME):
        self.sock, self.deadline, self.remaining_bytes = sock, time.monotonic() + seconds, limit

    def _timeout(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("frame deadline")
        self.sock.settimeout(left)

    def recv(self, size):
        if size > self.remaining_bytes:
            raise ValueError("frame exceeds byte budget")
        self._timeout()
        data = self.sock.recv(size)
        self.remaining_bytes -= len(data)
        return data

    def sendall(self, data):
        if len(data) > self.remaining_bytes:
            raise ValueError("frame exceeds byte budget")
        for offset in range(0, len(data), 65536):
            self._timeout()
            self.sock.sendall(data[offset:offset + 65536])
        self.remaining_bytes -= len(data)


def checked_reply(reply, limit=MAX_FRAME):
    """Bound worker output before encoding/caching; clients still validate native arrays."""
    header, blobs = reply
    if not isinstance(header, dict) or not isinstance(blobs, (tuple, list)) or len(blobs) > 8:
        raise ValueError("invalid worker reply")
    size = len(canonical(header))
    if size > 65536 or any(not isinstance(blob, bytes) for blob in blobs):
        raise ValueError("invalid worker reply")
    size += 10 + 8 * len(blobs) + sum(map(len, blobs))
    if size > limit:
        raise ValueError("worker reply exceeds byte budget")
    # Prevent a caller mutating a cached header after admission; blob bytes are immutable.
    return (json.loads(canonical(header)), tuple(blobs)), size


def failure(reason, retry_after=None):
    head = {"ok": False, "error": reason}
    if retry_after is not None:
        head["retry_after"] = retry_after
    return head, ()


class _Entry:
    def __init__(self):
        self.result = Future()
        self.actual = None
        self.timer = None


class Broker:
    """One aggregate pool, admission per unique job and bounded completed replies."""
    def __init__(self, settings, cache_dir, *, workers=4, max_jobs=8,
                 cache_bytes=128 << 20, cache_ttl=300.0, hard_timeout=90.0,
                 executor=None, runner=None, validator=None, identity=None,
                 admission=None):
        self.workers = _positive(workers, "workers", 32, True)
        self.max_jobs = _positive(max_jobs, "max_jobs", 128, True)
        self.cache_bytes = _positive(cache_bytes, "cache_bytes", 2 << 30, True)
        self.cache_ttl = _positive(cache_ttl, "cache_ttl", 3600)
        self.hard_timeout = _positive(hard_timeout, "hard_timeout", 600)
        if (not isinstance(settings, dict) or set(settings) != set(peerwork.SETTINGS)
                or any(type(v) is not bool for v in settings.values())):
            raise ValueError("configure all six boolean world settings")
        self.settings = dict(settings)
        if runner is None or validator is None or identity is None:
            import assistjobs
            runner = runner or assistjobs.run
            validator = validator or assistjobs.validate_request
            identity = identity if identity is not None else assistjobs.identity()
        self.identity = json.loads(canonical(identity))
        self.runner, self.validator, self.admission = runner, validator, admission
        self._owned_executor = executor is None
        if executor is None:
            import assistjobs
            import plancache
            import pools
            # All spawned workers inherit one run ID, so a road cell planned by
            # one worker is immediately reusable by the others.
            plancache.start_run(str(cache_dir))
            executor = pools.RespawningPool(
                "assistance", workers, mp_context=multiprocessing.get_context("spawn"),
                initializer=assistjobs.init_worker, initargs=(str(cache_dir), self.settings))
        self.executor = executor
        self.lock = threading.Lock()
        self.inflight = {}
        self.cache = OrderedDict()
        self.cache_size = 0
        self.closed = False
        self.stats = dict(submitted=0, completed=0, failed=0, cache_hits=0,
                          joined=0, busy=0, pending=0, expired=0)

    def _expire_cache(self, now):
        for key in [k for k, (expiry, _, _) in self.cache.items() if expiry <= now]:
            self.cache_size -= self.cache.pop(key)[2]

    def request(self, header, wait_s=3.0):
        """Return a result or a stable failure; pending work remains admitted."""
        _positive(wait_s, "request wait", 120)
        try:
            request = self.validator(header, self.settings)
            if not isinstance(request, dict):
                raise ValueError("invalid request")
            data = canonical(request)
            if len(data) > 65536:
                raise ValueError("request too large")
            key = hashlib.sha256(data).digest()
        except Exception:
            return failure("invalid_request")
        with self.lock:
            if self.closed:
                return failure("unavailable", 2)
            self._expire_cache(time.monotonic())
            cached = self.cache.get(key)
            if cached is not None:
                self.cache.move_to_end(key)
                self.stats["cache_hits"] += 1
                # A fresh header avoids cross-handler mutation while sharing immutable blobs.
                return json.loads(canonical(cached[1][0])), cached[1][1]
            entry = self.inflight.get(key)
            mine = entry is None
            if not mine:
                self.stats["joined"] += 1
            elif len(self.inflight) >= self.max_jobs:
                self.stats["busy"] += 1
                return failure("busy", 1)
            else:
                if self.admission is not None and not self.admission():
                    self.stats["busy"] += 1
                    return failure("storage_unavailable", 5)
                entry = self.inflight[key] = _Entry()
                self.stats["submitted"] += 1
        if mine:
            try:
                # Serialize process submission with close: shutdown must see
                # every actual Future/process before taking its termination snapshot.
                with self.lock:
                    if self.closed:
                        raise RuntimeError("service closing")
                    actual = self.executor.submit(self.runner, request)
                    entry.actual = actual
                    timer = entry.timer = threading.Timer(self.hard_timeout, self._timeout, args=(key, entry))
                    timer.daemon = True
                # A completed Future invokes callbacks synchronously, so register
                # this outside the lock (_finish takes it too).
                actual.add_done_callback(lambda future: self._finish(key, entry, future))
                with self.lock:
                    if not self.closed and not actual.done():
                        timer.start()
            except Exception:
                bad = Future()
                bad.set_exception(RuntimeError("worker submission failed"))
                self._finish(key, entry, bad)
        try:
            return entry.result.result(wait_s)
        except TimeoutError:
            with self.lock:
                self.stats["pending"] += 1
            return failure("pending", 1)

    def _finish(self, key, entry, future):
        try:
            reply, size = checked_reply(future.result())
        except Exception:
            reply, size = failure("worker_failed", 2), 0
        if entry.timer is not None:
            entry.timer.cancel()
        with self.lock:
            if self.inflight.get(key) is not entry:
                return
            self.inflight.pop(key)
            self.stats["completed" if reply[0].get("ok") is True else "failed"] += 1
            if (not self.closed and reply[0].get("ok") is True
                    and reply[0].get("cacheable") is True and size <= self.cache_bytes):
                old = self.cache.pop(key, None)
                if old is not None:
                    self.cache_size -= old[2]
                while self.cache and self.cache_size + size > self.cache_bytes:
                    self.cache_size -= self.cache.popitem(last=False)[1][2]
                self.cache[key] = (time.monotonic() + self.cache_ttl, reply, size)
                self.cache_size += size
        entry.result.set_result(reply)

    def _timeout(self, key, entry):
        with self.lock:
            if self.closed or self.inflight.get(key) is not entry:
                return
            self.stats["expired"] += 1
        # Cancelling a running Future does not stop native work. Kill/recreate the
        # production pool; its callbacks release admissions only after actual termination.
        recycle = getattr(self.executor, "recycle", None)
        if recycle is not None:
            try:
                renewed = recycle("assistance hard job deadline")
                if renewed is False:
                    # RespawningPool throttles repeated restarts. Recheck after
                    # that cooldown, retaining admission while the job is live.
                    with self.lock:
                        if not self.closed and self.inflight.get(key) is entry:
                            entry.timer = threading.Timer(10.1, self._timeout, args=(key, entry))
                            entry.timer.daemon = True
                            entry.timer.start()
            except Exception:
                log.warning("assistance worker recycle failed")

    def snapshot(self):
        with self.lock:
            return dict(self.stats, inflight=len(self.inflight), cache_entries=len(self.cache),
                        cache_bytes=self.cache_size, workers=self.workers)

    def close(self, grace_s=5.0):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            entries = list(self.inflight.values())
            self.cache.clear()
            self.cache_size = 0
        for entry in entries:
            if entry.timer is not None:
                entry.timer.cancel()
            if entry.actual is not None:
                entry.actual.cancel()
        if entries:
            wait([e.result for e in entries], timeout=grace_s)
        if self._owned_executor:
            # RespawningPool already uses this exact termination pattern for hung
            # native jobs. Shutdown must not leave its atexit join blocked forever.
            pool = getattr(self.executor, "_pool", None)
            for process in list((getattr(pool, "_processes", None) or {}).values()):
                try:
                    process.kill()
                except (OSError, AttributeError):
                    pass
            self.executor.shutdown(wait=False, cancel_futures=True)


class DiskAdmission:
    """Refuse new work at a disk budget; active writes may briefly overshoot it.

    This never follows directory symlinks or deletes workers' live files. A host
    filesystem quota is the hard cap; this guard is an operational early stop.
    """
    def __init__(self, root, max_bytes=8_000_000_000, min_free_bytes=1_000_000_000,
                 interval=30.0):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = _positive(max_bytes, "disk budget", 10 ** 15, True)
        self.min_free_bytes = _positive(min_free_bytes, "free disk reserve", 10 ** 15, True)
        self.interval = _positive(interval, "disk scan interval", 3600)
        self.allowed = False
        self.used_bytes = 0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="assist-disk-budget", daemon=True)
        self.thread.start()

    def scan(self):
        try:
            total = 0
            for directory, dirs, files in os.walk(self.root, followlinks=False):
                if self.stop.is_set():
                    return
                dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(directory, name))]
                for name in files:
                    path = os.path.join(directory, name)
                    try:
                        total += os.lstat(path).st_size
                    except FileNotFoundError:
                        continue
            self.used_bytes = total
            self.allowed = total < self.max_bytes and shutil.disk_usage(self.root).free > self.min_free_bytes
        except OSError:
            self.allowed = False

    def _loop(self):
        while not self.stop.is_set():
            self.scan()
            self.stop.wait(self.interval)

    def __call__(self):
        return self.allowed

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        server = self.server
        sock = self.request
        try:
            sock.settimeout(server.frame_timeout)
            if server.ssl_context is not None:
                secure = server.ssl_context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
                server.replace_socket(sock, secure)
                sock = secure
                sock.do_handshake()
            hello, blobs = peerwork.recv_msg(DeadlineSocket(sock, server.frame_timeout, MAX_REQUEST))
            token = hello.get("token")
            authenticated = (not blobs and hello.get("op") == "hello" and type(hello.get("protocol")) is int
                             and hello.get("protocol") == PROTOCOL
                             and isinstance(token, str) and token.isascii()
                             and hmac.compare_digest(token, server._token)
                             and hello.get("identity") == server.broker.identity)
            if not authenticated:
                server.send(sock, *failure("unauthorized_or_incompatible"))
                return
            server.send(sock, {"ok": True, "protocol": PROTOCOL, "identity": server.broker.identity,
                               "workers": server.broker.workers}, ())
            while not server.stopping.is_set():
                header, blobs = peerwork.recv_msg(DeadlineSocket(sock, server.frame_timeout, MAX_REQUEST))
                if blobs:
                    server.send(sock, *failure("invalid_request"))
                    return
                server.send(sock, *server.broker.request(header, server.request_wait))
        except (OSError, ConnectionError, ValueError, TypeError, TimeoutError):
            # Malformed input and disconnects are routine. Never log request headers or tokens.
            pass
        finally:
            server.remove_socket(sock)
            try:
                sock.close()
            except OSError:
                pass


class AssistanceServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16

    def __init__(self, address, token, broker, ssl_context, *, max_connections=16,
                 frame_timeout=5.0, request_wait=3.0, allow_plain_loopback=False):
        address_ip = ipaddress.ip_address(address[0])
        if ssl_context is None and not (allow_plain_loopback and address_ip.is_loopback):
            raise ValueError("TLS is required outside explicit loopback test mode")
        if not isinstance(token, str) or not 32 <= len(token) <= 512 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("invalid assistance token")
        self._token = token
        self.broker, self.ssl_context = broker, ssl_context
        self.frame_timeout = _positive(frame_timeout, "frame timeout", 60)
        self.request_wait = _positive(request_wait, "request wait", 120)
        self.connection_limit = threading.BoundedSemaphore(_positive(max_connections, "connections", 256, True))
        self.socket_lock = threading.Lock()
        self.active_sockets = set()
        self.stopping = threading.Event()
        if address_ip.version == 6:
            self.address_family = socket.AF_INET6
        super().__init__(address, _Handler)

    def process_request(self, request, address):
        if self.stopping.is_set() or not self.connection_limit.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self.socket_lock:
            self.active_sockets.add(request)
        try:
            super().process_request(request, address)
        except Exception:
            self.remove_socket(request)
            self.connection_limit.release()
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.remove_socket(request)
            self.connection_limit.release()

    def replace_socket(self, old, new):
        with self.socket_lock:
            self.active_sockets.discard(old)
            self.active_sockets.add(new)

    def remove_socket(self, sock):
        with self.socket_lock:
            self.active_sockets.discard(sock)

    def send(self, sock, header, blobs):
        reply, _ = checked_reply((header, blobs))
        peerwork.send_msg(DeadlineSocket(sock, self.frame_timeout), *reply)

    def handle_error(self, request, client_address):
        log.warning("assistance connection handler failed")

    def server_close(self):
        self.stopping.set()
        with self.socket_lock:
            sockets = list(self.active_sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        super().server_close()


def main(argv=None):
    import argparse
    import signal
    parser = argparse.ArgumentParser(description="Bounded BeamNG.Earth world assistance service")
    parser.add_argument("--host", default="127.0.0.1", help="literal bind IP")
    parser.add_argument("--port", type=int, default=47623)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--settings", required=True, help="JSON file containing all six boolean world settings")
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-jobs", type=int, default=8)
    parser.add_argument("--max-connections", type=int, default=16)
    parser.add_argument("--reply-cache-mb", type=int, default=128)
    parser.add_argument("--disk-budget-gb", type=float, default=8.0)
    parser.add_argument("--hard-timeout", type=float, default=90.0)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Validate all configuration before creating worker processes or binding sockets.
    guard = broker = server = None
    try:
        token = read_token(args.token_file)
        context = tls_context(args.cert, args.key)
        with open(args.settings, "rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("settings file too large")
        settings = json.loads(raw)
        disk_budget = int(_positive(args.disk_budget_gb, "disk budget", 1e6) * 1e9)
        guard = DiskAdmission(args.cache_dir, disk_budget)
        broker = Broker(settings, args.cache_dir, workers=args.workers, max_jobs=args.max_jobs,
                        cache_bytes=args.reply_cache_mb << 20, hard_timeout=args.hard_timeout, admission=guard)
        server = AssistanceServer((args.host, args.port), token, broker, context,
                                  max_connections=args.max_connections)
    except (OSError, ValueError, TypeError):
        if server is not None:
            server.server_close()
        if broker is not None:
            broker.close(0)
        if guard is not None:
            guard.close()
        log.error("assistance configuration invalid; check certificate, token and settings files")
        return 2
    stopped = threading.Event()
    def stop(_signum, _frame):
        stopped.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)
    thread = threading.Thread(target=server.serve_forever, name="assist-listener", daemon=True)
    thread.start()
    log.info("assistance listening on %s:%d; workers=%d admitted_jobs=%d", args.host, args.port,
             args.workers, args.max_jobs)
    try:
        while not stopped.wait(30):
            log.info("assistance metrics %s", json.dumps(broker.snapshot(), sort_keys=True))
    finally:
        server.shutdown()
        server.server_close()
        broker.close()
        guard.close()
        thread.join(timeout=2)
    return 0
