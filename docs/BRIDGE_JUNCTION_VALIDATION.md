# Bridge junction collision correction

Status: **implemented and verified offline; BeamNG physical-drive validation pending**.

The reported Swiss junction near 46.66729 N, 7.72157 E had bridge slabs and
junction filler pads intersecting at different heights. Agreement at their
centreline endpoint did not make their overlapping triangles agree. One filler
pad also extended over a curved ground approach and ended in a concrete lip.

The correction gives eligible connected bridge branches, asphalt, markings and
filler pads one triangulated height surface. Slab sides and pad skirts split at
the same surface boundaries. It retains the road profiles' slopes and full
carriageway widths. At a matching ground-road endpoint, the filler pad is cut
out of the existing carriageway; the TerrainBlock remains its collider. Bridge
slabs retain collision. Separate stacked levels are not combined.

Complete profile metadata survives tile clipping and is transformed with the
road skin on an anchor change. Short-profile context extends far enough for
neighbouring tiles to reconstruct the same fork. Grid bounds extend from the
same analytic function when a pad or miter reaches beyond the initial grid;
outside points cannot wrap through a clamped boundary cell.

## Measured replay

Baseline: `d4e6c6f`. The small committed fixture is documented in
[bridge_fork_swiss.md](../server/tests/fixtures/bridge_fork_swiss.md). It is derived
from the actual cached geographic replay and rounded to millimetres. Tests
intersect triangle interiors along generated road centres and both tyre tracks.

| Compact replay metric | Before | After |
|---|---:|---:|
| Maximum overlapping top-face height spread | 0.36043 m | <0.000001 m |
| Samples with more than 1 cm overlap | 426 / 510 | 0 / 510 |
| Samples with bridge collision present | 510 / 510 | 510 / 510 |
| Wall protrusion above driving surface on those tracks | 0 m | 0 m |
| Mesh vertices | 2,117 | 5,085 |
| Mesh triangles | 1,947 | 6,899 |
| Median mesh construction, 11 warm repetitions here | 9.32 ms | 85.14 ms |

This is a deliberate construction/geometry cost for collision correctness,
applied during the local companion's tile preparation rather than a per-frame gameplay task.
It is **not** a loading-time or FPS improvement claim. The earlier experimental
0.5 m grid produced 46,485 vertices without fixing the approach pad; it is not
the shipped approach. The accepted surface uses a 2 m grid and shares vertices.

The larger local cached replay (unrounded profiles, original grading function)
measured maximum overlap falling from **0.36085 m** to numerical zero. Maximum
extra pad height above the primary connecting road fell from **0.62302 m** to
**0.000465 m**. Free-abutment centre heights remained unchanged within numerical
precision. At the old pad rim, the difference between samples 0.1 m apart fell
from **0.63380 m** to **0.01078 m**; the latter follows the underlying terrain,
not a remaining pad wall.

There is a separate unresolved terrain concern: the primary approach's analytic
graded height changes by **0.10377 m over 0.1 m**, 15.4 m from the abutment
(maximum derivative 1.03774). The corrected collision envelope equals the
underlying terrain at both samples, and there are **zero bridge/pad triangle
intersections** there. The secondary approach similarly retains a derivative
of 0.74262. These are derivatives of the analytic grader sampled at 0.1 m, not
measurements of the engine's quantized 2 m TerrainBlock or vehicle response.
They require a separate terrain replay and physical-drive check.

## Reproduce and limits

From a source checkout with the matching native module and Python dependencies:

```text
python -m unittest discover -s server/tests -p test_bridge_junction_surface.py -v
python server/tests/bench_bridge_junction.py --baseline-ref d4e6c6f
```

The focused scenarios cover the real fork and ground approach, skin/marking
agreement, sloped ramps and abutments, stacked forks and unrelated crossings,
adjacent tiles, reordered input, anchor transformations, outside-grid queries,
clipped long spans, ambiguous multipart identifiers, and legacy metadata.
The existing road mesh, bridge profile/clearance/types, Otto Road, short-deck,
road and geotile suites are also required before integration.

Eligibility is intentionally bounded: **3–8 incident short parts, each at most
60 m**, with matching endpoint position/elevation and complete unambiguous
profiles. Long spans, two-way bends, unrelated crossings, complex larger
networks and old cached inputs without the metadata retain their existing
behavior. This does not solve every bridge deformation or incorrect geographic
elevation. No native module changes or collision disabling are involved.

The `revisit.code_key()` source/CODE_ID fingerprint invalidates old `roads` and
`roadsg` content, including saved road-skin inputs, after integration/build.
Raw provider data does not need deletion. A packaged build must contain its new
CODE_ID; a running server must restart to use the new code/cache identity.

Before a release is called gameplay-verified, drive each Swiss branch and its
ground approach in both directions, check low-speed wheel contact first, then
repeat at normal road speed and across a tile boundary/rebase. Check stacked
bridges keep their guardrails, inspect visible/collision alignment, and capture
any residual bump. This patch does not establish or fix a native crash cause.
