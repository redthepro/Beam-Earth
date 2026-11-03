# 1.8.4-dev.3 high-speed test candidate

This candidate corrects a reproduced terrain-request loop and a separate texture-lifetime defect, and adds more precise streaming timings. Native game crash resolution and gameplay performance improvements remain unverified.

## Terrain kept ahead without repeated request/discard cycles

The client could request a server-cached terrain block, reject it because it differed from the geometric lookahead target, then immediately request the same cached block again. Preparation and rechecking now use the same effective target. Existing unsafe-block rejection, late-answer handling, commit gates, and edge rules remain in place.

The same ten-frame offline replay changes from five requests, five discards, and no commits to one request, no discards, and one commit. Simulated cache-present drives at 100 and 200 m/s cover 52 headings and two response latencies. See [the reproduction and validation](FOLLOW_READY_FIX.md). These measurements describe the scheduling rule, not an FPS improvement.

## Keep textures while a client still holds their geometry

Under cache pressure after the ten-minute retirement grace, the server could remove a cached tile and its DDS textures even though a client still held the associated pooled shape. The shape-slot allocator already prevented that held shape from being reused; the textures needed the same protection.

The cache now treats tiles owning existing held material slots as in use. This uses the existing hold acknowledgements and serials, with no additional retention timer. Unrelated idle tiles still evict. The protected tile becomes eligible again after every matching hold is released. Tests exercise local and global tiles, multiple texture revisions, two clients, stale release serials, and eventual eviction.

This is a reproduced long-session safety defect. It does not explain the most recent dev.2 crash: that run and its final anchor were younger than the retirement grace.

## More precise timing

Capture now separates gas-station processing from the road timer and records the road module's existing stage timings. The `road_*` values are subdivisions of the parent `roads` timer, not extra costs to add to it. The sea/trees/ambience timer is renamed `sea_trees` because the old `sea+trees` label was rejected by the capture's strict timer-name filter. No additional physics queries or scene scans are introduced.

The dev.2 capture recorded 381.1 seconds of measured gameplay, including 247.8 seconds of qualified reported-200+ MPH windows and two completed origin shifts. Its final sample was inside the terrain block with road coverage ahead. Different routes, speeds, pauses, and cache conditions prevent a controlled reliability comparison with dev.1.

## Crash evidence and next drive

BeamNG uploaded and removed the dev.2 primary crash report before the original local observer copied it. The surviving Windows dump belongs to a secondary CEF StorageService process and cannot identify the simulation's primary fault. No claim is made that this candidate fixes that native crash.

The separate local observer on the test machine now checks for reports every 0.2 seconds, immediately preserves candidate files, and distinguishes partial from structurally validated stable copies. It has bounded retention and a fixed expiry. This observer is separate from the executable's opt-in gameplay capture; it cannot recover a report already deleted.

For the next drive, start a fresh capture through Esc → BeamNG.Earth → Telemetry. Keep the game report/dump if offered and leave its dialog open until preservation is confirmed. Compare repeated terrain requests, terrain readiness, frame-time tails, and the new stage timings. A new primary dump is still needed if the game crashes again.

## Validation status

Focused regressions reproduce the request loop and held-texture eviction before their fixes and pass afterwards. Full candidate and executable checks are recorded after the build. This is a local development candidate; it does not change the published stable release.
