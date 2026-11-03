"""Opt-in, bounded local gameplay capture. No filesystem work on the caller's thread.

The companion owns one capture at a time; its connection layer owns authorization.
Only allowlisted numeric/boolean measurements reach the JSONL file. Coordinates
are an explicit session option. This is diagnostic evidence, not crash attribution.
"""
from collections import deque
from datetime import datetime, timezone
import json
from itertools import islice
import math
from pathlib import Path
import re
import threading
import time
import uuid

SCHEMA = 1
HISTOGRAM_MS = (1, 2, 4, 8, 12, 16.7, 20, 25, 33.4, 50, 66.7, 100, 150, 250, 500, 1000, 2000, 5000, None)
FRAME_FIELDS = frozenset("frames wall_ms avg_ms p50_ms p95_ms p99_ms max_ms over_33 over_50 over_100 sim_s sim_wall_ms sim_wall_ratio paused_frames".split())
CONTEXT_FIELDS = frozenset("speed_mps lua_mb connected active paused server_backlog terrain_wait_s terrain_last_s rebasing ground_held edge_held ground_ready far_loaded far_queued far_pending far_old far_materials roads_loaded roads_queued colliders_queued roads_solid_around trees_loaded trees_pending trees_items trees_errors follow_count rebase_count block_edge_m block_edge_s road_ahead_m road_ahead_s road_probe_m road_probe_step_m road_probe_limited telemetry_dropped telemetry_ms net_pending_frames".split())
SERVER_FIELDS = frozenset("loop_lag_ms snapshot_ms connections anchors tile_inflight terrain_inflight terrain_active tiles_cached hm_cached build_recent_count build_recent_avg_ms build_recent_max_ms imagery_failures far_pool_inflight road_pool_inflight block_pool_inflight pool_respawns desired shown sent backlog stream_inflight stream_queued swap_groups terrain_wait prefetch_chain held_slots tasks send_buffer_bytes road_done road_wanted tree_done tree_wanted net_requests net_bytes net_errors cache_bytes cache_files client_age_s client_messages".split())
CONTEXT_FIELDS = CONTEXT_FIELDS | {"queues_age_s"}
TIMER_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,47}\Z")
SESSION_FILE = re.compile(r"capture-\d{8}T\d{6}-[0-9a-f]{12}\.jsonl\Z")
REASONS = frozenset(("user", "disconnect", "shutdown", "size_limit", "write_error"))


def _number(value):
    return type(value) in (int, float) and abs(value) <= 1e15 and math.isfinite(value)


def _fields(value, allowed):
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in allowed if key in value
            and (isinstance(value[key], bool) or _number(value[key]))}


def _timers(value):
    if not isinstance(value, dict):
        return {}
    out = {}
    for key, val in islice(value.items(), 64):
        if len(out) >= 32:
            break
        if isinstance(key, str) and TIMER_KEY.fullmatch(key) and _number(val) and val >= 0:
            out[key] = val
    return out


def _context(value, include_location):
    out = _fields(value, CONTEXT_FIELDS)
    if include_location and isinstance(value, dict):
        for key, limit in (("lat", 90), ("lon", 180)):
            val = value.get(key)
            if _number(val) and abs(val) <= limit:
                out[key] = val
    return out


def clean_client(value, include_location=False):
    """Copy only supported telemetry; refuse malformed frame counters/histograms."""
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != SCHEMA:
        return None
    frame = _fields(value.get("frame"), FRAME_FIELDS)
    if not frame or type(frame.get("frames")) is not int or not 0 <= frame["frames"] <= 1_000_000:
        return None
    if not _number(frame.get("wall_ms")) or frame["wall_ms"] <= 0:
        return None
    if any(type(n) is bool or n < 0 for n in frame.values()):
        return None
    histogram = value.get("frame", {}).get("histogram")
    if (not isinstance(histogram, list) or len(histogram) != len(HISTOGRAM_MS)
            or any(type(n) is not int or n < 0 for n in histogram) or sum(histogram) != frame["frames"]):
        return None
    frame["histogram"] = list(histogram)
    out = {"schema": SCHEMA, "frame": frame, "context": _context(value.get("context"), include_location),
           "mod_ms": _timers(value.get("mod_ms")), "engine_ms": _timers(value.get("engine_ms"))}
    for key in ("seq", "elapsed_s"):
        if _number(value.get(key)) and value[key] >= 0:
            out[key] = value[key]
    worst = value.get("worst")
    if isinstance(worst, dict) and _number(worst.get("ms")) and worst["ms"] >= 0:
        out["worst"] = {"ms": worst["ms"], "mod_ms": _timers(worst.get("mod_ms")),
                        "engine_ms": _timers(worst.get("engine_ms")),
                        "context": _context(worst.get("context"), include_location)}
    return out


def clean_server(value):
    return _fields(value, SERVER_FIELDS)


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class CaptureManager:
    """Bounded queue and files. start/stop/sample/status never touch disk.

    stop() is asynchronous; status is 'stopping' until queued records are flushed.
    close() is for a shutdown worker thread, and waits at most five seconds.
    Disk errors disable capture only. No raw exception strings enter UI or files.
    """
    def __init__(self, root, version, *, max_bytes=64 * 1024 * 1024, max_sessions=8,
                 queue_size=128, resource_factory=None):
        self.root = Path(root)
        self.version = version if isinstance(version, str) and re.fullmatch(r"[A-Za-z0-9.+_-]{1,80}", version) else "unknown"
        self.max_bytes = max(4096, int(max_bytes))
        self.max_sessions = max(1, min(32, int(max_sessions)))
        self.queue_size = max(2, min(1024, int(queue_size)))
        self._condition = threading.Condition()
        self._queue = deque()
        self._closed = False
        self._state = {"available": True, "active": False, "session": "", "samples": 0, "dropped": 0,
                       "bytes": 0, "error": "", "status": "idle", "include_location": False,
                       "write_ms": 0.0, "processing_ms": 0.0}
        self._t0 = 0.0
        self._resource_factory = resource_factory
        self._thread = threading.Thread(target=self._run, name="earth-telemetry", daemon=True)
        self._thread.start()

    def status(self):
        with self._condition:
            return dict(self._state, queue_depth=len(self._queue))

    def start(self, include_location=False):
        with self._condition:
            if self._closed or self._state["active"] or self._state["status"] == "stopping":
                return dict(self._state)
            session = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-") + uuid.uuid4().hex[:12]
            self._t0 = time.monotonic()
            self._state.update(active=True, session=session, samples=0, dropped=0, bytes=0, error="",
                               status="starting", include_location=include_location is True,
                               write_ms=0.0, processing_ms=0.0)
            self._queue.clear()
            self._queue.append({"kind": "start", "schema": SCHEMA, "session": session, "version": self.version,
                                "utc": _utc(), "elapsed_s": 0.0, "include_location": include_location is True,
                                "histogram_upper_ms": HISTOGRAM_MS,
                                "limits": {"session_bytes": self.max_bytes, "sessions": self.max_sessions}})
            self._condition.notify()
            return dict(self._state)

    def _record(self, kind):
        return {"kind": kind, "elapsed_s": round(time.monotonic() - self._t0, 6), "utc": _utc()}

    def _enqueue(self, record):
        if len(self._queue) >= self.queue_size:
            self._state["dropped"] += 1
            return False
        self._queue.append(record)
        self._condition.notify()
        return True

    def sample(self, client=None, server=None):
        begin = time.perf_counter()
        with self._condition:
            if not self._state["active"] or self._closed:
                return False
            record = self._record("sample" if client is not None else "server")
            if client is not None:
                record["client"] = clean_client(client, self._state["include_location"])
                if record["client"] is None:
                    self._state["dropped"] += 1
                    return False
            if server is not None:
                record["server"] = clean_server(server)
            accepted = self._enqueue(record)
            self._state["processing_ms"] += (time.perf_counter() - begin) * 1000
            return accepted

    def marker(self):
        with self._condition:
            return bool(self._state["active"] and self._enqueue(self._record("marker")))

    def stop(self, reason="user"):
        with self._condition:
            if self._state["active"]:
                self._state.update(active=False, status="stopping")
                # Control records have one reserved slot, so a full data queue cannot lose Stop.
                self._queue.append(dict(self._record("end"), reason=reason if reason in REASONS else "user"))
                self._condition.notify()
            return dict(self._state)

    def close(self):
        self.stop("shutdown")
        with self._condition:
            self._closed = True
            self._state["available"] = False
            self._condition.notify()
        self._thread.join(timeout=5)

    def _retain(self):
        self.root.mkdir(parents=True, exist_ok=True)
        files = [p for p in self.root.iterdir() if SESSION_FILE.fullmatch(p.name) and p.is_file() and not p.is_symlink()]
        files.sort(key=lambda p: (p.stat().st_mtime_ns, p.name))
        # Reserve a full new session. Also recover oversized files left by older versions.
        total = sum(p.stat().st_size for p in files)
        limit = self.max_bytes * (self.max_sessions - 1)
        while files and (len(files) >= self.max_sessions or total > limit):
            path = files.pop(0)
            size = path.stat().st_size
            path.unlink()
            total -= size

    def _run(self):
        stream = sampler = None
        last_resources = 0.0
        try:
            while True:
                with self._condition:
                    if not self._queue and not self._closed:
                        self._condition.wait(timeout=0.25 if stream else None)
                    record = self._queue.popleft() if self._queue else None
                    if record is None and self._closed:
                        break
                    active = self._state["active"]
                try:
                    if record and record["kind"] == "start":
                        self._retain()
                        stream = (self.root / ("capture-" + record["session"] + ".jsonl")).open("xb")
                        with self._condition:
                            if self._state["active"]:
                                self._state["status"] = "recording"
                        try:
                            if self._resource_factory is None:
                                from telemetry_resources import ResourceSampler
                                sampler = ResourceSampler()
                            else:
                                sampler = self._resource_factory()
                        except Exception:
                            sampler = None
                        last_resources = 0.0
                    if stream and record:
                        if record["kind"] == "end":
                            self._finish(stream, record)
                            stream = None
                            if sampler:
                                sampler.close()
                                sampler = None
                        elif not self._write(stream, record):
                            self._finish(stream, dict(self._record("end"), reason="size_limit"))
                            stream = None
                            if sampler:
                                sampler.close()
                                sampler = None
                    if stream and active and time.monotonic() - last_resources >= 1.0:
                        last_resources = time.monotonic()
                        try:
                            resources = sampler.sample() if sampler else {"available": False}
                        except Exception:
                            resources = {"available": False}
                        if not self._write(stream, dict(self._record("resources"), resources=resources)):
                            self._finish(stream, dict(self._record("end"), reason="size_limit"))
                            stream = None
                            if sampler:
                                sampler.close()
                                sampler = None
                except Exception:
                    if stream:
                        try:
                            stream.close()
                        except OSError:
                            pass
                    stream = None
                    if sampler:
                        try:
                            sampler.close()
                        except Exception:
                            pass
                    sampler = None
                    with self._condition:
                        self._queue.clear()
                        self._state.update(active=False, status="error", error="write_failed")
        finally:
            if stream:
                stream.close()
            if sampler:
                sampler.close()

    def _write(self, stream, record, *, final=False):
        begin = time.perf_counter()
        payload = (json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        with self._condition:
            if not final and self._state["bytes"] + len(payload) > self.max_bytes - 1024:
                return False
        stream.write(payload)
        stream.flush()  # ordinary flush, no per-frame/fsync stall; preserves data if the game crashes
        with self._condition:
            self._state["bytes"] += len(payload)
            self._state["write_ms"] += (time.perf_counter() - begin) * 1000
            if record["kind"] == "sample":
                self._state["samples"] += 1
        return True

    def _finish(self, stream, record):
        with self._condition:
            self._state.update(active=False, status="stopping")
            if record["reason"] == "size_limit":
                # The triggering record and pending data no longer fit. Record that loss
                # in the footer before clearing the queue, rather than silently discarding it.
                self._state["dropped"] += 1 + sum(r["kind"] not in ("start", "end") for r in self._queue)
            record["capture"] = {key: self._state[key] for key in ("samples", "dropped", "bytes", "write_ms", "processing_ms")}
        self._write(stream, record, final=True)
        stream.close()
        with self._condition:
            self._queue.clear()
            self._state["status"] = record["reason"]
