# I-95 ramp junction correction

Status: implemented and verified in local tests and an offline replay of captured terrain inputs. A new game run with regenerated terrain is still required. This correction does not establish the cause of the native game crash.

## What went wrong

A motorway ramp near the captured I-95 route retained a road profile approximately 3 metres above the mainline where they joined. The terrain generator blended these conflicting profiles, creating a short mound in the mainline. At high driving speeds, even a short vertical change matters.

The junction solver is intended to preserve the main road's profile and bring the joining ramp to its height. However, `grading.road_rank()` recognized a ramp only when its class ended in `_link`. Map-derived `roads.Spec` values instead store `cls="motorway"` and `link=True`. Both roads therefore received the same priority. The native junction solver deliberately skips equal-priority continuations close to segment endpoints, so it left this ramp's height unchanged.

The fix recognizes `Spec.link` while preserving legacy `_link` class names and callers that supply only a class. It changes the joining ramp's priority; it does not flatten the mainline or apply stronger smoothing to every road.

## Evidence and validation

The investigation used the opt-in development capture, the first crash's preserved game log, four generated terrain blocks, nearby road meshes, the local map cell, and the original server process's grading plans. Private capture positions and generated assets remain in ignored local investigation storage rather than this repository.

The last four terrain blocks had identical heights in every shared grid region: six comparisons covering 49,152 to 229,376 samples each. The mound was within the final installed block, before the vehicle reached its boundary; these retained blocks do not show a height discontinuity introduced by a terrain-follow swap.

Replaying the three neighboring cached road plans with the original priority left all three unchanged. Calling the patched junction solver lowered the ramp's joining endpoint by 3.12685 metres while leaving both mainline profiles unchanged. The replay used the actual patched Python function and the existing native junction solver.

The reconstructed terrain used the original 2-metre grid spacing and 1/256-metre height quantum. Heights along the analyzed path agreed with the retained terrain within 8.9 mm, with a 3.1 mm root-mean-square difference. The local replay used a constant background elevation inside the road cores; this is a check of the junction's contribution, not a complete regeneration of the world.

| Surface along the analyzed path | Largest rising grade over a 1-metre interval |
| --- | ---: |
| Retained terrain from the game session | 20.49% |
| Offline reconstruction with the original ramp priority | 20.82% |
| Offline reconstruction with the corrected ramp priority | 3.26% |

These are geometry measurements, not frame-rate or loading-speed improvements. The path was interpolated between approximately one-second horizontal position samples; the capture did not record wheel contact, vehicle elevation, or vertical velocity. It cannot establish the exact impact point, airborne motion, or native crash cause.

Automated checks:

- Two new regressions failed against the original implementation and passed after the fix: map-derived ramp priority and a ramp meeting the endpoint of a motorway segment.
- The behavioral regression verifies the ramp meets the mainline, the mainline profiles remain unchanged, and the blended driving surface has no steep localized mound.
- Legacy `_link` classes, a class-only caller without a `link` attribute, and ordinary equal-priority road continuations are covered.
- `python -m unittest test_grading test_roads`, run from `server/tests`, passed all 121 tests locally.

## Cache behavior and live follow-up

No cache version bump is required. Persistent generated items and plans include `revisit.code_key()`, which fingerprints the server's Python sources and native module. Packaged builds embed the server source fingerprint as `CODE_ID` through `tools/build_app.py`. A rebuilt application therefore selects different generated-data keys; downloaded source geography remains reusable. In-process grading plans are scoped to the running server and require a restart to use new code.

The investigation did not replace the user's running executable, reload the active server, or edit generated world files. For engine validation, start the corrected build in a new session, return to the same junction at ordinary road speed first, and verify the road surface and vehicle response. Repeat the high-speed route with development capture only after that check. The telemetry still needs to distinguish ground contact, vertical motion, terrain availability, and collision-object lifecycle before attributing a native crash to any one of them.
