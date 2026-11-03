"""Local capture ownership and diagnostic failure isolation, no geographic services."""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from telemetry_bridge import Bridge, snapshot


class Capture:
    def __init__(self):
        self.active = False
        self.records = []

    def status(self):
        return {"active": self.active, "available": True, "session": "test"}

    def start(self, **kw):
        self.active = True
        self.records.append(("start", kw))
        return self.status()

    def sample(self, *args):
        self.records.append(("sample", args))
        return True

    def stop(self, **kw):
        self.active = False
        self.records.append(("stop", kw))

    def marker(self):
        self.records.append(("marker",))


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.cap = Capture()
        self.bridge = Bridge(NS(), self.cap, clock=lambda: self.now)
        self.c = NS(send=Mock())
        self.other = NS(send=Mock())

    def control(self, c, action, **extra):
        self.now += 1
        self.bridge.dispatch(c, dict(type="telemetry_control", action=action, **extra))

    def test_owner_only_and_reconnect_is_opt_in(self):
        self.control(self.c, "start", include_location=True)
        self.control(self.other, "stop")
        self.control(self.other, "start")
        self.assertTrue(self.cap.active)
        self.assertFalse(self.other.send.call_args[0][0]["available"])
        self.bridge.dispatch(self.other, {"type": "telemetry_sample", "seq": 1})
        self.assertEqual(len(self.cap.records), 1)
        self.bridge.disconnected(self.other)
        self.assertTrue(self.cap.active)
        self.bridge.disconnected(self.c)
        self.assertFalse(self.cap.active)
        self.bridge.status(self.other)
        self.assertFalse(self.other.send.call_args[0][0]["active"])

    def test_location_consent_requires_boolean(self):
        self.control(self.c, "start", include_location="true")
        self.assertFalse(self.cap.records[0][1]["include_location"])

    def test_stop_immediately_after_start_is_not_throttled(self):
        self.control(self.c, "start")
        self.bridge.dispatch(self.c, {"type": "telemetry_control", "action": "stop"})
        self.assertFalse(self.cap.active)
        self.assertEqual(self.cap.records[-1], ("stop", {"reason": "user"}))
        self.c.send.reset_mock()
        for _ in range(100):
            self.bridge.dispatch(self.c, {"type": "telemetry_control", "action": "stop"})
        self.c.send.assert_not_called()

    def test_rejected_schema_is_not_counted_as_gameplay_sample(self):
        self.control(self.c, "start")
        self.cap.sample = Mock(return_value=False)
        self.bridge.dispatch(self.c, {"type": "telemetry_sample"})
        self.assertEqual(self.bridge.client_messages, 0)
        self.assertEqual(self.bridge.last_sample, -float("inf"))

    def test_status_write_failure_is_isolated(self):
        self.c.send.side_effect = ConnectionError
        self.assertFalse(self.bridge.status(self.c))

    def test_samples_are_rate_limited_and_no_auto_start(self):
        msg = {"type": "telemetry_sample", "seq": 1}
        self.bridge.dispatch(self.c, msg)
        self.assertFalse(self.cap.records)
        self.control(self.c, "start")
        for _ in range(100):
            self.bridge.dispatch(self.c, msg)
        self.assertEqual(self.bridge.client_messages, 1)
        self.now += 1
        self.bridge.dispatch(self.c, msg)
        self.assertEqual(self.bridge.client_messages, 2)
        self.control(self.c, "stop")
        self.bridge.dispatch(self.c, msg)
        self.assertEqual(self.bridge.client_messages, 2)

    def test_control_rate_limit_and_unknown_actions(self):
        self.bridge.dispatch(self.c, {"type": "telemetry_control", "action": "upload"})
        self.c.send.assert_not_called()
        self.control(self.c, "start")
        for _ in range(100):
            self.bridge.dispatch(self.c, {"type": "telemetry_control", "action": "start"})
        self.assertEqual(len(self.cap.records), 1)
        self.control(self.c, "marker")
        self.assertEqual(self.cap.records[-1], ("marker",))

    def test_snapshot_counters_have_no_identity_or_locations(self):
        pool = NS(inflight=3, respawns=1)
        s = NS(connections={1}, anchors={"private": object()}, tile_inflight={1: object()},
               build_stats=[0.1, 0.3], far_pool=NS(local=pool), road_pool=pool, block_pools=[(pool, object())],
               net=NS(snapshot=lambda: {"private.host": {"requests": 4, "bytes": 12, "errors": 1}}),
               cache=NS(usage=lambda: (1024, 2)))
        c = NS(backlog=lambda: 3, queue=[1, 2, 3], qi=1, terrain_wait=2,
               writer=NS(transport=NS(get_write_buffer_size=lambda: 23)),
               roads=NS(progress=lambda: (4, 5)), veg=None)
        d = snapshot(s, c, 27)
        self.assertEqual(d["pool_respawns"], 1)
        self.assertEqual(d["block_pool_inflight"], 3)
        self.assertEqual(d["far_pool_inflight"], 3)
        self.assertEqual(d["stream_queued"], 2)
        self.assertEqual(d["net_bytes"], 12)
        self.assertEqual(d["build_recent_avg_ms"], 200)
        self.assertEqual(d["loop_lag_ms"], 27)
        self.assertTrue(all(isinstance(v, (int, float)) for v in d.values()))
        self.assertNotIn("private", str(d))


class TickTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_status_is_delivered_before_releasing_owner(self):
        now = [0.0]
        cap = Capture()
        bridge = Bridge(NS(), cap, clock=lambda: now[0])
        c = NS(send=Mock())
        bridge.dispatch(c, {"type": "telemetry_control", "action": "start"})
        bridge.dispatch(c, {"type": "telemetry_control", "action": "stop"})

        async def sleep(delay):
            now[0] += delay
            if now[0] >= 2:
                raise asyncio.CancelledError

        with patch("telemetry_bridge.asyncio.sleep", sleep):
            with self.assertRaises(asyncio.CancelledError):
                await bridge.run()
        self.assertFalse(c.send.call_args[0][0]["active"])
        self.assertIsNone(bridge.owner)

    async def test_periodic_snapshot_failure_does_not_end_monitor(self):
        now = [0.0]
        cap = Capture()
        bridge = Bridge(NS(), cap, clock=lambda: now[0])
        c = NS(send=Mock())
        bridge.dispatch(c, {"type": "telemetry_control", "action": "start"})
        calls = []

        async def sleep(delay):
            calls.append(delay)
            now[0] += delay + 2  # a loop stall; never catch up with a sample burst
            if len(calls) > 2:
                raise asyncio.CancelledError

        with patch("telemetry_bridge.asyncio.sleep", sleep), patch("telemetry_bridge.snapshot", side_effect=ValueError):
            with self.assertRaises(asyncio.CancelledError):
                await bridge.run()
        self.assertEqual(calls, [1, 1, 1])
        self.assertEqual(c.send.call_count, 3)  # start plus both health ticks


if __name__ == "__main__":
    unittest.main()
