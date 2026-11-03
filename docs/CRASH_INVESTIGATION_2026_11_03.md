# High-speed driving investigation: 3 November 2026

The 1.8.4-dev.2 source candidate contains three reproducible corrections and better diagnostic measurements. None of the native game crashes is yet verified fixed. The running game, installed mod, server, graphics settings, and generated world files were not changed during the investigation.

## Captured evidence

Two driving captures and both native crash bundles were preserved locally. A third, three-second recording was entirely paused. The observer stopped after the player took a break. Raw captures, positions, logs, and dumps remain in ignored local investigation storage; they are not release assets.

| Observation | What it supports | What it does not prove |
| --- | --- | --- |
| First run: a localized mound in retained terrain near a motorway merge | Conflicting mainline/ramp grading profiles; reproduced offline | The bump caused the native crash |
| First crash: null read at game RVA `0xA7CCB4`, address `0x88` | Code and RTTI identify a TerrainBlock reflection-rendering path and a null TerrainCellMaterial-related argument | The precise resource mutation responsible, GPU failure, or an out-of-memory condition |
| Second crash: read access violation at RVA `0x990043`, matching the preserved 1.8.2 signature | Constructor/global references identify an SFX3DWorld update path; different from the first crash | Which streamed sound object, if any, caused the fault |
| Second crash about 277 ms after rebase phase 2, with a 247 ms frame | A world-origin transition is a concrete investigation target | Every rebase crashes, or the transition alone caused this fault |
| Second run's last sampled transition: current far tiles 534 to 185, old tiles 0 to 763 | Objects changed ownership categories during transition | All those old objects were deleted or invisible |

The approximately fourteen-second gap after the last gameplay sample largely overlaps native crash reporting. Companion backlog during that interval is not a pre-crash load measurement. The earlier reported peak of 314 includes this interval; the final minute before the final gameplay sample peaked at 163 in the independent server observations.

The last three approximately ten-second windows in the second drive averaged 48.8, 44.2, and 39.5 FPS while reported driving speed increased. That is an observed decline, not a controlled benchmark. Game resident memory in the final minute stayed around 4.20–4.27 GiB. Companion/worker memory grew; the recording is too short to establish a leak.

## Implemented corrections

### Motorway ramps respect mainline elevation

`grading.road_rank()` now recognizes the map parser's separate `Spec.link` flag, as well as legacy `_link` class names. Previously, the junction solver treated a ramp near a motorway segment endpoint as an equal-priority continuation and left its profile approximately three metres too high.

The replay reproduces the retained surface to millimetre-scale agreement on the analyzed path. Correcting the ramp priority changes the reconstructed maximum rising grade from 20.82% to 3.26% while preserving the mainline profiles. These are geometry measurements, not FPS measurements. [Junction investigation and validation](I95_JUNCTION_FIX.md) records the method and limits. Existing source fingerprints select fresh generated-data cache keys after rebuilding and restarting; downloaded source geography remains reusable.

### Retained terrain uses the incoming block's coverage

`farfield.switchAnchor()` applied coverage to retained global terrain tiles before installing the incoming terrain block bounds. A tile could retain its old hidden/sunk state until another terrain-follow commit. The new order installs those bounds before applying coverage to retained global and old tiles.

Regression tests cover both newly exposed and newly covered tiles in terrain and detail modes. This is a reproducible visibility defect consistent with missing scenery during transitions; it is not proof that it explains every bare-terrain episode or a native access violation.

### Streamed sound objects retain the correct kind

`ambience.placeSpaces()` formerly reused sound objects whenever the row count matched, even if a tunnel became a bridge, a sound space became a point emitter, or a point's sound kind changed. It now checks supported row kinds and object existence before remapping, rebuilding an incompatible set and preserving compatible objects without extra churn.

Offline tests verify replacement, missing-object recovery, unchanged-object reuse, rebase geometry, and cleanup. Stock scripts use similar engine creation/deletion operations; the evidence does not establish that ordinary deletion itself is invalid or that this correction fixes the SFX3DWorld crash.

## Diagnostic corrections

- Record signed vertical velocity, explicit terrain-bound membership, guard decisions/attempts, and tracked streamed-audio lifecycle counts without extra physics queries or scene scans.
- Record monotonic wall ages alongside legacy engine-clock ages; the latter can undercount a stall. Scheduling behavior is unchanged.
- Clear cached queue observations across capture and level resets.
- Qualify player observations by their world-origin frame. Omit spatial road/boundary measurements when an origin move makes the cached position stale. The published dev.1 capture could report false zero lead on that transition frame; those zeros alone do not prove missing ground.
- Count reported velocities above 1,000 MPH as diagnostic outlier windows and exclude them from qualified high-speed exposure. Preserve raw reported values. The second recording contains nine such windows, often near resets/pauses; their cause remains unproven.

See [telemetry measurements and limits](TELEMETRY.md). Collection remains opt-in, with existing bounds and privacy filtering.

## Is telemetry overloading the game?

In the second run's final minute, measured client capture work averaged 1.68 ms per approximately one-second window, with no client/sink drops or pending telemetry network frames. Background resource sampling averaged 29.9 ms and reached 400.8 ms in its slowest observation. These results show no recorder saturation, but do not rule out indirect CPU/I/O contention. An otherwise comparable capture-on/capture-off run remains necessary. No claim of zero overhead is made.

## Remaining validation

The candidate requires a fresh engine session. First inspect the corrected junction at ordinary road speed. Then repeat the same high-speed route, vehicle, renderer, and graphics settings; preserve capture and dump together if it crashes. Compare capture on/off separately. A persistent crash with the same native signature warrants targeted TerrainBlock/material or positional-audio isolation in a subsequent build, rather than disabling unrelated systems by guesswork.

Automated tests and replay validate the specific source corrections. They do not validate native engine lifetime, sustained driving stability, FPS improvement, or crash elimination. Release notes must retain that distinction.

## Local candidate validation

The Windows candidate was built from source commit `a386700081cd47b84f1f3210717beeeb28f48e99`, with version `1.8.4-dev.2`. It has not been installed into the running game or published as a GitHub release.

- `python tools/quality_gate.py --par 3 --timeout 240`: passed 43 Python modules (685 cases: 665 passed, 20 explicit platform/data skips), all 74 Lua modules, Node checks, four Rust tests, and source/BeamMP package creation.
- Additional focused road/revisit/telemetry checks: 177 cases passed. New grading, coverage-order, and audio-remapping regressions passed; the applicable grading and coverage regressions were demonstrated failing before their fixes.
- The exact source-built native module bundled in the executable passed 45 grading and seven native-kernel tests. Native source/build inputs matched the previous verified build; the original tracked binary was restored after packaging.
- The source-built Windows bootloader was required. Frozen `--server --help` and `--inspect-capture` exited successfully. The frozen analyzer matched the source analyzer on a preserved capture. Packaged version metadata, required Python modules, changed Lua bytes, and native module digest were checked.

Local executable: `dist/1.8.4-dev.2/BeamNG.Earth-1.8.4-dev.2.exe`, 57,289,878 bytes.

SHA-256: `5b5feca97f56482a5615bd8184087be2feb63c294ae57a29b361c77bb1cedf55`.

Linux execution, new gameplay, native crash elimination, and performance A/B validation remain outstanding for this candidate. The previous published release and PR have not been changed by this local investigation.
