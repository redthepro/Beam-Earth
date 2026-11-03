# 1.8.4-dev.4: scenery continuity and development diagnostics

This local candidate addresses reproduced scenery handoff defects and adds
ordered diagnostic events for the next drive. It does not establish that every
reported disappearance or native game crash is fixed.

## Keep scenery through origin changes

The companion considers a global scenery tile shown after sending its swap,
but the client may still be waiting to import or reveal it. Changing origin
discarded those waiting swaps while retaining their hidden tiles. A stubbed
reproduction left a fully imported tile hidden after 600 update frames, with
no queued work capable of revealing it.

The client now preserves the final intended global reveals. If a replacement
also contained local geometry invalidated by the origin change, the visible
old surface stays until replacement coverage arrives. Withdrawn adds remain
cancelled; old local geometry is never replayed in the new coordinate frame.

Global tile/swap messages already in transit receive the same reconciliation.
Acceptance is limited to the immediately previous anchor in the same session,
before the first current-anchor far-field message. Connection lifecycle changes
close that window. Current selection and cancellation remain authoritative.

## Keep spatial decisions in one coordinate frame

The player was sampled before the world update, which could change origin
later in the same frame. Roads, scenery, safety checks and subsequent poses
could then consume coordinates belonging to the previous origin. Spatial work
now waits until a fresh player sample matches the committed frame and the
requested vehicle move has been observed. Network receiving and UI continue.

Three regression checks failed before the fix: same-frame origin change,
unconfirmed delayed movement, and the delayed second phase. Tests also check
normal resumption, no-car teleports, cancellation of an obsolete move by an
explicit teleport, and late confirmation after timeout. A confirmation timeout
is surfaced with recovery guidance; it does not silently authorize stale poses
or pause physics that may still need to apply the movement.

## Bound speculative warm-up work

Road and imagery warming previously extrapolated every finite velocity for
30 seconds without limiting distance. Two observed velocity outliers would
target 827 km and 11,002 km away in a controlled reproduction. The user's
crash recovery/reset movements may explain these outliers; they are not proof
of unrequested movement or a streaming-caused physics failure.

Speculative targets now stay within 12 km by shortening that prediction
horizon. Existing targets through 400 m/s are unchanged, including 240 MPH
driving and 300 m/s flight. Reported/gameplay velocities remain untouched.
This bounds geographic targeting, not the number of jobs, which was already
bounded. It is not a measured FPS improvement.

## Detailed capture only in development packages

A build-selected diagnostics interface records origin changes, player position
discontinuities, scenery reveals/retirements and safety holds. Production uses
a no-op provider and physically omits the capture provider. Both distributions
use the same gameplay implementation. The normal launcher installs the selected
mod, and replacing a dev install with production removes the diagnostic provider.

Events require an explicitly started capture, retain at most 32 entries per
sample window, and report overflow. No coordinates, paths or arbitrary strings
are accepted. The analyzer reports event counts and the latest 64 events.
See [capture instructions and event meanings](TELEMETRY.md).

## Evidence and remaining validation

The preceding dev.3 drive ended normally after about 947 measured gameplay
seconds, including 289 seconds of qualified reported-200+ MPH windows. The
same-block request loop seen in dev.2 was absent: zero consecutive duplicate
replies in dev.3 versus 63 in the earlier capture. Different routes, speeds,
pauses and cache conditions prevent a controlled performance comparison.

The user still observed scenery disappearing while driving normally. Separate
large-jump intervals can include crash recovery and position resets and must
not be treated as uninterrupted driving. Existing live counters describe
membership, not physical visibility. The new regressions prove specific
handoff defects; a new marked drive is needed to determine their impact.

Run the same Virginia route with unchanged car and graphics settings. Start
capture before driving and mark a visible disappearance with Ctrl+Shift+T.
Include any reset/recovery activity in the description. Native crash resolution,
gameplay performance gains, Linux execution and a full multiplayer drive remain
unverified. Local validation/build results are recorded below after completion.
