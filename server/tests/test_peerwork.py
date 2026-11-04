"""peerwork.py: compute sharing's wire format (no pickle), the job checks, the Built round trip, and a requester using
a helper (and building locally when the helper is gone or of another code)."""
import os
import socket
import subprocess
import tempfile
from unittest import mock
import sys
import threading
import types
import unittest
from concurrent.futures import Future
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import farfield  # noqa: E402
import peerwork  # noqa: E402


def built(seed=1):
    rng = np.random.default_rng(seed)
    return farfield.Built(pos=[1.0, 2.0, 3.0], positions=rng.random((9, 3)), normals=rng.random((9, 3)),
                          uvs=rng.random((9, 2)), planar=rng.random((9, 2)), tris=rng.integers(0, 9, (8, 3)),
                          grid=rng.random((3, 3)).astype(np.float32), err=0.5, sink=None, skirt=2.0, size=512.0)


def header(**over):
    h = {"op": "far_geometry", "lat": 39.28, "lon": -76.59, "tile": [1, -2, 3], "zoom": 13, "q": None, "ref_zoom": 14,
         "with_sink": True, "layout": {"cells": 64, "block": 1024, "mesh": True, "tex": None},
         "settings": {"roads": True, "grading": True}}
    h.update(over)
    return h


class WireTest(unittest.TestCase):
    def test_frames_round_trip(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        peerwork.send_msg(a, {"op": "x", "n": 3}, [b"abc", b""])
        h, blobs = peerwork.recv_msg(b)
        self.assertEqual((h, blobs), ({"op": "x", "n": 3}, [b"abc", b""]))
        a.sendall(b"EVIL" + b"\0" * 10)
        with self.assertRaises(ValueError):
            peerwork.recv_msg(b)

    def test_built_round_trip_without_pickle(self):
        b = built()
        d, blobs = peerwork.built_msg(b)
        self.assertTrue(all(blob.startswith(b"\x93NUMPY") for blob in blobs))
        c = peerwork.built_from(d, blobs)
        for n in peerwork.BUILT_ARRAYS:
            np.testing.assert_array_equal(getattr(b, n), getattr(c, n))
        self.assertEqual((c.pos, c.err, c.sink, c.skirt, c.size), (b.pos, b.err, b.sink, b.skirt, b.size))
        obj = np.array([{"a": 1}], dtype=object)       # an object array is refused, never unpickled
        with self.assertRaises(ValueError):
            peerwork._from_npy(peerwork._npy(obj))


class CheckTest(unittest.TestCase):
    def test_a_good_job(self):
        op, settings, kw = peerwork.check_job(header())
        self.assertEqual(op, "far_geometry")
        self.assertEqual(kw["tile"].key, "1_-2_3")
        self.assertEqual(settings, (True, True, False, False, False, True))

    def test_bad_jobs_refused(self):
        for bad in (header(op="os.system"), header(lat=91.0), header(lon=float("nan")), header(tile=[1, 2]),
                    header(zoom=40), header(q=-1.0), header(layout={"cells": 7, "block": 1024}),
                    header(lat="39"), header(op="far_tile", tex={"px": 256, "chain": ["http://evil"]})):
            with self.assertRaises(ValueError, msg=str(bad)[:80]):
                peerwork.check_job(bad)


class PinnedHelperTest(unittest.TestCase):
    def test_pinned_helper_rejected_before_resources(self):
        import pools
        with mock.patch.dict(os.environ):
            os.environ.pop("BNGE_WORLD_BUNDLE", None)
            helper = peerwork.Helper("unused-cache", 1, "unused-out")
            os.environ["BNGE_WORLD_BUNDLE"] = "must-not-be-opened"
            with mock.patch.object(peerwork.threading, "Lock") as lock, \
                    mock.patch.object(peerwork.os, "makedirs") as makedirs, \
                    mock.patch.object(peerwork.socketserver, "ThreadingTCPServer") as server, \
                    mock.patch.object(pools, "RespawningPool") as pool:
                for start in (lambda: peerwork.Helper("unused-cache", 1, "unused-out"),
                              lambda: peerwork.main(["serve", "--cache-dir", "unused-cache"]),
                              lambda: helper.pool((True, True, False, False, False, True)),
                              lambda: helper.serve("127.0.0.1", 0), helper.hello):
                    with self.assertRaisesRegex(ValueError, "do not negotiate BNGE_WORLD_BUNDLE"):
                        start()
                lock.assert_not_called()
                makedirs.assert_not_called()
                server.assert_not_called()
                pool.assert_not_called()

    def test_pinned_helper_cli_exits_without_creating_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "unused-cache"
            env = dict(os.environ, BNGE_WORLD_BUNDLE="must-not-be-opened")
            result = subprocess.run([sys.executable, str(Path(peerwork.__file__)), "serve", "--host", "127.0.0.1",
                                     "--port", "0", "--cache-dir", str(cache)], env=env,
                                    capture_output=True, text=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("do not negotiate BNGE_WORLD_BUNDLE", result.stderr)
            self.assertFalse(cache.exists())


class TextureSizeTest(unittest.TestCase):
    def test_one_key_for_every_texture_size(self):
        h = header(op="far_tile", tex={"px": 512, "chain": ["esri"], "tone": 1.0, "leaf_on": True,
                                        "season_mode": "auto", "mask_px": 256})
        h2 = dict(h, layout=dict(h["layout"], tex=[256, 256]), tex=dict(h["tex"], px=256, mask_px=128))
        self.assertEqual(peerwork.job_key(h), peerwork.job_key(h2))
        self.assertNotEqual(peerwork.job_key(h), peerwork.job_key(dict(h, tile=[1, -2, 4])))

    def test_shrink_drops_top_mips(self):
        import imagery
        rgb = np.random.default_rng(3).integers(0, 255, (512, 512, 3), dtype=np.uint8)
        big = imagery.encode_dds(rgb)
        w, hgt, mips, _, sizes = imagery.read_dds_info(big)
        small = peerwork.dds_shrink(big, 256)
        w2, h2, mips2, fourcc, sizes2 = imagery.read_dds_info(small)
        self.assertEqual((w2, h2, mips2), (256, 256, mips - 1))
        self.assertEqual(len(small), 128 + sum(sizes2))
        self.assertEqual(small[128:], big[128 + sizes[0]:], "the 512's own mip levels, not re-encoded")
        self.assertIs(peerwork.dds_shrink(big, 512), big)

    def test_fits(self):
        head = {"ok": True, "texture": {"blob": 0}, "px": 512, "mask_px": 256}
        self.assertTrue(peerwork.fits(head, (256, 128)))
        self.assertFalse(peerwork.fits(dict(head, px=256), (512, 0)))
        self.assertTrue(peerwork.fits({"ok": True, "texture": None}, (512, 0)))


class LocalPool:
    def __init__(self):
        self.calls = 0

    def submit(self, fn, *args, **kw):
        self.calls += 1
        f = Future()
        f.set_result(("local", args[3].key))
        return f

    def shutdown(self, *args, **kw):
        pass


def far_geometry(*args):        # (the job's name is what the pool routes by)
    raise AssertionError("not called here")


class FakeHelper:
    """A helper on a local port answering every job with a fixed Built (or refusing another code)."""

    def __init__(self, code=None, hold_jobs=False):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen()
        self.srv.settimeout(.1)
        self.closed = threading.Event()
        self.connections = set()
        self.lock = threading.Lock()
        self.port = self.srv.getsockname()[1]
        self.code = code or peerwork.code_id()
        self.jobs = []
        self.release_jobs = threading.Event()
        if not hold_jobs:
            self.release_jobs.set()
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while not self.closed.is_set():
            try:
                s, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self.lock:
                self.connections.add(s)
            threading.Thread(target=self.conn, args=(s,), daemon=True).start()

    def conn(self, s):
        try:
            h, _ = peerwork.recv_msg(s)
            peerwork.send_msg(s, {"ok": h.get("code") == self.code, "workers": 2, "code": self.code})
            while True:
                h, _ = peerwork.recv_msg(s)
                self.jobs.append(h["tile"])
                if not self.release_jobs.wait(10):
                    return
                d, blobs = peerwork.built_msg(built(len(self.jobs)))
                peerwork.send_msg(s, {"ok": True, "built": d}, blobs)
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            with self.lock:
                self.connections.discard(s)
            s.close()

    def close(self):
        self.closed.set()
        self.release_jobs.set()
        self.srv.close()
        with self.lock:
            sockets = tuple(self.connections)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()


class PoolTest(unittest.TestCase):
    def args(self, i):
        return (1, 39.28, -76.59, farfield.Tile(1, i, 0), 13, None, 14, True, farfield.Layout(64))

    def test_helper_used_then_local(self):
        helper, local = FakeHelper(hold_jobs=True), LocalPool()
        self.addCleanup(helper.close)
        pool = peerwork.SharedFarPool(local, [("127.0.0.1", helper.port)], {"roads": True}, None)
        self.addCleanup(pool.shutdown)
        for _ in range(100):
            if pool.free.qsize() == 2:
                break
            threading.Event().wait(0.02)
        self.assertEqual(pool.remote_slots(), 2, "earthd keeps this many more far tiles in flight")
        try:
            futs = [pool.submit(far_geometry, *self.args(i)) for i in range(4)]
        finally:
            helper.release_jobs.set()
        out = [f.result(10) for f in futs]
        remote = [o for o in out if isinstance(o, farfield.Built)]
        self.assertEqual(len(remote), 2, "the helper's two slots")
        self.assertEqual(local.calls, 2, "the rest built here")
        self.assertEqual(len(helper.jobs), 2)

    def test_other_code_not_used(self):
        helper, local = FakeHelper(code="another-0"), LocalPool()
        self.addCleanup(helper.close)
        pool = peerwork.SharedFarPool(local, [("127.0.0.1", helper.port)], {}, None)
        self.addCleanup(pool.shutdown)
        threading.Event().wait(0.3)
        self.assertEqual((pool.free.qsize(), pool.remote_slots()), (0, 0))
        self.assertEqual(pool.submit(far_geometry, *self.args(0)).result(5), ("local", "1_0_0"))


class CountingLocal:
    """A local pool building far_geometry jobs (a Built per tile), counting what it built."""

    def __init__(self):
        self.built = []
        self.lock = threading.Lock()

    def submit(self, fn, *args, **kw):
        with self.lock:
            self.built.append(args[3].key)
        f = Future()
        f.set_result(built(args[3].i + 7))
        return f


@unittest.skip("shared world building, commented out 2026-10-27 (off since 2026-10-23; the user: not coming back for a while): GroupHelper")
class GroupTest(unittest.TestCase):
    def test_each_tile_built_once_in_the_group(self):
        import tempfile
        pools = []
        for name in ("a", "b"):
            local = CountingLocal()
            pool = peerwork.SharedFarPool(local, [], {"roads": True}, None,
                                          peerwork.ReplyCache(tempfile.mkdtemp(prefix="bnge_rc_")))
            helper = peerwork.GroupHelper(pool, lambda la, lo: 1, tempfile.mkdtemp(prefix="bnge_po_"))
            port = helper.start(0, 1)
            pools.append((name, local, pool, port))
        members = [(n, "127.0.0.1", port) for n, _, _, port in pools]
        for n, _, pool, _ in pools:
            pool.set_group(n, members)
        self.assertEqual(pools[0][2].remote_slots(), peerwork.GROUP_CONNS)
        args = [(1, 39.28, -76.59, farfield.Tile(1, i, 0), 13, None, 14, True, farfield.Layout(64)) for i in range(12)]
        futs = [pool.submit(far_geometry, *a) for _, _, pool, _ in pools for a in args]
        out = [f.result(20) for f in futs]
        self.assertTrue(all(isinstance(o, farfield.Built) for o in out))
        for i, a in enumerate(args):        # both players got the same tile
            np.testing.assert_array_equal(out[i].positions, out[len(args) + i].positions)
        built_a, built_b = pools[0][1].built, pools[1][1].built
        self.assertEqual(sorted(built_a + built_b), sorted(a[3].key for a in args), "each tile built once in all")
        self.assertTrue(built_a and built_b, "the work split between the two")
        self.assertEqual(pools[0][2].stats["fallback"] + pools[1][2].stats["fallback"], 0)

    def test_newcomer_gets_the_kept_tiles(self):
        import tempfile
        pools = []
        for name in ("old", "new"):
            local = CountingLocal()
            pool = peerwork.SharedFarPool(local, [], {"roads": True}, None,
                                          peerwork.ReplyCache(tempfile.mkdtemp(prefix="bnge_rc_")))
            pool.sharing = True
            port = peerwork.GroupHelper(pool, lambda la, lo: 1, tempfile.mkdtemp(prefix="bnge_po_")).start(0, 1)
            pools.append((name, local, pool, port))
        args = [(1, 39.28, -76.59, farfield.Tile(1, i, 0), 13, None, 14, True, farfield.Layout(64)) for i in range(10)]
        old, new = pools[0][2], pools[1][2]
        for a in args:                       # alone first: everything built and kept
            old.submit(far_geometry, *a).result(10)
        members = [(n, "127.0.0.1", port) for n, _, _, port in pools]
        old.set_group("old", members)
        new.set_group("new", members)
        out = [new.submit(far_geometry, *a).result(20) for a in args]
        self.assertTrue(all(isinstance(o, farfield.Built) for o in out))
        self.assertEqual(pools[1][1].built, [], "the newcomer built nothing: all from the player there before")
        self.assertEqual(len(pools[0][1].built), len(args))

    def test_alone_builds_everything(self):
        import tempfile
        local = CountingLocal()
        pool = peerwork.SharedFarPool(local, [], {"roads": True}, None,
                                      peerwork.ReplyCache(tempfile.mkdtemp(prefix="bnge_rc_")))
        pool.set_group("italy", [("italy", "127.0.0.1", 1)])
        self.assertIsNone(pool.group)
        args = (1, 45.0, 9.0, farfield.Tile(1, 0, 0), 13, None, 14, True, farfield.Layout(64))
        self.assertIsInstance(pool.submit(far_geometry, *args).result(5), farfield.Built)
        self.assertEqual(local.built, ["1_0_0"])

    def test_owner_gone_built_here(self):
        import tempfile
        local = CountingLocal()
        pool = peerwork.SharedFarPool(local, [], {"roads": True}, None,
                                      peerwork.ReplyCache(tempfile.mkdtemp(prefix="bnge_rc_")))
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
        s.close()
        pool.set_group("a", [("a", "127.0.0.1", 1), ("b", "127.0.0.1", dead)])
        args = [(1, 39.28, -76.59, farfield.Tile(1, i, 0), 13, None, 14, True, farfield.Layout(64)) for i in range(8)]
        out = [pool.submit(far_geometry, *a).result(20) for a in args]
        self.assertTrue(all(isinstance(o, farfield.Built) for o in out))
        self.assertEqual(sorted(local.built), sorted(a[3].key for a in args))


class SharedRenderingTest(unittest.TestCase):
    def test_missing_mode_keeps_terrain_default(self):
        _, settings, _ = peerwork.check_job(header())
        self.assertIs(settings[-1], True)

    def test_job_settings_include_rendering_mode(self):
        for mode in (False, True):
            with self.subTest(terrain=mode):
                _, settings, _ = peerwork.check_job(header(settings={"roads": True, "roads_in_ground": mode}))
                self.assertIn("roads_in_ground", peerwork.SETTINGS)
                self.assertEqual(settings[peerwork.SETTINGS.index("roads_in_ground")], mode)

    def test_job_cache_separates_modes(self):
        terrain = header(settings={"roads": True, "roads_in_ground": True})
        mesh = header(settings={"roads": True, "roads_in_ground": False})
        self.assertNotEqual(peerwork.job_key(terrain), peerwork.job_key(mesh))

    def test_group_refuses_other_rendering_before_building(self):
        for local_mode in (False, True):
            with self.subTest(terrain=local_mode):
                pool = peerwork.SharedFarPool(None, [], {"roads": True, "roads_in_ground": local_mode}, None)
                try:
                    h = header(settings={"roads": True, "roads_in_ground": not local_mode})
                    with self.assertRaisesRegex(ValueError, "other world settings"):
                        pool.serve(h, lambda *_: self.fail("must reject before resolving anchor"), None)
                finally:
                    pool.io.shutdown(wait=True)

    def test_helper_separates_pools_and_forwards_mode(self):
        import pools
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(pools, "RespawningPool") as factory:
            factory.side_effect = [object(), object()]
            helper = peerwork.Helper(tmp, 1, tmp)
            for mode in (False, True):
                _, settings, _ = peerwork.check_job(header(settings={"roads": True, "roads_in_ground": mode}))
                pool = helper.pool(settings)
                self.assertIs(helper.pool(settings), pool)
                self.assertIs(factory.call_args.kwargs["initargs"][-1], mode)
                self.assertIsNone(factory.call_args.kwargs["initargs"][6], "no warm queue: far tiles plan no roads ahead")
            self.assertEqual(factory.call_count, 2)

    def test_invalid_wire_mode_is_rejected(self):
        for bad in ("false", "mesh", 0, None, []):
            with self.subTest(mode=bad), self.assertRaises(ValueError):
                peerwork.check_job(header(settings={"roads_in_ground": bad}))

    def test_spawned_worker_uses_requester_mode_over_its_environment(self):
        import multiprocessing
        import os
        from concurrent.futures import ProcessPoolExecutor
        import blockworker
        ctx = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmp:
            for mode in (False, True):
                with self.subTest(terrain=mode), mock.patch.dict(os.environ, {
                    "LOCALAPPDATA": tmp, "XDG_CONFIG_HOME": tmp, "HOME": tmp, "USERPROFILE": tmp,
                    "BNGE_ROADS_IN_GROUND": "0" if mode else "1", "BNGE_SAMPLE": "", "BNGE_TRACEMALLOC": "",
                }):
                    warm = ctx.Queue(maxsize=1)
                    try:
                        with ProcessPoolExecutor(1, mp_context=ctx, initializer=blockworker.init,
                                                 initargs=(tmp, True, False, False, False, False, warm,
                                                           30, False, False, 0, mode)) as pool:
                            self.assertIs(pool.submit(worker_rendering_mode).result(30), mode)
                    finally:
                        warm.close()
                        warm.join_thread()


def worker_rendering_mode():
    import roads
    return roads.ROADS_IN_GROUND


class AssistanceTest(unittest.TestCase):
    """The modern global geometry path uses assistance; failed work remains playable locally."""

    class Local:
        def __init__(self):
            self.calls = []
            self.closed = False

        def submit(self, fn, *args, **kw):
            self.calls.append((fn.__name__, args, kw))
            result = Future()
            result.set_result("local geometry")
            return result

        def shutdown(self, *args, **kw):
            self.closed = True

    class Assistance:
        def __init__(self):
            self.gate = threading.BoundedSemaphore(1)
            self.closed = False
            self.calls = []
            self.error = False
            self.rejected = 0

        def try_acquire(self):
            return not self.closed and self.gate.acquire(blocking=False)

        def release(self):
            self.gate.release()

        def available_slots(self):
            return 0 if self.closed else 1

        def call(self, header, *, reserved=False):
            assert reserved
            try:
                self.calls.append(header)
                if self.error:
                    raise ConnectionError("remote down")
                return {"ok": True}, [b"geometry"]
            finally:
                self.release()

        def note_failure(self):
            self.rejected += 1

        def close(self):
            self.closed = True

    def setUp(self):
        self.local = self.Local()
        self.assist = self.Assistance()
        self.pool = peerwork.SharedFarPool(self.local, [], {"roads": True}, None,
                                           assistance=self.assist, assist_cache_dir="cache-dir")
        self.addCleanup(self.pool.shutdown)
        self.jobs = types.SimpleNamespace(make_request=mock.Mock(return_value={"op": "far_global_geometry", "context": {"source": "a"}}),
                                         source_context=mock.Mock(return_value={"source": "a"}),
                                         decode_result=mock.Mock(return_value="remote geometry"))
        self.module = mock.patch.dict(sys.modules, {"assistjobs": self.jobs})
        self.module.start()
        self.addCleanup(self.module.stop)

    @staticmethod
    def far_global_geometry(*args):
        raise AssertionError("runs in worker")

    @staticmethod
    def far_tile(*args):
        raise AssertionError("runs in worker")

    def test_global_geometry_offloaded_with_context(self):
        args = (10, 100, 200, 13, 14, farfield.Layout(64))
        self.assertEqual(self.pool.submit(self.far_global_geometry, *args).result(2), "remote geometry")
        self.jobs.make_request.assert_called_once_with("far_global_geometry", args, {"roads": True}, "cache-dir")
        self.jobs.decode_result.assert_called_once_with("far_global_geometry", {"ok": True}, [b"geometry"],
                                                        {"op": "far_global_geometry", "context": {"source": "a"}})
        self.assertEqual(self.local.calls, [])
        self.assertEqual(self.pool.stats["assistRemote"], 1)
        self.assertEqual(self.pool.remote_slots(), 1)

    def test_saturated_helper_does_not_queue(self):
        self.assertTrue(self.assist.try_acquire())
        try:
            self.assertEqual(self.pool.submit(self.far_global_geometry, 1).result(2), "local geometry")
            self.assertEqual(self.assist.calls, [])
        finally:
            self.assist.release()

    def test_failed_helper_falls_back_and_returns_slot(self):
        self.assist.error = True
        self.assertEqual(self.pool.submit(self.far_global_geometry, 1).result(2), "local geometry")
        self.assertEqual(self.pool.stats["assistFailed"], 1)
        self.assertEqual(len(self.local.calls), 1)
        self.assertTrue(self.assist.try_acquire())
        self.assist.release()

    def test_invalid_result_falls_back_and_backs_off(self):
        self.jobs.decode_result.side_effect = ValueError("invalid geometry")
        self.assertEqual(self.pool.submit(self.far_global_geometry, 1).result(2), "local geometry")
        self.assertEqual(self.assist.rejected, 1)

    def test_source_refresh_during_request_keeps_local_geometry(self):
        self.jobs.source_context.return_value = {"source": "b"}
        self.assertEqual(self.pool.submit(self.far_global_geometry, 1).result(2), "local geometry")
        self.jobs.decode_result.assert_not_called()
        self.assertEqual(self.assist.rejected, 1)

    def test_request_context_failure_returns_reservation(self):
        self.jobs.make_request.side_effect = ValueError("source context unavailable")
        self.assertEqual(self.pool.submit(self.far_global_geometry, 1).result(2), "local geometry")
        self.assertEqual(self.assist.calls, [])
        self.assertTrue(self.assist.try_acquire())
        self.assist.release()

    def test_imagery_stays_local(self):
        self.assertEqual(self.pool.submit(self.far_tile, 1).result(2), "local geometry")
        self.assertEqual(self.assist.calls, [])
        self.jobs.make_request.assert_not_called()

    def test_near_collision_geometry_stays_local(self):
        args = (1, 39.28, -76.59, farfield.Tile(0, 0, 0), 13, None, 14, True, farfield.Layout(64))
        self.assertEqual(self.pool.submit(far_geometry, *args).result(2), "local geometry")
        self.assertEqual(self.assist.calls, [])
        self.jobs.make_request.assert_not_called()

    def test_pending_falls_back_without_poisoning_helper(self):
        def pending(header, *, reserved=False):
            self.assist.release()
            return {"ok": False, "error": "pending"}, []
        self.assist.call = pending
        self.assertEqual(self.pool.submit(self.far_global_geometry, 1).result(2), "local geometry")
        self.assertEqual(self.assist.rejected, 0)
        self.assertEqual(self.pool.stats["assistFailed"], 0)
        self.assertEqual(self.pool.stats["assistDeferred"], 1)

    def test_shutdown_closes_assistance_and_refuses_new_jobs(self):
        self.pool.shutdown()
        self.assertTrue(self.assist.closed)
        self.assertTrue(self.local.closed)
        self.assertEqual(self.pool.remote_slots(), 0)
        with self.assertRaises(RuntimeError):
            self.pool.submit(self.far_global_geometry, 1)

    def test_cancelled_queued_task_returns_reservation(self):
        pending = Future()
        with mock.patch.object(self.pool.io, "submit", return_value=pending):
            future = self.pool.submit(self.far_global_geometry, 1)
        self.assertFalse(self.assist.try_acquire())
        self.assertTrue(future.cancel())
        self.assertTrue(self.assist.try_acquire())
        self.assist.release()

    def test_legacy_retry_timer_is_daemon_and_cancelled_on_shutdown(self):
        conn = mock.Mock(host="127.0.0.1", port=1)
        conn.call.side_effect = ConnectionError("gone")
        args = (1, 39.28, -76.59, farfield.Tile(1, 0, 0), 13, None, 14, True, farfield.Layout(64))
        self.pool.slots = 1
        self.pool._connections.add(conn)
        self.assertEqual(self.pool._remote(conn, far_geometry, "far_geometry", args), "local geometry")
        timers = tuple(self.pool._retry_timers)
        self.assertEqual(len(timers), 1)
        self.assertTrue(timers[0].daemon)
        self.pool.shutdown()
        self.assertTrue(timers[0].finished.is_set())
        self.assertEqual(self.pool._retry_timers, set())


if __name__ == "__main__":
    unittest.main()
