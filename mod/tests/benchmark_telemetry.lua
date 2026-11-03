-- Synthetic collector cost, not a BeamNG FPS benchmark. No JSON encoding, scene
-- scans, socket or disk I/O. Real capture overhead is recorded as mod_ms.telemetry.
-- Run: python tools/run_lua_tests.py benchmark_telemetry
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
dofile(dir .. '/testlib.lua')
local cap = require('beamngEarth/telemetry')
local marks = { recv = 0.02, roads = 0.3, trees = 0.7, ui = 0.1, far = 0.8, terrainCommit = 0,
  gc = 0.1, weather = 0.01, sky = 0.01, telemetry = 0.002 }
local function context() return { speed_mps = 107.2896, trees_pending = 400, far_queued = 32, lua_mb = 170 } end
local function engine() return { Ticking = 3, RenderShadows = 2, Lua = 2 } end
local samples = 0
local function send() samples = samples + 1; return true end
local function run(enabled, count)
  cap.startRequested()
  cap.setStatus({ available = true, active = enabled, session = tostring(enabled) .. tostring(count) })
  collectgarbage('collect')
  local before, started = collectgarbage('count'), os.clock()
  for i = 1, count do
    cap.simulation(1 / 60)
    cap.frame(1000 / 60, marks, nil, context, engine, send)
  end
  local seconds = os.clock() - started
  collectgarbage('collect')
  return seconds, collectgarbage('count') - before
end
run(false, 10000)
run(true, 10000)
local count = 1000000
print('Synthetic collector: 1,000,000 frames at 60 Hz, 10 existing mod timers, five alternating trials')
print('enabled,seconds,us_per_frame,retained_kib_after_gc')
for i = 1, 5 do
  for _, on in ipairs({ false, true }) do
    local sec, memory = run(on, count)
    print(string.format('%s,%.6f,%.6f,%.3f', tostring(on), sec, sec * 1e6 / count, memory))
  end
end
