"""Opt-in, bounded TLS client for the operator's geographic compute helper.

Configuration is local to the companion, never supplied by a BeamMP packet. The
endpoint is a literal IP so DNS lookup cannot evade the request deadline. The
certificate must include that IP in its SAN. Each call owns one TLS connection;
there are no reconnect threads, idle sockets, or queues waiting for capacity.
"""
from dataclasses import dataclass
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import socket
import ssl
import struct
import threading
import time

import peerwork

log = logging.getLogger("earthd.assist")
CONFIG_ENV = "BNGE_ASSIST_CONFIG"
MAX_FRAME = 64 << 20
MAX_HEADER = 64 << 10
MAX_BLOBS = 8
PROTOCOL = 1


class Unavailable(ConnectionError):
    """A sanitized failure; the caller should use its existing local path."""


def _number(value, low, high, name, integer=False):
    if (isinstance(value, bool) or not isinstance(value, int if integer else (int, float))
            or not math.isfinite(value) or not low <= value <= high):
        raise ValueError("Invalid assistance " + name)
    return value


def _json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate assistance JSON key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("Nonfinite assistance JSON")
    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    ca_file: str
    token_file: str
    slots: int = 2
    timeout_s: float = 4.0

    @classmethod
    def load(cls, path):
        path = Path(path)
        if not path.is_absolute():
            raise ValueError("Assistance configuration must use an absolute path")
        with path.open("rb") as stream:
            data = stream.read(16385)
        if len(data) > 16384:
            raise ValueError("Assistance configuration exceeds size limit")
        fields = _json(data)
        required = {"schema", "host", "port", "ca_file", "token_file"}
        if (not isinstance(fields, dict) or not required <= fields.keys()
                or fields.keys() - required - {"slots", "timeout_s"}
                or type(fields["schema"]) is not int or fields["schema"] != 1):
            raise ValueError("Invalid assistance configuration schema")
        host = fields["host"]
        if not isinstance(host, str) or "%" in host:
            raise ValueError("Assistance host must be a literal IP address")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            raise ValueError("Assistance host must be a literal IP address") from None
        if address.is_unspecified or address.is_multicast:
            raise ValueError("Invalid assistance destination")
        for name in ("ca_file", "token_file"):
            value = fields[name]
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise ValueError("Assistance file paths must be absolute")
        return cls(str(address), _number(fields["port"], 1, 65535, "port", True),
                   fields["ca_file"], fields["token_file"],
                   _number(fields.get("slots", 2), 1, 4, "slots", True),
                   float(_number(fields.get("timeout_s", 4.0), .25, 15, "timeout")))


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Assistance deadline expired")
    return remaining


def _exact(sock, count, deadline):
    result = bytearray()
    while len(result) < count:
        sock.settimeout(_remaining(deadline))
        part = sock.recv(min(count - len(result), 1 << 20))
        if not part:
            raise Unavailable("Assistance connection closed")
        result += part
    return bytes(result)


def recv_frame(sock, deadline, max_bytes=MAX_FRAME):
    """peerwork framing with an aggregate byte bound and an absolute deadline."""
    if _exact(sock, 4, deadline) != peerwork.MAGIC:
        raise ValueError("Invalid assistance frame")
    length = struct.unpack("<I", _exact(sock, 4, deadline))[0]
    if length > MAX_HEADER or length + 10 > max_bytes:
        raise ValueError("Assistance header exceeds size limit")
    head = _json(_exact(sock, length, deadline))
    if not isinstance(head, dict):
        raise ValueError("Invalid assistance header")
    count = struct.unpack("<H", _exact(sock, 2, deadline))[0]
    if count > MAX_BLOBS:
        raise ValueError("Assistance blob count exceeds limit")
    total = 10 + length + count * 8
    if total > max_bytes:
        raise ValueError("Assistance frame exceeds size limit")
    blobs = []
    for _ in range(count):
        length = struct.unpack("<Q", _exact(sock, 8, deadline))[0]
        total += length
        if total > max_bytes:
            raise ValueError("Assistance frame exceeds size limit")
        blobs.append(_exact(sock, length, deadline))
    return head, blobs


def _send(sock, header, deadline):
    data = json.dumps(header, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > MAX_HEADER:
        raise ValueError("Assistance header exceeds size limit")
    sock.settimeout(_remaining(deadline))
    sock.sendall(peerwork.MAGIC + struct.pack("<I", len(data)) + data + b"\0\0")


class Client:
    def __init__(self, config):
        self.config = config
        self.slots = config.slots
        with open(config.token_file, "rb") as stream:
            token = stream.read(514).strip()
        if not 32 <= len(token) <= 512 or any(c < 33 or c > 126 for c in token):
            raise ValueError("Invalid assistance token file")
        self._token = token.decode("ascii")
        self._context = ssl.create_default_context(cafile=config.ca_file)
        self._context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._gate = threading.BoundedSemaphore(self.slots)
        self._lock = threading.Lock()
        self._sockets = set()
        self._closed = False
        self._retry_at = 0.0
        self._failures = 0
        self.stats = {"completed": 0, "failed": 0, "busy": 0, "declined": 0, "seconds": 0.0, "bytes": 0}

    def try_acquire(self):
        """Reserve without waiting, before queuing an I/O task. False means local work."""
        with self._lock:
            if self._closed or time.monotonic() < self._retry_at:
                return False
            return self._gate.acquire(blocking=False)

    def release(self):
        """Return a reservation when preparing/submitting the request failed."""
        self._gate.release()

    def available_slots(self):
        with self._lock:
            return 0 if self._closed or time.monotonic() < self._retry_at else self.slots

    def note_failure(self):
        """Also back off when the caller rejects a structurally invalid job result."""
        with self._lock:
            self._failures += 1
            self._retry_at = time.monotonic() + min(30.0, 2.0 ** min(self._failures, 5))
            self.stats["failed"] += 1

    def _track(self, sock, old=None):
        with self._lock:
            if old is not None:
                self._sockets.discard(old)
            if self._closed:
                sock.close()
                raise Unavailable("Assistance client closed")
            self._sockets.add(sock)

    def call(self, header, timeout_s=None, *, reserved=False):
        """One bounded request; failures are sanitized, capacity is always returned."""
        if not reserved and not self.try_acquire():
            raise Unavailable("Assistance temporarily unavailable")
        start = time.monotonic()
        sock = None
        try:
            timeout = self.config.timeout_s if timeout_s is None else timeout_s
            deadline = start + _number(timeout, .25, 15, "timeout")
            import assistjobs
            identity = assistjobs.identity()
            family = socket.AF_INET6 if ":" in self.config.host else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_STREAM)
            self._track(sock)
            sock.settimeout(_remaining(deadline))
            sock.connect((self.config.host, self.config.port))
            raw = sock
            sock = self._context.wrap_socket(raw, server_hostname=self.config.host,
                                             do_handshake_on_connect=False)
            self._track(sock, raw)
            sock.settimeout(_remaining(deadline))
            sock.do_handshake()
            _send(sock, {"op": "hello", "protocol": PROTOCOL, "token": self._token,
                         "identity": identity}, deadline)
            hello, blobs = recv_frame(sock, deadline, MAX_HEADER + 10)
            if (blobs or hello.get("ok") is not True or type(hello.get("protocol")) is not int
                    or hello["protocol"] != PROTOCOL or hello.get("identity") != identity
                    or type(hello.get("workers")) is not int or not 1 <= hello["workers"] <= 32):
                raise Unavailable("Assistance handshake refused")
            _send(sock, header, deadline)
            head, blobs = recv_frame(sock, deadline)
            if head.get("ok") is not True:
                if (head.get("ok") is False and not blobs and head.get("error") in
                        ("busy", "pending", "source_mismatch", "incomplete_inputs")):
                    with self._lock:
                        self.stats["declined"] += 1
                        if head["error"] == "busy":
                            self.stats["busy"] += 1
                    return head, []
                raise Unavailable("Assistance request refused")
            with self._lock:
                self._failures = 0
                self.stats["completed"] += 1
                self.stats["bytes"] += sum(len(b) for b in blobs)
            return head, blobs
        except Exception:
            self.note_failure()
            # Exception text may contain a remote-controlled error or a local secret path.
            raise Unavailable("Assistance unavailable; use local generation") from None
        finally:
            if sock is not None:
                with self._lock:
                    self._sockets.discard(sock)
                sock.close()
            with self._lock:
                self.stats["seconds"] += time.monotonic() - start
            self.release()

    def close(self):
        """Interrupt in-flight calls; no joins or retry timers on the gameplay thread."""
        with self._lock:
            self._closed = True
            sockets = tuple(self._sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()


_cached_key = None
_cached_client = None
_cached_lock = threading.Lock()


def get_client():
    """One configuration per process/path; changing file contents needs a restart."""
    global _cached_key, _cached_client
    if (os.environ.get("BNGE_ASSIST_WORKER") == "1" or os.environ.get("BNGE_WORLD_BUNDLE")
            or os.environ.get("BNGE_ASSIST_OFFLINE") == "1"):
        return None
    path = os.environ.get(CONFIG_ENV)
    if not path:
        return None
    key = (os.getpid(), path)
    with _cached_lock:
        if key != _cached_key:
            if _cached_client is not None:
                _cached_client.close()
            _cached_key, _cached_client = key, None
            try:
                _cached_client = Client(Config.load(path))
            except Exception:
                log.warning("Server assistance disabled: invalid local configuration")
        return _cached_client
