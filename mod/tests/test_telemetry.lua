local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local cap = require('beamngEarth/telemetry')
local calls, sent, allow = 0, {}, true
local function context(full) calls = calls + 1; return { speed_mps = 107.2896, full = full } end
local function send(row) sent[#sent + 1] = row; return allow end
local function frame(ms, sim, marks, hitch)
  cap.simulation(sim)
  return cap.frame(ms, marks or { roads = 2 }, hitch, context, function() return { Ticking = 4 } end, send)
end
frame(1200, 1.2)
T.check(calls == 0 and #sent == 0, 'disabled capture does no sampling or sending')
cap.setStatus({ available = true, active = true, session = 'a', include_location = false })
for i = 1, 99 do frame(10, 0.01) end
T.check(#sent == 0 and calls == 0, 'ordinary frames do not walk world objects or send packets')
local row = frame(10, 0.01)
T.check(#sent == 1 and calls == 1 and row.frame.frames == 100, 'one full context and sample at one second')
T.near(row.frame.wall_ms, 1000, 1e-6, 'actual interval sum')
T.near(row.frame.sim_wall_ratio, 1, 1e-6, 'observed sim time against actual wall time')
T.check(row.frame.histogram[5] == 100 and #row.frame.histogram == 19, 'fixed histogram count')
T.check(row.frame.p99_ms == 12 and row.mod_ms.roads == 200, 'histogram upper bound and summed mod timings')
local marks, eng = { terrainCommit = 120, roads = 20, road_walks = 15, gasstations = 3, sea_trees = 8 }, { RenderShadows = 160 }
frame(200, 0.1, marks, { engine = eng })
marks.terrainCommit, eng.RenderShadows = 9999, 9999
for i = 1, 8 do row = frame(100, 0) end
T.check(row.frame.frames == 9 and row.frame.over_100 == 1 and row.frame.over_50 == 9, 'hitch counts include all frames')
T.check(row.worst.ms == 200 and row.worst.mod_ms.terrainCommit == 120 and row.worst.engine_ms.RenderShadows == 160,
  'worst hitch preserves that frame instead of aliasing mutable timer tables')
T.check(row.worst.mod_ms.road_walks == 15 and row.worst.mod_ms.gasstations == 3
  and row.mod_ms.road_walks == 15 and row.worst.mod_ms.sea_trees == 8,
  'road subdivisions, gas-station and vegetation work survive window and hitch capture')
T.check(row.worst.context.speed_mps == 107.2896 and row.context.full, 'hitch context and full end-of-window context')
T.check(row.frame.paused_frames == 8, 'paused frames remain explicit')
T.near(row.frame.sim_wall_ratio, 0.1, 1e-6, 'pause / physics slowdown is not hidden')
allow = false
row = frame(1100, 1)
T.check(row.seq == 3 and row.frame.wall_ms == 1100, 'slow frame is one actual-duration window, not fabricated samples')
allow = true
row = frame(1000, 1)
T.check(row.seq == 4 and row.context.telemetry_dropped == 1, 'dropped windows have sequence gaps and a cumulative counter')
T.check(cap.frame(0 / 0) == nil and cap.frame(-1) == nil and cap.frame(math.huge) == nil, 'bad timings are ignored')
row = frame(6500, nil)
T.check(row.frame.histogram[19] == 1 and row.frame.p99_ms == 6500, 'overflow bucket uses its observed maximum')
T.check(row.frame.sim_wall_ratio == nil and row.frame.sim_wall_ms == 0, 'missing simulation observations remain unknown')
cap.stopLocal()
cap.setStatus({ available = true, active = true, session = 'a' })
T.check(not cap.isActive(), 'in-flight stale status cannot undo a local stop')
local n = #sent
frame(2000, 1)
T.check(#sent == n, 'stopped capture does not emit')
cap.startRequested()
cap.setStatus({ available = true, active = true, session = 'b' })
row = frame(1000, 1)
T.check(row.seq == 1 and row.elapsed_s == 1 and row.context.telemetry_dropped == 0, 'new capture resets sequence / timing / drops')
cap.disconnected()
cap.setStatus({ available = true, active = true, session = 'b' })
T.check(not cap.isActive(), 'reconnect does not resume an old capture silently')
cap.startRequested()
cap.setStatus({ available = true, active = true, session = 'c' })
local dirty = { x = 0 / 0, y = math.huge, z = -1, secret = 'not numeric', good = 3 }
row = frame(1000, 1, dirty)
T.check(row.mod_ms.good == 3 and row.mod_ms.x == nil and row.mod_ms.secret == nil and row.mod_ms.z == nil,
  'nonfinite / negative / string timer values never escape')
cap.setStatus({ available = true, active = false })
T.check(not cap.isActive(), 'server limit / disk failure can stop collection')
T.done('test_telemetry')
