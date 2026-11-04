# Recording a high-speed drive

The 1.8.4-dev.1 development build adds opt-in local telemetry for diagnosing
streaming problems and intermittent stutters. It does not change streaming budgets,
graphics settings, physics, or the existing world-generation algorithms. It is
based on 1.8.3; the native game crash remains unresolved.

Diagnostic `-dev` builds are separate GitHub prereleases. The regular release and
its download remain available, and its update check ignores these prereleases.
Capture remains off until explicitly started, even in the diagnostic build. The
release workflow also recognizes an explicit `-dev` version when run manually, so
it cannot accidentally publish that version as a regular release.

## Start a recording

1. Run the development player executable/AppImage and enter the Earth level.
2. Open **Esc → BeamNG.Earth → Telemetry → Start capture**.
3. Leave **Include geographic coordinates** off unless the route is relevant and
   you want its latitude/longitude recorded locally. This is a per-session choice.
4. Drive normally. **Ctrl+Shift+T** marks a noticeable problem without opening a
   menu; it is configurable under the game's Earth controls. The panel also has a
   **Mark issue** button. Marks only work during an active capture.
5. Use **Stop capture** when finished. Wait for the panel's completed status.

Capture starts only when the companion acknowledges it. A disconnect ends that
session; reconnecting does not silently resume recording. A full disk or capture
failure stops diagnostics without stopping gameplay. The panel reports errors and
dropped samples. Data is written approximately once a second, so a crash can lose
the current, unfinished frame window.

No MCP setup, Python installation, external monitoring client, screenshots, or
microphone access is required. The normal performance logs remain available.

## Files and reports

- Windows recordings: `%LOCALAPPDATA%\beamng-earth\telemetry`
- Linux recordings: `~/.cache/beamng-earth/telemetry`
- Files: `capture-<UTC time>-<random session id>.jsonl`
- Companion override: `--server --telemetry-dir PATH` (or launcher arguments after
  `--`, alongside the other server options).

The player download includes the analyzer. In PowerShell, for example:

```powershell
$capture = Get-ChildItem "$env:LOCALAPPDATA\beamng-earth\telemetry\capture-*.jsonl" |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
& '.\BeamNG.Earth-1.8.4-dev.1.exe' --inspect-capture $capture.FullName --out '.\drive-report.md'
```

Linux or a source checkout:

```sh
./BeamNG.Earth-1.8.4-dev.1-x86_64.AppImage --inspect-capture /path/to/capture.jsonl --out drive-report.md
python tools/inspect_capture.py /path/to/capture.jsonl --json --out drive-report.json
```

The report can read an active or abruptly interrupted file. It identifies missing
end records and malformed/truncated lines instead of declaring a successful run.
It excludes geographic coordinates even when the raw recording contains them.
Nothing is uploaded automatically. Share the generated report first; review raw
recordings and the separate game/server logs before sharing them.

Storage is limited to eight recording files, up to 64 MiB each. Starting a new
session removes the oldest matching capture files as needed. A session stops at
its size limit. The in-memory writer queue holds 128 data records plus a reserved
stop record; overload drops diagnostics and reports the count. Lua telemetry has
at most one queued packet and yields to existing gameplay messages.

## What it measures

| Measurement | Use and limits |
| --- | --- |
| Wall-clock frame histogram, average, p50/p95/p99, maximum, frames over 33/50/100 ms | One-second windows and aggregated histogram counts. Percentiles are approximate bucket upper bounds; overflow uses the observed maximum. Reports do not average percentiles or treat sample count as elapsed time. |
| Simulation time versus measured wall time | Includes paused intervals, with the paused-frame count shown separately. A low ratio during a pause is not evidence of a physics bottleneck. |
| Mod timing totals, sampled engine timers, worst-frame timers | Correlates measured work with a hitch. Engine samples are not GPU utilization or proof of causality. Work outside the timers remains unexplained. |
| Speed, road readiness ahead, block edge distance/time, load holds, terrain requests | Helps determine whether the car is outrunning prepared terrain/roads. Road readiness uses a straight-line probe up to 1,984 m (about 2 km) in 32 m steps, not the route planner or a guarantee of collision safety. A fully covered probe is a lower bound. Worst-frame readings include their age and probe-limit flag. |
| Road/terrain/tree queues, objects, slots, Lua heap, rebase/follow counts | Shows backlog and object churn alongside frame windows. Worst-frame queue context can be up to one sample old; cheap position/hold fields are refreshed. |
| Companion scheduling lag, tile/terrain jobs, process pool work/restarts, cache occupancy, socket output buffer | Distinguishes companion overload or stalled output from client work. Tile build timings summarize the bounded recent completion history. |
| Companion network request/byte/error counters | Cumulative counters for that Fetcher today; worker-process fetchers are not included. Counter resets at midnight are not negative traffic. These are not a whole-system throughput or cache-hit measurement. |
| Game, companion and worker CPU/memory/I/O/handles or file descriptors | Native OS counters sampled off the game and companion event threads. CPU 100% means one logical core. Summed resident memory can count shared pages more than once. Missing permissions/processes/metrics remain unknown. Game children such as CEF are excluded from the game group. |
| Capture overhead, dropped data, player issue markers | Helps distinguish a real measurement from gaps or collection overhead. |

Processes are rediscovered every five seconds with creation-time checks for PID
reuse. Windows and native Linux have implementations; identifying the Windows
game through Proton can be unavailable. No GPU hardware utilization/VRAM, precise
driver timings, native crash stack attribution, or automatic leak diagnosis is
claimed. A native crash still needs the game's crash report/minidump.

## Additional motion diagnostics (dev.2 and later)

The source candidate adds signed `vertical_speed_mps`, `block_inside`, and
`block_outside_m` (the largest axis distance beyond the terrain bounds). These
use existing player state and geometry without additional physics queries.
They distinguish an ascent/descent and an out-of-bounds position without recording
geographic coordinates. They do not measure tyre contact or height above ground.
The existing `ground_ready` is a logical loading flag, not a collision test; it
can remain true while a vehicle is outside the block.

`guard_mode` records the existing guard decision: 0 off, 1 clear, 2 warning,
3 hold, 4 airborne/no nearby ground. Unknown decisions are omitted. Hold/rescue
counters (guard decisions/attempts, not confirmed successful actions) and
`guard_failed` help distinguish intentional bypass from an error.
These observations do not change the guard's behavior.

`player_frame_current` verifies the cached player observation belongs to the
current world origin. Road lead and terrain-boundary measurements are omitted
while the origin is changing or the player observation belongs to the previous
origin. In 1.8.4-dev.1, that transition could produce false zero distances by
comparing old-frame player coordinates with new-frame terrain. A zero in that
version's rebase window alone is therefore not evidence of unloaded ground.

`queues_age_wall_s`, `terrain_wait_wall_s`, and `terrain_last_wall_s` use a
monotonic wall clock. The older fields without `_wall` retain their engine-clock
meaning for compatibility; engine `dtReal` can undercount long stalls. A pending
request's age includes time spent ready but waiting for a safe commit;
`terrain_pending_ready` distinguishes that state. The last response latency ends
when the matching ready message arrives. The analyzer prefers wall age when
available and labels the engine-clock fallback in older recordings.

These fields are absent from published 1.8.4-dev.1 recordings. They are present
in the local dev.2/dev.3 driving captures; those observations do not establish
native-crash resolution.

`audio_spaces_live/created/deleted/moved/replaced` describe streamed sound-space
and point-emitter objects managed by Earth. Counts exclude vehicle audio, rain,
room tone, and the engine's own audio objects. Live is a tracked count, reconciled
when normal work discovers a missing object; collection does not scan the scene.
Created/deleted/moved are cumulative operations; replaced counts new objects
created for an already tracked tile. They show churn, not native object lifetime
or proof of an audio-engine crash cause.

## Repeatable I-95 comparison

Dev.3 and later separate `gasstations` from the `roads` frame timer. In
dev.1/dev.2, `roads` included both updates, so an old road-stage hitch does not
identify which one was expensive. While capture is active, `road_anchor`,
`road_ghosts`, `road_orphans`, `road_delete_chunk`, `road_held`,
`road_collide_range`, `road_walks`, `road_far`, and `road_collision` reuse the
road module's existing wall-clock stage timers. They are subdivisions of
`roads`: do not add them to the parent total. Recording them adds no physics
queries or scene scans. These fields narrow attribution on a future run; they
cannot recover missing detail from an older capture.

The combined sea/trees/ambience stage is now named `sea_trees`. Its previous
`sea+trees` label was rejected by the capture's timer-key filter, so it appeared
in performance logs but was absent from captured timer maps. The allowed-key
filter remains strict; the producer now uses a valid name.

Record two passes of the same Virginia section, one in each direction, with the
same car, graphics settings and target speed. At 240 MPH, the car covers about
107 m/s, so five seconds of road lead is about 536 m. Begin capture before the
high-speed section; mark stutters when safe, and include a brief parked interval
after the drive. Compare a first pass with a second pass through cached terrain.
Record the renderer and graphics preset alongside the report.

The useful comparisons are frame-time tails, hitch frequency, road-lead seconds,
queue growth, process memory trend, and whether a gap coincides with companion
scheduling lag. Changing one setting at a time makes those comparisons useful.
This build collects evidence for subsequent tuning; it does not establish an FPS
improvement or prove the crash is fixed.

## Dev-only event diagnostics (dev.4)

The launcher installs these probes inside the normal Earth Lua mod. A shared
`diagnostics` interface selects a capture provider in development packages and
a no-op provider in production. `tools/mod_package.py` generates the selection
from the version suffix for Windows/Linux frozen apps and both ZIP formats.
Production packages physically omit `diagnostics_capture.lua`. Installing one
over a development copy replaces the mod tree and removes the old provider.
Source checkouts default to the no-op provider; packaging does not edit them.
The existing aggregate capture remains opt-in. Detailed events additionally
require a development package; no runtime setting enables them in production.

During an active dev capture, ordered events record origin changes, large player
position discontinuities, scenery swaps/retirement and safety holds. A
`player_jump` records a displacement over 250 m between observations in the
same world frame. It does not identify intent: collision recovery, resetting
the car or moving it forward to the crash site can legitimately produce it.
Exclude those recovery intervals from uninterrupted-driving comparisons.

Events carry a capture-local frame, sequence and elapsed frame-duration clock.
The report keeps companion receipt time separate. At most 32 events are retained
per sample window; overflow is counted. Capture stops clear buffered events,
and the inspector retains only the most recent 64 events plus total counts.
Only allowlisted numeric/boolean fields and fixed event names are accepted.
No tile keys, coordinates, filenames or arbitrary text enter these events.
They use existing state, without scene scans or extra physics queries.

| Event | Numeric reason/phase interpretation |
| --- | --- |
| `frame_shift` | Phase 1 vehicle move requested; 2 scene committed; 3 explicit teleport committed; 4 probe completed (reason 1 destination observed, 2 original vehicle absent/changed); 5 confirmation timed out. A timeout keeps spatial decisions gated while physics, UI and network continue; an Earth teleport can supersede the failed move. |
| `player_jump` | Observed same-frame displacement; recovery/reset may explain it. |
| `far_rebase` | Reason 1 preserves old coverage, 2 teleport. `retained` counts live globals, `adds` pending globals, `waiting` retained reveals; `retired` here counts previous-generation coverage still retained, not objects deleted. |
| `far_swap` | Reason 1 instant, 2 rise over old coverage, 3 crossover; reason 4 reconciles late global intent and is not a completed reveal (`ready=false`). |
| `far_retire` | Reason 1 replacement covers old scenery, 2 explicit removal, 3 completed far-field generation. Counts describe logical retirement; engine deletion can follow later. |
| `drop_hold` | Reason 1 start, 2 player resumed, 3 placed on ground, 4 timeout, 5 ready/released. |
| `edge_guard` | Mode 0 off, 1 clear, 2 warning, 3 hold, 4 airborne; cumulative hold/rescue attempts accompany transitions. |

These are lifecycle observations, not a screenshot or proof that every rendered
pixel remained covered. The unfinished sample window can still be lost in a
native crash. Mark visible disappearances so their timing can be compared with
the event sequence.

## Streamed sound isolation (dev.5)

Development packages expose **Streamed environmental sounds (next level)** in
the Debug capture page. It defaults to on. For a controlled isolation run,
turn it off, return to the main menu, load Earth again, and start capture.
Changing it during a drive only records the requested next-level choice;
disconnecting/reconnecting does not delete or recreate the live audio scene.
The page distinguishes the active mode from a pending change.

This suppresses Earth-created streamed sound zones and point emitters. Vehicle
sounds, rain, river ribbons, forest wind and background ambience remain. Logical
tunnel/roof geometry is retained for tunnel lighting and rain shelter. The
capture's `audio_streamed_enabled` and `audio_change_pending` booleans identify
the actual mode alongside the sound-object lifecycle counters.

Use the same route and settings when comparing runs. An absence of crashes in
one mode does not prove the audio objects caused a native crash. The captured
native SFX dispatch faults do not identify the responsible object. Invalid
sound geometry is now rejected before native placement, but it has not been
demonstrated in those crash captures.

Production packages hide and reject this control and always request sound on,
even if a previous development run saved it off. Detailed capture remains
opt-in; this control does not start recording.

## Validation boundaries

Unit/stub tests cover bounded collection, packet backpressure, consent filtering,
session ownership, cancellation and restart, partial recordings, writer failures,
retention, histogram aggregation, and process lifecycle accounting. Synthetic
collector/sink overhead measurements exclude real engine work. The Windows
resource sampler is exercised against native OS counters. Linux CI and packaged
smoke tests are separate from live game validation.

A real sustained 240 MPH run, in-game UI/input behavior, renderer-specific engine
metrics, and crash-tail usefulness require player testing with this build.
