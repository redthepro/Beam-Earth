"""Actual native geometry and road-plan round trips at the assistance boundary."""
import copy
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import assistjobs
import farfield
import geodesy
import grading
import mapcells
import peerwork
import plancache
import worldbundle
from test_grading import FakeMD
from test_roads import features, FRAME, LAT, LON

SETTINGS = dict(roads=True, grading=True, road_widths=False, airports=False, sea=False, roads_in_ground=False)
SOURCES = dict(pipeline=2, overture="fixture", worldcover="fixture", canopy="fixture",
               rails=None, lanes=None, gradesep=None)
CONTEXT = dict(sources=SOURCES, airport=None)


def request(op="far_global_geometry"):
    if op == "plan_cell":
        return dict(op=op, settings=SETTINGS, context=CONTEXT, cell=list(mapcells.cell_of(LAT, LON)))
    return dict(op=op, settings=SETTINGS, context=CONTEXT, cell=[10, 511, 340],
                zoom=farfield.global_dem_zoom(10, 32), ref_zoom=farfield.global_dem_zoom(11, 32),
                layout=dict(cells=32, block=512, mesh=True, tex=[256]))


class FlatDEM:
    def sample(self, lat, lon, zoom, **kw):
        return np.zeros(np.broadcast_shapes(np.shape(lat), np.shape(lon))) + 30.0


class JobsTest(unittest.TestCase):
    def test_native_global_geometry_round_trip(self):
        r = request()
        b = farfield.build_global(FlatDEM(), *r["cell"], r["zoom"], r["ref_zoom"], assistjobs._layout(r["layout"]))
        meta, blobs = peerwork.built_msg(b)
        head = dict(ok=True, complete=True, request=assistjobs.request_id(r), identity=assistjobs.identity(),
                    context=r["context"], built=meta)
        got = assistjobs.decode_result(r["op"], head, blobs, r)
        for name in peerwork.BUILT_ARRAYS:
            if getattr(b, name) is not None:
                np.testing.assert_array_equal(getattr(got, name), getattr(b, name))

    def test_requests_are_canonical_and_fixed_settings(self):
        self.assertEqual(assistjobs.validate_request(request(), SETTINGS), request())
        for change in ({"unknown": 1}, {"cell": [30, 0, 0]}, {"cell": [10, -1, 0]},
                       {"zoom": True}, {"settings": {**SETTINGS, "sea": True}},
                       {"context": {"sources": {}, "airport": None}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                assistjobs.validate_request({**request(), **change}, SETTINGS)

    def test_collision_near_geometry_rejected(self):
        args = (1, LAT, LON, farfield.Tile(0, 0, 0), 14, 1/64, None, True, farfield.Layout())
        r = peerwork.job_header("far_geometry", args, SETTINGS)
        r["context"] = CONTEXT
        with self.assertRaisesRegex(ValueError, "collision"):
            assistjobs.validate_request(r, SETTINGS)

    def test_bad_geometry_rejected_before_native_use(self):
        r = request()
        b = farfield.build_global(FlatDEM(), *r["cell"], r["zoom"], r["ref_zoom"], assistjobs._layout(r["layout"]))
        for field, value in (("positions", np.full_like(b.positions, np.nan)),
                             ("tris", np.full_like(b.tris, len(b.positions))),
                             ("normals", np.ones_like(b.normals) * 3),
                             ("uvs", np.ones((4, 3))),
                             ("grid", np.ones((3, 3)))):
            with self.subTest(field=field):
                bad = copy.copy(b)
                setattr(bad, field, value)
                meta, blobs = peerwork.built_msg(bad)
                with self.assertRaises(ValueError):
                    assistjobs.validate_built(meta, blobs, r)

    def test_anchor_identity_changes_and_is_bounded(self):
        with mock.patch.object(assistjobs, "_anchors", assistjobs.OrderedDict()), \
                mock.patch.object(assistjobs, "_next_anchor", 1):
            a = assistjobs._aid(1, 2)
            self.assertEqual(a, assistjobs._aid(1, 2))
            self.assertNotEqual(a, assistjobs._aid(1, 3))
            for n in range(20):
                assistjobs._aid(n, 2)
            self.assertLessEqual(len(assistjobs._anchors), 2)

    def test_mismatched_identity_and_request_rejected(self):
        r = request("plan_cell")
        base = dict(ok=True, complete=True, identity=assistjobs.identity(), request=assistjobs.request_id(r), context=CONTEXT)
        for field, value in (("identity", {}), ("request", "0"*64), ("context", {}), ("complete", False)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                assistjobs.decode_result("plan_cell", {**base, field: value}, [], r)

    def test_plan_integrity_metadata_is_mandatory(self):
        r = request("plan_cell")
        head = dict(ok=True, complete=True, identity=assistjobs.identity(), request=assistjobs.request_id(r), context=CONTEXT)
        with self.assertRaisesRegex(ValueError, "integrity"):
            assistjobs.decode_result("plan_cell", head, [b"anything"], r)

    def test_global_oversampling_rejected(self):
        r = request()
        r.update(cell=[0, 0, 0], zoom=16, ref_zoom=16)
        with self.assertRaisesRegex(ValueError, "sampling"):
            assistjobs.validate_request(r, SETTINGS)


class RealPlanTest(unittest.TestCase):
    def test_server_plan_shared_across_anchor_frames(self):
        import blockworker
        import revisit
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {plancache.RUN_ENV: "assist-test"}), \
                mock.patch.object(assistjobs, "_cache_dir", tmp), mock.patch.object(assistjobs, "_settings", SETTINGS), \
                mock.patch.object(assistjobs, "_tainted", False), \
                mock.patch.object(assistjobs, "source_context", return_value=CONTEXT), \
                mock.patch.object(revisit, "ON", False):
            md = FakeMD(features([[(-300, 20), (300, 20)], [(0, -300), (0, 300)]],
                                 ["residential", "secondary"]))
            raw = lambda la, lo: np.full(np.shape(la), 10.0)
            store = grading.PlanStore(md, raw)
            g = grading.Grader(FRAME, md, raw, store=store)
            g.shared = plancache.PlanCache(tmp, "w0s0")
            g.shared.source_versions = lambda: SOURCES
            state = SimpleNamespace(anchor=lambda *args: (g, None))
            r = request("plan_cell")
            with mock.patch.object(blockworker, "_s", state):
                head, blobs = assistjobs.run(r)
            self.assertTrue(head.get("ok"), head)
            decoded = assistjobs.decode_result("plan_cell", head, blobs, r)
            self.assertGreater(len(decoded[0]), 0)
            # A second client's cache consumes the validated arrays in the same
            # geographic planning frame without re-querying the fixture source.
            pc = plancache.PlanCache(str(Path(tmp)/"client"), "w0s0")
            self.assertTrue(pc.store(r["cell"], *decoded))
            md2 = FakeMD(None)
            frame2 = geodesy.EnuFrame(LAT + .01, LON + .01)
            client_store = grading.PlanStore(md2, raw)
            client = grading.Grader(frame2, md2, raw, store=client_store)
            client.shared = pc
            planner = client.store.planner_of_cell(mapcells.CellKey(*r["cell"]))
            actual = planner._cell_plans(mapcells.CellKey(*r["cell"]))
            self.assertEqual(md2.calls, 0)
            self.assertEqual(len(actual), len(decoded[0]))
            for (fid, a), (expected_fid, b) in zip(actual, decoded[0]):
                self.assertEqual(fid, expected_fid)
                np.testing.assert_array_equal(a.xy, b.xy)
                np.testing.assert_array_equal(a.z, b.z)
            bad = bytearray(blobs[0]); bad[-1] ^= 1
            with self.assertRaises(ValueError):
                assistjobs.decode_result("plan_cell", head, [bytes(bad)], r)

    def test_source_mismatch_never_runs_generator(self):
        import blockworker
        with mock.patch.object(assistjobs, "_cache_dir", "unused"), \
                mock.patch.object(assistjobs, "_tainted", False), \
                mock.patch.object(assistjobs, "_settings", SETTINGS), \
                mock.patch.object(assistjobs, "source_context", return_value={}), \
                mock.patch.object(blockworker, "far_global_geometry") as build:
            head, blobs = assistjobs.run(request())
        self.assertEqual(head, {"ok": False, "error": "source_mismatch"})
        self.assertEqual(blobs, [])
        build.assert_not_called()

    def test_incomplete_result_resets_dependencies_before_retry(self):
        with mock.patch.object(assistjobs, "_tainted", False), \
                mock.patch.object(assistjobs, "_reset_state") as reset, \
                mock.patch.object(assistjobs, "_run", side_effect=[
                    ({"ok": False, "error": "incomplete_inputs"}, []), ({"ok": True}, [])]):
            assistjobs.run(request())
            self.assertTrue(assistjobs._tainted)
            assistjobs.run(request())
            reset.assert_called_once()

    def test_source_mismatches_preserve_warm_worker_state(self):
        import blockworker
        with mock.patch.object(assistjobs, "_tainted", False), \
                mock.patch.object(assistjobs, "_settings", SETTINGS), \
                mock.patch.object(assistjobs, "source_context", return_value={}), \
                mock.patch.object(assistjobs, "_reset_state") as reset, \
                mock.patch.object(blockworker, "far_global_geometry") as build:
            for _ in range(3):
                self.assertEqual(assistjobs.run(request()), ({"ok": False, "error": "source_mismatch"}, []))
                self.assertFalse(assistjobs._tainted)
            reset.assert_not_called()
            build.assert_not_called()

    def test_generation_exception_resets_dependencies_before_retry(self):
        with mock.patch.object(assistjobs, "_tainted", False), \
                mock.patch.object(assistjobs, "_reset_state") as reset, \
                mock.patch.object(assistjobs, "_run", side_effect=[ValueError("incomplete generation"),
                                                               ({"ok": True}, [])]):
            with self.assertRaises(ValueError):
                assistjobs.run(request())
            self.assertTrue(assistjobs._tainted)
            assistjobs.run(request())
            reset.assert_called_once()
            self.assertFalse(assistjobs._tainted)

    def test_helper_rebuilds_unmarked_plan_after_source_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = plancache.PlanCache(tmp, "w0s0")
            c.require_complete = True
            c.source_versions = lambda: SOURCES
            key = (14, 2, 3)
            self.assertTrue(c.store(key, [], [], [], [], persist=False))
            self.assertIsNone(c.load(key), "incomplete plan cannot be reused")
            state, mine = c.claim(key)
            self.assertTrue(mine)
            self.assertIsNone(state)
            try:
                self.assertTrue(c.store(key, [], [], [], [], persist=True))
            finally:
                c.release(key)
            self.assertEqual(c.load(key), ([], [], [], []))


if __name__ == "__main__":
    unittest.main()
