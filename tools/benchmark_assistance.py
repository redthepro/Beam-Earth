#!/usr/bin/env python3
"""Bounded synthetic native-generation benchmark over the actual TLS assistance path.

Two spawned client processes have independent planners/caches and different anchors.
The helper uses assistjobs.run, real native geometry, real grading and complete plan
provenance. Only geographic inputs are synthetic; no provider downloads or game/FPS
measurement. Timings exclude process startup and certificate creation.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "server"), str(ROOT / "server/tests")]

SETTINGS = dict(roads=True, grading=True, road_widths=False, airports=False, sea=False, roads_in_ground=False)
SOURCES = dict(pipeline=2, overture="synthetic-fixture", worldcover="synthetic-fixture",
               canopy="synthetic-fixture", rails="synthetic-fixture", lanes="synthetic-fixture", gradesep=None)
GEOMETRY_CELLS = [[10, 511 + x, 340 + y] for y in range(2) for x in range(2)]


def environment():
    for name in ("BNGE_ASSIST_CONFIG", "BNGE_WORLD_BUNDLE", "BNGE_ASSIST_WORKER", "BNGE_ASSIST_OFFLINE"):
        os.environ.pop(name, None)
    os.environ["BNGE_PLAN_RUN"] = "assistance-benchmark"
    os.environ["BNE_REVISIT"] = "off"
    import revisit
    revisit.ON = False


class FixtureDEM:
    def __init__(self):
        self.calls = 0

    def sample(self, lat, lon, zoom=16, **kwargs):
        import numpy as np
        self.calls += 1
        return 10.0 + 2 * np.sin(np.asarray(lat) * 4500) * np.cos(np.asarray(lon) * 5000)


def fixture_md():
    from test_grading import FakeMD
    from test_roads import features

    class Source(FakeMD):
        def versions(self):
            return dict(SOURCES)

        bundle_versions = versions
    return Source(features([[(-300, 20), (300, 20)], [(0, -300), (0, 300)]],
                           ["residential", "secondary"]))


class FixtureState:
    """Source adapters around unchanged PlanStore, Grader and PlanCache implementations."""
    def __init__(self, root):
        self.cache_dir = str(root)
        self.md, self.dem = fixture_md(), FixtureDEM()
        self.sea = self.airports = None
        self.graders = {}

    def anchor(self, aid, lat, lon):
        import geodesy
        import grading
        import plancache
        if aid not in self.graders:
            frame = geodesy.EnuFrame(lat, lon)
            grader = grading.Grader(frame, self.md, self.dem.sample)
            grader.shared = plancache.for_grader(self.cache_dir, grader, None, None)
            self.graders[aid] = grader
        return self.graders[aid], None


def samples():
    import numpy as np
    from test_roads import FRAME
    lat, lon, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 256), np.linspace(-30, 60, 256), 0.0)
    return lat, lon


def geometry_requests(state):
    import assistjobs
    import farfield
    assistjobs.register_context(state.cache_dir, state.md)
    layout = farfield.Layout(64, 512, True, (256,))
    return [assistjobs.make_request("far_global_geometry",
            (*cell, farfield.global_dem_zoom(cell[0], layout.cells),
             farfield.global_dem_zoom(cell[0] + 1, layout.cells), layout), SETTINGS, state.cache_dir)
            for cell in GEOMETRY_CELLS]


def plan_requests(state):
    import assistjobs
    import grading
    return [assistjobs.plan_request(key, SETTINGS, state.cache_dir, state.md)
            for key in sorted(grading.Grader._keys(*samples()))]


def build_local(state, frame):
    import blockworker
    import assistjobs
    import grading
    blockworker._s = state
    geometry = [blockworker.far_global_geometry(*r["cell"], r["zoom"], r["ref_zoom"], assistjobs._layout(r["layout"]))
                for r in geometry_requests(state)]
    grader = grading.Grader(frame, state.md, state.dem.sample)
    lat, lon = samples()
    heights = grader.apply(lat, lon, state.dem.sample(lat, lon))
    return geometry, heights


def build_fallback(state, frame, config):
    """Exercise the actual pool's network-failure fallback, not a benchmark substitute."""
    from dataclasses import replace
    import assistclient
    import assistjobs
    import blockworker
    import grading
    import peerwork
    blockworker._s = state
    requests = geometry_requests(state)
    client = assistclient.Client(replace(config, timeout_s=.5))
    pool = peerwork.SharedFarPool(ThreadPoolExecutor(1), [], SETTINGS, None,
                                  assistance=client, assist_cache_dir=state.cache_dir)
    try:
        geometry = [pool.submit(blockworker.far_global_geometry, *r["cell"], r["zoom"], r["ref_zoom"],
                                assistjobs._layout(r["layout"])).result(10) for r in requests]
        if pool.stats["assistRemote"] or not pool.stats["assistFailed"]:
            raise AssertionError("Unavailable helper did not exercise local fallback")
        grader = grading.Grader(frame, state.md, state.dem.sample)
        lat, lon = samples()
        return (geometry, grader.apply(lat, lon, state.dem.sample(lat, lon))), pool.stats["assistFailed"]
    finally:
        pool.shutdown(wait=True)


def assert_equal(actual, expected):
    import numpy as np
    import peerwork
    for a, b in zip(actual[0], expected[0]):
        for name in peerwork.BUILT_ARRAYS:
            np.testing.assert_array_equal(getattr(a, name), getattr(b, name), err_msg=name)
        for name in ("pos", "err", "sink", "skirt", "size"):
            np.testing.assert_array_equal(getattr(a, name), getattr(b, name), err_msg=name)
    np.testing.assert_array_equal(actual[1], expected[1])


def digest(result):
    import peerwork
    h = hashlib.sha256()
    for built in result[0]:
        for name in peerwork.BUILT_ARRAYS:
            value = getattr(built, name)
            if value is not None:
                h.update(name.encode() + str(value.shape).encode() + value.dtype.str.encode() + value.tobytes())
    return {"geometry_sha256": h.hexdigest(), "heights_sha256": hashlib.sha256(result[1].tobytes()).hexdigest()}


def child(conn, index, root, config_file):
    """A separate process makes process_time a true per-client CPU measurement."""
    try:
        environment()
        import numpy as np
        import assistclient
        import assistjobs
        import geodesy
        import grading
        import plancache
        import peerwork
        from test_roads import LAT, LON
        frame = geodesy.EnuFrame(LAT + .01 * index, LON + .01 * index)
        config = assistclient.Config.load(config_file)
        client = assistclient.Client(config)
        # Warm Python/native code paths outside timing, then discard these planners.
        # Otherwise baseline alone pays lazy imports and appears unfairly expensive.
        build_local(FixtureState(Path(root) / "untimed-code-warmup"), frame)
        assistjobs.identity()
        expected = None
        conn.send({"ready": True, "pid": os.getpid()})
        while True:
            phase = conn.recv()
            if phase == "stop":
                client.close()
                return
            state = FixtureState(Path(root) / phase)
            request_bytes = response_bytes = fallback = 0
            start, cpu = time.perf_counter(), time.process_time()
            if phase in ("baseline", "fallback"):
                if phase == "fallback":
                    result, fallback = build_fallback(state, frame, config)
                else:
                    result = build_local(state, frame)
                    expected = result
            else:
                geometry = []
                cache = plancache.PlanCache(str(Path(root) / phase), "w0s0")

                def fetch(request):
                    nonlocal request_bytes, response_bytes
                    head, blobs = client.call(request, timeout_s=5)
                    # Application frame bytes: includes hello and job framing, excludes TLS/TCP overhead.
                    hello = {"op": "hello", "protocol": 1, "token": client._token, "identity": assistjobs.identity()}
                    request_bytes += len(peerwork.encode_msg(hello)) + len(peerwork.encode_msg(request))
                    response_bytes += len(peerwork.encode_msg(head, blobs))
                    return assistjobs.decode_result(request["op"], head, blobs, request)
                for request in geometry_requests(state):
                    geometry.append(fetch(request))
                for request in plan_requests(state):
                    plans = fetch(request)
                    if not cache.store(request["cell"], *plans):
                        raise AssertionError("Client plan installation failed")
                grader = grading.Grader(frame, state.md, state.dem.sample)
                grader.shared = cache
                lat, lon = samples()
                before = state.dem.calls
                base = state.dem.sample(lat, lon)
                heights = grader.apply(lat, lon, base)
                if state.md.calls or state.dem.calls != before + 1:
                    raise AssertionError("Assisted client rebuilt plans from geographic inputs")
                result = geometry, heights
            elapsed, used = (time.perf_counter() - start) * 1000, (time.process_time() - cpu) * 1000
            assert_equal(result, expected)
            if not np.any(result[1] != FixtureDEM().sample(*samples())):
                raise AssertionError("Road fixture did not exercise grading")
            conn.send({"phase": phase, "wall_ms": elapsed, "cpu_ms": used,
                       "request_frame_bytes": request_bytes, "response_job_frame_bytes": response_bytes,
                       "source_road_calls": state.md.calls, "source_dem_calls": state.dem.calls,
                       "fallback": fallback, **digest(result)})
    except BaseException:
        conn.send({"error": traceback.format_exc()})
    finally:
        conn.close()


def receive(conn):
    if not conn.poll(45):
        raise TimeoutError("Benchmark child exceeded its bounded deadline")
    result = conn.recv()
    if "error" in result:
        raise RuntimeError(result["error"])
    return result


def make_cert(root):
    openssl = shutil.which("openssl")
    if not openssl and Path("C:/Program Files/Git/usr/bin/openssl.exe").exists():
        openssl = "C:/Program Files/Git/usr/bin/openssl.exe"
    if not openssl:
        raise RuntimeError("OpenSSL is required to generate an ephemeral local test certificate")
    cert, key = root / "cert.pem", root / "key.pem"
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=IP:127.0.0.1"], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    return cert, key


def run_once(root, cert, key):
    import assistjobs
    import assistservice
    import blockworker
    import peerwork
    warmup = FixtureState(root / "helper-code-warmup")
    assistjobs._cache_dir, assistjobs._settings = warmup.cache_dir, dict(SETTINGS)
    assistjobs.register_context(warmup.cache_dir, warmup.md)
    blockworker._s = warmup
    for request in geometry_requests(warmup) + plan_requests(warmup):
        head, blobs = assistjobs.run(request)
        assistjobs.decode_result(request["op"], head, blobs, request)
    state = FixtureState(root / "helper")
    assistjobs._cache_dir, assistjobs._settings = state.cache_dir, dict(SETTINGS)
    assistjobs.register_context(state.cache_dir, state.md)
    blockworker._s = state
    token = "benchmark-only-not-a-production-credential-0123456789"
    token_file = root / "token"
    token_file.write_text(token)
    wire_lock, wire = threading.Lock(), {"sent": 0}

    class Server(assistservice.AssistanceServer):
        def send(self, sock, header, blobs):
            size = len(peerwork.encode_msg(header, blobs))
            with wire_lock:
                wire["sent"] += size
            try:
                super().send(sock, header, blobs)
            except BaseException:
                with wire_lock:
                    wire["sent"] -= size
                raise

    executor = ThreadPoolExecutor(1)
    broker = assistservice.Broker(SETTINGS, state.cache_dir, workers=1, executor=executor,
                                  runner=assistjobs.run, validator=assistjobs.validate_request,
                                  identity=assistjobs.identity())
    server = Server(("127.0.0.1", 0), token, broker, assistservice.tls_context(cert, key), request_wait=4)
    server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
    server_thread.start()
    config = root / "client.json"
    config.write_text(json.dumps({"schema": 1, "host": "127.0.0.1", "port": server.server_address[1],
                                 "ca_file": str(cert), "token_file": str(token_file), "slots": 1, "timeout_s": 5}))
    processes, pipes = [], []
    ctx = multiprocessing.get_context("spawn")
    stopped = False
    try:
        for index in range(2):
            parent, child_pipe = ctx.Pipe()
            process = ctx.Process(target=child, args=(child_pipe, index, root / f"client-{index}", str(config)))
            process.start()
            child_pipe.close()
            processes.append(process)
            pipes.append(parent)
        pids = [receive(pipe)["pid"] for pipe in pipes]
        report = {"client_pids": pids, "phases": {}}
        for phase in ("baseline", "shared", "warm", "fallback"):
            if phase == "fallback":
                server.shutdown()
                server.server_close()
                server_thread.join(2)
                stopped = True
            cpu = time.process_time()
            before_wire = wire["sent"]
            before_jobs = broker.snapshot()
            clients = []
            # Sequential requests expose the first client's cold service and the second's reuse.
            for pipe in pipes:
                pipe.send(phase)
                clients.append(receive(pipe))
            helper_cpu = (time.process_time() - cpu) * 1000
            after_jobs = broker.snapshot()
            report["phases"][phase] = {"clients": clients, "helper_process_cpu_ms": helper_cpu,
                "total_process_cpu_ms": helper_cpu + sum(c["cpu_ms"] for c in clients),
                "server_frame_bytes": wire["sent"] - before_wire,
                "client_frame_bytes": sum(c["request_frame_bytes"] for c in clients),
                "jobs_submitted": after_jobs["submitted"] - before_jobs["submitted"],
                "cache_hits": after_jobs["cache_hits"] - before_jobs["cache_hits"]}
        all_clients = [c for phase in report["phases"].values() for c in phase["clients"]]
        for key_name in ("geometry_sha256", "heights_sha256"):
            if len({c[key_name] for c in all_clients}) != 1:
                raise AssertionError("Cross-client output mismatch: " + key_name)
        report["verified"] = dict(equal_geometry=True, equal_graded_heights=True,
                                   different_anchors=True, independent_client_processes=True,
                                   fallback_local=True)
        return report
    finally:
        for pipe, process in zip(pipes, processes):
            try:
                if process.is_alive():
                    pipe.send("stop")
            except (OSError, EOFError):
                pass
            process.join(3)
            if process.is_alive():
                process.terminate()
                process.join(2)
            pipe.close()
        if not stopped:
            server.shutdown()
            server.server_close()
            server_thread.join(2)
        broker.close()
        executor.shutdown(wait=True, cancel_futures=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.iterations <= 5:
        parser.error("--iterations must be between 1 and 5")
    environment()
    import fast
    import numpy as np
    import assistjobs
    import peerwork
    fast.require(fast.MIN_VERSION, "assistance benchmark")
    with tempfile.TemporaryDirectory(prefix="earth-assistance-benchmark-") as tmp:
        root = Path(tmp)
        cert, key = make_cert(root)
        runs = []
        for iteration in range(args.iterations):
            directory = root / str(iteration)
            directory.mkdir()
            runs.append(run_once(directory, cert, key))
    for field in ("geometry_sha256", "heights_sha256"):
        if len({r["phases"]["baseline"]["clients"][0][field] for r in runs}) != 1:
            raise AssertionError("Output changed between iterations: " + field)
    medians = {}
    for phase in ("baseline", "shared", "warm", "fallback"):
        rows = [r["phases"][phase] for r in runs]
        medians[phase] = {name: statistics.median(r[name] for r in rows)
                         for name in ("helper_process_cpu_ms", "total_process_cpu_ms", "server_frame_bytes",
                                      "client_frame_bytes", "jobs_submitted", "cache_hits")}
        medians[phase]["client_cpu_ms"] = [statistics.median(r["clients"][i]["cpu_ms"] for r in rows) for i in range(2)]
        medians[phase]["client_wall_ms"] = [statistics.median(r["clients"][i]["wall_ms"] for r in rows) for i in range(2)]
    result = {"schema": 1, "kind": "synthetic-native-TLS-assistance", "iterations": args.iterations,
              "environment": {"python": platform.python_version(), "platform": platform.platform(),
                              "logical_cpus": os.cpu_count(), "processor": platform.processor(),
                              "native": fast._ef.VERSION, "numpy": np.__version__,
                              "source_id": peerwork.source_id(), "generator_identity": assistjobs.identity(),
                              "native_sha256": hashlib.sha256(Path(fast._ef.__file__).read_bytes()).hexdigest(),
                              "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
              "fixture": {"terrain_tiles": GEOMETRY_CELLS, "cells_per_edge": 64, "road_sample_count": 256,
                          "roads": 2, "synthetic_dem": "10 + 2*sin(lat*4500)*cos(lon*5000)",
                          "helper_workers": 1, "sequential_clients": True},
              "limits": ["No external provider downloads, BeamNG, FPS, low-spec hardware or public-network measurements.",
                         "Helper process CPU includes TLS server, native job worker and benchmark coordination.",
                         "Application frame bytes include handshakes but exclude TLS/TCP overhead.",
                         "OS filesystem caches are not flushed; cold means empty application reply/plan caches.",
                         "Python startup, certificate generation and client TLS context initialization are excluded.",
                         "Python/native code paths are warmed before timing; data/planner/reply caches start empty.",
                         "Unavailable-helper probe uses a 0.5 s request deadline and the actual SharedFarPool fallback.",
                         "Warm repeats re-fetch remote results into fresh client plan caches; not local disk reuse."],
              "median": medians, "runs": runs}
    text = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
