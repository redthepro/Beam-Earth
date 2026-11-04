# Server assistance: native generation over TLS, November 3, 2026

This small synthetic workload was faster locally. Server assistance reused work
correctly, but TLS, validation and plan installation cost more than generating
these four terrain tiles and two road cells on this desktop. These measurements
do not establish a gameplay or low-spec-client speedup.

## Reproduction and boundaries

```sh
python tools/benchmark_assistance.py --iterations 5 --output build/assistance-benchmark.json
```

The tool requires the development Python dependencies, the compiled `earthfast`
module, test fixtures, and OpenSSL to generate an ephemeral loopback certificate.
It accepts one to five iterations. It does not use the player's cache, game, live
server, credentials, or geographic providers. Temporary credentials and files
are removed after execution.

Two independent spawned client processes use different geographic anchor frames
and separate caches. The helper runs the real `assistjobs.validate_request`,
`assistjobs.run` and `assistjobs.decode_result` path through `AssistanceServer`
and the verified-TLS `Client`. The helper has one execution worker. Input adapters
provide synthetic road features and elevation; geometry and grading results are
not mocked. Native terrain generation, road planning, completion provenance,
artifact hashes, array validation and client grade application all run normally.

The workload contains four global terrain tiles with 64 cells per edge, two
crossing roads, and 256 geographic ground samples. Its elevation function is
`10 + 2*sin(latitude*4500)*cos(longitude*5000)`. The terrain tile fixture and road
fixture occupy separate geographic areas; this is a component benchmark, not a
continuous driving route.

Each client first generates its own baseline. The first assisted client then
requests a cold helper; the second reuses those results. Both repeat against the
warm helper, installing plans into fresh client cache directories. Finally the
helper is stopped and the actual `SharedFarPool` fallback builds locally with a
0.5-second failure deadline. Clients run sequentially so first-client production
and subsequent-client reuse are distinguishable.

Python/native code paths are warmed before measurement, then their planner and
application caches are discarded. Process startup, certificate creation and
initial client TLS-context setup are excluded. Connection handshakes, transfer,
result validation and plan installation remain inside assisted timings. OS
filesystem caches are not flushed. Warm repeats still transfer data; they do not
represent a client's already-installed local cache.

## Five-run results

Median elapsed milliseconds:

| Phase | Client 1 | Client 2 |
|---|---:|---:|
| Independent local generation | 20.66 | 20.78 |
| First assisted pair: cold helper, then reuse | 140.97 | 104.94 |
| Warm helper, fresh client cache directories | 94.01 | 101.12 |
| Helper stopped; local fallback | 523.49 | 532.07 |

Median process CPU milliseconds:

| Phase | Client 1 | Client 2 | Helper/driver process | Total across processes |
|---|---:|---:|---:|---:|
| Independent local | 15.625 | 31.250 | 0.000 | 46.875 |
| First assisted pair | 15.625 | 15.625 | 62.500 | 109.375 |
| Warm helper | 15.625 | 31.250 | 15.625 | 46.875 |
| Helper stopped | 46.875 | 15.625 | 0.000 | 62.500 |

Client CPU uses `time.process_time()` inside separate processes, including their
threads. Helper CPU includes its TLS service, generation worker and benchmark
coordination. These Windows CPU readings were quantized in 15.625 ms increments;
they cannot support a claim about small CPU savings. Each table entry is its own
median, so component medians need not sum to the total median.

For each assisted pair:

- First pair: six helper jobs generated and six cache hits. Warm pair: zero new
  jobs and twelve cache hits.
- 186,496 application-frame bytes sent by the helper and 7,664 by the clients:
  194,160 bytes total. This includes protocol handshakes, not TLS/TCP overhead.
- Client source-road calls fell from 19 per local client to zero per assisted
  client. Synthetic DEM calls fell from 13 to one; clients still sample their
  local base ground before applying the shared plans. These are in-memory source
  calls, not measured internet requests or bandwidth savings.

All five runs verified identical native geometry arrays and graded geographic
heights for both anchor frames, both assisted phases and helper-offline fallback.
The graded heights differ from raw ground, so the fixture exercises real grading.
Output SHA-256 values were identical across runs:

```text
geometry f01aa8130881b100b2c9f4cacf03e388af256cf2210a9d2b6cc8be33c01bb71e
heights  13b32841c49eb0504ad9617240421fec45ea04799190ac4abc52709aba1d0f1f
```

## Exact measured source

Measurement used an isolated filesystem snapshot while development continued.
It does not claim to represent a later working-tree HEAD. Snapshot evidence and
raw five-run JSON remain under the local ignored `build/` directory.

```text
Python                         3.14.7
NumPy                          2.5.4
Platform                       Windows 11 build 26200; 16 logical processors
Native module version          116
Python generator source ID     1571ed6e879ce686
Generator identity             1571ed6e879ce686-116-599dde557875ff50
Native module SHA-256           b7c8c19791937f8faa753815cfce38241ce9a62cda882fa6b290cd40c329e221
Benchmark script SHA-256        b438a974f4cc4eb3b1dd30df069927927b96f5df512ffea96e91969bf081ad88
```

The native binary is the existing compiled module, not a newly optimized build.
The generated JSON records the environment, fingerprints, every client timing,
wire-byte counts, cache counts and output hashes.

## Implication for deployment

Keep assistance opt-in while measuring a real cold city and a constrained client.
These results support shared-work correctness and safe fallback, but do not
justify routing every cheap job remotely. A useful next comparison must measure
the same region with and without assistance, including provider waits, local CPU
contention, server preparation time and client frame spikes. CPU saved on a weak
client and a warm prepared route are different questions from this desktop's
20 ms synthetic job.

The outage result also exposes the waiting cost: local geometry remained correct,
but roughly the configured 0.5-second timeout was added. The normal application's
configured deadline may be longer. Prefetching and a warmed helper should be
evaluated before making foreground work depend on remote turnaround. Engine
rendering, collision installation, multiplayer load, outside-network behavior and
long-session stability were not measured here.
