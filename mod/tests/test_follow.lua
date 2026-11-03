-- luajit mod/tests/test_follow.lua  (terrain-follow math and rule, V1_1_SPEC 1a, V1_2_DESIGN 1.2, 2.3)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local f = require('beamngEarth/follow')

-- block geometry: 1024 samples at exactly 2.0 m, collidable width (N-1)*s = 2046 m (P1)
T.check(f.SQUARE_SIZE == 2.0, 'squareSize = 2.0 (world-locked lattice)')
T.check((f.SIZE - 1) * f.SQUARE_SIZE == 2046, 'collidable width (N-1)*s = 2046 m')
T.check(f.REACH == 420, 'REACH = 1020 - 600 = 420 m (same as the server predict_centre)')
local x0, y0 = f.blockOrigin(512, -256)
T.check(x0 == -512 and y0 == -1280, 'block position = (cx-1024, cy-1024)')

-- snapping
T.check(f.snap(127) == 0 and f.snap(128) == 256 and f.snap(-128) == 0 and f.snap(-129) == -256, 'snap256')

-- target = snap256(player + v*4 s)
local tx, ty = f.target(300, 0, 0, 0)
T.check(tx == 256 and ty == 0, 'target at rest')
tx, ty = f.target(300, -50, 30, 0) -- 420 m ahead
T.check(tx == 512 and ty == 0, 'target leads by 4 s')
-- clamped so the player stays >= 600 m from every edge: |c - p| <= 420 per axis
-- (the collidable edges are at c - 1024 and c + 1022)
for _, c in ipairs({ { 0, 0, 200, 0 }, { 1000, 1000, 150, -150 }, { -333, 777, -90, 60 }, { 5, 5, 0, 400 },
  { 164, -164, 300, -300 }, { 676, 0, 500, 0 }, { -420.5, 0, -100, 0 } }) do
  local cx, cy = f.target(c[1], c[2], c[3], c[4])
  T.check(cx % 256 == 0 and cy % 256 == 0, 'target on the 256 m grid')
  T.check(math.abs(cx - c[1]) <= 420 and math.abs(cy - c[2]) <= 420,
    string.format('player (%g,%g) v=(%g,%g) >= 600 m inside block (%d,%d)', c[1], c[2], c[3], c[4], cx, cy))
  T.check(c[1] - (cx - 1024) >= 600 and (cx + 1022) - c[1] >= 600, 'x: >= 600 m from both collidable edges')
end
tx = f.target(0, 0, 200, 0) -- 800 m ahead -> clamped to 256 (<= 420)
T.check(tx == 256, 'fast player: clamped lead (' .. tx .. ')')
tx = f.target(0, 0, -200, 0)
T.check(tx == -256, 'fast player backwards: clamped lead')

-- rule
local d = f.decide
T.check(d(0, 0, 0, 0, nil, nil, nil, 0, 0) == nil, 'no block yet: nothing')
T.check(d(300, 0, 20, 0, 0, 0, nil, 0, 0) == nil, 'at 300 m: no request yet')
local a, cx, cy = d(301, 0, 20, 0, 0, 0, nil, 0, 0)
T.check(a == 'prepare' and cx == 256 and cy == 0, 'past 300 m: prepare snap256(p + 4v)')
a, cx, cy = d(-10, 301, 0, 25, 0, 0, nil, 0, 0)
T.check(a == 'prepare' and cx == 0 and cy == 512, 'Chebyshev distance, north')
T.check(d(301, 0, 0, 0, 0, 0, nil, 1, 5) == nil, 'prepare waits for retryAt')
T.check(d(310, 0, -60, 0, 0, 0, nil, 0, 0) == nil, 'heading back: target = current centre, no request')
local pend = { cx = 512, cy = 0 }
T.check(d(460, 0, 20, 0, 0, 0, pend, 0, 0) == nil, 'not ready: keep driving on the current block')
pend.ready = true
T.check(d(440, 0, 20, 0, 0, 0, pend, 0, 0) == nil, 'ready at 440 m: hold')
T.check(d(451, 0, 20, 0, 0, 0, pend, 0, 0) == 'commit', 'ready past 450 m: commit')
T.check(d(700, 0, 20, 0, 0, 0, pend, 0, 0) == 'commit', 'ready late: commit at once')
T.check(d(451, 600, 0, 20, 0, 0, pend, 0, 0) == 'discard', 'player left the prepared block: discard')
-- late answer: the player is past the prepared centre by more than 420 m but it is still nearer than
-- the current one (about to run off the block): take it rather than discard it
T.check(d(940, 0, 40, 0, 0, 0, pend, 0, 0) == 'commit', 'late ready block nearer than the current one: commit')
pend.ready = false
T.check(d(940, 0, 40, 0, 0, 0, pend, 0, 0) == 'discard', 'late and not ready: discard')
pend.ready = true
T.check(d(100, 600, 0, 20, 0, 0, pend, 0, 0) == 'discard', 'ready but not past 450 m and off the prepared block: discard')
-- commit gating (design 2.3): past 450 m only once the strip the move uncovers is live;
-- hard override past 700 m; a gated ready block is kept, not discarded
local gateArgs
local closed = function(ncx, ncy) gateArgs = { ncx, ncy }; return false end
local open = function() return true end
pend = { cx = 512, cy = 0, ready = true }
T.check(d(451, 0, 20, 0, 0, 0, pend, 0, 0, open) == 'commit', 'gate open: commit past 450 m')
T.check(d(451, 0, 20, 0, 0, 0, pend, 0, 0, closed) == nil and gateArgs[1] == 512 and gateArgs[2] == 0,
  'gate closed: hold (gate asked about the pending centre)')
T.check(d(699, 0, 20, 0, 0, 0, pend, 0, 0, closed) == nil, 'gate closed at 699 m: hold')
T.check(d(701, 0, 20, 0, 0, 0, pend, 0, 0, closed) == 'commit', 'hard override past 700 m')
T.check(d(940, 0, 40, 0, 0, 0, pend, 0, 0, closed) == 'commit', 'late and gated: hard override')
T.check(d(440, 0, 20, 0, 0, 0, pend, 0, 0, function() error('gate must not be asked below 450 m') end) == nil,
  'gate not evaluated below the commit distance')
local p1, p2 = f.thresholds(350, -20, 0, 0)
T.check(p1 == -50 and p2 == 100, 'UI thresholds')
-- the 1024² table has neither a fast mode nor a re-check (v1.3.1 unchanged)
T.check(f.RECHECK_S == 0 and f.FAST_MODE_V >= 1e9, '1024: no re-check, no fast mode')
tx = f.target(0, 0, 400, 0)
T.check(tx == 256, '1024 at 400 m/s: still the 420 m reach (no fast mode)')

------------------------------------------------------------------------------------------
-- M4 (docs/V1_4_SPEC.md 3.5): the 512² table, its fast mode and the commit-time re-check
local lattice = require('beamngEarth/lattice')
lattice.configure(512)
T.check(f.configure(512) == 512, 'configure(512)')
T.check(f.SIZE == 512 and f.HALF == 512 and f.GRID == 128 and f.PREPARE_DIST == 128 and f.COMMIT_DIST == 192
  and f.HARD_COMMIT_DIST == 320 and f.EDGE_MARGIN == 250 and f.REACH == 260 and f.LOOKAHEAD_S == 5
  and f.RECHECK_S == 2 and f.FAST_MODE_V == 150, '512 table active')
T.check(f.REACH_FAST == 420 and f.LOOKAHEAD_FAST_S == 3 and f.PREP_AHEAD_FAST_S == 0.75, 'fast mode: reach 420, 3 s, test 0.75 s ahead')
x0, y0 = f.blockOrigin(128, -384)
T.check(x0 == -384 and y0 == -896, '512: block position = (cx-512, cy-512)')
T.check(f.snap(63) == 0 and f.snap(64) == 128 and f.snap(-65) == -128, 'snap128')
-- the derived local REACH follows configure: 100 + 2 s * 140 = 380 -> 384, pulled back within 260 m
tx, ty = f.target(100, 0, 140, 0)
T.check(tx == 256 and ty == 0, 'target clamped to the 512 reach (' .. tx .. ')')
for _, c in ipairs({ { 0, 0, 120, 0 }, { 1000, 1000, 100, -100 }, { -333, 777, -90, 60 }, { 164, -164, 100, -100 } }) do
  local cx, cy = f.target(c[1], c[2], c[3], c[4])
  T.check(cx % 128 == 0 and cy % 128 == 0, 'target on the 128 m grid')
  T.check(c[1] - (cx - 512) >= 250 and (cx + 510) - c[1] >= 250 and c[2] - (cy - 512) >= 250 and (cy + 510) - c[2] >= 250,
    string.format('player (%g,%g) >= 250 m inside block (%d,%d)', c[1], c[2], cx, cy))
end
-- 40 m/s geometry: prepare past 128 m, commit past 192 m
T.check(d(128, 0, 40, 0, 0, 0, nil, 0, 0) == nil, '512: at 128 m: no request')
a, cx, cy = d(129, 0, 40, 0, 0, 0, nil, 0, 0)
T.check(a == 'prepare' and cx == 384 and cy == 0, '512: past 128 m: prepare snap128(p + 5 s v)')
pend = { cx = 256, cy = 0, ready = true }
T.check(d(192, 0, 40, 0, 0, 0, pend, 0, 0) == nil, '512: ready at 192 m: hold')
T.check(d(193, 0, 40, 0, 0, 0, pend, 0, 0) == 'commit', '512: ready past 192 m: commit')
T.check(d(400, 0, 40, 0, 0, 0, { cx = 0, cy = 384, ready = false }, 0, 0) == 'discard', '512: prepared block off by > 260 m: discard')
-- town speeds: the next block at least MIN_LEAD (256 m) ahead, not one grid step (a move every 128 m, each a ~50 ms frame
-- on a Deck); standing still: no lead
a, cx, cy = d(129, 0, 11, 0, 0, 0, nil, 0, 0)
T.check(a == 'prepare' and cx == 384 and cy == 0, '512 at 25 mph: prepare snap128(p + 256 m)')
a, cx, cy = d(129, 0, 0, 0, 0, 0, nil, 0, 0)
T.check(a == 'prepare' and cx == 128, '512 standing: the block round the player')
-- town drives: half the moves, never nearer than 225 m to the block's edge (1 s and 4 s heightmap latency)
do
  local function moves(speed, hdg, lat)
    local vx, vy = speed * math.cos(math.rad(hdg)), speed * math.sin(math.rad(hdg))
    local px, py, cx, cy, pending, t, n, minEdge = 0, 0, 0, 0, nil, 0, 0, math.huge
    while math.sqrt(px * px + py * py) < 5000 do
      t = t + 1 / 30
      px, py = px + vx / 30, py + vy / 30
      if pending and not pending.ready and t >= pending.at then pending.ready = true end
      local act, nx, ny = d(px, py, vx, vy, cx, cy, pending, t, 0, nil, lat)
      if act == 'prepare' then pending = { cx = nx, cy = ny, at = t + lat }
      elseif act == 'commit' then cx, cy, pending, n = pending.cx, pending.cy, nil, n + 1
      elseif act == 'discard' then pending = nil end
      local g0x, g0y, g1x, g1y = lattice.groundBox(cx, cy)
      minEdge = math.min(minEdge, px - g0x, g1x - px, py - g0y, g1y - py)
    end
    return n, minEdge
  end
  local worstN, worstE = 0, math.huge
  for _, hdg in ipairs({ 0, 20, 45 }) do
    for _, v in ipairs({ 7, 11, 20, 30 }) do
      for _, lat in ipairs({ 1.0, 4.0 }) do
        local n, e = moves(v, hdg, lat)
        worstN, worstE = math.max(worstN, n), math.min(worstE, e)
      end
    end
  end
  T.check(worstN <= 20 and worstE >= 225, string.format('town drives (16-67 mph): <= 20 moves in 5 km (%d), >= 225 m inside (%.0f m)', worstN, worstE))
end
-- 134 m/s geometry: 129 + 268 = 397 -> 384; at the commit p + 2 s v is 77 m from it: commit
a, cx = d(129, 0, 134, 0, 0, 0, nil, 0, 0)
T.check(a == 'prepare' and cx == 384, '134 m/s: prepare (384, 0)')
pend = { cx = 384, cy = 0, ready = true }
T.check(d(193, 0, 134, 0, 0, 0, pend, 0, 0, nil, 0.3) == 'commit', '134 m/s: re-check passes (p + 2 s v within 128 m)')
-- the re-check: heading 37 deg, a ready block that differs only on the minor axis (offline model:
-- committed, then replaced 0.31 s later)
local vx37, vy37 = 134 * math.cos(math.rad(37)), 134 * math.sin(math.rad(37))
pend = { cx = 4480, cy = 3456, ready = true }
T.check(d(4671.3, 3520.1, vx37, vy37, 4480, 3328, pend, 0, 0, nil, 0.3) == 'discard',
  're-check: the player would leave the prepared block within 2 s: discard (and re-prepare)')
T.check(d(4671.3, 3520.1, vx37, vy37, 4480, 3328, pend, 0, 0, nil, 1.2) == 'commit',
  're-check skipped when a re-prepare (1.2 s latency + 0.5 s) would not arrive before the 150 m edge band')
T.check(d(4671.3, 3520.1, vx37, vy37, 4480, 3328, pend, 0, 0) == 'discard'
  and d(4671.3, 3520.1, vx37, vy37, 4480, 3328, pend, 0, 0, nil, 1.1) == 'commit',
  'no latency measured: 1 s assumed (1.58 s to the edge band > 1.5 s; 1.1 s measured: 1.6 s)')
T.check(d(4801, 3520.1, vx37, vy37, 4480, 3328, { cx = 4864, cy = 3584, ready = true }, 0, 0, nil, 0.0) == 'commit',
  're-check never past the hard commit distance (321 m)')
-- the re-check assumes at least LATENCY_S (1 s): a 0.3 s measurement says little about the next request
pend = { cx = 384, cy = -128, ready = true }
T.check(d(193, 0, 133.6263447, 10, 0, 0, pend, 0, 0, nil, 0.3) == 'commit',
  're-check: 1.25 s to the edge band < 1 s + 0.5 s: commit (even though 0.3 s was measured)')
T.check(d(193, 0, 133.6263447, 10, 0, 0, pend, 0, 0, nil, 0.8) == 'commit', 're-check: 0.8 s measured: commit')
-- ... and never re-requests the centre it would discard (the target is the pending block itself)
-- (heading 80 deg: p + 2 s v is 131 m from the block, but the reach clamps the target onto it)
local vx80, vy80 = 134 * math.cos(math.rad(80)), 134 * math.sin(math.rad(80))
pend = { cx = 256, cy = 128, ready = true }
tx, ty = f.target(193, -5, vx80, vy80)
T.check(tx == 256 and ty == 128 and d(193, -5, vx80, vy80, 0, 0, pend, 0, 0, nil, 0.3) == 'commit',
  're-check: the target is the pending block: commit, not a re-request of the same centre')
-- a ready block no better than the current one where the player reaches the hard commit distance
-- blocks every new request (one at a time): dropped, so the next frame prepares the target
-- (offline model: prepared from 132 m behind the centre one grid step to the side, it was kept until
-- 260 m / fast mode 420 m, and a car accelerating to 200 m/s at 0.3-1.2 s latency left the block)
pend = { cx = 0, cy = 128, ready = true }
T.check(d(150, -10, 134, 0, 0, 0, pend, 0, 0, nil, 0.3) == 'discard', 'a block beside the way: discard')
a, tx, ty = d(150, -10, 134, 0, 0, 0, nil, 0, 0, nil, 0.3)
T.check(a == 'prepare' and tx == 384 and ty == 0, '... and the next frame prepares (384, 0)')
T.check(d(150, -10, 134, 0, 0, 0, { cx = 0, cy = 128, ready = false }, 0, 0, nil, 0.3) == nil, 'not ready yet: kept')
T.check(d(-100, 0, 134, 0, 0, 0, { cx = 128, cy = 0, ready = true }, 0, 0, nil, 0.3) == nil,
  'a block ahead (better by the hard commit distance): kept')
T.check(d(-30, 0, 200, 0, 0, 0, { cx = 384, cy = 0, ready = true }, 0, 0, nil, 0.3) == nil,
  'fast mode: the block 3 s ahead prepared behind the centre: kept')
T.check(d(150, -10, 0, 0, 0, 0, pend, 0, 0, nil, 0.3) == nil, 'at rest: kept')
T.near(f.timeTo(0, 0, 100, 0, 0, 0, 360), 3.6, 1e-12, 'timeTo along x')
T.near(f.timeTo(0, 0, -100, 50, 0, 0, 360), 3.6, 1e-12, 'timeTo: the first axis to reach the distance')
T.check(f.timeTo(400, 0, 100, 0, 0, 0, 360) == 0 and f.timeTo(5, 5, 0, 0, 0, 0, 360) == math.huge, 'timeTo: past it / at rest')
-- fast mode (|v| > 150 m/s): the prepare test 0.75 s ahead, target 3 s ahead within 420 m
T.check(d(0, 0, 150, 0, 0, 0, nil, 0, 0) == nil, '150 m/s at the centre: normal mode, nothing')
a, cx, cy = d(0, 0, 200, 0, 0, 0, nil, 0, 0)
T.check(a == 'prepare' and cx == 384 and cy == 0, '200 m/s: the test point 150 m ahead prepares; 600 m -> clamped to 384')
T.check(f.reachFor(200, 0) == 420 and f.reachFor(100, 0) == 260, 'reach per mode')
T.check(d(-40, 0, 200, 0, 0, 0, { cx = 384, cy = 0 }, 0, 0) == 'discard', 'fast mode: a block 424 m away is discarded')
T.check(d(-30, 0, 200, 0, 0, 0, { cx = 384, cy = 0 }, 0, 0) == nil, 'fast mode: 414 m is within the 420 m reach')
pend = { cx = 4480, cy = 3456, ready = true }
T.check(d(4671.3, 3520.1, 1.2 * vx37, 1.2 * vy37, 4480, 3328, pend, 0, 0, nil, 0.0) == 'commit', 'fast mode: no re-check')

-- A prefetched centre is the effective request target. Comparing a ready answer only against the raw target
-- discarded it and requested that same cached centre again on alternate frames (I-95 dev.2 capture).
f.setReady({ { 256, 0 } })
a, cx, cy = d(193, 0, 100, 0, 0, 0, nil, 0, 0, nil, 0.002)
T.check(a == 'prepare' and cx == 256 and cy == 0, 'ready cache: request the cached centre instead of raw target 384')
pend = { cx = 256, cy = 0, ready = true }
T.check(d(194, 0, 100, 0, 0, 0, pend, 0, 0, nil, 0.002) == 'commit',
  'ready cache: do not discard the centre that the next request would select again')
T.check(d(194, 0, 100, 0, 0, 0, pend, 0, 0, closed, 0.002) == nil,
  'ready cache: accepting the effective target still respects the uncovered-strip gate')
T.check(d(321, 0, 100, 0, 0, 0, pend, 0, 0, closed, 0.002) == 'commit',
  'ready cache: past hard commit the better block is accepted even if gated')
T.check(d(194, 0, 200, 0, 0, 0, pend, 0, 0, nil, 0.002) == 'commit',
  'ready cache: fast mode still accepts a better ready block without rechecking')
T.check(d(500, 0, 100, 0, 0, 0, pend, 0, 0, closed, 4) == 'commit',
  'ready cache: a late answer near the current edge still commits immediately')
do
  local current, pending, requests, discards, commits = 0, nil, 0, 0, 0
  for frame = 0, 9 do
    local t = frame / 60
    local act, nx, ny = d(193 + 100 * t, 0, 100, 0, current, 0, pending, t, 0, nil, 0.002)
    if act == 'prepare' then pending, requests = { cx = nx, cy = ny, ready = true }, requests + 1
    elseif act == 'discard' then pending, discards = nil, discards + 1
    elseif act == 'commit' then current, pending, commits = pending.cx, nil, commits + 1 end
  end
  print(string.format('  ready-cache replay (10 frames, 100 m/s): %d requests, %d discards, %d commits', requests, discards, commits))
  T.check(requests == 1 and discards == 0 and commits == 1,
    'ready cache: bounded replay commits once without a request/discard loop')
end
-- A ready candidate beside/behind the route must not gain immunity from the existing hard-distance recheck.
-- At x=-100, y=129 heading east it is within two grid steps of the raw target but no better at hard commit.
f.setReady({ { 0, -128 } })
a, cx, cy = d(-100, 129, 100, 0, 0, 0, nil, 0, 0, nil, 0.002)
T.check(a == 'prepare' and cx == 128 and cy == 128, 'ready cache: reject a candidate that is no better at hard commit')
T.check(d(-100, 129, 100, 0, 0, 0, { cx = 0, cy = -128, ready = true }, 0, 0, nil, 0.002) == 'discard',
  'ready cache: an existing unsafe side-block can still be discarded and replaced')
-- If the server withdraws the cached candidate, or a turn makes it unusable, re-target normally.
f.setReady({})
T.check(d(194, 0, 100, 0, 0, 0, { cx = 256, cy = 0, ready = true }, 0, 0, nil, 0.002) == 'discard',
  'ready cache removed: raw target can replace a short-lived pending centre')
f.setReady({ { 256, 0 } })
T.check(d(100, 300, 0, 100, 0, 0, { cx = 256, cy = 0, ready = true }, 0, 0, nil, 0.002) == 'discard',
  'ready cache: a turn beyond the pending block reach still discards it')
f.setReady({})

-- drives (the offline model of section 3.5 with the real rule): a straight run per heading at
-- 60 frames/s, heightmap latency L (measured by the caller and passed back), commits granted at once
local function drive(speed, hdg, lat, ox, oy, dist, cached)
  local vx, vy = speed * math.cos(math.rad(hdg)), speed * math.sin(math.rad(hdg))
  local px, py, cx, cy = ox, oy, 0, 0
  local pending, t, last, minGap, minEdge, commits = nil, 0, nil, math.huge, math.huge, 0
  local dt = 1 / 60
  while math.sqrt((px - ox) ^ 2 + (py - oy) ^ 2) < dist do
    t = t + dt
    px, py = px + vx * dt, py + vy * dt
    if pending and not pending.ready and t >= pending.at then pending.ready = true end
    if cached then
      -- An available block one grid step behind the geometric target, plus any request already finished.
      local tx, ty = f.target(px, py, vx, vy)
      local bx = math.abs(vx) >= math.abs(vy) and (vx > 0 and f.GRID or -f.GRID) or 0
      local by = math.abs(vy) > math.abs(vx) and (vy > 0 and f.GRID or -f.GRID) or 0
      local ready = { { tx - bx, ty - by } }
      if pending and pending.ready then ready[#ready + 1] = { pending.cx, pending.cy } end
      f.setReady(ready)
    end
    local act, nx, ny = d(px, py, vx, vy, cx, cy, pending, t, 0, nil, lat)
    if act == 'prepare' then pending = { cx = nx, cy = ny, at = t + lat }
    elseif act == 'commit' then
      cx, cy, pending = pending.cx, pending.cy, nil
      if last then minGap = math.min(minGap, t - last) end
      last, commits = t, commits + 1
    elseif act == 'discard' then pending = nil end
    local g0x, g0y, g1x, g1y = lattice.groundBox(cx, cy)
    minEdge = math.min(minEdge, px - g0x, g1x - px, py - g0y, g1y - py)
  end
  if cached then f.setReady({}) end
  return minGap, minEdge, commits
end
local worst = { gap = math.huge, edge = math.huge, edgeFast = math.huge, edge250 = math.huge }
for hdg = 0, 45 do
  for _, o in ipairs({ { 0, 0 }, { 37, -91 }, { -64, 64 } }) do
    local g, e = drive(134, hdg, 0.3, o[1], o[2], 6000)
    worst.gap, worst.edge = math.min(worst.gap, g), math.min(worst.edge, e)
    for _, lat in ipairs({ 0.6, 1.2 }) do
      local _, e2 = drive(134, hdg, lat, o[1], o[2], 6000)
      worst.edge = math.min(worst.edge, e2)
    end
    local _, ef = drive(200, hdg, 1.2, o[1], o[2], 6000)
    worst.edgeFast = math.min(worst.edgeFast, ef)
    local _, e250 = drive(250, hdg, 0.3, o[1], o[2], 6000)
    worst.edge250 = math.min(worst.edge250, e250)
  end
end
print(string.format('  512 drives: min commit gap %.2f s (134 m/s), min edge %.0f m (134), %.0f m (200, 1.2 s), %.0f m (250, 0.3 s)',
  worst.gap, worst.edge, worst.edgeFast, worst.edge250))
T.check(worst.gap >= 0.9, string.format('134 m/s, headings 0-45 deg, 0.3 s latency: commits >= 0.9 s apart (min %.2f s)', worst.gap))
T.check(worst.edge >= 150, string.format('134 m/s at 0.3 / 0.6 / 1.2 s latency: never within 150 m of an edge (min %.0f m)', worst.edge))
T.check(worst.edgeFast >= f.EDGE_MARGIN_FAST, string.format('200 m/s, 1.2 s latency (fast mode): on the block, >= 90 m inside (min %.0f m)', worst.edgeFast))
T.check(worst.edge250 >= f.EDGE_MARGIN_FAST, string.format('250 m/s with a warm DEM (0.3 s): on the block (min %.0f m)', worst.edge250))
local cachedNormal, cachedFast = math.huge, math.huge
for hdg = 0, 359, 7 do
  for _, lat in ipairs({ 0.3, 1.2 }) do
    local _, en = drive(100, hdg, lat, 37, -91, 6000, true)
    local _, ef = drive(200, hdg, lat, 37, -91, 6000, true)
    cachedNormal, cachedFast = math.min(cachedNormal, en), math.min(cachedFast, ef)
  end
end
print(string.format('  ready-cache drives: min edge %.0f m (100 m/s), %.0f m (200 m/s)', cachedNormal, cachedFast))
T.check(cachedNormal >= 150, 'ready-cache drives: normal mode keeps at least 150 m edge margin')
T.check(cachedFast >= f.EDGE_MARGIN_FAST, 'ready-cache drives: fast mode keeps at least 90 m edge margin')

-- the same with the latency of each request drawn from 0.3-1.2 s (the rule only knows the last one)
-- and the commit landing in a heavy slot 0-0.15 s later; all headings, and a car accelerating from
-- 100 to 200 m/s through the fast mode (the review's model: 78 m at 134 m/s and up to 121 m off the
-- block accelerating, before the two rules above)
local function jitterDrive(speed, acc, hdg, ox, oy, dist)
  local px, py, cx, cy = ox, oy, 0, 0
  local pending, t, measured, commitAt, minEdge, gone = nil, 0, nil, nil, math.huge, 0
  local dt = 1 / 60
  while gone < dist do
    t = t + dt
    speed = math.min(200, speed + acc * dt)
    local vx, vy = speed * math.cos(math.rad(hdg)), speed * math.sin(math.rad(hdg))
    px, py, gone = px + vx * dt, py + vy * dt, gone + speed * dt
    if pending and not pending.ready and t >= pending.at then pending.ready, measured = true, pending.lat end
    if commitAt and t >= commitAt then cx, cy, pending, commitAt = pending.cx, pending.cy, nil, nil end
    if not commitAt then
      local act, nx, ny = d(px, py, vx, vy, cx, cy, pending, t, 0, nil, measured)
      if act == 'prepare' then
        local lat = 0.3 + 0.9 * math.random()
        pending = { cx = nx, cy = ny, at = t + lat, lat = lat }
      elseif act == 'commit' then commitAt = t + 0.15 * math.random()
      elseif act == 'discard' then pending = nil end
    end
    local g0x, g0y, g1x, g1y = lattice.groundBox(cx, cy)
    minEdge = math.min(minEdge, px - g0x, g1x - px, py - g0y, g1y - py)
  end
  return minEdge
end
math.randomseed(20261010)
local jit134, jitAcc = math.huge, math.huge
for hdg = 0, 359, 7 do
  for _, o in ipairs({ { 0, 0 }, { 37, -91 } }) do
    jit134 = math.min(jit134, jitterDrive(134, 0, hdg, o[1], o[2], 8000))
    jitAcc = math.min(jitAcc, jitterDrive(100, 8, hdg, o[1], o[2], 8000))
  end
end
print(string.format('  512 jittered drives: min edge %.0f m (134 m/s), %.0f m (100 -> 200 m/s)', jit134, jitAcc))
T.check(jit134 >= 150, string.format('134 m/s, 0.3-1.2 s latency per request, all headings: >= 150 m inside (min %.0f m)', jit134))
T.check(jitAcc >= f.EDGE_MARGIN_FAST, string.format('100 -> 200 m/s through the fast mode: >= 90 m inside (min %.0f m)', jitAcc))

-- back to 1024²: the table and the derived locals
lattice.configure(1024)
T.check(f.configure(1024) == 1024 and f.configure(4096) == 1024, 'configure(1024); unknown sizes -> 1024')
T.check(f.SIZE == 1024 and f.HALF == 1024 and f.GRID == 256 and f.REACH == 420 and f.PREPARE_DIST == 300
  and f.RECHECK_S == 0, '1024 table restored')
tx = f.target(0, 0, 200, 0)
T.check(tx == 256, '1024: derived REACH restored (clamped lead 256)')

-- the altitude rule (follow.altitudeHold): holds high above the ground ahead, follows again on the way down
T.check(not f.altitudeHold(false, 1000, 0, nil) and not f.altitudeHold(true, 1000, 0, nil), 'no ground known: never holds')
T.check(not f.altitudeHold(false, 250, 0, 0) and f.altitudeHold(false, 350, 0, 0), 'holds above HOLD_AGL (300 m)')
T.check(f.altitudeHold(true, 200, 0, 0) and not f.altitudeHold(true, 140, 0, 0), 'hysteresis: held until RESUME_AGL (150 m)')
T.check(not f.altitudeHold(true, 500, -50, 0), 'a 50 m/s descent from 500 m resumes (8 s ahead: 100 m)')
T.check(not f.altitudeHold(false, 400, -20, 0), 'a 20 m/s descent at 400 m does not start holding')
T.check(f.altitudeHold(false, 1400, 0, 1000), 'heights above the ground ahead, not above z = 0')

T.done('test_follow')
