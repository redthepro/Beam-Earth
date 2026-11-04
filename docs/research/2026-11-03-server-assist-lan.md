# Server assistance: real LAN validation, 3 November 2026

The optional assistance service generated and transferred real terrain geometry and
road plans from Linux to a Windows client. Both clients used the actual native
generator and production protocol. The recipient checked source identity, settings,
artifact integrity and array structure before accepting each result. This proves
the tested offload path works; it does not establish an in-game FPS improvement or
capacity for 100 simultaneous players.

## Tested revision and environment

- Revision: `0a9a5f79`, before the subsequent bridge integration/final source sync.
- Generator identity on both ends:
  `94d864dad4c86419-116-599dde557875ff50`, native version `116`, protocol/schema `1`.
- Linux native library SHA-256:
  `f60e80743df158b7628954a94c79324ec5d90efa8d64916bc677ed7df9421b8f`.
- Server: RHEL 10.2, Python 3.12.15, eight CPUs and 16 GB RAM.
- Client: Windows, Python 3.14.7, on the same LAN. No simulation client was started
  or interrupted during this test.
- Settings: roads, grading, road widths, airports and sea enabled; roads in ground
  disabled. The matching public OurAirports snapshot was `oa-4840f1d52ff5`.
- The service had a private writable cache and read-only source/runtime. Only the
  public airport CSV snapshot and rail/lane source manifests were seeded from the
  client. No player cache or credentials were distributed as test artifacts.

## Method and measured results

Each probe created a separate authenticated TLS client, requested global terrain
geometry and a geographic road-plan cell, validated the received data and repeated
the identical request. The geometry layout used 32 cells per edge, a 512-sample
terrain block, mesh mode and a 256-pixel texture layout. Terrain requests used zoom-11 geographic cells; plans used
zoom-14 cells. Repeated replies were byte-identical to their first accepted reply.

The service returns `pending` after about three seconds while its bounded job keeps
running. The test waited two seconds between attempts. “First accepted” below is
wall time through the successful receipt, including those explicit retry waits;
it is not pure generation CPU time.

| Area and operation | First accepted | Pending replies | Repeated reply | Validated result |
| --- | ---: | ---: | ---: | --- |
| Long Beach terrain | 0.907 s | 0 | 63 ms | 158,100 bytes; 1,221 vertices; 2,304 triangles |
| Long Beach road plans | 5.124 s | 1 | 73 ms | 803,794 bytes; 685 plans |
| I-95 / Stafford, Virginia terrain | 0.674 s | 0 | 61 ms | 158,100 bytes; 1,221 vertices; 2,304 triangles |
| I-95 / Stafford, Virginia road plans | 30.419 s | 6 | 67 ms | 393,889 bytes; 250 plans |

Long Beach was at `(33.86605, -118.19534)`, terrain cell `(11, 351, 819)` and road
cell `(14, 2812, 6552)`. Its provider data was already warm from the preceding
diagnostic attempts, but the service had just restarted with an empty reply cache.
This is not a cold-source measurement.

The Virginia probe was at `(38.435, -77.410)`, terrain cell `(11, 583, 786)` and road
cell `(14, 4668, 6294)`.
It was the first request for that region on this helper. The cache already held
provider manifests, the airport snapshot and data from Long Beach. Its road data
was initially uncached; this was not an entirely empty-disk test. Each repeated
measurement was a service reply-cache hit, not a regeneration measurement. These
single observations are not statistically controlled local-versus-remote speedups.

After the four operations, server metrics showed four submitted and four completed
jobs, zero failed jobs, five joins onto existing jobs and six cache hits. No helper
automatic restart occurred. The restarted helper peaked at approximately 1.0 GiB
and used approximately 0.78 GiB after the tests; its disk cache was about 737 MiB.
The earlier diagnostic run peaked at 1.8 GiB. This small sequential workload does
not characterize four workers under sustained concurrency.

## A bug found by the real-data check

The first Long Beach road-plan transfer was safely refused as `incomplete_inputs`.
An isolated diagnostic worker confirmed zero source faults, a valid completion
marker and a successful plan write. The artifact validator rejected legitimate
native descending-cliff triples of the form `[finite_offset, NaN, NaN]`; it had
previously accepted only entirely finite or entirely NaN triples.

Revision `0a9a5f79` adds the exact native sentinel to the accepted schema while
rejecting other invalid mixed-NaN layouts and infinities. The real fixture had
857 cut rows and 65 legitimate descending sides. The fixed validator accepted all
685 plans, and the live TLS transfer above then passed. Sixty affected automated
tests passed before deployment, including a native sentinel regression. The
production validation boundary was not bypassed to make the test succeed.

## Isolation, access and limits

The helper is a separate systemd service using a non-login account. Its TLS listener
is bound to the server's LAN address on TCP 47623, with a source-subnet-only firewall
rule in both runtime and persistent configuration. No public/router port was opened.
The client pins the generated certificate and uses a private token file. Live
negative checks rejected invalid tokens, incompatible generator identities,
untrusted certificates and plaintext connections. Secrets were not printed or
included in the repository or release artifacts.

The configured limits were four worker processes, eight admitted unique jobs,
sixteen connection handlers, a 128 MiB reply cache, CPU quota 400%, MemoryHigh 6 GiB,
MemoryMax 8 GiB and TasksMax 512. Disk admission stops new work at a 6 GB budget;
this is not a filesystem quota, and in-flight writes can exceed it. Cache maintenance
remains necessary at that limit. The BeamMP service's memory limits were adjusted
live to MemoryHigh 3 GiB / MemoryMax 4 GiB, leaving operating-system headroom.
BeamMP and DNS retained their original running processes throughout deployment.

An initial Linux startup failure came from preserved SELinux home-directory labels
on copied runtime files. Restoring the installed paths' policy labels fixed the
startup, and the deployment procedure now includes that operation. The service then
passed the actual authenticated jobs described above.

## Remaining validation

Near collision installation, rendering and vehicle simulation remain on the client.
This service does not turn BeamMP into a remote simulation host. The checked source
metadata also does not represent an immutable snapshot of every raw elevation tile.

Still required: gameplay with assistance enabled, high-speed driving and
repositioning, long-session memory behavior,
controlled latency/loss tests against the deployed service, and concurrent-player
load tests. The 30-second first Virginia road request also demonstrates why predictive
work and shared reuse matter: a cold helper cannot guarantee immediate construction
at the vehicle's current location. Local fallback and admission limits remain active.

See [server assistance operations](../SERVER_ASSIST.md) for the supported protocol,
configuration and fallback behavior. A final code change must be synchronized to
both ends; mismatched identities intentionally decline offload.

## Final integrated source smoke check

The helper was synchronized again to `b8a2e3d0` after the bridge surface changes,
the geographic source-cell boundary fix and the additional assistance reliability
fixes. Exact source-file hashes were verified on installation, and the Linux native
library remained unchanged. The service alone was restarted; BeamMP and DNS retained
their original processes. The matching Windows probe accepted generator identity
`e786fccb4d4af1dd-116-599dde557875ff50` on both ends.

The same Virginia requests passed again with cached provider data and an empty
service reply cache after restart:

| Operation | First accepted wall time | Repeated identical reply | Validated result |
| --- | ---: | ---: | --- |
| Global terrain | 0.870 s | 55 ms | 158,100 bytes; 1,221 vertices; 2,304 triangles |
| Road plans | 1.106 s | 68 ms | 393,889 bytes; 250 plans |

There were no pending or failure responses. The terrain and plan artifact hashes
matched the prior Virginia results; the repeated replies were byte-identical.
Invalid token, incompatible identity, untrusted certificate and plaintext rejection
checks passed again. The helper remained active with zero automatic restarts,
roughly 225 MiB of current memory after this small warmed-data smoke test, and about
737 MiB of disk cache. These figures describe this request set, not sustained or
multiplayer peak use. The final code smoke check is complete; the gameplay and
capacity validation gaps listed above remain.
