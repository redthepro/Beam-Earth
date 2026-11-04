# 1.8.4-dev.5: bridge junctions and optional server assistance

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

## Automated validation before packaging

The foundation gate at `2eb192a4`/`219bb4a9` completed 49 Python modules:
752 passed and 20 explicit skips, plus all 78 offline/stubbed Lua modules,
two Node checks and four Rust server-list tests. The changed peer-work module
was rerun after that gate. Those results predate the final bridge integration.

At the combined bridge/assistance revision `0b221af2`, 16 affected Python modules
completed with 309 passed and five existing skips. After the geographic
source-boundary correction at `b8a2e3d0`, seven affected modules completed with
175 passed and one existing LuaJIT-unavailable skip. These overlapping test runs
must not be added together as a distinct-test total. The bridge report records
its broader 223-case regression run separately.

Versioned source and BeamMP plugin packages were generated successfully at
`b8a2e3d0`. They are package-construction checks, not native hosting executables
or evidence of a gameplay session. Windows frozen-build verification and the
final helper synchronization are recorded with the delivered artifact.

## Earlier local candidate and delivery

The local candidate below predates the final launcher correction described in the
next section. Its hash is retained as historical test evidence; it is **not the
published Windows download**. Use the prerelease's `SHA256SUMS.txt` for published
artifacts. The Windows executable is rebuilt for publication after the correction.

The Windows executable was compiled in an isolated checkout at
`01484f26f6e3b88ee25f56c33d5d1cca1ade39f8`. All 34 native/build input hashes
matched the previously source-built native module; its unchanged binary and
verified own bootloader were reused. The exact staged native binary passed
90 tests, including the new bridge and deck-pad scenarios.

Frozen validation checked versions, source identity, 14 Python code objects,
14 Lua files and both settings UI files against that commit. Server CLI help
and the capture inspector exited successfully, with source/frozen inspection
results equal. Development capture policy, production omission of the capture
provider and credential exclusion checks passed. The packaging checkout is
clean and its tracked native binary was restored. PyInstaller's existing optional
`duckdb.experimental`/`typing_extensions` collection warning remains; project
source does not use that experimental module.

`BeamNG.Earth-1.8.4-dev.5.exe`: **57,376,474 bytes**.

SHA-256: `c67df30abfc695c0ece51db65a3beaac46d58f41ebde2b606fd926bd76c7c0f8`.

A hash-matching copy, notes and a launcher for the privately configured LAN helper
were delivered to Downloads. The helper was synchronized and successfully served
the final-source Virginia jobs; see the LAN report. The executable has not been
automatically installed, launched for gameplay or published to GitHub.

A previous BeamNG process remained windowless after its log reported completed
shutdown. Its logs/process evidence were preserved and that specific leftover was
terminated before handoff. This cleanup is not a fix for the underlying shutdown
leak. The later terrain-grid replay also confirms a remaining steep approach;
see the bridge report before treating this candidate as gameplay-verified.

## Publication correction: preserve unknown Windows process status

Upstream PR review exposed a remaining launcher defect. A `tasklist` timeout
returned an empty process list, which the watcher interpreted as the game closing.
A reproducible fixture kept the game alive until 20 seconds but the old watcher
reported closure at eight seconds after two timed-out scans.

Process inspection now distinguishes present, confirmed absent and unknown.
Failed, empty or malformed scans reset the absence timers and preserve the world
server. Confirmed absence still uses the normal exit grace; the same fixture now
reports closure at 23 seconds. An actual world-server exit still ends the watcher.
Before startup mutations, three unsuccessful scans produce retry guidance without
changing the game, mod or existing services. A later confirmed result follows the
normal startup path.

The focused Windows lifecycle module passed all 26 tests. The cross-platform
launcher module ran 52 cases: 41 passed and 11 explicit platform skips on Windows.
This fixes a reproduced premature companion shutdown; it does not prove a fix for
native crashes or every windowless BeamNG process left after game shutdown.
