import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import assistplans
import plancache
import xproc


class PlanIntegrationTest(unittest.TestCase):
    def test_valid_remote_plan_is_shared_and_lock_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = plancache.PlanCache(tmp, "w0s0")
            c.remote = mock.Mock()
            c.remote.fetch.return_value = ([], [], [], [])
            key = (14, 2, 3)
            self.assertEqual(c.claim(key), (([], [], [], []), False))
            self.assertFalse(os.path.exists(c._path(key) + ".lock"))
            other = plancache.PlanCache(tmp, "w0s0")
            self.assertEqual(other.load(key), ([], [], [], []))
            self.assertEqual(c.claim(key), (([], [], [], []), False))
            c.remote.fetch.assert_called_once()

    def test_outage_keeps_local_generation_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = plancache.PlanCache(tmp, "w0s0")
            c.remote = mock.Mock()
            c.remote.fetch.return_value = None
            key = (14, 2, 3)
            self.assertEqual(c.claim(key), (None, True))
            self.assertTrue(os.path.exists(c._path(key) + ".lock"))
            c.release(key)

    def test_prefetch_respects_active_local_planner(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = plancache.PlanCache(tmp, "w0s0")
            key = (14, 2, 3)
            lock = c._path(key) + ".lock"
            self.assertTrue(xproc.take(lock))
            remote = mock.Mock(cache=c, stats={"prefetched": 0})
            remote.fetch.return_value = ([], [], [], [])
            worker = assistplans.Prefetcher()
            self.addCleanup(worker.close)
            worker._run(remote, key, (c.dir, key))
            self.assertIsNone(c.load(key))
            self.assertTrue(os.path.exists(lock))
            xproc.release(lock)
            worker._run(remote, key, (c.dir, key))
            self.assertEqual(c.load(key), ([], [], [], []))
            self.assertEqual(remote.stats["prefetched"], 1)

    def test_prefetch_queue_bounded_and_close_stops_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            entered, release = threading.Event(), threading.Event()
            c = plancache.PlanCache(tmp, "w0s0")
            remote = mock.Mock(cache=c, stats={"prefetched": 0})
            def fetch(*args):
                entered.set(); release.wait(3)
                return None
            remote.fetch.side_effect = fetch
            worker = assistplans.Prefetcher()
            self.addCleanup(worker.close)
            try:
                worker.submit(remote, [(14, i, 3) for i in range(100)])
                self.assertTrue(entered.wait(2))
                self.assertLessEqual(len(worker.pending), assistplans.MAX_PENDING)
                worker.close()
                before = len(worker.pending)
                worker.submit(remote, [(14, i, 4) for i in range(100)])
                self.assertLessEqual(len(worker.pending), before)
            finally:
                release.set()


if __name__ == "__main__":
    unittest.main()
