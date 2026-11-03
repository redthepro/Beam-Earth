"""Opt-in diagnostics on the local companion connection; never controls gameplay.

The event loop collects cheap counters once a second. CaptureManager owns disk and
OS resource sampling on a bounded writer thread. One connection owns a capture;
disconnect ends it, and reconnect requires an explicit new start.
"""
import asyncio
import math
import time


def snapshot(server, connection, lag_ms=0):
    start = time.perf_counter()
    out = {"loop_lag_ms": lag_ms}
    for field, attr in (("connections", "connections"), ("anchors", "anchors"),
                        ("tile_inflight", "tile_inflight"), ("terrain_inflight", "hm_inflight"),
                        ("tiles_cached", "tiles"), ("hm_cached", "hm_committed")):
        out[field] = len(getattr(server, attr, ()))
    out["terrain_active"] = getattr(server, "hm_here", 0)
    out["imagery_failures"] = getattr(server, "imagery_failures", 0)
    # Bounded history already maintained by earthd, in seconds. These are recent
    # completed tile timings, not latency percentiles for every desired tile.
    builds = tuple(getattr(server, "build_stats", ()))
    out["build_recent_count"] = len(builds)
    if builds:
        out["build_recent_avg_ms"] = 1000 * sum(builds) / len(builds)
        out["build_recent_max_ms"] = 1000 * max(builds)
    pools = [getattr(server, key, None) for key in ("far_pool", "road_pool")]
    pools[0] = getattr(pools[0], "local", pools[0])
    block_pools = [item[0] for item in getattr(server, "block_pools", ())]
    out["far_pool_inflight"] = getattr(pools[0], "inflight", 0)
    out["road_pool_inflight"] = getattr(pools[1], "inflight", 0)
    out["block_pool_inflight"] = sum(getattr(p, "inflight", 0) for p in block_pools)
    unique = {id(p): p for p in pools + list(block_pools) if p is not None}
    out["pool_respawns"] = sum(getattr(p, "respawns", 0) for p in unique.values())
    if connection is not None:
        c = connection
        for field, attr in (("desired", "desired"), ("shown", "shown"), ("sent", "sent"),
                            ("stream_inflight", "inflight"), ("swap_groups", "groups"),
                            ("prefetch_chain", "predicted_chain"), ("held_slots", "slot_holds"),
                            ("tasks", "tasks")):
            out[field] = len(getattr(c, attr, ()))
        out["backlog"] = c.backlog()
        out["stream_queued"] = max(0, len(c.queue) - c.qi)
        out["terrain_wait"] = c.terrain_wait
        transport = getattr(c.writer, "transport", None)
        if transport is not None:
            out["send_buffer_bytes"] = transport.get_write_buffer_size()
        for prefix, tracker in (("road", c.roads), ("tree", c.veg)):
            if tracker is not None:
                done, wanted = tracker.progress()
                out[prefix + "_done"], out[prefix + "_wanted"] = done, wanted
    net = getattr(server, "net", None)
    if net is not None:
        counters = net.snapshot().values()
        for field, key in (("net_requests", "requests"), ("net_bytes", "bytes"), ("net_errors", "errors")):
            out[field] = sum(v.get(key, 0) for v in counters)
    cache = getattr(server, "cache", None)
    if cache is not None:
        out["cache_bytes"], out["cache_files"] = cache.usage()
    out["snapshot_ms"] = (time.perf_counter() - start) * 1000
    return out


class Bridge:
    def __init__(self, server, capture, clock=time.monotonic):
        self.server, self.capture, self.clock = server, capture, clock
        self.owner = None
        self.last_sample = -math.inf
        self.last_packet = -math.inf
        self.client_messages = 0

    def status(self, c):
        state = dict(self.capture.status(), type="telemetry_status")
        if self.owner is not None and self.owner is not c:
            state.update(active=False, available=False, status="busy", error="another_capture_active")
        try:
            c.send(state)
            return True
        except Exception:
            return False

    def dispatch(self, c, msg):
        now = self.clock()
        if msg["type"] == "telemetry_sample":
            if c is self.owner and self.capture.status().get("active") and now - self.last_packet >= 0.5:
                self.last_packet = now
                if self.capture.sample(msg):
                    self.last_sample = now
                    self.client_messages += 1
            return
        action = msg.get("action")
        if action not in ("start", "stop", "marker", "status"):
            return
        # Stop is never throttled: the player must be able to stop immediately
        # after Start or a marker. Keep the observer until the writer finishes.
        if action == "stop" and c is self.owner:
            was_active = self.capture.status().get("active")
            self.capture.stop(reason="user")
            if was_active or now - getattr(c, "_telemetry_control_at", -math.inf) >= 0.5:
                c._telemetry_control_at = now
                self.status(c)
            return
        # No unbounded status replies or session creation from a malformed client.
        if now - getattr(c, "_telemetry_control_at", -math.inf) < 0.5:
            return
        c._telemetry_control_at = now
        if action == "start" and (self.owner is None or self.owner is c):
            if not self.capture.status().get("active"):
                state = self.capture.start(include_location=msg.get("include_location") is True)
                if state.get("active"):
                    self.owner = c
                    self.last_sample = -math.inf
                    self.last_packet = -math.inf
                    self.client_messages = 0
        elif c is self.owner:
            if action == "marker":
                self.capture.marker()
        self.status(c)

    def disconnected(self, c):
        if c is self.owner:
            self.capture.stop(reason="disconnect")
            self.owner = None

    async def run(self):
        due = self.clock() + 1
        while True:
            await asyncio.sleep(max(0, due - self.clock()))
            now = self.clock()
            lag = max(0, now - due) * 1000
            due = now + 1  # no catch-up burst after a stall
            c = self.owner
            if c is None:
                continue
            if self.capture.status().get("active"):
                try:
                    data = snapshot(self.server, c, lag)
                    if math.isfinite(self.last_sample):
                        data["client_age_s"] = max(0, now - self.last_sample)
                    data["client_messages"] = self.client_messages
                    self.capture.sample(None, data)
                except Exception:
                    # Diagnostics must never stop terrain work. A schema failure
                    # remains visible as a missing server sample in the report.
                    pass
            if not self.status(c):
                self.disconnected(c)
            state = self.capture.status()
            if not state.get("active") and state.get("status") != "stopping":
                self.owner = None

    def close(self):
        self.capture.close()
