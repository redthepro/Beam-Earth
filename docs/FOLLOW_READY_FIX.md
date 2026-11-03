# Cached terrain request loop

Status: implemented and verified in offline Lua tests; awaiting an in-game drive.

During the 1.8.4-dev.2 high-speed drive, the companion answered 175 terrain requests for 112 distinct
block keys: 63 responses repeated an earlier key. Several bursts returned the same block five or more
times in under one second. These log counts identify wasted requests; they do not establish that every
repeat came from one defect, or explain the subsequent native crash.

The client can request a nearby block that the server has already prepared instead of the geometric
lookahead target. Its commit-time check compared that answer against the geometric target alone.
It could therefore discard the cached answer, then request that exact cached block again on the next
frame. A deterministic replay of the actual follow module reproduces the loop.

Prepare and commit-time checks now select the same effective target, including the ready cache.
A candidate that provides no improvement either now or at the hard-commit threshold is excluded
from cache selection using the existing pending-block rejection rule. This preserves the ability
to replace a block beside or behind the route. Late-answer, fast-mode, uncovered-strip gating and
edge-distance rules are retained.

## Reproduction and measurements

`mod/tests/test_follow.lua` runs a ten-frame replay at 60 Hz, starting at x=193 m, moving east at
100 m/s. The current block is centred at (0, 0); the server advertises a ready block at (256, 0).
Responses become ready before the next frame, with 2 ms measured latency. The raw initial target
is (384, 0). A commit updates the current centre normally.

| Same ten-frame input | Requests | Discards | Commits |
| --- | ---: | ---: | ---: |
| Before | 5 | 5 | 0 |
| After | 1 | 0 | 1 |

Four new regression assertions failed against the previous source and passed with the fix. Additional
tests cover removal of the cached candidate, a turn beyond its reach, an unsuitable cached side-block,
closed commit gates, fast-mode and late answers.

The offline cache-present drives cover 52 headings, 6 km per drive, 100 and 200 m/s, and response
latencies of 0.3 and 1.2 s. Minimum distance inside the collidable terrain was 198 m at 100 m/s and
181 m at 200 m/s. Existing cache-empty straight and jittered drive checks retain their prior results.
These are rule simulations, not BeamNG frame-time or real network benchmarks.

Run from the repository root:

```text
python tools/run_lua_tests.py test_follow test_core_stubbed test_core_robustness
```

For engine validation, repeat a high-speed drive with debug capture enabled. Compare repeated
terrain-request keys and request/discard activity against the previous capture, and inspect terrain
waits, guard holds and frame times separately. Native-crash resolution and a gameplay performance
improvement remain unverified.
