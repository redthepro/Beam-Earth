local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
local function selectBuild(flag, missing)
  package.loaded['beamngEarth/buildinfo'] = nil
  package.loaded['beamngEarth/diagnostics'] = nil
  package.preload['beamngEarth/buildinfo'] = function()
    if missing then error('missing build metadata') end
    return { devDiagnostics = flag }
  end
  return require('beamngEarth/diagnostics')
end
local providerLoads = 0
package.preload['beamngEarth/diagnostics_capture'] = function()
  providerLoads = providerLoads + 1
  error('production must not load the dev provider')
end
for _, flag in ipairs({ false, 'true', 1 }) do
  local d = selectBuild(flag)
  d.setActive(true)
  d.frame(1000)
  T.check(not d.enabled() and not d.event('player_jump', {}) and d.drain() == nil,
    'stable/nonboolean build flag remains an allocation-free noop')
end
local missing = selectBuild(nil, true)
T.check(not missing.enabled() and missing.drain() == nil and providerLoads == 0,
  'missing metadata fails closed without importing the dev provider')
package.preload['beamngEarth/diagnostics_capture'] = nil
local d = selectBuild(true)
T.check(not d.enabled() and d.drain() == nil and not d.event('frame_shift', {}),
  'dev provider remains inactive until an explicit capture')
d.frame(100)
d.setActive(true)
d.frame(16)
local values = { anchor_id = 2, previous_anchor_id = 1, phase = 3, distance_m = 17.5,
  vertical_speed_mps = -12, ready = false, mode = 4, holds = 1, rescues = 0,
  lat = 37, lon = -77, key = 'PRIVATE', path = 'PRIVATE', token = 'PRIVATE',
  reason = 'PRIVATE', speed_mps = 0 / 0, retained = math.huge, adds = {}, removes = 1e16 }
T.check(d.event('frame_shift', values), 'known event accepted')
values.anchor_id = 999
T.check(not d.event('PRIVATE', {}) and not d.event({}, {}), 'arbitrary event names rejected')
d.frame(24)
d.event('drop_hold', { reason = 2, waiting = true, retired = 5 })
local row = d.drain()
local first, second = row.events[1], row.events[2]
T.check(#row.events == 2 and row.dropped == 0 and first.anchor_id == 2,
  'queue snapshots allowed fields without aliasing caller tables')
T.check(first.frame == 1 and first.seq == 1 and first.elapsed_ms == 16
  and second.frame == 2 and second.seq == 2 and second.elapsed_ms == 40,
  'capture-local frame, event ordering and elapsed time survive a drain')
T.check(first.vertical_speed_mps == -12 and first.ready == false and second.waiting == true,
  'signed measurements and false boolean values survive')
T.check(first.lat == nil and first.lon == nil and first.key == nil and first.path == nil
  and first.token == nil and first.reason == nil and first.speed_mps == nil
  and first.retained == nil and first.adds == nil and first.removes == nil,
  'coordinates, private strings, nested values, nonfinite and oversized numbers are omitted')
T.check(#d.drain().events == 0 and #row.events == 2, 'drain detaches ownership and clears the bounded queue')
for i = 1, 10000 do d.event('far_swap', { adds = i }) end
local full = d.drain()
T.check(#full.events == 32 and full.dropped == 9968 and full.events[32].adds == 32,
  'event storm keeps a fixed first-32 chronology and reports every overflow')
d.frame(0 / 0)
d.frame(-1)
d.frame(math.huge)
d.event('far_retire', { retired = 7 })
local after = d.drain()
T.check(after.events[1].seq == 10003 and after.events[1].frame == 5
  and after.events[1].elapsed_ms == 40 and after.dropped == 0,
  'overflow gaps persist in sequence while malformed durations cannot corrupt elapsed time')
d.setActive(true)
d.event('edge_guard', {})
T.check(d.drain().events[1].seq == 10004, 'repeated active status does not reset chronology')
d.event('player_jump', {})
d.setActive(false)
T.check(not d.enabled() and d.drain() == nil and not d.event('player_jump', {}),
  'stopping disables probes and discards queued old-session data')
d.setActive(true)
d.event('far_rebase', {})
local fresh = d.drain().events[1]
T.check(fresh.frame == 0 and fresh.seq == 1 and fresh.elapsed_ms == 0,
  'a new capture begins a fresh chronology, even before its first update')
d.frame(10)
d.reset()
d.event('player_jump', {})
T.check(d.enabled() and d.drain().events[1].seq == 1, 'reset supports active capture-session rollover')
T.done('test_diagnostics')
