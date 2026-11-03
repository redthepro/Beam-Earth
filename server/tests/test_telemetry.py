"""Capture is bounded, survives interrupted writes, and excludes unsolicited data."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "server"), str(ROOT / "tools")]
import telemetry
import inspect_capture


def sample(seq=1, frames=60, wall_ms=1000, speed=107.29):
    histogram = [0] * len(telemetry.HISTOGRAM_MS)
    histogram[5] = frames - 1
    histogram[10] = 1
    return {"schema": 1, "seq": seq, "elapsed_s": float(seq),
            "frame": {"frames": frames, "wall_ms": wall_ms, "max_ms": 60, "histogram": histogram,
                      "over_33": 1, "over_50": 1, "over_100": 0, "sim_s": .9, "sim_wall_ratio": .9},
            "context": {"speed_mps": speed, "lua_mb": 155, "lat": 37.2, "lon": -77.3,
                        "road_ahead_s": 4.2, "server_backlog": 10, "player_name": "PRIVATE PLAYER",
                        "token": "PRIVATE TOKEN", "source": "C:/PRIVATE/path"},
            "mod_ms": {"receive": 1.5, "PRIVATE TOKEN": 42},
            "worst": {"ms": 60, "mod_ms": {"receive": 1.2}, "engine_ms": {"render": 2.4},
                      "context": {"lat": 37.2, "lon": -77.3}}}


class Resources:
    def sample(self):
        return {"available": True, "sample_ms": .05,
                "game": {"cpu_pct": None, "rss_bytes": 1048576, "private_bytes": None}}

    def close(self):
        pass


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("timed out waiting for capture worker")


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.managers = []

    def tearDown(self):
        for manager in self.managers:
            manager.close()
        self.tmp.cleanup()

    def manager(self, **kwargs):
        manager = telemetry.CaptureManager(self.root, "1.8.4-dev.1", resource_factory=Resources, **kwargs)
        self.managers.append(manager)
        return manager

    def end(self, manager):
        manager.stop()
        wait_for(lambda: manager.status()["status"] not in ("starting", "recording", "stopping"))
        path = self.root / ("capture-" + manager.status()["session"] + ".jsonl")
        return path, [json.loads(line) for line in path.read_text().splitlines()]

    def test_disabled_by_default_and_no_disk_until_start(self):
        manager = self.manager()
        self.assertFalse(manager.status()["active"])
        self.assertFalse(manager.sample(sample()))
        self.assertFalse(manager.marker())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_capture_lifecycle_and_privacy(self):
        manager = self.manager()
        manager.start()
        self.assertTrue(manager.sample(sample(), {"backlog": 7, "credentials": "SECRET"}))
        self.assertTrue(manager.marker())
        path, rows = self.end(manager)
        self.assertEqual(rows[0]["kind"], "start")
        self.assertEqual(rows[-1]["kind"], "end")
        self.assertEqual(rows[-1]["reason"], "user")
        self.assertEqual(rows[-1]["capture"]["samples"], 1)
        self.assertTrue(all(r["elapsed_s"] >= 0 for r in rows))
        data = path.read_text()
        for private in ("PRIVATE", "SECRET", '"lat"', '"lon"', "credentials", "player_name"):
            self.assertNotIn(private, data)
        self.assertGreater(manager.status()["bytes"], 0)
        self.assertEqual(manager.status()["bytes"], path.stat().st_size)

    def test_route_coordinates_require_true_opt_in_including_worst(self):
        for include in (False, "true", True):
            manager = self.manager()
            manager.start(include_location=include)
            manager.sample(sample())
            _, rows = self.end(manager)
            item = next(r["client"] for r in rows if r["kind"] == "sample")
            self.assertEqual("lat" in item["context"], include is True)
            self.assertEqual("lon" in item["worst"]["context"], include is True)

    def test_motion_boundary_and_wall_ages_survive_privacy_filter(self):
        row = sample()
        measurements = {"vertical_speed_mps": -12.5, "block_inside": False,
                        "block_outside_m": 30, "queues_age_wall_s": 2.5,
                        "terrain_wait_wall_s": 10.5, "terrain_last_wall_s": 8,
                        "terrain_pending_ready": True, "guard_mode": 4,
                        "guard_holds": 2, "guard_rescues": 1, "guard_failed": False,
                        "player_frame_current": False, "audio_spaces_live": 5,
                        "audio_spaces_created": 10, "audio_spaces_deleted": 5,
                        "audio_spaces_moved": 20, "audio_spaces_replaced": 2}
        row["context"].update(measurements)
        row["worst"]["context"].update(measurements)
        cleaned = telemetry.clean_client(row)
        for context in (cleaned["context"], cleaned["worst"]["context"]):
            for key, value in measurements.items():
                self.assertEqual(context[key], value)
            self.assertNotIn("lat", context)
            self.assertNotIn("lon", context)
        row["context"]["vertical_speed_mps"] = float("nan")
        row["context"]["guard_mode"] = "PRIVATE"
        cleaned = telemetry.clean_client(row)
        self.assertNotIn("vertical_speed_mps", cleaned["context"])
        self.assertNotIn("guard_mode", cleaned["context"])

    def test_resource_observations_continue_without_game_packets(self):
        manager = self.manager()
        manager.start()
        wait_for(lambda: manager.status()["bytes"] > 700)
        path, rows = self.end(manager)
        resources = [r for r in rows if r["kind"] == "resources"]
        self.assertGreaterEqual(len(resources), 1)
        self.assertIsNone(resources[0]["resources"]["game"]["cpu_pct"])

    def test_diagnostics_fixed_schema_privacy_and_disk_roundtrip(self):
        row = sample()
        row["diagnostics"] = {"events": [{"name": "frame_shift", "frame": 12, "seq": 3,
            "elapsed_ms": 200.5, "anchor_id": 2, "previous_anchor_id": 1, "phase": 2,
            "distance_m": 600, "vertical_speed_mps": -12.5, "ready": False,
            "mode": 4, "reason": "PRIVATE", "lat": 37.2, "lon": -77.3,
            "key": "PRIVATE", "path": "PRIVATE", "token": "PRIVATE", "adds": [1],
            "removes": float("nan"), "waiting": float("inf"), "retired": 1e16}],
            "dropped": 4, "private": "PRIVATE"}
        expected = {"events": [{"name": "frame_shift", "frame": 12, "seq": 3,
            "elapsed_ms": 200.5, "anchor_id": 2, "previous_anchor_id": 1, "phase": 2,
            "distance_m": 600, "vertical_speed_mps": -12.5, "ready": False, "mode": 4}], "dropped": 4}
        for location in (False, True):
            self.assertEqual(telemetry.clean_client(row, location)["diagnostics"], expected)
        manager = self.manager()
        manager.start()
        self.assertTrue(manager.sample(row))
        path, rows = self.end(manager)
        saved = next(r["client"] for r in rows if r["kind"] == "sample")
        self.assertEqual(saved["diagnostics"], expected)
        self.assertNotIn("PRIVATE", path.read_text())

    def test_diagnostics_server_bound_and_rejected_events_are_counted(self):
        event = {"name": "player_jump", "frame": 1, "seq": 1, "elapsed_ms": 16}
        row = sample()
        events = [dict(event, seq=i + 1) for i in range(10000)]
        events[0]["name"] = "PRIVATE"
        events[1]["frame"] = True
        events[2]["seq"] = -1
        events[3]["elapsed_ms"] = float("inf")
        events[4]["name"] = ["not a string"]
        events[5] = "not an event"
        events[6]["elapsed_ms"] = -1
        events[7]["frame"] = 1.5
        row["diagnostics"] = {"events": events, "dropped": 2}
        result = telemetry.clean_client(row)["diagnostics"]
        self.assertEqual(len(result["events"]), 24)
        self.assertEqual(result["dropped"], 9978)
        self.assertEqual(result["events"][0]["seq"], 9)
        self.assertEqual(result["events"][-1]["seq"], 32)

    def test_diagnostics_malformed_optional_data_does_not_break_frame_capture(self):
        row = sample()
        for value in (None, [], "PRIVATE", {"events": "PRIVATE"}, {"events": {"private": 1}}):
            row["diagnostics"] = value
            cleaned = telemetry.clean_client(row)
            self.assertNotIn("diagnostics", cleaned)
            self.assertEqual(cleaned["frame"]["frames"], 60)
        for empty in ([], {}):
            for dropped in (True, -1, 1e16, "PRIVATE"):
                row["diagnostics"] = {"events": empty, "dropped": dropped}
                self.assertEqual(telemetry.clean_client(row)["diagnostics"], {"events": [], "dropped": 0})
        row["diagnostics"] = {"events": [None] * 35, "dropped": 10**15}
        self.assertEqual(telemetry.clean_client(row)["diagnostics"]["dropped"], 10**15)

    def test_server_only_samples_and_counter_allowlist(self):
        manager = self.manager()
        manager.start()
        self.assertTrue(manager.sample(None, {"loop_lag_ms": 3.5, "client_age_s": 2, "token": 123}))
        _, rows = self.end(manager)
        row = next(r for r in rows if r["kind"] == "server")
        self.assertEqual(row["server"], {"loop_lag_ms": 3.5, "client_age_s": 2})

    def test_disk_failure_isolated_and_recoverable(self):
        self.root.joinpath("file").write_text("not a folder")
        manager = self.manager()
        manager.root = self.root / "file"
        manager.start()
        wait_for(lambda: manager.status()["status"] == "error")
        self.assertFalse(manager.status()["active"])
        self.assertEqual(manager.status()["error"], "write_failed")
        manager.root = self.root
        manager.start()
        manager.sample(sample())
        _, rows = self.end(manager)
        self.assertEqual(rows[-1]["kind"], "end")

    def test_queue_full_is_nonblocking_and_stop_is_not_lost(self):
        entered, release = threading.Event(), threading.Event()

        class BlockingResources(Resources):
            def sample(self):
                entered.set()
                release.wait(2)
                return super().sample()

        manager = self.manager(queue_size=2)
        manager._resource_factory = BlockingResources
        manager.start()
        self.assertTrue(entered.wait(2))
        begin = time.monotonic()
        try:
            self.assertTrue(manager.sample(sample(1)))
            self.assertTrue(manager.sample(sample(2)))
            self.assertFalse(manager.sample(sample(3)))
            manager.stop()
            self.assertLess(time.monotonic() - begin, .2)
            self.assertLessEqual(manager.status()["queue_depth"], 3)
        finally:
            release.set()
        _, rows = self.end(manager)
        self.assertEqual(rows[-1]["capture"]["dropped"], 1)
        self.assertEqual(rows[-1]["capture"]["samples"], 2)

    def test_session_limit_closes_cleanly(self):
        manager = self.manager(max_bytes=4096)
        manager.start()
        for seq in range(30):
            manager.sample(sample(seq))
        wait_for(lambda: manager.status()["status"] == "size_limit")
        path, rows = self.end(manager)
        self.assertLessEqual(path.stat().st_size, 4096)
        self.assertEqual(rows[-1]["reason"], "size_limit")
        self.assertGreater(rows[-1]["capture"]["dropped"], 0)
        self.assertFalse(manager.status()["active"])

    def test_retention_only_removes_owned_files(self):
        private = self.root / "my-notes.jsonl"
        private.write_text("leave alone")
        old = self.root / "capture-20010101T000000-aaaaaaaaaaaa.jsonl"
        old.write_bytes(b"x" * 5000)
        manager = self.manager(max_bytes=4096, max_sessions=2)
        for _ in range(3):
            manager.start()
            manager.sample(sample())
            self.end(manager)
            time.sleep(.01)
        self.assertFalse(old.exists())
        self.assertEqual(private.read_text(), "leave alone")
        self.assertEqual(len(list(self.root.glob("capture-*.jsonl"))), 2)

    def test_partial_file_is_flushed_before_stop(self):
        manager = self.manager()
        manager.start()
        manager.sample(sample())
        wait_for(lambda: manager.status()["samples"] == 1)
        path = self.root / ("capture-" + manager.status()["session"] + ".jsonl")
        report = inspect_capture.inspect(path)
        self.assertEqual(report["samples"], 1)
        self.assertFalse(report["complete"])

    def test_optional_resource_failure_does_not_disable_frames(self):
        manager = self.manager()
        manager._resource_factory = mock.Mock(side_effect=OSError("private path"))
        manager.start()
        manager.sample(sample())
        _, rows = self.end(manager)
        self.assertEqual(rows[-1]["capture"]["samples"], 1)
        self.assertEqual(manager.status()["status"], "user")

    def test_invalid_frames_and_histograms_rejected(self):
        manager = self.manager()
        manager.start()
        for update in ({"wall_ms": float("nan")}, {"frames": True}, {"max_ms": -1},
                       {"histogram": [1, 2]}, {"histogram": [1] * len(telemetry.HISTOGRAM_MS)}):
            data = sample()
            data["frame"].update(update)
            self.assertFalse(manager.sample(data))
        _, rows = self.end(manager)
        self.assertEqual(rows[-1]["capture"]["samples"], 0)

    def test_large_numbers_and_timer_maps_have_bounded_filtering(self):
        data = sample()
        data["context"]["speed_mps"] = 10 ** 1000
        data["mod_ms"] = {"bad key" + str(n): 1 for n in range(10000)}
        data["mod_ms"]["late_valid_key"] = 1
        clean = telemetry.clean_client(data)
        self.assertNotIn("speed_mps", clean["context"])
        self.assertEqual(clean["mod_ms"], {})

    def test_cannot_restart_until_final_flush_completes(self):
        manager = self.manager()
        manager.start()
        session = manager.status()["session"]
        entered, release = threading.Event(), threading.Event()
        real_write = manager._write

        def blocked_write(stream, record, **kwargs):
            if kwargs.get("final"):
                entered.set()
                release.wait(2)
            return real_write(stream, record, **kwargs)

        manager._write = blocked_write
        manager.sample(sample())
        manager.stop()
        self.assertTrue(entered.wait(2))
        try:
            self.assertEqual(manager.start()["session"], session)
            self.assertEqual(manager.status()["status"], "stopping")
        finally:
            release.set()
        self.end(manager)
        self.assertNotEqual(manager.start()["session"], session)
        self.end(manager)

    def test_report_combines_histograms_and_uses_actual_frame_time(self):
        manager = self.manager()
        manager.start(include_location=True)
        manager.sample(sample(1, frames=60, wall_ms=1000))
        manager.sample(sample(3, frames=60, wall_ms=2000))
        path, _ = self.end(manager)
        report = inspect_capture.inspect(path)
        self.assertEqual(report["fps"], 40)
        self.assertEqual(report["sequence_gaps"], 1)
        self.assertEqual(report["p50_ms"], 16.7)
        self.assertEqual(report["p99_ms"], 66.7)
        self.assertEqual(report["high_speed_wall_s"], 3)
        self.assertNotIn("37.2", inspect_capture.render(report))
        self.assertNotIn("-77.3", json.dumps(report))
        self.assertTrue(report["coordinates_recorded"])

    def test_report_excludes_extreme_reported_speeds_but_preserves_measurements(self):
        packets = []
        # A valid 240 MPH run, an unpaused reset velocity, and the same transient while fully paused.
        for seq, speed, paused in ((1, 240 / inspect_capture.MPH_PER_MPS, 0),
                                   (2, 90000, 0), (3, 90000, 60)):
            packet = sample(seq, speed=speed)
            packet["frame"]["paused_frames"] = paused
            packet["worst"]["context"]["speed_mps"] = speed
            packets.append(packet)
        # Even when the window ends at a normal speed, an extreme worst-frame observation disqualifies it.
        packet = sample(4, speed=240 / inspect_capture.MPH_PER_MPS)
        packet["frame"]["paused_frames"] = 10
        packet["worst"]["context"]["speed_mps"] = 109885
        packets.append(packet)
        path = self.root / "reported-speed-outliers.jsonl"
        path.write_text("".join(json.dumps({"kind": "sample", "elapsed_s": i, "client": packet}) + "\n"
                                for i, packet in enumerate(packets, 1)))

        report = inspect_capture.inspect(path)
        self.assertEqual(report["reported_speed_outlier_windows"], 3)  # once per window, not twice per observation
        self.assertEqual(report["speed_outlier_threshold_mph"], 1000)
        self.assertEqual(report["high_speed_wall_s"], 1)
        self.assertEqual(report["high_speed_partial_pause_windows"], 0)
        self.assertEqual(report["context"]["speed_mps"]["max"], 90000)
        self.assertAlmostEqual(report["worst_windows"][-1]["speed_mph"], 109885 * inspect_capture.MPH_PER_MPS)
        self.assertEqual(sum(w["reported_speed_outlier"] for w in report["worst_windows"]), 3)
        text = inspect_capture.render(report)
        self.assertIn("Qualified reported 200+ MPH window exposure: 1.0 s", text)
        self.assertIn("Diagnostic speed outlier windows: 3", text)
        self.assertIn("above 1000 MPH", text)
        self.assertIn("Reported speed (raw)", text)
        self.assertIn("reported 240.0 MPH", text)
        self.assertIn("window contains a diagnostic speed outlier", text)

    def test_report_speed_outlier_threshold_is_strictly_above_1000_mph(self):
        path = self.root / "reported-speed-threshold.jsonl"
        path.write_text("".join(json.dumps({"kind": "sample", "elapsed_s": i,
                                            "client": sample(i, speed=mph / inspect_capture.MPH_PER_MPS)}) + "\n"
                                for i, mph in enumerate((1000, 1000.1), 1)))
        report = inspect_capture.inspect(path)
        self.assertEqual(report["reported_speed_outlier_windows"], 1)
        self.assertEqual(report["high_speed_wall_s"], 1)

    def test_report_survives_bad_and_truncated_records(self):
        path = self.root / "partial.jsonl"
        good = {"kind": "sample", "elapsed_s": 1, "client": sample()}
        path.write_bytes(json.dumps(good).encode() + b"\n" + b"x" * (inspect_capture.MAX_LINE + 10) + b"\n"
                         + b'{"kind":"end","elapsed_s":2,"capture":[],"reason":"not allowed"}\n'
                         + b'{"kind":"sample","elapsed_')
        report = inspect_capture.inspect(path)
        self.assertFalse(report["complete"])
        self.assertTrue(report["truncated_tail"])
        self.assertEqual(report["invalid_lines"], 2)
        self.assertEqual(report["samples"], 1)

    def test_report_bounded_event_chronology_separates_client_and_receipt_clocks(self):
        path = self.root / "dev-events.jsonl"
        rows = []
        for window in range(4):
            packet = sample(window + 1)
            packet["diagnostics"] = {"events": [
                {"name": "player_jump" if i == 0 else "far_swap", "frame": window * 60 + i,
                 "seq": window * 32 + i + 1, "elapsed_ms": window * 1000 + i,
                 "adds": i, "ready": False, "reason": 2, "lat": 37.2,
                 "path": "PRIVATE", "token": "PRIVATE"} for i in range(32)], "dropped": window}
            rows.append({"kind": "sample", "elapsed_s": 100 + window, "client": packet})
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        report = inspect_capture.inspect(path)
        diagnostics = report["diagnostics"]
        self.assertEqual(diagnostics["windows"], 4)
        self.assertEqual(diagnostics["events"], 128)
        self.assertEqual(diagnostics["counts"], {"player_jump": 4, "far_swap": 124})
        self.assertEqual(diagnostics["dropped"], 6)
        self.assertEqual(len(diagnostics["recent"]), 64)
        first, last = diagnostics["recent"][0], diagnostics["recent"][-1]
        self.assertEqual((first["seq"], first["frame"], first["elapsed_ms"], first["received_elapsed_s"]),
                         (65, 120, 2000, 102))
        self.assertEqual((last["seq"], last["frame"], last["elapsed_ms"], last["received_elapsed_s"]),
                         (128, 211, 3031, 103))
        text = inspect_capture.render(report)
        self.assertIn("crash recovery or a reset may explain it", text)
        self.assertIn("client frame 120, seq 65, elapsed 2000.000 ms; window received at 102.000 s", text)
        self.assertIn("reported/rejected events dropped: 6", text)
        self.assertNotIn("PRIVATE", text + json.dumps(report))
        self.assertNotIn('"lat"', json.dumps(diagnostics))
        self.assertFalse(last["ready"])

    def test_report_without_events_keeps_legacy_output_and_schema(self):
        path = self.root / "legacy.jsonl"
        path.write_text(json.dumps({"kind": "sample", "elapsed_s": 1, "client": sample()}) + "\n")
        report = inspect_capture.inspect(path)
        self.assertEqual(report["diagnostics"], {"windows": 0, "events": 0, "dropped": 0, "counts": {}, "recent": []})
        self.assertNotIn("Dev event chronology", inspect_capture.render(report))
        del report["diagnostics"]
        self.assertNotIn("Dev event chronology", inspect_capture.render(report))

    def test_cli_json_and_output_without_python_path_requirement(self):
        manager = self.manager()
        manager.start()
        manager.sample(sample())
        path, _ = self.end(manager)
        report = self.root / "report.json"
        self.assertEqual(inspect_capture.main([str(path), "--json", "--out", str(report)]), 0)
        self.assertEqual(json.loads(report.read_text())["samples"], 1)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            inspect_capture.main([str(path)])
        self.assertIn("driving capture", output.getvalue())

    def test_report_refuses_to_overwrite_its_capture(self):
        manager = self.manager()
        manager.start()
        manager.sample(sample())
        path, _ = self.end(manager)
        before = path.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ex:
            inspect_capture.main([str(path), "--out", str(path)])
        self.assertEqual(ex.exception.code, 2)
        self.assertEqual(path.read_bytes(), before)


class ClientContractTests(unittest.TestCase):
    def lua_module(self, dev=False):
        from lupa.luajit21 import LuaRuntime
        runtime = LuaRuntime()
        extension_path = ROOT / "mod/beamng_earth/lua/ge/extensions"
        runtime.execute("package.path = ... .. '/?.lua;' .. package.path", extension_path.as_posix())
        runtime.execute("package.loaded['beamngEarth/buildinfo'] = { devDiagnostics = ... }", dev)
        module = runtime.execute("return require('beamngEarth/telemetry')")
        return runtime, module

    def test_fully_paused_lua_window_with_stale_speed_is_not_high_speed_coverage(self):
        runtime, module = self.lua_module()
        module.setStatus(runtime.table_from({"active": True, "session": "paused-contract"}))
        context = runtime.eval("function() return {speed_mps=107.2896, road_ahead_s=18.49, road_probe_limited=true, queues_age_s=0.8} end")
        send = runtime.eval("function() return true end")
        row = None
        for _ in range(60):
            module.simulation(0)
            row = module.frame(16.7, runtime.table_from({"telemetry": .1}), None, context, None, send) or row

        def convert(value):
            if not hasattr(value, "items"):
                return value
            pairs = dict(value.items())
            if pairs and all(type(k) is int for k in pairs):
                return [convert(pairs[k]) for k in range(1, len(pairs) + 1)]
            return {k: convert(v) for k, v in pairs.items()}

        packet = convert(row)
        self.assertEqual(packet["frame"]["paused_frames"], 60)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paused-lua.jsonl"
            path.write_text(json.dumps({"kind": "sample", "elapsed_s": 1.1, "client": packet}) + "\n")
            report = inspect_capture.inspect(path)
        self.assertEqual(report["high_speed_wall_s"], 0)
        self.assertEqual(report["worst_windows"][0]["queues_age_s"], .8)
        self.assertTrue(report["worst_windows"][0]["road_probe_limited"])
        text = inspect_capture.render(report)
        self.assertIn("road ahead at least 18.5 s (probe limit reached)", text)
        self.assertIn("queue observation 0.8 engine-clock s (may undercount stalls) old", text)
        self.assertIn("Fully paused windows are excluded", text)
        report["worst_windows"][0]["queues_age_wall_s"] = 2.5
        self.assertIn("queue observation 2.5 wall-clock s old", inspect_capture.render(report))

    def test_actual_lua_packet_matches_capture_schema(self):
        runtime, module = self.lua_module()
        module.setStatus(runtime.table_from({"active": True, "session": "contract", "include_location": False}))
        context = runtime.eval("function() return {speed_mps=107.2896, queues_age_s=0.2, lat=38, lon=-77} end")
        send = runtime.eval("function() return true end")
        row = None
        for index in range(60):
            module.simulation(0 if index < 10 else 0.0167)
            row = module.frame(16.7, runtime.table_from({"terrain": 2, "telemetry": .25}),
                               None, context, None, send) or row

        def convert(value):
            if not hasattr(value, "items"):
                return value
            pairs = dict(value.items())
            if pairs and all(type(k) is int for k in pairs):
                return [convert(pairs[k]) for k in range(1, len(pairs) + 1)]
            return {k: convert(v) for k, v in pairs.items()}

        packet = convert(row)
        cleaned = telemetry.clean_client(packet)
        self.assertIsNotNone(cleaned)
        self.assertEqual(cleaned["frame"], packet["frame"])
        self.assertEqual(sum(cleaned["frame"]["histogram"]), 60)
        self.assertEqual(cleaned["mod_ms"]["terrain"], 120)
        self.assertEqual(cleaned["context"]["queues_age_s"], 0.2)
        self.assertNotIn("lat", cleaned["context"])
        self.assertNotIn("lon", cleaned["context"])
        self.assertEqual(cleaned["mod_ms"]["telemetry"], 15)
        self.assertEqual(cleaned["frame"]["paused_frames"], 10)
        self.assertNotIn("telemetry_ms", cleaned["context"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "actual-lua.jsonl"
            path.write_text(json.dumps({"kind": "sample", "elapsed_s": 2, "client": packet}) + "\n")
            report = inspect_capture.inspect(path)
        self.assertEqual(report["client_work"]["telemetry_ms_per_window"]["mean"], 15)
        self.assertAlmostEqual(report["high_speed_wall_s"], 1.002)
        self.assertEqual(report["high_speed_partial_pause_windows"], 1)
        self.assertEqual(report["worst_windows"][0]["received_elapsed_s"], 2)
        text = inspect_capture.render(report)
        self.assertIn("Client capture work per sample window: mean 15.0 ms", text)
        self.assertIn("Simulation/wall ratio (includes paused intervals)", text)
        self.assertIn("paused frames: 10", text)
        self.assertIn("Window received at t=2.0 s", text)
        self.assertIn("not exact hitch occurrence times", text)
        self.assertIn("1 partially paused windows count their whole measured duration", text)
        self.assertNotIn("Previous client telemetry work", text)

    def test_actual_dev_lua_events_survive_cleaning_and_empty_lua_tables(self):
        runtime, module = self.lua_module(dev=True)
        diagnostics = runtime.execute("return require('beamngEarth/diagnostics')")
        module.setStatus(runtime.table_from({"active": True, "session": "dev-contract"}))
        diagnostics.frame(1000)
        diagnostics.event("drop_hold", runtime.table_from({"phase": 1, "distance_m": 500, "ready": False, "lat": 37.2}))
        send = runtime.eval("function() return true end")

        def convert(value):
            if not hasattr(value, "items"):
                return value
            pairs = dict(value.items())
            if pairs and all(type(k) is int for k in pairs):
                return [convert(pairs[k]) for k in range(1, len(pairs) + 1)]
            return {k: convert(v) for k, v in pairs.items()}

        packet = convert(module.frame(1000, None, None, None, None, send))
        clean = telemetry.clean_client(packet)
        self.assertEqual(clean["diagnostics"], {"events": [{"name": "drop_hold", "frame": 1,
            "seq": 1, "elapsed_ms": 1000, "phase": 1, "distance_m": 500, "ready": False}], "dropped": 0})
        empty_packet = convert(module.frame(1000, None, None, None, None, send))
        self.assertEqual(empty_packet["diagnostics"]["events"], {})
        self.assertEqual(telemetry.clean_client(empty_packet)["diagnostics"], {"events": [], "dropped": 0})


if __name__ == "__main__":
    unittest.main()
