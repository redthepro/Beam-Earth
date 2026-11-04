# 1.8.4-dev.5 candidate: bridge junctions and optional server assistance

This development candidate targets reproduced bridge collision defects and lets
an opt-in client reuse work from a separate geographic helper. It requires a new
BeamNG driving test before either smoother gameplay or native-crash resolution
can be claimed. It is not a replacement for the stable release yet.

## Bridge correctness

Short connected bridge forks can have three road profiles that agree at the
junction point but produce different heights where their wide surfaces overlap.
The candidate reconciles those eligible forks onto one triangulated surface for
the visible road and collision. Complete profiles travel through tile clipping
and origin changes so neighbouring tiles derive the same surface.

Junction filler must not lay an elevated collision pad or wall across an existing
ground road. Separate fixes distinguish stacked bridge levels when making pads
and deciding where to remove parapets. This does not flatten all bridges or
replace the geographic height solver. Longer, incomplete or unsupported bridge
networks retain the existing generation path.

The regression fixture and measured construction cost are documented in the
[bridge validation report](BRIDGE_JUNCTION_VALIDATION.md). Physical vehicle response, other reported bridges,
and long-session behavior still require in-engine validation.

## Reuse server preparation

An optional authenticated TLS helper can prepare distant terrain geometry and
road-grading plans. Compatible clients share completed results instead of each
repeating that work. Road prediction extends up to 90 seconds along the current
travel direction, bounded by 12 km, a cell limit and a small background queue.
Local generation remains available when the helper is busy or incompatible.

The helper does not simulate vehicles, render the scene, install collision, or
mesh bridges for the player. Near collision geometry remains local. Known source
versions and generator settings must match; this is not a complete immutable
snapshot of all geographic inputs. The service has bounded admission, memory
caching, request deadlines and worker recovery, and is independent of BeamMP.

The real Virginia/I-95 LAN probe returned 250 road plans. The first successful
receipt took 30.419 seconds, including pending responses and retry waits with
region data initially uncached on the helper; a repeated identical reply took
67 ms. These are transfer/generation-path observations, not a controlled client
loading comparison. The small synthetic benchmark was slower remotely because
transport and validation cost more than its cheap local workload.

See [configuration and limits](SERVER_ASSIST.md), the
[repeatable component benchmark](research/2026-11-03-server-assistance.md), and
the [LAN validation report](research/2026-11-03-server-assist-lan.md).

## Reliability and development diagnostics

Shared road-plan validation now accepts the native generator's legitimate
descending-cliff constraint while still rejecting malformed partial values.
Client replies are checked against current source metadata before installation.
Completed jobs share a bounded cache; incomplete geographic inputs stay retryable.
Damaged local road-plan archives are treated as cache misses and rebuilt instead
of aborting grading. Source-mismatch refusals preserve the helper's warm state.

The candidate also carries the preceding development fixes for invalid ambient
audio placement, scoped level spawn filtering and water-object initialization.
A development-only streamed-audio isolation setting supports crash investigation;
it is not evidence that audio is the cause of the native crash. Production keeps
ordinary audio behavior and excludes the detailed capture provider.

Capture remains opt-in: **Esc → Earth → Debug capture → Start capture**. During a
comparison, use the same car, route and graphics settings, and identify any crash
recovery or position reset. Record helper use separately from bridge behavior.
Bridge collision needs testing at normal speed before another 200+ MPH run.

## Acceptance status

- Automated bridge geometry, road-plan validation and assistance regression
  results are recorded with the candidate's build evidence.
- Real Linux helper generation, authenticated TLS transfer and warm result reuse
  have been exercised over the LAN. The helper is not exposed through the router.
- Client frame-time gains, low-spec CPU savings, a 100-player load, public-network
  operation and native crash resolution are not verified by those checks.
- Frozen executable checks and local delivery must pass before this candidate is
  handed over. Source tests alone do not establish a working game session.
