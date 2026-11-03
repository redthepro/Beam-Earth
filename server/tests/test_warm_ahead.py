"""Road/imagery warm-up stays near the player even when finite pose velocities are extreme."""
import math
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import earthd  # noqa: E402
import geodesy  # noqa: E402


class RecordingFrame:
    def __init__(self):
        self.frame = geodesy.EnuFrame(36.17, -115.145)
        self.points = []

    def enu_to_geodetic(self, x, y, z):
        self.points.append((x, y))
        return self.frame.enu_to_geodetic(x, y, z)


class RecordingQueue:
    def __init__(self):
        self.items = []

    def put_nowait(self, item):
        self.items.append(item)


class RecordingImagery:
    def __init__(self):
        self.calls = []

    def warm(self, frame, points, radius, texel_m):
        self.calls.append((points, radius, texel_m))


def warm_fixture():
    frame = RecordingFrame()
    anchor = types.SimpleNamespace(id=1, lat=36.17, lon=-115.145, frame=frame)
    queue, imagery = RecordingQueue(), RecordingImagery()
    server = types.SimpleNamespace(grader_for=lambda a: object(), layout=types.SimpleNamespace(block=512),
                                   block_pools=[(None, queue)], road_pool=object(), imagery=imagery)
    return server, anchor, queue


class WarmAheadTest(unittest.TestCase):
    def warm(self, x, y, vx, vy):
        server, anchor, queue = warm_fixture()
        earthd.Server.warm_roads(server, anchor, x, y, vx, vy)
        earthd.Server.warm_imagery(server, anchor, x, y, vx, vy)
        road_points = anchor.frame.points
        image_points, radius, texel_m = server.imagery.calls[0]
        self.assertEqual(road_points[:-1], image_points, "both warmers use the same speculative points")
        self.assertEqual(road_points[-1], (x, y), "the larger local road-planning region is retained")
        self.assertEqual(radius, 512 + earthd.IMG_WARM_MARGIN)
        self.assertEqual(texel_m, 1)
        self.assertEqual(len(queue.items), 1)
        self.assertTrue(queue.items[0][3], "road cells still reach the background planner")
        return image_points

    def test_normal_driving_and_fast_flight_keep_full_horizon(self):
        # 240 MPH and the documented 300 m/s flight case retain all original 0/10/20/30 s targets.
        for vx, vy in ((0, 0), (107.2896, 0), (0, -300), (180, -240), (400, 0)):
            with self.subTest(vx=vx, vy=vy):
                points = self.warm(37, -91, vx, vy)
                self.assertEqual(points, [(37 + vx * t, -91 + vy * t) for t in earthd.WARM_STEPS_S])

    def test_extreme_finite_velocity_does_not_warm_remote_regions(self):
        # Finite velocities observed in the dev.3 capture, plus a diagonal case.
        for vx, vy in ((27583, 0), (366746.9, 0), (-220048.14, 293397.52)):
            with self.subTest(vx=vx, vy=vy):
                points = self.warm(37, -91, vx, vy)
                distances = [math.hypot(px - 37, py + 91) for px, py in points]
                self.assertLessEqual(max(distances), 12000 + 1e-8)
                self.assertEqual(distances, sorted(distances), "nearest speculative point remains first")
                self.assertEqual(points[0], (37, -91))
                for (px, py), t in zip(points, earthd.WARM_STEPS_S):
                    self.assertAlmostEqual(math.hypot(px - 37, py + 91), 12000 * t / 30, delta=1e-8)
                    self.assertAlmostEqual((px - 37) * vy - (py + 91) * vx, 0, delta=1e-3)

    def test_nonfinite_pose_velocity_still_rejected(self):
        # The network contract must reject invalid numbers, not quietly turn them into stationary motion.
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    earthd._num({"vx": value}, "vx", 0.0)
                for method in (earthd.Server.warm_roads, earthd.Server.warm_imagery):
                    server, anchor, queue = warm_fixture()
                    with self.assertRaises(ValueError):
                        method(server, anchor, 37, -91, value, 0.0)
                    self.assertEqual(queue.items, [])
                    self.assertEqual(server.imagery.calls, [])

    def test_large_finite_components_keep_finite_bounded_diagonal(self):
        # Each component is finite even though hypot overflows; speculative normalization must remain usable.
        points = self.warm(37, -91, 1.7e308, -1.7e308)
        for px, py in points:
            self.assertTrue(math.isfinite(px) and math.isfinite(py))
            self.assertAlmostEqual(px - 37, -(py + 91), delta=1e-8)
        self.assertAlmostEqual(math.hypot(points[-1][0] - 37, points[-1][1] + 91), 12000, delta=1e-8)


if __name__ == "__main__":
    unittest.main()
