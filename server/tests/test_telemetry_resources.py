"""Resource telemetry stays read-only, bounded, identity-aware and explicit about gaps."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from telemetry_resources import ResourceSampler, _Linux, _proc_stat


class FakeBackend:
    limited = False

    def __init__(self):
        self.calls = 0
        self.fail = False
        self.procs = {10: (1, "earth.exe"), 11: (10, "worker.exe"),
                      20: (1, "BeamNG.drive.x64.exe"), 21: (20, "BeamNG.drive.x64.exe")}
        self.rows = {p: {"birth": p, "name": n, "cpu_s": 10.0, "rss_bytes": 1024,
                         "read_bytes": 100, "write_bytes": 200} for p, (_, n) in self.procs.items()}

    def discover(self):
        self.calls += 1
        if self.fail:
            raise OSError("fixture discovery unavailable")
        return dict(self.procs)

    def read(self, pid):
        row = self.rows.get(pid)
        return dict(row) if row else None


class ResourcesTest(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.backend = FakeBackend()
        self.s = ResourceSampler(backend=self.backend, clock=lambda: self.now, pid=10)

    def tearDown(self):
        self.s.close()

    def test_first_sample_has_unknown_rates_and_does_not_export_identity(self):
        r = self.s.sample()
        self.assertIsNone(r["game"]["cpu_pct"])
        self.assertIsNone(r["game"]["private_bytes"])
        self.assertEqual(r["game"]["process_count"], 1)  # same-image CEF child excluded
        self.assertEqual(r["workers"]["process_count"], 1)
        text = json.dumps(r, allow_nan=False)
        for key in ("pid", "birth", "name", "earth.exe", "worker.exe", "BeamNG"):
            self.assertNotIn(key, text)

    def test_actual_elapsed_per_process_deltas(self):
        self.s.sample()
        self.now = 2.5
        self.backend.rows[20].update(cpu_s=11.25, read_bytes=350, write_bytes=700)
        r = self.s.sample()["game"]
        self.assertEqual(r["cpu_pct"], 50.0)
        self.assertEqual(r["read_bytes_s"], 100.0)
        self.assertEqual(r["write_bytes_s"], 200.0)
        self.assertEqual(self.backend.calls, 1)

    def test_same_clock_and_counter_rollback_are_unknown(self):
        self.s.sample()
        self.assertIsNone(self.s.sample()["game"]["cpu_pct"])
        self.now = 1
        self.backend.rows[20]["cpu_s"] = 1
        self.assertIsNone(self.s.sample()["game"]["cpu_pct"])

    def test_pid_reuse_is_not_attributed_or_used_for_delta(self):
        self.s.sample()
        self.now = 1
        self.backend.rows[20].update(birth=99, cpu_s=1000)
        reused = self.s.sample()["game"]
        self.assertEqual(reused["sampled"], 0)
        self.assertTrue(reused["partial"])
        self.assertIsNone(reused["rss_bytes"])
        self.now = 5
        fresh = self.s.sample()["game"]
        self.assertEqual(fresh["sampled"], 1)
        self.assertIsNone(fresh["cpu_pct"])

    def test_discovery_is_at_most_every_five_seconds(self):
        for t in (0, 0.1, 1, 4.99, 5, 5.5, 9.99):
            self.now = t
            self.s.sample()
        self.assertEqual(self.backend.calls, 2)

    def test_exited_process_and_discovery_failure_are_explicit(self):
        self.s.sample()
        self.backend.rows.pop(20)
        self.backend.fail = True
        self.now = 5
        r = self.s.sample()
        self.assertFalse(r["discovery_ok"])
        self.assertTrue(r["game"]["partial"])
        self.assertIsNone(r["game"]["rss_bytes"])

    def test_access_denied_is_unknown_not_zero_processes(self):
        self.backend.rows.pop(20)
        r = self.s.sample()["game"]
        self.assertEqual(r["process_count"], 1)
        self.assertEqual(r["sampled"], 0)
        self.assertIsNone(r["rss_bytes"])
        self.assertTrue(r["partial"])

    def test_recycled_parent_pid_does_not_claim_older_worker(self):
        self.backend.rows[10]["birth"] = 100
        self.assertEqual(self.s.sample()["workers"]["process_count"], 0)

    def test_game_discovery_rejects_name_changed_between_snapshot_and_read(self):
        self.backend.rows[20]["name"] = "unrelated.exe"
        self.assertEqual(self.s.sample()["game"]["process_count"], 0)

    def test_new_worker_has_unknown_cpu_until_second_sample(self):
        self.s.sample()
        self.backend.procs[12] = (11, "worker.exe")
        self.backend.rows[12] = dict(self.backend.rows[11], birth=12)
        self.now = 5
        r = self.s.sample()["workers"]
        self.assertEqual(r["process_count"], 2)
        self.assertEqual(r["rss_bytes"], 2048)
        self.assertIsNone(r["cpu_pct"])
        self.now = 6
        self.assertEqual(self.s.sample()["workers"]["cpu_pct"], 0.0)

    def test_close_prevents_more_reads(self):
        self.s.sample()
        self.s.close()
        self.assertFalse(self.s.sample()["available"])
        self.assertEqual(self.backend.calls, 1)


def stat_text(name="odd ) worker (name", *, birth=42):
    tail = ["0"] * 22
    for i, v in {0: "S", 1: "123", 7: "7", 9: "2", 11: "100", 12: "200",
                 19: str(birth), 21: "12"}.items():
        tail[i] = v
    return f"456 ({name}) " + " ".join(tail)


class LinuxParsingTest(unittest.TestCase):
    def test_stat_handles_parentheses_spaces_and_field_offsets(self):
        r = _proc_stat(stat_text())
        self.assertEqual(r, {"name": "odd ) worker (name", "state": "S", "ppid": 123,
                             "cpu_ticks": 300, "birth": 42, "rss_pages": 12,
                             "page_faults": 9, "major_faults": 2})

    def test_linux_file_backend_missing_optional_io(self):
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder) / "456"
            d.mkdir()
            (d / "stat").write_text(stat_text(), encoding="utf-8")
            (d / "fd").mkdir()
            (d / "fd" / "0").write_text("")
            # Cross-platform fixture: no platform-specific sysconf needed.
            b = _Linux.__new__(_Linux)
            b.root, b.ticks, b.page = Path(folder), 100, 4096
            r = b.read(456)
            self.assertEqual(r["cpu_s"], 3)
            self.assertEqual(r["rss_bytes"], 12 * 4096)
            self.assertEqual(r["fds"], 1)
            self.assertNotIn("read_bytes", r)
            (d / "io").write_text("read_bytes: 90\nwrite_bytes: 100\n")
            self.assertEqual(b.read(456)["read_bytes"], 90)
            (d / "stat").write_text("invalid")
            self.assertIsNone(b.read(456))


@unittest.skipUnless(os.name == "nt" or sys.platform == "linux", "native backend unavailable")
class LiveResourcesTest(unittest.TestCase):
    def test_current_process_counters_and_closed_sampler(self):
        sampler = ResourceSampler()
        try:
            first = sampler.sample()
            self.assertTrue(first["available"])
            self.assertTrue(first["discovery_ok"])
            self.assertEqual(first["companion"]["sampled"], 1)
            self.assertGreater(first["companion"]["rss_bytes"], 0)
            # Repeated calls also catch Windows pointer/handle declaration errors.
            for _ in range(40):
                last = sampler.sample()
                json.dumps(last, allow_nan=False)
            if os.name == "nt":
                self.assertLessEqual(last["companion"]["handles"], first["companion"]["handles"] + 4)
                self.assertGreater(last["companion"]["private_bytes"], 0)
        finally:
            sampler.close()
        self.assertFalse(sampler.sample()["available"])


if __name__ == "__main__":
    unittest.main()
